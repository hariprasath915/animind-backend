# sync_routes.py  —  GenZet / Animind  v6.0
# ============================================================
# v6.0 — Full normalized schema
#   NEW endpoints (all require JWT):
#     GET  /sync/all                      → full data pull on login
#     POST /sync/items                    → save generated item
#     GET  /sync/items                    → list saved items
#     PUT  /sync/items/{id}               → update item
#     DELETE /sync/items/{id}             → soft-delete item
#     GET  /sync/subjects                 → full subjects+COs+topics tree
#     POST /sync/subjects                 → create subject
#     PUT  /sync/subjects/{id}            → update subject
#     DELETE /sync/subjects/{id}          → delete subject (cascade)
#     POST /sync/cos                      → create CO
#     PUT  /sync/cos/{co_id}              → update CO
#     DELETE /sync/cos/{co_id}            → delete CO (cascade)
#     POST /sync/topics                   → create topic
#     PUT  /sync/topics/{topic_id}        → update topic / attach HTML
#     DELETE /sync/topics/{topic_id}      → delete topic
#     GET  /sync/vault/entries            → list vault entries
#     POST /sync/vault/entries            → add vault entry
#     DELETE /sync/vault/entries/{id}     → delete entry + storage file
#     POST /sync/files/upload             → upload to Supabase Storage
#     DELETE /sync/files/delete           → delete from Supabase Storage
#
#   LEGACY endpoints (kept for backward compat — remove in v7.0):
#     GET/POST/DELETE /sync/animations
#     POST /sync/animations/batch
#     POST /sync/animations/save-html
#     GET/PUT /sync/courses
#     GET/PUT /sync/vault
# ============================================================

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import List, Optional

import os
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from pydantic import BaseModel, Field

from auth_utils import get_current_user, get_supabase

# ── Main-user identity (set MAIN_USER_EMAIL in Railway env vars) ─────────────
MAIN_USER_EMAIL = os.getenv("MAIN_USER_EMAIL", "genzet@gmail.com")

router = APIRouter(prefix="/sync", tags=["Cloud Sync"])


# ════════════════════════════════════════════════════════════════
# PUBLIC CONFIG  —  GET /sync/config  (no JWT required)
# Returns the Supabase public URL and anon key so the browser
# can initialise Supabase Realtime WebSocket subscriptions.
# The anon key is intentionally public — it only allows read
# access to rows/buckets that have permissive RLS policies.
# ════════════════════════════════════════════════════════════════

@router.get("/config", status_code=200)
def get_public_config():
    """
    Returns public Supabase configuration needed by the browser.
    No authentication required.
    """
    return {
        "supabase_url":      os.getenv("SUPABASE_URL", ""),
        "supabase_anon_key": os.getenv("SUPABASE_ANON_KEY", ""),
    }


# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════

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
    """Return a user-scoped Supabase client (anon key + user JWT)."""
    return get_supabase(user.get("token"))


def _sb_admin():
    """Return a service-role Supabase client (bypasses RLS).
    Only use for routes that read shared/public data, not user-owned rows."""
    return get_supabase()


# ════════════════════════════════════════════════════════════════
# FULL DATA PULL  —  GET /sync/all
# Single endpoint called after login to hydrate the frontend.
# ════════════════════════════════════════════════════════════════

@router.get("/all", status_code=200)
def get_all_user_data(current_user: dict = Depends(get_current_user)):
    """
    Returns saved items + subjects tree + vault entries in one round-trip.
    Replaces the three separate pull calls that existed in v2.
    """
    # Fix #5: Wrap all DB calls in try/except so a Supabase 401 / connection
    # error returns a clean 502 instead of propagating as an unhandled 500
    # that floods Railway with 500+ log lines/sec and triggers the rate limiter.
    try:
        supabase = _sb(current_user)
        user_id  = current_user["id"]

        # ── 1. Generated items (is_saved=True only) ──────────────────
        items_res = (
            supabase.table("generated_items")
            .select("id, item_type, title, prompt, explanation, html_code, playlist, is_saved, source_topic, source_subtopic, source_pdf_name, created_at, updated_at")
            .eq("user_id", user_id)
            .eq("is_saved", True)
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .execute()
        )
        items = items_res.data or []

        # ── 2. Subjects tree ─────────────────────────────────────────
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
                    .select("id, co_id, subject_id, name, description, prompt, sort_order, generated_item_id, html_code_cache, topic_type, ppt_storage_path, ppt_public_url, ppt_file_name, created_at")
                    .in_("co_id", co_ids)
                    .order("sort_order")
                    .execute()
                )
                topics = topics_res.data or []

        # Build nested engineeringCourses-compatible structure
        topics_by_co = {}
        for t in topics:
            html_cache   = t.get("html_code_cache")
            db_type      = t.get("topic_type") or "animation"
            # Derive display type: prefer DB value, fall back to content sniffing
            if db_type == "ppt_upload":
                display_type = "ppt_upload"
            elif db_type == "html_upload" or (html_cache and html_cache.strip().startswith("<!DOCTYPE")):
                display_type = "html_upload"
            else:
                display_type = "animation"
            topics_by_co.setdefault(t["co_id"], []).append({
                "id":               t["id"],
                "name":             t["name"],
                "description":      t.get("description", ""),
                "prompt":           t.get("prompt", ""),
                # animCode and html_code carry HTML for animation/html_upload topics
                "animCode":         html_cache,
                "html_code":        html_cache,
                # PPT-specific fields
                "type":             display_type,
                "pptUrl":           t.get("ppt_public_url"),
                "pptStoragePath":   t.get("ppt_storage_path"),
                "fileName":         t.get("ppt_file_name"),
                "created_at":       t["created_at"],
                "generated_item_id": t.get("generated_item_id"),
            })

        cos_by_subject = {}
        for co in cos:
            cos_by_subject.setdefault(co["subject_id"], []).append({
                "id":          co["id"],
                "coNum":       co["co_num"],
                "name":        co.get("description", ""),   # ✅ frontend reads co.name
                "description": co.get("description", ""),
                "topics":      topics_by_co.get(co["id"], []),
            })

        subjects_tree = [
            {
                "id":          s["id"],
                "name":        s["name"],
                "description": s.get("description", ""),
                "share_token": s.get("share_token"),
                "cos":         cos_by_subject.get(s["id"], []),
                "syllabus": {
                    "pdf_name": s.get("syllabus_pdf_name"),
                    "units":    s.get("syllabus_units"),
                } if s.get("syllabus_pdf_name") else None,
            }
            for s in subjects
        ]

        # ── 3. Vault entries ─────────────────────────────────────────
        vault_res = (
            supabase.table("video_vault")
            .select("id, name, file_name, file_size, public_url, storage_path, mime_type, created_at")
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
            .order("created_at", desc=True)
            .execute()
        )
        vault = vault_res.data or []

        # ── 4. Subject Units (File Mode — Unit/Lesson containers) ────
        units_res = (
            supabase.table("subject_units")
            .select("id, subject_id, unit_type, unit_number, name, sort_order, created_at")
            .eq("user_id", user_id)
            .order("sort_order")
            .execute()
        )
        subject_units = units_res.data or []
        unit_ids = [u["id"] for u in subject_units]

        # ── 5. Unit Lessons (library lesson IDs per unit) ─────────────
        unit_lesson_rows = []
        if unit_ids:
            ul_res = (
                supabase.table("unit_lessons")
                .select("unit_id, lesson_id, sort_order")
                .eq("user_id", user_id)
                .in_("unit_id", unit_ids)
                .order("sort_order")
                .execute()
            )
            unit_lesson_rows = ul_res.data or []

        # Build lessons_by_unit map  { unit_id → [lesson_id, …] }
        lessons_by_unit: dict = {}
        for row in unit_lesson_rows:
            lessons_by_unit.setdefault(row["unit_id"], []).append(row["lesson_id"])

        # Attach lessons list to each unit row
        for u in subject_units:
            u["lesson_ids"] = lessons_by_unit.get(u["id"], [])

        # Build units_by_subject map  { subject_id → [unit, …] }
        units_by_subject: dict = {}
        for u in subject_units:
            units_by_subject.setdefault(u["subject_id"], []).append(u)

        # Attach units to the subjects tree (new field: s["units"])
        for s in subjects_tree:
            s["units"] = units_by_subject.get(s["id"], [])

        print(f"[SYNC] /all → {len(items)} items, {len(subjects_tree)} subjects, {len(subject_units)} units, {len(vault)} vault — user={current_user['email']!r}")
        return {
            "items":    items,
            "subjects": subjects_tree,
            "vault":    vault,
        }

    except HTTPException:
        raise  # re-raise 401/403 from get_current_user unchanged
    except Exception as exc:
        print(f"[SYNC] /all ERROR for user={current_user.get('email')!r}: {exc}")
        raise HTTPException(
            status_code=502,
            detail="Failed to load user data. Please try again.",
        )


# ════════════════════════════════════════════════════════════════
# GENERATED ITEMS
# ════════════════════════════════════════════════════════════════

class GeneratedItemCreate(BaseModel):
    item_type:       str   = Field(..., description="ai_creator | book_mode | question_anim | topic_content")
    title:           str   = Field(default="Untitled", max_length=500)
    prompt:          str   = Field(default="")
    explanation:     str   = Field(default="")
    html_code:       str   = Field(default="")
    playlist:        str   = Field(default="General")
    is_saved:        bool  = Field(default=True)
    source_pdf_name: Optional[str] = None
    source_topic:    Optional[str] = None
    source_subtopic: Optional[str] = None
    created_at:      Optional[str] = None
    # Legacy field alias — maps to html_code for backward compat
    animation_code:  Optional[str] = None

    def resolved_html(self) -> str:
        return self.html_code or self.animation_code or ""


class GeneratedItemUpdate(BaseModel):
    title:       Optional[str]  = None
    playlist:    Optional[str]  = None
    is_saved:    Optional[bool] = None
    html_code:   Optional[str]  = None
    explanation: Optional[str]  = None


@router.post("/items", status_code=200)
def save_item(
    body:         GeneratedItemCreate,
    current_user: dict = Depends(get_current_user),
):
    """Save a generated item. Returns the new row's UUID."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]

    valid_types = ("ai_creator", "book_mode", "question_anim", "topic_content")
    if body.item_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"item_type must be one of {valid_types}")

    # Ensure the user row exists in `users` table before writing to any
    # child table that has a user_id FK constraint (fixes 23503 error).
    _ensure_user_row(current_user)

    row = {
        "user_id":         user_id,
        "item_type":       body.item_type,
        "title":           (body.title or "Untitled").strip(),
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
    print(f"[SYNC] ✅ Item saved: type={body.item_type} title={body.title!r} user={current_user['email']!r}")
    return {"success": True, "id": item_id, "message": "Saved."}


@router.get("/items", status_code=200)
def get_items(
    item_type: Optional[str]  = None,
    playlist:  Optional[str]  = None,
    is_saved:  Optional[bool] = None,
    current_user: dict = Depends(get_current_user),
):
    """Fetch saved items, optionally filtered by type / playlist / is_saved."""
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

    res  = q.execute()
    rows = res.data or []
    print(f"[SYNC] ↓ {len(rows)} items (type={item_type}) user={current_user['email']!r}")
    return {"count": len(rows), "items": rows}


@router.put("/items/{item_id}", status_code=200)
def update_item(
    item_id:      str,
    body:         GeneratedItemUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update title, playlist, is_saved, html_code, or explanation."""
    supabase = _sb(current_user)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        return {"success": True, "message": "Nothing to update."}

    res = (
        supabase.table("generated_items")
        .update(patch)
        .eq("id", item_id)
        .eq("user_id", current_user["id"])
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Item not found.")
    return {"success": True, "id": item_id}


@router.delete("/items/{item_id}", status_code=200)
def delete_item(
    item_id:      str,
    current_user: dict = Depends(get_current_user),
):
    """Soft-delete a generated item (sets deleted_at)."""
    supabase = _sb(current_user)
    res = (
        supabase.table("generated_items")
        .update({"deleted_at": _now()})
        .eq("id", item_id)
        .eq("user_id", current_user["id"])
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Item not found.")
    print(f"[SYNC] 🗑 Item deleted: {item_id} user={current_user['email']!r}")
    return {"success": True, "id": item_id}


# ════════════════════════════════════════════════════════════════
# ENGINEERING SUBJECTS
# ════════════════════════════════════════════════════════════════

class SubjectCreate(BaseModel):
    name:        str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="")
    sort_order:  int = Field(default=0)


class SubjectUpdate(BaseModel):
    name:              Optional[str]  = None
    description:       Optional[str]  = None
    sort_order:        Optional[int]  = None
    syllabus_pdf_name: Optional[str]  = None
    syllabus_text:     Optional[str]  = None
    syllabus_units:    Optional[dict] = None


@router.get("/subjects", status_code=200)
def get_subjects(current_user: dict = Depends(get_current_user)):
    """Return all subjects with their COs and topics nested inside."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]

    subjects_res = (
        supabase.table("engineering_subjects")
        .select("*")
        .eq("user_id", user_id)
        .order("sort_order")
        .execute()
    )
    subjects    = subjects_res.data or []
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
        cos    = cos_res.data or []
        co_ids = [c["id"] for c in cos]
        if co_ids:
            topics_res = (
                supabase.table("course_topics")
                .select("id, co_id, subject_id, name, description, prompt, sort_order, generated_item_id, html_code_cache, topic_type, ppt_storage_path, ppt_public_url, ppt_file_name, created_at")
                .in_("co_id", co_ids)
                .order("sort_order")
                .execute()
            )
            topics = topics_res.data or []

    topics_by_co = {}
    for t in topics:
        html_cache   = t.get("html_code_cache")
        db_type      = t.get("topic_type") or "animation"
        if db_type == "ppt_upload":
            display_type = "ppt_upload"
        elif db_type == "html_upload" or (html_cache and html_cache.strip().startswith("<!DOCTYPE")):
            display_type = "html_upload"
        else:
            display_type = "animation"
        topics_by_co.setdefault(t["co_id"], []).append({
            "id":                t["id"],
            "name":              t["name"],
            "description":       t.get("description", ""),
            "prompt":            t.get("prompt", ""),
            "animCode":          html_cache,
            "type":              display_type,
            "pptUrl":            t.get("ppt_public_url"),
            "pptStoragePath":    t.get("ppt_storage_path"),
            "fileName":          t.get("ppt_file_name"),
            "created_at":        t["created_at"],
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

    result = [
        {
            "id":          s["id"],
            "name":        s["name"],
            "description": s.get("description", ""),
            "share_token": s.get("share_token"),
            "cos":         cos_by_subject.get(s["id"], []),
            "syllabus": {
                "pdf_name": s.get("syllabus_pdf_name"),
                "units":    s.get("syllabus_units"),
            } if s.get("syllabus_pdf_name") else None,
        }
        for s in subjects
    ]

    print(f"[SYNC] ↓ {len(result)} subjects user={current_user['email']!r}")
    return {"subjects": result}


@router.post("/subjects", status_code=201)
def create_subject(
    body:         SubjectCreate,
    current_user: dict = Depends(get_current_user),
):
    # ✅ Use service-role client to insert user row (bypasses RLS on users table)
    _ensure_user_row(current_user)
    supabase = _sb(current_user)
    # Generate a URL-safe share token (10 chars, ~60 bits of entropy).
    # Stored in engineering_subjects.share_token (UNIQUE column).
    share_token = secrets.token_urlsafe(8)  # e.g. "aB3xQ7mNpL"
    row = {
        "user_id":     current_user["id"],
        "name":        body.name.strip(),
        "description": body.description or "",
        "sort_order":  body.sort_order,
        "share_token": share_token,
    }
    res = supabase.table("engineering_subjects").insert(row).execute()
    subject = res.data[0] if res.data else {}
    # Always echo share_token even if the DB row didn't return it
    if "share_token" not in subject:
        subject["share_token"] = share_token
    print(f"[SYNC] ✅ Subject created: {body.name!r} share_token={share_token!r} user={current_user['email']!r}")
    return {"success": True, "subject": subject}


@router.put("/subjects/{subject_id}", status_code=200)
def update_subject(
    subject_id:   str,
    body:         SubjectUpdate,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        return {"success": True}
    res = (
        supabase.table("engineering_subjects")
        .update(patch)
        .eq("id", subject_id)
        .eq("user_id", current_user["id"])
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Subject not found.")
    return {"success": True, "subject": res.data[0]}


@router.delete("/subjects/{subject_id}", status_code=200)
def delete_subject(
    subject_id:   str,
    current_user: dict = Depends(get_current_user),
):
    """Delete subject. COs and topics cascade-delete automatically via FK."""
    supabase = _sb(current_user)
    supabase.table("engineering_subjects").delete().eq("id", subject_id).eq("user_id", current_user["id"]).execute()
    print(f"[SYNC] 🗑 Subject deleted: {subject_id} user={current_user['email']!r}")
    return {"success": True}


# ════════════════════════════════════════════════════════════════
# COURSE OUTCOMES
# ════════════════════════════════════════════════════════════════

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
    body:         COCreate,
    current_user: dict = Depends(get_current_user),
):
    # ✅ Use service-role client to insert user row (bypasses RLS on users table)
    _ensure_user_row(current_user)
    supabase = _sb(current_user)
    row = {
        "subject_id":  body.subject_id,
        "user_id":     current_user["id"],
        "co_num":      body.co_num.strip(),
        "description": body.description or "",
        "sort_order":  body.sort_order,
    }
    res = supabase.table("course_outcomes").insert(row).execute()
    co = res.data[0] if res.data else {}
    print(f"[SYNC] ✅ CO created: {body.co_num!r} subject={body.subject_id!r}")
    return {"success": True, "co": co}


@router.put("/cos/{co_id}", status_code=200)
def update_co(
    co_id:        str,
    body:         COUpdate,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        return {"success": True}
    res = (
        supabase.table("course_outcomes")
        .update(patch)
        .eq("id", co_id)
        .eq("user_id", current_user["id"])
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="CO not found.")
    return {"success": True}


@router.delete("/cos/{co_id}", status_code=200)
def delete_co(
    co_id:        str,
    current_user: dict = Depends(get_current_user),
):
    """Delete CO. Topics under it cascade-delete automatically."""
    supabase = _sb(current_user)
    supabase.table("course_outcomes").delete().eq("id", co_id).eq("user_id", current_user["id"]).execute()
    print(f"[SYNC] 🗑 CO deleted: {co_id} user={current_user['email']!r}")
    return {"success": True}


# ════════════════════════════════════════════════════════════════
# SUBJECT UNITS  —  Unit/Lesson containers inside a Subject Folder
# ════════════════════════════════════════════════════════════════

class UnitCreate(BaseModel):
    subject_id:  str
    unit_type:   str = Field(default="unit", pattern="^(unit|lesson)$")
    unit_number: int = Field(default=1, ge=1)
    name:        str = Field(..., min_length=1, max_length=100)
    sort_order:  int = Field(default=0)


class UnitUpdate(BaseModel):
    name:        Optional[str] = None
    unit_type:   Optional[str] = None
    unit_number: Optional[int] = None
    sort_order:  Optional[int] = None


@router.get("/units", status_code=200)
def get_units(
    subject_id:   str,
    current_user: dict = Depends(get_current_user),
):
    """List all units for a given subject_id, ordered by sort_order."""
    supabase = _sb(current_user)
    res = (
        supabase.table("subject_units")
        .select("id, subject_id, unit_type, unit_number, name, sort_order, created_at")
        .eq("subject_id", subject_id)
        .eq("user_id", current_user["id"])
        .order("sort_order")
        .execute()
    )
    units = res.data or []
    return {"units": units}


@router.post("/units", status_code=201)
def create_unit(
    body:         UnitCreate,
    current_user: dict = Depends(get_current_user),
):
    """Create a Unit or Lesson container inside a Subject Folder."""
    _ensure_user_row(current_user)
    supabase = _sb(current_user)
    row = {
        "subject_id":  body.subject_id,
        "user_id":     current_user["id"],
        "unit_type":   body.unit_type,
        "unit_number": body.unit_number,
        "name":        body.name.strip(),
        "sort_order":  body.sort_order,
    }
    res  = supabase.table("subject_units").insert(row).execute()
    unit = res.data[0] if res.data else {}
    print(f"[SYNC] ✅ Unit created: {body.name!r} subject={body.subject_id!r} user={current_user['email']!r}")
    return {"success": True, "unit": unit}


@router.put("/units/{unit_id}", status_code=200)
def update_unit(
    unit_id:      str,
    body:         UnitUpdate,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        return {"success": True}
    res = (
        supabase.table("subject_units")
        .update(patch)
        .eq("id", unit_id)
        .eq("user_id", current_user["id"])
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Unit not found.")
    return {"success": True, "unit": res.data[0]}


@router.delete("/units/{unit_id}", status_code=200)
def delete_unit(
    unit_id:      str,
    current_user: dict = Depends(get_current_user),
):
    """Delete unit. unit_lessons rows cascade-delete automatically."""
    supabase = _sb(current_user)
    supabase.table("subject_units").delete().eq("id", unit_id).eq("user_id", current_user["id"]).execute()
    print(f"[SYNC] 🗑 Unit deleted: {unit_id} user={current_user['email']!r}")
    return {"success": True}


# ════════════════════════════════════════════════════════════════
# UNIT LESSONS  —  library lesson IDs linked to a Unit
# ════════════════════════════════════════════════════════════════

class UnitLessonsSave(BaseModel):
    unit_id:    str
    lesson_ids: List[str]   # full replacement: old links not in list are removed


@router.get("/unit-lessons/{unit_id}", status_code=200)
def get_unit_lessons(
    unit_id:      str,
    current_user: dict = Depends(get_current_user),
):
    """Return all lesson IDs saved to a given unit."""
    supabase = _sb(current_user)
    res = (
        supabase.table("unit_lessons")
        .select("id, lesson_id, sort_order")
        .eq("unit_id", unit_id)
        .eq("user_id", current_user["id"])
        .order("sort_order")
        .execute()
    )
    rows = res.data or []
    return {"unit_id": unit_id, "lesson_ids": [r["lesson_id"] for r in rows], "rows": rows}


@router.post("/unit-lessons", status_code=200)
def save_unit_lessons(
    body:         UnitLessonsSave,
    current_user: dict = Depends(get_current_user),
):
    """
    Replace all lesson links for a unit with the supplied lesson_ids list.
    Deletes rows not in the list, inserts missing ones (upsert-by-replace).
    """
    _ensure_user_row(current_user)
    supabase  = _sb(current_user)
    user_id   = current_user["id"]
    unit_id   = body.unit_id
    lesson_ids = body.lesson_ids

    # 1. Fetch current links
    current_res = (
        supabase.table("unit_lessons")
        .select("id, lesson_id")
        .eq("unit_id", unit_id)
        .eq("user_id", user_id)
        .execute()
    )
    current_rows = current_res.data or []
    current_ids  = {r["lesson_id"] for r in current_rows}
    new_ids      = set(lesson_ids)

    # 2. Delete removed lessons
    to_delete = current_ids - new_ids
    if to_delete:
        supabase.table("unit_lessons").delete()\
            .eq("unit_id", unit_id)\
            .eq("user_id", user_id)\
            .in_("lesson_id", list(to_delete))\
            .execute()

    # 3. Insert new lessons
    to_insert = new_ids - current_ids
    if to_insert:
        rows = [
            {"unit_id": unit_id, "lesson_id": lid, "user_id": user_id, "sort_order": lesson_ids.index(lid)}
            for lid in to_insert
        ]
        supabase.table("unit_lessons").insert(rows).execute()

    print(f"[SYNC] ✅ Unit lessons saved: unit={unit_id} count={len(new_ids)} user={current_user['email']!r}")
    return {"success": True, "unit_id": unit_id, "count": len(new_ids)}


# ════════════════════════════════════════════════════════════════
# COURSE TOPICS
# ════════════════════════════════════════════════════════════════

class TopicCreate(BaseModel):
    co_id:             str
    subject_id:        str
    name:              str   = Field(..., min_length=1, max_length=300)
    description:       str   = Field(default="")
    prompt:            str   = Field(default="")
    sort_order:        int   = Field(default=0)
    generated_item_id: Optional[str] = None
    html_code:         Optional[str] = None  # fills html_code_cache
    topic_type:        str            = Field(default="animation")
    ppt_storage_path:  Optional[str] = None
    ppt_public_url:    Optional[str] = None
    ppt_file_name:     Optional[str] = None


class TopicUpdate(BaseModel):
    name:              Optional[str] = None
    description:       Optional[str] = None
    prompt:            Optional[str] = None
    sort_order:        Optional[int] = None
    generated_item_id: Optional[str] = None
    html_code_cache:   Optional[str] = None
    topic_type:        Optional[str] = None
    ppt_storage_path:  Optional[str] = None
    ppt_public_url:    Optional[str] = None
    ppt_file_name:     Optional[str] = None


@router.post("/topics", status_code=201)
def create_topic(
    body:         TopicCreate,
    current_user: dict = Depends(get_current_user),
):
    # ✅ Ensure user row exists (prevents FK 23503 on course_topics.user_id_fkey)
    _ensure_user_row(current_user)
    supabase = _sb(current_user)

    row = {
        "co_id":             body.co_id,
        "subject_id":        body.subject_id,
        "user_id":           current_user["id"],
        "name":              body.name.strip(),
        "description":       body.description or "",
        "prompt":            body.prompt or "",
        "sort_order":        body.sort_order,
        "generated_item_id": body.generated_item_id,
        "html_code_cache":   body.html_code,
        "topic_type":        body.topic_type or "animation",
        "ppt_storage_path":  body.ppt_storage_path,
        "ppt_public_url":    body.ppt_public_url,
        "ppt_file_name":     body.ppt_file_name,
    }
    res   = supabase.table("course_topics").insert(row).execute()
    topic = res.data[0] if res.data else {}
    print(f"[SYNC] ✅ Topic created: {body.name!r} type={body.topic_type!r} co={body.co_id!r}")
    return {"success": True, "topic": topic}


@router.put("/topics/{topic_id}", status_code=200)
def update_topic(
    topic_id:     str,
    body:         TopicUpdate,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if not patch:
        return {"success": True}
    res = (
        supabase.table("course_topics")
        .update(patch)
        .eq("id", topic_id)
        .eq("user_id", current_user["id"])
        .execute()
    )
    if not res.data:
        raise HTTPException(status_code=404, detail="Topic not found.")
    return {"success": True}


@router.delete("/topics/{topic_id}", status_code=200)
def delete_topic(
    topic_id:     str,
    current_user: dict = Depends(get_current_user),
):
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    # Fetch ppt_storage_path before deleting row so we can clean up Storage
    row_res = (
        supabase.table("course_topics")
        .select("ppt_storage_path, topic_type")
        .eq("id", topic_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if row_res and row_res.data:
        ppt_path = row_res.data.get("ppt_storage_path")
        if ppt_path and row_res.data.get("topic_type") == "ppt_upload":
            try:
                supabase.storage.from_("ppt-files").remove([ppt_path])
                print(f"[PPT] 🗑 Storage file deleted: {ppt_path}")
            except Exception as e:
                print(f"[PPT] ⚠ Storage delete failed for {topic_id}: {e}")
    supabase.table("course_topics").delete().eq("id", topic_id).eq("user_id", user_id).execute()
    print(f"[SYNC] 🗑 Topic deleted: {topic_id} user={current_user['email']!r}")
    return {"success": True}


# ════════════════════════════════════════════════════════════════
# VIDEO VAULT  —  normalized (one row per video)
# ════════════════════════════════════════════════════════════════

class VaultEntryCreate(BaseModel):
    name:         str
    file_name:    str
    file_size:    int  = 0
    storage_path: str  = ""
    public_url:   str  = ""
    mime_type:    str  = "video/mp4"


@router.get("/vault/entries", status_code=200)
def get_vault_entries(current_user: dict = Depends(get_current_user)):
    supabase = _sb(current_user)
    res = (
        supabase.table("video_vault")
        .select("id, name, file_name, file_size, public_url, storage_path, mime_type, created_at")
        .eq("user_id", current_user["id"])
        .is_("deleted_at", "null")
        .order("created_at", desc=True)
        .execute()
    )
    entries = res.data or []
    return {"count": len(entries), "entries": entries}


@router.post("/vault/entries", status_code=201)
def add_vault_entry(
    body:         VaultEntryCreate,
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
    res   = supabase.table("video_vault").insert(row).execute()
    entry = res.data[0] if res.data else {}
    print(f"[SYNC] ✅ Vault entry added: {body.name!r} user={current_user['email']!r}")
    return {"success": True, "entry": entry}


@router.delete("/vault/entries/{entry_id}", status_code=200)
def delete_vault_entry(
    entry_id:     str,
    current_user: dict = Depends(get_current_user),
):
    """Soft-delete the DB row and hard-delete the Storage file."""
    supabase = _sb(current_user)
    # Fetch storage_path first so we can delete the file
    row = (
        supabase.table("video_vault")
        .select("storage_path")
        .eq("id", entry_id)
        .eq("user_id", current_user["id"])
        .maybe_single()
        .execute()
    )
    if row.data and row.data.get("storage_path"):
        try:
            supabase.storage.from_("vault").remove([row.data["storage_path"]])
        except Exception as e:
            print(f"[VAULT] ⚠ Storage delete failed for {entry_id}: {e}")
    supabase.table("video_vault").update({"deleted_at": _now()}).eq("id", entry_id).eq("user_id", current_user["id"]).execute()
    print(f"[SYNC] 🗑 Vault entry deleted: {entry_id} user={current_user['email']!r}")
    return {"success": True}


# ════════════════════════════════════════════════════════════════
# FILE UPLOAD / DELETE  —  Supabase Storage
# ════════════════════════════════════════════════════════════════

@router.post("/files/upload", status_code=200)
async def upload_file(
    file:         UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Upload a file (video/HTML) to Supabase Storage bucket 'vault'."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    ts       = int(datetime.now(timezone.utc).timestamp())
    safe_fn  = (file.filename or "upload").replace(" ", "_")
    path     = f"{user_id}/{ts}_{safe_fn}"
    data     = await file.read()

    try:
        supabase.storage.from_("vault").upload(
            path=path,
            file=data,
            file_options={"content-type": file.content_type or "application/octet-stream"},
        )
        url = supabase.storage.from_("vault").get_public_url(path)
        print(f"[STORAGE] ✅ Uploaded: {path}")
        return {"success": True, "url": url, "path": path, "filename": safe_fn}
    except Exception as e:
        print(f"[STORAGE] ❌ Upload failed: {e}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")


# ════════════════════════════════════════════════════════════════
# PPT UPLOAD  —  POST /sync/ppt/upload
# Uploads a .pptx file to Supabase Storage (ppt-files bucket)
# and creates a course_topics row of type 'ppt_upload'.
# ════════════════════════════════════════════════════════════════

@router.post("/ppt/upload", status_code=201)
async def upload_ppt(
    file:         UploadFile  = File(...),
    co_id:        str         = Form(...),
    subject_id:   str         = Form(...),
    name:         str         = Form(...),
    sort_order:   int         = Form(0),
    current_user: dict        = Depends(get_current_user),
):
    """
    Multipart endpoint: upload a .pptx binary to Supabase Storage
    (bucket: ppt-files, path: {user_id}/{timestamp}_{filename})
    and create a corresponding course_topics row with:
      topic_type       = 'ppt_upload'
      ppt_storage_path = storage path
      ppt_public_url   = public download URL
      ppt_file_name    = original filename
    Returns the created topic row so the frontend can track the cloud ID.
    """
    if not co_id or not subject_id or not name:
        raise HTTPException(
            status_code=422,
            detail="co_id, subject_id, and name are required form fields."
        )

    fn = (file.filename or "upload.pptx").replace(" ", "_")
    if not fn.lower().endswith(".pptx"):
        raise HTTPException(status_code=400, detail="Only .pptx files are accepted.")

    data = await file.read()
    if len(data) > 52_428_800:   # 50 MB
        raise HTTPException(status_code=413, detail="File exceeds 50 MB limit.")

    # ── Ensure user row exists (FK guard) ──────────────────────
    _ensure_user_row(current_user)
    supabase = _sb(current_user)
    user_id  = current_user["id"]

    # ── Upload to Supabase Storage: ppt-files/{user_id}/{ts}_{fn} ──
    ts      = int(datetime.now(timezone.utc).timestamp())
    path    = f"{user_id}/{ts}_{fn}"
    ct      = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    try:
        supabase.storage.from_("ppt-files").upload(
            path=path,
            file=data,
            file_options={"content-type": ct},
        )
        public_url = supabase.storage.from_("ppt-files").get_public_url(path)
        print(f"[PPT] ✅ Uploaded: {path} ({len(data)} bytes)")
    except Exception as e:
        print(f"[PPT] ❌ Storage upload failed: {e}")
        raise HTTPException(status_code=500, detail=f"PPT storage upload failed: {e}")

    # ── Insert course_topics row ────────────────────────────────
    row = {
        "co_id":            co_id,
        "subject_id":       subject_id,
        "user_id":          user_id,
        "name":             name.strip(),
        "description":      "Uploaded PPT file",
        "prompt":           "[PPT Upload]",
        "sort_order":       sort_order,
        "topic_type":       "ppt_upload",
        "ppt_storage_path": path,
        "ppt_public_url":   public_url,
        "ppt_file_name":    fn,
    }
    try:
        res   = supabase.table("course_topics").insert(row).execute()
        topic = res.data[0] if res.data else row
    except Exception as e:
        # Storage upload succeeded but DB insert failed — try to clean up
        print(f"[PPT] ❌ DB insert failed (cleaning up storage): {e}")
        try:
            supabase.storage.from_("ppt-files").remove([path])
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"PPT DB save failed: {e}")

    print(f"[PPT] ✅ Topic created: {name!r} co={co_id!r} user={current_user['email']!r}")
    # Return shape the frontend expects
    return {
        "success":    True,
        "topic": {
            "id":             topic.get("id"),
            "name":           topic.get("name", name),
            "description":    "Uploaded PPT file",
            "prompt":         "[PPT Upload]",
            "type":           "ppt_upload",
            "topic_type":     "ppt_upload",
            "pptUrl":         public_url,
            "pptStoragePath": path,
            "fileName":       fn,
            "created_at":     topic.get("created_at", _now()),
        },
    }


@router.delete("/files/delete", status_code=200)
def delete_file(
    path:         str,
    current_user: dict = Depends(get_current_user),
):
    """Delete a file from Supabase Storage. Path must start with user_id/."""
    supabase = _sb(current_user)
    if not path.startswith(f"{current_user['id']}/"):
        raise HTTPException(status_code=403, detail="Not authorized to delete this file.")
    try:
        supabase.storage.from_("vault").remove([path])
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")


# ════════════════════════════════════════════════════════════════
# PUBLIC SHARE ENDPOINT  —  GET /sync/share/{token}
# No JWT required — students access this without logging in.
# ════════════════════════════════════════════════════════════════

@router.get("/share/{token}", status_code=200)
def get_shared_subject(token: str):
    """
    Public (no-auth) endpoint that returns a subject's full CO+topic tree
    identified by its share_token.  No user PII is exposed.
    """
    # Use the service-role client so RLS doesn't block the read.
    svc = get_supabase()  # no token → service-role singleton

    subj_res = (
        svc.table("engineering_subjects")
        .select("id, name, description, share_token")
        .eq("share_token", token)
        .maybe_single()
        .execute()
    )
    subj = subj_res.data if subj_res else None
    if not subj:
        raise HTTPException(status_code=404, detail="Shared subject not found.")

    subject_id = subj["id"]

    # ── COs ──────────────────────────────────────────────────────
    cos_res = (
        svc.table("course_outcomes")
        .select("id, co_num, description, sort_order")
        .eq("subject_id", subject_id)
        .order("sort_order")
        .execute()
    )
    cos = cos_res.data or []
    co_ids = [c["id"] for c in cos]

    # ── Topics (with html_code_cache so students can view animations) ─
    topics = []
    if co_ids:
        topics_res = (
            svc.table("course_topics")
            .select("id, co_id, name, description, sort_order, html_code_cache")
            .in_("co_id", co_ids)
            .order("sort_order")
            .execute()
        )
        topics = topics_res.data or []

    topics_by_co: dict = {}
    for t in topics:
        topics_by_co.setdefault(t["co_id"], []).append({
            "id":          t["id"],
            "name":        t["name"],
            "description": t.get("description", ""),
            "has_content": bool(t.get("html_code_cache")),
            "html_code":   t.get("html_code_cache") or "",
        })

    cos_out = [
        {
            "id":          co["id"],
            "coNum":       co["co_num"],
            "name":        co.get("description") or co["co_num"],
            "topics":      topics_by_co.get(co["id"], []),
        }
        for co in cos
    ]

    print(f"[SHARE] Public access: subject={subj['name']!r} token={token!r}")
    return {
        "subject": {
            "id":          subject_id,
            "name":        subj["name"],
            "description": subj.get("description", ""),
            "share_token": token,
        },
        "cos": cos_out,
    }


# ════════════════════════════════════════════════════════════════
# LEGACY ENDPOINTS  (backward compat — remove in v7.0)
# ════════════════════════════════════════════════════════════════

_ENG_COURSES_SENTINEL = "__eng_courses__"
_VAULT_SENTINEL       = "__vault__"


def _ensure_user_row(user: dict) -> None:
    """
    Upsert a minimal row into the `users` table so that FK constraints on
    engineering_subjects.user_id_fkey (and other tables) are satisfied.

    CRITICAL: This MUST use the SERVICE-ROLE client (no JWT token) because
    the `users` table has RLS enabled — a user-authenticated client cannot
    INSERT into it and will silently fail, causing FK 23503 on the next insert.

    Safe to call on every write request — ON CONFLICT DO NOTHING is idempotent.
    """
    try:
        # Service-role client bypasses RLS — this is the only client that can
        # write to the public.users table from the backend.
        svc = get_supabase()   # no token → service-role singleton
        svc.table("users").upsert(
            {
                "id":    user["id"],
                "email": user.get("email", ""),
            },
            on_conflict="id",
            ignore_duplicates=True,
        ).execute()
        print(f"[SYNC] ✅ User row ensured: {user.get('email', user['id'])}")
    except Exception as e:
        # Log clearly — this failure WILL cause FK errors on the next insert.
        print(f"[SYNC] ❌ _ensure_user_row FAILED (will cause FK 23503!): {e}")


def _legacy_upsert_contents(supabase, user_id: str, anim_id: str, row: dict):
    # Fix: .maybe_single() returns None (not an object with .data) when no row
    # is found in some supabase-py versions.  Guard both cases.
    try:
        result = (
            supabase.table("contents")
            .select("id")
            .eq("user_id", user_id)
            .contains("body", {"anim_id": anim_id})
            .maybe_single()
            .execute()
        )
        existing_data = result.data if result is not None else None
    except Exception:
        existing_data = None

    if existing_data:
        row.pop("created_at", None)
        supabase.table("contents").update(row).eq("id", existing_data["id"]).execute()
        return "updated"
    else:
        supabase.table("contents").insert(row).execute()
        return "inserted"


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
def legacy_sync_animation(
    payload:      _LegacyAnimPayload,
    current_user: dict = Depends(get_current_user),
):
    """LEGACY — use POST /sync/items instead."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    anim_id  = payload.id.strip()
    title    = (payload.filename or payload.title or "Untitled").strip()
    row = {
        "user_id":  user_id,
        "title":    title,
        "prompt":   payload.prompt or "",
        "playlist": payload.playlist or "General",
        "body": {
            "anim_id":        anim_id,
            "filename":       title,
            "explanation":    payload.explanation or "",
            "animation_code": payload.animation_code or "",
        },
        "created_at": _parse_iso(payload.created_at),
    }
    _legacy_upsert_contents(supabase, user_id, anim_id, row)
    return {"success": True, "anim_id": anim_id, "message": "Saved (legacy)."}


@router.get("/animations", status_code=200)
def legacy_get_animations(current_user: dict = Depends(get_current_user)):
    """LEGACY — use GET /sync/items instead."""
    try:
        supabase = _sb(current_user)
        user_id  = current_user["id"]
        res = (
            supabase.table("contents")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        rows = [
            r for r in (res.data or [])
            if r.get("body", {}).get("anim_id") not in (_ENG_COURSES_SENTINEL, _VAULT_SENTINEL)
        ]
        animations = [
            {
                "id":             r["body"].get("anim_id", r["id"]),
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
        return {"user_id": user_id, "count": len(animations), "animations": animations}
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[SYNC] /animations ERROR for user={current_user.get('email')!r}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to load animations. Please try again.")


@router.delete("/animations/{anim_id}", status_code=200)
def legacy_delete_animation(
    anim_id:      str,
    current_user: dict = Depends(get_current_user),
):
    """LEGACY — use DELETE /sync/items/{id} instead."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    existing = (
        supabase.table("contents")
        .select("id")
        .eq("user_id", user_id)
        .contains("body", {"anim_id": anim_id})
        .limit(1)
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail=f"Item '{anim_id}' not found.")
    supabase.table("contents").delete().eq("id", existing.data[0]["id"]).execute()
    return {"success": True, "anim_id": anim_id, "message": "Deleted (legacy)."}


class _CoursesPayload(BaseModel):
    courses: list


@router.put("/courses", status_code=200)
def legacy_save_courses(
    body:         _CoursesPayload,
    current_user: dict = Depends(get_current_user),
):
    """LEGACY — use POST /sync/subjects (and /cos, /topics) instead."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    row = {
        "user_id":    user_id,
        "title":      "__Engineering Courses__",
        "prompt":     "",
        "playlist":   "__system__",
        "body":       {"anim_id": _ENG_COURSES_SENTINEL, "courses": body.courses},
        "updated_at": _now(),
    }
    all_ex = (
        supabase.table("contents")
        .select("id, created_at")
        .eq("user_id", user_id)
        .contains("body", {"anim_id": _ENG_COURSES_SENTINEL})
        .order("created_at", desc=True)
        .execute()
    )
    rows = all_ex.data or []
    # Dedup: remove stale duplicate rows from old INSERT-only bug
    if len(rows) > 1:
        for r in rows[1:]:
            supabase.table("contents").delete().eq("id", r["id"]).execute()
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
    try:
        supabase = _sb(current_user)
        user_id  = current_user["id"]
        res = (
            supabase.table("contents")
            .select("body")
            .eq("user_id", user_id)
            .contains("body", {"anim_id": _ENG_COURSES_SENTINEL})
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if res.data and res.data[0].get("body"):
            return {"courses": res.data[0]["body"].get("courses") or []}
        return {"courses": []}
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[SYNC] /courses ERROR for user={current_user.get('email')!r}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to load courses. Please try again.")


class _VaultBlobPayload(BaseModel):
    entries: list


@router.put("/vault", status_code=200)
def legacy_save_vault(
    body:         _VaultBlobPayload,
    current_user: dict = Depends(get_current_user),
):
    """LEGACY — use POST /sync/vault/entries instead."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    row = {
        "user_id":    user_id,
        "title":      "__Video Vault__",
        "prompt":     "",
        "playlist":   "__system__",
        "body":       {"anim_id": _VAULT_SENTINEL, "entries": body.entries},
        "updated_at": _now(),
    }
    all_ex = (
        supabase.table("contents")
        .select("id, created_at")
        .eq("user_id", user_id)
        .contains("body", {"anim_id": _VAULT_SENTINEL})
        .order("created_at", desc=True)
        .execute()
    )
    rows = all_ex.data or []
    if len(rows) > 1:
        for r in rows[1:]:
            supabase.table("contents").delete().eq("id", r["id"]).execute()
    if rows:
        supabase.table("contents").update(row).eq("id", rows[0]["id"]).execute()
    else:
        row["created_at"] = _now()
        supabase.table("contents").insert(row).execute()
    return {"success": True, "message": "Vault saved (legacy)."}


@router.get("/vault", status_code=200)
def legacy_get_vault(current_user: dict = Depends(get_current_user)):
    """LEGACY — use GET /sync/vault/entries instead."""
    try:
        supabase = _sb(current_user)
        user_id  = current_user["id"]
        res = (
            supabase.table("contents")
            .select("body")
            .eq("user_id", user_id)
            .contains("body", {"anim_id": _VAULT_SENTINEL})
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if res.data and res.data[0].get("body"):
            return {"entries": res.data[0]["body"].get("entries") or []}
        return {"entries": []}
    except HTTPException:
        raise
    except Exception as exc:
        print(f"[SYNC] /vault ERROR for user={current_user.get('email')!r}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to load vault. Please try again.")


class _SaveHtmlRequest(BaseModel):
    filename:    str   = Field(..., min_length=1, max_length=200)
    html:        str   = Field(..., min_length=1)
    prompt:      Optional[str] = ""
    explanation: Optional[str] = ""
    playlist:    Optional[str] = "General"
    client_id:   Optional[str] = None


@router.post("/animations/save-html", status_code=200)
def legacy_save_html(
    body:         _SaveHtmlRequest,
    current_user: dict = Depends(get_current_user),
):
    """LEGACY — proxied to /sync/items internally."""
    supabase = _sb(current_user)
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
        "created_at": _now(),
    }
    _legacy_upsert_contents(supabase, user_id, anim_id, row)
    return {"success": True, "anim_id": anim_id, "filename": filename, "message": f'"{filename}" saved.'}


class _BatchSyncRequest(BaseModel):
    animations: List[_LegacyAnimPayload]


@router.post("/animations/batch", status_code=200)
def legacy_batch_sync(
    body:         _BatchSyncRequest,
    current_user: dict = Depends(get_current_user),
):
    """LEGACY bulk upsert — kept for migration window."""
    supabase = _sb(current_user)
    user_id  = current_user["id"]
    synced = failed = 0
    for payload in body.animations:
        try:
            anim_id = (payload.id or "").strip()
            if not anim_id:
                failed += 1
                continue
            title = (payload.filename or payload.title or "Untitled").strip()
            row = {
                "user_id":  user_id,
                "title":    title,
                "prompt":   payload.prompt or "",
                "playlist": payload.playlist or "General",
                "body": {
                    "anim_id":        anim_id,
                    "filename":       title,
                    "explanation":    payload.explanation or "",
                    "animation_code": payload.animation_code or "",
                },
                "created_at": _parse_iso(payload.created_at),
            }
            _legacy_upsert_contents(supabase, user_id, anim_id, row)
            synced += 1
        except Exception as e:
            failed += 1
            print(f"[SYNC] ⚠ Batch item failed: {e}")
    return {"success": failed == 0, "synced": synced, "failed": failed, "message": f"Synced {synced}. {failed} failed."}


# ════════════════════════════════════════════════════════════════
# GLOBAL ANIMATIONS  (uploaded by main user, visible to ALL users)
# ════════════════════════════════════════════════════════════════

class GlobalAnimationPayload(BaseModel):
    id:             str
    title:          str           = ""
    prompt:         str           = ""
    explanation:    str           = ""
    animation_code: str           = ""
    playlist:       str           = "Global"
    created_at:     Optional[str] = None


@router.get("/global-animations", status_code=200)
def get_global_animations(current_user: dict = Depends(get_current_user)):
    """
    Returns all animations uploaded by the main user.
    Accessible to every authenticated user — no user_id filter.
    """
    supabase = _sb(current_user)
    try:
        resp = supabase.table("global_animations") \
            .select("id, title, prompt, explanation, animation_code, playlist, uploaded_by, created_at") \
            .order("created_at", desc=True) \
            .execute()
        rows = resp.data or []
        # Normalise to match the frontend animation object shape
        animations = [
            {
                "id":             r["id"],
                "title":          r["title"],
                "prompt":         r["prompt"],
                "explanation":    r["explanation"],
                "animation_code": r["animation_code"],
                "playlist":       r["playlist"],
                "created_at":     r["created_at"],
                "is_global":      True,
            }
            for r in rows
        ]
        return {"count": len(animations), "animations": animations}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch global animations: {e}")


@router.post("/global-animations", status_code=200)
def upload_global_animation(
    payload: GlobalAnimationPayload,
    current_user: dict = Depends(get_current_user),
):
    """
    Saves an animation to global_animations so it is visible to ALL users.
    Only the main user (MAIN_USER_EMAIL env var) is allowed to call this.
    """
    user_email = (current_user.get("email") or "").strip().lower()
    if user_email != MAIN_USER_EMAIL.strip().lower():
        raise HTTPException(
            status_code=403,
            detail=f"Only the main user ({MAIN_USER_EMAIL}) can upload global animations."
        )

    supabase = _sb(current_user)
    row = {
        "id":             payload.id.strip(),
        "title":          (payload.title or "Untitled").strip(),
        "prompt":         payload.prompt or "",
        "explanation":    payload.explanation or "",
        "animation_code": payload.animation_code or "",
        "playlist":       payload.playlist or "Global",
        "uploaded_by":    user_email,
        "created_at":     _parse_iso(payload.created_at),
    }

    try:
        supabase.table("global_animations").upsert(row, on_conflict="id").execute()
        print(f"[GLOBAL] ✅ Uploaded global animation '{row['title']}' by {user_email}")
        return {"success": True, "id": row["id"], "message": "Global animation saved."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save global animation: {e}")


@router.delete("/global-animations/{anim_id}", status_code=200)
def delete_global_animation(
    anim_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Deletes a global animation. Only the main user can do this.
    """
    user_email = (current_user.get("email") or "").strip().lower()
    if user_email != MAIN_USER_EMAIL.strip().lower():
        raise HTTPException(status_code=403, detail="Only the main user can delete global animations.")

    supabase = _sb(current_user)
    try:
        supabase.table("global_animations").delete().eq("id", anim_id).execute()
        return {"success": True, "message": f"Deleted global animation {anim_id}."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete global animation: {e}")



# ════════════════════════════════════════════════════════════════
# LESSONS — Admin Content Management + Kahoot-Style Library
# ════════════════════════════════════════════════════════════════

LESSON_BUCKETS = {
    "thumbnail":  "lesson-thumbnails",
    "animation":  "lesson-videos",
    "theory":     "lesson-html",
    "realworld":  "lesson-realworld",
}


def _is_admin(current_user: dict) -> bool:
    return (current_user.get("email") or "").strip().lower() == MAIN_USER_EMAIL.strip().lower()


def _require_admin(current_user: dict) -> None:
    if not _is_admin(current_user):
        raise HTTPException(status_code=403, detail="Only the admin can perform this action.")


def _get_service_client():
    supa_url = os.getenv("SUPABASE_URL", "")
    supa_key = os.getenv("SUPABASE_SERVICE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    if not supa_url or not supa_key:
        raise HTTPException(status_code=503, detail="Supabase not configured.")
    from supabase import create_client as _create_client
    return _create_client(supa_url, supa_key)


@router.post("/lessons/upload-file", status_code=200)
async def upload_lesson_file(
    file: UploadFile = File(...),
    file_type: str = Form(...),
    current_user: dict = Depends(get_current_user),
):
    """Admin-only: upload thumbnail/video/html to Supabase Storage."""
    _require_admin(current_user)
    if file_type not in LESSON_BUCKETS:
        raise HTTPException(status_code=400, detail=f"file_type must be one of: {list(LESSON_BUCKETS.keys())}")
    bucket = LESSON_BUCKETS[file_type]
    import uuid as _uuid
    safe_name   = (file.filename or "file").replace(" ", "_")
    unique_name = f"{_uuid.uuid4().hex}_{safe_name}"
    data = await file.read()
    service_sb = _get_service_client()
    try:
        service_sb.storage.from_(bucket).upload(
            unique_name, data,
            file_options={"content-type": file.content_type or "application/octet-stream"},
        )
        supa_url   = os.getenv("SUPABASE_URL", "")
        public_url = f"{supa_url}/storage/v1/object/public/{bucket}/{unique_name}"
        print(f"[LESSONS] Uploaded {file_type} -> {public_url}")
        return {"public_url": public_url, "storage_path": unique_name, "bucket": bucket}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Storage upload failed: {e}")


class LessonCreate(BaseModel):
    title:             str           = Field(..., min_length=1, max_length=200)
    subject:           Optional[str] = 'science'   # 'science' | 'social' | 'maths'
    class_name:        Optional[str] = None         # 'Class 9' | 'Class 10' | ...
    content_type:      Optional[str] = 'mixed'
    thumbnail_url:     Optional[str] = None
    theory_url:        Optional[str] = None
    animation_url:     Optional[str] = None
    realworld_images:  list          = Field(default_factory=list)


@router.post("/lessons", status_code=201)
def create_lesson(payload: LessonCreate, current_user: dict = Depends(get_current_user)):
    """Admin-only: insert a new lesson row into public.lessons."""
    _require_admin(current_user)
    service_sb = _get_service_client()
    row = {
        "title":            payload.title.strip(),
        "subject":          (payload.subject      or 'science').strip().lower(),
        "class_name":       (payload.class_name   or '').strip() or None,
        "content_type":     (payload.content_type or 'mixed').strip(),
        "thumbnail_url":    payload.thumbnail_url    or None,
        "theory_url":       payload.theory_url       or None,
        "animation_url":    payload.animation_url    or None,
        "realworld_images": payload.realworld_images,
        "created_at":       _now(),
        "updated_at":       _now(),
    }
    try:
        res     = service_sb.table("lessons").insert(row).execute()
        created = (res.data or [{}])[0]
        print(f"[LESSONS] Created lesson '{payload.title}' (subject={row['subject']}) -> id={created.get('id')}")
        return {"success": True, "lesson": created}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create lesson: {e}")


@router.get("/lessons", status_code=200)
def list_lessons(current_user: dict = Depends(get_current_user)):
    """All authenticated users: list all lessons ordered by created_at asc.

    Uses the service-role client to bypass RLS.
    The route is still protected — get_current_user() rejects unauthenticated
    requests before this function is ever called.

    NOTE: Previously used _sb(current_user) (user JWT passed to service-key client),
    but that makes auth.role() = 'service_role' from Supabase's perspective,
    which does NOT satisfy the RLS SELECT policy USING (auth.role() = 'authenticated').
    The service-role client bypasses RLS entirely and returns all rows correctly.
    """
    service_sb = _get_service_client()
    try:
        res = (
            service_sb.table("lessons")
            .select("*")
            .order("created_at", desc=False)
            .execute()
        )
        lessons = res.data or []
        print(f"[LESSONS] Listed {len(lessons)} lesson(s).")
        return {"lessons": lessons, "count": len(lessons)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list lessons: {e}")


@router.delete("/lessons/{lesson_id}", status_code=200)
def delete_lesson(lesson_id: str, current_user: dict = Depends(get_current_user)):
    """Admin-only: delete lesson row and its Supabase Storage files."""
    _require_admin(current_user)
    service_sb = _get_service_client()
    try:
        row_res = service_sb.table("lessons").select("*").eq("id", lesson_id).execute()
        rows = row_res.data or []
        if not rows:
            raise HTTPException(status_code=404, detail=f"Lesson {lesson_id} not found.")
        lesson = rows[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch lesson: {e}")

    def _del_storage(bucket: str, url):
        if not url:
            return
        try:
            filename = url.split(f"/{bucket}/")[-1]
            service_sb.storage.from_(bucket).remove([filename])
        except Exception as se:
            print(f"[LESSONS] Could not delete {bucket}/{filename}: {se}")

    _del_storage("lesson-thumbnails", lesson.get("thumbnail_url"))
    _del_storage("lesson-videos",     lesson.get("animation_url"))
    _del_storage("lesson-html",       lesson.get("theory_url"))
    # Delete all Real-World Application images from storage
    for img_url in (lesson.get("realworld_images") or []):
        _del_storage("lesson-realworld", img_url)

    try:
        service_sb.table("lessons").delete().eq("id", lesson_id).execute()
        print(f"[LESSONS] Deleted lesson {lesson_id}")
        return {"success": True, "message": f"Lesson {lesson_id} deleted."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete lesson: {e}")


# ════════════════════════════════════════════════════════════════
# MATHS TOPICS — Library Mode: Mathematics subject
# ════════════════════════════════════════════════════════════════

MATHS_TOPIC_BUCKETS = {
    "thumbnail":  "maths-thumbnails",
    "animation":  "maths-videos",
    "realworld":  "maths-realworld",
    "html":       "maths-html",      # theory HTML files
}

SOCIAL_TOPIC_BUCKETS = {
    "thumbnail":  "social-thumbnails",
    "animation":  "social-videos",
    "realworld":  "social-realworld",
    "html":       "social-html",      # theory HTML files
}


@router.post("/maths-topics/upload-file", status_code=200)
async def upload_maths_topic_file(
    file: UploadFile = File(...),
    file_type: str = Form(...),
    current_user: dict = Depends(get_current_user),
):
    """Admin-only: upload thumbnail/video/realworld to Supabase Storage for maths topics."""
    _require_admin(current_user)
    if file_type not in MATHS_TOPIC_BUCKETS:
        raise HTTPException(status_code=400, detail=f"file_type must be one of: {list(MATHS_TOPIC_BUCKETS.keys())}")
    bucket = MATHS_TOPIC_BUCKETS[file_type]
    import uuid as _uuid
    safe_name   = (file.filename or "file").replace(" ", "_")
    unique_name = f"{_uuid.uuid4().hex}_{safe_name}"
    data = await file.read()
    service_sb = _get_service_client()
    try:
        service_sb.storage.from_(bucket).upload(
            unique_name, data,
            file_options={"content-type": file.content_type or "application/octet-stream"},
        )
        supa_url   = os.getenv("SUPABASE_URL", "")
        public_url = f"{supa_url}/storage/v1/object/public/{bucket}/{unique_name}"
        return {"public_url": public_url, "storage_path": unique_name, "bucket": bucket}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Storage upload failed: {e}")


class MathsTopicCreate(BaseModel):
    title:             str           = Field(..., min_length=1, max_length=200)
    thumbnail_url:     Optional[str] = None
    theory_url:        Optional[str] = None   # URL of uploaded theory HTML file
    animation_url:     Optional[str] = None
    realworld_images:  list          = Field(default_factory=list)  # array of image URLs (jsonb)


@router.post("/maths-topics", status_code=201)
def create_maths_topic(payload: MathsTopicCreate, current_user: dict = Depends(get_current_user)):
    """Admin-only: insert a new maths topic row."""
    _require_admin(current_user)
    service_sb = _get_service_client()
    row = {
        "title":            payload.title.strip(),
        "thumbnail_url":    payload.thumbnail_url  or None,
        "theory_url":       payload.theory_url     or None,
        "animation_url":    payload.animation_url  or None,
        "realworld_images": payload.realworld_images,   # jsonb array — mirrors lessons table
        "created_at":       _now(),
        "updated_at":       _now(),
    }
    try:
        res     = service_sb.table("maths_topics").insert(row).execute()
        created = (res.data or [{}])[0]
        return {"success": True, "topic": created}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create maths topic: {e}")


@router.get("/maths-topics", status_code=200)
def list_maths_topics(current_user: dict = Depends(get_current_user)):
    """All authenticated users: list all maths topics.
    Uses service-role client to bypass RLS (route is still auth-protected).
    """
    service_sb = _get_service_client()
    try:
        res = (
            service_sb.table("maths_topics")
            .select("*")
            .order("created_at", desc=False)
            .execute()
        )
        topics = res.data or []
        print(f"[MATHS_TOPICS] Listed {len(topics)} topic(s).")
        return {"topics": topics, "count": len(topics)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list maths topics: {e}")


@router.delete("/maths-topics/{topic_id}", status_code=200)
def delete_maths_topic(topic_id: str, current_user: dict = Depends(get_current_user)):
    """Admin-only: delete a maths topic and its storage files."""
    _require_admin(current_user)
    service_sb = _get_service_client()
    try:
        row_res = service_sb.table("maths_topics").select("*").eq("id", topic_id).execute()
        rows = row_res.data or []
        if not rows:
            raise HTTPException(status_code=404, detail=f"Maths topic {topic_id} not found.")
        topic = rows[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch maths topic: {e}")

    def _del_storage_maths(bucket: str, url):
        if not url:
            return
        try:
            filename = url.split(f"/{bucket}/")[-1]
            service_sb.storage.from_(bucket).remove([filename])
        except Exception as se:
            print(f"[MATHS_TOPICS] Could not delete {bucket}/{filename}: {se}")

    _del_storage_maths("maths-thumbnails", topic.get("thumbnail_url"))
    _del_storage_maths("maths-videos",     topic.get("animation_url"))
    _del_storage_maths("maths-html",       topic.get("theory_url"))  # theory HTML file
    # Delete all real-world application images (jsonb array — mirrors lessons delete logic)
    for img_url in (topic.get("realworld_images") or []):
        _del_storage_maths("maths-realworld", img_url)

    try:
        service_sb.table("maths_topics").delete().eq("id", topic_id).execute()
        return {"success": True, "message": f"Maths topic {topic_id} deleted."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete maths topic: {e}")


# ════════════════════════════════════════════════════════════════
# SOCIAL TOPICS — Library Mode: Social Science subject
# ════════════════════════════════════════════════════════════════

@router.post("/social-topics/upload-file", status_code=200)
async def upload_social_topic_file(
    file: UploadFile = File(...),
    file_type: str = Form(...),
    current_user: dict = Depends(get_current_user),
):
    """Admin-only: upload thumbnail/video/realworld to Supabase Storage for social topics."""
    _require_admin(current_user)
    if file_type not in SOCIAL_TOPIC_BUCKETS:
        raise HTTPException(status_code=400, detail=f"file_type must be one of: {list(SOCIAL_TOPIC_BUCKETS.keys())}")
    bucket = SOCIAL_TOPIC_BUCKETS[file_type]
    import uuid as _uuid
    safe_name   = (file.filename or "file").replace(" ", "_")
    unique_name = f"{_uuid.uuid4().hex}_{safe_name}"
    data = await file.read()
    service_sb = _get_service_client()
    try:
        service_sb.storage.from_(bucket).upload(
            unique_name, data,
            file_options={"content-type": file.content_type or "application/octet-stream"},
        )
        supa_url   = os.getenv("SUPABASE_URL", "")
        public_url = f"{supa_url}/storage/v1/object/public/{bucket}/{unique_name}"
        return {"public_url": public_url, "storage_path": unique_name, "bucket": bucket}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Storage upload failed: {e}")


class SocialTopicCreate(BaseModel):
    title:             str           = Field(..., min_length=1, max_length=200)
    thumbnail_url:     Optional[str] = None
    theory_url:        Optional[str] = None   # URL of uploaded theory HTML file
    animation_url:     Optional[str] = None
    realworld_images:  list          = Field(default_factory=list)  # array of image URLs (jsonb)


@router.post("/social-topics", status_code=201)
def create_social_topic(payload: SocialTopicCreate, current_user: dict = Depends(get_current_user)):
    """Admin-only: insert a new social science topic row."""
    _require_admin(current_user)
    service_sb = _get_service_client()
    row = {
        "title":            payload.title.strip(),
        "thumbnail_url":    payload.thumbnail_url  or None,
        "theory_url":       payload.theory_url     or None,
        "animation_url":    payload.animation_url  or None,
        "realworld_images": payload.realworld_images,   # jsonb array — mirrors lessons table
        "created_at":       _now(),
        "updated_at":       _now(),
    }
    try:
        res     = service_sb.table("social_topics").insert(row).execute()
        created = (res.data or [{}])[0]
        return {"success": True, "topic": created}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create social topic: {e}")


@router.get("/social-topics", status_code=200)
def list_social_topics(current_user: dict = Depends(get_current_user)):
    """All authenticated users: list all social science topics.
    Uses service-role client to bypass RLS (route is still auth-protected).
    """
    service_sb = _get_service_client()
    try:
        res = (
            service_sb.table("social_topics")
            .select("*")
            .order("created_at", desc=False)
            .execute()
        )
        topics = res.data or []
        print(f"[SOCIAL_TOPICS] Listed {len(topics)} topic(s).")
        return {"topics": topics, "count": len(topics)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list social topics: {e}")


@router.delete("/social-topics/{topic_id}", status_code=200)
def delete_social_topic(topic_id: str, current_user: dict = Depends(get_current_user)):
    """Admin-only: delete a social topic and its storage files."""
    _require_admin(current_user)
    service_sb = _get_service_client()
    try:
        row_res = service_sb.table("social_topics").select("*").eq("id", topic_id).execute()
        rows = row_res.data or []
        if not rows:
            raise HTTPException(status_code=404, detail=f"Social topic {topic_id} not found.")
        topic = rows[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch social topic: {e}")

    def _del_storage_social(bucket: str, url):
        if not url:
            return
        try:
            filename = url.split(f"/{bucket}/")[-1]
            service_sb.storage.from_(bucket).remove([filename])
        except Exception as se:
            print(f"[SOCIAL_TOPICS] Could not delete {bucket}/{filename}: {se}")

    _del_storage_social("social-thumbnails", topic.get("thumbnail_url"))
    _del_storage_social("social-videos",     topic.get("animation_url"))
    _del_storage_social("social-html",       topic.get("theory_url"))  # theory HTML file
    # Delete all real-world application images (jsonb array — mirrors lessons delete logic)
    for img_url in (topic.get("realworld_images") or []):
        _del_storage_social("social-realworld", img_url)

    try:
        service_sb.table("social_topics").delete().eq("id", topic_id).execute()
        return {"success": True, "message": f"Social topic {topic_id} deleted."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete social topic: {e}")


# ════════════════════════════════════════════════════════════════
# ASSESSMENT — Science, Maths, Social Science
# Each table: topic_title (unique key) + assessment_1..10 (HTML)
# ════════════════════════════════════════════════════════════════

ASSESSMENT_TABLES = {
    "science": "science_assessment",
    "maths":   "maths_assessment",
    "social":  "social_assessment",
}

# Supabase Storage buckets for assessment HTML files
ASSESSMENT_BUCKETS = {
    "science": "science-assessment",
    "maths":   "maths-assessment",
    "social":  "social-assessment",
}


class AssessmentSave(BaseModel):
    topic_title:      str           = Field(..., min_length=1, max_length=300)
    # assessment_N stores the public URL of the uploaded HTML file
    assessment_1:     Optional[str] = None
    assessment_2:     Optional[str] = None
    assessment_3:     Optional[str] = None
    assessment_4:     Optional[str] = None
    assessment_5:     Optional[str] = None
    assessment_6:     Optional[str] = None
    assessment_7:     Optional[str] = None
    assessment_8:     Optional[str] = None
    assessment_9:     Optional[str] = None
    assessment_10:    Optional[str] = None
    # thumbnail_url_N stores the public URL of the thumbnail image
    thumbnail_url_1:  Optional[str] = None
    thumbnail_url_2:  Optional[str] = None
    thumbnail_url_3:  Optional[str] = None
    thumbnail_url_4:  Optional[str] = None
    thumbnail_url_5:  Optional[str] = None
    thumbnail_url_6:  Optional[str] = None
    thumbnail_url_7:  Optional[str] = None
    thumbnail_url_8:  Optional[str] = None
    thumbnail_url_9:  Optional[str] = None
    thumbnail_url_10: Optional[str] = None


def _get_assessment_table(subject: str) -> str:
    tbl = ASSESSMENT_TABLES.get(subject)
    if not tbl:
        raise HTTPException(status_code=400, detail=f"subject must be one of: {list(ASSESSMENT_TABLES.keys())}")
    return tbl


@router.post("/assessment/{subject}/upload-file", status_code=200)
async def upload_assessment_file(
    subject: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Admin-only: upload an assessment HTML file to the subject-specific bucket.
    Returns { public_url, storage_path, bucket }.
    """
    _require_admin(current_user)
    bucket = ASSESSMENT_BUCKETS.get(subject)
    if not bucket:
        raise HTTPException(
            status_code=400,
            detail=f"subject must be one of: {list(ASSESSMENT_BUCKETS.keys())}",
        )
    import uuid as _uuid
    safe_name   = (file.filename or "assessment.html").replace(" ", "_")
    unique_name = f"{_uuid.uuid4().hex}_{safe_name}"
    data        = await file.read()
    service_sb  = _get_service_client()
    try:
        service_sb.storage.from_(bucket).upload(
            unique_name, data,
            # Always force text/html — the browser often reports application/octet-stream
            # for .html files, which causes Supabase to serve them as plain text and the
            # iframe displays raw source code instead of rendering the quiz/content.
            file_options={"content-type": "text/html; charset=utf-8"},
        )
        supa_url   = os.getenv("SUPABASE_URL", "")
        public_url = f"{supa_url}/storage/v1/object/public/{bucket}/{unique_name}"
        print(f"[ASSESSMENT_UPLOAD] Uploaded {safe_name} → {bucket}/{unique_name}")
        return {"public_url": public_url, "storage_path": unique_name, "bucket": bucket}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Assessment file upload failed: {e}")


@router.get("/assessment/{subject}", status_code=200)
def list_assessments(subject: str, current_user: dict = Depends(get_current_user)):
    """All authenticated users: list all assessment rows for a subject.

    Uses the service-role client for SELECT so that RLS is bypassed correctly.
    The user-scoped client (_sb) inadvertently presents as service_role to
    Supabase RLS (because create_client() is called with the service key even
    when a user JWT is supplied), causing the 'authenticated'-role SELECT
    policy to evaluate to FALSE and return 0 rows.
    """
    tbl = _get_assessment_table(subject)
    service_sb = _get_service_client()
    try:
        res = (
            service_sb.table(tbl)
            .select("*")
            .order("topic_title", desc=False)
            .execute()
        )
        rows = res.data or []
        return {"assessments": rows, "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list {subject} assessments: {e}")


@router.post("/assessment/{subject}", status_code=200)
def save_assessment(subject: str, payload: AssessmentSave, current_user: dict = Depends(get_current_user)):
    """Admin-only: upsert assessment row for a topic (insert or update on topic_title conflict)."""
    _require_admin(current_user)
    tbl = _get_assessment_table(subject)
    service_sb = _get_service_client()
    row = {
        "topic_title":      payload.topic_title.strip(),
        # assessment_N: public URL of the uploaded HTML file
        "assessment_1":     payload.assessment_1,
        "assessment_2":     payload.assessment_2,
        "assessment_3":     payload.assessment_3,
        "assessment_4":     payload.assessment_4,
        "assessment_5":     payload.assessment_5,
        "assessment_6":     payload.assessment_6,
        "assessment_7":     payload.assessment_7,
        "assessment_8":     payload.assessment_8,
        "assessment_9":     payload.assessment_9,
        "assessment_10":    payload.assessment_10,
        # thumbnail_url_N: public URL of the thumbnail image
        "thumbnail_url_1":  payload.thumbnail_url_1,
        "thumbnail_url_2":  payload.thumbnail_url_2,
        "thumbnail_url_3":  payload.thumbnail_url_3,
        "thumbnail_url_4":  payload.thumbnail_url_4,
        "thumbnail_url_5":  payload.thumbnail_url_5,
        "thumbnail_url_6":  payload.thumbnail_url_6,
        "thumbnail_url_7":  payload.thumbnail_url_7,
        "thumbnail_url_8":  payload.thumbnail_url_8,
        "thumbnail_url_9":  payload.thumbnail_url_9,
        "thumbnail_url_10": payload.thumbnail_url_10,
        "updated_at":       _now(),
    }
    try:
        res = (
            service_sb.table(tbl)
            .upsert(row, on_conflict="topic_title")
            .execute()
        )
        saved = (res.data or [{}])[0]
        return {"success": True, "assessment": saved}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save {subject} assessment: {e}")


# ════════════════════════════════════════════════════════════════
# ASSESSMENT SHARE SESSIONS  —  Teacher PIN/URL generation
# ════════════════════════════════════════════════════════════════
# Tables:
#   assessment_sessions        — teacher-created sessions (PIN + slug)
#   assessment_session_joins   — student join events per session
#
# Endpoints:
#   POST /sync/assessment-session/create          — teacher creates session
#   GET  /sync/assessment-session/by-pin/{pin}    — student/teacher look up by PIN
#   GET  /sync/assessment-session/by-slug/{slug}  — student joins via shareable URL
#   POST /sync/assessment-session/{sid}/join      — student records join
# ════════════════════════════════════════════════════════════════

from datetime import timedelta


class SessionCreate(BaseModel):
    subject:        str   = Field(..., min_length=1, max_length=50)
    topic_title:    str   = Field(..., min_length=1, max_length=300)
    assessment_num: int   = Field(..., ge=1, le=10)
    assessment_url: Optional[str] = None
    thumbnail_url:  Optional[str] = None


class SessionJoin(BaseModel):
    student_email: Optional[str] = None
    nickname:      Optional[str] = None


def _generate_unique_pin(service_sb) -> str:
    """Generate a unique 6-digit PIN not already in use by a non-expired session."""
    for _ in range(20):
        pin = f"{secrets.randbelow(1_000_000):06d}"
        # Check uniqueness among non-expired sessions only
        res = (
            service_sb.table("assessment_sessions")
            .select("id")
            .eq("pin", pin)
            .gt("expires_at", datetime.now(timezone.utc).isoformat())
            .execute()
        )
        if not (res.data or []):
            return pin
    raise HTTPException(status_code=500, detail="Could not generate a unique PIN — try again")


@router.post("/assessment-session/create", status_code=201)
def create_assessment_session(
    payload: SessionCreate,
    current_user: dict = Depends(get_current_user),
):
    """
    Teacher creates a share session for a specific assessment.
    Returns: { session_id, pin, slug, share_url, expires_at }
    PIN is valid for 10 hours from creation.
    """
    service_sb = _get_service_client()

    pin  = _generate_unique_pin(service_sb)
    slug = secrets.token_hex(8)   # 16-char hex slug for shareable URL

    now        = datetime.now(timezone.utc)
    expires_at = (now + timedelta(hours=10)).isoformat()

    row = {
        "pin":            pin,
        "slug":           slug,
        "subject":        payload.subject,
        "topic_title":    payload.topic_title,
        "assessment_num": payload.assessment_num,
        "assessment_url": payload.assessment_url,
        "thumbnail_url":  payload.thumbnail_url,
        "teacher_id":     current_user.get("sub") or current_user.get("id"),
        "teacher_email":  current_user.get("email"),
        "expires_at":     expires_at,
    }

    try:
        res = service_sb.table("assessment_sessions").insert(row).execute()
        saved = (res.data or [{}])[0]
        return {
            "session_id": saved.get("id"),
            "pin":        pin,
            "slug":       slug,
            "expires_at": expires_at,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create session: {e}")


@router.get("/assessment-session/by-pin/{pin}", status_code=200)
def get_session_by_pin(pin: str):
    """
    Public endpoint — no JWT required.
    Student enters a 6-digit PIN; returns session details if valid and non-expired.
    """
    service_sb = _get_service_client()
    now = datetime.now(timezone.utc).isoformat()

    try:
        res = (
            service_sb.table("assessment_sessions")
            .select("*")
            .eq("pin", pin.strip())
            .eq("is_expired", False)
            .gt("expires_at", now)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            raise HTTPException(status_code=404, detail="PIN not found or expired")
        session = rows[0]
        # Count joins
        joins_res = (
            service_sb.table("assessment_session_joins")
            .select("id", count="exact")
            .eq("session_id", session["id"])
            .execute()
        )
        join_count = joins_res.count or 0
        return {**session, "join_count": join_count}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PIN lookup failed: {e}")


@router.get("/assessment-session/by-slug/{slug}", status_code=200)
def get_session_by_slug(slug: str):
    """
    Public endpoint — no JWT required.
    Student visits shareable URL ?session=<slug>; returns session details if valid.
    """
    service_sb = _get_service_client()
    now = datetime.now(timezone.utc).isoformat()

    try:
        res = (
            service_sb.table("assessment_sessions")
            .select("*")
            .eq("slug", slug.strip())
            .eq("is_expired", False)
            .gt("expires_at", now)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            raise HTTPException(status_code=404, detail="Session not found or expired")
        session = rows[0]
        joins_res = (
            service_sb.table("assessment_session_joins")
            .select("id", count="exact")
            .eq("session_id", session["id"])
            .execute()
        )
        join_count = joins_res.count or 0
        return {**session, "join_count": join_count}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Slug lookup failed: {e}")


@router.post("/assessment-session/{session_id}/join", status_code=201)
def join_assessment_session(
    session_id: str,
    body: SessionJoin = SessionJoin(),
):
    """
    Public endpoint — no JWT required.
    Records a student join event for analytics / participant count.
    """
    service_sb = _get_service_client()

    row = {
        "session_id":    session_id,
        "student_email": body.student_email,
        "nickname":      body.nickname,
    }

    try:
        res = service_sb.table("assessment_session_joins").insert(row).execute()
        saved = (res.data or [{}])[0]
        return {"joined": True, "join_id": saved.get("id")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Join failed: {e}")


@router.get("/assessment-session/{session_id}/join-count", status_code=200)
def get_session_join_count(session_id: str):
    """
    Public endpoint - no JWT required.
    Returns the current participant (join) count for a session.
    Polled by the student lobby screen every few seconds to show live join counts.
    """
    service_sb = _get_service_client()
    try:
        res = (
            service_sb.table("assessment_session_joins")
            .select("id", count="exact")
            .eq("session_id", session_id)
            .execute()
        )
        return {"session_id": session_id, "count": res.count or 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Join count failed: {e}")


# ════════════════════════════════════════════════════════════════
# TEST STUDENTS  —  Student registration + result tracking
# ════════════════════════════════════════════════════════════════
# Table: test_students
#
# Endpoints:
#   POST  /sync/test-students/register                   — public, student registers before assessment
#   PATCH /sync/test-students/{record_id}/result         — public, save score after submission
#   GET   /sync/test-students/by-session/{session_id}    — teacher-auth required, view results
#   GET   /sync/test-students/by-pin/{pin}               — teacher-auth required, view results by PIN
#   GET   /sync/test-students/by-assessment              — teacher-auth fallback (by subject+topic+num)
# ════════════════════════════════════════════════════════════════


class StudentRegister(BaseModel):
    session_id:        Optional[str] = None
    pin_code:          str           = Field(..., min_length=1, max_length=10)
    student_name:      str           = Field(..., min_length=1, max_length=120)
    roll_number:       str           = Field(..., min_length=1, max_length=50)
    user_id:           Optional[str] = None   # email or nickname
    subject:           Optional[str] = None
    topic:             Optional[str] = None
    assessment_number: Optional[int] = None


class StudentResult(BaseModel):
    result: str = Field(..., min_length=1, max_length=50)   # e.g. "70%"


@router.post("/test-students/register", status_code=201)
def register_test_student(payload: StudentRegister):
    """
    Public endpoint — no JWT required.
    Called when the student clicks 'Start Assessment' after filling the registration form.
    Inserts a row in test_students and returns the record id (used later to update result).
    """
    service_sb = _get_service_client()

    row = {
        "pin_code":          payload.pin_code,
        "student_name":      payload.student_name,
        "roll_number":       payload.roll_number,
        "user_id":           payload.user_id,
        "subject":           payload.subject,
        "topic":             payload.topic,
        "assessment_number": payload.assessment_number,
        "result":            "Pending",
    }
    if payload.session_id:
        row["session_id"] = payload.session_id

    try:
        res = service_sb.table("test_students").insert(row).execute()
        saved = (res.data or [{}])[0]
        return {"registered": True, "record_id": saved.get("id")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Registration failed: {e}")


@router.patch("/test-students/{record_id}/result", status_code=200)
def update_student_result(record_id: str, body: StudentResult):
    """
    Public endpoint — no JWT required.
    Called (via postMessage relay from assessment iframe) when student submits the assessment.
    Updates the result column for the student's registration record.
    """
    service_sb = _get_service_client()

    try:
        res = (
            service_sb.table("test_students")
            .update({"result": body.result})
            .eq("id", record_id)
            .execute()
        )
        updated = res.data or []
        if not updated:
            raise HTTPException(status_code=404, detail="Student record not found")
        return {"updated": True, "record_id": record_id, "result": body.result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Result update failed: {e}")


# ════════════════════════════════════════════════════════════════
# QUIZ RESULTS  —  Detailed score storage per student
# ════════════════════════════════════════════════════════════════
# Workflow:
#   Student submits quiz → iframe posts { type:'ASSESSMENT_RESULT', score, total, percentage }
#   student.html catches it → POST /sync/quiz-results
#   Backend inserts into quiz_results AND updates test_students.result
#   Teacher clicks "View Results" → GET endpoint returns students + quiz_results joined
# ════════════════════════════════════════════════════════════════

class QuizResultSubmit(BaseModel):
    test_student_id:  Optional[str] = None   # FK to test_students.id
    session_id:       Optional[str] = None   # FK to assessment_sessions.id
    pin_code:         str           = Field(..., min_length=1, max_length=10)
    student_name:     str           = Field(..., min_length=1, max_length=120)
    roll_number:      str           = Field(..., min_length=1, max_length=50)
    score:            int           = Field(..., ge=0)          # correct answers e.g. 7
    total_questions:  int           = Field(..., ge=1)          # total questions  e.g. 10
    percentage:       float         = Field(..., ge=0, le=100)  # e.g. 70.0


@router.post("/quiz-results", status_code=201)
def submit_quiz_result(payload: QuizResultSubmit):
    """
    Public endpoint — no JWT required.
    Called by student.html when the assessment iframe posts an ASSESSMENT_RESULT message.

    Steps:
      1. Inserts a row into quiz_results (score, total_questions, percentage).
      2. Updates test_students.result to a human-readable string  e.g. '7/10 (70.00%)'.

    Returns the new quiz_result id so the frontend can reference it.
    """
    service_sb = _get_service_client()

    # Build a readable result string that will show in test_students.result
    pct_str    = f"{payload.percentage:.2f}"
    result_str = f"{payload.score}/{payload.total_questions} ({pct_str}%)"

    # 1. Insert into quiz_results
    qr_row = {
        "pin_code":         payload.pin_code,
        "student_name":     payload.student_name,
        "roll_number":      payload.roll_number,
        "score":            payload.score,
        "total_questions":  payload.total_questions,
        "percentage":       round(payload.percentage, 2),
    }
    if payload.test_student_id:
        qr_row["test_student_id"] = payload.test_student_id
    if payload.session_id:
        qr_row["session_id"] = payload.session_id

    try:
        qr_res = service_sb.table("quiz_results").insert(qr_row).execute()
        saved  = (qr_res.data or [{}])[0]
        quiz_result_id = saved.get("id")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"quiz_results insert failed: {e}")

    # 2. Update test_students.result (best-effort — don't fail if record not found)
    if payload.test_student_id:
        try:
            service_sb.table("test_students") \
                .update({"result": result_str}) \
                .eq("id", payload.test_student_id) \
                .execute()
        except Exception as e:
            print(f"[QUIZ] Warning: could not update test_students result: {e}")

    print(f"[QUIZ] ✅ Quiz result saved: {result_str} student={payload.student_name!r} pin={payload.pin_code!r}")
    return {"saved": True, "quiz_result_id": quiz_result_id, "result": result_str}


def _attach_quiz_results(service_sb, students: list) -> list:
    """
    Helper: given a list of test_students rows, look up their quiz_results
    and attach score, total_questions, percentage to each row.
    Matches on test_student_id (preferred) or pin_code+student_name (fallback).
    """
    if not students:
        return students

    # Collect all test_student ids that are not None
    ts_ids = [s["id"] for s in students if s.get("id")]
    if not ts_ids:
        return students

    try:
        qr_res = (
            service_sb.table("quiz_results")
            .select("test_student_id, score, total_questions, percentage, submitted_at")
            .in_("test_student_id", ts_ids)
            .order("submitted_at", desc=True)
            .execute()
        )
        quiz_rows = qr_res.data or []
    except Exception:
        # Non-fatal — just return students without quiz data
        return students

    # Build a map: test_student_id → quiz_result row (keep latest)
    qr_map: dict = {}
    for qr in quiz_rows:
        tid = qr.get("test_student_id")
        if tid and tid not in qr_map:
            qr_map[tid] = qr

    # Attach quiz result fields to each student row
    for s in students:
        qr = qr_map.get(s.get("id"))
        if qr:
            s["quiz_score"]       = qr["score"]
            s["quiz_total"]       = qr["total_questions"]
            s["quiz_percentage"]  = float(qr["percentage"] or 0)
            s["quiz_submitted_at"]= qr["submitted_at"]
        else:
            s["quiz_score"]       = None
            s["quiz_total"]       = None
            s["quiz_percentage"]  = None
            s["quiz_submitted_at"]= None

    return students


@router.get("/test-students/by-session/{session_id}", status_code=200)
def get_results_by_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Teacher-authenticated endpoint.
    Returns all student registrations for the given assessment session.
    Verifies the requesting teacher owns the session.
    """
    service_sb = _get_service_client()
    teacher_id    = current_user.get("sub") or current_user.get("id")
    teacher_email = current_user.get("email")

    # Verify teacher owns this session
    try:
        sess_res = (
            service_sb.table("assessment_sessions")
            .select("id, teacher_id, teacher_email")
            .eq("id", session_id)
            .limit(1)
            .execute()
        )
        rows = sess_res.data or []
        if not rows:
            raise HTTPException(status_code=404, detail="Session not found")
        sess = rows[0]
        if sess.get("teacher_id") != teacher_id and sess.get("teacher_email") != teacher_email:
            raise HTTPException(status_code=403, detail="You do not own this session")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Session lookup failed: {e}")

    # Fetch student rows + quiz_results
    try:
        res = (
            service_sb.table("test_students")
            .select("*")
            .eq("session_id", session_id)
            .order("timestamp", desc=False)
            .execute()
        )
        students = _attach_quiz_results(service_sb, res.data or [])
        return {"students": students, "count": len(students)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Results fetch failed: {e}")


@router.get("/test-students/by-pin/{pin}", status_code=200)
def get_results_by_pin(
    pin: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Teacher-authenticated endpoint.
    Looks up the most recent session by PIN (must belong to calling teacher),
    then returns all student rows for it.
    """
    service_sb = _get_service_client()
    teacher_id    = current_user.get("sub") or current_user.get("id")
    teacher_email = current_user.get("email")

    # Find session owned by this teacher
    try:
        sess_res = (
            service_sb.table("assessment_sessions")
            .select("id, teacher_id, teacher_email, created_at")
            .eq("pin", pin.strip())
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        rows = sess_res.data or []
        if not rows:
            raise HTTPException(status_code=404, detail="No session found for this PIN")
        owned = [r for r in rows if r.get("teacher_id") == teacher_id or r.get("teacher_email") == teacher_email]
        if not owned:
            raise HTTPException(status_code=403, detail="You do not own this session")
        session_id = owned[0]["id"]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PIN session lookup failed: {e}")

    # Fetch student rows + quiz_results
    try:
        res = (
            service_sb.table("test_students")
            .select("*")
            .eq("session_id", session_id)
            .order("timestamp", desc=False)
            .execute()
        )
        students = _attach_quiz_results(service_sb, res.data or [])
        return {"session_id": session_id, "students": students, "count": len(students)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Results fetch failed: {e}")


@router.get("/test-students/by-assessment", status_code=200)
def get_results_by_assessment(
    subject: str,
    topic_title: str,
    assessment_num: int,
    current_user: dict = Depends(get_current_user),
):
    """
    Teacher-authenticated endpoint (fallback).
    Finds the most recent session matching subject+topic+assessment_num owned by this teacher,
    then returns all student rows for it.
    Used when 'View Results' is clicked before any specific PIN has been cached in the UI.
    """
    service_sb = _get_service_client()
    teacher_id    = current_user.get("sub") or current_user.get("id")
    teacher_email = current_user.get("email")

    # Find most recent matching session owned by this teacher
    try:
        sess_res = (
            service_sb.table("assessment_sessions")
            .select("id, teacher_id, teacher_email, created_at")
            .eq("subject", subject.strip())
            .eq("topic_title", topic_title.strip())
            .eq("assessment_num", assessment_num)
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        rows = sess_res.data or []
        owned = [r for r in rows if r.get("teacher_id") == teacher_id or r.get("teacher_email") == teacher_email]
        if not owned:
            return {
                "session_id": None, "students": [], "count": 0,
                "message": "No shared session found — share this assessment first to see student results"
            }
        session_id = owned[0]["id"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Assessment session lookup failed: {e}")

    # Fetch student rows + quiz_results
    try:
        res = (
            service_sb.table("test_students")
            .select("*")
            .eq("session_id", session_id)
            .order("timestamp", desc=False)
            .execute()
        )
        students = _attach_quiz_results(service_sb, res.data or [])
        return {"session_id": session_id, "students": students, "count": len(students)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Results fetch failed: {e}")
