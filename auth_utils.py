"""
auth_utils.py — Supabase JWT verification for GenZet / Animind
==============================================================
Location: backend/auth_utils.py

Responsibilities:
  - Verify Supabase-issued JWTs (HS256, signed with SUPABASE_JWT_SECRET)
  - Extract user identity (id, email, name) from the verified token
  - Provide a service-role Supabase client for data operations
  - FastAPI dependency: get_current_user() — extracts user dict from
    Authorization: Bearer <token> header on every protected route

What changed from v4.x (SQLAlchemy version):
  - REMOVED: bcrypt hashing, local JWT creation, DB session dependency,
             SQLAlchemy User model lookup on every request
  - ADDED:   Supabase JWT verification using SUPABASE_JWT_SECRET,
             get_supabase() returning a service-role client for DB ops
  - KEPT:    Same get_current_user() FastAPI dependency signature —
             all routes that do `Depends(get_current_user)` work unchanged,
             except the return value is now a plain dict instead of models.User.

JWT flow:
  Browser → POST /auth/login → Supabase issues access_token (JWT)
  Browser → any protected route → Authorization: Bearer <access_token>
  Backend → decode_supabase_token() verifies signature + expiry
  Backend → injects { id, email, name } dict into route handler

user_id = current_user["id"] is always auth.users.id (UUID).
This is the foreign key used in all `contents` table rows.
"""

import os
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client, Client

# ── Configuration — load from environment ─────────────────────────────
# Render Dashboard env var names (what the user set):
#   SUPABASE_URL     — https://<project>.supabase.co
#   SUPABASE_KEY     — the Supabase key (used as anon key AND service key)
#   JWT_KEY          — Supabase JWT Secret (Settings → API → JWT Secret)
#
# Full canonical names also accepted:
#   SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY, SUPABASE_JWT_SECRET

_SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")   # user's Render var name

SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY    = os.getenv("SUPABASE_ANON_KEY")    or _SUPABASE_KEY
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or _SUPABASE_KEY
# JWT_KEY is the name the user added in Render; SUPABASE_JWT_SECRET is canonical
SUPABASE_JWT_SECRET  = os.getenv("SUPABASE_JWT_SECRET")  or os.getenv("JWT_KEY", "")

ALGORITHM = "HS256"   # Supabase signs JWTs with HS256 by default

# Startup diagnostics
if not SUPABASE_URL:         print("[AUTH] ⚠  SUPABASE_URL not set!")
if not SUPABASE_ANON_KEY:    print("[AUTH] ⚠  SUPABASE_KEY / SUPABASE_ANON_KEY not set!")
if not SUPABASE_SERVICE_KEY: print("[AUTH] ⚠  SUPABASE_KEY / SUPABASE_SERVICE_KEY not set!")
if not SUPABASE_JWT_SECRET:  print("[AUTH] ⚠  JWT_KEY / SUPABASE_JWT_SECRET not set!")
if SUPABASE_URL and SUPABASE_ANON_KEY:
    print(f"[AUTH] ✅ Supabase configured → {SUPABASE_URL[:40]}...")

# ── Bearer token extractor ─────────────────────────────────────────────
bearer_scheme = HTTPBearer(auto_error=False)


# ══════════════════════════════════════════════════════════════════════
# SUPABASE SERVICE CLIENT  (service-role key — never sent to browser)
# ══════════════════════════════════════════════════════════════════════

def get_supabase(token: Optional[str] = None) -> Client:
    """
    Return a Supabase client authenticated with the service-role key.
    If a token is provided, it acts as the user, securely enforcing RLS.
    """
    from supabase import ClientOptions
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in environment."
        )
    opts = ClientOptions()
    if token:
        opts.headers.update({"Authorization": f"Bearer {token}"})
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY, options=opts)


# ══════════════════════════════════════════════════════════════════════
# JWT VERIFICATION  (Supabase-issued tokens)
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# FASTAPI DEPENDENCY — PROTECTED ROUTES
# ══════════════════════════════════════════════════════════════════════

def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict:
    """
    FastAPI dependency that verifies the Supabase JWT from the Authorization
    header and returns the authenticated user as a plain dict.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    try:
        supabase = get_supabase()
        res = supabase.auth.get_user(token)
        user = res.user
        if not user:
            raise Exception("No user returned")
    except Exception as e:
        print(f"[AUTH] Token verification failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is invalid or has expired. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = str(user.id)
    email = user.email or ""
    meta = user.user_metadata or {}
    
    name = (
        meta.get("name")
        or meta.get("full_name")
        or email
    )

    return {"id": user_id, "email": email, "name": name, "token": token}


# ══════════════════════════════════════════════════════════════════════
# LEGACY STUBS — kept so older imports don't break during transition
# These were used in the SQLAlchemy v4.x version of auth_utils.py.
# They raise clear errors if accidentally called.
# ══════════════════════════════════════════════════════════════════════

def hash_password(plain_password: str) -> str:
    """DEPRECATED — passwords are now managed by Supabase Auth."""
    raise NotImplementedError(
        "hash_password() is no longer used. "
        "Password hashing is handled by Supabase Auth (supabase.auth.sign_up)."
    )


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """DEPRECATED — password verification is now handled by Supabase Auth."""
    raise NotImplementedError(
        "verify_password() is no longer used. "
        "Use supabase.auth.sign_in_with_password() instead."
    )


def create_access_token(user_id: str, email: str, name: str) -> str:
    """DEPRECATED — JWTs are now issued by Supabase Auth, not the backend."""
    raise NotImplementedError(
        "create_access_token() is no longer used. "
        "Supabase Auth issues the JWT on sign-in. "
        "The backend only VERIFIES tokens, not creates them."
    )
