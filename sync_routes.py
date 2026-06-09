# sync_routes.py  —  Cloud Sync via Supabase
# ============================================
# What changed from v4.x:
#   - Removed: SQLAlchemy db queries, models.Animation, IntegrityError handling
#   - Added:   supabase-py calls on `contents` table, always filtered by user_id
#   - Kept:    Same route paths, same AnimationPayload/SyncResponse schemas,
#              same upsert semantics (match on user_id + body->>'anim_id').
#
# user_id = current_user["id"] (auth.users.id) is extracted
# from the verified JWT by get_current_user() and injected
# into every Supabase insert/query.  Teachers never see each
# other's content because every query is scoped by user_id.

from typing import List, Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from auth_utils import get_current_user, get_supabase

router = APIRouter(prefix="/sync", tags=["Cloud Sync"])


# ── Schemas ────────────────────────────────────────────────────────────

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
    success:  bool
    anim_id:  str
    message:  str


class BatchSyncRequest(BaseModel):
    animations: List[AnimationPayload]


class BatchSyncResponse(BaseModel):
    success:  bool
    synced:   int
    failed:   int
    message:  str


# ── helpers ────────────────────────────────────────────────────────────

def _parse_iso(s: Optional[str]) -> str:
    """Return ISO string for created_at, defaulting to now."""
    if s:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
        except (ValueError, AttributeError):
            pass
    return datetime.now(timezone.utc).isoformat()


def _payload_to_row(payload: AnimationPayload, user_id: str) -> dict:
    """
    Convert AnimationPayload → Supabase row dict.
    user_id (auth.users.id) is injected here so it's always set
    from the verified token, never from untrusted client input.
    """
    return {
        # user_id flows: JWT → get_current_user() → here → Supabase
        "user_id":    user_id,
        "title":      payload.title or "Untitled",
        "prompt":     payload.prompt or "",
        "playlist":   payload.playlist or "General",
        # body is a JSONB column storing the generated content blob
        "body": {
            "anim_id":        payload.id,
            "explanation":    payload.explanation or "",
            "animation_code": payload.animation_code or "",
        },
        "created_at": _parse_iso(payload.created_at),
    }


# ── POST /sync/animations  (single upsert) ────────────────────────────

@router.post("/animations", response_model=SyncResponse, status_code=200)
def sync_animation(
    payload: AnimationPayload,
    current_user: dict = Depends(get_current_user),  # ← extracts user_id from JWT
):
    """
    Upsert one animation for the authenticated teacher.
    We match on (user_id, body->>'anim_id') — same semantics as before.
    This makes the endpoint safe to retry on network failure.
    """
    supabase = get_supabase()
    user_id  = current_user["id"]   # auth.users.id — scopes this write
    anim_id  = payload.id.strip()

    # Check if this (user, anim_id) already exists
    existing = (
        supabase.table("contents")
        .select("id")
        .eq("user_id", user_id)
        .eq("body->>anim_id", anim_id)   # query inside JSONB
        .maybe_single()
        .execute()
    )

    row = _payload_to_row(payload, user_id)

    if existing.data:
        # UPDATE — preserve created_at, bump updated_at automatically
        row.pop("created_at", None)
        supabase.table("contents") \
            .update(row) \
            .eq("id", existing.data["id"]) \
            .execute()
        print(f"[SYNC] ↑ Updated anim_id={anim_id!r} user={current_user['email']!r}")
        return SyncResponse(success=True, anim_id=anim_id, message="Animation updated.")
    else:
        # INSERT
        supabase.table("contents").insert(row).execute()
        print(f"[SYNC] ✅ Saved anim_id={anim_id!r} user={current_user['email']!r}")
        return SyncResponse(success=True, anim_id=anim_id, message="Animation saved to cloud.")


# ── POST /sync/animations/batch  (bulk upsert) ────────────────────────

@router.post("/animations/batch", response_model=BatchSyncResponse)
def batch_sync_animations(
    body: BatchSyncRequest,
    current_user: dict = Depends(get_current_user),
):
    """Bulk upsert — used on first login to push all local IndexedDB items."""
    supabase = get_supabase()
    user_id  = current_user["id"]
    synced = failed = 0

    for payload in body.animations:
        try:
            anim_id = (payload.id or "").strip()
            if not anim_id:
                failed += 1
                continue

            existing = (
                supabase.table("contents")
                .select("id")
                .eq("user_id", user_id)
                .eq("body->>anim_id", anim_id)
                .maybe_single()
                .execute()
            )

            row = _payload_to_row(payload, user_id)

            if existing.data:
                row.pop("created_at", None)
                supabase.table("contents") \
                    .update(row) \
                    .eq("id", existing.data["id"]) \
                    .execute()
            else:
                supabase.table("contents").insert(row).execute()

            synced += 1
        except Exception as e:
            failed += 1
            print(f"[SYNC] ⚠ Batch item failed: {e}")

    print(f"[SYNC] Batch done — {synced} ok, {failed} failed — user={current_user['email']!r}")
    return BatchSyncResponse(
        success=failed == 0, synced=synced, failed=failed,
        message=f"Synced {synced}. {failed} failed.",
    )


# ── GET /sync/animations  (fetch all for this user) ───────────────────

@router.get("/animations")
def get_animations(current_user: dict = Depends(get_current_user)):
    """
    Return ALL contents rows for this teacher.
    The .eq("user_id", user_id) filter is mandatory — never omit it.
    RLS is a backup, but explicit scoping is the primary guard.
    """
    supabase = get_supabase()
    user_id  = current_user["id"]   # scopes the SELECT to this teacher only

    res = (
        supabase.table("contents")
        .select("*")
        .eq("user_id", user_id)          # ← user_id from verified JWT
        .order("created_at", desc=True)
        .execute()
    )

    rows = res.data or []
    # Flatten JSONB body back to the shape the frontend expects
    animations = [
        {
            "id":             r["body"].get("anim_id", r["id"]),
            "title":          r["title"],
            "prompt":         r["prompt"],
            "explanation":    r["body"].get("explanation", ""),
            "animation_code": r["body"].get("animation_code", ""),
            "playlist":       r["playlist"],
            "created_at":     r["created_at"],
        }
        for r in rows
    ]

    print(f"[SYNC] ↓ Fetched {len(animations)} items for user={current_user['email']!r}")
    return {"user_id": user_id, "count": len(animations), "animations": animations}


# ── DELETE /sync/animations/{anim_id} ─────────────────────────────────

@router.delete("/animations/{anim_id}", response_model=SyncResponse)
def delete_animation(
    anim_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Delete one animation. Only the owner can delete their rows."""
    supabase = get_supabase()
    user_id  = current_user["id"]

    existing = (
        supabase.table("contents")
        .select("id")
        .eq("user_id", user_id)
        .eq("body->>anim_id", anim_id)
        .maybe_single()
        .execute()
    )

    if not existing.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Animation '{anim_id}' not found.",
        )

    supabase.table("contents") \
        .delete() \
        .eq("id", existing.data["id"]) \
        .execute()

    print(f"[SYNC] 🗑 Deleted anim_id={anim_id!r} user={current_user['email']!r}")
    return SyncResponse(success=True, anim_id=anim_id, message="Animation deleted from cloud.")
