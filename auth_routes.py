# auth_routes.py  —  Thin wrapper around Supabase Auth
# ======================================================
# What changed from v4.x:
#   - Removed: SQLAlchemy db queries, bcrypt hashing, local JWT issuance
#   - Added:   Delegates register → supabase.auth.sign_up()
#                                   login    → supabase.auth.sign_in_with_password()
#   - Kept:    Same route paths, same AuthResponse/UserProfile schemas,
#              same /verify and /me dependency patterns.
#
# The backend no longer stores passwords or issues JWTs.
# Supabase Auth is the single source of truth for users.

import os
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, EmailStr, Field
from supabase import create_client

from auth_utils import get_current_user

router = APIRouter(prefix="/auth", tags=["Authentication"])

SUPABASE_URL      = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")


def _anon_client():
    """Anon client for auth operations (sign-up, sign-in)."""
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


# ── Schemas ────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email:    EmailStr
    password: str = Field(..., min_length=6)
    name:     str = Field(..., min_length=2, max_length=120)


class LoginRequest(BaseModel):
    email:    EmailStr
    password: str = Field(..., min_length=1)


class AuthResponse(BaseModel):
    """Unified response for register and login — same shape as before."""
    token:   str    # Supabase access_token — frontend stores in localStorage
    user_id: str    # auth.users.id (UUID)
    email:   str
    name:    str
    message: str


class UserProfile(BaseModel):
    user_id: str
    email:   str
    name:    str


# ── POST /auth/register ────────────────────────────────────────────────

@router.post("/register", response_model=AuthResponse, status_code=201)
def register(body: RegisterRequest):
    """
    Create a new Supabase Auth user.
    On success, returns the Supabase JWT (access_token).
    The frontend stores this token and sends it as Bearer on every request.
    user_id = auth.users.id is the UUID that keys all content in Supabase.
    """
    supabase = _anon_client()
    try:
        res = supabase.auth.sign_up({
            "email":    body.email.lower().strip(),
            "password": body.password,
            "options":  {"data": {"name": body.name.strip()}},
        })
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if res.user is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Registration failed. Email may already be in use.",
        )

    token   = res.session.access_token if res.session else ""
    # user_id flows from Supabase → token payload → backend → DB
    user_id = res.user.id
    email   = res.user.email or body.email
    name    = (res.user.user_metadata or {}).get("name", body.name)

    print(f"[AUTH] ✅ Registered: {email} (user_id={user_id})")
    return AuthResponse(
        token=token, user_id=user_id, email=email, name=name,
        message=f"Welcome to GenZet, {name}!",
    )


# ── POST /auth/login ───────────────────────────────────────────────────

@router.post("/login", response_model=AuthResponse)
def login(body: LoginRequest):
    """
    Sign in with email + password via Supabase Auth.
    Returns a fresh Supabase JWT. user_id = auth.users.id.
    """
    supabase = _anon_client()
    try:
        res = supabase.auth.sign_in_with_password({
            "email":    body.email.lower().strip(),
            "password": body.password,
        })
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )

    if res.user is None or res.session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )

    token   = res.session.access_token
    user_id = res.user.id      # ← auth.users.id — the key for all content
    email   = res.user.email or ""
    name    = (res.user.user_metadata or {}).get("name", email)

    print(f"[AUTH] ✅ Login: {email} (user_id={user_id})")
    return AuthResponse(
        token=token, user_id=user_id, email=email, name=name,
        message=f"Welcome back, {name}!",
    )


# ── GET /auth/verify ───────────────────────────────────────────────────

@router.get("/verify", response_model=UserProfile)
def verify_token(current_user: dict = Depends(get_current_user)):
    """
    Validate existing JWT (for auto-login on page load).
    Returns 200 + profile if valid, 401 if expired/invalid.
    """
    return UserProfile(
        user_id=current_user["id"],
        email=current_user["email"],
        name=current_user.get("name", ""),
    )


# ── GET /auth/me ───────────────────────────────────────────────────────

@router.get("/me", response_model=UserProfile)
def get_me(current_user: dict = Depends(get_current_user)):
    """Return the authenticated user's profile."""
    return UserProfile(
        user_id=current_user["id"],
        email=current_user["email"],
        name=current_user.get("name", ""),
    )
