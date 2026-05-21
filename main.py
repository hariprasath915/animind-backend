"""
main.py  —  SmartBoard AI Backend v4.2
=======================================
UPDATED: Keep-alive pinger + CORS + HEAD support + better error handling

New in v4.2:
  - Background self-ping every 10 minutes (prevents Render free-tier spin-down)

From v4.1:
  - CORS now covers all Vercel preview URLs via regex
  - Root route handles HEAD (fixes Render health-check 405)
  - /health returns 200 on GET and HEAD
  - Startup errors are caught and logged cleanly
  - Optional DEBUG_CORS env var to allow all origins during dev

Preserved from v4.0 (UNCHANGED):
  POST /auth/register
  POST /auth/login
  GET  /auth/verify
  GET  /auth/me
  POST /sync/animations
  POST /sync/animations/batch
  GET  /sync/animations
  DELETE /sync/animations/{id}
  GET  /health
  POST /generate-animation
  POST /generate-from-book
  POST /generate-topic-content
  POST /generate-question-animation

Run:
    uvicorn main:app --host 0.0.0.0 --port 8000

Environment variables (in .env):
    ANTHROPIC_API_KEY=sk-ant-...
    JWT_SECRET_KEY=<long random string>
    JWT_EXPIRE_DAYS=30
    DATABASE_URL=sqlite:///./genzet.db   (optional — default is SQLite)
    DEBUG_CORS=true                       (optional — allows ALL origins, dev only)
"""

import sys, io, os, asyncio
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ── Load Env Variables (.env) ──────────────────────────────────────────
try:
    from dotenv import load_dotenv
    from pathlib import Path
    _current_dir = Path(__file__).resolve().parent
    # Attempt to load from script directory
    load_dotenv(dotenv_path=_current_dir / ".env")
    # Also load from parent directory in case of repo-root running environment
    load_dotenv(dotenv_path=_current_dir.parent / ".env")
    print(f"[STARTUP] ✅ Loaded environment variables from {_current_dir / '.env'} or parent")
except Exception as env_err:
    print(f"[STARTUP] ⚠ Could not run load_dotenv: {env_err}")

from contextlib import asynccontextmanager
import httpx

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

# ── NEW: Auth + Sync ──────────────────────────────────────────────────
from database import init_db
from auth_routes import router as auth_router
from sync_routes import router as sync_router

# ── Existing AI modules (unchanged) ───────────────────────────────────
from claude_client import (
    generate_animation,
    generate_genzet_book_content,
    subtopics_json_to_genzet_args,
)
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

import json

# ── Env flags ─────────────────────────────────────────────────────────
DEBUG_CORS = os.getenv("DEBUG_CORS", "false").lower() == "true"

# ── Keep-alive interval (seconds) ─────────────────────────────────────
KEEP_ALIVE_INTERVAL = int(os.getenv("KEEP_ALIVE_INTERVAL", "600"))  # 10 min default


async def _keep_alive_pinger():
    """
    Background task: pings our own /health endpoint every 10 minutes
    to prevent Render free-tier from spinning down after 15 min idle.
    Uses RENDER_EXTERNAL_URL (auto-set by Render) if available.
    """
    # Render sets this automatically; fallback to known URL
    self_url = os.getenv(
        "RENDER_EXTERNAL_URL",
        "https://animind-backend-1.onrender.com"
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


# ── Startup / Shutdown lifecycle ───────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once at startup: creates DB tables + starts keep-alive pinger."""
    print("[STARTUP]  Initializing database…")
    try:
        init_db()
        print("[STARTUP]  ✅ GenZet v4.2 ready")
    except Exception as e:
        # Non-fatal: log the error but keep the server alive.
        # Users can still reach the health endpoint; DB-backed routes
        # will return 500 until the DB is fixed, but the server won't crash.
        print(f"[STARTUP]  ⚠  DB init warning (server still running): {e}")

    # Start the keep-alive background pinger
    pinger_task = asyncio.create_task(_keep_alive_pinger())

    yield

    # Graceful shutdown: cancel the pinger
    pinger_task.cancel()
    try:
        await pinger_task
    except asyncio.CancelledError:
        pass
    print("[SHUTDOWN] GenZet shutting down.")


# ── App ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="SmartBoard AI API",
    version="4.2.0",
    lifespan=lifespan,
)

# ── CORS ───────────────────────────────────────────────────────────────
# If DEBUG_CORS=true (local dev), allow everything.
# In production, we list explicit origins + a regex for Vercel previews.
#
# To add a new Vercel URL without redeploying, set EXTRA_ORIGINS env var:
#   EXTRA_ORIGINS=https://your-app-abc123.vercel.app,https://other.vercel.app
EXTRA_ORIGINS = [
    o.strip() for o in os.getenv("EXTRA_ORIGINS", "").split(",") if o.strip()
]

BASE_ORIGINS = [
    "https://genzet-app.vercel.app",                                              # ✅ genzet frontend (main)
    "https://genzet-app-git-main-hari-prasath-genzet-web-project.vercel.app",    # ✅ genzet git-main preview
    "https://animind-gold.vercel.app",                                            # ✅ animind frontend
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8000",
] + EXTRA_ORIGINS

if DEBUG_CORS:
    print("[CORS] ⚠ DEBUG_CORS=true — allowing ALL origins (dev only)")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,   # credentials + wildcard is not allowed by spec
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    print(f"[CORS] Active origins: {BASE_ORIGINS}")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=BASE_ORIGINS,
        allow_origin_regex=r"https://(genzet|animind)[\w-]*\.vercel\.app",  # all preview URLs
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
# ── Register routers ───────────────────────────────────────────────────
app.include_router(auth_router)   # /auth/...
app.include_router(sync_router)   # /sync/...


# ══════════════════════════════════════════════════════════════════════
# HEALTH + ROOT
# ══════════════════════════════════════════════════════════════════════

@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health(request: Request):
    """
    GET  → full JSON status
    HEAD → 200 OK with no body (used by Render health checks)
    """
    if request.method == "HEAD":
        return JSONResponse(content=None, status_code=200)
    return {
        "status":  "ok",
        "version": "4.2.0",
        "debug_cors": DEBUG_CORS,
        "keep_alive_interval": KEEP_ALIVE_INTERVAL,
        "sub_topics_module": SUB_TOPICS_AVAILABLE,
        "endpoints": {
            # ── Auth endpoints ──
            "register":            "POST /auth/register",
            "login":               "POST /auth/login",
            "verify":              "GET  /auth/verify",
            "me":                  "GET  /auth/me",
            # ── Sync endpoints ──
            "sync_save":           "POST /sync/animations",
            "sync_batch":          "POST /sync/animations/batch",
            "sync_fetch":          "GET  /sync/animations",
            "sync_delete":         "DELETE /sync/animations/{anim_id}",
            # ── AI endpoints ──
            "animation":           "POST /generate-animation",
            "question_animation":  "POST /generate-question-animation",
            "book_mode":           "POST /generate-from-book",
            "skill_workflow":      "POST /generate-topic-content",
        },
    }


# ══════════════════════════════════════════════════════════════════════
# EXISTING ENDPOINTS — UNCHANGED FROM v3.7 / v4.0
# ══════════════════════════════════════════════════════════════════════

class AnimationRequest(BaseModel):
    prompt: str


class QuestionAnimRequest(BaseModel):
    question: str


class SkillContentRequest(BaseModel):
    topic:        str
    subject:      Optional[str] = "Engineering"
    retry_failed: Optional[bool] = True


@app.post("/generate-animation")
async def create_animation(request: AnimationRequest):
    if not request.prompt or not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    try:
        result = await generate_animation(request.prompt.strip())
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate-question-animation")
async def create_question_animation(request: QuestionAnimRequest):
    question = (request.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="'question' field cannot be empty")
    try:
        result = await generate_question_animation(question)
        return result
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Question animation generation failed: {e}")


@app.post("/generate-from-book")
async def create_from_book(
    topic:    str            = Form(...),
    file:     UploadFile     = File(...),
    subtopic: Optional[str]  = Form(default=None),
):
    topic    = (topic or "").strip()
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
        raise HTTPException(status_code=400, detail=f"Could not read PDF: {pdf_data.get('error', 'Unknown')}")

    full_text  = pdf_data["full_text"]
    word_count = pdf_data["word_count"]

    if word_count < 50:
        raise HTTPException(status_code=400, detail="PDF has no readable text.")

    topic_data = find_subtopics_in_pdf(full_text, topic)
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
            topic=topic, subtopic=subtopic,
            pdf_context=pdf_context, subtopics_list=subtopics_list,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generation failed: {e}")


@app.post("/generate-topic-content")
async def create_topic_content(request: SkillContentRequest):
    topic = (request.topic or "").strip()
    if not topic:
        raise HTTPException(status_code=400, detail="'topic' field cannot be empty")
    try:
        from claude_client import generate_skill_content
        result = await generate_skill_content(
            topic=topic,
            subject=request.subject or "Engineering",
            retry_failed=request.retry_failed if request.retry_failed is not None else True,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SKILL.md generation failed: {e}")


if __name__ == "__main__":
    import uvicorn
    _port = int(os.getenv("PORT", "8000"))
    print("=" * 65)
    print(f"  SmartBoard AI API v4.2 — with Auth + Cloud Sync + Keep-Alive on port {_port}")
    print("=" * 65)
    uvicorn.run("main:app", host="0.0.0.0", port=_port)