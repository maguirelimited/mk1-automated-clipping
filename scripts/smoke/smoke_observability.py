#!/usr/bin/env python3
"""End-to-end smoke checks for Operations & Observability (Phase 15).

Exercises the operator-facing surface through public HTTP interfaces
(Flask test client). Does not mutate production state: no enable-uploads,
no restart, no production pipeline runs.

Usage:
    ops-ui/.venv/bin/python scripts/smoke/smoke_observability.py --env dev
    ops-ui/.venv/bin/pytest tests/smoke/test_observability_smoke.py -q
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_UI_ROOT = REPO_ROOT / "ops-ui"

if str(OPS_UI_ROOT) not in sys.path:
    sys.path.insert(0, str(OPS_UI_ROOT))

PASSWORD = "smoke-operator-password"
SECRET_KEY = "smoke-observability-secret-key"


@dataclass
class CheckResult:
    name: str
    outcome: str  # PASS | WARN | FAIL
    detail: str = ""


@dataclass
class SmokeReport:
    environment: str
    started_at: str
    finished_at: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    overall: str = "FAIL"


def normalize_env(raw: str) -> str:
    token = (raw or "").strip().lower()
    if token in {"dev", "development"}:
        return "dev"
    if token in {"prod", "production"}:
        return "prod"
    raise ValueError(f"invalid environment: {raw!r}. Expected dev or prod.")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def overall_from_checks(checks: list[CheckResult]) -> str:
    if any(c.outcome == "FAIL" for c in checks):
        return "FAIL"
    if any(c.outcome == "WARN" for c in checks):
        return "WARN"
    return "PASS"


def _csrf_from_html(html: str) -> str:
    marker = 'name="csrf_token" value="'
    if marker not in html:
        raise AssertionError("csrf_token not found in HTML")
    return html.split(marker, 1)[1].split('"', 1)[0]


def _build_app(env: str):
    from ops_ui.app import create_app
    from ops_ui.config import ServiceConfig, Settings

    tmp = Path(tempfile.mkdtemp(prefix="obs-smoke-"))
    settings = Settings(
        host="127.0.0.1",
        port=5070,
        data_dir=tmp,
        control_db_path=tmp / "ops.sqlite3",
        controls_file=tmp / "controls.json",
        service_timeout_sec=0.5,
        journal_lines=1,
        funnel_run_timeout_sec=1.0,
        stuck_running_sec=7200.0,
        stuck_queued_sec=1800.0,
        stuck_uploading_sec=1800.0,
        environment=env,
        auth_enabled=True,
        operator_password=PASSWORD,
        secret_key=SECRET_KEY,
        session_lifetime_minutes=60,
        services=(
            ServiceConfig(
                key="source-input",
                label="source-input",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-source-input.service",
            ),
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-video-automation.service",
            ),
            ServiceConfig(
                key="output-funnel",
                label="output-funnel",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-output-funnel.service",
            ),
        ),
    )
    return create_app(settings), tmp


def _login(client) -> CheckResult:
    page = client.get("/login")
    if page.status_code != 200:
        return CheckResult("login_page", "FAIL", f"status={page.status_code}")
    if b"Operator sign in" not in page.data and b"Sign in" not in page.data:
        return CheckResult("login_page", "FAIL", "login page content missing")
    try:
        token = _csrf_from_html(page.get_data(as_text=True))
    except AssertionError as exc:
        return CheckResult("login_csrf", "FAIL", str(exc))
    response = client.post(
        "/login",
        data={"password": PASSWORD, "csrf_token": token, "next": "/ops"},
        follow_redirects=False,
    )
    if response.status_code not in {301, 302}:
        return CheckResult("login_post", "FAIL", f"status={response.status_code}")
    return CheckResult("login", "PASS", "authenticated")


def run_smoke(env: str) -> list[CheckResult]:
    checks: list[CheckResult] = []
    app, _tmp = _build_app(env)
    client = app.test_client()

    # 1. Auth gate
    unauth = client.get("/ops", follow_redirects=False)
    if unauth.status_code in {301, 302} and "/login" in (unauth.headers.get("Location") or ""):
        checks.append(CheckResult("unauthenticated_redirect", "PASS", "redirected to login"))
    else:
        checks.append(
            CheckResult(
                "unauthenticated_redirect",
                "FAIL",
                f"status={unauth.status_code} location={unauth.headers.get('Location')}",
            )
        )

    unauth_api = client.get("/health")
    if unauth_api.status_code == 401:
        checks.append(CheckResult("unauthenticated_api", "PASS", "401 on /health"))
    else:
        checks.append(CheckResult("unauthenticated_api", "FAIL", f"status={unauth_api.status_code}"))

    # 2. Login
    checks.append(_login(client))
    if checks[-1].outcome == "FAIL":
        return checks

    # 3. Overview / UI availability
    overview = client.get("/ops")
    if overview.status_code == 200:
        checks.append(CheckResult("overview_http", "PASS", "200"))
    else:
        checks.append(CheckResult("overview_http", "FAIL", f"status={overview.status_code}"))

    body = overview.data
    for label, needle in (
        ("overview_section_health", b"Overall health"),
        ("overview_section_activity", b"Current activity"),
        ("overview_section_services", b"Automation"),
        ("overview_section_controls", b"Safe actions"),
        ("overview_env_banner", b"DEVELOPMENT" if env == "dev" else b"PRODUCTION"),
    ):
        if needle in body:
            checks.append(CheckResult(label, "PASS"))
        else:
            checks.append(CheckResult(label, "FAIL", f"missing {needle!r}"))

    # 4. Static assets
    css = client.get("/static/ops.css")
    if css.status_code == 200 and b".env-banner" in css.data:
        checks.append(CheckResult("static_css", "PASS"))
    else:
        checks.append(CheckResult("static_css", "FAIL", f"status={css.status_code}"))

    # 5. Health endpoint
    health = client.get("/health")
    health_payload: dict[str, Any] | None = None
    if health.status_code != 200:
        checks.append(CheckResult("health_http", "FAIL", f"status={health.status_code}"))
    else:
        checks.append(CheckResult("health_http", "PASS"))
        try:
            envelope = health.get_json()
            health_payload = envelope.get("data") if isinstance(envelope, dict) else None
        except Exception as exc:
            checks.append(CheckResult("health_json", "FAIL", str(exc)))
            health_payload = None
        if not isinstance(health_payload, dict):
            checks.append(CheckResult("health_json", "FAIL", "missing data object"))
        else:
            checks.append(CheckResult("health_json", "PASS"))
            for key in ("overall", "environment", "upload", "scheduler", "services", "disk"):
                if key in health_payload:
                    checks.append(CheckResult(f"health_field_{key}", "PASS"))
                else:
                    checks.append(CheckResult(f"health_field_{key}", "FAIL", "missing"))

    # 6. Status endpoint
    status = client.get("/status")
    status_payload: dict[str, Any] | None = None
    if status.status_code == 200:
        checks.append(CheckResult("status_http", "PASS"))
        envelope = status.get_json() or {}
        status_payload = envelope.get("data") if isinstance(envelope, dict) else None
        if isinstance(status_payload, dict) and "state" in status_payload:
            checks.append(CheckResult("status_json", "PASS"))
        else:
            checks.append(CheckResult("status_json", "FAIL", "missing state"))
    else:
        checks.append(CheckResult("status_http", "FAIL", f"status={status.status_code}"))

    # 7. Configuration viewer
    config_page = client.get("/ops/configuration")
    if config_page.status_code == 200 and b"Configuration" in config_page.data:
        checks.append(CheckResult("config_page", "PASS"))
    else:
        checks.append(CheckResult("config_page", "FAIL", f"status={config_page.status_code}"))

    config_html = config_page.get_data(as_text=True).lower()
    if "resolved configuration" in config_html:
        checks.append(CheckResult("config_resolved_section", "PASS"))
    else:
        checks.append(CheckResult("config_resolved_section", "FAIL", "section missing"))

    for secret in ("sk-", "password=", "bearer ", "api_key=sk"):
        if secret in config_html:
            checks.append(CheckResult("config_secret_leak", "FAIL", f"found {secret!r}"))
            break
    else:
        checks.append(CheckResult("config_secret_leak", "PASS", "no obvious secrets"))

    config_api = client.get("/config/current")
    if config_api.status_code == 200:
        checks.append(CheckResult("config_api", "PASS"))
        data = (config_api.get_json() or {}).get("data") or {}
        validation = data.get("validation") or {}
        if str(validation.get("state", "")).upper() == "PASS":
            checks.append(CheckResult("config_validation", "PASS"))
        elif str(validation.get("state", "")).upper() == "FAIL":
            checks.append(
                CheckResult(
                    "config_validation",
                    "WARN",
                    str(validation.get("message") or "validation failed"),
                )
            )
        else:
            checks.append(CheckResult("config_validation", "FAIL", "unknown validation state"))
        blob = json.dumps(data).lower()
        if "<redacted>" in blob or "api_key" not in blob:
            checks.append(CheckResult("config_api_redaction", "PASS"))
        elif any(x in blob for x in ("sk-", "hunter2", "bearer ")):
            checks.append(CheckResult("config_api_redaction", "FAIL", "possible secret leak"))
        else:
            checks.append(CheckResult("config_api_redaction", "PASS"))
    else:
        checks.append(CheckResult("config_api", "FAIL", f"status={config_api.status_code}"))

    # 8. Operational controls surface (non-destructive)
    overview_html = overview.get_data(as_text=True)
    if 'name="csrf_token"' in overview_html and "/ops/actions/" in overview_html:
        checks.append(CheckResult("controls_forms", "PASS", "forms and csrf present"))
    else:
        checks.append(CheckResult("controls_forms", "FAIL", "controls surface incomplete"))

    csrf = _csrf_from_html(overview_html)
    confirm = client.post(
        "/ops/actions/enable_uploads",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    if confirm.status_code == 200 and b"Confirm high-risk action" in confirm.data:
        checks.append(CheckResult("controls_high_risk_confirm", "PASS"))
    else:
        checks.append(
            CheckResult(
                "controls_high_risk_confirm",
                "FAIL",
                f"status={confirm.status_code}",
            )
        )

    # Low-risk non-destructive action: validate_config / refresh_health
    csrf2 = _csrf_from_html(client.get("/ops").get_data(as_text=True))
    validate = client.post(
        "/ops/actions/validate_config",
        data={"csrf_token": csrf2},
        follow_redirects=True,
    )
    if validate.status_code == 200:
        checks.append(CheckResult("controls_validate_config", "PASS"))
    else:
        checks.append(
            CheckResult("controls_validate_config", "FAIL", f"status={validate.status_code}")
        )

    # 9. Health consistency (UI vs JSON)
    if health_payload and isinstance(health_payload, dict):
        overall = str(health_payload.get("overall") or "").upper()
        env_token = str(health_payload.get("environment") or "")
        ui_text = overview.get_data(as_text=True)
        if env == "dev" and env_token not in {"dev", "development"}:
            checks.append(
                CheckResult("health_env_consistency", "FAIL", f"health env={env_token!r}")
            )
        elif env == "prod" and env_token not in {"prod", "production"}:
            checks.append(
                CheckResult("health_env_consistency", "FAIL", f"health env={env_token!r}")
            )
        else:
            checks.append(CheckResult("health_env_consistency", "PASS", env_token))

        # Overview should surface the same overall token when connected.
        if overall and overall in ui_text:
            checks.append(CheckResult("health_ui_consistency", "PASS", overall))
        elif overall in {"PASS", "WARN", "FAIL"}:
            # Badge may say HEALTHY for PASS.
            if overall == "PASS" and ("HEALTHY" in ui_text or "PASS" in ui_text):
                checks.append(CheckResult("health_ui_consistency", "PASS", "HEALTHY/PASS"))
            else:
                checks.append(
                    CheckResult(
                        "health_ui_consistency",
                        "WARN",
                        f"overall={overall} not obviously mirrored in UI",
                    )
                )
        else:
            checks.append(CheckResult("health_ui_consistency", "WARN", "overall unavailable"))

        upload = health_payload.get("upload") if isinstance(health_payload.get("upload"), dict) else {}
        upload_status = str(upload.get("status") or "").lower()
        if upload_status and upload_status.upper() in ui_text.upper():
            checks.append(CheckResult("upload_ui_consistency", "PASS", upload_status))
        else:
            checks.append(
                CheckResult(
                    "upload_ui_consistency",
                    "WARN",
                    f"upload status={upload_status!r} not mirrored",
                )
            )

    # 10. Logout
    csrf3 = _csrf_from_html(client.get("/ops").get_data(as_text=True))
    logout = client.post("/logout", data={"csrf_token": csrf3}, follow_redirects=False)
    if logout.status_code in {301, 302} and "/login" in (logout.headers.get("Location") or ""):
        checks.append(CheckResult("logout", "PASS"))
    else:
        checks.append(CheckResult("logout", "FAIL", f"status={logout.status_code}"))

    blocked = client.get("/ops", follow_redirects=False)
    if blocked.status_code in {301, 302} and "/login" in (blocked.headers.get("Location") or ""):
        checks.append(CheckResult("post_logout_protection", "PASS"))
    else:
        checks.append(
            CheckResult("post_logout_protection", "FAIL", f"status={blocked.status_code}")
        )

    return checks


def render_report(report: SmokeReport) -> str:
    lines = [
        "Observability Smoke Report",
        "",
        f"Environment: {report.environment}",
        f"Started:     {report.started_at}",
        f"Finished:    {report.finished_at}",
        f"Overall:     {report.overall}",
        "",
        "Checks:",
    ]
    for item in report.checks:
        detail = f" — {item['detail']}" if item.get("detail") else ""
        lines.append(f"  [{item['outcome']}] {item['name']}{detail}")
    return "\n".join(lines)


def build_report(env: str) -> SmokeReport:
    started = _utc_now()
    checks = run_smoke(env)
    report = SmokeReport(
        environment=env,
        started_at=started,
        finished_at=_utc_now(),
        checks=[asdict(c) for c in checks],
        overall=overall_from_checks(checks),
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operations & Observability smoke test")
    parser.add_argument("--env", default="dev", help="dev or prod (default: dev)")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    parser.add_argument("--no-report", action="store_true", help="Suppress human report")
    args = parser.parse_args(argv)

    try:
        env = normalize_env(args.env)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report = build_report(env)
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    elif not args.no_report:
        print(render_report(report))

    if report.overall == "FAIL":
        return 1
    if report.overall == "WARN":
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
