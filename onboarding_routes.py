# onboarding_routes.py  —  Haezet Onboarding API
# ============================================================
#
# POST-LOGIN ONBOARDING FLOW
# --------------------------
# After a user authenticates (email/password or Google OAuth),
# they are redirected to onboarding.html which calls these endpoints:
#
#  GET  /onboarding/status
#       Returns { completed: bool }.  True if the user already has a
#       school_name row → frontend skips onboarding, goes to dashboard.
#
#  POST /onboarding/school
#       Body: { school: str }
#       Upserts a school_name row and a user_login row for the caller.
#       User identity comes entirely from the verified JWT — never trusted
#       from the request body.
#
#  POST /onboarding/role/student
#       No body required.
#       Inserts a student_login row (idempotent via ON CONFLICT DO NOTHING).
#       Returns { redirect: '/student.html' }.
#
#  POST /onboarding/role/teacher/verify-pin
#       Body: { pin: str }  — the 6-digit code the teacher typed.
#       Calls the Supabase SECURITY DEFINER RPC claim_teacher_pin(pin)
#       which atomically validates and claims the PIN without exposing the
#       full PIN table to the frontend.
#       Returns { valid: bool }.
#
# Security:
#   - All endpoints require a valid Bearer JWT (get_current_user dependency).
#   - user_id and email are taken from the verified JWT payload.
#   - The service-role client is used on the backend to bypass RLS where
#     needed (e.g. reading auth.users metadata inside the RPC), but the
#     service key is NEVER sent to the frontend.
#   - teacher_pincode has no SELECT RLS policy — users cannot enumerate PINs.
# ============================================================

import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field, field_validator

from auth_utils import get_current_user, get_supabase

router = APIRouter(prefix="/onboarding", tags=["Onboarding"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMAS
# ══════════════════════════════════════════════════════════════════════════════

class SchoolRequest(BaseModel):
    school: str = Field(..., min_length=2, max_length=200)

    @field_validator("school")
    @classmethod
    def school_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("School name cannot be blank.")
        return v


class PinRequest(BaseModel):
    pin: str = Field(..., min_length=6, max_length=6)

    @field_validator("pin")
    @classmethod
    def pin_digits_only(cls, v: str) -> str:
        if not re.fullmatch(r"\d{6}", v):
            raise ValueError("PIN must be exactly 6 digits.")
        return v


# ══════════════════════════════════════════════════════════════════════════════
# GET /onboarding/status
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/status")
def onboarding_status(current_user: dict = Depends(get_current_user)):
    """
    Check whether this user has already completed the onboarding school step.
    Returns { completed: bool }.
    Frontend skips onboarding and enters the dashboard if completed is true.
    """
    user_id = current_user["id"]
    sb = get_supabase()

    try:
        row = (
            sb.table("school_name")
            .select("id")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        completed = row.data is not None
    except Exception as exc:
        print(f"[ONBOARDING] ⚠ status check failed for {user_id}: {exc}")
        # Default to not completed so the user fills in the school name.
        completed = False

    return {"completed": completed}


# ══════════════════════════════════════════════════════════════════════════════
# POST /onboarding/school
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/school", status_code=201)
def save_school(body: SchoolRequest, current_user: dict = Depends(get_current_user)):
    """
    Save the user's school/institute name and upsert their user_login row.
    user_id and email come from the verified JWT — NOT from the request body.
    """
    user_id = current_user["id"]
    email   = current_user["email"]
    name    = current_user.get("name", email)
    now     = _now_iso()
    sb      = get_supabase()

    # 1. Upsert school_name (UPDATE if exists, INSERT otherwise)
    try:
        existing = (
            sb.table("school_name")
            .select("id")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if existing.data:
            sb.table("school_name").update({
                "school":     body.school,
                "updated_at": now,
            }).eq("user_id", user_id).execute()
            print(f"[ONBOARDING] 🏫 School updated for {email}: {body.school}")
        else:
            sb.table("school_name").insert({
                "user_id":    user_id,
                "email":      email,
                "school":     body.school,
                "created_at": now,
                "updated_at": now,
            }).execute()
            print(f"[ONBOARDING] 🏫 School saved for {email}: {body.school}")
    except Exception as exc:
        import traceback
        print(f"[ONBOARDING] ⚠ school save FULL ERROR for {email}: {repr(exc)}")
        print(traceback.format_exc())
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save school name: {str(exc)[:200]}",
        )

    # 2. Upsert user_login (idempotent — refreshes last_login on re-entry)
    try:
        ul_existing = (
            sb.table("user_login")
            .select("id")
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )
        provider = current_user.get("provider", "email")
        if ul_existing.data:
            sb.table("user_login").update({
                "email":      email,
                "user_name":  name,
                "last_login": now,
            }).eq("id", user_id).execute()
        else:
            sb.table("user_login").insert({
                "id":         user_id,
                "email":      email,
                "user_name":  name,
                "provider":   provider,
                "created_at": now,
                "last_login": now,
            }).execute()
        print(f"[ONBOARDING] ✅ user_login upserted for {email}")
    except Exception as exc:
        # Non-fatal: school_name is saved; user_login failure should not block.
        print(f"[ONBOARDING] ⚠ user_login upsert failed for {email}: {exc}")

    return {"success": True, "school": body.school}


# ══════════════════════════════════════════════════════════════════════════════
# POST /onboarding/role/student
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/role/student", status_code=201)
def register_student(current_user: dict = Depends(get_current_user)):
    """
    Record that this user chose the Student role.
    Inserts into student_login (idempotent — skips insert if row exists).
    Returns { redirect: '/student.html' }.
    """
    user_id = current_user["id"]
    email   = current_user["email"]
    name    = current_user.get("name", email)
    now     = _now_iso()
    sb      = get_supabase()

    try:
        existing = (
            sb.table("student_login")
            .select("id")
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        if not existing.data:
            sb.table("student_login").insert({
                "user_id":    user_id,
                "user_name":  name,
                "email":      email,
                "created_at": now,
            }).execute()
            print(f"[ONBOARDING] 🎓 Student registered: {email} (id={user_id})")
        else:
            print(f"[ONBOARDING] 🎓 Student already registered: {email}")
    except Exception as exc:
        print(f"[ONBOARDING] ⚠ student_login insert failed for {email}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save student record. Please try again.",
        )

    return {"success": True, "redirect": "/student.html"}


# ══════════════════════════════════════════════════════════════════════════════
# POST /onboarding/role/teacher/verify-pin
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/role/teacher/verify-pin")
def verify_teacher_pin(body: PinRequest, current_user: dict = Depends(get_current_user)):
    """
    Validate and claim a 6-digit teacher PIN via a SECURITY DEFINER RPC.

    Security model:
      - Calls the Supabase RPC claim_teacher_pin(pin) using a user-scoped
        client carrying the caller's JWT.  Inside the RPC, auth.uid() resolves
        to this user's UUID — no user_id is accepted from the request body.
      - The RPC atomically marks the PIN as 'claimed' (UPDATE WHERE pin_status
        = 'active' AND user_id IS NULL) so no PIN can be reused.
      - Returns { valid: true } on success; { valid: false } on failure.
      - The teacher_pincode table has NO SELECT policy — users can never
        enumerate available PINs directly.
    """
    user_id = current_user["id"]
    email   = current_user["email"]
    token   = current_user.get("token", "")

    # Use a user-scoped client so auth.uid() inside the RPC is correct
    try:
        sb_user = get_supabase(token=token)
        result  = sb_user.rpc("claim_teacher_pin", {"p_pin": body.pin}).execute()
        # result.data is a single boolean from the RPC RETURNS BOOLEAN
        valid = bool(result.data)
    except Exception as exc:
        print(f"[ONBOARDING] ⚠ claim_teacher_pin RPC error for {email}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PIN verification service error. Please try again.",
        )

    if valid:
        print(f"[ONBOARDING] 🏫 Teacher PIN claimed by {email} (id={user_id})")
    else:
        print(f"[ONBOARDING] ❌ Invalid/already-used PIN attempt by {email}")

    return {"valid": valid}
