"""
q_animation.py  --  QAnim Question Animation Generator  v11.7
=============================================================
v11.7 -- CHANGES FROM v11.6:
  CLARITY OVERHAUL -- The core animation problem was that scenes were
  visually complex, abstract, and hard for students to connect to the
  actual question. Every prompt, scene rule, and design token has been
  rewritten around ONE principle: "A student should understand both the
  question AND the concept within 3 seconds of seeing the animation."

  KEY CHANGES:
  * Scene 0 now always shows the EXACT QUESTION restated visually with
    clearly labeled known values and the unknown (what to find) highlighted
    in a distinct color -- students immediately see "this is MY question."
  * Scene labels use plain English only (no formula names in titles).
  * Info card text is conversational: written like a tutor talking to
    a student, not like a textbook.
  * Each scene has a ONE-LINE "plain English headline" displayed large
    at the top -- the big idea in 8 words or fewer.
  * Known values shown as a clean list with icons. Unknown shown with
    a question-mark / highlight box so the student knows what is being found.
  * Formulas are introduced AFTER the setup (Scene 1), not in Scene 0.
  * Animations are simplified: max 2 moving elements per scene.
    No decorative noise. Every shape has a purpose.
  * Color system is strictly semantic:
      GREEN  (#16a34a) = given / known values
      PURPLE (#7c3aed) = what we are finding (unknown)
      BLUE   (#2563eb) = formulas / equations
      AMBER  (#d97706) = key insight / warning
      GRAY   (#64748b) = labels / secondary info
  * QUESTION STRIP: The actual question text is always visible at the
    top of every scene in a clearly readable box.
  * CIRCUIT ANIMATIONS: Circuit topology is drawn with maximum symbol
    clarity -- each component labeled with both its symbol AND its value
    from the question. No unlabeled components.
  * BOTTOM INFO CARD: Rewritten to always contain:
      Line 1: What this scene is showing (plain English)
      Line 2: Why it matters for THIS question
      Line 3 (optional): What to notice / remember

  v11.6 features fully preserved:
  - FINAL ANSWER PANEL (numbered i, ii, iii results)
  - ANSWER BOX unchanged (Find-condition chip, multi-target)
  - EEE/ECE Circuit Visualization Pipeline (Module 15 & 16)
  - Solution step generator: claude-sonnet-4-6
  - Prev/Next navigation buttons inside controls bar
  - Scene explanation box at y=420
  - Quiz system remains removed (v11.6 change)

PIPELINE (v11.7 -- unchanged from v11.6):
  Stage 0 -- ToFind Extraction    (sync, no AI)
  Stage 1 -- Concept Animation    (claude-sonnet-4-6)
  Stage 2 -- Solution Animation   (claude-sonnet-4-6)
  Stage 3 -- Solution Steps       (claude-sonnet-4-6)
             [all 3 run concurrently via asyncio.gather]
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
client = anthropic.Anthropic()

CONCEPT_MODEL        = "claude-sonnet-4-6"
SOLUTION_MODEL       = "claude-sonnet-4-6"
Q_MODEL              = SOLUTION_MODEL
HAIKU_SOLUTION_MODEL = "claude-sonnet-4-6"

MAX_TOK                = 20000
MAX_TOK_CONCEPT        = 12000
MAX_TOK_HAIKU_SOLUTION = 12000


# ===========================================================================
#  MODULE 1 -- QAnimLogger
# ===========================================================================
class QAnimLogger:
    PREFIX = "[QAnim v11.7]"

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
    html = re.sub(r'(<body[^>]*>)', r'\1\n' + ERROR_BOUNDARY_HTML, html, count=1, flags=re.IGNORECASE)
    first_script = re.search(r'<script(?:\s[^>]*)?>(?!.*type\s*=\s*["\']application/json)', html, re.IGNORECASE)
    if first_script:
        pos = first_script.start()
        html = html[:pos] + QANIM_INNER_LOGGER_JS + '\n' + html[pos:]
    else:
        html = html.replace('</body>', QANIM_INNER_LOGGER_JS + '\n</body>', 1)
    _scroll_fix = (
        '\n<style id="qanim-scroll-fix">\n'
        'html,body{overflow-x:hidden!important;overflow-y:auto!important;height:100%!important;min-height:100vh;width:100%!important;}\n'
        'svg{width:100%!important;height:100%!important;}\n'
        '#container,[id="container"]{padding-bottom:80px;width:100%;}\n'
        '/* ── Bold question text fix ── */\n'
        '#qstrip .qtext{font-weight:700!important;}\n'
        '#qstrip .qtext *{font-weight:700!important;}\n'
        '/* ── Nav buttons now live inside #qanim-controls-bar (v11.5+) ── */\n'
        '/* Legacy standalone #prevbtn/#nextbtn hidden when bar is present */\n'
        'body:has(#qanim-controls-bar) #prevbtn,\n'
        'body:has(#qanim-controls-bar) #nextbtn{\n'
        '  display:none!important;\n'
        '}\n'
        '/* ── Scene Explanation Box position fix (y=420) ── */\n'
        '#anim-container{padding-bottom:16px!important;}\n'
        '#anim-container svg{min-height:380px!important;}\n'
        '/* qstrip (question strip above animation) */\n'
        '#qstrip{\n'
        '  padding:14px 22px!important;\n'
        '  font-size:14.5px!important;\n'
        '  line-height:1.65!important;\n'
        '  border-radius:12px!important;\n'
        '  margin-bottom:10px!important;\n'
        '}\n'
        '/* controls row spacing */\n'
        '#controls{margin-top:12px!important;gap:18px!important;}\n'
        '</style>\n')
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
#  MODULE 7.5 -- Solution Step Generator
# ===========================================================================

_HAIKU_SOLUTION_SYSTEM = """You are a patient, expert tutor generating a detailed step-by-step solution for a student.
You are powered by claude-sonnet-4-6, which means you must use your full reasoning capability for
accurate calculations, clear explanations, and structured mathematical working.

RULES (follow every one):
1. Number every step: "Step 1:", "Step 2:", etc. -- never skip numbering.
2. At the START of each step, name the concept or formula being used in BOLD using **formula/concept name**.
3. Show ALL working -- do not skip arithmetic or algebra.
4. Use simple, plain English a high-school student can understand.
5. After the last numbered step, add a "Final Answer:" section.
   IMPORTANT: Format each computed value on its own line as:  Symbol = value unit
   Example (for multiple answers):
     Final Answer:
     X_L = 31.42 Ohm
     X_C = 31.83 Ohm
     Z = 20.01 Ohm
     I = 11.49 A
     Power factor = 0.9995
6. Keep each step focused on ONE action only.
7. Do NOT use LaTeX notation -- write math in plain text (e.g. "F = m x a" not "F=ma^{}").
8. End with a one-sentence "Key Insight:" that captures the most important concept.
9. For multi-part questions, address EVERY asked quantity. Never skip any asked value.

FORMAT EXAMPLE:
Step 1: **Identify the given information**
We know the mass m = 5 kg and acceleration a = 3 m/s^2. Write these down first.

Step 2: **Apply Newton's Second Law**
The formula is F = m x a. Substitute the values: F = 5 x 3 = 15 N.

Final Answer:
F = 15 N

Key Insight: Newton's Second Law links force, mass, and acceleration -- if mass doubles, force doubles for the same acceleration."""

_HAIKU_SOLUTION_USER_TEMPLATE = """Generate a detailed, numbered step-by-step solution for this question.
Follow the system instructions exactly.

QUESTION: {question}"""


class HaikuSolutionGenerator:

    @classmethod
    def generate(cls, question):
        QAnimLogger.info("HaikuSolution", f"Generating via {HAIKU_SOLUTION_MODEL}")
        prompt = _HAIKU_SOLUTION_USER_TEMPLATE.format(question=question[:600])
        try:
            msg = client.messages.create(
                model=HAIKU_SOLUTION_MODEL,
                max_tokens=MAX_TOK_HAIKU_SOLUTION,
                system=_HAIKU_SOLUTION_SYSTEM,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = msg.content[0].text.strip()
            QAnimLogger.info("HaikuSolution", f"stop_reason={msg.stop_reason}  len={len(raw)}")
            return cls._parse(raw)
        except Exception as e:
            QAnimLogger.error("HaikuSolution", f"Generation failed: {e}")
            return cls._fallback(question)

    @classmethod
    async def generate_async(cls, question):
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
  transition: opacity 0.25s ease, transform 0.26s cubic-bezier(0.34, 1.56, 0.64, 1);
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
#answerbox-panel.open { opacity: 1; pointer-events: auto; transform: translateY(0) scale(1); }
.ab-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 20px; background: #ffffff; border-bottom: 1px solid #e2e8f0; flex-shrink: 0;
}
.ab-header-title {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 16px; font-weight: 800; color: #1e293b;
  display: flex; align-items: center; gap: 8px;
}
.ab-close-btn {
  width: 30px; height: 30px; border-radius: 8px; border: 1px solid #e2e8f0;
  background: #f8fafc; color: #64748b; font-size: 12px; cursor: pointer;
  display: flex; align-items: center; justify-content: center; transition: background 0.15s;
}
.ab-close-btn:hover { background: #fee2e2; color: #dc2626; }
.ab-progress-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 20px 0; flex-shrink: 0;
}
.ab-progress-label {
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 11px; font-weight: 700; color: #7c3aed;
  text-transform: uppercase; letter-spacing: 0.8px;
}
.ab-progress-dots { display: flex; gap: 5px; }
.ab-dot { width: 7px; height: 7px; border-radius: 50%; background: #e2e8f0; transition: background 0.2s; }
.ab-dot.done    { background: #16a34a; }
.ab-dot.current { background: #7c3aed; }
.ab-body { padding: 14px 20px 20px; overflow-y: auto; flex: 1; }
.ab-find-chip {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 10px 14px; border-radius: 10px; background: #f5f3ff;
  border: 1px solid #ddd6fe; margin-bottom: 14px;
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
  width: 100%; min-height: 80px; padding: 12px 14px; border-radius: 10px;
  border: 1.5px solid #e2e8f0; background: #f8fafc;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 13px; color: #1e293b; line-height: 1.6; resize: vertical;
  transition: border-color 0.15s; outline: none; box-sizing: border-box;
}
#ab-user-input:focus { border-color: #7c3aed; background: #ffffff; }
#ab-submit-btn {
  width: 100%; padding: 12px; margin-top: 10px; border-radius: 10px; border: none;
  background: #7c3aed; color: #ffffff; font-size: 14px; font-weight: 700;
  font-family: inherit; cursor: pointer; transition: background 0.15s, transform 0.1s;
}
#ab-submit-btn:hover { background: #6d28d9; transform: translateY(-1px); }
#ab-feedback {
  display: none; margin-top: 14px; border-radius: 12px; overflow: hidden;
  border: 1px solid transparent; animation: ab-feedback-in 0.28s cubic-bezier(0.34,1.56,0.64,1);
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
.ab-feedback-verdict { font-family: -apple-system, 'Segoe UI', Arial, sans-serif; font-size: 15px; font-weight: 800; }
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
.ab-insight-text { font-family: -apple-system, 'Segoe UI', Arial, sans-serif; font-size: 12.5px; color: #1e293b; line-height: 1.68; }
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
  display: none; text-align: center; padding: 28px 20px; border-radius: 14px;
  background: linear-gradient(135deg, #f0fdf4, #fefce8); border: 1.5px solid #bbf7d0; margin-top: 10px;
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
      <div class="ab-alldone-sub">Great work. Open <strong>Final Answer</strong> to review the full solution.</div>
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
}
.qanim-ctrl-btn:hover {
  background: linear-gradient(135deg, #ede9fe 0%, #fdf4ff 100%);
  border-color: #7c3aed;
  color: #6d28d9;
  transform: translateY(-2px);
  box-shadow: 0 4px 14px rgba(124, 58, 237, 0.22);
}
.qanim-ctrl-btn:active { transform: translateY(0); box-shadow: none; }
#ctrl-prevbtn,
#ctrl-nextbtn {
  background: linear-gradient(135deg, #ede9fe 0%, #e0d9fb 100%);
  border-color: #7c3aed;
  color: #6d28d9;
  font-size: 13px;
  padding: 8px 18px;
}
#ctrl-prevbtn:hover,
#ctrl-nextbtn:hover {
  background: linear-gradient(135deg, #7c3aed 0%, #6d28d9 100%);
  color: #ffffff;
  border-color: #5b21b6;
}
#ctrl-prevbtn:disabled,
#ctrl-nextbtn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
  transform: none;
  box-shadow: none;
}
.qanim-ctrl-sep {
  width: 1px;
  height: 22px;
  background: linear-gradient(to bottom, transparent, #c4b5fd, transparent);
  flex-shrink: 0;
}
@media (max-width: 600px) {
  #qanim-controls-bar { bottom: 10px; padding: 7px 8px; gap: 3px; }
  .qanim-ctrl-btn { padding: 7px 10px; font-size: 11px; }
  .qanim-ctrl-btn .ctrl-label { display: none; }
  #ctrl-prevbtn,#ctrl-nextbtn { padding: 7px 12px; font-size: 13px; }
  #qanim-notes-btn { padding: 8px 12px; font-size: 12px; }
  #qanim-notes-btn .notes-btn-emoji { font-size: 15px; }
}
</style>
"""

_CONTROLS_BAR_DOM = """
<div id="qanim-controls-bar" role="toolbar" aria-label="QAnim Controls">
  <button class="qanim-ctrl-btn" id="ctrl-prevbtn" title="Previous scene" disabled>
    <span>&#x25C4;</span><span class="ctrl-label">Prev</span>
  </button>
  <div class="qanim-ctrl-sep"></div>
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
  <div class="qanim-ctrl-sep"></div>
  <button id="qanim-notes-btn" aria-label="Open notes" title="My Notes">
    <span class="notes-btn-emoji">&#x1F4D3;</span><span>Notes</span>
  </button>
  <div class="qanim-ctrl-sep" id="ctrl-next-sep"></div>
  <button class="qanim-ctrl-btn" id="ctrl-nextbtn" title="Next scene">
    <span>&#x25BA;</span><span class="ctrl-label">Next</span>
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
        QAnimLogger.ok("ControlsBar", "Controls bar injected (v11.7)")
    except Exception as e:
        QAnimLogger.warn("ControlsBar", f"DOM failed: {e}")
    return html


# ===========================================================================
#  MODULE 11 -- Notes System
# ===========================================================================

_NOTES_CSS = """
<style id="qanim-notes-styles">
#qanim-notes-btn {
  display: flex; align-items: center; gap: 7px;
  padding: 10px 20px;
  border-radius: 12px;
  border: 2px solid #f59e0b;
  background: linear-gradient(135deg, #fffbeb 0%, #fef3c7 100%);
  color: #92400e;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 14px; font-weight: 800;
  cursor: pointer;
  letter-spacing: 0.3px;
  box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.5);
  animation: qanim-notes-pulse 2.8s ease-in-out infinite;
  transition: background 0.18s, border-color 0.18s, color 0.18s, box-shadow 0.18s, transform 0.14s;
}
#qanim-notes-btn .notes-btn-emoji { font-size: 18px; line-height: 1; }
@keyframes qanim-notes-pulse {
  0%,100% { box-shadow: 0 0 0 0 rgba(245,158,11,0.45); }
  50%      { box-shadow: 0 0 0 7px rgba(245,158,11,0); }
}
#qanim-notes-btn:hover {
  background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);
  border-color: #d97706; color: #78350f;
  transform: translateY(-2px);
  box-shadow: 0 6px 18px rgba(245, 158, 11, 0.40);
  animation: none;
}
#qanim-notes-btn:active { transform: translateY(0); box-shadow: none; animation: none; }
#qanim-notes-btn.notes-open {
  background: linear-gradient(135deg, #fde68a 0%, #fbbf24 100%);
  border-color: #b45309; color: #78350f; animation: none;
}
#qanim-notes-panel {
  position: fixed; bottom: 80px; right: 16px; z-index: 7200;
  width: 340px; max-height: 80vh; border-radius: 14px;
  background: #ffffff; border: 1px solid #e2e8f0;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.10);
  display: flex; flex-direction: column; overflow: hidden;
  opacity: 0; transform: translateY(8px) scale(0.97); pointer-events: none;
  transition: opacity 0.22s ease, transform 0.22s ease;
}
#qanim-notes-panel.open { opacity: 1; transform: translateY(0) scale(1); pointer-events: auto; }
#qanim-notes-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 14px; background: #fffbeb; border-bottom: 1px solid #fef3c7; cursor: grab; flex-shrink: 0;
}
.notes-header-title { font-family: -apple-system, 'Segoe UI', Arial, sans-serif; font-size: 13px; font-weight: 700; color: #92400e; }
.notes-hdr-btn {
  width: 24px; height: 24px; border-radius: 6px; border: 1px solid #fde68a;
  background: rgba(255,255,255,0.6); color: #92400e; font-size: 12px;
  display: flex; align-items: center; justify-content: center; cursor: pointer;
}
.notes-hdr-btn:hover { background: #fef3c7; }
#qanim-notes-tabs { display: flex; border-bottom: 1px solid #f1f5f9; flex-shrink: 0; }
.notes-tab {
  flex: 1; padding: 7px 0; text-align: center;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 11px; font-weight: 600; color: #94a3b8; cursor: pointer;
  border-bottom: 2px solid transparent; transition: color 0.15s, border-color 0.15s;
  text-transform: uppercase; letter-spacing: 0.5px;
}
.notes-tab.active { color: #f59e0b; border-bottom-color: #f59e0b; }
#qanim-canvas-toolbar {
  display: flex; align-items: center; gap: 5px; padding: 6px 10px;
  background: #f8fafc; border-bottom: 1px solid #f1f5f9; flex-shrink: 0; flex-wrap: wrap;
}
.canvas-tool-btn {
  padding: 3px 9px; border-radius: 5px; border: 1px solid #e2e8f0;
  background: #ffffff; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer;
}
.canvas-tool-btn.active { background: #fef3c7; border-color: #f59e0b; color: #92400e; }
.color-dot {
  width: 16px; height: 16px; border-radius: 50%; cursor: pointer;
  border: 2px solid transparent; transition: transform 0.12s;
}
.color-dot:hover   { transform: scale(1.2); }
.color-dot.selected { border-color: #1e293b; transform: scale(1.1); }
.size-btn {
  width: 20px; height: 20px; border-radius: 50%; border: 1px solid #e2e8f0;
  background: #ffffff; color: #64748b; font-size: 10px; font-weight: 700;
  display: flex; align-items: center; justify-content: center; cursor: pointer;
}
.size-btn.active { background: #fef3c7; border-color: #f59e0b; color: #92400e; }
.tool-sep { width: 1px; height: 18px; background: #e2e8f0; flex-shrink: 0; }
#qanim-canvas-wrap { flex: 1 1 auto; position: relative; overflow: hidden; min-height: 180px; }
#qanim-draw-canvas { display: block; width: 100%; height: 100%; cursor: crosshair; background: #fefce8; touch-action: none; }
#qanim-text-pane { display: none; flex-direction: column; flex: 1 1 auto; overflow: hidden; }
#qanim-notes-textarea {
  flex: 1 1 auto; width: 100%; min-height: 180px; resize: none; box-sizing: border-box;
  background: #f8fafc; border: none; outline: none; color: #1e293b;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif; font-size: 13px; line-height: 1.7; padding: 12px 14px;
}
#qanim-notes-textarea::placeholder { color: #cbd5e1; }
#qanim-notes-footer {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 12px; border-top: 1px solid #f1f5f9; flex-shrink: 0; background: #f8fafc;
}
.notes-status { font-size: 10px; color: #94a3b8; font-family: -apple-system, 'Segoe UI', Arial, sans-serif; }
.notes-action-btn {
  padding: 3px 10px; border-radius: 5px; border: 1px solid #e2e8f0;
  background: #ffffff; color: #64748b; font-size: 10px; font-weight: 600; cursor: pointer;
}
.notes-action-btn:hover { background: #ede9fe; border-color: #7c3aed; color: #7c3aed; }
</style>
"""

_NOTES_DOM = """
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
  function openNotes(){var panel=_el('qanim-notes-panel');if(!panel)return;panel.classList.add('open');panel.setAttribute('aria-hidden','false');isOpen=true;var btn=_el('qanim-notes-btn');if(btn)btn.classList.add('notes-open');setTimeout(function(){_resizeCanvas();},50);}
  function closeNotes(){var panel=_el('qanim-notes-panel');if(panel){panel.classList.remove('open');panel.setAttribute('aria-hidden','true');}isOpen=false;var btn=_el('qanim-notes-btn');if(btn)btn.classList.remove('notes-open');_saveNotes();}
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
#  MODULE 14 -- Voice Assistant System
# ===========================================================================

_VOICE_ASSISTANT_CSS = """
<style id="qanim-voice-styles">
#qanim-voice-btn {
  display: flex; align-items: center; gap: 5px; padding: 8px 15px;
  border-radius: 10px; border: 1.5px solid #e2e8f0;
  background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
  color: #334155; font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
  font-size: 12px; font-weight: 700; cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s, transform 0.12s, box-shadow 0.15s;
  user-select: none; letter-spacing: 0.2px;
}
#qanim-voice-btn:hover {
  background: linear-gradient(135deg, #ede9fe 0%, #fdf4ff 100%);
  border-color: #7c3aed; color: #6d28d9;
  transform: translateY(-2px); box-shadow: 0 4px 14px rgba(124, 58, 237, 0.22);
}
#qanim-voice-btn:active { transform: translateY(0); box-shadow: none; }
#qanim-voice-btn.speaking { background: linear-gradient(135deg, #ede9fe 0%, #fdf4ff 100%); border-color: #7c3aed; color: #6d28d9; }
#qanim-voice-btn.muted { color: #94a3b8; border-color: #e2e8f0; }
@media (max-width: 600px) {
  #qanim-voice-btn { padding: 7px 11px; font-size: 11px; }
  #qanim-voice-btn .ctrl-label { display: none; }
}
</style>
"""

_VOICE_ASSISTANT_JS = r"""
<script id="qanim-voice-assistant">
(function initVoiceAssistant(){
  'use strict';
  var _muted=false,_synth=window.speechSynthesis||null,_supported=!!_synth,_btn=null;
  var _lastSpokenIdx=-1,_speakTimer=null;
  function _setText(icon,label){if(!_btn)return;_btn.innerHTML='<span>'+icon+'</span><span class="ctrl-label">'+label+'</span>';}
  function _getSceneText(sceneEl){
    if(!sceneEl)return'';
    var dv=sceneEl.getAttribute('data-voice');if(dv&&dv.trim())return dv.trim();
    var nodes=sceneEl.querySelectorAll('text, foreignObject, p, h1, h2, h3, h4, li');
    var seen=Object.create(null),parts=[],total=0;
    for(var i=0;i<nodes.length;i++){
      var raw=(nodes[i].textContent||'').replace(/\s+/g,' ').trim();
      if(raw.length<5)continue;if(/^[\d\s\+\-\=\.\,\(\)\[\]\{\}\/\*\^\%]+$/.test(raw))continue;
      var key=raw.toLowerCase();if(seen[key])continue;seen[key]=true;
      parts.push(raw);total+=raw.length;if(total>600)break;
    }
    return parts.join('. ').trim();
  }
  function _speak(text){
    if(!_supported||_muted||!text)return;
    try{
      _synth.cancel();var u=new SpeechSynthesisUtterance(text);u.rate=0.90;u.pitch=1.0;u.volume=1.0;
      var voices=_synth.getVoices();
      for(var i=0;i<voices.length;i++){var v=voices[i];if(/en[-_]/i.test(v.lang)&&!/novelty|zira|hazel|espeak/i.test(v.name)){u.voice=v;break;}}
      u.onstart=function(){if(_btn){_btn.classList.add('speaking');_setText('&#x1F50A;','Speaking');}};
      u.onend=function(){if(_btn){_btn.classList.remove('speaking');_setText('&#x1F50A;','Voice');}};
      u.onerror=function(){if(_btn){_btn.classList.remove('speaking');_setText('&#x1F50A;','Voice');}};
      _synth.speak(u);
    }catch(e){console.warn('[QAnim VA] speak error:',e);}
  }
  function _onSceneChange(idx){
    if(!_supported||_muted)return;if(idx===_lastSpokenIdx)return;_lastSpokenIdx=idx;
    clearTimeout(_speakTimer);_synth.cancel();
    _speakTimer=setTimeout(function(){
      var sceneEl=document.getElementById('scene-'+idx);
      var text=_getSceneText(sceneEl);if(!text)text='Scene '+(idx+1)+'.';
      _speak(text);
    },450);
  }
  function _findVisibleSceneIdx(){
    for(var i=0;i<20;i++){var s=document.getElementById('scene-'+i);if(!s)break;var op=parseFloat(s.style.opacity);if(op>0.5)return i;}return 0;
  }
  function _toggleMute(){
    _muted=!_muted;
    if(_muted){clearTimeout(_speakTimer);if(_synth)_synth.cancel();_btn.classList.add('muted');_btn.classList.remove('speaking');_setText('&#x1F507;','Muted');}
    else{_btn.classList.remove('muted');_setText('&#x1F50A;','Voice');_lastSpokenIdx=-1;_onSceneChange(_findVisibleSceneIdx());}
  }
  function _attachListeners(){
    document.addEventListener('qanim:sceneChange',function(e){if(e&&e.detail&&typeof e.detail.idx==='number')_onSceneChange(e.detail.idx);});
    var _obDebounce=null,_obLastIdx=-1;
    function _obCheck(){for(var i=0;i<20;i++){var s=document.getElementById('scene-'+i);if(!s)break;var op=parseFloat(s.style.opacity);if(op>0.7&&i!==_obLastIdx){_obLastIdx=i;if(i!==_lastSpokenIdx)_onSceneChange(i);return;}}}
    var root=document.querySelector('svg')||document.body;
    var obs=new MutationObserver(function(){clearTimeout(_obDebounce);_obDebounce=setTimeout(_obCheck,60);});
    obs.observe(root,{attributes:true,subtree:true,attributeFilter:['style','opacity']});
    setTimeout(function(){if(_lastSpokenIdx===-1){_lastSpokenIdx=-1;_onSceneChange(0);}},950);
  }
  function _init(){
    if(!_supported){console.warn('[QAnim VA] Web Speech API not supported.');return;}
    var bar=document.getElementById('qanim-controls-bar');
    var nextSep=document.getElementById('ctrl-next-sep');
    if(bar && nextSep){
      var voiceSep=document.createElement('div');voiceSep.className='qanim-ctrl-sep';
      _btn=document.createElement('button');_btn.id='qanim-voice-btn';_btn.title='Toggle voice narration';
      _btn.innerHTML='<span>&#x1F50A;</span><span class="ctrl-label">Voice</span>';
      _btn.addEventListener('click',_toggleMute);
      bar.insertBefore(voiceSep, nextSep);
      bar.insertBefore(_btn, nextSep);
    } else if(bar){
      var sep=document.createElement('div');sep.className='qanim-ctrl-sep';bar.appendChild(sep);
      _btn=document.createElement('button');_btn.id='qanim-voice-btn';_btn.title='Toggle voice narration';
      _btn.innerHTML='<span>&#x1F50A;</span><span class="ctrl-label">Voice</span>';
      _btn.addEventListener('click',_toggleMute);bar.appendChild(_btn);
    } else {
      _btn=document.createElement('button');_btn.id='qanim-voice-btn';
      _btn.style.cssText='position:fixed;bottom:70px;right:70px;z-index:6800;';
      _btn.innerHTML='<span>&#x1F50A;</span><span class="ctrl-label">Voice</span>';
      _btn.addEventListener('click',_toggleMute);document.body.appendChild(_btn);
    }
    if(_synth.getVoices().length===0)_synth.addEventListener('voiceschanged',function(){},{once:true});
    _attachListeners();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',_init);else setTimeout(_init,0);
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
        QAnimLogger.ok("VoiceAssistant", "Voice assistant injected (inserts before Next btn)")
    except Exception as e:
        QAnimLogger.warn("VoiceAssistant", f"JS injection failed: {e}")
    return html


# ===========================================================================
#  MODULE 12 -- StepController Patcher
# ===========================================================================

_STEP_CONTROLLER_JS = r"""
<script id="qanim-step-controller">
(function patchStepController(){
  'use strict';
  function initSC(){
    try{
      var nextBtn=document.getElementById('ctrl-nextbtn')||document.getElementById('nextbtn');
      var prevBtn=document.getElementById('ctrl-prevbtn')||document.getElementById('prevbtn');
      if(!nextBtn){
        nextBtn=document.createElement('button');nextBtn.id='nextbtn';nextBtn.textContent='Next \u25B6';
        nextBtn.style.cssText='position:fixed;bottom:120px;right:20px;z-index:6500;padding:10px 22px;min-width:80px;border-radius:10px;border:1px solid #e2e8f0;background:#7c3aed;color:#fff;font-size:14px;font-weight:700;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.12);';
        document.body.appendChild(nextBtn);}
      if(!prevBtn){
        prevBtn=document.createElement('button');prevBtn.id='prevbtn';prevBtn.textContent='\u25C4 Prev';
        prevBtn.style.cssText='position:fixed;bottom:120px;left:20px;z-index:6500;padding:10px 22px;min-width:80px;border-radius:10px;border:1px solid #e2e8f0;background:#fff;color:#334155;font-size:14px;font-weight:700;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.12);';
        document.body.appendChild(prevBtn);}
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
        if(prevBtn){if(currentStep===0){prevBtn.setAttribute('disabled','true');prevBtn.style.opacity='0.35';}else{prevBtn.removeAttribute('disabled');prevBtn.style.opacity='1';}}
        if(nextBtn){if(currentStep===scenes.length-1){nextBtn.setAttribute('disabled','true');nextBtn.style.opacity='0.35';}else{nextBtn.removeAttribute('disabled');nextBtn.style.opacity='1';}}}
      var nb2=nextBtn.cloneNode(true);nextBtn.parentNode.replaceChild(nb2,nextBtn);nextBtn=nb2;
      if(prevBtn){var pb2=prevBtn.cloneNode(true);prevBtn.parentNode.replaceChild(pb2,prevBtn);prevBtn=pb2;}
      nextBtn.addEventListener('click',function(e){e.stopPropagation();if(currentStep<scenes.length-1)showScene(currentStep+1);});
      if(prevBtn)prevBtn.addEventListener('click',function(e){e.stopPropagation();if(currentStep>0)showScene(currentStep-1);});
      var _ri=window.setInterval;window.setInterval=function(fn,ms){
        var src=fn?fn.toString():'';
        if(ms&&ms<8000&&(src.indexOf('showScene')!==-1||src.indexOf('currentStep')!==-1||src.indexOf('nextStep')!==-1)){console.log('[SC] Blocked auto-advance interval ('+ms+'ms)');return -1;}
        return _ri.apply(window,arguments);};
      showScene(0);console.log('[QAnim SC v11.7] '+scenes.length+' scenes ready -- bar buttons wired');
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
        QAnimLogger.ok("StepController", "Manual step controller injected (v11.7)")
    except Exception as e:
        QAnimLogger.warn("StepController", f"Injection failed: {e}")
    return html


# ===========================================================================
#  RESPONSE PARSING UTILITIES
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
#  SYSTEM PROMPTS  (v11.7 -- CLARITY OVERHAUL)
# ===========================================================================

# ---------------------------------------------------------------------------
# SHARED DESIGN TOKENS (v11.7)
# ---------------------------------------------------------------------------
DESIGN_SYSTEM = """
=== v11.7 CLARITY DESIGN SYSTEM ===

THEME: White/light-gray background (#f8fafc). NEVER dark backgrounds.
SVG viewBox: "0 0 1000 600"

SEMANTIC COLOR PALETTE (strictly enforced):
  GREEN  #16a34a  = known/given values (what the question gives us)
  PURPLE #7c3aed  = unknown / what we are finding (highlighted)
  BLUE   #2563eb  = formulas and equations
  AMBER  #d97706  = key insight / warning / important note
  GRAY   #64748b  = secondary labels, axes, decorative

TYPOGRAPHY HIERARCHY:
  Scene headline (top center): font-size=22, font-weight=800, fill=#1e293b
    -- Plain English, 8 words or fewer, e.g. "Here's what we're finding"
  Section labels: font-size=16, font-weight=700
  Body text: font-size=14, font-weight=400, fill=#334155
  Small labels: font-size=12, fill=#64748b

QUESTION STRIP (#qstrip):
  - Always visible at top of every scene
  - White bg, subtle border, rounded, 14px font-weight=700
  - Actual question text (not paraphrased)

KNOWN VALUES BOX (v11.7 REQUIRED in Scene 0 and Scene 1):
  - White card, green left-accent bar (#16a34a, 6px wide)
  - List each given value: green circle bullet + symbol = value unit
  - Position: left side, y=120 to y=340

UNKNOWN TARGET BOX (v11.7 REQUIRED in Scene 0):
  - Purple card (#ede9fe bg, #7c3aed border, 2px)
  - Label: "? Find:" in purple bold
  - List each unknown in purple text
  - Position: right side, y=120 to y=260

BOTTOM INFO CARD (REQUIRED every scene):
  rect: x=30, y=420, width=940, height=130, rx=12, fill=white, stroke=#e2e8f0
  Left accent bar: x=30, y=420, width=8, height=130, fill=scene_accent_color
  Line 1 (title): font-size=15, font-weight=700, x=55, y=448
  Line 2 (body): font-size=13.5, x=55, y=470
  Line 3 (body): font-size=13.5, x=55, y=492 (optional)
  Max 3 lines. Write like a tutor talking to a student.
  NEVER use jargon or formula names in card text -- use plain English.

ANIMATION RULES (v11.7 SIMPLICITY):
  - MAX 2 moving/animated elements per scene
  - Every shape must serve a purpose -- no decoration
  - Opacity 0->1 fade-ins: max 4 elements per scene, staggered 150ms
  - NO spinning, rotating, or bouncing animations (distracting)
  - Arrows: simple straight lines with arrowhead marker
  - stroke-dashoffset only for drawing circuit wires or flow paths

SUBSCRIPTS: ALWAYS <tspan dy="5" font-size="0.72em">sub</tspan>
            NEVER underscore notation (f_s, T_1, etc.)
"""

SVG_TECHNIQUES = """
=== SVG ANIMATION TECHNIQUES (v11.7) ===

FADE IN (preferred):
  element.style.opacity = '0';
  setTimeout(function(){ el.style.transition='opacity .4s ease'; el.style.opacity='1'; }, delay);

SCALE IN (for icons/circles only):
  el.style.transform='scale(0)'; el.style.transition='none';
  setTimeout(function(){ el.style.transition='transform .3s cubic-bezier(0.34,1.56,0.64,1)';
    el.style.transform='scale(1)'; }, delay);

DRAW LINE (for wires/arrows):
  Use stroke-dasharray + stroke-dashoffset with CSS transition.

KEEP IT SIMPLE:
  Use setTimeout chains, NOT CSS animation-delay (more reliable cross-browser).
  Never stack more than 4 sequential animations per scene.

FORMULA DISPLAY:
  Show formula in a rounded rect (blue border, light-blue bg #eff6ff).
  Write formula in plain text with spaces: "F = m x a" not LaTeX.
  Font: monospace or bold sans-serif for readability.
"""

FALLBACK_RULES = """
IF STUCK: Use one of these clarity-first fallback layouts:
1. TWO-COLUMN: Left = known values (green), Right = unknown (purple), Center = formula (blue)
2. BEFORE/AFTER: Left half shows starting state, right half shows result
3. LABELED DIAGRAM: Simple shape + clearly labeled arrows
4. CHECKLIST: Numbered steps in white cards with icons
NEVER: dark backgrounds, neon colors, spinning animations, 3D effects.
"""

# ---------------------------------------------------------------------------
# MAIN SOLUTION SYSTEM PROMPT  (v11.7)
# ---------------------------------------------------------------------------
SYSTEM = """You are QAnim v11.7 -- a clarity-first educational SVG animation designer.

YOUR MISSION: Turn any student question into a CRYSTAL-CLEAR 5-scene animation.
The #1 rule: When a student opens Scene 0, they must INSTANTLY understand what
the question is asking -- what is given, and what they need to find.

=== SCENE STRUCTURE (v11.7 CLARITY MODEL) ===

Scene 0 "Here's the Question" (MOST IMPORTANT):
  TOP: Plain-English headline (22px bold): "Here's what we're working with"
  LEFT SIDE -- GIVEN VALUES BOX (green card):
    Title: "What we know" (green, bold)
    Each given value on its own line: green bullet + "symbol = value unit"
    Example: "• m = 5 kg", "• a = 3 m/s²", "• t = 10 s"
  RIGHT SIDE -- FIND BOX (purple card):
    Title: "? What to find" (purple, bold)
    Each unknown: purple bullet + description
    Example: "• Force (F)", "• Velocity (v)"
  CENTER: Simple picture of the physical situation (object, system, or setup)
    -- labeled with the given values on the picture itself
  BOTTOM CARD: "This question gives us [list knowns] and asks us to find [list unknowns]."
  ANIMATION: Each item in the cards fades in one by one (150ms apart)

Scene 1 "The Key Idea":
  TOP: Plain-English headline: "The formula that connects everything"
  CENTER: The formula in a large, clear blue box -- written in plain text
    Example: "Force = Mass × Acceleration" then below: "F = m × a"
  LEFT: Small reminder of what each symbol means (symbol: plain English name)
  BOTTOM CARD: "This formula connects [variable1], [variable2], and [variable3].
    Because we know [knowns], we can find [unknown]."
  ANIMATION: Formula box scales in, then symbol list fades in

Scene 2 "How It Works":
  TOP: Plain-English headline: "Visualizing what's happening"
  CENTER: A clear diagram showing the physical mechanism
    -- arrows show direction/flow
    -- labels use actual values from the question
    -- highlight the unknown with a purple question mark
  BOTTOM CARD: "In plain terms: [one sentence explaining the physical situation]"
  ANIMATION: Diagram builds up left to right

Scene 3 "Setting It Up":
  TOP: Plain-English headline: "Plugging in the numbers"
  CENTER: Show the substitution step visually:
    Formula template (blue) → values substituted (green) → result placeholder (purple ?)
    Example layout:
      F = m × a
      F = 5 × 3
      F = ?
  LEFT: A reminder card showing all given values again
  BOTTOM CARD: "We substitute the known values into the formula.
    The answer is one calculation away."
  ANIMATION: Each line of substitution appears with a short delay

Scene 4 "Solving It -- Your Turn":
  TOP: Plain-English headline: "Step-by-step approach"
  CENTER: A clean numbered checklist (4-5 steps):
    ① Identify what's given (with checkmarks)
    ② Write the formula
    ③ Substitute values
    ④ Calculate
    ⑤ Check units
  BOTTOM CARD: "Follow these steps in order and you'll get the answer every time."
  ANIMATION: Steps appear one by one with a 200ms stagger

=== IMPORTANT RULES ===
- Scene 0 MUST show the actual values from the question (not generic labels)
- NEVER use formula names as scene titles -- use plain English
- NEVER show the computed numerical answer in any scene
- PUT the fully computed answer in the "final_answer" JSON field
- Write bottom card text like a friendly tutor, not a textbook
- Use the SEMANTIC COLORS: green=known, purple=unknown, blue=formula
- MAX 2 animated elements per scene (fade-in is not counted as animation)
- Every text element must be readable: minimum font-size 12, fill #1e293b or #334155

=== TECHNICAL REQUIREMENTS ===
- Complete <!DOCTYPE html>...</html>
- SVG viewBox="0 0 1000 600", white/light background
- NO external scripts, NO backtick template literals, NO document.write()
- Balanced SVG and script tags
- Include: #prevbtn, #nextbtn, #dots, #qstrip .qtext
- DO NOT include Find/Solution/Answer Box buttons
- scene-0 through scene-4 as <g id="scene-N"> elements

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "animation_type": "concise label",
  "design_strategy": "2-4 sentence description",
  "solution_steps": ["Step 1: ...", "Step 2: ...", ...],
  "final_answer": "<fully computed answer with all values and units>",
  "key_insight": "one memorable sentence",
  "animation_code": "COMPLETE HTML AS ESCAPED JSON STRING"
}"""

# ---------------------------------------------------------------------------
# CONCEPT SYSTEM PROMPT  (v11.7)
# ---------------------------------------------------------------------------
SYSTEM_CONCEPT = """You are QAnim Concept Engine v11.7 -- clarity-first concept animator.

YOUR MISSION: 5-scene CONCEPT animation. No computed answers in scenes.
Students should instantly understand the physical/mathematical concept.

=== SCENE STRUCTURE (v11.7 CONCEPT MODEL) ===

Scene 0 "What Is This About?":
  Show the real-world situation this concept applies to.
  Use a clear, labeled diagram. Plain-English headline (22px).
  Bottom card: "This concept explains [plain description]."

Scene 1 "The Main Idea":
  Show the core formula/rule in a large blue box.
  Label every symbol in plain English.
  Bottom card: "The key relationship is: [plain English version of formula]."

Scene 2 "How It Behaves":
  Show what happens when one variable changes (simple arrow diagram or graph).
  Use green for increases, red for decreases.
  Bottom card: "When [A] increases, [B] [increases/decreases] because..."

Scene 3 "Connecting the Pieces":
  Show how two or more ideas from this topic connect.
  Simple arrows or flow diagram.
  Bottom card: "Together, [concept1] and [concept2] determine [outcome]."

Scene 4 "How to Use This":
  Show a 4-step approach checklist for solving problems with this concept.
  Each step is plain English and actionable.
  Bottom card: "Use these 4 steps every time you see this type of problem."

OUTPUT FORMAT (strict JSON):
{
  "animation_type": "label",
  "design_strategy": "2-4 sentences",
  "concept_code": "COMPLETE <!DOCTYPE html>...</html> AS ESCAPED JSON STRING"
}

SAFETY: White/light bg only, no backticks, no external scripts, balanced tags,
5 scenes, include #prevbtn/#nextbtn/#dots, manual step control.
SUBSCRIPTS: NEVER underscores. ALWAYS <tspan dy="5" font-size="0.72em">sub</tspan>."""

# ---------------------------------------------------------------------------
# STRATEGY TEMPLATES  (v11.7 -- clarity-first descriptions)
# ---------------------------------------------------------------------------
STRATEGY_TEMPLATES = {
    "VISUAL_PHYSICS":
        "Scene 0: labeled diagram of the physical system with all given values annotated directly on the drawing. "
        "Scene 1: the governing formula in a large blue box with each symbol explained in plain English. "
        "Scene 2: animated arrows or vectors showing direction and magnitude. "
        "Scene 3: substitution layout showing formula → numbers → unknown. "
        "Scene 4: 4-step solving checklist.",

    "PROCESS_BASED":
        "Scene 0: a labeled diagram of the system/machine with parts identified. "
        "Scene 1: the key principle or law that governs this process. "
        "Scene 2: a left-to-right flow diagram showing the process stages. "
        "Scene 3: input/output labeled clearly with given values. "
        "Scene 4: numbered steps to analyze or solve this type of problem.",

    "MATHEMATICAL":
        "Scene 0: coordinate axes or geometric setup with all given values plotted/labeled. "
        "Scene 1: the main formula or theorem in a large clear blue box. "
        "Scene 2: a graph or geometric illustration of the relationship. "
        "Scene 3: the substitution layout from formula to numbers. "
        "Scene 4: step-by-step approach checklist.",

    "BIOLOGICAL":
        "Scene 0: a simple labeled diagram of the biological structure. "
        "Scene 1: the key process or mechanism labeled in plain English. "
        "Scene 2: a left-to-right flow of the biological process. "
        "Scene 3: cause and effect labeled with arrows. "
        "Scene 4: a checklist of what to identify and describe.",

    "ABSTRACT":
        "Scene 0: a real-world example that represents the abstract concept. "
        "Scene 1: the core principle stated simply in a card. "
        "Scene 2: a comparison showing what changes vs what stays constant. "
        "Scene 3: how the concept connects to the specific question. "
        "Scene 4: a step-by-step approach for this type of question.",

    "MIXED":
        "Scene 0: labeled diagram of the situation with all given values clearly shown. "
        "Scene 1: the primary formula in a large blue box, all symbols explained. "
        "Scene 2: visualization of the primary effect with labeled arrows. "
        "Scene 3: the substitution layout. "
        "Scene 4: numbered solving steps.",
}

CONCEPT_STRATEGY_TEMPLATES = STRATEGY_TEMPLATES

# ---------------------------------------------------------------------------
# SHARED PROMPT BUILDERS  (v11.7)
# ---------------------------------------------------------------------------
HTML_SHELL_NOTE = """
REQUIRED HTML ELEMENTS:
- All scenes in <g id="scene-N"> groups (scene-0 to scene-4)
- #prevbtn, #nextbtn, #dots for navigation
- #qstrip containing a .qtext element showing the question (bold)
- DO NOT include Find/Solution/Answer Box buttons
- DO NOT generate #sol-backdrop or #sol-panel
- Include empty containers: <g id="sol-steps-container"></g>
"""

CLARITY_REMINDER = """
=== v11.7 CLARITY CHECKLIST (verify before outputting) ===
[ ] Scene 0 shows ACTUAL VALUES from the question (not "m = value", but "m = 5 kg")
[ ] Given values use GREEN color (#16a34a)
[ ] Unknown values use PURPLE color (#7c3aed)
[ ] Formulas use BLUE color (#2563eb) in clearly readable boxes
[ ] Bottom info card text sounds like a friendly tutor, not a textbook
[ ] Scene headlines are 8 words or fewer in plain English
[ ] No dark backgrounds anywhere
[ ] No more than 2 animated elements per scene
[ ] final_answer field contains the fully computed numerical answer
"""


def _build_concept_prompt(question, category):
    strategy = CONCEPT_STRATEGY_TEMPLATES.get(category, CONCEPT_STRATEGY_TEMPLATES["PROCESS_BASED"])
    return f"""Build a CLEAR, STUDENT-FRIENDLY 5-scene concept animation for QAnim v11.7.

QUESTION: {question}
CATEGORY: {category}
VISUAL STRATEGY: {strategy}

{DESIGN_SYSTEM}
{SVG_TECHNIQUES}
{FALLBACK_RULES}

CONCEPT ANIMATION v11.7 REQUIREMENTS:
- White/light background always
- Exactly 5 scenes (scene-0 to scene-4)
- Scene 0 MUST show the actual context from this specific question
- Plain English headlines -- maximum 8 words
- Bottom info card per scene: rect x=30 y=420 width=940 height=130 rx=12
- Manual navigation: showScene(), animateScene0-4(), nextStep(), prevStep()
- DO NOT include Find/Solution/Answer Box buttons

{CLARITY_REMINDER}

Return ONLY raw JSON. concept_code must be complete <!DOCTYPE html>...</html> as escaped JSON string."""


def _build_prompt(question, category):
    strategy = STRATEGY_TEMPLATES.get(category, STRATEGY_TEMPLATES["PROCESS_BASED"])
    return f"""Build a CRYSTAL-CLEAR 5-scene SVG animation for QAnim v11.7.

QUESTION: {question}
CATEGORY: {category}
STRATEGY: {strategy}

{DESIGN_SYSTEM}
{SVG_TECHNIQUES}
{FALLBACK_RULES}
{HTML_SHELL_NOTE}

v11.7 KEY REQUIREMENTS:
- Scene 0 MUST display ALL actual given values from the question in green
- Scene 0 MUST display ALL unknowns from the question in purple
- Plain English scene headlines (8 words max)
- Bottom info card: rect x=30 y=420 width=940 height=130 rx=12
- final_answer: REQUIRED -- fully computed with all values and units
- Write card text like a tutor, not a textbook

{CLARITY_REMINDER}

Return ONLY raw JSON. animation_code must be complete <!DOCTYPE html>...</html> as escaped JSON string."""


# ===========================================================================
#  MODULE 15 -- EEE/ECE Domain Classifier
# ===========================================================================

class EEEClassifier:
    _COMPONENT_KEYWORDS = {
        "resistor", "capacitor", "inductor", "impedance", "reactance",
        "ohm", "farad", "henry", "resistance", "inductance", "capacitance",
        "transistor", "bjt", "mosfet", "diode", "zener", "thyristor",
        "scr", "triac", "diac", "igbt", "fet", "jfet",
        "op-amp", "opamp", "operational amplifier",
        "ac supply", "dc supply", "ac source", "dc source", "voltage source",
        "current source", "emf", "electromotive force",
        "transformer", "motor", "generator", "alternator", "synchronous",
        "induction motor", "dc motor", "servo motor", "stepper motor",
        "rectifier", "inverter", "converter", "chopper",
        "rlc", "rc circuit", "rl circuit", "lc circuit",
        "series circuit", "parallel circuit", "mesh", "nodal",
        "thevenin", "norton", "superposition", "kirchhoff",
        "kvl", "kcl", "voltage divider", "current divider",
        "wheatstone", "bridge circuit", "resonance",
        "half wave", "full wave", "bridge rectifier",
        "logic gate", "and gate", "or gate", "not gate", "nand", "nor",
        "xor", "xnor", "flip flop", "latch", "counter", "register",
        "mux", "demux", "multiplexer", "demultiplexer", "encoder", "decoder",
        "adder", "subtractor", "alu", "boolean",
        "modulation", "demodulation", "am modulation", "fm modulation",
        "pcm", "pam", "ppm", "ask", "fsk", "psk", "qam",
        "bandwidth", "antenna", "transmission line", "waveguide",
        "filter", "amplifier", "oscillator",
        "voltmeter", "ammeter", "ohmmeter", "multimeter",
        "oscilloscope", "wattmeter", "power meter",
        "power factor", "real power", "reactive power", "apparent power",
        "load flow", "fault analysis", "short circuit", "open circuit",
        "per unit", "bus bar", "feeder", "transmission",
    }

    _TOPIC_KEYWORDS = {
        "electrical", "electronics", "circuit", "voltage", "current",
        "watt", "ampere", "volt", "frequency", "hertz", "hz",
        "signal", "waveform", "phasor", "sinusoidal", "alternating",
        "direct current", "alternating current", "phase angle",
        "gain", "attenuation", "decibel", "db",
        "microcontroller", "arduino", "embedded", "fpga", "plc",
        "power supply", "battery", "charge", "discharge",
        "magnetic field", "electromagnetic", "flux", "faraday",
        "maxwell", "coulomb", "lenz",
    }

    _NON_EEE_EXCLUSIONS = {
        "photosynthesis", "mitosis", "dna", "rna", "protein", "cell membrane",
        "animal", "plant", "ecosystem", "evolution", "bacteria", "virus",
        "democracy", "capitalism", "philosophy", "literature", "history",
        "thermodynamics", "heat engine", "turbine", "internal combustion",
        "beam", "truss", "moment of inertia", "shear force", "bending moment",
        "concrete", "civil", "soil", "foundation", "surveying",
        "sorting algorithm", "data structure", "linked list", "binary tree",
        "machine learning", "neural network", "deep learning", "gradient descent",
        "regression", "clustering", "reinforcement learning",
        "chemistry", "reaction", "mole", "atom", "molecule", "bond",
    }

    @classmethod
    def is_eee_ece(cls, question: str) -> bool:
        if not question or not question.strip():
            return False
        q_lower = question.lower()
        exclusion_hits = sum(1 for term in cls._NON_EEE_EXCLUSIONS if term in q_lower)
        if exclusion_hits >= 2:
            QAnimLogger.info("EEEClassifier", f"Excluded by {exclusion_hits} non-EEE terms")
            return False
        for kw in cls._COMPONENT_KEYWORDS:
            if kw in q_lower:
                QAnimLogger.ok("EEEClassifier", f"Tier-1 match: '{kw}' -> EEE/ECE pipeline")
                return True
        topic_hits = [kw for kw in cls._TOPIC_KEYWORDS if kw in q_lower]
        if len(topic_hits) >= 2:
            QAnimLogger.ok("EEEClassifier", f"Tier-2 match: {topic_hits[:3]} -> EEE/ECE pipeline")
            return True
        if len(topic_hits) == 1:
            try:
                resp = client.messages.create(
                    model=Q_MODEL, max_tokens=10,
                    system="Reply with ONLY 'YES' if this is an Electrical/Electronics Engineering question, or 'NO' otherwise.",
                    messages=[{"role": "user", "content": f"Is this EEE/ECE: {question[:200]}"}])
                answer = resp.content[0].text.strip().upper()
                result = answer.startswith("YES")
                QAnimLogger.ok("EEEClassifier", f"Tier-3 API: '{answer}' -> {'EEE/ECE' if result else 'non-EEE'}")
                return result
            except Exception as e:
                QAnimLogger.warn("EEEClassifier", f"Tier-3 API failed: {e} -- defaulting to non-EEE")
                return False
        QAnimLogger.info("EEEClassifier", "No EEE/ECE signals detected")
        return False


# ===========================================================================
#  MODULE 16 -- Circuit Visualization Engine  (v11.7 -- clarity overhaul)
# ===========================================================================

_CIRCUIT_TOPOLOGIES = {
    "series_rlc":        ["series rlc", "rlc series", "series r l c"],
    "parallel_rlc":      ["parallel rlc", "rlc parallel", "parallel r l c"],
    "series_rc":         ["series rc", "rc circuit", "rc series"],
    "series_rl":         ["series rl", "rl circuit", "rl series"],
    "half_wave_rect":    ["half wave rectifier", "half-wave rectifier"],
    "full_wave_rect":    ["full wave rectifier", "full-wave rectifier", "bridge rectifier"],
    "ce_amplifier":      ["common emitter", "ce amplifier", "common-emitter"],
    "cb_amplifier":      ["common base", "cb amplifier"],
    "cc_amplifier":      ["common collector", "cc amplifier", "emitter follower"],
    "cs_amplifier":      ["common source", "cs amplifier"],
    "inverting_opamp":   ["inverting amplifier", "inverting op-amp", "inverting opamp"],
    "non_inv_opamp":     ["non-inverting", "non inverting amplifier"],
    "series_resonance":  ["series resonance", "resonant frequency", "resonance circuit"],
    "thevenin":          ["thevenin", "thévenin"],
    "norton":            ["norton"],
    "wheatstone":        ["wheatstone", "bridge circuit"],
    "voltage_divider":   ["voltage divider", "potential divider"],
    "transformer_basic": ["transformer", "turns ratio", "step up", "step down"],
    "logic_circuit":     ["logic gate", "boolean", "truth table", "combinational"],
    "dc_motor":          ["dc motor", "armature", "back emf"],
    "induction_motor":   ["induction motor", "slip", "synchronous speed"],
}


def _detect_circuit_topology(question: str) -> str:
    q_lower = question.lower()
    for topology, patterns in _CIRCUIT_TOPOLOGIES.items():
        for pattern in patterns:
            if pattern in q_lower:
                QAnimLogger.info("CircuitEngine", f"Topology detected: {topology}")
                return topology
    return "generic_circuit"


CIRCUIT_SYSTEM_PROMPT = """You are QAnim CircuitEngine v11.7 -- a clarity-first electrical engineering circuit animator.

YOUR MISSION: Generate a STUDENT-FRIENDLY 5-scene circuit animation.
Students must instantly understand what circuit this is and what we are calculating.

=== SCENE STRUCTURE (v11.7 CIRCUIT CLARITY MODEL) ===

Scene 0 "Here's the Circuit":
  HEADLINE (22px bold): "Here's the circuit we're analyzing"
  CENTER: Complete, clearly labeled circuit schematic
    -- Every component labeled with both its symbol AND its value from the question
    -- Example: resistor labeled "R = 20 Ω", inductor "L = 0.1 H"
    -- Supply labeled "V = 230 V, f = 50 Hz"
  LEFT CARD (green): "What we know" -- list every given value
  RIGHT CARD (purple): "What to find" -- list every unknown
  ANIMATED current flow: dashed path loops around the circuit
  BOTTOM CARD: "This is a [topology name] circuit. We know [given values] and need to find [unknowns]."

Scene 1 "The Formula":
  HEADLINE: "The formula that solves this"
  CENTER: Large blue formula box showing the main equation
    -- Written in plain text, all symbols labeled
  SECONDARY: Phasor diagram or impedance triangle IF applicable
  BOTTOM CARD: "This formula connects [variables]. Since we know [knowns], we can find [unknown]."

Scene 2 "Each Component":
  HEADLINE: "What each component does"
  CENTER: Component-by-component breakdown
    -- Each component highlighted in sequence
    -- Plain English description of its role
  BOTTOM CARD: "The [component] [does this] in the circuit."

Scene 3 "The Calculation Setup":
  HEADLINE: "Plugging in our numbers"
  CENTER: Substitution layout:
    Formula → Numbers substituted → Result placeholder
    Use color: blue formula, green numbers, purple unknown
  BOTTOM CARD: "We substitute [values] into the formula. One calculation left."

Scene 4 "Step-by-Step Approach":
  HEADLINE: "How to solve this type of problem"
  CENTER: Numbered checklist (4-5 steps), plain English
  BOTTOM CARD: "Follow these steps for any [topology] circuit problem."

=== CIRCUIT SYMBOL GUIDE ===
RESISTOR: zigzag path with 6 peaks
CAPACITOR: two parallel vertical lines separated 8px
INDUCTOR: series of 4 semicircular bumps
AC SOURCE: circle with sine wave, labeled V and Hz
DC SOURCE: circle with + and - symbols
WIRE: straight lines stroke="#475569" stroke-width="2"
GROUND: three decreasing horizontal lines

CURRENT FLOW ANIMATION (Scene 0, required):
  Dashed overlay path: stroke-dasharray="12 8"
  CSS @keyframes flow { to { stroke-dashoffset: -200; } }
  animation="flow 1.5s linear infinite"

BOTTOM INFO CARD: rect x=30 y=420 width=940 height=130 rx=12

SAFETY: Complete HTML, no external scripts, no backticks, balanced tags.
Include #prevbtn, #nextbtn, #dots, #qstrip .qtext. NO Find/Answer buttons.

OUTPUT FORMAT (strict JSON):
{
  "animation_type": "circuit_diagram",
  "design_strategy": "2-4 sentence description",
  "solution_steps": ["Step 1: ...", "Step 2: ...", ...],
  "final_answer": "<fully computed numerical answer with all values and units>",
  "key_insight": "one memorable sentence",
  "animation_code": "COMPLETE SELF-CONTAINED HTML AS ESCAPED JSON STRING"
}"""


CIRCUIT_CONCEPT_SYSTEM_PROMPT = """You are QAnim CircuitEngine v11.7 -- clarity-first circuit concept animator.

Same as main circuit engine but CONCEPT only (no computed answers in scenes).
Show circuit topology, component roles, and principles clearly.

Scene 0: Circuit topology with labeled component symbols (no specific values)
Scene 1: Core formula with plain-English explanation of every symbol
Scene 2: Component roles and behaviors
Scene 3: Phasor diagram or equivalent circuit
Scene 4: Step-by-step approach checklist

BOTTOM INFO CARDS: rect x=30 y=420 width=940 height=130 rx=12

OUTPUT FORMAT (strict JSON):
{
  "animation_type": "circuit_concept",
  "design_strategy": "2-4 sentences",
  "concept_code": "COMPLETE <!DOCTYPE html>...</html> AS ESCAPED JSON STRING"
}"""


class CircuitVisualizationEngine:

    @classmethod
    def build_circuit_prompt(cls, question: str, topology: str) -> str:
        topology_hint = cls._get_topology_hint(topology)
        return f"""Generate a CRYSTAL-CLEAR 5-scene circuit animation for QAnim v11.7.

QUESTION: {question}
DETECTED TOPOLOGY: {topology}
TOPOLOGY-SPECIFIC GUIDANCE: {topology_hint}

{DESIGN_SYSTEM}
{SVG_TECHNIQUES}
{FALLBACK_RULES}

v11.7 CIRCUIT CLARITY RULES:
- Scene 0: Label EVERY component with its VALUE from the question
- Scene 0: Green card showing all given values, purple card showing all unknowns
- All formulas in large blue boxes -- readable at a glance
- Bottom card text = plain English tutor explanation
- final_answer MUST contain the complete computed numerical answer
- Current flow animation required in Scene 0

{CLARITY_REMINDER}

Return ONLY raw JSON. animation_code must be complete HTML as escaped JSON string."""

    @classmethod
    def build_concept_prompt(cls, question: str, topology: str) -> str:
        topology_hint = cls._get_topology_hint(topology)
        return f"""Generate a CLEAR 5-scene CONCEPT circuit animation for QAnim v11.7.

QUESTION: {question}
DETECTED TOPOLOGY: {topology}
TOPOLOGY-SPECIFIC GUIDANCE: {topology_hint}

{DESIGN_SYSTEM}
{SVG_TECHNIQUES}
{FALLBACK_RULES}

Info cards at y=420, height=130.
Plain English throughout. White/light background only.

Return ONLY raw JSON. concept_code must be complete <!DOCTYPE html>...</html>."""

    @classmethod
    def _get_topology_hint(cls, topology: str) -> str:
        hints = {
            "series_rlc": (
                "Scene 0: Draw AC source → R (zigzag, label value) → L (bumps, label value) → C (plates, label value) → back to source. "
                "Label V_supply and frequency. Green given-values card: R, L, C, V, f. Purple find-card: X_L, X_C, Z, I, PF. "
                "Scene 1: Impedance formula Z = sqrt(R² + (X_L - X_C)²) in large blue box. "
                "Scene 3: Substitution showing actual numbers from question."
            ),
            "parallel_rlc": (
                "Scene 0: Vertical bus bars. Three horizontal branches: R (labeled), L (labeled), C (labeled). "
                "Label supply voltage and frequency. Green/purple info cards. "
                "Scene 1: Admittance formula or branch current formulas."
            ),
            "half_wave_rect": (
                "Scene 0: AC source → diode (triangle symbol, labeled) → R_L (labeled). "
                "Scene 1: Half-wave output formula V_dc = V_m/π. "
                "Scene 2: Input waveform (full sine) vs output waveform (half bumps)."
            ),
            "full_wave_rect": (
                "Scene 0: 4 diodes in diamond. Label each D1-D4. AC input at sides, DC output at top/bottom. "
                "Scene 1: V_dc = 2V_m/π formula. "
                "Scene 2: Current path arrows for positive and negative half-cycles (different colors)."
            ),
            "ce_amplifier": (
                "Scene 0: NPN BJT center. R_B1/R_B2 labeled with values. R_C to Vcc labeled. R_E labeled. "
                "C_E bypass capacitor. C_in/C_out coupling capacitors. All values from question shown. "
                "Scene 1: Voltage gain formula A_v = -g_m × R_C."
            ),
            "transformer_basic": (
                "Scene 0: Two coil symbols with core. Primary: N1 turns, V1 voltage (labeled with values). "
                "Secondary: N2 turns, V2 (unknown, shown in purple with ?). "
                "Scene 1: Turns ratio formula V1/V2 = N1/N2 in large blue box."
            ),
            "series_resonance": (
                "Scene 0: Series RLC with all values labeled. Supply voltage and frequency shown. "
                "Scene 1: Resonant frequency formula f_0 = 1/(2π√(LC)) in blue box. "
                "Scene 2: Frequency response curve -- dip at f_0 clearly labeled."
            ),
            "generic_circuit": (
                "Scene 0: Draw the complete circuit with every component labeled with its value from the question. "
                "Green card: all given values. Purple card: all unknowns. "
                "Scene 1: The primary governing equation in a large blue box."
            ),
        }
        return hints.get(topology, hints["generic_circuit"])


# ===========================================================================
#  TOPIC CLASSIFIER
# ===========================================================================

def _classify_topic(question):
    q = question.lower()
    scores = {
        "BIOLOGICAL":     sum(1 for k in ["cell","dna","rna","protein","photosynthesis","mitosis","enzyme","hormone","gene","organism","bacteria","virus","chromosome","metabolism"] if k in q),
        "MATHEMATICAL":   sum(1 for k in ["integral","derivative","matrix","vector","theorem","equation","polynomial","logarithm","trigonometry","calculus","function","graph","proof"] if k in q),
        "ABSTRACT":       sum(1 for k in ["philosophy","ethics","democracy","capitalism","justice","freedom","psychology","consciousness","society","ideology","culture","politics"] if k in q),
        "PROCESS_BASED":  sum(1 for k in ["how does","how do","step by step","process","algorithm","mechanism","workflow","procedure","stages","works","function","operation"] if k in q),
        "VISUAL_PHYSICS": sum(1 for k in ["force","velocity","acceleration","mass","energy","momentum","gravity","pressure","current","voltage","wave","circuit","newton","friction","torque","field","charge","resistance","heat","thermal","temperature","pipe","cylinder","conduction","convection"] if k in q),
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
            messages=[{"role": "user", "content": f"Classify: {question[:200]}"}])
        cat = resp.content[0].text.strip().upper()
        if cat in STRATEGY_TEMPLATES:
            return cat
    except Exception:
        pass
    return "PROCESS_BASED"


# ===========================================================================
#  MODULE 13 -- Full Generation Pipeline  (v11.7)
# ===========================================================================

async def _generate_concept_animation(question, category, is_eee=False, topology=None):
    QAnimLogger.info("ConceptPipeline", f"START  category={category}  is_eee={is_eee}")

    if is_eee:
        prompt = CircuitVisualizationEngine.build_concept_prompt(question, topology or "generic_circuit")
        system = CIRCUIT_CONCEPT_SYSTEM_PROMPT
        QAnimLogger.info("ConceptPipeline", "Using CircuitVisualizationEngine for EEE/ECE concept")
    else:
        prompt = _build_concept_prompt(question, category)
        system = SYSTEM_CONCEPT

    try:
        msg = client.messages.create(
            model=CONCEPT_MODEL, max_tokens=MAX_TOK_CONCEPT,
            system=system,
            messages=[{"role": "user", "content": prompt}])
        raw = msg.content[0].text.strip()
        QAnimLogger.info("ConceptAI", f"model={CONCEPT_MODEL}  stop_reason={msg.stop_reason}  len={len(raw)}")
        if msg.stop_reason == "max_tokens":
            QAnimLogger.warn("ConceptAI", "Hit max_tokens -- may be truncated!")
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
    concept_html = inject_voice_assistant(concept_html)
    concept_html = inject_step_controller(concept_html)

    QAnimLogger.ok("ConceptPipeline", f"DONE -- len={len(concept_html):,}")
    return concept_html


async def generate_question_animation(question):
    """
    THREE-STAGE CONCURRENT PIPELINE (v11.7):

    Stage 0 -- ToFind Extraction   (sync, no AI)
    Stage 1 -- Concept Animation   (claude-sonnet-4-6)
    Stage 2 -- Solution Animation  (claude-sonnet-4-6)
    Stage 3 -- Solution Steps      (claude-sonnet-4-6)
               [Stages 1-3 run concurrently via asyncio.gather]

    v11.7 changes vs v11.6:
      - Complete clarity overhaul of all system prompts
      - Scene 0 now always shows actual question values (given/unknown)
      - Semantic color system enforced (green=known, purple=unknown, blue=formula)
      - Plain English headlines and card text throughout
      - Simplified animations (max 2 moving elements per scene)
      - Circuit animations label every component with its actual value
    """
    question = (question or "").strip()
    if not question:
        raise ValueError("Question cannot be empty")

    short_q = question[:80] + ("..." if len(question) > 80 else "")
    QAnimLogger.info("Pipeline", f"START v11.7 -- '{short_q}'")

    to_find_targets = ToFindExtractor.extract(question)
    QAnimLogger.info("Pipeline", f"ToFind: {to_find_targets}")

    is_eee = EEEClassifier.is_eee_ece(question)
    if is_eee:
        topology = _detect_circuit_topology(question)
        QAnimLogger.ok("Pipeline", f"EEE/ECE detected -> CircuitVisualizationEngine (topology={topology})")
    else:
        topology = None
        QAnimLogger.info("Pipeline", "Non-EEE/ECE question -> standard animation pipeline")

    category = _classify_topic(question)
    QAnimLogger.info("Classifier", f"Category: {category}")

    if is_eee:
        solution_prompt = CircuitVisualizationEngine.build_circuit_prompt(question, topology)
        solution_system = CIRCUIT_SYSTEM_PROMPT
    else:
        solution_prompt = _build_prompt(question, category)
        solution_system = SYSTEM

    async def _run_solution_ai():
        try:
            msg = client.messages.create(
                model=SOLUTION_MODEL, max_tokens=MAX_TOK,
                system=solution_system,
                messages=[{"role": "user", "content": solution_prompt}])
            raw = msg.content[0].text.strip()
            engine_tag = "CircuitAI" if is_eee else "SolutionAI"
            QAnimLogger.info(engine_tag, f"model={SOLUTION_MODEL}  stop_reason={msg.stop_reason}  len={len(raw)}")
            if msg.stop_reason == "max_tokens":
                QAnimLogger.warn(engine_tag, "Hit max_tokens -- may be truncated!")
            return raw
        except Exception as e:
            QAnimLogger.error("SolutionAI", f"API failed: {e}")
            raise

    QAnimLogger.info("Pipeline", "Launching 3 concurrent AI stages...")
    try:
        concept_html, sol_raw, haiku_sol = await asyncio.gather(
            _generate_concept_animation(question, category, is_eee=is_eee, topology=topology),
            _run_solution_ai(),
            HaikuSolutionGenerator.generate_async(question),
        )
    except Exception as e:
        QAnimLogger.error("Pipeline", f"Concurrent generation failed: {e}")
        return _build_failure_result(question, f"API error: {e}")

    result = _parse_response(sol_raw, question)
    result["category"]               = category
    result["engine_version"]         = "v11.7"
    result["engine_type"]            = "circuit" if is_eee else "standard"
    result["circuit_topology"]       = topology or "N/A"
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
            result["final_answer"] = "Solution computed -- open the step-by-step for full details."
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

    # POST-PROCESSING: inject all panels
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
    html = inject_voice_assistant(html)
    html = inject_step_controller(html)  # MUST be absolute last

    try:
        GenerationValidator.validate(html, require_svg=True)
    except ValidationError as e:
        QAnimLogger.warn("FinalValidator", f"Post-injection: {e} -- continuing")

    result["animation_code"] = html
    result["render_status"]  = "ok"

    QAnimLogger.ok("Pipeline", (
        f"DONE v11.7 -- '{result['title']}' "
        f"engine={result.get('engine_type','standard')} "
        f"topology={result.get('circuit_topology','N/A')} "
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
        "engine_version":         "v11.7",
        "engine_type":            "error",
        "circuit_topology":       "N/A",
        "render_status":          "error",
    }


def generate_question_animation_sync(question):
    return asyncio.run(generate_question_animation(question))


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
        "EEE_SERIES_RLC":  "A 230 V supply is connected to a circuit containing a 20 Ω resistor, a 0.1 H inductor, and a 100 μF capacitor in series. Supply frequency is 50 Hz. Calculate inductive reactance, capacitive reactance, total impedance, circuit current, and power factor.",
        "EEE_TRANSFORMER": "A single-phase transformer has 200 primary turns and 50 secondary turns. The primary is connected to a 400 V, 50 Hz supply. Find the secondary voltage, turns ratio, and transformation ratio.",
    }

    if len(sys.argv) > 1:
        questions_to_test = {"CUSTOM": " ".join(sys.argv[1:])}
    else:
        key = "VISUAL_PHYSICS"
        questions_to_test = {key: TEST_QUESTIONS[key]}

    for cat, q in questions_to_test.items():
        print("=" * 72)
        print(f"  QAnim v11.7 -- Clarity Overhaul | {cat}")
        print(f"  Q: {q[:65]}...")
        print("=" * 72)

        result = generate_question_animation_sync(q)

        concept_html  = result.get("concept_animation_code", "")
        solution_html = result.get("animation_code", "")

        print(f"\nTitle               : {result['title']}")
        print(f"Engine              : {result.get('engine_version','N/A')} / {result.get('engine_type','standard')}")
        print(f"Render Status       : {result.get('render_status','N/A')}")
        print(f"[Stage 1] Concept   : {len(concept_html):,} chars")
        print(f"[Stage 2] Solution  : {len(solution_html):,} chars")
        print(f"Final Answer        : {result.get('final_answer','')[:120]}")

        slug = cat.lower()
        concept_out  = f"q_anim_v117_{slug}_concept.html"
        solution_out = f"q_anim_v117_{slug}_solution.html"

        with open(concept_out,  "w", encoding="utf-8") as f: f.write(concept_html)
        with open(solution_out, "w", encoding="utf-8") as f: f.write(solution_html)

        print(f"\n[Stage 1] Concept saved  : {concept_out}")
        print(f"[Stage 2] Solution saved : {solution_out}")
        print()
        print("QAnim v11.7 Clarity Changes:")
        print("  IMPROVED -- Scene 0 now shows actual question values (given/unknown)")
        print("  IMPROVED -- Semantic colors: green=known, purple=unknown, blue=formula")
        print("  IMPROVED -- Plain English scene headlines (8 words max)")
        print("  IMPROVED -- Bottom cards written as tutor dialogue, not textbook")
        print("  IMPROVED -- Circuit scenes label every component with its actual value")
        print("  IMPROVED -- Max 2 animated elements per scene (reduced visual noise)")
        print("  IMPROVED -- CLARITY_REMINDER checklist enforced in all prompts")
        print()
        print("Controls bar: [◀ Prev] [Find] [Final Answer] [Answer Box] [Notes] [Voice] [Next ▶]")
