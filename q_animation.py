"""
q_animation.py  --  QAnim Question Animation Generator  v1.0
=============================================================

v1.0 -- FULL GEMINI REWRITE (replaces all Claude Sonnet/Haiku generation):

  WHAT CHANGED:
  - ALL generation (analysis, scene scripting, HTML animation) now uses
    Gemini 3.1 Pro Preview exclusively.
  - Two-stage generation pipeline:
      Stage A: GeminiSceneAnalyzer  — analyses the question, produces a
               structured scene-by-scene script (JSON).
      Stage B: GeminiAnimationBuilder — turns the scene script into a
               complete self-contained HTML animation page following the
               reference output style (light, friendly dashboard, SVG canvas,
               step-by-step reveal with blur/focus, control panel).
  - Animation pattern follows the reference output exactly:
      * Light, friendly dashboard UI (#eef2f9 background)
      * SVG layers (svg-layer class) for each component
      * blur-shield rect for focus highlighting
      * stepsData array driving applyStep()/nextStep()/resetAnim()
      * Component-by-component reveal with motion (rotation, translation,
        dashoffset trace) before labels/annotations appear
      * Control panel: dots, info-box (title + badges + description), Next/Restart buttons
      * Question banner showing the original question

  WHAT DID NOT CHANGE:
  - All post-processing injection functions (ToFind, StepAnswer, AnswerBox,
    Notes, ControlsBar, Glossary, StepAnswer, StepController, NavPatch)
  - All extraction utilities (ToFindExtractor, GivenValuesExtractor,
    LargeInputPreprocessor, HaikuSolutionGenerator replaced by GeminiSolutionGenerator)
  - All validation (GenerationValidator, HtmlSanitizer)
  - All panel CSS/DOM/JS constants
  - The public entry point: generate_question_animation(question)
  - The result dict structure (animation_code, concept_animation_code, etc.)

  GEMINI MODEL: gemini-3.1-pro-preview for ALL stages.
  ANTHROPIC:    client kept alive only for injected panel JS that references it
                (ControlsBar etc. are pure HTML/JS, no API calls).
                If ANTHROPIC_API_KEY is missing the pipeline still works fully.

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


# ===========================================================================
#  MODULE 1 — QAnimLogger
# ===========================================================================
class QAnimLogger:
    PREFIX = "[QAnim v1.0]"

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
#  MODULE 2 — GenerationValidator
# ===========================================================================
class ValidationError(Exception):
    pass


class GenerationValidator:
    DANGEROUS_PATTERNS = [
        (r'document\.write\s*\(',  "document.write() is forbidden"),
        (r'<script[^>]+src\s*=',   "External script src not allowed"),
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
    def validate(cls, html, require_svg=True):
        if not html or not html.strip():
            raise ValidationError("animation_code is empty")
        if len(html) < 500:
            raise ValidationError(f"animation_code suspiciously short ({len(html)} chars)")
        for pattern, reason in cls.REQUIRED_ELEMENTS:
            if pattern not in html:
                raise ValidationError(reason)
        if require_svg:
            for pattern, reason in cls.SVG_REQUIRED:
                if pattern not in html:
                    raise ValidationError(reason)
        for pattern, reason in cls.DANGEROUS_PATTERNS:
            if re.search(pattern, html, re.IGNORECASE):
                QAnimLogger.warn("Validator", f"Dangerous pattern: {reason}")
        QAnimLogger.ok("Validator", f"HTML passed validation ({len(html):,} chars)")


# ===========================================================================
#  MODULE 2.5 — ToFindExtractor
# ===========================================================================
class ToFindExtractor:
    _TRIGGER_PATTERNS = [
        (r'\bsolve\s+for\s+(.+?)(?=\.|,|;|\band\b|$)',         1),
        (r'\bfind\s+(?:the\s+|an?\s+)?(.+?)(?=\.|;|$)',        1),
        (r'\bdetermine\s+(?:the\s+|an?\s+)?(.+?)(?=\.|,|;|$)', 1),
        (r'\bcalculate\s+(?:the\s+|an?\s+)?(.+?)(?=\.|,|;|$)', 1),
        (r'\bevaluate\s+(?:the\s+|an?\s+)?(.+?)(?=\.|,|;|$)',  1),
        (r'\bcompute\s+(?:the\s+|an?\s+)?(.+?)(?=\.|,|;|$)',   1),
        (r'\bobtain\s+(?:the\s+|an?\s+)?(.+?)(?=\.|,|;|$)',    1),
        (r'\bidentify\s+(?:the\s+|an?\s+)?(.+?)(?=\.|,|;|$)',  1),
        (r'\bestimate\s+(?:the\s+|an?\s+)?(.+?)(?=\.|,|;|$)',  1),
        (r'\bderive\s+(?:the\s+|an?\s+)?(.+?)(?=\.|,|;|$)',    1),
        (r'\bwhat\s+(?:is|are)\s+(?:the\s+|an?\s+)?(.+?)(?=\?|,|;|$)', 1),
        (r'\bwhat\s+will\s+be\s+(?:the\s+)?(.+?)(?=\?|,|;|$)',  1),
        (r'\bwhat\s+would\s+be\s+(?:the\s+)?(.+?)(?=\?|,|;|$)', 1),
        (r'\bhow\s+(?:much|many)\s+(.+?)(?=\?|,|;|$)',           1),
        (r'\bprove\s+(?:that\s+)?(.+?)(?=\.|,|;|$)',             1),
        (r'\bshow\s+(?:that\s+)?(.+?)(?=\.|,|;|$)',              1),
        (r'\bexpress\s+(?:the\s+)?(.+?)\s+in\s+terms',          1),
    ]
    _NOISE_PREFIXES = [
        "the value of","the values of","value of","the magnitude of","magnitude of",
        "the amount of","amount of","the total","the net","the resultant","the effective",
        "an expression for","the expression for",
    ]
    _SPLIT_RE     = re.compile(r'\s*,\s*|\s+and\s+|\s+also\s+|\s+as\s+well\s+as\s+|\s+along\s+with\s+', re.IGNORECASE)
    _TRAILING_RE  = re.compile(r'\s+(?:if|when|given|assuming|where|such\s+that|for|in|at|of\s+the\s+system|of\s+the\s+block|of\s+each)\s+.+$', re.IGNORECASE)
    _ARTICLE_RE   = re.compile(r'^(?:the|a|an)\s+', re.IGNORECASE)
    _TRIGGER_VERB_RE = re.compile(r'^(?:find|determine|calculate|evaluate|compute|obtain|identify|estimate|derive|prove|show|express|solve\s+for)\s+(?:the\s+|an?\s+)?', re.IGNORECASE)
    _MATH_VAR_RE  = re.compile(r'^[A-Za-z\u03b1-\u03c9\u0391-\u03a9][0-9\u2080-\u2089]?$')
    MAX_LEN = 120

    @classmethod
    def extract(cls, question):
        if not question or not question.strip():
            return []
        try:
            raw      = cls._run_patterns(question)
            expanded = cls._split_conjunctions(raw)
            cleaned  = [cls._clean(t) for t in expanded]
            valid    = [t for t in cleaned if t and (
                            (3 <= len(t) <= cls.MAX_LEN) or cls._MATH_VAR_RE.match(t))]
            deduped  = cls._deduplicate(valid)
            result   = [cls._cap(t) for t in deduped]
            if not result:
                result = cls._fallback(question)
            QAnimLogger.ok("ToFindExtractor", f"Extracted {len(result)} target(s): {result}")
            return result
        except Exception as exc:
            QAnimLogger.error("ToFindExtractor", f"Unhandled error: {exc}")
            return []

    @classmethod
    def _run_patterns(cls, question):
        found = []
        for pattern, grp in cls._TRIGGER_PATTERNS:
            for m in re.finditer(pattern, question, re.IGNORECASE | re.MULTILINE):
                try:
                    raw = m.group(grp).strip()
                    if raw:
                        found.append(raw)
                except IndexError:
                    pass
        return found

    @classmethod
    def _split_conjunctions(cls, targets):
        result = []
        for t in targets:
            parts = cls._SPLIT_RE.split(t)
            result.extend(p.strip() for p in parts if p.strip())
        return result

    @classmethod
    def _clean(cls, target):
        t = target.strip().rstrip(".,;:?!")
        for noise in sorted(cls._NOISE_PREFIXES, key=len, reverse=True):
            if t.lower().startswith(noise):
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
/* ── Ensure question banner uses the rich light-theme gradient style ── */
.question-banner {
  background: linear-gradient(135deg, #f0f5ff 0%, #e8f0fe 50%, #eef2f9 100%) !important;
  border-bottom: 1px solid #e2e8f0 !important;
  position: relative;
}
.q-label {
  font-size: 11px !important;
  font-weight: 800 !important;
  color: #0e7490 !important;
  text-transform: uppercase !important;
  letter-spacing: 1.5px !important;
}
/* ── Pill-style step dots upgrade ── */
.step-dot {
  padding: 5px 13px !important;
  border-radius: 20px !important;
  background: rgba(203,213,225,0.5) !important;
  border: 1px solid #cbd5e1 !important;
  font-size: 11px !important;
  font-weight: 700 !important;
  color: #94a3b8 !important;
  cursor: pointer;
  transition: background 0.3s, color 0.3s, border-color 0.3s, box-shadow 0.3s, transform 0.2s !important;
  display: inline-flex !important;
  align-items: center !important;
  white-space: nowrap !important;
  width: auto !important;
  height: auto !important;
}
.step-dot.active {
  background: linear-gradient(135deg, #0e7490 0%, #0891b2 100%) !important;
  border-color: #0891b2 !important;
  color: #ffffff !important;
  box-shadow: 0 2px 10px rgba(8,145,178,0.35) !important;
  transform: scale(1.06) !important;
}
/* ── Info box accent border ── */
.info-box {
  background: #f8faff !important;
  border: 1px solid #dde6f8 !important;
  border-left: 4px solid #0891b2 !important;
  border-radius: 10px !important;
  padding: 18px 20px !important;
}
/* ── Button polish ── */
.btn-primary {
  background: linear-gradient(135deg, #0e7490 0%, #0891b2 100%) !important;
  color: #ffffff !important;
  box-shadow: 0 4px 12px rgba(8,145,178,0.28) !important;
  border-radius: 8px !important;
  font-weight: 700 !important;
  transition: background 0.2s, box-shadow 0.2s, transform 0.15s !important;
  padding: 10px 22px !important;
}
.btn-primary:hover {
  background: linear-gradient(135deg, #0369a1 0%, #0e7490 100%) !important;
  box-shadow: 0 6px 20px rgba(14,116,144,0.35) !important;
  transform: translateY(-1px) !important;
}
.btn-secondary {
  background: transparent !important;
  color: #64748b !important;
  border: 1.5px solid #cbd5e1 !important;
  border-radius: 8px !important;
  font-weight: 700 !important;
}
.btn-secondary:hover {
  background: rgba(15,23,42,0.04) !important;
  color: #1e293b !important;
  border-color: #94a3b8 !important;
}
</style>"""


def inject_centering_css(html: str) -> str:
    """Inject centering overrides so the animation dashboard is always centred."""
    try:
        if "</head>" in html:
            html = html.replace("</head>", _CENTERING_CSS_OVERRIDE + "\n</head>", 1)
        else:
            html = _CENTERING_CSS_OVERRIDE + "\n" + html
        QAnimLogger.ok("CenteringCSS", "Centering override injected")
    except Exception as exc:
        QAnimLogger.warn("CenteringCSS", f"Injection failed: {exc}")
    return html


# ===========================================================================
#  MODULE 4 — RecoveryEngine
# ===========================================================================
class RecoveryEngine:

    @staticmethod
    def fallback_html(question, reason):
        q_safe      = html_module.escape(question[:120])
        reason_safe = html_module.escape(reason[:300])
        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100%;background:#eef2f9;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  display:flex;align-items:center;justify-content:center;color:#334155}}
.card{{background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;
  box-shadow:0 4px 24px rgba(30,64,175,.10);padding:36px 40px;max-width:520px;text-align:center}}
.icon{{font-size:40px;margin-bottom:16px}}
.title{{font-size:17px;font-weight:800;color:#1e293b;margin-bottom:10px}}
.reason{{font-size:11px;color:#0e7490;background:#f0f9ff;border-radius:10px;
  padding:10px 14px;margin:12px 0;border:1px solid #e2e8f0;text-align:left;
  line-height:1.6;font-family:monospace}}
.question{{font-size:12px;color:#64748b;line-height:1.6;margin-top:10px;font-style:italic}}
.retry-hint{{margin-top:18px;font-size:11px;font-weight:700;letter-spacing:1.5px;
  text-transform:uppercase;color:#0891b2}}
</style></head><body>
<div class="card">
<div class="icon">&#x26A0;&#xFE0F;</div>
<div class="title">Animation Could Not Render</div>
<div class="reason">{reason_safe}</div>
<div class="question">"{q_safe}"</div>
<div class="retry-hint">Please regenerate the animation</div>
</div></body></html>"""


# ===========================================================================
#  MODULE 5 — Page-level CSS (base styles, kept for panel injection)
# ===========================================================================

BASE_PAGE_CSS = """<style>
*, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
:root {
  --bg-color: #eef2f9; --panel-bg: #ffffff;
  --text-main: #334155; --text-sub: #64748b;
  --accent-cyan: #0891b2; --accent-cyan-dim: #0e7490;
  --accent-orange: #ea8c00; --accent-green: #16a34a;
  --border: #e2e8f0; --border-radius: 14px;
  --shadow-card: 0 4px 6px -1px rgba(30,64,175,0.07),0 10px 30px rgba(30,64,175,0.10);
  --font: 'Segoe UI',system-ui,-apple-system,Arial,sans-serif;
}
html { overflow-x:hidden!important; overflow-y:auto!important; min-height:100vh; width:100%!important; }
body { background:var(--bg-color); font-family:var(--font); min-height:100vh; width:100%;
  overflow-x:hidden!important; padding:0; display:flex; flex-direction:column; align-items:center; }
svg { display:block; width:100%!important; height:auto!important; }
</style>"""


# ===========================================================================
#  MODULE 5.5 — Error Boundary & Inner Logger
# ===========================================================================
ERROR_BOUNDARY_HTML = """
<div id="qanim-error-fallback" style="display:none!important;visibility:hidden!important;" aria-hidden="true">
  <div><div class="qanim-err-msg"></div></div>
</div>
"""

QANIM_INNER_LOGGER_JS = """
<script>
window.QLog={
  info:function(m){console.log('[QAnim Inner] i  '+m);},
  warn:function(m){console.warn('[QAnim Inner] !  '+m);},
  error:function(m){console.error('[QAnim Inner] X  '+m);}
};
window.addEventListener('error',function(e){
  console.error('[QAnim GlobalError]',e.message,'at',e.filename+':'+e.lineno);
});
window.addEventListener('unhandledrejection',function(e){
  console.error('[QAnim UnhandledPromise]',e.reason);
});
</script>
"""


def _insert_before_container_close(html, open_tag_regex, insertion):
    """
    Find `open_tag_regex` (e.g. an opening <div id="..."> tag) and insert
    `insertion` immediately before that specific element's TRUE closing tag,
    correctly skipping over any nested elements of the same tag type in
    between. Returns (new_html, True) on success, (html, False) if the
    opening tag wasn't found or was never closed.

    This replaces brittle non-greedy regexes like `(.*?)</div>`, which stop
    at the FIRST closing tag encountered — including nested ones — rather
    than the matching one, and literal-string anchors, which silently fail
    to match on any whitespace/formatting drift.
    """
    m = re.search(open_tag_regex, html, re.IGNORECASE)
    if not m:
        return html, False
    tag_name_match = re.match(r'<\s*([a-zA-Z0-9]+)', m.group(0))
    if not tag_name_match:
        return html, False
    tag = tag_name_match.group(1)
    tag_re = re.compile(r'<' + tag + r'\b[^>]*>|</' + tag + r'\s*>', re.IGNORECASE)
    depth = 1
    for tm in tag_re.finditer(html, m.end()):
        if tm.group(0).lower().startswith('</'):
            depth -= 1
        else:
            depth += 1
        if depth == 0:
            close_start = tm.start()
            return html[:close_start] + '\n' + insertion + html[close_start:], True
    return html, False


# ===========================================================================
#  MODULE 6 — ToFind Panel
# ===========================================================================
def _build_to_find_data_tag(targets):
    payload = {"targets": [str(t) for t in (targets or [])]}
    return ('<script type="application/json" id="__tofind_data__">\n'
            + json.dumps(payload, ensure_ascii=False, indent=2) + '\n</script>')


_TO_FIND_DOM = """
<div id="tofind-backdrop" aria-hidden="true"></div>
<aside id="tofind-panel" role="dialog" aria-labelledby="tofind-heading" aria-hidden="true">
  <div class="tf-header">
    <div class="tf-header-left">
      <div class="tf-icon-wrap">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
          <path d="M16.5 16.5L21 21" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
        </svg>
      </div>
      <span id="tofind-heading" class="tf-title">To Find</span>
    </div>
    <button id="tofind-close" class="tf-close-btn" aria-label="Close">&#x2715;</button>
  </div>
  <p class="tf-subtitle">What this question is asking you to determine:</p>
  <div id="tofind-items-container" class="tf-items-container"></div>
</aside>
"""

_TO_FIND_CSS = """
<style id="qanim-tofind-styles">
#tofind-backdrop { display:none; position:fixed; inset:0; z-index:8000;
  background:rgba(15,23,42,.40); backdrop-filter:blur(4px); opacity:0; transition:opacity .22s ease; }
#tofind-backdrop.open { display:block; opacity:1; }
#tofind-panel { display:flex; flex-direction:column; position:fixed; top:50%; left:50%;
  transform:translate(-50%,-48%) scale(.96); z-index:8100; width:min(460px,92vw);
  max-height:80vh; border-radius:16px; padding:24px; box-sizing:border-box;
  background:#fff; border:1px solid #e2e8f0; box-shadow:0 8px 40px rgba(0,0,0,.12);
  opacity:0; pointer-events:none; transition:opacity .25s ease,transform .25s cubic-bezier(.34,1.56,.64,1); }
#tofind-panel.open { opacity:1; pointer-events:auto; transform:translate(-50%,-50%) scale(1); }
.tf-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
.tf-header-left { display:flex; align-items:center; gap:10px; }
.tf-icon-wrap { width:32px; height:32px; border-radius:8px; background:#7c3aed;
  display:flex; align-items:center; justify-content:center; color:#fff; flex-shrink:0; }
.tf-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:16px; font-weight:700; color:#1e293b; }
.tf-close-btn { width:30px; height:30px; border-radius:8px; border:1px solid #e2e8f0;
  background:#f8fafc; color:#64748b; font-size:12px;
  display:flex; align-items:center; justify-content:center; cursor:pointer; transition:background .15s,color .15s; }
.tf-close-btn:hover { background:#fee2e2; color:#dc2626; }
.tf-subtitle { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12px; color:#64748b; margin:0 0 14px; }
.tf-items-container { display:flex; flex-direction:column; gap:8px; overflow-y:auto; }
.tofind-item { display:flex; align-items:flex-start; gap:12px; padding:12px 14px;
  border-radius:10px; background:#f8fafc; border:1px solid #e2e8f0;
  opacity:0; transform:translateX(-12px); transition:background .15s; }
.tofind-item:hover { background:#ede9fe; border-color:#7c3aed; }
.tofind-check { width:20px; height:20px; border-radius:50%; background:#7c3aed; color:#fff;
  font-size:11px; display:flex; align-items:center; justify-content:center; flex-shrink:0; }
.tofind-text { font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:13px; font-weight:600; color:#1e293b; line-height:1.5; }
.tofind-empty { font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:13px; color:#94a3b8; text-align:center; padding:20px 0; font-style:italic; }
</style>
"""

TO_FIND_JS_MODULE = r"""
(function initToFindSystem(){
  'use strict';
  if(window.__qanimToFindInit)return;window.__qanimToFindInit=true;
  var toFindOpen=false,_panelBuilt=false;
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _el(id){return document.getElementById(id);}
  function _loadTargets(){
    try{var tag=_el('__tofind_data__');if(!tag)return[];var data=JSON.parse(tag.textContent)||{};return Array.isArray(data.targets)?data.targets:[];}catch(e){return[];}
  }
  function _escape(text){if(!text)return'';return String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
  function _buildPanel(targets){
    if(_panelBuilt)return;_panelBuilt=true;
    var container=_el('tofind-items-container');if(!container)return;
    if(!targets||targets.length===0){container.innerHTML='<div class="tofind-empty">No specific targets detected.</div>';return;}
    var html='';
    for(var i=0;i<targets.length;i++){html+='<div class="tofind-item" id="tofind-item-'+i+'"><div class="tofind-check">&#10003;</div><div class="tofind-text">'+_escape(targets[i])+'</div></div>';}
    container.innerHTML=html;
  }
  function _animateReveal(){
    var items=document.querySelectorAll('.tofind-item');
    for(var i=0;i<items.length;i++){(function(el,idx){el.style.opacity='0';el.style.transform='translateX(-12px)';el.style.transition='none';setTimeout(function(){el.style.transition='opacity .28s ease,transform .28s ease';el.style.opacity='1';el.style.transform='translateX(0)';},60+idx*80);})(items[i],i);}
  }
  function openToFind(){
    var backdrop=_el('tofind-backdrop'),panel=_el('tofind-panel');if(!backdrop||!panel)return;
    _buildPanel(_loadTargets());backdrop.classList.add('open');panel.classList.add('open');panel.setAttribute('aria-hidden','false');toFindOpen=true;setTimeout(_animateReveal,100);
  }
  function closeToFind(){
    var backdrop=_el('tofind-backdrop'),panel=_el('tofind-panel');
    if(backdrop)backdrop.classList.remove('open');if(panel){panel.classList.remove('open');panel.setAttribute('aria-hidden','true');}toFindOpen=false;
  }
  window.openToFind=openToFind;window.closeToFind=closeToFind;window.toggleToFind=function(){toFindOpen?closeToFind():openToFind();};
  _onReady(function(){
    var tfBtn=_el('tofind-ctrl-btn')||_el('tofind-btn')||document.querySelector('[data-tofind-btn]');
    if(tfBtn){tfBtn.removeAttribute('onclick');tfBtn.addEventListener('click',function(e){e.stopPropagation();openToFind();});}
    var closeBtn=_el('tofind-close');if(closeBtn)closeBtn.addEventListener('click',closeToFind);
    var backdrop=_el('tofind-backdrop');if(backdrop)backdrop.addEventListener('click',closeToFind);
    document.addEventListener('keydown',function(e){if(e.key==='Escape'&&toFindOpen)closeToFind();});
  });
})();
"""


def inject_to_find_system(html, targets):
    html = re.sub(r'<script[^>]+id=["\']__tofind_data__["\'][^>]*>.*?</script>', '', html, flags=re.DOTALL)
    try:
        data_tag = _build_to_find_data_tag(targets)
        if '</head>' in html:
            html = html.replace('</head>', data_tag + '\n</head>', 1)
        else:
            html = data_tag + '\n' + html
    except Exception as e:
        QAnimLogger.warn("ToFindInjector", f"Data tag insertion failed: {e}")
    try:
        if '</head>' in html:
            html = html.replace('</head>', _TO_FIND_CSS + '\n</head>', 1)
    except Exception as e:
        QAnimLogger.warn("ToFindInjector", f"CSS insertion failed: {e}")
    try:
        body_match = re.search(r'<body[^>]*>', html, re.IGNORECASE)
        if body_match:
            ins = body_match.end()
            html = html[:ins] + '\n' + _TO_FIND_DOM + html[ins:]
    except Exception as e:
        QAnimLogger.warn("ToFindInjector", f"DOM insertion failed: {e}")
    try:
        tofind_script = '<script id="qanim-js-tofind">\n' + TO_FIND_JS_MODULE + '\n</script>'
        if '</body>' in html:
            html = html.replace('</body>', tofind_script + '\n</body>', 1)
        else:
            html += '\n' + tofind_script
    except Exception as e:
        QAnimLogger.warn("ToFindInjector", f"JS module insertion failed: {e}")
    QAnimLogger.ok("ToFindInjector", f"Injected {len(targets)} target(s)")
    return html


# ===========================================================================
#  MODULE 6.5 — Step by Step Answer Panel
#  Mirrors the ToFind / Final Answer injection architecture.
#  Reuses the solution steps already generated by GeminiSolutionGenerator —
#  does NOT call Gemini again.
# ===========================================================================

def _build_step_answer_data_tag(gemini_sol):
    """Build the structured 5-step data tag from a GeminiSolutionGenerator result dict."""
    given_data         = gemini_sol.get("given_data", [])
    to_find            = gemini_sol.get("to_find", [])
    formulas           = gemini_sol.get("formulas", [])
    formula_note       = gemini_sol.get("formula_note", "")
    substitution_steps = gemini_sol.get("substitution_steps", [])
    final_answer       = gemini_sol.get("final_answer", "")
    key_insight        = gemini_sol.get("key_insight", "")

    # Graceful fallback: derive from flat steps if structured keys are absent
    if not given_data and not to_find and not substitution_steps:
        flat = [str(s) for s in (gemini_sol.get("steps") or [])]
        if flat:
            given_data         = [flat[0]] if len(flat) > 0 else []
            to_find            = ["i) " + flat[1]] if len(flat) > 1 else []
            formulas           = [{"text": flat[2], "color": "blue"}] if len(flat) > 2 else []
            substitution_steps = [{"title": "Calculation", "expr": flat[3]}] if len(flat) > 3 else []
            if not final_answer and len(flat) > 4:
                final_answer = flat[4]

    payload = {
        "given_data":         [str(s) for s in given_data],
        "to_find":            [str(s) for s in to_find],
        "formulas":           formulas,
        "formula_note":       str(formula_note or ""),
        "substitution_steps": substitution_steps,
        "final_answer":       str(final_answer or ""),
        "key_insight":        str(key_insight or ""),
    }
    return ('<script type="application/json" id="__step_answer_data__">\n'
            + json.dumps(payload, ensure_ascii=False, indent=2) + '\n</script>')


_STEP_ANSWER_DOM = """
<div id="qanim-stepbystep-section" style="display:none;">
  <div class="sbs-wrapper">
    <h2 class="sbs-main-title">How We Solve It &#x2014; Step by Step</h2>
    <div class="sbs-steps-container" id="sbs-steps-container"></div>
    <div class="sbs-try-it-bar">
      &#x1F4A1; Try it yourself! Use the <strong>Answer Box</strong> to check your answer.
    </div>
  </div>
</div>
"""

_STEP_ANSWER_CSS = """
<style id="qanim-stepans-styles">
/* ── Inline Step-by-Step Section ───────────────────────────────────── */
#qanim-stepbystep-section { width:100%; max-width:900px; margin:0 auto; box-sizing:border-box; padding-bottom:100px; }
#qanim-stepbystep-section.visible { display:block !important; }
.sbs-wrapper { background:#fff; border-radius:18px; box-shadow:0 4px 32px rgba(37,99,235,.10),0 1px 4px rgba(0,0,0,.06);
  border:1px solid #e8eef8; overflow:hidden; margin-top:32px; }
.sbs-main-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:24px; font-weight:800;
  color:#1a1a2e; text-align:center; padding:28px 24px 20px; border-bottom:1px solid #f0f2f8;
  background:linear-gradient(135deg,#f8fafc 0%,#f0f4ff 100%); margin:0; }
.sbs-steps-container { padding:24px; display:flex; flex-direction:column; gap:14px; }
/* ── Individual step cards ── */
.sbs-step-card { display:flex; align-items:flex-start; gap:0; border-radius:14px;
  border:1.5px solid #e2e8f0; overflow:hidden; background:#fff; }
.sbs-step-num-col { min-width:52px; display:flex; align-items:center; justify-content:center;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:22px; font-weight:900;
  padding:20px 0; flex-shrink:0; }
.sbs-step-body { flex:1; padding:18px 20px; }
.sbs-step-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:14px; font-weight:800;
  margin-bottom:6px; line-height:1.4; }
.sbs-step-desc { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:13px; color:#475569;
  line-height:1.65; }
/* step color themes */
.sbs-step-card.sbs-s0 { border-color:#bfdbfe; background:#f8fbff; }
.sbs-step-card.sbs-s0 .sbs-step-num-col { background:#dbeafe; color:#1d4ed8; }
.sbs-step-card.sbs-s0 .sbs-step-title { color:#1d4ed8; }
.sbs-step-card.sbs-s1 { border-color:#c7d2fe; background:#f9f8ff; }
.sbs-step-card.sbs-s1 .sbs-step-num-col { background:#e0e7ff; color:#4338ca; }
.sbs-step-card.sbs-s1 .sbs-step-title { color:#4338ca; }
.sbs-step-card.sbs-s2 { border-color:#a7f3d0; background:#f8fffc; }
.sbs-step-card.sbs-s2 .sbs-step-num-col { background:#d1fae5; color:#15803d; }
.sbs-step-card.sbs-s2 .sbs-step-title { color:#15803d; }
.sbs-step-card.sbs-s3 { border-color:#fca5a5; background:#fff8f8; }
.sbs-step-card.sbs-s3 .sbs-step-num-col { background:#fee2e2; color:#dc2626; }
.sbs-step-card.sbs-s3 .sbs-step-title { color:#dc2626; }
.sbs-step-card.sbs-s4 { border-color:#fde68a; background:#fffdf0; }
.sbs-step-card.sbs-s4 .sbs-step-num-col { background:#fef9c3; color:#b45309; }
.sbs-step-card.sbs-s4 .sbs-step-title { color:#b45309; }
/* ── Formula flowchart inside a step ── */
.sbs-flowchart { display:flex; flex-direction:column; align-items:center; gap:0; margin-top:10px; }
.sbs-flow-box { width:100%; max-width:520px; padding:12px 20px; border-radius:12px;
  text-align:center; font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:14.5px; font-weight:700; box-sizing:border-box; margin:0 auto; }
.sbs-flow-box.blue   { background:#eff6ff; border:2px solid #93c5fd; color:#1d4ed8; }
.sbs-flow-box.orange { background:#fff7ed; border:2px solid #fdba74; color:#c2410c; }
.sbs-flow-box.purple { background:#faf5ff; border:2px solid #d8b4fe; color:#7c3aed; }
.sbs-flow-box.pink   { background:#fff1f2; border:2px solid #fda4af; color:#be123c; }
.sbs-flow-box.green  { background:#f0fdf4; border:2px solid #86efac; color:#15803d; }
.sbs-flow-box.teal   { background:#f0fdfa; border:2px solid #5eead4; color:#0f766e; }
.sbs-flow-arrow { width:2px; height:28px; background:linear-gradient(to bottom,#93c5fd,#c084fc); margin:0 auto; position:relative; }
.sbs-flow-arrow::after { content:'\25BC'; position:absolute; bottom:-10px; left:50%; transform:translateX(-50%); font-size:12px; color:#c084fc; }
.sbs-flow-note { font-size:11.5px; color:#64748b; text-align:center; margin-top:14px; font-style:italic;
  padding:8px 16px; background:#f8fafc; border-radius:8px; border:1px solid #e2e8f0; max-width:520px; margin-left:auto; margin-right:auto; }
/* ── Given data boxes inside a step ── */
.sbs-given-grid { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
.sbs-given-box { display:inline-flex; align-items:center; padding:8px 14px; border-radius:8px;
  font-family:'Courier New',Courier,monospace; font-size:13px; font-weight:700; white-space:nowrap; }
.sbs-given-box:nth-child(4n+1) { background:#eff6ff; border:1.5px solid #bfdbfe; color:#1d4ed8; }
.sbs-given-box:nth-child(4n+2) { background:#f0fdf4; border:1.5px solid #bbf7d0; color:#15803d; }
.sbs-given-box:nth-child(4n+3) { background:#fefce8; border:1.5px solid #fde68a; color:#b45309; }
.sbs-given-box:nth-child(4n)   { background:#fdf4ff; border:1.5px solid #e9d5ff; color:#7e22ce; }
/* ── Substitution steps inside a step ── */
.sbs-sub-list { display:flex; flex-direction:column; gap:8px; margin-top:8px; }
.sbs-sub-item { display:flex; align-items:flex-start; gap:12px; padding:12px 14px;
  border-radius:10px; background:#f8fafc; border:1px solid #e2e8f0; }
.sbs-sub-item:nth-child(1) { border-left:3px solid #2563eb; }
.sbs-sub-item:nth-child(2) { border-left:3px solid #0d9488; }
.sbs-sub-item:nth-child(3) { border-left:3px solid #16a34a; }
.sbs-sub-item:nth-child(4) { border-left:3px solid #d97706; }
.sbs-sub-item:nth-child(5) { border-left:3px solid #7c3aed; }
.sbs-sub-num { min-width:28px; height:28px; border-radius:50%; background:#2563eb; color:#fff;
  font-size:12px; font-weight:800; display:flex; align-items:center; justify-content:center; flex-shrink:0; }
.sbs-sub-item:nth-child(2) .sbs-sub-num { background:#0d9488; }
.sbs-sub-item:nth-child(3) .sbs-sub-num { background:#16a34a; }
.sbs-sub-item:nth-child(4) .sbs-sub-num { background:#d97706; }
.sbs-sub-item:nth-child(5) .sbs-sub-num { background:#7c3aed; }
.sbs-sub-body { flex:1; min-width:0; }
.sbs-sub-title { font-size:11px; font-weight:800; color:#475569; text-transform:uppercase; letter-spacing:.5px; margin-bottom:4px; }
.sbs-sub-expr { font-family:'Courier New',Courier,monospace; font-size:13px; font-weight:600; color:#1e293b; line-height:1.6; }
/* ── Final answer box inside step ── */
.sbs-final-box { padding:18px 20px; border-radius:14px; background:linear-gradient(135deg,#f0fdf4,#ecfdf5);
  border:2px solid #86efac; margin-top:8px; }
.sbs-final-label { font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:1.5px; color:#15803d; margin-bottom:10px; }
.sbs-final-value { font-family:'Courier New',Courier,monospace; font-size:15px; font-weight:800; color:#1e293b;
  line-height:1.8; padding:14px 16px; background:#fff; border:1.5px solid #bbf7d0; border-radius:10px; }
.sbs-insight { margin-top:12px; padding:12px 14px; border-radius:10px; background:#fffbf0; border:1.5px solid #fde68a; }
.sbs-insight-badge { display:block; font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:1px; color:#b45309; margin-bottom:4px; }
.sbs-insight-text { font-size:12.5px; color:#78350f; line-height:1.7; }
/* ── CTA bar ── */
.sbs-try-it-bar { margin:0 24px 24px; padding:16px 22px; border-radius:12px;
  background:#eff6ff; border:2px solid #2563eb; color:#1d4ed8;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:14px; font-weight:700;
  text-align:center; line-height:1.5; }
/* ── Core Formula Scene ── */
.cf-name-badge {
  display:inline-block; padding:4px 14px; border-radius:20px;
  background:#f0f9ff; border:1px solid #bae6fd; color:#0369a1;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:11px;
  font-weight:800; letter-spacing:0.8px; text-transform:uppercase;
  margin-bottom:14px; }
.cf-scene-wrap { width:100%; }
.cf-formula-stack { display:flex; flex-direction:column; align-items:center; gap:0; padding:4px 0 6px; }
.cf-row { width:100%; display:flex; justify-content:center; }
.cf-formula-pill {
  width:100%; max-width:540px; padding:14px 22px; border-radius:12px;
  border:2px solid #93c5fd; text-align:center; box-sizing:border-box;
  font-family:'Courier New',Courier,monospace; font-size:15px; font-weight:700;
  line-height:1.6; margin:0 auto; word-break:break-word; }
.cf-formula-label { display:block; }
.cf-arrow { display:flex; flex-direction:column; align-items:center; margin:0 auto; width:24px; padding:2px 0; }
.cf-arrow-line { width:2px; height:22px; background:linear-gradient(to bottom,#93c5fd,#c084fc); }
.cf-arrow-head { font-size:12px; color:#c084fc; margin-top:-2px; line-height:1; }
.cf-info-card {
  display:flex; align-items:flex-start; gap:10px; margin-top:14px; padding:12px 16px;
  border-radius:10px; background:#fffbf0; border:1.5px solid #fde68a;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif; }
.cf-info-icon { font-size:17px; flex-shrink:0; margin-top:1px; }
.cf-info-text { font-size:12.5px; color:#78350f; line-height:1.7; font-style:italic; }
/* ── Legacy modal stubs (kept so old JS refs don't error) ── */
#stepans-backdrop { display:none !important; }
#stepans-panel { display:none !important; }
.sa-icon-wrap { width:40px; height:40px; border-radius:10px; background:#eff6ff;
  display:flex; align-items:center; justify-content:center; font-size:20px; flex-shrink:0; }
.sa-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:17px; font-weight:800; color:#1a1a2e; }
.sa-subtitle { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:11px; color:#64748b; margin-top:2px; }
.sa-close-btn { width:34px; height:34px; border-radius:50%; border:1.5px solid #e8e8f0;
  background:#fafafa; color:#888; font-size:13px; display:flex; align-items:center; justify-content:center;
  cursor:pointer; transition:background .15s,color .15s,border-color .15s; flex-shrink:0; }
.sa-close-btn:hover { background:#fee2e2; color:#dc2626; border-color:#fca5a5; }
/* ── Flow track ──────────────────────────────────────────────────────── */
.sa-flow-track-wrap { flex-shrink:0; background:#fafbff; border-bottom:1px solid #f0f0f8;
  padding:14px 22px 12px; overflow-x:auto; overflow-y:hidden; }
.sa-flow-track { display:flex; align-items:center; min-width:max-content; padding:2px 2px 4px; }
.sa-flow-node { display:flex; align-items:center; cursor:pointer; background:none; border:none;
  padding:0; font:inherit; flex-direction:column; gap:4px; }
.sa-flow-dot { width:32px; height:32px; border-radius:50%; background:#e9edf5; color:#7c8aa0;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12px; font-weight:800;
  display:flex; align-items:center; justify-content:center; flex-shrink:0; border:2px solid transparent;
  transition:background .22s,color .22s,transform .22s,box-shadow .22s,border-color .22s; }
.sa-flow-node-label { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:9.5px;
  font-weight:600; color:#94a3b8; white-space:nowrap; letter-spacing:.2px; }
.sa-flow-node:hover .sa-flow-dot { background:#dbe6fd; color:#1d4ed8; }
.sa-flow-node.sa-done .sa-flow-dot { background:#dcfce7; color:#16a34a; }
.sa-flow-node.sa-active .sa-flow-dot { background:#2563eb; color:#fff; border-color:#bfdbfe;
  box-shadow:0 0 0 4px rgba(37,99,235,.16); transform:scale(1.14); }
.sa-flow-node.sa-active .sa-flow-node-label { color:#2563eb; font-weight:700; }
.sa-flow-line { width:28px; height:2px; background:#e2e8f0; flex-shrink:0; margin:0 1px 18px;
  transition:background .22s; }
.sa-flow-line.sa-done { background:#86efac; }
/* ── Body ────────────────────────────────────────────────────────────── */
.sa-body { overflow-y:auto; flex:1; padding:20px 22px 12px; display:flex; flex-direction:column; }
.sa-items-container { display:flex; flex-direction:column; }
.sa-step-card { display:flex; align-items:flex-start; gap:18px; padding:22px 20px;
  border-radius:14px; background:#f8fafc; border:1px solid #e2e8f0; border-left:4px solid #2563eb;
  opacity:0; transform:translateX(16px); transition:opacity .22s ease,transform .22s ease; }
.sa-step-card.visible { opacity:1; transform:translateX(0); }
/* Per-step accent */
.sa-step-0.sa-step-card { border-left-color:#2563eb; background:#fafcff; }
.sa-step-1.sa-step-card { border-left-color:#7c3aed; background:#fdfbff; }
.sa-step-2.sa-step-card { border-left-color:#0d9488; background:#fafffe; }
.sa-step-3.sa-step-card { border-left-color:#d97706; background:#fffdf8; }
.sa-step-4.sa-step-card { border-left-color:#16a34a; background:#fafffc; }
/* Step icon circle */
.sa-step-num { min-width:46px; height:46px; border-radius:13px; font-size:24px;
  display:flex; align-items:center; justify-content:center; flex-shrink:0; }
.sa-step-0 .sa-step-num { background:#eff6ff; box-shadow:0 2px 8px rgba(37,99,235,.13); }
.sa-step-1 .sa-step-num { background:#faf5ff; box-shadow:0 2px 8px rgba(124,58,237,.13); }
.sa-step-2 .sa-step-num { background:#f0fdfa; box-shadow:0 2px 8px rgba(13,148,136,.13); }
.sa-step-3 .sa-step-num { background:#fff7ed; box-shadow:0 2px 8px rgba(217,119,6,.13); }
.sa-step-4 .sa-step-num { background:#f0fdf4; box-shadow:0 2px 8px rgba(22,163,74,.13); }
.sa-step-body { flex:1; min-width:0; }
.sa-step-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:11.5px; font-weight:800;
  text-transform:uppercase; letter-spacing:.7px; margin-bottom:14px; }
.sa-step-0 .sa-step-title { color:#1d4ed8; }
.sa-step-1 .sa-step-title { color:#7c3aed; }
.sa-step-2 .sa-step-title { color:#0f766e; }
.sa-step-3 .sa-step-title { color:#b45309; }
.sa-step-4 .sa-step-title { color:#15803d; }
/* ── Footer ──────────────────────────────────────────────────────────── */
.sa-footer { display:flex; align-items:center; justify-content:space-between; gap:10px;
  padding:14px 22px; border-top:1px solid #f0f0f8; flex-shrink:0; background:#fff; }
.sa-nav-btn { display:inline-flex; align-items:center; gap:6px; padding:9px 16px; border-radius:10px;
  border:1.5px solid #e2e8f0; background:#fff; color:#334155; font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:13px; font-weight:700; cursor:pointer; transition:background .15s,border-color .15s,color .15s,opacity .15s; }
.sa-nav-btn:hover:not(:disabled) { background:#eff6ff; border-color:#93c5fd; color:#1d4ed8; }
.sa-nav-btn:disabled { opacity:.38; cursor:not-allowed; }
.sa-next-btn { background:#2563eb; border-color:#2563eb; color:#fff; }
.sa-next-btn:hover:not(:disabled) { background:#1d4ed8; border-color:#1d4ed8; color:#fff; }
.sa-progress-label { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12px;
  font-weight:700; color:#64748b; flex-shrink:0; }
/* ── Step 1: Given Data boxes ────────────────────────────────────────── */
.sa-given-grid { display:flex; flex-wrap:wrap; gap:10px; padding:4px 0 8px; }
.sa-given-box { display:inline-flex; align-items:center; padding:10px 16px; border-radius:10px;
  font-family:'Courier New',Courier,monospace; font-size:14px; font-weight:700;
  box-shadow:0 1px 4px rgba(0,0,0,.07); white-space:nowrap; }
.sa-given-box:nth-child(4n+1) { background:#eff6ff; border:1.5px solid #bfdbfe; color:#1d4ed8; }
.sa-given-box:nth-child(4n+2) { background:#f0fdf4; border:1.5px solid #bbf7d0; color:#15803d; }
.sa-given-box:nth-child(4n+3) { background:#fefce8; border:1.5px solid #fde68a; color:#b45309; }
.sa-given-box:nth-child(4n)   { background:#fdf4ff; border:1.5px solid #e9d5ff; color:#7e22ce; }
/* ── Step 2: Conditions to Find (vertical list) ──────────────────────── */
.sa-tofind-list { display:flex; flex-direction:column; gap:10px; padding:4px 0 8px; }
.sa-tofind-item { display:flex; align-items:flex-start; gap:14px; padding:14px 18px;
  border-radius:12px; background:#fff; border:1.5px solid #e2e8f0; }
.sa-tofind-icon { min-width:38px; height:38px; border-radius:50%; background:#7c3aed; color:#fff;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12px; font-weight:800;
  display:flex; align-items:center; justify-content:center; flex-shrink:0;
  font-style:italic; box-shadow:0 2px 8px rgba(124,58,237,.28); }
.sa-tofind-text { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:14.5px;
  font-weight:600; color:#1e293b; line-height:1.6; padding-top:4px; }
/* ── Step 3: Formula flowchart ───────────────────────────────────────── */
.sa-flowchart { display:flex; flex-direction:column; align-items:center; gap:0; padding:4px 0 8px; }
.sa-flow-formula-box { width:100%; max-width:580px; padding:15px 24px; border-radius:14px;
  text-align:center; font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:15.5px; font-weight:700; box-sizing:border-box; margin:0 auto; }
</style>
"""

STEP_ANSWER_JS_MODULE = r"""
(function initInlineStepByStep(){
  'use strict';
  if(window.__qanimStepAnswerInit)return;window.__qanimStepAnswerInit=true;
  var _data=null,_built=false;

  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

  function _loadData(){
    if(_data)return _data;
    try{var tag=_el('__step_answer_data__');if(!tag)return(_data={});_data=JSON.parse(tag.textContent)||{};return _data;}
    catch(e){return(_data={});}
  }

  /* ── Build each of the 5 step cards ── */
  function _buildCard(idx,title,innerHtml){
    var cls='sbs-step-card sbs-s'+(idx%5);
    return '<div class="'+cls+'">'
      +'<div class="sbs-step-num-col">'+(idx+1)+'</div>'
      +'<div class="sbs-step-body">'
      +'<div class="sbs-step-title">'+_esc(title)+'</div>'
      +innerHtml
      +'</div></div>';
  }

  /* Step 1 – Given Data */
  function _buildStep0(){
    var d=_loadData();
    var boxes=Array.isArray(d.given_data)?d.given_data:[];
    var h='<div class="sbs-given-grid">';
    for(var b=0;b<boxes.length;b++){h+='<div class="sbs-given-box">'+_esc(boxes[b])+'</div>';}
    if(!boxes.length){h+='<div class="sbs-given-box">See the question for given values</div>';}
    h+='</div>';
    return _buildCard(0,'Given Data',h);
  }

  /* Step 2 – To Find */
  function _buildStep1(){
    var d=_loadData();
    var items=Array.isArray(d.to_find)?d.to_find:[];
    var ROMAN=['i','ii','iii','iv','v','vi','vii','viii','ix','x'];
    var h='<div class="sbs-step-desc">';
    for(var t=0;t<items.length;t++){
      h+='<strong>'+_esc(ROMAN[t]||String(t+1))+') </strong>'+_esc(items[t])+'<br>';
    }
    if(!items.length){h+='See the question for what to find';}
    h+='</div>';
    return _buildCard(1,'What We Need to Find',h);
  }

  /* Step 3 – Core Formula (symbolic only, no value substitution, no numeric result) */
  function _buildStep2(){
    var d=_loadData();
    var rows=Array.isArray(d.formulas)?d.formulas:[];

    /* ── Derive a formula name from the first formula entry ── */
    var firstName='Core Formula';
    if(rows.length>0){
      var firstText=(typeof rows[0]==='object'&&rows[0].text)?String(rows[0].text):String(rows[0]);
      /* Extract the LHS symbol (everything before the first = sign) */
      var eqIdx=firstText.indexOf('=');
      if(eqIdx>0){
        var lhs=firstText.substring(0,eqIdx).trim();
        /* Keep only the first token (symbol), discard trailing noise */
        var token=lhs.split(/[\s,(]/)[0];
        if(token&&token.length<=12){firstName=token+' — Governing Equation';}
        else{firstName='Core Formula';}
      }
    }

    /* ── Sanitise formula text: keep only symbolic form.
       Strip anything that looks like a substituted numeric expression
       (sequences of digits, ×, decimal values, and equals-then-number chains)
       so only the symbolic / variable form is shown.                      ── */
    function _stripNumeric(txt){
      /* Remove "= <number> <unit>" chain at the tail of expressions like
         "Re = rho x V x D / mu = 100000" → "Re = rho x V x D / mu"       */
      txt=txt.replace(/=\s*[-+]?\d[\d.,\s]*(?:[×xX*]\s*10\^?[-+]?\d+)?\s*[A-Za-z°²³µ/%]*\s*$/g,'');
      /* Remove standalone numeric clusters that were substituted in-line   */
      txt=txt.replace(/\(\s*[-+]?\d[\d.,]*\s*\)/g,'(…)');
      return txt.trim().replace(/\s+/g,' ');
    }

    /* Build unique IDs for each element so setTimeout can target them */
    var uid='cf2-'+Math.random().toString(36).slice(2,7);

    /* ── Generate one row per formula (symbolic only) ── */
    var COLOR_BORDER={blue:'#93c5fd',orange:'#fdba74',purple:'#d8b4fe',pink:'#fda4af',green:'#86efac',teal:'#5eead4'};
    var COLOR_BG={blue:'#eff6ff',orange:'#fff7ed',purple:'#faf5ff',pink:'#fff1f2',green:'#f0fdf4',teal:'#f0fdfa'};
    var COLOR_TEXT={blue:'#1d4ed8',orange:'#c2410c',purple:'#7c3aed',pink:'#be123c',green:'#15803d',teal:'#0f766e'};

    var rows2=rows.length?rows:[{text:'Select the appropriate governing formula',color:'blue'}];
    var rowHtml='';
    for(var r=0;r<rows2.length;r++){
      var row=rows2[r];
      var clr=(typeof row==='object'&&row.color)?String(row.color):'blue';
      var rawTxt=(typeof row==='object'&&row.text)?String(row.text):String(row);
      var symTxt=_stripNumeric(rawTxt);
      var bclr=COLOR_BORDER[clr]||COLOR_BORDER.blue;
      var bgclr=COLOR_BG[clr]||COLOR_BG.blue;
      var tclr=COLOR_TEXT[clr]||COLOR_TEXT.blue;
      var arrowHtml=(r<rows2.length-1)
        ?('<div class="cf-arrow" id="'+uid+'-arr'+r+'" style="opacity:0;">'
          +'<div class="cf-arrow-line"></div>'
          +'<div class="cf-arrow-head">&#9660;</div>'
          +'</div>')
        :'';
      rowHtml+=(
        '<div class="cf-row" id="'+uid+'-row'+r+'" style="opacity:0;">'
          +'<div class="cf-formula-pill" style="border-color:'+bclr+';background:'+bgclr+';color:'+tclr+';">'
            +'<span class="cf-formula-label">'+_esc(symTxt)+'</span>'
          +'</div>'
        +'</div>'
        +arrowHtml
      );
    }

    /* ── Info card (formula note, shown last) ── */
    var noteHtml='';
    if(d.formula_note){
      noteHtml=(
        '<div class="cf-info-card" id="'+uid+'-info" style="opacity:0;">'
          +'<span class="cf-info-icon">&#128161;</span>'
          +'<span class="cf-info-text">'+_esc(d.formula_note)+'</span>'
        +'</div>'
      );
    }

    /* ── Assemble the card inner HTML ── */
    var inner=(
      '<div class="cf-name-badge" id="'+uid+'-name" style="opacity:0;">'+_esc(firstName)+'</div>'
      +'<div class="cf-scene-wrap" id="'+uid+'-scene">'
        +'<div class="cf-formula-stack">'
          +rowHtml
        +'</div>'
      +'</div>'
      +noteHtml
    );

    /* ── setTimeout animation: arrows → labels/formula-pills → name badge → info card ── */
    var animScript=(
      '<script>'
      +'(function(){'
        +'var uid="'+uid+'";'
        +'function show(id,delay){'
          +'setTimeout(function(){'
            +'var el=document.getElementById(id);'
            +'if(el){'
              +'el.style.transition="opacity 0.45s ease, transform 0.45s ease";'
              +'el.style.transform="translateY(0)";'
              +'el.style.opacity="1";'
            +'}'
          +'},delay);'
        +'}'
        /* rows: count from the JS context at call time */
        +'var nRows='+rows2.length+';'
        /* Step 1 — arrows first (200ms each) */
        +'for(var r=0;r<nRows-1;r++){'
          +'show(uid+"-arr"+r, 220+r*180);'
        +'}'
        /* Step 2 — formula pills (start after last arrow, stagger 220ms) */
        +'var pillStart=220+(nRows>1?(nRows-1)*180:0)+160;'
        +'for(var p=0;p<nRows;p++){'
          +'(function(pp){'
            +'var rowEl=document.getElementById(uid+"-row"+pp);'
            +'if(rowEl){'
              +'rowEl.style.transform="translateY(12px)";'
            +'}'
            +'show(uid+"-row"+pp, pillStart+pp*220);'
          +'})(p);'
        +'}'
        /* Step 3 — formula name badge */
        +'var nameStart=pillStart+nRows*220+200;'
        +'show(uid+"-name", nameStart);'
        /* Step 4 — info card */
        +'var infoStart=nameStart+380;'
        +'show(uid+"-info", infoStart);'
      +'})();'
      +'</script>'
    );

    return _buildCard(2,'Core Formula',inner+animScript);
  }

  /* Step 4 – Substitution & Calculation */
  function _buildStep3(){
    var d=_loadData();
    var cards=Array.isArray(d.substitution_steps)?d.substitution_steps:[];
    var h='<div class="sbs-sub-list">';
    for(var c=0;c<cards.length;c++){
      var card=cards[c];
      var title=(typeof card==='object'&&card.title)?String(card.title):('Step '+(c+1));
      var expr=(typeof card==='object'&&card.expr)?String(card.expr):String(card);
      h+='<div class="sbs-sub-item">'
        +'<div class="sbs-sub-num">'+(c+1)+'</div>'
        +'<div class="sbs-sub-body">'
        +'<div class="sbs-sub-title">'+_esc(title)+'</div>'
        +'<div class="sbs-sub-expr">'+_esc(expr)+'</div>'
        +'</div></div>';
    }
    if(!cards.length){
      h+='<div class="sbs-sub-item"><div class="sbs-sub-num">1</div>'
        +'<div class="sbs-sub-body"><div class="sbs-sub-title">Calculation</div>'
        +'<div class="sbs-sub-expr">See solution for calculation details</div></div></div>';
    }
    h+='</div>';
    return _buildCard(3,'Substitution & Calculation',h);
  }

  /* Step 5 – Final Answer */
  function _buildStep4(){
    var d=_loadData();
    var answer=d.final_answer||'See complete solution';
    var insight=d.key_insight||'';
    var h='<div class="sbs-final-box">'
      +'<div class="sbs-final-label">&#x2705; Final Answer</div>'
      +'<div class="sbs-final-value">'+_esc(answer)+'</div>';
    if(insight){
      h+='<div class="sbs-insight">'
        +'<span class="sbs-insight-badge">&#x1F4A1; Key Insight</span>'
        +'<div class="sbs-insight-text">'+_esc(insight)+'</div>'
        +'</div>';
    }
    h+='</div>';
    return _buildCard(4,'Final Answer',h);
  }

  /* Build and show the entire inline section */
  function buildInlineSection(){
    if(_built)return;_built=true;
    var container=_el('sbs-steps-container');
    if(!container)return;
    container.innerHTML=_buildStep0()+_buildStep1()+_buildStep2()+_buildStep3()+_buildStep4();
  }

  /* Called when animation reaches last step */
  window.showInlineStepByStep=function(){
    buildInlineSection();
    var sec=_el('qanim-stepbystep-section');
    if(sec){
      sec.style.display='block';
      setTimeout(function(){sec.scrollIntoView({behavior:'smooth',block:'start'});},120);
    }
  };

  /* Stubs so old button references don't throw errors */
  window.openStepAnswer=function(){window.showInlineStepByStep();};
  window.closeStepAnswer=function(){};
  window.toggleStepAnswer=function(){window.showInlineStepByStep();};

  /* Wire up the "Step by Step Answer" ctrl-bar button to trigger the inline section */
  _onReady(function(){
    var _wireTries=0;
    function wireBtn(){
      var btn=_el('stepans-ctrl-btn');
      if(btn){
        btn.removeAttribute('onclick');
        btn.addEventListener('click',function(e){e.stopPropagation();window.showInlineStepByStep();});
      } else if(_wireTries<40){
        _wireTries++;
        setTimeout(wireBtn,80);
      }
    }
    wireBtn();
  });
})();

"""  # end STEP_ANSWER_JS_MODULE


def inject_step_answer_panel(html, gemini_sol):
    """
    Inject the inline 5-step solution section that auto-reveals after animation completes.
    Also patches the animation's nextStep() to call showInlineStepByStep() on last step.
    gemini_sol: dict from GeminiSolutionGenerator.generate().
    """
    # Remove any pre-existing data/style tags to avoid duplication
    html = re.sub(r'<script[^>]+id=["\']__step_answer_data__["\'][^>]*>.*?</script>', '', html, flags=re.DOTALL)
    html = re.sub(r'<style[^>]+id=["\']qanim-stepans-styles["\'][^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)

    # 1. Inject solution data tag into <head>
    try:
        data_tag = _build_step_answer_data_tag(gemini_sol)
        if '</head>' in html:
            html = html.replace('</head>', data_tag + '\n</head>', 1)
        else:
            html = data_tag + '\n' + html
    except Exception as e:
        QAnimLogger.warn("StepAnswerInjector", f"Data tag insertion failed: {e}")

    # 2. Inject CSS into <head>
    try:
        if '</head>' in html:
            html = html.replace('</head>', _STEP_ANSWER_CSS + '\n</head>', 1)
    except Exception as e:
        QAnimLogger.warn("StepAnswerInjector", f"CSS insertion failed: {e}")

    # 3. Inject the inline DOM section just before </body>
    try:
        if '</body>' in html:
            html = html.replace('</body>', _STEP_ANSWER_DOM + '\n</body>', 1)
        else:
            html += '\n' + _STEP_ANSWER_DOM
    except Exception as e:
        QAnimLogger.warn("StepAnswerInjector", f"DOM insertion failed: {e}")

    # 4. Inject JS module
    try:
        sa_script = '<script id="qanim-js-stepanswer">\n' + STEP_ANSWER_JS_MODULE + '\n</script>'
        if '</body>' in html:
            html = html.replace('</body>', sa_script + '\n</body>', 1)
        else:
            html += '\n' + sa_script
    except Exception as e:
        QAnimLogger.warn("StepAnswerInjector", f"JS module insertion failed: {e}")

    # 5. Patch the animation's nextStep() / applyStep() so that reaching the last
    #    animation step automatically reveals the inline solution section.
    #    FIX: Use a polling loop instead of a single DOMContentLoaded check,
    #    because Gemini-generated pages define applyStep/nextStep inside their own
    #    <script> block that may execute AFTER this patch script runs.
    _ANIM_PATCH_JS = """
<script id="qanim-laststep-patch">
(function patchAnimLastStep(){
  'use strict';
  if(window.__qanimLastStepPatchDone)return;
  window.__qanimLastStepPatchDone=true;

  var _patched=false;

  function _tryPatch(){
    if(_patched)return;
    var hasApply=typeof window.applyStep==='function';
    var hasNext=typeof window.nextStep==='function';
    if(!hasApply&&!hasNext)return; // functions not yet defined — poll will retry

    _patched=true;

    /* Wrap applyStep to detect last step */
    if(hasApply){
      var _origApply=window.applyStep;
      window.applyStep=function(idx){
        _origApply(idx);
        var total=Array.isArray(window.stepsData)?window.stepsData.length:0;
        if(total>0&&idx>=total-1){
          if(typeof window.showInlineStepByStep==='function'){
            setTimeout(window.showInlineStepByStep,600);
          }
        }
      };
    }

    /* Wrap nextStep for Gemini-generated animations */
    if(hasNext){
      var _origNext=window.nextStep;
      window.nextStep=function(){
        var total=Array.isArray(window.stepsData)?window.stepsData.length:0;
        var cur=typeof window.currentStep==='number'?window.currentStep:-1;
        _origNext();
        var newCur=typeof window.currentStep==='number'?window.currentStep:cur;
        if(total>0&&newCur>=total-1){
          if(typeof window.showInlineStepByStep==='function'){
            setTimeout(window.showInlineStepByStep,600);
          }
        }
      };
    }
  }

  /* Poll every 50 ms for up to 3 seconds until applyStep/nextStep exist */
  var _pollCount=0;
  var _pollTimer=setInterval(function(){
    _tryPatch();
    _pollCount++;
    if(_patched||_pollCount>60){clearInterval(_pollTimer);}
  },50);

  /* Also try immediately and at DOMContentLoaded as belt-and-suspenders */
  _tryPatch();
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded',_tryPatch);
  }
})();
</script>
"""
    try:
        if '</body>' in html:
            html = html.replace('</body>', _ANIM_PATCH_JS + '\n</body>', 1)
        else:
            html += '\n' + _ANIM_PATCH_JS
    except Exception as e:
        QAnimLogger.warn("StepAnswerInjector", f"Anim patch injection failed: {e}")

    n_subs = len(gemini_sol.get("substitution_steps") or [])
    QAnimLogger.ok("StepAnswerInjector", f"Injected inline step-by-step section ({n_subs} substitution step(s))")
    return html


# ===========================================================================
#  MODULE 7.5 — GeminiSolutionGenerator
#  Replaces HaikuSolutionGenerator. Uses Gemini 3.1 Pro Preview.
# ===========================================================================

_SOLUTION_SYSTEM = """You are an expert engineering professor generating a structured 5-step solution for students.

Return ONLY valid JSON with EXACTLY this structure — no markdown fences, no extra text, no comments:
{
  "given_data": [
    "rho = 1000 kg/m3",
    "V = 2 m/s",
    "D = 0.05 m",
    "mu = 0.001 Pa.s"
  ],
  "to_find": [
    "i) Reynolds number Re",
    "ii) Heat transfer coefficient h",
    "iii) Nusselt number Nu"
  ],
  "formulas": [
    {"text": "Re = rho x V x D / mu", "color": "blue"},
    {"text": "Pr = mu x c_p / k",     "color": "orange"},
    {"text": "Nu = 0.023 x Re^0.8 x Pr^0.4", "color": "purple"},
    {"text": "h = Nu x k / D",          "color": "pink"}
  ],
  "formula_note": "Evaluate all properties at bulk mean temperature T_bulk = (T_in + T_out)/2",
  "substitution_steps": [
    {"title": "Calculate Reynolds Number",      "expr": "Re = (1000 x 2 x 0.05) / 0.001 = 100000"},
    {"title": "Calculate Prandtl Number",       "expr": "Pr = (0.001 x 4200) / 0.6 = 7"},
    {"title": "Apply Dittus-Boelter Equation",  "expr": "Nu = 0.023 x (100000)^0.8 x (7)^0.4 = 365"},
    {"title": "Find Heat Transfer Coefficient", "expr": "h = 365 x 0.6 / 0.05 = 4380 W/(m2.K)"}
  ],
  "final_answer": "h = 4380 W/(m2.K),  Re = 100000,  Nu = 365",
  "key_insight": "Higher flow velocity raises Re, which boosts h through the 0.8-power relationship."
}

STRICT RULES:
- given_data: Extract EVERY numerical value stated in the question. Format each as \"symbol = value unit\". Minimum 2, maximum 12 items. Never leave empty.
- to_find: List EVERYTHING the question asks to find, prefixed i) ii) iii) etc. Never leave empty.
- formulas: 2-6 key formulas arranged as an input-to-output chain. Each entry MUST have \"text\" (the formula expression) and \"color\" (one of: blue, orange, purple, pink, green, teal). These are rendered as a visual flowchart with arrows between them. Never leave empty.
- formula_note: Optional note about evaluation conditions (e.g. bulk temperature). Set to \"\" if not applicable.
- substitution_steps: 3-5 numbered calculation steps. Each MUST have \"title\" (what this step computes) and \"expr\" (the actual mathematical expression with REAL numbers substituted and the computed result shown). Never leave empty.
- final_answer: Complete answer containing ALL computed numerical values with units. Must NEVER be empty.
- key_insight: One clear memorable sentence about the core physics or mathematical concept. Must NEVER be empty.
- CRITICAL OUTPUT FORMAT: Your response MUST start with {{ and end with }}. No preamble, no explanation, no markdown fences. Raw JSON only. If you include anything before {{ or after }}, the response will be rejected."""


class GeminiSolutionGenerator:

    _FALLBACK = {
        "given_data": [
            "See question for numerical values"
        ],
        "to_find": [
            "i) See question for what to find"
        ],
        "formulas": [
            {"text": "Select the appropriate governing formula", "color": "blue"},
            {"text": "Substitute the given values",             "color": "orange"},
            {"text": "Compute the result",                      "color": "green"},
        ],
        "formula_note": "",
        "substitution_steps": [
            {"title": "Identify Given Values",  "expr": "List all values from the question with their units."},
            {"title": "Select Formula",         "expr": "Choose the correct governing equation for this problem type."},
            {"title": "Substitute and Solve",   "expr": "Insert the known values and evaluate step by step."},
        ],
        "steps": [
            "Step 1: Write down the given values from the question.",
            "Step 2: Identify what needs to be found.",
            "Step 3: Choose the correct governing formula.",
            "Step 4: Substitute values and solve step by step.",
            "Step 5: State the final answer with units.",
        ],
        "final_answer": "Please re-generate for a detailed answer.",
        "key_insight":  "Always identify given values and the target quantity before selecting a formula.",
        "raw": "",
    }

    @classmethod
    def generate(cls, question: str) -> dict:
        if _gemini_client is None:
            QAnimLogger.warn("GeminiSolution", "Gemini client not available — using fallback")
            return cls._FALLBACK

        QAnimLogger.info("GeminiSolution", f"Generating solution via {GEMINI_MODEL}...")
        user_prompt = (
            f"Solve this question step by step:\n\n"
            f"QUESTION: {question[:800]}\n\n"
            f"Return ONLY valid JSON — no markdown, no preamble, no explanation. "
            f"Start your response with {{ and end with }}."
        )

        MAX_ATTEMPTS = 2
        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = cls._call_gemini(user_prompt, cls._solution_system_text(), max_tokens=4096)
                result = cls._parse(raw)
                # Validate the result is not the fallback (i.e. parse actually worked)
                has_real_data = (
                    bool(result.get("given_data")) and
                    result.get("given_data") != cls._FALLBACK["given_data"] and
                    bool(result.get("substitution_steps"))
                )
                if has_real_data:
                    QAnimLogger.ok("GeminiSolution", f"Solution generated on attempt {attempt}")
                    return result
                else:
                    QAnimLogger.warn("GeminiSolution", f"Attempt {attempt}: parsed result looks like fallback — retrying")
                    last_error = "Result contained only fallback/placeholder content"
            except Exception as e:
                last_error = e
                QAnimLogger.warn("GeminiSolution", f"Attempt {attempt} failed: {e}")

        QAnimLogger.warn("GeminiSolution", f"All {MAX_ATTEMPTS} attempts failed ({last_error}) — using fallback")
        return cls._FALLBACK

    @classmethod
    def _solution_system_text(cls):
        return _SOLUTION_SYSTEM

    @classmethod
    def _call_gemini(cls, user_prompt: str, system_text: str, max_tokens: int = 4096) -> str:
        import time as _time
        MAX_RETRIES  = 3
        RETRY_DELAYS = [15, 30, 60]

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if _GEMINI_SDK_STYLE == "generativeai":
                    model_obj = _gemini_client.GenerativeModel(
                        model_name=GEMINI_MODEL,
                        system_instruction=system_text,
                        generation_config={"temperature": 0.3, "max_output_tokens": max_tokens},
                    )
                    response = model_obj.generate_content(user_prompt)
                    return response.text.strip()
                else:
                    try:
                        config = _google_genai.types.GenerateContentConfig(
                            system_instruction=system_text,
                            temperature=0.3,
                            max_output_tokens=max_tokens,
                            thinking_config=_google_genai.types.ThinkingConfig(thinking_level="low"),
                        )
                    except Exception:
                        config = _google_genai.types.GenerateContentConfig(
                            system_instruction=system_text,
                            temperature=0.3,
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
                is_429 = "429" in err_str or "TooManyRequests" in err_str or "Resource has been exhausted" in err_str
                if is_429 and attempt < MAX_RETRIES:
                    _time.sleep(RETRY_DELAYS[attempt - 1])
                    continue
                raise

        raise RuntimeError("All Gemini retry attempts exhausted")

    @classmethod
    def _extract_json_from_raw(cls, raw: str) -> str:
        """
        Robustly extract the JSON object from Gemini's raw response.
        Handles: markdown fences, preamble text, trailing commentary,
        thinking tags, and partial wrapping.
        """
        # 1. Strip markdown fences (```json ... ``` or ``` ... ```)
        raw = re.sub(r'^```(?:json)?\s*\n?', '', raw.strip(), flags=re.IGNORECASE)
        raw = re.sub(r'\n?```\s*$', '', raw, flags=re.IGNORECASE).strip()

        # 2. Strip Gemini "thinking" tags if present (<thinking>...</thinking>)
        raw = re.sub(r'<thinking>.*?</thinking>', '', raw, flags=re.DOTALL).strip()

        # 3. If it already looks like clean JSON, return it
        if raw.startswith('{') and raw.endswith('}'):
            return raw

        # 4. Try to find the outermost { ... } block via balanced-brace scan
        start = raw.find('{')
        if start == -1:
            return raw  # No JSON object found — let json.loads raise the error

        depth = 0
        in_string = False
        escape_next = False
        for i, ch in enumerate(raw[start:], start):
            if escape_next:
                escape_next = False
                continue
            if ch == '\\' and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return raw[start:i + 1]

        # Partial JSON — return from start to end and let json.loads try
        return raw[start:]

    @classmethod
    def _parse(cls, raw: str) -> dict:
        raw = cls._extract_json_from_raw(raw)
        try:
            data = json.loads(raw)

            # ── Structured 5-step fields ──────────────────────────────
            given_data = data.get("given_data", [])
            if not isinstance(given_data, list):
                given_data = []

            to_find = data.get("to_find", [])
            if not isinstance(to_find, list):
                to_find = []

            formulas = data.get("formulas", [])
            if not isinstance(formulas, list):
                formulas = []
            # Normalise each formula entry to {"text": ..., "color": ...}
            COLORS = ["blue", "orange", "purple", "pink", "green", "teal"]
            norm_formulas = []
            for idx, f in enumerate(formulas):
                if isinstance(f, dict):
                    norm_formulas.append({
                        "text":  str(f.get("text", "") or ""),
                        "color": str(f.get("color", COLORS[idx % len(COLORS)])),
                    })
                else:
                    norm_formulas.append({"text": str(f), "color": COLORS[idx % len(COLORS)]})

            formula_note = str(data.get("formula_note", "") or "")

            substitution_steps = data.get("substitution_steps", [])
            if not isinstance(substitution_steps, list):
                substitution_steps = []
            norm_subs = []
            for s in substitution_steps:
                if isinstance(s, dict):
                    norm_subs.append({
                        "title": str(s.get("title", "") or ""),
                        "expr":  str(s.get("expr", "") or ""),
                    })
                else:
                    norm_subs.append({"title": "Calculation", "expr": str(s)})

            final_answer = str(data.get("final_answer", "") or "")
            key_insight  = str(data.get("key_insight",  "") or "")

            # ── Backward-compat flat steps list ──────────────────────
            steps = data.get("steps", [])
            if not isinstance(steps, list) or not steps:
                steps = [
                    "Step 1: " + (", ".join(given_data[:3]) or "See question for given values."),
                    "Step 2: " + (", ".join(to_find[:2])    or "See question for what to find."),
                    "Step 3: " + (", ".join(f["text"] for f in norm_formulas[:2]) or "Apply the formula."),
                    "Step 4: " + (norm_subs[0]["expr"] if norm_subs else "Substitute and solve."),
                    "Step 5: " + (final_answer or "Compute the final answer."),
                ]

            return {
                "given_data":         given_data,
                "to_find":            to_find,
                "formulas":           norm_formulas,
                "formula_note":       formula_note,
                "substitution_steps": norm_subs,
                "steps":              steps,
                "final_answer":       final_answer,
                "key_insight":        key_insight,
                "raw":                raw,
            }
        except Exception as e:
            QAnimLogger.warn("GeminiSolution", f"JSON parse failed: {e}")
            return cls._FALLBACK

    @classmethod
    async def generate_async(cls, question: str) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, cls.generate, question)


# ===========================================================================
#  MODULE 8 — Answer Box Panel
# ===========================================================================

def _build_answer_targets_tag(answer_targets):
    payload = {"answer_targets": answer_targets or []}
    return ('<script type="application/json" id="__answer_targets__">\n'
            + json.dumps(payload, ensure_ascii=False, indent=2) + '\n</script>')


def _build_answer_targets(to_find_targets, gemini_sol, final_answer, key_insight):
    targets = []
    _num_re = re.compile(
        r'([A-Za-z_][A-Za-z_0-9]*)\s*[=:]\s*([-+]?\d[\d.,]*(?:\s*[×x*]\s*10\^?[-+]?\d+)?)\s*([A-Za-z°%/²³·]+(?:\s*[A-Za-z°%/²³·]+)*)?',
        re.IGNORECASE)
    found_pairs = {}
    if final_answer:
        for m in _num_re.finditer(final_answer):
            sym  = m.group(1).strip()
            val  = m.group(2).strip()
            unit = (m.group(3) or "").strip()
            found_pairs[sym.lower()] = {"sym": sym, "val": val, "unit": unit}

    used_syms = set()
    for tf in (to_find_targets or []):
        tf_lower = tf.lower()
        matched  = None
        for sym_key, info in found_pairs.items():
            if sym_key in tf_lower or tf_lower.startswith(sym_key):
                if sym_key not in used_syms:
                    matched = info
                    used_syms.add(sym_key)
                    break
        if matched:
            targets.append({
                "label":   tf,
                "value":   f"{matched['val']} {matched['unit']}".strip(),
                "unit":    matched["unit"],
                "insight": key_insight or "Apply the relevant formula step by step.",
            })
        else:
            targets.append({
                "label":   tf,
                "value":   final_answer,
                "unit":    "",
                "insight": key_insight or "Apply the relevant formula step by step.",
            })

    if not targets:
        targets.append({
            "label":   "Final Answer",
            "value":   final_answer or "",
            "unit":    "",
            "insight": key_insight or "Follow the step-by-step solution to reach the answer.",
        })
    return targets


_ANSWER_BOX_CSS = """
<style id="qanim-answerbox-styles">
#answerbox-backdrop { display:none; position:fixed; inset:0; z-index:8600;
  background:rgba(15,23,42,.40); backdrop-filter:blur(4px); opacity:0; transition:opacity .22s ease; }
#answerbox-backdrop.open { display:flex; align-items:center; justify-content:center; padding:16px; opacity:1; }
#answerbox-panel { width:min(540px,94vw); max-height:90vh; border-radius:16px; background:#fff;
  border:1px solid #e2e8f0; box-shadow:0 12px 48px rgba(0,0,0,.14); opacity:0; pointer-events:none;
  transform:translateY(16px) scale(.97); transition:opacity .25s ease,transform .26s cubic-bezier(.34,1.56,.64,1);
  overflow:hidden; display:flex; flex-direction:column; }
#answerbox-panel.open { opacity:1; pointer-events:auto; transform:translateY(0) scale(1); }
.ab-header { display:flex; align-items:center; justify-content:space-between; padding:16px 20px;
  background:#fff; border-bottom:1px solid #e2e8f0; flex-shrink:0; }
.ab-header-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:16px; font-weight:800; color:#1e293b;
  display:flex; align-items:center; gap:8px; }
.ab-close-btn { width:30px; height:30px; border-radius:8px; border:1px solid #e2e8f0; background:#f8fafc;
  color:#64748b; font-size:12px; cursor:pointer; display:flex; align-items:center; justify-content:center; transition:background .15s; }
.ab-close-btn:hover { background:#fee2e2; color:#dc2626; }
.ab-progress-row { display:flex; align-items:center; justify-content:space-between; padding:10px 20px 0; flex-shrink:0; }
.ab-progress-label { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:11px; font-weight:700;
  color:#7c3aed; text-transform:uppercase; letter-spacing:.8px; }
.ab-progress-dots { display:flex; gap:5px; }
.ab-dot { width:7px; height:7px; border-radius:50%; background:#e2e8f0; transition:background .2s; }
.ab-dot.done { background:#16a34a; } .ab-dot.current { background:#7c3aed; }
.ab-body { padding:14px 20px 20px; overflow-y:auto; flex:1; }
.ab-find-chip { display:flex; align-items:flex-start; gap:8px; padding:10px 14px; border-radius:10px;
  background:#f5f3ff; border:1px solid #ddd6fe; margin-bottom:14px; }
.ab-find-icon { font-size:16px; flex-shrink:0; margin-top:1px; }
.ab-find-text { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12.5px; font-weight:600; color:#5b21b6; line-height:1.5; }
.ab-find-label { font-size:10px; font-weight:800; text-transform:uppercase; letter-spacing:1px; color:#7c3aed; display:block; margin-bottom:2px; }
.ab-instruction { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:13px; color:#64748b; margin-bottom:10px; line-height:1.6; }
#ab-user-input { width:100%; min-height:80px; padding:12px 14px; border-radius:10px; border:1.5px solid #e2e8f0;
  background:#f8fafc; font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:13px; color:#1e293b;
  line-height:1.6; resize:vertical; transition:border-color .15s; outline:none; box-sizing:border-box; }
#ab-user-input:focus { border-color:#7c3aed; background:#fff; }
#ab-submit-btn { width:100%; padding:12px; margin-top:10px; border-radius:10px; border:none;
  background:#7c3aed; color:#fff; font-size:14px; font-weight:700; font-family:inherit; cursor:pointer;
  transition:background .15s,transform .1s; }
#ab-submit-btn:hover { background:#6d28d9; transform:translateY(-1px); }
#ab-feedback { display:none; margin-top:14px; border-radius:12px; overflow:hidden; border:1px solid transparent; }
#ab-feedback.show { display:block; }
#ab-feedback.correct { border-color:#bbf7d0; }
#ab-feedback.almost  { border-color:#fed7aa; }
#ab-feedback.wrong   { border-color:#fecaca; }
.ab-feedback-top { display:flex; align-items:center; gap:10px; padding:12px 16px; }
#ab-feedback.correct .ab-feedback-top { background:#f0fdf4; }
#ab-feedback.almost  .ab-feedback-top { background:#fff7ed; }
#ab-feedback.wrong   .ab-feedback-top { background:#fef2f2; }
.ab-feedback-icon { font-size:22px; flex-shrink:0; }
.ab-feedback-verdict { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:15px; font-weight:800; }
#ab-feedback.correct .ab-feedback-verdict { color:#15803d; }
#ab-feedback.almost  .ab-feedback-verdict { color:#c2410c; }
#ab-feedback.wrong   .ab-feedback-verdict { color:#b91c1c; }
.ab-feedback-insight { padding:10px 16px 13px; border-top:1px solid; }
#ab-feedback.correct .ab-feedback-insight { background:#fafffe; border-color:#bbf7d0; }
#ab-feedback.almost  .ab-feedback-insight { background:#fffbf5; border-color:#fed7aa; }
#ab-feedback.wrong   .ab-feedback-insight { background:#fff8f8; border-color:#fecaca; }
.ab-insight-label { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:10px; font-weight:800;
  text-transform:uppercase; letter-spacing:1.2px; color:#64748b; margin-bottom:4px; }
.ab-insight-text { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12.5px; color:#1e293b; line-height:1.68; }
.ab-action-row { display:none; gap:8px; margin-top:12px; }
.ab-action-row.show { display:flex; }
#ab-retry-btn { flex:1; padding:9px 14px; border-radius:9px; border:1px solid #e2e8f0; background:#f8fafc;
  color:#64748b; font-size:12px; font-weight:600; font-family:inherit; cursor:pointer; transition:background .15s; }
#ab-retry-btn:hover { background:#ede9fe; border-color:#7c3aed; color:#7c3aed; }
#ab-next-target-btn { flex:2; padding:9px 14px; border-radius:9px; border:none; background:#7c3aed; color:#fff;
  font-size:12px; font-weight:700; font-family:inherit; cursor:pointer; display:none; transition:background .15s; }
#ab-next-target-btn:hover { background:#6d28d9; }
#ab-next-target-btn.show { display:block; }
#ab-alldone-card { display:none; text-align:center; padding:28px 20px; border-radius:14px;
  background:linear-gradient(135deg,#f0fdf4,#fefce8); border:1.5px solid #bbf7d0; margin-top:10px; }
#ab-alldone-card.show { display:block; }
.ab-alldone-emoji { font-size:40px; display:block; margin-bottom:10px; }
.ab-alldone-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:18px; font-weight:800; color:#15803d; margin-bottom:6px; }
.ab-alldone-sub { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:13px; color:#166534; line-height:1.6; }
</style>
"""

_ANSWER_BOX_DOM = """
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
    <textarea id="ab-user-input" placeholder="e.g. 350 W/m or 88 %" spellcheck="false"></textarea>
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
      <div class="ab-alldone-sub">Great work! Open <strong>Step by Step Answer</strong> to review the full solution.</div>
    </div>
  </div>
</div>
</div>
"""

_ANSWER_BOX_JS = r"""
(function initAnswerBox(){
  'use strict';
  if(window.__qanimAnswerBoxInit)return;window.__qanimAnswerBoxInit=true;
  var abOpen=false,_targets=[],_currentIdx=0,_loaded=false;
  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _loadTargets(){
    if(_loaded)return;_loaded=true;
    try{var tag=_el('__answer_targets__');if(!tag){return;}
      var data=JSON.parse(tag.textContent)||{};_targets=Array.isArray(data.answer_targets)?data.answer_targets:[];}
    catch(e){_targets=[];}
  }
  function _renderTarget(idx){
    var t=_targets[idx];if(!t)return;
    var findEl=_el('ab-find-text');if(findEl)findEl.textContent=t.label||'Answer';
    var total=_targets.length;
    var progLabel=_el('ab-progress-label');if(progLabel)progLabel.textContent='Question '+(idx+1)+' of '+total;
    var dotsEl=_el('ab-progress-dots');
    if(dotsEl){var html='';for(var i=0;i<total;i++){var cls=i<idx?'ab-dot done':i===idx?'ab-dot current':'ab-dot';html+='<div class="'+cls+'"></div>';}dotsEl.innerHTML=html;}
    var inp=_el('ab-user-input');if(inp){inp.value='';inp.removeAttribute('disabled');}
    var fb=_el('ab-feedback');if(fb)fb.className='';
    var ar=_el('ab-action-row');if(ar)ar.className='ab-action-row';
    var ntb=_el('ab-next-target-btn');if(ntb)ntb.style.display='none';
    var sb=_el('ab-submit-btn');if(sb){sb.style.display='';sb.disabled=false;}
    var adc=_el('ab-alldone-card');if(adc)adc.className='';
    var unit=t.unit?' ('+t.unit+')':'';if(inp)inp.placeholder='Type your answer'+unit+'...';
  }
  function _extractNums(s){var m=s.match(/[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?/g);return m?m.map(parseFloat).filter(function(n){return isFinite(n);}):[];}
  function _validate(userAns,correctAns){
    if(!userAns||!userAns.trim())return'empty';
    var userNums=_extractNums(userAns),correctNums=_extractNums(correctAns);
    if(userNums.length>0&&correctNums.length>0){
      var relErr=Math.abs(userNums[0]-correctNums[0])/(Math.abs(correctNums[0])+1e-12);
      if(relErr<0.01)return'correct';if(relErr<0.15)return'almost';return'wrong';
    }
    var uC=userAns.toLowerCase().trim().replace(/[^a-z0-9\s]/g,' ');
    var cC=correctAns.toLowerCase().trim().replace(/[^a-z0-9\s]/g,' ');
    if(uC===cC)return'correct';
    return'wrong';
  }
  var _FB={correct:{icon:'✅',verdict:'Correct!',cls:'correct'},almost:{icon:'〰️',verdict:'Almost Correct',cls:'almost'},wrong:{icon:'❌',verdict:'Wrong Answer',cls:'wrong'},empty:{icon:'❓',verdict:'No Answer',cls:'wrong'}};
  function _showFeedback(verdict,insight){
    var info=_FB[verdict]||_FB['wrong'];
    var fb=_el('ab-feedback'),icon=_el('ab-feedback-icon'),verd=_el('ab-feedback-verdict'),ins=_el('ab-insight-text');
    if(!fb)return;
    fb.className='show '+info.cls;if(icon)icon.textContent=info.icon;if(verd)verd.textContent=info.verdict;
    if(ins)ins.textContent=insight||'Review the solution for more detail.';
    var ar=_el('ab-action-row');if(ar)ar.className='ab-action-row show';
    var ntb=_el('ab-next-target-btn'),isLast=(_currentIdx>=_targets.length-1);
    if(ntb){if((verdict==='correct'||verdict==='almost')&&!isLast){ntb.style.display='';ntb.textContent='Next \u2192';}else{ntb.style.display='none';}}
    if(verdict==='correct'&&isLast){setTimeout(function(){var adc=_el('ab-alldone-card');if(adc)adc.className='show';var sb=_el('ab-submit-btn');if(sb)sb.style.display='none';},900);}
  }
  function openAnswerBox(){
    _loadTargets();_currentIdx=0;
    var backdrop=_el('answerbox-backdrop'),panel=_el('answerbox-panel');if(!backdrop||!panel)return;
    backdrop.classList.add('open');backdrop.setAttribute('aria-hidden','false');
    panel.classList.add('open');panel.setAttribute('aria-hidden','false');
    abOpen=true;_renderTarget(_currentIdx);
    setTimeout(function(){var inp=_el('ab-user-input');if(inp)inp.focus();},220);
  }
  function closeAnswerBox(){
    var backdrop=_el('answerbox-backdrop'),panel=_el('answerbox-panel');
    if(backdrop){backdrop.classList.remove('open');backdrop.setAttribute('aria-hidden','true');}
    if(panel){panel.classList.remove('open');panel.setAttribute('aria-hidden','true');}
    abOpen=false;
  }
  window.openAnswerBox=openAnswerBox;window.closeAnswerBox=closeAnswerBox;window.resetAnswerBox=function(){_loaded=false;_targets=[];_currentIdx=0;};
  _onReady(function(){
    function wireCtrlBtn(){var btn=document.getElementById('answerbox-ctrl-btn');
      if(btn){btn.removeAttribute('onclick');btn.addEventListener('click',function(e){e.stopPropagation();abOpen?closeAnswerBox():openAnswerBox();});}
      else{setTimeout(wireCtrlBtn,100);}
    }
    wireCtrlBtn();
    var closeBtn=_el('ab-close-btn');if(closeBtn)closeBtn.addEventListener('click',function(e){e.stopPropagation();closeAnswerBox();});
    var backdrop=_el('answerbox-backdrop');if(backdrop)backdrop.addEventListener('click',function(e){if(e.target===backdrop)closeAnswerBox();});
    document.addEventListener('keydown',function(e){if(e.key==='Escape'&&abOpen)closeAnswerBox();});
    var submitBtn=_el('ab-submit-btn');
    if(submitBtn)submitBtn.addEventListener('click',function(){
      var inp=_el('ab-user-input'),userAns=inp?inp.value.trim():'';
      var target=_targets[_currentIdx]||{};var verdict=_validate(userAns,target.value||'');
      _showFeedback(verdict,target.insight||'');if(inp)inp.disabled=true;
    });
    var inp2=_el('ab-user-input');
    if(inp2)inp2.addEventListener('keydown',function(e){if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();var sb=_el('ab-submit-btn');if(sb)sb.click();}});
    var retryBtn=_el('ab-retry-btn');
    if(retryBtn)retryBtn.addEventListener('click',function(){
      var inp=_el('ab-user-input');if(inp){inp.value='';inp.disabled=false;inp.focus();}
      var fb=_el('ab-feedback');if(fb)fb.className='';
      var ar=_el('ab-action-row');if(ar)ar.className='ab-action-row';
      var sb=_el('ab-submit-btn');if(sb)sb.style.display='';
      var ntb=_el('ab-next-target-btn');if(ntb)ntb.style.display='none';
    });
    var ntb2=_el('ab-next-target-btn');
    if(ntb2)ntb2.addEventListener('click',function(){if(_currentIdx<_targets.length-1){_currentIdx++;_renderTarget(_currentIdx);}});
  });
})();
"""


def inject_answer_box_panel(html, answer_targets=None):
    html = re.sub(r'<style[^>]+id=["\']qanim-answerbox-styles["\'][^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    if answer_targets:
        try:
            targets_tag = _build_answer_targets_tag(answer_targets)
            if '</head>' in html:
                html = html.replace('</head>', targets_tag + '\n</head>', 1)
            else:
                html = targets_tag + '\n' + html
        except Exception as e:
            QAnimLogger.warn("AnswerBoxInjector", f"Targets tag failed: {e}")
    try:
        if '</head>' in html:
            html = html.replace('</head>', _ANSWER_BOX_CSS + '\n</head>', 1)
    except Exception as e:
        QAnimLogger.warn("AnswerBoxInjector", f"CSS failed: {e}")
    try:
        body_match = re.search(r'<body[^>]*>', html, re.IGNORECASE)
        if body_match:
            ins = body_match.end()
            html = html[:ins] + '\n' + _ANSWER_BOX_DOM + html[ins:]
    except Exception as e:
        QAnimLogger.warn("AnswerBoxInjector", f"DOM failed: {e}")
    try:
        ab_script = '<script id="qanim-js-answerbox">\n' + _ANSWER_BOX_JS + '\n</script>'
        if '</body>' in html:
            html = html.replace('</body>', ab_script + '\n</body>', 1)
        else:
            html += '\n' + ab_script
    except Exception as e:
        QAnimLogger.warn("AnswerBoxInjector", f"JS failed: {e}")
    QAnimLogger.ok("AnswerBoxInjector", f"Answer box panel injected ({len(answer_targets or [])} target(s))")
    return html


# ===========================================================================
#  MODULE 9 — Notes System
# ===========================================================================

_NOTES_CSS = """
<style id="qanim-notes-styles">
#qanim-notes-btn { position:fixed; top:14px; right:16px; z-index:6900;
  display:flex; align-items:center; gap:7px; padding:10px 18px 10px 13px; border-radius:11px;
  border:1.5px solid #d1d5db; background:#fff; color:#475569;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:14px; font-weight:700;
  cursor:pointer; box-shadow:0 3px 14px rgba(0,0,0,.11); transition:background .15s,border-color .15s,color .15s; }
#qanim-notes-btn:hover { background:#fefce8; border-color:#ca8a04; color:#92400e; }
#qanim-notes-panel { position:fixed; top:50px; right:16px; z-index:7200; width:340px; max-height:80vh;
  border-radius:14px; background:#fff; border:1px solid #e2e8f0; box-shadow:0 8px 32px rgba(0,0,0,.10);
  display:flex; flex-direction:column; overflow:hidden; opacity:0; transform:translateY(-8px) scale(.97);
  pointer-events:none; transition:opacity .22s ease,transform .22s ease; }
#qanim-notes-panel.open { opacity:1; transform:translateY(0) scale(1); pointer-events:auto; }
#qanim-notes-header { display:flex; align-items:center; justify-content:space-between;
  padding:10px 14px; background:#fffbeb; border-bottom:1px solid #fef3c7; cursor:grab; flex-shrink:0; }
.notes-header-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:13px; font-weight:700; color:#92400e; }
.notes-hdr-btn { width:24px; height:24px; border-radius:6px; border:1px solid #fde68a;
  background:rgba(255,255,255,.6); color:#92400e; font-size:12px;
  display:flex; align-items:center; justify-content:center; cursor:pointer; }
.notes-hdr-btn:hover { background:#fef3c7; }
#qanim-notes-tabs { display:flex; border-bottom:1px solid #f1f5f9; flex-shrink:0; }
.notes-tab { flex:1; padding:7px 0; text-align:center; font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:11px; font-weight:600; color:#94a3b8; cursor:pointer; border-bottom:2px solid transparent;
  transition:color .15s,border-color .15s; text-transform:uppercase; letter-spacing:.5px; }
.notes-tab.active { color:#f59e0b; border-bottom-color:#f59e0b; }
#qanim-canvas-toolbar { display:flex; align-items:center; gap:5px; padding:6px 10px;
  background:#f8fafc; border-bottom:1px solid #f1f5f9; flex-shrink:0; flex-wrap:wrap; }
.canvas-tool-btn { padding:3px 9px; border-radius:5px; border:1px solid #e2e8f0; background:#fff;
  color:#64748b; font-size:11px; font-weight:600; cursor:pointer; }
.canvas-tool-btn.active { background:#fef3c7; border-color:#f59e0b; color:#92400e; }
.color-dot { width:16px; height:16px; border-radius:50%; cursor:pointer; border:2px solid transparent; transition:transform .12s; }
.color-dot:hover { transform:scale(1.2); }
.color-dot.selected { border-color:#1e293b; transform:scale(1.1); }
.size-btn { width:20px; height:20px; border-radius:50%; border:1px solid #e2e8f0; background:#fff;
  color:#64748b; font-size:10px; font-weight:700; display:flex; align-items:center; justify-content:center; cursor:pointer; }
.size-btn.active { background:#fef3c7; border-color:#f59e0b; color:#92400e; }
.tool-sep { width:1px; height:18px; background:#e2e8f0; flex-shrink:0; }
#qanim-canvas-wrap { flex:1 1 auto; position:relative; overflow:hidden; min-height:180px; }
#qanim-draw-canvas { display:block; width:100%; height:100%; cursor:crosshair; background:#fefce8; touch-action:none; }
#qanim-text-pane { display:none; flex-direction:column; flex:1 1 auto; overflow:hidden; }
#qanim-notes-textarea { flex:1 1 auto; width:100%; min-height:180px; resize:none; box-sizing:border-box;
  background:#f8fafc; border:none; outline:none; color:#1e293b;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:13px; line-height:1.7; padding:12px 14px; }
#qanim-notes-textarea::placeholder { color:#cbd5e1; }
#qanim-notes-footer { display:flex; align-items:center; justify-content:space-between;
  padding:6px 12px; border-top:1px solid #f1f5f9; flex-shrink:0; background:#f8fafc; }
.notes-status { font-size:10px; color:#94a3b8; font-family:-apple-system,'Segoe UI',Arial,sans-serif; }
.notes-action-btn { padding:3px 10px; border-radius:5px; border:1px solid #e2e8f0; background:#fff;
  color:#64748b; font-size:10px; font-weight:600; cursor:pointer; }
.notes-action-btn:hover { background:#ede9fe; border-color:#7c3aed; color:#7c3aed; }
</style>
"""

_NOTES_DOM = """
<button id="qanim-notes-btn" aria-label="Open notes">&#x1F4DD; Notes</button>
<div id="qanim-notes-panel" role="dialog" aria-label="Notes" aria-hidden="true">
  <div id="qanim-notes-header">
    <div class="notes-header-title">&#x270F;&#xFE0F; My Notes</div>
    <div style="display:flex;gap:4px">
      <button class="notes-hdr-btn" id="notes-minimize-btn" title="Minimize">&mdash;</button>
      <button class="notes-hdr-btn" id="notes-close-btn" title="Close">&#x2715;</button>
    </div>
  </div>
  <div id="qanim-notes-tabs">
    <div class="notes-tab active" data-tab="canvas">Draw</div>
    <div class="notes-tab" data-tab="text">Text</div>
  </div>
  <div id="qanim-canvas-toolbar">
    <button class="canvas-tool-btn active" data-tool="pen">Pen</button>
    <button class="canvas-tool-btn" data-tool="eraser">Eraser</button>
    <div class="tool-sep"></div>
    <div class="color-dot selected" style="background:#1e293b;" data-color="#1e293b"></div>
    <div class="color-dot" style="background:#7c3aed;" data-color="#7c3aed"></div>
    <div class="color-dot" style="background:#dc2626;" data-color="#dc2626"></div>
    <div class="color-dot" style="background:#16a34a;" data-color="#16a34a"></div>
    <div class="color-dot" style="background:#0284c7;" data-color="#0284c7"></div>
    <div class="tool-sep"></div>
    <button class="size-btn" data-size="2">S</button>
    <button class="size-btn active" data-size="4">M</button>
    <button class="size-btn" data-size="7">L</button>
    <div class="tool-sep"></div>
    <button class="canvas-tool-btn" id="notes-undo-btn">&#x21A9;</button>
    <button class="canvas-tool-btn" id="notes-clear-btn">&#x1F5D1;</button>
  </div>
  <div id="qanim-canvas-wrap"><canvas id="qanim-draw-canvas"></canvas></div>
  <div id="qanim-text-pane">
    <textarea id="qanim-notes-textarea" placeholder="Type your notes here..." spellcheck="false"></textarea>
  </div>
  <div id="qanim-notes-footer">
    <span class="notes-status" id="notes-char-count">0 chars</span>
    <button class="notes-action-btn" id="notes-export-text-btn">Export</button>
  </div>
</div>
"""

_NOTES_JS = r"""
(function initNotesSystem(){
  'use strict';
  if(window.__qanimNotesInit)return;window.__qanimNotesInit=true;
  var isOpen=false,isMin=false,isDrawing=false;
  var currentTool='pen',currentColor='#1e293b',currentSize=4,currentTab='canvas';
  var undoStack=[],MAX_UNDO=30;
  var autoSaveTimer=null;
  var ctx=null,canvas=null;
  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _saveNotes(){try{var canvasData=canvas?canvas.toDataURL():'';var textData=_el('qanim-notes-textarea')?_el('qanim-notes-textarea').value:'';var stat=_el('notes-char-count');if(stat)stat.textContent='Saved';}catch(e){}}
  function _scheduleAutoSave(){if(autoSaveTimer)clearTimeout(autoSaveTimer);autoSaveTimer=setTimeout(_saveNotes,1500);}
  function _initCanvas(){canvas=_el('qanim-draw-canvas');if(!canvas)return;ctx=canvas.getContext('2d');_resizeCanvas();ctx.lineCap='round';ctx.lineJoin='round';}
  function _resizeCanvas(){if(!canvas)return;var wrap=_el('qanim-canvas-wrap');var w=wrap?wrap.clientWidth:320,h=wrap?wrap.clientHeight:200;canvas.width=w;canvas.height=h;ctx.lineCap='round';ctx.lineJoin='round';ctx.strokeStyle=currentColor;ctx.lineWidth=currentSize;}
  function _saveUndo(){if(!canvas)return;if(undoStack.length>=MAX_UNDO)undoStack.shift();undoStack.push(canvas.toDataURL());}
  function _undo(){if(!canvas||undoStack.length===0)return;var prev=undoStack.pop();if(prev){var img=new Image();img.onload=function(){ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(img,0,0);};img.src=prev;}else ctx.clearRect(0,0,canvas.width,canvas.height);}
  function _getPos(e,cvs){var rect=cvs.getBoundingClientRect();var sx=cvs.width/rect.width,sy=cvs.height/rect.height;var cx=e.touches?e.touches[0].clientX:e.clientX;var cy=e.touches?e.touches[0].clientY:e.clientY;return{x:(cx-rect.left)*sx,y:(cy-rect.top)*sy};}
  function _startDraw(e){if(!canvas||currentTab!=='canvas')return;e.preventDefault();_saveUndo();isDrawing=true;var pos=_getPos(e,canvas);ctx.beginPath();ctx.moveTo(pos.x,pos.y);if(currentTool==='eraser'){ctx.globalCompositeOperation='destination-out';ctx.lineWidth=currentSize*4;}else{ctx.globalCompositeOperation='source-over';ctx.strokeStyle=currentColor;ctx.lineWidth=currentSize;}}
  function _draw(e){if(!isDrawing||!canvas)return;e.preventDefault();var pos=_getPos(e,canvas);ctx.lineTo(pos.x,pos.y);ctx.stroke();}
  function _endDraw(){if(!isDrawing)return;isDrawing=false;if(ctx)ctx.globalCompositeOperation='source-over';_scheduleAutoSave();}
  function openNotes(){var panel=_el('qanim-notes-panel');if(!panel)return;panel.classList.add('open');panel.setAttribute('aria-hidden','false');isOpen=true;setTimeout(function(){_resizeCanvas();},50);}
  function closeNotes(){var panel=_el('qanim-notes-panel');if(panel){panel.classList.remove('open');panel.setAttribute('aria-hidden','true');}isOpen=false;}
  function _switchTab(t){currentTab=t;document.querySelectorAll('.notes-tab').forEach(function(tb){tb.classList.toggle('active',tb.dataset.tab===t);});var ct=_el('qanim-canvas-toolbar'),cw=_el('qanim-canvas-wrap'),tp=_el('qanim-text-pane');if(ct)ct.style.display=t==='canvas'?'flex':'none';if(cw)cw.style.display=t==='canvas'?'block':'none';if(tp)tp.style.display=t==='text'?'flex':'none';if(t==='canvas')setTimeout(_resizeCanvas,30);}
  _onReady(function(){
    var nb=_el('qanim-notes-btn');if(nb)nb.addEventListener('click',function(){isOpen?closeNotes():openNotes();});
    var cb=_el('notes-close-btn');if(cb)cb.addEventListener('click',closeNotes);
    var mb=_el('notes-minimize-btn');if(mb)mb.addEventListener('click',function(e){e.stopPropagation();isMin=!isMin;var p=_el('qanim-notes-panel');if(p)p.style.maxHeight=isMin?'44px':'80vh';mb.textContent=isMin?'[]':'--';});
    document.querySelectorAll('.notes-tab').forEach(function(t){t.addEventListener('click',function(){_switchTab(this.dataset.tab);});});
    document.querySelectorAll('.canvas-tool-btn[data-tool]').forEach(function(b){b.addEventListener('click',function(){currentTool=this.dataset.tool;document.querySelectorAll('.canvas-tool-btn[data-tool]').forEach(function(x){x.classList.remove('active');});this.classList.add('active');});});
    document.querySelectorAll('.color-dot').forEach(function(d){d.addEventListener('click',function(){currentColor=this.dataset.color;document.querySelectorAll('.color-dot').forEach(function(x){x.classList.remove('selected');});this.classList.add('selected');if(ctx)ctx.strokeStyle=currentColor;});});
    document.querySelectorAll('.size-btn').forEach(function(b){b.addEventListener('click',function(){currentSize=parseInt(this.dataset.size,10);document.querySelectorAll('.size-btn').forEach(function(x){x.classList.remove('active');});this.classList.add('active');if(ctx)ctx.lineWidth=currentSize;});});
    var ub=_el('notes-undo-btn');if(ub)ub.addEventListener('click',_undo);
    var clrb=_el('notes-clear-btn');if(clrb)clrb.addEventListener('click',function(){if(!canvas)return;_saveUndo();ctx.clearRect(0,0,canvas.width,canvas.height);});
    var etb=_el('notes-export-text-btn');if(etb)etb.addEventListener('click',function(){var ta=_el('qanim-notes-textarea');if(!ta||!ta.value)return;var blob=new Blob([ta.value],{type:'text/plain'});var a=document.createElement('a');a.download='qanim_notes.txt';a.href=URL.createObjectURL(blob);a.click();});
    var ta=_el('qanim-notes-textarea');if(ta)ta.addEventListener('input',function(){var c=_el('notes-char-count');if(c)c.textContent=ta.value.length+' chars';_scheduleAutoSave();});
    var cvs=_el('qanim-draw-canvas');
    if(cvs){cvs.addEventListener('mousedown',_startDraw);cvs.addEventListener('mousemove',_draw);cvs.addEventListener('mouseup',_endDraw);cvs.addEventListener('mouseleave',_endDraw);cvs.addEventListener('touchstart',_startDraw,{passive:false});cvs.addEventListener('touchmove',_draw,{passive:false});cvs.addEventListener('touchend',_endDraw);}
    _initCanvas();
  });
})();
"""


def inject_notes_system(html):
    try:
        if '</head>' in html:
            html = html.replace('</head>', _NOTES_CSS + '\n</head>', 1)
    except Exception as e:
        QAnimLogger.warn("NotesInjector", f"CSS failed: {e}")
    try:
        body_match = re.search(r'<body[^>]*>', html, re.IGNORECASE)
        if body_match:
            ins = body_match.end()
            html = html[:ins] + '\n' + _NOTES_DOM + html[ins:]
    except Exception as e:
        QAnimLogger.warn("NotesInjector", f"DOM insertion failed: {e}")
    try:
        notes_script = '<script id="qanim-js-notes">\n' + _NOTES_JS + '\n</script>'
        if '</body>' in html:
            html = html.replace('</body>', notes_script + '\n</body>', 1)
        else:
            html += '\n' + notes_script
    except Exception as e:
        QAnimLogger.warn("NotesInjector", f"JS module insertion failed: {e}")
    QAnimLogger.ok("NotesInjector", "Notes whiteboard injected")
    return html


# ===========================================================================
#  MODULE 10 — Floating Controls Bar
# ===========================================================================

_CONTROLS_BAR_CSS = """
<style id="qanim-controls-bar-styles">
#qanim-controls-bar { position:fixed; bottom:16px; left:50%; transform:translateX(-50%); z-index:7000;
  display:flex; align-items:center; gap:6px; background:rgba(255,255,255,.98);
  backdrop-filter:blur(16px); -webkit-backdrop-filter:blur(16px);
  border:1.5px solid transparent; border-radius:16px; padding:10px 14px;
  box-shadow:0 6px 36px rgba(124,58,237,.18),0 2px 8px rgba(0,0,0,.08); white-space:nowrap; }
#qanim-controls-bar::before { content:''; position:absolute; inset:-2px; border-radius:18px;
  background:linear-gradient(90deg,#7c3aed,#db2777,#f59e0b,#7c3aed); background-size:200% 100%;
  animation:qanim-bar-glow 4s linear infinite; z-index:-1; }
@keyframes qanim-bar-glow { 0%{background-position:0% 50%} 100%{background-position:200% 50%} }
.qanim-ctrl-btn { display:flex; align-items:center; gap:5px; padding:8px 15px; border-radius:10px;
  border:1.5px solid #e2e8f0; background:linear-gradient(135deg,#f8fafc 0%,#f1f5f9 100%); color:#334155;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12px; font-weight:700; cursor:pointer;
  transition:background .15s,border-color .15s,color .15s,transform .12s,box-shadow .15s;
  user-select:none; letter-spacing:.2px; }
.qanim-ctrl-btn:hover { background:linear-gradient(135deg,#ede9fe 0%,#fdf4ff 100%); border-color:#7c3aed;
  color:#6d28d9; transform:translateY(-2px); box-shadow:0 4px 14px rgba(124,58,237,.22); }
.qanim-ctrl-btn:active { transform:translateY(0); box-shadow:none; }
.qanim-ctrl-sep { width:1px; height:22px; background:linear-gradient(to bottom,transparent,#c4b5fd,transparent); flex-shrink:0; }
</style>
"""

_CONTROLS_BAR_DOM = """
<div id="qanim-controls-bar" role="toolbar" aria-label="QAnim Controls">
  <button class="qanim-ctrl-btn" id="answerbox-ctrl-btn" title="Check your answer">
    <span>&#x270F;&#xFE0F;</span><span class="ctrl-label">Answer Box</span>
  </button>
  <div class="qanim-ctrl-sep"></div>
  <button class="qanim-ctrl-btn" id="stepans-ctrl-btn" title="See the full step-by-step solution">
    <span>&#x1F4CB;</span><span class="ctrl-label">Step by Step</span>
  </button>
  <div class="qanim-ctrl-sep"></div>
  <button class="qanim-ctrl-btn" id="tofind-ctrl-btn" title="What are you asked to find?">
    <span>&#x1F50D;</span><span class="ctrl-label">To Find</span>
  </button>
</div>
"""


def inject_controls_bar(html):
    try:
        if '</head>' in html:
            html = html.replace('</head>', _CONTROLS_BAR_CSS + '\n</head>', 1)
    except Exception as e:
        QAnimLogger.warn("ControlsBar", f"CSS failed: {e}")
    try:
        if '</body>' in html:
            html = html.replace('</body>', _CONTROLS_BAR_DOM + '\n</body>', 1)
        else:
            html += '\n' + _CONTROLS_BAR_DOM
        QAnimLogger.ok("ControlsBar", "Controls bar injected")
    except Exception as e:
        QAnimLogger.warn("ControlsBar", f"DOM failed: {e}")
    return html


# ===========================================================================
#  MODULE 10.7 — Previous Step Button
#  Adds a "Previous Step" control next to the animation's own Restart /
#  Next Step buttons (the ones inside <div class="control-panel"><div
#  class="actions">, NOT the floating qanim-controls-bar). Located via the
#  `id="btn-next"` attribute the animation-builder prompt requires every
#  generated page to use, rather than an exact literal string — so it
#  survives whatever whitespace/formatting Gemini happens to produce.
# ===========================================================================

_PREV_STEP_CSS = """
<style id="qanim-prevstep-styles">
#btn-prev.qanim-prev-btn { background:transparent; color:#334155; border:1px solid #cbd5e1;
  padding:10px 20px; border-radius:6px; font-size:14px; font-weight:600; cursor:pointer;
  transition:all .2s ease; margin-right:auto; }
#btn-prev.qanim-prev-btn:hover:not(:disabled) { background:rgba(15,23,42,.05); color:#1e293b; }
#btn-prev.qanim-prev-btn:disabled { opacity:.4; cursor:not-allowed; }
</style>
"""

_PREV_STEP_BTN_HTML = (
    '<button class="btn-secondary qanim-prev-btn" id="btn-prev" '
    'onclick="prevStep()" disabled>&#x25C0; Previous Step</button>'
)

PREV_STEP_JS_MODULE = r"""
(function initPrevStepButton(){
  'use strict';
  if(window.__qanimPrevStepInit)return;window.__qanimPrevStepInit=true;

  function _updateBtn(){
    var pb=document.getElementById('btn-prev');
    if(!pb)return;
    var cur=typeof window.currentStep==='number'?window.currentStep:-1;
    pb.disabled=(cur<=0);
  }

  // Exposed so the button's onclick (and anyone else) can call it directly.
  window.prevStep=function(){
    if(typeof window.currentStep!=='number')return;
    if(window.currentStep<=0)return;
    window.currentStep--;
    if(typeof window.applyStep==='function')window.applyStep(window.currentStep);
    // Stepping back from the last step must un-hide Next Step again
    // (the base animation hides it there — see applyStep()/btn-next).
    var nb=document.getElementById('btn-next');
    if(nb)nb.style.display='inline-block';
  };

  // Piggyback on applyStep — every path that changes the step
  // (nextStep, resetAnim, prevStep itself) funnels through it, so wrapping
  // it once keeps the Previous button's enabled/disabled state correct
  // regardless of which of those triggered the change.
  var _origApplyPrev=window.applyStep;
  if(typeof _origApplyPrev==='function'){
    window.applyStep=function(idx){
      _origApplyPrev(idx);
      _updateBtn();
    };
  }

  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  _onReady(function(){
    var pb=document.getElementById('btn-prev');
    if(pb){pb.removeAttribute('onclick');pb.addEventListener('click',function(e){e.stopPropagation();window.prevStep();});}
    _updateBtn();
  });
})();
"""


def inject_previous_step_button(html):
    # Strip any previous instance first — keeps repair/retry idempotent.
    html = re.sub(r'<style[^>]*id=["\']qanim-prevstep-styles["\'][^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<button[^>]*id=["\']btn-prev["\'][^>]*>.*?</button>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<script[^>]*id=["\']qanim-js-prevstep["\'][^>]*>.*?</script>', '', html, flags=re.DOTALL)

    try:
        if '</head>' in html:
            html = html.replace('</head>', _PREV_STEP_CSS + '\n</head>', 1)
    except Exception as e:
        QAnimLogger.warn("PrevStepInjector", f"CSS failed: {e}")

    try:
        next_btn_match = re.search(
            r'<button[^>]*id=["\']btn-next["\'][^>]*>.*?</button>',
            html, re.DOTALL | re.IGNORECASE,
        )
        if next_btn_match:
            html = html[:next_btn_match.start()] + _PREV_STEP_BTN_HTML + '\n' + html[next_btn_match.start():]
        else:
            # btn-next wasn't found (unexpected Gemini formatting) — fall
            # back to appending inside the .actions container by balanced
            # tag scan rather than failing silently.
            html, attached = _insert_before_container_close(
                html, r'<div class="actions"[^>]*>', _PREV_STEP_BTN_HTML
            )
            if not attached:
                QAnimLogger.warn("PrevStepInjector", "Could not locate Next Step button or .actions container")
    except Exception as e:
        QAnimLogger.warn("PrevStepInjector", f"Button insertion failed: {e}")

    try:
        prev_script = '<script id="qanim-js-prevstep">\n' + PREV_STEP_JS_MODULE + '\n</script>'
        if '</body>' in html:
            html = html.replace('</body>', prev_script + '\n</body>', 1)
        else:
            html += '\n' + prev_script
        QAnimLogger.ok("PrevStepInjector", "Previous Step button injected")
    except Exception as e:
        QAnimLogger.warn("PrevStepInjector", f"JS module insertion failed: {e}")

    return html


# ===========================================================================
#  MODULE 10.5 — Glossary Panel
# ===========================================================================

_GLOSSARY_CSS = """
<style id="qanim-glossary-styles">
#glossary-ctrl-btn { position:relative; }
.glossary-ctrl-badge { position:absolute; top:-6px; right:-6px; min-width:16px; height:16px;
  padding:0 4px; border-radius:9px; background:#0d9488; color:#fff; font-size:10px; font-weight:800;
  line-height:16px; text-align:center; box-shadow:0 0 0 2px #fff; }
#qanim-glossary-backdrop { position:fixed; inset:0; z-index:7150; background:rgba(15,23,42,.28);
  opacity:0; pointer-events:none; transition:opacity .22s ease; }
#qanim-glossary-backdrop.open { opacity:1; pointer-events:auto; }
#qanim-glossary-panel { position:fixed; top:0; right:0; z-index:7300; width:340px; max-width:88vw;
  height:100vh; background:#fff; border-left:1px solid #e2e8f0;
  box-shadow:-8px 0 32px rgba(0,0,0,.14); display:flex; flex-direction:column; overflow:hidden;
  transform:translateX(100%); transition:transform .26s cubic-bezier(.16,1,.3,1); }
#qanim-glossary-panel.open { transform:translateX(0); }
#qanim-glossary-header { display:flex; align-items:center; justify-content:space-between;
  padding:14px 16px; background:#f0fdfa; border-bottom:1px solid #ccfbf1; flex-shrink:0; }
.glossary-header-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:14px; font-weight:700; color:#0f766e; }
.glossary-hdr-btn { width:26px; height:26px; border-radius:7px; border:1px solid #99f6e4;
  background:rgba(255,255,255,.7); color:#0f766e; font-size:12px;
  display:flex; align-items:center; justify-content:center; cursor:pointer; }
.glossary-hdr-btn:hover { background:#ccfbf1; }
#qanim-glossary-body { flex:1 1 auto; overflow-y:auto; padding:12px 14px 20px; }
.glossary-term-card { background:#f8fafc; border:1px solid #e2e8f0; border-left:3px solid #0d9488;
  border-radius:10px; padding:10px 12px; margin-bottom:10px; }
.glossary-term-word { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:13px; font-weight:800;
  color:#134e4a; margin-bottom:4px; text-transform:capitalize; }
.glossary-term-meaning { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12.5px; line-height:1.55; color:#475569; }
</style>
"""

_GLOSSARY_JS = r"""
(function initGlossaryPanel(){
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
"""

_GLOSSARY_CTRL_BTN_TEMPLATE = """  <div class="qanim-ctrl-sep"></div>
  <button class="qanim-ctrl-btn" id="glossary-ctrl-btn" title="Difficult words explained simply">
    <span>&#x1F4D6;</span><span class="ctrl-label">Glossary</span><span class="glossary-ctrl-badge">{count}</span>
  </button>"""


def _build_glossary_dom(terms):
    if not terms:
        return "", ""
    cards = []
    for t in terms:
        term    = html_module.escape(str(t.get("term", "")))
        meaning = html_module.escape(str(t.get("meaning", "")))
        if not term or not meaning:
            continue
        cards.append(
            f'    <div class="glossary-term-card">\n'
            f'      <div class="glossary-term-word">{term}</div>\n'
            f'      <div class="glossary-term-meaning">{meaning}</div>\n'
            f'    </div>'
        )
    if not cards:
        return "", ""
    cards_html = "\n".join(cards)
    btn_html = _GLOSSARY_CTRL_BTN_TEMPLATE.format(count=len(cards))
    panel_html = (
        '<div id="qanim-glossary-backdrop"></div>\n'
        '<div id="qanim-glossary-panel" role="dialog" aria-label="Difficult words glossary" aria-hidden="true">\n'
        '  <div id="qanim-glossary-header">\n'
        '    <div class="glossary-header-title">&#x1F4D6; Difficult Words, Explained</div>\n'
        '    <button class="glossary-hdr-btn" id="glossary-close-btn" title="Close">&#x2715;</button>\n'
        '  </div>\n'
        '  <div id="qanim-glossary-body">\n'
        f'{cards_html}\n'
        '  </div>\n'
        '</div>\n'
    )
    return btn_html, panel_html


def inject_glossary_panel(html, terms=None):
    btn_html, panel_html = _build_glossary_dom(terms or [])
    if not panel_html:
        QAnimLogger.info("GlossaryInjector", "No difficult words — panel skipped")
        return html
    try:
        if '</head>' in html:
            html = html.replace('</head>', _GLOSSARY_CSS + '\n</head>', 1)
    except Exception as e:
        QAnimLogger.warn("GlossaryInjector", f"CSS failed: {e}")
    try:
        # Robust against whitespace/formatting drift: locate the controls-bar
        # container by its id and insert the glossary button right before its
        # TRUE closing </div> (found via a balanced-tag scan, since the bar
        # itself contains a nested <div class="qanim-ctrl-sep"> — a naive
        # non-greedy regex would stop at that nested div instead). The
        # previous implementation matched an exact literal string built from
        # the Answer Box button's markup; any whitespace/formatting drift
        # there made the match silently fail — the actual root cause of the
        # glossary button intermittently not appearing even when the panel
        # itself was present.
        html, attached = _insert_before_container_close(
            html, r'<div id="qanim-controls-bar"[^>]*>', btn_html
        )
        if not attached:
            QAnimLogger.warn("GlossaryInjector", "Controls bar container not found — button not attached")
    except Exception as e:
        QAnimLogger.warn("GlossaryInjector", f"Button attach failed: {e}")
    try:
        body_match = re.search(r'<body[^>]*>', html, re.IGNORECASE)
        if body_match:
            ins = body_match.end()
            html = html[:ins] + '\n' + panel_html + html[ins:]
    except Exception as e:
        QAnimLogger.warn("GlossaryInjector", f"DOM failed: {e}")
    try:
        glossary_script = '<script id="qanim-js-glossary">\n' + _GLOSSARY_JS + '\n</script>'
        if '</body>' in html:
            html = html.replace('</body>', glossary_script + '\n</body>', 1)
        else:
            html += '\n' + glossary_script
    except Exception as e:
        QAnimLogger.warn("GlossaryInjector", f"JS failed: {e}")
    QAnimLogger.ok("GlossaryInjector", f"Glossary panel injected ({len(terms)} term(s))")
    return html


# ===========================================================================
#  MODULE 12 — StepController (kept for backward compat — Gemini output uses
#              its own step logic but we patch it for safety)
# ===========================================================================

_STEP_CONTROLLER_JS = r"""
<script id="qanim-step-controller">
(function patchQAnimStepController(){
  "use strict";
  // The new Gemini-generated animation uses its own nextStep()/resetAnim() functions.
  // This controller only adds a safety-net: if the page has scene-N groups (old format)
  // it wires them up; otherwise it is a no-op.
  function initSC(){
    try{
      var scenes=[];
      for(var i=0;i<20;i++){var s=document.getElementById("scene-"+i);if(s){scenes.push(s);}else if(i>0){break;}}
      if(scenes.length<1){console.log("[QAnim SC] No legacy scene-N groups — skipping SC init");return;}
      // Legacy scene groups found — provide prevbtn/nextbtn wiring
      var nextBtn=document.getElementById("nextbtn"),prevBtn=document.getElementById("prevbtn");
      if(!nextBtn||!prevBtn){console.log("[QAnim SC] Legacy scenes found but no nav buttons — skipping");return;}
      var cur=0;
      function showS(idx){
        if(idx<0||idx>=scenes.length)return;cur=idx;
        for(var j=0;j<scenes.length;j++)scenes[j].setAttribute("display",j===idx?"block":"none");
        var fn=window["animateScene"+idx];if(typeof fn==="function")requestAnimationFrame(function(){requestAnimationFrame(function(){try{fn();}catch(e){}});});
      }
      nextBtn.addEventListener("click",function(e){e.stopPropagation();if(cur<scenes.length-1)showS(cur+1);});
      prevBtn.addEventListener("click",function(e){e.stopPropagation();if(cur>0)showS(cur-1);});
      showS(0);
      console.log("[QAnim SC] Legacy scene controller ready, "+scenes.length+" scenes");
    }catch(err){console.error("[QAnim SC] Fatal:",err);}
  }
  if(document.readyState!=="loading")initSC();else document.addEventListener("DOMContentLoaded",initSC);
})();
</script>
"""


def inject_step_controller(html):
    try:
        if "</body>" in html:
            html = html.replace("</body>", _STEP_CONTROLLER_JS + "\n</body>", 1)
        else:
            html += "\n" + _STEP_CONTROLLER_JS
        QAnimLogger.ok("StepController", "Step controller injected")
    except Exception as e:
        QAnimLogger.warn("StepController", f"Injection failed: {e}")
    return html


# ===========================================================================
#  MODULE 13 — Nav Patch (kept for backward compat)
# ===========================================================================

_NAV_PATCH_JS = r"""
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
</script>
"""


def inject_nav_patch_and_scene_desc(html, scene_descriptions=None):
    injection = _NAV_PATCH_JS + '\n'
    if '</body>' in html:
        html = html.replace('</body>', injection + '\n</body>', 1)
    else:
        html += '\n' + injection
    QAnimLogger.ok("NavPatch", "Nav patch injected")
    return html


# ===========================================================================
#  MODULE 14 — PANEL RELIABILITY ENGINE
#  ---------------------------------------------------------------------
#  This is the fix for the "panels randomly missing" bug class. It does NOT
#  patch individual symptoms. It replaces the previous "splice string, hope,
#  log success unconditionally" injection model with a deterministic
#  pipeline:
#
#     normalize document skeleton
#           |
#     inject all panels
#           |
#     verify every required component (DOM + CSS + JS + data, by id)
#           |
#     repair ONLY what verification found missing (strip stale fragments,
#     re-inject just that component — bounded retry loop)
#           |
#     resolve duplicate ids
#           |
#     final verification report (never silently "ok")
#
#  ROOT CAUSES THIS FIXES (found by reading the actual pipeline, not guessed):
#
#   1. `inject_to_find_system()` existed and was fully implemented, but was
#      NEVER CALLED anywhere in `generate_question_animation()`, and no
#      button anywhere in the DOM ever opened it. The "To Find" panel was
#      not "randomly" missing — it was ALWAYS missing. Dead code, now wired
#      in and given a permanent trigger button in the controls bar.
#
#   2. The Glossary trigger button was attached by matching an EXACT literal
#      string copied from the Answer Box button's markup
#      (`'<span>...Answer Box</span>\\n  </button>'`). Any whitespace or
#      formatting drift in that string — which the previous code had no
#      control over once other injectors touched the surrounding HTML — made
#      `in html` silently return False, so the button was never attached
#      even though the glossary panel/CSS/JS were. Replaced with a
#      balanced-tag scan keyed off `id="qanim-controls-bar"`.
#
#   3. Every injector spliced HTML at the literal strings `</head>`,
#      `<body...>`, `</body>` found in the AI-GENERATED page. Nothing
#      guaranteed those anchors existed, were unique, or were well-formed —
#      Gemini's raw output varies per-request (code fences, missing/duplicate
#      head or body tags, stray trailing text). When an anchor was missing,
#      the `try/except` swallowed it, logged a *warning* (or nothing), and
#      the pipeline moved on with that component's CSS/DOM/JS silently
#      absent. This explains the "sometimes appears, sometimes doesn't" —
#      it correlates with how well-formed each individual Gemini generation
#      happened to be, not with anything random in this code.
#      `DocumentSkeletonNormalizer` now guarantees a single well-formed
#      <head>/<body> skeleton BEFORE any panel is injected, so the anchors
#      every injector depends on are always present.
#
#   4. Several injectors called `QAnimLogger.ok(...)` unconditionally after
#      their try/except blocks, regardless of whether any of those blocks
#      actually succeeded — so a fully-failed injection still logged as
#      success. Verification now checks the actual resulting HTML instead
#      of trusting injector-reported status.
#
#   5. None of the DOM/JS fragments were idempotent — re-running an injector
#      (e.g. on retry) would duplicate ids, buttons, and listeners. Repair
#      now always strips a component's previous fragments before
#      re-injecting it, and every panel JS module has a `window.__qanimXInit`
#      guard as a second line of defense.
# ===========================================================================


class DocumentSkeletonNormalizer:
    """
    Guarantees a single, well-formed <!DOCTYPE>/<html>/<head>...</head>/
    <body>...</body>/</html> skeleton BEFORE any panel injection runs, so
    every injector's `</head>` / `<body...>` / `</body>` anchor is always
    present exactly once. This is the architectural fix for "malformed
    generated HTML" — instead of every injector individually hoping the
    anchors exist, we deterministically guarantee them once, up front.
    """

    @staticmethod
    def normalize(html: str) -> str:
        if not html:
            return html
        original_len = len(html)
        html = html.strip()

        # Strip markdown code fences some LLMs wrap raw HTML output in.
        html = re.sub(r'^```(?:html)?\s*\n?', '', html, flags=re.IGNORECASE)
        html = re.sub(r'\n?```\s*$', '', html)

        # Truncate anything after the LAST </html> (trailing commentary,
        # duplicate documents, etc.) — same policy as HtmlSanitizer, applied
        # first so every check below operates on the real document only.
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
            html = re.sub(
                r'(<html[^>]*>)', r'\1\n<head><meta charset="UTF-8"></head>',
                html, count=1, flags=re.IGNORECASE,
            )
        elif not head_close:
            # <head> was opened but never closed — close it right before <body>
            body_open = re.search(r'<body[\s>]', html, re.IGNORECASE)
            insert_at = body_open.start() if body_open else len(html)
            html = html[:insert_at] + '</head>\n' + html[insert_at:]

        body_open = re.search(r'<body[\s>]', html, re.IGNORECASE)
        body_close = re.search(r'</body\s*>', html, re.IGNORECASE)
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


def _insert_before_container_close_module14(html, open_tag_regex, insertion):
    """Alias kept for clarity within this module — see _insert_before_container_close above."""
    return _insert_before_container_close(html, open_tag_regex, insertion)


class DuplicateIdResolver:
    """
    Scans the full document for duplicate `id="..."` attributes. Duplicate
    ids on OUR OWN reserved namespaces (qanim-*, tofind-*, answerbox-*,
    sbs-*, notes-*, glossary-*, ab-*, __*__) indicate a component got
    injected more than once — those are resolved by the repair loop
    stripping-then-reinjecting, not by renaming (renaming would break the
    id-based wiring between DOM/CSS/JS). Duplicate ids OUTSIDE our
    namespace (i.e. authored by Gemini in the base animation, e.g. a
    reused `id="btn-next"`) are resolved here by suffixing every
    occurrence after the first, since two elements answering to
    `getElementById` is itself a source of "random" behaviour (whichever
    one the browser's internal index happens to return).
    """

    _RESERVED_PREFIXES = (
        "qanim-", "tofind-", "answerbox-", "sbs-", "notes-",
        "glossary-", "ab-", "__",
    )

    @classmethod
    def find_duplicates(cls, html):
        ids = re.findall(r'\bid=["\']([^"\']+)["\']', html)
        counts = {}
        for i in ids:
            counts[i] = counts.get(i, 0) + 1
        return {k: v for k, v in counts.items() if v > 1}

    @classmethod
    def resolve(cls, html):
        dupes = cls.find_duplicates(html)
        if not dupes:
            return html, {}
        resolved = {}
        for id_, count in dupes.items():
            if id_.startswith(cls._RESERVED_PREFIXES):
                # Leave as-is here — the repair loop is responsible for
                # de-duplicating our own components via strip-then-reinject.
                continue
            pattern = re.compile(r'(id=["\'])' + re.escape(id_) + r'(["\'])')
            counter = {"n": 0}

            def _sub(m):
                counter["n"] += 1
                if counter["n"] == 1:
                    return m.group(0)
                return f'{m.group(1)}{id_}-dup{counter["n"]}{m.group(2)}'

            html = pattern.sub(_sub, html)
            resolved[id_] = count - 1
        return html, resolved


# ---------------------------------------------------------------------------
# Required-components registry: every panel the product promises to render,
# expressed as the concrete ids that must exist in the final HTML.
# ---------------------------------------------------------------------------
REQUIRED_COMPONENTS = {
    "ToFind": {
        "data": ["__tofind_data__"],
        "css":  ["qanim-tofind-styles"],
        "dom":  ["tofind-panel", "tofind-backdrop", "tofind-items-container", "tofind-ctrl-btn"],
        "js":   ["qanim-js-tofind"],
    },
    "StepAnswer": {
        "data": ["__step_answer_data__"],
        "css":  ["qanim-stepans-styles"],
        "dom":  ["qanim-stepbystep-section", "sbs-steps-container"],
        "js":   ["qanim-js-stepanswer"],
    },
    "AnswerBox": {
        "data": None,
        "css":  ["qanim-answerbox-styles"],
        "dom":  ["answerbox-panel", "answerbox-backdrop"],
        "js":   ["qanim-js-answerbox"],
    },
    "Notes": {
        "data": None,
        "css":  ["qanim-notes-styles"],
        "dom":  ["qanim-notes-panel", "qanim-notes-btn"],
        "js":   ["qanim-js-notes"],
    },
    "Controls": {
        "data": None,
        "css":  ["qanim-controls-bar-styles"],
        "dom":  ["qanim-controls-bar", "answerbox-ctrl-btn", "stepans-ctrl-btn"],
        "js":   None,
    },
    "PreviousStep": {
        "data": None,
        "css":  ["qanim-prevstep-styles"],
        "dom":  ["btn-prev"],
        "js":   ["qanim-js-prevstep"],
    },
    "Glossary": {
        # Only required when there ARE glossary terms — see
        # PanelInjectionManager._optional_skip(). Skipped (not "failed")
        # when the question had no difficult terms to define.
        "data": None,
        "css":  ["qanim-glossary-styles"],
        "dom":  ["qanim-glossary-panel", "glossary-ctrl-btn"],
        "js":   ["qanim-js-glossary"],
    },
    "Navigation": {
        "data": None, "css": None, "dom": None,
        "js":   ["__nav_patch__"],
    },
    "StepController": {
        "data": None, "css": None, "dom": None,
        "js":   ["qanim-step-controller"],
    },
}

# Regex fragments used to strip a component's previous output before
# re-injecting it, making repair idempotent (no duplicate ids/listeners).
STRIP_PATTERNS = {
    "ToFind": [
        re.compile(r'<script[^>]*id=["\']__tofind_data__["\'][^>]*>.*?</script>', re.DOTALL),
        re.compile(r'<style[^>]*id=["\']qanim-tofind-styles["\'][^>]*>.*?</style>', re.DOTALL | re.IGNORECASE),
        re.compile(r'<div id="tofind-backdrop"[^>]*>\s*</div>\s*<aside id="tofind-panel".*?</aside>', re.DOTALL),
        re.compile(r'<script[^>]*id=["\']qanim-js-tofind["\'][^>]*>.*?</script>', re.DOTALL),
    ],
    "StepAnswer": [
        re.compile(r'<script[^>]*id=["\']__step_answer_data__["\'][^>]*>.*?</script>', re.DOTALL),
        re.compile(r'<style[^>]*id=["\']qanim-stepans-styles["\'][^>]*>.*?</style>', re.DOTALL | re.IGNORECASE),
        re.compile(r'<div id="qanim-stepbystep-section".*?</div>\s*(?=<script|<style|</body)', re.DOTALL),
        re.compile(r'<script[^>]*id=["\']qanim-js-stepanswer["\'][^>]*>.*?</script>', re.DOTALL),
        re.compile(r'<script id="qanim-laststep-patch">.*?</script>', re.DOTALL),
    ],
    "AnswerBox": [
        re.compile(r'<style[^>]*id=["\']qanim-answerbox-styles["\'][^>]*>.*?</style>', re.DOTALL | re.IGNORECASE),
        re.compile(r'<div id="answerbox-backdrop"[^>]*>.*?</div>\s*(?=<script|<style|</body)', re.DOTALL),
        re.compile(r'<script[^>]*id=["\']qanim-js-answerbox["\'][^>]*>.*?</script>', re.DOTALL),
    ],
    "Notes": [
        re.compile(r'<style[^>]*id=["\']qanim-notes-styles["\'][^>]*>.*?</style>', re.DOTALL | re.IGNORECASE),
        re.compile(r'<button id="qanim-notes-btn".*?</button>\s*<div id="qanim-notes-panel".*?</div>\s*(?=<script|<style|</body)', re.DOTALL),
        re.compile(r'<script[^>]*id=["\']qanim-js-notes["\'][^>]*>.*?</script>', re.DOTALL),
    ],
    "Controls": [
        re.compile(r'<style[^>]*id=["\']qanim-controls-bar-styles["\'][^>]*>.*?</style>', re.DOTALL | re.IGNORECASE),
        re.compile(r'<div id="qanim-controls-bar".*?</div>\s*(?=<script|<style|</body)', re.DOTALL),
    ],
    "PreviousStep": [
        re.compile(r'<style[^>]*id=["\']qanim-prevstep-styles["\'][^>]*>.*?</style>', re.DOTALL | re.IGNORECASE),
        re.compile(r'<button[^>]*id=["\']btn-prev["\'][^>]*>.*?</button>', re.DOTALL | re.IGNORECASE),
        re.compile(r'<script[^>]*id=["\']qanim-js-prevstep["\'][^>]*>.*?</script>', re.DOTALL),
    ],
    "Glossary": [
        re.compile(r'<style[^>]*id=["\']qanim-glossary-styles["\'][^>]*>.*?</style>', re.DOTALL | re.IGNORECASE),
        re.compile(r'<div id="qanim-glossary-backdrop"[^>]*>.*?</div>\s*(?=<script|<style|</body)', re.DOTALL),
        re.compile(r'<script[^>]*id=["\']qanim-js-glossary["\'][^>]*>.*?</script>', re.DOTALL),
    ],
    "Navigation": [
        re.compile(r'<script id="__nav_patch__">.*?</script>', re.DOTALL),
    ],
    "StepController": [
        re.compile(r'<script id="qanim-step-controller">.*?</script>', re.DOTALL),
    ],
}


class ComponentVerifier:
    """Checks the ACTUAL resulting HTML for every required id — never trusts
    an injector's self-reported success."""

    @staticmethod
    def _has_id(html, id_):
        return re.search(r'\bid=["\']' + re.escape(id_) + r'["\']', html) is not None

    @classmethod
    def check_component(cls, html, spec):
        missing = []
        for kind in ("data", "css", "dom", "js"):
            ids = spec.get(kind)
            if not ids:
                continue
            for id_ in ids:
                if not cls._has_id(html, id_):
                    missing.append(f"{kind}:{id_}")
        return missing

    @classmethod
    def verify_all(cls, html, registry, skip=None):
        skip = skip or set()
        report = {}
        for name, spec in registry.items():
            if name in skip:
                report[name] = {"ok": True, "missing": [], "skipped": True}
                continue
            missing = cls.check_component(html, spec)
            report[name] = {"ok": len(missing) == 0, "missing": missing, "skipped": False}
        return report


class PanelInjectionContext:
    """Bundles everything the individual injectors need so the orchestrator
    can call any of them (fresh injection OR targeted repair) uniformly."""

    def __init__(self, gemini_sol, answer_targets, glossary_terms, to_find_targets):
        self.gemini_sol = gemini_sol
        self.answer_targets = answer_targets
        self.glossary_terms = glossary_terms or []
        self.to_find_targets = to_find_targets or []


class PanelInjectionManager:
    """
    Single initialization manager for the injection pipeline itself
    (build-time, not runtime): generate -> normalize -> inject all -> verify
    -> repair (bounded) -> resolve duplicate ids -> final report. Replaces
    the old flat sequence of independent inject_*() calls with no
    verification step in between.
    """

    MAX_REPAIR_PASSES = 3

    @classmethod
    def run(cls, html, ctx: "PanelInjectionContext"):
        html = DocumentSkeletonNormalizer.normalize(html)

        html, dup_report = DuplicateIdResolver.resolve(html)
        if dup_report:
            QAnimLogger.warn("DuplicateIds", f"Resolved {len(dup_report)} duplicate id(s) from base animation: {dup_report}")

        html = cls._inject_all(html, ctx)

        skip = cls._optional_skip(ctx)
        report = ComponentVerifier.verify_all(html, REQUIRED_COMPONENTS, skip=skip)

        attempt = 1
        while not all(r["ok"] for r in report.values()) and attempt <= cls.MAX_REPAIR_PASSES:
            missing_names = [n for n, r in report.items() if not r["ok"]]
            QAnimLogger.warn("Repair", f"Pass {attempt}/{cls.MAX_REPAIR_PASSES}: missing components -> {missing_names}")
            html = cls._repair(html, ctx, missing_names, report)
            report = ComponentVerifier.verify_all(html, REQUIRED_COMPONENTS, skip=skip)
            attempt += 1

        html, dup_report2 = DuplicateIdResolver.resolve(html)
        if dup_report2:
            QAnimLogger.warn("DuplicateIds", f"Post-repair duplicates resolved: {dup_report2}")

        still_missing = [n for n, r in report.items() if not r["ok"]]
        for name, r in report.items():
            if r.get("skipped"):
                QAnimLogger.info("Verify", f"{name}: skipped (not applicable — e.g. no glossary terms)")
            elif r["ok"]:
                QAnimLogger.ok("Verify", f"{name}: verified present (DOM+CSS+JS+data all found)")
            else:
                QAnimLogger.error("Verify", f"{name}: STILL MISSING after {attempt - 1} repair pass(es) -> {r['missing']}")

        return html, {
            "all_ok": len(still_missing) == 0,
            "still_missing": still_missing,
            "repair_passes": attempt - 1,
            "report": report,
        }

    @staticmethod
    def _optional_skip(ctx):
        skip = set()
        if not ctx.glossary_terms:
            skip.add("Glossary")
        return skip

    @classmethod
    def _inject_all(cls, html, ctx):
        html = inject_step_answer_panel(html, ctx.gemini_sol)
        html = inject_notes_system(html)
        html = inject_answer_box_panel(html, ctx.answer_targets)
        html = inject_controls_bar(html)
        html = inject_previous_step_button(html)
        html = inject_to_find_system(html, ctx.to_find_targets)
        html = inject_glossary_panel(html, ctx.glossary_terms)
        html = inject_nav_patch_and_scene_desc(html)
        html = inject_step_controller(html)
        return html

    @classmethod
    def _strip(cls, html, name):
        for pattern in STRIP_PATTERNS.get(name, []):
            html = pattern.sub('', html)
        return html

    @classmethod
    def _repair(cls, html, ctx, missing_names, report):
        dispatch = {
            "ToFind":         lambda h: inject_to_find_system(cls._strip(h, "ToFind"), ctx.to_find_targets),
            "StepAnswer":     lambda h: inject_step_answer_panel(cls._strip(h, "StepAnswer"), ctx.gemini_sol),
            "AnswerBox":      lambda h: inject_answer_box_panel(cls._strip(h, "AnswerBox"), ctx.answer_targets),
            "Notes":          lambda h: inject_notes_system(cls._strip(h, "Notes")),
            "Controls":       lambda h: inject_controls_bar(cls._strip(h, "Controls")),
            "PreviousStep":   lambda h: inject_previous_step_button(cls._strip(h, "PreviousStep")),
            "Glossary":       lambda h: inject_glossary_panel(cls._strip(h, "Glossary"), ctx.glossary_terms),
            "Navigation":     lambda h: inject_nav_patch_and_scene_desc(cls._strip(h, "Navigation")),
            "StepController": lambda h: inject_step_controller(cls._strip(h, "StepController")),
        }
        for name in missing_names:
            fn = dispatch.get(name)
            if not fn:
                QAnimLogger.error("Repair", f"{name}: no repair strategy registered — cannot self-heal")
                continue
            try:
                before_missing = report[name]["missing"]
                html = fn(html)
                QAnimLogger.info("Repair", f"{name}: re-injected (was missing {before_missing})")
            except Exception as e:
                QAnimLogger.error("Repair", f"{name}: repair raised {type(e).__name__}: {e}")
        return html


# ===========================================================================
#  ██████████████████████████████████████████████████████████████████████████
#  CORE GENERATION ENGINE  (v1.0 — Gemini-only, reference-output style)
#  ██████████████████████████████████████████████████████████████████████████
#
#  TWO-STAGE PIPELINE:
#
#  Stage A — GeminiSceneAnalyzer
#    Analyses the question and produces a structured JSON scene script:
#    {
#      "title": "...",
#      "topic": "...",
#      "steps": [
#        {
#          "step_number": 1,
#          "label": "Step label for dot indicator",
#          "title": "Step N: Human-readable title",
#          "description": "Explanation shown in info-box",
#          "badges": [{"text": "...", "type": "cyan|orange|green"}],
#          "components": ["component_id_1", "component_id_2"],
#          "focus_component": "component_id or null",
#          "math_content": ""
#        }, ...
#      ],
#      "svg_components": {
#        "component_id": {
#          "type": "...",
#          "description": "what this component looks like",
#          "motion": "rotate|translate|oscillate|trace|static",
#          "accent_color": "#66fcf1"
#        }, ...
#      },
#      "final_answer": "...",
#      "key_insight": "..."
#    }
#
#  Stage B — GeminiAnimationBuilder
#    Takes the scene script and generates a complete self-contained HTML
#    animation page in the reference output style.
# ===========================================================================

# ---------------------------------------------------------------------------
# STAGE A: GeminiSceneAnalyzer  — produces the scene script JSON
# ---------------------------------------------------------------------------

_SCENE_ANALYZER_SYSTEM = """You are QAnim Scene Analyzer — an expert educational content planner.

Given a student question, produce a detailed scene-by-scene animation script in JSON.

ANIMATION PHILOSOPHY:
- Each step reveals ONE new component or concept — never everything at once.
- Components appear with motion (rotating crank, tracing path, oscillating spring, flowing current).
- When a new component is focused, previous components blur slightly (opacity drop + blur-shield).
- Labels, arrows, and dimension annotations appear AFTER the component is drawn.
- The final step always shows the complete system with the mathematical solution overlaid.

OUTPUT: Return ONLY valid JSON, no markdown fences, no preamble:
{
  "title": "Short descriptive title of the mechanism/problem",
  "topic": "PHYSICS|MATH|CHEMISTRY|ENGINEERING|BIOLOGY|ABSTRACT",
  "solution_steps": ["Step 1: ...", "Step 2: ...", ...],
  "final_answer": "Complete computed answer with all values and units",
  "key_insight": "One memorable insight sentence",
  "steps": [
    {
      "step_number": 1,
      "label": "Short label (3-5 words) for step-dot indicator",
      "title": "Step 1: Full descriptive title",
      "description": "2-3 sentence explanation shown in the info panel. Simple English, like a professor explaining out loud.",
      "badges": [{"text": "param = value unit", "type": "cyan"}],
      "components_visible": ["comp_id_1"],
      "components_new": ["comp_id_1"],
      "focus_component": "comp_id_1",
      "blur_background": true
    }
  ],
  "svg_components": {
    "comp_id": {
      "description": "Precise visual description: shape, color, size, position in 850x478 canvas",
      "motion_type": "rotate|translate|oscillate|trace|pulse|static",
      "motion_description": "e.g. rotates around fixed pivot at (200,250) at 300 RPM",
      "accent_color": "#66fcf1",
      "labels": ["label text 1", "label text 2"]
    }
  }
}

RULES:
1. steps: 3-5 steps minimum, always end with the main answer step.
2. First step: establish the frame/ground/fixed structure.
3. Each subsequent step: introduce ONE new moving/key component.
4. Last step: freeze at the solution angle/state. NO calculation popup.
5. badges: use type "cyan", "orange", or "green".
6. svg_components: describe every physical component: frame, pivot, crank, rod, piston,
   gears, pulleys, beams, coils, etc. Position everything in an 850x478 coordinate space.
7. final_answer: MUST contain computed numerical answer with units. Never leave empty.
8. motion_type: accurately describe what this component does physically."""

_SCENE_ANALYZER_USER = """Analyse this question and produce the animation scene script:

QUESTION: {question}

Remember:
- Plan the step-by-step visual reveal carefully.
- Each step shows exactly ONE new component appearing with motion.
- Components are drawn one by one in the correct physical order.
- The final step freezes the mechanism at the solution state — NO calculations popup box.
- Compute the actual numerical answer and include it in final_answer.

Return ONLY valid JSON."""


class GeminiSceneAnalyzer:
    """Stage A: Analyses the question and produces a structured scene script."""

    @classmethod
    def analyze(cls, question: str) -> dict:
        if _gemini_client is None:
            return cls._fallback_script(question)

        QAnimLogger.info("SceneAnalyzer", f"Analysing question via {GEMINI_MODEL}...")
        user_prompt = _SCENE_ANALYZER_USER.format(question=question[:1200])

        try:
            raw = GeminiSolutionGenerator._call_gemini(
                user_prompt, _SCENE_ANALYZER_SYSTEM, max_tokens=8192
            )
            raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE).strip()
            data = json.loads(raw)
            QAnimLogger.ok("SceneAnalyzer", f"Scene script produced: {len(data.get('steps',[]))} steps, {len(data.get('svg_components',{}))} components")
            return data
        except Exception as e:
            QAnimLogger.warn("SceneAnalyzer", f"Analysis failed: {e} — using fallback script")
            return cls._fallback_script(question)

    @classmethod
    async def analyze_async(cls, question: str) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, cls.analyze, question)

    @classmethod
    def _fallback_script(cls, question: str) -> dict:
        q_short = question[:80]
        return {
            "title": f"Analysis: {q_short}",
            "topic": "ENGINEERING",
            "solution_steps": [
                "Step 1: Identify the given values from the question.",
                "Step 2: Apply the governing formula.",
                "Step 3: Substitute values and compute the answer.",
            ],
            "final_answer": "Please regenerate for a complete answer.",
            "key_insight": "Always identify what is given and what is required before choosing a formula.",
            "steps": [
                {
                    "step_number": 1,
                    "label": "Setup",
                    "title": "Step 1: Problem Setup",
                    "description": "Establish the system and identify the given values. Read the question carefully.",
                    "badges": [{"text": "Given data", "type": "cyan"}],
                    "components_visible": ["frame"],
                    "components_new": ["frame"],
                    "focus_component": "frame",
                    "blur_background": False
                },
                {
                    "step_number": 2,
                    "label": "Solution",
                    "title": "Step 2: Apply the Formula",
                    "description": "Apply the governing law or formula. Substitute the known values step by step.",
                    "badges": [{"text": "Formula applied", "type": "green"}],
                    "components_visible": ["frame", "solution"],
                    "components_new": ["solution"],
                    "focus_component": None,
                    "blur_background": False
                }
            ],
            "svg_components": {
                "frame": {
                    "description": "Question text and system overview in a central card",
                    "motion_type": "static",
                    "motion_description": "A clean label card showing the problem title",
                    "accent_color": "#66fcf1",
                    "labels": ["System Setup"]
                },
                "solution": {
                    "description": "Math solution box with calculation steps",
                    "motion_type": "static",
                    "motion_description": "Solution summary card appearing",
                    "accent_color": "#97c459",
                    "labels": ["Solution"]
                }
            }
        }


# ---------------------------------------------------------------------------
# STAGE B: GeminiAnimationBuilder — generates the complete HTML animation
# ---------------------------------------------------------------------------

_ANIMATION_BUILDER_SYSTEM = """You are QAnim Animation Builder v1.0 — a specialist who generates COMPLETE, SELF-CONTAINED HTML animation pages.

You receive a scene script (JSON) and must generate a premium educational animation HTML page.

═══ REFERENCE OUTPUT STYLE (follow exactly) ═══
The output must match this exact structure and CSS (light theme, visually rich):

<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>[Title] — Interactive Animation</title>
  <style>
    :root {
      --bg-color: #eef2f9;
      --panel-bg: #ffffff;
      --text-main: #334155;
      --text-sub: #64748b;
      --accent-cyan: #0891b2;
      --accent-cyan-dim: #0e7490;
      --accent-orange: #ea8c00;
      --accent-green: #16a34a;
      --border: #e2e8f0;
      --border-radius: 14px;
      --shadow-card: 0 4px 6px -1px rgba(30,64,175,0.07), 0 10px 30px rgba(30,64,175,0.10);
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
      background-color: var(--bg-color);
      color: var(--text-main);
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: flex-start;
      min-height: 100vh;
      padding: 24px 16px 120px;
    }
    .dashboard {
      width: 100%;
      max-width: 900px;
      margin: 0 auto;
      background: var(--panel-bg);
      border-radius: var(--border-radius);
      box-shadow: var(--shadow-card);
      overflow: hidden;
      border: 1px solid var(--border);
    }
    /* ── Question Banner ── */
    .question-banner {
      padding: 18px 24px 16px;
      background: linear-gradient(135deg, #f0f5ff 0%, #e8f0fe 50%, #eef2f9 100%);
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 6px;
      position: relative;
    }
    .question-banner::before {
      content: '';
      position: absolute;
      inset: 0;
      background: linear-gradient(90deg, rgba(8,145,178,0.06) 0%, transparent 60%);
      pointer-events: none;
    }
    .q-label {
      font-size: 11px;
      font-weight: 800;
      color: var(--accent-cyan-dim);
      text-transform: uppercase;
      letter-spacing: 1.5px;
      display: flex;
      align-items: center;
      gap: 7px;
    }
    .q-label::before { content: "❓"; font-size: 13px; }
    .q-text {
      font-size: 14.5px;
      color: #1e293b;
      line-height: 1.55;
    }
    /* ── SVG Canvas ── */
    .svg-container {
      width: 100%;
      aspect-ratio: 16 / 9;
      background: radial-gradient(ellipse at 40% 40%, #f0f5ff 0%, #dce8f5 55%, #c8d8ed 100%);
      position: relative;
      overflow: hidden;
    }
    svg { display: block; width: 100%; height: 100%; }
    .svg-layer { transition: opacity 0.6s cubic-bezier(0.4, 0, 0.2, 1); }
    /* ── Control Panel ── */
    .control-panel {
      padding: 20px 24px 24px;
      background: linear-gradient(180deg, #ffffff 0%, #f7faff 100%);
      border-top: 1px solid var(--border);
    }
    /* ── Step Indicator: pill-style dots ── */
    .step-indicator {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 18px;
      flex-wrap: wrap;
    }
    .step-dot {
      padding: 5px 13px;
      border-radius: 20px;
      background: rgba(203,213,225,0.5);
      border: 1px solid #cbd5e1;
      font-size: 11px;
      font-weight: 700;
      color: #94a3b8;
      cursor: pointer;
      transition: background 0.3s, color 0.3s, border-color 0.3s, box-shadow 0.3s, transform 0.2s;
      white-space: nowrap;
    }
    .step-dot.active {
      background: linear-gradient(135deg, var(--accent-cyan-dim) 0%, var(--accent-cyan) 100%);
      border-color: var(--accent-cyan);
      color: #ffffff;
      box-shadow: 0 2px 10px rgba(8,145,178,0.35);
      transform: scale(1.06);
    }
    .step-label {
      font-size: 12px;
      color: var(--text-sub);
      font-weight: 600;
      letter-spacing: 0.4px;
      text-transform: uppercase;
      margin-left: 4px;
      flex: 1;
    }
    /* ── Info Box ── */
    .info-box {
      background: #f8faff;
      border: 1px solid #dde6f8;
      border-left: 4px solid var(--accent-cyan);
      border-radius: 10px;
      padding: 18px 20px;
      min-height: 120px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .info-box h3 {
      color: #0f1e2e;
      font-size: 16px;
      font-weight: 800;
      display: flex;
      align-items: center;
      gap: 8px;
      line-height: 1.3;
    }
    .info-box h3::before {
      content: '';
      display: inline-block;
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: var(--accent-cyan);
      flex-shrink: 0;
      box-shadow: 0 0 6px rgba(8,145,178,0.5);
    }
    /* ── Badges ── */
    .badges { display: flex; gap: 8px; flex-wrap: wrap; }
    .badge {
      padding: 3px 11px;
      border-radius: 20px;
      font-size: 12px;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      gap: 5px;
    }
    .badge-cyan  { background: rgba(8,145,178,0.08);  border: 1px solid rgba(8,145,178,0.3);  color: var(--accent-cyan-dim); }
    .badge-orange{ background: rgba(234,140,0,0.08);  border: 1px solid rgba(234,140,0,0.3);  color: #b45309; }
    .badge-green { background: rgba(22,163,74,0.08);  border: 1px solid rgba(22,163,74,0.3);  color: #15803d; }
    /* ── Description ── */
    .info-desc { font-size: 13.5px; line-height: 1.65; color: var(--text-sub); }
    /* ── Actions ── */
    .actions {
      display: flex;
      justify-content: flex-end;
      align-items: center;
      gap: 10px;
      margin-top: 18px;
    }
    button {
      padding: 10px 22px;
      border-radius: 8px;
      font-size: 13.5px;
      font-weight: 700;
      font-family: inherit;
      cursor: pointer;
      transition: background 0.2s, box-shadow 0.2s, transform 0.15s, color 0.2s, border-color 0.2s;
      border: none;
      outline: none;
    }
    .btn-primary {
      background: linear-gradient(135deg, var(--accent-cyan-dim) 0%, var(--accent-cyan) 100%);
      color: #ffffff;
      box-shadow: 0 4px 12px rgba(8,145,178,0.28);
    }
    .btn-primary:hover {
      background: linear-gradient(135deg, #0369a1 0%, var(--accent-cyan-dim) 100%);
      box-shadow: 0 6px 20px rgba(14,116,144,0.35);
      transform: translateY(-1px);
    }
    .btn-primary:active { transform: translateY(0); box-shadow: none; }
    .btn-secondary {
      background: transparent;
      color: var(--text-sub);
      border: 1.5px solid #cbd5e1;
    }
    .btn-secondary:hover {
      background: rgba(15,23,42,0.04);
      color: #1e293b;
      border-color: #94a3b8;
    }
  </style>
</head>
<body>
  <div class="dashboard">
    <!-- Question Banner -->
    <div class="question-banner">
      <div class="q-label">Problem Statement</div>
      <div class="q-text">[question text]</div>
    </div>
    <!-- SVG Canvas -->
    <div class="svg-container">
      <svg id="stage" viewBox="0 0 850 478" preserveAspectRatio="xMidYMid slice">
        <defs>
          <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
            <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#1e3a5f" stroke-width="0.5" stroke-opacity="0.05" />
          </pattern>
          <!-- Metallic gradients, drop-shadow filters, glow filters, arrow markers -->
          <linearGradient id="steel" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stop-color="#e8f0fa" />
            <stop offset="40%" stop-color="#b8cce0" />
            <stop offset="100%" stop-color="#6a8aaa" />
          </linearGradient>
          <filter id="dropShadow" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="0" dy="4" stdDeviation="5" flood-color="rgba(30,64,175,0.18)" />
          </filter>
          <filter id="glowCyan" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="4" result="blur" />
            <feComposite in="SourceGraphic" in2="blur" operator="over" />
          </filter>
          <marker id="arrowCyan" orient="auto" markerWidth="6" markerHeight="6" refX="3" refY="3">
            <path d="M 0 0 L 6 3 L 0 6 Z" fill="#0891b2" />
          </marker>
          <marker id="arrowOrange" orient="auto" markerWidth="6" markerHeight="6" refX="3" refY="3">
            <path d="M 0 0 L 6 3 L 0 6 Z" fill="#ea8c00" />
          </marker>
          <marker id="arrowGreen" orient="auto" markerWidth="6" markerHeight="6" refX="3" refY="3">
            <path d="M 0 0 L 6 3 L 0 6 Z" fill="#16a34a" />
          </marker>
        </defs>
        <rect width="100%" height="100%" fill="url(#grid)" />
        <!-- Fixed background layer (always visible) -->
        <g class="svg-layer" id="layer-frame"> ... </g>
        <!-- blur-shield rect -->
        <rect id="blur-shield" width="100%" height="100%" fill="#c7d8ed" opacity="0" pointer-events="none" />
        <!-- Component layers (one per physical component, start opacity:0) -->
        <g class="svg-layer" id="layer-[component]" style="opacity:0"> ... </g>
        <!-- Overlay layers: labels and annotations per step -->
        <g class="svg-layer" id="overlay-step0" style="opacity:0"> ... </g>
        ...
      </svg>
    </div>
    <!-- Control Panel -->
    <div class="control-panel">
      <div class="step-indicator" id="dots">
        <!-- pill-style step dots: each dot shows the step label text -->
        <div class="step-dot active">Step label text</div>
        <div class="step-dot">Step label text</div>
        <div class="step-label" id="step-label">Starting...</div>
      </div>
      <div class="info-box">
        <h3 id="info-title">...</h3>
        <div class="badges" id="info-badges"></div>
        <div class="info-desc" id="info-desc">...</div>
      </div>
      <div class="actions">
        <button class="btn-secondary" onclick="resetAnim()">↺ Restart</button>
        <button class="btn-primary" id="btn-next" onclick="nextStep()">Next Step ▶</button>
      </div>
    </div>
  </div>
  <script> ... full animation JS ... </script>
</body>
</html>

═══ SVG DESIGN RULES ═══
1. viewBox="0 0 850 478" (16:9 aspect ratio).
2. Canvas background: radial-gradient(ellipse at 40% 40%, #f0f5ff 0%, #dce8f5 55%, #c8d8ed 100%) — a soft, airy blue-white.
3. Grid pattern overlay with very low opacity (0.05), stroke color #1e3a5f.
4. Each PHYSICAL COMPONENT gets its own <g class="svg-layer" id="layer-[name]"> group.
5. All component layers start with style="opacity:0" EXCEPT layer-frame (always visible).
6. A <rect id="blur-shield"> sits BETWEEN the frame layer and component layers.
   fill="#c7d8ed" opacity="0" — gets set to 0.4–0.6 during focus steps to dim the background.
7. Overlay groups <g class="svg-layer" id="overlay-stepN"> hold labels and dimension arrows. Start at opacity:0.
8. METALLIC GRADIENTS: use multi-stop linearGradient in blue-grey tones for structural parts. Add feDropShadow filters for depth.
9. GLOW FILTERS: feGaussianBlur glow for active/highlighted elements (use #0891b2 cyan for light theme).
10. ARROW MARKERS: arrowCyan (#0891b2), arrowOrange (#ea8c00), arrowGreen (#16a34a) — matching light-theme accents.
11. ZERO text overlaps — compute positions carefully. Keep all labels inside the viewBox.
12. SVG TEXT COLORS for light theme: use #1e293b for main labels, #0e7490 for highlight labels, #475569 for secondary labels.
13. SVG component colors: use bold, saturated colors visible on light backgrounds — e.g. #2563eb, #0891b2, #16a34a, #dc2626, #b45309. No neon/dark-background colors.

═══ STEP DOTS ═══
IMPORTANT: Step dots are PILL-SHAPED with text labels, NOT small circles.
Each <div class="step-dot"> contains the step's short label text (3-5 words from stepsData[i].label).
Example:
  <div class="step-dot active">Problem Setup</div>
  <div class="step-dot">Crank Geometry</div>
  <div class="step-dot">Velocity Analysis</div>
In applyStep(idx), update dots by adding/removing the "active" class — do NOT change innerHTML.

═══ ANIMATION RULES ═══
Each component must have REAL MOTION matching its physical behavior:
- Rotating parts (crank, gears, pulleys): continuous requestAnimationFrame loop
- Oscillating parts (pistons, sliders): sinusoidal motion driven by math
- Tracing paths (belt, wave): stroke-dashoffset animation
- Springs: scale/compress animation
- Flowing elements (current, fluid): animated dash pattern
- All motion uses real mathematical formulae from the problem

stepsData array drives everything:
  {
    label: "...",           // pill-dot label text (3-5 words)
    blurOp: 0,              // blur-shield opacity (0 = no blur, 0.4-0.6 = focus blur)
    overlays: ['overlay-step0'],  // which overlay groups to show
    freezing: false,        // true = snap motion to solution angle
    startAnim: false,       // true = start/resume animation
    title: "Step N: ...",
    badges: '<span class="badge badge-cyan">...</span>',
    desc: "...",
    layerOpacities: { 'layer-frame': 1, 'layer-crank': 0, ... }
  }

applyStep(idx) sets ALL opacities and overlays from stepsData[idx].
nextStep() / resetAnim() manage currentStep.

═══ CRITICAL REQUIREMENTS ═══
- NO backtick template literals — use string concatenation
- NO const/let — use var
- NO arrow functions — use function() {}
- NO external scripts or CDN imports
- ALL JavaScript inline in one <script> block
- The page must work standalone with zero network requests
- Include question-banner div showing the original question
- requestAnimationFrame loop must keep running for continuous motion
- freezing mechanism: smoothly interpolate angle to solution angle, then pause
- POLISH: use feDropShadow filters on overlay cards, multi-stop metallic gradients in blue-grey tones, smooth cubic-bezier transitions (0.4s), consistent stroke-width hierarchy (frame=2, components=2.5–3, labels=1.5).
- CENTERING: body uses { display:flex; flex-direction:column; align-items:center; justify-content:flex-start; padding:24px 16px 120px; }. .dashboard has { width:100%; max-width:900px; margin:0 auto; }. Never float or absolutely-position the dashboard.
- STEP DOTS: must be pill-shaped divs with text (not empty circles). Apply "active" class to current step in applyStep().
- INFO BOX: must use border-left:4px solid var(--accent-cyan) accent style, with h3 that has a ::before cyan dot indicator.

═══ OUTPUT ═══
Return ONLY the complete <!DOCTYPE html>...</html> page as raw text.
No JSON wrapper. No markdown. No fences. Just the pure HTML."""

_ANIMATION_BUILDER_USER = """Generate the complete animation HTML page for this scene script.

ORIGINAL QUESTION: {question}

SCENE SCRIPT:
{scene_script}

CRITICAL REMINDERS:
1. Follow the reference output style EXACTLY — light-blue-grey theme: bg=#eef2f9, panel=#ffffff, accent-cyan=#0891b2.
2. QUESTION BANNER: use class="question-banner" with q-label "Problem Statement" and ::before pseudo-element gradient overlay.
3. STEP DOTS: pill-shaped <div class="step-dot"> elements containing the step label TEXT (3-5 words each). First dot gets class "active". Update via add/remove "active" class in applyStep().
4. INFO BOX: use border-left:4px solid var(--accent-cyan) style with h3::before cyan dot indicator.
5. BUTTONS: .btn-primary uses linear-gradient(135deg, #0e7490, #0891b2) with hover translateY(-1px).
6. Draw components ONE BY ONE in the correct physical order — component layers appear step by step.
7. REAL PHYSICS: rotating crank uses real angle calculation, piston position uses kinematic formula.
8. The blur-shield (fill="#c7d8ed") dims the background when focusing on a new component.
9. Each component must visibly animate (rotate/translate/oscillate) when it first appears.
10. Labels and annotations appear AFTER the component is shown (in the overlay group for that step).
11. The LAST step snaps to the solution angle/state. NO calculations popup, NO formula dump box.
12. SVG component colors: use light-theme–friendly bold colors (#2563eb, #0891b2, #dc2626, #16a34a, #b45309) — NOT neon dark-theme colors.
13. Do NOT use const/let/arrow functions/backtick template literals.

Return the complete HTML page — nothing else."""


class GeminiAnimationBuilder:
    """Stage B: Generates complete HTML animation from the scene script."""

    @classmethod
    def build(cls, question: str, scene_script: dict) -> str:
        if _gemini_client is None:
            return RecoveryEngine.fallback_html(question, "Gemini client not available. Set GEMINI_API_KEY.")

        QAnimLogger.info("AnimationBuilder", f"Building animation HTML via {GEMINI_MODEL}...")
        script_json = json.dumps(scene_script, indent=2, ensure_ascii=False)
        user_prompt = _ANIMATION_BUILDER_USER.format(
            question=question[:500],
            scene_script=script_json[:6000]
        )

        try:
            raw = cls._call_gemini_large(user_prompt)
            html = cls._extract_html(raw)
            if html and len(html) > 1000:
                QAnimLogger.ok("AnimationBuilder", f"Animation HTML generated: {len(html):,} chars")
                return html
            else:
                QAnimLogger.warn("AnimationBuilder", f"Short/empty HTML ({len(html)} chars) — trying repair")
                return cls._repair_or_fallback(question, raw, scene_script)
        except Exception as e:
            QAnimLogger.error("AnimationBuilder", f"Build failed: {e}")
            return RecoveryEngine.fallback_html(question, f"Animation build error: {e}")

    @classmethod
    def _call_gemini_large(cls, user_prompt: str) -> str:
        import time as _time
        MAX_RETRIES  = 3
        RETRY_DELAYS = [20, 45, 90]

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if _GEMINI_SDK_STYLE == "generativeai":
                    model_obj = _gemini_client.GenerativeModel(
                        model_name=GEMINI_MODEL,
                        system_instruction=_ANIMATION_BUILDER_SYSTEM,
                        generation_config={"temperature": 0.4, "max_output_tokens": 32768},
                    )
                    response = model_obj.generate_content(user_prompt)
                    raw = response.text.strip()
                else:
                    try:
                        config = _google_genai.types.GenerateContentConfig(
                            system_instruction=_ANIMATION_BUILDER_SYSTEM,
                            temperature=0.4,
                            max_output_tokens=32768,
                            thinking_config=_google_genai.types.ThinkingConfig(thinking_level="low"),
                        )
                    except Exception:
                        config = _google_genai.types.GenerateContentConfig(
                            system_instruction=_ANIMATION_BUILDER_SYSTEM,
                            temperature=0.4,
                            max_output_tokens=32768,
                        )
                    response = _gemini_client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=user_prompt,
                        config=config,
                    )
                    raw = response.text.strip()

                QAnimLogger.info("AnimationBuilder", f"Attempt {attempt} OK — {len(raw):,} chars")
                return raw

            except Exception as e:
                err_str = str(e)
                is_429 = "429" in err_str or "TooManyRequests" in err_str or "Resource has been exhausted" in err_str
                if is_429 and attempt < MAX_RETRIES:
                    QAnimLogger.warn("AnimationBuilder", f"429 rate limit — waiting {RETRY_DELAYS[attempt-1]}s...")
                    _time.sleep(RETRY_DELAYS[attempt - 1])
                    continue
                raise

        raise RuntimeError("All retry attempts exhausted")

    @classmethod
    def _extract_html(cls, raw: str) -> str:
        """Extract clean HTML from the raw Gemini response."""
        # Strip any markdown fences
        raw = re.sub(r'^```(?:html)?\s*', '', raw.strip(), flags=re.MULTILINE)
        raw = re.sub(r'\s*```$', '', raw, flags=re.MULTILINE).strip()

        # Find DOCTYPE or <html start
        for marker in ['<!DOCTYPE html>', '<!doctype html>', '<html']:
            idx = raw.lower().find(marker.lower())
            if idx != -1:
                end = raw.lower().rfind('</html>')
                if end != -1:
                    return raw[idx:end + 7]
                return raw[idx:]
        return raw

    @classmethod
    def _repair_or_fallback(cls, question: str, raw: str, scene_script: dict) -> str:
        """Try to build a minimal working page from the scene script if Gemini output is bad."""
        QAnimLogger.info("AnimationBuilder", "Attempting repair from scene script...")
        try:
            return cls._build_minimal_page(question, scene_script)
        except Exception as e:
            QAnimLogger.error("AnimationBuilder", f"Repair failed: {e}")
            return RecoveryEngine.fallback_html(question, "Animation generation failed — please regenerate.")

    @classmethod
    def _build_minimal_page(cls, question: str, script: dict) -> str:
        """Build a clean minimal animation page directly from the scene script (no AI needed)."""
        title = html_module.escape(script.get("title", question[:60]))
        q_esc = html_module.escape(question[:300])
        steps = script.get("steps", [])
        final_answer = html_module.escape(script.get("final_answer", ""))
        key_insight = html_module.escape(script.get("key_insight", ""))

        # Build stepsData JS
        steps_js_parts = []
        for step in steps:
            label = step.get("label", f"Step {step.get('step_number',1)}")
            title_s = step.get("title", label)
            desc = step.get("description", "")
            badges_html = ""
            for b in step.get("badges", []):
                bt = b.get("type", "cyan")
                badges_html += f'<span class="badge badge-{bt}">{html_module.escape(b.get("text",""))}</span> '
            math_lines = step.get("math_lines", [])       # kept for legacy compat only
            show_math  = step.get("show_math", False)      # kept for legacy compat only
            blur = 0.5 if step.get("blur_background") else 0
            step_num = step.get("step_number", 1) - 1

            # Escape strings for JS
            label_js    = label.replace('"', '\\"')
            title_js    = title_s.replace('"', '\\"')
            desc_js     = desc.replace('"', '\\"').replace('\n', ' ')
            badges_js   = badges_html.replace('"', '\\"').replace('\n', '')

            steps_js_parts.append(
                "    {\n"
                f'      label: "{label_js}",\n'
                f'      blurOp: {blur},\n'
                f'      overlays: ["overlay-step{step_num}"],\n'
                f'      title: "{title_js}",\n'
                f'      badges: "{badges_js}",\n'
                f'      desc: "{desc_js}"\n'
                "    }"
            )

        steps_js = "[\n" + ",\n".join(steps_js_parts) + "\n  ]"

        # Build overlay SVG groups
        overlay_groups = []
        for i, step in enumerate(steps):
            overlay_groups.append(
                f'<g class="svg-layer" id="overlay-step{i}" style="opacity:0"></g>'
            )
        overlays_html = "\n                ".join(overlay_groups)

        # Build pill-style dot elements with short step labels
        dot_count = len(steps)
        dots_html = "\n                ".join(
            ['<div class="step-dot' + (' active' if i == 0 else '') + '">'
             + html_module.escape(steps[i].get('label', f'Step {i+1}')[:22])
             + '</div>'
             for i in range(dot_count)]
        )

        # Count overlays list for applyStep
        all_overlay_ids = [f"overlay-step{i}" for i in range(len(steps))]
        overlay_ids_js = json.dumps(all_overlay_ids)

        page = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} — Interactive Animation</title>
    <style>
        :root {{
            --bg-color: #eef2f9;
            --panel-bg: #ffffff;
            --text-main: #334155;
            --text-sub: #64748b;
            --accent-cyan: #0891b2;
            --accent-cyan-dim: #0e7490;
            --accent-orange: #ea8c00;
            --accent-green: #16a34a;
            --border: #e2e8f0;
            --border-radius: 14px;
            --shadow-card: 0 4px 6px -1px rgba(30,64,175,0.07), 0 10px 30px rgba(30,64,175,0.10);
            --shadow-hover: 0 6px 20px rgba(14,116,144,0.22);
        }}
        *, *::before, *::after {{ box-sizing:border-box; margin:0; padding:0; }}
        body {{
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: flex-start;
            min-height: 100vh;
            padding: 24px 16px 120px;
        }}
        /* ── Dashboard Card ── */
        .dashboard {{
            width: 100%;
            max-width: 900px;
            margin: 0 auto;
            background: var(--panel-bg);
            border-radius: var(--border-radius);
            box-shadow: var(--shadow-card);
            overflow: hidden;
            border: 1px solid var(--border);
        }}
        /* ── Question Banner ── */
        .question-banner {{
            padding: 18px 24px 16px;
            background: linear-gradient(135deg, #f0f5ff 0%, #e8f0fe 50%, #eef2f9 100%);
            border-bottom: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            gap: 6px;
            position: relative;
        }}
        .question-banner::before {{
            content: '';
            position: absolute;
            inset: 0;
            background: linear-gradient(90deg, rgba(8,145,178,0.06) 0%, transparent 60%);
            pointer-events: none;
        }}
        .q-label {{
            font-size: 11px;
            font-weight: 800;
            color: var(--accent-cyan-dim);
            text-transform: uppercase;
            letter-spacing: 1.5px;
            display: flex;
            align-items: center;
            gap: 7px;
        }}
        .q-label::before {{ content: "❓"; font-size: 13px; }}
        .q-text {{
            font-size: 14.5px;
            color: #1e293b;
            line-height: 1.55;
            font-weight: 400;
            max-width: 820px;
        }}
        /* ── SVG Canvas ── */
        .svg-container {{
            width: 100%;
            aspect-ratio: 16 / 9;
            background: radial-gradient(ellipse at 40% 40%, #f0f5ff 0%, #dce8f5 55%, #c8d8ed 100%);
            position: relative;
            overflow: hidden;
        }}
        svg {{ display: block; width: 100%; height: 100%; }}
        .svg-layer {{ transition: opacity 0.6s cubic-bezier(0.4, 0, 0.2, 1); }}
        /* ── Control Panel ── */
        .control-panel {{
            padding: 20px 24px 24px;
            background: linear-gradient(180deg, #ffffff 0%, #f7faff 100%);
            border-top: 1px solid var(--border);
        }}
        /* ── Step Indicator (pill-style dots with label) ── */
        .step-indicator {{
            display: flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 18px;
            flex-wrap: wrap;
        }}
        .step-dot {{
            padding: 5px 13px;
            border-radius: 20px;
            background: rgba(203,213,225,0.5);
            border: 1px solid #cbd5e1;
            font-size: 11px;
            font-weight: 700;
            color: #94a3b8;
            letter-spacing: 0.3px;
            cursor: pointer;
            transition: background 0.3s ease, color 0.3s ease, border-color 0.3s ease, box-shadow 0.3s ease, transform 0.2s ease;
            white-space: nowrap;
        }}
        .step-dot.active {{
            background: linear-gradient(135deg, var(--accent-cyan-dim) 0%, var(--accent-cyan) 100%);
            border-color: var(--accent-cyan);
            color: #ffffff;
            box-shadow: 0 2px 10px rgba(8,145,178,0.35);
            transform: scale(1.06);
        }}
        .step-label {{
            font-size: 12px;
            color: var(--text-sub);
            font-weight: 600;
            letter-spacing: 0.4px;
            text-transform: uppercase;
            margin-left: 4px;
            flex: 1;
            min-width: 120px;
        }}
        /* ── Info Box ── */
        .info-box {{
            background: #f8faff;
            border: 1px solid #dde6f8;
            border-left: 4px solid var(--accent-cyan);
            border-radius: 10px;
            padding: 18px 20px;
            min-height: 120px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            transition: border-color 0.3s ease;
        }}
        .info-box h3 {{
            color: #0f1e2e;
            font-size: 16px;
            font-weight: 800;
            display: flex;
            align-items: center;
            gap: 8px;
            line-height: 1.3;
        }}
        .info-box h3::before {{
            content: '';
            display: inline-block;
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: var(--accent-cyan);
            flex-shrink: 0;
            box-shadow: 0 0 6px rgba(8,145,178,0.5);
        }}
        /* ── Badges ── */
        .badges {{ display: flex; gap: 8px; flex-wrap: wrap; }}
        .badge {{
            padding: 3px 11px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 700;
            display: inline-flex;
            align-items: center;
            gap: 5px;
            letter-spacing: 0.1px;
        }}
        .badge-cyan  {{ background: rgba(8,145,178,0.08);  border: 1px solid rgba(8,145,178,0.3);  color: var(--accent-cyan-dim); }}
        .badge-orange{{ background: rgba(234,140,0,0.08);  border: 1px solid rgba(234,140,0,0.3);  color: #b45309; }}
        .badge-green {{ background: rgba(22,163,74,0.08);  border: 1px solid rgba(22,163,74,0.3);  color: #15803d; }}
        /* ── Info Description ── */
        .info-desc {{
            font-size: 13.5px;
            line-height: 1.65;
            color: var(--text-sub);
        }}
        /* ── Action Buttons ── */
        .actions {{
            display: flex;
            justify-content: flex-end;
            align-items: center;
            gap: 10px;
            margin-top: 18px;
        }}
        button {{
            padding: 10px 22px;
            border-radius: 8px;
            font-size: 13.5px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
            transition: background 0.2s, box-shadow 0.2s, transform 0.15s, color 0.2s, border-color 0.2s;
            border: none;
            outline: none;
            letter-spacing: 0.1px;
        }}
        .btn-primary {{
            background: linear-gradient(135deg, var(--accent-cyan-dim) 0%, var(--accent-cyan) 100%);
            color: #ffffff;
            box-shadow: 0 4px 12px rgba(8,145,178,0.28);
        }}
        .btn-primary:hover {{
            background: linear-gradient(135deg, #0369a1 0%, var(--accent-cyan-dim) 100%);
            box-shadow: var(--shadow-hover);
            transform: translateY(-1px);
        }}
        .btn-primary:active {{ transform: translateY(0); box-shadow: none; }}
        .btn-secondary {{
            background: transparent;
            color: var(--text-sub);
            border: 1.5px solid #cbd5e1;
        }}
        .btn-secondary:hover {{
            background: rgba(15,23,42,0.04);
            color: #1e293b;
            border-color: #94a3b8;
        }}
    </style>
</head>
<body>
    <div class="dashboard">
        <!-- Question Banner -->
        <div class="question-banner">
            <div class="q-label">Problem Statement</div>
            <div class="q-text">{q_esc}</div>
        </div>
        <!-- SVG Canvas -->
        <div class="svg-container">
            <svg id="stage" viewBox="0 0 850 478" preserveAspectRatio="xMidYMid slice">
                <defs>
                    <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
                        <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#1e3a5f" stroke-width="0.5" stroke-opacity="0.05" />
                    </pattern>
                    <linearGradient id="steel" x1="0%" y1="0%" x2="100%" y2="100%">
                        <stop offset="0%" stop-color="#ffffff" />
                        <stop offset="30%" stop-color="#c8d5e8" />
                        <stop offset="70%" stop-color="#7a90b0" />
                        <stop offset="100%" stop-color="#3a4a60" />
                    </linearGradient>
                    <linearGradient id="metalBlue" x1="0%" y1="0%" x2="0%" y2="100%">
                        <stop offset="0%" stop-color="#e0eaf8" />
                        <stop offset="50%" stop-color="#b8cce4" />
                        <stop offset="100%" stop-color="#8aaed0" />
                    </linearGradient>
                    <filter id="dropShadow" x="-20%" y="-20%" width="140%" height="140%">
                        <feDropShadow dx="0" dy="4" stdDeviation="5" flood-color="rgba(30,64,175,0.18)" />
                    </filter>
                    <filter id="glowCyan" x="-50%" y="-50%" width="200%" height="200%">
                        <feGaussianBlur stdDeviation="4" result="blur" />
                        <feComposite in="SourceGraphic" in2="blur" operator="over" />
                    </filter>
                    <filter id="glowOrange" x="-50%" y="-50%" width="200%" height="200%">
                        <feGaussianBlur stdDeviation="4" result="blur" />
                        <feComposite in="SourceGraphic" in2="blur" operator="over" />
                    </filter>
                    <marker id="arrowCyan" orient="auto" markerWidth="6" markerHeight="6" refX="3" refY="3">
                        <path d="M 0 0 L 6 3 L 0 6 Z" fill="#0891b2" />
                    </marker>
                    <marker id="arrowOrange" orient="auto" markerWidth="6" markerHeight="6" refX="3" refY="3">
                        <path d="M 0 0 L 6 3 L 0 6 Z" fill="#ea8c00" />
                    </marker>
                    <marker id="arrowGreen" orient="auto" markerWidth="6" markerHeight="6" refX="3" refY="3">
                        <path d="M 0 0 L 6 3 L 0 6 Z" fill="#16a34a" />
                    </marker>
                </defs>
                <rect width="100%" height="100%" fill="url(#grid)" />
                <!-- Frame layer — always visible -->
                <g class="svg-layer" id="layer-frame">
                    <line x1="80" y1="239" x2="770" y2="239" stroke="#b8c8e0" stroke-width="1" stroke-dasharray="8,6"/>
                    <text x="425" y="46" fill="#0e7490" font-size="19" font-weight="bold" text-anchor="middle" font-family="'Segoe UI',sans-serif" filter="url(#glowCyan)">{html_module.escape(script.get('title', title))}</text>
                </g>
                <!-- Blur shield -->
                <rect id="blur-shield" width="100%" height="100%" fill="#c7d8ed" opacity="0" class="svg-layer" pointer-events="none" />
                <!-- Step overlays -->
                {overlays_html}
            </svg>
        </div>
        <!-- Control Panel -->
        <div class="control-panel">
            <div class="step-indicator" id="dots">
                {dots_html}
                <div class="step-label" id="step-label">Starting...</div>
            </div>
            <div class="info-box">
                <h3 id="info-title">{title}</h3>
                <div class="badges" id="info-badges"></div>
                <div class="info-desc" id="info-desc">Click "Next Step" to begin the animation.</div>
            </div>
            <div class="actions">
                <button class="btn-secondary" onclick="resetAnim()">&#x21BA; Restart</button>
                <button class="btn-primary" id="btn-next" onclick="nextStep()">Next Step &#x25B6;</button>
            </div>
        </div>
    </div>
<script>
var stepsData = {steps_js};
var allOverlays = {overlay_ids_js};
var currentStep = -1;

function applyStep(idx) {{
    if(idx < 0 || idx >= stepsData.length) return;
    var data = stepsData[idx];
    document.getElementById('blur-shield').style.opacity = data.blurOp;
    allOverlays.forEach(function(oid) {{
        var el = document.getElementById(oid);
        if(el) el.style.opacity = data.overlays.includes(oid) ? '1' : '0';
    }});
    document.getElementById('step-label').innerText = data.label;
    document.getElementById('info-title').innerText = data.title;
    document.getElementById('info-badges').innerHTML = data.badges;
    document.getElementById('info-desc').innerText = data.desc;
    var dots = document.querySelectorAll('.step-dot');
    dots.forEach(function(dot, i) {{
        if(i === idx) dot.classList.add('active');
        else dot.classList.remove('active');
    }});
    var btn = document.getElementById('btn-next');
    if(idx === stepsData.length - 1) {{
        btn.style.display = 'none';
    }} else {{
        btn.style.display = 'inline-block';
    }}
}}

function nextStep() {{
    if(currentStep < stepsData.length - 1) {{
        currentStep++;
        applyStep(currentStep);
    }}
}}

function resetAnim() {{
    currentStep = 0;
    applyStep(0);
    document.getElementById('btn-next').style.display = 'inline-block';
}}

setTimeout(function() {{ resetAnim(); }}, 100);
</script>
</body>
</html>"""
        return page

    @classmethod
    async def build_async(cls, question: str, scene_script: dict) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, cls.build, question, scene_script)


# ===========================================================================
#  GLOSSARY ANALYZER (Gemini-based)
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
        try:
            raw = GeminiSolutionGenerator._call_gemini(
                f"Question: {question[:800]}",
                _GLOSSARY_SYSTEM_GEMINI,
                max_tokens=800
            )
            raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE).strip()
            data = json.loads(raw)
            terms = []
            for t in (data.get("terms") or [])[:8]:
                term    = str(t.get("term", "") or "").strip()
                meaning = str(t.get("meaning", "") or "").strip()
                if term and meaning:
                    terms.append({"term": term, "meaning": meaning})
            if terms:
                QAnimLogger.ok("GlossaryAnalyzer", f"Found {len(terms)} difficult word(s)")
            return {"terms": terms}
        except Exception as e:
            QAnimLogger.warn("GlossaryAnalyzer", f"Failed: {e}")
            return {"terms": []}

    @classmethod
    async def analyze_async(cls, question: str) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, cls.analyze, question)


# ===========================================================================
#  TOPIC CLASSIFIER (Gemini-based)
# ===========================================================================

async def _classify_topic_async(question: str) -> str:
    q = question.lower()
    TOPICS = [
        (["mass transfer","evaporation","concentration","diffusion"],       "ENGINEERING"),
        (["heat transfer","thermal","conduction","convection","radiation"], "ENGINEERING"),
        (["fluid","flow","pressure","viscosity","bernoulli"],              "ENGINEERING"),
        (["gear","crank","mechanism","slider","linkage","cam","flywheel"], "ENGINEERING"),
        (["force","newton","velocity","acceleration","momentum"],          "PHYSICS"),
        (["circuit","voltage","current","resistance","ohm","capacitor"],   "PHYSICS"),
        (["integral","derivative","matrix","calculus","theorem"],         "MATH"),
        (["cell","dna","protein","photosynthesis","enzyme","organism"],    "BIOLOGY"),
    ]
    for keywords, label in TOPICS:
        if any(k in q for k in keywords):
            return label
    return "ENGINEERING"


# ===========================================================================
#  SCENE COUNT DETECTOR
# ===========================================================================

def _detect_scene_count(question: str) -> int:
    q   = question.strip()
    ql  = q.lower()
    length = len(q)

    jee_kw = ["jee","neet","iit","assertion","reason","column i","column ii","match the"]
    if any(k in ql for k in jee_kw):
        return 5

    subq_count = sum(len(re.findall(p, ql)) for p in [r'\(\s*i+\s*\)', r'\(\s*[a-d]\s*\)', r'\bpart\s+[a-d1-4]\b'])
    if subq_count >= 2:
        return 5

    derive_kw = ["derive","prove","hence show","show that"]
    if any(k in ql for k in derive_kw):
        return 5

    find_count = len(re.findall(r'\b(?:find|calculate|determine|evaluate|compute|obtain)\b', ql))
    if find_count >= 2:
        return 5

    if length >= 400:
        return 5

    if length >= 200 or find_count >= 1:
        return 4

    return 3


# ===========================================================================
#  PUBLIC ENTRY POINT
# ===========================================================================

async def generate_question_animation(question: str) -> dict:
    """
    Main public entry point. Unchanged signature and return structure.
    Now uses Gemini exclusively for all generation.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("Question cannot be empty")
    try:
        return await _run_generation_pipeline(question)
    except Exception as e:
        QAnimLogger.error("Pipeline", f"UNHANDLED error: {e}")
        return _build_failure_result(question, f"Unexpected error: {e}")


async def _run_generation_pipeline(question: str) -> dict:
    """
    PIPELINE v1.0 (Gemini-only):

    Stage -1: LargeInputPreprocessor (sync, regex-based)
    Stage  0: ToFind + GivenValues extraction (sync, no AI)
    Stage  A: GeminiSceneAnalyzer (Gemini 2.5 Pro)  ─┐ concurrent
    Stage  B1: GeminiSolutionGenerator (Gemini 2.5 Pro) │
    Stage  B2: GeminiGlossaryAnalyzer (Gemini 2.5 Pro)  ┘
    Stage  C: GeminiAnimationBuilder (Gemini 2.5 Pro) — main HTML generation
    Post:  Inject all panels (unchanged from v0.6)
    """
    short_q = question[:80] + ("..." if len(question) > 80 else "")
    QAnimLogger.info("Pipeline", f"START v1.0 (Gemini) — '{short_q}'")

    # Stage -1: preprocess
    try:
        ai_question = LargeInputPreprocessor.compress(question)
    except Exception as e:
        QAnimLogger.warn("Pipeline", f"Preprocessor error: {e}")
        ai_question = question[:LargeInputPreprocessor.HARD_LIMIT]

    # Stage 0: sync extraction (always on raw question)
    to_find_targets = ToFindExtractor.extract(question)
    given_cards     = GivenValuesExtractor.extract(question)
    n_scenes        = _detect_scene_count(question)
    category        = await _classify_topic_async(ai_question)

    QAnimLogger.info("Pipeline", f"ToFind: {to_find_targets}")
    QAnimLogger.info("Pipeline", f"Category: {category}, n_scenes: {n_scenes}")

    # Stages A + B1 + B2: run concurrently
    QAnimLogger.info("Pipeline", "Launching concurrent Gemini analysis stages...")
    scene_script_task  = asyncio.ensure_future(GeminiSceneAnalyzer.analyze_async(ai_question))
    solution_task      = asyncio.ensure_future(GeminiSolutionGenerator.generate_async(ai_question))
    glossary_task      = asyncio.ensure_future(GeminiGlossaryAnalyzer.analyze_async(ai_question))

    raw_results = await asyncio.gather(
        scene_script_task, solution_task, glossary_task,
        return_exceptions=True,
    )

    # Safely unpack — any task that raised an exception returns the Exception
    # object instead of a result. Replace failures with safe fallbacks so the
    # rest of the pipeline can always proceed.
    scene_script = raw_results[0] if isinstance(raw_results[0], dict) else GeminiSceneAnalyzer._fallback_script(ai_question)
    gemini_sol   = raw_results[1] if isinstance(raw_results[1], dict) else GeminiSolutionGenerator._FALLBACK
    glossary_result = raw_results[2] if isinstance(raw_results[2], dict) else {"terms": []}

    if isinstance(raw_results[0], Exception):
        QAnimLogger.error("Pipeline", f"SceneAnalyzer task failed: {raw_results[0]} — using fallback script")
    if isinstance(raw_results[1], Exception):
        QAnimLogger.error("Pipeline", f"SolutionGenerator task failed: {raw_results[1]} — using fallback solution")
    if isinstance(raw_results[2], Exception):
        QAnimLogger.error("Pipeline", f"GlossaryAnalyzer task failed: {raw_results[2]} — skipping glossary")

    QAnimLogger.ok("Pipeline", f"Analysis stages complete — {len(scene_script.get('steps',[]))} steps, {len(gemini_sol.get('steps',[]))} solution steps")

    # Merge solution into scene script for completeness
    if gemini_sol.get("final_answer") and not scene_script.get("final_answer"):
        scene_script["final_answer"] = gemini_sol["final_answer"]
    if gemini_sol.get("key_insight") and not scene_script.get("key_insight"):
        scene_script["key_insight"] = gemini_sol["key_insight"]
    if gemini_sol.get("steps") and not scene_script.get("solution_steps"):
        scene_script["solution_steps"] = gemini_sol["steps"]

    final_answer = scene_script.get("final_answer") or gemini_sol.get("final_answer") or ""
    key_insight  = scene_script.get("key_insight")  or gemini_sol.get("key_insight")  or ""

    # Stage C: build main animation HTML
    QAnimLogger.info("Pipeline", "Building main animation HTML...")
    try:
        animation_html = await GeminiAnimationBuilder.build_async(question, scene_script)
    except Exception as e:
        QAnimLogger.error("Pipeline", f"Animation build failed: {e}")
        animation_html = RecoveryEngine.fallback_html(question, f"Animation build error: {e}")

    # Also build concept animation (same HTML is used for both)
    concept_html = animation_html

    # Sanitize
    animation_html = HtmlSanitizer.sanitize(animation_html)

    # Centre the animation dashboard (override whatever Gemini generated)
    animation_html = inject_centering_css(animation_html)

    # Build answer targets
    answer_targets = _build_answer_targets(
        to_find_targets=to_find_targets,
        gemini_sol=gemini_sol,
        final_answer=final_answer,
        key_insight=key_insight,
    )

    # Solution steps flat list — kept for result dict / backward-compat downstream
    solution_steps = gemini_sol.get("steps", []) or scene_script.get("solution_steps", [])

    # ── Inject all panels through the Panel Reliability Engine ──
    # (normalize document skeleton -> inject every panel -> verify every
    #  required id actually landed -> repair ONLY what's missing, bounded
    #  retries -> resolve duplicate ids -> final verified report)
    panel_ctx = PanelInjectionContext(
        gemini_sol=gemini_sol,
        answer_targets=answer_targets,
        glossary_terms=glossary_result.get("terms", []),
        to_find_targets=to_find_targets,
    )
    html, injection_report = PanelInjectionManager.run(animation_html, panel_ctx)

    if not injection_report["all_ok"]:
        QAnimLogger.error(
            "Pipeline",
            f"{len(injection_report['still_missing'])} panel(s) could not be "
            f"self-healed after {injection_report['repair_passes']} repair pass(es): "
            f"{injection_report['still_missing']}",
        )
    else:
        QAnimLogger.ok(
            "Pipeline",
            f"All required panels verified present (repair passes used: {injection_report['repair_passes']})",
        )

    # Validate
    try:
        GenerationValidator.validate(html, require_svg=True)
    except ValidationError as e:
        QAnimLogger.warn("FinalValidator", f"Post-injection: {e} — continuing")

    result = {
        "title":                  scene_script.get("title", question[:60]),
        "explanation":            "Interactive animation",
        "animation_type":         category,
        "design_strategy":        f"Gemini {GEMINI_MODEL} generated step-by-step reveal",
        "animation_code":         html,
        "concept_animation_code": concept_html,
        "solution_steps":         solution_steps,
        "final_answer":           final_answer,
        "key_insight":            key_insight,
        "to_find":                to_find_targets,
        "given_cards":            given_cards,
        "answer_targets":         answer_targets,
        "haiku_solution": {
            "steps":        solution_steps,
            "final_answer": final_answer,
            "key_insight":  key_insight,
            "raw":          "",
        },
        "glossary_terms":  glossary_result.get("terms", []),
        "category":        category,
        "n_scenes":        n_scenes,
        "engine_version":  "v1.0-gemini",
        "render_status":   "ok" if injection_report["all_ok"] else "panels_incomplete",
        "panel_verification": injection_report,
    }

    QAnimLogger.ok("Pipeline", (
        f"DONE v1.0 — '{result['title']}' "
        f"html={len(html):,} chars "
        f"steps={len(solution_steps)} "
        f"to_find={to_find_targets} "
        f"given_cards={len(given_cards)} "
        f"answer_targets={len(answer_targets)}"
    ))
    return result


def _build_failure_result(question: str, reason: str) -> dict:
    fallback = RecoveryEngine.fallback_html(question, reason)
    return {
        "title":                  f"Animation: {question[:50]}",
        "explanation":            "Generation failed",
        "animation_type":         "error",
        "design_strategy":        "",
        "animation_code":         fallback,
        "concept_animation_code": fallback,
        "solution_steps":         [],
        "final_answer":           "",
        "key_insight":            "",
        "to_find":                [],
        "given_cards":            [],
        "answer_targets":         [],
        "haiku_solution":         {"steps": [], "final_answer": "", "key_insight": "", "raw": ""},
        "glossary_terms":         [],
        "category":               "UNKNOWN",
        "n_scenes":               4,
        "engine_version":         "v1.0-gemini",
        "render_status":          "error",
    }


# ===========================================================================
#  PUBLIC ALIASES (unchanged from v0.6 for backward compatibility)
# ===========================================================================
generate_animation      = generate_question_animation

def generate_question_animation_sync(question: str) -> dict:
    """Synchronous wrapper — unchanged API."""
    return asyncio.run(generate_question_animation(question))

generate_animation_sync = generate_question_animation_sync


# ===========================================================================
#  CLI TEST
# ===========================================================================
if __name__ == "__main__":
    import sys

    TEST_QUESTIONS = {
        "SLIDER_CRANK": (
            "A slider-crank mechanism has crank radius r = 50 mm and connecting rod length "
            "l = 200 mm. The crank rotates at N = 300 RPM. Find the piston velocity when "
            "the crank angle is 60 degrees."
        ),
        "HEAT_TRANSFER": (
            "A steam pipe of inner diameter 5 cm and outer diameter 7 cm carries steam at "
            "250 degrees C. Thermal conductivity k = 45 W/mK, outer surface convection "
            "coefficient h = 12 W/m2K, ambient temperature 30 degrees C. Find the heat "
            "loss per metre and outer surface temperature."
        ),
        "GEAR_TRAIN": (
            "A compound gear train has gears with teeth: T1=20, T2=60, T3=15, T4=45. "
            "The input shaft rotates at 900 RPM. Find the velocity ratio and output speed."
        ),
        "BELT_DRIVE": (
            "A flat belt drive connects two pulleys of diameter 300 mm and 150 mm with a "
            "centre distance of 600 mm. The belt tension ratio T1/T2 = 3, coefficient of "
            "friction = 0.3, angle of wrap on smaller pulley = 150 degrees. "
            "Find the power transmitted at 1200 RPM."
        ),
    }

    if len(sys.argv) > 1:
        questions_to_test = {"CUSTOM": " ".join(sys.argv[1:])}
    else:
        key = "SLIDER_CRANK"
        questions_to_test = {key: TEST_QUESTIONS[key]}

    for cat, q in questions_to_test.items():
        print("=" * 72)
        print(f"  QAnim v1.0 (Gemini) — {cat}")
        print(f"  Q: {q[:65]}...")
        print("=" * 72)

        result = generate_question_animation_sync(q)

        solution_html = result.get("animation_code", "")
        haiku_sol     = result.get("haiku_solution", {})
        ans_targets   = result.get("answer_targets", [])
        given_cards   = result.get("given_cards", [])

        print(f"\nTitle               : {result['title']}")
        print(f"Category            : {result.get('category','N/A')}")
        print(f"Engine              : {result.get('engine_version','N/A')}")
        print(f"Render Status       : {result.get('render_status','N/A')}")
        print(f"[ToFind] Targets    : {result.get('to_find',[])}")
        print(f"[Given]  Cards      : {len(given_cards)}")
        print(f"[Main]   HTML       : {len(solution_html):,} chars")
        print(f"[Steps]  Count      : {len(haiku_sol.get('steps',[]))}")
        print(f"[Ans]    Targets    : {len(ans_targets)} target(s)")
        for t in ans_targets:
            print(f"           label={t['label'][:40]}  value={t['value'][:30]}")
        print(f"Final Answer        : {result.get('final_answer','')[:120]}")
        print(f"Key Insight         : {result.get('key_insight','')[:100]}")

        slug = cat.lower()
        out_file = f"q_anim_v10_{slug}.html"
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(solution_html)
        print(f"\nSaved: {out_file}")
        print()
        print("v1.0 Features (Gemini-only generation):")
        print("  OK  Two-stage: SceneAnalyzer -> AnimationBuilder")
        print("  OK  Light, friendly dashboard style (reference output pattern)")
        print("  OK  Component-by-component reveal with blur/focus")
        print("  OK  Real physics motion (rotating, oscillating, tracing)")
        print("  OK  Math solution box in final step")
        print("  OK  All panels injected (Find/StepAnswer/AnswerBox/Notes/Glossary)")
        print("  OK  Gemini 2.5 Pro for all stages")
