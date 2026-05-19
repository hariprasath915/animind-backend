"""
╔══════════════════════════════════════════════════════════════════╗
║     claude_client.py  v18.1.0  —  EduAnimator GOLD STANDARD     ║
║     FULLY RE-ENGINEERED ANIMATION GENERATION ARCHITECTURE        ║
║     10-Stage Pipeline × Claude API × Anthropic                   ║
╠══════════════════════════════════════════════════════════════════╣
║  v18.1 PATCH NOTES (on top of v18.0):                            ║
║                                                                  ║
║  ✅ CHANGED:  Canvas/SVG light theme (white/soft-gray bg)        ║
║  ✅ CHANGED:  "Why It Matters" → 1 concise high-value impact     ║
║  ✅ CHANGED:  Animation section: "From Library" → "Video Vault"  ║
║  ✅ ADDED:    Specific sub-topic detection + focused generation   ║
║               All sections context-aware of exact sub-topic      ║
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

# ════════════════════════════════════════════════════════════════════════
#  MODEL CONSTANTS
# ════════════════════════════════════════════════════════════════════════

MODEL_SONNET = "claude-sonnet-4-20250514"
MODEL_HAIKU  = "claude-haiku-4-5-20251001"

# ════════════════════════════════════════════════════════════════════════
#  SECTION REGISTRY
# ════════════════════════════════════════════════════════════════════════

BASE_SECTIONS: List[str] = [
    "hook",
    "definition",
    "why_matters",
    "core_concepts",
    "types",
    "how_it_works",
    "applications",
    "quiz",
    "animation",
]

CONDITIONAL_SECTIONS: List[str] = ["formulas", "derivation"]

ORDERED_SECTION_TEMPLATE: List[str] = [
    "hook",
    "definition",
    "why_matters",
    "core_concepts",
    "formulas",
    "derivation",
    "types",
    "how_it_works",
    "applications",
    "quiz",
    "animation",
]

SECTION_MODEL_MAP: Dict[str, str] = {
    "hook":          MODEL_SONNET,
    "definition":    MODEL_SONNET,
    "why_matters":   MODEL_SONNET,
    "core_concepts": MODEL_HAIKU,
    "formulas":      MODEL_SONNET,
    "derivation":    MODEL_SONNET,
    "types":         MODEL_SONNET,
    "how_it_works":  MODEL_HAIKU,
    "applications":  MODEL_HAIKU,
    "quiz":          MODEL_HAIKU,
    "animation":     MODEL_HAIKU,
}


# ════════════════════════════════════════════════════════════════════════
#  ▶  v18.1 — SPECIFIC TOPIC DETECTION
#  Detects when the user requests a precise sub-topic such as:
#    "conduction in heat transfer"
#    "total internal reflection in optical fiber"
#    "Bernoulli's principle for fluid dynamics"
# ════════════════════════════════════════════════════════════════════════

_SPECIFIC_TOPIC_KEYWORDS = (
    " in ", " of ", " for ", " during ", " within ",
    " via ", " through ", " using ", " under ",
)

def _is_specific_subtopic(topic: str) -> bool:
    """
    Returns True when the topic string looks like a precise sub-topic
    (e.g. "conduction in heat transfer") rather than a broad subject.
    """
    lower = topic.lower()
    return any(kw in lower for kw in _SPECIFIC_TOPIC_KEYWORDS)


def _build_specific_focus_note(topic: str) -> str:
    """
    Returns a strong instructional note injected into every prompt when
    the user has requested a specific sub-topic.
    """
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
#  SUBTOPIC PARSER (unchanged from v17.x)
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

MASTER OBJECTIVE:
Transform any topic into a complete, student-ready learning experience that is:
- Understandable by a 15-year-old beginner with zero prior knowledge
- Visually rich — the Definition section contains a live interactive simulation
- Deeply engaging — critical thinking built in at every step
- Retention-optimized — structured for comprehension, not just reading
- Formula-complete — mathematical relationships properly explained (when applicable)

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
6. ALL SVG text must use font-family="Verdana, Geneva, sans-serif" — no exceptions
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

    tc = topic_classification or {}
    viz_type = tc.get("visualization_type", "particle_flow")
    phenomenon = tc.get("primary_phenomenon", topic)

    # ── v18.1: inject specific-focus note into every prompt ──
    specific_note = _build_specific_focus_note(topic)

    prompts = {

        # ══════════════════════════════════════════════════════════════════
        # §1 HOOK
        # ══════════════════════════════════════════════════════════════════
        "hook": f"""Generate Section 1: HOOK for topic: "{topic}"
{specific_note}
DEPTH REQUIREMENT — MANDATORY:
- Write 5-6 lines of genuine content depth (NOT a single headline).
- Structure:
  1. One bold opening fact sentence (≤12 words, real startling fact about "{topic}")
  2. A 2-3 sentence paragraph expanding on why that fact is remarkable
  3. 3-4 bullet points listing surprising implications or real-world stakes
- Language: age-appropriate for a 15-year-old, active voice, no jargon

NO ANIMATION. NO SVG. NO CANVAS. NO SCRIPT TAGS.
This section is pure, well-written text that hooks the student's curiosity.

Context: {context[:400]}

Return ONLY this HTML structure. Replace ALL placeholders with REAL content:

<div class="hook-card" data-section="hook">
  <div class="hook-icon">🎯</div>
  <div class="hook-text">
    <p class="hook-lead">[Bold opening fact — max 12 words, real fact about "{topic}"]</p>
    <p>[2-3 sentence expanding paragraph. Active voice. No jargon. Makes the student feel the weight of this topic.]</p>
    <ul class="hook-bullets">
      <li>[Surprising implication 1 — concrete, student-relatable, 1 sentence]</li>
      <li>[Surprising implication 2 — different domain, equally surprising]</li>
      <li>[Surprising implication 3 — forward-looking, future relevance]</li>
      <li>[Surprising implication 4 — optional, most dramatic one]</li>
    </ul>
  </div>
  <button class="img-upload-btn" onclick="uploadSectionImage('hook')">📸 Add Image</button>
  <div class="section-images" id="images-hook"></div>
</div>

CRITICAL: Replace ALL [placeholder] text with real, accurate content about "{topic}".
OUTPUT NOTHING after the closing </div> tag.""",

        # ══════════════════════════════════════════════════════════════════
        # §2 DEFINITION — LIGHT-THEMED INTERACTIVE CANVAS SIMULATION
        #   v18.1 CHANGE: canvas uses LIGHT background (#f0f4ff → #e8edf7)
        #   All colors adapted to be vivid on a light/white canvas.
        # ══════════════════════════════════════════════════════════════════
        "definition": f"""Generate Section 2: SIMPLE DEFINITION + INTERACTIVE CANVAS SIMULATION for topic: "{topic}"
{specific_note}
This section has TWO parts: text definition and a live interactive simulation.
The simulation is THE MAIN VISUAL ENGINE of the entire lesson.

════════════════════════════════════════════
PART 1 — TEXT CONTENT
════════════════════════════════════════════

Write 5-6 lines of genuine depth:
1. Analogy sentence: "[Topic] works like [everyday object] because [reason]"
2. 2-3 sentence formal definition paragraph using the analogy as scaffolding
3. Bullet list of 3 key defining properties

════════════════════════════════════════════
PART 2 — INTERACTIVE SIMULATION (LIGHT THEME)
════════════════════════════════════════════

Topic to simulate: "{topic}"
Core phenomenon: "{phenomenon}"
Visualization type hint: "{viz_type}"

▶ LIGHT THEME RULES (v18.1 — MANDATORY):
- Canvas background: radial gradient center=#f0f4ff (pale blue-white) → edge=#dde3f0
- ALL particle / entity colors must be vivid, saturated, and highly visible on the light bg:
    Hot/energy:   #dc2626 (red), #ea580c (orange), #ca8a04 (amber)
    Cold/low:     #2563eb (blue), #0891b2 (cyan)
    Signal:       #7c3aed (purple), #9333ea
    Biology:      #16a34a (green), #15803d
    Neutral:      #334155 (dark slate)
- Canvas HUD text: dark — ctx.fillStyle = '#1e293b'
- Shadow/glow: ctx.shadowBlur=10-18, ctx.shadowColor = saturated color (same as entity)
- Legend box: rgba(255,255,255,0.85) fill, #334155 border, dark text
- Grid lines (if used): rgba(100,116,139,0.15)
- Do NOT use any near-white or pale color for particles — they must pop on the light bg.

SIMULATION DESIGN — TOPIC ANALYSIS FIRST:
Before writing code, mentally answer:
  Q1: What is the PRIMARY PROCESS that defines "{topic}"?
  Q2: What entities (particles, waves, nodes, objects) are involved?
  Q3: How do they move, interact, or transform over time?
  Q4: What parameter can the user control to see the concept change?
  Q5: What educational labels belong on-canvas?

VISUALIZATION CATEGORIES:
A) PARTICLE FLOW — heat transfer, diffusion, fluid dynamics, electric current
B) WAVE PROPAGATION — sound, light, water waves, seismic, EM radiation
C) NETWORK / SIGNAL — neural networks, circuits, internet, bonds
D) FORCE FIELD — gravity, magnetism, electrostatics, planetary motion
E) BIOLOGICAL PROCESS — photosynthesis, cellular respiration, DNA, mitosis
F) THERMODYNAMIC — entropy, gas laws, Carnot engine, phase transitions
G) MECHANICAL — gears, pendulum, projectile, orbital mechanics, optics

CANVAS SIMULATION REQUIREMENTS:
1. Canvas: width=800, height=420, style="width:100%;height:auto;"
2. Light background as specified above (radial gradient, pale blue-white)
3. 60fps requestAnimationFrame loop
4. Glow on particles: ctx.shadowBlur=12-18, ctx.shadowColor = vivid saturated color
5. Educational HUD drawn on canvas:
   - Top-left: simulation title + live metric (dark text: #1e293b)
   - Bottom-right: legend box (white semi-transparent rect, dark text, color swatches)
   - Labels on key entities (dark font, legible on light bg)
6. Interactive controls BELOW the canvas:
   - ▶/⏸ Play/Pause toggle button
   - ↺ Reset button
   - Speed slider (0.5× to 4×)
   - Topic-specific parameter slider with meaningful label
7. Canvas click: add energy/particle/perturbation at click point
8. All entities defined as constructor functions with update() and draw()
9. Live metric shown in HUD updating each frame

JAVASCRIPT CODE STANDARDS:
- Use var (not const/let) everywhere
- Constructor function pattern (not class)
- roundRect polyfill inside IIFE
- Expose controls as window['fnName_'+ID]
- All IDs use the SAME random 6-char string [ID] throughout

Context: {context[:600]}

Return ONLY this exact HTML structure:

<div class="definition-box">
  <div class="definition-label">📖 What Is It?</div>
  <div class="definition-text">
    <p class="def-analogy">[Analogy sentence for "{topic}"]</p>
    <p>[2-3 sentence formal definition paragraph]</p>
    <ul class="def-properties">
      <li>[Key property 1]</li>
      <li>[Key property 2]</li>
      <li>[Key property 3]</li>
    </ul>
  </div>

  <div class="def-sim-wrapper" id="defSim-[6CHAR_ID]">
    <div class="def-sim-header">
      <span class="def-sim-badge">⚡ Interactive Simulation</span>
      <span class="def-sim-topic">{topic} — Live Model</span>
    </div>
    <div class="def-sim-canvas-wrap">
      <canvas id="defCanvas-[ID]" width="800" height="420"
        style="width:100%;height:auto;display:block;border-radius:0 0 8px 8px;"></canvas>
    </div>
    <div class="def-sim-controls">
      <button class="def-sim-btn" id="defPlayBtn-[ID]" onclick="defToggle_[ID]()">⏸ Pause</button>
      <button class="def-sim-btn secondary" onclick="defReset_[ID]()">↺ Reset</button>
      <div class="def-sim-slider-group">
        <label class="def-sim-label">Speed</label>
        <input type="range" min="1" max="5" value="2" step="1"
          id="defSpeed-[ID]" class="def-sim-slider"
          oninput="defSetSpeed_[ID](this.value)">
        <span class="def-sim-val" id="defSpeedVal-[ID]">2×</span>
      </div>
      <div class="def-sim-slider-group">
        <label class="def-sim-label" id="defParamLabel-[ID]">[Parameter Label]</label>
        <input type="range" min="0" max="100" value="50" step="1"
          id="defParam-[ID]" class="def-sim-slider"
          oninput="defSetParam_[ID](this.value)">
        <span class="def-sim-val" id="defParamVal-[ID]">50</span>
      </div>
    </div>
    <div class="def-sim-hint">💡 Click on the canvas to interact with the simulation</div>
  </div>

  <script>
  (function() {{
    var ID = '[ID]';
    var canvas = document.getElementById('defCanvas-' + ID);
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    var W = canvas.width, H = canvas.height;
    var running = true;
    var speed = 2;
    var param = 50;
    var frameCount = 0;
    var animId = null;

    // ── roundRect polyfill ──
    if (!ctx.roundRect) {{
      ctx.roundRect = function(x,y,w,h,r) {{
        ctx.beginPath(); ctx.moveTo(x+r,y); ctx.lineTo(x+w-r,y);
        ctx.quadraticCurveTo(x+w,y,x+w,y+r); ctx.lineTo(x+w,y+h-r);
        ctx.quadraticCurveTo(x+w,y+h,x+w-r,y+h); ctx.lineTo(x+r,y+h);
        ctx.quadraticCurveTo(x,y+h,x,y+h-r); ctx.lineTo(x,y+r);
        ctx.quadraticCurveTo(x,y,x+r,y); ctx.closePath();
      }};
    }}

    // ══════════════════════════════════════════════════════
    // COMPLETE LIGHT-THEME SIMULATION FOR "{topic}"
    // ══════════════════════════════════════════════════════

    // ── SIMULATION ENTITIES ──
    // [DEFINE entity constructor(s), initialize arrays]

    // ── BACKGROUND (LIGHT THEME) ──
    function drawBg() {{
      var bg = ctx.createRadialGradient(W/2, H/2, 0, W/2, H/2, W*0.75);
      bg.addColorStop(0, '#f0f4ff');
      bg.addColorStop(1, '#dde3f0');
      ctx.fillStyle = bg;
      ctx.fillRect(0, 0, W, H);
      // Optional subtle grid
      ctx.strokeStyle = 'rgba(100,116,139,0.12)';
      ctx.lineWidth = 0.5;
      for (var gx = 0; gx < W; gx += 40) {{
        ctx.beginPath(); ctx.moveTo(gx,0); ctx.lineTo(gx,H); ctx.stroke();
      }}
      for (var gy = 0; gy < H; gy += 40) {{
        ctx.beginPath(); ctx.moveTo(0,gy); ctx.lineTo(W,gy); ctx.stroke();
      }}
    }}

    // ── LEGEND (light bg, dark text) ──
    function drawLegend() {{
      ctx.save();
      ctx.fillStyle = 'rgba(255,255,255,0.88)';
      ctx.strokeStyle = '#94a3b8';
      ctx.lineWidth = 1;
      ctx.roundRect(W-200, H-90, 188, 78, 8);
      ctx.fill(); ctx.stroke();
      ctx.font = 'bold 11px Verdana';
      ctx.fillStyle = '#334155';
      ctx.fillText('LEGEND', W-186, H-71);
      // [draw swatches + labels for this topic — dark text on white box]
      ctx.restore();
    }}

    // ── HUD (dark text on light canvas) ──
    function drawHUD() {{
      ctx.save();
      ctx.font = 'bold 13px Verdana';
      ctx.fillStyle = 'rgba(255,255,255,0.7)';
      ctx.fillRect(8, 8, 240, 28);
      ctx.fillStyle = '#1e293b';
      ctx.fillText('[Simulation name + live metric]', 15, 27);
      ctx.restore();
    }}

    // ── UPDATE ──
    function update() {{
      frameCount++;
      // [UPDATE ALL ENTITIES]
    }}

    // ── DRAW ENTITIES (vivid colors on light bg) ──
    function drawScene() {{
      // [DRAW entities with shadow glow — use saturated colors e.g. #dc2626, #2563eb]
      // ctx.save(); ctx.shadowBlur=14; ctx.shadowColor='#dc2626'; ...draw... ctx.restore();
    }}

    // ── MAIN LOOP ──
    function loop() {{
      drawBg();
      drawScene();
      drawHUD();
      drawLegend();
      if (running) {{
        for (var s = 0; s < speed; s++) update();
      }}
      animId = requestAnimationFrame(loop);
    }}

    window['defToggle_'+ID] = function() {{
      running = !running;
      var btn = document.getElementById('defPlayBtn-'+ID);
      if (btn) btn.textContent = running ? '⏸ Pause' : '▶ Play';
    }};
    window['defReset_'+ID] = function() {{
      running = true; frameCount = 0;
      var btn = document.getElementById('defPlayBtn-'+ID);
      if (btn) btn.textContent = '⏸ Pause';
    }};
    window['defSetSpeed_'+ID] = function(v) {{
      speed = Math.max(1, parseInt(v));
      var el = document.getElementById('defSpeedVal-'+ID);
      if (el) el.textContent = speed + '×';
    }};
    window['defSetParam_'+ID] = function(v) {{
      param = parseInt(v);
      var el = document.getElementById('defParamVal-'+ID);
      if (el) el.textContent = v;
    }};

    canvas.addEventListener('click', function(e) {{
      var rect = canvas.getBoundingClientRect();
      var mx = (e.clientX - rect.left) * (W / rect.width);
      var my = (e.clientY - rect.top) * (H / rect.height);
      // [ADD ENERGY / PARTICLE / PERTURBATION at (mx, my)]
    }});

    function init() {{
      loop();
    }}
    if (document.readyState === 'loading') {{
      document.addEventListener('DOMContentLoaded', init);
    }} else {{
      setTimeout(init, 80);
    }}
  }})();
  </script>
</div>

CRITICAL IMPLEMENTATION RULES:
1. Replace [ID] with ONE real random 6-char alphanumeric string (same everywhere)
2. Replace EVERY comment with COMPLETE WORKING JavaScript for "{topic}"
3. Simulation MUST animate the actual phenomenon, not a generic animation
4. Light theme: background #f0f4ff→#dde3f0, entities vivid/saturated colors
5. HUD text MUST be dark (#1e293b) — legible on light background
6. Legend box: white semi-transparent, dark text
7. Use var (not const/let)
8. OUTPUT NOTHING after the closing </div> tag""",

        # ══════════════════════════════════════════════════════════════════
        # §3 WHY IT MATTERS — v18.1: ONE concise, high-value impact only
        # ══════════════════════════════════════════════════════════════════
        "why_matters": f"""Generate Section 3: WHY IT MATTERS for topic: "{topic}"
{specific_note}
CONTENT RULES (v18.1 — BREVITY MANDATE):
- Write exactly 2 sentences connecting the topic to the real world.
- Then show exactly ONE high-value, highly specific real-world impact card.
- The impact card MUST name a concrete product, industry, or phenomenon.
- Total section reading time: under 30 seconds.

NO ANIMATION. NO SVG. NO CANVAS. NO SCRIPT TAGS.

Context: {context[:300]}

Return ONLY this HTML. Replace ALL placeholders with real content:

<div class="why-matters-box">
  <div class="why-label">🌟 Why Should You Care?</div>
  <div class="why-text">[Exactly 2 sentences. Concrete. Active voice. Why "{topic}" matters right now — name a real technology or phenomenon it enables.]</div>
  <div class="why-impacts">
    <div class="why-impact-item">
      <div class="why-impact-icon">[emoji]</div>
      <div class="why-impact-content">
        <div class="why-impact-domain">[Specific Domain — e.g. "Fibre-Optic Internet" not just "Technology"]</div>
        <div class="why-impact-desc">[1-2 sentences: the single most impressive, specific use of "{topic}" — name real numbers, products, or effects if possible.]</div>
      </div>
    </div>
  </div>
</div>

CRITICAL: ONE impact card only. Replace ALL [placeholder] text with real content about "{topic}".
OUTPUT NOTHING after the closing </div> tag.""",

        # ══════════════════════════════════════════════════════════════════
        # §5 FORMULAS
        # ══════════════════════════════════════════════════════════════════
        "formulas": f"""Generate Section 5: FORMULAS & EQUATIONS for topic: "{topic}"
{specific_note}
Requirements:
- Identify 2-5 key formulas central to "{topic}"
- Each formula:
  * Proper LaTeX using $$...$$ delimiters
  * Clear title/name
  * Symbol breakdown table (variable, meaning, units)
  * 1 worked numerical example
  * When/why this formula is used

Context: {context[:800]}

Return ONLY this HTML structure with REAL formulas:

<div class="formulas-section">
  <div class="formulas-header">
    <div class="formulas-badge">📐 Mathematical Formulas</div>
    <div class="formulas-title">Key Equations for {topic}</div>
    <div class="formulas-subtitle">Understanding the math behind the concept</div>
  </div>

  <div class="formula-cards-grid">

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
    <div class="formula-when"><strong>When to use:</strong> [Context for "{topic}"]</div>
    <div class="formula-example">
      <div class="example-title">💡 Worked Example:</div>
      <div class="example-text">[Complete numerical worked example for "{topic}"]</div>
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
        <tr><td class="symbol-var">$$[var]$$</td><td class="symbol-desc">[Description]</td></tr>
      </table>
    </div>
    <div class="formula-when"><strong>When to use:</strong> [Context]</div>
    <div class="formula-example">
      <div class="example-title">💡 Worked Example:</div>
      <div class="example-text">[Worked example]</div>
    </div>
    <button class="img-upload-btn" onclick="uploadSectionImage('formula-2')">📸 Add Image</button>
    <div class="section-images" id="images-formula-2"></div>
  </div>

  </div>

  <div class="formulas-practice">
    ✏️ Practice Challenge: Can you rearrange each formula to solve for a different variable?
  </div>
</div>

CRITICAL: Replace ALL [placeholder] text with real, accurate formulas for "{topic}".
OUTPUT NOTHING after the closing </div> tag.""",

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
        # §8 HOW IT WORKS
        # ══════════════════════════════════════════════════════════════════
        "how_it_works": f"""Generate Section: HOW IT WORKS for topic: "{topic}"
{specific_note}
Requirements:
- 4-6 numbered steps. Each: 1-2 sentences max. Active voice. No jargon.
- Static SVG flow diagram (viewBox="0 0 360 200"), 4-6 nodes, left-to-right
- Each node: rounded-rect (rx=10) with linearGradient fill
- Connecting arrows via <marker> arrowhead
- Step labels: font-size 10, fill="#111827", font-weight 700, font-family="Verdana, Geneva, sans-serif"
- Required <defs>: one gradient per node, arrowhead marker
- NO @keyframes, NO animation, NO canvas, NO scripts

Context: {context[:1500]}

Return ONLY:

<div class="how-works-section">
  <div class="how-title">⚙️ How It Works</div>
  <div class="how-steps">
    <div class="step"><div class="step-number">1</div><div class="step-text">[Step 1]</div></div>
    <div class="step"><div class="step-number">2</div><div class="step-text">[Step 2]</div></div>
    [4-6 steps total]
  </div>
  <div class="eli10-visual-wrap">
    <svg viewBox="0 0 360 200" xmlns="http://www.w3.org/2000/svg" class="eli10-svg">
      <defs>
        [gradient defs + arrowhead marker — NO @keyframes]
      </defs>
      [STATIC FLOW DIAGRAM for "{topic}" — ALL text font-family="Verdana, Geneva, sans-serif"]
    </svg>
    <div class="eli10-visual-caption">[Caption max 6 words]</div>
  </div>
</div>

CRITICAL: Real content for "{topic}". NO animations. OUTPUT NOTHING after </div>.""",

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
        # §11 ANIMATION PLAYER — v18.1: "From Library" replaced by "Video Vault"
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

    /* ── TAB SWITCH ── */
    window.animSwitchTab = function(tab) {{
      _mode = tab;
      document.getElementById('animTabUpload').classList.toggle('active', tab==='upload');
      document.getElementById('animTabVault').classList.toggle('active', tab==='vault');
      document.getElementById('animPanelUpload').style.display = tab==='upload' ? '' : 'none';
      document.getElementById('animPanelVault').style.display  = tab==='vault'  ? '' : 'none';
      if (tab === 'vault') vaultRefresh();
    }};

    /* ══════════════════════════════════════
       VIDEO VAULT LOGIC
       Pulls videos from window.__videoVault
       (populated by the Vault backend bridge)
    ══════════════════════════════════════ */
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
        /* Direct video URL from vault */
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
        /* Embedded HTML animation from vault */
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

    /* ── UPLOAD LOGIC ── */
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

    /* ── Vault Bridge: accept vault items from parent window or message ── */
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

    /* ── Restore previously uploaded video ── */
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
  - Any eli10-visual-wrap or eli10-svg elements
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
        """
        Classify the topic — now also aware of specific sub-topics
        so that visualization_type and primary_phenomenon reflect
        the exact sub-topic (e.g. "conduction" not "heat transfer").
        """
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
        log.info(f"[ULTIMATE v18.1] Starting pipeline for: {topic}")
        log.info(f"[ULTIMATE v18.1] Specific sub-topic detected: {_is_specific_subtopic(topic)}")
        if subtopics_list:
            log.info(f"[ULTIMATE v18.1] Core Concepts subtopics: {subtopics_list}")
        log.info(f"{'═'*64}")

        # ── STAGE 0: Topic Classification ──────────────────────────────
        log.info("[STAGE 0] Classifying topic...")
        classification = await self._classify_topic(topic)

        # ── STAGE 1: Content Audit ─────────────────────────────────────
        audit_result = None
        if include_audit:
            log.info("[STAGE 1] Content audit...")
            audit_result = await self.generate_content_audit(topic, existing_content)
            context = json.dumps(audit_result)
        else:
            context = f"Topic: {topic}"

        # ── BUILD SECTION LIST ─────────────────────────────────────────
        lesson_sections = self._build_section_list(classification)
        log.info(f"[STAGE 0] Sections to generate: {lesson_sections}")

        # ── STAGE 2-N: Generate All Sections in Parallel ───────────────
        log.info(f"[STAGES 2-{len(lesson_sections)+1}] Generating {len(lesson_sections)} sections in parallel...")

        async def _gen(s: str) -> str:
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
            "hook":          "🎯 Hook",
            "definition":    "📖 Definition",
            "why_matters":   "🌟 Why It Matters",
            "core_concepts": "🧠 Core Concepts",
            "formulas":      "📐 Formulas",
            "derivation":    "📊 Derivation",
            "types":         "🌿 Types",
            "how_it_works":  "⚙️ How It Works",
            "applications":  "🌍 Applications",
            "quiz":          "❓ Quiz",
            "animation":     "🎬 Animation",
        }

        nav_items = [
            f'<button class="nav-btn" data-section="section-{i}" '
            f'onclick="scrollToSection(\'section-{i}\', this)">'
            f'{section_labels.get(s, s.replace("_"," ").title())}</button>'
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
            # v18.1: show specific sub-topic badge
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
        anim_idx = len(lesson_sections)

        footer_cta = f"""
    <div class="footer-cta">
      <button class="footer-cta-btn quiz-cta"
        onclick="scrollToSection('section-{quiz_idx}', null)">❓ Jump to Quiz</button>
      <button class="footer-cta-btn anim-cta"
        onclick="scrollToSection('section-{anim_idx}', null)">🎬 View Animation</button>
    </div>"""

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{topic} — Ultimate Learning Experience v18.1</title>
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
      <div class="header-badge">🎓 Ultimate Learning Experience v18.1</div>
      <h1 class="page-title">{topic}</h1>
      <p class="page-subtitle">A complete learning journey designed for maximum understanding and retention</p>
    </header>
    {classification_badge}
    {audit_html}
    <nav class="section-nav" id="sectionNav">
      {"".join(nav_items)}
    </nav>
    <main class="main-content">
      {"".join(section_html_parts)}
    </main>
    <footer class="page-footer">
      {footer_cta}
      <p>🧠 Built with the Ultimate Learning Content Generator Pipeline v18.1</p>
      <p>Optimized for comprehension, critical thinking, and retention</p>
    </footer>
  </div>
  <script>
    function scrollToSection(sectionId, btn) {{
      var target = document.getElementById(sectionId);
      if (!target) return;
      var navH = document.getElementById('sectionNav') ? document.getElementById('sectionNav').offsetHeight : 0;
      var top = target.getBoundingClientRect().top + window.pageYOffset - navH - 16;
      window.scrollTo({{ top: top, behavior: 'smooth' }});
      document.querySelectorAll('.nav-btn').forEach(function(b) {{ b.classList.remove('active'); }});
      if (btn) btn.classList.add('active');
    }}
    var _observer = new IntersectionObserver(function(entries) {{
      entries.forEach(function(e) {{
        if (e.isIntersecting) {{
          var id = e.target.id;
          document.querySelectorAll('.nav-btn').forEach(function(b) {{
            b.classList.toggle('active', b.dataset.section === id);
          }});
        }}
      }});
    }}, {{ rootMargin: '-80px 0px -60% 0px', threshold: 0 }});
    document.querySelectorAll('.lesson-section').forEach(function(s) {{ _observer.observe(s); }});
    var _bar = document.getElementById('progressBar');
    window.addEventListener('scroll', function() {{
      var s = window.scrollY;
      var m = document.documentElement.scrollHeight - window.innerHeight;
      if (_bar) _bar.style.width = (m > 0 ? (s/m)*100 : 0) + '%';
    }});
    var _firstBtn = document.querySelector('.nav-btn');
    if (_firstBtn) _firstBtn.classList.add('active');
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
    #  VIDEO VAULT BRIDGE SCRIPT  (v18.1 — replaces library bridge)
    # ──────────────────────────────────────────────────────────────────

    def _get_vault_bridge_script(self) -> str:
        return """
  <script>
    /* ══════════════════════════════════════════════════════════
       VIDEO VAULT BRIDGE  v18.1
       Connects the Video Vault panel to the host application.

       The host/backend should populate window.__videoVault with
       an array of video objects:
         [
           {
             title:      "Conduction Animation",
             src:        "https://vault.example.com/video.mp4",  // OR
             animation_code: "<html>...</html>",                 // embedded HTML
             thumbnail:  "https://...",   // optional
             duration:   "2:34",          // optional
             date:       "2025-01-15"     // optional ISO date
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
    #  CSS — v18.1  (light-themed simulation engine)
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
   ULTIMATE LEARNING CSS  v18.1
   Changes from v18.0:
   + def-sim-wrapper  → LIGHT THEME (white/soft-gray background)
   + why-impacts      → single-card layout
   + vault-*          → Video Vault panel styles
   + cls-badge.specific → specific sub-topic badge
   All v18.0 styles retained.
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
html {{ scroll-behavior: smooth; font-size: 16px; }}
body {{
  font-family: Verdana, Geneva, sans-serif;
  font-size: clamp(1rem,2.5vw,1.05rem);
  line-height: 1.7;
  color: var(--text-dark);
  background: white;
  min-height: 100vh;
}}

/* ── PROGRESS BAR ── */
#progressBar {{
  position: fixed; top: 0; left: 0; height: 4px; width: 0%;
  background: linear-gradient(90deg,var(--primary-blue),var(--info));
  z-index: 9999; transition: width .1s linear; border-radius: 0 2px 2px 0;
}}

/* ── PAGE CONTAINER ── */
.page-container {{
  max-width: 1200px; margin: 0 auto; padding: 48px 24px;
  display: flex; flex-direction: column;
  background:
    url("{svg_pattern_b64}") repeat,
    linear-gradient(135deg, #f0f8ff 0%, #e0f7fa 100%);
  background-size: 100px 100px, cover;
  min-height: 100vh;
}}

/* ── PAGE HEADER ── */
.page-header {{
  text-align: center; margin-bottom: 24px; padding: 48px;
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
/* v18.1: specific sub-topic badge */
.cls-badge.specific {{ background: #fff7ed; color: #c2410c; border: 1.5px solid #fb923c; }}

/* ── AUDIT SUMMARY ── */
.audit-summary {{
  background: #f0f9ff; border: 2px solid var(--primary-blue);
  border-radius: 12px; padding: 16px; margin-bottom: 24px;
}}
.audit-summary h3 {{ color: #1e40af; margin-bottom: 8px; font-family: Verdana,sans-serif; }}
.audit-summary p  {{ color: var(--text-dark); font-family: Verdana,sans-serif; }}

/* ── STICKY NAV ── */
.section-nav {{
  position: sticky; top: 0; z-index: 100;
  display: flex; flex-wrap: wrap; gap: 8px; padding: 16px;
  background: rgba(255,255,255,.97); backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-radius: var(--radius-lg); box-shadow: var(--shadow-md); margin-bottom: 32px; width: 100%;
}}
.nav-btn {{
  padding: 1rem 1.5rem; background: white; border: 2px solid var(--gray-200);
  border-radius: 8px; font-family: Verdana,sans-serif; font-size: 12px;
  font-weight: 700; color: var(--gray-700); cursor: pointer; transition: all .3s;
}}
.nav-btn:hover  {{ border-color: var(--primary-blue); color: var(--primary-blue); transform: scale(1.05); }}
.nav-btn:focus  {{ outline: 2px solid var(--primary-blue); outline-offset: 2px; }}
.nav-btn.active {{ background: var(--primary-blue); border-color: var(--primary-blue); color: white; box-shadow: var(--shadow-md); }}

/* ── MAIN CONTENT / SECTIONS ── */
.main-content {{ display: flex; flex-direction: column; gap: 32px; width: 100%; }}
.lesson-section {{
  width: 100%; margin: 2rem 0; border-radius: 12px;
  box-shadow: 0 4px 12px rgba(0,0,0,.1); background: white; overflow: hidden;
  padding: 2rem; scroll-margin-top: 80px;
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

/* ══════════════════════════════════════════════════════
   DEFINITION SIMULATION ENGINE  v18.1 — LIGHT THEME
   Canvas is now white/pale-blue. Controls use soft grays.
══════════════════════════════════════════════════════ */
.def-sim-wrapper {{
  margin-top: 20px;
  background: #ffffff;
  border: 2px solid #e2e8f0;
  border-radius: 12px;
  overflow: hidden;
  box-shadow: 0 4px 24px rgba(59,130,246,.12), 0 0 0 1px rgba(59,130,246,.1);
  transition: box-shadow .3s;
}}
.def-sim-wrapper:hover {{
  box-shadow: 0 8px 32px rgba(59,130,246,.2), 0 0 0 2px rgba(59,130,246,.2);
}}
.def-sim-header {{
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap;
  padding: 12px 18px;
  background: linear-gradient(135deg, #f8fafc, #eff6ff);
  border-bottom: 1px solid #e2e8f0;
  gap: 8px;
}}
.def-sim-badge {{
  display: inline-block; padding: 4px 12px;
  background: linear-gradient(135deg,#6366f1,#8b5cf6); color: white;
  border-radius: 20px; font-family: Verdana,sans-serif; font-size: 11px; font-weight: 800;
}}
.def-sim-topic {{
  font-family: Verdana,sans-serif; font-size: 12px; font-weight: 700; color: #64748b;
}}
.def-sim-canvas-wrap {{
  background: #f0f4ff;
  line-height: 0;
  border-bottom: 1px solid #e2e8f0;
}}
.def-sim-canvas-wrap canvas {{
  display: block; width: 100%; height: auto; cursor: crosshair;
}}
.def-sim-controls {{
  display: flex; align-items: center; flex-wrap: wrap; gap: 10px;
  padding: 12px 18px;
  background: #f8fafc;
  border-top: 1px solid #e2e8f0;
}}
.def-sim-btn {{
  padding: 8px 18px; border: none; border-radius: 8px;
  font-family: Verdana,sans-serif; font-size: 12px; font-weight: 800; cursor: pointer;
  transition: all .25s; min-width: 90px;
  background: linear-gradient(135deg,#6366f1,#4f46e5); color: white;
  box-shadow: 0 2px 8px rgba(99,102,241,.25);
}}
.def-sim-btn:hover {{ transform: scale(1.05); box-shadow: 0 4px 14px rgba(99,102,241,.4); }}
.def-sim-btn.secondary {{
  background: white; color: #64748b;
  border: 1.5px solid #cbd5e1; box-shadow: none;
}}
.def-sim-btn.secondary:hover {{ background: #f1f5f9; color: #334155; border-color: #94a3b8; }}
.def-sim-slider-group {{
  display: flex; align-items: center; gap: 8px;
}}
.def-sim-label {{
  font-family: Verdana,sans-serif; font-size: 11px; font-weight: 700; color: #64748b; white-space: nowrap;
}}
.def-sim-slider {{
  -webkit-appearance: none; appearance: none;
  width: 100px; height: 4px; border-radius: 2px;
  background: #cbd5e1; outline: none; cursor: pointer;
}}
.def-sim-slider::-webkit-slider-thumb {{
  -webkit-appearance: none; width: 16px; height: 16px; border-radius: 50%;
  background: #6366f1; cursor: pointer; box-shadow: 0 0 6px rgba(99,102,241,.4);
}}
.def-sim-slider::-moz-range-thumb {{
  width: 16px; height: 16px; border-radius: 50%; background: #6366f1;
  cursor: pointer; border: none;
}}
.def-sim-val {{
  font-family: var(--font-mono); font-size: 12px; color: #334155;
  font-weight: 700; min-width: 32px;
}}
.def-sim-hint {{
  padding: 8px 18px; font-family: Verdana,sans-serif; font-size: 11px;
  color: #94a3b8; font-weight: 600; background: #f8fafc;
  border-top: 1px solid #f1f5f9; text-align: center; font-style: italic;
}}

/* ── WHY MATTERS ── */
.why-matters-box {{
  margin: 16px 0; padding: 2rem;
  background: var(--green-bg); border-left: 5px solid var(--green-border);
  border-radius: 0 12px 12px 0; transition: all .3s; font-family: Verdana,sans-serif;
}}
.why-matters-box:hover {{ transform: translateY(-4px); box-shadow: 0 8px 20px rgba(0,0,0,.15); }}
.why-label {{
  font-family: Verdana,sans-serif; font-size: 11px; font-weight: 800;
  text-transform: uppercase; letter-spacing:.5px; margin-bottom: 10px; color: var(--gray-700);
}}
.why-text {{
  font-family: Verdana,sans-serif; font-size: clamp(.95rem,2.5vw,1.05rem);
  line-height: 1.7; color: var(--text-dark); margin-bottom: 16px;
}}

/* ── WHY IMPACTS — v18.1: single card, full-width ── */
.why-impacts {{
  display: flex; flex-direction: column; gap: 12px; margin-top: 4px;
}}
.why-impact-item {{
  display: flex; align-items: flex-start; gap: 14px; padding: 16px 18px;
  background: white; border-radius: var(--radius-md);
  border: 1.5px solid var(--green-border); transition: all .25s;
}}
.why-impact-item:hover {{ transform: translateY(-2px); box-shadow: var(--shadow-md); }}
.why-impact-icon {{ font-size: 28px; flex-shrink: 0; line-height: 1.2; }}
.why-impact-domain {{
  font-family: Verdana,sans-serif; font-size: 11px; font-weight: 800;
  text-transform: uppercase; color: #059669; letter-spacing:.4px; margin-bottom: 4px;
}}
.why-impact-desc {{
  font-family: Verdana,sans-serif; font-size: clamp(.9rem,2vw,1rem);
  line-height: 1.6; color: var(--text-dark);
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
.formula-cards-grid {{ display: grid; grid-template-columns: 1fr; gap: 20px; }}
@media (min-width: 768px) {{ .formula-cards-grid {{ grid-template-columns: repeat(2,1fr); }} }}
.formula-card {{
  background: white; border-left: 5px solid #7c3aed;
  border-radius: 0 12px 12px 0; padding: 2rem; line-height: 1.5;
  box-shadow: var(--shadow-md); transition: all .3s; font-family: Verdana,sans-serif;
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
.formula-when {{
  background: var(--blue-bg); border-left: 4px solid var(--blue-border);
  border-radius: var(--radius-sm); padding: 12px 16px; margin: 16px 0;
  font-family: Verdana,sans-serif; font-size: .9rem; line-height: 1.6;
}}
.formula-example {{
  background: var(--green-bg); border: 2px solid var(--green-border);
  border-radius: var(--radius-md); padding: 16px; margin: 16px 0;
}}
.example-title {{ font-family: Verdana,sans-serif; font-size: .9rem; font-weight: 800; color: #15803d; margin-bottom: 10px; }}
.example-text {{ font-family: var(--font-mono); font-size: .9rem; color: var(--text-dark); line-height: 1.7; }}
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

/* ── HOW IT WORKS ── */
.how-works-section {{ margin: 16px 0; font-family: Verdana,sans-serif; }}
.how-title {{ font-family: Verdana,sans-serif; font-size: clamp(1.2rem,3vw,1.5rem); font-weight: 700; margin-bottom: 24px; color: var(--gray-900); }}
.how-steps {{ display: flex; flex-direction: column; gap: 16px; margin-bottom: 24px; }}
.step {{ display: flex; align-items: flex-start; gap: 16px; padding: 16px; background: var(--bg-card); border-radius: var(--radius-md); }}
.step-number {{ flex-shrink: 0; width: 32px; height: 32px; display: flex; align-items: center; justify-content: center; background: var(--primary-blue); color: white; border-radius: 50%; font-family: Verdana,sans-serif; font-weight: 800; font-size: 13px; }}
.step-text {{ flex: 1; font-family: Verdana,sans-serif; font-size: clamp(.95rem,2.5vw,1rem); line-height: 1.6; color: var(--text-dark); }}
.eli10-visual-wrap {{ margin-top: 18px; background: white; border: 2px solid var(--gray-200); border-radius: var(--radius-md); padding: 16px; text-align: center; box-shadow: var(--shadow-sm); transition: all .25s; }}
.eli10-visual-wrap:hover {{ border-color: var(--primary-blue); box-shadow: var(--shadow-md); transform: translateY(-2px); }}
.eli10-svg {{ width: 100%; max-width: 720px; height: auto; display: block; margin: 0 auto; border-radius: var(--radius-sm); }}
.eli10-visual-caption {{ margin-top: 8px; font-family: Verdana,sans-serif; font-size: .85rem; font-weight: 700; color: var(--gray-700); text-transform: uppercase; letter-spacing:.4px; }}

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
   ANIMATION SECTION  v18.1 — Video Vault styles
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

/* ── Video Vault panel specific styles ── */
.vault-header {{ margin-bottom: 14px; }}
.vault-title-row {{
  display: flex; align-items: center; gap: 10px; margin-bottom: 10px;
}}
.vault-icon {{ font-size: 22px; }}
.vault-title {{
  font-family: Verdana,sans-serif; font-size: 16px; font-weight: 800; color: var(--gray-900); flex: 1;
}}
.vault-refresh-btn {{
  padding: 6px 14px; border: 1.5px solid #0d9488; background: #f0fdfa; color: #0d9488;
  border-radius: 8px; font-family: Verdana,sans-serif; font-size: 11px; font-weight: 700;
  cursor: pointer; transition: all .2s;
}}
.vault-refresh-btn:hover {{ background: #0d9488; color: white; }}
.vault-status {{ margin: 8px 0; }}
.vault-loading {{
  display: none; align-items: center; gap: 10px; padding: 16px;
  font-family: Verdana,sans-serif; font-size: 13px; color: #64748b; font-weight: 600;
}}
.vault-spinner {{
  width: 18px; height: 18px; border: 3px solid #e2e8f0; border-top-color: #0d9488;
  border-radius: 50%; animation: vaultSpin .7s linear infinite;
}}
@keyframes vaultSpin {{ to {{ transform: rotate(360deg); }} }}
.vault-empty {{
  display: none; text-align: center; padding: 40px 20px;
  color: var(--gray-700); font-family: Verdana,sans-serif;
}}
.vault-grid {{ margin-top: 12px; }}
.vault-card {{ padding: 0 0 10px; overflow: hidden; }}
.vault-card-thumb {{
  width: 100%; height: 90px; background: linear-gradient(135deg,#0f172a,#1e3a5f);
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  display: flex; align-items: center; justify-content: center;
  position: relative; overflow: hidden; margin-bottom: 8px;
}}
.vault-card-play {{
  font-size: 28px; color: rgba(255,255,255,.85);
  text-shadow: 0 2px 8px rgba(0,0,0,.4);
  transition: transform .2s;
}}
.vault-card:hover .vault-card-play {{ transform: scale(1.2); }}
.vault-card-dur {{
  position: absolute; bottom: 5px; right: 6px;
  background: rgba(0,0,0,.75); color: white;
  font-family: var(--font-mono); font-size: 10px; font-weight: 700;
  padding: 2px 6px; border-radius: 4px;
}}
.vault-card-meta {{ padding: 0 10px; }}
.vault-footer {{
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap;
  gap: 8px; margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--gray-200);
}}
.vault-count {{
  font-family: Verdana,sans-serif; font-size: 11px; font-weight: 700; color: #64748b;
}}
.vault-info {{
  font-family: Verdana,sans-serif; font-size: 10px; color: #94a3b8; font-style: italic;
}}

/* ── Player ── */
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
  body {{ background: #0f172a; color: #f1f5f9; }}
  .page-container {{
    background:
      url("{svg_pattern_b64}") repeat,
      linear-gradient(135deg,#0f172a 0%,#1e293b 100%);
  }}
  .page-header, .lesson-section, .page-footer {{ background: #1e293b; color: #f1f5f9; }}
  .section-header h2 {{ color: #f1f5f9; }}
  .section-nav {{ background: rgba(30,41,59,.97); }}
  .nav-btn {{ background: #1e293b; border-color: #334155; color: #cbd5e1; }}
  .nav-btn.active {{ background: var(--primary-blue); color: white; }}
  .classification-bar {{ background: #1e293b; }}
  /* Keep sim wrapper light even in dark mode for readability */
  .def-sim-wrapper {{ background: #f8fafc; border-color: #cbd5e1; }}
  .def-sim-header {{ background: linear-gradient(135deg,#f1f5f9,#e8f0fe); }}
  .def-sim-controls {{ background: #f1f5f9; }}
  .def-sim-hint {{ background: #f8fafc; color: #64748b; }}
  .concept-card, .formula-card, .app-card {{ background: #1e293b; color: #f1f5f9; }}
  .concept-definition, .concept-body p, .concept-bullets li,
  .app-text, .step-text, .q-text, .q-opt {{ color: #e2e8f0; }}
  .hook-lead, .hook-bullets li {{ color: #f1f5f9; }}
  .def-analogy {{ color: #93c5fd; }}
  .def-properties li {{ color: #e2e8f0; }}
  .why-text {{ color: #e2e8f0; }}
  .why-impact-item {{ background: #1e293b; }}
  .why-impact-desc {{ color: #e2e8f0; }}
  .quiz-question {{ background: #1e293b; border-color: #334155; }}
  .q-opt {{ background: #0f172a; border-color: #334155; color: #e2e8f0; }}
  .tc-table td {{ color: #e2e8f0; }}
  .eli10-visual-caption {{ color: #cbd5e1; }}
  .uploaded-image-wrap {{ background: #1e293b; border-color: #334155; }}
  .formula-when {{ color: #e2e8f0; }}
  .symbol-desc {{ color: #e2e8f0; }}
  .example-text {{ color: #e2e8f0; }}
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
}}

/* ── RESPONSIVE ── */
@media (max-width: 768px) {{
  .page-container {{ padding: 24px 16px; }}
  .page-header {{ padding: 24px; }}
  .page-title {{ font-size: 1.8rem; }}
  .section-nav {{ gap: 6px; padding: 8px; top: 0; }}
  .nav-btn {{ font-size: 10px; padding: 6px 10px; }}
  .lesson-section {{ padding: 24px 16px; margin: 1rem 0; }}
  .formula-cards-grid {{ grid-template-columns: 1fr; }}
  .app-grid {{ grid-template-columns: 1fr; }}
  .quiz-tabs {{ gap: 4px; }}
  .quiz-tab {{ font-size: 11px; padding: 8px 6px; }}
  .anim-lib-grid {{ grid-template-columns: 1fr; }}
  .fc-branches-row {{ flex-direction: column; align-items: center; }}
  .fc-branch-col {{ max-width: 280px; width: 100%; }}
  .eli10-svg {{ max-width: 100%; }}
  .def-sim-controls {{ flex-direction: column; align-items: flex-start; gap: 8px; }}
  .footer-cta {{ flex-direction: column; align-items: center; }}
  .uploaded-image {{ max-height: 420px; }}
  .uploaded-image-wrap {{ max-width: 100%; }}
  .vault-title-row {{ flex-wrap: wrap; }}
}}

@media print {{
  .section-nav, .page-footer, .anim-player-wrap, .quiz-section {{ display: none; }}
  .lesson-section {{ break-inside: avoid; page-break-inside: avoid; }}
}}
"""


# ════════════════════════════════════════════════════════════════════════
#  generate_animation  — PRIMARY BACKEND ENTRY POINT  (v18.1)
#  Now fully specific-sub-topic aware.
# ════════════════════════════════════════════════════════════════════════

async def generate_animation(prompt: str) -> dict:
    if not prompt or not prompt.strip():
        raise ValueError("Prompt cannot be empty")

    prompt = prompt.strip()
    log.info(f"\n{'═'*64}")
    log.info(f"[generate_animation v18.1] prompt='{prompt}'")
    log.info(f"{'═'*64}")

    subtopics_list = _extract_subtopics_from_input(prompt)

    # ── Topic extraction ──
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
        # v18.1: treat the full prompt as the topic (handles "conduction in heat transfer")
        topic = prompt

    is_specific = _is_specific_subtopic(topic)
    log.info(f"[generate_animation v18.1] topic='{topic}' | specific_subtopic={is_specific}")

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

    log.info(f"[generate_animation v18.1] ✅ HTML={len(html):,} chars | topic='{topic}'")

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
#  generate_genzet_book_content  (v18.1 — specific-sub-topic aware)
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

    # v18.1: if the combined full_topic is itself a specific sub-topic phrase,
    # or subtopic is a specific phrase, use the most specific form.
    if subtopic and _is_specific_subtopic(subtopic):
        # e.g. topic="Heat Transfer", subtopic="conduction in solids"
        # → full_topic stays as "Heat Transfer — conduction in solids"
        # The specific_note inside each prompt will lock all content to the subtopic.
        log.info(f"[generate_genzet_book_content v18.1] specific sub-topic detected: '{subtopic}'")

    log.info(f"\n{'═'*64}")
    log.info(f"[generate_genzet_book_content v18.1] topic='{full_topic}'")
    log.info(f"[generate_genzet_book_content v18.1] pdf_context={len(pdf_context):,} chars  "
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

    log.info(f"[generate_genzet_book_content v18.1] ✅ HTML={len(html):,} chars")

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
    print(f"ULTIMATE LEARNING CONTENT GENERATOR  v18.1")
    print(f"{'='*64}")
    print(f"Topic            : {topic}")
    print(f"Specific sub-topic: {_is_specific_subtopic(topic)}")
    print(f"Subtopics        : {subtopics if subtopics else '(auto-detect)'}")
    print(f"Output           : {output_file}")
    print(f"\nv18.1 CHANGES:")
    print(f"  ✅ CHANGED: Canvas/SVG → LIGHT THEME (pale blue-white bg, vivid entities)")
    print(f"  ✅ CHANGED: 'Why It Matters' → 1 concise, high-value impact only")
    print(f"  ✅ CHANGED: Animation section: 'From Library' → 'Video Vault' container")
    print(f"  ✅ ADDED:   Specific sub-topic detection + all-section focus enforcement")
    print(f"              (e.g. 'conduction in heat transfer' stays focused on conduction)")
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