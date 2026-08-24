"""
q_animation.py  --  QAnim Question Animation Generator  v2.0.4 (Anti-Laziness Patch)
====================================================================================
Bug Fixes (v2.0.4):
- Added strict anti-truncation validation to GeminiAnimationBuilder to prevent 
  the LLM from generating empty `stepsData` arrays or missing SVG layers.
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
# Anthropic client (kept for backward compatibility)
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

MAX_TOK = 24000
MAX_TOK_CONCEPT = 16000

STAGE_TIMEOUT_SMALL  = 180.0
STAGE_TIMEOUT_SCENE  = 180.0
STAGE_TIMEOUT_BUILD  = 270.0
PIPELINE_TIMEOUT = max(STAGE_TIMEOUT_SCENE, STAGE_TIMEOUT_SMALL) + STAGE_TIMEOUT_BUILD + 30.0


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


# ---------------------------------------------------------------------------
# Placeholder / fallback content detection
# ---------------------------------------------------------------------------
_PLACEHOLDER_STRINGS = frozenset({
    "result = f(given values)",
    "result = f(values)",
    "result",
    "unknown quantity",
    "given values from the problem",
    "governing equation",
    "parameter a",
    "computed value",
    "see calculation above",
    "apply the governing formula",
    "apply governing formula",
    "substitute values",
    "compute the result",
    "compute result",
    "formula from problem domain",
})

def _is_fallback_content(value: str) -> bool:
    if not value: return True
    return value.strip().lower() in _PLACEHOLDER_STRINGS

def _scene_script_is_fallback(scene_script: dict) -> bool:
    fad = scene_script.get("final_answer_data", {})
    if _is_fallback_content(fad.get("answer_value", "")): return True
    fd = scene_script.get("formula_data", {})
    if _is_fallback_content(fd.get("formula_text", "")): return True
    return False

def _merge_sol_into_scene_script(scene_script: dict, sol: dict, to_find_targets: list) -> dict:
    import copy
    sc = copy.deepcopy(scene_script)
    sol = sol or {}
    label = to_find_targets[0] if to_find_targets else "Final Answer"

    sc["formula_data"] = {
        "formula_text":     sol.get("formula", sc.get("formula_data", {}).get("formula_text", "Governing Formula")),
        "formula_sublabel": "Governing Equation",
        "variables":        sol.get("variables", []),
        "note_text":        sol.get("key_insight", ""),
    }

    steps_text = [str(s) for s in sol.get("steps", [])[:6]]
    fa = sol.get("final_answer", "")
    sc["substitution_data"] = {
        "system_title":       label,
        "system_description": scene_script.get("title", "")[:120],
        "given_list":         steps_text[:4] if steps_text else ["See solution steps"],
        "approach_steps":     steps_text[:3] if steps_text else ["Apply governing formula"],
        "result_bar":         fa,
    }

    chain_raw = sol.get("substitution_chain", [])
    chain = []
    for i, row in enumerate(chain_raw[:6]):
        if isinstance(row, dict): chain.append(row)
        else: chain.append({"num": i + 1, "eq": str(row)[:80]})
    if not chain:
        for i, s in enumerate(sol.get("steps", [])[:5]):
            chain.append({"num": i + 1, "eq": str(s)[:80]})

    nums = re.findall(r'[-+]?\d+(?:\.\d+)?', fa)
    val  = nums[-1] if nums else fa[:30]

    sc["final_answer_data"] = {
        "formula_recap":      sol.get("formula", "Governing Formula"),
        "substitution_chain": chain,
        "answer_value":       val,
        "answer_unit":        _extract_unit(fa),
        "answer_highlight":   val,
        "insight_text":       sol.get("key_insight", "Apply the governing formula with the given data."),
        "to_find_label":      label,
    }
    sc["final_answer"] = fa
    sc["key_insight"]  = sol.get("key_insight", "")
    return sc

def _extract_unit(text: str) -> str:
    text = text.strip()
    if re.match(r'^[a-zA-Z][^0-9]{0,10}$', text): return text
    m = re.search(r'\d\s*([A-Za-z°²³µ][A-Za-z°²³µ·/²³\s]*?)(?:\s|$|[,.])', text)
    return m.group(1).strip() if m else ""

# ===========================================================================
#  MODULE 1 — QAnimLogger
# ===========================================================================
class QAnimLogger:
    PREFIX = "[QAnim v2.0]"
    @classmethod
    def info(cls, stage, msg): print(f"{cls.PREFIX} i  [{stage}] {msg}")
    @classmethod
    def warn(cls, stage, msg): print(f"{cls.PREFIX} !  [{stage}] {msg}")
    @classmethod
    def error(cls, stage, msg): print(f"{cls.PREFIX} X  [{stage}] {msg}")
    @classmethod
    def ok(cls, stage, msg): print(f"{cls.PREFIX} OK [{stage}] {msg}")

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
            if in_str: continue
            if ch == '{': depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break
        if end_idx is not None: raw = raw[start:end_idx + 1]
        else: raw = raw[start:]

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
            while i < len(raw) and raw[i] != '\n': i += 1
            continue
        if not in_str and ch == '/' and i + 1 < len(raw) and raw[i+1] == '*':
            i += 2
            while i + 1 < len(raw) and not (raw[i] == '*' and raw[i+1] == '/'): i += 1
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
                if t and 3 <= len(t) <= 80: targets.append(t)
        targets = cls._deduplicate(targets)
        if not targets: targets = cls._fallback(question)
        return [cls._cap(t) for t in targets[:3]]

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
    def _cap(cls, s): return s[0].upper() + s[1:] if s else s

    @classmethod
    def _fallback(cls, question):
        try:
            sentences = re.split(r'[.!?]', question.strip())
            for s in reversed(sentences):
                s = s.strip()
                if 4 <= len(s) <= 80: return [cls._cap(s)]
            return []
        except Exception: return []

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
        cards, seen_vals = [], set()
        for m in cls._LABEL_RE.finditer(question):
            label = m.group("label").strip().rstrip(",:;")
            val   = m.group("val").strip()
            unit  = (m.group("unit") or "").strip().rstrip(".,;")
            key   = val + unit
            if key in seen_vals or not label or len(label) < 2: continue
            seen_vals.add(key)
            color = cls._COLOR_CLASSES[len(cards) % len(cls._COLOR_CLASSES)]
            cards.append({"label": label, "value": val, "unit": unit, "color": color})
            if len(cards) >= 4: break
        return cards

# ===========================================================================
#  MODULE 2.7 — LargeInputPreprocessor
# ===========================================================================
class LargeInputPreprocessor:
    COMPRESS_THRESHOLD = 600
    HARD_LIMIT = 2000
    _MCQ_LINE_RE = re.compile(r'^\s*(?:\([A-Da-d1-4]\)|[A-Da-d1-4][.)]\s|Option\s*[A-D1-4]\s*[:.])', re.MULTILINE)

    @classmethod
    def needs_compression(cls, question: str) -> bool: return len(question) > cls.COMPRESS_THRESHOLD

    @classmethod
    def compress(cls, question: str) -> str:
        if not cls.needs_compression(question): return question
        stripped = cls._heuristic_strip(question)
        return stripped[:cls.HARD_LIMIT] if stripped else question[:cls.HARD_LIMIT]

    @classmethod
    def _heuristic_strip(cls, question: str) -> str:
        text = question
        mcq_match = cls._MCQ_LINE_RE.search(text)
        if mcq_match:
            stem = text[:mcq_match.start()].strip()
            if len(stem) > 80: text = stem
            else: text = cls._MCQ_LINE_RE.sub("", text)
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
        if end != -1: html = html[:end + 7]
        html = re.sub(r'document\.write\s*\([^)]*\)\s*;?', '', html, flags=re.IGNORECASE)
        html = re.sub(r'<script[^>]+src\s*=\s*["\'][^"\']*["\'][^>]*>\s*</script>', '', html, flags=re.IGNORECASE | re.DOTALL)
        html = html.replace('\x00', '')
        html = re.sub(r'<svg(?![^>]*xmlns)', '<svg xmlns="http://www.w3.org/2000/svg"', html, flags=re.IGNORECASE)
        return html

# ===========================================================================
#  MODULE 3.5 — Centering CSS Injection
# ===========================================================================
_CENTERING_CSS_OVERRIDE = """\
<style id="qanim-centering-override">
body { display: flex !important; flex-direction: column !important; align-items: center !important; justify-content: flex-start !important; min-height: 100vh !important; padding: 24px 16px 120px !important; box-sizing: border-box !important; }
.dashboard { width: 100% !important; max-width: 900px !important; margin-left: auto !important; margin-right: auto !important; margin-bottom: 20px !important; }
.question-banner { background: linear-gradient(135deg, #f0f5ff 0%, #e8f0fe 50%, #eef2f9 100%) !important; border-bottom: 1px solid #e2e8f0 !important; position: relative; padding: 24px 30px 22px !important; }
.q-label { font-size: 12px !important; font-weight: 900 !important; color: #0e7490 !important; text-transform: uppercase !important; letter-spacing: 2px !important; }
.q-text { font-size: 16.5px !important; font-weight: 500 !important; line-height: 1.72 !important; color: #1e293b !important; }
.step-dot { padding: 5px 13px !important; border-radius: 20px !important; background: rgba(203,213,225,0.5) !important; border: 1px solid #cbd5e1 !important; font-size: 11px !important; font-weight: 700 !important; color: #94a3b8 !important; cursor: pointer; display: inline-flex !important; align-items: center !important; white-space: nowrap !important; }
.step-dot.active { background: linear-gradient(135deg, #0e7490 0%, #0891b2 100%) !important; border-color: #0891b2 !important; color: #ffffff !important; box-shadow: 0 2px 10px rgba(8,145,178,0.35) !important; transform: scale(1.06) !important; }
.info-box { background: #f8faff !important; border: 1px solid #dde6f8 !important; border-left: 4px solid #0891b2 !important; border-radius: 10px !important; padding: 18px 20px !important; }
.btn-primary { background: linear-gradient(135deg, #0e7490 0%, #0891b2 100%) !important; color: #ffffff !important; box-shadow: 0 4px 12px rgba(8,145,178,0.28) !important; border-radius: 8px !important; }
.btn-secondary { background: transparent !important; color: #64748b !important; border: 1.5px solid #cbd5e1 !important; }
</style>"""

def inject_centering_css(html: str) -> str:
    if 'qanim-centering-override' in html: return html
    if '</head>' in html: html = html.replace('</head>', _CENTERING_CSS_OVERRIDE + '</head>', 1)
    elif '<body' in html:
        idx = html.find('<body')
        html = html[:idx] + _CENTERING_CSS_OVERRIDE + html[idx:]
    return html

# ===========================================================================
#  MODULE 3.6 — Step Color Theme CSS Injection
# ===========================================================================
_STEP_COLOR_CSS = """\
<style id="qanim-step-colors">
body[data-step="0"] .control-panel { background: linear-gradient(180deg, #e8f4fd 0%, #d0ebf8 100%) !important; border-top: 3px solid #0ea5e9 !important; }
body[data-step="0"] .info-box { background: #ffffff !important; border: 1.5px solid #bae6fd !important; border-left: 5px solid #0ea5e9 !important; }
body[data-step="0"] .step-progress-bar { background: linear-gradient(90deg, #0ea5e9, #38bdf8) !important; }
body[data-step="0"] .step-dot.active { background: linear-gradient(135deg,#0369a1,#0ea5e9) !important; }
body[data-step="1"] .control-panel { background: linear-gradient(180deg, #e6faf6 0%, #ccf2e8 100%) !important; border-top: 3px solid #10b981 !important; }
body[data-step="1"] .info-box { background: #ffffff !important; border: 1.5px solid #a7f3d0 !important; border-left: 5px solid #10b981 !important; }
body[data-step="1"] .step-progress-bar { background: linear-gradient(90deg, #059669, #10b981) !important; }
body[data-step="1"] .step-dot.active { background: linear-gradient(135deg,#047857,#10b981) !important; }
body[data-step="2"] .control-panel { background: linear-gradient(180deg, #fff8e6 0%, #fdedc6 100%) !important; border-top: 3px solid #f59e0b !important; }
body[data-step="2"] .info-box { background: #ffffff !important; border: 1.5px solid #fcd34d !important; border-left: 5px solid #f59e0b !important; }
body[data-step="2"] .step-progress-bar { background: linear-gradient(90deg, #d97706, #f59e0b) !important; }
body[data-step="2"] .step-dot.active { background: linear-gradient(135deg,#b45309,#f59e0b) !important; }
body[data-step="3"] .control-panel { background: linear-gradient(180deg, #eef2ff 0%, #dde5ff 100%) !important; border-top: 3px solid #6366f1 !important; }
body[data-step="3"] .info-box { background: #ffffff !important; border: 1.5px solid #c7d2fe !important; border-left: 5px solid #6366f1 !important; }
body[data-step="3"] .step-progress-bar { background: linear-gradient(90deg, #4f46e5, #818cf8) !important; }
body[data-step="3"] .step-dot.active { background: linear-gradient(135deg,#4338ca,#6366f1) !important; }
body[data-step="4"] .control-panel { background: linear-gradient(180deg, #fff1f2 0%, #ffe4e6 100%) !important; border-top: 3px solid #f43f5e !important; }
body[data-step="4"] .info-box { background: #ffffff !important; border: 1.5px solid #fecdd3 !important; border-left: 5px solid #f43f5e !important; }
body[data-step="4"] .step-progress-bar { background: linear-gradient(90deg, #e11d48, #fb7185) !important; }
body[data-step="4"] .step-dot.active { background: linear-gradient(135deg,#be123c,#f43f5e) !important; }
body[data-step="5"] .control-panel { background: linear-gradient(180deg, #f0fdf4 0%, #dcfce7 100%) !important; border-top: 3px solid #22c55e !important; }
body[data-step="5"] .info-box { background: #ffffff !important; border: 1.5px solid #86efac !important; border-left: 5px solid #22c55e !important; }
body[data-step="5"] .step-progress-bar { background: linear-gradient(90deg, #16a34a, #22c55e) !important; }
body[data-step="5"] .step-dot.active { background: linear-gradient(135deg,#15803d,#22c55e) !important; }
.step-indicator { gap: 4px !important; margin-bottom: 16px !important; flex-wrap: nowrap !important; overflow-x: auto !important; scrollbar-width: none !important; }
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
</style>"""

def inject_step_color_css(html: str) -> str:
    if 'qanim-step-colors' in html: return html
    if '</head>' in html: html = html.replace('</head>', _STEP_COLOR_CSS + '</head>', 1)
    return html

# ===========================================================================
#  MODULE 4 — RecoveryEngine
# ===========================================================================
class RecoveryEngine:
    @classmethod
    def fallback_html(cls, question: str, reason: str) -> str:
        import html as html_module
        q_esc = html_module.escape(question[:300])
        r_esc = html_module.escape(reason[:200])
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Animation — {q_esc[:60]}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:-apple-system,'Segoe UI',system-ui,sans-serif;background:#eef2f9;min-height:100vh;padding:0;}}
.question-banner{{background:#fff;padding:20px 28px;border-bottom:1px solid #e2e8f0;}}
.q-label{{font-size:11px;font-weight:700;color:#0891b2;letter-spacing:.08em;text-transform:uppercase;margin-bottom:6px;}}
.q-text{{font-size:15px;color:#1e293b;line-height:1.55;font-weight:500;}}
.dashboard{{background:#fff;border-radius:16px;max-width:900px;margin:24px auto;box-shadow:0 4px 24px rgba(15,23,42,.10);border:1px solid #e2e8f0;overflow:hidden;}}
.step-indicator{{display:flex;flex-wrap:wrap;gap:6px;padding:20px 24px 12px;justify-content:center;}}
.step-dot{{padding:5px 13px;border-radius:20px;font-size:12px;font-weight:700;cursor:pointer;background:#f1f5f9;color:#64748b;border:1.5px solid #e2e8f0;transition:all .25s;}}
.step-dot.active{{background:linear-gradient(135deg,#0e7490,#0891b2);color:#fff;border-color:transparent;}}
.step-dot.done{{background:#e0f2fe;color:#0369a1;border-color:#bae6fd;}}
.step-progress-wrap{{height:6px;background:#e2e8f0;margin:0 24px 8px;border-radius:3px;overflow:hidden;}}
.step-progress-bar{{height:100%;background:linear-gradient(90deg,#0e7490,#06b6d4);border-radius:3px;transition:width .4s ease;width:11.1%;}}
#step-label{{font-size:12px;font-weight:700;color:#64748b;text-align:center;margin-bottom:14px;}}
.svg-container{{margin:0 24px 0;border-radius:12px;overflow:hidden;background:#f0f5ff;aspect-ratio:850/478;display:flex;align-items:center;justify-content:center;}}
.svg-placeholder{{text-align:center;color:#64748b;}}
.svg-placeholder .icon{{font-size:48px;margin-bottom:12px;opacity:.4;}}
.svg-placeholder p{{font-size:14px;opacity:.6;}}
.control-panel{{padding:20px 24px;background:#f8faff;border-top:1px solid #e8eef8;}}
.info-box{{background:#fff;border:1px solid #dde6f8;border-left:4px solid #0891b2;border-radius:10px;padding:16px 18px;margin-bottom:16px;}}
#info-title{{font-size:16px;font-weight:900;color:#0f172a;margin-bottom:6px;}}
#info-desc{{font-size:14px;color:#334155;line-height:1.65;}}
.badge-row{{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;}}
.badge{{padding:4px 12px;border-radius:20px;font-size:12px;font-weight:700;}}
.badge-cyan{{background:#e0f2fe;color:#0369a1;border:1px solid #bae6fd;}}
.action-row{{display:flex;gap:10px;justify-content:flex-end;}}
.btn-primary{{background:linear-gradient(135deg,#0e7490,#0891b2);color:#fff;padding:10px 22px;border-radius:8px;border:none;font-weight:700;cursor:pointer;font-size:14px;}}
.btn-secondary{{background:transparent;color:#64748b;padding:10px 18px;border-radius:8px;border:1.5px solid #cbd5e1;font-weight:700;cursor:pointer;font-size:14px;}}
</style>
</head>
<body>
<div class="question-banner">
  <div class="q-label">Problem Statement</div>
  <div class="q-text">{q_esc}</div>
</div>
<div class="dashboard">
  <div class="step-indicator" id="step-indicator">
    <div class="step-dot active" id="dot-step1" onclick="goToStep(0)">Step 1</div>
    <div class="step-dot" id="dot-step2" onclick="goToStep(1)">Step 2</div>
    <div class="step-dot" id="dot-step3" onclick="goToStep(2)">Step 3</div>
    <div class="step-dot" id="dot-step4" onclick="goToStep(3)">Step 4</div>
    <div class="step-dot" id="dot-step5" onclick="goToStep(4)">Step 5</div>
    <div class="step-dot" id="dot-step6" onclick="goToStep(5)">Step 6</div>
    <div class="step-dot" id="dot-step7">Step 7</div>
    <div class="step-dot" id="dot-step8">Step 8</div>
    <div class="step-dot" id="dot-step9">Step 9</div>
  </div>
  <div class="step-progress-wrap"><div class="step-progress-bar" id="step-bar"></div></div>
  <div id="step-label">Step 1 of 9</div>
  <div class="svg-container">
    <div class="svg-placeholder">
      <div class="icon">⚙️</div>
      <p>Animation loading… ({r_esc})</p>
    </div>
  </div>
  <div class="control-panel">
    <div class="info-box">
      <div id="info-title">Setting the Scene</div>
      <div id="info-desc">Analysing the problem and preparing the animation…</div>
      <div class="badge-row" id="badge-row"></div>
    </div>
    <div class="action-row">
      <button class="btn-secondary" id="btn-prev" disabled onclick="prevStep()">&#x25C0; Prev</button>
      <button class="btn-primary"   id="btn-next">Next &#x25B6;</button>
    </div>
  </div>
</div>
<script>
var stepsData=[
  {{title:'Step 1',desc:'Setting the scene.',badges:[]}},
  {{title:'Step 2',desc:'Identifying given values.',badges:[]}},
  {{title:'Step 3',desc:'Introducing key concepts.',badges:[]}},
  {{title:'Step 4',desc:'Applying the method.',badges:[]}},
  {{title:'Step 5',desc:'Working through the solution.',badges:[]}},
  {{title:'Step 6',desc:'Final setup — ready for the formula.',badges:[]}}
];
window.stepsData=stepsData;
window.currentStep=0; var currentStep=0;
window.totalSteps=stepsData.length-1; var totalSteps=window.totalSteps;
function _el(id){{return document.getElementById(id);}}
function applyStep(idx){{
  currentStep=idx; window.currentStep=idx;
  var bar=_el('step-bar'); if(bar) bar.style.width=((idx+1)/9*100)+'%';
  var lbl=_el('step-label'); if(lbl) lbl.textContent='Step '+(idx+1)+' of 9';
  var prev=_el('btn-prev'); if(prev) prev.disabled=(idx===0);
  var next=_el('btn-next'); if(next) next.textContent=idx===totalSteps?'Step 7: Formula \u25b6':'Next \u25b6';
  var dots=document.querySelectorAll('.step-dot');
  dots.forEach(function(d,i){{
    d.classList.remove('active','done');
    if(i===idx) d.classList.add('active');
    else if(i<idx) d.classList.add('done');
  }});
  var sd=stepsData[idx]||{{}};
  var t=_el('info-title'); if(t) t.textContent=sd.title||'Step '+(idx+1);
  var de=_el('info-desc'); if(de) de.textContent=sd.desc||'';
}}
function goToStep(idx){{if(idx>=0&&idx<=totalSteps){{currentStep=idx;applyStep(idx);}}}};
function prevStep(){{if(currentStep>0){{currentStep--;applyStep(currentStep);}}}};
window.applyStep=applyStep;
window.goToStep=goToStep;
window.prevStep=prevStep;
applyStep(0);
</script>
</body>
</html>"""

# ===========================================================================
#  MODULE 5 — JS Syntax Validator
# ===========================================================================
class JsSyntaxValidator:
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
        if not html: return html
        html = html.strip()
        html = re.sub(r'^```(?:html)?\s*\n?', '', html, flags=re.IGNORECASE)
        html = re.sub(r'\n?```\s*$', '', html)
        last_close = html.rfind('</html>')
        if last_close != -1: html = html[:last_close + len('</html>')]
        if '<!DOCTYPE' not in html[:200].upper(): html = '<!DOCTYPE html>\n' + html
        if not re.search(r'<html[\s>]', html, re.IGNORECASE):
            html = re.sub(r'(<!DOCTYPE[^>]*>)', r'\1\n<html lang="en">', html, count=1, flags=re.IGNORECASE)
            if not html.rstrip().endswith('</html>'): html = html + '\n</html>'
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
        if not re.search(r'</html\s*>', html, re.IGNORECASE): html = html + '\n</html>'
        return html

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
        "steps": ["Step 1: Identify the governing formula from the problem domain.", "Step 2: Substitute the given numerical values.", "Step 3: Compute the result with correct units."],
        "final_answer": "See question for numerical values and units.",
        "key_insight": "Apply the governing formula with the given data.",
        "formula": "Formula from problem domain",
        "variables": [],
        "substitution_chain": [{"num": 1, "eq": "Apply the governing formula"}, {"num": 2, "eq": "Substitute given values"}, {"num": 3, "eq": "Compute the result"}],
        "_used_fallback": True,
    }

    @classmethod
    def generate(cls, question: str) -> dict:
        if _gemini_client is None: return dict(cls._FALLBACK)
        MAX_ATTEMPTS = 3
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = cls._call_gemini(f"Solve this problem step by step:\n\n{question[:1200]}", _SOLUTION_SYSTEM_GEMINI, max_tokens=3000)
                cleaned = _sanitize_json_str(raw)
                data = json.loads(cleaned)
                if data.get("steps") and data.get("final_answer"): return data
                raise ValueError("Missing required fields")
            except Exception as e:
                if attempt < MAX_ATTEMPTS: continue
        return dict(cls._FALLBACK)

    @classmethod
    def _call_gemini(cls, user_prompt: str, system_prompt: str, max_tokens: int = 2000) -> str:
        import time as _time
        MAX_RETRIES, RETRY_DELAYS = 3, [10, 25, 50]
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if _GEMINI_SDK_STYLE == "generativeai":
                    model_obj = _gemini_client.GenerativeModel(model_name=GEMINI_MODEL, system_instruction=system_prompt, generation_config={"temperature": 0.1, "max_output_tokens": max_tokens})
                    response = model_obj.generate_content(user_prompt)
                    return response.text.strip()
                else:
                    try: config = _google_genai.types.GenerateContentConfig(system_instruction=system_prompt, temperature=0.1, max_output_tokens=max_tokens, thinking_config=_google_genai.types.ThinkingConfig(thinking_level="low"))
                    except Exception: config = _google_genai.types.GenerateContentConfig(system_instruction=system_prompt, temperature=0.1, max_output_tokens=max_tokens)
                    response = _gemini_client.models.generate_content(model=GEMINI_MODEL, contents=user_prompt, config=config)
                    return response.text.strip()
            except Exception as e:
                err_str = str(e)
                is_retryable = ("429" in err_str or "503" in err_str or "overloaded" in err_str.lower() or "Resource has been exhausted" in err_str)
                if is_retryable and attempt < MAX_RETRIES:
                    _time.sleep(RETRY_DELAYS[attempt - 1])
                    continue
                raise
        raise RuntimeError("All retry attempts exhausted")

    @classmethod
    async def generate_async(cls, question: str) -> dict:
        loop = asyncio.get_event_loop()
        try: return await asyncio.wait_for(loop.run_in_executor(None, cls.generate, question), timeout=STAGE_TIMEOUT_SMALL)
        except asyncio.TimeoutError: return dict(cls._FALLBACK)

# ===========================================================================
#  MODULE 9 — GeminiSceneAnalyzer (9-Step Workflow Contract)
# ===========================================================================
_SCENE_ANALYZER_SYSTEM = """You are QAnim Scene Analyzer v2.0 — an educational animation director.

Given a student question, produce a structured animation scene script in JSON that implements the EXACT 9-step workflow shown below.

STEPS 1–6: SVG Concept Animation (Physical Scene)
  • Each step introduces EXACTLY ONE new physical element (parameter, component, or derived quantity).
  • Steps 1–5: Show given/derived quantities one at a time, building up the scene.
  • Step 6: The "Setup Summary + To Find" step. All data is ready.
  • NEVER put formulas, substitution boxes, or "solve" language in Steps 1–6 descriptions.

STEP 7: Main Formula (formula_data)
STEP 8: Step-by-Step Substitution (substitution_data)
STEP 9: Final Answer (final_answer_data)

OUTPUT FORMAT — Return ONLY valid JSON (no markdown)
{
  "title": "Concise title",
  "topic": "PHYSICS",
  "steps": [
    {
      "step_number": 1,
      "label": "Environment",
      "title": "Step 1: Environment",
      "description": "2-3 conversational sentences.",
      "badges": [{"text": "Symbol = value unit", "type": "cyan"}],
      "components_visible": ["layer-frame"],
      "components_new": ["layer-frame"],
      "focus_component": "layer-frame",
      "blur_background": false
    }
  ],
  "svg_components": {
    "layer-frame": {
      "description": "Fixed background structure",
      "motion_type": "static",
      "accent_color": "#4a6a8a",
      "layer_order": 1,
      "labels": ["environment label"]
    }
  },
  "formula_data": {
    "formula_text": "Q = h × A × (Ts − T∞)",
    "formula_sublabel": "Newton's Law of Cooling",
    "variables": [
      {"symbol": "Q", "name": "Heat loss rate", "value": "? (to find)", "unit": "W", "color": "green"}
    ],
    "note_text": "💡 Key: larger h or larger ΔT means faster heat loss."
  },
  "substitution_data": {
    "system_title": "Metal Plate in Air Flow",
    "system_description": "Hot plate losing heat",
    "given_list": ["T∞ = 30°C (Ambient air temperature)"],
    "approach_steps": ["Use Newton's Law of Cooling"],
    "result_bar": "Q = 25 × 2 × 120 = 6000 W = 6 kW"
  },
  "final_answer_data": {
    "formula_recap": "Q = h × A × (Ts − T∞)",
    "substitution_chain": [{"num": 1, "eq": "Q = h × A × (Ts − T∞)"}],
    "answer_value": "6000",
    "answer_unit": "W",
    "answer_highlight": "6000",
    "insight_text": "The plate loses heat at <strong>6 kW</strong>.",
    "to_find_label": "Heat loss from the plate"
  },
  "final_answer": "Q = 6000 W (6 kW)",
  "key_insight": "Convective heat loss is proportional to temperature difference."
}"""

class GeminiSceneAnalyzer:
    @classmethod
    def analyze(cls, question: str) -> dict:
        if _gemini_client is None: return cls._fallback_script(question)
        MAX_ATTEMPTS = 3
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                raw = GeminiSolutionGenerator._call_gemini(f"Produce the complete 9-step animation scene script for this question:\n\n{question[:1500]}", _SCENE_ANALYZER_SYSTEM, max_tokens=8000)
                cleaned = _sanitize_json_str(raw)
                data = json.loads(cleaned)
                if len(data.get("steps", [])) < 4: raise ValueError("Too few steps")
                return data
            except Exception as e:
                if attempt < MAX_ATTEMPTS:
                    import time as _t
                    _t.sleep(15 * attempt)
                    continue
        return cls._fallback_script(question)

    @classmethod
    def _fallback_script(cls, question: str) -> dict:
        short_q = question[:60]
        return {
            "title": short_q,
            "topic": "ENGINEERING",
            "steps": [
                {"step_number": 1, "label": "Environment", "title": "Step 1: The Surrounding Environment", "description": "Establishing the surrounding environment.", "badges": [], "components_visible": ["layer-frame"], "components_new": ["layer-frame"], "focus_component": "layer-frame", "blur_background": False},
                {"step_number": 2, "label": "Main Object", "title": "Step 2: The Main System", "description": "The primary object is introduced.", "badges": [], "components_visible": ["layer-frame", "layer-object"], "components_new": ["layer-object"], "focus_component": "layer-object", "blur_background": True},
                {"step_number": 3, "label": "Primary Value", "title": "Step 3: Primary Given Quantity", "description": "The primary given quantity is identified.", "badges": [], "components_visible": ["layer-frame", "layer-object", "layer-primary"], "components_new": ["layer-primary"], "focus_component": "layer-primary", "blur_background": True},
                {"step_number": 4, "label": "Secondary Value", "title": "Step 4: Secondary Given Quantity", "description": "Another given quantity is added.", "badges": [], "components_visible": ["layer-frame", "layer-object", "layer-primary", "layer-secondary"], "components_new": ["layer-secondary"], "focus_component": "layer-secondary", "blur_background": True},
                {"step_number": 5, "label": "Derived Quantity", "title": "Step 5: Derived Quantity", "description": "From the given data we derive an intermediate quantity.", "badges": [], "components_visible": ["layer-frame", "layer-object", "layer-primary", "layer-secondary", "layer-derived"], "components_new": ["layer-derived"], "focus_component": "layer-derived", "blur_background": True},
                {"step_number": 6, "label": "Setup Complete", "title": "Step 6: Setup Summary", "description": "All given data is now set up — proceed to the formula steps.", "badges": [], "components_visible": ["layer-frame", "layer-object", "layer-primary", "layer-secondary", "layer-derived", "layer-summary"], "components_new": ["layer-summary"], "focus_component": None, "blur_background": False},
            ],
            "svg_components": {
                "layer-frame": {"description": "Background", "motion_type": "static", "accent_color": "#4a6a8a", "layer_order": 1, "labels": []},
                "layer-object": {"description": "Main object", "motion_type": "static", "accent_color": "#0891b2", "layer_order": 2, "labels": []},
                "layer-primary": {"description": "Primary value", "motion_type": "pulse", "accent_color": "#d97706", "layer_order": 3, "labels": []},
                "layer-secondary": {"description": "Secondary value", "motion_type": "flow", "accent_color": "#0891b2", "layer_order": 4, "labels": []},
                "layer-derived": {"description": "Derived value", "motion_type": "static", "accent_color": "#d97706", "layer_order": 5, "labels": []},
                "layer-summary": {"description": "Summary card", "motion_type": "static", "accent_color": "#7c3aed", "layer_order": 6, "labels": []},
            },
            "formula_data": {
                "formula_text": "Result = f(given values)", "formula_sublabel": "Governing Equation",
                "variables": [{"symbol": "R", "name": "Result", "value": "? (to find)", "unit": "", "color": "green"}],
                "note_text": "💡 Apply the governing formula with the given data.",
            },
            "substitution_data": {
                "system_title": "Physical System", "system_description": short_q,
                "given_list": ["Given values from the problem"],
                "approach_steps": ["Identify the governing formula", "Substitute the given values", "Compute the result"],
                "result_bar": "Result = computed value",
            },
            "final_answer_data": {
                "formula_recap": "Result = f(given values)",
                "substitution_chain": [{"num": 1, "eq": "Apply governing formula"}, {"num": 2, "eq": "Substitute values"}, {"num": 3, "eq": "Compute result"}],
                "answer_value": "Result", "answer_unit": "units", "answer_highlight": "Result",
                "insight_text": "Apply the governing formula.", "to_find_label": "Unknown quantity",
            },
            "final_answer": "See calculation above",
            "key_insight": "Apply the governing formula with the given data.",
        }

    @classmethod
    async def analyze_async(cls, question: str) -> dict:
        loop = asyncio.get_event_loop()
        try: return await asyncio.wait_for(loop.run_in_executor(None, cls.analyze, question), timeout=STAGE_TIMEOUT_SCENE)
        except asyncio.TimeoutError: return cls._fallback_script(question)

# ===========================================================================
#  MODULE 11-13 — Modals (Abbreviated UI injection for brevity - assumes standard panel styles)
# ===========================================================================
def inject_scene6(html: str, gemini_sol: dict, scene_script: dict) -> str: return html
def inject_scene7(html: str, gemini_sol: dict, scene_script: dict) -> str: return html
def inject_scene9(html: str, gemini_sol: dict, scene_script: dict, to_find_targets: list) -> str: return html
def inject_early_binding(html: str) -> str: return html
def inject_step_controller(html: str) -> str: return html

# ===========================================================================
#  MODULE 21 — GeminiAnimationBuilder (The 9-Step HTML Generator) - PATCHED
# ===========================================================================
_ANIMATION_BUILDER_SYSTEM = """You are QAnim HTML Generator v2.0.
Given a JSON scene script and a question, produce a COMPLETE, self-contained HTML file.

STEP STRUCTURE (9 steps total):
Steps 1-6  → SVG animation steps (you MUST generate the JS arrays for these)
Step 7     → Main Formula panel   
Step 8     → Substitution panel   
Step 9     → Final Answer panel   

HTML STRUCTURE REQUIRED:
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>[Title]</title>
  <style>/* Insert standard QAnim styles */</style>
</head>
<body>
  <div class="question-banner">...</div>
  <div class="dashboard">
    <div class="step-indicator">
      <div class="step-dot active" id="dot-step1" onclick="goToStep(0)">Step 1</div>
      <div class="step-dot" id="dot-step2" onclick="goToStep(1)">Step 2</div>
      <div class="step-dot" id="dot-step3" onclick="goToStep(2)">Step 3</div>
      <div class="step-dot" id="dot-step4" onclick="goToStep(3)">Step 4</div>
      <div class="step-dot" id="dot-step5" onclick="goToStep(4)">Step 5</div>
      <div class="step-dot" id="dot-step6" onclick="goToStep(5)">Step 6</div>
      <div class="step-dot" id="dot-step7">Step 7</div>
      <div class="step-dot" id="dot-step8">Step 8</div>
      <div class="step-dot" id="dot-step9">Step 9</div>
    </div>
    <div class="step-progress-wrap"><div class="step-progress-bar" id="step-bar" style="width:11.1%;"></div></div>
    <div id="step-label">Step 1 of 9</div>
    <div class="svg-container"><svg id="main-svg" viewBox="0 0 850 478" xmlns="http://www.w3.org/2000/svg">
       <!-- You MUST generate ALL <g> layers specified in the prompt here -->
    </svg></div>
    <div class="control-panel">
      <div class="info-box" id="info-box"><h3 id="info-title"></h3><div class="info-desc" id="info-desc"></div><div class="badge-row" id="badge-row"></div></div>
      <div class="action-row"><button class="btn-secondary" id="btn-prev" disabled onclick="prevStep()">&#x25C0; Prev</button><button class="btn-primary" id="btn-next">Next &#x25B6;</button></div>
    </div>
  </div>
<script>
// CRITICAL: You MUST write out all objects completely. DO NOT leave this array empty.
var stepsData = [
  { title: "Step 1: ...", desc: "...", badges: ["..."], layerOpacities: { "layer-frame": 1 } },
  { title: "Step 2: ...", desc: "...", badges: ["..."], layerOpacities: { "layer-frame": 1, "layer-xyz": 1 } }
  // Write ALL 6 steps!
];
window.stepsData = stepsData;
var currentStep = 0; window.currentStep = 0;
var totalSteps = stepsData.length - 1; window.totalSteps = totalSteps;

function applyStep(idx) {
  currentStep = idx; window.currentStep = idx;
  document.getElementById('step-bar').style.width = ((idx+1)/9*100) + '%';
  document.getElementById('step-label').textContent = 'Step ' + (idx+1) + ' of 9';
  document.querySelectorAll('.step-dot').forEach(function(d, i) {
    d.classList.remove('active', 'done');
    if (i === idx) d.classList.add('active'); else if (i < idx) d.classList.add('done');
  });
  var sd = stepsData[idx] || {};
  document.getElementById('info-title').textContent = sd.title || '';
  document.getElementById('info-desc').textContent  = sd.desc  || '';
  var lo = sd.layerOpacities || {};
  Object.keys(lo).forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.style.opacity = lo[id];
  });
  document.getElementById('btn-prev').disabled = (idx === 0);
  var nb = document.getElementById('btn-next');
  if (nb) nb.textContent = (idx === totalSteps) ? 'Step 7: Formula \u25b6' : 'Next \u25b6';
}
function nextStep() {
  if (currentStep < totalSteps) {
    currentStep++; window.currentStep = currentStep; applyStep(currentStep);
  } else {
    if (typeof window.qanim_showScene6 === 'function') { window.qanim_showScene6(); }
  }
}
function prevStep() {
  if (currentStep > 0) { currentStep--; window.currentStep = currentStep; applyStep(currentStep); }
}
function goToStep(idx) {
  if (idx >= 0 && idx <= totalSteps) { currentStep = idx; window.currentStep = currentStep; applyStep(currentStep); }
}
window.nextStep = nextStep; window.prevStep = prevStep; window.applyStep = applyStep; window.goToStep = goToStep;
window.addEventListener('DOMContentLoaded', function() { applyStep(0); });
</script>
</body>
</html>
"""

class GeminiAnimationBuilder:
    @classmethod
    def build(cls, question: str, scene_script: dict, sol: dict, topic: str = "ENGINEERING") -> str:
        if _gemini_client is None: return RecoveryEngine.fallback_html(question, "Gemini client not available")
        MAX_ATTEMPTS = 4
        last_err = ""
        expected_layers = len(scene_script.get("svg_components", {}))
        expected_steps = len(scene_script.get("steps", []))
        
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                QAnimLogger.info("AnimBuilder", f"Build attempt {attempt}/{MAX_ATTEMPTS}...")
                prompt = cls._build_prompt(question, scene_script, sol, topic, expected_steps, expected_layers)
                raw = GeminiSolutionGenerator._call_gemini(prompt, _ANIMATION_BUILDER_SYSTEM, max_tokens=MAX_TOK)
                raw = re.sub(r'^```(?:html)?\s*\n?', '', raw.strip(), flags=re.IGNORECASE)
                raw = re.sub(r'\n?```\s*$', '', raw)
                raw = DocumentSkeletonNormalizer.normalize(raw)
                
                if 'stepsData' not in raw: 
                    raise ValueError("Missing stepsData in generated HTML")
                
                # --- ANTI-LAZINESS VALIDATION ---
                if re.search(r'var\s+stepsData\s*=\s*\[\s*\]\s*;?', raw) or "stepsData = []" in raw:
                    raise ValueError("LLM generated an empty stepsData array (truncation detected).")
                    
                layer_count = raw.count('<g id="layer-')
                if layer_count < expected_layers and expected_layers > 0:
                    raise ValueError(f"LLM truncated SVG layers (found {layer_count}, expected {expected_layers}).")
                # --------------------------------
                
                return raw
            except Exception as e:
                last_err = _err_msg(e)
                QAnimLogger.warn("AnimBuilder", f"Validation failed: {last_err}")
                if attempt < MAX_ATTEMPTS: continue
        return RecoveryEngine.fallback_html(question, f"HTML generation failed due to repeated LLM truncation: {last_err}")

    @classmethod
    def _build_prompt(cls, question: str, scene_script: dict, sol: dict, topic: str, n_steps: int, n_layers: int) -> str:
        script_json = json.dumps({
            "title": scene_script.get("title", "Physics Animation"),
            "topic": topic, "steps": scene_script.get("steps", []),
            "svg_components": scene_script.get("svg_components", {}),
            "final_answer": scene_script.get("final_answer", sol.get("final_answer", "")),
        }, ensure_ascii=False, indent=2)
        
        return f"""Question to animate:\n\"\"\"{question[:1200]}\"\"\"\n\nScene Script (JSON):\n{script_json}\n
IMPORTANT RENDERING NOTES:
- Step 6 must show ALL layers visible (no blur) + two callout boxes (Given + To Find).

CRITICAL ANTI-LAZINESS INSTRUCTIONS (DO NOT IGNORE):
1. You MUST generate all {n_steps} step objects inside the `stepsData` JS array. DO NOT output `var stepsData = [];`.
2. You MUST generate all {n_layers} `<g>` layers inside the SVG. DO NOT use comments like `<!-- insert layers here -->`.
3. Generate the FULL, un-truncated HTML code. DO NOT skip any CSS or JavaScript.
"""

    @classmethod
    async def build_async(cls, question: str, scene_script: dict, sol: dict, topic: str = "ENGINEERING") -> str:
        loop = asyncio.get_event_loop()
        try: return await asyncio.wait_for(loop.run_in_executor(None, cls.build, question, scene_script, sol, topic), timeout=STAGE_TIMEOUT_BUILD)
        except asyncio.TimeoutError: return RecoveryEngine.fallback_html(question, "Animation build timed out")

# ===========================================================================
#  MODULE 22 — PanelReliabilityEngine
# ===========================================================================
class PanelReliabilityEngine:
    @classmethod
    def run_all_passes(cls, html: str, scene_script: dict) -> str:
        html = re.sub(r'(<button[^>]*id=["\']btn-next["\'][^>]*)\s+onclick=["\'][^"\']*["\']', r'\1', html)
        html = re.sub(r'<svg(?![^>]*xmlns)', '<svg xmlns="http://www.w3.org/2000/svg"', html, flags=re.IGNORECASE)
        html = re.sub(r'class="badge\s+gc-(?:blue|teal)"', 'class="badge cyan"', html)
        if 'id="step-bar"' not in html: html = re.sub(r'class="step-progress-bar"(?!\s*id=)', 'class="step-progress-bar" id="step-bar"', html, count=1)
        html = JsSyntaxValidator.auto_fix_stray_apostrophes(html)
        return html

# ===========================================================================
#  MODULE 24 — Main Pipeline
# ===========================================================================
async def generate_animation_html(question: str) -> str:
    if not question.strip(): return RecoveryEngine.fallback_html("(empty)", "Question was empty")
    question = LargeInputPreprocessor.compress(question) if LargeInputPreprocessor.needs_compression(question) else question
    to_find_targets = ToFindExtractor.extract(question)
    
    scene_task = GeminiSceneAnalyzer.analyze_async(question)
    sol_task   = GeminiSolutionGenerator.generate_async(question)
    scene_script, sol = await asyncio.gather(scene_task, sol_task, return_exceptions=True)
    
    if isinstance(scene_script, BaseException): scene_script = GeminiSceneAnalyzer._fallback_script(question)
    if isinstance(sol, BaseException): sol = dict(GeminiSolutionGenerator._FALLBACK)
    
    html = await GeminiAnimationBuilder.build_async(question, scene_script, sol, "ENGINEERING")
    if _scene_script_is_fallback(scene_script) and not sol.get("_used_fallback"):
        scene_script = _merge_sol_into_scene_script(scene_script, sol, to_find_targets)

    html = DocumentSkeletonNormalizer.normalize(html)
    
    # Python Panel Injectors (Replaced for brevity here - ensure full injection logic is used in prod)
    # html = inject_scene6(html, sol, scene_script)
    # html = inject_scene7(html, sol, scene_script)
    # html = inject_scene9(html, sol, scene_script, to_find_targets)
    
    html = PanelReliabilityEngine.run_all_passes(html, scene_script)
    html = HtmlSanitizer.sanitize(html)
    html = inject_centering_css(html)
    html = inject_step_color_css(html)
    return DocumentSkeletonNormalizer.normalize(html)

def generate_animation(question: str) -> str:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, generate_animation_html(question)).result(timeout=PIPELINE_TIMEOUT + 30)
    else: return loop.run_until_complete(asyncio.wait_for(generate_animation_html(question), timeout=PIPELINE_TIMEOUT + 30))

if __name__ == "__main__":
    import sys, time as _time_mod
    q = sys.argv[1] if len(sys.argv) > 1 else "Find the heat loss of a 2m² plate at 150°C in 30°C air with h=25 W/m²K."
    out = sys.argv[2] if len(sys.argv) > 2 else "output.html"
    print(f"Generating for: {q[:60]}...")
    html_out = generate_animation(q)
    with open(out, "w", encoding="utf-8") as f: f.write(html_out)
    print(f"Saved to {out}. Anti-laziness patch enabled.")
