"""Operations UI authentication and security foundation (Phase 13)."""

from __future__ import annotations

from .audit import AuditLogger
from .decorators import login_required
from .session import (
    SESSION_AUTH_USER,
    SESSION_CSRF,
    SESSION_LAST_ACTIVITY,
    clear_session,
    csrf_token,
    is_authenticated,
    mark_authenticated,
    refresh_activity,
    session_expired,
    validate_csrf,
)

__all__ = [
    "AuditLogger",
    "SESSION_AUTH_USER",
    "SESSION_CSRF",
    "SESSION_LAST_ACTIVITY",
    "clear_session",
    "csrf_token",
    "is_authenticated",
    "login_required",
    "mark_authenticated",
    "refresh_activity",
    "session_expired",
    "validate_csrf",
]
