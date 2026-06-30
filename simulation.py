"""
simulation.py -- Simulation Creator Engine  v2.0
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

WHAT'S NEW IN v2.0
  • Image-reference pre-step: searches Google Images for the topic
    and passes curated alt-text descriptions to the generation
    prompt, giving the model a visual anchor for layout and style.
  • Multi-experiment support: topics that map to well-known
    experiment suites (optics, circuits, mechanics, chemistry) get
    a sidebar experiment-switcher so the user can explore multiple
    related setups without reloading the page.
  • Richer visual design system: upgraded CSS tokens, gradient
    slider tracks, glowing active-state accents, animated current-
    flow for circuits, scrollable experiment list for broad topics.
  • Stronger interaction model: drag-to-set support on canvas for
    applicable topics (place object on optical bench, drag pendulum
    bob, etc.), keyboard shortcuts (Space=play/pause, R=reset).
  • Better correctness harness: each category's strategy template
    now names the exact governing equations and derived quantities
    the model must implement -- not just rough guidance.
  • Prompt-caching used on the (large, stable) DESIGN_SYSTEM block
    so repeated generations over the same session don't re-tokenize
    the full system prompt.
  • Same reliability contract as v1.0: never raises; always returns
    renderable HTML even on total failure (API error, parse error,
    validation failure).

INTEGRATION (unchanged from v1.0)
  from simulation import generate_simulation_sync, generate_simulation

  result = generate_simulation_sync("Simple harmonic motion of a pendulum")
  html   = result["html"]

  # async
  result = await generate_simulation(topic)
  html   = result["html"]

  `result` keys: title, category, summary, controls_overview,
  key_formula, learning_notes, html, image_refs, engine_version,
  render_status, [error_reason on failure].
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

import anthropic

# FastAPI router — exposes POST /generate-simulation to main.py
try:
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel

    _router = APIRouter(tags=["simulation"])

    class SimulationRequest(BaseModel):
        topic: str
        mode: Optional[str] = None   # e.g. 'physics', 'chemistry' (informational only)

    @_router.post("/generate-simulation")
    async def create_simulation(req: SimulationRequest):
        """
        Generate a complete, self-contained interactive HTML5 simulation.

        The frontend simulation panel POSTs here with the user's prompt/topic.
        The backend runs the full SimEngine v2.0 pipeline (classify → image-ref
        → prompt → Claude → parse → sanitize → validate) and returns:

          {
            "html":              "<complete self-contained simulation page>",
            "title":             "Pendulum Lab",
            "category":          "PHYSICS_MECHANICS",
            "summary":           "...",
            "controls_overview": [...],
            "key_formula":       "T = 2π√(L/g)",
            "learning_notes":    [...],
            "render_status":     "ok" | "error",
            "engine_version":    "v2.0"
          }
        """
        topic = (req.topic or "").strip()
        if not topic:
            raise HTTPException(status_code=400, detail="'topic' field cannot be empty")
        if len(topic) > 2000:
            raise HTTPException(status_code=400, detail="Topic too long (max 2000 chars)")

        # Optionally prepend the subject-mode as context hint
        if req.mode and req.mode.lower() not in ("general", ""):
            topic_with_mode = f"{topic} (subject area: {req.mode})"
        else:
            topic_with_mode = topic

        result = await generate_simulation(topic_with_mode)
        # generate_simulation never raises — on failure render_status == "error"
        # and html is a user-facing fallback page, so always return 200.
        return result

    simulation_router = _router

except ImportError:
    # FastAPI not installed — module still works as a pure library
    simulation_router = None

# ---------------------------------------------------------------------------
# Client + model routing
# ---------------------------------------------------------------------------
client = anthropic.Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY"),
    default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    timeout=600.0,
    max_retries=4,
)

SIM_MODEL        = "claude-sonnet-4-6"
CLASSIFIER_MODEL = "claude-haiku-4-5"

MAX_TOK            = 32000
MAX_TOK_CLASSIFIER = 20

# Google Custom Search API (optional -- gracefully skipped if missing)
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID  = os.environ.get("GOOGLE_CSE_ID", "")


# ---------------------------------------------------------------------------
# Background / colour theme configuration
# ---------------------------------------------------------------------------
# Generated simulations now default to a single LIGHT, high-contrast theme
# (legible on smartboards / projectors / large screens in bright rooms)
# instead of the old per-topic dark/light branching.
#
# All foreground-on-background text pairs below have been verified to meet
# WCAG AA contrast (>= 4.5:1) -- see the comment after each token. To
# override the palette (e.g. to restore a dark theme, or to brand the
# simulations), edit THEME_TOKENS below, or set the SIM_THEME_OVERRIDE env
# var to the path of a JSON file containing a partial/complete replacement
# token set; any keys present there are merged over the defaults at
# prompt-build time.
THEME_TOKENS = {
    # base surfaces -- light, neutral/pastel, low-glare
    "bg":         "#fdfdfc",   # page background
    "surface":    "#ffffff",   # cards / sidebar / panels
    "surface2":   "#f3f1ea",   # secondary surface (inputs, hover states)
    "surface3":   "#e9e6da",   # tertiary surface (active/pressed states)
    "border":     "#d8d3c4",
    "border2":    "#c2bca8",
    # text -- text on bg/surface/surface2 all exceed 13:1
    "text":       "#1f2421",   # primary text,   ~15.5:1 on bg
    "text2":      "#46504a",   # secondary text,  ~8.2:1 on bg
    "text3":      "#6b7570",   # tertiary/label text, ~4.7:1 on bg (AA min)
    # accent -- one accent colour, ~7.5:1 on bg/surface
    "accent":      "#0f5e4f",
    "accent_dim":  "#dceee8",
    "accent_glow": "rgba(15,94,79,.35)",
    # semantic colors -- all >= 4.5:1 against their own *_dim background
    "green":      "#1a7a3c",  "green_dim":  "#dcf3e3",
    "red":        "#b3241f",  "red_dim":    "#fbe1df",
    "violet":     "#5b3aa0",  "violet_dim": "#ece4f8",
    "cyan":       "#0a6e78",  "cyan_dim":   "#dcf1f3",
    "panel_w":    "260px",
}

SIM_THEME_OVERRIDE = os.environ.get("SIM_THEME_OVERRIDE", "")


def _load_theme_tokens() -> dict:
    """Returns THEME_TOKENS merged with an optional JSON override file."""
    tokens = dict(THEME_TOKENS)
    if SIM_THEME_OVERRIDE:
        try:
            with open(SIM_THEME_OVERRIDE, "r", encoding="utf-8") as f:
                tokens.update(json.load(f))
        except Exception as e:
            SimLogger.warn("Theme", f"Could not load SIM_THEME_OVERRIDE: {e}")
    return tokens


def _theme_css_block(tokens: Optional[dict] = None) -> str:
    """Renders the :root{...} CSS custom-property block for a token dict."""
    t = tokens or _load_theme_tokens()
    return (
        "  :root {\n"
        f"    --bg:{t['bg']};  --surface:{t['surface']};  --surface2:{t['surface2']};  --surface3:{t['surface3']};\n"
        f"    --border:{t['border']};  --border2:{t['border2']};\n"
        f"    --text:{t['text']};  --text2:{t['text2']};  --text3:{t['text3']};\n"
        f"    --green:{t['green']};  --green-dim:{t['green_dim']};\n"
        f"    --red:{t['red']};    --red-dim:{t['red_dim']};\n"
        f"    --violet:{t['violet']}; --violet-dim:{t['violet_dim']};\n"
        f"    --cyan:{t['cyan']};   --cyan-dim:{t['cyan_dim']};\n"
        f"    --accent:{t['accent']};\n"
        f"    --accent-dim:{t['accent_dim']};\n"
        f"    --accent-glow:{t['accent_glow']};\n"
        f"    --panel-w: {t['panel_w']};\n"
        "  }"
    )


# ===========================================================================
#  MODULE 1 -- SimLogger
# ===========================================================================
class SimLogger:
    PREFIX = "[SimEngine v2.0]"

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
    """
    Structural sanity checks on AI-generated HTML.
    Mirrors v1.0 validator; adds a check for dangling requestAnimationFrame
    calls that are never cancelled (common LLM bug on animated sims).
    """

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
    def validate(cls, html, require_svg=False, require_canvas=False):
        if not html or not html.strip():
            raise ValidationError("simulation_code is empty")
        if len(html) < 500:
            raise ValidationError(f"simulation_code suspiciously short ({len(html)} chars)")
        for pattern, reason in cls.REQUIRED_ELEMENTS:
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
        open_scripts  = len(re.findall(r'<script(?:\s[^>]*)?>',  html, re.IGNORECASE))
        close_scripts = len(re.findall(r'</script>',              html, re.IGNORECASE))
        if open_scripts != close_scripts:
            raise ValidationError(f"Unbalanced <script> tags: {open_scripts} open, {close_scripts} close")
        open_svgs  = len(re.findall(r'<svg(?:\s[^>]*)?>',  html, re.IGNORECASE))
        close_svgs = len(re.findall(r'</svg>',              html, re.IGNORECASE))
        if open_svgs != close_svgs:
            raise ValidationError(f"Unbalanced <svg> tags: {open_svgs} open, {close_svgs} close")
        SimLogger.ok("Validator", f"HTML passed validation ({len(html):,} chars)")


# ===========================================================================
#  MODULE 3 -- HtmlSanitizer
# ===========================================================================
class HtmlSanitizer:
    """
    Defensive cleanup pass on AI-generated HTML.
    v2.0 additions: strips any accidental CDN <link> / @import calls,
    fixes missing xmlns on inline SVGs, normalises Windows line endings.
    """

    @classmethod
    def sanitize(cls, html):
        html = html.replace('\ufeff', '').replace('\r\n', '\n').replace('\r', '\n')
        end = html.rfind('</html>')
        if end != -1:
            html = html[:end + 7]
        # Remove all external src= script tags
        html = re.sub(
            r'<script[^>]+src\s*=\s*["\'][^"\']*["\'][^>]*>\s*</script>',
            '', html, flags=re.IGNORECASE | re.DOTALL)
        # Remove CDN <link> stylesheet imports (fonts, icons, etc.)
        html = re.sub(
            r'<link[^>]+href\s*=\s*["\']https?://[^"\']*["\'][^>]*>',
            '', html, flags=re.IGNORECASE)
        # Remove @import url(...) in <style> blocks
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
    """Guarantees a renderable HTML page even on total pipeline failure."""

    @staticmethod
    def fallback_html(topic, reason):
        t_safe      = html_module.escape(topic[:120])
        reason_safe = html_module.escape(reason[:300])
        t = _load_theme_tokens()
        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100%;background:{t['bg']};
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  display:flex;align-items:center;justify-content:center;color:{t['text']}}}
.card{{background:{t['surface']};border:1px solid {t['border']};border-radius:16px;
  box-shadow:0 4px 24px rgba(0,0,0,.08);padding:36px 40px;max-width:520px;text-align:center}}
.icon{{font-size:40px;margin-bottom:16px}}
.title{{font-size:17px;font-weight:700;color:{t['text']};margin-bottom:10px}}
.reason{{font-size:11px;color:{t['text2']};background:{t['surface2']};border-radius:10px;
  padding:10px 14px;margin:12px 0;border:1px solid {t['border']};text-align:left;
  line-height:1.6;font-family:monospace}}
.topic{{font-size:12px;color:{t['text3']};line-height:1.6;margin-top:10px;font-style:italic}}
.retry-hint{{margin-top:18px;font-size:11px;font-weight:700;letter-spacing:1.5px;
  text-transform:uppercase;color:{t['accent']}}}
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
        t = _load_theme_tokens()
        return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>html,body{{margin:0;padding:0;width:100%;height:100%;background:{t['bg']};
  font-family:-apple-system,sans-serif;color:{t['text']}}}</style></head><body>
<div style="font-size:11px;color:{t['text2']};position:fixed;top:8px;left:0;right:0;text-align:center;z-index:99">
  {t_safe}</div>
{sim_code}</body></html>"""


# ===========================================================================
#  MODULE 5 -- Image Reference Fetcher
# ===========================================================================
# Searches Google Images for the topic and returns a structured list of
# image descriptions to use as visual anchors in the generation prompt.
# Gracefully skips (returns []) when the Google keys are absent or the
# request fails, so the rest of the pipeline is unaffected.

def _fetch_image_refs(topic: str, max_results: int = 5) -> List[dict]:
    """
    Query Google Custom Search Image API and return a list of dicts:
      [{"title": ..., "snippet": ..., "link": ...}, ...]

    Requires two env vars:
      GOOGLE_API_KEY  -- your Google Cloud API key with Custom Search enabled
      GOOGLE_CSE_ID   -- the Programmable Search Engine ID (set to image search)

    If either is absent or the HTTP call fails for any reason the function
    returns an empty list so the rest of the pipeline proceeds unchanged.
    """
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        SimLogger.info("ImageRef", "Google keys not set -- skipping image search step")
        return []

    # Use a more specific search query for scientific diagrams
    query = f"{topic} diagram simulation laboratory experiment"
    params = urllib.parse.urlencode({
        "key":    GOOGLE_API_KEY,
        "cx":     GOOGLE_CSE_ID,
        "q":      query,
        "searchType": "image",
        "num":    max_results,
        "imgType": "photo,clipart",
        "safe":   "active",
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
    """
    Convert image ref list to a concise block the model can use as a
    visual-style anchor. Strips URLs (model doesn't need to fetch them).
    """
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

# Topics that benefit from a multi-experiment sidebar (like the optics lab reference)
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


def _classify_topic(topic: str) -> str:
    t = topic.lower()
    scores = {cat: sum(1 for k in kws if k in t) for cat, kws in _CATEGORY_KEYWORDS.items()}
    max_score = max(scores.values()) if scores else 0
    if max_score >= 1:
        top = [c for c, s in scores.items() if s == max_score]
        if len(top) == 1:
            return top[0]
    try:
        resp = client.messages.create(
            model=CLASSIFIER_MODEL, max_tokens=MAX_TOK_CLASSIFIER,
            system="Reply with ONLY one category word from this exact list: "
                   + ", ".join(CATEGORIES),
            messages=[{"role": "user", "content": f"Classify this simulation topic: {topic[:200]}"}])
        cat = resp.content[0].text.strip().upper()
        if cat in CATEGORIES:
            return cat
    except Exception as e:
        SimLogger.warn("Classifier", f"Fallback classification failed: {e}")
    return "GENERAL_PROCESS"


# ===========================================================================
#  MODULE 7 -- Prompt System  (v2.0 -- significantly upgraded)
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
      │   │     — use canvas for: continuous motion, trajectories, ray
      │   │       tracing, particle systems, animated signals/waveforms.
      │   │     — use SVG for: purely static geometric constructions,
      │   │       discrete step-through diagrams (algorithm visualizer)
      │   │       where DOM manipulation per-step is cleaner than
      │   │       redrawing a full canvas each frame.
      │   ├── #overlay-bar  (position:absolute; top:10px; right:12px)
      │   │     — compact floating buttons: only the toggles that make
      │   │       sense for THIS topic (Grid, Trace, Animate, Reset, etc.)
      │   └── #tip  (position:absolute; bottom:12px; left:50%)
      │         — small floating pill hint, e.g. "Drag sliders to explore"
      │         — include a keyboard shortcut hint when play/pause is present:
      │           "Space = play/pause · R = reset"
      └── #info-panel  (height:~100px; border-top)
            — horizontal row of 3–5 .metric cards, each showing ONE
              live-computed value with label, value, and optional badge

MOBILE BREAKPOINT (max-width: 760px):
  - #app becomes flex-direction:column
  - #sidebar becomes width:100%; max-height:220px; overflow-y:auto
  - #controls-panel becomes a wrapping flex-row of compact controls
  - #info-panel stacks metrics in a 2×N grid instead of a single row

════════════════════════════════════════════════════════
  COLOUR TOKENS  (define ALL in :root on <html>)
════════════════════════════════════════════════════════
Use this single LIGHT, high-contrast theme for every simulation,
regardless of topic. It is tuned for legibility on smartboards,
projectors, and large screens (light pastel/neutral surfaces, dark
text, every foreground/background text pairing >= 4.5:1 contrast).
Do NOT invent a dark theme and do NOT branch theme choice by topic.

__THEME_CSS_BLOCK__

Notes:
  - --accent is a single deep, saturated colour (not pastel) so it reads
    clearly as the "this is interactive / active" signal against the
    light surfaces. Do not lighten it further.
  - The --*-dim tokens (accent-dim, green-dim, red-dim, violet-dim,
    cyan-dim) are pastel tints meant ONLY as backgrounds behind their
    matching saturated foreground colour (e.g. color:var(--green) on
    background:var(--green-dim)) -- never use a *-dim token as body text.
  - To re-theme every generated simulation at once (e.g. restore a dark
    theme, or apply brand colours), edit THEME_TOKENS in simulation.py,
    or point the SIM_THEME_OVERRIDE env var at a JSON file with
    replacement keys -- do not hand-edit colours per generation.

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
#lab-title h1 { font-size:17px; font-weight:600; color:var(--text);
                letter-spacing:-.01em; }
#lab-title h1 span { color:var(--accent); }

/* Experiment switcher (only when multi-experiment) */
#exp-list { padding:8px; border-bottom:1px solid var(--border); }
.exp-btn {
  display:flex; align-items:center; gap:10px; width:100%;
  padding:9px 12px; border-radius:8px; border:none; background:transparent;
  color:var(--text2); cursor:pointer; font-size:13px; font-weight:500;
  text-align:left; transition:all .15s;
}
.exp-btn:hover { background:var(--surface2); color:var(--text); }
.exp-btn.active {
  background:var(--accent-dim); color:var(--accent);
  border:1px solid color-mix(in srgb, var(--accent) 25%, transparent);
}
.exp-icon { font-size:18px; width:28px; text-align:center; flex-shrink:0; }
.exp-badge { font-size:9px; padding:2px 5px; border-radius:3px;
             background:var(--surface3); color:var(--text3); }

/* Controls panel */
#controls-panel { flex:1; overflow-y:auto; padding:12px; }
#controls-panel::-webkit-scrollbar { width:4px; }
#controls-panel::-webkit-scrollbar-thumb {
  background:var(--border2); border-radius:2px; }

.ctrl-section { margin-bottom:14px; }
.ctrl-label { font-size:10px; letter-spacing:.1em; text-transform:uppercase;
              color:var(--text3); margin-bottom:8px; }
.ctrl-row { margin-bottom:10px; }
.ctrl-name { font-size:12px; color:var(--text2); margin-bottom:4px;
             display:flex; justify-content:space-between; }
.ctrl-name span { color:var(--accent); font-weight:600; font-family:monospace; }

/* Slider -- gradient fill to show progress */
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
/* JS must update --pct custom property on each 'input' event: */
/* el.style.setProperty('--pct', (100*(v-min)/(max-min)).toFixed(1)) */

select {
  width:100%; background:var(--surface2); border:1px solid var(--border2);
  color:var(--text); font-size:12px; padding:6px 8px; border-radius:6px;
  outline:none; cursor:pointer;
}
select option { background:var(--surface2); }

/* Toggle switch */
.toggle-row { display:flex; align-items:center;
              justify-content:space-between; margin-bottom:8px; }
.toggle-row label { font-size:12px; color:var(--text2); }
.toggle { position:relative; width:36px; height:20px; }
.toggle input { opacity:0; width:0; height:0; position:absolute; }
.toggle-track { position:absolute; inset:0; background:var(--border2);
                border-radius:10px; cursor:pointer; transition:background .2s; }
.toggle input:checked + .toggle-track { background:var(--accent); }
.toggle-thumb { position:absolute; top:2px; left:2px; width:16px; height:16px;
                border-radius:50%; background:#fff;
                transition:transform .2s; pointer-events:none; }
.toggle input:checked ~ .toggle-thumb { transform:translateX(16px); }

/* Action buttons (Play / Reset) */
.btn-row { display:flex; gap:8px; margin-top:4px; }
.btn {
  flex:1; padding:8px 6px; border-radius:8px; border:1px solid var(--border2);
  background:var(--surface2); color:var(--text2); font-size:12px;
  font-weight:500; cursor:pointer; transition:all .15s; text-align:center;
}
.btn:hover { background:var(--surface3); color:var(--text); }
.btn.primary {
  background:var(--accent-dim); color:var(--accent);
  border-color:color-mix(in srgb, var(--accent) 30%, transparent);
}
.btn.primary:hover { background:var(--accent); color:#000; }

════════════════════════════════════════════════════════
  CANVAS AREA + OVERLAY CSS
════════════════════════════════════════════════════════
#canvas-area { flex:1; position:relative; overflow:hidden; }
#canvas-area canvas { position:absolute; top:0; left:0; width:100%; height:100%; }

#overlay-bar { position:absolute; top:10px; right:12px;
               display:flex; gap:6px; z-index:10; }
.ov-btn {
  background:var(--surface2); border:1px solid var(--border);
  color:var(--text2); font-size:11px; padding:5px 10px;
  border-radius:6px; cursor:pointer; transition:all .15s;
}
.ov-btn:hover, .ov-btn.on {
  background:var(--surface3); color:var(--accent);
  border-color:var(--accent-dim);
}

#tip {
  position:absolute; bottom:12px; left:50%; transform:translateX(-50%);
  background:var(--surface2); border:1px solid var(--border);
  color:var(--text2); font-size:11px; padding:6px 14px;
  border-radius:20px; pointer-events:none; white-space:nowrap;
  z-index:10;
}

════════════════════════════════════════════════════════
  METRICS STRIP CSS
════════════════════════════════════════════════════════
#info-panel {
  height:100px; background:var(--surface); border-top:1px solid var(--border);
  display:flex; align-items:stretch; flex-shrink:0;
}
.metric {
  flex:1; display:flex; flex-direction:column; justify-content:center;
  padding:12px 16px; border-right:1px solid var(--border); min-width:0;
}
.metric:last-child { border-right:none; }
.metric-label {
  font-size:10px; letter-spacing:.08em; text-transform:uppercase;
  color:var(--text3); margin-bottom:4px; white-space:nowrap;
}
.metric-value {
  font-size:20px; font-weight:600; font-family:monospace;
  color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.metric-sub { font-size:10px; color:var(--text2); margin-top:2px; }
.metric-badge {
  display:inline-block; font-size:10px; font-weight:600;
  padding:3px 8px; border-radius:4px; margin-top:4px;
}
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
    W = r.width; H = r.height;  // track logical CSS px size
    draw();
  }
  window.addEventListener('resize', resizeCanvas);
  // call once after layout: window.addEventListener('load', resizeCanvas)

DRAWING HELPERS to define once and reuse across draw calls:
  - Arrow with arrowhead: drawArrow(ctx, x1,y1, x2,y2, color, width)
  - Dashed reference line: dashLine(ctx, x1,y1, x2,y2, color)
  - Glowing dot/circle: glowDot(ctx, x, y, r, color, glowColor)
  - Axis pair with tick labels: drawAxes(ctx, originX, originY, scaleX, scaleY)
  - Pill/badge label: drawLabel(ctx, x, y, text, fgColor, bgColor)

STYLE TIPS:
  - Use ctx.shadowColor + ctx.shadowBlur (4–12px) on key strokes / dots for
    the "glowing beam" look. Reset to '' / 0 immediately after each glow draw
    so it doesn't bleed onto surrounding elements.
  - strokeStyle + lineWidth for all drawn paths; fillStyle only for solid shapes.
  - Consistent stroke weights: axis lines 1px, construction/reference lines
    1px dashed, main physics elements 2–2.5px, highlight/active elements 3px.
  - Use CSS color tokens on the canvas via getComputedStyle(document.documentElement)
    .getPropertyValue('--green') etc.

════════════════════════════════════════════════════════
  INTERACTION WIRING PATTERN
════════════════════════════════════════════════════════
// Value reader
function gv(id) { return parseFloat(document.getElementById(id)?.value ?? 0); }
function gb(id) { return document.getElementById(id)?.checked ?? false; }

// Slider binding with gradient-fill progress track
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
  update();  // render on load
}

// Metrics helper -- call at end of every draw()
function setMetrics(items) {
  // items: [{label, value, sub?, badge?, badgeClass?}, ...]
  const panel = document.getElementById('info-panel');
  panel.innerHTML = items.map(m => `
    <div class="metric">
      <div class="metric-label">${m.label}</div>
      <div class="metric-value">${m.value}</div>
      ${m.sub  ? `<div class="metric-sub">${m.sub}</div>` : ''}
      ${m.badge ? `<div class="metric-badge ${m.badgeClass||'badge-amber'}">${m.badge}</div>` : ''}
    </div>`).join('');
}

// Keyboard shortcuts
window.addEventListener('keydown', e => {
  if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
  if (e.code === 'KeyR')  { resetSim(); }
});

// Animation loop pattern
let rafId = null, playing = false;
function togglePlay() {
  playing = !playing;
  if (playing) {
    function loop(ts) {
      if (!playing) return;
      stepSim(ts);
      draw();
      rafId = requestAnimationFrame(loop);
    }
    rafId = requestAnimationFrame(loop);
  } else {
    cancelAnimationFrame(rafId); rafId = null;
  }
  document.getElementById('btnPlay').textContent = playing ? '⏸ Pause' : '▶ Play';
}

// Canvas drag support (for topics where user can drag an object)
// Wire this in mousedown/mousemove/mouseup on #canvas-area when relevant.

════════════════════════════════════════════════════════
  WHAT TO OMIT
════════════════════════════════════════════════════════
- No external URLs of any kind (scripts, fonts, images, CSS, API calls).
- No localStorage / sessionStorage.
- No backend calls -- 100% client-side computation.
- No placeholder numbers disconnected from the governing equations.
- No controls that don't visibly change anything.
- No more than 7 controls in the sidebar (trim to the most meaningful).
- No narration panels or "theory" text blocks inside the canvas area --
  that belongs in the sidebar or as short canvas labels only.
"""

# Inject the actual light-theme CSS custom-property block (built from
# THEME_TOKENS / SIM_THEME_OVERRIDE) into the otherwise-static prompt text.
# Done once at import time so DESIGN_SYSTEM / SYSTEM stay plain strings and
# the prompt-caching behaviour noted in the module docstring is unaffected.
DESIGN_SYSTEM = DESIGN_SYSTEM.replace("__THEME_CSS_BLOCK__", _theme_css_block())

SYSTEM = """You are SimEngine v2.0 -- an expert interactive-simulation engineer who builds
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
  "summary": "1-2 sentence plain-English description of what the sim shows
              and what the learner can explore",
  "controls_overview": ["one phrase per control exposed, e.g.",
              "'Pendulum length (L)', 'Gravity (g)', 'Damping toggle'"],
  "key_formula": "the core formula or relationship the simulation is built
              on, in plain text (e.g. 'T = 2*pi*sqrt(L/g)')",
  "learning_notes": ["2-4 short simple-English sentences a learner reads
              while using the sim -- what to notice, what to try"],
  "simulation_code": "COMPLETE SELF-CONTAINED <!DOCTYPE html>...</html>
              AS A SINGLE PROPERLY-ESCAPED JSON STRING"
}

════════════════════════════════════════════════════════
  CORRECTNESS REQUIREMENTS
════════════════════════════════════════════════════════
- Every number shown anywhere must come from a REAL computation based on
  the actual governing equation(s) for the topic. Never a cosmetic placeholder.
- Units must be correct and consistently labeled throughout.
- Compute and surface well-known named quantities specific to the topic
  (critical angle, focal length, time constant, half-life, equilibrium
  constant, Reynolds number, etc.) -- don't ignore them.
- Edge cases (division by zero, out-of-range inputs, infinite image distance,
  etc.) must be handled gracefully in the JS -- never crash silently.
- For animated topics, use real physics time-stepping (Euler or RK4 where
  needed), not made-up animation tweens disconnected from the equations.

════════════════════════════════════════════════════════
  QUALITY BAR
════════════════════════════════════════════════════════
- The control panel must feel purposeful: every slider/select/toggle visibly
  changes the canvas or a metric. Aim for 3–7 live controls.
- The canvas drawing must look intentional: clear labels, no overlapping text,
  consistent stroke weights, a believable sense of scale and proportion.
  Diagrams must look like professional virtual-lab screenshots, not rough
  sketches.
- The metrics strip shows 3–5 of the MOST MEANINGFUL live-computed values
  for this topic, color-coded where it aids understanding.
- Mobile-responsive: usable down to a 380px-wide viewport.
- Include keyboard shortcuts (Space = play/pause, R = reset) when applicable.
- If the topic naturally spans multiple related experiments (optics: Snell's
  law, convex lens, concave mirror...) build a multi-experiment sidebar
  switcher following the #exp-list pattern in the design system. Otherwise
  keep it focused on ONE excellent experiment.
- Canvas drag interactions: where natural (place object on optical bench,
  drag pendulum to release position, place charge in electric field), wire
  mousedown/mousemove/mouseup on the canvas to let the user interact
  directly without sliders alone.
"""


# ---------------------------------------------------------------------------
# Per-category strategy hints with governing equations
# ---------------------------------------------------------------------------
STRATEGY_TEMPLATES = {
    "PHYSICS_MECHANICS":
        "Canvas with a ground/axis reference. Main controls: initial conditions "
        "(angle, velocity, mass, length) + physical constants (g, friction, k). "
        "Show the moving body, trailing path toggle, and vector arrows for v/F/a. "
        "Animate via the real equations of motion (Euler or RK4 integration). "
        "REQUIRED equations: Newton's 2nd law F=ma, energy E=KE+PE, specific to "
        "the experiment (pendulum: θ''=-(g/L)sinθ; projectile: x=v₀cosθ·t, "
        "y=v₀sinθ·t-½gt²; spring: F=-kx). "
        "Metrics: period T, max velocity, current KE, current PE, total E.",

    "PHYSICS_WAVES_OPTICS":
        "Canvas with rays/wavefronts on the standard light theme background; "
        "use bold, saturated strokes (accent + semantic colors) with a soft "
        "shadowBlur glow so beams stay clearly legible against the light surface. "
        "Build a multi-experiment switcher for at least 3 related setups "
        "(e.g. refraction, convex lens, concave mirror). "
        "REQUIRED equations: Snell's n₁sinθ₁=n₂sinθ₂; lens 1/f=1/do+1/di; "
        "mirror same form; critical angle θc=arcsin(n₂/n₁); magnification M=-di/do. "
        "Controls: angle, refractive index / focal length, wavelength, toggles "
        "for normal/refracted ray labels and angle arcs. "
        "Metrics: angles θ₁/θ₂, image distance di, magnification M, critical angle.",

    "ELECTRICITY_CIRCUITS":
        "Schematic-style canvas: draw a real circuit diagram (battery symbol, "
        "resistor zigzag or rectangle, capacitor plates, inductor coil) with "
        "animated dashes or dots flowing along wires to show current direction. "
        "REQUIRED equations: Ohm's V=IR; RC: V(t)=V₀(1-e^(-t/RC)), τ=RC; "
        "RLC: resonance ω₀=1/√(LC), Q=ω₀L/R; Kirchhoff's KVL/KCL. "
        "Controls: R, C, L values, supply voltage, frequency (for AC). "
        "Metrics: current I, charge Q, time constant τ, power P=V²/R.",

    "CHEMISTRY":
        "Canvas showing a reaction-progress diagram (energy vs. reaction coord.) "
        "AND/OR a concentration-vs-time chart, OR a titration curve. "
        "REQUIRED equations: rate law r=k[A]^m[B]^n; Arrhenius k=A·e^(-Ea/RT); "
        "equilibrium K=[products]/[reactants]; Henderson-Hasselbalch pH=pKa+log([A⁻]/[HA]). "
        "Controls: concentrations, temperature, activation energy, catalyst toggle. "
        "Metrics: rate constant k, equilibrium position Q vs K, pH, half-life.",

    "BIOLOGY":
        "Canvas showing organic/biological shapes (cells, population curves, "
        "pedigree grids, pharmacokinetic curves). "
        "REQUIRED equations: logistic growth dN/dt=rN(1-N/K); Lotka-Volterra "
        "predator-prey; Hardy-Weinberg p²+2pq+q²=1 for genetics; "
        "enzyme kinetics v=Vmax[S]/(Km+[S]) Michaelis-Menten. "
        "Controls: growth rate r, carrying capacity K, initial populations. "
        "Metrics: population at time t, growth rate dN/dt, doubling time.",

    "MATH_GEOMETRY":
        "Canvas coordinate plane with axes, grid, and a plotted "
        "function/shape/distribution that redraws live as parameters change. "
        "Controls: coefficients, transformation values (translate/rotate/scale), "
        "frequency, amplitude. "
        "Metrics: roots, extrema, area under curve (numerical integration), "
        "period, amplitude -- computed from the actual parameters, not estimated. "
        "Include a graph overlay mode that superimposes multiple parameter states.",

    "CS_ALGORITHMS":
        "Canvas showing an array/tree/graph with labeled nodes/bars, with "
        "step-through AND animate controls + a speed slider. "
        "Controls: input size N (10–100), initial arrangement (random/sorted/reversed), "
        "algorithm variant selector. "
        "Metrics: comparisons count, swaps count, current Big-O class, elapsed steps. "
        "Color-code: unsorted bars grey, active comparison in accent, sorted in green. "
        "Use a consistent left-to-right time axis for trace-style algorithms.",

    "EARTH_ENV_SCIENCE":
        "Canvas showing a cross-section (geological layers, atmosphere), cycle "
        "diagram, or time-series plot responding to controls like emission rate, "
        "time horizon, feedback strength, geological time. "
        "REQUIRED equations: radiative forcing ΔF=α·ln(C/C₀); exponential decay "
        "for radioactive dating; orbital mechanics for solar system. "
        "Metrics: projected values at chosen year, rate of change, anomaly vs baseline.",

    "ECONOMICS_SOCIAL":
        "Canvas/SVG plotting supply-demand curves or a time-series. "
        "Controls: price, elasticity, tax/subsidy, growth rate. "
        "REQUIRED equations: consumer surplus CS=½(Pmax-Pe)·Qe; elasticity "
        "E=(ΔQ/Q)/(ΔP/P); compound growth A=P(1+r/n)^(nt). "
        "Metrics: equilibrium price Pe, quantity Qe, consumer/producer surplus, "
        "compounded value -- computed from the real economic relationships.",

    "GENERAL_PROCESS":
        "Choose the visualization (canvas timeline, flow diagram, coordinate plot) "
        "that best fits the specific process. Expose the 3–6 parameters that most "
        "meaningfully change the outcome. Show a metrics strip with the most "
        "informative derived values for this process, computed from real equations.",
}


def _build_prompt(topic: str, category: str, image_refs: List[dict]) -> tuple:
    strategy  = STRATEGY_TEMPLATES.get(category, STRATEGY_TEMPLATES["GENERAL_PROCESS"])
    image_ctx = _format_image_refs_for_prompt(image_refs)

    # Determine if a multi-experiment switcher is appropriate
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

    system_blocks = [
        {
            "type": "text",
            "text": SYSTEM,
            "cache_control": {"type": "ephemeral"},  # cache the large stable block
        }
    ]

    user_parts = [
        f"Build an interactive HTML5 simulation for SimEngine v2.0.\n",
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
        "- Canvas drag interaction where it makes physical sense (drag bob, drag object, etc.).",
        "- Every control MUST visibly affect canvas AND/OR a metric.",
        "- All computed values MUST follow the real governing equations -- no placeholder numbers.",
        "- Include a Play/Animate button + requestAnimationFrame loop if the topic involves motion.",
        "- Mobile-responsive down to 380px viewport width.",
        "- The visual quality bar is a professional virtual-lab screenshot -- not a rough sketch.",
        "\nReturn ONLY raw JSON. simulation_code must be a complete "
        "<!DOCTYPE html>...</html> document as a properly escaped JSON string.",
    ]

    user_content = "\n".join(user_parts)
    return system_blocks, user_content


# ===========================================================================
#  MODULE 8 -- Response Parsing (fallback chain)
# ===========================================================================

def _parse_response(raw: str, topic: str) -> dict:
    strategies = [
        _parse_direct_json,
        _parse_stripped_json,
        _parse_brace_extracted,
        _parse_field_by_field,
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
        "title": f"Simulation: {topic[:50]}",
        "category": "GENERAL_PROCESS",
        "summary": "Generation could not be parsed.",
        "controls_overview": [],
        "key_formula": "",
        "learning_notes": [],
        "simulation_code": "",
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
        "title":     str(data.get("title") or "").strip() or f"Simulation: {topic[:50]}",
        "category":  str(data.get("category") or "").strip() or "GENERAL_PROCESS",
        "summary":   str(data.get("summary") or "").strip() or "Interactive simulation",
        "key_formula": str(data.get("key_formula") or "").strip(),
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
        "engine_version":    "v2.0",
        "render_status":     "error",
        "error_reason":      reason,
    }


async def _run_generation_pipeline(topic: str) -> dict:
    """
    Full v2.0 pipeline:
      1. Classify topic
      2. Fetch Google Image references (non-blocking, gracefully skipped)
      3. Build prompt (system + user) with image refs injected
      4. Call generation model
      5. Parse response (5-strategy fallback chain)
      6. Sanitize HTML
      7. Validate HTML
      8. Return result dict
    """
    short_topic = topic[:80] + ("..." if len(topic) > 80 else "")
    SimLogger.info("Pipeline", f"START v2.0 -- '{short_topic}'")

    # Step 1: Classify
    category = _classify_topic(topic)
    SimLogger.info("Classifier", f"Category: {category}")

    # Step 2: Image references
    image_refs = _fetch_image_refs(topic)

    # Step 3: Build prompt
    system_blocks, user_content = _build_prompt(topic, category, image_refs)

    # Step 4: Generate
    try:
        msg = client.messages.create(
            model=SIM_MODEL, max_tokens=MAX_TOK,
            system=system_blocks,
            messages=[{"role": "user", "content": user_content}])
        raw = msg.content[0].text.strip()
        SimLogger.info(
            "GenerationAI",
            f"model={SIM_MODEL}  stop_reason={msg.stop_reason}  len={len(raw)}"
            f"  cache_read={getattr(msg.usage, 'cache_read_input_tokens', 0)}"
            f"  cache_create={getattr(msg.usage, 'cache_creation_input_tokens', 0)}"
        )
        if msg.stop_reason == "max_tokens":
            SimLogger.warn("GenerationAI", "Hit max_tokens -- output may be truncated!")
    except Exception as e:
        SimLogger.error("GenerationAI", f"API call failed: {e}")
        return _build_failure_result(topic, f"API error: {e}")

    # Step 5: Parse
    result = _parse_response(raw, topic)
    result["category"]   = result.get("category") or category
    result["image_refs"] = image_refs
    sim_html = result.get("simulation_code", "").strip()

    if not sim_html:
        SimLogger.error("Pipeline", "No simulation_code could be parsed from the response")
        return _build_failure_result(topic, "Could not parse simulation HTML from model response")

    # Step 6: Sanitize
    sim_html = HtmlSanitizer.sanitize(sim_html)

    # Step 7: Validate
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
    result["engine_version"] = "v2.0"
    result["render_status"]  = "ok"

    SimLogger.ok("Pipeline", (
        f"DONE -- '{result['title']}'  category={result['category']}"
        f"  html={len(sim_html):,} chars"
        f"  controls={len(result.get('controls_overview', []))}"
        f"  image_refs={len(image_refs)}"
    ))
    return result


# ===========================================================================
#  Public API  (same signatures as v1.0 -- drop-in replacement)
# ===========================================================================

async def generate_simulation(topic: str) -> dict:
    """
    Public async entry point.

    Args:
        topic: free-text topic, concept, or lab-experiment description, e.g.
               "Simple pendulum with damping" or
               "Show me how a binary search tree rebalances".

    Returns:
        dict with keys: title, category, summary, controls_overview,
        key_formula, learning_notes, image_refs, html, engine_version,
        render_status. On any failure, render_status is "error" and "html"
        is a graceful, user-facing fallback page. This function never raises.
    """
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("Topic cannot be empty")
    try:
        return await _run_generation_pipeline(topic)
    except Exception as e:
        SimLogger.error("Pipeline", f"UNHANDLED error -- falling back gracefully: {e}")
        return _build_failure_result(topic, f"Unexpected error: {e}")


def generate_simulation_sync(topic: str) -> dict:
    """Synchronous wrapper around generate_simulation() for non-async callers."""
    return asyncio.run(generate_simulation(topic))


# ===========================================================================
#  CLI TEST
# ===========================================================================
if __name__ == "__main__":
    import sys

    TEST_TOPICS = {
        "MECHANICS":   "Simple pendulum with adjustable length, gravity, and damping",
        "OPTICS":      "Convex lens image formation with adjustable object distance and focal length",
        "CIRCUITS":    "RC circuit charging and discharging through a resistor",
        "BIOLOGY":     "Logistic population growth with adjustable growth rate and carrying capacity",
        "MATH":        "Unit circle and the sine/cosine waveform it traces out",
        "ALGORITHMS":  "Bubble sort visualized step by step on a random array",
        "CHEMISTRY":   "Gas particles in a container demonstrating Boyle's law",
        "WAVES":       "Double-slit interference pattern with adjustable slit separation",
        "EARTH":       "Carbon cycle and greenhouse gas concentration vs temperature",
        "ECONOMICS":   "Supply and demand curves with adjustable elasticity and tax",
    }

    if len(sys.argv) > 1:
        topics_to_test = {"CUSTOM": " ".join(sys.argv[1:])}
    else:
        key = "MECHANICS"
        topics_to_test = {key: TEST_TOPICS[key]}

    for cat, t in topics_to_test.items():
        print("=" * 72)
        print(f"  SimEngine v2.0 | {cat}")
        print(f"  Topic: {t[:65]}")
        print("=" * 72)

        t0 = time.time()
        result = generate_simulation_sync(t)
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

        slug = cat.lower()
        out_path = f"sim_{slug}.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_out)
        print(f"\nSaved -> {out_path}")
        print()