"""
sync_routes.py — Cloud Sync Endpoints for GenZet Animations
============================================================
Location: backend/sync_routes.py

Endpoints:
  POST /sync/animations        — save or update one animation (upsert)
  POST /sync/animations/batch  — bulk upsert (used on first login/migration)
  GET  /sync/animations        — fetch ALL animations for the authenticated user
  DELETE /sync/animations/{anim_id} — delete one animation by client-side id

All routes require a valid JWT token in the Authorization header.

Design decisions:
  - POST /sync/animations is IDEMPOTENT:
      Same anim_id from the same user → UPDATE instead of INSERT
      Safe to call multiple times. Prevents duplicates.
  - GET /sync/animations returns full animation_code so the frontend
      can fully restore IndexedDB on a new device.
  - Animations are scoped strictly to user_id — teachers never see
      each other's content.
"""

from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from database import get_db
import models
from auth_utils import get_current_user

router = APIRouter(prefix="/sync", tags=["Cloud Sync"])


# ══════════════════════════════════════════════════════════════════════
# REQUEST / RESPONSE SCHEMAS
# ══════════════════════════════════════════════════════════════════════

class AnimationPayload(BaseModel):
    """
    Matches the shape of `currentAnimation` stored in the frontend.
    The `id` field is the client-generated ID (Date.now().toString()).
    """
    id:             str   = Field(..., description="Client-side animation ID (from IndexedDB)")
    title:          str   = Field(default="Untitled", max_length=500)
    prompt:         Optional[str] = ""
    explanation:    Optional[str] = ""
    animation_code: Optional[str] = ""
    playlist:       Optional[str] = "General"
    created_at:     Optional[str] = None   # ISO string from client


class SyncResponse(BaseModel):
    """Response after a successful sync."""
    success:    bool
    anim_id:    str
    message:    str


class BatchSyncRequest(BaseModel):
    animations: List[AnimationPayload]


class BatchSyncResponse(BaseModel):
    success:   bool
    synced:    int
    failed:    int
    message:   str


# ══════════════════════════════════════════════════════════════════════
# POST /sync/animations   (single upsert)
# ══════════════════════════════════════════════════════════════════════

@router.post("/animations", response_model=SyncResponse, status_code=200)
def sync_animation(
    payload: AnimationPayload,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Save or update one animation for the authenticated user.

    Logic:
      - If (user_id, anim_id) already exists → UPDATE the row
      - If it doesn't exist              → INSERT a new row

    This makes the endpoint safe to retry on network failure.
    The frontend calls this every time the user saves to library.
    """
    anim_id = payload.id.strip()

    # ── Look for existing row (this user + this client-side id) ──────
    existing = db.query(models.Animation).filter(
        models.Animation.user_id == current_user.id,
        models.Animation.anim_id == anim_id,
    ).first()

    if existing:
        # ── UPDATE ───────────────────────────────────────────────────
        existing.title          = payload.title or "Untitled"
        existing.prompt         = payload.prompt or ""
        existing.explanation    = payload.explanation or ""
        existing.animation_code = payload.animation_code or ""
        existing.playlist       = payload.playlist or "General"
        existing.updated_at     = datetime.utcnow()

        db.commit()
        print(f"[SYNC]  ↑ Updated anim_id={anim_id!r} for user={current_user.email!r}")
        return SyncResponse(success=True, anim_id=anim_id, message="Animation updated.")

    else:
        # ── INSERT ───────────────────────────────────────────────────
        # Parse client-side created_at if provided
        created_dt = datetime.utcnow()
        if payload.created_at:
            try:
                created_dt = datetime.fromisoformat(
                    payload.created_at.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except (ValueError, AttributeError):
                pass

        new_anim = models.Animation(
            anim_id        = anim_id,
            user_id        = current_user.id,
            title          = payload.title or "Untitled",
            prompt         = payload.prompt or "",
            explanation    = payload.explanation or "",
            animation_code = payload.animation_code or "",
            playlist       = payload.playlist or "General",
            created_at     = created_dt,
        )

        db.add(new_anim)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Animation '{anim_id}' could not be saved (duplicate key).",
            )

        print(f"[SYNC]  ✅ Saved anim_id={anim_id!r} for user={current_user.email!r}")
        return SyncResponse(success=True, anim_id=anim_id, message="Animation saved to cloud.")


# ══════════════════════════════════════════════════════════════════════
# POST /sync/animations/batch   (bulk upsert)
# ══════════════════════════════════════════════════════════════════════

@router.post("/animations/batch", response_model=BatchSyncResponse)
def batch_sync_animations(
    body: BatchSyncRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Bulk upsert animations — used when user first logs in on a new device
    and wants to push ALL their local IndexedDB animations to the cloud.

    Processes each animation independently so partial failures don't
    block the whole batch.
    """
    synced = 0
    failed = 0

    for payload in body.animations:
        try:
            anim_id = (payload.id or "").strip()
            if not anim_id:
                failed += 1
                continue

            existing = db.query(models.Animation).filter(
                models.Animation.user_id == current_user.id,
                models.Animation.anim_id == anim_id,
            ).first()

            if existing:
                existing.title          = payload.title or "Untitled"
                existing.prompt         = payload.prompt or ""
                existing.explanation    = payload.explanation or ""
                existing.animation_code = payload.animation_code or ""
                existing.playlist       = payload.playlist or "General"
                existing.updated_at     = datetime.utcnow()
            else:
                created_dt = datetime.utcnow()
                if payload.created_at:
                    try:
                        created_dt = datetime.fromisoformat(
                            payload.created_at.replace("Z", "+00:00")
                        ).replace(tzinfo=None)
                    except (ValueError, AttributeError):
                        pass

                new_anim = models.Animation(
                    anim_id        = anim_id,
                    user_id        = current_user.id,
                    title          = payload.title or "Untitled",
                    prompt         = payload.prompt or "",
                    explanation    = payload.explanation or "",
                    animation_code = payload.animation_code or "",
                    playlist       = payload.playlist or "General",
                    created_at     = created_dt,
                )
                db.add(new_anim)

            db.commit()
            synced += 1

        except Exception as e:
            db.rollback()
            failed += 1
            print(f"[SYNC]  ⚠ Batch item failed: {e}")

    print(f"[SYNC]  Batch complete — {synced} synced, {failed} failed — user={current_user.email!r}")
    return BatchSyncResponse(
        success = failed == 0,
        synced  = synced,
        failed  = failed,
        message = f"Synced {synced} animations. {failed} failed.",
    )


# ══════════════════════════════════════════════════════════════════════
# GET /sync/animations   (fetch all for this user)
# ══════════════════════════════════════════════════════════════════════

@router.get("/animations")
def get_animations(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return ALL animations for the authenticated user.

    Called on every login to restore the full library.
    Returns full animation_code so IndexedDB can be rebuilt completely.

    The frontend:
      1. Receives this list
      2. Merges with local IndexedDB (cloud wins for same anim_id)
      3. Re-renders the library cards
    """
    animations = (
        db.query(models.Animation)
        .filter(models.Animation.user_id == current_user.id)
        .order_by(models.Animation.created_at.desc())
        .all()
    )

    print(f"[SYNC]  ↓ Fetched {len(animations)} animations for user={current_user.email!r}")

    return {
        "user_id":    current_user.id,
        "count":      len(animations),
        "animations": [a.to_dict() for a in animations],
    }


# ══════════════════════════════════════════════════════════════════════
# DELETE /sync/animations/{anim_id}
# ══════════════════════════════════════════════════════════════════════

@router.delete("/animations/{anim_id}", response_model=SyncResponse)
def delete_animation(
    anim_id: str,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Delete one animation from cloud storage.
    Only the owner can delete their own animations.
    """
    animation = db.query(models.Animation).filter(
        models.Animation.anim_id == anim_id,
        models.Animation.user_id == current_user.id,
    ).first()

    if not animation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Animation '{anim_id}' not found.",
        )

    db.delete(animation)
    db.commit()
    print(f"[SYNC]  🗑 Deleted anim_id={anim_id!r} for user={current_user.email!r}")

    return SyncResponse(success=True, anim_id=anim_id, message="Animation deleted from cloud.")