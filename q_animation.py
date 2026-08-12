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
GEMINI_MODEL = "gemini-2.5-pro-preview-06-05"
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
<h2>⚠ Animation Generation Failed</h2>
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
#  MODULE 11 — Scene 6 (Main Formula) Injection
# ===========================================================================

_SCENE6_STYLES = """\
<style id="qanim-scene6-styles">
#qanim-scene-modal-backdrop{display:none;position:fixed;inset:0;z-index:7400;background:rgba(15,23,42,.50);backdrop-filter:blur(6px);opacity:0;transition:opacity .25s ease;}
#qanim-scene-modal-backdrop.qanim-scene-visible{display:block!important;opacity:1;}
#qanim-scene6-overlay{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) scale(.95);z-index:7500;width:min(860px,96vw);max-height:92vh;overflow-y:auto;box-sizing:border-box;opacity:0;pointer-events:none;transition:opacity .3s ease,transform .3s cubic-bezier(.34,1.56,.64,1);}
#qanim-scene6-overlay.qanim-scene-visible{display:block!important;opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.s6-card{background:#fff;border-radius:20px;box-shadow:0 8px 48px rgba(8,145,178,.14),0 2px 8px rgba(0,0,0,.08);border:1px solid #dde8f8;overflow:hidden;font-family:-apple-system,'Segoe UI',Arial,sans-serif;}
.s6-title-bar{text-align:center;padding:22px 28px 18px;background:#fff;border-bottom:1px solid #e8eef8;}
.s6-title-bar h2{font-size:20px;font-weight:900;color:#0f172a;letter-spacing:-0.3px;}
.s6-body{padding:28px 32px 24px;background:linear-gradient(160deg,#eef2f9 0%,#e8f0fe 50%,#eff6ff 100%);}
.s6-formula-box{background:#fff;border:2.5px solid #3b82f6;border-radius:18px;padding:20px 32px 16px;text-align:center;margin-bottom:10px;}
.s6-formula-main{font-family:'Courier New',monospace;font-size:28px;font-weight:900;color:#1d4ed8;letter-spacing:1px;line-height:1.4;opacity:0;transform:translateY(8px);transition:opacity .5s ease,transform .5s ease;}
.s6-formula-main.s6-shown{opacity:1;transform:translateY(0);}
.s6-formula-sublabel{font-size:11px;font-weight:700;color:#6366f1;letter-spacing:0.3px;margin-top:8px;opacity:0;transition:opacity .4s ease .2s;}
.s6-formula-sublabel.s6-shown{opacity:1;}
.s6-vars-row{display:flex;align-items:flex-start;justify-content:center;gap:12px;flex-wrap:wrap;margin-top:24px;}
.s6-var-box{display:flex;flex-direction:column;align-items:center;gap:0;min-width:120px;opacity:0;transform:translateY(12px);transition:opacity .35s ease,transform .35s cubic-bezier(.34,1.56,.64,1);}
.s6-var-box.s6-shown{opacity:1;transform:translateY(0);}
.s6-var-arrow{position:relative;height:24px;display:flex;align-items:flex-start;justify-content:center;width:100%;}
.s6-var-arrow::before{content:'';position:absolute;top:0;left:50%;transform:translateX(-50%);width:2.5px;height:16px;border-radius:2px;}
.s6-var-arrow::after{content:'';position:absolute;bottom:0;left:50%;transform:translateX(-50%);border-left:6px solid transparent;border-right:6px solid transparent;}
.s6-var-box.s6v-red .s6-var-inner{border-color:#f43f5e;background:#fff1f2;} .s6-var-box.s6v-red .s6-var-sym{color:#be123c;} .s6-var-box.s6v-red .s6-var-arrow::before{background:#f43f5e;} .s6-var-box.s6v-red .s6-var-arrow::after{border-top:8px solid #f43f5e;}
.s6-var-box.s6v-orange .s6-var-inner{border-color:#f59e0b;background:#fff7ed;} .s6-var-box.s6v-orange .s6-var-sym{color:#d97706;} .s6-var-box.s6v-orange .s6-var-arrow::before{background:#f59e0b;} .s6-var-box.s6v-orange .s6-var-arrow::after{border-top:8px solid #f59e0b;}
.s6-var-box.s6v-blue .s6-var-inner{border-color:#3b82f6;background:#eff6ff;} .s6-var-box.s6v-blue .s6-var-sym{color:#1d4ed8;} .s6-var-box.s6v-blue .s6-var-arrow::before{background:#3b82f6;} .s6-var-box.s6v-blue .s6-var-arrow::after{border-top:8px solid #3b82f6;}
.s6-var-box.s6v-green .s6-var-inner{border-color:#22c55e;background:#f0fdf4;} .s6-var-box.s6v-green .s6-var-sym{color:#15803d;} .s6-var-box.s6v-green .s6-var-arrow::before{background:#22c55e;} .s6-var-box.s6v-green .s6-var-arrow::after{border-top:8px solid #22c55e;}
.s6-var-box.s6v-purple .s6-var-inner{border-color:#a855f7;background:#faf5ff;} .s6-var-box.s6v-purple .s6-var-sym{color:#7c3aed;} .s6-var-box.s6v-purple .s6-var-arrow::before{background:#a855f7;} .s6-var-box.s6v-purple .s6-var-arrow::after{border-top:8px solid #a855f7;}
.s6-var-box.s6v-teal .s6-var-inner{border-color:#14b8a6;background:#f0fdfa;} .s6-var-box.s6v-teal .s6-var-sym{color:#0f766e;} .s6-var-box.s6v-teal .s6-var-arrow::before{background:#14b8a6;} .s6-var-box.s6v-teal .s6-var-arrow::after{border-top:8px solid #14b8a6;}
.s6-var-inner{border:2px solid;border-radius:14px;padding:14px 16px 12px;text-align:center;width:100%;box-sizing:border-box;}
.s6-var-sym{font-family:'Courier New',monospace;font-size:22px;font-weight:900;line-height:1;display:block;margin-bottom:5px;}
.s6-var-name{font-size:11.5px;font-weight:700;color:#475569;line-height:1.35;display:block;}
.s6-var-val{font-size:10.5px;font-weight:600;color:#94a3b8;margin-top:3px;display:block;}
.s6-note-bar{display:flex;align-items:center;justify-content:center;gap:8px;margin-top:22px;padding:12px 20px;background:#fff;border-radius:12px;border:1.5px solid #fde68a;opacity:0;transform:translateY(8px);transition:opacity .45s ease,transform .45s ease;}
.s6-note-bar.s6-shown{opacity:1;transform:translateY(0);}
.s6-note-text{font-size:13px;font-weight:700;color:#92400e;}
.s6-phase-progress{font-size:10.5px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:#0891b2;text-align:center;margin-bottom:4px;min-height:14px;}
.s6-phase-caption{font-size:13px;font-weight:600;color:#334155;text-align:center;margin-bottom:20px;line-height:1.5;min-height:18px;}
.s6-var-box.s6-active .s6-var-inner{box-shadow:0 0 0 4px rgba(8,145,178,.20),0 4px 16px rgba(8,145,178,.22);transform:scale(1.05);transition:transform .3s cubic-bezier(.34,1.56,.64,1),box-shadow .3s ease;}
.s6-nav-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:16px 32px 22px;border-top:1px solid #e8eef8;background:#fff;}
</style>"""


def _build_scene6_html(formula_data: dict) -> str:
    """Build the Scene 6 (Main Formula) overlay HTML from formula_data."""
    formula_text = html_module.escape(formula_data.get("formula_text", "Formula"))
    formula_sublabel = html_module.escape(formula_data.get("formula_sublabel", "Governing Equation"))
    note_text = formula_data.get("note_text", "")
    variables = formula_data.get("variables", [])

    # Map color names to CSS classes
    color_map = {
        "blue": "s6v-blue", "cyan": "s6v-teal", "orange": "s6v-orange",
        "green": "s6v-green", "red": "s6v-red", "purple": "s6v-purple",
        "teal": "s6v-teal",
    }

    var_boxes_html = ""
    for v in variables:
        sym = html_module.escape(v.get("symbol", "?"))
        name = html_module.escape(v.get("name", "Variable"))
        val = html_module.escape(v.get("value", ""))
        unit = html_module.escape(v.get("unit", ""))
        color_cls = color_map.get(v.get("color", "blue"), "s6v-blue")
        val_display = f"{val} {unit}".strip() if val else ""
        var_boxes_html += f"""
          <div class="s6-var-box {color_cls}">
            <div class="s6-var-arrow"></div>
            <div class="s6-var-inner">
              <span class="s6-var-sym">{sym}</span>
              <span class="s6-var-name">{name}</span>
              {f'<span class="s6-var-val">{val_display}</span>' if val_display else ''}
            </div>
          </div>"""

    note_html = ""
    if note_text:
        note_html = f'<div class="s6-note-bar" id="s6-note-bar"><span class="s6-note-text">{html_module.escape(note_text)}</span></div>'

    return f"""
<div id="qanim-scene-modal-backdrop"></div>
<div id="qanim-scene6-overlay">
  <div class="s6-card">
    <div class="s6-title-bar"><h2>Step 7 &mdash; Main Formula</h2></div>
    <div class="s6-body">
      <div class="s6-phase-progress" id="s6-phase-progress"></div>
      <div class="s6-phase-caption" id="s6-phase-caption">This is the governing formula. Click &ldquo;Next&rdquo; to explore each variable.</div>
      <div class="s6-formula-box">
        <div class="s6-formula-main" id="s6-formula-text">{formula_text}</div>
        <div class="s6-formula-sublabel" id="s6-formula-sublabel">{formula_sublabel}</div>
      </div>
      <div class="s6-vars-row" id="s6-vars-row">{var_boxes_html}</div>
      {note_html}
    </div>
    <div class="s6-nav-row">
      <button onclick="qanim_goToPrevScene()" style="background:#f1f5f9;border:1.5px solid #cbd5e1;color:#475569;padding:10px 20px;border-radius:8px;font-weight:700;font-family:inherit;cursor:pointer;">&#9664; Back to Animation</button>
      <button id="s6-next-btn" onclick="qanim_s6Advance()" style="background:linear-gradient(135deg,#0e7490,#0891b2);color:#fff;border:none;padding:10px 22px;border-radius:8px;font-weight:700;font-family:inherit;cursor:pointer;">Next &#9654;</button>
    </div>
  </div>
</div>"""


_SCENE6_JS = """\
<script id="qanim-js-scene6">
(function initScene6(){
  'use strict';
  if(window.__qanimScene6Init)return; window.__qanimScene6Init=true;
  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _varBoxes(){return document.querySelectorAll('#s6-vars-row .s6-var-box');}

  var s6Phase = -1;
  var s6AutoAdvanceScheduled = false;
  var s6AutoAdvanceTimer = null;
  var S6_TOTAL_VAR_PHASES = 0;

  function s6Render(){
    var boxes = _varBoxes();
    var n = boxes.length;
    S6_TOTAL_VAR_PHASES = n;
    var formulaEl = _el('s6-formula-text');
    var sublabelEl = _el('s6-formula-sublabel');
    var captionEl = _el('s6-phase-caption');
    var progressEl = _el('s6-phase-progress');
    var noteEl = _el('s6-note-bar');
    var nextBtn = _el('s6-next-btn');
    if(formulaEl) formulaEl.classList.add('s6-shown');
    if(sublabelEl) sublabelEl.classList.add('s6-shown');
    for(var i=0;i<n;i++){
      var b = boxes[i];
      b.classList.remove('s6-active');
      if(s6Phase >= i+1){ b.classList.add('s6-shown'); if(s6Phase===i+1) b.classList.add('s6-active'); }
    }
    var showNote = s6Phase >= n+1;
    if(noteEl) noteEl.classList.toggle('s6-shown', showNote);
    if(showNote && !s6AutoAdvanceScheduled){
      s6AutoAdvanceScheduled = true;
      s6AutoAdvanceTimer = setTimeout(function(){
        var ov = _el('qanim-scene6-overlay');
        if(ov && ov.classList.contains('qanim-scene-visible')){
          if(typeof window.qanim_showScene8==='function') window.qanim_showScene8();
        }
      }, 2800);
    }
    if(progressEl){
      if(s6Phase<=0) progressEl.innerText = 'Step 1 of '+(n+2)+' \u2014 The Formula';
      else if(s6Phase<=n) progressEl.innerText = 'Step '+(s6Phase+1)+' of '+(n+2)+' \u2014 Variable '+s6Phase+' of '+n;
      else progressEl.innerText = 'Step '+(n+2)+' of '+(n+2)+' \u2014 Key Insight';
    }
    if(captionEl){
      var cap = 'This is the governing formula. Click \u201CNext\u201D to explore each variable.';
      if(s6Phase>=1&&s6Phase<=n){
        var b2=boxes[s6Phase-1];
        var symEl=b2?b2.querySelector('.s6-var-sym'):null;
        var nameEl=b2?b2.querySelector('.s6-var-name'):null;
        var valEl=b2?b2.querySelector('.s6-var-val'):null;
        cap=(symEl?symEl.innerText:'')+' \u2014 '+(nameEl?nameEl.innerText:'variable')+((valEl&&valEl.innerText)?' ('+valEl.innerText+')':'')+'.'
      } else if(s6Phase>=n+1){
        cap='All variables identified. See the key insight below, then continue to the step-by-step solution.';
      }
      captionEl.innerText=cap;
    }
    if(nextBtn){
      if(s6Phase>=n+1){
        nextBtn.innerText='Step 8: Substitution \u25B6';
        nextBtn.style.background='linear-gradient(135deg,#4338ca,#7c3aed)';
        nextBtn.onclick=function(){ window.qanim_showScene8(); };
      } else {
        nextBtn.innerText='Next \u25B6';
        nextBtn.style.background='';
        nextBtn.onclick=function(){ window.qanim_s6Advance(); };
      }
    }
  }

  window.qanim_s6Advance = function(){
    var n=_varBoxes().length;
    if(s6Phase < n+1) s6Phase++;
    s6Render();
  };

  function _qanimCancelRAF(){
    if(typeof window.qanimRafId!=='undefined'&&window.qanimRafId){ cancelAnimationFrame(window.qanimRafId); window.qanimRafId=null; }
    if(typeof window.rafId!=='undefined'&&window.rafId){ cancelAnimationFrame(window.rafId); window.rafId=null; }
  }
  function _qanimResumeRAF(){
    if(typeof window.qanimStartRAF==='function'){ window.qanimStartRAF(); return; }
    if(typeof window.startRAF==='function'){ window.startRAF(); return; }
    if(typeof window.animate==='function'){ requestAnimationFrame(window.animate); return; }
  }

  function _syncDots(idx){
    var dots=document.querySelectorAll('.step-dot');
    for(var i=0;i<dots.length;i++){ dots[i].classList.remove('active','done'); if(i<idx) dots[i].classList.add('done'); if(i===idx) dots[i].classList.add('active'); }
    var lbl=_el('step-label'); if(lbl) lbl.innerText='Step 7 of 9: Main Formula';
    var bar=_el('step-bar'); if(bar) bar.style.width=Math.round(7/9*100)+'%';
  }

  window.qanim_showScene6 = function(){
    var ov=_el('qanim-scene6-overlay'); if(ov) ov.classList.add('qanim-scene-visible');
    var ov7=_el('qanim-scene7-overlay'); if(ov7) ov7.classList.remove('qanim-scene-visible');
    var ov9=_el('qanim-scene9-overlay'); if(ov9) ov9.classList.remove('qanim-scene-visible');
    var bd=_el('qanim-scene-modal-backdrop'); if(bd) bd.classList.add('qanim-scene-visible');
    _qanimCancelRAF();
    _syncDots(6);
    s6Phase=0; s6AutoAdvanceScheduled=false;
    if(s6AutoAdvanceTimer){ clearTimeout(s6AutoAdvanceTimer); s6AutoAdvanceTimer=null; }
    s6Render();
  };

  window.qanim_goToPrevScene = function(){
    var ov6=_el('qanim-scene6-overlay'); if(ov6) ov6.classList.remove('qanim-scene-visible');
    var ov7=_el('qanim-scene7-overlay'); if(ov7) ov7.classList.remove('qanim-scene-visible');
    var ov9=_el('qanim-scene9-overlay'); if(ov9) ov9.classList.remove('qanim-scene-visible');
    var bd=_el('qanim-scene-modal-backdrop'); if(bd) bd.classList.remove('qanim-scene-visible');
    if(s6AutoAdvanceTimer){ clearTimeout(s6AutoAdvanceTimer); s6AutoAdvanceTimer=null; }
    var svgCont=document.querySelector('.svg-container'); if(svgCont){ svgCont.style.transition='opacity .45s ease'; svgCont.style.opacity='1'; }
    if(typeof window.applyStep==='function'&&typeof window.stepsData!=='undefined'){
      var last=window.stepsData.length-1; window.currentStep=last; window.applyStep(last);
    }
    _qanimResumeRAF();
  };

  window.qanim_goToScene7 = function(){
    var ov6=_el('qanim-scene6-overlay'); if(ov6) ov6.classList.remove('qanim-scene-visible');
    if(s6AutoAdvanceTimer){ clearTimeout(s6AutoAdvanceTimer); s6AutoAdvanceTimer=null; }
    if(typeof window.qanim_showScene8==='function') window.qanim_showScene8();
  };

  _onReady(function(){
    var _origReset=window.resetAnim;
    window.resetAnim=function(){
      var ov6=_el('qanim-scene6-overlay'); if(ov6) ov6.classList.remove('qanim-scene-visible');
      var ov7=_el('qanim-scene7-overlay'); if(ov7) ov7.classList.remove('qanim-scene-visible');
      var ov9=_el('qanim-scene9-overlay'); if(ov9) ov9.classList.remove('qanim-scene-visible');
      var bd=_el('qanim-scene-modal-backdrop'); if(bd) bd.classList.remove('qanim-scene-visible');
      var svgCont=document.querySelector('.svg-container'); if(svgCont) svgCont.style.opacity='1';
      if(typeof _origReset==='function') _origReset();
    };
  });
})();
</script>"""


def inject_scene6_big_idea(html: str, gemini_sol: dict, scene_script: dict) -> str:
    if 'qanim-scene6-styles' in html:
        return html
    formula_data = scene_script.get("formula_data", {})
    if not formula_data:
        # Build from solution data
        sol = gemini_sol or {}
        variables = sol.get("variables", [])
        formula_data = {
            "formula_text": sol.get("formula", "Governing Formula"),
            "formula_sublabel": "Governing Equation",
            "variables": variables,
            "note_text": sol.get("key_insight", ""),
        }
    scene6_html = _build_scene6_html(formula_data)
    inject = _SCENE6_STYLES + "\n" + scene6_html + "\n" + _SCENE6_JS
    if '</head>' in html:
        html = html.replace('</head>', _SCENE6_STYLES + '\n</head>', 1)
        inject_body = scene6_html + "\n" + _SCENE6_JS
        if '<body' in html:
            idx = html.find('>', html.find('<body')) + 1
            html = html[:idx] + inject_body + html[idx:]
        else:
            html = html + inject_body
    else:
        html = inject + html
    QAnimLogger.ok("Scene6", "Main Formula panel injected")
    return html



# ===========================================================================
#  MODULE 12 — Scene 7/8 (Step-by-Step Substitution) Injection
# ===========================================================================

_SCENE7_STYLES = """\
<style id="qanim-scene7-styles">
#qanim-scene7-overlay{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) scale(.95);z-index:7500;width:min(900px,96vw);max-height:92vh;overflow-y:auto;box-sizing:border-box;opacity:0;pointer-events:none;transition:opacity .3s ease,transform .3s cubic-bezier(.34,1.56,.64,1);}
#qanim-scene7-overlay.qanim-scene-visible{display:block!important;opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.s7-card{background:#fff;border-radius:20px;box-shadow:0 8px 48px rgba(37,99,235,.12),0 2px 8px rgba(0,0,0,.07);border:1px solid #e8eef8;overflow:hidden;font-family:-apple-system,'Segoe UI',Arial,sans-serif;}
.s7-title-bar{text-align:center;padding:20px 28px 16px;border-bottom:1px solid #e8eef8;background:#fff;}
.s7-title-bar h2{font-size:20px;font-weight:900;color:#0f172a;letter-spacing:-0.3px;}
.s7-body-cols{display:flex;align-items:flex-start;gap:0;min-height:320px;}
.s7-left-col{width:44%;min-width:200px;border-right:1.5px solid #e8eef8;padding:22px 20px 22px 26px;background:linear-gradient(180deg,#eff6ff 0%,#dbeafe 100%);display:flex;flex-direction:column;gap:0;align-self:stretch;}
.s7-system-label{font-size:10.5px;font-weight:800;color:#1d4ed8;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;}
.s7-system-visual{background:linear-gradient(135deg,#bfdbfe 0%,#93c5fd 100%);border-radius:12px;padding:16px 14px 14px;margin-bottom:16px;text-align:center;}
.s7-system-visual-title{font-size:13px;font-weight:800;color:#1e3a5f;margin-bottom:6px;}
.s7-system-arrows{display:flex;justify-content:center;gap:10px;margin:8px 0;font-size:20px;color:#d97706;}
.s7-system-label2{font-size:10px;font-weight:600;color:#1e40af;margin-top:4px;}
.s7-right-col{flex:1;padding:22px 26px 20px 20px;display:flex;flex-direction:column;gap:16px;}
.s7-given-section-title{font-size:13px;font-weight:900;color:#1d4ed8;margin-bottom:8px;}
.s7-given-list{display:flex;flex-direction:column;gap:5px;margin-bottom:14px;}
.s7-given-item{font-size:12.5px;color:#334155;line-height:1.5;display:flex;align-items:flex-start;gap:7px;}
.s7-given-item::before{content:'•';color:#3b82f6;font-weight:900;flex-shrink:0;margin-top:1px;}
.s7-given-item strong{font-weight:700;color:#1e293b;}
.s7-approach-section-title{font-size:13px;font-weight:900;color:#7c3aed;margin-bottom:8px;}
.s7-approach-list{display:flex;flex-direction:column;gap:5px;margin-bottom:14px;}
.s7-approach-item{font-size:12.5px;color:#1e293b;line-height:1.5;}
.s7-formula-result-bar{background:linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);border:2px solid #22c55e;border-radius:12px;padding:12px 18px;margin-top:auto;}
.s7-result-label{font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:1.2px;color:#15803d;margin-bottom:6px;}
.s7-result-eq{font-family:'Courier New',monospace;font-size:16px;font-weight:800;color:#14532d;line-height:1.4;}
.s7-nav-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:16px 28px 20px;border-top:1px solid #e8eef8;background:#fff;}
</style>"""


def _build_scene7_html(substitution_data: dict) -> str:
    sub = substitution_data or {}
    system_title = html_module.escape(sub.get("system_title", "Physical System"))
    system_desc = html_module.escape(sub.get("system_description", ""))
    given_list = sub.get("given_list", [])
    approach_steps = sub.get("approach_steps", [])
    result_bar = html_module.escape(sub.get("result_bar", ""))

    given_items_html = "".join(
        f'<div class="s7-given-item"><strong>{html_module.escape(g)}</strong></div>'
        for g in given_list
    )
    approach_items_html = "".join(
        f'<div class="s7-approach-item">{i+1}. {html_module.escape(s)}</div>'
        for i, s in enumerate(approach_steps)
    )

    return f"""
<div id="qanim-scene7-overlay">
  <div class="s7-card">
    <div class="s7-title-bar"><h2>Step 8 &mdash; Step-by-Step Substitution</h2></div>
    <div class="s7-body-cols">
      <div class="s7-left-col">
        <div class="s7-system-label">Physical System</div>
        <div class="s7-system-visual">
          <div class="s7-system-visual-title">{system_title}</div>
          <div class="s7-system-arrows">&#x1F321;&#xFE0F; &rarr; &#x1F4A8;</div>
          <div class="s7-system-label2">{system_desc[:80]}</div>
        </div>
      </div>
      <div class="s7-right-col">
        <div>
          <div class="s7-given-section-title">Given Parameters</div>
          <div class="s7-given-list">{given_items_html}</div>
        </div>
        <div>
          <div class="s7-approach-section-title">Solution Approach</div>
          <div class="s7-approach-list">{approach_items_html}</div>
        </div>
        <div class="s7-formula-result-bar">
          <div class="s7-result-label">Formula + Result</div>
          <div class="s7-result-eq">{result_bar}</div>
        </div>
      </div>
    </div>
    <div class="s7-nav-row">
      <button onclick="if(typeof window.qanim_showScene6===\'function\') window.qanim_showScene6()" style="background:#f1f5f9;border:1.5px solid #cbd5e1;color:#475569;padding:10px 20px;border-radius:8px;font-weight:700;font-family:inherit;cursor:pointer;">&#9664; Step 7: Formula</button>
      <button onclick="if(typeof window.qanim_showScene9===\'function\') window.qanim_showScene9()" style="background:linear-gradient(135deg,#15803d,#22c55e);color:#fff;border:none;padding:10px 22px;border-radius:8px;font-weight:700;font-family:inherit;cursor:pointer;">Step 9: Final Answer &#9654;</button>
    </div>
  </div>
</div>"""


_SCENE7_JS = """\
<script id="qanim-js-scene7">
(function initScene7(){
  'use strict';
  if(window.__qanimScene7Init)return; window.__qanimScene7Init=true;
  function _el(id){return document.getElementById(id);}

  function _syncDots8(){
    var dots=document.querySelectorAll('.step-dot');
    for(var i=0;i<dots.length;i++){ dots[i].classList.remove('active','done'); if(i<7) dots[i].classList.add('done'); if(i===7) dots[i].classList.add('active'); }
    var lbl=_el('step-label'); if(lbl) lbl.innerText='Step 8 of 9: Step-by-Step Substitution';
    var bar=_el('step-bar'); if(bar) bar.style.width=Math.round(8/9*100)+'%';
  }

  window.qanim_showScene8 = function(){
    var ov6=_el('qanim-scene6-overlay'); if(ov6) ov6.classList.remove('qanim-scene-visible');
    var ov9=_el('qanim-scene9-overlay'); if(ov9) ov9.classList.remove('qanim-scene-visible');
    var ov7=_el('qanim-scene7-overlay'); if(ov7) ov7.classList.add('qanim-scene-visible');
    var bd=_el('qanim-scene-modal-backdrop'); if(bd) bd.classList.add('qanim-scene-visible');
    _syncDots8();
  };
  window.qanim_showScene7 = window.qanim_showScene8;
})();
</script>"""


def inject_scene7_how_we_solve_it(html: str, gemini_sol: dict, scene_script: dict) -> str:
    if 'qanim-scene7-styles' in html:
        return html
    substitution_data = scene_script.get("substitution_data", {})
    if not substitution_data:
        sol = gemini_sol or {}
        substitution_data = {
            "system_title": "Physical System",
            "system_description": "",
            "given_list": [f"{s}" for s in (sol.get("steps", []) or [])[:3]],
            "approach_steps": sol.get("steps", [])[:3],
            "result_bar": sol.get("final_answer", "See calculation above"),
        }
    scene7_html = _build_scene7_html(substitution_data)
    if '</head>' in html:
        html = html.replace('</head>', _SCENE7_STYLES + '\n</head>', 1)
    if '<body' in html:
        idx = html.find('>', html.find('<body')) + 1
        html = html[:idx] + scene7_html + html[idx:]
    else:
        html = html + scene7_html
    if '</body>' in html:
        html = html.replace('</body>', _SCENE7_JS + '\n</body>', 1)
    else:
        html = html + _SCENE7_JS
    QAnimLogger.ok("Scene7", "Step-by-Step Substitution panel injected")
    return html



# ===========================================================================
#  MODULE 13 — Scene 9 (Final Answer) Injection
# ===========================================================================

_SCENE9_STYLES = """\
<style id="qanim-scene9-styles">
#qanim-scene9-overlay{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) scale(.95);z-index:7500;width:min(860px,96vw);max-height:92vh;overflow-y:auto;box-sizing:border-box;opacity:0;pointer-events:none;transition:opacity .3s ease,transform .3s cubic-bezier(.34,1.56,.64,1);}
#qanim-scene9-overlay.qanim-scene-visible{display:block!important;opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.s9-card{background:#fff;border-radius:20px;box-shadow:0 8px 48px rgba(34,197,94,.14),0 2px 8px rgba(0,0,0,.07);border:1px solid #e8eef8;overflow:hidden;font-family:-apple-system,'Segoe UI',Arial,sans-serif;}
.s9-title-bar{text-align:center;padding:20px 28px 16px;border-bottom:1px solid #bbf7d0;background:linear-gradient(135deg,#f0fdf4,#dcfce7);}
.s9-title-bar h2{font-size:20px;font-weight:900;color:#14532d;}
.s9-body{padding:24px 32px 20px;display:flex;flex-direction:column;gap:22px;}
.s9-formula-recap{background:#eff6ff;border:1.5px solid #bfdbfe;border-radius:12px;padding:14px 20px;text-align:center;}
.s9-formula-recap-label{font-size:10.5px;font-weight:800;color:#1d4ed8;text-transform:uppercase;letter-spacing:1.2px;margin-bottom:6px;}
.s9-formula-recap-eq{font-family:'Courier New',monospace;font-size:18px;font-weight:900;color:#1d4ed8;}
.s9-sub-chain{display:flex;flex-direction:column;gap:10px;}
.s9-sub-row{display:flex;align-items:center;gap:12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 16px;opacity:0;transform:translateX(-18px);transition:opacity .4s ease,transform .4s cubic-bezier(.34,1.56,.64,1);}
.s9-sub-row.s9-shown{opacity:1;transform:translateX(0);}
.s9-sub-num{background:#0891b2;color:#fff;border-radius:50%;width:26px;height:26px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;flex-shrink:0;}
.s9-sub-eq{font-family:'Courier New',monospace;font-size:15px;font-weight:700;color:#1e293b;flex:1;}
.s9-final-box{background:linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);border:3px solid #22c55e;border-radius:18px;padding:28px 32px;text-align:center;position:relative;overflow:hidden;opacity:0;transform:scale(0.94);transition:opacity .5s ease .3s,transform .5s cubic-bezier(.34,1.56,.64,1) .3s;}
.s9-final-box.s9-shown{opacity:1;transform:scale(1);}
.s9-final-label{font-size:11px;font-weight:900;text-transform:uppercase;letter-spacing:2px;color:#15803d;margin-bottom:12px;}
.s9-final-value{font-family:'Courier New',monospace;font-size:36px;font-weight:900;color:#14532d;line-height:1.2;}
.s9-final-value span.s9-highlight{color:#16a34a;font-size:44px;}
.s9-final-unit{font-size:15px;color:#166534;margin-top:8px;font-weight:600;}
.s9-insight-bar{display:flex;align-items:flex-start;gap:10px;background:#fff7ed;border:1.5px solid #fed7aa;border-radius:10px;padding:13px 18px;opacity:0;transition:opacity .4s ease .6s;}
.s9-insight-bar.s9-shown{opacity:1;}
.s9-insight-icon{font-size:20px;flex-shrink:0;}
.s9-insight-text{font-size:13px;color:#92400e;line-height:1.6;}
.s9-insight-text strong{color:#78350f;}
.s9-nav-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:16px 36px 22px;border-top:1px solid #bbf7d0;background:#f0fdf4;}
</style>"""


def _build_scene9_html(final_answer_data: dict, to_find_label: str = "Final Answer") -> str:
    fad = final_answer_data or {}
    formula_recap = html_module.escape(fad.get("formula_recap", "Result = f(values)"))
    chain = fad.get("substitution_chain", [])
    answer_value = html_module.escape(fad.get("answer_value", "?"))
    answer_unit = html_module.escape(fad.get("answer_unit", ""))
    answer_highlight = html_module.escape(fad.get("answer_highlight", answer_value))
    insight_text = fad.get("insight_text", "Apply the governing formula with the given data.")
    label = html_module.escape(fad.get("to_find_label", to_find_label))

    chain_html = ""
    for row in chain:
        num = row.get("num", "")
        eq = html_module.escape(row.get("eq", ""))
        chain_html += f"""
        <div class="s9-sub-row">
          <div class="s9-sub-num">{num}</div>
          <div class="s9-sub-eq">{eq}</div>
        </div>"""

    return f"""
<div id="qanim-scene9-overlay">
  <div class="s9-card">
    <div class="s9-title-bar"><h2>Step 9 &mdash; Final Answer</h2></div>
    <div class="s9-body">
      <div class="s9-formula-recap">
        <div class="s9-formula-recap-label">Governing Formula</div>
        <div class="s9-formula-recap-eq">{formula_recap}</div>
      </div>
      <div class="s9-sub-chain" id="s9-sub-chain">{chain_html}</div>
      <div class="s9-final-box" id="s9-final-box">
        <div class="s9-final-label">&#x2705; {label}</div>
        <div class="s9-final-value"><span class="s9-highlight">{answer_highlight}</span></div>
        <div class="s9-final-unit">{answer_unit}</div>
      </div>
      <div class="s9-insight-bar" id="s9-insight-bar">
        <div class="s9-insight-icon">&#x1F4A1;</div>
        <div class="s9-insight-text">{insight_text}</div>
      </div>
    </div>
    <div class="s9-nav-row">
      <button onclick="if(typeof window.qanim_showScene8===\'function\') window.qanim_showScene8()" style="background:#f1f5f9;border:1.5px solid #cbd5e1;color:#475569;padding:10px 20px;border-radius:8px;font-weight:700;font-family:inherit;cursor:pointer;">&#9664; Step 8: Substitution</button>
      <button onclick="if(typeof window.qanim_goToPrevScene===\'function\') window.qanim_goToPrevScene()" style="background:linear-gradient(135deg,#0e7490,#0891b2);color:#fff;border:none;padding:10px 22px;border-radius:8px;font-weight:700;font-family:inherit;cursor:pointer;">&#x21BA; Restart Animation</button>
    </div>
  </div>
</div>"""


_SCENE9_JS = """\
<script id="qanim-js-scene9">
(function initScene9(){
  'use strict';
  if(window.__qanimScene9Init)return; window.__qanimScene9Init=true;
  function _el(id){return document.getElementById(id);}

  function _syncDots9(){
    var dots=document.querySelectorAll('.step-dot');
    for(var i=0;i<dots.length;i++){ dots[i].classList.remove('active','done'); if(i<8) dots[i].classList.add('done'); if(i===8) dots[i].classList.add('active'); }
    var lbl=_el('step-label'); if(lbl) lbl.innerText='Step 9 of 9: Final Answer';
    var bar=_el('step-bar'); if(bar) bar.style.width='100%';
  }

  function _animateEntrance(){
    var rows=document.querySelectorAll('#s9-sub-chain .s9-sub-row');
    for(var i=0;i<rows.length;i++){
      (function(el,delay){ setTimeout(function(){ el.classList.add('s9-shown'); }, delay); })(rows[i], 200+i*200);
    }
    var fb=_el('s9-final-box');
    if(fb) setTimeout(function(){ fb.classList.add('s9-shown'); }, 200+rows.length*200);
    var ib=_el('s9-insight-bar');
    if(ib) setTimeout(function(){ ib.classList.add('s9-shown'); }, 200+rows.length*200+300);
  }

  function _resetEntrance(){
    var rows=document.querySelectorAll('#s9-sub-chain .s9-sub-row');
    for(var i=0;i<rows.length;i++) rows[i].classList.remove('s9-shown');
    var fb=_el('s9-final-box'); if(fb) fb.classList.remove('s9-shown');
    var ib=_el('s9-insight-bar'); if(ib) ib.classList.remove('s9-shown');
  }

  window.qanim_showScene9 = function(){
    var ov7=_el('qanim-scene7-overlay'); if(ov7) ov7.classList.remove('qanim-scene-visible');
    var ov6=_el('qanim-scene6-overlay'); if(ov6) ov6.classList.remove('qanim-scene-visible');
    var ov9=_el('qanim-scene9-overlay'); if(ov9) ov9.classList.add('qanim-scene-visible');
    var bd=_el('qanim-scene-modal-backdrop'); if(bd) bd.classList.add('qanim-scene-visible');
    _syncDots9();
    _resetEntrance();
    setTimeout(_animateEntrance, 120);
  };
})();
</script>"""


def inject_scene9_final_answer(html: str, gemini_sol: dict, scene_script: dict, to_find_targets: list) -> str:
    if 'qanim-scene9-styles' in html:
        return html
    final_answer_data = scene_script.get("final_answer_data", {})
    if not final_answer_data:
        sol = gemini_sol or {}
        chain_rows = []
        for i, s in enumerate(sol.get("substitution_chain", sol.get("steps", []))[:5]):
            if isinstance(s, dict):
                chain_rows.append(s)
            else:
                chain_rows.append({"num": i + 1, "eq": str(s)[:80]})
        fa = sol.get("final_answer", "See calculation above")
        nums = re.findall(r'[-+]?\d+(?:\.\d+)?', fa)
        val = nums[-1] if nums else fa[:20]
        final_answer_data = {
            "formula_recap": sol.get("formula", "Governing formula"),
            "substitution_chain": chain_rows,
            "answer_value": val,
            "answer_unit": "",
            "answer_highlight": val,
            "insight_text": sol.get("key_insight", "Apply the governing formula."),
            "to_find_label": to_find_targets[0] if to_find_targets else "Final Answer",
        }
    to_find_label = to_find_targets[0] if to_find_targets else "Final Answer"
    scene9_html = _build_scene9_html(final_answer_data, to_find_label)
    if '</head>' in html:
        html = html.replace('</head>', _SCENE9_STYLES + '\n</head>', 1)
    if '<body' in html:
        idx = html.find('>', html.find('<body')) + 1
        html = html[:idx] + scene9_html + html[idx:]
    if '</body>' in html:
        html = html.replace('</body>', _SCENE9_JS + '\n</body>', 1)
    else:
        html = html + _SCENE9_JS
    QAnimLogger.ok("Scene9", "Final Answer panel injected")
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
  function _normalize(s){return String(s).replace(/[^0-9.\\x2D]/g,'').trim();}
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
    # Add styles to head
    if '</head>' in html:
        html = html.replace('</head>', _PREVSTEP_STYLES + '\n</head>', 1)
    # Add the btn-prev button before btn-next in the actions div
    # Look for the btn-next button and insert btn-prev before it
    btn_next_pattern = re.search(r'(<button[^>]*class="btn-primary"[^>]*id="btn-next"[^>]*>)', html)
    if btn_next_pattern:
        prev_btn = '<button class="btn-secondary qanim-prev-btn" id="btn-prev" disabled>&#x25C0; Previous Step</button>\n'
        html = html[:btn_next_pattern.start()] + prev_btn + html[btn_next_pattern.start():]
    elif 'id="btn-next"' in html:
        html = html.replace(
            'id="btn-next"',
            'id="btn-next-placeholder-REPLACED"',
            1
        )
        html = html.replace(
            'id="btn-next-placeholder-REPLACED"',
            'id="btn-next"',
            1
        )
    # Add JS
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

_SCENE6_AUTOTRIGGER_JS = """\
<script id="qanim-js-scene6-autotrigger">
(function(){
  'use strict';
  if(window.__qanimScene6AutoTrigger)return;window.__qanimScene6AutoTrigger=true;
  function _tryTrigger(){
    var cs=typeof window.currentStep==='number'?window.currentStep:-1;
    var ts=typeof window.totalSteps==='number'?window.totalSteps:6;
    var isAtEnd=(cs>=ts);
    if(!isAtEnd)return;
    var ov6=document.getElementById('qanim-scene6-overlay');
    var ov7=document.getElementById('qanim-scene7-overlay');
    var ov9=document.getElementById('qanim-scene9-overlay');
    var alreadyOpen=(ov6&&ov6.classList.contains('qanim-scene-visible'))||(ov7&&ov7.classList.contains('qanim-scene-visible'))||(ov9&&ov9.classList.contains('qanim-scene-visible'));
    if(alreadyOpen)return;
    if(typeof window.qanim_showScene6==='function'){
      var svgCont=document.querySelector('.svg-container');
      var doShow=function(){window.qanim_showScene6();};
      if(svgCont){svgCont.style.transition='opacity .45s ease';svgCont.style.opacity='0';setTimeout(doShow,460);}else{setTimeout(doShow,120);}
    }
  }
  function _wireBtn(){var btn=document.getElementById('btn-next');if(!btn||btn.__qanimAutoWired)return;btn.__qanimAutoWired=true;btn.addEventListener('click',function(){setTimeout(_tryTrigger,30);});}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  _onReady(function(){_wireBtn();});
})();
</script>"""


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
    if 'qanim-step-controller' in html:
        return html
    if '</body>' in html:
        html = html.replace('</body>', _STEP_CONTROLLER_JS + '\n' + _SCENE6_AUTOTRIGGER_JS + '\n</body>', 1)
    else:
        html = html + _STEP_CONTROLLER_JS + '\n' + _SCENE6_AUTOTRIGGER_JS
    QAnimLogger.ok("StepController", "Step controller + scene6 autotrigger injected")
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
   and Scene 6 overlay opens (Step 7: Main Formula).
4. From Scene 6, user advances to Scene 7 (Step 8: Substitution).
5. From Scene 7, user advances to Scene 9 (Step 9: Final Answer).

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
      <div class="action-row" style="display:flex;gap:10px;margin-top:16px;justify-content:flex-end;">
        <button class="btn-secondary" id="btn-prev" disabled onclick="prevStep()">&#x25C0; Prev</button>
        <button class="btn-primary" id="btn-next" onclick="nextStep()">Next &#x25B6;</button>
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
var currentStep = 0;
var totalSteps = stepsData.length - 1; // 0-indexed, so last SVG step = 5 (step 6)

function applyStep(idx) {
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
    triggerScene6();
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

function triggerScene6() {
  var svgCont = document.querySelector('.svg-container');
  if (svgCont) {
    svgCont.style.transition = 'opacity .45s ease';
    svgCont.style.opacity = '0';
  }
  setTimeout(function() {
    if (typeof window.qanim_showScene6 === 'function') window.qanim_showScene6();
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
2. The stepsData array MUST contain EXACTLY the steps from scene_script.steps.
3. ALL SVG layer IDs must match the keys in scene_script.svg_components.
4. The var totalSteps must equal scene_script.steps.length - 1.
5. The step indicator must show exactly 9 dots (Steps 1-9), where dots 7-9 represent the formula/substitution/answer scenes.
6. Do NOT pre-build Scene 6/7/9 overlays in this HTML — they will be injected by the Python pipeline.
7. The Next button on step 6 (the last SVG step, index 5) must call triggerScene6() not advance further.
8. The step-bar progress tracks all 9 steps: width = (currentStep+1)/9 * 100 + '%'
   (Steps 7-9 progress will be updated by the injected scene JS.)

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
                # Strip markdown fences
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
    """
    Post-processing suite that repairs common generation defects so the
    final HTML is maximally robust before delivery.
    """

    # ── Pass 1: ensure stepsData drives dots correctly ───────────────────
    @staticmethod
    def _fix_step_dot_sync(html: str) -> str:
        """
        Make sure the step dots (1-9) update correctly from applyStep().
        If the generated applyStep doesn't handle data-step attribute,
        inject a wrapper.
        """
        if 'data-step' not in html:
            # Inject a tiny patch that sets data-step on every applyStep call
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

    # ── Pass 2: ensure btn-next on last SVG step triggers scene6 ─────────
    @staticmethod
    def _fix_next_button_trigger(html: str) -> str:
        """
        If the generated nextStep() does not call triggerScene6() or
        qanim_showScene6() at the last step, inject a wrapper.
        """
        if 'triggerScene6' in html or 'qanim_showScene6' in html:
            return html  # already handled
        patch = """\
<script id="__nexttrigger_patch__">
(function(){
  var _origNext = window.nextStep;
  if(typeof _origNext !== 'function') return;
  window.nextStep = function() {
    var ts = typeof window.totalSteps === 'number' ? window.totalSteps : 5;
    var cs = typeof window.currentStep === 'number' ? window.currentStep : 0;
    if(cs >= ts) {
      // trigger scene6
      var svgC = document.querySelector('.svg-container');
      if(svgC){ svgC.style.transition='opacity .45s ease'; svgC.style.opacity='0'; }
      setTimeout(function(){
        if(typeof window.qanim_showScene6==='function') window.qanim_showScene6();
      }, 460);
    } else {
      _origNext();
    }
  };
})();
</script>"""
        if '</body>' in html:
            html = html.replace('</body>', patch + '\n</body>', 1)
        return html

    # ── Pass 3: fix SVG xmlns attribute ──────────────────────────────────
    @staticmethod
    def _fix_svg_xmlns(html: str) -> str:
        html = re.sub(
            r'<svg(?![^>]*xmlns)',
            '<svg xmlns="http://www.w3.org/2000/svg"',
            html,
            flags=re.IGNORECASE
        )
        return html

    # ── Pass 4: fix broken badge colors (missing class) ──────────────────
    @staticmethod
    def _fix_badge_classes(html: str) -> str:
        # Ensure badge class names are correct
        html = re.sub(r'class="badge\s+gc-blue"',  'class="badge cyan"',   html)
        html = re.sub(r'class="badge\s+gc-teal"',  'class="badge cyan"',   html)
        html = re.sub(r'class="badge\s+gc-amber"',  'class="badge orange"', html)
        html = re.sub(r'class="badge\s+gc-green"',  'class="badge green"',  html)
        return html

    # ── Pass 5: ensure step-bar id exists ────────────────────────────────
    @staticmethod
    def _fix_step_bar_id(html: str) -> str:
        if 'id="step-bar"' not in html and 'id=\'step-bar\'' not in html:
            # Try to add id to the progress bar div
            html = re.sub(
                r'class="step-progress-bar"(?!\s*id=)',
                'class="step-progress-bar" id="step-bar"',
                html, count=1
            )
        return html

    # ── Pass 6: fix stray single-quote apostrophes in JS strings ─────────
    @staticmethod
    def _fix_js_apostrophes(html: str) -> str:
        return JsSyntaxValidator.auto_fix_stray_apostrophes(html)

    # ── Pass 7: ensure all SVG layers start with correct opacity ─────────
    @staticmethod
    def _fix_svg_layer_opacity(html: str, scene_script: dict) -> str:
        """
        For every svg_component except layer-frame, ensure the group
        starts with opacity="0" (not 1).
        """
        components = scene_script.get("svg_components", {})
        for layer_id, info in components.items():
            if layer_id == "layer-frame":
                continue
            # Check if this layer group exists and has wrong initial opacity
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

    # ── Pass 8: ensure totalSteps is correct ─────────────────────────────
    @staticmethod
    def _fix_total_steps(html: str, scene_script: dict) -> str:
        n_steps = len(scene_script.get("steps", []))
        if n_steps > 0:
            last_idx = n_steps - 1
            # Replace any var totalSteps = N; with the correct value
            html = re.sub(
                r'var\s+totalSteps\s*=\s*\d+\s*;',
                f'var totalSteps = {last_idx};',
                html
            )
        return html

    # ── Pass 9: ensure step-label id exists ──────────────────────────────
    @staticmethod
    def _fix_step_label_id(html: str) -> str:
        if 'id="step-label"' not in html:
            # Inject a step label element near the progress bar if missing
            html = re.sub(
                r'(<div[^>]*class="step-progress-wrap"[^>]*>)',
                r'\1\n<div id="step-label" style="font-size:12px;font-weight:700;color:#64748b;text-align:center;margin-bottom:12px;padding-top:6px;">Step 1 of 9</div>',
                html, count=1
            )
        return html

    # ── Pass 10: inject MathJax if LaTeX detected ─────────────────────────
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
        html = cls._fix_step_dot_sync(html)
        html = cls._fix_next_button_trigger(html)
        html = cls._fix_svg_xmlns(html)
        html = cls._fix_badge_classes(html)
        html = cls._fix_step_bar_id(html)
        html = cls._fix_step_label_id(html)
        html = cls._fix_svg_layer_opacity(html, scene_script)
        html = cls._fix_total_steps(html, scene_script)
        html = cls._inject_mathjax_if_needed(html)
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
        """Upgrade typography in badge text spans only (safe — won't break code)"""
        def _upgrade_badge(m):
            return m.group(0)  # conservative — don't modify SVG/JS context
        return html



# ===========================================================================
#  MODULE 24 — Main Pipeline: generate_animation_html()
# ===========================================================================

async def generate_animation_html(question: str) -> str:
    """
    Full 9-step QAnim pipeline:

    Stage A: Parallel — scene analysis + solution generation + glossary analysis
    Stage B: Build HTML (GeminiAnimationBuilder)
    Stage C: Post-processing — inject all 9-step panels + controls
    Stage D: Reliability passes, sanitization, centering CSS

    Returns complete self-contained HTML string.
    """
    QAnimLogger.info("Pipeline", f"Starting 9-step pipeline for: {question[:80]!r}")
    if not question or not question.strip():
        return RecoveryEngine.fallback_html("(empty)", "Question was empty")

    # ── Pre-processing ────────────────────────────────────────────────────
    question = LargeInputPreprocessor.compress(question) if LargeInputPreprocessor.needs_compression(question) else question
    to_find_targets = ToFindExtractor.extract(question)
    given_values    = GivenValuesExtractor.extract(question)

    # ── Stage A: Parallel analysis ────────────────────────────────────────
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

    # Handle exceptions from gather
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

    # Classify topic
    topic = await _classify_topic_async(question)
    QAnimLogger.ok("Pipeline", f"Topic: {topic}, To Find: {to_find_targets}, Given values: {len(given_values)}")

    # ── Stage B: Build HTML ───────────────────────────────────────────────
    QAnimLogger.info("Pipeline", "Stage B: Building HTML animation...")
    html = await GeminiAnimationBuilder.build_async(question, scene_script, sol, topic)

    if not html or len(html) < 500:
        QAnimLogger.error("Pipeline", "Stage B produced empty/invalid HTML — returning fallback")
        return RecoveryEngine.fallback_html(question, "HTML builder returned empty content")

    # ── Stage C: Post-processing ──────────────────────────────────────────
    QAnimLogger.info("Pipeline", "Stage C: Injecting 9-step panels...")

    # Build answer targets for the answer box
    answer_targets = _build_answer_targets(to_find_targets, sol, scene_script.get("final_answer", ""), scene_script.get("key_insight", ""))

    # Override answer targets with scene9 data if available
    fad = scene_script.get("final_answer_data", {})
    if fad.get("answer_value"):
        answer_targets = [{
            "label": fad.get("to_find_label", to_find_targets[0] if to_find_targets else "Final Answer"),
            "value": fad.get("answer_value", ""),
            "insight": scene_script.get("key_insight", sol.get("key_insight", "")),
        }]

    # Normalize document skeleton
    html = DocumentSkeletonNormalizer.normalize(html)

    # Inject Scene 6 (Step 7: Main Formula)
    html = inject_scene6_big_idea(html, sol, scene_script)

    # Inject Scene 7 (Step 8: Step-by-Step Substitution)
    html = inject_scene7_how_we_solve_it(html, sol, scene_script)

    # Inject Scene 9 (Step 9: Final Answer)
    html = inject_scene9_final_answer(html, sol, scene_script, to_find_targets)

    # Inject To Find panel
    html = inject_to_find_system(html, to_find_targets)

    # Inject Answer Box
    html = inject_answer_box(html, answer_targets)

    # Inject Controls Bar (Answer Box + To Find buttons)
    html = inject_controls_bar(html)

    # Inject Glossary
    if glossary_terms:
        html = inject_glossary(html, glossary_terms)

    # Inject navigation patch
    html = inject_nav_patch_and_scene_desc(html)

    # Inject step controller
    html = inject_step_controller(html)

    # Inject Previous Step button
    html = inject_prev_step_button(html)

    # ── Stage D: Final passes ─────────────────────────────────────────────
    QAnimLogger.info("Pipeline", "Stage D: Final reliability + styling passes...")

    html = PanelReliabilityEngine.run_all_passes(html, scene_script)
    html = HtmlSanitizer.sanitize(html)
    html = inject_centering_css(html)
    html = inject_step_color_css(html)

    # Final document normalization
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
            # We're inside an existing event loop (e.g., Jupyter/FastAPI)
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

# Primary entry point
def generate_animation(question: str) -> str:
    """Generate a complete 9-step animated HTML for the given question."""
    return generate_animation_html_sync(question)

# Async primary entry point
async def generate_animation_async(question: str) -> str:
    """Async version of generate_animation."""
    return await generate_animation_html(question)

# Legacy aliases kept for backward compatibility
analyse_question = GeminiSceneAnalyzer.analyze
generate_solution = GeminiSolutionGenerator.generate
build_animation = GeminiAnimationBuilder.build


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
    print("  QAnim v2.0 — 9-Step Animation Generator")
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
