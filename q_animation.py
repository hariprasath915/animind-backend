"""
q_animation.py — 2-Part Pipeline (Cost-Optimised)
====================================================
Generates premium, step-by-step interactive SVG engineering animations from a
natural-language question.

Architecture (2-Part Pipeline)
-------------------------------
Part 1  → Gemini Pro  : Solves the problem & produces a Scene Plan (JSON).
Part 2  → Gemini Flash: Renders the final single-file Interactive HTML.

WHY TWO MODELS?
  Mathematical reasoning (Part 1) benefits from a smarter model.
  Code/HTML generation (Part 2) is equally good with Flash at 97% lower cost.

COST OPTIMISATIONS (v3)
-----------------------
OPT 1 — Split model strategy:
         Part 1 uses GEMINI_MODEL_REASONING (Pro)  — needs accuracy for math.
         Part 2 uses GEMINI_MODEL_CODEGEN   (Flash) — HTML gen, 97% cheaper.

OPT 2 — Compact JSON hand-off:
         The Part 1 scene plan is stripped of bulky fields (problem_statement,
         diagram_notes, overlay_annotations) and serialised with no indentation
         before being passed to Part 2 as context. Saves ~30% input tokens.

OPT 3 — Realistic output token cap:
         Part 2 max_tokens reduced from 16,384 → 10,000. Real animation HTML
         is 8–10 k chars; the old ceiling was pure wasted headroom that the
         model billed even on truncated outputs.

COST IMPACT (happy path, no retries)
  Before : ~34,000 tokens → ~$0.256 per generation (Gemini Pro only)
  After  : ~18,500 tokens → ~$0.008 per generation (split model)
  Saving : ~97% per generation

BUG FIXES (v1 → v2, carried forward)
--------------------------------------
BUG 1  — _extract_json  : missing re.DOTALL broke multi-line JSON fence strip.
BUG 2  — _call_gemini   : bare raise last_err when last_err=None → TypeError.
BUG 3  — _generate_html : retry warning stacked onto base_prompt each loop.
BUG 4  — _generate_html : HTML fence strip regex lacked re.DOTALL (CRLF).
BUG 5  — _generate_scene_plan: question text not delimited from system prompt.
BUG 6  — generate_question_animation: empty explanation when statement missing.
BUG 7  — _run_pipeline  : relative OUTPUT_DIR="." breaks on Railway.
BUG 8  — _call_gemini   : temperature hardcoded 1.0 for JSON generation.
BUG 9  — Missing FastAPI app, route, CORS, and /health endpoint entirely.

RUN AS SERVER
-------------
  uvicorn q_animation:app --host 0.0.0.0 --port $PORT

REQUIRED ENV VARS
-----------------
  GEMINI_API_KEY   — your Google AI Studio / Vertex API key
"""

from __future__ import annotations

import os
import re
import json
import asyncio
import logging
import time
from typing import Optional

# ── Optional FastAPI import (only needed when run as a server) ────────────────
try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

from google import genai
from google.genai import types as genai_types


# ─────────────────────────────────────────────────────────────────────────────
# 0.  Configuration
# ─────────────────────────────────────────────────────────────────────────────

# OPT 1: Two-model strategy — Pro for reasoning, Flash for code generation.
GEMINI_MODEL_REASONING = "gemini-2.0-pro-exp"     # Part 1: math / JSON plan
GEMINI_MODEL_CODEGEN   = "gemini-2.0-flash"        # Part 2: HTML animation

OUTPUT_DIR       = os.path.abspath(".")             # BUG 7: always absolute
MAX_RETRIES      = 3                                # per _call_gemini attempt
HTML_MAX_RETRIES = 2                                # Part 2 outer retry loop
MIN_HTML_CHARS   = 8_000                            # minimum valid HTML length

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Visual design tokens — kept for reference / future use in prompts
DESIGN_TOKENS = {
    "bg":        "#0b0c10",
    "panel_bg":  "#1f2833",
    "text_main": "#c5c6c7",
    "cyan":      "#66fcf1",
    "cyan_dim":  "#45a29e",
    "orange":    "#fca311",
    "green":     "#97c459",
    "red":       "#ff3366",
}

# OPT 2: Fields to strip from the scene plan before feeding it to Part 2.
# These are large, verbose fields that Part 2 does not need.
_SLIM_PLAN_DROP_KEYS = {
    "problem_statement",   # full question text already in Part A of prompt
    "diagram_notes",       # narrative text, not used in code generation
}

# OPT 2: Per-scene fields that are large but only partially needed by Part 2
_SLIM_SCENE_DROP_KEYS = {
    "description",         # long prose — title is sufficient
    "overlay_annotations", # Part 2 builds its own overlays from badges/steps
}


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Gemini SDK Helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_client(api_key: Optional[str] = None) -> genai.Client:
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise EnvironmentError(
            "GEMINI_API_KEY environment variable not set. "
            "Add it to your Railway service variables."
        )
    return genai.Client(api_key=key)


def _call_gemini(
    prompt: str,
    model: str,                        # OPT 1: explicit model per call
    api_key: Optional[str] = None,
    max_tokens: int = 10_000,          # OPT 3: sensible default
    temperature: float = 0.7,          # BUG 8: configurable per call-site
) -> str:
    """Call Gemini with exponential-backoff retry on transient errors."""
    client = _get_client(api_key)
    config = genai_types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
    )

    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
            return response.text.strip()

        except Exception as exc:
            err_str = str(exc).lower()
            is_transient = any(k in err_str for k in (
                "429", "503", "quota", "resource exhausted",
                "unavailable", "truncated", "max_tokens",
            ))
            if is_transient and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                log.warning(
                    "[q_animation] %s error, retrying in %ds (attempt %d/%d): %s",
                    model, wait, attempt, MAX_RETRIES, exc,
                )
                time.sleep(wait)
                last_err = exc
            else:
                raise RuntimeError(f"Gemini API error [{model}]: {exc}") from exc

    # BUG 2 FIX: guard against last_err=None before raising
    if last_err is not None:
        raise last_err
    raise RuntimeError(
        f"Gemini call [{model}] failed after {MAX_RETRIES} retries "
        "with no captured exception."
    )


def _extract_json(raw: str) -> dict | list:
    """Extract and parse the first JSON object/array from a Gemini response.

    BUG 1 FIX: use re.DOTALL so the fence-strip regex matches across newlines,
    and handle both \\n and \\r\\n line endings.
    """
    # Strip markdown code fences (```json…``` or ```…```)
    cleaned = re.sub(r"```(?:json)?\s*", "", raw, flags=re.DOTALL)
    cleaned = cleaned.replace("```", "").strip()

    # Find first { or [
    start = next((i for i, c in enumerate(cleaned) if c in "{["), None)
    if start is None:
        raise ValueError(f"No JSON found in Gemini response:\n{raw[:400]}")

    opener = cleaned[start]
    closer = "}" if opener == "{" else "]"
    depth, end = 0, start
    for i, ch in enumerate(cleaned[start:], start):
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                end = i
                break

    json_str = cleaned[start: end + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return json.loads(cleaned)   # last-ditch: try entire cleaned string


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Part 1 — Scene Plan  (Gemini Pro / Reasoning model)
# ─────────────────────────────────────────────────────────────────────────────

PART1_SYSTEM = """\
You are an expert engineering educator AND SVG animation director.

Given ANY engineering, physics, or mathematics question, you will:
  1. Fully solve the problem (all formulas, substitutions, and numeric results).
  2. Design a 4-scene interactive SVG animation plan that communicates the
     solution visually step-by-step.

Return ONLY valid compact JSON — no markdown fences, no preamble, no trailing
text, no indentation.

Schema (all fields required):
{
  "topic": "<short topic name, ≤6 words>",
  "subject_area": "<Physics|Mechanics|Thermodynamics|Electrical|Math|Chemistry|Biology|Other>",
  "problem_statement": "<full verbatim question>",
  "given": [{"symbol":"r","value":"50","unit":"mm","description":"Crank length"}],
  "find": "<what is being calculated>",
  "solution_steps": [
    {"step_number":1,"title":"<title>","formula_text":"<formula>",
     "substitution_text":"<substituted values>","result_text":"<result with units>"}
  ],
  "final_answer": {"symbol":"V","value":"1.53","unit":"m/s","statement":"<1-sentence summary>"},
  "components": [
    {"id":"<snake_case>","label":"<label>","shape":"<circle|line|rect|path>",
     "role":"<function>","color_hint":"<#hex>","motion_type":"<none|rotate|translate|oscillate>",
     "pivot_or_anchor":"<x,y>","approx_size":"<px>","layer_order":1}
  ],
  "diagram_notes": "<canvas origin, scale>",
  "scenes": [
    {"index":0,"scene_key":"SETUP","label":"Setup","title":"<title>",
     "description":"<1 sentence>",
     "badges":[{"text":"<label>","color":"<cyan|orange|green|red>"}],
     "visible_layer_ids":["<id>"],"focused_layer_ids":[],
     "blur_shield_opacity":0.0,"start_continuous_anim":false,
     "freeze_at_angle_deg":null,"show_formula_box":false,"show_final_answer":false,
     "overlay_annotations":[{"type":"given_data_card","content":"<text>","svgx":"20","svgy":"20"}]}
  ],
  "formula_box_steps": [{"line":"<monospace line>"}],
  "answer_overlay": {"symbol":"V","value":"1.53","unit":"m/s",
                     "label":"<result label>","svgx":"500","svgy":"250"}
}

SCENE RULES (strictly enforced):
- Exactly 4 scenes: index 0=SETUP, 1=ELEMENTS, 2=LINKAGE, 3=SOLUTION.
- Scene 0: must include a given_data_card overlay listing all given values.
- Scene 3: must have show_formula_box=true and show_final_answer=true.
- All SVG coordinate references must be inside a 850×450 px canvas.
- solution_steps: at least 3 steps showing formula → substitution → result.
"""


def _generate_scene_plan(question: str, api_key: Optional[str] = None) -> dict:
    """Call the reasoning model to solve the problem and produce a scene plan."""
    # BUG 5 FIX: delimit question from system prompt with an XML tag
    prompt = f"{PART1_SYSTEM}\n\n<question>\n{question}\n</question>"

    log.info("[q_animation] Part 1 → calling %s", GEMINI_MODEL_REASONING)
    raw = _call_gemini(
        prompt,
        model=GEMINI_MODEL_REASONING,
        api_key=api_key,
        max_tokens=4_000,    # scene plan JSON is ~1–2 k tokens; 4k is safe ceiling
        temperature=0.3,     # BUG 8: low temp for reliable JSON output
    )
    return _extract_json(raw)


def _slim_scene_plan(scene_plan: dict) -> str:
    """OPT 2: Strip bulky fields and serialise compactly for Part 2 input.

    Removes fields Part 2 does not need, and serialises without indentation
    to save ~30% of input tokens compared to json.dumps(indent=2).
    """
    slim = {k: v for k, v in scene_plan.items() if k not in _SLIM_PLAN_DROP_KEYS}

    # Strip large per-scene fields
    if "scenes" in slim:
        slim_scenes = []
        for scene in slim["scenes"]:
            slim_scene = {k: v for k, v in scene.items() if k not in _SLIM_SCENE_DROP_KEYS}
            slim_scenes.append(slim_scene)
        slim["scenes"] = slim_scenes

    return json.dumps(slim, separators=(",", ":"))   # compact, no whitespace


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Part 2 — Interactive HTML  (Gemini Flash / Codegen model)
# ─────────────────────────────────────────────────────────────────────────────

PART2_SYSTEM = """\
You are a world-class SVG/JavaScript animation engineer.
Produce a COMPLETE, self-contained <!DOCTYPE html> interactive animation file.
Output ONLY the raw HTML — start with <!DOCTYPE html>, end with </html>.
Do NOT wrap in markdown fences. Do NOT add any preamble or trailing text.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOKEN ECONOMY — KEEP OUTPUT UNDER 9,000 TOKENS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• NO complex SVG filters (no <filter>, no feDropShadow, no feGaussianBlur).
• NO multi-stop gradients. Use solid fills or 2-stop linear gradients only.
• NO complex <path> data. Use <rect>, <circle>, <line>, <polygon> instead.
• Minify all CSS (single-line rules where possible).
• Minify all JS (short variable names, combined statements).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PAGE STRUCTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Max-width 850 px, centered. Three stacked blocks:

  1. .problem-banner — dark panel (#1f2833), shows topic + subject area.

  2. .svg-container — <svg viewBox="0 0 850 450">
       Layer 1: subtle grid (40×40 px, white 5% opacity lines).
       Layer 2: component <g> elements  →  id="layer-{component.id}"
                each starts with style="opacity:0;transition:opacity .6s"
       Layer 3: <rect id="blur-shield" width="850" height="450"
                     fill="#050508" opacity="0" pointer-events="none"/>
       Layer 4: overlay <g> groups  →  id="overlay-scene0" … "overlay-scene3"
                each starts with style="opacity:0;transition:opacity .6s"

  3. .control-panel — row of 4 clickable step-dots + info card + Prev/Next.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OVERLAYS (one <g> per scene)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scene 0 (SETUP)    — "Given Data" card: dark rect + all given[] values as text.
Scene 1 (ELEMENTS) — labels and dimension arrows on each component.
Scene 2 (LINKAGE)  — motion-path arrows, angle arcs, velocity direction hint.
Scene 3 (SOLUTION) — formula box (dark rect, monospace formula_box_steps lines,
                      final answer in #ff3366) + result vector arrow.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CSS VARIABLES (use exactly these names)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  --bg:#0b0c10  --panel:#1f2833  --text:#c5c6c7
  --cyan:#66fcf1  --orange:#fca311  --green:#97c459  --red:#ff3366

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
JAVASCRIPT REQUIREMENTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• const CONFIG = { /* geometric/physical constants */ };
• One animate_{id}(theta) function per moving component.
• requestAnimationFrame loop: advances theta, calls all animate_* functions.
• const STEPS = [ /* 4 objects */ ]; each object:
    { layers:{id:opacity,...}, blur:0, title:'', info:'', anim:false, freeze:null }
• function applyStep(i) — sets layer opacities, blur-shield opacity, updates
  info card text, highlights active dot, starts/stops rAF loop.
• function nextStep(), prevStep() — clamp to 0–3, call applyStep.
• document.addEventListener('DOMContentLoaded', () => applyStep(0));
"""


def _generate_html(
    question: str,
    scene_plan: dict,
    api_key: Optional[str] = None,
) -> str:
    """Call the codegen model to produce the interactive HTML animation."""

    # OPT 2: compact scene plan — no wasted whitespace or unused fields
    slim_json = _slim_scene_plan(scene_plan)

    base_prompt = (
        f"{PART2_SYSTEM}\n\n"
        f"=== PART A: Question ===\n"
        f"<question>\n{question}\n</question>\n\n"
        f"=== PART B: Scene Plan ===\n"
        f"{slim_json}"
    )

    best_html = ""

    for attempt in range(1, HTML_MAX_RETRIES + 1):
        # BUG 3 FIX: build per-attempt prompt; never mutate base_prompt
        if attempt == 1:
            prompt = base_prompt
        else:
            log.warning(
                "[q_animation] Retrying Part 2 (attempt %d/%d) with minification nudge.",
                attempt, HTML_MAX_RETRIES,
            )
            prompt = (
                base_prompt
                + "\n\nCRITICAL: Previous output was truncated. "
                "Aggressively simplify — remove ALL decorative SVG, reduce CSS, "
                "shorten JS variable names. Keep all 4 scenes working. "
                "Output must be under 9,000 tokens total."
            )

        log.info("[q_animation] Part 2 attempt %d → calling %s", attempt, GEMINI_MODEL_CODEGEN)
        raw = _call_gemini(
            prompt,
            model=GEMINI_MODEL_CODEGEN,
            api_key=api_key,
            max_tokens=10_000,   # OPT 3: reduced from 16,384; real HTML ~8–10 k chars
            temperature=0.5,
        )

        # BUG 4 FIX: strip markdown fences with re.DOTALL (handles CRLF too)
        raw = re.sub(r"```(?:html)?\s*", "", raw, flags=re.DOTALL)
        raw = raw.replace("```", "").strip()

        # Trim any preamble before <!DOCTYPE
        doctype_idx = raw.lower().find("<!doctype")
        if doctype_idx == -1:
            html_idx = raw.lower().find("<html")
            if html_idx != -1:
                raw = raw[html_idx:]
        else:
            raw = raw[doctype_idx:]

        # Trim any trailing text after </html>
        close_idx = raw.lower().rfind("</html>")
        if close_idx != -1:
            raw = raw[: close_idx + 7]

        is_complete    = raw.lower().endswith("</html>")
        is_long_enough = len(raw) >= MIN_HTML_CHARS

        log.info(
            "[q_animation] HTML attempt %d — complete=%s, length=%d chars",
            attempt, is_complete, len(raw),
        )

        if is_complete and is_long_enough:
            return raw

        if len(raw) > len(best_html):
            best_html = raw

        if attempt < HTML_MAX_RETRIES:
            time.sleep(3)

    log.warning(
        "[q_animation] All HTML attempts exhausted. "
        "Returning best result (%d chars, complete=%s).",
        len(best_html),
        best_html.lower().endswith("</html>"),
    )
    return best_html


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Core Synchronous Pipeline
# ─────────────────────────────────────────────────────────────────────────────

def _run_pipeline(
    question: str,
    api_key: Optional[str] = None,
    output_path: Optional[str] = None,
    verbose: bool = True,
) -> tuple[str, str, dict]:

    def log_msg(msg: str) -> None:
        if verbose:
            print(f"[q_animation] {msg}")
        log.info("[q_animation] %s", msg)

    log_msg(f"Reasoning model : {GEMINI_MODEL_REASONING}")
    log_msg(f"Codegen model   : {GEMINI_MODEL_CODEGEN}")
    log_msg("Part 1 → Solving problem and generating 4-scene plan …")

    scene_plan = _generate_scene_plan(question, api_key=api_key)
    topic = scene_plan.get("topic", "animation")
    subject = scene_plan.get("subject_area", "")
    log_msg(f"  Topic   : {topic}  [{subject}]")

    log_msg("Part 2 → Generating interactive HTML animation …")
    html = _generate_html(question, scene_plan, api_key=api_key)
    log_msg(f"  HTML length : {len(html):,} chars")

    if not html.lower().endswith("</html>"):
        log_msg("  ⚠ WARNING: HTML appears truncated (missing </html>)")

    # BUG 7 FIX: always resolve to an absolute path
    if output_path is None:
        safe_name = re.sub(r"[^\w]+", "_", topic.lower())[:40].strip("_")
        output_path = os.path.join(OUTPUT_DIR, f"{safe_name}_animation.html")
    output_path = os.path.abspath(output_path)

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    log_msg(f"  Written to  : {output_path}")

    return html, output_path, scene_plan


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Public APIs
# ─────────────────────────────────────────────────────────────────────────────

def generate_animation(
    question: str,
    api_key: Optional[str] = None,
    output_path: Optional[str] = None,
    verbose: bool = True,
) -> str:
    """Synchronous convenience wrapper. Returns the path of the written HTML."""
    _, written_path, _ = _run_pipeline(
        question=question,
        api_key=api_key,
        output_path=output_path,
        verbose=verbose,
    )
    return written_path


async def generate_question_animation(question: str) -> dict:
    """Async entry-point consumed by the FastAPI route.

    BUG 6 FIX: graceful explanation fallback chain — never returns empty string.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("'question' field cannot be empty.")

    loop = asyncio.get_event_loop()
    html, _, scene_plan = await loop.run_in_executor(
        None,
        lambda: _run_pipeline(question=question, verbose=True),
    )

    final = scene_plan.get("final_answer") or {}

    # Fallback chain: statement → "symbol = value unit" → topic → question
    explanation: str = (
        final.get("statement")
        or (
            f"{final['symbol']} = {final['value']} {final.get('unit', '')}".strip()
            if final.get("symbol") and final.get("value")
            else ""
        )
        or scene_plan.get("topic", "")
        or question[:120]
    )

    return {
        "animation_code": html,
        "title": scene_plan.get("topic") or question[:80],
        "explanation": explanation,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6.  FastAPI App  (BUG 9 FIX — was missing entirely from the original file)
# ─────────────────────────────────────────────────────────────────────────────

if _FASTAPI_AVAILABLE:

    class QuestionRequest(BaseModel):
        question: str

    app = FastAPI(
        title="QAnim — Question Animation API",
        description=(
            "Generates interactive SVG engineering animations from a "
            "natural-language question using Google Gemini."
        ),
        version="3.0.0",
    )

    # CORS — covers Railway origin, Vercel preview deploys, and local dev.
    # Add your production Vercel URL to _ALLOWED_ORIGINS below.
    _ALLOWED_ORIGINS = [
        "https://animind-backend-production-2.up.railway.app",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        # "https://your-app.vercel.app",   ← add production URL here
    ]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_origin_regex=r"https://.*\.vercel\.app",  # all Vercel preview URLs
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["ops"])
    async def health_check() -> dict:
        """Railway / load-balancer health probe. Returns 200 when service is ready."""
        return {
            "status": "ok",
            "reasoning_model": GEMINI_MODEL_REASONING,
            "codegen_model":   GEMINI_MODEL_CODEGEN,
        }

    @app.post("/generate-question-animation", tags=["animation"])
    async def api_generate_question_animation(body: QuestionRequest) -> dict:
        """
        Generate an interactive SVG animation that answers any engineering or
        science question step-by-step.

        Request body:
          { "question": "What is the velocity of the piston when crank angle is 60°?" }

        Response:
          {
            "animation_code": "<complete self-contained HTML>",
            "title":          "<short topic title>",
            "explanation":    "<final answer / summary>"
          }
        """
        try:
            return await generate_question_animation(body.question)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except EnvironmentError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except RuntimeError as exc:
            log.exception("[q_animation] Pipeline runtime error: %s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            log.exception("[q_animation] Unexpected error: %s", exc)
            raise HTTPException(
                status_code=500,
                detail=f"Unexpected server error: {exc}",
            ) from exc

else:
    # Stub so `from q_animation import app` fails loudly instead of silently
    class _MissingApp:  # type: ignore[no-redef]
        def __getattr__(self, _: str):
            raise ImportError(
                "FastAPI is not installed. "
                "Run: pip install fastapi uvicorn[standard]"
            )

    app = _MissingApp()  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────
# 7.  CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("=" * 64)
    print("  q_animation.py  —  Engineering Animation Generator")
    print(f"  Reasoning: {GEMINI_MODEL_REASONING}")
    print(f"  Codegen  : {GEMINI_MODEL_CODEGEN}")
    print("=" * 64)

    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:]).strip()
        print(f"Question (from args): {q}\n")
    else:
        q = input("Enter your engineering question:\n> ").strip()

    if not q:
        print("No question provided. Exiting.")
        sys.exit(1)

    try:
        out = generate_animation(
            question=q,
            api_key=os.environ.get("GEMINI_API_KEY"),
            verbose=True,
        )
        print(f"\n✅ Done! Open in browser:\n   {out}")
    except EnvironmentError as e:
        print(f"\n❌ Configuration error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Pipeline error: {e}", file=sys.stderr)
        sys.exit(1)
