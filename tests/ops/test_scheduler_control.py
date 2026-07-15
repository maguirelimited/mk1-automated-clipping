"""Tests for runtime scheduler control (Remote Operations Prompt 8)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"
STOP_SH = OPS_DIR / "stop-scheduler.sh"
START_SH = OPS_DIR / "start-scheduler.sh"
STATUS_SH = OPS_DIR / "scheduler-status.sh"


def _run_bash(script: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=merged,
        timeout=120,
    )


def _import_scheduler_control():
    if str(OPS_DIR) not in sys.path:
        sys.path.insert(0, str(OPS_DIR))
    import scheduler_control  # noqa: PLC0415

    return scheduler_control


def _import_upload_control():
    if str(OPS_DIR) not in sys.path:
        sys.path.insert(0, str(OPS_DIR))
    import upload_control  # noqa: PLC0415

    return upload_control


class TestEffectiveSchedulerLogic:
    def test_compute_effective_scheduler(self, monkeypatch: pytest.MonkeyPatch):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        from ops_readonly import UnderlyingScheduler, compute_effective_scheduler

        monkeypatch.setenv("MK04_SCHEDULER_MODE", "autonomous")
        underlying = UnderlyingScheduler(mechanism="cron", active=True, detail="cron active")
        assert compute_effective_scheduler(True, underlying, mk04_env_token="prod") == (
            "disabled",
            "disabled by runtime control",
        )
        assert compute_effective_scheduler(False, underlying, mk04_env_token="prod") == (
            "enabled",
            "enabled by runtime control; cron active",
        )
        monkeypatch.setenv("MK04_SCHEDULER_MODE", "manual")
        manual = UnderlyingScheduler(mechanism="manual", active=False, detail="manual")
        assert compute_effective_scheduler(None, manual, mk04_env_token="dev")[0] == "disabled"


class TestSchedulerControlWrites:
    def test_stop_writes_scheduler_disabled_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        scheduler_control = _import_scheduler_control()
        dev_root = tmp_path / "data" / "dev"
        monkeypatch.setattr(scheduler_control, "resolve_data_root", lambda _canonical: dev_root)

        assert scheduler_control.set_runtime_scheduler_disabled("dev", disabled=True, reason="test") == 0
        payload = json.loads((dev_root / "control_state.json").read_text(encoding="utf-8"))
        assert payload["scheduler_disabled"] is True

    def test_start_writes_scheduler_disabled_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        scheduler_control = _import_scheduler_control()
        prod_root = tmp_path / "data" / "prod"
        prod_root.mkdir(parents=True)
        (prod_root / "control_state.json").write_text(
            json.dumps({"scheduler_disabled": True, "uploads_disabled": True}),
            encoding="utf-8",
        )
        monkeypatch.setattr(scheduler_control, "resolve_data_root", lambda _canonical: prod_root)

        assert (
            scheduler_control.set_runtime_scheduler_disabled(
                "prod",
                disabled=False,
                reason="test",
                require_prod_confirm=True,
                confirmed=True,
            )
            == 0
        )
        payload = json.loads((prod_root / "control_state.json").read_text(encoding="utf-8"))
        assert payload["scheduler_disabled"] is False
        assert payload["uploads_disabled"] is True

    def test_stop_preserves_upload_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        scheduler_control = _import_scheduler_control()
        dev_root = tmp_path / "data" / "dev"
        dev_root.mkdir(parents=True)
        (dev_root / "control_state.json").write_text(
            json.dumps({"uploads_disabled": True, "reason": "manual_remote_disable"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(scheduler_control, "resolve_data_root", lambda _canonical: dev_root)

        scheduler_control.set_runtime_scheduler_disabled("dev", disabled=True, reason="test")
        payload = json.loads((dev_root / "control_state.json").read_text(encoding="utf-8"))
        assert payload["uploads_disabled"] is True
        assert payload["scheduler_disabled"] is True

    def test_upload_control_preserves_scheduler_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        upload_control = _import_upload_control()
        dev_root = tmp_path / "data" / "dev"
        dev_root.mkdir(parents=True)
        (dev_root / "control_state.json").write_text(
            json.dumps({"scheduler_disabled": True, "scheduler_reason": "manual_remote_stop"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(upload_control, "resolve_data_root", lambda _canonical: dev_root)
        monkeypatch.setattr(upload_control, "_load_config_upload_enabled", lambda _canonical: (False, ""))

        upload_control.set_runtime_uploads_disabled("dev", disabled=True, reason="test")
        payload = json.loads((dev_root / "control_state.json").read_text(encoding="utf-8"))
        assert payload["scheduler_disabled"] is True
        assert payload["uploads_disabled"] is True

    def test_dev_and_prod_paths_are_separate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        scheduler_control = _import_scheduler_control()
        dev_root = tmp_path / "data" / "dev"
        prod_root = tmp_path / "data" / "prod"

        def _resolve(canonical: str) -> Path:
            return dev_root if canonical == "development" else prod_root

        monkeypatch.setattr(scheduler_control, "resolve_data_root", _resolve)

        scheduler_control.set_runtime_scheduler_disabled("dev", disabled=True, reason="test")
        scheduler_control.set_runtime_scheduler_disabled(
            "prod",
            disabled=False,
            reason="test",
            require_prod_confirm=True,
            confirmed=True,
        )

        dev_payload = json.loads((dev_root / "control_state.json").read_text(encoding="utf-8"))
        prod_payload = json.loads((prod_root / "control_state.json").read_text(encoding="utf-8"))
        assert dev_payload["scheduler_disabled"] is True
        assert prod_payload["scheduler_disabled"] is False


class TestSchedulerProdConfirm:
    def test_prod_start_without_confirm_fails(self):
        scheduler_control = _import_scheduler_control()
        rc = scheduler_control.set_runtime_scheduler_disabled(
            "prod",
            disabled=False,
            reason="test",
            require_prod_confirm=True,
            confirmed=False,
        )
        assert rc == 1

    def test_start_scheduler_prod_without_confirm_fails(self):
        result = _run_bash(START_SH, "prod")
        assert result.returncode != 0
        assert "--confirm" in result.stderr or "--confirm" in result.stdout


class TestSchedulerShellInterface:
    def test_help_flags(self):
        for script in (STOP_SH, START_SH, STATUS_SH):
            for flag in ("--help", "-h"):
                result = _run_bash(script, flag)
                assert result.returncode == 0, result.stdout + result.stderr
                assert "Usage:" in result.stdout

    def test_missing_env_fails(self):
        assert _run_bash(STOP_SH).returncode != 0
        assert _run_bash(START_SH).returncode != 0
        assert _run_bash(STATUS_SH).returncode != 0

    def test_invalid_env_fails(self):
        assert _run_bash(STOP_SH, "staging").returncode != 0
        assert _run_bash(START_SH, "staging").returncode != 0
        assert _run_bash(STATUS_SH, "staging").returncode != 0

    def test_stop_does_not_change_upload_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        scheduler_control = _import_scheduler_control()
        dev_root = tmp_path / "data" / "dev"
        dev_root.mkdir(parents=True)
        (dev_root / "control_state.json").write_text(
            json.dumps({"uploads_disabled": False}),
            encoding="utf-8",
        )
        monkeypatch.setattr(scheduler_control, "resolve_data_root", lambda _canonical: dev_root)

        scheduler_control.set_runtime_scheduler_disabled("dev", disabled=True, reason="test")
        payload = json.loads((dev_root / "control_state.json").read_text(encoding="utf-8"))
        assert payload["uploads_disabled"] is False
        assert payload["scheduler_disabled"] is True

    def test_stop_does_not_delete_jobs_or_clips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        scheduler_control = _import_scheduler_control()
        dev_root = tmp_path / "data" / "dev"
        jobs_root = tmp_path / "jobs" / "dev" / "job_test"
        clips_root = tmp_path / "outputs" / "dev" / "clips"
        jobs_root.mkdir(parents=True)
        clips_root.mkdir(parents=True)
        (jobs_root / "task.json").write_text("{}", encoding="utf-8")
        (clips_root / "clip.mp4").write_bytes(b"clip")

        monkeypatch.setattr(scheduler_control, "resolve_data_root", lambda _canonical: dev_root)
        scheduler_control.set_runtime_scheduler_disabled("dev", disabled=True, reason="test")

        assert (jobs_root / "task.json").is_file()
        assert (clips_root / "clip.mp4").is_file()


class TestScheduledRuntimeGate:
    """Gate checks runtime control only; readiness lives in run-pipeline."""

    def test_gate_skips_when_runtime_disabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        scheduler_control = _import_scheduler_control()
        control_root = tmp_path / "data" / "dev"
        control_root.mkdir(parents=True)
        (control_root / "control_state.json").write_text(
            json.dumps({"scheduler_disabled": True}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            scheduler_control,
            "resolve_data_root_for_gate",
            lambda _env: control_root,
        )

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = scheduler_control.gate_scheduled_run("dev")
        assert rc == 0
        out = buffer.getvalue()
        assert "scheduled run skipped" in out
        assert "runtime control" in out or "stop-scheduler" in out

    def test_gate_silent_when_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        scheduler_control = _import_scheduler_control()
        control_root = tmp_path / "data" / "dev"
        control_root.mkdir(parents=True)
        monkeypatch.setattr(
            scheduler_control,
            "resolve_data_root_for_gate",
            lambda _env: control_root,
        )

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = scheduler_control.gate_scheduled_run("dev")
        assert rc == 0
        assert buffer.getvalue().strip() == ""

    def test_status_reports_control_surface(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        scheduler_control = _import_scheduler_control()
        data_root = tmp_path / "data" / "dev"
        data_root.mkdir(parents=True)
        monkeypatch.setattr(scheduler_control, "resolve_data_root", lambda _c: data_root)
        text = scheduler_control.render_scheduler_status(
            mk04_env_token="dev", data_root=data_root
        )
        assert "Operational controls: stop-scheduler" in text
        assert "run-scheduled.sh" in text
        assert "New scheduled runs allowed:" in text
        assert "Stop does not kill running pipelines" in text


class TestControlIntegratesWithRunPipeline:
    def test_stop_blocks_scheduled_run_pipeline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        import ops_readonly
        import run_pipeline as rp

        scheduler_control = _import_scheduler_control()
        data_root = tmp_path / "data" / "dev"
        data_root.mkdir(parents=True)
        monkeypatch.setattr(scheduler_control, "resolve_data_root", lambda _c: data_root)
        monkeypatch.setattr(rp, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rp, "write_config_snapshot", lambda *_a, **_k: None)
        monkeypatch.setattr(
            rp,
            "validate_config",
            lambda _env: (rp.EXIT_SUCCESS, "config ok", object()),
        )
        monkeypatch.setattr(
            rp,
            "check_boot_readiness",
            lambda _env: (rp.EXIT_SUCCESS, "boot readiness READY"),
        )

        def gate_from_control_state(env, trigger):
            if trigger != "scheduled":
                return "proceed", "non-scheduled"
            allowed, detail = ops_readonly.scheduled_runs_allowed(data_root)
            if not allowed:
                return "skip", f"scheduled run skipped: {detail}"
            return "proceed", detail

        monkeypatch.setattr(rp, "check_scheduled_runtime_gate", gate_from_control_state)

        run_dir = tmp_path / "runs" / "dev" / "run_blocked"
        run_dir.mkdir(parents=True)
        ctx = rp.PipelineRunContext(
            environment="dev",
            env_label="DEVELOPMENT",
            funnel_id="funnel_x",
            trigger="scheduled",
            run_id="run_blocked",
            run_dir=run_dir,
            log_path=run_dir / "run.log",
        )
        monkeypatch.setattr(rp, "prepare_run_context", lambda *_a, **_k: ctx)

        assert (
            scheduler_control.set_runtime_scheduler_disabled(
                "dev", disabled=True, reason="test"
            )
            == 0
        )
        assert json.loads((data_root / "control_state.json").read_text())[
            "scheduler_disabled"
        ] is True

        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="scheduled")
        assert code == rp.EXIT_SUCCESS
        record = json.loads((run_dir / "run_record.json").read_text(encoding="utf-8"))
        assert record["status"] == "SKIPPED"
        assert "stop-scheduler" in (record.get("failure_reason") or "")

        assert (
            scheduler_control.set_runtime_scheduler_disabled(
                "dev", disabled=False, reason="test"
            )
            == 0
        )
        allowed, _ = ops_readonly.scheduled_runs_allowed(data_root)
        assert allowed is True


class TestSchedulerReadinessEvaluation:
    def test_required_probes_block_optional_do_not(self):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        from ops_readonly import evaluate_scheduler_readiness

        def probe(url: str) -> tuple[bool, str]:
            if "/healthz" in url:
                return True, "HTTP 200"
            return False, "connection refused"

        result = evaluate_scheduler_readiness("dev", probe_fn=probe)
        assert result.ready is True
        assert result.reasons == []
        optional = [p for p in result.probes if not p.required]
        assert optional
        assert all(not p.ready for p in optional)

    def test_missing_api_blocks(self):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        from ops_readonly import evaluate_scheduler_readiness

        def probe(url: str) -> tuple[bool, str]:
            if ":5160/" in url:
                return False, "connection refused"
            return True, "HTTP 200"

        result = evaluate_scheduler_readiness("dev", probe_fn=probe)
        assert result.ready is False
        assert any("API" in r for r in result.reasons)
