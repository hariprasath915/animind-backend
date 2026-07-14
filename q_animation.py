"""
q_animation.py — Fully Refactored + Backend-Connected
======================================================
Generates premium, step-by-step interactive SVG engineering animations from a
natural-language question.  Uses **Google Gemini 3.1 Pro Preview** (via the official
google-genai SDK) for every AI step.

Architecture (3-Part Pipeline)
-------------------------------
Part 1  → Gemini produces a structured *Explanation Script*
           (given data, formulas, step-by-step calculations, final answer).
Part 2  → Gemini produces a *Scene-by-Scene Animation Plan*
           (which SVG components appear, when, with what motion).
Part 3  → Gemini produces the final single-file *High-Rich Interactive SVG HTML*
           document that exactly follows Parts 1 & 2.

Backend Integration
-------------------
  • generate_question_animation(question) → async function called by main.py
    via POST /generate-question-animation.
  • Returns { animation_code, title, explanation } as expected by index.html.

Animation Workflow (Part 2 / Part 3 contract)
----------------------------------------------
Every problem, regardless of type, follows this 4-scene structure:

  Scene 0  — "SETUP"
    Show the basic diagram/frame, fixed geometry, and a "Given Data" card
    with all known values. No motion yet; just the stage.

  Scene 1  — "ELEMENTS"
    Introduce the main moving element(s) one by one with fade-in + motion
    (rotation, translation, oscillation, etc.). Use blur-shield to focus.

  Scene 2  — "LINKAGE / INTERACTION"
    Show how elements interact (connecting rod + slider, gear mesh, belt,
    beam deflection …). Continue motion. Introduce second-level elements.

  Scene 3  — "SOLUTION"
    Freeze (or highlight) the key state (specific angle, load, instant).
    Show the formula box with numbered steps. Display final answer vector /
    result. All components visible; blur-shield off.

The SVG HTML must:
  • Have a problem-statement banner at the top.
  • Have a 16:9 SVG stage (viewBox="0 0 850 450") in the middle.
  • Have a control panel at the bottom with step dots, info card, Prev/Next.
  • Use the dark-mechanical palette from DESIGN_TOKENS below.
  • Keep ALL CSS and JS inline — single self-contained file.
  • Drive all animation through requestAnimationFrame.
  • Use smooth CSS opacity transitions (0.6 s) for layer reveal.

FIX LOG
-------
• 2026-07-14  Original 4-call pipeline collapsed to 3 parts for clarity.
              PART1_SYSTEM now generates a rigorous explanation script.
              PART2_SYSTEM produces a JSON scene plan with strict 4-scene
              structure (SETUP / ELEMENTS / LINKAGE / SOLUTION).
              PART3_SYSTEM is a comprehensive HTML-generation prompt that
              embeds both the explanation text and scene plan, and includes
              exact structural requirements derived from the reference
              Slider-Crank and Flat-Belt HTML templates.
              Model updated to gemini-2.5-flash (June 2026 release).

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

GEMINI_MODEL = "gemini-3.1-pro-preview"
OUTPUT_DIR   = "."
MAX_RETRIES  = 3

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
            is_transient = any(k in err_str for k in (
                "429", "503", "quota", "resource exhausted", "unavailable"
            ))
            if is_transient and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                log.warning(
                    f"[q_animation] Gemini transient error, retrying in {wait}s "
                    f"(attempt {attempt}): {exc}"
                )
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
# 2.  Part 1 — Explanation Script (backend-only; drives the animation)
# ─────────────────────────────────────────────────────────────────────────────

PART1_SYSTEM = """\
You are an expert engineering educator.

Given ANY engineering, physics, or mathematics question, produce a COMPLETE
structured JSON "explanation script" that will drive an interactive SVG
animation.  This script is BACKEND ONLY — it is never shown raw to the user,
but its data, formulas, and results populate SVG labels and formula boxes.

Return ONLY valid JSON — no markdown fences, no preamble, no trailing text.

{
  "topic": "<short topic name>",
  "subject_area": "<Kinematics | Statics | Dynamics | Thermodynamics | Electrical | Fluid Mechanics | Structural | Mathematics | Other>",
  "problem_statement": "<full verbatim question text>",

  "given": [
    {"symbol": "<e.g. r>", "value": "<e.g. 50>", "unit": "<e.g. mm>", "description": "<Crank length>"}
  ],

  "find": "<what is being calculated, e.g. 'Linear velocity of piston at θ=60°'>",

  "formulas": [
    {
      "name": "<formula name, e.g. Angular Velocity>",
      "expression": "<plain-text formula, e.g. ω = 2πN/60>",
      "purpose": "<one sentence: what this formula does>"
    }
  ],

  "solution_steps": [
    {
      "step_number": 1,
      "title": "<e.g. Convert RPM to rad/s>",
      "formula_used": "<formula name from formulas list>",
      "formula_text": "<formula in plain text>",
      "substitution_text": "<formula with actual numbers substituted>",
      "result_text": "<result with unit>",
      "result_numeric": "<just the number + unit, e.g. 31.42 rad/s>"
    }
  ],

  "final_answer": {
    "symbol": "<e.g. V>",
    "value": "<numeric>",
    "unit": "<unit>",
    "statement": "<one-sentence plain-English summary of the result>"
  },

  "components": [
    {
      "id": "<short_snake_case_id, e.g. crank>",
      "label": "<human label, e.g. Crank Arm>",
      "shape": "<circle | line | rectangle | arc | polygon | path | gear | arrow | beam | custom>",
      "role": "<one sentence: what this component physically does in the problem>",
      "color_hint": "<hex, e.g. #66fcf1>",
      "motion_type": "<none | rotate | translate | oscillate | pulse | continuous_rotate>",
      "pivot_or_anchor": "<e.g. (200, 250) — SVG coordinates in an 850×450 canvas>",
      "approx_size": "<pixels>",
      "layer_order": "<integer; lower = drawn first / behind>"
    }
  ],

  "diagram_notes": "<2–3 sentences describing the overall geometry of the diagram: where the origin is, what scale factor maps physical units to pixels, and which direction is positive.>"
}

Rules:
- Every solution_step must have a formula_text, substitution_text, AND result_text.
- final_answer.value must be the number that will be shown in the SVG answer box.
- component ids must be unique, lowercase, underscored.
- Assume SVG canvas 850 wide × 450 tall, origin top-left, Y increases downward.
"""


def _generate_explanation(question: str, api_key: str | None = None) -> dict:
    """Part 1: produce the structured explanation script."""
    prompt = f"{PART1_SYSTEM}\n\nQuestion:\n{question}"
    raw = _call_gemini(prompt, api_key=api_key, max_tokens=6000)
    return _extract_json(raw)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Part 2 — Scene Plan  (4-scene animation workflow)
# ─────────────────────────────────────────────────────────────────────────────

PART2_SYSTEM = """\
You are an SVG animation director.

You will receive a JSON "explanation script" for an engineering/physics/maths
problem.  Produce a JSON "scene plan" that describes EXACTLY 4 scenes for the
interactive SVG animation.

The 4 scenes MUST follow this generic workflow (adapt the content to the
specific problem — do NOT hardcode Slider-Crank details):

  Scene 0 — "SETUP"
    • Show the fixed frame / base geometry only.
    • Reveal a "Given Data" card top-left with 2–4 key parameter values from
      the explanation script.
    • No motion; blur_shield = 0.
    • visible_layer_ids: the frame/base component only.

  Scene 1 — "ELEMENTS"
    • Introduce the first/main moving element (first component by layer_order).
    • Apply blur_shield ~ 0.6 to focus on it.
    • Show its angular velocity, speed, or primary parameter in an overlay label.
    • Start continuous animation for this element (rotate, oscillate, etc.).
    • visible_layer_ids: frame + this element.

  Scene 2 — "LINKAGE"
    • Reveal all remaining elements one by one.
    • Reduce blur_shield to ~ 0.4.
    • Show the kinematic relationship or interaction between elements.
    • Continue motion for all revealed elements.
    • visible_layer_ids: all elements.

  Scene 3 — "SOLUTION"
    • Freeze or highlight the key state (specific angle, load case, instant).
    • blur_shield = 0 (all visible).
    • Show a formula box (dark rect with green border) with all solution_steps.
    • Show the final answer with a red-highlighted value and cyan glow.
    • Show a result vector / arrow on the diagram at the answer location.
    • visible_layer_ids: all elements + answer overlay.

Return ONLY valid JSON, no markdown, no preamble:

{
  "scenes": [
    {
      "index": 0,
      "scene_key": "SETUP",
      "label": "<SHORT CAPS label ≤8 chars, e.g. SETUP>",
      "title": "<Scene title shown in info card>",
      "description": "<2-3 sentences shown in the info panel below the SVG>",
      "badges": [
        {"text": "<badge text>", "color": "<cyan|orange|green|red>"}
      ],
      "visible_layer_ids": ["<component id from explanation script>"],
      "focused_layer_ids": [],
      "blur_shield_opacity": 0.0,
      "start_continuous_anim": false,
      "freeze_at_angle_deg": null,
      "show_formula_box": false,
      "show_final_answer": false,
      "overlay_annotations": [
        {
          "type": "<given_data_card | label | angle_arc | velocity_vector | formula_box>",
          "content": "<text or formula shown>",
          "svgx": "<approx SVG x position>",
          "svgy": "<approx SVG y position>"
        }
      ]
    }
  ],
  "formula_box_steps": [
    {
      "line": "<line of text for the formula box, e.g. V = ωr(sinθ + sin2θ/2n)>"
    }
  ],
  "answer_overlay": {
    "symbol": "<e.g. V>",
    "value": "<e.g. 1.53>",
    "unit": "<e.g. m/s>",
    "label": "<e.g. Piston Velocity>",
    "svgx": "<x position for arrow start>",
    "svgy": "<y position for arrow>"
  }
}

Important rules:
- Exactly 4 scenes, indexes 0–3.
- scene_key must be exactly one of: SETUP, ELEMENTS, LINKAGE, SOLUTION.
- visible_layer_ids must use component ids from the explanation script.
- freeze_at_angle_deg: null for scenes 0–2; the key angle (e.g. 60) for scene 3
  if the problem has a specific angle; otherwise null.
- overlay_annotations for scene 0 MUST include a given_data_card.
- overlay_annotations for scene 3 MUST include a formula_box and a
  velocity_vector (or result arrow).
- formula_box_steps must show all solution steps in readable mono format.
"""


def _generate_scene_plan(explanation: dict, api_key: str | None = None) -> dict:
    """Part 2: produce the 4-scene animation plan from the explanation script."""
    prompt = (
        f"{PART2_SYSTEM}\n\n"
        f"Explanation Script (JSON):\n{json.dumps(explanation, indent=2)}"
    )
    raw = _call_gemini(prompt, api_key=api_key, max_tokens=6000)
    return _extract_json(raw)


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Part 3 — High-Rich Interactive SVG HTML
# ─────────────────────────────────────────────────────────────────────────────

PART3_SYSTEM = """\
You are a world-class SVG/JavaScript animation engineer.

You will receive:
  A) An "explanation script" (Part 1 JSON) — problem data, formulas, solution steps.
  B) A "scene plan" (Part 2 JSON) — 4-scene animation workflow with component ids,
     blur values, overlays, freeze angles, formula box content.

Produce a COMPLETE, self-contained <!DOCTYPE html> interactive animation file.
Return ONLY the raw HTML starting with <!DOCTYPE html> and ending with </html>.
No markdown fences. No preamble. No trailing commentary.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRUCTURAL REQUIREMENTS  (non-negotiable)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. PAGE LAYOUT
   ┌──────────────────────────────────────────┐
   │  .problem-banner  (dark panel, top)      │
   │  .svg-container  (16:9, SVG stage)       │
   │  .control-panel  (step dots + info card) │
   └──────────────────────────────────────────┘
   Max width 850 px, centred.

2. PROBLEM BANNER
   <div class="problem-banner">
     <span class="banner-label">PROBLEM</span>
     <p class="banner-text">{ problem_statement from explanation script }</p>
   </div>

3. SVG STAGE  (viewBox="0 0 850 450", preserveAspectRatio="xMidYMid slice")
   <defs> must contain:
     a. Grid pattern (40×40, white 5% opacity)
     b. Metallic gradients: "steel", "darkMetal"
     c. Belt/rope gradient if needed: "beltGrad"
     d. Filters: "dropShadow", "glowCyan", "glowOrange"
     e. Arrow markers: "arrowCyan", "arrowOrange", "arrowGreen", "arrowRed"
        (all orient="auto", 6×6, refX/refY=3)

   Layer stacking order in SVG (bottom to top):
     i.  <rect width="100%" height="100%" fill="url(#grid)"/>  — always visible
     ii. One <g class="svg-layer" id="layer-{componentId}" style="opacity:0;">
         per component, in layer_order sequence.
     iii.<rect id="blur-shield" width="100%" height="100%"
              fill="#050508" opacity="0"
              class="svg-layer" pointer-events="none"/>
     iv. One <g class="svg-layer" id="overlay-scene{N}" style="opacity:0;">
         per scene N = 0..3 — contains all annotation SVG elements for that scene.

   Inside each component layer, draw a REALISTIC, HIGH-QUALITY SVG depiction:
   - Use gradients, filters (dropShadow), metallic fills.
   - Mechanical parts: thick strokes with inner lighter stroke for 3-D look.
   - Pivot pins: two concentric circles (outer dark, inner bright).
   - Gears: proper tooth paths using SVG <path> arcs.
   - Pistons/sliders: rect with internal ribbing lines.
   - Beams/rods: thick rounded linecap strokes.
   - Every animated element MUST have an id on the element that JS will target.

4. INSIDE overlay-scene0 (SETUP overlay):
   - Text labels for fixed points (e.g. "Fixed Pivot (O)", "Cylinder Guide").
   - A "Given Data" card:
       <rect x="30" y="30" width="200" height="{ height }" rx="6"
             fill="rgba(31,40,51,0.85)" stroke="#555" stroke-width="1"
             filter="url(#dropShadow)"/>
       <text x="45" y="55" fill="#66fcf1" font-size="14" font-weight="bold">
         { topic } Parameters:
       </text>
       <!-- one <text> per given item from explanation script -->

5. INSIDE overlay-scene1 (ELEMENTS overlay):
   - Rotation arc arrow (dashed, cyan) near the main element.
   - Text label: main element's angular velocity / speed.
   - Optional callout pill showing the primary parameter.

6. INSIDE overlay-scene2 (LINKAGE overlay):
   - A small callout box showing the kinematic relationship or key
     dimension connecting the elements.
   - Dashed dimension lines between elements if appropriate.

7. INSIDE overlay-scene3 (SOLUTION overlay):
   - Freeze/snapshot marker (e.g. a red angle arc with label, or a highlight rect).
   - Result VECTOR ARROW: a <line> from the answer location in the direction of
     the result, plus a <text> label with the final answer value.
       e.g. <line id="result-vector" x1="..." y1="..." x2="..." y2="..."
                  stroke="#ff3366" stroke-width="4"
                  marker-end="url(#arrowRed)"/>
            <text ...>{ symbol } = { value } { unit }</text>
   - FORMULA BOX (dark rect, green border):
       Position: top-right quadrant (~x=420 to 800, y=30 to 165).
       Content: kinematic solution header + all solution_steps from explanation
       script, laid out as monospace <text> rows.
       Final answer line in cyan, with the numeric value in red (#ff3366).
       Example structure:
         <rect x="420" y="30" width="370" height="135" rx="8"
               fill="rgba(15,15,19,0.9)" stroke="#97c459" stroke-width="1.5"
               filter="url(#dropShadow)"/>
         <text x="605" y="55" fill="#97c459" font-size="14" font-weight="bold"
               text-anchor="middle" letter-spacing="1">SOLUTION</text>
         <line x1="440" y1="65" x2="780" y2="65" stroke="#333" stroke-width="1"/>
         <!-- one <text> per formula_box_steps line -->
         <text x="440" y="..." fill="#66fcf1" font-size="16" font-weight="bold"
               font-family="monospace">
           { symbol } = <tspan fill="#ff3366">{ value } { unit }</tspan>
         </text>

8. CONTROL PANEL
   <div class="control-panel">
     <div class="step-indicator" id="dots">
       <!-- 4 step-dots (one per scene), plus step-label span -->
     </div>
     <div class="info-box">
       <h3 id="info-title">{ topic }</h3>
       <div class="badges" id="info-badges"></div>
       <div class="info-desc" id="info-desc">Click Next Step to begin.</div>
     </div>
     <div class="actions">
       <button class="btn-secondary" onclick="resetAnim()">↺ Restart</button>
       <button class="btn-primary" id="btn-next" onclick="nextStep()">
         Next Step ▶
       </button>
     </div>
   </div>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CSS  (all inline, inside <style>)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
:root {
  --bg-color: #0b0c10;
  --panel-bg: #1f2833;
  --text-main: #c5c6c7;
  --accent-cyan: #66fcf1;
  --accent-cyan-dim: #45a29e;
  --accent-orange: #fca311;
  --accent-green: #97c459;
  --accent-red: #ff3366;
  --border-radius: 12px;
}
.svg-layer { transition: opacity 0.6s cubic-bezier(0.4,0,0.2,1); }
.step-dot.active { background: var(--accent-cyan); box-shadow: 0 0 10px var(--accent-cyan); transform: scale(1.2); }
.badge-cyan   { background: rgba(102,252,241,0.1); border:1px solid var(--accent-cyan-dim); color:var(--accent-cyan); }
.badge-orange { background: rgba(252,163,17,0.1);  border:1px solid #b3730b;               color:var(--accent-orange); }
.badge-green  { background: rgba(151,196,89,0.1);  border:1px solid #6b933a;               color:var(--accent-green); }
.badge-red    { background: rgba(255,51,102,0.1);  border:1px solid #991f3d;               color:var(--accent-red); }
.problem-banner {
  background: linear-gradient(135deg, #12161d 0%, #1a2030 100%);
  border-bottom: 1px solid #2a3040;
  padding: 14px 20px;
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.banner-label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 2px;
  color: var(--accent-cyan-dim);
  padding: 3px 8px;
  border: 1px solid var(--accent-cyan-dim);
  border-radius: 4px;
  white-space: nowrap;
  margin-top: 2px;
}
.banner-text {
  font-size: 13px;
  color: var(--text-main);
  line-height: 1.5;
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
JAVASCRIPT  (inside <script> at end of <body>)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A. CONFIG object — stores all physical/pixel dimensions derived from the
   explanation script (crank radius in px, connecting rod length in px, pivot
   coordinates, scale factor, omega, etc.).

B. Per-component animate functions:
     function animate_{componentId}(theta) { ... }
   Each function updates the SVG element's geometry for the given angle/state.
   For multi-link mechanisms, derive all joint positions from the master angle.

C. requestAnimationFrame master loop:
     let theta = 0, isAnimating = true, isFreezing = false, targetTheta = null;
     function loop() {
       if (isAnimating) {
         if (isFreezing && targetTheta !== null) {
           // Ease toward targetTheta
           let diff = targetTheta - (theta % (Math.PI*2));
           if (diff < -Math.PI) diff += Math.PI*2;
           if (diff > Math.PI)  diff -= Math.PI*2;
           theta += diff * 0.05;
           if (Math.abs(diff) < 0.005) { theta = targetTheta; isAnimating = false; }
         } else {
           theta += CONFIG.omega;
           if (theta > Math.PI*2) theta -= Math.PI*2;
         }
         // Call all animate functions
         animate_{componentId1}(theta);
         animate_{componentId2}(theta);
         // ...
       }
       requestAnimationFrame(loop);
     }
     loop();

D. stepsData array — exactly 4 entries matching the 4 scenes:
     const stepsData = [
       {
         label:       "{ scene.label }",
         blurOp:      { scene.blur_shield_opacity },
         overlays:    ["overlay-scene0"],   // scene-specific overlays to show
         layerOps:    { componentId: opacity, ... },  // 0 or 1 per component layer
         animating:   { scene.start_continuous_anim },
         freezing:    { true if scene 3 and freeze_at_angle_deg is not null },
         targetTheta: { scene3: freeze_at_angle_deg * Math.PI/180, others: null },
         title:       "{ scene.title }",
         badges:      "{ HTML string of badge spans }",
         desc:        "{ scene.description }"
       },
       // ... scenes 1, 2, 3
     ];

E. applyStep(idx) — applies stepsData[idx]:
   - Sets blur-shield opacity.
   - Shows/hides component layers (layerOps).
   - Shows/hides overlay-sceneN groups.
   - Updates isAnimating, isFreezing, targetTheta.
   - Updates info-title, info-badges, info-desc, step-label.
   - Updates step-dot active class.
   - Hides/shows btn-next.

F. nextStep(), prevStep() (or just nextStep + resetAnim):
     let currentStep = -1;
     function nextStep() { if (currentStep < 3) applyStep(++currentStep); }
     function resetAnim() {
       currentStep = 0; theta = 0; isAnimating = false; isFreezing = false;
       // reset all layers to opacity 0 except frame
       applyStep(0);
     }

G. Initialize: setTimeout(() => resetAnim(), 100);

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUALITY CHECKLIST  (verify before outputting)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
□ Problem statement shown in banner (full text from explanation script).
□ All given values shown in scene-0 Given Data card.
□ All 4 overlay groups (overlay-scene0 … overlay-scene3) present in SVG.
□ blur-shield transitions correctly per scene.
□ Component layers start at opacity:0 (except frame at opacity:1).
□ Formula box in scene-3 overlay contains ALL solution steps.
□ Final answer value highlighted in red (#ff3366).
□ requestAnimationFrame loop present and functional.
□ Freeze-to-angle logic present in loop (for scene 3 if applicable).
□ resetAnim() resets theta to 0 and returns to scene 0.
□ Step dots update correctly on each step.
□ Next Step button hidden on last step; Restart always visible.
□ No external dependencies — single self-contained HTML file.
□ SVG viewBox exactly "0 0 850 450".
□ All element ids referenced in JS actually exist in the SVG.
"""


def _generate_html(
    explanation: dict,
    scene_plan: dict,
    api_key: str | None = None,
) -> str:
    """Part 3: produce the full interactive HTML from explanation + scene plan."""
    prompt = (
        f"{PART3_SYSTEM}\n\n"
        f"=== PART A: Explanation Script (JSON) ===\n"
        f"{json.dumps(explanation, indent=2)}\n\n"
        f"=== PART B: Scene Plan (JSON) ===\n"
        f"{json.dumps(scene_plan, indent=2)}"
    )
    raw = _call_gemini(prompt, api_key=api_key, max_tokens=16384)
    # Strip any accidental markdown fences
    raw = re.sub(r"```(?:html)?\s*", "", raw).replace("```", "").strip()
    # Ensure it starts with a doctype
    if not raw.lower().startswith("<!doctype"):
        idx = raw.lower().find("<!doctype")
        if idx != -1:
            raw = raw[idx:]
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Core Synchronous Pipeline
# ─────────────────────────────────────────────────────────────────────────────

def _run_pipeline(
    question: str,
    api_key: str | None = None,
    output_path: str | None = None,
    verbose: bool = True,
) -> tuple[str, str, dict, dict]:
    """
    Synchronous 3-part pipeline: question → HTML.

    Returns
    -------
    tuple of (html_string, output_path_written, explanation_dict, scene_plan_dict)
    """
    def log_msg(msg: str) -> None:
        if verbose:
            print(f"[q_animation] {msg}")
        log.info(f"[q_animation] {msg}")

    log_msg(f"Gemini model: {GEMINI_MODEL}")

    # Part 1 — Explanation Script
    log_msg("Part 1 → Generating explanation script ...")
    explanation = _generate_explanation(question, api_key=api_key)
    topic = explanation.get("topic", "animation")
    log_msg(f"  Topic: {topic}")
    log_msg(f"  Subject area: {explanation.get('subject_area', 'Unknown')}")
    log_msg(f"  Components: {len(explanation.get('components', []))}")
    log_msg(f"  Solution steps: {len(explanation.get('solution_steps', []))}")

    # Part 2 — Scene Plan
    log_msg("Part 2 → Generating 4-scene animation plan ...")
    scene_plan = _generate_scene_plan(explanation, api_key=api_key)
    log_msg(f"  Scenes generated: {len(scene_plan.get('scenes', []))}")

    # Part 3 — High-Rich Interactive SVG HTML
    log_msg("Part 3 → Generating interactive HTML animation ...")
    html = _generate_html(explanation, scene_plan, api_key=api_key)
    log_msg(f"  HTML length: {len(html):,} chars")

    # Write to disk
    if output_path is None:
        safe_name = re.sub(r"[^\w]+", "_", topic.lower())[:40]
        output_path = os.path.join(OUTPUT_DIR, f"{safe_name}_animation.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    log_msg(f"  Written to: {output_path}  ({len(html):,} bytes)")

    return html, output_path, explanation, scene_plan


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Public API — CLI / direct import
# ─────────────────────────────────────────────────────────────────────────────

def generate_animation(
    question: str,
    api_key: str | None = None,
    output_path: str | None = None,
    verbose: bool = True,
) -> str:
    """
    Full 3-part pipeline: question → premium interactive HTML file.

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
    _, written_path, _, _ = _run_pipeline(
        question=question,
        api_key=api_key,
        output_path=output_path,
        verbose=verbose,
    )
    return written_path


# ─────────────────────────────────────────────────────────────────────────────
# 7.  Backend API — called by main.py via POST /generate-question-animation
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

    html, _, explanation, _ = await loop.run_in_executor(
        None,
        lambda: _run_pipeline(question=question, verbose=True),
    )

    final = explanation.get("final_answer", {})
    explanation_text = (
        final.get("statement")
        or f"Topic: {explanation.get('topic', question[:80])}"
    )

    return {
        "animation_code": html,
        "title":          explanation.get("topic", question[:80]),
        "explanation":    explanation_text,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 8.  Example Questions  (for CLI testing)
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
    "flat_belt": (
        "A flat belt transmits 8 kW of power at a belt speed of 15 m/s. "
        "Find the difference between the tight side and slack side tension."
    ),
    "simply_supported_beam": (
        "A simply supported beam of span 6 m carries a central point load "
        "of 20 kN. Determine the maximum bending moment and the reactions "
        "at the supports."
    ),
    "projectile": (
        "A ball is projected at an angle of 30° with an initial velocity "
        "of 40 m/s. Find the maximum height, time of flight, and horizontal "
        "range. Ignore air resistance."
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# 9.  CLI Entry Point
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
