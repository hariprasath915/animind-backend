"""
╔══════════════════════════════════════════════════════════════════╗
║     claude_client.py  v20.0.0  —  EduAnimator GOLD STANDARD     ║
║     FULLY RE-ENGINEERED ANIMATION GENERATION ARCHITECTURE        ║
║     10-Stage Pipeline × Claude API × Anthropic                   ║
╠══════════════════════════════════════════════════════════════════╣
║  v20.0 PATCH NOTES (on top of v19.3):                            ║
║                                                                  ║
║  ✅ REWORKED: "Working Process" animation prompt simplified and  ║
║              rebuilt to match a set of hand-built reference      ║
║              animations (fibre-optic light path, conduction      ║
║              heat-bar, hybrid solar/wind system):                ║
║              • REMOVED the 10-forced-archetype topic-analysis    ║
║                pipeline (no more mandatory ORBITAL/PIPELINE/      ║
║                CASCADE/etc. selection step)                      ║
║              • REMOVED the rotating "Step 1 of N" CSS-keyframe   ║
║                caption system — no more sequential narrated      ║
║                staging; the whole process animates at once       ║
║              • Output is now ONE simple, realistic, easy-to-     ║
║                understand looping scene per topic                ║
║              • Scene objects must look like the real thing       ║
║                (not abstract boxes/arrows), inspired by but not  ║
║                copied from the reference animations               ║
║              • Smaller, simpler markup: title + single SVG card  ║
║                + short legend row — no forced 6+ gradient/filter ║
║                minimums                                          ║
║              • Zero required JavaScript for the core animation;  ║
║                JS allowed only for an optional simple control    ║
║                                                                  ║
║  v19.3 PATCH NOTES (on top of v19.2):                            ║
║                                                                  ║
║  ✅ REWORKED: "Working Process" animation now generates a        ║
║              UNIQUE scenario, style, UI/UX layout, motion        ║
║              pattern, colors, labels, and scene objects for      ║
║              every topic. No two animations share the same       ║
║              structure. Claude must:                             ║
║              • Pick one LAYOUT ARCHETYPE from 10 options        ║
║              • Derive a unique COLOR PALETTE from the topic     ║
║              • Choose a MOTION PATTERN (orbital, cascade,       ║
║                wave, radial, pipeline, network, microscopic,    ║
║                cross-section, PCB, dashboard)                   ║
║              • Build scene objects that ARE the topic           ║
║                components — not generic boxes or arrows         ║
║              • Vary viewBox composition per archetype           ║
║              • Never reuse the same structure twice             ║
║                                                                  ║
║  v19.2 PATCH NOTES (on top of v19.1):                            ║
║                                                                  ║
║  ✅ REWORKED: "Working Process" section completely redesigned.    ║
║              Now generates a high-quality SVG infographic-style  ║
║              scene animation — matching the reference animation   ║
║              style (hybrid-wind-solar-animation.html):           ║
║              • Full illustrated scene (sky, ground, environment) ║
║              • Rich linearGradient / radialGradient fills         ║
║              • feDropShadow + feGaussianBlur filter effects       ║
║              • Smooth looping <animate> / <animateTransform>      ║
║              • Animated dashed flow lines (stroke-dashoffset)     ║
║              • Pill-badge component labels with drop shadows      ║
║              • CSS @keyframes step captions (zero JS)             ║
║              • Topic-aware environment mapping                    ║
║              • Nunito + Baloo 2 Google Fonts typography           ║
║              • Legend row below SVG                               ║
║              • NO canvas, NO JavaScript motion                    ║
║                                                                  ║
║  v19.1 PATCH NOTES (on top of v19.0):                            ║
║                                                                  ║
║  ✅ REMOVED:  "How It Works" section entirely (static SVG flow   ║
║               diagram + numbered steps) — and its prompt entry   ║
║  ✅ ADDED:    "Working Process" section (now upgraded to v19.3)   ║
║                                                                  ║
║  v19.0 PATCH NOTES (on top of v18.1):                            ║
║                                                                  ║
║  ✅ REMOVED:  "Why It Matters" section entirely                  ║
║  ✅ CHANGED:  Hook section now generates exactly 2 strong,       ║
║               high-value hook points (was 3-4 bullets)           ║
║  ✅ CHANGED:  Definition section simplified — plain English,     ║
║               4-5 points, last point is a real-world example.    ║
║  ✅ CHANGED:  Animation section: "From Library" → "Video Vault"  ║
║  ✅ ADDED:    Specific sub-topic detection + focused generation   ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import anthropic
import os
import re
import json
import time
import logging
import sys
import base64
import hashlib
import requests
from pathlib import Path
from typing import Optional, Dict, List
from concurrent.futures import ThreadPoolExecutor

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── Clients ─────────────────────────────────────────────────────────────────
_sync_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ─── Video Storage Directory ─────────────────────────────────────────────────
VIDEO_STORAGE_DIR = Path(os.getenv("VIDEO_STORAGE_DIR", "/tmp/videos"))
VIDEO_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# ─── Google Custom Search (Rough Diagram — 2D image fetch) ───────────────────
# Requires a Google Custom Search JSON API key + a Programmable Search Engine
# (CX) with "Image search" turned ON. Set these in your .env:
#   GOOGLE_SEARCH_API_KEY=...
#   GOOGLE_SEARCH_CX=...
GOOGLE_SEARCH_API_KEY   = os.getenv("GOOGLE_SEARCH_API_KEY", "")
GOOGLE_SEARCH_CX        = os.getenv("GOOGLE_SEARCH_CX", "")
GOOGLE_IMAGE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"

# ════════════════════════════════════════════════════════════════════════
#  MODEL CONSTANTS
# ════════════════════════════════════════════════════════════════════════

MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU  = "claude-haiku-4-5-20251001"

# ════════════════════════════════════════════════════════════════════════
#  SECTION REGISTRY
# ════════════════════════════════════════════════════════════════════════

BASE_SECTIONS: List[str] = [
    "hook",
    "definition",
    "working_process",
    "core_concepts",
    "types",
    "applications",
    "quiz",
    "animation",
    "rough_diagram",
]

CONDITIONAL_SECTIONS: List[str] = ["formulas", "derivation"]

ORDERED_SECTION_TEMPLATE: List[str] = [
    "hook",
    "definition",
    "working_process",
    "core_concepts",
    "formulas",
    "derivation",
    "types",
    "applications",
    "quiz",
    "animation",
    "rough_diagram",
]

SECTION_MODEL_MAP: Dict[str, str] = {
    "hook":             MODEL_SONNET,
    "definition":       MODEL_SONNET,
    "working_process":  MODEL_SONNET,
    "core_concepts":    MODEL_HAIKU,
    "formulas":         MODEL_SONNET,
    "derivation":       MODEL_SONNET,
    "types":            MODEL_SONNET,
    "applications":     MODEL_HAIKU,
    "quiz":             MODEL_HAIKU,
    "animation":        MODEL_HAIKU,
    # rough_diagram doesn't call the section-writer prompt (it fetches real
    # images), but Haiku is used briefly to turn the topic into a good
    # Google Images search query — see generate_rough_diagram_section().
    "rough_diagram":    MODEL_HAIKU,
}


# ════════════════════════════════════════════════════════════════════════
#  ▶  SPECIFIC TOPIC DETECTION
# ════════════════════════════════════════════════════════════════════════

_SPECIFIC_TOPIC_KEYWORDS = (
    " in ", " of ", " for ", " during ", " within ",
    " via ", " through ", " using ", " under ",
)

def _is_specific_subtopic(topic: str) -> bool:
    lower = topic.lower()
    return any(kw in lower for kw in _SPECIFIC_TOPIC_KEYWORDS)


def _build_specific_focus_note(topic: str) -> str:
    if not _is_specific_subtopic(topic):
        return ""
    return (
        f'\n\n⚠️ SPECIFIC SUB-TOPIC FOCUS — MANDATORY:\n'
        f'The user has requested the EXACT sub-topic: "{topic}".\n'
        f'Every sentence, example, formula, diagram, and simulation in this section '
        f'MUST focus exclusively on "{topic}".\n'
        f'Do NOT drift into the broader parent subject. '
        f'Stay laser-focused on "{topic}" at all times.\n'
    )


# ════════════════════════════════════════════════════════════════════════
#  SUBTOPIC PARSER
# ════════════════════════════════════════════════════════════════════════

def _extract_subtopics_from_input(user_input: str) -> List[str]:
    subtopics: List[str] = []

    if " -- " in user_input:
        _, rest = user_input.split(" -- ", 1)
        subtopics = [s.strip() for s in rest.split(",") if s.strip()]
    elif user_input.count(" - ") > 1:
        parts = user_input.split(" - ")
        if len(parts) > 1:
            subtopics = [s.strip() for s in parts[1:] if s.strip()]
    elif " - " in user_input:
        parts = user_input.split(" - ", 1)
        if len(parts) == 2:
            rest = parts[1].strip()
            subtopics = [s.strip() for s in rest.split(",") if s.strip()]

    seen: set = set()
    unique: List[str] = []
    for s in subtopics:
        if s.lower() not in seen:
            seen.add(s.lower())
            unique.append(s)

    log.info(f"[_extract_subtopics] found {len(unique)} subtopics: {unique}")
    return unique


# ════════════════════════════════════════════════════════════════════════
#  MASTER SYSTEM PROMPT
# ════════════════════════════════════════════════════════════════════════

ULTIMATE_LEARNING_SYSTEM_PROMPT = """You are a PRINCIPAL LEARNING ARCHITECT combining the expertise of:
- Cognitive Learning Scientist (how the brain absorbs and retains knowledge)
- Instructional Design Engineer (how to structure content for maximum clarity)
- Visual Simulation Engineer (how to build interactive educational canvas simulations)
- Mathematics Educator (how to explain formulas with clarity and context)
- Motion Graphics Engineer (how to create educational animation systems)
- SVG Scene Illustrator (how to build rich, professional infographic SVG animations)

MASTER OBJECTIVE:
Transform any topic into a complete, student-ready learning experience that is:
- Understandable by a 15-year-old beginner with zero prior knowledge
- Clear and simple — the Definition section uses plain, everyday English
- Deeply engaging — critical thinking built in at every step
- Retention-optimized — structured for comprehension, not just reading
- Formula-complete — mathematical relationships properly explained (when applicable)
- Visually stunning — the Working Process animation must look like a professional
  motion-graphics explainer video

OUTPUT FORMAT RULES:
1. Return ONLY valid HTML content (no markdown, no code fences)
2. Structure using proper semantic HTML
3. Maximum paragraph: 3-4 lines
4. Use proper LaTeX formatting: $$...$$ for display, $...$ for inline

HARD CONSTRAINTS — NEVER VIOLATE:
1. Never write a paragraph longer than 4 lines
2. All formulas must use proper LaTeX syntax
3. Must work for a 15-year-old with zero prior knowledge
4. NEVER append verification tables or post-analysis commentary after any section
5. Each section response MUST end exactly at its closing HTML tag
6. ALL SVG text must use font-family="Nunito, sans-serif" — no exceptions
7. JavaScript in inline scripts: use var (not const/let) for maximum browser compat
8. Never use external fetch calls or XHR in inline scripts"""


# ════════════════════════════════════════════════════════════════════════
#  SECTION PROMPT BUILDER
# ════════════════════════════════════════════════════════════════════════

def _build_ultimate_section_prompt(
    section_name: str,
    topic: str,
    context: str = "",
    subtopics_list: Optional[List[str]] = None,
    topic_classification: Optional[Dict] = None,
) -> str:

    if section_name == "core_concepts":
        return _build_hybrid_core_concepts_prompt(topic, context, subtopics_list)

    specific_note = _build_specific_focus_note(topic)

    prompts = {

        # ══════════════════════════════════════════════════════════════════
        # §1 HOOK
        # ══════════════════════════════════════════════════════════════════
        "hook": f"""Generate Section 1: HOOK for topic: "{topic}"
{specific_note}
DEPTH REQUIREMENT — MANDATORY:
- Write 1 bold opening fact sentence (under 30 words, real startling fact about "{topic}").
- Language: age-appropriate for a 15-year-old, active voice, no jargon.

NO ANIMATION. NO SVG. NO CANVAS. NO SCRIPT TAGS.
This section is pure, well-written text that hooks the student's curiosity.

Context: {context[:400]}

Return ONLY this HTML structure. Replace ALL placeholders with REAL content:

<div class="hook-card" data-section="hook">
  <div class="hook-icon">🎯</div>
  <div class="hook-text">
    <p class="hook-lead">[Bold opening fact — under 30 words, real startling fact about "{topic}"]</p>
  </div>
  <button class="img-upload-btn" onclick="uploadSectionImage('hook')">📸 Add Image</button>
  <div class="section-images" id="images-hook"></div>
</div>

CRITICAL: One bold opening fact only — no bullet lists, no additional points. Replace ALL [placeholder] text with real, accurate content about "{topic}".
OUTPUT NOTHING after the closing </div> tag.""",

        # ══════════════════════════════════════════════════════════════════
        # §2 DEFINITION
        # ══════════════════════════════════════════════════════════════════
        "definition": f"""Generate Section 2: SIMPLE DEFINITION for topic: "{topic}"
{specific_note}
GOAL: Explain "{topic}" so clearly that a complete beginner understands it
in under a minute. Use very simple, everyday English. No jargon. No fancy
words. Short sentences.

NO ANIMATION. NO SVG. NO CANVAS. NO SCRIPT TAGS. NO interactive simulation
of any kind. This section is pure, simple, well-written text.

CONTENT STRUCTURE — MANDATORY:
1. A 1-2 sentence plain-English definition (no jargon, simple words a 12-year-old knows).
2. Write exactly 4-5 short points total:
   - Points 1 to 3 (or 4): simple, clear facts. One short sentence each.
   - The LAST point MUST be a real-world example.

Context: {context[:400]}

Return ONLY this HTML structure. Replace ALL placeholders with REAL content:

<div class="definition-box">
  <div class="definition-label">📖 What Is It?</div>
  <div class="definition-text">
    <p>[1-2 sentence plain-English definition. Very simple words.]</p>
    <ul class="def-properties">
      <li>[Simple point 1 about "{topic}" — one short, clear sentence]</li>
      <li>[Simple point 2 about "{topic}" — one short, clear sentence]</li>
      <li>[Simple point 3 about "{topic}" — one short, clear sentence]</li>
      <li class="def-example"><strong>Real-life example:</strong> [A specific, real-world situation or application where "{topic}" shows up — 1-2 simple sentences]</li>
    </ul>
  </div>
</div>

CRITICAL:
1. Use very simple English throughout — short words, short sentences.
2. 4-5 bullet points total, last one is the real-world example.
3. Replace ALL [placeholder] text with real, accurate, simple content about "{topic}".
4. OUTPUT NOTHING after the closing </div> tag.""",

        # ══════════════════════════════════════════════════════════════════
        # §3 WORKING PROCESS — v20.0: Reference-Style Realistic Animation
        # ══════════════════════════════════════════════════════════════════
        "working_process": f"""You are a SENIOR SVG/CSS motion-graphics illustrator who builds clean,
realistic, single-scene educational animations. Generate Section: WORKING PROCESS
for topic: "{topic}"
{specific_note}

═══════════════════════════════════════════════════════════════════
PRIME DIRECTIVE
═══════════════════════════════════════════════════════════════════

Build ONE self-contained animated scene that shows how "{topic}" actually
works, the way it would really look if you could see it happening. The
reference style for this section is a small set of hand-built reference
animations (fibre-optic light path, a metal bar conducting heat, a hybrid
solar/wind power system). Match THEIR quality bar and THEIR approach, not
a generic textbook diagram:

- The objects in the scene are realistic miniatures of the real thing
  (an actual-looking fibre cross-section, an actual-looking turbine,
  an actual-looking metal bar with a temperature gradient) — never
  abstract boxes, circles-with-letters, or "Box A → Box B → Box C".
- The whole scene is visible and animating AT ONCE. There is no
  narrated "Step 1 of N" sequence and no rotating caption box that
  hides and reveals different stages. A learner should be able to
  watch the looping animation for 5 seconds and see the entire
  process happening simultaneously, beginning to end, the way a real
  fibre-optic cable or a real turbine doesn't pause between "phases".
- Simple over busy. Use only as many moving parts, gradients, and
  colors as are needed to make the process unmistakably clear. If a
  plain clean look already explains it, do not add extra decoration.
- The text on the scene (labels, the small metric/legend row beneath
  it) should make the diagram understandable on its own, without
  requiring the student to read a paragraph beforehand.

═══════════════════════════════════════════════════════════════════
WHAT TO DRAW — derive this from "{topic}" itself
═══════════════════════════════════════════════════════════════════

Think briefly (do not output this thinking) about:
- What 2-5 real physical or conceptual parts make up "{topic}"? Draw
  those exact parts, shaped the way they really look (a coiled spring
  looks like a coil, a lens looks like a lens, a circuit trace looks
  like a copper trace).
- What is actually moving or changing? Light, current, heat, fluid,
  a mechanical part, data, a chemical, a wave. Animate THAT thing,
  continuously and simultaneously across the whole scene, not as a
  sequence of separate stages.
- What 3-6 realistic colors does this topic suggest in real life? Use
  a small, coherent palette instead of generic rainbow colors. Reuse
  one accent palette consistently (a warm color for input/hot/start,
  a cool color for output/cold/end, one neutral background).

═══════════════════════════════════════════════════════════════════
TECHNICAL APPROACH — SVG + CSS only, no narrative JavaScript
═══════════════════════════════════════════════════════════════════

- Use one clean SVG scene as the centerpiece. viewBox sized to fit the
  content naturally (a wide shallow viewBox like "0 0 700 220" for a
  linear process, roughly square like "0 0 600 420" for a system with
  several components — pick whatever fits the real shape of the topic,
  do not force every topic into the same canvas).
- All motion comes from native SVG <animate> / <animateTransform> and
  CSS @keyframes, running on infinite loops. No JavaScript is required
  to make the animation play. JavaScript may ONLY be used for an
  optional, genuinely useful interactive control (for example a single
  slider or toggle that changes a CSS variable or animation speed) —
  never to drive step-by-step narration or to reveal/hide stages.
- Keep gradients and filters purposeful, not decorative overload: a
  background gradient, maybe one soft drop-shadow filter, and gradient
  fills on the 1-3 components where a real material gradient exists
  (metal heating up, a glowing lamp, a sky). Do not require glow
  filters on every element.
- Small text labels sit directly next to the part they describe,
  using a clean rounded "pill" or simple text label — not large boxes
  covering the scene.
- Beneath the SVG, include a short legend / key-facts row (2-4 small
  items) explaining what each color or moving element represents, in
  the same understated style as a caption strip — not a quiz, not a
  numbered step list.

═══════════════════════════════════════════════════════════════════
GENERATE THE COMPLETE HTML
═══════════════════════════════════════════════════════════════════

Choose one real 4-6 character alphanumeric ID (e.g. "wp4x9") and use it
consistently in every class name, gradient id, and filter id so this
section never collides with another section's styles on the same page.

Return ONLY the HTML below, fully filled in with real content for
"{topic}" — replace every [placeholder], write every gradient stop,
every shape, and every animation in full (no "..." shortcuts):

<div class="working-process-section" data-section="working-process">

<style>
.wp-outer-[ID] {{
  max-width: 720px;
  margin: 0 auto;
  font-family: 'Nunito', Verdana, sans-serif;
}}
.wp-title-[ID] {{
  text-align: center;
  padding: 14px 12px 8px;
}}
.wp-title-[ID] h3 {{
  font-size: clamp(1rem, 2.6vw, 1.3rem);
  font-weight: 800;
  color: #2c3e50;
  margin: 0 0 4px;
}}
.wp-title-[ID] p {{
  font-size: 0.82rem;
  color: #7f8c8d;
  font-weight: 600;
  margin: 0;
}}
.wp-card-[ID] {{
  background: #ffffff;
  border-radius: 14px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.10);
  overflow: hidden;
}}
.wp-card-[ID] svg {{ display: block; width: 100%; height: auto; }}
.wp-legend-[ID] {{
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
  padding: 12px 10px 4px;
}}
.wp-leg-item-[ID] {{
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 0.78rem;
  font-weight: 700;
  color: #34495e;
  background: #f7f8fa;
  padding: 5px 11px;
  border-radius: 16px;
}}
.wp-leg-swatch-[ID] {{
  width: 20px;
  height: 5px;
  border-radius: 3px;
  flex-shrink: 0;
}}

/* ── Looping animations — write every keyframe / animation your scene uses ── */
[ALL_KEYFRAMES_AND_ANIMATION_CLASSES_HERE]

</style>

<div class="wp-outer-[ID]">

  <div class="wp-title-[ID]">
    <h3>[Short, real title naming the process in "{topic}"]</h3>
    <p>[One-sentence plain-English caption of what the animation shows]</p>
  </div>

  <div class="wp-card-[ID]">
    <svg viewBox="0 0 [VBW] [VBH]" xmlns="http://www.w3.org/2000/svg" role="img">
      <title>[Descriptive title of the animated process]</title>
      <desc>[One sentence describing what is moving and why]</desc>

      <defs>
        <!-- Only the gradients/filters you actually use — keep this short -->
        [REAL_GRADIENTS_AND_AT_MOST_ONE_OR_TWO_FILTERS]
      </defs>

      <!-- Background / environment, only if the topic needs one -->
      [OPTIONAL_BACKGROUND_ELEMENTS]

      <!-- ═══ Real component 1 — drawn as it actually looks ═══ -->
      [COMPONENT_1_SHAPE_WITH_LOOPING_ANIMATION]
      <!-- small label for component 1 -->
      [LABEL_1]

      <!-- ═══ What is moving (light / current / heat / fluid / data) ═══ -->
      [MOVING_ELEMENT_WITH_CONTINUOUS_LOOPING_ANIMATION]

      <!-- ═══ Real component 2 ═══ -->
      [COMPONENT_2_SHAPE_WITH_LOOPING_ANIMATION]
      [LABEL_2]

      <!-- ═══ Additional real components (2-4 total is usually enough) ═══ -->
      [ADDITIONAL_COMPONENTS_AS_NEEDED]

    </svg>
  </div>

  <!-- LEGEND — 2-4 short, real items -->
  <div class="wp-legend-[ID]">
    <div class="wp-leg-item-[ID]">
      <div class="wp-leg-swatch-[ID]" style="background:[COLOR_1];"></div>
      [What this color/element represents, in plain words]
    </div>
    <div class="wp-leg-item-[ID]">
      <div class="wp-leg-swatch-[ID]" style="background:[COLOR_2];"></div>
      [What this color/element represents, in plain words]
    </div>
    [1-2 MORE LEGEND ITEMS ONLY IF GENUINELY NEEDED]
  </div>

</div>
</div>

═══════════════════════════════════════════════════════════════════
MANDATORY RULES
═══════════════════════════════════════════════════════════════════

1. [ID] REPLACEMENT: pick one real ID and use it consistently across
   every class name, gradient id, and filter id.
2. NO STEP-BY-STEP STAGING: do not generate a "Step 1 of N" caption
   rotation, do not generate tabs/buttons that switch between hidden
   stages as the primary explanation, and do not use opacity-timed
   captions to narrate a sequence. The whole process must be visible
   and animating at the same time.
3. REALISTIC, NOT ABSTRACT: every shape must visually resemble the
   real-world thing it represents. No plain unlabeled rectangles
   standing in for real objects.
4. SIMPLE AND CLEAR: use the minimum number of components, colors,
   and effects needed to make "{topic}" understandable at a glance.
   Prefer one clean accent palette over many competing colors.
5. ZERO REQUIRED JAVASCRIPT: the animation must play on its own via
   SVG <animate>/<animateTransform> and CSS @keyframes. JavaScript is
   only allowed for an optional simple control, never for narration.
6. COMPLETE CODE: write every gradient, shape, and animation in full.
   No "..." shortcuts. Output must be copy-paste ready.
7. OUTPUT NOTHING after the final closing </div> tag.""",

        # ══════════════════════════════════════════════════════════════════
        # §5 FORMULAS
        # ══════════════════════════════════════════════════════════════════
        "formulas": f"""Generate Section 5: FORMULAS & EQUATIONS for topic: "{topic}"
{specific_note}
Requirements:
- Identify 2-5 key formulas central to "{topic}"
- Each formula must include ONLY:
  * Proper LaTeX using $$...$$ delimiters
  * Clear title/name
  * Symbol breakdown table (variable, meaning, units)
- Do NOT include a "When to use" section
- Do NOT include a worked example section
- Cards must be stacked VERTICALLY (single column), one below the other

Context: {context[:800]}

Return ONLY this HTML structure with REAL formulas:

<div class="formulas-section">
  <div class="formulas-header">
    <div class="formulas-badge">📐 Mathematical Formulas</div>
    <div class="formulas-title">Key Equations for {topic}</div>
    <div class="formulas-subtitle">Understanding the math behind the concept</div>
  </div>

  <div class="formula-cards-stack">

  <div class="formula-card" data-section="formula-1">
    <div class="formula-name">[Formula 1 Name]</div>
    <div class="formula-equation">$$[LaTeX formula]$$</div>
    <div class="formula-symbols">
      <div class="formula-symbols-title">📋 Symbol Breakdown:</div>
      <table class="symbols-table">
        <tr><td class="symbol-var">$$[var]$$</td><td class="symbol-desc">[Description with units]</td></tr>
        <tr><td class="symbol-var">$$[var]$$</td><td class="symbol-desc">[Description with units]</td></tr>
      </table>
    </div>
    <button class="img-upload-btn" onclick="uploadSectionImage('formula-1')">📸 Add Image</button>
    <div class="section-images" id="images-formula-1"></div>
  </div>

  <div class="formula-card" data-section="formula-2">
    <div class="formula-name">[Formula 2 Name]</div>
    <div class="formula-equation">$$[LaTeX formula]$$</div>
    <div class="formula-symbols">
      <div class="formula-symbols-title">📋 Symbol Breakdown:</div>
      <table class="symbols-table">
        <tr><td class="symbol-var">$$[var]$$</td><td class="symbol-desc">[Description with units]</td></tr>
        <tr><td class="symbol-var">$$[var]$$</td><td class="symbol-desc">[Description with units]</td></tr>
      </table>
    </div>
    <button class="img-upload-btn" onclick="uploadSectionImage('formula-2')">📸 Add Image</button>
    <div class="section-images" id="images-formula-2"></div>
  </div>

  </div>

  <div class="formulas-practice">
    ✏️ Practice Challenge: Can you rearrange each formula to solve for a different variable?
  </div>
</div>

CRITICAL:
- Replace ALL [placeholder] text with real, accurate formulas for "{topic}".
- Do NOT add formula-when or formula-example divs — these are removed.
- Use class "formula-cards-stack" (not "formula-cards-grid") on the wrapper.
- OUTPUT NOTHING after the closing </div> tag.""",

        # ══════════════════════════════════════════════════════════════════
        # §6 DERIVATION
        # ══════════════════════════════════════════════════════════════════
        "derivation": f"""Generate Section 6: STEP-BY-STEP DERIVATION for topic: "{topic}"
{specific_note}
This section walks students through the mathematical derivation of the key equation
for "{topic}" from first principles.

Requirements:
- 4-8 numbered derivation steps
- Each step: one equation in LaTeX ($$...$$) + 1-2 sentence explanation
- Steps must build logically: start from a fundamental law, end at the key result
- Beginner-friendly explanations

Context: {context[:600]}

Return ONLY this HTML. Replace ALL placeholders with REAL derivation steps for "{topic}":

<div class="derivation-section" id="derivSection-[6CHAR_ID]">
  <div class="deriv-header">
    <div class="deriv-badge">📊 Mathematical Derivation</div>
    <div class="deriv-title">Deriving the Key Equation for {topic}</div>
    <div class="deriv-subtitle">Step-by-step from first principles — follow every move</div>
  </div>

  <div class="deriv-intro">
    <p>[2-3 sentences: what equation we are about to derive for "{topic}", why it matters, and what fundamental principle we start from]</p>
  </div>

  <div class="deriv-steps" id="derivSteps-[ID]">

    <div class="deriv-step" id="dstep-1-[ID]">
      <div class="deriv-step-header">
        <span class="deriv-step-num">Step 1</span>
        <span class="deriv-step-title">[Step title]</span>
      </div>
      <div class="deriv-step-eq">$$[LaTeX equation for step 1]$$</div>
      <div class="deriv-step-explain">[1-2 sentence explanation]</div>
    </div>

    <div class="deriv-step" id="dstep-2-[ID]">
      <div class="deriv-step-header">
        <span class="deriv-step-num">Step 2</span>
        <span class="deriv-step-title">[Step title]</span>
      </div>
      <div class="deriv-step-eq">$$[LaTeX equation for step 2]$$</div>
      <div class="deriv-step-explain">[Explanation]</div>
    </div>

    [Continue steps 3 through N in same format]

    <div class="deriv-final-box" id="dstep-final-[ID]">
      <div class="deriv-final-label">🎯 Final Result</div>
      <div class="deriv-final-eq">$$[The final derived equation for "{topic}"]$$</div>
      <div class="deriv-final-explain">[2-3 sentences on meaning]</div>
    </div>

  </div>

  <div class="deriv-meaning">
    <div class="deriv-meaning-title">📐 What Does This Tell Us?</div>
    <p>[2-3 sentences on the physical significance of the derived result for "{topic}"]</p>
  </div>

  <script>
  (function() {{
    var ID = '[ID]';
    function initDerivAnim() {{
      if (!window.anime) {{ setTimeout(initDerivAnim, 200); return; }}
      var stepEls = document.querySelectorAll('#derivSteps-' + ID + ' .deriv-step, #dstep-final-' + ID);
      var targets = Array.prototype.slice.call(stepEls);
      targets.forEach(function(el) {{
        el.style.opacity = '0';
        el.style.transform = 'translateY(24px)';
      }});
      window.anime({{
        targets: targets,
        opacity: [0, 1],
        translateY: [24, 0],
        easing: 'easeOutExpo',
        duration: 700,
        delay: window.anime.stagger(280, {{start: 200}})
      }});
      if (window.MathJax && window.MathJax.typesetPromise) {{
        setTimeout(function() {{ window.MathJax.typesetPromise(); }}, 300);
      }}
    }}
    if (document.readyState === 'loading') {{
      document.addEventListener('DOMContentLoaded', initDerivAnim);
    }} else {{
      setTimeout(initDerivAnim, 150);
    }}
  }})();
  </script>
</div>

CRITICAL:
1. Replace [ID] with ONE real random 6-char alphanumeric string
2. Show a REAL derivation for "{topic}" — actual mathematical steps
3. Every equation in proper LaTeX $$...$$
4. OUTPUT NOTHING after the closing </div> tag""",

        # ══════════════════════════════════════════════════════════════════
        # §7 TYPES
        # ══════════════════════════════════════════════════════════════════
        "types": f"""Generate Section: TYPES & CLASSIFICATION for topic: "{topic}"
{specific_note}
Requirements:
- 3-6 main types/categories with subtypes
- Each type: emoji + name + one-line description (max 12 words)
- Comparison table

Context: {context[:800]}

Return ONLY this HTML with REAL content:

<div class="types-section">
  <div class="types-header">
    <div class="types-badge">🌿 Classification</div>
    <div class="types-main-title">Types of {topic}</div>
    <div class="types-subtitle">A complete visual hierarchy — every category explained</div>
  </div>

  <div class="types-flowchart-wrap">
    <div class="fc-root-wrap">
      <div class="fc-root-node">{topic}</div>
    </div>
    <div class="fc-v-line"></div>
    <div class="fc-h-rail"></div>
    <div class="fc-branches-row">
      <div class="fc-branch-col">
        <div class="fc-down-line"></div>
        <div class="fc-type-card" style="--tc:var(--type-color-1)">
          <div class="fc-type-emoji">[emoji]</div>
          <div class="fc-type-name">[Type 1]</div>
          <div class="fc-type-desc">[Description max 12 words]</div>
        </div>
        <div class="fc-subtypes-col">
          <div class="fc-subtype-item">[Subtype 1a]</div>
          <div class="fc-subtype-item">[Subtype 1b]</div>
        </div>
      </div>
      [2-5 more branch columns in same format]
    </div>
  </div>

  <div class="types-compare-box">
    <div class="tc-header">⚖️ Quick Comparison</div>
    <div class="tc-table-wrap">
      <table class="tc-table">
        <thead>
          <tr>
            <th>Feature</th>
            <th>[Type 1]</th>
            <th>[Type 2]</th>
            <th>[Type 3]</th>
          </tr>
        </thead>
        <tbody>
          <tr><td>[Feature 1]</td><td>[Val]</td><td>[Val]</td><td>[Val]</td></tr>
          <tr><td>[Feature 2]</td><td>[Val]</td><td>[Val]</td><td>[Val]</td></tr>
          <tr><td>[Feature 3]</td><td>[Val]</td><td>[Val]</td><td>[Val]</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="types-recall">✏️ Active Recall: Without looking above, name all types of {topic}. What makes each one unique?</div>
</div>

CRITICAL: Replace ALL [placeholder] text with real content about "{topic}".
OUTPUT NOTHING after the closing </div> tag.""",

        # ══════════════════════════════════════════════════════════════════
        # §9 APPLICATIONS
        # ══════════════════════════════════════════════════════════════════
        "applications": f"""Generate Section: REAL-WORLD APPLICATIONS for topic: "{topic}"
{specific_note}
Requirements:
- Minimum 3 distinct, diverse examples across different domains
- Each: 40-60 words, relatable to a 15-year-old
- Cover: home, school/university, industry, technology, nature

Context: {context[:800]}

Return ONLY:

<div class="applications-section">
  <div class="app-title">🌍 Real-World Applications</div>
  <div class="app-grid">
    <div class="app-card">
      <div class="app-icon">🏠</div>
      <div class="app-domain">At Home</div>
      <div class="app-text">[40-60 word description of "{topic}" applied at home]</div>
    </div>
    [Repeat for at least 2 more applications in different domains]
  </div>
  <div class="creativity-challenge">🎨 Your turn: Can you think of a 4th application we didn't mention?</div>
</div>

OUTPUT NOTHING after the closing </div> tag.""",

        # ══════════════════════════════════════════════════════════════════
        # §10 QUIZ
        # ══════════════════════════════════════════════════════════════════
        "quiz": f"""Generate Section: INTERACTIVE QUIZ for topic: "{topic}"
{specific_note}
Generate exactly 25 MCQs in 5 sets of 5 with PROGRESSIVE DIFFICULTY:
  Q1: Easy, Q2: Easy, Q3: Easy, Q4: Medium, Q5: Hard

Rules:
- 4 options (A, B, C, D) per question
- Exactly ONE correct option
- Wrong options should be plausible, not obviously wrong
- All questions MUST be specifically about "{topic}" — not the broader parent subject

Context: {context[:800]}

Return ONLY this HTML. Replace ALL [PLACEHOLDER] values with real content.

<div class="quiz-section">
  <div class="quiz-header">
    <div class="quiz-title">❓ Knowledge Quiz: {topic}</div>
    <div class="quiz-subtitle">25 Questions · 5 Sets · Test Your Understanding</div>
    <div class="quiz-score-bar">
      <span class="quiz-score-label">Total Score</span>
      <span class="quiz-score-value" id="totalScore">0 / 25</span>
    </div>
  </div>

  <div class="quiz-tabs" id="quizTabs">
    <button class="quiz-tab active" onclick="showQuizSet(0,this)">Set 1</button>
    <button class="quiz-tab" onclick="showQuizSet(1,this)">Set 2</button>
    <button class="quiz-tab" onclick="showQuizSet(2,this)">Set 3</button>
    <button class="quiz-tab" onclick="showQuizSet(3,this)">Set 4</button>
    <button class="quiz-tab" onclick="showQuizSet(4,this)">Set 5</button>
  </div>

  <div class="quiz-set active" id="quizSet0">
    <div class="set-title">📘 Set 1: [Sub-theme]</div>
    <div class="set-progress">Questions 1–5 · Easy → Hard</div>
    <div class="quiz-question" id="qq0_0" data-correct="[A/B/C/D]">
      <div class="q-number">Q1 <span class="q-difficulty easy">Easy</span></div>
      <div class="q-text">[Question text]</div>
      <div class="q-options">
        <button class="q-opt" onclick="answerQuiz(this,0,0,'A')">A. [Option A]</button>
        <button class="q-opt" onclick="answerQuiz(this,0,0,'B')">B. [Option B]</button>
        <button class="q-opt" onclick="answerQuiz(this,0,0,'C')">C. [Option C]</button>
        <button class="q-opt" onclick="answerQuiz(this,0,0,'D')">D. [Option D]</button>
      </div>
      <div class="q-feedback" id="qf0_0"></div>
    </div>
    [Q2-Q5 in same format with ids qq0_1 through qq0_4 and qf0_1 through qf0_4]
    <div class="set-score-bar">Set 1 Score: <strong id="setScore0">0 / 5</strong></div>
  </div>

  [quizSet1 through quizSet4 in same format — 5 questions each]

  <script>
  (function() {{
    var scores = [0,0,0,0,0];
    var answered = {{}};
    window.showQuizSet = function(idx, btn) {{
      document.querySelectorAll('.quiz-set').forEach(function(s) {{ s.classList.remove('active'); }});
      document.querySelectorAll('.quiz-tab').forEach(function(b) {{ b.classList.remove('active'); }});
      document.getElementById('quizSet'+idx).classList.add('active');
      if (btn) btn.classList.add('active');
    }};
    window.answerQuiz = function(btn, setIdx, qIdx, choice) {{
      var key = setIdx+'_'+qIdx;
      if (answered[key]) return;
      answered[key] = true;
      var qEl = document.getElementById('qq'+setIdx+'_'+qIdx);
      var correct = qEl.getAttribute('data-correct');
      var fb = document.getElementById('qf'+setIdx+'_'+qIdx);
      var opts = qEl.querySelectorAll('.q-opt');
      opts.forEach(function(o) {{ o.disabled = true; }});
      if (choice === correct) {{
        btn.classList.add('q-correct');
        fb.textContent = '✅ Correct!';
        fb.className = 'q-feedback q-fb-correct';
        scores[setIdx]++;
      }} else {{
        btn.classList.add('q-wrong');
        fb.textContent = '❌ Wrong. Correct answer: ' + correct;
        fb.className = 'q-feedback q-fb-wrong';
        opts.forEach(function(o) {{
          if (o.textContent.trim().startsWith(correct+'.')) o.classList.add('q-correct');
        }});
      }}
      document.getElementById('setScore'+setIdx).textContent = scores[setIdx]+' / 5';
      var total = scores.reduce(function(a,b){{return a+b;}},0);
      document.getElementById('totalScore').textContent = total+' / 25';
    }};
  }})();
  </script>
</div>

CRITICAL: Replace every [A/B/C/D] with the actual correct letter.
Generate all 25 REAL questions. No placeholder text remaining.
OUTPUT NOTHING after the closing </div> tag.""",

        # ══════════════════════════════════════════════════════════════════
        # §11 ANIMATION PLAYER — Video Vault
        # ══════════════════════════════════════════════════════════════════
        "animation": f"""Generate Section: ANIMATION PLAYER for topic: "{topic}"

Return ONLY this self-contained HTML. No markdown. No code fences.

<div class="animation-section" id="animSection">
  <div class="anim-section-header">
    <div class="anim-title-badge">🎬 {topic} Animation</div>
    <div class="anim-subtitle">Upload a video or pick from your Video Vault</div>
  </div>

  <div class="anim-source-tabs">
    <button class="anim-tab active" id="animTabUpload" onclick="animSwitchTab('upload')">📂 Upload Video</button>
    <button class="anim-tab" id="animTabVault" onclick="animSwitchTab('vault')">🔐 Video Vault</button>
  </div>

  <!-- ── PANEL 1: Upload ── -->
  <div class="anim-panel" id="animPanelUpload">
    <div class="anim-drop-zone" id="animDropZone"
         onclick="document.getElementById('animFileInput').click()"
         ondragover="event.preventDefault();this.classList.add('anim-drag-over')"
         ondragleave="this.classList.remove('anim-drag-over')"
         ondrop="animHandleDrop(event)">
      <div class="anim-drop-icon">🎥</div>
      <div class="anim-drop-text">Drag &amp; drop an mp4 file here, or click to browse</div>
      <div class="anim-drop-sub">Supports .mp4, .webm, .ogv</div>
    </div>
    <input type="file" id="animFileInput" accept="video/mp4,video/webm,video/ogg"
           style="display:none" onchange="animLoadFile(event)" />
    <div class="anim-file-info" id="animFileInfo" style="display:none">
      <span id="animFileName" class="anim-file-name"></span>
      <button onclick="animClearFile()" class="anim-clear-btn">✕ Remove</button>
    </div>
  </div>

  <!-- ── PANEL 2: Video Vault ── -->
  <div class="anim-panel" id="animPanelVault" style="display:none">
    <div class="vault-header">
      <div class="vault-title-row">
        <span class="vault-icon">🔐</span>
        <span class="vault-title">Video Vault</span>
        <button class="vault-refresh-btn" onclick="vaultRefresh()">↺ Refresh</button>
      </div>
      <input type="text" id="vaultSearch" class="anim-lib-search"
             placeholder="🔍 Search vault videos…"
             oninput="vaultFilter(this.value)" />
    </div>
    <div class="vault-status" id="vaultStatus">
      <div class="vault-loading" id="vaultLoading" style="display:none">
        <div class="vault-spinner"></div>
        <span>Loading vault…</span>
      </div>
      <div class="vault-empty" id="vaultEmpty" style="display:none">
        <div style="font-size:36px;margin-bottom:10px;">📭</div>
        <p>No videos found in your Vault. Upload videos via the Vault backend or drag &amp; drop above.</p>
      </div>
    </div>
    <div class="anim-lib-grid vault-grid" id="vaultGrid"></div>
    <div class="vault-footer">
      <span class="vault-count" id="vaultCount">0 videos</span>
      <span class="vault-info">Videos are sourced from your connected Video Vault backend.</span>
    </div>
  </div>

  <!-- ── Player ── -->
  <div class="anim-player-wrap" id="animPlayerWrap" style="display:none">
    <div class="anim-player-topbar">
      <span class="anim-player-label" id="animPlayerLabel">▶ Now Playing</span>
      <div class="anim-player-actions">
        <button class="anim-ctrl-btn present" id="animPresentBtn" onclick="animPresent()">▶ Present</button>
        <button class="anim-ctrl-btn pause" id="animPauseBtn" onclick="animPause()" style="display:none">⏸ Pause</button>
        <button class="anim-ctrl-btn fullscreen" onclick="animFullscreen()">⛶ Fullscreen</button>
        <button class="anim-ctrl-btn restart" onclick="animRestart()">↺ Restart</button>
      </div>
    </div>
    <div class="anim-video-container" id="animVideoContainer" style="display:none">
      <video id="animVideoEl" class="anim-video" controls playsinline preload="auto"
             style="width:100%;max-height:520px;background:#000;display:block;">
        Your browser does not support the video tag.
      </video>
    </div>
    <iframe id="animIframeEl" class="anim-iframe" style="display:none"
            sandbox="allow-scripts allow-same-origin" title="{topic} Animation"></iframe>
    <div class="anim-save-bar" id="animSaveBar">
      <button class="anim-save-btn" id="animSaveBtn" onclick="animSaveVideo()" style="display:none">💾 Save</button>
      <span class="anim-save-status" id="animSaveStatus"></span>
    </div>
  </div>

  <script>
  (function() {{
    var _mode='upload', _vaultItems=[], _currentType=null, _videoBlob=null, _currentVideoData=null;

    window.animSwitchTab = function(tab) {{
      _mode = tab;
      document.getElementById('animTabUpload').classList.toggle('active', tab==='upload');
      document.getElementById('animTabVault').classList.toggle('active', tab==='vault');
      document.getElementById('animPanelUpload').style.display = tab==='upload' ? '' : 'none';
      document.getElementById('animPanelVault').style.display  = tab==='vault'  ? '' : 'none';
      if (tab === 'vault') vaultRefresh();
    }};

    window.vaultRefresh = function() {{
      var loading = document.getElementById('vaultLoading');
      var empty   = document.getElementById('vaultEmpty');
      var grid    = document.getElementById('vaultGrid');
      if (loading) loading.style.display = 'flex';
      if (empty)   empty.style.display   = 'none';
      if (grid)    grid.innerHTML = '';

      setTimeout(function() {{
        var items = [];
        try {{ items = window.__videoVault || []; }} catch(e) {{}}
        if (!items.length) {{
          try {{ items = window.parent.__videoVault || []; }} catch(e) {{}}
        }}
        _vaultItems = items;
        if (loading) loading.style.display = 'none';
        _renderVault(items);
      }}, 600);
    }};

    function _renderVault(items) {{
      var grid  = document.getElementById('vaultGrid');
      var empty = document.getElementById('vaultEmpty');
      var count = document.getElementById('vaultCount');
      if (!items || !items.length) {{
        if (empty) empty.style.display = 'block';
        if (grid)  grid.innerHTML = '';
        if (count) count.textContent = '0 videos';
        return;
      }}
      if (empty) empty.style.display = 'none';
      if (count) count.textContent = items.length + ' video' + (items.length !== 1 ? 's' : '');
      grid.innerHTML = items.map(function(v, i) {{
        var thumb = v.thumbnail ? 'background-image:url('+_esc(v.thumbnail)+');background-size:cover;background-position:center;' : '';
        var dur   = v.duration  ? '<span class="vault-card-dur">'+_esc(v.duration)+'</span>' : '';
        return '<div class="anim-lib-card vault-card" onclick="vaultSelectItem('+i+')">'
          + '<div class="vault-card-thumb" style="'+thumb+'">'+dur+'<div class="vault-card-play">▶</div></div>'
          + '<div class="vault-card-meta">'
          + '<div class="anim-lib-card-title">'+_esc(v.title||'Untitled Video')+'</div>'
          + '<div class="anim-lib-card-date">'+(v.date?new Date(v.date).toLocaleDateString():'')+'</div>'
          + '</div>'
          + '</div>';
      }}).join('');
    }}

    window.vaultFilter = function(q) {{
      var filtered = q
        ? _vaultItems.filter(function(v) {{ return (v.title||'').toLowerCase().includes(q.toLowerCase()); }})
        : _vaultItems;
      _renderVault(filtered);
    }};

    window.vaultSelectItem = function(idx) {{
      var item = _vaultItems[idx];
      if (!item) return;
      if (item.src || item.url) {{
        _currentType = 'video';
        var vid = document.getElementById('animVideoEl');
        vid.pause(); vid.removeAttribute('src'); vid.load();
        vid.src = item.src || item.url; vid.load();
        document.getElementById('animIframeEl').style.display = 'none';
        document.getElementById('animVideoContainer').style.display = 'block';
        document.getElementById('animPlayerLabel').textContent = '▶ ' + (item.title||'Vault Video');
        document.getElementById('animPlayerWrap').style.display = 'block';
        document.getElementById('animSaveBtn').style.display = 'none';
        document.getElementById('animPlayerWrap').scrollIntoView({{behavior:'smooth',block:'center'}});
      }} else if (item.animation_code || item.html) {{
        _currentType = 'iframe';
        document.getElementById('animVideoContainer').style.display = 'none';
        var iframe = document.getElementById('animIframeEl');
        iframe.srcdoc = item.animation_code || item.html || '';
        iframe.style.display = 'block';
        document.getElementById('animPlayerLabel').textContent = '▶ ' + (item.title||'Vault Animation');
        document.getElementById('animPlayerWrap').style.display = 'block';
        document.getElementById('animSaveBtn').style.display = 'none';
        document.getElementById('animPlayerWrap').scrollIntoView({{behavior:'smooth',block:'center'}});
      }}
    }};

    window.animLoadFile = function(e) {{ var f = e.target.files&&e.target.files[0]; if(f) _setFile(f); }};
    window.animHandleDrop = function(e) {{
      e.preventDefault();
      document.getElementById('animDropZone').classList.remove('anim-drag-over');
      var f = e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0]; if(f) _setFile(f);
    }};

    function _setFile(file) {{
      if (_videoBlob) {{ URL.revokeObjectURL(_videoBlob); _videoBlob=null; }}
      _videoBlob = URL.createObjectURL(file); _currentType='video';
      var reader = new FileReader();
      reader.onload = function(e) {{
        _currentVideoData = {{name:file.name,type:file.type,data:e.target.result,topic:'{topic}'}};
        try {{ localStorage.setItem('uploaded_video_{topic}', JSON.stringify(_currentVideoData)); }} catch(err) {{}}
      }};
      reader.readAsDataURL(file);
      var vid = document.getElementById('animVideoEl');
      vid.pause(); vid.removeAttribute('src'); vid.load(); vid.src=_videoBlob; vid.load();
      document.getElementById('animIframeEl').style.display='none';
      document.getElementById('animVideoContainer').style.display='block';
      document.getElementById('animFileInfo').style.display='flex';
      document.getElementById('animFileName').textContent=file.name;
      document.getElementById('animDropZone').style.display='none';
      document.getElementById('animPlayerLabel').textContent='▶ '+file.name;
      document.getElementById('animPlayerWrap').style.display='block';
      document.getElementById('animPresentBtn').style.display='inline-flex';
      document.getElementById('animPauseBtn').style.display='none';
      document.getElementById('animSaveBtn').style.display='inline-flex';
      document.getElementById('animSaveStatus').textContent='';
      vid.onplay    = function() {{ document.getElementById('animPresentBtn').style.display='none'; document.getElementById('animPauseBtn').style.display='inline-flex'; }};
      vid.onpause = vid.onended = function() {{ document.getElementById('animPresentBtn').style.display='inline-flex'; document.getElementById('animPauseBtn').style.display='none'; }};
    }}

    window.animClearFile = function() {{
      if (_videoBlob) {{ URL.revokeObjectURL(_videoBlob); _videoBlob=null; }}
      _currentVideoData=null;
      try {{ localStorage.removeItem('uploaded_video_{topic}'); }} catch(e) {{}}
      var vid=document.getElementById('animVideoEl'); vid.pause(); vid.removeAttribute('src'); vid.load();
      document.getElementById('animVideoContainer').style.display='none';
      document.getElementById('animFileInfo').style.display='none';
      document.getElementById('animDropZone').style.display='';
      document.getElementById('animFileInput').value='';
      document.getElementById('animPlayerWrap').style.display='none';
      document.getElementById('animSaveBtn').style.display='none';
      document.getElementById('animSaveStatus').textContent='';
      _currentType=null;
    }};

    window.animSaveVideo = function() {{
      if (!_videoBlob) return;
      var filename=(_currentVideoData&&_currentVideoData.name)?_currentVideoData.name:'{topic}_animation.mp4';
      var a=document.createElement('a'); a.href=_videoBlob; a.download=filename;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      var btn=document.getElementById('animSaveBtn');
      var st=document.getElementById('animSaveStatus');
      if (btn) {{ var t=btn.textContent; btn.textContent='✅ Saved!'; btn.disabled=true; setTimeout(function(){{btn.textContent=t;btn.disabled=false;}},2500); }}
      if (st)  {{ st.textContent='✅ Saved — check your Downloads!'; st.style.color='#22c55e'; setTimeout(function(){{st.textContent='';}},4000); }}
    }};

    window.animPresent = function() {{
      if (_currentType==='video') {{ document.getElementById('animVideoEl').play(); }}
      else if (_currentType==='iframe') {{
        var f=document.getElementById('animIframeEl'); var s=f.srcdoc; f.srcdoc='';
        setTimeout(function(){{f.srcdoc=s;}},80);
        document.getElementById('animPresentBtn').style.display='none';
        document.getElementById('animPauseBtn').style.display='inline-flex';
      }}
    }};
    window.animPause = function() {{
      if (_currentType==='video') {{ document.getElementById('animVideoEl').pause(); }}
      else {{
        try{{document.getElementById('animIframeEl').contentWindow.postMessage('pause','*');}}catch(e){{}}
        document.getElementById('animPresentBtn').style.display='inline-flex';
        document.getElementById('animPauseBtn').style.display='none';
      }}
    }};
    window.animRestart = function() {{
      if (_currentType==='video') {{ var v=document.getElementById('animVideoEl'); v.currentTime=0; v.play(); }}
      else {{ animPresent(); }}
    }};
    window.animFullscreen = function() {{
      if (_currentType==='video') {{
        var v=document.getElementById('animVideoEl');
        if(v.requestFullscreen)v.requestFullscreen();else if(v.webkitRequestFullscreen)v.webkitRequestFullscreen();
      }} else {{
        var f=document.getElementById('animIframeEl'); if(!f.srcdoc)return;
        var w=window.open('','_blank'); w.document.write(f.srcdoc); w.document.close();
      }}
    }};

    function _esc(s){{return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}}

    window.__injectVideoVault = function(items) {{
      window.__videoVault = items;
      _vaultItems = items;
      if (_mode === 'vault') _renderVault(items);
    }};
    window.addEventListener('message', function(e) {{
      if (e.data && e.data.type === 'video_vault' && Array.isArray(e.data.items)) {{
        window.__injectVideoVault(e.data.items);
      }}
    }});

    (function _restore() {{
      try {{
        var saved = localStorage.getItem('uploaded_video_{topic}');
        if (saved) {{
          var d = JSON.parse(saved);
          fetch(d.data).then(function(r){{return r.blob();}}).then(function(b){{_setFile(new File([b],d.name,{{type:d.type}}));}});
        }}
      }} catch(e) {{}}
    }})();
  }})();
  </script>
</div>""",
    }

    return prompts.get(section_name, f"Generate content for {section_name} about {topic}")


# ════════════════════════════════════════════════════════════════════════
#  HYBRID CORE CONCEPTS BUILDER
# ════════════════════════════════════════════════════════════════════════

def _build_hybrid_core_concepts_prompt(
    topic: str,
    context: str = "",
    subtopics_list: Optional[List[str]] = None,
) -> str:

    specific_note = _build_specific_focus_note(topic)

    if subtopics_list and len(subtopics_list) > 0:
        numbered_cards = "\n".join(
            f"  Concept {i+1} — ONLY about \"{s}\"  "
            f"[standalone card — do NOT combine with any other subtopic]"
            for i, s in enumerate(subtopics_list)
        )
        part_b_start = len(subtopics_list) + 1

        user_block = f"""
PART A — USER-SPECIFIED SUBTOPICS:
Each subtopic below is a COMPLETELY SEPARATE concept card.
Generate exactly ONE card per subtopic, in the order listed. NEVER merge two subtopics.

{numbered_cards}

PART B — AUTO-DETECTED EXTRAS (start numbering from Concept {part_b_start}):
After completing ALL {len(subtopics_list)} Part A cards, identify 3-5 additional
foundational concepts for "{topic}" NOT already covered by Part A.
Continue sequential numbering.

Total card count = {len(subtopics_list)} (Part A) + 3 to 5 (Part B).
"""
    else:
        user_block = f"""
AUTO-DETECT: Identify 5-7 key foundational concepts that best explain "{topic}".
Number all cards sequentially: Concept 1, Concept 2, …
"""

    return f"""Generate Section 4: CORE CONCEPTS for topic: "{topic}"
{specific_note}
{user_block}

CARD FORMAT — MANDATORY FOR EVERY CARD:
1. One clear definition sentence (max 20 words)
2. 2-3 sentence explanatory paragraph
3. Bullet list of 3-5 key properties or facts

STRICTLY DO NOT include:
  - Any SVG elements, visual diagrams, or canvas elements
  - Any working-process-section, wp-canvas, or wp-stage-label elements
  - "Think of it like" / analogy boxes
  - "What If" / critical-thinking question boxes
  - "Active Recall" / recall-prompt boxes

Context: {context[:800]}

Return ONLY the HTML content. Generate ALL cards.

<div class="concept-card" data-section="concept-1">
  <div class="concept-number">Concept 1</div>
  <div class="concept-title">[Concept name]</div>
  <div class="concept-definition">[One clear definition sentence — max 20 words]</div>
  <div class="concept-body">
    <p>[2-3 sentence explanatory paragraph]</p>
    <ul class="concept-bullets">
      <li>[Key property or fact 1]</li>
      <li>[Key property or fact 2]</li>
      <li>[Key property or fact 3]</li>
    </ul>
  </div>
  <button class="img-upload-btn" onclick="uploadSectionImage('concept-1')">📸 Add Image</button>
  <div class="section-images" id="images-concept-1"></div>
</div>

[Continue with concept-2, concept-3, … for ALL required cards]

CRITICAL:
- Replace ALL placeholder text with real, accurate content for "{topic}"
- Do NOT include SVG, canvas, or any visual diagram elements
- Part A cards: each covers EXACTLY ONE user subtopic — never combine
- OUTPUT NOTHING after the final closing </div> tag"""


# ════════════════════════════════════════════════════════════════════════
#  STRIP VERIFICATION TAIL
# ════════════════════════════════════════════════════════════════════════

def _strip_verification_tail(html: str) -> str:
    last_close = -1
    for tag in ('</div>', '</section>', '</ul>', '</ol>', '</table>', '</script>'):
        idx = html.rfind(tag)
        if idx != -1:
            candidate = idx + len(tag)
            if candidate > last_close:
                last_close = candidate

    if last_close == -1:
        return html

    tail = html[last_close:]
    verification_markers = [
        '###', 'Verification', 'Requirement', 'Why This Works',
        '| ---', '|---|', '✅', 'Status |', 'provided across',
        'words per card', 'read time', 'relatable', 'markdown',
        '**40-', '**20-', '**Minimum',
    ]
    if tail.strip() and any(marker in tail for marker in verification_markers):
        log.info("[_strip_verification_tail] Stripped verification block")
        return html[:last_close]

    return html


# ════════════════════════════════════════════════════════════════════════
#  ✏️  ROUGH DIAGRAM — GOOGLE IMAGE SEARCH (2D, exam-sketchable diagrams)
# ════════════════════════════════════════════════════════════════════════
#
#  The 3D "Working Process" / "Animation" sections are intentionally rich
#  and immersive — great for understanding, but not something a student
#  can realistically reproduce by hand in an exam. This section fetches a
#  handful of REAL, existing 2D diagrams from Google Images so students
#  have something simple they can actually copy/practice drawing.
#
#  This does NOT touch the animation pipeline in any way — it is a fully
#  separate, additive section that runs alongside it.
# ════════════════════════════════════════════════════════════════════════

def _fetch_google_diagram_images(query: str, num: int = 4) -> List[Dict]:
    """Blocking call to the Google Custom Search JSON API (image search).
    Returns [] on any failure/misconfiguration so the caller can fall back
    gracefully instead of breaking the whole lesson generation."""
    if not GOOGLE_SEARCH_API_KEY or not GOOGLE_SEARCH_CX:
        log.warning(
            "[_fetch_google_diagram_images] GOOGLE_SEARCH_API_KEY / GOOGLE_SEARCH_CX "
            "not configured — skipping live image fetch."
        )
        return []

    params = {
        "key":        GOOGLE_SEARCH_API_KEY,
        "cx":         GOOGLE_SEARCH_CX,
        "q":          query,
        "searchType": "image",
        "num":        min(max(int(num), 1), 10),
        "safe":       "active",
        # bias results toward simple line-art/diagram style rather than photos
        "imgType":    "clipart",
    }

    try:
        resp = requests.get(GOOGLE_IMAGE_SEARCH_URL, params=params, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", []) or []

        results: List[Dict] = []
        for item in items:
            image_meta = item.get("image", {}) or {}
            results.append({
                "url":       item.get("link", ""),
                "thumbnail": image_meta.get("thumbnailLink") or item.get("link", ""),
                "title":     item.get("title", "Diagram"),
                "context":   image_meta.get("contextLink", ""),
            })
        log.info(f"[_fetch_google_diagram_images] '{query}' → {len(results)} images")
        return results

    except Exception as e:
        log.warning(f"[_fetch_google_diagram_images] request failed: {e}")
        return []


# ════════════════════════════════════════════════════════════════════════
#  ULTIMATE LEARNING GENERATOR CLASS
# ════════════════════════════════════════════════════════════════════════

class UltimateLearningGenerator:

    def __init__(self, api_key: Optional[str] = None):
        self._client = (
            anthropic.AsyncAnthropic(api_key=api_key)
            if api_key
            else client
        )

    # ──────────────────────────────────────────────────────────────────
    #  STAGE 0 — TOPIC CLASSIFIER
    # ──────────────────────────────────────────────────────────────────

    async def _classify_topic(self, topic: str) -> Dict:
        is_specific = _is_specific_subtopic(topic)
        specific_note = (
            f'\nNOTE: This is a SPECIFIC SUB-TOPIC request ("{topic}"). '
            f'primary_phenomenon must describe the exact process named, not the parent subject.'
            if is_specific else ""
        )

        prompt = f"""Analyze this educational topic and classify it precisely.

Topic: "{topic}"{specific_note}

Return ONLY a valid JSON object with NO markdown, NO backticks, NO preamble:
{{
  "category": "mathematical" | "semi_mathematical" | "conceptual",
  "needs_formula": true | false,
  "needs_derivation": true | false,
  "reasoning": "one sentence explaining the classification",
  "primary_phenomenon": "the core physical/conceptual process to simulate — be specific to the exact topic",
  "visualization_type": "particle_flow" | "wave" | "network" | "field" | "biological" | "mechanical" | "thermodynamic" | "abstract"
}}

CLASSIFICATION RULES:

"mathematical" (needs_formula=true, needs_derivation=true):
  → Core physics equations, thermodynamics laws, electromagnetism, fluid mechanics,
    wave equations, optics formulas, signal processing, structural mechanics

"semi_mathematical" (needs_formula=true, needs_derivation=false):
  → Topics with useful formulas but no deep first-principles derivation needed

"conceptual" (needs_formula=false, needs_derivation=false):
  → Biological overviews, historical concepts, structural descriptions,
    purely qualitative phenomena, social/organizational topics

VISUALIZATION_TYPE mapping:
  particle_flow   → heat, diffusion, fluid, current, gas molecules
  wave            → sound, light, EM radiation, water waves, seismic, quantum
  network         → neural networks, circuits, social graphs, internet, bonds
  field           → gravity, magnetism, electric field, pressure field
  biological      → cells, DNA, photosynthesis, metabolism, neurons
  mechanical      → gears, pendulums, orbits, levers, optics ray tracing
  thermodynamic   → entropy, gas laws, phase transitions, Carnot, PV diagrams
  abstract        → algorithms, information theory, pure math concepts

Return ONLY the JSON object. No other text."""

        try:
            msg = await self._client.messages.create(
                model=MODEL_HAIKU,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = re.sub(r'```json\s*|\s*```', '', msg.content[0].text.strip())
            result = json.loads(raw)
            log.info(
                f"[_classify_topic] '{topic}' → {result.get('category')} | "
                f"formula={result.get('needs_formula')} | "
                f"deriv={result.get('needs_derivation')} | "
                f"viz={result.get('visualization_type')} | "
                f"specific={is_specific}"
            )
            return result
        except Exception as e:
            log.warning(f"[_classify_topic] failed ({e}), defaulting to semi_mathematical")
            return {
                "category": "semi_mathematical",
                "needs_formula": True,
                "needs_derivation": False,
                "reasoning": "Classification failed; defaulting to semi_mathematical",
                "primary_phenomenon": topic,
                "visualization_type": "particle_flow",
            }

    # ──────────────────────────────────────────────────────────────────
    #  BUILD SECTION LIST
    # ──────────────────────────────────────────────────────────────────

    def _build_section_list(self, classification: Dict) -> List[str]:
        sections: List[str] = []
        for s in ORDERED_SECTION_TEMPLATE:
            if s == "formulas":
                if classification.get("needs_formula"):
                    sections.append(s)
            elif s == "derivation":
                if classification.get("needs_derivation"):
                    sections.append(s)
            else:
                sections.append(s)
        return sections

    # ──────────────────────────────────────────────────────────────────
    #  CONTENT AUDIT
    # ──────────────────────────────────────────────────────────────────

    async def generate_content_audit(self, topic: str, existing_content: str = "") -> Dict:
        specific_note = _build_specific_focus_note(topic)
        prompt = f"""STAGE 1: CONTENT AUDIT

Topic: {topic}{specific_note}
Existing Content: {existing_content[:2000] if existing_content else "None provided"}

Return ONLY valid JSON:
{{
  "core_idea": "The single core idea students must walk away with",
  "existing_sections": [],
  "missing_pieces": [],
  "simplification_needed": [],
  "redundancies": []
}}"""

        try:
            msg = await self._client.messages.create(
                model=MODEL_SONNET,
                max_tokens=2000,
                system=ULTIMATE_LEARNING_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = re.sub(r'```json\s*|\s*```', '', msg.content[0].text.strip())
            return json.loads(raw)
        except Exception as e:
            log.warning(f"Content audit failed: {e}")
            return {
                "core_idea": f"Understanding {topic}",
                "existing_sections": [],
                "missing_pieces": ["All sections need to be generated"],
                "simplification_needed": [],
                "redundancies": [],
            }

    # ──────────────────────────────────────────────────────────────────
    #  GENERATE SINGLE SECTION
    # ──────────────────────────────────────────────────────────────────

    async def generate_section(
        self,
        section_name: str,
        topic: str,
        context: str = "",
        subtopics_list: Optional[List[str]] = None,
        topic_classification: Optional[Dict] = None,
        max_retries: int = 2,
    ) -> str:
        prompt = _build_ultimate_section_prompt(
            section_name,
            topic,
            context,
            subtopics_list=subtopics_list,
            topic_classification=topic_classification,
        )
        model = SECTION_MODEL_MAP.get(section_name, MODEL_SONNET)
        log.info(f"  Generating [{section_name}] with {model.split('-')[1]} ...")

        for attempt in range(1, max_retries + 1):
            try:
                msg = await self._client.messages.create(
                    model=model,
                    max_tokens=16000,
                    system=ULTIMATE_LEARNING_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}]
                )
                content = msg.content[0].text.strip()
                content = re.sub(r'```html\s*|\s*```', '', content).strip()
                content = _strip_verification_tail(content)
                log.info(f"  ✅ [{section_name}] done ({len(content):,} chars)")
                return content
            except Exception as e:
                log.warning(f"  ⚠️ [{section_name}] attempt {attempt}/{max_retries}: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2)

        log.error(f"  ❌ [{section_name}] FAILED after {max_retries} attempts")
        return (
            f'<div class="error-section">'
            f'⚠️ Section <strong>{section_name}</strong> could not be generated. '
            f'<a href="javascript:location.reload()">Retry page</a>.'
            f'</div>'
        )

    # ──────────────────────────────────────────────────────────────────
    #  ✏️  ROUGH DIAGRAM  (2D, exam-sketchable — separate from animation)
    # ──────────────────────────────────────────────────────────────────

    async def _generate_diagram_search_query(self, topic: str) -> str:
        """Turn the raw topic into a short, well-targeted Google Images
        query that favours simple labeled 2D line-diagrams over photos,
        3D renders, or busy infographics."""
        prompt = (
            f'Give ONE short Google Images search query (max 8 words) that will '
            f'find a simple, clearly-labeled 2D line diagram of this topic — the '
            f'kind found in a school textbook. NOT a photo, NOT a 3D render, '
            f'NOT decorative infographic art.\n\n'
            f'Topic: "{topic}"\n\n'
            f'Reply with ONLY the search query text — no quotes, no explanation.'
        )
        try:
            msg = await self._client.messages.create(
                model=MODEL_HAIKU,
                max_tokens=40,
                messages=[{"role": "user", "content": prompt}],
            )
            query = msg.content[0].text.strip().strip('"').strip()
            return query or f"{topic} labeled diagram"
        except Exception as e:
            log.warning(f"[_generate_diagram_search_query] failed ({e}), using raw topic")
            return f"{topic} labeled diagram simple"

    def _build_rough_diagram_html(self, topic: str, query: str, images: List[Dict]) -> str:
        search_link = f"https://www.google.com/search?tbm=isch&q={requests.utils.quote(query)}"

        if not images:
            return f"""
    <div class="rough-diagram-intro">
      <p>✏️ These should be simple <strong>2D diagrams</strong> you can actually
      draw in an exam — quick sketches, not the 3D animation above.</p>
    </div>
    <div class="rough-diagram-fallback">
      <p>⚠️ Couldn't fetch a live diagram right now.</p>
      <p><a href="{search_link}" target="_blank" rel="noopener">
        🔍 Search "{query}" on Google Images ↗</a></p>
    </div>"""

        cards = ""
        for img in images:
            safe_title = (img.get("title") or "Diagram").replace('"', "'")
            img_url    = img.get("url", "")
            thumb_url  = img.get("thumbnail", img_url)
            cards += f"""
      <a class="rough-diagram-card" href="{img_url}" target="_blank" rel="noopener">
        <img src="{thumb_url}" alt="{safe_title}" loading="lazy"
             onerror="this.closest('.rough-diagram-card').style.display='none'">
        <span class="rough-diagram-caption">{safe_title}</span>
      </a>"""

        return f"""
    <div class="rough-diagram-intro">
      <p>✏️ These are simple <strong>2D diagrams</strong> you can actually draw
      in an exam — quick, hand-sketchable versions of the concept above.</p>
    </div>
    <div class="rough-diagram-grid">{cards}
    </div>
    <p class="rough-diagram-more">
      <a href="{search_link}" target="_blank" rel="noopener">
        🔍 See more diagrams for "{topic}" on Google Images ↗</a>
    </p>"""

    async def generate_rough_diagram_section(self, topic: str) -> str:
        """Fetches real 2D diagram images for the topic via Google Image
        Search and renders them as a small gallery. Fully independent of
        the 3D animation pipeline — never modifies it."""
        log.info("  Generating [rough_diagram] ...")
        query = topic
        try:
            query = await self._generate_diagram_search_query(topic)
            images = await asyncio.to_thread(_fetch_google_diagram_images, query, 4)
            html = self._build_rough_diagram_html(topic, query, images)
            log.info(f"  ✅ [rough_diagram] done ({len(images)} images, query='{query}')")
            return html
        except Exception as e:
            log.error(f"  ❌ [rough_diagram] failed: {e}")
            return self._build_rough_diagram_html(topic, query, [])

    # ──────────────────────────────────────────────────────────────────
    #  GENERATE COMPLETE LESSON
    # ──────────────────────────────────────────────────────────────────

    async def generate_complete_lesson(
        self,
        topic: str,
        existing_content: str = "",
        include_audit: bool = True,
        subtopics_list: Optional[List[str]] = None,
    ) -> Dict:
        log.info(f"\n{'═'*64}")
        log.info(f"[ULTIMATE v19.3] Starting pipeline for: {topic}")
        log.info(f"[ULTIMATE v19.3] Specific sub-topic detected: {_is_specific_subtopic(topic)}")
        if subtopics_list:
            log.info(f"[ULTIMATE v19.3] Core Concepts subtopics: {subtopics_list}")
        log.info(f"{'═'*64}")

        log.info("[STAGE 0] Classifying topic...")
        classification = await self._classify_topic(topic)

        audit_result = None
        if include_audit:
            log.info("[STAGE 1] Content audit...")
            audit_result = await self.generate_content_audit(topic, existing_content)
            context = json.dumps(audit_result)
        else:
            context = f"Topic: {topic}"

        lesson_sections = self._build_section_list(classification)
        log.info(f"[STAGE 0] Sections to generate: {lesson_sections}")

        log.info(f"[STAGES 2-{len(lesson_sections)+1}] Generating {len(lesson_sections)} sections in parallel...")

        async def _gen(s: str) -> str:
            if s == "rough_diagram":
                # Not Claude-generated text — fetches real 2D diagram images.
                return await self.generate_rough_diagram_section(topic)
            return await self.generate_section(
                s, topic, context,
                subtopics_list=(subtopics_list if s == "core_concepts" else None),
                topic_classification=classification,
            )

        section_contents = await asyncio.gather(*[_gen(s) for s in lesson_sections])
        sections = dict(zip(lesson_sections, section_contents))

        log.info("[FINAL STAGE] Assembling HTML...")
        html = self._assemble_html(topic, sections, lesson_sections, audit_result, classification)

        total_words = sum(len(c.split()) for c in section_contents)
        metadata = {
            "topic":                  topic,
            "is_specific_subtopic":   _is_specific_subtopic(topic),
            "total_sections":         len(lesson_sections),
            "sections_generated":     lesson_sections,
            "classification":         classification,
            "total_words":            total_words,
            "estimated_read_minutes": round(total_words / 200, 1),
            "generation_timestamp":   time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        log.info(f"[COMPLETE] ✅ {len(html):,} chars | {total_words:,} words | sections: {lesson_sections}")
        return {"audit": audit_result, "sections": sections, "html": html, "metadata": metadata}

    # ──────────────────────────────────────────────────────────────────
    #  ASSEMBLE HTML
    # ──────────────────────────────────────────────────────────────────

    def _assemble_html(
        self,
        topic: str,
        sections: Dict[str, str],
        lesson_sections: List[str],
        audit: Optional[Dict] = None,
        classification: Optional[Dict] = None,
    ) -> str:
        css = self._get_ultimate_learning_css()

        section_labels = {
            "hook":             "🎯 Hook",
            "definition":       "📖 Definition",
            "working_process":  "🛠️ Working Process",
            "core_concepts":    "🧠 Core Concepts",
            "formulas":         "📐 Formulas",
            "derivation":       "📊 Derivation",
            "types":            "🌿 Types",
            "applications":     "🌍 Applications",
            "quiz":             "❓ Quiz",
            "animation":        "🎬 Animation",
            "rough_diagram":    "✏️ Rough Diagram",
        }

        nav_items = [
            f'<div class="acc-item" id="acc-item-{i}">'
            f'<button class="acc-header" onclick="toggleAccSection({i}, this)">'
            f'<span class="acc-icon">{section_labels.get(s, s.replace("_"," ").title()).split(" ")[0]}</span>'
            f'<span class="acc-label">{" ".join(section_labels.get(s, s.replace("_"," ").title()).split(" ")[1:])}</span>'
            f'<span class="acc-arrow">▸</span>'
            f'</button>'
            f'<div class="acc-body" id="acc-body-{i}">'
            f'<button class="acc-goto-btn" onclick="scrollToSectionAcc(\'section-{i}\')">📍 Go to section</button>'
            f'</div>'
            f'</div>'
            for i, s in enumerate(lesson_sections, 1)
        ]

        section_html_parts = [
            f"""
    <section id="section-{i}" class="lesson-section">
      <div class="section-header"><h2>{section_labels.get(s, s.replace("_"," ").title())}</h2></div>
      <div class="section-content">{content}</div>
    </section>"""
            for i, (s, content) in enumerate(sections.items(), 1)
        ]

        audit_html = ""
        if audit:
            audit_html = f"""
    <div class="audit-summary">
      <h3>📋 Content Audit Summary</h3>
      <p><strong>Core Idea:</strong> {audit.get('core_idea', 'N/A')}</p>
    </div>"""

        classification_badge = ""
        if classification:
            cat = classification.get("category", "")
            cat_map = {
                "mathematical":      ("🔢 Mathematical Topic", "#7c3aed"),
                "semi_mathematical": ("📊 Semi-Mathematical Topic", "#0891b2"),
                "conceptual":        ("💡 Conceptual Topic", "#059669"),
            }
            label, color = cat_map.get(cat, ("📚 Topic", "#374151"))
            has_formula = classification.get("needs_formula", False)
            has_deriv   = classification.get("needs_derivation", False)
            badges = ""
            if has_formula:
                badges += '<span class="cls-badge formula">📐 Formulas Generated</span>'
            if has_deriv:
                badges += '<span class="cls-badge deriv">📊 Derivation Generated</span>'
            if not has_formula and not has_deriv:
                badges += '<span class="cls-badge concept">📖 Conceptual Focus</span>'
            if _is_specific_subtopic(topic):
                badges += '<span class="cls-badge specific">🎯 Specific Sub-Topic Mode</span>'
            classification_badge = f"""
    <div class="classification-bar" style="border-color:{color}">
      <span class="cls-label" style="color:{color}">{label}</span>
      {badges}
    </div>"""

        image_upload_script = self._get_image_upload_script()
        vault_bridge_script = self._get_vault_bridge_script()

        animejs_cdn = (
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/animejs/3.2.1/anime.min.js"'
            ' integrity="sha512-z4OUqw38qNLpn1libAN9BsoDx6nbNFio5lA6CunMkdgMBhTJs3zP5vHIl2MlMSRvO4GbHRR0em+XFuOGHAz4g=="'
            ' crossorigin="anonymous" referrerpolicy="no-referrer"></script>'
        )

        mathjax_script = """<script>
  MathJax = {
    tex: { inlineMath: [['$','$'],['\\\\(','\\\\)']], displayMath: [['$$','$$'],['\\\\[','\\\\]']] },
    svg: { fontCache: 'global' }
  };
</script>
<script id="MathJax-script" async
  src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>"""

        quiz_idx = sum(1 for s in lesson_sections if s not in ("quiz", "animation")) + 1
        anim_idx = (
            lesson_sections.index("animation") + 1
            if "animation" in lesson_sections
            else len(lesson_sections)
        )

        footer_cta = f"""
    <div class="footer-cta">
      <button class="footer-cta-btn quiz-cta"
        onclick="scrollToSectionAcc('section-{quiz_idx}')">❓ Jump to Quiz</button>
      <button class="footer-cta-btn anim-cta"
        onclick="scrollToSectionAcc('section-{anim_idx}')">🎬 View Animation</button>
    </div>"""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{topic} — Ultimate Learning Experience v19.3</title>
  {animejs_cdn}
  {mathjax_script}
  <style>
{css}
  </style>
</head>
<body>
  <div id="progressBar"></div>
  <div class="page-container">
    <header class="page-header">
      <div class="header-badge">🎓 Ultimate Learning Experience v19.3</div>
      <h1 class="page-title">{topic}</h1>
      <p class="page-subtitle">A complete learning journey designed for maximum understanding and retention</p>
    </header>
    {classification_badge}
    {audit_html}
    <div class="content-layout">
      <nav class="section-nav" id="sectionNav">
        <div class="acc-panel-title">📚 Sections</div>
        {"".join(nav_items)}
      </nav>
      <main class="main-content">
        {"".join(section_html_parts)}
      </main>
    </div>
    <footer class="page-footer">
      {footer_cta}
      <p>🧠 Built with the Ultimate Learning Content Generator Pipeline v19.3</p>
      <p>Optimized for comprehension, critical thinking, and retention</p>
    </footer>
  </div>
  <script>
    /* ══════════════════════════════════════════════════════
       ACCORDION NAV  — Expand/Collapse section list
    ══════════════════════════════════════════════════════ */
    var _openAccIdx = null;

    function toggleAccSection(idx, btn) {{
      var body = document.getElementById('acc-body-' + idx);
      var item = document.getElementById('acc-item-' + idx);
      var arrow = btn ? btn.querySelector('.acc-arrow') : null;

      if (_openAccIdx === idx) {{
        /* collapse current */
        body.style.maxHeight = '0';
        body.style.opacity   = '0';
        item.classList.remove('acc-open');
        if (arrow) arrow.textContent = '▸';
        _openAccIdx = null;
      }} else {{
        /* close previously open */
        if (_openAccIdx !== null) {{
          var prevBody = document.getElementById('acc-body-' + _openAccIdx);
          var prevItem = document.getElementById('acc-item-' + _openAccIdx);
          var prevArrow = prevItem ? prevItem.querySelector('.acc-arrow') : null;
          if (prevBody) {{ prevBody.style.maxHeight = '0'; prevBody.style.opacity = '0'; }}
          if (prevItem) prevItem.classList.remove('acc-open');
          if (prevArrow) prevArrow.textContent = '▸';
        }}
        /* open new */
        body.style.maxHeight = body.scrollHeight + 'px';
        body.style.opacity   = '1';
        item.classList.add('acc-open');
        if (arrow) arrow.textContent = '▾';
        _openAccIdx = idx;
      }}
    }}

    function scrollToSectionAcc(sectionId) {{
      var target = document.getElementById(sectionId);
      if (!target) return;
      var navH = document.getElementById('sectionNav') ? document.getElementById('sectionNav').offsetHeight : 0;
      var top = target.getBoundingClientRect().top + window.pageYOffset - navH - 16;
      window.scrollTo({{ top: top, behavior: 'smooth' }});
    }}

    /* scroll-spy: highlight acc-header of visible section */
    var _observer = new IntersectionObserver(function(entries) {{
      entries.forEach(function(e) {{
        if (e.isIntersecting) {{
          var sIdx = parseInt(e.target.id.replace('section-', ''), 10);
          document.querySelectorAll('.acc-header').forEach(function(h) {{
            h.classList.remove('acc-active');
          }});
          var activeItem = document.getElementById('acc-item-' + sIdx);
          if (activeItem) {{
            var activeHdr = activeItem.querySelector('.acc-header');
            if (activeHdr) activeHdr.classList.add('acc-active');
          }}
        }}
      }});
    }}, {{ rootMargin: '-80px 0px -60% 0px', threshold: 0 }});
    document.querySelectorAll('.lesson-section').forEach(function(s) {{ _observer.observe(s); }});

    /* progress bar */
    var _bar = document.getElementById('progressBar');
    window.addEventListener('scroll', function() {{
      var s = window.scrollY;
      var m = document.documentElement.scrollHeight - window.innerHeight;
      if (_bar) _bar.style.width = (m > 0 ? (s/m)*100 : 0) + '%';
    }});

    /* open first item on load */
    setTimeout(function() {{
      var firstHdr = document.querySelector('.acc-header');
      if (firstHdr) toggleAccSection(1, firstHdr);
    }}, 120);
  </script>
  {image_upload_script}
  {vault_bridge_script}
</body>
</html>"""

    # ──────────────────────────────────────────────────────────────────
    #  IMAGE UPLOAD SCRIPT
    # ──────────────────────────────────────────────────────────────────

    def _get_image_upload_script(self) -> str:
        return """
  <script>
    function _escImgHtml(s) {
      return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }
    window.uploadSectionImage = function(sectionId) {
      var old = document.getElementById('__img_upload_input__');
      if (old && old.parentNode) old.parentNode.removeChild(old);
      var input = document.createElement('input');
      input.type='file'; input.id='__img_upload_input__';
      input.accept='image/jpeg,image/jpg,image/png,image/webp,image/gif,image/svg+xml,image/bmp,image/tiff';
      input.multiple=false;
      input.setAttribute('aria-hidden','true');
      input.style.cssText='position:fixed;top:-9999px;left:-9999px;width:1px;height:1px;opacity:0;pointer-events:none;';
      var _cleaned=false;
      function _cleanup(){if(_cleaned)return;_cleaned=true;setTimeout(function(){if(input.parentNode)input.parentNode.removeChild(input);},500);}
      input.addEventListener('change',function(e){
        _cleanup();
        var file=e.target.files&&e.target.files[0]; if(!file)return;
        if(!file.type.startsWith('image/')){alert('Please select a valid image file.');return;}
        var imgId='img-'+sectionId+'-'+Date.now();
        var container=document.getElementById('images-'+sectionId);
        var objectUrl=URL.createObjectURL(file);
        if(container){
          var wrap=document.createElement('div'); wrap.className='uploaded-image-wrap'; wrap.setAttribute('data-img-id',imgId);
          var img=document.createElement('img'); img.className='uploaded-image'; img.alt=_escImgHtml(file.name||'Uploaded image'); img.loading='lazy'; img.src=objectUrl;
          img.onerror=function(){wrap.innerHTML='<div style="color:#dc2626;padding:16px;font-weight:700;">⚠️ Could not display image.</div>';};
          var delBtn=document.createElement('button'); delBtn.className='delete-image-btn'; delBtn.textContent='✕ Delete';
          delBtn.onclick=function(){deleteSectionImageEl(sectionId,imgId,wrap,objectUrl);};
          wrap.appendChild(img); wrap.appendChild(delBtn); container.appendChild(wrap);
        }
        var reader=new FileReader();
        reader.onerror=function(){console.warn('FileReader failed for',file.name);};
        reader.onload=function(ev){
          try{
            var imgData=ev.target.result;
            if(container){var ei=container.querySelector('[data-img-id="'+imgId+'"] img');if(ei)ei.src=imgData;}
            var saved=_getSaved(sectionId); saved.push({id:imgId,data:imgData,name:file.name,size:file.size});
            try{localStorage.setItem('images-'+sectionId,JSON.stringify(saved));}catch(se){console.warn('localStorage quota exceeded');}
          }catch(err){console.warn('DataURL error:',err);}
        };
        reader.readAsDataURL(file);
      });
      document.body.appendChild(input);
      setTimeout(function(){input.click();},50);
    };
    window.deleteSectionImageEl=function(sectionId,imgId,wrapEl,objectUrl){
      if(!confirm('Delete this image?'))return;
      if(objectUrl){try{URL.revokeObjectURL(objectUrl);}catch(e){}}
      if(wrapEl&&wrapEl.parentNode)wrapEl.parentNode.removeChild(wrapEl);
      var saved=_getSaved(sectionId).filter(function(img){return img.id!==imgId;});
      try{localStorage.setItem('images-'+sectionId,JSON.stringify(saved));}catch(e){}
    };
    window.deleteSectionImage=function(sectionId,imgId){
      if(!confirm('Delete this image?'))return;
      var saved=_getSaved(sectionId).filter(function(img){return img.id!==imgId;});
      try{localStorage.setItem('images-'+sectionId,JSON.stringify(saved));}catch(e){}
      _renderImages(sectionId);
    };
    function _getSaved(sectionId){
      try{var d=localStorage.getItem('images-'+sectionId);return d?JSON.parse(d):[];}catch(e){return[];}
    }
    function _renderImages(sectionId){
      var container=document.getElementById('images-'+sectionId); if(!container)return;
      var images=_getSaved(sectionId); container.innerHTML=''; if(!images.length)return;
      images.forEach(function(imgData){
        var wrap=document.createElement('div'); wrap.className='uploaded-image-wrap'; wrap.setAttribute('data-img-id',imgData.id);
        var img=document.createElement('img'); img.src=imgData.data; img.alt=imgData.name||'Uploaded image'; img.className='uploaded-image'; img.loading='lazy';
        img.onerror=function(){this.style.display='none';};
        var delBtn=document.createElement('button'); delBtn.className='delete-image-btn'; delBtn.textContent='\u2715 Delete';
        delBtn.onclick=function(){deleteSectionImage(sectionId,imgData.id);};
        wrap.appendChild(img); wrap.appendChild(delBtn); container.appendChild(wrap);
      });
    }
    setTimeout(function(){
      document.querySelectorAll('.section-images').forEach(function(c){
        _renderImages(c.id.replace('images-',''));
      });
    },300);
  </script>"""

    # ──────────────────────────────────────────────────────────────────
    #  VIDEO VAULT BRIDGE SCRIPT
    # ──────────────────────────────────────────────────────────────────

    def _get_vault_bridge_script(self) -> str:
        return """
  <script>
    /* ══════════════════════════════════════════════════════════
       VIDEO VAULT BRIDGE  v19.3
       Connects the Video Vault panel to the host application.

       The host/backend should populate window.__videoVault with
       an array of video objects:
         [
           {
             title:      "Conduction Animation",
             src:        "https://vault.example.com/video.mp4",
             animation_code: "<html>...</html>",
             thumbnail:  "https://...",
             duration:   "2:34",
             date:       "2025-01-15"
           },
           ...
         ]

       Alternatively, post a window message:
         window.postMessage({ type: 'video_vault', items: [...] }, '*');
    ══════════════════════════════════════════════════════════ */
    (function() {
      function _tryInject() {
        var items = null;
        try { items = window.opener && window.opener.__videoVault; } catch(e) {}
        if (!items) { try { items = window.parent && window.parent.__videoVault; } catch(e) {} }
        if (!items) { try { items = window.__videoVault; } catch(e) {} }
        if (items && Array.isArray(items) && typeof window.__injectVideoVault === 'function') {
          window.__injectVideoVault(items);
          return true;
        }
        return false;
      }
      if (!_tryInject()) {
        setTimeout(_tryInject, 1000);
        setTimeout(_tryInject, 3000);
      }
      window.addEventListener('message', function(e) {
        if (e.data && e.data.type === 'video_vault' && Array.isArray(e.data.items)) {
          if (typeof window.__injectVideoVault === 'function') {
            window.__injectVideoVault(e.data.items);
          }
        }
      });
    })();
  </script>"""

    # ──────────────────────────────────────────────────────────────────
    #  CSS — v19.3
    # ──────────────────────────────────────────────────────────────────

    def _get_ultimate_learning_css(self) -> str:
        svg_pattern_b64 = (
            "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='100' height='100'%3E"
            "%3Cpath d='M20 80 Q50 40 80 80' stroke='%233b82f6' stroke-width='1.5' fill='none' opacity='0.15'/%3E"
            "%3Cpath d='M10 50 Q50 10 90 50' stroke='%2306b6d4' stroke-width='1.5' fill='none' opacity='0.12'/%3E"
            "%3Cpolygon points='80,80 75,70 85,70' fill='%233b82f6' opacity='0.12'/%3E"
            "%3C/svg%3E"
        )

        return f"""
/* ══════════════════════════════════════════════════════════
   ULTIMATE LEARNING CSS  v20.0
   Working Process now generates ONE simple, realistic, single-scene
   animation per topic, modeled on hand-built reference animations.
   No forced layout archetypes, no rotating step captions — the whole
   process animates simultaneously and is built to be easy to follow.
   All other section styles retained from v19.0/v18.x.
══════════════════════════════════════════════════════════ */

:root {{
  --primary-blue:    #3b82f6;
  --success-green:   #10b981;
  --warning-orange:  #f59e0b;
  --text-gray:       #374151;
  --text-dark:       #111827;
  --bg-card:         #f8fafc;

  --primary:        #3b82f6;
  --primary-light:  #93c5fd;
  --primary-dark:   #1e40af;
  --success:        #22c55e;
  --warning:        #f59e0b;
  --danger:         #ef4444;
  --info:           #06b6d4;
  --gray-50:        #f9fafb;
  --gray-100:       #f3f4f6;
  --gray-200:       #e5e7eb;
  --gray-300:       #d1d5db;
  --gray-700:       #374151;
  --gray-900:       #111827;
  --blue-bg:        #eff6ff;   --blue-border:   #3b82f6;
  --green-bg:       #f0fdf4;  --green-border:  #22c55e;
  --red-bg:         #fef2f2;  --red-border:    #ef4444;
  --yellow-bg:      #fefce8;  --yellow-border: #eab308;
  --purple-bg:      #faf5ff;  --purple-border: #a855f7;
  --orange-bg:      #fff7ed;  --orange-border: #f97316;
  --type-color-1:   #3b82f6;
  --type-color-2:   #10b981;
  --type-color-3:   #f97316;
  --type-color-4:   #8b5cf6;
  --type-color-5:   #ef4444;
  --type-color-6:   #06b6d4;

  --font-body: Verdana, Geneva, sans-serif;
  --font-mono: 'Courier New', Courier, monospace;
  --radius-sm: 6px; --radius-md: 10px; --radius-lg: 16px; --radius-xl: 20px;
  --shadow-sm: 0 1px 2px rgba(0,0,0,.05);
  --shadow-md: 0 4px 6px -1px rgba(0,0,0,.1);
  --shadow-lg: 0 10px 15px -3px rgba(0,0,0,.1);
  --shadow-xl: 0 20px 25px -5px rgba(0,0,0,.1);
}}

*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
html {{
  scroll-behavior: smooth;
  font-size: 16px;
  overflow-x: hidden;
  -webkit-text-size-adjust: 100%;
  text-size-adjust: 100%;
}}
body {{
  font-family: Verdana, Geneva, sans-serif;
  font-size: clamp(1rem,2.5vw,1.05rem);
  line-height: 1.7;
  color: var(--text-dark);
  background: white;
  min-height: 100vh;
  width: 100%;
  overflow-x: hidden;
}}

/* ══════════════════════════════════════════════════════════
   RESPONSIVE BREAKPOINT MAP — mobile-first
   base (0–479px)   : phones
   480px+           : large phones / phablets
   768px+           : tablets
   1024px+          : laptops & desktops
   1440px+          : smartboards & large displays
   Every layout rule below is written mobile-first: unqualified
   rules target phones, and each min-width query progressively
   layers on more room, restoring the full original desktop
   design from 1024px up and scaling further for smartboards.
══════════════════════════════════════════════════════════ */

/* Media/overflow safety net — nothing is allowed to force
   horizontal scrolling on a narrow viewport. */
img, svg, video, iframe, table, canvas {{ max-width: 100%; height: auto; }}
pre, code {{ white-space: pre-wrap; word-break: break-word; }}

/* Touch-friendly controls: any coarse-pointer device (phone,
   tablet, or a touch-enabled smartboard) gets larger tap
   targets. Mouse-driven desktops are completely unaffected. */
@media (hover: none) and (pointer: coarse) {{
  button, .acc-header, .acc-goto-btn, .quiz-tab, .anim-tab, .q-opt,
  .footer-cta-btn, .anim-ctrl-btn, .anim-save-btn, .img-upload-btn,
  .delete-image-btn, .vault-refresh-btn, .anim-clear-btn,
  input, select, textarea {{
    min-height: 44px;
  }}
  .footer-cta-btn, .anim-ctrl-btn, .anim-save-btn, .q-opt, .img-upload-btn {{
    min-width: 44px;
  }}
  .fc-subtype-item, .app-card, .concept-card, .hook-card, .definition-box,
  .formula-card, .anim-lib-card, .vault-card, .quiz-question {{
    min-height: 44px;
  }}
}}

/* ── PROGRESS BAR ── */
#progressBar {{
  position: fixed; top: 0; left: 0; height: 4px; width: 0%;
  background: linear-gradient(90deg,var(--primary-blue),var(--info));
  z-index: 9999; transition: width .1s linear; border-radius: 0 2px 2px 0;
}}

/* ── PAGE CONTAINER (mobile-first padding) ── */
.page-container {{
  width: 100%;
  max-width: 1200px; margin: 0 auto; padding: 18px 14px;
  display: flex; flex-direction: column;
  background:
    url("{svg_pattern_b64}") repeat,
    linear-gradient(135deg, #f0f8ff 0%, #e0f7fa 100%);
  background-size: 100px 100px, cover;
  min-height: 100vh;
  overflow-x: hidden;
}}

/* ── PAGE HEADER (mobile-first padding) ── */
.page-header {{
  text-align: center; margin-bottom: 16px; padding: 22px 16px;
  background: white; border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl); display: flex; flex-direction: column;
  align-items: center; width: 100%;
}}
.header-badge {{
  display: inline-block; padding: 8px 16px;
  background: linear-gradient(135deg,var(--primary-blue),var(--info));
  color: white; border-radius: 20px; font-family: Verdana,sans-serif;
  font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing:.5px; margin-bottom: 16px;
}}
.page-title {{
  font-family: Verdana,sans-serif; font-size: clamp(1.8rem,5vw,2.4rem);
  font-weight: 700; margin-bottom: 8px; line-height: 1.2; color: var(--gray-900);
}}
.page-subtitle {{
  font-family: Verdana,sans-serif; font-size: clamp(.95rem,2.5vw,1rem);
  color: var(--gray-700); font-weight: 500;
}}

/* ── CLASSIFICATION BAR ── */
.classification-bar {{
  display: flex; align-items: center; flex-wrap: wrap; gap: 10px;
  padding: 10px 18px; margin-bottom: 16px;
  background: white; border: 2px solid; border-radius: var(--radius-md);
  box-shadow: var(--shadow-sm);
}}
.cls-label {{
  font-family: Verdana,sans-serif; font-size: 12px; font-weight: 800;
  text-transform: uppercase; letter-spacing:.4px;
}}
.cls-badge {{
  padding: 4px 12px; border-radius: 12px; font-family: Verdana,sans-serif;
  font-size: 11px; font-weight: 700;
}}
.cls-badge.formula  {{ background: #faf5ff; color: #7c3aed; border: 1.5px solid #c084fc; }}
.cls-badge.deriv    {{ background: #f0fdf4; color: #15803d; border: 1.5px solid #4ade80; }}
.cls-badge.concept  {{ background: var(--blue-bg); color: var(--primary-dark); border: 1.5px solid var(--primary-light); }}
.cls-badge.specific {{ background: #fff7ed; color: #c2410c; border: 1.5px solid #fb923c; }}

/* ── AUDIT SUMMARY ── */
.audit-summary {{
  background: #f0f9ff; border: 2px solid var(--primary-blue);
  border-radius: 12px; padding: 16px; margin-bottom: 24px;
}}
.audit-summary h3 {{ color: #1e40af; margin-bottom: 8px; font-family: Verdana,sans-serif; }}
.audit-summary p  {{ color: var(--text-dark); font-family: Verdana,sans-serif; }}

/* ── CONTENT LAYOUT (mobile-first: single column) ──
   Stacked nav-above-content on phones/tablets; becomes a
   sidebar-beside-main layout at laptop widths (1024px+),
   matching the original desktop design — see breakpoints below. */
.content-layout {{
  display: flex; flex-direction: column; gap: 14px; align-items: stretch; width: 100%;
}}
/* ── MAIN CONTENT / SECTIONS ── */
.main-content {{ display: flex; flex-direction: column; gap: 20px; flex: 1; min-width: 0; width: 100%; }}

/* ── SECTION NAV (mobile-first) ──
   Default = compact, horizontally-wrapping tab strip that sits
   above the content and never causes page-level horizontal
   scroll. A vertical sticky sidebar accordion (the original
   desktop look) is restored at 1024px+. */
.section-nav {{
  position: static; top: 0; z-index: 100;
  width: 100%; min-width: 0; max-width: 100%;
  align-self: stretch;
  background: white;
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-md);
  padding: 8px;
  margin-bottom: 4px;
  display: flex; flex-direction: row; flex-wrap: wrap;
  border: 1.5px solid var(--gray-200);
}}
.acc-panel-title {{
  display: none;
  font-family: Verdana,sans-serif; font-size: 12px; font-weight: 800;
  text-transform: uppercase; letter-spacing: .6px; color: var(--gray-700);
  padding: 0 16px 10px; border-bottom: 2px solid var(--gray-200); margin-bottom: 4px;
}}
.acc-item {{
  position: relative;
  border-bottom: none; border-right: 1px solid var(--gray-100);
  flex: 1 1 auto; min-width: 88px;
}}
.acc-item:last-child {{ border-right: none; }}
.acc-header {{
  width: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px;
  padding: 9px 6px; background: transparent; border: none; border-radius: 8px;
  font-family: Verdana,sans-serif; font-size: 11.5px; font-weight: 700;
  color: var(--gray-700); cursor: pointer; text-align: center;
  transition: background .2s, color .2s;
}}
.acc-header:hover {{ background: var(--gray-100); color: var(--primary-blue); }}
.acc-header.acc-active {{ color: var(--primary-blue); background: var(--blue-bg); }}
.acc-icon  {{ font-size: 16px; flex-shrink: 0; }}
.acc-label {{ flex: none; line-height: 1.25; }}
.acc-arrow {{
  font-size: 11px; color: var(--gray-700); flex-shrink: 0;
  transition: transform .25s;
}}
.acc-item.acc-open .acc-arrow {{ color: var(--primary-blue); }}
.acc-body {{
  max-height: 0; overflow: hidden; opacity: 0;
  transition: max-height .3s ease, opacity .25s ease;
  background: var(--gray-50);
  padding: 0 12px;
  position: absolute; z-index: 200; top: 100%; left: 50%; transform: translateX(-50%);
  min-width: 160px; max-width: min(220px, calc(100vw - 24px));
  border: 1.5px solid var(--primary-blue); border-radius: 8px;
  box-shadow: var(--shadow-lg);
}}
.acc-item.acc-open .acc-body {{ /* JS sets max-height + opacity */ }}
.acc-goto-btn {{
  display: block; width: 100%; text-align: left;
  padding: 10px 10px; margin: 8px 0;
  background: white; border: 1.5px solid var(--primary-blue);
  border-radius: 8px; font-family: Verdana,sans-serif; font-size: 12px;
  font-weight: 700; color: var(--primary-blue); cursor: pointer;
  transition: background .2s, color .2s;
}}
.acc-goto-btn:hover {{ background: var(--primary-blue); color: white; }}
.lesson-section {{
  width: 100%; margin: 1.25rem 0; border-radius: 12px;
  box-shadow: 0 4px 12px rgba(0,0,0,.1); background: white; overflow: hidden;
  padding: 1.25rem; scroll-margin-top: 80px; max-width: 100%;
}}
.section-header h2 {{
  font-family: Verdana,sans-serif; font-size: clamp(1.3rem,4vw,1.8rem);
  font-weight: 700; margin-bottom: 24px; padding-bottom: 16px;
  border-bottom: 3px solid var(--primary-blue); line-height: 1.3; color: var(--gray-900);
}}
.section-content {{
  line-height: 1.8; color: var(--text-dark); font-family: Verdana,sans-serif;
}}

/* ── HOOK CARD ── */
.hook-card {{
  margin: 16px 0; padding: 2rem;
  background: var(--orange-bg); border-left: 5px solid var(--orange-border);
  border-radius: 0 12px 12px 0; line-height: 1.6; transition: all .3s;
  font-family: Verdana,sans-serif;
}}
.hook-card:hover {{ transform: translateY(-4px); box-shadow: 0 8px 20px rgba(0,0,0,.15); }}
.hook-icon  {{ font-size: 28px; margin-bottom: 12px; }}
.hook-text  {{ color: var(--text-dark); font-family: Verdana,sans-serif; }}
.hook-lead  {{
  font-family: Verdana,sans-serif; font-size: clamp(1rem,2.5vw,1.15rem);
  font-weight: 800; color: var(--gray-900); margin-bottom: 10px; line-height: 1.4;
}}
.hook-bullets {{
  margin: 12px 0 0 20px; display: flex; flex-direction: column; gap: 6px; list-style: disc;
}}
.hook-bullets li {{
  font-family: Verdana,sans-serif; font-size: clamp(.9rem,2vw,1rem);
  color: var(--text-dark); line-height: 1.6;
}}
.hook-bullets li::marker {{ color: var(--orange-border); }}

/* ── DEFINITION BOX ── */
.definition-box {{
  margin: 16px 0; padding: 2rem;
  background: var(--blue-bg); border-left: 5px solid var(--blue-border);
  border-radius: 0 12px 12px 0; line-height: 1.6; transition: all .3s;
  font-family: Verdana,sans-serif;
}}
.definition-label {{
  font-family: Verdana,sans-serif; font-size: 11px; font-weight: 800;
  text-transform: uppercase; letter-spacing:.5px; margin-bottom: 10px; color: var(--gray-700);
}}
.definition-text {{ color: var(--text-dark); font-family: Verdana,sans-serif; }}
.def-analogy {{
  font-family: Verdana,sans-serif; font-size: clamp(.95rem,2.5vw,1.05rem);
  font-weight: 700; font-style: italic; color: var(--primary-dark);
  margin-bottom: 10px; line-height: 1.5;
}}
.def-properties {{ margin: 12px 0 0 20px; display: flex; flex-direction: column; gap: 6px; list-style: disc; }}
.def-properties li {{ font-family: Verdana,sans-serif; font-size: clamp(.9rem,2vw,1rem); color: var(--text-dark); line-height: 1.6; }}
.def-properties li::marker {{ color: var(--blue-border); }}
.def-properties li.def-example {{
  list-style: none; margin-left: -20px; margin-top: 6px; padding: 10px 14px;
  background: rgba(59,130,246,.08); border-radius: var(--radius-md);
  border-left: 3px solid var(--blue-border);
}}
.def-properties li.def-example strong {{ color: var(--primary-dark); }}

/* ── WORKING PROCESS (Unique SVG per topic) ── */
/* The generated SVG animation is fully self-contained with inline <style>.  */
/* These rules provide only the minimal outer-wrapper fallback layout.        */
.working-process-section {{
  margin: 16px 0;
  font-family: 'Nunito', Verdana, sans-serif;
}}

/* ── CONCEPT CARDS ── */
.concept-card {{
  background: white; border-left: 5px solid var(--primary-blue);
  border-radius: 0 12px 12px 0; padding: 2rem; margin: 16px 0;
  line-height: 1.6; box-shadow: var(--shadow-sm); transition: all .3s; font-family: Verdana,sans-serif;
}}
.concept-card:hover {{ transform: translateY(-4px); box-shadow: 0 8px 20px rgba(0,0,0,.15); }}
.concept-number {{
  display: inline-block; padding: 4px 12px; background: var(--primary-blue);
  color: white; border-radius: 20px; font-family: Verdana,sans-serif;
  font-size: 10px; font-weight: 800; text-transform: uppercase; margin-bottom: 8px;
}}
.concept-title {{
  font-family: Verdana,sans-serif; font-size: clamp(1.1rem,3vw,1.35rem);
  font-weight: 700; margin-bottom: 8px; color: var(--gray-900);
}}
.concept-definition {{
  font-family: Verdana,sans-serif; font-size: clamp(.95rem,2.5vw,1rem);
  color: var(--text-dark); font-weight: 600; margin-bottom: 12px;
  line-height: 1.6; border-left: 3px solid var(--primary-blue); padding-left: 12px;
}}
.concept-body {{ color: var(--text-dark); font-family: Verdana,sans-serif; }}
.concept-body p {{ font-family: Verdana,sans-serif; font-size: clamp(.95rem,2.5vw,1rem); line-height: 1.7; margin-bottom: 12px; }}
.concept-bullets {{ margin: 8px 0 0 20px; display: flex; flex-direction: column; gap: 7px; list-style: disc; }}
.concept-bullets li {{ font-family: Verdana,sans-serif; font-size: clamp(.9rem,2vw,1rem); color: var(--text-dark); line-height: 1.6; padding-left: 4px; }}
.concept-bullets li::marker {{ color: var(--primary-blue); }}

/* ── FORMULAS ── */
.formulas-section {{ margin: 16px 0; font-family: Verdana,sans-serif; }}
.formulas-header {{ text-align: center; margin-bottom: 28px; }}
.formulas-badge {{
  display: inline-block; padding: 5px 14px;
  background: linear-gradient(135deg,#7c3aed,#a855f7); color: white;
  border-radius: 20px; font-family: Verdana,sans-serif; font-size: 10px;
  font-weight: 800; text-transform: uppercase; letter-spacing:.6px; margin-bottom: 10px;
}}
.formulas-title {{ font-family: Verdana,sans-serif; font-size: clamp(1.3rem,4vw,1.8rem); font-weight: 700; color: var(--gray-900); margin-bottom: 6px; }}
.formulas-subtitle {{ font-family: Verdana,sans-serif; font-size: .9rem; color: var(--gray-700); font-weight: 500; }}
.formula-cards-stack {{ display: flex; flex-direction: column; gap: 24px; }}
.formula-card {{
  background: white; border-left: 5px solid #7c3aed;
  border-radius: 0 12px 12px 0; padding: 2rem; line-height: 1.5;
  box-shadow: var(--shadow-md); transition: all .3s; font-family: Verdana,sans-serif;
  width: 100%;
}}
.formula-card:hover {{ transform: translateY(-4px); box-shadow: 0 8px 20px rgba(0,0,0,.15); }}
.formula-name {{ font-family: Verdana,sans-serif; font-size: clamp(1rem,2.5vw,1.15rem); font-weight: 700; color: #7c3aed; margin-bottom: 16px; text-align: center; }}
.formula-equation {{
  background: linear-gradient(135deg,#faf5ff,#f3e8ff); border: 2px solid #c084fc;
  border-radius: var(--radius-md); padding: 2rem; margin: 16px 0;
  text-align: center; font-size: 24px; overflow-x: auto;
}}
.formula-symbols {{ margin: 20px 0; }}
.formula-symbols-title {{ font-family: Verdana,sans-serif; font-size: .9rem; font-weight: 700; margin-bottom: 10px; color: var(--text-dark); }}
.symbols-table {{ width: 100%; border-collapse: collapse; }}
.symbols-table tr {{ border-bottom: 1px solid var(--gray-200); }}
.symbols-table td {{ padding: 8px 12px; vertical-align: top; font-family: Verdana,sans-serif; }}
.symbol-var {{ font-family: var(--font-mono); font-weight: 700; color: #7c3aed; white-space: nowrap; width: 100px; }}
.symbol-desc {{ color: var(--text-dark); font-size: .9rem; line-height: 1.5; }}
.formulas-practice {{
  margin-top: 24px; padding: 14px 18px;
  background: var(--yellow-bg); border: 2px dashed var(--yellow-border);
  border-radius: var(--radius-md); font-family: Verdana,sans-serif; font-weight: 700;
  font-size: .9rem; text-align: center; color: var(--text-dark);
}}

/* ── DERIVATION SECTION ── */
.derivation-section {{ margin: 16px 0; font-family: Verdana,sans-serif; }}
.deriv-header {{
  text-align: center; margin-bottom: 24px; padding: 28px 20px;
  background: linear-gradient(135deg,#f0fdf4,#dcfce7);
  border: 2px solid #4ade80; border-radius: var(--radius-lg);
}}
.deriv-badge {{
  display: inline-block; padding: 5px 14px;
  background: linear-gradient(135deg,#16a34a,#22c55e); color: white;
  border-radius: 20px; font-family: Verdana,sans-serif; font-size: 10px;
  font-weight: 800; text-transform: uppercase; letter-spacing:.6px; margin-bottom: 10px;
}}
.deriv-title {{ font-family: Verdana,sans-serif; font-size: clamp(1.2rem,3.5vw,1.6rem); font-weight: 700; color: #14532d; margin-bottom: 6px; }}
.deriv-subtitle {{ font-family: Verdana,sans-serif; font-size: .9rem; color: #166534; font-weight: 500; }}
.deriv-intro {{ background: white; border-left: 4px solid #22c55e; border-radius: 0 var(--radius-md) var(--radius-md) 0; padding: 14px 18px; margin-bottom: 24px; }}
.deriv-intro p {{ font-family: Verdana,sans-serif; font-size: clamp(.95rem,2.5vw,1rem); line-height: 1.7; color: var(--text-dark); }}
.deriv-steps {{ display: flex; flex-direction: column; gap: 16px; }}
.deriv-step {{ background: white; border: 2px solid var(--gray-200); border-radius: var(--radius-md); padding: 20px; transition: all .3s; }}
.deriv-step:hover {{ border-color: #22c55e; box-shadow: var(--shadow-md); }}
.deriv-step-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }}
.deriv-step-num {{ flex-shrink: 0; padding: 4px 14px; background: linear-gradient(135deg,#16a34a,#22c55e); color: white; border-radius: 20px; font-family: Verdana,sans-serif; font-size: 11px; font-weight: 800; text-transform: uppercase; }}
.deriv-step-title {{ font-family: Verdana,sans-serif; font-size: clamp(1rem,2.5vw,1.1rem); font-weight: 700; color: var(--gray-900); }}
.deriv-step-eq {{ background: linear-gradient(135deg,#f0fdf4,#dcfce7); border: 2px solid #86efac; border-radius: var(--radius-md); padding: 1.5rem; text-align: center; font-size: 20px; overflow-x: auto; margin: 12px 0; }}
.deriv-step-explain {{ font-family: Verdana,sans-serif; font-size: clamp(.9rem,2vw,.98rem); line-height: 1.65; color: var(--text-dark); }}
.deriv-final-box {{ background: linear-gradient(135deg,#14532d,#166534); border-radius: var(--radius-lg); padding: 24px; text-align: center; margin-top: 8px; border: 2px solid #22c55e; }}
.deriv-final-label {{ font-family: Verdana,sans-serif; font-size: 14px; font-weight: 800; color: #86efac; text-transform: uppercase; letter-spacing:.6px; margin-bottom: 14px; }}
.deriv-final-eq {{ background: rgba(0,0,0,.3); border-radius: var(--radius-md); padding: 1.5rem; font-size: 22px; overflow-x: auto; color: white; margin-bottom: 16px; }}
.deriv-final-explain {{ font-family: Verdana,sans-serif; font-size: clamp(.9rem,2vw,.98rem); line-height: 1.65; color: #bbf7d0; }}
.deriv-meaning {{ margin-top: 24px; background: #f0fdf4; border: 2px solid var(--green-border); border-radius: var(--radius-md); padding: 18px 20px; }}
.deriv-meaning-title {{ font-family: Verdana,sans-serif; font-size: 14px; font-weight: 800; color: #15803d; margin-bottom: 10px; }}
.deriv-meaning p {{ font-family: Verdana,sans-serif; font-size: clamp(.95rem,2.5vw,1rem); line-height: 1.7; color: var(--text-dark); }}

/* ── TYPES ── */
.types-section {{ margin: 16px 0; font-family: Verdana,sans-serif; }}
.types-header {{ text-align: center; margin-bottom: 28px; }}
.types-badge {{ display: inline-block; padding: 5px 14px; background: linear-gradient(135deg,var(--success-green),var(--info)); color: white; border-radius: 20px; font-family: Verdana,sans-serif; font-size: 10px; font-weight: 800; text-transform: uppercase; letter-spacing:.6px; margin-bottom: 10px; }}
.types-main-title {{ font-family: Verdana,sans-serif; font-size: clamp(1.3rem,4vw,1.8rem); font-weight: 700; color: var(--gray-900); margin-bottom: 6px; }}
.types-subtitle {{ font-family: Verdana,sans-serif; font-size: .9rem; color: var(--gray-700); font-weight: 500; }}
.types-flowchart-wrap {{ background: linear-gradient(135deg,#f0f9ff,#f0fdf4); border: 2px solid var(--gray-200); border-radius: var(--radius-lg); padding: 2rem 20px 28px; overflow-x: auto; display: flex; flex-direction: column; align-items: center; }}
.fc-root-wrap {{ display: flex; justify-content: center; margin-bottom: 0; }}
.fc-root-node {{ padding: 14px 36px; background: linear-gradient(135deg,#1e40af,var(--primary-blue)); color: white; border-radius: 50px; font-family: Verdana,sans-serif; font-size: 16px; font-weight: 700; box-shadow: 0 6px 20px rgba(59,130,246,.35); text-align: center; min-width: 180px; }}
.fc-v-line {{ width: 2px; height: 28px; background: var(--gray-300); margin: 0 auto; }}
.fc-h-rail {{ height: 2px; width: 90%; background: var(--gray-300); margin: 0 auto; }}
.fc-branches-row {{ display: flex; gap: 14px; justify-content: center; align-items: flex-start; flex-wrap: wrap; padding-top: 0; width: 100%; }}
.fc-branch-col {{ display: flex; flex-direction: column; align-items: center; gap: 8px; min-width: 150px; max-width: 190px; flex: 1; }}
.fc-down-line {{ width: 2px; height: 24px; background: var(--gray-300); margin: 0 auto; }}
.fc-type-card {{ background: white; border: 2px solid var(--gray-200); border-top: 4px solid var(--tc,var(--primary-blue)); border-radius: var(--radius-md); padding: 14px 12px; text-align: center; width: 100%; box-shadow: var(--shadow-sm); transition: all .3s; }}
.fc-type-card:hover {{ transform: translateY(-4px); box-shadow: 0 8px 20px rgba(0,0,0,.15); }}
.fc-type-emoji {{ font-size: 28px; margin-bottom: 6px; }}
.fc-type-name {{ font-family: Verdana,sans-serif; font-size: 12px; font-weight: 800; color: var(--gray-900); margin-bottom: 5px; }}
.fc-type-desc {{ font-family: Verdana,sans-serif; font-size: 10px; color: var(--gray-700); line-height: 1.45; }}
.fc-subtypes-col {{ display: flex; flex-direction: column; gap: 5px; width: 100%; }}
.fc-subtype-item {{ background: var(--blue-bg); border: 1.5px solid var(--primary-light); border-radius: 8px; padding: 5px 10px; font-family: Verdana,sans-serif; font-size: 10px; font-weight: 700; color: var(--primary-dark); text-align: center; transition: all .2s; }}
.fc-subtype-item:hover {{ background: var(--primary-blue); color: white; border-color: var(--primary-blue); }}
.types-compare-box {{ margin-top: 28px; background: white; border: 2px solid var(--gray-200); border-radius: var(--radius-md); overflow: hidden; }}
.tc-header {{ padding: 12px 18px; background: var(--gray-900); color: white; font-family: Verdana,sans-serif; font-size: 13px; font-weight: 800; }}
.tc-table-wrap {{ overflow-x: auto; }}
.tc-table {{ width: 100%; border-collapse: collapse; font-family: Verdana,sans-serif; font-size: 12px; }}
.tc-table th {{ background: var(--gray-100); padding: 10px 14px; text-align: left; font-family: Verdana,sans-serif; font-weight: 800; color: var(--gray-900); border-bottom: 2px solid var(--gray-200); white-space: nowrap; }}
.tc-table td {{ padding: 9px 14px; border-bottom: 1px solid var(--gray-100); color: var(--text-dark); vertical-align: top; line-height: 1.5; font-family: Verdana,sans-serif; }}
.tc-table tr:nth-child(even) td {{ background: var(--gray-50); }}
.tc-table tr:hover td {{ background: var(--blue-bg); }}
.tc-table td:first-child {{ font-weight: 700; color: var(--gray-900); }}
.types-recall {{ margin-top: 20px; padding: 14px 18px; background: var(--yellow-bg); border: 2px dashed var(--yellow-border); border-radius: var(--radius-md); font-family: Verdana,sans-serif; font-weight: 700; font-size: .9rem; text-align: center; color: var(--text-dark); }}

/* ── APPLICATIONS ── */
.applications-section {{ margin: 16px 0; font-family: Verdana,sans-serif; }}
.app-title {{ font-family: Verdana,sans-serif; font-size: clamp(1.2rem,3vw,1.5rem); font-weight: 700; margin-bottom: 24px; color: var(--gray-900); }}
.app-grid {{ display: grid; grid-template-columns: 1fr; gap: 16px; margin-bottom: 24px; }}
@media (min-width: 768px) {{ .app-grid {{ grid-template-columns: repeat(2,1fr); }} }}
.app-card {{ padding: 2rem; background: white; border-left: 5px solid var(--primary-blue); border-radius: 0 12px 12px 0; line-height: 1.5; transition: all .3s; font-family: Verdana,sans-serif; }}
.app-card:hover {{ transform: translateY(-4px); box-shadow: 0 8px 20px rgba(0,0,0,.15); }}
.app-icon {{ font-size: 32px; margin-bottom: 8px; }}
.app-domain {{ font-family: Verdana,sans-serif; font-size: 11px; font-weight: 800; text-transform: uppercase; color: var(--primary-blue); margin-bottom: 8px; letter-spacing:.5px; }}
.app-text {{ font-family: Verdana,sans-serif; font-size: clamp(.95rem,2.5vw,1rem); line-height: 1.6; color: var(--text-dark); }}
.creativity-challenge {{ padding: 16px; background: var(--orange-bg); border: 2px solid var(--orange-border); border-radius: var(--radius-md); text-align: center; font-family: Verdana,sans-serif; font-weight: 700; color: var(--text-dark); }}

/* ── IMAGE UPLOAD ── */
.img-upload-btn {{ display: inline-block; margin-top: 12px; padding: 1rem 1.5rem; background: linear-gradient(135deg,#06b6d4,#0891b2); color: white; border: none; border-radius: 8px; font-family: Verdana,sans-serif; font-size: 12px; font-weight: 700; cursor: pointer; transition: all .3s; }}
.img-upload-btn:hover {{ transform: scale(1.05); box-shadow: var(--shadow-md); }}
.section-images {{ display: flex; flex-direction: column; gap: 24px; margin-top: 24px; align-items: center; width: 100%; }}
.uploaded-image-wrap {{ position: relative; border: 3px solid var(--gray-200); border-radius: var(--radius-lg); padding: 20px; background: white; transition: all .3s; display: flex; flex-direction: column; justify-content: center; align-items: center; width: 100%; max-width: 960px; box-shadow: var(--shadow-md); }}
.uploaded-image-wrap:hover {{ border-color: var(--primary-blue); box-shadow: var(--shadow-xl); transform: translateY(-2px); }}
.uploaded-image {{ display: block; width: auto; max-width: 100%; max-height: 640px; height: auto; border-radius: var(--radius-md); object-fit: contain; margin: 0 auto; }}
.delete-image-btn {{ margin-top: 12px; align-self: flex-end; padding: 8px 18px; background: rgba(239,68,68,.95); color: white; border: none; border-radius: var(--radius-md); font-family: Verdana,sans-serif; font-size: 12px; font-weight: 700; cursor: pointer; transition: all .3s; }}
.delete-image-btn:hover {{ background: #dc2626; transform: scale(1.05); }}

/* ── QUIZ ── */
.quiz-section {{ margin: 16px 0; font-family: Verdana,sans-serif; }}
.quiz-header {{ text-align: center; margin-bottom: 24px; padding: 24px; background: linear-gradient(135deg,var(--purple-bg),var(--blue-bg)); border-radius: var(--radius-lg); border: 2px solid var(--purple-border); }}
.quiz-title {{ font-family: Verdana,sans-serif; font-size: clamp(1.2rem,3vw,1.5rem); font-weight: 700; margin-bottom: 6px; color: var(--gray-900); }}
.quiz-subtitle {{ font-family: Verdana,sans-serif; font-size: .9rem; color: var(--gray-700); margin-bottom: 16px; }}
.quiz-score-bar {{ display: inline-flex; align-items: center; gap: 10px; padding: 8px 20px; background: white; border-radius: 20px; border: 2px solid var(--purple-border); }}
.quiz-score-label {{ font-family: Verdana,sans-serif; font-size: 11px; font-weight: 700; color: var(--gray-700); }}
.quiz-score-value {{ font-family: var(--font-mono); font-size: 17px; font-weight: 900; color: var(--purple-border); }}
.quiz-tabs {{ display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 20px; background: var(--gray-100); padding: 6px; border-radius: var(--radius-md); }}
.quiz-tab {{ flex: 1; min-width: 60px; padding: 9px 12px; border: none; border-radius: 8px; font-family: Verdana,sans-serif; font-size: 12px; font-weight: 700; cursor: pointer; background: transparent; color: var(--gray-700); transition: all .2s; }}
.quiz-tab.active {{ background: linear-gradient(135deg,#7c3aed,#a855f7); color: white; box-shadow: var(--shadow-md); }}
.quiz-tab:hover:not(.active) {{ background: var(--purple-bg); color: #7c3aed; }}
.quiz-set {{ display: none; max-height: 400px; overflow-y: auto; }}
.quiz-set.active {{ display: block; }}
.set-title {{ font-family: Verdana,sans-serif; font-size: 16px; font-weight: 700; color: #7c3aed; margin-bottom: 4px; }}
.set-progress {{ font-family: Verdana,sans-serif; font-size: 11px; color: var(--gray-700); margin-bottom: 20px; font-weight: 600; }}
.quiz-question {{ background: white; border: 2px solid var(--gray-200); border-radius: var(--radius-md); padding: 20px; margin-bottom: 16px; transition: border-color .2s; }}
.quiz-question:hover {{ border-color: var(--purple-border); }}
.q-number {{ font-family: Verdana,sans-serif; font-size: 11px; font-weight: 800; color: var(--gray-700); text-transform: uppercase; letter-spacing:.5px; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }}
.q-difficulty {{ padding: 3px 10px; border-radius: 12px; font-family: Verdana,sans-serif; font-size: 9px; font-weight: 800; text-transform: uppercase; letter-spacing:.5px; }}
.q-difficulty.easy   {{ background: #dcfce7; color: #15803d; }}
.q-difficulty.medium {{ background: #fef9c3; color: #854d0e; }}
.q-difficulty.hard   {{ background: #fee2e2; color: #991b1b; }}
.q-text {{ font-family: Verdana,sans-serif; font-size: clamp(.95rem,2.5vw,1rem); font-weight: 600; line-height: 1.6; margin-bottom: 14px; color: var(--text-dark); }}
.q-options {{ display: flex; flex-direction: column; gap: 8px; }}
.q-opt {{ text-align: left; padding: 11px 16px; background: var(--bg-card); border: 2px solid var(--gray-200); border-radius: var(--radius-sm); font-family: Verdana,sans-serif; font-size: .9rem; font-weight: 600; color: var(--text-dark); cursor: pointer; transition: all .2s; }}
.q-opt:hover:not(:disabled) {{ background: var(--purple-bg); border-color: var(--purple-border); color: #7c3aed; transform: translateX(4px); }}
.q-opt.q-correct {{ background: #dcfce7!important; border-color: #22c55e!important; color: #14532d!important; }}
.q-opt.q-wrong   {{ background: #fee2e2!important; border-color: #ef4444!important; color: #7f1d1d!important; }}
.q-opt:disabled  {{ cursor: not-allowed; opacity: .85; }}
.q-feedback {{ margin-top: 10px; padding: 10px 14px; border-radius: var(--radius-sm); font-family: Verdana,sans-serif; font-size: 12px; font-weight: 700; display: none; }}
.q-feedback.q-fb-correct {{ display: block; background: #dcfce7; color: #14532d; border: 1.5px solid #22c55e; }}
.q-feedback.q-fb-wrong   {{ display: block; background: #fee2e2; color: #7f1d1d; border: 1.5px solid #ef4444; }}
.set-score-bar {{ text-align: right; margin-top: 16px; padding: 10px 16px; background: var(--bg-card); border-radius: var(--radius-sm); font-family: Verdana,sans-serif; font-size: 13px; color: var(--text-dark); }}

/* ══════════════════════════════════════════════════════
   ANIMATION SECTION  — Video Vault styles
══════════════════════════════════════════════════════ */
.animation-section {{ margin: 16px 0; font-family: Verdana,sans-serif; }}
.anim-section-header {{ text-align: center; margin-bottom: 24px; padding: 28px; background: linear-gradient(135deg,#0f172a,#1e3a5f,#312e81); border-radius: var(--radius-lg); }}
.anim-title-badge {{ font-family: Verdana,sans-serif; font-size: 20px; font-weight: 700; color: white; margin-bottom: 8px; }}
.anim-subtitle {{ font-family: Verdana,sans-serif; font-size: 12px; color: #94a3b8; font-weight: 500; }}
.anim-source-tabs {{ display: flex; gap: 0; margin-bottom: 20px; background: var(--gray-100); padding: 5px; border-radius: var(--radius-md); border: 1.5px solid var(--gray-200); }}
.anim-tab {{ flex: 1; padding: 1rem 1.5rem; border: none; border-radius: 9px; font-family: Verdana,sans-serif; font-size: 12px; font-weight: 700; cursor: pointer; background: transparent; color: var(--gray-700); transition: all .3s; }}
.anim-tab.active {{ background: linear-gradient(135deg,#0d9488,#14b8a6); color: white; box-shadow: var(--shadow-md); }}
.anim-tab:hover:not(.active) {{ background: #f0fdfa; color: #0d9488; }}
.anim-panel {{ margin-bottom: 16px; }}
.anim-drop-zone {{ border: 2px dashed #0d9488; border-radius: var(--radius-md); background: #f0fdfa; padding: 48px 20px; text-align: center; cursor: pointer; transition: all .2s; display: flex; flex-direction: column; align-items: center; gap: 10px; }}
.anim-drop-zone:hover, .anim-drag-over {{ border-color: #0f766e; background: #ccfbf1; }}
.anim-drop-icon {{ font-size: 48px; line-height: 1; }}
.anim-drop-text {{ font-family: Verdana,sans-serif; font-size: 14px; font-weight: 700; color: #0d9488; }}
.anim-drop-sub {{ font-family: Verdana,sans-serif; font-size: 11px; color: var(--gray-700); font-weight: 500; }}
.anim-file-info {{ display: flex; align-items: center; gap: 10px; padding: 10px 14px; background: #f0fdf4; border: 1.5px solid var(--success-green); border-radius: 10px; margin-top: 10px; }}
.anim-file-name {{ font-family: Verdana,sans-serif; font-size: 12px; font-weight: 700; color: #15803d; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.anim-clear-btn {{ padding: 5px 12px; border-radius: 7px; border: 1.5px solid #fca5a5; background: #fee2e2; color: #ef4444; font-family: Verdana,sans-serif; font-size: 11px; font-weight: 700; cursor: pointer; }}
.anim-lib-search {{ width: 100%; padding: 10px 14px; border: 2px solid var(--gray-200); border-radius: var(--radius-sm); font-family: Verdana,sans-serif; font-size: 13px; outline: none; transition: border-color .2s; color: var(--text-dark); }}
.anim-lib-search:focus {{ border-color: #0d9488; }}
.anim-lib-grid {{ display: grid; grid-template-columns: repeat(auto-fill,minmax(180px,1fr)); gap: 14px; }}
.anim-lib-card {{ background: white; border: 2px solid var(--gray-200); border-radius: var(--radius-md); padding: 16px; cursor: pointer; transition: all .22s; }}
.anim-lib-card:hover {{ border-color: #0d9488; transform: translateY(-3px); box-shadow: var(--shadow-lg); }}
.anim-lib-card-icon {{ font-size: 28px; margin-bottom: 6px; }}
.anim-lib-card-title {{ font-family: Verdana,sans-serif; font-size: 13px; font-weight: 700; color: var(--gray-900); margin-bottom: 4px; }}
.anim-lib-card-date {{ font-family: Verdana,sans-serif; font-size: 10px; color: var(--gray-700); }}
.vault-header {{ margin-bottom: 14px; }}
.vault-title-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }}
.vault-icon {{ font-size: 22px; }}
.vault-title {{ font-family: Verdana,sans-serif; font-size: 16px; font-weight: 800; color: var(--gray-900); flex: 1; }}
.vault-refresh-btn {{ padding: 6px 14px; border: 1.5px solid #0d9488; background: #f0fdfa; color: #0d9488; border-radius: 8px; font-family: Verdana,sans-serif; font-size: 11px; font-weight: 700; cursor: pointer; transition: all .2s; }}
.vault-refresh-btn:hover {{ background: #0d9488; color: white; }}
.vault-status {{ margin: 8px 0; }}
.vault-loading {{ display: none; align-items: center; gap: 10px; padding: 16px; font-family: Verdana,sans-serif; font-size: 13px; color: #64748b; font-weight: 600; }}
.vault-spinner {{ width: 18px; height: 18px; border: 3px solid #e2e8f0; border-top-color: #0d9488; border-radius: 50%; animation: vaultSpin .7s linear infinite; }}
@keyframes vaultSpin {{ to {{ transform: rotate(360deg); }} }}
.vault-empty {{ display: none; text-align: center; padding: 40px 20px; color: var(--gray-700); font-family: Verdana,sans-serif; }}
.vault-grid {{ margin-top: 12px; }}
.vault-card {{ padding: 0 0 10px; overflow: hidden; }}
.vault-card-thumb {{ width: 100%; height: 90px; background: linear-gradient(135deg,#0f172a,#1e3a5f); border-radius: var(--radius-sm) var(--radius-sm) 0 0; display: flex; align-items: center; justify-content: center; position: relative; overflow: hidden; margin-bottom: 8px; }}
.vault-card-play {{ font-size: 28px; color: rgba(255,255,255,.85); text-shadow: 0 2px 8px rgba(0,0,0,.4); transition: transform .2s; }}
.vault-card:hover .vault-card-play {{ transform: scale(1.2); }}
.vault-card-dur {{ position: absolute; bottom: 5px; right: 6px; background: rgba(0,0,0,.75); color: white; font-family: var(--font-mono); font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px; }}
.vault-card-meta {{ padding: 0 10px; }}
.vault-footer {{ display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--gray-200); }}
.vault-count {{ font-family: Verdana,sans-serif; font-size: 11px; font-weight: 700; color: #64748b; }}
.vault-info {{ font-family: Verdana,sans-serif; font-size: 10px; color: #94a3b8; font-style: italic; }}
.anim-player-wrap {{ background: #0f172a; border-radius: var(--radius-lg); overflow: hidden; border: 2px solid #1e3a5f; box-shadow: 0 8px 32px rgba(0,0,0,.4); }}
.anim-player-topbar {{ display: flex; align-items: center; justify-content: space-between; padding: 12px 18px; background: #0f172a; border-bottom: 1px solid #1e293b; flex-wrap: wrap; gap: 10px; }}
.anim-player-label {{ font-family: Verdana,sans-serif; font-size: 12px; font-weight: 700; color: #94a3b8; }}
.anim-player-actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
.anim-ctrl-btn {{ display: inline-flex; align-items: center; gap: 5px; padding: 8px 16px; border-radius: 8px; border: none; font-family: Verdana,sans-serif; font-size: 11px; font-weight: 700; cursor: pointer; transition: all .3s; }}
.anim-ctrl-btn:hover {{ transform: scale(1.05); }}
.anim-ctrl-btn.present    {{ background: linear-gradient(135deg,#22c55e,#16a34a); color: white; }}
.anim-ctrl-btn.pause      {{ background: linear-gradient(135deg,var(--warning-orange),#d97706); color: white; }}
.anim-ctrl-btn.fullscreen {{ background: rgba(255,255,255,.1); color: #94a3b8; border: 1.5px solid #334155; }}
.anim-ctrl-btn.restart    {{ background: rgba(255,255,255,.08); color: #94a3b8; border: 1.5px solid #334155; }}
.anim-video-container {{ background: #000; width: 100%; line-height: 0; }}
.anim-video {{ width: 100%; max-height: 520px; display: block; background: #000; }}
.anim-iframe {{ width: 100%; height: 520px; border: none; display: block; }}
.anim-save-bar {{ display: flex; align-items: center; gap: 14px; padding: 14px 18px; background: #0f172a; border-top: 1px solid #1e293b; min-height: 54px; }}
.anim-save-btn {{ display: inline-flex; align-items: center; gap: 6px; padding: 1rem 1.5rem; background: linear-gradient(135deg,#14b8a6,#0d9488); color: white; border: none; border-radius: 8px; font-family: Verdana,sans-serif; font-size: 13px; font-weight: 800; cursor: pointer; transition: all .3s; box-shadow: 0 2px 8px rgba(20,184,166,.35); }}
.anim-save-btn:hover:not(:disabled) {{ transform: scale(1.05); box-shadow: 0 4px 14px rgba(20,184,166,.45); }}
.anim-save-btn:disabled {{ background: linear-gradient(135deg,#22c55e,#16a34a); cursor: not-allowed; opacity: .9; }}
.anim-save-status {{ font-family: Verdana,sans-serif; font-size: 12px; font-weight: 700; color: #94a3b8; flex: 1; }}

/* ── FOOTER ── */
.page-footer {{ text-align: center; margin-top: 48px; padding: 32px; background: white; border-radius: var(--radius-lg); box-shadow: var(--shadow-md); width: 100%; font-family: Verdana,sans-serif; }}
.page-footer p {{ font-family: Verdana,sans-serif; font-size: .9rem; color: var(--gray-700); font-weight: 500; margin: 8px 0; }}
.footer-cta {{ display: flex; justify-content: center; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
.footer-cta-btn {{ padding: 1rem 1.5rem; border: none; border-radius: 8px; font-family: Verdana,sans-serif; font-size: 14px; font-weight: 700; cursor: pointer; transition: all .3s; }}
.footer-cta-btn:hover {{ transform: scale(1.05); }}
.footer-cta-btn.quiz-cta {{ background: linear-gradient(135deg,#7c3aed,#a855f7); color: white; box-shadow: 0 4px 12px rgba(124,58,237,.3); }}
.footer-cta-btn.anim-cta {{ background: linear-gradient(135deg,#0d9488,#14b8a6); color: white; box-shadow: 0 4px 12px rgba(13,148,136,.3); }}
.error-section {{ padding: 16px; background: var(--red-bg); border: 2px solid var(--red-border); border-radius: var(--radius-md); color: #7f1d1d; font-family: Verdana,sans-serif; font-weight: 600; }}

/* ── DARK MODE ── */
@media (prefers-color-scheme: dark) {{
  /* Re-point the shared design tokens at dark-safe values. Every
     component that reads var(--text-dark), var(--gray-900/700),
     var(--bg-card), or a semantic tinted-panel color (--blue-bg,
     --orange-bg, etc.) picks this up automatically — this fixes
     "text becomes invisible in dark mode" at the root instead of
     chasing it selector by selector. */
  :root {{
    --text-dark:    #f1f5f9;
    --text-gray:    #cbd5e1;
    --gray-900:     #f1f5f9;
    --gray-700:     #cbd5e1;
    --gray-300:     #475569;
    --gray-200:     #334155;
    --gray-100:     #1e293b;
    --gray-50:      #0f172a;
    --bg-card:      #0f172a;
    --primary-dark: #93c5fd;
    --blue-bg:      rgba(59,130,246,.16);
    --green-bg:     rgba(34,197,94,.16);
    --red-bg:       rgba(239,68,68,.16);
    --yellow-bg:    rgba(234,179,8,.16);
    --purple-bg:    rgba(168,85,247,.16);
    --orange-bg:    rgba(249,115,22,.16);
  }}

  body {{ background: #0f172a; color: #f1f5f9; }}
  .page-container {{
    background:
      url("{svg_pattern_b64}") repeat,
      linear-gradient(135deg,#0f172a 0%,#1e293b 100%);
  }}
  .page-header, .lesson-section, .page-footer {{ background: #1e293b; color: #f1f5f9; }}
  .section-header h2 {{ color: #f1f5f9; }}
  .section-nav {{ background: #1e293b; border-color: #334155; }}
  .acc-header {{ color: #cbd5e1; }}
  .acc-header:hover {{ background: #0f172a; color: #93c5fd; }}
  .acc-header.acc-active {{ background: rgba(59,130,246,.12); color: #93c5fd; }}
  .acc-body {{ background: #0f172a; }}
  .acc-goto-btn {{ background: #1e293b; color: #93c5fd; border-color: #3b82f6; }}
  .acc-goto-btn:hover {{ background: #3b82f6; color: white; }}
  .acc-panel-title {{ color: #94a3b8; border-color: #334155; }}
  .classification-bar {{ background: #1e293b; }}
  .concept-card, .formula-card, .app-card {{ background: #1e293b; color: #f1f5f9; }}
  .concept-definition, .concept-body p, .concept-bullets li,
  .app-text, .q-text, .q-opt {{ color: #e2e8f0; }}
  .hook-lead, .hook-bullets li {{ color: #f1f5f9; }}
  .def-analogy {{ color: #93c5fd; }}
  .def-properties li {{ color: #e2e8f0; }}
  .def-properties li.def-example {{ background: rgba(59,130,246,.15); }}
  .quiz-question {{ background: #1e293b; border-color: #334155; }}
  .q-opt {{ background: #0f172a; border-color: #334155; color: #e2e8f0; }}
  .tc-table td {{ color: #e2e8f0; }}
  .uploaded-image-wrap {{ background: #1e293b; border-color: #334155; }}
  .symbol-desc {{ color: #e2e8f0; }}
  .deriv-intro {{ background: #1e293b; }}
  .deriv-intro p, .deriv-step-explain, .deriv-meaning p {{ color: #e2e8f0; }}
  .deriv-step {{ background: #1e293b; border-color: #334155; }}
  .deriv-step-title {{ color: #f1f5f9; }}
  .deriv-meaning {{ background: #1a2e1a; }}
  .audit-summary {{ background: #1e3a5f; }}
  .audit-summary p {{ color: #e2e8f0; }}
  .vault-title {{ color: #f1f5f9; }}
  .anim-lib-card {{ background: #1e293b; border-color: #334155; }}
  .anim-lib-card-title {{ color: #f1f5f9; }}

  /* ── Remaining panels that use a hardcoded (non-token) light
     fill, or rely on a browser-default white form-control
     background, still need an explicit dark surface so the
     now-light text drawn on top of them stays readable. ── */
  .formula-equation      {{ background: linear-gradient(135deg,#241a3d,#2f2150); border-color: #7c3aed; color: #f1f5f9; }}
  .deriv-step-eq          {{ background: linear-gradient(135deg,#0f2b18,#123a1e); border-color: #22c55e; color: #f1f5f9; }}
  .deriv-header           {{ background: linear-gradient(135deg,#0f2b18,#123a1e); border-color: #22c55e; }}
  .deriv-title            {{ color: #86efac; }}
  .deriv-subtitle         {{ color: #bbf7d0; }}
  .deriv-meaning-title    {{ color: #86efac; }}
  .audit-summary h3       {{ color: #93c5fd; }}
  .types-flowchart-wrap   {{ background: linear-gradient(135deg,#0f172a,#132030); border-color: #334155; }}
  .fc-type-card           {{ background: #1e293b; }}
  .types-compare-box      {{ background: #1e293b; border-color: #334155; }}
  .tc-header              {{ background: #0f172a; }}
  .anim-lib-search        {{ background: #0f172a; color: #f1f5f9; }}
  .anim-lib-search::placeholder {{ color: #94a3b8; }}
}}

/* ══════════════════════════════════════════════════════════
   RESPONSIVE — mobile-first breakpoint ladder
   Everything above this point already renders correctly on a
   phone (single column, stacked nav, no horizontal scroll).
   Each block below only ADDS more room/structure as the
   viewport grows — nothing here needs to "fix" mobile.
══════════════════════════════════════════════════════════ */

/* ── PHONE — small screens (≤479px): a bit more compact ── */
@media (max-width: 479px) {{
  .page-container {{ padding: 14px 10px; }}
  .page-header {{ padding: 18px 12px; }}
  .acc-item {{ min-width: 74px; }}
  .acc-header {{ font-size: 10.5px; padding: 8px 4px; }}
  .acc-label {{ display: none; }}           /* icon-only tabs on the smallest screens */
  .q-difficulty, .fc-type-desc, .anim-lib-card-date, .vault-info, .symbol-var {{ font-size: 11px; }}
  .fc-branches-row {{ flex-direction: column; align-items: center; }}
  .fc-branch-col {{ max-width: 100%; width: 100%; }}
  .footer-cta {{ flex-direction: column; align-items: stretch; }}
  .footer-cta-btn {{ width: 100%; text-align: center; }}
  .uploaded-image {{ max-height: 320px; }}
  .quiz-tabs {{ gap: 4px; }}
  .quiz-tab {{ font-size: 11px; padding: 8px 6px; }}
  .anim-player-actions {{ width: 100%; justify-content: stretch; }}
  .anim-ctrl-btn {{ flex: 1; justify-content: center; }}
}}

/* ── PHABLET / large phones (480px+) ── */
@media (min-width: 480px) {{
  .page-container {{ padding: 22px 18px; }}
  .page-header {{ padding: 26px 20px; }}
  .lesson-section {{ padding: 1.5rem; }}
  .acc-item {{ min-width: 96px; }}
}}

/* ── TABLET (768px+): more breathing room, still stacked nav ── */
@media (min-width: 768px) {{
  .page-container {{ padding: 32px 24px; }}
  .page-header {{ padding: 36px 32px; }}
  .content-layout {{ gap: 20px; }}
  .main-content {{ gap: 28px; }}
  .lesson-section {{ padding: 1.75rem; margin: 1.5rem 0; }}
  .section-nav {{ padding: 10px; }}
  .acc-header {{ font-size: 12.5px; padding: 10px 8px; }}
  .app-grid {{ grid-template-columns: repeat(2,1fr); }}
  .anim-lib-grid {{ grid-template-columns: repeat(auto-fill,minmax(160px,1fr)); }}
  .footer-cta {{ flex-direction: row; }}
}}

/* ── LAPTOP / DESKTOP (1024px+): restores the original sidebar
   layout — sticky vertical accordion nav beside the content,
   exactly as in the desktop-only design this was based on. ── */
@media (min-width: 1024px) {{
  .page-container {{ padding: 48px 24px; }}
  .page-header {{ padding: 48px; margin-bottom: 24px; }}
  .content-layout {{ flex-direction: row; gap: 28px; align-items: flex-start; }}
  .main-content {{ gap: 32px; }}
  .lesson-section {{ padding: 2rem; margin: 2rem 0; }}

  .section-nav {{
    position: sticky; top: 0;
    width: 260px; min-width: 220px; max-width: 280px;
    align-self: flex-start;
    flex-direction: column; flex-wrap: nowrap;
    padding: 12px 0 16px;
    margin-bottom: 32px;
    border-radius: var(--radius-lg);
  }}
  .acc-panel-title {{ display: block; }}
  .acc-item {{
    border-right: none; border-bottom: 1px solid var(--gray-100);
    flex: none; min-width: 0;
  }}
  .acc-header {{
    flex-direction: row; align-items: center; justify-content: flex-start;
    gap: 8px; padding: 10px 16px; font-size: 12px; text-align: left;
  }}
  .acc-label {{ display: inline; flex: 1; }}
  .acc-body {{
    position: static; left: auto; top: auto; transform: none;
    min-width: 0; max-width: none;
    border: none; border-radius: 0; box-shadow: none;
    padding: 0 16px;
  }}
}}

/* ── SMARTBOARD / large interactive displays (1440px+):
   wider canvas and bigger type for room-scale, at-a-distance
   readability, plus extra-generous touch targets for
   touch-enabled smartboards/kiosks. ── */
@media (min-width: 1440px) {{
  html {{ font-size: 18px; }}
  .page-container {{ max-width: 1500px; padding: 64px 40px; }}
  .page-header {{ padding: 56px; }}
  .content-layout {{ gap: 36px; }}
  .section-nav {{ width: 300px; min-width: 260px; max-width: 320px; padding: 16px 0 20px; }}
  .acc-header {{ font-size: 13.5px; padding: 14px 18px; }}
  .lesson-section {{ padding: 2.5rem; }}
  button, .footer-cta-btn, .anim-ctrl-btn, .anim-save-btn, .q-opt, .quiz-tab, .anim-tab {{
    min-height: 48px;
  }}
}}

@media print {{
  .section-nav, .page-footer, .anim-player-wrap, .quiz-section {{ display: none; }}
  .lesson-section {{ break-inside: avoid; page-break-inside: avoid; }}
}}

/* ══════════════════════════════════════════════════════════════
   ✏️  ROUGH DIAGRAM  — 2D exam-sketchable image gallery
════════════════════════════════════════════════════════════════ */
.rough-diagram-intro {{
  background: #fff7ed;
  border: 1px solid #fed7aa;
  border-radius: 12px;
  padding: 14px 18px;
  margin-bottom: 18px;
  font-size: 0.95rem;
  color: #7c2d12;
}}
.rough-diagram-intro p {{ margin: 0; }}

.rough-diagram-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}}

.rough-diagram-card {{
  display: flex;
  flex-direction: column;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  overflow: hidden;
  background: #fff;
  text-decoration: none;
  color: inherit;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}}
.rough-diagram-card:hover {{
  transform: translateY(-2px);
  box-shadow: 0 6px 18px rgba(0,0,0,0.08);
}}
.rough-diagram-card img {{
  width: 100%;
  height: 140px;
  object-fit: contain;
  background: #f9fafb;
  padding: 8px;
}}
.rough-diagram-caption {{
  font-size: 0.8rem;
  color: #4b5563;
  padding: 8px 10px;
  border-top: 1px solid #f0f0f0;
  line-height: 1.3;
}}

.rough-diagram-more {{
  font-size: 0.9rem;
  margin-top: 8px;
}}
.rough-diagram-more a,
.rough-diagram-fallback a {{
  color: #0891b2;
  font-weight: 600;
  text-decoration: none;
}}
.rough-diagram-more a:hover,
.rough-diagram-fallback a:hover {{ text-decoration: underline; }}

.rough-diagram-fallback {{
  background: #f9fafb;
  border: 1px dashed #d1d5db;
  border-radius: 12px;
  padding: 18px;
  text-align: center;
  color: #4b5563;
}}
.rough-diagram-fallback p {{ margin: 6px 0; }}

@media (max-width: 640px) {{
  .rough-diagram-grid {{ grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); }}
  .rough-diagram-card img {{ height: 110px; }}
}}
"""


# ════════════════════════════════════════════════════════════════════════
#  generate_animation  — PRIMARY BACKEND ENTRY POINT  (v19.3)
# ════════════════════════════════════════════════════════════════════════

async def generate_animation(prompt: str) -> dict:
    if not prompt or not prompt.strip():
        raise ValueError("Prompt cannot be empty")

    prompt = prompt.strip()
    log.info(f"\n{'═'*64}")
    log.info(f"[generate_animation v19.3] prompt='{prompt}'")
    log.info(f"{'═'*64}")

    subtopics_list = _extract_subtopics_from_input(prompt)

    if " -- " in prompt:
        topic = prompt.split(" -- ", 1)[0].strip()
    elif prompt.count(" - ") > 1:
        topic = prompt.split(" - ", 1)[0].strip()
    elif " - " in prompt:
        parts = prompt.split(" - ", 1)
        topic = parts[0].strip()
        if not subtopics_list:
            subtopic = parts[1].strip() if len(parts) > 1 else ""
            topic = f"{topic} — {subtopic}" if subtopic else topic
    else:
        topic = prompt

    is_specific = _is_specific_subtopic(topic)
    log.info(f"[generate_animation v19.3] topic='{topic}' | specific_subtopic={is_specific}")

    generator = UltimateLearningGenerator()
    result = await generator.generate_complete_lesson(
        topic=topic,
        include_audit=False,
        subtopics_list=subtopics_list if subtopics_list else None,
    )

    html = result["html"]

    hook_html   = result["sections"].get("hook", "")
    explanation = re.sub(r"<[^>]+>", " ", hook_html)
    explanation = " ".join(explanation.split())[:220]
    if not explanation:
        explanation = f"A complete interactive lesson on {topic}."

    log.info(f"[generate_animation v19.3] ✅ HTML={len(html):,} chars | topic='{topic}'")

    return {
        "title":          topic,
        "explanation":    explanation,
        "animation_code": html,
    }


# ════════════════════════════════════════════════════════════════════════
#  PUBLIC API FUNCTIONS
# ════════════════════════════════════════════════════════════════════════

async def generate_ultimate_learning_content(
    topic: str,
    existing_content: str = "",
    include_audit: bool = True,
    output_file: Optional[str] = None,
    subtopics_list: Optional[List[str]] = None,
) -> Dict:
    generator = UltimateLearningGenerator()
    result = await generator.generate_complete_lesson(
        topic=topic,
        existing_content=existing_content,
        include_audit=include_audit,
        subtopics_list=subtopics_list,
    )
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(result["html"])
        log.info(f"💾 Saved HTML to: {output_file}")
    return result


def generate_ultimate_learning_content_sync(
    topic: str,
    existing_content: str = "",
    include_audit: bool = True,
    output_file: Optional[str] = None,
    subtopics_list: Optional[List[str]] = None,
) -> Dict:
    return asyncio.run(
        generate_ultimate_learning_content(
            topic=topic,
            existing_content=existing_content,
            include_audit=include_audit,
            output_file=output_file,
            subtopics_list=subtopics_list,
        )
    )


# ════════════════════════════════════════════════════════════════════════
#  subtopics_json_to_genzet_args
# ════════════════════════════════════════════════════════════════════════

def subtopics_json_to_genzet_args(subtopics_json_str: str, subtopic: str) -> dict:
    try:
        data = json.loads(subtopics_json_str)
    except Exception:
        items = [s.strip() for s in str(subtopics_json_str).split(",") if s.strip()]
        return {"subtopics_list": items or [subtopic]}

    collected: list = []

    if isinstance(data, list):
        collected = [str(v) for v in data if v]
    elif isinstance(data, dict):
        sbq = data.get("subtopics_by_query", {})
        if isinstance(sbq, dict):
            for val in sbq.values():
                if isinstance(val, list):
                    collected.extend(str(v) for v in val if v)
        if not collected:
            all_sub = data.get("all_subtopics", [])
            if isinstance(all_sub, list):
                collected = [str(v) for v in all_sub if v]
        if not collected:
            for val in data.values():
                if isinstance(val, list):
                    collected.extend(str(v) for v in val if v)
                elif isinstance(val, str) and val:
                    collected.append(val)

    seen: set = set()
    unique: list = []
    for item in collected:
        if item not in seen:
            seen.add(item)
            unique.append(item)

    log.info(f"[subtopics_json_to_genzet_args] parsed {len(unique)} subtopics")
    return {"subtopics_list": unique or [subtopic]}


# ════════════════════════════════════════════════════════════════════════
#  generate_genzet_book_content  (v19.3)
# ════════════════════════════════════════════════════════════════════════

async def generate_genzet_book_content(
    topic: str,
    subtopic: str,
    pdf_context: str = "",
    subtopics_list: Optional[List[str]] = None,
) -> dict:
    topic    = (topic    or "").strip()
    subtopic = (subtopic or "").strip()

    if not topic:
        raise ValueError("topic cannot be empty")

    full_topic = (
        f"{topic} — {subtopic}"
        if subtopic and subtopic.lower() != topic.lower()
        else topic
    )

    if subtopic and _is_specific_subtopic(subtopic):
        log.info(f"[generate_genzet_book_content v19.3] specific sub-topic detected: '{subtopic}'")

    log.info(f"\n{'═'*64}")
    log.info(f"[generate_genzet_book_content v19.3] topic='{full_topic}'")
    log.info(f"[generate_genzet_book_content v19.3] pdf_context={len(pdf_context):,} chars  "
             f"subtopics={len(subtopics_list or [])}")
    log.info(f"{'═'*64}")

    subtopics_block = ""
    if subtopics_list:
        bullet_list = "\n".join(f"  • {s}" for s in subtopics_list[:20])
        subtopics_block = f"\nRelated subtopics from the textbook:\n{bullet_list}"

    existing_content = (
        f"TEXTBOOK SOURCE MATERIAL\n{'─'*40}\n"
        f"Main topic   : {topic}\n"
        f"Focus section: {subtopic}\n"
        f"{subtopics_block}\n\n"
        f"--- Extracted PDF Text ---\n"
        f"{pdf_context[:5500]}"
    )

    generator = UltimateLearningGenerator()
    result = await generator.generate_complete_lesson(
        topic=full_topic,
        existing_content=existing_content,
        include_audit=True,
        subtopics_list=subtopics_list,
    )

    html = result["html"]

    hook_html   = result["sections"].get("hook", "")
    explanation = re.sub(r"<[^>]+>", " ", hook_html)
    explanation = " ".join(explanation.split())[:220]
    if not explanation:
        explanation = f"A complete textbook-grounded lesson on {full_topic}."

    log.info(f"[generate_genzet_book_content v19.3] ✅ HTML={len(html):,} chars")

    return {
        "title":          full_topic,
        "explanation":    explanation,
        "animation_code": html,
    }


# ════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python claude_client.py <topic> [-- sub1, sub2, sub3]")
        print("       python claude_client.py <topic> [- sub1 - sub2 - sub3]")
        print("       python claude_client.py 'conduction in heat transfer'")
        print("       python claude_client.py 'total internal reflection in optical fiber'")
        sys.exit(1)

    raw_input = " ".join(sys.argv[1:])
    subtopics = _extract_subtopics_from_input(raw_input)

    if " -- " in raw_input:
        topic = raw_input.split(" -- ", 1)[0].strip()
    elif raw_input.count(" - ") > 1:
        topic = raw_input.split(" - ", 1)[0].strip()
    elif " - " in raw_input:
        topic = raw_input.split(" - ", 1)[0].strip()
    else:
        topic = raw_input

    output_file = f"ultimate_learning_{topic.replace(' ', '_').lower()}.html"

    print(f"\n{'='*64}")
    print(f"ULTIMATE LEARNING CONTENT GENERATOR  v20.0")
    print(f"{'='*64}")
    print(f"Topic             : {topic}")
    print(f"Specific sub-topic: {_is_specific_subtopic(topic)}")
    print(f"Subtopics         : {subtopics if subtopics else '(auto-detect)'}")
    print(f"Output            : {output_file}")
    print(f"\nv20.0 CHANGES:")
    print(f"  ✅ REWORKED: Working Process → Single Realistic Scene Animation")
    print(f"     • Modeled on hand-built reference animations (fibre-optic,")
    print(f"       conduction heat-bar, hybrid solar/wind system)")
    print(f"     • REMOVED forced 10-archetype topic-analysis pipeline")
    print(f"     • REMOVED rotating 'Step 1 of N' caption staging")
    print(f"     • Whole process animates simultaneously, not in stages")
    print(f"     • Scene objects look like the real thing, not boxes")
    print(f"     • Simple, realistic, easy to understand at a glance")
    print(f"     • Zero required JS — all animation via SVG native + CSS")
    print(f"{'='*64}\n")


    result = generate_ultimate_learning_content_sync(
        topic=topic,
        include_audit=True,
        output_file=output_file,
        subtopics_list=subtopics if subtopics else None,
    )

    print(f"\n{'='*64}")
    print(f"GENERATION COMPLETE")
    print(f"{'='*64}")
    meta = result["metadata"]
    print(f"Sections        : {meta['total_sections']} — {meta['sections_generated']}")
    print(f"Specific mode   : {meta.get('is_specific_subtopic', False)}")
    print(f"Total words     : {meta['total_words']:,}")
    print(f"Read time       : {meta['estimated_read_minutes']} minutes")
    print(f"HTML file       : {output_file}")
    cls = meta.get('classification', {})
    print(f"Classification  : {cls.get('category','?')} | formula={cls.get('needs_formula','?')} | deriv={cls.get('needs_derivation','?')}")
    print(f"{'='*64}\n")

    if result["audit"]:
        print(f"Core Idea : {result['audit']['core_idea']}")
