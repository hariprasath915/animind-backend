"""
database.py — SQLAlchemy setup with auto-fallback to SQLite
============================================================
If DATABASE_URL is set and points to a valid PostgreSQL instance,
we use it.  Otherwise we silently fall back to a local SQLite file.

The fallback is intentionally aggressive: ANY failure during the
PostgreSQL connection test triggers a switch to SQLite so the server
can always start and serve at least health-check requests.
"""
import os
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

_SQLITE_URL  = "sqlite:///./genzet.db"
_configured  = os.getenv("DATABASE_URL", "").strip()

# ── Reject obvious placeholder values ─────────────────────────────────
# Users sometimes paste template URLs like "postgresql://user:pass@host/db"
# which have unresolvable hostnames.  Catch those early.
_PLACEHOLDER_TOKENS = {"@host/", "@host:", "@localhost/", "://user:", "://username:"}

def _looks_like_placeholder(url: str) -> bool:
    """Return True if the URL contains obvious placeholder fragments."""
    low = url.lower()
    return any(tok in low for tok in _PLACEHOLDER_TOKENS)


def _make_engine(url: str):
    return create_engine(
        url,
        connect_args={"check_same_thread": False} if "sqlite" in url else {},
        pool_pre_ping=True,
        echo=False,
    )


# ── Resolve which database to use ─────────────────────────────────────
DATABASE_URL = _configured if _configured else _SQLITE_URL
engine = None

if DATABASE_URL != _SQLITE_URL:
    if _looks_like_placeholder(DATABASE_URL):
        print(f"[DB]  ⚠  DATABASE_URL looks like a placeholder — ignoring it", file=sys.stderr)
        print(f"[DB]  ⚠  Falling back to SQLite → {_SQLITE_URL}", file=sys.stderr)
        DATABASE_URL = _SQLITE_URL
        engine = _make_engine(_SQLITE_URL)
    else:
        try:
            engine = _make_engine(DATABASE_URL)
            with engine.connect() as _conn:
                _conn.execute(text("SELECT 1"))
            print(f"[DB]  ✅  Connected to PostgreSQL")
        except Exception as _db_err:
            print(f"[DB]  ⚠  Cannot reach DATABASE_URL: {_db_err}", file=sys.stderr)
            print(f"[DB]  ⚠  Falling back to SQLite → {_SQLITE_URL}", file=sys.stderr)
            DATABASE_URL = _SQLITE_URL
            engine = _make_engine(_SQLITE_URL)
else:
    print(f"[DB]  ✅  Using SQLite → {_SQLITE_URL}")
    engine = _make_engine(_SQLITE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    import models
    Base.metadata.create_all(bind=engine)
    using = "SQLite" if "sqlite" in DATABASE_URL else "PostgreSQL"
    print(f"[DB]  ✅  Tables created / verified ({using})")