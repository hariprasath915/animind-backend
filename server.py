"""
server.py — Animind / GenZet Backend  v4.0  (Supabase edition)
==============================================================
Architecture:
  - FastAPI application
  - Auth:      Supabase Auth  (sign-up / sign-in via auth_routes.py)
  - Database:  Supabase (supabase-py service-role client via auth_utils.py)
  - JWT:       Supabase-issued HS256 tokens verified with SUPABASE_JWT_SECRET
  - Storage:   `contents` table in Supabase (filtered by user_id on every query)

Required environment variables (set in Render Dashboard → Environment):
  ANTHROPIC_API_KEY    — sk-ant-... key for AI generation
  SUPABASE_URL         — https://<project>.supabase.co
  SUPABASE_ANON_KEY    — anon/public key (used in auth_routes for sign-up/sign-in)
  SUPABASE_SERVICE_KEY — service-role key (backend-only; never expose to browser)
  SUPABASE_JWT_SECRET  — JWT secret from Supabase Dashboard → Settings → API
  ADMIN_SECRET_TOKEN   — any long random string for /admin/* endpoints

Optional:
  JWT_EXPIRE_DAYS      — not used (Supabase controls token expiry); kept for compat
  LOG_LEVEL            — debug | info | warning | error (default: debug)
  SHOW_ERROR_DETAILS   — true | false (default: true)

Environment variables (set in Render Dashboard → Environment):
  ANTHROPIC_API_KEY    — Claude AI key
  SUPABASE_URL         — https://<project>.supabase.co
  SUPABASE_ANON_KEY    — anon/public key
  SUPABASE_SERVICE_KEY — service-role key (backend-only)
  SUPABASE_JWT_SECRET  — JWT secret from Supabase Dashboard → Settings → API
  ADMIN_SECRET_TOKEN   — for /admin/* endpoints
  FRONTEND_URL         — e.g. https://genzet-app.vercel.app (for OAuth redirect)
  EXTRA_ORIGINS        — comma-separated extra frontend URLs

Endpoints:
  GET  /                              →  health + endpoint list
  GET  /health                        →  version check
  POST /auth/register                 →  register + create public.users row
  POST /auth/login                    →  login  + update public.users.last_login
  POST /auth/logout                   →  revoke session
  GET  /auth/verify                   →  validate existing JWT
  GET  /auth/me                       →  profile from public.users
  GET  /auth/google                   →  start Google OAuth flow
  POST /auth/google/callback          →  finish Google OAuth + create profile
  POST /sync/animations               →  sync_routes (JWT required)
  POST /sync/animations/batch         →  sync_routes (JWT required)
  GET  /sync/animations               →  sync_routes (JWT required)
  DELETE /sync/animations/{anim_id}   →  sync_routes (JWT required)
  GET  /admin/errors                  →  admin_router (X-Admin-Token required)
  GET  /admin/users                   →  admin_router (X-Admin-Token required)
  POST /generate-animation            →  claude_client.generate_animation()
  POST /generate-topic-content        →  claude_client.generate_topic_content()
  POST /generate-question-animation   →  q_animation.generate_question_animation()

Run locally:
  uvicorn server:app --reload --port 8000
"""

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── Route modules ──────────────────────────────────────────────────────
from auth_routes import router as auth_router
from sync_routes import router as sync_router
from admin_router import router as admin_router, install_error_handler

# ── AI generation modules ──────────────────────────────────────────────
from claude_client import generate_animation, generate_topic_content
from q_animation import generate_question_animation

from fastapi import HTTPException
from pydantic import BaseModel
from typing import Optional

# ── App ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Animind / GenZet API",
    version="4.0.0",
    description="AI-powered educational animation platform — Supabase edition",
)

# ── CORS ───────────────────────────────────────────────────────────────
# CRITICAL: allow_origins=["*"] + allow_credentials=True is INVALID per
# the CORS spec. Browsers reject the preflight response — this is the root
# cause of "Failed to Fetch" on login. Use explicit origins instead.
#
# Add new Vercel preview URLs without redeploying via EXTRA_ORIGINS env var:
#   EXTRA_ORIGINS=https://your-preview-abc.vercel.app
_EXTRA_ORIGINS = [
    o.strip()
    for o in os.getenv("EXTRA_ORIGINS", "").split(",")
    if o.strip()
]

_ALLOWED_ORIGINS = [
    "https://genzet-app.vercel.app",
    "https://genzet-app-git-main-hari-prasath-genzet-web-project.vercel.app",
    "https://animind-gold.vercel.app",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8000",
] + _EXTRA_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_origin_regex=r"https://(genzet|animind)[\w-]*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────────
app.include_router(auth_router)    # /auth/*
app.include_router(sync_router)    # /sync/*
app.include_router(admin_router)   # /admin/*

# ── Global error handler (feeds /admin/errors endpoint) ───────────────
install_error_handler(app)


# ── Request models ─────────────────────────────────────────────────────

class AnimationRequest(BaseModel):
    prompt: str


class TopicContentRequest(BaseModel):
    prompt: str
    subject: Optional[str] = "Engineering"
    retry_failed: Optional[bool] = True


class QuestionAnimRequest(BaseModel):
    question: str


# ── Health ─────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "status": "ok",
        "version": "4.1.0",
        "message": "GenZet / Animind API — Supabase edition 🎓",
        "endpoints": {
            "auth": {
                "register":       "POST /auth/register      → creates public.users row",
                "login":          "POST /auth/login          → updates last_login",
                "logout":         "POST /auth/logout",
                "verify":         "GET  /auth/verify         → validate JWT",
                "me":             "GET  /auth/me             → profile from DB",
                "google":         "GET  /auth/google         → Google OAuth URL",
                "google_finish":  "POST /auth/google/callback → finish Google OAuth",
            },
            "sync": {
                "pull":   "GET    /sync/animations",
                "push":   "POST   /sync/animations",
                "batch":  "POST   /sync/animations/batch",
                "delete": "DELETE /sync/animations/{anim_id}",
            },
            "ai": {
                "animation":          "POST /generate-animation",
                "topic_content":      "POST /generate-topic-content",
                "question_animation": "POST /generate-question-animation",
            },
            "admin": {
                "errors": "GET /admin/errors  (X-Admin-Token header required)",
                "users":  "GET /admin/users   (X-Admin-Token header required)",
            },
        },
    }


@app.get("/health")
async def health():
    return {
        "status":  "ok",
        "version": "4.0",
        "auth":    "supabase",
        "db":      "supabase",
    }


# ── AI Generation endpoints ────────────────────────────────────────────

@app.post("/generate-animation")
async def animation_endpoint(req: AnimationRequest):
    """
    Generate full 8-section HTML animation page for the topic.
    No auth required — generation is open.
    Save the result via POST /sync/animations (JWT required).
    """
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    try:
        result = await generate_animation(req.prompt.strip())
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate-topic-content")
async def topic_content_endpoint(req: TopicContentRequest):
    """
    Generate comprehensive 10-section educational content as JSON.
    No auth required — generation is open.
    """
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    try:
        result = generate_topic_content(
            topic=req.prompt.strip(),
            subject=req.subject or "Engineering",
            retry_failed=req.retry_failed if req.retry_failed is not None else True,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/generate-question-animation")
async def question_animation_endpoint(req: QuestionAnimRequest):
    """
    Generate a rich, interactive Canvas + SVG + anime.js HTML5 animation
    that visually answers any educational question.
    No auth required — generation is open.
    Save the result via POST /sync/animations (JWT required).
    """
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="'question' field cannot be empty")

    print(f"\n{'═'*64}")
    print(f"[Q_ANIM]  Received: '{question[:80]}{'...' if len(question) > 80 else ''}'")
    print(f"{'═'*64}")

    try:
        result = await generate_question_animation(question)
        print(f"[Q_ANIM] ✅ title='{result['title']}' code={len(result.get('animation_code', ''))} chars")
        return result
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Question animation generation failed: {e}")




# ── Book Animation endpoint (PDF → Animation) ─────────────────────────

class BookAnimRequest(BaseModel):
    prompt: str
    pdf_text: Optional[str] = ""   # extracted text from uploaded PDF


@app.post("/generate-from-book")
async def book_animation_endpoint(req: BookAnimRequest):
    """
    Generate an animation from a PDF/book excerpt.
    Frontend calls this from the Book Creator tab (amCurrentMode = 'book').

    Currently delegates to generate_animation() with the PDF text
    prepended to the prompt. Replace with a dedicated claude_client
    function when ready.

    Request body:
        { "prompt": "user question", "pdf_text": "extracted PDF content" }
    """
    prompt = (req.prompt or "").strip()
    pdf_text = (req.pdf_text or "").strip()

    if not prompt and not pdf_text:
        raise HTTPException(status_code=400, detail="Either 'prompt' or 'pdf_text' must be provided.")

    # Combine PDF context with user prompt for the animation generator
    combined = prompt
    if pdf_text:
        # Truncate PDF text to avoid token overflow — adjust limit as needed
        max_pdf_chars = 4000
        truncated = pdf_text[:max_pdf_chars] + ("…" if len(pdf_text) > max_pdf_chars else "")
        combined = f"{prompt}\n\nContext from document:\n{truncated}" if prompt else truncated

    try:
        result = await generate_animation(combined)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Direct run ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("🚀 Animind / GenZet API v4.0 starting on http://localhost:8000")
    print()
    print("   Auth:    Supabase Auth (SUPABASE_URL + SUPABASE_JWT_SECRET)")
    print("   Storage: Supabase `contents` table (SUPABASE_SERVICE_KEY)")
    print()
    print("   POST /auth/register         → sign up via Supabase Auth")
    print("   POST /auth/login            → sign in via Supabase Auth")
    print("   GET  /sync/animations       → fetch user's cloud library (JWT)")
    print("   POST /sync/animations       → save one animation to cloud (JWT)")
    print("   POST /sync/animations/batch → bulk upload to cloud (JWT)")
    print("   DELETE /sync/animations/:id → delete from cloud (JWT)")
    print()
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
