"""
database.py — SQLAlchemy + SQLite setup for GenZet
===================================================
Location: backend/database.py

Creates the SQLite database file at ./genzet.db (next to main.py).
Uses SQLAlchemy for ORM and table creation.

No migration tooling needed — tables auto-create on startup.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# ── Database URL ───────────────────────────────────────────────────────
# SQLite stores the DB file right inside your backend folder.
# Change to PostgreSQL URL for production:
#   postgresql://user:pass@host:5432/genzet_db
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./genzet.db")

# ── Engine ─────────────────────────────────────────────────────────────
# check_same_thread=False is required for SQLite + FastAPI async routes
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    echo=False,  # Set True to see SQL statements in console
)

# ── Session factory ────────────────────────────────────────────────────
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# ── Base class for all models ──────────────────────────────────────────
Base = declarative_base()


# ── Dependency: get DB session ─────────────────────────────────────────
def get_db():
    """
    FastAPI dependency — yields a DB session per request, closes it after.

    Usage in route:
        from database import get_db
        from sqlalchemy.orm import Session
        from fastapi import Depends

        @app.get("/example")
        def example(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Create all tables ──────────────────────────────────────────────────
def init_db():
    """
    Called once at startup (in main.py lifespan).
    Creates all tables defined in models.py if they don't exist yet.
    """
    import models  # Import here to avoid circular imports
    Base.metadata.create_all(bind=engine)
    print("[DB]  ✅  Tables created / verified → genzet.db")