"""
server.py  —  GenZet / Animind Backend  v6.0  (Supabase + Normalized Schema)
=============================================================================
This file is an ALIAS entry point for the same app as main.py.
Some deployment configs (older Railway / Render setups) reference server:app
instead of main:app. Both files import from the same modules.

If your Railway start command uses:   uvicorn main:app   → use main.py
If your Railway start command uses:   uvicorn server:app → use this file

What changed from v4.0 (server.py / Animind edition):
  - Added all new /sync/* normalized endpoints to health listing
  - Added /generate-from-book endpoint (was missing from some server.py builds)
  - Kept ALL original endpoints, CORS config, lifespan, keep-alive pinger

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
    FRONTEND_URL             — e.g. https://genzet-app.vercel.app (for OAuth redirect)
"""

import sys
import io
import os
import asyncio
import json
import uuid
import hashlib

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from contextlib import asynccontextmanager
import httpx

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional

# ── Auth + Sync ───────────────────────────────────────────────────────────────
from auth_routes import router as auth_router
from sync_routes import router as sync_router          # v6 normalized + legacy
# NOTE: passkey_routes.py does not exist as a separate file — passkey endpoints
#       are defined inline below. Importing it crashed the server at startup.

# ── Admin ─────────────────────────────────────────────────────────────────────
from admin_router import router as admin_router, install_error_handler

# ── AI modules ────────────────────────────────────────────────────────────────
from claude_client import (
    generate_animation,
    generate_genzet_book_content,
    subtopics_json_to_genzet_args,
    generate_ultimate_learning_content,
)
from simulation import generate_simulation   # FIX: was missing — caused 404→502 on /generate-simulation
from pdf_handler import (
    extract_pdf_text,
    find_subtopics_in_pdf,
    build_subtopics_json,
)
from q_animation import generate_question_animation

try:
    from sub_topics import process_subtopics_json
    SUB_TOPICS_AVAILABLE = True
    print("[INFO]  sub_topics.py loaded OK")
except ImportError:
    SUB_TOPICS_AVAILABLE = False
    print("[WARNING] sub_topics.py not found — falling back to pdf_handler output")

# ── Env flags ─────────────────────────────────────────────────────────────────
DEBUG_CORS          = os.getenv("DEBUG_CORS", "false").lower() == "true"
KEEP_ALIVE_INTERVAL = int(os.getenv("KEEP_ALIVE_INTERVAL", "600"))


# ══════════════════════════════════════════════════════════════════════════════
# KEEP-ALIVE PINGER
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

EXTRA_ORIGINS = [
    o.strip()
    for o in os.getenv("EXTRA_ORIGINS", "").split(",")
    if o.strip()
]

BASE_ORIGINS = [
    "https://haezet.com/",
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
    "null",   # file:// origin for local dev
] + EXTRA_ORIGINS

if DEBUG_CORS:
    print("[CORS] ⚠ DEBUG_CORS=true — allowing ALL origins (dev only, no credentials)")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    print(f"[CORS] Active origins: {BASE_ORIGINS}")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=BASE_ORIGINS,
        allow_origin_regex=r"https://(genzet|animind)[\w-]*\.vercel\.app",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ══════════════════════════════════════════════════════════════════════════════
# ROUTERS
# ══════════════════════════════════════════════════════════════════════════════

app.include_router(auth_router)     # /auth/*
app.include_router(sync_router)     # /sync/*
app.include_router(admin_router)    # /admin/*
# passkey_router removed — endpoints defined inline below

install_error_handler(app)


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH + ROOT
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health(request: Request):
    if request.method == "HEAD":
        return JSONResponse(content=None, status_code=200)

    return {
        "status":              "ok",
        "version":             "6.0.0",
        "backend":             "Supabase",
        "schema":              "normalized-v3",
        "debug_cors":          DEBUG_CORS,
        "keep_alive_interval": KEEP_ALIVE_INTERVAL,
        "sub_topics_module":   SUB_TOPICS_AVAILABLE,
        "endpoints": {
            "auth": {
                "register":      "POST /auth/register",
                "login":         "POST /auth/login",
                "logout":        "POST /auth/logout",
                "verify":        "GET  /auth/verify",
                "me":            "GET  /auth/me",
                "google":        "GET  /auth/google",
                "google_finish": "POST /auth/google/callback",
            },
            "sync": {
                # v6 normalized (new)
                "all_data":       "GET    /sync/all",
                "items_save":     "POST   /sync/items",
                "items_list":     "GET    /sync/items",
                "item_update":    "PUT    /sync/items/{id}",
                "item_delete":    "DELETE /sync/items/{id}",
                "subjects_list":  "GET    /sync/subjects",
                "subject_create": "POST   /sync/subjects",
                "subject_update": "PUT    /sync/subjects/{id}",
                "subject_delete": "DELETE /sync/subjects/{id}",
                "co_create":      "POST   /sync/cos",
                "co_update":      "PUT    /sync/cos/{id}",
                "co_delete":      "DELETE /sync/cos/{id}",
                "topic_create":   "POST   /sync/topics",
                "topic_update":   "PUT    /sync/topics/{id}",
                "topic_delete":   "DELETE /sync/topics/{id}",
                "vault_list":     "GET    /sync/vault/entries",
                "vault_add":      "POST   /sync/vault/entries",
                "vault_delete":   "DELETE /sync/vault/entries/{id}",
                "file_upload":    "POST   /sync/files/upload",
                "file_delete":    "DELETE /sync/files/delete",
                # legacy
                "legacy_pull":    "GET    /sync/animations    [LEGACY]",
                "legacy_push":    "POST   /sync/animations    [LEGACY]",
                "legacy_batch":   "POST   /sync/animations/batch [LEGACY]",
                "legacy_courses": "GET|PUT /sync/courses      [LEGACY]",
                "legacy_vault":   "GET|PUT /sync/vault        [LEGACY]",
            },
            "ai": {
                "animation":          "POST /generate-animation",
                "question_animation": "POST /generate-question-animation (returns job_id) -> GET /generate-question-animation/status/{job_id}",
                "book_mode":          "POST /generate-from-book",
                "topic_content":      "POST /generate-topic-content",
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


class PasskeyVerifyRequest(BaseModel):
    passkey: str


class PasskeyGrantRequest(BaseModel):
    passkey: str


# ══════════════════════════════════════════════════════════════════════════════
# PASSKEY ENDPOINTS  (inline — passkey_routes.py does not exist as a file)
# ══════════════════════════════════════════════════════════════════════════════

def _pk_get_user(request: Request):
    """Extract and verify the Supabase JWT. Returns (db, user_id, user_name, user_email)."""
    from auth_utils import get_supabase
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required.")
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        db = get_supabase()
        user_res = db.auth.get_user(token)
        user = user_res.user
        if not user:
            raise Exception("No user returned")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    user_id    = str(user.id)
    user_email = user.email or ""
    meta       = user.user_metadata or {}
    user_name  = meta.get("name") or meta.get("full_name") or user_email
    return db, user_id, user_name, user_email


@app.post("/auth/verify-passkey")
async def verify_passkey(request: Request, body: PasskeyVerifyRequest):
    passkey = (body.passkey or "").strip()
    if len(passkey) < 8 or len(passkey) > 9:
        raise HTTPException(status_code=400, detail="Passkey must be exactly 8 or 9 characters.")
    db, user_id, user_name, user_email = _pk_get_user(request)
    try:
        existing = db.table("passkey_access").select("id").eq("user_id", user_id).limit(1).execute()
        if existing.data:
            return {"ok": True, "already_granted": True}
        passkey_hash = hashlib.sha256(passkey.encode("utf-8")).hexdigest()
        pk_result = db.table("admin_passkeys").select("id, passkey").eq("passkey_hash", passkey_hash).limit(1).execute()
        if not pk_result.data:
            return {"ok": False}
        passkey_id = pk_result.data[0]["id"]
        claimed = db.table("passkey_access").select("user_id").eq("passkey_id", passkey_id).limit(1).execute()
        if claimed.data and claimed.data[0]["user_id"] != user_id:
            return {"ok": False, "detail": "already_assigned"}
        return {"ok": True, "already_granted": False}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[PASSKEY] ⚠ Supabase error: {e}")
        raise HTTPException(status_code=500, detail="Passkey verification failed. Please try again.")


@app.post("/auth/passkey/grant")
async def passkey_grant(request: Request, body: PasskeyGrantRequest):
    passkey = (body.passkey or "").strip()
    if not passkey:
        raise HTTPException(status_code=400, detail="passkey is required.")
    db, user_id, user_name, user_email = _pk_get_user(request)
    try:
        existing = db.table("passkey_access").select("id").eq("user_id", user_id).limit(1).execute()
        if existing.data:
            return {"granted": True}
        passkey_hash = hashlib.sha256(passkey.encode("utf-8")).hexdigest()
        pk_result = db.table("admin_passkeys").select("id, passkey").eq("passkey_hash", passkey_hash).limit(1).execute()
        if not pk_result.data:
            raise HTTPException(status_code=400, detail="Invalid passkey.")
        passkey_row  = pk_result.data[0]
        passkey_id   = passkey_row["id"]
        passkey_text = passkey_row["passkey"]
        db.table("passkey_access").insert({
            "user_id": user_id, "user_name": user_name,
            "user_email": user_email, "passkey_id": passkey_id, "passkey_used": passkey_text,
        }).execute()
        print(f"[PASSKEY] ✅ Grant recorded — user={user_id[:8]}…  email={user_email}")
    except HTTPException:
        raise
    except Exception as e:
        err_str = str(e)
        if "23505" in err_str or "duplicate" in err_str.lower():
            raise HTTPException(status_code=409, detail="This passkey has already been claimed by another user.")
        print(f"[PASSKEY] ⚠ Grant insert failed: {e}")
        raise HTTPException(status_code=500, detail="Could not record access grant.")
    return {"granted": True}


@app.get("/auth/passkey/check")
async def passkey_check(request: Request):
    db, user_id, _name, _email = _pk_get_user(request)
    try:
        result = db.table("passkey_access").select("id").eq("user_id", user_id).limit(1).execute()
        granted = bool(result.data)
    except Exception as e:
        print(f"[PASSKEY] ⚠ Access check failed: {e}")
        granted = False
    return {"granted": granted}


@app.post("/admin/passkeys/add")
async def admin_add_passkey(request: Request, body: PasskeyGrantRequest):
    _admin_token = os.getenv("ADMIN_SECRET_TOKEN", "")
    if not _admin_token or request.headers.get("X-Admin-Token", "") != _admin_token:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token header.")
    from auth_utils import get_supabase
    passkey = (body.passkey or "").strip()
    if len(passkey) < 8 or len(passkey) > 9:
        raise HTTPException(status_code=400, detail="Passkey must be exactly 8 or 9 characters.")
    passkey_hash = hashlib.sha256(passkey.encode("utf-8")).hexdigest()
    try:
        db = get_supabase()
        result = db.table("admin_passkeys").insert({"passkey": passkey, "passkey_hash": passkey_hash}).execute()
        row = result.data[0] if result.data else {}
        return {"id": row.get("id"), "passkey": row.get("passkey"), "label": row.get("label"), "created_at": row.get("created_at")}
    except Exception as e:
        err_str = str(e)
        if "23505" in err_str or "duplicate" in err_str.lower():
            raise HTTPException(status_code=409, detail="A passkey with that value already exists.")
        raise HTTPException(status_code=500, detail="Could not add passkey.")


@app.get("/admin/passkeys/list")
async def admin_list_passkeys(request: Request):
    _admin_token = os.getenv("ADMIN_SECRET_TOKEN", "")
    if not _admin_token or request.headers.get("X-Admin-Token", "") != _admin_token:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token header.")
    from auth_utils import get_supabase
    try:
        db = get_supabase()
        pk_res  = db.table("admin_passkeys").select("id, passkey, label, created_at").order("created_at").execute()
        acc_res = db.table("passkey_access").select("passkey_id, user_id, user_name, user_email, granted_at").execute()
        claims  = {row["passkey_id"]: row for row in (acc_res.data or []) if row.get("passkey_id")}
        result  = []
        for pk in (pk_res.data or []):
            pk_id = pk["id"]
            entry = {"id": pk_id, "passkey": pk["passkey"], "label": pk.get("label"),
                     "created_at": pk.get("created_at"), "claimed": pk_id in claims}
            if pk_id in claims:
                claim = claims[pk_id]
                entry["claimed_by"] = {"user_id": claim.get("user_id"), "user_name": claim.get("user_name"),
                                       "user_email": claim.get("user_email"), "granted_at": claim.get("granted_at")}
            result.append(entry)
        return {"passkeys": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Could not retrieve passkeys.")


# ══════════════════════════════════════════════════════════════════════════════
# QUESTION-ANIMATION JOB QUEUE
# ══════════════════════════════════════════════════════════════════════════════
# generate_question_animation() can legitimately run past the ~120s timeout
# that Vercel enforces on the /api/* rewrite proxying to this service
# (see vercel.json — "rewrites" -> that proxy has a hard, non-configurable
# 120s ceiling). Running it inline in the POST handler means any request
# that crosses that line gets killed upstream, and Railway logs it as a 499
# (client closed the connection) even though generation was still working.
#
# Fix: the POST handler only ENQUEUES the job and returns a job_id in
# milliseconds. The actual generation runs as a background asyncio task,
# completely outside of any request that passes through the Vercel proxy.
# The frontend then polls /generate-question-animation/status/{job_id},
# and each poll is a cheap, near-instant call that can never hit that
# ceiling either.
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


@app.post("/generate-simulation")
async def create_simulation(request: SimulationRequest):
    """
    Generate a complete, self-contained interactive HTML5 simulation.

    WHY SSE / StreamingResponse:
    Railway (and most reverse-proxy gateways) enforce a ~30–60 s idle-write
    timeout.  The old implementation called generate_simulation() and then
    returned a single JSON blob — the gateway saw zero bytes for 60+ seconds
    and killed the connection with a 502 before the response arrived.

    Fix: return a StreamingResponse that:
      1. Sends "ping" SSE heartbeat lines every 10 s while generation runs.
      2. Sends a single "result" SSE event containing the final JSON.
      3. Closes the stream.

    The frontend (index.html) already has a plain fetch() call that reads
    response.json().  We update the response to be compatible with both the
    existing JS (which reads the *last* data line as JSON) via a thin
    compatibility shim at the end of this generator.
    """
    topic = (request.topic or "").strip()
    if not topic:
        raise HTTPException(status_code=400, detail="'topic' field cannot be empty")
    if len(topic) > 2000:
        raise HTTPException(status_code=400, detail="Topic too long (max 2000 chars)")

    if request.mode and request.mode.lower() not in ("general", ""):
        topic_with_mode = f"{topic} (subject area: {request.mode})"
    else:
        topic_with_mode = topic

    async def _stream():
        import asyncio, json as _json

        # Run generation in the background and send pings while we wait
        task = asyncio.ensure_future(generate_simulation(topic_with_mode))

        while not task.done():
            yield "event: ping\ndata: {\"status\":\"generating\"}\n\n"
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
            except asyncio.TimeoutError:
                pass  # not done yet — loop and send another ping
            except Exception:
                break  # task raised — let the outer block handle it

        # Collect the result
        try:
            result = task.result()
        except Exception as exc:
            err = _json.dumps({"render_status": "error", "error_reason": str(exc)})
            yield f"event: result\ndata: {err}\n\n"
            return

        result["source"] = "generated"
        payload = _json.dumps(result)
        yield f"event: result\ndata: {payload}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # tells Nginx/Railway not to buffer
        },
    )


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


@app.post("/generate-topic-content")
async def create_topic_content(request: SkillContentRequest):
    """Generate comprehensive 10-section educational content as JSON."""
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
    print(f"  GenZet / Animind API v6.0 (server.py) — port {_port}")
    print("=" * 65)
    uvicorn.run("server:app", host="0.0.0.0", port=_port, reload=True)
