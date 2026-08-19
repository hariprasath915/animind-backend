"""
q_animation.py  --  QAnim Question Animation Generator  v2.0
=============================================================

v2.0 -- FULL 9-STEP WORKFLOW REFACTOR:

  WHAT CHANGED FROM v1.x:
  - The entire pipeline now generates the SAME 9-step workflow that the
    reference HTML (Convective_Heat_Loss_Updated.html) demonstrates:

      Steps 1–6:  SVG concept animation (component-by-component reveal).
                  Each step introduces exactly ONE new physical element
                  (given parameter, derived quantity, or system component).
                  Step 6 always shows the "Setup Summary" + "To Find" state.

      Step 7:     Scene 6 modal — Main Formula reveal with per-variable
                  explanation (symbol, name, value, unit, color-coded box).

      Step 8:     Scene 7/8 modal — Step-by-step substitution walkthrough
                  (left panel: physical system visual; right panel: given
                  parameters list + solution approach + formula result bar).

      Step 9:     Scene 9 modal — Final Answer with animated substitution
                  chain, green highlighted result box, orange insight bar
                  + AnswerBox for user answer input and feedback.

  - GeminiSceneAnalyzer now produces a structured script that maps directly
    to this 9-step contract.  The scene script is richer: it includes
    formula_data (Step 7), substitution_data (Step 8), and final_answer_data
    (Step 9) alongside the existing SVG steps for Steps 1–6.

  - GeminiAnimationBuilder uses an updated prompt that enforces the 9-step
    contract in the generated HTML.

  - All existing infrastructure (panels, CSS/JS injection, validation,
    PanelReliabilityEngine, MathTypography, etc.) is preserved unchanged.

  BUG FIX (v2.0.1):
    GEMINI_MODEL updated from "gemini-2.5-pro-preview-06-05" (404 NOT_FOUND)
    to "gemini-2.5-pro" (stable GA release).

  BUG FIX (v2.0.2):
    GEMINI_MODEL updated from "gemini-2.5-pro" (404 NOT_FOUND — no longer
    available to new users) to "gemini-3.1-pro-preview".

    ROOT CAUSE OF INCOMPLETE OUTPUT:
    When the Gemini API returned 404 (old model), ALL three pipeline stages
    (SceneAnalyzer, SolutionGenerator, GlossaryAnalyzer) fell back to their
    hardcoded placeholder data:
      - formula_data:      "Result = f(given values)"
      - substitution_data: "Given values from the problem"
      - final_answer_data: answer_value="Result", to_find_label="Unknown quantity"
    The pipeline never checked whether it was using fallback data before
    injecting these placeholders into Scenes 6, 7, and 9.
    Additionally, fad.get("answer_value") returned "Result" which is truthy,
    so the AnswerBox also received the placeholder value.

    FIXES APPLIED (beyond model upgrade):
    1. Added _is_fallback_content() to detect placeholder strings.
    2. Pipeline now merges real sol data into scene_script panels when
       SceneAnalyzer used its fallback but SolutionGenerator succeeded.
    3. answer_targets guard now rejects placeholder "Result" values.
    4. inject_scene7/7/9 each validate formula/substitution/answer content
       before rendering, falling back to sol data when placeholders detected.

  REQUIRED ENV VAR:
    GEMINI_API_KEY=your-key
"""

import json
import re
import asyncio
import html as html_module
from typing import Optional
import os as _os

# ---------------------------------------------------------------------------
# Gemini SDK — dual-variant import guard
# ---------------------------------------------------------------------------
_GEMINI_AVAILABLE  = False
_GEMINI_SDK_STYLE  = None
_google_genai      = None

try:
    from google import genai as _google_genai
    _GEMINI_AVAILABLE = True
    _GEMINI_SDK_STYLE = "genai"
    print("[QAnim Gemini] SDK: google-genai loaded")
except ImportError:
    try:
        import google.generativeai as _google_genai
        _GEMINI_AVAILABLE = True
        _GEMINI_SDK_STYLE = "generativeai"
        print("[QAnim Gemini] SDK: google-generativeai loaded (deprecated)")
    except ImportError:
        print("[QAnim Gemini] No Gemini SDK found — generation will fail gracefully")

# ---------------------------------------------------------------------------
# Anthropic client (kept for backward compatibility — panels don't call API)
# ---------------------------------------------------------------------------
_ANTHROPIC_INIT_ERROR = None

class _DeadAnthropicClient:
    class _DeadMessages:
        def create(self, *args, **kwargs):
            raise RuntimeError(
                f"Anthropic client unavailable: {_ANTHROPIC_INIT_ERROR}. "
                "Generation now uses Gemini — ANTHROPIC_API_KEY is not required."
            )
    def __init__(self):
        self.messages = self._DeadMessages()

try:
    import anthropic as _anthropic_module
    _anthropic_api_key = _os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not _anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY not set — using Gemini-only pipeline")
    client = _anthropic_module.Anthropic(
        api_key=_anthropic_api_key,
        default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        timeout=600.0,
        max_retries=4,
    )
    print("[QAnim Anthropic] Client ready (kept for backward compatibility).")
except Exception as _anthropic_init_err:
    _ANTHROPIC_INIT_ERROR = repr(_anthropic_init_err)
    print(f"[QAnim Anthropic] Client not available ({_ANTHROPIC_INIT_ERROR}) — Gemini-only mode.")
    client = _DeadAnthropicClient()

# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------

# ╔══════════════════════════════════════════════════════════════════╗
# ║  FIX (v2.0.2): "gemini-2.5-pro" is no longer available to new  ║
# ║  users (HTTP 404 NOT_FOUND).                                    ║
# ║  Updated to "gemini-3.1-pro-preview" (current stable release). ║
# ╚══════════════════════════════════════════════════════════════════╝
GEMINI_MODEL = "gemini-3.1-pro-preview"

_gemini_client = None
_GEMINI_DISABLED_REASON = None

if _GEMINI_AVAILABLE:
    _gkey = _os.environ.get("GEMINI_API_KEY", "").strip()
    if not _gkey:
        _GEMINI_DISABLED_REASON = "GEMINI_API_KEY not set"
        print("[QAnim Gemini] GEMINI_API_KEY not set — all generation will fail gracefully")
    elif _GEMINI_SDK_STYLE == "generativeai":
        try:
            _google_genai.configure(api_key=_gkey)
            _gemini_client = _google_genai
            print(f"[QAnim Gemini] Client ready (google-generativeai, model={GEMINI_MODEL})")
        except Exception as _gem_err:
            _GEMINI_DISABLED_REASON = repr(_gem_err)
            print(f"[QAnim Gemini] Init failed: {_gem_err}")
    else:
        try:
            _gemini_client = _google_genai.Client(api_key=_gkey)
            print(f"[QAnim Gemini] Client ready (google-genai, model={GEMINI_MODEL})")
        except Exception as _gem_err:
            _GEMINI_DISABLED_REASON = repr(_gem_err)
            print(f"[QAnim Gemini] Init failed: {_gem_err}")
else:
    _GEMINI_DISABLED_REASON = "No Gemini SDK installed"

MAX_TOK = 18000
MAX_TOK_CONCEPT = 16000

# ---------------------------------------------------------------------------
# Timeout budgets
# ---------------------------------------------------------------------------
STAGE_TIMEOUT_SMALL  = 180.0
STAGE_TIMEOUT_SCENE  = 150.0
STAGE_TIMEOUT_BUILD  = 150.0
PIPELINE_TIMEOUT = max(STAGE_TIMEOUT_SCENE, STAGE_TIMEOUT_SMALL) + STAGE_TIMEOUT_BUILD + 20.0


def _err_msg(e: BaseException) -> str:
    if isinstance(e, asyncio.TimeoutError):
        return (
            "Gemini took too long to respond and the request was cancelled "
            "(the model may be overloaded, or the response was unusually "
            "large). This is a timeout, not a code error — please try again."
        )
    msg = str(e).strip()
    if msg:
        return f"{type(e).__name__}: {msg}"
    return f"{type(e).__name__} (no additional detail was provided by the exception)"


# ---------------------------------------------------------------------------
# Placeholder / fallback content detection
# ---------------------------------------------------------------------------
_PLACEHOLDER_STRINGS = frozenset({
    "result = f(given values)",
    "result = f(values)",
    "result",
    "unknown quantity",
    "given values from the problem",
    "governing equation",
    "parameter a",
    "computed value",
    "see calculation above",
    "apply the governing formula",
    "apply governing formula",
    "substitute values",
    "compute the result",
    "compute result",
    "formula from problem domain",
})


def _is_fallback_content(value: str) -> bool:
    """Return True if value is a known placeholder string from _fallback_script()."""
    if not value:
        return True
    return value.strip().lower() in _PLACEHOLDER_STRINGS


def _scene_script_is_fallback(scene_script: dict) -> bool:
    """Return True if scene_script came from _fallback_script() (contains placeholder data)."""
    fad = scene_script.get("final_answer_data", {})
    if _is_fallback_content(fad.get("answer_value", "")):
        return True
    fd = scene_script.get("formula_data", {})
    if _is_fallback_content(fd.get("formula_text", "")):
        return True
    return False


def _merge_sol_into_scene_script(scene_script: dict, sol: dict, to_find_targets: list) -> dict:
    """
    When SceneAnalyzer used its fallback but SolutionGenerator has real data,
    overwrite the placeholder fields in scene_script with data from sol.
    This ensures Scenes 6, 7, 9 show real physics content even when the
    SceneAnalyzer API call failed.
    """
    import copy
    sc = copy.deepcopy(scene_script)
    sol = sol or {}
    label = to_find_targets[0] if to_find_targets else "Final Answer"

    # ── formula_data (Scene 6) ──────────────────────────────────────
    sc["formula_data"] = {
        "formula_text":     sol.get("formula", sc.get("formula_data", {}).get("formula_text", "Governing Formula")),
        "formula_sublabel": "Governing Equation",
        "variables":        sol.get("variables", []),
        "note_text":        sol.get("key_insight", ""),
    }

    # ── substitution_data (Scene 7) ─────────────────────────────────
    steps_text = [str(s) for s in sol.get("steps", [])[:6]]
    fa = sol.get("final_answer", "")
    sc["substitution_data"] = {
        "system_title":       label,
        "system_description": scene_script.get("title", "")[:120],
        "given_list":         steps_text[:4] if steps_text else ["See solution steps"],
        "approach_steps":     steps_text[:3] if steps_text else ["Apply governing formula"],
        "result_bar":         fa,
    }

    # ── final_answer_data (Scene 9) ────────────────────────────────
    chain_raw = sol.get("substitution_chain", [])
    chain = []
    for i, row in enumerate(chain_raw[:6]):
        if isinstance(row, dict):
            chain.append(row)
        else:
            chain.append({"num": i + 1, "eq": str(row)[:80]})
    if not chain:
        for i, s in enumerate(sol.get("steps", [])[:5]):
            chain.append({"num": i + 1, "eq": str(s)[:80]})

    nums = re.findall(r'[-+]?\d+(?:\.\d+)?', fa)
    val  = nums[-1] if nums else fa[:30]

    sc["final_answer_data"] = {
        "formula_recap":      sol.get("formula", "Governing Formula"),
        "substitution_chain": chain,
        "answer_value":       val,
        "answer_unit":        _extract_unit(fa),
        "answer_highlight":   val,
        "insight_text":       sol.get("key_insight", "Apply the governing formula with the given data."),
        "to_find_label":      label,
    }
    sc["final_answer"] = fa
    sc["key_insight"]  = sol.get("key_insight", "")

    QAnimLogger.ok("MergeSol", "Merged SolutionGenerator data into fallback scene_script")
    return sc


def _extract_unit(text: str) -> str:
    """Extract unit from a final answer string like '6000 W' or 'g/4'."""
    text = text.strip()
    # Symbolic answers like "g/4", "g/2" — keep as-is
    if re.match(r'^[a-zA-Z][^0-9]{0,10}$', text):
        return text
    m = re.search(r'\d\s*([A-Za-z°²³µ][A-Za-z°²³µ·/²³\s]*?)(?:\s|$|[,.])', text)
    return m.group(1).strip() if m else ""


# ===========================================================================
#  MODULE 1 — QAnimLogger
# ===========================================================================
class QAnimLogger:
    PREFIX = "[QAnim v2.0]"

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
#  MODULE 1.5 — Robust JSON Sanitizer
# ===========================================================================
def _sanitize_json_str(raw: str) -> str:
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
        else:
            raw = raw[start:]

    out = []
    in_str = False
    esc = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if esc:
            out.append(ch)
            esc = False
            i += 1
            continue
        if ch == '\\' and in_str:
            out.append(ch)
            esc = True
            i += 1
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            i += 1
            continue
        if not in_str and ch == '/' and i + 1 < len(raw) and raw[i+1] == '/':
            while i < len(raw) and raw[i] != '\n':
                i += 1
            continue
        if not in_str and ch == '/' and i + 1 < len(raw) and raw[i+1] == '*':
            i += 2
            while i + 1 < len(raw) and not (raw[i] == '*' and raw[i+1] == '/'):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    raw = ''.join(out)

    raw = re.sub(r',\s*([}\]])', r'\1', raw)
    raw = re.sub(r"(?<![:\\])(?<!\w)'([^']*?)'(?!\w)", r'"\1"', raw)
    raw = re.sub(r'(?<={|,)\s*([A-Za-z_]\w*)\s*:', r'"\1":', raw)
    raw = re.sub(r'\bTrue\b', 'true', raw)
    raw = re.sub(r'\bFalse\b', 'false', raw)
    raw = re.sub(r'\bNone\b', 'null', raw)
    raw = re.sub(r'\.\.\.', '', raw)
    return raw.strip()


# ===========================================================================
#  MODULE 2 — GenerationValidator
# ===========================================================================
class ValidationError(Exception):
    pass

class GenerationValidator:
    @classmethod
    def validate(cls, html: str, require_svg: bool = True):
        if not html or len(html) < 500:
            raise ValidationError(f"HTML too short ({len(html)} chars)")
        if '<html' not in html.lower():
            raise ValidationError("Missing <html> tag")
        if require_svg and '<svg' not in html.lower():
            raise ValidationError("Missing <svg> tag")
        if 'stepsData' not in html:
            raise ValidationError("Missing stepsData JS array")
        return True


# ===========================================================================
#  MODULE 2.5 — ToFindExtractor
# ===========================================================================
class ToFindExtractor:
    _TRIGGER_PATTERNS = [
        re.compile(r'\b(?:find|calculate|determine|evaluate|compute|obtain|derive|solve for|what is|what are)\b\s+(.{5,90}?)(?:[.?!]|$)', re.IGNORECASE),
        re.compile(r'\b(?:find|calculate|determine)\b[^.?!]{0,120}', re.IGNORECASE),
    ]
    _NOISE_PREFIXES = ["the", "a", "an", "its", "their", "this", "that", "these", "those"]
    _TRAILING_RE = re.compile(r'\s*(?:if|when|given|where|assuming|for|with|of)\b.*$', re.IGNORECASE)
    _TRIGGER_VERB_RE = re.compile(r'^(?:find|calculate|determine|evaluate|compute|obtain|derive|solve for|what is|what are)\b\s*', re.IGNORECASE)
    _ARTICLE_RE = re.compile(r'^(?:the|a|an)\s+', re.IGNORECASE)

    @classmethod
    def extract(cls, question: str) -> list:
        targets = []
        for pat in cls._TRIGGER_PATTERNS:
            for m in pat.finditer(question):
                raw = m.group(0) if m.lastindex is None else m.group(1)
                t = cls._clean(raw)
                if t and 3 <= len(t) <= 80:
                    targets.append(t)
        targets = cls._deduplicate(targets)
        if not targets:
            targets = cls._fallback(question)
        result = [cls._cap(t) for t in targets[:3]]
        QAnimLogger.ok("ToFindExtractor", f"Extracted {len(result)} target(s): {result}")
        return result

    @classmethod
    def _clean(cls, t):
        t = t.strip()
        for noise in cls._NOISE_PREFIXES:
            if t.lower().startswith(noise + " "):
                t = t[len(noise):].strip()
                break
        t = cls._TRAILING_RE.sub("", t).strip()
        t = cls._TRIGGER_VERB_RE.sub("", t).strip()
        t = cls._ARTICLE_RE.sub("", t).strip()
        return t.rstrip(".,;:?!")

    @classmethod
    def _deduplicate(cls, targets):
        seen, result = set(), []
        for t in targets:
            key = t.lower().strip()
            if key and key not in seen:
                seen.add(key)
                result.append(t)
        return result

    @classmethod
    def _cap(cls, s):
        return s[0].upper() + s[1:] if s else s

    @classmethod
    def _fallback(cls, question):
        try:
            sentences = re.split(r'[.!?]', question.strip())
            for s in reversed(sentences):
                s = s.strip()
                if 4 <= len(s) <= 80:
                    return [cls._cap(s)]
            return []
        except Exception:
            return []


# ===========================================================================
#  MODULE 2.6 — GivenValuesExtractor
# ===========================================================================
class GivenValuesExtractor:
    _COLOR_CLASSES = ["gc-blue", "gc-teal", "gc-green", "gc-amber"]
    _LABEL_RE = re.compile(
        r'(?P<label>[A-Za-z_][A-Za-z_\s]{0,40}?)'
        r'\s*(?:=|is|of|:)\s*'
        r'(?P<val>[-+]?\d+(?:\.\d+)?(?:\s*[×x]\s*10\^?[-+]?\d+)?)'
        r'\s*(?P<unit>[A-Za-z°²³µ/%][A-Za-z°²³µ·/²³\s]*(?:/[A-Za-z²³]+)?)?',
        re.IGNORECASE
    )

    @classmethod
    def extract(cls, question):
        cards = []
        seen_vals = set()
        for m in cls._LABEL_RE.finditer(question):
            label = m.group("label").strip().rstrip(",:;")
            val   = m.group("val").strip()
            unit  = (m.group("unit") or "").strip().rstrip(".,;")
            key   = val + unit
            if key in seen_vals or not label or len(label) < 2:
                continue
            seen_vals.add(key)
            color = cls._COLOR_CLASSES[len(cards) % len(cls._COLOR_CLASSES)]
            cards.append({"label": label, "value": val, "unit": unit, "color": color})
            if len(cards) >= 4:
                break
        QAnimLogger.ok("GivenExtractor", f"Extracted {len(cards)} given card(s)")
        return cards


# ===========================================================================
#  MODULE 2.7 — LargeInputPreprocessor
# ===========================================================================
class LargeInputPreprocessor:
    COMPRESS_THRESHOLD = 600
    HARD_LIMIT = 2000
    _MCQ_LINE_RE = re.compile(
        r'^\s*(?:\([A-Da-d1-4]\)|[A-Da-d1-4][.)]\s|Option\s*[A-D1-4]\s*[:.])',
        re.MULTILINE,
    )

    @classmethod
    def needs_compression(cls, question: str) -> bool:
        return len(question) > cls.COMPRESS_THRESHOLD

    @classmethod
    def compress(cls, question: str) -> str:
        if not cls.needs_compression(question):
            return question
        QAnimLogger.info("LargeInputPreprocessor", f"Compressing {len(question)} char input...")
        stripped = cls._heuristic_strip(question)
        result = stripped[:cls.HARD_LIMIT] if stripped else question[:cls.HARD_LIMIT]
        QAnimLogger.ok("LargeInputPreprocessor", f"Compressed to {len(result)} chars")
        return result

    @classmethod
    def _heuristic_strip(cls, question: str) -> str:
        text = question
        mcq_match = cls._MCQ_LINE_RE.search(text)
        if mcq_match:
            stem = text[:mcq_match.start()].strip()
            if len(stem) > 80:
                text = stem
            else:
                text = cls._MCQ_LINE_RE.sub("", text)
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
        return text


# ===========================================================================
#  MODULE 3 — HtmlSanitizer
# ===========================================================================
class HtmlSanitizer:
    @classmethod
    def sanitize(cls, html):
        html = html.replace('\ufeff', '')
        end = html.rfind('</html>')
        if end != -1:
            html = html[:end + 7]
        html = re.sub(r'document\.write\s*\([^)]*\)\s*;?', '', html, flags=re.IGNORECASE)
        html = re.sub(
            r'<script[^>]+src\s*=\s*["\'][^"\']*["\'][^>]*>\s*</script>',
            '', html, flags=re.IGNORECASE | re.DOTALL)
        html = html.replace('\x00', '')
        html = re.sub(r'<svg(?![^>]*xmlns)', '<svg xmlns="http://www.w3.org/2000/svg"', html, flags=re.IGNORECASE)
        QAnimLogger.ok("Sanitizer", "HTML sanitized")
        return html


# ===========================================================================
#  MODULE 3.5 — Centering CSS Injection
# ===========================================================================
_CENTERING_CSS_OVERRIDE = """\
<style id="qanim-centering-override">
body {
  display: flex !important;
  flex-direction: column !important;
  align-items: center !important;
  justify-content: flex-start !important;
  min-height: 100vh !important;
  padding: 24px 16px 120px !important;
  box-sizing: border-box !important;
}
.dashboard {
  width: 100% !important;
  max-width: 900px !important;
  margin-left: auto !important;
  margin-right: auto !important;
  margin-bottom: 20px !important;
}
.question-banner {
  background: linear-gradient(135deg, #f0f5ff 0%, #e8f0fe 50%, #eef2f9 100%) !important;
  border-bottom: 1px solid #e2e8f0 !important;
  position: relative;
  padding: 24px 30px 22px !important;
}
.q-label {
  font-size: 12px !important;
  font-weight: 900 !important;
  color: #0e7490 !important;
  text-transform: uppercase !important;
  letter-spacing: 2px !important;
}
.q-text {
  font-size: 16.5px !important;
  font-weight: 500 !important;
  line-height: 1.72 !important;
  color: #1e293b !important;
}
.step-dot {
  padding: 5px 13px !important;
  border-radius: 20px !important;
  background: rgba(203,213,225,0.5) !important;
  border: 1px solid #cbd5e1 !important;
  font-size: 11px !important;
  font-weight: 700 !important;
  color: #94a3b8 !important;
  cursor: pointer;
  display: inline-flex !important;
  align-items: center !important;
  white-space: nowrap !important;
}
.step-dot.active {
  background: linear-gradient(135deg, #0e7490 0%, #0891b2 100%) !important;
  border-color: #0891b2 !important;
  color: #ffffff !important;
  box-shadow: 0 2px 10px rgba(8,145,178,0.35) !important;
  transform: scale(1.06) !important;
}
.info-box {
  background: #f8faff !important;
  border: 1px solid #dde6f8 !important;
  border-left: 4px solid #0891b2 !important;
  border-radius: 10px !important;
  padding: 18px 20px !important;
}
.btn-primary {
  background: linear-gradient(135deg, #0e7490 0%, #0891b2 100%) !important;
  color: #ffffff !important;
  box-shadow: 0 4px 12px rgba(8,145,178,0.28) !important;
  border-radius: 8px !important;
}
.btn-secondary {
  background: transparent !important;
  color: #64748b !important;
  border: 1.5px solid #cbd5e1 !important;
}
</style>
"""


def inject_centering_css(html: str) -> str:
    if 'qanim-centering-override' in html:
        return html
    if '</head>' in html:
        html = html.replace('</head>', _CENTERING_CSS_OVERRIDE + '</head>', 1)
    elif '<body' in html:
        idx = html.find('<body')
        html = html[:idx] + _CENTERING_CSS_OVERRIDE + html[idx:]
    QAnimLogger.ok("CenteringCSS", "Centering CSS injected")
    return html


# ===========================================================================
#  MODULE 3.6 — Step Color Theme CSS Injection
# ===========================================================================
_STEP_COLOR_CSS = """\
<style id="qanim-step-colors">
/* Step 1 — Sky Blue */
body[data-step="0"] .control-panel { background: linear-gradient(180deg, #e8f4fd 0%, #d0ebf8 100%) !important; border-top: 3px solid #0ea5e9 !important; }
body[data-step="0"] .info-box { background: #ffffff !important; border: 1.5px solid #bae6fd !important; border-left: 5px solid #0ea5e9 !important; }
body[data-step="0"] .step-progress-bar { background: linear-gradient(90deg, #0ea5e9, #38bdf8) !important; }
body[data-step="0"] .step-dot.active { background: linear-gradient(135deg,#0369a1,#0ea5e9) !important; }

/* Step 2 — Teal */
body[data-step="1"] .control-panel { background: linear-gradient(180deg, #e6faf6 0%, #ccf2e8 100%) !important; border-top: 3px solid #10b981 !important; }
body[data-step="1"] .info-box { background: #ffffff !important; border: 1.5px solid #a7f3d0 !important; border-left: 5px solid #10b981 !important; }
body[data-step="1"] .step-progress-bar { background: linear-gradient(90deg, #059669, #10b981) !important; }
body[data-step="1"] .step-dot.active { background: linear-gradient(135deg,#047857,#10b981) !important; }

/* Step 3 — Amber */
body[data-step="2"] .control-panel { background: linear-gradient(180deg, #fff8e6 0%, #fdedc6 100%) !important; border-top: 3px solid #f59e0b !important; }
body[data-step="2"] .info-box { background: #ffffff !important; border: 1.5px solid #fcd34d !important; border-left: 5px solid #f59e0b !important; }
body[data-step="2"] .step-progress-bar { background: linear-gradient(90deg, #d97706, #f59e0b) !important; }
body[data-step="2"] .step-dot.active { background: linear-gradient(135deg,#b45309,#f59e0b) !important; }

/* Step 4 — Indigo */
body[data-step="3"] .control-panel { background: linear-gradient(180deg, #eef2ff 0%, #dde5ff 100%) !important; border-top: 3px solid #6366f1 !important; }
body[data-step="3"] .info-box { background: #ffffff !important; border: 1.5px solid #c7d2fe !important; border-left: 5px solid #6366f1 !important; }
body[data-step="3"] .step-progress-bar { background: linear-gradient(90deg, #4f46e5, #818cf8) !important; }
body[data-step="3"] .step-dot.active { background: linear-gradient(135deg,#4338ca,#6366f1) !important; }

/* Step 5 — Rose */
body[data-step="4"] .control-panel { background: linear-gradient(180deg, #fff1f2 0%, #ffe4e6 100%) !important; border-top: 3px solid #f43f5e !important; }
body[data-step="4"] .info-box { background: #ffffff !important; border: 1.5px solid #fecdd3 !important; border-left: 5px solid #f43f5e !important; }
body[data-step="4"] .step-progress-bar { background: linear-gradient(90deg, #e11d48, #fb7185) !important; }
body[data-step="4"] .step-dot.active { background: linear-gradient(135deg,#be123c,#f43f5e) !important; }

/* Step 6 — Emerald */
body[data-step="5"] .control-panel { background: linear-gradient(180deg, #f0fdf4 0%, #dcfce7 100%) !important; border-top: 3px solid #22c55e !important; }
body[data-step="5"] .info-box { background: #ffffff !important; border: 1.5px solid #86efac !important; border-left: 5px solid #22c55e !important; }
body[data-step="5"] .step-progress-bar { background: linear-gradient(90deg, #16a34a, #22c55e) !important; }
body[data-step="5"] .step-dot.active { background: linear-gradient(135deg,#15803d,#22c55e) !important; }

/* Step indicator layout */
.step-indicator {
  gap: 4px !important;
  margin-bottom: 16px !important;
  flex-wrap: nowrap !important;
  overflow-x: auto !important;
  scrollbar-width: none !important;
}
.step-indicator::-webkit-scrollbar { display: none; }
.step-dot { padding: 7px 13px !important; font-size: 12px !important; font-weight: 800 !important; border-radius: 22px !important; }
.step-dot.active { padding: 8px 16px !important; transform: scale(1.09) !important; }
.step-label { font-size: 13px !important; font-weight: 800 !important; color: #475569 !important; flex-shrink: 0 !important; }
.step-progress-wrap { height: 6px !important; border-radius: 3px !important; margin-bottom: 18px !important; }
.info-box { padding: 22px 24px !important; min-height: 140px !important; border-radius: 14px !important; gap: 12px !important; }
.info-box h3 { font-size: 18px !important; font-weight: 900 !important; }
.info-desc { font-size: 15px !important; line-height: 1.75 !important; font-weight: 500 !important; color: #334155 !important; }
.badge { font-size: 13px !important; font-weight: 800 !important; padding: 5px 14px !important; }
button { font-size: 14.5px !important; font-weight: 800 !important; }
.control-panel { transition: background 0.4s ease, border-top-color 0.4s ease !important; }
</style>
"""


def inject_step_color_css(html: str) -> str:
    if 'qanim-step-colors' in html:
        return html
    if '</head>' in html:
        html = html.replace('</head>', _STEP_COLOR_CSS + '</head>', 1)
    QAnimLogger.ok("StepColorCSS", "Step color theme CSS injected")
    return html


# ===========================================================================
#  MODULE 4 — RecoveryEngine
# ===========================================================================
class RecoveryEngine:
    @classmethod
    def fallback_html(cls, question: str, reason: str) -> str:
        q_esc = html_module.escape(question[:300])
        r_esc = html_module.escape(reason[:200])
        return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Animation Error</title>
<style>
body{{font-family:'Segoe UI',system-ui,sans-serif;background:#eef2f9;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:24px;}}
.card{{background:#fff;border-radius:16px;padding:32px;max-width:700px;width:100%;box-shadow:0 4px 24px rgba(15,23,42,.10);border:1px solid #e2e8f0;}}
.err{{font-size:13px;color:#dc2626;background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:12px 16px;margin-top:16px;}}
h2{{color:#0e7490;margin-bottom:8px;font-size:20px;}}
p{{color:#475569;line-height:1.6;font-size:14px;}}
</style></head>
<body><div class="card">
<h2>&#9888; Animation Generation Failed</h2>
<p><strong>Question:</strong> {q_esc}</p>
<p class="err"><strong>Reason:</strong> {r_esc}</p>
<p>Please wait a moment and try again. If the problem persists, check your GEMINI_API_KEY.</p>
</div></body></html>"""


# ===========================================================================
#  MODULE 5 — JS Syntax Validator
# ===========================================================================
class JsSyntaxValidator:
    @classmethod
    def find_errors(cls, html: str) -> list:
        errors = []
        script_blocks = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL | re.IGNORECASE)
        for i, block in enumerate(script_blocks):
            try:
                import py_compile, tempfile, os
                with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False) as f:
                    f.write(block)
                    fname = f.name
                try:
                    result = __import__('subprocess').run(
                        ['node', '--check', fname],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode != 0:
                        errors.append(i)
                except Exception:
                    pass
                finally:
                    try:
                        os.unlink(fname)
                    except Exception:
                        pass
            except Exception:
                pass
        return errors

    @classmethod
    def auto_fix_stray_apostrophes(cls, html: str) -> str:
        def fix_block(m):
            content = m.group(1)
            def fix_strings(sm):
                s = sm.group(0)
                inner = s[1:-1]
                inner = inner.replace("'", "&#39;")
                return s[0] + inner + s[-1]
            content = re.sub(r"'[^']*'", fix_strings, content)
            return f'<script>{content}</script>'
        return re.sub(r'<script[^>]*>(.*?)</script>', fix_block, html, flags=re.DOTALL | re.IGNORECASE)


# ===========================================================================
#  MODULE 6 — Document Skeleton Normalizer
# ===========================================================================
class DocumentSkeletonNormalizer:
    @staticmethod
    def normalize(html: str) -> str:
        if not html:
            return html
        original_len = len(html)
        html = html.strip()
        html = re.sub(r'^```(?:html)?\s*\n?', '', html, flags=re.IGNORECASE)
        html = re.sub(r'\n?```\s*$', '', html)
        last_close = html.rfind('</html>')
        if last_close != -1:
            html = html[:last_close + len('</html>')]
        if '<!DOCTYPE' not in html[:200].upper():
            html = '<!DOCTYPE html>\n' + html
        if not re.search(r'<html[\s>]', html, re.IGNORECASE):
            html = re.sub(r'(<!DOCTYPE[^>]*>)', r'\1\n<html lang="en">', html, count=1, flags=re.IGNORECASE)
            if not html.rstrip().endswith('</html>'):
                html = html + '\n</html>'
        head_open = re.search(r'<head[\s>]', html, re.IGNORECASE)
        head_close = re.search(r'</head\s*>', html, re.IGNORECASE)
        if not head_open:
            html = re.sub(r'(<html[^>]*>)', r'\1\n<head><meta charset="UTF-8"></head>', html, count=1, flags=re.IGNORECASE)
        elif not head_close:
            body_open = re.search(r'<body[\s>]', html, re.IGNORECASE)
            insert_at = body_open.start() if body_open else len(html)
            html = html[:insert_at] + '</head>\n' + html[insert_at:]
        body_open = re.search(r'<body[\s>]', html, re.IGNORECASE)
        if not body_open:
            head_close2 = re.search(r'</head\s*>', html, re.IGNORECASE)
            insert_at = head_close2.end() if head_close2 else len(html)
            html = html[:insert_at] + '\n<body>' + html[insert_at:]
        if not re.search(r'</body\s*>', html, re.IGNORECASE):
            html_close = re.search(r'</html\s*>', html, re.IGNORECASE)
            insert_at = html_close.start() if html_close else len(html)
            html = html[:insert_at] + '\n</body>\n' + html[insert_at:]
        if not re.search(r'</html\s*>', html, re.IGNORECASE):
            html = html + '\n</html>'
        if len(html) != original_len:
            QAnimLogger.warn("SkeletonNormalizer", f"Document skeleton repaired ({original_len} -> {len(html)} chars)")
        else:
            QAnimLogger.ok("SkeletonNormalizer", "Document skeleton already well-formed")
        return html


# ===========================================================================
#  MODULE 7 — Helper: insert_before_close
# ===========================================================================
def _insert_before_container_close(html: str, open_tag_regex: str, insertion: str) -> str:
    m = re.search(open_tag_regex, html, re.IGNORECASE)
    if not m:
        return html
    start = m.start()
    depth = 0
    in_str = False
    i = start
    while i < len(html):
        ch = html[i]
        if html[i:i+4].lower() == '<div' and not in_str:
            depth += 1
        elif html[i:i+6].lower() == '</div>' and not in_str:
            depth -= 1
            if depth == 0:
                return html[:i] + insertion + html[i:]
        i += 1
    return html + insertion


# ===========================================================================
#  MODULE 8 — GeminiSolutionGenerator
# ===========================================================================

_SOLUTION_SYSTEM_GEMINI = """You are a precise physics/engineering/math solver.
Given a question, produce a step-by-step solution and final answer in JSON.

Return ONLY valid JSON (no markdown, no fences):
{
  "steps": [
    "Step 1: Identify the governing formula: Q = h × A × (Ts - T∞)",
    "Step 2: Substitute values: Q = 25 × 2 × (150 - 30)",
    "Step 3: Compute: Q = 25 × 2 × 120 = 6000 W"
  ],
  "final_answer": "Q = 6000 W (6 kW)",
  "key_insight": "Higher convective coefficient h means faster heat loss from the surface.",
  "formula": "Q = h × A × ΔT",
  "variables": [
    {"symbol": "Q", "name": "Heat loss rate", "value": "6000", "unit": "W", "color": "green"},
    {"symbol": "h", "name": "Convective coefficient", "value": "25", "unit": "W/m²·K", "color": "blue"},
    {"symbol": "A", "name": "Surface area", "value": "2", "unit": "m²", "color": "blue"},
    {"symbol": "ΔT", "name": "Temperature difference", "value": "120", "unit": "K", "color": "orange"}
  ],
  "substitution_chain": [
    {"num": 1, "eq": "Q = h × A × (Ts − T∞)"},
    {"num": 2, "eq": "Q = 25 × 2 × (150 − 30)"},
    {"num": 3, "eq": "Q = 25 × 2 × 120"},
    {"num": 4, "eq": "Q = 6000 W"}
  ]
}

Rules:
- steps: 3-6 numbered solution steps, each a complete sentence.
- final_answer: complete with value and unit.
- key_insight: one memorable sentence explaining WHY.
- formula: the governing equation in plain text.
- variables: all variables in the formula with symbol, full name, value from question, unit.
  color = "blue" for given data, "orange" for derived/intermediate, "green" for the answer.
- substitution_chain: 3-5 rows showing numeric substitution step by step.
- Pure JSON only — no markdown fences."""


class GeminiSolutionGenerator:
    _FALLBACK = {
        "steps": [
            "Step 1: Identify the governing formula from the problem domain.",
            "Step 2: Substitute the given numerical values.",
            "Step 3: Compute the result with correct units.",
        ],
        "final_answer": "See question for numerical values and units.",
        "key_insight": "Apply the governing formula with the given data.",
        "formula": "Formula from problem domain",
        "variables": [],
        "substitution_chain": [
            {"num": 1, "eq": "Apply the governing formula"},
            {"num": 2, "eq": "Substitute given values"},
            {"num": 3, "eq": "Compute the result"},
        ],
        "_used_fallback": True,
    }

    @classmethod
    def generate(cls, question: str) -> dict:
        if _gemini_client is None:
            return dict(cls._FALLBACK)
        MAX_ATTEMPTS = 3
        last_raw = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = cls._call_gemini(
                    f"Solve this problem step by step:\n\n{question[:1200]}",
                    _SOLUTION_SYSTEM_GEMINI,
                    max_tokens=3000,
                )
                last_raw = raw
                cleaned = _sanitize_json_str(raw)
                data = json.loads(cleaned)
                if data.get("steps") and data.get("final_answer"):
                    QAnimLogger.ok("SolutionGenerator", f"Solution generated (attempt {attempt}): {len(data['steps'])} steps")
                    return data
                raise ValueError("Missing required fields")
            except Exception as e:
                QAnimLogger.warn("SolutionGenerator", f"Attempt {attempt}/{MAX_ATTEMPTS} failed: {e}")
                if attempt < MAX_ATTEMPTS:
                    continue
        QAnimLogger.error("SolutionGenerator", f"All attempts failed — using fallback. Last raw: {last_raw[:200]!r}")
        return dict(cls._FALLBACK)

    @classmethod
    def _call_gemini(cls, user_prompt: str, system_prompt: str, max_tokens: int = 2000) -> str:
        import time as _time
        MAX_RETRIES  = 3
        RETRY_DELAYS = [10, 25, 50]
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if _GEMINI_SDK_STYLE == "generativeai":
                    model_obj = _gemini_client.GenerativeModel(
                        model_name=GEMINI_MODEL,
                        system_instruction=system_prompt,
                        generation_config={"temperature": 0.1, "max_output_tokens": max_tokens},
                    )
                    response = model_obj.generate_content(user_prompt)
                    return response.text.strip()
                else:
                    try:
                        config = _google_genai.types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            temperature=0.1,
                            max_output_tokens=max_tokens,
                            thinking_config=_google_genai.types.ThinkingConfig(thinking_level="low"),
                        )
                    except Exception:
                        config = _google_genai.types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            temperature=0.1,
                            max_output_tokens=max_tokens,
                        )
                    response = _gemini_client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=user_prompt,
                        config=config,
                    )
                    return response.text.strip()
            except Exception as e:
                err_str = str(e)
                is_retryable = (
                    "429" in err_str or "503" in err_str or "overloaded" in err_str.lower()
                    or "Resource has been exhausted" in err_str
                )
                if is_retryable and attempt < MAX_RETRIES:
                    _time.sleep(RETRY_DELAYS[attempt - 1])
                    continue
                raise
        raise RuntimeError("All retry attempts exhausted")

    @classmethod
    async def generate_async(cls, question: str) -> dict:
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, cls.generate, question),
                timeout=STAGE_TIMEOUT_SMALL,
            )
        except asyncio.TimeoutError:
            QAnimLogger.error("SolutionGenerator", f"Stage exceeded {STAGE_TIMEOUT_SMALL}s — using fallback")
            return dict(cls._FALLBACK)


# ===========================================================================
#  MODULE 9 — GeminiSceneAnalyzer (9-Step Workflow Contract)
# ===========================================================================

_SCENE_ANALYZER_SYSTEM = """You are QAnim Scene Analyzer v2.0 — an educational animation director.

Given a student question, produce a structured animation scene script in JSON that implements the EXACT 9-step workflow shown below. This workflow mirrors the Convective Heat Loss reference animation style.

═══════════════════════════════════════════════════════
THE 9-STEP WORKFLOW CONTRACT
═══════════════════════════════════════════════════════

STEPS 1–6: SVG Concept Animation (Physical Scene)
  • Each step introduces EXACTLY ONE new physical element (parameter, component, or derived quantity).
  • Steps 1–5: Show given/derived quantities one at a time, building up the scene.
    - Step 1: Establish the environment/medium/reference frame. Show one given datum.
    - Step 2: Introduce the main object/system. Show another given datum.
    - Step 3: Show the primary given quantity (temperature, force, concentration, etc.).
    - Step 4: Show another given quantity (coefficient, area, length, etc.).
    - Step 5: Show derived intermediate quantity (temperature difference, displacement, etc.).
  • Step 6: The "Setup Summary + To Find" step:
    - Left callout: all given data accumulated.
    - Right callout: the unknown we must find (Q=?, F=?, etc.) with the governing formula hinted.
    - Description: states "All given data is set up — proceed to the formula."
  • NEVER put formulas, substitution boxes, or "solve" language in Steps 1–6 descriptions.

STEP 7: Main Formula (formula_data)
  • Display the governing formula in large text (e.g. Q = h·A·(Ts − T∞)).
  • Reveal each variable box one at a time with: symbol, full name, value, unit, accent color.
  • End with a key insight/note bar.

STEP 8: Step-by-Step Substitution (substitution_data)
  • Left panel: physical system visual description + system title.
  • Right panel: given parameters list + solution approach steps + formula result bar.
  • The result bar shows the final formula with numbers substituted and the answer.

STEP 9: Final Answer (final_answer_data)
  • Formula recap line (the governing equation).
  • Substitution chain rows (numbered, e.g. "Q = 25 × 2 × 120 = 6000 W").
  • Big result box: value highlighted, unit shown.
  • Insight bar: one memorable sentence about WHY the answer makes sense.

═══════════════════════════════════════════════════════
SVG SCENE DESIGN RULES FOR STEPS 1–6
═══════════════════════════════════════════════════════
• viewBox 850×478, light airy background, radial gradient blue-white.
• Each step: ONE new svg-layer becomes visible (opacity 0→1).
• blur-shield dims prior layers (opacity 0.38) to spotlight the new element.
• Step 6 (final animation step): blur-shield off (opacity 0), show all layers + both callout overlays.
• Layer types: ambient medium, main object, primary parameter glow, flow/current/motion, derived quantity, setup summary.
• Overlay callouts: colored header bar + symbol + value displayed in rounded rect chips.
• Colors: cyan #0891b2 for given data, orange #d97706 for derived/intermediate, green #22c55e for the answer.

═══════════════════════════════════════════════════════
OUTPUT FORMAT — Return ONLY valid JSON (no markdown)
═══════════════════════════════════════════════════════
{
  "title": "Concise title (max 60 chars)",
  "topic": "PHYSICS|ENGINEERING|CHEMISTRY|MATH|BIOLOGY",
  "steps": [
    {
      "step_number": 1,
      "label": "3-5 word pill label",
      "title": "Step 1: Full descriptive title",
      "description": "2-3 conversational sentences. What we see, what it means, what to notice. NO formula exposition here.",
      "badges": [{"text": "Symbol = value unit", "type": "cyan"}],
      "components_visible": ["layer-frame"],
      "components_new": ["layer-frame"],
      "focus_component": "layer-frame",
      "blur_background": false
    },
    ... (steps 2-5 follow same pattern, blur_background: true for steps 2-5)
    {
      "step_number": 6,
      "label": "Setup Complete",
      "title": "Step 6: Summary — To Find",
      "description": "All given data is now assembled. The unknown [quantity] awaits calculation. Proceed to the formula.",
      "badges": [
        {"text": "param1 = val1 unit1", "type": "cyan"},
        {"text": "param2 = val2 unit2", "type": "cyan"},
        {"text": "derived = val unit", "type": "orange"},
        {"text": "Q = ?", "type": "green"}
      ],
      "components_visible": ["layer-frame", "layer-comp1", "layer-comp2", "layer-comp3", "layer-comp4", "layer-comp5"],
      "components_new": [],
      "focus_component": null,
      "blur_background": false
    }
  ],
  "svg_components": {
    "layer-frame": {
      "description": "Fixed background structure: [describe what the frame looks like for this problem]",
      "motion_type": "static",
      "accent_color": "#4a6a8a",
      "layer_order": 1,
      "labels": ["environment label"]
    },
    "layer-comp1": {
      "description": "The main object: [describe its shape, position in 850x478 space, fill]",
      "motion_type": "static|flow|pulse|rotate",
      "accent_color": "#0891b2",
      "layer_order": 2,
      "labels": ["object name", "value label"]
    }
  },
  "formula_data": {
    "formula_text": "Q = h × A × (Ts − T∞)",
    "formula_sublabel": "Newton's Law of Cooling",
    "variables": [
      {"symbol": "Q", "name": "Heat loss rate", "value": "? (to find)", "unit": "W", "color": "green"},
      {"symbol": "h", "name": "Convective coefficient", "value": "25", "unit": "W/m²·K", "color": "blue"},
      {"symbol": "A", "name": "Surface area", "value": "2", "unit": "m²", "color": "blue"},
      {"symbol": "ΔT", "name": "Temperature difference", "value": "120", "unit": "K", "color": "orange"}
    ],
    "note_text": "💡 Key: larger h or larger ΔT means faster heat loss."
  },
  "substitution_data": {
    "system_title": "Metal Plate in Air Flow",
    "system_description": "Hot plate at Ts=150°C losing heat to surrounding air at T∞=30°C via forced convection",
    "given_list": [
      "T∞ = 30°C (Ambient air temperature)",
      "A = 2 m² (Plate surface area)",
      "Ts = 150°C (Plate surface temperature)",
      "h = 25 W/m²·K (Convective coefficient)"
    ],
    "approach_steps": [
      "Use Newton's Law of Cooling: Q = h × A × (Ts − T∞)",
      "Calculate ΔT = Ts − T∞ = 150 − 30 = 120 K",
      "Substitute all values and compute Q"
    ],
    "result_bar": "Q = 25 × 2 × 120 = 6000 W = 6 kW"
  },
  "final_answer_data": {
    "formula_recap": "Q = h × A × (Ts − T∞)",
    "substitution_chain": [
      {"num": 1, "eq": "Q = h × A × (Ts − T∞)"},
      {"num": 2, "eq": "Q = 25 × 2 × (150 − 30)"},
      {"num": 3, "eq": "Q = 25 × 2 × 120"},
      {"num": 4, "eq": "Q = 6000 W"}
    ],
    "answer_value": "6000",
    "answer_unit": "W",
    "answer_highlight": "6000",
    "insight_text": "The plate loses heat at <strong>6 kW</strong> — doubling h or the plate area would double the heat loss.",
    "to_find_label": "Heat loss from the plate"
  },
  "final_answer": "Q = 6000 W (6 kW)",
  "key_insight": "Convective heat loss is proportional to both the temperature difference and the surface area."
}

═══════════════════════════════════════════════════════
STRICT RULES
═══════════════════════════════════════════════════════
1. EXACTLY 6 SVG steps in the "steps" array (Steps 1–6). Not 5, not 7. Always 6.
2. Step 6 must have blur_background: false and show ALL components_visible (all layers).
3. Steps 2–5 must have blur_background: true.
4. formula_data, substitution_data, final_answer_data are REQUIRED — never omit.
5. Every badge text must use HTML-entity &#39; instead of apostrophe ' for any prime notation.
6. svg_components must list EVERY layer-id that appears in any step's components_visible.
7. descriptions MUST NOT mention formulas, derivations, or solution steps — those belong only in formula_data, substitution_data, final_answer_data.
8. Adapt fully to the question's domain: heat transfer, mechanics, circuits, chemistry, optics, etc.
9. Return PURE JSON only — no markdown, no fences, no preamble."""


class GeminiSceneAnalyzer:
    @classmethod
    def analyze(cls, question: str) -> dict:
        if _gemini_client is None:
            return cls._fallback_script(question)
        MAX_ATTEMPTS = 3
        last_raw = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                QAnimLogger.info("SceneAnalyzer", f"Attempt {attempt}/{MAX_ATTEMPTS}...")
                raw = GeminiSolutionGenerator._call_gemini(
                    f"Produce the complete 9-step animation scene script for this question:\n\n{question[:1500]}",
                    _SCENE_ANALYZER_SYSTEM,
                    max_tokens=8000,
                )
                last_raw = raw
                cleaned = _sanitize_json_str(raw)
                data = json.loads(cleaned)
                steps = data.get("steps", [])
                if len(steps) < 4:
                    raise ValueError(f"Too few steps ({len(steps)})")
                if not data.get("formula_data"):
                    raise ValueError("Missing formula_data")
                if not data.get("final_answer_data"):
                    raise ValueError("Missing final_answer_data")
                QAnimLogger.ok("SceneAnalyzer", f"Scene script parsed: {len(steps)} steps (attempt {attempt})")
                return data
            except Exception as e:
                QAnimLogger.warn("SceneAnalyzer", f"Attempt {attempt} failed: {e}. Raw: {last_raw[:200]!r}")
                if attempt < MAX_ATTEMPTS:
                    import time as _t
                    _t.sleep(15 * attempt)
                    continue
        QAnimLogger.error("SceneAnalyzer", "All attempts failed — using fallback script")
        return cls._fallback_script(question)

    @classmethod
    def _fallback_script(cls, question: str) -> dict:
        short_q = question[:60]
        return {
            "title": short_q,
            "topic": "ENGINEERING",
            "steps": [
                {
                    "step_number": 1,
                    "label": "Environment",
                    "title": "Step 1: The Surrounding Environment",
                    "description": "We begin by establishing the surrounding environment and initial conditions for this problem.",
                    "badges": [{"text": "Given: see problem", "type": "cyan"}],
                    "components_visible": ["layer-frame"],
                    "components_new": ["layer-frame"],
                    "focus_component": "layer-frame",
                    "blur_background": False,
                },
                {
                    "step_number": 2,
                    "label": "Main Object",
                    "title": "Step 2: The Main System",
                    "description": "The primary object or system is introduced. It has defined physical properties relevant to this problem.",
                    "badges": [{"text": "System: defined", "type": "cyan"}],
                    "components_visible": ["layer-frame", "layer-object"],
                    "components_new": ["layer-object"],
                    "focus_component": "layer-object",
                    "blur_background": True,
                },
                {
                    "step_number": 3,
                    "label": "Primary Value",
                    "title": "Step 3: Primary Given Quantity",
                    "description": "The primary given quantity is identified. This is a key input to the governing formula.",
                    "badges": [{"text": "Given: value", "type": "orange"}],
                    "components_visible": ["layer-frame", "layer-object", "layer-primary"],
                    "components_new": ["layer-primary"],
                    "focus_component": "layer-primary",
                    "blur_background": True,
                },
                {
                    "step_number": 4,
                    "label": "Secondary Value",
                    "title": "Step 4: Secondary Given Quantity",
                    "description": "Another given quantity is added. Together with the primary value, this enables us to apply the formula.",
                    "badges": [{"text": "Given: coefficient", "type": "cyan"}],
                    "components_visible": ["layer-frame", "layer-object", "layer-primary", "layer-secondary"],
                    "components_new": ["layer-secondary"],
                    "focus_component": "layer-secondary",
                    "blur_background": True,
                },
                {
                    "step_number": 5,
                    "label": "Derived Quantity",
                    "title": "Step 5: Derived Intermediate Quantity",
                    "description": "From the given data we derive an intermediate quantity. This is calculated directly from the given values.",
                    "badges": [{"text": "Derived: ΔQ", "type": "orange"}],
                    "components_visible": ["layer-frame", "layer-object", "layer-primary", "layer-secondary", "layer-derived"],
                    "components_new": ["layer-derived"],
                    "focus_component": "layer-derived",
                    "blur_background": True,
                },
                {
                    "step_number": 6,
                    "label": "Setup Complete",
                    "title": "Step 6: Setup Summary — To Find",
                    "description": "All given data is assembled. Our goal is to find the unknown quantity using the governing formula. All given data is now set up — proceed to the formula steps.",
                    "badges": [
                        {"text": "All given data", "type": "cyan"},
                        {"text": "Unknown = ?", "type": "green"},
                    ],
                    "components_visible": ["layer-frame", "layer-object", "layer-primary", "layer-secondary", "layer-derived", "layer-summary"],
                    "components_new": ["layer-summary"],
                    "focus_component": None,
                    "blur_background": False,
                },
            ],
            "svg_components": {
                "layer-frame": {
                    "description": "Background environment: light blue gradient, reference grid, axis labels",
                    "motion_type": "static",
                    "accent_color": "#4a6a8a",
                    "layer_order": 1,
                    "labels": ["Environment"],
                },
                "layer-object": {
                    "description": "Main physical object: centered rectangle with metallic fill",
                    "motion_type": "static",
                    "accent_color": "#0891b2",
                    "layer_order": 2,
                    "labels": ["System"],
                },
                "layer-primary": {
                    "description": "Primary quantity visualization: glow effect + callout card",
                    "motion_type": "pulse",
                    "accent_color": "#d97706",
                    "layer_order": 3,
                    "labels": ["Primary value"],
                },
                "layer-secondary": {
                    "description": "Secondary quantity: flow arrows or coefficient callout",
                    "motion_type": "flow",
                    "accent_color": "#0891b2",
                    "layer_order": 4,
                    "labels": ["Coefficient"],
                },
                "layer-derived": {
                    "description": "Derived intermediate quantity: gradient bar + delta label",
                    "motion_type": "static",
                    "accent_color": "#d97706",
                    "layer_order": 5,
                    "labels": ["ΔQ"],
                },
                "layer-summary": {
                    "description": "Setup summary card (left) and To Find card (right)",
                    "motion_type": "static",
                    "accent_color": "#7c3aed",
                    "layer_order": 6,
                    "labels": ["Given", "To Find"],
                },
            },
            "formula_data": {
                "formula_text": "Result = f(given values)",
                "formula_sublabel": "Governing Equation",
                "variables": [
                    {"symbol": "R", "name": "Result", "value": "? (to find)", "unit": "", "color": "green"},
                    {"symbol": "A", "name": "Parameter A", "value": "value", "unit": "unit", "color": "blue"},
                ],
                "note_text": "💡 Apply the governing formula with the given data.",
            },
            "substitution_data": {
                "system_title": "Physical System",
                "system_description": short_q,
                "given_list": ["Given values from the problem"],
                "approach_steps": [
                    "Identify the governing formula",
                    "Substitute the given values",
                    "Compute the result",
                ],
                "result_bar": "Result = computed value",
            },
            "final_answer_data": {
                "formula_recap": "Result = f(given values)",
                "substitution_chain": [
                    {"num": 1, "eq": "Apply governing formula"},
                    {"num": 2, "eq": "Substitute values"},
                    {"num": 3, "eq": "Compute result"},
                ],
                "answer_value": "Result",
                "answer_unit": "units",
                "answer_highlight": "Result",
                "insight_text": "Apply the governing formula with the given data to find the answer.",
                "to_find_label": "Unknown quantity",
            },
            "final_answer": "See calculation above",
            "key_insight": "Apply the governing formula with the given data.",
        }

    @classmethod
    async def analyze_async(cls, question: str) -> dict:
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, cls.analyze, question),
                timeout=STAGE_TIMEOUT_SCENE,
            )
        except asyncio.TimeoutError:
            QAnimLogger.error("SceneAnalyzer", f"Stage exceeded {STAGE_TIMEOUT_SCENE}s — using fallback")
            return cls._fallback_script(question)


# ===========================================================================
#  MODULE 10 — Panel Injection Helpers
# ===========================================================================

def _build_answer_targets(to_find_targets, gemini_sol, final_answer, key_insight):
    targets = []
    sol = gemini_sol or {}
    final = final_answer or sol.get("final_answer", "")
    insight = key_insight or sol.get("key_insight", "")
    nums = re.findall(r'[-+]?\d+(?:\.\d+)?', final)
    main_val = nums[-1] if nums else ""
    if to_find_targets:
        label = to_find_targets[0]
    else:
        label = "Final Answer"
    targets.append({
        "label": label,
        "value": main_val or final[:60],
        "insight": insight[:200] if insight else "See solution above.",
    })
    return targets


# ===========================================================================
#  MODULE 11 — Scene 6 (Step 7: Main Formula) — Full Redesign
# ===========================================================================

_SCENE7_STYLES = """\
<style id="qanim-scene7-styles">
/* ── Fonts ── */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700;800&display=swap');

/* ── Backdrop ── */
#qanim-scene-modal-backdrop{
  display:none;position:fixed;inset:0;z-index:7400;
  background:rgba(10,15,30,.55);
  backdrop-filter:blur(8px) saturate(1.4);
  -webkit-backdrop-filter:blur(8px) saturate(1.4);
  opacity:0;transition:opacity .3s ease;
}
#qanim-scene-modal-backdrop.qanim-scene-visible{display:block!important;opacity:1;}

/* ── Overlay shell ── */
#qanim-scene7-overlay{
  display:none;position:fixed;
  top:50%;left:50%;
  transform:translate(-50%,-50%) scale(.93) translateY(12px);
  z-index:7500;
  width:min(880px,96vw);max-height:94vh;overflow-y:auto;
  box-sizing:border-box;opacity:0;pointer-events:none;
  transition:opacity .35s cubic-bezier(.4,0,.2,1),
             transform .4s cubic-bezier(.34,1.28,.64,1);
  scrollbar-width:thin;scrollbar-color:#c7d2fe #f1f5f9;
}
#qanim-scene7-overlay::-webkit-scrollbar{width:5px;}
#qanim-scene7-overlay::-webkit-scrollbar-track{background:#f1f5f9;}
#qanim-scene7-overlay::-webkit-scrollbar-thumb{background:#c7d2fe;border-radius:4px;}
#qanim-scene7-overlay.qanim-scene-visible{
  display:block!important;opacity:1;pointer-events:auto;
  transform:translate(-50%,-50%) scale(1) translateY(0);
}

/* ── Card ── */
.s7-card{
  background:#fff;border-radius:24px;overflow:hidden;
  box-shadow:0 24px 80px rgba(29,78,216,.14),0 4px 16px rgba(0,0,0,.07);
  border:1px solid #e0e7ff;
  font-family:'Inter',system-ui,-apple-system,sans-serif;
}

/* ── Header ── */
.s7-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 28px;height:58px;
  background:linear-gradient(90deg,#1e3a8a 0%,#1d4ed8 50%,#2563eb 100%);
  position:relative;overflow:hidden;
}
.s7-header::after{
  content:'';position:absolute;inset:0;
  background:url("data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%23ffffff' fill-opacity='0.04'%3E%3Ccircle cx='30' cy='30' r='28'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E") repeat;
  pointer-events:none;
}
.s7-header-left{display:flex;align-items:center;gap:10px;z-index:1;}
.s7-step-badge{
  background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.3);
  border-radius:8px;padding:3px 10px;
  font-size:10.5px;font-weight:800;letter-spacing:1.5px;color:#bfdbfe;
  text-transform:uppercase;
}
.s7-header-title{font-size:15px;font-weight:700;color:#fff;letter-spacing:-0.2px;}
.s7-header-right{z-index:1;}
.s7-progress-text{font-size:11px;font-weight:700;color:#93c5fd;letter-spacing:.5px;}

/* ── Body ── */
.s7-body{
  padding:28px 32px 24px;
  background:linear-gradient(160deg,#f8faff 0%,#eef2ff 40%,#f0f9ff 100%);
}

/* ── Phase caption row ── */
.s7-phase-row{
  display:flex;align-items:center;justify-content:space-between;
  margin-bottom:18px;min-height:22px;
}
.s7-phase-caption{
  font-size:13.5px;font-weight:600;color:#475569;line-height:1.5;
  flex:1;padding-right:16px;
}
.s7-phase-caption .s7-accent{color:#1d4ed8;font-weight:800;}
.s7-phase-dots{display:flex;gap:5px;flex-shrink:0;}
.s7-phase-dot{
  width:8px;height:8px;border-radius:50%;
  background:#dde3f0;transition:background .3s,transform .25s;
}
.s7-phase-dot.active{background:#1d4ed8;transform:scale(1.3);}
.s7-phase-dot.done{background:#93c5fd;}

/* ── Formula box ── */
.s7-formula-box{
  background:#fff;
  border:2px solid #bfdbfe;border-radius:16px;
  padding:22px 28px 18px;text-align:center;
  margin-bottom:22px;
  box-shadow:0 2px 12px rgba(29,78,216,.07);
  position:relative;overflow:hidden;
}
.s7-formula-box::before{
  content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:linear-gradient(90deg,#6366f1,#3b82f6,#0ea5e9);
}
.s7-formula-label{
  font-size:9.5px;font-weight:800;letter-spacing:2px;
  text-transform:uppercase;color:#94a3b8;margin-bottom:12px;
}
.s7-formula-main{
  font-family:'JetBrains Mono','Courier New',monospace;
  font-size:30px;font-weight:700;color:#1e40af;
  letter-spacing:1.5px;line-height:1.35;
  opacity:0;transform:translateY(10px);
  transition:opacity .55s cubic-bezier(.4,0,.2,1),
             transform .55s cubic-bezier(.34,1.2,.64,1);
}
.s7-formula-main.s7-shown{opacity:1;transform:translateY(0);}
.s7-formula-sublabel{
  font-size:12px;font-weight:600;color:#6366f1;
  letter-spacing:.3px;margin-top:10px;
  opacity:0;transition:opacity .5s ease .25s;
}
.s7-formula-sublabel.s7-shown{opacity:1;}

/* ── Variable cards row ── */
.s7-vars-row{
  display:flex;align-items:flex-start;justify-content:center;
  gap:10px;flex-wrap:wrap;
}

/* ── Individual variable card ── */
.s7-var-box{
  display:flex;flex-direction:column;align-items:center;
  min-width:126px;max-width:160px;flex:1;
  opacity:0;transform:translateY(16px) scale(.97);
  transition:opacity .4s cubic-bezier(.4,0,.2,1),
             transform .4s cubic-bezier(.34,1.4,.64,1);
}
.s7-var-box.s7-shown{opacity:1;transform:translateY(0) scale(1);}

.s7-var-connector{
  width:2px;height:18px;border-radius:2px;
  flex-shrink:0;margin-bottom:0;
  opacity:0;transition:opacity .3s ease .1s;
}
.s7-var-box.s7-shown .s7-var-connector{opacity:1;}
.s7-var-connector-dot{
  width:8px;height:8px;border-radius:50%;
  margin:0 auto -4px;flex-shrink:0;
  opacity:0;transition:opacity .3s ease .15s;
}
.s7-var-box.s7-shown .s7-var-connector-dot{opacity:1;}

.s7-var-inner{
  border:2px solid;border-radius:14px;
  padding:14px 14px 12px;text-align:center;
  width:100%;box-sizing:border-box;
  transition:box-shadow .3s,transform .25s cubic-bezier(.34,1.3,.64,1);
}
.s7-var-box.s7-active .s7-var-inner{
  box-shadow:0 0 0 3.5px rgba(59,130,246,.25),
             0 6px 24px rgba(29,78,216,.18);
  transform:scale(1.06) translateY(-2px);
}

.s7-var-sym{
  font-family:'JetBrains Mono','Courier New',monospace;
  font-size:24px;font-weight:800;line-height:1;
  display:block;margin-bottom:6px;letter-spacing:.5px;
}
.s7-var-name{
  font-family:'Inter',sans-serif;
  font-size:11px;font-weight:600;color:#64748b;
  line-height:1.4;display:block;
}
.s7-var-val-chip{
  display:inline-flex;align-items:center;gap:4px;
  margin-top:8px;padding:3px 8px;border-radius:20px;
  font-family:'JetBrains Mono','Courier New',monospace;
  font-size:11px;font-weight:700;
}
.s7-var-val-num{font-size:12.5px;}
.s7-var-val-unit{font-size:10px;font-weight:600;opacity:.8;}

.s6v-blue  .s7-var-inner{border-color:#3b82f6;background:linear-gradient(145deg,#eff6ff,#dbeafe);}
.s6v-blue  .s7-var-sym{color:#1d4ed8;}
.s6v-blue  .s7-var-connector{background:#3b82f6;}
.s6v-blue  .s7-var-connector-dot{background:#3b82f6;}
.s6v-blue  .s7-var-val-chip{background:#dbeafe;color:#1d4ed8;border:1px solid #bfdbfe;}

.s6v-green .s7-var-inner{border-color:#22c55e;background:linear-gradient(145deg,#f0fdf4,#dcfce7);}
.s6v-green .s7-var-sym{color:#15803d;}
.s6v-green .s7-var-connector{background:#22c55e;}
.s6v-green .s7-var-connector-dot{background:#22c55e;}
.s6v-green .s7-var-val-chip{background:#dcfce7;color:#15803d;border:1px solid #bbf7d0;}

.s6v-orange .s7-var-inner{border-color:#f59e0b;background:linear-gradient(145deg,#fffbeb,#fef3c7);}
.s6v-orange .s7-var-sym{color:#b45309;}
.s6v-orange .s7-var-connector{background:#f59e0b;}
.s6v-orange .s7-var-connector-dot{background:#f59e0b;}
.s6v-orange .s7-var-val-chip{background:#fef3c7;color:#92400e;border:1px solid #fde68a;}

.s6v-red    .s7-var-inner{border-color:#f43f5e;background:linear-gradient(145deg,#fff1f2,#ffe4e6);}
.s6v-red    .s7-var-sym{color:#be123c;}
.s6v-red    .s7-var-connector{background:#f43f5e;}
.s6v-red    .s7-var-connector-dot{background:#f43f5e;}
.s6v-red    .s7-var-val-chip{background:#ffe4e6;color:#be123c;border:1px solid #fecdd3;}

.s6v-purple .s7-var-inner{border-color:#a855f7;background:linear-gradient(145deg,#faf5ff,#f3e8ff);}
.s6v-purple .s7-var-sym{color:#7c3aed;}
.s6v-purple .s7-var-connector{background:#a855f7;}
.s6v-purple .s7-var-connector-dot{background:#a855f7;}
.s6v-purple .s7-var-val-chip{background:#f3e8ff;color:#6d28d9;border:1px solid #e9d5ff;}

.s6v-teal  .s7-var-inner{border-color:#14b8a6;background:linear-gradient(145deg,#f0fdfa,#ccfbf1);}
.s6v-teal  .s7-var-sym{color:#0f766e;}
.s6v-teal  .s7-var-connector{background:#14b8a6;}
.s6v-teal  .s7-var-connector-dot{background:#14b8a6;}
.s6v-teal  .s7-var-val-chip{background:#ccfbf1;color:#0f766e;border:1px solid #99f6e4;}

/* ── Insight / note bar ── */
.s7-note-bar{
  display:flex;align-items:flex-start;gap:12px;
  margin-top:22px;padding:14px 20px;
  background:linear-gradient(135deg,#fffbeb,#fef9c3);
  border-radius:14px;border:1.5px solid #fde68a;
  box-shadow:0 2px 8px rgba(245,158,11,.10);
  opacity:0;transform:translateY(10px);
  transition:opacity .5s ease,transform .5s ease;
}
.s7-note-bar.s7-shown{opacity:1;transform:translateY(0);}
.s7-note-icon{font-size:20px;flex-shrink:0;margin-top:1px;}
.s7-note-content{}
.s7-note-label{
  font-size:9px;font-weight:800;letter-spacing:2px;
  text-transform:uppercase;color:#a16207;margin-bottom:3px;
}
.s7-note-text{font-size:13px;font-weight:500;color:#78350f;line-height:1.6;}

/* ── Navigation row ── */
.s7-nav-row{
  display:flex;justify-content:space-between;align-items:center;
  gap:12px;padding:16px 28px 20px;
  border-top:1px solid #e8eef8;background:#fdfdff;
}
.s7-btn{
  display:inline-flex;align-items:center;gap:7px;
  padding:10px 22px;border-radius:10px;
  font-family:'Inter',sans-serif;font-size:13.5px;font-weight:700;
  cursor:pointer;border:none;transition:all .18s ease;letter-spacing:.1px;
}
.s7-btn-back{
  background:#f1f5f9;color:#64748b;
  border:1.5px solid #cbd5e1!important;
}
.s7-btn-back:hover{background:#e2e8f0;color:#334155;transform:translateX(-2px);}
.s7-btn-next{
  background:linear-gradient(135deg,#1d4ed8,#3b82f6);
  color:#fff;box-shadow:0 4px 14px rgba(29,78,216,.28);
}
.s7-btn-next:hover{box-shadow:0 6px 20px rgba(29,78,216,.38);transform:translateY(-1px);}
.s7-btn-finish{
  background:linear-gradient(135deg,#6d28d9,#7c3aed);
  color:#fff;box-shadow:0 4px 14px rgba(109,40,217,.28);
}
.s7-btn-finish:hover{box-shadow:0 6px 20px rgba(109,40,217,.38);transform:translateY(-1px);}
</style>"""


def _build_scene7_html(formula_data: dict) -> str:
    formula_text    = html_module.escape(formula_data.get("formula_text", "Formula"))
    formula_sublabel= html_module.escape(formula_data.get("formula_sublabel", "Governing Equation"))
    note_text       = formula_data.get("note_text", "")
    variables       = formula_data.get("variables", [])

    color_map = {
        "blue":   "s6v-blue",  "cyan":   "s6v-teal",   "orange": "s6v-orange",
        "green":  "s6v-green", "red":    "s6v-red",     "purple": "s6v-purple",
        "teal":   "s6v-teal",  "indigo": "s6v-blue",   "amber":  "s6v-orange",
        "rose":   "s6v-red",   "violet": "s6v-purple",
    }

    var_boxes_html = ""
    for v in variables:
        sym        = html_module.escape(v.get("symbol", "?"))
        name       = html_module.escape(v.get("name", "Variable"))
        val        = html_module.escape(v.get("value", ""))
        unit       = html_module.escape(v.get("unit", ""))
        color_cls  = color_map.get(v.get("color", "blue"), "s6v-blue")
        chip_html  = ""
        if val:
            chip_html = (
                f'<div class="s7-var-val-chip">'
                f'<span class="s7-var-val-num">{val}</span>'
                + (f'<span class="s7-var-val-unit">{unit}</span>' if unit else "")
                + f'</div>'
            )
        var_boxes_html += f"""
          <div class="s7-var-box {color_cls}">
            <div class="s7-var-connector"></div>
            <div class="s7-var-connector-dot"></div>
            <div class="s7-var-inner">
              <span class="s7-var-sym">{sym}</span>
              <span class="s7-var-name">{name}</span>
              {chip_html}
            </div>
          </div>"""

    n_vars = len(variables)
    dots_html = "".join(
        f'<div class="s7-phase-dot" id="s7-dot-{i}"></div>'
        for i in range(n_vars + 1)
    )

    note_html = ""
    if note_text:
        note_html = f"""
      <div class="s7-note-bar" id="s7-note-bar">
        <div class="s7-note-icon">&#x1F4A1;</div>
        <div class="s7-note-content">
          <div class="s7-note-label">Key Insight</div>
          <div class="s7-note-text">{html_module.escape(note_text)}</div>
        </div>
      </div>"""

    return f"""
<div id="qanim-scene-modal-backdrop"></div>
<div id="qanim-scene7-overlay" role="dialog" aria-modal="true" aria-label="Step 7: Main Formula">
  <div class="s7-card">
    <div class="s7-header">
      <div class="s7-header-left">
        <div class="s7-step-badge">Step 7 of 9</div>
        <div class="s7-header-title">Main Formula</div>
      </div>
      <div class="s7-header-right">
        <div class="s7-progress-text" id="s7-phase-progress">Formula</div>
      </div>
    </div>
    <div class="s7-body">
      <div class="s7-phase-row">
        <div class="s7-phase-caption" id="s7-phase-caption">
          This is the <span class="s7-accent">governing formula</span>
          for this problem. Click <strong>Next</strong> to explore each variable.
        </div>
        <div class="s7-phase-dots" id="s7-phase-dots">{dots_html}</div>
      </div>
      <div class="s7-formula-box">
        <div class="s7-formula-label">Governing Equation</div>
        <div class="s7-formula-main" id="s7-formula-text">{formula_text}</div>
        <div class="s7-formula-sublabel" id="s7-formula-sublabel">{formula_sublabel}</div>
      </div>
      <div class="s7-vars-row" id="s7-vars-row">{var_boxes_html}</div>
      {note_html}
    </div>
    <div class="s7-nav-row">
      <button class="s7-btn s7-btn-back" onclick="qanim_goToPrevScene()">
        &#9664; Back to Animation
      </button>
      <button class="s7-btn s7-btn-next" id="s7-next-btn" onclick="qanim_s7Advance()">
        Next Variable &#9654;
      </button>
    </div>
  </div>
</div>"""


_SCENE7_JS = """\
<script id="qanim-js-scene7">
(function initScene7(){
  'use strict';
  if(window.__qanimScene7Init)return;window.__qanimScene7Init=true;
  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _varBoxes(){return document.querySelectorAll('#s7-vars-row .s7-var-box');}
  function _dots(){return document.querySelectorAll('#s7-phase-dots .s7-phase-dot');}

  var s6Phase=-1;
  var s6AutoTimer=null;

  function _buildCaptions(){
    var boxes=_varBoxes();
    var caps=[ 'This is the <span class="s7-accent">governing formula</span> for this problem. Click <strong>Next</strong> to explore each variable.' ];
    boxes.forEach(function(b){
      var sym=b.querySelector('.s7-var-sym');
      var nm=b.querySelector('.s7-var-name');
      var chip=b.querySelector('.s7-var-val-chip');
      var symT=sym?sym.textContent.trim():'';
      var nmT=nm?nm.textContent.trim():'';
      var chipT=chip?chip.textContent.trim():'';
      var txt='<span class="s7-accent">'+symT+'</span> &mdash; <strong>'+nmT+'</strong>'+(chipT?' &ensp;<span style="font-family:\'JetBrains Mono\',monospace;font-size:12px;background:#f1f5f9;padding:2px 7px;border-radius:6px;">'+chipT+'</span>':'')+ '.';
      caps.push(txt);
    });
    caps.push('All variables identified. The key insight is shown below. Click to proceed to the substitution step.');
    return caps;
  }

  function s6Render(){
    var boxes=_varBoxes();
    var n=boxes.length;
    var caps=_buildCaptions();
    var dots=_dots();

    var fEl=_el('s7-formula-text');var sEl=_el('s7-formula-sublabel');
    if(fEl)fEl.classList.add('s7-shown');
    if(sEl)sEl.classList.add('s7-shown');

    for(var i=0;i<n;i++){
      var b=boxes[i];
      if(s6Phase>=i+1) b.classList.add('s7-shown'); else b.classList.remove('s7-shown');
      b.classList.toggle('s7-active', s6Phase===i+1);
    }

    for(var d=0;d<dots.length;d++){
      dots[d].classList.remove('active','done');
      if(d<s6Phase) dots[d].classList.add('done');
      if(d===s6Phase) dots[d].classList.add('active');
    }

    var noteEl=_el('s7-note-bar');
    var showNote=(s6Phase>=n+1);
    if(noteEl) noteEl.classList.toggle('s7-shown',showNote);

    var capEl=_el('s7-phase-caption');
    var capIdx=Math.max(0,Math.min(s6Phase<0?0:s6Phase, caps.length-1));
    if(capEl) capEl.innerHTML=caps[capIdx]||caps[0];

    var progEl=_el('s7-phase-progress');
    if(progEl){
      if(s6Phase<=0) progEl.textContent='Formula';
      else if(s6Phase<=n) progEl.textContent='Variable '+s6Phase+' / '+n;
      else progEl.textContent='Insight';
    }

    var nb=_el('s7-next-btn');
    if(nb){
      if(s6Phase<n){
        nb.innerHTML='Next Variable &#9654;';
        nb.className='s7-btn s7-btn-next';
        nb.onclick=function(){window.qanim_s7Advance();};
      } else if(s6Phase===n){
        nb.innerHTML='See Insight &#9654;';
        nb.className='s7-btn s7-btn-next';
        nb.onclick=function(){window.qanim_s7Advance();};
      } else {
        nb.innerHTML='Step 8: Substitution &#9654;';
        nb.className='s7-btn s7-btn-finish';
        nb.onclick=function(){window.qanim_showScene8();};
        if(!s6AutoTimer){
          s6AutoTimer=setTimeout(function(){
            var ov=_el('qanim-scene7-overlay');
            if(ov&&ov.classList.contains('qanim-scene-visible')) window.qanim_showScene8();
          },3200);
        }
      }
    }
  }

  window.qanim_s7Advance=function(){
    var n=_varBoxes().length;
    if(s6Phase<n+1) s6Phase++;
    s6Render();
  };

  function _cancelRAF(){
    if(typeof window.qanimRafId!=='undefined'&&window.qanimRafId){cancelAnimationFrame(window.qanimRafId);window.qanimRafId=null;}
    if(typeof window.rafId!=='undefined'&&window.rafId){cancelAnimationFrame(window.rafId);window.rafId=null;}
  }
  function _resumeRAF(){
    if(typeof window.qanimStartRAF==='function'){window.qanimStartRAF();return;}
    if(typeof window.startRAF==='function'){window.startRAF();return;}
    if(typeof window.animate==='function'){requestAnimationFrame(window.animate);}
  }
  function _syncDots7(){
    var dots=document.querySelectorAll('.step-dot');
    for(var i=0;i<dots.length;i++){dots[i].classList.remove('active','done');if(i<6)dots[i].classList.add('done');if(i===6)dots[i].classList.add('active');}
    var lbl=_el('step-label');if(lbl)lbl.innerText='Step 7 of 9: Main Formula';
    var bar=_el('step-bar');if(bar)bar.style.width=Math.round(7/9*100)+'%';
  }

  window.qanim_showScene7=function(){
    var ov=_el('qanim-scene7-overlay');if(ov)ov.classList.add('qanim-scene-visible');
    var ov7=_el('qanim-scene8-overlay');if(ov7)ov7.classList.remove('qanim-scene-visible');
    var ov9=_el('qanim-scene9-overlay');if(ov9)ov9.classList.remove('qanim-scene-visible');
    var bd=_el('qanim-scene-modal-backdrop');if(bd)bd.classList.add('qanim-scene-visible');
    _cancelRAF();_syncDots7();
    s6Phase=0;if(s6AutoTimer){clearTimeout(s6AutoTimer);s6AutoTimer=null;}
    s6Render();
  };

  window.qanim_goToPrevScene=function(){
    ['qanim-scene7-overlay','qanim-scene8-overlay','qanim-scene9-overlay'].forEach(function(id){var el=_el(id);if(el)el.classList.remove('qanim-scene-visible');});
    var bd=_el('qanim-scene-modal-backdrop');if(bd)bd.classList.remove('qanim-scene-visible');
    if(s6AutoTimer){clearTimeout(s6AutoTimer);s6AutoTimer=null;}
    var svgC=document.querySelector('.svg-container');if(svgC){svgC.style.transition='opacity .45s ease';svgC.style.opacity='1';}
    if(typeof window.applyStep==='function'&&typeof window.stepsData!=='undefined'){var last=window.stepsData.length-1;window.currentStep=last;window.applyStep(last);}
    _resumeRAF();
  };

  _onReady(function(){
    var origReset=window.resetAnim;
    window.resetAnim=function(){
      ['qanim-scene7-overlay','qanim-scene8-overlay','qanim-scene9-overlay'].forEach(function(id){var el=_el(id);if(el)el.classList.remove('qanim-scene-visible');});
      var bd=_el('qanim-scene-modal-backdrop');if(bd)bd.classList.remove('qanim-scene-visible');
      var svgC=document.querySelector('.svg-container');if(svgC)svgC.style.opacity='1';
      if(typeof origReset==='function')origReset();
    };
  });
})();
</script>"""


def inject_scene7_formula(html: str, gemini_sol: dict, scene_script: dict) -> str:
    if 'qanim-scene7-styles' in html:
        return html
    formula_data = scene_script.get("formula_data", {})
    sol = gemini_sol or {}
    # BUG FIX (v2.0.2): if formula_data is missing OR contains placeholder
    # content, fall back to the real SolutionGenerator data instead of
    # rendering "Result = f(given values)" to the user.
    if not formula_data or _is_fallback_content(formula_data.get("formula_text", "")):
        formula_data = {
            "formula_text":     sol.get("formula", "Governing Formula"),
            "formula_sublabel": "Governing Equation",
            "variables":        sol.get("variables", []),
            "note_text":        sol.get("key_insight", ""),
        }
    scene7_html = _build_scene7_html(formula_data)
    if '</head>' in html:
        html = html.replace('</head>', _SCENE7_STYLES + '\n</head>', 1)
        inject_body = scene7_html + "\n" + _SCENE7_JS
        if '<body' in html:
            idx = html.find('>', html.find('<body')) + 1
            html = html[:idx] + inject_body + html[idx:]
        else:
            html = html + inject_body
    else:
        html = _SCENE7_STYLES + "\n" + scene7_html + "\n" + _SCENE7_JS + html
    QAnimLogger.ok("Scene7", "Main Formula panel injected (v2 design)")
    return html


# ===========================================================================
#  MODULE 12 — Scene 7/8 (Step 8: Substitution) — Full Redesign
# ===========================================================================

_SCENE7_STYLES = """\
<style id="qanim-scene8-styles">
#qanim-scene8-overlay{
  display:none;position:fixed;
  top:50%;left:50%;
  transform:translate(-50%,-50%) scale(.93) translateY(12px);
  z-index:7500;width:min(940px,96vw);max-height:94vh;
  overflow-y:auto;box-sizing:border-box;
  opacity:0;pointer-events:none;
  transition:opacity .35s cubic-bezier(.4,0,.2,1),
             transform .4s cubic-bezier(.34,1.28,.64,1);
  scrollbar-width:thin;scrollbar-color:#a7f3d0 #f0fdf4;
}
#qanim-scene8-overlay::-webkit-scrollbar{width:5px;}
#qanim-scene8-overlay::-webkit-scrollbar-track{background:#f0fdf4;}
#qanim-scene8-overlay::-webkit-scrollbar-thumb{background:#a7f3d0;border-radius:4px;}
#qanim-scene8-overlay.qanim-scene-visible{
  display:block!important;opacity:1;pointer-events:auto;
  transform:translate(-50%,-50%) scale(1) translateY(0);
}

.s8-card{
  background:#fff;border-radius:24px;overflow:hidden;
  box-shadow:0 24px 80px rgba(5,150,105,.12),0 4px 16px rgba(0,0,0,.07);
  border:1px solid #d1fae5;
  font-family:'Inter',system-ui,-apple-system,sans-serif;
}

.s8-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 28px;height:58px;
  background:linear-gradient(90deg,#065f46 0%,#059669 55%,#10b981 100%);
  position:relative;overflow:hidden;
}
.s8-header::after{
  content:'';position:absolute;inset:0;
  background:repeating-linear-gradient(45deg,transparent,transparent 20px,rgba(255,255,255,.03) 20px,rgba(255,255,255,.03) 40px);
  pointer-events:none;
}
.s8-header-left{display:flex;align-items:center;gap:10px;z-index:1;}
.s8-step-badge{
  background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.3);
  border-radius:8px;padding:3px 10px;
  font-size:10.5px;font-weight:800;letter-spacing:1.5px;color:#a7f3d0;
  text-transform:uppercase;
}
.s8-header-title{font-size:15px;font-weight:700;color:#fff;}
.s8-header-right{z-index:1;}
.s8-header-system{font-size:11.5px;font-weight:600;color:#6ee7b7;letter-spacing:.2px;max-width:260px;text-align:right;line-height:1.35;}

.s8-body{display:flex;min-height:380px;}

.s8-left{
  width:42%;min-width:220px;
  background:linear-gradient(160deg,#ecfdf5 0%,#d1fae5 60%,#a7f3d0 100%);
  border-right:1.5px solid #bbf7d0;
  padding:24px 20px 22px 26px;
  display:flex;flex-direction:column;gap:14px;
}
.s8-left-title{
  font-size:9.5px;font-weight:800;letter-spacing:2px;
  text-transform:uppercase;color:#065f46;
}
.s8-diagram-card{
  background:#fff;border:1.5px solid #a7f3d0;border-radius:16px;
  padding:18px 16px 16px;flex:1;
  display:flex;flex-direction:column;align-items:center;gap:8px;
  box-shadow:0 2px 10px rgba(5,150,105,.09);
}
.s8-diagram-system-name{
  font-size:14px;font-weight:800;color:#065f46;text-align:center;line-height:1.3;
}
.s8-diagram-icon{font-size:32px;margin:4px 0;}
.s8-diagram-desc{
  font-size:11.5px;color:#374151;text-align:center;line-height:1.55;
  background:#f0fdf4;border-radius:8px;padding:8px 10px;
  border:1px solid #d1fae5;width:100%;box-sizing:border-box;
}
.s8-given-table{
  background:#fff;border:1.5px solid #a7f3d0;border-radius:12px;
  overflow:hidden;box-shadow:0 1px 6px rgba(5,150,105,.07);
}
.s8-given-table-head{
  background:linear-gradient(90deg,#059669,#10b981);
  padding:8px 14px;
  font-size:9.5px;font-weight:800;letter-spacing:1.8px;
  text-transform:uppercase;color:#fff;
}
.s8-given-row{
  display:flex;align-items:center;gap:0;
  border-top:1px solid #d1fae5;
  transition:background .15s;
}
.s8-given-row:hover{background:#f0fdf4;}
.s8-given-sym{
  font-family:'JetBrains Mono','Courier New',monospace;
  font-size:13px;font-weight:800;color:#065f46;
  padding:9px 12px;min-width:46px;text-align:center;
  border-right:1px solid #d1fae5;background:#f0fdf4;
  flex-shrink:0;
}
.s8-given-detail{padding:9px 12px;flex:1;}
.s8-given-val{
  font-family:'JetBrains Mono','Courier New',monospace;
  font-size:13px;font-weight:700;color:#065f46;display:block;
}
.s8-given-desc{font-size:10.5px;color:#6b7280;line-height:1.3;}

.s8-right{
  flex:1;padding:24px 28px 22px 22px;
  display:flex;flex-direction:column;gap:0;
  background:#fafffe;
}

.s8-section{margin-bottom:18px;}
.s8-section-title{
  display:flex;align-items:center;gap:8px;
  font-size:10px;font-weight:800;letter-spacing:1.8px;
  text-transform:uppercase;margin-bottom:10px;
}
.s8-section-title-dot{
  width:8px;height:8px;border-radius:50%;flex-shrink:0;
}

.s8-approach-list{display:flex;flex-direction:column;gap:8px;}
.s8-approach-step{
  display:flex;align-items:flex-start;gap:10px;
  padding:10px 14px;border-radius:10px;
  background:#f8fffe;border:1px solid #ccfbf1;
  transition:background .15s;
}
.s8-approach-step:hover{background:#ecfdf5;}
.s8-approach-num{
  width:22px;height:22px;border-radius:50%;
  background:linear-gradient(135deg,#059669,#10b981);
  color:#fff;font-size:11px;font-weight:800;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:1px;
}
.s8-approach-text{
  font-size:13px;color:#1e293b;line-height:1.55;font-weight:500;
  font-family:'Inter',sans-serif;
}
.s8-approach-text code{
  font-family:'JetBrains Mono','Courier New',monospace;
  font-size:12.5px;font-weight:700;color:#065f46;
  background:#d1fae5;padding:1px 6px;border-radius:5px;
}

.s8-result-bar{
  margin-top:auto;padding:16px 20px;
  background:linear-gradient(135deg,#065f46,#059669);
  border-radius:14px;
  box-shadow:0 4px 18px rgba(5,150,105,.25);
  display:flex;align-items:center;gap:16px;
}
.s8-result-bar-label{
  font-size:9.5px;font-weight:800;letter-spacing:2px;
  text-transform:uppercase;color:#6ee7b7;
  writing-mode:vertical-rl;text-orientation:mixed;
  transform:rotate(180deg);flex-shrink:0;
}
.s8-result-bar-eq{
  font-family:'JetBrains Mono','Courier New',monospace;
  font-size:15.5px;font-weight:700;color:#fff;
  line-height:1.45;letter-spacing:.5px;flex:1;
}
.s8-result-bar-eq .s8-result-final{
  color:#86efac;font-size:18px;font-weight:900;
}

.s8-nav-row{
  display:flex;justify-content:space-between;align-items:center;
  gap:12px;padding:15px 28px 20px;
  border-top:1.5px solid #d1fae5;background:#f0fdf4;
}
.s8-btn{
  display:inline-flex;align-items:center;gap:7px;
  padding:10px 22px;border-radius:10px;
  font-family:'Inter',sans-serif;font-size:13.5px;font-weight:700;
  cursor:pointer;border:none;transition:all .18s ease;
}
.s8-btn-back{background:#f1f5f9;color:#64748b;border:1.5px solid #cbd5e1!important;}
.s8-btn-back:hover{background:#e2e8f0;color:#334155;transform:translateX(-2px);}
.s8-btn-next{
  background:linear-gradient(135deg,#065f46,#059669);color:#fff;
  box-shadow:0 4px 14px rgba(5,150,105,.28);
}
.s8-btn-next:hover{box-shadow:0 6px 20px rgba(5,150,105,.38);transform:translateY(-1px);}
</style>"""


def _build_scene8_html(substitution_data: dict) -> str:
    sub          = substitution_data or {}
    system_title = html_module.escape(sub.get("system_title", "Physical System"))
    system_desc  = html_module.escape(sub.get("system_description", ""))
    given_list   = sub.get("given_list", [])
    approach_steps = sub.get("approach_steps", [])
    result_bar   = sub.get("result_bar", "")

    given_rows_html = ""
    for g in given_list:
        g_str = str(g)
        import re as _re
        m = _re.match(r'^([A-Za-zΔδαβγθλμρσφωΩΠπ∞₀-₉_\s\^\']+?)\s*=\s*([^\(]+?)(?:\s*\((.+?)\))?\s*$', g_str)
        if m:
            sym   = m.group(1).strip()
            val   = m.group(2).strip()
            desc  = m.group(3).strip() if m.group(3) else ""
        else:
            parts = g_str.split('=', 1)
            sym   = parts[0].strip() if len(parts) > 1 else "—"
            val   = parts[1].strip() if len(parts) > 1 else g_str
            desc  = ""
        given_rows_html += f"""
          <div class="s8-given-row">
            <div class="s8-given-sym">{html_module.escape(sym)}</div>
            <div class="s8-given-detail">
              <span class="s8-given-val">{html_module.escape(val)}</span>
              {f'<span class="s8-given-desc">{html_module.escape(desc)}</span>' if desc else ''}
            </div>
          </div>"""

    import re as _re2
    approach_html = ""
    for i, s in enumerate(approach_steps):
        def _codify(text):
            return _re2.sub(
                r'([A-Za-z_Δδ][A-Za-z_₀-₉Δδ]?\s*(?:[=×\-+/÷·]\s*)?(?:[A-Za-z0-9_Δδ.×\-+/÷·\(\)\[\]°²³µ]+\s*)+)',
                lambda m2: f'<code>{html_module.escape(m2.group(0).strip())}</code>',
                html_module.escape(text)
            )
        approach_html += f"""
        <div class="s8-approach-step">
          <div class="s8-approach-num">{i+1}</div>
          <div class="s8-approach-text">{_codify(s)}</div>
        </div>"""

    result_escaped = html_module.escape(result_bar)
    result_html = _re2.sub(
        r'=\s*([\d.,\s\w°²³µ/·\-+]+)$',
        lambda m3: f'= <span class="s8-result-final">{html_module.escape(m3.group(1).strip())}</span>',
        result_escaped
    )

    return f"""
<div id="qanim-scene8-overlay" role="dialog" aria-modal="true" aria-label="Step 8: Substitution">
  <div class="s8-card">
    <div class="s8-header">
      <div class="s8-header-left">
        <div class="s8-step-badge">Step 8 of 9</div>
        <div class="s8-header-title">Step-by-Step Substitution</div>
      </div>
      <div class="s8-header-right">
        <div class="s8-header-system">{system_title}</div>
      </div>
    </div>
    <div class="s8-body">
      <div class="s8-left">
        <div class="s8-left-title">Physical System</div>
        <div class="s8-diagram-card">
          <div class="s8-diagram-system-name">{system_title}</div>
          <div class="s8-diagram-icon">&#x1F4D0;</div>
          <div class="s8-diagram-desc">{system_desc[:120]}</div>
        </div>
        <div class="s8-given-table">
          <div class="s8-given-table-head">Given Data</div>
          {given_rows_html}
        </div>
      </div>
      <div class="s8-right">
        <div class="s8-section">
          <div class="s8-section-title" style="color:#059669;">
            <div class="s8-section-title-dot" style="background:#059669;"></div>
            Solution Approach
          </div>
          <div class="s8-approach-list">{approach_html}</div>
        </div>
        <div class="s8-result-bar">
          <div class="s8-result-bar-label">Result</div>
          <div class="s8-result-bar-eq">{result_html}</div>
        </div>
      </div>
    </div>
    <div class="s8-nav-row">
      <button class="s8-btn s8-btn-back"
        onclick="if(typeof window.qanim_showScene7===\'function\') window.qanim_showScene7()">
        &#9664; Step 7: Formula
      </button>
      <button class="s8-btn s8-btn-next"
        onclick="if(typeof window.qanim_showScene9===\'function\') window.qanim_showScene9()">
        Step 9: Final Answer &#9654;
      </button>
    </div>
  </div>
</div>"""


_SCENE7_JS = """\
<script id="qanim-js-scene8">
(function initScene7(){
  'use strict';
  if(window.__qanimScene7Init)return;window.__qanimScene7Init=true;
  function _el(id){return document.getElementById(id);}
  function _syncDots8(){
    var dots=document.querySelectorAll('.step-dot');
    for(var i=0;i<dots.length;i++){dots[i].classList.remove('active','done');if(i<7)dots[i].classList.add('done');if(i===7)dots[i].classList.add('active');}
    var lbl=_el('step-label');if(lbl)lbl.innerText='Step 8 of 9: Step-by-Step Substitution';
    var bar=_el('step-bar');if(bar)bar.style.width=Math.round(8/9*100)+'%';
  }
  window.qanim_showScene8=function(){
    var ov6=_el('qanim-scene7-overlay');if(ov6)ov6.classList.remove('qanim-scene-visible');
    var ov9=_el('qanim-scene9-overlay');if(ov9)ov9.classList.remove('qanim-scene-visible');
    var ov7=_el('qanim-scene8-overlay');if(ov7)ov7.classList.add('qanim-scene-visible');
    var bd=_el('qanim-scene-modal-backdrop');if(bd)bd.classList.add('qanim-scene-visible');
    _syncDots8();
  };
  window.qanim_showScene8=window.qanim_showScene8;
})();
</script>"""


def inject_scene8_how_we_solve_it(html: str, gemini_sol: dict, scene_script: dict) -> str:
    if 'qanim-scene8-styles' in html:
        return html
    substitution_data = scene_script.get("substitution_data", {})
    sol = gemini_sol or {}
    # BUG FIX (v2.0.2): if substitution_data is missing OR its given_list
    # contains placeholder text, use real SolutionGenerator data instead.
    given_list = substitution_data.get("given_list", [])
    _sub_is_placeholder = (
        not substitution_data
        or not given_list
        or (len(given_list) == 1 and _is_fallback_content(given_list[0]))
        or _is_fallback_content(substitution_data.get("result_bar", ""))
    )
    if _sub_is_placeholder:
        sol_steps = [str(s) for s in (sol.get("steps", []) or [])[:6]]
        substitution_data = {
            "system_title":       substitution_data.get("system_title", "Physical System"),
            "system_description": substitution_data.get("system_description", ""),
            "given_list":         sol_steps[:4] if sol_steps else ["Apply the governing formula."],
            "approach_steps":     sol_steps[:3] if sol_steps else ["Identify formula", "Substitute values", "Compute result"],
            "result_bar":         sol.get("final_answer", "See calculation above"),
        }
    scene8_html = _build_scene8_html(substitution_data)
    if '</head>' in html:
        html = html.replace('</head>', _SCENE7_STYLES + '\n</head>', 1)
    if '<body' in html:
        idx = html.find('>', html.find('<body')) + 1
        html = html[:idx] + scene8_html + html[idx:]
    else:
        html = html + scene8_html
    if '</body>' in html:
        html = html.replace('</body>', _SCENE7_JS + '\n</body>', 1)
    else:
        html = html + _SCENE7_JS
    QAnimLogger.ok("Scene7", "Substitution panel injected (v2 design)")
    return html


# ===========================================================================
#  MODULE 13 — Scene 9 (Step 9: Final Answer) — Full Redesign
# ===========================================================================

_SCENE9_STYLES = """\
<style id="qanim-scene9-styles">
#qanim-scene9-overlay{
  display:none;position:fixed;
  top:50%;left:50%;
  transform:translate(-50%,-50%) scale(.93) translateY(12px);
  z-index:7500;width:min(880px,96vw);max-height:94vh;
  overflow-y:auto;box-sizing:border-box;
  opacity:0;pointer-events:none;
  transition:opacity .35s cubic-bezier(.4,0,.2,1),
             transform .4s cubic-bezier(.34,1.28,.64,1);
  scrollbar-width:thin;scrollbar-color:#86efac #f0fdf4;
}
#qanim-scene9-overlay::-webkit-scrollbar{width:5px;}
#qanim-scene9-overlay::-webkit-scrollbar-track{background:#f0fdf4;}
#qanim-scene9-overlay::-webkit-scrollbar-thumb{background:#86efac;border-radius:4px;}
#qanim-scene9-overlay.qanim-scene-visible{
  display:block!important;opacity:1;pointer-events:auto;
  transform:translate(-50%,-50%) scale(1) translateY(0);
}

.s9-card{
  background:#fff;border-radius:24px;overflow:hidden;
  box-shadow:0 24px 80px rgba(22,163,74,.15),0 4px 16px rgba(0,0,0,.07);
  border:1px solid #bbf7d0;
  font-family:'Inter',system-ui,-apple-system,sans-serif;
}

.s9-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 28px;height:58px;
  background:linear-gradient(90deg,#14532d 0%,#16a34a 55%,#22c55e 100%);
  position:relative;overflow:hidden;
}
.s9-header::after{
  content:'';position:absolute;inset:0;
  background:url("data:image/svg+xml,%3Csvg width='40' height='40' viewBox='0 0 40 40' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M0 40L40 0H20L0 20M40 40V20L20 40' fill='none' stroke='white' stroke-opacity='.04' stroke-width='1'/%3E%3C/svg%3E") repeat;
  pointer-events:none;
}
.s9-header-left{display:flex;align-items:center;gap:10px;z-index:1;}
.s9-step-badge{
  background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.3);
  border-radius:8px;padding:3px 10px;
  font-size:10.5px;font-weight:800;letter-spacing:1.5px;color:#86efac;
  text-transform:uppercase;
}
.s9-header-title{font-size:15px;font-weight:700;color:#fff;}
.s9-header-right{z-index:1;}
.s9-header-target{
  font-size:11.5px;font-weight:600;color:#86efac;text-align:right;
  max-width:260px;line-height:1.3;
}

.s9-body{padding:26px 32px 22px;display:flex;flex-direction:column;gap:20px;}

.s9-formula-recap{
  display:flex;align-items:center;gap:0;
  background:#eff6ff;border:1.5px solid #bfdbfe;border-radius:14px;
  overflow:hidden;
}
.s9-formula-recap-tab{
  background:linear-gradient(135deg,#1d4ed8,#3b82f6);
  padding:0 18px;align-self:stretch;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  writing-mode:vertical-rl;text-orientation:mixed;transform:rotate(180deg);
}
.s9-formula-recap-tab-text{
  font-size:9px;font-weight:800;letter-spacing:2px;
  text-transform:uppercase;color:#bfdbfe;white-space:nowrap;
}
.s9-formula-recap-body{padding:13px 20px;flex:1;}
.s9-formula-recap-eq{
  font-family:'JetBrains Mono','Courier New',monospace;
  font-size:17px;font-weight:700;color:#1d4ed8;letter-spacing:.8px;
  line-height:1.4;
}

.s9-chain-title{
  font-size:9.5px;font-weight:800;letter-spacing:2px;
  text-transform:uppercase;color:#64748b;margin-bottom:10px;
}
.s9-sub-chain{display:flex;flex-direction:column;gap:8px;}
.s9-sub-row{
  display:flex;align-items:center;gap:12px;
  background:#f8fafc;border:1.5px solid #e2e8f0;border-radius:12px;
  padding:12px 18px 12px 14px;
  opacity:0;transform:translateX(-22px);
  transition:opacity .42s cubic-bezier(.4,0,.2,1),
             transform .42s cubic-bezier(.34,1.2,.64,1);
  position:relative;overflow:hidden;
}
.s9-sub-row::before{
  content:'';position:absolute;left:0;top:0;bottom:0;
  width:3px;background:linear-gradient(to bottom,#0891b2,#6366f1);
  border-radius:3px 0 0 3px;opacity:0;
  transition:opacity .3s ease;
}
.s9-sub-row.s9-shown{opacity:1;transform:translateX(0);}
.s9-sub-row.s9-shown::before{opacity:1;}
.s9-sub-row.s9-final{
  background:linear-gradient(135deg,#f0fdf4,#dcfce7);
  border-color:#86efac;border-width:2px;
}
.s9-sub-row.s9-final::before{
  background:linear-gradient(to bottom,#22c55e,#16a34a);
  width:4px;
}
.s9-sub-num{
  width:28px;height:28px;border-radius:50%;
  background:linear-gradient(135deg,#0891b2,#6366f1);
  color:#fff;font-size:12px;font-weight:800;
  display:flex;align-items:center;justify-content:center;flex-shrink:0;
  box-shadow:0 2px 6px rgba(8,145,178,.25);
}
.s9-sub-row.s9-final .s9-sub-num{
  background:linear-gradient(135deg,#16a34a,#22c55e);
  box-shadow:0 2px 6px rgba(22,163,74,.3);
}
.s9-sub-eq{
  font-family:'JetBrains Mono','Courier New',monospace;
  font-size:14.5px;font-weight:700;color:#1e293b;flex:1;
  letter-spacing:.3px;line-height:1.4;
}
.s9-sub-row.s9-final .s9-sub-eq{
  font-size:16px;color:#14532d;font-weight:800;
}

.s9-final-box{
  background:linear-gradient(145deg,#f0fdf4 0%,#dcfce7 50%,#bbf7d0 100%);
  border:3px solid #22c55e;border-radius:20px;
  padding:0;overflow:hidden;
  opacity:0;transform:scale(.94) translateY(8px);
  transition:opacity .55s cubic-bezier(.4,0,.2,1) .2s,
             transform .55s cubic-bezier(.34,1.2,.64,1) .2s;
  box-shadow:0 8px 32px rgba(22,163,74,.18);
}
.s9-final-box.s9-shown{opacity:1;transform:scale(1) translateY(0);}
.s9-final-box-top{
  display:flex;align-items:center;justify-content:space-between;
  padding:12px 24px;background:rgba(22,163,74,.10);
  border-bottom:1.5px solid #86efac;
}
.s9-final-box-label{
  font-size:10px;font-weight:800;letter-spacing:2px;
  text-transform:uppercase;color:#15803d;
}
.s9-final-box-check{font-size:22px;}
.s9-final-box-body{
  display:flex;align-items:center;justify-content:center;gap:16px;
  padding:28px 32px;
}
.s9-final-value-wrap{display:flex;align-items:baseline;gap:8px;}
.s9-final-number{
  font-family:'JetBrains Mono','Courier New',monospace;
  font-size:52px;font-weight:900;color:#14532d;
  line-height:1;letter-spacing:-1px;
}
.s9-final-unit-wrap{display:flex;flex-direction:column;gap:2px;}
.s9-final-unit{
  font-family:'JetBrains Mono','Courier New',monospace;
  font-size:18px;font-weight:700;color:#166534;line-height:1.2;
}
.s9-final-unit-name{font-size:10.5px;color:#4ade80;font-weight:600;}

.s9-insight-bar{
  display:flex;align-items:flex-start;gap:14px;
  background:linear-gradient(135deg,#fffbeb,#fef9c3);
  border:1.5px solid #fde68a;border-radius:14px;
  padding:16px 20px;
  box-shadow:0 2px 10px rgba(245,158,11,.10);
  opacity:0;transition:opacity .5s ease .5s;
}
.s9-insight-bar.s9-shown{opacity:1;}
.s9-insight-icon-wrap{
  width:36px;height:36px;border-radius:10px;
  background:linear-gradient(135deg,#f59e0b,#fbbf24);
  display:flex;align-items:center;justify-content:center;
  flex-shrink:0;box-shadow:0 2px 8px rgba(245,158,11,.25);
}
.s9-insight-icon{font-size:18px;}
.s9-insight-content{flex:1;}
.s9-insight-label{
  font-size:9px;font-weight:800;letter-spacing:2px;
  text-transform:uppercase;color:#a16207;margin-bottom:4px;
}
.s9-insight-text{font-size:13.5px;color:#78350f;line-height:1.65;font-weight:500;}
.s9-insight-text strong{color:#451a03;font-weight:800;}

.s9-nav-row{
  display:flex;justify-content:space-between;align-items:center;
  gap:12px;padding:15px 28px 22px;
  border-top:1.5px solid #bbf7d0;background:#f0fdf4;
}
.s9-btn{
  display:inline-flex;align-items:center;gap:7px;
  padding:10px 22px;border-radius:10px;
  font-family:'Inter',sans-serif;font-size:13.5px;font-weight:700;
  cursor:pointer;border:none;transition:all .18s ease;
}
.s9-btn-back{background:#f1f5f9;color:#64748b;border:1.5px solid #cbd5e1!important;}
.s9-btn-back:hover{background:#e2e8f0;color:#334155;transform:translateX(-2px);}
.s9-btn-restart{
  background:linear-gradient(135deg,#0e7490,#0891b2);color:#fff;
  box-shadow:0 4px 14px rgba(14,116,144,.28);
}
.s9-btn-restart:hover{box-shadow:0 6px 20px rgba(14,116,144,.38);transform:translateY(-1px);}
</style>"""


def _build_scene9_html(final_answer_data: dict, to_find_label: str = "Final Answer") -> str:
    fad              = final_answer_data or {}
    formula_recap    = html_module.escape(fad.get("formula_recap", "Result = f(values)"))
    chain            = fad.get("substitution_chain", [])
    answer_value     = html_module.escape(fad.get("answer_value", "?"))
    answer_unit      = html_module.escape(fad.get("answer_unit", ""))
    answer_highlight = html_module.escape(fad.get("answer_highlight", answer_value))
    insight_text     = fad.get("insight_text", "Apply the governing formula with the given data.")
    label            = html_module.escape(fad.get("to_find_label", to_find_label))

    unit_names = {
        "W": "Watts",  "kW": "Kilowatts",  "J": "Joules",  "kJ": "Kilojoules",
        "N": "Newtons","Pa": "Pascals",     "K": "Kelvin",  "°C": "Celsius",
        "m": "Metres", "m²": "Sq metres",  "m³": "Cu metres",
        "s": "Seconds","kg": "Kilograms",   "m/s": "m per s", "m/s²": "m/s²",
        "A": "Amperes","V": "Volts",       "Ω": "Ohms",    "F": "Farads",
        "mol": "Moles","Hz": "Hertz",
    }
    unit_name = unit_names.get(answer_unit, "")

    chain_html = ""
    for idx2, row in enumerate(chain):
        num    = row.get("num", idx2 + 1)
        eq     = html_module.escape(row.get("eq", ""))
        is_last = (idx2 == len(chain) - 1)
        extra  = " s9-final" if is_last else ""
        chain_html += f"""
        <div class="s9-sub-row{extra}">
          <div class="s9-sub-num">{num}</div>
          <div class="s9-sub-eq">{eq}</div>
        </div>"""

    return f"""
<div id="qanim-scene9-overlay" role="dialog" aria-modal="true" aria-label="Step 9: Final Answer">
  <div class="s9-card">
    <div class="s9-header">
      <div class="s9-header-left">
        <div class="s9-step-badge">Step 9 of 9</div>
        <div class="s9-header-title">Final Answer</div>
      </div>
      <div class="s9-header-right">
        <div class="s9-header-target">Finding: {label}</div>
      </div>
    </div>
    <div class="s9-body">
      <div class="s9-formula-recap">
        <div class="s9-formula-recap-tab">
          <span class="s9-formula-recap-tab-text">Formula</span>
        </div>
        <div class="s9-formula-recap-body">
          <div class="s9-formula-recap-eq">{formula_recap}</div>
        </div>
      </div>
      <div>
        <div class="s9-chain-title">Substitution Chain</div>
        <div class="s9-sub-chain" id="s9-sub-chain">{chain_html}</div>
      </div>
      <div class="s9-final-box" id="s9-final-box">
        <div class="s9-final-box-top">
          <div class="s9-final-box-label">&#x2705; {label}</div>
          <div class="s9-final-box-check">&#x1F3AF;</div>
        </div>
        <div class="s9-final-box-body">
          <div class="s9-final-value-wrap">
            <div class="s9-final-number">{answer_highlight}</div>
            <div class="s9-final-unit-wrap">
              <div class="s9-final-unit">{answer_unit}</div>
              {f'<div class="s9-final-unit-name">{unit_name}</div>' if unit_name else ''}
            </div>
          </div>
        </div>
      </div>
      <div class="s9-insight-bar" id="s9-insight-bar">
        <div class="s9-insight-icon-wrap">
          <div class="s9-insight-icon">&#x1F4A1;</div>
        </div>
        <div class="s9-insight-content">
          <div class="s9-insight-label">Key Insight</div>
          <div class="s9-insight-text">{insight_text}</div>
        </div>
      </div>
    </div>
    <div class="s9-nav-row">
      <button class="s9-btn s9-btn-back"
        onclick="if(typeof window.qanim_showScene8===\'function\') window.qanim_showScene8()">
        &#9664; Step 8: Substitution
      </button>
      <button class="s9-btn s9-btn-restart"
        onclick="if(typeof window.qanim_goToPrevScene===\'function\') window.qanim_goToPrevScene()">
        &#x21BA; Restart Animation
      </button>
    </div>
  </div>
</div>"""


_SCENE9_JS = """\
<script id="qanim-js-scene9">
(function initScene9(){
  'use strict';
  if(window.__qanimScene9Init)return;window.__qanimScene9Init=true;
  function _el(id){return document.getElementById(id);}

  function _syncDots9(){
    var dots=document.querySelectorAll('.step-dot');
    for(var i=0;i<dots.length;i++){dots[i].classList.remove('active','done');if(i<8)dots[i].classList.add('done');if(i===8)dots[i].classList.add('active');}
    var lbl=_el('step-label');if(lbl)lbl.innerText='Step 9 of 9: Final Answer';
    var bar=_el('step-bar');if(bar)bar.style.width='100%';
  }

  function _animateEntrance(){
    var rows=document.querySelectorAll('#s9-sub-chain .s9-sub-row');
    var delay=180;
    for(var i=0;i<rows.length;i++){
      (function(el,d){setTimeout(function(){el.classList.add('s9-shown');},d);})(rows[i],delay+i*220);
    }
    var fb=_el('s9-final-box');
    if(fb) setTimeout(function(){fb.classList.add('s9-shown');},delay+rows.length*220+80);
    var ib=_el('s9-insight-bar');
    if(ib) setTimeout(function(){ib.classList.add('s9-shown');},delay+rows.length*220+380);
  }

  function _resetEntrance(){
    var rows=document.querySelectorAll('#s9-sub-chain .s9-sub-row');
    for(var i=0;i<rows.length;i++) rows[i].classList.remove('s9-shown');
    var fb=_el('s9-final-box');if(fb)fb.classList.remove('s9-shown');
    var ib=_el('s9-insight-bar');if(ib)ib.classList.remove('s9-shown');
  }

  window.qanim_showScene9=function(){
    var ov7=_el('qanim-scene8-overlay');if(ov7)ov7.classList.remove('qanim-scene-visible');
    var ov6=_el('qanim-scene7-overlay');if(ov6)ov6.classList.remove('qanim-scene-visible');
    var ov9=_el('qanim-scene9-overlay');if(ov9)ov9.classList.add('qanim-scene-visible');
    var bd=_el('qanim-scene-modal-backdrop');if(bd)bd.classList.add('qanim-scene-visible');
    _syncDots9();_resetEntrance();setTimeout(_animateEntrance,120);
  };
})();
</script>"""


def inject_scene9_final_answer(html: str, gemini_sol: dict, scene_script: dict, to_find_targets: list) -> str:
    if 'qanim-scene9-styles' in html:
        return html
    final_answer_data = scene_script.get("final_answer_data", {})
    sol = gemini_sol or {}
    # BUG FIX (v2.0.2): detect placeholder content in final_answer_data and
    # replace with real SolutionGenerator data. Checks both missing data AND
    # the case where data exists but contains placeholder values like "Result".
    _fad_is_placeholder = (
        not final_answer_data
        or _is_fallback_content(final_answer_data.get("answer_value", ""))
        or _is_fallback_content(final_answer_data.get("formula_recap", ""))
    )
    if _fad_is_placeholder:
        chain_raw = sol.get("substitution_chain", sol.get("steps", []))
        chain_rows = []
        for i, s in enumerate(chain_raw[:6]):
            if isinstance(s, dict):
                chain_rows.append(s)
            else:
                chain_rows.append({"num": i + 1, "eq": str(s)[:80]})
        fa   = sol.get("final_answer", "See calculation above")
        nums = re.findall(r'[-+]?\d+(?:\.\d+)?', fa)
        val  = nums[-1] if nums else fa[:30]
        final_answer_data = {
            "formula_recap":      sol.get("formula", "Governing Formula"),
            "substitution_chain": chain_rows,
            "answer_value":       val,
            "answer_unit":        _extract_unit(fa),
            "answer_highlight":   val,
            "insight_text":       sol.get("key_insight", "Apply the governing formula with the given data."),
            "to_find_label":      to_find_targets[0] if to_find_targets else "Final Answer",
        }
    to_find_label = to_find_targets[0] if to_find_targets else "Final Answer"
    scene9_html   = _build_scene9_html(final_answer_data, to_find_label)
    if '</head>' in html:
        html = html.replace('</head>', _SCENE9_STYLES + '\n</head>', 1)
    if '<body' in html:
        idx = html.find('>', html.find('<body')) + 1
        html = html[:idx] + scene9_html + html[idx:]
    if '</body>' in html:
        html = html.replace('</body>', _SCENE9_JS + '\n</body>', 1)
    else:
        html = html + _SCENE9_JS
    QAnimLogger.ok("Scene9", "Final Answer panel injected (v2 design)")
    return html


# ===========================================================================
#  MODULE 14 — To Find Panel Injection
# ===========================================================================

_TOFIND_STYLES = """\
<style id="qanim-tofind-styles">
#tofind-backdrop{display:none;position:fixed;inset:0;z-index:8000;background:rgba(15,23,42,.40);backdrop-filter:blur(4px);opacity:0;transition:opacity .22s ease;}
#tofind-backdrop.open{display:block;opacity:1;}
#tofind-panel{display:flex;flex-direction:column;position:fixed;top:50%;left:50%;transform:translate(-50%,-48%) scale(.96);z-index:8100;width:min(460px,92vw);max-height:80vh;border-radius:16px;padding:24px;box-sizing:border-box;background:#fff;border:1px solid #e2e8f0;box-shadow:0 8px 40px rgba(0,0,0,.12);opacity:0;pointer-events:none;transition:opacity .25s ease,transform .25s cubic-bezier(.34,1.56,.64,1);}
#tofind-panel.open{opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.tf-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;}
.tf-header-left{display:flex;align-items:center;gap:10px;}
.tf-icon-wrap{width:32px;height:32px;border-radius:8px;background:#7c3aed;display:flex;align-items:center;justify-content:center;color:#fff;flex-shrink:0;}
.tf-title{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:16px;font-weight:700;color:#1e293b;}
.tf-close-btn{width:30px;height:30px;border-radius:8px;border:1px solid #e2e8f0;background:#f8fafc;color:#64748b;font-size:12px;display:flex;align-items:center;justify-content:center;cursor:pointer;transition:background .15s,color .15s;}
.tf-close-btn:hover{background:#fee2e2;color:#dc2626;}
.tf-subtitle{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:12px;color:#64748b;margin:0 0 14px;}
.tf-items-container{display:flex;flex-direction:column;gap:8px;overflow-y:auto;}
.tofind-item{display:flex;align-items:flex-start;gap:12px;padding:12px 14px;border-radius:10px;background:#f8fafc;border:1px solid #e2e8f0;opacity:0;transform:translateX(-12px);transition:background .15s;}
.tofind-item:hover{background:#ede9fe;border-color:#7c3aed;}
.tofind-check{width:20px;height:20px;border-radius:50%;background:#7c3aed;color:#fff;font-size:11px;display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.tofind-text{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:13px;font-weight:600;color:#1e293b;line-height:1.5;}
</style>"""


def inject_to_find_system(html: str, to_find_targets: list) -> str:
    if 'qanim-tofind-styles' in html:
        return html
    targets_json = json.dumps(to_find_targets, ensure_ascii=False)
    tofind_data = f'<script type="application/json" id="__tofind_data__">{{"targets": {targets_json}}}</script>'
    tofind_dom = """
<div id="tofind-backdrop" onclick="if(typeof closeToFind==='function')closeToFind()"></div>
<div id="tofind-panel" aria-hidden="true">
  <div class="tf-header">
    <div class="tf-header-left">
      <div class="tf-icon-wrap">&#x1F50D;</div>
      <div class="tf-title">What To Find</div>
    </div>
    <button class="tf-close-btn" id="tofind-close" onclick="if(typeof closeToFind==='function')closeToFind()">&#x2715;</button>
  </div>
  <div class="tf-subtitle">These are the quantities you need to calculate:</div>
  <div class="tf-items-container" id="tofind-items-container"></div>
</div>"""
    tofind_js = """<script id="qanim-js-tofind">
(function initToFindSystem(){
  'use strict';
  if(window.__qanimToFindInit)return;window.__qanimToFindInit=true;
  var toFindOpen=false,_panelBuilt=false;
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _el(id){return document.getElementById(id);}
  function _loadTargets(){try{var tag=_el('__tofind_data__');if(!tag)return[];var data=JSON.parse(tag.textContent)||{};return Array.isArray(data.targets)?data.targets:[];}catch(e){return[];}}
  function _esc(t){return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
  function _buildPanel(targets){
    if(_panelBuilt)return;_panelBuilt=true;
    var container=_el('tofind-items-container');if(!container)return;
    if(!targets||targets.length===0){container.innerHTML='<div style="font-size:13px;color:#94a3b8;text-align:center;padding:20px 0;font-style:italic;">No specific targets detected.</div>';return;}
    var html='';
    for(var i=0;i<targets.length;i++){html+='<div class="tofind-item" id="tofind-item-'+i+'"><div class="tofind-check">&#10003;</div><div class="tofind-text">'+_esc(targets[i])+'</div></div>';}
    container.innerHTML=html;
  }
  function _animateReveal(){
    var items=document.querySelectorAll('.tofind-item');
    for(var i=0;i<items.length;i++){(function(el,idx){el.style.opacity='0';el.style.transform='translateX(-12px)';el.style.transition='none';setTimeout(function(){el.style.transition='opacity .28s ease,transform .28s ease';el.style.opacity='1';el.style.transform='translateX(0)';},60+idx*80);})(items[i],i);}
  }
  function openToFind(){var bd=_el('tofind-backdrop'),pn=_el('tofind-panel');if(!bd||!pn)return;_buildPanel(_loadTargets());bd.classList.add('open');pn.classList.add('open');pn.setAttribute('aria-hidden','false');toFindOpen=true;setTimeout(_animateReveal,100);}
  function closeToFind(){var bd=_el('tofind-backdrop'),pn=_el('tofind-panel');if(bd)bd.classList.remove('open');if(pn){pn.classList.remove('open');pn.setAttribute('aria-hidden','true');}toFindOpen=false;}
  window.openToFind=openToFind;window.closeToFind=closeToFind;window.toggleToFind=function(){toFindOpen?closeToFind():openToFind();};
  _onReady(function(){
    var tfBtn=_el('tofind-ctrl-btn')||document.querySelector('[data-tofind-btn]');
    if(tfBtn){tfBtn.removeAttribute('onclick');tfBtn.addEventListener('click',function(e){e.stopPropagation();openToFind();});}
    var closeBtn=_el('tofind-close');if(closeBtn)closeBtn.addEventListener('click',closeToFind);
    var bd=_el('tofind-backdrop');if(bd)bd.addEventListener('click',closeToFind);
    document.addEventListener('keydown',function(e){if(e.key==='Escape'&&toFindOpen)closeToFind();});
  });
})();
</script>"""
    if '</head>' in html:
        html = html.replace('</head>', _TOFIND_STYLES + '\n' + tofind_data + '\n</head>', 1)
    if '<body' in html:
        idx = html.find('>', html.find('<body')) + 1
        html = html[:idx] + tofind_dom + html[idx:]
    if '</body>' in html:
        html = html.replace('</body>', tofind_js + '\n</body>', 1)
    else:
        html = html + tofind_js
    QAnimLogger.ok("ToFind", f"To Find panel injected with {len(to_find_targets)} target(s)")
    return html


# ===========================================================================
#  MODULE 15 — Answer Box Injection (Step 9 user input)
# ===========================================================================

_ANSWERBOX_STYLES = """\
<style id="qanim-answerbox-styles">
#ab-backdrop{display:none;position:fixed;inset:0;z-index:8500;background:rgba(15,23,42,.45);backdrop-filter:blur(5px);opacity:0;transition:opacity .22s ease;}
#ab-backdrop.open{display:block;opacity:1;}
#ab-panel{display:flex;flex-direction:column;position:fixed;top:50%;left:50%;transform:translate(-50%,-48%) scale(.96);z-index:8600;width:min(480px,94vw);border-radius:16px;padding:24px;box-sizing:border-box;background:#fff;border:1px solid #e2e8f0;box-shadow:0 8px 48px rgba(124,58,237,.18);opacity:0;pointer-events:none;transition:opacity .25s ease,transform .25s cubic-bezier(.34,1.56,.64,1);}
#ab-panel.open{opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
#ab-panel-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;}
#ab-panel-title{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:16px;font-weight:800;color:#1e293b;}
#ab-close-btn{width:28px;height:28px;border-radius:8px;border:1px solid #e2e8f0;background:#f8fafc;color:#64748b;font-size:12px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s;}
#ab-close-btn:hover{background:#fee2e2;color:#dc2626;}
#ab-target-label{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:13px;font-weight:700;color:#475569;margin-bottom:10px;}
#ab-user-input{width:100%;padding:12px 16px;border:2px solid #e2e8f0;border-radius:10px;font-size:16px;font-family:'Courier New',monospace;font-weight:700;color:#1e293b;box-sizing:border-box;outline:none;transition:border-color .2s;}
#ab-user-input:focus{border-color:#7c3aed;}
#ab-hint{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:11.5px;color:#94a3b8;margin-top:8px;}
#ab-submit-btn{width:100%;margin-top:14px;padding:13px;border-radius:10px;border:none;background:#7c3aed;color:#fff;font-size:14px;font-weight:700;font-family:inherit;cursor:pointer;transition:background .15s,transform .1s;}
#ab-submit-btn:hover{background:#6d28d9;transform:translateY(-1px);}
#ab-feedback{display:none;margin-top:14px;border-radius:12px;overflow:hidden;border:1px solid transparent;}
#ab-feedback.show{display:block;}
#ab-feedback.correct{border-color:#bbf7d0;} #ab-feedback.almost{border-color:#fed7aa;} #ab-feedback.wrong{border-color:#fecaca;}
.ab-feedback-top{display:flex;align-items:center;gap:10px;padding:12px 16px;}
#ab-feedback.correct .ab-feedback-top{background:#f0fdf4;} #ab-feedback.almost .ab-feedback-top{background:#fff7ed;} #ab-feedback.wrong .ab-feedback-top{background:#fef2f2;}
.ab-feedback-icon{font-size:22px;flex-shrink:0;}
.ab-feedback-verdict{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:15px;font-weight:800;}
#ab-feedback.correct .ab-feedback-verdict{color:#15803d;} #ab-feedback.almost .ab-feedback-verdict{color:#c2410c;} #ab-feedback.wrong .ab-feedback-verdict{color:#b91c1c;}
.ab-feedback-insight{padding:10px 16px 13px;border-top:1px solid;}
#ab-feedback.correct .ab-feedback-insight{background:#fafffe;border-color:#bbf7d0;} #ab-feedback.almost .ab-feedback-insight{background:#fffbf5;border-color:#fed7aa;} #ab-feedback.wrong .ab-feedback-insight{background:#fff8f8;border-color:#fecaca;}
.ab-insight-label{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1.2px;color:#64748b;margin-bottom:4px;}
.ab-insight-text{font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:12.5px;color:#1e293b;line-height:1.68;}
.ab-action-row{display:none;gap:8px;margin-top:12px;}
.ab-action-row.show{display:flex;}
#ab-retry-btn{flex:1;padding:9px 14px;border-radius:9px;border:1px solid #e2e8f0;background:#f8fafc;color:#64748b;font-size:12px;font-weight:600;font-family:inherit;cursor:pointer;transition:background .15s;}
#ab-retry-btn:hover{background:#ede9fe;border-color:#7c3aed;color:#7c3aed;}
</style>"""


def inject_answer_box(html: str, answer_targets: list) -> str:
    if 'qanim-answerbox-styles' in html:
        return html
    if not answer_targets:
        answer_targets = [{"label": "Final Answer", "value": "", "insight": "See solution above."}]
    targets_json = json.dumps(answer_targets, ensure_ascii=False)
    ab_dom = f"""
<div id="ab-backdrop" onclick="if(typeof closeAnswerBox==='function')closeAnswerBox()"></div>
<div id="ab-panel" aria-hidden="true">
  <div id="ab-panel-header">
    <div id="ab-panel-title">&#x270F;&#xFE0F; Check Your Answer</div>
    <button id="ab-close-btn" onclick="if(typeof closeAnswerBox==='function')closeAnswerBox()">&#x2715;</button>
  </div>
  <div id="ab-target-label">Enter your answer below:</div>
  <input type="text" id="ab-user-input" placeholder="e.g. 6000" autocomplete="off"/>
  <div id="ab-hint">Enter the numerical value (units optional). Press Ctrl+Enter to submit.</div>
  <button id="ab-submit-btn">Submit Answer &#x2713;</button>
  <div id="ab-feedback">
    <div class="ab-feedback-top">
      <div class="ab-feedback-icon" id="ab-feedback-icon"></div>
      <div class="ab-feedback-verdict" id="ab-feedback-verdict"></div>
    </div>
    <div class="ab-feedback-insight">
      <div class="ab-insight-label">Insight</div>
      <div class="ab-insight-text" id="ab-insight-text"></div>
    </div>
  </div>
  <div class="ab-action-row" id="ab-action-row">
    <button id="ab-retry-btn">&#x21BA; Try Again</button>
  </div>
</div>
<script type="application/json" id="__answerbox_targets__">{targets_json}</script>"""
    ab_js = """\
<script id="qanim-js-answerbox">
(function initAnswerBox(){
  'use strict';
  if(window.__qanimAnswerBoxInit)return;window.__qanimAnswerBoxInit=true;
  function _el(id){return document.getElementById(id);}
  var _targets=[],_currentIdx=0,abOpen=false;
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _loadTargets(){try{var tag=_el('__answerbox_targets__');if(!tag)return[];return JSON.parse(tag.textContent)||[];}catch(e){return[];}}
  function _normalize(s){return String(s).replace(/[^0-9.\x2D]/g,'').trim();}
  function _validate(userAns,correctVal){
    var u=parseFloat(_normalize(userAns));var c=parseFloat(_normalize(correctVal));
    if(isNaN(u)||isNaN(c)) return 'unknown';
    var diff=Math.abs((u-c)/(c||1));
    if(diff<0.01) return 'correct';
    if(diff<0.05) return 'almost';
    return 'wrong';
  }
  function _showFeedback(verdict,insight){
    var fb=_el('ab-feedback');if(!fb)return;
    fb.className='';void fb.offsetWidth;fb.classList.add('show',verdict);
    var icon=_el('ab-feedback-icon');
    var verd=_el('ab-feedback-verdict');
    if(icon) icon.textContent=verdict==='correct'?'✅':verdict==='almost'?'⚠️':'❌';
    if(verd) verd.textContent=verdict==='correct'?'Correct! Well done.':verdict==='almost'?'Very close — check rounding.':'Not quite. Review the solution.';
    var ins=_el('ab-insight-text');if(ins) ins.innerHTML=insight||'See solution above.';
    var ar=_el('ab-action-row');if(ar) ar.classList.add('show');
    var sb=_el('ab-submit-btn');if(sb) sb.style.display='none';
  }
  function _renderTarget(idx){
    var t=_targets[idx]||{};
    var lbl=_el('ab-target-label');
    if(lbl) lbl.textContent='Find: '+((t.label)||'Final Answer');
    var inp=_el('ab-user-input');if(inp){inp.value='';inp.disabled=false;}
    var fb=_el('ab-feedback');if(fb)fb.className='';
    var ar=_el('ab-action-row');if(ar)ar.className='ab-action-row';
    var sb=_el('ab-submit-btn');if(sb)sb.style.display='';
  }
  function openAnswerBox(){var bd=_el('ab-backdrop'),pn=_el('ab-panel');if(!bd||!pn)return;_targets=_loadTargets();_currentIdx=0;_renderTarget(0);bd.classList.add('open');pn.classList.add('open');pn.setAttribute('aria-hidden','false');abOpen=true;setTimeout(function(){var i=_el('ab-user-input');if(i)i.focus();},300);}
  function closeAnswerBox(){var bd=_el('ab-backdrop'),pn=_el('ab-panel');if(bd)bd.classList.remove('open');if(pn){pn.classList.remove('open');pn.setAttribute('aria-hidden','true');}abOpen=false;}
  window.openAnswerBox=openAnswerBox;window.closeAnswerBox=closeAnswerBox;window.toggleAnswerBox=function(){abOpen?closeAnswerBox():openAnswerBox();};
  _onReady(function(){
    var abBtn=_el('answerbox-ctrl-btn')||document.querySelector('[data-ab-btn]');
    if(abBtn){abBtn.removeAttribute('onclick');abBtn.addEventListener('click',function(e){e.stopPropagation();openAnswerBox();});}
    document.addEventListener('keydown',function(e){if(e.key==='Escape'&&abOpen)closeAnswerBox();});
    var sb=_el('ab-submit-btn');
    if(sb) sb.addEventListener('click',function(){
      var inp=_el('ab-user-input');var userAns=inp?inp.value.trim():'';
      var target=_targets[_currentIdx]||{};var verdict=_validate(userAns,target.value||'');
      _showFeedback(verdict,target.insight||'');if(inp)inp.disabled=true;
    });
    var inp2=_el('ab-user-input');
    if(inp2) inp2.addEventListener('keydown',function(e){if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();var sb2=_el('ab-submit-btn');if(sb2)sb2.click();}});
    var retryBtn=_el('ab-retry-btn');
    if(retryBtn) retryBtn.addEventListener('click',function(){
      var inp=_el('ab-user-input');if(inp){inp.value='';inp.disabled=false;inp.focus();}
      var fb=_el('ab-feedback');if(fb)fb.className='';
      var ar=_el('ab-action-row');if(ar)ar.className='ab-action-row';
      var sb=_el('ab-submit-btn');if(sb)sb.style.display='';
    });
  });
})();
</script>"""
    if '</head>' in html:
        html = html.replace('</head>', _ANSWERBOX_STYLES + '\n</head>', 1)
    if '<body' in html:
        idx = html.find('>', html.find('<body')) + 1
        html = html[:idx] + ab_dom + html[idx:]
    if '</body>' in html:
        html = html.replace('</body>', ab_js + '\n</body>', 1)
    else:
        html = html + ab_js
    QAnimLogger.ok("AnswerBox", f"Answer box injected with {len(answer_targets)} target(s)")
    return html


# ===========================================================================
#  MODULE 16 — Controls Bar Injection
# ===========================================================================

_CONTROLS_BAR_HTML = """\
<style id="qanim-controls-bar-styles">
#qanim-controls-bar{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);z-index:7000;display:flex;align-items:center;gap:6px;background:rgba(255,255,255,.98);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1.5px solid transparent;border-radius:16px;padding:10px 14px;box-shadow:0 6px 36px rgba(124,58,237,.18),0 2px 8px rgba(0,0,0,.08);white-space:nowrap;}
#qanim-controls-bar::before{content:'';position:absolute;inset:-2px;border-radius:18px;background:linear-gradient(90deg,#7c3aed,#db2777,#f59e0b,#7c3aed);background-size:200% 100%;animation:qanim-bar-glow 4s linear infinite;z-index:-1;}
@keyframes qanim-bar-glow{0%{background-position:0% 50%}100%{background-position:200% 50%}}
.qanim-ctrl-btn{display:flex;align-items:center;gap:5px;padding:8px 15px;border-radius:10px;border:1.5px solid #e2e8f0;background:linear-gradient(135deg,#f8fafc 0%,#f1f5f9 100%);color:#334155;font-family:-apple-system,'Segoe UI',Arial,sans-serif;font-size:12px;font-weight:700;cursor:pointer;transition:background .15s,border-color .15s,color .15s,transform .12s,box-shadow .15s;user-select:none;letter-spacing:.2px;}
.qanim-ctrl-btn:hover{background:linear-gradient(135deg,#ede9fe 0%,#fdf4ff 100%);border-color:#7c3aed;color:#6d28d9;transform:translateY(-2px);box-shadow:0 4px 14px rgba(124,58,237,.22);}
.qanim-ctrl-sep{width:1px;height:22px;background:linear-gradient(to bottom,transparent,#c4b5fd,transparent);flex-shrink:0;}
</style>
<div id="qanim-controls-bar" role="toolbar" aria-label="QAnim Controls">
  <button class="qanim-ctrl-btn" id="answerbox-ctrl-btn" title="Check your answer">
    <span>&#x270F;&#xFE0F;</span><span class="ctrl-label">Answer Box</span>
  </button>
  <div class="qanim-ctrl-sep"></div>
  <button class="qanim-ctrl-btn" id="tofind-ctrl-btn" title="What are you asked to find?">
    <span>&#x1F50D;</span><span class="ctrl-label">To Find</span>
  </button>
</div>"""


def inject_controls_bar(html: str) -> str:
    if 'qanim-controls-bar' in html:
        return html
    if '</body>' in html:
        html = html.replace('</body>', _CONTROLS_BAR_HTML + '\n</body>', 1)
    else:
        html = html + _CONTROLS_BAR_HTML
    QAnimLogger.ok("ControlsBar", "Controls bar injected")
    return html


# ===========================================================================
#  MODULE 17 — Previous Step Button Injection
# ===========================================================================

_PREVSTEP_STYLES = """\
<style id="qanim-prevstep-styles">
#btn-prev.qanim-prev-btn{background:#ffffff;color:#64748b;border:1.5px solid #cbd5e1;padding:11px 20px;border-radius:10px;font-size:13.5px;font-weight:700;font-family:inherit;cursor:pointer;transition:background .2s ease,color .2s ease,border-color .2s ease,transform .18s cubic-bezier(0.34,1.56,0.64,1),box-shadow .2s ease;margin-right:auto;box-shadow:0 1px 3px rgba(15,23,42,0.06);}
#btn-prev.qanim-prev-btn:hover:not(:disabled){background:#f8fafc;color:#1e293b;border-color:#94a3b8;box-shadow:0 2px 8px rgba(15,23,42,0.10);transform:translateY(-1px);}
#btn-prev.qanim-prev-btn:disabled{opacity:.38;cursor:not-allowed;box-shadow:none;}
</style>"""

_PREVSTEP_JS = """\
<script id="qanim-js-prevstep">
(function initPrevStepButton(){
  'use strict';
  if(window.__qanimPrevStepInit)return;window.__qanimPrevStepInit=true;
  function _updateBtn(){var pb=document.getElementById('btn-prev');if(!pb)return;var cur=typeof window.currentStep==='number'?window.currentStep:-1;pb.disabled=(cur<=0);}
  function _resumeRAFIfNeeded(){if(typeof window.qanimStartRAF==='function'){window.qanimStartRAF();return;}if(typeof window.startRAF==='function'){window.startRAF();return;}if(typeof window.animate==='function'){requestAnimationFrame(window.animate);return;}}
  window.prevStep=function(){if(typeof window.currentStep!=='number')return;if(window.currentStep<=0)return;window.currentStep--;if(typeof window.applyStep==='function')window.applyStep(window.currentStep);_resumeRAFIfNeeded();var nb=document.getElementById('btn-next');if(nb)nb.style.display='inline-block';};
  var _origApplyPrev=window.applyStep;
  if(typeof _origApplyPrev==='function'){window.applyStep=function(idx){_origApplyPrev(idx);_updateBtn();};}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  _onReady(function(){var pb=document.getElementById('btn-prev');if(pb){pb.removeAttribute('onclick');pb.addEventListener('click',function(e){e.stopPropagation();window.prevStep();});}  _updateBtn();});
})();
</script>"""


def inject_prev_step_button(html: str) -> str:
    if 'qanim-prevstep-styles' in html:
        return html
    if '</head>' in html:
        html = html.replace('</head>', _PREVSTEP_STYLES + '\n</head>', 1)
    btn_next_pattern = re.search(r'(<button[^>]*class="btn-primary"[^>]*id="btn-next"[^>]*>)', html)
    if btn_next_pattern:
        prev_btn = '<button class="btn-secondary qanim-prev-btn" id="btn-prev" disabled>&#x25C0; Previous Step</button>\n'
        html = html[:btn_next_pattern.start()] + prev_btn + html[btn_next_pattern.start():]
    elif 'id="btn-next"' in html:
        html = html.replace('id="btn-next"', 'id="btn-next-placeholder-REPLACED"', 1)
        html = html.replace('id="btn-next-placeholder-REPLACED"', 'id="btn-next"', 1)
    if '</body>' in html:
        html = html.replace('</body>', _PREVSTEP_JS + '\n</body>', 1)
    QAnimLogger.ok("PrevStep", "Previous step button injected")
    return html


# ===========================================================================
#  MODULE 18 — Nav Patch & Step Controller Injection
# ===========================================================================

_NAV_PATCH_JS = """\
<script id="__nav_patch__">
(function(){
  if(window.__navPatched)return;window.__navPatched=true;
  function showS(id){document.querySelectorAll('.csec').forEach(function(s){s.classList.remove('active');});var el=document.getElementById(id);if(el)el.classList.add('active');}
  function togAcc(el){var b=el.nextElementSibling;if(!b)return;var o=b.classList.contains('open');el.classList.toggle('open',!o);b.classList.toggle('open',!o);}
  function checkQ(btn,chosenIdx){var wrap=btn.closest('.qwrap')||btn.closest('.qblock');if(!wrap)return;if(wrap.getAttribute('data-answered')==='1')return;wrap.setAttribute('data-answered','1');var correctIdx=parseInt(wrap.getAttribute('data-correct')||'0',10);var opts=wrap.querySelectorAll('.qopt');opts.forEach(function(o){o.setAttribute('disabled','true');o.style.pointerEvents='none';});if(chosenIdx===correctIdx){btn.classList.add('correct');}else{btn.classList.add('wrong');if(opts[correctIdx])opts[correctIdx].classList.add('correct');}}
  if(typeof window.showS!=='function')window.showS=showS;
  if(typeof window.togAcc!=='function')window.togAcc=togAcc;
  if(typeof window.checkQ!=='function')window.checkQ=checkQ;
})();
</script>"""

_STEP_CONTROLLER_JS = """\
<script id="qanim-step-controller">
(function patchQAnimStepController(){
  "use strict";
  function initSC(){
    try{
      var scenes=[];
      for(var i=0;i<20;i++){var s=document.getElementById("scene-"+i);if(s){scenes.push(s);}else if(i>0){break;}}
      if(scenes.length<1){return;}
      var nextBtn=document.getElementById("nextbtn"),prevBtn=document.getElementById("prevbtn");
      if(!nextBtn||!prevBtn){return;}
      var cur=0;
      function showS(idx){if(idx<0||idx>=scenes.length)return;cur=idx;for(var j=0;j<scenes.length;j++)scenes[j].setAttribute("display",j===idx?"block":"none");var fn=window["animateScene"+idx];if(typeof fn==="function")requestAnimationFrame(function(){requestAnimationFrame(function(){try{fn();}catch(e){}});});}
      nextBtn.addEventListener("click",function(e){e.stopPropagation();if(cur<scenes.length-1)showS(cur+1);});
      prevBtn.addEventListener("click",function(e){e.stopPropagation();if(cur>0)showS(cur-1);});
      showS(0);
    }catch(err){}
  }
  if(document.readyState!=="loading")initSC();else document.addEventListener("DOMContentLoaded",initSC);
})();
</script>"""

# ─────────────────────────────────────────────────────────────────────────────
# MASTER STEP CONTROLLER (v3)
#
# This single script is the SOLE authority over step navigation.
# It runs at </body> (after all Gemini-generated script blocks), so:
#   • It can read stepsData / totalSteps from the Gemini scripts safely.
#   • It REPLACES window.nextStep with its own reliable version.
#   • It physically removes onclick="nextStep()" from btn-next at runtime.
#   • It syncs window.currentStep on every applyStep call.
#   • It triggers Scene7 (Step 7) when the user reaches the last SVG step.
#
# No regex post-processing needed — this script handles everything at runtime.
# ─────────────────────────────────────────────────────────────────────────────
_MASTER_STEP_CONTROLLER_JS = """\
<script id="qanim-master-step-controller">
/*
  QAnim Master Step Controller v4
  ================================
  This is the SOLE authority over step navigation for Steps 1-9.
  It runs as the LAST script before </body>, after all Gemini-generated
  code, and fixes every known Gemini hallucination:

  KNOWN GEMINI BUGS THIS FIXES:
  1. btn-next keeps onclick="nextStep()" → our clone+removeAttribute strips it
  2. Gemini's nextStep() calls non-existent qanim_showScene8/8/9 → aliased here
  3. Gemini's nextStep() uses currentStep < totalSteps-1 (off-by-one) → we own nextStep
  4. Gemini forgets to update currentStep inside nextStep() → we own currentStep
  5. totalSteps block-scoped var → we resolve from stepsData or DOM
  6. qanim_showScene8/qanim_showScene9 called instead of qanim_showScene7 → aliased
*/
(function(){
  'use strict';
  if(window.__qanimMasterCtrl)return;
  window.__qanimMasterCtrl=true;

  /* ── helpers ──────────────────────────────────────────────────── */
  function _el(id){return document.getElementById(id);}
  function _onReady(fn){
    if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',fn);
    else fn();
  }

  /* ── alias ALL Gemini hallucinated show-function names ─────────
     Gemini sometimes calls qanim_showScene8, qanim_showScene8,
     qanim_showScene9 instead of the correct qanim_showScene7.
     We alias them all so any of those calls opens the formula panel. */
  function _aliasShowFunctions(){
    // Wait until qanim_showScene7 is defined (injected by inject_scene7)
    function _doAlias(){
      var fn=window.qanim_showScene7;  // Step 7: Main Formula trigger
      if(typeof fn!=='function') return;
      // Alias old/wrong Gemini function names so they all open the formula panel
      if(typeof window.qanim_showScene6!=='function') window.qanim_showScene6=fn;  // Gemini old name
      if(typeof window.qanim_showScene8!=='function') window.qanim_showScene8=fn;  // Gemini wrong name
      if(typeof window.qanim_showScene9!=='function') window.qanim_showScene9=fn;  // Gemini wrong name
    }
    _doAlias();
    // Also alias after a short delay in case inject order varies
    setTimeout(_doAlias, 200);
  }

  /* ── resolve totalSteps from whatever Gemini generated ─────────
     Tries multiple sources in order of reliability. */
  function _resolveTotalSteps(){
    // 1. stepsData array (most reliable — actual data Gemini generated)
    if(typeof window.stepsData!=='undefined'&&Array.isArray(window.stepsData)&&window.stepsData.length>0)
      return window.stepsData.length-1;
    // 2. window.totalSteps set by _fix_total_steps regex pass
    if(typeof window.totalSteps==='number'&&window.totalSteps>0) return window.totalSteps;
    // 3. Count step-dot elements in the DOM
    var dots=document.querySelectorAll('.step-dot[id^="dot-step"]');
    // Only count SVG step dots (not the formula/sub/answer dots which have ids dot-step7/8/9)
    var svgDots=0;
    dots.forEach(function(d){
      var n=parseInt((d.id||'').replace('dot-step',''),10);
      if(!isNaN(n)&&n>=1&&n<=6) svgDots++;
    });
    if(svgDots>0) return svgDots-1;
    // 4. Safe fallback
    return 5;
  }

  /* ── trigger the formula panel (Step 7 overlay) ────────────────
     Called when user clicks Next on the last SVG step. */
  function _openFormulaPanel(){
    var ov6=_el('qanim-scene7-overlay');
    var ov7=_el('qanim-scene8-overlay');
    var ov9=_el('qanim-scene9-overlay');
    // Already open — don't re-trigger
    if((ov6&&ov6.classList.contains('qanim-scene-visible'))||
       (ov7&&ov7.classList.contains('qanim-scene-visible'))||
       (ov9&&ov9.classList.contains('qanim-scene-visible'))) return;
    if(typeof window.qanim_showScene7!=='function'){
      // qanim_showScene7 (formula panel) not yet defined — retry after short delay
      setTimeout(_openFormulaPanel, 150);
      return;
    }
    var svgC=document.querySelector('.svg-container');
    if(svgC){svgC.style.transition='opacity .45s ease';svgC.style.opacity='0';}
    setTimeout(function(){
      if(typeof window.qanim_showScene7==='function') window.qanim_showScene7();
    }, svgC?460:60);
  }

  /* ── our authoritative nextStep function ───────────────────────
     Completely replaces Gemini's nextStep(). Tracks currentStep
     correctly and triggers the formula panel at the right moment. */
  function _nextStep(){
    var ts=_resolveTotalSteps();
    var cs=typeof window.currentStep==='number'?window.currentStep:0;
    if(cs>=ts){
      _openFormulaPanel();
    } else {
      var next=cs+1;
      window.currentStep=next;
      if(typeof window.applyStep==='function') window.applyStep(next);
    }
  }

  /* ── wrap applyStep so it always syncs window.currentStep ──────
     Gemini's applyStep(idx) doesn't update window.currentStep.
     Our wrapper ensures every applyStep call keeps it in sync. */
  function _patchApplyStep(){
    var _orig=window.applyStep;
    if(typeof _orig!=='function'||_orig.__masterPatched) return;
    window.applyStep=function(idx){
      window.currentStep=idx;   // sync BEFORE calling original
      _orig(idx);
    };
    window.applyStep.__masterPatched=true;
  }

  /* ── wire btn-next: clone to strip all listeners, then add ours ─
     Cloning is the only reliable way to remove both inline onclick
     AND any previously addEventListener'd handlers. */
  function _wireNextBtn(){
    var btn=_el('btn-next');
    if(!btn) return;
    var fresh=btn.cloneNode(true);
    fresh.removeAttribute('onclick');
    btn.parentNode.replaceChild(fresh,btn);
    fresh.addEventListener('click',function(e){
      e.stopPropagation();
      _nextStep();
    });
  }

  /* ── expose block-scoped Gemini vars as window globals ─────────
     Gemini uses 'var stepsData' inside a <script> block which is
     NOT automatically on window. We hoist it here. */
  function _exposeGlobals(){
    try{if(typeof window.stepsData==='undefined'&&typeof stepsData!=='undefined') window.stepsData=stepsData;}catch(e){}
    try{if(typeof window.totalSteps==='undefined'&&typeof totalSteps!=='undefined') window.totalSteps=totalSteps;}catch(e){}
    try{if(typeof window.currentStep==='undefined'&&typeof currentStep!=='undefined') window.currentStep=currentStep;}catch(e){}
  }

  /* ── main init (runs at DOMContentLoaded) ───────────────────── */
  _onReady(function(){
    _exposeGlobals();
    // Re-resolve now that stepsData is exposed
    window.totalSteps=_resolveTotalSteps();
    if(typeof window.currentStep!=='number') window.currentStep=0;
    _patchApplyStep();
    _wireNextBtn();
    // Take full ownership of window.nextStep
    window.nextStep=_nextStep;
    // Alias hallucinated function names to the real trigger
    _aliasShowFunctions();
  });
})();
</script>"""

_SCENE7_AUTOTRIGGER_JS = ""  # Replaced entirely by _MASTER_STEP_CONTROLLER_JS (v4)


def inject_nav_patch_and_scene_desc(html: str) -> str:
    if '__nav_patch__' in html:
        return html
    if '</body>' in html:
        html = html.replace('</body>', _NAV_PATCH_JS + '\n</body>', 1)
    else:
        html = html + _NAV_PATCH_JS
    QAnimLogger.ok("NavPatch", "Nav patch injected")
    return html


def inject_step_controller(html: str) -> str:
    if 'qanim-master-step-controller' in html:
        return html

    # Remove old autotrigger — replaced by master controller
    html = re.sub(
        r'<script id="qanim-js-scene7-autotrigger">.*?</script>',
        '', html, flags=re.DOTALL
    )

    # If the old step-controller no-op is present, also remove it —
    # the master controller subsumes all its responsibilities
    html = re.sub(
        r'<script id="qanim-step-controller">.*?</script>',
        '', html, flags=re.DOTALL
    )

    # Inject legacy step-controller stub + master controller as the last scripts
    payload = _STEP_CONTROLLER_JS + '\n' + _MASTER_STEP_CONTROLLER_JS
    if '</body>' in html:
        html = html.replace('</body>', payload + '\n</body>', 1)
    else:
        html = html + payload

    QAnimLogger.ok("StepController", "Master step controller injected (v4)")
    return html


# ===========================================================================
#  MODULE 19 — GeminiGlossaryAnalyzer
# ===========================================================================

_GLOSSARY_SYSTEM_GEMINI = """Find DIFFICULT or TECHNICAL words in the question that could confuse a student.
Return ONLY valid JSON:
{"terms": [{"term": "word", "meaning": "simple explanation in 15 words or less"}]}

Rules:
- Pick 2-8 genuinely hard/technical/jargon words only.
- Write meanings in very simple, everyday English.
- If no hard words found, return {"terms": []}.
- Pure JSON only — no markdown, no fences."""


class GeminiGlossaryAnalyzer:
    @classmethod
    def analyze(cls, question: str) -> dict:
        if _gemini_client is None:
            return {"terms": []}
        MAX_ATTEMPTS = 3
        last_raw = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = GeminiSolutionGenerator._call_gemini(
                    f"Question: {question[:800]}",
                    _GLOSSARY_SYSTEM_GEMINI,
                    max_tokens=1536,
                )
                last_raw = raw
                cleaned = _sanitize_json_str(raw)
                data = json.loads(cleaned)
                terms = []
                for t in (data.get("terms") or [])[:8]:
                    term    = str(t.get("term", "") or "").strip()
                    meaning = str(t.get("meaning", "") or "").strip()
                    if term and meaning:
                        terms.append({"term": term, "meaning": meaning})
                QAnimLogger.ok("GlossaryAnalyzer", f"Found {len(terms)} difficult word(s) (attempt {attempt})")
                return {"terms": terms}
            except Exception as e:
                QAnimLogger.warn("GlossaryAnalyzer", f"Attempt {attempt}/{MAX_ATTEMPTS} failed: {e}")
                if attempt < MAX_ATTEMPTS:
                    continue
        QAnimLogger.error("GlossaryAnalyzer", "All attempts failed — skipping glossary")
        return {"terms": []}

    @classmethod
    async def analyze_async(cls, question: str) -> dict:
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, cls.analyze, question),
                timeout=STAGE_TIMEOUT_SMALL,
            )
        except asyncio.TimeoutError:
            QAnimLogger.error("GlossaryAnalyzer", f"Stage exceeded {STAGE_TIMEOUT_SMALL}s — skipping glossary")
            return {"terms": []}


def inject_glossary(html: str, glossary_terms: list) -> str:
    if not glossary_terms:
        return html
    if 'qanim-glossary-styles' in html:
        return html
    terms_json = json.dumps(glossary_terms, ensure_ascii=False)
    glossary_css = """\
<style id="qanim-glossary-styles">
#qanim-glossary-backdrop{display:none;position:fixed;inset:0;z-index:8200;background:rgba(15,23,42,.45);backdrop-filter:blur(5px);opacity:0;transition:opacity .22s ease;}
#qanim-glossary-backdrop.open{display:block;opacity:1;}
#qanim-glossary-panel{position:fixed;top:50%;left:50%;transform:translate(-50%,-48%) scale(.96);z-index:8300;width:min(500px,94vw);max-height:80vh;border-radius:16px;padding:24px;box-sizing:border-box;background:#fff;border:1px solid #e2e8f0;box-shadow:0 8px 48px rgba(0,0,0,.14);opacity:0;pointer-events:none;display:none;transition:opacity .25s ease,transform .25s cubic-bezier(.34,1.56,.64,1);overflow-y:auto;}
#qanim-glossary-panel.open{opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);display:block;}
.gloss-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;}
.gloss-title{font-size:16px;font-weight:800;color:#1e293b;font-family:-apple-system,'Segoe UI',Arial,sans-serif;}
.gloss-close{cursor:pointer;font-size:14px;color:#64748b;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:8px;padding:4px 10px;}
.gloss-item{padding:12px 0;border-bottom:1px solid #f1f5f9;}
.gloss-term{font-size:13px;font-weight:800;color:#0e7490;margin-bottom:3px;font-family:-apple-system,'Segoe UI',Arial,sans-serif;}
.gloss-meaning{font-size:12.5px;color:#475569;line-height:1.55;font-family:-apple-system,'Segoe UI',Arial,sans-serif;}
</style>"""
    glossary_dom = f"""
<div id="qanim-glossary-backdrop" onclick="if(typeof closeGlossary==='function')closeGlossary()"></div>
<div id="qanim-glossary-panel" aria-hidden="true">
  <div class="gloss-header">
    <div class="gloss-title">&#x1F4DA; Key Terms</div>
    <button class="gloss-close" onclick="if(typeof closeGlossary==='function')closeGlossary()">&#x2715; Close</button>
  </div>
  <div id="qanim-glossary-items"></div>
</div>
<script type="application/json" id="__glossary_data__">{terms_json}</script>"""
    glossary_js = """\
<script id="qanim-js-glossary">
(function initGlossary(){
  'use strict';
  if(window.__qanimGlossaryInit)return;window.__qanimGlossaryInit=true;
  var open=false,built=false;
  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _build(){if(built)return;built=true;var tag=_el('__glossary_data__');if(!tag)return;var terms=[];try{terms=JSON.parse(tag.textContent)||[];}catch(e){}var c=_el('qanim-glossary-items');if(!c)return;var html='';for(var i=0;i<terms.length;i++){html+='<div class="gloss-item"><div class="gloss-term">'+terms[i].term+'</div><div class="gloss-meaning">'+terms[i].meaning+'</div></div>';}c.innerHTML=html;}
  function openGlossary(){_build();var bd=_el('qanim-glossary-backdrop'),pn=_el('qanim-glossary-panel');if(bd)bd.classList.add('open');if(pn){pn.classList.add('open');pn.setAttribute('aria-hidden','false');}open=true;}
  function closeGlossary(){var bd=_el('qanim-glossary-backdrop'),pn=_el('qanim-glossary-panel');if(bd)bd.classList.remove('open');if(pn){pn.classList.remove('open');pn.setAttribute('aria-hidden','true');}open=false;}
  window.openGlossary=openGlossary;window.closeGlossary=closeGlossary;
  _onReady(function(){
    var btn=_el('glossary-ctrl-btn');
    if(btn){btn.removeAttribute('onclick');btn.addEventListener('click',function(e){e.stopPropagation();open?closeGlossary():openGlossary();});}
    document.addEventListener('keydown',function(e){if(e.key==='Escape'&&open)closeGlossary();});
  });
})();
</script>"""
    if '</head>' in html:
        html = html.replace('</head>', glossary_css + '\n</head>', 1)
    if '<body' in html:
        idx = html.find('>', html.find('<body')) + 1
        html = html[:idx] + glossary_dom + html[idx:]
    if '</body>' in html:
        html = html.replace('</body>', glossary_js + '\n</body>', 1)
    QAnimLogger.ok("Glossary", f"Glossary injected ({len(glossary_terms)} terms)")
    return html


# ===========================================================================
#  MODULE 20 — Topic Classifier & Scene Count Detector
# ===========================================================================

async def _classify_topic_async(question: str) -> str:
    q = question.lower()
    TOPICS = [
        (["mass transfer", "evaporation", "concentration", "diffusion"],       "ENGINEERING"),
        (["heat transfer", "thermal", "conduction", "convection", "radiation"], "ENGINEERING"),
        (["fluid", "flow", "pressure", "viscosity", "bernoulli"],              "ENGINEERING"),
        (["gear", "crank", "mechanism", "slider", "linkage", "cam"],           "ENGINEERING"),
        (["force", "newton", "velocity", "acceleration", "momentum"],          "PHYSICS"),
        (["circuit", "voltage", "current", "resistance", "ohm", "capacitor"],  "PHYSICS"),
        (["integral", "derivative", "matrix", "calculus", "theorem"],          "MATH"),
        (["cell", "dna", "protein", "photosynthesis", "enzyme"],               "BIOLOGY"),
    ]
    for keywords, label in TOPICS:
        if any(k in q for k in keywords):
            return label
    return "ENGINEERING"


def _detect_scene_count(question: str) -> int:
    return 9  # Always 9 steps in the new workflow


# ===========================================================================
#  MODULE 21 — GeminiAnimationBuilder (The 9-Step HTML Generator)
# ===========================================================================

_ANIMATION_BUILDER_SYSTEM = """You are QAnim HTML Generator v2.0.

Given a JSON scene script and a question, produce a COMPLETE, self-contained HTML file 
that implements the exact 9-step workflow described below.

════════════════════════════════════════
CRITICAL: The HTML must implement the exact same layout as the Convective Heat Loss 
reference animation (Convective_Heat_Loss_Updated.html). The core structure is:

1. A fixed question banner at the top.
2. A dashboard card with:
   - Step indicator row (9 dots numbered 1-9)
   - Progress bar
   - SVG animation panel (Steps 1-6 — the scene_script.steps data)
   - Control panel at bottom:
     - Info box (step description + badges)
     - Navigation buttons (Prev / Next)
3. When "Next" is clicked on the last SVG step (Step 6), the SVG fades out
   and Scene7 overlay opens (Step 7: Main Formula).
4. From Scene7, user advances to Scene8 (Step 8: Substitution).
5. From Scene8, user advances to Scene9 (Step 9: Final Answer).

════════════════════════════════════════
COMPLETE HTML STRUCTURE REQUIRED:
════════════════════════════════════════

<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>[Title from scene_script.title]</title>
  <style>/* Full CSS — see below */</style>
</head>
<body>
  <!-- 1. Question Banner -->
  <div class="question-banner">
    <div class="q-label">Problem Statement</div>
    <div class="q-text">[The full question text]</div>
  </div>

  <!-- 2. Dashboard Card -->
  <div class="dashboard">
    <!-- Step Indicator (9 dots) -->
    <div class="step-indicator">
      <div class="step-dot active" onclick="goToStep(0)">Step 1</div>
      <div class="step-dot" onclick="goToStep(1)">Step 2</div>
      ... through Step 9
    </div>
    <!-- Progress bar -->
    <div class="step-progress-wrap">
      <div class="step-progress-bar" id="step-bar" style="width:11.1%;"></div>
    </div>
    <!-- Step label -->
    <div id="step-label" style="font-size:12px;font-weight:700;color:#64748b;margin-bottom:14px;text-align:center;">Step 1 of 9</div>
    
    <!-- SVG Animation Area -->
    <div class="svg-container">
      <svg id="main-svg" viewBox="0 0 850 478" xmlns="http://www.w3.org/2000/svg">
        <!-- ALL SVG layers defined here based on scene_script.svg_components -->
        <!-- Each layer group has id matching the layer key -->
        <!-- Initially only layer-frame is visible (opacity 1), others opacity 0 -->
      </svg>
    </div>
    
    <!-- Control Panel (bottom) -->
    <div class="control-panel">
      <!-- Info Box -->
      <div class="info-box" id="info-box">
        <h3 id="info-title">Step 1: [title from step 1]</h3>
        <div class="info-desc" id="info-desc">[description from step 1]</div>
        <div class="badge-row" id="badge-row">
          <!-- Badges from step 1 -->
        </div>
      </div>
      <!-- Navigation buttons -->
      <!-- IMPORTANT: btn-next must NOT have an inline onclick attribute.       -->
      <!-- The injected __nexttrigger_patch__ script wires it via addEventListener -->
      <!-- so window.nextStep (which handles the scene7 transition) is used.    -->
      <div class="action-row" style="display:flex;gap:10px;margin-top:16px;justify-content:flex-end;">
        <button class="btn-secondary" id="btn-prev" disabled onclick="prevStep()">&#x25C0; Prev</button>
        <button class="btn-primary" id="btn-next">Next &#x25B6;</button>
      </div>
    </div>
  </div>
</body>
</html>

════════════════════════════════════════
CSS REQUIREMENTS (embed in <style>):
════════════════════════════════════════
- body: font -apple-system/'Segoe UI', background: #eef2f9, min-height:100vh
- .question-banner: white bg, padding 24px 30px, border-bottom 1px solid #e2e8f0
- .dashboard: background white, border-radius 16px, max-width 900px, margin 0 auto 20px, box-shadow
- .svg-container: border-radius 12px, overflow hidden, aspect-ratio 850/478, background #f0f5ff
- svg: width 100%, height 100%
- .step-dot: pill buttons, 9 total, .active has teal gradient background
- .step-progress-wrap: height 6px, background #e2e8f0, border-radius 3px
- .step-progress-bar: height 100%, transition width .4s, background linear-gradient(teal→cyan)
- .control-panel: padding 20px 24px, background #f8faff, border-top 1px solid #e8eef8
- .info-box: white bg, border 1px solid #dde6f8, border-left 4px solid #0891b2, border-radius 10px, padding 18px 20px
- .info-box h3: font-size 18px, font-weight 900, color #0f172a
- .info-desc: font-size 14px, color #334155, line-height 1.7
- .badge-row: display flex, flex-wrap wrap, gap 8px, margin-top 12px
- .badge: padding 5px 12px, border-radius 20px, font-size 12px, font-weight 700
- .badge.cyan: background #e0f2fe, color #0369a1, border 1px solid #bae6fd
- .badge.orange: background #fff7ed, color #c2410c, border 1px solid #fed7aa
- .badge.green: background #f0fdf4, color #15803d, border 1px solid #bbf7d0
- .btn-primary: background linear-gradient(135deg,#0e7490,#0891b2), color white, padding 11px 24px, border-radius 8px, border none, font-weight 700, cursor pointer
- .btn-secondary: same but transparent bg, color #64748b, border 1px solid #cbd5e1
- SVG layer transitions: use CSS: #layer-X { transition: opacity 0.5s ease; }

════════════════════════════════════════
JAVASCRIPT REQUIREMENTS:
════════════════════════════════════════

var stepsData = [/* Array of ALL step objects from scene_script.steps */];
window.currentStep = 0;   // MUST be on window — other scripts read window.currentStep
var currentStep = window.currentStep;
window.totalSteps = stepsData.length - 1; // MUST be on window — other scripts read window.totalSteps
var totalSteps = window.totalSteps;

function applyStep(idx) {
  // CRITICAL: sync window.currentStep on EVERY call
  currentStep = idx;
  window.currentStep = idx;
  // 1. Update step dots (active class on current, done class on past)
  // 2. Update progress bar width: (idx+1)/9 * 100 + '%'  (9 = total steps including scenes 7,8,9)
  // 3. Update step label text: 'Step N of 9'
  // 4. Show/hide SVG layers based on stepsData[idx].components_visible
  //    - Layer in components_visible: opacity = 1
  //    - Layer NOT in components_visible: opacity = (blur_background ? 0.38 : 0)
  //    - Layer in components_new: add a subtle scale/glow animation
  // 5. Update info-box: title, description, badges
  // 6. Update body data-step attribute for CSS theming
  // 7. Update Prev button disabled state
  // 8. If idx === totalSteps (last SVG step), change Next button text to 'Step 7: Formula →'
  //    Otherwise Next button text = 'Next ▶'
  document.body.setAttribute('data-step', idx);
}

function nextStep() {
  if (currentStep < totalSteps) {
    currentStep++;
    applyStep(currentStep);
  } else {
    // At last SVG step — trigger Scene 6
    triggerScene7();
  }
}

function prevStep() {
  if (currentStep > 0) {
    currentStep--;
    applyStep(currentStep);
  }
}

function goToStep(idx) {
  if (idx <= totalSteps) {
    currentStep = idx;
    applyStep(idx);
  }
}

function triggerScene7() {
  var svgCont = document.querySelector('.svg-container');
  if (svgCont) {
    svgCont.style.transition = 'opacity .45s ease';
    svgCont.style.opacity = '0';
  }
  setTimeout(function() {
    if (typeof window.qanim_showScene7 === 'function') window.qanim_showScene7();
  }, 460);
}

// Initialize on load
window.addEventListener('DOMContentLoaded', function() {
  applyStep(0);
});

════════════════════════════════════════
SVG SCENE DESIGN RULES:
════════════════════════════════════════

For each scene_script.svg_components entry, create a <g id="[key]"> group in the SVG.

SVG viewBox is 0 0 850 478. Design a realistic, detailed physical scene specific to the problem domain:

- HEAT TRANSFER problems: Show a plate/wall, temperature arrows, fluid flow, gradient colors
- CIRCUIT problems: Show circuit elements, wires, voltage/current labels
- FLUID MECHANICS: Show pipes, flow arrows, pressure gauges
- MECHANICS: Show gears, forces, motion arrows
- CHEMISTRY: Show beakers, molecules, reaction arrows

EVERY layer group must have initial opacity set correctly in the SVG element:
- layer-frame: opacity="1" (always visible from start)
- all other layers: opacity="0" (revealed by JS as steps advance)

Each layer must contain RICH SVG content:
- Gradients, patterns, realistic shapes
- Text labels with symbols and values
- Callout boxes for given data (rounded rects + text)
- Arrows and flow indicators
- Color-coded to match the accent_color from scene_script.svg_components

═══════════════════════════
IMPORTANT CONSTRAINTS:
═══════════════════════════
1. The HTML must be COMPLETELY SELF-CONTAINED (no external dependencies except MathJax CDN if needed).
2. The stepsData array MUST contain EXACTLY the steps from scene_script.steps — no more, no fewer.
3. ALL SVG layer IDs must match the keys in scene_script.svg_components.
4. var totalSteps MUST equal stepsData.length - 1  (i.e. the last index, NOT the count).
   If stepsData has 6 entries, totalSteps = 5. If 7 entries, totalSteps = 6. NEVER set totalSteps = stepsData.length.
5. The step indicator must show exactly 9 dots (Steps 1-9), where dots 7-9 represent the formula/substitution/answer scenes.
6. Do NOT pre-build Scene7/8/9 overlays in this HTML — they will be injected by the Python pipeline.
7. btn-next MUST NOT have an inline onclick attribute. Write it as:
      <button class="btn-primary" id="btn-next">Next ▶</button>
   The Python pipeline wires it via addEventListener. Adding onclick="nextStep()" will BREAK Steps 7/8/9.
8. The step-bar progress tracks all 9 steps: width = (currentStep+1)/9 * 100 + '%'
9. In nextStep(), ALWAYS update currentStep BEFORE calling applyStep:
      function nextStep() {
        if (currentStep < totalSteps) {
          currentStep++;
          window.currentStep = currentStep;
          applyStep(currentStep);
        } else {
          if (typeof window.qanim_showScene7 === 'function') window.qanim_showScene7();
        }
      }
   CRITICAL: use  currentStep < totalSteps  (NOT < totalSteps-1).
   CRITICAL: ALWAYS call window.qanim_showScene7() — NEVER qanim_showScene6/8/9 — only qanim_showScene7 is correct.
   CRITICAL: ALWAYS set window.currentStep = currentStep inside applyStep AND nextStep.
10. In applyStep(idx), ALWAYS sync window.currentStep:
      function applyStep(idx) {
        currentStep = idx;
        window.currentStep = idx;
        /* ... rest of your applyStep logic ... */
      }

Return ONLY the complete HTML — no preamble, no explanation, no markdown fences."""


class GeminiAnimationBuilder:

    @classmethod
    def build(cls, question: str, scene_script: dict, sol: dict, topic: str = "ENGINEERING") -> str:
        if _gemini_client is None:
            return RecoveryEngine.fallback_html(question, "Gemini client not available")
        MAX_ATTEMPTS = 3
        last_err = ""
        last_raw = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                QAnimLogger.info("AnimBuilder", f"Build attempt {attempt}/{MAX_ATTEMPTS}...")
                prompt = cls._build_prompt(question, scene_script, sol, topic)
                raw = GeminiSolutionGenerator._call_gemini(prompt, _ANIMATION_BUILDER_SYSTEM, max_tokens=MAX_TOK)
                last_raw = raw
                raw = raw.strip()
                raw = re.sub(r'^```(?:html)?\s*\n?', '', raw, flags=re.IGNORECASE)
                raw = re.sub(r'\n?```\s*$', '', raw)
                raw = DocumentSkeletonNormalizer.normalize(raw)
                if 'stepsData' not in raw:
                    raise ValueError("Missing stepsData in generated HTML")
                if '<svg' not in raw.lower():
                    raise ValueError("Missing <svg> tag in generated HTML")
                if len(raw) < 3000:
                    raise ValueError(f"HTML too short ({len(raw)} chars)")
                QAnimLogger.ok("AnimBuilder", f"HTML generated: {len(raw)} chars (attempt {attempt})")
                return raw
            except Exception as e:
                last_err = _err_msg(e)
                QAnimLogger.warn("AnimBuilder", f"Attempt {attempt} failed: {last_err}")
                if attempt < MAX_ATTEMPTS:
                    import time as _t
                    _t.sleep(20 * attempt)
                    continue
        QAnimLogger.error("AnimBuilder", f"All attempts failed: {last_err}")
        return RecoveryEngine.fallback_html(question, f"HTML generation failed: {last_err}")

    @classmethod
    def _build_prompt(cls, question: str, scene_script: dict, sol: dict, topic: str) -> str:
        steps_summary = []
        for s in scene_script.get("steps", []):
            steps_summary.append({
                "step_number": s.get("step_number"),
                "label": s.get("label"),
                "title": s.get("title"),
                "description": s.get("description"),
                "badges": s.get("badges", []),
                "components_visible": s.get("components_visible", []),
                "components_new": s.get("components_new", []),
                "focus_component": s.get("focus_component"),
                "blur_background": s.get("blur_background", False),
            })
        svg_components_summary = {}
        for k, v in scene_script.get("svg_components", {}).items():
            svg_components_summary[k] = {
                "description": v.get("description", ""),
                "motion_type": v.get("motion_type", "static"),
                "accent_color": v.get("accent_color", "#0891b2"),
                "layer_order": v.get("layer_order", 1),
                "labels": v.get("labels", []),
            }
        formula_data = scene_script.get("formula_data", {})
        final_answer = scene_script.get("final_answer", sol.get("final_answer", ""))
        script_json = json.dumps({
            "title": scene_script.get("title", "Physics Animation"),
            "topic": topic,
            "steps": steps_summary,
            "svg_components": svg_components_summary,
            "final_answer": final_answer,
            "key_insight": scene_script.get("key_insight", ""),
        }, ensure_ascii=False, indent=2)

        return f"""Question to animate:
\"\"\"{question[:1200]}\"\"\"

Scene Script (JSON):
{script_json}

IMPORTANT RENDERING NOTES:
- The SVG layers must be detailed and visually rich for the problem domain: {topic}
- The governing formula is: {formula_data.get('formula_text', 'See formula_data')}
- The final answer is: {final_answer}
- Step 6 (the last SVG step) must show ALL layers visible (no blur) + two callout boxes:
  LEFT callout: all given parameters with their values
  RIGHT callout: the unknown quantity (marked with ?)
- The Next button text on step 6 should read "Step 7: Main Formula →"
- Dots 7, 8, 9 in the step indicator are placeholders for the formula/substitution/answer scenes
  (they will be activated by injected JS — just include them as inactive dots)

Generate the complete, self-contained HTML now."""

    @classmethod
    async def build_async(cls, question: str, scene_script: dict, sol: dict, topic: str = "ENGINEERING") -> str:
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, cls.build, question, scene_script, sol, topic),
                timeout=STAGE_TIMEOUT_BUILD,
            )
        except asyncio.TimeoutError:
            QAnimLogger.error("AnimBuilder", f"Build stage exceeded {STAGE_TIMEOUT_BUILD}s — using fallback")
            return RecoveryEngine.fallback_html(question, f"Animation build timed out after {STAGE_TIMEOUT_BUILD}s")


# ===========================================================================
#  MODULE 22 — PanelReliabilityEngine (post-processing passes)
# ===========================================================================

class PanelReliabilityEngine:

    @staticmethod
    def _fix_step_dot_sync(html: str) -> str:
        if 'data-step' not in html:
            patch = """\
<script id="__datastep_patch__">
(function(){
  var _orig = window.applyStep;
  if(typeof _orig !== 'function') return;
  window.applyStep = function(idx) {
    _orig(idx);
    document.body.setAttribute('data-step', idx);
  };
})();
</script>"""
            if '</body>' in html:
                html = html.replace('</body>', patch + '\n</body>', 1)
        return html

    @staticmethod
    def _fix_next_button_trigger(html: str) -> str:
        # Best-effort static pre-pass: strip onclick="nextStep()" from btn-next
        # in the raw HTML so it isn't present even before the master controller
        # runs. The master controller (_MASTER_STEP_CONTROLLER_JS) handles this
        # at runtime too via btn.removeAttribute('onclick') + node clone, so
        # even if this regex doesn't match, the runtime fix covers it.
        html = re.sub(
            r'(<button[^>]*id=["\']btn-next["\'][^>]*)\s+onclick=["\'][^"\']*["\']',
            r'\1',
            html
        )
        html = re.sub(
            r'(<button[^>]*onclick=["\'][^"\']*["\'][^>]*id=["\']btn-next["\'][^>]*)',
            lambda m: re.sub(r'\s+onclick=["\'][^"\']*["\']', '', m.group(0)),
            html
        )
        return html

    @staticmethod
    def _fix_svg_xmlns(html: str) -> str:
        html = re.sub(
            r'<svg(?![^>]*xmlns)',
            '<svg xmlns="http://www.w3.org/2000/svg"',
            html, flags=re.IGNORECASE
        )
        return html

    @staticmethod
    def _fix_badge_classes(html: str) -> str:
        html = re.sub(r'class="badge\s+gc-blue"',  'class="badge cyan"',   html)
        html = re.sub(r'class="badge\s+gc-teal"',  'class="badge cyan"',   html)
        html = re.sub(r'class="badge\s+gc-amber"', 'class="badge orange"', html)
        html = re.sub(r'class="badge\s+gc-green"', 'class="badge green"',  html)
        return html

    @staticmethod
    def _fix_step_bar_id(html: str) -> str:
        if 'id="step-bar"' not in html and 'id=\'step-bar\'' not in html:
            html = re.sub(
                r'class="step-progress-bar"(?!\s*id=)',
                'class="step-progress-bar" id="step-bar"',
                html, count=1
            )
        return html

    @staticmethod
    def _fix_js_apostrophes(html: str) -> str:
        return JsSyntaxValidator.auto_fix_stray_apostrophes(html)

    @staticmethod
    def _fix_svg_layer_opacity(html: str, scene_script: dict) -> str:
        components = scene_script.get("svg_components", {})
        for layer_id, info in components.items():
            if layer_id == "layer-frame":
                continue
            pattern = re.compile(
                rf'<g\s+id="{re.escape(layer_id)}"([^>]*?)opacity=["\']1["\']',
                re.IGNORECASE
            )
            if pattern.search(html):
                html = pattern.sub(
                    lambda m: f'<g id="{layer_id}"{m.group(1)}opacity="0"',
                    html
                )
        return html

    @staticmethod
    def _fix_total_steps(html: str, scene_script: dict) -> str:
        # Expose totalSteps/currentStep as window globals so our master
        # controller can read them regardless of Gemini's block scoping.
        # Also patch the Gemini nextStep() body to use window.currentStep
        # so it stays in sync even if the master controller hasn't run yet.
        n_steps = len(scene_script.get("steps", []))
        if n_steps < 1:
            return html
        last_idx = n_steps - 1

        # Replace literal integer: var totalSteps = 7;  (any value Gemini generates)
        html = re.sub(
            r'var\s+totalSteps\s*=\s*\d+\s*;',
            f'var totalSteps = {last_idx}; window.totalSteps = {last_idx};',
            html
        )
        # Replace dynamic: var totalSteps = stepsData.length - 1;
        html = re.sub(
            r'var\s+totalSteps\s*=\s*stepsData\.length\s*-\s*1\s*;',
            f'var totalSteps = {last_idx}; window.totalSteps = {last_idx};',
            html
        )
        # Expose currentStep as window global
        html = re.sub(
            r'var\s+currentStep\s*=\s*0\s*;',
            'var currentStep = 0; window.currentStep = 0;',
            html, count=1
        )
        return html

    @staticmethod
    def _fix_step_label_id(html: str) -> str:
        if 'id="step-label"' not in html:
            html = re.sub(
                r'(<div[^>]*class="step-progress-wrap"[^>]*>)',
                r'\1\n<div id="step-label" style="font-size:12px;font-weight:700;color:#64748b;text-align:center;margin-bottom:12px;padding-top:6px;">Step 1 of 9</div>',
                html, count=1
            )
        return html

    @staticmethod
    def _inject_mathjax_if_needed(html: str) -> str:
        if ('\\(' in html or '\\[' in html or r'\frac' in html) and 'MathJax' not in html:
            mathjax_tag = """\
<script>
MathJax = {tex: {inlineMath: [['\\\\(','\\\\)'],['$','$']]}, svg: {fontCache:'global'}};
</script>
<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>"""
            if '</head>' in html:
                html = html.replace('</head>', mathjax_tag + '\n</head>', 1)
        return html

    @classmethod
    def run_all_passes(cls, html: str, scene_script: dict) -> str:
        QAnimLogger.info("PanelReliability", "Running post-processing passes...")
        # Static regex passes — must run BEFORE master controller injection
        html = cls._fix_step_dot_sync(html)
        html = cls._fix_next_button_trigger(html)   # best-effort static onclick strip
        html = cls._fix_svg_xmlns(html)
        html = cls._fix_badge_classes(html)
        html = cls._fix_step_bar_id(html)
        html = cls._fix_step_label_id(html)
        html = cls._fix_svg_layer_opacity(html, scene_script)
        html = cls._fix_total_steps(html, scene_script)  # injects window.totalSteps/currentStep
        html = cls._inject_mathjax_if_needed(html)
        # Master controller MUST be the last injection so it runs after all
        # Gemini-generated scripts and sees the final DOM state at DOMContentLoaded.
        html = inject_step_controller(html)
        QAnimLogger.ok("PanelReliability", "All passes complete")
        return html


# ===========================================================================
#  MODULE 23 — MathTypography (Unicode symbol upgrader)
# ===========================================================================

class MathTypography:
    _REPLACEMENTS = [
        (r'\bDelta\b',   'Δ'),
        (r'\bTheta\b',   'Θ'),
        (r'\btheta\b',   'θ'),
        (r'\balpha\b',   'α'),
        (r'\bbeta\b',    'β'),
        (r'\bgamma\b',   'γ'),
        (r'\blambda\b',  'λ'),
        (r'\bmu\b',      'μ'),
        (r'\brho\b',     'ρ'),
        (r'\bsigma\b',   'σ'),
        (r'\bpi\b',      'π'),
        (r'\bomega\b',   'ω'),
        (r'\bphi\b',     'φ'),
        (r'\binfty\b',   '∞'),
        (r'\bsqrt\b',    '√'),
        (r'!=',          '≠'),
        (r'<=',          '≤'),
        (r'>=',          '≥'),
        (r'\+-',         '±'),
        (r'\.{3}',       '…'),
        (r"T_s\b",       'Tₛ'),
        (r"T_inf\b",     'T∞'),
        (r"\bT_inf\b",   'T∞'),
        (r"m\^2",        'm²'),
        (r"m\^3",        'm³'),
        (r"\bdeg C\b",   '°C'),
        (r"\bdeg F\b",   '°F'),
        (r"\bdeg K\b",   'K'),
    ]

    @classmethod
    def upgrade(cls, text: str) -> str:
        if not text:
            return text
        for pattern, replacement in cls._REPLACEMENTS:
            try:
                text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
            except Exception:
                pass
        return text

    @classmethod
    def upgrade_html_badges(cls, html: str) -> str:
        return html  # conservative — don't modify SVG/JS context


# ===========================================================================
#  MODULE 24 — Main Pipeline: generate_animation_html()
# ===========================================================================

async def generate_animation_html(question: str) -> str:
    """
    Full 9-step QAnim pipeline.

    Stage A: Parallel — scene analysis + solution generation + glossary analysis
    Stage B: Build HTML (GeminiAnimationBuilder)
    Stage C: Post-processing — inject all 9-step panels + controls
    Stage D: Reliability passes, sanitization, centering CSS

    Returns complete self-contained HTML string.
    """
    QAnimLogger.info("Pipeline", f"Starting 9-step pipeline for: {question[:80]!r}")
    if not question or not question.strip():
        return RecoveryEngine.fallback_html("(empty)", "Question was empty")

    question = LargeInputPreprocessor.compress(question) if LargeInputPreprocessor.needs_compression(question) else question
    to_find_targets = ToFindExtractor.extract(question)
    given_values    = GivenValuesExtractor.extract(question)

    QAnimLogger.info("Pipeline", "Stage A: Parallel scene analysis + solution + glossary...")
    try:
        scene_task    = GeminiSceneAnalyzer.analyze_async(question)
        sol_task      = GeminiSolutionGenerator.generate_async(question)
        glossary_task = GeminiGlossaryAnalyzer.analyze_async(question)
        scene_script, sol, glossary_result = await asyncio.gather(
            scene_task, sol_task, glossary_task,
            return_exceptions=True,
        )
    except Exception as e:
        QAnimLogger.error("Pipeline", f"Stage A failed catastrophically: {e}")
        return RecoveryEngine.fallback_html(question, f"Analysis stage failed: {_err_msg(e)}")

    if isinstance(scene_script, BaseException):
        QAnimLogger.warn("Pipeline", f"Scene analysis failed: {scene_script} — using fallback")
        scene_script = GeminiSceneAnalyzer._fallback_script(question)
    if isinstance(sol, BaseException):
        QAnimLogger.warn("Pipeline", f"Solution generation failed: {sol} — using fallback")
        sol = dict(GeminiSolutionGenerator._FALLBACK)
    if isinstance(glossary_result, BaseException):
        QAnimLogger.warn("Pipeline", f"Glossary analysis failed: {glossary_result} — skipping")
        glossary_result = {"terms": []}

    glossary_terms = glossary_result.get("terms", []) if isinstance(glossary_result, dict) else []

    topic = await _classify_topic_async(question)
    QAnimLogger.ok("Pipeline", f"Topic: {topic}, To Find: {to_find_targets}, Given values: {len(given_values)}")

    QAnimLogger.info("Pipeline", "Stage B: Building HTML animation...")
    html = await GeminiAnimationBuilder.build_async(question, scene_script, sol, topic)

    if not html or len(html) < 500:
        QAnimLogger.error("Pipeline", "Stage B produced empty/invalid HTML — returning fallback")
        return RecoveryEngine.fallback_html(question, "HTML builder returned empty content")

    QAnimLogger.info("Pipeline", "Stage C: Injecting 9-step panels...")

    # ── BUG FIX (v2.0.2): If SceneAnalyzer fell back to placeholder data,
    # merge real SolutionGenerator data into scene_script before injection.
    # Without this, Scenes 6/7/9 render generic "Result = f(given values)"
    # placeholders even when the solution was computed correctly. ──────────
    if _scene_script_is_fallback(scene_script) and not sol.get("_used_fallback"):
        QAnimLogger.warn("Pipeline", "SceneAnalyzer used fallback — merging real sol data into scene_script")
        scene_script = _merge_sol_into_scene_script(scene_script, sol, to_find_targets)

    answer_targets = _build_answer_targets(to_find_targets, sol, scene_script.get("final_answer", ""), scene_script.get("key_insight", ""))

    fad = scene_script.get("final_answer_data", {})
    fad_value = fad.get("answer_value", "")
    # ── BUG FIX (v2.0.2): Guard against placeholder "Result" being used as
    # the AnswerBox target. Only use fad value when it's real content. ─────
    if fad_value and not _is_fallback_content(fad_value):
        answer_targets = [{
            "label": fad.get("to_find_label", to_find_targets[0] if to_find_targets else "Final Answer"),
            "value": fad_value,
            "insight": scene_script.get("key_insight", sol.get("key_insight", "")),
        }]

    html = DocumentSkeletonNormalizer.normalize(html)
    html = inject_scene7_formula(html, sol, scene_script)
    html = inject_scene8_how_we_solve_it(html, sol, scene_script)
    html = inject_scene9_final_answer(html, sol, scene_script, to_find_targets)
    html = inject_to_find_system(html, to_find_targets)
    html = inject_answer_box(html, answer_targets)
    html = inject_controls_bar(html)
    if glossary_terms:
        html = inject_glossary(html, glossary_terms)
    html = inject_nav_patch_and_scene_desc(html)
    html = inject_prev_step_button(html)

    QAnimLogger.info("Pipeline", "Stage D: Final reliability + styling passes...")
    # inject_step_controller is called inside run_all_passes as the final step,
    # ensuring the master controller runs after all other scripts.
    html = PanelReliabilityEngine.run_all_passes(html, scene_script)
    html = HtmlSanitizer.sanitize(html)
    html = inject_centering_css(html)
    html = inject_step_color_css(html)
    html = DocumentSkeletonNormalizer.normalize(html)

    QAnimLogger.ok("Pipeline", f"Pipeline complete: {len(html):,} chars, {html.count('<g ')} SVG groups")
    return html


# ===========================================================================
#  MODULE 25 — Sync wrapper (for non-async callers)
# ===========================================================================

def generate_animation_html_sync(question: str) -> str:
    """Synchronous wrapper around the async pipeline."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, generate_animation_html(question))
                return future.result(timeout=PIPELINE_TIMEOUT + 30)
        else:
            return loop.run_until_complete(
                asyncio.wait_for(generate_animation_html(question), timeout=PIPELINE_TIMEOUT + 30)
            )
    except asyncio.TimeoutError:
        return RecoveryEngine.fallback_html(question, f"Pipeline timed out after {PIPELINE_TIMEOUT}s")
    except Exception as e:
        return RecoveryEngine.fallback_html(question, f"Pipeline error: {_err_msg(e)}")


# ===========================================================================
#  MODULE 26 — Public API aliases (backward compatibility)
# ===========================================================================

def generate_animation(question: str) -> str:
    """Generate a complete 9-step animated HTML for the given question."""
    return generate_animation_html_sync(question)

async def generate_animation_async(question: str) -> str:
    """Async version of generate_animation."""
    return await generate_animation_html(question)


async def generate_question_animation(question: str) -> dict:
    """
    Public async entry point imported by main.py.

    Returns:
        {
            "title":          str,
            "explanation":    str,
            "animation_code": str,
        }

    Raises ValueError for empty/blank input.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("'question' field cannot be empty")

    QAnimLogger.info("generate_question_animation", f"question={question[:80]!r}")

    html = await generate_animation_html(question)

    explanation: str = ""
    try:
        m = re.search(r'<title[^>]*>([^<]{5,120})</title>', html, re.IGNORECASE)
        if m:
            explanation = m.group(1).strip()
        if not explanation:
            m2 = re.search(r'<h3[^>]*>([^<]{5,120})</h3>', html, re.IGNORECASE)
            if m2:
                explanation = re.sub(r'<[^>]+>', '', m2.group(1)).strip()
        explanation = explanation[:220]
    except Exception:
        pass

    if not explanation:
        explanation = f"9-step animated solution for: {question[:160]}"

    return {
        "title":          question[:80],
        "explanation":    explanation,
        "animation_code": html,
    }


# Legacy aliases
analyse_question  = GeminiSceneAnalyzer.analyze
generate_solution = GeminiSolutionGenerator.generate
build_animation   = GeminiAnimationBuilder.build


# ===========================================================================
#  MODULE 27 — CLI Entry Point
# ===========================================================================

if __name__ == "__main__":
    import sys
    import time as _time_mod

    if len(sys.argv) < 2:
        print("Usage: python q_animation.py '<question>' [output.html]")
        print()
        print("Example:")
        print("  python q_animation.py 'A metal plate of area 2 m² is maintained at 150°C.")
        print("  Air at 30°C flows over it with h = 25 W/m²·K. Find the heat loss.' output.html")
        sys.exit(0)

    q   = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "animation_output.html"

    print(f"\n{'='*60}")
    print("  QAnim v2.0.2 — 9-Step Animation Generator")
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
    print(f"  Done in {elapsed:.1f}s")
    print(f"  Output size : {size_kb:.1f} KB")
    print(f"  Saved to    : {out}")
    print(f"{'='*60}\n")
    print(f"  Open {out} in your browser to view the animation.")
