"""
╔══════════════════════════════════════════════════════════════════════╗
║  claude_client.py  v23.0  —  Reference-HTML Pipeline Edition        ║
║                                                                      ║
║  Generates educational HTML following the EXACT pipeline,            ║
║  section structure, CSS token set, UI/UX and design language of:    ║
║    • rocketpropulsion.html   (sticky header, timeline, flip facts)   ║
║    • gravition.html          (interactive visuals, revision, quiz)   ║
║    • Monocot___Dicot_Roots.html  (side-nav, flowchart, activities)  ║
║    • tissues.html            (tabs, comparison table, glossary)      ║
║                                                                      ║
║  SECTION PIPELINE (ordered):                                         ║
║   §1  hook          — hero gradient card + GLOSSARY_JSON embed       ║
║   §2  definition    — def cards + learning objectives                ║
║   §3  fundamentals  — CTA card + glossary modal trigger              ║
║   §4  subtopics     — auto-fill card grid with keywords              ║
║   §5  types         — flowchart + type comparison cards              ║
║   §6  deep_sections — per-type deep dive (SVG / process visual)      ║
║   §7  visual        — toggle-based interactive animated diagram      ║
║   §8  working       — stepper / timeline process walkthrough         ║
║   §9  comparison    — side-by-side feature table                     ║
║  §10  activities    — 3-tab game area (scenario, matcher, MCQ)       ║
║  §11  funfacts      — click-to-reveal fact cards                     ║
║  §12  revision      — quick revision blocks + key equations          ║
║  §13  quiz          — 10-Q quiz: progress bar, explanation, result   ║
║  [+§14 formulas, §15 derivation for mathematical topics]            ║
║                                                                      ║
║  CSS TOKENS (exact reference set):                                   ║
║   --primary, --secondary/--accent, --bg, --bg-card, --text,         ║
║   --text-soft, --border, --shadow, --radius, --radius-sm,           ║
║   --success, --warning, --danger  +  topic accent pair               ║
║                                                                      ║
║  ENTRY POINTS (all backward-compatible):                             ║
║   generate_animation()          — primary async entry point          ║
║   generate_edu_page()           — async, saves to file               ║
║   generate_edu_page_sync()      — synchronous wrapper                ║
║   generate_genzet_book_content() — book-context entry point          ║
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
from typing import Optional, Dict, List

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── Client ───────────────────────────────────────────────────────────────────
client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# ══════════════════════════════════════════════════════════════════════════════
#  MODEL CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU  = "claude-haiku-4-5-20251001"

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION REGISTRY — mirrors reference-file section order exactly
# ══════════════════════════════════════════════════════════════════════════════
BASE_SECTIONS: List[str] = [
    "hook",           # §1  Hero gradient card + embedded GLOSSARY_JSON
    "definition",     # §2  Definition cards + learning objectives
    "fundamentals",   # §3  CTA card + glossary modal trigger
    "subtopics",      # §4  Subtopic card grid with keyword chips
    "types",          # §5  Flowchart + type comparison cards
    "deep_sections",  # §6  Per-type deep-dive (process visual, SVG)
    "visual",         # §7  Toggle-based interactive animated diagram
    "working",        # §8  Stepper / timeline working process
    "comparison",     # §9  Side-by-side comparison table
    "activities",     # §10 3-tab game area (scenario MCQ, matcher, challenge)
    "funfacts",       # §11 Click-to-reveal fun-fact cards
    "revision",       # §12 Quick revision blocks + equations
    "quiz",           # §13 10-Q quiz with progress bar, explanation, result card
]

CONDITIONAL_SECTIONS: List[str] = ["formulas", "derivation"]

ORDERED_SECTIONS: List[str] = [
    "hook", "definition", "fundamentals", "subtopics", "types",
    "deep_sections", "formulas", "derivation", "visual", "working",
    "comparison", "activities", "funfacts", "revision", "quiz",
]

# Model assignment: heavy creative sections → Sonnet, lightweight → Haiku
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
    "activities":    MODEL_SONNET,
    "funfacts":      MODEL_HAIKU,
    "revision":      MODEL_HAIKU,
    "quiz":          MODEL_HAIKU,
}

# ══════════════════════════════════════════════════════════════════════════════
#  TOPIC UTILITIES
# ══════════════════════════════════════════════════════════════════════════════
_SPECIFIC_KW = (" in ", " of ", " for ", " during ", " within ",
                " via ", " through ", " using ", " under ")

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
#  MASTER SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT = """\
You write short, simple educational HTML sections for students aged 13–18.

WRITING RULES — ALWAYS:
  • Use plain, everyday English. Write like you're explaining to a friend.
  • Keep every sentence short (max 15 words). No jargon without a quick explanation.
  • Max 2 sentences per paragraph. Never write a wall of text.
  • Use concrete examples (e.g. "like a ball falling off a table").
  • Bold only the 1-2 most important words per card.

DESIGN RULES:
  • Fonts: Nunito (body) + Poppins (headings) from Google Fonts
  • CSS tokens: --primary, --color-a, --color-b, --color-a-light, --color-b-light,
    --bg, --bg-card, --text, --text-soft, --border, --shadow, --radius, --radius-sm,
    --success, --warning, --danger
  • section-title: <h2 class="section-title"><span class="icon">emoji</span> Title</h2>
  • Cards: white bg, border, rounded corners, soft shadow
  • Hook: gradient card, hook-badge eyebrow, h1, 2-sentence hook
  • Subtopic cards: top 4px accent border, .kw keyword chips
  • Fun facts: .fact-card onclick="revealFact(this)", .fact-front / .fact-back
  • Quiz: progress bar, options buttons, explanation on answer, result card
  • Side nav: fixed left, slide-in on toggleNav()
  • Floating glossary: fixed bottom-right button + panel
  • JavaScript: use var (not const/let)
  • All IDs unique per section

OUTPUT RULES:
  1. Return ONLY valid HTML — no markdown, no code fences
  2. End at the last </div> or </script>
  3. Every placeholder replaced with REAL content about the topic
"""

# ══════════════════════════════════════════════════════════════════════════════
#  SECTION PROMPTS — each returns a self-contained HTML snippet
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
"hook": f"""Write the HOOK section for topic: "{T}"
{focus}
Rules: gradient card, hook-badge, h1, 1-2 short punchy sentences. Max 20 words per sentence.
Also embed GLOSSARY_JSON (10 terms) as an HTML comment.

Return ONLY this exact structure with REAL content:

<section id="hook">
  <!--GLOSSARY_JSON
  [
    {{"term":"[Term 1]","def":"[One plain sentence — no jargon]"}},
    {{"term":"[Term 2]","def":"[One plain sentence]"}},
    {{"term":"[Term 3]","def":"[One plain sentence]"}},
    {{"term":"[Term 4]","def":"[One plain sentence]"}},
    {{"term":"[Term 5]","def":"[One plain sentence]"}},
    {{"term":"[Term 6]","def":"[One plain sentence]"}},
    {{"term":"[Term 7]","def":"[One plain sentence]"}},
    {{"term":"[Term 8]","def":"[One plain sentence]"}},
    {{"term":"[Term 9]","def":"[One plain sentence]"}},
    {{"term":"[Term 10]","def":"[One plain sentence]"}}
  ]
  -->
  <span class="hook-badge">[Subject] · [Level, e.g. Class 10]</span>
  <h1>[Emoji] {T}</h1>
  <p>[One surprising fact about "{T}" in simple words. Then one question to make the student curious.]</p>
</section>

Replace ALL placeholders. OUTPUT NOTHING after </section>.""",

# ─────────────────────────────────────────────────────────────────
# §2  DEFINITION + OBJECTIVES
# ─────────────────────────────────────────────────────────────────
"definition": f"""Write the DEFINITION section for: "{T}"
{focus}
Rules: short sentences, simple words, no jargon. Max 2 sentences per card.

Return ONLY:

<section id="definition">
  <h2 class="section-title"><span class="icon">📖</span> What is {T}?</h2>
  <div class="card">
    <div class="def-grid">
      <div class="def-card">
        <h4>[Emoji] [Key concept A — one word or short phrase]</h4>
        <p>[What it is, in 1 sentence. Give a real-life example.]</p>
      </div>
      <div class="def-card" style="border-left-color:var(--color-b);">
        <h4>[Emoji] [Key concept B]</h4>
        <p>[What it is, in 1 sentence. Give a real-life example.]</p>
      </div>
    </div>
    <div class="objectives">
      <h4>🎯 You will learn to…</h4>
      <ul>
        <li>[Short objective 1 — start with a verb, e.g. "Explain what {T} means"]</li>
        <li>[Short objective 2]</li>
        <li>[Short objective 3]</li>
        <li>[Short objective 4]</li>
      </ul>
    </div>
  </div>
</section>

Replace ALL placeholders with REAL content. OUTPUT NOTHING after </section>.""",

# ─────────────────────────────────────────────────────────────────
# §3  FUNDAMENTALS
# ─────────────────────────────────────────────────────────────────
"fundamentals": f"""Write the FUNDAMENTALS section for: "{T}"
{focus}
Rules: 6 key terms, one-line definitions only. Simple words.

Return ONLY:

<section id="fundamentals">
  <h2 class="section-title"><span class="icon">🔑</span> Key Terms</h2>
  <div class="card">
    <p>Know these words and {T} will make perfect sense.</p>
    <div class="glossary-grid" style="margin:16px 0;">
      <div class="glossary-item"><h5>[Term 1]</h5><p>[One plain sentence definition]</p></div>
      <div class="glossary-item"><h5>[Term 2]</h5><p>[One plain sentence definition]</p></div>
      <div class="glossary-item"><h5>[Term 3]</h5><p>[One plain sentence definition]</p></div>
      <div class="glossary-item"><h5>[Term 4]</h5><p>[One plain sentence definition]</p></div>
      <div class="glossary-item"><h5>[Term 5]</h5><p>[One plain sentence definition]</p></div>
      <div class="glossary-item"><h5>[Term 6]</h5><p>[One plain sentence definition]</p></div>
    </div>
    <button id="fundamentals-btn" onclick="toggleGlossary()">📖 Full Glossary</button>
  </div>
</section>

Replace ALL placeholders. OUTPUT NOTHING after </section>.""",

# ─────────────────────────────────────────────────────────────────
# §4  SUBTOPICS GRID
# ─────────────────────────────────────────────────────────────────
"subtopics": f"""Write the SUBTOPICS section for: "{T}"
{focus}
Subtopics to cover: {subtopics or '(pick 6 key subtopics)'}
Rules: each card = 1 sentence description + 2-3 keyword chips. Short and punchy.

Return ONLY:

<section id="subtopics">
  <h2 class="section-title"><span class="icon">🧩</span> Key Subtopics</h2>
  <div class="subtopics-grid">
    <div class="subtopic-card">
      <h4>[Emoji] [Subtopic name]</h4>
      <p>[One sentence: what it is and why it matters.]</p>
      <div class="keywords"><span class="kw">[keyword 1]</span><span class="kw">[keyword 2]</span></div>
    </div>
    [Repeat for all 6 subtopics — REAL content, all different]
  </div>
</section>

Replace ALL placeholders. OUTPUT NOTHING after </section>.""",

# ─────────────────────────────────────────────────────────────────
# §5  TYPES FLOWCHART
# ─────────────────────────────────────────────────────────────────
"types": f"""Write the TYPES section for: "{T}"
{focus}
Rules: flowchart first, then 2 type-cards. Each bullet = 1 short phrase (not a sentence).

Return ONLY:

<section id="types">
  <h2 class="section-title"><span class="icon">🌿</span> Types of {T}</h2>
  <div class="card">
    <div class="flowchart">
      <div class="flow-node">[Emoji] {T}</div>
      <div class="flow-arrow">↓</div>
      <div class="flow-node" style="background:var(--primary);min-width:160px;">Two main types</div>
      <div class="flow-arrow">↓</div>
      <div class="flow-branch">
        <div class="flow-branch-item">
          <div class="flow-arrow">↙</div>
          <div class="flow-node" style="background:var(--color-a);">[Emoji] [Type A name]</div>
          <div class="flow-arrow">↓</div>
          <div class="flow-node sub" style="background:var(--color-a-light);color:var(--text);">[One key feature]<br><small>[e.g. "Found in: muscles"]</small></div>
        </div>
        <div class="flow-branch-item">
          <div class="flow-arrow">↘</div>
          <div class="flow-node" style="background:var(--color-b);">[Emoji] [Type B name]</div>
          <div class="flow-arrow">↓</div>
          <div class="flow-node sub" style="background:var(--color-b-light);color:var(--text);">[One key feature]<br><small>[e.g. "Found in: leaves"]</small></div>
        </div>
      </div>
    </div>
    <div class="type-cards" style="margin-top:20px;">
      <div class="type-card" style="border-color:var(--color-a);">
        <h3 style="color:var(--color-a);">[Emoji] [Type A]</h3>
        <ul>
          <li>[Short feature 1]</li><li>[Short feature 2]</li><li>[Short feature 3]</li>
        </ul>
      </div>
      <div class="type-card" style="border-color:var(--color-b);">
        <h3 style="color:var(--color-b);">[Emoji] [Type B]</h3>
        <ul>
          <li>[Short feature 1]</li><li>[Short feature 2]</li><li>[Short feature 3]</li>
        </ul>
      </div>
    </div>
  </div>
</section>

Replace ALL placeholders with REAL content. OUTPUT NOTHING after </section>.""",

# ─────────────────────────────────────────────────────────────────
# §6  DEEP SECTIONS
# ─────────────────────────────────────────────────────────────────
"deep_sections": f"""Write DEEP DETAIL sections for the 2 main types of: "{T}"
{focus}
Rules: max 3 short sentences per type. Use simple words. Show a mini process flow (3-4 steps).

Return ONLY (2 sections, no outer wrapper):

<section id="type-a">
  <h2 class="section-title" style="color:var(--color-a);"><span class="icon">[emoji]</span> [Type A name]</h2>
  <div class="card" style="border-top:4px solid var(--color-a);">
    <p>[What Type A is. One real-world example. Why it matters. Max 3 sentences total.]</p>
    <div style="background:var(--color-a-light);border-radius:var(--radius-sm);padding:14px;margin:12px 0;display:flex;flex-direction:column;align-items:center;gap:5px;">
      <div style="background:var(--color-a);color:#fff;border-radius:var(--radius-sm);padding:7px 20px;font-weight:700;">[Step 1 — short label]</div>
      <div style="color:var(--color-a);font-size:1.4rem;">↓</div>
      <div style="background:var(--color-a);color:#fff;border-radius:var(--radius-sm);padding:7px 20px;font-weight:700;">[Step 2]</div>
      <div style="color:var(--color-a);font-size:1.4rem;">↓</div>
      <div style="background:#16a34a;color:#fff;border-radius:var(--radius-sm);padding:7px 20px;font-weight:700;">⚡ [Output/product]</div>
    </div>
    <div style="background:#fffbeb;border:2px solid #fcd34d;border-radius:var(--radius-sm);padding:12px 16px;display:flex;align-items:center;gap:12px;">
      <span style="font-size:2rem;">[emoji]</span>
      <div><strong>[Key component name]</strong> — [One sentence: what it does here.]</div>
    </div>
  </div>
</section>

<section id="type-b">
  [Same structure for Type B using var(--color-b) colors]
</section>

Replace ALL placeholders. OUTPUT NOTHING after final </section>.""",

# ─────────────────────────────────────────────────────────────────
# §7  INTERACTIVE PHYSICS SIMULATION
# ─────────────────────────────────────────────────────────────────
"visual": f"""Build a RICH INTERACTIVE PHYSICS SIMULATION for: "{T}"

This is the star feature of the page. Think: a real physics sandbox the student can play with.

WHAT TO BUILD — a canvas-based simulation with:

1. SCENARIO SWITCHER (tabs across top):
   Pick 2-3 real scenarios relevant to "{T}". Examples for gravitation: "Drop from Building", "Earth vs Moon", "Orbit Simulator". For respiration: "Cell Animation", "ATP Factory". For waves: "Wave Tank", "Sound vs Light". Choose scenarios that FIT "{T}" best.

2. EACH SCENARIO must have:
   • An HTML5 <canvas> (600×340px) with real physics animation using requestAnimationFrame
   • Moving objects (falling balls, orbiting planets, bouncing particles, waves, etc.)
   • SLIDERS the student can drag to change physics values in real time:
     — at least 2 sliders (e.g. mass, gravity, speed, angle, frequency)
     — slider change instantly affects the animation
   • A live readout panel showing calculated values (e.g. "Time to fall: 2.3s", "Force: 49N")
   • A RESET button and a PLAY/PAUSE button

3. PHYSICS must be REAL and ACCURATE:
   — Use actual formulas (F=ma, v=u+at, s=½gt², etc.)
   — Show formula used in a small box below canvas
   — Numbers must update live as sliders move

4. VISUAL QUALITY:
   — Draw objects with gradients (ctx.createRadialGradient or linearGradient)
   — Add trails, glow effects, or particle sparks where appropriate
   — Grid lines or reference lines on canvas (subtle, gray)
   — Color-coded labels directly on canvas (ctx.fillText)
   — Smooth 60fps animation

5. JAVASCRIPT RULES:
   — Use var (not const/let)
   — All function/variable names prefixed with "sim_" to avoid collisions
   — requestAnimationFrame loop, cancelAnimationFrame on scenario switch
   — Sliders use oninput= handlers

Return ONLY the complete section HTML + <style> + <script>:

<section id="visual">
  <h2 class="section-title"><span class="icon">🎮</span> Interactive Simulation — {T}</h2>
  <div class="card" style="padding:16px;">
    [scenario tab buttons]
    [canvas + controls for scenario 1]
    [canvas + controls for scenario 2]
    [canvas + controls for scenario 3 if applicable]
  </div>
</section>
<style>
  [All simulation-specific CSS here — canvas border, slider styles, readout panel, tab buttons]
</style>
<script>
  [Complete working simulation JS — real physics, real animation, real sliders]
</script>

CRITICAL: The simulation must actually WORK. Write complete, runnable JavaScript.
No placeholder comments like "// add physics here". Write the full physics loop.
OUTPUT NOTHING after </script>.""",

# ─────────────────────────────────────────────────────────────────
# §8  WORKING PROCESS (stepper / timeline)
# ─────────────────────────────────────────────────────────────────
"working": f"""Write the WORKING PROCESS section for: "{T}"
{focus}
Rules: max 2 sentences per step. Use simple action words. No long explanations.

Return ONLY:

<section id="working">
  <h2 class="section-title"><span class="icon">⚙️</span> How It Works</h2>
  <div class="card">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;">
      <div>
        <h3 style="color:var(--color-a);margin-bottom:14px;">[Emoji] [Type A]</h3>
        <div class="timeline">
          <div class="tl-item">
            <div class="tl-dot" style="background:var(--color-a);">1</div>
            <div class="tl-card" style="border-color:var(--color-a);">
              <div class="tl-icon">[emoji]</div>
              <h3>[Step title — 3 words max]</h3>
              <p>[1 sentence what happens]</p>
            </div>
          </div>
          [3-4 more tl-items for Type A]
        </div>
      </div>
      <div>
        <h3 style="color:var(--color-b);margin-bottom:14px;">[Emoji] [Type B]</h3>
        <div class="timeline">
          [4-5 tl-items for Type B with var(--color-b)]
        </div>
      </div>
    </div>
  </div>
</section>

Replace ALL placeholders. OUTPUT NOTHING after </section>.""",

# ─────────────────────────────────────────────────────────────────
# §9  COMPARISON TABLE
# ─────────────────────────────────────────────────────────────────
"comparison": f"""Write the COMPARISON TABLE for: "{T}"
{focus}
Rules: 6 rows, short values (1-5 words per cell). No sentences in table cells.

Return ONLY:

<section id="comparison">
  <h2 class="section-title"><span class="icon">⚖️</span> [Type A] vs [Type B]</h2>
  <div class="card">
    <div style="overflow-x:auto;">
      <table class="compare-table" style="width:100%;border-collapse:collapse;min-width:420px;">
        <thead>
          <tr>
            <th style="background:var(--bg);color:var(--text-soft);padding:10px 14px;text-align:left;font-size:13px;text-transform:uppercase;">Feature</th>
            <th style="background:var(--color-a);color:#fff;padding:10px 14px;">[Emoji] [Type A]</th>
            <th style="background:var(--color-b);color:#fff;padding:10px 14px;">[Emoji] [Type B]</th>
          </tr>
        </thead>
        <tbody>
          <tr><td style="padding:10px 14px;border-top:1px solid var(--border);font-weight:600;color:var(--text-soft);font-size:13px;">[Feature 1]</td><td style="padding:10px 14px;border-top:1px solid var(--border);">[short value]</td><td style="padding:10px 14px;border-top:1px solid var(--border);">[short value]</td></tr>
          <tr style="background:#f8fafc;"><td style="padding:10px 14px;border-top:1px solid var(--border);font-weight:600;color:var(--text-soft);font-size:13px;">[Feature 2]</td><td style="padding:10px 14px;border-top:1px solid var(--border);">[short value]</td><td style="padding:10px 14px;border-top:1px solid var(--border);">[short value]</td></tr>
          <tr><td style="padding:10px 14px;border-top:1px solid var(--border);font-weight:600;color:var(--text-soft);font-size:13px;">[Feature 3]</td><td style="padding:10px 14px;border-top:1px solid var(--border);">[short value]</td><td style="padding:10px 14px;border-top:1px solid var(--border);">[short value]</td></tr>
          <tr style="background:#f8fafc;"><td style="padding:10px 14px;border-top:1px solid var(--border);font-weight:600;color:var(--text-soft);font-size:13px;">[Feature 4]</td><td style="padding:10px 14px;border-top:1px solid var(--border);">[short value]</td><td style="padding:10px 14px;border-top:1px solid var(--border);">[short value]</td></tr>
          <tr><td style="padding:10px 14px;border-top:1px solid var(--border);font-weight:600;color:var(--text-soft);font-size:13px;">[Feature 5]</td><td style="padding:10px 14px;border-top:1px solid var(--border);">[short value]</td><td style="padding:10px 14px;border-top:1px solid var(--border);">[short value]</td></tr>
          <tr style="background:#f8fafc;"><td style="padding:10px 14px;border-top:1px solid var(--border);font-weight:600;color:var(--text-soft);font-size:13px;">[Feature 6]</td><td style="padding:10px 14px;border-top:1px solid var(--border);">[short value]</td><td style="padding:10px 14px;border-top:1px solid var(--border);">[short value]</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</section>

Replace ALL placeholders with REAL values. OUTPUT NOTHING after </section>.""",

# ─────────────────────────────────────────────────────────────────
# §10  ACTIVITIES (3-tab game area)
# ─────────────────────────────────────────────────────────────────
"activities": f"""Write the 3-TAB ACTIVITIES section for: "{T}"
{focus}
Rules: real data, working JS, use var. All 3 games must be complete and playable.

Activity 1 — "Spot It!" — 5 short scenario cards, student picks Type A or B.
Activity 2 — "Match It!" — 6 cards, click-to-match to correct column.
Activity 3 — "5 Quick Qs" — 5 MCQ with score. Simple questions, simple options.

Return ONLY the complete HTML + <script>:

<section id="activities">
  <h2 class="section-title"><span class="icon">🎮</span> Activities</h2>
  <div class="card">
    <div class="act-tabs">
      <button class="act-tab active" onclick="switchActX(1)">🔍 Spot It!</button>
      <button class="act-tab" onclick="switchActX(2)">🔗 Match It!</button>
      <button class="act-tab" onclick="switchActX(3)">⚡ Quick Quiz</button>
    </div>
    <div class="activity-panel active" id="actX-1">
      [Spot It game — 5 real scenarios about "{T}", pick Type A or B, show ✅/❌ + next button]
    </div>
    <div class="activity-panel" id="actX-2">
      [Match It game — 6 items, click item then click correct column, show score]
    </div>
    <div class="activity-panel" id="actX-3">
      [5-question MCQ, show score out of 5, "Play Again" button]
    </div>
  </div>
</section>
<script>
function switchActX(n) {{
  document.querySelectorAll('.activity-panel').forEach(function(p){{p.classList.remove('active');}});
  document.querySelectorAll('.act-tab').forEach(function(t){{t.classList.remove('active');}});
  document.getElementById('actX-'+n).classList.add('active');
  document.querySelectorAll('.act-tab')[n-1].classList.add('active');
}}
[All game JS — use var, real data arrays about "{T}", complete working logic]
</script>

Replace ALL placeholders. Write COMPLETE working game code. OUTPUT NOTHING after </script>.""",

# ─────────────────────────────────────────────────────────────────
# §11  FUN FACTS
# ─────────────────────────────────────────────────────────────────
"funfacts": f"""Write 6 FUN FACT cards for: "{T}"
{focus}
Rules: each fact = 1-2 short sentences. Use bold for 1 key number or word. Must be a REAL fact.

Return ONLY:

<section id="funfacts">
  <h2 class="section-title"><span class="icon">💡</span> Fun Facts — Tap to Reveal!</h2>
  <div class="facts-grid">
    <div class="fact-card" onclick="revealFact(this)">
      <div class="fact-front"><div style="font-size:2rem">[emoji]</div><p>Tap to Reveal!</p></div>
      <div class="fact-back">[Real fun fact about "{T}". Bold 1 key number or word. Max 2 sentences.]</div>
    </div>
    [Repeat exactly 5 more times — all different facts, all REAL, all short]
  </div>
</section>

Replace ALL placeholders. OUTPUT NOTHING after </section>.""",

# ─────────────────────────────────────────────────────────────────
# §12  QUICK REVISION
# ─────────────────────────────────────────────────────────────────
"revision": f"""Write the QUICK REVISION section for: "{T}"
{focus}
Rules: 4 stat blocks (big number/word + 1-line label), then 2-3 "Remember This" boxes.
Keep everything short. No long sentences.

Return ONLY:

<section id="revision">
  <h2 class="section-title"><span class="icon">📝</span> Quick Revision</h2>
  <div class="card">
    <div class="revision-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:18px;">
      <div class="rev-block" style="background:var(--color-a-light);border-radius:var(--radius-sm);padding:14px;text-align:center;">
        <div style="font-size:1.6rem;font-weight:800;color:var(--color-a);">[Key value/word for Type A]</div>
        <div style="font-size:0.85rem;color:var(--text-soft);margin-top:4px;">[Short label]</div>
      </div>
      <div class="rev-block" style="background:var(--color-b-light);border-radius:var(--radius-sm);padding:14px;text-align:center;">
        <div style="font-size:1.6rem;font-weight:800;color:var(--color-b);">[Key value for Type B]</div>
        <div style="font-size:0.85rem;color:var(--text-soft);margin-top:4px;">[Short label]</div>
      </div>
      <div class="rev-block" style="background:#dcfce7;border-radius:var(--radius-sm);padding:14px;text-align:center;">
        <div style="font-size:1.6rem;font-weight:800;color:#16a34a;">[Third key fact]</div>
        <div style="font-size:0.85rem;color:var(--text-soft);margin-top:4px;">[Short label]</div>
      </div>
      <div class="rev-block" style="background:#ede9fe;border-radius:var(--radius-sm);padding:14px;text-align:center;">
        <div style="font-size:1.6rem;font-weight:800;color:#7c3aed;">[Fourth key fact]</div>
        <div style="font-size:0.85rem;color:var(--text-soft);margin-top:4px;">[Short label]</div>
      </div>
    </div>
    <p style="font-weight:700;font-size:14px;margin-bottom:8px;">📌 Remember:</p>
    <div style="border-left:4px solid var(--color-a);background:#fff;border-radius:var(--radius-sm);padding:10px 14px;margin-bottom:8px;font-size:15px;font-weight:600;">
      <span style="color:var(--color-a);font-weight:800;">[Left term]</span>
      <span style="color:var(--text-soft);margin:0 6px;">→</span>
      <span>[Right term/value]</span>
    </div>
    [1-2 more remember boxes for other key facts]
  </div>
</section>

Replace ALL placeholders with REAL values for "{T}". OUTPUT NOTHING after </section>.""",

# ─────────────────────────────────────────────────────────────────
# §13  QUIZ
# ─────────────────────────────────────────────────────────────────
"quiz": f"""Write a 10-question QUIZ section for: "{T}"
{focus}
Rules: short questions (max 15 words). 4 options each. Brief explanation (1 sentence).

Return ONLY the complete HTML + <script>:

<section id="quiz">
  <h2 class="section-title"><span class="icon">❓</span> Quiz — 10 Questions</h2>
  <div class="card">
    <div class="quiz-wrap" id="quiz-wrapQZ">
      <div style="background:var(--border);border-radius:99px;height:8px;margin-bottom:18px;">
        <div id="quiz-barQZ" style="width:0%;height:8px;background:linear-gradient(90deg,var(--color-a),var(--color-b));border-radius:99px;transition:width .4s;"></div>
      </div>
      <div id="quiz-qnumQZ" style="font-size:13px;color:var(--text-soft);margin-bottom:6px;font-weight:700;text-transform:uppercase;letter-spacing:1px;">Question 1 of 10</div>
      <div id="quiz-qtextQZ" style="font-size:1.1rem;font-weight:700;margin-bottom:16px;line-height:1.4;"></div>
      <div id="quiz-optsQZ" style="display:flex;flex-direction:column;gap:9px;"></div>
      <div id="quiz-explanationQZ" style="display:none;margin-top:12px;padding:10px 14px;background:#f0fdf4;border:1.5px solid #86efac;border-radius:var(--radius-sm);font-size:14px;color:#166534;"></div>
      <button id="quiz-nextQZ" onclick="nextQuizQQ()" style="display:none;margin-top:14px;background:var(--color-a);color:#fff;border:none;padding:10px 24px;border-radius:var(--radius-sm);font-size:15px;font-weight:700;cursor:pointer;">Next →</button>
    </div>
    <div id="quiz-resultQZ" style="display:none;text-align:center;padding:24px;">
      <div id="quiz-score-bigQZ" style="font-size:64px;font-weight:900;color:var(--color-a);"></div>
      <div style="font-size:15px;color:var(--text-soft);margin-bottom:10px;">out of 10</div>
      <div id="quiz-score-msgQZ" style="font-size:1.1rem;font-weight:700;margin-bottom:6px;"></div>
      <div id="quiz-score-subQZ" style="color:var(--text-soft);font-size:14px;margin-bottom:18px;"></div>
      <button onclick="resetQuizQQ()" style="background:var(--color-a);color:#fff;border:none;padding:11px 26px;border-radius:var(--radius-sm);font-size:15px;font-weight:700;cursor:pointer;">🔄 Try Again</button>
    </div>
  </div>
</section>
<script>
var questionsQZ = [
  {{q:"[Q1 — Easy — max 15 words]",opts:["[A]","[B]","[C]","[D]"],ans:0,ex:"[1 sentence why]"}},
  {{q:"[Q2 — Easy]",opts:["[A]","[B]","[C]","[D]"],ans:1,ex:"[1 sentence why]"}},
  {{q:"[Q3 — Easy]",opts:["[A]","[B]","[C]","[D]"],ans:2,ex:"[1 sentence why]"}},
  {{q:"[Q4 — Medium]",opts:["[A]","[B]","[C]","[D]"],ans:3,ex:"[1 sentence why]"}},
  {{q:"[Q5 — Medium]",opts:["[A]","[B]","[C]","[D]"],ans:0,ex:"[1 sentence why]"}},
  {{q:"[Q6 — Medium]",opts:["[A]","[B]","[C]","[D]"],ans:1,ex:"[1 sentence why]"}},
  {{q:"[Q7 — Medium]",opts:["[A]","[B]","[C]","[D]"],ans:2,ex:"[1 sentence why]"}},
  {{q:"[Q8 — Hard]",opts:["[A]","[B]","[C]","[D]"],ans:3,ex:"[1 sentence why]"}},
  {{q:"[Q9 — Hard]",opts:["[A]","[B]","[C]","[D]"],ans:0,ex:"[1 sentence why]"}},
  {{q:"[Q10 — Hard]",opts:["[A]","[B]","[C]","[D]"],ans:1,ex:"[1 sentence why]"}}
];
var qIdxQZ=0,qScoreQZ=0,qAnsweredQZ=false;
function initQuizQQ(){{qIdxQZ=0;qScoreQZ=0;qAnsweredQZ=false;document.getElementById('quiz-wrapQZ').style.display='block';document.getElementById('quiz-resultQZ').style.display='none';loadQQQ();}}
function loadQQQ(){{
  qAnsweredQZ=false;
  var q=questionsQZ[qIdxQZ];
  document.getElementById('quiz-barQZ').style.width=Math.round(qIdxQZ/questionsQZ.length*100)+'%';
  document.getElementById('quiz-qnumQZ').textContent='Question '+(qIdxQZ+1)+' of '+questionsQZ.length;
  document.getElementById('quiz-qtextQZ').textContent=q.q;
  document.getElementById('quiz-explanationQZ').style.display='none';
  document.getElementById('quiz-nextQZ').style.display='none';
  var opts=document.getElementById('quiz-optsQZ');opts.innerHTML='';
  q.opts.forEach(function(opt,i){{
    var btn=document.createElement('button');
    btn.textContent=opt;
    btn.style.cssText='padding:11px 16px;border-radius:var(--radius-sm);border:2px solid var(--border);background:#fff;font-size:15px;text-align:left;cursor:pointer;transition:all .2s;font-family:inherit;font-weight:500;width:100%;';
    btn.onclick=function(){{answerQQQ(i,btn,q);}};
    opts.appendChild(btn);
  }});
}}
function answerQQQ(i,btn,q){{
  if(qAnsweredQZ)return;qAnsweredQZ=true;
  document.querySelectorAll('#quiz-optsQZ button').forEach(function(b){{b.disabled=true;}});
  if(i===q.ans){{btn.style.background='#dcfce7';btn.style.borderColor='#22c55e';btn.style.color='#166534';qScoreQZ++;}}
  else{{btn.style.background='#fee2e2';btn.style.borderColor='#ef4444';btn.style.color='#991b1b';document.querySelectorAll('#quiz-optsQZ button')[q.ans].style.background='#dcfce7';document.querySelectorAll('#quiz-optsQZ button')[q.ans].style.borderColor='#22c55e';}}
  var expl=document.getElementById('quiz-explanationQZ');expl.textContent='💡 '+q.ex;expl.style.display='block';
  var nxt=document.getElementById('quiz-nextQZ');nxt.style.display='inline-block';
  if(qIdxQZ===questionsQZ.length-1)nxt.textContent='See Results 🏆';
}}
function nextQuizQQ(){{qIdxQZ++;if(qIdxQZ>=questionsQZ.length){{showResultsQQ();return;}}loadQQQ();}}
function showResultsQQ(){{
  document.getElementById('quiz-wrapQZ').style.display='none';
  document.getElementById('quiz-resultQZ').style.display='block';
  document.getElementById('quiz-score-bigQZ').textContent=qScoreQZ;
  var perf=qScoreQZ>=9?'Excellent! 🌟':qScoreQZ>=7?'Great! 👍':qScoreQZ>=5?'Good! 📖':"Keep going! 💪";
  var sub=qScoreQZ>=9?'You nailed it!':qScoreQZ>=7?'Almost perfect!':qScoreQZ>=5?'Review a bit more.':'Read again and retry!';
  document.getElementById('quiz-score-msgQZ').textContent=perf;
  document.getElementById('quiz-score-subQZ').textContent=sub;
}}
function resetQuizQQ(){{initQuizQQ();}}
initQuizQQ();
</script>

CRITICAL: Replace ALL [placeholder] text with REAL questions and options about "{T}".
Set ans (0–3) to the correct answer index. Questions max 15 words each.
OUTPUT NOTHING after </script>.""",

# ─────────────────────────────────────────────────────────────────
# §14  FORMULAS  (conditional: mathematical topics)
# ─────────────────────────────────────────────────────────────────
"formulas": f"""Write the FORMULAS section for: "{T}"
{focus}
Rules: 3-4 formulas max. Each formula = equation box + short symbol table (no sentences).

Return ONLY:

<section id="formulas">
  <h2 class="section-title"><span class="icon">📐</span> Key Formulas</h2>
  <div class="card" style="margin-bottom:14px;">
    <h3 style="color:var(--color-a);margin-bottom:8px;">[Formula name]</h3>
    <div style="background:var(--bg);border-left:4px solid var(--color-a);border-radius:var(--radius-sm);padding:12px;font-size:1.2em;text-align:center;margin-bottom:10px;">$$[LaTeX]$$</div>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <tr style="border-bottom:1px solid var(--border);"><td style="padding:5px 10px;font-family:monospace;color:var(--color-a);font-weight:700;">$$[symbol]$$</td><td style="padding:5px 10px;color:var(--text-soft);">[meaning — unit]</td></tr>
      [1 row per variable — short]
    </table>
  </div>
  [Repeat for 2-3 more formulas]
</section>

Replace ALL placeholders. OUTPUT NOTHING after </section>.""",

# ─────────────────────────────────────────────────────────────────
# §15  DERIVATION  (conditional: mathematical topics)
# ─────────────────────────────────────────────────────────────────
"derivation": f"""Write the DERIVATION section for: "{T}"
{focus}
Rules: 5-6 steps max. Each step = 1 equation + 1 sentence explanation. Keep it simple.

Return ONLY:

<section id="derivation">
  <h2 class="section-title"><span class="icon">📊</span> Deriving the Formula</h2>
  <div class="card">
    <p style="color:var(--text-soft);font-size:14px;margin-bottom:18px;">[1 sentence: what we are deriving and why it's useful.]</p>
    <div class="timeline">
      <div class="tl-item">
        <div class="tl-dot">1</div>
        <div class="tl-card">
          <div class="tl-icon">[emoji]</div>
          <h3>[Step title — 3 words]</h3>
          <div style="background:var(--bg);border-radius:var(--radius-sm);padding:10px;margin:6px 0;text-align:center;font-size:1.05em;">$$[equation]$$</div>
          <p>[1 sentence what this step means]</p>
        </div>
      </div>
      [4-5 more steps]
      <div class="tl-item">
        <div class="tl-dot" style="background:var(--success,#22c55e);">✓</div>
        <div class="tl-card" style="border-color:var(--success,#22c55e);">
          <div class="tl-icon">🎯</div>
          <h3>Final Formula</h3>
          <div style="background:#dcfce7;border-radius:var(--radius-sm);padding:10px;margin:6px 0;text-align:center;font-size:1.1em;">$$[final equation]$$</div>
          <p>[1 sentence: what this means in real life]</p>
        </div>
      </div>
    </div>
  </div>
</section>

Replace ALL placeholders. OUTPUT NOTHING after </section>.""",
    }

    return PROMPTS.get(section, f"Generate content for section '{section}' about '{T}' in the reference HTML style.")


# ══════════════════════════════════════════════════════════════════════════════
#  TAIL STRIPPER — removes markdown artefacts that sometimes appear after HTML
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
Return ONLY valid JSON (no markdown fences):
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
        return {
            "category": "conceptual", "needs_formula": False,
            "needs_derivation": False, "subject": "Biology",
            "level": "Class 9-10",
            "color_a": "#2563eb", "color_b": "#d97706",
            "color_a_light": "#dbeafe", "color_b_light": "#fef3c7",
            "label_a": "Type A", "label_b": "Type B",
            "emoji_a": "🟢", "emoji_b": "🔴"
        }


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
#  CSS — exact reference-file token set (Monocot/rocketpropulsion fusion)
# ══════════════════════════════════════════════════════════════════════════════
def _build_css(cls: Dict, topic: str) -> str:
    ca   = cls.get("color_a",       "#2563eb")
    cb   = cls.get("color_b",       "#d97706")
    cal  = cls.get("color_a_light", "#dbeafe")
    cbl  = cls.get("color_b_light", "#fef3c7")
    subj = cls.get("subject", "Biology")

    # Primary palette per subject (matches reference aesthetic)
    primary_map = {
        "Biology":     ("#2D6A4F", "#D8F3DC", "#B7E4C7", "#52B788"),  # green
        "Physics":     ("#1D4ED8", "#DBEAFE", "#93C5FD", "#3B82F6"),  # blue
        "Chemistry":   ("#7C3AED", "#EDE9FE", "#C4B5FD", "#8B5CF6"),  # purple
        "Mathematics": ("#0F766E", "#CCFBF1", "#5EEAD4", "#14B8A6"),  # teal
        "Other":       ("#1E293B", "#F1F5F9", "#CBD5E1", "#64748B"),  # slate
    }
    p, pbg, pmid, phl = primary_map.get(subj, primary_map["Other"])

    return f"""
@import url('https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&family=Poppins:wght@600;700;800&display=swap');

/* ══ EXACT REFERENCE TOKEN SET ══ */
:root {{
  /* Subject palette */
  --primary:       {p};
  --primary-bg:    {pbg};
  --primary-mid:   {pmid};
  --primary-hl:    {phl};
  /* Topic accent pair */
  --color-a:       {ca};
  --color-b:       {cb};
  --color-a-light: {cal};
  --color-b-light: {cbl};
  /* Semantic */
  --success:       #22C55E;
  --warning:       #F59E0B;
  --danger:        #EF4444;
  /* Neutral */
  --bg:            #FAFCFF;
  --bg-card:       #FFFFFF;
  --card:          #FFFFFF;
  --text:          #1E293B;
  --text-soft:     #64748B;
  --border:        #E5E7EB;
  --shadow:        0 4px 24px rgba(0,0,0,0.08);
  --radius:        16px;
  --radius-sm:     10px;
}}

/* ══ RESET ══ */
*,*::before,*::after {{box-sizing:border-box;margin:0;padding:0}}
html {{scroll-behavior:smooth}}
body {{
  font-family:'Nunito',system-ui,sans-serif;
  background:var(--bg);color:var(--text);
  font-size:18px;line-height:1.75;overflow-x:hidden;
}}
h1,h2,h3,h4 {{font-family:'Poppins',sans-serif;line-height:1.3;}}
strong {{font-weight:800}}
a {{color:var(--primary);text-decoration:none}}
button {{cursor:pointer;font-family:inherit}}

/* ══ STICKY HEADER (rocketpropulsion style) ══ */
#site-header {{
  position:sticky;top:0;z-index:997;
  background:rgba(250,252,255,0.92);
  backdrop-filter:blur(12px);
  border-bottom:1px solid var(--border);
  box-shadow:0 2px 16px rgba(0,0,0,0.06);
}}
.header-inner {{
  max-width:960px;margin:0 auto;
  padding:12px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;
}}
.header-brand {{display:flex;align-items:center;gap:10px}}
.header-title {{font-size:18px;font-weight:700;color:var(--primary);font-family:'Poppins',sans-serif;}}
.header-section {{font-size:13px;color:var(--text-soft);margin-left:auto}}
.progress-wrap {{width:100%;height:5px;background:var(--border);border-radius:99px;margin-top:4px}}
.progress-bar {{
  height:5px;background:linear-gradient(90deg,var(--color-a),var(--color-b));
  border-radius:99px;width:0%;transition:width .4s;
}}

/* ══ LEFT SIDE-NAV (Monocot style) ══ */
#nav-toggle {{
  position:fixed;left:0;top:50%;transform:translateY(-50%);
  z-index:1000;background:var(--primary);color:#fff;
  border:none;cursor:pointer;padding:14px 10px;
  border-radius:0 12px 12px 0;font-size:13px;
  writing-mode:vertical-rl;letter-spacing:2px;font-family:'Nunito',sans-serif;
  font-weight:700;text-orientation:mixed;
  box-shadow:2px 0 12px rgba(0,0,0,0.2);transition:background .2s;
}}
#nav-toggle:hover {{background:var(--color-a)}}
#side-nav {{
  position:fixed;left:-230px;top:0;height:100vh;width:230px;
  background:#fff;z-index:999;
  box-shadow:4px 0 24px rgba(0,0,0,0.12);
  transition:left .35s cubic-bezier(.4,0,.2,1);
  display:flex;flex-direction:column;padding:24px 0;
  border-right:2px solid var(--border);
}}
#side-nav.open {{left:0}}
#side-nav h3 {{
  padding:0 20px 16px;color:var(--primary);
  font-size:15px;font-family:'Poppins',sans-serif;
  border-bottom:1px solid var(--border);margin-bottom:8px;
}}
#side-nav a {{
  display:block;padding:9px 20px;color:var(--text-soft);
  text-decoration:none;font-size:15px;font-weight:600;
  transition:all .2s;border-left:3px solid transparent;
}}
#side-nav a:hover,#side-nav a.active {{
  color:var(--primary);background:var(--primary-bg);
  border-left-color:var(--primary);
}}
#nav-close {{
  position:absolute;top:12px;right:12px;
  background:none;border:none;font-size:20px;cursor:pointer;color:var(--text-soft);
}}

/* ══ FLOATING GLOSSARY (Monocot style) ══ */
#glossary-float {{position:fixed;right:18px;bottom:30px;z-index:900}}
#glossary-btn {{
  background:var(--primary);color:#fff;border:none;cursor:pointer;
  padding:10px 22px;border-radius:50px;font-family:'Nunito',sans-serif;
  font-weight:700;font-size:1rem;transition:background .2s;
  display:inline-flex;align-items:center;gap:8px;
  box-shadow:0 4px 20px rgba(0,0,0,0.20);
}}
#glossary-btn:hover {{background:var(--color-a)}}
#glossary-panel {{
  display:none;position:fixed;right:18px;bottom:90px;
  width:310px;max-height:430px;overflow-y:auto;
  background:#fff;border-radius:var(--radius);
  box-shadow:0 8px 40px rgba(0,0,0,0.16);
  border:1px solid var(--border);z-index:1500;padding:20px;
}}
#glossary-panel.open {{display:block}}
#glossary-panel h4 {{color:var(--primary);margin-bottom:12px;font-size:1.05rem;}}
#glossary-panel .g-close {{
  position:absolute;top:12px;right:14px;
  background:none;border:none;font-size:18px;cursor:pointer;color:var(--text-soft);
}}
.gp-item {{margin-bottom:10px;border-bottom:1px solid var(--border);padding-bottom:8px;}}
.gp-item:last-child {{border-bottom:none}}
.gp-item strong {{color:var(--primary);font-size:1rem;}}
.gp-item p {{font-size:0.92rem;color:var(--text-soft);margin-top:2px;}}

/* ══ LAYOUT ══ */
#app {{display:flex;min-height:100vh}}
#content {{flex:1;padding:24px 32px;max-width:940px;margin:0 auto;width:100%;}}
section {{margin-bottom:48px;scroll-margin-top:80px;}}
.section-title {{
  font-size:1.75rem;color:var(--primary);margin-bottom:18px;
  display:flex;align-items:center;gap:10px;
}}
.section-title .icon {{font-size:1.4rem}}
.card {{
  background:var(--bg-card);border-radius:var(--radius);
  box-shadow:var(--shadow);padding:24px;border:1px solid var(--border);
}}

/* ══ HOOK ══ */
#hook {{
  background:linear-gradient(135deg,var(--primary) 0%,var(--color-a) 60%,var(--color-b) 100%);
  border-radius:20px;padding:36px 32px;text-align:center;color:#fff;
  margin-bottom:36px;position:relative;overflow:hidden;
}}
#hook::before {{
  content:'';position:absolute;top:-40px;right:-40px;
  width:160px;height:160px;border-radius:50%;background:rgba(255,255,255,0.08);
}}
#hook h1 {{font-size:clamp(1.8rem,4vw,2.6rem);margin-bottom:10px;}}
#hook p {{font-size:1.1rem;opacity:.93;max-width:640px;margin:0 auto;}}
.hook-badge {{
  display:inline-block;background:rgba(255,255,255,0.22);
  border:1.5px solid rgba(255,255,255,0.5);
  border-radius:20px;padding:4px 14px;font-size:13px;font-weight:700;
  margin-bottom:14px;color:#fff;
}}

/* ══ DEFINITION ══ */
.def-grid {{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px;}}
.def-card {{
  background:var(--primary-bg);border-radius:var(--radius-sm);
  padding:18px;border-left:4px solid var(--color-a);
}}
.def-card h4 {{color:var(--primary);font-size:1.08rem;margin-bottom:6px;}}
.def-card p {{font-size:1rem;color:var(--text-soft);}}
.objectives {{background:var(--color-a-light);border-radius:var(--radius-sm);padding:16px 20px;margin-top:4px;}}
.objectives h4 {{font-size:1rem;color:var(--color-a);margin-bottom:8px;font-family:'Poppins',sans-serif;}}
.objectives ul {{list-style:none;}}
.objectives ul li::before {{content:"✔ ";color:var(--primary);font-weight:800;}}
.objectives ul li {{font-size:1rem;margin-bottom:5px;color:var(--text);}}

/* ══ FUNDAMENTALS / GLOSSARY MODAL ══ */
#fundamentals-btn {{
  background:var(--primary);color:#fff;border:none;cursor:pointer;
  padding:10px 22px;border-radius:50px;font-family:'Nunito',sans-serif;
  font-weight:700;font-size:1rem;transition:background .2s;
  display:inline-flex;align-items:center;gap:8px;
}}
#fundamentals-btn:hover {{background:var(--color-a)}}
.modal-overlay {{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,0.45);
  z-index:2000;align-items:center;justify-content:center;
}}
.modal-overlay.open {{display:flex}}
.modal {{
  background:#fff;border-radius:20px;padding:28px;max-width:580px;
  width:92%;max-height:80vh;overflow-y:auto;
  box-shadow:0 8px 48px rgba(0,0,0,0.22);position:relative;
}}
.modal h3 {{color:var(--primary);font-size:1.2rem;margin-bottom:16px;}}
.modal-close {{
  position:absolute;top:16px;right:18px;
  background:none;border:none;font-size:22px;cursor:pointer;color:var(--text-soft);
}}
.glossary-grid {{display:grid;grid-template-columns:1fr 1fr;gap:12px;}}
.glossary-item {{background:var(--primary-bg);border-radius:var(--radius-sm);padding:12px 14px;}}
.glossary-item h5 {{color:var(--primary);font-size:1rem;margin-bottom:4px;}}
.glossary-item p {{font-size:0.93rem;color:var(--text-soft);line-height:1.5;}}

/* ══ SUBTOPICS ══ */
.subtopics-grid {{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:16px;}}
.subtopic-card {{
  background:#fff;border-radius:var(--radius-sm);padding:18px;
  border:1px solid var(--border);box-shadow:var(--shadow);
  border-top:4px solid var(--color-a);cursor:pointer;
  transition:transform .2s,box-shadow .2s;
}}
.subtopic-card:hover {{transform:translateY(-3px);box-shadow:0 6px 24px rgba(0,0,0,0.12);}}
.subtopic-card h4 {{color:var(--primary);font-size:1.05rem;margin-bottom:6px;}}
.subtopic-card p {{font-size:0.95rem;color:var(--text-soft);line-height:1.5;}}
.subtopic-card .keywords {{margin-top:8px;display:flex;flex-wrap:wrap;gap:5px;}}
.kw {{
  background:var(--primary-bg);color:var(--primary);
  border-radius:50px;padding:2px 10px;font-size:0.75rem;font-weight:700;
}}

/* ══ FLOWCHART ══ */
.flowchart {{display:flex;flex-direction:column;align-items:center;gap:0;}}
.flow-node {{
  background:var(--primary);color:#fff;border-radius:var(--radius-sm);
  padding:12px 28px;font-weight:700;font-size:1.05rem;
  text-align:center;box-shadow:var(--shadow);min-width:180px;
}}
.flow-arrow {{font-size:1.8rem;color:var(--color-a);line-height:1;margin:2px 0;}}
.flow-branch {{display:flex;gap:40px;align-items:flex-start;margin-top:0;flex-wrap:wrap;justify-content:center;}}
.flow-branch-item {{display:flex;flex-direction:column;align-items:center;gap:0;}}
.flow-node.sub {{
  background:var(--primary-bg);color:var(--text);font-size:0.93rem;
  padding:8px 18px;min-width:140px;margin-top:2px;border:2px solid var(--border);
}}
.type-cards {{display:grid;grid-template-columns:1fr 1fr;gap:20px;}}
.type-card {{border-radius:var(--radius-sm);padding:20px;border:2px solid var(--border);}}
.type-card h3 {{font-size:1.1rem;margin-bottom:10px;}}
.type-card ul {{list-style:none;}}
.type-card ul li {{font-size:0.96rem;margin-bottom:4px;color:var(--text-soft);}}
.type-card ul li::before {{content:"• ";font-weight:800;color:var(--primary);}}

/* ══ TIMELINE (rocketpropulsion style) ══ */
.timeline {{margin-top:20px;position:relative;}}
.timeline::before {{
  content:'';position:absolute;left:20px;top:0;bottom:0;
  width:3px;background:linear-gradient(180deg,var(--color-a),var(--color-b));
  border-radius:99px;
}}
.tl-item {{
  display:flex;align-items:flex-start;gap:20px;
  margin-bottom:28px;position:relative;padding-left:60px;
}}
.tl-dot {{
  position:absolute;left:4px;top:8px;width:36px;height:36px;
  border-radius:50%;background:linear-gradient(135deg,var(--color-a),var(--color-b));
  display:flex;align-items:center;justify-content:center;
  color:#fff;font-size:15px;font-weight:800;box-shadow:0 4px 12px rgba(0,0,0,0.15);
  flex-shrink:0;
}}
.tl-card {{
  background:#fff;border-radius:var(--radius-sm);
  border:1.5px solid var(--border);padding:16px 20px;
  box-shadow:var(--shadow);flex:1;
}}
.tl-card h3 {{color:var(--primary);font-size:1rem;margin-bottom:5px;}}
.tl-card p {{font-size:0.95rem;color:var(--text-soft);}}
.tl-icon {{font-size:1.6rem;margin-bottom:5px;}}

/* ══ ACTIVITIES (tabs) ══ */
.act-tabs {{display:flex;gap:8px;margin-bottom:20px;flex-wrap:wrap;}}
.act-tab {{
  padding:8px 18px;border-radius:50px;
  border:2px solid var(--border);background:#fff;
  cursor:pointer;font-size:14px;font-weight:700;
  transition:all .2s;color:var(--text-soft);
}}
.act-tab.active {{background:var(--primary);color:#fff;border-color:var(--primary);}}
.activity-panel {{display:none;}}
.activity-panel.active {{display:block;}}

/* ══ MATCH GAME ══ */
.match-area {{display:flex;gap:16px;flex-wrap:wrap;}}
.match-col {{flex:1;min-width:180px;}}
.match-col-title {{
  text-align:center;padding:10px;border-radius:var(--radius-sm);
  font-weight:700;font-size:14px;margin-bottom:8px;
}}
.match-drop {{
  min-height:80px;border:2px dashed var(--border);
  border-radius:var(--radius-sm);padding:8px;
  display:flex;flex-direction:column;gap:6px;
}}
.match-cards {{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;}}
.match-card {{
  padding:8px 14px;background:#fff;border:2px solid var(--border);
  border-radius:50px;font-size:13px;font-weight:600;cursor:pointer;
  transition:all .2s;user-select:none;
}}
.match-card:hover {{border-color:var(--primary);background:var(--primary-bg);color:var(--primary);}}

/* ══ FUN FACTS ══ */
.facts-grid {{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px;}}
.fact-card {{
  background:#fff;border-radius:var(--radius-sm);border:2px solid var(--border);
  padding:20px;cursor:pointer;transition:all .2s;
  min-height:110px;display:flex;flex-direction:column;
  align-items:center;justify-content:center;text-align:center;
}}
.fact-card:hover {{border-color:var(--success);box-shadow:var(--shadow);transform:translateY(-2px);}}
.fact-card.revealed {{border-color:var(--success);background:#f0fdf4;}}
.fact-front {{display:flex;flex-direction:column;align-items:center;gap:6px;}}
.fact-front p {{font-size:0.85rem;color:var(--text-soft);font-style:italic;}}
.fact-back {{display:none;font-size:0.95rem;color:#166534;line-height:1.55;font-weight:600;}}
.fact-card.revealed .fact-front {{display:none;}}
.fact-card.revealed .fact-back {{display:block;}}

/* ══ REVISION ══ */
.revision-grid {{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px;}}
.rev-block {{border-radius:var(--radius-sm);padding:16px;text-align:center;font-weight:700;}}

/* ══ QUIZ ══ */
.quiz-wrap {{padding:8px 0;}}
.quiz-question {{font-size:1.1rem;font-weight:700;margin-bottom:18px;line-height:1.45;}}
.quiz-options {{display:flex;flex-direction:column;gap:10px;}}
.quiz-result {{text-align:center;padding:24px 0;}}
.score-big {{font-size:64px;font-weight:900;color:var(--color-a);font-family:'Poppins',sans-serif;}}
.score-msg {{font-size:1.2rem;font-weight:700;margin:8px 0;}}

/* ══ RESPONSIVE ══ */
@media(max-width:640px){{
  #content {{padding:16px 14px 80px;}}
  h1 {{font-size:1.6rem;}}
  .def-grid,.type-cards,.revision-grid {{grid-template-columns:1fr;}}
  .flow-branch {{flex-direction:column;align-items:center;gap:4px;}}
  #site-header .header-section {{display:none;}}
  .match-area {{flex-direction:column;}}
  .facts-grid {{grid-template-columns:1fr 1fr;}}
  #content {{padding-left:48px;}}
  .timeline::before {{left:16px;}}
  .tl-item {{padding-left:52px;}}
  .tl-dot {{left:0;}}
}}
@media(max-width:400px){{
  .facts-grid,.subtopics-grid {{grid-template-columns:1fr;}}
}}
"""


# ══════════════════════════════════════════════════════════════════════════════
#  GLOSSARY EXTRACTOR — reads from <!--GLOSSARY_JSON ...-->  in hook HTML
# ══════════════════════════════════════════════════════════════════════════════
def _extract_glossary(hook_html: str) -> List[Dict]:
    m = re.search(r'<!--GLOSSARY_JSON\s*(.*?)\s*-->', hook_html, re.DOTALL)
    if not m:
        return []
    try:
        return json.loads(m.group(1).strip())
    except Exception:
        return []


def _build_nav_items(sections: List[str]) -> str:
    labels = {
        "hook":          "🌱 Hook",
        "definition":    "📖 Definition",
        "fundamentals":  "🔑 Fundamentals",
        "subtopics":     "🧬 Subtopics",
        "types":         "🌿 Types",
        "deep_sections": "🔬 Deep Dive",
        "formulas":      "📐 Formulas",
        "derivation":    "📊 Derivation",
        "visual":        "🎥 Visual",
        "working":       "⚙️ Working",
        "comparison":    "⚖️ Comparison",
        "activities":    "🎮 Activities",
        "funfacts":      "💡 Fun Facts",
        "revision":      "📝 Revision",
        "quiz":          "❓ Quiz",
    }
    out = ""
    for s in sections:
        label = labels.get(s, s.replace("_", " ").title())
        out += f'  <a href="#{s}" onclick="closeNav()">{label}</a>\n'
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  HTML ASSEMBLER — stitches CSS, nav, glossary, sections into a full page
# ══════════════════════════════════════════════════════════════════════════════
def _assemble_html(topic: str, sections: Dict[str, str],
                   section_list: List[str], cls: Dict) -> str:
    css  = _build_css(cls, topic)
    subj = cls.get("subject", "Biology")
    level = cls.get("level", "Class 10")
    needs_mathjax = any(s in section_list for s in ("formulas", "derivation"))

    # Glossary from hook
    glossary_terms = _extract_glossary(sections.get("hook", ""))
    gp_items = ""
    for t in glossary_terms:
        gp_items += (f'    <div class="gp-item">'
                     f'<strong>{t.get("term","")}</strong>'
                     f'<p>{t.get("def","")}</p></div>\n')
    if not gp_items:
        gp_items = '    <div class="gp-item"><strong>Loading…</strong><p>Key terms will appear here.</p></div>\n'

    nav_items = _build_nav_items(section_list)
    section_ids_js = ", ".join(f'"{s}"' for s in section_list)

    mathjax = ""
    if needs_mathjax:
        mathjax = """\
<script>
MathJax={tex:{inlineMath:[['$','$'],['\\\\(','\\\\)']],displayMath:[['$$','$$'],['\\\\[','\\\\]']]},svg:{fontCache:'global'}};
</script>
<script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>"""

    section_bodies = "\n".join(sections.get(s, "") for s in section_list)

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

<!-- ═══ LEFT SIDE-NAV (Monocot style) ═══ -->
<button id="nav-toggle" onclick="toggleNav()" aria-label="Toggle navigation">☰ Sections</button>
<nav id="side-nav" aria-label="Page sections">
  <button id="nav-close" onclick="closeNav()" aria-label="Close navigation">✕</button>
  <h3>📚 Sections</h3>
{nav_items}
</nav>

<!-- ═══ FLOATING GLOSSARY (Monocot style) ═══ -->
<div id="glossary-float">
  <div id="glossary-panel">
    <button class="g-close" onclick="toggleGlossary()">✕</button>
    <h4>📖 Quick Glossary</h4>
{gp_items}
  </div>
  <button id="glossary-btn" onclick="toggleGlossary()">📖 Glossary</button>
</div>

<!-- ═══ MAIN CONTENT ═══ -->
<div id="app">
<main id="content">

{section_bodies}

</main>
</div>

<script>
/* ── NAVIGATION ── */
function toggleNav() {{
  document.getElementById('side-nav').classList.toggle('open');
}}
function closeNav() {{
  document.getElementById('side-nav').classList.remove('open');
}}

/* ── GLOSSARY ── */
function toggleGlossary() {{
  document.getElementById('glossary-panel').classList.toggle('open');
}}

/* ── FUN FACTS ── */
function revealFact(card) {{ card.classList.toggle('revealed'); }}

/* ── ACTIVE NAV HIGHLIGHT + SCROLL PROGRESS ── */
var _sids = [{section_ids_js}];
var _snames = {{
  hook:"Hook",definition:"Definition",fundamentals:"Fundamentals",
  subtopics:"Subtopics",types:"Types",deep_sections:"Deep Dive",
  formulas:"Formulas",derivation:"Derivation",visual:"Visual",
  working:"Working Process",comparison:"Comparison",
  activities:"Activities",funfacts:"Fun Facts",revision:"Revision",quiz:"Quiz"
}};
window.addEventListener('scroll', function() {{
  var st = document.documentElement.scrollTop;
  var sh = document.documentElement.scrollHeight - window.innerHeight;
  var pct = sh > 0 ? Math.round(st / sh * 100) : 0;
  document.getElementById('progressBar').style.width = pct + '%';
  var cur = '';
  _sids.forEach(function(id) {{
    var el = document.getElementById(id);
    if (el && window.scrollY >= el.offsetTop - 120) cur = id;
  }});
  document.querySelectorAll('#side-nav a').forEach(function(a) {{
    a.classList.remove('active');
    if (a.getAttribute('href') === '#' + cur) a.classList.add('active');
  }});
  if (cur) {{
    var cs = document.getElementById('currentSection');
    if (cs) cs.textContent = (_snames[cur] || cur) + ' — {level} {subj}';
  }}
}});

/* ── CLOSE NAV ON OUTSIDE CLICK ── */
document.addEventListener('click', function(e) {{
  var nav = document.getElementById('side-nav');
  var toggle = document.getElementById('nav-toggle');
  if (nav.classList.contains('open') && !nav.contains(e.target) && e.target !== toggle) {{
    nav.classList.remove('open');
  }}
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
        model  = SECTION_MODEL_MAP.get(section, MODEL_SONNET)
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
                log.warning(f"  ⚠️  [{section}] attempt {attempt}: {e}")
                if attempt < retries:
                    await asyncio.sleep(2)
        log.error(f"  ❌ [{section}] failed after {retries} attempts")
        return (f'<section id="{section}">'
                f'<div class="card" style="color:#dc2626;border:2px solid #fca5a5;">'
                f'⚠️ Section <strong>{section}</strong> could not be generated.</div>'
                f'</section>')

    async def generate_page(self, topic: str,
                            subtopics: Optional[List[str]] = None) -> Dict:
        log.info(f"\n{'═'*60}")
        log.info(f"[EduPage v23] topic='{topic}'")
        if subtopics:
            log.info(f"[EduPage v23] subtopics={subtopics}")
        log.info(f"{'═'*60}")

        # Stage 0: Classify
        log.info("[STAGE 0] Classifying …")
        cls = await _classify(topic)
        ctx = json.dumps(cls)

        sl = _section_list(cls)
        log.info(f"[STAGE 0] Sections: {sl}")

        # Stage 1: Generate all sections in parallel
        log.info(f"[STAGE 1] Generating {len(sl)} sections …")

        async def _gen(s: str) -> str:
            return await self._generate_section(
                s, topic, ctx,
                subtopics=(subtopics if s == "subtopics" else None),
                cls=cls)

        contents = await asyncio.gather(*[_gen(s) for s in sl])
        sections = dict(zip(sl, contents))

        # Stage 2: Assemble
        log.info("[STAGE 2] Assembling HTML …")
        html = _assemble_html(topic, sections, sl, cls)

        total_words = sum(len(c.split()) for c in contents)
        meta = {
            "topic":          topic,
            "is_specific":    _is_specific(topic),
            "sections":       sl,
            "total_sections": len(sl),
            "total_words":    total_words,
            "read_minutes":   round(total_words / 200, 1),
            "classification": cls,
            "timestamp":      time.strftime("%Y-%m-%d %H:%M:%S"),
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
    log.info(f"[generate_animation v23] prompt='{prompt}'")

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
    result = await gen.generate_page(topic=topic, subtopics=subtopics or None)
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
    """Async entry point; optionally saves HTML to file."""
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
    """Synchronous wrapper around generate_edu_page."""
    return asyncio.run(generate_edu_page(
        topic=topic, output_file=output_file,
        subtopics_list=subtopics_list))


async def generate_genzet_book_content(topic: str, subtopic: str,
                                        pdf_context: str = "",
                                        subtopics_list: Optional[List[str]] = None) -> dict:
    """Backward-compatible entry point for book-context generation."""
    topic    = (topic    or "").strip()
    subtopic = (subtopic or "").strip()
    if not topic:
        raise ValueError("topic cannot be empty")
    full = (f"{topic} — {subtopic}"
            if subtopic and subtopic.lower() != topic.lower() else topic)
    log.info(f"[generate_genzet_book_content v23] topic='{full}'")
    gen = EduPageGenerator()
    result = await gen.generate_page(topic=full, subtopics=subtopics_list)
    html = result["html"]
    def_html = result["sections"].get("definition", "")
    explanation = re.sub(r"<[^>]+>", " ", def_html)
    explanation = " ".join(explanation.split())[:220]
    if not explanation:
        explanation = f"A complete textbook-grounded lesson on {full}."
    return {"title": full, "explanation": explanation, "animation_code": html}


async def generate_ultimate_learning_content(
        topic: str,
        subtopic: str = "",
        pdf_context: str = "",
        subtopics_list: Optional[List[str]] = None,
        output_file: Optional[str] = None,
        **kwargs) -> dict:
    """
    Primary entry point expected by main.py.

    Accepts the same arguments as generate_genzet_book_content plus an
    optional output_file path.  Returns the standard result dict:
        {
          "title":          str,
          "explanation":    str,   # 220-char plain-text summary
          "animation_code": str,   # full HTML page
          "metadata":       dict,
        }
    and additionally writes output.html when output_file is given.
    """
    topic    = (topic    or "").strip()
    subtopic = (subtopic or "").strip()
    if not topic:
        raise ValueError("topic cannot be empty")

    full_topic = (f"{topic} — {subtopic}"
                  if subtopic and subtopic.lower() != topic.lower()
                  else topic)
    log.info(f"[generate_ultimate_learning_content v23] topic='{full_topic}'")

    gen    = EduPageGenerator()
    result = await gen.generate_page(topic=full_topic, subtopics=subtopics_list)
    html   = result["html"]

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(html)
        log.info(f"💾 Saved → {output_file}")

    def_html    = result["sections"].get("definition", "")
    explanation = re.sub(r"<[^>]+>", " ", def_html)
    explanation = " ".join(explanation.split())[:220]
    if not explanation:
        explanation = f"A complete interactive lesson on {full_topic}."

    return {
        "title":          full_topic,
        "explanation":    explanation,
        "animation_code": html,
        "metadata":       result.get("metadata", {}),
    }


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
        print("  python claude_client.py 'Gravitation'")
        print("  python claude_client.py 'Tissues -- Epithelial, Connective, Muscular'")
        print("  python claude_client.py 'Monocot and Dicot Roots'")
        print("  python claude_client.py 'Newton Laws of Motion'")
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
    out  = f"output.html"

    print(f"\n{'='*60}")
    print(f"EduPage Generator v23.0 — Reference-HTML Pipeline")
    print(f"{'='*60}")
    print(f"Topic      : {topic}")
    print(f"Subtopics  : {subs if subs else '(auto-detect)'}")
    print(f"Output     : {out}")
    print(f"{'='*60}\n")

    result = generate_edu_page_sync(
        topic=topic, output_file=out, subtopics_list=subs or None)
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
    print(f"Colors     : A={c.get('color_a','?')}  B={c.get('color_b','?')}")
    print(f"{'='*60}\n")
