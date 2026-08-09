"""
passkey_routes.py — GenZet Passkey Access Control  (v2.0 simplified)
=====================================================================
Single passkey unlocks both "AI Creator" (animind) and "Q Anim" (question).
Simulation is NOT protected.

Endpoints
---------
  POST /auth/verify-passkey   — hash input, check against mode_passkeys table
  POST /auth/passkey/grant    — record user_id in passkey_access (JWT required)
  GET  /auth/passkey/check    — check if user already has access (JWT required)

Tables (see passkey_setup.sql)
------------------------------
  mode_passkeys : id, passkey_hash, label, created_at
  passkey_access: id, user_id, granted_at
"""

import hashlib

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(tags=["Passkey"])


class PasskeyVerifyRequest(BaseModel):
    passkey: str   # plain passkey (8–9 chars) — never stored, immediately hashed


# ══════════════════════════════════════════════════════════════════════════════
# POST /auth/verify-passkey
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/auth/verify-passkey")
async def verify_passkey(request: PasskeyVerifyRequest):
    """
    Verify the typed passkey for the "Create with AI" gate.

    Accepts  : { passkey: str }
    Returns  : { ok: bool }
    HTTP 400 : passkey length not 8–9 characters
    HTTP 500 : unexpected Supabase error
    """
    passkey = (request.passkey or "").strip()

    if len(passkey) < 8 or len(passkey) > 9:
        raise HTTPException(
            status_code=400,
            detail="Passkey must be exactly 8 or 9 characters.",
        )

    passkey_hash = hashlib.sha256(passkey.encode("utf-8")).hexdigest()

    try:
        from auth_utils import get_supabase  # pyrefly: ignore [missing-import]
        db = get_supabase()
        result = (
            db.table("mode_passkeys")
            .select("id")
            .eq("passkey_hash", passkey_hash)
            .limit(1)
            .execute()
        )
        matched = bool(result.data)
    except Exception as e:
        print(f"[PASSKEY] ⚠ Supabase error during verification: {e}")
        raise HTTPException(
            status_code=500,
            detail="Passkey verification failed due to a server error. Please try again.",
        )

    if matched:
        print("[PASSKEY] ✅ Access granted")
    else:
        print("[PASSKEY] ❌ Access denied (wrong passkey)")

    return {"ok": matched}


# ══════════════════════════════════════════════════════════════════════════════
# POST /auth/passkey/grant
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/auth/passkey/grant")
async def passkey_grant(request: Request):
    """
    Record that the authenticated user has been granted access.
    Called by the frontend after a successful verify-passkey.

    Auth    : Authorization: Bearer <jwt>  (required)
    Returns : { "granted": true }
    """
    from auth_utils import get_supabase  # pyrefly: ignore [missing-import]

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required.")
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required.")

    try:
        db = get_supabase()
        user_res = db.auth.get_user(token)
        user_id = str(user_res.user.id)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")

    try:
        db.table("passkey_access").upsert(
            {"user_id": user_id},
            on_conflict="user_id",
        ).execute()
        print(f"[PASSKEY] ✅ Grant recorded for user={user_id[:8]}…")
    except Exception as e:
        print(f"[PASSKEY] ⚠ Grant insert failed: {e}")
        raise HTTPException(status_code=500, detail="Could not record access grant.")

    return {"granted": True}


# ══════════════════════════════════════════════════════════════════════════════
# GET /auth/passkey/check
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/auth/passkey/check")
async def passkey_check(request: Request):
    """
    Check whether the authenticated user already has passkey access.

    Auth    : Authorization: Bearer <jwt>  (required)
    Returns : { "granted": true | false }
    """
    from auth_utils import get_supabase  # pyrefly: ignore [missing-import]

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required.")
    token = auth_header.split(" ", 1)[1].strip()

    try:
        db = get_supabase()
        user_res = db.auth.get_user(token)
        user_id = str(user_res.user.id)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")

    try:
        result = (
            db.table("passkey_access")
            .select("id")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        granted = bool(result.data)
    except Exception as e:
        print(f"[PASSKEY] ⚠ Access check failed: {e}")
        # Fail open — modal re-appears and user can verify again
        granted = False

    return {"granted": granted}
