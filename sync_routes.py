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

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
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
    # Filter out sentinels so they don't pollute the animation library
    rows = [r for r in rows if r.get("body", {}).get("anim_id") not in ("__eng_courses__", "__vault__")]

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
        .limit(1)
        .execute()
    )

    if not existing.data or len(existing.data) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item '{anim_id}' not found in your library.",
        )

    supabase.table("contents").delete().eq("id", existing.data[0]["id"]).execute()
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
        .limit(1)
        .execute()
    )

    if existing.data and len(existing.data) > 0:
        row.pop("created_at", None)
        supabase.table("contents").update(row).eq("id", existing.data[0]["id"]).execute()
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
        .limit(1)
        .execute()
    )

    if res.data and len(res.data) > 0 and res.data[0].get("body"):
        courses = res.data[0]["body"].get("courses") or []

        print(f"[SYNC] ↓ Courses fetched for user={current_user['email']!r} ({len(courses)} subjects)")
        return {"courses": courses if courses else None}

    print(f"[SYNC] ↓ No courses found for user={current_user['email']!r}")
    return {"courses": None}


# ══════════════════════════════════════════════════════════════════════
# VIDEO VAULT — PUT /sync/vault  |  GET /sync/vault  |  DELETE /sync/vault/{id}
# ══════════════════════════════════════════════════════════════════════
# The Video Vault stores lightweight metadata entries (no raw binary).
# Each entry: { id, name, fileName, size, created_at }
# All entries are packed into a single JSONB row keyed by __vault__.

_VAULT_SENTINEL = "__vault__"


class VaultPayload(BaseModel):
    """Full list of vault entries (replaces the stored list on every PUT)."""
    entries: list   # Array of { id, name, fileName, size, created_at }


class VaultEntryPayload(BaseModel):
    """A single new vault entry to upsert."""
    id:         str
    name:       str
    fileName:   str
    size:       int
    created_at: Optional[str] = None


class VaultResponse(BaseModel):
    success: bool
    message: str


# ── PUT /sync/vault  — save full vault list ───────────────────────────
@router.put(
    "/vault",
    response_model=VaultResponse,
    summary="Save the full Video Vault entry list to cloud",
)
def save_vault(
    body:         VaultPayload,
    current_user: dict = Depends(get_current_user),
):
    """Upsert the entire vault entries list for this user."""
    supabase = get_supabase(current_user.get("token"))
    user_id  = current_user["id"]

    row = {
        "user_id":  user_id,
        "title":    "__Video Vault__",
        "prompt":   "",
        "playlist": "__system__",
        "body": {
            "anim_id": _VAULT_SENTINEL,
            "entries": body.entries,
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    existing = (
        supabase.table("contents")
        .select("id")
        .eq("user_id", user_id)
        .contains("body", {"anim_id": _VAULT_SENTINEL})
        .limit(1)
        .execute()
    )

    if existing.data and len(existing.data) > 0:
        supabase.table("contents").update(row).eq("id", existing.data[0]["id"]).execute()
        print(f"[SYNC] ↑ Vault updated for user={current_user['email']!r} ({len(body.entries)} entries)")
    else:
        row["created_at"] = datetime.now(timezone.utc).isoformat()
        supabase.table("contents").insert(row).execute()
        print(f"[SYNC] ✅ Vault saved for user={current_user['email']!r} ({len(body.entries)} entries)")

    return VaultResponse(success=True, message="Video vault saved to cloud.")


# ── GET /sync/vault  — retrieve vault list ────────────────────────────
@router.get(
    "/vault",
    summary="Retrieve the Video Vault entry list from cloud",
)
def get_vault(current_user: dict = Depends(get_current_user)):
    """Return stored vault entries for this user, or empty list if none."""
    supabase = get_supabase(current_user.get("token"))
    user_id  = current_user["id"]

    res = (
        supabase.table("contents")
        .select("body")
        .eq("user_id", user_id)
        .contains("body", {"anim_id": _VAULT_SENTINEL})
        .limit(1)
        .execute()
    )

    if res.data and len(res.data) > 0 and res.data[0].get("body"):
        entries = res.data[0]["body"].get("entries") or []
        print(f"[SYNC] ↓ Vault fetched for user={current_user['email']!r} ({len(entries)} entries)")
        return {"entries": entries}

    print(f"[SYNC] ↓ No vault found for user={current_user['email']!r}")
    return {"entries": []}

@router.post(
    "/files/upload",
    summary="Upload a heavy file (mp4, html) directly to Supabase Storage",
)
async def upload_file(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    supabase = get_supabase(current_user.get("token"))
    user_id  = current_user["id"]
    
    # Prefix filename with timestamp to avoid collisions
    timestamp = int(datetime.now(timezone.utc).timestamp())
    safe_filename = file.filename.replace(" ", "_")
    storage_path = f"{user_id}/{timestamp}_{safe_filename}"
    
    file_bytes = await file.read()
    
    try:
        # Upload to 'vault' bucket
        supabase.storage.from_("vault").upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": file.content_type}
        )
        
        # Get public URL
        public_url = supabase.storage.from_("vault").get_public_url(storage_path)
        
        return {
            "success": True,
            "url": public_url,
            "filename": safe_filename,
            "path": storage_path,
        }
    except Exception as e:
        print(f"[STORAGE ERROR] Upload failed: {e}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

@router.delete(
    "/files/delete",
    summary="Delete a file from Supabase Storage",
)
def delete_file(
    path: str,
    current_user: dict = Depends(get_current_user),
):
    supabase = get_supabase(current_user.get("token"))
    
    # Ensure users can only delete from their own folder
    user_id = current_user["id"]
    if not path.startswith(f"{user_id}/"):
        raise HTTPException(status_code=403, detail="Not authorized to delete this file.")
        
    try:
        supabase.storage.from_("vault").remove([path])
        return {"success": True, "message": "File deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")
