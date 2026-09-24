"""
simulation.py -- Simulation Creator Engine  v2.1
====================================================================
PURPOSE
  Turn a user-supplied topic / concept / lab experiment (e.g.
  "Snell's Law", "RC circuit charging", "projectile motion",
  "population growth model", "binary search visualization") into a
  single, self-contained, interactive HTML5 simulation:

    - A live <canvas> (or inline SVG for purely diagrammatic topics)
      driven by sliders / toggles / dropdowns in a control panel
    - Real-time redraw on every input change (no fixed "scenes",
      no prev/next narration -- this is a LAB, not a slideshow)
    - A metrics strip that reports live computed values
    - Professional, distraction-free chrome (sidebar/header + canvas
      + metrics), matching the visual quality bar of a hand-built
      virtual-lab page
    - Google Image Search used as a visual-reference step so the
      model understands real instrument / diagram aesthetics before
      generating the simulation

ROOT CAUSE FIX (v2.1.7) -- dead Play button + dead tutorial:
  Generated scripts called bindSlider() (which fires onChange synchronously) before
  `const ctx` was declared, so draw() threw a temporal-dead-zone ReferenceError.  The
  v2.1 try/catch "error boundary" then (a) swallowed the error -- its fallback element never
  existed -- and (b) turned every declaration into block scope, so togglePlay() and all
  later listener wiring (Play, Reset, tutorial, "?" button, launch timer) never ran.
  Fixes: no more try{} wrapper (legacy wrappers are stripped); a real JS lexer finds and
  hoists TDZ hazards for ANY variable (not just `playing`); a visible non-swallowing error
  banner; a canvas shim resolving var(--x) colours; the tutorial engine is now injected by
  Python (deterministic, independent of the model's script); the prompt teaches an
  A/B/init()/bootstrap script architecture, addEventListener-only wiring and setTransform().

ROOT CAUSE FIX (v2.1.6):
  Google's own API error message confirmed:
  'This model models/gemini-2.5-pro is no longer available to new users.
   Please update your code to use models/gemini-3.1-pro-preview'

  Fix applied:
    1. Default SIM_MODEL = 'gemini-3.1-pro-preview' (Google-confirmed replacement)
    2. Removed gemini-3.1 from _BAD_MODEL_PATTERNS (it IS the correct model)
    3. AutomaticFunctionCallingConfig removed (caused silent failures)
    4. ThinkingConfig with try/except fallback added (mirrors q_animation pattern)
"""

import os
import re
import json
import time
import asyncio
import urllib.request
import urllib.parse
import html as html_module
from typing import Optional, List

from google import genai as _google_genai
# pyrefly: ignore [missing-import]
from google.genai import types as _genai_types

# ---------------------------------------------------------------------------
# Client + model routing  (v2.1.5 — self-healing model ID)
# ---------------------------------------------------------------------------

CLIENT_TIMEOUT_SECONDS   = float(os.environ.get("SIM_CLIENT_TIMEOUT_SECONDS", "300"))
CLIENT_MAX_RETRIES       = int(os.environ.get("SIM_CLIENT_MAX_RETRIES", "0"))
PIPELINE_TIMEOUT_SECONDS = float(os.environ.get("SIM_PIPELINE_TIMEOUT_SECONDS", "310"))

# BUG FIX: Cap MAX_TOK to prevent excessive timeouts, but allow enough tokens
# for very complex simulations (which can exceed 15k tokens).
_MAX_TOK_HARD_CAP = 32768
_max_tok_raw = int(os.environ.get("SIM_MAX_TOKENS", "32768"))
if _max_tok_raw > _MAX_TOK_HARD_CAP:
    print(
        f"[SimEngine] ⚠  SIM_MAX_TOKENS={_max_tok_raw} exceeds hard cap of {_MAX_TOK_HARD_CAP}. "
        f"Clamped to {_MAX_TOK_HARD_CAP}. Update Railway Variable SIM_MAX_TOKENS to fix this."
    )
MAX_TOK             = min(_max_tok_raw, _MAX_TOK_HARD_CAP)
MAX_TOK_CLASSIFIER  = 20


GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID  = os.environ.get("GOOGLE_CSE_ID", "")

# Safe defaults — use model IDs confirmed working on Google AI Studio API keys.
# Google API confirmed: gemini-3.1-pro-preview is the replacement for gemini-2.5-pro.
_SAFE_DEFAULT_SIM_MODEL        = "gemini-3.1-pro-preview"
_SAFE_DEFAULT_CLASSIFIER_MODEL = "gemini-3.1-pro-preview"


# Confirmed working: gemini-3.1-pro-preview (Google's own API error message said to use it)
# Patterns known to cause 404 — only flag genuinely bad IDs, NOT gemini-3.1.
_BAD_MODEL_PATTERNS = [
    # Date-suffix preview variants — retired periodically by Google
    (r"gemini-[\d.]+-pro-preview-\d{2}-\d{2}",  "gemini-3.1-pro-preview"),
    (r"gemini-[\d.]+-pro-preview-\d{2}",         "gemini-3.1-pro-preview"),
    (r"gemini-[\d.]+-flash-preview-\d{2}-\d{2}", "gemini-3.1-pro-preview"),
    (r"gemini-[\d.]+-flash-preview-\d{2}",       "gemini-3.1-pro-preview"),
    # Anthropic model IDs (switched away from Claude)
    (r"claude-",                                  _SAFE_DEFAULT_SIM_MODEL),
]


def _sanitize_model_id(model_id: str, role: str = "SIM_MODEL") -> str:
    """
    Detect and auto-correct known-bad model IDs at import time.
    Returns the original string unchanged if it looks fine.
    """
    for pattern, replacement in _BAD_MODEL_PATTERNS:
        if re.search(pattern, model_id, re.IGNORECASE):
            print(
                f"[SimEngine] ⚠  {role}='{model_id}' matches a known-bad pattern "
                f"→ auto-corrected to '{replacement}'. "
                f"Update your SIM_MODEL env var to silence this warning."
            )
            return replacement
    return model_id


# Resolve model IDs at import time so a stale env var self-heals.
SIM_MODEL = _sanitize_model_id(
    os.environ.get("SIM_MODEL", _SAFE_DEFAULT_SIM_MODEL),
    role="SIM_MODEL",
)
CLASSIFIER_MODEL = _sanitize_model_id(
    os.environ.get("SIM_CLASSIFIER_MODEL", _SAFE_DEFAULT_CLASSIFIER_MODEL),
    role="SIM_CLASSIFIER_MODEL",
)
print(f"[SimEngine] Model configured: SIM_MODEL='{SIM_MODEL}', CLASSIFIER='{CLASSIFIER_MODEL}'")

# Gemini async client.
# NOTE: Do NOT set api_version here — the SDK default (v1beta) supports
# gemini-3.1-pro-preview and all current Gemini models on Google AI Studio API keys.
_gemini_client = _google_genai.Client(
    api_key=os.environ.get("GEMINI_API_KEY") or GOOGLE_API_KEY,
    http_options=_genai_types.HttpOptions(
        timeout=int(CLIENT_TIMEOUT_SECONDS * 1000),
    ),
)


# ===========================================================================
#  MODULE 1 -- SimLogger
# ===========================================================================
class SimLogger:
    PREFIX = "[SimEngine v2.1]"

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
        (r'\bfetch\s*\(',          "Network fetch() call -- must be offline-only"),
        (r'\bXMLHttpRequest\b',    "XHR call -- must be offline-only"),
        (r'localStorage\s*\.',     "localStorage not allowed"),
        (r'sessionStorage\s*\.',   "sessionStorage not allowed"),
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
    def repair(cls, html: str) -> str:
        """
        Auto-repair common truncation artifacts from LLM output.
        Appends missing closing tags so a nearly-complete simulation
        is not thrown away entirely.
        """
        h = html.rstrip()
        # If </html> is missing, try to add it
        if '</html>' not in h.lower():
            # Add </body> if also missing
            if '</body>' not in h.lower():
                # Close any open script tag first
                open_scripts  = len(re.findall(r'<script(?:\s[^>]*)?>',  h, re.IGNORECASE))
                close_scripts = len(re.findall(r'</script>',              h, re.IGNORECASE))
                if open_scripts > close_scripts:
                    h += '\n</script>'
                h += '\n</body>'
            h += '\n</html>'
        elif '</body>' not in h.lower():
            # Has </html> but missing </body> — insert before </html>
            h = re.sub(r'</html>', '\n</body>\n</html>', h, flags=re.IGNORECASE)
        return h

    @classmethod
    def lint(cls, html):
        """Non-fatal structural warnings about patterns that historically broke interactivity."""
        out = []
        if re.search(r'<[a-z][^>]*\son(?:click|input|change|keydown|pointerdown|mousedown)\s*=', html, re.IGNORECASE):
            out.append("Inline on*= handler attributes found -- handlers should use addEventListener inside init()")
        for m in HtmlSanitizer._SCRIPT_RE.finditer(html):
            if not HtmlSanitizer._is_model_js(m.group(1)):
                continue
            body = m.group(2)
            if JsToolkit.has_tdz_hazard(body):
                out.append("Unresolved temporal-dead-zone hazard remains in a script")
            if re.search(r'^\s*try\s*\{', body):
                out.append("Whole script is wrapped in try{} -- declarations become block-scoped")
        if re.search(r'\.scale\(\s*dpr\s*,\s*dpr\s*\)', html):
            out.append("ctx.scale(dpr, dpr) found -- use setTransform")
        return out

    @classmethod
    def validate(cls, html, require_svg=False, require_canvas=False):
        if not html or not html.strip():
            raise ValidationError("simulation_code is empty")
        if len(html) < 500:
            raise ValidationError(f"simulation_code suspiciously short ({len(html)} chars)")
        # Check required structural elements — repaired before reaching here
        must_have = [
            ("<!DOCTYPE", "Missing DOCTYPE declaration"),
            ("<html",     "Missing <html> tag"),
            ("<body",     "Missing <body> tag"),
            ("<script",   "No script block"),
        ]
        for pattern, reason in must_have:
            if pattern not in html:
                raise ValidationError(reason)
        if require_svg:
            for pattern, reason in cls.SVG_REQUIRED:
                if pattern not in html:
                    raise ValidationError(reason)
        if require_canvas and "<canvas" not in html:
            raise ValidationError("No <canvas> element found")
        for pattern, reason in cls.DANGEROUS_PATTERNS:
            if re.search(pattern, html, re.IGNORECASE):
                SimLogger.warn("Validator", f"Dangerous pattern: {reason}")
        if "tut-root" not in html:
            SimLogger.warn("Validator", "Onboarding tutorial missing (no #tut-root) -- "
                                         "TutorialEngine.inject() did not run or was skipped")
        for w in cls.lint(html):
            SimLogger.warn("Validator", w)
        open_scripts  = len(re.findall(r'<script(?:\s[^>]*)?>',  html, re.IGNORECASE))
        close_scripts = len(re.findall(r'</script>',              html, re.IGNORECASE))
        if open_scripts != close_scripts:
            raise ValidationError(f"Unbalanced <script> tags: {open_scripts} open, {close_scripts} close")
        SimLogger.ok("Validator", f"HTML passed validation ({len(html):,} chars)")



# ===========================================================================
#  MODULE 2b -- JsToolkit  (tiny JS lexer + top-level statement analysis)
# ===========================================================================
# Not a JS parser.  It understands exactly enough of the language to:
#   * tell code apart from comments / strings / template literals / regex
#     literals (so a stray apostrophe in a comment can never corrupt code)
#   * split a script into TOP-LEVEL statements
#   * find and repair temporal-dead-zone (TDZ) hazards: a top-level statement
#     that (directly or through function bodies) touches a let/const/class
#     which is declared LATER in the file.
# Every entry point fails SAFE: if anything looks unfamiliar it returns the
# input unchanged instead of guessing.
# ===========================================================================
class JsToolkit:
    _PUNCT = sorted([
        '>>>=', '...', '===', '!==', '**=', '<<=', '>>=', '>>>', '&&=', '||=', '??=',
        '=>', '==', '!=', '<=', '>=', '&&', '||', '??', '?.', '++', '--', '+=', '-=',
        '*=', '/=', '%=', '&=', '|=', '^=', '**', '<<', '>>'], key=len, reverse=True)
    _ID_RE  = re.compile(r'[A-Za-z_$][\w$]*')
    _NUM_RE = re.compile(r'(?:0[xXbBoO][0-9a-fA-F_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?)n?')
    _REGEX_AFTER_KW = {'return', 'typeof', 'case', 'in', 'of', 'delete', 'void', 'throw',
                       'new', 'else', 'do', 'instanceof', 'yield', 'await'}
    # tokens after which a NEWLINE does not end a statement
    _CONT_PREV = {'=', '+', '-', '*', '/', '%', '**', '&', '|', '^', '&&', '||', '??', '!', '~',
                  '<', '>', '<=', '>=', '==', '!=', '===', '!==', '?', ':', ',', '.', '?.', '=>',
                  '+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=', '**=', '<<=', '>>=', '>>>=',
                  '&&=', '||=', '??=', '<<', '>>', '>>>', '...',
                  'const', 'let', 'var', 'new', 'typeof', 'in', 'of', 'instanceof', 'delete',
                  'void', 'await', 'yield', 'else', 'do', 'case', 'extends'}
    # tokens that, at the start of the next line, continue the previous statement
    _CONT_NEXT = {'.', '?.', ',', '(', '[', '=', '+', '-', '*', '/', '%', '**', '&', '|', '^',
                  '&&', '||', '??', '?', ':', '<', '>', '<=', '>=', '==', '!=', '===', '!==',
                  '=>', '+=', '-=', '*=', '/=', '%=', '&=', '|=', '^=', 'in', 'instanceof', 'of'}

    # ------------------------------------------------------------------ lexer
    @classmethod
    def _scan_string(cls, s, i, n, max_raw_nl):
        q, j, raw = s[i], i + 1, 0
        while j < n:
            c = s[j]
            if c == '\\':
                j += 2
                continue
            if c == q:
                return j + 1
            if c == '\n':
                raw += 1
                if raw > max_raw_nl:
                    return None
            j += 1
        return None

    @classmethod
    def _scan_regex(cls, s, i, n):
        j, in_cls = i + 1, False
        while j < n:
            c = s[j]
            if c == '\n':
                return None
            if c == '\\':
                j += 2
                continue
            if in_cls:
                if c == ']':
                    in_cls = False
            elif c == '[':
                in_cls = True
            elif c == '/':
                j += 1
                while j < n and s[j].isalpha():
                    j += 1
                return j
            j += 1
        return None

    @classmethod
    def lex(cls, s, i=0, stop_at_close_brace=False, max_raw_nl=3):
        """
        Returns (tokens, end_index).  token = (kind, start, end, inner)
        kinds: ws nl comment str tpl regex id num punct.  `inner` is only used
        by 'tpl' (tokens found inside ${...}) and by comments containing a
        newline (kind 'comment_nl').
        """
        n, toks, prev, depth = len(s), [], None, 0

        def regex_ok(p):
            if p is None:
                return True
            k, t = p
            if k == 'id':
                return t in cls._REGEX_AFTER_KW
            if k in ('num', 'str', 'tpl', 'regex'):
                return False
            return t not in (')', ']', '}', '++', '--')

        while i < n:
            c = s[i]
            if c == '\n':
                toks.append(('nl', i, i + 1, None)); i += 1; continue
            if c.isspace():
                j = i + 1
                while j < n and s[j].isspace() and s[j] != '\n':
                    j += 1
                toks.append(('ws', i, j, None)); i = j; continue
            if c == '/' and i + 1 < n and s[i + 1] == '/':
                j = s.find('\n', i)
                j = n if j == -1 else j
                toks.append(('comment', i, j, None)); i = j; continue
            if c == '/' and i + 1 < n and s[i + 1] == '*':
                j = s.find('*/', i + 2)
                j = n if j == -1 else j + 2
                toks.append(('comment_nl' if '\n' in s[i:j] else 'comment', i, j, None)); i = j; continue
            if c in '\'"':
                j = cls._scan_string(s, i, n, max_raw_nl)
                if j is not None:
                    toks.append(('str', i, j, None)); prev = ('str', s[i:j]); i = j; continue
                toks.append(('punct', i, i + 1, None)); prev = ('punct', c); i += 1; continue
            if c == '`':
                j, inner, ok = i + 1, [], True
                while j < n:
                    d = s[j]
                    if d == '\\':
                        j += 2; continue
                    if d == '`':
                        break
                    if d == '$' and j + 1 < n and s[j + 1] == '{':
                        sub, k = cls.lex(s, j + 2, True, max_raw_nl)
                        inner.extend(sub); j = k + 1; continue
                    j += 1
                else:
                    ok = False
                if ok:
                    toks.append(('tpl', i, j + 1, inner)); prev = ('tpl', ''); i = j + 1; continue
                toks.append(('punct', i, i + 1, None)); prev = ('punct', c); i += 1; continue
            if c == '/' and regex_ok(prev):
                j = cls._scan_regex(s, i, n)
                if j is not None:
                    toks.append(('regex', i, j, None)); prev = ('regex', ''); i = j; continue
            m = cls._ID_RE.match(s, i)
            if m:
                toks.append(('id', i, m.end(), None)); prev = ('id', m.group()); i = m.end(); continue
            m = cls._NUM_RE.match(s, i) if (c.isdigit() or (c == '.' and i + 1 < n and s[i + 1].isdigit())) else None
            if m and m.end() > i:
                toks.append(('num', i, m.end(), None)); prev = ('num', ''); i = m.end(); continue
            op = next((p for p in cls._PUNCT if s.startswith(p, i)), c)
            if stop_at_close_brace:
                if op == '{':
                    depth += 1
                elif op == '}':
                    if depth == 0:
                        return toks, i
                    depth -= 1
            toks.append(('punct', i, i + len(op), None)); prev = ('punct', op); i += len(op)
        return toks, i

    # ------------------------------------------------- newline-in-string fix
    @classmethod
    def fix_raw_newlines_in_strings(cls, s):
        """Escape raw newlines/tabs that sit inside '...' / "..." literals (code only)."""
        toks, _ = cls.lex(s)
        out, last, changed = [], 0, False

        def walk(tl):
            for k, a, b, inner in tl:
                if k == 'str':
                    body = s[a:b]
                    if '\n' in body or '\t' in body:
                        fixed = body.replace('\\\n', '\x00').replace('\n', '\\n').replace('\t', '\\t').replace('\x00', '\\\n')
                        yield a, b, fixed
                elif k == 'tpl' and inner:
                    yield from walk(inner)

        for a, b, fixed in sorted(walk(toks)):
            out.append(s[last:a]); out.append(fixed); last = b; changed = True
        out.append(s[last:])
        return ''.join(out), changed

    # ------------------------------------------------- statement splitting
    _CONTROL = {'if', 'for', 'while', 'switch', 'try', 'do', 'with'}

    @classmethod
    def _flatten_ids(cls, tl, s):
        """identifier names used (not property names) in a token list, incl. template ${}."""
        out, prev = set(), None
        for k, a, b, inner in tl:
            if k in ('ws', 'nl', 'comment', 'comment_nl'):
                continue
            if k == 'id' and not (prev and prev[0] == 'punct' and prev[1] in ('.', '?.')):
                out.add(s[a:b])
            elif k == 'tpl' and inner:
                out |= cls._flatten_ids(inner, s)
            prev = (k, s[a:b] if k == 'punct' else '')
        return out

    @classmethod
    def split_statements(cls, s, toks):
        """
        -> list of dicts {start,end,kind,names,ids,first} or None (bail out).
        kind: FUNC | CLASS | DECL | EXEC
        """
        sig, nl_flag, pending_nl = [], [], False
        for t in toks:
            if t[0] in ('ws', 'comment'):
                continue
            if t[0] in ('nl', 'comment_nl'):
                pending_nl = True
                continue
            sig.append(t); nl_flag.append(pending_nl); pending_nl = False
        n, stmts, i = len(sig), [], 0
        text = lambda t: s[t[1]:t[2]]

        while i < n:
            first = text(sig[i]) if sig[i][0] in ('id', 'punct') else ''
            second = text(sig[i + 1]) if i + 1 < n else ''
            if first in ('import', 'export') or (sig[i][0] == 'id' and second == ':' and first not in ('default',)):
                return None
            if sig[i][0] == 'punct' and first in (')', ']', '}', ',', '.', '=', '?', ':'):
                return None
            is_async_fn = first == 'async' and second == 'function'
            control = first in cls._CONTROL
            block_style = control or first in ('function', 'class', '{') or is_async_fn
            j, depth, need_brace, prev_kw = i, 0, False, ""
            end = None
            while j < n:
                k = sig[j][0]; t = text(sig[j]) if k in ('id', 'punct') else ''
                if depth == 0 and j > i and nl_flag[j] and not block_style:
                    p = sig[j - 1]; pt = text(p) if p[0] in ('id', 'punct') else ''
                    nxt_cont = (t in cls._CONT_NEXT and k in ('punct', 'id')) or k == 'tpl'
                    if pt not in cls._CONT_PREV and not nxt_cont and t not in ('else', 'catch', 'finally'):
                        end = j; break
                if need_brace and depth == 0:
                    if t == '{':
                        need_brace = False
                    elif t == 'if' and prev_kw == 'else':
                        need_brace = False
                    else:
                        return None            # un-braced control-flow body: don't touch
                if k == 'punct' and t in ('(', '[', '{'):
                    depth += 1
                elif k == 'punct' and t in (')', ']', '}'):
                    depth -= 1
                    if depth < 0:
                        return None
                    if depth == 0:
                        if t == ')' and control:
                            need_brace = True
                        if t == '}' and block_style:
                            nxt = text(sig[j + 1]) if j + 1 < n else ''
                            cont = (first == 'if' and nxt == 'else') or (first == 'try' and nxt in ('catch', 'finally')) \
                                   or (first == 'do' and nxt == 'while')
                            # `else`/`catch`/`finally` chains of an if/try that started earlier in this statement
                            if not cont and control and nxt in ('else', 'catch', 'finally') and first in ('if', 'try'):
                                cont = True
                            if not cont:
                                end = j + 1; break
                            if first == 'do' and nxt == 'while':
                                block_style = False; first = 'do-tail'
                elif k == 'punct' and t == ';' and depth == 0:
                    end = j + 1; break
                prev_kw = t if k == 'id' else ''
                if depth == 0 and k == 'id' and t in ('else', 'try', 'finally', 'do') and control:
                    need_brace = True
                    if t == 'else':
                        prev_kw = 'else'
                j += 1
            if end is None:
                end = j
            if need_brace and end >= n and control:
                return None
            toks_stmt = sig[i:end]
            st = {'start': toks_stmt[0][1], 'end': toks_stmt[-1][2], 'first': first,
                  'kind': 'EXEC', 'names': set(), 'ids': set(), 'has_init': True}
            st['ids'] = cls._flatten_ids(toks_stmt, s)
            if first == 'function' or is_async_fn:
                st['kind'] = 'FUNC'
                idx = 1 if first == 'function' else 2
                if idx < len(toks_stmt) and text(toks_stmt[idx]) == '*':
                    idx += 1
                if idx < len(toks_stmt) and toks_stmt[idx][0] == 'id':
                    st['names'] = {text(toks_stmt[idx])}
            elif first == 'class':
                st['kind'] = 'CLASS'
                if len(toks_stmt) > 1 and toks_stmt[1][0] == 'id':
                    st['names'] = {text(toks_stmt[1])}
            elif first in ('let', 'const', 'var') and sig[i][0] == 'id':
                st['kind'] = 'DECL'
                st['names'], st['has_init'] = cls._decl_names(s, toks_stmt)
                st['decl_kw'] = first
            stmts.append(st)
            i = end
        return stmts

    @classmethod
    def _decl_names(cls, s, tl):
        """names bound by a let/const/var statement, and whether any declarator has an initializer."""
        names, has_init, depth, expect_target, in_pat = set(), False, 0, True, False
        pat_depth = 0
        for idx in range(1, len(tl)):
            k, a, b, _ = tl[idx]
            t = s[a:b] if k in ('id', 'punct') else ''
            if k == 'punct' and t in ('(', '[', '{'):
                if expect_target and depth == 0 and t in ('[', '{'):
                    in_pat = True
                depth += 1; continue
            if k == 'punct' and t in (')', ']', '}'):
                depth -= 1
                if depth == 0:
                    in_pat = False
                    expect_target = False
                continue
            if depth == 0 and k == 'punct' and t == ',':
                expect_target = True; continue
            if depth == 0 and k == 'punct' and t == '=':
                has_init = True; expect_target = False; continue
            if depth == 0 and expect_target and k == 'id':
                names.add(t); expect_target = False; continue
            if in_pat and k == 'id':
                nxt = s[tl[idx + 1][1]:tl[idx + 1][2]] if idx + 1 < len(tl) else ''
                if nxt != ':':
                    names.add(t)
        return names, has_init

    # ------------------------------------------------------- TDZ hoisting
    @classmethod
    def _find_hazard(cls, stmts):
        funcs = {}
        for st in stmts:
            if st['kind'] == 'FUNC':
                for nm in st['names']:
                    funcs[nm] = st['ids']
        decl_idx = {}
        for i, st in enumerate(stmts):
            if st['kind'] == 'CLASS' or (st['kind'] == 'DECL' and (st['decl_kw'] != 'var' or st['has_init'])):
                for nm in st['names']:
                    decl_idx.setdefault(nm, i)

        def reach(ids, at):
            seen, work, hz = set(), list(ids), set()
            while work:
                nm = work.pop()
                if nm in seen:
                    continue
                seen.add(nm)
                if nm in funcs:
                    work.extend(funcs[nm])
                di = decl_idx.get(nm)
                if di is not None:
                    if di > at:
                        hz.add(nm)
                    elif di < at:
                        work.extend(stmts[di]['ids'])
            return hz

        for i, st in enumerate(stmts):
            if st['kind'] == 'FUNC':
                continue
            hz = reach(st['ids'] - (st['names'] if st['kind'] in ('DECL', 'CLASS') else set()), i)
            if not hz:
                continue
            move = {decl_idx[h] for h in hz}
            queue = list(move)
            while queue:
                d = queue.pop()
                for h in reach(stmts[d]['ids'] - stmts[d]['names'], i):
                    di = decl_idx[h]
                    if di not in move:
                        move.add(di); queue.append(di)
            return i, sorted(move), sorted(hz)
        return None

    @classmethod
    def hoist_tdz_declarations(cls, s, max_rounds=10):
        """
        Returns (new_source, moved_names).  Moves let/const/class declarations that are
        referenced (directly or via function bodies) by an EARLIER top-level statement up to
        just before that statement, so nothing runs against an uninitialised binding.
        """
        moved_all = []
        for _ in range(max_rounds):
            toks, _end = cls.lex(s)
            stmts = cls.split_statements(s, toks)
            if stmts is None:
                return s, moved_all
            hz = cls._find_hazard(stmts)
            if hz is None:
                return s, moved_all
            at, move, names = hz
            e = stmts[at]
            line_start = s.rfind('\n', 0, e['start']) + 1
            indent = re.match(r'[ \t]*', s[line_start:e['start']]).group() if s[line_start:e['start']].strip() == '' else ''
            block = []
            for mi in move:
                m = stmts[mi]
                txt = s[m['start']:m['end']].rstrip()
                if not txt.endswith(';'):
                    txt += ';'
                block.append(indent + txt)
            insert = (indent + '/* SimEngine: declarations hoisted above first use (avoids temporal-dead-zone errors) */\n'
                      + '\n'.join(block) + '\n')
            for mi in sorted(move, reverse=True):
                m = stmts[mi]
                a, b = m['start'], m['end']
                ls = s.rfind('\n', 0, a) + 1
                if s[ls:a].strip() == '':
                    a = ls
                    rest = b
                    while rest < len(s) and s[rest] in ' \t':
                        rest += 1
                    if rest < len(s) and s[rest] == '\n':
                        b = rest + 1
                s = s[:a] + s[b:]
                if a < line_start:
                    line_start -= (b - a)
            s = s[:line_start] + insert + s[line_start:]
            for mi in move:
                moved_all.extend(sorted(stmts[mi]['names']))
        return s, moved_all

    @classmethod
    def has_tdz_hazard(cls, s):
        toks, _ = cls.lex(s)
        stmts = cls.split_statements(s, toks)
        return bool(stmts) and cls._find_hazard(stmts) is not None


# ===========================================================================
#  MODULE 3b -- RuntimeGuard + TutorialEngine  (platform-owned, deterministic)
# ===========================================================================
class RuntimeGuard:
    """
    Tiny <head> script injected BEFORE the generated code.
      1. Visible error banner.  It never swallows anything: window 'error' /
         'unhandledrejection' still reach the console; the banner only makes the
         failure visible instead of leaving a dead-looking page.
      2. Canvas colour shim.  Canvas 2D cannot resolve CSS variables, so
         `ctx.fillStyle = 'var(--red)'` is silently ignored (draws black).  The shim
         resolves var(--x[, fallback]) at assignment time.
    """
    MARK = 'sim-runtime-guard'
    JS = r"""
(function () {
  'use strict';
  if (window.__simRuntimeGuard) return;
  window.__simRuntimeGuard = true;

  /* ---------- 1. visible (non-swallowing) error banner ---------- */
  var box = null, list = null, queue = [];
  function build() {
    if (box || !document.body) return;
    box = document.createElement('div');
    box.id = 'sim-error-banner';
    box.setAttribute('role', 'alert');
    box.style.cssText = 'position:fixed;left:8px;right:8px;top:8px;z-index:2147483000;max-height:40vh;overflow:auto;' +
      'background:#3d0000;border:1px solid #ff5f57;color:#ffd7d4;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;' +
      'padding:10px 36px 10px 12px;border-radius:8px;box-shadow:0 8px 30px rgba(0,0,0,.5);white-space:pre-wrap;display:none';
    var head = document.createElement('div');
    head.style.cssText = 'font-weight:700;margin-bottom:4px';
    head.textContent = 'Simulation error (details also in the browser console)';
    list = document.createElement('div');
    var x = document.createElement('button');
    x.type = 'button'; x.textContent = '\u00d7'; x.setAttribute('aria-label', 'Dismiss error');
    x.style.cssText = 'position:absolute;top:6px;right:8px;background:none;border:0;color:#ffd7d4;font-size:18px;cursor:pointer';
    x.addEventListener('click', function () { box.style.display = 'none'; });
    box.appendChild(head); box.appendChild(list); box.appendChild(x);
    document.body.appendChild(box);
    queue.splice(0).forEach(add);
  }
  function add(line) {
    if (!list) { queue.push(line); return; }
    if (list.childNodes.length >= 5) return;
    box.style.display = 'block';
    var d = document.createElement('div'); d.textContent = line; list.appendChild(d);
  }
  function report(msg, where) {
    var line = String(msg) + (where ? '  (' + where + ')' : '');
    if (!box) { queue.push(line); build(); } else add(line);
  }
  window.addEventListener('error', function (e) {
    var m = (e && e.message) || '';
    if (/ResizeObserver loop/i.test(m)) return;
    report(m || 'Script error', e && e.filename ? (e.filename.split('/').pop() || 'inline') + ':' + e.lineno + ':' + e.colno : '');
  });
  window.addEventListener('unhandledrejection', function (e) {
    var r = e && e.reason; report('Unhandled promise rejection: ' + (r && r.message ? r.message : r));
  });
  /* the banner is only ever built after a real error; until then nothing is added to the page */
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { if (queue.length) build(); }, { once: true });

  /* ---------- 2. canvas: resolve CSS custom properties in colours ---------- */
  var VAR_RE = /var\(\s*(--[\w-]+)\s*(?:,\s*([^)]*))?\)/g;
  function resolve(v) {
    if (typeof v !== 'string' || v.indexOf('var(') === -1) return v;
    var cs = getComputedStyle(document.documentElement);
    return v.replace(VAR_RE, function (m, name, fb) {
      var val = cs.getPropertyValue(name).trim();
      return val || (fb ? fb.trim() : m);
    });
  }
  try {
    var P = window.CanvasRenderingContext2D && window.CanvasRenderingContext2D.prototype;
    if (P) ['fillStyle', 'strokeStyle', 'shadowColor'].forEach(function (p) {
      var d = Object.getOwnPropertyDescriptor(P, p);
      if (!d || !d.get || !d.set) return;
      Object.defineProperty(P, p, { configurable: true, enumerable: d.enumerable, get: d.get,
        set: function (v) { d.set.call(this, resolve(v)); } });
    });
    var G = window.CanvasGradient && window.CanvasGradient.prototype;
    if (G && G.addColorStop) {
      var orig = G.addColorStop;
      G.addColorStop = function (o, c) { return orig.call(this, o, resolve(c)); };
    }
  } catch (err) { /* shim is best-effort */ }
})();
"""

    @classmethod
    def html(cls):
        return f'<script id="{cls.MARK}">{cls.JS}</script>'


class TutorialEngine:
    """
    The onboarding tutorial is owned by the platform, NOT by the language model.
    The model only supplies a JSON step list (<script type="application/json" id="tut-config">).
    Markup, CSS and JS below are fixed, tested code that:
      * runs in its own <script>, so it works even if the simulation script fails
      * uses addEventListener only, guarded so it can never bind twice
      * is a real modal: page content is made `inert` while open (no z-index tricks),
        Esc / arrows / Tab-trap work, focus is restored on close
      * pauses a running simulation while open and resumes it afterwards
      * never leaves an overlay behind (hidden = display:none + no pointer events)
    """
    MARK = 'sim-tutorial-engine'
    CSS_MARK = 'sim-tutorial-css'

    CSS = r"""
#tut-help{position:fixed;right:16px;bottom:16px;z-index:9000;width:32px;height:32px;padding:0;border-radius:50%;
  background:var(--surface2,#1a1f2b);border:1px solid var(--border2,#3a4560);color:var(--text2,#8892a4);
  font:700 13px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  display:flex;align-items:center;justify-content:center;cursor:pointer;transition:background .15s,color .15s}
#tut-help:hover,#tut-help:focus-visible{background:var(--accent-dim,#002244);color:var(--accent,#4a9eff)}
#tut-root{position:fixed;inset:0;z-index:10000;transition:background .25s}
#tut-root.tut-hidden{display:none!important;visibility:hidden;pointer-events:none}
#tut-root.tut-dim{background:rgba(4,6,10,.78)}
#tut-spot{position:fixed;z-index:10001;border-radius:12px;box-shadow:0 0 0 9999px rgba(4,6,10,.78);pointer-events:none;
  transition:top .3s cubic-bezier(.4,0,.2,1),left .3s cubic-bezier(.4,0,.2,1),width .3s cubic-bezier(.4,0,.2,1),height .3s cubic-bezier(.4,0,.2,1),opacity .2s}
#tut-spot.tut-none{opacity:0;box-shadow:none}
.tut-card{position:fixed;z-index:10002;box-sizing:border-box;width:min(320px,calc(100vw - 20px));
  background:var(--surface2,#1a1f2b);border:1px solid var(--border2,#3a4560);border-radius:14px;
  box-shadow:0 12px 40px rgba(0,0,0,.5);padding:20px 22px;color:var(--text,#e8eaf0);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  opacity:0;visibility:hidden;pointer-events:none;transition:opacity .2s,visibility 0s linear .2s}
.tut-card.tut-visible{opacity:1;visibility:visible;pointer-events:auto;transition:opacity .2s}
.tut-modal{left:50%;top:50%;transform:translate(-50%,-50%);width:min(380px,calc(100vw - 20px))}
.tut-eyebrow{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent,#4a9eff);font-weight:700;margin-bottom:6px}
.tut-step-count{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--text3,#556070);margin-bottom:6px}
.tut-card h2{font-size:19px;font-weight:700;color:var(--text,#e8eaf0);margin:0 0 8px}
.tut-card h3{font-size:14px;font-weight:700;color:var(--text,#e8eaf0);margin:0 0 6px}
.tut-card p{font-size:12.5px;line-height:1.55;color:var(--text2,#8892a4);margin:0}
.tut-actions{display:flex;gap:8px;margin-top:16px;justify-content:flex-end}
.tut-tip .tut-actions{justify-content:space-between}
.tut-dots{display:flex;gap:5px;margin-bottom:8px;flex-wrap:wrap}
.tut-dot{width:5px;height:5px;border-radius:50%;background:var(--border2,#3a4560)}
.tut-dot.tut-dot-active{background:var(--accent,#4a9eff);width:14px;border-radius:3px}
.tut-btn{padding:8px 14px;border-radius:8px;border:1px solid var(--border2,#3a4560);background:var(--surface3,#222837);
  color:var(--text,#e8eaf0);font:600 13px -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;cursor:pointer}
.tut-btn:hover{background:var(--surface,#12151c)}
.tut-btn:disabled{opacity:.4;cursor:not-allowed}
.tut-btn.tut-primary{background:var(--accent-dim,#002244);color:var(--accent,#4a9eff);border-color:var(--accent,#4a9eff)}
.tut-btn.tut-primary:hover{background:var(--accent,#4a9eff);color:#000}
.tut-btn:focus-visible,#tut-help:focus-visible{outline:2px solid var(--accent,#4a9eff);outline-offset:2px}
"""

    MARKUP = r"""
<button type="button" id="tut-help" title="Replay tutorial" aria-label="Replay tutorial">?</button>
<div id="tut-root" class="tut-hidden" aria-hidden="true">
  <div id="tut-spot" class="tut-none"></div>
  <div id="tut-welcome" class="tut-card tut-modal" role="dialog" aria-modal="true" aria-labelledby="tut-w-title">
    <div class="tut-eyebrow">Welcome to</div>
    <h2 id="tut-w-title"></h2>
    <p id="tut-w-body"></p>
    <div class="tut-actions">
      <button type="button" id="tut-skip-w" class="tut-btn">Skip</button>
      <button type="button" id="tut-start" class="tut-btn tut-primary">Start Tour</button>
    </div>
  </div>
  <div id="tut-tooltip" class="tut-card tut-tip" role="dialog" aria-modal="true" aria-labelledby="tut-t-title">
    <div class="tut-dots" id="tut-dots"></div>
    <div class="tut-step-count" id="tut-count"></div>
    <h3 id="tut-t-title"></h3>
    <p id="tut-t-body"></p>
    <div class="tut-actions">
      <button type="button" id="tut-prev" class="tut-btn">Previous</button>
      <button type="button" id="tut-skip" class="tut-btn">Skip</button>
      <button type="button" id="tut-next" class="tut-btn tut-primary">Next</button>
    </div>
  </div>
  <div id="tut-done" class="tut-card tut-modal" role="dialog" aria-modal="true" aria-labelledby="tut-d-title">
    <div class="tut-eyebrow">You're ready!</div>
    <h2 id="tut-d-title">Now experiment for yourself</h2>
    <p>Have fun exploring &mdash; adjust anything, anytime. Press <b>?</b> to replay this tour.</p>
    <div class="tut-actions">
      <button type="button" id="tut-finish" class="tut-btn tut-primary">Start</button>
    </div>
  </div>
</div>
"""

    JS = r"""
(function () {
  'use strict';
  if (window.__simTutorialLoaded) return;          /* never bind twice */
  window.__simTutorialLoaded = true;

  function boot() {
    var $ = function (id) { return document.getElementById(id); };
    var root = $('tut-root'), spot = $('tut-spot'), help = $('tut-help');
    var cards = { welcome: $('tut-welcome'), step: $('tut-tooltip'), done: $('tut-done') };
    if (!root || !spot || !help || !cards.welcome || !cards.step || !cards.done) {
      console.error('[SimTutorial] markup missing - tutorial disabled'); return;
    }

    /* ---------------- config ---------------- */
    var cfg = {};
    try { var node = $('tut-config'); if (node) cfg = JSON.parse(node.textContent || '{}') || {}; }
    catch (e) { console.warn('[SimTutorial] invalid #tut-config JSON - using auto-generated steps', e); }

    function pageTitle() {
      if (cfg.title) return String(cfg.title);
      var h = document.querySelector('#lab-title h1');
      return (h && h.textContent.trim()) || document.title || 'this simulation';
    }
    $('tut-w-title').textContent = pageTitle();
    $('tut-w-body').textContent = cfg.intro ? String(cfg.intro)
      : 'Take a quick tour of the controls, then experiment on your own.';

    function labelOf(el) {
      var row = el.closest && el.closest('.ctrl-row, .toggle-row');
      var nm = row && row.querySelector('.ctrl-name');
      var src = nm || (el.tagName === 'BUTTON' ? el : null);
      if (src) { var c = src.cloneNode(true); [].forEach.call(c.querySelectorAll('span'), function (s) { s.remove(); });
        var t = c.textContent.trim(); if (t) return t; }
      return el.getAttribute('aria-label') || el.title || (el.textContent || '').trim() || el.id || 'Control';
    }
    function autoSteps() {
      var out = [], seen = [];
      function add(el, title, body) { if (!el || seen.indexOf(el) > -1) return; seen.push(el); out.push({ el: el, title: title, body: body }); }
      add(document.querySelector('#exp-list'), 'Experiments', 'Pick which experiment to explore.');
      [].forEach.call(document.querySelectorAll('#controls-panel select, #controls-panel input, #controls-panel button'), function (el) {
        if (el.type === 'hidden') return;
        var kind = el.tagName === 'BUTTON' ? 'Click to use this control.' : el.tagName === 'SELECT' ? 'Choose an option and watch the simulation respond.' : 'Adjust this and watch the simulation respond.';
        add(el, labelOf(el), kind);
      });
      [].forEach.call(document.querySelectorAll('.ov-btn'), function (el) { add(el, labelOf(el), 'Click to toggle this view option.'); });
      add(document.querySelector('#info-panel'), 'Live Metrics', 'These values are recomputed continuously from the governing equations.');
      return out.slice(0, 12);
    }
    function resolveEl(s) {
      if (s.el && document.contains(s.el)) return s.el;
      try { return s.selector ? document.querySelector(s.selector) : null; } catch (e) { return null; }
    }
    function buildSteps() {
      var list = [];
      (Array.isArray(cfg.steps) ? cfg.steps : []).forEach(function (s) {
        if (!s || typeof s !== 'object') return;
        var el = null; try { el = s.selector ? document.querySelector(s.selector) : null; } catch (e) {}
        if (!el) return;                                  /* skip steps whose target does not exist */
        list.push({ el: el, selector: s.selector, title: String(s.title || labelOf(el)), body: String(s.body || '') });
      });
      return list.length ? list : autoSteps();
    }

    /* ---------------- simulation adapter (all best-effort) ---------------- */
    function simPlaying() {
      try { if (window.SimAPI && typeof window.SimAPI.isPlaying === 'function') return !!window.SimAPI.isPlaying(); } catch (e) {}
      try { if (typeof playing !== 'undefined') return !!playing; } catch (e) {}
      var b = $('btnPlay'); return !!(b && /pause|\u23F8/i.test(b.textContent || ''));
    }
    function simSet(want) {
      try {
        if (simPlaying() === want) return;
        var api = window.SimAPI;
        if (api && want && typeof api.play === 'function') return api.play();
        if (api && !want && typeof api.pause === 'function') return api.pause();
        if (typeof togglePlay === 'function') return togglePlay();
        var b = $('btnPlay'); if (b) b.click();
      } catch (e) { console.warn('[SimTutorial] could not change play state', e); }
    }

    /* ---------------- state ---------------- */
    var open = false, mode = null, steps = [], idx = -1, wasPlaying = false, prevFocus = null;
    var locked = [], layoutRaf = 0, startTimer = 0;

    function lockPage() {
      [].forEach.call(document.body.children, function (el) {
        if (el === root || /^(SCRIPT|STYLE|LINK|TEMPLATE)$/.test(el.tagName) || el.inert) return;
        el.inert = true; el.setAttribute('inert', ''); locked.push(el);
      });
    }
    function unlockPage() {
      locked.splice(0).forEach(function (el) { el.inert = false; el.removeAttribute('inert'); });
    }
    function showCard(which) {
      Object.keys(cards).forEach(function (k) { cards[k].classList.toggle('tut-visible', k === which); });
      mode = which;
    }
    function begin() {
      if (open) return;
      open = true; prevFocus = document.activeElement;
      wasPlaying = simPlaying(); if (wasPlaying) simSet(false);   /* pause BEFORE locking the page */
      lockPage();
      root.classList.remove('tut-hidden'); root.setAttribute('aria-hidden', 'false');
    }
    function focusFirst(card) {
      var b = card.querySelector('.tut-primary:not(:disabled)') || card.querySelector('button:not(:disabled)');
      if (b) b.focus({ preventScroll: true });
    }

    function openWelcome() {
      begin(); idx = -1;
      root.classList.add('tut-dim'); spot.classList.add('tut-none');
      showCard('welcome'); focusFirst(cards.welcome);
    }
    function showDone() {
      begin(); root.classList.add('tut-dim'); spot.classList.add('tut-none');
      showCard('done'); focusFirst(cards.done);
    }
    function place(rect) {
      var card = cards.step, vw = window.innerWidth, vh = window.innerHeight, m = 14, pad = 10;
      var cw = card.offsetWidth, ch = card.offsetHeight;
      var clampX = function (x) { return Math.max(pad, Math.min(x, vw - cw - pad)); };
      var clampY = function (y) { return Math.max(pad, Math.min(y, vh - ch - pad)); };
      var fits = function (x, y) { return x >= pad && y >= pad && x + cw <= vw - pad && y + ch <= vh - pad; };
      var tries = [
        [rect.right + m, clampY(rect.top)], [rect.left - cw - m, clampY(rect.top)],
        [clampX(rect.left), rect.bottom + m], [clampX(rect.left), rect.top - ch - m]
      ];
      var pick = null;
      for (var i = 0; i < tries.length; i++) if (fits(tries[i][0], tries[i][1])) { pick = tries[i]; break; }
      if (!pick) {                                     /* nothing fits cleanly: use the roomier vertical side */
        var below = vh - rect.bottom, above = rect.top;
        pick = [clampX(rect.left), below >= above ? clampY(rect.bottom + m) : clampY(rect.top - ch - m)];
      }
      card.style.left = pick[0] + 'px'; card.style.top = pick[1] + 'px';
    }
    function layout() {
      if (!open || mode !== 'step') return;
      var s = steps[idx], el = s && resolveEl(s), r = el ? el.getBoundingClientRect() : null;
      if (r && r.width === 0 && r.height === 0) r = null;
      if (!r) {                                        /* no visible target: centre the card, dim the page */
        spot.classList.add('tut-none'); root.classList.add('tut-dim');
        var c = cards.step; c.style.left = Math.max(10, (window.innerWidth - c.offsetWidth) / 2) + 'px';
        c.style.top = Math.max(10, (window.innerHeight - c.offsetHeight) / 2) + 'px'; return;
      }
      var p = 6;
      root.classList.remove('tut-dim'); spot.classList.remove('tut-none');
      spot.style.top = (r.top - p) + 'px'; spot.style.left = (r.left - p) + 'px';
      spot.style.width = (r.width + p * 2) + 'px'; spot.style.height = (r.height + p * 2) + 'px';
      place(r);
    }
    function scheduleLayout() {
      if (layoutRaf) return;
      layoutRaf = requestAnimationFrame(function () { layoutRaf = 0; layout(); });
    }
    function showStep(i) {
      if (!steps.length) return showDone();
      idx = Math.max(0, Math.min(i, steps.length - 1));
      var s = steps[idx], el = resolveEl(s);
      if (el && el.scrollIntoView) { try { el.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) {} }
      $('tut-t-title').textContent = s.title; $('tut-t-body').textContent = s.body;
      $('tut-count').textContent = 'Step ' + (idx + 1) + ' of ' + steps.length;
      var dots = $('tut-dots'); dots.textContent = '';
      steps.forEach(function (_, n) { var d = document.createElement('span'); d.className = 'tut-dot' + (n === idx ? ' tut-dot-active' : ''); dots.appendChild(d); });
      $('tut-prev').disabled = idx === 0;
      $('tut-next').textContent = idx === steps.length - 1 ? 'Finish' : 'Next';
      showCard('step'); layout(); focusFirst(cards.step);
    }
    function start() { begin(); steps = buildSteps(); showStep(0); }
    function next() { if (mode !== 'step') return; if (idx >= steps.length - 1) showDone(); else showStep(idx + 1); }
    function prev() { if (mode === 'step' && idx > 0) showStep(idx - 1); }
    function end(resume) {
      if (!open) return;
      open = false; mode = null; idx = -1;
      Object.keys(cards).forEach(function (k) { cards[k].classList.remove('tut-visible'); });
      spot.classList.add('tut-none'); root.classList.remove('tut-dim');
      root.classList.add('tut-hidden'); root.setAttribute('aria-hidden', 'true');
      unlockPage();                                    /* page is interactive again BEFORE we resume */
      var f = prevFocus && document.contains(prevFocus) ? prevFocus : help;
      try { f.focus({ preventScroll: true }); } catch (e) {}
      if (resume && wasPlaying) simSet(true);
      wasPlaying = false;
    }

    /* ---------------- listeners (each bound exactly once) ---------------- */
    function on(id, fn) { var el = $(id); if (el) el.addEventListener('click', fn); else console.warn('[SimTutorial] missing #' + id); }
    on('tut-help', openWelcome);
    on('tut-start', start);
    on('tut-skip-w', function () { end(true); });
    on('tut-skip', function () { end(true); });
    on('tut-finish', function () { end(true); });
    on('tut-next', next);
    on('tut-prev', prev);

    /* capture phase: while the tutorial is open the simulation must not see keystrokes (Space, R ...) */
    window.addEventListener('keydown', function (e) {
      if (!open) return;
      e.stopPropagation();
      if (e.key === 'Escape') { e.preventDefault(); end(true); }
      else if (e.key === 'ArrowRight' && mode === 'step') { e.preventDefault(); next(); }
      else if (e.key === 'ArrowLeft' && mode === 'step') { e.preventDefault(); prev(); }
      else if (e.key === 'Tab') {                      /* keep focus inside the visible card */
        var card = cards[mode]; if (!card) return;
        var f = [].filter.call(card.querySelectorAll('button'), function (b) { return !b.disabled; });
        if (!f.length) return;
        var i = f.indexOf(document.activeElement);
        if (i === -1 || (!e.shiftKey && i === f.length - 1)) { e.preventDefault(); f[0].focus(); }
        else if (e.shiftKey && i === 0) { e.preventDefault(); f[f.length - 1].focus(); }
      }
    }, true);
    window.addEventListener('keyup', function (e) { if (open) e.stopPropagation(); }, true);
    window.addEventListener('resize', scheduleLayout);
    window.addEventListener('scroll', scheduleLayout, true);

    window.SimTutorial = { open: openWelcome, start: start, next: next, prev: prev, end: end,
      isOpen: function () { return open; }, mode: function () { return mode; } };

    /* deterministic first-run launch: exactly once per page load */
    startTimer = setTimeout(function () { startTimer = 0; if (!open) openWelcome(); }, 300);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true });
  else boot();
})();
"""

    _CFG_RE = re.compile(r'<script\b[^>]*\bid\s*=\s*["\']tut-config["\'][^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)

    @classmethod
    def has_legacy(cls, html):
        return bool(re.search(r'id\s*=\s*["\']tut-root["\']', html)) and cls.MARK not in html

    @classmethod
    def _clean_config(cls, html):
        """Parse + normalise the model-supplied config.  Returns dict (possibly empty)."""
        m = cls._CFG_RE.search(html)
        if not m:
            return {}
        try:
            raw = json.loads(m.group(1).strip())
        except Exception as e:
            SimLogger.warn("Tutorial", f"#tut-config is not valid JSON ({e}) -- using auto-generated steps")
            return {}
        if not isinstance(raw, dict):
            return {}
        clip = lambda v, n: str(v).strip()[:n]
        cfg = {}
        if raw.get("title"):
            cfg["title"] = clip(raw["title"], 80)
        if raw.get("intro"):
            cfg["intro"] = clip(raw["intro"], 240)
        steps = []
        for s in (raw.get("steps") or [])[:12]:
            if not isinstance(s, dict) or not s.get("selector"):
                continue
            sel = clip(s["selector"], 120)
            idm = re.fullmatch(r'#([\w-]+)', sel)
            if idm and not re.search(r'\bid\s*=\s*["\']%s["\']' % re.escape(idm.group(1)), html):
                SimLogger.warn("Tutorial", f"step selector {sel} matches no element id -- dropped")
                continue
            steps.append({"selector": sel, "title": clip(s.get("title", ""), 60), "body": clip(s.get("body", ""), 220)})
        if steps:
            cfg["steps"] = steps
        return cfg

    @classmethod
    def inject(cls, html):
        if cls.MARK in html:
            return html                                   # idempotent
        if cls.has_legacy(html):
            SimLogger.warn("Tutorial", "Model emitted its own tutorial markup (#tut-root); leaving it in place. "
                                       "The platform tutorial engine was NOT injected to avoid duplicate ids.")
            return html
        cfg = cls._clean_config(html)
        html = cls._CFG_RE.sub('', html)
        cfg_json = json.dumps(cfg, ensure_ascii=False).replace('</', '<\\/')
        css = f'<style id="{cls.CSS_MARK}">{cls.CSS}</style>'
        body = (f'\n<!-- platform tutorial (injected) -->{cls.MARKUP}'
                f'<script type="application/json" id="tut-config">{cfg_json}</script>\n'
                f'<script id="{cls.MARK}">{cls.JS}</script>\n')
        if re.search(r'</head>', html, re.IGNORECASE):
            html = re.sub(r'</head>', lambda m: css + '\n</head>', html, count=1, flags=re.IGNORECASE)
        else:
            body = css + body
        idx = html.lower().rfind('</body>')
        if idx == -1:
            return html + body
        return html[:idx] + body + html[idx:]


# ===========================================================================
#  MODULE 3 -- HtmlSanitizer
# ===========================================================================
class HtmlSanitizer:
    _SCRIPT_RE = re.compile(r'(<script(?:\s[^>]*)?>)(.*?)(</script>)', re.DOTALL | re.IGNORECASE)

    @classmethod
    def sanitize(cls, html):
        html = html.replace('\ufeff', '').replace('\r\n', '\n').replace('\r', '\n')
        end = html.rfind('</html>')
        if end != -1:
            html = html[:end + 7]
        html = re.sub(
            r'<script[^>]+src\s*=\s*["\'][^"\']*["\'][^>]*>\s*</script>',
            '', html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(
            r'<link[^>]+href\s*=\s*["\']https?://[^"\']*["\'][^>]*>',
            '', html, flags=re.IGNORECASE)
        html = re.sub(r'@import\s+url\([^)]*\)\s*;?', '', html, flags=re.IGNORECASE)
        html = re.sub(r'@import\s+["\'][^"\']*["\']\s*;?', '', html, flags=re.IGNORECASE)
        html = re.sub(r'document\.write\s*\([^)]*\)\s*;?', '', html, flags=re.IGNORECASE)
        html = cls._strip_network_calls(html)
        # --- repairs on the model's own scripts (order matters) ---
        html = cls._unwrap_legacy_error_boundary(html)   # remove the scope-breaking try{} wrapper
        html = cls._repair_scripts(html)                 # string newlines, TDZ hoisting, canvas DPR
        # --- platform-owned runtime layer (deterministic, model cannot break it) ---
        html = cls._inject_runtime_guard(html)
        html = TutorialEngine.inject(html)
        html = re.sub(r'<svg(?![^>]*xmlns)', '<svg xmlns="http://www.w3.org/2000/svg"', html, flags=re.IGNORECASE)
        html = html.replace('\x00', '')
        SimLogger.ok("Sanitizer", "HTML sanitized")
        return html

    # ---- helpers -----------------------------------------------------
    @classmethod
    def _is_model_js(cls, tag):
        """True for classic scripts written by the model (skip JSON data blocks, modules, platform scripts)."""
        if re.search(r'type\s*=\s*["\']?(?:application/|module|text/template)', tag, re.IGNORECASE):
            return False
        if re.search(r'id\s*=\s*["\'](?:%s|%s)["\']' % (RuntimeGuard.MARK, TutorialEngine.MARK), tag):
            return False
        return True

    @classmethod
    def _unwrap_legacy_error_boundary(cls, html):
        """
        v2.1 wrapped every script in `try { ... } catch (_sim_err) {...}`.  That was the root cause
        of the dead Play button / tutorial: (1) let/const/function declared inside a try block are
        BLOCK-scoped, so inline handlers such as onclick="togglePlay()" could not see them if the
        block aborted early; (2) the catch swallowed the real error (its fallback element never
        existed) and silently skipped every statement after the failing line.
        Removes our own wrapper (identified by its marker comment) so old stored output is repaired too.
        """
        head_re = re.compile(r'\s*/\*\s*-+\s*SimEngine Error Boundary\s*-+\s*\*/\s*try\s*\{\n?')
        tail_re = re.compile(r'\}\s*catch\s*\(\s*_sim_err\s*\)\s*\{[\s\S]*?\n\}\s*$')

        def fix(m):
            tag, body, close = m.group(1), m.group(2), m.group(3)
            if not cls._is_model_js(tag) or not head_re.match(body) or not tail_re.search(body):
                return m.group(0)
            body = tail_re.sub('', head_re.sub('\n', body, count=1)).rstrip() + '\n'
            SimLogger.warn("Sanitizer", "Removed legacy try/catch error boundary from script")
            return f"{tag}{body}{close}"
        return cls._SCRIPT_RE.sub(fix, html)

    @classmethod
    def _repair_scripts(cls, html):
        def fix(m):
            tag, body, close = m.group(1), m.group(2), m.group(3)
            if not cls._is_model_js(tag) or len(body.strip()) < 20:
                return m.group(0)
            body, nl_changed = JsToolkit.fix_raw_newlines_in_strings(body)
            if nl_changed:
                SimLogger.warn("Sanitizer", "Raw newline/tab inside a JS string literal -- escaped")
            try:
                body, moved = JsToolkit.hoist_tdz_declarations(body)
            except Exception as e:                       # analysis must never break a good page
                SimLogger.warn("Sanitizer", f"TDZ analysis skipped ({e})")
                moved = []
            if moved:
                SimLogger.warn("Sanitizer", "Hoisted declarations above first use to prevent a "
                                            f"temporal-dead-zone crash: {', '.join(dict.fromkeys(moved))}")
            # scale() ACCUMULATES on repeated resizes unless the transform is reset; setTransform() is absolute.
            body, k = re.subn(r'(\b[A-Za-z_$][\w$]*)\.scale\(\s*(dpr|pixelRatio|ratio)\s*,\s*\2\s*\)',
                              r'\1.setTransform(\2, 0, 0, \2, 0, 0)', body)
            if k:
                SimLogger.warn("Sanitizer", "Replaced ctx.scale(dpr, dpr) with setTransform() (no accumulation on resize)")
            return f"{tag}{body}{close}"
        return cls._SCRIPT_RE.sub(fix, html)

    @classmethod
    def _inject_runtime_guard(cls, html):
        if RuntimeGuard.MARK in html:
            return html
        tag = RuntimeGuard.html()
        m = re.search(r'<head[^>]*>', html, re.IGNORECASE)
        if m:
            return html[:m.end()] + '\n' + tag + html[m.end():]
        m = re.search(r'<(?:style|script|body)\b', html, re.IGNORECASE)
        return (html[:m.start()] + tag + '\n' + html[m.start():]) if m else tag + html

    @classmethod
    def _strip_network_calls(cls, html):
        def process_script(m):
            tag, body, close = m.group(1), m.group(2), m.group(3)
            if re.search(r'type\s*=\s*["\']application/', tag, re.IGNORECASE):
                return m.group(0)
            new_body = re.sub(r'\bfetch\s*\([^)]*\)[^;]*;?', '/* network call removed */;', body)
            new_body = re.sub(r'new\s+XMLHttpRequest\s*\([^)]*\)', '({})', new_body)
            if new_body != body:
                SimLogger.warn("Sanitizer", "Removed a network call (fetch/XHR) from generated JS")
            return f"{tag}{new_body}{close}"
        return cls._SCRIPT_RE.sub(process_script, html)


# ===========================================================================
#  MODULE 4 -- RecoveryEngine
# ===========================================================================
class RecoveryEngine:
    @staticmethod
    def fallback_html(topic, reason):
        t_safe      = html_module.escape(topic[:120])
        reason_safe = html_module.escape(reason[:300])
        return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100%;background:#0a0c10;
  font-family:-apple-system,'Segoe UI',Arial,sans-serif;
  display:flex;align-items:center;justify-content:center;color:#e8eaf0}}
.card{{background:#12151c;border:1px solid #2a3040;border-radius:16px;
  box-shadow:0 4px 24px rgba(0,0,0,.4);padding:36px 40px;max-width:520px;text-align:center}}
.icon{{font-size:40px;margin-bottom:16px}}
.title{{font-size:17px;font-weight:700;color:#e8eaf0;margin-bottom:10px}}
.reason{{font-size:11px;color:#8892a4;background:#1a1f2b;border-radius:10px;
  padding:10px 14px;margin:12px 0;border:1px solid #2a3040;text-align:left;
  line-height:1.6;font-family:monospace}}
.topic{{font-size:12px;color:#556070;line-height:1.6;margin-top:10px;font-style:italic}}
.retry-hint{{margin-top:18px;font-size:11px;font-weight:700;letter-spacing:1.5px;
  text-transform:uppercase;color:#f5a623}}
</style></head><body>
<div class="card">
<div class="icon">&#x26A0;&#xFE0F;</div>
<div class="title">Simulation Could Not Render</div>
<div class="reason">{reason_safe}</div>
<div class="topic">"{t_safe}"</div>
<div class="retry-hint">Please try generating again</div>
</div></body></html>"""

    @staticmethod
    def partial_html(topic, sim_code):
        if '<!DOCTYPE' in sim_code or '<html' in sim_code:
            return sim_code
        t_safe = html_module.escape(topic[:120])
        return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>html,body{{margin:0;padding:0;width:100%;height:100%;background:#0a0c10;
  font-family:-apple-system,sans-serif;color:#e8eaf0}}</style></head><body>
<div style="font-size:11px;color:#8892a4;position:fixed;top:8px;left:0;right:0;text-align:center;z-index:99">
  {t_safe}</div>
{sim_code}</body></html>"""


# ===========================================================================
#  MODULE 5 -- Image Reference Fetcher
# ===========================================================================

def _fetch_image_refs(topic: str, max_results: int = 5) -> List[dict]:
    """
    Query Google Custom Search Image API for visual references.
    Returns [] gracefully when keys are absent or the request fails.
    Must be called via asyncio.to_thread() from async code.
    """
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        SimLogger.info("ImageRef", "Google keys not set -- skipping image search step")
        return []

    query = f"{topic} diagram simulation laboratory experiment"
    params = urllib.parse.urlencode({
        "key":        GOOGLE_API_KEY,
        "cx":         GOOGLE_CSE_ID,
        "q":          query,
        "searchType": "image",
        "num":        max_results,
        "imgType":    "photo,clipart",
        "safe":       "active",
    })
    url = f"https://www.googleapis.com/customsearch/v1?{params}"

    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("items", [])
        refs = []
        for item in items[:max_results]:
            refs.append({
                "title":   item.get("title", ""),
                "snippet": item.get("snippet", ""),
                "link":    item.get("link", ""),
            })
        SimLogger.ok("ImageRef", f"Fetched {len(refs)} image references for '{topic[:50]}'")
        return refs
    except Exception as e:
        SimLogger.warn("ImageRef", f"Image search failed (non-fatal): {e}")
        return []


def _format_image_refs_for_prompt(refs: List[dict]) -> str:
    if not refs:
        return ""
    lines = ["VISUAL REFERENCE (image titles/descriptions found on Google for this topic):"]
    for i, r in enumerate(refs, 1):
        title   = r.get("title", "").strip()[:120]
        snippet = r.get("snippet", "").strip()[:200]
        if title:
            lines.append(f"  [{i}] {title}")
        if snippet:
            lines.append(f"       {snippet}")
    lines.append(
        "Use these as visual anchors when designing the canvas layout, "
        "choosing diagram conventions, and picking instrument/component styles. "
        "Do NOT attempt to load or embed any URLs."
    )
    return "\n".join(lines)


# ===========================================================================
#  MODULE 6 -- Topic Classification
# ===========================================================================
CATEGORIES = [
    "PHYSICS_MECHANICS",
    "PHYSICS_WAVES_OPTICS",
    "ELECTRICITY_CIRCUITS",
    "CHEMISTRY",
    "BIOLOGY",
    "MATH_GEOMETRY",
    "CS_ALGORITHMS",
    "EARTH_ENV_SCIENCE",
    "ECONOMICS_SOCIAL",
    "GENERAL_PROCESS",
]

_CATEGORY_KEYWORDS = {
    "PHYSICS_MECHANICS": ["projectile", "pendulum", "spring", "friction", "collision",
        "momentum", "force", "velocity", "acceleration", "gravity", "torque",
        "newton", "oscillation", "harmonic motion", "free fall", "incline",
        "kinematics", "dynamics", "angular", "rotational", "centripetal"],
    "PHYSICS_WAVES_OPTICS": ["wave", "light", "lens", "mirror", "refraction", "reflection",
        "diffraction", "interference", "prism", "snell", "optic", "sound",
        "frequency", "wavelength", "doppler", "polarization", "photoelectric",
        "electromagnetic", "spectrum", "coherent", "interference pattern"],
    "ELECTRICITY_CIRCUITS": ["circuit", "resistor", "capacitor", "inductor", "voltage",
        "current", "ohm", "charge", "electric field", "magnetic field", "rc circuit",
        "rlc", "kirchhoff", "battery", "diode", "transistor", "semiconductor",
        "band gap", "p-n junction", "logic gate", "digital circuit"],
    "CHEMISTRY": ["reaction", "equilibrium", "ph", "titration", "molarity", "gas law",
        "boyle", "charles", "stoichiometry", "acid", "base", "catalyst", "bond",
        "electron configuration", "periodic", "polymerization", "polymer",
        "enthalpy", "entropy", "activation energy", "colligative"],
    "BIOLOGY": ["cell", "dna", "rna", "protein", "photosynthesis", "mitosis", "enzyme",
        "hormone", "gene", "population growth", "predator", "prey", "ecosystem",
        "natural selection", "neuron", "heart rate", "osmosis", "diffusion",
        "action potential", "genetics", "heredity"],
    "MATH_GEOMETRY": ["function", "derivative", "integral", "matrix", "vector", "polygon",
        "triangle", "circle", "probability", "distribution", "fourier", "fractal",
        "trigonometry", "graph of", "parametric", "transformation", "linear algebra",
        "differential equation", "taylor series", "complex number"],
    "CS_ALGORITHMS": ["sorting", "sort algorithm", "binary search", "linked list", "stack",
        "queue", "tree traversal", "graph algorithm", "dijkstra", "recursion",
        "dynamic programming", "hash table", "automaton", "neural network",
        "pathfinding", "convex hull", "compression", "encryption"],
    "EARTH_ENV_SCIENCE": ["climate", "plate tectonic", "earthquake", "weather", "erosion",
        "carbon cycle", "greenhouse", "orbit", "solar system", "tide", "volcano",
        "water cycle", "ecosystem", "seismic", "atmospheric", "ocean current"],
    "ECONOMICS_SOCIAL": ["supply and demand", "market", "interest rate", "inflation",
        "compound interest", "population dynamics", "game theory", "auction",
        "elasticity", "gdp", "investment", "portfolio", "regression"],
}

_MULTI_EXPERIMENT_TOPICS = {
    "PHYSICS_WAVES_OPTICS": [
        "Snell's Law / Refraction", "Convex Lens", "Concave Lens",
        "Concave Mirror", "Double-Slit Interference", "Single-Slit Diffraction",
    ],
    "ELECTRICITY_CIRCUITS": [
        "RC Charging/Discharging", "RLC Oscillator", "Ohm's Law",
        "Series & Parallel Circuits", "EM Induction",
    ],
    "PHYSICS_MECHANICS": [
        "Projectile Motion", "Simple Pendulum", "Spring-Mass System",
        "Elastic Collision", "Inclined Plane",
    ],
    "CHEMISTRY": [
        "Acid-Base Titration", "Gas Laws (Boyle/Charles)", "Chemical Equilibrium",
        "Reaction Kinetics", "Electrochemistry",
    ],
}


async def _classify_topic(topic: str) -> str:
    """
    Keyword-match first (instant, no network). Falls back to an LLM call
    only when keywords are ambiguous — awaited on the async client.
    """
    t = topic.lower()
    scores = {cat: sum(1 for k in kws if k in t) for cat, kws in _CATEGORY_KEYWORDS.items()}
    max_score = max(scores.values()) if scores else 0
    if max_score >= 1:
        top = [c for c, s in scores.items() if s == max_score]
        if len(top) == 1:
            return top[0]
    try:
        resp = await _gemini_client.aio.models.generate_content(
            model=CLASSIFIER_MODEL,
            contents=f"Classify this simulation topic: {topic[:200]}",
            config=_genai_types.GenerateContentConfig(
                system_instruction="Reply with ONLY one category word from this exact list: "
                                   + ", ".join(CATEGORIES),
                max_output_tokens=MAX_TOK_CLASSIFIER,
                temperature=0.0,
            ),
        )
        cat = (resp.text or "").strip().upper()
        if cat in CATEGORIES:
            return cat
    except Exception as e:
        SimLogger.warn("Classifier", f"Fallback classification failed: {e}")
    return "GENERAL_PROCESS"


# ===========================================================================
#  MODULE 7 -- Prompt System
# ===========================================================================

DESIGN_SYSTEM = """
════════════════════════════════════════════════════════
  REQUIRED PAGE ARCHITECTURE
════════════════════════════════════════════════════════
Build ONE self-contained HTML5 page (no external resources, no CDN,
no network calls) structured as:

  #app  (display:flex; height:100vh)
  ├── #sidebar  (fixed width 260–300px, flex-shrink:0)
  │   ├── #lab-title       — eyebrow label "Interactive Simulation" +
  │   │                      bold page title with ONE accent-colored word
  │   ├── #exp-list        — (OPTIONAL: only for broad topics covering
  │   │                      multiple experiments) a vertical list of
  │   │                      .exp-btn buttons; clicking one swaps the
  │   │                      experiment shown without reloading. If the
  │   │                      topic is a single focused experiment, omit
  │   │                      #exp-list entirely.
  │   └── #controls-panel  — sliders / selects / toggles wired to the sim
  └── #main  (flex:1; flex-direction:column)
      ├── #canvas-area  (flex:1; position:relative; overflow:hidden)
      │   ├── <canvas id="cvs"> OR inline <svg id="simsvg">
      │   ├── #overlay-bar  (position:absolute; top:10px; right:12px)
      │   └── #tip  (position:absolute; bottom:12px; left:50%)
      └── #info-panel  (height:~100px; border-top)
            — horizontal row of 3–5 .metric cards

MOBILE BREAKPOINT (max-width: 760px):
  - #app becomes flex-direction:column
  - #sidebar becomes width:100%; max-height:220px; overflow-y:auto
  - #controls-panel becomes a wrapping flex-row of compact controls
  - #info-panel stacks metrics in a 2×N grid

════════════════════════════════════════════════════════
  COLOUR TOKENS  (define ALL in :root on <html>)
════════════════════════════════════════════════════════
DARK THEME — default for science/math/engineering:
  :root {
    --bg:#0a0c10;  --surface:#12151c;  --surface2:#1a1f2b;  --surface3:#222837;
    --border:#2a3040;  --border2:#3a4560;
    --text:#e8eaf0;  --text2:#8892a4;  --text3:#556070;
    --green:#3ddc84;  --green-dim:#003320;
    --red:#ff5f57;    --red-dim:#3d0000;
    --violet:#b57aff; --violet-dim:#1e0040;
    --cyan:#00d4d8;   --cyan-dim:#003d3e;
    --accent: <pick one: #f5a623 amber | #4a9eff blue | #00d4d8 cyan
                         | #3ddc84 green | #e056b4 magenta>;
    --accent-dim: <matching dim>;
    --accent-glow: <rgba version with 0.5 alpha>;
    --panel-w: 260px;
  }

LIGHT THEME — use ONLY for economics/statistics/printed-diagram topics:
  :root {
    --bg:#f0f5ff;  --surface:#ffffff;  --surface2:#f1f5f9;
    --border:#e2e8f0;  --border2:#cbd5e1;
    --text:#1e293b;  --text2:#475569;  --text3:#94a3b8;
    --accent:#3b5bdb;  --accent-dim:#dbe4ff;  --accent-glow:rgba(59,91,219,.3);
    --panel-w: 260px;
  }

════════════════════════════════════════════════════════
  SIDEBAR + CONTROLS CSS PATTERNS
════════════════════════════════════════════════════════
#sidebar {
  width:var(--panel-w); background:var(--surface);
  border-right:1px solid var(--border);
  display:flex; flex-direction:column; flex-shrink:0; overflow:hidden;
}
#lab-title { padding:16px; border-bottom:1px solid var(--border); }
.eyebrow { font-size:10px; letter-spacing:.12em; text-transform:uppercase;
           color:var(--text3); margin-bottom:4px; }
#lab-title h1 { font-size:17px; font-weight:600; color:var(--text); }
#lab-title h1 span { color:var(--accent); }

.exp-btn {
  display:flex; align-items:center; gap:10px; width:100%;
  padding:9px 12px; border-radius:8px; border:none; background:transparent;
  color:var(--text2); cursor:pointer; font-size:13px; font-weight:500;
  text-align:left; transition:all .15s;
}
.exp-btn.active {
  background:var(--accent-dim); color:var(--accent);
  border:1px solid color-mix(in srgb, var(--accent) 25%, transparent);
}

#controls-panel { flex:1; overflow-y:auto; padding:12px; }
.ctrl-row { margin-bottom:10px; }
.ctrl-name { font-size:12px; color:var(--text2); margin-bottom:4px;
             display:flex; justify-content:space-between; }
.ctrl-name span { color:var(--accent); font-weight:600; font-family:monospace; }

input[type=range] {
  -webkit-appearance:none; width:100%; height:3px; border-radius:2px;
  background:linear-gradient(to right,
    var(--accent) 0%,
    var(--accent) calc(var(--pct,50%) * 1%),
    var(--border2) calc(var(--pct,50%) * 1%));
  outline:none; cursor:pointer;
}
input[type=range]::-webkit-slider-thumb {
  -webkit-appearance:none; width:14px; height:14px; border-radius:50%;
  background:var(--accent); cursor:pointer;
  box-shadow:0 0 6px var(--accent-glow);
}

select {
  width:100%; background:var(--surface2); border:1px solid var(--border2);
  color:var(--text); font-size:12px; padding:6px 8px; border-radius:6px;
}

.toggle-row { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
.toggle { position:relative; width:36px; height:20px; }
.toggle input { opacity:0; width:0; height:0; position:absolute; }
.toggle-track { position:absolute; inset:0; background:var(--border2); border-radius:10px; cursor:pointer; transition:background .2s; }
.toggle input:checked + .toggle-track { background:var(--accent); }
.toggle-thumb { position:absolute; top:2px; left:2px; width:16px; height:16px; border-radius:50%; background:#fff; transition:transform .2s; pointer-events:none; }
.toggle input:checked ~ .toggle-thumb { transform:translateX(16px); }

.btn-row { display:flex; gap:8px; margin-top:4px; }
.btn { flex:1; padding:8px 6px; border-radius:8px; border:1px solid var(--border2);
  background:var(--surface2); color:var(--text2); font-size:12px; font-weight:500; cursor:pointer; transition:all .15s; }
.btn.primary { background:var(--accent-dim); color:var(--accent);
  border-color:color-mix(in srgb, var(--accent) 30%, transparent); }
.btn.primary:hover { background:var(--accent); color:#000; }

════════════════════════════════════════════════════════
  CANVAS AREA + OVERLAY CSS
════════════════════════════════════════════════════════
#canvas-area { flex:1; position:relative; overflow:hidden; }
#canvas-area canvas { position:absolute; top:0; left:0; width:100%; height:100%; }

.ov-btn {
  background:var(--surface2); border:1px solid var(--border);
  color:var(--text2); font-size:11px; padding:5px 10px;
  border-radius:6px; cursor:pointer; transition:all .15s;
}
.ov-btn:hover, .ov-btn.on { background:var(--surface3); color:var(--accent); }

#tip {
  position:absolute; bottom:12px; left:50%; transform:translateX(-50%);
  background:var(--surface2); border:1px solid var(--border);
  color:var(--text2); font-size:11px; padding:6px 14px;
  border-radius:20px; pointer-events:none; white-space:nowrap; z-index:10;
}

════════════════════════════════════════════════════════
  METRICS STRIP CSS
════════════════════════════════════════════════════════
#info-panel {
  height:100px; background:var(--surface); border-top:1px solid var(--border);
  display:flex; align-items:stretch; flex-shrink:0;
}
.metric { flex:1; display:flex; flex-direction:column; justify-content:center;
  padding:12px 16px; border-right:1px solid var(--border); min-width:0; }
.metric:last-child { border-right:none; }
.metric-label { font-size:10px; letter-spacing:.08em; text-transform:uppercase;
  color:var(--text3); margin-bottom:4px; }
.metric-value { font-size:20px; font-weight:600; font-family:monospace; color:var(--text); }
.metric-badge { display:inline-block; font-size:10px; font-weight:600;
  padding:3px 8px; border-radius:4px; margin-top:4px; }
.badge-green  { background:var(--green-dim);  color:var(--green);  }
.badge-red    { background:var(--red-dim);    color:var(--red);    }
.badge-amber  { background:var(--accent-dim); color:var(--accent); }
.badge-violet { background:var(--violet-dim); color:var(--violet); }

════════════════════════════════════════════════════════
  CANVAS DRAWING TECHNIQUES
════════════════════════════════════════════════════════
════════════════════════════════════════════════════════
  SCRIPT ARCHITECTURE CONTRACT  (MANDATORY -- breaking it kills Play / Reset / every control)
════════════════════════════════════════════════════════
Use ONE classic <script> at the END of <body>. No type="module", no try/catch
wrapped around the whole program, no inline on*="..." attributes.
Structure that script in EXACTLY this order:

  A. STATE      every top-level let/const the program needs: rafId, playing, lastTs,
                simTime, view/pan/zoom values, cvs, ctx, W, H, DOM references ...
  B. FUNCTIONS  physics, draw(), updateMetrics(), helpers, bindSlider(), play/pause ...
  C. function init() { ... }   EVERYTHING that has side effects: bindSlider() calls,
                addEventListener() calls, canvas sizing, initial state, first draw().
  D. bootstrap  the LAST statement of the script, nothing after it:
       if (document.readyState === 'loading') {
         document.addEventListener('DOMContentLoaded', init, { once: true });
       } else { init(); }

HARD RULES
 1. At top level ONLY declarations (A, B) and the bootstrap (D) may exist. NEVER call
    bindSlider(), draw(), resizeCanvas(), resetSim() or addEventListener() at top level.
    bindSlider() runs its onChange callback synchronously; if that callback (or draw())
    touches a let/const declared lower in the file, JavaScript throws a temporal-dead-zone
    ReferenceError ("Cannot access 'ctx' before initialization") and every later line of the
    script never runs. Doing all work inside init() makes this impossible.
 2. Attach EVERY handler with element.addEventListener(...) inside init(). Never onclick="...".
 3. Canvas colours must be real colour strings. Canvas IGNORES CSS variables, so never write
    ctx.fillStyle = 'var(--red)'. Resolve once:
      const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
      const COLORS = { a: cssVar('--red'), b: cssVar('--green'), c: cssVar('--accent') };
    (declare COLORS inside init() or fill it there, since it needs the DOM styles).
 4. Never hide errors with empty catch blocks. Real errors must reach the console.
 5. IDs starting with "tut-" are reserved by the platform. Do not use z-index above 900.

════════════════════════════════════════════════════════
  CANVAS SETUP  (DPR-safe, resize-safe)
════════════════════════════════════════════════════════
  // state (section A)
  const cvs = document.getElementById('cvs');
  const ctx = cvs.getContext('2d');
  let W = 0, H = 0;

  // function (section B)
  function resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const r = cvs.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    const oldW = W, oldH = H;
    cvs.width  = Math.round(r.width  * dpr);
    cvs.height = Math.round(r.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);   // ABSOLUTE transform: never ctx.scale() (it accumulates)
    W = r.width; H = r.height;
    // keep any pan offset relative to the canvas centre so the view survives a resize:
    //   if (oldW) { offsetX += (W - oldW) / 2; offsetY += (H - oldH) / 2; } else { offsetX = W / 2; offsetY = H / 2; }
    draw();
  }
  // inside init():
  if (window.ResizeObserver) new ResizeObserver(resizeCanvas).observe(document.getElementById('canvas-area'));
  else window.addEventListener('resize', resizeCanvas);
  resizeCanvas();

Dragging / panning: use pointer events (pointerdown / pointermove / pointerup with
setPointerCapture) and CSS  touch-action:none  on the canvas so mouse AND touch work.
Wheel zoom: addEventListener('wheel', handler, { passive: false }) and call preventDefault().

════════════════════════════════════════════════════════
  INTERACTION WIRING PATTERN
════════════════════════════════════════════════════════
  // section B
  function gv(id) { return parseFloat(document.getElementById(id)?.value ?? 0); }
  function gb(id) { return document.getElementById(id)?.checked ?? false; }

  function bindSlider(id, displayId, fmt, onChange) {
    const el = document.getElementById(id);
    const dv = document.getElementById(displayId);
    function update() {
      const v = parseFloat(el.value);
      if (dv) dv.textContent = fmt(v);
      const pct = 100 * (v - parseFloat(el.min)) / (parseFloat(el.max) - parseFloat(el.min));
      el.style.setProperty('--pct', pct.toFixed(1));
      onChange(v);
    }
    el.addEventListener('input', update);
    update();          // safe ONLY because bindSlider() is called from inside init()
  }

  function setMetrics(items) {
    const panel = document.getElementById('info-panel');
    panel.innerHTML = items.map(m => `
      <div class="metric">
        <div class="metric-label">${m.label}</div>
        <div class="metric-value">${m.value}</div>
        ${m.sub  ? `<div class="metric-sub">${m.sub}</div>` : ''}
        ${m.badge ? `<div class="metric-badge ${m.badgeClass||'badge-amber'}">${m.badge}</div>` : ''}
      </div>`).join('');
  }

PLAY / PAUSE / RESET  -- a strict state machine (use this exact shape when the topic animates):
  // state (section A)
  let rafId = null, playing = false, lastTs = null;

  // functions (section B)
  function updatePlayButton() {
    const b = document.getElementById('btnPlay');
    if (b) b.textContent = playing ? '⏸ Pause' : '▶ Play';
  }
  function animationLoop(ts) {
    if (!playing) return;
    const frameSec = lastTs === null ? 1 / 60 : Math.min((ts - lastTs) / 1000, 0.05);
    lastTs = ts;
    stepSim(frameSec);          // advance the model by real elapsed time (frame-rate independent)
    draw();
    rafId = requestAnimationFrame(animationLoop);
  }
  function startSimulation() {
    if (playing) return;                    // never create a second loop
    playing = true; lastTs = null;
    updatePlayButton();
    rafId = requestAnimationFrame(animationLoop);
  }
  function stopSimulation() {
    playing = false;
    if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
    updatePlayButton();
  }
  function togglePlay() { if (playing) stopSimulation(); else startSimulation(); }
  function resetSim() {
    stopSimulation();                       // stops the loop even if it is running
    /* rebuild the model from the CURRENT control values, clear trails/history, simTime = 0 */
    draw(); updateMetrics();                // always leave a valid visible frame; button reads "▶ Play"
  }

  // inside init():
  document.getElementById('btnPlay').addEventListener('click', togglePlay);
  document.getElementById('btnReset').addEventListener('click', resetSim);
  window.SimAPI = { isPlaying: () => playing, play: startSimulation, pause: stopSimulation, reset: resetSim };
  window.addEventListener('keydown', e => {
    const t = e.target, tag = t && t.tagName;
    if (tag === 'SELECT' || tag === 'TEXTAREA' || (tag === 'INPUT' && t.type !== 'range' && t.type !== 'checkbox')) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.code === 'Space') { e.preventDefault(); if (!e.repeat) togglePlay(); }
    else if (e.code === 'KeyR' && !e.repeat) resetSim();
  });
  // stop a focused <button> from ALSO firing a click when Space is released (would double-toggle)
  window.addEventListener('keyup', e => { if (e.code === 'Space') e.preventDefault(); });

For topics that do not animate, omit the play machinery but keep the same
A / B / init() / bootstrap structure and still expose real-time control redraws.

NUMERICAL SAFETY (physics topics)
  - Soften on the SQUARED distance BEFORE dividing:  r2 = dx*dx + dy*dy; if (r2 < EPS2) r2 = EPS2;
    use r2 in every division (force AND potential energy). Never clamp only the square root while
    still dividing by the raw squared distance.
  - Substep so one step never moves a body a large fraction of the smallest length scale.
  - After each step verify Number.isFinite() on state; if not, stop the loop and restore the
    initial state instead of drawing NaN.

════════════════════════════════════════════════════════
  ONBOARDING TUTORIAL  (platform-provided -- you only supply the step list)
════════════════════════════════════════════════════════
DO NOT write any tutorial markup, CSS or JavaScript. The platform injects the
welcome card, spotlight, step tooltips, "?" replay button and all logic itself, and
guarantees it works. You provide ONLY this data block, placed just before the main <script>:

  <script type="application/json" id="tut-config">
  {
    "title": "Pendulum Lab",
    "intro": "One friendly sentence about what the learner will explore.",
    "steps": [
      { "selector": "#lenSlider",  "title": "Pendulum Length", "body": "Longer arms swing more slowly." },
      { "selector": "#btnPlay",    "title": "Playback",        "body": "Start or pause the motion." },
      { "selector": "#info-panel", "title": "Live Metrics",    "body": "Values recomputed every frame." }
    ]
  }
  </script>

Rules: strictly valid JSON (double quotes, no comments, no trailing commas); 4 to 8 steps in
visual order (sidebar top to bottom, then overlay buttons, then #info-panel); every "selector"
must be "#some-id" of an element that exists in YOUR page (group several related controls
by giving their wrapper an id); plain text only in title/body (no HTML).

════════════════════════════════════════════════════════
  WHAT TO OMIT
════════════════════════════════════════════════════════
- No external URLs of any kind.
- No localStorage / sessionStorage.
- No backend calls -- 100% client-side computation.
- No placeholder numbers disconnected from governing equations.
- No controls that don't visibly change anything.
- No more than 7 controls in the sidebar.
- Do NOT write tutorial markup/CSS/JS (the platform injects it) -- but DO include the #tut-config JSON block.
- No inline on*="..." attributes, no top-level side effects outside init(), no try/catch around the whole script.
"""

SYSTEM = """You are SimEngine v2.1 -- an expert interactive-simulation engineer who builds
single-page HTML5 virtual-lab simulations for students and curious learners.

YOUR MISSION: Given ONE topic, concept, or lab experiment, design and build a
COMPLETE, SELF-CONTAINED, INTERACTIVE simulation -- live controls (sliders,
selects, toggles) that drive a real-time canvas (or SVG) visualization, with
a metrics strip showing live computed values. This is a HANDS-ON LAB, not a
narrated slideshow. Everything updates instantly as the learner adjusts controls.

""" + DESIGN_SYSTEM + """

════════════════════════════════════════════════════════
  REQUIRED OUTPUT FORMAT
════════════════════════════════════════════════════════
Return ONLY raw JSON (no markdown, no code fences, no commentary):
{
  "title": "short page title, e.g. 'Pendulum Lab' or 'RC Circuit Lab'",
  "category": "one of: """ + ", ".join(CATEGORIES) + """",
  "summary": "1-2 sentence plain-English description",
  "controls_overview": ["one phrase per control exposed"],
  "key_formula": "the core formula or relationship the simulation is built on",
  "learning_notes": ["2-4 short simple-English sentences"],
  "simulation_code": "COMPLETE SELF-CONTAINED <!DOCTYPE html>...</html> AS A SINGLE PROPERLY-ESCAPED JSON STRING"
}

════════════════════════════════════════════════════════
  CORRECTNESS REQUIREMENTS
════════════════════════════════════════════════════════
- Every number shown must come from a REAL computation based on the actual governing equations.
- Units must be correct and consistently labeled.
- Edge cases must be handled gracefully -- never crash silently.
- For animated topics, use real physics time-stepping (Euler or RK4).

════════════════════════════════════════════════════════
  QUALITY BAR
════════════════════════════════════════════════════════
- Every slider/select/toggle visibly changes the canvas or a metric.
- Canvas drawing must look professional: clear labels, consistent stroke weights.
- Metrics strip shows 3-5 of the MOST MEANINGFUL live-computed values.
- Mobile-responsive down to 380px viewport.
- Include keyboard shortcuts (Space = play/pause, R = reset) when applicable.
- EVERY simulation includes a <script type="application/json" id="tut-config"> step list (tutorial UI is platform-provided).
"""

STRATEGY_TEMPLATES = {
    "PHYSICS_MECHANICS":
        "Canvas MUST draw a physical, visual scene (e.g. pendulums swinging, blocks sliding, "
        "springs bouncing, planets orbiting) rather than just a graph. "
        "REQUIRED equations: Newton's 2nd law F=ma, energy E=KE+PE. "
        "Metrics: period T, velocity, current KE, current PE, total E.",

    "PHYSICS_WAVES_OPTICS":
        "Canvas MUST draw physical optical components (lenses, mirrors, prisms) and visual "
        "light rays/wavefronts on a dark background. "
        "REQUIRED equations: Snell's law; lens/mirror equations. "
        "Metrics: angles θ₁/θ₂, image distance, magnification, critical angle.",

    "ELECTRICITY_CIRCUITS":
        "Canvas MUST draw a visual, interactive circuit diagram (resistors, capacitors, batteries) "
        "with animated particles or arrows showing current flow. "
        "REQUIRED equations: Ohm's V=IR; RC/RLC dynamics. "
        "Metrics: current I, charge Q, time constant τ, power P=V²/R.",

    "CHEMISTRY":
        "Canvas MUST draw a visual laboratory setup (e.g. flasks, beakers, burettes, burners) "
        "with animated liquid colors, bubbles, or particles. Do NOT just draw a graph. If a "
        "titration curve or reaction plot is needed, draw it alongside the physical beaker/flask. "
        "REQUIRED equations: rate laws, equilibrium, Henderson-Hasselbalch, Nernst. "
        "Metrics: rate constant k, pH, concentration, cell potential.",

    "BIOLOGY":
        "Canvas MUST draw visual biological structures (cells dividing, DNA strands, bacteria in a petri dish, "
        "ecosystem agents). Do NOT just draw a population graph. "
        "REQUIRED equations: logistic growth, Michaelis-Menten. "
        "Metrics: population count, growth rate, substrate concentration.",

    "MATH_GEOMETRY":
        "Canvas MUST draw interactive geometric shapes, curves, or fractals on a coordinate plane. "
        "Metrics: area, perimeter, roots, extrema, period.",

    "CS_ALGORITHMS":
        "Canvas MUST draw visual data structures (nodes, trees, arrays) with animated highlights "
        "showing the algorithm's progress step-by-step. "
        "Metrics: comparisons count, swaps count, elapsed steps.",

    "EARTH_ENV_SCIENCE":
        "Canvas MUST draw a visual cross-section of the earth, atmosphere, or environment "
        "(e.g. clouds, ice caps, tectonic plates) rather than just a time-series plot. "
        "Metrics: projected temperature, CO2 concentration, sea level.",

    "ECONOMICS_SOCIAL":
        "Canvas drawing supply-demand curves or market agents. "
        "Metrics: equilibrium price, quantity, consumer/producer surplus.",

    "GENERAL_PROCESS":
        "Canvas MUST draw a highly visual, physical representation of the process "
        "(e.g. machines, agents, fluid flow) alongside any necessary data plots. "
        "Show a metrics strip with the most informative derived values.",
}


def _build_prompt(topic: str, category: str, image_refs: List[dict]) -> tuple:
    strategy      = STRATEGY_TEMPLATES.get(category, STRATEGY_TEMPLATES["GENERAL_PROCESS"])
    image_ctx     = _format_image_refs_for_prompt(image_refs)
    multi_exp_list = _MULTI_EXPERIMENT_TOPICS.get(category, [])
    multi_exp_hint = ""
    if multi_exp_list:
        exp_str = "; ".join(multi_exp_list)
        multi_exp_hint = (
            f"\nMULTI-EXPERIMENT HINT: This category ({category}) suits a sidebar "
            f"experiment-switcher. If the topic '{topic}' is broad enough, add an "
            f"#exp-list with these experiments: [{exp_str}]. If the user gave a very "
            f"specific single-experiment topic, you may omit the switcher.\n"
        )

    system_text = SYSTEM
    user_parts  = [
        f"Build an interactive HTML5 simulation for SimEngine v2.1.\n",
        f"TOPIC / LAB EXPERIMENT: {topic}",
        f"CATEGORY: {category}",
        f"STRATEGY HINT: {strategy}",
    ]
    if multi_exp_hint:
        user_parts.append(multi_exp_hint)
    if image_ctx:
        user_parts.append(f"\n{image_ctx}")
    user_parts += [
        "\nREMINDERS:",
        "- One self-contained HTML page, ZERO external resources.",
        "- Use the sidebar/canvas/metrics layout from the design system exactly.",
        "- Graduate the slider track fill using the --pct CSS custom property trick.",
        "- Implement keyboard shortcuts: Space=play/pause, R=reset (where applicable).",
        "- Canvas drag interaction where it makes physical sense.",
        "- Every control MUST visibly affect canvas AND/OR a metric.",
        "- All computed values MUST follow the real governing equations.",
        "- Include a Play/Animate button + requestAnimationFrame loop if the topic involves motion.",
        "- Follow the SCRIPT ARCHITECTURE CONTRACT exactly: state, then functions, then "
        "init() containing ALL bindSlider()/addEventListener()/first-draw calls, then the "
        "DOMContentLoaded-or-now bootstrap as the last statement. No top-level side effects, "
        "no inline on*= handlers, no try/catch around the whole script.",
        "- Canvas colours must be real colour strings (resolve CSS vars with getComputedStyle); "
        "size the canvas with ctx.setTransform(dpr,0,0,dpr,0,0), never ctx.scale().",
        "- Mobile-responsive down to 380px viewport width.",
        "- REQUIRED: include the <script type=\"application/json\" id=\"tut-config\"> block "
        "(title, intro, 4-8 steps whose selectors are #ids that exist in your page). Do NOT write "
        "any tutorial markup, CSS or JS -- the platform injects and owns the tutorial UI.",
        "\nReturn ONLY raw JSON. simulation_code must be a complete "
        "<!DOCTYPE html>...</html> document as a properly escaped JSON string.",
    ]
    user_content = "\n".join(user_parts)
    return system_text, user_content


# ===========================================================================
#  MODULE 8 -- Response Parsing (fallback chain)
# ===========================================================================

def _parse_response(raw: str, topic: str) -> dict:
    strategies = [
        _parse_direct_json,
        _parse_stripped_json,
        _parse_brace_extracted,
        _parse_field_by_field,
        _parse_markdown_fenced,
        _parse_bare_html,
    ]
    for i, strategy in enumerate(strategies):
        try:
            result = strategy(raw, topic)
            if result:
                SimLogger.ok("Parser", f"Strategy {i+1} ({strategy.__name__}) succeeded")
                return result
        except Exception as e:
            SimLogger.warn("Parser", f"Strategy {i+1} failed: {e}")
    SimLogger.error("Parser", "All strategies failed")
    return {
        "title":             f"Simulation: {topic[:50]}",
        "category":          "GENERAL_PROCESS",
        "summary":           "Generation could not be parsed.",
        "controls_overview": [],
        "key_formula":       "",
        "learning_notes":    [],
        "simulation_code":   "",
    }


def _parse_direct_json(raw, topic):
    data = json.loads(raw)
    return _normalize_parsed(data, topic)


def _parse_stripped_json(raw, topic):
    stripped = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE).strip()
    data = json.loads(stripped)
    return _normalize_parsed(data, topic)


def _parse_brace_extracted(raw, topic):
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return None
    data = json.loads(m.group(0))
    return _normalize_parsed(data, topic)


def _parse_field_by_field(raw, topic):
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

    code = _extract_simulation_code_field(raw)
    if not code:
        return None
    return {
        "title":             extract_string("title") or f"Simulation: {topic[:50]}",
        "category":          extract_string("category") or "GENERAL_PROCESS",
        "summary":           extract_string("summary") or "Interactive simulation",
        "controls_overview": extract_array("controls_overview"),
        "key_formula":       extract_string("key_formula"),
        "learning_notes":    extract_array("learning_notes"),
        "simulation_code":   code,
    }


def _extract_simulation_code_field(raw):
    key_pos = raw.find('"simulation_code"')
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


def _parse_markdown_fenced(raw, topic):
    stripped = raw.strip()
    fence_match = re.match(r'^```(?:html|json)?\s*\n?(.*?)\n?```$', stripped, re.DOTALL | re.IGNORECASE)
    if fence_match:
        inner = fence_match.group(1).strip()
        try:
            data = json.loads(inner)
            result = _normalize_parsed(data, topic)
            if result:
                return result
        except Exception:
            pass
        for marker in ['<!DOCTYPE html>', '<html', '<svg']:
            idx = inner.find(marker)
            if idx != -1:
                end = inner.rfind('</html>')
                code = inner[idx:end + 7] if end != -1 else inner[idx:]
                if len(code) > 200:
                    return {
                        "title":             f"Simulation: {topic[:50]}",
                        "category":          "GENERAL_PROCESS",
                        "summary":           "Interactive simulation",
                        "controls_overview": [],
                        "key_formula":       "",
                        "learning_notes":    [],
                        "simulation_code":   code.strip(),
                    }
    for pat in [r'```html\s*\n(.*?)\n```', r'```json\s*\n(.*?)\n```']:
        m = re.search(pat, raw, re.DOTALL | re.IGNORECASE)
        if m:
            inner = m.group(1).strip()
            try:
                data = json.loads(inner)
                result = _normalize_parsed(data, topic)
                if result:
                    return result
            except Exception:
                pass
            for marker in ['<!DOCTYPE html>', '<html']:
                idx = inner.find(marker)
                if idx != -1:
                    end = inner.rfind('</html>')
                    code = inner[idx:end + 7] if end != -1 else inner[idx:]
                    if len(code) > 200:
                        return {
                            "title":             f"Simulation: {topic[:50]}",
                            "category":          "GENERAL_PROCESS",
                            "summary":           "Interactive simulation",
                            "controls_overview": [],
                            "key_formula":       "",
                            "learning_notes":    [],
                            "simulation_code":   code.strip(),
                        }
    return None


def _parse_bare_html(raw, topic):
    for marker in ['<!DOCTYPE html>', '<html', '<svg']:
        idx = raw.find(marker)
        if idx != -1:
            end = raw.rfind('</html>')
            code = raw[idx:end + 7] if end != -1 else raw[idx:]
            if len(code) > 200:
                return {
                    "title":             f"Simulation: {topic[:50]}",
                    "category":          "GENERAL_PROCESS",
                    "summary":           "Interactive simulation",
                    "controls_overview": [],
                    "key_formula":       "",
                    "learning_notes":    [],
                    "simulation_code":   code.strip(),
                }
    return None


def _normalize_parsed(data, topic):
    if not isinstance(data, dict):
        raise ValueError("Not a dict")
    result = {
        "title":           str(data.get("title") or "").strip() or f"Simulation: {topic[:50]}",
        "category":        str(data.get("category") or "").strip() or "GENERAL_PROCESS",
        "summary":         str(data.get("summary") or "").strip() or "Interactive simulation",
        "key_formula":     str(data.get("key_formula") or "").strip(),
        "simulation_code": str(data.get("simulation_code") or "").strip(),
    }
    controls = data.get("controls_overview")
    result["controls_overview"] = controls if isinstance(controls, list) else []
    notes = data.get("learning_notes")
    result["learning_notes"] = notes if isinstance(notes, list) else []
    return result


def _find_json_string_end(s):
    i = 0
    while i < len(s):
        if s[i] == '\\':
            i += 2
        elif s[i] == '"':
            return i
        else:
            i += 1
    return -1


def _unescape_json_string(s):
    return (s.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
             .replace('\\r', '\r').replace("\\'", "'").replace('\\\\', '\\'))


# ===========================================================================
#  MODULE 9 -- Generation Pipeline
# ===========================================================================

def _build_failure_result(topic, reason):
    fallback = RecoveryEngine.fallback_html(topic, reason)
    return {
        "title":             f"Simulation: {topic[:50]}",
        "category":          "GENERAL_PROCESS",
        "summary":           "Generation failed",
        "controls_overview": [],
        "key_formula":       "",
        "learning_notes":    [],
        "image_refs":        [],
        "html":              fallback,
        "engine_version":    "v2.1",
        "render_status":     "error",
        "error_reason":      reason,
    }


async def _run_generation_pipeline(topic: str) -> dict:
    short_topic = topic[:80] + ("..." if len(topic) > 80 else "")
    SimLogger.info("Pipeline", f"START v2.1 -- '{short_topic}'")

    # Step 1: Classify
    category = await _classify_topic(topic)
    SimLogger.info("Classifier", f"Category: {category}")

    # Step 2: Image references (blocking urllib → worker thread)
    image_refs = await asyncio.to_thread(_fetch_image_refs, topic)

    # Step 3: Build prompt
    system_text, user_content = _build_prompt(topic, category, image_refs)

    # Step 4: Generate via Gemini (mirrors q_animation._call_gemini pattern exactly)
    try:
        try:
            config = _genai_types.GenerateContentConfig(
                system_instruction=system_text,
                temperature=0.7,
                max_output_tokens=MAX_TOK,
                thinking_config=_genai_types.ThinkingConfig(thinking_level="low"),
            )
        except Exception:
            # ThinkingConfig not supported on this SDK version — use minimal config
            config = _genai_types.GenerateContentConfig(
                system_instruction=system_text,
                temperature=0.7,
                max_output_tokens=MAX_TOK,
            )
        response = await _gemini_client.aio.models.generate_content(
            model=SIM_MODEL,
            contents=user_content,
            config=config,
        )
        raw    = (response.text or "").strip()
        finish = getattr(response.candidates[0], 'finish_reason', 'unknown') if response.candidates else 'unknown'
        SimLogger.info("GenerationAI", f"model={SIM_MODEL}  finish_reason={finish}  len={len(raw)}")
        if finish in ('MAX_TOKENS', 'max_tokens', 2):
            SimLogger.warn("GenerationAI", "Hit max_output_tokens -- output may be truncated!")

    except Exception as e:
        err_str = str(e)
        is_model_not_found = (
            "404" in err_str or "NOT_FOUND" in err_str or
            "not found" in err_str.lower() or
            "is not supported for generateContent" in err_str
        )
        is_auth_error = (
            "401" in err_str or "403" in err_str or
            "UNAUTHENTICATED" in err_str or "API_KEY_INVALID" in err_str
        )
        if is_model_not_found:
            SimLogger.error(
                "GenerationAI",
                f"CRITICAL: Model '{SIM_MODEL}' does not exist or is not supported. "
                f"Set SIM_MODEL env var to a valid model (e.g. 'gemini-3.1-pro-preview'). "
                f"Raw error: {err_str}"
            )
            return _build_failure_result(
                topic,
                f"Model '{SIM_MODEL}' not found. Check your SIM_MODEL environment variable."
            )
        elif is_auth_error:
            SimLogger.error(
                "GenerationAI",
                f"CRITICAL: API authentication failed ({err_str[:200]}). "
                f"Check GEMINI_API_KEY environment variable."
            )
            return _build_failure_result(topic, "API authentication failed. Check your GEMINI_API_KEY.")
        else:
            SimLogger.error("GenerationAI", f"API call failed: {err_str}")
            return _build_failure_result(topic, f"API error: {err_str}")

    # Step 5: Parse
    result = _parse_response(raw, topic)
    result["category"]   = result.get("category") or category
    result["image_refs"] = image_refs
    sim_html = result.get("simulation_code", "").strip()

    if not sim_html:
        SimLogger.error("Pipeline", "No simulation_code could be parsed from the response")
        return _build_failure_result(topic, "Could not parse simulation HTML from model response")

    # Step 6: Auto-repair truncated closing tags before validation
    sim_html = GenerationValidator.repair(sim_html)

    # Step 7: Sanitize
    sim_html = HtmlSanitizer.sanitize(sim_html)

    # Step 8: Validate
    try:
        GenerationValidator.validate(sim_html, require_svg=False, require_canvas=False)
    except ValidationError as e:
        SimLogger.warn("Validator", f"Validation failed: {e}")
        if ('<canvas' in sim_html or '<svg' in sim_html) and len(sim_html) > 400:
            sim_html = RecoveryEngine.partial_html(topic, sim_html)
            SimLogger.warn("Pipeline", "Wrapped partial content via RecoveryEngine.partial_html")
        else:
            return _build_failure_result(topic, str(e))

    result["html"]           = sim_html
    result["engine_version"] = "v2.1"
    result["render_status"]  = "ok"

    SimLogger.ok("Pipeline", (
        f"DONE -- '{result['title']}'  category={result['category']}"
        f"  html={len(sim_html):,} chars"
        f"  controls={len(result.get('controls_overview', []))}"
        f"  image_refs={len(image_refs)}"
    ))
    return result


# ===========================================================================
#  Public API
# ===========================================================================

async def generate_simulation(topic: str) -> dict:
    """
    Public async entry point. Never raises — returns a graceful fallback
    page on any error.

    Returns dict with keys: title, category, summary, controls_overview,
    key_formula, learning_notes, image_refs, html, engine_version,
    render_status. On failure: render_status='error', error_reason set.
    """
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("Topic cannot be empty")
    try:
        return await asyncio.wait_for(
            _run_generation_pipeline(topic), timeout=PIPELINE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        SimLogger.error(
            "Pipeline",
            f"Pipeline exceeded {PIPELINE_TIMEOUT_SECONDS:.0f}s wall-clock cap -- "
            "returning graceful timeout result")
        return _build_failure_result(
            topic,
            f"Generation took longer than {PIPELINE_TIMEOUT_SECONDS:.0f}s and was stopped. "
            "Please try again -- shorter or more specific topics generate faster.")
    except Exception as e:
        SimLogger.error("Pipeline", f"UNHANDLED error -- falling back gracefully: {e}")
        return _build_failure_result(topic, f"Unexpected error: {e}")


def generate_simulation_sync(topic: str) -> dict:
    """Synchronous wrapper around generate_simulation() for non-async callers."""
    return asyncio.run(generate_simulation(topic))


# ---------------------------------------------------------------------------
# Streaming entry point (prevents gateway 502s on long generations)
# ---------------------------------------------------------------------------
# Wire this as SSE in FastAPI:
#
#   from fastapi.responses import StreamingResponse
#   @app.post("/generate-simulation-stream")
#   async def stream_endpoint(topic: str):
#       async def sse():
#           async for event in generate_simulation_stream(topic):
#               yield f"data: {json.dumps(event)}\n\n"
#       return StreamingResponse(sse(), media_type="text/event-stream")
#
# Events emitted:
#   {"type": "status",  "stage": str, "message": str}
#   {"type": "chunk",   "text": str}
#   {"type": "done",    "result": <same shape as generate_simulation()>}
#   {"type": "error",   "result": <failure result dict>}
# ---------------------------------------------------------------------------

async def generate_simulation_stream(topic: str):
    """
    Async generator yielding progress events for the full generation pipeline.
    Streams model tokens continuously so gateway inactivity timeouts don't fire.
    Never raises — failures come as {"type": "error", ...} events.
    """
    topic = (topic or "").strip()
    if not topic:
        yield {"type": "error", "result": _build_failure_result("", "Topic cannot be empty")}
        return

    short_topic = topic[:80] + ("..." if len(topic) > 80 else "")
    SimLogger.info("Pipeline", f"START (stream) v2.1 -- '{short_topic}'")

    try:
        yield {"type": "status", "stage": "classify", "message": "Classifying topic..."}
        category = await asyncio.wait_for(_classify_topic(topic), timeout=CLIENT_TIMEOUT_SECONDS)
        SimLogger.info("Classifier", f"Category: {category}")

        yield {"type": "status", "stage": "image_refs", "message": "Gathering visual references..."}
        image_refs = await asyncio.wait_for(
            asyncio.to_thread(_fetch_image_refs, topic), timeout=CLIENT_TIMEOUT_SECONDS)

        system_text, user_content = _build_prompt(topic, category, image_refs)

        yield {"type": "status", "stage": "generating", "message": "Generating simulation..."}
        raw_parts: List[str] = []
        try:
            stream_config = _genai_types.GenerateContentConfig(
                system_instruction=system_text,
                temperature=0.7,
                max_output_tokens=MAX_TOK,
                thinking_config=_genai_types.ThinkingConfig(thinking_level="low"),
            )
        except Exception:
            stream_config = _genai_types.GenerateContentConfig(
                system_instruction=system_text,
                temperature=0.7,
                max_output_tokens=MAX_TOK,
            )
        async for chunk in await _gemini_client.aio.models.generate_content_stream(
            model=SIM_MODEL,
            contents=user_content,
            config=stream_config,
        ):
            text = chunk.text or ""
            if text:
                raw_parts.append(text)
                yield {"type": "chunk", "text": text}
        raw = "".join(raw_parts).strip()
        SimLogger.info("GenerationAI", f"model={SIM_MODEL}  len={len(raw)}")


        result   = _parse_response(raw, topic)
        result["category"]   = result.get("category") or category
        result["image_refs"] = image_refs
        sim_html = result.get("simulation_code", "").strip()

        if not sim_html:
            SimLogger.error("Pipeline", "No simulation_code could be parsed from the response")
            yield {"type": "error", "result": _build_failure_result(
                topic, "Could not parse simulation HTML from model response")}
            return

        # Auto-repair truncated closing tags before validation
        sim_html = GenerationValidator.repair(sim_html)

        sim_html = HtmlSanitizer.sanitize(sim_html)

        try:
            GenerationValidator.validate(sim_html, require_svg=False, require_canvas=False)
        except ValidationError as e:
            SimLogger.warn("Validator", f"Validation failed: {e}")
            if ('<canvas' in sim_html or '<svg' in sim_html) and len(sim_html) > 400:
                sim_html = RecoveryEngine.partial_html(topic, sim_html)
            else:
                yield {"type": "error", "result": _build_failure_result(topic, str(e))}
                return


        result["html"]           = sim_html
        result["engine_version"] = "v2.1"
        result["render_status"]  = "ok"

        SimLogger.ok("Pipeline", f"DONE (stream) -- '{result['title']}'  html={len(sim_html):,} chars")
        yield {"type": "done", "result": result}

    except asyncio.TimeoutError:
        SimLogger.error("Pipeline", "Stage exceeded its timeout during streaming pipeline")
        yield {"type": "error", "result": _build_failure_result(
            topic, "Generation took too long and was stopped. Please try again.")}
    except Exception as e:
        err_str = str(e)
        is_model_not_found = (
            "404" in err_str or "NOT_FOUND" in err_str or
            "not found" in err_str.lower() or
            "is not supported for generateContent" in err_str
        )
        is_auth_error = (
            "401" in err_str or "403" in err_str or
            "UNAUTHENTICATED" in err_str or "API_KEY_INVALID" in err_str
        )
        if is_model_not_found:
            SimLogger.error(
                "Pipeline",
                f"CRITICAL: Model '{SIM_MODEL}' does not exist or is not supported. "
                f"Set SIM_MODEL env var to 'gemini-3.1-pro-preview'. Raw error: {err_str}"
            )
            yield {"type": "error", "result": _build_failure_result(
                topic, f"Model '{SIM_MODEL}' not found. Check your SIM_MODEL env var.")}
        elif is_auth_error:
            SimLogger.error("Pipeline", f"CRITICAL: API auth failed. Check GEMINI_API_KEY. ({err_str[:200]})")
            yield {"type": "error", "result": _build_failure_result(
                topic, "API authentication failed. Check your GEMINI_API_KEY.")}
        else:
            SimLogger.error("Pipeline", f"UNHANDLED error in streaming pipeline: {err_str}")
            yield {"type": "error", "result": _build_failure_result(topic, f"Unexpected error: {err_str}")}


# ===========================================================================
#  CLI TEST
# ===========================================================================
if __name__ == "__main__":
    import sys

    TEST_TOPICS = {
        "MECHANICS":  "Simple pendulum with adjustable length, gravity, and damping",
        "OPTICS":     "Convex lens image formation with adjustable object distance and focal length",
        "CIRCUITS":   "RC circuit charging and discharging through a resistor",
        "BIOLOGY":    "Logistic population growth with adjustable growth rate and carrying capacity",
        "MATH":       "Unit circle and the sine/cosine waveform it traces out",
        "ALGORITHMS": "Bubble sort visualized step by step on a random array",
        "CHEMISTRY":  "Gas particles in a container demonstrating Boyle's law",
        "WAVES":      "Double-slit interference pattern with adjustable slit separation",
        "EARTH":      "Carbon cycle and greenhouse gas concentration vs temperature",
        "ECONOMICS":  "Supply and demand curves with adjustable elasticity and tax",
    }

    if len(sys.argv) > 1:
        topics_to_test = {"CUSTOM": " ".join(sys.argv[1:])}
    else:
        key = "MECHANICS"
        topics_to_test = {key: TEST_TOPICS[key]}

    for cat, t in topics_to_test.items():
        print("=" * 72)
        print(f"  SimEngine v2.1 | {cat}")
        print(f"  Topic: {t[:65]}")
        print("=" * 72)

        t0      = time.time()
        result  = generate_simulation_sync(t)
        elapsed = time.time() - t0

        print(f"\nTitle           : {result.get('title','N/A')}")
        print(f"Category        : {result.get('category','N/A')}")
        print(f"Render Status   : {result.get('render_status','N/A')}")
        print(f"Engine Version  : {result.get('engine_version','N/A')}")
        print(f"Summary         : {result.get('summary','')[:140]}")
        print(f"Key Formula     : {result.get('key_formula','')[:80]}")
        print(f"Controls        : {result.get('controls_overview',[])}")
        print(f"Learning Notes  : {len(result.get('learning_notes',[]))} note(s)")
        print(f"Image Refs      : {len(result.get('image_refs',[]))} image(s)")
        html_out = result.get("html", "")
        print(f"HTML Size       : {len(html_out):,} chars")
        print(f"Total Time      : {elapsed:.1f}s")

        if result.get("error_reason"):
            print(f"Error Reason    : {result['error_reason']}")

        slug     = cat.lower()
        out_path = f"sim_{slug}.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_out)
        print(f"\nSaved -> {out_path}")
        print()
