# auth_routes.py  —  GenZet / Animind  v5.2
# ============================================================
#
# Changes in v5.2
# ---------------
# Feature 1 — Gmail-based account with persistent user_id
#   * POST /auth/register  now returns a FULL AuthResponse (token + user_id).
#     Previously it returned RegisterResponse with no token, forcing users to
#     log in a second time.  Now register → auto-login in one step.
#   * The user_id is auth.users.id (UUID) assigned by Supabase — it is
#     deterministically tied to the email address, so logging in with the
#     same Gmail on any device always resolves to the same UUID and the
#     same data rows in public.contents.
#   * _upsert_user_profile() is called on every register AND login so
#     public.users always has a fresh row matching auth.users.
#   * GET /auth/verify returns user_id in the response so the frontend
#     can restore the session without a second /me call.
#
# Feature 2 — Save HTML to Supabase library
#   * No changes needed in auth_routes; saving is handled by sync_routes.
#   * AuthResponse.user_id is now surfaced so the frontend can attach it
#     to every /sync/animations POST.
#
# Unchanged
# ---------
#   * POST /auth/login          (unchanged except logging)
#   * POST /auth/logout
#   * GET  /auth/google
#   * POST /auth/google/callback
#   * GET  /auth/me
#   * All Pydantic schemas (AuthResponse extended, RegisterResponse removed)
# ============================================================

import os
import base64
import hashlib
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, EmailStr, Field
from supabase import create_client

from auth_utils import get_current_user, get_supabase

router = APIRouter(prefix="/auth", tags=["Authentication"])

# Frontend URL — used for OAuth redirect and dashboard_url in responses
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://haezet.com")

# Google OAuth callback page — where Supabase redirects after Google sign-in.
# Must be listed as an "Authorized redirect URI" in Google Cloud Console AND
# configured as a "Redirect URL" in Supabase Dashboard → Authentication →
# URL Configuration.  Override via env var for custom deployments.
GOOGLE_OAUTH_CALLBACK_URL = os.getenv(
    "GOOGLE_OAUTH_CALLBACK_URL",
    f"{FRONTEND_URL}/oauth_callback.html",
)


# ── Lazy Supabase anon client (reads env vars at call time, not import) ──────
def _anon_client():
    """Public/anon Supabase client — sign-up and sign-in only."""
    url = os.getenv("SUPABASE_URL", "")
    # Accept SUPABASE_KEY (Render env var name) as fallback for SUPABASE_ANON_KEY
    key = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_KEY", "")
    if not url or not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service not configured. Set SUPABASE_URL and SUPABASE_KEY in Render.",
        )
    return create_client(url, key)


# ── Write / refresh the user profile row in public.users ─────────────────────
def _upsert_user_profile(
    user_id: str,
    email: str,
    name: str,
    avatar_url: str = "",
    provider: str = "email",
) -> None:
    """
    Ensure public.users has a row for this Supabase Auth user.
    Called on every register and login — idempotent.
    Uses the service-role client (bypasses RLS).
    Failures are non-fatal: the JWT is already valid even if this fails.
    """
    try:
        sb  = get_supabase()
        now = datetime.now(timezone.utc).isoformat()

        existing = (
            sb.table("users")
            .select("id, name, avatar_url")
            .eq("id", user_id)
            .maybe_single()
            .execute()
        )

        if existing.data:
            sb.table("users").update({
                "last_login": now,
                # Only overwrite name/avatar if the new value is non-empty
                "name":       name       or existing.data.get("name", ""),
                "avatar_url": avatar_url or existing.data.get("avatar_url", ""),
            }).eq("id", user_id).execute()
            print(f"[AUTH] 🔄 Profile refreshed: {email} (id={user_id})")
        else:
            sb.table("users").insert({
                "id":         user_id,
                "email":      email,
                "name":       name,
                "avatar_url": avatar_url,
                "provider":   provider,
                "created_at": now,
                "last_login": now,
            }).execute()
            print(f"[AUTH] ✅ Profile created: {email} (id={user_id}, provider={provider})")

    except Exception as exc:
        print(f"[AUTH] ⚠ Profile upsert failed for {email}: {exc}")


# ══════════════════════════════════════════════════════════════════════
# SCHEMAS
# ══════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    email:    EmailStr
    password: str = Field(..., min_length=6)
    name:     str = Field(..., min_length=2, max_length=120)


class LoginRequest(BaseModel):
    email:    EmailStr
    password: str = Field(..., min_length=1)


class AuthResponse(BaseModel):
    """
    Returned after a successful REGISTER or LOGIN.

    user_id is auth.users.id — the stable UUID tied to the email address.
    It is identical across all devices and sessions for the same account.
    The frontend must store both `token` and `user_id` in localStorage.
    """
    token:         str   # Supabase JWT — store in localStorage as genzet_jwt
    user_id:       str   # auth.users.id UUID — persistent, cross-device identity
    email:         str
    name:          str
    provider:      str   # "email" | "google"
    avatar_url:    str
    is_new_user:   bool  # True on first registration, False on subsequent logins
    dashboard_url: str
    message:       str


class UserProfile(BaseModel):
    user_id:    str
    email:      str
    name:       str
    avatar_url: str
    provider:   str


class OAuthCallbackRequest(BaseModel):
    access_token:  str
    refresh_token: Optional[str] = ""


# ══════════════════════════════════════════════════════════════════════
# POST /auth/register
# ══════════════════════════════════════════════════════════════════════

@router.post("/register", response_model=AuthResponse, status_code=201)
def register(body: RegisterRequest):
    """
    Create a new account and immediately return a session token.

    Flow (v5.2 — one round-trip instead of two):
      1. POST /auth/register  →  201 + AuthResponse (token + user_id)
      2. Frontend stores token + user_id in localStorage
      3. Frontend goes directly to dashboard — no second login needed

    The user_id in the response is auth.users.id (Supabase UUID).
    It is permanent, tied to the email, and identical on every device.
    All saved animations in public.contents reference this user_id.
    """
    supabase = _anon_client()

    # ── Create the Supabase Auth user ────────────────────────────────
    try:
        res = supabase.auth.sign_up({
            "email":   body.email.lower().strip(),
            "password": body.password,
            "options": {"data": {"name": body.name.strip()}},
        })
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    if res.user is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Registration failed. This email may already be registered.",
        )

    user_id    = str(res.user.id)
    email      = res.user.email or body.email
    meta       = res.user.user_metadata or {}
    name       = meta.get("name", body.name)
    avatar_url = meta.get("avatar_url", "")

    # ── Extract the session token Supabase issues on sign_up ─────────
    # Supabase returns a session immediately when email confirmation is
    # disabled (the common setup for non-production apps).
    # If email confirmation IS enabled, session will be None — we then
    # do an explicit sign-in to get a token for the frontend.
    token: str = ""
    if res.session:
        token = res.session.access_token
    else:
        # Email confirmation is enabled — do a silent sign-in to get token.
        try:
            login_res = supabase.auth.sign_in_with_password({
                "email":    email,
                "password": body.password,
            })
            if login_res.session:
                token = login_res.session.access_token
        except Exception:
            pass  # token stays empty; frontend will use the login form

    # ── Write public.users profile row ───────────────────────────────
    _upsert_user_profile(
        user_id=user_id,
        email=email,
        name=name,
        avatar_url=avatar_url,
        provider="email",
    )

    print(f"[AUTH] ✅ Registered: {email} (user_id={user_id}, has_token={bool(token)})")

    return AuthResponse(
        token=token,
        user_id=user_id,
        email=email,
        name=name,
        provider="email",
        avatar_url=avatar_url,
        is_new_user=True,
        dashboard_url=f"{FRONTEND_URL}/dashboard",
        message=f"Welcome to GenZet, {name}! Your account is ready.",
    )


# ══════════════════════════════════════════════════════════════════════
# POST /auth/login
# ══════════════════════════════════════════════════════════════════════

@router.post("/login", response_model=AuthResponse)
def login(body: LoginRequest):
    """
    Sign in with email + password.

    The returned user_id is auth.users.id — identical on every device.
    Logging in with the same Gmail on a new device returns the same
    user_id and therefore the same animation library in public.contents.
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
    user_id    = str(res.user.id)
    email      = res.user.email or ""
    meta       = res.user.user_metadata or {}
    name       = meta.get("name", email)
    avatar_url = meta.get("avatar_url", "")

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
        is_new_user=False,
        dashboard_url=f"{FRONTEND_URL}/dashboard",
        message=f"Welcome back, {name}!",
    )


# ══════════════════════════════════════════════════════════════════════
# POST /auth/logout
# ══════════════════════════════════════════════════════════════════════

@router.post("/logout")
def logout(current_user: dict = Depends(get_current_user)):
    """Revoke the session. Frontend clears localStorage after this."""
    try:
        _anon_client().auth.sign_out()
    except Exception:
        pass
    print(f"[AUTH] 👋 Logout: {current_user.get('email', 'unknown')}")
    return {"message": "Logged out successfully.", "redirect": f"{FRONTEND_URL}/"}


# ══════════════════════════════════════════════════════════════════════
# GET /auth/verify
# ══════════════════════════════════════════════════════════════════════

@router.get("/verify", response_model=UserProfile)
def verify_token(current_user: dict = Depends(get_current_user)):
    """
    Validate an existing JWT (called on every page load for auto-login).
    Returns the user profile extracted from the verified token.
    The user_id here is auth.users.id — same UUID on every device.
    """
    return UserProfile(
        user_id=current_user["id"],
        email=current_user["email"],
        name=current_user.get("name", ""),
        avatar_url=current_user.get("avatar_url", ""),
        provider=(current_user.get("app_metadata") or {}).get("provider", "email"),
    )


# ══════════════════════════════════════════════════════════════════════
# GET /auth/me
# ══════════════════════════════════════════════════════════════════════

@router.get("/me", response_model=UserProfile)
def get_me(current_user: dict = Depends(get_current_user)):
    """Return the full profile from public.users (DB source of truth)."""
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

    # Fallback to JWT payload
    return UserProfile(
        user_id=user_id,
        email=current_user["email"],
        name=current_user.get("name", ""),
        avatar_url=current_user.get("avatar_url", ""),
        provider=(current_user.get("app_metadata") or {}).get("provider", "email"),
    )


# ══════════════════════════════════════════════════════════════════════
# GOOGLE OAUTH
# ══════════════════════════════════════════════════════════════════════

@router.get("/google")
def google_login(redirect_to: Optional[str] = None):
    """
    Return the Google OAuth sign-in URL with manual PKCE flow.

    Since the Supabase client hides the generated code_verifier in its own memory
    (which is lost between requests in a stateless API), we generate the PKCE 
    challenge and verifier manually here.
    
    We return the `verifier` to the frontend. The frontend stores it in localStorage,
    redirects the browser to the `url`, and later sends the `verifier` back to us 
    during the code exchange step.
    """
    callback = redirect_to or GOOGLE_OAUTH_CALLBACK_URL
    supabase_url = os.getenv("SUPABASE_URL")
    
    if not supabase_url:
        raise HTTPException(status_code=500, detail="SUPABASE_URL not configured")

    # 1. Generate PKCE verifier and challenge
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip('=')
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode('utf-8')).digest()
    ).decode('utf-8').rstrip('=')

    # 2. Build the authorize URL
    query_params = {
        "provider": "google",
        "redirect_to": callback,
        "code_challenge": challenge,
        "code_challenge_method": "s256",
        "access_type": "offline",
        "prompt": "consent"
    }
    
    auth_url = f"{supabase_url}/auth/v1/authorize?{urlencode(query_params)}"

    return {
        "url": auth_url, 
        "provider": "google", 
        "redirect_to": callback,
        "verifier": verifier  # Frontend must save this!
    }



@router.post("/google/callback", response_model=AuthResponse)
def google_callback(body: OAuthCallbackRequest):
    """
    Exchange OAuth tokens for a session.
    user_id will be the same stable UUID for this Google account
    on every device — so animations sync cross-device automatically.
    """
    supabase = _anon_client()
    try:
        res = supabase.auth.set_session(body.access_token, body.refresh_token or "")
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
    user_id    = str(user.id)
    email      = user.email or ""
    meta       = user.user_metadata or {}
    name       = meta.get("full_name") or meta.get("name") or email
    avatar_url = meta.get("avatar_url") or meta.get("picture", "")

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
        is_new_user=False,
        dashboard_url=f"{FRONTEND_URL}/dashboard",
        message=f"Welcome, {name}!",
    )


class ExchangeCodeRequest(BaseModel):
    code: str
    verifier: str


@router.post("/google/exchange-code", response_model=AuthResponse)
def google_exchange_code(body: ExchangeCodeRequest):
    """
    Exchange a Supabase PKCE auth code for a session.
    """
    supabase = _anon_client()
    try:
        res = supabase.auth.exchange_code_for_session({
            "auth_code": body.code,
            "code_verifier": body.verifier
        })
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"PKCE code exchange failed: {exc}",
        )

    if not res or res.user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not exchange auth code for session.",
        )

    user       = res.user
    session    = res.session
    token      = session.access_token if session else ""
    user_id    = str(user.id)
    email      = user.email or ""
    meta       = user.user_metadata or {}
    name       = meta.get("full_name") or meta.get("name") or email
    avatar_url = meta.get("avatar_url") or meta.get("picture", "")

    _upsert_user_profile(
        user_id=user_id,
        email=email,
        name=name,
        avatar_url=avatar_url,
        provider="google",
    )

    print(f"[AUTH] ✅ Google PKCE exchange: {email} (user_id={user_id})")
    return AuthResponse(
        token=token,
        user_id=user_id,
        email=email,
        name=name,
        provider="google",
        avatar_url=avatar_url,
        is_new_user=False,
        dashboard_url=f"{FRONTEND_URL}/dashboard",
        message=f"Welcome, {name}!",
    )
