"""
╔══════════════════════════════════════════════════════════════════╗
║     claude_client.py  v21.0.0  —  EduPage Reference Style       ║
║     FULLY REFACTORED  ·  Matches reference HTML workflow         ║
╠══════════════════════════════════════════════════════════════════╣
║  v21.0 REFACTOR NOTES:                                           ║
║                                                                  ║
║  ✅ OUTPUT STYLE: Matches the reference HTML files               ║
║     (stimulus_output.html, Aerobic_Anaerobic, gravition,         ║
║     tissues) — Space Grotesk/Inter fonts, navy/blue palette,     ║
║     left slide-out nav, floating glossary, hero section,         ║
║     interactive pathway, flip-cards, activities, quiz.           ║
║                                                                  ║
║  ✅ PIPELINE: 10-section generation pipeline adapted to the      ║
║     reference HTML workflow:                                     ║
║     §1 Hero  §2 Definition  §3 Fundamentals  §4 Subtopics        ║
║     §5 Types / Classification  §6 Pathway / Working Process      ║
║     §7 Deep Concepts  §8 Real-Life / Applications                ║
║     §9 Fun Facts  §10 Activities  §11 Quiz  §12 Revision         ║
║     §13 Exam Ready   [+ optional Formulas / Derivation]          ║
║                                                                  ║
║  ✅ GENERATION: Claude generates each section's HTML content     ║
║     with topic-specific text; the shell / page structure is      ║
║     assembled by _assemble_html(), not re-generated each run.    ║
║                                                                  ║
║  ✅ CLEAN API: generate_animation(prompt) is the primary         ║
║     entry point for integration.                                 ║
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
from pathlib import Path
from typing import Optional, Dict, List

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
client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ════════════════════════════════════════════════════════════════════════
#  MODEL CONSTANTS
# ════════════════════════════════════════════════════════════════════════

MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU  = "claude-haiku-4-5-20251001"

# ════════════════════════════════════════════════════════════════════════
#  SECTION REGISTRY
# ════════════════════════════════════════════════════════════════════════

# These are the content sections Claude generates (the page shell is static).
BASE_SECTIONS: List[str] = [
    "hero",
    "definition",
    "fundamentals",
    "subtopics",
    "types",
    "pathway",
    "deep_concepts",
    "reallife",
    "funfacts",
    "activities",
    "quiz",
    "revision",
    "exam_ready",
]

CONDITIONAL_SECTIONS: List[str] = ["formulas", "derivation"]

ORDERED_SECTION_TEMPLATE: List[str] = [
    "hero",
    "definition",
    "fundamentals",
    "subtopics",
    "types",
    "pathway",
    "formulas",
    "derivation",
    "deep_concepts",
    "reallife",
    "funfacts",
    "activities",
    "quiz",
    "revision",
    "exam_ready",
]

SECTION_MODEL_MAP: Dict[str, str] = {
    "hero":          MODEL_SONNET,
    "definition":    MODEL_SONNET,
    "fundamentals":  MODEL_HAIKU,
    "subtopics":     MODEL_SONNET,
    "types":         MODEL_SONNET,
    "pathway":       MODEL_SONNET,
    "formulas":      MODEL_SONNET,
    "derivation":    MODEL_SONNET,
    "deep_concepts": MODEL_SONNET,
    "reallife":      MODEL_HAIKU,
    "funfacts":      MODEL_HAIKU,
    "activities":    MODEL_SONNET,
    "quiz":          MODEL_HAIKU,
    "revision":      MODEL_HAIKU,
    "exam_ready":    MODEL_HAIKU,
}

# ════════════════════════════════════════════════════════════════════════
#  TOPIC UTILITIES
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
        f'\n\n⚠️ SPECIFIC SUB-TOPIC — stay laser-focused on "{topic}". '
        f'Do NOT drift into the broader parent subject.\n'
    )


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
            subtopics = [s.strip() for s in parts[1].split(",") if s.strip()]

    seen: set = set()
    unique: List[str] = []
    for s in subtopics:
        if s.lower() not in seen:
            seen.add(s.lower())
            unique.append(s)
    return unique


# ════════════════════════════════════════════════════════════════════════
#  MASTER SYSTEM PROMPT
# ════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """\
You are a SENIOR EDUCATIONAL CONTENT ARCHITECT who creates interactive HTML
learning pages for students aged 13–18.

YOUR OUTPUT STYLE matches these reference files:
  • stimulus_output.html      (biology / Class 10 style)
  • Aerobic_Anaerobic.html    (comparison + flowchart style)
  • gravition.html            (physics + formulas style)
  • tissues.html              (biology classification style)

DESIGN LANGUAGE:
  • Fonts: Space Grotesk (headings) + Inter (body) from Google Fonts
  • Palette: --navy #1E3A5F, --blue #4A90D9, --mint #52C97C,
             --coral #FF6B6B, --amber #F5A623, --purple #7B61FF
  • Cards with left-coloured border and soft box-shadow
  • Interactive elements: clickable flowchart nodes, flip-cards,
    step-by-step animators, drag-and-drop / sequence activities
  • Comparison tables with navy header + alternating rows
  • Quiz: one question at a time with progress bar + explanation
  • "Did you know?" amber boxes inside subtopic cards

OUTPUT FORMAT RULES:
  1. Return ONLY valid HTML content (no markdown, no code fences)
  2. Use CSS variables matching the palette above
  3. All JavaScript must use var (not const/let) for browser compat
  4. LaTeX: $$...$$ for display equations, $...$ inline
  5. Keep prose paragraphs ≤ 3 lines each
  6. End each section response exactly at its closing </div> tag — no extra text
"""

# ════════════════════════════════════════════════════════════════════════
#  SECTION PROMPT BUILDER
# ════════════════════════════════════════════════════════════════════════

def _build_section_prompt(
    section_name: str,
    topic: str,
    context: str = "",
    subtopics_list: Optional[List[str]] = None,
    classification: Optional[Dict] = None,
) -> str:
    focus = _build_specific_focus_note(topic)
    ctx = context[:600]

    prompts: Dict[str, str] = {

        # ── §1 HERO ───────────────────────────────────────────────────
        "hero": f"""Generate the HERO section for topic: "{topic}"
{focus}
Return ONLY this HTML (replace ALL placeholders with real content):

<div class="hero-inner">
  <span class="badge">[Subject / Class level, e.g. "Class 10 Biology" or "Physics"]</span>
  <h1>[Engaging emoji] [Short punchy title about "{topic}"]</h1>
  <p>[2-3 sentence hook — a surprising fact or relatable scenario that makes "{topic}" feel exciting to a 15-year-old. Use simple language.]</p>
</div>
<svg id="hero-svg" width="180" height="120" viewBox="0 0 180 120"
     xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  [A simple animated SVG illustration relevant to "{topic}" —
   2-4 shapes/paths + 1-2 <animate> or <animateTransform> elements.
   Colors: #4A90D9, #52C97C, #F5A623. Keep it clean and minimal.]
</svg>

OUTPUT NOTHING after the closing </svg> tag.""",

        # ── §2 DEFINITION ─────────────────────────────────────────────
        "definition": f"""Generate the DEFINITION section for topic: "{topic}"
{focus}
Context: {ctx}

Return ONLY this HTML (replace ALL placeholders):

<div class="def-grid">
  <div class="def-card">
    <h4>Definition</h4>
    <p>[Plain-English definition of "{topic}" in 1-2 sentences — no jargon]</p>
  </div>
  <div class="def-card">
    <h4>Key Idea</h4>
    <p>[The single most important concept to understand about "{topic}"]</p>
  </div>
  <div class="def-card">
    <h4>Where You See It</h4>
    <p>[1-2 everyday real-world examples of "{topic}" a 15-year-old would recognize]</p>
  </div>
  <div class="def-card">
    <h4>Simple Rule</h4>
    <p>[One memorable rule, analogy, or pattern about "{topic}" — bold the key words]</p>
  </div>
</div>
<div class="obj-box">
  <h4>🎯 Learning Objectives</h4>
  <ul>
    <li>[Objective 1 — what the student will be able to define/explain]</li>
    <li>[Objective 2 — what the student will be able to identify/classify]</li>
    <li>[Objective 3 — what the student will understand about the process]</li>
    <li>[Objective 4 — what the student will be able to apply/compare]</li>
    <li>[Objective 5 — exam-relevant skill about "{topic}"]</li>
  </ul>
</div>

OUTPUT NOTHING after the closing </div> tag.""",

        # ── §3 FUNDAMENTALS ───────────────────────────────────────────
        "fundamentals": f"""Generate the FUNDAMENTALS glossary content for topic: "{topic}"
{focus}

Produce 10-14 key terms that a student must know BEFORE studying "{topic}".
Each term: a word/phrase + a 1-sentence plain-English definition.

Return ONLY this HTML (replace ALL placeholders):

<div class="glos-grid" id="glos-grid">
  <div class="glos-item"><h5>[Term 1]</h5><p>[1-sentence definition]</p></div>
  <div class="glos-item"><h5>[Term 2]</h5><p>[1-sentence definition]</p></div>
  [Continue for all 10-14 terms]
</div>

OUTPUT NOTHING after the closing </div> tag.""",

        # ── §4 SUBTOPICS ──────────────────────────────────────────────
        "subtopics": f"""Generate the SUBTOPICS GRID for topic: "{topic}"
{focus}
User-requested subtopics (generate a card for EACH, in order): {subtopics_list or '(auto-detect 6-8 key subtopics)'}
Context: {ctx}

Each card must have:
  • Title + 2-3 sentence explanation
  • 2-3 keyword chips
  • 1 "Did you know?" amber box (at least 3 out of 8 cards)

Return ONLY this HTML (replace ALL placeholders):

<div class="sub-grid">
  <div class="sub-card">
    <h4>[Subtopic name]</h4>
    <p>[2-3 sentence explanation relevant to "{topic}"]</p>
    <div class="kw-tags">
      <span class="kw">[Keyword 1]</span>
      <span class="kw">[Keyword 2]</span>
    </div>
    [Optional: <div class="dyk">💡 [Interesting "Did you know" fact]</div>]
  </div>
  [Repeat for all subtopics]
</div>

OUTPUT NOTHING after the closing </div> tag.""",

        # ── §5 TYPES / CLASSIFICATION ─────────────────────────────────
        "types": f"""Generate the TYPES / CLASSIFICATION section for topic: "{topic}"
{focus}
Context: {ctx}

Build an interactive clickable flowchart that shows the classification
tree for "{topic}". Each node is clickable and reveals an info panel below.

Return ONLY this HTML (replace ALL placeholders):

<div class="fc-wrap">
  <div class="fc-node" onclick="showFcInfo('root')">⚡ ["{topic}" root label]</div>
  <div class="fc-arrow">↓</div>
  <div class="fc-branch">
    <div class="fc-branch-col">
      <div class="fc-arrow" style="color:var(--coral)">↙</div>
      <div class="fc-sub ext" onclick="showFcInfo('type1')">[Type/Category 1 with emoji]</div>
      <div class="fc-arrow" style="font-size:1.2rem;color:var(--coral)">↓</div>
      <div style="display:flex;flex-direction:column;gap:7px;align-items:center">
        <div class="fc-sub" style="background:#e8d5ff;color:var(--navy);font-size:.8rem;min-width:130px"
             onclick="showFcInfo('sub1a')">[Sub-type 1a]</div>
        <div class="fc-sub" style="background:#e8d5ff;color:var(--navy);font-size:.8rem;min-width:130px"
             onclick="showFcInfo('sub1b')">[Sub-type 1b]</div>
      </div>
    </div>
    <div class="fc-branch-col">
      <div class="fc-arrow" style="color:var(--mint)">↘</div>
      <div class="fc-sub int" onclick="showFcInfo('type2')">[Type/Category 2 with emoji]</div>
      <div class="fc-arrow" style="font-size:1.2rem;color:var(--mint)">↓</div>
      <div style="display:flex;flex-direction:column;gap:7px;align-items:center">
        <div class="fc-sub" style="background:#d9f5e7;color:var(--navy);font-size:.8rem;min-width:130px"
             onclick="showFcInfo('sub2a')">[Sub-type 2a]</div>
        <div class="fc-sub" style="background:#d9f5e7;color:var(--navy);font-size:.8rem;min-width:130px"
             onclick="showFcInfo('sub2b')">[Sub-type 2b]</div>
      </div>
    </div>
    [Add more branch columns for additional main types if needed]
  </div>
</div>
<div class="fc-info" id="fc-info-box"></div>

<script>
var fcData = {{
  root:  {{title:'[root title]', text:'[root info text about "{topic}"]'}},
  type1: {{title:'[Type 1 name]', text:'[description of Type 1 in context of "{topic}"]'}},
  sub1a: {{title:'[Sub-type 1a]', text:'[description]'}},
  sub1b: {{title:'[Sub-type 1b]', text:'[description]'}},
  type2: {{title:'[Type 2 name]', text:'[description of Type 2 in context of "{topic}"]'}},
  sub2a: {{title:'[Sub-type 2a]', text:'[description]'}},
  sub2b: {{title:'[Sub-type 2b]', text:'[description]'}}
  [add more keys as needed]
}};
window.showFcInfo = function(key) {{
  var d = fcData[key];
  if (!d) return;
  var box = document.getElementById('fc-info-box');
  box.innerHTML = '<h4>' + d.title + '</h4><p>' + d.text + '</p>';
  box.classList.add('show');
}};
</script>

Also generate a concise comparison table (3-4 columns) for the main types below the flowchart:

<div style="overflow-x:auto;margin-top:28px">
  <table class="cmp-table">
    <thead><tr><th>Feature</th><th>[Type 1]</th><th>[Type 2]</th>[<th>[Type 3 if exists]</th>]</tr></thead>
    <tbody>
      <tr><td>[Feature 1]</td><td>[Value]</td><td>[Value]</td></tr>
      <tr><td>[Feature 2]</td><td>[Value]</td><td>[Value]</td></tr>
      <tr><td>[Feature 3]</td><td>[Value]</td><td>[Value]</td></tr>
      <tr><td>[Feature 4]</td><td>[Value]</td><td>[Value]</td></tr>
    </tbody>
  </table>
</div>

OUTPUT NOTHING after the closing </div> tag.""",

        # ── §6 PATHWAY / WORKING PROCESS ─────────────────────────────
        "pathway": f"""Generate the PATHWAY / WORKING PROCESS section for topic: "{topic}"
{focus}
Context: {ctx}

This section has TWO parts:

PART A — "Step-by-Step Path" (clickable):
Show the sequential process/pathway for "{topic}" as 5-7 clickable steps
in a horizontal or vertical chain. Each step click reveals what happens at
that stage.

PART B — "Play Animation" flow:
The same steps rendered as stacked boxes that light up one by one when
the user clicks "▶ Play Animation".

Return ONLY this HTML:

<p style="color:var(--text-soft);font-size:.93rem;margin-bottom:18px">
Click each step to learn what happens at that stage.
</p>
<div class="path-wrap" id="path-wrap">
  <div class="path-step" onclick="showPath(0)"><span class="ico">[emoji]</span><div class="lbl">[Step 1 label]</div></div>
  <div class="path-arrow">→</div>
  <div class="path-step" onclick="showPath(1)"><span class="ico">[emoji]</span><div class="lbl">[Step 2 label]</div></div>
  <div class="path-arrow">→</div>
  [Continue for all steps]
</div>
<div class="path-info" id="path-info"></div>

<h3 style="color:var(--navy);margin:24px 0 12px;font-size:1rem">⚙️ Step-by-Step Process</h3>
<p style="color:var(--text-soft);font-size:.93rem;margin-bottom:18px">
Click <strong>Play Animation</strong> to see each step light up.
</p>
<div class="steps-flow">
  <div class="step-box" id="ws0">① [Step 1 description for "{topic}"]</div><div class="step-dn">↓</div>
  <div class="step-box" id="ws1">② [Step 2 description]</div><div class="step-dn">↓</div>
  [Continue for all steps — use ③ ④ ⑤ ⑥ ⑦ etc.]
  <div class="step-box" id="ws[N]">⑦ [Final step description]</div>
</div>
<button id="play-btn" onclick="playWorkAnim()">▶ Play Animation</button>

<script>
var pathData = [
  {{label:'[Step 1]', info:'[Detailed explanation of what happens at step 1 in "{topic}"]'}},
  {{label:'[Step 2]', info:'[Detailed explanation of step 2]'}},
  [Continue for all steps]
];
window.showPath = function(i) {{
  document.querySelectorAll('.path-step').forEach(function(s,j){{s.classList.toggle('lit',j===i);}});
  var box = document.getElementById('path-info');
  box.innerHTML = pathData[i].info;
  box.classList.add('show');
}};
var _wsTotal = [TOTAL_STEP_COUNT];
var _wsIdx = 0;
var _wsTimer = null;
window.playWorkAnim = function() {{
  if (_wsTimer) {{ clearInterval(_wsTimer); _wsTimer = null; }}
  for (var k = 0; k < _wsTotal; k++) {{
    var el = document.getElementById('ws'+k);
    if (el) el.classList.remove('active-step');
  }}
  _wsIdx = 0;
  document.getElementById('play-btn').disabled = true;
  _wsTimer = setInterval(function() {{
    var el = document.getElementById('ws'+_wsIdx);
    if (el) el.classList.add('active-step');
    _wsIdx++;
    if (_wsIdx >= _wsTotal) {{
      clearInterval(_wsTimer); _wsTimer = null;
      document.getElementById('play-btn').disabled = false;
    }}
  }}, 900);
}};
</script>

OUTPUT NOTHING after the closing <script> end tag.""",

        # ── §7 FORMULAS ───────────────────────────────────────────────
        "formulas": f"""Generate the FORMULAS section for topic: "{topic}"
{focus}
Context: {ctx}

Return ONLY this HTML (2-5 formulas, each with a symbol table):

<div class="formula-stack">
  <div class="formula-card">
    <div class="formula-name">[Formula name]</div>
    <div class="formula-eq">$$[LaTeX formula]$$</div>
    <table class="sym-table">
      <tr><td class="sym-var">$$[var]$$</td><td>[Variable name and units]</td></tr>
      [repeat for each variable]
    </table>
  </div>
  [Repeat for each formula]
</div>
<div class="practice-box">
  ✏️ Practice: Rearrange each formula to solve for a different variable.
</div>

OUTPUT NOTHING after the closing </div>.""",

        # ── §8 DERIVATION ─────────────────────────────────────────────
        "derivation": f"""Generate the STEP-BY-STEP DERIVATION for topic: "{topic}"
{focus}
Context: {ctx}

Return ONLY this HTML (4-8 steps from first principles to key result):

<div class="deriv-intro">
  <p>[2-3 sentences: what equation we derive and why it matters for "{topic}"]</p>
</div>
<div class="deriv-steps">
  <div class="deriv-step">
    <div class="step-header"><span class="step-num">Step 1</span><span class="step-title">[title]</span></div>
    <div class="step-eq">$$[LaTeX]$$</div>
    <div class="step-explain">[1-2 sentence explanation]</div>
  </div>
  [Continue for all steps]
  <div class="deriv-final">
    <div class="final-label">🎯 Final Result</div>
    <div class="step-eq">$$[Final equation]$$</div>
    <div class="step-explain">[2-3 sentence meaning]</div>
  </div>
</div>

OUTPUT NOTHING after the closing </div>.""",

        # ── §9 DEEP CONCEPTS ──────────────────────────────────────────
        "deep_concepts": f"""Generate the DEEP CONCEPTS section for topic: "{topic}"
{focus}
Context: {ctx}

This is a "Differences / Comparisons" section with:
  • 1-2 comparison tables (3-4 rows, navy header)
  • 1-2 concept panels showing nuanced or advanced understanding

Return ONLY this HTML:

<h3 style="color:var(--navy);font-size:1.1rem;margin-bottom:14px">[First comparison title for "{topic}"]</h3>
<div style="overflow-x:auto;margin-bottom:28px">
  <table class="cmp-table">
    <thead><tr><th>Feature</th><th>[Concept A]</th><th>[Concept B]</th></tr></thead>
    <tbody>
      <tr><td>[Feature 1]</td><td>[Value A]</td><td>[Value B]</td></tr>
      <tr><td>[Feature 2]</td><td>[Value A]</td><td>[Value B]</td></tr>
      <tr><td>[Feature 3]</td><td>[Value A]</td><td>[Value B]</td></tr>
      <tr><td>[Feature 4]</td><td>[Value A]</td><td>[Value B]</td></tr>
      <tr><td>[Feature 5]</td><td>[Value A]</td><td>[Value B]</td></tr>
    </tbody>
  </table>
</div>

[Optional second table or concept panel with deep-dive insight]

OUTPUT NOTHING after the closing </div>.""",

        # ── §10 REAL LIFE ─────────────────────────────────────────────
        "reallife": f"""Generate the REAL-LIFE EXAMPLES section for topic: "{topic}"
{focus}

Create 6-8 flip-cards. Front shows an emoji + short scenario label.
Back shows the concept applied (Stimulus → Response style, or the
relevant mechanism for "{topic}").

Return ONLY this HTML:

<p style="color:var(--text-soft);font-size:.93rem;margin-bottom:16px">
Tap each card to see how <strong>{topic}</strong> applies!
</p>
<div class="rl-grid">
  <div class="rl-card" onclick="revealRL(this)">
    <div class="rl-front"><div class="emoji">[emoji]</div><p>[Short scenario — 3-5 words]</p></div>
    <div class="rl-back">[Explanation of "{topic}" in this scenario — 2-3 lines. Bold the key concept terms.]</div>
  </div>
  [Repeat for all 6-8 cards]
</div>

OUTPUT NOTHING after the closing </div>.""",

        # ── §11 FUN FACTS ─────────────────────────────────────────────
        "funfacts": f"""Generate FUN FACTS flip-cards for topic: "{topic}"
{focus}

Create 6 fun-fact cards. Front = intriguing question. Back = answer with
a surprising or counterintuitive fact about "{topic}".

Return ONLY this HTML:

<div class="ff-grid">
  <div class="ff-card" onclick="revealFF(this)">
    <div class="ff-front">
      <div style="font-size:1.8rem;margin-bottom:8px">[emoji]</div>
      <p class="ff-q">[Intriguing question about "{topic}" — phrased as a question]</p>
    </div>
    <div class="ff-back">[Surprising answer with key terms in <strong>bold</strong>. 2-3 sentences.]</div>
  </div>
  [Repeat for all 6 cards]
</div>

OUTPUT NOTHING after the closing </div>.""",

        # ── §12 ACTIVITIES ────────────────────────────────────────────
        "activities": f"""Generate THREE interactive activities for topic: "{topic}"
{focus}
Context: {ctx}

ACTIVITY 1 — "Identify the Concept" (scenario MCQ):
  5 scenario questions about "{topic}". User picks which option is
  the correct concept/mechanism. Show ✅/❌ feedback with explanation.

ACTIVITY 2 — "Build the Sequence" (drag-and-click ordering):
  4-6 items to place in correct order (a process, a pathway, or a
  hierarchy related to "{topic}").

ACTIVITY 3 — "Challenge Round" (sequence click game):
  User clicks items in the correct order (like a reflex arc challenge).
  Show progress bar + score.

Return ONLY this complete HTML with working JavaScript:

<div class="act-tabs">
  <button class="act-tab active" onclick="switchAct(1)">Activity 1: Identify the Concept</button>
  <button class="act-tab" onclick="switchAct(2)">Activity 2: Build the Sequence</button>
  <button class="act-tab" onclick="switchAct(3)">Activity 3: Challenge Round</button>
</div>

<!-- ACT 1 -->
<div class="act-panel active" id="act1">
  <div class="act1-scenario">
    <div class="scene" id="a1-scene"></div>
    <h4 id="a1-situation"></h4>
    <p style="font-size:.88rem;color:var(--text-soft);margin-top:6px">
      What is the <strong>[concept being identified]</strong> here?
    </p>
  </div>
  <div class="act1-opts" id="a1-opts"></div>
  <div id="act1-result"></div>
  <div style="text-align:center">
    <button id="act1-next" onclick="nextA1()" style="display:none;margin-top:10px;padding:9px 22px;border-radius:50px;background:var(--navy);color:#fff;border:none;font-weight:700">
      Next Situation →
    </button>
  </div>
</div>

<!-- ACT 2 -->
<div class="act-panel" id="act2">
  <p style="font-size:.93rem;color:var(--text-soft);margin-bottom:14px">
    Click the blocks in the correct order to build the sequence.
  </p>
  <div class="build-pool" id="build-pool"></div>
  <div style="text-align:center;margin-bottom:8px;font-size:.85rem;color:var(--text-soft)">Your sequence:</div>
  <div class="build-slots" id="build-slots"></div>
  <div style="text-align:center">
    <button id="build-check" onclick="checkBuild()" style="padding:10px 24px;border-radius:50px;background:var(--navy);color:#fff;border:none;font-weight:700;margin-right:8px">✔ Check</button>
    <button id="build-reset" onclick="resetBuild()" style="padding:10px 24px;border-radius:50px;background:var(--coral);color:#fff;border:none;font-weight:700">↺ Reset</button>
  </div>
  <div id="build-feedback" style="text-align:center;font-weight:700;font-size:1rem;margin-top:10px;min-height:28px"></div>
</div>

<!-- ACT 3 -->
<div class="act-panel" id="act3">
  <p style="font-size:.93rem;color:var(--text-soft);margin-bottom:12px">
    Click the items in the correct order. Score: <span id="arc-score">0</span> / [N]
  </p>
  <div id="arc-progress2" style="height:8px;background:var(--border);border-radius:50px;overflow:hidden;margin-bottom:14px">
    <div id="arc-bar2" style="height:100%;background:var(--mint);border-radius:50px;transition:width .4s;width:0"></div>
  </div>
  <div class="arc-challenge-steps" id="arc-ch-wrap"></div>
  <div id="arc-ch-feedback" style="text-align:center;font-weight:700;font-size:1rem;min-height:28px;margin-top:10px"></div>
  <div style="text-align:center">
    <button id="arc-ch-retry" style="display:none;margin-top:10px;padding:9px 22px;border-radius:50px;background:var(--navy);color:#fff;border:none;font-weight:700" onclick="resetArcCh()">🔄 Try Again</button>
  </div>
</div>

<script>
// ── Tab switcher ──
function switchAct(n) {{
  document.querySelectorAll('.act-panel').forEach(function(p){{p.classList.remove('active');}});
  document.querySelectorAll('.act-tab').forEach(function(t){{t.classList.remove('active');}});
  document.getElementById('act'+n).classList.add('active');
  document.querySelectorAll('.act-tab')[n-1].classList.add('active');
}}

// ── ACTIVITY 1 DATA — fill with REAL content about "{topic}" ──
var a1Data = [
  {{scene:'[emoji]', situation:'[Scenario 1 for "{topic}"]', opts:['A. [opt]','B. [opt]','C. [opt]','D. [opt]'], ans:[0/1/2/3], exp:'[Explanation]'}},
  {{scene:'[emoji]', situation:'[Scenario 2]', opts:['A. [opt]','B. [opt]','C. [opt]','D. [opt]'], ans:[0/1/2/3], exp:'[Explanation]'}},
  {{scene:'[emoji]', situation:'[Scenario 3]', opts:['A. [opt]','B. [opt]','C. [opt]','D. [opt]'], ans:[0/1/2/3], exp:'[Explanation]'}},
  {{scene:'[emoji]', situation:'[Scenario 4]', opts:['A. [opt]','B. [opt]','C. [opt]','D. [opt]'], ans:[0/1/2/3], exp:'[Explanation]'}},
  {{scene:'[emoji]', situation:'[Scenario 5]', opts:['A. [opt]','B. [opt]','C. [opt]','D. [opt]'], ans:[0/1/2/3], exp:'[Explanation]'}}
];
var a1Idx=0,a1Done=false;
function renderA1(){{
  a1Done=false;
  var d=a1Data[a1Idx%a1Data.length];
  document.getElementById('a1-scene').textContent=d.scene;
  document.getElementById('a1-situation').textContent=d.situation;
  document.getElementById('act1-result').innerHTML='';
  document.getElementById('act1-next').style.display='none';
  var optsDiv=document.getElementById('a1-opts');
  optsDiv.innerHTML='';
  d.opts.forEach(function(opt,i){{
    var btn=document.createElement('button');
    btn.className='act1-opt';btn.textContent=opt;
    btn.onclick=function(){{checkA1(i,btn,d);}};
    optsDiv.appendChild(btn);
  }});
}}
function checkA1(i,btn,d){{
  if(a1Done)return;a1Done=true;
  document.querySelectorAll('.act1-opt').forEach(function(b){{b.disabled=true;}});
  if(i===d.ans){{btn.classList.add('correct');document.getElementById('act1-result').innerHTML='✅ <span style="color:var(--mint)">Correct! '+d.exp+'</span>';}}
  else{{btn.classList.add('wrong');document.querySelectorAll('.act1-opt')[d.ans].classList.add('correct');document.getElementById('act1-result').innerHTML='❌ <span style="color:var(--coral)">Not quite. '+d.exp+'</span>';}}
  document.getElementById('act1-next').style.display='inline-block';
}}
function nextA1(){{a1Idx++;renderA1();}}
renderA1();

// ── ACTIVITY 2 DATA — fill with REAL sequence for "{topic}" ──
var buildCorrect = ['[Step 1]','[Step 2]','[Step 3]','[Step 4]','[Step 5]'];
var buildSelected=[];
function initBuild(){{
  buildSelected=[];
  var pool=document.getElementById('build-pool');
  var slots=document.getElementById('build-slots');
  document.getElementById('build-feedback').textContent='';
  var shuffled=buildCorrect.slice().sort(function(){{return Math.random()-.5;}});
  pool.innerHTML='';slots.innerHTML='';
  shuffled.forEach(function(item){{
    var btn=document.createElement('button');
    btn.className='build-block';btn.textContent=item;btn.dataset.item=item;
    btn.onclick=function(){{selectBuild(btn,item);}};
    pool.appendChild(btn);
  }});
  buildCorrect.forEach(function(_,i){{
    var slot=document.createElement('div');
    slot.className='build-slot';slot.dataset.idx=i;slot.textContent=(i+1)+'?';
    slot.onclick=function(){{removeFromSlot(slot);}};
    slots.appendChild(slot);
  }});
}}
function selectBuild(btn,item){{
  var emptySlot=document.querySelector('.build-slot:not(.filled)');
  if(!emptySlot)return;
  emptySlot.classList.add('filled');emptySlot.textContent=item;emptySlot.dataset.val=item;
  btn.classList.add('used');
}}
function removeFromSlot(slot){{
  if(!slot.classList.contains('filled'))return;
  var val=slot.dataset.val;
  document.querySelectorAll('.build-block').forEach(function(b){{if(b.dataset.item===val&&b.classList.contains('used')){{b.classList.remove('used');return;}}}});
  slot.classList.remove('filled','correct','wrong');slot.textContent=(parseInt(slot.dataset.idx)+1)+'?';delete slot.dataset.val;
}}
function checkBuild(){{
  var slots=document.querySelectorAll('.build-slot');
  var allFilled=true,allCorrect=true;
  slots.forEach(function(slot,i){{
    if(!slot.classList.contains('filled')){{allFilled=false;return;}}
    if(slot.dataset.val===buildCorrect[i])slot.classList.add('correct');
    else{{slot.classList.add('wrong');allCorrect=false;}}
  }});
  if(!allFilled){{document.getElementById('build-feedback').innerHTML='<span style="color:var(--coral)">Fill all slots first!</span>';return;}}
  document.getElementById('build-feedback').innerHTML=allCorrect?'🎉 <span style="color:var(--mint)">Perfect sequence!</span>':'<span style="color:var(--coral)">Some steps are wrong — reset and try again!</span>';
}}
function resetBuild(){{initBuild();}}
initBuild();

// ── ACTIVITY 3 DATA — fill with REAL ordered items for "{topic}" ──
var arcChOrder=['[Item 1]','[Item 2]','[Item 3]','[Item 4]','[Item 5]','[Item 6]'];
var arcChCurrent=0,arcChScore=0;
function initArcCh(){{
  arcChCurrent=0;arcChScore=0;
  document.getElementById('arc-score').textContent='0';
  document.getElementById('arc-bar2').style.width='0';
  document.getElementById('arc-ch-feedback').textContent='';
  document.getElementById('arc-ch-retry').style.display='none';
  var wrap=document.getElementById('arc-ch-wrap');
  var shuffled=arcChOrder.slice().sort(function(){{return Math.random()-.5;}});
  wrap.innerHTML='';
  shuffled.forEach(function(step){{
    var btn=document.createElement('button');
    btn.className='arc-ch-btn';btn.textContent=step;
    btn.onclick=function(){{clickArcCh(btn,step);}};
    wrap.appendChild(btn);
  }});
}}
function clickArcCh(btn,step){{
  if(btn.classList.contains('correct'))return;
  if(step===arcChOrder[arcChCurrent]){{
    btn.classList.add('correct');arcChCurrent++;arcChScore++;
    document.getElementById('arc-score').textContent=arcChScore;
    document.getElementById('arc-bar2').style.width=(arcChCurrent/arcChOrder.length*100)+'%';
    if(arcChCurrent===arcChOrder.length){{
      document.getElementById('arc-ch-feedback').innerHTML='🏆 <span style="color:var(--mint)">Excellent! Full sequence complete! Score: '+arcChScore+'/'+arcChOrder.length+'</span>';
      document.getElementById('arc-ch-retry').style.display='inline-block';
    }}else{{
      document.getElementById('arc-ch-feedback').innerHTML='✅ Next: <strong>'+arcChOrder[arcChCurrent]+'</strong>';
    }}
  }}else{{
    btn.classList.add('wrong-ans');
    setTimeout(function(){{btn.classList.remove('wrong-ans');}},400);
    document.getElementById('arc-ch-feedback').innerHTML='❌ <span style="color:var(--coral)">Wrong order! Next should be: <strong>'+arcChOrder[arcChCurrent]+'</strong></span>';
  }}
}}
function resetArcCh(){{initArcCh();}}
initArcCh();
</script>

CRITICAL: Replace ALL [placeholder] values with REAL content about "{topic}".
Replace [N] with the actual count of arcChOrder items.
OUTPUT NOTHING after the closing </script> tag.""",

        # ── §13 QUIZ ──────────────────────────────────────────────────
        "quiz": f"""Generate a 10-question quiz about topic: "{topic}"
{focus}
Context: {ctx}

Rules:
  • 10 MCQ questions, difficulty rising: Q1-3 Easy, Q4-7 Medium, Q8-10 Hard
  • 3-4 options per question; exactly ONE correct answer
  • Include explanation text for each question
  • Use the one-at-a-time display style with progress bar

Return ONLY this complete HTML with working JavaScript:

<div id="quiz-wrap">
  <div id="quiz-progress"><div id="quiz-bar"></div></div>
  <div class="quiz-qnum" id="quiz-qnum">Question 1 of 10</div>
  <div class="quiz-q" id="quiz-q"></div>
  <div class="quiz-opts" id="quiz-opts"></div>
  <div id="quiz-exp"></div>
  <button id="quiz-next" onclick="nextQuiz()" style="display:none;margin-top:14px;padding:11px 28px;border-radius:50px;background:var(--navy);color:#fff;border:none;font-weight:700;font-size:.95rem">Next Question →</button>
</div>
<div id="quiz-result" style="display:none;text-align:center">
  <div class="score-big" id="quiz-score-big"></div>
  <div class="score-msg" id="quiz-score-msg"></div>
  <button onclick="resetQuiz()" style="padding:12px 30px;border-radius:50px;background:var(--navy);color:#fff;border:none;font-weight:700;font-size:1rem">🔄 Try Again</button>
</div>

<script>
var quizData=[
  {{q:'[Question 1 — Easy, about "{topic}"]',opts:['[A]','[B]','[C]','[D]'],ans:[0/1/2/3],exp:'[Explanation why the correct answer is correct]'}},
  {{q:'[Question 2 — Easy]',opts:['[A]','[B]','[C]'],ans:[0/1/2],exp:'[Explanation]'}},
  {{q:'[Question 3 — Easy]',opts:['[A]','[B]','[C]','[D]'],ans:[0/1/2/3],exp:'[Explanation]'}},
  {{q:'[Question 4 — Medium]',opts:['[A]','[B]','[C]','[D]'],ans:[0/1/2/3],exp:'[Explanation]'}},
  {{q:'[Question 5 — Medium]',opts:['[A]','[B]','[C]','[D]'],ans:[0/1/2/3],exp:'[Explanation]'}},
  {{q:'[Question 6 — Medium]',opts:['[A]','[B]','[C]','[D]'],ans:[0/1/2/3],exp:'[Explanation]'}},
  {{q:'[Question 7 — Medium]',opts:['[A]','[B]','[C]','[D]'],ans:[0/1/2/3],exp:'[Explanation]'}},
  {{q:'[Question 8 — Hard]',opts:['[A]','[B]','[C]','[D]'],ans:[0/1/2/3],exp:'[Explanation]'}},
  {{q:'[Question 9 — Hard]',opts:['[A]','[B]','[C]','[D]'],ans:[0/1/2/3],exp:'[Explanation]'}},
  {{q:'[Question 10 — Hard]',opts:['[A]','[B]','[C]','[D]'],ans:[0/1/2/3],exp:'[Explanation]'}}
];
var qIdx=0,qScore=0;
function renderQuiz(){{
  var d=quizData[qIdx];
  document.getElementById('quiz-qnum').textContent='Question '+(qIdx+1)+' of 10';
  document.getElementById('quiz-bar').style.width=(qIdx/10*100)+'%';
  document.getElementById('quiz-q').textContent=d.q;
  document.getElementById('quiz-exp').style.display='none';
  document.getElementById('quiz-next').style.display='none';
  var optsDiv=document.getElementById('quiz-opts');
  optsDiv.innerHTML='';
  d.opts.forEach(function(opt,i){{
    var btn=document.createElement('button');
    btn.className='quiz-opt';btn.textContent=opt;
    btn.onclick=function(){{answerQuiz(i,btn);}};
    optsDiv.appendChild(btn);
  }});
}}
function answerQuiz(i,btn){{
  var d=quizData[qIdx];
  document.querySelectorAll('.quiz-opt').forEach(function(b){{b.disabled=true;}});
  if(i===d.ans){{btn.classList.add('correct');qScore++;}}
  else{{btn.classList.add('wrong');document.querySelectorAll('.quiz-opt')[d.ans].classList.add('correct');}}
  var exp=document.getElementById('quiz-exp');
  exp.textContent='💡 '+d.exp;exp.style.display='block';
  document.getElementById('quiz-next').style.display='inline-block';
}}
function nextQuiz(){{
  qIdx++;
  if(qIdx>=10)showQuizResult();
  else renderQuiz();
}}
function showQuizResult(){{
  document.getElementById('quiz-bar').style.width='100%';
  document.getElementById('quiz-wrap').style.display='none';
  document.getElementById('quiz-result').style.display='block';
  document.getElementById('quiz-score-big').textContent=qScore+'/10';
  var msg='';
  if(qScore>=9)msg='🌟 Outstanding! You are a "{topic}" expert!';
  else if(qScore>=7)msg='👍 Very Good! Keep it up!';
  else if(qScore>=5)msg='😊 Good effort! Review once more.';
  else msg='📚 Keep practising — you have got this!';
  document.getElementById('quiz-score-msg').textContent=msg;
}}
function resetQuiz(){{
  qIdx=0;qScore=0;
  document.getElementById('quiz-wrap').style.display='block';
  document.getElementById('quiz-result').style.display='none';
  renderQuiz();
}}
renderQuiz();
</script>

CRITICAL: Replace ALL [placeholder] values (question text, options, ans index, explanation)
with real, accurate content about "{topic}". ans must be 0-based integer.
OUTPUT NOTHING after the closing </script> tag.""",

        # ── §14 REVISION ──────────────────────────────────────────────
        "revision": f"""Generate a QUICK REVISION section for topic: "{topic}"
{focus}

Produce 8-12 revision points — each is a bold "term:" followed by a 1-sentence summary.

Return ONLY this HTML:

<div class="rev-grid">
  <div class="rev-item"><strong>[Term 1]:</strong> [1-sentence summary about "{topic}"]</div>
  <div class="rev-item"><strong>[Term 2]:</strong> [1-sentence summary]</div>
  [Continue for all 8-12 items]
</div>

OUTPUT NOTHING after the closing </div>.""",

        # ── §15 EXAM READY ────────────────────────────────────────────
        "exam_ready": f"""Generate an EXAM READY section for topic: "{topic}"
{focus}

Produce 4 boxes:
  1. "Must-Know Facts" (5-6 bullet points — exam essentials)
  2. "Common Mistakes" (4-5 bullet points — what students get wrong)
  3. "Key Formulas / Definitions" (3-4 items — only if applicable)
  4. "Exam Tips" (3-4 bullet points — strategy and wording advice)

Return ONLY this HTML:

<div class="exam-grid">
  <div class="exam-box">
    <h4>✅ Must-Know Facts</h4>
    <ul>
      <li>[Fact 1 about "{topic}" — essential for exam]</li>
      <li>[Fact 2]</li>
      <li>[Fact 3]</li>
      <li>[Fact 4]</li>
      <li>[Fact 5]</li>
    </ul>
  </div>
  <div class="exam-box warn">
    <h4>⚠️ Common Mistakes</h4>
    <ul>
      <li>[Mistake 1 students make about "{topic}"]</li>
      <li>[Mistake 2]</li>
      <li>[Mistake 3]</li>
      <li>[Mistake 4]</li>
    </ul>
  </div>
  <div class="exam-box">
    <h4>📐 Key Formulas / Definitions</h4>
    <ul>
      <li>[Formula or definition 1 — if topic is mathematical; else replace with a "Key term: definition" point]</li>
      <li>[Formula or definition 2]</li>
      <li>[Formula or definition 3]</li>
    </ul>
  </div>
  <div class="exam-box">
    <h4>💡 Exam Tips</h4>
    <ul>
      <li>[Exam strategy tip 1 for "{topic}"]</li>
      <li>[Tip 2]</li>
      <li>[Tip 3]</li>
      <li>[Tip 4]</li>
    </ul>
  </div>
</div>

OUTPUT NOTHING after the closing </div>.""",
    }

    return prompts.get(section_name, f"Generate content for section '{section_name}' about topic '{topic}'.")


# ════════════════════════════════════════════════════════════════════════
#  STRIP VERIFICATION TAIL
# ════════════════════════════════════════════════════════════════════════

def _strip_tail(html: str) -> str:
    """Remove any verification/commentary block after the last HTML closing tag."""
    last_close = -1
    for tag in ('</div>', '</script>', '</table>', '</ul>', '</ol>', '</section>'):
        idx = html.rfind(tag)
        if idx != -1:
            candidate = idx + len(tag)
            if candidate > last_close:
                last_close = candidate
    if last_close == -1:
        return html
    tail = html[last_close:]
    markers = ['###', 'Verification', 'Requirement', '| ---', '✅', '**', 'Status |']
    if tail.strip() and any(m in tail for m in markers):
        return html[:last_close]
    return html


# ════════════════════════════════════════════════════════════════════════
#  HTML PAGE SHELL + CSS
# ════════════════════════════════════════════════════════════════════════

def _get_page_css(topic: str) -> str:
    """Returns the full CSS matching the reference HTML style."""
    return """
/* ════════════════════════════════════════════════════════
   EduPage CSS  — matches reference HTML style
   Fonts: Space Grotesk (headings) + Inter (body)
   Palette: navy / blue / mint / coral / amber / purple
════════════════════════════════════════════════════════ */
:root{
  --bg:#F0F4FF;--bg2:#E8EEF8;--card:#FFFFFF;
  --navy:#1E3A5F;--blue:#4A90D9;--blue-light:#D6E9F8;
  --amber:#F5A623;--amber-light:#FEF3DC;
  --mint:#52C97C;--mint-light:#D9F5E7;
  --coral:#FF6B6B;--coral-light:#FFE8E8;
  --purple:#7B61FF;--purple-light:#EDE9FF;
  --text:#1E3A5F;--text-soft:#5A7290;--text-xs:#8BA0B5;
  --border:#D0DCF0;--shadow:0 2px 20px rgba(30,58,95,0.08);
  --r:14px;--r-sm:9px;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);
     color:var(--text);font-size:17px;line-height:1.7;overflow-x:hidden}
h1,h2,h3,h4,h5{font-family:'Space Grotesk',sans-serif;line-height:1.3}
a{color:var(--blue);text-decoration:none}
button{cursor:pointer;font-family:'Inter',sans-serif}
img{max-width:100%;height:auto}

/* ── LEFT NAV TOGGLE ── */
#nav-btn{position:fixed;left:0;top:50%;transform:translateY(-50%);z-index:1100;
  background:var(--navy);color:#fff;border:none;padding:16px 8px;
  border-radius:0 12px 12px 0;writing-mode:vertical-rl;
  font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:13px;
  letter-spacing:1px;box-shadow:3px 0 16px rgba(30,58,95,0.22);transition:background .2s}
#nav-btn:hover{background:var(--blue)}
#side-nav{position:fixed;left:-260px;top:0;height:100vh;width:260px;
  background:#fff;z-index:1099;border-right:2px solid var(--border);
  box-shadow:4px 0 32px rgba(30,58,95,0.12);
  transition:left .35s cubic-bezier(.4,0,.2,1);
  display:flex;flex-direction:column;overflow:hidden}
#side-nav.open{left:0}
#side-nav .nav-head{padding:20px 18px 14px;border-bottom:2px solid var(--border);
  display:flex;align-items:center;justify-content:space-between}
#side-nav .nav-head h3{font-size:15px;color:var(--navy)}
#nav-close{background:none;border:none;font-size:20px;color:var(--text-soft)}
#side-nav nav{overflow-y:auto;flex:1;padding:8px 0}
#side-nav nav a{display:block;padding:9px 20px;font-size:13.5px;font-weight:600;
  color:var(--text-soft);border-left:3px solid transparent;
  transition:all .18s;text-decoration:none}
#side-nav nav a:hover,#side-nav nav a.active{color:var(--navy);background:var(--bg);border-left-color:var(--blue)}

/* ── LAYOUT ── */
#content{max-width:920px;margin:0 auto;padding:28px 24px 60px}
section{margin-bottom:52px;scroll-margin-top:24px}
.sec-title{font-size:1.55rem;color:var(--navy);margin-bottom:20px;display:flex;align-items:center;gap:10px}
.sec-title .ic{font-size:1.4rem}
.card{background:var(--card);border-radius:var(--r);box-shadow:var(--shadow);padding:24px;border:1px solid var(--border)}
.badge{display:inline-block;padding:4px 14px;border-radius:50px;font-size:.8rem;font-weight:700}

/* ── HERO ── */
#hero{background:linear-gradient(135deg,var(--navy) 0%,#2A5080 60%,#1A6EAB 100%);
  border-radius:22px;padding:44px 36px;color:#fff;position:relative;
  overflow:hidden;margin-bottom:52px}
#hero::before{content:'';position:absolute;top:-60px;right:-60px;width:220px;height:220px;
  border-radius:50%;background:rgba(74,144,217,.15)}
#hero .badge{background:var(--amber);color:#333;margin-bottom:14px}
#hero h1{font-size:2.3rem;margin-bottom:12px;position:relative}
#hero p{font-size:1.15rem;opacity:.92;max-width:580px;position:relative}
#hero-svg{position:absolute;right:32px;top:50%;transform:translateY(-50%);opacity:.4}

/* ── DEFINITION ── */
.def-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:18px}
.def-card{background:var(--bg);border-radius:var(--r-sm);padding:18px;border-left:4px solid var(--blue)}
.def-card h4{color:var(--navy);font-size:1rem;margin-bottom:6px}
.def-card p{font-size:.92rem;color:var(--text-soft)}
.obj-box{background:var(--amber-light);border-radius:var(--r-sm);padding:16px 20px;border-left:4px solid var(--amber)}
.obj-box h4{color:#a0620a;font-size:.98rem;margin-bottom:8px}
.obj-box ul{list-style:none}
.obj-box li{font-size:.9rem;color:var(--text);margin-bottom:4px}
.obj-box li::before{content:"✔ ";color:var(--mint);font-weight:800}

/* ── FUNDAMENTALS ── */
#fund-btn{background:var(--navy);color:#fff;border:none;padding:12px 26px;
  border-radius:50px;font-weight:700;font-size:1rem;
  display:inline-flex;align-items:center;gap:9px;
  box-shadow:0 4px 18px rgba(30,58,95,.22);transition:background .2s}
#fund-btn:hover{background:var(--blue)}
#fund-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.42);z-index:2000;
  align-items:center;justify-content:center}
#fund-overlay.open{display:flex}
#fund-panel{background:#fff;border-radius:20px;padding:30px;max-width:600px;
  width:94%;max-height:82vh;overflow-y:auto;position:relative;
  box-shadow:0 12px 56px rgba(30,58,95,.22)}
#fund-panel h3{color:var(--navy);font-size:1.2rem;margin-bottom:6px}
#fund-search{width:100%;padding:9px 14px;border:2px solid var(--border);
  border-radius:var(--r-sm);font-size:.95rem;margin:12px 0 16px;
  font-family:'Inter',sans-serif;outline:none}
#fund-search:focus{border-color:var(--blue)}
.fund-close{position:absolute;top:14px;right:16px;background:none;border:none;
  font-size:22px;color:var(--text-soft)}
.glos-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.glos-item{background:var(--bg);border-radius:var(--r-sm);padding:13px 15px}
.glos-item h5{color:var(--navy);font-size:.92rem;margin-bottom:4px}
.glos-item p{font-size:.82rem;color:var(--text-soft);line-height:1.5}

/* ── FLOATING GLOSSARY ── */
#float-glos{position:fixed;right:18px;bottom:24px;z-index:1000}
#float-btn{background:var(--blue);color:#fff;border:none;padding:12px 20px;
  border-radius:50px;font-weight:700;font-size:.92rem;
  box-shadow:0 4px 20px rgba(74,144,217,.4);transition:background .2s}
#float-btn:hover{background:var(--navy)}
#float-panel{display:none;position:fixed;right:18px;bottom:78px;width:280px;
  max-height:400px;overflow-y:auto;background:#fff;border-radius:var(--r);
  box-shadow:0 8px 40px rgba(30,58,95,.18);border:1px solid var(--border);
  z-index:1001;padding:18px}
#float-panel.open{display:block}
#float-panel h4{color:var(--navy);font-size:1rem;margin-bottom:12px}
.fp-close{position:absolute;top:12px;right:14px;background:none;border:none;
  font-size:18px;color:var(--text-soft);cursor:pointer}
.fp-item{margin-bottom:9px;border-bottom:1px solid var(--border);padding-bottom:8px}
.fp-item:last-child{border-bottom:none}
.fp-item strong{color:var(--navy);font-size:.88rem}
.fp-item p{font-size:.8rem;color:var(--text-soft);margin-top:2px}

/* ── SUBTOPICS ── */
.sub-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px}
.sub-card{background:#fff;border-radius:var(--r-sm);padding:18px;
  border:1px solid var(--border);border-top:4px solid var(--blue);
  box-shadow:var(--shadow);transition:transform .2s,box-shadow .2s}
.sub-card:hover{transform:translateY(-3px);box-shadow:0 6px 28px rgba(30,58,95,.12)}
.sub-card h4{color:var(--navy);font-size:1rem;margin-bottom:7px}
.sub-card p{font-size:.87rem;color:var(--text-soft);line-height:1.55}
.sub-card .dyk{margin-top:10px;background:var(--amber-light);border-radius:6px;
  padding:7px 10px;font-size:.8rem;color:#a0620a}
.kw-tags{margin-top:9px;display:flex;flex-wrap:wrap;gap:5px}
.kw{background:var(--blue-light);color:var(--navy);border-radius:50px;
  padding:2px 10px;font-size:.75rem;font-weight:700}

/* ── TYPES FLOWCHART ── */
.fc-wrap{display:flex;flex-direction:column;align-items:center;gap:0}
.fc-node{background:var(--navy);color:#fff;padding:12px 30px;
  border-radius:var(--r-sm);font-weight:700;font-size:1rem;text-align:center;
  min-width:190px;cursor:pointer;transition:background .2s}
.fc-node:hover{background:var(--blue)}
.fc-arrow{color:var(--blue);font-size:1.9rem;line-height:.9;margin:1px 0}
.fc-branch{display:flex;gap:36px;margin-top:2px;flex-wrap:wrap;justify-content:center}
.fc-branch-col{display:flex;flex-direction:column;align-items:center}
.fc-sub{background:var(--blue);color:#fff;padding:9px 20px;border-radius:var(--r-sm);
  font-size:.88rem;font-weight:700;min-width:150px;text-align:center;
  cursor:pointer;transition:background .2s}
.fc-sub:hover,.fc-sub.active{background:var(--navy)}
.fc-sub.ext{background:var(--coral)}
.fc-sub.int{background:var(--mint);color:var(--navy)}
.fc-info{margin-top:20px;background:var(--blue-light);border-radius:var(--r-sm);
  padding:16px 20px;display:none}
.fc-info.show{display:block}
.fc-info h4{color:var(--navy);font-size:1rem;margin-bottom:6px}
.fc-info p{font-size:.92rem;color:var(--text-soft)}

/* ── PATHWAY ── */
.path-wrap{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;
  gap:0;margin:20px 0}
.path-step{background:#fff;border:2px solid var(--border);border-radius:var(--r-sm);
  padding:14px 18px;text-align:center;cursor:pointer;transition:all .3s;
  min-width:110px;position:relative}
.path-step:hover{border-color:var(--blue);transform:translateY(-2px)}
.path-step.lit{background:var(--blue);border-color:var(--blue);color:#fff}
.path-step .ico{font-size:1.8rem;display:block;margin-bottom:4px}
.path-step .lbl{font-size:.8rem;font-weight:700}
.path-arrow{font-size:1.5rem;color:var(--blue);padding:0 4px;flex-shrink:0}
.path-info{background:var(--navy);color:#fff;border-radius:var(--r-sm);
  padding:14px 18px;margin-top:14px;font-size:.92rem;min-height:54px;display:none}
.path-info.show{display:block}

/* ── WORKING PROCESS ── */
.steps-flow{display:flex;flex-direction:column;align-items:center;gap:0}
.step-box{background:#fff;border:2px solid var(--border);border-radius:var(--r-sm);
  padding:13px 24px;text-align:center;min-width:280px;font-weight:600;
  font-size:.96rem;transition:background .3s,border-color .3s}
.step-box.active-step{background:var(--blue);border-color:var(--blue);color:#fff}
.step-dn{color:var(--blue);font-size:1.9rem;line-height:.9}
#play-btn{margin:20px auto 0;display:block;background:var(--mint);color:var(--navy);
  border:none;padding:11px 28px;border-radius:50px;font-weight:700;font-size:1rem;
  transition:background .2s}
#play-btn:hover{background:#3ab867}

/* ── COMPARISON TABLE ── */
.cmp-table{width:100%;border-collapse:collapse;border-radius:12px;overflow:hidden;box-shadow:var(--shadow)}
.cmp-table th{background:var(--navy);color:#fff;padding:13px 16px;font-size:.98rem}
.cmp-table th:first-child{background:#264653}
.cmp-table td{padding:11px 16px;font-size:.9rem;border-bottom:1px solid var(--border)}
.cmp-table tr:nth-child(even) td{background:var(--bg)}
.cmp-table td:first-child{font-weight:700;color:var(--navy);background:#f9fbff}
.cmp-table tr:hover td{background:var(--blue-light)}

/* ── REAL-LIFE CARDS ── */
.rl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:15px}
.rl-card{background:var(--navy);color:#fff;border-radius:var(--r-sm);padding:18px;
  text-align:center;cursor:pointer;min-height:120px;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  transition:transform .2s;box-shadow:var(--shadow)}
.rl-card:hover{transform:translateY(-3px)}
.rl-front .emoji{font-size:2.2rem;margin-bottom:8px}
.rl-front p{font-size:.88rem;font-weight:700;opacity:.88}
.rl-back{display:none;font-size:.88rem;line-height:1.6}
.rl-card.revealed .rl-front{display:none}
.rl-card.revealed .rl-back{display:block}
.rl-card.revealed{background:var(--amber);color:var(--text)}

/* ── FUN FACTS ── */
.ff-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}
.ff-card{background:var(--purple);color:#fff;border-radius:var(--r-sm);padding:20px;
  text-align:center;cursor:pointer;min-height:130px;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  transition:transform .2s;box-shadow:var(--shadow)}
.ff-card:hover{transform:translateY(-3px)}
.ff-front .ff-q{font-size:.88rem;font-weight:700;opacity:.9;line-height:1.5}
.ff-back{display:none;font-size:.88rem;line-height:1.6}
.ff-card.revealed .ff-front{display:none}
.ff-card.revealed .ff-back{display:block}
.ff-card.revealed{background:var(--mint-light);color:var(--text)}

/* ── ACTIVITIES ── */
.act-tabs{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}
.act-tab{padding:9px 22px;border-radius:50px;border:2px solid var(--blue);
  background:none;font-weight:700;font-size:.92rem;color:var(--navy);transition:all .2s}
.act-tab.active,.act-tab:hover{background:var(--navy);color:#fff;border-color:var(--navy)}
.act-panel{display:none}
.act-panel.active{display:block}
.act1-scenario{background:var(--bg);border-radius:var(--r-sm);padding:20px;
  text-align:center;margin-bottom:16px}
.act1-scenario .scene{font-size:3rem;margin-bottom:10px}
.act1-scenario h4{font-size:1.05rem;color:var(--navy);margin-bottom:6px}
.act1-opts{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.act1-opt{background:#fff;border:2px solid var(--border);border-radius:var(--r-sm);
  padding:13px;text-align:center;font-weight:700;font-size:.95rem;transition:all .2s}
.act1-opt:hover{border-color:var(--blue);background:var(--blue-light)}
.act1-opt.correct{background:var(--mint-light);border-color:var(--mint);color:#1a7a42}
.act1-opt.wrong{background:var(--coral-light);border-color:var(--coral);color:#c0392b}
.build-pool{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px;justify-content:center}
.build-block{background:var(--blue-light);border:2px solid var(--blue);
  border-radius:var(--r-sm);padding:9px 18px;font-weight:700;font-size:.92rem;
  cursor:pointer;transition:all .2s;user-select:none}
.build-block:hover{background:var(--blue);color:#fff}
.build-block.used{opacity:.35;pointer-events:none}
.build-slots{display:flex;flex-wrap:wrap;gap:10px;justify-content:center;margin-bottom:14px}
.build-slot{background:#fff;border:2px dashed var(--border);border-radius:var(--r-sm);
  padding:9px 18px;min-width:110px;text-align:center;font-weight:700;
  font-size:.88rem;color:var(--text-soft);cursor:pointer}
.build-slot.filled{border-style:solid;border-color:var(--blue);color:var(--navy);background:var(--blue-light)}
.build-slot.correct{border-color:var(--mint);background:var(--mint-light);color:#1a7a42}
.build-slot.wrong{border-color:var(--coral);background:var(--coral-light);color:#c0392b}
.arc-challenge-steps{display:flex;flex-wrap:wrap;gap:10px;justify-content:center;margin:14px 0}
.arc-ch-btn{background:#fff;border:2px solid var(--border);border-radius:var(--r-sm);
  padding:10px 16px;font-weight:700;font-size:.88rem;cursor:pointer;transition:all .2s}
.arc-ch-btn:hover{border-color:var(--purple)}
.arc-ch-btn.correct{background:var(--mint);border-color:var(--mint);color:#fff;pointer-events:none}
.arc-ch-btn.wrong-ans{animation:shake .3s;border-color:var(--coral);background:var(--coral-light)}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-6px)}75%{transform:translateX(6px)}}

/* ── FORMULAS ── */
.formula-stack{display:flex;flex-direction:column;gap:20px;margin-bottom:20px}
.formula-card{background:#fff;border:1px solid var(--border);border-radius:var(--r);
  padding:20px;border-left:4px solid var(--purple)}
.formula-name{font-family:'Space Grotesk',sans-serif;font-size:1.05rem;
  font-weight:700;color:var(--purple);margin-bottom:12px}
.formula-eq{background:var(--purple-light);border-radius:var(--r-sm);padding:18px;
  text-align:center;font-size:1.2rem;margin:12px 0;overflow-x:auto}
.sym-table{width:100%;border-collapse:collapse;margin-top:8px}
.sym-table tr{border-bottom:1px solid var(--border)}
.sym-table td{padding:6px 10px;font-size:.88rem}
.sym-var{font-family:monospace;color:var(--purple);font-weight:700;width:80px}
.practice-box{background:var(--amber-light);border-radius:var(--r-sm);padding:14px 18px;
  font-weight:700;font-size:.9rem;text-align:center;color:#a0620a}

/* ── DERIVATION ── */
.deriv-intro{background:var(--mint-light);border-left:4px solid var(--mint);
  border-radius:0 var(--r-sm) var(--r-sm) 0;padding:14px 18px;margin-bottom:20px}
.deriv-steps{display:flex;flex-direction:column;gap:14px}
.deriv-step{background:#fff;border:2px solid var(--border);border-radius:var(--r-sm);padding:18px}
.step-header{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.step-num{background:var(--mint);color:#fff;border-radius:50px;padding:4px 12px;
  font-size:.82rem;font-weight:800}
.step-title{font-weight:700;color:var(--navy);font-size:1rem}
.step-eq{background:var(--mint-light);border-radius:var(--r-sm);padding:14px;
  text-align:center;font-size:1.1rem;margin:10px 0;overflow-x:auto}
.step-explain{font-size:.9rem;color:var(--text-soft);line-height:1.6}
.deriv-final{background:linear-gradient(135deg,#14532d,#166534);border-radius:var(--r);
  padding:22px;text-align:center;color:#fff}
.final-label{font-size:13px;font-weight:800;color:#86efac;margin-bottom:12px;
  text-transform:uppercase;letter-spacing:.5px}

/* ── QUIZ ── */
#quiz-progress{height:8px;background:var(--border);border-radius:50px;overflow:hidden;margin-bottom:16px}
#quiz-bar{height:100%;background:var(--blue);border-radius:50px;transition:width .4s;width:0}
.quiz-qnum{font-size:.88rem;color:var(--text-soft);margin-bottom:8px;font-weight:600}
.quiz-q{font-size:1.1rem;font-weight:700;color:var(--text);margin-bottom:18px}
.quiz-opts{display:flex;flex-direction:column;gap:10px}
.quiz-opt{background:#fff;border:2px solid var(--border);border-radius:var(--r-sm);
  padding:12px 18px;font-size:.95rem;font-weight:600;text-align:left;
  transition:all .2s;color:var(--text)}
.quiz-opt:hover{border-color:var(--blue);background:var(--blue-light)}
.quiz-opt.correct{background:var(--mint-light);border-color:var(--mint);color:#1a7a42}
.quiz-opt.wrong{background:var(--coral-light);border-color:var(--coral);color:#c0392b}
.quiz-opt:disabled{cursor:default}
#quiz-exp{margin-top:12px;padding:12px 16px;border-radius:var(--r-sm);
  background:var(--amber-light);color:var(--text);font-size:.9rem;display:none}
.score-big{font-size:4rem;font-weight:800;font-family:'Space Grotesk',sans-serif;
  color:var(--navy);margin-top:24px}
.score-msg{font-size:1.15rem;font-weight:700;margin:10px 0 22px;color:var(--text-soft)}

/* ── REVISION ── */
.rev-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.rev-item{background:var(--bg);border-radius:var(--r-sm);padding:14px 16px;
  border-left:4px solid var(--blue);font-size:.93rem}
.rev-item strong{color:var(--navy);display:block;margin-bottom:3px}

/* ── EXAM READY ── */
.exam-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.exam-box{background:#fff;border-radius:var(--r-sm);padding:18px;border:1px solid var(--border)}
.exam-box h4{color:var(--navy);font-size:1rem;margin-bottom:12px;
  padding-bottom:8px;border-bottom:2px solid var(--border)}
.exam-box ul{list-style:none}
.exam-box ul li{font-size:.88rem;margin-bottom:7px;color:var(--text-soft)}
.exam-box ul li::before{content:"▸ ";color:var(--blue);font-weight:800}
.exam-box.warn{border-color:var(--coral)}
.exam-box.warn h4{color:var(--coral)}
.exam-box.warn li::before{color:var(--coral)}

/* ── RESPONSIVE ── */
@media(max-width:700px){
  #content{padding:16px 14px}
  .def-grid,.exam-grid{grid-template-columns:1fr}
  #hero h1{font-size:1.55rem}
  #hero{padding:28px 20px}
  #hero-svg{display:none}
  .glos-grid{grid-template-columns:1fr}
  .path-wrap{gap:4px}
  .path-step{min-width:80px;padding:10px 8px}
  .act1-opts{grid-template-columns:1fr}
  .fc-branch{gap:16px}
}
@media(max-width:480px){
  .cmp-table th,.cmp-table td{padding:8px 10px;font-size:.82rem}
}
"""


def _assemble_html(
    topic: str,
    sections: Dict[str, str],
    lesson_sections: List[str],
    classification: Optional[Dict] = None,
) -> str:
    """Build the complete HTML page from section content blobs."""

    css = _get_page_css(topic)

    # ── Section label map ──────────────────────────────────────────────
    section_meta = {
        "hero":          ("🌟 Hook",            "hero"),
        "definition":    ("📖 What Is It?",      "definition"),
        "fundamentals":  ("🔑 Fundamentals",     "fundamentals"),
        "subtopics":     ("🧬 Subtopics",        "subtopics"),
        "types":         ("📊 Types",            "types"),
        "pathway":       ("⚙️ How It Works",    "pathway"),
        "formulas":      ("📐 Formulas",         "formulas"),
        "derivation":    ("📊 Derivation",       "derivation"),
        "deep_concepts": ("⚖️ Key Differences", "deep_concepts"),
        "reallife":      ("🌍 Real-Life",        "reallife"),
        "funfacts":      ("💡 Fun Facts",        "funfacts"),
        "activities":    ("🎮 Activities",       "activities"),
        "quiz":          ("❓ Quiz",             "quiz"),
        "revision":      ("📝 Revision",         "revision"),
        "exam_ready":    ("🎯 Exam Ready",       "exam_ready"),
    }

    # ── Side-nav links ──────────────────────────────────────────────────
    nav_links = "\n".join(
        f'    <a href="#{section_meta[s][1]}" onclick="closeNav()">{section_meta[s][0]}</a>'
        for s in lesson_sections
        if s in section_meta
    )

    # ── Floating glossary — pre-populate from fundamentals if available ─
    # (will be re-populated from the generated fundamentals HTML via JS)
    floating_glossary_items = ""
    if "fundamentals" in sections:
        # Extract h5 terms from the fundamentals section for the floating panel
        terms = re.findall(r'<h5>(.*?)</h5>\s*<p>(.*?)</p>', sections["fundamentals"], re.DOTALL)
        for term, defn in terms[:8]:  # show max 8 in floating panel
            floating_glossary_items += (
                f'<div class="fp-item"><strong>{term}</strong>'
                f'<p>{defn.strip()}</p></div>\n'
            )

    # ── Section bodies ──────────────────────────────────────────────────
    section_bodies = ""
    for s in lesson_sections:
        meta = section_meta.get(s, (s.replace("_", " ").title(), s))
        label, anchor_id = meta
        content = sections.get(s, "")

        # Hero is special — no .card wrapper
        if s == "hero":
            section_bodies += f"""
  <section id="{anchor_id}">
    {content}
  </section>
"""
        elif s == "fundamentals":
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">🔑</span> Fundamentals</h2>
    <div class="card">
      <p style="color:var(--text-soft);font-size:.95rem;margin-bottom:16px">
        New to some words? Open the Fundamentals panel to quickly look up any key term!
      </p>
      <button id="fund-btn" onclick="openFund()">📚 Open Fundamentals Panel</button>
    </div>
  </section>
"""
        elif s == "quiz":
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">❓</span> Quiz — 10 Questions</h2>
    <div class="card">
      {content}
    </div>
  </section>
"""
        elif s == "activities":
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">🎮</span> Interactive Activities</h2>
    <div class="card">
      {content}
    </div>
  </section>
"""
        elif s == "funfacts":
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">💡</span> Fun Facts — Tap to Reveal!</h2>
    {content}
  </section>
"""
        elif s == "reallife":
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">🌍</span> {topic} Around Us</h2>
    {content}
  </section>
"""
        elif s == "revision":
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">📝</span> Quick Revision</h2>
    {content}
  </section>
"""
        elif s == "exam_ready":
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">🎯</span> Exam Ready</h2>
    {content}
  </section>
"""
        elif s == "subtopics":
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">🧬</span> Important Subtopics</h2>
    {content}
  </section>
"""
        elif s == "types":
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">📊</span> Types & Classification</h2>
    <div class="card">
      {content}
    </div>
  </section>
"""
        elif s == "pathway":
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">⚙️</span> How It Works — Step by Step</h2>
    <div class="card">
      {content}
    </div>
  </section>
"""
        elif s == "deep_concepts":
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">⚖️</span> Key Differences & Concepts</h2>
    <div class="card">
      {content}
    </div>
  </section>
"""
        elif s == "formulas":
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">📐</span> Formulas & Equations</h2>
    <div class="card">
      {content}
    </div>
  </section>
"""
        elif s == "derivation":
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">📊</span> Mathematical Derivation</h2>
    <div class="card">
      {content}
    </div>
  </section>
"""
        else:
            section_bodies += f"""
  <section id="{anchor_id}">
    <h2 class="sec-title"><span class="ic">{label.split()[0]}</span> {' '.join(label.split()[1:])}</h2>
    <div class="card">
      {content}
    </div>
  </section>
"""

    # ── MathJax ────────────────────────────────────────────────────────
    needs_mathjax = any(s in lesson_sections for s in ("formulas", "derivation"))
    mathjax_block = ""
    if needs_mathjax:
        mathjax_block = """\
<script>
MathJax = {
  tex: { inlineMath: [['$','$'],['\\\\(','\\\\)']], displayMath: [['$$','$$'],['\\\\[','\\\\]']] },
  svg: { fontCache: 'global' }
};
</script>
<script id="MathJax-script" async
  src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>"""

    # ── Fund overlay HTML ───────────────────────────────────────────────
    fund_html = sections.get("fundamentals", "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{topic} — Interactive Learning</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
{mathjax_block}
<style>
{css}
</style>
</head>
<body>

<!-- ═══ LEFT NAV ═══ -->
<button id="nav-btn" onclick="toggleNav()" aria-label="Open sections menu">☰ Sections</button>
<div id="side-nav">
  <div class="nav-head">
    <h3>📚 Sections</h3>
    <button id="nav-close" onclick="toggleNav()" aria-label="Close menu">✕</button>
  </div>
  <nav>
{nav_links}
  </nav>
</div>

<!-- ═══ FLOATING GLOSSARY ═══ -->
<div id="float-glos">
  <div id="float-panel" style="position:relative">
    <button class="fp-close" onclick="toggleFloat()">✕</button>
    <h4>📖 Quick Glossary</h4>
    {floating_glossary_items}
  </div>
  <button id="float-btn" onclick="toggleFloat()">📖 Glossary</button>
</div>

<!-- ═══ FUNDAMENTALS OVERLAY ═══ -->
<div id="fund-overlay">
  <div id="fund-panel">
    <button class="fund-close" onclick="closeFund()">✕</button>
    <h3>🔑 Fundamentals — Key Terms</h3>
    <p style="font-size:.88rem;color:var(--text-soft);margin-bottom:4px">Tap any term to understand it before diving in.</p>
    <input id="fund-search" type="text" placeholder="Search a term..." oninput="filterGlos(this.value)" />
    {fund_html}
  </div>
</div>

<!-- ═══ MAIN CONTENT ═══ -->
<main id="content">
{section_bodies}
</main>

<script>
// ── NAV ──────────────────────────────────────────────────────────
function toggleNav(){{
  document.getElementById('side-nav').classList.toggle('open');
}}
function closeNav(){{
  document.getElementById('side-nav').classList.remove('open');
}}

// ── FLOATING GLOSSARY ─────────────────────────────────────────────
function toggleFloat(){{
  document.getElementById('float-panel').classList.toggle('open');
}}

// ── FUNDAMENTALS OVERLAY ─────────────────────────────────────────
function openFund(){{
  document.getElementById('fund-overlay').classList.add('open');
}}
function closeFund(){{
  document.getElementById('fund-overlay').classList.remove('open');
}}
function filterGlos(q){{
  document.querySelectorAll('.glos-item').forEach(function(item){{
    var term = item.querySelector('h5') ? item.querySelector('h5').textContent.toLowerCase() : '';
    var def  = item.querySelector('p')  ? item.querySelector('p').textContent.toLowerCase() : '';
    item.style.display = (term.includes(q.toLowerCase()) || def.includes(q.toLowerCase())) ? '' : 'none';
  }});
}}

// ── SCROLL SPY ────────────────────────────────────────────────────
var sections = document.querySelectorAll('section[id]');
var navLinks = document.querySelectorAll('#side-nav nav a');
window.addEventListener('scroll', function(){{
  var scrollY = window.pageYOffset;
  sections.forEach(function(s){{
    var top = s.offsetTop - 60;
    var bottom = top + s.offsetHeight;
    if(scrollY >= top && scrollY < bottom){{
      navLinks.forEach(function(a){{
        a.classList.toggle('active', a.getAttribute('href') === '#' + s.id);
      }});
    }}
  }});
}});

// ── REAL-LIFE CARD FLIP ───────────────────────────────────────────
function revealRL(card){{ card.classList.toggle('revealed'); }}

// ── FUN FACTS FLIP ────────────────────────────────────────────────
function revealFF(card){{ card.classList.toggle('revealed'); }}
</script>

</body>
</html>"""


# ════════════════════════════════════════════════════════════════════════
#  TOPIC CLASSIFIER
# ════════════════════════════════════════════════════════════════════════

async def _classify_topic(topic: str) -> Dict:
    prompt = f"""Classify this educational topic for content generation.

Topic: "{topic}"

Return ONLY valid JSON, no markdown:
{{
  "category": "mathematical" | "semi_mathematical" | "conceptual",
  "needs_formula": true | false,
  "needs_derivation": true | false,
  "subject": "Biology" | "Physics" | "Chemistry" | "Mathematics" | "History" | "Geography" | "Other",
  "level": "Primary" | "Class 9-10" | "Class 11-12" | "University",
  "primary_phenomenon": "brief phrase describing the core process"
}}

"mathematical" = needs_formula=true AND needs_derivation=true
  (optics, thermodynamics, mechanics, waves, electromagnetism, signal processing)
"semi_mathematical" = needs_formula=true, needs_derivation=false
  (basic laws with useful formulas but no deep derivation)
"conceptual" = needs_formula=false, needs_derivation=false
  (biology overviews, history, classification, purely qualitative topics)"""

    try:
        msg = await client.messages.create(
            model=MODEL_HAIKU,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = re.sub(r'```json\s*|\s*```', '', msg.content[0].text.strip())
        result = json.loads(raw)
        log.info(
            f"[_classify_topic] '{topic}' → {result.get('category')} | "
            f"formula={result.get('needs_formula')} | deriv={result.get('needs_derivation')} | "
            f"subject={result.get('subject')}"
        )
        return result
    except Exception as e:
        log.warning(f"[_classify_topic] failed ({e}), defaulting to conceptual")
        return {
            "category": "conceptual",
            "needs_formula": False,
            "needs_derivation": False,
            "subject": "Other",
            "level": "Class 9-10",
            "primary_phenomenon": topic,
        }


# ════════════════════════════════════════════════════════════════════════
#  SECTION LIST BUILDER
# ════════════════════════════════════════════════════════════════════════

def _build_section_list(classification: Dict) -> List[str]:
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


# ════════════════════════════════════════════════════════════════════════
#  CORE GENERATOR CLASS
# ════════════════════════════════════════════════════════════════════════

class EduPageGenerator:

    def __init__(self, api_key: Optional[str] = None):
        self._client = (
            anthropic.AsyncAnthropic(api_key=api_key)
            if api_key
            else client
        )

    # ──────────────────────────────────────────────────────────────────
    #  GENERATE SINGLE SECTION
    # ──────────────────────────────────────────────────────────────────

    async def generate_section(
        self,
        section_name: str,
        topic: str,
        context: str = "",
        subtopics_list: Optional[List[str]] = None,
        classification: Optional[Dict] = None,
        max_retries: int = 2,
    ) -> str:
        prompt = _build_section_prompt(
            section_name, topic, context,
            subtopics_list=subtopics_list,
            classification=classification,
        )
        model = SECTION_MODEL_MAP.get(section_name, MODEL_SONNET)
        log.info(f"  Generating [{section_name}] with {model.split('-')[1]} …")

        for attempt in range(1, max_retries + 1):
            try:
                msg = await self._client.messages.create(
                    model=model,
                    max_tokens=12000,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}]
                )
                content = msg.content[0].text.strip()
                content = re.sub(r'```html\s*|\s*```', '', content).strip()
                content = _strip_tail(content)
                log.info(f"  ✅ [{section_name}] done ({len(content):,} chars)")
                return content
            except Exception as e:
                log.warning(f"  ⚠️ [{section_name}] attempt {attempt}/{max_retries}: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2)

        log.error(f"  ❌ [{section_name}] FAILED after {max_retries} attempts")
        return (
            f'<div style="padding:16px;background:#fee2e2;border-radius:8px;color:#7f1d1d;">'
            f'⚠️ Section <strong>{section_name}</strong> could not be generated.'
            f'</div>'
        )

    # ──────────────────────────────────────────────────────────────────
    #  GENERATE COMPLETE PAGE
    # ──────────────────────────────────────────────────────────────────

    async def generate_complete_page(
        self,
        topic: str,
        subtopics_list: Optional[List[str]] = None,
    ) -> Dict:
        log.info(f"\n{'═'*64}")
        log.info(f"[EduPageGenerator v21.0] topic='{topic}'")
        log.info(f"[EduPageGenerator v21.0] specific={_is_specific_subtopic(topic)}")
        if subtopics_list:
            log.info(f"[EduPageGenerator v21.0] subtopics={subtopics_list}")
        log.info(f"{'═'*64}")

        # Stage 0 — classify
        log.info("[STAGE 0] Classifying topic …")
        classification = await _classify_topic(topic)
        context = json.dumps(classification)

        # Stage 1 — build section list
        lesson_sections = _build_section_list(classification)
        log.info(f"[STAGE 0] Sections: {lesson_sections}")

        # Stage 2 — generate all sections in parallel
        log.info(f"[STAGE 1] Generating {len(lesson_sections)} sections in parallel …")

        async def _gen(s: str) -> str:
            return await self.generate_section(
                s, topic, context,
                subtopics_list=(subtopics_list if s == "subtopics" else None),
                classification=classification,
            )

        section_contents = await asyncio.gather(*[_gen(s) for s in lesson_sections])
        sections = dict(zip(lesson_sections, section_contents))

        # Stage 3 — assemble
        log.info("[STAGE 2] Assembling HTML page …")
        html = _assemble_html(topic, sections, lesson_sections, classification)

        # Metadata
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

        log.info(
            f"[COMPLETE] ✅ {len(html):,} chars | {total_words:,} words | "
            f"{len(lesson_sections)} sections"
        )
        return {"sections": sections, "html": html, "metadata": metadata}


# ════════════════════════════════════════════════════════════════════════
#  PRIMARY ENTRY POINT — generate_animation
# ════════════════════════════════════════════════════════════════════════

async def generate_animation(prompt: str) -> dict:
    """
    Primary backend entry point.
    Accepts a user prompt (topic name, with optional subtopics via ' - ' or ' -- ').
    Returns:
        {
          "title":          str,
          "explanation":    str,   # short summary (~220 chars)
          "animation_code": str,   # complete HTML page
        }
    """
    if not prompt or not prompt.strip():
        raise ValueError("Prompt cannot be empty")

    prompt = prompt.strip()
    log.info(f"\n{'═'*64}")
    log.info(f"[generate_animation v21.0] prompt='{prompt}'")
    log.info(f"{'═'*64}")

    subtopics_list = _extract_subtopics_from_input(prompt)

    # Parse topic from prompt
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

    log.info(f"[generate_animation] topic='{topic}' | subtopics={subtopics_list}")

    generator = EduPageGenerator()
    result = await generator.generate_complete_page(
        topic=topic,
        subtopics_list=subtopics_list if subtopics_list else None,
    )

    html = result["html"]

    # Build short explanation from definition section
    def_html = result["sections"].get("definition", "")
    explanation = re.sub(r"<[^>]+>", " ", def_html)
    explanation = " ".join(explanation.split())[:220]
    if not explanation:
        explanation = f"A complete interactive learning page on {topic}."

    log.info(f"[generate_animation] ✅ HTML={len(html):,} chars")

    return {
        "title":          topic,
        "explanation":    explanation,
        "animation_code": html,
    }


# ════════════════════════════════════════════════════════════════════════
#  PUBLIC ASYNC API
# ════════════════════════════════════════════════════════════════════════

async def generate_edu_page(
    topic: str,
    output_file: Optional[str] = None,
    subtopics_list: Optional[List[str]] = None,
) -> Dict:
    """
    High-level async API — generates a complete educational HTML page.
    Optionally saves to output_file.
    Returns the full result dict.
    """
    generator = EduPageGenerator()
    result = await generator.generate_complete_page(
        topic=topic,
        subtopics_list=subtopics_list,
    )
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(result["html"])
        log.info(f"💾 Saved HTML to: {output_file}")
    return result


def generate_edu_page_sync(
    topic: str,
    output_file: Optional[str] = None,
    subtopics_list: Optional[List[str]] = None,
) -> Dict:
    """Synchronous wrapper around generate_edu_page."""
    return asyncio.run(
        generate_edu_page(
            topic=topic,
            output_file=output_file,
            subtopics_list=subtopics_list,
        )
    )


# ════════════════════════════════════════════════════════════════════════
#  GENZET BOOK CONTENT ENTRY POINT (backward-compatible)
# ════════════════════════════════════════════════════════════════════════

async def generate_genzet_book_content(
    topic: str,
    subtopic: str,
    pdf_context: str = "",
    subtopics_list: Optional[List[str]] = None,
) -> dict:
    """
    Backward-compatible entry point for book-context generation.
    Assembles a full_topic string and delegates to generate_animation.
    """
    topic    = (topic    or "").strip()
    subtopic = (subtopic or "").strip()
    if not topic:
        raise ValueError("topic cannot be empty")

    full_topic = (
        f"{topic} — {subtopic}"
        if subtopic and subtopic.lower() != topic.lower()
        else topic
    )

    log.info(f"[generate_genzet_book_content] topic='{full_topic}' | "
             f"pdf_ctx={len(pdf_context)} chars | subtopics={len(subtopics_list or [])}")

    # If we have PDF context inject it as a hint via the subtopics mechanism
    generator = EduPageGenerator()
    result = await generator.generate_complete_page(
        topic=full_topic,
        subtopics_list=subtopics_list if subtopics_list else None,
    )

    html = result["html"]
    def_html = result["sections"].get("definition", "")
    explanation = re.sub(r"<[^>]+>", " ", def_html)
    explanation = " ".join(explanation.split())[:220]
    if not explanation:
        explanation = f"A complete textbook-grounded lesson on {full_topic}."

    return {
        "title":          full_topic,
        "explanation":    explanation,
        "animation_code": html,
    }


# ════════════════════════════════════════════════════════════════════════
#  subtopics_json_to_genzet_args  (unchanged helper)
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
#  CLI
# ════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python claude_client.py <topic> [-- sub1, sub2, sub3]")
        print("       python claude_client.py <topic> [- sub1, sub2]")
        print()
        print("Examples:")
        print("  python claude_client.py 'Stimulus'")
        print("  python claude_client.py 'Photosynthesis'")
        print("  python claude_client.py 'Gravitation' -- 'Kepler Laws, Orbital Velocity'")
        print("  python claude_client.py 'Aerobic and Anaerobic Respiration'")
        print("  python claude_client.py 'Convective Heat Transfer'")
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

    safe_name = re.sub(r'[^\w\-]', '_', topic.lower())[:60]
    output_file = f"edupage_{safe_name}.html"

    print(f"\n{'='*64}")
    print(f"EduPage Generator  v21.0  —  Reference HTML Style")
    print(f"{'='*64}")
    print(f"Topic      : {topic}")
    print(f"Specific   : {_is_specific_subtopic(topic)}")
    print(f"Subtopics  : {subtopics if subtopics else '(auto-detect)'}")
    print(f"Output     : {output_file}")
    print(f"{'='*64}\n")

    result = generate_edu_page_sync(
        topic=topic,
        output_file=output_file,
        subtopics_list=subtopics if subtopics else None,
    )

    print(f"\n{'='*64}")
    print(f"GENERATION COMPLETE")
    print(f"{'='*64}")
    meta = result["metadata"]
    print(f"Sections   : {meta['total_sections']} — {meta['sections_generated']}")
    print(f"Words      : {meta['total_words']:,}")
    print(f"Read time  : {meta['estimated_read_minutes']} min")
    print(f"HTML file  : {output_file}")
    cls = meta.get('classification', {})
    print(f"Category   : {cls.get('category','?')} | "
          f"formula={cls.get('needs_formula','?')} | "
          f"deriv={cls.get('needs_derivation','?')}")
    print(f"{'='*64}\n")
