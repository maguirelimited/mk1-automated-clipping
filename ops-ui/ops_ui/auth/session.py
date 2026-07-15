"""Session helpers for the Operations UI operator session."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from flask import session

SESSION_AUTH_USER = "ops_auth_user"
SESSION_LAST_ACTIVITY = "ops_last_activity"
SESSION_CSRF = "ops_csrf_token"
OPERATOR_USERNAME = "operator"


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _parse_iso(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_authenticated() -> bool:
    return bool(session.get(SESSION_AUTH_USER))


def mark_authenticated(*, username: str = OPERATOR_USERNAME) -> None:
    session[SESSION_AUTH_USER] = username
    session[SESSION_LAST_ACTIVITY] = _utc_now().isoformat().replace("+00:00", "Z")
    session[SESSION_CSRF] = secrets.token_hex(32)
    session.permanent = True


def clear_session() -> None:
    session.pop(SESSION_AUTH_USER, None)
    session.pop(SESSION_LAST_ACTIVITY, None)
    session.pop(SESSION_CSRF, None)


def refresh_activity() -> None:
    if is_authenticated():
        session[SESSION_LAST_ACTIVITY] = _utc_now().isoformat().replace("+00:00", "Z")


def session_expired(*, lifetime_minutes: int) -> bool:
    if not is_authenticated():
        return False
    last = _parse_iso(session.get(SESSION_LAST_ACTIVITY))
    if last is None:
        return True
    age_seconds = (_utc_now() - last).total_seconds()
    return age_seconds > max(1, int(lifetime_minutes)) * 60


def csrf_token() -> str:
    token = session.get(SESSION_CSRF)
    if not token:
        token = secrets.token_hex(32)
        session[SESSION_CSRF] = token
    return str(token)


def validate_csrf(token: str | None) -> bool:
    expected = session.get(SESSION_CSRF)
    if not expected or not token:
        return False
    return secrets.compare_digest(str(expected), str(token))


def current_user() -> str | None:
    user = session.get(SESSION_AUTH_USER)
    return str(user) if user else None
