"""Route protection helpers."""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable, TypeVar

from flask import current_app, flash, redirect, request, url_for

from .session import is_authenticated

F = TypeVar("F", bound=Callable[..., Any])


def login_required(view: F) -> F:
    """Require an authenticated operator session."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any):
        settings = current_app.config.get("OPS_UI_SETTINGS")
        if settings is not None and not getattr(settings, "auth_enabled", True):
            return view(*args, **kwargs)
        if not is_authenticated():
            if request.accept_mimetypes.best == "application/json" or request.path.startswith(
                (
                    "/health",
                    "/status",
                    "/services",
                    "/runs",
                    "/jobs",
                    "/outputs",
                    "/failures",
                    "/config",
                    "/logs",
                    "/api/",
                )
            ):
                return {"error": "authentication_required"}, 401
            flash("Authentication required.", "bad")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped  # type: ignore[return-value]
