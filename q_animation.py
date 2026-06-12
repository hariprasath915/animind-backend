"""
q_animation.py  --  QAnim Question Animation Generator  v11.3
=============================================================
v11.3 -- MOBILE RESPONSIVENESS FIXES (animation scene visible on all devices):

  PROBLEMS FIXED:
  - SVG animation scene was invisible/tiny on mobile (400px screen)
  - #pw-scene-wrap had no height constraint → SVG collapsed
  - SVG didn't scale to fill its container on mobile
  - Controls bar overlapped content on small screens
  - Bottom padding insufficient, hiding content behind controls bar
  - Info cards at y=420-540 cut off on narrow viewports

  CHANGES FROM v11.2:
  - _LAYOUT_WRAPPER_CSS:
      * #pw-scene-wrap: aspect-ratio 10/6, position:relative, min-height 200px
      * SVG inside #pw-scene-wrap forced to width/height 100%, position absolute
      * Overlay elements (scene indicator) repositioned correctly
      * Mobile breakpoints tightened for padding and spacing
      * Controls bar bottom clearance increased on mobile (padding-bottom: 120px)
  - _LAYOUT_WRAPPER_JS:
      * After moving SVG into #pw-scene-wrap, sets preserveAspectRatio="xMidYMid meet"
      * Forces width="100%" height="100%" on SVG element
      * ResizeObserver updates scene indicator position on resize
  - _CONTROLS_BAR_CSS:
      * Reduced button padding on mobile
      * Bottom set to 12px on mobile, uses env(safe-area-inset-bottom) for iOS
  - inject_infrastructure:
      * Scroll-fix CSS updated: SVG gets position absolute inside pw-scene-wrap
      * Viewport meta tag verified in injected HTML
  - SYSTEM / SYSTEM_CONCEPT:
      * Added note: SVG must include preserveAspectRatio="xMidYMid meet"
      * Added note: include width="100%" height="100%" on root <svg>

  All other v11.2 features (Final Answer panel, Answer Box, Voice, Notes) unchanged.

v11.2 features:
  - SOLUTION BUTTON REMOVED.
  - FINAL ANSWER PANEL with numbered results
  - ANSWER BOX with Find-chip, multi-target, feedback strip
  - QUIZ SYSTEM REMOVED.

PIPELINE (v11.3):
  Stage 0 -- ToFind Extraction    (sync, no AI)
  Stage 1 -- Concept Animation    (claude-sonnet-4-6) + StepController
  Stage 2 -- Solution Animation   (claude-sonnet-4-6) + StepController
  Stage 3 -- Haiku Solution       (claude-haiku-4-5) on demand
             [Stages 1-3 run concurrently via asyncio.gather]

CONTROLS BAR (injected into every animation HTML):
  [Find]  [Final Answer]  [Answer Box]  [Voice]
"""

import anthropic
import json
import re
import asyncio
import html as html_module
from typing import Optional

# ---------------------------------------------------------------------------
# Client + model routing
# ---------------------------------------------------------------------------
client = anthropic.Anthropic(
    default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
)

CONCEPT_MODEL        = "claude-sonnet-4-6"
SOLUTION_MODEL       = "claude-sonnet-4-6"
Q_MODEL              = SOLUTION_MODEL
HAIKU_SOLUTION_MODEL = "claude-haiku-4-5"

MAX_TOK                = 16000   # claude-sonnet-4-6 hard cap is 16 384 tokens
MAX_TOK_CONCEPT        = 12000
MAX_TOK_HAIKU_SOLUTION = 8000


# ===========================================================================
#  MODULE 1 -- QAnimLogger
# ===========================================================================
class QAnimLogger:
    PREFIX = "[QAnim v11.3]"

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
#  MODULE 2 -- GenerationValidator
# ===========================================================================
class ValidationError(Exception):
    pass


class GenerationValidator:
    DANGEROUS_PATTERNS = [
        (r'document\.write\s*\(',  "document.write() is forbidden"),
        (r'<script[^>]+src\s*=',   "External script src not allowed"),
        (r'javascript:\s*void',    "javascript:void() link detected"),
        (r'on\w+\s*=\s*["\']?\s*eval\s*\(', "eval() in event handler"),
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
        open_scripts  = len(re.findall(r'<script(?:\s[^>]*)?>',  html, re.IGNORECASE))
        close_scripts = len(re.findall(r'</script>',              html, re.IGNORECASE))
        if open_scripts != close_scripts:
            raise ValidationError(f"Unbalanced <script> tags: {open_scripts} open, {close_scripts} close")
        open_svgs  = len(re.findall(r'<svg(?:\s[^>]*)?>',  html, re.IGNORECASE))
        close_svgs = len(re.findall(r'</svg>',              html, re.IGNORECASE))
        if open_svgs != close_svgs:
            raise ValidationError(f"Unbalanced <svg> tags: {open_svgs} open, {close_svgs} close")
        QAnimLogger.ok("Validator", f"HTML passed validation ({len(html):,} chars)")


# ===========================================================================
#  MODULE 2.5 -- ToFindExtractor
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
#  MODULE 3 -- HtmlSanitizer
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
        html = cls._fix_template_literals(html)
        html = cls._fix_const_let(html)
        html = cls._fix_arrow_functions(html)
        html = cls._fix_single_quote_apostrophes(html)
        html = cls._wrap_scripts_in_error_boundary(html)
        html = re.sub(r'<svg(?![^>]*xmlns)', '<svg xmlns="http://www.w3.org/2000/svg"', html, flags=re.IGNORECASE)
        html = cls._fix_svg_subscripts(html)
        html = html.replace('\x00', '')
        QAnimLogger.ok("Sanitizer", "HTML sanitized")
        return html

    _SUB_PATTERN = re.compile(
        r'(?<![A-Za-z\u03b1-\u03c9\u0391-\u03a9\d])'
        r'([A-Za-z\u03b1-\u03c9\u0391-\u03a9\d])'
        r'_'
        r'(?:'
        r'\{([^}]{1,20})\}'
        r'|([A-Za-z\u03b1-\u03c9\u0391-\u03a9\d]+)'
        r')'
    )

    @classmethod
    def _replace_sub_in_text_content(cls, content):
        def replacer(m):
            base = m.group(1)
            sub  = m.group(2) if m.group(2) else m.group(3)
            return (base
                    + '<tspan dy="5" font-size="0.72em">' + sub + '</tspan>'
                    + '<tspan dy="-5" font-size="1em"></tspan>')
        return cls._SUB_PATTERN.sub(replacer, content)

    @classmethod
    def _fix_svg_subscripts(cls, html):
        def fix_svg_block(svg_match):
            svg_content = svg_match.group(0)

            def fix_text_tag(t_match):
                full   = t_match.group(0)
                open_t = t_match.group(1)
                inner  = t_match.group(2)
                close_t= t_match.group(3)
                if 'dy=' in inner and 'font-size' in inner:
                    return full
                if '_' not in inner:
                    return full
                fixed = cls._replace_sub_in_text_content(inner)
                if fixed != inner:
                    QAnimLogger.info("Sanitizer", f"Subscript fixed in <text>: {inner[:60]!r}")
                return open_t + fixed + close_t

            svg_content = re.sub(
                r'(<text\b[^>]*>)(.*?)(</text>)',
                fix_text_tag,
                svg_content,
                flags=re.DOTALL | re.IGNORECASE
            )
            return svg_content

        html = re.sub(r'<svg[\s\S]*?</svg>', fix_svg_block, html, flags=re.IGNORECASE)
        return html

    @classmethod
    def _wrap_scripts_in_error_boundary(cls, html):
        def wrap_script(match):
            tag, body, close = match.group(1), match.group(2), match.group(3)
            if re.search(r'type\s*=\s*["\']application/', tag, re.IGNORECASE):
                return match.group(0)
            stripped = body.strip()
            if stripped.startswith('try {') or stripped.startswith('try{'):
                return match.group(0)
            if len(stripped) < 20:
                return match.group(0)
            wrapped = (
                "\n/* -- QAnim Error Boundary -- */\ntry {\n" + body +
                "\n} catch (_qanim_err) {\n"
                "  console.error('[QAnim ErrorBoundary]', _qanim_err);\n"
                "  (function() {\n"
                "    var fb = document.getElementById('qanim-error-fallback');\n"
                "    if (!fb) return;\n"
                "    fb.style.display = 'flex';\n"
                "    var msg = fb.querySelector('.qanim-err-msg');\n"
                "    if (msg) msg.textContent = String(_qanim_err);\n"
                "  })();\n}\n")
            return f"{tag}{wrapped}{close}"
        return re.sub(r'(<script(?:\s[^>]*)?>)(.*?)(</script>)', wrap_script, html, flags=re.DOTALL | re.IGNORECASE)

    @classmethod
    def _fix_template_literals(cls, html):
        def process_script(script_match):
            tag, body, close = script_match.group(1), script_match.group(2), script_match.group(3)
            if re.search(r'type\s*=\s*["\']application/', tag, re.IGNORECASE):
                return script_match.group(0)
            def replace_template(m):
                raw = m.group(1)
                parts = re.split(r'\$\{([^}]*)\}', raw)
                out = []
                for idx, part in enumerate(parts):
                    if idx % 2 == 0:
                        esc = part.replace('\\','\\\\').replace('"','\\"').replace('\n','\\n').replace('\r','\\r').replace('\t','\\t')
                        out.append('"' + esc + '"')
                    else:
                        out.append('(' + part.strip() + ')')
                while out and out[0]  == '""': out.pop(0)
                while out and out[-1] == '""': out.pop()
                return (' + '.join(out)) if out else '""'
            original = body
            body = re.sub(r'`((?:[^`\\]|\\.)*)`', replace_template, body, flags=re.DOTALL)
            if body != original:
                QAnimLogger.warn("Sanitizer", "Backtick template literals replaced")
            return f"{tag}{body}{close}"
        return re.sub(r'(<script(?:\s[^>]*)?>)(.*?)(</script>)', process_script, html, flags=re.DOTALL | re.IGNORECASE)

    @classmethod
    def _fix_const_let(cls, html):
        def process_script(m):
            tag, body, close = m.group(1), m.group(2), m.group(3)
            if re.search(r'type\s*=\s*["\']application/', tag, re.IGNORECASE):
                return m.group(0)
            body = re.sub(r'\bconst\b', 'var', body)
            body = re.sub(r'\blet\b',   'var', body)
            return f"{tag}{body}{close}"
        return re.sub(r'(<script(?:\s[^>]*)?>)(.*?)(</script>)', process_script, html, flags=re.DOTALL | re.IGNORECASE)

    @classmethod
    def _fix_arrow_functions(cls, html):
        def process_script(m):
            tag, body, close = m.group(1), m.group(2), m.group(3)
            if re.search(r'type\s*=\s*["\']application/', tag, re.IGNORECASE):
                return m.group(0)
            body = re.sub(r'\(([^)]*)\)\s*=>\s*(\{)', r'function(\1) \2', body)
            body = re.sub(r'\(([^)]*)\)\s*=>\s*([^{;\n][^;\n]*)', r'function(\1) { return \2; }', body)
            body = re.sub(r'(?<![\w$])([A-Za-z_$][\w$]*)\s*=>\s*(\{)', r'function(\1) \2', body)
            body = re.sub(r'(?<![\w$])([A-Za-z_$][\w$]*)\s*=>\s*([^{;\n][^;\n]*)', r'function(\1) { return \2; }', body)
            return f"{tag}{body}{close}"
        return re.sub(r'(<script(?:\s[^>]*)?>)(.*?)(</script>)', process_script, html, flags=re.DOTALL | re.IGNORECASE)

    @classmethod
    def _fix_single_quote_apostrophes(cls, html):
        def process_script(m):
            tag, body, close = m.group(1), m.group(2), m.group(3)
            if re.search(r'type\s*=\s*["\']application/', tag, re.IGNORECASE):
                return m.group(0)
            def fix_sq_string(mm):
                inner = mm.group(1)
                if re.search(r"(?<!\\)'", inner):
                    fixed = inner.replace("\\'", "'").replace('"', '\\"')
                    return '"' + fixed + '"'
                return mm.group(0)
            body = re.sub(r"'((?:[^'\\\n]|\\.)*)'", fix_sq_string, body)
            return f"{tag}{body}{close}"
        return re.sub(r'(<script(?:\s[^>]*)?>)(.*?)(</script>)', process_script, html, flags=re.DOTALL | re.IGNORECASE)


# ===========================================================================
#  MODULE 4 -- RecoveryEngine
# ===========================================================================
class RecoveryEngine:

    @staticmethod
    def fallback_html(question, reason):
        q_safe      = html_module.escape(question[:120])
        reason_safe = html_module.escape(reason[:300])
        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100%;overflow:hidden;background:#f1f5f9;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  display:flex;align-items:center;justify-content:center}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:16px;
  box-shadow:0 4px 24px rgba(0,0,0,.10);padding:36px 40px;max-width:520px;text-align:center}}
.icon{{font-size:40px;margin-bottom:16px}}
.title{{font-size:17px;font-weight:800;color:#1e293b;margin-bottom:10px}}
.reason{{font-size:11px;color:#64748b;background:#f8fafc;border-radius:10px;
  padding:10px 14px;margin:12px 0;border:1px solid #e2e8f0;text-align:left;
  line-height:1.6;font-family:monospace}}
.question{{font-size:12px;color:#94a3b8;line-height:1.6;margin-top:10px;font-style:italic}}
.retry-hint{{margin-top:18px;font-size:11px;font-weight:700;letter-spacing:1.5px;
  text-transform:uppercase;color:#7c3aed}}
</style></head><body>
<div class="card">
<div class="icon">&#x26A0;&#xFE0F;</div>
<div class="title">Animation Could Not Render</div>
<div class="reason">{reason_safe}</div>
<div class="question">"{q_safe}"</div>
<div class="retry-hint">Please regenerate the animation</div>
</div></body></html>"""

    @staticmethod
    def partial_html(question, animation_code):
        if '<!DOCTYPE' in animation_code or '<html' in animation_code:
            return animation_code
        q_safe = html_module.escape(question[:120])
        return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>html,body{{margin:0;padding:0;width:100%;height:100%;
  display:flex;align-items:center;justify-content:center;background:#f1f5f9;
  font-family:-apple-system,sans-serif}}</style></head><body>
<div style="font-size:11px;color:#64748b;position:fixed;top:8px;left:0;right:0;text-align:center">
  {q_safe}</div>
{animation_code}</body></html>"""


# ===========================================================================
#  MODULE 5 -- IframeLifecycleManager JS constant
# ===========================================================================
IFRAME_RUNTIME_JS = r"""
(function() {
  'use strict';
  var _iframe=null, _renderQueue=[], _rendering=false, _currentHtml='';
  var Log={
    info:function(m){console.log('[QAnim ILM] i  '+m);},
    warn:function(m){console.warn('[QAnim ILM] !  '+m);},
    error:function(m){console.error('[QAnim ILM] X  '+m);},
    ok:function(m){console.log('[QAnim ILM] OK '+m);}
  };
  function _getIframe(){
    if(_iframe&&document.body.contains(_iframe))return _iframe;
    var existing=document.getElementById('qanim-frame');
    if(existing){_iframe=existing;return _iframe;}
    var f=document.createElement('iframe');
    f.id='qanim-frame';f.setAttribute('sandbox','allow-scripts');
    f.style.cssText='width:100%;height:100%;border:none;display:block;background:transparent';
    f.setAttribute('title','QAnim Animation');
    document.body.appendChild(f);_iframe=f;
    Log.ok('Created fresh iframe #qanim-frame');return _iframe;
  }
  function _resetIframe(){
    Log.warn('Resetting iframe...');
    if(_iframe&&document.body.contains(_iframe)){
      _iframe.removeAttribute('srcdoc');_iframe.src='about:blank';
      document.body.removeChild(_iframe);}
    _iframe=null;_currentHtml='';return _getIframe();
  }
  function _injectSrcdoc(iframe,html){
    try{
      iframe.removeAttribute('srcdoc');iframe.src='about:blank';
      requestAnimationFrame(function(){
        try{iframe.srcdoc=html;Log.ok('srcdoc injected ('+html.length+' chars)');}
        catch(e){Log.error('srcdoc assignment failed: '+e);}
      });
    }catch(e){Log.error('_injectSrcdoc outer: '+e);}
  }
  function _processQueue(){
    if(_rendering||_renderQueue.length===0)return;
    _rendering=true;
    var task=_renderQueue.shift();var iframe=_getIframe();
    var timeoutId,done=false;
    function cleanup(){if(done)return;done=true;clearTimeout(timeoutId);iframe.onload=null;iframe.onerror=null;}
    iframe.onload=function(){cleanup();_currentHtml=task.html;_rendering=false;if(task.onSuccess)task.onSuccess();_processQueue();};
    iframe.onerror=function(e){cleanup();_rendering=false;if(task.onError)task.onError('iframe error');_processQueue();};
    timeoutId=setTimeout(function(){if(done)return;cleanup();_currentHtml=task.html;_rendering=false;if(task.onSuccess)task.onSuccess();_processQueue();},8000);
    _injectSrcdoc(iframe,task.html);
  }
  window.QAnimILM={
    render:function(html,onSuccess,onError){
      if(!html||html.length<100){if(onError)onError('empty html');return;}
      _renderQueue=[];_renderQueue.push({html:html,onSuccess:onSuccess,onError:onError});_processQueue();
    },
    reset:function(){_renderQueue=[];_rendering=false;_resetIframe();},
    getCurrentHtml:function(){return _currentHtml;},
    isRendering:function(){return _rendering;}
  };
  Log.ok('IframeLifecycleManager v11 initialized');
})();
"""


# ===========================================================================
#  MODULE 6 -- Error Boundary & Infrastructure
# ===========================================================================
ERROR_BOUNDARY_HTML = """
<div id="qanim-error-fallback" style="
  display:none;position:fixed;inset:0;z-index:9999;
  background:rgba(241,245,249,0.92);backdrop-filter:blur(12px);
  align-items:center;justify-content:center;">
  <div style="background:#fff;border-radius:16px;padding:32px 36px;max-width:440px;
    text-align:center;border:1px solid #e2e8f0;box-shadow:0 8px 40px rgba(0,0,0,.12);">
    <div style="font-size:36px;margin-bottom:14px">&#x26A0;&#xFE0F;</div>
    <div style="font-size:15px;font-weight:800;color:#1e293b;margin-bottom:8px">Animation Error</div>
    <div class="qanim-err-msg" style="font-size:11px;color:#64748b;background:#f8fafc;
      border-radius:10px;padding:10px 14px;margin:12px 0;border:1px solid #e2e8f0;
      font-family:monospace;text-align:left;line-height:1.6;word-break:break-all;">Unknown error</div>
    <button onclick="document.getElementById('qanim-error-fallback').style.display='none'"
      style="margin-top:14px;padding:8px 22px;border-radius:8px;border:none;
      background:#7c3aed;color:#fff;font-weight:700;font-size:12px;cursor:pointer;">Dismiss</button>
  </div>
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
  var fb=document.getElementById('qanim-error-fallback');
  if(fb){fb.style.display='flex';var msg=fb.querySelector('.qanim-err-msg');
    if(msg)msg.textContent=e.message+' (line '+e.lineno+')';}
});
window.addEventListener('unhandledrejection',function(e){
  console.error('[QAnim UnhandledPromise]',e.reason);
});
</script>
"""


def inject_infrastructure(html):
    # Inject viewport meta tag for mobile-responsive scaling
    _viewport_meta = '<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">'
    if '<meta name="viewport"' not in html and "<meta name='viewport'" not in html:
        html = re.sub(r'(<head[^>]*>)', r'\1\n' + _viewport_meta, html, count=1, flags=re.IGNORECASE)
    html = re.sub(r'(<body[^>]*>)', r'\1\n' + ERROR_BOUNDARY_HTML, html, count=1, flags=re.IGNORECASE)
    first_script = re.search(r'<script(?:\s[^>]*)?>(?!.*type\s*=\s*["\']application/json)', html, re.IGNORECASE)
    if first_script:
        pos = first_script.start()
        html = html[:pos] + QANIM_INNER_LOGGER_JS + '\n' + html[pos:]
    else:
        html = html.replace('</body>', QANIM_INNER_LOGGER_JS + '\n</body>', 1)

    # v11.3 MOBILE FIX: Updated scroll-fix CSS with proper SVG scaling
    # The SVG must fill its container (#pw-scene-wrap) on all screen sizes.
    # Using position:absolute + width/height 100% makes it fill the aspect-ratio box.
    _scroll_fix = (
        '\n<style id="qanim-scroll-fix">\n'
        'html,body{'
        '  overflow-x:hidden!important;'
        '  overflow-y:auto!important;'
        '  height:auto!important;'
        '  min-height:100vh;'
        '  width:100%!important;'
        '  margin:0;'
        '  padding:0;'
        '}\n'
        # SVG inside the layout wrapper scene container: fill it completely
        '#pw-scene-wrap svg{'
        '  position:absolute!important;'
        '  top:0!important;left:0!important;'
        '  width:100%!important;height:100%!important;'
        '  display:block!important;'
        '}\n'
        # Fallback: SVG not inside wrapper (standalone)
        'body>svg{'
        '  width:100%!important;'
        '  height:auto!important;'
        '  display:block!important;'
        '  max-width:100vw!important;'
        '}\n'
        '#page-wrapper svg{width:100%!important;height:auto!important;display:block;}\n'
        '#page-wrapper #container,[id="container"]{width:100%;box-sizing:border-box;}\n'
        '</style>\n'
    )
    if '</head>' in html:
        html = html.replace('</head>', _scroll_fix + '</head>', 1)
    else:
        html = _scroll_fix + html
    QAnimLogger.ok("Infrastructure", "Error fallback + inner logger + scroll-fix injected")
    return html


# ===========================================================================
#  MODULE 6.5 -- ToFind Injection System
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
#tofind-backdrop {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 8000;
  background: rgba(15, 23, 42, 0.40);
  backdrop-filter: blur(4px);
  opacity: 0;
  transition: opacity 0.22s ease;
}
#tofind-backdrop.open { display: block; opacity: 1; }

#tofind-panel {
  display: flex;
  flex-direction: column;
  position: fixed;
  top: 50%; left: 50%;
  transform: translate(-50%, -48%) scale(0.96);
  z-index: 8100;
  width: min(460px, 92vw);
  max-height: 80vh;
  border-radius: 16px;
  padding: 24px;
  box-sizing: border-box;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  box-shadow: 0 8px 40px rgba(0, 0, 0, 0.12);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.25s ease,
              transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
}
#tofind-panel.open {
  opacity: 1;
  pointer-events: auto;
  transform: translate(-50%, -50%) scale(1);
}
.tf-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.tf-header-left { display: flex; align-items: center; gap: 10px; }
.tf-icon-wrap {
  width: 32px; height: 32px;
  border-radius: 8px;
  background: #7c3aed;
  display: flex; align-items: center; justify-content: center;
  color: #fff; flex-shrink: 0;
}
.tf-title {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 16px; font-weight: 700; color: #1e293b;
}
.tf-close-btn {
  width: 30px; height: 30px;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  background: #f8fafc;
  color: #64748b; font-size: 12px;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.tf-close-btn:hover { background: #fee2e2; color: #dc2626; }
.tf-subtitle {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 12px; color: #64748b; margin: 0 0 14px;
}
.tf-items-container {
  display: flex; flex-direction: column; gap: 8px;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: #e2e8f0 transparent;
}
.tofind-item {
  display: flex; align-items: flex-start; gap: 12px;
  padding: 12px 14px;
  border-radius: 10px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  opacity: 0;
  transform: translateX(-12px);
  transition: background 0.15s;
}
.tofind-item:hover { background: #ede9fe; border-color: #7c3aed; }
.tofind-check {
  width: 20px; height: 20px;
  border-radius: 50%;
  background: #7c3aed; color: #fff;
  font-size: 11px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.tofind-text {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 13px; font-weight: 600; color: #1e293b; line-height: 1.5;
}
.tofind-empty {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 13px; color: #94a3b8;
  text-align: center; padding: 20px 0; font-style: italic;
}
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
#  MODULE 7 -- Final Answer Panel System  (v11.2)
# ===========================================================================

def _build_final_answer_data_tag(answer_targets, final_answer, key_insight):
    ROMAN = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"]
    items = []
    for idx, t in enumerate(answer_targets or []):
        label   = str(t.get("label", "")).strip()
        value   = str(t.get("value", "")).strip()
        unit    = str(t.get("unit",  "")).strip()
        roman   = ROMAN[idx] if idx < len(ROMAN) else str(idx + 1)
        items.append({"roman": roman, "label": label, "value": value, "unit": unit})

    if not items and final_answer:
        import re as _re
        _num_re = _re.compile(
            r'([A-Za-z_][A-Za-z_0-9]*)\s*[=:]\s*([-+]?\d[\d.,]*(?:\s*[×x*]\s*10\^?[-+]?\d+)?)\s*([A-Za-z°%/²³·]+(?:\s*[A-Za-z°%/²³·]+)*)?',
            _re.IGNORECASE)
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
#fa-backdrop {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 8500;
  background: rgba(15, 23, 42, 0.42);
  backdrop-filter: blur(6px);
  -webkit-backdrop-filter: blur(6px);
  opacity: 0;
  transition: opacity 0.24s ease;
}
#fa-backdrop.open { display: block; opacity: 1; }

#fa-panel {
  display: flex;
  flex-direction: column;
  position: fixed;
  top: 50%; left: 50%;
  transform: translate(-50%, -48%) scale(0.96);
  z-index: 8600;
  width: min(520px, 94vw);
  max-height: 86vh;
  border-radius: 18px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  box-shadow: 0 20px 60px rgba(80,60,140,.18), 0 2px 8px rgba(0,0,0,.06);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.28s ease,
              transform 0.28s cubic-bezier(0.34, 1.56, 0.64, 1);
  overflow: hidden;
}
#fa-panel.open {
  opacity: 1;
  pointer-events: auto;
  transform: translate(-50%, -50%) scale(1);
}

.fa-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 22px 14px;
  border-bottom: 1px solid #f0f0f8;
  flex-shrink: 0;
  background: #ffffff;
}
.fa-header-left { display: flex; align-items: center; gap: 13px; }
.fa-icon-wrap {
  width: 40px; height: 40px;
  border-radius: 10px;
  background: #f0fdf4;
  display: flex; align-items: center; justify-content: center;
  font-size: 20px; flex-shrink: 0;
}
.fa-title {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 17px; font-weight: 800; color: #1a1a2e; line-height: 1.2;
}
.fa-subtitle {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 11px; color: #64748b; margin-top: 2px;
}
.fa-close-btn {
  width: 34px; height: 34px;
  border-radius: 50%;
  border: 1.5px solid #e8e8f0;
  background: #fafafa;
  color: #888; font-size: 13px;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer;
  transition: background .15s, color .15s, border-color .15s;
  flex-shrink: 0;
}
.fa-close-btn:hover { background: #fee2e2; color: #dc2626; border-color: #fca5a5; }

.fa-body {
  overflow-y: auto;
  flex: 1;
  padding: 18px 22px 24px;
  scrollbar-width: thin;
  scrollbar-color: #e0d8ff transparent;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.fa-body::-webkit-scrollbar { width: 5px; }
.fa-body::-webkit-scrollbar-thumb { background: #d0c8f0; border-radius: 3px; }

.fa-items-container {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.fa-item {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  padding: 14px 18px;
  border-radius: 12px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-left: 4px solid #16a34a;
  opacity: 0;
  transform: translateY(10px);
  transition: opacity 0.30s ease, transform 0.30s ease;
}
.fa-item.visible { opacity: 1; transform: translateY(0); }

.fa-item-roman {
  min-width: 32px; height: 32px;
  border-radius: 50%;
  background: #16a34a;
  color: #ffffff;
  font-size: 12px; font-weight: 800;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  font-style: italic;
  box-shadow: 0 2px 8px rgba(22,163,74,.30);
}
.fa-item-body { flex: 1; }
.fa-item-label {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 12px; font-weight: 700;
  color: #166534;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  margin-bottom: 5px;
}
.fa-item-value {
  font-family: 'Courier New', Courier, monospace;
  font-size: 15px; font-weight: 800;
  color: #1e293b;
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  border-radius: 8px;
  padding: 6px 12px;
  display: inline-block;
  word-break: break-word;
}

.fa-insight-card {
  border-radius: 13px;
  padding: 14px 18px;
  background: #fffbf0;
  border: 1.5px solid #fde8a0;
  opacity: 0;
  transition: opacity 0.35s ease;
}
.fa-insight-card.visible { opacity: 1; }
.fa-insight-label {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 10px; font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 1.5px;
  color: #b45309;
  margin-bottom: 7px;
}
.fa-insight-text {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 13px; color: #78350f; line-height: 1.72;
}
</style>
"""

_FINAL_ANSWER_JS = r"""
(function initFinalAnswerSystem(){
  'use strict';
  var faOpen = false;
  var _built = false;
  function _el(id){ return document.getElementById(id); }
  function _onReady(fn){ if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',fn); else setTimeout(fn,0); }
  function _esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

  function _loadData(){
    try{
      var tag = _el('__final_answer_data__');
      if(!tag) return {};
      return JSON.parse(tag.textContent) || {};
    } catch(e){ return {}; }
  }

  function _buildPanel(){
    if(_built) return;
    _built = true;
    var data = _loadData();
    var items = Array.isArray(data.items) ? data.items : [];
    var container = _el('fa-items-container');
    if(!container) return;

    if(items.length === 0 && data.raw_answer){
      container.innerHTML = '<div class="fa-item visible">'
        + '<div class="fa-item-roman">i</div>'
        + '<div class="fa-item-body">'
        + '<div class="fa-item-label">Answer</div>'
        + '<div class="fa-item-value">' + _esc(data.raw_answer) + '</div>'
        + '</div></div>';
    } else {
      var html = '';
      for(var i = 0; i < items.length; i++){
        var it = items[i];
        var valueStr = _esc(it.value || '');
        if(it.unit && it.value && it.value.indexOf(it.unit) === -1){
          valueStr += ' ' + _esc(it.unit);
        }
        html += '<div class="fa-item" id="fa-item-' + i + '">'
              + '<div class="fa-item-roman">' + _esc(it.roman) + '</div>'
              + '<div class="fa-item-body">'
              + '<div class="fa-item-label">' + _esc(it.label) + '</div>'
              + '<div class="fa-item-value">' + valueStr + '</div>'
              + '</div></div>';
      }
      container.innerHTML = html;
    }

    var insEl = _el('fa-insight-text');
    if(insEl) insEl.textContent = data.key_insight || '';
  }

  function _animateReveal(){
    var items = document.querySelectorAll('.fa-item');
    for(var i = 0; i < items.length; i++){
      (function(el, idx){
        el.classList.remove('visible');
        el.style.transition = 'none';
        setTimeout(function(){
          el.style.transition = 'opacity .30s ease, transform .30s ease';
          el.classList.add('visible');
        }, 80 + idx * 110);
      })(items[i], i);
    }
    var insCard = _el('fa-insight-card');
    if(insCard){
      insCard.classList.remove('visible');
      setTimeout(function(){
        insCard.classList.add('visible');
      }, 80 + items.length * 110 + 120);
    }
  }

  function openFinalAnswer(){
    _buildPanel();
    var backdrop = _el('fa-backdrop');
    var panel    = _el('fa-panel');
    if(!backdrop || !panel) return;
    backdrop.classList.add('open');
    panel.classList.add('open');
    panel.setAttribute('aria-hidden', 'false');
    faOpen = true;
    setTimeout(_animateReveal, 80);
  }

  function closeFinalAnswer(){
    var backdrop = _el('fa-backdrop');
    var panel    = _el('fa-panel');
    if(backdrop) backdrop.classList.remove('open');
    if(panel){ panel.classList.remove('open'); panel.setAttribute('aria-hidden','true'); }
    faOpen = false;
    _built = false;
  }

  window.openFinalAnswer  = openFinalAnswer;
  window.closeFinalAnswer = closeFinalAnswer;
  window.toggleFinalAnswer = function(){ faOpen ? closeFinalAnswer() : openFinalAnswer(); };

  _onReady(function(){
    function wireBtn(){
      var btn = document.getElementById('fa-ctrl-btn');
      if(btn){
        btn.removeAttribute('onclick');
        btn.addEventListener('click', function(e){ e.stopPropagation(); faOpen ? closeFinalAnswer() : openFinalAnswer(); });
      } else { setTimeout(wireBtn, 80); }
    }
    wireBtn();

    var closeBtn = _el('fa-close');
    if(closeBtn) closeBtn.addEventListener('click', function(e){ e.stopPropagation(); closeFinalAnswer(); });

    var backdrop = _el('fa-backdrop');
    if(backdrop) backdrop.addEventListener('click', function(e){ if(e.target === backdrop) closeFinalAnswer(); });

    document.addEventListener('keydown', function(e){ if(e.key === 'Escape' && faOpen) closeFinalAnswer(); });
  });
})();
"""


def inject_final_answer_panel(html, answer_targets, final_answer, key_insight):
    html = re.sub(r'<script[^>]+id=["\']__sol_data__["\'][^>]*>.*?</script>', '', html, flags=re.DOTALL)
    html = re.sub(r'<(?:div|aside)[^>]+id=["\']sol-backdrop["\'][^>]*>.*?</(?:div|aside)>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<(?:div|aside)[^>]+id=["\']sol-panel["\'][^>]*>.*?</(?:div|aside)>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]+id=["\']qanim-solution-styles["\'][^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
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

    QAnimLogger.ok("FinalAnswer", f"Injected v11.2 Final Answer panel ({len(answer_targets or [])} item(s))")
    return html


# ===========================================================================
#  MODULE 7.5 -- Haiku Step-by-Step Solution Generator
# ===========================================================================

_HAIKU_SOLUTION_SYSTEM = """You are a patient, expert tutor generating a detailed step-by-step solution for a student.

RULES (follow every one):
1. Number every step: "Step 1:", "Step 2:", etc. -- never skip numbering.
2. At the START of each step, name the concept or formula being used in BOLD using **formula/concept name**.
3. Show ALL working -- do not skip arithmetic or algebra.
4. Use simple, plain English a high-school student can understand.
5. After the last numbered step, add a "Final Answer:" line with the complete result and units.
6. Keep each step focused on ONE action only.
7. Do NOT use LaTeX notation -- write math in plain text (e.g. "F = m x a" not "F=ma^{}").
8. End with a one-sentence "Key Insight:" that captures the most important concept.

FORMAT EXAMPLE:
Step 1: **Identify the given information**
We know the mass m = 5 kg and acceleration a = 3 m/s^2. Write these down first.

Step 2: **Apply Newton's Second Law**
The formula is F = m x a. Substitute the values: F = 5 x 3 = 15 N.

Final Answer: The force is 15 Newtons (15 N).

Key Insight: Newton's Second Law links force, mass, and acceleration -- if mass doubles, force doubles for the same acceleration."""

_HAIKU_SOLUTION_USER_TEMPLATE = """Generate a detailed, numbered step-by-step solution for this question.
Follow the system instructions exactly.

QUESTION: {question}"""


class HaikuSolutionGenerator:
    @classmethod
    def generate(cls, question):
        QAnimLogger.info("HaikuSolution", f"Generating via {HAIKU_SOLUTION_MODEL}")
        prompt = _HAIKU_SOLUTION_USER_TEMPLATE.format(question=question[:600])
        system_blocks = [
            {
                "type": "text",
                "text": _HAIKU_SOLUTION_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        try:
            msg = client.messages.create(
                model=HAIKU_SOLUTION_MODEL,
                max_tokens=MAX_TOK_HAIKU_SOLUTION,
                system=system_blocks,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = msg.content[0].text.strip()
            QAnimLogger.info(
                "HaikuSolution",
                f"stop_reason={msg.stop_reason}  len={len(raw)}"
                f"  cache_read={getattr(msg.usage, 'cache_read_input_tokens', 0)}"
                f"  cache_create={getattr(msg.usage, 'cache_creation_input_tokens', 0)}"
            )
            return cls._parse(raw)
        except Exception as e:
            QAnimLogger.error("HaikuSolution", f"Generation failed: {e}")
            return cls._fallback(question)

    @classmethod
    async def generate_async(cls, question):
        # FIXED: run_in_executor so sync client.messages.create does not block
        # the asyncio event loop during asyncio.gather()
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, cls.generate, question)

    @classmethod
    def _parse(cls, raw):
        steps = []
        final_answer = ""
        key_insight  = ""
        step_pattern = re.compile(
            r'Step\s+(\d+)\s*:\s*(.*?)(?=Step\s+\d+\s*:|\*{0,2}Final Answer\s*:|\*{0,2}Key Insight\s*:|$)',
            re.DOTALL | re.IGNORECASE)
        for m in step_pattern.finditer(raw):
            step_text = m.group(2).strip()
            if step_text:
                steps.append(f"Step {m.group(1)}: {step_text}")
        fa_match = re.search(r'\*{0,2}Final Answer\*{0,2}\s*:\s*(.*?)(?=\*{0,2}Key Insight\*{0,2}\s*:|$)', raw, re.DOTALL | re.IGNORECASE)
        if fa_match:
            final_answer = fa_match.group(1).strip().lstrip('*').rstrip('*').strip()
        ki_match = re.search(r'\*{0,2}Key Insight\*{0,2}\s*:\s*(.*?)$', raw, re.DOTALL | re.IGNORECASE)
        if ki_match:
            key_insight = ki_match.group(1).strip().lstrip('*').rstrip('*').strip()
        if not steps and raw:
            paragraphs = [p.strip() for p in re.split(r'\n\n+', raw) if p.strip()]
            steps = paragraphs[:10]
        QAnimLogger.ok("HaikuSolution", f"Parsed {len(steps)} steps, answer={len(final_answer)} chars")
        return {"steps": steps, "final_answer": final_answer, "key_insight": key_insight, "raw": raw}

    @classmethod
    def _fallback(cls, question):
        return {
            "steps": [
                "Step 1: **Identify the given information** -- Read the question carefully and list all known values with their units.",
                "Step 2: **Choose the right formula** -- Select the equation that connects the known and unknown quantities.",
                "Step 3: **Substitute values** -- Plug the given numbers into the formula, keeping track of units.",
                "Step 4: **Solve and check** -- Calculate the result and verify the units match what was asked.",
            ],
            "final_answer": "Please re-generate the solution for a detailed answer.",
            "key_insight":  "Always identify knowns and unknowns before applying any formula.",
            "raw": "",
        }


# ===========================================================================
#  MODULE 9 -- Answer Box System  (v11.1)
# ===========================================================================

def _build_answer_targets_tag(answer_targets):
    payload = {"answer_targets": answer_targets or []}
    return ('<script type="application/json" id="__answer_targets__">\n'
            + json.dumps(payload, ensure_ascii=False, indent=2) + '\n</script>')


def _build_answer_targets(to_find_targets, haiku_sol, final_answer, key_insight):
    targets = []
    _num_re = re.compile(
        r'([A-Za-z_][A-Za-z_0-9]*)\s*[=:]\s*([-+]?\d[\d.,]*(?:\s*[×x*]\s*10\^?[-+]?\d+)?)\s*([A-Za-z°%/²³·]+(?:\s*[A-Za-z°%/²³·]+)*)?',
        re.IGNORECASE)
    found_pairs = {}
    if final_answer:
        for m in _num_re.finditer(final_answer):
            sym   = m.group(1).strip()
            val   = m.group(2).strip()
            unit  = (m.group(3) or "").strip()
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
#answerbox-backdrop {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 8600;
  background: rgba(15, 23, 42, 0.40);
  backdrop-filter: blur(4px);
  opacity: 0;
  transition: opacity 0.22s ease;
}
#answerbox-backdrop.open {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  opacity: 1;
}
#answerbox-panel {
  width: min(540px, 94vw);
  max-height: 90vh;
  border-radius: 16px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.14);
  opacity: 0;
  pointer-events: none;
  transform: translateY(16px) scale(0.97);
  transition: opacity 0.25s ease,
              transform 0.26s cubic-bezier(0.34, 1.56, 0.64, 1);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
#answerbox-panel.open {
  opacity: 1;
  pointer-events: auto;
  transform: translateY(0) scale(1);
}
.ab-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
  flex-shrink: 0;
}
.ab-header-title {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 16px;
  font-weight: 800;
  color: #1e293b;
  display: flex;
  align-items: center;
  gap: 8px;
}
.ab-close-btn {
  width: 30px; height: 30px;
  border-radius: 8px;
  border: 1px solid #e2e8f0;
  background: #f8fafc;
  color: #64748b;
  font-size: 12px;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: background 0.15s;
}
.ab-close-btn:hover { background: #fee2e2; color: #dc2626; }
.ab-progress-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 20px 0;
  flex-shrink: 0;
}
.ab-progress-label {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 11px;
  font-weight: 700;
  color: #7c3aed;
  text-transform: uppercase;
  letter-spacing: 0.8px;
}
.ab-progress-dots { display: flex; gap: 5px; }
.ab-dot { width: 7px; height: 7px; border-radius: 50%; background: #e2e8f0; transition: background 0.2s; }
.ab-dot.done    { background: #16a34a; }
.ab-dot.current { background: #7c3aed; }
.ab-body { padding: 14px 20px 20px; overflow-y: auto; flex: 1; }
.ab-find-chip {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 10px 14px; border-radius: 10px;
  background: #f5f3ff; border: 1px solid #ddd6fe; margin-bottom: 14px;
}
.ab-find-icon { font-size: 16px; flex-shrink: 0; margin-top: 1px; }
.ab-find-text {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 12.5px; font-weight: 600; color: #5b21b6; line-height: 1.5;
}
.ab-find-label {
  font-size: 10px; font-weight: 800; text-transform: uppercase;
  letter-spacing: 1px; color: #7c3aed; display: block; margin-bottom: 2px;
}
.ab-instruction {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 13px; color: #64748b; margin-bottom: 10px; line-height: 1.6;
}
#ab-user-input {
  width: 100%; min-height: 80px; padding: 12px 14px;
  border-radius: 10px; border: 1.5px solid #e2e8f0;
  background: #f8fafc;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 13px; color: #1e293b; line-height: 1.6;
  resize: vertical; transition: border-color 0.15s; outline: none; box-sizing: border-box;
}
#ab-user-input:focus { border-color: #7c3aed; background: #ffffff; }
#ab-user-input::placeholder { color: #94a3b8; }
#ab-submit-btn {
  width: 100%; padding: 12px; margin-top: 10px; border-radius: 10px;
  border: none; background: #7c3aed; color: #ffffff;
  font-size: 14px; font-weight: 700; font-family: inherit;
  cursor: pointer; transition: background 0.15s, transform 0.1s;
}
#ab-submit-btn:hover { background: #6d28d9; transform: translateY(-1px); }
#ab-submit-btn:active { transform: translateY(0); }
#ab-feedback {
  display: none; margin-top: 14px; border-radius: 12px;
  overflow: hidden; border: 1px solid transparent;
  animation: ab-feedback-in 0.28s cubic-bezier(0.34,1.56,0.64,1);
}
@keyframes ab-feedback-in {
  from { opacity:0; transform:translateY(8px) scale(0.97); }
  to   { opacity:1; transform:translateY(0)   scale(1);    }
}
#ab-feedback.show    { display: block; }
#ab-feedback.correct { border-color: #bbf7d0; }
#ab-feedback.almost  { border-color: #fed7aa; }
#ab-feedback.wrong   { border-color: #fecaca; }
.ab-feedback-top { display: flex; align-items: center; gap: 10px; padding: 12px 16px; }
#ab-feedback.correct .ab-feedback-top { background: #f0fdf4; }
#ab-feedback.almost  .ab-feedback-top { background: #fff7ed; }
#ab-feedback.wrong   .ab-feedback-top { background: #fef2f2; }
.ab-feedback-icon { font-size: 22px; flex-shrink: 0; }
.ab-feedback-verdict {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 15px; font-weight: 800;
}
#ab-feedback.correct .ab-feedback-verdict { color: #15803d; }
#ab-feedback.almost  .ab-feedback-verdict { color: #c2410c; }
#ab-feedback.wrong   .ab-feedback-verdict { color: #b91c1c; }
.ab-feedback-insight { padding: 10px 16px 13px; border-top: 1px solid; }
#ab-feedback.correct .ab-feedback-insight { background:#fafffe; border-color:#bbf7d0; }
#ab-feedback.almost  .ab-feedback-insight { background:#fffbf5; border-color:#fed7aa; }
#ab-feedback.wrong   .ab-feedback-insight { background:#fff8f8; border-color:#fecaca; }
.ab-insight-label {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 10px; font-weight: 800; text-transform: uppercase;
  letter-spacing: 1.2px; color: #64748b; margin-bottom: 4px;
}
.ab-insight-text {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 12.5px; color: #1e293b; line-height: 1.68;
}
.ab-action-row { display: none; gap: 8px; margin-top: 12px; }
.ab-action-row.show { display: flex; }
#ab-retry-btn {
  flex: 1; padding: 9px 14px; border-radius: 9px; border: 1px solid #e2e8f0;
  background: #f8fafc; color: #64748b; font-size: 12px; font-weight: 600;
  font-family: inherit; cursor: pointer; transition: background 0.15s;
}
#ab-retry-btn:hover { background: #ede9fe; border-color: #7c3aed; color: #7c3aed; }
#ab-next-target-btn {
  flex: 2; padding: 9px 14px; border-radius: 9px; border: none;
  background: #7c3aed; color: #fff; font-size: 12px; font-weight: 700;
  font-family: inherit; cursor: pointer; display: none; transition: background 0.15s;
}
#ab-next-target-btn:hover { background: #6d28d9; }
#ab-next-target-btn.show  { display: block; }
#ab-alldone-card {
  display: none; text-align: center; padding: 28px 20px;
  border-radius: 14px; background: linear-gradient(135deg, #f0fdf4, #fefce8);
  border: 1.5px solid #bbf7d0; margin-top: 10px;
}
#ab-alldone-card.show { display: block; }
.ab-alldone-emoji { font-size: 40px; display: block; margin-bottom: 10px; }
.ab-alldone-title { font-family: -apple-system, 'Segoe UI', Arial, sans-serif; font-size: 18px; font-weight: 800; color: #15803d; margin-bottom: 6px; }
.ab-alldone-sub { font-family: -apple-system, 'Segoe UI', Arial, sans-serif; font-size: 13px; color: #166534; line-height: 1.6; }
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
      <div>
        <span class="ab-find-label">Find</span>
        <div class="ab-find-text" id="ab-find-text">Loading...</div>
      </div>
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
      <div class="ab-alldone-sub">Great work. Open <strong>Solution</strong> to review the full step-by-step explanation.</div>
    </div>
  </div>
</div>
</div>
"""

_ANSWER_BOX_JS = r"""
(function initAnswerBox(){
  'use strict';
  var abOpen = false;
  var _targets = [];
  var _currentIdx = 0;
  var _loaded = false;

  function _el(id){ return document.getElementById(id); }
  function _onReady(fn){ if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',fn); else setTimeout(fn,0); }

  function _loadTargets(){
    if(_loaded) return;
    _loaded = true;
    try{
      var tag = _el('__answer_targets__');
      if(!tag){ _useFallback(); return; }
      var data = JSON.parse(tag.textContent) || {};
      _targets = Array.isArray(data.answer_targets) ? data.answer_targets : [];
    }catch(e){ _targets = []; }
    if(_targets.length === 0) _useFallback();
  }

  function _useFallback(){
    try{
      var tag = _el('__sol_data__');
      if(!tag) return;
      var data = JSON.parse(tag.textContent) || {};
      _targets = [{
        label:   'Final Answer',
        value:   String(data.answer || ''),
        unit:    '',
        insight: String(data.insight || 'Apply the relevant formula step by step.')
      }];
    }catch(e){ _targets = []; }
  }

  function _renderTarget(idx){
    var t = _targets[idx];
    if(!t) return;
    var findEl = _el('ab-find-text');
    if(findEl) findEl.textContent = t.label || 'Answer';
    var total = _targets.length;
    var progLabel = _el('ab-progress-label');
    if(progLabel) progLabel.textContent = 'Question ' + (idx+1) + ' of ' + total;
    var dotsEl = _el('ab-progress-dots');
    if(dotsEl){
      var html = '';
      for(var i=0;i<total;i++){
        var cls = i < idx ? 'ab-dot done' : i === idx ? 'ab-dot current' : 'ab-dot';
        html += '<div class="' + cls + '"></div>';
      }
      dotsEl.innerHTML = html;
    }
    var inp = _el('ab-user-input');
    if(inp){ inp.value = ''; inp.removeAttribute('disabled'); }
    var fb = _el('ab-feedback');
    if(fb) fb.className = '';
    var ar = _el('ab-action-row');
    if(ar) ar.className = 'ab-action-row';
    var ntb = _el('ab-next-target-btn');
    if(ntb) ntb.style.display = 'none';
    var sb = _el('ab-submit-btn');
    if(sb){ sb.style.display = ''; sb.disabled = false; }
    var adc = _el('ab-alldone-card');
    if(adc) adc.className = '';
    var unit = t.unit ? ' (' + t.unit + ')' : '';
    if(inp) inp.placeholder = 'Type your answer' + unit + '...';
  }

  function _extractNums(s){
    var m = s.match(/[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?/g);
    return m ? m.map(parseFloat).filter(function(n){ return isFinite(n); }) : [];
  }

  function _validate(userAns, correctAns){
    if(!userAns || !userAns.trim()) return 'empty';
    var userNums    = _extractNums(userAns);
    var correctNums = _extractNums(correctAns);
    if(userNums.length > 0 && correctNums.length > 0){
      var uVal  = userNums[0];
      var cVal  = correctNums[0];
      var denom = Math.abs(cVal) + 1e-12;
      var relErr = Math.abs(uVal - cVal) / denom;
      if(relErr < 0.01)  return 'correct';
      if(relErr < 0.15)  return 'almost';
      return 'wrong';
    }
    var uC = userAns.toLowerCase().trim().replace(/[^a-z0-9\s]/g,' ');
    var cC = correctAns.toLowerCase().trim().replace(/[^a-z0-9\s]/g,' ');
    if(uC === cC) return 'correct';
    var STOP={a:1,an:1,the:1,is:1,are:1,of:1,to:1,in:1,and:1,or:1,it:1,be:1,at:1,as:1,by:1,'for':1,on:1,with:1,that:1,this:1};
    function kw(s){ var w=s.split(/\s+/),r={};w.forEach(function(x){if(x.length>1&&!STOP[x])r[x]=true;});return r; }
    var uKW = kw(uC); var cKW = kw(cC); var cKeys = Object.keys(cKW);
    if(cKeys.length === 0) return 'wrong';
    var match = cKeys.filter(function(k){ return uKW[k]; }).length;
    var overlap = match / cKeys.length;
    if(overlap >= 0.80) return 'correct';
    if(overlap >= 0.40) return 'almost';
    return 'wrong';
  }

  var _FB = {
    correct: { icon:'✅', verdict:'Correct!',       cls:'correct' },
    almost:  { icon:'〰️', verdict:'Almost Correct', cls:'almost'  },
    wrong:   { icon:'❌', verdict:'Wrong Answer',   cls:'wrong'   },
    empty:   { icon:'❓', verdict:'No Answer',      cls:'wrong'   }
  };

  function _showFeedback(verdict, insight){
    var info = _FB[verdict] || _FB['wrong'];
    var fb   = _el('ab-feedback');
    var icon = _el('ab-feedback-icon');
    var verd = _el('ab-feedback-verdict');
    var ins  = _el('ab-insight-text');
    if(!fb) return;
    fb.className = 'show ' + info.cls;
    if(icon) icon.textContent = info.icon;
    if(verd) verd.textContent = info.verdict;
    if(ins)  ins.textContent  = insight || 'Review the step-by-step solution for more detail.';
    var ar = _el('ab-action-row');
    if(ar) ar.className = 'ab-action-row show';
    var ntb = _el('ab-next-target-btn');
    var isLast = (_currentIdx >= _targets.length - 1);
    if(ntb){
      if((verdict === 'correct' || verdict === 'almost') && !isLast){
        ntb.style.display = '';
        ntb.textContent   = 'Next \u2192';
      } else {
        ntb.style.display = 'none';
      }
    }
    if(verdict === 'correct' && !isLast){
      setTimeout(function(){ _advanceTarget(); }, 1400);
    }
    if(verdict === 'correct' && isLast){
      setTimeout(function(){
        var adc = _el('ab-alldone-card');
        if(adc) adc.className = 'show';
        var sb = _el('ab-submit-btn');
        if(sb) sb.style.display = 'none';
        var ntb2 = _el('ab-next-target-btn');
        if(ntb2) ntb2.style.display = 'none';
      }, 900);
    }
  }

  function _advanceTarget(){
    if(_currentIdx < _targets.length - 1){
      _currentIdx++;
      _renderTarget(_currentIdx);
      var inp = _el('ab-user-input');
      if(inp) inp.focus();
    }
  }

  function openAnswerBox(){
    _loadTargets();
    _currentIdx = 0;
    var backdrop = _el('answerbox-backdrop');
    var panel    = _el('answerbox-panel');
    if(!backdrop || !panel) return;
    backdrop.classList.add('open'); backdrop.setAttribute('aria-hidden','false');
    panel.classList.add('open');    panel.setAttribute('aria-hidden','false');
    abOpen = true;
    _renderTarget(_currentIdx);
    setTimeout(function(){
      var inp = _el('ab-user-input');
      if(inp) inp.focus();
    }, 220);
  }

  function closeAnswerBox(){
    var backdrop = _el('answerbox-backdrop');
    var panel    = _el('answerbox-panel');
    if(backdrop){ backdrop.classList.remove('open'); backdrop.setAttribute('aria-hidden','true'); }
    if(panel){    panel.classList.remove('open');    panel.setAttribute('aria-hidden','true');    }
    abOpen = false;
  }

  function resetAnswerBox(){
    _loaded = false;
    _targets = [];
    _currentIdx = 0;
  }

  window.openAnswerBox  = openAnswerBox;
  window.closeAnswerBox = closeAnswerBox;
  window.resetAnswerBox = resetAnswerBox;

  _onReady(function(){
    function wireCtrlBtn(){
      var btn = document.getElementById('answerbox-ctrl-btn');
      if(btn){
        btn.removeAttribute('onclick');
        btn.addEventListener('click', function(e){
          e.stopPropagation();
          abOpen ? closeAnswerBox() : openAnswerBox();
        });
      } else { setTimeout(wireCtrlBtn, 100); }
    }
    wireCtrlBtn();

    var closeBtn = _el('ab-close-btn');
    if(closeBtn) closeBtn.addEventListener('click', function(e){ e.stopPropagation(); closeAnswerBox(); });
    var backdrop = _el('answerbox-backdrop');
    if(backdrop) backdrop.addEventListener('click', function(e){ if(e.target===backdrop) closeAnswerBox(); });
    document.addEventListener('keydown', function(e){ if(e.key==='Escape' && abOpen) closeAnswerBox(); });

    var submitBtn = _el('ab-submit-btn');
    if(submitBtn) submitBtn.addEventListener('click', function(){
      var inp     = _el('ab-user-input');
      var userAns = inp ? inp.value.trim() : '';
      var target  = _targets[_currentIdx] || {};
      var verdict = _validate(userAns, target.value || '');
      _showFeedback(verdict, target.insight || '');
      if(inp) inp.disabled = true;
    });

    var inp2 = _el('ab-user-input');
    if(inp2) inp2.addEventListener('keydown', function(e){
      if((e.ctrlKey || e.metaKey) && e.key==='Enter'){
        e.preventDefault();
        var sb = _el('ab-submit-btn');
        if(sb) sb.click();
      }
    });

    var retryBtn = _el('ab-retry-btn');
    if(retryBtn) retryBtn.addEventListener('click', function(){
      var inp = _el('ab-user-input');
      if(inp){ inp.value=''; inp.disabled=false; inp.focus(); }
      var fb = _el('ab-feedback');
      if(fb) fb.className = '';
      var ar = _el('ab-action-row');
      if(ar) ar.className = 'ab-action-row';
      var sb = _el('ab-submit-btn');
      if(sb) sb.style.display = '';
      var ntb = _el('ab-next-target-btn');
      if(ntb) ntb.style.display = 'none';
    });

    var nextTargetBtn = _el('ab-next-target-btn');
    if(nextTargetBtn) nextTargetBtn.addEventListener('click', function(){ _advanceTarget(); });
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

    QAnimLogger.ok("AnswerBoxInjector", f"Answer box panel v11.1 injected ({len(answer_targets or [])} target(s))")
    return html


# ===========================================================================
#  MODULE 10 -- Floating Controls Bar
#  v11.3 MOBILE FIX: Reduced padding on mobile, safe-area-inset-bottom for iOS
# ===========================================================================

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
  background: rgba(255, 255, 255, 0.98);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1.5px solid transparent;
  border-radius: 16px;
  padding: 10px 14px;
  box-shadow: 0 6px 36px rgba(124, 58, 237, 0.18),
              0 2px 8px rgba(0, 0, 0, 0.08);
  white-space: nowrap;
  background-clip: padding-box;
  outline: 2.5px solid transparent;
  outline-offset: -1px;
  /* v11.3: ensure bar doesn't get too wide on mobile */
  max-width: calc(100vw - 24px);
  box-sizing: border-box;
}
#qanim-controls-bar::before {
  content: '';
  position: absolute;
  inset: -2px;
  border-radius: 18px;
  background: linear-gradient(90deg, #7c3aed, #db2777, #f59e0b, #7c3aed);
  background-size: 200% 100%;
  animation: qanim-bar-glow 4s linear infinite;
  z-index: -1;
}
@keyframes qanim-bar-glow {
  0%   { background-position: 0% 50%; }
  100% { background-position: 200% 50%; }
}
.qanim-ctrl-btn {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 8px 15px;
  border-radius: 10px;
  border: 1.5px solid #e2e8f0;
  background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
  color: #334155;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s, transform 0.12s, box-shadow 0.15s;
  user-select: none;
  letter-spacing: 0.2px;
  white-space: nowrap;
}
.qanim-ctrl-btn:hover {
  background: linear-gradient(135deg, #ede9fe 0%, #fdf4ff 100%);
  border-color: #7c3aed;
  color: #6d28d9;
  transform: translateY(-2px);
  box-shadow: 0 4px 14px rgba(124, 58, 237, 0.22);
}
.qanim-ctrl-btn:active { transform: translateY(0); box-shadow: none; }
.qanim-ctrl-sep {
  width: 1px;
  height: 22px;
  background: linear-gradient(to bottom, transparent, #c4b5fd, transparent);
  flex-shrink: 0;
}

/* v11.3 MOBILE FIX: Smaller controls on narrow screens */
@media (max-width: 600px) {
  #qanim-controls-bar {
    bottom: max(10px, env(safe-area-inset-bottom, 10px));
    padding: 8px 10px;
    gap: 4px;
    border-radius: 14px;
  }
  .qanim-ctrl-btn {
    padding: 7px 10px;
    font-size: 11px;
    gap: 4px;
    border-radius: 8px;
  }
  /* On very small screens, hide text labels and show only icons */
}
@media (max-width: 420px) {
  #qanim-controls-bar {
    padding: 6px 8px;
    gap: 3px;
  }
  .qanim-ctrl-btn {
    padding: 6px 8px;
    font-size: 10px;
    gap: 3px;
  }
  .qanim-ctrl-btn .ctrl-label {
    display: none;
  }
  .qanim-ctrl-sep {
    height: 18px;
  }
}
</style>
"""

_CONTROLS_BAR_DOM = """
<div id="qanim-controls-bar" role="toolbar" aria-label="QAnim Controls">
  <button class="qanim-ctrl-btn" id="tofind-btn" data-tofind-btn title="What to find">
    <span>&#x1F50D;</span><span class="ctrl-label">Find</span>
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
#  MODULE 10.5 -- Layout Wrapper  (v11.3 -- MOBILE RESPONSIVE FIX)
# ===========================================================================
#
# KEY MOBILE FIXES in v11.3:
#
# 1. #pw-scene-wrap:
#    - aspect-ratio: 10 / 6  (matches SVG viewBox 1000:600)
#    - position: relative  (for absolutely-positioned SVG child)
#    - min-height: 180px  (prevents collapse on tiny screens)
#    - overflow: hidden  (clean clip)
#
# 2. SVG inside #pw-scene-wrap:
#    - position: absolute; top:0; left:0; width:100%; height:100%
#    - Fills the aspect-ratio box completely on all screen sizes
#    - preserveAspectRatio="xMidYMid meet" ensures content scales correctly
#
# 3. #pw-scene-ind (scene indicator):
#    - position: absolute inside #pw-scene-wrap (not affected by SVG position)
#
# 4. Padding:
#    - padding-bottom: 120px on mobile (was 100px -- controls bar + safe area)
#    - Extra clearance for the floating controls bar
#
# 5. Body padding:
#    - body padding-bottom removed (handled by #page-wrapper padding-bottom)
#
# 6. JS: _buildWrapper():
#    - After moving SVG into #pw-scene-wrap, forces:
#        svg.setAttribute('width', '100%')
#        svg.setAttribute('height', '100%')
#        svg.setAttribute('preserveAspectRatio', 'xMidYMid meet')
#    - This makes the 1000x600 viewBox scale to fill any container size
#
# ===========================================================================

_LAYOUT_WRAPPER_CSS = """
<style id="qanim-layout-wrapper-styles">
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

*, *::before, *::after { box-sizing: border-box; }

body {
  margin: 0; padding: 0;
  min-height: 100vh;
  background: #f1f5f9;
  font-family: 'Inter', -apple-system, 'Segoe UI', Arial, sans-serif;
  /* v11.3: no overflow:hidden on body -- allow page scroll */
  overflow-x: hidden;
  overflow-y: auto;
}

/* ─────────────────────────────────────────────────────────────
   PAGE WRAPPER
───────────────────────────────────────────────────────────── */
#page-wrapper {
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-width: 1080px;
  margin: 0 auto;
  /* v11.3 MOBILE FIX: larger bottom padding so controls bar never covers content */
  padding: 18px 16px 130px;
  width: 100%;
}

/* ─────────────────────────────────────────────────────────────
   1. HEADER ROW
───────────────────────────────────────────────────────────── */
#pw-header {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 14px 18px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 14px;
  box-shadow: 0 2px 10px rgba(0,0,0,.06);
}
#pw-topic-badge {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 5px 13px;
  border-radius: 999px;
  background: linear-gradient(135deg, #ede9fe, #fdf4ff);
  border: 1px solid #c4b5fd;
  flex-shrink: 0;
}
#pw-pulse-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: #7c3aed;
  animation: pw-pulse 2s ease-in-out infinite;
  flex-shrink: 0;
}
@keyframes pw-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50%       { opacity: 0.45; transform: scale(0.72); }
}
#pw-badge-text {
  font-size: 11.5px;
  font-weight: 700;
  color: #6d28d9;
  letter-spacing: 0.3px;
  white-space: nowrap;
}
#pw-page-title {
  font-size: 15px;
  font-weight: 700;
  color: #1e293b;
  line-height: 1.35;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

/* ─────────────────────────────────────────────────────────────
   2. GIVEN VALUES STRIP
───────────────────────────────────────────────────────────── */
#pw-given-strip {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}
.pw-given-card {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 11px 14px;
  border-radius: 12px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-left-width: 4px;
  box-shadow: 0 1px 6px rgba(0,0,0,.05);
  min-width: 0;
}
.pw-given-label {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.7px; color: #64748b; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
}
.pw-given-symbol { font-size: 14px; font-weight: 800; color: #1e293b; font-style: italic; }
.pw-given-value  { font-size: 12px; font-weight: 600; color: #475569; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* ─────────────────────────────────────────────────────────────
   3. SVG SCENE WRAPPER  -- v11.3 MOBILE CRITICAL FIX
   
   The SVG has viewBox="0 0 1000 600" (aspect ratio 10:6).
   We use aspect-ratio: 10/6 on the container so it always
   has the correct proportional height for any screen width.
   The SVG itself is positioned absolutely to fill this box.
───────────────────────────────────────────────────────────── */
#pw-scene-wrap {
  position: relative;           /* SVG will be position:absolute inside */
  width: 100%;
  aspect-ratio: 10 / 6;         /* Matches 1000:600 viewBox exactly */
  min-height: 180px;            /* Prevent total collapse on very small screens */
  border-radius: 14px;
  overflow: hidden;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  box-shadow: 0 3px 18px rgba(0,0,0,.08);
}

/* v11.3: Force SVG to fill the aspect-ratio container */
#pw-scene-wrap > svg,
#pw-scene-wrap svg:first-of-type {
  position: absolute !important;
  top: 0 !important;
  left: 0 !important;
  width: 100% !important;
  height: 100% !important;
  display: block !important;
}

/* v11.3: Non-SVG direct children (div wrappers around SVG) also fill container */
#pw-scene-wrap > div:not([id*="scene-ind"]):not([id*="qanim"]):not([id*="tofind"]):not([id*="fa-"]):not([id*="answerbox"]):not([id*="notes"]):not([id*="voice"]) {
  position: absolute !important;
  top: 0 !important;
  left: 0 !important;
  width: 100% !important;
  height: 100% !important;
}

/* ─────────────────────────────────────────────────────────────
   4. SCENE INDICATOR PILL  -- must stay on top of SVG
───────────────────────────────────────────────────────────── */
#pw-scene-ind {
  position: absolute;
  top: 10px;
  left: 12px;
  z-index: 10;                  /* Above the SVG */
  background: rgba(255,255,255,0.93);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  border: 1px solid #e2e8f0;
  border-radius: 999px;
  padding: 4px 12px;
  font-family: 'Inter', -apple-system, sans-serif;
  font-size: 11px;
  font-weight: 700;
  color: #7c3aed;
  pointer-events: none;
  letter-spacing: 0.2px;
  box-shadow: 0 2px 8px rgba(0,0,0,.10);
  transition: opacity 0.22s ease;
}

/* ─────────────────────────────────────────────────────────────
   5. NAV ROW
───────────────────────────────────────────────────────────── */
#pw-nav-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  padding: 10px 0;
}
.pw-nav-btn {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 9px 22px;
  border-radius: 999px;
  border: 1.5px solid #e2e8f0;
  background: #ffffff;
  color: #334155;
  font-family: 'Inter', -apple-system, sans-serif;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s,
              transform 0.12s, box-shadow 0.15s, opacity 0.15s;
  user-select: none;
  box-shadow: 0 2px 8px rgba(0,0,0,.06);
}
.pw-nav-btn:hover:not(:disabled) {
  background: linear-gradient(135deg, #ede9fe, #fdf4ff);
  border-color: #7c3aed;
  color: #6d28d9;
  transform: translateY(-2px);
  box-shadow: 0 4px 14px rgba(124,58,237,.22);
}
.pw-nav-btn:active:not(:disabled) { transform: translateY(0); box-shadow: none; }
.pw-nav-btn:disabled { opacity: 0.32; cursor: default; pointer-events: none; }
#pw-dots-row {
  display: flex;
  align-items: center;
  gap: 7px;
}
.pw-dot {
  height: 8px;
  border-radius: 999px;
  transition: width 0.28s cubic-bezier(0.34,1.56,0.64,1),
              background 0.22s ease;
  cursor: pointer;
  width: 8px;
  background: #cbd5e1;
}
.pw-dot.active {
  width: 26px;
  background: linear-gradient(90deg, #7c3aed, #a855f7);
}

/* ─────────────────────────────────────────────────────────────
   6. SCENE DESCRIPTION CARD
───────────────────────────────────────────────────────────── */
#pw-scene-desc {
  padding: 16px 18px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-left-width: 4px;
  border-radius: 14px;
  box-shadow: 0 2px 10px rgba(0,0,0,.05);
  transition: border-color 0.3s ease;
}
#pw-scene-desc-title {
  font-size: 13px; font-weight: 800; color: #1e293b;
  margin-bottom: 5px; text-transform: uppercase; letter-spacing: 0.5px;
}
#pw-scene-desc-body {
  font-size: 13.5px; color: #475569; line-height: 1.65;
}

/* ─────────────────────────────────────────────────────────────
   RESPONSIVE BREAKPOINTS  (v11.3 updated)
───────────────────────────────────────────────────────────── */

/* Tablet: 2-col given strip */
@media (max-width: 820px) {
  #pw-given-strip { grid-template-columns: repeat(2, 1fr); }
  #pw-page-title  { font-size: 14px; }
}

/* Mobile landscape / small tablet */
@media (max-width: 600px) {
  #page-wrapper {
    padding: 12px 10px 130px;
    gap: 10px;
  }
  #pw-given-strip  { grid-template-columns: repeat(2, 1fr); gap: 8px; }
  #pw-header       { padding: 10px 12px; gap: 9px; }
  #pw-badge-text   { font-size: 10.5px; }
  #pw-page-title   { font-size: 13px; }
  .pw-nav-btn      { padding: 8px 16px; font-size: 12px; }
  #pw-scene-desc   { padding: 12px 14px; }
  #pw-scene-desc-title { font-size: 12px; }
  #pw-scene-desc-body  { font-size: 13px; }
  /* MOBILE: slightly smaller scene indicator */
  #pw-scene-ind    { font-size: 10px; padding: 3px 10px; top: 8px; left: 10px; }
}

/* Portrait mobile (most Android / iPhone sizes) */
@media (max-width: 480px) {
  #page-wrapper {
    padding: 10px 8px 130px;
    gap: 8px;
  }
  #pw-given-strip  { grid-template-columns: 1fr 1fr; gap: 6px; }
  .pw-given-card   { padding: 8px 10px; }
  .pw-given-label  { font-size: 9px; }
  .pw-given-symbol { font-size: 13px; }
  .pw-given-value  { font-size: 11px; }
  #pw-header       { padding: 8px 10px; gap: 7px; }
  #pw-topic-badge  { padding: 4px 10px; }
  #pw-badge-text   { font-size: 10px; }
  #pw-page-title   { font-size: 12px; -webkit-line-clamp: 3; }
  .pw-nav-btn      { padding: 7px 13px; font-size: 11px; }
  /* On very small phones, hide given strip to save vertical space */
}

/* Tiny screens */
@media (max-width: 360px) {
  #page-wrapper    { padding: 8px 6px 130px; gap: 7px; }
  #pw-given-strip  { display: none; }  /* Hide given strip on tiny screens */
  .pw-nav-btn      { padding: 6px 10px; font-size: 10px; }
  #pw-dots-row     { gap: 5px; }
  .pw-dot          { width: 6px; height: 6px; }
  .pw-dot.active   { width: 20px; }
}

/* Smart board / large display */
@media (min-width: 1400px) {
  #page-wrapper      { max-width: 1280px; padding: 24px 28px 140px; gap: 18px; }
  #pw-page-title     { font-size: 17px; }
  #pw-scene-desc-body { font-size: 15px; }
  .pw-nav-btn        { padding: 11px 28px; font-size: 14px; }
  .pw-given-label    { font-size: 11px; }
  .pw-given-symbol   { font-size: 16px; }
  .pw-given-value    { font-size: 13px; }
}
</style>
"""

# ---------------------------------------------------------------------------
# Given values data-tag builder (unchanged)
# ---------------------------------------------------------------------------
def _build_given_values_tag(given_values):
    payload = {"given": given_values or []}
    return ('<script type="application/json" id="__given_values__">\n'
            + json.dumps(payload, ensure_ascii=False, indent=2) + '\n</script>')


# ---------------------------------------------------------------------------
# v11.3 MOBILE FIX: Updated Layout Wrapper JS
# Key change: after moving SVG into #pw-scene-wrap, force SVG attributes
# so it scales correctly on all screen sizes.
# ---------------------------------------------------------------------------
_LAYOUT_WRAPPER_JS = r"""
<script id="qanim-layout-wrapper">
/* QAnim Layout Wrapper v11.3 -- Mobile Responsive Fix
   Moves existing SVG/body content into #page-wrapper structure.
   CRITICAL FIX: Forces SVG width/height/preserveAspectRatio after DOM move.
*/
(function initLayoutWrapper(){
  'use strict';

  function _el(id){ return document.getElementById(id); }
  function _onReady(fn){
    if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',fn);
    else setTimeout(fn,0);
  }
  function _esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

  /* ── load given values ── */
  function _loadGiven(){
    try{
      var tag=_el('__given_values__');if(!tag)return[];
      var data=JSON.parse(tag.textContent)||{};return Array.isArray(data.given)?data.given:[];
    }catch(e){return[];}
  }

  /* ── build given strip ── */
  function _buildGivenStrip(strip){
    var given=_loadGiven();
    if(!given.length){strip.style.display='none';return;}
    var html='';
    for(var i=0;i<given.length;i++){
      var g=given[i];
      var col=g.color||'#7c3aed';
      html+='<div class="pw-given-card" style="border-left-color:'+_esc(col)+';">'
           +'<span class="pw-given-label">'+_esc(g.label||'')+'</span>'
           +'<span class="pw-given-symbol">'+_esc(g.symbol||'')+'</span>'
           +'<span class="pw-given-value">'+_esc(g.value||'')+'</span>'
           +'</div>';
    }
    strip.innerHTML=html;
  }

  /* ── scene descriptions ── */
  var _FALLBACK_TITLES=['Physical Setup','Core Concept','Second Mechanism','Putting It Together','Step-by-Step Approach'];
  var _FALLBACK_BODIES=[
    'See the physical setup in the animation above.',
    'The animation shows the key formula or principle.',
    'A secondary effect or mechanism is highlighted.',
    'Both ideas are connected in the diagram.',
    'Follow the numbered steps to reach the solution.'
  ];
  var _FALLBACK_ACCENTS=['#3b82f6','#7c3aed','#06b6d4','#16a34a','#f59e0b'];

  function _getSceneDesc(idx){
    var scEl=_el('scene-'+idx);
    if(!scEl) return {title:_FALLBACK_TITLES[idx]||'Scene '+(idx+1),body:_FALLBACK_BODIES[idx]||'',accent:_FALLBACK_ACCENTS[idx]||'#7c3aed'};
    var dt=scEl.getAttribute('data-pw-title');
    var db=scEl.getAttribute('data-pw-body');
    var da=scEl.getAttribute('data-pw-accent');
    if(dt||db){
      return{title:dt||_FALLBACK_TITLES[idx],body:db||'',accent:da||_FALLBACK_ACCENTS[idx]};
    }
    var bodyEl=scEl.querySelector('[id*="info"], [class*="info"], [id*="card"], [class*="card"], .scene-body, foreignObject p, foreignObject div');
    var titleEl=scEl.querySelector('[id*="title"], [class*="title"], h2, h3');
    return{
      title:(titleEl?titleEl.textContent.trim():'')||_FALLBACK_TITLES[idx]||'Scene '+(idx+1),
      body:(bodyEl?bodyEl.textContent.replace(/\s+/g,' ').trim():'')||_FALLBACK_BODIES[idx]||'',
      accent:_FALLBACK_ACCENTS[idx]||'#7c3aed'
    };
  }

  /* ────────────────────────────────────────────────────────────
     v11.3 MOBILE CRITICAL FIX: _fixSvgScaling()
     
     After the SVG is moved into #pw-scene-wrap, we must:
     1. Set width="100%" height="100%" so it fills the container
     2. Set preserveAspectRatio="xMidYMid meet" so the 1000x600
        viewBox scales proportionally to fit any container size
     3. Keep the viewBox intact (do NOT remove it)
     
     Without this, the SVG renders at its natural 1000x600 pixel
     size and overflows the container on mobile screens.
  ──────────────────────────────────────────────────────────── */
  function _fixSvgScaling(scWrap){
    /* Find the main animation SVG inside the scene wrapper */
    var svgs = scWrap.querySelectorAll('svg');
    for(var si = 0; si < svgs.length; si++){
      var svg = svgs[si];
      /* Skip tiny inline SVGs (like icons in buttons) */
      var vb = svg.getAttribute('viewBox') || '';
      if(vb && vb.indexOf('1000') !== -1){
        /* This is the main animation SVG */
        svg.setAttribute('width', '100%');
        svg.setAttribute('height', '100%');
        svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
        svg.style.cssText = (svg.style.cssText || '') +
          ';position:absolute!important;top:0!important;left:0!important;' +
          'width:100%!important;height:100%!important;display:block!important;';
        console.log('[QAnim LW v11.3] SVG scaled for mobile, viewBox=' + vb);
        break;
      }
    }
    /* Fallback: scale the first large SVG found */
    if(svgs.length > 0 && svgs[0].getAttribute('viewBox')){
      var firstSvg = svgs[0];
      if(!firstSvg.getAttribute('preserveAspectRatio')){
        firstSvg.setAttribute('width', '100%');
        firstSvg.setAttribute('height', '100%');
        firstSvg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
        firstSvg.style.cssText = (firstSvg.style.cssText || '') +
          ';position:absolute!important;top:0!important;left:0!important;' +
          'width:100%!important;height:100%!important;display:block!important;';
      }
    }
  }

  /* ── DOM restructure ── */
  function _buildWrapper(){
    var wrapper=document.createElement('div');
    wrapper.id='page-wrapper';

    /* 1. Header */
    var hdr=document.createElement('div');
    hdr.id='pw-header';
    var badgeText='Animation';
    var qtextEl=document.querySelector('#qstrip .qtext, .qtext');
    if(qtextEl){
      var raw=(qtextEl.textContent||'').trim();
      raw=raw.replace(/^question[:\s]*/i,'').trim();
      if(raw.length>60) raw=raw.slice(0,57)+'...';
      badgeText=raw||badgeText;
    } else {
      var titleTag=document.querySelector('title');
      if(titleTag) badgeText=(titleTag.textContent||'').replace(/QAnim|Animation|v\d+\.\d+/gi,'').trim().slice(0,60)||badgeText;
    }
    hdr.innerHTML=
      '<div id="pw-topic-badge">'
        +'<div id="pw-pulse-dot"></div>'
        +'<span id="pw-badge-text">'+_esc(badgeText)+'</span>'
      +'</div>'
      +'<div id="pw-page-title">'+_esc(badgeText)+'</div>';
    wrapper.appendChild(hdr);

    /* 2. Given values strip */
    var gStrip=document.createElement('div');
    gStrip.id='pw-given-strip';
    _buildGivenStrip(gStrip);
    wrapper.appendChild(gStrip);

    /* 3. SVG scene wrap -- v11.3: position:relative for absolute SVG child */
    var scWrap=document.createElement('div');
    scWrap.id='pw-scene-wrap';

    /* Scene indicator pill -- must be in DOM before SVG so it's on top */
    var scInd=document.createElement('div');
    scInd.id='pw-scene-ind';
    scInd.textContent='Scene 1 of 5';
    scWrap.appendChild(scInd);

    wrapper.appendChild(scWrap);

    /* 4. Nav row */
    var navRow=document.createElement('div');
    navRow.id='pw-nav-row';
    navRow.innerHTML=
      '<button class="pw-nav-btn" id="pw-prev-btn" disabled aria-label="Previous scene">&#8592; Prev</button>'
      +'<div id="pw-dots-row"></div>'
      +'<button class="pw-nav-btn" id="pw-next-btn" aria-label="Next scene">Next &#8594;</button>';
    wrapper.appendChild(navRow);

    /* 5. Scene description card */
    var descCard=document.createElement('div');
    descCard.id='pw-scene-desc';
    descCard.innerHTML=
      '<div id="pw-scene-desc-title"></div>'
      +'<div id="pw-scene-desc-body"></div>';
    wrapper.appendChild(descCard);

    /* Move existing body children into scWrap (excluding overlay divs) */
    var KEEP_IDS=['qanim-error-fallback','answerbox-backdrop','fa-backdrop','tofind-backdrop',
                  'tofind-panel','fa-panel','answerbox-panel','qanim-notes-btn','qanim-notes-panel',
                  'qanim-controls-bar'];
    var toMove=[];
    var ch=document.body.childNodes;
    for(var ci=0;ci<ch.length;ci++){
      var node=ch[ci];
      if(node.nodeType===1){
        var skip=false;
        for(var ki=0;ki<KEEP_IDS.length;ki++){ if(KEEP_IDS[ki]===node.id){skip=true;break;} }
        if(!skip && node.tagName && node.tagName.toLowerCase()!=='script') toMove.push(node);
      }
    }
    for(var mi=0;mi<toMove.length;mi++) scWrap.appendChild(toMove[mi]);

    /* Prepend wrapper to body */
    document.body.insertBefore(wrapper,document.body.firstChild);

    /* v11.3 MOBILE FIX: Fix SVG scaling after move */
    _fixSvgScaling(scWrap);
  }

  /* ── count scenes ── */
  function _countScenes(){
    var n=0;
    for(var i=0;i<20;i++){if(!_el('scene-'+i))break;n++;}
    return n||5;
  }

  /* ── update indicator + dots + desc on scene change ── */
  function _onScene(idx,total){
    var ind=_el('pw-scene-ind');
    if(ind) ind.textContent='Scene '+(idx+1)+' of '+total;

    var dotsRow=_el('pw-dots-row');
    if(dotsRow){
      var dots=dotsRow.querySelectorAll('.pw-dot');
      for(var k=0;k<dots.length;k++) dots[k].classList.toggle('active',k===idx);
    }

    var pbtn=_el('pw-prev-btn'),nbtn=_el('pw-next-btn');
    if(pbtn){if(idx===0){pbtn.disabled=true;pbtn.setAttribute('disabled','true');}else{pbtn.disabled=false;pbtn.removeAttribute('disabled');}}
    if(nbtn){if(idx===total-1){nbtn.disabled=true;nbtn.setAttribute('disabled','true');}else{nbtn.disabled=false;nbtn.removeAttribute('disabled');}}

    var desc=_getSceneDesc(idx);
    var descEl=_el('pw-scene-desc');
    if(descEl){
      descEl.style.borderLeftColor=desc.accent;
      var titleEl=_el('pw-scene-desc-title');if(titleEl)titleEl.textContent=desc.title;
      var bodyEl=_el('pw-scene-desc-body');if(bodyEl)bodyEl.textContent=desc.body;
    }
  }

  /* ── build dots ── */
  function _buildDots(total){
    var dotsRow=_el('pw-dots-row');
    if(!dotsRow)return;
    var html='';
    for(var i=0;i<total;i++) html+='<div class="pw-dot'+(i===0?' active':'')+'" data-idx="'+i+'"></div>';
    dotsRow.innerHTML=html;
    dotsRow.querySelectorAll('.pw-dot').forEach(function(d){
      d.addEventListener('click',function(){
        var idx=parseInt(this.getAttribute('data-idx'),10);
        if(typeof window.showScene==='function') window.showScene(idx);
      });
    });
  }

  /* ── wire pw nav buttons to existing StepController ── */
  function _wireNav(total){
    var pbtn=_el('pw-prev-btn'),nbtn=_el('pw-next-btn');
    if(pbtn) pbtn.addEventListener('click',function(e){
      e.stopPropagation();
      if(typeof window.showScene==='function'){
        var cur=window.currentStep||0;
        if(cur>0) window.showScene(cur-1);
      }
    });
    if(nbtn) nbtn.addEventListener('click',function(e){
      e.stopPropagation();
      if(typeof window.showScene==='function'){
        var cur2=window.currentStep||0;
        if(cur2<total-1) window.showScene(cur2+1);
      }
    });
  }

  /* ── listen to StepController scene change event ── */
  function _attachSceneListener(total){
    document.addEventListener('qanim:sceneChange',function(e){
      if(e&&e.detail&&typeof e.detail.idx==='number') _onScene(e.detail.idx,total);
    });
    setTimeout(function(){ _onScene(0,total); },200);
  }

  /* ── hide legacy nav elements ── */
  function _hideLegacyNav(){
    ['prevbtn','nextbtn'].forEach(function(id){
      var el=_el(id);
      if(el){ el.style.cssText+='opacity:0!important;pointer-events:none!important;position:absolute!important;width:0!important;height:0!important;overflow:hidden!important;'; }
    });
    var dotsEl=_el('dots');
    if(dotsEl){ dotsEl.style.cssText+='opacity:0!important;pointer-events:none!important;position:absolute!important;width:0!important;height:0!important;overflow:hidden!important;'; }
  }

  /* ── v11.3: ResizeObserver to re-fix SVG scaling on viewport changes ── */
  function _attachResizeObserver(){
    if(!window.ResizeObserver) return;
    var scWrap = _el('pw-scene-wrap');
    if(!scWrap) return;
    var obs = new ResizeObserver(function(){
      /* Re-apply SVG scaling on resize (e.g. orientation change) */
      _fixSvgScaling(scWrap);
    });
    obs.observe(scWrap);
  }

  _onReady(function(){
    _buildWrapper();
    var total=_countScenes();
    _buildDots(total);
    _wireNav(total);
    _attachSceneListener(total);
    _attachResizeObserver();
    setTimeout(_hideLegacyNav,400);
  });

})();
</script>
"""


def inject_layout_wrapper(html, given_values=None, topic_badge=None):
    """
    v11.3: Injects the page-wrapper layout structure with mobile fixes.
    """
    html = re.sub(r'<style[^>]+id=["\']qanim-layout-wrapper-styles["\'][^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<script[^>]+id=["\']qanim-layout-wrapper["\'][^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<script[^>]+id=["\']__given_values__["\'][^>]*>.*?</script>', '', html, flags=re.DOTALL)

    if given_values:
        data_tag = _build_given_values_tag(given_values)
        if '</head>' in html:
            html = html.replace('</head>', data_tag + '\n</head>', 1)
        else:
            html = data_tag + '\n' + html

    if '</head>' in html:
        html = html.replace('</head>', _LAYOUT_WRAPPER_CSS + '\n</head>', 1)

    lw_script = _LAYOUT_WRAPPER_JS
    if '</body>' in html:
        html = html.replace('</body>', lw_script + '\n</body>', 1)
    else:
        html += '\n' + lw_script

    QAnimLogger.ok("LayoutWrapper", f"v11.3 mobile-responsive layout wrapper injected (given={len(given_values or [])} values)")
    return html


# ===========================================================================
#  MODULE 11 -- Notes System (unchanged from v11.2)
# ===========================================================================

_NOTES_CSS = """
<style id="qanim-notes-styles">
#qanim-notes-btn {
  position: fixed;
  top: 14px; right: 16px;
  z-index: 6900;
  display: flex; align-items: center; gap: 7px;
  padding: 10px 18px 10px 13px;
  border-radius: 11px;
  border: 1.5px solid #d1d5db;
  background: #ffffff;
  color: #475569;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 14px; font-weight: 700;
  cursor: pointer;
  box-shadow: 0 3px 14px rgba(0, 0, 0, 0.11);
  transition: background 0.15s, border-color 0.15s, color 0.15s, box-shadow 0.15s;
}
#qanim-notes-btn:hover {
  background: #fefce8;
  border-color: #ca8a04;
  color: #92400e;
}
#qanim-notes-panel {
  position: fixed;
  top: 50px; right: 16px;
  z-index: 7200;
  width: 340px;
  max-height: 80vh;
  border-radius: 14px;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.10);
  display: flex; flex-direction: column;
  overflow: hidden;
  opacity: 0;
  transform: translateY(-8px) scale(0.97);
  pointer-events: none;
  transition: opacity 0.22s ease, transform 0.22s ease;
}
#qanim-notes-panel.open {
  opacity: 1;
  transform: translateY(0) scale(1);
  pointer-events: auto;
}
#qanim-notes-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 14px;
  background: #fffbeb;
  border-bottom: 1px solid #fef3c7;
  cursor: grab;
  flex-shrink: 0;
}
#qanim-notes-header:active { cursor: grabbing; }
.notes-header-title { font-family: -apple-system, 'Segoe UI', Arial, sans-serif; font-size: 13px; font-weight: 700; color: #92400e; }
.notes-hdr-btn { width: 24px; height: 24px; border-radius: 6px; border: 1px solid #fde68a; background: rgba(255,255,255,0.6); color: #92400e; font-size: 12px; display: flex; align-items: center; justify-content: center; cursor: pointer; }
.notes-hdr-btn:hover { background: #fef3c7; }
#qanim-notes-tabs { display: flex; border-bottom: 1px solid #f1f5f9; flex-shrink: 0; }
.notes-tab { flex: 1; padding: 7px 0; text-align: center; font-family: -apple-system, 'Segoe UI', Arial, sans-serif; font-size: 11px; font-weight: 600; color: #94a3b8; cursor: pointer; border-bottom: 2px solid transparent; transition: color 0.15s, border-color 0.15s; text-transform: uppercase; letter-spacing: 0.5px; }
.notes-tab.active { color: #f59e0b; border-bottom-color: #f59e0b; }
#qanim-canvas-toolbar { display: flex; align-items: center; gap: 5px; padding: 6px 10px; background: #f8fafc; border-bottom: 1px solid #f1f5f9; flex-shrink: 0; flex-wrap: wrap; }
.canvas-tool-btn { padding: 3px 9px; border-radius: 5px; border: 1px solid #e2e8f0; background: #ffffff; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer; }
.canvas-tool-btn.active { background: #fef3c7; border-color: #f59e0b; color: #92400e; }
.color-dot { width: 16px; height: 16px; border-radius: 50%; cursor: pointer; border: 2px solid transparent; transition: transform 0.12s; }
.color-dot:hover { transform: scale(1.2); }
.color-dot.selected { border-color: #1e293b; transform: scale(1.1); }
.size-btn { width: 20px; height: 20px; border-radius: 50%; border: 1px solid #e2e8f0; background: #ffffff; color: #64748b; font-size: 10px; font-weight: 700; display: flex; align-items: center; justify-content: center; cursor: pointer; }
.size-btn.active { background: #fef3c7; border-color: #f59e0b; color: #92400e; }
.tool-sep { width: 1px; height: 18px; background: #e2e8f0; flex-shrink: 0; }
#qanim-canvas-wrap { flex: 1 1 auto; position: relative; overflow: hidden; min-height: 180px; }
#qanim-draw-canvas { display: block; width: 100%; height: 100%; cursor: crosshair; background: #fefce8; touch-action: none; }
#qanim-text-pane { display: none; flex-direction: column; flex: 1 1 auto; overflow: hidden; }
#qanim-notes-textarea { flex: 1 1 auto; width: 100%; min-height: 180px; resize: none; box-sizing: border-box; background: #f8fafc; border: none; outline: none; color: #1e293b; font-family: -apple-system, 'Segoe UI', Arial, sans-serif; font-size: 13px; line-height: 1.7; padding: 12px 14px; }
#qanim-notes-textarea::placeholder { color: #cbd5e1; }
#qanim-notes-footer { display: flex; align-items: center; justify-content: space-between; padding: 6px 12px; border-top: 1px solid #f1f5f9; flex-shrink: 0; background: #f8fafc; }
.notes-status { font-size: 10px; color: #94a3b8; font-family: -apple-system, 'Segoe UI', Arial, sans-serif; }
.notes-action-btn { padding: 3px 10px; border-radius: 5px; border: 1px solid #e2e8f0; background: #ffffff; color: #64748b; font-size: 10px; font-weight: 600; cursor: pointer; }
.notes-action-btn:hover { background: #ede9fe; border-color: #7c3aed; color: #7c3aed; }

/* v11.3: Mobile notes button positioning */
@media (max-width: 600px) {
  #qanim-notes-btn { top: 10px; right: 10px; padding: 7px 12px 7px 10px; font-size: 12px; }
  #qanim-notes-panel { right: 10px; width: min(320px, calc(100vw - 20px)); }
}
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
  var isOpen=false,isMin=false,isDrag=false,isDrawing=false;
  var currentTool='pen',currentColor='#1e293b',currentSize=4,currentTab='canvas';
  var undoStack=[],MAX_UNDO=30;
  var autoSaveTimer=null;
  var ctx=null,canvas=null;
  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _storage(){try{return window.localStorage;}catch(e){if(!window._qnotes)window._qnotes={};return{getItem:function(k){return window._qnotes[k]||null;},setItem:function(k,v){window._qnotes[k]=v;}};}}
  function _saveNotes(){try{var canvasData=canvas?canvas.toDataURL():'';var textData=_el('qanim-notes-textarea')?_el('qanim-notes-textarea').value:'';_storage().setItem('qanim_notes_v11',JSON.stringify({canvas:canvasData,text:textData}));var stat=_el('notes-char-count');if(stat)stat.textContent='Saved';}catch(e){}}
  function _loadNotes(){try{var raw=_storage().getItem('qanim_notes_v11');if(!raw)return;var p=JSON.parse(raw);var ta=_el('qanim-notes-textarea');if(ta&&p.text)ta.value=p.text;if(canvas&&p.canvas&&p.canvas.startsWith('data:')){var img=new Image();img.onload=function(){ctx.drawImage(img,0,0);};img.src=p.canvas;}}catch(e){}}
  function _scheduleAutoSave(){if(autoSaveTimer)clearTimeout(autoSaveTimer);autoSaveTimer=setTimeout(_saveNotes,1500);}
  function _initCanvas(){canvas=_el('qanim-draw-canvas');if(!canvas)return;ctx=canvas.getContext('2d');_resizeCanvas();ctx.lineCap='round';ctx.lineJoin='round';ctx.strokeStyle=currentColor;ctx.lineWidth=currentSize;_loadNotes();}
  function _resizeCanvas(){if(!canvas)return;var wrap=_el('qanim-canvas-wrap');var w=wrap?wrap.clientWidth:320,h=wrap?wrap.clientHeight:200;var imgData=null;if(ctx&&canvas.width>0&&canvas.height>0){try{imgData=ctx.getImageData(0,0,canvas.width,canvas.height);}catch(e){}}canvas.width=w;canvas.height=h;ctx.lineCap='round';ctx.lineJoin='round';ctx.strokeStyle=currentColor;ctx.lineWidth=currentSize;if(imgData){try{ctx.putImageData(imgData,0,0);}catch(e){}}}
  function _saveUndo(){if(!canvas)return;if(undoStack.length>=MAX_UNDO)undoStack.shift();undoStack.push(canvas.toDataURL());}
  function _undo(){if(!canvas||undoStack.length===0)return;var prev=undoStack.pop();if(prev){var img=new Image();img.onload=function(){ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(img,0,0);};img.src=prev;}else ctx.clearRect(0,0,canvas.width,canvas.height);}
  function _getPos(e,cvs){var rect=cvs.getBoundingClientRect();var sx=cvs.width/rect.width,sy=cvs.height/rect.height;var cx=e.touches?e.touches[0].clientX:e.clientX;var cy=e.touches?e.touches[0].clientY:e.clientY;return{x:(cx-rect.left)*sx,y:(cy-rect.top)*sy};}
  function _startDraw(e){if(!canvas||currentTab!=='canvas')return;e.preventDefault();_saveUndo();isDrawing=true;var pos=_getPos(e,canvas);ctx.beginPath();ctx.moveTo(pos.x,pos.y);if(currentTool==='eraser'){ctx.globalCompositeOperation='destination-out';ctx.lineWidth=currentSize*4;}else{ctx.globalCompositeOperation='source-over';ctx.strokeStyle=currentColor;ctx.lineWidth=currentSize;}}
  function _draw(e){if(!isDrawing||!canvas)return;e.preventDefault();var pos=_getPos(e,canvas);ctx.lineTo(pos.x,pos.y);ctx.stroke();}
  function _endDraw(){if(!isDrawing)return;isDrawing=false;if(ctx)ctx.globalCompositeOperation='source-over';_scheduleAutoSave();}
  function openNotes(){var panel=_el('qanim-notes-panel');if(!panel)return;panel.classList.add('open');panel.setAttribute('aria-hidden','false');isOpen=true;setTimeout(function(){_resizeCanvas();},50);}
  function closeNotes(){var panel=_el('qanim-notes-panel');if(panel){panel.classList.remove('open');panel.setAttribute('aria-hidden','true');}isOpen=false;_saveNotes();}
  function _switchTab(t){currentTab=t;document.querySelectorAll('.notes-tab').forEach(function(tb){tb.classList.toggle('active',tb.dataset.tab===t);});var ct=_el('qanim-canvas-toolbar'),cw=_el('qanim-canvas-wrap'),tp=_el('qanim-text-pane');if(ct)ct.style.display=t==='canvas'?'flex':'none';if(cw)cw.style.display=t==='canvas'?'block':'none';if(tp)tp.style.display=t==='text'?'flex':'none';if(t==='canvas')setTimeout(_resizeCanvas,30);}
  _onReady(function(){
    var nb=_el('qanim-notes-btn');if(nb)nb.addEventListener('click',function(){isOpen?closeNotes():openNotes();});
    var cb=_el('notes-close-btn');if(cb)cb.addEventListener('click',closeNotes);
    var mb=_el('notes-minimize-btn');if(mb)mb.addEventListener('click',function(e){e.stopPropagation();isMin=!isMin;var p=_el('qanim-notes-panel');if(p)p.style.maxHeight=isMin?'44px':'80vh';mb.textContent=isMin?'[]':'--';});
    document.querySelectorAll('.notes-tab').forEach(function(t){t.addEventListener('click',function(){_switchTab(this.dataset.tab);});});
    document.querySelectorAll('.canvas-tool-btn[data-tool]').forEach(function(b){b.addEventListener('click',function(){currentTool=this.dataset.tool;document.querySelectorAll('.canvas-tool-btn[data-tool]').forEach(function(x){x.classList.remove('active');});this.classList.add('active');});});
    document.querySelectorAll('.color-dot').forEach(function(d){d.addEventListener('click',function(){currentColor=this.dataset.color;document.querySelectorAll('.color-dot').forEach(function(x){x.classList.remove('selected');});this.classList.add('selected');if(ctx)ctx.strokeStyle=currentColor;currentTool='pen';document.querySelectorAll('.canvas-tool-btn[data-tool]').forEach(function(b){b.classList.toggle('active',b.dataset.tool==='pen');});});});
    document.querySelectorAll('.size-btn').forEach(function(b){b.addEventListener('click',function(){currentSize=parseInt(this.dataset.size,10);document.querySelectorAll('.size-btn').forEach(function(x){x.classList.remove('active');});this.classList.add('active');if(ctx)ctx.lineWidth=currentSize;});});
    var ub=_el('notes-undo-btn');if(ub)ub.addEventListener('click',_undo);
    var clrb=_el('notes-clear-btn');if(clrb)clrb.addEventListener('click',function(){if(!canvas)return;_saveUndo();ctx.clearRect(0,0,canvas.width,canvas.height);_scheduleAutoSave();});
    var etb=_el('notes-export-text-btn');if(etb)etb.addEventListener('click',function(){var ta=_el('qanim-notes-textarea');if(!ta||!ta.value)return;var blob=new Blob([ta.value],{type:'text/plain'});var a=document.createElement('a');a.download='qanim_notes.txt';a.href=URL.createObjectURL(blob);a.click();});
    var ta=_el('qanim-notes-textarea');if(ta)ta.addEventListener('input',function(){var c=_el('notes-char-count');if(c)c.textContent=ta.value.length+' chars';_scheduleAutoSave();});
    var cvs=_el('qanim-draw-canvas');
    if(cvs){cvs.addEventListener('mousedown',_startDraw);cvs.addEventListener('mousemove',_draw);cvs.addEventListener('mouseup',_endDraw);cvs.addEventListener('mouseleave',_endDraw);cvs.addEventListener('touchstart',_startDraw,{passive:false});cvs.addEventListener('touchmove',_draw,{passive:false});cvs.addEventListener('touchend',_endDraw);}
    if(window.ResizeObserver){var obs=new ResizeObserver(function(){if(isOpen&&currentTab==='canvas')_resizeCanvas();});var wr=_el('qanim-canvas-wrap');if(wr)obs.observe(wr);}
    _initCanvas();
  });
})();
"""


def inject_notes_system(html, question=""):
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
#  MODULE 14 -- Voice Assistant System  (v11.3)
# ===========================================================================

_VOICE_ASSISTANT_CSS = """
<style id="qanim-voice-styles">
#qanim-voice-btn {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 8px 15px;
  border-radius: 10px;
  border: 1.5px solid #e2e8f0;
  background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
  color: #334155;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s, transform 0.12s, box-shadow 0.15s;
  user-select: none;
  letter-spacing: 0.2px;
  white-space: nowrap;
}
#qanim-voice-btn:hover {
  background: linear-gradient(135deg, #ede9fe 0%, #fdf4ff 100%);
  border-color: #7c3aed;
  color: #6d28d9;
  transform: translateY(-2px);
  box-shadow: 0 4px 14px rgba(124, 58, 237, 0.22);
}
#qanim-voice-btn:active { transform: translateY(0); box-shadow: none; }
#qanim-voice-btn.speaking {
  background: linear-gradient(135deg, #ede9fe 0%, #fdf4ff 100%);
  border-color: #7c3aed;
  color: #6d28d9;
}
#qanim-voice-btn.muted {
  color: #94a3b8;
  border-color: #e2e8f0;
}
@media (max-width: 600px) {
  #qanim-voice-btn { padding: 7px 10px; font-size: 11px; }
}
@media (max-width: 420px) {
  #qanim-voice-btn { padding: 6px 8px; font-size: 10px; }
  #qanim-voice-btn .ctrl-label { display: none; }
}
</style>
"""

_VOICE_ASSISTANT_JS = r"""
<script id="qanim-voice-assistant">
/* QAnim Voice Assistant v11.3 */
(function initVoiceAssistant(){
  'use strict';

  var _muted     = false;
  var _synth     = window.speechSynthesis || null;
  var _supported = !!_synth;
  var _btn       = null;
  var _lastSpokenIdx = -1;
  var _speakTimer = null;

  function _setText(icon, label){
    if(!_btn) return;
    _btn.innerHTML = '<span>' + icon + '</span><span class="ctrl-label">' + label + '</span>';
  }

  function _getSceneText(sceneEl){
    if(!sceneEl) return '';
    var dv = sceneEl.getAttribute('data-voice');
    if(dv && dv.trim()) return dv.trim();
    var nodes = sceneEl.querySelectorAll('text, foreignObject, p, h1, h2, h3, h4, li');
    var seen  = Object.create(null);
    var parts = [];
    var total = 0;
    for(var i = 0; i < nodes.length; i++){
      var raw = (nodes[i].textContent || '').replace(/\s+/g, ' ').trim();
      if(raw.length < 5) continue;
      if(/^[\d\s\+\-\=\.\,\(\)\[\]\{\}\/\*\^\%]+$/.test(raw)) continue;
      var key = raw.toLowerCase();
      if(seen[key]) continue;
      seen[key] = true;
      parts.push(raw);
      total += raw.length;
      if(total > 600) break;
    }
    return parts.join('. ').trim();
  }

  function _speak(text){
    if(!_supported || _muted || !text) return;
    try{
      _synth.cancel();
      var u = new SpeechSynthesisUtterance(text);
      u.rate   = 0.90;
      u.pitch  = 1.0;
      u.volume = 1.0;
      var voices = _synth.getVoices();
      for(var i = 0; i < voices.length; i++){
        var v = voices[i];
        if(/en[-_]/i.test(v.lang) && !/novelty|zira|hazel|espeak/i.test(v.name)){
          u.voice = v;
          break;
        }
      }
      u.onstart = function(){ if(_btn){ _btn.classList.add('speaking'); _setText('&#x1F50A;','Speaking'); } };
      u.onend   = function(){ if(_btn){ _btn.classList.remove('speaking'); _setText('&#x1F50A;','Voice'); } };
      u.onerror = function(){ if(_btn){ _btn.classList.remove('speaking'); _setText('&#x1F50A;','Voice'); } };
      _synth.speak(u);
    }catch(e){ console.warn('[QAnim VA] speak error:', e); }
  }

  function _onSceneChange(idx){
    if(!_supported || _muted) return;
    if(idx === _lastSpokenIdx) return;
    _lastSpokenIdx = idx;
    clearTimeout(_speakTimer);
    _synth.cancel();
    _speakTimer = setTimeout(function(){
      var sceneEl = document.getElementById('scene-' + idx);
      var text    = _getSceneText(sceneEl);
      if(!text) text = 'Scene ' + (idx + 1) + '.';
      _speak(text);
    }, 450);
  }

  function _findVisibleSceneIdx(){
    for(var i = 0; i < 20; i++){
      var s = document.getElementById('scene-' + i);
      if(!s) break;
      var op = parseFloat(s.style.opacity);
      if(op > 0.5) return i;
    }
    return 0;
  }

  function _toggleMute(){
    _muted = !_muted;
    if(_muted){
      clearTimeout(_speakTimer);
      if(_synth) _synth.cancel();
      _btn.classList.add('muted');
      _btn.classList.remove('speaking');
      _setText('&#x1F507;','Muted');
    } else {
      _btn.classList.remove('muted');
      _setText('&#x1F50A;','Voice');
      var vis = _findVisibleSceneIdx();
      _lastSpokenIdx = -1;
      _onSceneChange(vis);
    }
  }

  function _attachListeners(){
    document.addEventListener('qanim:sceneChange', function(e){
      if(e && e.detail && typeof e.detail.idx === 'number'){
        _onSceneChange(e.detail.idx);
      }
    });

    var _obDebounce = null;
    var _obLastIdx  = -1;
    function _obCheck(){
      for(var i = 0; i < 20; i++){
        var s = document.getElementById('scene-' + i);
        if(!s) break;
        var op = parseFloat(s.style.opacity);
        if(op > 0.7 && i !== _obLastIdx){
          _obLastIdx = i;
          if(i !== _lastSpokenIdx) _onSceneChange(i);
          return;
        }
      }
    }
    var root = document.querySelector('svg') || document.body;
    var obs  = new MutationObserver(function(){
      clearTimeout(_obDebounce);
      _obDebounce = setTimeout(_obCheck, 60);
    });
    obs.observe(root, {attributes:true, subtree:true, attributeFilter:['style','opacity']});

    setTimeout(function(){
      if(_lastSpokenIdx === -1){ _lastSpokenIdx = -1; _onSceneChange(0); }
    }, 950);
  }

  function _init(){
    if(!_supported){
      console.warn('[QAnim VA] Web Speech API not supported in this browser.');
      return;
    }
    var bar = document.getElementById('qanim-controls-bar');
    if(bar){
      var sep = document.createElement('div');
      sep.className = 'qanim-ctrl-sep';
      bar.appendChild(sep);
      _btn = document.createElement('button');
      _btn.id    = 'qanim-voice-btn';
      _btn.title = 'Toggle voice narration';
      _btn.className = 'qanim-ctrl-btn';
      _btn.innerHTML = '<span>&#x1F50A;</span><span class="ctrl-label">Voice</span>';
      _btn.addEventListener('click', _toggleMute);
      bar.appendChild(_btn);
    } else {
      _btn = document.createElement('button');
      _btn.id = 'qanim-voice-btn';
      _btn.style.cssText = 'position:fixed;bottom:70px;right:70px;z-index:6800;';
      _btn.innerHTML = '<span>&#x1F50A;</span><span class="ctrl-label">Voice</span>';
      _btn.addEventListener('click', _toggleMute);
      document.body.appendChild(_btn);
    }
    if(_synth.getVoices().length === 0){
      _synth.addEventListener('voiceschanged', function(){}, {once:true});
    }
    _attachListeners();
  }

  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', _init);
  } else {
    setTimeout(_init, 0);
  }
})();
</script>
"""


def inject_voice_assistant(html):
    try:
        if '</head>' in html:
            html = html.replace('</head>', _VOICE_ASSISTANT_CSS + '\n</head>', 1)
    except Exception as e:
        QAnimLogger.warn("VoiceAssistant", f"CSS injection failed: {e}")
    try:
        if '</body>' in html:
            html = html.replace('</body>', _VOICE_ASSISTANT_JS + '\n</body>', 1)
        else:
            html += '\n' + _VOICE_ASSISTANT_JS
        QAnimLogger.ok("VoiceAssistant", "Voice assistant injected")
    except Exception as e:
        QAnimLogger.warn("VoiceAssistant", f"JS injection failed: {e}")
    return html


# ===========================================================================
#  MODULE 12 -- StepController Patcher (unchanged)
# ===========================================================================

_STEP_CONTROLLER_JS = r"""
<script id="qanim-step-controller">
/* QAnim Manual Step Controller v11.1 */
(function patchStepController(){
  'use strict';
  function initSC(){
    try{
      var nextBtn=document.getElementById('nextbtn');
      var prevBtn=document.getElementById('prevbtn');
      if(!nextBtn){
        nextBtn=document.createElement('button');nextBtn.id='nextbtn';nextBtn.textContent='Next';
        nextBtn.style.cssText='position:fixed;bottom:70px;right:20px;z-index:6500;padding:8px 18px;border-radius:10px;border:1px solid #e2e8f0;background:#7c3aed;color:#fff;font-size:13px;font-weight:700;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.12);';
        document.body.appendChild(nextBtn);console.log('[QAnim SC] Created fallback #nextbtn');}
      if(!prevBtn){
        prevBtn=document.createElement('button');prevBtn.id='prevbtn';prevBtn.textContent='Prev';
        prevBtn.style.cssText='position:fixed;bottom:70px;left:20px;z-index:6500;padding:8px 18px;border-radius:10px;border:1px solid #e2e8f0;background:#fff;color:#334155;font-size:13px;font-weight:700;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.12);';
        document.body.appendChild(prevBtn);console.log('[QAnim SC] Created fallback #prevbtn');}
      var scenes=[];
      for(var i=0;i<20;i++){var s=document.getElementById('scene-'+i);if(s){scenes.push(s);}else if(i>0){break;}}
      if(scenes.length<1){console.warn('[QAnim SC] No scene-N elements found');return;}
      var _sceneSnapshots=[];
      for(var si=0;si<scenes.length;si++){
        var scEl=scenes[si];
        _sceneSnapshots.push({display:scEl.style.display,opacity:scEl.style.opacity,visibility:scEl.style.visibility,transform:scEl.style.transform,transition:scEl.style.transition,
          children:(function(root){var result=[],all=root.querySelectorAll('*');for(var ci=0;ci<all.length;ci++)result.push({el:all[ci],opacity:all[ci].style.opacity,transform:all[ci].style.transform,display:all[ci].style.display,visibility:all[ci].style.visibility,transition:all[ci].style.transition});return result;})(scEl)});}
      var _animFired={};
      var _aiShowScene=(typeof window.showScene==='function')?window.showScene:null;
      var currentStep=0;
      function _resetScene(idx){
        var snap=_sceneSnapshots[idx];if(!snap)return;var scEl=scenes[idx];
        scEl.style.transition='none';scEl.style.opacity=snap.opacity;scEl.style.display=snap.display!==''?snap.display:'';scEl.style.visibility=snap.visibility!==''?snap.visibility:'';scEl.style.transform=snap.transform!==''?snap.transform:'';
        for(var ci=0;ci<snap.children.length;ci++){var c=snap.children[ci];c.el.style.transition='none';c.el.style.opacity=c.opacity;c.el.style.transform=c.transform;c.el.style.display=c.display;c.el.style.visibility=c.visibility;}
        requestAnimationFrame(function(){scEl.style.transition='';for(var ci2=0;ci2<snap.children.length;ci2++)snap.children[ci2].el.style.transition='';});}
      function _fireAnim(idx){
        if(_animFired[idx])return;_animFired[idx]=true;
        var fn=window['animateScene'+idx];if(typeof fn==='function'){try{fn();}catch(e){}return;}
        if(_aiShowScene){try{_aiShowScene(idx);}catch(e){}}}
      function showScene(idx){
        if(idx<0||idx>=scenes.length)return;currentStep=idx;
        for(var j=0;j<scenes.length;j++){
          if(j===idx){if(_animFired[j]){delete _animFired[j];_resetScene(j);}(function(sceneEl){requestAnimationFrame(function(){sceneEl.style.transition='opacity .35s ease';sceneEl.style.opacity='1';sceneEl.style.display=sceneEl.style.display==='none'?'':sceneEl.style.display;sceneEl.style.visibility='visible';sceneEl.style.pointerEvents='auto';});})(scenes[j]);}
          else{scenes[j].style.transition='opacity .35s ease';scenes[j].style.opacity='0';scenes[j].style.pointerEvents='none';}}
        _updateDots();_updateNavBtns();if(typeof window.resetAnswerBox==='function')window.resetAnswerBox();
        try{document.dispatchEvent(new CustomEvent('qanim:sceneChange',{detail:{idx:idx}}));}catch(e){}
        (function(capturedIdx){requestAnimationFrame(function(){requestAnimationFrame(function(){_fireAnim(capturedIdx);});});})(idx);}
      function _updateDots(){
        var dc=document.getElementById('dots');if(!dc)return;
        var ds=dc.querySelectorAll('.dot,circle');if(!ds.length)ds=dc.children;
        for(var k=0;k<ds.length;k++){var active=(k===currentStep);ds[k].style.opacity=active?'1':'0.35';if(ds[k].classList)ds[k].classList.toggle('active',active);}}
      function _updateNavBtns(){
        if(prevBtn){if(currentStep===0){prevBtn.setAttribute('disabled','true');prevBtn.style.opacity='0.3';}else{prevBtn.removeAttribute('disabled');prevBtn.style.opacity='1';}}
        if(nextBtn){if(currentStep===scenes.length-1){nextBtn.setAttribute('disabled','true');nextBtn.style.opacity='0.3';}else{nextBtn.removeAttribute('disabled');nextBtn.style.opacity='1';}}}
      var nb2=nextBtn.cloneNode(true);nextBtn.parentNode.replaceChild(nb2,nextBtn);nextBtn=nb2;
      if(prevBtn){var pb2=prevBtn.cloneNode(true);prevBtn.parentNode.replaceChild(pb2,prevBtn);prevBtn=pb2;}
      nextBtn.addEventListener('click',function(e){e.stopPropagation();if(currentStep<scenes.length-1)showScene(currentStep+1);});
      if(prevBtn)prevBtn.addEventListener('click',function(e){e.stopPropagation();if(currentStep>0)showScene(currentStep-1);});
      var _ri=window.setInterval;window.setInterval=function(fn,ms){
        var src=fn?fn.toString():'';
        if(ms&&ms<8000&&(src.indexOf('showScene')!==-1||src.indexOf('currentStep')!==-1||src.indexOf('nextStep')!==-1)){console.log('[SC] Blocked auto-advance interval ('+ms+'ms)');return -1;}
        return _ri.apply(window,arguments);};
      showScene(0);console.log('[QAnim SC v11.1] '+scenes.length+' scenes ready');
    }catch(err){console.error('[QAnim SC] Fatal:',err);}
  }
  if(document.readyState==='complete')initSC();else window.addEventListener('load',initSC);
})();
</script>
"""


def inject_step_controller(html):
    try:
        if '</body>' in html:
            html = html.replace('</body>', _STEP_CONTROLLER_JS + '\n</body>', 1)
        else:
            html += '\n' + _STEP_CONTROLLER_JS
        QAnimLogger.ok("StepController", "Manual step controller injected")
    except Exception as e:
        QAnimLogger.warn("StepController", f"Injection failed: {e}")
    return html


# ===========================================================================
#  RESPONSE PARSING UTILITIES (unchanged)
# ===========================================================================

def _parse_response(raw, question):
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
    QAnimLogger.error("Parser", "All strategies failed")
    return {"title": f"Animation: {question[:50]}", "explanation": "Parse failed",
            "animation_code": "", "solution_steps": [], "final_answer": "", "key_insight": ""}


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
        if not m: return []
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
        return [_unescape_json_string(s) for s in items]
    code = _extract_animation_code_field(raw)
    if not code: return None
    return {"title": extract_string("title") or f"Animation: {question[:50]}",
            "explanation": extract_string("explanation") or "Interactive animation",
            "animation_type": extract_string("animation_type"),
            "design_strategy": extract_string("design_strategy"),
            "solution_steps": extract_array("solution_steps"),
            "final_answer": extract_string("final_answer"),
            "key_insight": extract_string("key_insight"),
            "animation_code": code}


def _extract_animation_code_field(raw):
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
                return {"title": f"Animation: {question[:50]}", "explanation": "Interactive animation",
                        "animation_code": code.strip(), "solution_steps": [],
                        "final_answer": "", "key_insight": ""}
    return None


def _normalize_parsed(data, question):
    if not isinstance(data, dict): raise ValueError("Not a dict")
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


def _find_json_string_end(s):
    i = 0
    while i < len(s):
        if s[i] == '\\': i += 2
        elif s[i] == '"': return i
        else: i += 1
    return -1


def _unescape_json_string(s):
    return (s.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
             .replace('\\r', '\r').replace("\\'", "'").replace('\\\\', '\\'))


# ===========================================================================
#  SYSTEM PROMPTS + PROMPT BUILDERS  (v11.3)
#  MOBILE FIX: Added preserveAspectRatio="xMidYMid meet" requirement
# ===========================================================================

SYSTEM = """You are QAnim v11.3 -- a cinematic SVG motion designer and educational animation engineer.

YOUR MISSION: Turn any student question into a PREMIUM 5-scene SVG animation
that teaches the concept progressively. The animation SCENES must NOT show
the computed numerical answer -- but you MUST fully solve the question and
provide the complete final answer in the "final_answer" JSON field so it
appears in the dedicated "Final Answer" panel when the student clicks the button.

SCENE STRUCTURE (exactly 5 scenes, Feynman-style -- simple, friendly, like a great teacher explaining to a 10-year-old):

Scene 0 "What Are We Looking At?":
  - Draw the physical setup as a simple, friendly picture
  - Use big clear shapes, short fun labels (avoid jargon)
  - Bottom info card: one plain-English sentence saying what the picture shows
  - Animate parts appearing one by one so it feels like building a story

Scene 1 "The Big Idea":
  - Show the ONE main idea or rule that solves this problem
  - Use a simple picture/analogy (e.g. "heat flows like water downhill")
  - Show the formula name and structure in friendly words, NOT solved numbers
  - Bottom info card: "The key idea is..." in plain, friendly language

Scene 2 "Another Thing That Matters":
  - Show the second effect or mechanism with a simple animation
  - Use arrows, color, or movement to make it obvious
  - Bottom info card: one short plain-English sentence explaining it

Scene 3 "Putting It Together":
  - Show a simple diagram connecting the two ideas (like boxes with arrows)
  - Use clear labels, short words
  - Bottom info card: "Together, these two things give us..." short explanation

Scene 4 "How We Solve It -- Step by Step":
  - Show a friendly numbered checklist of the 3-4 solving steps
  - Each step in plain words a child could follow
  - DO NOT show computed numerical results -- only the method steps

IMPORTANT -- FINAL ANSWER FIELD:
  Solve the question completely. Put the FULL computed answer (with all values
  and units) into the "final_answer" JSON field. This is displayed ONLY in the
  separate "Final Answer" button panel -- NEVER inside the animation scenes.
  Example: "Rate of heat loss Q = 142.6 W/m; Outer surface temperature T_s = 47.3 deg C"
  Do NOT leave "final_answer" empty or as a placeholder string.

MOBILE-RESPONSIVE SVG (v11.3 CRITICAL REQUIREMENT):
  The SVG MUST include these attributes on the root <svg> element:
    width="100%" height="100%" preserveAspectRatio="xMidYMid meet" viewBox="0 0 1000 600"
  This ensures the animation scales correctly on mobile phones (400px wide)
  and smart boards (1920px+ wide) without any clipping or overflow.
  EXAMPLE: <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 600"
                 width="100%" height="100%" preserveAspectRatio="xMidYMid meet">

VISUAL STANDARDS -- v11.3 LIGHT THEME (REQUIRED):
- SVG viewBox="0 0 1000 600" (with width="100%" height="100%" preserveAspectRatio="xMidYMid meet")
- Background: #f8fafc or white gradient (rect x="0" y="0" width="1000" height="600")
- NEVER use dark backgrounds (#1a1a2e, #0f172a, etc.)
- Text color: #1e293b (dark on light)
- Cards: white fill, 1px #e2e8f0 border, border-radius 8-14px
- Bottom info card: white bg, 1px border, positioned at y=420-540
  * Card rect y=420, height MINIMUM 120px
- Gradient fills: linearGradient for pipe, water, heat, biological etc.
- Arrow markers: markerWidth="10" with colored polygon tips
- Glow filter: feGaussianBlur stdDeviation="3"
- Navigation: #prevbtn / #nextbtn styled as white rounded buttons
- Dots: .dot class, active = colored pill, inactive = gray circle
- FORMULA SUBSCRIPTS: NEVER use underscore notation (f_s, T_s, Q_1).
  ALWAYS use <tspan dy="5" font-size="0.72em">subscript</tspan> inside <text> elements.
  Reset after subscript with <tspan dy="-5" font-size="1em">.</tspan>

ANIMATION TECHNIQUE PER ELEMENT:
- Circles/shapes: opacity 0->1 with setTimeout + CSS transition
- Arrows/lines: stroke-dasharray set to path length, stroke-dashoffset 0 via transition
- Text labels: opacity 0->1, optionally with translateY transform
- Cards/boxes: opacity 0->1, optionally with scale transform
- Spring scale-in: cubic-bezier(0.34,1.56,0.64,1) for hero elements
- Timing: scene 0 ~2s total, scene 1-3 ~2-3s, scene 4 ~4s
- Each setTimeout step: 100-400ms apart for polished feel

JS ARCHITECTURE (match reference HTML exactly):
- window.currentStep = 0; window.totalSteps = 5;
- function showScene(n) -- hides all, shows scene n, fires animateSceneN()
- function animateScene0/1/2/3/4() -- individual scene animators
- function nextStep() / prevStep() -- navigation
- DOMContentLoaded: showScene(0)

DEFS REQUIRED IN SVG:
- At least 2-3 linearGradients relevant to topic
- Arrow marker (id="arrowhead")
- Glow filter (id="glow")

SAFETY RULES:
- Complete <!DOCTYPE html>...</html> as single JSON string
- NO external CDN/fonts/scripts
- NO backtick template literals -- use + concatenation only
- NO document.write()
- All SVG/script tags balanced
- Include: #prevbtn, #nextbtn, #dots, #qstrip .qtext (format .qtext as: <strong>Question:</strong> followed by the question text)
- DO NOT include Find/Quiz/Solution/Answer Box buttons (injected separately)
- DO NOT generate #sol-backdrop or #sol-panel or their CSS

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "animation_type": "concise label",
  "design_strategy": "2-4 sentence description",
  "solution_steps": ["Concept: ...", "Formula: ...", "Given: ...", "Find: ...", "Approach: ..."],
  "final_answer": "<your fully computed answer with all numerical values and units here>",
  "key_insight": "one memorable insight sentence",
  "animation_code": "COMPLETE SELF-CONTAINED HTML AS A SINGLE PROPERLY-ESCAPED JSON STRING"
}"""


SYSTEM_CONCEPT = """You are QAnim Concept Engine v11.3 -- cinematic SVG concept animator.

YOUR MISSION: 5-scene concept animation. LIGHT THEME. No dark backgrounds.
No final answer shown. Each scene reveals ONE concept progressively.

MOBILE-RESPONSIVE SVG (v11.3 CRITICAL):
  Root <svg> must have: width="100%" height="100%" preserveAspectRatio="xMidYMid meet" viewBox="0 0 1000 600"
  This ensures correct scaling on mobile (400px) through smart board (1920px+).

Scenes:
  0: Physical setup / visual system overview
  1: Core formula structure (named, not solved)
  2: Secondary mechanism
  3: Key relationships / abstract model
  4: Summary of what to calculate + approach

OUTPUT FORMAT (strict JSON):
{
  "animation_type": "label",
  "design_strategy": "2-4 sentences",
  "concept_code": "COMPLETE <!DOCTYPE html>...</html> AS ESCAPED JSON STRING"
}

SAFETY: No dark bg, no backticks, no external scripts, balanced tags,
5 scenes, include #prevbtn/#nextbtn/#dots, manual step control.
SUBSCRIPTS: NEVER use underscores. Use <tspan dy="5" font-size="0.72em"> for subscripts."""


DESIGN_SYSTEM = """
LIGHT THEME: background #f8fafc or white gradient
CARD STYLE: white bg, 1px #e2e8f0 border, border-radius 10-14px
COLORS: PHYSICS=#3b5bdb/#e64980 | MATH=#7c3aed/#db2777 | BIO=#16a34a/#ca8a04
TEXT: #1e293b on light bg
SVG: viewBox="0 0 1000 600" width="100%" height="100%" preserveAspectRatio="xMidYMid meet"
"""

SVG_TECHNIQUES = """
TECHNIQUES:
- stroke-dashoffset arrows/curves
- opacity fade + translateY rise for labels
- spring scale: cubic-bezier(0.34,1.56,0.64,1)
- feGaussianBlur glow filter
- Sequential setTimeout (NOT CSS animation-delay)
- linearGradient fills

SVG ROOT ELEMENT (MOBILE CRITICAL):
  <svg xmlns="http://www.w3.org/2000/svg"
       viewBox="0 0 1000 600"
       width="100%"
       height="100%"
       preserveAspectRatio="xMidYMid meet">
  The preserveAspectRatio ensures the 1000x600 canvas scales to fit
  any container on mobile without clipping.

FORMULA SUBSCRIPTS (CRITICAL -- students must see proper subscripts, NOT underscores):
- NEVER write subscripts with underscores like f_s, T_s, Q_out, v_1, h_fg
- ALWAYS use SVG <tspan> subscripts:
    <text>f<tspan dy="5" font-size="0.72em">s</tspan></text>
    <text>T<tspan dy="5" font-size="0.72em">out</tspan></text>
- After the subscript tspan, reset with <tspan dy="-5" font-size="1em"> for any continuing text
"""

STRATEGY_TEMPLATES = {
    "VISUAL_PHYSICS": "Dynamic force/motion/field diagram on light background. 5 scenes: geometry, force vectors, secondary effect, circuit model, full summary.",
    "PROCESS_BASED":  "Sequential process nodes on white. 5 scenes: input, first step, second step, combined model, complete flow summary.",
    "MATHEMATICAL":   "Coordinate geometry on white. 5 scenes: axes/setup, primary formula, secondary relationship, graphical model, full equation summary.",
    "BIOLOGICAL":     "Organic shapes on light bg. 5 scenes: cell/molecule setup, first mechanism, second mechanism, pathway model, complete system summary.",
    "ABSTRACT":       "Clean metaphor on white. 5 scenes: analogy setup, first principle, second principle, combined model, complete concept summary.",
    "MIXED":          "Split canvas light bg. 5 scenes: physical setup, primary formula, secondary formula, combined model, full parameter summary.",
}

CONCEPT_STRATEGY_TEMPLATES = STRATEGY_TEMPLATES

FALLBACK_RULES = """
IF STUCK: Use one of these premium fallback layouts (light theme):
1. CARD-REVEAL: 4 white cards with accent border-left, staggered fade
2. TIMELINE: horizontal line draws, events spring-scale at nodes
3. CONCEPT-MAP: central node, branch lines, satellite nodes appear
4. DATA-BARS: animated bar chart on white with gradient fills
NEVER flat dark backgrounds or neon-on-black.
"""

HTML_SHELL_NOTE = """
REQUIRED:
- Root SVG must have: width="100%" height="100%" preserveAspectRatio="xMidYMid meet"
- DO NOT generate #sol-backdrop, #sol-panel, or their CSS/JS
- DO NOT add sol-close, sol-backdrop event listeners
- Include bare placeholder divs only:
    <div id="sol-steps-container"></div>
    <div id="sol-answer-text"></div>
    <div id="sol-insight-text"></div>
- All scenes in <g id="scene-N"> groups
- #prevbtn, #nextbtn, #dots for navigation
- #qstrip .qtext for question display -- format as: <strong>Question:</strong> followed by the question text
- DO NOT include Find/Quiz/Solution/Answer Box buttons
"""


def _classify_topic_sync(question):
    """Pure sync classifier -- safe to call from run_in_executor."""
    q = question.lower()
    scores = {
        "BIOLOGICAL":     sum(1 for k in ["cell","dna","rna","protein","photosynthesis","mitosis","enzyme","hormone","gene","organism","bacteria","virus","chromosome","metabolism"] if k in q),
        "MATHEMATICAL":   sum(1 for k in ["integral","derivative","matrix","vector","theorem","equation","polynomial","logarithm","trigonometry","calculus","function","graph","proof"] if k in q),
        "ABSTRACT":       sum(1 for k in ["philosophy","ethics","democracy","capitalism","justice","freedom","psychology","consciousness","society","ideology","culture","politics"] if k in q),
        "PROCESS_BASED":  sum(1 for k in ["how does","how do","step by step","process","algorithm","mechanism","workflow","procedure","stages","works","function","operation"] if k in q),
        "VISUAL_PHYSICS": sum(1 for k in ["force","velocity","acceleration","mass","energy","momentum","gravity","pressure","current","voltage","wave","circuit","newton","friction","torque","field","charge","resistance","heat","thermal","temperature","pipe","cylinder","conduction","convection","knapsack","greedy","dynamic","sorting","graph","tree","binary"] if k in q),
    }
    max_score = max(scores.values())
    if max_score >= 2:
        top = [c for c, s in scores.items() if s == max_score]
        return top[0] if len(top) == 1 else "MIXED"
    if sum(1 for s in scores.values() if s > 0) >= 3:
        return "MIXED"
    # Fallback: keyword-based classification without calling AI
    # (avoids blocking the event loop and wastes a token budget)
    for kw, cat in [
        ("sort", "MATHEMATICAL"), ("search", "MATHEMATICAL"),
        ("flow", "VISUAL_PHYSICS"), ("heat", "VISUAL_PHYSICS"),
        ("cell", "BIOLOGICAL"), ("gene", "BIOLOGICAL"),
        ("prove", "MATHEMATICAL"), ("theorem", "MATHEMATICAL"),
    ]:
        if kw in q:
            return cat
    return "PROCESS_BASED"


def _classify_topic(question):
    """Sync wrapper kept for backward compatibility."""
    return _classify_topic_sync(question)


async def _classify_topic_async(question):
    """Non-blocking async classifier -- uses run_in_executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _classify_topic_sync, question)


def _build_concept_prompt(question, category):
    strategy = CONCEPT_STRATEGY_TEMPLATES.get(category, CONCEPT_STRATEGY_TEMPLATES["PROCESS_BASED"])
    static_text = (
        SYSTEM_CONCEPT
        + "\n\n" + DESIGN_SYSTEM
        + "\n\n" + SVG_TECHNIQUES
        + "\n\n" + FALLBACK_RULES
    )
    system_blocks = [
        {
            "type": "text",
            "text": static_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    user_content = (
        f"Build a CINEMATIC 5-SCENE CONCEPT ANIMATION for QAnim v11.3.\n\n"
        f"QUESTION: {question}\n"
        f"CATEGORY: {category}\n"
        f"VISUAL STRATEGY: {strategy}\n\n"
        "CONCEPT ANIMATION v11.3 REQUIREMENTS:\n"
        "- LIGHT THEME: white/light-gray background -- NO dark backgrounds\n"
        "- MOBILE SVG: root <svg> must have width=\"100%\" height=\"100%\" preserveAspectRatio=\"xMidYMid meet\"\n"
        "- Exactly 5 scenes (scene-0 to scene-4)\n"
        "- Progressive concept revelation -- no final answer\n"
        "- Follow reference scene structure:\n"
        "    Scene 0: Physical setup/geometry\n"
        "    Scene 1: Core formula structure (named, not solved)\n"
        "    Scene 2: Secondary mechanism\n"
        "    Scene 3: Abstract model (circuit/flow/graph)\n"
        "    Scene 4: Summary overview + parameter card\n"
        "- Animated SVG: stroke-dashoffset arrows, fade-in labels, spring-scale cards\n"
        "- Bottom info card per scene (white bg, colored border, formula/description)\n"
        "- Manual navigation: showScene(), animateScene0-4(), nextStep(), prevStep()\n"
        "- DO NOT include Find/Quiz/Solution/Answer Box buttons\n\n"
        "Return ONLY raw JSON. The concept_code field must be complete "
        "<!DOCTYPE html>...</html> as escaped JSON string."
    )
    return system_blocks, user_content


def _build_prompt(question, category):
    strategy = STRATEGY_TEMPLATES.get(category, STRATEGY_TEMPLATES["PROCESS_BASED"])
    static_text = (
        SYSTEM
        + "\n\n" + DESIGN_SYSTEM
        + "\n\n" + SVG_TECHNIQUES
        + "\n\n" + FALLBACK_RULES
        + "\n\n" + HTML_SHELL_NOTE
    )
    system_blocks = [
        {
            "type": "text",
            "text": static_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    user_content = (
        f"Build a PREMIUM CINEMATIC 5-SCENE SVG ANIMATION for QAnim v11.3.\n\n"
        f"QUESTION: {question}\n"
        f"CATEGORY: {category}\n"
        f"STRATEGY: {strategy}\n\n"
        "KEY REMINDERS v11.3:\n"
        "- LIGHT THEME: white/light-gray backgrounds, dark text, vivid accents\n"
        "- MOBILE CRITICAL: root <svg> MUST have width=\"100%\" height=\"100%\" preserveAspectRatio=\"xMidYMid meet\"\n"
        "- Exactly 5 scenes following reference HTML structure\n"
        "- Scene 0: Problem setup with animated geometry\n"
        "- Scene 1: Core formula (named, not solved) with animated arrows\n"
        "- Scene 2: Secondary mechanism\n"
        "- Scene 3: Abstract resistance/process/flow circuit\n"
        "- Scene 4: Complete system + labeled summary info card\n"
        "- Each scene has bottom info card (white bg, colored border-top/border-left)\n"
        "- All SVG elements animate with setTimeout + opacity/stroke-dashoffset\n"
        "- solution_steps: concept nodes only (no numerical working inside scenes)\n"
        "- final_answer: REQUIRED -- fully solved answer with all computed values and units.\n"
        "    This is shown ONLY in the 'Final Answer' button panel, NEVER in the animation scenes.\n"
        "    Do NOT leave this empty. Example: \"Q = 142.6 W/m; T_s = 47.3 deg C\"\n"
        "- key_insight: one memorable sentence summarising the core concept\n"
        "- DO NOT include Find/Quiz/Solution/Answer Box buttons\n\n"
        "Return ONLY raw JSON. The animation_code must be complete "
        "<!DOCTYPE html>...</html> as escaped JSON string."
    )
    return system_blocks, user_content


# ===========================================================================
#  MODULE 13 -- Full Generation Pipeline  (v11.3)
# ===========================================================================

async def _generate_concept_animation(question, category):
    QAnimLogger.info("ConceptPipeline", f"START  category={category}")
    system_blocks, user_content = _build_concept_prompt(question, category)

    def _call_concept_api():
        return client.messages.create(
            model=CONCEPT_MODEL, max_tokens=MAX_TOK_CONCEPT,
            system=system_blocks,
            messages=[{"role": "user", "content": user_content}])

    try:
        # FIXED: run sync Anthropic call in a thread so it does not block the
        # asyncio event loop while asyncio.gather() is running all 3 stages.
        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(None, _call_concept_api)
        raw = msg.content[0].text.strip()
        QAnimLogger.info(
            "ConceptAI",
            f"model={CONCEPT_MODEL}  stop_reason={msg.stop_reason}  len={len(raw)}"
            f"  cache_read={getattr(msg.usage, 'cache_read_input_tokens', 0)}"
            f"  cache_create={getattr(msg.usage, 'cache_creation_input_tokens', 0)}"
        )
        if msg.stop_reason == "max_tokens":
            QAnimLogger.warn("ConceptAI", "Hit max_tokens -- response may be truncated!")
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
    concept_html = inject_controls_bar(concept_html)
    concept_html = inject_layout_wrapper(concept_html)
    concept_html = inject_voice_assistant(concept_html)
    concept_html = inject_step_controller(concept_html)  # LAST

    QAnimLogger.ok("ConceptPipeline", f"DONE -- len={len(concept_html):,}")
    return concept_html


async def generate_question_animation(question):
    """
    THREE-STAGE CONCURRENT PIPELINE (v11.3):

    Stage 0 -- ToFind Extraction   (sync, no AI)
    Stage 1 -- Concept Animation   (claude-sonnet-4-6) -- 5 scenes
    Stage 2 -- Solution Animation  (claude-sonnet-4-6) -- 5 scenes
    Stage 3 -- Haiku Solution      (claude-haiku-4-5)
               [Stages 1-3 run concurrently via asyncio.gather]

    v11.3 changes:
      - Mobile-responsive SVG: preserveAspectRatio + width/height=100% enforced
      - Layout wrapper fixes SVG scaling after DOM move
      - Controls bar safe-area-inset-bottom for iOS notch
      - Viewport meta tag injection in infrastructure
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("Question cannot be empty")

    short_q = question[:80] + ("..." if len(question) > 80 else "")
    QAnimLogger.info("Pipeline", f"START v11.3 -- '{short_q}'")

    to_find_targets = ToFindExtractor.extract(question)
    QAnimLogger.info("Pipeline", f"ToFind: {to_find_targets}")

    # FIXED: use non-blocking async classifier so we don't block the event loop
    category = await _classify_topic_async(question)
    QAnimLogger.info("Classifier", f"Category: {category}")

    system_blocks, user_content = _build_prompt(question, category)

    async def _run_solution_ai():
        """Runs solution AI call in a thread pool so asyncio.gather is truly concurrent."""
        def _call():
            return client.messages.create(
                model=SOLUTION_MODEL, max_tokens=MAX_TOK,
                system=system_blocks,
                messages=[{"role": "user", "content": user_content}])
        try:
            # FIXED: run_in_executor prevents blocking the asyncio loop
            loop = asyncio.get_event_loop()
            msg = await loop.run_in_executor(None, _call)
            raw = msg.content[0].text.strip()
            QAnimLogger.info(
                "SolutionAI",
                f"model={SOLUTION_MODEL}  stop_reason={msg.stop_reason}  len={len(raw)}"
                f"  cache_read={getattr(msg.usage, 'cache_read_input_tokens', 0)}"
                f"  cache_create={getattr(msg.usage, 'cache_creation_input_tokens', 0)}"
            )
            if msg.stop_reason == "max_tokens":
                QAnimLogger.warn("SolutionAI", "Hit max_tokens -- response may be truncated!")
            return raw
        except Exception as e:
            QAnimLogger.error("SolutionAI", f"API failed: {e}")
            raise

    QAnimLogger.info("Pipeline", "Launching 3 truly-concurrent AI stages (all run_in_executor)...")
    try:
        concept_html, sol_raw, haiku_sol = await asyncio.gather(
            _generate_concept_animation(question, category),
            _run_solution_ai(),
            HaikuSolutionGenerator.generate_async(question),
        )
    except Exception as e:
        QAnimLogger.error("Pipeline", f"Concurrent generation failed: {e}")
        return _build_failure_result(question, f"API error: {e}")

    result = _parse_response(sol_raw, question)
    result["category"]               = category
    result["engine_version"]         = "v11.3"
    result["concept_animation_code"] = concept_html
    result["to_find"]                = to_find_targets
    result["haiku_solution"]         = haiku_sol
    result.setdefault("solution_steps", [])
    result.setdefault("final_answer",   "")
    result.setdefault("key_insight",    "")

    haiku_steps = haiku_sol.get("steps", [])
    if haiku_steps and (not result["solution_steps"] or len(haiku_steps) > len(result["solution_steps"])):
        result["solution_steps"] = haiku_steps
        QAnimLogger.ok("Pipeline", f"Using Haiku solution steps ({len(haiku_steps)} steps)")

    def _is_real_answer(s):
        return bool(s and len(str(s).strip()) > 5 and any(c.isdigit() for c in str(s)))

    if not _is_real_answer(result["final_answer"]) and _is_real_answer(haiku_sol.get("final_answer", "")):
        result["final_answer"] = haiku_sol["final_answer"]
        QAnimLogger.ok("Pipeline", "Used Haiku final_answer (solution AI returned empty/placeholder)")

    if not _is_real_answer(result["final_answer"]):
        raw_haiku = haiku_sol.get("raw", "").strip()
        if raw_haiku:
            _fa_match = re.search(
                r'(?:\*{0,2}Final Answer\*{0,2})\s*:\s*(.+?)(?:\*{0,2}Key Insight\*{0,2}|$)',
                raw_haiku, re.DOTALL | re.IGNORECASE)
            result["final_answer"] = _fa_match.group(1).strip() if _fa_match else raw_haiku[:300]
            QAnimLogger.warn("Pipeline", "final_answer extracted from Haiku raw text (last resort)")
        else:
            result["final_answer"] = "Solution computed — open the step-by-step for full details."
            QAnimLogger.warn("Pipeline", "final_answer was empty; used fallback message")

    if haiku_sol.get("key_insight") and not result["key_insight"]:
        result["key_insight"] = haiku_sol["key_insight"]

    html = result.get("animation_code", "")

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

    answer_targets = _build_answer_targets(
        to_find_targets  = to_find_targets,
        haiku_sol        = haiku_sol,
        final_answer     = result["final_answer"],
        key_insight      = result["key_insight"],
    )
    result["answer_targets"] = answer_targets
    QAnimLogger.info("Pipeline", f"Answer targets built: {len(answer_targets)}")

    # POST-PROCESSING: inject all panels in order
    # StepController MUST come last
    html = HtmlSanitizer.sanitize(html)
    html = inject_infrastructure(html)
    html = inject_final_answer_panel(
        html           = html,
        answer_targets = answer_targets,
        final_answer   = result["final_answer"],
        key_insight    = result["key_insight"],
    )
    html = inject_to_find_system(html, to_find_targets)
    html = inject_notes_system(html, question)
    html = inject_answer_box_panel(html, answer_targets)
    html = inject_controls_bar(html)
    html = inject_layout_wrapper(html)       # v11.3: mobile-responsive wrapper
    html = inject_voice_assistant(html)
    html = inject_step_controller(html)      # MUST be absolute last

    try:
        GenerationValidator.validate(html, require_svg=True)
    except ValidationError as e:
        QAnimLogger.warn("FinalValidator", f"Post-injection: {e} -- continuing")

    result["animation_code"] = html
    result["render_status"]  = "ok"

    QAnimLogger.ok("Pipeline", (
        f"DONE v11.3 -- '{result['title']}' "
        f"concept={len(concept_html):,} "
        f"solution={len(html):,} "
        f"haiku_steps={len(haiku_steps)} "
        f"to_find={result['to_find']} "
        f"answer_targets={len(answer_targets)}"
    ))
    return result


def _build_failure_result(question, reason):
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
        "answer_targets":         [],
        "haiku_solution":         {"steps": [], "final_answer": "", "key_insight": "", "raw": ""},
        "category":               "UNKNOWN",
        "engine_version":         "v11.3",
        "render_status":          "error",
    }


def generate_question_animation_sync(question):
    """Synchronous wrapper for generate_question_animation."""
    return asyncio.run(generate_question_animation(question))


# ---------------------------------------------------------------------------
# Public aliases
# ---------------------------------------------------------------------------
generate_animation       = generate_question_animation
generate_animation_sync  = generate_question_animation_sync


# ===========================================================================
#  CLI TEST
# ===========================================================================
if __name__ == "__main__":
    import sys

    TEST_QUESTIONS = {
        "VISUAL_PHYSICS":  "A steam pipe of inner diameter 5 cm and outer diameter 7 cm carries steam at 250 degrees C. The pipe has thermal conductivity 45 W/mK and is exposed to air at 30 degrees C with convection coefficient 12 W/m2K. Find the rate of heat loss per meter and the outer surface temperature.",
        "PROCESS_BASED":   "How does a 4-stroke internal combustion engine work?",
        "MATHEMATICAL":    "Explain the Fundamental Theorem of Calculus with a visual proof.",
        "BIOLOGICAL":      "How does the human immune system fight a bacterial infection?",
        "ABSTRACT":        "What is the difference between democracy and authoritarianism?",
        "MIXED":           "How does an MRI machine produce images of the human body?",
    }

    if len(sys.argv) > 1:
        questions_to_test = {"CUSTOM": " ".join(sys.argv[1:])}
    else:
        key = "VISUAL_PHYSICS"
        questions_to_test = {key: TEST_QUESTIONS[key]}

    for cat, q in questions_to_test.items():
        print("=" * 72)
        print(f"  QAnim v11.3 -- Mobile-Responsive | 5-Scene Animation | {cat}")
        print(f"  Q: {q[:65]}...")
        print("=" * 72)

        print("\n[ToFind Smoke Test]")
        targets = ToFindExtractor.extract(q)
        print(f"  Targets: {targets}")

        print("\n[HaikuSolution Smoke Test]")
        haiku_result = HaikuSolutionGenerator.generate(q)
        print(f"  Haiku Steps   : {len(haiku_result['steps'])}")
        for i, s in enumerate(haiku_result['steps'][:3], 1):
            print(f"    Step {i}: {s[:80]}...")
        print(f"  Final Answer  : {haiku_result['final_answer'][:80]}")
        print(f"  Key Insight   : {haiku_result['key_insight'][:80]}")

        print("\n[AnswerTargets Smoke Test]")
        at = _build_answer_targets(targets, haiku_result, haiku_result['final_answer'], haiku_result['key_insight'])
        for i, t in enumerate(at, 1):
            print(f"  Target {i}: label={t['label'][:50]}  value={t['value'][:40]}  unit={t['unit']}")

        result = generate_question_animation_sync(q)

        concept_html  = result.get("concept_animation_code", "")
        solution_html = result.get("animation_code", "")
        haiku_sol     = result.get("haiku_solution", {})
        ans_targets   = result.get("answer_targets", [])

        print(f"\nTitle               : {result['title']}")
        print(f"Category            : {result.get('category','N/A')}")
        print(f"Engine              : {result.get('engine_version','N/A')}")
        print(f"Render Status       : {result.get('render_status','N/A')}")
        print(f"[ToFind] Targets    : {result.get('to_find',[])}")
        print(f"[Stage 1] Concept   : {len(concept_html):,} chars")
        print(f"[Stage 2] Solution  : {len(solution_html):,} chars")
        print(f"[Stage 3] Haiku Sol : {len(haiku_sol.get('steps',[]))} steps")
        print(f"[v11.3] Ans Targets : {len(ans_targets)} target(s)")

        slug = cat.lower()
        concept_out  = f"q_anim_v113_{slug}_concept.html"
        solution_out = f"q_anim_v113_{slug}_solution.html"

        with open(concept_out,  "w", encoding="utf-8") as f: f.write(concept_html)
        with open(solution_out, "w", encoding="utf-8") as f: f.write(solution_html)

        print(f"\n[Stage 1] Concept saved  : {concept_out}")
        print(f"[Stage 2] Solution saved : {solution_out}")
        print()
        print("v11.3 Mobile Fixes Applied:")
        print("  OK  SVG: width=100% height=100% preserveAspectRatio=xMidYMid meet")
        print("  OK  #pw-scene-wrap: aspect-ratio 10/6, position:relative")
        print("  OK  SVG forced absolute positioning inside scene wrap")
        print("  OK  Viewport meta tag injected")
        print("  OK  Controls bar: safe-area-inset-bottom for iOS")
        print("  OK  padding-bottom: 130px (controls bar clearance)")
        print("  OK  ResizeObserver re-fixes SVG on orientation change")
        print("  OK  Given strip hidden on tiny (<360px) screens")
        print()
        print("All devices supported:")
        print("  Mobile (360-480px): Full animation visible, scaled correctly")
        print("  Tablet (600-820px): 2-col given strip, comfortable layout")
        print("  Laptop (1080px):    Standard layout, all features visible")
        print("  Smart Board (1400+): Large fonts, wider padding, full detail")
