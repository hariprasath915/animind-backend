"""
q_animation.py  —  QAnim Question Animation Generator  v3.1
============================================================

v3.1 — MODEL FIX: gemini-2.5-pro → gemini-3.1-pro-preview
  - gemini-2.5-pro returns HTTP 404 NOT_FOUND for new API keys.
  - Model is now read from GEMINI_MODEL env var (default: gemini-3.1-pro-preview).
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

def _he(text) -> str:
    """Helper to escape HTML and handle None."""
    return html_module.escape(str(text)) if text is not None else ""

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
            # ── Fix: Force IPv4 to prevent wsarecv TCP stream kills ──────────
            # Root cause: on dual-stack Windows machines (common on Indian ISPs)
            # DNS resolves generativelanguage.googleapis.com to an IPv6 address.
            # The IPv6 path drops large streaming responses mid-transfer
            # (wsarecv: An established connection was aborted by the software in
            # your host machine). Two-layer fix:
            #   Layer 1 — socket.getaddrinfo monkey-patch: forces the OS DNS
            #     resolver to return ONLY AF_INET (IPv4) results, so the httpx
            #     connection pool never even sees the IPv6 address.
            #   Layer 2 — httpx local_address + http2=False: binds the outgoing
            #     socket to 0.0.0.0 (IPv4) and disables HTTP/2 (h2 stream
            #     multiplexing can amplify the wsarecv kill on large responses).
            try:
                import socket as _socket_mod
                _orig_getaddrinfo = _socket_mod.getaddrinfo
                def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
                    """Return only AF_INET results to force IPv4 DNS resolution."""
                    return _orig_getaddrinfo(host, port, _socket_mod.AF_INET, type, proto, flags)
                _socket_mod.getaddrinfo = _ipv4_only_getaddrinfo
                print("[QAnim] socket.getaddrinfo patched → IPv4-only DNS (wsarecv fix)")
            except Exception as _sock_e:
                print(f"[QAnim] socket patch skipped: {_sock_e}")

            try:
                import httpx as _httpx
                _ipv4_transport = _httpx.HTTPTransport(
                    local_address="0.0.0.0",  # bind to IPv4 local address
                )
                _ipv4_http_client = _httpx.Client(
                    transport=_ipv4_transport,
                    http2=False,              # HTTP/1.1 only — avoids h2 stream kills
                )
                _gemini_client = _google_genai.Client(
                    api_key=_gkey,
                    http_client=_ipv4_http_client,
                )
                print(f"[QAnim] Gemini ready (google-genai + IPv4-forced + HTTP/1.1, model={GEMINI_MODEL})")
            except Exception:
                # httpx unavailable or http_client kwarg not supported — default client
                # (socket patch above still protects against IPv6 DNS resolution)
                _gemini_client = _google_genai.Client(api_key=_gkey)
                print(f"[QAnim] Gemini ready (google-genai + socket-IPv4, model={GEMINI_MODEL})")
        except Exception as e:
            _GEMINI_DISABLED_REASON = repr(e)
else:
    _GEMINI_DISABLED_REASON = "No Gemini SDK installed"

MAX_TOKENS_SOLUTION  = 5000
MAX_TOKENS_SCENE     = 10000
# ── Reduced from 18000 to 12000 ──────────────────────────────────────────────
# Root cause of TCP stream kills: large token responses stream ~120KB over IPv6
# which gets killed mid-stream by wsarecv on Indian ISP connections.
# 12000 tokens is sufficient for a well-formed 6-step SVG animation JSON and
# produces ~40-50KB streams that complete reliably even on unstable IPv6 paths.
# The primary fix is forcing IPv4 on the API client (see _gemini_client init below).
MAX_TOKENS_HTML      = 12000
TIMEOUT_SOLUTION     = 180.0   # ↑ increased from 120s — allows slower Gemini responses
TIMEOUT_SCENE        = 210.0   # ↑ increased from 150s — SVG scene generation can be slow
# ── Increased from 480s to 600s ───────────────────────────────────────────────
# With MAX_RETRIES=4 and RETRY_DELAYS=[15,35,70], worst-case retry time is
# 180s (generation) + 15+35+70=120s (sleeps) = 300s. 600s gives 300s headroom.
TIMEOUT_HTML         = 600.0   # ↑ increased from 480s
PIPELINE_TIMEOUT     = 780.0   # ↑ increased from 660s


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
    """Call Gemini with retry on 429/503 and connection/stream errors."""
    import time as _time
    if _gemini_client is None:
        raise RuntimeError(f"Gemini unavailable: {_GEMINI_DISABLED_REASON}")
    MAX_RETRIES = 4
    RETRY_DELAYS = [15, 35, 70]
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
            err_lower = err.lower()
            retryable = (
                "429" in err or "503" in err or "overloaded" in err_lower
                or "resource has been exhausted" in err_lower
                # Network / stream errors — covers the exact wsarecv crash:
                #   "stream reading error: read tcp ... wsarecv:
                #    An existing connection was forcibly closed by the remote host"
                or "wsarecv"            in err_lower
                or "forcibly closed"    in err_lower
                or "stream reading"     in err_lower
                or "read tcp"           in err_lower
                or "connection reset"   in err_lower
                or "connectionreset"    in err_lower
                or "connection aborted" in err_lower
                or "broken pipe"        in err_lower
                or "brokenpipe"         in err_lower
                or "remotedisconnected" in err_lower
                or "eof"                in err_lower
                or isinstance(e, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError))
            )
            if retryable:
                Log.warn("Gemini", f"Attempt {attempt}/{MAX_RETRIES} — network/rate error: {err[:120]}")
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
  "system_label2": "Forced convection over a hot surface",
  "customize": {
    "fields": [
      {"id": "h",  "symbol": "h",   "label": "Convective coefficient", "default": 25,  "unit": "W/m²·K"},
      {"id": "A",  "symbol": "A",   "label": "Surface area",           "default": 2,   "unit": "m²"},
      {"id": "Ts", "symbol": "Ts",  "label": "Surface temperature",    "default": 150, "unit": "°C"},
      {"id": "Ti", "symbol": "T∞",  "label": "Ambient temperature",  "default": 30,  "unit": "°C"}
    ],
    "compute_js": "var Q = vals.h * vals.A * Math.abs(vals.Ts - vals.Ti); return { answer: _fmt(Q), answer_unit: 'W', answer_label: 'Q', derived: {'Q (heat loss)': _fmt(Q) + ' W'} };",
    "question_template": "A surface with area {A} m² and convective coefficient h = {h} W/m²·K. Surface temperature = {Ts} °C, ambient temperature = {Ti} °C. Find the heat loss rate Q."
  }
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
- customize.fields: one entry per GIVEN numeric value (not the unknown). id = valid JS identifier. default = original numeric value as a number. For percentage values (e.g. 25% loss) use the raw percentage number as default (e.g. 25, not 0.25).
- customize.compute_js: JS function body (not the function declaration) that receives vals (object keyed by field id) and _fmt(v) helper. Must return {answer, answer_unit, answer_label, derived:{label:value_str}}. ALWAYS include a guard for invalid inputs (e.g. zero height, negative fraction). For percentage fields, convert inside JS: var frac = 1 - vals.loss/100;
- customize.question_template: question text with {id} placeholders for each field.
- ALWAYS include the customize block. NEVER omit it, even for rolling/rotation/energy questions.
- compute_js body: write plain JS braces { } — do NOT escape them. The host will not re-process them.
- Pure JSON only.

SECOND EXAMPLE — Rolling body with energy loss (ring on incline):
{
  "steps": [
    "Step 1: For a ring I=mR2, rolling gives KE_total=mv2",
    "Step 2: Energy balance: mv2 = (1-0.25)*mgh = 0.75*mgh",
    "Step 3: Solve: v = sqrt(0.75*9.81*5) = 6.07 m/s"
  ],
  "final_answer": "v = 6.07 m/s",
  "answer_value": "6.07",
  "answer_unit": "m/s",
  "key_insight": "A ring converts half its available KE to rotation, so friction removes more energy than for a disk.",
  "formula": "v = sqrt((1-f)*g*h)",
  "formula_name": "Energy Conservation with Rolling Loss",
  "variables": [
    {"symbol": "v", "name": "Speed at bottom",    "value": "? (to find)", "unit": "m/s",  "color": "green"},
    {"symbol": "g", "name": "Gravitational accel","value": "9.81",         "unit": "m/s2", "color": "blue"},
    {"symbol": "h", "name": "Vertical height",    "value": "5",            "unit": "m",    "color": "blue"},
    {"symbol": "f", "name": "Loss fraction",      "value": "0.25",         "unit": "",     "color": "orange"}
  ],
  "substitution_chain": [
    {"num": 1, "eq": "v = sqrt((1-f)*g*h)"},
    {"num": 2, "eq": "v = sqrt((1-0.25)*9.81*5)"},
    {"num": 3, "eq": "v = sqrt(0.75*49.05)"},
    {"num": 4, "eq": "v = sqrt(36.79) = 6.07 m/s"}
  ],
  "given_list": ["h = 5 m", "Energy loss = 25%", "Ring: I = mR2", "g = 9.81 m/s2"],
  "approach_steps": [
    {"num": "8.1", "label": "Total KE of ring", "eq": "KE = mv2 (ring rolling)", "note": "I=mR2 and v=omegaR"},
    {"num": "8.2", "label": "Energy equation",  "eq": "mv2 = 0.75*mgh",          "note": "25% lost"},
    {"num": "8.3", "label": "Solve for v",      "eq": "v = sqrt(0.75*g*h)",      "note": "Final"}
  ],
  "system_title": "Ring Rolling Down Incline",
  "system_label2": "Rolling with energy loss due to friction",
  "customize": {
    "fields": [
      {"id": "h",    "symbol": "h", "label": "Vertical height",     "default": 5,    "unit": "m"},
      {"id": "loss", "symbol": "f", "label": "Energy loss",         "default": 25,   "unit": "%"},
      {"id": "g",    "symbol": "g", "label": "Gravitational accel", "default": 9.81, "unit": "m/s2"}
    ],
    "compute_js": "var frac = 1 - vals.loss / 100; if(frac < 0) frac = 0; if(vals.h <= 0 || vals.g <= 0) return {answer:'?', answer_unit:'m/s', answer_label:'v', derived:{}}; var v = Math.sqrt(frac * vals.g * vals.h); return {answer: _fmt(v), answer_unit: 'm/s', answer_label: 'v', derived: {'Speed v': _fmt(v) + ' m/s', 'Energy retained': _fmt(frac * 100) + ' %'}};",
    "question_template": "A ring rolls from height {h} m. {loss}% of mechanical energy lost to friction. g = {g} m/s2. Find speed at the bottom."
  }
}

THIRD EXAMPLE — Rocket equation with logarithm (exhaust speed / liftoff condition):
{
  "steps": [
    "Step 1: At liftoff, thrust must exceed weight: u*alpha >= m0*g",
    "Step 2: Minimum exhaust speed: u_min = (m0 * g) / alpha",
    "Step 3: Substitute: u_min = (1000 * 9.81) / 10 = 981 m/s"
  ],
  "final_answer": "u_min = 981 m/s",
  "answer_value": "981",
  "answer_unit": "m/s",
  "key_insight": "The rocket lifts off only when thrust u*alpha exceeds gravitational force m0*g.",
  "formula": "u_min = m0*g / alpha",
  "formula_name": "Tsiolkovsky Liftoff Condition",
  "variables": [
    {"symbol": "u_min", "name": "Minimum exhaust speed", "value": "? (to find)", "unit": "m/s",  "color": "green"},
    {"symbol": "m0",    "name": "Initial mass",          "value": "1000",         "unit": "kg",   "color": "blue"},
    {"symbol": "alpha", "name": "Mass-loss rate",        "value": "10",           "unit": "kg/s", "color": "blue"},
    {"symbol": "g",     "name": "Gravitational accel",   "value": "9.81",         "unit": "m/s2", "color": "blue"}
  ],
  "substitution_chain": [
    {"num": 1, "eq": "u_min = m0 * g / alpha"},
    {"num": 2, "eq": "u_min = 1000 * 9.81 / 10"},
    {"num": 3, "eq": "u_min = 9810 / 10 = 981 m/s"}
  ],
  "given_list": ["m0 = 1000 kg (initial mass)", "alpha = 10 kg/s (mass-loss rate)", "g = 9.81 m/s2"],
  "approach_steps": [
    {"num": "8.1", "label": "Liftoff condition", "eq": "Thrust = u*alpha >= m0*g", "note": "Newton's 2nd law"},
    {"num": "8.2", "label": "Solve for u_min",   "eq": "u_min = m0*g / alpha",     "note": "Rearrange"},
    {"num": "8.3", "label": "Substitute",        "eq": "u_min = 1000*9.81/10 = 981 m/s", "note": "Final"}
  ],
  "system_title": "Rocket Liftoff",
  "system_label2": "Minimum exhaust speed for vertical liftoff",
  "customize": {
    "fields": [
      {"id": "m0",    "symbol": "m0",    "label": "Initial mass",        "default": 1000, "unit": "kg"},
      {"id": "alpha", "symbol": "alpha", "label": "Mass-loss rate",      "default": 10,   "unit": "kg/s"},
      {"id": "g",     "symbol": "g",     "label": "Gravitational accel", "default": 9.81, "unit": "m/s2"}
    ],
    "compute_js": "if(vals.alpha <= 0) return {answer:'?', answer_unit:'m/s', answer_label:'u_min', derived:{}}; var u = (vals.m0 * vals.g) / vals.alpha; return {answer: _fmt(u), answer_unit: 'm/s', answer_label: 'u_min', derived: {'Thrust needed': _fmt(vals.m0 * vals.g) + ' N', 'Thrust provided (at u)': _fmt(vals.alpha) + ' * u N'}};",
    "question_template": "A rocket with initial mass {m0} kg is launched vertically. Mass-loss rate = {alpha} kg/s, g = {g} m/s2. Find the minimum exhaust speed u for liftoff."
  }
}"""


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
        # Fallback customize — a minimal single-field panel so the button always works
        "customize": {"fields": [], "compute_js": "", "question_template": ""},
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
_SVG_BUILDER_SYSTEM = r"""
You are QAnim Studio, an expert SVG artist and motion designer.

Your task: for ANY physics/math question, generate a realistic, well-structured,
6-step SVG concept animation with perfect layout and premium design.

============================================================
OUTPUT FORMAT
============================================================

Return ONLY valid JSON with these fields:

{
  "svg_defs": "...",
  "svg_layers": "...",
  "steps_data_js": "...",
  "apply_step_js": "...",
  "raf_js": "..."
}

No Markdown. No extra text. Only JSON.

============================================================
VISUAL GOAL
============================================================

Create a PREMIUM, PHOTOREALISTIC scientific visualization tailored to the question.
The animation must look like it belongs in a professional science textbook or museum exhibit.
Make every element polished: gradients, shadows, highlights, glows, and smooth motion.

Requirements:

1. Realistic objects:
   - Draw the main physical objects realistically:
     - Earth, satellite, planet, star, atom, wire, plate, projectile, circuit, etc.
   - Use gradients, shading, and subtle highlights to suggest 3D form.
   - Avoid cartoonish or overly abstract shapes.

2. Perfect layout:
   - Use viewBox="0 0 850 478".
   - Center the main system and keep clear margins.
   - Important content must stay inside x=24..826 and y=24..454.
   - Labels must not overlap objects.
   - Composition must look balanced on desktop, tablet, and mobile.

3. Structure:
   - Use exactly these layers (or those given by the scene script):
     <g id="layer-frame">...</g>
     <g id="layer-object">...</g>
     <g id="layer-param1">...</g>
     <g id="layer-param2">...</g>
     <g id="layer-derived">...</g>
     <g id="layer-summary">...</g>
   - layer-frame starts visible (opacity="1").
   - All other layers start hidden (opacity="0").
   - Each layer must have a clear visual purpose.

3b. PREMIUM QUALITY REQUIREMENTS (mandatory for every output):
   - Every main object MUST use at least one linearGradient or radialGradient for 3-D depth.
   - Every main object MUST have a <filter> drop-shadow (feDropShadow or feMerge) for lift.
   - Use stroke-linecap="round" and stroke-linejoin="round" on all mechanical parts.
   - Important labels must have a subtle pill background (a <rect> behind the text, rx≥5, fill white/light, opacity 0.85).
   - Annotation arrows must use <marker> arrowheads (not just bare lines).
   - Color palette must be vivid and harmonious — pick 3–5 coordinated accent colors, never plain red/blue/green.
   - All layer reveal transitions must use opacity 0→1 plus a subtle scale or translateY transform (done via JS in applyStep).
   - The layer-summary (Step 6) must show a polished "callout" box with rounded corners, gradient background, and a glowing border for the "? unknown" label.

4. Design style:
   - LIGHT, CLEAN, PROFESSIONAL background — always use white or very light grey/blue (#f8fafc, #eef5ff, #f0f6ff).
   - NEVER use dark navy, charcoal, black, or dark space backgrounds.
   - HIGH-CONTRAST main objects using vivid, saturated colors (cyan #0891b2, blue #2563eb, green #16a34a, orange #d97706, violet #7c3aed) on the light background.
   - Mechanism parts (cranks, rods, sliders, gears, links, pistons, wheels): draw them clearly with thick strokes (2–4px), gradient fills, and subtle drop shadows.
   - Text labels: dark (#1e293b, #0f172a) on the light background — always readable.
   - Soft gradients on objects only, NOT on the background.
   - Subtle grid lines (#cbd5e1 at 0.25 opacity) for scale reference.
   - No clutter. Every element must help understanding.

============================================================
SVG DEFS
============================================================

Create a <defs> section with only what you use:

- Gradients for main objects (NOT for the background — background must stay light).
- Drop shadows / subtle glow filters for important elements (use low opacity, e.g. flood-opacity="0.18").
- Arrow markers for forces, motion, fields, or dimensions.
- A subtle light grid pattern: <pattern id="bg-grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M 40 0 L 0 0 0 40" fill="none" stroke="#cbd5e1" stroke-width="0.4"/></pattern>

Recommended gradient IDs:
  grad-crank, grad-rod, grad-slider, grad-plate, grad-wire, grad-object — all vivid colors on top of light fills.
  Do NOT create dark background gradients.

Do not use external images, fonts, or URLs.

============================================================
ANIMATION — SMOOTH & REALISTIC
============================================================

- Reveal objects step by step (opacity 0→1 combined with a translateY(12px)→0 or scale(0.92)→1 transform).
- Use smooth transitions (600–900 ms) with cubic-bezier(0.34,1.56,0.64,1) spring easing for objects
  and cubic-bezier(0.4,0,0.2,1) for opacity.
- NEVER use abrupt jumps — all motion must feel natural and physics-based.
- Motion must visually explain the physical concept (e.g., projectile arc, wire stretching, satellite orbit).
- No flashing, no jitter, no random decoration.
- applyStep must set layer opacity via element.style.opacity (NOT setAttribute). It may also toggle
  a CSS class (e.g. 'layer-shown') that drives a CSS transition for scale/translate if desired.

SMOOTH PHYSICS MOTION (mandatory if the concept involves dynamics):
- Projectile / ballistic: animate along a true parabolic arc using requestAnimationFrame.
  Use parametric equations: x = x₀ + v₀ₓ·t, y = y₀ + v₀ᵧ·t − ½g·t².
  Show the trajectory trail as a dashed path that appears incrementally.
  Show velocity components (horizontal arrow constant length, vertical arrow shrinks then grows).
- Orbit / circular motion: use sin/cos for perfectly smooth orbit.
  Add a faint elliptical orbit path; planet/satellite casts a moving shadow.
- Wave / oscillation: render sinusoidal wave with requestAnimationFrame, phase-shifting each frame.
- Wire / elastic: animate length change with a smooth stretch transform.
- Heat / diffusion: animate a stop-color or fill interpolation smoothly from hot (#ef4444) to cold (#3b82f6).
- Fluid flow: animate streamlines or particle movement along defined SVG paths.
- Rotating machinery: use requestAnimationFrame with sin/cos kinematics; show angle arcs and velocity arrows.

If continuous animation is needed, define:
  window.qanimStartRAF = function(){{
    if (window.qanimRafId) cancelAnimationFrame(window.qanimRafId);
    window.qanimRafId = requestAnimationFrame(drawFrame);
  }};

If no continuous animation is needed, return:
  "raf_js": ""

If continuous animation is needed, define:

window.qanimStartRAF = function(){
  if (window.qanimRafId) cancelAnimationFrame(window.qanimRafId);
  window.qanimRafId = requestAnimationFrame(drawFrame);
};

If no continuous animation is needed, return:

"raf_js": ""

============================================================
STEPS DATA
============================================================

Return exactly 6 steps:

var stepsData = [
  {
    title: "Step 1: ...",
    desc: "...",
    badges: ['<span class="badge badge-cyan">Key value = 120 K</span>'],
    blurOp: 0.0,
    layerOpacities: {
      "layer-frame": 1,
      "layer-object": 0,
      "layer-param1": 0,
      "layer-param2": 0,
      "layer-derived": 0,
      "layer-summary": 0
    },
    overlays: []
  }
];

Rules:
- badges must be an array of HTML strings.
- blurOp = 0.0 for steps 1 and 6; 0.38 for steps 2–5.
- layerOpacities must include all layers.
- Step 6 shows all layers. DO NOT include "? unknown" or "= ?" badges in step 6.
  Step 6 is the "Complete Setup" — show only the given parameters as badges (cyan/orange/green).
  The unknown is revealed only in Steps 7-9 (the formula/solution overlay scenes).

============================================================
APPLY STEP JAVASCRIPT
============================================================

Return the body of applyStep(idx):

- Set window.currentStep = idx.
- Update progress bar: ((idx + 1) / 9 * 100) + '%'.
- Update label: 'Step ' + (idx + 1) + ' of 9'.
- Update step dots (active/done).
- Set info-title and info-desc text.
- Render badges:
  document.getElementById('info-badges').innerHTML =
    (stepsData[idx].badges || []).join('');
- Set layer opacity with:
  element.style.opacity = value;
- Set blur-shield opacity.
- Disable/enable navigation buttons correctly.

Do not use setAttribute('opacity', value).
Do not use innerHTML for user text.

============================================================
NOTATION AND LABELS — MATHEMATICAL PRECISION
============================================================

Always use correct mathematical symbols and notation:
- Greek letters: Δ (delta), θ (theta), μ (mu), ρ (rho), Ω (omega), α (alpha), β (beta), φ (phi), λ (lambda), ω (angular velocity), π
- Special: ∞, °, ×, ·, ≈, ≠, ≤, ≥, ∝, √, ∫
- Subscripts using SVG <tspan baseline-shift="sub" font-size="75%">: v₀, aₓ, T₁, R₂, Fₙ
- Superscripts using SVG <tspan baseline-shift="super" font-size="75%">: m², m³, v², s⁻¹
- Units always in roman (non-italic): m, m/s, m/s², kg, N, W, J, Ω, K, °C, Pa, m²
- Vector notation: add an arrow or bold label, e.g. F⃗, v⃗
- Example label styles:
    "v₀ = 20 m/s" not "v0 = 20 m/s"
    "a = 9.8 m/s²" not "a = 9.8 m/s^2"
    "ΔT = 120 K" not "DeltaT = 120 K"
    "R₂ = ?" not "R2 = ?"
- Keep labels short, readable, and never overlapping.
- Use high-contrast text (white or light on dark backgrounds, dark on light).
- Do not invent values. Match the verified solution exactly.

============================================================
REALISM BY EXAMPLE (ADAPT TO QUESTION)
============================================================

Adapt realism to the topic. ALWAYS use a LIGHT BACKGROUND (#f8fafc, #eef5ff, or #f0f6ff):

- Mechanisms (slider-crank, four-bar linkage, cam-follower, gear trains, etc.):
  - Crank: thick circular/elliptical arc or line, gradient-filled (e.g. steel blue #2563eb→#1d4ed8), pivot pin circle.
  - Connecting rod: thick line with end circles at pin joints, labeled with its length.
  - Slider: a filled rectangle (piston) on a horizontal guide rail with clear end-stops.
  - Guide rail: a double horizontal line (I-beam), light grey fill, with tick marks.
  - Fixed pivot: a triangle with hatch lines beneath it (ground symbol).
  - Show the crank angle θ as an arc with label near the pivot.
  - Show the slider path as a dashed horizontal line.
  - Velocity arrows: vivid orange arrows with labels (v⃗, ω, r, l).
  - All components labeled: crank (r), rod (l), slider, fixed point O, pin A, slider B.

- Space / gravity:
  - Planet: spherical, gradient shading, atmosphere glow on LIGHT background.
  - Satellite / spacecraft: body + panels + antenna, slight shadow.
  - Orbit: smooth circular/elliptical path with subtle glow.
  - Background: very light blue (#eef5ff) with faint dots for stars.

- Mechanics:
  - Blocks, ramps, pulleys: clean 3D-like shading, clear edges on light background.
  - Forces: well-sized arrows with labels (F, mg, N, T).
  - Motion: path lines or velocity arrows.

- Electricity / circuits:
  - Wires: clean paths with consistent stroke (#1e293b) on white/light background.
  - Components (R, C, L, battery): clear symbols, vivid color coding.
  - Current direction: small arrows along wires.

- Heat / fluids:
  - Plates, fins, pipes: smooth gradients for temperature/flow on light background.
  - Arrows for heat flow or fluid direction.
  - Color coding for hot (red/orange) / cold (blue/cyan) regions.

- Waves / optics:
  - Rays, wavefronts, lenses, mirrors: precise geometry on light background.
  - Smooth sinusoidal waves or ray paths.
  - Clear labels for angles, focal points, etc.

Always:
- Light background (#f8fafc, #eef5ff, or #f0f6ff) — mandatory.
- Make the main object look like a real physical system with high visual fidelity.
- Use gradients and shading on objects to suggest depth (NOT on the background).
- Keep labels and arrows clean, dark (#1e293b), and unambiguous.

============================================================
VALIDATION BEFORE OUTPUT
============================================================

Check silently:

- All SVG tags closed.
- All layer IDs unique and present.
- No external URLs.
- stepsData has exactly 6 steps.
- badges are arrays.
- applyStep uses style.opacity and join('').
- Progress label says "of 9".
- Notation matches the solution.
- layer-frame contains a light background rect (fill="#f8fafc" or similar light color) as the FIRST child.
- NO dark backgrounds anywhere in the SVG.

============================================================
PER-STEP PHYSICS MOTION (mandatory for dynamic problems)
============================================================

CRITICAL: The applyStep() function you provide in "apply_step_js" is DISCARDED at
runtime and replaced by a Python-controlled implementation. Therefore:

  ► NEVER put per-step motion code inside applyStep().
  ► ALL step-dependent motion MUST live inside the RAF loop ("raf_js").

How to drive step-specific motion from the RAF loop:

1. At the top of your drawFrame() function, read:
     var step = (typeof window.currentStep === 'number') ? window.currentStep : 0;

2. Maintain a 'prevStep' variable. When step !== prevStep, reset the motion
   state for the new step (record startTime, set start/end positions):
     if (step !== prevStep) {
       prevStep = step;
       motionStartTime = performance.now();
       // set startPos, endPos based on step
     }

3. For each step that involves physical motion, smoothly interpolate over ~1500 ms:
     var elapsed = Math.min(performance.now() - motionStartTime, 1500);
     var t = elapsed / 1500;   // 0 → 1
     // easing: ease-in-out
     t = t < 0.5 ? 2*t*t : -1+(4-2*t)*t;
     var pos = startPos + (endPos - startPos) * t;
     // apply pos to the SVG element's transform

4. For static steps (just showing labels, forces, values) — no motion needed;
   simply ensure the relevant SVG element is at its final position.

Examples of CORRECT step-keyed motion in raf_js:

  Rolling disc problem — step 3 shows disc at max height:
    When step === 3, animate the disc element from y=bottomY to y=topY
    along the incline path, over 1.5 s. On subsequent RAF ticks, hold at topY.

  Projectile — step 4 shows peak:
    When step === 4, animate the projectile along its parabolic arc
    from launch to peak. Display the trajectory trail incrementally.

  Orbit — always animating:
    Use elapsed total time (not step-based) for continuous orbital motion.
    Pause the orbit (stop updating angle) if the current step is a static annotation.

  Wire stretching — step 2 shows elongation:
    When step === 2, smoothly scale the wire element from 1.0 to stretchRatio.

When to use motion vs. static:
  - Step involves a physical PROCESS or end-STATE of motion → use RAF motion.
  - Step only labels, annotates, or shows a given value → keep element static.
  - Always tie motion to the PHYSICS of the step, so the student sees WHY.

Do NOT return an empty "raf_js" for problems involving dynamics.
Return "raf_js": "" ONLY for purely static problems (circuit labels, formula derivation).
"""


def _rebuild_steps_data_js(scene: dict) -> str:
    """Rebuild stepsData from scene dict to avoid JS syntax errors."""
    steps_data = []
    
    all_layers = [
        "layer-frame", "layer-object", "layer-param1",
        "layer-param2", "layer-derived", "layer-summary"
    ]
    if scene.get("svg_layers"):
        all_layers = list(scene["svg_layers"].keys())
        
    for s in scene.get("steps", []):
        badges_html = []
        for b in s.get("badges", []):
            b_text = html_module.escape(str(b.get("text", "")))
            b_type = html_module.escape(str(b.get("type", "cyan")))
            badges_html.append(f"<span class='badge badge-{b_type}'>{b_text}</span>")
            
        layer_ops = {}
        visible_layers = s.get("layers_visible", [])
        for layer in all_layers:
            layer_ops[layer] = 1 if layer in visible_layers else 0
            
        steps_data.append({
            "title": str(s.get("title", "")),
            "desc": str(s.get("description", "")),
            "badges": badges_html,
            "blurOp": 0.38 if s.get("blur") else 0.0,
            "layerOpacities": layer_ops,
            "overlays": []
        })
        
    return "var stepsData = " + json.dumps(steps_data, indent=2) + ";"


def build_svg_and_steps(question: str, scene: dict, sol: dict) -> dict:
    """Call Gemini to generate the SVG and step data."""
    if _gemini_client is None:
        return {"svg_defs": "", "svg_layers": "", "steps_data_js": "var stepsData=[];", "apply_step_js": "function applyStep(idx){window.currentStep=idx;}", "raf_js": ""}

    scene_str = json.dumps(scene, indent=2)
    sol_str = json.dumps(sol, indent=2)
    # NOTE: Use string concatenation (NOT f-string) here.
    # sol_str and scene_str are JSON-dumped dicts that may contain literal
    # {variable_name} text (e.g. {note_text} in a formula or note field).
    # Python f-string interpolation would try to evaluate those as Python
    # expressions, raising NameError: name 'note_text' is not defined.
    prompt = ("Question:\n" + question + "\n\nScene Script:\n" + scene_str
              + "\n\nSolution:\n" + sol_str
              + "\n\nGenerate the 6-step SVG concept animation as JSON.")

    for attempt in range(1, 5):   # 4 attempts total
        try:
            raw = _call_gemini(prompt, _SVG_BUILDER_SYSTEM, max_tokens=MAX_TOKENS_HTML)
            data = json.loads(_sanitize_json(raw))
            data["_scene"] = scene
            return _sanitize_svg_data(data)
        except Exception as e:
            err = str(e)
            Log.warn("SVGBuilder", f"Attempt {attempt}/4 failed: {err[:120]}")
            if attempt < 4:
                # Short sleep between JSON-parse retries.
                # Network-level errors are already retried inside _call_gemini.
                import time as _t
                _t.sleep(10 * attempt)

    # ── All Gemini attempts exhausted: build a scene-based fallback ──────────
    # Instead of returning empty stepsData (which shows a completely blank
    # animation), rebuild stepsData from the already-computed scene dict so the
    # user still sees the 6-step concept walkthrough with correct titles/badges.
    Log.warn("SVGBuilder", "All attempts failed — using scene-dict fallback (no SVG art)")
    return _build_scene_fallback_svg(scene)


def _build_scene_fallback_svg(scene: dict) -> dict:
    """
    Build a minimal but *working* svg_data dict from the scene script alone.

    Called when all 4 Gemini SVG-generation attempts fail (e.g. wsarecv TCP
    stream kill).  Produces:
      • A simple placeholder SVG with the title + one text label per layer
      • Correct stepsData rebuilt from scene["steps"] so all 6 steps render
      • A working applyStep() that shows/hides layers and updates the info panel
    The Customize panel, Scenes 7-9, and all Python-injected HTML are unaffected.
    """
    steps   = scene.get("steps") or []
    title   = scene.get("title", "Physics Problem")[:80]
    layers  = list((scene.get("svg_layers") or {}).keys())

    # ── Build SVG defs + background ──────────────────────────────────────────
    svg_defs = """
<pattern id="qanim-fb-grid" width="40" height="40" patternUnits="userSpaceOnUse">
  <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#cbd5e1" stroke-width="0.5"/>
</pattern>"""

    # One <g> per layer with a simple label
    layer_groups = []
    for i, lid in enumerate(layers):
        linfo = (scene.get("svg_layers") or {}).get(lid, {})
        ldesc = str(linfo.get("description", lid))[:60]
        lcolor = str(linfo.get("color", "#0891b2"))
        y_pos = 80 + i * 56
        vis = "1" if i == 0 else "0"
        layer_groups.append(
            f'<g id="{lid}" style="opacity:{vis}">\n'
            f'  <rect x="40" y="{y_pos}" width="770" height="44" rx="8" '
            f'fill="{lcolor}" fill-opacity="0.12" stroke="{lcolor}" stroke-width="1.5"/>\n'
            f'  <text x="60" y="{y_pos + 27}" font-family="Inter,sans-serif" '
            f'font-size="15" fill="{lcolor}" font-weight="600">{_he(ldesc)}</text>\n'
            f'</g>'
        )

    svg_layers = (
        f'<g id="layer-canvas-bg">'
        f'<rect width="850" height="478" fill="#f8fafc"/>'
        f'<rect width="850" height="478" fill="url(#qanim-fb-grid)"/>'
        f'</g>\n'
        f'<text x="425" y="48" font-family="Inter,sans-serif" font-size="19" '
        f'font-weight="700" fill="#1e293b" text-anchor="middle">{_he(title)}</text>\n'
        + "\n".join(layer_groups)
    )

    # ── Build stepsData from scene["steps"] ──────────────────────────────────
    BADGE_COLOR_MAP = {"cyan": "badge-cyan", "green": "badge-green",
                       "orange": "badge-orange", "red": "badge-red",
                       "purple": "badge-purple", "blue": "badge-cyan"}

    steps_list = []
    all_layer_ids = [s.get("layer_new") for s in steps if s.get("layer_new")]
    # accumulate layers visible list per step (same logic as assemble_html)
    for s in steps:
        step_num = s.get("step_number", 1)
        blur_op  = 0.0 if s.get("blur") is False else 0.38

        # Build layerOpacities: show layers_visible for this step
        visible_set = set(s.get("layers_visible") or [])
        layer_ops = {}
        for lid in layers:
            layer_ops[lid] = 1 if lid in visible_set else 0

        # Build badge HTML strings
        raw_badges = s.get("badges") or []
        badge_htmls = []
        for b in raw_badges:
            btext = _he(str(b.get("text", "") if isinstance(b, dict) else b))
            btype = (b.get("type", "cyan") if isinstance(b, dict) else "cyan")
            cls   = BADGE_COLOR_MAP.get(btype, "badge-cyan")
            badge_htmls.append(f"<span class='badge {cls}'>{btext}</span>")

        steps_list.append({
            "title":         s.get("title", f"Step {step_num}"),
            "desc":          s.get("description", ""),
            "badges":        badge_htmls,
            "blurOp":        blur_op,
            "layerOpacities": layer_ops,
            "overlays":      [],
        })

    steps_data_js = "var stepsData = " + json.dumps(steps_list, ensure_ascii=False) + ";"

    # ── applyStep JS ─────────────────────────────────────────────────────────
    apply_step_js = r"""
function applyStep(index) {
  if (!window.stepsData || index < 0 || index >= window.stepsData.length) return;
  window.currentStep = index;
  var s = window.stepsData[index];
  var total = window.CONCEPT_STEP_COUNT || 6;
  // Progress bar
  var bar = document.getElementById('step-bar');
  if (bar) bar.style.width = ((index + 1) / 9 * 100) + '%';
  var lbl = document.getElementById('step-label');
  if (lbl) lbl.textContent = 'Step ' + (index + 1) + ' of 9';
  // Info panel
  var ti = document.getElementById('info-title');
  if (ti) ti.textContent = s.title || '';
  var di = document.getElementById('info-desc');
  if (di) di.textContent = s.desc || '';
  var bi = document.getElementById('info-badges');
  if (bi) bi.innerHTML = (s.badges || []).join('');
  // Layer opacities
  var ops = s.layerOpacities || {};
  Object.keys(ops).forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.style.opacity = ops[id];
  });
  // Dots
  for (var i = 0; i < total; i++) {
    var d = document.getElementById('dot-step' + (i + 1));
    if (!d) continue;
    d.classList.remove('active', 'done');
    if (i === index) d.classList.add('active');
    else if (i < index) d.classList.add('done');
  }
  // Buttons
  var bp = document.getElementById('btn-prev');
  var bn = document.getElementById('btn-next');
  if (bp) bp.disabled = (index === 0);
  if (bn) bn.disabled = (index === total - 1);
}
"""

    return {
        "svg_defs":      svg_defs,
        "svg_layers":    svg_layers,
        "steps_data_js": steps_data_js,
        "apply_step_js": apply_step_js,
        "raf_js":        "",
        "_scene":        scene,
        "_fallback":     True,
    }



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

    # ── Fix svg_defs: strip any <defs>...</defs> wrapper ─────────────────────
    # Gemini sometimes returns svg_defs with its own <defs> opening and/or
    # </defs> closing tag. We inject it INSIDE our own <defs> block, so an
    # extra </defs> prematurely closes our block, causing invalid SVG where
    # the injected light-bg pattern falls outside defs → gradients break.
    raw_defs = data.get("svg_defs", "")
    raw_defs = _re.sub(r'^\s*<defs[^>]*>', '', raw_defs, flags=_re.IGNORECASE).strip()
    raw_defs = _re.sub(r'\s*</defs>\s*$', '', raw_defs, flags=_re.IGNORECASE).strip()
    data["svg_defs"] = raw_defs

    # ── Bug 3 / Fix D: stepsData ALWAYS rebuilt from Python scene dict ────────
    # Root cause of Steps 1-6 not rendering:
    #   Gemini writes badge HTML inside JS string literals, causing SyntaxErrors
    #   that silently kill the entire <script> block. applyStep(), stepsData, and
    #   all navigation become undefined. Steps 1-6 never render.
    #
    # Fix C/D: Discard Gemini's stepsData. Rebuild from _scene dict using
    #   json.dumps() — immune to any quote, backslash, or emoji conflicts.
    #   _fix_badge_spans() regex rewriter is PERMANENTLY REMOVED — it was the
    #   alternative fix that itself corrupted valid JS strings.
    scene_for_rebuild = data.pop("_scene", None)
    if scene_for_rebuild and scene_for_rebuild.get("steps"):
        data["steps_data_js"] = _rebuild_steps_data_js(scene_for_rebuild)
        Log.ok("SVGSanitizer", "stepsData rebuilt from scene dict (Fix C/D: json.dumps, no regex rewrite)")
    else:
        # This branch is never reached in practice because build_svg_and_steps()
        # always injects data["_scene"] before calling _sanitize_svg_data().
        # If somehow reached, keep Gemini's stepsData verbatim — do NOT rewrite
        # it with regex, which risks corrupting valid JS strings (Fix D).
        Log.warn("SVGSanitizer", "_scene missing — keeping Gemini stepsData verbatim (Fix D: no regex rewrite)")



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
    # FIX: Normalize other wrong element IDs Gemini commonly hallucinates
    apply_js = _re.sub(r"getElementById\(['\"]step-bar-inner['\"]\)", "getElementById('step-bar')", apply_js)
    apply_js = _re.sub(r"getElementById\(['\"]progress-bar['\"]\)", "getElementById('step-bar')", apply_js)
    apply_js = _re.sub(r"getElementById\(['\"]step-progress['\"]\)", "getElementById('step-bar')", apply_js)
    apply_js = _re.sub(r"getElementById\(['\"]badge-container['\"]\)", "getElementById('info-badges')", apply_js)
    apply_js = _re.sub(r"getElementById\(['\"]badge-list['\"]\)", "getElementById('info-badges')", apply_js)
    apply_js = _re.sub(r"getElementById\(['\"]step-count['\"]\)", "getElementById('step-label')", apply_js)
    apply_js = _re.sub(r"getElementById\(['\"]step-number['\"]\)", "getElementById('step-label')", apply_js)
    apply_js = _re.sub(r"getElementById\(['\"]current-step['\"]\)", "getElementById('step-label')", apply_js)
    apply_js = _re.sub(r"getElementById\(['\"]description['\"]\)", "getElementById('info-desc')", apply_js)
    apply_js = _re.sub(r"getElementById\(['\"]title['\"]\)", "getElementById('info-title')", apply_js)
    # FIX: Fix .textContent for info-title/info-desc — allow innerHTML too, no change needed
    # FIX: Ensure apply_step_js is always wrapped in function applyStep(idx){...}
    # Gemini sometimes returns just the function body without the def line/closing brace.
    apply_js_stripped = apply_js.strip()
    _has_fn_def = bool(_re.search(r'function\s+applyStep\s*\(', apply_js_stripped))
    if not _has_fn_def:
        # Wrap the raw body in the function definition
        apply_js = "function applyStep(idx) {\n" + apply_js_stripped + "\n}"
        Log.warn("SVGSanitizer", "apply_step_js was missing function wrapper — wrapped automatically")
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
    # IMPORTANT: Only fix the 6 canonical layer-* IDs (layer-object, layer-param1, etc.).
    # Sub-groups inside layers (crank-group, rod-group, slider-group, etc.) must NOT
    # be forced to opacity:0 — they should remain at whatever the SVG author set,
    # because applyStep() only sets opacity on the top-level layer-* groups.
    # Setting sub-group opacity to 0 makes them invisible even after applyStep.
    _CANONICAL_LAYERS = {
        "layer-frame", "layer-object",
        "layer-param1", "layer-param2",
        "layer-derived", "layer-summary",
    }

    svg_layers = data.get("svg_layers", "")

    def _fix_layer_opacity(m):
        tag = m.group(0)
        gid = _re.search(r'id=["\']([^"\']+)["\']', tag)
        layer_id = gid.group(1) if gid else ""
        # Only touch the canonical layer-* groups, leave sub-groups untouched
        if layer_id not in _CANONICAL_LAYERS:
            return tag
        if layer_id == "layer-frame":
            # layer-frame must always start visible
            tag = _re.sub(r'\bopacity=["\']0["\']', 'style="opacity:1"', tag)
            tag = _re.sub(r'style=["\']opacity:\s*0["\']', 'style="opacity:1"', tag)
            if 'opacity' not in tag and 'style' not in tag:
                tag = tag.rstrip('>') + ' style="opacity:1">'
            return tag
        # All other canonical layers start hidden
        tag = _re.sub(r'\bopacity=["\']1["\']', 'style="opacity:0"', tag)
        tag = _re.sub(r'\bopacity=["\']0["\']', 'style="opacity:0"', tag)
        if 'opacity' not in tag and 'style' not in tag:
            tag = tag.rstrip('>') + ' style="opacity:0">'
        return tag

    svg_layers = _re.sub(
        r'<g\b[^>]*id=["\'][^"\']+["\'][^>]*>',
        _fix_layer_opacity,
        svg_layers
    )
    data["svg_layers"] = svg_layers

    # ── Fallback RAF: auto-inject slider-crank animation if Gemini skipped it ─
    # Root cause: Gemini sometimes omits raf_js entirely, leaving the crank/
    # piston completely static even though the SVG has crank-group / rod-group /
    # slider-group elements. We detect this and inject a physics-correct
    # slider-crank animation loop automatically.
    #
    # Bug 2 fix — Incomplete mechanism:
    # Gemini sometimes generates only the connecting rod and slider but omits the
    # rotating crank entirely. The animation then shows a partial mechanism.
    # Fix: if rod-group or slider-group is present but crank-group is absent,
    # inject a complete fallback crank SVG into layer-object so every slider-crank
    # question always renders all three components.
    raf_js = data.get("raf_js", "").strip()
    if not raf_js:
        layers_html = data.get("svg_layers", "")
        has_crank  = "crank-group"  in layers_html
        has_rod    = "rod-group"    in layers_html
        has_slider = "slider-group" in layers_html

        # If the mechanism is partially present (rod/slider but NO crank),
        # synthesise a complete crank group and insert it into layer-object.
        if (has_rod or has_slider) and not has_crank:
            Log.warn("SVGSanitizer", "crank-group missing — injecting fallback SVG crank into layer-object")
            _fallback_crank_svg = """<g id="crank-group">
  <!-- fallback crank: pivot at (200,300), r=120 -->
  <defs>
    <linearGradient id="grad-crank-fb" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%"  stop-color="#3b82f6"/>
      <stop offset="100%" stop-color="#1d4ed8"/>
    </linearGradient>
    <filter id="shadow-crank-fb" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="2" dy="3" stdDeviation="4" flood-color="#1d4ed8" flood-opacity="0.22"/>
    </filter>
  </defs>
  <!-- pivot pin -->
  <circle cx="200" cy="300" r="9" fill="#1e293b" stroke="#0ea5e9" stroke-width="2.5"
          filter="url(#shadow-crank-fb)"/>
  <circle cx="200" cy="300" r="4" fill="#38bdf8"/>
  <!-- crank arm (x1="200" y1="300" x2 / y2 are updated by RAF) -->
  <line id="crank-glow" x1="200" y1="300" x2="200" y2="180"
        stroke="#93c5fd" stroke-width="14" stroke-linecap="round" opacity="0.35"/>
  <line id="crank-body" x1="200" y1="300" x2="200" y2="180"
        stroke="url(#grad-crank-fb)" stroke-width="9" stroke-linecap="round"
        filter="url(#shadow-crank-fb)"/>
  <line id="crank-shine" x1="200" y1="300" x2="200" y2="185"
        stroke="rgba(255,255,255,0.45)" stroke-width="3.5" stroke-linecap="round"/>
  <!-- crank-pin (wrist pin A, updated by RAF) -->
  <circle id="crank-pin-outer" cx="200" cy="180" r="9" fill="#1d4ed8"
          stroke="#93c5fd" stroke-width="2.5"/>
  <circle id="crank-pin-inner" cx="200" cy="180" r="4.5" fill="#bfdbfe"/>
  <circle id="crank-pin-shine" cx="198" cy="178" r="2" fill="white" opacity="0.7"/>
  <!-- crank-radius label -->
  <rect x="158" y="228" width="40" height="18" rx="5" fill="white" opacity="0.85"/>
  <text id="crank-label" x="164" y="241" font-family="Inter,sans-serif" font-size="12"
        font-weight="800" fill="#1d4ed8">r</text>
  <!-- ground symbol -->
  <polygon points="200,300 192,316 208,316" fill="#64748b"/>
  <line x1="186" y1="316" x2="214" y2="316" stroke="#475569" stroke-width="2.5"/>
  <line x1="184" y1="320" x2="188" y2="316" stroke="#94a3b8" stroke-width="1.5"/>
  <line x1="190" y1="320" x2="194" y2="316" stroke="#94a3b8" stroke-width="1.5"/>
  <line x1="196" y1="320" x2="200" y2="316" stroke="#94a3b8" stroke-width="1.5"/>
  <line x1="202" y1="320" x2="206" y2="316" stroke="#94a3b8" stroke-width="1.5"/>
  <line x1="208" y1="320" x2="212" y2="316" stroke="#94a3b8" stroke-width="1.5"/>
</g>"""
            # Insert the fallback crank into layer-object (before its closing tag).
            # If layer-object has a </g> closing tag, insert before it; otherwise append.
            svg_lyr = data.get("svg_layers", "")
            import re as _re_crank
            # Find the layer-object group and insert crank just before </g>
            def _insert_crank(m):
                inner = m.group(1)
                return f'<g id="layer-object">{inner}\n{_fallback_crank_svg}\n</g>'
            svg_lyr_new = _re_crank.sub(
                r'<g\s+id=["\']layer-object["\']>(.*?)</g>',
                _insert_crank,
                svg_lyr,
                count=1,
                flags=_re_crank.DOTALL,
            )
            if svg_lyr_new != svg_lyr:
                data["svg_layers"] = svg_lyr_new
                layers_html = svg_lyr_new
                has_crank = True
                Log.ok("SVGSanitizer", "Fallback crank SVG injected into layer-object")
            else:
                # layer-object not found — append a new layer before svg_layers end
                data["svg_layers"] = (
                    f'<g id="layer-object" style="opacity:0">\n{_fallback_crank_svg}\n</g>\n'
                    + svg_lyr
                )
                layers_html = data["svg_layers"]
                has_crank = True
                Log.ok("SVGSanitizer", "Fallback crank SVG prepended as new layer-object")

        if has_crank and has_rod and has_slider:
            Log.ok("SVGSanitizer", "raf_js empty — injecting slider-crank fallback animation")
            data["raf_js"] = """\
window.qanimStartRAF = function() {
  if (window.qanimRafId) cancelAnimationFrame(window.qanimRafId);

  // ── Auto-detect pivot + dimensions from SVG at runtime ─────────────────
  var PX = 200, PY = 300, R = 120, L = 280;
  var stg = document.getElementById('stage');
  if (stg) {
    var pg = stg.querySelector('#layer-object > g[transform], #layer-param1 > g[transform]');
    if (pg) {
      var dm = (pg.getAttribute('transform')||'').match(/translate\\(\\s*([-\\d.]+)[,\\s]+([-\\d.]+)\\)/);
      if (dm) { PX = parseFloat(dm[1]); PY = parseFloat(dm[2]); }
    }
    var cl0 = stg.querySelector('#crank-group line, #crank-body');
    if (cl0) { R = Math.abs(parseFloat(cl0.getAttribute('y2') || '-120')); }
    var rl0 = stg.querySelector('#rod-group line, #rod-body');
    if (rl0) {
      var rx2 = parseFloat(rl0.getAttribute('x2')||'280');
      var rx1 = parseFloat(rl0.getAttribute('x1')||'0');
      L = Math.abs(rx2 - rx1) || L;
    }
  }

  var OMEGA  = 1.5;
  var TARGET = Math.PI / 2;
  var startTime = null, frozenTheta = null, freezeAt = null;
  var _findBadge = null, _thetaAnnot = null;

  // ── Annotation builders ───────────────────────────────────────────────
  function _buildFindBadge(stage) {
    var ns = 'http://www.w3.org/2000/svg';
    var g = document.createElementNS(ns, 'g');
    g.id = 'qanim-find-badge';
    g.style.cssText = 'opacity:0;transition:opacity 0.7s ease;';
    var glow = document.createElementNS(ns, 'rect');
    glow.setAttribute('x','-80'); glow.setAttribute('y','-26');
    glow.setAttribute('width','160'); glow.setAttribute('height','52'); glow.setAttribute('rx','14');
    glow.setAttribute('fill','#fef3c7'); glow.setAttribute('opacity','0.55');
    g.appendChild(glow);
    var r = document.createElementNS(ns, 'rect');
    r.setAttribute('x','-74'); r.setAttribute('y','-22');
    r.setAttribute('width','148'); r.setAttribute('height','44'); r.setAttribute('rx','11');
    r.setAttribute('fill','#fffbeb'); r.setAttribute('stroke','#f59e0b'); r.setAttribute('stroke-width','2.5');
    r.style.filter = 'drop-shadow(0 4px 12px rgba(245,158,11,.35))';
    g.appendChild(r);
    var t = document.createElementNS(ns, 'text');
    t.setAttribute('text-anchor','middle'); t.setAttribute('x','0'); t.setAttribute('y','6');
    t.setAttribute('font-family','Inter,sans-serif'); t.setAttribute('font-size','15');
    t.setAttribute('font-weight','900'); t.setAttribute('fill','#92400e');
    t.textContent = '\u27A4 Find  v = ?';
    g.appendChild(t);
    g.setAttribute('transform', 'translate(' + (PX + L * 0.75) + ',' + (PY - 62) + ')');
    var anchor = stage.querySelector('#layer-derived') || stage.lastElementChild;
    anchor.parentNode.insertBefore(g, anchor.nextSibling);
    _findBadge = g; return g;
  }

  function _buildThetaAnnot(stage) {
    var ns = 'http://www.w3.org/2000/svg';
    var g = document.createElementNS(ns, 'g');
    g.id = 'qanim-theta-annot';
    g.style.cssText = 'opacity:0;transition:opacity 0.7s ease;';
    g.setAttribute('transform', 'translate(' + PX + ',' + PY + ')');
    var sector = document.createElementNS(ns, 'path');
    sector.setAttribute('d','M 0 0 L 50 0 A 50 50 0 0 0 0 -50 Z');
    sector.setAttribute('fill','#6366f1'); sector.setAttribute('fill-opacity','0.08');
    g.appendChild(sector);
    var arc = document.createElementNS(ns, 'path');
    arc.setAttribute('d','M 50 0 A 50 50 0 0 0 0 -50');
    arc.setAttribute('fill','none'); arc.setAttribute('stroke','#6366f1');
    arc.setAttribute('stroke-width','2.5'); arc.setAttribute('stroke-dasharray','6,3');
    g.appendChild(arc);
    var box = document.createElementNS(ns, 'path');
    box.setAttribute('d','M 18 0 L 18 -18 L 0 -18');
    box.setAttribute('fill','none'); box.setAttribute('stroke','#6366f1'); box.setAttribute('stroke-width','2');
    g.appendChild(box);
    var lbg = document.createElementNS(ns, 'rect');
    lbg.setAttribute('x','34'); lbg.setAttribute('y','-66');
    lbg.setAttribute('width','74'); lbg.setAttribute('height','26'); lbg.setAttribute('rx','7');
    lbg.setAttribute('fill','#eef2ff'); lbg.setAttribute('stroke','#818cf8'); lbg.setAttribute('stroke-width','1.5');
    g.appendChild(lbg);
    var t = document.createElementNS(ns, 'text');
    t.setAttribute('x','40'); t.setAttribute('y','-47');
    t.setAttribute('font-family','Inter,sans-serif'); t.setAttribute('font-size','14');
    t.setAttribute('font-weight','800'); t.setAttribute('fill','#4f46e5');
    t.textContent = '\u03b8 = 90\u00b0';
    g.appendChild(t);
    var anchor = stage.querySelector('#layer-param2') || stage.lastElementChild;
    anchor.parentNode.insertBefore(g, anchor.nextSibling);
    _thetaAnnot = g; return g;
  }

  function _showAnnotations(stage, show) {
    if (!_findBadge)  _findBadge  = document.getElementById('qanim-find-badge')  || _buildFindBadge(stage);
    if (!_thetaAnnot) _thetaAnnot = document.getElementById('qanim-theta-annot') || _buildThetaAnnot(stage);
    if (_findBadge)  _findBadge.style.opacity  = show ? '1' : '0';
    if (_thetaAnnot) _thetaAnnot.style.opacity = show ? '1' : '0';
  }

  // ── Core kinematics ──────────────────────────────────────────────────
  function _draw(stage, theta) {
    var cosT = Math.cos(theta), sinT = Math.sin(theta);
    var aX = R * cosT,  aY = -R * sinT;
    var disc = L*L - R*R*sinT*sinT;
    if (disc < 0) disc = 0;
    var bX = R * cosT + Math.sqrt(disc);

    // Crank
    var cg = stage.querySelector('#crank-group');
    if (cg) {
      ['#crank-glow','#crank-body','#crank-shine'].forEach(function(sel) {
        var ln = cg.querySelector(sel);
        if (ln) { ln.setAttribute('x2', aX.toFixed(2)); ln.setAttribute('y2', aY.toFixed(2)); }
      });
      var sh = cg.querySelector('#crank-shine');
      if (sh) { sh.setAttribute('x2',(aX*.96).toFixed(2)); sh.setAttribute('y2',(aY*.96).toFixed(2)); }
      ['#crank-pin-outer','#crank-pin-inner'].forEach(function(sel) {
        var c = cg.querySelector(sel);
        if (c) { c.setAttribute('cx', aX.toFixed(2)); c.setAttribute('cy', aY.toFixed(2)); }
      });
      var ps = cg.querySelector('#crank-pin-shine');
      if (ps) { ps.setAttribute('cx',(aX-2).toFixed(2)); ps.setAttribute('cy',(aY-3).toFixed(2)); }
      var lbl = cg.querySelector('#crank-label, text');
      if (lbl) { lbl.setAttribute('x',(aX/2-30).toFixed(1)); lbl.setAttribute('y',(aY/2+7).toFixed(1)); }
      // fallback: update all circles[1] as crank pin
      var ccs = cg.querySelectorAll('circle');
      if (ccs[1]) { ccs[1].setAttribute('cx',aX.toFixed(2)); ccs[1].setAttribute('cy',aY.toFixed(2)); }
      if (ccs[2]) { ccs[2].setAttribute('cx',aX.toFixed(2)); ccs[2].setAttribute('cy',aY.toFixed(2)); }
      if (ccs[3]) { ccs[3].setAttribute('cx',(aX-2).toFixed(2)); ccs[3].setAttribute('cy',(aY-3).toFixed(2)); }
    }

    // Rod
    var rg = stage.querySelector('#rod-group');
    if (rg) {
      ['#rod-shadow','#rod-body','#rod-cl'].forEach(function(sel) {
        var ln = rg.querySelector(sel);
        if (ln) {
          ln.setAttribute('x1',aX.toFixed(2)); ln.setAttribute('y1',aY.toFixed(2));
          ln.setAttribute('x2',bX.toFixed(2)); ln.setAttribute('y2','0');
        }
      });
      var be = rg.querySelector('#rod-big-end');
      if (be) { be.setAttribute('cx',aX.toFixed(2)); be.setAttribute('cy',aY.toFixed(2)); }
      var se = rg.querySelector('#rod-small-end');
      if (se) { se.setAttribute('cx',bX.toFixed(2)); se.setAttribute('cy','0'); }
      // fallback circles
      var rcs = rg.querySelectorAll('circle');
      if (rcs[0]) { rcs[0].setAttribute('cx',aX.toFixed(2)); rcs[0].setAttribute('cy',aY.toFixed(2)); }
      if (rcs[1]) { rcs[1].setAttribute('cx',aX.toFixed(2)); rcs[1].setAttribute('cy',aY.toFixed(2)); }
      if (rcs[2]) { rcs[2].setAttribute('cx',bX.toFixed(2)); rcs[2].setAttribute('cy','0'); }
      if (rcs[3]) { rcs[3].setAttribute('cx',bX.toFixed(2)); rcs[3].setAttribute('cy','0'); }
      var rlbl = rg.querySelector('#rod-label, text');
      if (rlbl) { rlbl.setAttribute('x',((aX+bX)/2+6).toFixed(1)); rlbl.setAttribute('y',(aY/2-16).toFixed(1)); }
    }

    // A label
    var Albl = stage.querySelector('#lbl-A');
    if (Albl) { Albl.setAttribute('x',(aX+14).toFixed(1)); Albl.setAttribute('y',(aY-10).toFixed(1)); }

    // Slider
    var sg = stage.querySelector('#slider-group');
    if (sg) {
      var tfm = sg.getAttribute('transform') || '';
      if (tfm.indexOf('translate') !== -1) {
        sg.setAttribute('transform','translate('+bX.toFixed(2)+', 0)');
      }
    }

    // Velocity arrow
    var vg = stage.querySelector('#velocity-group');
    if (vg) {
      var denom = Math.sqrt(disc);
      var vel = -R*OMEGA*sinT;
      if (denom > 1) vel -= (R*R*OMEGA*sinT*cosT)/denom;
      var vEnd = bX + vel*0.38;
      var vgl = vg.querySelector('#vel-glow');
      if (vgl) { vgl.setAttribute('x1',bX.toFixed(2)); vgl.setAttribute('x2',vEnd.toFixed(2)); }
      var vl = vg.querySelector('#vel-line, line');
      if (vl) { vl.setAttribute('x1',bX.toFixed(2)); vl.setAttribute('x2',vEnd.toFixed(2)); }
      var mid = (bX+vEnd)/2;
      var vlbg = vg.querySelector('#vel-lbl-bg');
      if (vlbg) { vlbg.setAttribute('x',(mid-33).toFixed(1)); }
      var vlt = vg.querySelector('#vel-label, text');
      if (vlt) { vlt.setAttribute('x',(mid-16).toFixed(1)); }
    } else {
      // fallback: no velocity-group, find line in layer-derived
      var vl2 = stage.querySelector('#layer-derived line');
      if (vl2) {
        var denom2 = Math.sqrt(disc), vel2 = -R*OMEGA*sinT;
        if (denom2 > 1) vel2 -= (R*R*OMEGA*sinT*cosT)/denom2;
        var vEnd2 = bX + vel2*0.38;
        vl2.setAttribute('x1',bX.toFixed(2)); vl2.setAttribute('y1','0');
        vl2.setAttribute('x2',vEnd2.toFixed(2)); vl2.setAttribute('y2','0');
      }
    }
  }

  // ── RAF loop ─────────────────────────────────────────────────────────
  function drawFrame(now) {
    if (!startTime) startTime = now;
    var stage = document.getElementById('stage');
    if (!stage) { window.qanimRafId = requestAnimationFrame(drawFrame); return; }

    var step = Number(window.currentStep) || 0;
    var isStep6 = (step >= 5);
    var theta;

    if (!isStep6) {
      frozenTheta = null; freezeAt = null;
      theta = OMEGA * ((now - startTime) / 1000);
      _showAnnotations(stage, false);
    } else {
      if (freezeAt === null) {
        var rawT = OMEGA * ((now - startTime) / 1000);
        frozenTheta = ((rawT % (2*Math.PI)) + 2*Math.PI) % (2*Math.PI);
        freezeAt = now;
      }
      var elapsed = Math.min((now - freezeAt) / 1100, 1);
      var ease = 1 - Math.pow(1 - elapsed, 4);
      var diff = TARGET - frozenTheta;
      while (diff >  Math.PI) diff -= 2*Math.PI;
      while (diff < -Math.PI) diff += 2*Math.PI;
      theta = frozenTheta + diff * ease;
      _showAnnotations(stage, elapsed > 0.88);
    }

    _draw(stage, theta);
    window.qanimRafId = requestAnimationFrame(drawFrame);
  }

  window.qanimRafId = requestAnimationFrame(drawFrame);
};
if (!window.__qanimRAFStarted) {
  window.__qanimRAFStarted = true;
  document.addEventListener('DOMContentLoaded', function() {
    if (typeof window.qanimStartRAF === 'function') window.qanimStartRAF();
  });
}
"""
    return data

def _build_scene6_html(sol: dict, scene: dict) -> str:
    """Build Scene 7 (Main Formula) HTML — matches reference exactly."""
    formula_raw    = sol.get("formula", "Governing Formula")
    formula_text   = _he(formula_raw)
    formula_attr   = html_module.escape(formula_raw, quote=True)
    formula_name   = _he(sol.get("formula_name", "Formula"))

    variables = sol.get("variables", [])
    var_boxes = ""
    for v in variables:
        # Support both Gemini key names: "symbol" (correct) and "sym" (legacy)
        sym_raw  = v.get("symbol") or v.get("sym") or "?"
        sym  = _he(sym_raw)
        name = _he(v.get("name", "Variable"))
        # Show value + unit together if available
        val_raw  = v.get("value") or v.get("val") or ""
        unit_raw = v.get("unit", "")
        val_disp = _he((val_raw + (" " + unit_raw if unit_raw else "")).strip())
        # Map color string to CSS variant class
        color_map = {
            "blue": "s6v-blue", "green": "s6v-green", "orange": "s6v-orange",
            "red": "s6v-red", "purple": "s6v-purple", "teal": "s6v-teal",
        }
        color_cls = color_map.get(str(v.get("color", "blue")).lower(), "s6v-blue")
        var_boxes += f"""<div class="s6-var-box {color_cls}">
          <div class="s6-var-arrow"></div>
          <div class="s6-var-inner">
            <span class="s6-var-sym">{sym}</span>
            <span class="s6-var-name">{sym} &mdash; {name}</span>
            <span class="s6-var-val" id="s6v-{sym_raw}-val">{val_disp}</span>
          </div>
        </div>\n"""

    note_text = _he(sol.get("note", ""))
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
      <div class="s6-phase-caption" id="s6-phase-caption">This is the governing equation for this problem. Each symbol is explained below.</div>
      <div class="s6-formula-box">
        <div class="s6-formula-badge">Governing Equation</div>
        <div class="s6-formula-main s6-math-formula" id="s6-formula-text"
             data-formula="{formula_attr}">{formula_text}</div>
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
    formula_raw    = sol.get("formula", "Governing Formula")
    formula_recap  = _he(formula_raw)
    formula_attr   = html_module.escape(formula_raw, quote=True)
    chain = sol.get("substitution_chain", [])
    answer_value = _he(sol.get("answer_value", "?"))
    answer_unit = _he(sol.get("answer_unit", ""))
    key_insight = sol.get("key_insight", "Apply the governing formula with the given data.")
    to_find_label = _he(to_find[0] if to_find else "Final Answer")
    final_answer = _he(sol.get("final_answer", "See calculation"))

    chain_html = ""
    for row in chain:
        num = row.get("num", 1)
        eq_raw  = row.get("eq", "")
        eq_attr = html_module.escape(eq_raw, quote=True)
        eq_text = _he(eq_raw)
        # Add a descriptive step label if available
        step_labels = ["Write formula", "Substitute values", "Simplify", "Compute result", "Final answer"]
        step_label = step_labels[num - 1] if (num - 1) < len(step_labels) else f"Step {num}"
        chain_html += (
            f'<div class="s9-sub-row" data-s9-idx="{num-1}">'
            f'<div class="s9-sub-num">{num}</div>'
            f'<div class="s9-sub-eq s9-math-formula" data-formula="{eq_attr}">'
            f'<span class="s9-step-lbl">{step_label}:</span> {eq_text}</div>'
            f'</div>\n'
        )

    return f"""<div id="qanim-scene9-overlay">
  <div class="s9-card">
    <div class="s9-title-bar">
      <h2>&#x2705; Step 9 &mdash; Final Answer</h2>
      <p>{to_find_label}</p>
    </div>
    <div class="s9-body">
      <div class="s9-formula-recap">
        <div class="s9-formula-recap-label">&#x1F4D0; Governing Formula (from Step 7)</div>
        <div class="s9-formula-recap-eq s9-math-formula" id="s9-formula-recap"
             data-formula="{formula_attr}">{formula_recap}</div>
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
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Fira+Code:wght@400;500;600;700&display=swap');

/* ── Topic-adaptive CSS variables (overridden per-render by inline <style>) ── */
:root {
  /* Palette */
  --c-primary:        #0369a1;
  --c-primary-mid:    #0891b2;
  --c-primary-dim:    #0e7490;
  --c-accent:         #38bdf8;
  --c-accent-soft:    rgba(56,189,248,.12);
  --c-orange:         #d97706;
  --c-green:          #16a34a;
  --c-green-soft:     rgba(22,163,74,.10);
  --c-purple:         #7c3aed;
  /* Surfaces */
  --bg-page:          #f0f5fc;
  --bg-page-end:      #e8f0fe;
  --panel-bg:         #ffffff;
  --panel-bg-alt:     #f8fbff;
  --border:           #e2e8f0;
  --border-strong:    #cbd5e1;
  --text-main:        #0f172a;
  --text-sub:         #475569;
  --text-muted:       #94a3b8;
  /* Tokens */
  --radius-card:      18px;
  --radius-sm:        10px;
  --radius-xs:        7px;
  --shadow-card:      0 1px 2px rgba(15,23,42,.04), 0 4px 16px rgba(15,23,42,.07), 0 20px 48px rgba(15,23,42,.05);
  --shadow-deep:      0 8px 32px rgba(15,23,42,.12), 0 2px 8px rgba(15,23,42,.07);
  --ease-spring:      cubic-bezier(.34,1.56,.64,1);
  --ease-smooth:      cubic-bezier(.4,0,.2,1);
  --transition:       .4s var(--ease-smooth);
}

/* ── Reset ───────────────────────────────────────────────────────────────── */
*,*::before,*::after { box-sizing:border-box; margin:0; padding:0; }

body {
  font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;
  background: linear-gradient(155deg, var(--bg-page) 0%, var(--bg-page-end) 55%, #eff6ff 100%);
  background-attachment: fixed;
  color: var(--text-main);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  min-height: 100vh;
  padding: 32px 16px 160px;
  letter-spacing: -.01em;
}

/* ── Page header ─────────────────────────────────────────────────────────── */
.page-header {
  width: 100%; max-width: 900px; margin-bottom: 18px;
  display: flex; align-items: center; gap: 10px;
}
.page-chip {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 6px 14px; border-radius: 24px;
  background: rgba(var(--c-primary-rgb, 3,105,161),.09);
  border: 1px solid rgba(var(--c-primary-rgb, 3,105,161),.20);
  font-size: 10.5px; font-weight: 800; color: var(--c-primary-dim);
  text-transform: uppercase; letter-spacing: 1px;
  backdrop-filter: blur(6px);
}
.page-chip-icon { font-size: 13px; line-height: 1; }
.page-topic-badge {
  margin-left: auto;
  display: inline-flex; align-items: center; gap: 5px;
  padding: 5px 11px; border-radius: 20px;
  background: var(--c-accent-soft);
  border: 1px solid rgba(var(--c-primary-rgb,3,105,161),.18);
  font-size: 10px; font-weight: 800; color: var(--c-primary-dim);
  text-transform: uppercase; letter-spacing: .9px;
}

/* ── Fullscreen button ───────────────────────────────────────────────────── */
#qanim-fullscreen-btn {
  position: fixed; top: 14px; right: 16px; z-index: 9000;
  display: flex; align-items: center; gap: 6px; padding: 8px 15px;
  border-radius: 12px;
  border: 1.5px solid rgba(var(--c-primary-rgb,3,105,161),.25);
  background: rgba(255,255,255,.94); backdrop-filter: blur(14px);
  color: var(--c-primary-dim); font-family: inherit; font-size: 12px; font-weight: 700;
  cursor: pointer;
  box-shadow: 0 4px 16px rgba(var(--c-primary-rgb,3,105,161),.14), 0 1px 4px rgba(0,0,0,.07);
  transition: background .2s, border-color .2s, transform .22s var(--ease-spring), box-shadow .2s;
}
#qanim-fullscreen-btn:hover {
  background: var(--c-accent-soft);
  border-color: var(--c-primary-mid);
  transform: translateY(-2px);
  box-shadow: 0 6px 22px rgba(var(--c-primary-rgb,3,105,161),.24);
}
#qanim-fullscreen-btn .fs-icon { font-size: 14px; line-height: 1; transition: transform .3s; }
#qanim-fullscreen-btn.is-fullscreen { background: var(--c-accent-soft); border-color: var(--c-primary-mid); }
#qanim-fullscreen-btn.is-fullscreen .fs-icon { transform: rotate(180deg); }

/* Fullscreen tweaks */
body.qanim-fullscreen { padding: 0 !important; background: var(--panel-bg) !important; }
body.qanim-fullscreen .dashboard { max-width: 100% !important; border-radius: 0 !important; box-shadow: none !important; border: none !important; height: 100vh; display: flex; flex-direction: column; }
body.qanim-fullscreen .page-header { display: none !important; }
body.qanim-fullscreen .question-banner { display: none !important; }
body.qanim-fullscreen .svg-container { flex: 1 1 auto; aspect-ratio: unset !important; }
body.qanim-fullscreen .step-indicator { display: none !important; }
body.qanim-fullscreen .step-color-legend { display: none !important; }
body.qanim-fullscreen .step-progress-wrap { display: none !important; }
body.qanim-fullscreen .step-label { display: none !important; }
body.qanim-fullscreen #qanim-controls-bar { bottom: 10px; }
body.qanim-fullscreen #qanim-fullscreen-btn { top: 10px; right: 12px; }
body.qanim-fullscreen .control-panel { padding: 12px 20px 14px !important; flex-shrink: 0; }
body.qanim-fullscreen .info-box { min-height: 70px !important; padding: 12px 16px !important; }
body.qanim-fullscreen .actions { margin-top: 10px !important; }

/* ── Dashboard card ──────────────────────────────────────────────────────── */

.dashboard {
  width: 100%; max-width: 900px; margin: 0 auto;
  background: var(--panel-bg);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-card);
  overflow: hidden;
  border: 1px solid var(--border);
  position: relative;
}
.dashboard::before {
  content: ''; position: absolute; top: 0; left: 0; right: 0; height: 3.5px;
  background: linear-gradient(90deg, var(--c-primary-dim) 0%, var(--c-purple) 50%, var(--c-orange) 100%);
  border-radius: var(--radius-card) var(--radius-card) 0 0; z-index: 2;
}

/* ── Question banner ─────────────────────────────────────────────────────── */
.question-banner {
  padding: 22px 28px 18px;
  background: linear-gradient(135deg, var(--panel-bg-alt) 0%, #f0f5ff 40%, var(--bg-page) 100%);
  border-bottom: 1px solid var(--border);
  display: flex; flex-direction: column; gap: 8px;
  position: relative; overflow: hidden;
}
.question-banner::after {
  content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 3.5px;
  background: linear-gradient(to bottom, var(--c-primary-mid), var(--c-accent));
  border-radius: 0 3px 3px 0;
}
.q-label {
  font-size: 10px; font-weight: 800; color: var(--c-primary-dim);
  text-transform: uppercase; letter-spacing: 2px;
  display: flex; align-items: center; gap: 8px;
}
.q-label-dot {
  display: inline-block; width: 16px; height: 16px; border-radius: 5px;
  background: linear-gradient(135deg, var(--c-primary-dim), var(--c-primary-mid));
  flex-shrink: 0;
}
.q-text {
  font-size: 15px; color: var(--text-main); line-height: 1.65;
  font-weight: 450; max-width: 820px;
}

/* ── SVG container ───────────────────────────────────────────────────────── */
.svg-container {
  width: 100%; aspect-ratio: 16/9;
  position: relative; overflow: hidden;
  border-bottom: 1px solid var(--border);
  background: linear-gradient(145deg, #f8fafc 0%, #eef5ff 55%, #f0f6ff 100%);
}
svg { display: block; width: 100%; height: 100%; }
.svg-layer { transition: opacity .55s var(--ease-smooth); }

/* ── Control panel ───────────────────────────────────────────────────────── */
.control-panel {
  padding: 22px 28px 26px;
  background: linear-gradient(180deg, #ffffff 0%, var(--panel-bg-alt) 100%);
  border-top: 1px solid var(--border);
}
.step-color-legend {
  display: flex; gap: 12px; margin-bottom: 14px; flex-wrap: wrap;
}
.step-legend-item {
  display: flex; align-items: center; gap: 4px;
  font-size: 10px; color: var(--text-sub); font-weight: 600;
  text-transform: uppercase; letter-spacing: .6px;
}
.step-legend-dot { width: 8px; height: 8px; border-radius: 50%; }

/* ── Step indicator ──────────────────────────────────────────────────────── */
.step-indicator {
  display: flex; align-items: center; gap: 6px;
  margin-bottom: 18px; flex-wrap: wrap;
}
.step-connector {
  flex: 0 0 14px; height: 1.5px;
  background: linear-gradient(90deg, #cbd5e1, #e2e8f0);
  border-radius: 2px;
}
.step-dot {
  padding: 6px 13px; border-radius: 20px;
  background: #f1f5f9; border: 1.5px solid #e2e8f0;
  font-size: 11.5px; font-weight: 700; color: #94a3b8;
  cursor: pointer; white-space: nowrap; user-select: none;
  position: relative;
  transition: background .28s, color .28s, border-color .28s,
              box-shadow .28s, transform .22s var(--ease-spring);
}
.step-dot:hover:not(.active) {
  background: var(--c-accent-soft);
  border-color: rgba(var(--c-primary-rgb,3,105,161),.28);
  color: var(--c-primary-dim);
  transform: translateY(-2px);
}
.step-dot.active {
  background: linear-gradient(135deg, var(--c-primary-dim) 0%, var(--c-primary-mid) 100%);
  border-color: transparent; color: #fff;
  box-shadow: 0 3px 12px rgba(var(--c-primary-rgb,3,105,161),.38),
              0 1px 3px rgba(var(--c-primary-rgb,3,105,161),.22);
  transform: scale(1.07);
}
.step-dot.done {
  background: var(--c-green-soft);
  border-color: rgba(22,163,74,.28);
  color: #15803d;
}
.step-label {
  font-size: 11px; color: var(--text-muted); font-weight: 600;
  letter-spacing: .6px; text-transform: uppercase;
  margin-left: 6px; flex: 1; min-width: 0;
}
.step-progress-wrap {
  height: 3px; background: #f1f5f9; border-radius: 2px;
  margin-bottom: 22px; overflow: hidden;
}
.step-progress-bar {
  height: 100%;
  background: linear-gradient(90deg, var(--c-primary-dim), var(--c-primary-mid), var(--c-accent));
  border-radius: 2px;
  transition: width .5s var(--ease-smooth);
  width: 0%;
}

/* ── Info box ────────────────────────────────────────────────────────────── */
.info-box {
  background: linear-gradient(135deg, #f8fbff 0%, #f3f8ff 100%);
  border: 1px solid #d8e8f8;
  border-left: 4px solid var(--c-primary-mid);
  border-radius: var(--radius-sm);
  padding: 20px 22px;
  min-height: 130px;
  display: flex; flex-direction: column; gap: 11px;
  position: relative; overflow: hidden;
}
.info-box::before {
  content: ''; position: absolute; top: -30px; right: -30px;
  width: 100px; height: 100px; border-radius: 50%;
  background: radial-gradient(circle, rgba(var(--c-primary-rgb,3,105,161),.06) 0%, transparent 70%);
  pointer-events: none;
}
.info-box h3 {
  color: var(--text-main); font-size: 16px; font-weight: 800;
  display: flex; align-items: center; gap: 10px; line-height: 1.35;
  letter-spacing: -.25px;
}
.info-box h3::before {
  content: ''; display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  background: var(--c-primary-mid); flex-shrink: 0;
  box-shadow: 0 0 0 3px rgba(var(--c-primary-rgb,3,105,161),.18);
}

/* ── Badges ──────────────────────────────────────────────────────────────── */
.badges { display: flex; gap: 7px; flex-wrap: wrap; align-items: center; }
.badge {
  padding: 4px 12px; border-radius: 20px;
  font-size: 11.5px; font-weight: 700;
  display: inline-flex; align-items: center; gap: 5px;
  letter-spacing: .1px; font-family: 'Inter', system-ui, sans-serif;
}
.badge-cyan  { background: rgba(8,145,178,.09);  border: 1px solid rgba(8,145,178,.28);  color: #0e7490; }
.badge-orange{ background: rgba(217,119,6,.09);  border: 1px solid rgba(217,119,6,.28);  color: #92400e; }
.badge-green { background: rgba(22,163,74,.09);  border: 1px solid rgba(22,163,74,.28);  color: #15803d; }
.info-desc { font-size: 14px; line-height: 1.72; color: var(--text-sub); font-weight: 400; }

/* ── Action buttons ──────────────────────────────────────────────────────── */
.actions {
  display: flex; justify-content: flex-end; align-items: center;
  gap: 10px; margin-top: 20px;
}
button {
  padding: 11px 24px; border-radius: 10px;
  font-size: 13.5px; font-weight: 700; font-family: inherit;
  cursor: pointer; border: none; outline: none; letter-spacing: .05px;
  transition: background .22s, box-shadow .22s, transform .2s var(--ease-spring), color .2s, border-color .2s;
}
.btn-primary {
  background: linear-gradient(135deg, var(--c-primary-dim) 0%, var(--c-primary-mid) 100%);
  color: #fff;
  box-shadow: 0 4px 14px rgba(var(--c-primary-rgb,3,105,161),.30), 0 1px 3px rgba(var(--c-primary-rgb,3,105,161),.15);
}
.btn-primary:hover {
  background: linear-gradient(135deg, var(--c-primary) 0%, var(--c-primary-dim) 100%);
  box-shadow: 0 6px 22px rgba(var(--c-primary-rgb,3,105,161),.38);
  transform: translateY(-2px);
}
.btn-secondary {
  background: #fff; color: var(--text-sub);
  border: 1.5px solid var(--border-strong);
  box-shadow: 0 1px 3px rgba(15,23,42,.06);
}
.btn-secondary:hover {
  background: var(--panel-bg-alt); color: var(--text-main);
  border-color: #94a3b8;
  box-shadow: 0 2px 8px rgba(15,23,42,.10);
  transform: translateY(-1px);
}
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
/* ── Fullscreen button ─────────────────────────────────────────────── */
#qanim-fullscreen-btn{position:fixed;top:14px;right:16px;z-index:9000;
  display:flex;align-items:center;gap:6px;padding:8px 14px;
  border-radius:12px;border:1.5px solid rgba(8,145,178,.30);
  background:rgba(255,255,255,.92);backdrop-filter:blur(12px);
  color:var(--accent-cyan-dim);font-family:inherit;font-size:12px;font-weight:700;
  cursor:pointer;box-shadow:0 4px 16px rgba(8,145,178,.18),0 1px 4px rgba(0,0,0,.08);
  transition:background .2s,border-color .2s,color .2s,transform .18s cubic-bezier(.34,1.56,.64,1),box-shadow .2s;}
#qanim-fullscreen-btn:hover{background:rgba(8,145,178,.10);border-color:var(--accent-cyan);
  transform:translateY(-2px);box-shadow:0 6px 22px rgba(8,145,178,.28);}
#qanim-fullscreen-btn .fs-icon{font-size:14px;line-height:1;transition:transform .3s;}
#qanim-fullscreen-btn.is-fullscreen{background:rgba(8,145,178,.12);border-color:var(--accent-cyan);}
#qanim-fullscreen-btn.is-fullscreen .fs-icon{transform:rotate(180deg);}
/* Fullscreen mode tweaks */
body.qanim-fullscreen{padding:0!important;background:var(--panel-bg)!important;}
body.qanim-fullscreen .dashboard{max-width:100%!important;border-radius:0!important;
  box-shadow:none!important;border:none!important;height:100vh;display:flex;flex-direction:column;}
body.qanim-fullscreen .page-header{display:none!important;}
body.qanim-fullscreen .question-banner{display:none!important;}
body.qanim-fullscreen .svg-container{flex:1 1 auto;aspect-ratio:unset!important;}
body.qanim-fullscreen .step-indicator{display:none!important;}
body.qanim-fullscreen .step-color-legend{display:none!important;}
body.qanim-fullscreen .step-progress-wrap{display:none!important;}
body.qanim-fullscreen .step-label{display:none!important;}
body.qanim-fullscreen #qanim-controls-bar{bottom:10px;}
body.qanim-fullscreen #qanim-fullscreen-btn{top:10px;right:12px;}
body.qanim-fullscreen .control-panel{
  padding:12px 20px 14px!important;flex-shrink:0;}
body.qanim-fullscreen .info-box{
  min-height:70px!important;padding:12px 16px!important;}
body.qanim-fullscreen .actions{
  margin-top:10px!important;}
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
  background:linear-gradient(145deg,#f8fafc 0%,#eef5ff 55%,#f0f6ff 100%);
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
/* ── Scene 7: Formula overlay ─────────────────────────────────────────── */
#qanim-scene-modal-backdrop {
  display: none; position: fixed; inset: 0; z-index: 7400;
  background: rgba(10,18,40,.55); backdrop-filter: blur(8px);
  opacity: 0; transition: opacity .28s ease;
}
#qanim-scene-modal-backdrop.qanim-scene-visible { display: block !important; opacity: 1; }
#qanim-scene6-overlay {
  display: none; position: fixed; top: 50%; left: 50%;
  transform: translate(-50%,-50%) scale(.94);
  z-index: 7500; width: min(880px,96vw); max-height: 92vh;
  overflow-y: auto; box-sizing: border-box;
  opacity: 0; pointer-events: none;
  transition: opacity .32s ease, transform .38s var(--ease-spring);
}
#qanim-scene6-overlay.qanim-scene-visible {
  display: block !important; opacity: 1; pointer-events: auto;
  transform: translate(-50%,-50%) scale(1);
}
.s6-card {
  background: var(--panel-bg); border-radius: 22px;
  box-shadow: 0 12px 60px rgba(var(--c-primary-rgb,3,105,161),.16), 0 2px 10px rgba(0,0,0,.09);
  border: 1px solid #dde8f8; overflow: hidden;
  font-family: 'Inter', -apple-system, sans-serif;
}
.s6-title-bar {
  text-align: center; padding: 24px 32px 20px;
  background: linear-gradient(135deg, var(--panel-bg) 0%, var(--panel-bg-alt) 100%);
  border-bottom: 1px solid var(--border);
}
.s6-title-bar h2 {
  font-size: 20px; font-weight: 900; color: var(--text-main); letter-spacing: -.4px;
}
.s6-title-bar h2 .s6-step-chip {
  display: inline-flex; align-items: center; gap: 5px;
  background: var(--c-accent-soft); border: 1px solid rgba(var(--c-primary-rgb,3,105,161),.22);
  color: var(--c-primary-dim); font-size: 11px; font-weight: 800;
  padding: 3px 10px; border-radius: 12px; margin-right: 8px;
  text-transform: uppercase; letter-spacing: .9px;
  vertical-align: middle;
}
.s6-body {
  padding: 28px 32px 24px;
  background: linear-gradient(160deg, var(--bg-page) 0%, var(--bg-page-end) 60%, #eff6ff 100%);
}
.s6-phase-progress {
  font-size: 10.5px; font-weight: 800; letter-spacing: 1.2px;
  text-transform: uppercase; color: var(--c-primary-mid);
  text-align: center; margin-bottom: 4px; min-height: 14px;
}
.s6-phase-caption {
  font-size: 13px; font-weight: 500; color: #334155;
  text-align: center; margin-bottom: 22px; line-height: 1.55;
  min-height: 18px; transition: opacity .3s;
}
.s6-formula-box {
  background: linear-gradient(135deg, #fff 0%, #f0f6ff 100%);
  border: 2.5px solid rgba(59,130,246,.55); border-radius: 18px;
  padding: 22px 32px 18px; text-align: center; margin-bottom: 12px;
  position: relative; overflow: hidden;
  box-shadow: 0 4px 28px rgba(59,130,246,.12), inset 0 1px 0 rgba(255,255,255,.9);
}
.s6-formula-box::before {
  content: ''; position: absolute; inset: 0;
  background: linear-gradient(135deg, rgba(59,130,246,.04) 0%, transparent 60%);
  pointer-events: none;
}
.s6-formula-badge {
  display: inline-block; padding: 3px 11px; border-radius: 20px;
  background: rgba(59,130,246,.10); border: 1px solid rgba(59,130,246,.28);
  font-size: 10px; font-weight: 800; color: #1d4ed8;
  letter-spacing: 1px; text-transform: uppercase; margin-bottom: 12px;
}
.s6-formula-main {
  font-family: 'Cambria Math','STIX Two Math','Times New Roman', serif;
  font-size: 30px; font-weight: 700; color: #1d4ed8;
  letter-spacing: .5px; line-height: 1.5; word-break: break-word;
  opacity: 0; transform: translateY(10px);
  transition: opacity .5s var(--ease-smooth), transform .5s var(--ease-smooth);
  font-style: italic;
}
.s6-formula-main.s6-shown { opacity: 1; transform: translateY(0); }
.s6-formula-sublabel {
  font-size: 11.5px; font-weight: 700; color: #6366f1;
  margin-top: 9px; opacity: 0;
  transition: opacity .4s ease .2s;
  text-transform: uppercase; letter-spacing: 1.2px;
}
.s6-formula-sublabel.s6-shown { opacity: 1; }
.s6-vars-row {
  display: flex; align-items: flex-start; justify-content: center;
  gap: 14px; flex-wrap: wrap; margin-top: 26px;
}
.s6-var-box {
  display: flex; flex-direction: column; align-items: center; gap: 0;
  min-width: 118px; max-width: 158px;
  opacity: 0; transform: translateY(18px);
  transition: opacity .45s var(--ease-smooth), transform .42s var(--ease-spring);
}
.s6-var-box.s6-shown { opacity: 1; transform: translateY(0); }
.s6-var-arrow { width: 2px; height: 26px; position: relative; margin-bottom: 0; }
.s6-var-arrow::before {
  content: ''; position: absolute; left: 50%; transform: translateX(-50%);
  top: 0; width: 2px; height: 20px; border-radius: 1px;
}
.s6-var-arrow::after {
  content: ''; position: absolute; bottom: 0; left: 50%; transform: translateX(-50%);
  border-left: 6px solid transparent; border-right: 6px solid transparent;
}
/* Variable box colour variants */
.s6-var-box.s6v-red    .s6-var-inner{border-color:#f43f5e;background:#fff1f2;} .s6-var-box.s6v-red    .s6-var-sym{color:#be123c;} .s6-var-box.s6v-red    .s6-var-arrow::before{background:#f43f5e;} .s6-var-box.s6v-red    .s6-var-arrow::after{border-top:8px solid #f43f5e;}
.s6-var-box.s6v-orange .s6-var-inner{border-color:#f59e0b;background:#fff7ed;} .s6-var-box.s6v-orange .s6-var-sym{color:#d97706;} .s6-var-box.s6v-orange .s6-var-arrow::before{background:#f59e0b;} .s6-var-box.s6v-orange .s6-var-arrow::after{border-top:8px solid #f59e0b;}
.s6-var-box.s6v-blue   .s6-var-inner{border-color:#3b82f6;background:#eff6ff;} .s6-var-box.s6v-blue   .s6-var-sym{color:#1d4ed8;} .s6-var-box.s6v-blue   .s6-var-arrow::before{background:#3b82f6;} .s6-var-box.s6v-blue   .s6-var-arrow::after{border-top:8px solid #3b82f6;}
.s6-var-box.s6v-green  .s6-var-inner{border-color:#22c55e;background:#f0fdf4;} .s6-var-box.s6v-green  .s6-var-sym{color:#15803d;} .s6-var-box.s6v-green  .s6-var-arrow::before{background:#22c55e;} .s6-var-box.s6v-green  .s6-var-arrow::after{border-top:8px solid #22c55e;}
.s6-var-box.s6v-purple .s6-var-inner{border-color:#a855f7;background:#faf5ff;} .s6-var-box.s6v-purple .s6-var-sym{color:#7c3aed;} .s6-var-box.s6v-purple .s6-var-arrow::before{background:#a855f7;} .s6-var-box.s6v-purple .s6-var-arrow::after{border-top:8px solid #a855f7;}
.s6-var-box.s6v-teal   .s6-var-inner{border-color:#14b8a6;background:#f0fdfa;} .s6-var-box.s6v-teal   .s6-var-sym{color:#0f766e;} .s6-var-box.s6v-teal   .s6-var-arrow::before{background:#14b8a6;} .s6-var-box.s6v-teal   .s6-var-arrow::after{border-top:8px solid #14b8a6;}
.s6-var-inner {
  border: 2px solid; border-radius: 15px; padding: 15px 17px 13px;
  text-align: center; width: 100%; box-sizing: border-box;
  transition: box-shadow .28s var(--ease-spring), transform .28s var(--ease-spring);
}
.s6-var-box.s6-active .s6-var-inner {
  box-shadow: 0 0 0 4px rgba(var(--c-primary-rgb,3,105,161),.20), 0 6px 20px rgba(var(--c-primary-rgb,3,105,161),.22);
  transform: scale(1.06);
}
.s6-var-sym { font-family: 'Fira Code', 'Courier New', monospace; font-size: 23px; font-weight: 900; line-height: 1; display: block; margin-bottom: 6px; }
.s6-var-name { font-size: 11.5px; font-weight: 700; color: #475569; line-height: 1.35; display: block; }
.s6-var-val  { font-size: 10.5px; font-weight: 600; color: #94a3b8; margin-top: 4px; display: block; font-family: 'Fira Code', monospace; }
.s6-note-bar {
  display: flex; align-items: center; justify-content: center; gap: 9px;
  margin-top: 24px; padding: 13px 22px;
  background: linear-gradient(135deg, #fffbeb, #fef9c3);
  border-radius: 14px; border: 1.5px solid #fde68a;
  opacity: 0; transform: translateY(8px);
  transition: opacity .45s var(--ease-smooth), transform .45s var(--ease-spring);
  box-shadow: 0 2px 8px rgba(245,158,11,.12);
}
.s6-note-bar.s6-shown { opacity: 1; transform: translateY(0); }
.s6-note-icon { font-size: 17px; flex-shrink: 0; }
.s6-note-text { font-size: 13px; font-weight: 700; color: #92400e; }
.s6-nav-row {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  padding: 18px 32px 24px; border-top: 1px solid var(--border);
  background: var(--panel-bg);
}
#qanim-scene-modal-backdrop{display:none;position:fixed;inset:0;z-index:7400;background:rgba(15,23,42,.50);backdrop-filter:blur(6px);opacity:0;transition:opacity .25s ease;}
#qanim-scene-modal-backdrop.qanim-scene-visible{display:block!important;opacity:1;}
#qanim-scene6-overlay{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) scale(.95);z-index:7500;width:min(860px,96vw);max-height:92vh;overflow-y:auto;box-sizing:border-box;opacity:0;pointer-events:none;transition:opacity .3s ease,transform .3s cubic-bezier(.34,1.56,.64,1);}
#qanim-scene6-overlay.qanim-scene-visible{display:block!important;opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.s6-card{background:#fff;border-radius:20px;box-shadow:0 8px 48px rgba(8,145,178,.14),0 2px 8px rgba(0,0,0,.08);border:1px solid #dde8f8;overflow:hidden;font-family:-apple-system,'Segoe UI',Arial,sans-serif;}
.s6-title-bar{text-align:center;padding:22px 28px 18px;background:#fff;border-bottom:1px solid #e8eef8;}
.s6-title-bar h2{font-size:20px;font-weight:900;color:#0f172a;letter-spacing:-.3px;}
.s6-body{padding:28px 32px 24px;background:linear-gradient(160deg,#eef2f9 0%,#e8f0fe 50%,#eff6ff 100%);}
.s6-formula-box{background:#fff;border:2.5px solid #3b82f6;border-radius:18px;padding:20px 32px 16px;text-align:center;margin-bottom:10px;position:relative;box-shadow:0 4px 24px rgba(59,130,246,.12);}
.s6-formula-badge{display:inline-block;padding:3px 10px;border-radius:20px;background:rgba(59,130,246,.10);border:1px solid rgba(59,130,246,.28);font-size:10px;font-weight:800;color:#1d4ed8;letter-spacing:1px;text-transform:uppercase;margin-bottom:10px;}
.s6-formula-main{font-family:'Cambria Math','STIX Two Math','Times New Roman',serif;font-size:30px;font-weight:700;color:#1d4ed8;letter-spacing:.5px;line-height:1.5;word-break:break-word;opacity:0;transform:translateY(8px);transition:opacity .5s,transform .5s;font-style:italic;}
.s6-formula-main.s6-shown{opacity:1;transform:translateY(0);}
.s6-formula-sublabel{font-size:12px;font-weight:700;color:#6366f1;letter-spacing:.3px;margin-top:8px;opacity:0;transition:opacity .4s ease .2s;text-transform:uppercase;letter-spacing:1.2px;}
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

/* ── Step-6 "To Find" floating badge (Update 2) ──────────────────────────── */
/* Replaces the old two-column Given/ToFind card.                             */
/* Shows a small, elegant badge directly on the SVG so the unknown is visible */
/* in context — without covering the diagram.                                 */
#step6-info-panel {
  position: absolute;
  bottom: 18px; right: 18px;
  z-index: 10;
  pointer-events: none; opacity: 0;
  transition: opacity .45s cubic-bezier(.4,0,.2,1);
}
#step6-info-panel.s6info-visible { opacity: 1; pointer-events: auto; }

.s6tofind-badge {
  background: rgba(10,22,44,.82);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1.5px solid rgba(74,222,128,.42);
  border-radius: 16px;
  padding: 14px 18px 12px;
  min-width: 180px;
  max-width: 280px;
  box-shadow: 0 0 0 1px rgba(74,222,128,.14), 0 8px 32px rgba(74,222,128,.22), 0 2px 8px rgba(0,0,0,.38);
}
.s6tofind-heading {
  font-family: 'Inter','Segoe UI',system-ui,sans-serif;
  font-size: 9.5px; font-weight: 900;
  text-transform: uppercase; letter-spacing: 1.8px;
  color: #4ade80; margin-bottom: 10px;
  display: flex; align-items: center; gap: 4px;
}
.s6tofind-chip {
  display: flex; align-items: center; gap: 9px;
  background: rgba(74,222,128,.13);
  border: 1.5px solid rgba(74,222,128,.32);
  border-radius: 10px; padding: 9px 13px; margin-bottom: 8px;
}
.s6tofind-icon { font-size: 15px; flex-shrink: 0; }
.s6tofind-label {
  font-family: 'Fira Code','Courier New',monospace;
  font-size: 14px; font-weight: 800; color: #4ade80;
  letter-spacing: .3px;
}
.s6tofind-hint {
  display: flex; align-items: center; gap: 6px;
  font-family: 'Inter','Segoe UI',system-ui,sans-serif;
  font-size: 11px; color: #94a3b8;
  margin-top: 4px; padding: 6px 10px;
  border: 1px dashed rgba(148,163,184,.32); border-radius: 8px;
  background: rgba(148,163,184,.07);
}
.s6tofind-hint strong { color: #cbd5e1; }
"""

_SCENE7_CSS = """
/* ── Scene 8: Substitution overlay ───────────────────────────────────── */
#qanim-scene7-overlay {
  display: none; position: fixed; top: 50%; left: 50%;
  transform: translate(-50%,-50%) scale(.94);
  z-index: 7500; width: min(920px,96vw); max-height: 92vh;
  overflow-y: auto; box-sizing: border-box;
  opacity: 0; pointer-events: none;
  transition: opacity .32s ease, transform .38s var(--ease-spring);
}
#qanim-scene7-overlay.qanim-scene-visible {
  display: block !important; opacity: 1; pointer-events: auto;
  transform: translate(-50%,-50%) scale(1);
}
.s7-card {
  background: var(--panel-bg); border-radius: 22px;
  box-shadow: 0 12px 60px rgba(37,99,235,.14), 0 2px 10px rgba(0,0,0,.08);
  border: 1px solid #e0eaf8; overflow: hidden;
  font-family: 'Inter', -apple-system, sans-serif;
}
.s7-title-bar {
  text-align: center; padding: 22px 32px 18px;
  border-bottom: 1px solid var(--border); background: var(--panel-bg);
}
.s7-title-bar h2 {
  font-size: 20px; font-weight: 900; color: var(--text-main); letter-spacing: -.4px;
}
.s7-body-cols { display: flex; align-items: flex-start; gap: 0; min-height: 340px; }
.s7-left-col {
  width: 42%; min-width: 200px; border-right: 1.5px solid var(--border);
  padding: 24px 22px 24px 28px;
  background: linear-gradient(175deg, #eff6ff 0%, #dbeafe 55%, #bfdbfe 100%);
  display: flex; flex-direction: column; gap: 0; align-self: stretch;
}
.s7-system-label {
  font-size: 10px; font-weight: 800; color: #1d4ed8;
  text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 11px;
}
.s7-system-visual {
  background: linear-gradient(135deg, #bfdbfe 0%, #93c5fd 50%, #60a5fa 100%);
  border-radius: 14px; padding: 18px 16px 15px; margin-bottom: 18px;
  text-align: center; position: relative; overflow: hidden;
  box-shadow: 0 4px 16px rgba(59,130,246,.20), inset 0 1px 0 rgba(255,255,255,.4);
}
.s7-system-visual::before {
  content: ''; position: absolute; inset: 0;
  background: radial-gradient(ellipse at 30% 30%, rgba(255,255,255,.35) 0%, transparent 60%);
  pointer-events: none;
}
.s7-system-visual-title { font-size: 13.5px; font-weight: 800; color: #1e3a5f; margin-bottom: 7px; position: relative; }
.s7-system-arrows { display: flex; justify-content: center; gap: 10px; margin: 9px 0; font-size: 22px; color: #d97706; position: relative; }
.s7-system-label2 { font-size: 10.5px; font-weight: 600; color: #1e40af; margin-top: 5px; position: relative; }
.s7-right-col { flex: 1; padding: 24px 28px 22px 22px; display: flex; flex-direction: column; gap: 18px; }
.s7-given-section-title { font-size: 12.5px; font-weight: 900; color: #1d4ed8; margin-bottom: 9px; letter-spacing: -.1px; }
.s7-given-list { display: flex; flex-direction: column; gap: 5px; margin-bottom: 14px; }
.s7-given-item {
  font-size: 12.5px; color: #334155; line-height: 1.55;
  display: flex; align-items: flex-start; gap: 7px;
  padding: 5px 0;
}
.s7-given-item::before { content: '•'; color: #3b82f6; font-weight: 900; flex-shrink: 0; margin-top: 1px; }
.s7-given-item strong { font-weight: 700; color: #1e293b; font-family: 'Fira Code', monospace; }
.s7-approach-section-title { font-size: 12.5px; font-weight: 900; color: #7c3aed; margin-bottom: 9px; }
.s7-approach-list { display: flex; flex-direction: column; gap: 7px; margin-bottom: 14px; }
.s7-approach-step {
  display: flex; align-items: flex-start; gap: 10px;
  font-size: 12.5px; color: #1e293b; line-height: 1.55;
  background: #f8fafc; border: 1px solid var(--border);
  border-radius: 9px; padding: 10px 13px;
}
.s7-approach-step-num {
  font-weight: 900; color: #fff; flex-shrink: 0; min-width: 22px; height: 22px;
  background: linear-gradient(135deg, #7c3aed, #6d28d9);
  border-radius: 50%; display: flex; align-items: center; justify-content: center;
  font-size: 10.5px; box-shadow: 0 2px 6px rgba(124,58,237,.35);
  margin-top: 1px;
}
.s7-approach-step-eq {
  display: block; margin-top: 5px;
  font-family: 'Fira Code', 'Courier New', monospace;
  font-size: 12px; font-weight: 600; color: #dc2626;
  background: #fff7ed; border-radius: 6px; padding: 3px 9px;
  word-break: break-word; border: 1px solid #fed7aa;
}
.s7-formula-result-bar {
  background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%);
  border: 2px solid #86efac; border-radius: 13px; padding: 13px 18px;
  box-shadow: 0 2px 10px rgba(22,163,74,.12);
}
.s7-formula-result-text {
  font-family: 'Fira Code', 'Courier New', monospace;
  font-size: 14px; font-weight: 900; color: #15803d; line-height: 1.55; word-break: break-word;
}
.s7-formula-units { font-size: 11px; color: #166534; margin-top: 4px; font-style: italic; }
.s7-nav-row {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  padding: 18px 28px 24px; border-top: 1px solid var(--border); background: var(--panel-bg);
}
@media(max-width:600px) {
  .s7-body-cols { flex-direction: column; }
  .s7-left-col { width: 100%; border-right: none; border-bottom: 1.5px solid var(--border); }
}
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
/* ── Scene 9: Final Answer overlay ───────────────────────────────────── */
#qanim-scene9-overlay {
  display: none; position: fixed; top: 50%; left: 50%;
  transform: translate(-50%,-50%) scale(.94);
  z-index: 7500; width: min(800px,96vw); max-height: 92vh;
  overflow-y: auto; box-sizing: border-box;
  opacity: 0; pointer-events: none;
  transition: opacity .32s ease, transform .38s var(--ease-spring);
}
#qanim-scene9-overlay.qanim-scene-visible {
  display: block !important; opacity: 1; pointer-events: auto;
  transform: translate(-50%,-50%) scale(1);
}
.s9-card {
  background: var(--panel-bg); border-radius: 22px;
  box-shadow: 0 12px 60px rgba(22,163,74,.22), 0 2px 10px rgba(0,0,0,.09);
  border: 2px solid #86efac; overflow: hidden;
  font-family: 'Inter', -apple-system, sans-serif;
}
.s9-title-bar {
  text-align: center; padding: 24px 32px 20px;
  background: linear-gradient(135deg, #f0fdf4 0%, #d1fae5 50%, #a7f3d0 100%);
  border-bottom: 2px solid #86efac; position: relative; overflow: hidden;
}
.s9-title-bar::before {
  content: ''; position: absolute; inset: 0;
  background: radial-gradient(ellipse at 50% 0%, rgba(255,255,255,.5) 0%, transparent 70%);
  pointer-events: none;
}
.s9-title-bar h2 {
  font-size: 22px; font-weight: 900; color: #14532d; letter-spacing: -.4px; margin-bottom: 5px;
  position: relative;
}
.s9-title-bar p { font-size: 13px; color: #166534; margin: 0; font-weight: 500; position: relative; }
.s9-body { padding: 32px 36px 28px; background: var(--panel-bg); display: flex; flex-direction: column; gap: 22px; }
.s9-formula-recap {
  background: linear-gradient(135deg, #eff6ff 0%, #e0f2fe 100%);
  border: 1.5px solid #bfdbfe; border-radius: 13px; padding: 15px 22px; text-align: center;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.8);
}
.s9-formula-recap-label {
  font-size: 10px; font-weight: 800; color: #1d4ed8;
  text-transform: uppercase; letter-spacing: 1.3px; margin-bottom: 7px;
}
.s9-formula-recap-eq {
  font-family: 'Cambria Math','STIX Two Math','Times New Roman', serif;
  font-size: 20px; font-weight: 700; color: #1d4ed8; font-style: italic;
}
.s9-sub-chain { display: flex; flex-direction: column; gap: 10px; }
.s9-sub-row {
  display: flex; align-items: center; gap: 13px;
  background: var(--panel-bg-alt); border: 1px solid var(--border);
  border-radius: 11px; padding: 13px 18px;
  opacity: 0; transform: translateX(-22px);
  transition: opacity .45s var(--ease-spring), transform .45s var(--ease-spring);
  box-shadow: 0 1px 4px rgba(15,23,42,.05);
}
.s9-sub-row.s9-shown { opacity: 1; transform: translateX(0); }
.s9-sub-num {
  background: linear-gradient(135deg, var(--c-primary-mid), var(--c-primary-dim));
  color: #fff; border-radius: 50%; width: 30px; height: 30px;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 800; flex-shrink: 0;
  box-shadow: 0 2px 8px rgba(var(--c-primary-rgb,3,105,161),.35);
}
.s9-sub-eq {
  font-family: 'Cambria Math','STIX Two Math','Times New Roman', serif;
  font-size: 16px; font-weight: 600; color: var(--text-main); flex: 1; font-style: italic;
}
.s9-final-box {
  background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 60%, #bbf7d0 100%);
  border: 3px solid #22c55e; border-radius: 20px; padding: 30px 36px;
  text-align: center; position: relative; overflow: hidden;
  opacity: 0; transform: scale(.93);
  transition: opacity .55s ease .3s, transform .55s var(--ease-spring) .3s;
  box-shadow: 0 8px 40px rgba(22,163,74,.20), inset 0 1px 0 rgba(255,255,255,.8);
}
.s9-final-box::before {
  content: ''; position: absolute; inset: 0;
  background: radial-gradient(ellipse at 50% 0%, rgba(255,255,255,.55) 0%, transparent 65%);
  pointer-events: none;
}
.s9-final-box.s9-shown { opacity: 1; transform: scale(1); }
.s9-final-label {
  font-size: 11px; font-weight: 900; text-transform: uppercase;
  letter-spacing: 2.2px; color: #15803d; margin-bottom: 13px;
}
.s9-final-value {
  font-family: 'Cambria Math','STIX Two Math','Times New Roman', serif;
  font-size: 38px; font-weight: 700; color: #14532d; line-height: 1.2;
  position: relative;
}
.s9-final-value .s9-highlight {
  color: #16a34a; font-size: 50px; font-weight: 900;
  display: inline-block;
  animation: s9-pulse-value 2.4s ease-in-out infinite;
}
@keyframes s9-pulse-value {
  0%,100% { text-shadow: 0 0 0 transparent; }
  50% { text-shadow: 0 0 28px rgba(22,163,74,.45), 0 0 10px rgba(22,163,74,.25); }
}
.s9-final-unit {
  font-size: 15px; color: #166534; margin-top: 9px;
  font-weight: 700; letter-spacing: .5px;
}
.s9-step-lbl {
  font-family: 'Inter', system-ui, sans-serif;
  font-size: 10.5px; font-weight: 800; color: #64748b;
  text-transform: uppercase; letter-spacing: .9px; font-style: normal;
  display: inline-block; margin-right: 5px;
}
.s9-insight-bar {
  display: flex; align-items: flex-start; gap: 11px;
  background: linear-gradient(135deg, #fff7ed, #ffedd5);
  border: 1.5px solid #fed7aa; border-radius: 12px; padding: 14px 20px;
  opacity: 0; transition: opacity .55s ease .75s;
  box-shadow: 0 2px 10px rgba(217,119,6,.10);
}
.s9-insight-bar.s9-shown { opacity: 1; }
.s9-insight-icon { font-size: 22px; flex-shrink: 0; }
.s9-insight-text { font-size: 13px; color: #92400e; line-height: 1.68; }
.s9-insight-text strong { color: #78350f; font-weight: 800; }
.s9-nav-row {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  padding: 18px 36px 24px; border-top: 1px solid #bbf7d0;
  background: linear-gradient(135deg, #f0fdf4, #dcfce7);
}
#qanim-scene9-overlay{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) scale(.95);z-index:7500;width:min(780px,96vw);max-height:92vh;overflow-y:auto;box-sizing:border-box;opacity:0;pointer-events:none;transition:opacity .3s,transform .3s cubic-bezier(.34,1.56,.64,1);}
#qanim-scene9-overlay.qanim-scene-visible{display:block!important;opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.s9-card{background:#fff;border-radius:20px;box-shadow:0 8px 48px rgba(22,163,74,.18),0 2px 8px rgba(0,0,0,.08);border:2px solid #86efac;overflow:hidden;font-family:-apple-system,'Segoe UI',Arial,sans-serif;}
.s9-title-bar{text-align:center;padding:22px 28px 18px;background:linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);border-bottom:2px solid #86efac;}
.s9-title-bar h2{font-size:22px;font-weight:900;color:#14532d;letter-spacing:-.3px;margin-bottom:4px;}
.s9-title-bar p{font-size:13px;color:#166534;margin:0;}
.s9-body{padding:32px 36px 28px;background:#fff;display:flex;flex-direction:column;gap:22px;}
.s9-formula-recap{background:#eff6ff;border:1.5px solid #bfdbfe;border-radius:12px;padding:14px 20px;text-align:center;}
.s9-formula-recap-label{font-size:10.5px;font-weight:800;color:#1d4ed8;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:6px;}
.s9-formula-recap-eq{font-family:'Cambria Math','STIX Two Math','Times New Roman',serif;font-size:20px;font-weight:700;color:#1d4ed8;font-style:italic;}
.s9-sub-chain{display:flex;flex-direction:column;gap:10px;}
.s9-sub-row{display:flex;align-items:center;gap:12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 16px;opacity:0;transform:translateX(-18px);transition:opacity .45s cubic-bezier(.34,1.56,.64,1),transform .45s cubic-bezier(.34,1.56,.64,1);}
.s9-sub-row.s9-shown{opacity:1;transform:translateX(0);}
.s9-sub-num{background:linear-gradient(135deg,#0891b2,#0e7490);color:#fff;border-radius:50%;width:28px;height:28px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;flex-shrink:0;box-shadow:0 2px 6px rgba(8,145,178,.35);}
.s9-sub-eq{font-family:'Cambria Math','STIX Two Math','Times New Roman',serif;font-size:16px;font-weight:600;color:#1e293b;flex:1;font-style:italic;}
.s9-final-box{background:linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);border:3px solid #22c55e;border-radius:18px;padding:28px 32px;text-align:center;position:relative;overflow:hidden;opacity:0;transform:scale(.94);transition:opacity .5s ease .3s,transform .5s cubic-bezier(.34,1.56,.64,1) .3s;}
.s9-final-box.s9-shown{opacity:1;transform:scale(1);}
.s9-final-label{font-size:11px;font-weight:900;text-transform:uppercase;letter-spacing:2px;color:#15803d;margin-bottom:12px;}
.s9-final-value{font-family:'Cambria Math','STIX Two Math','Times New Roman',serif;font-size:36px;font-weight:700;color:#14532d;line-height:1.2;}
.s9-final-value .s9-highlight{color:#16a34a;font-size:46px;font-weight:900;}
.s9-final-unit{font-size:15px;color:#166534;margin-top:8px;font-weight:700;letter-spacing:.5px;}
.s9-step-lbl{font-family:'Segoe UI',system-ui,sans-serif;font-size:11px;font-weight:800;color:#64748b;text-transform:uppercase;letter-spacing:.8px;font-style:normal;display:inline-block;margin-right:4px;}
.s9-insight-bar{display:flex;align-items:flex-start;gap:10px;background:#fff7ed;border:1.5px solid #fed7aa;border-radius:10px;padding:13px 18px;opacity:0;transition:opacity .5s ease .7s;}
.s9-insight-bar.s9-shown{opacity:1;}
.s9-insight-icon{font-size:20px;flex-shrink:0;}
.s9-insight-text{font-size:13px;color:#92400e;line-height:1.65;}
.s9-insight-text strong{color:#78350f;}
.s9-nav-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:16px 36px 22px;border-top:1px solid #bbf7d0;background:#f0fdf4;}
"""

_CONTROLS_CSS = """
/* ── Controls bar ─────────────────────────────────────────────────────────── */
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
  // Reset hook: called by __qanimSetAnswerTargets after Customize updates the JSON element.
  // Clears the internal _loaded flag so the next openAnswerBox() re-reads _targets from DOM.
  window.__qanimAnswerBoxReset=function(){_loaded=false;_targets=[];_currentIdx=0;};
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
# Customize Panel
# ===========================================================================

_CUSTOMIZE_CSS = """
<style id="qanim-customize-styles">
#customize-backdrop {
  display:none; position:fixed; inset:0; z-index:8800;
  background:rgba(15,23,42,.52); backdrop-filter:blur(5px);
}
#customize-backdrop.open { display:block; }

#customize-panel {
  display:flex; flex-direction:column;
  position:fixed; top:50%; left:50%;
  transform:translate(-50%,-50%) scale(.96);
  z-index:8900; width:min(520px,95vw); max-height:88vh;
  border-radius:20px; overflow:hidden;
  background:#fff;
  border:1px solid #e2e8f0;
  box-shadow:0 8px 56px rgba(99,102,241,.22),0 2px 10px rgba(0,0,0,.10);
  opacity:0; pointer-events:none;
  transition:opacity .26s, transform .26s cubic-bezier(.34,1.56,.64,1);
}
#customize-panel.open {
  opacity:1; pointer-events:auto;
  transform:translate(-50%,-50%) scale(1);
}
.cust-header {
  display:flex; align-items:center; justify-content:space-between;
  padding:16px 22px;
  background:linear-gradient(135deg,#f5f3ff,#eff6ff);
  border-bottom:1px solid #e2e8f0; flex-shrink:0;
}
.cust-header-title {
  font-size:16px; font-weight:800; color:#1e293b;
  display:flex; align-items:center; gap:8px;
}
.cust-header-badge {
  font-size:10px; font-weight:800; text-transform:uppercase;
  letter-spacing:1px; padding:2px 9px; border-radius:20px;
  background:rgba(99,102,241,.12); border:1px solid rgba(99,102,241,.28);
  color:#4338ca;
}
.cust-close-btn {
  width:30px; height:30px; border-radius:8px;
  border:1px solid #e2e8f0; background:#f8fafc;
  color:#64748b; font-size:13px; cursor:pointer;
  display:flex; align-items:center; justify-content:center;
  transition:background .15s; padding:0;
}
.cust-close-btn:hover { background:#fee2e2; color:#dc2626; }
.cust-body {
  padding:20px 22px; overflow-y:auto; flex:1 1 auto;
  display:flex; flex-direction:column; gap:0;
}
.cust-section-title {
  font-size:10px; font-weight:800; text-transform:uppercase;
  letter-spacing:1.4px; color:#6366f1; margin-bottom:12px;
  display:flex; align-items:center; gap:6px;
}
.cust-section-title::after {
  content:''; flex:1; height:1px;
  background:linear-gradient(90deg,rgba(99,102,241,.25),transparent);
}
.cust-field-grid {
  display:grid; grid-template-columns:1fr 1fr; gap:12px;
  margin-bottom:18px;
}
.cust-field { display:flex; flex-direction:column; gap:4px; }
.cust-field label {
  font-size:11.5px; font-weight:700; color:#475569;
  display:flex; align-items:center; gap:5px;
}
.cust-field label .cust-sym {
  font-family:'Fira Code','Courier New',monospace;
  font-weight:900; font-size:13px; color:#0e7490;
}
.cust-field input {
  padding:9px 12px; border-radius:9px;
  border:1.5px solid #e2e8f0; background:#f8fafc;
  font-family:inherit; font-size:13.5px; font-weight:600;
  color:#1e293b; outline:none;
  transition:border-color .15s,background .15s;
  box-sizing:border-box; width:100%;
}
.cust-field input:focus {
  border-color:#6366f1; background:#fff;
  box-shadow:0 0 0 3px rgba(99,102,241,.12);
}
.cust-field .cust-unit { font-size:10.5px; color:#94a3b8; margin-top:1px; }
.cust-preview-box {
  background:linear-gradient(135deg,#f5f3ff,#eff6ff);
  border:1.5px solid rgba(99,102,241,.28);
  border-radius:12px; padding:14px 16px; margin-bottom:16px;
}
.cust-preview-title {
  font-size:10.5px; font-weight:800; text-transform:uppercase;
  letter-spacing:1.2px; color:#4338ca; margin-bottom:10px;
}
.cust-preview-grid { display:grid; grid-template-columns:1fr 1fr; gap:7px; }
.cust-preview-item {
  display:flex; align-items:center; gap:6px;
  font-size:12.5px; color:#334155;
}
.cust-preview-item .cpv-sym {
  font-weight:800; color:#1e293b;
  font-family:'Fira Code','Courier New',monospace; font-size:13px;
}
.cust-preview-item .cpv-arrow { color:#6366f1; font-size:11px; font-weight:700; }
.cust-result-bar {
  background:linear-gradient(135deg,#f0fdf4,#dcfce7);
  border:1.5px solid #86efac; border-radius:12px;
  padding:13px 16px; margin-bottom:16px; display:none;
}
.cust-result-bar.visible { display:block; }
.cust-result-title {
  font-size:10.5px; font-weight:800; text-transform:uppercase;
  letter-spacing:1.2px; color:#15803d; margin-bottom:7px;
}
.cust-result-values {
  font-size:14px; font-weight:700; color:#14532d;
  font-family:'Fira Code','Courier New',monospace;
}
.cust-error-bar {
  background:#fef2f2; border:1.5px solid #fecaca;
  border-radius:10px; padding:10px 14px;
  font-size:12.5px; color:#b91c1c; font-weight:600;
  margin-bottom:14px; display:none;
}
.cust-error-bar.visible { display:block; }
.cust-footer {
  display:flex; gap:10px; justify-content:flex-end;
  padding:16px 22px; border-top:1px solid #f1f5f9;
  background:#fafbff; flex-shrink:0;
}
.cust-btn-reset {
  padding:10px 20px; border-radius:10px; border:1.5px solid #e2e8f0;
  background:#fff; color:#64748b; font-size:13px; font-weight:700;
  font-family:inherit; cursor:pointer;
  transition:background .15s,border-color .15s,color .15s;
}
.cust-btn-reset:hover { background:#f8fafc; border-color:#94a3b8; color:#334155; }
.cust-btn-apply {
  padding:10px 24px; border-radius:10px; border:none;
  background:linear-gradient(135deg,#4f46e5,#6366f1);
  color:#fff; font-size:13px; font-weight:700; font-family:inherit;
  cursor:pointer; box-shadow:0 3px 12px rgba(99,102,241,.35);
  transition:background .15s,transform .12s,box-shadow .15s;
}
.cust-btn-apply:hover {
  background:linear-gradient(135deg,#4338ca,#4f46e5);
  transform:translateY(-1px); box-shadow:0 5px 18px rgba(99,102,241,.42);
}
.cust-btn-apply:active { transform:translateY(0); }
@keyframes cust-pulse-ring {
  0%   { box-shadow:0 0 0 0 rgba(99,102,241,.5); }
  70%  { box-shadow:0 0 0 8px rgba(99,102,241,0); }
  100% { box-shadow:0 0 0 0 rgba(99,102,241,0); }
}
.cust-applied-ring { animation:cust-pulse-ring .7s ease-out; }
</style>
"""


def _build_customize_html(sol: dict, scene: dict) -> str:
    """Build the Customize panel HTML + JS for live value editing.

    Reads sol["customize"] produced by Gemini. Falls back gracefully to a
    synthesised version from sol["variables"] if the field is absent — so the
    Customize panel is always shown when there are known given values.
    Returns the combined CSS + panel HTML + JS string.
    """
    import re as _re_cust

    cust = sol.get("customize") or {}
    fields = cust.get("fields") or []
    compute_js_body = cust.get("compute_js", "") or ""
    question_template = cust.get("question_template", "") or ""

    # ── Bug 1 fix: auto-synthesise customize from variables when Gemini omits it ─
    # Root cause: Gemini sometimes returns a solution without the "customize" key
    # (or with an empty "fields" list). _build_customize_html then returns only the
    # CSS, has_customize stays False, and the Customize button is never injected.
    # Fix: if fields is still empty, build a basic one from sol["variables"].
    # We accept ALL variables that are NOT explicitly marked as unknown/answer
    # (color "green" or value containing "?") as editable given fields.
    if not fields:
        variables = sol.get("variables") or []
        for v in variables:
            color = str(v.get("color", "blue")).lower()
            val_str = str(v.get("value") or v.get("val") or "")
            # Skip the unknown/answer variable
            if color in ("green",) or "?" in val_str or "to find" in val_str.lower():
                continue
            raw_id = str(v.get("symbol") or v.get("sym") or "v")
            # make a safe JS identifier
            safe_id = _re_cust.sub(r'[^a-zA-Z0-9_]', '_', raw_id).strip('_') or "v"
            try:
                default_val = float(val_str.replace("?", "").split()[0])
            except (ValueError, IndexError):
                # Symbolic value (e.g. "M", "R", "h") — keep as 1.0 placeholder
                default_val = 1.0
            fields.append({
                "id":      safe_id,
                "symbol":  raw_id,
                "label":   str(v.get("name", raw_id)),
                "unit":    str(v.get("unit", "")),
                "default": default_val,
            })
        if fields and not compute_js_body:
            # Build a generic compute that returns the answer_value as a number
            compute_js_body = (
                "var ans = parseFloat('" + str(sol.get("answer_value", "0")).replace("'", "") + "');"
                " return { answer: _fmt(ans), answer_unit: '" +
                str(sol.get("answer_unit", "")).replace("'", "") + "',"
                " answer_label: '" + str(sol.get("formula", "Answer")).replace("'", "")[:40] + "',"
                " derived: {} };"
            )
        if fields and not question_template:
            question_template = sol.get("formula", "") or ""

    # ── Bug 1 fix: Replace greedy regex wrapper strip with brace-depth counter ─
    # Root cause: the greedy regex `(.*)\}\s*$` works most of the time, but
    # when Gemini returns escaped strings (e.g. '{\"M0/Mf\": ...}'), the
    # unescaped body can contain a `}` that the regex mistakes for the outer
    # function closing brace, silently truncating the return value.
    # Fix: use a proper brace-depth counter that skips string literals.
    def _strip_compute_wrapper(body):
        """Strip 'function compute(vals){...}' outer wrapper via brace depth counting."""
        import re as _rw
        m = _rw.match(r'^\s*function\s+compute\s*\([^)]*\)\s*\{', body, _rw.DOTALL)
        if not m:
            return body  # no wrapper — return as-is
        start = m.end()  # position right after the opening '{'
        depth = 1
        i = start
        while i < len(body) and depth > 0:
            ch = body[i]
            if ch == '{':
                depth += 1
                i += 1
            elif ch == '}':
                depth -= 1
                i += 1
            elif ch in ('"', "'", '`'):
                # Skip string literal to avoid counting braces inside strings
                q = ch
                i += 1
                while i < len(body):
                    if body[i] == '\\':   # escape sequence — skip next char
                        i += 2
                        continue
                    if body[i] == q:
                        i += 1
                        break
                    i += 1
            else:
                i += 1
        if depth == 0:
            return body[start:i].strip()  # i is the index of the matched closing '}', so [start:i] excludes it
        return body  # unbalanced braces — return original unchanged

    stripped = _strip_compute_wrapper(compute_js_body)
    if stripped != compute_js_body:
        compute_js_body = stripped  # wrapper was found and stripped
    else:
        compute_js_body = compute_js_body.strip()
    if not compute_js_body:
        compute_js_body = "return { answer: '?', answer_unit: '', answer_label: '?', derived: {} };"

    # ── Bug 2 fix: second-pass synthesis from sol['given_list'] ──────────────
    # When sol['variables'] is [] (Gemini failed to enumerate), the first
    # synthesis pass produces nothing. Try parsing the plain-text given_list
    # strings (format "symbol = value unit") as a fallback source of fields.
    if not fields:
        given_list = sol.get('given_list') or []
        for g in given_list:
            g_str = str(g).strip()
            # Match: "symbol = value unit"  or "symbol: value unit"
            import re as _re_gl
            m_gl = _re_gl.match(
                r'^([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*([\d.eE+\-]+)\s*(\S*)\s*$',
                g_str
            )
            if not m_gl:
                continue
            raw_id_gl  = m_gl.group(1)
            safe_id_gl = _re_cust.sub(r'[^a-zA-Z0-9_]', '_', raw_id_gl).strip('_') or 'v'
            try:
                default_gl = float(m_gl.group(2))
            except ValueError:
                default_gl = 1.0
            unit_gl = m_gl.group(3)
            if any(f.get('id') == safe_id_gl for f in fields):
                continue
            fields.append({
                'id':      safe_id_gl,
                'symbol':  raw_id_gl,
                'label':   raw_id_gl,
                'unit':    unit_gl,
                'default': default_gl,
            })
        # Build a minimal compute if we now have fields but still a stub body
        _stub = "return { answer: '?', answer_unit: '', answer_label: '?', derived: {} };"
        if fields and compute_js_body == _stub:
            compute_js_body = (
                "var ans = parseFloat('"
                + str(sol.get('answer_value', '0')).replace("'", '') + "');"
                " return { answer: _fmt(ans), answer_unit: '"
                + str(sol.get('answer_unit', '')).replace("'", '') + "',"
                " answer_label: '"
                + str(sol.get('formula', 'Answer')).replace("'", '')[:40] + "',"
                " derived: {} };"
            )

    if not fields:
        # No fields after all synthesis attempts — return CSS + a minimal panel
        # that shows a friendly message. The button always exists in the DOM, so
        # opening it must show something rather than nothing.

        _no_fields_panel = """
<div id="customize-backdrop"></div>
<div id="customize-panel" role="dialog" aria-label="Customize question values" aria-hidden="true">
  <div class="cust-header">
    <div class="cust-header-title">
      &#x2699;&#xFE0F; Customize Values
      <span class="cust-header-badge">Live Update</span>
    </div>
    <button class="cust-close-btn" id="cust-close-btn">&#x2715;</button>
  </div>
  <div class="cust-body" style="align-items:center;justify-content:center;min-height:120px;">
    <div style="text-align:center;padding:32px 20px;">
      <div style="font-size:32px;margin-bottom:12px;">&#x2699;&#xFE0F;</div>
      <div style="font-size:14px;font-weight:700;color:#475569;margin-bottom:6px;">
        No customisable fields available
      </div>
      <div style="font-size:12.5px;color:#94a3b8;line-height:1.6;">
        The AI did not identify any numeric input values<br>that can be changed for this question.
      </div>
    </div>
  </div>
  <div class="cust-footer" style="justify-content:center;">
    <button class="cust-btn-reset" id="cust-btn-reset" onclick="
      var p=document.getElementById('customize-panel');
      var b=document.getElementById('customize-backdrop');
      if(p){p.classList.remove('open');p.setAttribute('aria-hidden','true');}
      if(b)b.classList.remove('open');
    ">Close</button>
  </div>
</div>
<script id="qanim-js-customize">
(function initCustomize(){
  'use strict';
  if(window.__qanimCustomizeInit)return;
  window.__qanimCustomizeInit=true;
  function _el(id){return document.getElementById(id);}
  function openPanel(){
    var bd=_el('customize-backdrop'),p=_el('customize-panel');
    if(bd)bd.classList.add('open');
    if(p){p.classList.add('open');p.setAttribute('aria-hidden','false');}
  }
  function closePanel(){
    var bd=_el('customize-backdrop'),p=_el('customize-panel');
    if(bd)bd.classList.remove('open');
    if(p){p.classList.remove('open');p.setAttribute('aria-hidden','true');}
  }
  function onReady(fn){
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);
    else setTimeout(fn,0);
  }
  onReady(function(){
    var ob=_el('customize-ctrl-btn');if(ob)ob.addEventListener('click',openPanel);
    var cb=_el('cust-close-btn');if(cb)cb.addEventListener('click',closePanel);
    var bd=_el('customize-backdrop');if(bd)bd.addEventListener('click',closePanel);
    document.addEventListener('keydown',function(e){if(e.key==='Escape')closePanel();});
  });
})();
</script>
"""
        return _CUSTOMIZE_CSS + _no_fields_panel

    # Build field inputs HTML
    fields_html = ""
    for f in fields:
        fid   = _he(str(f.get("id",  "v")))
        sym   = _he(str(f.get("symbol", fid)))
        label = _he(str(f.get("label", sym)))
        unit  = _he(str(f.get("unit",  "")))
        default_val = f.get("default", 0)
        fields_html += f"""      <div class="cust-field">
        <label><span class="cust-sym">{sym}</span> {label}</label>
        <input type="number" id="cust-field-{fid}" value="{default_val}" step="any">
        <span class="cust-unit">Unit: {unit}</span>
      </div>\n"""

    # Build defaults JS object — guard against None/non-numeric defaults
    def _safe_default(v):
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0
    defaults_entries = ", ".join(
        f"{f.get('id','v')}: {_safe_default(f.get('default'))}"
        for f in fields
    )
    defaults_js = "{ " + defaults_entries + " }"

    # Build readInputs JS — reads each field by id
    read_parts = []
    for f in fields:
        fid_raw = f.get("id", "v")
        read_parts.append(
            f"vals['{fid_raw}'] = parseFloat((document.getElementById('cust-field-{fid_raw}') || {{}}).value || '0');"
        )
    read_lines = "\n    ".join(read_parts)

    # Build preview rows — show each field value in the preview grid
    preview_html = ""
    for f in fields:
        fid = _he(str(f.get("id", "v")))
        sym = _he(str(f.get("symbol", fid)))
        unit = _he(str(f.get("unit", "")))
        preview_html += f'      <div class="cust-preview-item"><span class="cpv-sym">{sym}</span><span class="cpv-arrow">\u2192</span><span id="cpv-{fid}">--</span></div>\n'
    # The JS question template — replace {id} placeholders with vals[id]
    if question_template:
        # Escape backtick/backslash in question_template for JS template literal
        qt_js_safe = question_template.replace('\\', '\\\\').replace('`', "\\`")
        # Replace {id} with ${_fmt(vals.id)} for JS template literals
        for f in fields:
            fid = f.get("id", "v")
            qt_js_safe = qt_js_safe.replace('{' + fid + '}', '${_fmt(vals.' + fid + ')}')
        # ── ROOT-CAUSE FIX ────────────────────────────────────────────────────
        # Any remaining {word} patterns in qt_js_safe (e.g. {unit}, {formula})
        # that weren't replaced by a field id would cause Python's f-string
        # parser to raise KeyError if we used f'`{qt_js_safe}`'.
        # FIX: use plain string concatenation — never an f-string — so Python
        # never tries to evaluate the remaining { } chars in qt_js_safe.
        # Strip only BARE {word} patterns that are NOT part of ${...} JS
        # template literal expressions (those are already correct and must stay).
        # Use negative lookbehind (?<!\$) so ${_fmt(vals.x)} is preserved.
        import re as _re_qt
        qt_js_safe = _re_qt.sub(r'(?<!\$)\{[^}]{1,40}\}', '', qt_js_safe)
        question_tmpl_js = '`' + qt_js_safe + '`'  # NO f-string — safe concat
    else:
        question_tmpl_js = "null"

    # Build given list JS for s6/s7 panels — one entry per field
    given_parts = []
    _dq = '"'  # double-quote char, used to avoid backslash inside f-string
    for f in fields:
        fsym    = _he(str(f.get("symbol", f.get("id", "v"))))
        fid_raw = f.get("id", "v")
        funit   = _he(str(f.get("unit", "")))
        # ── Safe string concat — no f-string, so funit/fid_raw/fsym
        # containing { or } never crash Python ────────────────────────────────
        piece = (
            "'<span class=" + _dq + "s6info-sym" + _dq + ">'"
            + "+" + repr(str(fsym))
            + "+'</span> = '+_fmt(vals." + str(fid_raw) + ")+' " + str(funit) + "'"
        )
        given_parts.append(piece)
    given_entries_js = "[" + ", ".join(given_parts) + "]"

    # Build UNITS JS object — maps field id → unit string for Scene 6 var-box updates
    # Escape single quotes in unit strings so they don't break the JS object literal
    units_entries = ", ".join(
        "'" + f.get('id','v') + "': '" + _he(str(f.get('unit',''))).replace("'", "\\'") + "'"
        for f in fields
    )
    units_js = "{ " + units_entries + " }"

    # ── TRUE ROOT-CAUSE FIX ────────────────────────────────────────────────────
    # f-string is UNSAFE for Gemini-sourced JS: if compute_js contains {word},
    # Python's f-string parser raises KeyError. _fstr_safe ({ → {{) was the
    # attempted workaround, but {{ in a substitution VALUE stays as {{ in output
    # → produces {{answer:...}} in the JS → SyntaxError → panel never opens.
    # SOLUTION: Use a plain string template with __PLACEHOLDER__ tokens and
    # .replace() for each substitution. .replace() never re-parses the value,
    # so Gemini JS with any { } content is always safe.

    # ── Bug 1 fix: _PANEL_TMPL defined BEFORE try so its construction never
    # triggers the except clause and silently returns the empty fallback panel.
    # The try/except now guards ONLY the .replace() substitution chain.
    _PANEL_TMPL = """
<!-- ╒═════════════════════════════════════════════════════════════
     CUSTOMIZE PANEL
     ╙═════════════════════════════════════════════════════════════ -->
<div id="customize-backdrop"></div>
<div id="customize-panel" role="dialog" aria-label="Customize question values" aria-hidden="true">
  <div class="cust-header">
    <div class="cust-header-title">
      &#x2699;&#xFE0F; Customize Values
      <span class="cust-header-badge">Live Update</span>
    </div>
    <button class="cust-close-btn" id="cust-close-btn">&#x2715;</button>
  </div>

  <div class="cust-body">
    <div class="cust-section-title">Given Parameters</div>
    <div class="cust-field-grid">
__FIELDS_HTML__
    </div>

    <div class="cust-section-title">Live Preview</div>
    <div class="cust-preview-box">
      <div class="cust-preview-title">&#x1F4D0; Calculated Values</div>
      <div class="cust-preview-grid" id="cust-preview-grid">
__PREVIEW_HTML__
        <div class="cust-preview-item"><span class="cpv-sym" id="cpv-answer-label">?</span><span class="cpv-arrow">&#x2192;</span><span id="cpv-answer">--</span></div>
      </div>
    </div>

    <div class="cust-result-bar" id="cust-result-bar">
      <div class="cust-result-title">&#x2705; Ready to Apply</div>
      <div class="cust-result-values" id="cust-result-values"></div>
    </div>

    <div class="cust-error-bar" id="cust-error-bar">
      &#x26A0;&#xFE0F; <span id="cust-error-msg">Please enter valid positive numbers.</span>
    </div>
  </div>

  <div class="cust-footer">
    <button class="cust-btn-reset" id="cust-btn-reset">&#x21BA; Reset Defaults</button>
    <button class="cust-btn-apply" id="cust-btn-apply">&#x2713; Apply &amp; Update</button>
  </div>
</div>

<script id="qanim-js-customize">
(function initCustomize(){
  'use strict';
  if(window.__qanimCustomizeInit)return;
  window.__qanimCustomizeInit=true;

  var DEFAULTS = __DEFAULTS_JS__;
  var CURRENT  = Object.assign({}, DEFAULTS);

  function _el(id){ return document.getElementById(id); }
  function _round(v, d){ var m=Math.pow(10,d); return Math.round(v*m)/m; }
  // Bug 3 fix: _escRe uses split/join to escape regex special chars.
  // All previous regex-based approaches broke because representing `]` inside
  // a JS character class requires `\]`, but the Python string escaping layers
  // (Python string → JS string → JS regex) made it impossible to write
  // the correct bytes without ambiguity. split/join requires NO regex at all.
  function _escRe(s){
    var sp=['.','*','+','?','^','$','{','}','(',')','|','[',']','\\'];
    s=String(s);
    // escape \ FIRST so we don't double-escape our own replacements
    s=s.split('\\').join('\\\\');
    for(var i=0;i<sp.length-1;i++)s=s.split(sp[i]).join('\\'+sp[i]);
    return s;
  }
  function _fmt(v){
    if(typeof v !== 'number' || isNaN(v)) return '?';
    if(v === 0) return '0';
    if(Math.abs(v) < 0.001 || Math.abs(v) >= 1e6) return v.toExponential(3);
    return _round(v, 4) + '';
  }

  function compute(vals){
    try { __COMPUTE_JS_BODY__ }
    catch(e){ console.warn('[QAnim Customize] compute error:', e); return null; }
  }

  function readInputs(){
    var vals = {};
    __READ_LINES__
    return vals;
  }

  function validate(vals){
    var keys = Object.keys(vals);
    for(var i=0;i<keys.length;i++){
      if(isNaN(vals[keys[i]])) return 'Please enter valid numbers in all fields.';
    }
    return null;
  }

  function updatePreview(){
    var vals = readInputs();
    var err  = validate(vals);
    var errBar = _el('cust-error-bar'), errMsg = _el('cust-error-msg');
    var resBar = _el('cust-result-bar'), resVal = _el('cust-result-values');

    if(err){
      if(errBar) errBar.classList.add('visible');
      if(errMsg) errMsg.textContent = err;
      if(resBar) resBar.classList.remove('visible');
      return;
    }
    if(errBar) errBar.classList.remove('visible');

    // Update per-field preview chips
    var fieldIds = Object.keys(DEFAULTS);
    fieldIds.forEach(function(id){
      var el = _el('cpv-' + id);
      if(el) el.textContent = _fmt(vals[id]);
    });

    var c = compute(vals);
    if(c && c.answer !== undefined){
      var ansEl    = _el('cpv-answer');       if(ansEl) ansEl.textContent = c.answer + (c.answer_unit ? ' ' + c.answer_unit : '');
      var ansLabel = _el('cpv-answer-label'); if(ansLabel) ansLabel.textContent = c.answer_label || 'Answer';
      if(resBar) resBar.classList.add('visible');
      if(resVal) resVal.textContent = (c.answer_label || '?') + ' = ' + c.answer + ' ' + (c.answer_unit || '');
    }
  }

  function applyToAnimation(vals, c){
    CURRENT = Object.assign({}, vals);

    // 1. Question banner text
    var newQ = __QUESTION_TMPL_JS__;
    if(newQ) document.querySelectorAll('.q-text').forEach(function(el){ el.textContent = newQ; });

    // 2. SVG layer text labels — match any text element whose content
    //    contains a field symbol or value pattern, and update it
    var fieldIds = Object.keys(DEFAULTS);
    fieldIds.forEach(function(id){
      var dflt = DEFAULTS[id];
      var newV = _fmt(vals[id]);
      // Attempt to update any SVG text containing the default value
      document.querySelectorAll('svg text').forEach(function(t){
        if(t.textContent.indexOf(dflt) > -1) {
          t.textContent = t.textContent.replace(String(dflt), newV);
        }
      });
    });

    // 3. stepsData badges & descriptions (Steps 3-6 concept animation)
    if(window.stepsData && Array.isArray(window.stepsData)){
      // Update steps 2-5 (0-indexed) with new badge values if badges reference field values
      window.stepsData.forEach(function(step, idx){
        if(!step) return;
        // Replace old numeric value strings in badges with new ones
        var newBadges = (step.badges || []).map(function(b){
          var out = b;
          fieldIds.forEach(function(id){
            // Replace occurrences of DEFAULTS[id] in the badge HTML text
            var re = new RegExp(_escRe(DEFAULTS[id]), 'g');
            out = out.replace(re, _fmt(vals[id]));
          });
          return out;
        });
        step.badges = newBadges;
        if(step.desc){
          var newDesc = step.desc;
          fieldIds.forEach(function(id){
            var re = new RegExp(_escRe(DEFAULTS[id]), 'g');
            newDesc = newDesc.replace(re, _fmt(vals[id]));
          });
          step.desc = newDesc;
        }
      });
      // Bug 3 fix: bare applyStep() throws ReferenceError inside an IIFE.
      // Always qualify globals with window. when crossing script boundaries.
      if(typeof window.applyStep === 'function') window.applyStep(window.currentStep || 0);
    }

    // 4. Step-6 To-Find badge — keep unchanged (shows unknown symbol, not values)

    // 5. Scene 6 (Step 7) variable boxes — update by field id
    var UNITS = __UNITS_JS__;
    fieldIds.forEach(function(id){
      var el = _el('s6v-' + id + '-val');
      if(el) el.textContent = _fmt(vals[id]) + (UNITS[id] ? ' ' + UNITS[id] : '');
    });

    // 6. Scene 7/8 (Step 8) given list
    var s7given = _el('s7-given-list');
    if(s7given){
      var givenLines = __GIVEN_ENTRIES_JS__;
      s7given.innerHTML = givenLines.map(function(g){
        return '<div class="s7-given-item">' + g + '</div>';
      }).join('');
    }

    // 6b. Scene 7/8 (Step 8) approach steps — rebuild using derived values from compute
    var s7approach = _el('s7-approach-list');
    if(s7approach && c && c.derived && typeof c.derived === 'object'){
      var derivedKeys = Object.keys(c.derived);
      if(derivedKeys.length > 0){
        var apHTML = '';
        derivedKeys.forEach(function(dkey, di){
          apHTML += '<div class="s7-approach-step">' +
            '<span class="s7-approach-step-num">' + (di + 1) + '</span>' +
            '<span>' + dkey +
              '<span class="s7-approach-step-eq">' + c.derived[dkey] + '</span>' +
            '</span>' +
          '</div>';
        });
        // Final step: the main answer
        if(c.answer !== undefined){
          apHTML += '<div class="s7-approach-step">' +
            '<span class="s7-approach-step-num">' + (derivedKeys.length + 1) + '</span>' +
            '<span>' + (c.answer_label || 'Answer') + ' = ' + c.answer + ' ' + (c.answer_unit || '') +
              '<span style="display:block;font-size:11px;color:#64748b;margin-top:3px;">Final result</span>' +
            '</span>' +
          '</div>';
        }
        s7approach.innerHTML = apHTML;
      }
    }

    // 7. Scene 9 (Step 9) substitution chain — update numeric values
    var s9chain = _el('s9-sub-chain');
    if(s9chain){
      var rows = s9chain.querySelectorAll('.s9-sub-row');
      rows.forEach(function(row){
        var eq = row.querySelector('.s9-sub-eq');
        if(!eq) return;
        var text = eq.innerHTML;
        fieldIds.forEach(function(id){
          var re = new RegExp(_escRe(DEFAULTS[id]), 'g');
          text = text.replace(re, _fmt(vals[id]));
        });
        eq.innerHTML = text;
      });
    }

    // 8. Scene 9 final answer value
    if(c && c.answer !== undefined){
      var fv = _el('s9-final-value');
      if(fv){
        var hl = fv.querySelector('.s9-highlight');
        if(hl) hl.textContent = c.answer;
      }
      var fu = _el('s9-final-unit');
      if(fu) fu.innerHTML = 'Units: <strong>' + (c.answer_unit || '') + '</strong>';
      var st = _el('s9-insight-text');
      if(st && c.answer_label) st.innerHTML = '<strong>Result:</strong> ' + c.answer_label + ' = ' + c.answer + ' ' + (c.answer_unit || '');
    }

    // 9. Answer Box — update live targets via the global hook
    if(typeof window.__qanimSetAnswerTargets === 'function' && c && c.answer !== undefined){
      window.__qanimSetAnswerTargets([{
        label: c.answer_label || 'Final Answer',
        value: String(c.answer),
        unit:  c.answer_unit || '',
        insight: 'Calculated with updated values: ' + (c.answer_label || '') + ' = ' + c.answer + ' ' + (c.answer_unit || '') + '.'
      }]);
    }
    // Also update the legacy _answerTargets array if present
    if(window._answerTargets && window._answerTargets[0] && c && c.answer !== undefined){
      window._answerTargets[0].value = c.answer;
      window._answerTargets[0].unit  = c.answer_unit || '';
    }

    // 10. Close any open modal overlays and return to Step 1
    // Do not hide overlays when customizing
    if(typeof window.applyStep === 'function' && window.currentStep !== undefined) {
      window.applyStep(window.currentStep);
    }
  }

  function openPanel(){
    var bd = _el('customize-backdrop'), p = _el('customize-panel');
    if(bd){ bd.classList.add('open'); }
    if(p){ p.classList.add('open'); p.setAttribute('aria-hidden','false'); }
    updatePreview();
  }

  function closePanel(){
    var bd = _el('customize-backdrop'), p = _el('customize-panel');
    if(bd){ bd.classList.remove('open'); }
    if(p){ p.classList.remove('open'); p.setAttribute('aria-hidden','true'); }
  }

  function onReady(fn){
    if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else setTimeout(fn, 0);
  }

  onReady(function(){
    // Wire close/backdrop
    var cb = _el('cust-close-btn'); if(cb) cb.addEventListener('click', closePanel);
    var bd = _el('customize-backdrop'); if(bd) bd.addEventListener('click', closePanel);
    document.addEventListener('keydown', function(e){ if(e.key === 'Escape') closePanel(); });

    // Wire open button
    var ob = _el('customize-ctrl-btn'); if(ob) ob.addEventListener('click', openPanel);

    // Wire input fields — live preview on every keystroke
    Object.keys(DEFAULTS).forEach(function(id){
      var inp = _el('cust-field-' + id);
      if(inp) inp.addEventListener('input', updatePreview);
    });

    // Reset defaults
    var rb = _el('cust-btn-reset');
    if(rb) rb.addEventListener('click', function(){
      Object.keys(DEFAULTS).forEach(function(id){
        var inp = _el('cust-field-' + id);
        if(inp) inp.value = DEFAULTS[id];
      });
      updatePreview();
    });

    // Apply & Update
    var ab = _el('cust-btn-apply');
    if(ab) ab.addEventListener('click', function(){
      var vals = readInputs();
      var err  = validate(vals);
      if(err){
        var errBar = _el('cust-error-bar'), errMsg = _el('cust-error-msg');
        if(errBar) errBar.classList.add('visible');
        if(errMsg) errMsg.textContent = err;
        return;
      }
      var c = compute(vals);
      applyToAnimation(vals, c);
      closePanel();
      // Pulse the Customize button in the controls bar
      var custBtn = _el('customize-ctrl-btn');
      if(custBtn){
        custBtn.classList.remove('cust-applied-ring');
        void custBtn.offsetWidth; // force reflow to restart animation
        custBtn.classList.add('cust-applied-ring');
        setTimeout(function(){ custBtn.classList.remove('cust-applied-ring'); }, 800);
      }
    });

    updatePreview();
  });
})();
</script>
"""

    # ── Bug 1 fix: try ONLY wraps the .replace() substitution chain ─────────
    # _PANEL_TMPL is defined above unconditionally, so its construction never
    # triggers this except. Any encoding / type issue in the Gemini-sourced
    # values (compute_js_body, given_entries_js, etc.) is caught here instead
    # of silently producing the empty fallback panel.
    try:
        panel_html = (
            _PANEL_TMPL
            .replace("__FIELDS_HTML__",      fields_html)
            .replace("__PREVIEW_HTML__",     preview_html)
            .replace("__DEFAULTS_JS__",      defaults_js)
            .replace("__COMPUTE_JS_BODY__",  compute_js_body)
            .replace("__READ_LINES__",       read_lines)
            .replace("__UNITS_JS__",         units_js)
            .replace("__GIVEN_ENTRIES_JS__", given_entries_js)
            .replace("__QUESTION_TMPL_JS__", question_tmpl_js)
        )
        return _CUSTOMIZE_CSS + panel_html

    except Exception as _fstr_err:
        # RC#2: A bare {word} in Gemini-sourced content (compute_js_body,
        # defaults_js, read_lines, given_entries_js, units_js, or
        # question_tmpl_js) was interpreted as a Python format specifier and
        # caused a KeyError / ValueError.  Log it and fall back to the
        # no-fields panel so the Customize button still opens something useful
        # instead of taking down the entire page silently.
        import logging as _log_cust
        _log_cust.getLogger(__name__).warning(
            '[_build_customize_html] f-string interpolation error '
            '(likely bare {word} in Gemini JS output) — '
            'falling back to no-fields panel. Error: %s', _fstr_err
        )
        # Re-use the minimal no-fields panel defined earlier in this function.
        # We can't reference _no_fields_panel (different branch), so inline it.
        _fb_panel = """
<div id="customize-backdrop"></div>
<div id="customize-panel" role="dialog" aria-label="Customize question values" aria-hidden="true">
  <div class="cust-header">
    <div class="cust-header-title">
      &#x2699;&#xFE0F; Customize Values
      <span class="cust-header-badge">Live Update</span>
    </div>
    <button class="cust-close-btn" id="cust-close-btn">&#x2715;</button>
  </div>
  <div class="cust-body" style="align-items:center;justify-content:center;min-height:120px;">
    <div style="text-align:center;padding:32px 20px;">
      <div style="font-size:32px;margin-bottom:12px;">&#x2699;&#xFE0F;</div>
      <div style="font-size:14px;font-weight:700;color:#475569;margin-bottom:6px;">
        Customize panel could not be generated
      </div>
      <div style="font-size:12.5px;color:#94a3b8;line-height:1.6;">
        The AI returned a formula containing special characters<br>that prevented the live editor from loading.
      </div>
    </div>
  </div>
  <div class="cust-footer" style="justify-content:center;">
    <button class="cust-btn-reset" id="cust-btn-reset" onclick="
      var p=document.getElementById('customize-panel');
      var b=document.getElementById('customize-backdrop');
      if(p){p.classList.remove('open');p.setAttribute('aria-hidden','true');}
      if(b)b.classList.remove('open');
    ">Close</button>
  </div>
</div>
<script id="qanim-js-customize">
(function initCustomize(){
  'use strict';
  if(window.__qanimCustomizeInit)return;
  window.__qanimCustomizeInit=true;
  function _el(id){return document.getElementById(id);}
  function openPanel(){
    var bd=_el('customize-backdrop'),p=_el('customize-panel');
    if(bd)bd.classList.add('open');
    if(p){p.classList.add('open');p.setAttribute('aria-hidden','false');}
  }
  function closePanel(){
    var bd=_el('customize-backdrop'),p=_el('customize-panel');
    if(bd)bd.classList.remove('open');
    if(p){p.classList.remove('open');p.setAttribute('aria-hidden','true');}
  }
  function onReady(fn){
    if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);
    else setTimeout(fn,0);
  }
  onReady(function(){
    var ob=_el('customize-ctrl-btn');if(ob)ob.addEventListener('click',openPanel);
    var cb=_el('cust-close-btn');if(cb)cb.addEventListener('click',closePanel);
    var bd=_el('customize-backdrop');if(bd)bd.addEventListener('click',closePanel);
    document.addEventListener('keydown',function(e){if(e.key==='Escape')closePanel();});
  });
})();
</script>
"""
        return _CUSTOMIZE_CSS + _fb_panel


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


def _build_step6_panel_html(sol: dict, scene: dict) -> str:
    """Build the Step-6 'To Find' floating badge shown directly on the SVG canvas.

    Change (Update 2): The old two-column "Given Data / To Find" card overlay is
    removed.  Instead, we show a minimal, visually unobtrusive floating badge that
    labels the unknown quantity with its notation (e.g. "T₃ ?") right on the SVG
    so the student can see WHERE the unknown lives in the physical diagram.

    IMPORTANT: Must never reveal the final answer value — the answer only appears
    in Steps 8–9.
    """
    to_find      = scene.get("to_find", ["The unknown quantity"])
    answer_value = str(sol.get("answer_value", "")).strip()   # used for scrubbing
    final_answer = str(sol.get("final_answer", "")).strip()   # used for scrubbing

    # Build compact "Find: SYMBOL ?" chips — one per unknown
    find_items_html = ""
    for tf in to_find:
        # Never reveal the numerical answer
        tf_clean = tf
        if answer_value and answer_value != "?" and answer_value in tf:
            tf_clean = tf.replace(answer_value, "?")
        find_items_html += (
            f'<div class="s6tofind-chip">'
            f'<span class="s6tofind-icon">&#x2753;</span>'
            f'<span class="s6tofind-label">{_he(tf_clean)}&thinsp;?</span>'
            f'</div>\n'
        )

    hint = (
        '<div class="s6tofind-hint">'
        '<span>&#x1F512;</span> Answer revealed in <strong>Step&nbsp;9</strong>'
        '</div>'
    )

    return f"""<div id="step6-info-panel" role="region" aria-label="What to find">
  <div class="s6tofind-badge">
    <div class="s6tofind-heading">&#x1F3AF;&thinsp;Find</div>
    {find_items_html}
    {hint}
  </div>
</div>"""


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

    # Step-6 given/to-find panel
    step6_panel_html = _build_step6_panel_html(sol, scene)

    # SVG content
    svg_defs = svg_data.get("svg_defs", "")
    # Strip any <defs>...</defs> wrapper Gemini may have included in svg_defs.
    # We inject svg_defs INSIDE our own <defs> block in the template, so any
    # extra </defs> or <defs> tags would produce malformed SVG (broken gradients).
    import re as _re_assemble
    svg_defs = _re_assemble.sub(r'^\s*<defs[^>]*>', '', svg_defs, flags=_re_assemble.IGNORECASE).strip()
    svg_defs = _re_assemble.sub(r'\s*</defs>\s*$', '', svg_defs, flags=_re_assemble.IGNORECASE).strip()
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
    scene6_html = _build_scene6_html(sol, scene)
    scene7_html = _build_scene7_html(sol, scene)
    scene9_html = _build_scene9_html(sol, to_find)
    glossary_panel = _build_glossary_panel(glossary)
    glossary_badge = f'<span class="glossary-ctrl-badge">{len(glossary)}</span>' if glossary else ""
    glossary_sep = '<div class="qanim-ctrl-sep"></div>' if glossary else ""
    glossary_btn = f"""  {glossary_sep}
  <button class="qanim-ctrl-btn" id="glossary-ctrl-btn" title="Difficult words explained" style="position:relative;">
    <span>&#x1F4D6;</span><span class="ctrl-label">Glossary</span>{glossary_badge}
  </button>""" if glossary else ""

    # Customize panel — button is ALWAYS injected unconditionally.
    # Root-cause fix: all previous approaches conditioned the button on whether
    # Gemini returned customize.fields or variables could be synthesised.
    # When both paths yield nothing the button disappears entirely.
    # Solution: decouple the button from the panel content — the button is always
    # written into the DOM, and the JS opens whatever panel _build_customize_html
    # produced (full panel, fallback message, or a graceful empty state).
    # ORDER: Customize is always FIRST in the controls bar (no leading separator).
    customize_html = _build_customize_html(sol, scene)
    customize_btn  = """  <button class="qanim-ctrl-btn" id="customize-ctrl-btn" title="Customize question values" style="position:relative;">
    <span>&#x2699;&#xFE0F;</span><span class="ctrl-label">Customize</span>
  </button>"""

    # Answer box JS with targets
    answerbox_js = _ANSWERBOX_JS_TMPL.replace("{{TARGETS_JSON}}", targets_json)

    # Fix A/E/F/H/I/J: nav_js is the authoritative JavaScript injection.
    # - applyStep is ALWAYS Python-controlled (never Gemini's apply_step_js).
    # - CONCEPT_STEP_COUNT=6 / TOTAL_STEP_COUNT=9 replace all magic numbers.
    # - qanim_nextStep() / qanim_previousStep() replace nextStep() / prevStep().
    # - DOMContentLoaded validates stepsData.length === 6 before starting.
    nav_js = f"""
  <script>
    // ── Step-count constants (authoritative — Fix A) ───────────────────────────
    const CONCEPT_STEP_COUNT = 6;   // Steps 1-6: SVG concept animation (stepsData indexes 0-5)
    const TOTAL_STEP_COUNT   = 9;   // Steps 1-9 total (includes modal scenes 7, 8, 9)

    // ── Runtime state ──────────────────────────────────────────────────────────
    var currentStep = 0;
    window.currentStep  = 0;
    window.qanimRafId   = null;

    // ── stepsData (always rebuilt by Python — Fix B/C) ────────────────────────
    {steps_data_js}
    // Ensure window scope
    if (typeof stepsData !== 'undefined') window.stepsData = stepsData;

    // ── RAF loop (Gemini-provided, if any continuous animation needed) ─────────
    {raf_js}

    // ── applyStep: AUTHORITATIVE Python-controlled implementation (Fix E) ──────
    // - Clamps index strictly to 0 .. CONCEPT_STEP_COUNT-1 (never accesses stepsData[6+])
    // - Uses TOTAL_STEP_COUNT=9 for progress bar percentage
    // - Gemini's apply_step_js is NOT injected — this function is the only applyStep
    function applyStep(index) {{
      const idx = Math.max(
        0,
        Math.min(Number(index) || 0, CONCEPT_STEP_COUNT - 1)
      );

      window.currentStep = idx;

      const data = window.stepsData && window.stepsData[idx];
      if (!data) {{
        console.error('[QAnim] Missing concept step:', idx);
        return;
      }}

      const titleEl  = document.getElementById('info-title');
      const descEl   = document.getElementById('info-desc');
      const badgesEl = document.getElementById('info-badges');
      const blurEl   = document.getElementById('blur-shield');

      if (titleEl)  titleEl.textContent  = data.title || '';
      if (descEl)   descEl.textContent   = data.desc  || '';

      if (badgesEl) {{
        badgesEl.innerHTML = Array.isArray(data.badges)
          ? data.badges.join('')
          : '';
      }}

      Object.entries(data.layerOpacities || {{}}).forEach(([id, value]) => {{
        const layer = document.getElementById(id);
        if (layer) layer.style.opacity = String(value);
      }});

      if (blurEl) {{
        blurEl.style.opacity = String(
          typeof data.blurOp === 'number' ? data.blurOp : 0
        );
      }}

      document.querySelectorAll('.step-dot').forEach((dot, dotIndex) => {{
        dot.classList.toggle('active', dotIndex === idx);
        dot.classList.toggle('done',   dotIndex < idx);
      }});

      const labelEl = document.getElementById('step-label');
      if (labelEl) {{
        labelEl.textContent = 'Step ' + (idx + 1) + ' of ' + TOTAL_STEP_COUNT;
      }}

      const progressEl = document.getElementById('step-bar');
      if (progressEl) {{
        progressEl.style.width = (((idx + 1) / TOTAL_STEP_COUNT) * 100) + '%';
      }}

      const prevBtn = document.getElementById('btn-prev');
      if (prevBtn) prevBtn.disabled = (idx === 0);

      const nextBtn = document.getElementById('btn-next');
      if (nextBtn) {{
        nextBtn.textContent = (idx === CONCEPT_STEP_COUNT - 1)
          ? 'Step 7: Formula \u25b6'
          : 'Next Step \u25b6';
      }}

      // ── Step-6 floating badge: permanently hidden (was cluttering the SVG canvas)
      // The "To Find" unknown is only shown in Step 9 (final answer overlay).
      const s6panel = document.getElementById('step6-info-panel');
      if (s6panel) s6panel.style.display = 'none';
    }}
    window.applyStep = applyStep;

    // ── Deterministic navigation (Fix F) ──────────────────────────────────────
    // qanim_nextStep: concept steps 0-4 → next concept step;
    //                 concept step 5 (Step 6) → open Step 7 modal
    function qanim_nextStep() {{
      const current = Number(window.currentStep) || 0;
      if (current < CONCEPT_STEP_COUNT - 1) {{
        applyStep(current + 1);
        return;
      }}
      // At concept step 5 (= Step 6) — open Step 7 (Scene 6 modal)
      if (typeof window.qanim_showScene6 === 'function') {{
        window.qanim_showScene6();
      }}
    }}
    window.qanim_nextStep = qanim_nextStep;

    // qanim_previousStep: concept steps 1-5 → previous concept step;
    //                     concept step 0 → button is disabled (no action)
    function qanim_previousStep() {{
      const current = Number(window.currentStep) || 0;
      if (current > 0) {{
        applyStep(current - 1);
      }}
    }}
    window.qanim_previousStep = qanim_previousStep;

    // ── Utility: hide all modal overlays ──────────────────────────────────────
    function _qanim_hideAllOverlays() {{
      ['qanim-scene6-overlay', 'qanim-scene7-overlay', 'qanim-scene9-overlay']
        .forEach(function(id) {{
          var el = document.getElementById(id);
          if (el) el.classList.remove('qanim-scene-visible');
        }});
    }}

    // ── goToStep: for step-dot click navigation (concept steps only) ──────────
    function goToStep(idx) {{
      if (idx >= 0 && idx < CONCEPT_STEP_COUNT) {{
        _qanim_hideAllOverlays();
        var bd = document.getElementById('qanim-scene-modal-backdrop');
        if (bd) bd.classList.remove('qanim-scene-visible');
        var svgCont = document.querySelector('.svg-container');
        if (svgCont) svgCont.style.opacity = '1';
        applyStep(idx);
      }}
    }}

    // ── resetAnim: Restart button + Step 9 Restart button ─────────────────────
    function resetAnim() {{
      _qanim_hideAllOverlays();
      var bd = document.getElementById('qanim-scene-modal-backdrop');
      if (bd) bd.classList.remove('qanim-scene-visible');
      var svgCont = document.querySelector('.svg-container');
      if (svgCont) svgCont.style.opacity = '1';
      applyStep(0);
    }}
    window.resetAnim = resetAnim;

    // ── window.__qanimSetAnswerTargets: hook for Customize panel answer updates ──
    // Called by applyToAnimation() in the Customize JS after recomputing the answer
    // with new field values. Updates the Answer Box targets so the student can check
    // their answer against the freshly computed result.
    window.__qanimSetAnswerTargets = function(targets) {{
      try {{
        var el = document.getElementById('__answer_targets__');
        if (el) {{
          var current = JSON.parse(el.textContent || '{{}}');
          current.answer_targets = Array.isArray(targets) ? targets : [targets];
          el.textContent = JSON.stringify(current);
        }}
        // If the AnswerBox JS has already initialised its _targets cache,
        // reset it so the next openAnswerBox() call re-reads from the element.
        if (typeof window.__qanimAnswerBoxReset === 'function') {{
          window.__qanimAnswerBoxReset();
        }}
      }} catch(e) {{
        console.warn('[QAnim] __qanimSetAnswerTargets error:', e);
      }}
    }};


    document.addEventListener('DOMContentLoaded', function () {{
      if (!Array.isArray(window.stepsData) || window.stepsData.length !== CONCEPT_STEP_COUNT) {{
        console.error(
          '[QAnim] Invalid stepsData. Expected exactly ' + CONCEPT_STEP_COUNT +
          ' concept steps. Got: ' + (Array.isArray(window.stepsData) ? window.stepsData.length : 'undefined')
        );
        return;
      }}

      console.log('[QAnim] Ready:', {{
        conceptSteps: window.stepsData.length,
        totalSteps: TOTAL_STEP_COUNT,
        titles: window.stepsData.map(function(step) {{ return step.title; }})
      }});

      applyStep(0);

      if (typeof window.qanimStartRAF === 'function') {{
        window.qanimStartRAF();
      }}

      // Render any math formulas already in the DOM (Scene 7 & 9 overlays)
      if (typeof window.qanimRenderMath === 'function') {{
        window.qanimRenderMath();
      }}
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
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css" crossorigin="anonymous">
  <script src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js" crossorigin="anonymous"></script>
  <script>
  // ── KaTeX math renderer ────────────────────────────────────────────────────
  // Scans any element with [data-formula] and renders its value with KaTeX.
  // Falls back to showing the raw formula text if KaTeX is unavailable.
  window.qanimRenderMath = function(scope) {{
    var root = scope || document;
    var els = root.querySelectorAll('[data-formula]:not([data-math-ok])');
    if (!els.length) return;
    if (typeof katex === 'undefined') {{
      // KaTeX not yet loaded — retry once after a short delay
      setTimeout(function(){{window.qanimRenderMath(scope);}}, 300);
      return;
    }}
    els.forEach(function(el) {{
      var f = el.getAttribute('data-formula');
      if (!f) return;
      try {{
        katex.render(f, el, {{
          throwOnError: false,
          displayMode: el.classList.contains('s6-formula-main'),
          output: 'html',
          trust: true
        }});
        el.setAttribute('data-math-ok', '1');
      }} catch(e) {{
        // Keep the plain text fallback; mark as attempted to avoid retry loop
        el.setAttribute('data-math-ok', 'err');
        console.warn('[QAnim] KaTeX render failed:', e.message, f);
      }}
    }});
  }};
  </script>
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

<!-- Fullscreen button -->
<button id="qanim-fullscreen-btn" title="Toggle fullscreen" onclick="qanimToggleFullscreen()">
  <span class="fs-icon">&#x26F6;</span><span>Fullscreen</span>
</button>

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
        <!-- Guaranteed light background pattern -->
        <pattern id="qanim-grid-light" width="40" height="40" patternUnits="userSpaceOnUse">
          <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#cbd5e1" stroke-width="0.4" opacity="0.5"/>
        </pattern>
      </defs>
      <!-- Always-on light base: overrides any dark fill Gemini may produce -->
      <g id="layer-canvas-bg">
        <rect width="850" height="478" fill="#f8fafc"/>
        <rect width="850" height="478" fill="url(#qanim-grid-light)" opacity="0.8"/>
      </g>
      {svg_layers}
    </svg>
    {step6_panel_html}
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
      <button class="btn-secondary qanim-prev-btn" id="btn-prev" onclick="qanim_previousStep()" disabled>&#x25C0; Previous Step</button>
      <button class="btn-primary" id="btn-next" onclick="qanim_nextStep()">Next Step &#x25B6;</button>
    </div>
  </div>
</div>

{nav_js}

{_SCENE6_JS}
{_SCENE7_JS}
{_SCENE9_JS}
{answerbox_js}
{_GLOSSARY_JS}
{customize_html}

<script id="qanim-js-fullscreen">
(function(){{
  'use strict';
  window.qanimToggleFullscreen = function(){{
    var btn = document.getElementById('qanim-fullscreen-btn');
    var isFs = !!(document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement);
    if(!isFs){{
      var el = document.documentElement;
      if(el.requestFullscreen) el.requestFullscreen();
      else if(el.webkitRequestFullscreen) el.webkitRequestFullscreen();
      else if(el.mozRequestFullScreen) el.mozRequestFullScreen();
    }} else {{
      if(document.exitFullscreen) document.exitFullscreen();
      else if(document.webkitExitFullscreen) document.webkitExitFullscreen();
      else if(document.mozCancelFullScreen) document.mozCancelFullScreen();
    }}
  }};

  function _onFsChange(){{
    var btn = document.getElementById('qanim-fullscreen-btn');
    var icon = btn ? btn.querySelector('.fs-icon') : null;
    var label = btn ? btn.querySelector('span:last-child') : null;
    var isFs = !!(document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement);
    if(btn) btn.classList.toggle('is-fullscreen', isFs);
    if(icon) icon.innerHTML = isFs ? '&#x2716;' : '&#x26F6;';
    if(label) label.textContent = isFs ? 'Exit' : 'Fullscreen';
    document.body.classList.toggle('qanim-fullscreen', isFs);
    // Toggle SVG preserveAspectRatio: 'meet' shows full SVG; 'slice' fills container
    var svg = document.getElementById('stage');
    if(svg) svg.setAttribute('preserveAspectRatio', isFs ? 'xMidYMid meet' : 'xMidYMid slice');
    // Re-render math formulas (layout shift may have cleared rendered content)
    if(typeof window.qanimRenderMath === 'function') setTimeout(window.qanimRenderMath, 60);
  }}

  document.addEventListener('fullscreenchange', _onFsChange);
  document.addEventListener('webkitfullscreenchange', _onFsChange);
  document.addEventListener('mozfullscreenchange', _onFsChange);

  // Keyboard shortcut: F key toggles fullscreen
  document.addEventListener('keydown', function(e){{
    if(e.key === 'f' || e.key === 'F'){{
      var tag = (e.target||{{}}).tagName||'';
      if(tag === 'INPUT' || tag === 'TEXTAREA') return;
      window.qanimToggleFullscreen();
    }}
  }});
}})();
</script>

<div id="qanim-controls-bar" role="toolbar" aria-label="QAnim Controls">
  {customize_btn}
  <div class="qanim-ctrl-sep"></div>
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
