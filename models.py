"""
models.py — SQLAlchemy ORM Models for GenZet
=============================================
Location: backend/models.py

Two tables:
  users       — teacher accounts (email, hashed_password, user_id)
  animations  — saved animations linked to a user_id

Relationships:
  users ──< animations  (one user → many animations)

Indexes:
  users.email          — fast login lookup
  animations.user_id   — fast per-user fetch
  animations.anim_id   — fast upsert dedup
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Text, DateTime, ForeignKey, Index, Boolean
)
from sqlalchemy.orm import relationship

from database import Base


# ══════════════════════════════════════════════════════════════════════
# USER MODEL
# ══════════════════════════════════════════════════════════════════════
class User(Base):
    __tablename__ = "users"

    # ── Primary key: UUID string ─────────────────────────────────────
    id = Column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        index=True,
    )

    # ── Identity fields ──────────────────────────────────────────────
    name          = Column(String(120), nullable=False)
    email         = Column(String(254), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)

    # ── Account status ───────────────────────────────────────────────
    is_active  = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ── Relationship: one user → many animations ─────────────────────
    animations = relationship(
        "Animation",
        back_populates="owner",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def __repr__(self):
        return f"<User id={self.id!r} email={self.email!r}>"


# ══════════════════════════════════════════════════════════════════════
# ANIMATION MODEL
# ══════════════════════════════════════════════════════════════════════
class Animation(Base):
    __tablename__ = "animations"

    # ── Primary key: DB row ID ───────────────────────────────────────
    id = Column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    # ── Client-side ID (matches the `id` field in IndexedDB) ─────────
    # This is the idempotency key — prevents duplicate syncs.
    anim_id = Column(String(64), nullable=False, index=True)

    # ── Foreign key: owner ───────────────────────────────────────────
    user_id = Column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ── Animation data ───────────────────────────────────────────────
    title          = Column(String(500), nullable=False, default="Untitled")
    prompt         = Column(Text, default="")
    explanation    = Column(Text, default="")
    animation_code = Column(Text, default="")   # The full HTML — can be very large
    playlist       = Column(String(200), default="General")

    # ── Timestamps ───────────────────────────────────────────────────
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ── Relationship back to user ─────────────────────────────────────
    owner = relationship("User", back_populates="animations")

    def __repr__(self):
        return f"<Animation anim_id={self.anim_id!r} user={self.user_id!r}>"

    def to_dict(self) -> dict:
        """Serialize to the exact shape the frontend expects."""
        return {
            "id":             self.anim_id,          # client-side id
            "db_id":          self.id,               # server row id
            "title":          self.title,
            "prompt":         self.prompt or "",
            "explanation":    self.explanation or "",
            "animation_code": self.animation_code or "",
            "playlist":       self.playlist or "General",
            "created_at":     self.created_at.isoformat() if self.created_at else None,
            "updated_at":     self.updated_at.isoformat() if self.updated_at else None,
        }


# ══════════════════════════════════════════════════════════════════════
# COMPOSITE INDEXES — created after table definitions
# ══════════════════════════════════════════════════════════════════════
# Unique constraint: one user cannot have two rows with the same anim_id
# This makes POST /sync/animations idempotent — safe to call multiple times
Index(
    "uq_user_anim",
    Animation.user_id,
    Animation.anim_id,
    unique=True,
)