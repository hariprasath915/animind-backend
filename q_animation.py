"""
q_animation.py — 2-Part Pipeline (Optimized for Token Economy)
====================================================
Generates premium, step-by-step interactive SVG engineering animations from a
natural-language question.  Uses **Google Gemini 3.1 Pro Preview**.

Architecture (2-Part Pipeline)
-------------------------------
Part 1  → Gemini produces a *Scene-by-Scene Animation Plan*.
Part 2  → Gemini produces the final single-file *Interactive HTML*.

FIX LOG
-------
• 2026-07-14 Fix: Resolved frontend timeout / MAX_TOKENS exhaustion. 
  - Simplified SVG visual requirements (removed heavy dropShadows/gradients) to save tokens.
  - Added smart truncation detection (`endswith("</html>")`).
  - Added dynamic prompt warnings on retry to force the model to minify output if it fails.
  - Reduced MIN_HTML_CHARS to 8,000 to prevent false-positive retries on concise code.
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

GEMINI_MODEL     = "gemini-3.1-pro-preview"
OUTPUT_DIR       = "."
MAX_RETRIES      = 3
MIN_HTML_CHARS   = 8_000    # Lowered from 15k to prevent false retries on optimized code
HTML_MAX_RETRIES = 2        # Retries for truncated HTML

log = logging.getLogger(__name__)

# CSS / visual design tokens shared across the pipeline
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
# 1.  Gemini SDK Helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_client(api_key: str | None = None) -> genai.Client:
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
    """Call Gemini with exponential-backoff retry."""
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
            is_transient = any(k in err_str for k in (
                "429", "503", "quota", "resource exhausted", "unavailable",
                "truncated", "max_tokens",
            ))
            if is_transient and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                log.warning(
                    f"[q_animation] API error, retrying in {wait}s "
                    f"(attempt {attempt}): {exc}"
                )
                time.sleep(wait)
                last_err = exc
            else:
                raise RuntimeError(f"Gemini API error: {exc}") from exc

    raise last_err  # type: ignore[misc]


def _extract_json(raw: str) -> dict | list:
    """Extract and parse JSON object/array from Gemini response."""
    cleaned = re.sub(r"
```(?:json)?\s*", "", raw).replace("```", "").strip()
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
# 2.  Part 1 — Scene Plan
# ─────────────────────────────────────────────────────────────────────────────

PART1_SYSTEM = """\
You are an expert engineering educator AND SVG animation director.

Given ANY engineering, physics, or mathematics question, you will:
  1. Fully solve the problem (all formulas, substitutions, and numeric results).
  2. Design a 4-scene interactive SVG animation plan that communicates the
     solution visually.

All problem data (given values, formulas, solution steps, final answer, and
SVG component geometry) are embedded directly in the scene plan JSON below.

Return ONLY valid JSON — no markdown fences, no preamble, no trailing text.

{
  "topic": "<short topic name>",
  "subject_area": "<category>",
  "problem_statement": "<full verbatim question text>",
  "given": [
    {"symbol": "r", "value": "50", "unit": "mm", "description": "Crank length"}
  ],
  "find": "<what is being calculated>",
  "solution_steps": [
    {
      "step_number": 1,
      "title": "<step title>",
      "formula_text": "<formula>",
      "substitution_text": "<substituted>",
      "result_text": "<result>"
    }
  ],
  "final_answer": {
    "symbol": "V", "value": "12.5", "unit": "m/s", "statement": "<summary>"
  },
  "components": [
    {
      "id": "<short_snake_case_id>",
      "label": "<human label>",
      "shape": "<circle|line|rect|path>",
      "role": "<what it does>",
      "color_hint": "<hex color>",
      "motion_type": "<none|rotate|translate|oscillate>",
      "pivot_or_anchor": "<SVG coords>",
      "approx_size": "<pixels>",
      "layer_order": 1
    }
  ],
  "diagram_notes": "<origin location, scale factor>",
  "scenes": [
    {
      "index": 0,
      "scene_key": "SETUP",
      "label": "<≤8 chars>",
      "title": "<title>",
      "description": "<description>",
      "badges": [{"text": "<text>", "color": "<cyan|orange|green|red>"}],
      "visible_layer_ids": ["<id>"],
      "focused_layer_ids": [],
      "blur_shield_opacity": 0.0,
      "start_continuous_anim": false,
      "freeze_at_angle_deg": null,
      "show_formula_box": false,
      "show_final_answer": false,
      "overlay_annotations": [
        {"type": "<given_data_card|label|angle_arc|velocity_vector|formula_box>", "content": "<text>", "svgx": "10", "svgy": "10"}
      ]
    }
  ],
  "formula_box_steps": [{"line": "<monospace formula line>"}],
  "answer_overlay": {"symbol": "V", "value": "1.53", "unit": "m/s", "label": "Piston Velocity", "svgx": "500", "svgy": "250"}
}

Scene rules (MUST follow exactly):
- Exactly 4 scenes: 0 (SETUP), 1 (ELEMENTS), 2 (LINKAGE), 3 (SOLUTION).
- Scene 0 must have given_data_card overlay.
- Scene 3 must have formula_box and velocity_vector overlays.
- Keep coordinate references inside an 850x450 canvas.
"""

def _generate_scene_plan(question: str, api_key: str | None = None) -> dict:
    prompt = f"{PART1_SYSTEM}\n\nQuestion:\n{question}"
    raw = _call_gemini(prompt, api_key=api_key, max_tokens=8000)
    return _extract_json(raw)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Part 2 — High-Rich Interactive SVG HTML
# ─────────────────────────────────────────────────────────────────────────────

PART2_SYSTEM = """\
You are a world-class SVG/JavaScript animation engineer.
Produce a COMPLETE, self-contained <!DOCTYPE html> interactive animation file.
Return ONLY the raw HTML starting with <!DOCTYPE html> and ending with </html>.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOKEN ECONOMY & FILE SIZE (CRITICAL)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
To prevent your output from being truncated by token limits:
1. DO NOT use complex SVG drop-shadow filters (<filter id="dropShadow">). Use basic CSS shadows or simple shapes.
2. DO NOT use hyper-realistic metallic gradients. Use simple solid fills (#1f2833, #66fcf1) or very basic 2-stop linear gradients.
3. Keep SVG paths clean and minimal. Use standard shapes (<rect>, <circle>, <line>) where possible instead of complex <path> data.
4. Minify your CSS and JavaScript logic. Keep everything concise.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRUCTURAL REQUIREMENTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. PAGE LAYOUT (Max width 850px, centered):
   - .problem-banner (dark panel, top)
   - .svg-container (viewBox="0 0 850 450")
   - .control-panel (step dots, info card, Prev/Next buttons)

2. SVG STAGE:
   - Layer 1: Grid pattern (40x40, white 5% opacity).
   - Layer 2: Component layers (<g class="svg-layer" id="layer-{id}" style="opacity:0;">)
   - Layer 3: <rect id="blur-shield" width="100%" height="100%" fill="#050508" opacity="0" pointer-events="none"/>
   - Layer 4: Overlay groups (<g class="svg-layer" id="overlay-scene{N}" style="opacity:0;">)

3. OVERLAYS:
   - Scene 0: "Given Data" card showing parameters.
   - Scene 1 & 2: Labels, arrows, and dimension lines.
   - Scene 3: Result vector arrow and FORMULA BOX (dark rect with solution_steps as monospace text). Final answer in red (#ff3366).

4. CSS:
   - Use these vars: --bg-color:#0b0c10; --panel-bg:#1f2833; --text-main:#c5c6c7; --accent-cyan:#66fcf1; --accent-red:#ff3366;
   - .svg-layer { transition: opacity 0.6s; }

5. JAVASCRIPT:
   - CONFIG object holding physical dimensions.
   - animate_{id}(theta) functions for moving parts.
   - requestAnimationFrame loop updating `theta` and calling animate functions.
   - `stepsData` array with 4 scene configurations (layer opacities, blur amount, text).
   - applyStep(idx), nextStep(), resetAnim() functions.
"""

def _generate_html(
    question: str,
    scene_plan: dict,
    api_key: str | None = None,
) -> str:
    # Remove large text blocks from scene_plan to save input tokens
    slim_plan = dict(scene_plan)
    slim_plan.pop("problem_statement", None)
    
    base_prompt = (
        f"{PART2_SYSTEM}\n\n"
        f"=== PART A: Original Question ===\n{question}\n\n"
        f"=== PART B: Scene Plan ===\n{json.dumps(slim_plan, indent=2)}"
    )

    last_html = ""
    for attempt in range(1, HTML_MAX_RETRIES + 1):
        prompt = base_prompt
        if attempt > 1:
            log.warning(f"[q_animation] Retrying Part 2 (Attempt {attempt}) with forced minification prompt.")
            prompt += (
                "\n\nCRITICAL WARNING: Your previous output was truncated because it exceeded the max token limit. "
                "You MUST drastically simplify your SVG shapes, CSS, and JS. "
                "Remove ALL filters, complex gradients, and decorative elements. Make the HTML as short as possible!"
            )

        raw = _call_gemini(prompt, api_key=api_key, max_tokens=16384)
        raw = re.sub(r"
```(?:html)?\s*", "", raw).replace("```", "").strip()

        # Fix missing doctype
        if not raw.lower().startswith("<!doctype"):
            idx = raw.lower().find("<!doctype")
            if idx != -1: raw = raw[idx:]

        # Validate completion: Does it end with the HTML closing tag?
        is_complete = raw.lower().endswith("</html>")

        if is_complete and len(raw) >= MIN_HTML_CHARS:
            return raw

        log.warning(
            f"[q_animation] HTML validation failed on attempt {attempt}. "
            f"Complete: {is_complete}, Length: {len(raw):,} chars."
        )
        if len(raw) > len(last_html):
            last_html = raw
            
        if attempt < HTML_MAX_RETRIES:
            time.sleep(3)

    log.warning("[q_animation] Max retries hit. Returning longest generated HTML (may be truncated).")
    return last_html


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Core Synchronous Pipeline
# ─────────────────────────────────────────────────────────────────────────────

def _run_pipeline(
    question: str,
    api_key: str | None = None,
    output_path: str | None = None,
    verbose: bool = True,
) -> tuple[str, str, dict]:
    def log_msg(msg: str) -> None:
        if verbose: print(f"[q_animation] {msg}")
        log.info(f"[q_animation] {msg}")

    log_msg(f"Gemini model: {GEMINI_MODEL}")
    log_msg("Part 1 → Solving problem and generating 4-scene animation plan ...")
    
    scene_plan = _generate_scene_plan(question, api_key=api_key)
    topic = scene_plan.get("topic", "animation")
    
    log_msg(f"  Topic: {topic}")
    log_msg("Part 2 → Generating interactive HTML animation ...")
    
    html = _generate_html(question, scene_plan, api_key=api_key)
    log_msg(f"  HTML length: {len(html):,} chars")
    
    if not html.lower().endswith("</html>"):
        log_msg("  ⚠️ WARNING: HTML appears truncated (missing </html>)")

    if output_path is None:
        safe_name = re.sub(r"[^\w]+", "_", topic.lower())[:40]
        output_path = os.path.join(OUTPUT_DIR, f"{safe_name}_animation.html")
        
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
        
    log_msg(f"  Written to: {output_path}")
    return html, output_path, scene_plan


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Public APIs
# ─────────────────────────────────────────────────────────────────────────────

def generate_animation(question: str, api_key: str | None = None, output_path: str | None = None, verbose: bool = True) -> str:
    _, written_path, _ = _run_pipeline(question=question, api_key=api_key, output_path=output_path, verbose=verbose)
    return written_path

async def generate_question_animation(question: str) -> dict:
    question = (question or "").strip()
    if not question: raise ValueError("'question' field cannot be empty.")

    loop = asyncio.get_event_loop()
    html, _, scene_plan = await loop.run_in_executor(
        None, lambda: _run_pipeline(question=question, verbose=True)
    )

    final = scene_plan.get("final_answer", {})
    explanation_text = final.get("statement") or f"Topic: {scene_plan.get('topic', question[:80])}"

    return {
        "animation_code": html,
        "title": scene_plan.get("topic", question[:80]),
        "explanation": explanation_text,
    }


if __name__ == "__main__":
    import sys
    print("=" * 64)
    print("  q_animation.py  —  Engineering Animation Generator")
    print("=" * 64)
    q = input("Enter your engineering question:\n> ").strip()
    if q:
        out = generate_animation(question=q, api_key=os.environ.get("GEMINI_API_KEY"), verbose=True)
        print(f"\nDone! Open in browser: {out}")
```eof
