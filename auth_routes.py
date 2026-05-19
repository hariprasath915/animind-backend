"""
auth_routes.py — Registration, Login, Token Verification
=========================================================
Location: backend/auth_routes.py

Endpoints:
  POST /auth/register   — create a new teacher account
  POST /auth/login      — verify credentials, return JWT
  GET  /auth/verify     — validate existing JWT (auto-login check)
  GET  /auth/me         — return current user profile

All endpoints return a consistent JSON shape so the frontend
can handle them uniformly.
"""

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
import uuid

from database import get_db
import models
from auth_utils import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])


# ══════════════════════════════════════════════════════════════════════
# REQUEST / RESPONSE SCHEMAS
# ══════════════════════════════════════════════════════════════════════

class RegisterRequest(BaseModel):
    name:     str   = Field(..., min_length=2, max_length=120, description="Teacher's full name")
    email:    EmailStr
    password: str   = Field(..., min_length=6, max_length=128, description="Minimum 6 characters")


class LoginRequest(BaseModel):
    email:    EmailStr
    password: str   = Field(..., min_length=1)


class AuthResponse(BaseModel):
    """Unified response for register and login."""
    token:   str
    user_id: str
    email:   str
    name:    str
    message: str


class UserProfile(BaseModel):
    user_id: str
    email:   str
    name:    str


# ══════════════════════════════════════════════════════════════════════
# POST /auth/register
# ══════════════════════════════════════════════════════════════════════

@router.post("/register", response_model=AuthResponse, status_code=201)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """
    Register a new teacher account.

    Steps:
      1. Check email is not already in use
      2. Hash the password with bcrypt
      3. Create and persist the User row
      4. Return a fresh JWT + user identity

    Frontend stores the JWT in localStorage immediately on success.
    """
    # ── 1. Check for existing account ───────────────────────────────
    existing = db.query(models.User).filter(
        models.User.email == body.email.lower().strip()
    ).first()

    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists. Please log in.",
        )

    # ── 2. Create user with hashed password ─────────────────────────
    new_user = models.User(
        id            = str(uuid.uuid4()),
        name          = body.name.strip(),
        email         = body.email.lower().strip(),
        password_hash = hash_password(body.password),
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    print(f"[AUTH]  ✅ Registered: {new_user.email} (id={new_user.id})")

    # ── 3. Generate JWT ──────────────────────────────────────────────
    token = create_access_token(
        user_id = new_user.id,
        email   = new_user.email,
        name    = new_user.name,
    )

    return AuthResponse(
        token   = token,
        user_id = new_user.id,
        email   = new_user.email,
        name    = new_user.name,
        message = f"Welcome to GenZet, {new_user.name}!",
    )


# ══════════════════════════════════════════════════════════════════════
# POST /auth/login
# ══════════════════════════════════════════════════════════════════════

@router.post("/login", response_model=AuthResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """
    Authenticate a teacher and return a fresh JWT.

    Steps:
      1. Look up user by email
      2. Verify bcrypt password hash
      3. Return fresh JWT + user identity

    Frontend:
      - Stores JWT in localStorage
      - Calls GET /sync/animations to restore library
    """
    # ── 1. Find user ─────────────────────────────────────────────────
    user = db.query(models.User).filter(
        models.User.email == body.email.lower().strip()
    ).first()

    # ── 2. Verify credentials (same error message for both cases) ────
    # Never reveal whether the email exists or not — security best practice
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been disabled.",
        )

    print(f"[AUTH]  ✅ Login: {user.email} (id={user.id})")

    # ── 3. Issue fresh JWT ───────────────────────────────────────────
    token = create_access_token(
        user_id = user.id,
        email   = user.email,
        name    = user.name,
    )

    return AuthResponse(
        token   = token,
        user_id = user.id,
        email   = user.email,
        name    = user.name,
        message = f"Welcome back, {user.name}!",
    )


# ══════════════════════════════════════════════════════════════════════
# GET /auth/verify
# ══════════════════════════════════════════════════════════════════════

@router.get("/verify", response_model=UserProfile)
def verify_token(current_user: models.User = Depends(get_current_user)):
    """
    Validate an existing JWT token (for auto-login on app load).

    Frontend calls this on every page load:
      - If 200  → user is still authenticated, skip login screen
      - If 401  → token expired or invalid, show login screen

    Returns the current user's profile (no new token issued).
    """
    return UserProfile(
        user_id = current_user.id,
        email   = current_user.email,
        name    = current_user.name,
    )


# ══════════════════════════════════════════════════════════════════════
# GET /auth/me
# ══════════════════════════════════════════════════════════════════════

@router.get("/me", response_model=UserProfile)
def get_me(current_user: models.User = Depends(get_current_user)):
    """Return the authenticated user's profile."""
    return UserProfile(
        user_id = current_user.id,
        email   = current_user.email,
        name    = current_user.name,
    )