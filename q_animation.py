"""
q_animation.py  —  QAnim Question Animation Generator  v10.0
=============================================================
╔══════════════════════════════════════════════════════════════╗
║  v10.0 — LIGHT THEME + NEW QUIZ/SOLUTION/ANSWER SYSTEMS     ║
╠══════════════════════════════════════════════════════════════╣
║  CHANGES IN v10.0 vs v9.0:                                  ║
║  ✅ LIGHT THEME: White/light-gray UI throughout             ║
║  ✅ REMOVED: Hook section (HookGenerator, Stage 1)          ║
║  ✅ NEW QUIZ: 3 sets × 5 questions, separate cards          ║
║  ✅ NEW VIEW SOLUTION: Step-by-step numbered display        ║
║  ✅ NEW ANSWER BOX: Input + validation (correct/wrong/close) ║
║  ✅ Floating controls bar (Find / Quiz / View Solution /    ║
║     Answer Box) — always visible                            ║
║  ✅ Three-stage concurrent pipeline (no hook stage)         ║
╚══════════════════════════════════════════════════════════════╝

PIPELINE (v10.0):
  Stage 0 — ToFind Extraction    (sync, no AI)
  Stage 1 — Concept Animation    (claude-sonnet-4-5) + StepController
  Stage 2 — Solution Animation   (claude-sonnet-4-5) + StepController
  Stage 3 — Quiz Generation      (claude-haiku-4-5)  + 3 sets × 5 Qs

CONTROLS BAR (injected into every animation HTML):
  [🔍 Find]  [📝 Quiz]  [💡 View Solution]  [✏️ Answer Box]

ANSWER VALIDATION LOGIC:
  Numerical: ±1% → Correct, ±15% → Almost Correct, >15% → Wrong
  Text:       ≥80% keyword overlap → Correct, ≥40% → Almost, <40% → Wrong

RESULT DICT KEYS:
  concept_animation_code  — concept teaching animation HTML
  animation_code          — solution animation HTML (all panels embedded)
  quiz_html               — standalone quiz HTML (3 sets × 5 Qs)
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
client         = anthropic.Anthropic()

QUIZ_MODEL     = "claude-haiku-4-5"       # Stage 3 — quiz generation (3×5 Qs)
CONCEPT_MODEL  = "claude-sonnet-4-5"      # Stage 1 — concept SVG animation
SOLUTION_MODEL = "claude-sonnet-4-5"      # Stage 2 — solution SVG animation
Q_MODEL        = SOLUTION_MODEL           # used by _classify_topic

MAX_TOK           = 16000
MAX_TOK_CONCEPT   = 12000
MAX_TOK_QUIZ      = 6000


# ══════════════════════════════════════════════════════════════════════
#  MODULE 1 — QAnimLogger
# ══════════════════════════════════════════════════════════════════════

class QAnimLogger:
    """Centralized logger. All lifecycle events go through here."""
    PREFIX = "[QAnim v10]"

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
    """Validates AI-generated HTML before iframe injection."""

    DANGEROUS_PATTERNS = [
        (r'document\.write\s*\(', "document.write() is forbidden"),
        (r'<script[^>]+src\s*=',  "External script src not allowed"),
        (r'javascript:\s*void',   "javascript:void() link detected"),
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
        (r'\bwhat\s+will\s+be\s+(?:the\s+)?(.+?)(?=\?|,|;|$)',  1),
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
    _ARTICLE_RE      = re.compile(r'^(?:the|a|an)\s+', re.IGNORECASE)
    _TRIGGER_VERB_RE = re.compile(
        r'^(?:find|determine|calculate|evaluate|compute|obtain|'
        r'identify|estimate|derive|prove|show|express|solve\s+for)'
        r'\s+(?:the\s+|an?\s+)?', re.IGNORECASE
    )
    _MATH_VAR_RE = re.compile(r'^[A-Za-zα-ωΑ-Ω][0-9₀-₉]?$')
    MAX_LEN = 120

    @classmethod
    def extract(cls, question: str) -> list:
        if not question or not question.strip():
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
        for noise in sorted(cls._NOISE_PREFIXES, key=len, reverse=True):
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
        html = cls._fix_template_literals(html)        # double-quote safe version
        html = cls._fix_const_let(html)                 # const/let → var
        html = cls._fix_arrow_functions(html)           # arrow fn → regular fn
        html = cls._fix_single_quote_apostrophes(html)  # 'it\'s' → "it's"
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

    @classmethod
    def _fix_template_literals(cls, html: str) -> str:
        """Replace backtick template literals with double-quoted string concatenation."""
        def process_script(script_match: re.Match) -> str:
            tag   = script_match.group(1)
            body  = script_match.group(2)
            close = script_match.group(3)
            if re.search(r'type\s*=\s*["\']application/', tag, re.IGNORECASE):
                return script_match.group(0)

            def replace_template(m: re.Match) -> str:
                raw = m.group(1)
                # Split on ${...} — alternating literal / expression parts
                parts = re.split(r'\$\{([^}]*)\}', raw)
                out = []
                for idx, part in enumerate(parts):
                    if idx % 2 == 0:
                        # String literal — use DOUBLE quotes to avoid apostrophe bug
                        esc = (part
                               .replace('\\', '\\\\')
                               .replace('"',  '\\"')
                               .replace('\n', '\\n')
                               .replace('\r', '\\r')
                               .replace('\t', '\\t'))
                        out.append('"' + esc + '"')
                    else:
                        # Expression — parenthesise and keep verbatim
                        out.append('(' + part.strip() + ')')
                # Drop empty string tokens at edges
                while out and out[0]  == '""': out.pop(0)
                while out and out[-1] == '""': out.pop()
                return (' + '.join(out)) if out else '""'

            original = body
            body = re.sub(r'`((?:[^`\\]|\\.)*)`', replace_template, body, flags=re.DOTALL)
            if body != original:
                QAnimLogger.warn("Sanitizer", "Backtick template literals detected and replaced")
            return f"{tag}{body}{close}"

        pattern = r'(<script(?:\s[^>]*)?>)(.*?)(</script>)'
        return re.sub(pattern, process_script, html, flags=re.DOTALL | re.IGNORECASE)

    @classmethod
    def _fix_const_let(cls, html: str) -> str:
        """Convert const/let → var for maximum JS compatibility in iframes."""
        def process_script(script_match: re.Match) -> str:
            tag   = script_match.group(1)
            body  = script_match.group(2)
            close = script_match.group(3)
            if re.search(r'type\s*=\s*["\']application/', tag, re.IGNORECASE):
                return script_match.group(0)
            body = re.sub(r'\bconst\b', 'var', body)
            body = re.sub(r'\blet\b',   'var', body)
            return f"{tag}{body}{close}"
        pattern = r'(<script(?:\s[^>]*)?>)(.*?)(</script>)'
        return re.sub(pattern, process_script, html, flags=re.DOTALL | re.IGNORECASE)

    @classmethod
    def _fix_arrow_functions(cls, html: str) -> str:
        """
        Convert simple ES6 arrow functions to ES5 regular functions.
        Handles: (params) => { body }  and  (params) => expr
        Skips application/json blocks.
        """
        def process_script(script_match: re.Match) -> str:
            tag   = script_match.group(1)
            body  = script_match.group(2)
            close = script_match.group(3)
            if re.search(r'type\s*=\s*["\']application/', tag, re.IGNORECASE):
                return script_match.group(0)
            # (params) => { block } → function(params) { block }  [safe]
            body = re.sub(
                r'\(([^)]*)\)\s*=>\s*(\{)',
                r'function(\1) \2',
                body
            )
            # param => { block }  (single param, no parens) [safe]
            body = re.sub(
                r'(?<![\w$])([A-Za-z_$][\w$]*)\s*=>\s*(\{)',
                r'function(\1) \2',
                body
            )
            return f"{tag}{body}{close}"
        pattern = r'(<script(?:\s[^>]*)?>)(.*?)(</script>)'
        return re.sub(pattern, process_script, html, flags=re.DOTALL | re.IGNORECASE)

    @classmethod
    def _fix_single_quote_apostrophes(cls, html: str) -> str:
        """
        Find single-quoted JS strings that contain apostrophes and rewrap
        them in double quotes.
        e.g. 'Layer\'s resistance'  or  'it's broken'
        Both become: "Layer's resistance"  / "it's broken"
        Skips application/json blocks.
        """
        def process_script(script_match: re.Match) -> str:
            tag   = script_match.group(1)
            body  = script_match.group(2)
            close = script_match.group(3)
            if re.search(r'type\s*=\s*["\']application/', tag, re.IGNORECASE):
                return script_match.group(0)

            def fix_sq_string(m: re.Match) -> str:
                inner = m.group(1)  # content between outer single quotes
                # If there's a bare apostrophe (not preceded by backslash), rewrap
                if re.search(r"(?<!\\)'", inner):
                    # Unescape any \' in the original and escape " instead
                    fixed = inner.replace("\\'" , "'" ).replace('"', '\\"')
                    return '"' + fixed + '"'
                return m.group(0)  # no apostrophe issue, leave alone

            # Pre-escape inter-word apostrophes (e.g. 'it's' -> 'it\'s')
            # so the closing regex can see the full string as one token.
            body = re.sub(r"(?<=[a-zA-Z])'(?=[a-zA-Z])", "\\'", body)
            # Match single-quoted strings (not crossing newlines, handles \' escapes)
            body = re.sub(r"'((?:[^'\\\n]|\\.)*)'", fix_sq_string, body)
            return f"{tag}{body}{close}"

        pattern = r'(<script(?:\s[^>]*)?>)(.*?)(</script>)'
        return re.sub(pattern, process_script, html, flags=re.DOTALL | re.IGNORECASE)


# ══════════════════════════════════════════════════════════════════════
#  MODULE 4 — RecoveryEngine  (Light Theme)
# ══════════════════════════════════════════════════════════════════════

class RecoveryEngine:
    """Graceful fallback HTML when generation or validation fails. LIGHT THEME."""

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
    background:#f1f5f9;
    font-family:-apple-system,'Segoe UI',Arial,sans-serif;
    display:flex; align-items:center; justify-content:center;
  }}
  .card {{
    background:#ffffff;
    border:1px solid #e2e8f0;
    border-radius:16px;
    box-shadow:0 4px 24px rgba(0,0,0,0.10);
    padding:36px 40px; max-width:520px; text-align:center;
  }}
  .icon {{ font-size:40px; margin-bottom:16px; }}
  .title {{ font-size:17px; font-weight:800; color:#1e293b; margin-bottom:10px; }}
  .reason {{
    font-size:11px; color:#64748b; background:#f8fafc;
    border-radius:10px; padding:10px 14px; margin:12px 0;
    border:1px solid #e2e8f0; text-align:left;
    line-height:1.6; font-family:monospace;
  }}
  .question {{ font-size:12px; color:#94a3b8; line-height:1.6; margin-top:10px; font-style:italic; }}
  .retry-hint {{
    margin-top:18px; font-size:11px; font-weight:700;
    letter-spacing:1.5px; text-transform:uppercase; color:#7c3aed;
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
    background:#f1f5f9;font-family:-apple-system,sans-serif}}
</style>
</head>
<body>
<div style="font-size:11px;color:#64748b;position:fixed;top:8px;left:0;right:0;text-align:center">
  {q_safe}
</div>
{animation_code}
</body></html>"""


# ══════════════════════════════════════════════════════════════════════
#  MODULE 5 — IframeLifecycleManager JS constant (no hook bridge)
# ══════════════════════════════════════════════════════════════════════

IFRAME_RUNTIME_JS = r"""
/* ═══════════════════════════════════════════════════════════
   QAnim IframeLifecycleManager v10 — srcdoc-based safe render
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
        }
      });
    } catch(e) {
      Log.error('_injectSrcdoc outer: ' + e);
    }
  }

  function _processQueue() {
    if (_rendering || _renderQueue.length === 0) return;
    _rendering = true;
    var task = _renderQueue.shift();
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
      _currentHtml = task.html;
      _rendering = false;
      if (task.onSuccess) task.onSuccess();
      _processQueue();
    };
    iframe.onerror = function(e) {
      cleanup();
      _rendering = false;
      if (task.onError) task.onError('iframe error');
      _processQueue();
    };
    timeoutId = setTimeout(function() {
      if (done) return;
      cleanup();
      _currentHtml = task.html;
      _rendering = false;
      if (task.onSuccess) task.onSuccess();
      _processQueue();
    }, 8000);

    _injectSrcdoc(iframe, task.html);
  }

  window.QAnimILM = {
    render: function(html, onSuccess, onError) {
      if (!html || html.length < 100) {
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
    },
    getCurrentHtml: function() { return _currentHtml; },
    isRendering:    function() { return _rendering; }
  };

  Log.ok('IframeLifecycleManager v10 initialized');
})();
"""


# ══════════════════════════════════════════════════════════════════════
#  MODULE 6 — Error Boundary & Infrastructure  (Light Theme)
# ══════════════════════════════════════════════════════════════════════

ERROR_BOUNDARY_HTML = """
<!-- QAnim Error Fallback — hidden unless needed -->
<div id="qanim-error-fallback" style="
  display:none; position:fixed; inset:0; z-index:9999;
  background:rgba(241,245,249,0.92); backdrop-filter:blur(12px);
  align-items:center; justify-content:center;
">
  <div style="
    background:#ffffff; border-radius:16px; padding:32px 36px;
    max-width:440px; text-align:center;
    border:1px solid #e2e8f0;
    box-shadow:0 8px 40px rgba(0,0,0,0.12);
  ">
    <div style="font-size:36px;margin-bottom:14px">⚠️</div>
    <div style="font-size:15px;font-weight:800;color:#1e293b;margin-bottom:8px">Animation Error</div>
    <div class="qanim-err-msg" style="
      font-size:11px;color:#64748b;background:#f8fafc;
      border-radius:10px;padding:10px 14px;margin:12px 0;
      border:1px solid #e2e8f0;font-family:monospace;
      text-align:left;line-height:1.6;word-break:break-all;
    ">Unknown error</div>
    <button onclick="document.getElementById('qanim-error-fallback').style.display='none'"
      style="margin-top:14px;padding:8px 22px;border-radius:8px;border:none;
        background:#7c3aed;color:#fff;
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

    # Scroll-enable override
    _scroll_fix = (
        '\n<style id="qanim-scroll-fix">\n'
        'html, body {\n'
        '  overflow-x: hidden !important;\n'
        '  overflow-y: auto !important;\n'
        '  height: auto !important;\n'
        '  min-height: 100vh;\n'
        '}\n'
        '#container, [id="container"] { padding-bottom: 80px; }\n'
        '</style>\n'
    )
    if '</head>' in html:
        html = html.replace('</head>', _scroll_fix + '</head>', 1)
    else:
        html = _scroll_fix + html

    QAnimLogger.ok("Infrastructure", "Error fallback + inner logger + scroll-fix injected")
    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 6.5 — ToFind Injection System  (Light Theme)
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
    <button id="tofind-close" class="tf-close-btn" aria-label="Close">✕</button>
  </div>
  <p class="tf-subtitle">What this question is asking you to determine:</p>
  <div id="tofind-items-container" class="tf-items-container"></div>
</aside>
"""

_TO_FIND_CSS = """
<style id="qanim-tofind-styles">
#tofind-backdrop {
  display:block; position:fixed; inset:0; z-index:8000;
  background:rgba(15,23,42,0.40); backdrop-filter:blur(4px);
  opacity:0; visibility:hidden; pointer-events:none;
  transition:opacity 0.22s ease, visibility 0.22s ease;
}
#tofind-backdrop.open { visibility:visible; pointer-events:auto; opacity:1; }
#tofind-panel {
  display:flex; flex-direction:column; position:fixed;
  top:50%; left:50%; transform:translate(-50%,-48%) scale(0.96);
  z-index:8100; width:min(460px,92vw); max-height:80vh;
  border-radius:16px; padding:24px; box-sizing:border-box;
  background:#ffffff;
  border:1px solid #e2e8f0;
  box-shadow:0 8px 40px rgba(0,0,0,0.12);
  opacity:0; pointer-events:none;
  transition:opacity 0.25s ease,transform 0.25s cubic-bezier(0.34,1.56,0.64,1);
}
#tofind-panel.open { opacity:1; pointer-events:auto; transform:translate(-50%,-50%) scale(1); }
.tf-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
.tf-header-left { display:flex; align-items:center; gap:10px; }
.tf-icon-wrap {
  width:32px; height:32px; border-radius:8px;
  background:#7c3aed; display:flex; align-items:center;
  justify-content:center; color:#fff; flex-shrink:0;
}
.tf-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:16px; font-weight:700; color:#1e293b; }
.tf-close-btn {
  width:30px; height:30px; border-radius:8px; border:1px solid #e2e8f0;
  background:#f8fafc; color:#64748b; font-size:12px;
  display:flex; align-items:center; justify-content:center; cursor:pointer;
  transition:background 0.15s,color 0.15s;
}
.tf-close-btn:hover { background:#fee2e2; color:#dc2626; }
.tf-subtitle { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:12px; color:#64748b; margin:0 0 14px; }
.tf-items-container {
  display:flex; flex-direction:column; gap:8px; overflow-y:auto;
  scrollbar-width:thin; scrollbar-color:#e2e8f0 transparent;
}
.tofind-item {
  display:flex; align-items:flex-start; gap:12px; padding:12px 14px; border-radius:10px;
  background:#f8fafc; border:1px solid #e2e8f0;
  opacity:0; transform:translateX(-12px); transition:background 0.15s;
}
.tofind-item:hover { background:#ede9fe; border-color:#7c3aed; }
.tofind-check {
  width:20px; height:20px; border-radius:50%;
  background:#7c3aed; color:#fff; font-size:11px;
  display:flex; align-items:center; justify-content:center; flex-shrink:0;
}
.tofind-text { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:13px; font-weight:600; color:#1e293b; line-height:1.5; }
.tofind-empty { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:13px; color:#94a3b8; text-align:center; padding:20px 0; font-style:italic; }
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
    } catch(e) { return []; }
  }
  function _escape(text) {
    if (!text) return '';
    return String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function _buildPanel(targets) {
    if (_panelBuilt) return;
    _panelBuilt = true;
    var container = _el('tofind-items-container');
    if (!container) return;
    if (!targets || targets.length === 0) {
      container.innerHTML = '<div class="tofind-empty">No specific targets detected.</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < targets.length; i++) {
      html += '<div class="tofind-item" id="tofind-item-'+i+'"><div class="tofind-check">&#10003;</div><div class="tofind-text">'+_escape(targets[i])+'</div></div>';
    }
    container.innerHTML = html;
  }
  function _animateReveal() {
    var items = document.querySelectorAll('.tofind-item');
    for (var i = 0; i < items.length; i++) {
      (function(el, idx) {
        el.style.opacity = '0'; el.style.transform = 'translateX(-12px)'; el.style.transition = 'none';
        setTimeout(function() {
          el.style.transition = 'opacity 0.28s ease,transform 0.28s ease';
          el.style.opacity = '1'; el.style.transform = 'translateX(0)';
        }, 60 + idx * 80);
      })(items[i], i);
    }
  }
  function openToFind() {
    var backdrop = _el('tofind-backdrop'), panel = _el('tofind-panel');
    if (!backdrop || !panel) return;
    _buildPanel(_loadTargets());
    backdrop.classList.add('open');
    panel.classList.add('open');
    panel.setAttribute('aria-hidden','false');
    toFindOpen = true;
    setTimeout(_animateReveal, 100);
  }
  function closeToFind() {
    var backdrop = _el('tofind-backdrop'), panel = _el('tofind-panel');
    if (backdrop) backdrop.classList.remove('open');
    if (panel) { panel.classList.remove('open'); panel.setAttribute('aria-hidden','true'); }
    toFindOpen = false;
  }
  window.openToFind   = openToFind;
  window.closeToFind  = closeToFind;
  window.toggleToFind = function() { toFindOpen ? closeToFind() : openToFind(); };
  _onReady(function() {
    var tfBtn = _el('tofind-btn') || document.querySelector('[data-tofind-btn]');
    if (tfBtn) { tfBtn.removeAttribute('onclick'); tfBtn.addEventListener('click', function(e) { e.stopPropagation(); openToFind(); }); }
    var closeBtn = _el('tofind-close');
    if (closeBtn) { closeBtn.addEventListener('click', closeToFind); }
    var backdrop = _el('tofind-backdrop');
    if (backdrop) { backdrop.addEventListener('click', closeToFind); }
    document.addEventListener('keydown', function(e) { if (e.key === 'Escape' && toFindOpen) closeToFind(); });
  });
})();
"""


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


# ══════════════════════════════════════════════════════════════════════
#  MODULE 7 — Solution System  (Light Theme + "View Solution" button)
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


# Solution panel DOM (light theme) — panel is INSIDE backdrop for proper flex centering
_SOLUTION_PANEL_DOM = """
<div id="sol-backdrop" aria-hidden="true">
<aside id="sol-panel" role="dialog" aria-labelledby="sol-heading" aria-hidden="true">
  <div class="sol-header">
    <div class="sol-header-left">
      <div class="sol-icon">💡</div>
      <span id="sol-heading" class="sol-title">Step-by-Step Solution</span>
    </div>
    <button id="sol-close" class="sol-close-btn" aria-label="Close">✕</button>
  </div>
  <div class="sol-panel-body">
    <div id="sol-steps-container" class="sol-steps-container"></div>
    <div id="sol-answer-card" class="sol-answer-card">
      <div class="sol-answer-label">✅ Final Answer</div>
      <div id="sol-answer-text" class="sol-answer-text"></div>
    </div>
    <div id="sol-insight-card" class="sol-insight-card">
      <div class="sol-insight-label">💬 Key Insight</div>
      <div id="sol-insight-text" class="sol-insight-text"></div>
    </div>
  </div>
</aside>
</div>
"""

_SOLUTION_PANEL_CSS = """
<style id="qanim-solution-styles">
/* ── Solution Panel — Light Theme ── */
#sol-backdrop {
  display:flex; position:fixed; inset:0; z-index:8500;
  background:rgba(15,23,42,0.42); backdrop-filter:blur(6px);
  -webkit-backdrop-filter:blur(6px);
  opacity:0; visibility:hidden; pointer-events:none;
  transition:opacity 0.26s ease, visibility 0.26s ease;
  align-items:center; justify-content:center;
  padding:16px; box-sizing:border-box;
}
#sol-backdrop.open { visibility:visible; pointer-events:auto; opacity:1; }

#sol-panel {
  background:#ffffff; border-radius:18px;
  width:min(620px,96vw); max-height:88vh;
  border:1px solid #e2e8f0;
  box-shadow:0 12px 48px rgba(0,0,0,0.14);
  opacity:0; transform:translateY(14px) scale(0.97);
  transition:opacity 0.26s ease,transform 0.26s cubic-bezier(0.34,1.56,0.64,1);
  display:flex; flex-direction:column; overflow:hidden;
}
#sol-panel.open { opacity:1; transform:translateY(0) scale(1); }

.sol-panel-body {
  overflow-y:auto; flex:1; padding:20px 24px;
  scrollbar-width:thin; scrollbar-color:#e2e8f0 transparent;
  display:flex; flex-direction:column; gap:0;
}

.sol-header {
  display:flex; align-items:center; justify-content:space-between;
  padding:18px 22px; border-bottom:1px solid #e2e8f0; flex-shrink:0;
  background:#ffffff; border-radius:18px 18px 0 0;
}
.sol-header-left { display:flex; align-items:center; gap:10px; }
.sol-icon { font-size:22px; }
.sol-title {
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:17px; font-weight:800; color:#1e293b;
}
.sol-close-btn {
  width:32px; height:32px; border-radius:8px; border:1px solid #e2e8f0;
  background:#f8fafc; color:#64748b; font-size:13px; cursor:pointer;
  display:flex; align-items:center; justify-content:center;
  transition:background 0.15s,color 0.15s; flex-shrink:0;
}
.sol-close-btn:hover { background:#fee2e2; color:#dc2626; }

.sol-steps-container { display:flex; flex-direction:column; gap:0; margin-bottom:14px; }

.sol-step {
  display:flex; align-items:flex-start; gap:12px; padding:12px 0;
  border-bottom:1px solid #f1f5f9;
  opacity:0; transform:translateX(-12px);
  transition:opacity 0.30s ease,transform 0.30s ease;
}
.sol-step:last-child { border-bottom:none; }
.sol-step.visible { opacity:1; transform:translateX(0); }
.sol-step-num {
  width:28px; height:28px; border-radius:50%; flex-shrink:0;
  background:#7c3aed; color:#fff; font-size:12px; font-weight:700;
  display:flex; align-items:center; justify-content:center; margin-top:2px;
}
.sol-step-text {
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:13.5px; color:#334155; line-height:1.75;
}
.formula {
  background:#ede9fe; color:#6d28d9; padding:2px 6px;
  border-radius:4px; font-family:monospace; font-size:12px;
}

.sol-answer-card, .sol-insight-card {
  border-radius:10px; padding:14px 16px; margin-bottom:10px;
  opacity:0; transition:opacity 0.35s ease;
}
.sol-answer-card.visible, .sol-insight-card.visible { opacity:1; }

.sol-answer-card {
  background:#f0fdf4; border:1px solid #bbf7d0;
}
.sol-answer-label {
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:11px; font-weight:700; text-transform:uppercase;
  letter-spacing:1px; color:#16a34a; margin-bottom:6px;
}
.sol-answer-text {
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:14px; font-weight:600; color:#166534; line-height:1.65;
}

.sol-insight-card { background:#fefce8; border:1px solid #fef08a; }
.sol-insight-label {
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:11px; font-weight:700; text-transform:uppercase;
  letter-spacing:1px; color:#ca8a04; margin-bottom:6px;
}
.sol-insight-text {
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:13px; color:#78350f; line-height:1.65;
}
</style>
"""

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
    } catch(e) { return {}; }
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
  }
  function _animateReveal() {
    var stepEls = document.querySelectorAll('.sol-step');
    for (var i = 0; i < stepEls.length; i++) {
      (function(el, idx) {
        el.classList.remove('visible');
        setTimeout(function() { el.classList.add('visible'); }, 50 + idx * 80);
      })(stepEls[i], i);
    }
    var base = 50 + stepEls.length * 80;
    var ac = _el('sol-answer-card'), ic = _el('sol-insight-card');
    if (ac) { ac.classList.remove('visible'); setTimeout(function(){ ac.classList.add('visible'); }, base); }
    if (ic) { ic.classList.remove('visible'); setTimeout(function(){ ic.classList.add('visible'); }, base+100); }
  }
  function openSolution() {
    var backdrop = _el('sol-backdrop'), panel = _el('sol-panel');
    if (!backdrop || !panel) return;
    _buildSteps(_loadData());
    backdrop.classList.add('open');
    backdrop.setAttribute('aria-hidden','false');
    panel.classList.add('open');
    panel.setAttribute('aria-hidden','false');
    solutionOpen = true;
    /* Scroll panel body to top so content starts from beginning */
    var body = panel.querySelector('.sol-panel-body');
    if (body) body.scrollTop = 0;
    setTimeout(_animateReveal, 80);
  }
  function closeSolution() {
    var backdrop = _el('sol-backdrop'), panel = _el('sol-panel');
    if (backdrop) { backdrop.classList.remove('open'); backdrop.setAttribute('aria-hidden','true'); }
    if (panel)    { panel.classList.remove('open');    panel.setAttribute('aria-hidden','true'); }
    /* Reset step visibility for next open */
    document.querySelectorAll('.sol-step').forEach(function(el){ el.classList.remove('visible'); });
    var ac = _el('sol-answer-card'), ic = _el('sol-insight-card');
    if (ac) ac.classList.remove('visible');
    if (ic) ic.classList.remove('visible');
    solutionOpen = false;
  }
  window.openSolution   = openSolution;
  window.closeSolution  = closeSolution;
  window.toggleSolution = function() { solutionOpen ? closeSolution() : openSolution(); };
  _onReady(function() {
    var closeBtn = _el('sol-close');
    if (closeBtn) closeBtn.addEventListener('click', function(e){ e.stopPropagation(); closeSolution(); });
    var backdrop = _el('sol-backdrop');
    if (backdrop) backdrop.addEventListener('click', function(e){ if (e.target === backdrop) closeSolution(); });
    document.addEventListener('keydown', function(e) { if (e.key === 'Escape' && solutionOpen) closeSolution(); });
  });
})();
"""


def inject_solution_system(html: str, steps: list, answer: str, insight: str) -> str:
    """Injects solution data, panel DOM, CSS, and JS module."""
    # Remove any existing sol_data tag
    html = re.sub(
        r'<script[^>]+id=["\']__sol_data__["\'][^>]*>.*?</script>',
        '', html, flags=re.DOTALL
    )
    # Inject data tag
    data_tag = _build_solution_data_tag(steps, answer, insight)
    if '</head>' in html:
        html = html.replace('</head>', data_tag + '\n</head>', 1)
    else:
        html = data_tag + '\n' + html

    # Inject CSS
    if '</head>' in html:
        html = html.replace('</head>', _SOLUTION_PANEL_CSS + '\n</head>', 1)

    # Remove any AI-generated #solbtn (we'll add our own in controls bar)
    html = re.sub(
        r'<button[^>]+id=["\']solbtn["\'][^>]*>.*?</button>',
        '', html, flags=re.DOTALL | re.IGNORECASE
    )

    # Inject panel DOM after <body>
    body_match = re.search(r'<body[^>]*>', html, re.IGNORECASE)
    if body_match:
        ins = body_match.end()
        html = html[:ins] + '\n' + _SOLUTION_PANEL_DOM + html[ins:]

    # Remove old inline solution data patterns
    for pat in [
        r'var SOL_STEPS\s*=\s*\(function\(\).*?\}\)\(\);',
        r"var SOL_ANSWER\s*=\s*'[^']*';",
        r"var SOL_INSIGHT\s*=\s*'[^']*';",
    ]:
        html = re.sub(pat, '', html, flags=re.DOTALL)

    # Inject JS
    sol_script = '<script>\n' + SOLUTION_JS_MODULE + '\n</script>'
    if '</body>' in html:
        html = html.replace('</body>', sol_script + '\n</body>', 1)
    else:
        html += '\n' + sol_script

    QAnimLogger.ok("Solution", f"Injected {len(steps)} steps — light theme panel added")
    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 8 — NEW Quiz System  (3 Sets × 5 Questions)
# ══════════════════════════════════════════════════════════════════════

NEW_QUIZ_SYSTEM_PROMPT = """You are an expert educational quiz designer.
Generate 3 quiz sets with 5 questions each about the given topic.

CRITICAL: Return ONLY valid JSON. No markdown, no explanation, no code fences.
Exact format:
{
  "sets": [
    {
      "title": "Set 1: Core Concepts",
      "focus": "Fundamental principles and definitions",
      "questions": [
        {
          "q": "Question text here?",
          "type": "mcq",
          "options": ["Option A", "Option B", "Option C", "Option D"],
          "correct": 0,
          "explanation": "Brief explanation of why this is correct."
        }
      ]
    },
    {
      "title": "Set 2: Application",
      "focus": "Problem-solving and practical scenarios",
      "questions": [ ...5 questions... ]
    },
    {
      "title": "Set 3: Advanced",
      "focus": "Challenging and nuanced questions",
      "questions": [ ...5 questions... ]
    }
  ]
}

Question types allowed:
- "mcq": 4 options (correct index 0-3)
- "tf": 2 options ["True", "False"] (correct index 0 or 1)
- "numerical": 4 options with numerical answers

Set difficulty:
- Set 1: Beginner — fundamental concepts, definitions, basic principles
- Set 2: Intermediate — application, calculations, real-world scenarios
- Set 3: Advanced — tricky edge cases, deeper analysis, common mistakes

Rules:
- Questions must be CONCEPTUALLY RELATED to the topic, NOT copies of the original question
- Each question must have exactly the options listed (2 for tf, 4 for mcq/numerical)
- correct field is the 0-based index of the correct option
- Keep explanations under 100 words
- Make questions genuinely educational and progressively harder"""


NEW_QUIZ_PROMPT_TEMPLATE = """Generate 3 quiz sets × 5 questions each for this topic.

ORIGINAL QUESTION (for context only — do NOT copy it directly into quiz questions):
{question}

TOPIC CATEGORY: {category}

Return ONLY the JSON object. No preamble, no markdown."""


# ── Quiz panel HTML (light theme, 3 cards) ──────────────────────────

_QUIZ_PANEL_CSS = """
<style id="qanim-quiz-styles">
/* ── Quiz Panel v10.1 — One-at-a-time Design ── */
#quiz-backdrop {
  display:flex; position:fixed; inset:0; z-index:8700;
  background:rgba(15,23,42,0.48); backdrop-filter:blur(8px);
  -webkit-backdrop-filter:blur(8px);
  opacity:0; visibility:hidden; pointer-events:none;
  transition:opacity 0.25s ease, visibility 0.25s ease;
  align-items:center; justify-content:center;
  padding:16px; box-sizing:border-box;
}
#quiz-backdrop.open { visibility:visible; pointer-events:auto; opacity:1; }

#quiz-panel {
  width:min(680px,96vw); max-height:92vh;
  border-radius:20px; background:#eceaf4;
  box-shadow:0 16px 56px rgba(100,50,200,0.18);
  opacity:0; transform:translateY(16px) scale(0.97); pointer-events:none;
  transition:opacity 0.25s ease, transform 0.26s cubic-bezier(0.34,1.56,0.64,1);
  display:flex; flex-direction:column; overflow:hidden;
}
#quiz-panel.open { opacity:1; transform:none; pointer-events:auto; }

/* ── Header ── */
.qz-header {
  background:#eceaf4; padding:18px 22px 10px;
  display:flex; align-items:center; justify-content:space-between; flex-shrink:0;
}
.qz-header-left { display:flex; align-items:center; gap:10px; }
.qz-title-text h2 {
  font:800 20px/1.2 -apple-system,'Segoe UI',Arial,sans-serif;
  color:#1e293b; margin:0;
}
.qz-title-text p {
  font:500 12px/1 -apple-system,'Segoe UI',Arial,sans-serif;
  color:#64748b; margin:4px 0 0;
}
.qz-close-btn {
  width:32px; height:32px; border-radius:50%;
  border:1.5px solid #c4b5fd; background:#fff; color:#7c3aed;
  font-size:14px; cursor:pointer; display:flex;
  align-items:center; justify-content:center; transition:background 0.15s; flex-shrink:0;
}
.qz-close-btn:hover { background:#ede9fe; }

/* ── Progress area ── */
.qz-progress-area { padding:0 22px 14px; flex-shrink:0; }
.qz-progress-meta {
  display:flex; justify-content:space-between; align-items:center;
  font:600 12px/1 -apple-system,'Segoe UI',Arial,sans-serif;
  color:#7c3aed; margin-bottom:7px;
}
.qz-score-badge { font:700 12px/1 -apple-system,'Segoe UI',Arial,sans-serif; color:#64748b; }
.qz-progress-track { height:5px; background:#d8d4f0; border-radius:99px; overflow:hidden; }
.qz-progress-fill {
  height:100%; border-radius:99px;
  background:linear-gradient(90deg,#7c3aed,#db2777);
  transition:width 0.45s cubic-bezier(0.4,0,0.2,1);
}

/* ── Scrollable body ── */
.qz-body { flex:1; overflow-y:auto; padding:0 16px 20px;
  scrollbar-width:thin; scrollbar-color:#c4b5fd transparent; }

/* ── Question card ── */
.qz-card {
  background:#fff; border-radius:16px; padding:22px 20px 18px;
  box-shadow:0 2px 14px rgba(100,50,200,0.09);
}
.qz-card-meta {
  display:flex; align-items:center; gap:8px; margin-bottom:14px; flex-wrap:wrap;
}
.qz-card-counter {
  font:700 11px/1 -apple-system,'Segoe UI',Arial,sans-serif;
  color:#7c3aed; text-transform:uppercase; letter-spacing:0.6px;
}
.qz-card-pts {
  font:700 10px/1 -apple-system,'Segoe UI',Arial,sans-serif;
  color:#64748b;
}
.qz-type-badge {
  font:700 10px/1 -apple-system,'Segoe UI',Arial,sans-serif;
  color:#7c3aed; background:#ede9fe; border-radius:20px;
  padding:3px 10px; text-transform:uppercase;
}
.qz-question-text {
  font:700 15px/1.65 -apple-system,'Segoe UI',Arial,sans-serif;
  color:#1e293b; margin:0 0 16px;
}

/* ── Hint ── */
.qz-hint-toggle {
  display:inline-flex; align-items:center; gap:6px;
  padding:7px 14px; border-radius:10px;
  border:1.5px dashed #e2e8f0; background:#fafafa;
  color:#64748b; font:600 12px/1 -apple-system,'Segoe UI',Arial,sans-serif;
  cursor:pointer; margin-bottom:12px;
  transition:background 0.15s,border-color 0.15s,color 0.15s;
}
.qz-hint-toggle:hover { background:#fef9c3; border-color:#fde047; color:#854d0e; }
.qz-hint-box {
  display:none; background:#fef9c3; border-radius:8px;
  padding:10px 13px; margin-bottom:14px;
  font:400 12.5px/1.65 -apple-system,'Segoe UI',Arial,sans-serif;
  color:#713f12; border-left:3px solid #fbbf24;
}
.qz-hint-box.show { display:block; }

/* ── Options ── */
.qz-options { display:flex; flex-direction:column; gap:9px; margin-bottom:14px; }
.qz-opt {
  display:flex; align-items:center; gap:12px; padding:11px 14px;
  border-radius:11px; border:1.5px solid #e2e8f0; background:#f8fafc;
  cursor:pointer; transition:background 0.15s,border-color 0.15s,transform 0.12s;
  font:500 13px/1.5 -apple-system,'Segoe UI',Arial,sans-serif; color:#334155;
  text-align:left; width:100%;
}
.qz-opt:hover:not([disabled]) {
  background:#ede9fe; border-color:#a78bfa; transform:translateX(3px);
}
.qz-opt-letter {
  width:26px; height:26px; border-radius:50%; flex-shrink:0;
  background:#e2e8f0; color:#64748b;
  font:700 12px/26px -apple-system,sans-serif; text-align:center;
  transition:background 0.15s,color 0.15s;
}
.qz-opt.correct { background:#f0fdf4; border-color:#4ade80; color:#166534; }
.qz-opt.correct .qz-opt-letter { background:#22c55e; color:#fff; }
.qz-opt.wrong { background:#fef2f2; border-color:#f87171; color:#991b1b; }
.qz-opt.wrong .qz-opt-letter { background:#ef4444; color:#fff; }

/* ── Explanation ── */
.qz-expl {
  display:none; background:#f1f5f9; border-radius:9px;
  padding:12px 14px; margin-bottom:6px;
  font:400 12.5px/1.7 -apple-system,'Segoe UI',Arial,sans-serif;
  color:#475569; border-left:3px solid #7c3aed;
}
.qz-expl.show { display:block; }

/* ── Next button ── */
.qz-next-btn {
  display:none; width:100%; padding:14px; margin-top:14px;
  border-radius:12px; border:none;
  background:linear-gradient(135deg,#7c3aed,#db2777);
  color:#fff; font:700 14px/1 -apple-system,'Segoe UI',Arial,sans-serif;
  cursor:pointer; letter-spacing:0.3px;
  transition:opacity 0.15s, transform 0.1s;
}
.qz-next-btn:hover { opacity:0.92; transform:translateY(-1px); }
.qz-next-btn:active { transform:translateY(0); }
.qz-next-btn.show { display:block; }

/* ── Finish screen ── */
.qz-finish { text-align:center; padding:28px 16px; }
.qz-finish-emoji { font-size:50px; display:block; margin-bottom:14px; }
.qz-finish h3 {
  font:800 20px/1.2 -apple-system,'Segoe UI',Arial,sans-serif;
  color:#1e293b; margin:0 0 8px;
}
.qz-finish p {
  font:500 13px/1.6 -apple-system,'Segoe UI',Arial,sans-serif;
  color:#64748b; margin:0 0 18px;
}
.qz-restart-btn {
  padding:11px 28px; border-radius:10px; border:none;
  background:linear-gradient(135deg,#7c3aed,#db2777);
  color:#fff; font:700 13px/1 -apple-system,sans-serif; cursor:pointer;
  transition:opacity 0.15s;
}
.qz-restart-btn:hover { opacity:0.9; }

/* ── Loading ── */
.qz-loading {
  text-align:center; padding:40px 20px;
  font:500 14px/1 -apple-system,'Segoe UI',Arial,sans-serif; color:#64748b;
}
.qz-spin { font-size:28px; display:block; margin-bottom:12px; animation:qz-spin 1s linear infinite; }
@keyframes qz-spin { to { transform:rotate(360deg); } }
</style>
"""

_QUIZ_PANEL_DOM = """
<div id="quiz-backdrop" aria-hidden="true">
<div id="quiz-panel" role="dialog" aria-label="Quiz" aria-hidden="true">
  <div class="qz-header">
    <div class="qz-header-left">
      <span style="font-size:28px;line-height:1">🔥</span>
      <div class="qz-title-text">
        <h2 id="qz-panel-title">Quiz</h2>
        <p id="qz-panel-sub">Loading…</p>
      </div>
    </div>
    <button class="qz-close-btn" id="quiz-close-btn">✕</button>
  </div>
  <div class="qz-progress-area">
    <div class="qz-progress-meta">
      <span id="qz-q-counter">Question 1 of 15</span>
      <span class="qz-score-badge" id="qz-score-display">Score: 0</span>
    </div>
    <div class="qz-progress-track">
      <div class="qz-progress-fill" id="qz-progress-fill" style="width:0%"></div>
    </div>
  </div>
  <div class="qz-body" id="qz-body">
    <div class="qz-loading">
      <span class="qz-spin">⟳</span>
      Generating quiz questions…
    </div>
  </div>
</div>
</div>
"""

_QUIZ_PANEL_JS = r"""
(function initQuizSystem() {
  'use strict';
  var quizOpen = false;
  var _built   = false;
  var _allQ    = [];
  var _cur     = 0;
  var _score   = 0;
  var _answered = false;

  function _el(id) { return document.getElementById(id); }
  function _onReady(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else setTimeout(fn, 0);
  }
  function _esc(s) {
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }
  function _loadData() {
    try { var t = document.getElementById('__quiz_data__'); return t ? JSON.parse(t.textContent) : null; }
    catch(e) { return null; }
  }
  function _flatten(data) {
    var all = [];
    if (!data || !data.sets) return all;
    for (var si = 0; si < data.sets.length; si++) {
      var set = data.sets[si];
      var qs = set.questions || [];
      for (var qi = 0; qi < qs.length; qi++) {
        all.push({
          q:           qs[qi].q || qs[qi].question || '',
          type:        qs[qi].type || 'mcq',
          options:     qs[qi].options || [],
          correct:     qs[qi].correct || 0,
          explanation: qs[qi].explanation || '',
          hint:        (qs[qi].explanation || '').split('.')[0] + '.'
        });
      }
    }
    return all;
  }

  function _renderQ(idx) {
    var body  = _el('qz-body');
    var total = _allQ.length;
    if (!body) return;
    if (idx >= total) { _renderFinish(); return; }

    var q = _allQ[idx];
    var letters = ['A','B','C','D'];

    /* Update header chrome */
    var counter = _el('qz-q-counter');
    if (counter) counter.textContent = 'Question ' + (idx+1) + ' of ' + total;
    var fill = _el('qz-progress-fill');
    if (fill) fill.style.width = Math.round(idx / total * 100) + '%';
    var scoreEl = _el('qz-score-display');
    if (scoreEl) scoreEl.textContent = 'Score: ' + _score;

    var typeLabel = (q.type || 'mcq').toUpperCase();
    var optHtml = '';
    for (var oi = 0; oi < q.options.length; oi++) {
      optHtml += '<button class="qz-opt" data-opt="' + oi + '" data-correct="' + q.correct + '">' +
        '<span class="qz-opt-letter">' + (letters[oi] || oi) + '</span>' +
        _esc(q.options[oi]) + '</button>';
    }

    body.innerHTML =
      '<div class="qz-card">' +
        '<div class="qz-card-meta">' +
          '<span class="qz-card-counter">QUESTION ' + (idx+1) + ' OF ' + total + '</span>' +
          '<span class="qz-card-pts">&nbsp;&middot;&nbsp;2 PTS</span>' +
          '<span class="qz-type-badge">' + typeLabel + '</span>' +
        '</div>' +
        '<p class="qz-question-text">' + _esc(q.q) + '</p>' +
        '<div class="qz-hint-toggle" id="qz-hint-btn">\uD83D\uDCA1 Show Hint</div>' +
        '<div class="qz-hint-box" id="qz-hint-box">' + _esc(q.hint) + '</div>' +
        '<div class="qz-options" id="qz-opts">' + optHtml + '</div>' +
        '<div class="qz-expl" id="qz-expl">' + _esc(q.explanation) + '</div>' +
        '<button class="qz-next-btn" id="qz-next-btn">' +
          (idx < total - 1 ? 'Next Question \u2192' : 'See Results') +
        '</button>' +
      '</div>';

    _answered = false;

    /* Hint toggle */
    var hintBtn = document.getElementById('qz-hint-btn');
    var hintBox = document.getElementById('qz-hint-box');
    if (hintBtn && hintBox) {
      hintBtn.addEventListener('click', function() {
        var open = hintBox.classList.toggle('show');
        hintBtn.innerHTML = (open ? '\uD83D\uDCA1 Hide Hint' : '\uD83D\uDCA1 Show Hint');
      });
    }

    /* Option click */
    var optsEl = document.getElementById('qz-opts');
    if (optsEl) {
      optsEl.addEventListener('click', function(e) {
        if (_answered) return;
        var btn = e.target.closest('.qz-opt');
        if (!btn) return;
        _answered = true;

        var chosen  = parseInt(btn.getAttribute('data-opt'), 10);
        var correct = parseInt(btn.getAttribute('data-correct'), 10);

        optsEl.querySelectorAll('.qz-opt').forEach(function(o) {
          o.setAttribute('disabled', '1');
          o.style.pointerEvents = 'none';
          o.style.cursor = 'default';
          var oi2 = parseInt(o.getAttribute('data-opt'), 10);
          if (oi2 === correct) o.classList.add('correct');
          else if (oi2 === chosen && chosen !== correct) o.classList.add('wrong');
        });

        if (chosen === correct) _score++;

        var explEl = document.getElementById('qz-expl');
        if (explEl) explEl.classList.add('show');

        var nextBtn = document.getElementById('qz-next-btn');
        if (nextBtn) nextBtn.classList.add('show');

        var scoreEl2 = _el('qz-score-display');
        if (scoreEl2) scoreEl2.textContent = 'Score: ' + _score;
      });
    }

    /* Next button */
    var nb = document.getElementById('qz-next-btn');
    if (nb) {
      nb.addEventListener('click', function() {
        _cur++;
        _renderQ(_cur);
      });
    }

    body.scrollTop = 0;
  }

  function _renderFinish() {
    var body  = _el('qz-body');
    var total = _allQ.length;
    var pct   = Math.round(_score / total * 100);
    var emoji = pct >= 80 ? '\uD83C\uDF89' : pct >= 60 ? '\uD83D\uDC4D' : '\uD83D\uDCDA';
    var msg   = pct >= 80 ? 'Excellent work!' : pct >= 60 ? 'Good effort!' : 'Keep practising!';

    var fill = _el('qz-progress-fill');
    if (fill) fill.style.width = '100%';
    var counter = _el('qz-q-counter');
    if (counter) counter.textContent = 'Completed!';

    body.innerHTML =
      '<div class="qz-card qz-finish">' +
        '<span class="qz-finish-emoji">' + emoji + '</span>' +
        '<h3>' + msg + '</h3>' +
        '<p>You scored <strong>' + _score + ' / ' + total + '</strong> (' + pct + '%)</p>' +
        '<button class="qz-restart-btn" id="qz-restart-btn">Try Again</button>' +
      '</div>';

    var rb = document.getElementById('qz-restart-btn');
    if (rb) {
      rb.addEventListener('click', function() {
        _cur = 0; _score = 0; _answered = false;
        var f = _el('qz-progress-fill'); if (f) f.style.width = '0%';
        var s = _el('qz-score-display'); if (s) s.textContent = 'Score: 0';
        _renderQ(0);
      });
    }
  }

  function _init() {
    if (_built) return;
    _built = true;
    var data = _loadData();
    _allQ = _flatten(data);

    /* Update subtitle */
    var sub = _el('qz-panel-sub');
    if (sub) sub.textContent = _allQ.length + ' Questions';

    if (_allQ.length === 0) {
      var body = _el('qz-body');
      if (body) body.innerHTML = '<div class="qz-loading">No questions available.</div>';
      return;
    }
    _renderQ(0);
  }

  function openQuiz() {
    var backdrop = _el('quiz-backdrop'), panel = _el('quiz-panel');
    if (!backdrop || !panel) return;
    if (!_built) _init();
    backdrop.classList.add('open'); backdrop.setAttribute('aria-hidden', 'false');
    panel.classList.add('open');    panel.setAttribute('aria-hidden', 'false');
    quizOpen = true;
    var body = _el('qz-body'); if (body) body.scrollTop = 0;
  }
  function closeQuiz() {
    var backdrop = _el('quiz-backdrop'), panel = _el('quiz-panel');
    if (backdrop) { backdrop.classList.remove('open'); backdrop.setAttribute('aria-hidden', 'true'); }
    if (panel)    { panel.classList.remove('open');    panel.setAttribute('aria-hidden', 'true'); }
    quizOpen = false;
  }

  window.openQuiz  = openQuiz;
  window.closeQuiz = closeQuiz;

  _onReady(function() {
    var qBtn = _el('quiz-ctrl-btn');
    if (qBtn) qBtn.addEventListener('click', function(e) { e.stopPropagation(); quizOpen ? closeQuiz() : openQuiz(); });
    var cBtn = _el('quiz-close-btn');
    if (cBtn) cBtn.addEventListener('click', function(e) { e.stopPropagation(); closeQuiz(); });
    var bd = _el('quiz-backdrop');
    if (bd) bd.addEventListener('click', function(e) { if (e.target === bd) closeQuiz(); });
    document.addEventListener('keydown', function(e) { if (e.key === 'Escape' && quizOpen) closeQuiz(); });
  });
})();
"""


class QuizGeneratorV2:
    """
    Generates 3 quiz sets × 5 questions each as JSON.
    Returns quiz JSON data and builds the embeddable HTML panel.
    Never raises — returns fallback on any failure.
    """

    @classmethod
    async def generate(cls, question: str, category: str) -> dict:
        """Generate 3×5 quiz questions as structured JSON."""
        QAnimLogger.info("QuizGenV2", f"Generating 3×5 quiz for category={category}")
        prompt = NEW_QUIZ_PROMPT_TEMPLATE.format(
            question=question[:400],
            category=category
        )
        try:
            msg = client.messages.create(
                model=QUIZ_MODEL,
                max_tokens=MAX_TOK_QUIZ,
                system=NEW_QUIZ_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = msg.content[0].text.strip()
            QAnimLogger.info("QuizGenV2", f"model={QUIZ_MODEL}  stop_reason={msg.stop_reason}  len={len(raw)}")

            # Strip code fences if present
            raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.MULTILINE).strip()

            data = json.loads(raw)
            if not isinstance(data, dict) or 'sets' not in data:
                raise ValueError("Missing 'sets' key in response")

            QAnimLogger.ok("QuizGenV2", f"Generated {len(data['sets'])} sets")
            return data

        except Exception as e:
            QAnimLogger.error("QuizGenV2", f"Generation failed: {e} — using fallback")
            return cls._fallback_data(question, category)

    @classmethod
    def _fallback_data(cls, question: str, category: str) -> dict:
        """Returns a minimal 3×5 fallback quiz dataset."""
        return {
            "sets": [
                {
                    "title": "Set 1: Core Concepts",
                    "focus": "Fundamental principles",
                    "questions": [
                        {"q": "What is the primary principle governing this type of problem?",
                         "type": "mcq",
                         "options": ["Conservation of energy", "Newton's third law", "Fundamental governing law", "Superposition principle"],
                         "correct": 2, "explanation": "The core principle depends on the topic — review the animation to identify it."},
                        {"q": "True or False: All variables in this problem are independent of each other.",
                         "type": "tf",
                         "options": ["True", "False — they are related by the governing equation"],
                         "correct": 1, "explanation": "Variables are linked through the governing equation; changing one affects others."},
                        {"q": "Why is it important to identify all given quantities before solving?",
                         "type": "mcq",
                         "options": ["To choose the correct formula", "It isn't necessary", "Only for physics problems", "To add length"],
                         "correct": 0, "explanation": "Identifying givens ensures formula selection is correct and no constraints are missed."},
                        {"q": "Which step should come FIRST when solving this type of problem?",
                         "type": "mcq",
                         "options": ["Calculate the answer immediately", "Identify knowns and unknowns", "Write any formula", "Check units last"],
                         "correct": 1, "explanation": "Identifying knowns and unknowns first is essential to structured problem-solving."},
                        {"q": "What does a correct solution always include?",
                         "type": "mcq",
                         "options": ["Correct formula, substitution, and answer with units", "Just the numerical answer", "Formulas only", "Units are optional"],
                         "correct": 0, "explanation": "A complete solution needs the formula, substituted values, calculated result, and proper units."},
                    ]
                },
                {
                    "title": "Set 2: Application",
                    "focus": "Problem-solving scenarios",
                    "questions": [
                        {"q": "If all other variables are held constant, what happens when the main variable doubles?",
                         "type": "mcq",
                         "options": ["The result stays the same", "The result doubles", "The result halves", "The result squares"],
                         "correct": 1, "explanation": "In a direct proportional relationship, doubling one variable doubles the result."},
                        {"q": "Which formula is most relevant to this category of problem?",
                         "type": "mcq",
                         "options": ["The governing equation for this domain", "E = mc²", "F = ma", "V = IR"],
                         "correct": 0, "explanation": "The correct formula depends on the specific topic — check the concept animation."},
                        {"q": "True or False: Units must always be consistent when applying formulas.",
                         "type": "tf",
                         "options": ["True", "False"],
                         "correct": 0, "explanation": "Unit consistency is critical; mixing units (e.g., kg and g) leads to incorrect answers."},
                        {"q": "In a multi-step problem, what is the safest approach?",
                         "type": "mcq",
                         "options": ["Solve all steps in one go", "Solve step by step, checking each result", "Skip intermediate steps", "Estimate only"],
                         "correct": 1, "explanation": "Step-by-step solving reduces errors and makes it easier to check work."},
                        {"q": "What does a negative result typically indicate in physics problems?",
                         "type": "mcq",
                         "options": ["An error was made", "Direction opposite to chosen positive direction", "The answer is zero", "Units are wrong"],
                         "correct": 1, "explanation": "A negative sign indicates direction (or sign convention), not necessarily an error."},
                    ]
                },
                {
                    "title": "Set 3: Advanced",
                    "focus": "Nuanced and challenging questions",
                    "questions": [
                        {"q": "Which condition would make this problem unsolvable with the standard approach?",
                         "type": "mcq",
                         "options": ["Insufficient given data", "Too many given values", "Large numbers", "Metric units"],
                         "correct": 0, "explanation": "Without enough independent equations or given values, the system is underdetermined."},
                        {"q": "True or False: Approximation methods always give less accurate results than exact methods.",
                         "type": "tf",
                         "options": ["True", "False — approximations can be very accurate within stated tolerances"],
                         "correct": 1, "explanation": "Approximations are designed to be accurate within defined error bounds and are widely used in practice."},
                        {"q": "A student gets a numerically correct answer but with wrong units. Is this considered correct?",
                         "type": "mcq",
                         "options": ["Yes — numbers are what matter", "No — units are integral to the answer", "Sometimes, depending on context", "Only in pure mathematics"],
                         "correct": 1, "explanation": "Units are fundamental to physical quantities; a wrong unit means a meaningfully different answer."},
                        {"q": "What is the primary source of significant error in manual calculations?",
                         "type": "mcq",
                         "options": ["Rounding intermediate results too aggressively", "Using exact values", "Following the formula", "Writing all steps"],
                         "correct": 0, "explanation": "Premature rounding of intermediate results accumulates errors that affect the final answer."},
                        {"q": "If two different methods give slightly different numerical answers, what should you do?",
                         "type": "mcq",
                         "options": ["Choose the larger value", "Choose the smaller value", "Check both methods for errors and identify which is exact", "Average the two values"],
                         "correct": 2, "explanation": "Different methods may use different approximations. Identify which is more exact or where rounding occurred."},
                    ]
                }
            ]
        }

    @classmethod
    def build_standalone_html(cls, data: dict, question: str, category: str) -> str:
        """Builds a standalone one-at-a-time quiz HTML from quiz data."""
        q_safe   = html_module.escape(question[:100])
        cat_safe = html_module.escape(category)
        data_json = json.dumps(data, ensure_ascii=False)
        total_q = sum(len(s.get('questions', [])) for s in data.get('sets', []))

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QAnim Quiz — {cat_safe}</title>
<script type="application/json" id="__quiz_data__">{data_json}</script>
<style>
*, *::before, *::after {{ margin:0; padding:0; box-sizing:border-box; }}
html, body {{
  min-height:100%; background:#eceaf4;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
}}
.page-wrap {{ max-width:680px; margin:0 auto; padding:24px 16px 40px; }}

/* Header card */
.qz-header-card {{
  background:#fff; border-radius:16px; padding:20px 22px;
  box-shadow:0 2px 14px rgba(100,50,200,0.09);
  margin-bottom:14px; display:flex; align-items:center; gap:14px;
}}
.qz-header-card h1 {{
  font:800 20px/1.2 -apple-system,'Segoe UI',Arial,sans-serif; color:#1e293b; margin:0;
}}
.qz-header-card p {{
  font:500 12px/1 -apple-system,'Segoe UI',Arial,sans-serif; color:#64748b; margin:4px 0 0;
}}

/* Progress */
.qz-progress-row {{
  display:flex; justify-content:space-between; align-items:center;
  font:600 12px/1 -apple-system,'Segoe UI',Arial,sans-serif;
  color:#7c3aed; margin-bottom:7px;
}}
.qz-score-lbl {{ font:700 12px/1 -apple-system,'Segoe UI',Arial,sans-serif; color:#64748b; }}
.qz-track {{ height:5px; background:#d8d4f0; border-radius:99px; overflow:hidden; margin-bottom:16px; }}
.qz-fill {{
  height:100%; border-radius:99px;
  background:linear-gradient(90deg,#7c3aed,#db2777);
  transition:width 0.45s cubic-bezier(0.4,0,0.2,1);
}}

/* Question card */
.qz-card {{
  background:#fff; border-radius:16px; padding:22px 20px 18px;
  box-shadow:0 2px 14px rgba(100,50,200,0.09);
}}
.qz-meta {{ display:flex; align-items:center; gap:8px; margin-bottom:14px; flex-wrap:wrap; }}
.qz-counter {{ font:700 11px/1 -apple-system,'Segoe UI',Arial,sans-serif; color:#7c3aed; text-transform:uppercase; letter-spacing:0.6px; }}
.qz-pts {{ font:700 10px/1 -apple-system,'Segoe UI',Arial,sans-serif; color:#64748b; }}
.qz-badge {{
  font:700 10px/1 -apple-system,'Segoe UI',Arial,sans-serif; color:#7c3aed;
  background:#ede9fe; border-radius:20px; padding:3px 10px; text-transform:uppercase;
}}
.qz-qtext {{ font:700 15px/1.65 -apple-system,'Segoe UI',Arial,sans-serif; color:#1e293b; margin:0 0 16px; }}

.qz-hint-btn {{
  display:inline-flex; align-items:center; gap:6px; padding:7px 14px;
  border-radius:10px; border:1.5px dashed #e2e8f0; background:#fafafa;
  color:#64748b; font:600 12px/1 -apple-system,'Segoe UI',Arial,sans-serif;
  cursor:pointer; margin-bottom:12px;
  transition:background 0.15s,border-color 0.15s;
}}
.qz-hint-btn:hover {{ background:#fef9c3; border-color:#fde047; color:#854d0e; }}
.qz-hint-content {{
  display:none; background:#fef9c3; border-radius:8px; padding:10px 13px; margin-bottom:14px;
  font:400 12.5px/1.65 -apple-system,'Segoe UI',Arial,sans-serif; color:#713f12;
  border-left:3px solid #fbbf24;
}}
.qz-hint-content.show {{ display:block; }}

.qz-opts {{ display:flex; flex-direction:column; gap:9px; margin-bottom:14px; }}
.qz-opt {{
  display:flex; align-items:center; gap:12px; padding:11px 14px;
  border-radius:11px; border:1.5px solid #e2e8f0; background:#f8fafc;
  cursor:pointer; font:500 13px/1.5 -apple-system,'Segoe UI',Arial,sans-serif;
  color:#334155; text-align:left; width:100%;
  transition:background 0.15s,border-color 0.15s,transform 0.12s;
}}
.qz-opt:hover:not([disabled]) {{ background:#ede9fe; border-color:#a78bfa; transform:translateX(3px); }}
.qz-ltr {{
  width:26px; height:26px; border-radius:50%; flex-shrink:0; background:#e2e8f0;
  color:#64748b; font:700 12px/26px -apple-system,sans-serif; text-align:center;
  transition:background 0.15s,color 0.15s;
}}
.qz-opt.correct {{ background:#f0fdf4; border-color:#4ade80; color:#166534; }}
.qz-opt.correct .qz-ltr {{ background:#22c55e; color:#fff; }}
.qz-opt.wrong {{ background:#fef2f2; border-color:#f87171; color:#991b1b; }}
.qz-opt.wrong .qz-ltr {{ background:#ef4444; color:#fff; }}

.qz-expl {{
  display:none; background:#f1f5f9; border-radius:9px; padding:12px 14px;
  font:400 12.5px/1.7 -apple-system,'Segoe UI',Arial,sans-serif; color:#475569;
  border-left:3px solid #7c3aed; margin-bottom:6px;
}}
.qz-expl.show {{ display:block; }}

.qz-next {{
  display:none; width:100%; padding:14px; margin-top:14px;
  border-radius:12px; border:none;
  background:linear-gradient(135deg,#7c3aed,#db2777);
  color:#fff; font:700 14px/1 -apple-system,'Segoe UI',Arial,sans-serif;
  cursor:pointer; letter-spacing:0.3px;
  transition:opacity 0.15s,transform 0.1s;
}}
.qz-next:hover {{ opacity:0.92; transform:translateY(-1px); }}
.qz-next.show {{ display:block; }}

.qz-finish {{ text-align:center; padding:28px 16px; }}
.qz-finish-emoji {{ font-size:50px; display:block; margin-bottom:14px; }}
.qz-finish h3 {{ font:800 20px/1.2 -apple-system,'Segoe UI',Arial,sans-serif; color:#1e293b; margin:0 0 8px; }}
.qz-finish p {{ font:500 13px/1.6 -apple-system,'Segoe UI',Arial,sans-serif; color:#64748b; margin:0 0 18px; }}
.qz-restart {{
  padding:11px 28px; border-radius:10px; border:none;
  background:linear-gradient(135deg,#7c3aed,#db2777);
  color:#fff; font:700 13px/1 -apple-system,sans-serif; cursor:pointer;
}}
</style>
</head>
<body>
<div class="page-wrap">
  <div class="qz-header-card">
    <span style="font-size:32px;line-height:1">🔥</span>
    <div>
      <h1>{cat_safe} Quiz</h1>
      <p id="sa-sub">{q_safe[:60]}{'…' if len(q_safe)>60 else ''} &middot; {total_q} Questions</p>
    </div>
  </div>
  <div class="qz-progress-row">
    <span id="sa-counter">Question 1 of {total_q}</span>
    <span class="qz-score-lbl" id="sa-score">Score: 0</span>
  </div>
  <div class="qz-track"><div class="qz-fill" id="sa-fill" style="width:0%"></div></div>
  <div id="sa-body"></div>
</div>
<script>
(function() {{
  'use strict';
  var _all = []; var _cur = 0; var _score = 0; var _answered = false;

  function _esc(s) {{
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }}
  function _flatten(data) {{
    var all = [];
    if (!data || !data.sets) return all;
    for (var si = 0; si < data.sets.length; si++) {{
      var qs = data.sets[si].questions || [];
      for (var qi = 0; qi < qs.length; qi++) {{
        all.push({{
          q: qs[qi].q||qs[qi].question||'',
          type: qs[qi].type||'mcq',
          options: qs[qi].options||[],
          correct: qs[qi].correct||0,
          explanation: qs[qi].explanation||'',
          hint: (qs[qi].explanation||'').split('.')[0]+'.'
        }});
      }}
    }}
    return all;
  }}

  function renderQ(idx) {{
    var body = document.getElementById('sa-body');
    var total = _all.length;
    if (!body) return;
    if (idx >= total) {{ renderFinish(); return; }}

    var q = _all[idx];
    var letters = ['A','B','C','D'];

    document.getElementById('sa-counter').textContent = 'Question '+(idx+1)+' of '+total;
    document.getElementById('sa-fill').style.width = Math.round(idx/total*100)+'%';
    document.getElementById('sa-score').textContent = 'Score: '+_score;

    var opts = '';
    for (var oi = 0; oi < q.options.length; oi++) {{
      opts += '<button class="qz-opt" data-opt="'+oi+'" data-correct="'+q.correct+'">'+
        '<span class="qz-ltr">'+(letters[oi]||oi)+'</span>'+_esc(q.options[oi])+'</button>';
    }}

    body.innerHTML =
      '<div class="qz-card">'+
        '<div class="qz-meta">'+
          '<span class="qz-counter">QUESTION '+(idx+1)+' OF '+total+'</span>'+
          '<span class="qz-pts">&nbsp;&middot;&nbsp;2 PTS</span>'+
          '<span class="qz-badge">'+(q.type||'mcq').toUpperCase()+'</span>'+
        '</div>'+
        '<p class="qz-qtext">'+_esc(q.q)+'</p>'+
        '<div class="qz-hint-btn" id="sa-hint-btn">\uD83D\uDCA1 Show Hint</div>'+
        '<div class="qz-hint-content" id="sa-hint-box">'+_esc(q.hint)+'</div>'+
        '<div class="qz-opts" id="sa-opts">'+opts+'</div>'+
        '<div class="qz-expl" id="sa-expl">'+_esc(q.explanation)+'</div>'+
        '<button class="qz-next show" id="sa-next" style="display:none">'+
          (idx < total-1 ? 'Next Question \u2192' : 'See Results')+
        '</button>'+
      '</div>';

    _answered = false;

    var hBtn = document.getElementById('sa-hint-btn');
    var hBox = document.getElementById('sa-hint-box');
    if (hBtn && hBox) {{
      hBtn.addEventListener('click', function() {{
        var o = hBox.classList.toggle('show');
        hBtn.innerHTML = o ? '\uD83D\uDCA1 Hide Hint' : '\uD83D\uDCA1 Show Hint';
      }});
    }}

    var optsEl = document.getElementById('sa-opts');
    if (optsEl) {{
      optsEl.addEventListener('click', function(e) {{
        if (_answered) return;
        var btn = e.target.closest('.qz-opt');
        if (!btn) return;
        _answered = true;
        var chosen = parseInt(btn.getAttribute('data-opt'),10);
        var correct = parseInt(btn.getAttribute('data-correct'),10);
        optsEl.querySelectorAll('.qz-opt').forEach(function(o) {{
          o.setAttribute('disabled','1'); o.style.pointerEvents='none'; o.style.cursor='default';
          var oi2 = parseInt(o.getAttribute('data-opt'),10);
          if (oi2===correct) o.classList.add('correct');
          else if (oi2===chosen && chosen!==correct) o.classList.add('wrong');
        }});
        if (chosen===correct) _score++;
        var ex = document.getElementById('sa-expl'); if (ex) ex.classList.add('show');
        var nb = document.getElementById('sa-next'); if (nb) nb.style.display='block';
        document.getElementById('sa-score').textContent = 'Score: '+_score;
      }});
    }}

    var nb2 = document.getElementById('sa-next');
    if (nb2) {{ nb2.addEventListener('click', function() {{ _cur++; renderQ(_cur); }}); }}

    body.scrollTop = 0;
    window.scrollTo(0,0);
  }}

  function renderFinish() {{
    var body = document.getElementById('sa-body');
    var total = _all.length;
    var pct = Math.round(_score/total*100);
    var emoji = pct>=80?'\uD83C\uDF89':pct>=60?'\uD83D\uDC4D':'\uD83D\uDCDA';
    var msg = pct>=80?'Excellent work!':pct>=60?'Good effort!':'Keep practising!';
    document.getElementById('sa-fill').style.width='100%';
    document.getElementById('sa-counter').textContent='Completed!';
    body.innerHTML =
      '<div class="qz-card qz-finish">'+
        '<span class="qz-finish-emoji">'+emoji+'</span>'+
        '<h3>'+msg+'</h3>'+
        '<p>You scored <strong>'+_score+' / '+total+'</strong> ('+pct+'%)</p>'+
        '<button class="qz-restart" id="sa-restart">Try Again</button>'+
      '</div>';
    var rb = document.getElementById('sa-restart');
    if (rb) {{ rb.addEventListener('click', function() {{
      _cur=0; _score=0; _answered=false;
      document.getElementById('sa-fill').style.width='0%';
      document.getElementById('sa-score').textContent='Score: 0';
      renderQ(0);
    }}); }}
  }}

  document.addEventListener('DOMContentLoaded', function() {{
    try {{
      var tag = document.getElementById('__quiz_data__');
      if (tag) {{
        _all = _flatten(JSON.parse(tag.textContent));
        if (_all.length > 0) renderQ(0);
        else document.getElementById('sa-body').innerHTML = '<div style="text-align:center;padding:40px;color:#64748b">No questions.</div>';
      }}
    }} catch(e) {{ console.error('Quiz init error:', e); }}
  }});
}})();
</script>
</body>
</html>"""


def inject_quiz_v2_panel(html: str, quiz_data: dict) -> str:
    """Injects the 3×5 quiz panel + data + CSS + JS into animation HTML."""
    # 1. Embed quiz data as JSON tag
    try:
        data_json = json.dumps(quiz_data, ensure_ascii=False)
        data_tag = '<script type="application/json" id="__quiz_data__">\n' + data_json + '\n</script>'
        if '</head>' in html:
            html = html.replace('</head>', data_tag + '\n</head>', 1)
        else:
            html = data_tag + '\n' + html
    except Exception as e:
        QAnimLogger.warn("QuizV2Injector", f"Data tag failed: {e}")

    # 2. CSS
    try:
        if '</head>' in html:
            html = html.replace('</head>', _QUIZ_PANEL_CSS + '\n</head>', 1)
    except Exception as e:
        QAnimLogger.warn("QuizV2Injector", f"CSS failed: {e}")

    # 3. Panel DOM after <body>
    try:
        body_match = re.search(r'<body[^>]*>', html, re.IGNORECASE)
        if body_match:
            ins = body_match.end()
            html = html[:ins] + '\n' + _QUIZ_PANEL_DOM + html[ins:]
    except Exception as e:
        QAnimLogger.warn("QuizV2Injector", f"DOM failed: {e}")

    # 4. JS
    try:
        quiz_script = '<script>\n' + _QUIZ_PANEL_JS + '\n</script>'
        if '</body>' in html:
            html = html.replace('</body>', quiz_script + '\n</body>', 1)
        else:
            html += '\n' + quiz_script
    except Exception as e:
        QAnimLogger.warn("QuizV2Injector", f"JS failed: {e}")

    QAnimLogger.ok("QuizV2Injector", "3×5 quiz panel injected")
    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 9 — Answer Box System  (with validation logic)
# ══════════════════════════════════════════════════════════════════════
#
#  VALIDATION LOGIC:
#  1. Try numerical extraction from both user and correct answers.
#     - If numbers found: |user - correct| / |correct| < 0.01 → CORRECT
#                                                             < 0.15 → ALMOST CORRECT
#                                                             ≥ 0.15 → WRONG
#  2. Fall back to text comparison:
#     - Exact match (case-insensitive) → CORRECT
#     - Keyword overlap ≥ 80% → CORRECT
#     - Keyword overlap ≥ 40% → ALMOST CORRECT
#     - Keyword overlap  < 40% → WRONG
#
# ══════════════════════════════════════════════════════════════════════

_ANSWER_BOX_CSS = """
<style id="qanim-answerbox-styles">
/* ── Answer Box Panel — Light Theme ── */
#answerbox-backdrop {
  display:flex; position:fixed; inset:0; z-index:8600;
  background:rgba(15,23,42,0.40); backdrop-filter:blur(4px);
  opacity:0; visibility:hidden; pointer-events:none;
  transition:opacity 0.25s ease, visibility 0.25s ease;
  align-items:center; justify-content:center; padding:16px; box-sizing:border-box;
}
#answerbox-backdrop.open { visibility:visible; pointer-events:auto; opacity:1; }

#answerbox-panel {
  width:min(540px,94vw); max-height:90vh;
  border-radius:16px; background:#ffffff;
  border:1px solid #e2e8f0;
  box-shadow:0 12px 48px rgba(0,0,0,0.14);
  opacity:0; pointer-events:none;
  transform:translateY(16px) scale(0.97);
  transition:opacity 0.25s ease,transform 0.26s cubic-bezier(0.34,1.56,0.64,1);
  overflow:hidden; display:flex; flex-direction:column;
}
#answerbox-panel.open { opacity:1; pointer-events:auto; transform:translateY(0) scale(1); }

.ab-header {
  display:flex; align-items:center; justify-content:space-between;
  padding:16px 20px; background:#ffffff; border-bottom:1px solid #e2e8f0;
}
.ab-header-title {
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:16px; font-weight:800; color:#1e293b;
  display:flex; align-items:center; gap:8px;
}
.ab-close-btn {
  width:30px; height:30px; border-radius:8px; border:1px solid #e2e8f0;
  background:#f8fafc; color:#64748b; font-size:12px; cursor:pointer;
  display:flex; align-items:center; justify-content:center; transition:background 0.15s;
}
.ab-close-btn:hover { background:#fee2e2; color:#dc2626; }

.ab-body { padding:20px; }
.ab-instruction {
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:13px; color:#64748b; margin-bottom:14px; line-height:1.6;
}
#ab-user-input {
  width:100%; min-height:90px; padding:12px 14px; border-radius:10px;
  border:1.5px solid #e2e8f0; background:#f8fafc;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:13px; color:#1e293b; line-height:1.6; resize:vertical;
  transition:border-color 0.15s; outline:none;
}
#ab-user-input:focus { border-color:#7c3aed; background:#ffffff; }
#ab-user-input::placeholder { color:#94a3b8; }

#ab-submit-btn {
  width:100%; padding:12px; margin-top:12px; border-radius:10px; border:none;
  background:#7c3aed; color:#ffffff; font-size:14px; font-weight:700;
  font-family:inherit; cursor:pointer;
  transition:background 0.15s,transform 0.1s;
}
#ab-submit-btn:hover { background:#6d28d9; transform:translateY(-1px); }
#ab-submit-btn:active { transform:translateY(0); }

#ab-result {
  display:none; margin-top:14px; border-radius:10px; padding:14px 16px;
  border:1px solid transparent; font-family:-apple-system,'Segoe UI',Arial,sans-serif;
}
#ab-result.show { display:block; }
#ab-result.correct  { background:#f0fdf4; border-color:#bbf7d0; }
#ab-result.wrong    { background:#fef2f2; border-color:#fecaca; }
#ab-result.almost   { background:#fff7ed; border-color:#fed7aa; }

.ab-result-icon { font-size:24px; margin-bottom:6px; display:block; }
.ab-result-verdict {
  font-size:16px; font-weight:800; margin-bottom:4px;
}
.ab-result-verdict.correct { color:#15803d; }
.ab-result-verdict.wrong   { color:#b91c1c; }
.ab-result-verdict.almost  { color:#c2410c; }
.ab-result-msg { font-size:13px; line-height:1.6; }
.ab-result-msg.correct { color:#166534; }
.ab-result-msg.wrong   { color:#991b1b; }
.ab-result-msg.almost  { color:#9a3412; }

#ab-retry-btn {
  margin-top:10px; padding:7px 18px; border-radius:8px; border:1px solid #e2e8f0;
  background:#f8fafc; color:#64748b; font-size:12px; font-weight:600;
  font-family:inherit; cursor:pointer; transition:background 0.15s;
}
#ab-retry-btn:hover { background:#ede9fe; border-color:#7c3aed; color:#7c3aed; }
</style>
"""

_ANSWER_BOX_DOM = """
<div id="answerbox-backdrop" aria-hidden="true">
<div id="answerbox-panel" role="dialog" aria-label="Answer Box" aria-hidden="true">
  <div class="ab-header">
    <div class="ab-header-title">✏️ Answer Box</div>
    <button class="ab-close-btn" id="ab-close-btn">✕</button>
  </div>
  <div class="ab-body" style="overflow-y:auto;flex:1;">
    <p class="ab-instruction">
      Type your answer below and click <strong>Submit</strong> to check it.
      You can enter a numerical value, formula, or a brief explanation.
    </p>
    <textarea id="ab-user-input" placeholder="Type your answer here…" spellcheck="false"></textarea>
    <button id="ab-submit-btn">Submit Answer</button>
    <div id="ab-result" role="alert">
      <span class="ab-result-icon" id="ab-result-icon"></span>
      <div class="ab-result-verdict" id="ab-result-verdict"></div>
      <div class="ab-result-msg"    id="ab-result-msg"></div>
      <button id="ab-retry-btn">Try Again</button>
    </div>
  </div>
</div>
</div>
"""

_ANSWER_BOX_JS = r"""
(function initAnswerBox() {
  'use strict';
  var abOpen = false;

  function _el(id) { return document.getElementById(id); }
  function _onReady(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else setTimeout(fn, 0);
  }

  /* ── Load correct answer from embedded solution data ── */
  function _loadCorrectAnswer() {
    try {
      var tag = document.getElementById('__sol_data__');
      if (!tag) return '';
      var data = JSON.parse(tag.textContent);
      return (data && data.answer) ? String(data.answer) : '';
    } catch(e) { return ''; }
  }

  /* ── Extract first number from a string ── */
  function _extractNumbers(str) {
    var matches = str.match(/[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?/g);
    return matches ? matches.map(parseFloat).filter(function(n){ return isFinite(n); }) : [];
  }

  /* ── Core validation function ──
     Returns: 'correct' | 'almost' | 'wrong'

     NUMERICAL PATH:
       tolerance < 1%  → correct
       tolerance < 15% → almost
       else            → wrong

     TEXT PATH:
       exact match (lower, trimmed)  → correct
       keyword overlap ≥ 80%         → correct
       keyword overlap ≥ 40%         → almost
       else                          → wrong
  */
  function _validate(userAns, correctAns) {
    if (!userAns || !userAns.trim()) return 'empty';

    var userNums    = _extractNumbers(userAns);
    var correctNums = _extractNumbers(correctAns);

    /* Numerical comparison */
    if (userNums.length > 0 && correctNums.length > 0) {
      var uVal = userNums[0];
      var cVal = correctNums[0];
      var denom = Math.abs(cVal) + 1e-12;
      var relErr = Math.abs(uVal - cVal) / denom;

      if (relErr < 0.01)  return 'correct';
      if (relErr < 0.15)  return 'almost';
      return 'wrong';
    }

    /* Text comparison */
    var uClean = userAns.toLowerCase().trim().replace(/[^a-z0-9\s]/g,' ');
    var cClean = correctAns.toLowerCase().trim().replace(/[^a-z0-9\s]/g,' ');

    if (uClean === cClean) return 'correct';

    /* Keyword overlap */
    var STOP = {a:1,an:1,the:1,is:1,are:1,of:1,to:1,in:1,and:1,or:1,it:1,
                be:1,at:1,as:1,by:1,'for':1,on:1,with:1,that:1,this:1};
    function keywords(s) {
      var words = s.split(/\s+/);
      var kw = {};
      words.forEach(function(w){ if(w.length > 1 && !STOP[w]) kw[w] = true; });
      return kw;
    }
    var uKW = keywords(uClean);
    var cKW = keywords(cClean);
    var cKeys = Object.keys(cKW);
    if (cKeys.length === 0) return 'wrong';
    var matchCount = cKeys.filter(function(k){ return uKW[k]; }).length;
    var overlap = matchCount / cKeys.length;

    if (overlap >= 0.80) return 'correct';
    if (overlap >= 0.40) return 'almost';
    return 'wrong';
  }

  var _RESULTS = {
    correct: {
      icon:    "✅",
      verdict: "Correct Answer!",
      msg:     "Excellent work! Your answer matches the solution. You understood this concept well — keep it up!",
      cls:     "correct"
    },
    almost: {
      icon:    "🟡",
      verdict: "Almost Correct",
      msg:     "Your answer is close! There may be a small numerical difference or a missing detail. Review the step-by-step solution to fine-tune your understanding.",
      cls:     "almost"
    },
    wrong: {
      icon:    "❌",
      verdict: "Wrong Answer",
      msg:     "Not quite right — but every mistake is a learning opportunity! Click View Solution to see the step-by-step explanation and try again.",
      cls:     "wrong"
    },
    empty: {
      icon:    "📝",
      verdict: "Empty Answer",
      msg:     "Please type your answer before submitting.",
      cls:     "wrong"
    }
  };

  function _showResult(verdict) {
    var info    = _RESULTS[verdict] || _RESULTS['wrong'];
    var result  = _el('ab-result');
    var icon    = _el('ab-result-icon');
    var verdEl  = _el('ab-result-verdict');
    var msgEl   = _el('ab-result-msg');

    if (!result || !icon || !verdEl || !msgEl) return;

    /* Reset classes */
    result.className  = '';
    verdEl.className  = 'ab-result-verdict';
    msgEl.className   = 'ab-result-msg';

    icon.textContent    = info.icon;
    verdEl.textContent  = info.verdict;
    msgEl.textContent   = info.msg;

    result.classList.add('show', info.cls);
    verdEl.classList.add(info.cls);
    msgEl.classList.add(info.cls);

    /* Scroll result into view */
    setTimeout(function() { result.scrollIntoView({ behavior:'smooth', block:'nearest' }); }, 50);
  }

  function openAnswerBox() {
    var backdrop = _el('answerbox-backdrop'), panel = _el('answerbox-panel');
    if (!backdrop || !panel) return;
    /* Clear previous result state */
    var prevResult = _el('ab-result');
    if (prevResult) prevResult.className = '';
    var prevInp = _el('ab-user-input');
    if (prevInp) prevInp.value = '';
    backdrop.classList.add('open');
    backdrop.setAttribute('aria-hidden','false');
    panel.classList.add('open');
    panel.setAttribute('aria-hidden','false');
    abOpen = true;
    setTimeout(function() { var inp = _el('ab-user-input'); if(inp) inp.focus(); }, 150);
  }

  function closeAnswerBox() {
    var backdrop = _el('answerbox-backdrop'), panel = _el('answerbox-panel');
    if (backdrop) { backdrop.classList.remove('open'); backdrop.setAttribute('aria-hidden','true'); }
    if (panel)    { panel.classList.remove('open');    panel.setAttribute('aria-hidden','true'); }
    abOpen = false;
  }

  window.openAnswerBox  = openAnswerBox;
  window.closeAnswerBox = closeAnswerBox;

  _onReady(function() {
    /* Open button (wired from controls bar) */
    function wireControlsBtn() {
      var abBtn = document.getElementById('answerbox-ctrl-btn');
      if (abBtn) {
        abBtn.addEventListener('click', function(e) {
          e.stopPropagation();
          abOpen ? closeAnswerBox() : openAnswerBox();
        });
      } else {
        setTimeout(wireControlsBtn, 100);
      }
    }
    wireControlsBtn();

    /* Close button */
    var closeBtn = _el('ab-close-btn');
    if (closeBtn) closeBtn.addEventListener('click', function(e) { e.stopPropagation(); closeAnswerBox(); });

    /* Backdrop click */
    var backdrop = _el('answerbox-backdrop');
    if (backdrop) backdrop.addEventListener('click', function(e) { if (e.target === backdrop) closeAnswerBox(); });

    document.addEventListener('keydown', function(e) { if (e.key === 'Escape' && abOpen) closeAnswerBox(); });

    /* Submit */
    var submitBtn = _el('ab-submit-btn');
    if (submitBtn) {
      submitBtn.addEventListener('click', function() {
        var inputEl   = _el('ab-user-input');
        var userAns   = inputEl ? inputEl.value.trim() : '';
        var correctAns = _loadCorrectAnswer();
        var verdict   = _validate(userAns, correctAns);
        _showResult(verdict);
      });
    }

    /* Retry */
    var retryBtn = _el('ab-retry-btn');
    if (retryBtn) {
      retryBtn.addEventListener('click', function() {
        var inp = _el('ab-user-input');
        if (inp) inp.value = '';
        var result = _el('ab-result');
        if (result) result.className = '';
        if (inp) inp.focus();
      });
    }

    /* Enter key in textarea does NOT submit (to allow multi-line input) */
    /* Ctrl+Enter submits */
    var inp2 = _el('ab-user-input');
    if (inp2) {
      inp2.addEventListener('keydown', function(e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
          e.preventDefault();
          var sb = _el('ab-submit-btn');
          if (sb) sb.click();
        }
      });
    }

    /* Escape closes */
    document.addEventListener('keydown', function(e) {
      if (e.key === 'Escape' && abOpen) closeAnswerBox();
    });
  });
})();
"""


def inject_answer_box_panel(html: str) -> str:
    """Injects the Answer Box panel (DOM, CSS, JS) into animation HTML."""
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

    QAnimLogger.ok("AnswerBoxInjector", "Answer box panel injected")
    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 10 — Floating Controls Bar  (light theme, all 4 buttons)
# ══════════════════════════════════════════════════════════════════════

_CONTROLS_BAR_CSS = """
<style id="qanim-controls-bar-styles">
#qanim-controls-bar {
  position: fixed;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 7000;
  display: flex;
  align-items: center;
  gap: 6px;
  background: rgba(255, 255, 255, 0.97);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid #e2e8f0;
  border-radius: 14px;
  padding: 8px 12px;
  box-shadow: 0 4px 28px rgba(0,0,0,0.10), 0 1px 4px rgba(0,0,0,0.06);
  white-space: nowrap;
}
.qanim-ctrl-btn {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 7px 14px;
  border-radius: 9px;
  border: 1px solid #e2e8f0;
  background: #f8fafc;
  color: #334155;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s, transform 0.12s;
  user-select: none;
}
.qanim-ctrl-btn:hover {
  background: #ede9fe;
  border-color: #7c3aed;
  color: #6d28d9;
  transform: translateY(-1px);
}
.qanim-ctrl-btn:active { transform: translateY(0); }
.qanim-ctrl-sep {
  width: 1px; height: 22px; background: #e2e8f0; flex-shrink: 0;
}
@media (max-width: 520px) {
  #qanim-controls-bar { bottom: 10px; padding: 6px 8px; gap: 4px; }
  .qanim-ctrl-btn { padding: 6px 10px; font-size: 11px; }
  .qanim-ctrl-btn .ctrl-label { display: none; }
}
</style>
"""

_CONTROLS_BAR_DOM = """
<div id="qanim-controls-bar" role="toolbar" aria-label="QAnim Controls">
  <button class="qanim-ctrl-btn" id="tofind-btn" data-tofind-btn title="What to find">
    <span>🔍</span><span class="ctrl-label">Find</span>
  </button>
  <div class="qanim-ctrl-sep"></div>
  <button class="qanim-ctrl-btn" id="quiz-ctrl-btn" title="Take the quiz">
    <span>📝</span><span class="ctrl-label">Quiz</span>
  </button>
  <div class="qanim-ctrl-sep"></div>
  <button class="qanim-ctrl-btn" id="sol-ctrl-btn" title="View step-by-step solution" onclick="if(window.openSolution)openSolution();">
    <span>💡</span><span class="ctrl-label">View Solution</span>
  </button>
  <div class="qanim-ctrl-sep"></div>
  <button class="qanim-ctrl-btn" id="answerbox-ctrl-btn" title="Check your answer">
    <span>✏️</span><span class="ctrl-label">Answer Box</span>
  </button>
</div>
"""


def inject_controls_bar(html: str) -> str:
    """Injects the unified floating controls bar into animation HTML."""
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
        QAnimLogger.ok("ControlsBar", "Controls bar injected (Find / Quiz / View Solution / Answer Box)")
    except Exception as e:
        QAnimLogger.warn("ControlsBar", f"DOM failed: {e}")

    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 11 — Notes System  (Light Theme)
# ══════════════════════════════════════════════════════════════════════

_NOTES_CSS = """
<style id="qanim-notes-styles">
#qanim-notes-btn {
  position: fixed; top: 14px; right: 16px; z-index: 6900;
  display: flex; align-items: center; gap: 6px; padding: 7px 13px 7px 9px;
  border-radius: 9px; border: 1px solid #e2e8f0; background: #ffffff;
  color: #64748b; font-family: -apple-system,'Segoe UI',Arial,sans-serif;
  font-size: 12px; font-weight: 600; cursor: pointer;
  box-shadow: 0 2px 10px rgba(0,0,0,0.08);
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}
#qanim-notes-btn:hover { background:#fefce8; border-color:#ca8a04; color:#92400e; }

#qanim-notes-panel {
  position: fixed; top: 50px; right: 16px; z-index: 7200;
  width: 340px; max-height: 80vh; border-radius: 14px;
  background: #ffffff; border: 1px solid #e2e8f0;
  box-shadow: 0 8px 32px rgba(0,0,0,0.10);
  display: flex; flex-direction: column; overflow: hidden;
  opacity: 0; transform: translateY(-8px) scale(0.97); pointer-events: none;
  transition: opacity 0.22s ease, transform 0.22s ease;
}
#qanim-notes-panel.open { opacity: 1; transform: translateY(0) scale(1); pointer-events: auto; }

#qanim-notes-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 14px; background: #fffbeb; border-bottom: 1px solid #fef3c7;
  cursor: grab; flex-shrink: 0;
}
#qanim-notes-header:active { cursor: grabbing; }
.notes-header-title { font-family:-apple-system,'Segoe UI',Arial,sans-serif; font-size:13px; font-weight:700; color:#92400e; }
.notes-hdr-btn {
  width:24px; height:24px; border-radius:6px; border:1px solid #fde68a;
  background:rgba(255,255,255,0.6); color:#92400e; font-size:12px;
  display:flex; align-items:center; justify-content:center; cursor:pointer;
}
.notes-hdr-btn:hover { background:#fef3c7; }

#qanim-notes-tabs { display:flex; border-bottom:1px solid #f1f5f9; flex-shrink:0; }
.notes-tab {
  flex:1; padding:7px 0; text-align:center;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:11px; font-weight:600; color:#94a3b8; cursor:pointer;
  border-bottom:2px solid transparent; transition:color 0.15s,border-color 0.15s;
  text-transform:uppercase; letter-spacing:0.5px;
}
.notes-tab.active { color:#f59e0b; border-bottom-color:#f59e0b; }

#qanim-canvas-toolbar {
  display:flex; align-items:center; gap:5px; padding:6px 10px;
  background:#f8fafc; border-bottom:1px solid #f1f5f9; flex-shrink:0; flex-wrap:wrap;
}
.canvas-tool-btn {
  padding:3px 9px; border-radius:5px; border:1px solid #e2e8f0;
  background:#ffffff; color:#64748b; font-size:11px; font-weight:600; cursor:pointer;
}
.canvas-tool-btn.active { background:#fef3c7; border-color:#f59e0b; color:#92400e; }
.color-dot {
  width:16px; height:16px; border-radius:50%; cursor:pointer;
  border:2px solid transparent; transition:transform 0.12s;
}
.color-dot:hover { transform:scale(1.2); }
.color-dot.selected { border-color:#1e293b; transform:scale(1.1); }
.size-btn {
  width:20px; height:20px; border-radius:50%; border:1px solid #e2e8f0;
  background:#ffffff; color:#64748b; font-size:10px; font-weight:700;
  display:flex; align-items:center; justify-content:center; cursor:pointer;
}
.size-btn.active { background:#fef3c7; border-color:#f59e0b; color:#92400e; }
.tool-sep { width:1px; height:18px; background:#e2e8f0; flex-shrink:0; }

#qanim-canvas-wrap { flex:1 1 auto; position:relative; overflow:hidden; min-height:180px; }
#qanim-draw-canvas { display:block; width:100%; height:100%; cursor:crosshair; background:#fefce8; touch-action:none; }

#qanim-text-pane { display:none; flex-direction:column; flex:1 1 auto; overflow:hidden; }
#qanim-notes-textarea {
  flex:1 1 auto; width:100%; min-height:180px; resize:none; box-sizing:border-box;
  background:#f8fafc; border:none; outline:none;
  color:#1e293b; font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  font-size:13px; line-height:1.7; padding:12px 14px;
}
#qanim-notes-textarea::placeholder { color:#cbd5e1; }

#qanim-notes-footer {
  display:flex; align-items:center; justify-content:space-between;
  padding:6px 12px; border-top:1px solid #f1f5f9; flex-shrink:0; background:#f8fafc;
}
.notes-status { font-size:10px; color:#94a3b8; font-family:-apple-system,'Segoe UI',Arial,sans-serif; }
.notes-action-btn {
  padding:3px 10px; border-radius:5px; border:1px solid #e2e8f0;
  background:#ffffff; color:#64748b; font-size:10px; font-weight:600; cursor:pointer;
}
.notes-action-btn:hover { background:#ede9fe; border-color:#7c3aed; color:#7c3aed; }
</style>
"""

_NOTES_DOM = """
<button id="qanim-notes-btn" aria-label="Open notes">📝 Notes</button>
<div id="qanim-notes-panel" role="dialog" aria-label="Notes" aria-hidden="true">
  <div id="qanim-notes-header">
    <div class="notes-header-title">✏️ My Notes</div>
    <div style="display:flex;gap:4px">
      <button class="notes-hdr-btn" id="notes-minimize-btn" title="Minimize">—</button>
      <button class="notes-hdr-btn" id="notes-close-btn" title="Close">✕</button>
    </div>
  </div>
  <div id="qanim-notes-tabs">
    <div class="notes-tab active" data-tab="canvas">🖊 Draw</div>
    <div class="notes-tab" data-tab="text">📄 Text</div>
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
    <button class="canvas-tool-btn" id="notes-undo-btn">↩</button>
    <button class="canvas-tool-btn" id="notes-clear-btn">🗑</button>
  </div>
  <div id="qanim-canvas-wrap">
    <canvas id="qanim-draw-canvas"></canvas>
  </div>
  <div id="qanim-text-pane">
    <textarea id="qanim-notes-textarea" placeholder="Type your notes here…" spellcheck="false"></textarea>
  </div>
  <div id="qanim-notes-footer">
    <span class="notes-status" id="notes-char-count">0 chars</span>
    <button class="notes-action-btn" id="notes-export-text-btn">Export</button>
  </div>
</div>
"""

_NOTES_JS = r"""
(function initNotesSystem() {
  'use strict';
  var isOpen=false, isMin=false, isDrag=false, isDrawing=false;
  var currentTool='pen', currentColor='#1e293b', currentSize=4, currentTab='canvas';
  var undoStack=[], redoStack=[], MAX_UNDO=30;
  var dragOffX=0, dragOffY=0, autoSaveTimer=null;
  var ctx=null, canvas=null;

  function _el(id){ return document.getElementById(id); }
  function _onReady(fn){
    if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',fn);
    else setTimeout(fn,0);
  }
  function _storage(){
    try{ return window.localStorage; }
    catch(e){ if(!window._qnotes) window._qnotes={}; return {getItem:function(k){return window._qnotes[k]||null;},setItem:function(k,v){window._qnotes[k]=v;}}; }
  }

  function _saveNotes(){
    try{
      var canvasData=canvas?canvas.toDataURL():'';
      var textData=_el('qanim-notes-textarea')?_el('qanim-notes-textarea').value:'';
      _storage().setItem('qanim_notes_v10',JSON.stringify({canvas:canvasData,text:textData}));
      var stat=_el('notes-char-count');
      if(stat) stat.textContent='Saved';
    }catch(e){}
  }
  function _loadNotes(){
    try{
      var raw=_storage().getItem('qanim_notes_v10');
      if(!raw) return;
      var p=JSON.parse(raw);
      var ta=_el('qanim-notes-textarea');
      if(ta&&p.text) ta.value=p.text;
      if(canvas&&p.canvas&&p.canvas.startsWith('data:')){
        var img=new Image();
        img.onload=function(){ ctx.drawImage(img,0,0); };
        img.src=p.canvas;
      }
    }catch(e){}
  }
  function _scheduleAutoSave(){
    if(autoSaveTimer) clearTimeout(autoSaveTimer);
    autoSaveTimer=setTimeout(_saveNotes,1500);
  }
  function _initCanvas(){
    canvas=_el('qanim-draw-canvas');
    if(!canvas) return;
    ctx=canvas.getContext('2d');
    _resizeCanvas();
    ctx.lineCap='round'; ctx.lineJoin='round';
    ctx.strokeStyle=currentColor; ctx.lineWidth=currentSize;
    _loadNotes();
  }
  function _resizeCanvas(){
    if(!canvas) return;
    var wrap=_el('qanim-canvas-wrap');
    var w=wrap?wrap.clientWidth:320, h=wrap?wrap.clientHeight:200;
    var imgData=null;
    if(ctx&&canvas.width>0&&canvas.height>0){ try{imgData=ctx.getImageData(0,0,canvas.width,canvas.height);}catch(e){} }
    canvas.width=w; canvas.height=h;
    ctx.lineCap='round'; ctx.lineJoin='round';
    ctx.strokeStyle=currentColor; ctx.lineWidth=currentSize;
    if(imgData){ try{ctx.putImageData(imgData,0,0);}catch(e){} }
  }
  function _saveUndo(){ if(!canvas) return; if(undoStack.length>=MAX_UNDO) undoStack.shift(); undoStack.push(canvas.toDataURL()); redoStack=[]; }
  function _undo(){
    if(!canvas||undoStack.length===0) return;
    redoStack.push(canvas.toDataURL());
    var prev=undoStack.pop();
    if(prev){ var img=new Image(); img.onload=function(){ ctx.clearRect(0,0,canvas.width,canvas.height); ctx.drawImage(img,0,0); }; img.src=prev; }
    else ctx.clearRect(0,0,canvas.width,canvas.height);
  }
  function _getPos(e,cvs){
    var rect=cvs.getBoundingClientRect();
    var sx=cvs.width/rect.width, sy=cvs.height/rect.height;
    var cx=e.touches?e.touches[0].clientX:e.clientX;
    var cy=e.touches?e.touches[0].clientY:e.clientY;
    return{x:(cx-rect.left)*sx,y:(cy-rect.top)*sy};
  }
  function _startDraw(e){
    if(!canvas||currentTab!=='canvas') return;
    e.preventDefault(); _saveUndo(); isDrawing=true;
    var pos=_getPos(e,canvas); ctx.beginPath(); ctx.moveTo(pos.x,pos.y);
    if(currentTool==='eraser'){ ctx.globalCompositeOperation='destination-out'; ctx.lineWidth=currentSize*4; }
    else{ ctx.globalCompositeOperation='source-over'; ctx.strokeStyle=currentColor; ctx.lineWidth=currentSize; }
  }
  function _draw(e){
    if(!isDrawing||!canvas) return; e.preventDefault();
    var pos=_getPos(e,canvas); ctx.lineTo(pos.x,pos.y); ctx.stroke();
  }
  function _endDraw(){ if(!isDrawing) return; isDrawing=false; if(ctx) ctx.globalCompositeOperation='source-over'; _scheduleAutoSave(); }
  function openNotes(){
    var panel=_el('qanim-notes-panel');
    if(!panel) return;
    panel.classList.add('open'); panel.setAttribute('aria-hidden','false'); isOpen=true;
    setTimeout(function(){ _resizeCanvas(); },50);
  }
  function closeNotes(){
    var panel=_el('qanim-notes-panel');
    if(panel){ panel.classList.remove('open'); panel.setAttribute('aria-hidden','true'); }
    isOpen=false; _saveNotes();
  }
  function _switchTab(t){
    currentTab=t;
    document.querySelectorAll('.notes-tab').forEach(function(tb){ tb.classList.toggle('active',tb.dataset.tab===t); });
    var ct=_el('qanim-canvas-toolbar'), cw=_el('qanim-canvas-wrap'), tp=_el('qanim-text-pane');
    if(ct) ct.style.display=t==='canvas'?'flex':'none';
    if(cw) cw.style.display=t==='canvas'?'block':'none';
    if(tp) tp.style.display=t==='text'?'flex':'none';
    if(t==='canvas') setTimeout(_resizeCanvas,30);
  }

  _onReady(function(){
    var nb=_el('qanim-notes-btn');
    if(nb) nb.addEventListener('click',function(){ isOpen?closeNotes():openNotes(); });
    var cb=_el('notes-close-btn'); if(cb) cb.addEventListener('click',closeNotes);
    var mb=_el('notes-minimize-btn');
    if(mb) mb.addEventListener('click',function(e){ e.stopPropagation(); isMin=!isMin; var p=_el('qanim-notes-panel'); if(p) p.style.maxHeight=isMin?'44px':'80vh'; mb.textContent=isMin?'□':'—'; });
    document.querySelectorAll('.notes-tab').forEach(function(t){ t.addEventListener('click',function(){ _switchTab(this.dataset.tab); }); });
    document.querySelectorAll('.canvas-tool-btn[data-tool]').forEach(function(b){ b.addEventListener('click',function(){ currentTool=this.dataset.tool; document.querySelectorAll('.canvas-tool-btn[data-tool]').forEach(function(x){ x.classList.remove('active'); }); this.classList.add('active'); }); });
    document.querySelectorAll('.color-dot').forEach(function(d){ d.addEventListener('click',function(){ currentColor=this.dataset.color; document.querySelectorAll('.color-dot').forEach(function(x){ x.classList.remove('selected'); }); this.classList.add('selected'); if(ctx) ctx.strokeStyle=currentColor; currentTool='pen'; document.querySelectorAll('.canvas-tool-btn[data-tool]').forEach(function(b){ b.classList.toggle('active',b.dataset.tool==='pen'); }); }); });
    document.querySelectorAll('.size-btn').forEach(function(b){ b.addEventListener('click',function(){ currentSize=parseInt(this.dataset.size,10); document.querySelectorAll('.size-btn').forEach(function(x){ x.classList.remove('active'); }); this.classList.add('active'); if(ctx) ctx.lineWidth=currentSize; }); });
    var ub=_el('notes-undo-btn'); if(ub) ub.addEventListener('click',_undo);
    var clrb=_el('notes-clear-btn'); if(clrb) clrb.addEventListener('click',function(){ if(!canvas) return; _saveUndo(); ctx.clearRect(0,0,canvas.width,canvas.height); _scheduleAutoSave(); });
    var etb=_el('notes-export-text-btn'); if(etb) etb.addEventListener('click',function(){ var ta=_el('qanim-notes-textarea'); if(!ta||!ta.value) return; var blob=new Blob([ta.value],{type:'text/plain'}); var a=document.createElement('a'); a.download='qanim_notes.txt'; a.href=URL.createObjectURL(blob); a.click(); });
    var ta=_el('qanim-notes-textarea'); if(ta) ta.addEventListener('input',function(){ var c=_el('notes-char-count'); if(c) c.textContent=ta.value.length+' chars'; _scheduleAutoSave(); });
    var cvs=_el('qanim-draw-canvas');
    if(cvs){
      cvs.addEventListener('mousedown',_startDraw); cvs.addEventListener('mousemove',_draw);
      cvs.addEventListener('mouseup',_endDraw); cvs.addEventListener('mouseleave',_endDraw);
      cvs.addEventListener('touchstart',_startDraw,{passive:false}); cvs.addEventListener('touchmove',_draw,{passive:false}); cvs.addEventListener('touchend',_endDraw);
    }
    if(window.ResizeObserver){ var obs=new ResizeObserver(function(){ if(isOpen&&currentTab==='canvas') _resizeCanvas(); }); var wr=_el('qanim-canvas-wrap'); if(wr) obs.observe(wr); }
    _initCanvas();
  });
})();
"""


def inject_notes_system(html: str, question: str = "") -> str:
    """Injects the floating Notes whiteboard into animation HTML. Light theme."""
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
        QAnimLogger.warn("NotesInjector", f"DOM failed: {e}")

    try:
        notes_script = '<script>\n' + _NOTES_JS + '\n</script>'
        if '</body>' in html:
            html = html.replace('</body>', notes_script + '\n</body>', 1)
        else:
            html += '\n' + notes_script
    except Exception as e:
        QAnimLogger.warn("NotesInjector", f"JS failed: {e}")

    QAnimLogger.ok("NotesInjector", "Notes whiteboard injected (light theme)")
    return html


# ══════════════════════════════════════════════════════════════════════
#  MODULE 12 — StepController Patcher  (preserved from v9)
# ══════════════════════════════════════════════════════════════════════

_STEP_CONTROLLER_JS = r"""
<script id="qanim-step-controller">
/* QAnim Manual Step Controller v10 */
(function patchStepController() {
  'use strict';
  window.addEventListener('load', function() {
    try {
      var nextBtn = document.getElementById('nextbtn');
      var prevBtn = document.getElementById('prevbtn');
      if (!nextBtn && !prevBtn) return;

      var scenes = [];
      for (var i = 0; i < 20; i++) {
        var s = document.getElementById('scene-' + i);
        if (s) { scenes.push(s); } else if (i > 0) { break; }
      }
      if (scenes.length < 1) return;

      var _sceneSnapshots = [];
      for (var si = 0; si < scenes.length; si++) {
        var scEl = scenes[si];
        _sceneSnapshots.push({
          display: scEl.style.display, opacity: scEl.style.opacity,
          visibility: scEl.style.visibility, transform: scEl.style.transform,
          transition: scEl.style.transition,
          children: (function(root) {
            var result = [], all = root.querySelectorAll('*');
            for (var ci = 0; ci < all.length; ci++) {
              result.push({ el: all[ci], opacity: all[ci].style.opacity,
                transform: all[ci].style.transform, display: all[ci].style.display,
                visibility: all[ci].style.visibility, transition: all[ci].style.transition });
            }
            return result;
          })(scEl)
        });
      }

      var _animFired = {};
      var _aiShowScene = (typeof window.showScene === 'function') ? window.showScene : null;
      var currentStep = 0;

      function _resetScene(idx) {
        var snap = _sceneSnapshots[idx];
        if (!snap) return;
        var scEl = scenes[idx];
        scEl.style.transition = 'none'; scEl.style.opacity = snap.opacity;
        scEl.style.display = snap.display !== '' ? snap.display : '';
        scEl.style.visibility = snap.visibility !== '' ? snap.visibility : '';
        scEl.style.transform = snap.transform !== '' ? snap.transform : '';
        for (var ci = 0; ci < snap.children.length; ci++) {
          var c = snap.children[ci];
          c.el.style.transition = 'none'; c.el.style.opacity = c.opacity;
          c.el.style.transform = c.transform; c.el.style.display = c.display;
          c.el.style.visibility = c.visibility;
        }
        requestAnimationFrame(function() {
          scEl.style.transition = '';
          for (var ci2 = 0; ci2 < snap.children.length; ci2++) snap.children[ci2].el.style.transition = '';
        });
      }

      function _fireAnim(idx) {
        if (_animFired[idx]) return;
        _animFired[idx] = true;
        var fn = window['animateScene' + idx];
        if (typeof fn === 'function') { try { fn(); } catch(e) {} return; }
        if (_aiShowScene) { try { _aiShowScene(idx); } catch(e) {} }
      }

      function showScene(idx) {
        if (idx < 0 || idx >= scenes.length) return;
        currentStep = idx;
        for (var j = 0; j < scenes.length; j++) {
          if (j === idx) {
            if (_animFired[j]) { delete _animFired[j]; _resetScene(j); }
            /* Force display:block FIRST so opacity transition has a visible target */
            scenes[j].style.display    = 'block';
            scenes[j].style.visibility = 'visible';
            scenes[j].style.pointerEvents = 'auto';
            /* Double rAF ensures display change is painted before opacity starts */
            (function(sceneEl) {
              requestAnimationFrame(function() {
                requestAnimationFrame(function() {
                  sceneEl.style.transition = 'opacity 0.35s ease';
                  sceneEl.style.opacity    = '1';
                });
              });
            })(scenes[j]);
          } else {
            scenes[j].style.transition    = 'opacity 0.2s ease';
            scenes[j].style.opacity       = '0';
            scenes[j].style.pointerEvents = 'none';
            /* Hide after fade completes so layout doesn't collapse mid-transition */
            (function(el) {
              setTimeout(function() {
                if (el.style.opacity === '0') {
                  el.style.display    = 'none';
                  el.style.visibility = 'hidden';
                }
              }, 230);
            })(scenes[j]);
          }
        }
        _updateDots(); _updateNavBtns();
        (function(capturedIdx) {
          requestAnimationFrame(function() { requestAnimationFrame(function() { _fireAnim(capturedIdx); }); });
        })(idx);
      }

      function _updateDots() {
        var dc = document.getElementById('dots');
        if (!dc) return;
        var ds = dc.querySelectorAll('.dot, circle');
        if (!ds.length) ds = dc.children;
        for (var k = 0; k < ds.length; k++) {
          var active = (k === currentStep);
          ds[k].style.opacity = active ? '1' : '0.35';
          if (ds[k].classList) ds[k].classList.toggle('active', active);
        }
      }

      function _updateNavBtns() {
        if (prevBtn) {
          if (currentStep === 0) { prevBtn.setAttribute('disabled','true'); prevBtn.style.opacity='0.3'; }
          else { prevBtn.removeAttribute('disabled'); prevBtn.style.opacity='1'; }
        }
        if (nextBtn) {
          if (currentStep === scenes.length - 1) { nextBtn.setAttribute('disabled','true'); nextBtn.style.opacity='0.3'; }
          else { nextBtn.removeAttribute('disabled'); nextBtn.style.opacity='1'; }
        }
      }

      var nb2 = nextBtn.cloneNode(true);
      nextBtn.parentNode.replaceChild(nb2, nextBtn);
      nextBtn = nb2;
      if (prevBtn) {
        var pb2 = prevBtn.cloneNode(true);
        prevBtn.parentNode.replaceChild(pb2, prevBtn);
        prevBtn = pb2;
      }

      nextBtn.addEventListener('click', function(e) { e.stopPropagation(); if (currentStep < scenes.length - 1) showScene(currentStep + 1); });
      if (prevBtn) prevBtn.addEventListener('click', function(e) { e.stopPropagation(); if (currentStep > 0) showScene(currentStep - 1); });

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
      console.log('[QAnim SC v10] ' + scenes.length + ' scenes ready');
    } catch(err) { console.error('[QAnim SC] Fatal:', err); }
  });
})();
</script>
"""


def inject_step_controller(html: str) -> str:
    """Injects the manual step controller. Must be called LAST."""
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
#  RESPONSE PARSING UTILITIES
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
            QAnimLogger.warn("Parser", f"Strategy {i+1} failed: {e}")

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
    if not m:
        return None
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
        if not m:
            return []
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
        return [_unescape_json_string(s) for s in items]

    code = _extract_animation_code_field(raw)
    if not code:
        return None

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
        if s[i] == '\\':
            i += 2
        elif s[i] == '"':
            return i
        else:
            i += 1
    return -1


def _unescape_json_string(s: str) -> str:
    return (s.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
             .replace('\\r', '\r').replace("\\'", "'").replace('\\\\', '\\'))


# ══════════════════════════════════════════════════════════════════════
#  MODULE 13 — Full Generation Pipeline  (v10.0 — Three-Stage Concurrent)
# ══════════════════════════════════════════════════════════════════════

async def _generate_concept_animation(question: str, category: str) -> str:
    """STAGE 1 — Pure concept animation (no solution, no answer). Light theme."""
    QAnimLogger.info("ConceptPipeline", f"START  category={category}")
    prompt = _build_concept_prompt(question, category)
    try:
        msg = client.messages.create(
            model=CONCEPT_MODEL,
            max_tokens=MAX_TOK_CONCEPT,
            system=SYSTEM_CONCEPT,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        QAnimLogger.info("ConceptAI", f"model={CONCEPT_MODEL}  stop_reason={msg.stop_reason}  len={len(raw)}")
        if msg.stop_reason == "max_tokens":
            QAnimLogger.warn("ConceptAI", "Hit max_tokens — may be truncated!")
    except Exception as e:
        QAnimLogger.error("ConceptAI", f"API call failed: {e}")
        return RecoveryEngine.fallback_html(question, f"Concept AI error: {e}")

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
        return RecoveryEngine.fallback_html(question, "Concept parse failed")

    try:
        GenerationValidator.validate(concept_html, require_svg=True)
    except ValidationError as e:
        QAnimLogger.warn("ConceptValidator", f"Validation failed: {e}")
        if '<svg' in concept_html and len(concept_html) > 200:
            concept_html = RecoveryEngine.partial_html(question, concept_html)
        else:
            return RecoveryEngine.fallback_html(question, str(e))

    concept_html = HtmlSanitizer.sanitize(concept_html)
    concept_html = inject_infrastructure(concept_html)
    concept_html = inject_notes_system(concept_html, question)
    concept_html = inject_step_controller(concept_html)

    QAnimLogger.ok("ConceptPipeline", f"DONE — len={len(concept_html):,}")
    return concept_html


async def generate_question_animation(question: str) -> dict:
    """
    THREE-STAGE CONCURRENT PIPELINE (v10.0):

    Stage 0 — ToFind Extraction   (sync, no AI)
    Stage 1 — Concept Animation   (claude-sonnet-4-5) + StepController
    Stage 2 — Solution Animation  (claude-sonnet-4-5) + all panels injected
    Stage 3 — Quiz Generation     (claude-haiku-4-5)  + 3 sets × 5 Qs

    Solution HTML receives full post-processing:
      Validate → Sanitize → inject_infrastructure
      → inject_solution_system → inject_to_find_system
      → inject_notes_system → inject_quiz_v2_panel
      → inject_answer_box_panel → inject_controls_bar
      → inject_step_controller (LAST)

    Returns result dict with three HTML outputs.
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("Question cannot be empty")

    short_q = question[:80] + ("..." if len(question) > 80 else "")
    QAnimLogger.info("Pipeline", f"START v10 — '{short_q}'")
    QAnimLogger.info("Pipeline", f"Concept/Solution: {SOLUTION_MODEL}  Quiz: {QUIZ_MODEL}")

    # ── Stage 0: ToFind (sync) ──────────────────────────────────────
    to_find_targets = ToFindExtractor.extract(question)
    QAnimLogger.info("Pipeline", f"ToFind: {to_find_targets}")

    # ── Classify ────────────────────────────────────────────────────
    category = _classify_topic(question)
    QAnimLogger.info("Classifier", f"Category: {category}")

    # ── Build solution prompt ────────────────────────────────────────
    solution_prompt = _build_prompt(question, category)

    # ── Stage 1-3: Three concurrent AI calls ─────────────────────────
    QAnimLogger.info("Pipeline", "Launching 3 concurrent AI stages…")

    async def _run_solution_ai() -> str:
        try:
            msg = client.messages.create(
                model=SOLUTION_MODEL,
                max_tokens=MAX_TOK,
                system=SYSTEM,
                messages=[{"role": "user", "content": solution_prompt}]
            )
            raw = msg.content[0].text.strip()
            QAnimLogger.info("SolutionAI", f"model={SOLUTION_MODEL}  stop_reason={msg.stop_reason}  len={len(raw)}")
            if msg.stop_reason == "max_tokens":
                QAnimLogger.warn("SolutionAI", "Hit max_tokens — may be truncated!")
            return raw
        except Exception as e:
            QAnimLogger.error("SolutionAI", f"API failed: {e}")
            raise

    try:
        concept_html, sol_raw, quiz_data = await asyncio.gather(
            _generate_concept_animation(question, category),
            _run_solution_ai(),
            QuizGeneratorV2.generate(question, category),
        )
    except Exception as e:
        QAnimLogger.error("Pipeline", f"Concurrent generation failed: {e}")
        return _build_failure_result(question, f"API error: {e}")

    # ── Parse solution ───────────────────────────────────────────────
    result = _parse_response(sol_raw, question)
    result["category"]               = category
    result["engine_version"]         = "v10.0"
    result["concept_animation_code"] = concept_html
    result["quiz_data"]              = quiz_data
    result["to_find"]                = to_find_targets
    result.setdefault("solution_steps", [])
    result.setdefault("final_answer",   "")
    result.setdefault("key_insight",    "")

    html = result.get("animation_code", "")

    # ── Validate solution HTML ───────────────────────────────────────
    try:
        GenerationValidator.validate(html, require_svg=True)
    except ValidationError as e:
        QAnimLogger.warn("Validator", f"Strict validation failed: {e}")
        if '<svg' in html and len(html) > 200:
            html = RecoveryEngine.partial_html(question, html)
            try:
                GenerationValidator.validate(html, require_svg=True)
                QAnimLogger.ok("Validator", "Partial recovery succeeded")
            except ValidationError as e2:
                result["animation_code"] = RecoveryEngine.fallback_html(question, str(e2))
                result["render_status"]  = "fallback"
                return result
        else:
            result["animation_code"] = RecoveryEngine.fallback_html(question, str(e))
            result["render_status"]  = "fallback"
            return result

    # ── Post-processing: inject all systems ──────────────────────────
    html = HtmlSanitizer.sanitize(html)
    html = inject_infrastructure(html)
    html = inject_solution_system(
        html    = html,
        steps   = result["solution_steps"],
        answer  = result["final_answer"],
        insight = result["key_insight"],
    )
    html = inject_to_find_system(html, to_find_targets)
    html = inject_notes_system(html, question)
    html = inject_quiz_v2_panel(html, quiz_data)
    html = inject_answer_box_panel(html)
    html = inject_controls_bar(html)          # ← unified controls bar (LAST before SC)
    html = inject_step_controller(html)       # ← MUST be absolute last

    # ── Build standalone quiz HTML ───────────────────────────────────
    quiz_html = QuizGeneratorV2.build_standalone_html(quiz_data, question, category)

    # ── Final validation ─────────────────────────────────────────────
    try:
        GenerationValidator.validate(html, require_svg=True)
    except ValidationError as e:
        QAnimLogger.warn("FinalValidator", f"Post-injection validation: {e} — continuing")

    result["animation_code"]         = html
    result["quiz_html"]              = quiz_html
    result["render_status"]          = "ok"

    QAnimLogger.ok("Pipeline", (
        f"DONE v10 — '{result['title']}' "
        f"concept={len(concept_html):,} "
        f"solution={len(html):,} "
        f"quiz_sets={len(quiz_data.get('sets',[]))} "
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
        "concept_animation_code": fallback,
        "quiz_html":              fallback,
        "quiz_data":              {"sets": []},
        "solution_steps":         [],
        "final_answer":           "",
        "key_insight":            "",
        "to_find":                [],
        "category":               "UNKNOWN",
        "engine_version":         "v10.0",
        "render_status":          "error",
    }


def generate_question_animation_sync(question: str) -> dict:
    """Synchronous wrapper for generate_question_animation."""
    return asyncio.run(generate_question_animation(question))


# ══════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPTS + PROMPT BUILDERS  (v10.0 — Light Theme)
# ══════════════════════════════════════════════════════════════════════

SYSTEM = """You are QAnim v10 — a cinematic SVG motion designer and educational animation engineer.

YOUR MISSION: Turn any student question into a PREMIUM, self-contained SVG animation.

═══════════════════════════════════════════════════════
CRITICAL: DO NOT REVEAL THE ANSWER IN THE ANIMATION
═══════════════════════════════════════════════════════
The animation is a CONCEPTUAL VISUALIZATION LAYER only.
- NEVER show the final numerical answer
- NEVER reveal complete solution steps
- ONLY teach the concept, show relationships, build intuition

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
VISUAL STANDARDS — v10.0 LIGHT THEME (REQUIRED)
═══════════════════════════════════════════════════════
BACKGROUND: MUST use LIGHT theme — #f8fafc (very light gray) or #ffffff
SVG background: light gradient — linear-gradient(135deg, #f1f5f9, #eff6ff) or white (#f8fafc)
DO NOT use dark backgrounds (#1e1b4b, #2d2a6e, etc.) — use LIGHT backgrounds only
TYPOGRAPHY: font-family: -apple-system, 'Segoe UI', Arial, sans-serif; color: #1e293b
CARDS: background: #ffffff; border: 1px solid #e2e8f0; border-radius: 14px;
       box-shadow: 0 4px 20px rgba(0,0,0,0.08)
SVG TEXT: fill: #1e293b (dark on light background)
ACCENT COLORS (vivid, on light bg):
  PHYSICS:  #3b5bdb (royal blue) + #e64980 (crimson pink)
  MATH:     #7c3aed (electric purple) + #db2777 (hot pink)
  BIO:      #16a34a (emerald) + #ca8a04 (amber)
  PROCESS:  #059669 (teal) + #0284c7 (sky blue)
  ABSTRACT: #d97706 (orange) + #7c3aed (purple)
  MIXED:    #0284c7 (blue) + #7c3aed (purple)
NAVIGATION BUTTONS: white/light bg, dark text, rounded, subtle border
SCENE BACKGROUNDS: white or very light gray (#f8fafc / #f1f5f9)

═══════════════════════════════════════════════════════
CRITICAL SAFETY RULES FOR animation_code
═══════════════════════════════════════════════════════
✅ Must be valid JSON string: escape " → \\", newline → \\n, backslash → \\\\
✅ Contains complete <!DOCTYPE html>...</html>
✅ Self-contained: NO external fonts, NO CDN links
✅ NO document.write()
✅ ABSOLUTELY NO backtick template literals — use only single/double quoted strings with + concatenation
   WRONG:  `Hello ${name}`
   RIGHT:  'Hello ' + name
✅ All SVG must have xmlns="http://www.w3.org/2000/svg"
✅ All <script> and <svg> tags must be balanced
✅ Solution panel DOM must be present: #sol-backdrop, #sol-panel, #sol-close,
   #sol-steps-container, #sol-answer-text, #sol-insight-text
✅ Solution panel must be EMPTY (steps injected by post-processor)
✅ Include: #prevbtn, #nextbtn, #dots for scene navigation
✅ Include: #qstrip .qtext for question display
✅ DO NOT include any buttons for "Find", "Quiz", "View Solution", or "Answer Box"
   — these are injected separately by the post-processor

═══════════════════════════════════════════════════════
ANIMATION TECHNIQUES
═══════════════════════════════════════════════════════
- stroke-dashoffset reveal for paths and arrows
- fade + translateY rise-in for labels (on light background, dark text)
- spring scale-in (cubic-bezier(0.34,1.56,0.64,1)) for hero elements
- Glow pulse: SVG filter + periodic opacity animation (subtle on light bg)
- Sequential JS setTimeout orchestration (NOT CSS animation-delay)
- Gradient fills on major shapes (pastels / vivid on white bg)
- Animated dashed borders for emphasis

SCENE STRUCTURE:
- 3-5 scenes per animation
- SVG viewBox="0 0 800 500"
- MANUAL STEP CONTROL:
  * NO auto-advance timers between scenes
  * window.currentStep, window.showScene, window.animateSceneN, window.nextStep, window.prevStep
  * #nextbtn / #prevbtn for navigation; #dots for progress indicators
  * Scene 0 animates on DOMContentLoaded via window.showScene(0)"""

SYSTEM_CONCEPT = """You are QAnim Concept Engine v10 — a cinematic SVG educational animator.

YOUR MISSION: Create a premium, self-contained concept animation that teaches
the CONCEPT visually. Do NOT show the answer. Light theme only.

OUTPUT FORMAT — STRICT:
{
  "animation_type": "concise type label",
  "design_strategy": "2-4 sentences",
  "concept_code": "COMPLETE SELF-CONTAINED HTML FILE AS A SINGLE JSON STRING"
}

VISUAL STANDARDS — v10.0 LIGHT THEME (REQUIRED):
BACKGROUND: #f8fafc or #ffffff — NEVER dark backgrounds
SVG background: light gradient or white
Text: color #1e293b on light backgrounds
Accent colors: vivid (purple #7c3aed, blue #3b5bdb, etc.) on light bg
Cards: white bg, 1px #e2e8f0 border, soft shadow

SAFETY RULES:
✅ Valid JSON string in concept_code
✅ Complete <!DOCTYPE html>...</html>
✅ Self-contained — no external resources
✅ NO document.write(), NO backtick template literals
✅ All SVG/script tags balanced
✅ NO solution panel DOM elements
✅ Include: #prevbtn, #nextbtn, #dots, #qstrip .qtext
✅ MANUAL STEP CONTROL — no auto-advance:
   window.showScene, window.animateSceneN, window.nextStep, window.prevStep
✅ 3-5 scenes of progressive concept revelation
✅ Light theme throughout"""

DESIGN_SYSTEM = """
TYPOGRAPHY: font-family: -apple-system, 'Segoe UI', Arial, sans-serif
SVG viewBox: "0 0 800 500"
BACKGROUNDS: #f8fafc (light), #ffffff (white), #f1f5f9 (very light gray)
  DO NOT use dark backgrounds — light theme required
COLOR PALETTES (vivid on light bg):
  PHYSICS=#3b5bdb/#e64980 | MATH=#7c3aed/#db2777
  BIOLOGY=#16a34a/#ca8a04 | PROCESS=#059669/#0284c7
  ABSTRACT=#d97706/#7c3aed | MIXED=#0284c7/#7c3aed
CARDS: background:#ffffff; border:1px solid #e2e8f0; border-radius:14px; box-shadow:0 4px 16px rgba(0,0,0,0.07)
SVG TEXT: fill:#1e293b or fill:#334155 (dark on light)
"""

SVG_TECHNIQUES = """
KEY TECHNIQUES:
- stroke-dashoffset path reveal on arrows/curves
- fade+translateY rise for labels (opacity 0→1, translateY 16px→0)
- spring scale-in: cubic-bezier(0.34,1.56,0.64,1)
- Subtle glow: SVG filter feGaussianBlur (not too intense on light bg)
- Sequential JS setTimeout (NOT CSS animation-delay attributes)
- Gradient fills: linearGradient/radialGradient — use pastels or vivid on white
- Animated dashed borders: stroke-dasharray + stroke-dashoffset
"""

STRATEGY_TEMPLATES = {
    "VISUAL_PHYSICS": (
        "Dynamic force/motion diagram on light background: draw physical setup with "
        "gradient shapes on white SVG, animate force vectors with colored arrowheads, "
        "show trajectory arc with stroke-dashoffset reveal, reveal formula symbols progressively."
    ),
    "PROCESS_BASED": (
        "Sequential process nodes on light background connected by animated paths. "
        "Each node reveals with spring scale-in, highlights its mechanism, "
        "then dims as next activates. Progress bar draws across top."
    ),
    "MATHEMATICAL": (
        "Coordinate axes draw in on white background, function curve traces left-to-right "
        "with vivid color trail. Shaded region pulses with opacity. Formula symbols "
        "materialize one token at a time with staggered fade-rise."
    ),
    "BIOLOGICAL": (
        "Organic cell/molecule shapes with gradient fills on light background. Animated "
        "process arrows trace the biological pathway. Color-coded structures appear "
        "sequentially with labels rising in."
    ),
    "ABSTRACT": (
        "Clean visual metaphor on white: scales for balance, Venn circles for overlap, "
        "network graph for relationships. Concept dimensions animate as visual zones."
    ),
    "MIXED": (
        "Split-zone canvas on light gray background. Left zone: physical/visual system. "
        "Right zone: formula/data visualization. Center connector pulses data between zones."
    ),
}

CONCEPT_STRATEGY_TEMPLATES = {
    "VISUAL_PHYSICS": (
        "Force diagram on light background: animate setup with gradient shapes, "
        "draw colored force vectors, show trajectory arc via stroke-dashoffset. "
        "End on key variable highlight — do NOT solve it."
    ),
    "PROCESS_BASED": (
        "Sequential node graph on white: each stage spring-scales in, highlights "
        "mechanism, dims as next activates. Traveling dot shows flow direction."
    ),
    "MATHEMATICAL": (
        "Axes draw on white, function curve traces with vivid color trail, "
        "shaded region pulses. Formula tokens materialize one at a time."
    ),
    "BIOLOGICAL": (
        "Organic shapes on light background with gradient fills. Process arrows "
        "trace pathway via stroke-dashoffset. Sequential color-coded reveals."
    ),
    "ABSTRACT": (
        "Physical analogy on white: scales, spectrum bars, Venn circles. "
        "Concept zones animate separately with bridge connectors."
    ),
    "MIXED": (
        "Split canvas on light bg: physical animation left, formula/data right. "
        "Central bridge connects zones."
    ),
}

FALLBACK_RULES = """
IF STUCK: Use one of these premium fallback layouts (all light theme):
1. CARD-REVEAL: 3-4 white cards with colored top border, fade+rise staggered
2. TIMELINE: Horizontal line draws, events spring-scale in at nodes
3. CONCEPT-MAP: Central node, branch lines draw, satellite nodes appear
4. DATA-BARS: Animated bar chart on white bg with gradient fills
NEVER: flat dark backgrounds, neon on black, static text.
"""

HTML_SHELL_NOTE = """
REQUIRED HTML STRUCTURE (solution panel DOM must exist but be EMPTY):
- #sol-backdrop, #sol-panel, #sol-close
- #sol-steps-container (EMPTY), #sol-answer-text (EMPTY), #sol-insight-text (EMPTY)
- #sol-answer-card, #sol-insight-card
- .sol-step class, .formula class
- All scenes in <g id="scene-N"> groups
- Navigation: #prevbtn, #nextbtn, #dots
- Question: #qstrip .qtext

DO NOT include any "Find", "Quiz", "View Solution", or "Answer Box" buttons.
These are injected by the post-processor.

DO NOT include document.write() or external script src= tags.
"""


def _classify_topic(question: str) -> str:
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
    try:
        resp = client.messages.create(
            model=Q_MODEL, max_tokens=30,
            system="Reply with ONLY one of: VISUAL_PHYSICS, PROCESS_BASED, MATHEMATICAL, BIOLOGICAL, ABSTRACT, MIXED",
            messages=[{"role": "user", "content": f"Classify: {question[:200]}"}]
        )
        cat = resp.content[0].text.strip().upper()
        if cat in STRATEGY_TEMPLATES:
            return cat
    except Exception:
        pass
    return "PROCESS_BASED"


def _build_concept_prompt(question: str, category: str) -> str:
    strategy = CONCEPT_STRATEGY_TEMPLATES.get(category, CONCEPT_STRATEGY_TEMPLATES["PROCESS_BASED"])
    return f"""Build a CINEMATIC CONCEPT ANIMATION for QAnim v10 Stage 1.

QUESTION: {question}
CATEGORY: {category}
VISUAL STRATEGY: {strategy}

{DESIGN_SYSTEM}
{SVG_TECHNIQUES}
{FALLBACK_RULES}

CONCEPT ANIMATION REQUIREMENTS:
- LIGHT THEME: white/light-gray background (#f8fafc or #ffffff) — NO dark backgrounds
- Cards: white bg, colored border-top/border-left accent
- Vivid accent colors on light background
- 3-5 scenes of progressive conceptual revelation
- NO solution, NO final numeric answer
- DO NOT include any "Find", "Quiz", "View Solution", or "Answer Box" buttons
- Navigation: #prevbtn, #nextbtn, #dots
- All animations visible on light background

IMPORTANT: Return ONLY the raw JSON object. No markdown. No extra text.
The concept_code field must be a complete <!DOCTYPE html>...</html>
as a properly escaped JSON string."""


def _build_prompt(question: str, category: str) -> str:
    strategy = STRATEGY_TEMPLATES.get(category, STRATEGY_TEMPLATES["PROCESS_BASED"])
    return f"""Build a PREMIUM CINEMATIC SVG animation for QAnim v10.

QUESTION: {question}
CATEGORY: {category}
STRATEGY: {strategy}

{DESIGN_SYSTEM}
{SVG_TECHNIQUES}
{FALLBACK_RULES}
{HTML_SHELL_NOTE}

KEY REMINDERS:
- LIGHT THEME REQUIRED: white/light-gray backgrounds, dark text, vivid accents
- NEVER show the final answer in the animation body
- DO NOT include any "Find", "Quiz", "View Solution", or "Answer Box" buttons
- Include solution panel DOM shell (EMPTY containers)

IMPORTANT: Return ONLY the raw JSON object. No markdown. No extra text.
The animation_code must be a complete <!DOCTYPE html>...</html>
as a properly escaped JSON string."""


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
        print(f"  QAnim v10.0 — Light Theme | 3×5 Quiz | Answer Box | {cat}")
        print(f"  Q: {q[:65]}...")
        print("=" * 72)

        print("\n[ToFind Smoke Test]")
        targets = ToFindExtractor.extract(q)
        print(f"  Targets: {targets}")

        result = generate_question_animation_sync(q)

        concept_html  = result.get("concept_animation_code", "")
        solution_html = result.get("animation_code", "")
        quiz_html     = result.get("quiz_html", "")
        quiz_data     = result.get("quiz_data", {})

        print(f"\nTitle               : {result['title']}")
        print(f"Category            : {result.get('category','N/A')}")
        print(f"Engine              : {result.get('engine_version','N/A')}")
        print(f"Render Status       : {result.get('render_status','N/A')}")
        print(f"[ToFind] Targets    : {result.get('to_find',[])}")
        print(f"[Stage 1] Concept   : {len(concept_html):,} chars")
        print(f"[Stage 2] Solution  : {len(solution_html):,} chars")
        print(f"[Stage 3] Quiz HTML : {len(quiz_html):,} chars")
        sets = quiz_data.get('sets', [])
        print(f"[Stage 3] Quiz Sets : {len(sets)} sets × {len(sets[0].get('questions',[])) if sets else 0} questions each")

        steps = result.get('solution_steps', [])
        print(f"Solution Steps      : {len(steps)}")
        for i, s in enumerate(steps, 1):
            print(f"  Step {i}: {s[:90]}...")
        print(f"Final Answer        : {result.get('final_answer','')[:120]}")
        print(f"Key Insight         : {result.get('key_insight','')[:100]}")

        slug = cat.lower()

        concept_out  = f"q_anim_v100_{slug}_concept.html"
        solution_out = f"q_anim_v100_{slug}_solution.html"
        quiz_out     = f"q_anim_v100_{slug}_quiz.html"

        with open(concept_out,  "w", encoding="utf-8") as f: f.write(concept_html)
        with open(solution_out, "w", encoding="utf-8") as f: f.write(solution_html)
        with open(quiz_out,     "w", encoding="utf-8") as f: f.write(quiz_html)

        print(f"\n[Stage 1] Concept saved  : {concept_out}")
        print(f"[Stage 2] Solution saved : {solution_out}")
        print(f"[Stage 3] Quiz saved     : {quiz_out}")
        print()
        print("Controls injected into solution HTML:")
        print("  [🔍 Find]  [📝 Quiz]  [💡 View Solution]  [✏️ Answer Box]")
        print()
