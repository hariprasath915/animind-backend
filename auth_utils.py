"""
auth_utils.py — JWT + bcrypt utilities for GenZet
===================================================
Location: backend/auth_utils.py

Responsibilities:
  - Hash passwords with bcrypt (never store plain text)
  - Verify password hashes on login
  - Create JWT access tokens signed with HS256
  - Decode and validate JWT tokens
  - FastAPI dependency: get_current_user() — extracts user from Authorization header
"""

import os
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from database import get_db
import models

# ── Configuration (loaded from .env) ──────────────────────────────────
SECRET_KEY      = os.getenv("JWT_SECRET_KEY", "CHANGE_THIS_TO_A_LONG_RANDOM_STRING_IN_PRODUCTION")
ALGORITHM       = "HS256"
TOKEN_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "30"))  # 30-day tokens

# ── bcrypt context ─────────────────────────────────────────────────────
# bcrypt is the gold standard for password hashing:
# - Adaptive cost factor (slow by design — defeats brute force)
# - Built-in salt (no rainbow table attacks)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── Bearer token extractor ─────────────────────────────────────────────
bearer_scheme = HTTPBearer(auto_error=False)


# ══════════════════════════════════════════════════════════════════════
# PASSWORD HASHING
# ══════════════════════════════════════════════════════════════════════

def hash_password(plain_password: str) -> str:
    """
    Hash a plain-text password using bcrypt.
    Returns a 60-character bcrypt hash string.

    Example:
        stored_hash = hash_password("MySecretPass123")
    """
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plain-text password against its stored bcrypt hash.
    Returns True if they match, False otherwise.

    Example:
        is_valid = verify_password("MySecretPass123", stored_hash)
    """
    return pwd_context.verify(plain_password, hashed_password)


# ══════════════════════════════════════════════════════════════════════
# JWT TOKEN CREATION & VERIFICATION
# ══════════════════════════════════════════════════════════════════════

def create_access_token(user_id: str, email: str, name: str) -> str:
    """
    Create a signed JWT token containing user identity.

    Payload:
      sub   — user_id (subject, used to fetch user from DB)
      email — user email (for display purposes)
      name  — user display name
      exp   — expiry timestamp (TOKEN_EXPIRE_DAYS from now)

    Returns a compact JWT string safe to store in localStorage.
    """
    expire = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    payload = {
        "sub":   user_id,
        "email": email,
        "name":  name,
        "exp":   expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """
    Decode and validate a JWT token.

    Returns the payload dict on success, or None if invalid/expired.

    The payload contains:
      {
        "sub":   "<user_id>",
        "email": "<email>",
        "name":  "<name>",
        "exp":   <timestamp>
      }
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


# ══════════════════════════════════════════════════════════════════════
# FASTAPI DEPENDENCY — PROTECTED ROUTES
# ══════════════════════════════════════════════════════════════════════

def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    """
    FastAPI dependency that extracts and validates the JWT from the
    Authorization header, then returns the authenticated User object.

    Usage:
        @router.get("/protected")
        def protected(current_user: User = Depends(get_current_user)):
            return {"hello": current_user.name}

    Raises HTTP 401 if:
      - No Authorization header
      - Token is invalid or expired
      - User no longer exists in DB
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    payload = decode_token(token)

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is invalid or has expired. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id: str = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token payload.",
        )

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account not found.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled.",
        )

    return user