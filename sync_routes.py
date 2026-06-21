# sync_routes.py  —  GenZet / Animind  v6.0
# =============================================
# v6.0: Full normalized schema (generated_items, engineering_subjects,
#       course_outcomes, course_topics, video_vault).
#       Legacy /sync/animations, /sync/courses, /sync/vault endpoints
#       are kept for backward compatibility during the migration window.
# =============================================

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from pydantic import BaseModel, Field

from auth_utils import get_current_user, get_supabase

router = APIRouter(prefix="/sync", tags=["Cloud Sync"])


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _parse_iso(s: Optional[str]) -> str:
    if s:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
        except (ValueError, AttributeError):
            pass
    return _now()

def _sb(user: dict):
    return get_supabase(user.get("token"))


# ══════════════════════════════════════════════════════════════════
# SCHEMA — GENERATED ITEMS  (AI Creator, Book, Question, Topic)
# ══════════════════════════════════════════════════════════════════

class GeneratedItemCreate(BaseModel):
    item_type:       str  = Field(..., description="ai_creator | book_mode | question_anim | topic_content")
    title:           str  = Field(default="Untitled", max_length=500)
    prompt:          str  = Field(default="")
    explanation:     str  = Field(default="")
    html_code:       str  = Field(default="")
    playlist:        str  = Field(default="General")
    is_saved:        bool = Field(default=False)
    source_pdf_name: Optional[str] = None
    source_topic:    Optional[str] = None
    source_subtopic: Optional[str] = None
    created_at:      Optional[str] = None
    # Legacy field — mapped to html_code for backward compat
    animation_code:  Optional[str] = None

    def resolved_html(self) -> str:
        return self.html_code or self.animation_code or ""


class GeneratedItemUpdate(BaseModel):
    title:       Optional[str] = None
    playlist:    Optional[str] = None
    is_saved:    Optional[bool] = None
    html_code:   Optional[str] = None
    explanation: Optional[str] = None


# ── POST /sync/items  — save/upsert a generated item ─────────────

@router.post("/items", status_code=200)
def save_item(
    body: GeneratedItemCreate,
    current_user: dict = Depends(get_current_user),
):
    """Save or update a generated item. Returns the item's UUID."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]

    row = {
        "user_id":         user_id,
        "item_type":       body.item_type,
        "title":           body.title or "Untitled",
        "prompt":          body.prompt or "",
        "explanation":     body.explanation or "",
        "html_code":       body.resolved_html(),
        "playlist":        body.playlist or "General",
        "is_saved":        body.is_saved,
        "source_pdf_name": body.source_pdf_name,
        "source_topic":    body.source_topic,
        "source_subtopic": body.source_subtopic,
        "created_at":      _parse_iso(body.created_at),
    }

    res = supabase.table("generated_items").insert(row).execute()
    item_id = res.data[0]["id"] if res.data else None
    print(f"[SYNC] ✅ Item saved: type={body.item_type} user={current_user['email']!r}")
    return {"success": True, "id": item_id, "message": "Saved."}


@router.put("/items/{item_id}", status_code=200)
def update_item(
    item_id: str,
    body: GeneratedItemUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update a specific generated item (title, playlist, html, is_saved)."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]

    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        return {"success": True, "message": "Nothing to update."}

    res = (
        supabase.table("generated_items")
        .update(patch)
        .eq("id", item_id)
        .eq("user_id", user_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Item not found.")
    return {"success": True, "id": item_id}


@router.get("/items", status_code=200)
def get_items(
    item_type: Optional[str] = None,
    playlist:  Optional[str] = None,
    is_saved:  Optional[bool] = None,
    current_user: dict = Depends(get_current_user),
):
    """
    Fetch generated items for the authenticated user.
    Filter by item_type, playlist, or is_saved as needed.
    """
    supabase = _sb(current_user)
    user_id  = current_user["id"]

    q = (
        supabase.table("generated_items")
        .select("id, item_type, title, prompt, explanation, html_code, playlist, is_saved, source_topic, source_subtopic, source_pdf_name, created_at, updated_at")
        .eq("user_id", user_id)
        .is_("deleted_at", "null")
        .order("created_at", desc=True)
    )
    if item_type:
        q = q.eq("item_type", item_type)
    if playlist:
        q = q.eq("playlist", playlist)
    if is_saved is not None:
        q = q.eq("is_saved", is_saved)

    res = q.execute()
    items = res.data or []
    print(f"[SYNC] ↓ {len(items)} items for user={current_user['email']!r} (type={item_type})")
    return {"count": len(items), "items": items}


@router.delete("/items/{item_id}", status_code=200)
def delete_item(
    item_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Soft-delete a generated item."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]

    res = (
        supabase.table("generated_items")
        .update({"deleted_at": _now()})
        .eq("id", item_id)
        .eq("user_id", user_id)
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Item not found.")
    return {"success": True, "id": item_id}


# ══════════════════════════════════════════════════════════════════
# SCHEMA — ENGINEERING SUBJECTS
# ══════════════════════════════════════════════════════════════════

class SubjectCreate(BaseModel):
    name:        str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="")
    sort_order:  int = Field(default=0)

class SubjectUpdate(BaseModel):
    name:             Optional[str] = None
    description:      Optional[str] = None
    sort_order:       Optional[int] = None
    syllabus_pdf_name: Optional[str] = None
    syllabus_text:    Optional[str] = None
    syllabus_units:   Optional[dict] = None


@router.post("/subjects", status_code=201)
def create_subject(
    body: SubjectCreate,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    row = {"user_id": current_user["id"], "name": body.name, "description": body.description, "sort_order": body.sort_order}
    res = supabase.table("engineering_subjects").insert(row).execute()
    subject = res.data[0] if res.data else {}
    print(f"[SYNC] ✅ Subject created: {body.name!r} user={current_user['email']!r}")
    return {"success": True, "subject": subject}


@router.put("/subjects/{subject_id}", status_code=200)
def update_subject(
    subject_id: str,
    body: SubjectUpdate,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        return {"success": True}
    res = supabase.table("engineering_subjects").update(patch).eq("id", subject_id).eq("user_id", current_user["id"]).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Subject not found.")
    return {"success": True, "subject": res.data[0]}


@router.delete("/subjects/{subject_id}", status_code=200)
def delete_subject(
    subject_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Delete subject. COs and topics cascade-delete automatically."""
    supabase = _sb(current_user)
    supabase.table("engineering_subjects").delete().eq("id", subject_id).eq("user_id", current_user["id"]).execute()
    return {"success": True}


@router.get("/subjects", status_code=200)
def get_subjects(current_user: dict = Depends(get_current_user)):
    """Return all subjects with their COs and topics in one payload."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]

    subjects_res = (
        supabase.table("engineering_subjects")
        .select("*")
        .eq("user_id", user_id)
        .order("sort_order")
        .execute()
    )
    subjects = subjects_res.data or []

    if not subjects:
        return {"subjects": []}

    subject_ids = [s["id"] for s in subjects]

    cos_res = (
        supabase.table("course_outcomes")
        .select("*")
        .in_("subject_id", subject_ids)
        .order("sort_order")
        .execute()
    )
    cos = cos_res.data or []

    co_ids = [c["id"] for c in cos]
    topics = []
    if co_ids:
        topics_res = (
            supabase.table("course_topics")
            .select("id, co_id, subject_id, name, description, prompt, sort_order, generated_item_id, html_code_cache, created_at, updated_at")
            .in_("co_id", co_ids)
            .order("sort_order")
            .execute()
        )
        topics = topics_res.data or []

    # Build nested structure matching frontend's engineeringCourses shape
    topics_by_co   = {}
    for t in topics:
        topics_by_co.setdefault(t["co_id"], []).append({
            "id":          t["id"],
            "name":        t["name"],
            "description": t.get("description", ""),
            "prompt":      t.get("prompt", ""),
            "animCode":    t.get("html_code_cache"),
            "created_at":  t["created_at"],
            "generated_item_id": t.get("generated_item_id"),
        })

    cos_by_subject = {}
    for co in cos:
        cos_by_subject.setdefault(co["subject_id"], []).append({
            "id":          co["id"],
            "coNum":       co["co_num"],
            "description": co.get("description", ""),
            "topics":      topics_by_co.get(co["id"], []),
        })

    result = []
    for s in subjects:
        result.append({
            "id":          s["id"],
            "name":        s["name"],
            "description": s.get("description", ""),
            "cos":         cos_by_subject.get(s["id"], []),
            "syllabus": {
                "pdf_name": s.get("syllabus_pdf_name"),
                "units":    s.get("syllabus_units"),
            } if s.get("syllabus_pdf_name") else None,
        })

    print(f"[SYNC] ↓ {len(result)} subjects for user={current_user['email']!r}")
    return {"subjects": result}


# ══════════════════════════════════════════════════════════════════
# SCHEMA — COURSE OUTCOMES
# ══════════════════════════════════════════════════════════════════

class COCreate(BaseModel):
    subject_id:  str
    co_num:      str = Field(..., min_length=1, max_length=20)
    description: str = Field(default="")
    sort_order:  int = Field(default=0)

class COUpdate(BaseModel):
    co_num:      Optional[str] = None
    description: Optional[str] = None
    sort_order:  Optional[int] = None


@router.post("/cos", status_code=201)
def create_co(
    body: COCreate,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    row = {
        "subject_id":  body.subject_id,
        "user_id":     current_user["id"],
        "co_num":      body.co_num,
        "description": body.description,
        "sort_order":  body.sort_order,
    }
    res = supabase.table("course_outcomes").insert(row).execute()
    return {"success": True, "co": res.data[0] if res.data else {}}


@router.put("/cos/{co_id}", status_code=200)
def update_co(
    co_id: str,
    body: COUpdate,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        return {"success": True}
    res = supabase.table("course_outcomes").update(patch).eq("id", co_id).eq("user_id", current_user["id"]).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="CO not found.")
    return {"success": True}


@router.delete("/cos/{co_id}", status_code=200)
def delete_co(
    co_id: str,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    supabase.table("course_outcomes").delete().eq("id", co_id).eq("user_id", current_user["id"]).execute()
    return {"success": True}


# ══════════════════════════════════════════════════════════════════
# SCHEMA — COURSE TOPICS
# ══════════════════════════════════════════════════════════════════

class TopicCreate(BaseModel):
    co_id:      str
    subject_id: str
    name:       str = Field(..., min_length=1, max_length=300)
    description: str = Field(default="")
    prompt:     str = Field(default="")
    sort_order: int = Field(default=0)
    # Optional: link to an already-saved generated_item
    generated_item_id: Optional[str] = None
    html_code:         Optional[str] = None   # used to fill html_code_cache


class TopicUpdate(BaseModel):
    name:              Optional[str] = None
    description:       Optional[str] = None
    prompt:            Optional[str] = None
    sort_order:        Optional[int] = None
    generated_item_id: Optional[str] = None
    html_code_cache:   Optional[str] = None


@router.post("/topics", status_code=201)
def create_topic(
    body: TopicCreate,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    row = {
        "co_id":             body.co_id,
        "subject_id":        body.subject_id,
        "user_id":           current_user["id"],
        "name":              body.name,
        "description":       body.description,
        "prompt":            body.prompt,
        "sort_order":        body.sort_order,
        "generated_item_id": body.generated_item_id,
        "html_code_cache":   body.html_code,
    }
    res = supabase.table("course_topics").insert(row).execute()
    topic = res.data[0] if res.data else {}
    print(f"[SYNC] ✅ Topic created: {body.name!r} CO={body.co_id!r}")
    return {"success": True, "topic": topic}


@router.put("/topics/{topic_id}", status_code=200)
def update_topic(
    topic_id: str,
    body: TopicUpdate,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        return {"success": True}
    res = supabase.table("course_topics").update(patch).eq("id", topic_id).eq("user_id", current_user["id"]).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Topic not found.")
    return {"success": True}


@router.delete("/topics/{topic_id}", status_code=200)
def delete_topic(
    topic_id: str,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    supabase.table("course_topics").delete().eq("id", topic_id).eq("user_id", current_user["id"]).execute()
    return {"success": True}


# ══════════════════════════════════════════════════════════════════
# VIDEO VAULT — normalized (one row per video)
# ══════════════════════════════════════════════════════════════════

class VaultEntryCreate(BaseModel):
    name:         str
    file_name:    str
    file_size:    int = 0
    storage_path: str
    public_url:   str = ""
    mime_type:    str = "video/mp4"


@router.post("/vault/entries", status_code=201)
def add_vault_entry(
    body: VaultEntryCreate,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    row = {
        "user_id":      current_user["id"],
        "name":         body.name,
        "file_name":    body.file_name,
        "file_size":    body.file_size,
        "storage_path": body.storage_path,
        "public_url":   body.public_url,
        "mime_type":    body.mime_type,
    }
    res = supabase.table("video_vault").insert(row).execute()
    entry = res.data[0] if res.data else {}
    print(f"[SYNC] ✅ Vault entry added: {body.name!r}")
    return {"success": True, "entry": entry}


@router.get("/vault/entries", status_code=200)
def get_vault_entries(current_user: dict = Depends(get_current_user)):
    supabase = _sb(current_user)
    res = (
        supabase.table("video_vault")
        .select("id, name, file_name, file_size, storage_path, public_url, mime_type, created_at")
        .eq("user_id", current_user["id"])
        .is_("deleted_at", "null")
        .order("created_at", desc=True)
        .execute()
    )
    entries = res.data or []
    return {"count": len(entries), "entries": entries}


@router.delete("/vault/entries/{entry_id}", status_code=200)
def delete_vault_entry(
    entry_id: str,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    # Get the storage path so we can delete the file too
    row = (
        supabase.table("video_vault")
        .select("storage_path")
        .eq("id", entry_id)
        .eq("user_id", current_user["id"])
        .maybe_single()
        .execute()
    )
    if row.data:
        try:
            supabase.storage.from_("vault").remove([row.data["storage_path"]])
        except Exception as e:
            print(f"[VAULT] ⚠ Storage delete failed: {e}")
        supabase.table("video_vault").update({"deleted_at": _now()}).eq("id", entry_id).execute()
    return {"success": True}


# ══════════════════════════════════════════════════════════════════
# FILE UPLOAD (Supabase Storage)
# ══════════════════════════════════════════════════════════════════

@router.post("/files/upload", status_code=200)
async def upload_file(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    ts       = int(datetime.now(timezone.utc).timestamp())
    safe_fn  = (file.filename or "upload").replace(" ", "_")
    path     = f"{user_id}/{ts}_{safe_fn}"
    data     = await file.read()

    try:
        supabase.storage.from_("vault").upload(
            path=path, file=data,
            file_options={"content-type": file.content_type or "application/octet-stream"},
        )
        url = supabase.storage.from_("vault").get_public_url(path)
        return {"success": True, "url": url, "path": path, "filename": safe_fn}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")


@router.delete("/files/delete", status_code=200)
def delete_file(
    path: str,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    if not path.startswith(f"{current_user['id']}/"):
        raise HTTPException(status_code=403, detail="Not authorized.")
    try:
        supabase.storage.from_("vault").remove([path])
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")


# ══════════════════════════════════════════════════════════════════
# FULL DATA PULL — single endpoint to load everything on login
# ══════════════════════════════════════════════════════════════════

@router.get("/all", status_code=200)
def get_all_user_data(current_user: dict = Depends(get_current_user)):
    """
    Single endpoint called after login to hydrate the frontend.
    Returns saved animations + subjects tree + vault entries.
    Replaces the three separate pull calls in _syncPull().
    """
    supabase = _sb(current_user)
    user_id  = current_user["id"]

    # 1. Generated items (saved only — not unsaved drafts)
    items_res = (
        supabase.table("generated_items")
        .select("id, item_type, title, prompt, explanation, html_code, playlist, is_saved, source_topic, created_at")
        .eq("user_id", user_id)
        .eq("is_saved", True)
        .is_("deleted_at", "null")
        .order("created_at", desc=True)
        .execute()
    )
    items = items_res.data or []

    # 2. Subjects tree (via get_subjects logic)
    subjects_res = (
        supabase.table("engineering_subjects")
        .select("*")
        .eq("user_id", user_id)
        .order("sort_order")
        .execute()
    )
    subjects = subjects_res.data or []
    subject_ids = [s["id"] for s in subjects]

    cos, topics = [], []
    if subject_ids:
        cos_res = (
            supabase.table("course_outcomes")
            .select("*")
            .in_("subject_id", subject_ids)
            .order("sort_order")
            .execute()
        )
        cos = cos_res.data or []
        co_ids = [c["id"] for c in cos]
        if co_ids:
            topics_res = (
                supabase.table("course_topics")
                .select("id, co_id, subject_id, name, description, prompt, sort_order, generated_item_id, html_code_cache, created_at")
                .in_("co_id", co_ids)
                .order("sort_order")
                .execute()
            )
            topics = topics_res.data or []

    topics_by_co = {}
    for t in topics:
        topics_by_co.setdefault(t["co_id"], []).append({
            "id": t["id"], "name": t["name"],
            "description": t.get("description", ""),
            "prompt": t.get("prompt", ""),
            "animCode": t.get("html_code_cache"),
            "created_at": t["created_at"],
            "generated_item_id": t.get("generated_item_id"),
        })

    cos_by_subject = {}
    for co in cos:
        cos_by_subject.setdefault(co["subject_id"], []).append({
            "id": co["id"], "coNum": co["co_num"],
            "description": co.get("description", ""),
            "topics": topics_by_co.get(co["id"], []),
        })

    subjects_tree = [{
        "id": s["id"], "name": s["name"], "description": s.get("description", ""),
        "cos": cos_by_subject.get(s["id"], []),
        "syllabus": {"pdf_name": s.get("syllabus_pdf_name"), "units": s.get("syllabus_units")} if s.get("syllabus_pdf_name") else None,
    } for s in subjects]

    # 3. Vault entries
    vault_res = (
        supabase.table("video_vault")
        .select("id, name, file_name, file_size, public_url, created_at")
        .eq("user_id", user_id)
        .is_("deleted_at", "null")
        .order("created_at", desc=True)
        .execute()
    )
    vault = vault_res.data or []

    print(f"[SYNC] ↓ /all — {len(items)} items, {len(subjects_tree)} subjects, {len(vault)} vault for user={current_user['email']!r}")
    return {
        "items":    items,
        "subjects": subjects_tree,
        "vault":    vault,
    }


# ══════════════════════════════════════════════════════════════════
# LEGACY ENDPOINTS (kept for backward compat during migration)
# ══════════════════════════════════════════════════════════════════
# These are the old /sync/animations, /sync/courses, /sync/vault
# endpoints. They continue to work so the current frontend doesn't
# break while you migrate. Remove them in v7.0.

_ENG_COURSES_SENTINEL = "__eng_courses__"
_VAULT_SENTINEL       = "__vault__"


class _LegacyAnimPayload(BaseModel):
    id:             str
    filename:       Optional[str] = None
    title:          str = "Untitled"
    prompt:         Optional[str] = ""
    explanation:    Optional[str] = ""
    animation_code: Optional[str] = ""
    playlist:       Optional[str] = "General"
    created_at:     Optional[str] = None


@router.post("/animations", status_code=200)
def legacy_sync_animation(payload: _LegacyAnimPayload, current_user: dict = Depends(get_current_user)):
    """LEGACY — use POST /sync/items instead."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    anim_id  = payload.id.strip()
    title    = (payload.filename or payload.title or "Untitled").strip()
    existing = supabase.table("contents").select("id").eq("user_id", user_id).contains("body", {"anim_id": anim_id}).maybe_single().execute()
    row = {"user_id": user_id, "title": title, "prompt": payload.prompt or "", "playlist": payload.playlist or "General",
           "body": {"anim_id": anim_id, "filename": title, "explanation": payload.explanation or "", "animation_code": payload.animation_code or ""},
           "created_at": _parse_iso(payload.created_at)}
    if existing.data:
        row.pop("created_at", None)
        supabase.table("contents").update(row).eq("id", existing.data["id"]).execute()
    else:
        supabase.table("contents").insert(row).execute()
    return {"success": True, "anim_id": anim_id, "message": "Saved (legacy)."}


@router.get("/animations", status_code=200)
def legacy_get_animations(current_user: dict = Depends(get_current_user)):
    """LEGACY — use GET /sync/items instead."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    res = supabase.table("contents").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
    rows = [r for r in (res.data or []) if r.get("body", {}).get("anim_id") not in (_ENG_COURSES_SENTINEL, _VAULT_SENTINEL)]
    animations = [{"id": r["body"].get("anim_id", r["id"]), "filename": r["body"].get("filename") or r["title"], "title": r["title"],
                   "prompt": r["prompt"], "explanation": r["body"].get("explanation", ""), "animation_code": r["body"].get("animation_code", ""),
                   "playlist": r["playlist"], "created_at": r["created_at"]} for r in rows]
    return {"user_id": user_id, "count": len(animations), "animations": animations}


@router.delete("/animations/{anim_id}", status_code=200)
def legacy_delete_animation(anim_id: str, current_user: dict = Depends(get_current_user)):
    """LEGACY — use DELETE /sync/items/{id} instead."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    existing = supabase.table("contents").select("id").eq("user_id", user_id).contains("body", {"anim_id": anim_id}).limit(1).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail=f"Item '{anim_id}' not found.")
    supabase.table("contents").delete().eq("id", existing.data[0]["id"]).execute()
    return {"success": True, "anim_id": anim_id, "message": "Deleted (legacy)."}


class _CoursesPayload(BaseModel):
    courses: list

@router.put("/courses", status_code=200)
def legacy_save_courses(body: _CoursesPayload, current_user: dict = Depends(get_current_user)):
    """LEGACY — use POST /sync/subjects (and /cos, /topics) instead."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    row = {"user_id": user_id, "title": "__Engineering Courses__", "prompt": "", "playlist": "__system__",
           "body": {"anim_id": _ENG_COURSES_SENTINEL, "courses": body.courses}, "updated_at": _now()}
    all_ex = supabase.table("contents").select("id, created_at").eq("user_id", user_id).contains("body", {"anim_id": _ENG_COURSES_SENTINEL}).order("created_at", desc=True).execute()
    rows = all_ex.data or []
    if len(rows) > 1:
        for r in rows[1:]: supabase.table("contents").delete().eq("id", r["id"]).execute()
    if rows:
        row.pop("created_at", None)
        supabase.table("contents").update(row).eq("id", rows[0]["id"]).execute()
    else:
        row["created_at"] = _now()
        supabase.table("contents").insert(row).execute()
    return {"success": True, "message": "Courses saved (legacy)."}


@router.get("/courses", status_code=200)
def legacy_get_courses(current_user: dict = Depends(get_current_user)):
    """LEGACY — use GET /sync/subjects instead."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    res = supabase.table("contents").select("body").eq("user_id", user_id).contains("body", {"anim_id": _ENG_COURSES_SENTINEL}).order("created_at", desc=True).limit(1).execute()
    if res.data and res.data[0].get("body"):
        return {"courses": res.data[0]["body"].get("courses") or []}
    return {"courses": []}


class _VaultPayload(BaseModel):
    entries: list

@router.put("/vault", status_code=200)
def legacy_save_vault(body: _VaultPayload, current_user: dict = Depends(get_current_user)):
    """LEGACY — use POST /sync/vault/entries instead."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    row = {"user_id": user_id, "title": "__Video Vault__", "prompt": "", "playlist": "__system__",
           "body": {"anim_id": _VAULT_SENTINEL, "entries": body.entries}, "updated_at": _now()}
    all_ex = supabase.table("contents").select("id, created_at").eq("user_id", user_id).contains("body", {"anim_id": _VAULT_SENTINEL}).order("created_at", desc=True).execute()
    rows = all_ex.data or []
    if len(rows) > 1:
        for r in rows[1:]: supabase.table("contents").delete().eq("id", r["id"]).execute()
    if rows:
        supabase.table("contents").update(row).eq("id", rows[0]["id"]).execute()
    else:
        row["created_at"] = _now()
        supabase.table("contents").insert(row).execute()
    return {"success": True, "message": "Vault saved (legacy)."}


@router.get("/vault", status_code=200)
def legacy_get_vault(current_user: dict = Depends(get_current_user)):
    """LEGACY — use GET /sync/vault/entries instead."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    res = supabase.table("contents").select("body").eq("user_id", user_id).contains("body", {"anim_id": _VAULT_SENTINEL}).order("created_at", desc=True).limit(1).execute()
    if res.data and res.data[0].get("body"):
        return {"entries": res.data[0]["body"].get("entries") or []}
    return {"entries": []}


class SaveHtmlRequest(BaseModel):
    filename:    str = Field(..., min_length=1, max_length=200)
    html:        str = Field(..., min_length=1)
    prompt:      Optional[str] = ""
    explanation: Optional[str] = ""
    playlist:    Optional[str] = "General"
    client_id:   Optional[str] = None

@router.post("/animations/save-html", status_code=200)
def legacy_save_html(body: SaveHtmlRequest, current_user: dict = Depends(get_current_user)):
    """LEGACY — now calls the new /sync/items endpoint internally."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    filename = body.filename.strip()
    anim_id  = body.client_id or f"html_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    row = {"user_id": user_id, "title": filename, "prompt": body.prompt or "", "playlist": body.playlist or "General",
           "body": {"anim_id": anim_id, "filename": filename, "explanation": body.explanation or "", "animation_code": body.html},
           "created_at": _now()}
    existing = supabase.table("contents").select("id").eq("user_id", user_id).contains("body", {"anim_id": anim_id}).maybe_single().execute()
    if existing.data:
        row.pop("created_at", None)
        supabase.table("contents").update(row).eq("id", existing.data["id"]).execute()
    else:
        supabase.table("contents").insert(row).execute()
    return {"success": True, "anim_id": anim_id, "filename": filename, "message": f'"{filename}" saved.'}


class _BatchSyncRequest(BaseModel):
    animations: List[_LegacyAnimPayload]

@router.post("/animations/batch", status_code=200)
def legacy_batch_sync(body: _BatchSyncRequest, current_user: dict = Depends(get_current_user)):
    """LEGACY batch upsert — kept for migration."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    synced = failed = 0
    for payload in body.animations:
        try:
            anim_id = (payload.id or "").strip()
            if not anim_id: failed += 1; continue
            title = (payload.filename or payload.title or "Untitled").strip()
            existing = supabase.table("contents").select("id").eq("user_id", user_id).contains("body", {"anim_id": anim_id}).maybe_single().execute()
            row = {"user_id": user_id, "title": title, "prompt": payload.prompt or "", "playlist": payload.playlist or "General",
                   "body": {"anim_id": anim_id, "filename": title, "explanation": payload.explanation or "", "animation_code": payload.animation_code or ""},
                   "created_at": _parse_iso(payload.created_at)}
            if existing.data:
                row.pop("created_at", None)
                supabase.table("contents").update(row).eq("id", existing.data["id"]).execute()
            else:
                supabase.table("contents").insert(row).execute()
            synced += 1
        except Exception as e:
            failed += 1
            print(f"[SYNC] ⚠ Batch item failed: {e}")
    return {"success": failed == 0, "synced": synced, "failed": failed, "message": f"Synced {synced}. {failed} failed."}
