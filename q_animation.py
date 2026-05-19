"""
q_animation.py  —  QAnim Question Animation Generator  v6.0
=============================================================
╔══════════════════════════════════════════════════════════════╗
║  v6.0 — TWO-STAGE + TO-FIND ARCHITECTURE                    ║
╠══════════════════════════════════════════════════════════════╣
║  WHAT CHANGED vs v5.0:                                      ║
║  ✅ ToFindExtractor — isolated extraction module (NEW)      ║
║  ✅ to_find field in result object (NEW)                    ║
║  ✅ inject_to_find_system() — HTML/JS/CSS injector (NEW)    ║
║  ✅ "To Find" button + glassmorphism modal in animations    ║
║  ✅ ToFind data tag pattern mirrors Solution data tag       ║
║  ✅ All existing architecture fully preserved               ║
╚══════════════════════════════════════════════════════════════╝

APPLIED UPDATES (v6.0 → v6.0-updated):
  [UPDATE 1] Background color changed to light/clean (#F8F9FA)
  [UPDATE 2] Topic/question font size increased (~1.5x)
  [UPDATE 3] Final answer/solution display removed from animation flow
  [UPDATE 4] 15-question quiz section added (3 sets × 5 questions)
  [UPDATE 5] Real-Time Application hook section added as first frame
"""

import anthropic
import json
import re
import asyncio
import html as html_module
from typing import Optional

# ── Client + model ──────────────────────────────────────────────────────
client  = anthropic.Anthropic()
Q_MODEL = "claude-sonnet-4-5"
MAX_TOK = 16000
MAX_TOK_CONCEPT = 12000   # Stage-1 concept animation (no solution data needed)


# ══════════════════════════════════════════════════════════════════════
#  MODULE 1 — QAnimLogger (centralized diagnostics)
# ══════════════════════════════════════════════════════════════════════

class QAnimLogger:
    """Centralized logger. All lifecycle events go through here."""

    PREFIX = "[QAnim v6]"

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
#  Validates raw AI output BEFORE any injection attempt
# ══════════════════════════════════════════════════════════════════════

class ValidationError(Exception):
    pass

class GenerationValidator:
    """
    Validates AI-generated HTML before it ever touches an iframe.
    Raises ValidationError with a descriptive reason on failure.
    """

    # Patterns that will definitely break iframe execution
    DANGEROUS_PATTERNS = [
        (r'document\.write\s*\(', "document.write() is forbidden in generated output"),
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
        ("<script",   "No script block found — animation would be static"),
    ]

    SVG_REQUIRED = [
        ("<svg",  "No SVG element found"),
        ("</svg>","SVG element not closed"),
    ]

    @classmethod
    def validate(cls, html: str, require_svg: bool = True) -> None:
        if not html or not html.strip():
            raise ValidationError("animation_code is empty")

        if len(html) < 500:
            raise ValidationError(f"animation_code suspiciously short ({len(html)} chars) — likely truncated")

        for pattern, reason in cls.REQUIRED_ELEMENTS:
            if pattern not in html:
                raise ValidationError(reason)

        if require_svg:
            for pattern, reason in cls.SVG_REQUIRED:
                if pattern not in html:
                    raise ValidationError(reason)

        for pattern, reason in cls.DANGEROUS_PATTERNS:
            if re.search(pattern, html, re.IGNORECASE):
                QAnimLogger.warn("Validator", f"Dangerous pattern stripped: {reason}")
                # Don't raise — sanitizer will strip it

        # Structural balance check
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
#  Intelligently identifies what the student is asked to find.
#  Fully isolated, fault-tolerant, never raises.
# ══════════════════════════════════════════════════════════════════════

class ToFindExtractor:
    """
    Parses an academic question and returns a deduplicated, student-friendly
    list of the quantities / targets the student must find or determine.
    """

    _TRIGGER_PATTERNS: list[tuple[str, int]] = [
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
        (r'\bwhat\s+would\s+be\s+(?:the\s+)?(.+?)(?=\?|,|;|$)',1),
        (r'\bhow\s+(?:much|many)\s+(.+?)(?=\?|,|;|$)',          1),
        (r'\bprove\s+(?:that\s+)?(.+?)(?=\.|,|;|$)',            1),
        (r'\bshow\s+(?:that\s+)?(.+?)(?=\.|,|;|$)',             1),
        (r'\bexpress\s+(?:the\s+)?(.+?)\s+in\s+terms',         1),
    ]

    _NOISE_PREFIXES: list[str] = [
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

    _ARTICLE_RE = re.compile(r'^(?:the|a|an)\s+', re.IGNORECASE)

    _TRIGGER_VERB_RE = re.compile(
        r'^(?:find|determine|calculate|evaluate|compute|obtain|'
        r'identify|estimate|derive|prove|show|express|solve\s+for)'
        r'\s+(?:the\s+|an?\s+)?',
        re.IGNORECASE
    )

    _MATH_VAR_RE = re.compile(r'^[A-Za-zα-ωΑ-Ω][0-9₀-₉]?$')

    MIN_LEN = 1
    MAX_LEN = 120

    @classmethod
    def extract(cls, question: str) -> list[str]:
        if not question or not question.strip():
            QAnimLogger.warn("ToFindExtractor", "Empty question supplied — returning []")
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
                QAnimLogger.warn("ToFindExtractor",
                                 "No targets matched — trying fallback")
                result = cls._fallback(question)

            QAnimLogger.ok("ToFindExtractor",
                           f"Extracted {len(result)} target(s): {result}")
            return result

        except Exception as exc:
            QAnimLogger.error("ToFindExtractor", f"Unhandled error: {exc}")
            return []

    @classmethod
    def _run_patterns(cls, question: str) -> list[str]:
        found: list[str] = []
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
    def _split_conjunctions(cls, targets: list[str]) -> list[str]:
        result: list[str] = []
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
    def _deduplicate(cls, targets: list[str]) -> list[str]:
        seen:   set[str]  = set()
        result: list[str] = []
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
    def _fallback(cls, question: str) -> list[str]:
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
    """Cleans AI-generated HTML."""

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
                "try {\n"
                + body +
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
    """Produces graceful fallback HTML when the generation pipeline fails."""

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
    background:#F8F9FA; /* [UPDATED] light background */
    font-family:-apple-system,'Segoe UI',Arial,sans-serif;
    display:flex; align-items:center; justify-content:center;
  }}
  .card {{
    background:#fff; border-radius:20px;
    box-shadow:0 20px 60px rgba(0,0,0,0.10);
    padding:36px 40px; max-width:520px; text-align:center;
  }}
  .icon {{ font-size:40px; margin-bottom:16px; }}
  .title {{ font-size:17px; font-weight:800; color:#1e293b; margin-bottom:10px; }}
  .reason {{
    font-size:11px; color:#64748b; background:#f8fafc;
    border-radius:10px; padding:10px 14px; margin:12px 0;
    border:1px solid #e2e8f0; text-align:left; line-height:1.6;
    font-family:monospace;
  }}
  .question {{
    font-size:12px; color:#475569; line-height:1.6; margin-top:10px;
    font-style:italic;
  }}
  .retry-hint {{
    margin-top:18px; font-size:11px; font-weight:700;
    letter-spacing:1.5px; text-transform:uppercase; color:#94a3b8;
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
    background:#F8F9FA;font-family:-apple-system,sans-serif}} /* [UPDATED] light background */
</style>
</head>
<body>
<div style="font-size:11px;color:#64748b;position:fixed;top:8px;left:0;right:0;text-align:center">
  {q_safe}
</div>
{animation_code}
</body></html>"""


# ══════════════════════════════════════════════════════════════════════
#  MODULE 5 — IframeLifecycleManager
# ══════════════════════════════════════════════════════════════════════

IFRAME_RUNTIME_JS = r"""
/* ═══════════════════════════════════════════════════════════
   QAnim IframeLifecycleManager v6 — srcdoc-based safe render
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
    f.style.cssText = [
      'width:100%', 'height:100%', 'border:none',
      'display:block', 'background:transparent'
    ].join(';');
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
          _onRenderError('srcdoc assignment: ' + e.message);
        }
      });
    } catch(e) {
      Log.error('_injectSrcdoc outer error: ' + e);
      _onRenderError('_injectSrcdoc: ' + e.message);
    }
  }

  function _processQueue() {
    if (_rendering || _renderQueue.length === 0) return;
    _rendering = true;
    var task = _renderQueue.shift();
    Log.info('Processing render task. Queue remaining: ' + _renderQueue.length);

    var iframe = _getIframe();
    var timeoutId;
    var done = false;

    function cleanup() {
      if (done) return;
      done = true;
      clearTimeout(timeoutId);
      iframe.onload = null;
      iframe.onerror = null;
    }

    iframe.onload = function() {
      cleanup();
      Log.ok('iframe loaded successfully');
      _currentHtml = task.html;
      _rendering = false;
      if (task.onSuccess) task.onSuccess();
      _processQueue();
    };

    iframe.onerror = function(e) {
      cleanup();
      Log.error('iframe onerror: ' + (e && e.message || 'unknown'));
      _rendering = false;
      if (task.onError) task.onError('iframe load error');
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
      _rendering   = false;
      _resetIframe();
      Log.ok('ILM reset complete');
    },
    getCurrentHtml: function() { return _currentHtml; },
    isRendering: function() { return _rendering; }
  };

  Log.ok('IframeLifecycleManager initialized');
})();
"""


# ══════════════════════════════════════════════════════════════════════
#  MODULE 6 — ErrorFallbackInjector
# ══════════════════════════════════════════════════════════════════════

ERROR_BOUNDARY_HTML = """
<!-- QAnim Error Fallback — always present, hidden unless needed -->
<div id="qanim-error-fallback" style="
  display:none; position:fixed; inset:0; z-index:9999;
  background:rgba(15,23,42,0.88); backdrop-filter:blur(8px);
  align-items:center; justify-content:center;
">
  <div style="
    background:#fff; border-radius:20px; padding:32px 36px;
    max-width:440px; text-align:center;
    box-shadow:0 40px 100px rgba(0,0,0,0.3);
  ">
    <div style="font-size:36px;margin-bottom:14px">⚠️</div>
    <div style="font-size:15px;font-weight:800;color:#1e293b;margin-bottom:8px">
      Animation Error
    </div>
    <div class="qanim-err-msg" style="
      font-size:11px;color:#64748b;background:#f8fafc;
      border-radius:10px;padding:10px 14px;margin:12px 0;
      border:1px solid #e2e8f0;font-family:monospace;
      text-align:left;line-height:1.6;word-break:break-all;
    ">Unknown error</div>
    <button onclick="document.getElementById('qanim-error-fallback').style.display='none'"
      style="
        margin-top:14px;padding:8px 22px;border-radius:50px;border:none;
        background:linear-gradient(135deg,#7c3aed,#db2777);
        color:#fff;font-weight:700;font-size:12px;cursor:pointer;
      ">Dismiss</button>
  </div>
</div>
"""

QANIM_INNER_LOGGER_JS = """
<script>
/* QAnim Inner Logger — visible to animation scripts inside iframe */
window.QLog = {
  info:  function(m) { console.log('[QAnim Inner] ℹ  ' + m); },
  warn:  function(m) { console.warn('[QAnim Inner] ⚠  ' + m); },
  error: function(m) { console.error('[QAnim Inner] ✖  ' + m); }
};

/* Global uncaught error catcher inside iframe */
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
    """
    Injects QAnim infrastructure into generated HTML:
    - Error fallback UI
    - Inner logger + global error catcher
    """
    html = re.sub(
        r'(<body[^>]*>)',
        r'\1\n' + ERROR_BOUNDARY_HTML,
        html, count=1, flags=re.IGNORECASE
    )
    first_script = re.search(r'<script(?:\s[^>]*)?>(?!.*type\s*=\s*["\']application/json)', html, re.IGNORECASE)
    if first_script:
        pos = first_script.start()
        html = html[:pos] + QANIM_INNER_LOGGER_JS + '\n' + html[pos:]
    else:
        html = html.replace('</body>', QANIM_INNER_LOGGER_JS + '\n</body>', 1)

    QAnimLogger.ok("Infrastructure", "Error fallback + inner logger injected")
    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 6.5 — ToFind Injection System
# ══════════════════════════════════════════════════════════════════════

def _build_to_find_data_tag(targets: list) -> str:
    payload = {"targets": [str(t) for t in (targets or [])]}
    return (
        '<script type="application/json" id="__tofind_data__">\n'
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + '\n</script>'
    )


_TO_FIND_DOM = """
<!-- ═══════════════════════════════════════════════════════════════
     QAnim ToFind Panel v6 — glassmorphism modal
     ═══════════════════════════════════════════════════════════════ -->

<div id="tofind-backdrop" aria-hidden="true"></div>

<aside id="tofind-panel" role="dialog"
       aria-labelledby="tofind-heading" aria-hidden="true">

  <div class="tf-glow-ring"></div>

  <div class="tf-header">
    <div class="tf-header-left">
      <div class="tf-icon-wrap">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
             xmlns="http://www.w3.org/2000/svg">
          <circle cx="11" cy="11" r="7" stroke="currentColor"
                  stroke-width="2.2" stroke-linecap="round"/>
          <path d="M16.5 16.5L21 21" stroke="currentColor"
                stroke-width="2.2" stroke-linecap="round"/>
        </svg>
      </div>
      <span id="tofind-heading" class="tf-title">To Find</span>
    </div>
    <button id="tofind-close" class="tf-close-btn"
            aria-label="Close To Find panel">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
           xmlns="http://www.w3.org/2000/svg">
        <path d="M18 6L6 18M6 6l12 12" stroke="currentColor"
              stroke-width="2.5" stroke-linecap="round"/>
      </svg>
    </button>
  </div>

  <p class="tf-subtitle">What this question is asking you to determine:</p>

  <div id="tofind-items-container" class="tf-items-container">
  </div>

  <div class="tf-footer">
    <span class="tf-badge">QAnim v6 · Target Identifier</span>
  </div>

</aside>
"""

_TO_FIND_CSS = """
<style id="qanim-tofind-styles">
#tofind-backdrop {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 8000;
  background: rgba(10, 10, 26, 0.72);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  opacity: 0;
  transition: opacity 0.28s ease;
}
#tofind-backdrop.open {
  display: block;
  opacity: 1;
}

#tofind-panel {
  display: flex;
  flex-direction: column;
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -48%) scale(0.96);
  z-index: 8100;
  width: min(480px, 92vw);
  max-height: 82vh;
  border-radius: 24px;
  padding: 28px 28px 22px;
  box-sizing: border-box;
  overflow: hidden;
  background: linear-gradient(
    145deg,
    rgba(22, 22, 44, 0.92) 0%,
    rgba(15, 15, 35, 0.96) 100%
  );
  border: 1px solid rgba(120, 80, 255, 0.30);
  box-shadow:
    0 0  0   1px rgba(120, 80, 255, 0.12),
    0 24px 60px rgba(0, 0, 0, 0.55),
    0  4px 20px rgba(120, 80, 255, 0.18);
  opacity: 0;
  pointer-events: none;
  transition:
    opacity   0.30s ease,
    transform 0.30s cubic-bezier(0.34, 1.56, 0.64, 1);
}
#tofind-panel.open {
  opacity: 1;
  pointer-events: auto;
  transform: translate(-50%, -50%) scale(1);
}

.tf-glow-ring {
  position: absolute;
  top: -60px; right: -60px;
  width: 200px; height: 200px;
  border-radius: 50%;
  background: radial-gradient(
    circle,
    rgba(124, 58, 237, 0.28) 0%,
    transparent 70%
  );
  pointer-events: none;
}

.tf-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  flex-shrink: 0;
}
.tf-header-left {
  display: flex;
  align-items: center;
  gap: 11px;
}
.tf-icon-wrap {
  width: 36px; height: 36px;
  border-radius: 12px;
  background: linear-gradient(135deg, #7c3aed, #4f46e5);
  display: flex; align-items: center; justify-content: center;
  color: #fff;
  flex-shrink: 0;
  box-shadow: 0 4px 14px rgba(124, 58, 237, 0.45);
}
.tf-title {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 17px;
  font-weight: 800;
  color: #f1f5f9;
  letter-spacing: -0.3px;
}

.tf-close-btn {
  width: 32px; height: 32px;
  border-radius: 50%;
  border: 1px solid rgba(255,255,255,0.12);
  background: rgba(255,255,255,0.07);
  color: #94a3b8;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer;
  transition: background 0.18s, color 0.18s, transform 0.18s;
  flex-shrink: 0;
}
.tf-close-btn:hover {
  background: rgba(255,255,255,0.14);
  color: #f1f5f9;
  transform: rotate(90deg);
}

.tf-subtitle {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 11.5px;
  color: #64748b;
  margin: 0 0 18px;
  letter-spacing: 0.1px;
  flex-shrink: 0;
}

.tf-items-container {
  display: flex;
  flex-direction: column;
  gap: 10px;
  overflow-y: auto;
  flex: 1 1 auto;
  padding-right: 4px;
  scrollbar-width: thin;
  scrollbar-color: rgba(124,58,237,0.4) transparent;
}
.tf-items-container::-webkit-scrollbar { width: 4px; }
.tf-items-container::-webkit-scrollbar-track { background: transparent; }
.tf-items-container::-webkit-scrollbar-thumb {
  background: rgba(124,58,237,0.4);
  border-radius: 4px;
}

.tofind-item {
  display: flex;
  align-items: flex-start;
  gap: 13px;
  padding: 14px 16px;
  border-radius: 14px;
  background: rgba(255,255,255,0.04);
  border: 1px solid rgba(124,58,237,0.18);
  opacity: 0;
  transform: translateX(-16px);
  transition: background 0.18s, border-color 0.18s;
}
.tofind-item:hover {
  background: rgba(124,58,237,0.10);
  border-color: rgba(124,58,237,0.38);
}

.tofind-check {
  width: 22px; height: 22px;
  border-radius: 50%;
  background: linear-gradient(135deg, #7c3aed, #4f46e5);
  color: #fff;
  font-size: 12px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  margin-top: 1px;
  box-shadow: 0 2px 8px rgba(124,58,237,0.40);
}

.tofind-text {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 14px;
  font-weight: 600;
  color: #e2e8f0;
  line-height: 1.5;
}

.tofind-empty {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 13px;
  color: #475569;
  text-align: center;
  padding: 24px 0;
  font-style: italic;
}

.tf-footer {
  margin-top: 18px;
  display: flex;
  justify-content: center;
  flex-shrink: 0;
}
.tf-badge {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1.4px;
  text-transform: uppercase;
  color: #334155;
  background: rgba(255,255,255,0.04);
  border: 1px solid rgba(255,255,255,0.07);
  border-radius: 50px;
  padding: 4px 12px;
}

#tofind-btn {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 9px 18px;
  border-radius: 50px;
  border: 1.5px solid rgba(124,58,237,0.55);
  background: rgba(124,58,237,0.12);
  color: #a78bfa;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.2px;
  cursor: pointer;
  transition:
    background  0.22s ease,
    border-color 0.22s ease,
    color        0.22s ease,
    transform    0.18s cubic-bezier(0.34,1.56,0.64,1),
    box-shadow   0.22s ease;
  backdrop-filter: blur(6px);
}
#tofind-btn:hover {
  background: rgba(124,58,237,0.28);
  border-color: rgba(124,58,237,0.85);
  color: #ede9fe;
  transform: translateY(-2px);
  box-shadow: 0 8px 24px rgba(124,58,237,0.35);
}
#tofind-btn:active { transform: translateY(0) scale(0.97); }
#tofind-btn .tf-btn-icon { width: 16px; height: 16px; opacity: 0.85; }

@media (max-width: 540px) {
  #tofind-panel { width: 96vw; padding: 22px 18px 18px; border-radius: 20px; }
  .tf-title { font-size: 15px; }
  .tofind-text { font-size: 13px; }
}
</style>
"""

TO_FIND_JS_MODULE = r"""
(function initToFindSystem() {
  'use strict';

  var toFindOpen = false;
  var _panelBuilt = false;

  function _onReady(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      setTimeout(fn, 0);
    }
  }

  function _el(id) { return document.getElementById(id); }

  function _loadTargets() {
    try {
      var tag = _el('__tofind_data__');
      if (!tag) {
        console.warn('[QAnim ToFind] Data tag #__tofind_data__ not found');
        return [];
      }
      var data = JSON.parse(tag.textContent) || {};
      return Array.isArray(data.targets) ? data.targets : [];
    } catch (e) {
      console.error('[QAnim ToFind] Failed to parse target data:', e);
      return [];
    }
  }

  function _escape(text) {
    if (!text) return '';
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function _buildPanel(targets) {
    if (_panelBuilt) return;
    _panelBuilt = true;

    try {
      var container = _el('tofind-items-container');
      if (!container) return;

      if (!targets || targets.length === 0) {
        container.innerHTML =
          '<div class="tofind-empty">'
          + 'No specific targets were detected in this question.<br>'
          + '<span style="font-size:11px;opacity:0.6;">Read the full question carefully.</span>'
          + '</div>';
        return;
      }

      var html = '';
      for (var i = 0; i < targets.length; i++) {
        html +=
          '<div class="tofind-item" id="tofind-item-' + i + '">'
          + '<div class="tofind-check">&#10003;</div>'
          + '<div class="tofind-text">' + _escape(targets[i]) + '</div>'
          + '</div>';
      }
      container.innerHTML = html;
    } catch (e) {
      console.error('[QAnim ToFind] _buildPanel error:', e);
    }
  }

  function _animateReveal() {
    try {
      var items = document.querySelectorAll('.tofind-item');
      for (var i = 0; i < items.length; i++) {
        (function(el, idx) {
          el.style.opacity = '0';
          el.style.transform = 'translateX(-18px)';
          el.style.transition = 'none';
          setTimeout(function() {
            el.style.transition =
              'opacity 0.32s ease, transform 0.32s cubic-bezier(0.34,1.56,0.64,1)';
            el.style.opacity = '1';
            el.style.transform = 'translateX(0)';
          }, 90 + idx * 95);
        })(items[i], i);
      }
    } catch (e) {
      console.error('[QAnim ToFind] _animateReveal error:', e);
    }
  }

  function openToFind() {
    try {
      var backdrop = _el('tofind-backdrop');
      var panel    = _el('tofind-panel');
      if (!backdrop || !panel) {
        console.error('[QAnim ToFind] DOM elements missing — panel cannot open');
        return;
      }

      var targets = _loadTargets();
      _buildPanel(targets);

      backdrop.classList.add('open');
      panel.classList.add('open');
      panel.setAttribute('aria-hidden', 'false');
      toFindOpen = true;

      setTimeout(_animateReveal, 150);
      console.log('[QAnim ToFind] Panel opened — ' + targets.length + ' target(s)');
    } catch (err) {
      console.error('[QAnim ToFind] openToFind crashed:', err);
    }
  }

  function closeToFind() {
    try {
      var backdrop = _el('tofind-backdrop');
      var panel    = _el('tofind-panel');
      if (backdrop) backdrop.classList.remove('open');
      if (panel) {
        panel.classList.remove('open');
        panel.setAttribute('aria-hidden', 'true');
      }
      toFindOpen = false;
    } catch (err) {
      console.error('[QAnim ToFind] closeToFind crashed:', err);
    }
  }

  window.openToFind   = openToFind;
  window.closeToFind  = closeToFind;
  window.toggleToFind = function() {
    toFindOpen ? closeToFind() : openToFind();
  };

  _onReady(function() {
    try {
      var tfBtn = _el('tofind-btn') || document.querySelector('[data-tofind-btn]');
      if (tfBtn) {
        tfBtn.removeAttribute('onclick');
        tfBtn.addEventListener('click', function(e) {
          e.stopPropagation();
          openToFind();
        });
      }

      var closeBtn = _el('tofind-close');
      if (closeBtn) {
        closeBtn.removeAttribute('onclick');
        closeBtn.addEventListener('click', closeToFind);
      }

      var backdrop = _el('tofind-backdrop');
      if (backdrop) {
        backdrop.removeAttribute('onclick');
        backdrop.addEventListener('click', closeToFind);
      }

      document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && toFindOpen) closeToFind();
      });

      console.log('[QAnim ToFind] System initialized ✓');
    } catch (e) {
      console.error('[QAnim ToFind] Init binding error:', e);
    }
  });

})();
"""

_TO_FIND_BUTTON_HTML = """<button id="tofind-btn" data-tofind-btn aria-haspopup="dialog">
  <svg class="tf-btn-icon" viewBox="0 0 24 24" fill="none"
       xmlns="http://www.w3.org/2000/svg">
    <circle cx="11" cy="11" r="7" stroke="currentColor"
            stroke-width="2.2" stroke-linecap="round"/>
    <path d="M16.5 16.5L21 21" stroke="currentColor"
          stroke-width="2.2" stroke-linecap="round"/>
  </svg>
  To Find
</button>"""


def inject_to_find_system(html: str, targets: list) -> str:
    """Injects the complete "To Find" feature into a generated animation HTML."""

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
        solbtn_match = re.search(
            r'(<button[^>]+id=["\']solbtn["\'][^>]*>.*?</button>)',
            html, re.IGNORECASE | re.DOTALL
        )
        if solbtn_match:
            html = (
                html[:solbtn_match.start()]
                + _TO_FIND_BUTTON_HTML + '\n'
                + solbtn_match.group(0)
                + html[solbtn_match.end():]
            )
            QAnimLogger.ok("ToFindInjector", "ToFind button inserted before #solbtn")
        else:
            html = html.replace(
                '</body>',
                '\n<!-- ToFind button (standalone) -->\n'
                + _TO_FIND_BUTTON_HTML
                + '\n</body>',
                1
            )
            QAnimLogger.warn("ToFindInjector",
                             "#solbtn not found — ToFind button appended before </body>")
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

    QAnimLogger.ok("ToFindInjector",
                   f"Injected {len(targets)} target(s) into animation HTML")
    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 7 — Solution System (unchanged from v5.0)
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
/* ════════════════════════════════════════════════════════════════
   QAnim Solution System v6 — Isolated, Resilient, Error-Bounded
   ════════════════════════════════════════════════════════════════ */
(function initSolutionSystem() {
  'use strict';

  var solutionOpen = false;
  var _solBuilt    = false;

  function _onReady(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      setTimeout(fn, 0);
    }
  }

  function _el(id) { return document.getElementById(id); }

  function _loadData() {
    try {
      var tag = _el('__sol_data__');
      if (!tag) return {};
      return JSON.parse(tag.textContent) || {};
    } catch(e) {
      console.error('[QAnim Sol] Failed to parse solution data:', e);
      return {};
    }
  }

  function _escape(text) {
    if (!text) return '';
    return String(text)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function _highlight(text) {
    var safe = _escape(text);
    return safe.replace(/([A-Za-z_]\w*\s*=\s*[^<&,;.]+)/g,
      '<span class="formula">$1</span>');
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
        html = '<div class="sol-step visible"><div class="sol-step-num">!</div>'
             + '<div class="sol-step-text">Solution steps unavailable.</div></div>';
      } else {
        for (var i = 0; i < steps.length; i++) {
          html += '<div class="sol-step" id="sol-step-' + i + '">'
               +    '<div class="sol-step-num" id="sol-num-' + i + '">' + (i+1) + '</div>'
               +    '<div class="sol-step-text">' + _highlight(steps[i]) + '</div>'
               + '</div>';
        }
      }
      container.innerHTML = html;
      var ansEl = _el('sol-answer-text');
      var insEl = _el('sol-insight-text');
      if (ansEl && data.answer)  ansEl.innerHTML = _highlight(data.answer);
      if (insEl && data.insight) insEl.innerHTML = _highlight(data.insight);
    } catch(e) {
      console.error('[QAnim Sol] _buildSteps error:', e);
    }
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
      var ac = _el('sol-answer-card');
      var ic = _el('sol-insight-card');
      if (ac) { ac.classList.remove('visible'); setTimeout(function(){ ac.classList.add('visible'); }, base); }
      if (ic) { ic.classList.remove('visible'); setTimeout(function(){ ic.classList.add('visible'); }, base+120); }
    } catch(e) {
      console.error('[QAnim Sol] _animateReveal error:', e);
    }
  }

  function _showFallback(reason) {
    console.error('[QAnim Sol] Panel failed:', reason);
    var fb = document.getElementById('qanim-error-fallback');
    if (fb) {
      fb.style.display = 'flex';
      var msg = fb.querySelector('.qanim-err-msg');
      if (msg) msg.textContent = 'Solution: ' + reason;
    }
  }

  function openSolution() {
    try {
      var backdrop = _el('sol-backdrop');
      var panel    = _el('sol-panel');
      if (!backdrop || !panel) { _showFallback('DOM elements missing'); return; }
      var data = _loadData();
      _buildSteps(data);
      backdrop.classList.add('open');
      panel.classList.add('open');
      panel.setAttribute('aria-hidden', 'false');
      solutionOpen = true;
      _animateReveal();
    } catch(err) {
      console.error('[QAnim Sol] openSolution crashed:', err);
      _showFallback(err.message);
    }
  }

  function closeSolution() {
    try {
      var backdrop = _el('sol-backdrop');
      var panel    = _el('sol-panel');
      if (backdrop) backdrop.classList.remove('open');
      if (panel)    { panel.classList.remove('open'); panel.setAttribute('aria-hidden','true'); }
      solutionOpen = false;
    } catch(err) {
      console.error('[QAnim Sol] closeSolution crashed:', err);
    }
  }

  window.openSolution   = openSolution;
  window.closeSolution  = closeSolution;
  window.toggleSolution = function() { solutionOpen ? closeSolution() : openSolution(); };

  _onReady(function() {
    try {
      var solBtn = _el('solbtn') || document.querySelector('[data-sol-btn]');
      if (solBtn) {
        solBtn.removeAttribute('onclick');
        solBtn.addEventListener('click', function(e) { e.stopPropagation(); openSolution(); });
      }
      var closeBtn = _el('sol-close');
      if (closeBtn) { closeBtn.removeAttribute('onclick'); closeBtn.addEventListener('click', closeSolution); }
      var backdrop = _el('sol-backdrop');
      if (backdrop) { backdrop.removeAttribute('onclick'); backdrop.addEventListener('click', closeSolution); }
      document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && solutionOpen) closeSolution();
      });
      console.log('[QAnim Sol] Solution system initialized ✓');
    } catch(e) {
      console.error('[QAnim Sol] Init error:', e);
    }
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
        r'var _solBuilt\s*=\s*false;',
        r'function _buildSolutionSteps\(\)\s*\{.*?\n  \}',
        r'function openSolution\(\)\s*\{.*?\n  \}',
        r'function closeSolution\(\)\s*\{.*?\n  \}',
        r'window\.openSolution\s*=\s*openSolution\s*;?',
        r'window\.closeSolution\s*=\s*closeSolution\s*;?',
    ]:
        html = re.sub(pat, '', html, flags=re.DOTALL)

    sol_script = '<script>\n' + SOLUTION_JS_MODULE + '\n</script>'
    if '</body>' in html:
        html = html.replace('</body>', sol_script + '\n</body>', 1)
    else:
        html += '\n' + sol_script

    html = re.sub(
        r'(<button[^>]+id=["\']solbtn["\'][^>]*)\s+onclick=["\'][^"\']*["\']',
        r'\1', html
    )
    QAnimLogger.ok("Solution", f"Injected {len(steps)} steps")
    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 8 — Robust JSON Parser (unchanged)
# ══════════════════════════════════════════════════════════════════════

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
        "title": f"Animation: {question[:50]}",
        "explanation": "Parse failed",
        "animation_code": "",
        "solution_steps": [],
        "final_answer": "",
        "key_insight": "",
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
    if not code:
        return None

    return {
        "title":          extract_string("title") or f"Animation: {question[:50]}",
        "explanation":    extract_string("explanation") or "Interactive animation",
        "animation_type": extract_string("animation_type"),
        "design_strategy":extract_string("design_strategy"),
        "solution_steps": extract_array("solution_steps"),
        "final_answer":   extract_string("final_answer"),
        "key_insight":    extract_string("key_insight"),
        "animation_code": code,
    }

def _extract_animation_code_field(raw: str) -> str:
    key_pos = raw.find('"animation_code"')
    if key_pos == -1: return ""
    colon_pos = raw.find(':', key_pos)
    if colon_pos == -1: return ""
    after_colon = raw[colon_pos+1:].lstrip()
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
            code = raw[idx: end+7] if end != -1 else raw[idx:]
            if len(code) > 200:
                return {
                    "title": f"Animation: {question[:50]}",
                    "explanation": "Interactive animation",
                    "animation_code": code.strip(),
                    "solution_steps": [],
                    "final_answer": "",
                    "key_insight": "",
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
    return (s.replace('\\"','"').replace('\\n','\n').replace('\\t','\t')
             .replace('\\r','\r').replace("\\'","'").replace('\\\\','\\'))


# ══════════════════════════════════════════════════════════════════════
#  [UPDATE 5] Real-Time Application Hook Frame Builder
#  Generates the "Real-Time Application" intro frame HTML.
#  Placed as the first frame/screen before any animation content.
# ══════════════════════════════════════════════════════════════════════

def _build_realtime_hook_frame(topic: str, applications: list[str]) -> str:  # [UPDATED]
    """
    Builds the HTML for the Real-Time Application intro frame.
    This is injected as the very first scene/frame in the animation.
    Dynamically uses the topic and generated real-world applications.
    """  # [UPDATED]
    apps_html = "".join(  # [UPDATED]
        f'<li style="margin:8px 0; font-size:14px; color:#334155; line-height:1.6;">'  # [UPDATED]
        f'<span style="color:#7c3aed; font-weight:700; margin-right:6px;">•</span>'  # [UPDATED]
        f'{html_module.escape(app)}'  # [UPDATED]
        f'</li>'  # [UPDATED]
        for app in applications  # [UPDATED]
    )  # [UPDATED]

    return f"""
<!-- [UPDATED] Real-Time Application Hook Frame — injected as Scene 0 -->
<g id="scene-realtime-hook" class="qanim-scene" style="display:none;">
  <foreignObject x="40" y="60" width="720" height="400">
    <div xmlns="http://www.w3.org/1999/xhtml"
         style="
           background: linear-gradient(135deg, #ede9fe 0%, #f0f9ff 100%);
           border: 2px solid #a78bfa;
           border-radius: 18px;
           padding: 28px 32px;
           box-shadow: 0 8px 32px rgba(124,58,237,0.12);
           font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
         ">
      <!-- Heading -->
      <div style="
           font-size: 22px;
           font-weight: 800;
           color: #4c1d95;
           margin-bottom: 6px;
           letter-spacing: -0.3px;
         ">
        {html_module.escape(topic)} in Our Everyday Life
      </div>
      <!-- Subheading -->
      <div style="
           font-size: 13px;
           font-weight: 700;
           color: #7c3aed;
           text-transform: uppercase;
           letter-spacing: 1.5px;
           margin-bottom: 18px;
         ">
        Real-Time Applications
      </div>
      <!-- Applications list -->
      <ul style="list-style:none; margin:0; padding:0;">
        {apps_html}
      </ul>
      <!-- Footer badge -->
      <div style="
           margin-top: 22px;
           font-size: 10px;
           font-weight: 700;
           letter-spacing: 1.4px;
           text-transform: uppercase;
           color: #94a3b8;
         ">
        QAnim v6 · Real-World Connection
      </div>
    </div>
  </foreignObject>
</g>
"""  # [UPDATED]


async def _generate_realtime_applications(question: str, topic: str) -> list[str]:  # [UPDATED]
    """
    Uses AI to generate 2-3 real-world applications for the given topic.
    Returns a list of application strings. Falls back gracefully on error.
    """  # [UPDATED]
    try:  # [UPDATED]
        msg = client.messages.create(  # [UPDATED]
            model=Q_MODEL,  # [UPDATED]
            max_tokens=300,  # [UPDATED]
            system=(  # [UPDATED]
                "You are an educational content writer. Given a topic, list 2-3 specific, "  # [UPDATED]
                "concrete real-world applications in everyday life. "  # [UPDATED]
                "Return ONLY a JSON array of strings, no markdown, no extra text. "  # [UPDATED]
                'Example: ["Automobile radiators for engine cooling.", "Heat sinks in CPU and GPU cooling."]'  # [UPDATED]
            ),  # [UPDATED]
            messages=[{"role": "user", "content": f"Topic: {topic}\nQuestion context: {question[:200]}"}]  # [UPDATED]
        )  # [UPDATED]
        raw = msg.content[0].text.strip()  # [UPDATED]
        # Strip markdown fences if present
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()  # [UPDATED]
        apps = json.loads(raw)  # [UPDATED]
        if isinstance(apps, list):  # [UPDATED]
            return [str(a) for a in apps[:3]]  # [UPDATED]
    except Exception as e:  # [UPDATED]
        QAnimLogger.warn("RealtimeHook", f"Application generation failed: {e}")  # [UPDATED]
    return [f"Practical applications of {topic} in real-world scenarios."]  # [UPDATED]


def inject_realtime_hook_frame(html: str, topic: str, applications: list[str]) -> str:  # [UPDATED]
    """
    Injects the Real-Time Application hook as the very first scene in the animation.
    Finds the first <g id="scene-..."> group and inserts before it.
    Also registers the scene in the navigation system.
    """  # [UPDATED]
    hook_html = _build_realtime_hook_frame(topic, applications)  # [UPDATED]

    # Find first SVG scene group and insert before it
    first_scene = re.search(r'<g\s+id=["\']scene-(?!realtime-hook)[^"\']*["\']', html, re.IGNORECASE)  # [UPDATED]
    if first_scene:  # [UPDATED]
        pos = first_scene.start()  # [UPDATED]
        html = html[:pos] + hook_html + '\n' + html[pos:]  # [UPDATED]
        QAnimLogger.ok("RealtimeHook", f"Hook frame injected before first scene for topic: {topic}")  # [UPDATED]
    else:  # [UPDATED]
        # Fallback: inject after <svg ...> opening tag
        svg_match = re.search(r'<svg[^>]*>', html, re.IGNORECASE)  # [UPDATED]
        if svg_match:  # [UPDATED]
            pos = svg_match.end()  # [UPDATED]
            html = html[:pos] + '\n' + hook_html + html[pos:]  # [UPDATED]
            QAnimLogger.warn("RealtimeHook", "No scene groups found; injected after <svg> tag")  # [UPDATED]

    # Inject JS to register the realtime hook scene and prepend it to scene navigation
    realtime_nav_js = """
<script>
/* [UPDATED] Real-Time Hook Scene Registration */
(function() {
  function _onReady(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else { setTimeout(fn, 0); }
  }
  _onReady(function() {
    try {
      // Show the hook scene on load as the first frame
      var hookScene = document.getElementById('scene-realtime-hook');
      if (!hookScene) return;

      // Collect all existing scenes to re-sequence them
      var allScenes = document.querySelectorAll('.qanim-scene, [id^="scene-"]');
      var sceneArr = Array.prototype.slice.call(allScenes);

      // Hide all, then show only the hook on first load
      sceneArr.forEach(function(s) {
        if (s.id !== 'scene-realtime-hook') {
          s.style.display = 'none';
        }
      });
      hookScene.style.display = 'block';
      hookScene.style.opacity = '0';

      // Fade in the hook scene
      setTimeout(function() {
        hookScene.style.transition = 'opacity 0.5s ease';
        hookScene.style.opacity = '1';
      }, 50);

      // Override nextbtn to go from hook to scene 1 on first click
      var nextBtn = document.getElementById('nextbtn');
      if (!nextBtn) return;

      var _originalNext = nextBtn.onclick;
      var _hookShowing = true;

      nextBtn.addEventListener('click', function(e) {
        if (_hookShowing) {
          e.stopImmediatePropagation();
          _hookShowing = false;
          // Fade out hook
          hookScene.style.opacity = '0';
          setTimeout(function() {
            hookScene.style.display = 'none';
            // Show first actual scene
            var firstReal = null;
            for (var i = 0; i < sceneArr.length; i++) {
              if (sceneArr[i].id !== 'scene-realtime-hook') {
                firstReal = sceneArr[i];
                break;
              }
            }
            if (firstReal) {
              firstReal.style.display = 'block';
              firstReal.style.opacity = '0';
              setTimeout(function() {
                firstReal.style.transition = 'opacity 0.4s ease';
                firstReal.style.opacity = '1';
              }, 30);
            }
          }, 400);
        }
      }, true); // capture phase to intercept first

      console.log('[QAnim RealtimeHook] Hook scene registered ✓');
    } catch(e) {
      console.error('[QAnim RealtimeHook] Init error:', e);
    }
  });
})();
</script>
"""  # [UPDATED]

    # Inject the registration script before </body>
    if '</body>' in html:  # [UPDATED]
        html = html.replace('</body>', realtime_nav_js + '\n</body>', 1)  # [UPDATED]
    else:  # [UPDATED]
        html += '\n' + realtime_nav_js  # [UPDATED]

    return html  # [UPDATED]


# ══════════════════════════════════════════════════════════════════════
#  [UPDATE 4] Quiz Section Builder
#  Generates 15 questions (3 sets × 5) related to the topic.
#  Injected after main animation content, before or instead of last quiz.
# ══════════════════════════════════════════════════════════════════════

async def _generate_quiz_questions(question: str, topic: str) -> list[dict]:  # [UPDATED]
    """
    Uses AI to generate 15 quiz questions (3 sets × 5) related to the topic.
    Returns list of {set, question, options, correct_index} dicts.
    Never shows answers inside the animation — only questions + options.
    Falls back to placeholder questions on error.
    """  # [UPDATED]
    prompt = (  # [UPDATED]
        f"Generate exactly 15 multiple-choice quiz questions about the topic: '{topic}'.\n"  # [UPDATED]
        f"Context question: {question[:300]}\n\n"  # [UPDATED]
        "Rules:\n"  # [UPDATED]
        "- 15 questions total, organized in 3 sets of 5.\n"  # [UPDATED]
        "- Each question must have 4 options (A, B, C, D).\n"  # [UPDATED]
        "- Include correct_index (0-3) for the correct option.\n"  # [UPDATED]
        "- Questions must be directly related to the topic.\n"  # [UPDATED]
        "- Vary difficulty: Set 1 = basic, Set 2 = intermediate, Set 3 = advanced.\n\n"  # [UPDATED]
        "Return ONLY a JSON array of 15 objects, no markdown, no extra text:\n"  # [UPDATED]
        '[{"set":1,"question":"...","options":["A)...","B)...","C)...","D)..."],"correct_index":0}, ...]'  # [UPDATED]
    )  # [UPDATED]

    try:  # [UPDATED]
        msg = client.messages.create(  # [UPDATED]
            model=Q_MODEL,  # [UPDATED]
            max_tokens=3000,  # [UPDATED]
            system=(  # [UPDATED]
                "You are an expert quiz generator for educational animations. "  # [UPDATED]
                "Return ONLY valid JSON arrays, no markdown, no preamble."  # [UPDATED]
            ),  # [UPDATED]
            messages=[{"role": "user", "content": prompt}]  # [UPDATED]
        )  # [UPDATED]
        raw = msg.content[0].text.strip()  # [UPDATED]
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()  # [UPDATED]
        questions = json.loads(raw)  # [UPDATED]
        if isinstance(questions, list) and len(questions) >= 10:  # [UPDATED]
            return questions[:15]  # [UPDATED]
    except Exception as e:  # [UPDATED]
        QAnimLogger.warn("QuizGenerator", f"Quiz generation failed: {e}")  # [UPDATED]

    # Fallback: placeholder questions
    fallback = []  # [UPDATED]
    for set_num in range(1, 4):  # [UPDATED]
        for q_num in range(1, 6):  # [UPDATED]
            fallback.append({  # [UPDATED]
                "set": set_num,  # [UPDATED]
                "question": f"Set {set_num} — Question {q_num}: Which of the following relates to {topic}?",  # [UPDATED]
                "options": [  # [UPDATED]
                    f"A) Option A about {topic}",  # [UPDATED]
                    f"B) Option B about {topic}",  # [UPDATED]
                    f"C) Option C about {topic}",  # [UPDATED]
                    f"D) Option D about {topic}",  # [UPDATED]
                ],  # [UPDATED]
                "correct_index": 0,  # [UPDATED]
            })  # [UPDATED]
    return fallback  # [UPDATED]


def _build_quiz_scene_html(quiz_questions: list[dict], topic: str) -> str:  # [UPDATED]
    """
    Builds the complete HTML for the 3-set, 15-question quiz section.
    Renders only questions and options — NO answer reveal inside animation.
    The "Next >" button remains functional for navigation between quiz sets.
    """  # [UPDATED]

    # Group by set
    sets: dict[int, list] = {1: [], 2: [], 3: []}  # [UPDATED]
    for q in quiz_questions:  # [UPDATED]
        s = int(q.get("set", 1))  # [UPDATED]
        if s in sets:  # [UPDATED]
            sets[s].append(q)  # [UPDATED]

    scenes_html = ""  # [UPDATED]
    for set_num in range(1, 4):  # [UPDATED]
        set_questions = sets[set_num]  # [UPDATED]
        questions_html = ""  # [UPDATED]
        for i, q in enumerate(set_questions):  # [UPDATED]
            opts = q.get("options", [])  # [UPDATED]
            opts_html = "".join(  # [UPDATED]
                f'<div style="'  # [UPDATED]
                f'margin:4px 0; padding:8px 14px; '  # [UPDATED]
                f'background:#fff; border:1.5px solid #e2e8f0; border-radius:10px; '  # [UPDATED]
                f'font-size:13px; color:#374151; cursor:pointer; '  # [UPDATED]
                f'transition:background 0.18s, border-color 0.18s;" '  # [UPDATED]
                f'onmouseover="this.style.background=\'#ede9fe\';this.style.borderColor=\'#7c3aed\';" '  # [UPDATED]
                f'onmouseout="this.style.background=\'#fff\';this.style.borderColor=\'#e2e8f0\';" '  # [UPDATED]
                f'onclick="this.style.background=\'#ddd6fe\';this.style.borderColor=\'#7c3aed\';">'  # [UPDATED]
                f'{html_module.escape(str(opt))}'  # [UPDATED]
                f'</div>'  # [UPDATED]
                for opt in opts  # [UPDATED]
            )  # [UPDATED]
            questions_html += f"""
  <div style="margin-bottom:18px; padding:14px 16px;
              background:#f8fafc; border-radius:14px;
              border:1px solid #e2e8f0;">
    <div style="font-size:13px; font-weight:700; color:#1e293b; margin-bottom:10px; line-height:1.5;">
      Q{i+1}. {html_module.escape(str(q.get('question', '')))}
    </div>
    {opts_html}
  </div>"""  # [UPDATED]

        scenes_html += f"""
<!-- [UPDATED] Quiz Set {set_num} Scene -->
<g id="scene-quiz-set{set_num}" class="qanim-scene" style="display:none;">
  <foreignObject x="20" y="50" width="760" height="440">
    <div xmlns="http://www.w3.org/1999/xhtml"
         style="font-family:-apple-system,'Segoe UI',Arial,sans-serif;
                height:100%; overflow-y:auto;
                padding:10px 12px; box-sizing:border-box;">
      <!-- Set header -->
      <div style="display:flex; align-items:center; justify-content:space-between;
                  margin-bottom:16px; padding-bottom:10px;
                  border-bottom:2px solid #ede9fe;">
        <div>
          <div style="font-size:11px; font-weight:700; letter-spacing:1.5px;
                      text-transform:uppercase; color:#7c3aed; margin-bottom:2px;">
            {topic} Quiz
          </div>
          <div style="font-size:18px; font-weight:800; color:#1e293b;">
            Set {set_num} <span style="font-size:13px;color:#94a3b8;font-weight:500;">/ 3</span>
          </div>
        </div>
        <div style="background:#ede9fe; border-radius:50px; padding:6px 14px;
                    font-size:12px; font-weight:700; color:#7c3aed;">
          5 Questions
        </div>
      </div>
      <!-- Questions -->
      {questions_html}
    </div>
  </foreignObject>
</g>"""  # [UPDATED]

    return scenes_html  # [UPDATED]


def inject_quiz_section(html: str, quiz_questions: list[dict], topic: str) -> str:  # [UPDATED]
    """
    Injects the 3-set quiz section into the animation HTML.
    Placed after the last main content scene, before </svg>.
    The "Next >" button remains functional for scene navigation.
    No answer/solution display is included.
    """  # [UPDATED]
    quiz_html = _build_quiz_scene_html(quiz_questions, topic)  # [UPDATED]

    # Find last </g> before </svg> and inject after it
    svg_close = html.rfind('</svg>')  # [UPDATED]
    if svg_close != -1:  # [UPDATED]
        html = html[:svg_close] + quiz_html + '\n' + html[svg_close:]  # [UPDATED]
        QAnimLogger.ok("QuizInjector", f"Quiz section injected ({len(quiz_questions)} questions, 3 sets)")  # [UPDATED]
    else:  # [UPDATED]
        QAnimLogger.warn("QuizInjector", "No </svg> found — quiz injected before </body>")  # [UPDATED]
        html = html.replace('</body>', quiz_html + '\n</body>', 1)  # [UPDATED]

    # Inject JS to register quiz scenes in the navigation
    quiz_nav_js = """
<script>
/* [UPDATED] Quiz Section Navigation Registration */
(function() {
  function _onReady(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else { setTimeout(fn, 0); }
  }
  _onReady(function() {
    try {
      var quizScenes = [
        document.getElementById('scene-quiz-set1'),
        document.getElementById('scene-quiz-set2'),
        document.getElementById('scene-quiz-set3')
      ].filter(Boolean);

      if (!quizScenes.length) return;

      // Register quiz scenes with existing navigation if available
      if (typeof window._qanimScenes !== 'undefined' && Array.isArray(window._qanimScenes)) {
        window._qanimScenes = window._qanimScenes.concat(quizScenes);
        console.log('[QAnim Quiz] Appended ' + quizScenes.length + ' quiz scenes to navigation');
      } else {
        // Store for any custom nav integration
        window._qanimQuizScenes = quizScenes;
        console.log('[QAnim Quiz] Quiz scenes registered: ' + quizScenes.length);
      }
    } catch(e) {
      console.error('[QAnim Quiz] Registration error:', e);
    }
  });
})();
</script>
"""  # [UPDATED]

    if '</body>' in html:  # [UPDATED]
        html = html.replace('</body>', quiz_nav_js + '\n</body>', 1)  # [UPDATED]
    else:  # [UPDATED]
        html += '\n' + quiz_nav_js  # [UPDATED]

    return html  # [UPDATED]


# ══════════════════════════════════════════════════════════════════════
#  [UPDATE 3] Remove Final Answer / Solution Display from Animation
#  Strips any auto-reveal, frame-transition, or end-of-animation
#  logic that would display the final answer or solution inside
#  the animation HTML itself. The solution modal (openSolution)
#  remains available via the "View Solution" button only.
# ══════════════════════════════════════════════════════════════════════

def remove_answer_display_from_animation(html: str) -> str:  # [UPDATED]
    """
    Removes all automatic final-answer / solution-reveal logic from
    the animation HTML. This includes:
      - Auto-triggered openSolution() calls (setTimeout, event-based)
      - "Answer" or "Solution" slide/frame content rendered inside SVG scenes
      - Any scene whose id suggests it's a solution/answer frame
      - Auto-play end-of-animation solution triggers

    The "View Solution" button (#solbtn) and its click handler are preserved.
    The "Next >" (#nextbtn) button is preserved.
    """  # [UPDATED]

    # 1. Remove setTimeout-triggered openSolution() calls
    # Pattern: setTimeout(function(){ openSolution(); }, N)
    html = re.sub(  # [UPDATED]
        r'setTimeout\s*\(\s*function\s*\(\s*\)\s*\{\s*(?:window\.)?openSolution\s*\(\s*\)\s*;?\s*\}\s*,\s*\d+\s*\)',  # [UPDATED]
        '/* [UPDATED] auto-openSolution removed */',  # [UPDATED]
        html, flags=re.IGNORECASE  # [UPDATED]
    )  # [UPDATED]

    # 2. Remove any scene group with id containing "answer" or "solution"
    # but NOT the solution panel itself (id="sol-panel")
    html = re.sub(  # [UPDATED]
        r'<g[^>]+id=["\'](?!sol-panel|sol-backdrop)[^"\']*(?:answer|solution|final)[^"\']*["\'][^>]*>.*?</g>',  # [UPDATED]
        '<!-- [UPDATED] answer/solution scene removed -->',  # [UPDATED]
        html, flags=re.IGNORECASE | re.DOTALL  # [UPDATED]
    )  # [UPDATED]

    # 3. Remove inline auto-reveal answer text blocks
    # Pattern: text elements with "Final Answer" or "Solution:" in SVG
    html = re.sub(  # [UPDATED]
        r'<text[^>]*>[^<]*(?:Final Answer|Solution:|Answer:)[^<]*</text>',  # [UPDATED]
        '<!-- [UPDATED] answer text element removed -->',  # [UPDATED]
        html, flags=re.IGNORECASE  # [UPDATED]
    )  # [UPDATED]

    # 4. Remove any "showAnswer", "revealAnswer", "showSolution" function calls
    # that are NOT inside the solbtn click handler
    html = re.sub(  # [UPDATED]
        r'\b(?:showAnswer|revealAnswer|showSolution|displayAnswer)\s*\(\s*\)\s*;',  # [UPDATED]
        '/* [UPDATED] answer reveal call removed */',  # [UPDATED]
        html, flags=re.IGNORECASE  # [UPDATED]
    )  # [UPDATED]

    # 5. Remove lastScene/endScene auto-trigger patterns
    # Pattern: if (currentScene === totalScenes) { openSolution(); }
    html = re.sub(  # [UPDATED]
        r'if\s*\([^)]*(?:currentScene|sceneIndex|scene)\s*[><=!]+\s*[^)]*\)\s*\{\s*'  # [UPDATED]
        r'(?:window\.)?openSolution\s*\(\s*\)\s*;?\s*\}',  # [UPDATED]
        '/* [UPDATED] end-of-scene openSolution removed */',  # [UPDATED]
        html, flags=re.IGNORECASE | re.DOTALL  # [UPDATED]
    )  # [UPDATED]

    QAnimLogger.ok("AnswerRemover", "Final answer/solution display removed from animation flow")  # [UPDATED]
    return html  # [UPDATED]


# ══════════════════════════════════════════════════════════════════════
#  MODULE 9 — Full Generation Pipeline  (v6 — with all 5 updates)
# ══════════════════════════════════════════════════════════════════════

async def _generate_concept_animation(question: str, category: str) -> str:
    """
    STAGE 1 — Pure concept animation (no solution, no answer).
    Returns the finished concept_animation_code HTML string,
    or a fallback HTML on any failure.
    """
    QAnimLogger.info("ConceptPipeline", f"START  category={category}")

    # [UPDATED] Apply background color update to concept system prompt
    prompt = _build_concept_prompt(question, category)

    try:
        msg = client.messages.create(
            model=Q_MODEL,
            max_tokens=MAX_TOK_CONCEPT,
            system=SYSTEM_CONCEPT,
            messages=[{"role": "user", "content": prompt}]
        )
        raw         = msg.content[0].text.strip()
        stop_reason = msg.stop_reason
        QAnimLogger.info("ConceptAI", f"stop_reason={stop_reason}  raw_len={len(raw)}")
        if stop_reason == "max_tokens":
            QAnimLogger.warn("ConceptAI", "Hit max_tokens — concept response may be truncated!")
    except Exception as e:
        QAnimLogger.error("ConceptAI", f"API call failed: {e}")
        return RecoveryEngine.fallback_html(question, f"Concept AI error: {e}")

    parsed = _parse_response(raw, question)
    raw_for_concept = raw.replace('"concept_code"', '"animation_code"')
    parsed_c = _parse_response(raw_for_concept, question)
    concept_html = parsed_c.get("animation_code", "").strip()

    if not concept_html:
        for marker in ['<!DOCTYPE html>', '<html', '<svg']:
            idx = raw.find(marker)
            if idx != -1:
                end = raw.rfind('</html>')
                concept_html = raw[idx: end + 7] if end != -1 else raw[idx:]
                break

    if not concept_html:
        QAnimLogger.error("ConceptParser", "Could not extract concept_code from response")
        return RecoveryEngine.fallback_html(question, "Concept parse failed — no HTML extracted")

    try:
        GenerationValidator.validate(concept_html, require_svg=True)
    except ValidationError as e:
        QAnimLogger.warn("ConceptValidator", f"Strict validation failed: {e}")
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

    # [UPDATED] Apply background color fix to concept animation
    concept_html = _apply_background_color_fix(concept_html)

    concept_html = inject_infrastructure(concept_html)

    QAnimLogger.ok("ConceptPipeline", f"DONE — concept_html_len={len(concept_html):,}")
    return concept_html


def _apply_background_color_fix(html: str) -> str:  # [UPDATED]
    """
    [UPDATE 1] Replaces dark/black background colors in the animation HTML
    with a clean, light background (#F8F9FA). Targets common dark patterns
    used in the AI-generated animations.
    Only updates the background; all other styles remain unchanged.
    """  # [UPDATED]
    LIGHT_BG = "#F8F9FA"  # [UPDATED]

    # Replace common dark background hex values in CSS
    dark_patterns = [  # [UPDATED]
        (r'background(?:-color)?\s*:\s*#0a0a1a\b', f'background: {LIGHT_BG}'),  # [UPDATED]
        (r'background(?:-color)?\s*:\s*#0d0d1a\b', f'background: {LIGHT_BG}'),  # [UPDATED]
        (r'background(?:-color)?\s*:\s*#111111\b', f'background: {LIGHT_BG}'),  # [UPDATED]
        (r'background(?:-color)?\s*:\s*#000000\b', f'background: {LIGHT_BG}'),  # [UPDATED]
        (r'background(?:-color)?\s*:\s*#0f0f0f\b', f'background: {LIGHT_BG}'),  # [UPDATED]
        (r'background(?:-color)?\s*:\s*#1a1a2e\b', f'background: {LIGHT_BG}'),  # [UPDATED]
        (r'background(?:-color)?\s*:\s*#12121f\b', f'background: {LIGHT_BG}'),  # [UPDATED]
        (r'background(?:-color)?\s*:\s*#0e0e1a\b', f'background: {LIGHT_BG}'),  # [UPDATED]
        # Dark linear gradients used as backgrounds on html/body
        (r'background\s*:\s*linear-gradient\s*\(\s*(?:135deg|180deg|to\s+\w+)\s*,\s*#(?:0[0-9a-f]{5}|1[0-3][0-9a-f]{4})[^;)]*\)',
         f'background: {LIGHT_BG}'),  # [UPDATED]
    ]  # [UPDATED]

    for pattern, replacement in dark_patterns:  # [UPDATED]
        html = re.sub(pattern, replacement, html, flags=re.IGNORECASE)  # [UPDATED]

    # Also replace SVG rect fill used as background (first large rect)
    # Pattern: <rect width="800" height="500" fill="#0a0a1a" ...>
    html = re.sub(  # [UPDATED]
        r'(<rect[^>]+(?:width=["\']800["\']|width=["\']100%["\'])[^>]+fill=["\'])#(?:0[0-9a-f]{5}|1[0-3][0-9a-f]{4})(["\'])',  # [UPDATED]
        r'\g<1>' + LIGHT_BG + r'\g<2>',  # [UPDATED]
        html, flags=re.IGNORECASE  # [UPDATED]
    )  # [UPDATED]

    QAnimLogger.ok("BgColorFix", f"Background color updated to {LIGHT_BG}")  # [UPDATED]
    return html  # [UPDATED]


def _apply_question_font_size_fix(html: str) -> str:  # [UPDATED]
    """
    [UPDATE 2] Increases the font size of the topic/question label (#qstrip .qtext)
    by approximately 1.5x. Targets common font-size values used in the question strip.
    Only the font-size property is changed; family, color, weight, position are preserved.
    """  # [UPDATED]
    UPDATED_FONT_SIZE = "22px"  # [UPDATED]  # ~1.5x from typical 14-16px

    # Target .qtext font-size in <style> blocks
    # Common sizes: 12px, 13px, 14px, 15px, 16px → replace with 22px
    html = re.sub(  # [UPDATED]
        r'(\.qtext\s*\{[^}]*?)font-size\s*:\s*(?:1[0-9]px|[0-9]+(?:\.[0-9]+)?(?:px|em|rem))',  # [UPDATED]
        r'\g<1>font-size: ' + UPDATED_FONT_SIZE,  # [UPDATED]
        html, flags=re.IGNORECASE | re.DOTALL  # [UPDATED]
    )  # [UPDATED]

    # Also target inline font-size on #qstrip or .qtext elements
    html = re.sub(  # [UPDATED]
        r'(id=["\']qstrip["\'][^>]*style=["\'][^"\']*?)font-size\s*:\s*[0-9]+(?:\.[0-9]+)?(?:px|em|rem)',  # [UPDATED]
        r'\g<1>font-size: ' + UPDATED_FONT_SIZE,  # [UPDATED]
        html, flags=re.IGNORECASE  # [UPDATED]
    )  # [UPDATED]

    # Target SVG <text> elements that are the question display (font-size attr)
    # Pattern: <text ... class="qtext" ... font-size="14" ...>
    html = re.sub(  # [UPDATED]
        r'(<text[^>]+class=["\'][^"\']*qtext[^"\']*["\'][^>]*)font-size=["\'][0-9]+(?:\.[0-9]+)?["\']',  # [UPDATED]
        r'\g<1>font-size="22"',  # [UPDATED]  # SVG font-size attr uses unitless px
        html, flags=re.IGNORECASE  # [UPDATED]
    )  # [UPDATED]

    QAnimLogger.ok("FontSizeFix", f"Question font size updated to {UPDATED_FONT_SIZE}")  # [UPDATED]
    return html  # [UPDATED]


async def generate_question_animation(question: str) -> dict:
    """
    TWO-STAGE PIPELINE with ToFind feature + all 5 updates (v6-updated):

    STAGE 0 — ToFind Extraction  (synchronous, instant, no AI required)
    STAGE 1 — Concept Animation  (concurrent with Stage 2)
    STAGE 2 — Solution Animation  (concurrent with Stage 1)

    Updates applied in this pipeline:
      [UPDATE 1] Background color → #F8F9FA (light)
      [UPDATE 2] Question font size → 22px (~1.5x)
      [UPDATE 3] Final answer/solution auto-display removed
      [UPDATE 4] 15-question quiz section injected (3 sets × 5)
      [UPDATE 5] Real-Time Application hook frame injected as first frame
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("Question cannot be empty")

    short_q = question[:80] + ("..." if len(question) > 80 else "")
    QAnimLogger.info("Pipeline", f"START — '{short_q}'")
    QAnimLogger.info("Pipeline", f"Model: {Q_MODEL}  MaxTokens: {MAX_TOK}")

    # ── Step 0: Extract ToFind targets ──
    to_find_targets = ToFindExtractor.extract(question)
    QAnimLogger.info("Pipeline", f"ToFind targets: {to_find_targets}")

    # ── Step 1: Classify ──
    category = _classify_topic(question)
    QAnimLogger.info("Classifier", f"Category: {category}")

    # ── Step 2: Extract topic label for hook + quiz ──
    topic = _extract_topic_label(question, category)  # [UPDATED]
    QAnimLogger.info("Pipeline", f"Topic label: {topic}")  # [UPDATED]

    # ── Step 3: Build Stage-2 prompt ──
    prompt = _build_prompt(question, category)

    # ── Step 4: Run BOTH stages + real-time apps + quiz generation concurrently ──
    QAnimLogger.info("Pipeline",
                     "Launching Stage-1, Stage-2, real-time apps, and quiz concurrently…")  # [UPDATED]

    async def _run_stage2_ai() -> str:
        try:
            msg = client.messages.create(
                model=Q_MODEL,
                max_tokens=MAX_TOK,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}]
            )
            raw         = msg.content[0].text.strip()
            stop_reason = msg.stop_reason
            QAnimLogger.info("Stage2AI",
                             f"stop_reason={stop_reason}  raw_len={len(raw)}")
            if stop_reason == "max_tokens":
                QAnimLogger.warn("Stage2AI",
                                 "Hit max_tokens — response may be truncated!")
            return raw
        except Exception as e:
            QAnimLogger.error("Stage2AI", f"API call failed: {e}")
            raise

    try:
        concept_html, raw, realtime_apps, quiz_questions = await asyncio.gather(  # [UPDATED]
            _generate_concept_animation(question, category),
            _run_stage2_ai(),
            _generate_realtime_applications(question, topic),   # [UPDATED] Update 5
            _generate_quiz_questions(question, topic),           # [UPDATED] Update 4
        )
    except Exception as e:
        QAnimLogger.error("AI", f"Concurrent generation failed: {e}")
        return _build_failure_result(question, f"API error: {e}")

    # ── Step 5: Parse Stage-2 ──
    result = _parse_response(raw, question)
    result["category"]               = category
    result["engine_version"]         = "v6.0-updated"  # [UPDATED]
    result["concept_animation_code"] = concept_html
    result["to_find"]                = to_find_targets
    result.setdefault("solution_steps", [])
    result.setdefault("final_answer",   "")
    result.setdefault("key_insight",    "")

    html = result.get("animation_code", "")

    # ── Step 6: Validate Stage-2 (strict first pass) ──
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
                QAnimLogger.error("Validator", f"Recovery also failed: {e2}")
                result["animation_code"] = RecoveryEngine.fallback_html(question, str(e2))
                result["render_status"]  = "fallback"
                return result
        else:
            result["animation_code"] = RecoveryEngine.fallback_html(question, str(e))
            result["render_status"]  = "fallback"
            return result

    # ── Step 7: Sanitize Stage-2 ──
    html = HtmlSanitizer.sanitize(html)

    # ── Step 8: [UPDATE 1] Apply background color fix ──
    html = _apply_background_color_fix(html)  # [UPDATED]

    # ── Step 9: [UPDATE 2] Apply question font size fix ──
    html = _apply_question_font_size_fix(html)  # [UPDATED]

    # ── Step 10: [UPDATE 3] Remove final answer/solution display ──
    html = remove_answer_display_from_animation(html)  # [UPDATED]

    # ── Step 11: Inject infrastructure ──
    html = inject_infrastructure(html)

    # ── Step 12: Inject solution system ──
    html = inject_solution_system(
        html    = html,
        steps   = result["solution_steps"],
        answer  = result["final_answer"],
        insight = result["key_insight"],
    )

    # ── Step 13: Inject ToFind system ──
    html = inject_to_find_system(
        html    = html,
        targets = to_find_targets,
    )

    # ── Step 14: [UPDATE 5] Inject Real-Time Application hook as first frame ──
    html = inject_realtime_hook_frame(html, topic, realtime_apps)  # [UPDATED]

    # ── Step 15: [UPDATE 4] Inject 15-question quiz section ──
    html = inject_quiz_section(html, quiz_questions, topic)  # [UPDATED]

    # ── Step 16: Final validation ──
    try:
        GenerationValidator.validate(html, require_svg=True)
    except ValidationError as e:
        QAnimLogger.warn("FinalValidator",
                         f"Post-injection validation failed: {e} — continuing")

    result["animation_code"] = html
    result["render_status"]  = "ok"

    QAnimLogger.ok("Pipeline", (
        f"DONE — title='{result['title']}' "
        f"to_find={result['to_find']} "
        f"concept_len={len(result['concept_animation_code']):,} "
        f"solution_len={len(result['animation_code']):,} "
        f"steps={len(result['solution_steps'])}"
    ))
    return result


def _extract_topic_label(question: str, category: str) -> str:  # [UPDATED]
    """
    Extracts a concise topic label from the question for use in the
    Real-Time Application hook and Quiz section headings.
    Uses simple heuristics; falls back to category name.
    """  # [UPDATED]
    # Try to find a key noun phrase near the question start
    q = question.strip().rstrip('?.')
    # Remove common question prefixes
    q = re.sub(r'^(?:find|calculate|determine|what is|how does|explain|describe|show|prove)\s+',
               '', q, flags=re.IGNORECASE).strip()
    # Take first 40 chars as topic label, clean up
    topic = re.sub(r'\s+', ' ', q[:50]).strip()
    topic = re.sub(r'\s+(?:of|in|for|with|using|given|if|when|where)\s+.+$', '', topic,
                   flags=re.IGNORECASE).strip()
    if len(topic) < 4:
        topic = category.replace('_', ' ').title()
    return topic  # [UPDATED]


def _build_failure_result(question: str, reason: str) -> dict:
    fallback = RecoveryEngine.fallback_html(question, reason)
    return {
        "title":                   f"Animation: {question[:50]}",
        "explanation":             "Generation failed",
        "animation_type":          "error",
        "design_strategy":         "",
        "animation_code":          fallback,
        "concept_animation_code":  fallback,
        "solution_steps":          [],
        "final_answer":            "",
        "key_insight":             "",
        "to_find":                 [],
        "category":                "UNKNOWN",
        "engine_version":          "v6.0-updated",  # [UPDATED]
        "render_status":           "error",
    }


# ══════════════════════════════════════════════════════════════════════
#  SYNC WRAPPER
# ══════════════════════════════════════════════════════════════════════
def generate_question_animation_sync(question: str) -> dict:
    return asyncio.run(generate_question_animation(question))


# ══════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPTS + PROMPT BUILDERS
# ══════════════════════════════════════════════════════════════════════

# [UPDATED] Update 1: Added light background requirement to system prompt
SYSTEM = """You are QAnim v6 — a cinematic SVG motion designer and educational animation engineer.

YOUR MISSION: Turn any student question into a premium, self-contained SVG animation.
The output must feel like: Khan Academy × Apple UI × motion-design studio.

════════════════════════════════════════════
OUTPUT FORMAT — STRICT (no markdown fences)
════════════════════════════════════════════
{
  "animation_type": "concise type label",
  "design_strategy": "2-4 sentences",
  "solution_steps": ["Step 1: ...", "Step 2: ...", "Step 3: ...", "Step 4: ..."],
  "final_answer": "The complete, precise final answer in 1-2 sentences.",
  "key_insight": "One memorable conceptual insight.",
  "animation_code": "COMPLETE SELF-CONTAINED HTML FILE AS A SINGLE JSON STRING"
}

════════════════════════════════════════════
CRITICAL SAFETY RULES FOR animation_code
════════════════════════════════════════════
✅ Must be valid JSON string: escape \" → \\", newline → \\n, backslash → \\\\
✅ Contains complete <!DOCTYPE html>...</html>
✅ Self-contained: NO external fonts, NO CDN links, NO imports
✅ NO document.write() — it is forbidden and will be stripped
✅ NO backtick template literals in JS
✅ All SVG must have xmlns="http://www.w3.org/2000/svg"
✅ All <script> tags must be balanced (same number of open and close)
✅ All <svg> tags must be balanced
✅ BACKGROUND: Use a LIGHT, clean background (#F8F9FA or #EFEFEF or soft white). [UPDATED]
   Do NOT use dark backgrounds (#0a0a1a, #111, #000, etc). [UPDATED]
   All text and elements must be clearly visible on the light background. [UPDATED]
"""  # [UPDATED]

# [UPDATED] Update 2: Increased question font size in design system
DESIGN_SYSTEM = """
TYPOGRAPHY: font-family: -apple-system, 'Segoe UI', Arial, sans-serif
SVG viewBox: "0 0 800 500"
QUESTION STRIP (.qtext): font-size MUST be 22px or larger for readability. [UPDATED]
COLOR PALETTES: PHYSICS=#3b5bdb/#e64980, MATH=#7c3aed/#db2777,
                BIOLOGY=#16a34a/#ca8a04, PROCESS=#059669/#0284c7,
                ABSTRACT=#d97706/#7c3aed, MIXED=#0284c7/#7c3aed
BACKGROUND: Always use light/off-white (#F8F9FA). Never use dark backgrounds. [UPDATED]
"""  # [UPDATED]

SVG_TECHNIQUES = """
KEY TECHNIQUES: stroke-dashoffset path reveal, fade+rise for labels,
spring scale-in for heroes, glow pulse for active elements,
sequential JS setTimeout orchestration (not CSS animation-delay).
"""

STRATEGY_TEMPLATES = {
    "VISUAL_PHYSICS": "Draw physical system with forces, motion trajectories, and equation reveal.",
    "PROCESS_BASED":  "Show sequential nodes with traveling dot connector and progress bar.",
    "MATHEMATICAL":   "Animate on coordinate axes with function reveal, shaded area, and formula.",
    "BIOLOGICAL":     "Organic shapes with molecule paths, color-coded structures, cycle arrows.",
    "ABSTRACT":       "Physical analogy (scales/dominos/tracks) mapped to the concept.",
    "MIXED":          "Zone-divided canvas with cross-zone connector animation.",
}

# [UPDATED] Update 3: Added instruction to NOT auto-reveal solution at end
FALLBACK_RULES = """
If stuck: use CARD-REVEAL (3-4 gradient cards fade-rise staggered) or
TIMELINE (horizontal line draws, events scale-in) or
WORD-MAP (central node, branch lines draw, satellite nodes appear).
NEVER: flat colors, static diagrams, placeholder comments.
NEVER: auto-trigger openSolution() or show the final answer automatically. [UPDATED]
The animation must STOP or LOOP after the last content frame. [UPDATED]
Do NOT proceed to or render any answer/solution frame automatically. [UPDATED]
"""  # [UPDATED]

HTML_SHELL_NOTE = """
REQUIRED HTML STRUCTURE:
- Standard solution panel DOM: #sol-steps-container, #sol-answer-text, #sol-insight-text,
  #sol-backdrop, #sol-panel, #sol-close, #solbtn
- All scenes in <g id="scene-N"> groups
- Navigation: #prevbtn, #nextbtn, #dots
- Question display: #qstrip .qtext (font-size: 22px minimum) [UPDATED]
DO NOT include document.write() anywhere.
DO NOT include external script src= tags.
DO NOT auto-trigger openSolution() at the end of the animation. [UPDATED]
DO NOT render the final answer or solution inline in any scene. [UPDATED]
"""  # [UPDATED]

# [UPDATED] Update 1+2 applied to concept system prompt as well
SYSTEM_CONCEPT = """You are QAnim Concept Engine — a cinematic SVG educational animator.

YOUR ONLY MISSION: Create a premium, self-contained educational animation that visually
teaches the CONCEPT behind the question. Do NOT show the answer. Do NOT reveal solution
steps. The student should watch and understand the concept, not get the answer.

Think: Apple product video meets Khan Academy meets motion design studio.

════════════════════════════════════════════
OUTPUT FORMAT — STRICT (no markdown fences)
════════════════════════════════════════════
{
  "animation_type": "concise type label",
  "design_strategy": "2-4 sentences describing visual approach",
  "concept_code": "COMPLETE SELF-CONTAINED HTML FILE AS A SINGLE JSON STRING"
}

════════════════════════════════════════════
CRITICAL SAFETY RULES FOR concept_code
════════════════════════════════════════════
✅ Must be valid JSON string: escape \\" → \\\\", newline → \\\\n, backslash → \\\\\\\\
✅ Contains complete <!DOCTYPE html>...</html>
✅ Self-contained: NO external fonts, NO CDN links, NO imports
✅ NO document.write() — forbidden
✅ NO backtick template literals in JS
✅ All SVG must have xmlns="http://www.w3.org/2000/svg"
✅ All <script> and <svg> tags must be balanced
✅ BACKGROUND: Use light, clean background (#F8F9FA). NOT dark. [UPDATED]
✅ QUESTION FONT: .qtext font-size must be 22px or larger. [UPDATED]

════════════════════════════════════════════
ANIMATION RULES — CONCEPT STAGE
════════════════════════════════════════════
✅ Show the concept visually: forces, particles, graphs, molecules, diagrams
✅ Use cinematic progressive reveal with JS setTimeout orchestration
✅ Glow effects, gradient fills, animated arrows, dynamic labels
✅ Multi-scene with #prevbtn/#nextbtn/#dots navigation
✅ Question strip at top: #qstrip .qtext (font-size: 22px minimum) [UPDATED]
✅ Smooth transitions between scenes (fade + translate)
✅ Light background (#F8F9FA) with vivid accent colors on light bg [UPDATED]
✅ Motion hierarchy: hero element first, then supporting elements, then labels
✅ Animated formula/variable reveals (stroke-dashoffset on text paths or opacity)
❌ DO NOT show the numeric answer or final solution
❌ DO NOT include any solution panel DOM elements
❌ DO NOT include a "View Solution" button (the host UI adds this)
❌ DO NOT use dark backgrounds (#0a0a1a, #111, etc) [UPDATED]
"""  # [UPDATED]

CONCEPT_STRATEGY_TEMPLATES = {
    "VISUAL_PHYSICS": (
        "Cinematic force diagram: animate the physical setup, draw force vectors with "
        "glowing arrow heads, show trajectory arc, reveal formula symbols one-by-one. "
        "End on a dramatic zoom to the key variable — do NOT solve it."
    ),
    "PROCESS_BASED": (
        "Sequential process nodes connected by animated traveling-dot paths. "
        "Each stage reveals with spring scale-in, highlights the mechanism, then dims "
        "as the next stage lights up. Progress bar at the bottom."
    ),
    "MATHEMATICAL": (
        "Coordinate axes draw in, function curve traces from left to right with glow trail. "
        "Shaded region pulses. Key terms fade-rise near relevant graph features. "
        "Formula symbols materialize one token at a time."
    ),
    "BIOLOGICAL": (
        "Organic cell/molecule shapes with soft gradient fills. Animated process arrows "
        "trace the biological pathway. Color-coded components appear sequentially. "
        "Labels fade-rise with gentle bounce."
    ),
    "ABSTRACT": (
        "Physical analogy rendered visually: scales, spectrum bars, Venn circles, or "
        "network graph. Concept dimensions animate as separate visual zones with "
        "connecting bridges that pulse to show relationships."
    ),
    "MIXED": (
        "Split-zone canvas: left zone animates the physical/biological system, right zone "
        "shows the data/formula side. A dynamic connector bridge pulses between zones "
        "to illustrate the relationship."
    ),
}

def _build_concept_prompt(question: str, category: str) -> str:
    strategy = CONCEPT_STRATEGY_TEMPLATES.get(
        category, CONCEPT_STRATEGY_TEMPLATES["PROCESS_BASED"]
    )
    return f"""Build a PREMIUM CONCEPT ANIMATION for QAnim Stage 1.

QUESTION: {question}
CATEGORY: {category}
VISUAL STRATEGY: {strategy}

{DESIGN_SYSTEM}
{SVG_TECHNIQUES}
{FALLBACK_RULES}

CONCEPT ANIMATION REQUIREMENTS:
- Light background: #F8F9FA or similar clean off-white color [UPDATED]
- Vivid accent colors matching category palette (work on light bg)
- Multi-scene progressive reveal (3-5 scenes max)
- Scene 1: Establish the visual context (draw the system/setup)
- Scene 2-3: Animate the core concept mechanism (the "how/why")
- Scene 4 (optional): Show relationships / key insight visually
- NO solution, NO final numeric answer
- Question text at top in #qstrip .qtext (font-size: 22px) [UPDATED]
- Navigation: #prevbtn, #nextbtn, #dots

IMPORTANT: Return ONLY the raw JSON object. No markdown. No extra text.
The concept_code must be a complete <!DOCTYPE html>...</html> document
as a properly escaped JSON string.
"""


def _classify_topic(question: str) -> str:
    q = question.lower()
    scores = {
        "BIOLOGICAL":    sum(1 for k in ["cell","dna","rna","protein","photosynthesis","mitosis","enzyme","hormone","gene","organism"] if k in q),
        "MATHEMATICAL":  sum(1 for k in ["integral","derivative","matrix","vector","theorem","equation","polynomial","logarithm","trigonometry","calculus"] if k in q),
        "ABSTRACT":      sum(1 for k in ["philosophy","ethics","democracy","capitalism","justice","freedom","psychology","consciousness","society","ideology"] if k in q),
        "PROCESS_BASED": sum(1 for k in ["how does","how do","step by step","process","algorithm","mechanism","workflow","procedure"] if k in q),
        "VISUAL_PHYSICS":sum(1 for k in ["force","velocity","acceleration","mass","energy","momentum","gravity","pressure","current","voltage","wave","circuit"] if k in q),
    }
    max_score = max(scores.values())
    if max_score >= 2:
        top = [c for c,s in scores.items() if s == max_score]
        return top[0] if len(top) == 1 else "MIXED"
    if sum(1 for s in scores.values() if s > 0) >= 3:
        return "MIXED"
    try:
        resp = client.messages.create(
            model=Q_MODEL, max_tokens=30,
            system="Reply with ONLY one of: VISUAL_PHYSICS, PROCESS_BASED, MATHEMATICAL, BIOLOGICAL, ABSTRACT, MIXED",
            messages=[{"role":"user","content":f"Classify: {question[:200]}"}]
        )
        cat = resp.content[0].text.strip().upper()
        if cat in STRATEGY_TEMPLATES: return cat
    except Exception:
        pass
    return "PROCESS_BASED"

def _build_prompt(question: str, category: str) -> str:
    strategy = STRATEGY_TEMPLATES.get(category, STRATEGY_TEMPLATES["PROCESS_BASED"])
    return f"""Build a PREMIUM SVG animation for QAnim v6.

QUESTION: {question}
CATEGORY: {category}
STRATEGY: {strategy}

{DESIGN_SYSTEM}
{SVG_TECHNIQUES}
{FALLBACK_RULES}
{HTML_SHELL_NOTE}

IMPORTANT: Return ONLY the raw JSON object. No markdown. No extra text.
The animation_code must be a complete <!DOCTYPE html>...</html> document
as a properly escaped JSON string.

Solution data (steps/answer/insight) will be injected automatically by
the post-processor. Include the solution panel DOM elements but do NOT
hardcode solution content — just the shell HTML with empty containers.
"""


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
        print("=" * 70)
        print(f"  QAnim v6.0-updated (Two-Stage + ToFind + 5 Updates) — Category: {cat}")  # [UPDATED]
        print(f"  Q: {q[:65]}...")
        print("=" * 70)

        # ── Quick offline ToFind smoke test ──
        print("\n[ToFind Smoke Test]")
        targets = ToFindExtractor.extract(q)
        print(f"  Targets: {targets}")

        result = generate_question_animation_sync(q)

        concept_html  = result.get("concept_animation_code", "")
        solution_html = result.get("animation_code", "")

        print(f"\nTitle               : {result['title']}")
        print(f"Category            : {result.get('category', 'N/A')}")
        print(f"Engine              : {result.get('engine_version', 'N/A')}")
        print(f"Render Status       : {result.get('render_status', 'N/A')}")
        print(f"[ToFind] Targets    : {result.get('to_find', [])}")
        print(f"[Stage 1] Concept   : {len(concept_html):,} chars")
        print(f"[Stage 2] Solution  : {len(solution_html):,} chars")
        steps = result.get('solution_steps', [])
        print(f"Solution Steps      : {len(steps)}")
        for i, s in enumerate(steps, 1):
            print(f"  Step {i}: {s[:90]}...")
        print(f"Final Answer        : {result.get('final_answer','')[:120]}")
        print(f"Key Insight         : {result.get('key_insight','')[:100]}")

        # ── Save Stage 1 — Concept Animation ──
        concept_out = f"q_anim_v60_{cat.lower()}_concept.html"
        with open(concept_out, "w", encoding="utf-8") as f:
            f.write(concept_html)
        print(f"\n[Stage 1] Saved     : {concept_out}")

        # ── Save Stage 2 — Solution Animation (with ToFind + all updates) ──
        solution_out = f"q_anim_v60_{cat.lower()}_solution.html"
        with open(solution_out, "w", encoding="utf-8") as f:
            f.write(solution_html)
        print(f"[Stage 2] Saved     : {solution_out}\n")