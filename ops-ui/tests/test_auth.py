"""Phase 13 authentication and security foundation tests."""

from __future__ import annotations

from pathlib import Path

from ops_ui.app import create_app
from ops_ui.auth.session import SESSION_AUTH_USER, SESSION_CSRF, SESSION_LAST_ACTIVITY
from ops_ui.config import ServiceConfig, Settings
from ops_ui.store import ControlStore


def _settings(
    tmp_path: Path,
    *,
    auth_enabled: bool = True,
    password: str = "secret-pass",
    lifetime_minutes: int = 60,
) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=5070,
        data_dir=tmp_path,
        control_db_path=tmp_path / "ops.sqlite3",
        controls_file=tmp_path / "controls.json",
        service_timeout_sec=0.01,
        journal_lines=1,
        funnel_run_timeout_sec=1.0,
        stuck_running_sec=7200.0,
        stuck_queued_sec=1800.0,
        stuck_uploading_sec=1800.0,
        environment="dev",
        auth_enabled=auth_enabled,
        operator_password=password,
        secret_key="test-secret-key",
        session_lifetime_minutes=lifetime_minutes,
        services=(
            ServiceConfig(
                key="source-input",
                label="source-input",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-source-input.service",
            ),
        ),
    )


def _login(client, *, password: str = "secret-pass", next_url: str = "/ops") -> None:
    page = client.get("/login")
    assert page.status_code == 200
    # CSRF token is in the form.
    html = page.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    assert marker in html
    token = html.split(marker, 1)[1].split('"', 1)[0]
    response = client.post(
        "/login",
        data={"password": password, "csrf_token": token, "next": next_url},
        follow_redirects=False,
    )
    assert response.status_code in {302, 301}


class TestAuthProtection:
    def test_ops_requires_authentication(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        client = app.test_client()
        for path in (
            "/ops",
            "/ops/runs",
            "/ops/jobs",
            "/ops/outputs",
            "/ops/failures",
            "/ops/configuration",
            "/health",
            "/status",
            "/runs",
            "/config/current",
        ):
            response = client.get(path)
            if path.startswith("/ops"):
                assert response.status_code in {302, 301}, path
                assert "/login" in (response.headers.get("Location") or "")
            else:
                assert response.status_code == 401, path
                assert response.get_json()["error"] == "authentication_required"

    def test_login_logout_flow_and_audit(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        app = create_app(settings)
        client = app.test_client()
        _login(client)
        overview = client.get("/ops")
        assert overview.status_code == 200
        assert b"Operator Console" in overview.data
        assert b"Sign out" in overview.data

        # Logout via POST with CSRF.
        page = client.get("/ops")
        html = page.get_data(as_text=True)
        marker = 'name="csrf_token" value="'
        token = html.split(marker, 1)[1].split('"', 1)[0]
        logout = client.post("/logout", data={"csrf_token": token})
        assert logout.status_code in {302, 301}
        blocked = client.get("/ops")
        assert blocked.status_code in {302, 301}

        store = ControlStore(settings.control_db_path)
        actions = store.recent_actions(limit=10)
        action_names = [a["action"] for a in actions]
        assert "auth.login" in action_names
        assert "auth.logout" in action_names

    def test_invalid_password_is_audited(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        app = create_app(settings)
        client = app.test_client()
        page = client.get("/login")
        html = page.get_data(as_text=True)
        marker = 'name="csrf_token" value="'
        token = html.split(marker, 1)[1].split('"', 1)[0]
        client.post(
            "/login",
            data={"password": "wrong", "csrf_token": token, "next": "/ops"},
        )
        actions = ControlStore(settings.control_db_path).recent_actions(limit=5)
        assert any(a["action"] == "auth.login_failure" for a in actions)
        assert client.get("/ops").status_code in {302, 301}

    def test_session_timeout(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path, lifetime_minutes=1))
        client = app.test_client()
        _login(client)
        assert client.get("/ops").status_code == 200
        with client.session_transaction() as sess:
            sess[SESSION_LAST_ACTIVITY] = "2000-01-01T00:00:00Z"
        response = client.get("/ops")
        assert response.status_code in {302, 301}
        assert "/login" in (response.headers.get("Location") or "")

    def test_auth_disabled_allows_access(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path, auth_enabled=False))
        response = app.test_client().get("/ops")
        assert response.status_code == 200


class TestCsrfFoundation:
    def test_login_rejects_bad_csrf(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        client = app.test_client()
        client.get("/login")  # establish session csrf
        response = client.post(
            "/login",
            data={"password": "secret-pass", "csrf_token": "not-valid", "next": "/ops"},
        )
        assert response.status_code in {302, 301}
        assert client.get("/ops").status_code in {302, 301}
