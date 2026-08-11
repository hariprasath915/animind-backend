"""
passkey_routes.py — GenZet Passkey Access Control  (v3.0)
=========================================================
Refactored to support:
  - One passkey per user   (user_id UNIQUE in passkey_access)
  - One user per passkey   (passkey_id UNIQUE in passkey_access)
  - User identity stored   (user_name, user_email, passkey_used)
  - Returning-user bypass  (check existing grant before prompting)
  - Admin passkey management (add / list passkeys via X-Admin-Token)

Single passkey unlocks both "AI Creator" (animind) and "Q Anim" (question).
Simulation is NOT protected.

Endpoints
---------
  POST /auth/verify-passkey      — verify passkey; also checks if user already
                                    has a grant (so frontend can skip the modal)
  POST /auth/passkey/grant       — claim a passkey for the authenticated user;
                                    stores user_id, user_name, user_email, passkey
  GET  /auth/passkey/check       — check if user already has a grant (JWT required)
  POST /admin/passkeys/add       — add a new passkey (X-Admin-Token required)
  GET  /admin/passkeys/list      — list all passkeys   (X-Admin-Token required)

Tables (see passkey_setup.sql)
------------------------------
  admin_passkeys : id, passkey, passkey_hash, label, created_at
  passkey_access : id, user_id, user_name, user_email, passkey_id,
                   passkey_used, granted_at
"""

import hashlib
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(tags=["Passkey"])

# ── Admin token (same env var used by admin_router.py) ────────────────────────
_ADMIN_TOKEN = os.getenv("ADMIN_SECRET_TOKEN", "")


# ── Pydantic request models ───────────────────────────────────────────────────

class PasskeyVerifyRequest(BaseModel):
    passkey: str   # plain passkey — never stored, immediately hashed for lookup


class PasskeyGrantRequest(BaseModel):
    passkey: str   # the passkey that was verified (stored in passkey_access)


class AdminAddPasskeyRequest(BaseModel):
    passkey: str
    label:   Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _require_admin_token(request: Request) -> None:
    """Reject requests that don't carry the correct X-Admin-Token header."""
    if not _ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_SECRET_TOKEN is not configured on this server.",
        )
    if request.headers.get("X-Admin-Token", "") != _ADMIN_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing X-Admin-Token header.",
        )


def _get_user_from_token(request: Request):
    """
    Extract and verify the Supabase JWT from the Authorization header.
    Returns (db, user_id, user_name, user_email).
    Raises HTTPException on auth failure.
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
        user = user_res.user
        if not user:
            raise Exception("No user returned")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")

    user_id    = str(user.id)
    user_email = user.email or ""
    meta       = user.user_metadata or {}
    user_name  = (
        meta.get("name")
        or meta.get("full_name")
        or user_email
    )

    return db, user_id, user_name, user_email


# ══════════════════════════════════════════════════════════════════════════════
# POST /auth/verify-passkey
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/auth/verify-passkey")
async def verify_passkey(request: Request, body: PasskeyVerifyRequest):
    """
    Verify the typed passkey for the "Create with AI" gate.

    Requires: Authorization: Bearer <jwt>

    Flow:
      1. Authenticate the user from the JWT.
      2. If the user already has a grant in passkey_access → return ok=true,
         already_granted=true (frontend skips the grant call).
      3. Hash the submitted passkey and look up admin_passkeys.
      4. If found, check whether any OTHER user has already claimed this passkey
         (UNIQUE passkey_id in passkey_access). If so → return ok=false,
         detail="already_assigned".
      5. Otherwise → return ok=true (frontend must call /auth/passkey/grant next).

    Returns : { ok: bool, already_granted?: bool, detail?: str }
    HTTP 400 : passkey length not 8–9 characters
    HTTP 401 : missing / invalid JWT
    HTTP 500 : unexpected Supabase error
    """
    passkey = (body.passkey or "").strip()

    if len(passkey) < 8 or len(passkey) > 9:
        raise HTTPException(
            status_code=400,
            detail="Passkey must be exactly 8 or 9 characters.",
        )

    # ── Auth ──────────────────────────────────────────────────────────────────
    try:
        db, user_id, user_name, user_email = _get_user_from_token(request)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[PASSKEY] ⚠ Auth error: {e}")
        raise HTTPException(status_code=401, detail="Authentication required.")

    try:
        # ── Step 1: Check if user already has a grant ─────────────────────
        existing = (
            db.table("passkey_access")
            .select("id, passkey_used")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            print(f"[PASSKEY] ✅ Returning user={user_id[:8]}… already granted — skipping modal")
            return {"ok": True, "already_granted": True}

        # ── Step 2: Look up the passkey hash in admin_passkeys ────────────
        passkey_hash = hashlib.sha256(passkey.encode("utf-8")).hexdigest()
        pk_result = (
            db.table("admin_passkeys")
            .select("id, passkey")
            .eq("passkey_hash", passkey_hash)
            .limit(1)
            .execute()
        )

        if not pk_result.data:
            print(f"[PASSKEY] ❌ Access denied — wrong passkey from user={user_id[:8]}…")
            return {"ok": False}

        passkey_row = pk_result.data[0]
        passkey_id  = passkey_row["id"]

        # ── Step 3: Check whether this passkey is already claimed ─────────
        claimed = (
            db.table("passkey_access")
            .select("id, user_id")
            .eq("passkey_id", passkey_id)
            .limit(1)
            .execute()
        )
        if claimed.data and claimed.data[0]["user_id"] != user_id:
            print(
                f"[PASSKEY] ❌ Passkey already assigned to another user. "
                f"Denied for user={user_id[:8]}…"
            )
            return {
                "ok":     False,
                "detail": "already_assigned",
            }

        print(f"[PASSKEY] ✅ Passkey valid for user={user_id[:8]}… — awaiting grant call")
        return {"ok": True, "already_granted": False}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[PASSKEY] ⚠ Supabase error during verification: {e}")
        raise HTTPException(
            status_code=500,
            detail="Passkey verification failed due to a server error. Please try again.",
        )


# ══════════════════════════════════════════════════════════════════════════════
# POST /auth/passkey/grant
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/auth/passkey/grant")
async def passkey_grant(request: Request, body: PasskeyGrantRequest):
    """
    Claim a passkey for the authenticated user.
    Called by the frontend immediately after a successful verify-passkey
    (when already_granted is false).

    Stores: user_id, user_name, user_email, passkey_id (FK), passkey_used.

    Uses INSERT (not upsert) to let the DB UNIQUE(passkey_id) constraint
    reject a second user attempting to claim the same passkey concurrently.

    Auth    : Authorization: Bearer <jwt>  (required)
    Body    : { passkey: str }
    Returns : { "granted": true }
    HTTP 409 : passkey already claimed by another user (race condition)
    """
    passkey = (body.passkey or "").strip()
    if not passkey:
        raise HTTPException(status_code=400, detail="passkey is required.")

    db, user_id, user_name, user_email = _get_user_from_token(request)

    try:
        # Re-check: does this user already have a grant? (idempotency)
        existing = (
            db.table("passkey_access")
            .select("id")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            print(f"[PASSKEY] ℹ Grant already exists for user={user_id[:8]}…")
            return {"granted": True}

        # Resolve passkey_id from the submitted passkey
        passkey_hash = hashlib.sha256(passkey.encode("utf-8")).hexdigest()
        pk_result = (
            db.table("admin_passkeys")
            .select("id, passkey")
            .eq("passkey_hash", passkey_hash)
            .limit(1)
            .execute()
        )
        if not pk_result.data:
            raise HTTPException(status_code=400, detail="Invalid passkey.")

        passkey_row = pk_result.data[0]
        passkey_id  = passkey_row["id"]
        passkey_text = passkey_row["passkey"]

        # INSERT — the UNIQUE(user_id) and UNIQUE(passkey_id) constraints
        # in the DB prevent duplicate grants and concurrent claims atomically.
        db.table("passkey_access").insert({
            "user_id":      user_id,
            "user_name":    user_name,
            "user_email":   user_email,
            "passkey_id":   passkey_id,
            "passkey_used": passkey_text,
        }).execute()

        print(
            f"[PASSKEY] ✅ Grant recorded — user={user_id[:8]}… "
            f"email={user_email}  passkey_id={passkey_id}"
        )
        return {"granted": True}

    except HTTPException:
        raise
    except Exception as e:
        err_str = str(e)
        # PostgreSQL error code 23505 = unique_violation
        if "23505" in err_str or "duplicate" in err_str.lower():
            print(
                f"[PASSKEY] ❌ Concurrent claim rejected for user={user_id[:8]}… "
                f"— passkey already taken"
            )
            raise HTTPException(
                status_code=409,
                detail="This passkey has already been claimed by another user.",
            )
        print(f"[PASSKEY] ⚠ Grant insert failed: {e}")
        raise HTTPException(status_code=500, detail="Could not record access grant.")


# ══════════════════════════════════════════════════════════════════════════════
# GET /auth/passkey/check
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/auth/passkey/check")
async def passkey_check(request: Request):
    """
    Check whether the authenticated user already has passkey access.
    Called on page load to pre-populate grantedModes so the modal is skipped.

    Auth    : Authorization: Bearer <jwt>  (required)
    Returns : { "granted": true | false }
    """
    db, user_id, _name, _email = _get_user_from_token(request)

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
        # Fail closed — modal re-appears and user can verify again
        granted = False

    return {"granted": granted}


# ══════════════════════════════════════════════════════════════════════════════
# POST /admin/passkeys/add
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/admin/passkeys/add")
async def admin_add_passkey(request: Request, body: AdminAddPasskeyRequest):
    """
    Add a new passkey to the admin_passkeys table.

    Auth    : X-Admin-Token: <ADMIN_SECRET_TOKEN>  (required)
    Body    : { passkey: str, label?: str }
    Returns : { id, passkey, label, created_at }

    The passkey must be 8–9 characters. The SHA-256 hash is computed
    server-side and stored alongside the plain text.
    """
    _require_admin_token(request)
    from auth_utils import get_supabase  # pyrefly: ignore [missing-import]

    passkey = (body.passkey or "").strip()
    if len(passkey) < 8 or len(passkey) > 9:
        raise HTTPException(
            status_code=400,
            detail="Passkey must be exactly 8 or 9 characters.",
        )

    passkey_hash = hashlib.sha256(passkey.encode("utf-8")).hexdigest()

    try:
        db = get_supabase()
        result = db.table("admin_passkeys").insert({
            "passkey":      passkey,
            "passkey_hash": passkey_hash,
            "label":        body.label or None,
        }).execute()

        row = result.data[0] if result.data else {}
        print(f"[PASSKEY-ADMIN] ✅ New passkey added — label={body.label!r}")
        return {
            "id":         row.get("id"),
            "passkey":    row.get("passkey"),
            "label":      row.get("label"),
            "created_at": row.get("created_at"),
        }
    except Exception as e:
        err_str = str(e)
        if "23505" in err_str or "duplicate" in err_str.lower():
            raise HTTPException(
                status_code=409,
                detail="A passkey with that value already exists.",
            )
        print(f"[PASSKEY-ADMIN] ⚠ Insert failed: {e}")
        raise HTTPException(status_code=500, detail="Could not add passkey.")


# ══════════════════════════════════════════════════════════════════════════════
# GET /admin/passkeys/list
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/passkeys/list")
async def admin_list_passkeys(request: Request):
    """
    List all passkeys in admin_passkeys, with claim status.

    Auth    : X-Admin-Token: <ADMIN_SECRET_TOKEN>  (required)
    Returns : { passkeys: [ { id, passkey, label, created_at, claimed_by? } ] }
    """
    _require_admin_token(request)
    from auth_utils import get_supabase  # pyrefly: ignore [missing-import]

    try:
        db = get_supabase()

        # All passkeys
        pk_res = (
            db.table("admin_passkeys")
            .select("id, passkey, label, created_at")
            .order("created_at")
            .execute()
        )
        passkeys = pk_res.data or []

        # All grants keyed by passkey_id for claim status
        acc_res = (
            db.table("passkey_access")
            .select("passkey_id, user_id, user_name, user_email, granted_at")
            .execute()
        )
        claims = {row["passkey_id"]: row for row in (acc_res.data or []) if row.get("passkey_id")}

        result = []
        for pk in passkeys:
            pk_id   = pk["id"]
            entry   = {
                "id":         pk_id,
                "passkey":    pk["passkey"],
                "label":      pk.get("label"),
                "created_at": pk.get("created_at"),
                "claimed":    pk_id in claims,
            }
            if pk_id in claims:
                claim = claims[pk_id]
                entry["claimed_by"] = {
                    "user_id":    claim.get("user_id"),
                    "user_name":  claim.get("user_name"),
                    "user_email": claim.get("user_email"),
                    "granted_at": claim.get("granted_at"),
                }
            result.append(entry)

        print(f"[PASSKEY-ADMIN] Listed {len(result)} passkeys")
        return {"passkeys": result}

    except Exception as e:
        print(f"[PASSKEY-ADMIN] ⚠ List failed: {e}")
        raise HTTPException(status_code=500, detail="Could not retrieve passkeys.")
