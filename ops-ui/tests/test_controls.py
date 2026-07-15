"""Safe control actions tests (Phase 14)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.controls import ActionResult, _summarize_run_pipeline_output, execute_control_action
from ops_ui.store import ControlStore


def _settings(
    tmp_path: Path,
    *,
    environment: str = "dev",
    auth_enabled: bool = False,
    password: str = "secret-pass",
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
        environment=environment,
        auth_enabled=auth_enabled,
        operator_password=password,
        secret_key="test-secret-key",
        services=(
            ServiceConfig(
                key="source-input",
                label="source-input",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-source-input.service",
            ),
        ),
    )


def _login(client, password: str = "secret-pass") -> None:
    page = client.get("/login")
    html = page.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    token = html.split(marker, 1)[1].split('"', 1)[0]
    client.post(
        "/login",
        data={"password": password, "csrf_token": token, "next": "/ops"},
    )


class TestControlDispatch:
    def test_disable_uploads_calls_ops_layer(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with mock.patch("ops_ui.controls.set_runtime_uploads_disabled", return_value=0) as fn:
            result = execute_control_action(settings, "disable_uploads")
        assert result.ok is True
        fn.assert_called_once()
        assert fn.call_args.kwargs["disabled"] is True

    def test_enable_uploads_requires_confirmation(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        denied = execute_control_action(settings, "enable_uploads", confirmed=False)
        assert denied.ok is False
        assert "Confirmation required" in denied.message

        with mock.patch("ops_ui.controls.set_runtime_uploads_disabled", return_value=0) as fn:
            allowed = execute_control_action(settings, "enable_uploads", confirmed=True)
        assert allowed.ok is True
        assert fn.call_args.kwargs["confirmed"] is True

    def test_restart_uses_execute_restart(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with mock.patch("ops_ui.controls.execute_restart", return_value=0) as fn:
            result = execute_control_action(
                settings, "restart_service", confirmed=True, restart_target="api"
            )
        assert result.ok is True
        fn.assert_called_once()
        assert fn.call_args.args[1] == "api"

    def test_dev_run_uses_run_pipeline(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, environment="dev")
        with mock.patch("ops_ui.controls.run_pipeline", return_value=0) as fn:
            with mock.patch(
                "ops_ui.controls.resolve_manual_funnel",
                return_value=mock.Mock(funnel_id="explicit_funnel"),
            ):
                result = execute_control_action(
                    settings, "run_pipeline_dev", funnel_id="explicit_funnel"
                )
        assert result.ok is True
        assert fn.call_args.kwargs["trigger"] == "operations_ui"
        assert fn.call_args.kwargs["funnel_id"] == "explicit_funnel"

    def test_prod_run_blocked_on_dev_env(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, environment="dev")
        result = execute_control_action(settings, "run_pipeline_prod", confirmed=True)
        assert result.ok is False


class TestControlRoutes:
    def test_unauthenticated_cannot_post_actions(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path, auth_enabled=True))
        response = app.test_client().post("/ops/actions/disable_uploads")
        assert response.status_code in {302, 301, 401}

    def test_low_risk_action_audited(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, auth_enabled=True)
        app = create_app(settings)
        client = app.test_client()
        _login(client)
        page = client.get("/ops")
        html = page.get_data(as_text=True)
        marker = 'name="csrf_token" value="'
        token = html.split(marker, 1)[1].split('"', 1)[0]
        with mock.patch(
            "ops_ui.app.execute_control_action",
            return_value=ActionResult(True, "disable_uploads", "Uploads disabled."),
        ):
            response = client.post(
                "/ops/actions/disable_uploads",
                data={"csrf_token": token},
                follow_redirects=False,
            )
        assert response.status_code in {302, 301}
        actions = ControlStore(settings.control_db_path).recent_actions(limit=10)
        assert any(a["action"] == "control.disable_uploads" for a in actions)

    def test_high_risk_requires_confirm_step(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, auth_enabled=True)
        app = create_app(settings)
        client = app.test_client()
        _login(client)
        page = client.get("/ops")
        html = page.get_data(as_text=True)
        marker = 'name="csrf_token" value="'
        token = html.split(marker, 1)[1].split('"', 1)[0]
        response = client.post(
            "/ops/actions/enable_uploads",
            data={"csrf_token": token},
        )
        assert response.status_code == 200
        assert b"Confirm high-risk action" in response.data
        assert b'name="confirm" value="yes"' in response.data

    def test_csrf_required_when_auth_enabled(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, auth_enabled=True)
        app = create_app(settings)
        client = app.test_client()
        _login(client)
        response = client.post("/ops/actions/disable_uploads", data={})
        assert response.status_code in {302, 301}
        # Should not execute without CSRF.
        actions = ControlStore(settings.control_db_path).recent_actions(limit=20)
        assert not any(a["action"] == "control.disable_uploads" for a in actions)


class TestRunPipelineSummary:
    _SAMPLE_OUTPUT = """\
run_pipeline start env=DEVELOPMENT run_id=run_20260705T151044Z_operations_ui
trigger=operations_ui funnel_id=mfm_business_ai_001
config validation passed
boot readiness READY
execution lock acquired
invoke POST http://127.0.0.1:5160/run-funnel funnel_id=mfm_business_ai_001
response HTTP 200: {"status":"no_input_available","success":true,"reason":"All valid candidates already have active or completed input ledger records."}
result funnel_id=mfm_business_ai_001 status=no_input_available
run_pipeline finished status=SUCCESS exit=0 detail=pipeline status=no_input_available
run_id=run_20260705T151044Z_operations_ui
log_path=/home/maguireltd/mk1-automated-clipping/runs/dev/run_20260705T151044Z_operations_ui/run.log
record_path=/home/maguireltd/mk1-automated-clipping/runs/dev/run_20260705T151044Z_operations_ui/run_record.json
status=SUCCESS
"""

    def test_no_input_available_summary(self) -> None:
        message = _summarize_run_pipeline_output(
            0,
            self._SAMPLE_OUTPUT,
            "mfm_business_ai_001",
        )
        assert "No new videos to process" in message
        assert "mfm_business_ai_001" in message
        assert "run_20260705T151044Z_operations_ui" in message
        assert "HTTP 200" not in message
        assert "execution lock" not in message

    def test_input_ready_summary(self) -> None:
        text = self._SAMPLE_OUTPUT.replace(
            '"status":"no_input_available"',
            '"status":"input_ready"',
        ).replace("status=no_input_available", "status=input_ready")
        message = _summarize_run_pipeline_output(0, text, "business")
        assert "New input queued" in message

    def test_failure_summary(self) -> None:
        text = "Error: boot readiness NOT READY\nstatus=FAIL"
        message = _summarize_run_pipeline_output(4, text, "business")
        assert "failed" in message.lower()
        assert "NOT READY" in message

    def test_dev_run_uses_summary(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, environment="dev")
        sample = self._SAMPLE_OUTPUT
        with mock.patch("ops_ui.controls.run_pipeline", return_value=0):
            with mock.patch("ops_ui.controls._capture", return_value=(0, sample)):
                result = execute_control_action(
                    settings,
                    "run_pipeline_dev",
                    funnel_id="mfm_business_ai_001",
                )
        assert result.ok is True
        assert "No new videos to process" in result.message
        assert "run_pipeline start" not in result.message
