"""
q_animation.py  —  QAnim Question Animation Generator  v9.0
=============================================================
╔══════════════════════════════════════════════════════════════╗
║  v9.0 — MULTI-MODEL ROUTING + TEXT HOOK + 15-Q QUIZ         ║
╠══════════════════════════════════════════════════════════════╣
║  NEW IN v9.0 vs v8.0:                                       ║
║  ✅ HOOK_MODEL / QUIZ_MODEL = claude-haiku-4-5  (NEW)       ║
║  ✅ CONCEPT_MODEL / SOLUTION_MODEL = claude-sonnet-4-5      ║
║  ✅ Text-only cinematic hook (no SVG, glassmorphism)  (NEW) ║
║  ✅ 15 quiz questions with progressive difficulty   (NEW)   ║
║  ✅ Retry Quiz → host postMessage regeneration     (NEW)    ║
║  ✅ Brighter indigo theme (#2d2a6e / #3d3a91)     (NEW)    ║
║  ✅ All v8.0 features fully preserved                       ║
╚══════════════════════════════════════════════════════════════╝

PIPELINE (v9.0):
  Stage 0 — ToFind Extraction    (sync, no AI)
  Stage 1 — Hook Animation       (claude-haiku-4-5)  + HookGate + StepController
  Stage 2 — Concept Animation    (claude-sonnet-4-5) + StepController
  Stage 3 — Solution Animation   (claude-sonnet-4-5) + StepController
  Stage 4 — Quiz Generation      (claude-haiku-4-5)  + QuizGate + 15 questions

MODEL ROUTING:
  HOOK_MODEL     = "claude-haiku-4-5"   → Stage 1 hook generation
  QUIZ_MODEL     = "claude-haiku-4-5"   → Stage 4 quiz generation (15 Qs)
  CONCEPT_MODEL  = "claude-sonnet-4-5"  → Stage 2 concept SVG animation
  SOLUTION_MODEL = "claude-sonnet-4-5"  → Stage 3 solution SVG animation

STEP CONTROL ARCHITECTURE:
  Hook:     5 text scenes via #nextbtn/#prevbtn → Scene 5 shows "✦ Start Learning"
  Concept:  Scene navigation via #nextbtn/#prevbtn only (no auto-advance)
  Solution: Scene navigation via #nextbtn/#prevbtn only (no auto-advance)
  Quiz:     QuizGate → one question at a time → score screen → retry postMessage

RETRY QUIZ FLOW:
  1. Student clicks "Retry Quiz" → JS fires postMessage({type:'qanim:retryQuiz', ...})
  2. Host receives 'qanim:retryQuiz' → calls window.QAnimOnRetryQuiz(question, category)
  3. App layer calls QuizGenerator.generate() → fresh 15 Qs via claude-haiku-4-5
  4. App re-injects solution HTML with new quiz data
  5. Fallback: if no host handler, JS shuffles existing questions locally

RESULT DICT KEYS:
  hook_animation_code     — text-only cinematic hook HTML (+ HookGate + StepController)
  concept_animation_code  — concept teaching animation (+ StepController + Notes)
  animation_code          — solution animation (+ StepController + Notes + ToFind + Quiz btn)
  quiz_html               — standalone interactive quiz HTML (15 Qs + QuizGate)
  to_find                 — list of extracted target quantities
  solution_steps          — list of step strings
  final_answer            — complete answer string
  key_insight             — one-line conceptual insight
  title, category, engine_version, render_status
"""

import anthropic
import json
import re
import asyncio
import html as html_module
from typing import Optional

# ── Client + model routing ──────────────────────────────────────────────
# v9.0: Four dedicated model constants for precise cost/quality routing.
#   Haiku 4.5  → Hook + Quiz  (fast, lightweight generative tasks)
#   Sonnet 4.5 → Concept + Solution animations (complex, high-quality SVG)
client        = anthropic.Anthropic()

# ── Per-stage model constants ───────────────────────────────────────────
HOOK_MODEL     = "claude-haiku-4-5"        # Stage 1 — cinematic hook text
QUIZ_MODEL     = "claude-haiku-4-5"        # Stage 4 — quiz generation (15 Qs)
CONCEPT_MODEL  = "claude-sonnet-4-5"       # Stage 2 — concept SVG animation
SOLUTION_MODEL = "claude-sonnet-4-5"       # Stage 3 — solution SVG animation

MAX_TOK         = 12000   # Sonnet solution: realistic max (was 16000)
MAX_TOK_CONCEPT = 10000   # Sonnet concept:  realistic max (was 12000)
MAX_TOK_HOOK    =  8000   # Haiku hook:      realistic max (was 10000)
MAX_TOK_QUIZ    = 10000   # Haiku quiz:      realistic max (was 16000)


# ══════════════════════════════════════════════════════════════════════
#  MODULE 1 — QAnimLogger
# ══════════════════════════════════════════════════════════════════════

class QAnimLogger:
    """Centralized logger. All lifecycle events go through here."""

    PREFIX = "[QAnim v9]"

    @classmethod
    def info(cls, stage: str, msg: str):
        print(f"{cls.PREFIX} ℹ  [{stage}] {msg}")

    @classmethod
    def warn(cls, stage: str, msg: str):
        print(f"{cls.PREFIX} ⚠  [{stage}] {msg}")

    @classmethod
    def error(cls, stage: str, msg: str):
        print(f"{cls.PREFIX} ✖  [{stage}] {msg}")

    @classmethod
    def ok(cls, stage: str, msg: str):
        print(f"{cls.PREFIX} ✅ [{stage}] {msg}")


# ══════════════════════════════════════════════════════════════════════
#  MODULE 2 — GenerationValidator
# ══════════════════════════════════════════════════════════════════════

class ValidationError(Exception):
    pass

class GenerationValidator:
    """
    Validates AI-generated HTML before iframe injection.
    Raises ValidationError with a descriptive reason on failure.
    """

    DANGEROUS_PATTERNS = [
        (r'document\.write\s*\(', "document.write() is forbidden"),
        (r'<script[^>]+src\s*=', "External script src not allowed"),
        (r'javascript:\s*void', "javascript:void() link detected"),
        (r'on\w+\s*=\s*["\']?\s*eval\s*\(', "eval() in event handler"),
    ]

    REQUIRED_ELEMENTS = [
        ("<!DOCTYPE", "Missing DOCTYPE declaration"),
        ("<html",     "Missing <html> tag"),
        ("</html>",   "Missing closing </html> tag"),
        ("<body",     "Missing <body> tag"),
        ("</body>",   "Missing closing </body> tag"),
        ("<script",   "No script block — animation would be static"),
    ]

    SVG_REQUIRED = [
        ("<svg",   "No SVG element found"),
        ("</svg>", "SVG element not closed"),
    ]

    @classmethod
    def validate(cls, html: str, require_svg: bool = True) -> None:
        if not html or not html.strip():
            raise ValidationError("animation_code is empty")
        if len(html) < 500:
            raise ValidationError(
                f"animation_code suspiciously short ({len(html)} chars) — likely truncated"
            )
        for pattern, reason in cls.REQUIRED_ELEMENTS:
            if pattern not in html:
                raise ValidationError(reason)
        if require_svg:
            for pattern, reason in cls.SVG_REQUIRED:
                if pattern not in html:
                    raise ValidationError(reason)
        for pattern, reason in cls.DANGEROUS_PATTERNS:
            if re.search(pattern, html, re.IGNORECASE):
                QAnimLogger.warn("Validator", f"Dangerous pattern detected: {reason}")
        open_scripts  = len(re.findall(r'<script(?:\s[^>]*)?>',  html, re.IGNORECASE))
        close_scripts = len(re.findall(r'</script>',              html, re.IGNORECASE))
        if open_scripts != close_scripts:
            raise ValidationError(
                f"Unbalanced <script> tags: {open_scripts} open, {close_scripts} close"
            )
        open_svgs  = len(re.findall(r'<svg(?:\s[^>]*)?>',  html, re.IGNORECASE))
        close_svgs = len(re.findall(r'</svg>',              html, re.IGNORECASE))
        if open_svgs != close_svgs:
            raise ValidationError(
                f"Unbalanced <svg> tags: {open_svgs} open, {close_svgs} close"
            )
        QAnimLogger.ok("Validator", f"HTML passed validation ({len(html):,} chars)")


# ══════════════════════════════════════════════════════════════════════
#  MODULE 2.5 — ToFindExtractor
# ══════════════════════════════════════════════════════════════════════

class ToFindExtractor:
    """
    Parses an academic question and returns a deduplicated, student-friendly
    list of the quantities the student must find.
    Never raises — always returns a list.
    """

    _TRIGGER_PATTERNS: list = [
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
        (r'\bwhat\s+will\s+be\s+(?:the\s+)?(.+?)(?=\?|,|;|$)', 1),
        (r'\bwhat\s+would\s+be\s+(?:the\s+)?(.+?)(?=\?|,|;|$)', 1),
        (r'\bhow\s+(?:much|many)\s+(.+?)(?=\?|,|;|$)',           1),
        (r'\bprove\s+(?:that\s+)?(.+?)(?=\.|,|;|$)',             1),
        (r'\bshow\s+(?:that\s+)?(.+?)(?=\.|,|;|$)',              1),
        (r'\bexpress\s+(?:the\s+)?(.+?)\s+in\s+terms',          1),
    ]

    _NOISE_PREFIXES: list = [
        "the value of", "the values of", "value of",
        "the magnitude of", "magnitude of",
        "the amount of", "amount of",
        "the total", "the net", "the resultant", "the effective",
        "an expression for", "the expression for",
    ]

    _SPLIT_RE = re.compile(
        r'\s*,\s*|\s+and\s+|\s+also\s+|\s+as\s+well\s+as\s+|\s+along\s+with\s+',
        re.IGNORECASE
    )
    _TRAILING_RE = re.compile(
        r'\s+(?:if|when|given|assuming|where|such\s+that|for|in|at|'
        r'of\s+the\s+system|of\s+the\s+block|of\s+each)\s+.+$',
        re.IGNORECASE
    )
    _ARTICLE_RE     = re.compile(r'^(?:the|a|an)\s+', re.IGNORECASE)
    _TRIGGER_VERB_RE = re.compile(
        r'^(?:find|determine|calculate|evaluate|compute|obtain|'
        r'identify|estimate|derive|prove|show|express|solve\s+for)'
        r'\s+(?:the\s+|an?\s+)?', re.IGNORECASE
    )
    _MATH_VAR_RE = re.compile(r'^[A-Za-zα-ωΑ-Ω][0-9₀-₉]?$')

    MIN_LEN = 1
    MAX_LEN = 120

    @classmethod
    def extract(cls, question: str) -> list:
        if not question or not question.strip():
            QAnimLogger.warn("ToFindExtractor", "Empty question — returning []")
            return []
        try:
            raw      = cls._run_patterns(question)
            expanded = cls._split_conjunctions(raw)
            cleaned  = [cls._clean(t) for t in expanded]
            valid    = [t for t in cleaned
                        if t and (
                            (len(t) >= 3 and len(t) <= cls.MAX_LEN)
                            or cls._MATH_VAR_RE.match(t)
                        )]
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
    def _run_patterns(cls, question: str) -> list:
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
    def _split_conjunctions(cls, targets: list) -> list:
        result = []
        for t in targets:
            parts = cls._SPLIT_RE.split(t)
            result.extend(p.strip() for p in parts if p.strip())
        return result

    @classmethod
    def _clean(cls, target: str) -> str:
        t = target.strip().rstrip(".,;:?!")
        sorted_noise = sorted(cls._NOISE_PREFIXES, key=len, reverse=True)
        for noise in sorted_noise:
            if t.lower().startswith(noise):
                t = t[len(noise):].strip()
                break
        t = cls._TRAILING_RE.sub("", t).strip()
        t = cls._TRIGGER_VERB_RE.sub("", t).strip()
        t = cls._ARTICLE_RE.sub("", t).strip()
        return t.rstrip(".,;:?!")

    @classmethod
    def _deduplicate(cls, targets: list) -> list:
        seen, result = set(), []
        for t in targets:
            key = t.lower().strip()
            if key and key not in seen:
                seen.add(key)
                result.append(t)
        return result

    @classmethod
    def _cap(cls, s: str) -> str:
        return s[0].upper() + s[1:] if s else s

    @classmethod
    def _fallback(cls, question: str) -> list:
        try:
            sentences = re.split(r'[.!?]', question.strip())
            for s in reversed(sentences):
                s = s.strip()
                if 4 <= len(s) <= 80:
                    return [cls._cap(s)]
            return []
        except Exception:
            return []


# ══════════════════════════════════════════════════════════════════════
#  MODULE 3 — HtmlSanitizer
# ══════════════════════════════════════════════════════════════════════

class HtmlSanitizer:
    """Cleans AI-generated HTML. Does NOT validate."""

    @classmethod
    def sanitize(cls, html: str) -> str:
        html = html.replace('\ufeff', '')
        end = html.rfind('</html>')
        if end != -1:
            html = html[:end + 7]
        html = re.sub(r'document\.write\s*\([^)]*\)\s*;?', '', html, flags=re.IGNORECASE)
        html = re.sub(
            r'<script[^>]+src\s*=\s*["\'][^"\']*["\'][^>]*>\s*</script>',
            '', html, flags=re.IGNORECASE | re.DOTALL
        )
        html = cls._wrap_scripts_in_error_boundary(html)
        html = re.sub(
            r'<svg(?![^>]*xmlns)',
            '<svg xmlns="http://www.w3.org/2000/svg"',
            html, flags=re.IGNORECASE
        )
        html = html.replace('\x00', '')
        QAnimLogger.ok("Sanitizer", "HTML sanitized")
        return html

    @classmethod
    def _wrap_scripts_in_error_boundary(cls, html: str) -> str:
        def wrap_script(match: re.Match) -> str:
            tag   = match.group(1)
            body  = match.group(2)
            close = match.group(3)
            if re.search(r'type\s*=\s*["\']application/', tag, re.IGNORECASE):
                return match.group(0)
            stripped = body.strip()
            if stripped.startswith('try {') or stripped.startswith('try{'):
                return match.group(0)
            if len(stripped) < 20:
                return match.group(0)
            wrapped_body = (
                "\n/* ── QAnim Error Boundary ── */\n"
                "try {\n" + body +
                "\n} catch (_qanim_err) {\n"
                "  console.error('[QAnim ErrorBoundary]', _qanim_err);\n"
                "  (function() {\n"
                "    var fb = document.getElementById('qanim-error-fallback');\n"
                "    if (!fb) return;\n"
                "    fb.style.display = 'flex';\n"
                "    var msg = fb.querySelector('.qanim-err-msg');\n"
                "    if (msg) msg.textContent = String(_qanim_err);\n"
                "  })();\n"
                "}\n"
            )
            return f"{tag}{wrapped_body}{close}"

        pattern = r'(<script(?:\s[^>]*)?>)(.*?)(</script>)'
        return re.sub(pattern, wrap_script, html, flags=re.DOTALL | re.IGNORECASE)


# ══════════════════════════════════════════════════════════════════════
#  MODULE 4 — RecoveryEngine
# ══════════════════════════════════════════════════════════════════════

class RecoveryEngine:
    """Graceful fallback HTML when generation or validation fails."""

    @staticmethod
    def fallback_html(question: str, reason: str) -> str:
        q_safe      = html_module.escape(question[:120])
        reason_safe = html_module.escape(reason[:300])
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  *, *::before, *::after {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{
    width:100%; height:100%; overflow:hidden;
    background:linear-gradient(135deg,#2d2a6e,#3d3a91,#2a2760);
    font-family:-apple-system,'Segoe UI',Arial,sans-serif;
    display:flex; align-items:center; justify-content:center;
  }}
  .card {{
    background:rgba(255,255,255,0.08);
    border:1px solid rgba(139,92,246,0.45);
    border-radius:20px;
    box-shadow:0 20px 60px rgba(0,0,0,0.30),0 0 0 1px rgba(139,92,246,0.12);
    padding:36px 40px; max-width:520px; text-align:center;
    backdrop-filter:blur(20px);
  }}
  .icon {{ font-size:40px; margin-bottom:16px; }}
  .title {{ font-size:17px; font-weight:800; color:#f1f5f9; margin-bottom:10px; }}
  .reason {{
    font-size:11px; color:#94a3b8; background:rgba(255,255,255,0.05);
    border-radius:10px; padding:10px 14px; margin:12px 0;
    border:1px solid rgba(255,255,255,0.09); text-align:left;
    line-height:1.6; font-family:monospace;
  }}
  .question {{ font-size:12px; color:#64748b; line-height:1.6; margin-top:10px; font-style:italic; }}
  .retry-hint {{
    margin-top:18px; font-size:11px; font-weight:700;
    letter-spacing:1.5px; text-transform:uppercase; color:#a78bfa;
  }}
</style>
</head>
<body>
  <div class="card">
    <div class="icon">⚠️</div>
    <div class="title">Animation Could Not Render</div>
    <div class="reason">{reason_safe}</div>
    <div class="question">"{q_safe}"</div>
    <div class="retry-hint">Please regenerate the animation</div>
  </div>
</body>
</html>"""

    @staticmethod
    def partial_html(question: str, animation_code: str) -> str:
        has_doctype = '<!DOCTYPE' in animation_code or '<html' in animation_code
        if has_doctype:
            return animation_code
        q_safe = html_module.escape(question[:120])
        return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8">
<style>
  html,body{{margin:0;padding:0;width:100%;height:100%;
    display:flex;align-items:center;justify-content:center;
    background:linear-gradient(135deg,#2d2a6e,#3d3a91);font-family:-apple-system,sans-serif}}
</style>
</head>
<body>
<div style="font-size:11px;color:#6d5fac;position:fixed;top:8px;left:0;right:0;text-align:center">
  {q_safe}
</div>
{animation_code}
</body></html>"""


# ══════════════════════════════════════════════════════════════════════
#  MODULE 5 — IframeLifecycleManager JS constant
# ══════════════════════════════════════════════════════════════════════

IFRAME_RUNTIME_JS = r"""
/* ═══════════════════════════════════════════════════════════
   QAnim IframeLifecycleManager v7 — srcdoc-based safe render
   ═══════════════════════════════════════════════════════════ */
(function() {
  'use strict';

  var _iframe      = null;
  var _renderQueue = [];
  var _rendering   = false;
  var _currentHtml = '';

  var Log = {
    info:  function(m) { console.log('[QAnim ILM] ℹ  ' + m); },
    warn:  function(m) { console.warn('[QAnim ILM] ⚠  ' + m); },
    error: function(m) { console.error('[QAnim ILM] ✖  ' + m); },
    ok:    function(m) { console.log('[QAnim ILM] ✅ ' + m); }
  };

  function _getIframe() {
    if (_iframe && document.body.contains(_iframe)) return _iframe;
    var existing = document.getElementById('qanim-frame');
    if (existing) { _iframe = existing; return _iframe; }
    var f = document.createElement('iframe');
    f.id = 'qanim-frame';
    f.setAttribute('sandbox', 'allow-scripts');
    f.style.cssText = 'width:100%;height:100%;border:none;display:block;background:transparent';
    f.setAttribute('title', 'QAnim Animation');
    document.body.appendChild(f);
    _iframe = f;
    Log.ok('Created fresh iframe #qanim-frame');
    return _iframe;
  }

  function _resetIframe() {
    Log.warn('Resetting iframe...');
    if (_iframe && document.body.contains(_iframe)) {
      _iframe.removeAttribute('srcdoc');
      _iframe.src = 'about:blank';
      document.body.removeChild(_iframe);
    }
    _iframe = null;
    _currentHtml = '';
    return _getIframe();
  }

  function _injectSrcdoc(iframe, html) {
    try {
      iframe.removeAttribute('srcdoc');
      iframe.src = 'about:blank';
      requestAnimationFrame(function() {
        try {
          iframe.srcdoc = html;
          Log.ok('srcdoc injected (' + html.length + ' chars)');
        } catch(e) {
          Log.error('srcdoc assignment failed: ' + e);
          _onRenderError('srcdoc: ' + e.message);
        }
      });
    } catch(e) {
      Log.error('_injectSrcdoc outer: ' + e);
      _onRenderError('_injectSrcdoc: ' + e.message);
    }
  }

  function _processQueue() {
    if (_rendering || _renderQueue.length === 0) return;
    _rendering = true;
    var task = _renderQueue.shift();
    Log.info('Processing render task. Queue: ' + _renderQueue.length);
    var iframe = _getIframe();
    var timeoutId, done = false;

    function cleanup() {
      if (done) return;
      done = true;
      clearTimeout(timeoutId);
      iframe.onload = null;
      iframe.onerror = null;
    }

    iframe.onload = function() {
      cleanup();
      Log.ok('iframe loaded');
      _currentHtml = task.html;
      _rendering = false;
      if (task.onSuccess) task.onSuccess();
      _processQueue();
    };
    iframe.onerror = function(e) {
      cleanup();
      Log.error('iframe onerror: ' + (e && e.message || 'unknown'));
      _rendering = false;
      if (task.onError) task.onError('iframe error');
      _processQueue();
    };
    timeoutId = setTimeout(function() {
      if (done) return;
      cleanup();
      Log.warn('iframe load timeout — continuing');
      _currentHtml = task.html;
      _rendering = false;
      if (task.onSuccess) task.onSuccess();
      _processQueue();
    }, 8000);

    _injectSrcdoc(iframe, task.html);
  }

  function _onRenderError(reason) {
    Log.error('Render error: ' + reason);
    _rendering = false;
    _processQueue();
  }

  window.QAnimILM = {
    render: function(html, onSuccess, onError) {
      if (!html || html.length < 100) {
        Log.error('render() called with empty/tiny html');
        if (onError) onError('empty html');
        return;
      }
      _renderQueue = [];
      _renderQueue.push({ html: html, onSuccess: onSuccess, onError: onError });
      _processQueue();
    },
    reset: function() {
      _renderQueue = [];
      _rendering = false;
      _resetIframe();
      Log.ok('ILM reset complete');
    },
    getCurrentHtml: function() { return _currentHtml; },
    isRendering: function() { return _rendering; }
  };

  /* Hook->Concept transition bridge via postMessage */
  window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'qanim:hookComplete') {
      Log.ok('Received hookComplete signal');
      if (typeof window.QAnimOnHookComplete === 'function') {
        window.QAnimOnHookComplete();
      } else {
        Log.warn('QAnimOnHookComplete not defined on host');
      }
    }

    /* ── CHANGE 4: Quiz retry regeneration bridge ──────────────────
       The inline quiz iframe fires 'qanim:retryQuiz' when the student
       clicks "Retry Quiz". The host is responsible for calling
       window.QAnimOnRetryQuiz(question, category) — which the
       application layer must implement to call QuizGenerator.generate()
       and re-inject a fresh solution HTML with new quiz data.          */
    if (e.data && e.data.type === 'qanim:retryQuiz') {
      Log.ok('Received retryQuiz signal — question=' + (e.data.question || '').slice(0, 60));
      if (typeof window.QAnimOnRetryQuiz === 'function') {
        window.QAnimOnRetryQuiz(e.data.question || '', e.data.category || '');
      } else {
        Log.warn('QAnimOnRetryQuiz not defined on host — retry ignored');
      }
    }
  });

  Log.ok('IframeLifecycleManager v7 initialized');
})();
"""


# ══════════════════════════════════════════════════════════════════════
#  MODULE 6 — ErrorFallbackInjector
# ══════════════════════════════════════════════════════════════════════

ERROR_BOUNDARY_HTML = """
<!-- QAnim Error Fallback — hidden unless needed -->
<div id="qanim-error-fallback" style="
  display:none; position:fixed; inset:0; z-index:9999;
  background:rgba(45,42,110,0.92); backdrop-filter:blur(12px);
  align-items:center; justify-content:center;
">
  <div style="
    background:rgba(61,58,145,0.97); border-radius:20px; padding:32px 36px;
    max-width:440px; text-align:center;
    border:1px solid rgba(139,92,246,0.45);
    box-shadow:0 40px 100px rgba(0,0,0,0.36);
  ">
    <div style="font-size:36px;margin-bottom:14px">⚠️</div>
    <div style="font-size:15px;font-weight:800;color:#f1f5f9;margin-bottom:8px">Animation Error</div>
    <div class="qanim-err-msg" style="
      font-size:11px;color:#94a3b8;background:rgba(255,255,255,0.04);
      border-radius:10px;padding:10px 14px;margin:12px 0;
      border:1px solid rgba(255,255,255,0.08);font-family:monospace;
      text-align:left;line-height:1.6;word-break:break-all;
    ">Unknown error</div>
    <button onclick="document.getElementById('qanim-error-fallback').style.display='none'"
      style="margin-top:14px;padding:8px 22px;border-radius:50px;border:none;
        background:linear-gradient(135deg,#7c3aed,#db2777);color:#fff;
        font-weight:700;font-size:12px;cursor:pointer;">Dismiss</button>
  </div>
</div>
"""

QANIM_INNER_LOGGER_JS = """
<script>
window.QLog = {
  info:  function(m) { console.log('[QAnim Inner] ℹ  ' + m); },
  warn:  function(m) { console.warn('[QAnim Inner] ⚠  ' + m); },
  error: function(m) { console.error('[QAnim Inner] ✖  ' + m); }
};
window.addEventListener('error', function(e) {
  console.error('[QAnim GlobalError]', e.message, 'at', e.filename + ':' + e.lineno);
  var fb = document.getElementById('qanim-error-fallback');
  if (fb) {
    fb.style.display = 'flex';
    var msg = fb.querySelector('.qanim-err-msg');
    if (msg) msg.textContent = e.message + ' (line ' + e.lineno + ')';
  }
});
window.addEventListener('unhandledrejection', function(e) {
  console.error('[QAnim UnhandledPromise]', e.reason);
});
</script>
"""

def inject_infrastructure(html: str) -> str:
    """Injects QAnim error fallback UI + inner logger + scroll-enable override."""
    html = re.sub(
        r'(<body[^>]*>)',
        r'\1\n' + ERROR_BOUNDARY_HTML,
        html, count=1, flags=re.IGNORECASE
    )
    first_script = re.search(
        r'<script(?:\s[^>]*)?>(?!.*type\s*=\s*["\']application/json)',
        html, re.IGNORECASE
    )
    if first_script:
        pos = first_script.start()
        html = html[:pos] + QANIM_INNER_LOGGER_JS + '\n' + html[pos:]
    else:
        html = html.replace('</body>', QANIM_INNER_LOGGER_JS + '\n</body>', 1)

    # ── Scroll-enable override ──────────────────────────────────────
    # AI often sets `overflow:hidden` on html/body which prevents the page
    # from scrolling to the quiz below the animation. Override it here.
    _scroll_fix = (
        '\n<style id="qanim-scroll-fix">\n'
        'html, body {\n'
        '  overflow-x: hidden !important;\n'
        '  overflow-y: auto !important;\n'
        '  height: auto !important;\n'
        '  min-height: 100vh;\n'
        '}\n'
        '#container, [id="container"] {\n'
        '  padding-bottom: 60px;\n'
        '}\n'
        '</style>\n'
    )
    if '</head>' in html:
        html = html.replace('</head>', _scroll_fix + '</head>', 1)
    else:
        html = _scroll_fix + html

    QAnimLogger.ok("Infrastructure", "Error fallback + inner logger + scroll-fix injected")
    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 6.5 — ToFind Injection System  (from v6.0, fully preserved)
# ══════════════════════════════════════════════════════════════════════

def _build_to_find_data_tag(targets: list) -> str:
    payload = {"targets": [str(t) for t in (targets or [])]}
    return (
        '<script type="application/json" id="__tofind_data__">\n'
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + '\n</script>'
    )


_TO_FIND_DOM = """
<div id="tofind-backdrop" aria-hidden="true"></div>
<aside id="tofind-panel" role="dialog" aria-labelledby="tofind-heading" aria-hidden="true">
  <div class="tf-glow-ring"></div>
  <div class="tf-header">
    <div class="tf-header-left">
      <div class="tf-icon-wrap">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
          <circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
          <path d="M16.5 16.5L21 21" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
        </svg>
      </div>
      <span id="tofind-heading" class="tf-title">To Find</span>
    </div>
    <button id="tofind-close" class="tf-close-btn" aria-label="Close To Find panel">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M18 6L6 18M6 6l12 12" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>
      </svg>
    </button>
  </div>
  <p class="tf-subtitle">What this question is asking you to determine:</p>
  <div id="tofind-items-container" class="tf-items-container"></div>
  <div class="tf-footer"><span class="tf-badge">QAnim v7 · Target Identifier</span></div>
</aside>
"""

_TO_FIND_CSS = """
<style id="qanim-tofind-styles">
#tofind-backdrop {
  display:none; position:fixed; inset:0; z-index:8000;
  background:rgba(10,10,26,0.75); backdrop-filter:blur(10px);
  opacity:0; transition:opacity 0.28s ease;
}
#tofind-backdrop.open { display:block; opacity:1; }
#tofind-panel {
  display:flex; flex-direction:column; position:fixed;
  top:50%; left:50%; transform:translate(-50%,-48%) scale(0.96);
  z-index:8100; width:min(480px,92vw); max-height:82vh;
  border-radius:24px; padding:28px 28px 22px; box-sizing:border-box; overflow:hidden;
  background:linear-gradient(145deg,rgba(49,46,129,0.95),rgba(30,27,75,0.98));
  border:1px solid rgba(120,80,255,0.45);
  box-shadow:0 0 0 1px rgba(120,80,255,0.18),0 24px 60px rgba(0,0,0,0.4),0 4px 20px rgba(120,80,255,0.25);
  opacity:0; pointer-events:none;
  transition:opacity 0.30s ease,transform 0.30s cubic-bezier(0.34,1.56,0.64,1);
}
#tofind-panel.open { opacity:1; pointer-events:auto; transform:translate(-50%,-50%) scale(1); }
.tf-glow-ring {
  position:absolute; top:-60px; right:-60px; width:200px; height:200px;
  border-radius:50%; background:radial-gradient(circle,rgba(124,58,237,0.28) 0%,transparent 70%);
  pointer-events:none;
}
.tf-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; flex-shrink:0; }
.tf-header-left { display:flex; align-items:center; gap:11px; }
.tf-icon-wrap {
  width:36px; height:36px; border-radius:12px;
  background:linear-gradient(135deg,#7c3aed,#4f46e5);
  display:flex; align-items:center; justify-content:center; color:#fff; flex-shrink:0;
  box-shadow:0 4px 14px rgba(124,58,237,0.45);
}
.tf-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:17px; font-weight:800; color:#f1f5f9; }
.tf-close-btn {
  width:32px; height:32px; border-radius:50%; border:1px solid rgba(255,255,255,0.12);
  background:rgba(255,255,255,0.07); color:#94a3b8;
  display:flex; align-items:center; justify-content:center; cursor:pointer;
  transition:background 0.18s,color 0.18s,transform 0.18s;
}
.tf-close-btn:hover { background:rgba(255,255,255,0.14); color:#f1f5f9; transform:rotate(90deg); }
.tf-subtitle { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:11.5px; color:#64748b; margin:0 0 18px; flex-shrink:0; }
.tf-items-container {
  display:flex; flex-direction:column; gap:10px; overflow-y:auto; flex:1 1 auto; padding-right:4px;
  scrollbar-width:thin; scrollbar-color:rgba(124,58,237,0.4) transparent;
}
.tofind-item {
  display:flex; align-items:flex-start; gap:13px; padding:14px 16px; border-radius:14px;
  background:rgba(255,255,255,0.07); border:1px solid rgba(124,58,237,0.25);
  opacity:0; transform:translateX(-16px); transition:background 0.18s,border-color 0.18s;
}
.tofind-item:hover { background:rgba(124,58,237,0.18); border-color:rgba(124,58,237,0.50); }
.tofind-check {
  width:22px; height:22px; border-radius:50%;
  background:linear-gradient(135deg,#7c3aed,#4f46e5); color:#fff; font-size:12px;
  display:flex; align-items:center; justify-content:center; flex-shrink:0; margin-top:1px;
  box-shadow:0 2px 8px rgba(124,58,237,0.40);
}
.tofind-text { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:14px; font-weight:600; color:#e2e8f0; line-height:1.5; }
.tofind-empty { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:13px; color:#475569; text-align:center; padding:24px 0; font-style:italic; }
.tf-footer { margin-top:18px; display:flex; justify-content:center; flex-shrink:0; }
.tf-badge { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:10px; font-weight:700; letter-spacing:1.4px; text-transform:uppercase; color:#334155; background:rgba(255,255,255,0.04); border:1px solid rgba(255,255,255,0.07); border-radius:50px; padding:4px 12px; }
#tofind-btn {
  display:inline-flex; align-items:center; gap:7px; padding:9px 18px; border-radius:50px;
  border:1.5px solid rgba(124,58,237,0.55); background:rgba(124,58,237,0.12); color:#a78bfa;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12px; font-weight:700;
  cursor:pointer; transition:background 0.22s,border-color 0.22s,color 0.22s,transform 0.18s,box-shadow 0.22s;
  backdrop-filter:blur(6px);
}
#tofind-btn:hover { background:rgba(124,58,237,0.28); border-color:rgba(124,58,237,0.85); color:#ede9fe; transform:translateY(-2px); box-shadow:0 8px 24px rgba(124,58,237,0.35); }
.tf-btn-icon { width:16px; height:16px; opacity:0.85; }
@media (max-width:540px) { #tofind-panel { width:96vw; padding:22px 18px 18px; border-radius:20px; } }
</style>
"""

TO_FIND_JS_MODULE = r"""
(function initToFindSystem() {
  'use strict';
  var toFindOpen = false, _panelBuilt = false;
  function _onReady(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else setTimeout(fn, 0);
  }
  function _el(id) { return document.getElementById(id); }
  function _loadTargets() {
    try {
      var tag = _el('__tofind_data__');
      if (!tag) return [];
      var data = JSON.parse(tag.textContent) || {};
      return Array.isArray(data.targets) ? data.targets : [];
    } catch(e) { console.error('[QAnim ToFind] parse error:', e); return []; }
  }
  function _escape(text) {
    if (!text) return '';
    return String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function _buildPanel(targets) {
    if (_panelBuilt) return;
    _panelBuilt = true;
    try {
      var container = _el('tofind-items-container');
      if (!container) return;
      if (!targets || targets.length === 0) {
        container.innerHTML = '<div class="tofind-empty">No specific targets detected.<br><span style="font-size:11px;opacity:0.6;">Read the question carefully.</span></div>';
        return;
      }
      var html = '';
      for (var i = 0; i < targets.length; i++) {
        html += '<div class="tofind-item" id="tofind-item-'+i+'"><div class="tofind-check">&#10003;</div><div class="tofind-text">'+_escape(targets[i])+'</div></div>';
      }
      container.innerHTML = html;
    } catch(e) { console.error('[QAnim ToFind] _buildPanel error:', e); }
  }
  function _animateReveal() {
    try {
      var items = document.querySelectorAll('.tofind-item');
      for (var i = 0; i < items.length; i++) {
        (function(el, idx) {
          el.style.opacity = '0'; el.style.transform = 'translateX(-18px)'; el.style.transition = 'none';
          setTimeout(function() {
            el.style.transition = 'opacity 0.32s ease,transform 0.32s cubic-bezier(0.34,1.56,0.64,1)';
            el.style.opacity = '1'; el.style.transform = 'translateX(0)';
          }, 90 + idx * 95);
        })(items[i], i);
      }
    } catch(e) { console.error('[QAnim ToFind] _animateReveal error:', e); }
  }
  function openToFind() {
    try {
      var backdrop = _el('tofind-backdrop'), panel = _el('tofind-panel');
      if (!backdrop || !panel) return;
      var targets = _loadTargets();
      _buildPanel(targets);
      backdrop.classList.add('open');
      panel.classList.add('open');
      panel.setAttribute('aria-hidden','false');
      toFindOpen = true;
      setTimeout(_animateReveal, 150);
    } catch(err) { console.error('[QAnim ToFind] openToFind crashed:', err); }
  }
  function closeToFind() {
    try {
      var backdrop = _el('tofind-backdrop'), panel = _el('tofind-panel');
      if (backdrop) backdrop.classList.remove('open');
      if (panel) { panel.classList.remove('open'); panel.setAttribute('aria-hidden','true'); }
      toFindOpen = false;
    } catch(err) { console.error('[QAnim ToFind] closeToFind crashed:', err); }
  }
  window.openToFind   = openToFind;
  window.closeToFind  = closeToFind;
  window.toggleToFind = function() { toFindOpen ? closeToFind() : openToFind(); };
  _onReady(function() {
    try {
      var tfBtn = _el('tofind-btn') || document.querySelector('[data-tofind-btn]');
      if (tfBtn) { tfBtn.removeAttribute('onclick'); tfBtn.addEventListener('click', function(e) { e.stopPropagation(); openToFind(); }); }
      var closeBtn = _el('tofind-close');
      if (closeBtn) { closeBtn.removeAttribute('onclick'); closeBtn.addEventListener('click', closeToFind); }
      var backdrop = _el('tofind-backdrop');
      if (backdrop) { backdrop.removeAttribute('onclick'); backdrop.addEventListener('click', closeToFind); }
      document.addEventListener('keydown', function(e) { if (e.key === 'Escape' && toFindOpen) closeToFind(); });
      console.log('[QAnim ToFind] System initialized ✓');
    } catch(e) { console.error('[QAnim ToFind] Init error:', e); }
  });
})();
"""

_TO_FIND_BUTTON_HTML = """<button id="tofind-btn" data-tofind-btn aria-haspopup="dialog">
  <svg class="tf-btn-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
    <circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
    <path d="M16.5 16.5L21 21" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
  </svg>
  To Find
</button>"""


def inject_to_find_system(html: str, targets: list) -> str:
    """Injects the complete 'To Find' feature into animation HTML."""
    html = re.sub(
        r'<script[^>]+id=["\']__tofind_data__["\'][^>]*>.*?</script>',
        '', html, flags=re.DOTALL
    )
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
        else:
            html = _TO_FIND_CSS + '\n' + html
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
        html = html.replace(
            '</body>',
            '\n' + _TO_FIND_BUTTON_HTML + '\n</body>', 1
        )
    except Exception as e:
        QAnimLogger.warn("ToFindInjector", f"Button insertion failed: {e}")

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


# ══════════════════════════════════════════════════════════════════════
#  MODULE 7 — Solution System (preserved from v6.0)
# ══════════════════════════════════════════════════════════════════════

def _build_solution_data_tag(steps: list, answer: str, insight: str) -> str:
    payload = {
        "steps":   [str(s) for s in (steps or [])],
        "answer":  str(answer  or ""),
        "insight": str(insight or ""),
    }
    return (
        '<script type="application/json" id="__sol_data__">\n'
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + '\n</script>'
    )

SOLUTION_JS_MODULE = r"""
(function initSolutionSystem() {
  'use strict';
  var solutionOpen = false, _solBuilt = false;
  function _onReady(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else setTimeout(fn, 0);
  }
  function _el(id) { return document.getElementById(id); }
  function _loadData() {
    try {
      var tag = _el('__sol_data__');
      if (!tag) return {};
      return JSON.parse(tag.textContent) || {};
    } catch(e) { console.error('[QAnim Sol] parse error:', e); return {}; }
  }
  function _escape(text) {
    if (!text) return '';
    return String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  function _highlight(text) {
    var safe = _escape(text);
    return safe.replace(/([A-Za-z_]\w*\s*=\s*[^<&,;.]+)/g,'<span class="formula">$1</span>');
  }
  function _buildSteps(data) {
    if (_solBuilt) return;
    _solBuilt = true;
    try {
      var container = _el('sol-steps-container');
      if (!container) return;
      var steps = Array.isArray(data.steps) ? data.steps : [];
      var html = '';
      if (steps.length === 0) {
        html = '<div class="sol-step visible"><div class="sol-step-num">!</div><div class="sol-step-text">Solution steps unavailable.</div></div>';
      } else {
        for (var i = 0; i < steps.length; i++) {
          html += '<div class="sol-step" id="sol-step-'+i+'"><div class="sol-step-num">'+(i+1)+'</div><div class="sol-step-text">'+_highlight(steps[i])+'</div></div>';
        }
      }
      container.innerHTML = html;
      var ansEl = _el('sol-answer-text'), insEl = _el('sol-insight-text');
      if (ansEl && data.answer)  ansEl.innerHTML = _highlight(data.answer);
      if (insEl && data.insight) insEl.innerHTML = _highlight(data.insight);
    } catch(e) { console.error('[QAnim Sol] _buildSteps error:', e); }
  }
  function _animateReveal() {
    try {
      var stepEls = document.querySelectorAll('.sol-step');
      var delay = 60;
      for (var i = 0; i < stepEls.length; i++) {
        (function(el, idx) {
          el.classList.remove('visible');
          setTimeout(function() { el.classList.add('visible'); }, delay + idx * 90);
        })(stepEls[i], i);
      }
      var base = delay + stepEls.length * 90;
      var ac = _el('sol-answer-card'), ic = _el('sol-insight-card');
      if (ac) { ac.classList.remove('visible'); setTimeout(function(){ ac.classList.add('visible'); }, base); }
      if (ic) { ic.classList.remove('visible'); setTimeout(function(){ ic.classList.add('visible'); }, base+120); }
    } catch(e) { console.error('[QAnim Sol] _animateReveal error:', e); }
  }
  function openSolution() {
    try {
      var backdrop = _el('sol-backdrop'), panel = _el('sol-panel');
      if (!backdrop || !panel) return;
      var data = _loadData();
      _buildSteps(data);
      backdrop.classList.add('open');
      panel.classList.add('open');
      panel.setAttribute('aria-hidden','false');
      solutionOpen = true;
      _animateReveal();
    } catch(err) { console.error('[QAnim Sol] openSolution crashed:', err); }
  }
  function closeSolution() {
    try {
      var backdrop = _el('sol-backdrop'), panel = _el('sol-panel');
      if (backdrop) backdrop.classList.remove('open');
      if (panel) { panel.classList.remove('open'); panel.setAttribute('aria-hidden','true'); }
      solutionOpen = false;
    } catch(err) { console.error('[QAnim Sol] closeSolution crashed:', err); }
  }
  window.openSolution   = openSolution;
  window.closeSolution  = closeSolution;
  window.toggleSolution = function() { solutionOpen ? closeSolution() : openSolution(); };
  _onReady(function() {
    try {
      var closeBtn = _el('sol-close');
      if (closeBtn) { closeBtn.removeAttribute('onclick'); closeBtn.addEventListener('click', closeSolution); }
      var backdrop = _el('sol-backdrop');
      if (backdrop) { backdrop.removeAttribute('onclick'); backdrop.addEventListener('click', closeSolution); }
      document.addEventListener('keydown', function(e) { if (e.key === 'Escape' && solutionOpen) closeSolution(); });
      console.log('[QAnim Sol] Solution system initialized ✓');
    } catch(e) { console.error('[QAnim Sol] Init error:', e); }
  });
})();
"""

def inject_solution_system(html: str, steps: list, answer: str, insight: str) -> str:
    """Injects solution data tag and isolated solution JS module."""
    html = re.sub(
        r'<script[^>]+id=["\']__sol_data__["\'][^>]*>.*?</script>',
        '', html, flags=re.DOTALL
    )
    data_tag = _build_solution_data_tag(steps, answer, insight)
    if '</head>' in html:
        html = html.replace('</head>', data_tag + '\n</head>', 1)
    else:
        html = data_tag + '\n' + html

    for pat in [
        r'var SOL_STEPS\s*=\s*\(function\(\).*?\}\)\(\);',
        r"var SOL_ANSWER\s*=\s*'[^']*';",
        r"var SOL_INSIGHT\s*=\s*'[^']*';",
    ]:
        html = re.sub(pat, '', html, flags=re.DOTALL)

    sol_script = '<script>\n' + SOLUTION_JS_MODULE + '\n</script>'
    if '</body>' in html:
        html = html.replace('</body>', sol_script + '\n</body>', 1)
    else:
        html += '\n' + sol_script

    # Remove any AI-generated #solbtn ("View Solution") from the output
    html = re.sub(
        r'<button[^>]+id=["\']solbtn["\'][^>]*>.*?</button>',
        '', html, flags=re.DOTALL | re.IGNORECASE
    )
    QAnimLogger.ok("Solution", f"Injected {len(steps)} steps — #solbtn removed")
    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 7.5 — Notes System  (NEW in v7.0)
#  Floating whiteboard + text notes panel injected into any animation.
#  Features: canvas drawing, undo/redo, colors, localStorage persistence,
#  drag-to-move, minimize, export canvas as PNG.
# ══════════════════════════════════════════════════════════════════════

_NOTES_CSS = """
<style id="qanim-notes-styles">
/* ═══════════════════════════════════════════════════════════
   QAnim Notes System v7 — Floating Glassmorphism Whiteboard
   ═══════════════════════════════════════════════════════════ */

/* ── Floating trigger button ── */
#qanim-notes-btn {
  position: fixed;
  top: 16px;
  right: 16px;
  z-index: 7900;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 14px 8px 10px;
  border-radius: 50px;
  border: 1.5px solid rgba(251, 191, 36, 0.5);
  background: rgba(251, 191, 36, 0.12);
  color: #fbbf24;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  box-shadow: 0 4px 20px rgba(251,191,36,0.18);
  transition: background 0.2s, border-color 0.2s, transform 0.18s, box-shadow 0.2s;
  user-select: none;
}
#qanim-notes-btn:hover {
  background: rgba(251,191,36,0.24);
  border-color: rgba(251,191,36,0.9);
  transform: translateY(-2px);
  box-shadow: 0 8px 28px rgba(251,191,36,0.30);
}
#qanim-notes-btn .nb-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: #fbbf24;
  box-shadow: 0 0 6px rgba(251,191,36,0.8);
  animation: nb-pulse 2s ease-in-out infinite;
}
@keyframes nb-pulse {
  0%,100% { transform:scale(1); opacity:1; }
  50%      { transform:scale(1.4); opacity:0.7; }
}

/* ── Backdrop ── */
#qanim-notes-backdrop {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 7950;
  background: rgba(30,27,75,0.45);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  opacity: 0;
  transition: opacity 0.25s ease;
}
#qanim-notes-backdrop.open { display: block; opacity: 1; }

/* ── Notes Panel ── */
#qanim-notes-panel {
  position: fixed;
  top: 60px;
  right: 20px;
  z-index: 8000;
  width: 380px;
  border-radius: 20px;
  overflow: hidden;
  background: linear-gradient(145deg, rgba(49,46,129,0.97), rgba(30,27,75,0.98));
  border: 1px solid rgba(251,191,36,0.35);
  box-shadow:
    0 0 0 1px rgba(251,191,36,0.15),
    0 24px 60px rgba(0,0,0,0.4),
    0 4px 20px rgba(251,191,36,0.18);
  opacity: 0;
  transform: translateY(-8px) scale(0.97);
  pointer-events: none;
  transition: opacity 0.28s ease, transform 0.28s cubic-bezier(0.34,1.56,0.64,1);
  display: flex;
  flex-direction: column;
  max-height: 85vh;
  min-width: 320px;
  min-height: 420px;
  resize: both;
  user-select: none;
}
#qanim-notes-panel.open {
  opacity: 1;
  transform: translateY(0) scale(1);
  pointer-events: auto;
}
#qanim-notes-panel.minimized { max-height: 52px; min-height: 52px; overflow: hidden; resize: none; }

/* ── Panel Header (drag handle) ── */
#qanim-notes-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  background: rgba(251,191,36,0.07);
  border-bottom: 1px solid rgba(251,191,36,0.15);
  cursor: grab;
  flex-shrink: 0;
}
#qanim-notes-header:active { cursor: grabbing; }
.notes-header-left { display:flex; align-items:center; gap:8px; }
.notes-header-icon {
  width: 28px; height: 28px; border-radius: 8px;
  background: linear-gradient(135deg,#f59e0b,#d97706);
  display:flex; align-items:center; justify-content:center;
  color:#fff; font-size:14px;
  box-shadow: 0 2px 8px rgba(245,158,11,0.4);
}
.notes-header-title {
  font-family: -apple-system,'Segoe UI',Arial,sans-serif;
  font-size: 13px; font-weight: 800; color: #fde68a;
  letter-spacing: -0.2px;
}
.notes-header-sub {
  font-family: -apple-system,'Segoe UI',Arial,sans-serif;
  font-size: 10px; color: #92400e; margin-top: 1px;
}
.notes-header-actions { display:flex; align-items:center; gap:6px; }
.notes-hdr-btn {
  width: 26px; height: 26px; border-radius: 6px;
  border: 1px solid rgba(255,255,255,0.1);
  background: rgba(255,255,255,0.05);
  color: #94a3b8; font-size: 12px;
  display:flex; align-items:center; justify-content:center;
  cursor:pointer; transition:background 0.15s, color 0.15s;
}
.notes-hdr-btn:hover { background:rgba(255,255,255,0.12); color:#f1f5f9; }

/* ── Tabs ── */
#qanim-notes-tabs {
  display:flex; border-bottom: 1px solid rgba(255,255,255,0.06);
  flex-shrink:0;
}
.notes-tab {
  flex:1; padding:8px 0; text-align:center;
  font-family: -apple-system,'Segoe UI',Arial,sans-serif;
  font-size:11px; font-weight:700; color:#64748b;
  cursor:pointer; border-bottom:2px solid transparent;
  transition:color 0.15s, border-color 0.15s;
  letter-spacing:0.5px; text-transform:uppercase;
}
.notes-tab.active { color:#fbbf24; border-bottom-color:#fbbf24; }

/* ── Canvas Toolbar ── */
#qanim-canvas-toolbar {
  display:flex; align-items:center; gap:6px; padding:8px 12px;
  background:rgba(255,255,255,0.02); border-bottom:1px solid rgba(255,255,255,0.05);
  flex-shrink:0; flex-wrap:wrap;
}
.canvas-tool-btn {
  padding:4px 10px; border-radius:6px; border:1px solid rgba(255,255,255,0.1);
  background:rgba(255,255,255,0.04); color:#94a3b8; font-size:11px; font-weight:700;
  cursor:pointer; transition:background 0.15s,color 0.15s,border-color 0.15s;
}
.canvas-tool-btn:hover { background:rgba(255,255,255,0.10); color:#e2e8f0; }
.canvas-tool-btn.active { background:rgba(251,191,36,0.2); border-color:rgba(251,191,36,0.6); color:#fbbf24; }
.color-dot {
  width:18px; height:18px; border-radius:50%; cursor:pointer; border:2px solid transparent;
  transition:transform 0.15s, border-color 0.15s; flex-shrink:0;
}
.color-dot:hover { transform:scale(1.2); }
.color-dot.selected { border-color:#fff; transform:scale(1.15); }
.size-btn {
  width:22px; height:22px; border-radius:50%; border:1px solid rgba(255,255,255,0.15);
  background:rgba(255,255,255,0.04); color:#94a3b8; font-size:10px; font-weight:700;
  display:flex; align-items:center; justify-content:center; cursor:pointer;
  transition:background 0.15s,color 0.15s;
}
.size-btn.active { background:rgba(251,191,36,0.2); color:#fbbf24; border-color:rgba(251,191,36,0.5); }
.tool-sep { width:1px; height:20px; background:rgba(255,255,255,0.08); flex-shrink:0; }

/* ── Canvas container ── */
#qanim-canvas-wrap {
  flex:1 1 auto; position:relative; overflow:hidden; min-height:200px;
}
#qanim-draw-canvas {
  display:block; width:100%; height:100%; cursor:crosshair;
  background:#1e1b4b;
  touch-action: none;
}

/* ── Text notes area ── */
#qanim-text-pane {
  display:none; flex-direction:column; flex:1 1 auto; overflow:hidden;
}
#qanim-notes-textarea {
  flex:1 1 auto; width:100%; min-height:200px; resize:none; box-sizing:border-box;
  background:#1e1b4b; border:none; outline:none;
  color:#e2e8f0; font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:13px; line-height:1.7; padding:14px 16px;
  scrollbar-width:thin; scrollbar-color:rgba(251,191,36,0.3) transparent;
}
#qanim-notes-textarea::placeholder { color:#334155; }

/* ── Footer ── */
#qanim-notes-footer {
  display:flex; align-items:center; justify-content:space-between; padding:8px 12px;
  border-top:1px solid rgba(255,255,255,0.06); flex-shrink:0;
  background:rgba(255,255,255,0.02);
}
.notes-status { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:10px; color:#334155; }
.notes-action-btn {
  padding:4px 12px; border-radius:6px; border:1px solid rgba(251,191,36,0.3);
  background:rgba(251,191,36,0.08); color:#f59e0b; font-size:10px; font-weight:700;
  cursor:pointer; transition:background 0.15s,border-color 0.15s;
}
.notes-action-btn:hover { background:rgba(251,191,36,0.18); border-color:rgba(251,191,36,0.6); }

@media (max-width:480px) {
  #qanim-notes-panel { width:calc(100vw - 24px); right:12px; top:50px; min-width:0; }
}
</style>
"""

_NOTES_DOM = """
<!-- QAnim Notes System v7 -->
<button id="qanim-notes-btn" aria-label="Open notes whiteboard">
  <div class="nb-dot"></div>
  📝 Notes
</button>
<div id="qanim-notes-backdrop" aria-hidden="true"></div>
<div id="qanim-notes-panel" role="dialog" aria-label="Notes whiteboard" aria-hidden="true">
  <!-- Header / drag handle -->
  <div id="qanim-notes-header">
    <div class="notes-header-left">
      <div class="notes-header-icon">✏️</div>
      <div>
        <div class="notes-header-title">My Notes</div>
        <div class="notes-header-sub" id="notes-save-status">Auto-saved</div>
      </div>
    </div>
    <div class="notes-header-actions">
      <button class="notes-hdr-btn" id="notes-export-btn" title="Export canvas as PNG">↓</button>
      <button class="notes-hdr-btn" id="notes-minimize-btn" title="Minimize">—</button>
      <button class="notes-hdr-btn" id="notes-close-btn" title="Close">✕</button>
    </div>
  </div>

  <!-- Tabs -->
  <div id="qanim-notes-tabs">
    <div class="notes-tab active" data-tab="canvas">🖊 Draw</div>
    <div class="notes-tab" data-tab="text">📄 Text</div>
  </div>

  <!-- Canvas toolbar -->
  <div id="qanim-canvas-toolbar">
    <button class="canvas-tool-btn active" data-tool="pen">Pen</button>
    <button class="canvas-tool-btn" data-tool="eraser">Eraser</button>
    <div class="tool-sep"></div>
    <div class="color-dot selected" style="background:#a78bfa;" data-color="#a78bfa"></div>
    <div class="color-dot" style="background:#fbbf24;" data-color="#fbbf24"></div>
    <div class="color-dot" style="background:#34d399;" data-color="#34d399"></div>
    <div class="color-dot" style="background:#f87171;" data-color="#f87171"></div>
    <div class="color-dot" style="background:#60a5fa;" data-color="#60a5fa"></div>
    <div class="color-dot" style="background:#ffffff;" data-color="#ffffff"></div>
    <div class="tool-sep"></div>
    <button class="size-btn" data-size="2" title="Thin">S</button>
    <button class="size-btn active" data-size="4" title="Medium">M</button>
    <button class="size-btn" data-size="8" title="Thick">L</button>
    <div class="tool-sep"></div>
    <button class="canvas-tool-btn" id="notes-undo-btn" title="Undo (Ctrl+Z)">↩</button>
    <button class="canvas-tool-btn" id="notes-redo-btn" title="Redo (Ctrl+Y)">↪</button>
    <button class="canvas-tool-btn" id="notes-clear-btn" title="Clear canvas">🗑</button>
  </div>

  <!-- Canvas -->
  <div id="qanim-canvas-wrap">
    <canvas id="qanim-draw-canvas"></canvas>
  </div>

  <!-- Text pane -->
  <div id="qanim-text-pane">
    <textarea id="qanim-notes-textarea" placeholder="Type your notes here...
Use this space for formulas, observations, and key ideas." spellcheck="false"></textarea>
  </div>

  <!-- Footer -->
  <div id="qanim-notes-footer">
    <span class="notes-status" id="notes-char-count">0 chars</span>
    <button class="notes-action-btn" id="notes-export-text-btn">Export Text</button>
  </div>
</div>
"""

_NOTES_JS = r"""
(function initNotesSystem() {
  'use strict';

  /* ── State ── */
  var isOpen       = false;
  var isMinimized  = false;
  var isDragging   = false;
  var isDrawing    = false;
  var currentTool  = 'pen';
  var currentColor = '#a78bfa';
  var currentSize  = 4;
  var currentTab   = 'canvas';
  var undoStack    = [];
  var redoStack    = [];
  var MAX_UNDO     = 40;
  var dragOffX     = 0;
  var dragOffY     = 0;
  var autoSaveTimer = null;
  var ctx          = null;
  var canvas       = null;

  /* ── Storage key (per question) ── */
  var questionEl = document.querySelector('.qtext');
  var questionText = questionEl ? questionEl.textContent.trim() : 'qanim_default';
  var storageKey = 'qanim_notes_' + _hashStr(questionText);

  function _hashStr(s) {
    var h = 5381;
    for (var i = 0; i < Math.min(s.length, 200); i++) {
      h = ((h << 5) + h) ^ s.charCodeAt(i);
      h = h >>> 0;
    }
    return h.toString(36);
  }

  function _el(id) { return document.getElementById(id); }

  /* ── Storage helpers ── */
  function _storage() {
    try { return window.localStorage; } catch(e) {
      /* fallback: in-memory store */
      if (!window._qanimInMemStore) window._qanimInMemStore = {};
      return { getItem: function(k) { return window._qanimInMemStore[k]||null; }, setItem: function(k,v){ window._qanimInMemStore[k]=v; } };
    }
  }

  function _saveNotes() {
    try {
      var canvasData = canvas ? canvas.toDataURL() : '';
      var textData   = _el('qanim-notes-textarea') ? _el('qanim-notes-textarea').value : '';
      var payload    = JSON.stringify({ canvas: canvasData, text: textData, ts: Date.now() });
      _storage().setItem(storageKey, payload);
      var stat = _el('notes-save-status');
      if (stat) {
        stat.textContent = 'Saved ' + new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
      }
    } catch(e) { console.warn('[QAnim Notes] Save failed:', e); }
  }

  function _loadNotes() {
    try {
      var raw = _storage().getItem(storageKey);
      if (!raw) return;
      var payload = JSON.parse(raw);
      /* Load text */
      var ta = _el('qanim-notes-textarea');
      if (ta && payload.text) ta.value = payload.text;
      /* Load canvas */
      if (canvas && payload.canvas && payload.canvas.startsWith('data:')) {
        var img = new Image();
        img.onload = function() {
          ctx.drawImage(img, 0, 0);
          _saveUndoState(); /* initial state after load */
        };
        img.src = payload.canvas;
      }
    } catch(e) { console.warn('[QAnim Notes] Load failed:', e); }
  }

  function _scheduleAutoSave() {
    if (autoSaveTimer) clearTimeout(autoSaveTimer);
    autoSaveTimer = setTimeout(_saveNotes, 1500);
  }

  /* ── Canvas setup ── */
  function _initCanvas() {
    canvas = _el('qanim-draw-canvas');
    if (!canvas) return;
    ctx = canvas.getContext('2d');
    _resizeCanvas();
    ctx.lineCap    = 'round';
    ctx.lineJoin   = 'round';
    ctx.strokeStyle = currentColor;
    ctx.lineWidth   = currentSize;
    _loadNotes();
  }

  function _resizeCanvas() {
    if (!canvas) return;
    var wrap = _el('qanim-canvas-wrap');
    if (!wrap) return;
    var w = wrap.clientWidth  || 360;
    var h = wrap.clientHeight || 260;
    var imageData = null;
    if (ctx && canvas.width > 0 && canvas.height > 0) {
      try { imageData = ctx.getImageData(0, 0, canvas.width, canvas.height); } catch(e) {}
    }
    canvas.width  = w;
    canvas.height = h;
    ctx.lineCap    = 'round';
    ctx.lineJoin   = 'round';
    ctx.strokeStyle = currentColor;
    ctx.lineWidth   = currentSize;
    if (imageData) {
      try { ctx.putImageData(imageData, 0, 0); } catch(e) {}
    }
  }

  /* ── Undo/Redo ── */
  function _saveUndoState() {
    if (!canvas) return;
    if (undoStack.length >= MAX_UNDO) undoStack.shift();
    undoStack.push(canvas.toDataURL());
    redoStack = [];
  }

  function _undo() {
    if (!canvas || undoStack.length === 0) return;
    redoStack.push(canvas.toDataURL());
    var prev = undoStack.pop();
    if (prev) {
      var img = new Image();
      img.onload = function() { ctx.clearRect(0,0,canvas.width,canvas.height); ctx.drawImage(img,0,0); _scheduleAutoSave(); };
      img.src = prev;
    } else {
      ctx.clearRect(0,0,canvas.width,canvas.height);
      _scheduleAutoSave();
    }
  }

  function _redo() {
    if (!canvas || redoStack.length === 0) return;
    undoStack.push(canvas.toDataURL());
    var next = redoStack.pop();
    var img = new Image();
    img.onload = function() { ctx.clearRect(0,0,canvas.width,canvas.height); ctx.drawImage(img,0,0); _scheduleAutoSave(); };
    img.src = next;
  }

  /* ── Drawing ── */
  function _getPos(e, cvs) {
    var rect = cvs.getBoundingClientRect();
    var scaleX = cvs.width  / rect.width;
    var scaleY = cvs.height / rect.height;
    var clientX = e.touches ? e.touches[0].clientX : e.clientX;
    var clientY = e.touches ? e.touches[0].clientY : e.clientY;
    return { x: (clientX - rect.left) * scaleX, y: (clientY - rect.top)  * scaleY };
  }

  function _startDraw(e) {
    if (!canvas || currentTab !== 'canvas') return;
    e.preventDefault();
    _saveUndoState();
    isDrawing = true;
    var pos = _getPos(e, canvas);
    ctx.beginPath();
    ctx.moveTo(pos.x, pos.y);
    if (currentTool === 'eraser') {
      ctx.globalCompositeOperation = 'destination-out';
      ctx.lineWidth = currentSize * 4;
    } else {
      ctx.globalCompositeOperation = 'source-over';
      ctx.strokeStyle = currentColor;
      ctx.lineWidth   = currentSize;
    }
  }

  function _draw(e) {
    if (!isDrawing || !canvas) return;
    e.preventDefault();
    var pos = _getPos(e, canvas);
    ctx.lineTo(pos.x, pos.y);
    ctx.stroke();
  }

  function _endDraw(e) {
    if (!isDrawing) return;
    isDrawing = false;
    if (ctx) ctx.globalCompositeOperation = 'source-over';
    _scheduleAutoSave();
  }

  /* ── Panel open / close / toggle ── */
  function openNotes() {
    var panel    = _el('qanim-notes-panel');
    var backdrop = _el('qanim-notes-backdrop');
    if (!panel) return;
    panel.classList.add('open');
    panel.setAttribute('aria-hidden', 'false');
    if (backdrop) backdrop.classList.add('open');
    isOpen = true;
    setTimeout(function() {
      _resizeCanvas();
    }, 50);
  }

  function closeNotes() {
    var panel    = _el('qanim-notes-panel');
    var backdrop = _el('qanim-notes-backdrop');
    if (panel) { panel.classList.remove('open'); panel.setAttribute('aria-hidden','true'); }
    if (backdrop) backdrop.classList.remove('open');
    isOpen = false;
    _saveNotes();
  }

  function minimizeNotes() {
    var panel = _el('qanim-notes-panel');
    if (!panel) return;
    isMinimized = !isMinimized;
    if (isMinimized) {
      panel.classList.add('minimized');
      var btn = _el('notes-minimize-btn');
      if (btn) btn.textContent = '□';
    } else {
      panel.classList.remove('minimized');
      var btn2 = _el('notes-minimize-btn');
      if (btn2) btn2.textContent = '—';
      setTimeout(_resizeCanvas, 50);
    }
  }

  /* ── Tab switching ── */
  function _switchTab(tabName) {
    currentTab = tabName;
    var tabs = document.querySelectorAll('.notes-tab');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].classList.toggle('active', tabs[i].dataset.tab === tabName);
    }
    var canvasToolbar = _el('qanim-canvas-toolbar');
    var canvasWrap    = _el('qanim-canvas-wrap');
    var textPane      = _el('qanim-text-pane');
    if (canvasToolbar) canvasToolbar.style.display = tabName === 'canvas' ? 'flex' : 'none';
    if (canvasWrap)    canvasWrap.style.display    = tabName === 'canvas' ? 'block' : 'none';
    if (textPane)      textPane.style.display      = tabName === 'text'   ? 'flex'  : 'none';
    if (tabName === 'canvas') setTimeout(_resizeCanvas, 30);
  }

  /* ── Dragging ── */
  function _startDrag(e) {
    if (e.target !== _el('qanim-notes-header') && !e.target.closest || (e.target.closest && !e.target.closest('#qanim-notes-header'))) return;
    if (e.target.classList && (e.target.classList.contains('notes-hdr-btn') || e.target.closest('.notes-hdr-btn'))) return;
    isDragging = true;
    var panel = _el('qanim-notes-panel');
    if (!panel) return;
    var rect = panel.getBoundingClientRect();
    dragOffX = (e.clientX || (e.touches && e.touches[0].clientX) || 0) - rect.left;
    dragOffY = (e.clientY || (e.touches && e.touches[0].clientY) || 0) - rect.top;
    panel.style.transition = 'none';
  }

  function _drag(e) {
    if (!isDragging) return;
    var panel = _el('qanim-notes-panel');
    if (!panel) return;
    var clientX = e.clientX || (e.touches && e.touches[0].clientX) || 0;
    var clientY = e.clientY || (e.touches && e.touches[0].clientY) || 0;
    var newLeft = clientX - dragOffX;
    var newTop  = clientY - dragOffY;
    newLeft = Math.max(0, Math.min(window.innerWidth - 80, newLeft));
    newTop  = Math.max(0, Math.min(window.innerHeight - 60, newTop));
    panel.style.left  = newLeft + 'px';
    panel.style.top   = newTop  + 'px';
    panel.style.right = 'auto';
    e.preventDefault();
  }

  function _endDrag() {
    if (!isDragging) return;
    isDragging = false;
    var panel = _el('qanim-notes-panel');
    if (panel) panel.style.transition = '';
  }

  /* ── Export ── */
  function _exportCanvas() {
    if (!canvas) return;
    var link = document.createElement('a');
    link.download = 'qanim_notes_drawing.png';
    link.href = canvas.toDataURL('image/png');
    link.click();
  }

  function _exportText() {
    var ta = _el('qanim-notes-textarea');
    if (!ta || !ta.value) return;
    var blob = new Blob([ta.value], { type: 'text/plain' });
    var link = document.createElement('a');
    link.download = 'qanim_notes.txt';
    link.href = URL.createObjectURL(blob);
    link.click();
  }

  /* ── DOM Binding ── */
  function _onReady(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else setTimeout(fn, 0);
  }

  _onReady(function() {
    try {
      /* Main toggle button */
      var notesBtn = _el('qanim-notes-btn');
      if (notesBtn) notesBtn.addEventListener('click', function() { isOpen ? closeNotes() : openNotes(); });

      /* Close + minimize */
      var closeBtn = _el('notes-close-btn');
      if (closeBtn) closeBtn.addEventListener('click', closeNotes);
      var minBtn = _el('notes-minimize-btn');
      if (minBtn) minBtn.addEventListener('click', function(e) { e.stopPropagation(); minimizeNotes(); });

      /* Tab switching */
      var tabs = document.querySelectorAll('.notes-tab');
      for (var i = 0; i < tabs.length; i++) {
        tabs[i].addEventListener('click', function() { _switchTab(this.dataset.tab); });
      }

      /* Tool buttons */
      var toolBtns = document.querySelectorAll('.canvas-tool-btn[data-tool]');
      for (var t = 0; t < toolBtns.length; t++) {
        toolBtns[t].addEventListener('click', function() {
          currentTool = this.dataset.tool;
          document.querySelectorAll('.canvas-tool-btn[data-tool]').forEach(function(b) { b.classList.remove('active'); });
          this.classList.add('active');
        });
      }

      /* Color dots */
      var colorDots = document.querySelectorAll('.color-dot');
      for (var c = 0; c < colorDots.length; c++) {
        colorDots[c].addEventListener('click', function() {
          currentColor = this.dataset.color;
          document.querySelectorAll('.color-dot').forEach(function(d) { d.classList.remove('selected'); });
          this.classList.add('selected');
          if (ctx) ctx.strokeStyle = currentColor;
          currentTool = 'pen';
          document.querySelectorAll('.canvas-tool-btn[data-tool]').forEach(function(b) {
            b.classList.toggle('active', b.dataset.tool === 'pen');
          });
        });
      }

      /* Size buttons */
      var sizeBtns = document.querySelectorAll('.size-btn');
      for (var s = 0; s < sizeBtns.length; s++) {
        sizeBtns[s].addEventListener('click', function() {
          currentSize = parseInt(this.dataset.size, 10);
          document.querySelectorAll('.size-btn').forEach(function(b) { b.classList.remove('active'); });
          this.classList.add('active');
          if (ctx) ctx.lineWidth = currentSize;
        });
      }

      /* Undo / Redo / Clear */
      var undoBtn = _el('notes-undo-btn');
      if (undoBtn) undoBtn.addEventListener('click', _undo);
      var redoBtn = _el('notes-redo-btn');
      if (redoBtn) redoBtn.addEventListener('click', _redo);
      var clearBtn = _el('notes-clear-btn');
      if (clearBtn) clearBtn.addEventListener('click', function() {
        if (!canvas) return;
        _saveUndoState();
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        _scheduleAutoSave();
      });

      /* Export buttons */
      var exportBtn = _el('notes-export-btn');
      if (exportBtn) exportBtn.addEventListener('click', _exportCanvas);
      var exportTextBtn = _el('notes-export-text-btn');
      if (exportTextBtn) exportTextBtn.addEventListener('click', _exportText);

      /* Textarea auto-save + char count */
      var ta = _el('qanim-notes-textarea');
      if (ta) {
        ta.addEventListener('input', function() {
          var count = _el('notes-char-count');
          if (count) count.textContent = ta.value.length + ' chars';
          _scheduleAutoSave();
        });
      }

      /* Canvas events — mouse */
      var cvs = _el('qanim-draw-canvas');
      if (cvs) {
        cvs.addEventListener('mousedown', _startDraw);
        cvs.addEventListener('mousemove', _draw);
        cvs.addEventListener('mouseup',   _endDraw);
        cvs.addEventListener('mouseleave', _endDraw);
        /* Touch */
        cvs.addEventListener('touchstart', _startDraw, { passive:false });
        cvs.addEventListener('touchmove',  _draw,      { passive:false });
        cvs.addEventListener('touchend',   _endDraw);
      }

      /* Drag events on header */
      var hdr = _el('qanim-notes-header');
      if (hdr) {
        hdr.addEventListener('mousedown',  _startDrag);
        hdr.addEventListener('touchstart', _startDrag, { passive:false });
      }
      document.addEventListener('mousemove', _drag);
      document.addEventListener('touchmove', _drag, { passive:false });
      document.addEventListener('mouseup',   _endDrag);
      document.addEventListener('touchend',  _endDrag);

      /* Keyboard shortcuts */
      document.addEventListener('keydown', function(e) {
        if (e.ctrlKey || e.metaKey) {
          if (e.key === 'z' && isOpen) { e.preventDefault(); _undo(); }
          if ((e.key === 'y' || (e.shiftKey && e.key === 'z')) && isOpen) { e.preventDefault(); _redo(); }
        }
      });

      /* Resize observer */
      if (window.ResizeObserver) {
        var observer = new ResizeObserver(function() { if (isOpen && currentTab === 'canvas') _resizeCanvas(); });
        var wrap = _el('qanim-canvas-wrap');
        if (wrap) observer.observe(wrap);
      }

      /* Init canvas */
      _initCanvas();

      console.log('[QAnim Notes] System initialized ✓');
    } catch(e) {
      console.error('[QAnim Notes] Init error:', e);
    }
  });

})();
"""

def inject_notes_system(html: str, question: str = "") -> str:
    """
    Injects the floating Notes whiteboard system into any animation HTML.
    Injects: CSS → body DOM → JS module.
    """
    # ── 1. CSS before </head> ──
    try:
        if '</head>' in html:
            html = html.replace('</head>', _NOTES_CSS + '\n</head>', 1)
        else:
            html = _NOTES_CSS + '\n' + html
    except Exception as e:
        QAnimLogger.warn("NotesInjector", f"CSS insertion failed: {e}")

    # ── 2. DOM after <body> ──
    try:
        body_match = re.search(r'<body[^>]*>', html, re.IGNORECASE)
        if body_match:
            ins = body_match.end()
            html = html[:ins] + '\n' + _NOTES_DOM + html[ins:]
        else:
            html = _NOTES_DOM + '\n' + html
    except Exception as e:
        QAnimLogger.warn("NotesInjector", f"DOM insertion failed: {e}")

    # ── 3. JS before </body> ──
    try:
        notes_script = '<script>\n' + _NOTES_JS + '\n</script>'
        if '</body>' in html:
            html = html.replace('</body>', notes_script + '\n</body>', 1)
        else:
            html += '\n' + notes_script
    except Exception as e:
        QAnimLogger.warn("NotesInjector", f"JS insertion failed: {e}")

    QAnimLogger.ok("NotesInjector", "Notes whiteboard system injected")
    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 7.6 — Quiz Generator  (NEW in v7.0)
#  AI-powered adaptive quiz engine. Generates a complete interactive
#  quiz HTML from the student's question + topic category.
# ══════════════════════════════════════════════════════════════════════

QUIZ_SYSTEM_PROMPT = """You are QAnim Quiz Engine v9 — an expert educational quiz designer.

YOUR MISSION: Generate a premium self-contained interactive quiz HTML that:
- Tests conceptual understanding of the given topic
- Uses varied question types (MCQ, True/False, Numerical)
- Provides immediate animated feedback
- Explains why each answer is correct or wrong
- Shows a final score with motivational message
- Has a retry button that requests fresh questions from the host

VISUAL REQUIREMENTS:
- Dark premium background (#2d2a6e → #3d3a91 gradient — slightly brighter than before)
- Vivid accent colors (purple/blue/amber)
- Smooth CSS animations on correct/wrong answers
- Progress bar at top
- Celebration animation on completion (CSS only)
- Mobile-responsive design
- Font: -apple-system, 'Segoe UI', Arial, sans-serif

CRITICAL RULES:
✅ Return ONLY a complete <!DOCTYPE html>...</html> document
✅ Self-contained: NO external fonts, NO CDN links, NO external scripts
✅ NO document.write() anywhere
✅ NO backtick template literals in JS
✅ All JS in a single <script> block at end of body
✅ Questions must be conceptually related to the topic (NOT the exact same question)
✅ Include EXACTLY 15 questions: 6 MCQ, 3 True/False, 3 Numerical, 2 Reasoning MCQ, 1 Formula MCQ
✅ Each question must have: question text, correct answer, explanation, and a hint
✅ Numerical questions must accept a range ±10% for tolerance
✅ Show progress as "Question N of 15" counter on each question card
✅ Show score, percentage, and a star rating (1-3 stars) at the end
✅ QUIZ GATE SCREEN: The page must open with a cinematic "Quiz Unlocked" intro screen
   - div id="quiz-gate" covers full viewport (position:fixed inset:0) -- shown on load
   - Contents: "Quiz Unlocked!" heading, subtext, large "Start Quiz" button id="quiz-gate-btn"
   - Clicking #quiz-gate-btn: fades out #quiz-gate (opacity 0, pointer-events:none),
     then sets display:block on div id="quiz-main"
   - #quiz-main is hidden initially (display:none)
✅ ONE QUESTION AT A TIME: Show only one question card at a time
   - currentQ variable tracks active question index
   - After selecting an answer and seeing feedback, user clicks "Next Question" button
   - Last question's "Next" button shows the score screen instead
✅ RETRY: "Retry Quiz" button calls regenerateQuiz() which:
   1. Sends postMessage to parent: window.parent.postMessage({type:'qanim:retryQuiz', question: QUESTION_TEXT, category: CATEGORY_TEXT}, '*')
   2. Shows a loading spinner while waiting for host to regenerate
   3. Falls back to Fisher-Yates shuffle of masterQuestions if no host responds within 3s
   Store original question and category in JS vars: var QUESTION_TEXT and var CATEGORY_TEXT"""

QUIZ_PROMPT_TEMPLATE = """Generate a complete premium interactive quiz for this topic.

ORIGINAL QUESTION (for context only -- do NOT copy it directly into quiz):
{question}

TOPIC CATEGORY: {category}

QUIZ REQUIREMENTS -- GENERATE EXACTLY 15 QUESTIONS:
Q1  (MCQ, 2pts): Core concept -- fundamental principle at play
Q2  (MCQ, 2pts): Application -- slightly different real-world scenario
Q3  (True/False, 1pt): Common misconception -- students often get this wrong
Q4  (Numerical, 3pts): Calculate a related quantity (use different numbers)
Q5  (MCQ, 2pts): Conceptual -- what happens when one variable changes?
Q6  (MCQ, 2pts): Formula identification -- which equation governs this?
Q7  (True/False, 1pt): Edge case -- is this statement true in all conditions?
Q8  (Numerical, 3pts): Multi-step calculation related to the concept
Q9  (MCQ, 2pts): Tricky reasoning -- which of these is NOT correct?
Q10 (MCQ, 2pts): Application in a different physical context
Q11 (True/False, 1pt): Conceptual depth -- deeper insight about the topic
Q12 (Numerical, 3pts): Inverse calculation (find a different variable)
Q13 (MCQ, 2pts): Exam-style -- compare two scenarios, which is larger?
Q14 (MCQ, 2pts): Common exam trap question on this topic
Q15 (MCQ, 2pts): Synthesis -- combine two concepts from this topic

DESIGN:
- Background: #2d2a6e (brighter indigo — v9 theme)
- Correct answer highlight: green (#10b981)
- Wrong answer highlight: red (#ef4444)
- Accent: purple (#7c3aed) / amber (#f59e0b)
- Smooth scale/color transitions on answer selection
- Expandable explanation section per question
- Hint button per question
- Progress shown as "Question N of 15" counter at top of each question card
- Progress bar filling from 0% to 100% across 15 questions

QUIZ GATE (shown before quiz questions):
- Full-page overlay div id="quiz-gate": dark glassmorphism bg, centered content
- "Quiz Unlocked!" h2 + "Complete the animation first, then test yourself." p
- Large "Start Quiz" button id="quiz-gate-btn"
- On click: transition #quiz-gate out (opacity fade), show div id="quiz-main"

ONE-AT-A-TIME QUESTION FLOW (inside #quiz-main):
- Show only ONE question card at a time; others hidden
- After user picks an answer: show correct/wrong highlight + explanation
- "Next Question" button appears after answering; clicking it reveals the next question
- After Q15: show score screen

RETRY IMPLEMENTATION -- do NOT use location.reload():
- Store all 15 questions in: var masterQuestions = [ ...all 15 objects... ];
- Fisher-Yates shuffle function: function shuffle(arr){{ var a=arr.slice(); for(var i=a.length-1;i>0;i--){{ var j=Math.floor(Math.random()*(i+1)); var t=a[j]; a[j]=a[i]; a[i]=t; }} return a; }}
- regenerateQuiz(): shuffles masterQuestions, also shuffles each question's options tracking new correct index, resets score/currentQ/answered, re-renders all cards from Q1
- "Retry Quiz" button onclick must call regenerateQuiz()

Return ONLY the complete <!DOCTYPE html>...</html> document. No markdown. No preamble."""


class QuizGenerator:
    """
    AI-powered quiz generator. Generates a complete interactive quiz HTML
    for a given question + topic category.
    Never raises — returns fallback HTML on any failure.
    """

    @classmethod
    async def generate(cls, question: str, category: str) -> str:
        """Generate a complete interactive quiz HTML document."""
        QAnimLogger.info("QuizGen", f"Generating quiz for category={category}")

        prompt = QUIZ_PROMPT_TEMPLATE.format(
            question=question[:400],
            category=category
        )

        try:
            # QUIZ_MODEL = claude-haiku-4.5 — handles 15-Q quiz generation efficiently
            # system prompt is cached (large static text, saves ~90% on repeated calls)
            msg = client.messages.create(
                model=QUIZ_MODEL,
                max_tokens=MAX_TOK_QUIZ,
                system=[{"type": "text", "text": QUIZ_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}]
            )
            raw = msg.content[0].text.strip()
            QAnimLogger.info("QuizGen", f"model={QUIZ_MODEL}  stop_reason={msg.stop_reason}  len={len(raw)}")

            # Extract HTML
            quiz_html = cls._extract_html(raw)
            if not quiz_html:
                QAnimLogger.warn("QuizGen", "No HTML found — using fallback")
                return cls._fallback_quiz(question, category)

            # Validate
            try:
                GenerationValidator.validate(quiz_html, require_svg=False)
            except ValidationError as e:
                QAnimLogger.warn("QuizGen", f"Validation failed ({e}) — attempting recovery")
                if '<html' in quiz_html and len(quiz_html) > 400:
                    pass  # allow imperfect quiz HTML
                else:
                    return cls._fallback_quiz(question, category)

            quiz_html = HtmlSanitizer.sanitize(quiz_html)
            QAnimLogger.ok("QuizGen", f"Quiz generated ({len(quiz_html):,} chars)")
            return quiz_html

        except Exception as e:
            QAnimLogger.error("QuizGen", f"API error: {e}")
            return cls._fallback_quiz(question, category)

    @classmethod
    def _extract_html(cls, raw: str) -> str:
        """Extract HTML from AI response."""
        for marker in ['<!DOCTYPE html>', '<!doctype html>', '<html']:
            idx = raw.lower().find(marker.lower())
            if idx != -1:
                end = raw.rfind('</html>')
                if end != -1:
                    return raw[idx:end + 7]
                return raw[idx:]
        # Strip code fences
        stripped = re.sub(r'^```(?:html)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE).strip()
        if stripped.startswith('<'):
            return stripped
        return ""

    @classmethod
    def _fallback_quiz(cls, question: str, category: str) -> str:
        """Returns a minimal but functional fallback quiz."""
        q_safe  = html_module.escape(question[:100])
        cat_safe = html_module.escape(category)
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QAnim Quiz</title>
<style>
  *, *::before, *::after {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ width:100%; height:100%; overflow:auto; background:linear-gradient(135deg,#2d2a6e,#3d3a91);
    font-family:-apple-system,'Segoe UI',Arial,sans-serif;
    display:flex; align-items:flex-start; justify-content:center; padding:30px 16px; }}
  .card {{ background:rgba(49,46,129,0.9); border:1px solid rgba(124,58,237,0.45);
    border-radius:20px; padding:32px; max-width:560px; width:100%;
    box-shadow:0 24px 60px rgba(0,0,0,0.35); }}
  .icon {{ font-size:36px; text-align:center; margin-bottom:16px; }}
  h2 {{ font-size:18px; color:#f1f5f9; text-align:center; margin-bottom:8px; }}
  .sub {{ font-size:12px; color:#64748b; text-align:center; margin-bottom:24px; }}
  .q {{ background:rgba(255,255,255,0.04); border:1px solid rgba(124,58,237,0.2);
    border-radius:14px; padding:18px; margin-bottom:14px; }}
  .q-text {{ font-size:14px; color:#e2e8f0; margin-bottom:12px; line-height:1.6; }}
  .opt {{ padding:10px 14px; border-radius:10px; border:1px solid rgba(255,255,255,0.1);
    background:rgba(255,255,255,0.04); color:#cbd5e1; font-size:13px; cursor:pointer;
    margin-bottom:6px; transition:background 0.2s,border-color 0.2s; }}
  .opt:hover {{ background:rgba(124,58,237,0.15); border-color:rgba(124,58,237,0.4); }}
  .opt.correct {{ background:rgba(16,185,129,0.2); border-color:#10b981; color:#6ee7b7; }}
  .opt.wrong   {{ background:rgba(239,68,68,0.2);  border-color:#ef4444; color:#fca5a5; }}
  .expl {{ font-size:12px; color:#64748b; margin-top:10px; padding:10px 12px;
    border-radius:8px; background:rgba(255,255,255,0.03); display:none; line-height:1.6; }}
  .expl.show {{ display:block; }}
  .submit-btn {{ width:100%; padding:12px; border-radius:12px; border:none;
    background:linear-gradient(135deg,#7c3aed,#db2777); color:#fff; font-size:14px;
    font-weight:700; cursor:pointer; margin-top:8px;
    transition:opacity 0.2s,transform 0.2s; }}
  .submit-btn:hover {{ opacity:0.9; transform:translateY(-1px); }}
  .score {{ text-align:center; padding:24px; display:none; }}
  .score h3 {{ font-size:22px; color:#f1f5f9; margin-bottom:8px; }}
  .score .pct {{ font-size:40px; font-weight:800; color:#7c3aed; }}
  .retry {{ margin-top:16px; padding:10px 28px; border-radius:50px; border:none;
    background:rgba(124,58,237,0.2); border:1px solid rgba(124,58,237,0.5);
    color:#a78bfa; font-size:13px; font-weight:700; cursor:pointer; }}
</style>
</head>
<body>
<div class="card">
  <div class="icon">🧠</div>
  <h2>Concept Check: {cat_safe}</h2>
  <div class="sub">Test your understanding after the animation</div>

  <div id="quiz-form">
    <div class="q">
      <div class="q-text">1. What fundamental principle governs this type of problem?</div>
      <div class="opt" data-q="0" data-i="0">Conservation of energy</div>
      <div class="opt" data-q="0" data-i="1">Newton's third law</div>
      <div class="opt" data-q="0" data-i="2">Ohm's Law / fundamental relationship</div>
      <div class="opt" data-q="0" data-i="3">Superposition principle</div>
      <div class="expl" id="expl-0">The correct answer depends on your specific topic. Review the animation to identify the core principle at work in this type of problem.</div>
    </div>
    <div class="q">
      <div class="q-text">2. True or False: In this type of problem, all variables are independent of each other.</div>
      <div class="opt" data-q="1" data-i="0">True</div>
      <div class="opt" data-q="1" data-i="1">False — they are related by the governing equation</div>
      <div class="expl" id="expl-1">False. The variables in this problem type are related through the governing equation. Changing one affects the others.</div>
    </div>
    <div class="q">
      <div class="q-text">3. Why is it important to identify all given quantities before solving?</div>
      <div class="opt" data-q="2" data-i="0">To choose the correct formula and avoid missing constraints</div>
      <div class="opt" data-q="2" data-i="1">It isn't — you can always guess</div>
      <div class="opt" data-q="2" data-i="2">Only relevant in physics problems</div>
      <div class="opt" data-q="2" data-i="3">To impress your teacher</div>
      <div class="expl" id="expl-2">Identifying all given quantities ensures you select the right formula and don't overlook constraints that affect the solution.</div>
    </div>
    <button class="submit-btn" onclick="submitQuiz()">Submit Answers</button>
  </div>

  <div class="score" id="score-view">
    <h3>Quiz Complete!</h3>
    <div class="pct" id="score-pct">0%</div>
    <p style="color:#64748b;font-size:13px;margin-top:8px;">Keep reviewing the animation to strengthen your understanding.</p>
    <button class="retry" onclick="location.reload()">🔄 Try Again</button>
  </div>
</div>
<script>
try {{
  var answers = {{0: 2, 1: 1, 2: 0}};
  var selected = {{}};
  var submitted = false;

  document.querySelectorAll('.opt').forEach(function(opt) {{
    opt.addEventListener('click', function() {{
      if (submitted) return;
      var q = this.dataset.q;
      selected[q] = parseInt(this.dataset.i, 10);
      document.querySelectorAll('[data-q="' + q + '"]').forEach(function(o) {{ o.style.opacity = '0.6'; }});
      this.style.opacity = '1';
      this.style.borderColor = 'rgba(124,58,237,0.7)';
      this.style.background = 'rgba(124,58,237,0.2)';
      this.style.color = '#c4b5fd';
    }});
  }});

  function submitQuiz() {{
    submitted = true;
    var score = 0;
    var total = Object.keys(answers).length;
    for (var q in answers) {{
      var correct = answers[q];
      var chosen  = selected[q];
      var opts = document.querySelectorAll('[data-q="' + q + '"]');
      opts[correct].classList.add('correct');
      if (chosen !== undefined && chosen !== correct) {{
        opts[chosen].classList.add('wrong');
      }}
      var expl = document.getElementById('expl-' + q);
      if (expl) expl.classList.add('show');
      if (chosen === correct) score++;
    }}
    setTimeout(function() {{
      document.getElementById('quiz-form').style.display = 'none';
      var sv = document.getElementById('score-view');
      sv.style.display = 'block';
      var pct = Math.round(score / total * 100);
      document.getElementById('score-pct').textContent = pct + '%';
    }}, 1200);
  }}
}} catch(e) {{ console.error('Quiz error:', e); }}
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════
#  MODULE 7.7 — Hook Generator  (NEW in v7.0)
#  Cinematic curiosity-driven hook animation. No answers revealed.
#  Uses storytelling, real-world scenarios, dramatic reveals.
# ══════════════════════════════════════════════════════════════════════

HOOK_SYSTEM_PROMPT = """You are QAnim Cinematic Hook Engine v9 — a premium educational storytelling designer.

YOUR MISSION: Create a stunning text-only cinematic hook experience that:
1. Opens with a dramatic real-world scenario related to the question
2. Builds suspense and emotional investment through typography
3. Poses the "Why does this happen?" question with beautiful formatting
4. Creates a strong "I need to know the answer" feeling
5. Ends with an elegant "Start Learning" invitation

CRITICAL: NO SVG animations. NO moving objects. NO animation-heavy rendering.
Instead: Beautiful typography, cinematic fade-in transitions, glassmorphism UI.

THE HOOK MUST:
- Feel like a Netflix title card / Apple product page
- Use dramatic large typography with gradient text
- Use CSS fade-in / slide-up transitions ONLY (no SVG, no canvas)
- Reference a real-world application or story
- Create emotional resonance through words, not graphics
- End on a cliffhanger / open question leading to "Start Learning"

VISUAL STYLE:
- Background: slightly brighter indigo gradient (#2d2a6e → #3d3a91 → #2a2760)
- Glassmorphism cards with soft violet glow borders
- Large bold headline with purple→pink gradient text
- Short suspenseful educational storytelling paragraphs
- Minimal motion: smooth CSS fade-in only (opacity + translateY)
- Scene structure: 4-5 text scenes, each fades in cleanly
- Typography: -apple-system, 'Segoe UI', sans-serif; weights 300/400/700/900

SCENE NARRATIVE STRUCTURE:
Scene 1 — WORLD: One dramatic real-world sentence. Big. Bold.
Scene 2 — INCIDENT: A surprising fact or paradox. Smaller, elegant.
Scene 3 — MYSTERY: The "why?" question posed with tension.
Scene 4 — STAKES: What mastering this unlocks.
Scene 5 — INVITATION: "✦ Start Learning" gateway screen.

OUTPUT FORMAT — STRICT:
Return ONLY a complete <!DOCTYPE html>...</html> document.
No JSON. No markdown. No preamble. Just the raw HTML.

TECHNICAL RULES:
✅ Self-contained: NO external fonts, NO CDN links
✅ NO SVG elements (except tiny inline icons if needed)
✅ NO document.write() anywhere
✅ NO backtick template literals
✅ All <script> and <style> tags must be balanced
✅ Include: #prevbtn, #nextbtn, #dots for scene navigation
✅ Include: #qstrip .qtext for question display at top
✅ Progress: MANUAL user navigation only — NO auto-advance
✅ HOOK GATE — Scene 5 (final) must show "✦ Start Learning" button:
   - id="hook-start-btn"
   - Large gradient pill, glowing purple/pink, centered, pulse animation
   - onclick: if(window.onHookComplete) window.onHookComplete();
✅ Background: linear-gradient(135deg, #2d2a6e 0%, #3d3a91 40%, #2a2760 100%)
✅ Each scene: glassmorphism card, centered, max-width 680px, fade-in on activation"""

HOOK_PROMPT_TEMPLATE = """Create a CINEMATIC TEXT-ONLY HOOK for this question.

QUESTION: {question}
CATEGORY: {category}

HOOK NARRATIVE — 5 SCENES (text-only, glassmorphism cards, fade-in CSS only):

Scene 1 — WORLD (Big dramatic statement):
  A single jaw-dropping real-world sentence about the topic. Large font. Bold gradient text.
  Example feel: "Every second, 3 billion CPU transistors switch on and off."

Scene 2 — INCIDENT (Surprising fact or paradox):
  Something unexpected. Creates cognitive dissonance. Smaller elegant typography.
  Subheadline + 1-2 sentence story.

Scene 3 — MYSTERY (The "Why?" tension):
  Pose the core question dramatically. Use suspense pacing.
  "But why does this happen?" style. Add a glowing question mark visual.

Scene 4 — STAKES (What this unlocks):
  Show the real-world power of understanding this concept.
  Engineers / scientists / professionals who rely on this.

Scene 5 — INVITATION:
  "Let's find out together."
  Large "✦ Start Learning" button (id="hook-start-btn").

CATEGORY-SPECIFIC STORYTELLING for "{category}":
- VISUAL_PHYSICS: Lead with a dramatic physical event (crash, orbit, explosion)
- MATHEMATICAL: Lead with a surprising number or pattern that defies intuition
- PROCESS_BASED: Lead with what happens when the process fails catastrophically
- BIOLOGICAL: Lead with the microscopic world being more dramatic than imagined
- ABSTRACT: Lead with a historical moment where this concept changed society
- MIXED: Lead with the human vs. machine scale contrast

DESIGN REQUIREMENTS:
- Background: linear-gradient(135deg, #2d2a6e 0%, #3d3a91 40%, #2a2760 100%)
- Each scene: centered glassmorphism card (backdrop-filter:blur(20px), rgba white bg)
- Scene headline: 2.8rem–3.6rem, font-weight:900, gradient text (purple→pink)
- Body text: 1.05rem, color:#c4b5fd or #e2e8f0, line-height:1.8
- Glow accent lines: thin horizontal rules with purple gradient
- Navigation: small pill buttons #prevbtn / #nextbtn + dot indicators #dots
- Scene transitions: CSS opacity 0→1 + translateY(24px)→0, 0.6s ease
- NO SVG (except tiny inline icon if needed), NO canvas, NO heavy animation

MANUAL STEP CONTROL — CRITICAL:
- Remove ALL auto-advance timers between scenes
- User clicks #nextbtn to advance; #prevbtn to go back
- Each scene fades in automatically when it becomes active (CSS transition)
- Scene 5 shows "✦ Start Learning" button: onclick calls window.onHookComplete if defined

IMPORTANT: Return ONLY the raw <!DOCTYPE html>...</html>. Nothing else."""


class HookGenerator:
    """
    Cinematic hook animation generator. Creates a curiosity-driven,
    emotionally engaging intro animation before the teaching begins.
    Never reveals the answer. Never raises — returns fallback on failure.
    """

    @classmethod
    async def generate(cls, question: str, category: str) -> str:
        """Generate a cinematic hook animation HTML document."""
        QAnimLogger.info("HookGen", f"Generating hook for category={category}")

        prompt = HOOK_PROMPT_TEMPLATE.format(
            question=question[:400],
            category=category
        )

        try:
            # HOOK_MODEL = claude-haiku-4.5 — fast and sufficient for text hook
            # system prompt is cached (large static text, saves ~90% on repeated calls)
            msg = client.messages.create(
                model=HOOK_MODEL,
                max_tokens=MAX_TOK_HOOK,
                system=[{"type": "text", "text": HOOK_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}]
            )
            raw = msg.content[0].text.strip()
            QAnimLogger.info("HookGen", f"model={HOOK_MODEL}  stop_reason={msg.stop_reason}  len={len(raw)}")

            # Extract HTML
            hook_html = cls._extract_html(raw)
            if not hook_html:
                QAnimLogger.warn("HookGen", "No HTML extracted — using fallback")
                return cls._fallback_hook(question, category)

            # Validate — text-only hook: SVG is NOT required (Change 2: text-only redesign)
            try:
                GenerationValidator.validate(hook_html, require_svg=False)
            except ValidationError as e:
                QAnimLogger.warn("HookGen", f"Validation: {e}")
                if '<html' in hook_html and len(hook_html) > 500:
                    hook_html = RecoveryEngine.partial_html(question, hook_html)
                else:
                    return cls._fallback_hook(question, category)

            hook_html = HtmlSanitizer.sanitize(hook_html)
            hook_html = inject_infrastructure(hook_html)
            hook_html = inject_notes_system(hook_html, question)
            hook_html = inject_hook_gate(hook_html)        # ← NEW: Start Learning gate
            hook_html = inject_step_controller(hook_html)  # ← NEW: manual step safety net

            QAnimLogger.ok("HookGen", f"Hook generated ({len(hook_html):,} chars)")
            return hook_html

        except Exception as e:
            QAnimLogger.error("HookGen", f"Error: {e}")
            return cls._fallback_hook(question, category)

    @classmethod
    def _extract_html(cls, raw: str) -> str:
        """Extract HTML document from AI response."""
        # Strip JSON wrapping if present
        for marker in ['<!DOCTYPE html>', '<!doctype html>', '<html']:
            idx = raw.lower().find(marker.lower())
            if idx != -1:
                end = raw.rfind('</html>')
                if end != -1:
                    return raw[idx:end + 7]
                return raw[idx:]
        stripped = re.sub(r'^```(?:html)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE).strip()
        if stripped.lower().startswith('<'):
            return stripped
        return ""

    @classmethod
    def _fallback_hook(cls, question: str, category: str) -> str:
        """
        Text-only cinematic fallback hook — 5 scenes, glassmorphism cards,
        fade-in transitions only. No SVG required. (v9.0 redesign)
        """
        q_safe   = html_module.escape(question[:160])
        cat_safe = html_module.escape(category)

        # Category-specific headlines and story beats
        cat_data = {
            "VISUAL_PHYSICS": {
                "emoji": "⚡",
                "headline": "The Universe Runs on Forces You Can't See",
                "scene2_title": "Something Unexpected Happens",
                "scene2_body": "At the exact moment forces balance — motion stops. Or does it? Physics hides its deepest secrets in plain sight, waiting for the right question to unlock them.",
                "scene3_question": "Why does this system behave the way it does?",
                "scene4_stakes": "Engineers design bridges, rockets, and microchips by mastering exactly this principle. One equation governs it all.",
            },
            "MATHEMATICAL": {
                "emoji": "∞",
                "headline": "The Pattern Was There All Along",
                "scene2_title": "Numbers Reveal Hidden Structure",
                "scene2_body": "Mathematics isn't invented — it's discovered. The relationship you're about to uncover has existed since the universe began. Mathematicians just learned to see it.",
                "scene3_question": "What elegant principle connects these quantities?",
                "scene4_stakes": "From cryptography to AI, from architecture to music — this mathematical relationship shapes everything modern civilization is built on.",
            },
            "PROCESS_BASED": {
                "emoji": "⚙️",
                "headline": "Every Machine Has a Secret Heartbeat",
                "scene2_title": "When the Process Fails",
                "scene2_body": "In 1986, a misunderstood process led to catastrophic failure. The engineers knew the components — but not how they worked together. Understanding process isn't optional.",
                "scene3_question": "How does this system actually function step by step?",
                "scene4_stakes": "The engineers, developers, and scientists who truly understand how systems work are the ones who build the future.",
            },
            "BIOLOGICAL": {
                "emoji": "🧬",
                "headline": "Inside You, a War Is Being Won Right Now",
                "scene2_title": "The Microscopic World Is Stranger Than Fiction",
                "scene2_body": "At this very moment, billions of molecular machines in your body are performing operations more complex than any computer — guided by principles discovered only in the last century.",
                "scene3_question": "How does this biological process actually work?",
                "scene4_stakes": "Every medical breakthrough — every vaccine, every targeted therapy — was built on understanding exactly this type of biological mechanism.",
            },
            "ABSTRACT": {
                "emoji": "🌍",
                "headline": "Ideas Have Shaped Civilizations",
                "scene2_title": "When Concepts Collide",
                "scene2_body": "The tension between these ideas has started revolutions, toppled governments, and redefined what it means to live in a society. Understanding the distinction matters more now than ever.",
                "scene3_question": "What truly separates these concepts at their core?",
                "scene4_stakes": "Political scientists, philosophers, and citizens who clearly understand this distinction make better decisions — and build more just societies.",
            },
            "MIXED": {
                "emoji": "🔬",
                "headline": "Where Human Meets Machine",
                "scene2_title": "Two Scales, One Answer",
                "scene2_body": "The phenomenon looks completely different depending on whether you view it from human scale or machine scale. Yet both perspectives are governed by the same underlying principle.",
                "scene3_question": "What single principle unifies these perspectives?",
                "scene4_stakes": "The innovators who master cross-domain thinking — connecting the physical and the theoretical — are the ones building tomorrow's breakthroughs.",
            },
        }
        d = cat_data.get(category, cat_data["MIXED"])

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QAnim Hook — {cat_safe}</title>
<style>
/* ═══════════════════════════════════════════════════════
   QAnim v9 Cinematic Text Hook — Text-Only Premium Design
   ═══════════════════════════════════════════════════════ */
*, *::before, *::after {{ margin:0; padding:0; box-sizing:border-box; }}

html, body {{
  width:100%; height:100%;
  background: linear-gradient(135deg, #2d2a6e 0%, #3d3a91 40%, #2a2760 100%);
  font-family: -apple-system, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
  overflow: hidden;
  color: #e2e8f0;
}}

/* ── Question strip ── */
#qstrip {{
  position: fixed; top: 0; left: 0; right: 0; z-index: 100;
  background: rgba(15, 12, 50, 0.80);
  backdrop-filter: blur(16px);
  padding: 10px 24px;
  border-bottom: 1px solid rgba(139, 92, 246, 0.25);
}}
.qtext {{
  font-size: 11.5px; color: #7c6fac; text-align: center;
  line-height: 1.5; max-width: 700px; margin: 0 auto;
  font-weight: 400; letter-spacing: 0.2px;
}}

/* ── Scene wrapper ── */
#scenes-container {{
  position: fixed; inset: 0;
  display: flex; align-items: center; justify-content: center;
  padding: 60px 20px 80px;
}}

/* ── Individual scene ── */
.hook-scene {{
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  padding: 60px 20px 80px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.65s ease, transform 0.65s cubic-bezier(0.25, 0.46, 0.45, 0.94);
  transform: translateY(18px);
}}
.hook-scene.active {{
  opacity: 1; pointer-events: auto; transform: translateY(0);
}}

/* ── Glassmorphism card ── */
.hook-card {{
  background: rgba(255, 255, 255, 0.055);
  border: 1px solid rgba(139, 92, 246, 0.30);
  border-radius: 28px;
  padding: 44px 48px;
  max-width: 640px;
  width: 100%;
  text-align: center;
  backdrop-filter: blur(24px);
  -webkit-backdrop-filter: blur(24px);
  box-shadow:
    0 0 0 1px rgba(139,92,246,0.12),
    0 32px 80px rgba(0,0,0,0.38),
    0 8px 32px rgba(109,40,217,0.20);
  position: relative;
  overflow: hidden;
}}
.hook-card::before {{
  content: '';
  position: absolute; top: 0; left: 15%; right: 15%; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(167,139,250,0.6), transparent);
}}

/* ── Scene number badge ── */
.scene-badge {{
  display: inline-block;
  font-size: 10px; font-weight: 700; letter-spacing: 2.5px;
  text-transform: uppercase; color: #7c3aed;
  background: rgba(124,58,237,0.12);
  border: 1px solid rgba(124,58,237,0.28);
  border-radius: 50px; padding: 4px 14px;
  margin-bottom: 22px;
}}

/* ── Emoji ── */
.hook-emoji {{
  font-size: 52px; display: block; margin-bottom: 20px;
  filter: drop-shadow(0 4px 16px rgba(124,58,237,0.45));
  animation: floatEmoji 4s ease-in-out infinite;
}}
@keyframes floatEmoji {{
  0%,100% {{ transform: translateY(0); }}
  50%      {{ transform: translateY(-8px); }}
}}

/* ── Headlines ── */
.hook-headline {{
  font-size: clamp(1.7rem, 4vw, 2.6rem);
  font-weight: 900;
  line-height: 1.18;
  letter-spacing: -0.5px;
  margin-bottom: 18px;
  background: linear-gradient(135deg, #c4b5fd 0%, #a78bfa 40%, #ec4899 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}}

/* ── Scene title (smaller) ── */
.hook-scene-title {{
  font-size: 1.05rem; font-weight: 700;
  color: #a78bfa; margin-bottom: 14px;
  letter-spacing: 0.3px;
}}

/* ── Body text ── */
.hook-body {{
  font-size: 1rem; line-height: 1.82;
  color: #c4b5fd; font-weight: 300;
  max-width: 520px; margin: 0 auto;
}}
.hook-body strong {{ color: #e9d5ff; font-weight: 700; }}

/* ── Question highlight ── */
.hook-question {{
  font-size: 1.15rem; font-weight: 700; font-style: italic;
  color: #f0abfc; margin: 16px 0;
  padding: 14px 20px;
  border-left: 3px solid rgba(216,180,254,0.5);
  text-align: left;
  background: rgba(124,58,237,0.10);
  border-radius: 0 12px 12px 0;
}}

/* ── Glow divider ── */
.hook-divider {{
  width: 60px; height: 2px;
  background: linear-gradient(90deg, #7c3aed, #ec4899);
  border-radius: 2px; margin: 20px auto;
  box-shadow: 0 0 12px rgba(124,58,237,0.6);
}}

/* ── Start Learning button (Scene 5) ── */
#hook-start-btn {{
  display: inline-flex; align-items: center; gap: 10px;
  margin-top: 28px;
  padding: 16px 44px; border-radius: 50px; border: none; cursor: pointer;
  background: linear-gradient(135deg, #7c3aed 0%, #a855f7 50%, #ec4899 100%);
  color: #fff; font-size: 1rem; font-weight: 800; letter-spacing: 0.5px;
  font-family: inherit;
  box-shadow: 0 0 40px rgba(124,58,237,0.55), 0 8px 32px rgba(0,0,0,0.35);
  animation: pulseCta 2.2s ease-in-out infinite;
  transition: transform 0.2s, box-shadow 0.2s;
  position: relative; z-index: 10;
}}
#hook-start-btn:hover {{
  transform: scale(1.05) translateY(-2px);
  box-shadow: 0 0 60px rgba(124,58,237,0.75), 0 12px 40px rgba(0,0,0,0.4);
}}
@keyframes pulseCta {{
  0%,100% {{ box-shadow: 0 0 40px rgba(124,58,237,0.55), 0 8px 32px rgba(0,0,0,0.35); }}
  50%      {{ box-shadow: 0 0 65px rgba(236,72,153,0.65), 0 8px 32px rgba(0,0,0,0.35); }}
}}

/* ── Navigation bar ── */
#hook-nav {{
  position: fixed; bottom: 0; left: 0; right: 0; z-index: 200;
  display: flex; align-items: center; justify-content: center; gap: 20px;
  padding: 14px 24px 18px;
  background: rgba(15,12,50,0.70);
  backdrop-filter: blur(14px);
  border-top: 1px solid rgba(139,92,246,0.18);
}}
#prevbtn, #nextbtn {{
  display: flex; align-items: center; justify-content: center;
  width: 40px; height: 40px; border-radius: 50%;
  border: 1.5px solid rgba(139,92,246,0.40);
  background: rgba(124,58,237,0.14);
  color: #a78bfa; font-size: 16px; cursor: pointer;
  transition: background 0.2s, border-color 0.2s, transform 0.15s;
  font-family: inherit;
}}
#prevbtn:hover:not(:disabled), #nextbtn:hover:not(:disabled) {{
  background: rgba(124,58,237,0.28); border-color: rgba(139,92,246,0.70);
  transform: scale(1.1);
}}
#prevbtn:disabled, #nextbtn:disabled {{ opacity: 0.28; cursor: not-allowed; }}
#dots {{
  display: flex; gap: 8px; align-items: center;
}}
.dot {{
  width: 8px; height: 8px; border-radius: 50%;
  background: rgba(139,92,246,0.30);
  transition: background 0.3s, transform 0.3s;
  cursor: pointer;
}}
.dot.active {{
  background: #a78bfa; transform: scale(1.35);
  box-shadow: 0 0 8px rgba(167,139,250,0.7);
}}

/* ── Ambient glow orbs (CSS only, no SVG) ── */
.glow-orb {{
  position: fixed; border-radius: 50%; pointer-events: none; z-index: 0;
  animation: orbPulse 6s ease-in-out infinite;
}}
.glow-orb-1 {{
  width: 340px; height: 340px; top: -80px; left: -80px;
  background: radial-gradient(circle, rgba(124,58,237,0.18) 0%, transparent 70%);
  animation-delay: 0s;
}}
.glow-orb-2 {{
  width: 280px; height: 280px; bottom: -60px; right: -60px;
  background: radial-gradient(circle, rgba(236,72,153,0.14) 0%, transparent 70%);
  animation-delay: 2.5s;
}}
.glow-orb-3 {{
  width: 200px; height: 200px; top: 40%; left: 50%; transform: translateX(-50%);
  background: radial-gradient(circle, rgba(99,102,241,0.12) 0%, transparent 70%);
  animation-delay: 1.2s;
}}
@keyframes orbPulse {{
  0%,100% {{ opacity:1; transform: scale(1); }}
  50%      {{ opacity:0.6; transform: scale(1.15); }}
}}
</style>
</head>
<body>

<!-- Background ambient glow orbs (CSS-only, no SVG) -->
<div class="glow-orb glow-orb-1"></div>
<div class="glow-orb glow-orb-2"></div>
<div class="glow-orb glow-orb-3"></div>

<!-- Question strip -->
<div id="qstrip">
  <div class="qtext">{q_safe}</div>
</div>

<!-- Scenes container -->
<div id="scenes-container">

  <!-- Scene 1: WORLD -->
  <div class="hook-scene active" id="scene-0">
    <div class="hook-card">
      <span class="scene-badge">Chapter 1 · The World</span>
      <span class="hook-emoji">{d["emoji"]}</span>
      <h1 class="hook-headline">{d["headline"]}</h1>
      <div class="hook-divider"></div>
      <p class="hook-body">The question in front of you is more than an exercise.<br>
        It's a window into how the universe actually works.</p>
    </div>
  </div>

  <!-- Scene 2: INCIDENT -->
  <div class="hook-scene" id="scene-1">
    <div class="hook-card">
      <span class="scene-badge">Chapter 2 · The Incident</span>
      <div class="hook-scene-title">{d["scene2_title"]}</div>
      <div class="hook-divider"></div>
      <p class="hook-body">{d["scene2_body"]}</p>
    </div>
  </div>

  <!-- Scene 3: MYSTERY -->
  <div class="hook-scene" id="scene-2">
    <div class="hook-card">
      <span class="scene-badge">Chapter 3 · The Mystery</span>
      <span class="hook-emoji">🔍</span>
      <blockquote class="hook-question">{d["scene3_question"]}</blockquote>
      <div class="hook-divider"></div>
      <p class="hook-body">The answer isn't obvious. It requires understanding
        the <strong>principles</strong> that govern this type of problem —
        which is exactly what you're about to learn.</p>
    </div>
  </div>

  <!-- Scene 4: STAKES -->
  <div class="hook-scene" id="scene-3">
    <div class="hook-card">
      <span class="scene-badge">Chapter 4 · The Stakes</span>
      <span class="hook-emoji">🚀</span>
      <div class="hook-scene-title">Why This Actually Matters</div>
      <div class="hook-divider"></div>
      <p class="hook-body">{d["scene4_stakes"]}</p>
    </div>
  </div>

  <!-- Scene 5: INVITATION -->
  <div class="hook-scene" id="scene-4">
    <div class="hook-card">
      <span class="scene-badge">Chapter 5 · The Journey Begins</span>
      <h1 class="hook-headline">Let's Find Out Together.</h1>
      <div class="hook-divider"></div>
      <p class="hook-body" style="margin-bottom:8px">
        The animation ahead will build your intuition step by step.<br>
        No shortcuts. Just deep understanding.
      </p>
      <button id="hook-start-btn"
        onclick="if(window.onHookComplete) window.onHookComplete();">
        ✦ Start Learning
      </button>
    </div>
  </div>

</div><!-- /scenes-container -->

<!-- Navigation -->
<div id="hook-nav">
  <button id="prevbtn" disabled aria-label="Previous scene">&#8592;</button>
  <div id="dots">
    <div class="dot active" data-idx="0"></div>
    <div class="dot" data-idx="1"></div>
    <div class="dot" data-idx="2"></div>
    <div class="dot" data-idx="3"></div>
    <div class="dot" data-idx="4"></div>
  </div>
  <button id="nextbtn" aria-label="Next scene">&#8594;</button>
</div>

<script>
/* QAnim v9 Text Hook — Manual Scene Controller */
(function() {{
  'use strict';
  var scenes  = document.querySelectorAll('.hook-scene');
  var dots    = document.querySelectorAll('#dots .dot');
  var prevBtn = document.getElementById('prevbtn');
  var nextBtn = document.getElementById('nextbtn');
  var current = 0;

  function showScene(idx) {{
    if (idx < 0 || idx >= scenes.length) return;
    scenes[current].classList.remove('active');
    dots[current].classList.remove('active');
    current = idx;
    scenes[current].classList.add('active');
    dots[current].classList.add('active');
    prevBtn.disabled = (current === 0);
    nextBtn.disabled = (current === scenes.length - 1);
  }}

  /* Wire nav buttons */
  nextBtn.addEventListener('click', function() {{ if (current < scenes.length - 1) showScene(current + 1); }});
  prevBtn.addEventListener('click', function() {{ if (current > 0) showScene(current - 1); }});

  /* Dot navigation */
  dots.forEach(function(dot) {{
    dot.addEventListener('click', function() {{ showScene(parseInt(this.dataset.idx, 10)); }});
  }});

  /* Expose for StepController compatibility */
  window.currentStep = current;
  window.showScene   = showScene;

  console.log('[QAnim Hook v9] Text-only cinematic hook initialized — 5 scenes');
}})();
</script>

</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════
#  MODULE 7.8 — StepControllerPatcher  (NEW in v8.0)
#  Post-processes generated HTML to strip auto-advance timers and
#  inject a bulletproof manual step controller as a safety net.
#  Runs on: hook_html, concept_html, solution_html
# ══════════════════════════════════════════════════════════════════════

_STEP_CONTROLLER_JS = r"""
<script id="qanim-step-controller">
/* QAnim Manual Step Controller v8.2 — Previous Navigation Fix */
(function patchStepController() {
  'use strict';
  /* Run on 'load' so all AI scripts have already executed */
  window.addEventListener('load', function() {
    try {
      var nextBtn = document.getElementById('nextbtn');
      var prevBtn = document.getElementById('prevbtn');
      if (!nextBtn && !prevBtn) return;

      /* Collect scenes */
      var scenes = [];
      for (var i = 0; i < 20; i++) {
        var s = document.getElementById('scene-' + i);
        if (s) { scenes.push(s); } else if (i > 0) { break; }
      }
      if (scenes.length < 1) return;

      /* ── Snapshot each scene's initial computed visibility state ──────
         Captured BEFORE we touch anything, so we have a clean baseline to
         restore when the user navigates back to a previously-visited scene.
         We store: display, opacity, visibility, and all child element states
         that the AI's CSS sets to hide inactive scenes (opacity:0, display:none,
         visibility:hidden, transform translate, etc.).                        */
      var _sceneSnapshots = [];
      for (var si = 0; si < scenes.length; si++) {
        var scEl = scenes[si];
        /* Capture inline styles only — these are what the AI animation JS sets */
        _sceneSnapshots.push({
          display:    scEl.style.display,
          opacity:    scEl.style.opacity,
          visibility: scEl.style.visibility,
          transform:  scEl.style.transform,
          transition: scEl.style.transition,
          /* Also capture ALL descendant elements' inline opacity/transform/display
             so we can wipe animation-set inline styles on re-entry, forcing the
             scene's CSS-defined initial state to take over again.              */
          children: (function(root) {
            var result = [];
            var all = root.querySelectorAll('*');
            for (var ci = 0; ci < all.length; ci++) {
              result.push({
                el:         all[ci],
                opacity:    all[ci].style.opacity,
                transform:  all[ci].style.transform,
                display:    all[ci].style.display,
                visibility: all[ci].style.visibility,
                transition: all[ci].style.transition
              });
            }
            return result;
          })(scEl)
        });
      }

      /* ── _animFired tracks whether a scene's intro animation has run.
         KEY FIX: we DELETE the flag when navigating AWAY from a scene so
         that returning to it via Previous re-runs the intro animation from
         its clean initial state — instead of being permanently locked out. */
      var _animFired = {};

      /* Cache the AI's own showScene if defined — used for triggering anims */
      var _aiShowScene = (typeof window.showScene === 'function') ? window.showScene : null;
      var currentStep = 0;

      /* ── _resetScene: wipe all JS-injected inline styles on a scene and
         its children, restoring the CSS-defined initial (hidden) state.
         This undoes whatever the previous animation run did to the DOM.    */
      function _resetScene(idx) {
        var snap = _sceneSnapshots[idx];
        if (!snap) return;
        var scEl = scenes[idx];

        /* Temporarily suppress transitions so the reset is instant/invisible */
        scEl.style.transition = 'none';
        scEl.style.opacity    = snap.opacity;
        scEl.style.display    = snap.display    !== '' ? snap.display    : '';
        scEl.style.visibility = snap.visibility !== '' ? snap.visibility : '';
        scEl.style.transform  = snap.transform  !== '' ? snap.transform  : '';

        /* Reset all children to their snapshotted inline styles */
        for (var ci = 0; ci < snap.children.length; ci++) {
          var c = snap.children[ci];
          c.el.style.transition = 'none';
          c.el.style.opacity    = c.opacity;
          c.el.style.transform  = c.transform;
          c.el.style.display    = c.display;
          c.el.style.visibility = c.visibility;
        }

        /* Re-enable transitions on next frame so the incoming scene fades in */
        requestAnimationFrame(function() {
          scEl.style.transition = '';
          for (var ci2 = 0; ci2 < snap.children.length; ci2++) {
            snap.children[ci2].el.style.transition = '';
          }
        });
      }

      /* ── _fireAnim: trigger the AI's intro animation for a scene.
         REMOVED the permanent _animFired guard that was preventing re-fire.
         Instead we reset inline styles first so the animation always starts
         from a clean state, regardless of how many times the scene is visited. */
      function _fireAnim(idx) {
        if (_animFired[idx]) return;
        _animFired[idx] = true;

        /* Try direct window.animateSceneN first (most AI-generated scenes) */
        var fn = window['animateScene' + idx];
        if (typeof fn === 'function') {
          try { fn(); } catch(e) { console.warn('[SC] animateScene' + idx + ' error:', e); }
          return;
        }
        /* Fallback: use AI's own showScene — but ONLY if it won't loop back
           into our overridden showScene (we check the source text).          */
        if (_aiShowScene) {
          try { _aiShowScene(idx); } catch(e) { console.warn('[SC] _aiShowScene error:', e); }
        }
      }

      function showScene(idx) {
        if (idx < 0 || idx >= scenes.length) return;

        var prevIdx = currentStep;
        currentStep = idx;

        for (var j = 0; j < scenes.length; j++) {
          if (j === idx) {
            /* ── Incoming scene ──────────────────────────────────────── */
            /* Step 1: If this scene was visited before, clear the animFired
               flag and wipe all JS-injected inline styles so the animation
               re-runs from its pristine initial state.                    */
            if (_animFired[j]) {
              delete _animFired[j];
              _resetScene(j);
            }
            /* Step 2: Make visible — defer by one frame so _resetScene's
               RAF has time to clear transitions before we set opacity.    */
            (function(sceneEl) {
              requestAnimationFrame(function() {
                sceneEl.style.transition  = 'opacity 0.38s ease';
                sceneEl.style.opacity     = '1';
                sceneEl.style.display     = sceneEl.style.display === 'none' ? '' : sceneEl.style.display;
                sceneEl.style.visibility  = 'visible';
                sceneEl.style.pointerEvents = 'auto';
              });
            })(scenes[j]);
          } else {
            /* ── Outgoing / inactive scenes ─────────────────────────── */
            scenes[j].style.transition  = 'opacity 0.38s ease';
            scenes[j].style.opacity     = '0';
            scenes[j].style.pointerEvents = 'none';
            /* Do NOT set display:none here — opacity:0 + pointerEvents:none
               is sufficient. Setting display:none would break re-entry
               because the browser won't apply transitions to display-none
               elements, and child measurements break.                     */
          }
        }

        _updateDots();
        _updateNavBtns();

        /* Fire animation AFTER the frame that made the scene visible */
        (function(capturedIdx) {
          requestAnimationFrame(function() {
            requestAnimationFrame(function() {
              _fireAnim(capturedIdx);
            });
          });
        })(idx);

        /* Show inline quiz BELOW animation on last scene, hide on earlier scenes */
        var quiz = document.getElementById('qanim-inline-quiz');
        if (quiz) {
          if (idx === scenes.length - 1) {
            quiz.style.display = 'block';
            quiz.style.transition = 'opacity 0.7s ease';
            setTimeout(function() {
              quiz.style.opacity = '1';
              setTimeout(function() {
                quiz.scrollIntoView({ behavior: 'smooth', block: 'start' });
              }, 300);
            }, 400);
          } else {
            quiz.style.opacity = '0';
            quiz.style.transition = 'opacity 0.3s ease';
            setTimeout(function() { quiz.style.display = 'none'; }, 300);
          }
        }
      }

      function _updateDots() {
        var dc = document.getElementById('dots');
        if (!dc) return;
        var ds = dc.querySelectorAll('.dot, circle');
        if (!ds.length) ds = dc.children;
        for (var k = 0; k < ds.length; k++) {
          var active = (k === currentStep);
          ds[k].style.opacity = active ? '1' : '0.3';
          if (ds[k].classList) ds[k].classList.toggle('active', active);
        }
      }

      function _updateNavBtns() {
        /* MUST remove/add 'disabled' attribute — style.opacity alone doesn't unblock clicks */
        if (prevBtn) {
          if (currentStep === 0) {
            prevBtn.setAttribute('disabled', 'true');
            prevBtn.style.opacity = '0.3';
          } else {
            prevBtn.removeAttribute('disabled');
            prevBtn.style.opacity = '1';
          }
        }
        if (nextBtn) {
          if (currentStep === scenes.length - 1) {
            nextBtn.setAttribute('disabled', 'true');
            nextBtn.style.opacity = '0.3';
          } else {
            nextBtn.removeAttribute('disabled');
            nextBtn.style.opacity = '1';
          }
        }
      }

      /* Clone buttons to strip ALL existing onclick + listeners */
      var nb2 = nextBtn.cloneNode(true);
      nextBtn.parentNode.replaceChild(nb2, nextBtn);
      nextBtn = nb2;

      if (prevBtn) {
        var pb2 = prevBtn.cloneNode(true);
        prevBtn.parentNode.replaceChild(pb2, prevBtn);
        prevBtn = pb2;
      }

      nextBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        if (currentStep < scenes.length - 1) showScene(currentStep + 1);
      });
      if (prevBtn) {
        prevBtn.addEventListener('click', function(e) {
          e.stopPropagation();
          if (currentStep > 0) showScene(currentStep - 1);
        });
      }

      /* Block future auto-advance setIntervals */
      var _ri = window.setInterval;
      window.setInterval = function(fn, ms) {
        var src = fn ? fn.toString() : '';
        if (ms && ms < 8000 && (src.indexOf('showScene') !== -1 || src.indexOf('currentStep') !== -1 || src.indexOf('nextStep') !== -1)) {
          console.log('[SC] Blocked auto-advance interval (' + ms + 'ms)');
          return -1;
        }
        return _ri.apply(window, arguments);
      };

      showScene(0);
      console.log('[QAnim SC v8.2] ' + scenes.length + ' scenes, Previous-nav fix active');
    } catch(err) {
      console.error('[QAnim SC] Fatal:', err);
    }
  });
})();
</script>
"""


def inject_step_controller(html: str) -> str:
    """
    Injects the manual step controller safety net into animation HTML.
    Runs AFTER all other injectors so it can override any lingering auto-advance.
    """
    try:
        if '</body>' in html:
            html = html.replace('</body>', _STEP_CONTROLLER_JS + '\n</body>', 1)
        else:
            html += '\n' + _STEP_CONTROLLER_JS
        QAnimLogger.ok("StepController", "Manual step controller injected")
    except Exception as e:
        QAnimLogger.warn("StepController", f"Injection failed: {e}")
    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 7.9 — HookGate Injector  (NEW in v8.0)
#  Ensures hook HTML always has a "Start Learning" gate button,
#  even if the AI forgot to include it.
# ══════════════════════════════════════════════════════════════════════

_HOOK_GATE_CSS = """
<style id="qanim-hook-gate-style">
#hook-start-btn {
  display: inline-flex; align-items: center; gap: 10px;
  padding: 16px 40px; border-radius: 50px; border: none; cursor: pointer;
  background: linear-gradient(135deg, #7c3aed, #ec4899);
  color: #fff; font-size: 16px; font-weight: 800; letter-spacing: 0.5px;
  box-shadow: 0 0 40px rgba(124,58,237,0.5), 0 8px 32px rgba(0,0,0,0.4);
  animation: hookBtnPulse 2s ease-in-out infinite;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  position: relative; z-index: 500;
}
#hook-start-btn:hover {
  transform: scale(1.06);
  box-shadow: 0 0 60px rgba(124,58,237,0.7), 0 12px 40px rgba(0,0,0,0.5);
}
@keyframes hookBtnPulse {
  0%, 100% { box-shadow: 0 0 40px rgba(124,58,237,0.5), 0 8px 32px rgba(0,0,0,0.4); }
  50%       { box-shadow: 0 0 70px rgba(236,72,153,0.6), 0 8px 32px rgba(0,0,0,0.4); }
}
#hook-gate-overlay {
  position: fixed; inset: 0; z-index: 450;
  display: flex; align-items: flex-end; justify-content: center;
  padding-bottom: 60px; pointer-events: none;
}
</style>
"""

_HOOK_GATE_JS = r"""
<script id="qanim-hook-gate">
(function initHookGate() {
  'use strict';
  document.addEventListener('DOMContentLoaded', function() {
    /* If AI already included #hook-start-btn, just wire it up */
    var existing = document.getElementById('hook-start-btn');
    if (existing) {
      existing.addEventListener('click', function() {
        if (typeof window.onHookComplete === 'function') window.onHookComplete();
      });
      return;
    }
    /* Otherwise inject a floating gate button */
    var overlay = document.createElement('div');
    overlay.id = 'hook-gate-overlay';
    var btn = document.createElement('button');
    btn.id = 'hook-start-btn';
    btn.textContent = '✦ Start Learning';
    btn.style.pointerEvents = 'auto';
    btn.addEventListener('click', function() {
      if (typeof window.onHookComplete === 'function') window.onHookComplete();
    });
    overlay.appendChild(btn);
    document.body.appendChild(overlay);
    /* Fade in after 3s so hook has time to play */
    overlay.style.opacity = '0';
    overlay.style.transition = 'opacity 0.8s ease';
    setTimeout(function() { overlay.style.opacity = '1'; }, 3000);
    console.log('[QAnim HookGate] Start Learning button injected');
  });
})();
</script>
"""


def inject_hook_gate(html: str) -> str:
    """
    Ensures the hook HTML has a 'Start Learning' gate button.
    Injects CSS into <head> and JS before </body>.
    """
    try:
        if '</head>' in html:
            html = html.replace('</head>', _HOOK_GATE_CSS + '\n</head>', 1)
        else:
            html = _HOOK_GATE_CSS + '\n' + html
    except Exception as e:
        QAnimLogger.warn("HookGate", f"CSS injection failed: {e}")
    try:
        if '</body>' in html:
            html = html.replace('</body>', _HOOK_GATE_JS + '\n</body>', 1)
        else:
            html += '\n' + _HOOK_GATE_JS
        QAnimLogger.ok("HookGate", "Hook gate injected")
    except Exception as e:
        QAnimLogger.warn("HookGate", f"JS injection failed: {e}")
    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 7.95 — QuizGate Injector  (NEW in v8.0)
#  Ensures quiz HTML always has an unlock gate screen before questions.
# ══════════════════════════════════════════════════════════════════════

_QUIZ_GATE_HTML = """
<div id="qanim-quiz-gate" style="
  position:fixed;inset:0;z-index:800;
  background:linear-gradient(135deg,#2d2a6e 0%,#3d3a91 100%);
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  transition:opacity 0.6s ease;
">
  <div style="text-align:center;max-width:400px;padding:0 24px;">
    <div style="font-size:64px;margin-bottom:20px;animation:trophyBounce 1s ease 0.3s both">🏆</div>
    <h2 style="font-size:26px;font-weight:900;color:#f1f5f9;margin:0 0 12px;
      background:linear-gradient(135deg,#a78bfa,#ec4899);
      -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;">
      Quiz Unlocked!
    </h2>
    <p style="font-size:14px;color:#64748b;line-height:1.7;margin:0 0 32px;">
      You've completed the animation.<br>Now test your understanding.
    </p>
    <button id="qanim-quiz-gate-btn" style="
      padding:14px 40px;border-radius:50px;border:none;cursor:pointer;
      background:linear-gradient(135deg,#7c3aed,#ec4899);
      color:#fff;font-size:15px;font-weight:800;letter-spacing:0.3px;
      box-shadow:0 0 40px rgba(124,58,237,0.4);
      font-family:inherit;transition:transform 0.18s,box-shadow 0.18s;
    " onmouseover="this.style.transform='scale(1.05)'"
       onmouseout="this.style.transform='scale(1)'">
      Start Quiz →
    </button>
  </div>
  <style>
    @keyframes trophyBounce {
      from { opacity:0; transform:translateY(-30px) scale(0.7); }
      to   { opacity:1; transform:translateY(0)     scale(1);   }
    }
  </style>
</div>
"""

_QUIZ_GATE_JS = r"""
<script id="qanim-quiz-gate-controller">
(function initQuizGate() {
  'use strict';
  document.addEventListener('DOMContentLoaded', function() {
    var gate = document.getElementById('qanim-quiz-gate');
    var gateBtn = document.getElementById('qanim-quiz-gate-btn');
    if (!gate || !gateBtn) return;
    /* If AI already built its own gate, remove ours to avoid double gate */
    var aiGate = document.getElementById('quiz-gate');
    if (aiGate && aiGate !== gate) { gate.remove(); return; }
    gateBtn.addEventListener('click', function() {
      gate.style.opacity = '0';
      gate.style.pointerEvents = 'none';
      setTimeout(function() {
        gate.style.display = 'none';
        /* Try to show quiz-main or the card container */
        var main = document.getElementById('quiz-main') || document.querySelector('.card');
        if (main) { main.style.display = ''; main.style.opacity = '0';
          main.style.transition = 'opacity 0.5s ease';
          requestAnimationFrame(function() { main.style.opacity = '1'; }); }
      }, 600);
    });
    /* Hide quiz content until gate is dismissed */
    var quizMain = document.getElementById('quiz-main') || document.querySelector('.card');
    if (quizMain) quizMain.style.display = 'none';
    console.log('[QAnim QuizGate] Gate initialized');
  });
})();
</script>
"""


def inject_quiz_gate(html: str) -> str:
    """
    Injects the Quiz Unlocked gate screen into quiz HTML.
    Gate appears first; student clicks 'Start Quiz →' to reveal questions.
    """
    try:
        body_match = re.search(r'<body[^>]*>', html, re.IGNORECASE)
        if body_match:
            ins = body_match.end()
            html = html[:ins] + '\n' + _QUIZ_GATE_HTML + html[ins:]
        else:
            html = _QUIZ_GATE_HTML + '\n' + html
    except Exception as e:
        QAnimLogger.warn("QuizGate", f"DOM injection failed: {e}")
    try:
        if '</body>' in html:
            html = html.replace('</body>', _QUIZ_GATE_JS + '\n</body>', 1)
        else:
            html += '\n' + _QUIZ_GATE_JS
        QAnimLogger.ok("QuizGate", "Quiz gate injected")
    except Exception as e:
        QAnimLogger.warn("QuizGate", f"JS injection failed: {e}")
    return html


# ── Inline Quiz Styles ────────────────────────────────────────────────
_INLINE_QUIZ_CSS = """
<style id="qanim-inline-quiz-styles">
/* Quiz lives BELOW the animation in normal page flow — not a popup/overlay */
#qanim-inline-quiz {
  display: none;                /* hidden until last scene reached */
  opacity: 0;
  width: 100%; max-width: 820px;
  margin: 32px auto 0;
  font-family: -apple-system,'Segoe UI',Arial,sans-serif;
  transition: opacity 0.6s ease;
  /* Scroll hint separator */
  border-top: 1px solid rgba(124,58,237,0.2);
  padding-top: 32px;
}
#qanim-inline-quiz.visible {
  display: block;
}
/* Scroll-reveal label */
#qanim-inline-quiz .iq-reveal-label {
  text-align: center; font-size: 11px; font-weight: 700;
  letter-spacing: 2px; text-transform: uppercase; color: #4a3580;
  margin-bottom: 20px;
}
#qanim-inline-quiz .iq-card {
  background: linear-gradient(145deg,rgba(45,42,110,0.97),rgba(35,32,90,0.99));
  border: 1px solid rgba(139,92,246,0.35);
  border-radius: 24px; padding: 32px 36px;
  width: 100%; position: relative;
  box-shadow: 0 20px 60px rgba(0,0,0,0.38), 0 0 0 1px rgba(139,92,246,0.14);
}
#qanim-inline-quiz .iq-header {
  text-align: center; margin-bottom: 24px;
}
#qanim-inline-quiz .iq-trophy { font-size: 44px; display: block; margin-bottom: 10px; }
#qanim-inline-quiz .iq-title {
  font-size: 22px; font-weight: 900; margin: 0 0 6px;
  background: linear-gradient(135deg,#a78bfa,#ec4899);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}
#qanim-inline-quiz .iq-sub { font-size: 13px; color: #64748b; }
#qanim-inline-quiz .iq-progress {
  display: flex; gap: 6px; justify-content: center; margin-bottom: 22px;
}
#qanim-inline-quiz .iq-pdot {
  width: 28px; height: 5px; border-radius: 3px;
  background: rgba(255,255,255,0.12); transition: background 0.3s;
}
#qanim-inline-quiz .iq-pdot.done { background: #7c3aed; }
#qanim-inline-quiz .iq-pdot.active { background: #ec4899; }
#qanim-inline-quiz .iq-q { display: none; }
#qanim-inline-quiz .iq-q.active { display: block; }
#qanim-inline-quiz .iq-qtext {
  font-size: 15px; font-weight: 700; color: #f1f5f9; margin-bottom: 16px; line-height: 1.6;
}
#qanim-inline-quiz .iq-opt {
  display: block; width: 100%; text-align: left; padding: 12px 16px;
  margin-bottom: 8px; border-radius: 12px; cursor: pointer;
  border: 1px solid rgba(255,255,255,0.1);
  background: rgba(255,255,255,0.04); color: #cbd5e1; font-size: 13px;
  font-family: inherit; transition: background 0.18s, border-color 0.18s;
}
#qanim-inline-quiz .iq-opt:hover:not([disabled]) {
  background: rgba(124,58,237,0.15); border-color: rgba(124,58,237,0.45);
}
#qanim-inline-quiz .iq-opt.correct { background: rgba(16,185,129,0.2); border-color: #10b981; color: #6ee7b7; }
#qanim-inline-quiz .iq-opt.wrong   { background: rgba(239,68,68,0.2);  border-color: #ef4444; color: #fca5a5; }
#qanim-inline-quiz .iq-expl {
  display: none; font-size: 12px; color: #64748b; margin-top: 10px;
  padding: 10px 14px; border-radius: 10px; background: rgba(255,255,255,0.03);
  border: 1px solid rgba(255,255,255,0.07); line-height: 1.6;
}
#qanim-inline-quiz .iq-expl.show { display: block; }
#qanim-inline-quiz .iq-next-btn {
  display: none; width: 100%; padding: 12px; margin-top: 14px;
  border-radius: 12px; border: none; cursor: pointer;
  background: linear-gradient(135deg,#7c3aed,#db2777); color: #fff;
  font-size: 14px; font-weight: 700; font-family: inherit;
  transition: opacity 0.2s, transform 0.2s;
}
#qanim-inline-quiz .iq-next-btn:hover { opacity: 0.9; transform: translateY(-1px); }
#qanim-inline-quiz .iq-next-btn.show { display: block; }
#qanim-inline-quiz .iq-score {
  display: none; text-align: center; padding: 8px 0;
}
#qanim-inline-quiz .iq-score.show { display: block; }
#qanim-inline-quiz .iq-score-pct {
  font-size: 56px; font-weight: 900; line-height: 1;
  background: linear-gradient(135deg,#a78bfa,#ec4899);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
  margin-bottom: 8px;
}
#qanim-inline-quiz .iq-score-msg { font-size: 14px; color: #64748b; margin-bottom: 20px; }
#qanim-inline-quiz .iq-retry-btn {
  padding: 10px 30px; border-radius: 50px;
  border: 1px solid rgba(124,58,237,0.5); background: rgba(124,58,237,0.15);
  color: #a78bfa; font-size: 13px; font-weight: 700; cursor: pointer; font-family: inherit;
  transition: background 0.2s;
}
#qanim-inline-quiz .iq-retry-btn:hover { background: rgba(124,58,237,0.3); }
/* No close btn needed — quiz is inline, user just scrolls past it */
</style>
"""

_INLINE_QUIZ_JS = r"""
<script id="qanim-inline-quiz-controller">
(function() {
  'use strict';
  /* ── Inline quiz data — embedded by Python injector ── */
  var _iqData = (function() {
    try {
      var tag = document.getElementById('__qanim_inline_quiz_data__');
      if (tag) return JSON.parse(tag.textContent);
    } catch(e) {}
    return null;
  })();

  if (!_iqData || !_iqData.questions || !_iqData.questions.length) {
    console.log('[IQ] No inline quiz data found');
    return;
  }

  var questions = _iqData.questions;
  var totalQ = questions.length;
  var currentQ = 0;
  var score = 0;
  var answered = {};

  document.addEventListener('DOMContentLoaded', function() {
    var panel = document.getElementById('qanim-inline-quiz');
    if (!panel) return;

    /* Build quiz HTML */
    var card = panel.querySelector('.iq-card');
    if (!card) return;

    /* Progress dots */
    var progEl = panel.querySelector('.iq-progress');
    if (progEl) {
      var dotsHtml = '';
      for (var p = 0; p < totalQ; p++) dotsHtml += '<div class="iq-pdot" id="iq-pd-' + p + '"></div>';
      progEl.innerHTML = dotsHtml;
    }

    /* Question cards */
    var qContainer = panel.querySelector('.iq-questions-wrap');
    if (qContainer) {
      var qHtml = '';
      for (var qi = 0; qi < questions.length; qi++) {
        var q = questions[qi];
        qHtml += '<div class="iq-q" id="iq-q-' + qi + '">';
        qHtml += '<div class="iq-qtext">' + _esc(q.question) + '</div>';
        for (var oi = 0; oi < q.options.length; oi++) {
          qHtml += '<button class="iq-opt" data-qi="' + qi + '" data-oi="' + oi + '">' + _esc(q.options[oi]) + '</button>';
        }
        qHtml += '<div class="iq-expl" id="iq-expl-' + qi + '">' + _esc(q.explanation) + '</div>';
        var nextLabel = (qi < totalQ - 1) ? 'Next Question &rarr;' : 'See Results';
        qHtml += '<button class="iq-next-btn" id="iq-next-' + qi + '" data-qi="' + qi + '">' + nextLabel + '</button>';
        qHtml += '</div>';
      }
      qContainer.innerHTML = qHtml;
    }

    /* Score view */
    var scoreEl = panel.querySelector('.iq-score');

    /* Wire option clicks */
    panel.addEventListener('click', function(e) {
      var opt = e.target.closest('.iq-opt');
      if (opt) {
        var qi = parseInt(opt.getAttribute('data-qi'), 10);
        var oi = parseInt(opt.getAttribute('data-oi'), 10);
        if (answered[qi] !== undefined) return;
        answered[qi] = oi;
        var correct = questions[qi].correct;
        var opts = panel.querySelectorAll('.iq-opt[data-qi="' + qi + '"]');
        opts.forEach(function(o) { o.setAttribute('disabled', '1'); o.style.pointerEvents = 'none'; });
        opts[correct].classList.add('correct');
        if (oi !== correct) opts[oi].classList.add('wrong');
        else score++;
        var expl = document.getElementById('iq-expl-' + qi);
        if (expl) expl.classList.add('show');
        var nxt = document.getElementById('iq-next-' + qi);
        if (nxt) nxt.classList.add('show');
        _updatePdot(qi, 'done');
        return;
      }

      var nxtBtn = e.target.closest('.iq-next-btn');
      if (nxtBtn) {
        var qi2 = parseInt(nxtBtn.getAttribute('data-qi'), 10);
        if (qi2 < totalQ - 1) {
          _showQ(qi2 + 1);
        } else {
          _showScore();
        }
        return;
      }

      var retryBtn = e.target.closest('.iq-retry-btn');
      if (retryBtn) { _resetQuiz(); return; }
    });

    _showQ(0);
  });

  function _showQ(idx) {
    currentQ = idx;
    var all = document.querySelectorAll('.iq-q');
    all.forEach(function(el) { el.classList.remove('active'); });
    var target = document.getElementById('iq-q-' + idx);
    if (target) target.classList.add('active');
    _updatePdot(idx, 'active');
  }

  function _updatePdot(idx, cls) {
    var pd = document.getElementById('iq-pd-' + idx);
    if (!pd) return;
    pd.classList.remove('active', 'done');
    pd.classList.add(cls);
  }

  function _showScore() {
    var all = document.querySelectorAll('.iq-q');
    all.forEach(function(el) { el.classList.remove('active'); });
    var scoreEl = document.querySelector('#qanim-inline-quiz .iq-score');
    if (!scoreEl) return;
    var pct = Math.round(score / totalQ * 100);
    var pctEl = scoreEl.querySelector('.iq-score-pct');
    var msgEl = scoreEl.querySelector('.iq-score-msg');
    var stars = pct >= 80 ? '⭐⭐⭐' : pct >= 50 ? '⭐⭐' : '⭐';
    if (pctEl) pctEl.textContent = pct + '%';
    if (msgEl) msgEl.textContent = stars + '  ' + score + ' / ' + totalQ + ' correct';
    scoreEl.classList.add('show');
    for (var p = 0; p < totalQ; p++) _updatePdot(p, 'done');
  }

  function _shuffle(arr) {
    var a = arr.slice();
    for (var i = a.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var tmp = a[j]; a[j] = a[i]; a[i] = tmp;
    }
    return a;
  }

  function _resetQuiz() {
    /* ── CHANGE 4: Send postMessage to host for fresh question regeneration.
       The host (QAnimILM / app layer) should listen for 'qanim:retryQuiz'
       and trigger a new QuizGenerator.generate() call, then re-inject the
       result into the iframe. While waiting, show a loading state.
       If no host handler is registered (standalone usage), fall back to
       local shuffle so the quiz remains functional in all contexts.       */
    var hostRegenRequested = false;

    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({
          type: 'qanim:retryQuiz',
          question: (_iqData && _iqData.question) ? _iqData.question : '',
          category: (_iqData && _iqData.category) ? _iqData.category : ''
        }, '*');
        hostRegenRequested = true;
      }
    } catch(e) { /* cross-origin parent — ignore */ }

    if (hostRegenRequested) {
      /* Show loading state while waiting for host to regenerate */
      var panel3 = document.getElementById('qanim-inline-quiz');
      var qWrap  = panel3 && panel3.querySelector('.iq-questions-wrap');
      var scoreEl3 = panel3 && panel3.querySelector('.iq-score');
      if (scoreEl3) scoreEl3.classList.remove('show');
      if (qWrap) {
        qWrap.innerHTML = '<div style="text-align:center;padding:32px 16px;">'
          + '<div style="font-size:32px;margin-bottom:14px;animation:iq-spin 1s linear infinite;display:inline-block">⟳</div>'
          + '<div style="color:#a78bfa;font-size:14px;font-weight:600">Generating fresh questions...</div>'
          + '<div style="color:#64748b;font-size:11px;margin-top:6px">This takes a moment</div>'
          + '</div>';
      }
      /* Reset progress dots */
      for (var pp = 0; pp < totalQ; pp++) {
        var pd2 = document.getElementById('iq-pd-' + pp);
        if (pd2) { pd2.classList.remove('active', 'done'); }
      }
      /* Add spin keyframe if not present */
      if (!document.getElementById('iq-spin-style')) {
        var st = document.createElement('style');
        st.id = 'iq-spin-style';
        st.textContent = '@keyframes iq-spin { to { transform: rotate(360deg); } }';
        document.head.appendChild(st);
      }
      return; /* Host will reload iframe with new quiz data */
    }

    /* ── Fallback: local shuffle when running standalone (no host) ──
       Shuffles question ORDER and each question's OPTION ORDER.
       This is intentionally different from the host-regenerated path
       which produces entirely new AI-generated questions.             */
    var shuffled = _shuffle(questions.map(function(q) {
      var opts = q.options.slice();
      var correctAnswer = opts[q.correct];
      opts = _shuffle(opts);
      return { question: q.question, options: opts, correct: opts.indexOf(correctAnswer), explanation: q.explanation };
    }));
    questions = shuffled;
    score = 0; currentQ = 0; answered = {};
    /* Re-render question cards */
    var qContainer = document.querySelector('#qanim-inline-quiz .iq-questions-wrap');
    if (qContainer) {
      var qHtml = '';
      for (var qi = 0; qi < questions.length; qi++) {
        var q = questions[qi];
        qHtml += '<div class="iq-q" id="iq-q-' + qi + '">';
        qHtml += '<div class="iq-qtext">' + _esc(q.question) + '</div>';
        for (var oi = 0; oi < q.options.length; oi++) {
          qHtml += '<button class="iq-opt" data-qi="' + qi + '" data-oi="' + oi + '">' + _esc(q.options[oi]) + '</button>';
        }
        qHtml += '<div class="iq-expl" id="iq-expl-' + qi + '">' + _esc(q.explanation) + '</div>';
        var nextLabel2 = (qi < totalQ - 1) ? 'Next Question &rarr;' : 'See Results';
        qHtml += '<button class="iq-next-btn" id="iq-next-' + qi + '" data-qi="' + qi + '">' + nextLabel2 + '</button>';
        qHtml += '</div>';
      }
      qContainer.innerHTML = qHtml;
    }
    var scoreEl = document.querySelector('#qanim-inline-quiz .iq-score');
    if (scoreEl) scoreEl.classList.remove('show');
    for (var p = 0; p < totalQ; p++) {
      var pd = document.getElementById('iq-pd-' + p);
      if (pd) { pd.classList.remove('active','done'); }
    }
    _showQ(0);
    var panel2 = document.getElementById('qanim-inline-quiz');
    if (panel2) setTimeout(function() { panel2.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 50);
  }

  function _esc(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
})();
</script>
"""

_INLINE_QUIZ_DOM = """
<div id="qanim-inline-quiz">
  <div class="iq-reveal-label">✦ Quick Check — Test Your Understanding</div>
  <div class="iq-card">
    <div class="iq-header">
      <span class="iq-trophy">🏆</span>
      <div class="iq-title">Quiz Time!</div>
      <div class="iq-sub">Test your understanding of this concept</div>
    </div>
    <div class="iq-progress"></div>
    <div class="iq-questions-wrap"></div>
    <div class="iq-score">
      <div class="iq-score-pct">0%</div>
      <div class="iq-score-msg">Loading...</div>
      <button class="iq-retry-btn">🔄 Retry Quiz</button>
    </div>
  </div>
</div>
"""


def _build_inline_quiz_data(question: str, category: str) -> str:
    """
    Generates a small deterministic fallback quiz JSON embedded in a script tag.
    Used when the full QuizGenerator HTML is too large to embed inline.
    The real quiz data is generated via AI by QuizGenerator and stripped to
    just the question data array for inline embedding.
    """
    q_safe = question[:200].replace('"', '\\"')
    # Hardcoded fallback quiz — 3 reliable generic questions
    # The AI-generated quiz replaces this via inject_inline_quiz()
    payload = {
        "questions": [
            {
                "question": "What is the primary concept being demonstrated in this animation?",
                "options": [
                    "A fundamental law or principle governing this system",
                    "An exception to standard rules",
                    "A random coincidence",
                    "An outdated theory"
                ],
                "correct": 0,
                "explanation": "The animation demonstrates a core principle. Review the concept scenes to identify it."
            },
            {
                "question": "Why is it important to identify all given quantities before solving?",
                "options": [
                    "It isn't — you can always guess",
                    "To choose the correct formula and avoid missing constraints",
                    "Only relevant in physics problems",
                    "To make the solution look longer"
                ],
                "correct": 1,
                "explanation": "Identifying given quantities ensures you select the right formula and don't overlook constraints."
            },
            {
                "question": "True or False: In this type of problem, the variables are independent of each other.",
                "options": [
                    "True — each variable stands alone",
                    "False — they are related by a governing equation"
                ],
                "correct": 1,
                "explanation": "The variables are related through the governing equation. Changing one affects the others."
            }
        ]
    }
    return (
        '<script type="application/json" id="__qanim_inline_quiz_data__">\n'
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + '\n</script>'
    )


def inject_inline_quiz(html: str, question: str, category: str, quiz_html: str = "") -> str:
    """
    Embeds a self-contained quiz panel INSIDE the solution animation HTML.
    The quiz appears as an overlay when the student reaches the last scene.
    Uses AI-generated quiz data if extractable from quiz_html, else fallback.

    Steps:
    1. Inject quiz data JSON tag into <head>
    2. Inject inline quiz CSS into <head>
    3. Inject quiz DOM before </body>
    4. Inject quiz JS before </body>
    """
    # ── 1. Extract quiz question data from AI quiz_html if possible ──
    quiz_data_tag = ""
    if quiz_html and len(quiz_html) > 200:
        # Try to pull structured quiz data from AI output — look for var questions = [...]
        import re as _re
        q_match = _re.search(
            r'var\s+questions\s*=\s*(\[.+?\]);',
            quiz_html, _re.DOTALL
        )
        if q_match:
            try:
                raw_arr = q_match.group(1)
                q_list  = json.loads(raw_arr)
                payload = {"questions": q_list}
                quiz_data_tag = (
                    '<script type="application/json" id="__qanim_inline_quiz_data__">\n'
                    + json.dumps(payload, ensure_ascii=False, indent=2)
                    + '\n</script>'
                )
                QAnimLogger.ok("InlineQuiz", f"Extracted {len(q_list)} questions from AI quiz")
            except Exception as ex:
                QAnimLogger.warn("InlineQuiz", f"AI quiz parse failed ({ex}) — using fallback")

    if not quiz_data_tag:
        quiz_data_tag = _build_inline_quiz_data(question, category)

    # ── 2. Inject data tag + CSS into <head> ──
    try:
        if '</head>' in html:
            html = html.replace('</head>', quiz_data_tag + '\n' + _INLINE_QUIZ_CSS + '\n</head>', 1)
        else:
            html = quiz_data_tag + '\n' + _INLINE_QUIZ_CSS + '\n' + html
    except Exception as e:
        QAnimLogger.warn("InlineQuiz", f"Head injection failed: {e}")

    # ── 3. Inject DOM inside #container, after the controls div ──
    # Look for the closing </div> of #container — insert quiz just before it.
    # The container always ends with </div>\n</div> (controls then container close).
    # We anchor on id="container" closing pattern.
    try:
        import re as _re2
        # Strategy: find last </div> before </body> that closes #container
        # Reliable anchor: the controls block ends, then container closes.
        # Insert quiz before the final </div> that closes #container.
        # We detect by finding </div>\n</body> or </div></body> pattern.
        injected = False
        # Try: insert after last </div> before </body>
        container_close = _re2.search(r'(</div>\s*)(</body>)', html, _re2.IGNORECASE)
        if container_close:
            ins = container_close.start(2)
            html = html[:ins] + _INLINE_QUIZ_DOM + '\n' + html[ins:]
            injected = True
        if not injected:
            if '</body>' in html:
                html = html.replace('</body>', _INLINE_QUIZ_DOM + '\n</body>', 1)
            else:
                html += '\n' + _INLINE_QUIZ_DOM
    except Exception as e:
        QAnimLogger.warn("InlineQuiz", f"DOM injection failed: {e}")

    # ── 4. Inject JS before </body> ──
    try:
        if '</body>' in html:
            html = html.replace('</body>', _INLINE_QUIZ_JS + '\n</body>', 1)
        else:
            html += '\n' + _INLINE_QUIZ_JS
    except Exception as e:
        QAnimLogger.warn("InlineQuiz", f"JS injection failed: {e}")

    QAnimLogger.ok("InlineQuiz", "Inline quiz panel embedded into solution HTML")
    return html




def _parse_response(raw: str, question: str) -> dict:
    strategies = [
        _parse_direct_json,
        _parse_stripped_json,
        _parse_brace_extracted,
        _parse_field_by_field,
        _parse_bare_html,
    ]
    for i, strategy in enumerate(strategies):
        try:
            result = strategy(raw, question)
            if result:
                QAnimLogger.ok("Parser", f"Strategy {i+1} ({strategy.__name__}) succeeded")
                return result
        except Exception as e:
            QAnimLogger.warn("Parser", f"Strategy {i+1} ({strategy.__name__}) failed: {e}")

    QAnimLogger.error("Parser", "All strategies failed — returning empty result")
    return {
        "title":          f"Animation: {question[:50]}",
        "explanation":    "Parse failed",
        "animation_code": "",
        "solution_steps": [],
        "final_answer":   "",
        "key_insight":    "",
    }

def _parse_direct_json(raw, question):
    data = json.loads(raw)
    return _normalize_parsed(data, question)

def _parse_stripped_json(raw, question):
    stripped = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE).strip()
    data = json.loads(stripped)
    return _normalize_parsed(data, question)

def _parse_brace_extracted(raw, question):
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m: return None
    data = json.loads(m.group(0))
    return _normalize_parsed(data, question)

def _parse_field_by_field(raw, question):
    def extract_string(field):
        pat = r'"' + re.escape(field) + r'"\s*:\s*"((?:[^"\\]|\\.)*)"'
        m = re.search(pat, raw)
        return _unescape_json_string(m.group(1)) if m else ""

    def extract_array(field):
        pat = r'"' + re.escape(field) + r'"\s*:\s*\[(.*?)\]'
        m = re.search(pat, raw, re.DOTALL)
        if not m: return []
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
        return [_unescape_json_string(s) for s in items]

    code = _extract_animation_code_field(raw)
    if not code: return None

    return {
        "title":           extract_string("title") or f"Animation: {question[:50]}",
        "explanation":     extract_string("explanation") or "Interactive animation",
        "animation_type":  extract_string("animation_type"),
        "design_strategy": extract_string("design_strategy"),
        "solution_steps":  extract_array("solution_steps"),
        "final_answer":    extract_string("final_answer"),
        "key_insight":     extract_string("key_insight"),
        "animation_code":  code,
    }

def _extract_animation_code_field(raw: str) -> str:
    key_pos = raw.find('"animation_code"')
    if key_pos == -1: return ""
    colon_pos = raw.find(':', key_pos)
    if colon_pos == -1: return ""
    after_colon = raw[colon_pos + 1:].lstrip()
    if not after_colon.startswith('"'): return ""
    content = after_colon[1:]
    end = _find_json_string_end(content)
    if end == -1: return ""
    return _unescape_json_string(content[:end])

def _parse_bare_html(raw, question):
    for marker in ['<!DOCTYPE html>', '<html', '<svg']:
        idx = raw.find(marker)
        if idx != -1:
            end = raw.rfind('</html>')
            code = raw[idx:end + 7] if end != -1 else raw[idx:]
            if len(code) > 200:
                return {
                    "title":          f"Animation: {question[:50]}",
                    "explanation":    "Interactive animation",
                    "animation_code": code.strip(),
                    "solution_steps": [],
                    "final_answer":   "",
                    "key_insight":    "",
                }
    return None

def _normalize_parsed(data: dict, question: str) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Not a dict")
    result = {
        "title":           str(data.get("title") or "").strip() or f"Animation: {question[:50]}",
        "explanation":     str(data.get("explanation") or "").strip() or "Interactive animation",
        "animation_type":  str(data.get("animation_type") or "").strip(),
        "design_strategy": str(data.get("design_strategy") or "").strip(),
        "animation_code":  str(data.get("animation_code") or "").strip(),
        "final_answer":    str(data.get("final_answer") or "").strip(),
        "key_insight":     str(data.get("key_insight") or "").strip(),
    }
    sol = data.get("solution_steps")
    result["solution_steps"] = sol if isinstance(sol, list) else []
    return result

def _find_json_string_end(s: str) -> int:
    i = 0
    while i < len(s):
        if s[i] == '\\': i += 2
        elif s[i] == '"': return i
        else: i += 1
    return -1

def _unescape_json_string(s: str) -> str:
    return (s.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
             .replace('\\r', '\r').replace("\\'", "'").replace('\\\\', '\\'))


# ══════════════════════════════════════════════════════════════════════
#  MODULE 9 — Full Generation Pipeline  (v7.0 — Four-Stage Concurrent)
# ══════════════════════════════════════════════════════════════════════

async def _generate_concept_animation(question: str, category: str) -> str:
    """
    STAGE 2 — Pure concept animation (no solution, no answer).
    Injects notes system into the concept HTML.
    """
    QAnimLogger.info("ConceptPipeline", f"START  category={category}")
    prompt = _build_concept_prompt(question, category)

    try:
        # CONCEPT_MODEL = claude-sonnet-4.5 — complex SVG animation generation
        # system prompt is cached (large static text, saves ~90% on repeated calls)
        msg = client.messages.create(
            model=CONCEPT_MODEL,
            max_tokens=MAX_TOK_CONCEPT,
            system=[{"type": "text", "text": SYSTEM_CONCEPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}]
        )
        raw         = msg.content[0].text.strip()
        stop_reason = msg.stop_reason
        QAnimLogger.info("ConceptAI", f"model={CONCEPT_MODEL}  stop_reason={stop_reason}  raw_len={len(raw)}")
        if stop_reason == "max_tokens":
            QAnimLogger.warn("ConceptAI", "Hit max_tokens — may be truncated!")
    except Exception as e:
        QAnimLogger.error("ConceptAI", f"API call failed: {e}")
        return RecoveryEngine.fallback_html(question, f"Concept AI error: {e}")

    # Try parsing with concept_code key first, then animation_code
    raw_for_concept = raw.replace('"concept_code"', '"animation_code"')
    parsed_c = _parse_response(raw_for_concept, question)
    concept_html = parsed_c.get("animation_code", "").strip()

    if not concept_html:
        for marker in ['<!DOCTYPE html>', '<html', '<svg']:
            idx = raw.find(marker)
            if idx != -1:
                end = raw.rfind('</html>')
                concept_html = raw[idx:end + 7] if end != -1 else raw[idx:]
                break

    if not concept_html:
        QAnimLogger.error("ConceptParser", "Could not extract concept_code")
        return RecoveryEngine.fallback_html(question, "Concept parse failed")

    try:
        GenerationValidator.validate(concept_html, require_svg=True)
    except ValidationError as e:
        QAnimLogger.warn("ConceptValidator", f"Validation failed: {e}")
        if '<svg' in concept_html and len(concept_html) > 200:
            concept_html = RecoveryEngine.partial_html(question, concept_html)
            try:
                GenerationValidator.validate(concept_html, require_svg=True)
                QAnimLogger.ok("ConceptValidator", "Partial recovery succeeded")
            except ValidationError as e2:
                return RecoveryEngine.fallback_html(question, str(e2))
        else:
            return RecoveryEngine.fallback_html(question, str(e))

    concept_html = HtmlSanitizer.sanitize(concept_html)
    concept_html = inject_infrastructure(concept_html)
    concept_html = inject_notes_system(concept_html, question)  # ← NEW in v7
    concept_html = inject_step_controller(concept_html)          # ← NEW: manual step safety net

    QAnimLogger.ok("ConceptPipeline", f"DONE — len={len(concept_html):,}")
    return concept_html


async def generate_question_animation(question: str) -> dict:
    """
    FOUR-STAGE CONCURRENT PIPELINE (v7.0):

    Stage 0 — ToFind Extraction   (sync, no AI)
    Stage 1 — Hook Animation      (AI: HookGenerator)      ┐
    Stage 2 — Concept Animation   (AI: concept engine)     ├ concurrent
    Stage 3 — Solution Animation  (AI: main engine)        │
    Stage 4 — Quiz Generation     (AI: QuizGenerator)      ┘

    Post-processing (solution HTML only):
      Validate → Sanitize → Inject infrastructure
      → Inject solution → Inject ToFind → Inject notes
      → Final validation

    Returns extended result dict with all four HTML outputs.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("Question cannot be empty")

    short_q = question[:80] + ("..." if len(question) > 80 else "")
    QAnimLogger.info("Pipeline", f"START v9 — '{short_q}'")
    QAnimLogger.info("Pipeline", f"Hook/Quiz: {HOOK_MODEL}  Concept/Solution: {SOLUTION_MODEL}  MaxTokens: {MAX_TOK}")

    # ── Stage 0: ToFind (sync) ──────────────────────────────────────
    to_find_targets = ToFindExtractor.extract(question)
    QAnimLogger.info("Pipeline", f"ToFind targets: {to_find_targets}")

    # ── Classify ────────────────────────────────────────────────────
    category = _classify_topic(question)
    QAnimLogger.info("Classifier", f"Category: {category}")

    # ── Build solution prompt ────────────────────────────────────────
    solution_prompt = _build_prompt(question, category)

    # ── Stage 1-4: Four concurrent AI calls ─────────────────────────
    QAnimLogger.info("Pipeline", "Launching 4 concurrent AI stages…")

    async def _run_solution_ai() -> str:
        try:
            # SOLUTION_MODEL = claude-sonnet-4.5 — premium SVG animation quality
            # system prompt is cached (large static text, saves ~90% on repeated calls)
            msg = client.messages.create(
                model=SOLUTION_MODEL,
                max_tokens=MAX_TOK,
                system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": solution_prompt}]
            )
            raw = msg.content[0].text.strip()
            QAnimLogger.info("SolutionAI",
                             f"model={SOLUTION_MODEL}  stop_reason={msg.stop_reason}  len={len(raw)}")
            if msg.stop_reason == "max_tokens":
                QAnimLogger.warn("SolutionAI", "Hit max_tokens — may be truncated!")
            return raw
        except Exception as e:
            QAnimLogger.error("SolutionAI", f"API failed: {e}")
            raise

    try:
        hook_html, concept_html, sol_raw, quiz_html = await asyncio.gather(
            HookGenerator.generate(question, category),
            _generate_concept_animation(question, category),
            _run_solution_ai(),
            QuizGenerator.generate(question, category),
        )
    except Exception as e:
        QAnimLogger.error("Pipeline", f"Concurrent generation failed: {e}")
        return _build_failure_result(question, f"API error: {e}")

    # ── Parse solution ───────────────────────────────────────────────
    result = _parse_response(sol_raw, question)
    result["category"]               = category
    result["engine_version"]         = "v9.0"
    result["hook_animation_code"]    = hook_html      # NEW
    result["concept_animation_code"] = concept_html
    result["quiz_html"]              = quiz_html      # NEW
    result["to_find"]                = to_find_targets
    result.setdefault("solution_steps", [])
    result.setdefault("final_answer",   "")
    result.setdefault("key_insight",    "")

    html = result.get("animation_code", "")

    # ── Validate solution HTML (strict) ─────────────────────────────
    try:
        GenerationValidator.validate(html, require_svg=True)
    except ValidationError as e:
        QAnimLogger.warn("Validator", f"Strict validation failed: {e}")
        if '<svg' in html and len(html) > 200:
            QAnimLogger.warn("Validator", "Attempting partial recovery…")
            html = RecoveryEngine.partial_html(question, html)
            try:
                GenerationValidator.validate(html, require_svg=True)
                QAnimLogger.ok("Validator", "Partial recovery succeeded")
            except ValidationError as e2:
                QAnimLogger.error("Validator", f"Recovery failed: {e2}")
                result["animation_code"] = RecoveryEngine.fallback_html(question, str(e2))
                result["render_status"]  = "fallback"
                return result
        else:
            result["animation_code"] = RecoveryEngine.fallback_html(question, str(e))
            result["render_status"]  = "fallback"
            return result

    # ── Post-processing: sanitize + inject all systems ───────────────
    html = HtmlSanitizer.sanitize(html)
    html = inject_infrastructure(html)
    html = inject_solution_system(
        html    = html,
        steps   = result["solution_steps"],
        answer  = result["final_answer"],
        insight = result["key_insight"],
    )
    html = inject_to_find_system(html, to_find_targets)
    html = inject_notes_system(html, question)   # ← NEW in v7
    html = inject_inline_quiz(html, question, category, quiz_html)  # ← NEW v8.1: quiz embedded inline
    html = inject_step_controller(html)          # ← NEW: manual step safety net (must be LAST)

    # ── Quiz gate injection (standalone quiz_html) ───────────────────
    quiz_html = inject_quiz_gate(quiz_html)      # ← NEW: quiz unlock gate

    # ── Final validation ─────────────────────────────────────────────
    try:
        GenerationValidator.validate(html, require_svg=True)
    except ValidationError as e:
        QAnimLogger.warn("FinalValidator", f"Post-injection validation: {e} — continuing")

    result["animation_code"]         = html
    result["quiz_html"]              = quiz_html  # updated with gate injection
    result["render_status"]          = "ok"
    result["render_order"]           = ["hook_animation_code", "concept_animation_code", "animation_code", "quiz_html"]

    QAnimLogger.ok("Pipeline", (
        f"DONE v9 — '{result['title']}' "
        f"hook={len(hook_html):,} "
        f"concept={len(concept_html):,} "
        f"solution={len(html):,} "
        f"quiz={len(quiz_html):,} "
        f"steps={len(result['solution_steps'])} "
        f"to_find={result['to_find']}"
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
        "hook_animation_code":    fallback,
        "concept_animation_code": fallback,
        "quiz_html":              fallback,
        "solution_steps":         [],
        "final_answer":           "",
        "key_insight":            "",
        "to_find":                [],
        "category":               "UNKNOWN",
        "engine_version":         "v9.0",
        "render_status":          "error",
    }


# ── Sync wrapper ─────────────────────────────────────────────────────
def generate_question_animation_sync(question: str) -> dict:
    return asyncio.run(generate_question_animation(question))


# ══════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPTS + PROMPT BUILDERS  (enhanced for v7.0)
# ══════════════════════════════════════════════════════════════════════

SYSTEM = """You are QAnim v7 — a cinematic SVG motion designer and educational animation engineer.

YOUR MISSION: Turn any student question into a PREMIUM, self-contained SVG animation.
The output must feel like: Khan Academy × Apple UI × WWDC keynote × motion design studio.

═══════════════════════════════════════════════════════
CRITICAL: DO NOT REVEAL THE ANSWER IN THE ANIMATION
═══════════════════════════════════════════════════════
The animation is a CONCEPTUAL VISUALIZATION LAYER only.
- NEVER show the final numerical answer in the animation body
- NEVER reveal complete solution steps during animation
- ONLY teach the concept, show relationships, build intuition
- The answer is revealed via the solution panel (auto-injected by post-processor)

═══════════════════════════════════════════════════════
OUTPUT FORMAT — STRICT (no markdown fences)
═══════════════════════════════════════════════════════
{
  "animation_type": "concise type label",
  "design_strategy": "2-4 sentences describing visual approach",
  "solution_steps": ["Step 1: ...", "Step 2: ...", "Step 3: ...", "Step 4: ..."],
  "final_answer": "The complete, precise final answer in 1-2 sentences.",
  "key_insight": "One memorable conceptual insight in 1 sentence.",
  "animation_code": "COMPLETE SELF-CONTAINED HTML FILE AS A SINGLE JSON STRING"
}

═══════════════════════════════════════════════════════
CRITICAL SAFETY RULES FOR animation_code
═══════════════════════════════════════════════════════
✅ Must be valid JSON string: escape \\" → \\\\", newline → \\\\n, backslash → \\\\\\\\
✅ Contains complete <!DOCTYPE html>...</html>
✅ Self-contained: NO external fonts, NO CDN links, NO imports
✅ NO document.write() — forbidden
✅ NO backtick template literals in JS
✅ All SVG must have xmlns="http://www.w3.org/2000/svg"
✅ All <script> and <svg> tags must be balanced
✅ Solution panel DOM must be present: #sol-backdrop, #sol-panel, #sol-close,
   #sol-steps-container, #sol-answer-text, #sol-insight-text
✅ Solution panel must be EMPTY (steps injected by post-processor)
✅ Include: #prevbtn, #nextbtn, #dots for scene navigation
✅ Include: #qstrip .qtext for question display

═══════════════════════════════════════════════════════
VISUAL STANDARDS — v7.0 CINEMATIC QUALITY
═══════════════════════════════════════════════════════
BACKGROUND: Brighter premium indigo (#2d2a6e or #3d3a91) — DO NOT use #1e1b4b (too dark)
TYPOGRAPHY: -apple-system, 'Segoe UI', Arial, sans-serif
SVG: viewBox="0 0 800 500"
COLOR PALETTES:
  PHYSICS:  #3b5bdb (royal blue) + #e64980 (crimson pink)
  MATH:     #7c3aed (electric purple) + #db2777 (hot pink)
  BIO:      #16a34a (emerald) + #ca8a04 (amber)
  PROCESS:  #059669 (teal) + #0284c7 (sky blue)
  ABSTRACT: #d97706 (orange) + #7c3aed (purple)
  MIXED:    #0284c7 (blue) + #7c3aed (purple)

ANIMATION TECHNIQUES:
- stroke-dashoffset reveal for paths and arrows
- fade + translateY rise-in for labels and cards
- spring scale-in (cubic-bezier(0.34,1.56,0.64,1)) for heroes
- Glow pulse: SVG filter + periodic opacity animation
- Sequential JS setTimeout orchestration (NOT CSS animation-delay)
- SVG feGaussianBlur for glow effects
- Gradient fills on all major shapes
- Animated dashed borders for emphasis
- Motion trails via multiple offset copies with decreasing opacity

SCENE STRUCTURE:
- 3-5 scenes per animation
- Each scene: establish → animate → label → pause
- Scene groups: <g id="scene-N"> with opacity toggle
- Smooth fade between scenes (300ms opacity transition)
- MANUAL STEP CONTROL — CRITICAL:
  * NO auto-advance timers between scenes — NO setInterval, NO setTimeout that calls showScene
  * ALL navigation functions MUST be on window (not local scope):
      window.currentStep = 0;
      window.showScene = function(n) { ... };
      window.animateScene0 = function() { ... };
      window.animateScene1 = function() { ... };
      ... (one per scene)
      window.nextStep = function() { ... };
      window.prevStep = function() { ... };
  * #nextbtn onclick calls window.nextStep(); #prevbtn onclick calls window.prevStep()
  * showScene(n) must: hide all scenes, show scene n, call animateSceneN()
  * animateSceneN() runs that scene's internal setTimeout animations ONLY (no showScene call inside)
  * Each scene's internal animations fire once when it becomes active
  * Dots (#dots .dot elements) get class 'active' toggled per currentStep
  * Scene 0 animates automatically on DOMContentLoaded via window.showScene(0)
  * All other scenes wait for #nextbtn click"""

SYSTEM_CONCEPT = """You are QAnim Concept Engine v7 — a cinematic SVG educational animator.

YOUR ONLY MISSION: Create a premium, self-contained educational animation that visually
teaches the CONCEPT behind the question. Do NOT show the answer. Do NOT reveal solution
steps. The student should watch and understand the concept, not get the answer.

Think: Apple WWDC × Khan Academy × 3Blue1Brown visual style.

═══════════════════════════════════════════════════════
OUTPUT FORMAT — STRICT (no markdown fences)
═══════════════════════════════════════════════════════
{
  "animation_type": "concise type label",
  "design_strategy": "2-4 sentences describing visual approach",
  "concept_code": "COMPLETE SELF-CONTAINED HTML FILE AS A SINGLE JSON STRING"
}

═══════════════════════════════════════════════════════
CRITICAL SAFETY RULES FOR concept_code
═══════════════════════════════════════════════════════
✅ Must be valid JSON string: escape \\" → \\\\", newline → \\\\n, backslash → \\\\\\\\
✅ Contains complete <!DOCTYPE html>...</html>
✅ Self-contained: NO external fonts, NO CDN links, NO imports
✅ NO document.write() — forbidden
✅ NO backtick template literals in JS
✅ All SVG must have xmlns="http://www.w3.org/2000/svg"
✅ All <script> and <svg> tags must be balanced
✅ DO NOT include solution panel DOM elements
✅ DO NOT include a "View Solution" button
✅ Include: #prevbtn, #nextbtn, #dots, #qstrip .qtext

═══════════════════════════════════════════════════════
CONCEPT ANIMATION RULES — CINEMATIC QUALITY
═══════════════════════════════════════════════════════
✅ Brighter premium indigo background (#2d2a6e or #3d3a91 — do NOT use #1e1b4b)
✅ Vivid accent colors matching category palette
✅ 3-5 scenes of progressive conceptual revelation
✅ Scene 1: Establish visual context / "hook visual"
✅ Scene 2-3: Animate the MECHANISM (how/why it works)
✅ Scene 4: Key relationship visualization
✅ Scene 5 (optional): Real-world connection visual
✅ Motion hierarchy: hero → supporting → labels
✅ Every label should ANIMATE IN (not appear instantly)
✅ MANUAL STEP CONTROL — CRITICAL:
   * NO auto-advance timers between scenes
   * ALL navigation on window: window.showScene, window.animateSceneN, window.nextStep, window.prevStep
   * showScene(n) hides all scenes, shows scene n, calls animateSceneN()
   * animateSceneN() fires only internal setTimeout animations for scene n (never calls showScene)
   * #nextbtn onclick → window.nextStep(); #prevbtn onclick → window.prevStep()
   * Scene 0 starts on DOMContentLoaded via window.showScene(0)
✅ Glow effects using SVG feGaussianBlur
✅ Gradient-filled shapes, never flat colors
✅ Animated arrows with arrowhead markers
❌ NO final numerical answer
❌ NO solution panel DOM elements
❌ NO static boring diagrams"""

DESIGN_SYSTEM = """
TYPOGRAPHY: font-family: -apple-system, 'Segoe UI', Arial, sans-serif
SVG viewBox: "0 0 800 500"
BACKGROUNDS: #2d2a6e (brighter indigo — v9 default), #3d3a91 (vivid indigo), #312e81 (rich indigo)
  DO NOT use #1e1b4b — too dark. Use #2d2a6e or brighter as the base.
COLOR PALETTES:
  PHYSICS=#3b5bdb/#e64980 | MATH=#7c3aed/#db2777
  BIOLOGY=#16a34a/#ca8a04 | PROCESS=#059669/#0284c7
  ABSTRACT=#d97706/#7c3aed | MIXED=#0284c7/#7c3aed
GLASSMORPHISM: rgba(255,255,255,0.08) backgrounds, border rgba(139,92,246,0.30)
GLOW: box-shadow 0 0 40px rgba(109,40,217,0.35) for cards
"""

SVG_TECHNIQUES = """
KEY TECHNIQUES:
- stroke-dashoffset path reveal on arrows/curves
- fade+translateY rise for labels (opacity 0→1, transform translateY(20px)→0)
- spring scale-in: cubic-bezier(0.34,1.56,0.64,1) for hero elements
- Glow: SVG filter feGaussianBlur + feComposite
- Sequential JS setTimeout (NOT CSS animation-delay attributes)
- Gradient fills: linearGradient or radialGradient for every major shape
- Particle dots: small animated circles with staggered opacity pulses
- Dashed animated borders: stroke-dasharray + stroke-dashoffset animation
"""

STRATEGY_TEMPLATES = {
    "VISUAL_PHYSICS": (
        "Dynamic force/motion diagram: draw the physical setup with gradient shapes, "
        "animate force vectors with glowing arrowheads, show trajectory arc with "
        "stroke-dashoffset reveal, reveal formula symbols progressively. "
        "Cinematic zoom effect simulation via SVG transform scale."
    ),
    "PROCESS_BASED": (
        "Sequential process nodes connected by animated traveling-dot paths. "
        "Each node reveals with spring scale-in, highlights its mechanism, "
        "then dims as the next activates. Progress bar draws across bottom."
    ),
    "MATHEMATICAL": (
        "Coordinate axes draw in with glow, function curve traces left-to-right with "
        "gradient color trail. Shaded region pulses with opacity. Formula symbols "
        "materialize one token at a time with staggered fade-rise."
    ),
    "BIOLOGICAL": (
        "Organic cell/molecule shapes with gradient fills and soft glow. Animated "
        "process arrows trace the biological pathway with stroke-dashoffset. "
        "Color-coded structures appear sequentially with labels bouncing in."
    ),
    "ABSTRACT": (
        "Visual metaphor rendered: scales for balance, Venn circles for overlap, "
        "network graph for relationships. Concept dimensions animate as visual zones "
        "with pulsing connectors showing inter-relationships."
    ),
    "MIXED": (
        "Split-zone canvas with diagonal gradient divider. Left zone: physical/visual "
        "system animation. Right zone: formula/data visualization. Center connector "
        "pulses data between zones."
    ),
}

CONCEPT_STRATEGY_TEMPLATES = {
    "VISUAL_PHYSICS": (
        "Cinematic force diagram: animate setup with gradient shapes, draw glowing "
        "force vectors, show trajectory arc via stroke-dashoffset. End on dramatic "
        "zoom to key variable — do NOT solve it."
    ),
    "PROCESS_BASED": (
        "Sequential node graph: each stage spring-scales in, highlights mechanism, "
        "dims as next activates. Traveling dot shows flow direction."
    ),
    "MATHEMATICAL": (
        "Axes draw in, function curve traces with glow trail, shaded region pulses. "
        "Formula tokens materialize one at a time with stagger."
    ),
    "BIOLOGICAL": (
        "Organic shapes with soft gradient fills. Process arrows trace pathway "
        "via stroke-dashoffset. Sequential color-coded component reveals."
    ),
    "ABSTRACT": (
        "Physical analogy: scales, spectrum bars, Venn circles. Concept zones "
        "animate separately with pulsing bridge connectors."
    ),
    "MIXED": (
        "Split canvas: physical animation left, data/formula animation right. "
        "Central pulsing bridge connects zones."
    ),
}

FALLBACK_RULES = """
IF STUCK: Use one of these premium fallback layouts:
1. CARD-REVEAL: 3-4 glassmorphism cards with gradient borders, fade+rise staggered
2. TIMELINE: Horizontal glowing line draws, events spring-scale in at nodes
3. CONCEPT-MAP: Central glowing node, branch lines draw, satellite nodes appear
4. DATA-BARS: Animated bar chart with gradient fills and label reveals
NEVER: flat colors, static text, placeholder comments, boring white backgrounds.
"""

HTML_SHELL_NOTE = """
REQUIRED HTML STRUCTURE (solution panel DOM must exist but be EMPTY):
- #sol-backdrop, #sol-panel (glassmorphism modal), #sol-close
- #sol-steps-container (empty — steps injected by post-processor)
- #sol-answer-text (empty), #sol-insight-text (empty)
- #sol-answer-card, #sol-insight-card (wrapping divs with CSS transitions)
- .sol-step class for step items (with 'visible' class toggle)
- .formula class for highlighted formula spans
- Matching CSS for .sol-step, .sol-step.visible, .formula, etc.
- All scenes in <g id="scene-N"> groups
- Navigation: #prevbtn, #nextbtn, #dots
- Question: #qstrip .qtext

SOLUTION PANEL CSS TO INCLUDE (copy these styles):
#sol-backdrop { display:none; position:fixed; inset:0; z-index:900; background:rgba(30,27,75,0.85); backdrop-filter:blur(12px); }
#sol-backdrop.open { display:flex; align-items:center; justify-content:center; }
#sol-panel { background:linear-gradient(145deg,rgba(61,58,145,0.98),rgba(45,42,110,0.99)); border-radius:24px; padding:28px; max-width:580px; width:90vw; max-height:85vh; overflow-y:auto; border:1px solid rgba(139,92,246,0.45); box-shadow:0 40px 100px rgba(0,0,0,0.36); opacity:0; transform:scale(0.96); transition:opacity 0.3s,transform 0.3s cubic-bezier(0.34,1.56,0.64,1); }
#sol-panel.open { opacity:1; transform:scale(1); }
.sol-step { display:flex; gap:14px; padding:14px 0; border-bottom:1px solid rgba(255,255,255,0.06); opacity:0; transform:translateX(-16px); transition:opacity 0.35s ease,transform 0.35s ease; }
.sol-step.visible { opacity:1; transform:translateX(0); }
.sol-step-num { width:28px; height:28px; border-radius:50%; background:linear-gradient(135deg,#7c3aed,#db2777); color:#fff; display:flex; align-items:center; justify-content:center; font-size:12px; font-weight:800; flex-shrink:0; }
.sol-step-text { font-size:13px; color:#e2e8f0; line-height:1.7; }
.formula { background:rgba(124,58,237,0.2); color:#c4b5fd; padding:1px 6px; border-radius:4px; font-family:monospace; }
#sol-answer-card, #sol-insight-card { opacity:0; transition:opacity 0.4s ease; }
#sol-answer-card.visible, #sol-insight-card.visible { opacity:1; }

DO NOT include document.write() anywhere.
DO NOT include external script src= tags.
"""


def _classify_topic(question: str) -> str:
    """Classify question into animation category using keyword scoring only."""
    q = question.lower()
    scores = {
        "BIOLOGICAL":     sum(1 for k in ["cell","dna","rna","protein","photosynthesis","mitosis","enzyme","hormone","gene","organism","bacteria","virus","chromosome","metabolism"] if k in q),
        "MATHEMATICAL":   sum(1 for k in ["integral","derivative","matrix","vector","theorem","equation","polynomial","logarithm","trigonometry","calculus","function","graph","proof"] if k in q),
        "ABSTRACT":       sum(1 for k in ["philosophy","ethics","democracy","capitalism","justice","freedom","psychology","consciousness","society","ideology","culture","politics"] if k in q),
        "PROCESS_BASED":  sum(1 for k in ["how does","how do","step by step","process","algorithm","mechanism","workflow","procedure","stages","works","function","operation"] if k in q),
        "VISUAL_PHYSICS": sum(1 for k in ["force","velocity","acceleration","mass","energy","momentum","gravity","pressure","current","voltage","wave","circuit","newton","friction","torque","field","charge","resistance"] if k in q),
    }
    max_score = max(scores.values())
    if max_score >= 2:
        top = [c for c, s in scores.items() if s == max_score]
        return top[0] if len(top) == 1 else "MIXED"
    if sum(1 for s in scores.values() if s > 0) >= 3:
        return "MIXED"
    # Pick the highest single-score category; fall back to PROCESS_BASED
    # (avoids a costly Sonnet AI call for just 30 output tokens)
    if max_score == 1:
        top = [c for c, s in scores.items() if s == max_score]
        return top[0]
    return "PROCESS_BASED"


def _build_concept_prompt(question: str, category: str) -> str:
    strategy = CONCEPT_STRATEGY_TEMPLATES.get(
        category, CONCEPT_STRATEGY_TEMPLATES["PROCESS_BASED"]
    )
    return f"""Build a CINEMATIC CONCEPT ANIMATION for QAnim v7 Stage 2.

QUESTION: {question}
CATEGORY: {category}
VISUAL STRATEGY: {strategy}

{DESIGN_SYSTEM}
{SVG_TECHNIQUES}
{FALLBACK_RULES}

CONCEPT ANIMATION REQUIREMENTS:
- Premium bright-dark background: #2d2a6e or #3d3a91 (brighter indigo — do NOT use #1e1b4b)
- Glassmorphism cards with rgba(255,255,255,0.08) bg and rgba(139,92,246,0.30) borders
- Vivid accent colors matching category palette
- 3-5 scenes of progressive conceptual revelation
- Scene 1: Establish the visual context (cinematic world-building)
- Scene 2-3: Animate the core mechanism (the "how/why")
- Scene 4: Key relationship / insight visualization
- NO solution, NO final numeric answer
- Question text at top in #qstrip .qtext
- Navigation: #prevbtn, #nextbtn, #dots
- Every element should animate in — no static appearances
- Use SVG glow effects (feGaussianBlur filters) liberally
- Gradient fills on all major shapes

IMPORTANT: Return ONLY the raw JSON object. No markdown. No extra text.
The concept_code field must be a complete <!DOCTYPE html>...</html>
as a properly escaped JSON string."""


def _build_prompt(question: str, category: str) -> str:
    strategy = STRATEGY_TEMPLATES.get(category, STRATEGY_TEMPLATES["PROCESS_BASED"])
    return f"""Build a PREMIUM CINEMATIC SVG animation for QAnim v7.

QUESTION: {question}
CATEGORY: {category}
STRATEGY: {strategy}

{DESIGN_SYSTEM}
{SVG_TECHNIQUES}
{FALLBACK_RULES}
{HTML_SHELL_NOTE}

KEY REMINDERS:
- NEVER show the final answer during the animation body
- NEVER reveal solution steps during animation
- The animation teaches the CONCEPT only
- All answers appear ONLY in the Solution Panel (injected separately)

IMPORTANT: Return ONLY the raw JSON object. No markdown. No extra text.
The animation_code must be a complete <!DOCTYPE html>...</html> document
as a properly escaped JSON string.

Solution data (steps/answer/insight) will be injected automatically by
the post-processor. Include the solution panel DOM shell but leave all
containers EMPTY — do NOT hardcode any solution content."""


# ══════════════════════════════════════════════════════════════════════
#  CLI TEST
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys

    TEST_QUESTIONS = {
        "VISUAL_PHYSICS":  "Two blocks of mass 4kg and 6kg connected by string over pulley. Find acceleration and tension.",
        "PROCESS_BASED":   "How does a 4-stroke internal combustion engine work?",
        "MATHEMATICAL":    "Explain the Fundamental Theorem of Calculus with a visual proof.",
        "BIOLOGICAL":      "How does the human immune system fight a bacterial infection?",
        "ABSTRACT":        "What is the difference between democracy and authoritarianism?",
        "MIXED":           "How does an MRI machine produce images of the human body?",
        "TOFIND_TEST":     "A resistor of 10Ω has 20V across it. Find the current and determine the power dissipated.",
    }

    if len(sys.argv) > 1:
        questions_to_test = {"CUSTOM": " ".join(sys.argv[1:])}
    else:
        key = "TOFIND_TEST"
        questions_to_test = {key: TEST_QUESTIONS[key]}

    for cat, q in questions_to_test.items():
        print("=" * 72)
        print(f"  QAnim v9.0 — Interactive Step-Based Engine — Category: {cat}")
        print(f"  Q: {q[:65]}...")
        print("=" * 72)

        # Quick ToFind smoke test
        print("\n[ToFind Smoke Test]")
        targets = ToFindExtractor.extract(q)
        print(f"  Targets: {targets}")

        result = generate_question_animation_sync(q)

        hook_html     = result.get("hook_animation_code", "")
        concept_html  = result.get("concept_animation_code", "")
        solution_html = result.get("animation_code", "")
        quiz_html     = result.get("quiz_html", "")

        print(f"\nTitle               : {result['title']}")
        print(f"Category            : {result.get('category','N/A')}")
        print(f"Engine              : {result.get('engine_version','N/A')}")
        print(f"Render Status       : {result.get('render_status','N/A')}")
        print(f"[ToFind] Targets    : {result.get('to_find',[])}")
        print(f"[Stage 1] Hook      : {len(hook_html):,} chars")
        print(f"[Stage 2] Concept   : {len(concept_html):,} chars")
        print(f"[Stage 3] Solution  : {len(solution_html):,} chars")
        print(f"[Stage 4] Quiz      : {len(quiz_html):,} chars")

        steps = result.get('solution_steps', [])
        print(f"Solution Steps      : {len(steps)}")
        for i, s in enumerate(steps, 1):
            print(f"  Step {i}: {s[:90]}...")
        print(f"Final Answer        : {result.get('final_answer','')[:120]}")
        print(f"Key Insight         : {result.get('key_insight','')[:100]}")

        slug = cat.lower()

        # Save Stage 1 — Hook Animation
        hook_out = f"q_anim_v70_{slug}_hook.html"
        with open(hook_out, "w", encoding="utf-8") as f:
            f.write(hook_html)
        print(f"\n[Stage 1] Hook saved    : {hook_out}")

        # Save Stage 2 — Concept Animation (with Notes)
        concept_out = f"q_anim_v70_{slug}_concept.html"
        with open(concept_out, "w", encoding="utf-8") as f:
            f.write(concept_html)
        print(f"[Stage 2] Concept saved : {concept_out}")

        # Save Stage 3 — Solution Animation (with Notes + ToFind + Solution)
        solution_out = f"q_anim_v70_{slug}_solution.html"
        with open(solution_out, "w", encoding="utf-8") as f:
            f.write(solution_html)
        print(f"[Stage 3] Solution saved: {solution_out}")

        # Save Stage 4 — Quiz
        quiz_out = f"q_anim_v70_{slug}_quiz.html"
        with open(quiz_out, "w", encoding="utf-8") as f:
            f.write(quiz_html)
        print(f"[Stage 4] Quiz saved    : {quiz_out}\n")
