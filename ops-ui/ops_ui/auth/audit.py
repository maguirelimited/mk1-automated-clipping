"""Security audit events for authentication (Phase 13).

Phase 14 will extend this with control actions (scheduler, uploads, restarts).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..store import ControlStore

ACTION_LOGIN = "auth.login"
ACTION_LOGOUT = "auth.logout"
ACTION_LOGIN_FAILURE = "auth.login_failure"
ACTION_SESSION_EXPIRED = "auth.session_expired"


class AuditLogger:
    """Thin wrapper over ControlStore.action_log for security events."""

    def __init__(self, store: ControlStore):
        self._store = store

    def login(self, *, username: str, ok: bool, message: str = "") -> None:
        action = ACTION_LOGIN if ok else ACTION_LOGIN_FAILURE
        self._store.log_action(action, username, ok=ok, message=message)

    def logout(self, *, username: str) -> None:
        self._store.log_action(ACTION_LOGOUT, username, ok=True, message="logout")

    def session_expired(self, *, username: str) -> None:
        self._store.log_action(
            ACTION_SESSION_EXPIRED,
            username,
            ok=True,
            message="session expired due to inactivity",
        )

    def record(self, action: str, target: str = "", *, ok: bool, message: str = "") -> None:
        """Generic hook for Phase 14 control actions."""
        self._store.log_action(action, target, ok=ok, message=message)
