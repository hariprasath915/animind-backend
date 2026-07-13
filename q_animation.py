"""
q_animation.py — Fully Refactored
===================================
Generates premium, step-by-step interactive SVG engineering animations from a
natural-language question.  Uses **Google Gemini 2.5 Pro** for every AI step;
no Anthropic API calls remain.

Architecture (3-Part Pipeline)
-------------------------------
Part 1  → Gemini produces a structured *Explanation Script*
           (problem statement + full mathematical solution).
Part 2  → Gemini produces a structured *Step-by-Step Animation Sequence*
           (scene descriptions, focus/blur notes, visual overlays, badge data).
Part 3  → Gemini (or a deterministic builder guided by Parts 1 & 2) produces
           the final single-file *High-Rich Interactive SVG HTML* document.

The output style matches the reference slider-crank example:
  • Dark dashboard shell  (#0b0c10 / #1f2833 palette)
  • 850 × 450 SVG stage with grid, metallic gradients, drop-shadows
  • Layered SVG groups with opacity transitions
  • Blur-shield overlay for focus/defocus effects
  • Step-dot progress indicator + info card with badges
  • Physics / geometry engine in pure JS (requestAnimationFrame loop)
  • "Next Step ▶ / Restart" control buttons
  • Optional question-banner at the top

Usage
-----
  python q_animation.py
  # (or import and call generate_animation(question, api_key))

Environment variable:  GEMINI_API_KEY
                   or  pass api_key= argument to generate_animation().
"""

import os
import re
import json
import textwrap
import google.generativeai as genai

# ─────────────────────────────────────────────────────────────────────────────
# 0.  Configuration
# ─────────────────────────────────────────────────────────────────────────────

GEMINI_MODEL = "gemini-2.5-pro"          # High-level Gemini model
OUTPUT_DIR   = "."                       # Where .html files are written

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
# 1.  Gemini Client Helper
# ─────────────────────────────────────────────────────────────────────────────

def _init_gemini(api_key: str | None = None) -> genai.GenerativeModel:
    """Configure the Gemini SDK and return a GenerativeModel instance."""
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise EnvironmentError(
            "Gemini API key not found.  Set the GEMINI_API_KEY environment "
            "variable or pass api_key= to generate_animation()."
        )
    genai.configure(api_key=key)
    return genai.GenerativeModel(GEMINI_MODEL)


def _call_gemini(model: genai.GenerativeModel, prompt: str) -> str:
    """Send a prompt and return the raw text response."""
    response = model.generate_content(prompt)
    return response.text.strip()


def _extract_json(raw: str) -> dict | list:
    """
    Extract and parse the first JSON object/array from a Gemini response,
    tolerating markdown code-fences.
    """
    # Strip ```json ... ``` or ``` ... ```
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    # Find the first { or [ and pair it
    start = next((i for i, c in enumerate(cleaned) if c in "{["), None)
    if start is None:
        raise ValueError(f"No JSON found in Gemini response:\n{raw[:300]}")
    return json.loads(cleaned[start:])


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Part 1 — Explanation Script Generator
# ─────────────────────────────────────────────────────────────────────────────

PART1_SYSTEM = """
You are an expert engineering educator and visual explainer.

Given ANY engineering or science question, produce a COMPLETE structured
Explanation Script that will drive a step-by-step animated visual explanation.

Your job is to:
1. Identify every physical object / component in the problem (gears, beams,
   pulleys, pistons, fluids, circuits, etc.) and decide the ORDER in which
   they should be DRAWN on screen — simplest first, building up to the full scene.
2. For EACH drawn component, describe a short animation that shows its ROLE
   (rotation, sliding, force direction, flow, current, etc.).
3. Break the mathematical solution into SIMPLE LOGICAL STEPS — one formula per step.
4. Design the visual layout appropriate to the question type:
   • Mechanical → dark metallic engineering dashboard
   • Electrical → dark circuit board / oscilloscope feel
   • Fluid/Thermal → dark navy + blue/orange gradient
   • Structural → dark concrete + steel grey
   • Chemical / Biology → dark lab feel with molecule colours
   Use whichever palette fits — do NOT default to one style.

Return ONLY valid JSON — no markdown, no preamble, no trailing text.

{
  "topic": "<short topic name, e.g. 'Spur Gear Speed Ratio'>",
  "subject_area": "<e.g. 'Kinematics' | 'Statics' | 'Thermodynamics' | 'Electrical' | 'Fluid Mechanics' | 'Structural' | 'General'>",
  "visual_theme": {
    "palette": "<e.g. 'dark_mechanical' | 'dark_electrical' | 'dark_fluid' | 'dark_structural' | 'dark_chemistry'>",
    "stage_bg_color": "<hex, e.g. '#05080f'>",
    "accent_color":   "<hex, primary highlight>",
    "accent2_color":  "<hex, secondary highlight>",
    "note": "<one sentence describing overall visual feel>"
  },
  "problem_statement": "<full verbatim question text>",
  "given": [
    {"symbol": "<plain text symbol>", "value": "<numeric>", "unit": "<unit>", "description": "<label>"}
  ],
  "find": "<what must be calculated>",
  "components": [
    {
      "id": "<short_snake_case_id, e.g. 'driving_gear'>",
      "draw_order": 1,
      "label": "<human label, e.g. 'Driving Gear (20 teeth)'>",
      "shape": "<e.g. 'circle' | 'rectangle' | 'line' | 'gear' | 'pulley' | 'arrow' | 'beam' | 'custom'>",
      "approx_cx": "<0-850 pixel x centre on 850×450 canvas>",
      "approx_cy": "<0-450 pixel y centre>",
      "approx_size": "<radius or width in pixels>",
      "color_hint": "<fill color hex>",
      "role_animation": "<one sentence: what motion/animation shows its role, e.g. 'Rotates clockwise at 900 RPM; show teeth moving'>",
      "label_position": "<'top' | 'bottom' | 'left' | 'right'>",
      "arrow_hints": ["<optional arrows to draw, e.g. 'rotation arrow CW'>"]
    }
  ],
  "solution_steps": [
    {
      "step_number": 1,
      "title": "<Step title>",
      "formula_text": "<formula in plain text or simple notation>",
      "substitution_text": "<formula with numbers substituted>",
      "result_text": "<result in plain text>",
      "result_numeric": "<numeric value + unit, e.g. '300 RPM'>",
      "focus_component_ids": ["<id of component(s) this step is about>"],
      "annotation": "<one sentence shown as on-screen annotation during this step>"
    }
  ],
  "final_answer": {
    "symbol": "<symbol>",
    "value": "<numeric>",
    "unit": "<unit>",
    "statement": "<one-sentence plain-English summary of the answer>",
    "highlight_components": ["<ids of components to highlight in the final scene>"]
  }
}
"""

def generate_explanation_script(model, question: str) -> dict:
    """Part 1: Ask Gemini to produce a rich, component-aware explanation script."""
    prompt = f"{PART1_SYSTEM}\n\nQuestion:\n{question}"
    raw = _call_gemini(model, prompt)
    return _extract_json(raw)


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Part 2 — Animation Sequence Generator
# ─────────────────────────────────────────────────────────────────────────────

PART2_SYSTEM = """
You are an expert engineering animation director.

Given a structured Explanation Script (Part 1 JSON), produce a complete
Step-by-Step Animation Sequence that drives a high-quality interactive SVG
animation.

CRITICAL RULES — read carefully:

1. SCENE COUNT:
   - ALWAYS produce scenes in this order:
     a) One INTRO scene  (index 0): show the blank stage + problem parameters.
     b) One DRAW scene per component in "components" (sorted by draw_order):
        draw that component, slightly BLUR all previously drawn components,
        then play its role_animation, then UN-BLUR everything.
     c) One SOLUTION scene per solution_step in "solution_steps":
        focus on the relevant component(s), show the formula, show the result.
     d) One FINAL scene: show the complete scene with all components animated
        simultaneously, display the final answer prominently.
   - Total scenes = 1 + len(components) + len(solution_steps) + 1.
   - Index them 0, 1, 2, … continuously.

2. BLUR / FOCUS EFFECT:
   - When introducing a NEW component: set blur_shield_opacity to 0.55 on
     all PREVIOUSLY drawn layers, making the new component the focal point.
   - After the role animation finishes: set blur_shield_opacity to 0.0
     (full scene visible).
   - During SOLUTION scenes: blur everything EXCEPT the focused component(s).

3. ANIMATIONS:
   - Each component draw scene must have a "draw_animation" field describing
     how the component appears (fade-in, scale-up, slide-in, etc.).
   - Each component draw scene must have a "role_animation" field with the
     motion that demonstrates its function (rotation, translation, oscillation,
     flow, etc.).
   - Use "motion_type": "rotate" | "translate" | "oscillate" | "pulse" |
     "flow" | "static" to help the JS engine know what animation to run.

4. OVERLAYS:
   - Use labels with arrows to identify components.
   - Use formula boxes for solution steps.
   - Use value badges to show key numbers.
   - Use annotation text to explain what is happening.

5. badge_color must be one of: "cyan", "orange", "green", "red".

6. The "mechanism_type" must reflect the actual content:
   e.g. "gear_mesh" | "slider_crank" | "four_bar" | "belt_pulley" |
        "cam_follower" | "truss" | "beam_bending" | "fluid_flow" |
        "electrical_circuit" | "thermal" | "projectile" | "general"

Return ONLY valid JSON — no markdown, no preamble, no trailing text.

{
  "mechanism_type": "<type string>",
  "canvas_description": "<2-3 sentence description of the full finished scene>",
  "total_scenes": <integer>,
  "scenes": [
    {
      "index": 0,
      "scene_type": "intro",
      "label": "<SHORT CAPS label for dot indicator, max 8 chars>",
      "title": "<Scene title shown in info card>",
      "description": "<2-3 sentence explanation shown in the info card>",
      "badges": [{"text": "<badge text>", "color": "cyan|orange|green|red"}],
      "visible_component_ids": [],
      "focused_component_ids": [],
      "blur_shield_opacity": 0.0,
      "draw_animation": null,
      "role_animation": null,
      "motion_type": "static",
      "overlay_items": [
        {
          "type": "param_box",
          "title": "<box title>",
          "params": [{"label": "<label>", "value": "<value>", "color": "cyan|orange|green"}]
        }
      ],
      "show_formula_box": false,
      "formula_box": null,
      "show_final_answer": false,
      "start_continuous_anim": false,
      "freeze_at": null
    },
    {
      "index": 1,
      "scene_type": "draw_component",
      "label": "COMP 1",
      "title": "<e.g. 'Step 1: Driving Gear'>",
      "description": "<explain what this component is and its role>",
      "badges": [{"text": "<relevant data badge>", "color": "cyan"}],
      "visible_component_ids": ["<id of THIS component only>"],
      "focused_component_ids": ["<id of THIS component>"],
      "blur_shield_opacity": 0.55,
      "draw_animation": "<how it appears: 'fade-in' | 'scale-up' | 'slide-in-left' | 'slide-in-right' | 'drop-in'>",
      "role_animation": "<description of the motion that shows its role>",
      "motion_type": "rotate",
      "overlay_items": [
        {
          "type": "label_arrow",
          "text": "<component label>",
          "target_component_id": "<id>",
          "arrow_direction": "<'right' | 'left' | 'up' | 'down'>"
        },
        {
          "type": "rotation_arrow",
          "direction": "<'CW' | 'CCW'>",
          "target_component_id": "<id>"
        }
      ],
      "show_formula_box": false,
      "formula_box": null,
      "show_final_answer": false,
      "start_continuous_anim": true,
      "freeze_at": null
    },
    {
      "index": "<N>",
      "scene_type": "solution_step",
      "label": "CALC",
      "title": "<Step title from solution_steps>",
      "description": "<explanation of the calculation>",
      "badges": [{"text": "<result badge>", "color": "green"}],
      "visible_component_ids": ["<all drawn so far>"],
      "focused_component_ids": ["<component(s) relevant to this step>"],
      "blur_shield_opacity": 0.4,
      "draw_animation": null,
      "role_animation": null,
      "motion_type": "static",
      "overlay_items": [],
      "show_formula_box": true,
      "formula_box": {
        "title": "<step title>",
        "formula": "<formula text>",
        "substitution": "<numbers substituted>",
        "result": "<result text>",
        "result_color": "green"
      },
      "show_final_answer": false,
      "start_continuous_anim": false,
      "freeze_at": null
    },
    {
      "index": "<last>",
      "scene_type": "final",
      "label": "RESULT",
      "title": "Final Answer",
      "description": "<full final answer explanation>",
      "badges": [{"text": "<final value badge>", "color": "green"}],
      "visible_component_ids": ["<all component ids>"],
      "focused_component_ids": ["<all component ids>"],
      "blur_shield_opacity": 0.0,
      "draw_animation": null,
      "role_animation": "All components animate simultaneously at correct speeds.",
      "motion_type": "rotate",
      "overlay_items": [],
      "show_formula_box": false,
      "formula_box": null,
      "show_final_answer": true,
      "final_answer_box": {
        "symbol": "<symbol>",
        "value": "<numeric>",
        "unit": "<unit>",
        "statement": "<one-sentence summary>"
      },
      "start_continuous_anim": true,
      "freeze_at": null
    }
  ]
}
"""

def generate_animation_sequence(model, explanation: dict) -> dict:
    """
    Part 2: Ask Gemini to produce the animation sequence.

    The new sequence always follows:
      Intro → (one scene per component, draw + role anim) →
      (one scene per solution step, formula + focus) → Final answer scene.
    """
    prompt = (
        f"{PART2_SYSTEM}\n\n"
        f"Explanation Script:\n{json.dumps(explanation, indent=2)}"
    )
    raw = _call_gemini(model, prompt)
    return _extract_json(raw)


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Part 3 — HTML Builder  (Gemini-guided + deterministic shell)
# ─────────────────────────────────────────────────────────────────────────────

PART3_SYSTEM = """
You are an expert SVG/JavaScript animation engineer.
Given:
  • An Explanation Script (Part 1 JSON)   — defines components, colours, theme
  • An Animation Sequence (Part 2 JSON)  — defines scenes, overlays, draw order

Produce the COMPLETE BODY of an SVG scene — specifically the content that goes
INSIDE <svg id="stage" viewBox="0 0 850 450"> … </svg>.

STRUCTURE REQUIREMENTS:
  1. <defs> block — gradients, filters, markers, patterns tuned to the
     visual_theme in Part 1 (palette, accent_color, accent2_color).
  2. Background layer — grid, theme-appropriate background fill, subtle vignette.
  3. One <g class="svg-layer" id="layer-{component.id}" style="opacity:0;"> per
     component listed in Part 1 "components", in draw_order order.
     Each layer contains:
       - The drawn shape (gear, rectangle, circle, line, etc.) sized and
         positioned according to approx_cx/cy/size in Part 1.
       - An id on each animated sub-element that matches what the JS will target.
       - A static label text element (hidden by default, shown by JS).
  4. A blur-shield element:
       <rect id="blur-shield" x="0" y="0" width="850" height="450"
             fill="rgba(5,8,15,VAR)" opacity="0"
             style="pointer-events:none; transition: opacity 0.5s;"/>
     This sits ABOVE the component layers but BELOW the overlay groups.
  5. One <g id="overlay-scene{N}" style="opacity:0;"> per scene in Part 2.
     Each overlay group contains ONLY the overlays for that scene:
       - param_box:     dark rounded rect + header + key=value rows
       - label_arrow:   text + line with arrowhead pointing to the component
       - rotation_arrow: arc with curved arrowhead (CW or CCW)
       - formula_box:   dark rect + coloured border + formula / result rows
       - final_answer_box: prominent centred box with large value text
     All text is legible on the dark background.

STYLE CONVENTIONS:
  • Adapt the colour palette to visual_theme.palette from Part 1:
      dark_mechanical → #2a2a30 metal + cyan #66fcf1 accent
      dark_electrical → dark green #0d1f0d + lime #39ff14 accent
      dark_fluid      → dark navy #05080f + blue #4fc3f7 accent
      dark_structural → dark grey #1a1a1a + amber #fca311 accent
      dark_chemistry  → dark purple #120d1f + pink #ff79c6 accent
  • Drop shadow filter: id="dropShadow"
  • Glow filter using the accent color: id="glowAccent"
  • Arrow markers: id="arrowAccent" and id="arrowAccent2"
  • All component SVG elements that will be animated by JS MUST have id attributes.
  • Label text: fill="#fff" or accent color; font-size 12–14; font-family monospace.
  • Formula boxes: dark rect (fill="#0a0a0f") + accent-color border (stroke-width 1.5)
    + header text in accent color + formula rows in white monospace.
  • Final answer box: larger rect, bold text, accent2_color border + glow.

Return ONLY the raw SVG inner content (starting with <defs>, ending with the
last </g> before </svg>).  Do NOT include DOCTYPE, <html>, <head>, <body>,
<style>, <script>, or the outer <svg> tag itself.
"""

PART3_JS_SYSTEM = """
You are an expert JavaScript animation engineer.
Given:
  • An Explanation Script (Part 1 JSON)  — components, their positions/sizes,
    role_animations, visual theme
  • An Animation Sequence (Part 2 JSON)  — scenes, visible_component_ids,
    blur_shield_opacity, motion_type, overlay_items, formula boxes, final answer

Produce the COMPLETE JavaScript that drives the interactive animation.
Return ONLY the raw JavaScript code — no <script> tags, no markdown fences.

REQUIRED CODE STRUCTURE:

1. CONFIG object
   • Pull all numeric parameters (teeth, radii, RPM, lengths, etc.) from
     Part 1 "given" and "components" into a CONFIG object with pixel-scaled values.

2. Per-component animation functions
   • For EACH component in Part 1 "components", write a dedicated function
     animateComponent_{id}(t) that moves/rotates that component's SVG element(s).
   • Rotation:    use SVG transform="rotate(deg, cx, cy)"
   • Translation: use transform="translate(dx, dy)"
   • Oscillation: use a sine/cosine expression
   • Flow:        animate stroke-dashoffset
   • Pulse:       animate opacity or scale

3. Master animation loop
   • animationLoop(timestamp) using requestAnimationFrame.
   • Calls only the animateComponent_ functions for currently active components
     (tracked by activeComponents Set).
   • Supports speed multipliers per component.

4. Draw-reveal animation
   • revealComponent(id, drawAnimation) — fades in or slides in the SVG layer
     <g id="layer-{id}"> using CSS transitions and/or JS opacity animation.
     drawAnimation values: 'fade-in', 'scale-up', 'slide-in-left',
     'slide-in-right', 'drop-in'.

5. stepsData[] array
   • One object per scene in Part 2 "scenes", in order.
   • Each entry:
     {
       label:              "<dot label>",
       title:              "<info card title>",
       desc:               "<HTML description>",
       badges:             "<HTML badge spans>",
       visibleComponents:  ["<ids>"],
       focusedComponents:  ["<ids>"],
       blurShieldOpacity:  <0.0-0.7>,
       drawAnimation:      "<string | null>",
       motionType:         "<string>",
       startAnim:          <bool>,
       overlayIndex:       <scene index for overlay-sceneN>,
       showFormulaBox:     <bool>,
       showFinalAnswer:    <bool>
     }

6. applyStep(idx) function
   • Hides all overlay-sceneN groups (opacity:0).
   • Shows overlay-sceneN for this step (opacity:1).
   • Sets blur-shield opacity to stepsData[idx].blurShieldOpacity.
   • Reveals components in visibleComponents using revealComponent().
   • Starts/stops animation loop based on startAnim.
   • Updates dot indicators: removes 'active' from all, adds to current.
   • Updates #info-title, #info-badges, #info-desc, #step-label.
   • Disables #btn-next on last scene, changes text to "✓ Done".

7. nextStep() — advances currentStep, calls applyStep().

8. resetAnim() — stops animation loop, sets currentStep=0, hides all layers
   (opacity:0), hides all overlays (opacity:0), resets blur-shield (opacity:0),
   clears activeComponents, calls applyStep(0).

9. Call setTimeout(() => resetAnim(), 100) at the end.

IMPORTANT RULES:
  • Layer IDs MUST match the SVG: "layer-{component.id}" for each component.
  • Overlay IDs MUST match the SVG: "overlay-scene{N}" for each scene N.
  • Blur shield ID: "blur-shield".
  • Badge HTML: <span class="badge badge-cyan|badge-orange|badge-green|badge-red">
  • SVG Y-axis is INVERTED (y increases downward). For angles: use -sin for up.
  • For gear meshes: driven gear rotates opposite direction at speed ratio.
  • For slider-crank: piston x = cx + r·cos(θ) + √(L²−r²·sin²(θ)).
  • All transitions should be smooth (0.4–0.6s CSS or rAF easing).
  • Do NOT hardcode scene count to 4. Use the actual scenes array length
    from Part 2 "total_scenes".
"""

def generate_svg_body(model, explanation: dict, sequence: dict) -> str:
    """Part 3a: Ask Gemini to produce the SVG inner content."""
    # Build a concise component summary to guide SVG generation
    components = explanation.get("components", [])
    component_summary = "\n".join(
        f"  - id='{c['id']}' | label='{c['label']}' | shape='{c['shape']}' "
        f"| cx≈{c['approx_cx']} cy≈{c['approx_cy']} size≈{c['approx_size']} "
        f"| color={c['color_hint']} | role='{c['role_animation']}'"
        for c in sorted(components, key=lambda x: x.get("draw_order", 99))
    )
    scene_count = sequence.get("total_scenes", len(sequence.get("scenes", [])))
    theme = explanation.get("visual_theme", {})

    prompt = (
        f"{PART3_SYSTEM}\n\n"
        f"=== VISUAL THEME ===\n"
        f"Palette: {theme.get('palette','dark_mechanical')}\n"
        f"Stage BG: {theme.get('stage_bg_color','#05080f')}\n"
        f"Accent 1: {theme.get('accent_color','#66fcf1')}\n"
        f"Accent 2: {theme.get('accent2_color','#fca311')}\n"
        f"Feel: {theme.get('note','')}\n\n"
        f"=== COMPONENTS (draw in this order) ===\n{component_summary}\n\n"
        f"=== SCENE COUNT ===\n{scene_count} scenes total "
        f"(overlay-scene0 … overlay-scene{scene_count-1})\n\n"
        f"=== FULL EXPLANATION SCRIPT ===\n{json.dumps(explanation, indent=2)}\n\n"
        f"=== FULL ANIMATION SEQUENCE ===\n{json.dumps(sequence, indent=2)}"
    )
    raw = _call_gemini(model, prompt)
    # Strip any accidental outer SVG tags or markdown fences
    raw = re.sub(r"```(?:svg|html|xml)?\s*", "", raw).replace("```", "").strip()
    # Remove outer <svg …> … </svg> wrapper if Gemini added one
    raw = re.sub(r"^<svg[^>]*>", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"</svg>\s*$", "", raw, flags=re.IGNORECASE).strip()
    return raw


def generate_js_body(model, explanation: dict, sequence: dict) -> str:
    """Part 3b: Ask Gemini to produce the JavaScript animation engine."""
    components = explanation.get("components", [])
    component_ids = [c["id"] for c in sorted(components, key=lambda x: x.get("draw_order", 99))]
    scene_count = sequence.get("total_scenes", len(sequence.get("scenes", [])))

    prompt = (
        f"{PART3_JS_SYSTEM}\n\n"
        f"=== COMPONENT IDS (in draw order) ===\n{component_ids}\n\n"
        f"=== TOTAL SCENES ===\n{scene_count}\n\n"
        f"=== FULL EXPLANATION SCRIPT ===\n{json.dumps(explanation, indent=2)}\n\n"
        f"=== FULL ANIMATION SEQUENCE ===\n{json.dumps(sequence, indent=2)}"
    )
    raw = _call_gemini(model, prompt)
    # Strip markdown fences
    raw = re.sub(r"```(?:javascript|js)?\s*", "", raw).replace("```", "").strip()
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# 5.  HTML Document Assembler
# ─────────────────────────────────────────────────────────────────────────────

BASE_CSS = """\
:root {
    --bg-color: #0b0c10;
    --panel-bg: #1f2833;
    --text-main: #c5c6c7;
    --accent-cyan: #66fcf1;
    --accent-cyan-dim: #45a29e;
    --accent-orange: #fca311;
    --accent-green: #97c459;
    --border-radius: 12px;
}
* { box-sizing: border-box; margin: 0; padding: 0;
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; }
body {
    background-color: var(--bg-color); color: var(--text-main);
    display: flex; flex-direction: column; align-items: center;
    min-height: 100vh; padding: 20px;
}
.dashboard {
    width: 100%; max-width: 850px; background: var(--panel-bg);
    border-radius: var(--border-radius); box-shadow: 0 15px 35px rgba(0,0,0,0.5);
    overflow: hidden; border: 1px solid #333;
}
/* Question Banner */
.question-banner {
    padding: 18px 24px;
    background: linear-gradient(135deg, #1f2833 0%, #141a21 100%);
    border-bottom: 1px solid #333; display: flex; flex-direction: column; gap: 6px;
}
.q-label {
    font-size: 12px; font-weight: 700; color: var(--accent-cyan);
    text-transform: uppercase; letter-spacing: 1px;
    display: flex; align-items: center; gap: 6px;
}
.q-label::before { content: "❓"; font-size: 14px; }
.q-text { font-size: 15px; color: #fff; line-height: 1.4; font-weight: 400; }
.q-text strong { color: var(--accent-orange); font-weight: 600; }
/* SVG Stage */
.svg-container {
    width: 100%; aspect-ratio: 16/9;
    background: radial-gradient(circle at center, #1a1a24 0%, #050508 100%);
    position: relative; overflow: hidden;
}
svg { display: block; width: 100%; height: 100%; }
.svg-layer { transition: opacity 0.6s cubic-bezier(0.4, 0, 0.2, 1); }
/* Control Panel */
.control-panel {
    padding: 24px;
    background: linear-gradient(180deg, #1f2833 0%, #151b22 100%);
    border-top: 1px solid #333;
}
.step-indicator { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.step-dot {
    width: 10px; height: 10px; border-radius: 50%; background: #444;
    transition: background 0.4s, transform 0.4s;
}
.step-dot.active {
    background: var(--accent-cyan); box-shadow: 0 0 10px var(--accent-cyan);
    transform: scale(1.2);
}
.step-label { font-size: 14px; color: #888; font-weight: 500;
    letter-spacing: 0.5px; text-transform: uppercase; }
.info-box {
    background: #0b0c10; border: 1px solid #333; border-radius: 8px;
    padding: 16px; min-height: 120px; display: flex;
    flex-direction: column; justify-content: center;
}
.info-box h3 { color: #fff; margin-bottom: 12px; font-size: 16px;
    display: flex; align-items: center; gap: 8px; }
.badges { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.badge {
    padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: 600;
    display: flex; align-items: center; gap: 6px;
}
.badge-cyan   { background: rgba(102,252,241,0.1); border: 1px solid var(--accent-cyan-dim); color: var(--accent-cyan); }
.badge-orange { background: rgba(252,163,17,0.1);  border: 1px solid #b3730b; color: var(--accent-orange); }
.badge-green  { background: rgba(151,196,89,0.1);  border: 1px solid #6b933a; color: var(--accent-green); }
.info-desc { font-size: 14px; line-height: 1.5; color: #a0a0a0; }
.actions { display: flex; justify-content: flex-end; gap: 12px; margin-top: 20px; }
button {
    padding: 10px 20px; border-radius: 6px; font-size: 14px; font-weight: 600;
    cursor: pointer; transition: all 0.2s; border: none; outline: none;
}
.btn-primary {
    background: var(--accent-cyan-dim); color: #fff;
    box-shadow: 0 4px 10px rgba(69,162,158,0.3);
}
.btn-primary:hover {
    background: var(--accent-cyan); color: #000;
    box-shadow: 0 6px 15px rgba(102,252,241,0.4);
}
.btn-secondary { background: transparent; color: var(--text-main); border: 1px solid #555; }
.btn-secondary:hover { background: rgba(255,255,255,0.05); color: #fff; }
"""


def _build_dots_html(scenes: list) -> str:
    """Build step-dot indicator HTML from the scenes list."""
    dots_parts = []
    for i, scene in enumerate(scenes):
        label = scene.get("label", f"S{i}")
        active_class = " active" if i == 0 else ""
        dots_parts.append(
            f'                <div class="step-dot{active_class}" '
            f'title="{label}" data-index="{i}"></div>'
        )
    return "\n".join(dots_parts)


def assemble_html(
    question: str,
    explanation: dict,
    sequence: dict,
    svg_body: str,
    js_body: str,
) -> str:
    """Combine all parts into a single self-contained HTML file."""
    topic = explanation.get("topic", "Engineering Animation")
    scenes = sequence.get("scenes", [])
    if not scenes:
        scenes = [{"label": f"S{i}"} for i in range(4)]
    dots_html = _build_dots_html(scenes)

    # Inject visual theme colours into CSS if available
    theme = explanation.get("visual_theme", {})
    stage_bg  = theme.get("stage_bg_color", "#050508")
    accent1   = theme.get("accent_color",   "#66fcf1")
    accent2   = theme.get("accent2_color",  "#fca311")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{topic} — Interactive Animation</title>
    <style>
{BASE_CSS}
    </style>
</head>
<body>

    <div class="dashboard">
        <!-- Question Banner -->
        <div class="question-banner">
            <div class="q-label">Problem Statement</div>
            <div class="q-text">{question}</div>
        </div>

        <!-- SVG Stage -->
        <div class="svg-container">
            <svg id="stage" viewBox="0 0 850 450" preserveAspectRatio="xMidYMid slice">
{textwrap.indent(svg_body, "                ")}
            </svg>
        </div>

        <!-- Control Panel -->
        <div class="control-panel">
            <div class="step-indicator" id="dots">
{dots_html}
                <div class="step-label" id="step-label">Setting Up...</div>
            </div>

            <div class="info-box">
                <h3 id="info-title">{topic}</h3>
                <div class="badges" id="info-badges"></div>
                <div class="info-desc" id="info-desc">
                    Click "Next Step ▶" to begin the kinematic analysis.
                </div>
            </div>

            <div class="actions">
                <button class="btn-secondary" onclick="resetAnim()">↺ Restart</button>
                <button class="btn-primary" id="btn-next" onclick="nextStep()">Next Step ▶</button>
            </div>
        </div>
    </div>

<script>
{js_body}
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_animation(
    question: str,
    api_key: str | None = None,
    output_path: str | None = None,
    verbose: bool = True,
) -> str:
    """
    Full pipeline: question → premium interactive SVG HTML.

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
    def log(msg):
        if verbose:
            print(f"[q_animation] {msg}")

    # ── Init ──────────────────────────────────────────────────────────────────
    model = _init_gemini(api_key)
    log(f"Gemini model: {GEMINI_MODEL}")

    # ── Part 1: Explanation Script ────────────────────────────────────────────
    log("Part 1 → Generating explanation script …")
    explanation = generate_explanation_script(model, question)
    topic = explanation.get("topic", "animation")
    log(f"  Topic: {topic}")
    log(f"  Solution steps: {len(explanation.get('solution_steps', []))}")

    # ── Part 2: Animation Sequence ────────────────────────────────────────────
    log("Part 2 → Generating animation sequence …")
    sequence = generate_animation_sequence(model, explanation)
    log(f"  Mechanism type: {sequence.get('mechanism_type', '?')}")
    log(f"  Scenes: {len(sequence.get('scenes', []))}")

    # ── Part 3a: SVG Body ─────────────────────────────────────────────────────
    log("Part 3a → Generating SVG scene content …")
    svg_body = generate_svg_body(model, explanation, sequence)
    log(f"  SVG content length: {len(svg_body):,} chars")

    # ── Part 3b: JavaScript Engine ────────────────────────────────────────────
    log("Part 3b → Generating JavaScript animation engine …")
    js_body = generate_js_body(model, explanation, sequence)
    log(f"  JS content length: {len(js_body):,} chars")

    # ── Assemble ──────────────────────────────────────────────────────────────
    log("Assembling final HTML document …")
    html = assemble_html(question, explanation, sequence, svg_body, js_body)

    # ── Write ─────────────────────────────────────────────────────────────────
    if output_path is None:
        safe_name = re.sub(r"[^\w]+", "_", topic.lower())[:40]
        output_path = os.path.join(OUTPUT_DIR, f"{safe_name}_animation.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"✓ Written to: {output_path}  ({len(html):,} bytes)")
    return output_path


# ─────────────────────────────────────────────────────────────────────────────
# 7.  Example Questions  (run directly to test)
# ─────────────────────────────────────────────────────────────────────────────

EXAMPLE_QUESTIONS = {

    # ── Slider-Crank ──────────────────────────────────────────────────────────
    "slider_crank": (
        "In a slider-crank mechanism, the crank length is 50 mm and the "
        "connecting rod is 200 mm. The crank rotates at 300 RPM. "
        "Determine the linear velocity of the piston when the crank angle "
        "is 60° from inner dead centre."
    ),

    # ── Spur Gear Train ───────────────────────────────────────────────────────
    "spur_gear": (
        "A spur gear train consists of a driving pinion with 20 teeth "
        "rotating at 900 RPM (clockwise) meshing with a driven gear of "
        "60 teeth. Calculate the speed and direction of rotation of the "
        "driven gear."
    ),

    # ── Four-Bar Linkage ──────────────────────────────────────────────────────
    "four_bar": (
        "In a four-bar linkage, the fixed link (frame) is 120 mm, the "
        "crank is 40 mm rotating at 200 RPM, the coupler is 160 mm, and "
        "the follower (rocker) is 80 mm. Determine the angular velocity "
        "of the rocker when the crank is at 45° from the fixed link."
    ),

    # ── Belt & Pulley ─────────────────────────────────────────────────────────
    "belt_pulley": (
        "A belt drive system has a driver pulley of diameter 250 mm "
        "rotating at 720 RPM. It is connected to a driven pulley of "
        "diameter 600 mm. Calculate the speed of the driven pulley and "
        "the velocity of the belt."
    ),

    # ── Cam & Follower ────────────────────────────────────────────────────────
    "cam_follower": (
        "A circular disc cam of base circle radius 30 mm and lift 20 mm "
        "rotates at 150 RPM. The knife-edge follower rises with simple "
        "harmonic motion during 120° of cam rotation. Find the maximum "
        "velocity and maximum acceleration of the follower."
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# 8.  CLI Entry Point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("=" * 64)
    print("  q_animation.py  —  Engineering Animation Generator")
    print("  Powered by Google Gemini 2.5 Pro")
    print("=" * 64)
    print()
    print("Available example questions:")
    for i, (key, q) in enumerate(EXAMPLE_QUESTIONS.items(), 1):
        preview = q[:80].replace("\n", " ")
        print(f"  [{i}] {key}: {preview}…")
    print(f"  [{len(EXAMPLE_QUESTIONS)+1}] Enter a custom question")
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
    print(f"\n✓ Open in browser:  {out}")
