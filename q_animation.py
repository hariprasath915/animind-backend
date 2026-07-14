"""
q_animation.py — Fully Refactored + Backend-Connected
======================================================
Generates premium, step-by-step interactive SVG engineering animations from a
natural-language question.  Uses **Google Gemini 3.1 Flash-Lite** (via the official
google-genai SDK) for every AI step.

Architecture (2-Part Pipeline)
-------------------------------
Part 1  → Gemini produces a structured *Explanation + Animation Plan*
           (problem statement + full mathematical solution + scene design).
Part 2  → Gemini produces the final single-file *High-Rich Interactive SVG HTML*
           document (SVG body + JavaScript animation engine).

Backend Integration
-------------------
  • generate_question_animation(question) → async function called by main.py
    via POST /generate-question-animation.
  • Returns { animation_code, title, explanation } as expected by index.html.

FIX LOG (2026-07-14)
---------------------
• ROOT CAUSE (502):  The original file used raw urllib + a non-existent model
  name ("gemini-3.1-pro" bare string doesn't exist).  The correct API model
  string is "gemini-3.1-pro-preview".  Every call returned 404 → unhandled
  RuntimeError → Railway 502 Bad Gateway.
• FIX 1:  Switched from raw urllib to the official google-genai SDK
          (google.generativeai is deprecated as of 2026; use google.genai).
• FIX 2:  Correct model string: "gemini-3.1-pro-preview".
• FIX 3:  Collapsed 4-call pipeline → 2-call pipeline to stay well under
          Railway's ~30 s router timeout.
• FIX 4:  generate_question_animation() runs blocking SDK calls in
          asyncio's thread-pool executor so FastAPI is never blocked.
• FIX 5:  Exponential-backoff retry (3 attempts) on 429/503/quota errors.
• FIX 7:  Switched model from "gemini-3.1-pro-preview" ($2/$12 per 1M)
          to "gemini-3.1-flash-lite" ($0.25/$1.50 per 1M) — 8x cheaper on
          output tokens.  Quality is sufficient for SVG/HTML code generation.
• FIX 6:  _extract_json() uses bracket-depth counting — robust against
          trailing commentary that breaks json.loads.

Requirements (add to requirements.txt if not already present):
  google-genai>=2.0.0
"""

from __future__ import annotations

import os
import re
import json
import asyncio
import logging
import time

from google import genai
from google.genai import types as genai_types

# ─────────────────────────────────────────────────────────────────────────────
# 0.  Configuration
# ─────────────────────────────────────────────────────────────────────────────

GEMINI_MODEL = "gemini-3.1-flash-lite"   # ✅ correct API model string
OUTPUT_DIR   = "."
MAX_RETRIES  = 3

log = logging.getLogger(__name__)

# CSS / visual design tokens
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


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Gemini SDK Helper  (google-genai ≥ 2.0)
# ─────────────────────────────────────────────────────────────────────────────

def _get_client(api_key: str | None = None) -> genai.Client:
    """Return a configured google-genai Client."""
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise EnvironmentError(
            "GEMINI_API_KEY environment variable not set. "
            "Add it to your Railway service variables."
        )
    return genai.Client(api_key=key)


def _call_gemini(
    prompt: str,
    api_key: str | None = None,
    max_tokens: int = 16384,
) -> str:
    """
    Call Gemini with exponential-backoff retry.
    Returns the response text.
    """
    client = _get_client(api_key)
    config = genai_types.GenerateContentConfig(
        temperature=1.0,
        max_output_tokens=max_tokens,
    )

    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=config,
            )
            return response.text.strip()
        except Exception as exc:
            err_str = str(exc).lower()
            is_transient = any(k in err_str for k in ("429", "503", "quota", "resource exhausted", "unavailable"))
            if is_transient and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                log.warning(f"[q_animation] Gemini transient error, retrying in {wait}s (attempt {attempt}): {exc}")
                time.sleep(wait)
                last_err = exc
            else:
                raise RuntimeError(f"Gemini API error: {exc}") from exc

    raise last_err  # type: ignore[misc]


def _extract_json(raw: str) -> dict | list:
    """
    Extract and parse the first JSON object/array from a Gemini response,
    tolerating markdown fences and trailing commentary.
    Uses bracket-depth counting to find the true end of the JSON.
    """
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
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
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return json.loads(cleaned[start:])


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Part 1 — Explanation + Animation Plan
# ─────────────────────────────────────────────────────────────────────────────

PART1_SYSTEM = """\
You are an expert engineering educator and SVG animation director.

Given ANY engineering or science question, produce a COMPLETE structured JSON plan
that drives a step-by-step animated visual explanation.

Return ONLY valid JSON — no markdown fences, no preamble, no trailing text.

{
  "topic": "<short topic name, e.g. 'Slider-Crank Velocity'>",
  "subject_area": "<e.g. 'Kinematics' | 'Statics' | 'Thermodynamics' | 'Electrical' | 'Fluid Mechanics' | 'Structural' | 'General'>",
  "visual_theme": {
    "palette": "<'dark_mechanical' | 'dark_electrical' | 'dark_fluid' | 'dark_structural' | 'dark_chemistry'>",
    "stage_bg_color": "<hex, e.g. '#05080f'>",
    "accent_color":   "<hex, primary highlight>",
    "accent2_color":  "<hex, secondary highlight>",
    "note": "<one sentence describing overall visual feel>"
  },
  "problem_statement": "<full verbatim question text>",
  "given": [
    {"symbol": "<symbol>", "value": "<numeric>", "unit": "<unit>", "description": "<label>"}
  ],
  "find": "<what must be calculated>",
  "components": [
    {
      "id": "<short_snake_case_id>",
      "draw_order": 1,
      "label": "<human label>",
      "shape": "<'circle' | 'rectangle' | 'line' | 'gear' | 'arrow' | 'beam' | 'custom'>",
      "approx_cx": "<0-850>",
      "approx_cy": "<0-450>",
      "approx_size": "<pixels>",
      "color_hint": "<hex>",
      "role_animation": "<one sentence: what motion shows its role>",
      "label_position": "<'top' | 'bottom' | 'left' | 'right'>",
      "arrow_hints": ["<e.g. 'rotation arrow CW'>"]
    }
  ],
  "solution_steps": [
    {
      "step_number": 1,
      "title": "<Step title>",
      "formula_text": "<formula in plain text>",
      "substitution_text": "<formula with numbers>",
      "result_text": "<result in plain text>",
      "result_numeric": "<value + unit>",
      "focus_component_ids": ["<id>"],
      "annotation": "<one sentence shown as on-screen annotation>"
    }
  ],
  "final_answer": {
    "symbol": "<symbol>",
    "value": "<numeric>",
    "unit": "<unit>",
    "statement": "<one-sentence plain-English summary of the answer>",
    "highlight_components": ["<ids>"]
  },
  "scenes": [
    {
      "index": 0,
      "scene_type": "<'intro' | 'draw_component' | 'solution_step' | 'final'>",
      "label": "<SHORT CAPS label max 8 chars>",
      "title": "<Scene title>",
      "description": "<2-3 sentence explanation>",
      "badges": [{"text": "<badge text>", "color": "<'cyan'|'orange'|'green'|'red'>"}],
      "visible_component_ids": [],
      "focused_component_ids": [],
      "blur_shield_opacity": 0.0,
      "motion_type": "<'static' | 'rotate' | 'translate' | 'oscillate' | 'pulse'>",
      "show_formula_box": false,
      "formula_box": null,
      "show_final_answer": false,
      "final_answer_box": null,
      "start_continuous_anim": false
    }
  ]
}

Scene ordering rules:
  - Scene 0:     intro (blank stage + problem params)
  - Scenes 1..N: one draw_component scene per component (sorted by draw_order)
  - Next scenes: one solution_step scene per solution step
  - Last scene:  final (all components animated, final answer box shown)
"""


def _generate_plan(question: str, api_key: str | None = None) -> dict:
    """Part 1: produce the structured explanation + animation plan."""
    prompt = f"{PART1_SYSTEM}\n\nQuestion:\n{question}"
    raw = _call_gemini(prompt, api_key=api_key, max_tokens=8192)
    return _extract_json(raw)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Part 2 — Full HTML Generator
# ─────────────────────────────────────────────────────────────────────────────

PART2_SYSTEM = """\
You are an expert SVG/JavaScript animation engineer.

Given a JSON plan (topic, components, solution_steps, scenes, visual_theme),
produce a COMPLETE, self-contained <!DOCTYPE html> interactive animation file.

REQUIREMENTS:
1. Single file — all CSS and JS inline, no external dependencies.
2. Dark theme matching visual_theme.palette:
     dark_mechanical → bg #0b0c10, accent cyan #66fcf1
     dark_electrical → bg #0d1f0d, accent lime #39ff14
     dark_fluid      → bg #050d1a, accent blue #4fc3f7
     dark_structural → bg #0f0f0f, accent amber #fca311
     dark_chemistry  → bg #120d1f, accent pink #ff79c6
3. Layout:
     • Top: problem statement banner (dark panel)
     • Middle: SVG stage (viewBox="0 0 850 450"), 16:9 aspect ratio
     • Bottom: control panel — step dots, info card (title+badges+desc), Prev/Next buttons
4. SVG structure:
     • <defs> with gradients, filters (dropShadow, glowAccent), arrow markers
     • Background layer (grid + radial vignette)
     • One <g class="svg-layer" id="layer-{component.id}" style="opacity:0;"> per component
     • <rect id="blur-shield" opacity="0" fill="rgba(5,8,15,0.7)" width="850" height="450"
             style="pointer-events:none; transition:opacity 0.5s;"/>
     • One <g id="overlay-scene{N}" style="opacity:0;"> per scene N
5. JavaScript:
     • stepsData[] array (one entry per scene from the plan)
     • Per-component animateComponent_{id}(t) functions
     • requestAnimationFrame master loop (animationLoop)
     • revealComponent(id, drawAnimation) for fade-in/scale-up/slide effects
     • applyStep(idx) — show overlay, set blur-shield, reveal components, update info card
     • nextStep() / prevStep() — navigation
     • resetAnim() — reset everything to scene 0
     • setTimeout(() => resetAnim(), 100) at the very end
6. Info card badges: <span class="badge badge-cyan|badge-orange|badge-green|badge-red">
7. Formula boxes: dark rect + accent-color border + formula rows
8. Final answer box: large centred text with accent2_color glow
9. ALL component SVG elements that JS will animate MUST have id attributes.

Return ONLY the complete HTML — starting with <!DOCTYPE html>, ending with </html>.
No markdown fences, no preamble, no trailing text.
"""


def _generate_html(plan: dict, api_key: str | None = None) -> str:
    """Part 2: produce the full interactive HTML from the plan."""
    prompt = f"{PART2_SYSTEM}\n\nAnimation Plan (JSON):\n{json.dumps(plan, indent=2)}"
    raw = _call_gemini(prompt, api_key=api_key, max_tokens=16384)
    raw = re.sub(r"```(?:html)?\s*", "", raw).replace("```", "").strip()
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Core Synchronous Pipeline
# ─────────────────────────────────────────────────────────────────────────────

def _run_pipeline(
    question: str,
    api_key: str | None = None,
    output_path: str | None = None,
    verbose: bool = True,
) -> tuple[str, str, dict]:
    """
    Synchronous 2-part pipeline: question → HTML.

    Returns
    -------
    tuple of (html_string, output_path_written, plan_dict)
    """
    def log_msg(msg: str) -> None:
        if verbose:
            print(f"[q_animation] {msg}")
        log.info(f"[q_animation] {msg}")

    log_msg(f"Gemini model: {GEMINI_MODEL}")

    # Part 1
    log_msg("Part 1 → Generating explanation + animation plan ...")
    plan = _generate_plan(question, api_key=api_key)
    topic = plan.get("topic", "animation")
    log_msg(f"  Topic: {topic}")
    log_msg(f"  Components: {len(plan.get('components', []))}")
    log_msg(f"  Solution steps: {len(plan.get('solution_steps', []))}")
    log_msg(f"  Scenes: {len(plan.get('scenes', []))}")

    # Part 2
    log_msg("Part 2 → Generating interactive HTML animation ...")
    html = _generate_html(plan, api_key=api_key)
    log_msg(f"  HTML length: {len(html):,} chars")

    # Write to disk
    if output_path is None:
        safe_name = re.sub(r"[^\w]+", "_", topic.lower())[:40]
        output_path = os.path.join(OUTPUT_DIR, f"{safe_name}_animation.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    log_msg(f"  Written to: {output_path}  ({len(html):,} bytes)")

    return html, output_path, plan


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Public API — CLI / direct import
# ─────────────────────────────────────────────────────────────────────────────

def generate_animation(
    question: str,
    api_key: str | None = None,
    output_path: str | None = None,
    verbose: bool = True,
) -> str:
    """
    Full pipeline: question → premium interactive HTML file.

    Parameters
    ----------
    question    : Natural-language engineering question.
    api_key     : Gemini API key (falls back to GEMINI_API_KEY env var).
    output_path : Where to write the .html file.  Auto-generated if None.
    verbose     : Print progress messages.

    Returns
    -------
    str : Path to the generated HTML file.
    """
    _, written_path, _ = _run_pipeline(
        question=question,
        api_key=api_key,
        output_path=output_path,
        verbose=verbose,
    )
    return written_path


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Backend API — called by main.py via POST /generate-question-animation
# ─────────────────────────────────────────────────────────────────────────────

async def generate_question_animation(question: str) -> dict:
    """
    Async entry point for the FastAPI backend (main.py).

    Called by:
        POST /generate-question-animation  →  { question: "..." }

    Returns a dict that index.html expects:
        {
            "animation_code": "<full <!DOCTYPE html>...>",
            "title":          "<topic name>",
            "explanation":    "<one-sentence plain-English answer>"
        }

    The blocking google-genai SDK calls run in asyncio's default thread-pool
    executor so they never block the FastAPI event loop.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("'question' field cannot be empty.")

    loop = asyncio.get_event_loop()

    html, _, plan = await loop.run_in_executor(
        None,
        lambda: _run_pipeline(question=question, verbose=True),
    )

    final = plan.get("final_answer", {})
    explanation_text = (
        final.get("statement")
        or f"Topic: {plan.get('topic', question[:80])}"
    )

    return {
        "animation_code": html,
        "title":          plan.get("topic", question[:80]),
        "explanation":    explanation_text,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 7.  Example Questions  (for CLI testing)
# ─────────────────────────────────────────────────────────────────────────────

EXAMPLE_QUESTIONS = {
    "slider_crank": (
        "In a slider-crank mechanism, the crank length is 50 mm and the "
        "connecting rod is 200 mm. The crank rotates at 300 RPM. "
        "Determine the linear velocity of the piston when the crank angle "
        "is 60 degrees from inner dead centre."
    ),
    "spur_gear": (
        "A spur gear train consists of a driving pinion with 20 teeth "
        "rotating at 900 RPM (clockwise) meshing with a driven gear of "
        "60 teeth. Calculate the speed and direction of rotation of the "
        "driven gear."
    ),
    "four_bar": (
        "In a four-bar linkage, the fixed link (frame) is 120 mm, the "
        "crank is 40 mm rotating at 200 RPM, the coupler is 160 mm, and "
        "the follower (rocker) is 80 mm. Determine the angular velocity "
        "of the rocker when the crank is at 45 degrees from the fixed link."
    ),
    "belt_pulley": (
        "A belt drive system has a driver pulley of diameter 250 mm "
        "rotating at 720 RPM. It is connected to a driven pulley of "
        "diameter 600 mm. Calculate the speed of the driven pulley and "
        "the velocity of the belt."
    ),
    "cam_follower": (
        "A circular disc cam of base circle radius 30 mm and lift 20 mm "
        "rotates at 150 RPM. The knife-edge follower rises with simple "
        "harmonic motion during 120 degrees of cam rotation. Find the "
        "maximum velocity and maximum acceleration of the follower."
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# 8.  CLI Entry Point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("=" * 64)
    print("  q_animation.py  —  Engineering Animation Generator")
    print(f"  Powered by Google Gemini  ({GEMINI_MODEL})")
    print("=" * 64)
    print()
    print("Available example questions:")
    for i, (key, q) in enumerate(EXAMPLE_QUESTIONS.items(), 1):
        preview = q[:80].replace("\n", " ")
        print(f"  [{i}] {key}: {preview}...")
    print(f"  [{len(EXAMPLE_QUESTIONS) + 1}] Enter a custom question")
    print()

    choice_raw = input("Select an option [1]: ").strip() or "1"

    try:
        choice = int(choice_raw)
    except ValueError:
        choice = 1

    keys = list(EXAMPLE_QUESTIONS.keys())

    if 1 <= choice <= len(keys):
        selected_key = keys[choice - 1]
        user_question = EXAMPLE_QUESTIONS[selected_key]
        print(f"\nSelected: {selected_key}")
    else:
        user_question = input("Enter your question:\n> ").strip()
        if not user_question:
            print("No question entered. Exiting.")
            sys.exit(0)

    print(f"\nQuestion:\n{user_question}\n")

    out = generate_animation(
        question=user_question,
        api_key=os.environ.get("GEMINI_API_KEY"),
        verbose=True,
    )
    print(f"\nDone! Open in browser:  {out}")
