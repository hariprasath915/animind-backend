# sync_routes.py  —  GenZet / Animind  v5.2
# ============================================
#
# Changes in v5.2
# ---------------
# Feature 2 — Save HTML output to Supabase library
#   * AnimationPayload gains a `filename` field (the name the user typed
#     in the "Save in Library" modal).  When present it overrides `title`.
#   * GET /sync/animations response now includes `filename` so the library
#     UI can display it as the card heading.
#   * The `animation_code` field in body JSONB already holds the full HTML;
#     no schema change needed — it was always there.
#   * Added POST /sync/animations/save-html  — dedicated "Save HTML" endpoint
#     that the frontend calls directly from the dashboard Save button.
#     It accepts { filename, html, prompt?, explanation?, playlist? }.
#
# Feature 1 — user_id persistence
#   * No changes here.  user_id comes from get_current_user() (JWT) on
#     every route — it is auth.users.id, stable and cross-device by design.
#
# Unchanged
# ---------
#   * POST /sync/animations        (single upsert, now with filename)
#   * POST /sync/animations/batch  (bulk upsert)
#   * GET  /sync/animations        (fetch all, now returns filename)
#   * DELETE /sync/animations/{id} (unchanged)
# ============================================

from typing import List, Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from auth_utils import get_current_user, get_supabase

router = APIRouter(prefix="/sync", tags=["Cloud Sync"])


# ══════════════════════════════════════════════════════════════════════
# SCHEMAS
# ══════════════════════════════════════════════════════════════════════

class AnimationPayload(BaseModel):
    """
    Shape sent by the frontend when saving a generated animation.

    `id`       — client-generated timestamp ID (Date.now().toString())
    `filename` — name the user typed in the "Save in Library" modal.
                 Stored in body.filename and also used as the row title.
    """
    id:             str   = Field(..., description="Client-side animation ID")
    # filename is the user-facing name ("My Photosynthesis Lesson").
    # title is a fallback if filename is not provided.
    filename:       Optional[str] = None
    title:          str   = Field(default="Untitled", max_length=500)
    prompt:         Optional[str] = ""
    explanation:    Optional[str] = ""
    animation_code: Optional[str] = ""   # full HTML string
    playlist:       Optional[str] = "General"
    created_at:     Optional[str] = None


class SaveHtmlRequest(BaseModel):
    """
    Dedicated payload for the dashboard "Save in Library" button.

    The frontend sends this after the user types a filename and clicks Save.
    `html` is the complete generated HTML output string.
    """
    filename:    str   = Field(..., min_length=1, max_length=200,
                               description="Name the user typed in the Save modal")
    html:        str   = Field(..., min_length=1,
                               description="Complete HTML string to save")
    prompt:      Optional[str] = ""
    explanation: Optional[str] = ""
    playlist:    Optional[str] = "General"
    # client_id lets the frontend do optimistic UI — pass Date.now().toString()
    client_id:   Optional[str] = None


class SyncResponse(BaseModel):
    success: bool
    anim_id: str
    message: str


class SaveHtmlResponse(BaseModel):
    success:  bool
    anim_id:  str   # the ID you can use to delete / update later
    filename: str
    message:  str


class BatchSyncRequest(BaseModel):
    animations: List[AnimationPayload]


class BatchSyncResponse(BaseModel):
    success: bool
    synced:  int
    failed:  int
    message: str


# ══════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════

def _parse_iso(s: Optional[str]) -> str:
    """Return a UTC ISO string, defaulting to now()."""
    if s:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
        except (ValueError, AttributeError):
            pass
    return datetime.now(timezone.utc).isoformat()


def _resolve_title(payload: AnimationPayload) -> str:
    """filename wins over title; fall back to 'Untitled'."""
    return (payload.filename or payload.title or "Untitled").strip()


def _payload_to_row(payload: AnimationPayload, user_id: str) -> dict:
    """
    Convert AnimationPayload → Supabase contents row.
    user_id always comes from the verified JWT — never from the client body.
    """
    resolved_title = _resolve_title(payload)
    return {
        "user_id":  user_id,
        "title":    resolved_title,
        "prompt":   payload.prompt or "",
        "playlist": payload.playlist or "General",
        "body": {
            "anim_id":        payload.id,
            "filename":       payload.filename or resolved_title,
            "explanation":    payload.explanation or "",
            "animation_code": payload.animation_code or "",
        },
        "created_at": _parse_iso(payload.created_at),
    }


# ══════════════════════════════════════════════════════════════════════
# POST /sync/animations/save-html   ← NEW (Feature 2)
# ══════════════════════════════════════════════════════════════════════

@router.post(
    "/animations/save-html",
    response_model=SaveHtmlResponse,
    status_code=200,
    summary="Save generated HTML output to the user's library",
)
def save_html(
    body:         SaveHtmlRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Called by the dashboard "Save in Library" button.

    The user types a filename in the modal, clicks Save, and the frontend
    sends this request.  The HTML is stored in body.animation_code (JSONB).

    Upsert semantics: if the same filename already exists for this user,
    the HTML is replaced (idempotent, safe to retry).

    Returns anim_id which the frontend uses for subsequent deletes.
    """
    supabase = get_supabase(current_user.get("token"))
    user_id  = current_user["id"]
    filename = body.filename.strip()
    anim_id  = body.client_id or f"html_{int(datetime.now(timezone.utc).timestamp() * 1000)}"

    row = {
        "user_id":  user_id,
        "title":    filename,
        "prompt":   body.prompt or "",
        "playlist": body.playlist or "General",
        "body": {
            "anim_id":        anim_id,
            "filename":       filename,
            "explanation":    body.explanation or "",
            "animation_code": body.html,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    # Check if a row with the same anim_id already exists for this user
    existing = (
        supabase.table("contents")
        .select("id")
        .eq("user_id", user_id)
        .contains("body", {"anim_id": anim_id})
        .maybe_single()
        .execute()
    )

    if existing.data:
        row_without_created = {k: v for k, v in row.items() if k != "created_at"}
        supabase.table("contents").update(row_without_created).eq("id", existing.data["id"]).execute()
        print(f"[SYNC] ↑ HTML updated: filename={filename!r} user={current_user['email']!r}")
    else:
        supabase.table("contents").insert(row).execute()
        print(f"[SYNC] ✅ HTML saved:  filename={filename!r} user={current_user['email']!r}")

    return SaveHtmlResponse(
        success=True,
        anim_id=anim_id,
        filename=filename,
        message=f'"{filename}" saved to your library.',
    )


# ══════════════════════════════════════════════════════════════════════
# POST /sync/animations   (single upsert — existing endpoint, extended)
# ══════════════════════════════════════════════════════════════════════

@router.post("/animations", response_model=SyncResponse, status_code=200)
def sync_animation(
    payload:      AnimationPayload,
    current_user: dict = Depends(get_current_user),
):
    """
    Upsert one animation for the authenticated user.
    Now also accepts `filename` — when provided it becomes the library card title.
    """
    supabase = get_supabase(current_user.get("token"))
    user_id  = current_user["id"]
    anim_id  = payload.id.strip()

    existing = (
        supabase.table("contents")
        .select("id")
        .eq("user_id", user_id)
        .contains("body", {"anim_id": anim_id})
        .maybe_single()
        .execute()
    )

    row = _payload_to_row(payload, user_id)

    if existing.data:
        row.pop("created_at", None)
        supabase.table("contents").update(row).eq("id", existing.data["id"]).execute()
        print(f"[SYNC] ↑ Updated anim_id={anim_id!r} user={current_user['email']!r}")
        return SyncResponse(success=True, anim_id=anim_id, message="Animation updated.")
    else:
        supabase.table("contents").insert(row).execute()
        print(f"[SYNC] ✅ Saved anim_id={anim_id!r} user={current_user['email']!r}")
        return SyncResponse(success=True, anim_id=anim_id, message="Animation saved to library.")


# ══════════════════════════════════════════════════════════════════════
# POST /sync/animations/batch   (bulk upsert — unchanged logic)
# ══════════════════════════════════════════════════════════════════════

@router.post("/animations/batch", response_model=BatchSyncResponse)
def batch_sync_animations(
    body:         BatchSyncRequest,
    current_user: dict = Depends(get_current_user),
):
    """Bulk upsert — used to push all local items to cloud on first login."""
    supabase = get_supabase(current_user.get("token"))
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
                .contains("body", {"anim_id": anim_id})
                .maybe_single()
                .execute()
            )

            row = _payload_to_row(payload, user_id)

            if existing.data:
                row.pop("created_at", None)
                supabase.table("contents").update(row).eq("id", existing.data["id"]).execute()
            else:
                supabase.table("contents").insert(row).execute()

            synced += 1
        except Exception as e:
            failed += 1
            print(f"[SYNC] ⚠ Batch item failed: {e}")

    print(f"[SYNC] Batch done — {synced} ok, {failed} failed — user={current_user['email']!r}")
    return BatchSyncResponse(
        success=failed == 0,
        synced=synced,
        failed=failed,
        message=f"Synced {synced}. {failed} failed.",
    )


# ══════════════════════════════════════════════════════════════════════
# GET /sync/animations   (fetch all — now returns filename)
# ══════════════════════════════════════════════════════════════════════

@router.get("/animations")
def get_animations(current_user: dict = Depends(get_current_user)):
    """
    Return all saved animations/HTML files for the authenticated user.
    user_id comes from the JWT — only this user's rows are returned.
    Each item includes `filename` for the library card UI.
    """
    supabase = get_supabase(current_user.get("token"))
    user_id  = current_user["id"]

    res = (
        supabase.table("contents")
        .select("*")
        .eq("user_id", user_id)          # ← scoped to this user only
        .order("created_at", desc=True)
        .execute()
    )

    rows = res.data or []
    animations = [
        {
            # `id` is the client-side anim_id (used for deletes)
            "id":             r["body"].get("anim_id", r["id"]),
            # `filename` is the name the user gave it in the Save modal
            "filename":       r["body"].get("filename") or r["title"],
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


# ══════════════════════════════════════════════════════════════════════
# DELETE /sync/animations/{anim_id}
# ══════════════════════════════════════════════════════════════════════

@router.delete("/animations/{anim_id}", response_model=SyncResponse)
def delete_animation(
    anim_id:      str,
    current_user: dict = Depends(get_current_user),
):
    """Delete one saved item. Only the owner can delete their rows."""
    supabase = get_supabase(current_user.get("token"))
    user_id  = current_user["id"]

    existing = (
        supabase.table("contents")
        .select("id")
        .eq("user_id", user_id)
        .contains("body", {"anim_id": anim_id})
        .maybe_single()
        .execute()
    )

    if not existing.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{anim_id}' not found in your library.",
        )

    supabase.table("contents").delete().eq("id", existing.data["id"]).execute()
    print(f"[SYNC] 🗑 Deleted anim_id={anim_id!r} user={current_user['email']!r}")
    return SyncResponse(success=True, anim_id=anim_id, message="Item deleted from library.")


# ══════════════════════════════════════════════════════════════════════
# PUT /sync/courses   — save the full engineeringCourses structure
# ══════════════════════════════════════════════════════════════════════
# The frontend's File mode stores subjects → COs → topics in a local
# array called `engineeringCourses`.  This endpoint persists the entire
# structure as a single JSONB document in the `contents` table, using
# a sentinel anim_id of "__eng_courses__" to distinguish it from
# individual animation rows.
#
# Upsert semantics: if a row with __eng_courses__ already exists for
# this user, it is replaced; otherwise a new row is inserted.

_ENG_COURSES_SENTINEL = "__eng_courses__"


class CoursesPayload(BaseModel):
    """The full engineeringCourses array sent by the frontend."""
    courses: list  # Array of subject objects — stored as-is in JSONB


class CoursesResponse(BaseModel):
    success: bool
    message: str


@router.put(
    "/courses",
    response_model=CoursesResponse,
    summary="Save the full engineering-courses structure to cloud",
)
def save_courses(
    body:         CoursesPayload,
    current_user: dict = Depends(get_current_user),
):
    """
    Upsert the entire engineeringCourses tree for this user.

    The structure is stored as JSONB in `body.courses` inside a single
    `contents` row keyed by anim_id = "__eng_courses__".
    """
    supabase = get_supabase(current_user.get("token"))
    user_id  = current_user["id"]

    row = {
        "user_id":  user_id,
        "title":    "__Engineering Courses__",
        "prompt":   "",
        "playlist": "__system__",
        "body": {
            "anim_id": _ENG_COURSES_SENTINEL,
            "courses": body.courses,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    existing = (
        supabase.table("contents")
        .select("id")
        .eq("user_id", user_id)
        .contains("body", {"anim_id": _ENG_COURSES_SENTINEL})
        .maybe_single()
        .execute()
    )

    if existing.data:
        row.pop("created_at", None)
        supabase.table("contents").update(row).eq("id", existing.data["id"]).execute()
        print(f"[SYNC] ↑ Courses updated for user={current_user['email']!r}")
    else:
        supabase.table("contents").insert(row).execute()
        print(f"[SYNC] ✅ Courses saved for user={current_user['email']!r}")

    return CoursesResponse(success=True, message="Engineering courses saved to cloud.")


@router.get(
    "/courses",
    summary="Retrieve the engineering-courses structure from cloud",
)
def get_courses(current_user: dict = Depends(get_current_user)):
    """
    Return the stored engineeringCourses array for this user.
    Returns { courses: [...] } or { courses: null } if none saved yet.
    """
    supabase = get_supabase(current_user.get("token"))
    user_id  = current_user["id"]

    res = (
        supabase.table("contents")
        .select("body")
        .eq("user_id", user_id)
        .contains("body", {"anim_id": _ENG_COURSES_SENTINEL})
        .maybe_single()
        .execute()
    )

    if res.data and res.data.get("body"):
        courses = res.data["body"].get("courses") or []

        # Strip legacy pre-seeded course IDs that were auto-pushed before users
        # created their own subjects. Filtering here ensures no client ever
        # sees Engineering Physics / Chemistry / Mathematics / etc. again,
        # regardless of what's stored in Supabase from old sessions.
        LEGACY_DEFAULT_IDS = {"ep", "ec", "em", "ht", "mc"}
        courses = [c for c in courses if c.get("id") not in LEGACY_DEFAULT_IDS]

        print(f"[SYNC] ↓ Courses fetched for user={current_user['email']!r} ({len(courses)} subjects)")
        return {"courses": courses if courses else None}

    print(f"[SYNC] ↓ No courses found for user={current_user['email']!r}")
    return {"courses": None}
