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
  - All post-processing injection functions (ToFind, FinalAnswer, AnswerBox,
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
  padding: 20px 20px 110px 20px !important;
  box-sizing: border-box !important;
}
.dashboard {
  width: 100% !important;
  max-width: 900px !important;
  margin-left: auto !important;
  margin-right: auto !important;
  margin-bottom: 20px !important;
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
  --blue-deep:  #1e40af; --blue-mid: #3b5bdb; --blue-light: #60a5fa;
  --sky: #e0f2fe; --sky-dark: #0ea5e9; --teal: #0d9488;
  --green: #16a34a; --amber: #f59e0b; --rose: #e64980;
  --slate-900: #0f172a; --slate-800: #1e293b; --slate-600: #475569;
  --slate-400: #94a3b8; --slate-200: #e2e8f0; --slate-100: #f1f5f9;
  --white: #ffffff; --bg: #f0f5ff; --card-bg: #ffffff;
  --max-w: 1080px; --radius-xl: 18px; --radius-lg: 12px; --radius-md: 8px;
  --shadow-lg: 0 8px 32px rgba(30,64,175,0.13),0 2px 8px rgba(0,0,0,0.07);
  --shadow-md: 0 4px 16px rgba(30,64,175,0.10);
  --font: 'Segoe UI',system-ui,-apple-system,Arial,sans-serif;
}
html { overflow-x:hidden!important; overflow-y:auto!important; min-height:100vh; width:100%!important; }
body { background:var(--bg); font-family:var(--font); min-height:100vh; width:100%;
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
    var tfBtn=_el('tofind-btn')||document.querySelector('[data-tofind-btn]');
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
        tofind_script = '<script>\n' + TO_FIND_JS_MODULE + '\n</script>'
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

def _build_step_answer_data_tag(steps):
    payload = {"steps": [str(s) for s in (steps or [])]}
    return ('<script type="application/json" id="__step_answer_data__">\n'
            + json.dumps(payload, ensure_ascii=False, indent=2) + '\n</script>')


_STEP_ANSWER_DOM = """
<div id="stepans-backdrop" aria-hidden="true"></div>
<aside id="stepans-panel" role="dialog" aria-labelledby="stepans-heading" aria-hidden="true">
  <div class="sa-header">
    <div class="sa-header-left">
      <div class="sa-icon-wrap">&#x1FA9C;</div>
      <div>
        <div id="stepans-heading" class="sa-title">Step by Step Answer</div>
        <div class="sa-subtitle">Solution workflow for this question</div>
      </div>
    </div>
    <button id="stepans-close" class="sa-close-btn" aria-label="Close">&#x2715;</button>
  </div>
  <div class="sa-flow-track-wrap">
    <div id="sa-flow-track" class="sa-flow-track"></div>
  </div>
  <div class="sa-body">
    <div id="stepans-items-container" class="sa-items-container"></div>
  </div>
  <div class="sa-footer">
    <button id="sa-prev-btn" class="sa-nav-btn sa-prev-btn" type="button">&#8249; Previous</button>
    <div id="sa-progress-label" class="sa-progress-label">Step 1 of 1</div>
    <button id="sa-next-btn" class="sa-nav-btn sa-next-btn" type="button">Next &#8250;</button>
  </div>
</aside>
"""

_STEP_ANSWER_CSS = """
<style id="qanim-stepans-styles">
#stepans-backdrop { display:none; position:fixed; inset:0; z-index:8500;
  background:rgba(15,23,42,.42); backdrop-filter:blur(6px); opacity:0; transition:opacity .24s ease; }
#stepans-backdrop.open { display:block; opacity:1; }
#stepans-panel { display:flex; flex-direction:column; position:fixed; top:50%; left:50%;
  transform:translate(-50%,-48%) scale(.96); z-index:8600; width:min(820px,96vw);
  max-height:92vh; border-radius:18px; background:#fff; border:1px solid #e2e8f0;
  box-shadow:0 20px 60px rgba(37,99,235,.18),0 2px 8px rgba(0,0,0,.06);
  opacity:0; pointer-events:none;
  transition:opacity .28s ease,transform .28s cubic-bezier(.34,1.56,.64,1); overflow:hidden; }
#stepans-panel.open { opacity:1; pointer-events:auto; transform:translate(-50%,-50%) scale(1); }
.sa-header { display:flex; align-items:center; justify-content:space-between;
  padding:18px 22px 14px; border-bottom:1px solid #f0f0f8; flex-shrink:0; background:#fff; }
.sa-header-left { display:flex; align-items:center; gap:13px; }
.sa-icon-wrap { width:40px; height:40px; border-radius:10px; background:#eff6ff;
  display:flex; align-items:center; justify-content:center; font-size:20px; flex-shrink:0; }
.sa-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:17px; font-weight:800; color:#1a1a2e; }
.sa-subtitle { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:11px; color:#64748b; margin-top:2px; }
.sa-close-btn { width:34px; height:34px; border-radius:50%; border:1.5px solid #e8e8f0;
  background:#fafafa; color:#888; font-size:13px; display:flex; align-items:center; justify-content:center;
  cursor:pointer; transition:background .15s,color .15s,border-color .15s; flex-shrink:0; }
.sa-close-btn:hover { background:#fee2e2; color:#dc2626; border-color:#fca5a5; }
.sa-flow-track-wrap { flex-shrink:0; background:#fafbff; border-bottom:1px solid #f0f0f8;
  padding:14px 22px 12px; overflow-x:auto; overflow-y:hidden; }
.sa-flow-track { display:flex; align-items:center; min-width:max-content; padding:2px 2px 4px; }
.sa-flow-node { display:flex; align-items:center; cursor:pointer; background:none; border:none;
  padding:0; font:inherit; }
.sa-flow-dot { width:30px; height:30px; border-radius:50%; background:#e9edf5; color:#7c8aa0;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12px; font-weight:800;
  display:flex; align-items:center; justify-content:center; flex-shrink:0; border:2px solid transparent;
  transition:background .22s ease,color .22s ease,transform .22s ease,box-shadow .22s ease,border-color .22s ease; }
.sa-flow-node:hover .sa-flow-dot { background:#dbe6fd; color:#1d4ed8; }
.sa-flow-node.sa-done .sa-flow-dot { background:#dcfce7; color:#16a34a; }
.sa-flow-node.sa-active .sa-flow-dot { background:#2563eb; color:#fff; border-color:#bfdbfe;
  box-shadow:0 0 0 4px rgba(37,99,235,.16); transform:scale(1.14); }
.sa-flow-line { width:28px; height:2px; background:#e2e8f0; flex-shrink:0; margin:0 1px;
  transition:background .22s ease; }
.sa-flow-line.sa-done { background:#86efac; }
.sa-body { overflow-y:auto; flex:1; padding:20px 22px 10px; display:flex; flex-direction:column; }
.sa-items-container { display:flex; flex-direction:column; }
.sa-step-card { display:flex; align-items:flex-start; gap:16px; padding:20px 20px;
  border-radius:14px; background:#f8fafc; border:1px solid #e2e8f0; border-left:4px solid #2563eb;
  opacity:0; transform:translateX(14px); transition:opacity .22s ease,transform .22s ease; }
.sa-step-card.visible { opacity:1; transform:translateX(0); }
.sa-step-num { min-width:38px; height:38px; border-radius:50%; background:#2563eb; color:#fff;
  font-size:15px; font-weight:800; display:flex; align-items:center; justify-content:center;
  flex-shrink:0; box-shadow:0 2px 8px rgba(37,99,235,.30); }
.sa-step-body { flex:1; min-width:0; }
.sa-step-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12.5px; font-weight:700;
  color:#1d4ed8; text-transform:uppercase; letter-spacing:.5px; margin-bottom:7px; }
.sa-step-text { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:14.5px; font-weight:500;
  color:#1e293b; line-height:1.75; white-space:pre-wrap; word-break:break-word; }
.sa-empty { font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:13px; color:#94a3b8; text-align:center; padding:28px 0; font-style:italic; }
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
/* ── Given data boxes (Step 1) ── */
.sa-given-grid { display:flex; flex-wrap:wrap; gap:10px; padding:4px 0 6px; }
.sa-given-box { display:inline-flex; align-items:center; gap:6px; padding:8px 14px;
  border-radius:10px; background:#eff6ff; border:1.5px solid #bfdbfe; color:#1d4ed8;
  font-family:'Courier New',Courier,monospace; font-size:14px; font-weight:700;
  box-shadow:0 1px 4px rgba(37,99,235,.10); white-space:nowrap; }
.sa-given-box:nth-child(4n+1) { background:#eff6ff; border-color:#bfdbfe; color:#1d4ed8; }
.sa-given-box:nth-child(4n+2) { background:#f0fdf4; border-color:#bbf7d0; color:#15803d; }
.sa-given-box:nth-child(4n+3) { background:#fefce8; border-color:#fde68a; color:#b45309; }
.sa-given-box:nth-child(4n)   { background:#fdf4ff; border-color:#e9d5ff; color:#7e22ce; }
/* ── Formula flowchart (Step 4) ── */
.sa-flowchart { display:flex; flex-direction:column; align-items:center; gap:0; padding:6px 0; }
.sa-flow-formula-box { width:100%; max-width:560px; padding:13px 20px; border-radius:12px;
  text-align:center; font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:15px; font-weight:700; box-sizing:border-box; margin:0 auto; }
.sa-flow-formula-box.blue  { background:#eff6ff; border:2px solid #93c5fd; color:#1d4ed8; }
.sa-flow-formula-box.orange{ background:#fff7ed; border:2px solid #fdba74; color:#c2410c; }
.sa-flow-formula-box.purple{ background:#faf5ff; border:2px solid #d8b4fe; color:#7c3aed; }
.sa-flow-formula-box.pink  { background:#fff1f2; border:2px solid #fda4af; color:#be123c; }
.sa-flow-formula-box .sub-text { font-size:12px; font-weight:400; color:inherit; opacity:0.72; margin-top:4px; }
.sa-flow-arrow { width:2px; height:28px; background:linear-gradient(to bottom,#93c5fd,#c084fc);
  margin:0 auto; position:relative; }
.sa-flow-arrow::after { content:'▼'; position:absolute; bottom:-10px; left:50%;
  transform:translateX(-50%); font-size:14px; color:#c084fc; }
.sa-flow-footer-note { font-size:11.5px; color:#64748b; text-align:center; margin-top:10px; font-style:italic; }
/* ── Substitution steps (Step 5) ── */
.sa-sub-container { display:flex; flex-direction:column; gap:10px; padding:4px 0; }
.sa-sub-card { display:flex; align-items:flex-start; gap:14px; padding:14px 16px;
  border-radius:12px; background:#f8fafc; border:1px solid #e2e8f0; border-left:4px solid #2563eb; }
.sa-sub-card:nth-child(2) { border-left-color:#0d9488; }
.sa-sub-card:nth-child(3) { border-left-color:#16a34a; }
.sa-sub-card:nth-child(4) { border-left-color:#d97706; }
.sa-sub-num { min-width:32px; height:32px; border-radius:50%; background:#2563eb; color:#fff;
  font-size:13px; font-weight:800; display:flex; align-items:center; justify-content:center;
  flex-shrink:0; box-shadow:0 2px 6px rgba(37,99,235,.28); }
.sa-sub-card:nth-child(2) .sa-sub-num { background:#0d9488; box-shadow:0 2px 6px rgba(13,148,136,.28); }
.sa-sub-card:nth-child(3) .sa-sub-num { background:#16a34a; box-shadow:0 2px 6px rgba(22,163,74,.28); }
.sa-sub-card:nth-child(4) .sa-sub-num { background:#d97706; box-shadow:0 2px 6px rgba(217,119,6,.28); }
.sa-sub-body { flex:1; min-width:0; }
.sa-sub-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12px; font-weight:700;
  color:#475569; text-transform:uppercase; letter-spacing:.5px; margin-bottom:5px; }
.sa-sub-expr { font-family:'Courier New',Courier,monospace; font-size:14.5px; font-weight:600;
  color:#1e293b; line-height:1.6; white-space:pre-wrap; word-break:break-word; }
</style>
"""

STEP_ANSWER_JS_MODULE = r"""
(function initStepAnswerSystem(){
  'use strict';
  var stepAnsOpen=false,_built=false,_steps=[],_cur=0;
  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  function _loadSteps(){
    try{var tag=_el('__step_answer_data__');if(!tag)return[];var data=JSON.parse(tag.textContent)||{};return Array.isArray(data.steps)?data.steps:[];}catch(e){return[];}
  }
  function _splitStep(raw,idx){
    var text=String(raw||'').trim();
    var m=text.match(/^(Step\s*\d+)\s*[:\-]\s*(.*)$/i);
    if(m&&m[2]){return{num:parseInt(m[1].replace(/\D/g,''),10)||idx+1,title:m[1],body:m[2]};}
    return{num:idx+1,title:'Step '+(idx+1),body:text};
  }

  /* ── Extract given values from step 1 body text ──────────────────────── */
  function _parseGivenBoxes(bodyText){
    var boxes=[];
    /* match patterns like: symbol = value unit  or  Name = value unit */
    var re=/([A-Za-z_\u00b0][A-Za-z_0-9\u00b0\u03b1-\u03c9\u0391-\u03a9]{0,20})\s*=\s*([-+]?\d[\d.,]*(?:\s*[xX\u00d7]\s*10[\^]?[-+]?\d+)?)\s*([A-Za-z\u00b0\u00b2\u00b3][A-Za-z\u00b0\u00b2\u00b3\u00b7\/\s]{0,15})?/g;
    var m;
    while((m=re.exec(bodyText))!==null){
      var sym=m[1].trim(),val=m[2].trim(),unit=(m[3]||'').trim().replace(/[.,;]+$/,'');
      if(sym&&val){boxes.push(sym+' = '+val+(unit?' '+unit:''));}
      if(boxes.length>=12)break;
    }
    /* fallback: split by comma/semicolon if regex found nothing */
    if(boxes.length===0){
      var parts=bodyText.split(/[,;]/);
      for(var i=0;i<parts.length&&i<10;i++){var p=parts[i].trim();if(p.length>1&&p.length<50)boxes.push(p);}
    }
    return boxes;
  }

  /* ── Extract formula flowchart rows from step 4 body text ─────────────── */
  function _parseFlowchart(bodyText){
    /* look for lines that look like formulas: contain =, numbers, or variables */
    var rows=[];
    var lines=bodyText.split(/[\n;|→\-]+/);
    var colors=['blue','orange','purple','pink'];
    for(var i=0;i<lines.length;i++){
      var ln=lines[i].trim().replace(/^[-–•*]+\s*/,'');
      if(ln.length>2){rows.push({text:ln,color:colors[rows.length%4]});}
      if(rows.length>=6)break;
    }
    /* if only one long sentence, split at connectors */
    if(rows.length<=1){
      var alt=bodyText.split(/[,](?=\s*(?:where|so|then|therefore|thus|gives|yields|hence))/i);
      rows=[];
      for(var j=0;j<alt.length&&j<5;j++){var s=alt[j].trim();if(s.length>1){rows.push({text:s,color:colors[j%4]});}}
    }
    /* last resort: treat whole body as one box */
    if(rows.length===0){rows=[{text:bodyText,color:'blue'}];}
    return rows;
  }

  /* ── Extract substitution sub-steps from step 5 body text ─────────────── */
  function _parseSubstitution(bodyText){
    var labels=['Write Formula','Substitute Values','Simplify','Result'];
    var cards=[];
    var DELIM='||SPLIT||';
    var raw=bodyText;
    raw=raw.replace(/\u2192|\u27f9|=>/g,DELIM);
    raw=raw.replace(/\n|\r/g,DELIM);
    raw=raw.replace(/(\d)\.\s+([A-Z])/g,'$1.'+DELIM+'$2');
    raw=raw.replace(/\.\s+(Write|Substitute|Plug|Put|Calculate|Solve|Result|Final|Therefore|Thus|Hence)\b/gi,DELIM+'$1');
    var parts=raw.split(DELIM);
    for(var i=0;i<parts.length&&cards.length<4;i++){
      var p=parts[i].trim().replace(/^\d+[.)]\s*/,'');
      if(p.length>2){cards.push({title:labels[cards.length]||('Step '+(cards.length+1)),expr:p});}
    }
    if(cards.length<=1){
      var alt2=bodyText.replace(/,\s*([A-Z])/g,DELIM+'$1').split(DELIM);
      if(alt2.length>1){
        cards=[];
        for(var k=0;k<alt2.length&&k<4;k++){
          var q=alt2[k].trim();
          if(q.length>2){cards.push({title:labels[k]||('Step '+(k+1)),expr:q});}
        }
      }
    }
    if(cards.length===0){cards=[{title:'Substitution',expr:bodyText}];}
    return cards;
  }

  /* ── Render a step card ─────────────────────────────────────────────────── */
  function _renderCard(){
    var container=_el('stepans-items-container');if(!container)return;
    if(!_steps||_steps.length===0){
      container.innerHTML='<div class="sa-empty">No step-by-step solution is available.</div>';return;
    }
    var parts=_splitStep(_steps[_cur],_cur);
    var stepNum=parts.num;   /* 1-based step number */
    var inner='';

    if(stepNum===1){
      /* ── Step 1: Given data boxes ── */
      var boxes=_parseGivenBoxes(parts.body);
      var boxHtml='';
      for(var b=0;b<boxes.length;b++){boxHtml+='<div class="sa-given-box">'+_esc(boxes[b])+'</div>';}
      if(!boxHtml){boxHtml='<div class="sa-given-box">'+_esc(parts.body)+'</div>';}
      inner='<div class="sa-given-grid">'+boxHtml+'</div>';
    } else if(stepNum===4){
      /* ── Step 4: Formula flowchart ── */
      var rows=_parseFlowchart(parts.body);
      var fcHtml='';
      for(var r=0;r<rows.length;r++){
        fcHtml+='<div class="sa-flow-formula-box '+_esc(rows[r].color)+'">'+_esc(rows[r].text)+'</div>';
        if(r<rows.length-1){fcHtml+='<div class="sa-flow-arrow"></div>';}
      }
      inner='<div class="sa-flowchart">'+fcHtml+'</div>';
    } else if(stepNum===5){
      /* ── Step 5: Substitution steps ── */
      var cards=_parseSubstitution(parts.body);
      var subHtml='';
      for(var c=0;c<cards.length;c++){
        subHtml+='<div class="sa-sub-card"><div class="sa-sub-num">'+(c+1)+'</div>'
          +'<div class="sa-sub-body"><div class="sa-sub-title">'+_esc(cards[c].title)+'</div>'
          +'<div class="sa-sub-expr">'+_esc(cards[c].expr)+'</div></div></div>';
      }
      inner='<div class="sa-sub-container">'+subHtml+'</div>';
    } else {
      /* ── Default: plain text card (Steps 2, 3, 6, …) ── */
      inner='<div class="sa-step-text">'+_esc(parts.body)+'</div>';
    }

    container.innerHTML='<div class="sa-step-card" id="sa-step-'+_cur+'">'
      +'<div class="sa-step-num">'+(_cur+1)+'</div>'
      +'<div class="sa-step-body"><div class="sa-step-title">'+_esc(parts.title)+'</div>'
      +inner+'</div></div>';

    var card=container.querySelector('.sa-step-card');
    if(card){card.style.transition='none';setTimeout(function(){card.style.transition='opacity .22s ease,transform .22s ease';card.classList.add('visible');},20);}
  }

  function _buildFlowTrack(){
    var track=_el('sa-flow-track');if(!track)return;
    if(_steps.length===0){track.innerHTML='';return;}
    var html='';
    for(var i=0;i<_steps.length;i++){
      html+='<button type="button" class="sa-flow-node" data-idx="'+i+'" title="'+_esc(_splitStep(_steps[i],i).title)+'"><div class="sa-flow-dot">'+(i+1)+'</div></button>';
      if(i<_steps.length-1){html+='<div class="sa-flow-line" data-line="'+i+'"></div>';}
    }
    track.innerHTML=html;
    var nodes=track.querySelectorAll('.sa-flow-node');
    for(var n=0;n<nodes.length;n++){
      nodes[n].addEventListener('click',function(e){
        e.stopPropagation();
        var idx=parseInt(this.getAttribute('data-idx'),10);
        if(!isNaN(idx))_goTo(idx);
      });
    }
  }
  function _updateFlowTrack(){
    var track=_el('sa-flow-track');if(!track)return;
    var nodes=track.querySelectorAll('.sa-flow-node');
    var lines=track.querySelectorAll('.sa-flow-line');
    for(var i=0;i<nodes.length;i++){
      nodes[i].classList.remove('sa-active','sa-done');
      if(i===_cur)nodes[i].classList.add('sa-active');
      else if(i<_cur)nodes[i].classList.add('sa-done');
    }
    for(var j=0;j<lines.length;j++){
      var lidx=parseInt(lines[j].getAttribute('data-line'),10);
      if(lidx<_cur)lines[j].classList.add('sa-done');else lines[j].classList.remove('sa-done');
    }
    var activeNode=track.querySelector('.sa-flow-node.sa-active');
    if(activeNode&&activeNode.scrollIntoView){activeNode.scrollIntoView({behavior:'smooth',inline:'center',block:'nearest'});}
  }
  function _updateFooter(){
    var prevBtn=_el('sa-prev-btn'),nextBtn=_el('sa-next-btn'),label=_el('sa-progress-label');
    var n=_steps.length;
    if(label)label.textContent=n?('Step '+(_cur+1)+' of '+n):'No steps';
    if(prevBtn)prevBtn.disabled=(_cur<=0);
    if(nextBtn){
      nextBtn.disabled=(n===0);
      nextBtn.textContent=(n>0&&_cur>=n-1)?'Done \u2713':'Next \u203A';
    }
  }
  function _goTo(idx){
    if(!_steps||_steps.length===0)return;
    if(idx<0)idx=0;if(idx>_steps.length-1)idx=_steps.length-1;
    _cur=idx;
    _renderCard();_updateFlowTrack();_updateFooter();
  }
  function _buildPanel(){
    if(_built)return;_built=true;
    _steps=_loadSteps();_cur=0;
    _buildFlowTrack();_renderCard();_updateFlowTrack();_updateFooter();
  }
  function openStepAnswer(){
    _buildPanel();
    var backdrop=_el('stepans-backdrop'),panel=_el('stepans-panel');if(!backdrop||!panel)return;
    backdrop.classList.add('open');panel.classList.add('open');panel.setAttribute('aria-hidden','false');stepAnsOpen=true;
  }
  function closeStepAnswer(){
    var backdrop=_el('stepans-backdrop'),panel=_el('stepans-panel');
    if(backdrop)backdrop.classList.remove('open');
    if(panel){panel.classList.remove('open');panel.setAttribute('aria-hidden','true');}
    stepAnsOpen=false;
  }
  window.openStepAnswer=openStepAnswer;window.closeStepAnswer=closeStepAnswer;
  window.toggleStepAnswer=function(){stepAnsOpen?closeStepAnswer():openStepAnswer();};
  _onReady(function(){
    function wireBtn(){var btn=document.getElementById('stepans-ctrl-btn');
      if(btn){btn.removeAttribute('onclick');btn.addEventListener('click',function(e){e.stopPropagation();stepAnsOpen?closeStepAnswer():openStepAnswer();});}
      else{setTimeout(wireBtn,80);}
    }
    wireBtn();
    var closeBtn=_el('stepans-close');if(closeBtn)closeBtn.addEventListener('click',function(e){e.stopPropagation();closeStepAnswer();});
    var backdrop=_el('stepans-backdrop');if(backdrop)backdrop.addEventListener('click',function(e){if(e.target===backdrop)closeStepAnswer();});
    var prevBtn=_el('sa-prev-btn');if(prevBtn)prevBtn.addEventListener('click',function(e){e.stopPropagation();_goTo(_cur-1);});
    var nextBtn=_el('sa-next-btn');if(nextBtn)nextBtn.addEventListener('click',function(e){e.stopPropagation();if(_cur>=_steps.length-1){closeStepAnswer();}else{_goTo(_cur+1);}});
    document.addEventListener('keydown',function(e){
      if(!stepAnsOpen)return;
      if(e.key==='Escape')closeStepAnswer();
      else if(e.key==='ArrowRight')_goTo(_cur+1);
      else if(e.key==='ArrowLeft')_goTo(_cur-1);
    });
  });
})();
"""


def inject_step_answer_panel(html, solution_steps):
    html = re.sub(r'<script[^>]+id=["\']__step_answer_data__["\'][^>]*>.*?</script>', '', html, flags=re.DOTALL)
    html = re.sub(r'<style[^>]+id=["\']qanim-stepans-styles["\'][^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)

    try:
        data_tag = _build_step_answer_data_tag(solution_steps)
        if '</head>' in html:
            html = html.replace('</head>', data_tag + '\n</head>', 1)
        else:
            html = data_tag + '\n' + html
    except Exception as e:
        QAnimLogger.warn("StepAnswerInjector", f"Data tag insertion failed: {e}")

    try:
        if '</head>' in html:
            html = html.replace('</head>', _STEP_ANSWER_CSS + '\n</head>', 1)
    except Exception as e:
        QAnimLogger.warn("StepAnswerInjector", f"CSS insertion failed: {e}")

    try:
        body_match = re.search(r'<body[^>]*>', html, re.IGNORECASE)
        if body_match:
            ins = body_match.end()
            html = html[:ins] + '\n' + _STEP_ANSWER_DOM + html[ins:]
    except Exception as e:
        QAnimLogger.warn("StepAnswerInjector", f"DOM insertion failed: {e}")

    try:
        sa_script = '<script>\n' + STEP_ANSWER_JS_MODULE + '\n</script>'
        if '</body>' in html:
            html = html.replace('</body>', sa_script + '\n</body>', 1)
        else:
            html += '\n' + sa_script
    except Exception as e:
        QAnimLogger.warn("StepAnswerInjector", f"JS module insertion failed: {e}")

    QAnimLogger.ok("StepAnswerInjector", f"Injected Step by Step Answer panel ({len(solution_steps or [])} step(s))")
    return html


# ===========================================================================
#  MODULE 7 — Final Answer Panel System
# ===========================================================================

def _build_final_answer_data_tag(answer_targets, final_answer, key_insight):
    ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"]
    items = []
    for idx, t in enumerate(answer_targets or []):
        label  = str(t.get("label", "")).strip()
        value  = str(t.get("value", "")).strip()
        unit   = str(t.get("unit",  "")).strip()
        roman  = ROMAN[idx] if idx < len(ROMAN) else str(idx + 1)
        items.append({"roman": roman, "label": label, "value": value, "unit": unit})

    if not items and final_answer:
        _num_re = re.compile(
            r'([A-Za-z_][A-Za-z_0-9]*)\s*[=:]\s*([-+]?\d[\d.,]*(?:\s*[×x*]\s*10\^?[-+]?\d+)?)\s*([A-Za-z°%/²³·]+(?:\s*[A-Za-z°%/²³·]+)*)?',
            re.IGNORECASE)
        for i, m in enumerate(_num_re.finditer(final_answer)):
            roman = ROMAN[i] if i < len(ROMAN) else str(i + 1)
            items.append({
                "roman": roman,
                "label": m.group(1).strip(),
                "value": m.group(2).strip(),
                "unit":  (m.group(3) or "").strip(),
            })

    payload = {
        "items":       items,
        "raw_answer":  str(final_answer  or ""),
        "key_insight": str(key_insight   or ""),
    }
    return ('<script type="application/json" id="__final_answer_data__">\n'
            + json.dumps(payload, ensure_ascii=False, indent=2) + '\n</script>')


_FINAL_ANSWER_PANEL_DOM = """
<div id="fa-backdrop" aria-hidden="true"></div>
<aside id="fa-panel" role="dialog" aria-labelledby="fa-heading" aria-hidden="true">
  <div class="fa-header">
    <div class="fa-header-left">
      <div class="fa-icon-wrap">&#x2705;</div>
      <div>
        <div id="fa-heading" class="fa-title">Final Answer</div>
        <div class="fa-subtitle">Computed results for this question</div>
      </div>
    </div>
    <button id="fa-close" class="fa-close-btn" aria-label="Close">&#x2715;</button>
  </div>
  <div class="fa-body">
    <div id="fa-items-container" class="fa-items-container"></div>
    <div id="fa-insight-card" class="fa-insight-card">
      <div class="fa-insight-label">&#x1F4A1; Key Insight</div>
      <div id="fa-insight-text" class="fa-insight-text"></div>
    </div>
  </div>
</aside>
"""

_FINAL_ANSWER_PANEL_CSS = """
<style id="qanim-fa-styles">
#fa-backdrop { display:none; position:fixed; inset:0; z-index:8500;
  background:rgba(15,23,42,.42); backdrop-filter:blur(6px); opacity:0; transition:opacity .24s ease; }
#fa-backdrop.open { display:block; opacity:1; }
#fa-panel { display:flex; flex-direction:column; position:fixed; top:50%; left:50%;
  transform:translate(-50%,-48%) scale(.96); z-index:8600; width:min(520px,94vw);
  max-height:86vh; border-radius:18px; background:#fff; border:1px solid #e2e8f0;
  box-shadow:0 20px 60px rgba(80,60,140,.18),0 2px 8px rgba(0,0,0,.06);
  opacity:0; pointer-events:none;
  transition:opacity .28s ease,transform .28s cubic-bezier(.34,1.56,.64,1); overflow:hidden; }
#fa-panel.open { opacity:1; pointer-events:auto; transform:translate(-50%,-50%) scale(1); }
.fa-header { display:flex; align-items:center; justify-content:space-between;
  padding:18px 22px 14px; border-bottom:1px solid #f0f0f8; flex-shrink:0; background:#fff; }
.fa-header-left { display:flex; align-items:center; gap:13px; }
.fa-icon-wrap { width:40px; height:40px; border-radius:10px; background:#f0fdf4;
  display:flex; align-items:center; justify-content:center; font-size:20px; flex-shrink:0; }
.fa-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:17px; font-weight:800; color:#1a1a2e; }
.fa-subtitle { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:11px; color:#64748b; margin-top:2px; }
.fa-close-btn { width:34px; height:34px; border-radius:50%; border:1.5px solid #e8e8f0;
  background:#fafafa; color:#888; font-size:13px; display:flex; align-items:center; justify-content:center;
  cursor:pointer; transition:background .15s,color .15s,border-color .15s; flex-shrink:0; }
.fa-close-btn:hover { background:#fee2e2; color:#dc2626; border-color:#fca5a5; }
.fa-body { overflow-y:auto; flex:1; padding:18px 22px 24px; display:flex; flex-direction:column; gap:12px; }
.fa-items-container { display:flex; flex-direction:column; gap:10px; }
.fa-item { display:flex; align-items:flex-start; gap:14px; padding:14px 18px;
  border-radius:12px; background:#f8fafc; border:1px solid #e2e8f0; border-left:4px solid #16a34a;
  opacity:0; transform:translateY(10px); transition:opacity .30s ease,transform .30s ease; }
.fa-item.visible { opacity:1; transform:translateY(0); }
.fa-item-roman { min-width:32px; height:32px; border-radius:50%; background:#16a34a; color:#fff;
  font-size:12px; font-weight:800; display:flex; align-items:center; justify-content:center;
  flex-shrink:0; font-style:italic; box-shadow:0 2px 8px rgba(22,163,74,.30); }
.fa-item-body { flex:1; }
.fa-item-label { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12px; font-weight:700;
  color:#166534; text-transform:uppercase; letter-spacing:.6px; margin-bottom:5px; }
.fa-item-value { font-family:'Courier New',Courier,monospace; font-size:15px; font-weight:800; color:#1e293b;
  background:#f0fdf4; border:1px solid #bbf7d0; border-radius:8px; padding:6px 12px;
  display:inline-block; word-break:break-word; }
.fa-insight-card { border-radius:13px; padding:14px 18px; background:#fffbf0;
  border:1.5px solid #fde8a0; opacity:0; transition:opacity .35s ease; }
.fa-insight-card.visible { opacity:1; }
.fa-insight-label { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:10px; font-weight:800;
  text-transform:uppercase; letter-spacing:1.5px; color:#b45309; margin-bottom:7px; }
.fa-insight-text { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:13px; color:#78350f; line-height:1.72; }
</style>
"""

_FINAL_ANSWER_JS = r"""
(function initFinalAnswerSystem(){
  'use strict';
  var faOpen=false,_built=false;
  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
  function _loadData(){
    try{var tag=_el('__final_answer_data__');if(!tag)return{};return JSON.parse(tag.textContent)||{};}catch(e){return{};}
  }
  function _buildPanel(){
    if(_built)return;_built=true;
    var data=_loadData();var items=Array.isArray(data.items)?data.items:[];
    var container=_el('fa-items-container');if(!container)return;
    if(items.length===0&&data.raw_answer){
      container.innerHTML='<div class="fa-item visible"><div class="fa-item-roman">i</div><div class="fa-item-body"><div class="fa-item-label">Answer</div><div class="fa-item-value">'+_esc(data.raw_answer)+'</div></div></div>';
    }else{
      var html='';
      for(var i=0;i<items.length;i++){
        var it=items[i];var valueStr=_esc(it.value||'');
        if(it.unit&&it.value&&it.value.indexOf(it.unit)===-1){valueStr+=' '+_esc(it.unit);}
        html+='<div class="fa-item" id="fa-item-'+i+'"><div class="fa-item-roman">'+_esc(it.roman)+'</div><div class="fa-item-body"><div class="fa-item-label">'+_esc(it.label)+'</div><div class="fa-item-value">'+valueStr+'</div></div></div>';
      }
      container.innerHTML=html;
    }
    var insEl=_el('fa-insight-text'),insCard=_el('fa-insight-card');
    if(data.key_insight){if(insEl)insEl.textContent=data.key_insight;if(insCard)insCard.style.display='';}
    else{if(insCard)insCard.style.display='none';}
  }
  function _animateReveal(){
    var items=document.querySelectorAll('.fa-item');
    for(var i=0;i<items.length;i++){(function(el,idx){el.classList.remove('visible');el.style.transition='none';setTimeout(function(){el.style.transition='opacity .30s ease,transform .30s ease';el.classList.add('visible');},80+idx*110);})(items[i],i);}
    var insCard=_el('fa-insight-card');
    if(insCard){insCard.classList.remove('visible');setTimeout(function(){insCard.classList.add('visible');},80+items.length*110+120);}
  }
  function openFinalAnswer(){
    _buildPanel();
    var backdrop=_el('fa-backdrop'),panel=_el('fa-panel');if(!backdrop||!panel)return;
    backdrop.classList.add('open');panel.classList.add('open');panel.setAttribute('aria-hidden','false');faOpen=true;setTimeout(_animateReveal,80);
  }
  function closeFinalAnswer(){
    var backdrop=_el('fa-backdrop'),panel=_el('fa-panel');
    if(backdrop)backdrop.classList.remove('open');
    if(panel){panel.classList.remove('open');panel.setAttribute('aria-hidden','true');}
    faOpen=false;_built=false;
  }
  window.openFinalAnswer=openFinalAnswer;window.closeFinalAnswer=closeFinalAnswer;
  window.toggleFinalAnswer=function(){faOpen?closeFinalAnswer():openFinalAnswer();};
  _onReady(function(){
    function wireBtn(){var btn=document.getElementById('fa-ctrl-btn');
      if(btn){btn.removeAttribute('onclick');btn.addEventListener('click',function(e){e.stopPropagation();faOpen?closeFinalAnswer():openFinalAnswer();});}
      else{setTimeout(wireBtn,80);}
    }
    wireBtn();
    var closeBtn=_el('fa-close');if(closeBtn)closeBtn.addEventListener('click',function(e){e.stopPropagation();closeFinalAnswer();});
    var backdrop=_el('fa-backdrop');if(backdrop)backdrop.addEventListener('click',function(e){if(e.target===backdrop)closeFinalAnswer();});
    document.addEventListener('keydown',function(e){if(e.key==='Escape'&&faOpen)closeFinalAnswer();});
  });
})();
"""


def inject_final_answer_panel(html, answer_targets, final_answer, key_insight):
    html = re.sub(r'<script[^>]+id=["\']__sol_data__["\'][^>]*>.*?</script>', '', html, flags=re.DOTALL)
    html = re.sub(r'<script[^>]+id=["\']__final_answer_data__["\'][^>]*>.*?</script>', '', html, flags=re.DOTALL)
    html = re.sub(r'<style[^>]+id=["\']qanim-fa-styles["\'][^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)

    data_tag = _build_final_answer_data_tag(answer_targets, final_answer, key_insight)
    if '</head>' in html:
        html = html.replace('</head>', data_tag + '\n</head>', 1)
    else:
        html = data_tag + '\n' + html

    if '</head>' in html:
        html = html.replace('</head>', _FINAL_ANSWER_PANEL_CSS + '\n</head>', 1)

    body_match = re.search(r'<body[^>]*>', html, re.IGNORECASE)
    if body_match:
        ins = body_match.end()
        html = html[:ins] + '\n' + _FINAL_ANSWER_PANEL_DOM + html[ins:]

    fa_script = '<script>\n' + _FINAL_ANSWER_JS + '\n</script>'
    if '</body>' in html:
        html = html.replace('</body>', fa_script + '\n</body>', 1)
    else:
        html += '\n' + fa_script

    QAnimLogger.ok("FinalAnswer", f"Injected Final Answer panel ({len(answer_targets or [])} item(s))")
    return html


# ===========================================================================
#  MODULE 7.5 — GeminiSolutionGenerator
#  Replaces HaikuSolutionGenerator. Uses Gemini 3.1 Pro Preview.
# ===========================================================================

_SOLUTION_SYSTEM = """You are an expert engineering professor generating a step-by-step solution.

Return ONLY valid JSON with this structure:
{
  "steps": ["Step 1: ...", "Step 2: ...", ...],
  "final_answer": "complete answer with all values and units",
  "key_insight": "one memorable insight sentence"
}

RULES:
- steps: numbered steps following: Given -> Find -> Governing Law -> Formula -> Substitution -> Calculation -> Result
- final_answer: MUST include computed numerical values with units. Never leave empty.
- key_insight: one clear sentence capturing the core concept.
- Use simple, clear English. Short sentences.
- Do NOT use markdown. No backtick fences. Pure JSON only."""


class GeminiSolutionGenerator:

    _FALLBACK = {
        "steps": [
            "Step 1: Write down the given values from the question.",
            "Step 2: Identify what needs to be found.",
            "Step 3: Choose the correct governing formula.",
            "Step 4: Substitute values and solve step by step.",
        ],
        "final_answer": "Please re-generate for a detailed answer.",
        "key_insight":  "Always identify given values and the target before choosing a formula.",
        "raw": "",
    }

    @classmethod
    def generate(cls, question: str) -> dict:
        if _gemini_client is None:
            QAnimLogger.warn("GeminiSolution", "Gemini client not available — using fallback")
            return cls._FALLBACK

        QAnimLogger.info("GeminiSolution", f"Generating solution via {GEMINI_MODEL}...")
        user_prompt = f"Solve this question step by step:\n\nQUESTION: {question[:800]}\n\nReturn ONLY valid JSON."

        try:
            raw = cls._call_gemini(user_prompt, cls._solution_system_text(), max_tokens=4096)
            return cls._parse(raw)
        except Exception as e:
            QAnimLogger.warn("GeminiSolution", f"Generation failed: {e} — using fallback")
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
    def _parse(cls, raw: str) -> dict:
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE).strip()
        try:
            data = json.loads(raw)
            steps = data.get("steps", [])
            if not isinstance(steps, list):
                steps = []
            return {
                "steps":        steps,
                "final_answer": str(data.get("final_answer", "") or ""),
                "key_insight":  str(data.get("key_insight", "") or ""),
                "raw":          raw,
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
      <div class="ab-alldone-sub">Great work. Open <strong>Final Answer</strong> to review the complete result.</div>
    </div>
  </div>
</div>
</div>
"""

_ANSWER_BOX_JS = r"""
(function initAnswerBox(){
  'use strict';
  var abOpen=false,_targets=[],_currentIdx=0,_loaded=false;
  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _loadTargets(){
    if(_loaded)return;_loaded=true;
    try{var tag=_el('__answer_targets__');if(!tag){_useFallback();return;}
      var data=JSON.parse(tag.textContent)||{};_targets=Array.isArray(data.answer_targets)?data.answer_targets:[];}
    catch(e){_targets=[];}
    if(_targets.length===0)_useFallback();
  }
  function _useFallback(){
    try{var tag=_el('__final_answer_data__');if(!tag)return;
      var data=JSON.parse(tag.textContent)||{};
      _targets=[{label:'Final Answer',value:String(data.raw_answer||''),unit:'',insight:String(data.key_insight||'')}];}
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
        ab_script = '<script>\n' + _ANSWER_BOX_JS + '\n</script>'
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
        notes_script = '<script>\n' + _NOTES_JS + '\n</script>'
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
  <button class="qanim-ctrl-btn" id="stepans-ctrl-btn" title="View the full step-by-step solution">
    <span>&#x1FA9C;</span><span class="ctrl-label">Step by Step Answer</span>
  </button>
  <div class="qanim-ctrl-sep"></div>
  <button class="qanim-ctrl-btn" id="fa-ctrl-btn" title="View final answer">
    <span>&#x2705;</span><span class="ctrl-label">Final Answer</span>
  </button>
  <div class="qanim-ctrl-sep"></div>
  <button class="qanim-ctrl-btn" id="answerbox-ctrl-btn" title="Check your answer">
    <span>&#x270F;&#xFE0F;</span><span class="ctrl-label">Answer Box</span>
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
        anchor = '<span>&#x270F;&#xFE0F;</span><span class="ctrl-label">Answer Box</span>\n  </button>'
        if anchor in html:
            html = html.replace(anchor, anchor + '\n' + btn_html, 1)
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
        glossary_script = '<script>\n' + _GLOSSARY_JS + '\n</script>'
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
  if(document.readyState==="complete")initSC();else window.addEventListener("load",initSC);
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
#          "show_math": false,
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
      "blur_background": true,
      "show_math": false,
      "math_lines": []
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
1. steps: 3-5 steps minimum, always end with a math/solution step.
2. First step: establish the frame/ground/fixed structure.
3. Each subsequent step: introduce ONE new moving/key component.
4. Last step: freeze at the solution angle/state and show the math calculation box.
5. badges: use type "cyan", "orange", or "green".
6. svg_components: describe every physical component: frame, pivot, crank, rod, piston,
   gears, pulleys, beams, coils, etc. Position everything in an 850x478 coordinate space.
7. final_answer: MUST contain computed numerical answer with units. Never leave empty.
8. math_lines: for the last step, provide the actual calculation lines to show in the math box.
9. motion_type: accurately describe what this component does physically."""

_SCENE_ANALYZER_USER = """Analyse this question and produce the animation scene script:

QUESTION: {question}

Remember:
- Plan the step-by-step visual reveal carefully.
- Each step shows exactly ONE new component appearing with motion.
- Components are drawn one by one in the correct physical order.
- The final step freezes the mechanism at the solution state and shows the math.
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
                    "blur_background": False,
                    "show_math": False,
                    "math_lines": []
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
                    "blur_background": False,
                    "show_math": True,
                    "math_lines": ["Apply governing formula", "Substitute given values", "Compute the answer"]
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
                    "motion_description": "Math box appearing with the solution",
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
The output must match this exact structure:

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
      --accent-cyan: #0891b2;
      --accent-cyan-dim: #0e7490;
      --accent-orange: #ea8c00;
      --accent-green: #16a34a;
      --border-radius: 12px;
    }
    /* ... full light-theme styles ... */
  </style>
</head>
<body>
  <div class="dashboard">
    <!-- Question Banner -->
    <div class="question-banner">
      <div class="q-label">Question</div>
      <div class="q-text">[question text]</div>
    </div>
    <!-- SVG Canvas -->
    <div class="svg-container">
      <svg id="stage" viewBox="0 0 850 478" preserveAspectRatio="xMidYMid slice">
        <defs> ... gradients, filters, markers ... </defs>
        <!-- Base Canvas (grid pattern) -->
        <rect width="100%" height="100%" fill="url(#grid)" />
        <!-- Fixed background layer -->
        <g class="svg-layer" id="layer-frame"> ... </g>
        <!-- blur-shield rect -->
        <rect id="blur-shield" width="100%" height="100%" fill="#c7d2e0" opacity="0" pointer-events="none" />
        <!-- Component layers (one per physical component) -->
        <g class="svg-layer" id="layer-[component]" style="opacity:0"> ... </g>
        <!-- Overlay layers (labels/annotations per step) -->
        <g class="svg-layer" id="overlay-step0" style="opacity:0"> ... </g>
        ...
      </svg>
    </div>
    <!-- Control Panel -->
    <div class="control-panel">
      <div class="step-indicator" id="dots"> ... step-dots ... </div>
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
2. Soft light background: radial-gradient(circle at center, #f6f9fd 0%, #dbe4f0 100%).
3. Grid pattern overlay with very low opacity (0.05).
4. Each PHYSICAL COMPONENT gets its own <g class="svg-layer" id="layer-[name]"> group.
5. All component layers start with style="opacity:0" EXCEPT layer-frame (always visible).
6. A <rect id="blur-shield"> sits BETWEEN the frame layer and component layers.
   It starts with opacity="0" and gets set to 0.4-0.6 during focus steps to dim the background.
7. Overlay groups <g class="svg-layer" id="overlay-stepN"> hold labels, arrows, math boxes for each step. Start at opacity:0.
8. METALLIC GRADIENTS: use linearGradient for physical parts (steel, crank, rod, piston, gears).
9. GLOW FILTERS: feGaussianBlur glow for active/highlighted elements.
10. ARROW MARKERS: at least arrowCyan (#66fcf1), arrowOrange (#fca311), arrowGreen (#97c459).
11. The math box (last step overlay) must show actual calculation lines in monospace font.
12. ZERO text overlaps — compute positions carefully.

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
    label: "...",           // dot label
    // layer opacities
    blurOp: 0,              // blur-shield opacity (0 = no blur, 0.5 = blur background)
    overlays: ['overlay-step0'],  // which overlay groups to show
    freezing: false,        // true = snap motion to solution angle
    startAnim: false,       // true = start/resume animation
    title: "Step N: ...",
    badges: `<span class="badge badge-cyan">...</span>`,
    desc: "...",
    // component opacities: each layer has an explicit opacity entry
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
- Math box (last step): show actual formula, substitution, and final answer
- CENTERING: body must use { display:flex; flex-direction:column; align-items:center; justify-content:flex-start; padding:20px; } so the .dashboard div is horizontally centred on the page. .dashboard must have { width:100%; max-width:900px; margin:0 auto; }. Never float or absolutely-position the dashboard to the right.

═══ OUTPUT ═══
Return ONLY the complete <!DOCTYPE html>...</html> page as raw text.
No JSON wrapper. No markdown. No fences. Just the pure HTML."""

_ANIMATION_BUILDER_USER = """Generate the complete animation HTML page for this scene script.

ORIGINAL QUESTION: {question}

SCENE SCRIPT:
{scene_script}

CRITICAL REMINDERS:
1. Follow the reference output style EXACTLY (light, friendly dashboard, SVG canvas, control panel).
2. Draw components ONE BY ONE in the correct physical order — component layers appear step by step.
3. REAL PHYSICS: rotating crank uses real angle calculation, piston position uses kinematic formula.
4. The blur-shield dims the background when focusing on a new component.
5. Each component must visibly animate (rotate/translate/oscillate) when it first appears.
6. Labels and annotations appear AFTER the component is shown (in the overlay group for that step).
7. The LAST step snaps to the solution angle/state and shows the full math calculation box.
8. Use the accent colors from the scene script for each component.
9. Do NOT use const/let/arrow functions/backtick template literals.
10. The math box in the last step MUST show the actual numerical calculation from final_answer.

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
            math_lines = step.get("math_lines", [])
            show_math = step.get("show_math", False)
            blur = 0.5 if step.get("blur_background") else 0
            step_num = step.get("step_number", 1) - 1

            # Escape strings for JS
            label_js   = label.replace('"', '\\"')
            title_js   = title_s.replace('"', '\\"')
            desc_js    = desc.replace('"', '\\"').replace('\n', ' ')
            badges_js  = badges_html.replace('"', '\\"').replace('\n', '')

            steps_js_parts.append(
                "    {\n"
                f'      label: "{label_js}",\n'
                f'      blurOp: {blur},\n'
                f'      overlays: ["overlay-step{step_num}"],\n'
                f'      title: "{title_js}",\n'
                f'      badges: "{badges_js}",\n'
                f'      desc: "{desc_js}",\n'
                f'      showMath: {"true" if show_math else "false"}\n'
                "    }"
            )

        steps_js = "[\n" + ",\n".join(steps_js_parts) + "\n  ]"

        # Build overlay SVG groups
        overlay_groups = []
        for i, step in enumerate(steps):
            math_lines = step.get("math_lines", [])
            math_svg = ""
            if step.get("show_math") and math_lines:
                math_svg += f'<rect x="50" y="30" width="750" height="{30 + len(math_lines)*22}" rx="8" fill="rgba(15,15,19,0.9)" stroke="#97c459" stroke-width="1.5"/>'
                math_svg += f'<text x="425" y="55" fill="#97c459" font-size="13" font-weight="bold" text-anchor="middle">SOLUTION</text>'
                y_pos = 75
                for j, line in enumerate(math_lines):
                    line_esc = html_module.escape(str(line))
                    color = "#66fcf1" if j == len(math_lines) - 1 else "#ffffff"
                    math_svg += f'<text x="60" y="{y_pos}" fill="{color}" font-size="13" font-family="monospace">{line_esc}</text>'
                    y_pos += 22
            overlay_groups.append(
                f'<g class="svg-layer" id="overlay-step{i}" style="opacity:0">{math_svg}</g>'
            )
        overlays_html = "\n                ".join(overlay_groups)

        # Build dot elements
        dot_count = len(steps)
        dots_html = "\n                ".join(
            ['<div class="step-dot' + (' active' if i == 0 else '') + '"></div>'
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
            --accent-cyan: #0891b2;
            --accent-cyan-dim: #0e7490;
            --accent-orange: #ea8c00;
            --accent-green: #16a34a;
            --border-radius: 12px;
        }}
        * {{ box-sizing:border-box; margin:0; padding:0; font-family:'Segoe UI',system-ui,-apple-system,sans-serif; }}
        body {{ background-color:var(--bg-color); color:var(--text-main); display:flex; flex-direction:column; align-items:center; min-height:100vh; padding:20px; }}
        .dashboard {{ width:100%; max-width:850px; background:var(--panel-bg); border-radius:var(--border-radius); box-shadow:0 15px 35px rgba(30,64,175,0.12); overflow:hidden; border:1px solid #e2e8f0; }}
        .question-banner {{ padding:18px 24px; background:linear-gradient(135deg,#f8fafc 0%,#eef2f9 100%); border-bottom:1px solid #e2e8f0; display:flex; flex-direction:column; gap:6px; }}
        .q-label {{ font-size:12px; font-weight:700; color:var(--accent-cyan); text-transform:uppercase; letter-spacing:1px; }}
        .q-text {{ font-size:15px; color:#1e293b; line-height:1.4; }}
        .svg-container {{ width:100%; aspect-ratio:16/9; background:radial-gradient(circle at center,#f6f9fd 0%,#dbe4f0 100%); position:relative; overflow:hidden; }}
        svg {{ display:block; width:100%; height:100%; }}
        .svg-layer {{ transition:opacity 0.6s cubic-bezier(0.4,0,0.2,1); }}
        .control-panel {{ padding:24px; background:linear-gradient(180deg,#ffffff 0%,#f4f7fb 100%); border-top:1px solid #e2e8f0; }}
        .step-indicator {{ display:flex; align-items:center; gap:12px; margin-bottom:16px; }}
        .step-dot {{ width:10px; height:10px; border-radius:50%; background:#cbd5e1; transition:background 0.4s,transform 0.4s; }}
        .step-dot.active {{ background:var(--accent-cyan); box-shadow:0 0 10px var(--accent-cyan); transform:scale(1.2); }}
        .step-label {{ font-size:14px; color:#64748b; font-weight:500; letter-spacing:0.5px; text-transform:uppercase; }}
        .info-box {{ background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:16px; min-height:120px; display:flex; flex-direction:column; justify-content:center; }}
        .info-box h3 {{ color:#1e293b; margin-bottom:12px; font-size:16px; display:flex; align-items:center; gap:8px; }}
        .badges {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:10px; }}
        .badge {{ padding:4px 12px; border-radius:20px; font-size:13px; font-weight:600; display:flex; align-items:center; gap:6px; }}
        .badge-cyan {{ background:rgba(8,145,178,0.1); border:1px solid var(--accent-cyan-dim); color:var(--accent-cyan); }}
        .badge-orange {{ background:rgba(234,140,0,0.1); border:1px solid #c97600; color:var(--accent-orange); }}
        .badge-green {{ background:rgba(22,163,74,0.1); border:1px solid #15803d; color:var(--accent-green); }}
        .info-desc {{ font-size:14px; line-height:1.5; color:#64748b; }}
        .actions {{ display:flex; justify-content:flex-end; gap:12px; margin-top:20px; }}
        button {{ padding:10px 20px; border-radius:6px; font-size:14px; font-weight:600; cursor:pointer; transition:all 0.2s; border:none; outline:none; }}
        .btn-primary {{ background:var(--accent-cyan-dim); color:#fff; box-shadow:0 4px 10px rgba(14,116,144,0.3); }}
        .btn-primary:hover {{ background:var(--accent-cyan); color:#fff; box-shadow:0 6px 15px rgba(8,145,178,0.4); }}
        .btn-secondary {{ background:transparent; color:var(--text-main); border:1px solid #cbd5e1; }}
        .btn-secondary:hover {{ background:rgba(15,23,42,0.05); color:#1e293b; }}
    </style>
</head>
<body>
    <div class="dashboard">
        <div class="question-banner">
            <div class="q-label">&#x2753; Question</div>
            <div class="q-text">{q_esc}</div>
        </div>
        <div class="svg-container">
            <svg id="stage" viewBox="0 0 850 478" preserveAspectRatio="xMidYMid slice">
                <defs>
                    <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
                        <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#1e293b" stroke-width="0.5" stroke-opacity="0.05" />
                    </pattern>
                    <linearGradient id="steel" x1="0%" y1="0%" x2="100%" y2="100%">
                        <stop offset="0%" stop-color="#ffffff" />
                        <stop offset="30%" stop-color="#c0c0c0" />
                        <stop offset="70%" stop-color="#707070" />
                        <stop offset="100%" stop-color="#303030" />
                    </linearGradient>
                    <filter id="glowCyan" x="-50%" y="-50%" width="200%" height="200%">
                        <feGaussianBlur stdDeviation="4" result="blur" />
                        <feComposite in="SourceGraphic" in2="blur" operator="over" />
                    </filter>
                    <marker id="arrowCyan" orient="auto" markerWidth="6" markerHeight="6" refX="3" refY="3">
                        <path d="M 0 0 L 6 3 L 0 6 Z" fill="#66fcf1" />
                    </marker>
                    <marker id="arrowGreen" orient="auto" markerWidth="6" markerHeight="6" refX="3" refY="3">
                        <path d="M 0 0 L 6 3 L 0 6 Z" fill="#97c459" />
                    </marker>
                </defs>
                <rect width="100%" height="100%" fill="url(#grid)" />
                <!-- Frame layer — always visible -->
                <g class="svg-layer" id="layer-frame">
                    <line x1="100" y1="239" x2="750" y2="239" stroke="#cbd5e1" stroke-width="1" stroke-dasharray="8,6"/>
                    <text x="425" y="50" fill="#0e7490" font-size="20" font-weight="bold" text-anchor="middle" filter="url(#glowCyan)">{html_module.escape(script.get('title', title))}</text>
                </g>
                <!-- Blur shield -->
                <rect id="blur-shield" width="100%" height="100%" fill="#c7d2e0" opacity="0" class="svg-layer" pointer-events="none" />
                <!-- Step overlays -->
                {overlays_html}
            </svg>
        </div>
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

    scene_script, gemini_sol, glossary_result = await asyncio.gather(
        scene_script_task, solution_task, glossary_task
    )
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

    # Solution steps (reused as-is for the Step by Step Answer panel — no extra Gemini call)
    solution_steps = gemini_sol.get("steps", []) or scene_script.get("solution_steps", [])

    # ── Inject all panels (unchanged injection pipeline) ──
    html = animation_html
    html = inject_final_answer_panel(
        html=html,
        answer_targets=answer_targets,
        final_answer=final_answer,
        key_insight=key_insight,
    )
    html = inject_step_answer_panel(html, solution_steps)
    html = inject_notes_system(html)
    html = inject_answer_box_panel(html, answer_targets)
    html = inject_controls_bar(html)
    html = inject_glossary_panel(html, glossary_result.get("terms", []))
    html = inject_nav_patch_and_scene_desc(html)
    html = inject_step_controller(html)   # absolute last

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
        "render_status":   "ok",
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
        print("  OK  All panels injected (Find/StepAnswer/FinalAnswer/AnswerBox/Notes/Glossary)")
        print("  OK  Gemini 2.5 Pro for all stages")
