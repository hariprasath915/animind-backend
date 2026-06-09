"""
SmartBoard API Server  v3.1
===========================
Updated for claude_client.py v10.1  +  q_animation.py

Endpoints:
  GET  /                              →  health + endpoint list
  GET  /health                        →  version check
  POST /generate-animation            →  claude_client.generate_animation()
  POST /generate-topic-content        →  claude_client.generate_topic_content()
  POST /generate-question-animation   →  q_animation.generate_question_animation()   ← ADDED

claude_client.py info:
  Version  : v10.1 — EduAnimator + EduContentGenerator
  Models   : claude-sonnet-4-6  (hook_motivation, types, animation)
             claude-haiku-4-5   (intro, history, overview, apps, quiz, problems, fun_facts)
  Sections : 8 HTML sections (AI Creator) + 10 JSON sections (EduContentGenerator)
  Quiz     : 20 questions, 5 pages × 4 per page (AI Creator)

Run:
  uvicorn server:app --reload --port 8000
  python server.py
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

# ── v10.1 unified client — all functionality in one file ──────────────
from claude_client import generate_animation, generate_topic_content

# ── Question Animation module ─────────────────────────────────────────
from q_animation import generate_question_animation

app = FastAPI(title="SmartBoard AI API", version="3.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        "message": "SmartBoard AI API v3.1 running 🎓",
        "claude_client_version": "v10.1",
        "endpoints": {
            "animation":           "POST /generate-animation",
            "topic_content":       "POST /generate-topic-content",
            "question_animation":  "POST /generate-question-animation",
        },
        "models": {
            "creative":  "claude-sonnet-4-6  → hook_motivation, types, animation, question_animation",
            "fast":      "claude-haiku-4-5   → intro, history, overview, apps, quiz, problems, fun_facts",
        }
    }


@app.get("/health")
async def health():
    return {"status": "ok", "version": "3.1", "claude_client": "v10.1"}


# ── Animation endpoint (AI Creator — claude_client.generate_animation) ─

@app.post("/generate-animation")
async def animation_endpoint(req: AnimationRequest):
    """
    Generate full 8-section HTML animation page for the topic.
    Uses claude_client.generate_animation() — AI Creator mode.

    Supports:
      - "optical fibre"
      - "optical fibre - characteristics, properties"

    Returns: { title, explanation, animation_code }
    """
    if not req.prompt or not req.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    try:
        result = await generate_animation(req.prompt.strip())
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Topic Content endpoint (EduContentGenerator — 10-section JSON) ─────

@app.post("/generate-topic-content")
async def topic_content_endpoint(req: TopicContentRequest):
    """
    Generate comprehensive 10-section educational content as JSON.
    Uses claude_client.generate_topic_content() — EduContentGenerator.

    Request body:
      {
        "prompt":       "Heat Transfer",           ← required
        "subject":      "Mechanical Engineering",  ← optional (default: "Engineering")
        "retry_failed": true                        ← optional (default: true)
      }

    Returns:
      {
        "topic":           str,
        "subject":         str,
        "total_sections":  int,
        "failed_sections": list,
        "sections": {
          "hook_motivation": { ... },
          "intro":           { ... },
          "history":         { ... },
          "overview":        { ... },
          "types":           { ... },
          "apps":            { ... },
          "quiz":            { ... },
          "animation":       { ... },
          "problems":        { ... },
          "fun_facts":       { ... }
        }
      }
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


# ── Question Animation endpoint ────────────────────────────────────────

@app.post("/generate-question-animation")
async def question_animation_endpoint(req: QuestionAnimRequest):
    """
    Generate a rich, interactive Canvas + SVG + anime.js HTML5 animation
    that visually answers any educational question.

    The animation is:
      - Self-contained HTML5 (Canvas + SVG + anime.js)
      - Question-specific structure, colors, and visuals
      - Step-by-step interactive navigation (PREV/NEXT + dots)
      - Continuously looping — no pause button
      - Realistic structural drawings (not placeholder boxes)

    Request body (JSON):
        { "question": "A furnace wall has three layers..." }

    Returns:
        {
          "title":          "Short descriptive title",
          "explanation":    "One sentence description",
          "animation_code": "Complete self-contained HTML"
        }
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


# ── Direct run ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("🚀 SmartBoard API v3.1 starting on http://localhost:8000")
    print("   claude_client v10.1 loaded")
    print("   q_animation module loaded")
    print()
    print("   POST /generate-animation            → 8-section HTML  (claude-sonnet-4-6)")
    print("   POST /generate-topic-content        → 10-section JSON (Sonnet + Haiku routing)")
    print("   POST /generate-question-animation   → Interactive Q&A animation (claude-sonnet-4-6)")
    print()
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
