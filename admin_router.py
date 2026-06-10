"""
admin_router.py  —  Animind Backend  (add-on for render.yaml v2)
================================================================
Wire into main.py:
    from admin_router import router as admin_router, install_error_handler
    app.include_router(admin_router)
    install_error_handler(app)

Provides two protected endpoints readable in the Render dashboard
or any HTTP client:

  GET /admin/errors          — recent unhandled exceptions (in-memory ring)
  GET /admin/users           — user accounts + last-login details (from DB)
  GET /health                — standard health-check (already in your app?)

All /admin/* routes require the header:
    X-Admin-Token: <value of ADMIN_SECRET_TOKEN env var>

Environment variables consumed (all already declared in render.yaml):
    ADMIN_SECRET_TOKEN   required
    SHOW_ERROR_DETAILS   "true" | "false"  (default "true")
    LOG_LEVEL            passed to uvicorn; also used by Python logging
    LOGIN_HISTORY_DAYS   int (default 30)
    ADMIN_USERS_PAGE_SIZE int (default 100)
"""

from __future__ import annotations

import collections
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

# ── logging setup (respects LOG_LEVEL env var) ────────────────────────────
_LOG_LEVEL = os.getenv("LOG_LEVEL", "debug").upper()
logging.basicConfig(
    stream=sys.stdout,
    level=getattr(logging, _LOG_LEVEL, logging.DEBUG),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("animind.admin")

# ── in-memory error ring (last 200 unhandled exceptions) ─────────────────
_MAX_ERRORS = 200
_error_ring: collections.deque[dict[str, Any]] = collections.deque(maxlen=_MAX_ERRORS)

# ── config ────────────────────────────────────────────────────────────────
_SHOW_DETAILS      = os.getenv("SHOW_ERROR_DETAILS", "true").lower() == "true"
_ADMIN_TOKEN       = os.getenv("ADMIN_SECRET_TOKEN", "")
_LOGIN_HISTORY_DAYS = int(os.getenv("LOGIN_HISTORY_DAYS", "30"))
_PAGE_SIZE         = int(os.getenv("ADMIN_USERS_PAGE_SIZE", "100"))

# ── router ────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/admin", tags=["admin"])


# ── auth dependency ───────────────────────────────────────────────────────
def _require_admin(request: Request) -> None:
    """Reject requests that don't carry the correct X-Admin-Token header."""
    if not _ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_SECRET_TOKEN is not configured on this server.",
        )
    token = request.headers.get("X-Admin-Token", "")
    if token != _ADMIN_TOKEN:
        log.warning("Admin auth failed from %s", request.client.host if request.client else "unknown")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Admin-Token header.",
        )


# ═════════════════════════════════════════════════════════════════════════
#  ERROR LOG ENDPOINT
# ═════════════════════════════════════════════════════════════════════════

def record_error(exc: Exception, context: str = "") -> None:
    """
    Call this from your global exception handler (see install_error_handler).
    Also call it anywhere you catch an unexpected exception:
        try:
            ...
        except Exception as e:
            record_error(e, context="generate_animation")
            raise
    """
    entry: dict[str, Any] = {
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "type":       type(exc).__name__,
        "message":    str(exc),
        "context":    context,
    }
    if _SHOW_DETAILS:
        entry["traceback"] = traceback.format_exc()
    _error_ring.appendleft(entry)
    log.error("[ERROR RECORDED] %s: %s  (context=%s)", type(exc).__name__, exc, context)


@router.get(
    "/errors",
    summary="Recent backend errors",
    description=(
        "Returns the last 200 unhandled exceptions captured by the global "
        "error handler, newest first. Requires X-Admin-Token header."
    ),
)
def get_errors(
    _: None = Depends(_require_admin),
    limit: int = 50,
) -> JSONResponse:
    limit = max(1, min(limit, _MAX_ERRORS))
    errors = list(_error_ring)[:limit]
    return JSONResponse(
        content={
            "total_captured": len(_error_ring),
            "returned":       len(errors),
            "show_details":   _SHOW_DETAILS,
            "errors":         errors,
        }
    )


# ═════════════════════════════════════════════════════════════════════════
#  USER LOGIN DETAILS ENDPOINT
# ═════════════════════════════════════════════════════════════════════════

@router.get(
    "/users",
    summary="User accounts and login history",
    description=(
        "Returns all user accounts with last-login timestamp and login count. "
        "Requires X-Admin-Token header."
    ),
)
async def get_users(
    request: Request,
    _: None = Depends(_require_admin),
    page: int = 1,
    page_size: int = _PAGE_SIZE,
) -> JSONResponse:
    """
    Queries your database for user records.
    Supports two ORM styles — SQLAlchemy (sync) and Tortoise/async.
    Adjust the query block to match your actual User model.
    """
    page_size = max(1, min(page_size, 200))
    offset    = (max(1, page) - 1) * page_size

    users_payload: list[dict] = []
    total = 0

    try:
        from auth_utils import get_supabase
        supabase = get_supabase()

        # List users via Supabase Admin API (service-role only)
        res = supabase.auth.admin.list_users()
        all_users = res  # returns a list of UserObject
        total = len(all_users)
        page_users = all_users[offset: offset + page_size]

        users_payload = [
            {
                "id":              u.id,
                "email":           u.email,
                "name":            (u.user_metadata or {}).get("name"),
                "created_at":      u.created_at.isoformat() if u.created_at else None,
                "last_sign_in":    u.last_sign_in_at.isoformat() if u.last_sign_in_at else None,
                "email_confirmed": u.email_confirmed_at is not None,
            }
            for u in page_users
        ]
    except Exception as exc:
        record_error(exc, context="admin /users query")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database query failed: {exc}" if _SHOW_DETAILS else "Database error.",
        )

    return JSONResponse(
        content={
            "page":       page,
            "page_size":  page_size,
            "total":      total,
            "returned":   len(users_payload),
            "users":      users_payload,
        }
    )


# ═════════════════════════════════════════════════════════════════════════
#  GLOBAL ERROR HANDLER  (install into FastAPI app)
# ═════════════════════════════════════════════════════════════════════════

def install_error_handler(app: Any) -> None:
    """
    Attaches a catch-all exception handler to your FastAPI app.
    This feeds every unhandled 500 error into the in-memory ring
    AND returns a structured JSON error body to the client.

    Usage in main.py:
        from admin_router import router as admin_router, install_error_handler
        app.include_router(admin_router)
        install_error_handler(app)
    """
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        record_error(exc, context=f"{request.method} {request.url.path}")
        body: dict[str, Any] = {
            "error":   type(exc).__name__,
            "message": str(exc) if _SHOW_DETAILS else "An internal server error occurred.",
            "path":    str(request.url.path),
        }
        if _SHOW_DETAILS:
            body["traceback"] = traceback.format_exc().splitlines()
        log.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content=body)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        log.warning("HTTP %s on %s %s — %s", exc.status_code, request.method, request.url.path, exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "HTTPException", "status_code": exc.status_code, "detail": exc.detail},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        log.warning("Validation error on %s %s: %s", request.method, request.url.path, exc.errors())
        return JSONResponse(
            status_code=422,
            content={
                "error":  "ValidationError",
                "detail": exc.errors() if _SHOW_DETAILS else "Request validation failed.",
            },
        )

    log.info("Global error handler installed. SHOW_ERROR_DETAILS=%s", _SHOW_DETAILS)


# ═════════════════════════════════════════════════════════════════════════
#  HEALTH CHECK  (can also live in main.py — included here for completeness)
# ═════════════════════════════════════════════════════════════════════════

@router.get("/health-detail", include_in_schema=False)
def health_detail(_: None = Depends(_require_admin)) -> JSONResponse:
    """Extended health check — shows error ring size, config, Python version."""
    return JSONResponse(
        content={
            "status":        "ok",
            "python":        sys.version,
            "log_level":     _LOG_LEVEL,
            "show_details":  _SHOW_DETAILS,
            "errors_in_ring": len(_error_ring),
            "env": {
                "APP_ENV":           os.getenv("APP_ENV", "unknown"),
                "CORS_ORIGINS":      os.getenv("CORS_ORIGINS", ""),
                "SUPABASE_URL":      os.getenv("SUPABASE_URL", ""),
                "LOGIN_HISTORY_DAYS": os.getenv("LOGIN_HISTORY_DAYS", ""),
            },
        }
    )
