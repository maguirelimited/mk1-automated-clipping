"""Login / logout routes and request-level auth enforcement."""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .audit import AuditLogger
from .session import (
    OPERATOR_USERNAME,
    clear_session,
    csrf_token,
    current_user,
    is_authenticated,
    mark_authenticated,
    refresh_activity,
    session_expired,
    validate_csrf,
)

if TYPE_CHECKING:
    from ..config import Settings
    from ..store import ControlStore

# Endpoints that never require authentication.
_PUBLIC_ENDPOINTS = frozenset({"login", "login_post", "static"})
_PUBLIC_PREFIXES = ("/static/",)


def _password_ok(settings: Settings, password: str) -> bool:
    expected = (settings.operator_password or "").strip()
    if not expected or not password:
        return False
    return secrets.compare_digest(password, expected)


def register_auth(
    app: Flask,
    *,
    settings: Settings,
    store: ControlStore,
) -> None:
    """Attach auth routes and protect operational endpoints."""
    app.config["OPS_UI_SETTINGS"] = settings
    app.secret_key = settings.secret_key
    app.permanent_session_lifetime = __import__("datetime").timedelta(
        minutes=max(1, int(settings.session_lifetime_minutes))
    )
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    audit = AuditLogger(store)

    @app.context_processor
    def _auth_context() -> dict:
        # Always expose a CSRF token for authenticated sessions and login.
        # When auth is disabled (tests), still issue a token so forms remain valid.
        needs_csrf = (
            not settings.auth_enabled
            or is_authenticated()
            or (request.endpoint or "") in {"login", "login_post"}
        )
        return {
            "auth_enabled": settings.auth_enabled,
            "auth_user": current_user(),
            "csrf_token": csrf_token() if needs_csrf else "",
        }

    @app.before_request
    def _enforce_auth():
        if not settings.auth_enabled:
            return None

        endpoint = request.endpoint or ""
        path = request.path or ""
        if endpoint in _PUBLIC_ENDPOINTS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return None

        if is_authenticated() and session_expired(
            lifetime_minutes=settings.session_lifetime_minutes
        ):
            user = current_user() or OPERATOR_USERNAME
            audit.session_expired(username=user)
            clear_session()
            flash("Session expired. Please sign in again.", "warn")
            if _wants_json():
                return jsonify({"error": "session_expired"}), 401
            return redirect(url_for("login", next=path))

        if is_authenticated():
            refresh_activity()
            return None

        if _wants_json():
            return jsonify({"error": "authentication_required"}), 401
        return redirect(url_for("login", next=path))

    @app.get("/login")
    def login():
        if settings.auth_enabled and is_authenticated():
            return redirect(url_for("ops_overview"))
        # Ensure CSRF token exists for the login form.
        token = csrf_token()
        return render_template(
            "login.html",
            auth_enabled=settings.auth_enabled,
            csrf_token=token,
            next_url=request.args.get("next") or url_for("ops_overview"),
        )

    @app.post("/login")
    def login_post():
        if not settings.auth_enabled:
            return redirect(url_for("ops_overview"))

        password = str(request.form.get("password") or "")
        next_url = str(request.form.get("next") or url_for("ops_overview"))
        if not next_url.startswith("/"):
            next_url = url_for("ops_overview")

        # CSRF required for login POST (foundation for Phase 14 forms).
        if not validate_csrf(request.form.get("csrf_token")):
            audit.login(username=OPERATOR_USERNAME, ok=False, message="csrf_failed")
            flash("Invalid security token. Try again.", "bad")
            return redirect(url_for("login", next=next_url))

        if not settings.operator_password:
            audit.login(
                username=OPERATOR_USERNAME,
                ok=False,
                message="operator password not configured",
            )
            flash(
                "Operator password is not configured. Set OPS_UI_OPERATOR_PASSWORD.",
                "bad",
            )
            return redirect(url_for("login", next=next_url))

        if not _password_ok(settings, password):
            audit.login(username=OPERATOR_USERNAME, ok=False, message="invalid_password")
            flash("Invalid credentials.", "bad")
            return redirect(url_for("login", next=next_url))

        mark_authenticated(username=OPERATOR_USERNAME)
        audit.login(username=OPERATOR_USERNAME, ok=True, message="login ok")
        flash("Signed in.", "ok")
        return redirect(next_url)

    @app.post("/logout")
    def logout():
        user = current_user() or OPERATOR_USERNAME
        if settings.auth_enabled:
            # Prefer CSRF validation when present; allow logout without token only
            # when session already cleared.
            token = request.form.get("csrf_token")
            if is_authenticated() and token and not validate_csrf(token):
                flash("Invalid security token.", "bad")
                return redirect(url_for("ops_overview"))
            if is_authenticated():
                audit.logout(username=user)
        clear_session()
        flash("Signed out.", "ok")
        return redirect(url_for("login"))

    @app.get("/logout")
    def logout_get():
        # GET logout is convenient but POST is preferred; still audit and clear.
        user = current_user() or OPERATOR_USERNAME
        if settings.auth_enabled and is_authenticated():
            audit.logout(username=user)
        clear_session()
        return redirect(url_for("login"))


def _wants_json() -> bool:
    if request.path.startswith(
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
        return True
    best = request.accept_mimetypes.best
    return best == "application/json"
