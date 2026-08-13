"""
╔══════════════════════════════════════════════════════════════════════╗
║  claude_client.py  v22.0  —  EXACT Reference-HTML Method            ║
║  Generates any topic following the EXACT pipeline, UI/UX,           ║
║  section structure and design language of the four reference files:  ║
║    • Aerobic___Anaerobic_Respiration.html                           ║
║    • Respiration.html                                               ║
║    • rocketpropulsion.html                                          ║
║    • tissues.html                                                   ║
║                                                                      ║
║  EXACT MATCHES FROM REFERENCES:                                      ║
║  ✅ CSS tokens: --bg, --card, --surface, --primary, --secondary,    ║
║     --success, --warning, --danger, --text, --border, --shadow,     ║
║     --radius, --radius-sm + topic-specific accent pair              ║
║  ✅ Navigation: fixed side-nav (left) + toggle button,              ║
║     floating glossary/fundamentals (right panel)                    ║
║  ✅ STICKY HEADER with scroll-progress bar (rocketpropulsion style)  ║
║  ✅ Section pipeline (13 sections):                                  ║
║     §1 Hook  §2 Definition+Objectives  §3 Fundamentals-CTA          ║
║     §4 Subtopics-Grid  §5 Types-Flowchart  §6 Deep-Sections         ║
║     §7 Interactive-Visual  §8 Working-Process(Stepper/Timeline)      ║
║     §9 Comparison-Table  §10 Games(3 tabs)  §11 Fun-Facts(flip)      ║
║     §12 Quick-Revision  §13 Quiz(dot-progress + explanation)         ║
║     [+ §14 Formulas  §15 Derivation  for mathematical topics]       ║
║  ✅ Game pattern: 3-tab game area (scenario MCQ, product matcher,    ║
║     situation challenge) — exactly as in Aerobic reference          ║
║  ✅ Quiz: dot-progress indicators + explanation box + result card   ║
║  ✅ Fun facts: click-to-reveal cards (fact-card pattern)            ║
║  ✅ SVG animations and canvas elements where appropriate            ║
║  ✅ Responsive breakpoints from references                          ║
║  ✅ All async entry-points preserved: generate_animation(),         ║
║     generate_genzet_book_content(), generate_edu_page()             ║
╚══════════════════════════════════════════════════════════════════════╝
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
from typing import Optional, Dict, List, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Clients ──────────────────────────────────────────────────────────────────
client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ══════════════════════════════════════════════════════════════════════════════
#  MODEL CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU  = "claude-haiku-4-5-20251001"

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION REGISTRY  (mirrors reference-file section order exactly)
# ══════════════════════════════════════════════════════════════════════════════
BASE_SECTIONS: List[str] = [
    "hook",           # §1  Hero / Hook card with SVG animation
    "definition",     # §2  Definition + Learning Objectives
    "fundamentals",   # §3  Fundamentals CTA + glossary data
    "subtopics",      # §4  Subtopic cards grid
    "types",          # §5  Types flowchart (clickable)
    "deep_sections",  # §6  Type-specific deep dive cards
    "visual",         # §7  Interactive visual / animated diagram
    "working",        # §8  Step-by-step working process stepper
    "comparison",     # §9  Comparison table (side-by-side)
    "games",          # §10 3-tab game area
    "funfacts",       # §11 Fun facts click-to-reveal
    "revision",       # §12 Quick revision cards + equations
    "quiz",           # §13 10-question quiz with dot progress
]

CONDITIONAL_SECTIONS: List[str] = ["formulas", "derivation"]

ORDERED_SECTIONS: List[str] = [
    "hook", "definition", "fundamentals", "subtopics", "types",
    "deep_sections", "formulas", "derivation", "visual", "working",
    "comparison", "games", "funfacts", "revision", "quiz",
]

SECTION_MODEL_MAP: Dict[str, str] = {
    "hook":          MODEL_SONNET,
    "definition":    MODEL_SONNET,
    "fundamentals":  MODEL_HAIKU,
    "subtopics":     MODEL_SONNET,
    "types":         MODEL_SONNET,
    "deep_sections": MODEL_SONNET,
    "formulas":      MODEL_SONNET,
    "derivation":    MODEL_SONNET,
    "visual":        MODEL_SONNET,
    "working":       MODEL_SONNET,
    "comparison":    MODEL_HAIKU,
    "games":         MODEL_SONNET,
    "funfacts":      MODEL_HAIKU,
    "revision":      MODEL_HAIKU,
    "quiz":          MODEL_HAIKU,
}

# ══════════════════════════════════════════════════════════════════════════════
#  TOPIC UTILITIES
# ══════════════════════════════════════════════════════════════════════════════
_SPECIFIC_KW = (" in "," of "," for "," during "," within ",
                " via "," through "," using "," under ")

def _is_specific(topic: str) -> bool:
    return any(kw in topic.lower() for kw in _SPECIFIC_KW)

def _focus(topic: str) -> str:
    if not _is_specific(topic):
        return ""
    return (f'\n⚠️ SPECIFIC SUB-TOPIC: "{topic}". '
            f'Every sentence must focus exclusively on this exact topic.\n')

def _extract_subtopics(raw: str) -> List[str]:
    subs: List[str] = []
    if " -- " in raw:
        _, rest = raw.split(" -- ", 1)
        subs = [s.strip() for s in rest.split(",") if s.strip()]
    elif raw.count(" - ") > 1:
        parts = raw.split(" - ")
        subs = [s.strip() for s in parts[1:] if s.strip()]
    elif " - " in raw:
        parts = raw.split(" - ", 1)
        if len(parts) == 2:
            subs = [s.strip() for s in parts[1].split(",") if s.strip()]
    seen: set = set()
    return [s for s in subs if s.lower() not in seen and not seen.add(s.lower())]  # type: ignore

# ══════════════════════════════════════════════════════════════════════════════
#  MASTER SYSTEM PROMPT  (exact reference style)
# ══════════════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """\
You are a SENIOR EDUCATIONAL HTML AUTHOR who writes interactive lesson pages
for school students aged 13-18.

YOUR OUTPUT MUST MATCH THESE REFERENCE FILES EXACTLY:
  1. Aerobic___Anaerobic_Respiration.html  (Inter+Poppins, blue/orange tokens,
     stepper, 3-tab game area, dot-progress quiz, fact-card grid)
  2. Respiration.html  (side-nav with overlay, right glossary panel, clickable
     pathway steps, SVG animation, match game, progress bar quiz)
  3. rocketpropulsion.html  (sticky header + progress bar, canvas animation,
     timeline working process, flip-card fun facts, score-circle result)
  4. tissues.html  (purple primary, fixed left toggle + right glossary toggle,
     tab-based deep sections, comparison table, match-game, dot-quiz)

DESIGN RULES — APPLY ALL:
  • Fonts: Inter (body) + Poppins (headings) from Google Fonts
  • CSS tokens match the reference set: --bg, --card/--surface, --primary,
    --secondary/--accent, --success/--green, --warning, --danger/--red,
    --text, --muted, --border, --shadow, --radius, --radius-sm
    PLUS a topic-specific accent pair (color A for type 1, color B for type 2)
  • Section labels: small ALL-CAPS uppercase eyebrow text above each h2
  • Cards: white background, 1-2px border, rounded corners, soft box-shadow
  • Equations: left-colored-border box with inline colored term markup
  • Stepper: border-left timeline with ::before numbered circle bullets
  • Flowchart: .flowchart / .flow-box / .flow-arrow / .flow-row / .flow-col
  • Tables: colored th for each column type, alternating tr background
  • Quiz: dot-progress (.q-dot), explanation box (.quiz-explain), result card
  • Game tabs: .game-tab / .game-panel / .resp-btn / .game-feedback pattern
  • Flip/reveal cards: .fact-card with .fact-front / .fact-back / .revealed
  • Fun-fact cards click to toggle .revealed class (NO JS function name collision)
  • nav: fixed left side-nav + toggle; right glossary panel + floating button
  • All JavaScript: use var (not const/let) for widest browser compat

OUTPUT RULES:
  1. Return ONLY valid HTML — no markdown, no code fences
  2. Every section ends exactly at its last closing </div> or </script>
  3. LaTeX: $$...$$ display, $...$ inline (MathJax loaded when needed)
  4. Never produce a paragraph longer than 4 lines
  5. JavaScript IDs/function names must be unique per section (append short suffix)
"""

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION PROMPTS
# ══════════════════════════════════════════════════════════════════════════════

def _build_prompt(section: str, topic: str, ctx: str = "",
                  subtopics: Optional[List[str]] = None,
                  classification: Optional[Dict] = None) -> str:
    focus = _focus(topic)
    c = ctx[:700]
    T = topic

    PROMPTS: Dict[str, str] = {

# ─────────────────────────────────────────────────────────────────
# §1  HOOK
# ─────────────────────────────────────────────────────────────────
"hook": f"""Generate the HOOK section for topic: "{T}"
{focus}
Style reference: the #hook section in Aerobic___Anaerobic_Respiration.html —
  a full-width gradient card, section-label eyebrow, h1, 2-3 sentence hook,
  and an optional decorative emoji as CSS ::before pseudo-content.

Also produce the GLOSSARY DATA as a JSON comment block inside a <!-- --> tag
so it can be extracted later. Format:
<!--GLOSSARY_JSON
[
  {{"term":"Term","def":"Definition"}},
  ...10-14 terms...
]
-->

Return ONLY this HTML (replace ALL placeholders):

<section id="hook" class="section">
  <!-- GLOSSARY DATA — DO NOT REMOVE -->
  <!--GLOSSARY_JSON
  [
    {{"term":"[Key term 1 for {T}]","def":"[Plain-English definition]"}},
    {{"term":"[Key term 2]","def":"[Definition]"}},
    [Continue for 10-14 total terms relevant to "{T}"]
  ]
  -->

  <div class="section-label">[Subject · Class level, e.g. "Class 10 Biology"]</div>
  <h1>[Topic title with relevant emoji prefix]</h1>
  <p>[2-3 sentence hook that surprises a 15-year-old and makes "{T}" feel exciting. Use simple language, real examples, a question or surprising fact.]</p>
</section>

OUTPUT NOTHING after the closing </section> tag.""",

# ─────────────────────────────────────────────────────────────────
# §2  DEFINITION + OBJECTIVES
# ─────────────────────────────────────────────────────────────────
"definition": f"""Generate the DEFINITION + OBJECTIVES section for topic: "{T}"
{focus}
Context: {c}

Style reference: Aerobic_Respiration.html #definition / Respiration.html #section-def.
The card has:
  - 2-sentence plain-English definition
  - 2 .chip spans for the two main sub-types or key variants (colored)
  - 1 sentence on why this matters
  - <hr> divider
  - h3 "🎯 Learning Objectives"
  - <ul class="obj-list"> with 5 ✓ checkmark items

Return ONLY this HTML:

<section id="definition" class="section">
  <div class="card">
    <div class="section-label">Definition / Objective</div>
    <h2>What is {T}?</h2>
    <p><strong>[Key term]</strong> is [plain-English definition — 1-2 sentences, simple words].</p>
    <p>Two aspects: <span class="chip chip-a">[Variant/Type A label]</span> <span class="chip chip-b">[Variant/Type B label]</span></p>
    <p>[One sentence on why "{T}" matters to everyday life or science].</p>
    <hr style="border:none;border-top:1px solid var(--border);margin:18px 0;">
    <h3>🎯 Learning Objectives</h3>
    <ul class="obj-list">
      <li>[Objective 1: define/explain "{T}"]</li>
      <li>[Objective 2: identify types or components]</li>
      <li>[Objective 3: understand the process/mechanism]</li>
      <li>[Objective 4: compare types or contrast with related concepts]</li>
      <li>[Objective 5: real-world application]</li>
    </ul>
  </div>
</section>

OUTPUT NOTHING after the closing </section>.""",

# ─────────────────────────────────────────────────────────────────
# §3  FUNDAMENTALS CTA
# ─────────────────────────────────────────────────────────────────
"fundamentals": f"""Generate the FUNDAMENTALS section for topic: "{T}"
{focus}

Style reference: the #fundamentals section in Aerobic_Respiration.html —
  a purple-light card with a "New words? Check the Glossary!" CTA.

Return ONLY this HTML:

<section id="fundamentals" class="section">
  <div class="card" style="background:var(--purple-light,#ede9fe);border-color:var(--purple,#7c3aed);text-align:center;padding:20px;">
    <div style="font-size:32px;margin-bottom:8px;">🧠</div>
    <h2 style="color:var(--purple,#7c3aed);">New words? Check the Glossary!</h2>
    <p style="color:#5b21b6;">Tap the <strong>📖 Glossary</strong> button (bottom right) anytime to look up terms like [list 3-4 key terms for "{T}" here].</p>
    <button onclick="toggleGlossary()" style="margin-top:10px;padding:10px 24px;background:var(--purple,#7c3aed);color:#fff;border:none;border-radius:50px;font-size:14px;font-weight:700;cursor:pointer;">Open Glossary →</button>
  </div>
</section>

OUTPUT NOTHING after the closing </section>.""",

# ─────────────────────────────────────────────────────────────────
# §4  SUBTOPICS GRID
# ─────────────────────────────────────────────────────────────────
"subtopics": f"""Generate the SUBTOPICS GRID section for topic: "{T}"
{focus}
User-requested subtopics: {subtopics or '(auto-detect 6-8 key subtopics)'}
Context: {c}

Style reference: #subtopics in Aerobic_Respiration.html — .subtopic-grid with
.subtopic-card elements. Each card has:
  • .subtopic-icon (emoji)
  • .subtopic-title
  • .subtopic-desc (2-3 sentences)
  • .kw-list with 2-3 .kw keyword chips
  Generate 6-8 cards.

Return ONLY this HTML:

<section id="subtopics" class="section">
  <div class="section-label">Key Topics</div>
  <h2>📚 What You'll Learn</h2>
  <div class="subtopic-grid">
    <div class="subtopic-card">
      <div class="subtopic-icon">[emoji]</div>
      <div class="subtopic-title">[Subtopic name]</div>
      <div class="subtopic-desc">[2-3 sentence description relevant to "{T}"]</div>
      <div class="kw-list"><span class="kw">[kw1]</span><span class="kw">[kw2]</span></div>
    </div>
    [Repeat for all 6-8 subtopics — every card complete]
  </div>
</section>

OUTPUT NOTHING after the closing </section>.""",

# ─────────────────────────────────────────────────────────────────
# §5  TYPES FLOWCHART
# ─────────────────────────────────────────────────────────────────
"types": f"""Generate the TYPES FLOWCHART section for topic: "{T}"
{focus}
Context: {c}

Style reference: #types in Aerobic_Respiration.html — a .flowchart inside
.card, with .flow-box.main at top, .flow-arrow, then a .flow-row with
.flow-col branches for each main type. Each branch has its own colored
.flow-box, sub-details, and a .flow-label at bottom.

Also include a small comparison summary table below the flowchart with
colored column headers for each type.

Return ONLY this HTML:

<section id="types" class="section">
  <div class="card">
    <div class="section-label">Overview</div>
    <h2>🌿 Types of {T}</h2>
    <div class="flowchart" style="padding:16px 0;">
      <div class="flow-box main">[ROOT: "{T}"]</div>
      <div class="flow-arrow">↓</div>
      <div class="flow-box sub">[Second-level classification or process name]<br><span style="font-size:12px;font-weight:400;color:var(--muted);">[brief subtitle]</span></div>
      <div class="flow-arrow">↓</div>
      <div class="flow-row">
        <div class="flow-col">
          <div class="flow-box" style="background:var(--color-a,#2563eb);color:#fff;">[Type A emoji + name]</div>
          <div class="flow-arrow">↓</div>
          <div class="flow-box sub" style="border-color:var(--color-a,#2563eb);">[Key characteristic of Type A]</div>
          <div class="flow-arrow">↓</div>
          <div class="flow-label">[Products/outcomes of Type A]</div>
          <div style="margin-top:6px;font-size:13px;color:var(--muted);">[Example organisms or contexts]</div>
        </div>
        <div class="flow-col">
          <div class="flow-box" style="background:var(--color-b,#d97706);color:#fff;">[Type B emoji + name]</div>
          <div class="flow-arrow">↓</div>
          <div class="flow-box sub" style="border-color:var(--color-b,#d97706);">[Key characteristic of Type B]</div>
          <div class="flow-arrow">↓</div>
          <div class="flow-label">[Products/outcomes of Type B]</div>
          <div style="margin-top:6px;font-size:13px;color:var(--muted);">[Example organisms or contexts]</div>
        </div>
        [Add a third .flow-col if there is a third main type; otherwise omit]
      </div>
    </div>
  </div>
</section>

OUTPUT NOTHING after the closing </section>.""",

# ─────────────────────────────────────────────────────────────────
# §6  DEEP SECTIONS (type-specific detail cards)
# ─────────────────────────────────────────────────────────────────
"deep_sections": f"""Generate DEEP TYPE DETAIL sections for topic: "{T}"
{focus}
Context: {c}

Style reference: #aerobic and #anaerobic sections in Aerobic_Respiration.html.
For each main type/category of "{T}", produce ONE .card with:
  • border-top 4px solid in the type's color
  • .section-label with color matching the type
  • h2 with type emoji + name
  • 1 paragraph explanation
  • An .equation or process-visual block (colored background box)
  • A sub-grid showing 2 variants or sub-types (if applicable)
  • A .mito-box style highlight box for the key location/organelle/component

Produce 2-3 such sections (one per main type).

Return ONLY the HTML of all these sections (no outer wrapper):

<section id="type-a" class="section">
  <div class="card" style="border-top:4px solid var(--color-a,#2563eb);">
    <div class="section-label" style="color:var(--color-a,#2563eb);">Type [1 / A]</div>
    <h2>[Type A emoji + full name]</h2>
    <p>[Clear explanation of Type A in context of "{T}" — what makes it different, when it happens, what is produced].</p>

    <div class="equation" style="border-left-color:var(--color-a,#2563eb);">
      <span style="color:var(--color-a,#2563eb);font-weight:800;">[Input A]</span>
      <span class="eq-plus">[+ or →]</span>
      <span style="color:var(--color-a,#2563eb);font-weight:800;">[Input B if any]</span>
      <span class="eq-arrow">→</span>
      <span>[Output 1]</span>
      <span class="eq-plus">+</span>
      <span>[Output 2]</span>
      <span class="eq-plus">+</span>
      <span style="color:var(--green,#16a34a);font-weight:800;">[Energy / Key Product]</span>
    </div>

    <!-- Inner process visual for Type A -->
    <div style="background:var(--color-a-light,#dbeafe);border-radius:var(--radius);padding:18px;margin:16px 0;">
      <div class="flowchart" style="gap:6px;">
        <div style="background:var(--color-a,#2563eb);color:#fff;border-radius:var(--radius-sm);padding:8px 20px;font-weight:700;font-size:14px;">[Step 1 for Type A]</div>
        <div class="flow-arrow">↓</div>
        <div style="background:var(--color-a,#2563eb);color:#fff;border-radius:var(--radius-sm);padding:8px 20px;font-weight:700;font-size:14px;">[Step 2]</div>
        <div class="flow-arrow">↓</div>
        <div style="background:var(--green,#16a34a);color:#fff;border-radius:var(--radius-sm);padding:8px 20px;font-weight:700;font-size:14px;">⚡ [Key output / energy release]</div>
        <div class="flow-arrow">↓</div>
        <div style="background:var(--surface,#fff);border:2px solid var(--color-a,#2563eb);border-radius:var(--radius-sm);padding:8px 20px;font-weight:700;font-size:14px;color:var(--text);">[Final products]</div>
      </div>
    </div>

    <!-- Key location / component highlight box -->
    <div class="mito-box">
      <div style="font-size:40px;">[Component emoji]</div>
      <div class="mito-text">
        <div class="mito-label">[Highlight label e.g. "The Powerhouse"]</div>
        <strong>[Key location or component name]</strong> is where [short explanation of its role in "{T}" Type A].
      </div>
    </div>
  </div>
</section>

[Repeat for Type B (and Type C if applicable) with matching structure,
 using var(--color-b,...) and appropriate content.]

OUTPUT NOTHING after the final closing </section>.""",

# ─────────────────────────────────────────────────────────────────
# §7  INTERACTIVE VISUAL
# ─────────────────────────────────────────────────────────────────
"visual": f"""Generate the INTERACTIVE VISUAL section for topic: "{T}"
{focus}
Context: {c}

Style reference: the "See Respiration in Action" section in Aerobic_Respiration.html.
Includes:
  • A .resp-toggle with 2 (or 3) toggle buttons (.resp-tog-btn) to switch views
  • A .anim-container with type-specific animated molecule rows
  • Each molecule is a colored circle div (.anim-molecule) with @keyframes pulse
  • An SVG diagram (viewBox ~680×150) showing both pathways side by side
    (same style as the resp-svg in Respiration.html: two labeled boxes, arrows,
    defs with arrowhead markers, animated circle particles)

All JavaScript must use var, not const/let. Use unique ID suffixes to avoid
collisions. The toggle switches between the visual representations.

Return ONLY the complete HTML + inline <style> + <script> for this section:

<section id="visual" class="section">
  <div class="card">
    <div class="section-label">Interactive Visual</div>
    <h2>🔬 See {T} in Action</h2>
    [toggle buttons + animated molecule rows + SVG diagram + JS]
  </div>
</section>

OUTPUT NOTHING after the closing </section>.""",

# ─────────────────────────────────────────────────────────────────
# §8  WORKING PROCESS (stepper)
# ─────────────────────────────────────────────────────────────────
"working": f"""Generate the WORKING PROCESS section for topic: "{T}"
{focus}
Context: {c}

Style reference: #working in Aerobic_Respiration.html.
A two-column grid (.process-grid) with one .stepper per main type.
Each stepper has 4-6 .step elements with:
  - data-n="N" attribute
  - ::before circle badge via CSS (aerobic-step or anaerobic-step class)
  - .step-title and .step-desc

If the topic has only one main pathway (not two types), use a single-column
timeline like rocketpropulsion.html #process (.timeline > .tl-item > .tl-dot + .tl-card).

Return ONLY this HTML:

<section id="working" class="section">
  <div class="card">
    <div class="section-label">Step by Step</div>
    <h2>⚙️ Working Process</h2>
    <div class="process-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:20px;">
      <div>
        <h3 style="color:var(--color-a,#2563eb);margin-bottom:12px;">[Type A emoji + name]</h3>
        <div class="stepper">
          <div class="step step-a" data-n="1">
            <div><div class="step-title">[Step 1 title for Type A]</div><div class="step-desc">[1-2 sentence description]</div></div>
          </div>
          <div class="step step-a" data-n="2">
            <div><div class="step-title">[Step 2]</div><div class="step-desc">[Description]</div></div>
          </div>
          [3-6 steps total]
        </div>
      </div>
      <div>
        <h3 style="color:var(--color-b,#d97706);margin-bottom:12px;">[Type B emoji + name]</h3>
        <div class="stepper">
          <div class="step step-b" data-n="1">
            <div><div class="step-title">[Step 1 for Type B]</div><div class="step-desc">[Description]</div></div>
          </div>
          [3-6 steps]
        </div>
      </div>
    </div>
  </div>
</section>

OUTPUT NOTHING after the closing </section>.""",

# ─────────────────────────────────────────────────────────────────
# §9  COMPARISON TABLE
# ─────────────────────────────────────────────────────────────────
"comparison": f"""Generate the COMPARISON TABLE section for topic: "{T}"
{focus}
Context: {c}

Style reference: #comparison in Aerobic_Respiration.html.
A .compare-table-wrap > .compare-table with:
  • th for each column (first col muted bg, type-A col color, type-B col color)
  • 6-8 feature rows including: key requirement, breakdown type, energy output,
    products/byproducts, location (site), speed, examples

Return ONLY this HTML:

<section id="comparison" class="section">
  <div class="card">
    <div class="section-label">Side by Side</div>
    <h2>⚖️ [Type A] vs [Type B]</h2>
    <div class="compare-table-wrap">
      <table class="compare-table">
        <thead>
          <tr>
            <th>Feature</th>
            <th>[Type A emoji + label]</th>
            <th>[Type B emoji + label]</th>
          </tr>
        </thead>
        <tbody>
          <tr><td>[Feature 1]</td><td>[A value]</td><td>[B value]</td></tr>
          <tr><td>[Feature 2]</td><td>[A value]</td><td>[B value]</td></tr>
          <tr><td>[Feature 3]</td><td>[A value]</td><td>[B value]</td></tr>
          <tr><td>[Feature 4]</td><td>[A value]</td><td>[B value]</td></tr>
          <tr><td>[Feature 5]</td><td>[A value]</td><td>[B value]</td></tr>
          <tr><td>[Feature 6]</td><td>[A value]</td><td>[B value]</td></tr>
          <tr><td>[Feature 7]</td><td>[A value]</td><td>[B value]</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</section>

OUTPUT NOTHING after the closing </section>.""",

# ─────────────────────────────────────────────────────────────────
# §10  GAMES (3-tab)
# ─────────────────────────────────────────────────────────────────
"games": f"""Generate the 3-TAB GAMES section for topic: "{T}"
{focus}
Context: {c}

Style reference: #games in Aerobic_Respiration.html.
EXACTLY THREE GAMES:
  Game 1 — "Which Type?" scenario MCQ (like the oxygen-available game)
  Game 2 — "Product Matcher" click-and-place (like match-cards/match-drop)
  Game 3 — "Situation Challenge" 5-question MCQ with score badge

Use the EXACT class names from the reference:
  .game-area, .game-tabs, .game-tab, .game-panel.active, .game-question,
  .btn-row, .resp-btn (.correct/.wrong), .game-feedback (.good/.bad),
  .score-badge, .match-area, .match-col, .match-col-title, .match-drop,
  .match-cards, .match-card, .btn-primary, .btn-ghost

All JavaScript must use var. Use unique IDs/function names (append "G" + short suffix).
Populate all game data arrays with REAL, ACCURATE content about "{T}".

Return ONLY the complete HTML + <script> for this section:

<section id="games" class="section">
  <div class="section-label">Interactive Activities</div>
  <h2>🎮 Games — Test Your Knowledge</h2>
  <div class="game-area">
    <div class="game-tabs">
      <button class="game-tab active" onclick="switchGameX(0)">[Game 1 short title]</button>
      <button class="game-tab" onclick="switchGameX(1)">[Game 2 short title]</button>
      <button class="game-tab" onclick="switchGameX(2)">[Game 3 short title]</button>
    </div>
    <!-- GAME 1 -->
    <div class="game-panel active" id="gameX0">
      [Which-type scenario game with answer reveal]
    </div>
    <!-- GAME 2 -->
    <div class="game-panel" id="gameX1">
      [Product/concept matcher game]
    </div>
    <!-- GAME 3 -->
    <div class="game-panel" id="gameX2">
      [Situation challenge MCQ with score counter]
    </div>
  </div>
</section>

Replace X with a unique 2-character suffix for all IDs and function names.
OUTPUT NOTHING after the closing </section>.""",

# ─────────────────────────────────────────────────────────────────
# §11  FUN FACTS
# ─────────────────────────────────────────────────────────────────
"funfacts": f"""Generate the FUN FACTS section for topic: "{T}"
{focus}

Style reference: #funfacts in Aerobic_Respiration.html.
6 .fact-card elements in a .facts-grid. Each card has:
  • .fact-front: large emoji icon + .fact-tap "👆 Tap to Reveal" text
  • .fact-back: 2-3 sentence surprising fact about "{T}" (hidden initially)
  • onclick="revealFact(this)" toggles .revealed class

Return ONLY this HTML:

<section id="funfacts" class="section">
  <div class="section-label">Did You Know?</div>
  <h2>💡 Fun Facts — Tap to Reveal!</h2>
  <div class="facts-grid">
    <div class="fact-card" onclick="revealFact(this)">
      <div class="fact-front"><div style="font-size:36px;">[emoji]</div></div>
      <div class="fact-tap">👆 Tap to Reveal</div>
      <div class="fact-back">[Surprising fact about "{T}" — 2-3 sentences, bold key numbers/terms]</div>
    </div>
    [Repeat for 5 more cards — all with REAL facts about "{T}"]
  </div>
</section>

OUTPUT NOTHING after the closing </section>.""",

# ─────────────────────────────────────────────────────────────────
# §12  QUICK REVISION
# ─────────────────────────────────────────────────────────────────
"revision": f"""Generate the QUICK REVISION section for topic: "{T}"
{focus}
Context: {c}

Style reference: #revision in Aerobic_Respiration.html.
A .card with:
  • .rev-grid (2×2 or 2×3 grid) of .rev-card elements (classes: .a, .b, .g, .p
    for different background colors)
  • Each .rev-card has .big (key summary phrase) and .small (detail)
  • Below: a section with key equations/word-equations as .equation boxes
    (border-left colored per type)

Return ONLY this HTML:

<section id="revision" class="section">
  <div class="card">
    <div class="section-label">1-Minute Revision</div>
    <h2>📝 Quick Revision</h2>
    <div class="rev-grid" style="margin-bottom:14px;">
      <div class="rev-card a">
        <div class="big">[Key fact about Type A — short, memorable]</div>
        <div class="small">[Sub-detail]</div>
      </div>
      <div class="rev-card b">
        <div class="big">[Key fact about Type B]</div>
        <div class="small">[Sub-detail]</div>
      </div>
      <div class="rev-card g">
        <div class="big">[Third key fact or comparison]</div>
        <div class="small">[Sub-detail]</div>
      </div>
      <div class="rev-card p">
        <div class="big">[Fourth key fact]</div>
        <div class="small">[Sub-detail]</div>
      </div>
    </div>
    <div style="background:var(--bg);border-radius:var(--radius-sm);padding:16px;margin-top:8px;">
      <p style="font-weight:700;margin-bottom:10px;font-size:14px;">📌 Remember These Equations / Definitions:</p>
      <div class="equation" style="margin-bottom:8px;">
        <span style="font-weight:800;">[Left side of equation/definition for "{T}"]</span>
        <span class="eq-arrow">→</span>
        <span>[Right side]</span>
        <span class="eq-plus">+</span>
        <span style="color:var(--green,#16a34a);font-weight:800;">[Key product / energy]</span>
      </div>
      [1-2 more .equation boxes for other key relationships]
    </div>
  </div>
</section>

OUTPUT NOTHING after the closing </section>.""",

# ─────────────────────────────────────────────────────────────────
# §13  QUIZ
# ─────────────────────────────────────────────────────────────────
"quiz": f"""Generate the QUIZ section for topic: "{T}"
{focus}
Context: {c}

Style reference: #quiz in Aerobic_Respiration.html.
EXACTLY 10 questions. Include:
  • .quiz-progress div with .q-dot elements (one per question)
    → .answered class on answered, .correct-q/.wrong-q for right/wrong
  • .quiz-q-num / .quiz-question / .quiz-options (.quiz-opt buttons)
  • .quiz-explain (hidden, shown after answer, with 💡 prefix)
  • quiz-next button (Next Question → / See Results 🏆)
  • Result panel: .quiz-result with .quiz-score-big (big number), .quiz-perf,
    .quiz-perf-sub, and a "🔄 Try Again" button

All JavaScript must use var. Use unique function suffix to avoid collisions.
Populate ALL 10 questions with REAL content about "{T}":
  Q1-3: Easy · Q4-7: Medium · Q8-10: Hard

Return ONLY the complete HTML + <script>:

<section id="quiz" class="section">
  <div class="card">
    <div class="section-label">Test Yourself</div>
    <h2>✏️ Quiz — 10 Questions</h2>
    <div id="quiz-ui-Q">
      <div class="quiz-progress" id="quiz-dots-Q"></div>
      <div class="quiz-q-num" id="quiz-qnum-Q"></div>
      <div class="quiz-question" id="quiz-question-Q"></div>
      <div class="quiz-options" id="quiz-options-Q"></div>
      <div class="quiz-explain" id="quiz-explain-Q"></div>
      <div class="quiz-nav">
        <button class="btn-primary" id="quiz-next-Q" onclick="nextQQ()" style="display:none;">Next Question →</button>
      </div>
    </div>
    <div class="quiz-result" id="quiz-result-Q" style="display:none;">
      <div class="quiz-score-big" id="result-score-Q"></div>
      <div style="font-size:16px;color:var(--muted);margin-bottom:12px;">out of 10</div>
      <div class="quiz-perf" id="result-perf-Q"></div>
      <div class="quiz-perf-sub" id="result-sub-Q"></div>
      <button class="btn-primary" style="margin-top:20px;" onclick="resetQQ()">🔄 Try Again</button>
    </div>
  </div>
</section>
<script>
var questionsQ = [
  {{q:"[Q1 — Easy, about {T}]",opts:["[opt A]","[opt B]","[opt C]","[opt D]"],ans:0,explain:"[explanation]"}},
  {{q:"[Q2 — Easy]",opts:["[A]","[B]","[C]","[D]"],ans:1,explain:"[explanation]"}},
  {{q:"[Q3 — Easy]",opts:["[A]","[B]","[C]","[D]"],ans:2,explain:"[explanation]"}},
  {{q:"[Q4 — Medium]",opts:["[A]","[B]","[C]","[D]"],ans:3,explain:"[explanation]"}},
  {{q:"[Q5 — Medium]",opts:["[A]","[B]","[C]","[D]"],ans:0,explain:"[explanation]"}},
  {{q:"[Q6 — Medium]",opts:["[A]","[B]","[C]","[D]"],ans:1,explain:"[explanation]"}},
  {{q:"[Q7 — Medium]",opts:["[A]","[B]","[C]","[D]"],ans:2,explain:"[explanation]"}},
  {{q:"[Q8 — Hard]",opts:["[A]","[B]","[C]","[D]"],ans:3,explain:"[explanation]"}},
  {{q:"[Q9 — Hard]",opts:["[A]","[B]","[C]","[D]"],ans:0,explain:"[explanation]"}},
  {{q:"[Q10 — Hard]",opts:["[A]","[B]","[C]","[D]"],ans:1,explain:"[explanation]"}}
];
var qIdxQ=0,qScoreQ=0,qAnsweredQ=false;
function initQQ(){{
  var dots=document.getElementById('quiz-dots-Q');
  dots.innerHTML='';
  questionsQ.forEach(function(_,i){{
    var d=document.createElement('div');
    d.className='q-dot';d.textContent=i+1;d.id='dot-Q-'+i;
    dots.appendChild(d);
  }});
  loadQQ();
}}
function loadQQ(){{
  qAnsweredQ=false;
  var q=questionsQ[qIdxQ];
  document.getElementById('quiz-qnum-Q').textContent='Question '+(qIdxQ+1)+' of '+questionsQ.length;
  document.getElementById('quiz-question-Q').textContent=q.q;
  document.getElementById('quiz-explain-Q').style.display='none';
  document.getElementById('quiz-next-Q').style.display='none';
  var opts=document.getElementById('quiz-options-Q');
  opts.innerHTML='';
  q.opts.forEach(function(opt,i){{
    var btn=document.createElement('button');
    btn.className='quiz-opt';btn.textContent=opt;
    btn.onclick=function(){{answerQQ(i,btn);}};
    opts.appendChild(btn);
  }});
}}
function answerQQ(i,btn){{
  if(qAnsweredQ)return;
  qAnsweredQ=true;
  var q=questionsQ[qIdxQ];
  var opts=document.querySelectorAll('#quiz-options-Q .quiz-opt');
  opts.forEach(function(b){{b.disabled=true;}});
  opts[q.ans].classList.add('show-correct');
  if(i===q.ans){{btn.classList.add('selected-correct');qScoreQ++;}}
  else{{btn.classList.add('selected-wrong');}}
  var dot=document.getElementById('dot-Q-'+qIdxQ);
  dot.classList.add('answered');
  dot.classList.add(i===q.ans?'correct-q':'wrong-q');
  var exp=document.getElementById('quiz-explain-Q');
  exp.textContent='💡 '+q.explain;exp.style.display='block';
  var nxt=document.getElementById('quiz-next-Q');
  nxt.style.display='inline-block';
  if(qIdxQ===questionsQ.length-1)nxt.textContent='See Results 🏆';
}}
function nextQQ(){{
  qIdxQ++;
  if(qIdxQ>=questionsQ.length){{showResultsQQ();return;}}
  loadQQ();
}}
function showResultsQQ(){{
  document.getElementById('quiz-ui-Q').style.display='none';
  document.getElementById('quiz-result-Q').style.display='block';
  document.getElementById('result-score-Q').textContent=qScoreQ;
  var perf,sub;
  if(qScoreQ>=9){{perf='Excellent! 🌟';sub='Outstanding! You have mastered {T}!';}}
  else if(qScoreQ>=7){{perf='Very Good! 👍';sub='Great job! Review a few points.';}}
  else if(qScoreQ>=5){{perf='Good! 📖';sub='Revise once more to strengthen your understanding.';}}
  else{{perf="Let's learn again! 💪";sub='Revisit the lesson and try the quiz again!';}}
  document.getElementById('result-perf-Q').textContent=perf;
  document.getElementById('result-sub-Q').textContent=sub;
}}
function resetQQ(){{
  qIdxQ=0;qScoreQ=0;qAnsweredQ=false;
  document.getElementById('quiz-result-Q').style.display='none';
  document.getElementById('quiz-ui-Q').style.display='block';
  document.getElementById('quiz-next-Q').textContent='Next Question →';
  initQQ();
}}
initQQ();
</script>

CRITICAL: Replace ALL [placeholder] text with REAL questions about "{T}".
Replace ans values (0-3) with the actual correct option index.
OUTPUT NOTHING after the closing </script>.""",

# ─────────────────────────────────────────────────────────────────
# §14  FORMULAS  (conditional: mathematical topics)
# ─────────────────────────────────────────────────────────────────
"formulas": f"""Generate the FORMULAS section for topic: "{T}"
{focus}
Context: {c}

Style reference: Inspired by rocketpropulsion.html "Important Points" + tissues.html card style.
Produce 2-5 key formulas as stacked .card elements with:
  • .section-label "Formulas & Equations"
  • Formula name as h3
  • .equation box with proper LaTeX $$...$$ display math
  • A small symbol table (2-col: symbol | meaning + units)
  • .mito-box style highlight box at bottom for the most important formula

Return ONLY this HTML:

<section id="formulas" class="section">
  <div class="section-label">Formulas & Equations</div>
  <h2>📐 Key Formulas</h2>
  <div class="card" style="margin-bottom:16px;">
    <h3 style="color:var(--primary);">[Formula 1 name]</h3>
    <div class="equation" style="justify-content:center;font-size:1.2em;">$$[LaTeX formula]$$</div>
    <table style="width:100%;border-collapse:collapse;margin-top:12px;font-size:14px;">
      <tr style="border-bottom:1px solid var(--border);">
        <td style="padding:6px 10px;font-family:monospace;color:var(--primary);font-weight:700;">$$[sym]$$</td>
        <td style="padding:6px 10px;color:var(--muted);">[meaning — units]</td>
      </tr>
      [1 row per variable]
    </table>
  </div>
  [Repeat for each formula — separate .card per formula]
</section>

OUTPUT NOTHING after the closing </section>.""",

# ─────────────────────────────────────────────────────────────────
# §15  DERIVATION  (conditional: mathematical topics)
# ─────────────────────────────────────────────────────────────────
"derivation": f"""Generate the STEP-BY-STEP DERIVATION for topic: "{T}"
{focus}
Context: {c}

Style reference: rocketpropulsion.html #process (timeline) + Respiration.html clickable pathway.
Produce a 4-8 step derivation using the .timeline / .tl-item / .tl-dot / .tl-card pattern
plus inline LaTeX $$...$$ for each equation step.

Return ONLY this HTML:

<section id="derivation" class="section">
  <div class="card">
    <div class="section-label">Mathematical Derivation</div>
    <h2>📊 Deriving the Key Equation</h2>
    <p style="color:var(--muted);font-size:14px;margin-bottom:20px;">[2-3 sentences: what we derive and why it matters for "{T}"]</p>
    <div class="timeline">
      <div class="tl-item">
        <div class="tl-dot">1</div>
        <div class="tl-card">
          <div class="tl-icon">[emoji]</div>
          <h3>[Step 1 title]</h3>
          <div style="background:var(--bg);border-radius:var(--radius-sm);padding:12px;margin:8px 0;text-align:center;font-size:1.1em;">$$[LaTeX step 1]$$</div>
          <p>[1-2 sentence explanation]</p>
        </div>
      </div>
      [Continue for all steps]
      <div class="tl-item" style="transition-delay:[Ns]">
        <div class="tl-dot">✓</div>
        <div class="tl-card" style="border-color:var(--success,#22c55e);">
          <div class="tl-icon">🎯</div>
          <h3>Final Result</h3>
          <div style="background:var(--green-light,#dcfce7);border-radius:var(--radius-sm);padding:12px;margin:8px 0;text-align:center;font-size:1.2em;">$$[Final equation]$$</div>
          <p>[2-3 sentences on physical significance]</p>
        </div>
      </div>
    </div>
  </div>
</section>

OUTPUT NOTHING after the closing </section>.""",
    }

    return PROMPTS.get(section, f"Generate content for section '{section}' about '{T}'.")


# ══════════════════════════════════════════════════════════════════════════════
#  TAIL STRIPPER
# ══════════════════════════════════════════════════════════════════════════════
def _strip_tail(html: str) -> str:
    last = -1
    for tag in ("</section>", "</div>", "</script>", "</table>", "</ul>"):
        idx = html.rfind(tag)
        if idx != -1:
            candidate = idx + len(tag)
            if candidate > last:
                last = candidate
    if last == -1:
        return html
    tail = html[last:]
    markers = ["###", "Verification", "| ---", "Status", "**", "✅ "]
    if tail.strip() and any(m in tail for m in markers):
        return html[:last]
    return html


# ══════════════════════════════════════════════════════════════════════════════
#  TOPIC CLASSIFIER
# ══════════════════════════════════════════════════════════════════════════════
async def _classify(topic: str) -> Dict:
    prompt = f"""Classify this educational topic.
Topic: "{topic}"
Return ONLY valid JSON (no markdown):
{{
  "category": "mathematical" | "semi_mathematical" | "conceptual",
  "needs_formula": true | false,
  "needs_derivation": true | false,
  "subject": "Biology" | "Physics" | "Chemistry" | "Mathematics" | "Other",
  "level": "Class 9-10" | "Class 11-12" | "University",
  "color_a": "#hex",
  "color_b": "#hex",
  "color_a_light": "#hex",
  "color_b_light": "#hex",
  "label_a": "Type A name",
  "label_b": "Type B name",
  "emoji_a": "emoji",
  "emoji_b": "emoji"
}}
Choose color_a / color_b to reflect the topic's two main types/aspects naturally.
mathematical = needs_formula=true AND needs_derivation=true
semi_mathematical = needs_formula=true, needs_derivation=false
conceptual = needs_formula=false, needs_derivation=false"""
    try:
        msg = await client.messages.create(
            model=MODEL_HAIKU, max_tokens=400,
            messages=[{"role": "user", "content": prompt}])
        raw = re.sub(r'```json\s*|\s*```', '', msg.content[0].text.strip())
        result = json.loads(raw)
        log.info(f"[classify] '{topic}' → {result.get('category')} "
                 f"formula={result.get('needs_formula')} "
                 f"colors={result.get('color_a')}/{result.get('color_b')}")
        return result
    except Exception as e:
        log.warning(f"[classify] failed ({e}), using defaults")
        return {"category": "conceptual", "needs_formula": False,
                "needs_derivation": False, "subject": "Biology",
                "level": "Class 9-10",
                "color_a": "#2563eb", "color_b": "#d97706",
                "color_a_light": "#dbeafe", "color_b_light": "#fef3c7",
                "label_a": "Type A", "label_b": "Type B",
                "emoji_a": "🟢", "emoji_b": "🔴"}


def _section_list(cls: Dict) -> List[str]:
    out: List[str] = []
    for s in ORDERED_SECTIONS:
        if s == "formulas" and not cls.get("needs_formula"):
            continue
        if s == "derivation" and not cls.get("needs_derivation"):
            continue
        out.append(s)
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  CSS — exact reference-file token set
# ══════════════════════════════════════════════════════════════════════════════
def _build_css(cls: Dict, topic: str) -> str:
    ca   = cls.get("color_a",       "#2563eb")
    cb   = cls.get("color_b",       "#d97706")
    cal  = cls.get("color_a_light", "#dbeafe")
    cbl  = cls.get("color_b_light", "#fef3c7")
    subj = cls.get("subject", "Biology")

    # Pick primary from subject
    primary_map = {
        "Biology":     ("#2563eb", "#dbeafe", "#93c5fd"),
        "Physics":     ("#0ea5e9", "#e0f2fe", "#7dd3fc"),
        "Chemistry":   ("#7c3aed", "#ede9fe", "#c4b5fd"),
        "Mathematics": ("#0f766e", "#ccfbf1", "#5eead4"),
        "Other":       ("#4f46e5", "#e0e7ff", "#a5b4fc"),
    }
    primary, plight, pmid = primary_map.get(subj, primary_map["Other"])

    return f"""
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;600;700;800&family=Inter:wght@300;400;500;600;700&display=swap');

/* ══ REFERENCE-FILE TOKEN SET ══ */
:root {{
  --bg:          #f0f4f8;
  --surface:     #ffffff;
  --card:        #ffffff;
  --primary:     {primary};
  --primary-light: {plight};
  --primary-mid: {pmid};
  --secondary:   {ca};
  --color-a:     {ca};
  --color-b:     {cb};
  --color-a-light: {cal};
  --color-b-light: {cbl};
  --success:     #22c55e;
  --green:       #16a34a;
  --green-light: #dcfce7;
  --warning:     #f59e0b;
  --danger:      #ef4444;
  --red:         #dc2626;
  --red-light:   #fee2e2;
  --purple:      #7c3aed;
  --purple-light:#ede9fe;
  --text:        #1a202c;
  --muted:       #64748b;
  --border:      #e2e8f0;
  --shadow:      0 2px 12px rgba(0,0,0,0.08);
  --shadow-md:   0 4px 20px rgba(0,0,0,0.12);
  --radius:      14px;
  --radius-sm:   8px;
}}

/* ══ RESET ══ */
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html{{scroll-behavior:smooth}}
body{{
  font-family:'Inter',system-ui,sans-serif;
  background:var(--bg);color:var(--text);
  line-height:1.7;font-size:16px;overflow-x:hidden;
}}
h1,h2,h3,h4{{font-family:'Poppins',system-ui,sans-serif;line-height:1.25}}
strong{{font-weight:700}}
a{{color:var(--primary);text-decoration:none}}
button{{cursor:pointer;font-family:'Inter',sans-serif}}
img{{max-width:100%;height:auto}}

/* ══ LAYOUT ══ */
.page-wrapper{{display:flex;min-height:100vh}}
.main-content{{
  flex:1;padding:24px 24px 100px 24px;
  max-width:860px;margin:0 auto;width:100%;
}}
section.section{{margin-bottom:36px;scroll-margin-top:80px}}

/* ══ STICKY HEADER (rocketpropulsion style) ══ */
#site-header{{
  position:sticky;top:0;z-index:997;
  background:rgba(255,255,255,0.95);
  backdrop-filter:blur(10px);
  border-bottom:1px solid var(--border);
  box-shadow:0 2px 12px rgba(0,0,0,0.06);
}}
.header-inner{{
  max-width:960px;margin:0 auto;
  padding:10px 20px;display:flex;
  align-items:center;gap:14px;flex-wrap:wrap;
}}
.header-brand{{display:flex;align-items:center;gap:10px}}
.header-title{{font-size:17px;font-weight:700;color:var(--primary);font-family:'Poppins',sans-serif}}
.header-section{{font-size:13px;color:var(--muted);margin-left:auto}}
.progress-wrap{{width:100%;height:5px;background:var(--border);border-radius:99px;margin-top:4px}}
.progress-bar{{
  height:5px;background:linear-gradient(90deg,var(--primary),var(--color-a));
  border-radius:99px;width:0%;transition:width .3s;
}}

/* ══ SIDE NAV ══ */
#side-nav{{
  position:fixed;left:0;top:50%;transform:translateY(-50%);
  z-index:1000;transition:all .3s ease;
}}
#nav-toggle{{
  background:var(--primary);color:#fff;border:none;
  border-radius:0 var(--radius-sm) var(--radius-sm) 0;
  padding:12px 14px;cursor:pointer;font-size:13px;
  font-weight:600;box-shadow:var(--shadow-md);
  writing-mode:vertical-rl;text-orientation:mixed;
  letter-spacing:1px;
}}
#nav-toggle:hover{{background:var(--secondary)}}
#nav-panel{{
  display:none;background:var(--surface);
  border:1px solid var(--border);border-left:none;
  border-radius:0 var(--radius) var(--radius) 0;
  box-shadow:var(--shadow-md);min-width:190px;
  max-height:80vh;overflow-y:auto;
}}
#nav-panel.open{{display:block}}
.nav-header{{
  padding:14px 16px;font-family:'Poppins',sans-serif;
  font-weight:700;font-size:12px;color:var(--primary);
  border-bottom:1px solid var(--border);
  text-transform:uppercase;letter-spacing:.5px;
}}
.nav-item{{
  display:block;padding:9px 16px;font-size:13px;
  color:var(--muted);cursor:pointer;transition:all .2s;
  border-bottom:1px solid #f1f5f9;border:none;
  background:none;width:100%;text-align:left;
}}
.nav-item:hover,.nav-item.active{{
  background:var(--primary-light);color:var(--primary);font-weight:600;
}}

/* ══ GLOSSARY PANEL ══ */
#glossary-btn{{
  position:fixed;bottom:24px;right:24px;z-index:200;
  background:var(--purple);color:#fff;border:none;
  border-radius:50px;padding:12px 20px;cursor:pointer;
  font-size:14px;font-weight:600;
  box-shadow:var(--shadow-md);transition:transform .2s;
}}
#glossary-btn:hover{{transform:scale(1.05)}}
#glossary-panel{{
  position:fixed;right:0;top:0;height:100vh;width:320px;
  background:var(--surface);border-left:1px solid var(--border);
  z-index:300;transform:translateX(100%);transition:transform .3s ease;
  overflow-y:auto;box-shadow:-4px 0 20px rgba(0,0,0,.12);
}}
#glossary-panel.open{{transform:translateX(0)}}
.glossary-header{{
  position:sticky;top:0;background:var(--purple);color:#fff;
  padding:16px 20px;display:flex;justify-content:space-between;
  align-items:center;font-family:'Poppins',sans-serif;font-weight:700;
}}
.close-btn{{
  background:rgba(255,255,255,.2);border:none;color:#fff;
  border-radius:50%;width:28px;height:28px;cursor:pointer;
  font-size:16px;display:flex;align-items:center;justify-content:center;
}}
#glossary-search{{
  width:calc(100% - 24px);margin:12px;padding:9px 14px;
  border:1.5px solid var(--border);border-radius:var(--radius-sm);
  font-size:14px;font-family:'Inter',sans-serif;outline:none;
}}
#glossary-search:focus{{border-color:var(--purple)}}
.glossary-list{{overflow-y:auto;padding:0 12px 20px}}
.glossary-item{{padding:12px;margin-bottom:8px;background:#f5f3ff;border-radius:var(--radius-sm)}}
.glossary-term{{font-weight:700;color:var(--purple);font-size:14px;margin-bottom:4px}}
.glossary-def{{font-size:13px;color:var(--muted);line-height:1.5}}

/* ══ CARDS / SECTIONS ══ */
.card{{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:24px;
  box-shadow:var(--shadow);margin-bottom:16px;
}}
.section-label{{
  font-size:11px;text-transform:uppercase;letter-spacing:1px;
  color:var(--muted);font-weight:600;margin-bottom:6px;
}}
h1{{font-size:26px;color:var(--text);margin-bottom:10px}}
h2{{font-size:20px;color:var(--text);margin-bottom:12px}}
h3{{font-size:16px;font-weight:700;color:var(--text);margin-bottom:8px}}
p{{font-size:15px;color:var(--text);margin-bottom:10px}}
.chip{{display:inline-block;padding:4px 14px;border-radius:50px;font-size:12px;font-weight:600;margin:2px}}
.chip-a{{background:var(--color-a-light);color:var(--color-a)}}
.chip-b{{background:var(--color-b-light);color:var(--color-b)}}

/* ══ HOOK ══ */
#hook{{
  background:linear-gradient(135deg,#1e3a8a 0%,var(--primary) 60%,var(--color-a) 100%);
  border-radius:var(--radius);padding:32px 28px;
  margin-bottom:28px;position:relative;overflow:hidden;
}}
#hook::before{{
  content:'';position:absolute;right:24px;top:20px;
  font-size:64px;opacity:.18;
}}
#hook .section-label{{color:rgba(255,255,255,.7)}}
#hook h1{{color:#fff;font-size:26px;margin-bottom:10px}}
#hook p{{color:rgba(255,255,255,.9);font-size:16px;margin-bottom:0}}

/* ══ OBJECTIVES ══ */
.obj-list{{list-style:none}}
.obj-list li{{
  padding:8px 0;border-bottom:1px solid var(--border);
  font-size:14px;display:flex;gap:10px;align-items:flex-start;
}}
.obj-list li:last-child{{border:none}}
.obj-list li::before{{content:'✓';color:var(--success);font-weight:700;flex-shrink:0}}

/* ══ SUBTOPICS ══ */
.subtopic-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}}
.subtopic-card{{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:18px;box-shadow:var(--shadow);
}}
.subtopic-icon{{font-size:24px;margin-bottom:8px}}
.subtopic-title{{font-weight:700;font-size:14px;margin-bottom:6px;color:var(--text)}}
.subtopic-desc{{font-size:13px;color:var(--muted);line-height:1.5}}
.kw-list{{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}}
.kw{{
  font-size:11px;background:var(--bg);padding:2px 8px;
  border-radius:50px;color:var(--muted);font-weight:600;
}}

/* ══ FLOWCHART ══ */
.flowchart{{display:flex;flex-direction:column;align-items:center;gap:0;padding:8px 0}}
.flow-box{{border-radius:var(--radius-sm);padding:10px 20px;font-weight:600;font-size:14px;text-align:center;min-width:140px}}
.flow-box.main{{background:var(--text);color:#fff}}
.flow-box.sub{{background:var(--surface);border:2px solid var(--border);color:var(--text)}}
.flow-arrow{{font-size:20px;color:var(--muted);line-height:1}}
.flow-row{{display:flex;gap:32px;align-items:flex-start;flex-wrap:wrap;justify-content:center}}
.flow-col{{display:flex;flex-direction:column;align-items:center;gap:4px}}
.flow-label{{font-size:13px;color:var(--muted);font-style:italic}}

/* ══ EQUATION BOXES ══ */
.equation{{
  background:var(--bg);border-left:4px solid var(--color-a);
  border-radius:var(--radius-sm);padding:14px 18px;
  font-size:15px;font-weight:600;margin:12px 0;
  display:flex;flex-wrap:wrap;gap:6px;align-items:center;
}}
.equation.eq-b{{border-left-color:var(--color-b)}}
.eq-plus,.eq-arrow{{color:var(--muted)}}

/* ══ MITO BOX ══ */
.mito-box{{
  background:#fffbeb;border:2px solid #fcd34d;
  border-radius:var(--radius);padding:16px;
  display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-top:12px;
}}
.mito-text{{flex:1;min-width:160px}}
.mito-label{{font-size:12px;color:var(--color-b);font-weight:700;text-transform:uppercase;margin-bottom:6px}}

/* ══ STEPPER ══ */
.stepper{{display:flex;flex-direction:column;gap:0}}
.step{{
  display:flex;gap:14px;padding:12px 0;
  border-left:3px solid var(--border);margin-left:12px;
  padding-left:20px;position:relative;
}}
.step::before{{
  content:attr(data-n);position:absolute;left:-14px;top:14px;
  width:24px;height:24px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:700;color:#fff;
}}
.step-a::before{{background:var(--color-a)}}
.step-b::before{{background:var(--color-b)}}
.step-title{{font-weight:700;font-size:14px;margin-bottom:2px;color:var(--text)}}
.step-desc{{font-size:13px;color:var(--muted)}}

/* ══ TIMELINE (rocketpropulsion style) ══ */
.timeline{{margin-top:20px;position:relative}}
.timeline::before{{
  content:'';position:absolute;left:32px;top:0;bottom:0;
  width:3px;background:linear-gradient(180deg,var(--primary),var(--color-a));
  border-radius:99px;
}}
.tl-item{{
  display:flex;align-items:flex-start;gap:24px;
  margin-bottom:36px;position:relative;padding-left:72px;
}}
.tl-dot{{
  position:absolute;left:12px;top:8px;width:40px;height:40px;
  border-radius:50%;background:linear-gradient(135deg,var(--primary),var(--color-a));
  display:flex;align-items:center;justify-content:center;
  color:#fff;font-size:16px;font-weight:900;box-shadow:0 4px 12px rgba(0,0,0,.15);
  flex-shrink:0;
}}
.tl-card{{
  background:var(--surface);border-radius:var(--radius);
  border:1.5px solid var(--border);padding:18px 22px;
  box-shadow:var(--shadow);flex:1;
}}
.tl-card h3{{color:var(--primary);font-size:16px;margin-bottom:6px}}
.tl-card p{{font-size:14px;color:var(--muted)}}
.tl-icon{{font-size:26px;margin-bottom:6px}}

/* ══ COMPARISON TABLE ══ */
.compare-table-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
.compare-table{{width:100%;border-collapse:collapse;min-width:480px}}
.compare-table th{{padding:12px 16px;text-align:left;font-size:13px;text-transform:uppercase;letter-spacing:.5px;font-weight:700}}
.compare-table th:nth-child(2){{background:var(--color-a);color:#fff}}
.compare-table th:nth-child(3){{background:var(--color-b);color:#fff}}
.compare-table th:first-child{{background:var(--bg);color:var(--muted)}}
.compare-table td{{padding:12px 16px;font-size:14px;border-top:1px solid var(--border);vertical-align:top}}
.compare-table tr:hover td{{background:#f8fafc}}
.compare-table td:first-child{{font-weight:600;color:var(--muted);font-size:13px}}

/* ══ GAMES ══ */
.game-area{{background:var(--bg);border-radius:var(--radius);padding:24px}}
.game-tabs{{display:flex;gap:8px;margin-bottom:20px;flex-wrap:wrap}}
.game-tab{{
  padding:8px 18px;border-radius:50px;border:2px solid var(--border);
  background:var(--surface);cursor:pointer;font-size:13px;
  font-weight:600;transition:all .2s;color:var(--muted);
}}
.game-tab.active{{background:var(--primary);color:#fff;border-color:var(--primary)}}
.game-panel{{display:none}}
.game-panel.active{{display:block}}
.game-question{{font-size:17px;font-weight:700;margin-bottom:16px;line-height:1.4}}
.btn-row{{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px}}
.resp-btn{{
  padding:12px 24px;border-radius:var(--radius-sm);border:2px solid var(--border);
  background:var(--surface);font-size:14px;font-weight:600;cursor:pointer;
  transition:all .2s;color:var(--text);
}}
.resp-btn:hover{{border-color:var(--primary);color:var(--primary);background:var(--primary-light)}}
.resp-btn.correct{{background:var(--green-light);border-color:var(--green);color:var(--green)}}
.resp-btn.wrong{{background:var(--red-light);border-color:var(--red);color:var(--red)}}
.game-feedback{{
  padding:12px 16px;border-radius:var(--radius-sm);font-size:14px;
  font-weight:600;display:none;margin-bottom:16px;
}}
.game-feedback.good{{background:var(--green-light);color:var(--green);display:block}}
.game-feedback.bad{{background:var(--red-light);color:var(--red);display:block}}
.score-badge{{
  display:inline-block;padding:4px 14px;background:var(--purple-light);
  color:var(--purple);border-radius:50px;font-size:13px;font-weight:700;
}}
.match-area{{display:flex;gap:16px;flex-wrap:wrap}}
.match-col{{flex:1;min-width:180px}}
.match-col-title{{
  text-align:center;padding:10px;border-radius:var(--radius-sm);
  font-weight:700;font-size:14px;margin-bottom:8px;
}}
.match-col-title.col-a{{background:var(--color-a);color:#fff}}
.match-col-title.col-b{{background:var(--color-b);color:#fff}}
.match-drop{{
  min-height:80px;border:2px dashed var(--border);
  border-radius:var(--radius-sm);padding:8px;
  display:flex;flex-direction:column;gap:6px;
}}
.match-cards{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}}
.match-card{{
  padding:8px 14px;background:var(--surface);border:2px solid var(--border);
  border-radius:50px;font-size:13px;font-weight:600;cursor:pointer;
  transition:all .2s;user-select:none;
}}
.match-card:hover{{border-color:var(--primary);background:var(--primary-light);color:var(--primary)}}
.match-card.placed-a{{background:var(--color-a-light);border-color:var(--color-a);color:var(--color-a)}}
.match-card.placed-b{{background:var(--color-b-light);border-color:var(--color-b);color:var(--color-b)}}
.match-card.correct-placed{{background:var(--green-light);border-color:var(--green);color:var(--green)}}
.match-card.wrong-placed{{background:var(--red-light);border-color:var(--red);color:var(--red)}}
.btn-primary{{
  padding:12px 24px;background:var(--primary);color:#fff;
  border:none;border-radius:var(--radius-sm);font-size:14px;
  font-weight:700;cursor:pointer;transition:opacity .2s;
}}
.btn-primary:hover{{opacity:.88}}
.btn-ghost{{
  padding:12px 24px;background:var(--surface);color:var(--text);
  border:2px solid var(--border);border-radius:var(--radius-sm);
  font-size:14px;font-weight:600;cursor:pointer;transition:all .2s;
}}
.btn-ghost:hover{{border-color:var(--primary);color:var(--primary)}}

/* ══ FUN FACTS ══ */
.facts-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}}
.fact-card{{
  background:var(--surface);border:2px solid var(--border);
  border-radius:var(--radius);padding:20px;cursor:pointer;
  transition:all .2s;min-height:110px;
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  text-align:center;
}}
.fact-card:hover{{border-color:var(--success);box-shadow:var(--shadow-md);transform:translateY(-2px)}}
.fact-card.revealed{{border-color:var(--success);background:var(--green-light)}}
.fact-front{{font-size:28px;margin-bottom:8px}}
.fact-tap{{font-size:12px;color:var(--muted);font-style:italic}}
.fact-back{{display:none;font-size:14px;color:#166534;line-height:1.5;font-weight:500}}
.fact-card.revealed .fact-front,.fact-card.revealed .fact-tap{{display:none}}
.fact-card.revealed .fact-back{{display:block}}

/* ══ REVISION ══ */
.rev-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.rev-card{{border-radius:var(--radius-sm);padding:16px;text-align:center;font-weight:700}}
.rev-card.a{{background:var(--color-a-light);color:var(--color-a)}}
.rev-card.b{{background:var(--color-b-light);color:var(--color-b)}}
.rev-card.g{{background:var(--green-light);color:var(--green)}}
.rev-card.p{{background:var(--purple-light);color:var(--purple)}}
.rev-card .big{{font-size:18px;font-weight:800}}
.rev-card .small{{font-size:12px;margin-top:4px;opacity:.8}}

/* ══ QUIZ ══ */
.quiz-progress{{display:flex;gap:6px;margin-bottom:20px;flex-wrap:wrap}}
.q-dot{{
  width:28px;height:28px;border-radius:50%;
  background:var(--border);display:flex;align-items:center;justify-content:center;
  font-size:12px;font-weight:700;color:var(--muted);transition:all .3s;
}}
.q-dot.answered{{background:var(--primary);color:#fff}}
.q-dot.correct-q{{background:var(--success)}}
.q-dot.wrong-q{{background:var(--danger)}}
.quiz-q-num{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;font-weight:600}}
.quiz-question{{font-size:18px;font-weight:700;margin-bottom:18px;line-height:1.4}}
.quiz-options{{display:flex;flex-direction:column;gap:10px}}
.quiz-opt{{
  padding:13px 18px;border-radius:var(--radius-sm);border:2px solid var(--border);
  background:var(--surface);font-size:15px;cursor:pointer;
  transition:all .2s;text-align:left;font-weight:500;
}}
.quiz-opt:hover:not(:disabled){{border-color:var(--primary);background:var(--primary-light);color:var(--primary)}}
.quiz-opt.selected-correct{{background:var(--green-light);border-color:var(--success);color:#166534;font-weight:700}}
.quiz-opt.selected-wrong{{background:var(--red-light);border-color:var(--red);color:#7f1d1d;font-weight:700}}
.quiz-opt.show-correct{{background:var(--green-light);border-color:var(--success);color:#166534}}
.quiz-opt:disabled{{cursor:not-allowed}}
.quiz-explain{{
  padding:12px 16px;background:#f0fdf4;border:1px solid #bbf7d0;
  border-radius:var(--radius-sm);font-size:14px;color:#166534;
  margin-top:12px;display:none;
}}
.quiz-nav{{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap}}
.quiz-result{{text-align:center;display:none;padding:28px}}
.quiz-score-big{{font-size:64px;font-family:'Poppins',sans-serif;font-weight:800;color:var(--primary)}}
.quiz-perf{{font-size:22px;font-weight:700;margin:8px 0}}
.quiz-perf-sub{{color:var(--muted);font-size:15px}}

/* ══ PROCESS GRID RESPONSIVE ══ */
@media(max-width:640px){{
  .main-content{{padding:16px 14px 80px}}
  h1{{font-size:22px}}
  h2{{font-size:17px}}
  #hook{{padding:22px 18px}}
  .rev-grid{{grid-template-columns:1fr}}
  .flow-row{{flex-direction:column;align-items:center;gap:4px}}
  #side-nav{{top:auto;bottom:60px;transform:none}}
  #nav-toggle{{writing-mode:horizontal-tb;font-size:20px;border-radius:var(--radius-sm) var(--radius-sm) 0 0}}
  .match-area{{flex-direction:column}}
  .compare-table-wrap{{margin:0 -14px;padding:0 14px}}
  .facts-grid{{grid-template-columns:1fr 1fr}}
  .process-grid{{grid-template-columns:1fr!important}}
}}
@media(max-width:400px){{
  .facts-grid{{grid-template-columns:1fr}}
  .subtopic-grid{{grid-template-columns:1fr}}
}}
"""


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE ASSEMBLER
# ══════════════════════════════════════════════════════════════════════════════

def _extract_glossary(hook_html: str) -> List[Dict]:
    """Extract glossary JSON from the <!--GLOSSARY_JSON ... --> comment."""
    m = re.search(r'<!--GLOSSARY_JSON\s*(.*?)\s*-->', hook_html, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(1).strip())
    except Exception:
        return []


def _build_nav_items(sections: List[str]) -> str:
    labels = {
        "hook":          "🎣 Hook",
        "definition":    "📌 Definition",
        "fundamentals":  "🧠 Fundamentals",
        "subtopics":     "🔬 Subtopics",
        "types":         "🌿 Types",
        "deep_sections": "🔍 Deep Dive",
        "formulas":      "📐 Formulas",
        "derivation":    "📊 Derivation",
        "visual":        "🎥 Visual",
        "working":       "⚙️ Working Process",
        "comparison":    "⚖️ Comparison",
        "games":         "🎮 Games",
        "funfacts":      "💡 Fun Facts",
        "revision":      "📝 Revision",
        "quiz":          "✏️ Quiz",
    }
    items = ""
    for s in sections:
        label = labels.get(s, s.replace("_", " ").title())
        items += (f'<div class="nav-item" onclick="scrollToSection(\'{s}\')">'
                  f'{label}</div>\n')
    return items


def _assemble_html(topic: str, sections: Dict[str, str],
                   section_list: List[str], cls: Dict) -> str:
    css = _build_css(cls, topic)
    subj = cls.get("subject", "Biology")
    level = cls.get("level", "Class 10")

    # Glossary from hook section
    glossary_terms = _extract_glossary(sections.get("hook", ""))
    glossary_items = ""
    for t in glossary_terms:
        glossary_items += (
            f'<div class="glossary-item">'
            f'<div class="glossary-term">{t.get("term","")}</div>'
            f'<div class="glossary-def">{t.get("def","")}</div>'
            f'</div>\n'
        )

    nav_items = _build_nav_items(section_list)

    # Section bodies
    section_bodies = ""
    for s in section_list:
        content = sections.get(s, "")
        # For hook, deep_sections, quiz — content already has <section> wrapper
        if s in ("hook", "deep_sections", "quiz"):
            section_bodies += f"\n{content}\n"
        else:
            section_bodies += f"\n{content}\n"

    # MathJax only if needed
    needs_mathjax = any(s in section_list for s in ("formulas", "derivation"))
    mathjax = ""
    if needs_mathjax:
        mathjax = """\
<script>
MathJax={tex:{inlineMath:[['$','$'],['\\\\(','\\\\)']],displayMath:[['$$','$$'],['\\\\[','\\\\]']]},svg:{fontCache:'global'}};
</script>
<script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{topic} — {level} {subj}</title>
{mathjax}
<style>
{css}
</style>
</head>
<body>

<!-- ═══ STICKY HEADER ═══ -->
<header id="site-header">
  <div class="header-inner">
    <div class="header-brand">
      <span style="font-size:22px;">📚</span>
      <span class="header-title">{topic}</span>
    </div>
    <span class="header-section" id="currentSection">{level} {subj}</span>
    <div class="progress-wrap" style="width:100%">
      <div class="progress-bar" id="progressBar"></div>
    </div>
  </div>
</header>

<!-- ═══ SIDE NAV ═══ -->
<div id="side-nav">
  <button id="nav-toggle" onclick="toggleNav()" title="Sections Menu">☰ Sections</button>
  <div id="nav-panel">
    <div class="nav-header">📚 Sections</div>
{nav_items}
  </div>
</div>

<!-- ═══ GLOSSARY BUTTON + PANEL ═══ -->
<button id="glossary-btn" onclick="toggleGlossary()">📖 Glossary</button>
<div id="glossary-panel">
  <div class="glossary-header">
    📖 Fundamentals
    <button class="close-btn" onclick="toggleGlossary()">✕</button>
  </div>
  <input type="search" id="glossary-search" placeholder="Search a term…"
         oninput="filterGlossary(this.value)" />
  <div class="glossary-list" id="glossary-list">
{glossary_items}
  </div>
</div>

<!-- ═══ MAIN CONTENT ═══ -->
<div class="main-content">
{section_bodies}
</div><!-- /main-content -->

<script>
/* ── NAV ── */
function toggleNav(){{
  var p=document.getElementById('nav-panel');
  p.classList.toggle('open');
}}
function scrollToSection(id){{
  var el=document.getElementById(id);
  if(el)el.scrollIntoView({{behavior:'smooth'}});
  document.getElementById('nav-panel').classList.remove('open');
}}

/* ── GLOSSARY ── */
function toggleGlossary(){{
  document.getElementById('glossary-panel').classList.toggle('open');
}}
function filterGlossary(q){{
  var q2=q.toLowerCase();
  document.querySelectorAll('.glossary-item').forEach(function(el){{
    var t=el.textContent.toLowerCase();
    el.style.display=t.includes(q2)?'':'none';
  }});
}}

/* ── FUN FACTS ── */
function revealFact(card){{card.classList.toggle('revealed');}}

/* ── SCROLL PROGRESS + SECTION HIGHLIGHT ── */
var _sectionIds=[{", ".join(f'"{s}"' for s in section_list)}];
window.addEventListener('scroll',function(){{
  var st=document.documentElement.scrollTop;
  var sh=document.documentElement.scrollHeight-window.innerHeight;
  var pct=sh>0?Math.round(st/sh*100):0;
  document.getElementById('progressBar').style.width=pct+'%';
  var cur='';
  _sectionIds.forEach(function(id){{
    var el=document.getElementById(id);
    if(el&&window.scrollY>=el.offsetTop-120)cur=id;
  }});
  document.querySelectorAll('.nav-item').forEach(function(n){{
    n.classList.toggle('active',n.getAttribute('onclick')&&n.getAttribute('onclick').includes(cur));
  }});
  var names={{
    hook:'Hook',definition:'Definition',fundamentals:'Fundamentals',
    subtopics:'Subtopics',types:'Types',deep_sections:'Deep Dive',
    formulas:'Formulas',derivation:'Derivation',visual:'Visual',
    working:'Working Process',comparison:'Comparison',games:'Games',
    funfacts:'Fun Facts',revision:'Revision',quiz:'Quiz'
  }};
  if(cur)document.getElementById('currentSection').textContent=names[cur]||'{level} {subj}';
}});
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
#  CORE GENERATOR CLASS
# ══════════════════════════════════════════════════════════════════════════════

class EduPageGenerator:

    def __init__(self, api_key: Optional[str] = None):
        self._client = (anthropic.AsyncAnthropic(api_key=api_key)
                        if api_key else client)

    async def _generate_section(self, section: str, topic: str,
                                 ctx: str = "",
                                 subtopics: Optional[List[str]] = None,
                                 cls: Optional[Dict] = None,
                                 retries: int = 2) -> str:
        prompt = _build_prompt(section, topic, ctx,
                               subtopics=subtopics, classification=cls)
        model = SECTION_MODEL_MAP.get(section, MODEL_SONNET)
        log.info(f"  [{section}] → {model.split('-')[1]} …")
        for attempt in range(1, retries + 1):
            try:
                msg = await self._client.messages.create(
                    model=model, max_tokens=14000,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}])
                text = msg.content[0].text.strip()
                text = re.sub(r'```html\s*|\s*```', '', text).strip()
                text = _strip_tail(text)
                log.info(f"  ✅ [{section}] {len(text):,} chars")
                return text
            except Exception as e:
                log.warning(f"  ⚠️ [{section}] attempt {attempt}: {e}")
                if attempt < retries:
                    await asyncio.sleep(2)
        log.error(f"  ❌ [{section}] failed")
        return (f'<section id="{section}" class="section">'
                f'<div class="card" style="color:#dc2626;">'
                f'⚠️ Section <strong>{section}</strong> could not be generated.'
                f'</div></section>')

    async def generate_page(self, topic: str,
                            subtopics: Optional[List[str]] = None) -> Dict:
        log.info(f"\n{'═'*60}")
        log.info(f"[EduPage v22] topic='{topic}'")
        if subtopics:
            log.info(f"[EduPage v22] subtopics={subtopics}")
        log.info(f"{'═'*60}")

        # Classify
        log.info("[STAGE 0] Classifying …")
        cls = await _classify(topic)
        ctx = json.dumps(cls)

        # Section list
        sl = _section_list(cls)
        log.info(f"[STAGE 0] Sections: {sl}")

        # Generate all sections in parallel
        log.info(f"[STAGE 1] Generating {len(sl)} sections …")

        async def _gen(s: str) -> str:
            return await self._generate_section(
                s, topic, ctx,
                subtopics=(subtopics if s == "subtopics" else None),
                cls=cls)

        contents = await asyncio.gather(*[_gen(s) for s in sl])
        sections = dict(zip(sl, contents))

        # Assemble
        log.info("[STAGE 2] Assembling HTML …")
        html = _assemble_html(topic, sections, sl, cls)

        total_words = sum(len(c.split()) for c in contents)
        meta = {
            "topic":             topic,
            "is_specific":       _is_specific(topic),
            "sections":          sl,
            "total_sections":    len(sl),
            "total_words":       total_words,
            "read_minutes":      round(total_words / 200, 1),
            "classification":    cls,
            "timestamp":         time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        log.info(f"[DONE] {len(html):,} chars | {total_words:,} words")
        return {"sections": sections, "html": html, "metadata": meta}


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC ENTRY POINTS  (all backward-compatible)
# ══════════════════════════════════════════════════════════════════════════════

async def generate_animation(prompt: str) -> dict:
    """
    Primary async entry point.
    Returns {"title": str, "explanation": str, "animation_code": str}
    """
    if not prompt or not prompt.strip():
        raise ValueError("Prompt cannot be empty")
    prompt = prompt.strip()
    log.info(f"[generate_animation v22] prompt='{prompt}'")

    subtopics = _extract_subtopics(prompt)
    if " -- " in prompt:
        topic = prompt.split(" -- ", 1)[0].strip()
    elif prompt.count(" - ") > 1:
        topic = prompt.split(" - ", 1)[0].strip()
    elif " - " in prompt:
        parts = prompt.split(" - ", 1)
        topic = parts[0].strip()
        if not subtopics:
            sub = parts[1].strip()
            topic = f"{topic} — {sub}" if sub else topic
    else:
        topic = prompt

    gen = EduPageGenerator()
    result = await gen.generate_page(topic=topic,
                                     subtopics=subtopics or None)
    html = result["html"]
    def_html = result["sections"].get("definition", "")
    explanation = re.sub(r"<[^>]+>", " ", def_html)
    explanation = " ".join(explanation.split())[:220]
    if not explanation:
        explanation = f"A complete interactive lesson on {topic}."
    return {"title": topic, "explanation": explanation, "animation_code": html}


async def generate_edu_page(topic: str,
                             output_file: Optional[str] = None,
                             subtopics_list: Optional[List[str]] = None) -> Dict:
    gen = EduPageGenerator()
    result = await gen.generate_page(topic=topic, subtopics=subtopics_list)
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(result["html"])
        log.info(f"💾 Saved → {output_file}")
    return result


def generate_edu_page_sync(topic: str,
                            output_file: Optional[str] = None,
                            subtopics_list: Optional[List[str]] = None) -> Dict:
    return asyncio.run(generate_edu_page(topic=topic,
                                         output_file=output_file,
                                         subtopics_list=subtopics_list))


async def generate_genzet_book_content(topic: str, subtopic: str,
                                        pdf_context: str = "",
                                        subtopics_list: Optional[List[str]] = None) -> dict:
    """Backward-compatible entry point for book-context generation."""
    topic   = (topic   or "").strip()
    subtopic = (subtopic or "").strip()
    if not topic:
        raise ValueError("topic cannot be empty")
    full = (f"{topic} — {subtopic}"
            if subtopic and subtopic.lower() != topic.lower() else topic)
    log.info(f"[generate_genzet_book_content v22] topic='{full}'")
    gen = EduPageGenerator()
    result = await gen.generate_page(topic=full, subtopics=subtopics_list)
    html = result["html"]
    def_html = result["sections"].get("definition", "")
    explanation = re.sub(r"<[^>]+>", " ", def_html)
    explanation = " ".join(explanation.split())[:220]
    if not explanation:
        explanation = f"A complete textbook-grounded lesson on {full}."
    return {"title": full, "explanation": explanation, "animation_code": html}


def subtopics_json_to_genzet_args(subtopics_json_str: str, subtopic: str) -> dict:
    try:
        data = json.loads(subtopics_json_str)
    except Exception:
        items = [s.strip() for s in str(subtopics_json_str).split(",") if s.strip()]
        return {"subtopics_list": items or [subtopic]}
    collected: List[str] = []
    if isinstance(data, list):
        collected = [str(v) for v in data if v]
    elif isinstance(data, dict):
        for val in data.values():
            if isinstance(val, list):
                collected.extend(str(v) for v in val if v)
            elif isinstance(val, str) and val:
                collected.append(val)
    seen: set = set()
    unique = [x for x in collected if x not in seen and not seen.add(x)]  # type: ignore
    return {"subtopics_list": unique or [subtopic]}


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python claude_client.py <topic> [-- sub1, sub2]")
        print("       python claude_client.py <topic> [- sub1 - sub2]")
        print()
        print("Examples:")
        print("  python claude_client.py 'Aerobic and Anaerobic Respiration'")
        print("  python claude_client.py 'Rocket Propulsion'")
        print("  python claude_client.py 'Tissues -- Epithelial, Connective, Muscular'")
        print("  python claude_client.py 'Convective Heat Transfer'")
        sys.exit(1)

    raw = " ".join(sys.argv[1:])
    subs = _extract_subtopics(raw)

    if " -- " in raw:
        topic = raw.split(" -- ", 1)[0].strip()
    elif raw.count(" - ") > 1:
        topic = raw.split(" - ", 1)[0].strip()
    elif " - " in raw:
        topic = raw.split(" - ", 1)[0].strip()
    else:
        topic = raw

    safe = re.sub(r'[^\w\-]', '_', topic.lower())[:60]
    out = f"edupage_{safe}.html"

    print(f"\n{'='*60}")
    print(f"EduPage Generator v22.0 — Exact Reference-HTML Method")
    print(f"{'='*60}")
    print(f"Topic      : {topic}")
    print(f"Subtopics  : {subs if subs else '(auto-detect)'}")
    print(f"Output     : {out}")
    print(f"{'='*60}\n")

    result = generate_edu_page_sync(topic=topic, output_file=out,
                                    subtopics_list=subs or None)
    meta = result["metadata"]
    print(f"\n{'='*60}")
    print(f"COMPLETE")
    print(f"{'='*60}")
    print(f"Sections   : {meta['total_sections']} — {meta['sections']}")
    print(f"Words      : {meta['total_words']:,}")
    print(f"Read time  : {meta['read_minutes']} min")
    print(f"HTML file  : {out}")
    c = meta.get('classification', {})
    print(f"Category   : {c.get('category','?')} | "
          f"formula={c.get('needs_formula','?')} | "
          f"deriv={c.get('needs_derivation','?')}")
    print(f"Colors     : A={c.get('color_a','?')} B={c.get('color_b','?')}")
    print(f"{'='*60}\n")
