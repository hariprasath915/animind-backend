"""
main.py  —  SmartBoard AI Backend v5.1 (Supabase)
==================================================
Migrated from SQLAlchemy + bcrypt/jose to Supabase Auth + supabase-py.

New in v5.1 (sync with all sibling modules):
  - Added:   admin_router + install_error_handler  (admin_router.py)
  - Fixed:   CORS — removed wildcard+credentials conflict (server.py had this bug)
  - Fixed:   SUPABASE_SERVICE_KEY env-var name (auth_utils.py uses this name)
  - Fixed:   /generate-topic-content uses generate_ultimate_learning_content
  - Kept:    keep-alive pinger, lifespan context, all existing endpoints

Entry point (render.yaml):
    uvicorn main:app --host 0.0.0.0 --port $PORT

Environment variables (Render Dashboard → Environment):
    ANTHROPIC_API_KEY        — sk-ant-... key for AI generation
    SUPABASE_URL             — https://<project-id>.supabase.co
    SUPABASE_ANON_KEY        — anon/public key (auth_routes sign-up/sign-in)
    SUPABASE_SERVICE_KEY     — service-role key (backend-only DB CRUD)
    SUPABASE_JWT_SECRET      — Supabase Dashboard → Settings → API → JWT Secret
    ADMIN_SECRET_TOKEN       — any long random string for /admin/* endpoints
    DEBUG_CORS               — "true" | "false"  (allow ALL origins, dev only)
    EXTRA_ORIGINS            — comma-separated extra Vercel preview URLs
    KEEP_ALIVE_INTERVAL      — seconds between self-pings (default 600)

Endpoints:
    GET  /                              →  health + endpoint list
    GET  /health                        →  version check
    POST /auth/register                 →  auth_routes.register()
    POST /auth/login                    →  auth_routes.login()
    GET  /auth/verify                   →  auth_routes.verify_token()
    GET  /auth/me                       →  auth_routes.get_me()
    POST /sync/animations               →  sync_routes.sync_animation()       (JWT)
    POST /sync/animations/batch         →  sync_routes.batch_sync_animations() (JWT)
    GET  /sync/animations               →  sync_routes.get_animations()        (JWT)
    DELETE /sync/animations/{anim_id}   →  sync_routes.delete_animation()      (JWT)
    GET  /admin/errors                  →  admin_router.get_errors()      (X-Admin-Token)
    GET  /admin/users                   →  admin_router.get_users()       (X-Admin-Token)
    POST /generate-animation            →  claude_client.generate_animation()
    POST /generate-question-animation   →  q_animation.generate_question_animation()
    POST /generate-from-book            →  claude_client.generate_genzet_book_content()
    POST /generate-topic-content        →  claude_client.generate_ultimate_learning_content()
"""

import sys
import io
import os
import asyncio

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Environment ────────────────────────────────────────────────────────
# Environment variables are managed by Render in production.
# Locally, ensure your variables are set in your shell before running.

from contextlib import asynccontextmanager
import httpx

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

# ── Auth + Sync (Supabase-backed) ─────────────────────────────────────────────
from auth_routes import router as auth_router
from sync_routes import router as sync_router

# ── Admin (error ring + user list) ────────────────────────────────────────────
# Wired as instructed in admin_router.py's own docstring:
#   from admin_router import router as admin_router, install_error_handler
#   app.include_router(admin_router)
#   install_error_handler(app)
from admin_router import router as admin_router_obj, install_error_handler

# ── AI modules ────────────────────────────────────────────────────────────────
from claude_client import (
    generate_animation,
    generate_genzet_book_content,
    subtopics_json_to_genzet_args,
    generate_ultimate_learning_content,   # used by /generate-topic-content
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

# ── Env flags ─────────────────────────────────────────────────────────────────
DEBUG_CORS = os.getenv("DEBUG_CORS", "false").lower() == "true"
KEEP_ALIVE_INTERVAL = int(os.getenv("KEEP_ALIVE_INTERVAL", "600"))  # 10 min


# ── Keep-alive pinger (prevents Render free-tier spin-down) ──────────────────
async def _keep_alive_pinger():
    """
    Pings /health every KEEP_ALIVE_INTERVAL seconds so Render doesn't
    spin down the service after 15 minutes of inactivity.
    Uses RENDER_EXTERNAL_URL when available (auto-set by Render).
    """
    self_url = os.getenv(
        "RENDER_EXTERNAL_URL",
        "https://animind-backend-y07f.onrender.com",
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


# ── App lifespan (startup / shutdown) ────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Starts the keep-alive pinger on startup; cancels it on shutdown."""
    print("[STARTUP] ✅ SmartBoard AI v5.1 ready (Supabase + Admin)")
    pinger = asyncio.create_task(_keep_alive_pinger())
    yield
    pinger.cancel()
    try:
        await pinger
    except asyncio.CancelledError:
        pass
    print("[SHUTDOWN] SmartBoard AI shutting down.")


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="SmartBoard AI API",
    version="5.1.0",
    description="AI-powered educational animation platform — Supabase edition",
    lifespan=lifespan,
)


# ── CORS ─────────────────────────────────────────────────────────────────────
# IMPORTANT: allow_origins=["*"] and allow_credentials=True is INVALID per the
# CORS spec — browsers will reject the preflight response.  We list explicit
# origins instead, and use allow_origin_regex for Vercel preview URLs.
#
# To add a new origin without redeploying, set EXTRA_ORIGINS env var:
#   EXTRA_ORIGINS=https://your-app-abc123.vercel.app,https://other.vercel.app
EXTRA_ORIGINS = [
    o.strip()
    for o in os.getenv("EXTRA_ORIGINS", "").split(",")
    if o.strip()
]

BASE_ORIGINS = [
    "https://genzet-app.vercel.app",
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
    "null",   # file:// origin — lets index.html run directly from disk during dev
] + EXTRA_ORIGINS

if DEBUG_CORS:
    # Dev-only: allow everything but WITHOUT credentials (spec requirement)
    print("[CORS] ⚠ DEBUG_CORS=true — allowing ALL origins (dev only)")
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
        allow_origin_regex=r"https://(genzet|animind)[\w-]*\.vercel\.app",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth_router)        # /auth/*
app.include_router(sync_router)        # /sync/*
app.include_router(admin_router_obj)   # /admin/*

# ── Global error handler (feeds /admin/errors endpoint) ──────────────────────
install_error_handler(app)


# ══════════════════════════════════════════════════════════════════════════════
# HEALTH + ROOT
# ══════════════════════════════════════════════════════════════════════════════

@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/health", methods=["GET", "HEAD"])
async def health(request: Request):
    """
    GET  → full JSON status payload
    HEAD → 200 OK with no body (used by Render health checks)
    """
    if request.method == "HEAD":
        return JSONResponse(content=None, status_code=200)
    return {
        "status":  "ok",
        "version": "5.1.0",
        "backend": "Supabase",
        "debug_cors": DEBUG_CORS,
        "keep_alive_interval": KEEP_ALIVE_INTERVAL,
        "sub_topics_module": SUB_TOPICS_AVAILABLE,
        "endpoints": {
            "auth": {
                "register": "POST /auth/register",
                "login":    "POST /auth/login",
                "verify":   "GET  /auth/verify",
                "me":       "GET  /auth/me",
            },
            "sync": {
                "pull":   "GET    /sync/animations",
                "push":   "POST   /sync/animations",
                "batch":  "POST   /sync/animations/batch",
                "delete": "DELETE /sync/animations/{anim_id}",
            },
            "ai": {
                "animation":          "POST /generate-animation",
                "question_animation": "POST /generate-question-animation",
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


@app.post("/generate-question-animation")
async def create_question_animation(request: QuestionAnimRequest):
    """Generate a rich Canvas+SVG+anime.js animation that visually answers any educational question."""
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
        result = await generate_ultimate_learning_content(
            topic=topic,
        )
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
    print(f"  SmartBoard AI API v5.1 — port {_port}")
    print("=" * 65)
    uvicorn.run("main:app", host="0.0.0.0", port=_port, reload=True)
