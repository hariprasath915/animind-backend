"""
main.py  —  GenZet / Animind Backend  v6.0  (Supabase + Normalized Schema)
===========================================================================
What changed from v5.1:
  - Added all new /sync/* normalized endpoints to the health/root listing
    (subjects, cos, topics, vault/entries, files/upload, /sync/all)
  - Updated endpoint list in root + health to reflect v6 sync routes
  - Everything else is IDENTICAL to v5.1 (CORS, keep-alive, AI endpoints,
    auth, admin — nothing removed, nothing broken)

Entry point (Railway / Render):
    uvicorn main:app --host 0.0.0.0 --port $PORT

Environment variables:
    ANTHROPIC_API_KEY        — sk-ant-... key for AI generation
    SUPABASE_URL             — https://<project-id>.supabase.co
    SUPABASE_ANON_KEY        — anon/public key (auth_routes sign-up/sign-in)
    SUPABASE_SERVICE_KEY     — service-role key (backend-only DB CRUD)
    SUPABASE_JWT_SECRET      — Supabase Dashboard → Settings → API → JWT Secret
    ADMIN_SECRET_TOKEN       — any long random string for /admin/* endpoints
    DEBUG_CORS               — "true" | "false"  (allow ALL origins, dev only)
    EXTRA_ORIGINS            — comma-separated extra Vercel preview URLs
    KEEP_ALIVE_INTERVAL      — seconds between self-pings (default 600)
"""

import sys
import io
import os
import re
import uuid
import asyncio
import json
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from contextlib import asynccontextmanager
import httpx

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

# ── Auth + Sync ───────────────────────────────────────────────────────────────
from auth_routes import router as auth_router                          # pyrefly: ignore [missing-import]
from sync_routes import router as sync_router                          # pyrefly: ignore [missing-import]

# ── Admin ─────────────────────────────────────────────────────────────────────
from admin_router import router as admin_router_obj, install_error_handler  # pyrefly: ignore [missing-import]

# ── AI modules ────────────────────────────────────────────────────────────────
from claude_client import (                                            # pyrefly: ignore [missing-import]
    generate_animation,
    generate_genzet_book_content,
    subtopics_json_to_genzet_args,
    generate_ultimate_learning_content,
)
from simulation import generate_simulation                               # pyrefly: ignore [missing-import]
from pdf_handler import (                                              # pyrefly: ignore [missing-import]
    extract_pdf_text,
    find_subtopics_in_pdf,
    build_subtopics_json,
)
from q_animation import generate_question_animation                    # pyrefly: ignore [missing-import]

try:
    from sub_topics import process_subtopics_json
    SUB_TOPICS_AVAILABLE = True
    print("[INFO]  sub_topics.py loaded OK")
except ImportError:
    SUB_TOPICS_AVAILABLE = False
    print("[WARNING] sub_topics.py not found — falling back to pdf_handler output")

# ── Env flags ─────────────────────────────────────────────────────────────────
DEBUG_CORS           = os.getenv("DEBUG_CORS", "false").lower() == "true"
KEEP_ALIVE_INTERVAL  = int(os.getenv("KEEP_ALIVE_INTERVAL", "600"))


# ══════════════════════════════════════════════════════════════════════════════
# KEEP-ALIVE PINGER  (prevents free-tier spin-down on Railway / Render)
# ══════════════════════════════════════════════════════════════════════════════

async def _keep_alive_pinger():
    self_url   = os.getenv(
        "RENDER_EXTERNAL_URL",
        "https://animind-backend-production-2.up.railway.app",
    )
    health_url = f"{self_url.rstrip('/')}/health"
    print(f"[KEEP-ALIVE] ✅ Pinger started → {health_url} every {KEEP_ALIVE_INTERVAL}s")

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            await asyncio.sleep(KEEP_ALIVE_INTERVAL)
            try:
                r = await client.get(health_url)
                print(f"[KEEP-ALIVE] ✅ Ping OK ({r.status_code})")
            except Exception as e:
                print(f"[KEEP-ALIVE] ⚠ Ping failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# APP LIFESPAN
# ══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[STARTUP] ✅ GenZet / Animind v6.0 ready (Supabase normalized schema)")
    pinger = asyncio.create_task(_keep_alive_pinger())
    yield
    pinger.cancel()
    try:
        await pinger
    except asyncio.CancelledError:
        pass
    print("[SHUTDOWN] GenZet / Animind shutting down.")


# ══════════════════════════════════════════════════════════════════════════════
# FASTAPI APP
# ══════════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="GenZet / Animind API",
    version="6.0.0",
    description="AI-powered educational animation platform — Supabase normalized schema",
    lifespan=lifespan,
)


# ══════════════════════════════════════════════════════════════════════════════
# CORS
# ══════════════════════════════════════════════════════════════════════════════
# RULE: allow_origins=["*"] + allow_credentials=True is INVALID per the CORS
# spec — browsers reject the preflight. Use explicit origins + allow_origin_regex.
#
# Add new preview URLs without redeploying:
#   EXTRA_ORIGINS=https://your-preview.vercel.app

EXTRA_ORIGINS = [
    o.strip()
    for o in os.getenv("EXTRA_ORIGINS", "").split(",")
    if o.strip()
]

BASE_ORIGINS = [
    "https://haezet.com",
    "https://genzet-app-git-main-hari-prasath-genzet-web-project.vercel.app",
    "https://animind-gold.vercel.app",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8000",
    "http://localhost:8080",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5500",
    "http://127.0.0.1:8000",
    "http://127.0.0.1:8080",
    "null",   # file:// origin — lets index.html open directly from disk during dev
] + EXTRA_ORIGINS

# Regex covers ALL *.vercel.app preview deployments (not just genzet/animind prefixes)
# so that any Vercel preview URL can reach the backend without CORS rejection.
_CORS_ORIGIN_REGEX = r"https://[\w-]+\.vercel\.app"

if DEBUG_CORS:
    print("[CORS] ⚠ DEBUG_CORS=true — allowing ALL origins (dev only, no credentials)")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,   # wildcard + credentials is NOT allowed by spec
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    print(f"[CORS] Active origins: {BASE_ORIGINS}")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=BASE_ORIGINS,
        allow_origin_regex=_CORS_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
        allow_headers=["*"],   # Starlette echoes requested headers — safe with credentials
        max_age=600,           # cache preflight for 10 min — reduces OPTIONS round-trips
    )


# ══════════════════════════════════════════════════════════════════════════════
# ROUTERS
# ══════════════════════════════════════════════════════════════════════════════

app.include_router(auth_router)       # /auth/*
app.include_router(sync_router)       # /sync/*  (v6 normalized + legacy)
app.include_router(admin_router_obj)  # /admin/*

# Global error handler → feeds /admin/errors ring
install_error_handler(app)


# ══════════════════════════════════════════════════════════════════════════════
# CORS-SAFE GLOBAL EXCEPTION HANDLER
# When any unhandled exception causes a 500, FastAPI’s default error
# response bypasses the CORSMiddleware and reaches the browser WITHOUT
# the Access-Control-Allow-Origin header.  The browser then reports a
# “CORS Missing Allow Origin” error instead of the real 500 message,
# making debugging impossible.  This handler injects the correct CORS
# header into every error response so the real error detail is visible.
# ══════════════════════════════════════════════════════════════════════════════

@app.exception_handler(Exception)
async def _cors_safe_500_handler(request: Request, exc: Exception):
    """
    Catch-all: return a JSON 500 that always carries the right
    Access-Control-Allow-Origin header so browsers can read the error.
    """
    import traceback
    print(f"[ERROR] Unhandled exception on {request.method} {request.url.path}:")
    traceback.print_exc()

    origin = request.headers.get("origin", "")
    cors_headers: dict = {}
    if origin:
        # Check explicit list first, then regex
        if origin in BASE_ORIGINS or re.match(_CORS_ORIGIN_REGEX, origin):
            cors_headers["Access-Control-Allow-Origin"]      = origin
            cors_headers["Access-Control-Allow-Credentials"] = "true"
            cors_headers["Vary"]                             = "Origin"

    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {exc}"},
        headers=cors_headers,
    )


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH + ROOT
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health(request: Request):
    """
    GET  → full JSON status payload
    HEAD → 200 OK with no body (used by Railway / Render health checks)
    """
    if request.method == "HEAD":
        return JSONResponse(content=None, status_code=200)

    return {
        "status":               "ok",
        "version":              "6.0.0",
        "backend":              "Supabase",
        "schema":               "normalized-v3",
        "debug_cors":           DEBUG_CORS,
        "keep_alive_interval":  KEEP_ALIVE_INTERVAL,
        "sub_topics_module":    SUB_TOPICS_AVAILABLE,
        "endpoints": {
            "auth": {
                "register":  "POST /auth/register",
                "login":     "POST /auth/login",
                "logout":    "POST /auth/logout",
                "verify":    "GET  /auth/verify",
                "me":        "GET  /auth/me",
            },
            "sync": {
                # ── v6 normalized (new) ───────────────────────────────
                "all_data":       "GET    /sync/all                    → full pull on login",
                "items_save":     "POST   /sync/items                  → save generated item",
                "items_list":     "GET    /sync/items                  → list saved items",
                "item_update":    "PUT    /sync/items/{id}             → update item",
                "item_delete":    "DELETE /sync/items/{id}             → soft-delete item",
                "subjects_list":  "GET    /sync/subjects               → full tree",
                "subject_create": "POST   /sync/subjects               → create subject",
                "subject_update": "PUT    /sync/subjects/{id}          → update subject",
                "subject_delete": "DELETE /sync/subjects/{id}          → cascade delete",
                "co_create":      "POST   /sync/cos                    → create CO",
                "co_update":      "PUT    /sync/cos/{id}               → update CO",
                "co_delete":      "DELETE /sync/cos/{id}               → cascade delete",
                "topic_create":   "POST   /sync/topics                 → create topic",
                "topic_update":   "PUT    /sync/topics/{id}            → update/attach HTML",
                "topic_delete":   "DELETE /sync/topics/{id}            → delete topic",
                "vault_list":     "GET    /sync/vault/entries          → list videos",
                "vault_add":      "POST   /sync/vault/entries          → add video entry",
                "vault_delete":   "DELETE /sync/vault/entries/{id}     → delete + storage",
                "file_upload":    "POST   /sync/files/upload           → upload to Storage",
                "file_delete":    "DELETE /sync/files/delete           → delete from Storage",
                "subject_share":  "GET    /sync/share/{token}          → public (no-auth) CO viewer",
                # ── legacy (kept for migration window) ───────────────
                "legacy_pull":    "GET    /sync/animations             [LEGACY]",
                "legacy_push":    "POST   /sync/animations             [LEGACY]",
                "legacy_batch":   "POST   /sync/animations/batch       [LEGACY]",
                "legacy_courses": "GET|PUT /sync/courses               [LEGACY]",
                "legacy_vault":   "GET|PUT /sync/vault                 [LEGACY]",
            },
            "ai": {
                "animation":          "POST /generate-animation",
                "question_animation": "POST /generate-question-animation",
                "book_mode":          "POST /generate-from-book",
                "topic_content":      "POST /generate-topic-content",
                "simulation":         "POST /generate-simulation",
            },
            "admin": {
                "errors": "GET /admin/errors  (X-Admin-Token header required)",
                "users":  "GET /admin/users   (X-Admin-Token header required)",
            },
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST MODELS
# ══════════════════════════════════════════════════════════════════════════════

class AnimationRequest(BaseModel):
    prompt: str


class QuestionAnimRequest(BaseModel):
    question: str


class SimulationRequest(BaseModel):
    topic: str
    mode: Optional[str] = None   # e.g. 'physics', 'chemistry' (informational only)


class SkillContentRequest(BaseModel):
    topic:        str
    subject:      Optional[str]  = "Engineering"
    retry_failed: Optional[bool] = True


# ══════════════════════════════════════════════════════════════════════════════
# AI GENERATION ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/generate-animation")
async def create_animation(request: AnimationRequest):
    """Generate a full 8-section HTML animation page for the topic."""
    if not request.prompt or not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    try:
        result = await generate_animation(request.prompt.strip())
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# QUESTION-ANIMATION JOB QUEUE
# ══════════════════════════════════════════════════════════════════════════════
# generate_question_animation() can legitimately run past the ~120s timeout
# that Vercel enforces on the /api/* rewrite proxying to this service.
# Running it inline in the POST handler means any request that crosses that
# line gets killed upstream. Fix: the POST handler only ENQUEUES the job and
# returns a job_id in milliseconds. The actual generation runs as a
# background asyncio task. The frontend polls
# /generate-question-animation/status/{job_id} for the result.
#
# NOTE: this in-memory dict works because this service currently runs as a
# single Railway instance/process. If you ever scale to multiple replicas,
# move JOBS to something shared (e.g. Redis) — a poll can otherwise land on
# an instance that never ran the job and 404.
JOBS: dict[str, dict] = {}
JOB_TTL_SECONDS = 60 * 60  # drop finished jobs after 1 hour

# ── Strong references to fire-and-forget background tasks ──────────────────
# asyncio.create_task() returns a Task that the event loop only holds a WEAK
# reference to. If nothing else keeps a strong reference, the task can be
# garbage-collected at any point before it finishes — silently, with no
# exception, no log line, nothing. The job's JOBS[job_id]["status"] just
# freezes at "pending" forever, and the frontend polls a healthy 200 for
# 10 minutes before giving up. This bit us in production. Fix: keep every
# background task alive in this set until it's actually done, and log
# anything that escapes it instead of swallowing it silently.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn_background_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                print(f"[BACKGROUND TASK] ⚠ unhandled exception: {exc!r}")

    task.add_done_callback(_on_done)
    return task


def _cleanup_old_jobs():
    now = asyncio.get_event_loop().time()
    stale = [
        jid for jid, j in JOBS.items()
        if j.get("finished_at") is not None and now - j["finished_at"] > JOB_TTL_SECONDS
    ]
    for jid in stale:
        del JOBS[jid]


async def _run_question_animation_job(job_id: str, question: str):
    try:
        JOBS[job_id]["status"] = "running"
        result = await generate_question_animation(question)
        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["result"] = result
        JOBS[job_id]["finished_at"] = asyncio.get_event_loop().time()
    except ValueError as ve:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["detail"] = str(ve)
        JOBS[job_id]["finished_at"] = asyncio.get_event_loop().time()
    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["detail"] = f"Question animation generation failed: {e}"
        JOBS[job_id]["finished_at"] = asyncio.get_event_loop().time()
        print(f"[QAnim Job {job_id}] ERROR: {e}")


@app.post("/generate-question-animation")
async def create_question_animation(request: QuestionAnimRequest):
    """
    Enqueue a question-animation generation job and return immediately.
    The frontend polls GET /generate-question-animation/status/{job_id}
    for the result. See the JOB QUEUE comment block above for why.
    """
    question = (request.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="'question' field cannot be empty")

    _cleanup_old_jobs()

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "status": "pending",
        "result": None,
        "detail": None,
        "finished_at": None,
    }
    _spawn_background_task(_run_question_animation_job(job_id, question))

    return {"job_id": job_id}


@app.get("/generate-question-animation/status/{job_id}")
async def get_question_animation_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job_id")
    return {
        "status": job["status"],       # "pending" | "running" | "done" | "error"
        "result": job["result"],
        "detail": job["detail"],
    }


# ── Simulation file store paths ───────────────────────────────────────────────
# Pre-built experiment HTML files (existing)
_SIM_EXPERIMENTS_DIR = Path(__file__).parent / "simulation_experiments"
# Topic-based simulation HTML files (new)
_SIM_TOPICS_DIR = Path(__file__).parent / "topics_simulations"

# Ensure both directories exist so uploads never fail
_SIM_EXPERIMENTS_DIR.mkdir(exist_ok=True)
_SIM_TOPICS_DIR.mkdir(exist_ok=True)


def _icon_for_name(name: str) -> str:
    """Return an emoji icon based on keywords in the simulation filename."""
    n = name.lower()
    if any(k in n for k in ["engine", "motor", "turbine", "gear", "piston"]): return "⚙️"
    if any(k in n for k in ["newton", "force", "motion", "inertia", "momentum"]): return "🍎"
    if any(k in n for k in ["circuit", "electric", "volt", "current", "ohm"]): return "⚡"
    if any(k in n for k in ["wave", "sound", "frequency", "oscillat"]): return "〰️"
    if any(k in n for k in ["optic", "light", "lens", "refract", "reflect"]): return "🔭"
    if any(k in n for k in ["heat", "thermo", "temperature", "energy"]): return "🌡️"
    if any(k in n for k in ["acid", "base", "titrat", "pH", "reaction", "chem"]): return "🧪"
    if any(k in n for k in ["cell", "dna", "bio", "plant", "photo", "osmosis"]): return "🔬"
    if any(k in n for k in ["planet", "orbit", "gravity", "satellite", "solar"]): return "🪐"
    if any(k in n for k in ["math", "function", "graph", "matrix", "vector"]): return "📐"
    if any(k in n for k in ["fluid", "pressure", "flow", "hydraulic"]): return "💧"
    if any(k in n for k in ["magnet", "field", "flux", "induct"]): return "🧲"
    return "🧬"


def _category_for_name(name: str) -> str:
    """Return a subject category based on keywords in the simulation filename."""
    n = name.lower()
    if any(k in n for k in ["engine", "motor", "turbine", "circuit", "electric", "gear", "piston", "hydraulic", "fluid", "pressure"]): return "Engineering"
    if any(k in n for k in ["newton", "force", "motion", "wave", "optic", "light", "heat", "thermo", "magnet", "gravity", "planet", "orbit"]): return "Physics"
    if any(k in n for k in ["acid", "base", "titrat", "pH", "reaction", "chem", "molecule"]): return "Chemistry"
    if any(k in n for k in ["cell", "dna", "bio", "plant", "photo", "osmosis"]): return "Biology"
    if any(k in n for k in ["math", "function", "graph", "matrix", "vector"]): return "Mathematics"
    return "Science"


def _list_sim_folder(folder: Path) -> list:
    """Scan a folder and return metadata for each HTML file found."""
    if not folder.is_dir():
        return []
    results = []
    for f in sorted(folder.glob("*.html")):
        raw_name = f.stem.replace("_", " ").replace("-", " ").title()
        results.append({
            "filename": f.name,
            "name":     raw_name,
            "icon":     _icon_for_name(f.stem),
            "category": _category_for_name(f.stem),
        })
    return results


@app.get("/simulations/list")
async def list_simulations(type: str = "experiments"):
    """
    List pre-built simulation HTML files from a specific folder.
    
    Query param:
      type = "topics"      → scans topics_simulations/
      type = "experiments" → scans simulation_experiments/
    
    Returns: [{filename, name, icon, category}]
    """
    if type == "topics":
        items = _list_sim_folder(_SIM_TOPICS_DIR)
    else:
        items = _list_sim_folder(_SIM_EXPERIMENTS_DIR)
    return {"type": type, "count": len(items), "items": items}


@app.post("/simulations/upload")
async def upload_simulation(
    type: str = Form(...),
    file: UploadFile = File(...)
):
    """
    Upload an HTML simulation file to either topics or experiments.
    """
    if type not in ["topics", "experiments"]:
        raise HTTPException(status_code=400, detail="Invalid type. Must be 'topics' or 'experiments'.")
    
    if not file.filename.endswith(".html") and not file.filename.endswith(".htm"):
        raise HTTPException(status_code=400, detail="Only HTML files are allowed.")

    folder = _SIM_TOPICS_DIR if type == "topics" else _SIM_EXPERIMENTS_DIR
    # Safe filename
    safe_name = re.sub(r'[^a-zA-Z0-9_\-\.\s]', '', file.filename)
    if not safe_name:
        safe_name = "uploaded_sim.html"
    
    target = folder / safe_name
    
    try:
        content = await file.read()
        target.write_bytes(content)
        return {"status": "success", "filename": safe_name, "type": type}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}")


@app.get("/simulations/file")
async def get_simulation_file(type: str = "experiments", file: str = ""):
    """
    Fetch the HTML content of a specific simulation file.
    
    Query params:
      type = "topics" | "experiments"
      file = filename (e.g. "si_engine.html")
    
    Security: path traversal is blocked — only bare filenames are accepted.
    """
    if not file or "/" in file or "\\" in file or ".." in file:
        raise HTTPException(status_code=400, detail="Invalid filename. Only bare filenames like 'si_engine.html' are allowed.")

    folder = _SIM_TOPICS_DIR if type == "topics" else _SIM_EXPERIMENTS_DIR
    target = folder / file

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"Simulation file '{file}' not found in {type} folder.")

    try:
        html = target.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read file: {e}")

    return {"filename": file, "html": html, "source": type}


def _normalize_for_match(text: str) -> str:
    """
    Normalise a string for fuzzy matching:
      • lowercase
      • collapse any run of whitespace / underscores / hyphens to a single space
      • strip leading / trailing whitespace
    """
    text = text.lower()
    text = re.sub(r"[\s_\-]+", " ", text)
    return text.strip()


def _find_cached_simulation(topic: str) -> str | None:
    """
    Search `simulation_experiments/` for an HTML file whose filename
    (minus the .html extension) normalises to a string that *contains*
    the normalised topic — or vice-versa.

    Returns the full HTML content string on a hit, or None on a miss.
    """
    if not _SIM_EXPERIMENTS_DIR.is_dir():
        return None

    needle = _normalize_for_match(topic)

    for html_file in _SIM_EXPERIMENTS_DIR.glob("*.html"):
        # Use the stem (filename without extension) as the candidate key
        candidate = _normalize_for_match(html_file.stem)
        # Match if the needle is contained in the candidate, or the candidate
        # is contained in the needle (handles both "optics lab" ↔ "optics lab experiment" style)
        if needle in candidate or candidate in needle:
            print(f"[SimCache] ✅ Hit: '{topic}' → {html_file.name}")
            try:
                return html_file.read_text(encoding="utf-8")
            except Exception as read_err:
                print(f"[SimCache] ⚠ Could not read {html_file.name}: {read_err}")
                return None

    print(f"[SimCache] ❌ Miss: no experiment file matched '{topic}'")
    return None


@app.post("/generate-simulation")
async def create_simulation(request: SimulationRequest):
    """
    Generate a complete, self-contained interactive HTML5 simulation.

    Flow:
      1. Normalise the topic string.
      2. Search `simulation_experiments/` for a pre-built HTML file whose
         filename (sans extension) matches the topic (case-insensitive,
         whitespace-normalised, substring match).
      3. If found → return the cached HTML immediately (no AI call).
      4. If not found → call generate_simulation from simulation.py.
    """
    topic = (request.topic or "").strip()
    if not topic:
        raise HTTPException(status_code=400, detail="'topic' field cannot be empty")
    if len(topic) > 2000:
        raise HTTPException(status_code=400, detail="Topic too long (max 2000 chars)")

    # ── Step 1: Check the experiment cache ───────────────────────────────────
    cached_html = _find_cached_simulation(topic)
    if cached_html:
        return {
            "title":             topic,
            "category":          "cached",
            "summary":           f"Pre-built experiment loaded for: {topic}",
            "controls_overview": [],
            "key_formula":       "",
            "learning_notes":    [],
            "image_refs":        [],
            "html":              cached_html,
            "engine_version":    "cache",
            "render_status":     "ok",
            "source":            "cache",
        }

    # ── Step 2: Generate via AI pipeline ─────────────────────────────────────
    # Optionally prepend the subject-mode as context hint
    if request.mode and request.mode.lower() not in ("general", ""):
        topic_with_mode = f"{topic} (subject area: {request.mode})"
    else:
        topic_with_mode = topic

    result = await generate_simulation(topic_with_mode)
    result["source"] = "generated"
    # generate_simulation never raises — on failure render_status == "error"
    return result


@app.post("/generate-topic-content")
async def create_topic_content(request: SkillContentRequest):
    """
    Generate comprehensive 10-section educational content as JSON.
    Uses generate_ultimate_learning_content from claude_client.py.
    """
    topic = (request.topic or "").strip()
    if not topic:
        raise HTTPException(status_code=400, detail="'topic' field cannot be empty")
    try:
        result = await generate_ultimate_learning_content(topic=topic)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Topic content generation failed: {e}")


@app.post("/generate-from-book")
async def create_from_book(
    topic:    str           = Form(...),
    file:     UploadFile    = File(...),
    subtopic: Optional[str] = Form(default=None),
):
    """Generate an animation from a PDF/book excerpt (Book Creator tab)."""
    topic    = (topic    or "").strip()
    subtopic = (subtopic or "").strip() or topic

    if not topic:
        raise HTTPException(status_code=400, detail="'topic' field cannot be empty")

    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="No file uploaded")
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail=f"Only PDF files accepted. Got: '{filename}'")

    try:
        pdf_bytes = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read file: {e}")

    if len(pdf_bytes) == 0:
        raise HTTPException(status_code=400, detail="Uploaded PDF is empty")

    print(f"[BOOK]  topic='{topic}'  file='{filename}'  ({len(pdf_bytes):,} bytes)")

    pdf_data = extract_pdf_text(pdf_bytes)
    if not pdf_data["success"]:
        raise HTTPException(
            status_code=400,
            detail=f"Could not read PDF: {pdf_data.get('error', 'Unknown')}",
        )

    full_text  = pdf_data["full_text"]
    word_count = pdf_data["word_count"]

    if word_count < 50:
        raise HTTPException(status_code=400, detail="PDF has no readable text.")

    topic_data       = find_subtopics_in_pdf(full_text, topic)
    pdf_context_json = build_subtopics_json(topic, topic_data)

    pdf_context = (
        f"Main topic: {topic}\nSubtopic focus: {subtopic}\n"
        f"Section headings: {'; '.join(topic_data.get('main_headings', []))}\n"
        f"Subtopics found: {', '.join(topic_data.get('all_subtopics', [])[:10])}\n\n"
        f"--- PDF Content (first 6000 chars) ---\n{full_text[:6000]}"
    )

    subtopics_list = None
    if SUB_TOPICS_AVAILABLE:
        try:
            formatted      = process_subtopics_json(pdf_context_json)
            gz_args        = subtopics_json_to_genzet_args(json.dumps(formatted), subtopic)
            subtopics_list = gz_args.get("subtopics_list") or None
        except Exception as e:
            print(f"[BOOK] ⚠ sub_topics failed: {e}")

    if not subtopics_list:
        grouped = topic_data.get("subtopics_by_query", {})
        for qk, sl in grouped.items():
            if subtopic.lower() in qk.lower():
                subtopics_list = sl or None
                break

    if not subtopics_list:
        subtopics_list = topic_data.get("all_subtopics") or None

    try:
        result = await generate_genzet_book_content(
            topic=topic,
            subtopic=subtopic,
            pdf_context=pdf_context,
            subtopics_list=subtopics_list,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generation failed: {e}")


# ── Direct run ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    _port = int(os.getenv("PORT", "8000"))
    print("=" * 65)
    print(f"  GenZet / Animind API v6.0 — port {_port}")
    print(f"  Schema: normalized-v3 (generated_items, subjects, cos, topics, vault)")
    print("=" * 65)
    uvicorn.run("main:app", host="0.0.0.0", port=_port, reload=True)
