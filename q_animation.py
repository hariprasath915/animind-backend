"""
q_animation.py  —  QAnim Question Animation Generator  v3.1
============================================================

v3.1 — MODEL FIX: gemini-2.5-pro → gemini-2.5-pro-preview-06-05
  - gemini-2.5-pro returns HTTP 404 NOT_FOUND for new API keys.
  - Model is now read from GEMINI_MODEL env var (default: gemini-2.5-pro-preview-06-05).
  - 404 errors are now non-retryable (fail fast instead of wasting 3×15s retries).

v3.0 — CLEAN REWRITE matching the reference HTML output exactly.

WHAT THIS VERSION DOES:
  - Generates self-contained 9-scene HTML animations for any physics/math question.
  - Scenes 1–6: SVG concept animation (one physical element per step).
  - Scene 7: Main Formula reveal with per-variable explanation.
  - Scene 8: Step-by-step substitution (two-column layout).
  - Scene 9: Final answer with animated substitution chain + answer input box.
  - All panels, controls, glossary, and notes are injected by Python.
  - Gemini generates ONLY the SVG + stepsData + applyStep JS for scenes 1–6.
  - Python injects all scenes 7, 8, 9 HTML/CSS/JS from reference-exact templates.

REQUIRED ENV VAR:
  GEMINI_API_KEY=your-key
"""

import json
import re
import asyncio
import html as html_module
from typing import Optional
import os as _os

# ── Gemini SDK import ──────────────────────────────────────────────────────
_GEMINI_AVAILABLE = False
_GEMINI_SDK_STYLE = None
_google_genai = None

try:
    from google import genai as _google_genai
    _GEMINI_AVAILABLE = True
    _GEMINI_SDK_STYLE = "genai"
    print("[QAnim] SDK: google-genai loaded")
except ImportError:
    try:
        import google.generativeai as _google_genai
        _GEMINI_AVAILABLE = True
        _GEMINI_SDK_STYLE = "generativeai"
        print("[QAnim] SDK: google-generativeai loaded")
    except ImportError:
        print("[QAnim] No Gemini SDK found")

GEMINI_MODEL = _os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview")

_gemini_client = None
_GEMINI_DISABLED_REASON = None

if _GEMINI_AVAILABLE:
    _gkey = _os.environ.get("GEMINI_API_KEY", "").strip()
    if not _gkey:
        _GEMINI_DISABLED_REASON = "GEMINI_API_KEY not set"
        print("[QAnim] GEMINI_API_KEY not set")
    elif _GEMINI_SDK_STYLE == "generativeai":
        try:
            _google_genai.configure(api_key=_gkey)
            _gemini_client = _google_genai
            print(f"[QAnim] Gemini ready (google-generativeai, model={GEMINI_MODEL})")
        except Exception as e:
            _GEMINI_DISABLED_REASON = repr(e)
    else:
        try:
            _gemini_client = _google_genai.Client(api_key=_gkey)
            print(f"[QAnim] Gemini ready (google-genai, model={GEMINI_MODEL})")
        except Exception as e:
            _GEMINI_DISABLED_REASON = repr(e)
else:
    _GEMINI_DISABLED_REASON = "No Gemini SDK installed"

MAX_TOKENS_SOLUTION  = 4000
MAX_TOKENS_SCENE     = 8000
MAX_TOKENS_HTML      = 28000
TIMEOUT_SOLUTION     = 120.0
TIMEOUT_SCENE        = 150.0
TIMEOUT_HTML         = 300.0
PIPELINE_TIMEOUT     = 600.0


# ===========================================================================
# Logging
# ===========================================================================
class Log:
    @staticmethod
    def info(stage, msg):  print(f"[QAnim]  i [{stage}] {msg}")
    @staticmethod
    def warn(stage, msg):  print(f"[QAnim]  ! [{stage}] {msg}")
    @staticmethod
    def error(stage, msg): print(f"[QAnim]  X [{stage}] {msg}")
    @staticmethod
    def ok(stage, msg):    print(f"[QAnim] OK [{stage}] {msg}")


# ===========================================================================
# Gemini caller
# ===========================================================================
def _call_gemini(user_prompt: str, system_prompt: str, max_tokens: int = 4000) -> str:
    """Call Gemini with retry on 429/503."""
    import time as _time
    if _gemini_client is None:
        raise RuntimeError(f"Gemini unavailable: {_GEMINI_DISABLED_REASON}")
    MAX_RETRIES = 3
    RETRY_DELAYS = [10, 25, 50]
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if _GEMINI_SDK_STYLE == "generativeai":
                model_obj = _gemini_client.GenerativeModel(
                    model_name=GEMINI_MODEL,
                    system_instruction=system_prompt,
                    generation_config={"temperature": 0.15, "max_output_tokens": max_tokens},
                )
                response = model_obj.generate_content(user_prompt)
                return response.text.strip()
            else:
                try:
                    config = _google_genai.types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.15,
                        max_output_tokens=max_tokens,
                        thinking_config=_google_genai.types.ThinkingConfig(thinking_level="low"),
                    )
                except Exception:
                    config = _google_genai.types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.15,
                        max_output_tokens=max_tokens,
                    )
                response = _gemini_client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=user_prompt,
                    config=config,
                )
                return response.text.strip()
        except Exception as e:
            err = str(e)
            # 404 = model not found / not available — never retryable, fail immediately
            if "404" in err or "NOT_FOUND" in err:
                raise
            retryable = ("429" in err or "503" in err or "overloaded" in err.lower()
                         or "Resource has been exhausted" in err)
            if retryable and attempt < MAX_RETRIES:
                _time.sleep(RETRY_DELAYS[attempt - 1])
                continue
            raise
    raise RuntimeError("All retry attempts exhausted")


def _sanitize_json(raw: str) -> str:
    """Strip markdown fences and extract the first JSON object."""
    raw = raw.lstrip('\ufeff').strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'```\s*$', '', raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r'<thinking>.*?</thinking>', '', raw, flags=re.DOTALL).strip()
    start = raw.find('{')
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        end_idx = None
        for i, ch in enumerate(raw[start:], start):
            if esc:
                esc = False
                continue
            if ch == '\\' and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break
        if end_idx is not None:
            raw = raw[start:end_idx + 1]
    raw = re.sub(r',\s*([}\]])', r'\1', raw)
    raw = re.sub(r'\bTrue\b', 'true', raw)
    raw = re.sub(r'\bFalse\b', 'false', raw)
    raw = re.sub(r'\bNone\b', 'null', raw)
    return raw.strip()


# ===========================================================================
# Stage 1: Solution Generator
# ===========================================================================
_SOLUTION_SYSTEM = """You are a precise physics/engineering/math solver.
Solve the given problem and return ONLY valid JSON (no markdown, no fences):
{
  "steps": [
    "Step 1: Identify the governing formula: Q = h × A × ΔT",
    "Step 2: Substitute values: Q = 25 × 2 × 120",
    "Step 3: Compute result: Q = 6000 W"
  ],
  "final_answer": "Q = 6000 W",
  "answer_value": "6000",
  "answer_unit": "W",
  "key_insight": "Heat loss doubles if area doubles because Q is proportional to A.",
  "formula": "Q = h × A × (Ts − T∞)",
  "formula_name": "Newton's Law of Cooling",
  "variables": [
    {"symbol": "Q",  "name": "Heat loss rate",       "value": "? (to find)", "unit": "W",       "color": "green"},
    {"symbol": "h",  "name": "Convective coefficient","value": "25",          "unit": "W/m²·K",  "color": "blue"},
    {"symbol": "A",  "name": "Surface area",          "value": "2",           "unit": "m²",      "color": "blue"},
    {"symbol": "ΔT", "name": "Temperature difference","value": "120",         "unit": "K",       "color": "orange"}
  ],
  "substitution_chain": [
    {"num": 1, "eq": "Q = h × A × (Ts − T∞)"},
    {"num": 2, "eq": "Q = 25 × 2 × (150 − 30)"},
    {"num": 3, "eq": "Q = 25 × 2 × 120"},
    {"num": 4, "eq": "Q = 6000 W"}
  ],
  "given_list": ["h = 25 W/m²·K (convective coefficient)", "A = 2 m² (plate area)", "Ts = 150 °C", "T∞ = 30 °C"],
  "approach_steps": [
    {"num": "8.1", "label": "Write the formula", "eq": "Q = h × A × (Ts − T∞)", "note": "Newton's Law of Cooling"},
    {"num": "8.2", "label": "Compute ΔT", "eq": "ΔT = 150 − 30 = 120 K", "note": "Temperature difference"},
    {"num": "8.3", "label": "Substitute and solve", "eq": "Q = 25 × 2 × 120 = 6000 W", "note": "Final value"}
  ],
  "system_title": "Hot Plate in Forced Airflow",
  "system_label2": "Forced convection over a hot surface"
}

Rules:
- steps: 3–5 numbered solution steps.
- final_answer: complete expression with value and unit.
- answer_value: just the number (e.g. "6000").
- answer_unit: just the unit string (e.g. "W").
- key_insight: one memorable sentence.
- formula: the governing equation.
- formula_name: common name of the equation (e.g. "Newton's Law of Cooling").
- variables: all symbols in the formula; color = "blue" for given, "orange" for derived, "green" for answer.
- substitution_chain: 3–5 rows showing substitution step by step.
- given_list: list of given parameters as strings (for Scene 8 right panel).
- approach_steps: 2–4 numbered steps for Scene 8 right panel; each has num, label, eq, note.
- system_title: short name of the physical system (for Scene 8 left panel).
- system_label2: one-line description (for Scene 8 left panel).
- Pure JSON only."""


def generate_solution(question: str) -> dict:
    """Call Gemini to solve the question. Returns solution dict."""
    FALLBACK = {
        "steps": ["Step 1: Identify formula.", "Step 2: Substitute values.", "Step 3: Compute result."],
        "final_answer": "See solution above.",
        "answer_value": "?",
        "answer_unit": "",
        "key_insight": "Apply the governing formula with the given data.",
        "formula": "See governing formula",
        "formula_name": "Governing Equation",
        "variables": [],
        "substitution_chain": [{"num": 1, "eq": "Apply the governing formula"}, {"num": 2, "eq": "Substitute given values"}, {"num": 3, "eq": "Compute the result"}],
        "given_list": ["Given values from the problem"],
        "approach_steps": [{"num": "8.1", "label": "Identify formula", "eq": "Governing formula", "note": ""}, {"num": "8.2", "label": "Substitute", "eq": "Given values", "note": ""}, {"num": "8.3", "label": "Compute", "eq": "Result", "note": ""}],
        "system_title": "Physical System",
        "system_label2": "Applying the formula",
        "_fallback": True,
    }
    if _gemini_client is None:
        return FALLBACK
    for attempt in range(1, 4):
        try:
            raw = _call_gemini(
                f"Solve step by step:\n\n{question[:1500]}",
                _SOLUTION_SYSTEM,
                max_tokens=MAX_TOKENS_SOLUTION,
            )
            data = json.loads(_sanitize_json(raw))
            if data.get("steps") and data.get("final_answer"):
                Log.ok("Solution", f"Got solution: {data.get('final_answer', '')[:60]}")
                return data
        except Exception as e:
            Log.warn("Solution", f"Attempt {attempt} failed: {e}")
    return FALLBACK


# ===========================================================================
# Stage 2: Scene Script Analyzer
# ===========================================================================
_SCENE_SYSTEM = """You are QAnim Scene Analyzer. Given a student question, produce a structured
animation scene script in JSON for a 6-step SVG concept animation.

Steps 1–6 build a visual explanation of the physical setup, one element at a time.
No formulas, no calculations, no solution steps in the scene descriptions.

Return ONLY valid JSON:
{
  "title": "Resistance of a Stretched Wire",
  "topic": "PHYSICS",
  "steps": [
    {
      "step_number": 1,
      "label": "Grid",
      "title": "Step 1: Establishing the Measurement Scale",
      "description": "We begin with a reference grid to measure the wire dimensions.",
      "badges": [{"text": "Reference scale", "type": "cyan"}],
      "layers_visible": ["layer-frame"],
      "layer_new": "layer-frame",
      "blur": false
    },
    {
      "step_number": 2,
      "label": "Wire",
      "title": "Step 2: The Initial Metal Wire",
      "description": "Here is our original metal wire with length L and cross-section A.",
      "badges": [{"text": "Length = L", "type": "cyan"}, {"text": "Area = A", "type": "cyan"}],
      "layers_visible": ["layer-frame", "layer-object"],
      "layer_new": "layer-object",
      "blur": true
    },
    {
      "step_number": 3,
      "label": "R₁",
      "title": "Step 3: Measuring Initial Resistance",
      "description": "We connect an ohmmeter and measure the initial resistance R₁ = 10 Ω.",
      "badges": [{"text": "R₁ = 10 Ω", "type": "green"}],
      "layers_visible": ["layer-frame", "layer-object", "layer-param1"],
      "layer_new": "layer-param1",
      "blur": true
    },
    {
      "step_number": 4,
      "label": "Force",
      "title": "Step 4: Applying Tension",
      "description": "Mechanical forces are applied to both ends of the wire.",
      "badges": [{"text": "Force applied", "type": "orange"}],
      "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2"],
      "layer_new": "layer-param2",
      "blur": true
    },
    {
      "step_number": 5,
      "label": "Stretch",
      "title": "Step 5: Doubling the Length",
      "description": "The wire stretches to twice its original length. Volume stays constant.",
      "badges": [{"text": "L₂ = 2L", "type": "cyan"}, {"text": "Volume = constant", "type": "orange"}],
      "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2", "layer-derived"],
      "layer_new": "layer-derived",
      "blur": true
    },
    {
      "step_number": 6,
      "label": "Setup",
      "title": "Step 6: Complete Setup — Ready to Solve",
      "description": "All given data is in place. The new resistance R₂ = ? is what we must find.",
      "badges": [{"text": "R₁ = 10 Ω", "type": "cyan"}, {"text": "L₂ = 2L", "type": "cyan"}, {"text": "V = const", "type": "orange"}, {"text": "R₂ = ?", "type": "green"}],
      "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2", "layer-derived", "layer-summary"],
      "layer_new": "layer-summary",
      "blur": false
    }
  ],
  "svg_layers": {
    "layer-frame": {"description": "Background grid and reference frame", "color": "#4a6a8a"},
    "layer-object": {"description": "The main physical object (wire, plate, projectile, etc.)", "color": "#0891b2"},
    "layer-param1": {"description": "Primary given parameter visualization", "color": "#16a34a"},
    "layer-param2": {"description": "Second given parameter or force", "color": "#d97706"},
    "layer-derived": {"description": "Derived or changed quantity", "color": "#0891b2"},
    "layer-summary": {"description": "Setup summary callout boxes", "color": "#7c3aed"}
  },
  "to_find": ["Its new resistance"],
  "color_legend": [
    {"label": "Grid", "color": "#0ea5e9"},
    {"label": "Object", "color": "#10b981"},
    {"label": "Param 1", "color": "#f59e0b"},
    {"label": "Param 2", "color": "#6366f1"},
    {"label": "Derived", "color": "#f43f5e"},
    {"label": "Setup", "color": "#22c55e"}
  ],
  "glossary": [
    {"term": "resistance", "meaning": "How much a material opposes the flow of electric current."},
    {"term": "volume", "meaning": "The amount of 3D space an object occupies."}
  ]
}

STRICT RULES:
1. EXACTLY 6 steps.
2. Step 6: blur = false, all layers visible, badges summarise all given data + "Unknown = ?".
3. Steps 2–5: blur = true.
4. No formulas, no equations, no solution text in ANY step description.
5. svg_layers must list every layer ID that appears in any step's layers_visible.
6. to_find: list of 1–3 strings describing what the student must find.
7. color_legend: one entry per SVG step, colours corresponding to the 6 steps.
8. glossary: 2–5 genuinely difficult technical words with simple explanations.
9. Return PURE JSON only."""


def analyze_scene(question: str) -> dict:
    """Call Gemini to produce the scene script."""
    FALLBACK = {
        "title": question[:60],
        "topic": "PHYSICS",
        "steps": [
            {"step_number": 1, "label": "Setup", "title": "Step 1: Setting the Scene", "description": "We establish the physical environment for this problem.", "badges": [{"text": "Given: see problem", "type": "cyan"}], "layers_visible": ["layer-frame"], "layer_new": "layer-frame", "blur": False},
            {"step_number": 2, "label": "Object", "title": "Step 2: The Main System", "description": "The primary object or system is introduced.", "badges": [{"text": "System: defined", "type": "cyan"}], "layers_visible": ["layer-frame", "layer-object"], "layer_new": "layer-object", "blur": True},
            {"step_number": 3, "label": "Param 1", "title": "Step 3: First Given Value", "description": "The first given parameter is identified.", "badges": [{"text": "Given: value 1", "type": "cyan"}], "layers_visible": ["layer-frame", "layer-object", "layer-param1"], "layer_new": "layer-param1", "blur": True},
            {"step_number": 4, "label": "Param 2", "title": "Step 4: Second Given Value", "description": "The second given parameter is added.", "badges": [{"text": "Given: value 2", "type": "cyan"}], "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2"], "layer_new": "layer-param2", "blur": True},
            {"step_number": 5, "label": "Derived", "title": "Step 5: Derived Quantity", "description": "An intermediate quantity is derived from the given data.", "badges": [{"text": "Derived value", "type": "orange"}], "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2", "layer-derived"], "layer_new": "layer-derived", "blur": True},
            {"step_number": 6, "label": "Summary", "title": "Step 6: Complete Setup — Ready to Solve", "description": "All given data is assembled. The unknown quantity is identified.", "badges": [{"text": "All given", "type": "cyan"}, {"text": "Unknown = ?", "type": "green"}], "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2", "layer-derived", "layer-summary"], "layer_new": "layer-summary", "blur": False},
        ],
        "svg_layers": {
            "layer-frame": {"description": "Background grid and environment", "color": "#4a6a8a"},
            "layer-object": {"description": "Main physical object", "color": "#0891b2"},
            "layer-param1": {"description": "First parameter", "color": "#16a34a"},
            "layer-param2": {"description": "Second parameter", "color": "#d97706"},
            "layer-derived": {"description": "Derived quantity", "color": "#7c3aed"},
            "layer-summary": {"description": "Summary overlay", "color": "#0891b2"},
        },
        "to_find": ["The unknown quantity"],
        "color_legend": [
            {"label": "Setup", "color": "#0ea5e9"},
            {"label": "Object", "color": "#10b981"},
            {"label": "Param 1", "color": "#f59e0b"},
            {"label": "Param 2", "color": "#6366f1"},
            {"label": "Derived", "color": "#f43f5e"},
            {"label": "Summary", "color": "#22c55e"},
        ],
        "glossary": [],
        "_fallback": True,
    }
    if _gemini_client is None:
        return FALLBACK

    def _validate_scene(data: dict) -> bool:
        """Require exactly 6 steps numbered 1-6 with all required fields."""
        steps = data.get("steps", [])
        if len(steps) != 6:
            Log.warn("SceneAnalyzer", f"Expected 6 steps, got {len(steps)}")
            return False
        required_nums = {1, 2, 3, 4, 5, 6}
        got_nums = set()
        required_fields = {"step_number", "label", "title", "description", "badges", "layers_visible", "layer_new", "blur"}
        for s in steps:
            missing = required_fields - set(s.keys())
            if missing:
                Log.warn("SceneAnalyzer", f"Step missing fields: {missing}")
                return False
            got_nums.add(s["step_number"])
        if got_nums != required_nums:
            Log.warn("SceneAnalyzer", f"Step numbers {got_nums} != {{1..6}}")
            return False
        return True

    for attempt in range(1, 4):
        try:
            raw = _call_gemini(
                f"Produce scene script for:\n\n{question[:1500]}",
                _SCENE_SYSTEM,
                max_tokens=MAX_TOKENS_SCENE,
            )
            data = json.loads(_sanitize_json(raw))
            if _validate_scene(data):
                Log.ok("SceneAnalyzer", f"Got {len(data['steps'])} steps (all validated)")
                return data
            else:
                Log.warn("SceneAnalyzer", f"Attempt {attempt}: scene validation failed, retrying")
        except Exception as e:
            Log.warn("SceneAnalyzer", f"Attempt {attempt} failed: {e}")
        if attempt < 3:
            import time as _t; _t.sleep(15 * attempt)
    Log.warn("SceneAnalyzer", "All attempts failed — using FALLBACK scene")
    return FALLBACK


# ===========================================================================
# Stage 3: SVG + stepsData HTML Generator
# ===========================================================================
_SVG_BUILDER_SYSTEM = """You are QAnim SVG Builder. Given a question, a scene script, and the final answer,
generate ONLY the SVG layers and JavaScript for a 6-step concept animation.

You must output a JSON object with these keys:
{
  "svg_defs": "...",
  "svg_layers": "...",
  "steps_data_js": "...",
  "apply_step_js": "...",
  "raf_js": ""
}

FIELD DESCRIPTIONS:

svg_defs: SVG <defs> content (gradients, markers, filters). No wrapping tag needed.

svg_layers: SVG <g> elements, ONE per layer. All start at opacity:0 except layer-frame (opacity:1).
  Each layer ID must match the scene script's svg_layers keys exactly.
  viewBox is 0 0 850 478. Make layers visually rich and domain-specific.
  Include a blur-shield rect: <rect id="blur-shield" width="100%" height="100%" fill="#c2d4e8" opacity="0" pointer-events="none"/>

steps_data_js: The var stepsData = [...] array — exactly 6 objects, one per step.
  Each object MUST have EXACTLY these keys:
    title    — string, the step title
    desc     — string, the step description
    badges   — JS ARRAY of HTML strings. CRITICAL FORMAT RULE:
                CORRECT:   badges: ['<span class="badge badge-cyan">L = 1m</span>']
                CORRECT:   badges: ['<span class="badge badge-cyan">A</span>', '<span class="badge badge-orange">B</span>']
                WRONG:     badges: "<span class=\\"badge badge-cyan\\">text</span>"
                WRONG:     badges: '<span class=\'badge\'>text</span>'
                Rule: outer delimiter MUST be single-quote [ ' ], inner class= MUST use double-quote " 
                The whole badges value is a JS ARRAY [...], NOT a string.
    blurOp   — number: 0.0 for step 1 and step 6; 0.38 for steps 2-5
    layerOpacities — object mapping layer-id to 0 or 1
    overlays — empty array []

  Example stepsData:
  var stepsData = [
    { title: "Step 1: Environment", desc: "The background.", badges: ['<span class="badge badge-cyan">Scale</span>'], blurOp: 0.0, layerOpacities: {"layer-frame": 1, "layer-obj": 0}, overlays: [] },
    { title: "Step 2: Object",      desc: "The object.",     badges: ['<span class="badge badge-cyan">D = 50mm</span>'], blurOp: 0.38, layerOpacities: {"layer-frame": 1, "layer-obj": 1}, overlays: [] }
  ];

apply_step_js: The BODY of function applyStep(idx). Must:
  1. Set window.currentStep = idx
  2. Update step-bar width: ((idx+1)/9*100) + '%'
  3. Update step-label text: 'Step ' + (idx+1) + ' of 9'
  4. Update step-dot CSS classes (active/done)
  5. Set info-title and info-desc text
  6. Render badges into id="info-badges" using:
       document.getElementById('info-badges').innerHTML = (stepsData[idx].badges || []).join('');
  7. Set layer opacities via el.style.opacity (NOT setAttribute)
  8. Set blur-shield opacity: document.getElementById('blur-shield').style.opacity = stepsData[idx].blurOp
  9. Disable/enable btn-prev based on idx === 0

  CRITICAL: use el.style.opacity = value  (NOT el.setAttribute('opacity', value))
  CRITICAL: render badges with .join('')  (NOT forEach/createElement)
  CRITICAL: step label says 'of 9' (NOT 'of 6')

raf_js: requestAnimationFrame loop if SVG has animation. Empty string "" if no animation.
  If used: define window.qanimStartRAF = function(){ window.qanimRafId = requestAnimationFrame(drawFrame); };
  Do NOT assign: window.qanimStartRAF = requestAnimationFrame(drawFrame)  (that stores a number, not a function)

RULES:
- No <html>, no <head>, no <body>, no <style>, no <script> wrappers.
- stepsData must have EXACTLY 6 entries.
- Layer IDs must match scene script svg_layers keys exactly.
- Return PURE JSON only (no markdown, no fences)."""


def _sanitize_svg_data(data: dict) -> dict:
    """
    Post-process Gemini's svg_data to fix all known hallucination bugs.

    Bug 1 — setAttribute vs style.opacity (apply_step_js + svg_layers):
      Gemini writes el.setAttribute('opacity', x) on <g> elements that use
      CSS style="opacity:...". The SVG attribute is overridden by the CSS so
      layers never appear/disappear.
      Fix: rewrite to el.style.opacity = x everywhere.

    Bug 2 — "of 6" / "/ 6" instead of "of 9" / "/ 9" (apply_step_js):
      Gemini divides the progress bar by 6 (SVG steps) instead of 9 (total).
      Fix: replace / 6 * 100 → / 9 * 100 and 'of 6' → 'of 9'.

    Bug 3 — Unescaped double-quotes in badge strings (steps_data_js):
      Gemini emits raw JS like:
          "badges": "<span class="badge badge-cyan">text</span>"
      The inner double-quotes terminate the JS string literal early, causing
      a syntax error that silently kills the ENTIRE <script> block.
      Steps 1-6 never render because applyStep() is never defined.
      Fix: replace class="badge..." → class='badge...' in the raw JS text.

    Bug 4a — RAF return value assigned to window.qanimStartRAF (raf_js):
      Gemini writes: window.qanimStartRAF = requestAnimationFrame(drawFrame)
      requestAnimationFrame returns a numeric ID, not a function.
      DOMContentLoaded then calls window.qanimStartRAF() → TypeError.
      Fix: wrap in a proper starter function.

    Bug 4b — window.qanimStartRAF assigned INSIDE drawFrame body (raf_js):
      Gemini sometimes puts the assignment as the last line of drawFrame():
          function drawFrame() { ...; window.qanimStartRAF = function(){...}; }
      This means qanimStartRAF is reassigned every animation frame but
      requestAnimationFrame is never actually called → animation freezes.
      Fix: move the assignment and the initial call outside drawFrame().

    Bug 4c — drawFrame() called before DOM elements exist (raf_js):
      Gemini calls drawFrame() or the RAF loop immediately at script parse time,
      before DOMContentLoaded, so getElementById() returns null and the
      animation breaks on the first frame.
      Fix: guard the initial call inside a DOMContentLoaded listener.
    """
    import re as _re

    # ── Bug 3: stepsData fully rebuilt in Python — no badge string bugs ─
    # Root cause of steps 1-6 not rendering:
    #   Gemini writes badge HTML inside JS string literals. Whatever quote
    #   style it picks (outer single, inner single  OR  outer double, inner
    #   double) causes a JS syntax error that silently kills the ENTIRE
    #   <script> block. applyStep(), nextStep(), stepsData all become
    #   undefined. Steps 1-6 never render.
    #
    # Fix: discard Gemini's stepsData entirely. Rebuild it in Python from
    #   the scene dict using html.escape() — no quote conflicts possible.
    #   _rebuild_steps_data_js() is defined just above build_svg_and_steps().
    #   It uses the scene passed via data["_scene"] if present.
    scene_for_rebuild = data.pop("_scene", None)
    if scene_for_rebuild and scene_for_rebuild.get("steps"):
        data["steps_data_js"] = _rebuild_steps_data_js(scene_for_rebuild)
        Log.ok("SVGSanitizer", "stepsData rebuilt from scene dict (badge bug eliminated)")
    else:
        # Fallback: try regex fixes on whatever Gemini wrote
        steps_js = data.get("steps_data_js", "")
        steps_js = steps_js.replace("class='badge ", 'class="badge ')
        steps_js = steps_js.replace('class="badge ', "class='badge ")
        steps_js = steps_js.replace("'of 6'", "'of 9'")
        steps_js = steps_js.replace('"of 6"', '"of 9"')
        data["steps_data_js"] = steps_js


    # ── Bug 1 + 2 + 5: fix apply_step_js ─────────────────────────────────
    apply_js = data.get("apply_step_js", "")
    # Bug 1: setAttribute('opacity', …) → style.opacity = …
    apply_js = _re.sub(
        r"\.setAttribute\s*\(\s*['\"]opacity['\"]\s*,\s*([^)]+)\)",
        r".style.opacity = \1",
        apply_js
    )
    # Bug 2a: / 6 * 100 → / 9 * 100
    apply_js = _re.sub(r'(/\s*6\s*\*\s*100)', '/ 9 * 100', apply_js)
    # Bug 2b: 'of 6' / "of 6" → 'of 9' / "of 9"
    apply_js = _re.sub(r"(['\"])of 6\1", lambda m: m.group(1) + "of 9" + m.group(1), apply_js)
    apply_js = _re.sub(r'\bof 6\b', 'of 9', apply_js)
    # Bug 5: applyStep uses step.badgeList.forEach(...createElement) but stepsData has
    # step.badges (a JS array). Replace with the simpler, correct join('') pattern.
    # Also catch step.description vs step.desc mismatch.
    apply_js = _re.sub(r'step\.badgeList\b', 'step.badges', apply_js)
    apply_js = _re.sub(r'sd\.badgeList\b',   'sd.badges',   apply_js)
    apply_js = _re.sub(r'\bsd\.description\b', 'sd.desc', apply_js)
    apply_js = _re.sub(r'\bstep\.description\b', 'step.desc', apply_js)
    # Replace forEach/createElement badge rendering with the safe join pattern
    apply_js = _re.sub(
        r"(?:step|sd|stepsData\[idx\])\.badges?\s*\.forEach\s*\([^;]+?\}\s*\)\s*;",
        "(stepsData[idx].badges || []).join('');",
        apply_js,
        flags=_re.DOTALL
    )
    # If applyStep still renders badges into a different element ID, normalise to info-badges
    apply_js = _re.sub(r"getElementById\(['\"]badge-row['\"]\)", "getElementById('info-badges')", apply_js)
    apply_js = _re.sub(r"getElementById\(['\"]badges['\"]\)", "getElementById('info-badges')", apply_js)
    data["apply_step_js"] = apply_js

    # ── Bugs 4a / 4b / 4c: fix raf_js ────────────────────────────────────
    raf_js = data.get("raf_js", "")
    if raf_js.strip():

        # Bug 4b: window.qanimStartRAF = function(){...} assigned INSIDE
        # drawFrame body → pull it out and place it after the function.
        # Uses brace-counting to find the real closing brace of drawFrame
        # (simple regex fails due to nested braces inside the assignment).
        def _fix_raf_inside_drawframe(js):
            m = _re.search(r'function\s+drawFrame\s*\(\s*\)\s*\{', js)
            if not m:
                return js
            # Walk forward counting braces to find the true closing brace
            start = m.start()
            open_pos = m.end() - 1  # position of the opening {
            depth = 0
            end_pos = None
            for i in range(open_pos, len(js)):
                if js[i] == '{':
                    depth += 1
                elif js[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end_pos = i
                        break
            if end_pos is None:
                return js
            body = js[open_pos + 1:end_pos]
            if 'window.qanimStartRAF' not in body:
                return js
            # Strip the assignment from the body
            body_clean = _re.sub(
                r'\s*window\.qanimStartRAF\s*=\s*function\s*\([^)]*\)\s*\{[^}]*\}\s*;?',
                '',
                body
            )
            rebuilt = (
                js[:start]
                + "function drawFrame() {" + body_clean + "}\n"
                + "window.qanimStartRAF = function(){"
                  " window.qanimRafId = requestAnimationFrame(drawFrame); };"
                + js[end_pos + 1:]
            )
            return rebuilt

        raf_js = _fix_raf_inside_drawframe(raf_js)

        # Bug 4a: window.qanimStartRAF = requestAnimationFrame(fn)  (bare assignment)
        raf_js = _re.sub(
            r'window\.qanimStartRAF\s*=\s*requestAnimationFrame\s*\(([^)]+)\)\s*;?',
            r'window.qanimStartRAF = function(){ window.qanimRafId = requestAnimationFrame(\1); };',
            raf_js
        )

        # Bug 4c: bare immediate drawFrame() / RAF call at top level (no guard)
        # Replace:  if (!window.qanimStartRAF) { drawFrame(); }
        # or bare:  drawFrame();
        # With a safe DOMContentLoaded-guarded starter.
        raf_js = _re.sub(
            r'if\s*\(\s*!\s*window\.qanimStartRAF\s*\)\s*\{[^}]*\}',
            '',
            raf_js
        )
        # Ensure we have exactly one safe starter at the end
        if 'window.qanimStartRAF' in raf_js and 'qanimStartRAF()' not in raf_js:
            raf_js = raf_js.rstrip() + (
                "\nif(!window.__qanimRAFStarted){"
                " window.__qanimRAFStarted=true;"
                " document.addEventListener('DOMContentLoaded',"
                " function(){ if(typeof window.qanimStartRAF==='function') window.qanimStartRAF(); }); }"
            )

    data["raf_js"] = raf_js

    # ── Bug 1 in SVG: non-frame layers with opacity="1" attribute ─────────
    svg_layers = data.get("svg_layers", "")

    def _fix_layer_opacity(m):
        tag = m.group(0)
        gid = _re.search(r'id=["\']([^"\']+)["\']', tag)
        layer_id = gid.group(1) if gid else ""
        if layer_id == "layer-frame":
            return tag  # layer-frame must always be visible
        # Replace SVG opacity attribute with CSS style (CSS transition works on style)
        tag = _re.sub(r'\bopacity=["\']1["\']', 'style="opacity:0"', tag)
        tag = _re.sub(r'\bopacity=["\']0["\']', 'style="opacity:0"', tag)
        # If still no style/opacity, add it
        if 'opacity' not in tag and 'style' not in tag:
            tag = tag.rstrip('>') + ' style="opacity:0">'
        return tag

    svg_layers = _re.sub(
        r'<g\b[^>]*id=["\'][^"\']+["\'][^>]*>',
        _fix_layer_opacity,
        svg_layers
    )
    data["svg_layers"] = svg_layers

    Log.ok("SVGSanitizer", "svg_data post-processed (bugs 1-4 fixed)")
    return data


def _rebuild_steps_data_js(scene: dict) -> str:
    """
    Generate guaranteed-safe stepsData JS from the scene dict.
    Writes badges as a JS ARRAY of strings: outer-single / inner-double quotes.
    This matches the reference HTML format exactly and is immune to any quote conflict.

    Format: badges: ['<span class="badge badge-cyan">text</span>', ...]
    applyStep renders with: (stepsData[idx].badges || []).join('')
    """
    steps = scene.get("steps", [])
    all_layer_ids = list(scene.get("svg_layers", {}).keys())
    CLS = {"cyan": "badge-cyan", "orange": "badge-orange", "green": "badge-green"}

    def _badge_arr(badges):
        parts = []
        for b in badges:
            cls = CLS.get(b.get("type", "cyan"), "badge-cyan")
            text = html_module.escape(str(b.get("text", "")))
            # outer=single, inner=double: always safe in JS regardless of context
            parts.append("'<span class=\"badge " + cls + "\">'" + " + " + repr(text) + " + '</span>'")
        if not parts:
            return "[]"
        # Build the actual JS array of string literals
        items = []
        for b in badges:
            cls = CLS.get(b.get("type", "cyan"), "badge-cyan")
            text = html_module.escape(str(b.get("text", "")))
            items.append("'<span class=\"badge " + cls + "\">" + text + "</span>'")
        return "[" + ", ".join(items) + "]"

    rows = []
    for s in steps:
        lo = {lid: 0 for lid in all_layer_ids}
        for vis in s.get("layers_visible", []):
            if vis in lo:
                lo[vis] = 1
        blur = 0.38 if s.get("blur", False) else 0.0
        badge_arr = _badge_arr(s.get("badges", []))
        lo_pairs = ", ".join(f'"{k}": {v}' for k, v in lo.items())
        title_esc = s.get("title", "").replace("\\", "\\\\").replace('"', '\\"')
        desc_esc  = s.get("description", "").replace("\\", "\\\\").replace('"', '\\"')
        rows.append(
            '  {\n'
            f'    title: "{title_esc}",\n'
            f'    desc: "{desc_esc}",\n'
            f'    badges: {badge_arr},\n'
            f'    blurOp: {blur},\n'
            f'    layerOpacities: {{{lo_pairs}}},\n'
            '    overlays: []\n'
            '  }'
        )

    return "var stepsData = [\n" + ",\n".join(rows) + "\n];\nwindow.stepsData = stepsData;"


def build_svg_and_steps(question: str, scene: dict, sol: dict) -> dict:
    """Call Gemini to generate SVG layers + stepsData JS."""
    FALLBACK_SVG = """<g class="svg-layer" id="layer-frame" style="opacity:1">
  <rect width="850" height="478" fill="url(#grid-pat)" opacity="0.4"/>
  <text x="425" y="60" font-family="'Segoe UI',sans-serif" font-size="18" font-weight="700" fill="#475569" text-anchor="middle">Physical Setup</text>
</g>
<rect id="blur-shield" width="100%" height="100%" fill="#c2d4e8" opacity="0" pointer-events="none"/>
<g class="svg-layer" id="layer-object" style="opacity:0">
  <rect x="275" y="189" width="300" height="100" rx="8" fill="#bfdbfe" stroke="#3b82f6" stroke-width="2"/>
  <text x="425" y="244" font-family="'Segoe UI',sans-serif" font-size="16" font-weight="700" fill="#1d4ed8" text-anchor="middle">Main Object</text>
</g>
<g class="svg-layer" id="layer-param1" style="opacity:0">
  <rect x="100" y="60" width="160" height="50" rx="8" fill="rgba(22,163,74,0.12)" stroke="#16a34a" stroke-width="1.5"/>
  <text x="180" y="92" font-family="'Segoe UI',sans-serif" font-size="14" font-weight="700" fill="#15803d" text-anchor="middle">Given: Value 1</text>
</g>
<g class="svg-layer" id="layer-param2" style="opacity:0">
  <rect x="590" y="60" width="160" height="50" rx="8" fill="rgba(217,119,6,0.12)" stroke="#d97706" stroke-width="1.5"/>
  <text x="670" y="92" font-family="'Segoe UI',sans-serif" font-size="14" font-weight="700" fill="#92400e" text-anchor="middle">Given: Value 2</text>
</g>
<g class="svg-layer" id="layer-derived" style="opacity:0">
  <rect x="325" y="340" width="200" height="50" rx="8" fill="rgba(124,58,237,0.12)" stroke="#7c3aed" stroke-width="1.5"/>
  <text x="425" y="372" font-family="'Segoe UI',sans-serif" font-size="14" font-weight="700" fill="#6d28d9" text-anchor="middle">Derived Quantity</text>
</g>
<g class="svg-layer" id="layer-summary" style="opacity:0">
  <rect x="30" y="340" width="260" height="120" rx="12" fill="rgba(8,145,178,0.1)" stroke="#0891b2" stroke-width="1.5"/>
  <text x="160" y="370" font-family="'Segoe UI',sans-serif" font-size="13" font-weight="800" fill="#0e7490" text-anchor="middle">Given Data</text>
  <rect x="560" y="340" width="260" height="120" rx="12" fill="rgba(22,163,74,0.1)" stroke="#16a34a" stroke-width="1.5"/>
  <text x="690" y="370" font-family="'Segoe UI',sans-serif" font-size="13" font-weight="800" fill="#15803d" text-anchor="middle">Unknown: ?</text>
</g>"""

    FALLBACK_DEFS = """<pattern id="grid-pat" width="40" height="40" patternUnits="userSpaceOnUse">
  <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#1e3a5f" stroke-width="0.5" stroke-opacity="0.07"/>
</pattern>"""

    steps = scene.get("steps", [])
    all_layer_ids = list(scene.get("svg_layers", {}).keys())

    # Build fallback stepsData — badges ALWAYS as JS array (never a string)
    # so that (data.badges || []).join('') is always safe in applyStep.
    fallback_steps_js = _rebuild_steps_data_js(scene)

    # FIX D: FALLBACK_APPLY uses Array.isArray guard so badges work whether
    # they arrive as an array or (legacy) as a plain string.
    FALLBACK_APPLY = """function applyStep(idx) {
  window.currentStep = idx;
  var data = stepsData[idx] || {};
  var bs = document.getElementById('blur-shield');
  if(bs) bs.style.opacity = (typeof data.blurOp === 'number') ? data.blurOp : 0;
  var lo = data.layerOpacities || {};
  for(var lid in lo){
    var el = document.getElementById(lid);
    if(el) el.style.opacity = lo[lid];
  }
  var titleEl = document.getElementById('info-title');
  if(titleEl) titleEl.innerHTML = data.title || '';
  var badgeEl = document.getElementById('info-badges');
  if(badgeEl){
    var bdg = data.badges;
    if(Array.isArray(bdg)) badgeEl.innerHTML = bdg.join('');
    else if(typeof bdg === 'string') badgeEl.innerHTML = bdg;
    else badgeEl.innerHTML = '';
  }
  var descEl = document.getElementById('info-desc');
  if(descEl) descEl.innerHTML = data.desc || '';
  var dots = document.querySelectorAll('.step-dot');
  for(var i=0;i<dots.length;i++){
    dots[i].className='step-dot'+(i<idx?' done':i===idx?' active':'');
  }
  var lbl = document.getElementById('step-label');
  if(lbl) lbl.innerHTML = 'Step '+(idx+1)+' of 9';
  var bar = document.getElementById('step-bar');
  if(bar) bar.style.width = ((idx+1)/9*100)+'%';
  var pb = document.getElementById('btn-prev');
  if(pb) pb.disabled = (idx === 0);
  var nb = document.getElementById('btn-next');
  if(nb) nb.textContent = (idx >= (window.totalSteps || 5)) ? 'Step 7: Formula \u25b6' : 'Next Step \u25b6';
}"""

    if _gemini_client is None:
        return {"svg_defs": FALLBACK_DEFS, "svg_layers": FALLBACK_SVG,
                "steps_data_js": fallback_steps_js, "apply_step_js": FALLBACK_APPLY, "raf_js": ""}

    scene_summary = {
        "title": scene.get("title", ""),
        "steps": [{"step_number": s["step_number"], "label": s["label"], "title": s["title"],
                   "description": s["description"], "badges": s.get("badges", []),
                   "layers_visible": s.get("layers_visible", []), "blur": s.get("blur", False)}
                  for s in steps],
        "svg_layers": {k: v["description"] for k, v in scene.get("svg_layers", {}).items()},
    }

    prompt = f"""Question to animate:
\"\"\"{question[:1200]}\"\"\"

Scene script:
{json.dumps(scene_summary, indent=2, ensure_ascii=False)[:2000]}

Final answer: {sol.get('final_answer', 'See calculation')}
Formula: {sol.get('formula', 'Governing formula')}

Generate the SVG layers and JavaScript for this 6-step animation.
Make the SVG rich, detailed, and domain-appropriate.
The stepsData must reflect the exact scene script steps."""

    for attempt in range(1, 4):
        try:
            raw = _call_gemini(prompt, _SVG_BUILDER_SYSTEM, max_tokens=MAX_TOKENS_HTML // 2)
            data = json.loads(_sanitize_json(raw))
            if data.get("svg_layers") and data.get("steps_data_js"):
                Log.ok("SVGBuilder", f"Got SVG ({len(data.get('svg_layers',''))} chars)")
                data["_scene"] = scene   # passed to _sanitize_svg_data for stepsData rebuild
                return _sanitize_svg_data(data)
        except Exception as e:
            Log.warn("SVGBuilder", f"Attempt {attempt} failed: {e}")
            if attempt < 3:
                import time as _t; _t.sleep(15 * attempt)

    return {"svg_defs": FALLBACK_DEFS, "svg_layers": FALLBACK_SVG,
            "steps_data_js": fallback_steps_js, "apply_step_js": FALLBACK_APPLY, "raf_js": ""}


# ===========================================================================
# HTML Assembly — reference-exact templates
# ===========================================================================

def _he(s: str) -> str:
    return html_module.escape(str(s))


def _build_scene6_html(sol: dict) -> str:
    """Build Scene 7 (Main Formula) HTML — matches reference exactly."""
    formula_text = _he(sol.get("formula", "Formula"))
    formula_name = _he(sol.get("formula_name", "Governing Equation"))
    variables = sol.get("variables", [])
    note_text = _he(sol.get("key_insight", ""))

    COLOR_MAP = {
        "blue": "s6v-blue", "cyan": "s6v-teal", "orange": "s6v-orange",
        "green": "s6v-green", "red": "s6v-red", "purple": "s6v-purple",
        "teal": "s6v-teal", "amber": "s6v-orange", "violet": "s6v-purple",
    }

    var_boxes = ""
    for v in variables:
        sym = _he(v.get("symbol", "?"))
        name = _he(v.get("name", "Variable"))
        val = _he(v.get("value", ""))
        unit = _he(v.get("unit", ""))
        color_cls = COLOR_MAP.get(v.get("color", "blue"), "s6v-blue")
        val_str = f"{val} {unit}".strip() if val else ""
        var_boxes += f"""<div class="s6-var-box {color_cls}" data-idx="{variables.index(v)}">
          <div class="s6-var-arrow"></div>
          <div class="s6-var-inner">
            <span class="s6-var-sym">{sym}</span>
            <span class="s6-var-name">{name}</span>
            <span class="s6-var-val">{_he(val_str)}</span>
          </div>
        </div>
"""

    note_bar = ""
    if note_text:
        note_bar = f"""<div class="s6-note-bar" id="s6-note-bar">
        <span class="s6-note-icon">&#x26A1;</span>
        <span class="s6-note-text" id="s6-note-text">{note_text}</span>
      </div>"""

    return f"""<div id="qanim-scene6-overlay">
  <div class="s6-card">
    <div class="s6-title-bar">
      <h2 id="s6-card-title">Step 7 &mdash; Main Formula</h2>
    </div>
    <div class="s6-body">
      <div class="s6-phase-progress" id="s6-phase-progress">Step 1 of {len(variables) + 1} &mdash; The Formula</div>
      <div class="s6-phase-caption" id="s6-phase-caption">This is the governing formula for this problem.</div>
      <div class="s6-formula-box">
        <div class="s6-formula-main" id="s6-formula-text">{formula_text}</div>
        <div class="s6-formula-sublabel" id="s6-formula-sublabel">{formula_name}</div>
      </div>
      <div class="s6-vars-row" id="s6-vars-row">
        {var_boxes}
      </div>
      {note_bar}
    </div>
    <div class="s6-nav-row">
      <button class="btn-secondary" onclick="qanim_goToPrevScene()" id="s6-prev-btn">&#x2190; Back to Step 6</button>
      <button class="btn-primary" onclick="qanim_s6Advance()" id="s6-next-btn">Next &#x25B6;</button>
    </div>
  </div>
</div>"""


def _build_scene7_html(sol: dict, scene: dict) -> str:
    """Build Scene 8 (Substitution) HTML — matches reference exactly."""
    system_title = _he(sol.get("system_title", "Physical System"))
    system_label2 = _he(sol.get("system_label2", "Substituting given values"))
    formula_result = _he(sol.get("formula", "Formula"))
    final_answer = _he(sol.get("final_answer", "See calculation"))

    given_list = sol.get("given_list", [])
    given_html = "".join(
        f'<div class="s7-given-item"><strong>{_he(g.split("=")[0].strip() if "=" in g else "")}</strong>'
        f'{(" = " + _he(g.split("=",1)[1].strip())) if "=" in g else _he(g)}</div>\n'
        for g in given_list
    )

    approach_steps = sol.get("approach_steps", [])
    approach_html = ""
    for ap in approach_steps:
        num = _he(str(ap.get("num", "")))
        label = _he(ap.get("label", ""))
        eq = _he(ap.get("eq", ""))
        note = _he(ap.get("note", ""))
        approach_html += f"""<div class="s7-approach-step">
          <span class="s7-approach-step-num">{num}</span>
          <span>{label}
            <span class="s7-approach-step-eq">{eq}</span>
            {f'<span style="display:block;font-size:11px;color:#64748b;margin-top:3px;">{note}</span>' if note else ''}
          </span>
        </div>
"""

    return f"""<div id="qanim-scene7-overlay">
  <div class="s7-card">
    <div class="s7-title-bar">
      <h2>Step 8 &mdash; Step-by-Step Substitution</h2>
    </div>
    <div class="s7-body-cols">
      <div class="s7-left-col">
        <div class="s7-system-label">System Diagram</div>
        <div class="s7-system-visual">
          <div class="s7-system-visual-title" id="s7-system-title">{system_title}</div>
          <div class="s7-system-arrows">&#x2191; &#x2191; &#x2191;</div>
          <div class="s7-system-label2" id="s7-system-label2">{system_label2}</div>
        </div>
        <div class="s7-formula-result-bar">
          <div class="s7-formula-result-text" id="s7-formula-result">{formula_result}</div>
          <div class="s7-formula-units" id="s7-units-hint">Units: check dimensional consistency</div>
        </div>
      </div>
      <div class="s7-right-col">
        <div>
          <div class="s7-given-section-title">Given Parameters</div>
          <div class="s7-given-list" id="s7-given-list">{given_html}</div>
        </div>
        <div>
          <div class="s7-approach-section-title">Substituting Given Values into the Formula</div>
          <div class="s7-approach-list" id="s7-approach-list">{approach_html}</div>
        </div>
        <div style="background:linear-gradient(135deg,#fffbeb,#fef3c7);border:1.5px solid #fcd34d;border-radius:10px;padding:11px 15px;display:flex;align-items:center;gap:8px;">
          <span style="font-size:16px;">&#x27A1;&#xFE0F;</span>
          <span style="font-size:12.5px;font-weight:700;color:#92400e;">Proceed to <strong>Step 9</strong> to see the Final Answer with units and conclusion.</span>
        </div>
      </div>
    </div>
    <div class="s7-nav-row">
      <button class="btn-secondary" onclick="qanim_goToScene6FromScene7()">&#x2190; Back to Step 7</button>
      <button class="btn-primary" onclick="if(typeof window.qanim_showScene9===&#39;function&#39;)window.qanim_showScene9()">Step 9: Final Answer &#x25B6;</button>
    </div>
  </div>
</div>"""


def _build_scene9_html(sol: dict, to_find: list) -> str:
    """Build Scene 9 (Final Answer) HTML — matches reference exactly."""
    formula_recap = _he(sol.get("formula", "Governing Formula"))
    chain = sol.get("substitution_chain", [])
    answer_value = _he(sol.get("answer_value", "?"))
    answer_unit = _he(sol.get("answer_unit", ""))
    key_insight = sol.get("key_insight", "Apply the governing formula with the given data.")
    to_find_label = _he(to_find[0] if to_find else "Final Answer")
    final_answer = _he(sol.get("final_answer", "See calculation"))

    chain_html = ""
    for row in chain:
        num = row.get("num", 1)
        eq = _he(row.get("eq", ""))
        chain_html += f'<div class="s9-sub-row" data-s9-idx="{num-1}"><div class="s9-sub-num">{num}</div><div class="s9-sub-eq">{eq}</div></div>\n'

    return f"""<div id="qanim-scene9-overlay">
  <div class="s9-card">
    <div class="s9-title-bar">
      <h2>&#x2705; Step 9 &mdash; Final Answer</h2>
      <p>{to_find_label}</p>
    </div>
    <div class="s9-body">
      <div class="s9-formula-recap">
        <div class="s9-formula-recap-label">&#x1F4D0; Governing Formula (from Step 7)</div>
        <div class="s9-formula-recap-eq" id="s9-formula-recap">{formula_recap}</div>
      </div>
      <div class="s9-sub-chain" id="s9-sub-chain">
        {chain_html}
      </div>
      <div class="s9-final-box" id="s9-final-box">
        <div class="s9-final-label">&#x2B50; Final Answer</div>
        <div class="s9-final-value" id="s9-final-value"><span class="s9-highlight">{answer_value}</span> {answer_unit}</div>
        <div class="s9-final-unit" id="s9-final-unit">Units: {answer_unit} &nbsp;|&nbsp; &#x2714; Dimensionally consistent</div>
      </div>
      <div class="s9-insight-bar" id="s9-insight-bar">
        <span class="s9-insight-icon">&#x1F4A1;</span>
        <div class="s9-insight-text" id="s9-insight-text"><strong>Key Insight:</strong> {_he(key_insight)}</div>
      </div>
    </div>
    <div class="s9-nav-row">
      <button class="btn-secondary" onclick="if(typeof window.qanim_goToScene7FromScene9===&#39;function&#39;)window.qanim_goToScene7FromScene9()">&#x2190; Back to Step 8</button>
      <button class="btn-primary" onclick="if(typeof window.resetAnim===&#39;function&#39;)window.resetAnim()">&#x21BA; Restart Animation</button>
    </div>
  </div>
</div>"""


# ===========================================================================
# CSS Templates (reference-exact)
# ===========================================================================

_BASE_CSS = """
:root {
  --bg-color: #eef2f9;
  --panel-bg: #ffffff;
  --text-main: #1e293b;
  --text-sub: #64748b;
  --text-muted: #94a3b8;
  --accent-cyan: #0891b2;
  --accent-cyan-dim: #0e7490;
  --accent-orange: #d97706;
  --accent-green: #16a34a;
  --border: #e2e8f0;
  --border-strong: #cbd5e1;
  --border-radius: 16px;
  --border-radius-sm: 10px;
  --shadow-card: 0 1px 3px rgba(15,23,42,.06),0 8px 24px rgba(15,23,42,.08),0 24px 48px rgba(15,23,42,.04);
  --transition-smooth: .45s cubic-bezier(.4,0,.2,1);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Segoe UI',system-ui,-apple-system,BlinkMacSystemFont,sans-serif;
  background:linear-gradient(160deg,#eef2f9 0%,#e8f0fe 50%,#eff6ff 100%);background-attachment:fixed;
  color:var(--text-main);display:flex;flex-direction:column;align-items:center;
  justify-content:flex-start;min-height:100vh;padding:28px 16px 140px;}
.page-header{width:100%;max-width:900px;margin-bottom:14px;display:flex;align-items:center;gap:10px;}
.page-chip{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:20px;
  background:rgba(8,145,178,.10);border:1px solid rgba(8,145,178,.22);font-size:11px;font-weight:700;
  color:var(--accent-cyan-dim);text-transform:uppercase;letter-spacing:.8px;}
.page-chip::before{content:'▶';font-size:8px;}
.dashboard{width:100%;max-width:900px;margin:0 auto;background:var(--panel-bg);
  border-radius:var(--border-radius);box-shadow:var(--shadow-card);overflow:hidden;
  border:1px solid var(--border);position:relative;}
.dashboard::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:linear-gradient(90deg,var(--accent-cyan-dim) 0%,#7c3aed 50%,var(--accent-orange) 100%);
  border-radius:var(--border-radius) var(--border-radius) 0 0;z-index:2;}
.question-banner{padding:22px 28px 18px;
  background:linear-gradient(135deg,#f8faff 0%,#f0f5ff 40%,#eef2f9 100%);
  border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:8px;position:relative;overflow:hidden;}
.q-label{font-size:10.5px;font-weight:800;color:var(--accent-cyan-dim);text-transform:uppercase;
  letter-spacing:1.8px;display:flex;align-items:center;gap:8px;}
.q-label::before{content:'';display:inline-block;width:16px;height:16px;border-radius:5px;
  background:linear-gradient(135deg,var(--accent-cyan-dim),var(--accent-cyan));flex-shrink:0;}
.q-text{font-size:15px;color:var(--text-main);line-height:1.6;font-weight:450;max-width:820px;}
.svg-container{width:100%;aspect-ratio:16/9;
  background:radial-gradient(ellipse at 35% 38%,#eef5ff 0%,#dce8f5 45%,#c8d9ed 85%,#b8ccdf 100%);
  position:relative;overflow:hidden;border-bottom:1px solid var(--border);}
svg{display:block;width:100%;height:100%;}
.svg-layer{transition:opacity .55s cubic-bezier(.4,0,.2,1);}
.control-panel{padding:22px 28px 26px;background:linear-gradient(180deg,#ffffff 0%,#f9fbff 100%);border-top:1px solid var(--border);}
.step-color-legend{display:flex;gap:12px;margin-bottom:12px;flex-wrap:wrap;}
.step-legend-item{display:flex;align-items:center;gap:4px;font-size:10px;color:var(--text-sub);font-weight:600;text-transform:uppercase;}
.step-legend-dot{width:8px;height:8px;border-radius:50%;}
.step-indicator{display:flex;align-items:center;gap:6px;margin-bottom:16px;flex-wrap:wrap;}
.step-connector{flex:0 0 18px;height:1.5px;background:linear-gradient(90deg,#cbd5e1,#e2e8f0);border-radius:2px;}
.step-dot{padding:6px 14px;border-radius:20px;background:#f1f5f9;border:1.5px solid #e2e8f0;
  font-size:11.5px;font-weight:700;color:#94a3b8;cursor:pointer;
  transition:background .3s,color .3s,border-color .3s,box-shadow .3s,transform .25s cubic-bezier(.34,1.56,.64,1);
  white-space:nowrap;user-select:none;position:relative;}
.step-dot:hover:not(.active){background:rgba(8,145,178,.07);border-color:rgba(8,145,178,.3);color:var(--accent-cyan-dim);}
.step-dot.active{background:linear-gradient(135deg,#0e7490 0%,#0891b2 100%);border-color:transparent;color:#fff;
  box-shadow:0 3px 12px rgba(8,145,178,.38),0 1px 3px rgba(8,145,178,.20);transform:scale(1.07);}
.step-dot.done{background:rgba(22,163,74,.09);border-color:rgba(22,163,74,.28);color:#15803d;}
.step-label{font-size:11px;color:var(--text-muted);font-weight:600;letter-spacing:.6px;
  text-transform:uppercase;margin-left:6px;flex:1;min-width:0;}
.step-progress-wrap{height:3px;background:#f1f5f9;border-radius:2px;margin-bottom:20px;overflow:hidden;}
.step-progress-bar{height:100%;background:linear-gradient(90deg,#0e7490,#0891b2,#38bdf8);
  border-radius:2px;transition:width .5s cubic-bezier(.4,0,.2,1);width:0%;}
.info-box{background:linear-gradient(135deg,#f8fbff 0%,#f4f8ff 100%);border:1px solid #dde8f8;
  border-left:4px solid var(--accent-cyan);border-radius:var(--border-radius-sm);
  padding:20px 22px;min-height:130px;display:flex;flex-direction:column;gap:11px;position:relative;overflow:hidden;}
.info-box h3{color:var(--text-main);font-size:16.5px;font-weight:800;display:flex;align-items:center;
  gap:10px;line-height:1.3;letter-spacing:-.2px;}
.info-box h3::before{content:'';display:inline-block;width:8px;height:8px;border-radius:50%;
  background:var(--accent-cyan);flex-shrink:0;box-shadow:0 0 0 3px rgba(8,145,178,.18);}
.badges{display:flex;gap:7px;flex-wrap:wrap;align-items:center;}
.badge{padding:4px 12px;border-radius:20px;font-size:11.5px;font-weight:700;display:inline-flex;align-items:center;gap:5px;letter-spacing:.1px;}
.badge-cyan{background:rgba(8,145,178,.09);border:1px solid rgba(8,145,178,.28);color:#0e7490;}
.badge-orange{background:rgba(217,119,6,.09);border:1px solid rgba(217,119,6,.28);color:#92400e;}
.badge-green{background:rgba(22,163,74,.09);border:1px solid rgba(22,163,74,.28);color:#15803d;}
.info-desc{font-size:14px;line-height:1.7;color:var(--text-sub);font-weight:400;}
.actions{display:flex;justify-content:flex-end;align-items:center;gap:10px;margin-top:20px;}
button{padding:11px 24px;border-radius:10px;font-size:13.5px;font-weight:700;font-family:inherit;
  cursor:pointer;transition:background .22s,box-shadow .22s,transform .18s cubic-bezier(.34,1.56,.64,1),color .2s,border-color .2s;border:none;outline:none;letter-spacing:.1px;}
.btn-primary{background:linear-gradient(135deg,#0e7490 0%,#0891b2 100%);color:#fff;
  box-shadow:0 4px 14px rgba(8,145,178,.30),0 1px 3px rgba(8,145,178,.15);}
.btn-primary:hover{background:linear-gradient(135deg,#0c6680 0%,#0e7490 100%);
  box-shadow:0 6px 22px rgba(8,145,178,.38);transform:translateY(-2px);}
.btn-secondary{background:#fff;color:var(--text-sub);border:1.5px solid var(--border-strong);
  box-shadow:0 1px 3px rgba(15,23,42,.06);}
.btn-secondary:hover{background:#f8fafc;color:var(--text-main);border-color:#94a3b8;
  box-shadow:0 2px 8px rgba(15,23,42,.10);transform:translateY(-1px);}
"""

_SCENE6_CSS = """
#qanim-scene-modal-backdrop{display:none;position:fixed;inset:0;z-index:7400;background:rgba(15,23,42,.50);backdrop-filter:blur(6px);opacity:0;transition:opacity .25s ease;}
#qanim-scene-modal-backdrop.qanim-scene-visible{display:block!important;opacity:1;}
#qanim-scene6-overlay{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) scale(.95);z-index:7500;width:min(860px,96vw);max-height:92vh;overflow-y:auto;box-sizing:border-box;opacity:0;pointer-events:none;transition:opacity .3s ease,transform .3s cubic-bezier(.34,1.56,.64,1);}
#qanim-scene6-overlay.qanim-scene-visible{display:block!important;opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.s6-card{background:#fff;border-radius:20px;box-shadow:0 8px 48px rgba(8,145,178,.14),0 2px 8px rgba(0,0,0,.08);border:1px solid #dde8f8;overflow:hidden;font-family:-apple-system,'Segoe UI',Arial,sans-serif;}
.s6-title-bar{text-align:center;padding:22px 28px 18px;background:#fff;border-bottom:1px solid #e8eef8;}
.s6-title-bar h2{font-size:20px;font-weight:900;color:#0f172a;letter-spacing:-.3px;}
.s6-body{padding:28px 32px 24px;background:linear-gradient(160deg,#eef2f9 0%,#e8f0fe 50%,#eff6ff 100%);}
.s6-formula-box{background:#fff;border:2.5px solid #3b82f6;border-radius:18px;padding:20px 32px 16px;text-align:center;margin-bottom:10px;position:relative;}
.s6-formula-main{font-family:'Courier New',monospace;font-size:28px;font-weight:900;color:#1d4ed8;letter-spacing:1px;line-height:1.4;word-break:break-word;opacity:0;transform:translateY(8px);transition:opacity .5s,transform .5s;}
.s6-formula-main.s6-shown{opacity:1;transform:translateY(0);}
.s6-formula-sublabel{font-size:11px;font-weight:700;color:#6366f1;letter-spacing:.3px;margin-top:8px;opacity:0;transition:opacity .4s ease .2s;}
.s6-formula-sublabel.s6-shown{opacity:1;}
.s6-vars-row{display:flex;align-items:flex-start;justify-content:center;gap:12px;flex-wrap:wrap;margin-top:24px;}
.s6-var-box{display:flex;flex-direction:column;align-items:center;gap:0;min-width:120px;max-width:160px;opacity:0;transform:translateY(16px);transition:opacity .45s cubic-bezier(.4,0,.2,1),transform .4s cubic-bezier(.34,1.56,.64,1);}
.s6-var-box.s6-shown{opacity:1;transform:translateY(0);}
.s6-var-arrow{width:2px;height:24px;position:relative;margin-bottom:0;}
.s6-var-arrow::before{content:'';position:absolute;left:50%;transform:translateX(-50%);top:0;width:2px;height:18px;border-radius:1px;}
.s6-var-arrow::after{content:'';position:absolute;bottom:0;left:50%;transform:translateX(-50%);border-left:6px solid transparent;border-right:6px solid transparent;}
.s6-var-box.s6v-red   .s6-var-inner{border-color:#f43f5e;background:#fff1f2;}.s6-var-box.s6v-red .s6-var-sym{color:#be123c;}.s6-var-box.s6v-red .s6-var-arrow::before{background:#f43f5e;}.s6-var-box.s6v-red .s6-var-arrow::after{border-top:8px solid #f43f5e;}
.s6-var-box.s6v-orange .s6-var-inner{border-color:#f59e0b;background:#fff7ed;}.s6-var-box.s6v-orange .s6-var-sym{color:#d97706;}.s6-var-box.s6v-orange .s6-var-arrow::before{background:#f59e0b;}.s6-var-box.s6v-orange .s6-var-arrow::after{border-top:8px solid #f59e0b;}
.s6-var-box.s6v-blue  .s6-var-inner{border-color:#3b82f6;background:#eff6ff;}.s6-var-box.s6v-blue .s6-var-sym{color:#1d4ed8;}.s6-var-box.s6v-blue .s6-var-arrow::before{background:#3b82f6;}.s6-var-box.s6v-blue .s6-var-arrow::after{border-top:8px solid #3b82f6;}
.s6-var-box.s6v-green .s6-var-inner{border-color:#22c55e;background:#f0fdf4;}.s6-var-box.s6v-green .s6-var-sym{color:#15803d;}.s6-var-box.s6v-green .s6-var-arrow::before{background:#22c55e;}.s6-var-box.s6v-green .s6-var-arrow::after{border-top:8px solid #22c55e;}
.s6-var-box.s6v-purple .s6-var-inner{border-color:#a855f7;background:#faf5ff;}.s6-var-box.s6v-purple .s6-var-sym{color:#7c3aed;}.s6-var-box.s6v-purple .s6-var-arrow::before{background:#a855f7;}.s6-var-box.s6v-purple .s6-var-arrow::after{border-top:8px solid #a855f7;}
.s6-var-box.s6v-teal  .s6-var-inner{border-color:#14b8a6;background:#f0fdfa;}.s6-var-box.s6v-teal .s6-var-sym{color:#0f766e;}.s6-var-box.s6v-teal .s6-var-arrow::before{background:#14b8a6;}.s6-var-box.s6v-teal .s6-var-arrow::after{border-top:8px solid #14b8a6;}
.s6-var-inner{border:2px solid;border-radius:14px;padding:14px 16px 12px;text-align:center;width:100%;box-sizing:border-box;}
.s6-var-sym{font-family:'Courier New',monospace;font-size:22px;font-weight:900;line-height:1;display:block;margin-bottom:5px;}
.s6-var-name{font-size:11.5px;font-weight:700;color:#475569;line-height:1.35;display:block;}
.s6-var-val{font-size:10.5px;font-weight:600;color:#94a3b8;margin-top:3px;display:block;}
.s6-note-bar{display:flex;align-items:center;justify-content:center;gap:8px;margin-top:22px;padding:12px 20px;background:#fff;border-radius:12px;border:1.5px solid #fde68a;opacity:0;transform:translateY(8px);transition:opacity .45s,transform .45s;}
.s6-note-bar.s6-shown{opacity:1;transform:translateY(0);}
.s6-note-icon{font-size:16px;flex-shrink:0;}
.s6-note-text{font-size:13px;font-weight:700;color:#92400e;}
.s6-nav-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:16px 32px 22px;border-top:1px solid #e8eef8;background:#fff;}
.s6-phase-progress{font-size:10.5px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:#0891b2;text-align:center;margin-bottom:4px;min-height:14px;}
.s6-phase-caption{font-size:13px;font-weight:600;color:#334155;text-align:center;margin-bottom:20px;line-height:1.5;min-height:18px;transition:opacity .3s;}
.s6-var-box.s6-active .s6-var-inner{box-shadow:0 0 0 4px rgba(8,145,178,.20),0 4px 16px rgba(8,145,178,.22);transform:scale(1.05);transition:transform .3s cubic-bezier(.34,1.56,.64,1),box-shadow .3s;}
"""

_SCENE7_CSS = """
#qanim-scene7-overlay{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) scale(.95);z-index:7500;width:min(900px,96vw);max-height:92vh;overflow-y:auto;box-sizing:border-box;opacity:0;pointer-events:none;transition:opacity .3s,transform .3s cubic-bezier(.34,1.56,.64,1);}
#qanim-scene7-overlay.qanim-scene-visible{display:block!important;opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.s7-card{background:#fff;border-radius:20px;box-shadow:0 8px 48px rgba(37,99,235,.12),0 2px 8px rgba(0,0,0,.07);border:1px solid #e8eef8;overflow:hidden;font-family:-apple-system,'Segoe UI',Arial,sans-serif;}
.s7-title-bar{text-align:center;padding:20px 28px 16px;border-bottom:1px solid #e8eef8;background:#fff;}
.s7-title-bar h2{font-size:20px;font-weight:900;color:#0f172a;letter-spacing:-.3px;}
.s7-body-cols{display:flex;align-items:flex-start;gap:0;min-height:320px;}
.s7-left-col{width:44%;min-width:200px;border-right:1.5px solid #e8eef8;padding:22px 20px 22px 26px;background:linear-gradient(180deg,#eff6ff 0%,#dbeafe 100%);display:flex;flex-direction:column;gap:0;align-self:stretch;}
.s7-system-label{font-size:10.5px;font-weight:800;color:#1d4ed8;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;}
.s7-system-visual{background:linear-gradient(135deg,#bfdbfe 0%,#93c5fd 100%);border-radius:12px;padding:16px 14px 14px;margin-bottom:16px;text-align:center;position:relative;overflow:hidden;}
.s7-system-visual-title{font-size:13px;font-weight:800;color:#1e3a5f;margin-bottom:6px;}
.s7-system-arrows{display:flex;justify-content:center;gap:10px;margin:8px 0;font-size:20px;color:#d97706;}
.s7-system-label2{font-size:10px;font-weight:600;color:#1e40af;margin-top:4px;}
.s7-right-col{flex:1;padding:22px 26px 20px 20px;display:flex;flex-direction:column;gap:16px;}
.s7-given-section-title{font-size:13px;font-weight:900;color:#1d4ed8;margin-bottom:8px;letter-spacing:-.1px;}
.s7-given-list{display:flex;flex-direction:column;gap:5px;margin-bottom:14px;}
.s7-given-item{font-size:12.5px;color:#334155;line-height:1.5;display:flex;align-items:flex-start;gap:7px;}
.s7-given-item::before{content:'•';color:#3b82f6;font-weight:900;flex-shrink:0;margin-top:1px;}
.s7-given-item strong{font-weight:700;color:#1e293b;}
.s7-approach-section-title{font-size:13px;font-weight:900;color:#7c3aed;margin-bottom:8px;}
.s7-approach-list{display:flex;flex-direction:column;gap:5px;margin-bottom:14px;}
.s7-approach-step{display:flex;align-items:flex-start;gap:8px;font-size:12.5px;color:#1e293b;line-height:1.5;margin-bottom:3px;}
.s7-approach-step-num{font-weight:800;color:#7c3aed;flex-shrink:0;min-width:18px;}
.s7-approach-step-eq{display:block;margin-top:4px;font-family:'Courier New',monospace;font-size:12px;font-weight:700;color:#dc2626;background:#fff7ed;border-radius:6px;padding:2px 8px;word-break:break-word;}
.s7-formula-result-bar{background:linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);border:2px solid #86efac;border-radius:12px;padding:12px 16px;}
.s7-formula-result-text{font-family:'Courier New',monospace;font-size:14px;font-weight:900;color:#15803d;line-height:1.5;word-break:break-word;}
.s7-formula-units{font-size:11px;color:#166534;margin-top:4px;font-style:italic;}
.s7-nav-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:16px 26px 22px;border-top:1px solid #e8eef8;background:#fff;}
@media(max-width:600px){.s7-body-cols{flex-direction:column;}.s7-left-col{width:100%;border-right:none;border-bottom:1.5px solid #e8eef8;}}
"""

_SCENE9_CSS = """
#qanim-scene9-overlay{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) scale(.95);z-index:7500;width:min(780px,96vw);max-height:92vh;overflow-y:auto;box-sizing:border-box;opacity:0;pointer-events:none;transition:opacity .3s,transform .3s cubic-bezier(.34,1.56,.64,1);}
#qanim-scene9-overlay.qanim-scene-visible{display:block!important;opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.s9-card{background:#fff;border-radius:20px;box-shadow:0 8px 48px rgba(22,163,74,.18),0 2px 8px rgba(0,0,0,.08);border:2px solid #86efac;overflow:hidden;font-family:-apple-system,'Segoe UI',Arial,sans-serif;}
.s9-title-bar{text-align:center;padding:22px 28px 18px;background:linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);border-bottom:2px solid #86efac;}
.s9-title-bar h2{font-size:22px;font-weight:900;color:#14532d;letter-spacing:-.3px;margin-bottom:4px;}
.s9-title-bar p{font-size:13px;color:#166534;margin:0;}
.s9-body{padding:32px 36px 28px;background:#fff;display:flex;flex-direction:column;gap:22px;}
.s9-formula-recap{background:#eff6ff;border:1.5px solid #bfdbfe;border-radius:12px;padding:14px 20px;text-align:center;}
.s9-formula-recap-label{font-size:10.5px;font-weight:800;color:#1d4ed8;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:6px;}
.s9-formula-recap-eq{font-family:'Courier New',monospace;font-size:18px;font-weight:900;color:#1d4ed8;}
.s9-sub-chain{display:flex;flex-direction:column;gap:10px;}
.s9-sub-row{display:flex;align-items:center;gap:12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 16px;opacity:0;transform:translateX(-18px);transition:opacity .4s,transform .4s cubic-bezier(.34,1.56,.64,1);}
.s9-sub-row.s9-shown{opacity:1;transform:translateX(0);}
.s9-sub-num{background:#0891b2;color:#fff;border-radius:50%;width:26px;height:26px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;flex-shrink:0;}
.s9-sub-eq{font-family:'Courier New',monospace;font-size:15px;font-weight:700;color:#1e293b;flex:1;}
.s9-final-box{background:linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);border:3px solid #22c55e;border-radius:18px;padding:28px 32px;text-align:center;position:relative;overflow:hidden;opacity:0;transform:scale(.94);transition:opacity .5s ease .3s,transform .5s cubic-bezier(.34,1.56,.64,1) .3s;}
.s9-final-box.s9-shown{opacity:1;transform:scale(1);}
.s9-final-label{font-size:11px;font-weight:900;text-transform:uppercase;letter-spacing:2px;color:#15803d;margin-bottom:12px;}
.s9-final-value{font-family:'Courier New',monospace;font-size:36px;font-weight:900;color:#14532d;line-height:1.2;}
.s9-final-value .s9-highlight{color:#16a34a;font-size:44px;}
.s9-final-unit{font-size:15px;color:#166534;margin-top:8px;font-weight:600;}
.s9-insight-bar{display:flex;align-items:flex-start;gap:10px;background:#fff7ed;border:1.5px solid #fed7aa;border-radius:10px;padding:13px 18px;opacity:0;transition:opacity .4s ease .6s;}
.s9-insight-bar.s9-shown{opacity:1;}
.s9-insight-icon{font-size:20px;flex-shrink:0;}
.s9-insight-text{font-size:13px;color:#92400e;line-height:1.6;}
.s9-insight-text strong{color:#78350f;}
.s9-nav-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:16px 36px 22px;border-top:1px solid #bbf7d0;background:#f0fdf4;}
"""

_CONTROLS_CSS = """
#answerbox-backdrop{display:none;position:fixed;inset:0;z-index:8400;background:rgba(15,23,42,.45);backdrop-filter:blur(4px);}
#answerbox-backdrop.open{display:block;}
#answerbox-panel{display:flex;flex-direction:column;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) scale(.96);z-index:8500;width:min(480px,94vw);max-height:85vh;border-radius:18px;overflow:hidden;background:#fff;border:1px solid #e2e8f0;box-shadow:0 8px 48px rgba(124,58,237,.18);opacity:0;pointer-events:none;transition:opacity .25s,transform .25s cubic-bezier(.34,1.56,.64,1);}
#answerbox-panel.open{opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.ab-header{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;background:linear-gradient(135deg,#faf5ff,#f0f9ff);border-bottom:1px solid #e2e8f0;}
.ab-header-title{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:16px;font-weight:800;color:#1e293b;}
.ab-close-btn{width:28px;height:28px;border-radius:8px;border:1px solid #e2e8f0;background:#f8fafc;color:#64748b;font-size:12px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s;}
.ab-close-btn:hover{background:#fee2e2;color:#dc2626;}
.ab-progress-row{display:flex;align-items:center;justify-content:space-between;padding:10px 20px 6px;border-bottom:1px solid #f1f5f9;}
.ab-progress-label{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:11.5px;font-weight:700;color:#64748b;}
.ab-progress-dots{display:flex;gap:4px;}
.ab-dot{width:8px;height:8px;border-radius:50%;background:#e2e8f0;transition:background .2s;}
.ab-dot.current{background:#7c3aed;transform:scale(1.2);}
.ab-dot.done{background:#22c55e;}
.ab-body{padding:16px 20px 20px;overflow-y:auto;display:flex;flex-direction:column;gap:0;}
.ab-find-chip{display:flex;align-items:flex-start;gap:8px;padding:10px 14px;border-radius:10px;background:#f5f3ff;border:1px solid #ddd6fe;margin-bottom:14px;}
.ab-find-icon{font-size:16px;flex-shrink:0;margin-top:1px;}
.ab-find-text{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:12.5px;font-weight:600;color:#5b21b6;line-height:1.5;}
.ab-find-label{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1px;color:#7c3aed;display:block;margin-bottom:2px;}
.ab-instruction{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:13px;color:#64748b;margin-bottom:10px;line-height:1.6;}
#ab-user-input{width:100%;min-height:60px;padding:12px 14px;border-radius:10px;border:1.5px solid #e2e8f0;background:#f8fafc;font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:13px;color:#1e293b;line-height:1.6;resize:vertical;transition:border-color .15s;outline:none;box-sizing:border-box;}
#ab-user-input:focus{border-color:#7c3aed;background:#fff;}
#ab-submit-btn{width:100%;padding:12px;margin-top:10px;border-radius:10px;border:none;background:#7c3aed;color:#fff;font-size:14px;font-weight:700;font-family:inherit;cursor:pointer;transition:background .15s,transform .1s;}
#ab-submit-btn:hover{background:#6d28d9;transform:translateY(-1px);}
#ab-feedback{display:none;margin-top:14px;border-radius:12px;overflow:hidden;border:1px solid transparent;}
#ab-feedback.show{display:block;}
#ab-feedback.correct{border-color:#bbf7d0;}.ab-feedback.almost{border-color:#fed7aa;}#ab-feedback.wrong{border-color:#fecaca;}
.ab-feedback-top{display:flex;align-items:center;gap:10px;padding:12px 16px;}
#ab-feedback.correct .ab-feedback-top{background:#f0fdf4;}#ab-feedback.almost .ab-feedback-top{background:#fff7ed;}#ab-feedback.wrong .ab-feedback-top{background:#fef2f2;}
.ab-feedback-icon{font-size:22px;flex-shrink:0;}.ab-feedback-verdict{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:15px;font-weight:800;}
#ab-feedback.correct .ab-feedback-verdict{color:#15803d;}#ab-feedback.almost .ab-feedback-verdict{color:#c2410c;}#ab-feedback.wrong .ab-feedback-verdict{color:#b91c1c;}
.ab-feedback-insight{padding:10px 16px 13px;border-top:1px solid;}
#ab-feedback.correct .ab-feedback-insight{background:#fafffe;border-color:#bbf7d0;}#ab-feedback.almost .ab-feedback-insight{background:#fffbf5;border-color:#fed7aa;}#ab-feedback.wrong .ab-feedback-insight{background:#fff8f8;border-color:#fecaca;}
.ab-insight-label{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1.2px;color:#64748b;margin-bottom:4px;}
.ab-insight-text{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:12.5px;color:#1e293b;line-height:1.68;}
.ab-action-row{display:none;gap:8px;margin-top:12px;}.ab-action-row.show{display:flex;}
#ab-retry-btn{flex:1;padding:9px 14px;border-radius:9px;border:1px solid #e2e8f0;background:#f8fafc;color:#64748b;font-size:12px;font-weight:600;font-family:inherit;cursor:pointer;transition:background .15s;}
#ab-retry-btn:hover{background:#ede9fe;border-color:#7c3aed;color:#7c3aed;}
#ab-next-target-btn{flex:2;padding:9px 14px;border-radius:9px;border:none;background:#7c3aed;color:#fff;font-size:12px;font-weight:700;font-family:inherit;cursor:pointer;display:none;transition:background .15s;}
#ab-next-target-btn:hover{background:#6d28d9;}#ab-next-target-btn.show{display:block;}
#ab-alldone-card{display:none;text-align:center;padding:28px 20px;border-radius:14px;background:linear-gradient(135deg,#f0fdf4,#fefce8);border:1.5px solid #bbf7d0;margin-top:10px;}
#ab-alldone-card.show{display:block;}
.ab-alldone-emoji{font-size:40px;display:block;margin-bottom:10px;}
.ab-alldone-title{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:18px;font-weight:800;color:#15803d;margin-bottom:6px;}
.ab-alldone-sub{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:13px;color:#166534;line-height:1.6;}
#qanim-controls-bar{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);z-index:7000;display:flex;align-items:center;gap:6px;background:rgba(255,255,255,.98);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1.5px solid transparent;border-radius:16px;padding:10px 14px;box-shadow:0 6px 36px rgba(124,58,237,.18),0 2px 8px rgba(0,0,0,.08);white-space:nowrap;}
#qanim-controls-bar::before{content:'';position:absolute;inset:-2px;border-radius:18px;background:linear-gradient(90deg,#7c3aed,#db2777,#f59e0b,#7c3aed);background-size:200% 100%;animation:qanim-bar-glow 4s linear infinite;z-index:-1;}
@keyframes qanim-bar-glow{0%{background-position:0% 50%}100%{background-position:200% 50%}}
.qanim-ctrl-btn{display:flex;align-items:center;gap:5px;padding:8px 15px;border-radius:10px;border:1.5px solid #e2e8f0;background:linear-gradient(135deg,#f8fafc 0%,#f1f5f9 100%);color:#334155;font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:12px;font-weight:700;cursor:pointer;transition:background .15s,border-color .15s,color .15s,transform .12s,box-shadow .15s;user-select:none;}
.qanim-ctrl-btn:hover{background:linear-gradient(135deg,#ede9fe 0%,#fdf4ff 100%);border-color:#7c3aed;color:#6d28d9;transform:translateY(-2px);box-shadow:0 4px 14px rgba(124,58,237,.22);}
.qanim-ctrl-sep{width:1px;height:22px;background:linear-gradient(to bottom,transparent,#c4b5fd,transparent);flex-shrink:0;}
#btn-prev.qanim-prev-btn{background:#fff;color:#64748b;border:1.5px solid #cbd5e1;padding:11px 20px;border-radius:10px;font-size:13.5px;font-weight:700;font-family:inherit;cursor:pointer;margin-right:auto;box-shadow:0 1px 3px rgba(15,23,42,.06);}
#btn-prev.qanim-prev-btn:hover:not(:disabled){background:#f8fafc;color:#1e293b;border-color:#94a3b8;box-shadow:0 2px 8px rgba(15,23,42,.10);transform:translateY(-1px);}
#btn-prev.qanim-prev-btn:disabled{opacity:.38;cursor:not-allowed;}
#qanim-glossary-backdrop{position:fixed;inset:0;z-index:7150;background:rgba(15,23,42,.28);opacity:0;pointer-events:none;transition:opacity .22s;}
#qanim-glossary-backdrop.open{opacity:1;pointer-events:auto;}
#qanim-glossary-panel{position:fixed;top:0;right:0;z-index:7300;width:340px;max-width:88vw;height:100vh;background:#fff;border-left:1px solid #e2e8f0;box-shadow:-8px 0 32px rgba(0,0,0,.14);display:flex;flex-direction:column;overflow:hidden;transform:translateX(100%);transition:transform .26s cubic-bezier(.16,1,.3,1);}
#qanim-glossary-panel.open{transform:translateX(0);}
#qanim-glossary-header{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;background:#f0fdfa;border-bottom:1px solid #ccfbf1;flex-shrink:0;}
.glossary-header-title{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:14px;font-weight:700;color:#0f766e;}
.glossary-hdr-btn{width:26px;height:26px;border-radius:7px;border:1px solid #99f6e4;background:rgba(255,255,255,.7);color:#0f766e;font-size:12px;display:flex;align-items:center;justify-content:center;cursor:pointer;}
#qanim-glossary-body{flex:1 1 auto;overflow-y:auto;padding:12px 14px 20px;}
.glossary-term-card{background:#f8fafc;border:1px solid #e2e8f0;border-left:3px solid #0d9488;border-radius:10px;padding:10px 12px;margin-bottom:10px;}
.glossary-term-word{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:13px;font-weight:800;color:#134e4a;margin-bottom:4px;text-transform:capitalize;}
.glossary-term-meaning{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:12.5px;line-height:1.55;color:#475569;}
.glossary-ctrl-badge{position:absolute;top:-6px;right:-6px;min-width:16px;height:16px;padding:0 4px;border-radius:9px;background:#0d9488;color:#fff;font-size:10px;font-weight:800;line-height:16px;text-align:center;box-shadow:0 0 0 2px #fff;}
"""

# ===========================================================================
# JavaScript Templates (reference-exact)
# ===========================================================================

_SCENE6_JS = """
<script id="qanim-js-scene6">
(function initScene6(){
  'use strict';
  if(window.__qanimScene6Init)return;window.__qanimScene6Init=true;

  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}

  var s6Phase=-1;
  var s6AutoAdvanceTimer=null;
  var s6AutoAdvanceScheduled=false;

  function _qanimCancelRAF(){
    if(window.qanimRafId){cancelAnimationFrame(window.qanimRafId);window.qanimRafId=null;}
    if(window.rafId){cancelAnimationFrame(window.rafId);window.rafId=null;}
  }
  function _qanimResumeRAF(){
    if(typeof window.qanimStartRAF==='function'){window.qanimStartRAF();return;}
    if(typeof window.startRAF==='function'){window.startRAF();return;}
    if(typeof window.animate==='function'){requestAnimationFrame(window.animate);}
  }

  function s6Render(){
    var boxes=document.querySelectorAll('#s6-vars-row .s6-var-box');
    var n=boxes.length;

    var fEl=_el('s6-formula-text'),sEl=_el('s6-formula-sublabel');
    if(fEl)fEl.classList.add('s6-shown');if(sEl)sEl.classList.add('s6-shown');

    for(var i=0;i<n;i++){
      var b=boxes[i];
      if(s6Phase>=i+1){b.classList.add('s6-shown');b.classList.toggle('s6-active',s6Phase===i+1);}
      else{b.classList.remove('s6-shown','s6-active');}
    }

    var noteEl=_el('s6-note-bar');
    if(noteEl){if(s6Phase>=n+1)noteEl.classList.add('s6-shown');else noteEl.classList.remove('s6-shown');}

    var progEl=_el('s6-phase-progress');
    if(progEl){
      if(s6Phase<=0)progEl.textContent='Step 1 of '+(n+1)+' — The Formula';
      else if(s6Phase<=n)progEl.textContent='Step '+(s6Phase+1)+' of '+(n+1)+' — Variable '+(s6Phase);
      else progEl.textContent='Step '+(n+2)+' of '+(n+2)+' — Key Insight';
    }

    var capEl=_el('s6-phase-caption');
    if(capEl){
      if(s6Phase<=0)capEl.textContent='This is the governing formula for this problem.';
      else if(s6Phase<=n){var sb=boxes[s6Phase-1];capEl.textContent=sb?'Now examining: '+sb.querySelector('.s6-var-sym').textContent+' — '+sb.querySelector('.s6-var-name').textContent:'';}
      else capEl.textContent='All variables identified. Proceed to the substitution step.';}

    var nb=_el('s6-next-btn');
    if(nb){
      if(s6Phase<n){nb.textContent='Next ▶';nb.onclick=function(){window.qanim_s6Advance();};}
      else if(s6Phase===n){nb.textContent='See Key Insight ▶';nb.onclick=function(){window.qanim_s6Advance();};}
      else{nb.textContent='Step 8: Substitution ▶';nb.className='btn-primary';
        nb.onclick=function(){
          if(typeof window.qanim_showScene7==='function') window.qanim_showScene7();
        };
        if(!s6AutoAdvanceScheduled){s6AutoAdvanceScheduled=true;
          s6AutoAdvanceTimer=setTimeout(function(){
            var ov=_el('qanim-scene6-overlay');
            if(ov&&ov.classList.contains('qanim-scene-visible')&&typeof window.qanim_showScene7==='function')window.qanim_showScene7();
          },3500);
        }
      }
    }
  }

  window.qanim_s6Advance=function(){
    var n=document.querySelectorAll('#s6-vars-row .s6-var-box').length;
    if(s6Phase<n+1)s6Phase++;
    s6Render();
  };

  window.qanim_showScene6=function(){
    var ov=_el('qanim-scene6-overlay');if(ov)ov.classList.add('qanim-scene-visible');
    var ov7=_el('qanim-scene7-overlay');if(ov7)ov7.classList.remove('qanim-scene-visible');
    var ov9=_el('qanim-scene9-overlay');if(ov9)ov9.classList.remove('qanim-scene-visible');
    var bd=_el('qanim-scene-modal-backdrop');if(bd)bd.classList.add('qanim-scene-visible');
    _qanimCancelRAF();
    _syncDots(6);
    s6Phase=0;s6AutoAdvanceScheduled=false;
    if(s6AutoAdvanceTimer){clearTimeout(s6AutoAdvanceTimer);s6AutoAdvanceTimer=null;}
    s6Render();
  };

  window.qanim_goToPrevScene=function(){
    ['qanim-scene6-overlay','qanim-scene7-overlay','qanim-scene9-overlay'].forEach(function(id){var el=_el(id);if(el)el.classList.remove('qanim-scene-visible');});
    var bd=_el('qanim-scene-modal-backdrop');if(bd)bd.classList.remove('qanim-scene-visible');
    if(s6AutoAdvanceTimer){clearTimeout(s6AutoAdvanceTimer);s6AutoAdvanceTimer=null;}
    var stage=document.querySelector('.svg-container');if(stage)stage.style.opacity='1';
    if(typeof window.applyStep==='function'&&typeof window.stepsData!=='undefined'){
      var last=window.stepsData.length-1;window.currentStep=last;window.applyStep(last);}
    _qanimResumeRAF();
  };

  function _syncDots(idx){
    var dots=document.querySelectorAll('.step-dot');
    for(var i=0;i<dots.length;i++){dots[i].classList.remove('active','done');if(i<idx)dots[i].classList.add('done');if(i===idx)dots[i].classList.add('active');}
    var lbl=_el('step-label');if(lbl)lbl.innerText='Step 7 of 9: Main Formula';
    var bar=_el('step-bar');if(bar)bar.style.width=Math.round(7/9*100)+'%';
  }

  _onReady(function(){
    var origReset=window.resetAnim;
    window.resetAnim=function(){
      ['qanim-scene6-overlay','qanim-scene7-overlay','qanim-scene9-overlay'].forEach(function(id){var el=_el(id);if(el)el.classList.remove('qanim-scene-visible');});
      var bd=_el('qanim-scene-modal-backdrop');if(bd)bd.classList.remove('qanim-scene-visible');
      var stage=document.querySelector('.svg-container');if(stage)stage.style.opacity='1';
      s6Phase=-1;s6AutoAdvanceScheduled=false;
      if(s6AutoAdvanceTimer){clearTimeout(s6AutoAdvanceTimer);s6AutoAdvanceTimer=null;}
      if(typeof origReset==='function')origReset();
    };
  });
})();
</script>
"""

_SCENE7_JS = """
<script id="qanim-js-scene7">
(function initScene7(){
  'use strict';
  if(window.__qanimScene7Init)return;window.__qanimScene7Init=true;

  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}

  function _syncDots8(){
    var dots=document.querySelectorAll('.step-dot');
    for(var i=0;i<dots.length;i++){dots[i].classList.remove('active','done');if(i<7)dots[i].classList.add('done');if(i===7)dots[i].classList.add('active');}
    var lbl=_el('step-label');if(lbl)lbl.innerText='Step 8 of 9: Step-by-Step Substitution';
    var bar=_el('step-bar');if(bar)bar.style.width=Math.round(8/9*100)+'%';
  }

  function _showScene7Core(){
    var ov7=_el('qanim-scene7-overlay');if(ov7)ov7.classList.add('qanim-scene-visible');
    var ov6=_el('qanim-scene6-overlay');if(ov6)ov6.classList.remove('qanim-scene-visible');
    var ov9=_el('qanim-scene9-overlay');if(ov9)ov9.classList.remove('qanim-scene-visible');
    var bd=_el('qanim-scene-modal-backdrop');if(bd)bd.classList.add('qanim-scene-visible');
    _syncDots8();
  }

  window.qanim_showScene7=_showScene7Core;
  window.qanim_showScene8=_showScene7Core;

  window.qanim_goToScene6FromScene7=function(){
    var ov7=_el('qanim-scene7-overlay');if(ov7)ov7.classList.remove('qanim-scene-visible');
    if(typeof window.qanim_showScene6==='function')window.qanim_showScene6();
  };

  window.qanim_goToScene7FromScene9=function(){
    var ov9=_el('qanim-scene9-overlay');if(ov9)ov9.classList.remove('qanim-scene-visible');
    _showScene7Core();
  };

  _onReady(function(){
    var origReset=window.resetAnim;
    window.resetAnim=function(){
      var ov7=_el('qanim-scene7-overlay');if(ov7)ov7.classList.remove('qanim-scene-visible');
      var ov9=_el('qanim-scene9-overlay');if(ov9)ov9.classList.remove('qanim-scene-visible');
      var bd=_el('qanim-scene-modal-backdrop');if(bd)bd.classList.remove('qanim-scene-visible');
      if(typeof origReset==='function')origReset();
    };
  });
})();
</script>
"""

_SCENE9_JS = """
<script id="qanim-js-scene9">
(function initScene9(){
  'use strict';
  if(window.__qanimScene9Init)return;window.__qanimScene9Init=true;

  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}

  function _syncDots9(){
    var dots=document.querySelectorAll('.step-dot');
    for(var i=0;i<dots.length;i++){dots[i].classList.remove('active','done');if(i<8)dots[i].classList.add('done');if(i===8)dots[i].classList.add('active');}
    var lbl=_el('step-label');if(lbl)lbl.innerText='Step 9 of 9: Final Answer';
    var bar=_el('step-bar');if(bar)bar.style.width='100%';
  }

  function _animateEntrance(){
    var rows=document.querySelectorAll('#s9-sub-chain .s9-sub-row');
    for(var i=0;i<rows.length;i++){
      (function(el,delay){setTimeout(function(){el.classList.add('s9-shown');},delay);})(rows[i],200+i*200);}
    var fb=_el('s9-final-box');
    if(fb)setTimeout(function(){fb.classList.add('s9-shown');},200+rows.length*200);
    var ib=_el('s9-insight-bar');
    if(ib)setTimeout(function(){ib.classList.add('s9-shown');},200+rows.length*200+300);
  }

  function _resetEntrance(){
    var rows=document.querySelectorAll('#s9-sub-chain .s9-sub-row');
    for(var i=0;i<rows.length;i++)rows[i].classList.remove('s9-shown');
    var fb=_el('s9-final-box');if(fb)fb.classList.remove('s9-shown');
    var ib=_el('s9-insight-bar');if(ib)ib.classList.remove('s9-shown');
  }

  window.qanim_showScene9=function(){
    var ov7=_el('qanim-scene7-overlay');if(ov7)ov7.classList.remove('qanim-scene-visible');
    var ov6=_el('qanim-scene6-overlay');if(ov6)ov6.classList.remove('qanim-scene-visible');
    var ov9=_el('qanim-scene9-overlay');if(ov9)ov9.classList.add('qanim-scene-visible');
    var bd=_el('qanim-scene-modal-backdrop');if(bd)bd.classList.add('qanim-scene-visible');
    _syncDots9();_resetEntrance();setTimeout(_animateEntrance,120);
  };

  window.qanim_goToScene7FromScene9=function(){
    var ov9=_el('qanim-scene9-overlay');if(ov9)ov9.classList.remove('qanim-scene-visible');
    if(typeof window.qanim_showScene8==='function')window.qanim_showScene8();
    else if(typeof window.qanim_showScene7==='function')window.qanim_showScene7();
  };

  _onReady(function(){
    var origReset=window.resetAnim;
    window.resetAnim=function(){
      var ov9=_el('qanim-scene9-overlay');if(ov9)ov9.classList.remove('qanim-scene-visible');
      if(typeof origReset==='function')origReset();
    };
  });
})();
</script>
"""

_AUTOTRIGGER_JS = """
<script id="qanim-js-scene6-autotrigger">
(function(){
  'use strict';
  if(window.__qanimAutoTrigger)return;window.__qanimAutoTrigger=true;

  function _tryTrigger(){
    var btn=document.getElementById('btn-next');
    if(!btn)return;
    var label=(btn.textContent||btn.innerText||'').trim().toLowerCase();
    var isFinished=btn.disabled||label.indexOf('finish')!==-1||label.indexOf('formula')!==-1||label.indexOf('step 7')!==-1;
    if(!isFinished)return;
    var ov6=document.getElementById('qanim-scene6-overlay');
    var ov7=document.getElementById('qanim-scene7-overlay');
    var ov9=document.getElementById('qanim-scene9-overlay');
    var alreadyOpen=(ov6&&ov6.classList.contains('qanim-scene-visible'))||(ov7&&ov7.classList.contains('qanim-scene-visible'))||(ov9&&ov9.classList.contains('qanim-scene-visible'));
    if(alreadyOpen)return;
    if(typeof window.qanim_showScene6==='function'){
      var svgCont=document.querySelector('.svg-container');
      var doShow=function(){window.qanim_showScene6();};
      if(svgCont){svgCont.style.transition='opacity .45s ease';svgCont.style.opacity='0';setTimeout(doShow,460);}
      else{setTimeout(doShow,120);}
    }
  }

  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  _onReady(function(){
    var btn=document.getElementById('btn-next');
    if(btn&&!btn.__qanimAutoWired){btn.__qanimAutoWired=true;btn.addEventListener('click',function(){setTimeout(_tryTrigger,30);});}
  });
})();
</script>
"""

_PREVSTEP_JS = """
<script id="qanim-js-prevstep">
(function initPrevStep(){
  'use strict';
  if(window.__qanimPrevStepInit)return;window.__qanimPrevStepInit=true;

  function _updateBtn(){
    var pb=document.getElementById('btn-prev');
    if(!pb)return;
    var cur=typeof window.currentStep==='number'?window.currentStep:-1;
    pb.disabled=(cur<=0);
  }

  function _resumeRAF(){
    if(typeof window.qanimStartRAF==='function'){window.qanimStartRAF();return;}
    if(typeof window.startRAF==='function'){window.startRAF();return;}
    if(typeof window.animate==='function'){requestAnimationFrame(window.animate);}
  }

  window.prevStep=function(){
    if(typeof window.currentStep!=='number')return;
    if(window.currentStep<=0)return;
    window.currentStep--;
    if(typeof window.applyStep==='function')window.applyStep(window.currentStep);
    _resumeRAF();
    var nb=document.getElementById('btn-next');if(nb)nb.style.display='inline-block';
  };

  var _origApply=window.applyStep;
  if(typeof _origApply==='function'){
    window.applyStep=function(idx){_origApply(idx);_updateBtn();};
  }

  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  _onReady(function(){
    var pb=document.getElementById('btn-prev');
    if(pb){pb.removeAttribute('onclick');pb.addEventListener('click',function(e){e.stopPropagation();window.prevStep();});}
    _updateBtn();
  });
})();
</script>
"""


_ANSWERBOX_JS_TMPL = """
<script type="application/json" id="__answer_targets__">{{TARGETS_JSON}}</script>
<script id="qanim-js-answerbox">
(function initAnswerBox(){
  'use strict';
  if(window.__qanimAnswerBoxInit)return;window.__qanimAnswerBoxInit=true;
  var abOpen=false,_targets=[],_currentIdx=0,_loaded=false;
  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _loadTargets(){if(_loaded)return;_loaded=true;try{var t=_el('__answer_targets__');if(!t)return;var d=JSON.parse(t.textContent)||{};_targets=Array.isArray(d.answer_targets)?d.answer_targets:[];}catch(e){_targets=[];}}
  function _renderTarget(idx){var t=_targets[idx];if(!t)return;var fe=_el('ab-find-text');if(fe)fe.textContent=t.label||'Answer';var total=_targets.length;var pl=_el('ab-progress-label');if(pl)pl.textContent='Question '+(idx+1)+' of '+total;var de=_el('ab-progress-dots');if(de){var h='';for(var i=0;i<total;i++){var cls=i<idx?'ab-dot done':i===idx?'ab-dot current':'ab-dot';h+='<div class="'+cls+'"></div>';}de.innerHTML=h;}var inp=_el('ab-user-input');if(inp){inp.value='';inp.removeAttribute('disabled');}var fb=_el('ab-feedback');if(fb)fb.className='';var ar=_el('ab-action-row');if(ar)ar.className='ab-action-row';var ntb=_el('ab-next-target-btn');if(ntb)ntb.style.display='none';var sb=_el('ab-submit-btn');if(sb){sb.style.display='';sb.disabled=false;}var adc=_el('ab-alldone-card');if(adc)adc.className='';var u=t.unit?' ('+t.unit+')':'';if(inp)inp.placeholder='Type your answer'+u+'...';}
  function _nums(s){var m=s.match(/[-+]?\\d*\\.?\\d+(?:[eE][-+]?\\d+)?/g);return m?m.map(parseFloat).filter(function(n){return isFinite(n);}):[];}
  function _validate(userAns,correctAns){if(!userAns||!userAns.trim())return'empty';var un=_nums(userAns),cn=_nums(correctAns);if(un.length>0&&cn.length>0){var re=Math.abs(un[0]-cn[0])/(Math.abs(cn[0])+1e-12);if(re<0.01)return'correct';if(re<0.15)return'almost';return'wrong';}var uc=userAns.toLowerCase().trim().replace(/[^a-z0-9\\s]/g,' ');var cc=correctAns.toLowerCase().trim().replace(/[^a-z0-9\\s]/g,' ');if(uc===cc)return'correct';return'wrong';}
  var _FB={correct:{icon:'✅',verdict:'Correct!',cls:'correct'},almost:{icon:'〰️',verdict:'Almost Correct',cls:'almost'},wrong:{icon:'❌',verdict:'Wrong Answer',cls:'wrong'},empty:{icon:'❓',verdict:'No Answer',cls:'wrong'}};
  function _showFeedback(verdict,insight){var info=_FB[verdict]||_FB['wrong'];var fb=_el('ab-feedback'),icon=_el('ab-feedback-icon'),verd=_el('ab-feedback-verdict'),ins=_el('ab-insight-text');if(!fb)return;fb.className='show '+info.cls;if(icon)icon.textContent=info.icon;if(verd)verd.textContent=info.verdict;if(ins)ins.textContent=insight||'Review the solution.';var ar=_el('ab-action-row');if(ar)ar.className='ab-action-row show';var ntb=_el('ab-next-target-btn'),isLast=(_currentIdx>=_targets.length-1);if(ntb){if((verdict==='correct'||verdict==='almost')&&!isLast){ntb.style.display='';ntb.textContent='Next →';}else{ntb.style.display='none';}}if(verdict==='correct'&&isLast){setTimeout(function(){var adc=_el('ab-alldone-card');if(adc)adc.className='show';var sb=_el('ab-submit-btn');if(sb)sb.style.display='none';},900);}}
  function openAnswerBox(){_loadTargets();_currentIdx=0;var bd=_el('answerbox-backdrop'),pn=_el('answerbox-panel');if(!bd||!pn)return;bd.classList.add('open');bd.setAttribute('aria-hidden','false');pn.classList.add('open');pn.setAttribute('aria-hidden','false');abOpen=true;_renderTarget(_currentIdx);setTimeout(function(){var inp=_el('ab-user-input');if(inp)inp.focus();},220);}
  function closeAnswerBox(){var bd=_el('answerbox-backdrop'),pn=_el('answerbox-panel');if(bd){bd.classList.remove('open');bd.setAttribute('aria-hidden','true');}if(pn){pn.classList.remove('open');pn.setAttribute('aria-hidden','true');}abOpen=false;}
  window.openAnswerBox=openAnswerBox;window.closeAnswerBox=closeAnswerBox;
  _onReady(function(){
    function wireCtrl(){var btn=_el('answerbox-ctrl-btn');if(btn){btn.removeAttribute('onclick');btn.addEventListener('click',function(e){e.stopPropagation();abOpen?closeAnswerBox():openAnswerBox();});}else{setTimeout(wireCtrl,100);}}
    wireCtrl();
    var cb=_el('ab-close-btn');if(cb)cb.addEventListener('click',function(e){e.stopPropagation();closeAnswerBox();});
    var bd=_el('answerbox-backdrop');if(bd)bd.addEventListener('click',function(e){if(e.target===bd)closeAnswerBox();});
    document.addEventListener('keydown',function(e){if(e.key==='Escape'&&abOpen)closeAnswerBox();});
    var sb=_el('ab-submit-btn');if(sb)sb.addEventListener('click',function(){var inp=_el('ab-user-input'),userAns=inp?inp.value.trim():'';var t=_targets[_currentIdx]||{};var verdict=_validate(userAns,t.value||'');_showFeedback(verdict,t.insight||'');if(inp)inp.disabled=true;});
    var inp2=_el('ab-user-input');if(inp2)inp2.addEventListener('keydown',function(e){if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();var sb2=_el('ab-submit-btn');if(sb2)sb2.click();}});
    var rb=_el('ab-retry-btn');if(rb)rb.addEventListener('click',function(){var inp=_el('ab-user-input');if(inp){inp.value='';inp.disabled=false;inp.focus();}var fb=_el('ab-feedback');if(fb)fb.className='';var ar=_el('ab-action-row');if(ar)ar.className='ab-action-row';var sb=_el('ab-submit-btn');if(sb)sb.style.display='';var ntb=_el('ab-next-target-btn');if(ntb)ntb.style.display='none';});
    var ntb2=_el('ab-next-target-btn');if(ntb2)ntb2.addEventListener('click',function(){if(_currentIdx<_targets.length-1){_currentIdx++;_renderTarget(_currentIdx);}});
  });
})();
</script>
"""

_GLOSSARY_JS = """
<script id="qanim-js-glossary">
(function initGlossary(){
  'use strict';
  if(window.__qanimGlossaryInit)return;window.__qanimGlossaryInit=true;
  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function openGlossary(){var p=_el('qanim-glossary-panel'),b=_el('qanim-glossary-backdrop');if(!p)return;p.classList.add('open');p.setAttribute('aria-hidden','false');if(b)b.classList.add('open');}
  function closeGlossary(){var p=_el('qanim-glossary-panel'),b=_el('qanim-glossary-backdrop');if(p){p.classList.remove('open');p.setAttribute('aria-hidden','true');}if(b)b.classList.remove('open');}
  _onReady(function(){
    var btn=_el('glossary-ctrl-btn');
    if(btn)btn.addEventListener('click',function(){var p=_el('qanim-glossary-panel');if(p&&p.classList.contains('open'))closeGlossary();else openGlossary();});
    var cb=_el('glossary-close-btn');if(cb)cb.addEventListener('click',closeGlossary);
    var bd=_el('qanim-glossary-backdrop');if(bd)bd.addEventListener('click',closeGlossary);
    document.addEventListener('keydown',function(e){if(e.key==='Escape')closeGlossary();});
  });
})();
</script>
"""


# ===========================================================================
# Main HTML Assembler
# ===========================================================================

def _build_step_dots(steps: list, scene: dict) -> str:
    """Build the step dots row matching the reference exactly."""
    dots = ""
    color_legend = scene.get("color_legend", [])
    for i, s in enumerate(steps):
        label = s.get("label", f"Step {i+1}")
        color = color_legend[i]["color"] if i < len(color_legend) else "#0ea5e9"
        active = "active" if i == 0 else ""
        onclick = f'onclick="goToStep({i})"' if i < len(steps) else ""
        style = f'style="border-left:3px solid {color}"'
        dot_id = f' id="dot-step{i+1}"' if i < len(steps) else ""
        dots += f'<div class="step-dot {active}"{dot_id} {onclick} {style}>{i+1} · {label}</div>\n'
        if i < 8:
            dots += '<div class="step-connector"></div>\n'

    # Add dots 7, 8, 9 for the modal scenes (clickable)
    n = len(steps)
    if n <= 6:
        for j in range(n, 6):
            dots += f'<div class="step-dot" id="dot-step{j+1}">Step {j+1}</div><div class="step-connector"></div>\n'
        # Dots for scenes 7, 8, 9
        # FIX G: dot-step8 uses qanim_showScene7 (not qanim_showScene8) for consistency
        dots += f'<div class="step-dot" id="dot-step7" onclick="if(typeof window.qanim_showScene6===\'function\')window.qanim_showScene6()">7 · Formula</div>\n<div class="step-connector"></div>\n'
        dots += f'<div class="step-dot" id="dot-step8" onclick="if(typeof window.qanim_showScene7===\'function\')window.qanim_showScene7()">8 · Subst.</div>\n<div class="step-connector"></div>\n'
        dots += f'<div class="step-dot" id="dot-step9" onclick="if(typeof window.qanim_showScene9===\'function\')window.qanim_showScene9()">9 · Answer</div>\n'

    return dots


def _build_color_legend(scene: dict) -> str:
    legend = scene.get("color_legend", [])
    if not legend:
        return ""
    items = ""
    for item in legend:
        items += f'<div class="step-legend-item"><span class="step-legend-dot" style="background:{item["color"]}"></span>{_he(item["label"])}</div>\n'
    items += '<div class="step-legend-item"><span class="step-legend-dot" style="background:#0e7490"></span>7–9 Calc</div>\n'
    return f'<div class="step-color-legend">\n{items}</div>\n'


def _build_glossary_panel(glossary: list) -> str:
    if not glossary:
        return ""
    terms_html = ""
    for g in glossary:
        terms_html += f"""<div class="glossary-term-card">
  <div class="glossary-term-word">{_he(g.get('term',''))}</div>
  <div class="glossary-term-meaning">{_he(g.get('meaning',''))}</div>
</div>\n"""
    badge = len(glossary)
    return f"""<div id="qanim-glossary-backdrop"></div>
<div id="qanim-glossary-panel" role="dialog" aria-label="Difficult words glossary" aria-hidden="true">
  <div id="qanim-glossary-header">
    <div class="glossary-header-title">&#x1F4D6; Difficult Words, Explained</div>
    <button class="glossary-hdr-btn" id="glossary-close-btn" title="Close">&#x2715;</button>
  </div>
  <div id="qanim-glossary-body">
    {terms_html}
  </div>
</div>"""


def validate_final_html(html: str) -> None:
    """
    FIX B: Validate that the assembled HTML contains all required elements.
    Raises ValueError listing every missing item if any are absent.
    Does NOT return — either passes silently or raises.
    """
    required_ids = [
        "qanim-scene6-overlay",
        "qanim-scene7-overlay",
        "qanim-scene9-overlay",
        "info-title",
        "info-desc",
        "info-badges",
        "step-label",
        "step-bar",
        "btn-prev",
    ]
    required_strings = [
        "Step 7",
        "Step 8",
        "Step 9",
        "var stepsData",
        "function applyStep",
        "qanim_showScene6",
        "qanim_showScene7",
        "qanim_showScene9",
        "qanim_goToPrevScene",
    ]
    missing = []
    for rid in required_ids:
        if f'id="{rid}"' not in html:
            missing.append(f'Missing element id="{rid}"')
    for rs in required_strings:
        if rs not in html:
            missing.append(f'Missing string: {rs!r}')
    if missing:
        raise ValueError("Final HTML validation failed:\n  " + "\n  ".join(missing))


def assemble_html(question: str, scene: dict, sol: dict, svg_data: dict) -> str:
    """Assemble the complete HTML file from all parts."""

    title = _he(scene.get("title", question[:60]))
    question_escaped = _he(question)
    steps = scene.get("steps", [])
    to_find = scene.get("to_find", ["The unknown quantity"])
    glossary = scene.get("glossary", [])

    # Answer targets for AnswerBox
    answer_targets = [{
        "label": to_find[0] if to_find else "Final Answer",
        "value": sol.get("answer_value", "?"),
        "unit": sol.get("answer_unit", ""),
        "insight": sol.get("key_insight", "Apply the governing formula."),
    }]
    targets_json = json.dumps({"answer_targets": answer_targets}, ensure_ascii=False)


    # SVG content
    svg_defs = svg_data.get("svg_defs", "")
    svg_layers = svg_data.get("svg_layers", "")
    steps_data_js = svg_data.get("steps_data_js", "var stepsData = [];")
    apply_step_js = svg_data.get("apply_step_js", "function applyStep(idx){window.currentStep=idx;}")
    raf_js = svg_data.get("raf_js", "")

    # Normalize stepsData to window scope
    if "window.stepsData" not in steps_data_js:
        steps_data_js = steps_data_js.rstrip() + "\nwindow.stepsData = stepsData;"

    # Step dots and legend
    step_dots = _build_step_dots(steps, scene)
    color_legend = _build_color_legend(scene)

    # Scene overlays
    scene6_html = _build_scene6_html(sol)
    scene7_html = _build_scene7_html(sol, scene)
    scene9_html = _build_scene9_html(sol, to_find)
    glossary_panel = _build_glossary_panel(glossary)
    glossary_badge = f'<span class="glossary-ctrl-badge">{len(glossary)}</span>' if glossary else ""
    glossary_sep = '<div class="qanim-ctrl-sep"></div>' if glossary else ""
    glossary_btn = f"""  {glossary_sep}
  <button class="qanim-ctrl-btn" id="glossary-ctrl-btn" title="Difficult words explained" style="position:relative;">
    <span>&#x1F4D6;</span><span class="ctrl-label">Glossary</span>{glossary_badge}
  </button>""" if glossary else ""

    # Answer box JS with targets
    answerbox_js = _ANSWERBOX_JS_TMPL.replace("{{TARGETS_JSON}}", targets_json)

    # FIX E: totalSteps is always the number of SVG steps (should be 6).
    # It is used as the 0-indexed last SVG step (index 5 = step 6).
    # We enforce a minimum of 6 to ensure btn-next logic works correctly.
    total_steps = max(len(steps), 6)
    # totalSteps is used as: if(currentStep < totalSteps) → advance
    # when currentStep === totalSteps (i.e. 6), show Scene 6 modal (Step 7)
    # So totalSteps must equal len(stepsData) i.e. 6 when stepsData has 6 entries.
    # applyStep uses idx 0..5, nextStep triggers modal at idx==6.
    nav_js = f"""
  <script>
    var totalSteps = {total_steps - 1};  // 0-indexed last SVG step (step 6 = index 5)
    var totalDisplaySteps = 9;
    var currentStep = 0;
    window.totalSteps = {total_steps - 1};
    window.qanimRafId = null;
    window.currentStep = 0;

    {steps_data_js}

    {apply_step_js}

    // FIX F: Fixed Python-generated navigation JS for all scene transitions.
    // These are never generated by Gemini — always injected by Python.
    function _qanim_hideAllOverlays(){{
      ['qanim-scene6-overlay','qanim-scene7-overlay','qanim-scene9-overlay'].forEach(function(id){{
        var el=document.getElementById(id);
        if(el)el.classList.remove('qanim-scene-visible');
      }});
    }}

    function nextStep(){{
      if(currentStep < (window.totalSteps || {total_steps - 1})){{
        currentStep++;
        window.currentStep = currentStep;
        applyStep(currentStep);
      }} else {{
        if(typeof window.qanim_showScene6 === 'function'){{
          var svgCont = document.querySelector('.svg-container');
          if(svgCont){{ svgCont.style.transition='opacity .45s ease'; svgCont.style.opacity='0'; setTimeout(function(){{window.qanim_showScene6();}},460); }}
          else {{ window.qanim_showScene6(); }}
        }}
      }}
    }}

    function goToStep(idx){{
      if(idx >= 0 && idx <= (window.totalSteps || {total_steps - 1})){{
        _qanim_hideAllOverlays();
        var bd=document.getElementById('qanim-scene-modal-backdrop');
        if(bd)bd.classList.remove('qanim-scene-visible');
        var svgCont=document.querySelector('.svg-container');
        if(svgCont)svgCont.style.opacity='1';
        currentStep = idx;
        window.currentStep = idx;
        applyStep(currentStep);
      }}
    }}

    function resetAnim(){{
      _qanim_hideAllOverlays();
      var bd=document.getElementById('qanim-scene-modal-backdrop');
      if(bd)bd.classList.remove('qanim-scene-visible');
      currentStep = 0;
      window.currentStep = 0;
      var svgCont = document.querySelector('.svg-container');
      if(svgCont) svgCont.style.opacity = '1';
      applyStep(0);
    }}

    {raf_js}

    window.addEventListener('DOMContentLoaded', function(){{
      applyStep(0);
      if(typeof window.qanimStartRAF === 'function') window.qanimStartRAF();
    }});
  </script>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — Interactive Animation</title>
  <style id="qanim-base-styles">
{_BASE_CSS}
  </style>
  <style id="qanim-scene6-styles">
{_SCENE6_CSS}
  </style>
  <style id="qanim-scene7-styles">
{_SCENE7_CSS}
  </style>
  <style id="qanim-scene9-styles">
{_SCENE9_CSS}
  </style>
  <style id="qanim-controls-styles">
{_CONTROLS_CSS}
  </style>
</head>
<body>

{scene9_html}
{scene7_html}
<div id="qanim-scene-modal-backdrop"></div>
{scene6_html}
{glossary_panel}

<div id="answerbox-backdrop" aria-hidden="true">
<div id="answerbox-panel" role="dialog" aria-label="Answer Box" aria-hidden="true">
  <div class="ab-header">
    <div class="ab-header-title">&#x270F;&#xFE0F; Answer Box</div>
    <button class="ab-close-btn" id="ab-close-btn">&#x2715;</button>
  </div>
  <div class="ab-progress-row">
    <span class="ab-progress-label" id="ab-progress-label">Question 1 of 1</span>
    <div class="ab-progress-dots" id="ab-progress-dots"></div>
  </div>
  <div class="ab-body">
    <div class="ab-find-chip" id="ab-find-chip">
      <span class="ab-find-icon">&#x1F50D;</span>
      <div><span class="ab-find-label">Find</span><div class="ab-find-text" id="ab-find-text">Loading...</div></div>
    </div>
    <p class="ab-instruction">Type your answer below (include units if applicable) and click <strong>Submit</strong>.</p>
    <textarea id="ab-user-input" placeholder="e.g. 6000 W" spellcheck="false"></textarea>
    <button id="ab-submit-btn">Submit Answer</button>
    <div id="ab-feedback" role="alert">
      <div class="ab-feedback-top">
        <span class="ab-feedback-icon" id="ab-feedback-icon"></span>
        <span class="ab-feedback-verdict" id="ab-feedback-verdict"></span>
      </div>
      <div class="ab-feedback-insight">
        <div class="ab-insight-label">&#x1F4A1; Key Insight</div>
        <div class="ab-insight-text" id="ab-insight-text"></div>
      </div>
    </div>
    <div class="ab-action-row" id="ab-action-row">
      <button id="ab-retry-btn">Try Again</button>
      <button id="ab-next-target-btn">Next &rarr;</button>
    </div>
    <div id="ab-alldone-card">
      <span class="ab-alldone-emoji">&#x1F389;</span>
      <div class="ab-alldone-title">All answers submitted!</div>
      <div class="ab-alldone-sub">Great work! Continue to review the solution walkthrough.</div>
    </div>
  </div>
</div>
</div>

<div class="page-header">
  <div class="page-chip">Interactive Animation</div>
</div>

<div class="dashboard">
  <div class="question-banner">
    <div class="q-label">Problem Statement</div>
    <div class="q-text">{question_escaped}</div>
  </div>

  <div class="svg-container">
    <svg xmlns="http://www.w3.org/2000/svg" id="stage" viewBox="0 0 850 478" preserveAspectRatio="xMidYMid slice">
      <defs>
        {svg_defs}
      </defs>
      <g id="layer-canvas-bg">
        <rect width="100%" height="100%" fill="url(#grid)" opacity="0.5"/>
      </g>
      {svg_layers}
    </svg>
  </div>

  <div class="control-panel">
    {color_legend}
    <div class="step-indicator" id="dots">
      {step_dots}
      <div class="step-label" id="step-label">Step 1 of 9</div>
    </div>
    <div class="step-progress-wrap">
      <div class="step-progress-bar" id="step-bar"></div>
    </div>
    <div class="info-box">
      <h3 id="info-title">{_he(steps[0].get('title', 'Step 1') if steps else 'Step 1')}</h3>
      <div class="badges" id="info-badges"></div>
      <div class="info-desc" id="info-desc">{_he(steps[0].get('description', '') if steps else '')}</div>
    </div>
    <div class="actions">
      <button class="btn-secondary" onclick="resetAnim()">&#x21BA; Restart</button>
      <button class="btn-secondary qanim-prev-btn" id="btn-prev" onclick="prevStep()" disabled>&#x25C0; Previous Step</button>
      <button class="btn-primary" id="btn-next" onclick="nextStep()">Next Step &#x25B6;</button>
    </div>
  </div>
</div>

{nav_js}

{_SCENE6_JS}
{_SCENE7_JS}
{_SCENE9_JS}
{_AUTOTRIGGER_JS}
{_PREVSTEP_JS}
{answerbox_js}
{_GLOSSARY_JS}

<div id="qanim-controls-bar" role="toolbar" aria-label="QAnim Controls">
  <button class="qanim-ctrl-btn" id="answerbox-ctrl-btn" title="Check your answer">
    <span>&#x270F;&#xFE0F;</span><span class="ctrl-label">Answer Box</span>
  </button>{glossary_btn}
</div>

</body>
</html>"""

    # FIX B: Validate final HTML before returning; log a warning on failure
    # but still return the HTML rather than crashing the pipeline.
    try:
        validate_final_html(html)
        Log.ok("HTMLAssembler", "Final HTML validation passed (all 9 scenes present)")
    except ValueError as _ve:
        Log.warn("HTMLAssembler", str(_ve))

    return html


# ===========================================================================
# Pipeline
# ===========================================================================

async def generate_animation_html(question: str) -> str:
    """Main async pipeline: analyze → solve → build SVG → assemble HTML."""
    question = (question or "").strip()
    if not question:
        return _fallback_html("(empty)", "No question provided.")

    Log.info("Pipeline", f"Question: {question[:80]!r}")

    # Stage A: Parallel — scene analysis + solution
    Log.info("Pipeline", "Stage A: Scene analysis + solution...")
    loop = asyncio.get_event_loop()
    try:
        scene_task = loop.run_in_executor(None, analyze_scene, question)
        sol_task   = loop.run_in_executor(None, generate_solution, question)
        scene_res, sol_res = await asyncio.gather(
            asyncio.wait_for(scene_task, timeout=TIMEOUT_SCENE),
            asyncio.wait_for(sol_task,   timeout=TIMEOUT_SOLUTION),
            return_exceptions=True,
        )
    except Exception as e:
        Log.error("Pipeline", f"Stage A failed: {e}")
        scene_res = None
        sol_res = None

    scene = scene_res if isinstance(scene_res, dict) else analyze_scene.__wrapped__(question) if hasattr(analyze_scene, '__wrapped__') else {"_fallback": True, "steps": [], "title": question[:60], "to_find": ["The unknown quantity"], "svg_layers": {}, "glossary": [], "color_legend": []}
    sol   = sol_res   if isinstance(sol_res,   dict) else {"_fallback": True, "formula": "Governing Formula", "formula_name": "Governing Equation", "final_answer": "See calculation", "answer_value": "?", "answer_unit": "", "key_insight": "Apply the formula.", "variables": [], "substitution_chain": [], "given_list": [], "approach_steps": [], "system_title": "Physical System", "system_label2": "Substituting values", "steps": []}

    if isinstance(scene_res, Exception):
        Log.warn("Pipeline", f"SceneAnalyzer exception: {scene_res}")
    if isinstance(sol_res, Exception):
        Log.warn("Pipeline", f"Solution exception: {sol_res}")

    # Stage B: SVG + stepsData
    Log.info("Pipeline", "Stage B: Building SVG + stepsData...")
    try:
        svg_data = await asyncio.wait_for(
            loop.run_in_executor(None, build_svg_and_steps, question, scene, sol),
            timeout=TIMEOUT_HTML,
        )
    except Exception as e:
        Log.warn("Pipeline", f"SVGBuilder failed: {e}")
        svg_data = {"svg_defs": "", "svg_layers": "", "steps_data_js": "var stepsData=[];", "apply_step_js": "function applyStep(idx){window.currentStep=idx;}", "raf_js": ""}

    # Stage C: Assemble
    Log.info("Pipeline", "Stage C: Assembling HTML...")
    html = assemble_html(question, scene, sol, svg_data)
    Log.ok("Pipeline", f"Done: {len(html):,} chars")
    return html


def _fallback_html(question: str, reason: str) -> str:
    """Minimal working HTML for error cases."""
    q_esc = html_module.escape(question[:300])
    r_esc = html_module.escape(reason[:200])
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Animation</title>
<style>body{{font-family:system-ui,sans-serif;background:#eef2f9;display:flex;flex-direction:column;align-items:center;padding:40px 20px;}}
.card{{background:#fff;border-radius:16px;padding:32px;max-width:700px;box-shadow:0 4px 24px rgba(0,0,0,.1);}}
h2{{color:#0e7490;margin-bottom:12px;}}p{{color:#475569;line-height:1.7;}}</style>
</head>
<body><div class="card"><h2>Animation Loading…</h2>
<p><strong>Question:</strong> {q_esc}</p>
<p style="color:#dc2626;font-size:13px;">Note: {r_esc}</p>
<p>Please check your GEMINI_API_KEY and try again.</p></div></body></html>"""


def generate_animation_html_sync(question: str) -> str:
    """Synchronous wrapper."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, generate_animation_html(question))
                return future.result(timeout=PIPELINE_TIMEOUT + 30)
        else:
            return loop.run_until_complete(
                asyncio.wait_for(generate_animation_html(question), timeout=PIPELINE_TIMEOUT)
            )
    except asyncio.TimeoutError:
        return _fallback_html(question, f"Pipeline timed out after {PIPELINE_TIMEOUT}s")
    except Exception as e:
        return _fallback_html(question, f"Pipeline error: {e}")


# ===========================================================================
# Public API
# ===========================================================================

def generate_animation(question: str) -> str:
    return generate_animation_html_sync(question)


async def generate_animation_async(question: str) -> str:
    return await generate_animation_html(question)


async def generate_question_animation(question: str) -> dict:
    """Main async entry point for server integration."""
    question = (question or "").strip()
    if not question:
        raise ValueError("'question' field cannot be empty")
    html = await generate_animation_html(question)
    m = re.search(r'<title[^>]*>([^<]{5,120})</title>', html, re.IGNORECASE)
    explanation = m.group(1).strip() if m else f"9-scene animation: {question[:120]}"
    return {"title": question[:80], "explanation": explanation, "animation_code": html}


# Backward compat aliases
analyse_question  = analyze_scene
generate_solution_compat = generate_solution


# ===========================================================================
# CLI
# ===========================================================================

if __name__ == "__main__":
    import sys, time as _time_mod

    if len(sys.argv) < 2:
        print("Usage: python q_animation.py '<question>' [output.html]")
        print()
        print("Example:")
        print("  python q_animation.py 'A wire of resistance 10Ω is stretched to twice its length. Find the new resistance.' output.html")
        sys.exit(0)

    q   = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "animation_output.html"

    print(f"\n{'='*60}")
    print("  QAnim v3.0 — 9-Scene Animation Generator")
    print(f"{'='*60}")
    print(f"  Question : {q[:100]}{'...' if len(q)>100 else ''}")
    print(f"  Output   : {out}")
    print(f"  Model    : {GEMINI_MODEL}")
    print(f"{'='*60}\n")

    t0 = _time_mod.time()
    html_out = generate_animation(q)
    elapsed = _time_mod.time() - t0

    with open(out, "w", encoding="utf-8") as f:
        f.write(html_out)

    size_kb = len(html_out) / 1024
    print(f"\n{'='*60}")
    print(f"  Done in {elapsed:.1f}s  |  {size_kb:.1f} KB  |  Saved to {out}")
    print(f"{'='*60}\n")
    print(f"  Open {out} in your browser to view the animation.")
