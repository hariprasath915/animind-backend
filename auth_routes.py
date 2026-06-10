# auth_routes.py  —  Supabase Auth + User Profile Management
# ============================================================
# Changes from previous version:
#   - FIXED:   _anon_client() now reads env vars lazily (on each call),
#              not at import time → fixes "Failed to Fetch" on Render
#   - ADDED:   _upsert_user_profile() — writes to public.users on every
#              login / register so the app has a DB record per user
#   - ADDED:   GET  /auth/google           → returns Google OAuth URL
#   - ADDED:   GET  /auth/google/callback  → exchanges code → session
#   - ADDED:   POST /auth/logout           → revoke session
#   - UPDATED: AuthResponse includes dashboard_url so frontend can redirect
#
# public.users table schema (run once in Supabase SQL editor):
# -----------------------------------------------------------
# CREATE TABLE IF NOT EXISTS public.users (
#   id           UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
#   email        TEXT UNIQUE NOT NULL,
#   name         TEXT,
#   avatar_url   TEXT,
#   provider     TEXT DEFAULT 'email',
#   created_at   TIMESTAMPTZ DEFAULT now(),
#   last_login   TIMESTAMPTZ DEFAULT now()
# );
# ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
# CREATE POLICY "Users see own row" ON public.users
#   FOR ALL USING (auth.uid() = id);
# -----------------------------------------------------------

import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from supabase import create_client

from auth_utils import get_current_user, get_supabase

router = APIRouter(prefix="/auth", tags=["Authentication"])

# ── Frontend base URL (for OAuth redirect + dashboard URL) ─────────────
FRONTEND_URL = os.getenv(
    "FRONTEND_URL",
    "https://genzet-app.vercel.app",   # override in Render env if different
)

# ── Lazy env-var readers (NOT at import time) ──────────────────────────
# Reading at import time means the values are "" on Render because the
# environment is not yet populated when the module loads.
# Reading inside the function guarantees fresh values at request time.

def _anon_client():
    """Anon/public Supabase client — for sign-up & sign-in only."""
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_ANON_KEY", "")
    if not url or not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service not configured. Set SUPABASE_URL and SUPABASE_ANON_KEY.",
        )
    return create_client(url, key)


# ── DB: create / update user profile after every auth event ───────────
def _upsert_user_profile(
    user_id: str,
    email: str,
    name: str,
    avatar_url: str = "",
    provider: str = "email",
) -> None:
    """
    Write / refresh the user's row in public.users.
    Uses the service-role client so it bypasses RLS.
    Silently logs failures — auth still succeeds even if profile write fails.
    """
    try:
        sb = get_supabase()
        now = datetime.now(timezone.utc).isoformat()

        # Check if the row exists
        existing = (
            sb.table("users")
            .select("id")
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )

        if existing.data:
            # UPDATE — refresh last_login (and name / avatar if changed)
            sb.table("users").update({
                "last_login":  now,
                "name":        name or existing.data.get("name", ""),
                "avatar_url":  avatar_url or existing.data.get("avatar_url", ""),
            }).eq("id", user_id).execute()
            print(f"[AUTH] 🔄 Profile updated: {email}")
        else:
            # INSERT — first time this user appears in our DB
            sb.table("users").insert({
                "id":          user_id,
                "email":       email,
                "name":        name,
                "avatar_url":  avatar_url,
                "provider":    provider,
                "created_at":  now,
                "last_login":  now,
            }).execute()
            print(f"[AUTH] ✅ Profile created: {email} (provider={provider})")

    except Exception as exc:
        # Non-fatal — auth token is already valid; just log and move on
        print(f"[AUTH] ⚠ Profile upsert failed for {email}: {exc}")


# ══════════════════════════════════════════════════════════════════════
# SCHEMAS
# ══════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    email:    EmailStr
    password: str   = Field(..., min_length=6)
    name:     str   = Field(..., min_length=2, max_length=120)


class LoginRequest(BaseModel):
    email:    EmailStr
    password: str   = Field(..., min_length=1)


class AuthResponse(BaseModel):
    """Unified response for register and login."""
    token:         str   # Supabase access_token — store in localStorage
    user_id:       str   # auth.users.id (UUID)
    email:         str
    name:          str
    provider:      str   # "email" | "google"
    avatar_url:    str
    dashboard_url: str   # frontend should redirect here after login
    message:       str


class UserProfile(BaseModel):
    user_id:    str
    email:      str
    name:       str
    avatar_url: str
    provider:   str


# ══════════════════════════════════════════════════════════════════════
# POST /auth/register
# ══════════════════════════════════════════════════════════════════════

@router.post("/register", response_model=AuthResponse, status_code=201)
def register(body: RegisterRequest):
    """
    Create a new Supabase Auth user.
    Also creates a row in public.users so the rest of the app can
    JOIN against a real user table.
    Returns a Supabase JWT + dashboard_url for the frontend to redirect.
    """
    supabase = _anon_client()
    try:
        res = supabase.auth.sign_up({
            "email":    body.email.lower().strip(),
            "password": body.password,
            "options":  {"data": {"name": body.name.strip()}},
        })
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    if res.user is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Registration failed. Email may already be in use.",
        )

    token      = res.session.access_token if res.session else ""
    user_id    = res.user.id
    email      = res.user.email or body.email
    meta       = res.user.user_metadata or {}
    name       = meta.get("name", body.name)
    avatar_url = meta.get("avatar_url", "")

    # ── Create user profile in public.users ──
    _upsert_user_profile(
        user_id=user_id,
        email=email,
        name=name,
        avatar_url=avatar_url,
        provider="email",
    )

    print(f"[AUTH] ✅ Registered: {email} (user_id={user_id})")
    return AuthResponse(
        token=token,
        user_id=user_id,
        email=email,
        name=name,
        provider="email",
        avatar_url=avatar_url,
        dashboard_url=f"{FRONTEND_URL}/dashboard",
        message=f"Welcome to GenZet, {name}! 🎉",
    )


# ══════════════════════════════════════════════════════════════════════
# POST /auth/login
# ══════════════════════════════════════════════════════════════════════

@router.post("/login", response_model=AuthResponse)
def login(body: LoginRequest):
    """
    Sign in with email + password via Supabase Auth.
    Updates public.users.last_login on every successful login.
    Returns JWT + dashboard_url so the frontend can redirect.
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

    token      = res.session.access_token
    user_id    = res.user.id
    email      = res.user.email or ""
    meta       = res.user.user_metadata or {}
    name       = meta.get("name", email)
    avatar_url = meta.get("avatar_url", "")

    # ── Refresh / create user profile in public.users ──
    _upsert_user_profile(
        user_id=user_id,
        email=email,
        name=name,
        avatar_url=avatar_url,
        provider="email",
    )

    print(f"[AUTH] ✅ Login: {email} (user_id={user_id})")
    return AuthResponse(
        token=token,
        user_id=user_id,
        email=email,
        name=name,
        provider="email",
        avatar_url=avatar_url,
        dashboard_url=f"{FRONTEND_URL}/dashboard",
        message=f"Welcome back, {name}! 👋",
    )


# ══════════════════════════════════════════════════════════════════════
# POST /auth/logout
# ══════════════════════════════════════════════════════════════════════

@router.post("/logout")
def logout(current_user: dict = Depends(get_current_user)):
    """
    Revoke the current user session in Supabase.
    Frontend should also clear localStorage after calling this.
    """
    try:
        sb = _anon_client()
        sb.auth.sign_out()
    except Exception:
        pass   # ignore errors — frontend will clear token anyway
    email = current_user.get("email", "unknown")
    print(f"[AUTH] 👋 Logout: {email}")
    return {"message": "Logged out successfully.", "redirect": f"{FRONTEND_URL}/"}


# ══════════════════════════════════════════════════════════════════════
# GOOGLE OAUTH  — GET /auth/google
# ══════════════════════════════════════════════════════════════════════

@router.get("/google")
def google_login(redirect_to: Optional[str] = None):
    """
    Step 1 of Google OAuth:
      Frontend calls GET /auth/google  → gets a Google sign-in URL
      Frontend redirects browser to that URL
      Google authenticates the user
      Google calls back to Supabase's callback URL
      Supabase redirects the browser to redirect_to (your frontend /auth/callback page)
      Frontend /auth/callback page calls GET /auth/google/finish?code=...

    Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in Supabase Dashboard
    → Authentication → Providers → Google before using this endpoint.
    """
    callback = redirect_to or f"{FRONTEND_URL}/auth/callback"
    supabase = _anon_client()
    try:
        res = supabase.auth.sign_in_with_oauth({
            "provider": "google",
            "options": {
                "redirect_to": callback,
                "query_params": {
                    "access_type": "offline",
                    "prompt": "consent",
                },
            },
        })
        return {
            "url":         res.url,
            "provider":    "google",
            "redirect_to": callback,
        }
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Google OAuth not configured in Supabase: {exc}",
        )


# ══════════════════════════════════════════════════════════════════════
# GOOGLE CALLBACK  — POST /auth/google/callback
# ══════════════════════════════════════════════════════════════════════

class OAuthCallbackRequest(BaseModel):
    """
    After Supabase redirects back to the frontend /auth/callback page,
    the frontend extracts the `access_token` and `refresh_token` from the URL
    hash (#access_token=...&refresh_token=...) and posts them here to get
    a full AuthResponse (with dashboard_url to redirect to).
    """
    access_token:  str
    refresh_token: Optional[str] = ""


@router.post("/google/callback", response_model=AuthResponse)
def google_callback(body: OAuthCallbackRequest):
    """
    Step 2 of Google OAuth:
    Frontend /auth/callback page posts the Supabase token here.
    Backend verifies the token, upserts the user profile, and returns
    AuthResponse with dashboard_url so the frontend can redirect.
    """
    supabase = _anon_client()
    try:
        # Exchange the OAuth tokens to get the user session
        res = supabase.auth.set_session(
            body.access_token,
            body.refresh_token or "",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid OAuth token: {exc}",
        )

    if not res or res.user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not retrieve user from Google OAuth token.",
        )

    user       = res.user
    session    = res.session
    token      = session.access_token if session else body.access_token
    user_id    = user.id
    email      = user.email or ""
    meta       = user.user_metadata or {}
    name       = meta.get("full_name") or meta.get("name") or email
    avatar_url = meta.get("avatar_url") or meta.get("picture", "")

    # ── Create / refresh user profile in public.users ──
    _upsert_user_profile(
        user_id=user_id,
        email=email,
        name=name,
        avatar_url=avatar_url,
        provider="google",
    )

    print(f"[AUTH] ✅ Google OAuth: {email} (user_id={user_id})")
    return AuthResponse(
        token=token,
        user_id=user_id,
        email=email,
        name=name,
        provider="google",
        avatar_url=avatar_url,
        dashboard_url=f"{FRONTEND_URL}/dashboard",
        message=f"Welcome, {name}! 🎉",
    )


# ══════════════════════════════════════════════════════════════════════
# GET /auth/verify
# ══════════════════════════════════════════════════════════════════════

@router.get("/verify", response_model=UserProfile)
def verify_token(current_user: dict = Depends(get_current_user)):
    """
    Validate an existing JWT (used for auto-login on page load).
    Returns 200 + profile if valid; 401 if expired / invalid.
    """
    return UserProfile(
        user_id=current_user["id"],
        email=current_user["email"],
        name=current_user.get("name", ""),
        avatar_url=current_user.get("avatar_url", ""),
        provider=current_user.get("app_metadata", {}).get("provider", "email"),
    )


# ══════════════════════════════════════════════════════════════════════
# GET /auth/me
# ══════════════════════════════════════════════════════════════════════

@router.get("/me", response_model=UserProfile)
def get_me(current_user: dict = Depends(get_current_user)):
    """Return the authenticated user's full profile from public.users."""
    user_id = current_user["id"]
    try:
        sb  = get_supabase()
        row = (
            sb.table("users")
            .select("*")
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )
        if row.data:
            return UserProfile(
                user_id=row.data["id"],
                email=row.data["email"],
                name=row.data.get("name", ""),
                avatar_url=row.data.get("avatar_url", ""),
                provider=row.data.get("provider", "email"),
            )
    except Exception as exc:
        print(f"[AUTH] ⚠ /me DB lookup failed for {user_id}: {exc}")

    # Fall back to JWT payload if DB lookup fails
    return UserProfile(
        user_id=user_id,
        email=current_user["email"],
        name=current_user.get("name", ""),
        avatar_url=current_user.get("avatar_url", ""),
        provider=current_user.get("app_metadata", {}).get("provider", "email"),
    )
