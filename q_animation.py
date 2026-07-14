"""
q_animation.py — Fully Refactored + Backend-Connected
======================================================
Generates premium, step-by-step interactive SVG engineering animations from a
natural-language question.  Uses **Claude (Anthropic)** for every AI step —
matching the architecture in claude_client.py.

Architecture (2-Part Pipeline)
-------------------------------
Part 1  → Claude produces a structured *Explanation + Animation Plan*
           (problem statement + full mathematical solution + scene design).
Part 2  → Claude produces the final single-file *High-Rich Interactive SVG HTML*
           document (SVG body + JavaScript animation engine).

Why 2 parts instead of 4?
  The original 4-call Gemini pipeline (Part1→2→3a→3b) easily exceeded
  Railway's router timeout (30s), causing the 502 errors you saw.
  Collapsing to 2 Claude calls keeps each well under the limit.

Backend Integration
-------------------
  • generate_question_animation(question) → async function called by main.py
    via POST /generate-question-animation.
  • Returns { animation_code, title, explanation } as expected by index.html.

FIX LOG (2026-07-13 → 2026-07-14)
-----------------------------------
• ROOT CAUSE:  "gemini-3.1-pro-preview" does not exist → API returns 404
  on every call → FastAPI backend raises unhandled RuntimeError → Railway
  converts that to a 502 Bad Gateway.
• FIX 1:  Replaced all Gemini REST calls with Anthropic Claude API calls,
  matching the existing claude_client.py architecture (same SDK, same model
  constants MODEL_SONNET / MODEL_HAIKU).
• FIX 2:  Collapsed 4-call pipeline (Part1+2+3a+3b) into 2-call pipeline
  to avoid Railway's ~30 s router timeout.
• FIX 3:  Added exponential-backoff retry (up to 3 attempts) on transient
  Anthropic API errors (overload / 529 / network blip).
• FIX 4:  generate_question_animation() now properly awaits the async
  Claude client instead of wrapping sync urllib in run_in_executor.
• FIX 5:  _extract_json() made more robust (handles extra trailing text).
"""

from __future__ import annotations

import os
import re
import json
import asyncio
import textwrap
import logging
import time
from typing import Any

import anthropic

# ─────────────────────────────────────────────────────────────────────────────
# 0.  Configuration  (mirrors claude_client.py constants)
# ─────────────────────────────────────────────────────────────────────────────

MODEL_SONNET = "claude-sonnet-4-6"   # heavy reasoning — Part 1 plan
MODEL_HAIKU  = "claude-haiku-4-5-20251001"  # fast codegen — Part 2 HTML

OUTPUT_DIR   = "."                    # Where .html files are written (CLI)
MAX_RETRIES  = 3                      # exponential-backoff retry count

log = logging.getLogger(__name__)

# CSS / visual design tokens shared across every generated file
DESIGN_TOKENS = {
    "bg":         "#0b0c10",
    "panel_bg":   "#1f2833",
    "text_main":  "#c5c6c7",
    "cyan":       "#66fcf1",
    "cyan_dim":   "#45a29e",
    "orange":     "#fca311",
    "green":      "#97c459",
    "red":        "#ff3366",
}


# ─────────────────────────────────────────────────────────────────────────────
# 1.  Anthropic Client Helper  (replaces broken Gemini REST helper)
# ─────────────────────────────────────────────────────────────────────────────

def _get_anthropic_client(async_mode: bool = False):
    """Return a (sync or async) Anthropic client, raising clearly if key absent."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "ANTHROPIC_API_KEY environment variable not set. "
            "Add it to your Railway service variables."
        )
    if async_mode:
        return anthropic.AsyncAnthropic(api_key=api_key)
    return anthropic.Anthropic(api_key=api_key)


def _call_claude_sync(
    prompt: str,
    model: str = MODEL_SONNET,
    max_tokens: int = 8192,
    system: str | None = None,
) -> str:
    """
    Synchronous Claude call with exponential-backoff retry.
    Returns the first text block content.
    """
    client = _get_anthropic_client(async_mode=False)
    kwargs: dict[str, Any] = {
        "model":      model,
        "max_tokens": max_tokens,
        "messages":   [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.messages.create(**kwargs)
            # Extract text from first content block
            for block in response.content:
                if block.type == "text":
                    return block.text.strip()
            raise RuntimeError("Claude returned no text block in response.")
        except anthropic.APIStatusError as exc:
            # 529 = overloaded; retry with backoff
            if exc.status_code in (429, 529) and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                log.warning(f"[q_animation] Claude {exc.status_code}, retrying in {wait}s (attempt {attempt})")
                time.sleep(wait)
                last_err = exc
                continue
            raise RuntimeError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except Exception as exc:
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt
                log.warning(f"[q_animation] Transient error, retrying in {wait}s: {exc}")
                time.sleep(wait)
                last_err = exc
                continue
            raise

    raise last_err  # type: ignore[misc]


async def _call_claude_async(
    prompt: str,
    model: str = MODEL_SONNET,
    max_tokens: int = 8192,
    system: str | None = None,
) -> str:
    """
    Async Claude call with exponential-backoff retry.
    Returns the first text block content.
    """
    client = _get_anthropic_client(async_mode=True)
    kwargs: dict[str, Any] = {
        "model":      model,
        "max_tokens": max_tokens,
        "messages":   [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await client.messages.create(**kwargs)
            for block in response.content:
                if block.type == "text":
                    return block.text.strip()
            raise RuntimeError("Claude returned no text block in response.")
        except anthropic.APIStatusError as exc:
            if exc.status_code in (429, 529) and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                log.warning(f"[q_animation] Claude {exc.status_code}, retrying in {wait}s (attempt {attempt})")
                await asyncio.sleep(wait)
                last_err = exc
                continue
            raise RuntimeError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except Exception as exc:
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt
                log.warning(f"[q_animation] Transient error, retrying in {wait}s: {exc}")
                await asyncio.sleep(wait)
                last_err = exc
                continue
            raise

    raise last_err  # type: ignore[misc]


def _extract_json(raw: str) -> dict | list:
    """
    Extract and parse the first JSON object/array from a Claude response,
    tolerating markdown code-fences and trailing commentary.
    """
    # Strip markdown fences
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    # Find first { or [
    start = next((i for i, c in enumerate(cleaned) if c in "{["), None)
    if start is None:
        raise ValueError(f"No JSON found in Claude response:\n{raw[:400]}")
    # Find matching closing bracket by scanning for balanced end
    opener = cleaned[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    end = start
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
        # Fallback: try parsing from start to end of string
        return json.loads(cleaned[start:])


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Part 1 — Explanation + Animation Plan  (Sonnet, structured JSON)
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
      "approx_cx": <0-850>,
      "approx_cy": <0-450>,
      "approx_size": <pixels>,
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


def _generate_plan_sync(question: str) -> dict:
    """Part 1 (sync): produce the structured explanation + animation plan."""
    prompt = f"Question:\n{question}"
    raw = _call_claude_sync(prompt, model=MODEL_SONNET, max_tokens=8192, system=PART1_SYSTEM)
    return _extract_json(raw)


async def _generate_plan_async(question: str) -> dict:
    """Part 1 (async): produce the structured explanation + animation plan."""
    prompt = f"Question:\n{question}"
    raw = await _call_claude_async(prompt, model=MODEL_SONNET, max_tokens=8192, system=PART1_SYSTEM)
    return _extract_json(raw)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Part 2 — Full HTML Generator  (Haiku, raw HTML output)
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


def _generate_html_sync(plan: dict) -> str:
    """Part 2 (sync): produce the full interactive HTML from the plan."""
    prompt = f"Animation Plan (JSON):\n{json.dumps(plan, indent=2)}"
    raw = _call_claude_sync(prompt, model=MODEL_HAIKU, max_tokens=16000, system=PART2_SYSTEM)
    # Strip any accidental markdown fences
    raw = re.sub(r"```(?:html)?\s*", "", raw).replace("```", "").strip()
    return raw


async def _generate_html_async(plan: dict) -> str:
    """Part 2 (async): produce the full interactive HTML from the plan."""
    prompt = f"Animation Plan (JSON):\n{json.dumps(plan, indent=2)}"
    raw = await _call_claude_async(prompt, model=MODEL_HAIKU, max_tokens=16000, system=PART2_SYSTEM)
    raw = re.sub(r"```(?:html)?\s*", "", raw).replace("```", "").strip()
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Core Pipelines
# ─────────────────────────────────────────────────────────────────────────────

def _run_pipeline_sync(
    question: str,
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

    log_msg(f"Model (Part 1): {MODEL_SONNET}")
    log_msg(f"Model (Part 2): {MODEL_HAIKU}")

    # ── Part 1: Explanation + Animation Plan ──────────────────────────────
    log_msg("Part 1 → Generating explanation + animation plan ...")
    plan = _generate_plan_sync(question)
    topic = plan.get("topic", "animation")
    log_msg(f"  Topic: {topic}")
    log_msg(f"  Components: {len(plan.get('components', []))}")
    log_msg(f"  Solution steps: {len(plan.get('solution_steps', []))}")
    log_msg(f"  Scenes: {len(plan.get('scenes', []))}")

    # ── Part 2: Full HTML ─────────────────────────────────────────────────
    log_msg("Part 2 → Generating interactive HTML animation ...")
    html = _generate_html_sync(plan)
    log_msg(f"  HTML length: {len(html):,} chars")

    # ── Write to disk ─────────────────────────────────────────────────────
    if output_path is None:
        safe_name = re.sub(r"[^\w]+", "_", topic.lower())[:40]
        output_path = os.path.join(OUTPUT_DIR, f"{safe_name}_animation.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    log_msg(f"  Written to: {output_path}  ({len(html):,} bytes)")

    return html, output_path, plan


async def _run_pipeline_async(question: str, verbose: bool = True) -> tuple[str, dict]:
    """
    Async 2-part pipeline: question → (html_string, plan_dict).
    Designed for use inside FastAPI without blocking the event loop.
    """
    def log_msg(msg: str) -> None:
        if verbose:
            print(f"[q_animation] {msg}")
        log.info(f"[q_animation] {msg}")

    log_msg("Part 1 → Generating explanation + animation plan ...")
    plan = await _generate_plan_async(question)
    topic = plan.get("topic", "animation")
    log_msg(f"  Topic: {topic}")
    log_msg(f"  Scenes: {len(plan.get('scenes', []))}")

    log_msg("Part 2 → Generating interactive HTML animation ...")
    html = await _generate_html_async(plan)
    log_msg(f"  HTML length: {len(html):,} chars")

    return html, plan


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Public API — CLI / direct import
# ─────────────────────────────────────────────────────────────────────────────

def generate_animation(
    question: str,
    output_path: str | None = None,
    verbose: bool = True,
) -> str:
    """
    Full pipeline: question → premium interactive HTML file.

    Parameters
    ----------
    question    : Natural-language engineering question.
    output_path : Where to write the .html file.  Auto-generated if None.
    verbose     : Print progress messages.

    Returns
    -------
    str : Path to the generated HTML file.
    """
    _, written_path, _ = _run_pipeline_sync(
        question=question,
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

    FIX: Now uses native async Claude calls instead of wrapping sync
    urllib/Gemini calls in run_in_executor. No more Railway 502 timeouts.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("'question' field cannot be empty.")

    html, plan = await _run_pipeline_async(question, verbose=True)

    # Build the plain-English explanation from Part 1 data
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
    print("  Powered by Claude (Anthropic)  — claude-sonnet-4-6")
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
        verbose=True,
    )
    print(f"\nDone! Open in browser:  {out}")
