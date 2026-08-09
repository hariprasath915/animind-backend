"""
q_animation.py  --  QAnim Question Animation Generator  v2.0
=============================================================

v2.0 -- 9-STEP WORKFLOW ENFORCEMENT (built on v1.1 Gemini rewrite):

  NEW IN v2.0:
  - Every animation now enforces exactly 9 steps:
      Steps 1-6: Animated SVG reveal (problem setup, one given per step)
      Step 7:    Formula modal — formula + coloured variable cards
      Step 8:    Substitution modal — step-by-step with system diagram
      Step 9:    Final Answer modal (NEW) — substitution chain + green
                 final box + key insight bar (matches reference HTML)
  - GeminiSceneAnalyzer: mandates exactly 6 SVG steps
  - GeminiAnimationBuilder: enforces 9 dots, correct nextStep() flow
  - inject_scene9_final_answer(): new injection function
  - PanelInjectionManager: Scene9 wired into all dispatch tables

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
  - All post-processing injection functions (ToFind, AnswerBox,
    Notes, ControlsBar, Glossary, StepController, NavPatch)
  - Scene 6 (Main Formula) / Scene 7 (Substitution) teach the formula and
    walk through solving in-canvas, one piece at a time — no separate
    scrollable panel (the old StepAnswer module was retired for this).
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

# ---------------------------------------------------------------------------
# Hard timeout budgets — every Gemini-calling stage MUST return within these
# windows, one way or another (real result OR a clean fallback/exception).
# Without this, a single slow/overloaded Gemini call had no ceiling at all:
# retry sleeps (up to 155s) plus an uncapped network call could make one
# stage alone run for minutes with nothing to cut it off. That unbounded
# worst case is the actual cause of "taking much longer than expected" --
# it doesn't happen every time, only when Gemini is slow, which is why it
# felt random/recurring instead of a clean reproducible bug.
#
# ROOT CAUSE OF THE "Animation build error:" (blank reason) FALLBACK:
# gemini-3.1-pro-preview is a Gemini-3-generation model, and Google's own
# docs state thinking CANNOT be disabled for Gemini 3 / 3.1 Pro — even with
# thinking_level="low" every call still pays a mandatory reasoning-token
# cost before the visible output starts streaming. The animation builder
# asks for a single ~32k-token, fully self-contained HTML page in one shot
# (unlike the "small" stages, which return a few hundred tokens of JSON),
# so its realistic latency is far higher than the other stages. The old
# STAGE_TIMEOUT_BUILD=75s budget was simply too tight for that combination,
# so builds routinely hit asyncio.TimeoutError. That exception's message is
# the empty string by default, so the pipeline's `except Exception as e`
# handler produced literally "Animation build error: " with nothing after
# the colon — which is exactly the blank box users were seeing. Both the
# timing AND the blank-message bug are fixed below (see _err_msg()).
# ---------------------------------------------------------------------------
STAGE_TIMEOUT_SMALL = 180.0   # classify/solution/glossary calls. Raised from 90s to
                              # 180s: the inner _call_gemini retry ladder uses
                              # RETRY_DELAYS=[10,25,50] (85s of sleeping) plus up to
                              # 3 actual API calls (~30s each), so 90s was cutting off
                              # all retries before they could succeed on the 2nd question.
                              # -> 90s because the retry ladder inside
                              # GeminiSolutionGenerator._call_gemini (up to
                              # 2 outer attempts x 3 inner retries with
                              # 4s/8s/15s backoff = ~54s worst case) could
                              # exceed the old 40s budget on a single 429/503,
                              # causing generate_async() to hit asyncio.wait_for's
                              # timeout and silently return _FALLBACK even
                              # though the retry would have succeeded shortly
                              # after. 90s gives the full retry ladder room
                              # to finish before we give up.
STAGE_TIMEOUT_SCENE = 150.0   # scene-analysis (the stage that decides WHAT
                              # the main animation actually shows — the real
                              # physical scene, e.g. charges/fields/forces
                              # being placed step by step, vs. a placeholder).
                              # This stage internally retries up to 3 times,
                              # each asking a thinking-locked Gemini 3.1 Pro
                              # model for up to 16,384 tokens of JSON — that
                              # does NOT fit in the 40s budget shared by the
                              # lightweight stages. When it timed out, the
                              # pipeline silently fell back to a generic,
                              # non-physical "Setup/Given/Formula/Substitute/
                              # Solution" placeholder script instead of a real
                              # step-by-step scene — exactly the "not
                              # step-by-step like the reference" symptom this
                              # constant fixes. Same class of bug as the
                              # STAGE_TIMEOUT_BUILD fix below — give a stage
                              # that does real multi-attempt work its own
                              # realistic budget instead of sharing a tight one.
                              # NOTE: 100.0 was tried first but was STILL not
                              # enough — observed logs showed attempt1 (33s) +
                              # attempt2 (36s) = 69s elapsed, then the 100s
                              # timeout fired while attempt3 was still running
                              # in the background (finishing at ~157s total),
                              # so the async wrapper raced ahead and returned
                              # its own fallback before the real 3rd attempt —
                              # which had a chance to succeed — ever got to
                              # report back. 150s gives 3 attempts a realistic
                              # ~50s each, matching STAGE_TIMEOUT_BUILD's
                              # single-shot budget for a similarly heavy call.
STAGE_TIMEOUT_BUILD = 150.0   # animation HTML builder — one large, mandatory-
                              # thinking, ~32k-token single-shot generation.
                              # Doubled from 75s: that budget was measured
                              # against ordinary (non-thinking-locked) models
                              # and was not enough headroom for Gemini 3.1 Pro.
# IMPORTANT: the pipeline's critical path is SEQUENTIAL, not flat —
#   Stage 0 (classify, ~instant, no API)
#   -> concurrent gather of scene/solution/glossary  (bounded by
#      max(STAGE_TIMEOUT_SCENE, STAGE_TIMEOUT_SMALL), since scene-analysis
#      now runs on its own, longer budget than solution/glossary)
#   -> animation HTML builder                        (bounded by STAGE_TIMEOUT_BUILD)
#   -> sanitize/post-process (~instant, no API)
# So the true worst case is that max(...) + STAGE_TIMEOUT_BUILD, plus a
# margin for JSON parsing / sanitization overhead. PIPELINE_TIMEOUT must be
# comfortably ABOVE that sum, or it becomes a guaranteed failure on any run
# where stages simply use their normal allotted time (not just on overload).
# An earlier version of this fix set PIPELINE_TIMEOUT=95 while the two stage
# budgets alone summed to 110 — mathematically impossible to complete within,
# which is why the fallback fired on ordinary, non-overloaded runs. Fixed here.
# Keep this derived from the stage constants (never hardcode a total) so the
# two can never drift out of sync again.
PIPELINE_TIMEOUT = max(STAGE_TIMEOUT_SCENE, STAGE_TIMEOUT_SMALL) + STAGE_TIMEOUT_BUILD + 20.0  # = 270.0


def _err_msg(e: BaseException) -> str:
    """
    Always return a non-empty, human-readable description of an exception.

    Several common exceptions — most importantly asyncio.TimeoutError —
    stringify to '' by default. Every place in this module that used to do
    f"...error: {e}" could therefore render a completely blank reason box
    (this is precisely what produced the "Animation build error:" screen
    with nothing after the colon). Route ALL user-facing error strings
    through this helper instead of interpolating exceptions directly.
    """
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
    PREFIX = "[QAnim v1.2]"

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
#  MODULE 1.5 — Robust JSON Sanitizer (shared by all Gemini JSON parsers)
# ===========================================================================

def _sanitize_json_str(raw: str) -> str:
    """
    Clean common LLM JSON defects before json.loads().

    Handles ALL of these real Gemini output patterns:
      • Markdown fences:           ```json ... ```  or  ``` ... ```
      • Thinking tags:             <thinking>...</thinking>
      • JS-style // line comments  (outside strings)
      • JS-style /* */ comments    (outside strings)
      • Trailing commas:           {... , }  or  [... , ]
      • Single-quoted keys/values: {'key': 'val'}  →  {"key": "val"}
      • Unquoted keys:             {key: "val"}     →  {"key": "val"}
      • Python literals:           True/False/None  →  true/false/null
      • Ellipsis placeholders:     ...              →  (removed)
      • BOM / stray whitespace
    """
    # 1. Strip BOM + surrounding whitespace
    raw = raw.lstrip('\ufeff').strip()

    # 2. Strip ALL markdown fences — handle both single-line and multi-line
    #    variants:  ```json\n…\n```  or  ```\n…\n```  or  `{…}`
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'```\s*$', '', raw, flags=re.IGNORECASE).strip()

    # 3. Strip <thinking>…</thinking> blocks (Gemini 2.x style)
    raw = re.sub(r'<thinking>.*?</thinking>', '', raw, flags=re.DOTALL).strip()

    # 4. Extract the outermost { … } via balanced-brace scan so we drop
    #    any preamble / trailing prose the model prepended or appended.
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

    # --- From here, work on the extracted JSON text ---

    # 5. Remove JS line comments  //…  that are outside strings.
    #    We walk char-by-char to avoid killing URLs ("https://...").
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
        if not in_str and ch == '/' and i + 1 < len(raw):
            if raw[i + 1] == '/':          # // comment → skip to EOL
                while i < len(raw) and raw[i] != '\n':
                    i += 1
                continue
            if raw[i + 1] == '*':          # /* comment → skip to */
                i += 2
                while i < len(raw) - 1 and not (raw[i] == '*' and raw[i + 1] == '/'):
                    i += 1
                i += 2                     # skip closing */
                continue
        out.append(ch)
        i += 1
    raw = ''.join(out)

    # 6. Remove trailing commas before } or ]
    #    e.g.  { "a": 1, }  →  { "a": 1 }
    raw = re.sub(r',\s*([}\]])', r'\1', raw)

    # 7. Replace Python literals with JSON equivalents (outside strings)
    #    True → true,  False → false,  None → null
    raw = re.sub(r'\bTrue\b',  'true',  raw)
    raw = re.sub(r'\bFalse\b', 'false', raw)
    raw = re.sub(r'\bNone\b',  'null',  raw)

    # 8. Replace single-quoted strings with double-quoted
    #    Only do a simple pass — full single-quote parsing is complex, but
    #    this catches the most common pattern: 'value'  or  'key'
    #    We avoid replacing apostrophes inside words (e.g. it's).
    #    Strategy: replace  'text'  that are adjacent to : , { } [ ]
    raw = re.sub(
        r"(?<![a-zA-Z])'([^'\\]*(?:\\.[^'\\]*)*)'(?![a-zA-Z])",
        lambda m: '"' + m.group(1).replace('"', '\\"') + '"',
        raw,
    )

    # 9. Remove ellipsis placeholders  ...  that break JSON
    raw = re.sub(r'\.\.\.\s*', '', raw)

    # 10. Repair unescaped double-quotes INSIDE string values.
    #     Gemini frequently writes descriptions/labels containing a literal
    #     " (an inch mark, a quoted phrase, a units abbreviation, etc.)
    #     without escaping it, e.g.:  "desc": "The 5" gap closes"
    #     That inner " prematurely closes the JSON string, so the parser
    #     then finds "gap closes"" where it expects a ',' delimiter — this
    #     is exactly the "Expecting ',' delimiter" failure seen in practice.
    #     Fix: walk the text tracking string state; whenever we hit a `"`
    #     while already inside a string, only treat it as the real closing
    #     quote if the next non-whitespace character is one of : , } ] or
    #     end-of-text (i.e. it's acting as a genuine JSON delimiter).
    #     Otherwise it's an embedded quote — escape it and stay in-string.
    raw = _repair_unescaped_inner_quotes(raw)

    return raw.strip()


def _repair_unescaped_inner_quotes(text: str) -> str:
    """Escape stray `"` characters found inside JSON string values.

    Safe/idempotent on well-formed JSON: a legitimate closing quote is
    always immediately followed (modulo whitespace) by one of : , } ] or
    the end of the text, so those are left untouched. Any other quote
    encountered while already inside a string is an unescaped embedded
    quote and gets escaped instead.
    """
    out = []
    i = 0
    n = len(text)
    in_str = False
    esc = False
    while i < n:
        ch = text[i]
        if esc:
            out.append(ch)
            esc = False
            i += 1
            continue
        if ch == '\\':
            out.append(ch)
            if in_str:
                esc = True
            i += 1
            continue
        if ch == '"':
            if not in_str:
                in_str = True
                out.append(ch)
                i += 1
                continue
            # Already inside a string — decide if this is the real closer.
            j = i + 1
            while j < n and text[j] in ' \t\r\n':
                j += 1
            nxt = text[j] if j < n else ''
            if j >= n or nxt in (',', '}', ']', ':'):
                in_str = False
                out.append(ch)
                i += 1
                continue
            else:
                out.append('\\"')
                i += 1
                continue
        out.append(ch)
        i += 1
    return ''.join(out)


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
#  MODULE 2.6 — JsSyntaxValidator
# ===========================================================================
class JsSyntaxValidator:
    """Validates that every inline <script> block in the generated HTML is
    syntactically valid JavaScript, and can auto-repair the #1 recurring
    cause of "buttons don't work" reports.

    ROOT CAUSE THIS EXISTS FOR:
    Gemini sometimes writes prime notation (l', theta', i') as a raw
    apostrophe inside a single-quoted JS string literal, e.g.:
        badges: '<span ...>l' = 32 cm</span>',
    That apostrophe closes the string early, which is a JS SYNTAX ERROR.
    A syntax error anywhere in a <script> block prevents the ENTIRE block
    from running -- not just that one line -- so nextStep(), applyStep(),
    and window.onload silently never get defined. The page still *looks*
    correct on load because step 1's text is hardcoded in the static HTML,
    but every button wired to that block is then permanently dead, with
    zero visible error to the user (only a console error in DevTools).
    GenerationValidator/PanelInjectionManager never caught this because
    they only check whether certain id strings are PRESENT in the HTML --
    they never check whether the resulting JavaScript actually parses.
    """

    _SCRIPT_RE = re.compile(r'<script\b([^>]*)>(.*?)</script>', re.DOTALL | re.IGNORECASE)

    @classmethod
    def find_errors(cls, html: str):
        """Returns [(script_id, error_message), ...] for every inline
        <script> block that fails to parse. Empty list = all good."""
        errors = []
        for attrs, body in cls._SCRIPT_RE.findall(html):
            attrs_l = attrs.lower()
            if 'application/json' in attrs_l or 'src=' in attrs_l:
                continue  # data islands and external scripts aren't JS to parse here
            if not body.strip():
                continue
            m = re.search(r'id=["\']([^"\']+)["\']', attrs)
            script_id = m.group(1) if m else "(unnamed script)"
            err = cls._check_syntax(body)
            if err:
                errors.append((script_id, err))
        return errors

    @classmethod
    def _check_syntax(cls, code: str):
        """Returns an error string if `code` is invalid JS, else None.
        Tries Node's own engine first (exactly what the browser uses),
        then falls back to a pure-Python parser (pip install esprima) if
        Node isn't available on this host. If neither is available, logs
        once and skips validation rather than failing the whole pipeline."""
        try:
            import subprocess, tempfile, os as _os
            fd, path = tempfile.mkstemp(suffix='.js')
            try:
                with _os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(code)
                proc = subprocess.run(
                    ["node", "--check", path],
                    capture_output=True, text=True, timeout=5,
                )
                return proc.stderr.strip()[:500] if proc.returncode != 0 else None
            finally:
                try:
                    _os.unlink(path)
                except OSError:
                    pass
        except FileNotFoundError:
            pass  # node not on PATH -- fall through to esprima
        except Exception:
            pass

        try:
            import esprima
            esprima.parseScript(code)
            return None
        except ImportError:
            QAnimLogger.warn(
                "JsSyntaxValidator",
                "Neither `node` nor `esprima` is available -- JS syntax "
                "validation skipped. Install one (`pip install esprima` "
                "needs no system deps) to enable this safety net.",
            )
            return None
        except Exception as e:
            return str(e)[:500]

    @classmethod
    def auto_fix_stray_apostrophes(cls, html: str) -> str:
        """Best-effort repair for the exact failure shape above: a raw
        apostrophe sitting between '>' and the next '<' inside a
        <script> block is HTML *text content* embedded in a JS string --
        never a legitimate JS token boundary in this codebase's generated
        output -- so it's safe to HTML-entity-encode automatically once
        validation has already flagged that block as broken."""
        def _fix_script(m):
            attrs, body = m.group(1), m.group(2)
            attrs_l = attrs.lower()
            if 'application/json' in attrs_l or 'src=' in attrs_l:
                return m.group(0)
            fixed_body = re.sub(r"(>)'(?=[^<]*<)", r"\1&#39;", body)
            return f"<script{attrs}>{fixed_body}</script>"
        return cls._SCRIPT_RE.sub(_fix_script, html)


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
#  MODULE 6.5 — (removed) The old scrollable "Step by Step Answer" panel used to
#  live here. It has been retired: Scene 6 (Main Formula) and Scene 7
#  (Substitution) below now teach the formula and walk through the
#  substitution/solving process directly inside the animation, in sequence,
#  with no separate scrollable section and no page scrolling required.
# ===========================================================================

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
  "formula_why": "One clear sentence on WHY this is the governing formula/principle for this exact problem (what physical law it comes from and when it applies).",
  "variable_meanings": [
    {"symbol": "rho", "meaning": "Fluid density", "unit": "kg/m3", "value": "1000"},
    {"symbol": "V",   "meaning": "Flow velocity",  "unit": "m/s",   "value": "2"},
    {"symbol": "D",   "meaning": "Pipe diameter",  "unit": "m",     "value": "0.05"},
    {"symbol": "mu",  "meaning": "Dynamic viscosity", "unit": "Pa.s", "value": "0.001"}
  ],
  "substitution_steps": [
    {"title": "Calculate Reynolds Number",      "expr": "Re = (1000 x 2 x 0.05) / 0.001 = 100000", "description": "We substitute the known density, velocity, diameter and viscosity to check the flow regime."},
    {"title": "Calculate Prandtl Number",       "expr": "Pr = (0.001 x 4200) / 0.6 = 7", "description": "Pr compares momentum diffusivity to thermal diffusivity and is needed for the Nusselt correlation."},
    {"title": "Apply Dittus-Boelter Equation",  "expr": "Nu = 0.023 x (100000)^0.8 x (7)^0.4 = 365", "description": "With Re and Pr known, the empirical correlation gives the dimensionless heat transfer number."},
    {"title": "Find Heat Transfer Coefficient", "expr": "h = 365 x 0.6 / 0.05 = 4380 W/(m2.K)", "description": "Multiplying Nu by the fluid conductivity and dividing by the diameter converts back to a physical coefficient."}
  ],
  "final_answer": "h = 4380 W/(m2.K),  Re = 100000,  Nu = 365",
  "final_answer_unit": "W/(m2.K)",
  "key_insight": "Higher flow velocity raises Re, which boosts h through the 0.8-power relationship.",
  "real_world_note": "One short optional sentence on where this result matters in practice (e.g. heat exchanger sizing). Set to \"\" if not meaningful."
}

STRICT RULES:
- given_data: Extract EVERY numerical value stated in the question. Format each as \"symbol = value unit\". Minimum 2, maximum 12 items. Never leave empty.
- to_find: List EVERYTHING the question asks to find, prefixed i) ii) iii) etc. Never leave empty.
- formulas: 2-6 key formulas arranged as an input-to-output chain. Each entry MUST have \"text\" (the formula expression) and \"color\" (one of: blue, orange, purple, pink, green, teal). These are rendered as a visual flowchart with arrows between them. Never leave empty.
- formula_note: Optional note about evaluation conditions (e.g. bulk temperature). Set to \"\" if not applicable.
- formula_why: One sentence, plain English, explaining why THIS formula/principle is the correct one to reach for. Never leave empty.
- variable_meanings: ONE entry per distinct symbol used in given_data/formulas. Each entry MUST have \"symbol\", \"meaning\" (what the variable physically represents), \"unit\" (correct SI or given unit), and \"value\" (the given numerical value, or \"?\" if it is the unknown being solved for). This is used to teach the formula variable-by-variable — never leave empty, never invent a variable that is not actually in the formula.
- substitution_steps: 3-6 numbered calculation steps. Each MUST have \"title\" (what this step computes), \"expr\" (the actual mathematical expression with REAL numbers substituted and the computed result shown), and \"description\" (ONE short sentence explaining WHY this step is done and what it accomplishes — the reasoning a teacher would say aloud, not just a restatement of the math). Never leave empty.
- final_answer: Complete answer containing ALL computed numerical values with units. Must NEVER be empty.
- final_answer_unit: The correct SI (or standard) unit of the primary requested quantity, written cleanly (e.g. \"W/(m2.K)\", \"m/s\", \"N\"). Must NEVER be empty.
- key_insight: One clear memorable sentence about the core physics or mathematical concept. Must NEVER be empty.
- real_world_note: One short optional real-world interpretation of the result. Set to \"\" (empty string) if nothing meaningful applies — never fabricate a forced example.

ACCURACY REQUIREMENTS — NON-NEGOTIABLE:
- The formula(s) you select MUST be the mathematically and physically correct ones for exactly what this question asks — verify the governing principle before writing anything down.
- NEVER approximate, round prematurely, or substitute a similar-but-wrong formula. NEVER hallucinate a constant, property value, or relationship that was not given or is not a standard, correct physical constant.
- Use correct SI units throughout (or the unit system explicitly given in the question) and correct standard variable notation for the subject (e.g. rho for density, mu for dynamic viscosity).
- Every number in substitution_steps must be traceable to either a given value or a previously-computed intermediate result in this same solution — never introduce an unexplained number.
- If you are not fully certain a value or formula is correct, prefer the standard textbook form for that topic rather than guessing.

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
        "formula_why": "This formula directly connects the given quantities to the unknown asked for in the question.",
        "variable_meanings": [],
        "substitution_steps": [
            {"title": "Identify Given Values",  "expr": "List all values from the question with their units.", "description": "We start by writing down everything we already know, with correct units, before touching the formula."},
            {"title": "Select Formula",         "expr": "Choose the correct governing equation for this problem type.", "description": "The right formula connects the given quantities to the one we need to find."},
            {"title": "Substitute and Solve",   "expr": "Insert the known values and evaluate step by step.", "description": "Plugging in the numbers and simplifying carefully avoids arithmetic and unit errors."},
        ],
        "steps": [
            "Step 1: Write down the given values from the question.",
            "Step 2: Identify what needs to be found.",
            "Step 3: Choose the correct governing formula.",
            "Step 4: Substitute values and solve step by step.",
            "Step 5: State the final answer with units.",
        ],
        "final_answer": "Please re-generate for a detailed answer.",
        "final_answer_unit": "",
        "key_insight":  "Always identify given values and the target quantity before selecting a formula.",
        "real_world_note": "",
        "raw": "",
        "_used_fallback": True,   # marks this dict as placeholder content so
                                  # downstream rendering can surface a visible
                                  # warning instead of silently shipping generic
                                  # text that looks like a real solved answer.
    }

    @classmethod
    def generate(cls, question: str) -> dict:
        if _gemini_client is None:
            QAnimLogger.warn("GeminiSolution", "Gemini client not available — using fallback")
            return dict(cls._FALLBACK)

        QAnimLogger.info("GeminiSolution", f"Generating solution via {GEMINI_MODEL}...")
        user_prompt = (
            f"Solve this question step by step:\n\n"
            f"QUESTION: {question[:800]}\n\n"
            f"Return ONLY valid JSON — no markdown, no preamble, no explanation. "
            f"Start your response with {{ and end with }}."
        )

        MAX_ATTEMPTS = 4   # 4 outer attempts with inter-attempt back-off (5/10/15 s)
        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = cls._call_gemini(user_prompt, cls._solution_system_text(), max_tokens=4096)
                result = cls._parse(raw)
                # Validate the result is NOT the fallback.
                # OLD check required substitution_steps — too strict: Gemini sometimes
                # returns real formulas + given_data but omits substitution_steps (even
                # though the prompt forbids it), causing every attempt to be discarded.
                # NEW check: given_data must be real (non-fallback) AND at least one
                # formula must be real (not one of the three placeholder formula texts).
                _fb_given    = cls._FALLBACK["given_data"]
                _fb_fmlas    = {f["text"] for f in cls._FALLBACK.get("formulas", [])
                                if isinstance(f, dict) and f.get("text")}
                real_formulas = [
                    f for f in result.get("formulas", [])
                    if isinstance(f, dict) and f.get("text") and f["text"] not in _fb_fmlas
                ]
                has_real_data = (
                    bool(result.get("given_data")) and
                    result.get("given_data") != _fb_given and
                    bool(real_formulas)
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
            # Brief back-off between outer attempts so the Gemini rate-limit window
            # (60 s for most tiers) has a chance to clear before we try again.
            # Without this, all 3–4 outer attempts fire back-to-back and all hit
            # the same rate-limit wall, making the retries pointless.
            if attempt < MAX_ATTEMPTS and last_error is not None:
                _wait = attempt * 5   # 5 s, 10 s, 15 s … gentle linear back-off
                QAnimLogger.info("GeminiSolution", f"Waiting {_wait}s before outer attempt {attempt+1}…")
                _time.sleep(_wait)

        QAnimLogger.warn("GeminiSolution", f"All {MAX_ATTEMPTS} attempts failed ({last_error}) — using fallback")
        return dict(cls._FALLBACK)

    @classmethod
    def _solution_system_text(cls):
        return _SOLUTION_SYSTEM

    @classmethod
    def _call_gemini(cls, user_prompt: str, system_text: str, max_tokens: int = 4096) -> str:
        import time as _time
        MAX_RETRIES  = 4
        # Longer delays so rate-limit windows (typically 60s) can clear.
        # The old [4, 8, 15] total was only 27s — not long enough to ride out
        # a 429 / 503 between the 1st and 2nd question pipeline run.
        RETRY_DELAYS = [10, 25, 50, 90]

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
                # 429 = rate limit. 503/UNAVAILABLE/"high demand"/"overloaded"
                # = the model itself is transiently overloaded on Google's
                # side. Both are self-resolving if you wait and retry — but
                # only 429 was being retried before, so every 503 (which
                # Gemini returns fairly often at peak load) went straight to
                # "Animation Could Not Render" on attempt 1 with no retry at
                # all. Treat both as retryable.
                is_retryable = (
                    "429" in err_str or "TooManyRequests" in err_str or "Resource has been exhausted" in err_str
                    or "503" in err_str or "UNAVAILABLE" in err_str or "overloaded" in err_str.lower()
                    or "high demand" in err_str.lower()
                )
                if is_retryable and attempt < MAX_RETRIES:
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

        # 3. Try to find the outermost { ... } block via balanced-brace scan
        start = raw.find('{')
        if start == -1:
            return _sanitize_json_str(raw)  # No JSON object found — let json.loads raise

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
        return _sanitize_json_str(raw[start:])

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
            formula_why  = str(data.get("formula_why",  "") or "")

            variable_meanings = data.get("variable_meanings", [])
            if not isinstance(variable_meanings, list):
                variable_meanings = []
            norm_var_meanings = []
            for v in variable_meanings:
                if isinstance(v, dict):
                    norm_var_meanings.append({
                        "symbol":  str(v.get("symbol",  "") or ""),
                        "meaning": str(v.get("meaning", "") or ""),
                        "unit":    str(v.get("unit",    "") or ""),
                        "value":   str(v.get("value",   "") or ""),
                    })

            substitution_steps = data.get("substitution_steps", [])
            if not isinstance(substitution_steps, list):
                substitution_steps = []
            norm_subs = []
            for s in substitution_steps:
                if isinstance(s, dict):
                    norm_subs.append({
                        "title":       str(s.get("title", "") or ""),
                        "expr":        str(s.get("expr", "") or ""),
                        "description": str(s.get("description", "") or s.get("desc", "") or ""),
                    })
                else:
                    norm_subs.append({"title": "Calculation", "expr": str(s), "description": ""})

            final_answer      = str(data.get("final_answer", "") or "")
            final_answer_unit = str(data.get("final_answer_unit", "") or "")
            key_insight       = str(data.get("key_insight",  "") or "")
            real_world_note   = str(data.get("real_world_note", "") or "")

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
                "formula_why":        formula_why,
                "variable_meanings":  norm_var_meanings,
                "substitution_steps": norm_subs,
                "steps":              steps,
                "final_answer":       final_answer,
                "final_answer_unit":  final_answer_unit,
                "key_insight":        key_insight,
                "real_world_note":    real_world_note,
                "raw":                raw,
            }
        except Exception as e:
            QAnimLogger.warn("GeminiSolution", f"JSON parse failed: {e}")
            return dict(cls._FALLBACK)

    @classmethod
    async def generate_async(cls, question: str) -> dict:
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, cls.generate, question),
                timeout=STAGE_TIMEOUT_SMALL,
            )
        except asyncio.TimeoutError:
            QAnimLogger.error("GeminiSolution", f"Stage exceeded {STAGE_TIMEOUT_SMALL}s — using fallback solution")
            return dict(cls._FALLBACK)


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
      <div class="ab-alldone-sub">Great work! Continue the animation to review the <strong>Main Formula</strong> and <strong>Solution</strong> walkthrough.</div>
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
#btn-prev.qanim-prev-btn {
  background: #ffffff;
  color: #64748b;
  border: 1.5px solid #cbd5e1;
  padding: 11px 20px;
  border-radius: 10px;
  font-size: 13.5px;
  font-weight: 700;
  font-family: inherit;
  cursor: pointer;
  transition: background .2s ease, color .2s ease, border-color .2s ease,
              transform .18s cubic-bezier(0.34,1.56,0.64,1), box-shadow .2s ease;
  margin-right: auto;
  box-shadow: 0 1px 3px rgba(15,23,42,0.06);
}
#btn-prev.qanim-prev-btn:hover:not(:disabled) {
  background: #f8fafc;
  color: #1e293b;
  border-color: #94a3b8;
  box-shadow: 0 2px 8px rgba(15,23,42,0.10);
  transform: translateY(-1px);
}
#btn-prev.qanim-prev-btn:active:not(:disabled) { transform: translateY(0); }
#btn-prev.qanim-prev-btn:disabled { opacity:.38; cursor:not-allowed; box-shadow:none; }
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

  function _resumeRAFIfNeeded(){
    // Mirrors Scene 6/7's resume helper: prefer the required naming
    // contract (window.qanimRafId / window.qanimStartRAF), fall back to
    // older guessed names for animations generated before that contract.
    if(typeof window.qanimStartRAF==='function'){ window.qanimStartRAF(); return; }
    if(typeof window.startRAF==='function'){ window.startRAF(); return; }
    if(typeof window.animate==='function'){ requestAnimationFrame(window.animate); return; }
  }

  // Exposed so the button's onclick (and anyone else) can call it directly.
  window.prevStep=function(){
    if(typeof window.currentStep!=='number')return;
    if(window.currentStep<=0)return;
    window.currentStep--;
    if(typeof window.applyStep==='function')window.applyStep(window.currentStep);
    // Stepping back off a frozen/final step must resume any paused motion
    // loop — applyStep() alone repositions moving parts for THIS step
    // instantly, but if the loop itself was left cancelled (e.g. stepping
    // back from a "freezing" final step), it must be restarted here too,
    // or continuous effects (rotation, flow, etc.) will stay dead for the
    // rest of the session even though this step's static frame is correct.
    _resumeRAFIfNeeded();
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
#  MODULE 12.5 — Scene 6: "The Big Idea" injector
#  Appends a new step to stepsData that shows the main formula/principle
#  with variable labels and a short info card. Builds an SVG overlay panel
#  injected into the existing stage SVG, and patches the JS stepsData array.
# ===========================================================================

_SCENE6_CSS = """
<style id="qanim-scene6-styles">
/* ── Shared modal backdrop for Scene 6 / Scene 7 ── */
#qanim-scene-modal-backdrop {
  display: none;
  position: fixed;
  inset: 0;
  z-index: 7400;
  background: rgba(15,23,42,.50);
  backdrop-filter: blur(6px);
  opacity: 0;
  transition: opacity .25s ease;
}
#qanim-scene-modal-backdrop.qanim-scene-visible {
  display: block !important;
  opacity: 1;
}
/* ── Scene 6: Main Formula — centered modal card ── */
#qanim-scene6-overlay {
  display: none;
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%,-50%) scale(.95);
  z-index: 7500;
  width: min(860px, 96vw);
  max-height: 92vh;
  overflow-y: auto;
  box-sizing: border-box;
  opacity: 0;
  pointer-events: none;
  transition: opacity .3s ease, transform .3s cubic-bezier(.34,1.56,.64,1);
}
#qanim-scene6-overlay.qanim-scene-visible {
  display: block !important;
  opacity: 1;
  pointer-events: auto;
  transform: translate(-50%,-50%) scale(1);
}
/* ── Main card ── */
.s6-card {
  background: #fff;
  border-radius: 20px;
  box-shadow: 0 8px 48px rgba(8,145,178,.14), 0 2px 8px rgba(0,0,0,.08);
  border: 1px solid #dde8f8;
  overflow: hidden;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
}
/* ── Title bar ── */
.s6-title-bar {
  text-align: center;
  padding: 22px 28px 18px;
  background: #fff;
  border-bottom: 1px solid #e8eef8;
}
.s6-title-bar h2 {
  font-size: 20px;
  font-weight: 900;
  color: #0f172a;
  letter-spacing: -0.3px;
  margin-bottom: 0;
}
/* ── Body ── */
.s6-body {
  padding: 28px 32px 24px;
  background: linear-gradient(160deg, #eef2f9 0%, #e8f0fe 50%, #eff6ff 100%);
}
/* ── Large formula box (top, centered, blue border) ── */
.s6-formula-box {
  background: #fff;
  border: 2.5px solid #3b82f6;
  border-radius: 18px;
  padding: 20px 32px 16px;
  text-align: center;
  margin-bottom: 10px;
  position: relative;
}
.s6-formula-main {
  font-family: 'Courier New', Courier, monospace;
  font-size: 28px;
  font-weight: 900;
  color: #1d4ed8;
  letter-spacing: 1px;
  line-height: 1.4;
  word-break: break-word;
  opacity: 0;
  transform: translateY(8px);
  transition: opacity .5s ease, transform .5s ease;
}
.s6-formula-main.s6-shown { opacity: 1; transform: translateY(0); }
.s6-formula-sublabel {
  font-size: 11px;
  font-weight: 700;
  color: #6366f1;
  letter-spacing: 0.3px;
  margin-top: 8px;
  opacity: 0;
  transition: opacity .4s ease .2s;
}
.s6-formula-sublabel.s6-shown { opacity: 1; }
/* ── Arrows section: formula → variable boxes ── */
.s6-vars-row {
  display: flex;
  align-items: flex-start;
  justify-content: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 24px;
}
/* Each variable box */
.s6-var-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0;
  min-width: 120px;
  max-width: 160px;
  opacity: 0;
  transform: translateY(16px);
  transition: opacity .45s cubic-bezier(0.4,0,0.2,1), transform .4s cubic-bezier(0.34,1.56,0.64,1);
}
.s6-var-box.s6-shown { opacity: 1; transform: translateY(0); }
/* Arrow from formula down to box */
.s6-var-arrow {
  width: 2px;
  height: 24px;
  position: relative;
  margin-bottom: 0;
}
.s6-var-arrow::before {
  content: '';
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  top: 0;
  width: 2px;
  height: 18px;
  border-radius: 1px;
}
.s6-var-arrow::after {
  content: '';
  position: absolute;
  bottom: 0;
  left: 50%;
  transform: translateX(-50%);
  border-left: 6px solid transparent;
  border-right: 6px solid transparent;
}
/* Color themes for variable boxes */
.s6-var-box.s6v-red   .s6-var-inner { border-color:#f43f5e; background:#fff1f2; }
.s6-var-box.s6v-red   .s6-var-sym   { color:#be123c; }
.s6-var-box.s6v-red   .s6-var-arrow::before { background:#f43f5e; }
.s6-var-box.s6v-red   .s6-var-arrow::after  { border-top:8px solid #f43f5e; }
.s6-var-box.s6v-orange .s6-var-inner { border-color:#f59e0b; background:#fff7ed; }
.s6-var-box.s6v-orange .s6-var-sym   { color:#d97706; }
.s6-var-box.s6v-orange .s6-var-arrow::before { background:#f59e0b; }
.s6-var-box.s6v-orange .s6-var-arrow::after  { border-top:8px solid #f59e0b; }
.s6-var-box.s6v-blue  .s6-var-inner { border-color:#3b82f6; background:#eff6ff; }
.s6-var-box.s6v-blue  .s6-var-sym   { color:#1d4ed8; }
.s6-var-box.s6v-blue  .s6-var-arrow::before { background:#3b82f6; }
.s6-var-box.s6v-blue  .s6-var-arrow::after  { border-top:8px solid #3b82f6; }
.s6-var-box.s6v-green .s6-var-inner { border-color:#22c55e; background:#f0fdf4; }
.s6-var-box.s6v-green .s6-var-sym   { color:#15803d; }
.s6-var-box.s6v-green .s6-var-arrow::before { background:#22c55e; }
.s6-var-box.s6v-green .s6-var-arrow::after  { border-top:8px solid #22c55e; }
.s6-var-box.s6v-purple .s6-var-inner { border-color:#a855f7; background:#faf5ff; }
.s6-var-box.s6v-purple .s6-var-sym   { color:#7c3aed; }
.s6-var-box.s6v-purple .s6-var-arrow::before { background:#a855f7; }
.s6-var-box.s6v-purple .s6-var-arrow::after  { border-top:8px solid #a855f7; }
.s6-var-box.s6v-teal  .s6-var-inner { border-color:#14b8a6; background:#f0fdfa; }
.s6-var-box.s6v-teal  .s6-var-sym   { color:#0f766e; }
.s6-var-box.s6v-teal  .s6-var-arrow::before { background:#14b8a6; }
.s6-var-box.s6v-teal  .s6-var-arrow::after  { border-top:8px solid #14b8a6; }
/* Inner box container */
.s6-var-inner {
  border: 2px solid;
  border-radius: 14px;
  padding: 14px 16px 12px;
  text-align: center;
  width: 100%;
  box-sizing: border-box;
}
.s6-var-sym {
  font-family: 'Courier New', Courier, monospace;
  font-size: 22px;
  font-weight: 900;
  line-height: 1;
  display: block;
  margin-bottom: 5px;
}
.s6-var-name {
  font-size: 11.5px;
  font-weight: 700;
  color: #475569;
  line-height: 1.35;
  display: block;
}
.s6-var-val {
  font-size: 10.5px;
  font-weight: 600;
  color: #94a3b8;
  margin-top: 3px;
  display: block;
}
/* ── Fourth-power note bar (like the ⚡ note in Image 1) ── */
.s6-note-bar {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  margin-top: 22px;
  padding: 12px 20px;
  background: #fff;
  border-radius: 12px;
  border: 1.5px solid #fde68a;
  opacity: 0;
  transform: translateY(8px);
  transition: opacity .45s ease, transform .45s ease;
}
.s6-note-bar.s6-shown { opacity: 1; transform: translateY(0); }
.s6-note-icon { font-size: 16px; flex-shrink: 0; }
.s6-note-text {
  font-size: 13px;
  font-weight: 700;
  color: #92400e;
}
/* ── Navigation row ── */
.s6-nav-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  padding: 16px 32px 22px;
  border-top: 1px solid #e8eef8;
  background: #fff;
}
/* ── Phase progress label ── */
.s6-phase-progress {
  font-size: 10.5px;
  font-weight: 800;
  letter-spacing: 1.2px;
  text-transform: uppercase;
  color: #0891b2;
  text-align: center;
  margin-bottom: 4px;
  min-height: 14px;
}
.s6-phase-caption {
  font-size: 13px;
  font-weight: 600;
  color: #334155;
  text-align: center;
  margin-bottom: 20px;
  line-height: 1.5;
  min-height: 18px;
  transition: opacity .3s ease;
}
/* highlight active var box */
.s6-var-box.s6-active .s6-var-inner {
  box-shadow: 0 0 0 4px rgba(8,145,178,.20), 0 4px 16px rgba(8,145,178,.22);
  transform: scale(1.05);
  transition: transform .3s cubic-bezier(.34,1.56,.64,1), box-shadow .3s ease;
}
</style>
"""

_SCENE6_DOM_TEMPLATE = """\
<div id="qanim-scene-modal-backdrop"></div>
<div id="qanim-scene6-overlay">
  <div class="s6-card">
    <div class="s6-title-bar">
      <h2 id="s6-card-title">Step 7 &mdash; Governing Formula</h2>
    </div>
    <div class="s6-body">
      <div class="s6-phase-progress" id="s6-phase-progress">Step 1 of 3 &mdash; The Formula</div>
      <div class="s6-phase-caption" id="s6-phase-caption">This is the governing formula for this problem.</div>
      <!-- Large formula box at top (like image 1) -->
      <div class="s6-formula-box">
        <div class="s6-formula-main" id="s6-formula-text">{formula}</div>
        <div class="s6-formula-sublabel" id="s6-formula-sublabel">{formula_label}</div>
      </div>
      <!-- Variable boxes with arrows (revealed step by step) -->
      <div class="s6-vars-row" id="s6-vars-row">{var_boxes_html}</div>
      <!-- Key insight note bar (like ⚡ note in image 1) -->
      <div class="s6-note-bar" id="s6-note-bar">
        <span class="s6-note-icon">&#x26A1;</span>
        <span class="s6-note-text" id="s6-note-text">{insight}</span>
      </div>
    </div>
    <div class="s6-nav-row">
      <button class="btn-secondary" onclick="qanim_goToPrevScene()" id="s6-prev-btn">&#x2190; Back to Step 6</button>
      <button class="btn-primary" onclick="qanim_s6Advance()" id="s6-next-btn">Next &#x25B6;</button>
    </div>
  </div>
</div>"""

_SCENE6_JS = r"""
<script id="qanim-js-scene6">
(function initScene6(){
  'use strict';
  if(window.__qanimScene6Init)return; window.__qanimScene6Init=true;

  /* ── helpers ── */
  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  function _varBoxes(){return document.querySelectorAll('#s6-vars-row .s6-var-box');}

  /* s6Phase:
       -1 = not started (overlay hidden)
        0 = formula revealed (large box shown, vars hidden)
        1..N = variable box i-1 revealed (step-by-step)
        N+1 = note bar / insight revealed — final state */
  var s6Phase = -1;
  var s6AutoAdvanceScheduled = false;
  var s6AutoAdvanceTimer = null;
  var S6_TOTAL_VAR_PHASES = 0; // set on first render

  function s6Render(){
    var boxes = _varBoxes();
    var n = boxes.length;
    S6_TOTAL_VAR_PHASES = n;
    var totalPhases = n + 1; // 0=formula, 1..n=vars, n+1=note

    var formulaEl  = _el('s6-formula-text');
    var sublabelEl = _el('s6-formula-sublabel');
    var captionEl  = _el('s6-phase-caption');
    var progressEl = _el('s6-phase-progress');
    var noteEl     = _el('s6-note-bar');
    var nextBtn    = _el('s6-next-btn');

    /* Phase 0+: reveal the formula box */
    if(formulaEl) formulaEl.classList.add('s6-shown');
    if(sublabelEl) sublabelEl.classList.add('s6-shown');

    /* Reveal variable boxes step by step */
    for(var i=0;i<n;i++){
      var b = boxes[i];
      b.classList.remove('s6-active');
      if(s6Phase >= i+1){
        b.classList.add('s6-shown');
        if(s6Phase === i+1) b.classList.add('s6-active');
      }
    }

    /* Note bar appears on final phase */
    var showNote = s6Phase >= n+1;
    if(noteEl) noteEl.classList.toggle('s6-shown', showNote);

    /* Auto-advance to Scene 7 once note bar appears */
    if(showNote && !s6AutoAdvanceScheduled){
      s6AutoAdvanceScheduled = true;
      s6AutoAdvanceTimer = setTimeout(function(){
        var ov = _el('qanim-scene6-overlay');
        if(ov && ov.classList.contains('qanim-scene-visible')){
          window.qanim_goToScene7();
        }
      }, 2800);
    }

    /* Progress label */
    if(progressEl){
      if(s6Phase <= 0) progressEl.innerText = 'Step 1 of '+(n+2)+' \u2014 The Formula';
      else if(s6Phase <= n) progressEl.innerText = 'Step '+(s6Phase+1)+' of '+(n+2)+' \u2014 Variable '+(s6Phase)+' of '+n;
      else progressEl.innerText = 'Step '+(n+2)+' of '+(n+2)+' \u2014 Key Insight';
    }

    /* Caption text */
    if(captionEl){
      var cap = 'This is the governing formula. Click \u201CNext\u201D to explore each variable.';
      if(s6Phase >= 1 && s6Phase <= n){
        var b2 = boxes[s6Phase-1];
        var symEl  = b2 ? b2.querySelector('.s6-var-sym')  : null;
        var nameEl = b2 ? b2.querySelector('.s6-var-name') : null;
        var valEl  = b2 ? b2.querySelector('.s6-var-val')  : null;
        var symTxt  = symEl  ? symEl.innerText  : '';
        var nameTxt = nameEl ? nameEl.innerText : 'a key variable';
        var valTxt  = valEl  ? valEl.innerText  : '';
        cap = symTxt + ' \u2014 ' + nameTxt + (valTxt ? (' (' + valTxt + ')') : '') + '.';
      } else if(s6Phase >= n+1){
        cap = 'All variables identified. See the key insight below, then continue to the step-by-step solution.';
      }
      captionEl.innerText = cap;
    }

    /* Next button label */
    if(nextBtn){
      if(s6Phase >= n+1){
        nextBtn.innerText = 'Continue to Solution \u25B6';
        nextBtn.style.background = 'linear-gradient(135deg,#4338ca,#7c3aed)';
        nextBtn.onclick = function(){ window.qanim_goToScene7(); };
      } else {
        nextBtn.innerText = 'Next \u25B6';
        nextBtn.style.background = '';
        nextBtn.onclick = function(){ window.qanim_s6Advance(); };
      }
    }
  }

  /* Reveal the next variable box (called by the Next button) */
  window.qanim_s6Advance = function(){
    var n = _varBoxes().length;
    var max = n + 1;
    if(s6Phase < max) s6Phase++;
    s6Render();
  };

  /* ── RAF cancel/resume helpers ──
     Prefer the required naming contract (window.qanimRafId /
     window.qanimStartRAF) that the animation builder prompt now mandates;
     fall back to the older guessed names for any animation generated
     before that contract existed, so previously-generated files still get
     best-effort behavior instead of a hard failure. */
  function _qanimCancelRAF(){
    if(typeof window.qanimRafId!=='undefined'&&window.qanimRafId){
      cancelAnimationFrame(window.qanimRafId);window.qanimRafId=null;
    }
    if(typeof window.rafId!=='undefined'&&window.rafId){
      cancelAnimationFrame(window.rafId);window.rafId=null;
    }
  }
  function _qanimResumeRAF(){
    if(typeof window.qanimStartRAF==='function'){ window.qanimStartRAF(); return; }
    if(typeof window.startRAF==='function'){ window.startRAF(); return; }
    if(typeof window.animate==='function'){ requestAnimationFrame(window.animate); return; }
  }

  /* ── show / hide ── */
  window.qanim_showScene6 = function(){
    var ov=_el('qanim-scene6-overlay');
    if(ov){ov.style.display='block';ov.classList.add('qanim-scene-visible');}
    var ov7=_el('qanim-scene7-overlay');
    if(ov7) ov7.classList.remove('qanim-scene-visible');
    var ov9=_el('qanim-scene9-overlay');
    if(ov9) ov9.classList.remove('qanim-scene-visible');
    var bd=_el('qanim-scene-modal-backdrop');
    if(bd){bd.style.display='block';bd.classList.add('qanim-scene-visible');}
    /* freeze the SVG animation if running */
    _qanimCancelRAF();
    /* update the dot bar: dots 0-5 done, dot 6 (Step 7) active */
    var dots=document.querySelectorAll('.step-dot');
    for(var i=0;i<dots.length;i++){
      dots[i].className='step-dot';
      if(i<6) dots[i].className+=' done';
      if(i===6) dots[i].className+=' active';
    }
    var sl=_el('step-label'); if(sl) sl.innerHTML='Step 7 of 9';
    var sb=_el('step-bar'); if(sb) sb.style.width='77.78%';
    /* start the teaching sequence from the beginning every time we arrive */
    s6Phase = 0;
    s6AutoAdvanceScheduled = false;
    if(s6AutoAdvanceTimer){ clearTimeout(s6AutoAdvanceTimer); s6AutoAdvanceTimer=null; }
    s6Render();
  };

  window.qanim_goToPrevScene = function(){
    /* hide both extra scenes, resume the SVG animation at last step */
    var ov6=_el('qanim-scene6-overlay');
    if(ov6) ov6.classList.remove('qanim-scene-visible');
    var ov7=_el('qanim-scene7-overlay');
    if(ov7) ov7.classList.remove('qanim-scene-visible');
    var bd=_el('qanim-scene-modal-backdrop');
    if(bd) bd.classList.remove('qanim-scene-visible');
    if(s6AutoAdvanceTimer){ clearTimeout(s6AutoAdvanceTimer); s6AutoAdvanceTimer=null; }
    _restoreConceptStage();
    /* go to the last SVG step */
    if(typeof window.applyStep==='function'&&typeof window.stepsData!=='undefined'){
      var last=window.stepsData.length-1;
      window.currentStep=last;
      window.applyStep(last);
    }
    /* resume RAF if there was one */
    _qanimResumeRAF();
  };

  window.qanim_goToScene7 = function(){
    var ov6=_el('qanim-scene6-overlay');
    if(ov6) ov6.classList.remove('qanim-scene-visible');
    if(s6AutoAdvanceTimer){ clearTimeout(s6AutoAdvanceTimer); s6AutoAdvanceTimer=null; }
    if(typeof window.qanim_showScene7==='function') window.qanim_showScene7();
  };

  function _syncDots(idx){
    var dots=document.querySelectorAll('.step-dot');
    for(var i=0;i<dots.length;i++){
      dots[i].classList.remove('active','done');
      if(i<idx) dots[i].classList.add('done');
      if(i===idx) dots[i].classList.add('active');
    }
    var lbl=_el('step-label');
    if(lbl) lbl.innerText='Step 6: Main Formula';
    var bar=_el('step-bar');
    if(bar) bar.style.width=Math.round((idx+1)/Math.max(dots.length,1)*100)+'%';
  }

  /* Pause briefly, then smoothly fade the concept-animation stage to black
     before Main Formula appears — the "teacher steps back from the board"
     beat between Concept Explanation and Main Formula. */
  function _fadeOutConceptStage(onDone){
    var stage = document.querySelector('.svg-container');
    if(!stage){ onDone(); return; }
    setTimeout(function(){
      stage.style.transition = 'opacity .45s ease';
      stage.style.opacity = '0';
      setTimeout(onDone, 460);
    }, 650); /* brief pause before the fade starts, per spec (~0.5-1s) */
  }
  function _restoreConceptStage(){
    var stage = document.querySelector('.svg-container');
    if(stage){ stage.style.opacity = '1'; }
  }

  /* ── wire resetAnim to hide overlays when restarting ── */
  _onReady(function(){
    /* Intercept resetAnim so it hides the overlay scenes when restarting */
    var _origReset=window.resetAnim;
    window.resetAnim=function(){
      var ov6=_el('qanim-scene6-overlay');
      if(ov6) ov6.classList.remove('qanim-scene-visible');
      var ov7=_el('qanim-scene7-overlay');
      if(ov7) ov7.classList.remove('qanim-scene-visible');
      var bd=_el('qanim-scene-modal-backdrop');
      if(bd) bd.classList.remove('qanim-scene-visible');
      s6Phase = -1;
      s6AutoAdvanceScheduled = false;
      if(s6AutoAdvanceTimer){ clearTimeout(s6AutoAdvanceTimer); s6AutoAdvanceTimer=null; }
      _restoreConceptStage();
      if(typeof _origReset==='function') _origReset();
    };
  });
})();
</script>
"""


_S6_VAR_THEMES = [
    "s6v-red",
    "s6v-orange",
    "s6v-blue",
    "s6v-green",
    "s6v-purple",
    "s6v-teal",
]

def _build_s6_var_boxes_html(gemini_sol, scene_script):
    """
    Build Image-1-style variable boxes with arrows for Scene 6.
    Returns (var_boxes_html, formula_label, insight_text) tuple.
    Each box: colored border box with symbol (large), name, and value underneath.
    Boxes start hidden and are revealed one-by-one via JS.
    """
    # ── 1. Collect variable entries ──
    var_entries = []

    # Build a symbol -> meaning lookup from variable_meanings
    meaning_lookup = {}
    for v in (gemini_sol.get("variable_meanings") or []):
        if isinstance(v, dict) and v.get("symbol"):
            key = str(v["symbol"]).strip().lower()
            mean = str(v.get("meaning", "") or "")
            unit = str(v.get("unit", "") or "")
            meaning_lookup[key] = (mean + (" (" + unit + ")" if unit else "")).strip()

    # Prefer structured given_data from solution generator
    given_raw = gemini_sol.get("given_data") or []
    for g in given_raw[:6]:
        g_str = str(g).strip()
        if "=" in g_str:
            parts = g_str.split("=", 1)
            sym     = parts[0].strip()[:14]
            val_raw = parts[1].strip()[:35]
            meaning = meaning_lookup.get(sym.lower(), "")
            var_entries.append({"sym": sym, "val": val_raw, "name": meaning})
        else:
            var_entries.append({"sym": g_str[:10], "val": "", "name": ""})

    # Enrich names from meaning_lookup for entries that don't have one
    for e in var_entries:
        if not e.get("name"):
            e["name"] = meaning_lookup.get(e["sym"].lower(), "")

    # Fallback: try badges from last step
    if not var_entries:
        steps = scene_script.get("steps") or []
        for step in reversed(steps):
            badges = step.get("badges") or []
            for b in badges[:6]:
                text = b.get("text","") if isinstance(b,dict) else str(b)
                if "=" in text:
                    parts = text.split("=",1)
                    sym = parts[0].strip()[:14]
                    val = parts[1].strip()[:35]
                    if not any(e["sym"] == sym for e in var_entries):
                        var_entries.append({"sym": sym, "val": val, "name": meaning_lookup.get(sym.lower(),"")})
            if var_entries:
                break

    # Final fallback
    if not var_entries:
        var_entries = [
            {"sym": "Q", "val": "?", "name": "Quantity to find"},
            {"sym": "X", "val": "given", "name": "Given variable"},
        ]

    # Cap at 5
    var_entries = var_entries[:5]

    # ── 2. Build formula label ──
    formula_raw = ""
    formulas = gemini_sol.get("formulas") or []
    for f in formulas[:1]:
        formula_raw = f.get("text","") if isinstance(f,dict) else str(f)
    if not formula_raw:
        sol_steps = gemini_sol.get("steps") or []
        for s in sol_steps:
            s_str = str(s)
            if "=" in s_str and len(s_str) < 120:
                formula_raw = s_str
                break

    # Derive a human-readable formula label (subtitle under formula)
    formula_label = scene_script.get("title", "Governing Equation") or "Governing Equation"
    if "formula_label" in (gemini_sol or {}):
        formula_label = str(gemini_sol["formula_label"])[:80]

    # ── 3. Build var box HTML ──
    box_parts = []
    for i, entry in enumerate(var_entries):
        theme  = _S6_VAR_THEMES[i % len(_S6_VAR_THEMES)]
        sym_e  = html_module.escape(str(entry.get("sym","?"))[:14])
        name_e = html_module.escape(str(entry.get("name",""))[:55])
        val_e  = html_module.escape(str(entry.get("val",""))[:35])

        # Arrow div (colored per theme)
        arrow_html = '<div class="s6-var-arrow"></div>'

        # Inner box content
        inner_html = (
            '<div class="s6-var-inner">'
            '<span class="s6-var-sym">' + sym_e + '</span>'
            + ('<span class="s6-var-name">' + name_e + '</span>' if name_e else '')
            + ('<span class="s6-var-val">' + val_e + '</span>' if val_e else '')
            + '</div>'
        )

        box_html = (
            '<div class="s6-var-box ' + theme + '" data-idx="' + str(i) + '">'
            + arrow_html + inner_html +
            '</div>'
        )
        box_parts.append(box_html)

    var_boxes_html = "\n".join(box_parts) if box_parts else ""
    return var_boxes_html, html_module.escape(str(formula_label)[:80])


def inject_scene6_big_idea(html, gemini_sol, scene_script):
    """
    Inject Scene 6 ("Main Formula") as a standalone overlay panel that
    appears AFTER the last SVG animation step when the user clicks "View Formula".

    Layout matches Image 1:
      - Title bar at top
      - Large formula in bordered box
      - Colored variable boxes with arrows, revealed one-by-one
      - Key insight note bar at bottom

    This function:
      1. Injects CSS into <head>
      2. Injects the DOM panel right after <body>
      3. Injects the JS module before </body>
    """
    # Detect fallback — if solution generation failed, show a clear retry message
    # inside the modal itself (not just the sticky banner in the body).
    _is_fallback = bool(gemini_sol.get("_used_fallback"))

    # Extract formula — try structured keys first, then fallback
    formula_raw = ""
    formulas = gemini_sol.get("formulas") or []
    _fb_fmla_texts = {f["text"] for f in GeminiSolutionGenerator._FALLBACK.get("formulas", [])
                      if isinstance(f, dict) and f.get("text")}
    for f in formulas[:1]:
        candidate = f.get("text", "") if isinstance(f, dict) else str(f)
        if candidate and candidate not in _fb_fmla_texts:
            formula_raw = candidate
            break
    if not formula_raw:
        sol_steps = gemini_sol.get("steps") or []
        for s in sol_steps:
            s_str = str(s)
            if "=" in s_str and len(s_str) < 120:
                formula_raw = s_str
                break
    if not formula_raw or _is_fallback:
        # Show a clear regeneration message instead of the misleading placeholder
        formula_raw = (
            "⚠ Formula could not be generated — please regenerate this animation"
        ) if _is_fallback else (scene_script.get("key_insight") or "See formula below")

    if _is_fallback:
        insight = (
            "The Gemini API did not return a solution for this question (rate limit or timeout). "
            "Click 'Restart' and regenerate the animation, or wait 60 s and try again."
        )
    else:
        insight = (
            gemini_sol.get("formula_why")
            or scene_script.get("key_insight")
            or gemini_sol.get("key_insight")
            or "This formula is the governing principle. Identify what is given, plug in the values, and compute the result systematically."
        )
        if "correct formula" not in str(insight).lower():
            insight = str(insight).rstrip(". ") + ". This is the correct formula for solving this problem."

    # Build variable boxes (Image 1 style: colored boxes with arrows)
    var_boxes_html, formula_label = _build_s6_var_boxes_html(gemini_sol, scene_script)

    # Card title = topic/title from scene_script
    card_title = scene_script.get("title", "Main Formula") or "Main Formula"

    dom = _SCENE6_DOM_TEMPLATE.format(
        formula=html_module.escape(str(formula_raw)[:300]),
        formula_label=formula_label,
        var_boxes_html=var_boxes_html,
        insight=html_module.escape(str(insight)[:300]),
    )

    # 1. CSS
    try:
        if "</head>" in html:
            html = html.replace("</head>", _SCENE6_CSS + "\n</head>", 1)
    except Exception as e:
        QAnimLogger.warn("Scene6Injector", f"CSS failed: {e}")

    # 2. DOM — insert right after <body ...>
    try:
        body_m = re.search(r"<body[^>]*>", html, re.IGNORECASE)
        if body_m:
            ins = body_m.end()
            html = html[:ins] + "\n" + dom + html[ins:]
    except Exception as e:
        QAnimLogger.warn("Scene6Injector", f"DOM failed: {e}")

    # 3. JS
    try:
        if "</body>" in html:
            html = html.replace("</body>", _SCENE6_JS + "\n</body>", 1)
        else:
            html += "\n" + _SCENE6_JS
    except Exception as e:
        QAnimLogger.warn("Scene6Injector", f"JS failed: {e}")

    QAnimLogger.ok("Scene6Injector", "Scene 6 (Main Formula — Image 1 style) injected")
    return html


_SCENE6_AUTOTRIGGER_JS = """
<script id="qanim-js-scene6-autotrigger">
(function(){
  'use strict';
  if(window.__qanimScene6AutoTrigger)return;window.__qanimScene6AutoTrigger=true;
  /* Deterministic safety net: rather than trusting the freely-generated
     base animation's own nextStep()/applyStep() to remember to call
     window.qanim_showScene6() on the final step (Gemini does not always
     include this), watch the #btn-next button's own state instead. The
     base animation always disables it / relabels it once the last step
     is reached (that behaviour is required and validated separately),
     so this works regardless of what Gemini named its internal
     variables or how it structured its step logic.

     IMPORTANT: whether to trigger is decided from the ACTUAL DOM state
     every time (is the button showing "Finish", AND is neither overlay
     already open) — never from a one-shot flag that only resets on the
     Restart button. A one-shot flag breaks the very common flow of:
     reach the end → Scene 6 opens → Back to Animation → Previous Step →
     Next Step back to the end again — that flow never touches Restart,
     so a flag left "already shown" from the first time would silently
     refuse to reopen Scene 6 the second time, requiring a full page
     refresh to recover. Checking real DOM state instead makes this
     naturally correct no matter how many times the user goes back and
     forth. */
  function _tryTrigger(){
    var btn=document.getElementById('btn-next');
    if(!btn)return;
    var label=(btn.textContent||btn.innerText||'').trim().toLowerCase();
    var isFinished = btn.disabled || label.indexOf('finish')!==-1;
    if(!isFinished)return;
    var ov6=document.getElementById('qanim-scene6-overlay');
    var ov7=document.getElementById('qanim-scene7-overlay');
    var alreadyOpen=(ov6&&ov6.classList.contains('qanim-scene-visible'))||
                    (ov7&&ov7.classList.contains('qanim-scene-visible'));
    if(alreadyOpen)return; /* don't re-trigger the fade-out while one is already open */
    if(typeof window.qanim_showScene6==='function'){
      var svgCont=document.querySelector('.svg-container');
      var doShow=function(){ window.qanim_showScene6(); };
      if(svgCont){
        svgCont.style.transition='opacity .45s ease';
        svgCont.style.opacity='0';
        setTimeout(doShow,460);
      } else {
        setTimeout(doShow,120);
      }
    }
  }
  function _wireBtn(){
    var btn=document.getElementById('btn-next');
    if(!btn||btn.__qanimAutoWired)return;
    btn.__qanimAutoWired=true;
    btn.addEventListener('click',function(){ setTimeout(_tryTrigger,30); });
  }
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}
  _onReady(function(){
    _wireBtn();
  });
})();
</script>
"""


def inject_scene6_autotrigger(html):
    """
    Deterministic post-processing patch (Module 12.5b): guarantees Scene 6
    ("Main Formula") opens once the base animation reaches its final step,
    independent of whether Gemini's own generated nextStep() remembered to
    call window.qanim_showScene6() itself. Fixes the class of bug where the
    Main Formula / Solution walkthrough never appears because the LLM wrote
    a nextStep() that just disables the Next button on the last step.
    """
    try:
        if "</body>" in html:
            html = html.replace("</body>", _SCENE6_AUTOTRIGGER_JS + "\n</body>", 1)
        else:
            html += "\n" + _SCENE6_AUTOTRIGGER_JS
        QAnimLogger.ok("Scene6AutoTrigger", "Autotrigger patch injected")
    except Exception as e:
        QAnimLogger.warn("Scene6AutoTrigger", f"Injection failed: {e}")
    return html


# ===========================================================================
#  MODULE 12.6 — Scene 7: "How We Solve It — Step by Step" injector
#  Appends a new overlay panel showing the solution method in 5–10
#  numbered steps with descriptions and equations. Does NOT reveal the
#  final numeric answer (that stays in the Answer Box panel).
# ===========================================================================

_SCENE7_CSS = """
<style id="qanim-scene7-styles">
/* ── Scene 7: Complete System — Solution Summary — centered modal card ── */
#qanim-scene7-overlay {
  display: none;
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%,-50%) scale(.95);
  z-index: 7500;
  width: min(900px, 96vw);
  max-height: 92vh;
  overflow-y: auto;
  box-sizing: border-box;
  opacity: 0;
  pointer-events: none;
  transition: opacity .3s ease, transform .3s cubic-bezier(.34,1.56,.64,1);
}
#qanim-scene7-overlay.qanim-scene-visible {
  display: block !important;
  opacity: 1;
  pointer-events: auto;
  transform: translate(-50%,-50%) scale(1);
}
/* ── Card — matches Image 2 white card style ── */
.s7-card {
  background: #fff;
  border-radius: 20px;
  box-shadow: 0 8px 48px rgba(37,99,235,.12), 0 2px 8px rgba(0,0,0,.07);
  border: 1px solid #e8eef8;
  overflow: hidden;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
}
/* ── Title bar ── */
.s7-title-bar {
  text-align: center;
  padding: 20px 28px 16px;
  border-bottom: 1px solid #e8eef8;
  background: #fff;
}
.s7-title-bar h2 {
  font-size: 20px;
  font-weight: 900;
  color: #0f172a;
  letter-spacing: -0.3px;
}
/* ── Two-column body ── */
.s7-body-cols {
  display: flex;
  align-items: flex-start;
  gap: 0;
  min-height: 320px;
}
/* LEFT column — light blue bg like Image 2 left panel */
.s7-left-col {
  width: 44%;
  min-width: 200px;
  border-right: 1.5px solid #e8eef8;
  padding: 22px 20px 22px 26px;
  background: linear-gradient(180deg, #eff6ff 0%, #dbeafe 100%);
  display: flex;
  flex-direction: column;
  gap: 0;
  align-self: stretch;
}
/* Physical system label at top of left col */
.s7-system-label {
  font-size: 10.5px;
  font-weight: 800;
  color: #1d4ed8;
  text-transform: uppercase;
  letter-spacing: 1px;
  margin-bottom: 10px;
}
/* System visual placeholder (gradient box like Image 2 plate) */
.s7-system-visual {
  background: linear-gradient(135deg, #bfdbfe 0%, #93c5fd 100%);
  border-radius: 12px;
  padding: 16px 14px 14px;
  margin-bottom: 16px;
  text-align: center;
  position: relative;
  overflow: hidden;
}
.s7-system-visual-title {
  font-size: 13px;
  font-weight: 800;
  color: #1e3a5f;
  margin-bottom: 6px;
}
.s7-system-arrows {
  display: flex;
  justify-content: center;
  gap: 10px;
  margin: 8px 0;
  font-size: 20px;
  color: #d97706;
}
.s7-system-label2 {
  font-size: 10px;
  font-weight: 600;
  color: #1e40af;
  margin-top: 4px;
}
/* RIGHT column — white, with given params + solution approach */
.s7-right-col {
  flex: 1;
  padding: 22px 26px 20px 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
/* Given Parameters section (matches Image 2 right panel header) */
.s7-given-section-title {
  font-size: 13px;
  font-weight: 900;
  color: #1d4ed8;
  margin-bottom: 8px;
  letter-spacing: -0.1px;
}
.s7-given-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-bottom: 14px;
}
.s7-given-item {
  font-size: 12.5px;
  color: #334155;
  line-height: 1.5;
  display: flex;
  align-items: flex-start;
  gap: 7px;
}
.s7-given-item::before {
  content: '•';
  color: #3b82f6;
  font-weight: 900;
  flex-shrink: 0;
  margin-top: 1px;
}
.s7-given-item strong { font-weight: 700; color: #1e293b; }
/* Solution Approach section (matches Image 2) */
.s7-approach-section-title {
  font-size: 13px;
  font-weight: 900;
  color: #7c3aed;
  margin-bottom: 8px;
  letter-spacing: -0.1px;
}
.s7-approach-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-bottom: 14px;
}
.s7-approach-item {
  font-size: 12.5px;
  color: #1e293b;
  line-height: 1.5;
}
/* Formula result bar (green, at bottom of right col — matches Image 2) */
.s7-formula-result-bar {
  background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%);
  border: 2px solid #86efac;
  border-radius: 12px;
  padding: 12px 16px;
}
.s7-formula-result-text {
  font-family: 'Courier New', Courier, monospace;
  font-size: 14px;
  font-weight: 900;
  color: #15803d;
  line-height: 1.5;
  word-break: break-word;
}
.s7-formula-units {
  font-size: 11px;
  color: #166534;
  margin-top: 4px;
  font-style: italic;
}
/* Navigation row */
.s7-nav-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  padding: 16px 26px 22px;
  border-top: 1px solid #e8eef8;
  background: #fff;
}
/* Responsive */
@media (max-width: 600px) {
  .s7-body-cols { flex-direction: column; }
  .s7-left-col { width: 100%; border-right: none; border-bottom: 1.5px solid #e8eef8; }
}
/* ── Step-by-step approach items (numbered, like Image 2) ── */
.s7-approach-step {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 12.5px;
  color: #1e293b;
  line-height: 1.5;
  margin-bottom: 3px;
}
.s7-approach-step-num {
  font-weight: 800;
  color: #7c3aed;
  flex-shrink: 0;
  min-width: 18px;
}
.s7-approach-step-eq {
  display: block;
  margin-top: 4px;
  font-family: 'Courier New', Courier, monospace;
  font-size: 12px;
  font-weight: 700;
  color: #dc2626;
  background: #fff7ed;
  border-radius: 6px;
  padding: 2px 8px;
  word-break: break-word;
}
/* Final answer card in right col */
.s7-final-answer-card {
  background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%);
  border: 2px solid #86efac;
  border-radius: 14px;
  padding: 16px 18px;
}
.s7-final-answer-label {
  font-size: 10.5px;
  font-weight: 900;
  text-transform: uppercase;
  letter-spacing: 1.2px;
  color: #15803d;
  margin-bottom: 8px;
}
.s7-final-answer-value {
  font-family: 'Courier New', Courier, monospace;
  font-size: 15px;
  font-weight: 900;
  color: #1e293b;
  line-height: 1.7;
  word-break: break-word;
}
.s7-final-answer-unit {
  font-size: 11px;
  color: #15803d;
  margin-top: 5px;
  font-style: italic;
}
@keyframes s7FinalPop {
  0%   { transform: scale(0.92); opacity: 0; }
  100% { transform: scale(1); opacity: 1; }
}
</style>
"""

_SCENE7_DOM_TEMPLATE = """\
<div id="qanim-scene7-overlay">
  <div class="s7-card">
    <div class="s7-title-bar">
      <h2>Complete System &mdash; Solution Summary</h2>
    </div>
    <div class="s7-body-cols">
      <!-- LEFT column: system visual (like Image 2 left panel) -->
      <div class="s7-left-col">
        <div class="s7-system-label">System Diagram</div>
        <div class="s7-system-visual">
          <div class="s7-system-visual-title" id="s7-system-title">{system_title}</div>
          <div class="s7-system-arrows">&#x2191; &#x2191; &#x2191;</div>
          <div class="s7-system-label2" id="s7-system-label2">{system_label}</div>
        </div>
        <!-- Formula result bar (green) -->
        <div class="s7-formula-result-bar">
          <div class="s7-formula-result-text" id="s7-formula-result">{formula_result}</div>
          <div class="s7-formula-units" id="s7-units-hint">{units_hint}</div>
        </div>
      </div>
      <!-- RIGHT column: given params + solution approach -->
      <div class="s7-right-col">
        <div>
          <div class="s7-given-section-title">Given Parameters</div>
          <div class="s7-given-list" id="s7-given-list">{given_html}</div>
        </div>
        <div>
          <div class="s7-approach-section-title">Solution Approach</div>
          <div class="s7-approach-list" id="s7-approach-list">{approach_html}</div>
        </div>
        <!-- Final answer card -->
        <div class="s7-final-answer-card" id="s7-final-card">
          <div class="s7-final-answer-label">&#x2705; Result</div>
          <div class="s7-final-answer-value" id="s7-final-value">{formula_result} &rarr; Result in W</div>
          <div class="s7-final-answer-unit" id="s7-final-unit">{units_hint}</div>
        </div>
      </div>
    </div>
    <div class="s7-nav-row">
      <button class="btn-secondary" onclick="qanim_goToScene6FromScene7()">&#x2190; Back to Step 7</button>
      <button class="btn-primary" onclick="if(typeof qanim_showScene9==='function')qanim_showScene9()">Step 9: Final Answer &#x25B6;</button>
    </div>
  </div>
</div>"""

_SCENE7_JS = r"""
<script id="qanim-js-scene7">
(function initScene7(){
  'use strict';
  if(window.__qanimScene7Init)return; window.__qanimScene7Init=true;

  function _el(id){return document.getElementById(id);}
  function _onReady(fn){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fn);else setTimeout(fn,0);}

  /* Show Scene 7 (Step 8: Substitution) — called from Scene 6 or dot */
  window.qanim_showScene7 = function(){
    var ov=_el('qanim-scene7-overlay');
    if(ov){ov.style.display='block';setTimeout(function(){ov.classList.add('qanim-scene-visible');},10);}
    var ov6=_el('qanim-scene6-overlay');
    if(ov6) ov6.classList.remove('qanim-scene-visible');
    var bd=_el('qanim-scene-modal-backdrop');
    if(bd){bd.style.display='block';setTimeout(function(){bd.classList.add('qanim-scene-visible');},10);}
    /* Mark dots 0-6 done, dot 7 (Step 8) active */
    var dots=document.querySelectorAll('.step-dot');
    for(var i=0;i<dots.length;i++){
      dots[i].className='step-dot';
      if(i<7) dots[i].className+=' done';
      if(i===7) dots[i].className+=' active';
    }
    var sl=_el('step-label'); if(sl) sl.innerHTML='Step 8 of 9';
    var sb=_el('step-bar'); if(sb) sb.style.width='88.89%';
    var lbl=_el('step-label');
    if(lbl) lbl.innerText='Solution Summary';
    var bar=_el('step-bar');
    if(bar) bar.style.width='100%';
  };

  window.qanim_goToScene6FromScene7 = function(){
    var ov7=_el('qanim-scene7-overlay');
    if(ov7) ov7.classList.remove('qanim-scene-visible');
    if(typeof window.qanim_showScene6==='function') window.qanim_showScene6();
  };

  /* resetAnim also hides Scene 7 */
  _onReady(function(){
    var _origReset=window.resetAnim;
    window.resetAnim=function(){
      var ov7=_el('qanim-scene7-overlay');
      if(ov7) ov7.classList.remove('qanim-scene-visible');
      var bd=_el('qanim-scene-modal-backdrop');
      if(bd) bd.classList.remove('qanim-scene-visible');
      if(typeof _origReset==='function') _origReset();
    };
  });
})();
</script>
"""


def _build_s7_steps_html(gemini_sol, scene_script):
    """
    Build numbered step cards for Scene 7 from the solution data.
    Steps are derived from the solution_steps / steps in gemini_sol.
    Does NOT include the final numeric answer value — keeps method only.
    """
    raw_steps = []

    # 1. Try structured substitution_steps (richer data)
    sub_steps = gemini_sol.get("substitution_steps") or []
    for s in sub_steps:
        if isinstance(s, dict):
            title = s.get("title", "") or s.get("name", "")
            expr  = s.get("expr", "") or s.get("equation", "") or s.get("value", "")
            desc  = s.get("description", "") or s.get("desc", "")
            raw_steps.append({"title": str(title), "eq": str(expr), "desc": str(desc)})
        else:
            raw_steps.append({"title": "", "eq": str(s), "desc": ""})

    # 2. Fallback to flat steps list if no substitution_steps
    if not raw_steps:
        flat = gemini_sol.get("steps") or scene_script.get("solution_steps") or []
        for s in flat:
            s_str = str(s).strip()
            # Split "Step N: title — equation" pattern if present
            m = re.match(r"^(?:Step\s*\d+[:\.]?\s*)?(.+?)(?:\s*[—:]\s*(.+))?$", s_str, re.IGNORECASE)
            if m:
                title = m.group(1).strip() if m.group(1) else ""
                eq    = m.group(2).strip() if m.group(2) else ""
                raw_steps.append({"title": title[:80], "eq": eq[:120], "desc": ""})
            else:
                raw_steps.append({"title": s_str[:80], "eq": "", "desc": ""})

    # 3. Add explicit method steps from scene_script steps (skip final/freeze step)
    if not raw_steps:
        steps = scene_script.get("steps") or []
        for step in steps:
            if isinstance(step, dict):
                title = step.get("title", "") or step.get("label", "")
                desc  = step.get("description", "")
                raw_steps.append({"title": str(title)[:80], "eq": "", "desc": str(desc)[:200]})

    # Cap at 10 steps; remove any step that reveals the final numeric answer
    final_ans = str(gemini_sol.get("final_answer") or "").strip()
    filtered  = []
    for s in raw_steps[:10]:
        # Skip steps whose equation IS essentially the final answer (exact match)
        if final_ans and s.get("eq", "").strip() == final_ans:
            continue
        filtered.append(s)

    if not filtered:
        filtered = [
            {"title": "Identify given values",       "eq": "",  "desc": "List all known quantities and their units."},
            {"title": "State what to find",           "eq": "",  "desc": "Clearly define the unknown quantity."},
            {"title": "Select the governing formula", "eq": "",  "desc": "Choose the applicable law or equation."},
            {"title": "Substitute values",            "eq": "",  "desc": "Plug the known values into the formula."},
            {"title": "Simplify and solve",           "eq": "",  "desc": "Carry out the arithmetic step by step."},
        ]

    parts = []
    for i, step in enumerate(filtered, start=1):
        title_e = html_module.escape(str(step.get("title") or f"Step {i}")[:80])
        desc_e  = html_module.escape(str(step.get("desc") or "")[:300])
        eq_raw  = str(step.get("eq") or "").strip()
        eq_html = (
            '<div class="s7-step-eq">' + html_module.escape(eq_raw[:150]) + '</div>'
            if eq_raw else ""
        )
        desc_html = (
            '<div class="s7-step-desc">' + desc_e + '</div>' if desc_e else ""
        )
        parts.append(
            '<div class="s7-step">'
            '<div class="s7-step-num">' + str(i) + '</div>'
            '<div class="s7-step-body">'
            '<div class="s7-step-title">' + title_e + '</div>'
            + desc_html + eq_html +
            '</div></div>'
        )
    return "\n".join(parts)


def _build_s7_given_html(gemini_sol, scene_script):
    """
    Build the 'Given Parameters' bullet list for the left column of Scene 7.
    Shows symbol = value unit for each given quantity.
    """
    items = []

    # 1. Try structured given_data
    given_raw = gemini_sol.get("given_data") or []
    for g in given_raw[:8]:
        g_str = str(g).strip()
        if g_str:
            items.append(g_str)

    # 2. Fallback: scan badges from the first animation step
    if not items:
        steps = scene_script.get("steps") or []
        for step in steps[:2]:
            for b in (step.get("badges") or [])[:6]:
                text = b.get("text","") if isinstance(b,dict) else str(b)
                if text:
                    items.append(text)
            if items:
                break

    # 3. Final fallback
    if not items:
        items = ["See question for given values"]

    parts = []
    for item in items[:8]:
        item_e = html_module.escape(str(item)[:80])
        # Bold anything before '=' to highlight the symbol
        if "=" in item_e:
            sp = item_e.split("=", 1)
            item_e = "<strong>" + sp[0].strip() + "</strong> = " + sp[1].strip()
        parts.append('<div class="s7-given-item">' + item_e + '</div>')
    return "\n".join(parts)


def _build_s7_approach_html(gemini_sol, scene_script):
    """
    Build the 'Solution Approach' numbered steps for the right column of Scene 7.
    Matches Image 2: "Step 1: Identify Newton's Law of Cooling", with inline
    equation in a red/orange pill, then plain text steps below.
    """
    flat = gemini_sol.get("steps") or scene_script.get("solution_steps") or []
    headlines = []
    for s in flat[:5]:
        s_str = str(s).strip()
        s_str = re.sub(r"^Step\s*\d+[:\.]?\s*", "", s_str, flags=re.IGNORECASE).strip()
        # Split title — equation at first dash/colon
        parts = re.split(r"\s*[—:\|]\s*", s_str, 1)
        title = parts[0].strip()[:80]
        eq    = parts[1].strip()[:60] if len(parts) > 1 else ""
        if title and len(title) > 4:
            headlines.append({"title": title, "eq": eq})

    if not headlines:
        headlines = [
            {"title": "Identify governing formula",    "eq": ""},
            {"title": "Compute \u0394T = T_s \u2212 T_fluid", "eq": ""},
            {"title": "Substitute h, A, and \u0394T",         "eq": ""},
            {"title": "Multiply to get Q in Watts",    "eq": ""},
        ]

    parts = []
    for i, h in enumerate(headlines[:5], start=1):
        title_e = html_module.escape(h["title"])
        eq_e    = html_module.escape(h["eq"])
        eq_span = (
            '<span class="s7-approach-step-eq">' + eq_e + '</span>'
            if eq_e else ""
        )
        parts.append(
            '<div class="s7-approach-step">'
            '<span class="s7-approach-step-num">Step ' + str(i) + ':</span>'
            '<span>' + title_e + eq_span + '</span>'
            '</div>'
        )
    return "\n".join(parts)


def _build_s7_final_html(gemini_sol):
    """
    Build the concluding 'Final Answer' card that is appended as the last
    step of the Scene 7 substitution walkthrough — arrived at only after
    the user has stepped through every substitution/simplification line.
    """
    final_answer = html_module.escape(str(gemini_sol.get("final_answer") or "See the complete calculation above")[:200])
    unit         = html_module.escape(str(gemini_sol.get("final_answer_unit") or "")[:40])
    key_insight  = html_module.escape(str(gemini_sol.get("key_insight") or "")[:240])
    real_world   = html_module.escape(str(gemini_sol.get("real_world_note") or "")[:240])

    unit_html = ('<div class="s7-final-unit">SI Unit: <strong>' + unit + '</strong></div>') if unit else ""
    insight_html = ('<div class="s7-final-insight">&#x1F4A1; ' + key_insight + '</div>') if key_insight else ""
    real_world_html = ('<div class="s7-final-insight">&#x1F30D; ' + real_world + '</div>') if real_world else ""
    return (
        '<div class="s7-step s7-final-card">'
        '<div class="s7-final-badge">&#x2705; Final Answer</div>'
        '<div class="s7-final-value">' + final_answer + '</div>'
        + unit_html + insight_html + real_world_html +
        '</div>'
    )


def inject_scene7_how_we_solve_it(html, gemini_sol, scene_script):
    """
    Inject Scene 7 ("Complete System — Solution Summary") as a standalone
    overlay panel that appears when the user clicks "Continue to Solution" on Scene 6.

    Layout matches Image 2:
      LEFT col: System diagram (gradient box with arrows) + formula result bar
      RIGHT col: Given Parameters list + Solution Approach steps + Final answer card

    This function:
      1. Injects CSS into <head>
      2. Injects the DOM panel right after <body>
      3. Injects the JS module before </body>
    """
    _is_fallback_s7 = bool(gemini_sol.get("_used_fallback"))

    given_html    = _build_s7_given_html(gemini_sol, scene_script)
    approach_html = _build_s7_approach_html(gemini_sol, scene_script)

    # If fallback, replace the generic given/approach HTML with a clear error notice
    if _is_fallback_s7:
        given_html = (
            '<div class="s7-given-item" style="color:#dc2626;font-weight:700;">'
            '&#9888; Solution generation failed (API timeout or rate limit). '
            'Please restart and regenerate.</div>'
        )
        approach_html = (
            '<div class="s7-approach-step">'
            '<span class="s7-approach-step-num" style="color:#dc2626;">!</span>'
            '<span style="color:#dc2626;">Wait 60 s then click &#8635; Restart to regenerate.</span>'
            '</div>'
        )

    # Build formula result text for the green bar
    formula_result = ""
    _fb_fmla_texts_s7 = {f["text"] for f in GeminiSolutionGenerator._FALLBACK.get("formulas", [])
                         if isinstance(f, dict) and f.get("text")}
    formulas = gemini_sol.get("formulas") or []
    for f in formulas[:1]:
        candidate = f.get("text","") if isinstance(f,dict) else str(f)
        if candidate and candidate not in _fb_fmla_texts_s7:
            formula_result = candidate
            break
    if not formula_result:
        sol_steps = gemini_sol.get("steps") or []
        for s in sol_steps:
            s_str = str(s)
            if "=" in s_str and len(s_str) < 120:
                formula_result = s_str
                break
    if not formula_result or _is_fallback_s7:
        formula_result = (
            "\u26A0 Formula unavailable — regenerate animation"
            if _is_fallback_s7 else "Q = h \u00B7 A \u00B7 \u0394T"
        )

    # Units hint
    units_hint = str(gemini_sol.get("units_check") or gemini_sol.get("units") or "").strip()
    if not units_hint:
        ki = str(scene_script.get("key_insight") or "").strip()
        if "=" in ki and len(ki) < 120:
            units_hint = ki
        elif _is_fallback_s7:
            units_hint = "Solution not available — please regenerate"
        else:
            units_hint = "Units: check dimensional consistency"

    # System title and label for the left column visual (derived from scene script)
    system_title = scene_script.get("title", "Physical System") or "Physical System"
    # Try to get surface/object description from given data
    given_raw = gemini_sol.get("given_data") or []
    system_label_parts = []
    for g in given_raw[:2]:
        g_str = str(g).strip()
        if "=" in g_str:
            system_label_parts.append(g_str.split("=")[0].strip() + " = " + g_str.split("=")[1].strip())
    system_label = ", ".join(system_label_parts) if system_label_parts else "Given conditions"

    dom = _SCENE7_DOM_TEMPLATE.format(
        given_html=given_html,
        approach_html=approach_html,
        formula_result=html_module.escape(str(formula_result)[:200]),
        units_hint=html_module.escape(str(units_hint)[:160]),
        system_title=html_module.escape(str(system_title)[:60]),
        system_label=html_module.escape(str(system_label)[:80]),
    )

    # 1. CSS
    try:
        if "</head>" in html:
            html = html.replace("</head>", _SCENE7_CSS + "\n</head>", 1)
    except Exception as e:
        QAnimLogger.warn("Scene7Injector", f"CSS failed: {e}")

    # 2. DOM — insert right after <body ...>
    try:
        body_m = re.search(r"<body[^>]*>", html, re.IGNORECASE)
        if body_m:
            ins = body_m.end()
            html = html[:ins] + "\n" + dom + html[ins:]
    except Exception as e:
        QAnimLogger.warn("Scene7Injector", f"DOM failed: {e}")

    # 3. JS
    try:
        if "</body>" in html:
            html = html.replace("</body>", _SCENE7_JS + "\n</body>", 1)
        else:
            html += "\n" + _SCENE7_JS
    except Exception as e:
        QAnimLogger.warn("Scene7Injector", f"JS failed: {e}")

    QAnimLogger.ok("Scene7Injector", f"Scene 7 (Solution Summary — Image 2 style) injected")
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



# ===========================================================================
#  MODULE 12b — Scene 9 Injector (Step 9: Final Answer Modal)
#  Matches the reference HTML pattern: animated substitution rows → green
#  final-answer box → key insight bar, with back/restart navigation.
# ===========================================================================

_SCENE9_CSS = """
<style id="qanim-scene9-styles">
#qanim-scene9-overlay {
  display: none;
  position: fixed;
  top: 50%;
  left: 50%;
  transform: translate(-50%,-50%) scale(.95);
  z-index: 7500;
  width: min(780px, 96vw);
  max-height: 92vh;
  overflow-y: auto;
  box-sizing: border-box;
  opacity: 0;
  pointer-events: none;
  transition: opacity .3s ease, transform .3s cubic-bezier(.34,1.56,.64,1);
}
#qanim-scene9-overlay.qanim-scene-visible {
  display: block !important;
  opacity: 1;
  pointer-events: auto;
  transform: translate(-50%,-50%) scale(1);
}
.s9-card {
  background: #fff;
  border-radius: 20px;
  box-shadow: 0 8px 48px rgba(22,163,74,.18), 0 2px 8px rgba(0,0,0,.08);
  border: 2px solid #86efac;
  overflow: hidden;
  font-family: -apple-system, 'Segoe UI', Arial, sans-serif;
}
.s9-title-bar {
  text-align: center;
  padding: 22px 28px 18px;
  background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%);
  border-bottom: 2px solid #86efac;
}
.s9-title-bar h2 { font-size: 22px; font-weight: 900; color: #14532d; margin-bottom: 4px; }
.s9-title-bar p { font-size: 13px; color: #166534; margin: 0; }
.s9-body {
  padding: 32px 36px 28px;
  background: #fff;
  display: flex;
  flex-direction: column;
  gap: 22px;
}
.s9-formula-recap {
  background: #eff6ff;
  border: 1.5px solid #bfdbfe;
  border-radius: 12px;
  padding: 14px 20px;
  text-align: center;
}
.s9-formula-recap-label {
  font-size: 10.5px;
  font-weight: 800;
  color: #1d4ed8;
  text-transform: uppercase;
  letter-spacing: 1.2px;
  margin-bottom: 6px;
}
.s9-formula-recap-eq {
  font-family: 'Courier New', monospace;
  font-size: 16px;
  font-weight: 900;
  color: #1d4ed8;
}
.s9-sub-chain { display: flex; flex-direction: column; gap: 10px; }
.s9-sub-row {
  display: flex;
  align-items: center;
  gap: 12px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  padding: 12px 16px;
  opacity: 0;
  transform: translateX(-18px);
  transition: opacity .4s ease, transform .4s cubic-bezier(.34,1.56,.64,1);
}
.s9-sub-row.s9-shown { opacity: 1; transform: translateX(0); }
.s9-sub-num {
  background: #0891b2;
  color: #fff;
  border-radius: 50%;
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 800;
  flex-shrink: 0;
}
.s9-sub-eq {
  font-family: 'Courier New', monospace;
  font-size: 14px;
  font-weight: 700;
  color: #1e293b;
  flex: 1;
}
.s9-final-box {
  background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 100%);
  border: 3px solid #22c55e;
  border-radius: 18px;
  padding: 28px 32px;
  text-align: center;
  position: relative;
  overflow: hidden;
  opacity: 0;
  transform: scale(0.94);
  transition: opacity .5s ease .3s, transform .5s cubic-bezier(.34,1.56,.64,1) .3s;
}
.s9-final-box.s9-shown { opacity: 1; transform: scale(1); }
.s9-final-label {
  font-size: 11px;
  font-weight: 900;
  text-transform: uppercase;
  letter-spacing: 2px;
  color: #15803d;
  margin-bottom: 12px;
}
.s9-final-value {
  font-family: 'Courier New', monospace;
  font-size: 28px;
  font-weight: 900;
  color: #14532d;
  line-height: 1.3;
}
.s9-final-value span.s9-highlight { color: #16a34a; font-size: 36px; }
.s9-final-unit { font-size: 13px; color: #166534; margin-top: 8px; font-weight: 600; }
.s9-insight-bar {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  background: #fff7ed;
  border: 1.5px solid #fed7aa;
  border-radius: 10px;
  padding: 13px 18px;
  opacity: 0;
  transition: opacity .4s ease .6s;
}
.s9-insight-bar.s9-shown { opacity: 1; }
.s9-insight-icon { font-size: 20px; flex-shrink: 0; }
.s9-insight-text { font-size: 13px; color: #92400e; line-height: 1.6; }
.s9-insight-text strong { color: #78350f; }
.s9-nav-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  padding: 16px 36px 22px;
  border-top: 1px solid #bbf7d0;
  background: #f0fdf4;
}
</style>
"""


def inject_scene9_final_answer(html, gemini_sol, scene_script):
    """
    Inject Scene 9 (Step 9: Final Answer) as a standalone overlay modal.

    Layout matches the reference Furnace HTML Step 9:
      - Green-themed title bar
      - Formula recap (from Step 7)
      - Animated substitution chain rows (slide in one by one)
      - Big green final-answer box (scales in)
      - Key insight bar (fades in)
      - Nav row: Back to Step 8 | Restart Animation

    Inserts:
      1. CSS into <head>
      2. DOM panel right after <body>
      3. JS module before </body>
    """
    _is_fallback = bool(gemini_sol.get("_used_fallback"))

    # ── Extract formula for recap ──
    formula_raw = ""
    formulas = gemini_sol.get("formulas") or []
    for f in formulas[:1]:
        candidate = f.get("text", "") if isinstance(f, dict) else str(f)
        if candidate:
            formula_raw = candidate
            break
    if not formula_raw:
        sol_steps = gemini_sol.get("steps") or []
        for s in sol_steps:
            s_str = str(s)
            if "=" in s_str and len(s_str) < 120:
                formula_raw = s_str
                break
    if not formula_raw:
        formula_raw = scene_script.get("key_insight", "See formula in Step 7") or "See formula in Step 7"

    # ── Build substitution rows from solution_steps ──
    sol_steps_list = gemini_sol.get("steps") or scene_script.get("solution_steps") or []
    sub_rows_parts = []
    for i, step_text in enumerate(sol_steps_list[:6]):
        step_str = html_module.escape(str(step_text).strip()[:200])
        sub_rows_parts.append(
            '<div class="s9-sub-row" id="s9r' + str(i) + '">'
            '<div class="s9-sub-num">' + str(i + 1) + '</div>'
            '<div class="s9-sub-eq">' + step_str + '</div>'
            '</div>'
        )

    if not sub_rows_parts:
        sub_rows_parts = [
            '<div class="s9-sub-row" id="s9r0">'
            '<div class="s9-sub-num">1</div>'
            '<div class="s9-sub-eq">Substitute given values into the governing formula.</div>'
            '</div>',
            '<div class="s9-sub-row" id="s9r1">'
            '<div class="s9-sub-num">2</div>'
            '<div class="s9-sub-eq">Compute the result numerically.</div>'
            '</div>',
        ]
    sub_rows_html = "\n".join(sub_rows_parts)
    n_rows = len(sub_rows_parts)

    # ── Final answer ──
    final_answer_raw = (
        gemini_sol.get("final_answer")
        or scene_script.get("final_answer")
        or "See calculation above"
    )
    if _is_fallback:
        final_answer_raw = "Regenerate animation to see numerical answer"

    final_answer_esc = html_module.escape(str(final_answer_raw)[:200])

    # ── Key insight ──
    insight_raw = (
        gemini_sol.get("key_insight")
        or scene_script.get("key_insight")
        or "Review the solution steps above for the key physical reasoning."
    )
    if _is_fallback:
        insight_raw = "Solution generation failed — please wait 60s and regenerate."
    insight_esc = html_module.escape(str(insight_raw)[:400])

    # ── Problem subtitle ──
    subtitle_raw = scene_script.get("title", "Step-by-step solution") or "Step-by-step solution"
    subtitle_esc = html_module.escape(str(subtitle_raw)[:80])

    formula_esc = html_module.escape(str(formula_raw)[:200])

    dom_lines = [
        '<div id="qanim-scene9-overlay">',
        '  <div class="s9-card">',
        '    <div class="s9-title-bar">',
        '      <h2>&#x2705; Step 9 &mdash; Final Answer</h2>',
        '      <p>' + subtitle_esc + '</p>',
        '    </div>',
        '    <div class="s9-body">',
        '      <div class="s9-formula-recap">',
        '        <div class="s9-formula-recap-label">&#x1F4D0; Governing Formula (from Step 7)</div>',
        '        <div class="s9-formula-recap-eq">' + formula_esc + '</div>',
        '      </div>',
        '      <div class="s9-sub-chain" id="s9-sub-chain">',
        sub_rows_html,
        '      </div>',
        '      <div class="s9-final-box" id="s9-final-box">',
        '        <div class="s9-final-label">&#x2B50; Final Answer</div>',
        '        <div class="s9-final-value"><span class="s9-highlight">' + final_answer_esc + '</span></div>',
        '        <div class="s9-final-unit">Verify units for dimensional consistency &#x2714;</div>',
        '      </div>',
        '      <div class="s9-insight-bar" id="s9-insight-bar">',
        '        <span class="s9-insight-icon">&#x1F4A1;</span>',
        '        <div class="s9-insight-text"><strong>Key Insight:</strong> ' + insight_esc + '</div>',
        '      </div>',
        '    </div>',
        '    <div class="s9-nav-row">',
        '      <button class="btn-secondary" onclick="qanim_hideScene9();if(typeof qanim_showScene7===\'function\')qanim_showScene7();">&#x2190; Back to Step 8</button>',
        '      <button class="btn-primary" onclick="qanim_hideScene9();if(typeof resetAnim===\'function\')resetAnim();">&#x21BA; Restart Animation</button>',
        '    </div>',
        '  </div>',
        '</div>',
    ]
    dom = "\n".join(dom_lines) + "\n"

    scene9_js_lines = [
        '<script id="qanim-js-scene9">',
        '(function initScene9(){',
        '  "use strict";',
        '  if(window.__qanimScene9Init)return; window.__qanimScene9Init=true;',
        '  var _nRows=' + str(n_rows) + ';',
        '  function _el(id){return document.getElementById(id);}',
        '',
        '  function qanim_showScene9(){',
        '    var bd=_el("qanim-scene-modal-backdrop");',
        '    if(bd){bd.style.display="block";setTimeout(function(){bd.classList.add("qanim-scene-visible");},10);}',
        '    var ov=_el("qanim-scene9-overlay");',
        '    if(!ov)return;',
        '    ov.style.display="block";',
        '    setTimeout(function(){ov.classList.add("qanim-scene-visible");},10);',
        '    /* Update step dot state: dots 0-7 done, dot 8 (Step 9) active */',
        '    var dots=document.querySelectorAll(".step-dot");',
        '    for(var i=0;i<dots.length;i++){',
        '      dots[i].className="step-dot";',
        '      if(i<8)dots[i].className+=" done";',
        '      if(i===8)dots[i].className+=" active";',
        '    }',
        '    var sl=_el("step-label"); if(sl)sl.innerHTML="Step 9 of 9";',
        '    var sb=_el("step-bar"); if(sb)sb.style.width="100%";',
        '    /* Animate rows */',
        '    var rows=document.querySelectorAll(".s9-sub-row");',
        '    var finalBox=_el("s9-final-box");',
        '    var insightBar=_el("s9-insight-bar");',
        '    rows.forEach(function(r){r.classList.remove("s9-shown");});',
        '    if(finalBox)finalBox.classList.remove("s9-shown");',
        '    if(insightBar)insightBar.classList.remove("s9-shown");',
        '    rows.forEach(function(r,i){',
        '      setTimeout(function(){r.classList.add("s9-shown");},200+i*200);',
        '    });',
        '    setTimeout(function(){if(finalBox)finalBox.classList.add("s9-shown");},200+_nRows*200+100);',
        '    setTimeout(function(){if(insightBar)insightBar.classList.add("s9-shown");},200+_nRows*200+500);',
        '  }',
        '',
        '  function qanim_hideScene9(){',
        '    var ov=_el("qanim-scene9-overlay");',
        '    if(ov){ov.classList.remove("qanim-scene-visible");setTimeout(function(){ov.style.display="none";},350);}',
        '    var bd=_el("qanim-scene-modal-backdrop");',
        '    if(bd){bd.classList.remove("qanim-scene-visible");setTimeout(function(){bd.style.display="none";},300);}',
        '  }',
        '',
        '  window.qanim_showScene9=qanim_showScene9;',
        '  window.qanim_hideScene9=qanim_hideScene9;',
        '',
        '  /* resetAnim hides Scene 9 */',
        '  function _onReady(fn){if(document.readyState!=="loading"){fn();}else{document.addEventListener("DOMContentLoaded",fn);}}',
        '  _onReady(function(){',
        '    var _origReset=window.resetAnim;',
        '    window.resetAnim=function(){',
        '      qanim_hideScene9();',
        '      if(typeof _origReset==="function")_origReset();',
        '    };',
        '    /* Wire dot-step9 if present */',
        '    var d9=_el("dot-step9");',
        '    if(d9){d9.onclick=function(){qanim_showScene9();};}',
        '  });',
        '})();',
        '</script>',
    ]
    scene9_js = "\n".join(scene9_js_lines) + "\n"

    # 1. CSS
    try:
        if "</head>" in html:
            html = html.replace("</head>", _SCENE9_CSS + "\n</head>", 1)
    except Exception as e:
        QAnimLogger.warn("Scene9Injector", "CSS failed: " + str(e))

    # 2. DOM — insert right after <body ...>
    try:
        body_m = re.search(r"<body[^>]*>", html, re.IGNORECASE)
        if body_m:
            ins = body_m.end()
            html = html[:ins] + "\n" + dom + html[ins:]
    except Exception as e:
        QAnimLogger.warn("Scene9Injector", "DOM failed: " + str(e))

    # 3. JS
    try:
        if "</body>" in html:
            html = html.replace("</body>", scene9_js + "\n</body>", 1)
        else:
            html += "\n" + scene9_js
    except Exception as e:
        QAnimLogger.warn("Scene9Injector", "JS failed: " + str(e))

    QAnimLogger.ok("Scene9Injector", "Scene 9 (Final Answer modal) injected")
    return html

def inject_nav_patch_and_scene_desc(html, scene_descriptions=None):
    injection = _NAV_PATCH_JS + '\n'
    if '</body>' in html:
        html = html.replace('</body>', injection + '\n</body>', 1)
    else:
        html += '\n' + injection
    QAnimLogger.ok("NavPatch", "Nav patch injected")
    return html


# ===========================================================================
#  MODULE 13.5 — Math Typography Engine
#  ---------------------------------------------------------------------
#  Converts raw ASCII/LaTeX-ish math text (R_A, 10^5, muC, theta, >=, sqrt(2),
#  8.99*10^9, \(R_A=10\text{cm}\), ...) into proper textbook-style Unicode/HTML
#  typography (Rₐ / R<sub>A</sub>, 10⁵, μC, θ, ≥, √2, 8.99 × 10⁹) EVERYWHERE
#  text is rendered on the page — formula cards, badges, given values, Main
#  Formula (Scene 6), Solution (Scene 7), Notes, Glossary, tooltips, labels —
#  including content injected later (AI-generated dynamic values), because it
#  runs as a live DOM text scanner + MutationObserver rather than a one-time
#  string patch on any single panel.
# ===========================================================================

_MATH_TYPOGRAPHY_JS = r"""
<script id="qanim-js-mathtypography">
(function initMathTypography(){
  'use strict';
  if(window.__qanimMathTypoInit)return; window.__qanimMathTypoInit=true;

  /* ── Unicode subscript / superscript character maps ──
     Unicode has no true subscript glyph for every Latin letter; where one
     doesn't exist we fall back to a real <sub>/<sup> HTML tag, which always
     renders correctly regardless of font support. */
  var SUB_MAP = {
    '0':'\u2080','1':'\u2081','2':'\u2082','3':'\u2083','4':'\u2084','5':'\u2085',
    '6':'\u2086','7':'\u2087','8':'\u2088','9':'\u2089',
    '+':'\u208A','-':'\u208B','=':'\u208C','(':'\u208D',')':'\u208E',
    'a':'\u2090','e':'\u2091','h':'\u2095','i':'\u1D62','j':'\u2C7C','k':'\u2096',
    'l':'\u2097','m':'\u2098','n':'\u2099','o':'\u2092','p':'\u209A','r':'\u1D63',
    's':'\u209B','t':'\u209C','u':'\u1D64','v':'\u1D65','x':'\u2093',
    'b':'\u1D66' /* Greek subscript beta — closest visual stand-in, no true Latin "b" subscript exists */
  };
  var SUP_MAP = {
    '0':'\u2070','1':'\u00B9','2':'\u00B2','3':'\u00B3','4':'\u2074','5':'\u2075',
    '6':'\u2076','7':'\u2077','8':'\u2078','9':'\u2079',
    '+':'\u207A','-':'\u207B','=':'\u207C','(':'\u207D',')':'\u207E',
    'n':'\u207F','i':'\u2071'
  };

  function canMap(tok, map){
    for(var i=0;i<tok.length;i++){ if(!map.hasOwnProperty(tok[i].toLowerCase())) return false; }
    return true;
  }
  function applyMap(tok, map){
    var out=''; for(var i=0;i<tok.length;i++){ out += map[tok[i].toLowerCase()] || tok[i]; } return out;
  }

  /* ── Greek letter names → symbols (word-boundary matched) ── */
  var GREEK = {
    'alpha':'\u03B1','Alpha':'\u0391','beta':'\u03B2','Beta':'\u0392',
    'gamma':'\u03B3','Gamma':'\u0393','delta':'\u03B4','Delta':'\u0394',
    'epsilon':'\u03B5','Epsilon':'\u0395','zeta':'\u03B6','Zeta':'\u0396',
    'eta':'\u03B7','Eta':'\u0397','theta':'\u03B8','Theta':'\u0398',
    'iota':'\u03B9','Iota':'\u0399','kappa':'\u03BA','Kappa':'\u039A',
    'lambda':'\u03BB','Lambda':'\u039B','nu':'\u03BD','Nu':'\u039D',
    'xi':'\u03BE','Xi':'\u039E','omicron':'\u03BF','Omicron':'\u039F',
    'pi':'\u03C0','Pi':'\u03A0','rho':'\u03C1','Rho':'\u03A1',
    'sigma':'\u03C3','Sigma':'\u03A3','tau':'\u03C4','Tau':'\u03A4',
    'upsilon':'\u03C5','Upsilon':'\u03A5','phi':'\u03C6','Phi':'\u03A6',
    'chi':'\u03C7','Chi':'\u03A7','psi':'\u03C8','Psi':'\u03A8',
    'omega':'\u03C9','Omega':'\u03A9'
    /* 'mu'/'Mu' handled separately below — needs to also match "muC" style
       unit prefixes with no trailing word boundary. */
  };

  /* ── LaTeX macros AI-generated content sometimes contains ── */
  var LATEX_MACROS = {
    '\\times':'\u00D7', '\\cdot':'\u00B7', '\\pm':'\u00B1', '\\mp':'\u2213',
    '\\geq':'\u2265', '\\leq':'\u2264', '\\neq':'\u2260', '\\approx':'\u2248',
    '\\propto':'\u221D', '\\infty':'\u221E', '\\rightarrow':'\u2192', '\\to':'\u2192',
    '\\leftarrow':'\u2190', '\\leftrightarrow':'\u2194',
    '\\Rightarrow':'\u21D2', '\\Leftrightarrow':'\u21D4',
    '\\rightleftharpoons':'\u21CC', '\\rightleftarrows':'\u21C4',
    '\\angle':'\u2220', '\\parallel':'\u2225', '\\perp':'\u27C2', '\\in':'\u2208',
    '\\notin':'\u2209',
    '\\subset':'\u2282', '\\cup':'\u222A', '\\cap':'\u2229', '\\int':'\u222B',
    '\\sum':'\u2211', '\\prod':'\u220F', '\\partial':'\u2202', '\\nabla':'\u2207',
    '\\therefore':'\u2234', '\\because':'\u2235',
    '\\varnothing':'\u2205', '\\emptyset':'\u2205', '\\hbar':'\u0127',
    '\\deg':'\u00B0',
    '\\mu':'\u03BC', '\\theta':'\u03B8', '\\pi':'\u03C0', '\\lambda':'\u03BB',
    '\\alpha':'\u03B1', '\\beta':'\u03B2', '\\gamma':'\u03B3', '\\delta':'\u03B4',
    '\\sigma':'\u03C3', '\\rho':'\u03C1', '\\phi':'\u03C6', '\\omega':'\u03C9',
    '\\Omega':'\u03A9', '\\Delta':'\u0394', '\\Theta':'\u0398', '\\Lambda':'\u039B',
    '\\Sigma':'\u03A3', '\\Phi':'\u03A6', '\\nu':'\u03BD', '\\eta':'\u03B7', '\\tau':'\u03C4'
  };

  function escapeRegExp(s){ return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  /* ── Protected-fragment vault ──
     Some conversions (stacked fractions, chemistry subscripts, \frac) need to
     emit their own <span>/<sub>/<sup> markup *before* the generic HTML-escape
     pass runs later in mathify(). Rather than special-case ordering, any such
     HTML is stashed here and swapped back in as the very last step, so it is
     immune to the escaping / superscript / subscript passes in between. */
  var _vault = [];
  function protect(htmlFragment){
    var token = '\u0001' + (_vault.length) + '\u0002';
    _vault.push(htmlFragment);
    return token;
  }
  function restoreProtected(text){
    if(!_vault.length) return text;
    return text.replace(/\u0001(\d+)\u0002/g, function(m, idx){
      return _vault[Number(idx)] !== undefined ? _vault[Number(idx)] : m;
    });
  }

  /* ── Reusable sub/superscript token formatter (used by the main pass AND
     by fraction / chemistry operand formatting) ── */
  function supSubToken(base, token, map){
    return base + (canMap(token, map) ? applyMap(token, map) : (map===SUB_MAP ? '<sub>'+token+'</sub>' : '<sup>'+token+'</sup>'));
  }

  /* ── Small self-contained formatter for fraction numerator/denominator
     and chemistry text — handles greek names + ^/_ notation without
     re-running fraction/chemistry detection (avoids recursion). ── */
  function formatAtom(s){
    var t = String(s);
    Object.keys(GREEK).forEach(function(word){
      var re = new RegExp('\\b' + word + '\\b', 'g');
      t = t.replace(re, GREEK[word]);
    });
    t = t.replace(/([A-Za-z0-9\u0370-\u03FF])_\{?([A-Za-z0-9+\-]+)\}?/g, function(m, base, sub){
      return supSubToken(base, sub, SUB_MAP);
    });
    t = t.replace(/([A-Za-z0-9)\]\u0370-\u03FF])\^\{?(-?[A-Za-z0-9]+)\}?/g, function(m, base, exp){
      return supSubToken(base, exp, SUP_MAP);
    });
    return t;
  }

  /* One-time CSS injection for stacked fractions */
  function ensureFractionCSS(){
    if(document.getElementById('qanim-frac-css')) return;
    var style = document.createElement('style');
    style.id = 'qanim-frac-css';
    style.textContent =
      '.qanim-frac{display:inline-flex;flex-direction:column;vertical-align:middle;' +
        'text-align:center;margin:0 .15em;font-size:0.95em;line-height:1.05;}' +
      '.qanim-frac .qanim-frac-num,.qanim-frac .qanim-frac-den{display:block;padding:0 .2em;}' +
      '.qanim-frac .qanim-frac-num{border-bottom:1.5px solid currentColor;padding-bottom:1px;}' +
      '.qanim-frac .qanim-frac-den{padding-top:1px;}';
    (document.head || document.documentElement).appendChild(style);
  }

  function buildFractionHtml(numRaw, denRaw){
    ensureFractionCSS();
    return '<span class="qanim-frac"><span class="qanim-frac-num">' + formatAtom(numRaw) +
           '</span><span class="qanim-frac-den">' + formatAtom(denRaw) + '</span></span>';
  }

  /* ── Chemistry: common element symbols (covers the compounds/ions that
     show up in high-school/intro university chemistry). Case-sensitive and
     longest-symbol-first so real element tokens are matched before the
     generic single-letter fallback, which keeps false positives (ordinary
     capitalized words) low. ── */
  var ELEMENTS = ['Na','Ca','Fe','Mg','Al','Zn','Cu','Ag','Au','Pb','Sn','Ni','Mn',
    'Cr','Co','Br','Si','He','Ne','Ar','Li','Be','Xe','Kr','Cl',
    'H','O','C','N','S','P','K','F','I','B','U','V','W'];
  ELEMENTS.sort(function(a,b){ return b.length - a.length; });
  var ELEM_ALT = ELEMENTS.join('|');
  var CHEM_RE = new RegExp('\\b(?:(?:' + ELEM_ALT + ')\\d{0,2}){2,6}(?:\\^?\\d{0,2}[+\\-])?(?![A-Za-z0-9])|' +
                            '\\b(?:' + ELEM_ALT + ')\\d{0,2}\\^?\\d{0,2}[+\\-](?![A-Za-z0-9])', 'g');

  function convertChemistry(text){
    return text.replace(CHEM_RE, function(whole){
      var work = whole;
      var chargeMatch = work.match(/\^?(\d{0,2})([+\-])$/);
      var charge = '';
      if(chargeMatch){
        work = work.slice(0, work.length - chargeMatch[0].length);
        charge = '<sup>' + (chargeMatch[1] || '') + chargeMatch[2] + '</sup>';
      }
      if(!work && !charge) return whole;
      var out = work.replace(/([A-Z][a-z]?)(\d+)/g, function(m2, el, num){
        return el + (canMap(num, SUB_MAP) ? applyMap(num, SUB_MAP) : '<sub>'+num+'</sub>');
      });
      if(out === work && !charge) return whole; /* nothing actually changed — leave untouched */
      return protect(out + charge);
    });
  }

  /* ── Plain a/b → stacked fraction detector.
     Restricted to simple numeric / single-variable operands (with optional
     sign, decimal point, or a single ^exponent) so we don't mangle dates
     (12/25/2024), URLs, or prose fractions like "3/4 cup of flour". ── */
  var FRAC_OPERAND = '-?(?:\\d+(?:\\.\\d+)?|[A-Za-z\u0391-\u03C9]+\\d*(?:\\^-?\\d+)?)';
  var FRAC_RE = new RegExp('(?:^|(?<=[\\s(=]))(' + FRAC_OPERAND + ')\\/(' + FRAC_OPERAND + ')(?=[\\s).,;]|$)', 'g');

  /* Common unit symbols — if either side of a "/" is one of these, treat it
     as a compound unit (m/s², kg/m³, J/mol, …) and leave it inline rather
     than rendering a stacked fraction, matching standard unit typography. */
  var UNIT_BASES = {m:1,s:1,g:1,kg:1,cm:1,mm:1,km:1,J:1,N:1,W:1,Pa:1,mol:1,K:1,
    A:1,V:1,C:1,T:1,Hz:1,L:1,ml:1,eV:1,lb:1,ft:1,'in':1,mi:1,hr:1,min:1,h:1,
    atm:1,cal:1,S:1,F:1,Wb:1,H:1,lx:1,sr:1,rad:1,mA:1,kA:1,kJ:1,kN:1,kW:1,
    mV:1,mm3:1,cm3:1,m3:1,cm2:1,m2:1};

  function operandBase(op){ return op.replace(/\^-?\d+$/,''); }

  function convertFractions(text){
    return text.replace(FRAC_RE, function(whole, num, den, offset, full){
      /* Skip anything that looks like a date: d/d immediately followed by another /d */
      var after = full.slice(offset + whole.length, offset + whole.length + 6);
      if(/^\/\d/.test(after)) return whole;
      var before = full.slice(Math.max(0, offset-6), offset);
      if(/\d\/$/.test(before)) return whole;
      /* Skip compound units — those stay inline (e.g. "m/s^2", "kg/m^3") */
      if(UNIT_BASES.hasOwnProperty(operandBase(num)) || UNIT_BASES.hasOwnProperty(operandBase(den))) return whole;
      return protect(buildFractionHtml(num, den));
    });
  }

  /* ── The core text → typographic-HTML converter ──
     Order matters: LaTeX cleanup → fractions/chemistry (protected HTML) →
     word/macro swaps → ASCII operators → escape any remaining raw <, >, & →
     subscript/superscript (may add tags) → restore protected fragments. */
  function mathify(raw){
    var text = String(raw);
    if(!text || text.indexOf('_')===-1 && text.indexOf('^')===-1 && text.indexOf('/')===-1 &&
       !/[A-Za-z]/.test(text) && text.indexOf('\\')===-1){
      return null; /* nothing worth scanning (pure whitespace/punctuation) */
    }

    /* 1. LaTeX delimiter / wrapper cleanup */
    text = text.replace(/\\\(|\\\)|\\\[|\\\]/g, '');
    text = text.replace(/\\text\{([^{}]*)\}/g, '$1');
    text = text.replace(/\\mathrm\{([^{}]*)\}/g, '$1');

    /* 1b. \vec{X} → combining right-arrow over the variable (plain unicode,
       no protection needed since it contains no HTML-sensitive chars) */
    text = text.replace(/\\vec\{([^{}]+)\}/g, function(m, v){ return v + '\u20D7'; });
    text = text.replace(/\\vec\s+([A-Za-z])/g, function(m, v){ return v + '\u20D7'; });

    text = text.replace(/\\sqrt\{([^{}]*)\}/g, '\u221A($1)');

    /* 1c. \frac{a}{b} → real stacked fraction (protected HTML) */
    text = text.replace(/\\frac\{([^{}]*)\}\{([^{}]*)\}/g, function(m, a, b){
      return protect(buildFractionHtml(a, b));
    });

    /* 2. LaTeX macros (longest-first isn't required — none of ours are prefixes of another) */
    Object.keys(LATEX_MACROS).forEach(function(key){
      var re = new RegExp(escapeRegExp(key), 'g');
      text = text.replace(re, LATEX_MACROS[key]);
    });
    /* strip any leftover unrecognised LaTeX macro backslashes, e.g. "\phi" already handled above */
    text = text.replace(/\\([a-zA-Z]+)/g, '$1');

    /* 3. Greek word names (word-boundary), plus compact concatenated forms
       like "deltaE" or "thetaC" (word immediately followed by a variable
       with no separating space — common in AI-generated compact notation). */
    Object.keys(GREEK).forEach(function(word){
      var re = new RegExp('\\b' + word + '\\b', 'g');
      text = text.replace(re, GREEK[word]);
      var reConcat = new RegExp('\\b' + word + '(?=[A-Z0-9])', 'g');
      text = text.replace(reConcat, GREEK[word]);
    });
    /* "mu"/"Mu" — also converts unit prefixes like "muC", "muF" with no space */
    text = text.replace(/\bmu(?![a-z])/g, '\u03BC');
    text = text.replace(/\bMu(?![a-zA-Z])/g, '\u03BC');
    /* common compact physics-constant words */
    text = text.replace(/\bhbar\b/g, '\u0127');
    text = text.replace(/\bmu0\b/g, '\u03BC\u2080');
    text = text.replace(/\bepsilon0\b/g, '\u03B5\u2080');
    text = text.replace(/\btherefore\b/g, '\u2234');
    text = text.replace(/\bbecause\b/g, '\u2235');

    /* 4. Units & everyday math words */
    text = text.replace(/\bohms?\b/g, '\u03A9');
    text = text.replace(/\s*\bdegrees?\b/g, '\u00B0');
    text = text.replace(/\bmicro\b/g, '\u03BC');
    text = text.replace(/\binfinity\b/g, '\u221E');
    text = text.replace(/\bapproximately\b/g, '\u2248');
    text = text.replace(/\bproportional\b/g, '\u221D');
    text = text.replace(/\bperpendicular\b/g, '\u27C2');
    text = text.replace(/\bparallel\b/g, '\u2225');
    text = text.replace(/\bangle\s+([A-Z]{2,5})\b/g, '\u2220$1');
    text = text.replace(/sqrt\s*\(([^)]+)\)/gi, '\u221A($1)');
    text = text.replace(/sqrt\s*([0-9]+(?:\.[0-9]+)?)/gi, '\u221A$1');

    /* 5. Unambiguous ASCII comparison/operator/arrow sequences */
    text = text.replace(/<=>/g, '\u21CC');
    text = text.replace(/<->/g, '\u2194');
    text = text.replace(/=>/g, '\u21D2');
    text = text.replace(/->/g, '\u2192');
    text = text.replace(/<-/g, '\u2190');
    text = text.replace(/>=/g, '\u2265');
    text = text.replace(/<=/g, '\u2264');
    text = text.replace(/!=/g, '\u2260');
    text = text.replace(/\+-/g, '\u00B1');

    /* 6. Multiplication "*" → "×", but never touch markdown "**bold**" */
    text = text.replace(/\*\*/g, '\u0000BOLD\u0000');
    text = text.replace(/([A-Za-z0-9)\]\u0370-\u03FF])\s*\*\s*([A-Za-z0-9(\u0370-\u03FF])/g, '$1 \u00D7 $2');
    text = text.replace(/\u0000BOLD\u0000/g, '**');

    /* 7. Scientific "e" notation → × 10^exp (exponent turned into superscript below) */
    text = text.replace(/(\d+(?:\.\d+)?)[eE]([+-]?\d+)\b/g, function(m, base, exp){
      return base + ' \u00D7 10^' + exp;
    });

    /* 7b. Chemistry formulas / ions (H2O, CO2, SO4^2-, Fe3+, Na+, Ca2+, Cl-)
       — must run before the generic HTML-escape pass since it emits its own
       <sub>/<sup> markup (protected until the very end). */
    text = convertChemistry(text);

    /* 7c. Plain a/b → stacked textbook fraction (also protected). Runs after
       chemistry/scientific-notation so it doesn't collide with those, and
       before escaping since it emits HTML. */
    text = convertFractions(text);

    /* 8. Escape any remaining literal HTML-sensitive characters before we
       start inserting our own <sub>/<sup> tags. (Protected fragments use
       \u0001/\u0002 control chars, which are untouched by this pass.) */
    text = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    /* 9. Subscripts: base_sub or base_{sub} */
    text = text.replace(/([A-Za-z0-9\u0370-\u03FF])_\{?([A-Za-z0-9+\-]+)\}?/g, function(m, base, sub){
      return supSubToken(base, sub, SUB_MAP);
    });

    /* 10. Superscripts: base^exp or base^{exp} */
    text = text.replace(/([A-Za-z0-9)\]\u0370-\u03FF])\^\{?(-?[A-Za-z0-9]+)\}?/g, function(m, base, exp){
      return supSubToken(base, exp, SUP_MAP);
    });

    /* 11. Swap protected fraction/chemistry HTML back in */
    text = restoreProtected(text);

    return text;
  }

  /* ── DOM walking: replace text nodes with their mathified HTML ── */
  var SKIP_TAGS = {SCRIPT:1, STYLE:1, NOSCRIPT:1, TEXTAREA:1, SELECT:1, OPTION:1, INPUT:1};

  function shouldSkip(el){
    while(el){
      if(el.nodeType===1){
        if(SKIP_TAGS[el.tagName]) return true;
        if(el.classList && el.classList.contains('qanim-no-mathify')) return true;
      }
      el = el.parentNode;
    }
    return false;
  }

  function processNode(root){
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
    var targets = [];
    var n;
    while((n = walker.nextNode())){
      if(!n.nodeValue || !n.nodeValue.trim()) continue;
      if(shouldSkip(n.parentNode)) continue;
      targets.push(n);
    }
    for(var i=0;i<targets.length;i++){
      var node = targets[i];
      var out = mathify(node.nodeValue);
      if(out===null || out===node.nodeValue) continue;
      var tmp = document.createElement('span');
      tmp.innerHTML = out;
      var frag = document.createDocumentFragment();
      while(tmp.firstChild) frag.appendChild(tmp.firstChild);
      if(node.parentNode) node.parentNode.replaceChild(frag, node);
    }
  }

  var observer = new MutationObserver(function(mutations){
    observer.disconnect();
    mutations.forEach(function(mut){
      mut.addedNodes && mut.addedNodes.forEach(function(added){
        if(added.nodeType===1 || added.nodeType===3) processNode(added.nodeType===3 ? added.parentNode : added);
      });
    });
    observer.observe(document.body, {childList:true, subtree:true});
  });

  function start(){
    processNode(document.body);
    observer.observe(document.body, {childList:true, subtree:true});
    patchCanvasText();
  }

  /* ── Canvas support ──
     Canvas <text> is drawn as pixels, not DOM text, so the MutationObserver/
     TreeWalker approach above can never reach it. We patch fillText/strokeText
     to run strings through a plain-unicode-only converter first (no HTML
     tags are possible on a canvas — sub/superscripts fall back to whichever
     Unicode glyphs exist, e.g. digits and common letters, and otherwise the
     original text is left as-is rather than emitting broken markup). */
  function mathifyPlain(raw){
    var text = String(raw);
    if(!text) return text;
    Object.keys(GREEK).forEach(function(word){
      var re = new RegExp('\\b' + word + '\\b', 'g');
      text = text.replace(re, GREEK[word]);
    });
    text = text.replace(/\bmu(?![a-z])/g, '\u03BC');
    text = text.replace(/\bhbar\b/g, '\u0127');
    text = text.replace(/(\d+(?:\.\d+)?)[eE]([+-]?\d+)\b/g, function(m, base, exp){
      return base + ' \u00D7 10' + (canMap(exp, SUP_MAP) ? applyMap(exp, SUP_MAP) : '^' + exp);
    });
    text = text.replace(/sqrt\s*\(([^)]+)\)/gi, '\u221A($1)');
    text = text.replace(/>=/g, '\u2265').replace(/<=/g, '\u2264').replace(/!=/g, '\u2260').replace(/\+-/g, '\u00B1');
    text = text.replace(/->/g, '\u2192');
    text = text.replace(/([A-Za-z0-9\u0370-\u03FF])_\{?([A-Za-z0-9+\-]+)\}?/g, function(m, base, sub){
      return base + (canMap(sub, SUB_MAP) ? applyMap(sub, SUB_MAP) : sub);
    });
    text = text.replace(/([A-Za-z0-9)\]\u0370-\u03FF])\^\{?(-?[A-Za-z0-9]+)\}?/g, function(m, base, exp){
      return base + (canMap(exp, SUP_MAP) ? applyMap(exp, SUP_MAP) : exp);
    });
    return text;
  }

  function patchCanvasText(){
    if(typeof CanvasRenderingContext2D === 'undefined') return;
    if(CanvasRenderingContext2D.prototype.__qanimPatched) return;
    CanvasRenderingContext2D.prototype.__qanimPatched = true;
    ['fillText','strokeText'].forEach(function(fn){
      var orig = CanvasRenderingContext2D.prototype[fn];
      CanvasRenderingContext2D.prototype[fn] = function(text, x, y, maxWidth){
        var converted = mathifyPlain(text);
        if(maxWidth === undefined) return orig.call(this, converted, x, y);
        return orig.call(this, converted, x, y, maxWidth);
      };
    });
  }

  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
</script>
"""


def inject_math_typography(html):
    """
    Injects the Math Typography Engine — a client-side script that rewrites
    every raw-text math expression on the page (R_A, 10^5, muC, theta, >=,
    sqrt(2), 8.99*10^9, LaTeX-ish \\(...\\) fragments, etc.) into proper
    textbook-style Unicode/HTML typography (Rₐ, 10⁵, μC, θ, ≥, √2, 8.99 × 10⁹).

    Runs last in the injection chain so it can scan the fully-assembled page
    (all panels, Scene 6, Scene 7), and keeps watching via MutationObserver so
    any later AI-generated / dynamically-added content is formatted too.
    """
    injection = _MATH_TYPOGRAPHY_JS + '\n'
    if '</body>' in html:
        html = html.replace('</body>', injection + '\n</body>', 1)
    else:
        html += '\n' + injection
    QAnimLogger.ok("MathTypography", "Math typography engine injected")
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
        "dom":  ["qanim-controls-bar", "answerbox-ctrl-btn"],
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
    "Scene6": {
        "data": None,
        "css":  ["qanim-scene6-styles"],
        "dom":  ["qanim-scene6-overlay", "s6-formula-text", "qanim-scene-modal-backdrop"],
        "js":   ["qanim-js-scene6"],
    },
    "Scene7": {
        "data": None,
        "css":  ["qanim-scene7-styles"],
        "dom":  ["qanim-scene7-overlay", "s7-given-list"],
        "js":   ["qanim-js-scene7"],
    },
    "MathTypography": {
        "data": None, "css": None, "dom": None,
        "js":   ["qanim-js-mathtypography"],
    },
    "Scene9": {
        "data": None,
        "css":  ["qanim-scene9-styles"],
        "dom":  ["qanim-scene9-overlay", "s9-final-box"],
        "js":   ["qanim-js-scene9"],
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
    "Scene6": [
        re.compile(r'<style[^>]*id=["\']qanim-scene6-styles["\'][^>]*>.*?</style>', re.DOTALL | re.IGNORECASE),
        re.compile(r'<div id="qanim-scene-modal-backdrop"[^>]*>\s*</div>\s*', re.DOTALL),
        # NOTE: DOM removal for #qanim-scene6-overlay is handled by
        # _strip_balanced_div() (balanced tag counting), not regex --
        # nested <div>s can't be matched reliably with a lookahead.
        re.compile(r'<script[^>]*id=["\']qanim-js-scene6["\'][^>]*>.*?</script>', re.DOTALL),
    ],
    "Scene7": [
        re.compile(r'<style[^>]*id=["\']qanim-scene7-styles["\'][^>]*>.*?</style>', re.DOTALL | re.IGNORECASE),
        # NOTE: DOM removal for #qanim-scene7-overlay is handled by
        # _strip_balanced_div() (balanced tag counting), not regex --
        # nested <div>s can't be matched reliably with a lookahead.
        re.compile(r'<script[^>]*id=["\']qanim-js-scene7["\'][^>]*>.*?</script>', re.DOTALL),
    ],
    "MathTypography": [
        re.compile(r'<script[^>]*id=["\']qanim-js-mathtypography["\'][^>]*>.*?</script>', re.DOTALL),
    ],
    "Scene9": [
        re.compile(r'<style[^>]*id=[\"\']qanim-scene9-styles[\"\'][^>]*>.*?</style>', re.DOTALL | re.IGNORECASE),
        re.compile(r'<script[^>]*id=[\"\']qanim-js-scene9[\"\'][^>]*>.*?</script>', re.DOTALL),
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

    def __init__(self, gemini_sol, answer_targets, glossary_terms, to_find_targets, scene_script=None):
        self.gemini_sol = gemini_sol
        self.answer_targets = answer_targets
        self.glossary_terms = glossary_terms or []
        self.to_find_targets = to_find_targets or []
        self.scene_script = scene_script or {}


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
        html = inject_notes_system(html)
        html = inject_answer_box_panel(html, ctx.answer_targets)
        html = inject_controls_bar(html)
        html = inject_previous_step_button(html)
        html = inject_to_find_system(html, ctx.to_find_targets)
        html = inject_glossary_panel(html, ctx.glossary_terms)
        html = inject_nav_patch_and_scene_desc(html)
        html = inject_step_controller(html)
        # Scene 6 & 7 — appended AFTER the core panels so they land last in <body>
        html = inject_scene6_big_idea(html, ctx.gemini_sol, ctx.scene_script)
        html = inject_scene7_how_we_solve_it(html, ctx.gemini_sol, ctx.scene_script)
        # Scene 9 — Final Answer modal (Step 9 of the 9-step workflow)
        html = inject_scene9_final_answer(html, ctx.gemini_sol, ctx.scene_script)
        # Deterministic safety net — do not rely on Gemini's own nextStep()
        # to remember to open Scene 6; watch button state instead.
        html = inject_scene6_autotrigger(html)
        # Math typography — runs LAST so it can scan the fully-assembled page
        html = inject_math_typography(html)
        return html

    @staticmethod
    def _strip_balanced_div(html, id_):
        """
        Remove a <div id="id_" ...> ... </div> block by counting nested
        <div> / </div> tags to find the TRUE matching close tag, rather
        than guessing via a regex lookahead. Regex cannot reliably match
        balanced/nested tags -- a lookahead like (?=<div|<script|</body)
        can stop at the wrong nested </div> whenever a comment or other
        sibling markup sits in between, leaving most of the old block
        behind and causing duplicate content on repair/re-injection.
        Safe no-op if the id isn't found.
        """
        m = re.search(r'<div[^>]*\bid=["\']' + re.escape(id_) + r'["\'][^>]*>', html)
        if not m:
            return html
        start = m.start()
        pos = m.end()
        depth = 1
        for tag_m in re.finditer(r'<div\b[^>]*>|</div\s*>', html[pos:]):
            if tag_m.group(0).startswith('</div'):
                depth -= 1
            else:
                depth += 1
            if depth == 0:
                end = pos + tag_m.end()
                return html[:start] + html[end:]
        # Unbalanced / not found -- leave html untouched rather than
        # risk corrupting the document.
        return html

    @classmethod
    def _strip(cls, html, name):
        for pattern in STRIP_PATTERNS.get(name, []):
            html = pattern.sub('', html)
        # Balanced-match strip for the components whose DOM root is a
        # <div> containing further nested <div>s -- regex lookaheads
        # aren't reliable for these (see _strip_balanced_div docstring).
        if name == "Scene6":
            html = cls._strip_balanced_div(html, "qanim-scene6-overlay")
        elif name == "Scene7":
            html = cls._strip_balanced_div(html, "qanim-scene7-overlay")
        elif name == "Scene9":
            html = cls._strip_balanced_div(html, "qanim-scene9-overlay")
        return html

    @classmethod
    def _repair(cls, html, ctx, missing_names, report):
        dispatch = {
            "ToFind":         lambda h: inject_to_find_system(cls._strip(h, "ToFind"), ctx.to_find_targets),
            "AnswerBox":      lambda h: inject_answer_box_panel(cls._strip(h, "AnswerBox"), ctx.answer_targets),
            "Notes":          lambda h: inject_notes_system(cls._strip(h, "Notes")),
            "Controls":       lambda h: inject_controls_bar(cls._strip(h, "Controls")),
            "PreviousStep":   lambda h: inject_previous_step_button(cls._strip(h, "PreviousStep")),
            "Glossary":       lambda h: inject_glossary_panel(cls._strip(h, "Glossary"), ctx.glossary_terms),
            "Navigation":     lambda h: inject_nav_patch_and_scene_desc(cls._strip(h, "Navigation")),
            "StepController": lambda h: inject_step_controller(cls._strip(h, "StepController")),
            "Scene6":         lambda h: inject_scene6_big_idea(cls._strip(h, "Scene6"), ctx.gemini_sol, ctx.scene_script),
            "Scene7":         lambda h: inject_scene7_how_we_solve_it(cls._strip(h, "Scene7"), ctx.gemini_sol, ctx.scene_script),
            "Scene9":         lambda h: inject_scene9_final_answer(cls._strip(h, "Scene9"), ctx.gemini_sol, ctx.scene_script),
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

_SCENE_ANALYZER_SYSTEM = """You are QAnim Scene Analyzer — a world-class educational animation director and content planner.

Given a student question, produce a cinematic, step-by-step animation script in JSON format that feels like a polished interactive textbook.

════════════════════════════════════════════════════════════
ANIMATION PHILOSOPHY — CINEMATIC REVEAL, TAUGHT LIKE A CLASSROOM TEACHER
════════════════════════════════════════════════════════════
• This is the "Step-by-Step Concept Animation" phase of the lesson (it builds toward, then completes, the concept — think of it as the teacher drawing on the board piece by piece, not flipping on a finished diagram). NOTHING should appear instantly or all at once.
• THIS ANIMATION IS A CONCEPTUAL / PHYSICAL SCENE, NEVER A WORKED-SOLUTION PAGE — svg_components must always be tangible objects, fields, or phenomena the question describes (charges, field lines, force vectors, wavefronts, beams, orbits, particles, molecules, circuit elements, containers of gas, etc.), never a rendering of the formula, a "given data" list, or a substitution/calculation box. Formula text, given-data callouts, and step-by-step substitution belong ONLY to the separate Main Formula and Solution scenes that are appended automatically after this animation — if you find yourself naming a component "formula", "solution", "substitution", "given_labels", or similar, stop and replace it with an actual visual element from the problem's physical scenario instead.
• Each step is a "scene": ONE new component enters the stage with purposeful, physically correct motion — never more than one new idea per step.
• Every step must implicitly answer, in order across the sequence: What is happening? Why is it happening? What changes? What should the student observe? What can they conclude?
• Scene order = physical assembly order (ground → frame → driver → driven → measurement).
• When a new component appears, prior elements dim slightly via blur-shield (opacity 0.35–0.5) so the viewer's eye is pulled — like a spotlight — to the one active thing. Inactive parts stay visibly faded, never fully hidden, so context is never lost.
• Labels, dimension arrows, and value callouts enter AFTER their component is visible — never before. Treat each step as: reveal → (implicit pause) → explain (description) → highlight (focus_component + blur_background) → the next step continues.
• The final step is the "answer reveal" / Concept Completion: the mechanism freezes at the exact solution state; a clean annotation layer shows the computed result. This concludes the concept phase before Main Formula and Solution take over.
• CRITICAL — DO NOT SOLVE THE PROBLEM IN THE LAST STEP: the last step's "description" and "badges" must NOT state the governing formula, walk through substitution, or restate the numeric derivation — say only that the system has reached its solved state (e.g. "The system settles here, with every quantity in place."). A dedicated Main Formula scene and a dedicated step-by-step Solution scene are appended automatically right after this animation ends; if the last step already explains the formula and the calculation, that same explanation will then be shown two more times back-to-back, which is a defect, not a feature. Save all formula/derivation content for those two scenes.
• Motion must reflect real physics — a crank rotates continuously, a piston oscillates with sin/cos kinematics, gears mesh at correct speed ratios, belt traces its path, heat-flow pulses along the pipe.
• Every step description is written like a great professor thinking aloud: conversational, precise, one "aha moment" per step — never a wall of information.

════════════════════════════════════════════════════════════
VISUAL DESIGN INTENT (for the AnimationBuilder to follow)
════════════════════════════════════════════════════════════
• Light, airy canvas: soft blue-white radial gradient (#f0f5ff → #dce8f5 → #c8d8ed).
• Structural parts: multi-stop metallic gradients in blue-grey (#e8f0fa → #b8cce0 → #6a8aaa).
• Accent hierarchy: cyan #0891b2 (primary highlights), orange #ea8c00 (forces/motion), green #16a34a (results/measurements).
• Drop-shadow filters on every major group (feDropShadow, stdDeviation 4–6).
• Glow pulse on newly-revealed components (feGaussianBlur glow, cyan hue).
• Stroke hierarchy: frame=2.5px, components=3px, dimension lines=1.5px (dashed), annotation arrows=1.5px.
• Text: main labels #1e293b bold, secondary #475569, value callouts in rounded rect chips with accent fill.

════════════════════════════════════════════════════════════
OUTPUT FORMAT — Return ONLY valid JSON, no markdown, no preamble
════════════════════════════════════════════════════════════
{
  "title": "Concise descriptive title (max 60 chars)",
  "topic": "PHYSICS|MATH|CHEMISTRY|ENGINEERING|BIOLOGY|ABSTRACT",
  "solution_steps": ["Step 1: ...", "Step 2: ...", "Step 3: ..."],
  "final_answer": "Complete computed answer with all numerical values and units",
  "key_insight": "One memorable, plain-English insight sentence",
  "steps": [
    {
      "step_number": 1,
      "label": "3–5 word pill label for the step dot",
      "title": "Step 1: Full descriptive title (max 55 chars)",
      "description": "2–3 sentences. Conversational, like a professor thinking aloud. State what we see, what it means, what comes next.",
      "badges": [{"text": "symbol = value unit", "type": "cyan|orange|green"}],
      "components_visible": ["comp_id_1"],
      "components_new": ["comp_id_1"],
      "focus_component": "comp_id_1",
      "blur_background": true,
      "motion_emphasis": "Short phrase describing how this component moves when revealed, e.g. 'crank sweeps 360° at 300 RPM'"
    }
  ],
  "svg_components": {
    "comp_id": {
      "description": "Precise SVG visual description: shape, fill, stroke, position in 850×478 coordinate space",
      "motion_type": "rotate|translate|oscillate|trace|pulse|flow|static",
      "motion_description": "Exact kinematic description, e.g. rotates around pivot (425,239), driven by θ(t)=ωt where ω=300RPM×2π/60",
      "accent_color": "#0891b2",
      "layer_order": 1,
      "labels": ["primary label", "value label"]
    }
  }
}

════════════════════════════════════════════════════════════
STRICT RULES — 9-STEP WORKFLOW (6 SVG + 3 MODAL)
════════════════════════════════════════════════════════════
THE OVERALL STRUCTURE IS ALWAYS EXACTLY 9 STEPS:
  • Steps 1–6: SVG animation steps (what you output here — exactly 6 steps).
  • Step 7: Formula modal (auto-injected — do NOT include in your JSON).
  • Step 8: Substitution modal (auto-injected — do NOT include in your JSON).
  • Step 9: Final Answer modal (auto-injected — do NOT include in your JSON).

YOUR JSON MUST HAVE EXACTLY 6 STEPS (steps array length = 6).

STEP ASSIGNMENT FOR THE 6 SVG STEPS:
  Step 1: Establish the physical environment — the fixed frame, ground, enclosure, body of fluid, or reference space. Show the domain visually with its boundaries. No given values yet — just the scene.
  Step 2: Reveal the primary object, heat/energy source, or driver — the thing that sets the physics in motion. Show it appearing with its most important given parameter as a badge overlay.
  Step 3: Reveal the first material layer, medium, or adjacent surface — e.g. a wall layer, a fluid zone, the object surface, a wire cross-section. Label its key property (thickness, conductivity, resistance, etc.).
  Step 4: Reveal additional layers, boundaries, or components — a second material layer, convective film, adjacent body, or boundary condition that adds to the system. Give its given parameter.
  Step 5: Reveal the ambient / far-field environment and the direction of energy/heat/current flow. Show flow arrows. Display all remaining given parameters as overlays accumulating on the SVG.
  Step 6: Full system summary step — ALL SVG layers visible simultaneously. Show the temperature/energy profile curve (if applicable). Display a summary "Given Data" card AND a "To Find" card on the SVG. This is setup-complete. Do NOT state any formula or calculation here — those come in Steps 7–9.

BADGE TYPES: "cyan" = given data, "orange" = motion/force/flow, "green" = derived/result.
svg_components: every component is a concrete visual object or phenomenon (frame, layer, fluid, arrows, profile curve, particles, field lines, etc.) — NEVER a formula string, a label block, or a calculation/substitution box. Place all in 850×478 space.
final_answer: MUST contain the computed numerical result with units. Never empty.
solution_steps: flat list of 3–8 plain-English calculation steps used to solve (these feed the Step 8 substitution modal).
motion_type: accurately match physical behavior (rotate|translate|oscillate|trace|pulse|flow|static).
layer_order: integer starting at 1 (lower = drawn first / behind).
Each step's "components_visible" lists ALL components visible at that step; "components_new" lists only newly revealed ones."""

_SCENE_ANALYZER_USER = """Analyse this question and produce the animation scene script:

QUESTION: {question}

Remember:
- You MUST produce EXACTLY 6 steps in the "steps" array (Steps 1–6 of the 9-step workflow).
- Steps 7, 8, 9 are the Formula / Substitution / Final Answer modals — they are auto-injected. Do NOT include them.
- Step 1: physical environment (frame, domain). Steps 2–5: reveal each given parameter/layer one at a time. Step 6: full summary with "Given Data" + "To Find" overlays and energy profile.
- Each step reveals exactly ONE new SVG component with its label/badge.
- Step 6 is the setup-complete step — show all given data but NO formula or calculation.
- Compute the actual numerical answer and include it in final_answer.
- Include solution_steps as 3–8 plain-English calculation steps (feeds Step 8 substitution modal).

Return ONLY valid JSON."""


class GeminiSceneAnalyzer:
    """Stage A: Analyses the question and produces a structured scene script."""

    @classmethod
    def analyze(cls, question: str) -> dict:
        if _gemini_client is None:
            return cls._fallback_script(question)

        QAnimLogger.info("SceneAnalyzer", f"Analysing question via {GEMINI_MODEL}...")
        # Use .replace() instead of .format() — question text may contain
        # literal { } (e.g. set notation, LaTeX) that .format() misinterprets.
        user_prompt = _SCENE_ANALYZER_USER.replace("{question}", question[:1200])

        # Falling straight to the generic 5-step "Setup/Given/Formula/
        # Substitute/Solution" fallback on the FIRST parse failure meant a
        # single truncated or malformed JSON response (which _call_gemini's
        # own retry logic doesn't catch, since it only retries API-level
        # 429/503 errors, not a 200 response containing bad JSON) silently
        # produced the generic template with no second attempt. Give the
        # actual model 2 more tries at a clean JSON response before
        # accepting the fallback.
        last_err = None
        last_raw_snippet = ""
        for attempt in range(1, 4):
            try:
                raw = GeminiSolutionGenerator._call_gemini(
                    user_prompt, _SCENE_ANALYZER_SYSTEM, max_tokens=16384
                )
                last_raw_snippet = raw[:400]
                cleaned = _sanitize_json_str(raw)
                data = json.loads(cleaned)
                QAnimLogger.ok("SceneAnalyzer", f"Scene script produced: {len(data.get('steps',[]))} steps, {len(data.get('svg_components',{}))} components")
                return data
            except Exception as e:
                last_err = e
                QAnimLogger.warn("SceneAnalyzer", f"Attempt {attempt}/3 failed: {e}")
                continue

        QAnimLogger.warn(
            "SceneAnalyzer",
            f"All attempts failed ({last_err}) — using fallback script. "
            f"Last raw response started with: {last_raw_snippet!r}"
        )
        return cls._fallback_script(question)

    @classmethod
    async def analyze_async(cls, question: str) -> dict:
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, cls.analyze, question),
                timeout=STAGE_TIMEOUT_SCENE,
            )
        except asyncio.TimeoutError:
            QAnimLogger.error("SceneAnalyzer", f"Stage exceeded {STAGE_TIMEOUT_SCENE}s — using fallback script")
            return cls._fallback_script(question)

    @classmethod
    def _fallback_script(cls, question: str) -> dict:
        q_short = question[:80]
        return {
            "title": f"Analysis: {q_short}",
            "topic": "ENGINEERING",
            "solution_steps": [
                "Step 1: Identify the given values from the question.",
                "Step 2: Define the unknown quantity clearly.",
                "Step 3: Select the governing formula or principle.",
                "Step 4: Substitute the known values into the formula.",
                "Step 5: Simplify and compute the result step by step.",
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
                    "label": "Given",
                    "title": "Step 2: List Given Values",
                    "description": "Write down every known quantity with its unit. This prevents errors later.",
                    "badges": [{"text": "Data extracted", "type": "cyan"}],
                    "components_visible": ["frame", "given_labels"],
                    "components_new": ["given_labels"],
                    "focus_component": "given_labels",
                    "blur_background": True
                },
                {
                    "step_number": 3,
                    "label": "Formula",
                    "title": "Step 3: Select the Formula",
                    "description": "Choose the governing law or equation that connects the given quantities to the unknown.",
                    "badges": [{"text": "Governing law", "type": "orange"}],
                    "components_visible": ["frame", "given_labels", "formula_box"],
                    "components_new": ["formula_box"],
                    "focus_component": "formula_box",
                    "blur_background": True
                },
                {
                    "step_number": 4,
                    "label": "Substitute",
                    "title": "Step 4: Substitute Values",
                    "description": "Replace each variable with its numerical value and unit. Keep the equation balanced.",
                    "badges": [{"text": "Values plugged in", "type": "orange"}],
                    "components_visible": ["frame", "given_labels", "formula_box", "substitution"],
                    "components_new": ["substitution"],
                    "focus_component": "substitution",
                    "blur_background": True
                },
                {
                    "step_number": 5,
                    "label": "Solution",
                    "title": "Step 5: Solve & Verify",
                    "description": "Carry out the arithmetic. Check units and order of magnitude for the result.",
                    "badges": [{"text": "Result computed", "type": "green"}],
                    "components_visible": ["frame", "given_labels", "formula_box", "substitution", "solution"],
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

_ANIMATION_BUILDER_SYSTEM = """You are QAnim Animation Builder v1.0 — a world-class specialist who generates COMPLETE, SELF-CONTAINED, VISUALLY POLISHED HTML animation pages for engineering and science education.

You receive a scene script (JSON) and must produce a premium interactive animation that feels like a professional educational platform (think Khan Academy × Brilliant × a high-end engineering textbook).

════════════════════════════════════════════════════════════
DESIGN PRINCIPLES
════════════════════════════════════════════════════════════
1. LIGHT, AIRY PALETTE — Never dark backgrounds. The page feels open and breathable.
   Body: #eef2f9 (soft blue-grey). Dashboard card: #ffffff. Canvas: radial gradient #f0f5ff→#dce8f5→#c8d8ed.
2. CLEAR VISUAL HIERARCHY — Every element has a purpose. No clutter.
   Question banner → SVG canvas → control panel. No floating noise.
3. SMOOTH, PURPOSEFUL MOTION — Every animation is physically correct and aesthetically satisfying.
   Transitions use cubic-bezier(0.4, 0, 0.2, 1). Layer reveals use opacity + slight translateY (0→natural).
4. LAYERED SVG DEPTH — Components exist in z-order. Shadows, gradients, and glow filters create depth.
5. POLISHED TYPOGRAPHY — Font stack: 'Segoe UI', system-ui, -apple-system. Consistent sizing scale.

════════════════════════════════════════════════════════════
REFERENCE OUTPUT STYLE (follow precisely)
════════════════════════════════════════════════════════════
The output must match this structure and CSS (light theme, visually rich):

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
      --text-main: #1e293b;
      --text-sub: #64748b;
      --text-muted: #94a3b8;
      --accent-cyan: #0891b2;
      --accent-cyan-dim: #0e7490;
      --accent-cyan-light: rgba(8,145,178,0.10);
      --accent-orange: #d97706;
      --accent-green: #16a34a;
      --border: #e2e8f0;
      --border-strong: #cbd5e1;
      --border-radius: 16px;
      --border-radius-sm: 10px;
      --shadow-card: 0 1px 3px rgba(15,23,42,0.06), 0 8px 24px rgba(15,23,42,0.08), 0 24px 48px rgba(15,23,42,0.04);
      --shadow-hover: 0 4px 16px rgba(8,145,178,0.18);
      --transition-smooth: 0.45s cubic-bezier(0.4, 0, 0.2, 1);
      --transition-spring: 0.35s cubic-bezier(0.34, 1.56, 0.64, 1);
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Segoe UI', system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      background: linear-gradient(160deg, #eef2f9 0%, #e8f0fe 50%, #eff6ff 100%);
      background-attachment: fixed;
      color: var(--text-main);
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: flex-start;
      min-height: 100vh;
      padding: 28px 16px 130px;
    }
    /* ── Page header (above dashboard) ── */
    .page-header {
      width: 100%;
      max-width: 900px;
      margin-bottom: 14px;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .page-chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 12px;
      border-radius: 20px;
      background: rgba(8,145,178,0.10);
      border: 1px solid rgba(8,145,178,0.22);
      font-size: 11px;
      font-weight: 700;
      color: var(--accent-cyan-dim);
      text-transform: uppercase;
      letter-spacing: 0.8px;
    }
    .page-chip::before { content: '▶'; font-size: 8px; }
    /* ── Dashboard Card ── */
    .dashboard {
      width: 100%;
      max-width: 900px;
      margin: 0 auto;
      background: var(--panel-bg);
      border-radius: var(--border-radius);
      box-shadow: var(--shadow-card);
      overflow: hidden;
      border: 1px solid var(--border);
      position: relative;
    }
    /* Subtle top accent line */
    .dashboard::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 3px;
      background: linear-gradient(90deg, var(--accent-cyan-dim) 0%, #7c3aed 50%, var(--accent-orange) 100%);
      border-radius: var(--border-radius) var(--border-radius) 0 0;
      z-index: 2;
    }
    /* ── Question Banner ── */
    .question-banner {
      padding: 22px 28px 18px;
      background: linear-gradient(135deg, #f8faff 0%, #f0f5ff 40%, #eef2f9 100%);
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 8px;
      position: relative;
      overflow: hidden;
    }
    .question-banner::before {
      content: '';
      position: absolute;
      inset: 0;
      background: linear-gradient(100deg, rgba(8,145,178,0.05) 0%, transparent 55%);
      pointer-events: none;
    }
    /* ── Question Banner Inner ── */
    .q-label {
      font-size: 10.5px;
      font-weight: 800;
      color: var(--accent-cyan-dim);
      text-transform: uppercase;
      letter-spacing: 1.8px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .q-label::before {
      content: '';
      display: inline-block;
      width: 16px; height: 16px;
      border-radius: 5px;
      background: linear-gradient(135deg, var(--accent-cyan-dim), var(--accent-cyan));
      flex-shrink: 0;
    }
    .q-text {
      font-size: 15px;
      color: var(--text-main);
      line-height: 1.6;
      font-weight: 450;
      max-width: 820px;
    }
    /* ── SVG Canvas ── */
    .svg-container {
      width: 100%;
      aspect-ratio: 16 / 9;
      background: radial-gradient(ellipse at 35% 38%, #eef5ff 0%, #dce8f5 45%, #c8d9ed 85%, #b8ccdf 100%);
      position: relative;
      overflow: hidden;
      border-bottom: 1px solid var(--border);
    }
    svg { display: block; width: 100%; height: 100%; }
    /* Smooth, physically-weighted layer transitions */
    .svg-layer {
      transition: opacity 0.55s cubic-bezier(0.4, 0, 0.2, 1);
    }
    /* ── Control Panel ── */
    .control-panel {
      padding: 22px 28px 26px;
      background: linear-gradient(180deg, #ffffff 0%, #f9fbff 100%);
      border-top: 1px solid var(--border);
    }
    /* ── Step Indicator: pill-style dots ── */
    .step-indicator {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 20px;
      flex-wrap: wrap;
    }
    /* Connector line between dots */
    .step-connector {
      flex: 0 0 18px;
      height: 1.5px;
      background: linear-gradient(90deg, #cbd5e1, #e2e8f0);
      border-radius: 2px;
    }
    .step-dot {
      padding: 6px 14px;
      border-radius: 20px;
      background: #f1f5f9;
      border: 1.5px solid #e2e8f0;
      font-size: 11.5px;
      font-weight: 700;
      color: #94a3b8;
      cursor: pointer;
      transition: background 0.3s ease, color 0.3s ease, border-color 0.3s ease,
                  box-shadow 0.3s ease, transform 0.25s cubic-bezier(0.34,1.56,0.64,1);
      white-space: nowrap;
      user-select: none;
      position: relative;
    }
    .step-dot:hover:not(.active) {
      background: rgba(8,145,178,0.07);
      border-color: rgba(8,145,178,0.3);
      color: var(--accent-cyan-dim);
    }
    .step-dot.active {
      background: linear-gradient(135deg, #0e7490 0%, #0891b2 100%);
      border-color: transparent;
      color: #ffffff;
      box-shadow: 0 3px 12px rgba(8,145,178,0.38), 0 1px 3px rgba(8,145,178,0.20);
      transform: scale(1.07);
    }
    /* Completed step indicator */
    .step-dot.done {
      background: rgba(22,163,74,0.09);
      border-color: rgba(22,163,74,0.28);
      color: #15803d;
    }
    .step-label {
      font-size: 11px;
      color: var(--text-muted);
      font-weight: 600;
      letter-spacing: 0.6px;
      text-transform: uppercase;
      margin-left: 6px;
      flex: 1;
      min-width: 0;
    }
    /* ── Info Box ── */
    .info-box {
      background: linear-gradient(135deg, #f8fbff 0%, #f4f8ff 100%);
      border: 1px solid #dde8f8;
      border-left: 4px solid var(--accent-cyan);
      border-radius: var(--border-radius-sm);
      padding: 20px 22px;
      min-height: 130px;
      display: flex;
      flex-direction: column;
      gap: 11px;
      position: relative;
      overflow: hidden;
    }
    .info-box::before {
      content: '';
      position: absolute;
      top: 0; right: 0;
      width: 120px; height: 120px;
      background: radial-gradient(circle, rgba(8,145,178,0.06) 0%, transparent 70%);
      pointer-events: none;
    }
    .info-box h3 {
      color: var(--text-main);
      font-size: 16.5px;
      font-weight: 800;
      display: flex;
      align-items: center;
      gap: 10px;
      line-height: 1.3;
      letter-spacing: -0.2px;
    }
    .info-box h3::before {
      content: '';
      display: inline-block;
      width: 8px; height: 8px;
      border-radius: 50%;
      background: var(--accent-cyan);
      flex-shrink: 0;
      box-shadow: 0 0 0 3px rgba(8,145,178,0.18);
    }
    /* ── Badges ── */
    .badges { display: flex; gap: 7px; flex-wrap: wrap; align-items: center; }
    .badge {
      padding: 4px 12px;
      border-radius: 20px;
      font-size: 11.5px;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      letter-spacing: 0.1px;
    }
    .badge-cyan  { background: rgba(8,145,178,0.09);  border: 1px solid rgba(8,145,178,0.28);  color: #0e7490; }
    .badge-orange{ background: rgba(217,119,6,0.09);  border: 1px solid rgba(217,119,6,0.28);  color: #92400e; }
    .badge-green { background: rgba(22,163,74,0.09);  border: 1px solid rgba(22,163,74,0.28);  color: #15803d; }
    /* ── Description ── */
    .info-desc {
      font-size: 14px;
      line-height: 1.7;
      color: var(--text-sub);
      font-weight: 400;
    }
    /* ── Step progress bar ── */
    .step-progress-wrap {
      height: 3px;
      background: #f1f5f9;
      border-radius: 2px;
      margin-bottom: 20px;
      overflow: hidden;
    }
    .step-progress-bar {
      height: 100%;
      background: linear-gradient(90deg, #0e7490, #0891b2, #38bdf8);
      border-radius: 2px;
      transition: width 0.5s cubic-bezier(0.4, 0, 0.2, 1);
    }
    /* ── Actions ── */
    .actions {
      display: flex;
      justify-content: flex-end;
      align-items: center;
      gap: 10px;
      margin-top: 20px;
    }
    button {
      padding: 11px 24px;
      border-radius: 10px;
      font-size: 13.5px;
      font-weight: 700;
      font-family: inherit;
      cursor: pointer;
      transition: background 0.22s ease, box-shadow 0.22s ease,
                  transform 0.18s cubic-bezier(0.34,1.56,0.64,1),
                  color 0.2s ease, border-color 0.2s ease;
      border: none;
      outline: none;
      letter-spacing: 0.1px;
    }
    .btn-primary {
      background: linear-gradient(135deg, #0e7490 0%, #0891b2 100%);
      color: #ffffff;
      box-shadow: 0 4px 14px rgba(8,145,178,0.30), 0 1px 3px rgba(8,145,178,0.15);
    }
    .btn-primary:hover {
      background: linear-gradient(135deg, #0c6680 0%, #0e7490 100%);
      box-shadow: 0 6px 22px rgba(8,145,178,0.38);
      transform: translateY(-2px);
    }
    .btn-primary:active { transform: translateY(0); box-shadow: 0 2px 6px rgba(8,145,178,0.20); }
    .btn-secondary {
      background: #ffffff;
      color: var(--text-sub);
      border: 1.5px solid var(--border-strong);
      box-shadow: 0 1px 3px rgba(15,23,42,0.06);
    }
    .btn-secondary:hover {
      background: #f8fafc;
      color: var(--text-main);
      border-color: #94a3b8;
      box-shadow: 0 2px 8px rgba(15,23,42,0.10);
      transform: translateY(-1px);
    }
    .btn-secondary:active { transform: translateY(0); }
  </style>
</head>
<body>
  <!-- Page header chip -->
  <div class="page-header">
    <div class="page-chip">Interactive Animation</div>
  </div>
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

════════════════════════════════════════════════════════════
SVG DESIGN RULES — HIGH-QUALITY LAYERED SVG
════════════════════════════════════════════════════════════
1. viewBox="0 0 850 478" (16:9). preserveAspectRatio="xMidYMid slice".
2. Canvas background: <rect> with fill="url(#canvasBg)" using a radial gradient:
   center light (#eef5ff), midpoint (#dce8f5), edge (#c8d9ed). Add subtle noise via feTurbulence.
3. Grid pattern: id="grid", 40×40 units, stroke "#1e3a5f" opacity 0.04, strokeWidth 0.6.
4. DEFS section must include ALL of:
   a) Metallic gradient "steel": #e8f0fa → #c8d8e8 → #8aaac0 → #5a7a9a (4-stop, 135°)
   b) Highlight gradient "steelHi": #f4f8fc → #d4e4f0 → #94b4c8 (lighter, for top-facing surfaces)
   c) Accent glow filter "glowCyan": feGaussianBlur stdDeviation="6", feComposite over source
   d) Accent glow filter "glowOrange": same, orange-tinted flood for force arrows
   e) Drop shadow "shadow": feDropShadow dx=0 dy=3 stdDeviation=5, flood-color rgba(14,30,64,0.16)
   f) Deep shadow "shadowDeep": feDropShadow dx=0 dy=6 stdDeviation=10, flood-color rgba(14,30,64,0.22)
   g) Inner glow "innerGlow": feGaussianBlur in="SourceAlpha", feOffset, feComposite
   h) Arrow marker "arrowCyan": fill #0891b2, markerWidth=8 markerHeight=8 refX=4 refY=4
   i) Arrow marker "arrowOrange": fill #d97706
   j) Arrow marker "arrowGreen": fill #16a34a
   k) Arrow marker "arrowGrey": fill #94a3b8 (for dimension lines)
5. LAYER STRUCTURE (strict order, top-to-bottom in source = back-to-front visually):
   <g id="layer-canvas-bg">  — background rect + grid (always visible, opacity:1)
   <rect id="blur-shield" …>  — dimming overlay between bg and components
   <g class="svg-layer" id="layer-frame" …>   — fixed structure, always visible
   <g class="svg-layer" id="layer-[comp1]" style="opacity:0"> — components in reveal order
   <g class="svg-layer" id="layer-[comp2]" style="opacity:0">
   …
   <g class="svg-layer" id="overlay-step0" style="opacity:0"> — labels/arrows for step 0
   <g class="svg-layer" id="overlay-step1" style="opacity:0"> — labels/arrows for step 1
   …
6. blur-shield: <rect id="blur-shield" width="100%" height="100%" fill="#c2d4e8" opacity="0" pointer-events="none"/>
   Opacity range: 0 (no focus) → 0.38 (focused step) → 0 (final reveal step).
7. STRUCTURAL COMPONENTS — use metallic fill="url(#steel)", stroke layering, filter="url(#shadow)":
   - Frames/housings: rounded rect or path, fill="url(#steel)", stroke="#6a8aaa" strokeWidth=2.5
   - Ground hatching: diagonal lines pattern, classic engineering style
   - Pivots/bearings: concentric circles with metallic gradient, inner circle lighter
8. MOVING COMPONENTS — each must have a distinct visual personality:
   - Cranks: thick rounded bar, fill="url(#steel)", with pivot circle at both ends
   - Connecting rods: tapered shape (wider at crank end, narrower at piston end)
   - Pistons: rectangular with rounded ends, fill="url(#steelHi)", subtle chamfer lines
   - Gears: proper involute-like teeth (use path or polygon approximation), fill="url(#steel)"
   - Pulleys: circles with spoke detail, belt grooves visible
   - Belts: thick stroke path, stroke="#334155", slightly textured with dash patterns
   - Springs: zigzag path, stroke="#475569", strokeWidth=2.5
   - Heat pipes: concentric circles or annular ring, fill gradient from hot to cool colors
9. TEXT IN SVG — strict hierarchy:
   - Component name labels: fontSize=13, fontWeight=800, fill="#1e293b", fontFamily="Segoe UI,system-ui,sans-serif"
   - Value callout chips: <rect rx=5 fill="rgba(8,145,178,0.12)" stroke="rgba(8,145,178,0.25)"/> + <text> centered
   - Dimension lines: stroke="#94a3b8", strokeWidth=1.5, strokeDasharray="5,3", with arrowGrey markers
   - Annotation arrows (forces, velocities): stroke="#0891b2" or "#d97706", strokeWidth=2.5, with arrowCyan/Orange
   - Secondary labels: fontSize=11, fill="#475569"
10. ZERO text overlaps — plan all label positions. Every label must be ≥12px from any other element.
11. Light-theme colors for components (NO dark/neon colors):
    Primary structure: #4a6a8a, #6a8aaa, #8aaac4
    Crank/driver: #2563eb (vibrant blue), stroke #1d4ed8
    Driven: #0891b2 (cyan), stroke #0e7490
    Forces/motion arrows: #d97706 (amber), stroke #b45309
    Results/measurements: #16a34a (green), stroke #15803d
    Danger/highlight: #dc2626 (red)

════════════════════════════════════════════════════════════
9-STEP WORKFLOW — MANDATORY STRUCTURE
════════════════════════════════════════════════════════════
EVERY animation MUST follow this exact 9-step structure:

  Steps 1–6: SVG animation steps (driven by stepsData array, inline in the page).
  Step 7:    "Formula" modal (injected automatically — you MUST add the dot but the modal content is injected separately).
  Step 8:    "Substitution" modal (injected automatically — add the dot only).
  Step 9:    "Final Answer" modal (injected automatically — add the dot only).

THE STEP DOT BAR MUST ALWAYS SHOW ALL 9 DOTS:
  <div class="step-dot active" onclick="goToStep(0)">1 · [Step1Label]</div>
  <div class="step-connector"></div>
  <div class="step-dot" onclick="goToStep(1)">2 · [Step2Label]</div>
  <div class="step-connector"></div>
  <div class="step-dot" onclick="goToStep(2)">3 · [Step3Label]</div>
  <div class="step-connector"></div>
  <div class="step-dot" onclick="goToStep(3)">4 · [Step4Label]</div>
  <div class="step-connector"></div>
  <div class="step-dot" onclick="goToStep(4)">5 · [Step5Label]</div>
  <div class="step-connector"></div>
  <div class="step-dot" onclick="goToStep(5)">6 · [Step6Label]</div>
  <div class="step-connector"></div>
  <div class="step-dot" id="dot-step7" onclick="if(typeof qanim_showScene6==='function')qanim_showScene6()">7 · Formula</div>
  <div class="step-connector"></div>
  <div class="step-dot" id="dot-step8" onclick="if(typeof qanim_showScene7==='function')qanim_showScene7()">8 · Substitution</div>
  <div class="step-connector"></div>
  <div class="step-dot" id="dot-step9" onclick="if(typeof qanim_showScene9==='function')qanim_showScene9()">9 · Final Answer</div>
  <div class="step-label" id="step-label">Step 1 of 9</div>

STEP LABEL: always display "Step N of 9" (total is always 9).
PROGRESS BAR: width = (currentStep+1)/9 * 100% for steps 1–6.

NEXTSTE BUTTON BEHAVIOR:
  - On steps 0–4: advance to next SVG step (goToStep(idx+1)).
  - On step 5 (last SVG step): clicking "Next Step ▶" should call qanim_showScene6() (opens Step 7 Formula modal).
    Use: onclick="if(currentStep===5){if(typeof qanim_showScene6==='function')qanim_showScene6();}else{goToStep(currentStep+1);}"
  - nextStep() function should also handle this: if(currentStep>=5){if(typeof qanim_showScene6==='function')qanim_showScene6();return;}

STEP COLORS — each of the 6 SVG steps has a distinct accent color applied to its dot and control panel:
  Step 0 (1): #0ea5e9 (sky blue)
  Step 1 (2): #10b981 (emerald)
  Step 2 (3): #f59e0b (amber)
  Step 3 (4): #6366f1 (indigo)
  Step 4 (5): #f43f5e (rose)
  Step 5 (6): #22c55e (green)

Apply step color to the active dot's border-left (3px solid [color]) and to the control panel background.

════════════════════════════════════════════════════════════
ANIMATION RULES — REAL PHYSICS
════════════════════════════════════════════════════════════
Motion must be physically correct, not just decorative:
- Rotating cranks/gears/pulleys: continuous requestAnimationFrame, angle=ω×t (real rpm)
- Oscillating pistons/sliders: x=r×cos(θ)+√(l²-r²×sin²(θ)) (actual kinematic formula)
- Gear trains: each gear's ω scaled by tooth ratio (ω₂/ω₁ = T₁/T₂)
- Belt drives: pulley animations synchronized, belt path traces smoothly
- Springs: translateY(amplitude×sin(ωt)) with correct stiffness-derived frequency
- Heat flow / current flow: animated stroke-dashoffset on the path
- Waveforms / signals: path d attribute updated each frame with sin/cos
- Freezing mechanism: lerp angle toward solution angle over ~60 frames, then pause RAF

stepsData schema (one object per step, drives ALL state):
  {
    label: "3-5 word pill text",
    blurOp: 0.0,                  // blur-shield opacity (0 = off, 0.38 = focus)
    overlays: ["overlay-step0"],  // which overlay layers become visible
    freezing: false,              // true = lerp to solution angle and pause
    solutionAngle: null,          // target angle in radians for freeze step
    title: "Step N: Full Title",
    badges: '<span class="badge badge-cyan">r = 50 mm</span>',
    desc: "Conversational 2-3 sentence description.",
    layerOpacities: {             // explicit opacity for EVERY layer
      "layer-frame": 1,
      "layer-crank": 0,
      ...
    }
  }

applyStep(idx) must:
  1. Set blur-shield opacity from stepsData[idx].blurOp
  2. Apply all layerOpacities (every layer, not just new ones)
  3. Hide all overlays, then show only stepsData[idx].overlays
  4. Update info-title, info-badges, info-desc
  5. Update step dots (active/done classes)
  6. Update step-label text ("Step N of M")
  7. Update progress bar width (idx+1)/total × 100%
  8. DIRECTLY set every moving component's position/rotation/dashoffset for
     THIS step — call the exact same drawing/positioning function the RAF
     loop uses (e.g. drawFrame(angleForStep(idx))), passing the angle/time
     value that is correct for stepsData[idx]. Do this unconditionally,
     every call, regardless of whether the RAF loop is currently running,
     paused, or was never started. This is not optional: applyStep(idx)
     must be able to render ANY step correctly all on its own, with the
     RAF loop fully stopped — because the Previous Step button, and the
     "Back to Animation" button on the Main Formula / Solution overlays,
     call applyStep() directly and expect a fully correct frame with no
     RAF loop involved. If a moving part's position only ever gets set
     inside the RAF callback, that part will be frozen/misplaced/invisible
     the instant a user navigates backward past a freezing step — this is
     a defect, not acceptable behavior.
  9. If idx is NOT a freezing step: ensure the RAF loop is running (start
     it if it was paused/never started). If idx IS a freezing step: run
     the angle-lerp-to-solution then pause the RAF loop as before.
     The loop must resume automatically the moment the user leaves the
     freezing step in either direction — never leave it paused on a
     non-freezing step.

REQUIRED GLOBAL NAMING CONTRACT for the RAF loop (exact names, no
substitutes — the page's fixed control-panel script calls these by name
when the Main Formula / Solution overlays hand control back to the
animation, and Previous Step calls them too):
  window.qanimRafId     — current requestAnimationFrame handle, or null
                           when the loop is not running. Set/clear this
                           EVERY time you call/cancel requestAnimationFrame
                           — never keep the id in a local/closure variable
                           only.
  window.qanimStartRAF  — a function, callable with no arguments at any
                           time, that (re)starts the continuous loop from
                           wherever state currently is (does not reset
                           angle/time). Must be safe to call even if the
                           loop is already running (no-op / idempotent).

════════════════════════════════════════════════════════════
CRITICAL CODE REQUIREMENTS
════════════════════════════════════════════════════════════
- NO backtick template literals — use string concatenation only
- NO const/let — use var everywhere
- NO arrow functions — use function() {} only
- NO external scripts or CDN imports — fully self-contained
- ALL JavaScript in one <script> block
- requestAnimationFrame loop keeps running unless explicitly paused (freeze step)
- The loop's id MUST live in window.qanimRafId and its restart function
  MUST be window.qanimStartRAF, per the naming contract above — Previous
  Step and the Back to Animation buttons rely on these exact names to
  resume motion; if they're missing, navigating backward from a frozen
  step silently leaves every moving part stuck in its frozen position.
- Restart button resets angle, currentStep=0, resumes RAF via
  window.qanimStartRAF(), applies step 0
- NEVER put a raw apostrophe/single-quote character inside a single-quoted
  JS string. ONE unescaped apostrophe silently breaks the ENTIRE <script>
  block it's in — every function in that block (including nextStep and
  window.onload) stops being defined, with no visible error on the page.
  This happens constantly with prime notation (l', θ', i', v') and words
  like "it's"/"cell's". Always write the HTML entity &#39; instead:
    WRONG: badges: '<span class="badge">l' = 32 cm</span>',
    RIGHT: badges: '<span class="badge">l&#39; = 32 cm</span>',
  This applies to every string field: title, badges, desc, jockeyLabel,
  glossary terms/meanings, and answer target labels — anywhere user-facing
  text is embedded inside a single-quoted JS string.

════════════════════════════════════════════════════════════
POLISH CHECKLIST (every output must pass)
════════════════════════════════════════════════════════════
✓ Page has page-header chip above dashboard
✓ Dashboard has ::before top accent gradient bar (3px)
✓ Question banner has q-label with square icon + q-text at 15px
✓ SVG canvas: all 4 gradient defs, shadow filters, glow filters, arrow markers
✓ blur-shield rect present between background and component layers
✓ Step dots are pills with text, connected by .step-connector divs
✓ Progress bar (.step-progress-wrap + .step-progress-bar) updates each step
✓ Info box has border-left:4px cyan, h3::before cyan dot with ring shadow
✓ Badges have correct type: cyan=given, orange=motion, green=result
✓ All buttons use CSS variables; .btn-primary has gradient + translateY hover
✓ ZERO text-overlaps in SVG; all labels inside viewBox
✓ All component layers start opacity:0 (except layer-frame)
✓ Final step freezes mechanism at exact solution state + annotation overlay

════════════════════════════════════════════════════════════
OUTPUT
════════════════════════════════════════════════════════════
Return ONLY the complete <!DOCTYPE html>...</html> page as raw text.
No JSON wrapper. No markdown. No fences. Pure HTML only."""

_ANIMATION_BUILDER_USER = """Generate the complete, polished animation HTML page for this scene script. This is a premium educational product — quality matters at every level.

ORIGINAL QUESTION: {question}

SCENE SCRIPT:
{scene_script}

════════════════════════════════════════════════════════════
CRITICAL REMINDERS FOR THIS OUTPUT
════════════════════════════════════════════════════════════

9-STEP STRUCTURE (MANDATORY):
0. The animation ALWAYS has exactly 9 steps total. Steps 1–6 are the SVG animation (driven by stepsData). Steps 7–9 are modals that are auto-injected after you generate the HTML — include their dots in the indicator bar but do NOT build their modal content (that's handled separately).
   The step dot bar MUST show all 9 dots labeled:
     "1 · [label]", "2 · [label]", ..., "6 · [label]", "7 · Formula", "8 · Substitution", "9 · Final Answer"
   Step label text = "Step N of 9" always.
   Progress bar width = (currentStep+1)/9 * 100% for steps 0–5.
   Dots 7, 8, 9 have onclick that calls qanim_showScene6(), qanim_showScene7(), qanim_showScene9() respectively.
   The nextStep() function: if(currentStep>=5){if(typeof qanim_showScene6==='function')qanim_showScene6();return;}
   The "Next Step ▶" button on step 6 (index 5) should trigger the Step 7 Formula modal.

STRUCTURE & LAYOUT:
1. Body background: linear-gradient(160deg, #eef2f9 0%, #e8f0fe 50%, #eff6ff 100%) with background-attachment:fixed.
2. Add page-header div with page-chip "Interactive Animation" ABOVE the .dashboard card.
3. Dashboard has CSS ::before with 3px top gradient bar (cyan→purple→orange).
4. Question banner: class="question-banner" — q-label with square icon box, q-text at 15px/1.6 line-height.

SVG CANVAS:
5. All defs: 4-stop steel gradient, steelHi gradient, glowCyan filter, glowOrange filter, shadow filter, shadowDeep filter, 3 color arrow markers + 1 grey marker for dimensions.
6. blur-shield rect: fill="#c2d4e8", opacity="0", sits between layer-frame and component layers.
7. Component colors MUST be light-theme friendly: structure=#4a6a8a, driver=#2563eb, driven=#0891b2, forces=#d97706, results=#16a34a.
8. Value callout chips in overlays: rounded rect with rgba fill + centered text, NOT bare text floated in space.
9. Dimension lines: dashed (#94a3b8), with arrowGrey markers on both ends.

STEP COLORS — apply step-specific accent to dot border-left and control-panel background:
   Step 0 → #0ea5e9, Step 1 → #10b981, Step 2 → #f59e0b, Step 3 → #6366f1, Step 4 → #f43f5e, Step 5 → #22c55e.

STEP CONTROL:
10. Step dots are pills with text. Between every two dots add <div class="step-connector"></div>.
11. Add <div class="step-progress-wrap"><div class="step-progress-bar" id="step-bar"></div></div> between step-indicator and info-box.
12. applyStep(idx) must: set blur opacity, set all layer opacities, show correct overlays, update info box, update dots (active/done), update step-label "Step N of 9", update progress bar width.
13. Include a "Given Data" accumulator panel below the info-box that grows as each step reveals a new given parameter (steps 1–5 only; hidden on step 6 which shows the full summary overlay).

PHYSICS & MOTION:
14. Rotating parts (cranks, gears, pulleys): continuous RAF loop, angle=omega*elapsed_time. Use REAL RPM from the problem.
15. Oscillating parts (pistons): x = r*cos(theta) + sqrt(L*L - r*r*sin(theta)*sin(theta)). Use REAL geometry.
16. Thermal/fluid problems: animate heat flux arrows (stroke-dashoffset), temperature gradient overlays, layer reveals.
17. Each component animates on the FRAME it first becomes visible (triggered in applyStep).

CODE QUALITY:
18. NO const/let/arrow functions/backtick template literals anywhere.
19. Use var for all variables. Use function() {} for all functions.
20. Single <script> block. Zero external dependencies.
21. window.qanimRafId and window.qanimStartRAF must be present (for modal back-navigation compat).

Return the complete <!DOCTYPE html>...</html> page — nothing else."""


class GeminiAnimationBuilder:
    """Stage B: Generates complete HTML animation from the scene script."""

    @classmethod
    def build(cls, question: str, scene_script: dict) -> str:
        if _gemini_client is None:
            return RecoveryEngine.fallback_html(question, "Gemini client not available. Set GEMINI_API_KEY.")

        QAnimLogger.info("AnimationBuilder", f"Building animation HTML via {GEMINI_MODEL}...")
        script_json = json.dumps(scene_script, indent=2, ensure_ascii=False)
        # Use .replace() instead of .format() — the scene_script is JSON and
        # contains many literal { } braces that .format() would misinterpret as
        # positional/keyword placeholders, raising:
        #   "Replacement index 0 out of range for positional args tuple"
        user_prompt = (
            _ANIMATION_BUILDER_USER
            .replace("{question}", question[:500])
            .replace("{scene_script}", script_json[:6000])
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
            QAnimLogger.error("AnimationBuilder", f"Build failed: {_err_msg(e)}")
            return RecoveryEngine.fallback_html(question, f"Animation build error: {_err_msg(e)}")

    @classmethod
    def _call_gemini_large(cls, user_prompt: str) -> str:
        import time as _time
        MAX_RETRIES  = 3
        RETRY_DELAYS = [6, 12, 20]

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
                is_retryable = (
                    "429" in err_str or "TooManyRequests" in err_str or "Resource has been exhausted" in err_str
                    or "503" in err_str or "UNAVAILABLE" in err_str or "overloaded" in err_str.lower()
                    or "high demand" in err_str.lower()
                )
                if is_retryable and attempt < MAX_RETRIES:
                    reason = "429 rate limit" if "429" in err_str else "503 model overloaded"
                    QAnimLogger.warn("AnimationBuilder", f"{reason} — waiting {RETRY_DELAYS[attempt-1]}s...")
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
            blur = 0.38 if step.get("blur_background") else 0
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

        # Build pill-style dot elements with step-connector divs between them
        dot_count = len(steps)
        dot_parts = []
        for i in range(dot_count):
            dot_parts.append(
                '<div class="step-dot' + (' active' if i == 0 else '') + '">'
                + html_module.escape(steps[i].get('label', f'Step {i+1}')[:24])
                + '</div>'
            )
            if i < dot_count - 1:
                dot_parts.append('<div class="step-connector"></div>')
        dots_html = "\n                ".join(dot_parts)

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
            --bg: #eef2f9;
            --panel: #ffffff;
            --text: #1e293b;
            --text-sub: #64748b;
            --text-muted: #94a3b8;
            --accent: #0891b2;
            --accent-dim: #0e7490;
            --accent-light: rgba(8,145,178,0.10);
            --orange: #d97706;
            --green: #16a34a;
            --border: #e2e8f0;
            --border-md: #cbd5e1;
            --radius: 16px;
            --radius-sm: 10px;
            --shadow: 0 1px 3px rgba(15,23,42,0.06), 0 8px 24px rgba(15,23,42,0.08);
        }}
        *, *::before, *::after {{ box-sizing:border-box; margin:0; padding:0; }}
        body {{
            font-family: 'Segoe UI', system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(160deg, #eef2f9 0%, #e8f0fe 50%, #eff6ff 100%);
            background-attachment: fixed;
            color: var(--text);
            display: flex;
            flex-direction: column;
            align-items: center;
            min-height: 100vh;
            padding: 28px 16px 130px;
        }}
        /* ── Page header chip ── */
        .page-header {{
            width: 100%;
            max-width: 900px;
            margin-bottom: 14px;
        }}
        .page-chip {{
            display: inline-flex;
            align-items: center;
            gap: 7px;
            padding: 5px 13px;
            border-radius: 20px;
            background: rgba(8,145,178,0.10);
            border: 1px solid rgba(8,145,178,0.22);
            font-size: 11px;
            font-weight: 700;
            color: var(--accent-dim);
            text-transform: uppercase;
            letter-spacing: 0.9px;
        }}
        .page-chip::before {{ content: '▶'; font-size: 8px; }}
        /* ── Dashboard Card ── */
        .dashboard {{
            width: 100%;
            max-width: 900px;
            background: var(--panel);
            border-radius: var(--radius);
            box-shadow: var(--shadow);
            overflow: hidden;
            border: 1px solid var(--border);
            position: relative;
        }}
        .dashboard::before {{
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0;
            height: 3px;
            background: linear-gradient(90deg, var(--accent-dim) 0%, #7c3aed 50%, var(--orange) 100%);
            border-radius: var(--radius) var(--radius) 0 0;
            z-index: 2;
        }}
        /* ── Question Banner ── */
        .question-banner {{
            padding: 22px 28px 18px;
            background: linear-gradient(135deg, #f8faff 0%, #f0f5ff 45%, #eef2f9 100%);
            border-bottom: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            gap: 8px;
            position: relative;
            overflow: hidden;
        }}
        .question-banner::before {{
            content: '';
            position: absolute;
            inset: 0;
            background: linear-gradient(100deg, rgba(8,145,178,0.05) 0%, transparent 55%);
            pointer-events: none;
        }}
        .q-label {{
            font-size: 10.5px;
            font-weight: 800;
            color: var(--accent-dim);
            text-transform: uppercase;
            letter-spacing: 1.8px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .q-label::before {{
            content: '';
            display: inline-block;
            width: 16px; height: 16px;
            border-radius: 5px;
            background: linear-gradient(135deg, var(--accent-dim), var(--accent));
            flex-shrink: 0;
        }}
        .q-text {{
            font-size: 15px;
            color: var(--text);
            line-height: 1.62;
            font-weight: 430;
            max-width: 820px;
        }}
        /* ── SVG Canvas ── */
        .svg-container {{
            width: 100%;
            aspect-ratio: 16 / 9;
            background: radial-gradient(ellipse at 35% 38%, #eef5ff 0%, #dce8f5 45%, #c8d9ed 85%, #b8ccdf 100%);
            position: relative;
            overflow: hidden;
            border-bottom: 1px solid var(--border);
        }}
        svg {{ display: block; width: 100%; height: 100%; }}
        .svg-layer {{ transition: opacity 0.52s cubic-bezier(0.4, 0, 0.2, 1); }}
        /* ── Control Panel ── */
        .control-panel {{
            padding: 22px 28px 26px;
            background: linear-gradient(180deg, #fff 0%, #f9fbff 100%);
            border-top: 1px solid var(--border);
        }}
        /* ── Step Indicator ── */
        .step-indicator {{
            display: flex;
            align-items: center;
            gap: 6px;
            margin-bottom: 8px;
            flex-wrap: wrap;
        }}
        .step-connector {{
            flex: 0 0 18px;
            height: 1.5px;
            background: linear-gradient(90deg, #cbd5e1, #e2e8f0);
            border-radius: 2px;
        }}
        .step-dot {{
            padding: 6px 14px;
            border-radius: 20px;
            background: #f1f5f9;
            border: 1.5px solid var(--border);
            font-size: 11.5px;
            font-weight: 700;
            color: var(--text-muted);
            cursor: pointer;
            transition: background 0.28s ease, color 0.28s ease, border-color 0.28s ease,
                        box-shadow 0.28s ease, transform 0.25s cubic-bezier(0.34,1.56,0.64,1);
            white-space: nowrap;
            user-select: none;
        }}
        .step-dot:hover:not(.active) {{
            background: rgba(8,145,178,0.07);
            border-color: rgba(8,145,178,0.30);
            color: var(--accent-dim);
        }}
        .step-dot.active {{
            background: linear-gradient(135deg, #0e7490, #0891b2);
            border-color: transparent;
            color: #fff;
            box-shadow: 0 3px 12px rgba(8,145,178,0.38);
            transform: scale(1.07);
        }}
        .step-dot.done {{
            background: rgba(22,163,74,0.09);
            border-color: rgba(22,163,74,0.28);
            color: #15803d;
        }}
        .step-label {{
            font-size: 11px;
            color: var(--text-muted);
            font-weight: 600;
            letter-spacing: 0.6px;
            text-transform: uppercase;
            margin-left: 8px;
            flex: 1;
        }}
        /* ── Progress bar ── */
        .step-progress-wrap {{
            height: 3px;
            background: #f1f5f9;
            border-radius: 2px;
            margin: 10px 0 18px;
            overflow: hidden;
        }}
        .step-progress-bar {{
            height: 100%;
            background: linear-gradient(90deg, #0e7490, #0891b2, #38bdf8);
            border-radius: 2px;
            transition: width 0.5s cubic-bezier(0.4, 0, 0.2, 1);
            width: 0%;
        }}
        /* ── Info Box ── */
        .info-box {{
            background: linear-gradient(135deg, #f8fbff, #f4f8ff);
            border: 1px solid #dde8f8;
            border-left: 4px solid var(--accent);
            border-radius: var(--radius-sm);
            padding: 20px 22px;
            min-height: 128px;
            display: flex;
            flex-direction: column;
            gap: 11px;
            position: relative;
            overflow: hidden;
        }}
        .info-box::before {{
            content: '';
            position: absolute;
            top: 0; right: 0;
            width: 110px; height: 110px;
            background: radial-gradient(circle, rgba(8,145,178,0.06), transparent 70%);
            pointer-events: none;
        }}
        .info-box h3 {{
            color: var(--text);
            font-size: 16.5px;
            font-weight: 800;
            display: flex;
            align-items: center;
            gap: 10px;
            line-height: 1.3;
            letter-spacing: -0.2px;
        }}
        .info-box h3::before {{
            content: '';
            display: inline-block;
            width: 8px; height: 8px;
            border-radius: 50%;
            background: var(--accent);
            flex-shrink: 0;
            box-shadow: 0 0 0 3px rgba(8,145,178,0.18);
        }}
        /* ── Badges ── */
        .badges {{ display:flex; gap:7px; flex-wrap:wrap; align-items:center; }}
        .badge {{
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 11.5px;
            font-weight: 700;
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }}
        .badge-cyan   {{ background:rgba(8,145,178,0.09);  border:1px solid rgba(8,145,178,0.28);  color:#0e7490; }}
        .badge-orange {{ background:rgba(217,119,6,0.09);  border:1px solid rgba(217,119,6,0.28);  color:#92400e; }}
        .badge-green  {{ background:rgba(22,163,74,0.09);  border:1px solid rgba(22,163,74,0.28);  color:#15803d; }}
        /* ── Description ── */
        .info-desc {{ font-size:14px; line-height:1.7; color:var(--text-sub); }}
        /* ── Actions ── */
        .actions {{
            display: flex;
            justify-content: flex-end;
            align-items: center;
            gap: 10px;
            margin-top: 20px;
        }}
        button {{
            padding: 11px 24px;
            border-radius: 10px;
            font-size: 13.5px;
            font-weight: 700;
            font-family: inherit;
            cursor: pointer;
            transition: background 0.22s ease, box-shadow 0.22s ease,
                        transform 0.18s cubic-bezier(0.34,1.56,0.64,1),
                        color 0.2s ease;
            border: none;
            outline: none;
            letter-spacing: 0.1px;
        }}
        .btn-primary {{
            background: linear-gradient(135deg, #0e7490, #0891b2);
            color: #fff;
            box-shadow: 0 4px 14px rgba(8,145,178,0.30);
        }}
        .btn-primary:hover {{
            background: linear-gradient(135deg, #0c6680, #0e7490);
            box-shadow: 0 6px 22px rgba(8,145,178,0.38);
            transform: translateY(-2px);
        }}
        .btn-primary:active {{ transform: translateY(0); }}
        .btn-secondary {{
            background: #fff;
            color: var(--text-sub);
            border: 1.5px solid var(--border-md);
            box-shadow: 0 1px 3px rgba(15,23,42,0.06);
        }}
        .btn-secondary:hover {{
            background: #f8fafc;
            color: var(--text);
            border-color: #94a3b8;
            transform: translateY(-1px);
        }}
    </style>
</head>
<body>
    <div class="page-header">
        <div class="page-chip">Interactive Animation</div>
    </div>
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
                    <radialGradient id="canvasBg" cx="35%" cy="38%" r="70%">
                        <stop offset="0%"   stop-color="#eef5ff" />
                        <stop offset="45%"  stop-color="#dce8f5" />
                        <stop offset="85%"  stop-color="#c8d9ed" />
                        <stop offset="100%" stop-color="#b8ccdf" />
                    </radialGradient>
                    <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
                        <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#1e3a5f" stroke-width="0.6" stroke-opacity="0.04" />
                    </pattern>
                    <linearGradient id="steel" x1="0%" y1="0%" x2="100%" y2="100%">
                        <stop offset="0%"   stop-color="#e8f0fa" />
                        <stop offset="35%"  stop-color="#c8d8e8" />
                        <stop offset="70%"  stop-color="#8aaac0" />
                        <stop offset="100%" stop-color="#5a7a9a" />
                    </linearGradient>
                    <linearGradient id="steelHi" x1="0%" y1="0%" x2="0%" y2="100%">
                        <stop offset="0%"   stop-color="#f4f8fc" />
                        <stop offset="50%"  stop-color="#d4e4f0" />
                        <stop offset="100%" stop-color="#94b4c8" />
                    </linearGradient>
                    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
                        <feDropShadow dx="0" dy="3" stdDeviation="5" flood-color="rgba(14,30,64,0.16)" />
                    </filter>
                    <filter id="shadowDeep" x="-25%" y="-25%" width="150%" height="150%">
                        <feDropShadow dx="0" dy="6" stdDeviation="10" flood-color="rgba(14,30,64,0.22)" />
                    </filter>
                    <filter id="glowCyan" x="-50%" y="-50%" width="200%" height="200%">
                        <feGaussianBlur stdDeviation="6" result="blur" />
                        <feComposite in="SourceGraphic" in2="blur" operator="over" />
                    </filter>
                    <filter id="glowOrange" x="-50%" y="-50%" width="200%" height="200%">
                        <feGaussianBlur stdDeviation="5" result="blur" />
                        <feComposite in="SourceGraphic" in2="blur" operator="over" />
                    </filter>
                    <marker id="arrowCyan" orient="auto" markerWidth="8" markerHeight="8" refX="4" refY="4">
                        <path d="M 0 1 L 7 4 L 0 7 Z" fill="#0891b2" />
                    </marker>
                    <marker id="arrowOrange" orient="auto" markerWidth="8" markerHeight="8" refX="4" refY="4">
                        <path d="M 0 1 L 7 4 L 0 7 Z" fill="#d97706" />
                    </marker>
                    <marker id="arrowGreen" orient="auto" markerWidth="8" markerHeight="8" refX="4" refY="4">
                        <path d="M 0 1 L 7 4 L 0 7 Z" fill="#16a34a" />
                    </marker>
                    <marker id="arrowGrey" orient="auto" markerWidth="7" markerHeight="7" refX="3.5" refY="3.5">
                        <path d="M 0 1 L 6 3.5 L 0 6 Z" fill="#94a3b8" />
                    </marker>
                </defs>
                <!-- Canvas background -->
                <rect width="850" height="478" fill="url(#canvasBg)" />
                <rect width="850" height="478" fill="url(#grid)" />
                <!-- Frame layer — always visible -->
                <g class="svg-layer" id="layer-frame">
                    <line x1="90" y1="239" x2="760" y2="239"
                          stroke="#b0c4de" stroke-width="1.2" stroke-dasharray="10,6" stroke-opacity="0.6"/>
                    <text x="425" y="50"
                          fill="#0e7490" font-size="20" font-weight="800"
                          text-anchor="middle"
                          font-family="'Segoe UI',system-ui,sans-serif"
                          filter="url(#glowCyan)"
                          letter-spacing="-0.3">{html_module.escape(script.get('title', title))}</text>
                    <text x="425" y="72"
                          fill="#64748b" font-size="12" font-weight="500"
                          text-anchor="middle"
                          font-family="'Segoe UI',system-ui,sans-serif">Interactive Step-by-Step Animation</text>
                </g>
                <!-- Blur shield (sits between frame and components) -->
                <rect id="blur-shield" width="850" height="478"
                      fill="#c2d4e8" opacity="0" pointer-events="none" />
                <!-- Step overlays -->
                {overlays_html}
            </svg>
        </div>
        <!-- Control Panel -->
        <div class="control-panel">
            <div class="step-indicator" id="dots">
                {dots_html}
                <div class="step-label" id="step-label">Ready</div>
            </div>
            <!-- Progress bar -->
            <div class="step-progress-wrap">
                <div class="step-progress-bar" id="step-bar"></div>
            </div>
            <div class="info-box">
                <h3 id="info-title">{title}</h3>
                <div class="badges" id="info-badges"></div>
                <div class="info-desc" id="info-desc">Press "Next Step" to begin the animation.</div>
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
var totalSteps = stepsData.length;

function applyStep(idx) {{
    if(idx < 0 || idx >= totalSteps) return;
    var data = stepsData[idx];

    // Blur shield
    var shield = document.getElementById('blur-shield');
    if(shield) shield.style.opacity = data.blurOp || 0;

    // Overlay visibility
    for(var oi = 0; oi < allOverlays.length; oi++) {{
        var el = document.getElementById(allOverlays[oi]);
        if(!el) continue;
        var show = false;
        if(data.overlays) {{
            for(var j = 0; j < data.overlays.length; j++) {{
                if(data.overlays[j] === allOverlays[oi]) {{ show = true; break; }}
            }}
        }}
        el.style.opacity = show ? '1' : '0';
    }}

    // Info box
    var elTitle = document.getElementById('info-title');
    var elBadges = document.getElementById('info-badges');
    var elDesc = document.getElementById('info-desc');
    if(elTitle) elTitle.innerText = data.title || '';
    if(elBadges) elBadges.innerHTML = data.badges || '';
    if(elDesc) elDesc.innerText = data.desc || '';

    // Step dots — active + done classes
    var dots = document.querySelectorAll('.step-dot');
    for(var di = 0; di < dots.length; di++) {{
        dots[di].classList.remove('active');
        dots[di].classList.remove('done');
        if(di < idx) dots[di].classList.add('done');
        if(di === idx) dots[di].classList.add('active');
    }}

    // Step label
    var slabel = document.getElementById('step-label');
    if(slabel) slabel.innerText = 'Step ' + (idx + 1) + ' of ' + totalSteps;

    // Progress bar
    var bar = document.getElementById('step-bar');
    if(bar) bar.style.width = Math.round((idx + 1) / totalSteps * 100) + '%';

    // Next button — keep visible on last step but change label to "View Formula →"
    var btn = document.getElementById('btn-next');
    if(btn) {{
        btn.style.display = 'inline-block';
        if(idx === totalSteps - 1) {{
            btn.textContent = 'View Formula \u25B6';
            btn.style.background = 'linear-gradient(135deg,#4338ca,#7c3aed)';
            btn.style.boxShadow = '0 4px 14px rgba(124,58,237,0.35)';
        }} else {{
            btn.textContent = 'Next Step \u25B6';
            btn.style.background = '';
            btn.style.boxShadow = '';
        }}
    }}
}}

function nextStep() {{
    if(currentStep < totalSteps - 1) {{
        currentStep++;
        applyStep(currentStep);
    }} else {{
        // Animation complete — open Scene 6 (Main Formula)
        if(typeof window.qanim_showScene6 === 'function') {{
            var svgCont = document.querySelector('.svg-container');
            if(svgCont) {{
                svgCont.style.transition = 'opacity .45s ease';
                svgCont.style.opacity = '0';
                setTimeout(function(){{ window.qanim_showScene6(); }}, 460);
            }} else {{
                window.qanim_showScene6();
            }}
        }}
    }}
}}

function resetAnim() {{
    currentStep = 0;
    var bar = document.getElementById('step-bar');
    if(bar) bar.style.width = '0%';
    // Restore svg container opacity if it was faded
    var svgCont = document.querySelector('.svg-container');
    if(svgCont) {{ svgCont.style.opacity = '1'; }}
    applyStep(0);
    var btn = document.getElementById('btn-next');
    if(btn) {{
        btn.style.display = 'inline-block';
        btn.textContent = 'Next Step \u25B6';
        btn.style.background = '';
        btn.style.boxShadow = '';
    }}
}}

setTimeout(function() {{ resetAnim(); }}, 80);
</script>
</body>
</html>"""
        return page

    @classmethod
    async def build_async(cls, question: str, scene_script: dict) -> str:
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, cls.build, question, scene_script),
                timeout=STAGE_TIMEOUT_BUILD,
            )
        except asyncio.TimeoutError:
            QAnimLogger.error("AnimationBuilder", f"Stage exceeded {STAGE_TIMEOUT_BUILD}s")
            raise  # caller (pipeline) already falls back to RecoveryEngine.fallback_html on any exception


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
        # This call runs CONCURRENTLY with the scene analyzer and solution
        # generator (asyncio.gather in the pipeline), which makes it the
        # most likely of the three to get rate-limited or come back
        # truncated. The previous version had ZERO retries of its own --
        # any single hiccup (a 429 that _call_gemini's own retries didn't
        # fully absorb, or JSON truncated at the old 800-token budget)
        # silently returned {"terms": []} and the button just never
        # appeared, with no visibility into why. That's the actual root
        # cause of the recurring "glossary missing" reports: it wasn't
        # missing by design, it was failing silently and often.
        MAX_ATTEMPTS = 3
        last_raw = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = GeminiSolutionGenerator._call_gemini(
                    f"Question: {question[:800]}",
                    _GLOSSARY_SYSTEM_GEMINI,
                    max_tokens=1536,  # was 800 -- too tight, caused truncated/invalid JSON
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
                if terms:
                    QAnimLogger.ok("GlossaryAnalyzer", f"Found {len(terms)} difficult word(s) (attempt {attempt})")
                else:
                    QAnimLogger.info("GlossaryAnalyzer", f"Parsed OK, 0 difficult words (attempt {attempt}) — genuinely none for this question")
                return {"terms": terms}
            except Exception as e:
                QAnimLogger.warn(
                    "GlossaryAnalyzer",
                    f"Attempt {attempt}/{MAX_ATTEMPTS} failed: {e} — "
                    f"raw response was: {last_raw[:300]!r}",
                )
                if attempt < MAX_ATTEMPTS:
                    continue
        QAnimLogger.error("GlossaryAnalyzer", f"All {MAX_ATTEMPTS} attempts failed — skipping glossary for this generation")
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
        return 8

    subq_count = sum(len(re.findall(p, ql)) for p in [r'\(\s*i+\s*\)', r'\(\s*[a-d]\s*\)', r'\bpart\s+[a-d1-4]\b'])
    if subq_count >= 2:
        return 8

    derive_kw = ["derive","prove","hence show","show that"]
    if any(k in ql for k in derive_kw):
        return 7

    find_count = len(re.findall(r'\b(?:find|calculate|determine|evaluate|compute|obtain)\b', ql))
    if find_count >= 2:
        return 7

    if length >= 400:
        return 7

    if length >= 200 or find_count >= 1:
        return 6

    return 5


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
        # Absolute ceiling on total wall-clock time. Every stage below this
        # already has its own timeout, so this should rarely fire — but it's
        # the backstop that guarantees this function NEVER hangs open-ended.
        # Before this existed, a slow/overloaded Gemini response had no
        # ceiling anywhere in the call chain, so "taking much longer than
        # expected" could mean anywhere from 2 minutes to 10+ minutes with
        # nothing to cut it off. Now the worst case is bounded and known.
        return await asyncio.wait_for(
            _run_generation_pipeline(question), timeout=PIPELINE_TIMEOUT
        )
    except asyncio.TimeoutError:
        QAnimLogger.error("Pipeline", f"TOTAL pipeline exceeded {PIPELINE_TIMEOUT}s — returning fallback")
        return _build_failure_result(
            question,
            f"Generation took longer than {int(PIPELINE_TIMEOUT)}s (Gemini was slow/overloaded) — "
            f"showing a fallback animation instead of hanging indefinitely. Please try again.",
        )
    except Exception as e:
        QAnimLogger.error("Pipeline", f"UNHANDLED error: {_err_msg(e)}")
        return _build_failure_result(question, f"Unexpected error: {_err_msg(e)}")


def _inject_fallback_warning_banner(html: str) -> str:
    """
    Inserts a visible, unmissable banner right after <body> telling the
    user that the actual formula/solution could not be generated for this
    question and that the panel below is a generic placeholder, not a
    real answer. This replaces the old silent behaviour where a failed
    Gemini call resulted in placeholder text rendered identically to a
    real solution, with nothing distinguishing the two.
    """
    banner = (
        # position:fixed so the banner floats ABOVE the Scene 6/7 modal overlays
        # (which are z-index 7400-7500) and is always visible to the user.
        # The old position:sticky was hidden behind the fixed modal backdrop.
        '<div id="qanim-fallback-banner" style="position:fixed;top:0;left:0;right:0;'
        'z-index:10000;background:#fef2f2;border-bottom:3px solid #dc2626;color:#991b1b;'
        'padding:12px 20px;font-family:-apple-system,\'Segoe UI\',Arial,sans-serif;'
        'font-size:13.5px;font-weight:700;text-align:center;'
        'box-shadow:0 4px 16px rgba(220,38,38,0.25);">'
        '&#9888; Solution generation failed (API timeout / rate limit) — '
        'formula boxes show placeholders, not the real answer. '
        'Wait 60 s then click &#8635; Restart to regenerate.'
        '<button onclick="document.getElementById(\'qanim-fallback-banner\').remove()" '
        'style="margin-left:14px;background:transparent;border:1.5px solid #dc2626;'
        'color:#dc2626;border-radius:6px;padding:3px 10px;cursor:pointer;'
        'font-size:12px;font-weight:700;">Dismiss</button>'
        '</div>'
    )
    if "<body" in html:
        # insert right after the opening <body ...> tag
        idx = html.find(">", html.find("<body")) + 1
        return html[:idx] + banner + html[idx:]
    # no <body> tag found (fragment) — just prepend
    return banner + html


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
    gemini_sol   = raw_results[1] if isinstance(raw_results[1], dict) else dict(GeminiSolutionGenerator._FALLBACK)
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
        QAnimLogger.error("Pipeline", f"Animation build failed: {_err_msg(e)}")
        animation_html = RecoveryEngine.fallback_html(question, f"Animation build error: {_err_msg(e)}")

    # Also build concept animation (same HTML is used for both)
    concept_html = animation_html

    # Sanitize
    animation_html = HtmlSanitizer.sanitize(animation_html)

    # Centre the animation dashboard (override whatever Gemini generated)
    animation_html = inject_centering_css(animation_html)

    # ── Surface silent solution-generation failures ──────────────────────
    # Previously, if GeminiSolutionGenerator exhausted its retries (rate
    # limit / timeout / bad JSON), the pipeline quietly substituted
    # cls._FALLBACK — generic text like "Select the appropriate governing
    # formula" and "See question for numerical values" — and rendered it
    # in the exact same styled boxes as a real solution. The page looked
    # complete and correct, so there was no signal to the user that
    # anything had gone wrong or that they should just retry. Now that the
    # fallback dict is tagged with _used_fallback, inject an explicit
    # warning banner instead of letting the placeholder pass as a result.
    if gemini_sol.get("_used_fallback"):
        QAnimLogger.warn("Pipeline", "Solution generation fell back to placeholder — injecting visible warning banner")
        animation_html = _inject_fallback_warning_banner(animation_html)

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
        scene_script=scene_script,
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

    # ── JS syntax gate ──────────────────────────────────────────────────
    # Every earlier check only confirms certain id strings are PRESENT in
    # the HTML. None of them confirm the JavaScript actually parses. One
    # stray apostrophe in stepsData (prime notation like l', theta', i')
    # is enough to silently kill nextStep/applyStep/window.onload for the
    # WHOLE page while it still looks fine on first load. Catch that here.
    js_errors = JsSyntaxValidator.find_errors(html)
    if js_errors:
        QAnimLogger.warn(
            "JsSyntaxValidator",
            f"{len(js_errors)} broken <script> block(s) found: {js_errors}",
        )
        repaired = JsSyntaxValidator.auto_fix_stray_apostrophes(html)
        remaining = JsSyntaxValidator.find_errors(repaired)
        if not remaining:
            QAnimLogger.ok(
                "JsSyntaxValidator",
                "Auto-repaired stray apostrophe(s) in JS string literal(s)",
            )
            html = repaired
        else:
            QAnimLogger.error(
                "JsSyntaxValidator",
                f"Could not auto-repair {len(remaining)} script block(s) "
                f"({remaining}) — serving the safe fallback page instead "
                f"of shipping one with dead buttons.",
            )
            html = RecoveryEngine.fallback_html(
                question, "Generated animation had unrecoverable JavaScript errors."
            )
            concept_html = html
    else:
        QAnimLogger.ok("JsSyntaxValidator", "All inline <script> blocks parse cleanly")

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
        "engine_version":  "v1.2-gemini",
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
        "engine_version":         "v1.2-gemini",
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
        print("  OK  All panels injected (Find/AnswerBox/Notes/Glossary/Scene6/Scene7)")
        print("  OK  Gemini 2.5 Pro for all stages")
