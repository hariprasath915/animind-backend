"""
simulation.py -- Simulation Creator Engine  v2.1
====================================================================
PURPOSE
  Turn a user-supplied topic / concept / lab experiment (e.g.
  "Snell's Law", "RC circuit charging", "projectile motion",
  "population growth model", "binary search visualization") into a
  single, self-contained, interactive HTML5 simulation:

    - A live <canvas> (or inline SVG for purely diagrammatic topics)
      driven by sliders / toggles / dropdowns in a control panel
    - Real-time redraw on every input change (no fixed "scenes",
      no prev/next narration -- this is a LAB, not a slideshow)
    - A metrics strip that reports live computed values
    - Professional, distraction-free chrome (sidebar/header + canvas
      + metrics), matching the visual quality bar of a hand-built
      virtual-lab page
    - Google Image Search used as a visual-reference step so the
      model understands real instrument / diagram aesthetics before
      generating the simulation

ROOT CAUSE FIX (v2.1.6):
  Google's own API error message confirmed:
  'This model models/gemini-2.5-pro is no longer available to new users.
   Please update your code to use models/gemini-3.1-pro-preview'

  Fix applied:
    1. Default SIM_MODEL = 'gemini-3.1-pro-preview' (Google-confirmed replacement)
    2. Removed gemini-3.1 from _BAD_MODEL_PATTERNS (it IS the correct model)
    3. AutomaticFunctionCallingConfig removed (caused silent failures)
    4. ThinkingConfig with try/except fallback added (mirrors q_animation pattern)
"""

import os
import re
import json
import time
import asyncio
import urllib.request
import urllib.parse
import html as html_module
from typing import Optional, List

from google import genai as _google_genai
# pyrefly: ignore [missing-import]
from google.genai import types as _genai_types

# ---------------------------------------------------------------------------
# Client + model routing  (v2.1.5 — self-healing model ID)
# ---------------------------------------------------------------------------

CLIENT_TIMEOUT_SECONDS   = float(os.environ.get("SIM_CLIENT_TIMEOUT_SECONDS", "300"))
CLIENT_MAX_RETRIES       = int(os.environ.get("SIM_CLIENT_MAX_RETRIES", "0"))
PIPELINE_TIMEOUT_SECONDS = float(os.environ.get("SIM_PIPELINE_TIMEOUT_SECONDS", "310"))

# BUG FIX: Cap MAX_TOK to prevent excessive timeouts, but allow enough tokens
# for very complex simulations (which can exceed 15k tokens).
_MAX_TOK_HARD_CAP = 32768
_max_tok_raw = int(os.environ.get("SIM_MAX_TOKENS", "32768"))
if _max_tok_raw > _MAX_TOK_HARD_CAP:
    print(
        f"[SimEngine] ⚠  SIM_MAX_TOKENS={_max_tok_raw} exceeds hard cap of {_MAX_TOK_HARD_CAP}. "
        f"Clamped to {_MAX_TOK_HARD_CAP}. Update Railway Variable SIM_MAX_TOKENS to fix this."
    )
MAX_TOK             = min(_max_tok_raw, _MAX_TOK_HARD_CAP)
MAX_TOK_CLASSIFIER  = 20


GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID  = os.environ.get("GOOGLE_CSE_ID", "")

# Safe defaults — use model IDs confirmed working on Google AI Studio API keys.
# Google API confirmed: gemini-3.1-pro-preview is the replacement for gemini-2.5-pro.
_SAFE_DEFAULT_SIM_MODEL        = "gemini-3.1-pro-preview"
_SAFE_DEFAULT_CLASSIFIER_MODEL = "gemini-3.1-pro-preview"


# Confirmed working: gemini-3.1-pro-preview (Google's own API error message said to use it)
# Patterns known to cause 404 — only flag genuinely bad IDs, NOT gemini-3.1.
_BAD_MODEL_PATTERNS = [
    # Date-suffix preview variants — retired periodically by Google
    (r"gemini-[\d.]+-pro-preview-\d{2}-\d{2}",  "gemini-3.1-pro-preview"),
    (r"gemini-[\d.]+-pro-preview-\d{2}",         "gemini-3.1-pro-preview"),
    (r"gemini-[\d.]+-flash-preview-\d{2}-\d{2}", "gemini-3.1-pro-preview"),
    (r"gemini-[\d.]+-flash-preview-\d{2}",       "gemini-3.1-pro-preview"),
    # Anthropic model IDs (switched away from Claude)
    (r"claude-",                                  _SAFE_DEFAULT_SIM_MODEL),
]


def _sanitize_model_id(model_id: str, role: str = "SIM_MODEL") -> str:
    """
    Detect and auto-correct known-bad model IDs at import time.
    Returns the original string unchanged if it looks fine.
    """
    for pattern, replacement in _BAD_MODEL_PATTERNS:
        if re.search(pattern, model_id, re.IGNORECASE):
            print(
                f"[SimEngine] ⚠  {role}='{model_id}' matches a known-bad pattern "
                f"→ auto-corrected to '{replacement}'. "
                f"Update your SIM_MODEL env var to silence this warning."
            )
            return replacement
    return model_id


# Resolve model IDs at import time so a stale env var self-heals.
SIM_MODEL = _sanitize_model_id(
    os.environ.get("SIM_MODEL", _SAFE_DEFAULT_SIM_MODEL),
    role="SIM_MODEL",
)
CLASSIFIER_MODEL = _sanitize_model_id(
    os.environ.get("SIM_CLASSIFIER_MODEL", _SAFE_DEFAULT_CLASSIFIER_MODEL),
    role="SIM_CLASSIFIER_MODEL",
)
print(f"[SimEngine] Model configured: SIM_MODEL='{SIM_MODEL}', CLASSIFIER='{CLASSIFIER_MODEL}'")

# Gemini async client.
# NOTE: Do NOT set api_version here — the SDK default (v1beta) supports
# gemini-3.1-pro-preview and all current Gemini models on Google AI Studio API keys.
_gemini_client = _google_genai.Client(
    api_key=os.environ.get("GEMINI_API_KEY") or GOOGLE_API_KEY,
    http_options=_genai_types.HttpOptions(
        timeout=int(CLIENT_TIMEOUT_SECONDS * 1000),
    ),
)


# ===========================================================================
#  MODULE 1 -- SimLogger
# ===========================================================================
class SimLogger:
    PREFIX = "[SimEngine v2.1]"

    @classmethod
    def info(cls, stage, msg):
        print(f"{cls.PREFIX} i  [{stage}] {msg}")

    @classmethod
    def warn(cls, stage, msg):
        print(f"{cls.PREFIX} !  [{stage}] {msg}")

    @classmethod
    def error(cls, stage, msg):
        print(f"{cls.PREFIX} X  [{stage}] {msg}")

    @classmethod
    def ok(cls, stage, msg):
        print(f"{cls.PREFIX} OK [{stage}] {msg}")


# ===========================================================================
#  MODULE 2 -- GenerationValidator
# ===========================================================================
class ValidationError(Exception):
    pass


class GenerationValidator:
    DANGEROUS_PATTERNS = [
        (r'document\.write\s*\(',  "document.write() is forbidden"),
        (r'<script[^>]+src\s*=',   "External script src not allowed"),
        (r'javascript:\s*void',    "javascript:void() link detected"),
        (r'on\w+\s*=\s*["\']?\s*eval\s*\(', "eval() in event handler"),
        (r'\bfetch\s*\(',          "Network fetch() call -- must be offline-only"),
        (r'\bXMLHttpRequest\b',    "XHR call -- must be offline-only"),
        (r'localStorage\s*\.',     "localStorage not allowed"),
        (r'sessionStorage\s*\.',   "sessionStorage not allowed"),
    ]
    REQUIRED_ELEMENTS = [
        ("<!DOCTYPE", "Missing DOCTYPE declaration"),
        ("<html",     "Missing <html> tag"),
        ("</html>",   "Missing closing </html> tag"),
        ("<body",     "Missing <body> tag"),
        ("</body>",   "Missing closing </body> tag"),
        ("<script",   "No script block"),
    ]
    SVG_REQUIRED = [
        ("<svg",   "No SVG element found"),
        ("</svg>", "SVG element not closed"),
    ]

    @classmethod
    def repair(cls, html: str) -> str:
        """
        Auto-repair common truncation artifacts from LLM output.
        Appends missing closing tags so a nearly-complete simulation
        is not thrown away entirely.
        """
        h = html.rstrip()
        # If </html> is missing, try to add it
        if '</html>' not in h.lower():
            # Add </body> if also missing
            if '</body>' not in h.lower():
                # Close any open script tag first
                open_scripts  = len(re.findall(r'<script(?:\s[^>]*)?>',  h, re.IGNORECASE))
                close_scripts = len(re.findall(r'</script>',              h, re.IGNORECASE))
                if open_scripts > close_scripts:
                    h += '\n</script>'
                h += '\n</body>'
            h += '\n</html>'
        elif '</body>' not in h.lower():
            # Has </html> but missing </body> — insert before </html>
            h = re.sub(r'</html>', '\n</body>\n</html>', h, flags=re.IGNORECASE)
        return h

    @classmethod
    def validate(cls, html, require_svg=False, require_canvas=False):
        if not html or not html.strip():
            raise ValidationError("simulation_code is empty")
        if len(html) < 500:
            raise ValidationError(f"simulation_code suspiciously short ({len(html)} chars)")
        # Check required structural elements — repaired before reaching here
        must_have = [
            ("<!DOCTYPE", "Missing DOCTYPE declaration"),
            ("<html",     "Missing <html> tag"),
            ("<body",     "Missing <body> tag"),
            ("<script",   "No script block"),
        ]
        for pattern, reason in must_have:
            if pattern not in html:
                raise ValidationError(reason)
        if require_svg:
            for pattern, reason in cls.SVG_REQUIRED:
                if pattern not in html:
                    raise ValidationError(reason)
        if require_canvas and "<canvas" not in html:
            raise ValidationError("No <canvas> element found")
        for pattern, reason in cls.DANGEROUS_PATTERNS:
            if re.search(pattern, html, re.IGNORECASE):
                SimLogger.warn("Validator", f"Dangerous pattern: {reason}")
        if "tut-root" not in html or "TUT_STEPS" not in html:
            SimLogger.warn("Validator", "Onboarding tutorial system missing or incomplete "
                                         "(no #tut-root / TUT_STEPS found) -- generation did "
                                         "not follow the required onboarding design pattern")
        open_scripts  = len(re.findall(r'<script(?:\s[^>]*)?>',  html, re.IGNORECASE))
        close_scripts = len(re.findall(r'</script>',              html, re.IGNORECASE))
        if open_scripts != close_scripts:
            raise ValidationError(f"Unbalanced <script> tags: {open_scripts} open, {close_scripts} close")
        SimLogger.ok("Validator", f"HTML passed validation ({len(html):,} chars)")



# ===========================================================================
#  MODULE 3 -- HtmlSanitizer
# ===========================================================================
class HtmlSanitizer:
    @classmethod
    def sanitize(cls, html):
        html = html.replace('\ufeff', '').replace('\r\n', '\n').replace('\r', '\n')
        end = html.rfind('</html>')
        if end != -1:
            html = html[:end + 7]
        html = re.sub(
            r'<script[^>]+src\s*=\s*["\'][^"\']*["\'][^>]*>\s*</script>',
            '', html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(
            r'<link[^>]+href\s*=\s*["\']https?://[^"\']*["\'][^>]*>',
            '', html, flags=re.IGNORECASE)
        html = re.sub(r'@import\s+url\([^)]*\)\s*;?', '', html, flags=re.IGNORECASE)
        html = re.sub(r'@import\s+["\'][^"\']*["\']\s*;?', '', html, flags=re.IGNORECASE)
        html = re.sub(r'document\.write\s*\([^)]*\)\s*;?', '', html, flags=re.IGNORECASE)
        html = cls._strip_network_calls(html)
        html = cls._fix_unescaped_string_newlines(html)
        html = cls._wrap_scripts_in_error_boundary(html)
        html = re.sub(r'<svg(?![^>]*xmlns)', '<svg xmlns="http://www.w3.org/2000/svg"', html, flags=re.IGNORECASE)
        html = html.replace('\x00', '')
        SimLogger.ok("Sanitizer", "HTML sanitized")
        return html

    @classmethod
    def _strip_network_calls(cls, html):
        def process_script(m):
            tag, body, close = m.group(1), m.group(2), m.group(3)
            if re.search(r'type\s*=\s*["\']application/', tag, re.IGNORECASE):
                return m.group(0)
            new_body = re.sub(r'\bfetch\s*\([^)]*\)[^;]*;?', '/* network call removed */;', body)
            new_body = re.sub(r'new\s+XMLHttpRequest\s*\([^)]*\)', '({})', new_body)
            if new_body != body:
                SimLogger.warn("Sanitizer", "Removed a network call (fetch/XHR) from generated JS")
            return f"{tag}{new_body}{close}"
        return re.sub(r'(<script(?:\s[^>]*)?>)(.*?)(</script>)', process_script, html, flags=re.DOTALL | re.IGNORECASE)

    @classmethod
    def _fix_unescaped_string_newlines(cls, html):
        STRING_RE = re.compile(r'(["\'])((?:\\.|(?!\1).)*)\1', re.DOTALL)

        def fix_str(sm):
            quote, inner = sm.group(1), sm.group(2)
            fixed = (inner.replace('\r\n', '\\n')
                           .replace('\n', '\\n')
                           .replace('\r', '\\n')
                           .replace('\t', '\\t'))
            return quote + fixed + quote

        def process_script(m):
            tag, body, close = m.group(1), m.group(2), m.group(3)
            fixed_body = STRING_RE.sub(fix_str, body)
            if fixed_body != body:
                SimLogger.warn("Sanitizer", "Raw newline/tab inside a JS string literal -- escaped")
            return f"{tag}{fixed_body}{close}"

        return re.sub(r'(<script(?:\s[^>]*)?>)(.*?)(</script>)', process_script, html, flags=re.DOTALL | re.IGNORECASE)

    @classmethod
    def _wrap_scripts_in_error_boundary(cls, html):
        def wrap_script(match):
            tag, body, close = match.group(1), match.group(2), match.group(3)
            if re.search(r'type\s*=\s*["\']application/', tag, re.IGNORECASE):
                return match.group(0)
            stripped = body.strip()
            if stripped.startswith('try {') or stripped.startswith('try{'):
                return match.group(0)
            if len(stripped) < 20:
                return match.group(0)
            wrapped = (
                "\n/* -- SimEngine Error Boundary -- */\ntry {\n" + body +
                "\n} catch (_sim_err) {\n"
                "  console.error('[SimEngine ErrorBoundary]', _sim_err);\n"
                "  (function() {\n"
                "    var fb = document.getElementById('sim-error-fallback');\n"
                "    if (!fb) return;\n"
                "    fb.style.display = 'flex';\n"
                "    var msg = fb.querySelector('.sim-err-msg');\n"
                "    if (msg) msg.textContent = String(_sim_err);\n"
                "  })();\n}\n")
            return f"{tag}{wrapped}{close}"
        return re.sub(r'(<script(?:\s[^>]*)?>)(.*?)(</script>)', wrap_script, html, flags=re.DOTALL | re.IGNORECASE)


# ===========================================================================
#  MODULE 4 -- RecoveryEngine
# ===========================================================================
class RecoveryEngine:
    @staticmethod
    def fallback_html(topic, reason):
        t_safe      = html_module.escape(topic[:120])
        reason_safe = html_module.escape(reason[:300])
        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100%;background:#0a0c10;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  display:flex;align-items:center;justify-content:center;color:#e8eaf0}}
.card{{background:#12151c;border:1px solid #2a3040;border-radius:16px;
  box-shadow:0 4px 24px rgba(0,0,0,.4);padding:36px 40px;max-width:520px;text-align:center}}
.icon{{font-size:40px;margin-bottom:16px}}
.title{{font-size:17px;font-weight:700;color:#e8eaf0;margin-bottom:10px}}
.reason{{font-size:11px;color:#8892a4;background:#1a1f2b;border-radius:10px;
  padding:10px 14px;margin:12px 0;border:1px solid #2a3040;text-align:left;
  line-height:1.6;font-family:monospace}}
.topic{{font-size:12px;color:#556070;line-height:1.6;margin-top:10px;font-style:italic}}
.retry-hint{{margin-top:18px;font-size:11px;font-weight:700;letter-spacing:1.5px;
  text-transform:uppercase;color:#f5a623}}
</style></head><body>
<div class="card">
<div class="icon">&#x26A0;&#xFE0F;</div>
<div class="title">Simulation Could Not Render</div>
<div class="reason">{reason_safe}</div>
<div class="topic">"{t_safe}"</div>
<div class="retry-hint">Please try generating again</div>
</div></body></html>"""

    @staticmethod
    def partial_html(topic, sim_code):
        if '<!DOCTYPE' in sim_code or '<html' in sim_code:
            return sim_code
        t_safe = html_module.escape(topic[:120])
        return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>html,body{{margin:0;padding:0;width:100%;height:100%;background:#0a0c10;
  font-family:-apple-system,sans-serif;color:#e8eaf0}}</style></head><body>
<div style="font-size:11px;color:#8892a4;position:fixed;top:8px;left:0;right:0;text-align:center;z-index:99">
  {t_safe}</div>
{sim_code}</body></html>"""


# ===========================================================================
#  MODULE 5 -- Image Reference Fetcher
# ===========================================================================

def _fetch_image_refs(topic: str, max_results: int = 5) -> List[dict]:
    """
    Query Google Custom Search Image API for visual references.
    Returns [] gracefully when keys are absent or the request fails.
    Must be called via asyncio.to_thread() from async code.
    """
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        SimLogger.info("ImageRef", "Google keys not set -- skipping image search step")
        return []

    query = f"{topic} diagram simulation laboratory experiment"
    params = urllib.parse.urlencode({
        "key":        GOOGLE_API_KEY,
        "cx":         GOOGLE_CSE_ID,
        "q":          query,
        "searchType": "image",
        "num":        max_results,
        "imgType":    "photo,clipart",
        "safe":       "active",
    })
    url = f"https://www.googleapis.com/customsearch/v1?{params}"

    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("items", [])
        refs = []
        for item in items[:max_results]:
            refs.append({
                "title":   item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "link":    item.get("link", ""),
            })
        SimLogger.ok("ImageRef", f"Fetched {len(refs)} image references for '{topic[:50]}'")
        return refs
    except Exception as e:
        SimLogger.warn("ImageRef", f"Image search failed (non-fatal): {e}")
        return []


def _format_image_refs_for_prompt(refs: List[dict]) -> str:
    if not refs:
        return ""
    lines = ["VISUAL REFERENCE (image titles/descriptions found on Google for this topic):"]
    for i, r in enumerate(refs, 1):
        title   = r.get("title", "").strip()[:120]
        snippet = r.get("snippet", "").strip()[:200]
        if title:
            lines.append(f"  [{i}] {title}")
        if snippet:
            lines.append(f"       {snippet}")
    lines.append(
        "Use these as visual anchors when designing the canvas layout, "
        "choosing diagram conventions, and picking instrument/component styles. "
        "Do NOT attempt to load or embed any URLs."
    )
    return "\n".join(lines)


# ===========================================================================
#  MODULE 6 -- Topic Classification
# ===========================================================================
CATEGORIES = [
    "PHYSICS_MECHANICS",
    "PHYSICS_WAVES_OPTICS",
    "ELECTRICITY_CIRCUITS",
    "CHEMISTRY",
    "BIOLOGY",
    "MATH_GEOMETRY",
    "CS_ALGORITHMS",
    "EARTH_ENV_SCIENCE",
    "ECONOMICS_SOCIAL",
    "GENERAL_PROCESS",
]

_CATEGORY_KEYWORDS = {
    "PHYSICS_MECHANICS": ["projectile", "pendulum", "spring", "friction", "collision",
        "momentum", "force", "velocity", "acceleration", "gravity", "torque",
        "newton", "oscillation", "harmonic motion", "free fall", "incline",
        "kinematics", "dynamics", "angular", "rotational", "centripetal"],
    "PHYSICS_WAVES_OPTICS": ["wave", "light", "lens", "mirror", "refraction", "reflection",
        "diffraction", "interference", "prism", "snell", "optic", "sound",
        "frequency", "wavelength", "doppler", "polarization", "photoelectric",
        "electromagnetic", "spectrum", "coherent", "interference pattern"],
    "ELECTRICITY_CIRCUITS": ["circuit", "resistor", "capacitor", "inductor", "voltage",
        "current", "ohm", "charge", "electric field", "magnetic field", "rc circuit",
        "rlc", "kirchhoff", "battery", "diode", "transistor", "semiconductor",
        "band gap", "p-n junction", "logic gate", "digital circuit"],
    "CHEMISTRY": ["reaction", "equilibrium", "ph", "titration", "molarity", "gas law",
        "boyle", "charles", "stoichiometry", "acid", "base", "catalyst", "bond",
        "electron configuration", "periodic", "polymerization", "polymer",
        "enthalpy", "entropy", "activation energy", "colligative"],
    "BIOLOGY": ["cell", "dna", "rna", "protein", "photosynthesis", "mitosis", "enzyme",
        "hormone", "gene", "population growth", "predator", "prey", "ecosystem",
        "natural selection", "neuron", "heart rate", "osmosis", "diffusion",
        "action potential", "genetics", "heredity"],
    "MATH_GEOMETRY": ["function", "derivative", "integral", "matrix", "vector", "polygon",
        "triangle", "circle", "probability", "distribution", "fourier", "fractal",
        "trigonometry", "graph of", "parametric", "transformation", "linear algebra",
        "differential equation", "taylor series", "complex number"],
    "CS_ALGORITHMS": ["sorting", "sort algorithm", "binary search", "linked list", "stack",
        "queue", "tree traversal", "graph algorithm", "dijkstra", "recursion",
        "dynamic programming", "hash table", "automaton", "neural network",
        "pathfinding", "convex hull", "compression", "encryption"],
    "EARTH_ENV_SCIENCE": ["climate", "plate tectonic", "earthquake", "weather", "erosion",
        "carbon cycle", "greenhouse", "orbit", "solar system", "tide", "volcano",
        "water cycle", "ecosystem", "seismic", "atmospheric", "ocean current"],
    "ECONOMICS_SOCIAL": ["supply and demand", "market", "interest rate", "inflation",
        "compound interest", "population dynamics", "game theory", "auction",
        "elasticity", "gdp", "investment", "portfolio", "regression"],
}

_MULTI_EXPERIMENT_TOPICS = {
    "PHYSICS_WAVES_OPTICS": [
        "Snell's Law / Refraction", "Convex Lens", "Concave Lens",
        "Concave Mirror", "Double-Slit Interference", "Single-Slit Diffraction",
    ],
    "ELECTRICITY_CIRCUITS": [
        "RC Charging/Discharging", "RLC Oscillator", "Ohm's Law",
        "Series & Parallel Circuits", "EM Induction",
    ],
    "PHYSICS_MECHANICS": [
        "Projectile Motion", "Simple Pendulum", "Spring-Mass System",
        "Elastic Collision", "Inclined Plane",
    ],
    "CHEMISTRY": [
        "Acid-Base Titration", "Gas Laws (Boyle/Charles)", "Chemical Equilibrium",
        "Reaction Kinetics", "Electrochemistry",
    ],
}


async def _classify_topic(topic: str) -> str:
    """
    Keyword-match first (instant, no network). Falls back to an LLM call
    only when keywords are ambiguous — awaited on the async client.
    """
    t = topic.lower()
    scores = {cat: sum(1 for k in kws if k in t) for cat, kws in _CATEGORY_KEYWORDS.items()}
    max_score = max(scores.values()) if scores else 0
    if max_score >= 1:
        top = [c for c, s in scores.items() if s == max_score]
        if len(top) == 1:
            return top[0]
    try:
        resp = await _gemini_client.aio.models.generate_content(
            model=CLASSIFIER_MODEL,
            contents=f"Classify this simulation topic: {topic[:200]}",
            config=_genai_types.GenerateContentConfig(
                system_instruction="Reply with ONLY one category word from this exact list: "
                                   + ", ".join(CATEGORIES),
                max_output_tokens=MAX_TOK_CLASSIFIER,
                temperature=0.0,
            ),
        )
        cat = (resp.text or "").strip().upper()
        if cat in CATEGORIES:
            return cat
    except Exception as e:
        SimLogger.warn("Classifier", f"Fallback classification failed: {e}")
    return "GENERAL_PROCESS"


# ===========================================================================
#  MODULE 7 -- Prompt System
# ===========================================================================

DESIGN_SYSTEM = """
════════════════════════════════════════════════════════
  REQUIRED PAGE ARCHITECTURE
════════════════════════════════════════════════════════
Build ONE self-contained HTML5 page (no external resources, no CDN,
no network calls) structured as:

  #app  (display:flex; height:100vh)
  ├── #sidebar  (fixed width 260–300px, flex-shrink:0)
  │   ├── #lab-title       — eyebrow label "Interactive Simulation" +
  │   │                      bold page title with ONE accent-colored word
  │   ├── #exp-list        — (OPTIONAL: only for broad topics covering
  │   │                      multiple experiments) a vertical list of
  │   │                      .exp-btn buttons; clicking one swaps the
  │   │                      experiment shown without reloading. If the
  │   │                      topic is a single focused experiment, omit
  │   │                      #exp-list entirely.
  │   └── #controls-panel  — sliders / selects / toggles wired to the sim
  └── #main  (flex:1; flex-direction:column)
      ├── #canvas-area  (flex:1; position:relative; overflow:hidden)
      │   ├── <canvas id="cvs"> OR inline <svg id="simsvg">
      │   ├── #overlay-bar  (position:absolute; top:10px; right:12px)
      │   └── #tip  (position:absolute; bottom:12px; left:50%)
      └── #info-panel  (height:~100px; border-top)
            — horizontal row of 3–5 .metric cards

MOBILE BREAKPOINT (max-width: 760px):
  - #app becomes flex-direction:column
  - #sidebar becomes width:100%; max-height:220px; overflow-y:auto
  - #controls-panel becomes a wrapping flex-row of compact controls
  - #info-panel stacks metrics in a 2×N grid

════════════════════════════════════════════════════════
  COLOUR TOKENS  (define ALL in :root on <html>)
════════════════════════════════════════════════════════
DARK THEME — default for science/math/engineering:
  :root {
    --bg:#0a0c10;  --surface:#12151c;  --surface2:#1a1f2b;  --surface3:#222837;
    --border:#2a3040;  --border2:#3a4560;
    --text:#e8eaf0;  --text2:#8892a4;  --text3:#556070;
    --green:#3ddc84;  --green-dim:#003320;
    --red:#ff5f57;    --red-dim:#3d0000;
    --violet:#b57aff; --violet-dim:#1e0040;
    --cyan:#00d4d8;   --cyan-dim:#003d3e;
    --accent: <pick one: #f5a623 amber | #4a9eff blue | #00d4d8 cyan
                         | #3ddc84 green | #e056b4 magenta>;
    --accent-dim: <matching dim>;
    --accent-glow: <rgba version with 0.5 alpha>;
    --panel-w: 260px;
  }

LIGHT THEME — use ONLY for economics/statistics/printed-diagram topics:
  :root {
    --bg:#f0f5ff;  --surface:#ffffff;  --surface2:#f1f5f9;
    --border:#e2e8f0;  --border2:#cbd5e1;
    --text:#1e293b;  --text2:#475569;  --text3:#94a3b8;
    --accent:#3b5bdb;  --accent-dim:#dbe4ff;  --accent-glow:rgba(59,91,219,.3);
    --panel-w: 260px;
  }

════════════════════════════════════════════════════════
  SIDEBAR + CONTROLS CSS PATTERNS
════════════════════════════════════════════════════════
#sidebar {
  width:var(--panel-w); background:var(--surface);
  border-right:1px solid var(--border);
  display:flex; flex-direction:column; flex-shrink:0; overflow:hidden;
}
#lab-title { padding:16px; border-bottom:1px solid var(--border); }
.eyebrow { font-size:10px; letter-spacing:.12em; text-transform:uppercase;
           color:var(--text3); margin-bottom:4px; }
#lab-title h1 { font-size:17px; font-weight:600; color:var(--text); }
#lab-title h1 span { color:var(--accent); }

.exp-btn {
  display:flex; align-items:center; gap:10px; width:100%;
  padding:9px 12px; border-radius:8px; border:none; background:transparent;
  color:var(--text2); cursor:pointer; font-size:13px; font-weight:500;
  text-align:left; transition:all .15s;
}
.exp-btn.active {
  background:var(--accent-dim); color:var(--accent);
  border:1px solid color-mix(in srgb, var(--accent) 25%, transparent);
}

#controls-panel { flex:1; overflow-y:auto; padding:12px; }
.ctrl-row { margin-bottom:10px; }
.ctrl-name { font-size:12px; color:var(--text2); margin-bottom:4px;
             display:flex; justify-content:space-between; }
.ctrl-name span { color:var(--accent); font-weight:600; font-family:monospace; }

input[type=range] {
  -webkit-appearance:none; width:100%; height:3px; border-radius:2px;
  background:linear-gradient(to right,
    var(--accent) 0%,
    var(--accent) calc(var(--pct,50%) * 1%),
    var(--border2) calc(var(--pct,50%) * 1%));
  outline:none; cursor:pointer;
}
input[type=range]::-webkit-slider-thumb {
  -webkit-appearance:none; width:14px; height:14px; border-radius:50%;
  background:var(--accent); cursor:pointer;
  box-shadow:0 0 6px var(--accent-glow);
}

select {
  width:100%; background:var(--surface2); border:1px solid var(--border2);
  color:var(--text); font-size:12px; padding:6px 8px; border-radius:6px;
}

.toggle-row { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
.toggle { position:relative; width:36px; height:20px; }
.toggle input { opacity:0; width:0; height:0; position:absolute; }
.toggle-track { position:absolute; inset:0; background:var(--border2); border-radius:10px; cursor:pointer; transition:background .2s; }
.toggle input:checked + .toggle-track { background:var(--accent); }
.toggle-thumb { position:absolute; top:2px; left:2px; width:16px; height:16px; border-radius:50%; background:#fff; transition:transform .2s; pointer-events:none; }
.toggle input:checked ~ .toggle-thumb { transform:translateX(16px); }

.btn-row { display:flex; gap:8px; margin-top:4px; }
.btn { flex:1; padding:8px 6px; border-radius:8px; border:1px solid var(--border2);
  background:var(--surface2); color:var(--text2); font-size:12px; font-weight:500; cursor:pointer; transition:all .15s; }
.btn.primary { background:var(--accent-dim); color:var(--accent);
  border-color:color-mix(in srgb, var(--accent) 30%, transparent); }
.btn.primary:hover { background:var(--accent); color:#000; }

════════════════════════════════════════════════════════
  CANVAS AREA + OVERLAY CSS
════════════════════════════════════════════════════════
#canvas-area { flex:1; position:relative; overflow:hidden; }
#canvas-area canvas { position:absolute; top:0; left:0; width:100%; height:100%; }

.ov-btn {
  background:var(--surface2); border:1px solid var(--border);
  color:var(--text2); font-size:11px; padding:5px 10px;
  border-radius:6px; cursor:pointer; transition:all .15s;
}
.ov-btn:hover, .ov-btn.on { background:var(--surface3); color:var(--accent); }

#tip {
  position:absolute; bottom:12px; left:50%; transform:translateX(-50%);
  background:var(--surface2); border:1px solid var(--border);
  color:var(--text2); font-size:11px; padding:6px 14px;
  border-radius:20px; pointer-events:none; white-space:nowrap; z-index:10;
}

════════════════════════════════════════════════════════
  METRICS STRIP CSS
════════════════════════════════════════════════════════
#info-panel {
  height:100px; background:var(--surface); border-top:1px solid var(--border);
  display:flex; align-items:stretch; flex-shrink:0;
}
.metric { flex:1; display:flex; flex-direction:column; justify-content:center;
  padding:12px 16px; border-right:1px solid var(--border); min-width:0; }
.metric:last-child { border-right:none; }
.metric-label { font-size:10px; letter-spacing:.08em; text-transform:uppercase;
  color:var(--text3); margin-bottom:4px; }
.metric-value { font-size:20px; font-weight:600; font-family:monospace; color:var(--text); }
.metric-badge { display:inline-block; font-size:10px; font-weight:600;
  padding:3px 8px; border-radius:4px; margin-top:4px; }
.badge-green  { background:var(--green-dim);  color:var(--green);  }
.badge-red    { background:var(--red-dim);    color:var(--red);    }
.badge-amber  { background:var(--accent-dim); color:var(--accent); }
.badge-violet { background:var(--violet-dim); color:var(--violet); }

════════════════════════════════════════════════════════
  CANVAS DRAWING TECHNIQUES
════════════════════════════════════════════════════════
DPR-AWARE SETUP (required):
  const cvs = document.getElementById('cvs');
  const ctx = cvs.getContext('2d');
  function resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const r = cvs.getBoundingClientRect();
    cvs.width  = r.width  * dpr;
    cvs.height = r.height * dpr;
    ctx.scale(dpr, dpr);
    W = r.width; H = r.height;
    draw();
  }
  window.addEventListener('resize', resizeCanvas);

════════════════════════════════════════════════════════
  INTERACTION WIRING PATTERN
════════════════════════════════════════════════════════
function gv(id) { return parseFloat(document.getElementById(id)?.value ?? 0); }
function gb(id) { return document.getElementById(id)?.checked ?? false; }

function bindSlider(id, displayId, fmt, onChange) {
  const el = document.getElementById(id);
  const dv = document.getElementById(displayId);
  function update() {
    const v = parseFloat(el.value);
    if (dv) dv.textContent = fmt(v);
    const pct = 100 * (v - parseFloat(el.min)) / (parseFloat(el.max) - parseFloat(el.min));
    el.style.setProperty('--pct', pct.toFixed(1));
    onChange(v);
  }
  el.addEventListener('input', update);
  update();
}

function setMetrics(items) {
  const panel = document.getElementById('info-panel');
  panel.innerHTML = items.map(m => `
    <div class="metric">
      <div class="metric-label">${m.label}</div>
      <div class="metric-value">${m.value}</div>
      ${m.sub  ? `<div class="metric-sub">${m.sub}</div>` : ''}
      ${m.badge ? `<div class="metric-badge ${m.badgeClass||'badge-amber'}">${m.badge}</div>` : ''}
    </div>`).join('');
}

window.addEventListener('keydown', e => {
  if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
  if (e.code === 'KeyR')  { resetSim(); }
});

let rafId = null, playing = false;
function togglePlay() {
  playing = !playing;
  if (playing) {
    function loop(ts) {
      if (!playing) return;
      stepSim(ts); draw();
      rafId = requestAnimationFrame(loop);
    }
    rafId = requestAnimationFrame(loop);
  } else {
    cancelAnimationFrame(rafId); rafId = null;
  }
  document.getElementById('btnPlay').textContent = playing ? '⏸ Pause' : '▶ Play';
}

════════════════════════════════════════════════════════
  ONBOARDING / TUTORIAL SYSTEM  (REQUIRED -- every simulation)
════════════════════════════════════════════════════════
Every generated page must include a game-style, first-run interactive
tutorial built from the actual controls generated for THIS simulation.

MARKUP TO ADD (inside #app, after #main, as fixed-position layers):

  <div id="tut-help" title="Replay tutorial" aria-label="Replay tutorial">?</div>

  <div id="tut-root" class="tut-hidden" aria-hidden="true">
    <div id="tut-spot"></div>
    <div id="tut-welcome" class="tut-card tut-modal">
      <div class="tut-eyebrow">Welcome to</div>
      <h2 id="tut-w-title"></h2>
      <p id="tut-w-body"></p>
      <div class="tut-actions">
        <button id="tut-skip-w" class="btn">Skip</button>
        <button id="tut-start" class="btn primary">Start Tour</button>
      </div>
    </div>
    <div id="tut-tooltip" class="tut-card tut-tip">
      <div class="tut-dots" id="tut-dots"></div>
      <div class="tut-step-count" id="tut-count"></div>
      <h3 id="tut-t-title"></h3>
      <p id="tut-t-body"></p>
      <div class="tut-actions">
        <button id="tut-prev" class="btn">Previous</button>
        <button id="tut-skip" class="btn">Skip Tutorial</button>
        <button id="tut-next" class="btn primary">Next</button>
      </div>
    </div>
    <div id="tut-done" class="tut-card tut-modal">
      <div class="tut-eyebrow">You're ready!</div>
      <h2>Now experiment for yourself</h2>
      <p>Have fun exploring -- adjust anything, anytime.</p>
      <div class="tut-actions">
        <button id="tut-finish" class="btn primary">Start Experiment</button>
      </div>
    </div>
  </div>

CSS PATTERNS:
  #tut-root { position:fixed; inset:0; z-index:1000; }
  #tut-root.tut-hidden { display:none; }
  #tut-spot { position:fixed; z-index:1001; border-radius:12px;
    box-shadow:0 0 0 9999px rgba(4,6,10,.78);
    transition:top .35s cubic-bezier(.4,0,.2,1), left .35s cubic-bezier(.4,0,.2,1),
               width .35s cubic-bezier(.4,0,.2,1), height .35s cubic-bezier(.4,0,.2,1),
               opacity .25s; pointer-events:none; }
  #tut-spot.tut-none { opacity:0; }
  .tut-card { position:fixed; z-index:1002; background:var(--surface2);
    border:1px solid var(--border2); border-radius:14px;
    box-shadow:0 12px 40px rgba(0,0,0,.5); padding:20px 22px; max-width:300px;
    opacity:0; transform:translateY(6px) scale(.98);
    transition:opacity .25s, transform .25s; pointer-events:none; }
  .tut-card.tut-visible { opacity:1; transform:translateY(0) scale(1); pointer-events:auto; }
  .tut-modal { max-width:380px; left:50%; top:50%;
    transform:translate(-50%,-46%) scale(.98); }
  .tut-modal.tut-visible { transform:translate(-50%,-50%) scale(1); }
  .tut-eyebrow { font-size:10px; letter-spacing:.12em; text-transform:uppercase;
    color:var(--accent); font-weight:700; margin-bottom:6px; }
  .tut-card h2 { font-size:19px; font-weight:700; color:var(--text); margin-bottom:8px; }
  .tut-card h3 { font-size:14px; font-weight:700; color:var(--text); margin-bottom:6px; }
  .tut-card p  { font-size:12.5px; line-height:1.55; color:var(--text2); }
  .tut-actions { display:flex; gap:8px; margin-top:16px; justify-content:flex-end; }
  .tut-tip .tut-actions { justify-content:space-between; }
  .tut-dots { display:flex; gap:5px; margin-bottom:8px; }
  .tut-dot { width:5px; height:5px; border-radius:50%; background:var(--border2); }
  .tut-dot.tut-dot-active { background:var(--accent); width:14px; border-radius:3px; }
  #tut-help { position:fixed; bottom:16px; right:16px; z-index:999;
    width:32px; height:32px; border-radius:50%; background:var(--surface2);
    border:1px solid var(--border2); color:var(--text2); font-size:13px;
    font-weight:700; display:flex; align-items:center; justify-content:center;
    cursor:pointer; transition:all .15s; }
  #tut-help:hover { background:var(--accent-dim); color:var(--accent); }

JS PATTERN -- DATA-DRIVEN from the exact controls you built:
  const TUT_STEPS = [
    { selector:'#lenSlider', title:'Pendulum Length',
      body:'Controls the length of the pendulum arm. Longer pendulums swing slower.' },
    // one entry per control, overlay button, and metric you generated
  ];
  (function autoFillTutorial() {
    const covered = new Set(TUT_STEPS.map(s => s.selector).filter(Boolean));
    document.querySelectorAll('#controls-panel [id], .ov-btn, #info-panel .metric')
      .forEach(el => {
        const sel = el.id ? '#' + el.id : null;
        if (!sel || covered.has(sel)) return;
        const label = el.closest('.ctrl-row')?.querySelector('.ctrl-name')?.textContent
                    || el.textContent || 'This control';
        TUT_STEPS.push({ selector: sel, title: label.trim(),
          body: 'Adjust this to see how it changes the simulation.' });
        covered.add(sel);
      });
  })();

  let tutIdx = -1, tutWasPlaying = false;
  function tutEl(id) { return document.getElementById(id); }
  function tutPositionSpot(target) {
    const spot = tutEl('tut-spot');
    if (!target) { spot.classList.add('tut-none'); return; }
    const r = target.getBoundingClientRect(), pad = 6;
    spot.classList.remove('tut-none');
    spot.style.top = (r.top - pad) + 'px'; spot.style.left = (r.left - pad) + 'px';
    spot.style.width = (r.width + pad*2) + 'px'; spot.style.height = (r.height + pad*2) + 'px';
  }
  function tutPositionCard(target) {
    const card = tutEl('tut-tooltip');
    if (!target) { card.style.left='50%'; card.style.top='50%';
      card.style.transform='translate(-50%,-50%)'; return; }
    const r = target.getBoundingClientRect();
    const cw = 300, ch = card.offsetHeight || 160, margin = 14;
    let left = r.right + margin, top = r.top;
    if (left + cw > window.innerWidth - 10) left = Math.max(10, r.left - cw - margin);
    if (top + ch > window.innerHeight - 10) top = Math.max(10, window.innerHeight - ch - 10);
    card.style.transform = 'none';
    card.style.left = left + 'px'; card.style.top = top + 'px';
  }
  function tutRenderDots() {
    tutEl('tut-dots').innerHTML = TUT_STEPS.map((_, i) =>
      `<span class="tut-dot${i===tutIdx?' tut-dot-active':''}"></span>`).join('');
    tutEl('tut-count').textContent = `Step ${tutIdx+1} of ${TUT_STEPS.length}`;
  }
  function tutShowStep(i) {
    tutIdx = Math.max(0, Math.min(i, TUT_STEPS.length - 1));
    const step = TUT_STEPS[tutIdx];
    const target = step.selector ? document.querySelector(step.selector) : null;
    tutEl('tut-t-title').textContent = step.title;
    tutEl('tut-t-body').textContent = step.body;
    tutPositionSpot(target); tutPositionCard(target); tutRenderDots();
    tutEl('tut-prev').disabled = tutIdx === 0;
    tutEl('tut-next').textContent = tutIdx === TUT_STEPS.length - 1 ? 'Finish' : 'Next';
    tutEl('tut-tooltip').classList.add('tut-visible');
    tutEl('tut-welcome').classList.remove('tut-visible');
    tutEl('tut-done').classList.remove('tut-visible');
  }
  function tutStart() {
    if (typeof playing !== 'undefined') { tutWasPlaying = playing; if (playing) togglePlay(); }
    tutEl('tut-root').classList.remove('tut-hidden');
    tutEl('tut-root').setAttribute('aria-hidden', 'false');
    tutShowStep(0);
  }
  function tutOpenWelcome() {
    tutEl('tut-root').classList.remove('tut-hidden');
    tutEl('tut-root').setAttribute('aria-hidden', 'false');
    tutPositionSpot(null);
    tutEl('tut-tooltip').classList.remove('tut-visible');
    tutEl('tut-done').classList.remove('tut-visible');
    tutEl('tut-welcome').classList.add('tut-visible');
  }
  function tutEnd(resume) {
    tutEl('tut-root').classList.add('tut-hidden');
    tutEl('tut-root').setAttribute('aria-hidden', 'true');
    ['tut-welcome','tut-tooltip','tut-done'].forEach(id => tutEl(id).classList.remove('tut-visible'));
    if (resume && tutWasPlaying && typeof togglePlay === 'function' && !playing) togglePlay();
  }
  function tutFinishSteps() {
    tutEl('tut-tooltip').classList.remove('tut-visible');
    tutEl('tut-done').classList.add('tut-visible');
  }
  tutEl('tut-help').addEventListener('click', tutOpenWelcome);
  tutEl('tut-start').addEventListener('click', tutStart);
  tutEl('tut-skip-w').addEventListener('click', () => tutEnd(true));
  tutEl('tut-skip').addEventListener('click', () => tutEnd(true));
  tutEl('tut-finish').addEventListener('click', () => tutEnd(true));
  tutEl('tut-next').addEventListener('click', () => {
    if (tutIdx >= TUT_STEPS.length - 1) tutFinishSteps(); else tutShowStep(tutIdx + 1);
  });
  tutEl('tut-prev').addEventListener('click', () => tutShowStep(tutIdx - 1));
  window.addEventListener('keydown', e => {
    if (tutEl('tut-root').classList.contains('tut-hidden')) return;
    if (e.key === 'Escape') tutEnd(true);
    if (e.key === 'ArrowRight' && tutEl('tut-tooltip').classList.contains('tut-visible')) tutEl('tut-next').click();
    if (e.key === 'ArrowLeft') tutEl('tut-prev').click();
  });
  window.addEventListener('resize', () => {
    if (tutIdx >= 0 && !tutEl('tut-root').classList.contains('tut-hidden')) tutShowStep(tutIdx);
  });
  window.addEventListener('load', () => setTimeout(tutOpenWelcome, 300));

════════════════════════════════════════════════════════
  WHAT TO OMIT
════════════════════════════════════════════════════════
- No external URLs of any kind.
- No localStorage / sessionStorage.
- No backend calls -- 100% client-side computation.
- No placeholder numbers disconnected from governing equations.
- No controls that don't visibly change anything.
- No more than 7 controls in the sidebar.
- Do NOT skip the onboarding tutorial system -- it is required.
"""

SYSTEM = """You are SimEngine v2.1 -- an expert interactive-simulation engineer who builds
single-page HTML5 virtual-lab simulations for students and curious learners.

YOUR MISSION: Given ONE topic, concept, or lab experiment, design and build a
COMPLETE, SELF-CONTAINED, INTERACTIVE simulation -- live controls (sliders,
selects, toggles) that drive a real-time canvas (or SVG) visualization, with
a metrics strip showing live computed values. This is a HANDS-ON LAB, not a
narrated slideshow. Everything updates instantly as the learner adjusts controls.

""" + DESIGN_SYSTEM + """

════════════════════════════════════════════════════════
  REQUIRED OUTPUT FORMAT
════════════════════════════════════════════════════════
Return ONLY raw JSON (no markdown, no code fences, no commentary):
{
  "title": "short page title, e.g. 'Pendulum Lab' or 'RC Circuit Lab'",
  "category": "one of: """ + ", ".join(CATEGORIES) + """",
  "summary": "1-2 sentence plain-English description",
  "controls_overview": ["one phrase per control exposed"],
  "key_formula": "the core formula or relationship the simulation is built on",
  "learning_notes": ["2-4 short simple-English sentences"],
  "simulation_code": "COMPLETE SELF-CONTAINED <!DOCTYPE html>...</html> AS A SINGLE PROPERLY-ESCAPED JSON STRING"
}

════════════════════════════════════════════════════════
  CORRECTNESS REQUIREMENTS
════════════════════════════════════════════════════════
- Every number shown must come from a REAL computation based on the actual governing equations.
- Units must be correct and consistently labeled.
- Edge cases must be handled gracefully -- never crash silently.
- For animated topics, use real physics time-stepping (Euler or RK4).

════════════════════════════════════════════════════════
  QUALITY BAR
════════════════════════════════════════════════════════
- Every slider/select/toggle visibly changes the canvas or a metric.
- Canvas drawing must look professional: clear labels, consistent stroke weights.
- Metrics strip shows 3-5 of the MOST MEANINGFUL live-computed values.
- Mobile-responsive down to 380px viewport.
- Include keyboard shortcuts (Space = play/pause, R = reset) when applicable.
- EVERY simulation includes the onboarding/tutorial system.
"""

STRATEGY_TEMPLATES = {
    "PHYSICS_MECHANICS":
        "Canvas MUST draw a physical, visual scene (e.g. pendulums swinging, blocks sliding, "
        "springs bouncing, planets orbiting) rather than just a graph. "
        "REQUIRED equations: Newton's 2nd law F=ma, energy E=KE+PE. "
        "Metrics: period T, velocity, current KE, current PE, total E.",

    "PHYSICS_WAVES_OPTICS":
        "Canvas MUST draw physical optical components (lenses, mirrors, prisms) and visual "
        "light rays/wavefronts on a dark background. "
        "REQUIRED equations: Snell's law; lens/mirror equations. "
        "Metrics: angles θ₁/θ₂, image distance, magnification, critical angle.",

    "ELECTRICITY_CIRCUITS":
        "Canvas MUST draw a visual, interactive circuit diagram (resistors, capacitors, batteries) "
        "with animated particles or arrows showing current flow. "
        "REQUIRED equations: Ohm's V=IR; RC/RLC dynamics. "
        "Metrics: current I, charge Q, time constant τ, power P=V²/R.",

    "CHEMISTRY":
        "Canvas MUST draw a visual laboratory setup (e.g. flasks, beakers, burettes, burners) "
        "with animated liquid colors, bubbles, or particles. Do NOT just draw a graph. If a "
        "titration curve or reaction plot is needed, draw it alongside the physical beaker/flask. "
        "REQUIRED equations: rate laws, equilibrium, Henderson-Hasselbalch, Nernst. "
        "Metrics: rate constant k, pH, concentration, cell potential.",

    "BIOLOGY":
        "Canvas MUST draw visual biological structures (cells dividing, DNA strands, bacteria in a petri dish, "
        "ecosystem agents). Do NOT just draw a population graph. "
        "REQUIRED equations: logistic growth, Michaelis-Menten. "
        "Metrics: population count, growth rate, substrate concentration.",

    "MATH_GEOMETRY":
        "Canvas MUST draw interactive geometric shapes, curves, or fractals on a coordinate plane. "
        "Metrics: area, perimeter, roots, extrema, period.",

    "CS_ALGORITHMS":
        "Canvas MUST draw visual data structures (nodes, trees, arrays) with animated highlights "
        "showing the algorithm's progress step-by-step. "
        "Metrics: comparisons count, swaps count, elapsed steps.",

    "EARTH_ENV_SCIENCE":
        "Canvas MUST draw a visual cross-section of the earth, atmosphere, or environment "
        "(e.g. clouds, ice caps, tectonic plates) rather than just a time-series plot. "
        "Metrics: projected temperature, CO2 concentration, sea level.",

    "ECONOMICS_SOCIAL":
        "Canvas drawing supply-demand curves or market agents. "
        "Metrics: equilibrium price, quantity, consumer/producer surplus.",

    "GENERAL_PROCESS":
        "Canvas MUST draw a highly visual, physical representation of the process "
        "(e.g. machines, agents, fluid flow) alongside any necessary data plots. "
        "Show a metrics strip with the most informative derived values.",
}


def _build_prompt(topic: str, category: str, image_refs: List[dict]) -> tuple:
    strategy      = STRATEGY_TEMPLATES.get(category, STRATEGY_TEMPLATES["GENERAL_PROCESS"])
    image_ctx     = _format_image_refs_for_prompt(image_refs)
    multi_exp_list = _MULTI_EXPERIMENT_TOPICS.get(category, [])
    multi_exp_hint = ""
    if multi_exp_list:
        exp_str = "; ".join(multi_exp_list)
        multi_exp_hint = (
            f"\nMULTI-EXPERIMENT HINT: This category ({category}) suits a sidebar "
            f"experiment-switcher. If the topic '{topic}' is broad enough, add an "
            f"#exp-list with these experiments: [{exp_str}]. If the user gave a very "
            f"specific single-experiment topic, you may omit the switcher.\n"
        )

    system_text = SYSTEM
    user_parts  = [
        f"Build an interactive HTML5 simulation for SimEngine v2.1.\n",
        f"TOPIC / LAB EXPERIMENT: {topic}",
        f"CATEGORY: {category}",
        f"STRATEGY HINT: {strategy}",
    ]
    if multi_exp_hint:
        user_parts.append(multi_exp_hint)
    if image_ctx:
        user_parts.append(f"\n{image_ctx}")
    user_parts += [
        "\nREMINDERS:",
        "- One self-contained HTML page, ZERO external resources.",
        "- Use the sidebar/canvas/metrics layout from the design system exactly.",
        "- Graduate the slider track fill using the --pct CSS custom property trick.",
        "- Implement keyboard shortcuts: Space=play/pause, R=reset (where applicable).",
        "- Canvas drag interaction where it makes physical sense.",
        "- Every control MUST visibly affect canvas AND/OR a metric.",
        "- All computed values MUST follow the real governing equations.",
        "- Include a Play/Animate button + requestAnimationFrame loop if the topic involves motion.",
        "- Mobile-responsive down to 380px viewport width.",
        "- REQUIRED: include the onboarding/tutorial system (#tut-root, #tut-help, spotlight, "
        "welcome/step/done cards) with TUT_STEPS containing one entry for EVERY control, "
        "overlay button, and metric you generated for THIS topic.",
        "\nReturn ONLY raw JSON. simulation_code must be a complete "
        "<!DOCTYPE html>...</html> document as a properly escaped JSON string.",
    ]
    user_content = "\n".join(user_parts)
    return system_text, user_content


# ===========================================================================
#  MODULE 8 -- Response Parsing (fallback chain)
# ===========================================================================

def _parse_response(raw: str, topic: str) -> dict:
    strategies = [
        _parse_direct_json,
        _parse_stripped_json,
        _parse_brace_extracted,
        _parse_field_by_field,
        _parse_markdown_fenced,
        _parse_bare_html,
    ]
    for i, strategy in enumerate(strategies):
        try:
            result = strategy(raw, topic)
            if result:
                SimLogger.ok("Parser", f"Strategy {i+1} ({strategy.__name__}) succeeded")
                return result
        except Exception as e:
            SimLogger.warn("Parser", f"Strategy {i+1} failed: {e}")
    SimLogger.error("Parser", "All strategies failed")
    return {
        "title":             f"Simulation: {topic[:50]}",
        "category":          "GENERAL_PROCESS",
        "summary":           "Generation could not be parsed.",
        "controls_overview": [],
        "key_formula":       "",
        "learning_notes":    [],
        "simulation_code":   "",
    }


def _parse_direct_json(raw, topic):
    data = json.loads(raw)
    return _normalize_parsed(data, topic)


def _parse_stripped_json(raw, topic):
    stripped = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE).strip()
    data = json.loads(stripped)
    return _normalize_parsed(data, topic)


def _parse_brace_extracted(raw, topic):
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return None
    data = json.loads(m.group(0))
    return _normalize_parsed(data, topic)


def _parse_field_by_field(raw, topic):
    def extract_string(field):
        pat = r'"' + re.escape(field) + r'"\s*:\s*"((?:[^"\\]|\\.)*)"'
        m = re.search(pat, raw)
        return _unescape_json_string(m.group(1)) if m else ""

    def extract_array(field):
        pat = r'"' + re.escape(field) + r'"\s*:\s*\[(.*?)\]'
        m = re.search(pat, raw, re.DOTALL)
        if not m:
            return []
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
        return [_unescape_json_string(s) for s in items]

    code = _extract_simulation_code_field(raw)
    if not code:
        return None
    return {
        "title":             extract_string("title") or f"Simulation: {topic[:50]}",
        "category":          extract_string("category") or "GENERAL_PROCESS",
        "summary":           extract_string("summary") or "Interactive simulation",
        "controls_overview": extract_array("controls_overview"),
        "key_formula":       extract_string("key_formula"),
        "learning_notes":    extract_array("learning_notes"),
        "simulation_code":   code,
    }


def _extract_simulation_code_field(raw):
    key_pos = raw.find('"simulation_code"')
    if key_pos == -1:
        return ""
    colon_pos = raw.find(':', key_pos)
    if colon_pos == -1:
        return ""
    after_colon = raw[colon_pos + 1:].lstrip()
    if not after_colon.startswith('"'):
        return ""
    content = after_colon[1:]
    end = _find_json_string_end(content)
    if end == -1:
        return ""
    return _unescape_json_string(content[:end])


def _parse_markdown_fenced(raw, topic):
    stripped = raw.strip()
    fence_match = re.match(r'^```(?:html|json)?\s*\n?(.*?)\n?```$', stripped, re.DOTALL | re.IGNORECASE)
    if fence_match:
        inner = fence_match.group(1).strip()
        try:
            data = json.loads(inner)
            result = _normalize_parsed(data, topic)
            if result:
                return result
        except Exception:
            pass
        for marker in ['<!DOCTYPE html>', '<html', '<svg']:
            idx = inner.find(marker)
            if idx != -1:
                end = inner.rfind('</html>')
                code = inner[idx:end + 7] if end != -1 else inner[idx:]
                if len(code) > 200:
                    return {
                        "title":             f"Simulation: {topic[:50]}",
                        "category":          "GENERAL_PROCESS",
                        "summary":           "Interactive simulation",
                        "controls_overview": [],
                        "key_formula":       "",
                        "learning_notes":    [],
                        "simulation_code":   code.strip(),
                    }
    for pat in [r'```html\s*\n(.*?)\n```', r'```json\s*\n(.*?)\n```']:
        m = re.search(pat, raw, re.DOTALL | re.IGNORECASE)
        if m:
            inner = m.group(1).strip()
            try:
                data = json.loads(inner)
                result = _normalize_parsed(data, topic)
                if result:
                    return result
            except Exception:
                pass
            for marker in ['<!DOCTYPE html>', '<html']:
                idx = inner.find(marker)
                if idx != -1:
                    end = inner.rfind('</html>')
                    code = inner[idx:end + 7] if end != -1 else inner[idx:]
                    if len(code) > 200:
                        return {
                            "title":             f"Simulation: {topic[:50]}",
                            "category":          "GENERAL_PROCESS",
                            "summary":           "Interactive simulation",
                            "controls_overview": [],
                            "key_formula":       "",
                            "learning_notes":    [],
                            "simulation_code":   code.strip(),
                        }
    return None


def _parse_bare_html(raw, topic):
    for marker in ['<!DOCTYPE html>', '<html', '<svg']:
        idx = raw.find(marker)
        if idx != -1:
            end = raw.rfind('</html>')
            code = raw[idx:end + 7] if end != -1 else raw[idx:]
            if len(code) > 200:
                return {
                    "title":             f"Simulation: {topic[:50]}",
                    "category":          "GENERAL_PROCESS",
                    "summary":           "Interactive simulation",
                    "controls_overview": [],
                    "key_formula":       "",
                    "learning_notes":    [],
                    "simulation_code":   code.strip(),
                }
    return None


def _normalize_parsed(data, topic):
    if not isinstance(data, dict):
        raise ValueError("Not a dict")
    result = {
        "title":           str(data.get("title") or "").strip() or f"Simulation: {topic[:50]}",
        "category":        str(data.get("category") or "").strip() or "GENERAL_PROCESS",
        "summary":         str(data.get("summary") or "").strip() or "Interactive simulation",
        "key_formula":     str(data.get("key_formula") or "").strip(),
        "simulation_code": str(data.get("simulation_code") or "").strip(),
    }
    controls = data.get("controls_overview")
    result["controls_overview"] = controls if isinstance(controls, list) else []
    notes = data.get("learning_notes")
    result["learning_notes"] = notes if isinstance(notes, list) else []
    return result


def _find_json_string_end(s):
    i = 0
    while i < len(s):
        if s[i] == '\\':
            i += 2
        elif s[i] == '"':
            return i
        else:
            i += 1
    return -1


def _unescape_json_string(s):
    return (s.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
             .replace('\\r', '\r').replace("\\'", "'").replace('\\\\', '\\'))


# ===========================================================================
#  MODULE 9 -- Generation Pipeline
# ===========================================================================

def _build_failure_result(topic, reason):
    fallback = RecoveryEngine.fallback_html(topic, reason)
    return {
        "title":             f"Simulation: {topic[:50]}",
        "category":          "GENERAL_PROCESS",
        "summary":           "Generation failed",
        "controls_overview": [],
        "key_formula":       "",
        "learning_notes":    [],
        "image_refs":        [],
        "html":              fallback,
        "engine_version":    "v2.1",
        "render_status":     "error",
        "error_reason":      reason,
    }


async def _run_generation_pipeline(topic: str) -> dict:
    short_topic = topic[:80] + ("..." if len(topic) > 80 else "")
    SimLogger.info("Pipeline", f"START v2.1 -- '{short_topic}'")

    # Step 1: Classify
    category = await _classify_topic(topic)
    SimLogger.info("Classifier", f"Category: {category}")

    # Step 2: Image references (blocking urllib → worker thread)
    image_refs = await asyncio.to_thread(_fetch_image_refs, topic)

    # Step 3: Build prompt
    system_text, user_content = _build_prompt(topic, category, image_refs)

    # Step 4: Generate via Gemini (mirrors q_animation._call_gemini pattern exactly)
    try:
        try:
            config = _genai_types.GenerateContentConfig(
                system_instruction=system_text,
                temperature=0.7,
                max_output_tokens=MAX_TOK,
                thinking_config=_genai_types.ThinkingConfig(thinking_level="low"),
            )
        except Exception:
            # ThinkingConfig not supported on this SDK version — use minimal config
            config = _genai_types.GenerateContentConfig(
                system_instruction=system_text,
                temperature=0.7,
                max_output_tokens=MAX_TOK,
            )
        response = await _gemini_client.aio.models.generate_content(
            model=SIM_MODEL,
            contents=user_content,
            config=config,
        )
        raw    = (response.text or "").strip()
        finish = getattr(response.candidates[0], 'finish_reason', 'unknown') if response.candidates else 'unknown'
        SimLogger.info("GenerationAI", f"model={SIM_MODEL}  finish_reason={finish}  len={len(raw)}")
        if finish in ('MAX_TOKENS', 'max_tokens', 2):
            SimLogger.warn("GenerationAI", "Hit max_output_tokens -- output may be truncated!")

    except Exception as e:
        err_str = str(e)
        is_model_not_found = (
            "404" in err_str or "NOT_FOUND" in err_str or
            "not found" in err_str.lower() or
            "is not supported for generateContent" in err_str
        )
        is_auth_error = (
            "401" in err_str or "403" in err_str or
            "UNAUTHENTICATED" in err_str or "API_KEY_INVALID" in err_str
        )
        if is_model_not_found:
            SimLogger.error(
                "GenerationAI",
                f"CRITICAL: Model '{SIM_MODEL}' does not exist or is not supported. "
                f"Set SIM_MODEL env var to a valid model (e.g. 'gemini-3.1-pro-preview'). "
                f"Raw error: {err_str}"
            )
            return _build_failure_result(
                topic,
                f"Model '{SIM_MODEL}' not found. Check your SIM_MODEL environment variable."
            )
        elif is_auth_error:
            SimLogger.error(
                "GenerationAI",
                f"CRITICAL: API authentication failed ({err_str[:200]}). "
                f"Check GEMINI_API_KEY environment variable."
            )
            return _build_failure_result(topic, "API authentication failed. Check your GEMINI_API_KEY.")
        else:
            SimLogger.error("GenerationAI", f"API call failed: {err_str}")
            return _build_failure_result(topic, f"API error: {err_str}")

    # Step 5: Parse
    result = _parse_response(raw, topic)
    result["category"]   = result.get("category") or category
    result["image_refs"] = image_refs
    sim_html = result.get("simulation_code", "").strip()

    if not sim_html:
        SimLogger.error("Pipeline", "No simulation_code could be parsed from the response")
        return _build_failure_result(topic, "Could not parse simulation HTML from model response")

    # Step 6: Auto-repair truncated closing tags before validation
    sim_html = GenerationValidator.repair(sim_html)

    # Step 7: Sanitize
    sim_html = HtmlSanitizer.sanitize(sim_html)

    # Step 8: Validate
    try:
        GenerationValidator.validate(sim_html, require_svg=False, require_canvas=False)
    except ValidationError as e:
        SimLogger.warn("Validator", f"Validation failed: {e}")
        if ('<canvas' in sim_html or '<svg' in sim_html) and len(sim_html) > 400:
            sim_html = RecoveryEngine.partial_html(topic, sim_html)
            SimLogger.warn("Pipeline", "Wrapped partial content via RecoveryEngine.partial_html")
        else:
            return _build_failure_result(topic, str(e))

    result["html"]           = sim_html
    result["engine_version"] = "v2.1"
    result["render_status"]  = "ok"

    SimLogger.ok("Pipeline", (
        f"DONE -- '{result['title']}'  category={result['category']}"
        f"  html={len(sim_html):,} chars"
        f"  controls={len(result.get('controls_overview', []))}"
        f"  image_refs={len(image_refs)}"
    ))
    return result


# ===========================================================================
#  Public API
# ===========================================================================

async def generate_simulation(topic: str) -> dict:
    """
    Public async entry point. Never raises — returns a graceful fallback
    page on any error.

    Returns dict with keys: title, category, summary, controls_overview,
    key_formula, learning_notes, image_refs, html, engine_version,
    render_status. On failure: render_status='error', error_reason set.
    """
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("Topic cannot be empty")
    try:
        return await asyncio.wait_for(
            _run_generation_pipeline(topic), timeout=PIPELINE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        SimLogger.error(
            "Pipeline",
            f"Pipeline exceeded {PIPELINE_TIMEOUT_SECONDS:.0f}s wall-clock cap -- "
            "returning graceful timeout result")
        return _build_failure_result(
            topic,
            f"Generation took longer than {PIPELINE_TIMEOUT_SECONDS:.0f}s and was stopped. "
            "Please try again -- shorter or more specific topics generate faster.")
    except Exception as e:
        SimLogger.error("Pipeline", f"UNHANDLED error -- falling back gracefully: {e}")
        return _build_failure_result(topic, f"Unexpected error: {e}")


def generate_simulation_sync(topic: str) -> dict:
    """Synchronous wrapper around generate_simulation() for non-async callers."""
    return asyncio.run(generate_simulation(topic))


# ---------------------------------------------------------------------------
# Streaming entry point (prevents gateway 502s on long generations)
# ---------------------------------------------------------------------------
# Wire this as SSE in FastAPI:
#
#   from fastapi.responses import StreamingResponse
#   @app.post("/generate-simulation-stream")
#   async def stream_endpoint(topic: str):
#       async def sse():
#           async for event in generate_simulation_stream(topic):
#               yield f"data: {json.dumps(event)}\n\n"
#       return StreamingResponse(sse(), media_type="text/event-stream")
#
# Events emitted:
#   {"type": "status",  "stage": str, "message": str}
#   {"type": "chunk",   "text": str}
#   {"type": "done",    "result": <same shape as generate_simulation()>}
#   {"type": "error",   "result": <failure result dict>}
# ---------------------------------------------------------------------------

async def generate_simulation_stream(topic: str):
    """
    Async generator yielding progress events for the full generation pipeline.
    Streams model tokens continuously so gateway inactivity timeouts don't fire.
    Never raises — failures come as {"type": "error", ...} events.
    """
    topic = (topic or "").strip()
    if not topic:
        yield {"type": "error", "result": _build_failure_result("", "Topic cannot be empty")}
        return

    short_topic = topic[:80] + ("..." if len(topic) > 80 else "")
    SimLogger.info("Pipeline", f"START (stream) v2.1 -- '{short_topic}'")

    try:
        yield {"type": "status", "stage": "classify", "message": "Classifying topic..."}
        category = await asyncio.wait_for(_classify_topic(topic), timeout=CLIENT_TIMEOUT_SECONDS)
        SimLogger.info("Classifier", f"Category: {category}")

        yield {"type": "status", "stage": "image_refs", "message": "Gathering visual references..."}
        image_refs = await asyncio.wait_for(
            asyncio.to_thread(_fetch_image_refs, topic), timeout=CLIENT_TIMEOUT_SECONDS)

        system_text, user_content = _build_prompt(topic, category, image_refs)

        yield {"type": "status", "stage": "generating", "message": "Generating simulation..."}
        raw_parts: List[str] = []
        try:
            stream_config = _genai_types.GenerateContentConfig(
                system_instruction=system_text,
                temperature=0.7,
                max_output_tokens=MAX_TOK,
                thinking_config=_genai_types.ThinkingConfig(thinking_level="low"),
            )
        except Exception:
            stream_config = _genai_types.GenerateContentConfig(
                system_instruction=system_text,
                temperature=0.7,
                max_output_tokens=MAX_TOK,
            )
        async for chunk in await _gemini_client.aio.models.generate_content_stream(
            model=SIM_MODEL,
            contents=user_content,
            config=stream_config,
        ):
            text = chunk.text or ""
            if text:
                raw_parts.append(text)
                yield {"type": "chunk", "text": text}
        raw = "".join(raw_parts).strip()
        SimLogger.info("GenerationAI", f"model={SIM_MODEL}  len={len(raw)}")


        result   = _parse_response(raw, topic)
        result["category"]   = result.get("category") or category
        result["image_refs"] = image_refs
        sim_html = result.get("simulation_code", "").strip()

        if not sim_html:
            SimLogger.error("Pipeline", "No simulation_code could be parsed from the response")
            yield {"type": "error", "result": _build_failure_result(
                topic, "Could not parse simulation HTML from model response")}
            return

        # Auto-repair truncated closing tags before validation
        sim_html = GenerationValidator.repair(sim_html)

        sim_html = HtmlSanitizer.sanitize(sim_html)

        try:
            GenerationValidator.validate(sim_html, require_svg=False, require_canvas=False)
        except ValidationError as e:
            SimLogger.warn("Validator", f"Validation failed: {e}")
            if ('<canvas' in sim_html or '<svg' in sim_html) and len(sim_html) > 400:
                sim_html = RecoveryEngine.partial_html(topic, sim_html)
            else:
                yield {"type": "error", "result": _build_failure_result(topic, str(e))}
                return


        result["html"]           = sim_html
        result["engine_version"] = "v2.1"
        result["render_status"]  = "ok"

        SimLogger.ok("Pipeline", f"DONE (stream) -- '{result['title']}'  html={len(sim_html):,} chars")
        yield {"type": "done", "result": result}

    except asyncio.TimeoutError:
        SimLogger.error("Pipeline", "Stage exceeded its timeout during streaming pipeline")
        yield {"type": "error", "result": _build_failure_result(
            topic, "Generation took too long and was stopped. Please try again.")}
    except Exception as e:
        err_str = str(e)
        is_model_not_found = (
            "404" in err_str or "NOT_FOUND" in err_str or
            "not found" in err_str.lower() or
            "is not supported for generateContent" in err_str
        )
        is_auth_error = (
            "401" in err_str or "403" in err_str or
            "UNAUTHENTICATED" in err_str or "API_KEY_INVALID" in err_str
        )
        if is_model_not_found:
            SimLogger.error(
                "Pipeline",
                f"CRITICAL: Model '{SIM_MODEL}' does not exist or is not supported. "
                f"Set SIM_MODEL env var to 'gemini-3.1-pro-preview'. Raw error: {err_str}"
            )
            yield {"type": "error", "result": _build_failure_result(
                topic, f"Model '{SIM_MODEL}' not found. Check your SIM_MODEL env var.")}
        elif is_auth_error:
            SimLogger.error("Pipeline", f"CRITICAL: API auth failed. Check GEMINI_API_KEY. ({err_str[:200]})")
            yield {"type": "error", "result": _build_failure_result(
                topic, "API authentication failed. Check your GEMINI_API_KEY.")}
        else:
            SimLogger.error("Pipeline", f"UNHANDLED error in streaming pipeline: {err_str}")
            yield {"type": "error", "result": _build_failure_result(topic, f"Unexpected error: {err_str}")}


# ===========================================================================
#  CLI TEST
# ===========================================================================
if __name__ == "__main__":
    import sys

    TEST_TOPICS = {
        "MECHANICS":  "Simple pendulum with adjustable length, gravity, and damping",
        "OPTICS":     "Convex lens image formation with adjustable object distance and focal length",
        "CIRCUITS":   "RC circuit charging and discharging through a resistor",
        "BIOLOGY":    "Logistic population growth with adjustable growth rate and carrying capacity",
        "MATH":       "Unit circle and the sine/cosine waveform it traces out",
        "ALGORITHMS": "Bubble sort visualized step by step on a random array",
        "CHEMISTRY":  "Gas particles in a container demonstrating Boyle's law",
        "WAVES":      "Double-slit interference pattern with adjustable slit separation",
        "EARTH":      "Carbon cycle and greenhouse gas concentration vs temperature",
        "ECONOMICS":  "Supply and demand curves with adjustable elasticity and tax",
    }

    if len(sys.argv) > 1:
        topics_to_test = {"CUSTOM": " ".join(sys.argv[1:])}
    else:
        key = "MECHANICS"
        topics_to_test = {key: TEST_TOPICS[key]}

    for cat, t in topics_to_test.items():
        print("=" * 72)
        print(f"  SimEngine v2.1 | {cat}")
        print(f"  Topic: {t[:65]}")
        print("=" * 72)

        t0      = time.time()
        result  = generate_simulation_sync(t)
        elapsed = time.time() - t0

        print(f"\nTitle           : {result.get('title','N/A')}")
        print(f"Category        : {result.get('category','N/A')}")
        print(f"Render Status   : {result.get('render_status','N/A')}")
        print(f"Engine Version  : {result.get('engine_version','N/A')}")
        print(f"Summary         : {result.get('summary','')[:140]}")
        print(f"Key Formula     : {result.get('key_formula','')[:80]}")
        print(f"Controls        : {result.get('controls_overview',[])}")
        print(f"Learning Notes  : {len(result.get('learning_notes',[]))} note(s)")
        print(f"Image Refs      : {len(result.get('image_refs',[]))} image(s)")
        html_out = result.get("html", "")
        print(f"HTML Size       : {len(html_out):,} chars")
        print(f"Total Time      : {elapsed:.1f}s")

        if result.get("error_reason"):
            print(f"Error Reason    : {result['error_reason']}")

        slug     = cat.lower()
        out_path = f"sim_{slug}.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_out)
        print(f"\nSaved -> {out_path}")
        print()
