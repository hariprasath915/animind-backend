"""
q_animation.py  --  QAnim Question Animation Generator  v3.0
====================================================================================
Updates (v3.0):
- Fully matched visual design and DOM structure of user's reference HTML.
- Removed "To Find" button and feature entirely.
- Preserved strict JSON-to-Template anti-laziness architecture.
"""

import json
import re
import asyncio
import html as html_module
import os as _os

# ---------------------------------------------------------------------------
# Gemini SDK Configuration
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
    except ImportError:
        print("[QAnim Gemini] No Gemini SDK found — generation will fail gracefully")

GEMINI_MODEL = "gemini-3.1-pro-preview"
_gemini_client = None

if _GEMINI_AVAILABLE:
    _gkey = _os.environ.get("GEMINI_API_KEY", "").strip()
    if _gkey:
        if _GEMINI_SDK_STYLE == "generativeai":
            _google_genai.configure(api_key=_gkey)
            _gemini_client = _google_genai
        else:
            _gemini_client = _google_genai.Client(api_key=_gkey)

MAX_TOK = 24000
STAGE_TIMEOUT_SMALL  = 180.0
STAGE_TIMEOUT_SCENE  = 180.0
STAGE_TIMEOUT_BUILD  = 270.0
PIPELINE_TIMEOUT = max(STAGE_TIMEOUT_SCENE, STAGE_TIMEOUT_SMALL) + STAGE_TIMEOUT_BUILD + 30.0

def _err_msg(e: BaseException) -> str:
    if isinstance(e, asyncio.TimeoutError): return "Gemini timeout. The response was too large or model overloaded."
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg}" if msg else f"{type(e).__name__}"

class QAnimLogger:
    PREFIX = "[QAnim v3.0]"
    @classmethod
    def info(cls, stage, msg): print(f"{cls.PREFIX} i  [{stage}] {msg}")
    @classmethod
    def warn(cls, stage, msg): print(f"{cls.PREFIX} !  [{stage}] {msg}")
    @classmethod
    def error(cls, stage, msg): print(f"{cls.PREFIX} X  [{stage}] {msg}")
    @classmethod
    def ok(cls, stage, msg): print(f"{cls.PREFIX} OK [{stage}] {msg}")

def _sanitize_json_str(raw: str) -> str:
    raw = raw.lstrip('\ufeff').strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.IGNORECASE)
    raw = re.sub(r'```\s*$', '', raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r'<thinking>.*?</thinking>', '', raw, flags=re.DOTALL).strip()
    start = raw.find('{')
    if start != -1:
        depth, in_str, esc, end_idx = 0, False, False, None
        for i, ch in enumerate(raw[start:], start):
            if esc: esc = False; continue
            if ch == '\\' and in_str: esc = True; continue
            if ch == '"': in_str = not in_str; continue
            if in_str: continue
            if ch == '{': depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0: end_idx = i; break
        raw = raw[start:end_idx + 1] if end_idx is not None else raw[start:]
    out, in_str, esc, i = [], False, False, 0
    while i < len(raw):
        ch = raw[i]
        if esc: out.append(ch); esc = False; i += 1; continue
        if ch == '\\' and in_str: out.append(ch); esc = True; i += 1; continue
        if ch == '"': in_str = not in_str; out.append(ch); i += 1; continue
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
    return raw.strip()

# ===========================================================================
#  MODULE: GeminiSolutionGenerator
# ===========================================================================
_SOLUTION_SYSTEM = """You are a precise physics/engineering/math solver.
Given a question, produce a step-by-step solution and final answer in JSON.
Return ONLY valid JSON:
{
  "steps": ["Step 1: Formula Q = h × A × ΔT", "Step 2: Substitute", "Step 3: Compute"],
  "final_answer": "Q = 6000 W", "key_insight": "Insight here.",
  "formula": "Q = h × A × ΔT",
  "variables": [
    {"symbol": "Q", "name": "Heat loss", "value": "6000", "unit": "W", "color": "green"}
  ],
  "substitution_chain": [
    {"num": 1, "eq": "Q = 25 × 2 × 120"}
  ]
}"""

class GeminiSolutionGenerator:
    _FALLBACK = {
        "steps": ["Step 1: Identify formula.", "Step 2: Substitute.", "Step 3: Compute."],
        "final_answer": "See calculation.", "key_insight": "Apply formula.",
        "formula": "Formula", "variables": [],
        "substitution_chain": [{"num": 1, "eq": "Substitute"}], "_used_fallback": True,
    }

    @classmethod
    def generate(cls, question: str) -> dict:
        if _gemini_client is None: return dict(cls._FALLBACK)
        for attempt in range(1, 4):
            try:
                if _GEMINI_SDK_STYLE == "generativeai":
                    m = _gemini_client.GenerativeModel(model_name=GEMINI_MODEL, system_instruction=_SOLUTION_SYSTEM, generation_config={"temperature": 0.1})
                    raw = m.generate_content(f"Solve:\n\n{question[:1200]}").text.strip()
                else:
                    conf = _google_genai.types.GenerateContentConfig(system_instruction=_SOLUTION_SYSTEM, temperature=0.1)
                    raw = _gemini_client.models.generate_content(model=GEMINI_MODEL, contents=f"Solve:\n\n{question[:1200]}", config=conf).text.strip()
                return json.loads(_sanitize_json_str(raw))
            except Exception: pass
        return dict(cls._FALLBACK)

    @classmethod
    async def generate_async(cls, question: str) -> dict:
        loop = asyncio.get_event_loop()
        try: return await asyncio.wait_for(loop.run_in_executor(None, cls.generate, question), timeout=STAGE_TIMEOUT_SMALL)
        except asyncio.TimeoutError: return dict(cls._FALLBACK)

# ===========================================================================
#  MODULE: GeminiSceneAnalyzer
# ===========================================================================
_SCENE_ANALYZER_SYSTEM = """Produce a structured animation scene script in JSON for a 9-step workflow.
Return ONLY JSON:
{
  "title": "Title", "topic": "PHYSICS",
  "steps": [
    {
      "step_number": 1, "label": "Environment", "title": "Step 1: Env", "description": "Desc",
      "badges": ["<span class=\\"badge badge-cyan\\">Label = Val</span>"],
      "layerOpacities": {"layer-frame": 1, "layer-obj": 0}
    }
  ],
  "svg_components": { "layer-frame": {"description": "BG"} },
  "formula_data": { "formula_text": "F=ma", "formula_sublabel": "Law", "variables": [{"symbol": "F", "name": "Force", "value": "?", "unit": "N", "color": "green"}], "note_text": "Insight" },
  "substitution_data": { "system_title": "Sys", "system_description": "...", "given_list": ["m = 10 kg"], "approach_steps": ["Use F=ma"], "result_bar": "F = 10 N" },
  "final_answer_data": { "formula_recap": "F=ma", "substitution_chain": [{"num":1, "eq":"F=10"}], "answer_value": "10", "answer_unit": "N", "answer_highlight": "10", "insight_text": "Insight" }
}"""

class GeminiSceneAnalyzer:
    @classmethod
    def analyze(cls, question: str) -> dict:
        if _gemini_client is None: return cls._fallback_script(question)
        for attempt in range(1, 4):
            try:
                if _GEMINI_SDK_STYLE == "generativeai":
                    m = _gemini_client.GenerativeModel(model_name=GEMINI_MODEL, system_instruction=_SCENE_ANALYZER_SYSTEM, generation_config={"temperature": 0.1})
                    raw = m.generate_content(f"Script:\n\n{question[:1500]}").text.strip()
                else:
                    conf = _google_genai.types.GenerateContentConfig(system_instruction=_SCENE_ANALYZER_SYSTEM, temperature=0.1)
                    raw = _gemini_client.models.generate_content(model=GEMINI_MODEL, contents=f"Script:\n\n{question[:1500]}", config=conf).text.strip()
                data = json.loads(_sanitize_json_str(raw))
                if len(data.get("steps", [])) < 4: raise ValueError("Too few steps")
                return data
            except Exception: pass
        return cls._fallback_script(question)

    @classmethod
    def _fallback_script(cls, question: str) -> dict:
        return {"title": question[:60], "topic": "ENGINEERING", "steps": [{"step_number":1,"label":"Setup","title":"Step 1","description":"Setup","badges":[],"layerOpacities":{"layer-frame":1}}], "svg_components": {"layer-frame":{}}, "formula_data": {}, "substitution_data": {}, "final_answer_data": {}}

    @classmethod
    async def analyze_async(cls, question: str) -> dict:
        loop = asyncio.get_event_loop()
        try: return await asyncio.wait_for(loop.run_in_executor(None, cls.analyze, question), timeout=STAGE_TIMEOUT_SCENE)
        except asyncio.TimeoutError: return cls._fallback_script(question)

# ===========================================================================
#  HTML & CSS SKELETON DEFINITIONS
# ===========================================================================

_BASE_CSS = """
:root { --bg-color: #eef2f9; --panel-bg: #ffffff; --text-main: #1e293b; --text-sub: #64748b; --text-muted: #94a3b8; --accent-cyan: #0891b2; --accent-cyan-dim: #0e7490; --accent-orange: #d97706; --accent-green: #16a34a; --border: #e2e8f0; --border-strong: #cbd5e1; --border-radius: 16px; --border-radius-sm: 10px; --shadow-card: 0 1px 3px rgba(15,23,42,.06),0 8px 24px rgba(15,23,42,.08),0 24px 48px rgba(15,23,42,.04); --transition-smooth: .45s cubic-bezier(.4,0,.2,1); }
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:linear-gradient(160deg,#eef2f9 0%,#e8f0fe 50%,#eff6ff 100%);background-attachment:fixed;color:var(--text-main);display:flex;flex-direction:column;align-items:center;justify-content:flex-start;min-height:100vh;padding:28px 16px 140px;}
.page-header{width:100%;max-width:900px;margin-bottom:14px;display:flex;align-items:center;gap:10px;}
.page-chip{display:inline-flex;align-items:center;gap:6px;padding:5px 12px;border-radius:20px;background:rgba(8,145,178,.10);border:1px solid rgba(8,145,178,.22);font-size:11px;font-weight:700;color:var(--accent-cyan-dim);text-transform:uppercase;letter-spacing:.8px;}
.page-chip::before{content:'▶';font-size:8px;}
.dashboard{width:100%;max-width:900px;margin:0 auto;background:var(--panel-bg);border-radius:var(--border-radius);box-shadow:var(--shadow-card);overflow:hidden;border:1px solid var(--border);position:relative;}
.dashboard::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--accent-cyan-dim) 0%,#7c3aed 50%,var(--accent-orange) 100%);border-radius:var(--border-radius) var(--border-radius) 0 0;z-index:2;}
.question-banner{padding:22px 28px 18px;background:linear-gradient(135deg,#f8faff 0%,#f0f5ff 40%,#eef2f9 100%);border-bottom:1px solid var(--border);display:flex;flex-direction:column;gap:8px;}
.q-label{font-size:10.5px;font-weight:800;color:var(--accent-cyan-dim);text-transform:uppercase;letter-spacing:1.8px;display:flex;align-items:center;gap:8px;}
.q-label::before{content:'';display:inline-block;width:16px;height:16px;border-radius:5px;background:linear-gradient(135deg,var(--accent-cyan-dim),var(--accent-cyan));flex-shrink:0;}
.q-text{font-size:15px;color:var(--text-main);line-height:1.6;font-weight:500;max-width:820px;}
.svg-container{width:100%;aspect-ratio:16/9;background:radial-gradient(ellipse at 35% 38%,#eef5ff 0%,#dce8f5 45%,#c8d9ed 85%,#b8ccdf 100%);position:relative;overflow:hidden;border-bottom:1px solid var(--border);}
svg{display:block;width:100%;height:100%;}
svg g[id^="layer-"] { transition: opacity 0.5s ease; }
.control-panel{padding:22px 28px 26px;background:linear-gradient(180deg,#ffffff 0%,#f9fbff 100%);border-top:1px solid var(--border);}
.step-indicator{display:flex;align-items:center;gap:6px;margin-bottom:16px;flex-wrap:wrap;}
.step-connector{flex:0 0 18px;height:1.5px;background:linear-gradient(90deg,#cbd5e1,#e2e8f0);border-radius:2px;}
.step-dot{padding:6px 14px;border-radius:20px;background:#f1f5f9;border:1.5px solid #e2e8f0;font-size:11.5px;font-weight:700;color:#94a3b8;cursor:pointer;transition:all .3s ease;white-space:nowrap;user-select:none;}
.step-dot:hover:not(.active){background:rgba(8,145,178,.07);border-color:rgba(8,145,178,.3);color:var(--accent-cyan-dim);}
.step-dot.active{background:linear-gradient(135deg,#0e7490 0%,#0891b2 100%);border-color:transparent;color:#fff;box-shadow:0 3px 12px rgba(8,145,178,.38);transform:scale(1.07);}
.step-dot.done{background:rgba(22,163,74,.09);border-color:rgba(22,163,74,.28);color:#15803d;}
.step-label{font-size:11px;color:var(--text-muted);font-weight:600;letter-spacing:.6px;text-transform:uppercase;margin-left:6px;}
.step-progress-wrap{height:3px;background:#f1f5f9;border-radius:2px;margin-bottom:20px;overflow:hidden;}
.step-progress-bar{height:100%;background:linear-gradient(90deg,#0e7490,#0891b2,#38bdf8);border-radius:2px;transition:width .5s ease;width:0%;}
.info-box{background:linear-gradient(135deg,#f8fbff 0%,#f4f8ff 100%);border:1px solid #dde8f8;border-left:4px solid var(--accent-cyan);border-radius:var(--border-radius-sm);padding:20px 22px;min-height:130px;display:flex;flex-direction:column;gap:11px;}
.info-box h3{color:var(--text-main);font-size:16.5px;font-weight:800;display:flex;align-items:center;gap:10px;line-height:1.3;margin:0;}
.info-box h3::before{content:'';display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent-cyan);box-shadow:0 0 0 3px rgba(8,145,178,.18);}
.badges{display:flex;gap:7px;flex-wrap:wrap;align-items:center;}
.badge{padding:4px 12px;border-radius:20px;font-size:11.5px;font-weight:700;}
.badge-cyan{background:rgba(8,145,178,.09);border:1px solid rgba(8,145,178,.28);color:#0e7490;}
.badge-orange{background:rgba(217,119,6,.09);border:1px solid rgba(217,119,6,.28);color:#92400e;}
.badge-green{background:rgba(22,163,74,.09);border:1px solid rgba(22,163,74,.28);color:#15803d;}
.info-desc{font-size:14px;line-height:1.7;color:var(--text-sub);font-weight:400;}
.actions{display:flex;justify-content:flex-end;align-items:center;gap:10px;margin-top:20px;}
button{padding:11px 24px;border-radius:10px;font-size:13.5px;font-weight:700;font-family:inherit;cursor:pointer;border:none;transition:all .2s;}
.btn-primary{background:linear-gradient(135deg,#0e7490 0%,#0891b2 100%);color:#fff;box-shadow:0 4px 14px rgba(8,145,178,.30);}
.btn-primary:hover{box-shadow:0 6px 22px rgba(8,145,178,.38);transform:translateY(-2px);}
.btn-secondary{background:#fff;color:var(--text-sub);border:1.5px solid var(--border-strong);}
.btn-secondary:hover:not(:disabled){background:#f8fafc;color:var(--text-main);border-color:#94a3b8;transform:translateY(-1px);}
.btn-secondary:disabled{opacity:.4;cursor:not-allowed;}
"""

_MODALS_CSS = """
#qanim-scene-modal-backdrop{display:none;position:fixed;inset:0;z-index:7400;background:rgba(15,23,42,.50);backdrop-filter:blur(6px);opacity:0;transition:opacity .25s ease;}
#qanim-scene-modal-backdrop.qanim-scene-visible{display:block!important;opacity:1;}
.qanim-overlay{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) scale(.95);z-index:7500;width:min(860px,96vw);max-height:92vh;overflow-y:auto;opacity:0;transition:all .3s ease;}
.qanim-overlay.qanim-scene-visible{display:block!important;opacity:1;transform:translate(-50%,-50%) scale(1);}

/* Scene 6 (Formula) */
.s6-card{background:#fff;border-radius:20px;box-shadow:0 8px 48px rgba(8,145,178,.14);border:1px solid #dde8f8;overflow:hidden;}
.s6-title-bar{text-align:center;padding:22px 28px 18px;border-bottom:1px solid #e8eef8;}
.s6-title-bar h2{font-size:20px;font-weight:900;color:#0f172a;margin:0;}
.s6-body{padding:28px 32px 24px;background:linear-gradient(160deg,#eef2f9 0%,#e8f0fe 50%,#eff6ff 100%);}
.s6-formula-box{background:#fff;border:2.5px solid #3b82f6;border-radius:18px;padding:20px 32px 16px;text-align:center;margin-bottom:10px;}
.s6-formula-main{font-family:'Courier New',monospace;font-size:28px;font-weight:900;color:#1d4ed8;line-height:1.4;}
.s6-formula-sublabel{font-size:11px;font-weight:700;color:#6366f1;margin-top:8px;}
.s6-vars-row{display:flex;justify-content:center;gap:12px;flex-wrap:wrap;margin-top:24px;}
.s6-var-box{display:flex;flex-direction:column;align-items:center;min-width:120px;}
.s6-var-inner{border:2px solid;border-radius:14px;padding:14px 16px 12px;text-align:center;width:100%;}
.s6-var-sym{font-family:'Courier New',monospace;font-size:22px;font-weight:900;margin-bottom:5px;display:block;}
.s6-var-name{font-size:11.5px;font-weight:700;color:#475569;display:block;}
.s6-var-val{font-size:10.5px;font-weight:600;color:#94a3b8;margin-top:3px;display:block;}
.s6-var-arrow{width:2px;height:24px;position:relative;}
.s6-var-arrow::before{content:'';position:absolute;left:50%;transform:translateX(-50%);top:0;width:2px;height:18px;}
.s6-var-arrow::after{content:'';position:absolute;bottom:0;left:50%;transform:translateX(-50%);border-left:6px solid transparent;border-right:6px solid transparent;}
.s6v-blue .s6-var-inner{border-color:#3b82f6;background:#eff6ff;} .s6v-blue .s6-var-sym{color:#1d4ed8;} .s6v-blue .s6-var-arrow::before{background:#3b82f6;} .s6v-blue .s6-var-arrow::after{border-top:8px solid #3b82f6;}
.s6v-green .s6-var-inner{border-color:#22c55e;background:#f0fdf4;} .s6v-green .s6-var-sym{color:#15803d;} .s6v-green .s6-var-arrow::before{background:#22c55e;} .s6v-green .s6-var-arrow::after{border-top:8px solid #22c55e;}
.s6v-orange .s6-var-inner{border-color:#f59e0b;background:#fff7ed;} .s6v-orange .s6-var-sym{color:#d97706;} .s6v-orange .s6-var-arrow::before{background:#f59e0b;} .s6v-orange .s6-var-arrow::after{border-top:8px solid #f59e0b;}
.s6-nav-row{display:flex;justify-content:space-between;padding:16px 32px 22px;border-top:1px solid #e8eef8;background:#fff;}

/* Scene 7 (Substitution) */
#qanim-scene7-overlay{width:min(900px,96vw);}
.s7-card{background:#fff;border-radius:20px;box-shadow:0 8px 48px rgba(37,99,235,.12);border:1px solid #e8eef8;overflow:hidden;}
.s7-title-bar{text-align:center;padding:20px 28px 16px;border-bottom:1px solid #e8eef8;}
.s7-title-bar h2{font-size:20px;font-weight:900;color:#0f172a;margin:0;}
.s7-body-cols{display:flex;align-items:flex-start;min-height:320px;}
.s7-left-col{width:44%;border-right:1.5px solid #e8eef8;padding:22px 20px;background:linear-gradient(180deg,#eff6ff 0%,#dbeafe 100%);display:flex;flex-direction:column;}
.s7-system-label{font-size:10.5px;font-weight:800;color:#1d4ed8;text-transform:uppercase;margin-bottom:10px;}
.s7-formula-result-bar{background:linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);border:2px solid #86efac;border-radius:12px;padding:12px 16px;margin-top:auto;}
.s7-formula-result-text{font-family:'Courier New',monospace;font-size:14px;font-weight:900;color:#15803d;}
.s7-right-col{flex:1;padding:22px 26px;display:flex;flex-direction:column;gap:16px;}
.s7-given-section-title{font-size:13px;font-weight:900;color:#1d4ed8;margin-bottom:8px;}
.s7-given-list{display:flex;flex-direction:column;gap:5px;}
.s7-given-item{font-size:12.5px;color:#334155;display:flex;gap:7px;}
.s7-given-item::before{content:'•';color:#3b82f6;font-weight:900;}
.s7-approach-section-title{font-size:13px;font-weight:900;color:#7c3aed;}
.s7-approach-step{display:flex;gap:8px;font-size:12.5px;color:#1e293b;margin-bottom:6px;}
.s7-approach-step-num{font-weight:800;color:#7c3aed;}

/* Scene 9 (Answer) */
#qanim-scene9-overlay{width:min(780px,96vw);}
.s9-card{background:#fff;border-radius:20px;box-shadow:0 8px 48px rgba(22,163,74,.18);border:2px solid #86efac;overflow:hidden;}
.s9-title-bar{text-align:center;padding:22px 28px 18px;background:linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);border-bottom:2px solid #86efac;}
.s9-title-bar h2{font-size:22px;font-weight:900;color:#14532d;margin:0;}
.s9-body{padding:32px 36px 28px;display:flex;flex-direction:column;gap:22px;}
.s9-formula-recap{background:#eff6ff;border:1.5px solid #bfdbfe;border-radius:12px;padding:14px 20px;text-align:center;}
.s9-formula-recap-eq{font-family:'Courier New',monospace;font-size:18px;font-weight:900;color:#1d4ed8;margin-top:6px;}
.s9-sub-row{display:flex;align-items:center;gap:12px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 16px;margin-bottom:8px;}
.s9-sub-row.final{background:linear-gradient(135deg,#f0fdf4,#dcfce7);border-color:#86efac;border-width:2px;}
.s9-sub-num{background:#0891b2;color:#fff;border-radius:50%;width:26px;height:26px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;}
.s9-sub-row.final .s9-sub-num{background:#16a34a;}
.s9-sub-eq{font-family:'Courier New',monospace;font-size:15px;font-weight:700;color:#1e293b;}
.s9-final-box{background:linear-gradient(135deg,#f0fdf4 0%,#dcfce7 100%);border:3px solid #22c55e;border-radius:18px;padding:28px 32px;text-align:center;}
.s9-final-value{font-family:'Courier New',monospace;font-size:36px;font-weight:900;color:#14532d;}
.s9-final-value .highlight{color:#16a34a;font-size:44px;}

/* Controls Bar & Answer Box */
#answerbox-backdrop{display:none;position:fixed;inset:0;z-index:8400;background:rgba(15,23,42,.45);backdrop-filter:blur(4px);}
#answerbox-backdrop.open{display:block;}
#answerbox-panel{display:flex;flex-direction:column;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) scale(.96);z-index:8500;width:min(480px,94vw);border-radius:18px;background:#fff;border:1px solid #e2e8f0;box-shadow:0 8px 48px rgba(124,58,237,.18);opacity:0;pointer-events:none;transition:all .25s;}
#answerbox-panel.open{opacity:1;pointer-events:auto;transform:translate(-50%,-50%) scale(1);}
.ab-header{display:flex;justify-content:space-between;padding:16px 20px;background:linear-gradient(135deg,#faf5ff,#f0f9ff);border-bottom:1px solid #e2e8f0;}
.ab-header-title{font-size:16px;font-weight:800;color:#1e293b;}
.ab-close-btn{border:none;background:transparent;font-size:16px;cursor:pointer;color:#64748b;}
.ab-body{padding:16px 20px 20px;display:flex;flex-direction:column;}
#ab-user-input{width:100%;padding:12px;border-radius:10px;border:1.5px solid #e2e8f0;font-size:14px;margin:10px 0;}
#ab-submit-btn{width:100%;padding:12px;border-radius:10px;border:none;background:#7c3aed;color:#fff;font-weight:700;cursor:pointer;}
#ab-feedback{display:none;margin-top:14px;padding:12px;border-radius:10px;}
#ab-feedback.show{display:block;}
#ab-feedback.correct{background:#f0fdf4;border:1px solid #bbf7d0;color:#15803d;}
#ab-feedback.wrong{background:#fef2f2;border:1px solid #fecaca;color:#b91c1c;}
.ab-action-row{display:none;gap:10px;margin-top:10px;}
.ab-action-row.show{display:flex;}
#ab-retry-btn{flex:1;padding:10px;border-radius:8px;border:1px solid #e2e8f0;background:#f8fafc;cursor:pointer;}
#qanim-controls-bar{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);z-index:7000;display:flex;background:rgba(255,255,255,.98);border-radius:16px;padding:10px 14px;box-shadow:0 6px 36px rgba(124,58,237,.18);}
.qanim-ctrl-btn{display:flex;gap:5px;padding:8px 15px;border-radius:10px;border:1.5px solid #e2e8f0;background:#f8fafc;font-weight:700;cursor:pointer;color:#334155;}
"""

_HTML_SKELETON = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{TITLE}</title>
  <style>
{BASE_CSS}
{MODALS_CSS}
  </style>
</head>
<body>
  <div class="page-header">
    <div class="page-chip">Interactive Animation</div>
  </div>
  <div class="dashboard">
    <div class="question-banner">
      <div class="q-label">Problem Statement</div>
      <div class="q-text">{QUESTION}</div>
    </div>
    <div class="svg-container">
      <svg xmlns="http://www.w3.org/2000/svg" id="stage" viewBox="0 0 850 478" preserveAspectRatio="xMidYMid slice">
        <g id="layer-canvas-bg"><rect width="100%" height="100%" fill="url(#grid)" opacity="0.5"/></g>
        {SVG_CONTENT}
      </svg>
    </div>
    <div class="control-panel">
      <div class="step-indicator" id="dots">
        <div class="step-dot" id="dot-step1" onclick="goToStep(0)">Step 1</div><div class="step-connector"></div>
        <div class="step-dot" id="dot-step2" onclick="goToStep(1)">Step 2</div><div class="step-connector"></div>
        <div class="step-dot" id="dot-step3" onclick="goToStep(2)">Step 3</div><div class="step-connector"></div>
        <div class="step-dot" id="dot-step4" onclick="goToStep(3)">Step 4</div><div class="step-connector"></div>
        <div class="step-dot" id="dot-step5" onclick="goToStep(4)">Step 5</div><div class="step-connector"></div>
        <div class="step-dot" id="dot-step6" onclick="goToStep(5)">Step 6</div><div class="step-connector"></div>
        <div class="step-dot" id="dot-step7" onclick="if(typeof window.qanim_showScene6==='function')window.qanim_showScene6()">7 · Formula</div><div class="step-connector"></div>
        <div class="step-dot" id="dot-step8" onclick="if(typeof window.qanim_showScene8==='function')window.qanim_showScene8()">8 · Subst.</div><div class="step-connector"></div>
        <div class="step-dot" id="dot-step9" onclick="if(typeof window.qanim_showScene9==='function')window.qanim_showScene9()">9 · Answer</div>
        <div class="step-label" id="step-label">Step 1 of 9</div>
      </div>
      <div class="step-progress-wrap"><div class="step-progress-bar" id="step-bar"></div></div>
      <div class="info-box">
        <h3 id="info-title">Step 1</h3>
        <div class="badges" id="info-badges"></div>
        <div class="info-desc" id="info-desc"></div>
      </div>
      <div class="actions">
        <button class="btn-secondary" onclick="resetAnim()">&#x21BA; Restart</button>
        <button class="btn-secondary qanim-prev-btn" id="btn-prev" onclick="prevStep()" disabled>&#x25C0; Previous Step</button>
        <button class="btn-primary" id="btn-next" onclick="nextStep()">Next Step &#x25B6;</button>
      </div>
    </div>
  </div>

  {MODALS_HTML}

  <div id="qanim-controls-bar" role="toolbar" aria-label="QAnim Controls">
    <button class="qanim-ctrl-btn" id="answerbox-ctrl-btn" title="Check your answer">
      <span>&#x270F;&#xFE0F;</span><span class="ctrl-label">Answer Box</span>
    </button>
  </div>

<script>
var stepsData = {STEPS_DATA};
window.stepsData = stepsData;
var currentStep = 0; window.currentStep = 0;
var totalSteps = stepsData.length > 0 ? stepsData.length - 1 : 5; window.totalSteps = totalSteps;

function applyStep(idx) {
  currentStep = idx; window.currentStep = idx;
  var bar = document.getElementById('step-bar'); if(bar) bar.style.width = ((idx+1)/9*100) + '%';
  var lbl = document.getElementById('step-label'); if(lbl) lbl.textContent = 'Step ' + (idx+1) + ' of 9';
  document.querySelectorAll('.step-dot').forEach(function(d, i) {
    d.classList.remove('active', 'done');
    if (i === idx) d.classList.add('active'); else if (i < idx) d.classList.add('done');
  });
  var sd = stepsData[idx] || {};
  var it = document.getElementById('info-title'); if(it) it.textContent = sd.title || '';
  var idesc = document.getElementById('info-desc'); if(idesc) idesc.textContent  = sd.desc  || '';
  var br = document.getElementById('info-badges');
  if (br) br.innerHTML = (sd.badges || []).join('');
  var lo = sd.layerOpacities || {};
  Object.keys(lo).forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.style.opacity = lo[id];
  });
  var bp = document.getElementById('btn-prev'); if(bp) bp.disabled = (idx === 0);
  var nb = document.getElementById('btn-next');
  if (nb) nb.textContent = (idx === totalSteps) ? 'Step 7: Formula \u25b6' : 'Next Step \u25B6';
}
function nextStep() {
  if (currentStep < totalSteps) {
    currentStep++; window.currentStep = currentStep; applyStep(currentStep);
  } else {
    if (typeof window.qanim_showScene6 === 'function') { window.qanim_showScene6(); }
  }
}
function prevStep() {
  if (currentStep > 0) {
    var svgC = document.querySelector('.svg-container'); if (svgC) { svgC.style.transition = 'none'; svgC.style.opacity = '1'; }
    currentStep--; window.currentStep = currentStep; applyStep(currentStep);
  }
}
function goToStep(idx) {
  if (idx >= 0 && idx <= totalSteps) {
    var svgC = document.querySelector('.svg-container'); if (svgC) { svgC.style.transition = 'none'; svgC.style.opacity = '1'; }
    ['qanim-scene6-overlay','qanim-scene7-overlay','qanim-scene9-overlay'].forEach(id => {
      var o = document.getElementById(id); if(o) o.classList.remove('qanim-scene-visible');
    });
    var bd = document.getElementById('qanim-scene-modal-backdrop'); if(bd) bd.classList.remove('qanim-scene-visible');
    currentStep = idx; window.currentStep = idx; applyStep(currentStep);
  }
}
function resetAnim() {
  ['qanim-scene6-overlay','qanim-scene7-overlay','qanim-scene9-overlay'].forEach(id => {
    var o = document.getElementById(id); if(o) o.classList.remove('qanim-scene-visible');
  });
  var bd = document.getElementById('qanim-scene-modal-backdrop'); if(bd) bd.classList.remove('qanim-scene-visible');
  var svgC = document.querySelector('.svg-container'); if (svgC) { svgC.style.transition = 'none'; svgC.style.opacity = '1'; }
  currentStep = 0; window.currentStep = 0; applyStep(currentStep);
}
window.nextStep = nextStep; window.prevStep = prevStep; window.applyStep = applyStep; window.goToStep = goToStep; window.resetAnim = resetAnim;
window.addEventListener('DOMContentLoaded', function() { applyStep(0); });
</script>
{MODALS_JS}
</body>
</html>"""

# ===========================================================================
#  BUILDER SYSTEM
# ===========================================================================
_ANIMATION_BUILDER_SYSTEM = """You are QAnim SVG & Data Generator.
Given a JSON scene script and a question, produce a JSON object containing the raw SVG layers and the steps data.

OUTPUT FORMAT (Return ONLY valid JSON):
{
  "svg_layers": "<g id=\\"layer-frame\\">... detailed SVG shapes ...</g><g id=\\"layer-xyz\\">...</g>",
  "stepsData": [
    {
      "title": "Step 1: ...", "desc": "...",
      "badges": ["<span class=\\"badge badge-cyan\\">...</span>"],
      "layerOpacities": { "layer-frame": 1, "layer-xyz": 0 }
    }
  ]
}

CRITICAL RULES:
- You MUST output exactly 6 objects in `stepsData`. DO NOT TRUNCATE.
- Provide engaging titles and descriptions.
- Badges must use `badge-cyan`, `badge-orange`, or `badge-green`.
"""

class GeminiAnimationBuilder:
    @classmethod
    def build(cls, question: str, scene_script: dict, sol: dict, topic: str = "ENGINEERING") -> str:
        if _gemini_client is None: return RecoveryEngine.fallback_html(question, "Gemini client not available")
        MAX_ATTEMPTS = 4
        last_err = ""
        
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                QAnimLogger.info("AnimBuilder", f"Build attempt {attempt}/{MAX_ATTEMPTS}...")
                prompt = f"Question:\n\"\"\"{question[:1200]}\"\"\"\n\nScene Script (JSON):\n{json.dumps({'topic': topic, 'steps': scene_script.get('steps', []), 'svg_components': scene_script.get('svg_components', {})}, ensure_ascii=False, indent=2)}\n\nCRITICAL: Output exactly {len(scene_script.get('steps', []))} steps in stepsData array."
                
                raw = GeminiSolutionGenerator._call_gemini(prompt, _ANIMATION_BUILDER_SYSTEM, max_tokens=MAX_TOK)
                data = json.loads(_sanitize_json_str(raw))
                
                svg_content = data.get("svg_layers", "")
                steps_data = data.get("stepsData", [])
                
                if len(steps_data) < 4: raise ValueError("Truncated stepsData.")

                # Build Modals HTML
                modals_html = cls._build_modals(scene_script, sol)
                
                # Answer Box Logic
                ans_targets = json.dumps([{"label": "Final Answer", "value": sol.get("final_answer", "?"), "insight": "Apply formula"}])
                ab_html = f"""
                <div id="answerbox-backdrop"></div>
                <div id="answerbox-panel">
                  <div class="ab-header"><div class="ab-header-title">&#x270F;&#xFE0F; Answer Box</div><button class="ab-close-btn" id="ab-close-btn">&#x2715;</button></div>
                  <div class="ab-body">
                    <input type="text" id="ab-user-input" placeholder="Type answer here..." />
                    <button id="ab-submit-btn">Submit Answer</button>
                    <div id="ab-feedback"><span id="ab-feedback-verdict"></span></div>
                    <div class="ab-action-row" id="ab-action-row"><button id="ab-retry-btn">Try Again</button></div>
                  </div>
                </div>
                <script type="application/json" id="__answer_targets__">{ans_targets}</script>
                """

                # JavaScript for Modals & Answerbox
                modals_js = f"""
                {ab_html}
                <script>
                function _syncDots(stepIndex) {{
                  document.getElementById('step-bar').style.width = ((stepIndex+1)/9*100) + '%';
                  document.getElementById('step-label').textContent = 'Step ' + (stepIndex+1) + ' of 9';
                  document.querySelectorAll('.step-dot').forEach((d, i) => {{
                    d.classList.remove('active', 'done');
                    if (i === stepIndex) d.classList.add('active'); else if (i < stepIndex) d.classList.add('done');
                  }});
                }}
                window.qanim_showScene6 = function() {{
                  ['qanim-scene7-overlay','qanim-scene9-overlay'].forEach(id => {{ var o=document.getElementById(id); if(o)o.classList.remove('qanim-scene-visible'); }});
                  document.getElementById('qanim-scene-modal-backdrop').classList.add('qanim-scene-visible');
                  document.getElementById('qanim-scene6-overlay').classList.add('qanim-scene-visible');
                  document.querySelector('.svg-container').style.opacity = '0';
                  _syncDots(6);
                }};
                window.qanim_showScene8 = window.qanim_showScene7 = function() {{
                  ['qanim-scene6-overlay','qanim-scene9-overlay'].forEach(id => {{ var o=document.getElementById(id); if(o)o.classList.remove('qanim-scene-visible'); }});
                  document.getElementById('qanim-scene-modal-backdrop').classList.add('qanim-scene-visible');
                  document.getElementById('qanim-scene7-overlay').classList.add('qanim-scene-visible');
                  _syncDots(7);
                }};
                window.qanim_showScene9 = function() {{
                  ['qanim-scene6-overlay','qanim-scene7-overlay'].forEach(id => {{ var o=document.getElementById(id); if(o)o.classList.remove('qanim-scene-visible'); }});
                  document.getElementById('qanim-scene-modal-backdrop').classList.add('qanim-scene-visible');
                  document.getElementById('qanim-scene9-overlay').classList.add('qanim-scene-visible');
                  _syncDots(8);
                }};
                
                // Answer Box Logic
                (function(){{
                  var abOpen=false, _targets=[];
                  function _el(id){{return document.getElementById(id);}}
                  function openAnswerBox(){{ 
                    var tag=_el('__answer_targets__'); if(tag) _targets=JSON.parse(tag.textContent);
                    var bd=_el('answerbox-backdrop'),pn=_el('answerbox-panel');
                    if(bd)bd.classList.add('open'); if(pn)pn.classList.add('open'); abOpen=true;
                    var inp=_el('ab-user-input'); if(inp){{inp.value=''; inp.disabled=false; inp.focus();}}
                    var fb=_el('ab-feedback'); if(fb)fb.className='';
                    var ar=_el('ab-action-row'); if(ar)ar.className='ab-action-row';
                    var sb=_el('ab-submit-btn'); if(sb)sb.style.display='';
                  }}
                  function closeAnswerBox(){{ 
                    var bd=_el('answerbox-backdrop'),pn=_el('answerbox-panel');
                    if(bd)bd.classList.remove('open'); if(pn)pn.classList.remove('open'); abOpen=false;
                  }}
                  window.openAnswerBox=openAnswerBox; window.closeAnswerBox=closeAnswerBox;
                  document.addEventListener('DOMContentLoaded', function(){{
                    var btn=_el('answerbox-ctrl-btn'); if(btn) btn.addEventListener('click', function(e){{e.stopPropagation(); openAnswerBox();}});
                    var cb=_el('ab-close-btn'); if(cb) cb.addEventListener('click', closeAnswerBox);
                    var sb=_el('ab-submit-btn'); if(sb) sb.addEventListener('click', function(){{
                      var inp=_el('ab-user-input'); var val=inp?inp.value.trim():'';
                      var tgt=_targets[0]||{{}};
                      var un=(val.match(/[-+]?\\d*\\.?\\d+/g)||[]).map(parseFloat);
                      var cn=(tgt.value.match(/[-+]?\\d*\\.?\\d+/g)||[]).map(parseFloat);
                      var verdict = 'wrong';
                      if(un.length>0 && cn.length>0 && Math.abs(un[0]-cn[0])/(Math.abs(cn[0])+1e-12) < 0.05) verdict='correct';
                      var fb=_el('ab-feedback'); if(fb) {{ fb.className='show '+verdict; fb.querySelector('#ab-feedback-verdict').textContent = verdict==='correct'?'Correct!':'Incorrect.'; }}
                      if(inp) inp.disabled=true;
                      var ar=_el('ab-action-row'); if(ar) ar.className='ab-action-row show';
                      if(sb) sb.style.display='none';
                    }});
                    var rb=_el('ab-retry-btn'); if(rb) rb.addEventListener('click', openAnswerBox);
                  }});
                }})();
                </script>
                """
                
                html = _HTML_SKELETON.replace("{TITLE}", html_module.escape(scene_script.get("title", "Animation")))
                html = html.replace("{QUESTION}", html_module.escape(question))
                html = html.replace("{SVG_CONTENT}", svg_content)
                html = html.replace("{STEPS_DATA}", json.dumps(steps_data, ensure_ascii=False))
                html = html.replace("{BASE_CSS}", _BASE_CSS)
                html = html.replace("{MODALS_CSS}", _MODALS_CSS)
                html = html.replace("{MODALS_HTML}", modals_html)
                html = html.replace("{MODALS_JS}", modals_js)
                
                return html
            except Exception as e:
                last_err = _err_msg(e)
                if attempt < MAX_ATTEMPTS: continue
        return RecoveryEngine.fallback_html(question, f"Failed: {last_err}")

    @classmethod
    def _build_modals(cls, scene: dict, sol: dict) -> str:
        # Scene 6 (Formula)
        fd = scene.get("formula_data", {})
        vars_html = ""
        colors = {"blue":"s6v-blue", "green":"s6v-green", "orange":"s6v-orange", "red":"s6v-red", "purple":"s6v-purple", "teal":"s6v-teal"}
        for v in fd.get("variables", sol.get("variables", [])):
            c = colors.get(v.get("color", "blue"), "s6v-blue")
            vars_html += f"<div class='s6-var-box {c}'><div class='s6-var-arrow'></div><div class='s6-var-inner'><span class='s6-var-sym'>{html_module.escape(v.get('symbol','?'))}</span><span class='s6-var-name'>{html_module.escape(v.get('name',''))}</span><span class='s6-var-val'>{html_module.escape(v.get('value','?'))} {html_module.escape(v.get('unit',''))}</span></div></div>"
            
        s6 = f"""
        <div id="qanim-scene-modal-backdrop"></div>
        <div id="qanim-scene6-overlay" class="qanim-overlay">
          <div class="s6-card">
            <div class="s6-title-bar"><h2>Step 7 &mdash; Main Formula</h2></div>
            <div class="s6-body">
              <div class="s6-formula-box">
                <div class="s6-formula-main">{html_module.escape(fd.get("formula_text", sol.get("formula", "")))}</div>
                <div class="s6-formula-sublabel">Governing Equation</div>
              </div>
              <div class="s6-vars-row">{vars_html}</div>
            </div>
            <div class="s6-nav-row">
              <button class="btn-secondary" onclick="goToStep(window.totalSteps)">&#x2190; Back to Step 6</button>
              <button class="btn-primary" onclick="qanim_showScene8()">Step 8: Substitution &#x25B6;</button>
            </div>
          </div>
        </div>"""

        # Scene 7 (Substitution)
        sd = scene.get("substitution_data", {})
        given_html = "".join([f"<div class='s7-given-item'><strong>{html_module.escape(str(g))}</strong></div>" for g in sd.get("given_list", [])])
        approach_html = "".join([f"<div class='s7-approach-step'><div class='s7-approach-step-num'>{i+1}.</div><div>{html_module.escape(str(a))}</div></div>" for i, a in enumerate(sd.get("approach_steps", []))])
        
        s7 = f"""
        <div id="qanim-scene7-overlay" class="qanim-overlay">
          <div class="s7-card">
            <div class="s7-title-bar"><h2>Step 8 &mdash; Step-by-Step Substitution</h2></div>
            <div class="s7-body-cols">
              <div class="s7-left-col">
                <div class="s7-system-label">System Diagram</div>
                <div class="s7-formula-result-bar"><div class="s7-formula-result-text">{html_module.escape(sd.get("result_bar", sol.get("final_answer","")))}</div></div>
              </div>
              <div class="s7-right-col">
                <div><div class="s7-given-section-title">Given Parameters</div><div class="s7-given-list">{given_html}</div></div>
                <div><div class="s7-approach-section-title">Substituting Values</div><div class="s7-approach-list">{approach_html}</div></div>
              </div>
            </div>
            <div class="s7-nav-row">
              <button class="btn-secondary" onclick="qanim_showScene6()">&#x2190; Back to Step 7</button>
              <button class="btn-primary" onclick="qanim_showScene9()">Step 9: Final Answer &#x25B6;</button>
            </div>
          </div>
        </div>"""

        # Scene 9 (Answer)
        fad = scene.get("final_answer_data", {})
        chain = fad.get("substitution_chain", sol.get("substitution_chain", []))
        chain_html = ""
        for i, row in enumerate(chain):
            cls = " s9-sub-row final" if i == len(chain)-1 else " s9-sub-row"
            chain_html += f"<div class='{cls}'><div class='s9-sub-num'>{row.get('num', i+1)}</div><div class='s9-sub-eq'>{html_module.escape(str(row.get('eq','')))}</div></div>"

        s9 = f"""
        <div id="qanim-scene9-overlay" class="qanim-overlay">
          <div class="s9-card">
            <div class="s9-title-bar"><h2>&#x2705; Step 9 &mdash; Final Answer</h2></div>
            <div class="s9-body">
              <div class="s9-formula-recap"><div class="s9-formula-recap-eq">{html_module.escape(fad.get("formula_recap", sol.get("formula", "")))}</div></div>
              <div class="s9-sub-chain">{chain_html}</div>
              <div class="s9-final-box">
                <div class="s9-final-value"><span class="highlight">{html_module.escape(fad.get("answer_value", sol.get("final_answer","")))}</span></div>
                <div class="s9-final-unit">{html_module.escape(fad.get("answer_unit", ""))}</div>
              </div>
            </div>
            <div class="s9-nav-row">
              <button class="btn-secondary" onclick="qanim_showScene8()">&#x2190; Back to Step 8</button>
              <button class="btn-primary" onclick="resetAnim()">&#x21BA; Restart Animation</button>
            </div>
          </div>
        </div>"""

        return s6 + s7 + s9

class RecoveryEngine:
    @classmethod
    def fallback_html(cls, question: str, reason: str) -> str:
        return f"<html><body><h3>Error generating animation</h3><p>{html_module.escape(reason)}</p></body></html>"

async def generate_animation_html(question: str) -> str:
    if not question.strip(): return RecoveryEngine.fallback_html("(empty)", "Question was empty")
    
    scene_task = GeminiSceneAnalyzer.analyze_async(question)
    sol_task   = GeminiSolutionGenerator.generate_async(question)
    scene_script, sol = await asyncio.gather(scene_task, sol_task, return_exceptions=True)
    
    if isinstance(scene_script, BaseException): scene_script = GeminiSceneAnalyzer._fallback_script(question)
    if isinstance(sol, BaseException): sol = dict(GeminiSolutionGenerator._FALLBACK)
    
    html = await GeminiAnimationBuilder.build_async(question, scene_script, sol, "ENGINEERING")
    return html

def generate_animation(question: str) -> str:
    loop = asyncio.get_event_loop()
    if loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, generate_animation_html(question)).result(timeout=PIPELINE_TIMEOUT + 30)
    else: return loop.run_until_complete(asyncio.wait_for(generate_animation_html(question), timeout=PIPELINE_TIMEOUT + 30))

async def generate_question_animation(question: str) -> dict:
    """Public async entry point imported by main.py."""
    question = (question or "").strip()
    if not question: raise ValueError("'question' field cannot be empty")
    QAnimLogger.info("generate_question_animation", f"question={question[:80]!r}")
    html = await generate_animation_html(question)
    return { "title": question[:80], "explanation": "9-step animated solution", "animation_code": html }

if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "Find the heat loss of a 2m² plate at 150°C in 30°C air with h=25 W/m²K."
    out = sys.argv[2] if len(sys.argv) > 2 else "output.html"
    print(f"Generating for: {q[:60]}...")
    html_out = generate_animation(q)
    with open(out, "w", encoding="utf-8") as f: f.write(html_out)
    print(f"Saved to {out}. V3 Architecture Applied.")
