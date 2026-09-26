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


def _clean_latex(text: str) -> str:
    """Convert LaTeX-style math notation to plain professional text.

    Gemini sometimes returns formulas like:
      r \\frac{d^2\\omega}{dt^2} + \\left(\\frac{1}{C} + ...\\right) = 0
    This function strips/replaces all such LaTeX commands with readable equivalents.
    """
    if not isinstance(text, str):
        return str(text) if text is not None else ""
    t = text

    # ── 1. Strip outer \\ backslash escaping (JSON double-escape) ────────────
    # JSON strings from Gemini often have \\frac instead of \frac
    t = t.replace('\\\\', '\x00BSLASH\x00')  # temporarily protect \\

    # ── 2. Replace \frac{num}{den} → (num)/(den) ────────────────────────────
    import re as _re_lt
    # Handle up to 3 levels of nesting via repeated passes
    for _ in range(4):
        t = _re_lt.sub(
            r'\\frac\{([^{}]*)\}\{([^{}]*)\}',
            lambda m: '(' + m.group(1) + ')/(' + m.group(2) + ')',
            t
        )

    # ── 3. Superscripts: ^{expr} → ^expr, x^2 → x² (common cases) ──────────
    SUPER = {'0':'⁰','1':'¹','2':'²','3':'³','4':'⁴','5':'⁵',
              '6':'⁶','7':'⁷','8':'⁸','9':'⁹','+':'+','-':'\u207b','n':'ⁿ'}
    def _sup(m):
        inner = m.group(1)
        if len(inner) == 1 and inner in SUPER:
            return SUPER[inner]
        return '^' + inner
    t = _re_lt.sub(r'\^\{([^}]{1,12})\}', _sup, t)
    # bare ^n for single digit/char
    t = _re_lt.sub(r'\^([0-9])', lambda m: SUPER.get(m.group(1), '^'+m.group(1)), t)

    # ── 4. Subscripts: _{expr} → keep as _expr (plain text) ─────────────────
    t = _re_lt.sub(r'_\{([^}]{1,12})\}', r'_\1', t)

    # ── 5. Remove \left, \right, \big, \Big, \bigg delimiters ───────────────
    for cmd in [r'\left', r'\right', r'\Big', r'\bigg', r'\big']:
        t = t.replace(cmd + '(', '(').replace(cmd + ')', ')')
        t = t.replace(cmd + '[', '[').replace(cmd + ']', ']')
        t = t.replace(cmd + '\\{', '{').replace(cmd + '\\}', '}')
        t = t.replace(cmd, '')

    # ── 6. Replace common LaTeX Greek / math commands ────────────────────────
    GREEK = {
        r'\alpha': 'α', r'\beta': 'β', r'\gamma': 'γ', r'\delta': 'δ',
        r'\epsilon': 'ε', r'\varepsilon': 'ε', r'\zeta': 'ζ', r'\eta': 'η',
        r'\theta': 'θ', r'\iota': 'ι', r'\kappa': 'κ', r'\lambda': 'λ',
        r'\mu': 'μ', r'\nu': 'ν', r'\xi': 'ξ', r'\pi': 'π', r'\rho': 'ρ',
        r'\sigma': 'σ', r'\tau': 'τ', r'\upsilon': 'υ', r'\phi': 'φ',
        r'\varphi': 'φ', r'\chi': 'χ', r'\psi': 'ψ', r'\omega': 'ω',
        r'\Gamma': 'Γ', r'\Delta': 'Δ', r'\Theta': 'Θ', r'\Lambda': 'Λ',
        r'\Xi': 'Ξ', r'\Pi': 'Π', r'\Sigma': 'Σ', r'\Phi': 'Φ',
        r'\Psi': 'Ψ', r'\Omega': 'Ω',
        r'\sqrt': '√', r'\infty': '∞', r'\cdot': '·', r'\times': '×',
        r'\pm': '±', r'\leq': '≤', r'\geq': '≥', r'\neq': '≠',
        r'\approx': '≈', r'\propto': '∝', r'\partial': '∂', r'\nabla': '∇',
        r'\int': '∫', r'\sum': 'Σ', r'\prod': 'Π',
        r'\mathrm': '', r'\mathbf': '', r'\mathit': '', r'\text': '',
        r'\dot': '', r'\ddot': '', r'\hat': '', r'\bar': '', r'\vec': '',
        r'\tilde': '', r'\overline': '',
    }
    for latex_cmd, replacement in GREEK.items():
        t = t.replace(latex_cmd + ' ', replacement + ' ')
        t = t.replace(latex_cmd + '{', replacement + '{')
        t = t.replace(latex_cmd, replacement)

    # ── 7. Strip remaining \command patterns ─────────────────────────────────
    t = _re_lt.sub(r'\\[a-zA-Z]+', '', t)

    # ── 8. Strip bare { } braces left over from \frac etc. ───────────────────
    t = _re_lt.sub(r'(?<!\$)\{([^}]*)\}', r'\1', t)

    # ── 9. Restore protected backslash then clean up whitespace ──────────────
    t = t.replace('\x00BSLASH\x00', '')
    t = _re_lt.sub(r'[ \t]+', ' ', t).strip()
    return t


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
  "lesson_tasks": [
    {
      "id": "Q_task",
      "name": "Heat Loss Rate Q",
      "symbol": "Q",
      "formula": "Q = h × A × (Ts − T∞)",
      "why": "Find the rate of heat transfer from the hot plate to the surrounding air.",
      "meaning": "h = convective coefficient, A = area, ΔT = temperature difference.",
      "given": "h = 25 W/m²·K\nA = 2 m²\nTs = 150 °C, T∞ = 30 °C",
      "work": [
        {"label": "1. Compute temperature difference", "eq": "ΔT = 150 − 30 = 120 K"},
        {"label": "2. Substitute into formula", "eq": "Q = 25 × 2 × 120 = 6000 W"}
      ],
      "value": "6000",
      "unit": "W",
      "note": "Heat loss from the hot plate"
    }
  ],
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
- lesson_tasks: CRITICAL — one entry per answer to find. If the problem asks for multiple answers
  (e.g. IP, FP, IMEP, efficiency), create one task per answer. If only one answer required, make one task.
  Each task has: id (JS identifier), name (full name), symbol, formula (plain text), why (one sentence
  explaining what this finds), meaning (what each symbol in the formula means), given (multiline string
  listing the values used, one per line as "symbol = value unit"), work (array of {label, eq} objects
  showing 1–4 step-by-step calculation steps), value (numeric result as string), unit, note (brief
  context note like "Power lost to friction").
- customize.fields: one entry per GIVEN numeric value (not the unknown). id = valid JS identifier. default = original numeric value as a number. For percentage values (e.g. 25% loss) use the raw percentage number as default (e.g. 25, not 0.25).
- customize.compute_js: JS function body (not the function declaration) that receives vals (object keyed by field id) and _fmt(v) helper. Must return {answer, answer_unit, answer_label, derived:{label:value_str}}. ALWAYS include a guard for invalid inputs (e.g. zero height, negative fraction). For percentage fields, convert inside JS: var frac = 1 - vals.loss/100;
- customize.question_template: question text with {id} placeholders for each field.
- ALWAYS include the customize block. NEVER omit it, even for rolling/rotation/energy questions.
- ALWAYS include the lesson_tasks block. NEVER omit it.
- compute_js body: write plain JS braces { } — do NOT escape them. The host will not re-process them.
- Pure JSON only.
- CRITICAL — NEVER USE LaTeX NOTATION. All formulas, equations, and expressions MUST be
  written in plain text / Unicode only. Use: ×, ·, /, √, ^, Greek letters (α, β, ω, etc.),
  superscripts (², ³), subscripts (_0, _min), fractions as (a)/(b). NEVER use \\frac, \\left,
  \\right, \\omega, \\alpha, \\sqrt, \\cdot, or ANY LaTeX backslash command.
  Example CORRECT: "u_min = m0*g / alpha"  Example WRONG: "u_{min} = \\frac{m_0 g}{\\alpha}"

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
        "lesson_tasks": [{
            "id": "answer",
            "name": "Final Answer",
            "symbol": "?",
            "formula": "Governing formula",
            "why": "Find the unknown quantity using the governing formula.",
            "meaning": "Apply the formula with the given values.",
            "given": "Given values from the problem",
            "work": [{"label": "Substitute values", "eq": "Apply formula with given data"}, {"label": "Compute result", "eq": "Final answer"}],
            "value": "?",
            "unit": "",
            "note": "Apply the governing formula"
        }],
        # Fallback customize — a minimal single-field panel so the button always works
        "customize": {"fields": [], "compute_js": "", "question_template": ""},
        "_fallback": True,
    }
    if _gemini_client is None:
        return FALLBACK
    for attempt in range(1, 4):
        try:
            raw = _call_gemini(
                (
                    "Solve this problem step by step with DETAILED, NUMBERED solution steps "
                    "(Step 1, Step 2, …). Each step must state what is being done and show "
                    "the intermediate calculation. Use plain Unicode for all math — no LaTeX. "
                    "Return ONLY valid JSON as specified.\n\n"
                    + question[:1500]
                ),
                _SOLUTION_SYSTEM,
                max_tokens=MAX_TOKENS_SOLUTION,
            )
            data = json.loads(_sanitize_json(raw))
            if data.get("steps") and data.get("final_answer"):
                # ── LaTeX sanitization: clean all formula/equation fields ──
                for _fkey in ("formula", "formula_name", "final_answer", "key_insight"):
                    if _fkey in data:
                        data[_fkey] = _clean_latex(str(data[_fkey]))
                for _step in data.get("steps", []):
                    if isinstance(_step, str):
                        data["steps"][data["steps"].index(_step)] = _clean_latex(_step)
                for _sc in data.get("substitution_chain", []):
                    if isinstance(_sc, dict) and "eq" in _sc:
                        _sc["eq"] = _clean_latex(_sc["eq"])
                for _ap in data.get("approach_steps", []):
                    if isinstance(_ap, dict):
                        if "eq"    in _ap: _ap["eq"]    = _clean_latex(_ap["eq"])
                        if "label" in _ap: _ap["label"] = _clean_latex(_ap["label"])
                for _var in data.get("variables", []):
                    if isinstance(_var, dict):
                        for _vk in ("name", "value"):
                            if _vk in _var: _var[_vk] = _clean_latex(_var[_vk])
                # ── lesson_tasks cleanup ──────────────────────────────────────
                for _lt in data.get("lesson_tasks", []):
                    if not isinstance(_lt, dict):
                        continue
                    for _lk in ("formula", "why", "meaning", "note"):
                        if _lk in _lt:
                            _lt[_lk] = _clean_latex(str(_lt[_lk]))
                    for _lw in _lt.get("work", []):
                        if isinstance(_lw, dict):
                            if "eq"    in _lw: _lw["eq"]    = _clean_latex(_lw["eq"])
                            if "label" in _lw: _lw["label"] = _clean_latex(_lw["label"])
                # ── Ensure lesson_tasks always exists ─────────────────────────
                if not data.get("lesson_tasks"):
                    data["lesson_tasks"] = [{
                        "id": "answer",
                        "name": data.get("formula_name", "Final Answer"),
                        "symbol": str(data.get("answer_value", "?")),
                        "formula": _clean_latex(str(data.get("formula", "Governing formula"))),
                        "why": "Find the unknown quantity using the governing formula.",
                        "meaning": "Apply the formula with the given values.",
                        "given": "\n".join(data.get("given_list", ["Given values from the problem"])),
                        "work": [
                            {"label": ap.get("label", ""), "eq": ap.get("eq", "")}
                            for ap in data.get("approach_steps", [])
                        ] or [{"label": "Substitute and solve", "eq": data.get("final_answer", "See solution")}],
                        "value": str(data.get("answer_value", "?")),
                        "unit": str(data.get("answer_unit", "")),
                        "note": data.get("key_insight", "Apply the governing formula.")[:80],
                    }]
                Log.ok("Solution", f"Got solution: {data.get('final_answer', '')[:60]}")
                return data

        except Exception as e:
            Log.warn("Solution", f"Attempt {attempt} failed: {e}")
    return FALLBACK


# ===========================================================================
# Stage 2: Scene Script Analyzer
# ===========================================================================
_SCENE_SYSTEM = """You are QAnim Scene Analyzer — a world-class physics educator and visual storyteller.
Given a student question, produce a richly detailed, structured animation scene script in JSON
for a 6-step SVG concept animation that tells the complete physical story of the problem.

Your scene script must read like a professional science documentary:
  - Every step builds on the previous one, adding ONE specific element.
  - Descriptions are vivid, accurate, and genuinely helpful for a student who is seeing
    this concept for the first time.
  - Every physical quantity must be named, labeled with its symbol, given its units,
    and explained in plain English — never assume prior knowledge.
  - Use directional language (upward ↑, downward ↓, leftward ←, rightward →) wherever applicable.
  - If the problem involves forces, clearly state each force's direction and magnitude.
  - If the problem involves motion, describe the trajectory, speed, and direction of motion.
  - If the problem involves heat, describe the source, sink, and direction of transfer.

Steps 1–6 build a visual explanation of the physical setup, one element at a time.
NO formulas, NO calculations, NO solution steps in ANY step description.

============================================================
OBJECT NAMING RULE — CRITICAL
============================================================

Every step title and description MUST:
  - Name the SPECIFIC physical object introduced in that step by its REAL NAME
    (e.g. "Rocket", "Exhaust Gas", "Metal Wire", "Charged Sphere", "Satellite",
     "Connecting Rod", "Slider-Crank Mechanism", "Flat Plate", "Fluid Stream").
  - State its symbol/notation explicitly in brackets, e.g. "u (exhaust speed)",
    "α (mass-loss rate)", "L (wire length)", "m₀ (initial mass)", "θ (crank angle)".
  - Use the EXACT variable names from the problem statement — never invent new ones.
  - The student reading the description must immediately know WHAT object it is,
    WHAT quantity belongs to it, its VALUE (if given), and its DIRECTION (if applicable).
  - Badges must list the specific quantity name, symbol, value (if given), and unit.

Example of GOOD step title:   "Step 3: Exhaust Gas — Downward Jet at Speed u (exhaust speed)"
Example of BAD  step title:   "Step 3: First Given Value"

Example of GOOD description:  "The rocket body (mass m₀ = 1000 kg) sits on the launch pad,
                                pointing vertically upward. Earth's gravity pulls it downward
                                with weight force W = m₀ × g acting at the centre of mass."
Example of BAD  description:  "The main object is introduced."

Example of GOOD badge:        {"text": "m₀ = 1000 kg (initial mass)", "type": "cyan"}
Example of BAD  badge:        {"text": "Given 1", "type": "cyan"}

============================================================
EXAMPLE 1 — Stretched Wire (electrical resistance)
============================================================

Return ONLY valid JSON:
{
  "title": "Resistance of a Stretched Wire",
  "topic": "PHYSICS",
  "steps": [
    {
      "step_number": 1,
      "label": "Environment",
      "title": "Step 1: The Problem Environment — Reference Scale",
      "description": "We set up a reference grid so we can clearly measure the dimensions of the metal wire in this experiment.",
      "badges": [{"text": "Reference scale", "type": "cyan"}],
      "layers_visible": ["layer-frame"],
      "layer_new": "layer-frame",
      "blur": false
    },
    {
      "step_number": 2,
      "label": "Wire",
      "title": "Step 2: The Metal Wire — Length L, Cross-section A",
      "description": "The main object is a metal wire of initial length L and cross-sectional area A. This is the wire whose resistance we will track.",
      "badges": [{"text": "Wire: length L", "type": "cyan"}, {"text": "Area: A", "type": "cyan"}],
      "layers_visible": ["layer-frame", "layer-object"],
      "layer_new": "layer-object",
      "blur": true
    },
    {
      "step_number": 3,
      "label": "R₁",
      "title": "Step 3: Initial Resistance — R₁ = 10 Ω (given)",
      "description": "An ohmmeter is connected to the metal wire and measures the initial resistance R₁ = 10 Ω. This is our first given quantity.",
      "badges": [{"text": "R₁ = 10 Ω (given)", "type": "green"}],
      "layers_visible": ["layer-frame", "layer-object", "layer-param1"],
      "layer_new": "layer-param1",
      "blur": true
    },
    {
      "step_number": 4,
      "label": "Force F",
      "title": "Step 4: Applied Tension — Force F on the Wire",
      "description": "A mechanical tension force F is applied to both ends of the metal wire, causing it to stretch. The force direction is shown by arrows.",
      "badges": [{"text": "Force F applied", "type": "orange"}, {"text": "Wire stretching", "type": "cyan"}],
      "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2"],
      "layer_new": "layer-param2",
      "blur": true
    },
    {
      "step_number": 5,
      "label": "New L₂",
      "title": "Step 5: Stretched Wire — New Length L₂ = 2L",
      "description": "After stretching, the metal wire now has double its original length: L₂ = 2L. Because volume is conserved, the cross-sectional area A decreases accordingly.",
      "badges": [{"text": "L₂ = 2L (new length)", "type": "cyan"}, {"text": "Volume = constant", "type": "orange"}],
      "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2", "layer-derived"],
      "layer_new": "layer-derived",
      "blur": true
    },
    {
      "step_number": 6,
      "label": "Setup",
      "title": "Step 6: Complete Setup — Find New Resistance R₂ = ?",
      "description": "All given data is assembled. The metal wire (original L, area A, resistance R₁ = 10 Ω) is now stretched to L₂ = 2L at constant volume. We must find the new resistance R₂.",
      "badges": [{"text": "R₁ = 10 Ω (given)", "type": "cyan"}, {"text": "L₂ = 2L (given)", "type": "cyan"}, {"text": "Volume = const", "type": "orange"}, {"text": "R₂ = ? (to find)", "type": "green"}],
      "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2", "layer-derived", "layer-summary"],
      "layer_new": "layer-summary",
      "blur": false
    }
  ],
  "svg_layers": {
    "layer-frame": {"description": "Background grid and reference frame", "color": "#4a6a8a"},
    "layer-object": {"description": "The metal wire (main physical object)", "color": "#0891b2"},
    "layer-param1": {"description": "Initial resistance R₁ measurement", "color": "#16a34a"},
    "layer-param2": {"description": "Applied tension force F and stretching arrows", "color": "#d97706"},
    "layer-derived": {"description": "Stretched wire — new length L₂ = 2L", "color": "#0891b2"},
    "layer-summary": {"description": "Setup summary — all given values and R₂ = ?", "color": "#7c3aed"}
  },
  "to_find": ["New resistance R₂ of the stretched wire"],
  "color_legend": [
    {"label": "Environment", "color": "#0ea5e9"},
    {"label": "Wire", "color": "#10b981"},
    {"label": "R₁ (given)", "color": "#f59e0b"},
    {"label": "Force F", "color": "#6366f1"},
    {"label": "New L₂", "color": "#f43f5e"},
    {"label": "Setup", "color": "#22c55e"}
  ],
  "glossary": [
    {"term": "resistance", "meaning": "How much a material opposes the flow of electric current."},
    {"term": "cross-sectional area", "meaning": "The area of a slice cut perpendicular to the length of the wire."},
    {"term": "volume conservation", "meaning": "When the wire stretches, its total volume stays the same, so area must shrink."}
  ]
}

============================================================
EXAMPLE 2 — Rocket Liftoff (exhaust speed / mass-loss rate)
============================================================

{
  "title": "Rocket Vertical Launch — Minimum Exhaust Speed",
  "topic": "PHYSICS",
  "steps": [
    {
      "step_number": 1,
      "label": "Environment",
      "title": "Step 1: The Problem Environment — Vertical Launch Setting",
      "description": "We establish the vertical launch environment: the ground, the upward direction, and the gravitational field g acting downward on all objects in this problem.",
      "badges": [{"text": "Vertical direction ↑", "type": "cyan"}, {"text": "Gravity g ↓", "type": "orange"}],
      "layers_visible": ["layer-frame"],
      "layer_new": "layer-frame",
      "blur": false
    },
    {
      "step_number": 2,
      "label": "Rocket",
      "title": "Step 2: The Rocket — Initial Mass m₀",
      "description": "The rocket sits on the launch pad. Its initial total mass is m₀ (rocket body + unburned fuel). The rocket will be launched vertically upward.",
      "badges": [{"text": "Rocket: mass m₀", "type": "cyan"}, {"text": "Direction: upward ↑", "type": "cyan"}],
      "layers_visible": ["layer-frame", "layer-object"],
      "layer_new": "layer-object",
      "blur": true
    },
    {
      "step_number": 3,
      "label": "Exhaust u",
      "title": "Step 3: Exhaust Gas — Speed u (exhaust speed, given)",
      "description": "The rocket expels exhaust gas downward at speed u relative to the rocket. This exhaust speed u is the quantity we need to find the minimum value of. The exhaust jet is shown as a downward arrow labeled u.",
      "badges": [{"text": "Exhaust gas ↓", "type": "orange"}, {"text": "Speed u (to find min)", "type": "green"}],
      "layers_visible": ["layer-frame", "layer-object", "layer-param1"],
      "layer_new": "layer-param1",
      "blur": true
    },
    {
      "step_number": 4,
      "label": "Mass-loss α",
      "title": "Step 4: Mass-Loss Rate — α (alpha, given)",
      "description": "The rocket burns fuel and loses mass at a constant rate α (alpha) in kg/s. This is the mass-loss rate given in the problem. The rocket body becomes lighter as fuel is expelled.",
      "badges": [{"text": "Mass-loss rate α (given)", "type": "cyan"}, {"text": "Units: kg/s", "type": "orange"}],
      "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2"],
      "layer_new": "layer-param2",
      "blur": true
    },
    {
      "step_number": 5,
      "label": "Thrust",
      "title": "Step 5: Rocket Thrust — Force = u × α (upward)",
      "description": "The thrust force on the rocket acts upward and equals u × α (exhaust speed × mass-loss rate). For the rocket to accelerate upward at launch, this thrust must exceed the rocket's weight m₀ × g.",
      "badges": [{"text": "Thrust = u·α ↑", "type": "cyan"}, {"text": "Weight = m₀g ↓", "type": "orange"}, {"text": "Condition: u·α > m₀g", "type": "orange"}],
      "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2", "layer-derived"],
      "layer_new": "layer-derived",
      "blur": true
    },
    {
      "step_number": 6,
      "label": "Setup",
      "title": "Step 6: Complete Setup — Find Minimum Exhaust Speed u_min = ?",
      "description": "All objects and quantities are in place. The rocket (mass m₀) is launched upward. Exhaust gas leaves at speed u (downward). Mass-loss rate is α. Gravity is g. We must find the minimum value of u so that the net force on the rocket is upward at launch.",
      "badges": [{"text": "Rocket mass m₀", "type": "cyan"}, {"text": "Mass-loss rate α", "type": "cyan"}, {"text": "Gravity g ↓", "type": "cyan"}, {"text": "u_min = ? (to find)", "type": "green"}],
      "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2", "layer-derived", "layer-summary"],
      "layer_new": "layer-summary",
      "blur": false
    }
  ],
  "svg_layers": {
    "layer-frame": {"description": "Vertical launch environment — ground, sky, gravity direction", "color": "#4a6a8a"},
    "layer-object": {"description": "The rocket — body with initial mass m₀ on launch pad", "color": "#0891b2"},
    "layer-param1": {"description": "Exhaust gas jet — downward arrow labeled u (exhaust speed)", "color": "#d97706"},
    "layer-param2": {"description": "Mass-loss rate α — annotation showing fuel consumption", "color": "#16a34a"},
    "layer-derived": {"description": "Thrust force (u·α ↑) vs weight (m₀g ↓) force diagram", "color": "#7c3aed"},
    "layer-summary": {"description": "Summary callout — all given values, u_min = ? highlighted", "color": "#dc2626"}
  },
  "to_find": ["Minimum exhaust speed u_min for upward acceleration at launch"],
  "color_legend": [
    {"label": "Environment", "color": "#0ea5e9"},
    {"label": "Rocket (m₀)", "color": "#10b981"},
    {"label": "Exhaust u", "color": "#f59e0b"},
    {"label": "Mass-loss α", "color": "#6366f1"},
    {"label": "Thrust vs Weight", "color": "#a855f7"},
    {"label": "Setup", "color": "#22c55e"}
  ],
  "glossary": [
    {"term": "exhaust speed u", "meaning": "The speed at which hot gas is expelled backward out of the rocket engine, relative to the rocket."},
    {"term": "mass-loss rate α", "meaning": "The rate at which the rocket loses mass by burning and expelling fuel, measured in kg/s."},
    {"term": "thrust", "meaning": "The forward (upward) push on the rocket caused by the reaction to expelling exhaust gas downward."},
    {"term": "liftoff condition", "meaning": "The rocket lifts off only when the upward thrust exceeds the downward gravitational weight."}
  ]
}

============================================================
STRICT RULES
============================================================

1. EXACTLY 6 steps. No more, no less.
2. Step 1 (label "Environment"): Introduce the physical environment and setting ONLY.
   - Describe the physical world: ground, sky, fluid medium, gravitational field, temperature field,
     electric field, coordinate axes — whatever frames this specific problem.
   - Add directional indicators: gravity arrow pointing ↓, coordinate origin, scale reference.
   - NO physical objects introduced yet — environment only.
   - Badges: describe the environment type (e.g. "Gravitational field: g = 9.81 m/s² ↓").
3. Step 2 (label = real object name): Introduce the MAIN physical object.
   - Name it explicitly (e.g. "Rocket Body", "Flat Metal Plate", "Slider-Crank Mechanism").
   - Give its key physical property and symbol (mass m₀, length L, radius R, etc.).
   - Describe its shape, orientation, and position in the physical environment.
4. Steps 3–5: Each step introduces exactly ONE specific physical quantity, agent, or secondary object.
   EVERY title MUST include:
   a) The REAL NAME of the quantity/object (e.g. "Exhaust Gas Jet", "Applied Tension", "Thermal Gradient")
   b) Its symbol/notation and units (e.g. "u (exhaust speed) [m/s]", "F (tension) [N]", "ΔT [K]")
   c) Whether it is GIVEN, DERIVED, or the UNKNOWN to find.
   d) Its direction, value (if given), and physical meaning in one sentence.
5. Step 6 (blur = false, all layers visible): Title says "Complete Setup — Find [Unknown Symbol] = ?"
   - Summarise ALL named objects and quantities already shown in the description.
   - State what to find and why it is the goal.
   - Badges: ALL given values (cyan), ALL derived quantities (orange), the unknown (green).
   - Description must mention every quantity from steps 1–5 by name and symbol.
6. Steps 2–5: blur = true (to focus attention on the newly introduced element).
7. NEVER include formulas, equations, or solution steps in ANY description. All text is plain English.
8. svg_layers must list every layer ID used in any step's layers_visible.
   Layer descriptions must use the SPECIFIC object/quantity name, not generic placeholders.
9. to_find: 1–3 strings describing what the student must find — use specific quantity names and symbols.
10. color_legend: one entry per step, labels must be the real object/quantity names from the problem.
11. glossary: 2–5 genuinely difficult technical terms from THIS problem only, with simple plain-English
    explanations that a high-school student can understand.
12. Return PURE JSON only — no markdown, no fences, no extra text.
13. NOTATION STYLE — ACADEMIC / FORMAL (CRITICAL):
    All symbols in step titles, descriptions, and badges MUST follow academic/print-textbook style:
    • Greek letters: use Unicode directly — α β γ δ ε η θ κ λ μ ν ξ π ρ σ τ φ χ ψ ω Δ Ω
      e.g. write η (efficiency), μ (friction coefficient), ρ (density), ω (angular velocity)
    • Subscripts: use Unicode subscript digits/letters where available — v₀ T₁ R₂ m₀ a_n Fₜ
    • Superscripts: use Unicode superscript characters — m² m³ v² s⁻¹ rad²
    • Fractions: write as (numerator)/(denominator) with slash, e.g. (m₀×g)/α, or use ÷ sign
    • Operators: × (multiply), ÷ (divide), · (dot product), √ (square root), ∑ (sum), ∫ (integral)
    • Relations: ≥ ≤ ≈ ≠ ∝ ⇒ → ↔ ≠
    • Style: formal and compact, like a printed LaTeX derivation — no casual abbreviations
    NEVER use \\alpha, \\omega, \\frac, \\sqrt, $...$, or any LaTeX backslash command."""


def analyze_scene(question: str) -> dict:
    """Call Gemini to produce the scene script."""
    FALLBACK = {
        "title": question[:60],
        "topic": "PHYSICS",
        "steps": [
            {
                "step_number": 1, "label": "Environment",
                "title": "Step 1: The Problem Environment — Physical Setting",
                "description": "We establish the physical environment for this problem: the setting, direction, and any background conditions such as gravity or the medium.",
                "badges": [{"text": "Environment: set up", "type": "cyan"}],
                "layers_visible": ["layer-frame"], "layer_new": "layer-frame", "blur": False
            },
            {
                "step_number": 2, "label": "Main Object",
                "title": "Step 2: The Main Physical Object — Identified",
                "description": "The primary object of this problem is introduced with its key property and the symbol used to represent it in the solution.",
                "badges": [{"text": "Main object: identified", "type": "cyan"}],
                "layers_visible": ["layer-frame", "layer-object"], "layer_new": "layer-object", "blur": True
            },
            {
                "step_number": 3, "label": "Given Quantity 1",
                "title": "Step 3: First Given Quantity — Symbol and Value",
                "description": "The first physical quantity given in the problem is named and labeled with its symbol and units. This quantity will be used directly in the governing equation.",
                "badges": [{"text": "Given quantity 1: labeled", "type": "cyan"}],
                "layers_visible": ["layer-frame", "layer-object", "layer-param1"], "layer_new": "layer-param1", "blur": True
            },
            {
                "step_number": 4, "label": "Given Quantity 2",
                "title": "Step 4: Second Given Quantity — Symbol and Value",
                "description": "The second physical quantity given in the problem is named and added to the diagram with its symbol, value, and direction or units as appropriate.",
                "badges": [{"text": "Given quantity 2: labeled", "type": "cyan"}],
                "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2"], "layer_new": "layer-param2", "blur": True
            },
            {
                "step_number": 5, "label": "Key Condition",
                "title": "Step 5: Key Physical Condition or Derived Quantity",
                "description": "An important condition, constraint, or intermediate quantity relevant to this problem is highlighted. This bridges the given data to what we need to find.",
                "badges": [{"text": "Key condition: shown", "type": "orange"}],
                "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2", "layer-derived"], "layer_new": "layer-derived", "blur": True
            },
            {
                "step_number": 6, "label": "Complete Setup",
                "title": "Step 6: Complete Setup — All Objects and Quantities Identified",
                "description": "All named objects and given quantities from the problem are now assembled in the diagram. The unknown quantity to be found is clearly marked.",
                "badges": [{"text": "All given: assembled", "type": "cyan"}, {"text": "Unknown = ? (to find)", "type": "green"}],
                "layers_visible": ["layer-frame", "layer-object", "layer-param1", "layer-param2", "layer-derived", "layer-summary"], "layer_new": "layer-summary", "blur": False
            },
        ],
        "svg_layers": {
            "layer-frame": {"description": "Physical environment — background, direction, gravity", "color": "#4a6a8a"},
            "layer-object": {"description": "Main physical object of the problem", "color": "#0891b2"},
            "layer-param1": {"description": "First given quantity — named, labeled, with units", "color": "#16a34a"},
            "layer-param2": {"description": "Second given quantity — named, labeled, with units", "color": "#d97706"},
            "layer-derived": {"description": "Key physical condition or derived quantity", "color": "#7c3aed"},
            "layer-summary": {"description": "Summary overlay — all given values and unknown highlighted", "color": "#0891b2"},
        },
        "to_find": ["The unknown quantity — see problem statement"],
        "color_legend": [
            {"label": "Environment", "color": "#0ea5e9"},
            {"label": "Main Object", "color": "#10b981"},
            {"label": "Given 1", "color": "#f59e0b"},
            {"label": "Given 2", "color": "#6366f1"},
            {"label": "Condition", "color": "#f43f5e"},
            {"label": "Setup", "color": "#22c55e"},
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
                (
                    "Produce a DETAILED, RICHLY DESCRIBED 6-step scene script for the following "
                    "student question. Each step must name the specific physical object or quantity "
                    "introduced, give its symbol and units, describe its physical role clearly, "
                    "and use directional language where applicable (↑ ↓ → ←). "
                    "Write descriptions as if narrating a professional science documentary. "
                    "Do NOT use generic placeholders like 'Given 1' or 'Main object'. "
                    "Return PURE JSON only.\n\n"
                    + question[:1500]
                ),
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
You are QAnim Studio — a world-class SVG artist, physics visualizer, and educational motion designer.

Your task: for ANY physics/math/engineering question, generate a stunning, accurate, richly detailed
6-step SVG concept animation. This animation is the FIRST thing a student sees — it must immediately
clarify the physical setup before any formulas appear. Think of it as a professional science museum
exhibit brought to life in SVG.

EVERY element — objects, labels, arrows, annotations, callouts — must be SPECIFIC to the question.
NEVER use generic placeholder text. ALWAYS use the real object names, symbols, and values from the problem.

============================================================
OUTPUT FORMAT
============================================================

Return ONLY valid JSON with exactly these five fields:

{
  "svg_defs": "...",
  "svg_layers": "...",
  "steps_data_js": "...",
  "apply_step_js": "...",
  "raf_js": "..."
}

No Markdown fences. No extra text. Only the JSON object.

============================================================
VISUAL GOAL — HIGH-QUALITY SCIENTIFIC ILLUSTRATION
============================================================

Create a PREMIUM, PHOTOREALISTIC scientific visualization perfectly tailored to the question.
The animation must look like it belongs in a professional science textbook, research paper,
or interactive museum exhibit. Every frame must be publication-quality.

Core quality targets:
  - Objects look like real physical entities — not icons or clip art.
  - Lighting, shading, and gradients suggest 3-dimensional form.
  - Every label is mathematically precise with correct symbols and units.
  - Every arrow conveys real physical information (force, velocity, heat flow, field direction).
  - Each step clearly adds ONE new insight — the student should say "I see it now!" at each step.
  - Smooth, physics-accurate animation makes the concept intuitive, not just decorative.


============================================================
ULTRA-REALISM MANDATE — SCENES 1-6 (MANDATORY)
============================================================

You are creating a MUSEUM-GRADE science exhibit. Every element must look like it
belongs in a professional physics textbook illustration or an award-winning science
documentary. Follow these absolute requirements for scenes 1-6:

A) ENVIRONMENT (Step 1 — layer-frame):
   • Draw a FULL, RICH background environment — not just a grid.
   • Include physically accurate context: launch pad with flame trench (rocket),
     laboratory bench with measurement instruments (wire/circuit), orbital space
     background with stars (satellite), laboratory furnace room (heat transfer), etc.
   • Add dimension markers, coordinate axes (x, y) with arrowheads, a scale bar.
   • Sky/ground boundary: ground is a thick hatched line with soil texture below,
     sky/atmosphere gradient above. Include horizon glow if relevant.
   • All environmental labels in 'Times New Roman' serif, italic for symbols.

B) MAIN OBJECT (Step 2 — layer-object):
   • Draw the MOST DETAILED version of the object you can produce in SVG.
   • Every structural component visible: rivets, seams, panels, nozzle bell,
     wing spars, water meniscus, crystal lattice, axle bore — whatever is real.
   • Use 3-layer gradient shading (highlight: 80% lightness → base → shadow: 40%
     lightness). The highlight is always top-left; shadow is always bottom-right.
   • Add a subtle specular highlight (small white oval, 30% opacity) to every
     rounded surface to convey 3-D solidity.
   • Add micro-texture lines or patterns inside the object to suggest material
     (metal grain = thin parallel lines; insulation = wavy fill; fluid = wavy
     gradient; wood = curved grain lines; circuit board = green with traces).

C) PHYSICAL QUANTITIES (Steps 3-5 — layer-param1/2/derived):
   • EVERY force, velocity, and field quantity MUST be drawn as a PROFESSIONAL
     VECTOR ARROW:
       - Thick shaft (stroke-width ≥ 4) in a vivid accent colour
       - Filled arrowhead marker (not a plain line)
       - A white rounded-rect label background behind the text
       - Label in 'Times New Roman' serif, italic for the symbol
       - Proper sub/superscripts using SVG tspan baseline-shift
   • Force diagrams: draw ALL forces simultaneously in step 5, with tails at the
     object's centre of mass. Show the FREE-BODY DIAGRAM clearly.
   • Dimension arrows: double-headed arrows with tick marks at both ends, measured
     in SVG units proportional to the real physical value.
   • Annotate EVERY quantity with: symbol (italic serif) + "=" + value + unit

D) TYPOGRAPHY — TEXTBOOK STANDARD (ALL TEXT):
   • ALL formula/equation text: font-family="'Times New Roman', Georgia, serif"
     font-style="italic" for single-letter variables (v, F, x, t, α, ω, etc.)
   • Multi-letter names and units: font-style="normal" (roman)
   • Subscripts: <tspan baseline-shift="sub" font-size="72%">0</tspan>
   • Superscripts: <tspan baseline-shift="super" font-size="72%">2</tspan>
   • Fraction bars: stacked numerator / <line> / denominator in a white panel
   • Greek letters: α β γ δ ε η θ κ λ μ ν ξ π ρ σ τ φ χ ψ ω Δ Θ Ω
     (Unicode directly — NEVER \alpha, \omega, \frac, etc.)
   • ALL text dark: fill="#0f172a" (black ink) on white/light background
   • NEVER colour formula symbols themselves — colour only arrows and highlights

E) STEP 6 SUMMARY (layer-summary):
   • Render a FULL TEXTBOOK-STYLE data table listing ALL given values.
     Each row: symbol (serif italic) | name | value | unit
   • Highlight the unknown with a pulsing glow box (feGaussianBlur glow filter).
   • Draw a "FIND → " arrow pointing to the unknown symbol.
   • The summary box must have: rounded rect (rx=14), white fill, cyan border,
     drop-shadow, and gradient header bar in the primary accent colour.

============================================================
DRAWING REQUIREMENTS
============================================================

1. REALISTIC, SPECIFIC PHYSICAL OBJECTS:
   - Draw the ACTUAL physical entities from the problem — not generic boxes or circles.
   - Examples:
       • Rocket: streamlined fuselage body + nozzle + exhaust flame + stabiliser fins
       • Metal wire: cylindrical rod with end connectors + texture suggesting metal
       • Satellite: hexagonal body + solar panels + antenna dish + orbital path glow
       • Slider-crank: crank disc + connecting rod + piston in guide rail + ground pivot
       • Hot plate: solid rectangle with heat glow gradient + convection arrows rising
       • Projectile: ball/shell with velocity arrow + parabolic trajectory dashes
       • Charged sphere: metallic sphere with field lines radiating outward
       • Pendulum: rigid rod + bob + angular arc + pivot pin with ground hatch
   - Use gradients, shading, and highlights to convey 3-D solidity.
   - Every mechanical joint: add a small filled circle (pivot pin).
   - Every fixed support: add a ground hatch symbol (diagonal hatching below a baseline).
   - Avoid cartoonish, icon-like, or overly abstract shapes.

2. PERFECT, BALANCED LAYOUT:
   - Use viewBox="0 0 850 478" (landscape, 16:9-ish).
   - Centre the main physical system horizontally and vertically.
   - Maintain clear margins: all important content stays inside x=30..820, y=30..448.
   - Labels MUST NOT overlap each other or overlap the main object.
   - Use leader lines (thin lines from label to object) when labels cannot be placed directly adjacent.
   - For multi-component systems, lay out components left-to-right or bottom-to-top in reading order.
   - Composition must look balanced and uncluttered on a 1024×576 screen.

3. LAYER STRUCTURE:
   - Use exactly the layer IDs provided by the scene script (typically):
       <g id="layer-frame" style="opacity:1">...</g>   ← VISIBLE from the start
       <g id="layer-object" style="opacity:0">...</g>  ← hidden until Step 2
       <g id="layer-param1" style="opacity:0">...</g>  ← hidden until Step 3
       <g id="layer-param2" style="opacity:0">...</g>  ← hidden until Step 4
       <g id="layer-derived" style="opacity:0">...</g> ← hidden until Step 5
       <g id="layer-summary" style="opacity:0">...</g> ← hidden until Step 6
   - layer-frame: background environment (grid, axes, ground, sky, field lines, gravity arrow).
   - layer-object: the main physical object, drawn fully and realistically.
   - layer-param1 through layer-param4: one specific quantity/force/component each.
   - layer-summary: Step 6 summary callout listing all given values and the unknown.
   - Each layer must have a SINGLE, CLEAR visual purpose. No mixing of concepts.

4. MANDATORY PREMIUM QUALITY (every output MUST include ALL of these):
   a) GRADIENTS: Every main object MUST use at least one linearGradient or radialGradient
      to give it 3-D depth and material realism (metal, glass, glowing plasma, painted surface).
   b) DROP SHADOWS: Every main object MUST have a <filter> drop-shadow
      (feDropShadow stdDeviation="3" flood-color="#1e293b" flood-opacity="0.18") for visual lift.
   c) ROUND ENDS: stroke-linecap="round" and stroke-linejoin="round" on ALL mechanical parts.
   d) LABEL BACKGROUNDS: All text labels that float over the diagram MUST have a pill-shaped
      background <rect> behind them (rx≥6, fill="white" or fill="#f8fafc", opacity="0.88",
      slightly larger than the text bounding box) for readability.
   e) ARROWHEAD MARKERS: All annotation arrows (force vectors, velocity, heat flow, field,
      dimension lines) MUST use a <marker> arrowhead defined in <defs>. Never use bare lines.
   f) COLOUR HARMONY: Use a curated 4-colour accent palette (e.g. #0891b2 cyan, #2563eb blue,
      #d97706 amber, #7c3aed violet). NEVER use flat plain red, blue, or green.
   g) LAYER TRANSITIONS: Every layer reveal (in the RAF or applyStep) MUST include both
      opacity 0→1 AND a subtle transform: translateY(14px→0) or scale(0.93→1) for a
      smooth "pop in" feeling. Transitions: 700 ms, cubic-bezier(0.34,1.56,0.64,1).
   h) STEP 6 CALLOUT BOX: layer-summary MUST contain a polished summary callout:
      - Rounded rectangle (rx=14) with gradient fill (white→#f0f6ff) and a subtle coloured border.
      - A glowing label for the unknown: <rect> with filter glow + text "Unknown = ?".
      - List all given values as small pill badges inside the callout.
      - A "Find →" arrow pointing to the unknown label.

5. DESIGN LANGUAGE — LIGHT, PROFESSIONAL, HIGH-CONTRAST:
   - Background: ALWAYS white or very light (#f8fafc, #eef5ff, #f0f6ff). NO dark backgrounds.
   - Main objects: vivid, saturated accent colours on the light background.
   - Text: dark (#1e293b, #0f172a) — always readable against the light background.
   - Gradients ONLY on objects, never on the background.
   - Grid overlay: <pattern> with light grey lines (#cbd5e1, stroke-width="0.35", opacity 0.28).
   - Every element exists for a reason — no decorative clutter.

============================================================
SVG DEFS — MANDATORY CONTENTS
============================================================

Your <defs> section MUST contain at least all of the following (define only what you use):

a) BACKGROUND GRID PATTERN (always include):
   <pattern id="bg-grid" width="40" height="40" patternUnits="userSpaceOnUse">
     <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#cbd5e1" stroke-width="0.35"/>
   </pattern>

b) OBJECT GRADIENTS (at least 2; name them descriptively):
   Example IDs: grad-object, grad-rod, grad-crank, grad-plate, grad-wire, grad-piston,
   grad-rocket, grad-sphere, grad-fluid, grad-coil, grad-satellite, grad-highlight.
   Use vivid-to-dark stops for metal, glass, or glowing materials.
   Do NOT create dark background gradients — background stays white/light.

c) DROP-SHADOW FILTER (at least 1):
   <filter id="dropshadow" x="-15%" y="-15%" width="130%" height="130%">
     <feDropShadow dx="2" dy="3" stdDeviation="3"
       flood-color="#1e293b" flood-opacity="0.18"/>
   </filter>
   Additional glow filter for the unknown label in layer-summary:
   <filter id="glow-unknown">
     <feGaussianBlur stdDeviation="3" result="blur"/>
     <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
   </filter>

d) ARROWHEAD MARKERS (at least 2 sizes/colours):
   <marker id="arrow-dark" markerWidth="10" markerHeight="7"
     refX="10" refY="3.5" orient="auto">
     <polygon points="0 0, 10 3.5, 0 7" fill="#1e293b"/>
   </marker>
   <marker id="arrow-accent" markerWidth="10" markerHeight="7"
     refX="10" refY="3.5" orient="auto">
     <polygon points="0 0, 10 3.5, 0 7" fill="#d97706"/>
   </marker>
   Add additional markers in other accent colours as needed (blue, cyan, violet, green).

Do NOT use external images, external fonts, or data: URLs.
Do NOT embed raster images. All artwork must be pure SVG geometry.

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

SMOOTH PHYSICS MOTION (mandatory for ANY problem involving dynamics, motion, or processes):

- Projectile / ballistic:
    • Animate along a TRUE parabolic arc: x = x₀ + v₀cosθ·t, y = y₀ + v₀sinθ·t − ½g·t².
    • Show the trajectory trail as an incrementally growing dashed path (add one point per frame).
    • Show velocity components: horizontal arrow (constant length) + vertical arrow (shrinks then grows).
    • Show the peak height with a horizontal dashed line and label.
    • Display the time counter and current speed as animated text.

- Orbit / circular motion:
    • Orbit body with sin/cos for perfectly smooth, continuous orbital motion.
    • Show the faint elliptical orbit path as a dashed ellipse.
    • Planet/satellite casts a moving oval shadow below it.
    • Show the radial distance r as an animated line from centre to body.
    • Display the orbital speed v label near the body.

- Wave / oscillation / pendulum / SHM:
    • Render sinusoidal wave or pendulum with requestAnimationFrame, phase-shifting each frame.
    • Show the amplitude A, period T, and equilibrium position as labeled annotations.
    • For pendulum: show the angle θ arc and the restoring force arrow.

- Wire / elastic stretching:
    • Animate length change with a smooth scaleX or translate transform.
    • Show a dimension arrow (double-headed) that grows with the wire.
    • Label the new length L₂ appearing after the stretch.

- Heat / diffusion / conduction:
    • Animate a colour gradient that sweeps from hot (red #ef4444) to cold (blue #3b82f6) over time.
    • Show animated heat-flow arrows pointing from hot to cold region.
    • Label temperatures T₁ and T₂ at the boundaries.

- Fluid flow / convection:
    • Animate streamlines or particles along smooth curved SVG paths.
    • Show flow velocity arrows along the streamlines.
    • Animate particle opacity (appear, travel, disappear) for a continuous flow illusion.

- Rotating machinery (slider-crank, gears, cam-follower):
    • Use requestAnimationFrame with sin/cos kinematics for ALL moving parts simultaneously.
    • Draw crank as a rotating line with endpoint tracing an arc.
    • Draw connecting rod between crank pin and slider.
    • Slide the piston/slider left-right in the guide rail.
    • Show the crank angle θ as an animated arc with label near the pivot.
    • Show angular velocity ω and piston velocity v as animated arrows.

- Rocket / thrust:
    • Animate the rocket rising with a smooth translateY.
    • Animate the exhaust flame (animating multiple blobs/flames downward).
    • Show the thrust force arrow (↑ growing) and weight arrow (↓) as the rocket rises.

- Electrical circuits:
    • Animate current flow as moving dots along wire paths.
    • Show voltage labels appearing at nodes.
    • If a charging/discharging capacitor: animate fill level rising or falling.

RAF STRUCTURE (always use this exact pattern if animation is needed):

window.qanimStartRAF = function(){
  if (window.qanimRafId) cancelAnimationFrame(window.qanimRafId);
  window.qanimRafId = requestAnimationFrame(drawFrame);
};

If truly no continuous animation is needed (static label-only problem), return:
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
- badges: array of HTML strings. Each badge MUST use the real object name, symbol, value, and unit.
  Example: '<span class="badge badge-cyan">m₀ = 1000 kg (initial mass)</span>'
  NEVER use generic text like "Given 1" or "Quantity" in badges.
- blurOp = 0.0 for steps 1 and 6 (full clarity); 0.38 for steps 2–5 (focus on new element).
- layerOpacities must include ALL layer IDs (set to 0 or 1 per the scene script).
- Step 6 shows ALL layers simultaneously. Do NOT include "= ?" or "unknown" badges in step 6.
  Step 6 is the "Complete Setup" — show all GIVEN values as cyan badges, derived as orange,
  the unknown to find as a green badge. The unknown value is revealed in Steps 7-9.
- titles: use the real physical object/quantity name, not a generic placeholder.
- desc: write 2-3 clear sentences explaining what the student is seeing and why it matters.

============================================================
APPLY STEP JAVASCRIPT
============================================================

Return the body of applyStep(idx). This function is called every time the user
navigates to a new step. It MUST:

1. Set window.currentStep = idx  (the RAF loop reads this to drive step-specific motion).
2. Update progress bar:  document.getElementById('step-bar').style.width = ((idx+1)/9*100)+'%';
3. Update label:         document.getElementById('step-label').textContent = 'Step '+(idx+1)+' of 9';
4. Update step dots: loop i=0..CONCEPT_STEP_COUNT-1, set class 'active'/'done'/''/.
5. Set info panel text using textContent (NEVER innerHTML for user text):
     document.getElementById('info-title').textContent = stepsData[idx].title || '';
     document.getElementById('info-desc').textContent  = stepsData[idx].desc  || '';
6. Render badges (innerHTML is safe here — badges are HTML strings generated by Python):
     document.getElementById('info-badges').innerHTML = (stepsData[idx].badges || []).join('');
7. Set EACH layer's opacity using style.opacity (NEVER setAttribute):
     var ops = stepsData[idx].layerOpacities || {};
     Object.keys(ops).forEach(function(id){
       var el = document.getElementById(id);
       if (el) el.style.opacity = ops[id];
     });
8. Disable/enable prev/next buttons:
     var bp = document.getElementById('btn-prev');
     var bn = document.getElementById('btn-next');
     var total = window.CONCEPT_STEP_COUNT || 6;
     if (bp) bp.disabled = (idx === 0);
     if (bn) bn.disabled = (idx === total - 1);

CRITICAL RULES for applyStep:
- ALWAYS use element.style.opacity — NEVER el.setAttribute('opacity', value).
- ALWAYS use textContent for user-visible text — NEVER innerHTML for title/desc.
- NEVER put per-step motion code inside applyStep — ALL motion lives in the RAF loop.
- Progress bar divides by 9 (total scenes), not by 6 (concept steps only).

============================================================
NOTATION AND LABELS — ACADEMIC / FORMAL TYPESETTING
============================================================

All SVG text labels for mathematical quantities MUST follow academic print-textbook style,
replicating the look of LaTeX-rendered derivations (Times-like serif feel in content,
even though SVG uses its own font stack):

FONT FAMILY — Serif / LaTeX-style:
  - All formula labels and equation text: font-family="'Times New Roman', Georgia, serif"
  - All annotation labels (object names, units): font-family="'Times New Roman', Georgia, serif"
  - Variable names in italic feel: use font-style="italic" for single-letter variables (v, F, x, t)
  - Multi-letter identifiers and units: font-style="normal" (roman), e.g. "sin", "cos", "kg", "m/s"
  - Operator symbols (×, ·, =, +, −, /, ≥, ≤): font-style="normal", same serif font

NOTATION — Proper math symbols:
  - Greek letters (full set, use Unicode directly):
      α (alpha) β (beta) γ (gamma) δ (delta) ε (epsilon) η (eta) θ (theta)
      κ (kappa) λ (lambda) μ (mu) ν (nu) ξ (xi) π (pi) ρ (rho) σ (sigma)
      τ (tau) φ (phi) χ (chi) ψ (psi) ω (omega)
      Δ (Delta) Θ (Theta) Λ (Lambda) Σ (Sigma) Φ (Phi) Ω (Omega)
  - Subscripts via SVG tspan (preferred for clarity):
      <tspan baseline-shift="sub" font-size="72%">0</tspan> for v₀, m₀, T₀
      <tspan baseline-shift="sub" font-size="72%">1</tspan> for T₁, R₁, P₁
      <tspan baseline-shift="sub" font-size="72%">n</tspan> for F_n, a_n
      Also acceptable: Unicode subscript digits v₀ m₀ T₁ R₂ where rendering is reliable.
  - Superscripts via SVG tspan:
      <tspan baseline-shift="super" font-size="72%">2</tspan> for v², m², r³
      Also acceptable: Unicode superscripts v² m³ s⁻¹
  - Fraction bars — STACKED layout (numerator OVER denominator):
      For important formula labels, render fractions as two-line SVG text:
        Line 1 (numerator): e.g. "m₀ × g"
        Line 2 (fraction bar): a <line> element horizontally spanning the label width
        Line 3 (denominator): e.g. "α"
      This gives the true printed fraction-bar style, like:
          m₀g
          ———   ← horizontal line as fraction bar
           α
      For inline labels where stacking is impractical: use (m₀×g)/α
  - Special operators: × · ÷ √ ∑ ∫ ∂ ∇ ∞ ± ≥ ≤ ≠ ≈ ∝ ⇒
  - Units in roman (non-italic): kg, m, s, N, W, J, Pa, K, °C, m/s, m/s², Ω, rad
  - Vector notation: F⃗ v⃗ or bold with stroke, e.g. font-weight="bold"

COLOUR — Black ink on white (high contrast):
  - ALL formula/equation text: fill="#0f172a" (near-black, like printer ink)
  - Formula background panels: fill="#ffffff" or fill="#fafafa", stroke="#e2e8f0"
  - NEVER use coloured text for formula symbols themselves (colour only for arrows/highlights)
  - Quantity value labels (e.g. "= 981 m/s"): fill="#1e293b"
  - Units: fill="#334155" (slightly lighter than symbol, still dark)

LAYOUT — Print textbook style:
  - Equations left-aligned or centred in a white panel with subtle border.
  - Fraction-bar fractions centred with numerator, bar line, and denominator stacked vertically.
  - Operator spacing: leave 4–6px gap around ×, =, +, −, ≥.
  - Do not crowd labels — use leader lines (thin <line> from label to object) when needed.
  - Important formula panels: a rounded <rect> (rx=8, fill white, stroke #cbd5e1, shadow filter)
    with the equation rendered inside in serif font — looks like a textbook equation box.

EXAMPLE label styles (CORRECT vs WRONG):
  CORRECT: font-family="'Times New Roman', serif", font-style="italic" for “v₀ = 20 m/s”
  WRONG:   font-family="Arial", plain upright text
  CORRECT: “v₀ = 20 m/s” (Unicode subscript)
  WRONG:   “v0 = 20 m/s” (plain digit)
  CORRECT: “ΔT = 120 K”
  WRONG:   “DeltaT = 120 K”
  CORRECT: “R₂ = ?”
  WRONG:   “R2 = ?”
  CORRECT: fraction bar rendered as stacked SVG with <line> separator
  WRONG:   a/b written as a single flat text string (when a panel is available)

- Keep labels concise and never overlapping.
- Do not invent values — match the verified solution exactly.
- OBJECT NAMING: Every SVG label MUST use the REAL NAME and EXACT SYMBOL from the problem
  (e.g. "Rocket", "u (exhaust speed)", "α kg/s", "m₀ = initial mass").
  NEVER use generic placeholder labels like "object", "param", "value".
- NEVER USE LaTeX backslash commands in any SVG text element:
  Use α, ω, ², ·, ×, √, ∞ — NEVER \\alpha, \\omega, \\frac{}{}, \\left, \\right, $...$, etc.
  LaTeX in SVG renders as raw broken text — it completely breaks the visualization.

============================================================
REALISM BY SUBJECT — DETAILED DRAWING GUIDE
============================================================

ALWAYS use a LIGHT BACKGROUND (#f8fafc, #eef5ff, or #f0f6ff). NEVER dark backgrounds.
Adapt all details below to the EXACT objects and quantities in the question.

■ SLIDER-CRANK / FOUR-BAR LINKAGE / CAM-FOLLOWER MECHANISMS:
  - Crank: a thick rounded-end bar (stroke-width=5, stroke-linecap=round) rotating about a fixed pivot.
    Fill with steel-blue gradient (e.g. #2563eb → #1d4ed8). Show pivot as a solid filled circle (r=7).
  - Connecting rod: thick rounded bar between crank pin and slider pin.
    Fill with grey gradient (#64748b → #475569). Show pin joints as solid filled circles (r=5).
  - Slider/Piston: a filled rounded rectangle (rx=4) on a horizontal guide rail.
    Rail: two parallel lines with end stops; tick marks every 20px for scale.
  - Fixed pivot: equilateral triangle below the crank centre + diagonal hatching lines beneath it.
  - Crank angle θ: a curved arc from the 3 o'clock position to the crank arm, label "θ" at arc centre.
  - Slider displacement x: a dimension arrow (double-headed) from BDC to current slider position.
  - Velocity diagram: orange arrows (marker arrowhead) labeled ω (crank), v (slider), v_A (pin A).
  - All points labeled: O (fixed pivot), A (crank pin), B (slider pin), plus crank radius r, rod length l.

■ ROCKET / THRUST / PROPULSION:
  - Rocket body: a tall rounded pentagon (or use a <path> for a classic rocket silhouette) with:
    • Body gradient (white/silver top → grey bottom), a nose cone, and 2-4 stabiliser fins.
    • A circular viewport window near the top.
    • Engine nozzle at the bottom (flared trumpet shape).
  - Exhaust flame: animated multi-layer shapes below the nozzle — inner (white/yellow), mid (orange #f97316),
    outer (translucent red #ef4444). Animate with scaling + opacity pulse.
  - Forces:
    • Thrust arrow: thick cyan arrow pointing ↑ from nozzle, label "F_thrust = u·α ↑".
    • Weight arrow: thick orange arrow pointing ↓ from centre of mass, label "W = m₀g ↓".
  - Ground: a thick horizontal line with hatch below, launch pad rectangle.
  - Smoke trail: faint grey dashes trailing upward as rocket rises.

■ WIRE / RESISTANCE / ELECTRICAL PROPERTIES:
  - Wire: a cylindrical bar (use a linearGradient from bright metal top to dark shadow bottom).
    End connectors: small rectangles in copper colour (#b45309).
  - Ohmmeter / Voltmeter: draw a simple instrument circle with a meter needle.
  - Stretching: show the wire elongating with a horizontal scale transform; dimension arrows show L and L₂.
  - Cross-section: a small circle at the wire end showing area A reducing as length increases.
  - Resistivity annotation: a hatched texture inside the wire body to suggest material.

■ PROJECTILE / BALLISTIC MOTION:
  - Projectile: a solid sphere or shell (gradient-filled, drop shadow).
  - Launch platform: a ramp or cannon at the left, angled at θ degrees, labeled with v₀ and θ.
  - Trajectory: a smooth dashed parabolic arc from launch to landing. Incrementally reveal the trail.
  - Velocity arrows at multiple points along the arc:
    • vₓ (horizontal, constant length, pointing right)
    • vᵧ (vertical, shrinking to 0 at peak, then growing downward)
  - Peak height label: horizontal dashed line at maximum height H with label "H_max".
  - Range label: horizontal dimension arrow at ground level showing R.
  - Time counter: animated text near the top showing t = x.xx s.

■ SATELLITE / ORBITAL MECHANICS:
  - Planet: a large gradient-filled circle (blue-green for Earth, brown for other).
    Add a subtle atmosphere ring (radial gradient, translucent blue/cyan around the edge).
  - Satellite: hexagonal body with 2 rectangular solar panels + small antenna dish.
    Add a gradient to convey metallic surface.
  - Orbit path: a dashed ellipse (or circle) with subtle glow (filter: feGaussianBlur).
  - Radial vector r: animated line from planet centre to satellite, labeled "r".
  - Velocity arrow v: tangent to the orbit path, vivid accent colour, labeled "v".
  - Gravity arrow: from satellite toward planet centre, labeled "g" or "F_g".
  - Background: very light sky (#eef5ff) with 8-12 tiny star circles (r=1.5, fill=#94a3b8).

■ HEAT TRANSFER / THERMODYNAMICS:
  - Hot surface: a solid rectangle with a red-orange gradient (top: #ef4444, bottom: #b91c1c).
    Add a faint heat shimmer effect (wavy lines above the surface).
  - Cold fluid / ambient: a blue-tinted region (fill: #e0f2fe → #bae6fd) with flowing arrows.
  - Heat flow arrows: curved arrows (marker arrowhead) from hot to cold, coloured orange → blue gradient.
  - Temperature labels: T_s at hot surface, T_∞ at ambient edge, ΔT dimension bracket.
  - Convection arrows: rising curved arrows above the hot surface (animated upward drift).
  - Conduction gradient: a horizontal gradient bar showing temperature profile T(x).

■ PENDULUM / SHM / OSCILLATION:
  - Pivot: a horizontal bar fixed to the top (ceiling symbol with hatching).
  - Rod: a thick line (rounded ends) from pivot to bob.
  - Bob: a large gradient-filled circle (steel blue or copper) with drop shadow.
  - Arc path: a light dashed arc showing the range of motion.
  - Angle θ: curved arc from vertical equilibrium to current rod position, label "θ".
  - Restoring force: orange arrow tangent to arc pointing toward equilibrium, label "F = -mg sinθ".
  - Equilibrium line: vertical dashed line from pivot to rest position.
  - Animated: bob swings smoothly left-right; angle arc updates continuously.

■ BLOCKS / RAMPS / INCLINED PLANES:
  - Incline: a right-triangle ramp with gradient fill (light grey #f1f5f9 → #cbd5e1).
    Label the angle θ at the base.
  - Block: a gradient-filled rectangle on the incline with visible corners and drop shadow.
  - Forces (all labeled, all with arrowhead markers):
    • Weight W = mg: vertical arrow pointing straight down from block centre.
    • Normal N: perpendicular to incline surface, pointing away from surface.
    • Friction f: along incline, opposing motion.
    • Applied force F (if any): along incline, in direction of motion.
  - Motion arrow: a large velocity arrow v above the block.
  - Coordinate axes: x-axis along incline, y-axis perpendicular.

■ ELECTRIC CIRCUITS:
  - Wires: clean horizontal and vertical paths (stroke #1e293b, stroke-width=2).
  - Resistor: standard IEC symbol (rectangle) or zigzag (ANSI), colour-coded.
  - Capacitor: two parallel lines.
  - Inductor: a series of semicircular arcs.
  - Battery/Source: long-short line pair with ± labels.
  - Voltage labels: small pill badges at each node.
  - Current arrows: small filled arrowheads along wire paths with label "I".
  - Animated: current dots moving along the wire path.

■ FLUID MECHANICS:
  - Pipe / duct: two parallel lines (rectangular cross-section) with gradient interior.
  - Fluid: semi-transparent fill inside the pipe.
  - Streamlines: smooth curved paths with arrowheads, coloured by velocity magnitude.
  - Pressure labels: arrows pointing radially inward/outward at a cross-section.
  - Velocity profile: a set of horizontal arrows of varying length showing the velocity gradient.

ALWAYS:
  - Light background (#f8fafc, #eef5ff, or #f0f6ff) — absolutely mandatory.
  - All objects: real physical form with gradients + drop shadows.
  - All labels: specific (real name + symbol + value + unit), never generic.
  - All arrows: arrowhead markers, correct direction, vivid colour, legible label.
  - All text: dark (#1e293b) on light background — readable at 12px minimum.

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
    prompt = (
        "You are generating a PREMIUM, PHOTOREALISTIC 6-step SVG animation for a student "
        "physics/engineering/math question. QUALITY IS PARAMOUNT - this must look like a "
        "professional science textbook or award-winning museum exhibit.\n\n"
        "MANDATORY QUALITY REQUIREMENTS for Steps 1-6:\n"
        "  * STEP 1 (Environment): Draw a RICH, CONTEXTUAL BACKGROUND - laboratory bench, "
        "launch pad, mechanical workshop, physics lab, or real-world setting. Include "
        "coordinate axes with arrowheads, a scale bar, gravity arrow (if applicable), "
        "ground hatching with shadow, atmospheric gradient. NEVER just a plain grid.\n"
        "  * STEP 2 (Main Object): Draw the ACTUAL PHYSICAL OBJECT in full detail - not a "
        "generic box or circle. Use 3-layer gradient shading (highlight to base to shadow). "
        "Add specular highlights (small white ovals at 30% opacity) for 3D solidity. "
        "Add material texture (metal lines, fluid waviness, rubber texture, etc.).\n"
        "  * STEPS 3-5 (Quantities): Every force/velocity/field must be a PROFESSIONAL VECTOR ARROW "
        "(thick shaft stroke-width >= 4, filled arrowhead marker, white label background rect). "
        "Every dimension line must be a double-headed arrow with tick marks. Labels must show: "
        "symbol (italic serif) + = + exact value + unit.\n"
        "  * STEP 6 (Summary): Draw a textbook-style data table listing ALL given values. "
        "Highlight the unknown with a glowing box (feGaussianBlur). Add a FIND arrow.\n"
        "  * ANIMATIONS: Use real physics kinematics - parabolic arcs, sinusoidal oscillation, "
        "smooth slider-crank motion, orbital sin/cos, rocket rising with exhaust. "
        "ALL motion must be smooth (1000-1500ms transitions with spring easing). "
        "NEVER leave dynamic problems with empty raf_js.\n"
        "  * TYPOGRAPHY: ALL formula symbols in Times New Roman serif italic. "
        "All text dark (#0f172a) on white/light backgrounds. Never colour the text itself.\n"
        "  * LAYOUT: Use viewBox=0 0 850 478. Keep all content inside x=30..820, y=30..448. "
        "Centre the main object. NO overlapping labels - use leader lines when needed.\n"
        "  * LIGHT BACKGROUND ALWAYS: #f8fafc, #eef5ff, or #f0f6ff. NEVER dark SVG backgrounds.\n\n"
        "Follow the scene script and solution EXACTLY - match every object name, symbol, and value.\n"
        "Return ONLY valid JSON (no markdown, no fences).\n\n"
        "Question:\n" + question
        + "\n\nScene Script (follow this exactly for step titles, descriptions, badges, and layer structure):\n"
        + scene_str
        + "\n\nVerified Solution (use these exact values for all labels and annotations):\n"
        + sol_str
        + "\n\nGenerate the complete 6-step SVG concept animation JSON now."
    )

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
    # (layer_new ids collected but not needed after rebuild; omitted to silence linter)
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
    """Build Scene 7 (Formula Grid) HTML -- lesson-based design matching reference HTML."""
    lesson_tasks = sol.get('lesson_tasks', [])
    if not lesson_tasks:
        lesson_tasks = [{
            'id': 'answer',
            'name': sol.get('formula_name', 'Governing Formula'),
            'symbol': sol.get('answer_value', '?'),
            'formula': _clean_latex(str(sol.get('formula', 'Governing formula'))),
            'why': 'Find the unknown quantity using the governing formula.',
            'meaning': 'Apply the formula with the given values.',
            'given': '\n'.join(sol.get('given_list', [])),
            'work': [{'label': ap.get('label', ''), 'eq': ap.get('eq', '')} for ap in sol.get('approach_steps', [])],
            'value': str(sol.get('answer_value', '?')),
            'unit': str(sol.get('answer_unit', '')),
            'note': sol.get('key_insight', '')[:80],
        }]

    formula_cards = ''
    for task in lesson_tasks:
        name    = _he(str(task.get('name', 'Formula')))
        formula = _he(_clean_latex(str(task.get('formula', ''))))
        why     = _he(_clean_latex(str(task.get('why', ''))))
        meaning = _he(_clean_latex(str(task.get('meaning', ''))))
        formula_cards += (
            '<div class="lesson-formula">\n'
            '  <div class="s6-formula-name">' + name + '</div>\n'
            '  <div class="lesson-formula-eq">' + formula + '</div>\n'
            '  <div class="lesson-why">&#x1F4CC; ' + why + '</div>\n'
            '  <div class="lesson-key">&#x1F511; ' + meaning + '</div>\n'
            '</div>\n'
        )

    basis_note = _he(_clean_latex(str(sol.get('key_insight', ''))))

    return (
        '<div id="qanim-scene6-overlay" role="dialog" aria-modal="true" aria-labelledby="s6-card-title">\n'
        ' <div class="s6-card">\n'
        '  <div class="s6-title-bar"><h2 id="s6-card-title">Step 7 &mdash; Formulas to use</h2></div>\n'
        '  <div class="s6-body">\n'
        '   <div class="lesson-formula-grid" id="s7-formulas">\n'
        + formula_cards +
        '   </div>\n'
        '   <p id="lesson-basis" style="font-size:12px;line-height:1.5;color:#92400e;margin-top:16px">\n'
        '     &#x1F4A1; ' + basis_note + '\n'
        '   </p>\n'
        '  </div>\n'
        '  <div class="s6-nav-row">\n'
        '   <button class="btn-secondary" onclick="qanim_goToPrevScene()">&#x2190; Step 6</button>\n'
        '   <button class="btn-primary" onclick="qanim_showScene7()">Step 8: Solve &#x25B6;</button>\n'
        '  </div>\n'
        ' </div>\n'
        '</div>\n'
    )


def _build_scene7_html(sol: dict, scene: dict) -> str:
    """Build Scene 8 (Interactive Tabbed Solver) HTML -- lesson-based design matching reference HTML."""
    lesson_tasks = sol.get('lesson_tasks', [])
    if not lesson_tasks:
        lesson_tasks = [{
            'id': 'answer',
            'name': sol.get('formula_name', 'Solve for Answer'),
            'symbol': sol.get('answer_value', '?'),
            'formula': _clean_latex(str(sol.get('formula', 'Governing formula'))),
            'why': 'Find the unknown quantity.',
            'meaning': 'Apply formula with given values.',
            'given': '\n'.join(sol.get('given_list', [])),
            'work': [{'label': ap.get('label', ''), 'eq': ap.get('eq', '')} for ap in sol.get('approach_steps', [])],
            'value': str(sol.get('answer_value', '?')),
            'unit': str(sol.get('answer_unit', '')),
            'note': sol.get('key_insight', '')[:80],
        }]

    task_tabs = ''
    for i, task in enumerate(lesson_tasks):
        name = _he(str(task.get('name', f'Answer {i+1}')))
        active_cls = ' is-current' if i == 0 else ''
        tid = _he(str(task.get('id', f't{i}')))
        task_tabs += (
            '<button class="lesson-target' + active_cls + '"'
            ' data-task-idx="' + str(i) + '" id="s8-tab-' + tid + '"'
            ' onclick="qanimS8SwitchTask(' + str(i) + ')">' + name + '</button>\n'
        )

    tasks_json = json.dumps(lesson_tasks, ensure_ascii=False)
    first_title = _he(str(lesson_tasks[0].get('name', '') if lesson_tasks else ''))
    n_tasks = len(lesson_tasks)

    return (
        '<div id="qanim-scene7-overlay" role="dialog" aria-modal="true" aria-labelledby="s8-card-title">\n'
        ' <div class="s7-card">\n'
        '  <div class="s7-title-bar"><h2 id="s8-card-title">Step 8 &mdash; Solve each answer</h2></div>\n'
        '  <nav class="lesson-targets" id="s8-task-nav" aria-label="Answers to find">\n'
        + task_tabs +
        '  </nav>\n'
        '  <div class="s7-body-cols">\n'
        '   <div class="s7-left-col">\n'
        '    <div class="s7-system-label" id="s8-progress">Answer 1 of ' + str(n_tasks) + '</div>\n'
        '    <div class="s7-system-visual"><div class="s7-system-visual-title" id="s8-task-title">' + first_title + '</div></div>\n'
        '    <div class="s7-given-section-title">Use these values</div>\n'
        '    <div class="s7-given-list" id="s8-task-given" style="font-size:14px;line-height:1.9;color:#334155"></div>\n'
        '    <div class="s7-formula-result-bar"><div class="s7-formula-result-text" id="s8-task-formula"></div></div>\n'
        '   </div>\n'
        '   <div class="s7-right-col" aria-live="polite">\n'
        '    <div class="s7-approach-list" id="s8-task-work"></div>\n'
        '    <div class="s7-formula-result-bar"><div class="s7-formula-result-text" id="s8-task-result"></div></div>\n'
        '   </div>\n'
        '  </div>\n'
        '  <div class="s7-nav-row">\n'
        '   <button class="btn-secondary" id="s8-back" onclick="qanimPreviousCondition()">&#x2190; Step 7</button>\n'
        '   <button class="btn-primary" id="s8-next" onclick="qanimNextCondition()">Next answer &#x25B6;</button>\n'
        '  </div>\n'
        ' </div>\n'
        '</div>\n'
        '<script id="qanim-lesson-tasks-data" type="application/json">' + tasks_json + '</script>\n'
    )


def _build_scene9_html(sol: dict, to_find: list) -> str:
    """Build Scene 9 (Final Answer Grid) HTML -- lesson-based design matching reference HTML."""
    lesson_tasks = sol.get('lesson_tasks', [])
    if not lesson_tasks:
        lesson_tasks = [{
            'id': 'answer',
            'name': to_find[0] if to_find else 'Final Answer',
            'symbol': sol.get('answer_value', '?'),
            'formula': _clean_latex(str(sol.get('formula', ''))),
            'value': str(sol.get('answer_value', '?')),
            'unit': str(sol.get('answer_unit', '')),
            'note': sol.get('key_insight', '')[:80],
        }]

    answer_cards = ''
    for task in lesson_tasks:
        name  = _he(str(task.get('name', 'Answer')))
        value = _he(str(task.get('value', '?')))
        unit  = _he(str(task.get('unit', '')))
        note  = _he(_clean_latex(str(task.get('note', ''))))
        answer_cards += (
            '<div class="lesson-answer">\n'
            '  <h3>' + name + '</h3>\n'
            '  <div class="lesson-answer-number">' + value + '<span>' + unit + '</span></div>\n'
            '  <p>' + note + '</p>\n'
            '</div>\n'
        )

    return (
        '<div id="qanim-scene9-overlay" role="dialog" aria-modal="true" aria-labelledby="s9-card-title">\n'
        ' <div class="s9-card">\n'
        '  <div class="s9-title-bar"><h2 id="s9-card-title">Step 9 &mdash; Final answers</h2></div>\n'
        '  <div class="s9-body">\n'
        '   <div class="lesson-answer-grid" id="s9-answers">\n'
        + answer_cards +
        '   </div>\n'
        '  </div>\n'
        '  <div class="s9-nav-row">\n'
        '   <button class="btn-secondary" onclick="qanim_goToScene7FromScene9()">&#x2190; Step 8</button>\n'
        '   <button class="btn-primary" onclick="resetAnim()">&#x21BA; Restart</button>\n'
        '  </div>\n'
        ' </div>\n'
        '</div>\n'
    )


def _selfcheck_customize_js(customize_html: str) -> None:
    """Guard rail against the class of bug that has twice silently disabled
    the Customize button: a malformed backslash escape inside a template
    string throwing a JS SyntaxError that kills the whole <script> block,
    with no visible error anywhere in the app UI — the button just does
    nothing. This extracts every <script> block from the generated
    Customize panel HTML and parses it with a real JS engine (Node) before
    the build is allowed to proceed. If Node finds a syntax error, this
    raises loudly so a broken build can never silently ship again — no
    matter who edits _build_customize_html next, human or AI agent.
    If Node isn't available in this environment, this degrades to a
    warning rather than a hard failure, since Node is not otherwise a
    dependency of this pipeline.
    """
    import subprocess
    import tempfile

    scripts = re.findall(r"<script[^>]*>(.*?)</script>", customize_html, re.S)
    if not scripts:
        return
    combined = "\n\n".join(scripts)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(combined)
            tmp_path = tf.name
        result = subprocess.run(
            ["node", "--check", tmp_path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print(
                "[QAnim]  X [customize] Generated Customize panel JS failed a syntax "
                f"check — the Customize button would silently stop working. Details:\n"
                f"{result.stderr}"
            )
            raise RuntimeError(
                "Customize panel JS failed `node --check` — refusing to ship a build "
                "with a broken Customize button. See the [QAnim] X [customize] log "
                "line above for the exact syntax error and line number."
            )
        print("[QAnim] OK [customize] Customize panel JS passed syntax check")
    except FileNotFoundError:
        print(
            "[QAnim]  ! [customize] Node.js not found in this environment — skipped "
            "the Customize panel JS syntax self-check (best-effort only, not a hard "
            "dependency of this pipeline)"
        )
    except subprocess.TimeoutExpired:
        print("[QAnim]  ! [customize] Customize panel JS syntax check timed out — skipped")
    finally:
        if tmp_path:
            try:
                _os.remove(tmp_path)
            except OSError:
                pass


def _build_customize_html(sol: dict, scene: dict) -> str:
    """Build the Customize panel HTML + JS for live value editing.

    Reads sol["customize"] produced by Gemini. Falls back gracefully to a
    synthesised version from sol["variables"] if the field is absent — so the
    Customize panel is always shown when there are known given values.
    Returns the combined CSS + panel HTML + JS string.
    """
    _CUSTOMIZE_CSS = """<style id="qanim-customize-styles">
/* ── Customize Panel Overlay ────────────────────────────────────── */
#customize-backdrop {
  display: none; position: fixed; inset: 0;
  background: rgba(15,23,42,0.55); z-index: 9998;
  backdrop-filter: blur(2px);
}
#customize-backdrop.open { display: block; }

#customize-panel {
  display: none; position: fixed; top: 50%; right: 0;
  transform: translateY(-50%);
  width: min(420px, 96vw); max-height: 88vh;
  background: #fff; border-radius: 16px 0 0 16px;
  box-shadow: -8px 0 40px rgba(0,0,0,0.18);
  z-index: 9999; flex-direction: column; overflow: hidden;
  font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
}
#customize-panel.open { display: flex; }

.cust-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px 12px;
  background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
  color: #fff; flex-shrink: 0;
}
.cust-header-title {
  font-size: 15px; font-weight: 700; display: flex; align-items: center; gap: 8px;
}
.cust-header-badge {
  font-size: 10px; font-weight: 600; background: rgba(255,255,255,0.22);
  border-radius: 20px; padding: 2px 8px; letter-spacing: 0.3px;
}
.cust-close-btn {
  background: rgba(255,255,255,0.18); border: none; color: #fff;
  width: 28px; height: 28px; border-radius: 50%; cursor: pointer;
  font-size: 14px; display: flex; align-items: center; justify-content: center;
  transition: background 0.2s;
}
.cust-close-btn:hover { background: rgba(255,255,255,0.35); }

.cust-body {
  flex: 1; overflow-y: auto; padding: 16px 18px;
  display: flex; flex-direction: column; gap: 14px;
}
.cust-section-title {
  font-size: 11px; font-weight: 700; color: #64748b;
  text-transform: uppercase; letter-spacing: 0.7px; margin-bottom: 4px;
}
.cust-field-grid { display: flex; flex-direction: column; gap: 10px; }
.cust-field {
  display: grid; grid-template-columns: 1fr auto; align-items: center;
  gap: 6px 10px; background: #f8fafc; border: 1px solid #e2e8f0;
  border-radius: 10px; padding: 10px 12px;
}
.cust-field label {
  font-size: 13px; color: #334155; font-weight: 500;
  grid-column: 1 / -1;
}
.cust-sym {
  font-family: 'Georgia', serif; font-style: italic;
  color: #6366f1; font-weight: 700; margin-right: 4px;
}
.cust-field input[type="number"] {
  width: 90px; padding: 5px 8px; border: 1.5px solid #c7d2fe;
  border-radius: 7px; font-size: 13px; color: #1e293b;
  background: #fff; outline: none; transition: border-color 0.2s;
  text-align: right;
}
.cust-field input[type="number"]:focus { border-color: #6366f1; }
.cust-unit { font-size: 11px; color: #94a3b8; }

.cust-preview-box {
  background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 10px;
  padding: 10px 12px;
}
.cust-preview-title {
  font-size: 11.5px; font-weight: 700; color: #0284c7; margin-bottom: 8px;
}
.cust-preview-grid { display: flex; flex-wrap: wrap; gap: 6px; }
.cust-preview-item {
  display: flex; align-items: center; gap: 4px;
  background: #fff; border: 1px solid #e0f2fe; border-radius: 20px;
  padding: 3px 10px; font-size: 12px;
}
.cpv-sym { font-family: 'Georgia', serif; font-style: italic; color: #6366f1; font-weight: 700; }
.cpv-arrow { color: #94a3b8; }

.cust-result-bar {
  display: none; background: #f0fdf4; border: 1px solid #bbf7d0;
  border-radius: 8px; padding: 8px 12px; font-size: 12.5px; color: #166534;
}
.cust-result-bar.visible { display: block; }
.cust-result-title { font-weight: 700; margin-bottom: 2px; }

.cust-error-bar {
  display: none; background: #fef2f2; border: 1px solid #fecaca;
  border-radius: 8px; padding: 8px 12px; font-size: 12.5px; color: #b91c1c;
}
.cust-error-bar.visible { display: block; }

.cust-footer {
  display: flex; gap: 10px; padding: 12px 18px;
  border-top: 1px solid #f1f5f9; flex-shrink: 0; background: #fafafa;
}
.cust-btn-reset, .cust-btn-apply {
  flex: 1; padding: 9px 0; border-radius: 9px; font-size: 13px;
  font-weight: 600; cursor: pointer; border: none; transition: all 0.2s;
}
.cust-btn-reset {
  background: #f1f5f9; color: #475569;
}
.cust-btn-reset:hover { background: #e2e8f0; }
.cust-btn-apply {
  background: linear-gradient(135deg, #6366f1, #8b5cf6);
  color: #fff; box-shadow: 0 2px 8px rgba(99,102,241,0.3);
}
.cust-btn-apply:hover { opacity: 0.9; transform: translateY(-1px); }

/* Pulse ring on the Customize button after applying */
@keyframes custAppliedRing {
  0%   { box-shadow: 0 0 0 0 rgba(99,102,241,0.6); }
  70%  { box-shadow: 0 0 0 10px rgba(99,102,241,0); }
  100% { box-shadow: 0 0 0 0 rgba(99,102,241,0); }
}
.cust-applied-ring { animation: custAppliedRing 0.8s ease-out; }
</style>
"""

    import re as _re_cust

    # Robust parsing of customize block (Gemini sometimes returns a string, list, or omits it)
    cust_raw = sol.get("customize")
    if isinstance(cust_raw, str):
        try:
            import json as _json
            cust = _json.loads(cust_raw)
        except Exception:
            cust = {}
    elif isinstance(cust_raw, (dict, list)):
        cust = cust_raw
    else:
        cust = {}

    if isinstance(cust, list):
        fields = cust
        compute_js_body = ""
        question_template = ""
    else:
        fields = cust.get("fields") or []
        if isinstance(fields, dict):
            fields = [fields] # Just in case it returns a single field object
        elif not isinstance(fields, list):
            fields = []
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
        variables_raw = sol.get("variables") or []
        variables = []
        if isinstance(variables_raw, list):
            variables = variables_raw
        elif isinstance(variables_raw, dict):
            # Sometimes Gemini returns {"m": "10", "v": "5"}
            for k, v in variables_raw.items():
                if isinstance(v, dict):
                    variables.append(v)
                else:
                    variables.append({"symbol": k, "value": str(v), "color": "blue"})

        for v in variables:
            if not isinstance(v, dict):
                continue
            color = str(v.get("color", "blue")).lower()
            val_str = str(v.get("value") or v.get("val") or "")
            # Skip the unknown/answer variable
            if color in ("green",) or "?" in val_str or "to find" in val_str.lower() or "unknown" in val_str.lower():
                continue
            raw_id = str(v.get("symbol") or v.get("sym") or "v")
            # make a safe JS identifier
            safe_id = _re_cust.sub(r'[^a-zA-Z0-9_]', '_', raw_id).strip('_') or "v"
            try:
                # Handle things like "10 kg", extract just the number
                import re as _num_re
                num_match = _num_re.search(r'[\d.eE+\-]+', val_str.replace("?", ""))
                if num_match:
                    default_val = float(num_match.group(0))
                else:
                    default_val = 1.0
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
                " if(isNaN(ans)) { ans = '" + str(sol.get("answer_value", "?")).replace("'", "") + "'; }"
                " return { answer: (typeof ans === 'number' ? _fmt(ans) : ans), answer_unit: '" +
                str(sol.get("answer_unit", "")).replace("'", "") + "',"
                " answer_label: '" + str(sol.get("formula", "Answer")).replace("'", "")[:40] + "',"
                " derived: {} };"
            )
        if fields and not question_template:
            question_template = str(sol.get("formula", "")) or ""

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

    # ── Customize panel root-cause fix: sanitize LaTeX in compute_js ─────────
    # Root cause: Gemini sometimes emits compute_js containing LaTeX-style math
    # (e.g. `\frac{m_0 \cdot g}{\alpha}`) which is valid LaTeX but breaks JS
    # with a SyntaxError on the backslash character.  The panel script is then
    # never executed, so the Customize panel opens but the Apply button does
    # nothing (or the script block itself fails to load, leaving a blank panel).
    # Fix: apply a JS-safe LaTeX scrubber that converts common patterns to
    # plain arithmetic JS so the compute function always runs correctly.
    def _clean_js_latex(js: str) -> str:
        import re as _re_jl
        # Replace \\frac{num}{den} → (num)/(den) (handles double-escaped JSON)
        for _ in range(4):
            js = _re_jl.sub(
                r'\\\\?frac\{([^{}]*)\}\{([^{}]*)\}',
                lambda m: '(' + m.group(1) + ')/(' + m.group(2) + ')',
                js
            )
        # Greek letter commands → JS variable names (commonly used in physics)
        JS_GREEK = {
            r'\alpha': 'alpha', r'\\alpha': 'alpha',
            r'\beta': 'beta',   r'\\beta': 'beta',
            r'\gamma': 'gamma', r'\\gamma': 'gamma',
            r'\omega': 'omega', r'\\omega': 'omega',
            r'\Omega': 'Omega', r'\\Omega': 'Omega',
            r'\mu': 'mu',       r'\\mu': 'mu',
            r'\rho': 'rho',     r'\\rho': 'rho',
            r'\lambda': 'lambda_', r'\\lambda': 'lambda_',
            r'\sigma': 'sigma', r'\\sigma': 'sigma',
            r'\theta': 'theta', r'\\theta': 'theta',
            r'\phi': 'phi',     r'\\phi': 'phi',
            r'\pi': 'Math.PI',  r'\\pi': 'Math.PI',
            r'\cdot': '*',      r'\\cdot': '*',
            r'\times': '*',     r'\\times': '*',
            r'\sqrt': 'Math.sqrt', r'\\sqrt': 'Math.sqrt',
            r'\infty': 'Infinity', r'\\infty': 'Infinity',
        }
        for latex_cmd, js_equiv in JS_GREEK.items():
            js = js.replace(latex_cmd + '{', js_equiv + '(').replace(latex_cmd + ' ', js_equiv + ' ').replace(latex_cmd, js_equiv)
        # Strip remaining \command patterns (backslash + letters)
        js = _re_jl.sub(r'\\\\?[a-zA-Z]+', '', js)
        # Strip stray { } that came from \frac{} leftovers (but NOT JS object braces)
        # Only strip { } that appear INSIDE string literals (between quotes)
        # Safe approach: just remove lone { or } that are NOT part of valid JS syntax
        return js

    if compute_js_body:
        compute_js_body = _clean_js_latex(compute_js_body)

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
            # Match: "symbol = value unit"  or "symbol: value unit (extra info)"
            import re as _re_gl
            m_gl = _re_gl.match(
                r'^([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*([\d.eE+\-]+)\s*(.*)$',
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
            unit_gl = m_gl.group(3).strip()
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
                " if(isNaN(ans)) { ans = '" + str(sol.get('answer_value', '?')).replace("'", '') + "'; }"
                " return { answer: (typeof ans === 'number' ? _fmt(ans) : ans), answer_unit: '"
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
<script id="qanim-js-customize-nf">
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
  // Bug 3 fix (v15): every previous version of _escRe (split/join loop,
  // then a hand-escaped /regex/ literal) needed literal backslash
  // characters typed inside this Python triple-quoted template. Those
  // pass through Python's own string-escape parsing before landing in
  // the HTML, so the correct backslash COUNT depends on exactly how many
  // are typed in the Python source -- get it wrong by even one and the
  // emitted JS string/regex literal never terminates, which throws a
  // SyntaxError that silently kills this entire <script> block (and with
  // it the onReady() handler at the bottom that wires up the Customize
  // button). That happened twice with two different backslash counts.
  // Fix: build the backslash character at JS RUNTIME instead of typing
  // it in the source, so there is no backslash for Python's template
  // parsing to mis-transcribe -- this bug class cannot recur here.
  function _escRe(s){
    var BS = String.fromCharCode(92);
    var specials = ['.','*','+','?','^','$','{','}','(',')','|','[',']',BS];
    var re = new RegExp('[' + specials.map(function(c){ return BS + c; }).join('') + ']', 'g');
    return String(s).replace(re, function(m){ return BS + m; });
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
<script id="qanim-js-customize-fb">
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
        dots += '<div class="step-dot" id="dot-step7" onclick="if(typeof window.qanim_showScene6===\'function\')window.qanim_showScene6()">7 · Formula</div>\n<div class="step-connector"></div>\n'
        dots += '<div class="step-dot" id="dot-step8" onclick="if(typeof window.qanim_showScene7===\'function\')window.qanim_showScene7()">8 · Subst.</div>\n<div class="step-connector"></div>\n'
        dots += '<div class="step-dot" id="dot-step9" onclick="if(typeof window.qanim_showScene9===\'function\')window.qanim_showScene9()">9 · Answer</div>\n'

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
    # badge count available via len(glossary) at call site if needed
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
    # final_answer scrubbing reserved for future use (answer_value covers current cases)

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


# ===========================================================================
# Module-level CSS / JS constants used by assemble_html()
# ===========================================================================

_BASE_CSS = """
/* ── QAnim Base Styles ─────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
  background: #0f172a;
  color: #e2e8f0;
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  overflow-x: hidden;
}
/* Question banner */
.q-banner {
  background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
  border-bottom: 1px solid #1e3a5f;
  padding: 14px 24px;
  flex-shrink: 0;
}
.q-label {
  font-size: 10px; font-weight: 700; letter-spacing: 1px;
  text-transform: uppercase; color: #38bdf8; margin-bottom: 4px;
}
.q-text {
  font-size: 15px; color: #f1f5f9; line-height: 1.5; font-weight: 500;
}
/* Main layout */
.main-layout {
  display: flex; flex: 1; overflow: hidden;
}
.left-panel {
  width: 220px; flex-shrink: 0;
  background: #1e293b; border-right: 1px solid #334155;
  display: flex; flex-direction: column; padding: 16px 12px; gap: 6px;
  overflow-y: auto;
}
.step-dot {
  padding: 8px 10px; border-radius: 8px; font-size: 12px; font-weight: 600;
  color: #94a3b8; cursor: pointer; transition: all 0.2s;
  border-left: 3px solid transparent;
  line-height: 1.3;
}
.step-dot:hover { background: #1e3a5f; color: #e2e8f0; }
.step-dot.active { background: #1e3a5f; color: #38bdf8; }
.step-connector {
  width: 2px; height: 10px; background: #334155;
  margin-left: 18px;
}
.step-legend {
  margin-top: 12px; padding-top: 12px; border-top: 1px solid #334155;
}
.step-legend-item {
  display: flex; align-items: center; gap: 8px;
  font-size: 11px; color: #64748b; margin-bottom: 4px;
}
.step-legend-dot {
  width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
}
.right-panel {
  flex: 1; display: flex; flex-direction: column; overflow: hidden;
}
.svg-container {
  flex: 1; display: flex; align-items: center; justify-content: center;
  background: #0f172a; overflow: hidden; position: relative;
  transition: opacity 0.3s;
}
.svg-container svg {
  max-width: 100%; max-height: 100%;
  width: 100%; height: auto;
}
/* Info + actions bar */
.info-actions-bar {
  background: #1e293b; border-top: 1px solid #334155;
  padding: 12px 20px; flex-shrink: 0;
}
.step-header {
  display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px;
}
.step-num {
  font-size: 11px; font-weight: 700; color: #38bdf8;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.step-label {
  font-size: 13px; font-weight: 700; color: #f1f5f9;
}
.badges {
  display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px;
}
.badge {
  padding: 3px 10px; border-radius: 20px; font-size: 11.5px; font-weight: 600;
}
.info-desc {
  font-size: 12.5px; color: #94a3b8; line-height: 1.55;
}
.actions {
  display: flex; gap: 10px; margin-top: 10px; flex-wrap: wrap;
}
.btn-primary, .btn-secondary {
  padding: 8px 18px; border-radius: 8px; font-size: 13px;
  font-weight: 600; cursor: pointer; border: none; transition: all 0.2s;
}
.btn-primary {
  background: linear-gradient(135deg, #0ea5e9, #6366f1);
  color: #fff; box-shadow: 0 2px 8px rgba(14,165,233,0.3);
}
.btn-primary:hover { opacity: 0.88; transform: translateY(-1px); }
.btn-secondary {
  background: #334155; color: #cbd5e1;
}
.btn-secondary:hover { background: #475569; }
.btn-secondary:disabled { opacity: 0.4; cursor: not-allowed; }
/* Scene modal backdrop */
#qanim-scene-modal-backdrop {
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,0.7); z-index: 999;
}
#qanim-scene-modal-backdrop.qanim-scene-visible { display: block; }
/* S6 To-Find badge */
#step6-info-panel {
  position: absolute; top: 16px; right: 16px; z-index: 10;
  pointer-events: none;
}
.s6tofind-badge {
  background: rgba(15,23,42,0.88); border: 1px solid #334155;
  border-radius: 12px; padding: 10px 14px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.4);
  backdrop-filter: blur(4px);
  display: flex; flex-direction: column; gap: 6px;
}
.s6tofind-heading {
  font-size: 11px; font-weight: 700; color: #38bdf8;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.s6tofind-chip {
  display: flex; align-items: center; gap: 6px;
  font-size: 13px; color: #f1f5f9; font-weight: 600;
}
.s6tofind-icon { font-size: 14px; }
.s6tofind-hint {
  font-size: 11px; color: #64748b; margin-top: 4px;
}
/* Answer box backdrop */
#answerbox-backdrop {
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,0.65); z-index: 9990; backdrop-filter: blur(2px);
}
#answerbox-backdrop.ab-open { display: block; }
/* Answer box panel */
#answerbox-panel {
  position: fixed; top: 50%; left: 50%; transform: translate(-50%,-50%);
  width: min(480px, 94vw); background: #1e293b;
  border: 1px solid #334155; border-radius: 16px;
  box-shadow: 0 8px 40px rgba(0,0,0,0.5);
  z-index: 9991; display: none; flex-direction: column;
  font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
  overflow: hidden;
}
#answerbox-panel.ab-open { display: flex; }
.ab-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px;
  background: linear-gradient(135deg, #0ea5e9 0%, #6366f1 100%);
  color: #fff; flex-shrink: 0;
}
.ab-header-title { font-size: 15px; font-weight: 700; }
.ab-close-btn {
  background: rgba(255,255,255,0.18); border: none; color: #fff;
  width: 28px; height: 28px; border-radius: 50%; cursor: pointer;
  font-size: 14px; display: flex; align-items: center; justify-content: center;
  transition: background 0.2s;
}
.ab-close-btn:hover { background: rgba(255,255,255,0.35); }
.ab-progress-row {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 18px; border-bottom: 1px solid #334155; flex-shrink: 0;
}
.ab-progress-label { font-size: 12px; color: #64748b; }
.ab-progress-dots { display: flex; gap: 6px; }
.ab-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: #334155; transition: background 0.2s;
}
.ab-dot.ab-dot-active { background: #0ea5e9; }
.ab-dot.ab-dot-correct { background: #22c55e; }
.ab-dot.ab-dot-wrong { background: #ef4444; }
.ab-body { padding: 16px 18px; display: flex; flex-direction: column; gap: 12px; }
.ab-find-chip {
  display: flex; align-items: center; gap: 10px;
  background: #0f172a; border: 1px solid #334155; border-radius: 10px;
  padding: 10px 14px;
}
.ab-find-icon { font-size: 20px; }
.ab-find-label { font-size: 10px; font-weight: 700; color: #38bdf8; text-transform: uppercase; }
.ab-find-text { font-size: 13px; color: #f1f5f9; font-weight: 600; margin-top: 2px; }
.ab-instruction { font-size: 12.5px; color: #94a3b8; line-height: 1.5; }
#ab-user-input {
  width: 100%; min-height: 64px; padding: 10px 12px;
  background: #0f172a; border: 1.5px solid #334155; border-radius: 8px;
  color: #f1f5f9; font-size: 14px; font-family: inherit; resize: vertical;
  outline: none; transition: border-color 0.2s;
}
#ab-user-input:focus { border-color: #38bdf8; }
.ab-feedback {
  display: none; padding: 10px 14px; border-radius: 8px;
  font-size: 13px; font-weight: 600;
}
.ab-feedback.ab-correct {
  display: block; background: #052e16; color: #4ade80;
  border: 1px solid #166534;
}
.ab-feedback.ab-wrong {
  display: block; background: #450a0a; color: #f87171;
  border: 1px solid #991b1b;
}
.ab-reveal { display: none; }
.ab-reveal.ab-visible { display: block; font-size: 12.5px; color: #94a3b8; line-height: 1.5; }
.ab-footer {
  display: flex; gap: 10px; padding: 12px 18px;
  border-top: 1px solid #334155; flex-shrink: 0; flex-wrap: wrap;
}
.ab-btn {
  flex: 1; padding: 9px 0; border-radius: 8px; font-size: 13px;
  font-weight: 600; cursor: pointer; border: none; transition: all 0.2s;
}
.ab-btn-submit { background: linear-gradient(135deg,#0ea5e9,#6366f1); color:#fff; }
.ab-btn-submit:hover { opacity: 0.88; transform: translateY(-1px); }
.ab-btn-next { background: #334155; color: #cbd5e1; }
.ab-btn-next:hover { background: #475569; }
/* Fullscreen button */
#qanim-fullscreen-btn {
  position: fixed; top: 12px; right: 12px; z-index: 9980;
  background: rgba(30,41,59,0.85); border: 1px solid #334155;
  color: #94a3b8; border-radius: 8px; padding: 6px 10px;
  cursor: pointer; font-size: 14px; transition: all 0.2s;
  backdrop-filter: blur(4px);
}
#qanim-fullscreen-btn:hover { background: #1e3a5f; color: #38bdf8; }
"""

_SCENE6_CSS = """

/* ── Scene 6/7/9: shared light-theme variables ──────────────────────────────────────── */
:root {
  --panel-bg: #fff;
  --border: #e8eef8;
  --text-main: #0f172a;
  --ease-spring: cubic-bezier(.34,1.56,.64,1);
}

/* ── Scene 6: Formulas overlay ───────────────────────────────────────────────────────── */
#qanim-scene6-overlay {
  display: none; position: fixed; top: 50%; left: 50%;
  transform: translate(-50%,-50%) scale(.95);
  z-index: 7500; width: min(900px,96vw); max-height: 92vh;
  overflow-y: auto; box-sizing: border-box;
  opacity: 0; pointer-events: none;
  transition: opacity .3s, transform .32s var(--ease-spring);
}
#qanim-scene6-overlay.qanim-scene-visible {
  display: block !important; opacity: 1; pointer-events: auto;
  transform: translate(-50%,-50%) scale(1);
}
.s6-card {
  background: var(--panel-bg); border-radius: 22px;
  box-shadow: 0 12px 60px rgba(37,99,235,.14), 0 2px 10px rgba(0,0,0,.08);
  border: 1px solid var(--border); overflow: hidden;
  font-family: 'Inter', -apple-system, sans-serif;
}
.s6-title-bar {
  text-align: center; padding: 22px 32px 18px;
  border-bottom: 1px solid var(--border); background: var(--panel-bg);
}
.s6-title-bar h2 {
  font-size: 20px; font-weight: 900; color: var(--text-main); letter-spacing: -.4px;
}
.s6-body { padding: 28px 32px 20px; background: #fff; }
.s6-formula-name {
  font-size: 10.5px; font-weight: 800; color: #1d4ed8;
  text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 8px;
}
.s6-nav-row {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  padding: 16px 32px 22px; border-top: 1px solid var(--border); background: #fff;
}
#qanim-scene6-overlay .lesson-formula-grid,
#qanim-scene9-overlay .lesson-answer-grid {
  display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 14px;
}
#qanim-scene6-overlay .lesson-formula {
  background: #fff; border: 1.5px solid #bfdbfe; border-radius: 14px; padding: 18px;
}
#qanim-scene6-overlay .lesson-formula-eq {
  font-size: 19px; font-weight: 800; color: #1d4ed8; line-height: 1.5; overflow-wrap: anywhere;
}
#qanim-scene6-overlay .lesson-why { font-size: 14px; color: #334155; line-height: 1.5; margin-top: 9px; }
#qanim-scene6-overlay .lesson-key { font-size: 12px; color: #64748b; line-height: 1.5; margin-top: 6px; }
@media(max-width:600px){
  #qanim-scene6-overlay .lesson-formula-grid,
  #qanim-scene9-overlay .lesson-answer-grid{grid-template-columns:1fr;}
  #qanim-scene6-overlay .s6-body{padding:20px;}
}
"""

_SCENE7_CSS = """

/* ── Scene 7/8: Interactive solver overlay ───────────────────────────────────────────── */
#qanim-scene7-overlay {
  display: none; position: fixed; top: 50%; left: 50%;
  transform: translate(-50%,-50%) scale(.95);
  z-index: 7500; width: min(900px,96vw); max-height: 92vh;
  overflow-y: auto; box-sizing: border-box;
  opacity: 0; pointer-events: none;
  transition: opacity .3s, transform .3s cubic-bezier(.34,1.56,.64,1);
}
#qanim-scene7-overlay.qanim-scene-visible {
  display: block !important; opacity: 1; pointer-events: auto;
  transform: translate(-50%,-50%) scale(1);
}
.s7-card {
  background: #fff; border-radius: 20px;
  box-shadow: 0 8px 48px rgba(37,99,235,.12), 0 2px 8px rgba(0,0,0,.07);
  border: 1px solid #e8eef8; overflow: hidden; font-family: -apple-system,'Segoe UI',Arial,sans-serif;
}
.s7-title-bar {
  text-align: center; padding: 20px 28px 16px;
  border-bottom: 1px solid #e8eef8; background: #fff;
}
.s7-title-bar h2 { font-size: 20px; font-weight: 900; color: #0f172a; letter-spacing: -.3px; }
.s7-body-cols { display: flex; align-items: flex-start; gap: 0; min-height: 320px; }
.s7-left-col {
  width: 44%; min-width: 200px; border-right: 1.5px solid #e8eef8;
  padding: 22px 20px 22px 26px;
  background: linear-gradient(180deg,#eff6ff 0%,#dbeafe 100%);
  display: flex; flex-direction: column; gap: 0; align-self: stretch;
}
.s7-system-label { font-size: 10.5px; font-weight: 800; color: #1d4ed8; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }
.s7-system-visual {
  background: linear-gradient(135deg,#bfdbfe 0%,#93c5fd 100%);
  border-radius: 12px; padding: 16px 14px 14px; margin-bottom: 16px; text-align: center;
}
.s7-system-visual-title { font-size: 13px; font-weight: 800; color: #1e3a5f; margin-bottom: 6px; }
.s7-given-section-title { font-size: 12.5px; font-weight: 900; color: #1d4ed8; margin-bottom: 9px; }
.s7-given-list { display: flex; flex-direction: column; gap: 5px; margin-bottom: 14px; }
.s7-given-item { font-size: 12.5px; color: #334155; line-height: 1.55; display: flex; align-items: flex-start; gap: 7px; padding: 5px 0; }
.s7-given-item::before { content: 'o'; color: #3b82f6; font-weight: 900; flex-shrink: 0; margin-top: 1px; }
.s7-given-item strong { font-weight: 700; color: #1e293b; font-family: 'Fira Code', monospace; }
.s7-right-col { flex: 1; padding: 22px 26px 20px 20px; display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.s7-approach-section-title { font-size: 12.5px; font-weight: 900; color: #7c3aed; margin-bottom: 9px; }
.s7-approach-list { display: flex; flex-direction: column; gap: 7px; margin-bottom: 14px; }
.s7-approach-step {
  display: flex; align-items: flex-start; gap: 10px;
  font-size: 12.5px; color: #1e293b; line-height: 1.55;
  background: #f8fafc; border: 1px solid #e8eef8;
  border-radius: 9px; padding: 10px 13px;
}
.s7-approach-step-num {
  font-weight: 900; color: #fff; flex-shrink: 0; min-width: 22px; height: 22px;
  background: linear-gradient(135deg,#7c3aed,#6d28d9);
  border-radius: 50%; display: flex; align-items: center; justify-content: center;
  font-size: 10.5px; box-shadow: 0 2px 6px rgba(124,58,237,.35); margin-top: 1px;
}
.s7-approach-step-eq {
  display: block; margin-top: 5px;
  font-family: 'Fira Code','Courier New',monospace;
  font-size: 12px; font-weight: 600; color: #dc2626;
  background: #fff7ed; border-radius: 6px; padding: 3px 9px;
  word-break: break-word; border: 1px solid #fed7aa;
}
.s7-formula-result-bar {
  background: linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);
  border: 2px solid #86efac; border-radius: 13px; padding: 13px 18px;
}
.s7-formula-result-text {
  font-family: 'Fira Code','Courier New',monospace;
  font-size: 14px; font-weight: 900; color: #15803d; line-height: 1.55; word-break: break-word;
}
.s7-formula-units { font-size: 11px; color: #166534; margin-top: 4px; font-style: italic; }
.s7-nav-row {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  padding: 18px 28px 24px; border-top: 1px solid #e8eef8; background: #fff;
}
#qanim-scene7-overlay .lesson-targets {
  display: flex; flex-wrap: wrap; gap: 7px;
  padding: 16px 26px; background: #fff; border-bottom: 1px solid #e8eef8;
}
#qanim-scene7-overlay .lesson-target {
  font-family: inherit; font-size: 12px; font-weight: 700;
  border: 1px solid #cbd5e1; border-radius: 20px;
  background: #f8fafc; color: #475569; padding: 7px 12px; cursor: pointer; transition: all .15s;
}
#qanim-scene7-overlay .lesson-target.is-current { color: #fff; background: #0891b2; border-color: #0891b2; }
#qanim-scene7-overlay .lesson-target:focus-visible { outline: 3px solid #38bdf8; outline-offset: 2px; }
#qanim-scene7-overlay .s7-right-col{min-width:0;}
@media(max-width:600px){
  .s7-body-cols{flex-direction:column;}
  .s7-left-col{width:100%;border-right:none;border-bottom:1.5px solid #e8eef8;min-width:0;}
  #qanim-scene7-overlay .s7-nav-row{flex-wrap:wrap;}
}
"""

_SCENE9_CSS = """

/* ── Scene 9: Final answers overlay ─────────────────────────────────────────── */
#qanim-scene9-overlay {
  display: none; position: fixed; top: 50%; left: 50%;
  transform: translate(-50%,-50%) scale(.95);
  z-index: 7500; width: min(900px,96vw); max-height: 92vh;
  overflow-y: auto; box-sizing: border-box;
  opacity: 0; pointer-events: none;
  transition: opacity .3s, transform .32s var(--ease-spring);
}
#qanim-scene9-overlay.qanim-scene-visible {
  display: block !important; opacity: 1; pointer-events: auto;
  transform: translate(-50%,-50%) scale(1);
}
.s9-card {
  background: #fff; border-radius: 22px;
  box-shadow: 0 12px 60px rgba(22,163,74,.14), 0 2px 10px rgba(0,0,0,.08);
  border: 1px solid #bbf7d0; overflow: hidden;
  font-family: -apple-system,'Segoe UI',Arial,sans-serif;
}
.s9-title-bar {
  text-align: center; padding: 22px 28px 18px;
  background: linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);
  border-bottom: 2px solid #86efac;
}
.s9-title-bar h2 { font-size: 22px; font-weight: 900; color: #14532d; letter-spacing: -.3px; margin-bottom: 4px; }
.s9-title-bar p { font-size: 13px; color: #166534; margin: 0; }
.s9-body { padding: 32px 36px 28px; background: #fff; display: flex; flex-direction: column; gap: 22px; }
.s9-nav-row {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  padding: 16px 36px 22px; border-top: 1px solid #bbf7d0; background: #f0fdf4;
}
#qanim-scene9-overlay .lesson-answer-grid {
  display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 14px;
}
#qanim-scene9-overlay .lesson-answer {
  background: #edf9f6; border: 1px solid #bce7de; border-radius: 15px; padding: 20px;
}
#qanim-scene9-overlay .lesson-answer h3 {
  color: #00858d; font-size: 12px; font-weight: 800;
  letter-spacing: .7px; text-transform: uppercase; line-height: 1.5; margin: 0;
}
#qanim-scene9-overlay .lesson-answer-number {
  color: #00858d; font-size: 36px; font-weight: 800; line-height: 1.3;
  margin: 16px 0 10px; overflow-wrap: anywhere;
}
#qanim-scene9-overlay .lesson-answer-number span { font-size: 18px; font-weight: 600; margin-left: 7px; }
#qanim-scene9-overlay .lesson-answer p { font-size: 13px; color: #476d82; line-height: 1.5; margin: 0; }
@media(max-width:600px){
  #qanim-scene9-overlay .lesson-answer-grid{grid-template-columns:1fr;}
  #qanim-scene9-overlay .s9-body{padding:20px;}
}
"""

_CONTROLS_CSS = """
/* ── Controls bar ──────────────────────────────────────────────────────── */
#qanim-controls-bar {
  position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
  display: flex; align-items: center; gap: 8px;
  background: rgba(15,23,42,0.92); border: 1px solid #334155;
  border-radius: 40px; padding: 8px 14px;
  box-shadow: 0 4px 24px rgba(0,0,0,0.5);
  z-index: 9970; backdrop-filter: blur(8px);
}
.qanim-ctrl-btn {
  display: flex; flex-direction: column; align-items: center; gap: 2px;
  background: transparent; border: none; color: #94a3b8;
  cursor: pointer; padding: 6px 10px; border-radius: 10px;
  font-size: 10px; font-weight: 600; transition: all 0.2s;
  white-space: nowrap;
}
.qanim-ctrl-btn:hover { background: #1e293b; color: #e2e8f0; }
.qanim-ctrl-btn span:first-child { font-size: 18px; }
.ctrl-label { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.3px; }
.qanim-ctrl-sep { width: 1px; height: 28px; background: #334155; }
.glossary-ctrl-badge {
  position: absolute; top: 4px; right: 4px;
  background: #ef4444; color: #fff;
  font-size: 9px; font-weight: 800;
  width: 16px; height: 16px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
}
/* Glossary panel */
#qanim-glossary-backdrop {
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,0.6); z-index: 9994; backdrop-filter: blur(2px);
}
#qanim-glossary-backdrop.gp-open { display: block; }
#qanim-glossary-panel {
  display: none; position: fixed; top: 50%; left: 50%;
  transform: translate(-50%,-50%);
  width: min(440px,94vw); max-height: 80vh;
  background: #1e293b; border: 1px solid #334155;
  border-radius: 16px; box-shadow: 0 8px 40px rgba(0,0,0,0.5);
  z-index: 9995; flex-direction: column; overflow: hidden;
  font-family: 'Inter','Segoe UI',system-ui,sans-serif;
}
#qanim-glossary-panel.gp-open { display: flex; }
#qanim-glossary-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 18px;
  background: linear-gradient(135deg,#1e3a5f,#1e293b);
  border-bottom: 1px solid #334155; flex-shrink: 0;
}
.glossary-header-title { font-size: 15px; font-weight: 700; color: #f1f5f9; }
.glossary-hdr-btn {
  background: #334155; border: none; color: #94a3b8;
  width: 28px; height: 28px; border-radius: 50%; cursor: pointer;
  font-size: 14px; display:flex; align-items:center; justify-content:center;
  transition: background 0.2s;
}
.glossary-hdr-btn:hover { background: #475569; color: #f1f5f9; }
#qanim-glossary-body {
  flex: 1; overflow-y: auto; padding: 16px;
  display: flex; flex-direction: column; gap: 10px;
}
.glossary-term-card {
  background: #0f172a; border: 1px solid #334155; border-radius: 10px;
  padding: 12px 14px;
}
.glossary-term-word {
  font-size: 14px; font-weight: 700; color: #38bdf8; margin-bottom: 4px;
}
.glossary-term-meaning { font-size: 12.5px; color: #94a3b8; line-height: 1.55; }
"""


_MATHJAX_CSS = """
/* -- MathJax rendering tweaks -------------------------------------------- */
mjx-container { overflow-x: auto; max-width: 100%; }
mjx-container[jax="SVG"] { direction: ltr; }
"""

_MATHJAX_SCRIPT = """<script>
  window.MathJax = {
    tex: {
      inlineMath: [['\\(', '\\)']],
      displayMath: [['\\[', '\\]']],
      processEscapes: true
    },
    svg: { fontCache: 'global' },
    startup: {
      ready() {
        MathJax.startup.defaultReady();
        window.qanimRenderMath = function() {
          if (window.MathJax && MathJax.typesetPromise) {
            MathJax.typesetPromise().catch(function(e) {
              console.warn('[QAnim] MathJax typeset error:', e);
            });
          }
        };
      }
    }
  };
</script>
<script id="qanim-mathjax-loader" src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js" async></script>"""

_SCENE6_JS = """
<script id="qanim-js-scene6">
(function(){
  'use strict';
  function showScene6(){
    var ov=document.getElementById('qanim-scene6-overlay');
    var bd=document.getElementById('qanim-scene-modal-backdrop');
    var sc=document.querySelector('.svg-container');
    if(ov){ov.classList.add('qanim-scene-visible');}
    if(bd){bd.classList.add('qanim-scene-visible');}
    if(sc){sc.style.opacity='0.15';}
    var d6=document.getElementById('dot-step7');
    if(d6){document.querySelectorAll('.step-dot').forEach(function(d){d.classList.remove('active');});d6.classList.add('active');}
  }
  function hideScene6(){
    var ov=document.getElementById('qanim-scene6-overlay');
    var bd=document.getElementById('qanim-scene-modal-backdrop');
    var sc=document.querySelector('.svg-container');
    if(ov){ov.classList.remove('qanim-scene-visible');}
    if(bd){bd.classList.remove('qanim-scene-visible');}
    if(sc){sc.style.opacity='1';}
  }
  window.qanim_showScene6=showScene6;
  window.qanim_hideScene6=hideScene6;
  window.qanim_goToPrevScene=function(){
    hideScene6();
    if(typeof window.applyStep==='function') window.applyStep(5);
  };
})();
</script>
"""

_SCENE7_JS = """
<script id="qanim-js-scene7">
(function(){
  'use strict';
  var _currentTaskIdx=0;
  function _getLessonTasks(){
    try{
      var el=document.getElementById('qanim-lesson-tasks-data');
      if(el){var d=JSON.parse(el.textContent||'[]');if(Array.isArray(d)&&d.length)return d;}
    }catch(e){}
    return [];
  }
  function _esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  function _renderTask(idx){
    var tasks=_getLessonTasks();
    if(!tasks.length)return;
    idx=Math.max(0,Math.min(idx,tasks.length-1));
    _currentTaskIdx=idx;
    var t=tasks[idx];
    var n=tasks.length;
    var prog=document.getElementById('s8-progress');
    if(prog)prog.textContent='Answer '+(idx+1)+' of '+n;
    var titleEl=document.getElementById('s8-task-title');
    if(titleEl)titleEl.textContent=t.name||'';
    var givenEl=document.getElementById('s8-task-given');
    if(givenEl){
      var givenLines=(t.given||'').split('\n').filter(function(l){return l.trim();});
      givenEl.innerHTML=givenLines.map(function(l){
        return '<div style="padding:3px 0;border-bottom:1px solid rgba(59,130,246,.15);display:flex;align-items:center;gap:8px;">'
          +'<span style="color:#3b82f6;font-weight:900;flex-shrink:0">&#x2022;</span>'
          +'<span style="font-family:Courier New,monospace;font-weight:700;">'+_esc(l)+'</span>'
          +'</div>';
      }).join('');
    }
    var formulaEl=document.getElementById('s8-task-formula');
    if(formulaEl)formulaEl.textContent=t.formula||'';
    var workEl=document.getElementById('s8-task-work');
    if(workEl){
      var work=Array.isArray(t.work)?t.work:[];
      workEl.innerHTML=work.map(function(w,wi){
        return '<div class="s7-approach-step">'
          +'<span class="s7-approach-step-num">'+(wi+1)+'</span>'
          +'<span>'+_esc(w.label||'')
          +'<span class="s7-approach-step-eq">'+_esc(w.eq||'')+'</span>'
          +'</span>'
          +'</div>';
      }).join('');
    }
    var resultEl=document.getElementById('s8-task-result');
    if(resultEl)resultEl.textContent=(t.value||'?')+(t.unit?' '+t.unit:'');
    document.querySelectorAll('#s8-task-nav .lesson-target').forEach(function(btn,bi){
      btn.classList.toggle('is-current',bi===idx);
    });
    var backBtn=document.getElementById('s8-back');
    var nextBtn=document.getElementById('s8-next');
    if(backBtn)backBtn.textContent=(idx===0)?'← Step 7':'← Previous';
    if(nextBtn)nextBtn.textContent=(idx===n-1)?'Step 9: Answers ▶':'Next answer ▶';
  }
  function showScene7(){
    var ov=document.getElementById('qanim-scene7-overlay');
    var bd=document.getElementById('qanim-scene-modal-backdrop');
    var sc=document.querySelector('.svg-container');
    if(ov){ov.classList.add('qanim-scene-visible');}
    if(bd){bd.classList.add('qanim-scene-visible');}
    if(sc){sc.style.opacity='0.15';}
    var d8=document.getElementById('dot-step8');
    if(d8){document.querySelectorAll('.step-dot').forEach(function(d){d.classList.remove('active');});d8.classList.add('active');}
    _renderTask(0);
  }
  function hideScene7(){
    var ov=document.getElementById('qanim-scene7-overlay');
    var bd=document.getElementById('qanim-scene-modal-backdrop');
    var sc=document.querySelector('.svg-container');
    if(ov){ov.classList.remove('qanim-scene-visible');}
    if(bd){bd.classList.remove('qanim-scene-visible');}
    if(sc){sc.style.opacity='1';}
  }
  window.qanim_showScene7=showScene7;
  window.qanim_hideScene7=hideScene7;
  window.qanim_goToScene6FromScene7=function(){hideScene7();if(typeof window.qanim_showScene6==='function')window.qanim_showScene6();};
  window.qanimNextCondition=function(){
    var tasks=_getLessonTasks();
    if(_currentTaskIdx<tasks.length-1){_renderTask(_currentTaskIdx+1);}
    else{hideScene7();if(typeof window.qanim_showScene9==='function')window.qanim_showScene9();}
  };
  window.qanimPreviousCondition=function(){
    if(_currentTaskIdx>0){_renderTask(_currentTaskIdx-1);}
    else{hideScene7();if(typeof window.qanim_showScene6==='function')window.qanim_showScene6();}
  };
  window.qanimS8SwitchTask=function(idx){_renderTask(idx);};
})();
</script>
"""

_SCENE9_JS = """
<script id="qanim-js-scene9">
(function(){
  'use strict';
  function showScene9(){
    var ov=document.getElementById('qanim-scene9-overlay');
    var bd=document.getElementById('qanim-scene-modal-backdrop');
    var sc=document.querySelector('.svg-container');
    if(ov){ov.classList.add('qanim-scene-visible');}
    if(bd){bd.classList.add('qanim-scene-visible');}
    if(sc){sc.style.opacity='0.15';}
    var d9=document.getElementById('dot-step9');
    if(d9){document.querySelectorAll('.step-dot').forEach(function(d){d.classList.remove('active');});d9.classList.add('active');}
  }
  function hideScene9(){
    var ov=document.getElementById('qanim-scene9-overlay');
    var bd=document.getElementById('qanim-scene-modal-backdrop');
    var sc=document.querySelector('.svg-container');
    if(ov){ov.classList.remove('qanim-scene-visible');}
    if(bd){bd.classList.remove('qanim-scene-visible');}
    if(sc){sc.style.opacity='1';}
  }
  function qanim_goToScene7FromScene9(){
    hideScene9();
    if(typeof window.qanim_showScene7==='function')window.qanim_showScene7();
  }
  window.qanim_showScene9=showScene9;
  window.qanim_hideScene9=hideScene9;
  window.qanim_goToScene7FromScene9=qanim_goToScene7FromScene9;
})();
</script>
"""

_GLOSSARY_JS = """
<script id="qanim-js-glossary">
(function(){
  'use strict';
  if(window.__qanimGlossaryInit) return;
  window.__qanimGlossaryInit=true;
  function _el(id){return document.getElementById(id);}
  function openGlossary(){
    var bd=_el('qanim-glossary-backdrop'),p=_el('qanim-glossary-panel');
    if(bd){bd.classList.add('gp-open');}
    if(p){p.classList.add('gp-open');p.setAttribute('aria-hidden','false');}
  }
  function closeGlossary(){
    var bd=_el('qanim-glossary-backdrop'),p=_el('qanim-glossary-panel');
    if(bd){bd.classList.remove('gp-open');}
    if(p){p.classList.remove('gp-open');p.setAttribute('aria-hidden','true');}
  }
  function onReady(fn){
    if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',fn);
    else setTimeout(fn,0);
  }
  onReady(function(){
    var ob=_el('glossary-ctrl-btn');if(ob)ob.addEventListener('click',openGlossary);
    var cb=_el('glossary-close-btn');if(cb)cb.addEventListener('click',closeGlossary);
    var bd=_el('qanim-glossary-backdrop');if(bd)bd.addEventListener('click',closeGlossary);
    document.addEventListener('keydown',function(e){if(e.key==='Escape')closeGlossary();});
  });
})();
</script>
"""

_ANSWERBOX_JS_TMPL = """
<script id="qanim-js-answerbox">
(function(){
  'use strict';
  if(window.__qanimAnswerBoxInit) return;
  window.__qanimAnswerBoxInit=true;

  var _rawTargets = {{TARGETS_JSON}};
  var _targets = Array.isArray(_rawTargets) ? _rawTargets : [];

  // Allow Customize panel to reset the targets cache
  window.__qanimAnswerBoxReset = function() { _targets = null; };

  function _el(id){return document.getElementById(id);}

  function _getTargets(){
    if(Array.isArray(_targets)) return _targets;
    try{
      var el=_el('__answer_targets__');
      var data=el?JSON.parse(el.textContent||'{}'):{};
      _targets=Array.isArray(data.answer_targets)?data.answer_targets:_rawTargets||[];
    }catch(e){_targets=_rawTargets||[];}
    return _targets;
  }

  function _normalize(s){
    return String(s).toLowerCase()
      .replace(/[^a-z0-9.\-]/g,' ')
      .replace(/\\s+/g,' ').trim();
  }

  function _check(userAns, target){
    var u=_normalize(userAns);
    var tv=_normalize(String(target.value||''));
    var tu=_normalize(String(target.unit||''));
    if(!tv) return false;
    return u.indexOf(tv)>-1 && (!tu || u.indexOf(tu)>-1);
  }

  function openAnswerBox(){
    var targets=_getTargets();
    var ft=_el('ab-find-text');
    if(ft && targets.length){
      ft.textContent=targets.map(function(t){return t.label||(t.value+' '+(t.unit||''));}).join(' | ');
    }
    var inp=_el('ab-user-input');
    if(inp){inp.value='';}
    ['ab-feedback','ab-reveal'].forEach(function(cls){
      document.querySelectorAll('.'+cls).forEach(function(el){el.className=el.className.replace(' ab-correct','').replace(' ab-wrong','').replace(' ab-visible','');});
    });
    var bd=_el('answerbox-backdrop'),p=_el('answerbox-panel');
    if(bd){bd.classList.add('ab-open');}
    if(p){p.classList.add('ab-open');p.setAttribute('aria-hidden','false');}
    if(inp) inp.focus();
  }

  function closeAnswerBox(){
    var bd=_el('answerbox-backdrop'),p=_el('answerbox-panel');
    if(bd){bd.classList.remove('ab-open');}
    if(p){p.classList.remove('ab-open');p.setAttribute('aria-hidden','true');}
  }

  function submitAnswer(){
    var inp=_el('ab-user-input');
    if(!inp) return;
    var userAns=inp.value.trim();
    if(!userAns) return;
    var targets=_getTargets();
    var correct=targets.some(function(t){return _check(userAns,t);});
    var fb=_el('ab-feedback');
    var rv=_el('ab-reveal');
    if(fb){
      fb.className='ab-feedback '+(correct?'ab-correct':'ab-wrong');
      fb.textContent=correct
        ?'\u2705 Correct! Well done.'
        :'\u274C Not quite. Try again or check the answer below.';
    }
    if(rv){
      rv.className='ab-reveal ab-visible';
      rv.innerHTML='<strong>Expected:</strong> '
        +targets.map(function(t){return (t.value||'')+(t.unit?' '+t.unit:'');}).join(' or ');
    }
  }

  function onReady(fn){
    if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',fn);
    else setTimeout(fn,0);
  }

  onReady(function(){
    var ob=_el('answerbox-ctrl-btn');if(ob)ob.addEventListener('click',openAnswerBox);
    var cb=_el('ab-close-btn');if(cb)cb.addEventListener('click',closeAnswerBox);
    var bd=_el('answerbox-backdrop');if(bd)bd.addEventListener('click',function(e){if(e.target===bd)closeAnswerBox();});
    var sb=_el('ab-submit-btn');if(sb)sb.addEventListener('click',submitAnswer);
    var inp=_el('ab-user-input');
    if(inp)inp.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();submitAnswer();}});
    document.addEventListener('keydown',function(e){if(e.key==='Escape')closeAnswerBox();});
  });
})();
</script>
"""

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
    # apply_step_js from svg_data is not used: applyStep is defined in nav_js directly
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
    _selfcheck_customize_js(customize_html)
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
  <style id="qanim-mathjax-styles">
{_MATHJAX_CSS}
  </style>
  {_MATHJAX_SCRIPT}

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
