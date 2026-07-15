"""Tests for shared pipeline entrypoint (Reliability Phase 6)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"
RUN_SH = OPS_DIR / "run-pipeline.sh"

sys.path.insert(0, str(OPS_DIR))

import run_pipeline as rp  # noqa: E402


def _run_bash(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(RUN_SH), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=merged,
        timeout=120,
    )


class TestRunPipelineShell:
    def test_help(self):
        result = _run_bash("--help")
        assert result.returncode == 0
        assert "Usage:" in result.stdout
        assert "Boot readiness" in result.stdout or "boot readiness" in result.stdout.lower()

    def test_missing_env_fails(self):
        result = _run_bash()
        assert result.returncode == 2

    def test_missing_funnel_id_fails(self):
        result = _run_bash("dev")
        assert result.returncode == 2
        assert "funnel_id" in (result.stderr + result.stdout).lower()

    def test_not_ready_exits_4(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        import execution_lock as el
        import run_records as rr

        monkeypatch.setattr(el, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rp, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(
            rp,
            "validate_config",
            lambda _env: (rp.EXIT_SUCCESS, "config ok", object()),
        )
        monkeypatch.setattr(rp, "write_config_snapshot", lambda *_a, **_k: None)
        monkeypatch.setattr(
            rp,
            "check_boot_readiness",
            lambda _env: (rp.EXIT_NOT_READY, "boot readiness NOT READY (API=FAIL)"),
        )
        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="manual_cli")
        assert code == rp.EXIT_NOT_READY


class TestRunPipelineLogic:
    def test_config_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        import execution_lock as el
        import run_records as rr

        monkeypatch.setattr(el, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rp, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(
            rp,
            "validate_config",
            lambda _env: (rp.EXIT_CONFIG, "config validation failed: boom", None),
        )
        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="manual_cli")
        assert code == rp.EXIT_CONFIG

    def test_scheduled_skip_when_runtime_disabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        import execution_lock as el
        import run_records as rr

        monkeypatch.setattr(el, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rp, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(
            rp,
            "validate_config",
            lambda _env: (rp.EXIT_SUCCESS, "config ok", object()),
        )
        monkeypatch.setattr(rp, "write_config_snapshot", lambda *_a, **_k: None)
        monkeypatch.setattr(
            rp,
            "check_boot_readiness",
            lambda _env: (rp.EXIT_SUCCESS, "boot readiness READY"),
        )
        monkeypatch.setattr(
            rp,
            "check_scheduled_runtime_gate",
            lambda _env, _trigger: ("skip", "scheduled run skipped: scheduler disabled by runtime control"),
        )
        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="scheduled")
        assert code == rp.EXIT_SUCCESS

    def test_manual_ignores_scheduler_gate_and_invokes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        import execution_lock as el
        import run_records as rr

        monkeypatch.setattr(el, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rp, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)
        shared = tmp_path / "shared_locks"
        shared.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(shared))
        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.setattr(
            rp,
            "validate_config",
            lambda _env: (rp.EXIT_SUCCESS, "config ok", object()),
        )
        monkeypatch.setattr(rp, "write_config_snapshot", lambda *_a, **_k: None)
        monkeypatch.setattr(
            rp,
            "check_boot_readiness",
            lambda _env: (rp.EXIT_SUCCESS, "boot readiness READY"),
        )
        monkeypatch.setattr(
            rp,
            "check_scheduled_runtime_gate",
            lambda _env, _trigger: ("proceed", "non-scheduled"),
        )

        run_dir = tmp_path / "runs" / "dev" / "run_test"
        run_dir.mkdir(parents=True)
        log_path = run_dir / "run.log"
        ctx = rp.PipelineRunContext(
            environment="dev",
            env_label="DEVELOPMENT",
            funnel_id="funnel_x",
            trigger="manual_cli",
            run_id="run_test",
            run_dir=run_dir,
            log_path=log_path,
        )
        monkeypatch.setattr(rp, "prepare_run_context", lambda *_a, **_k: ctx)
        monkeypatch.setattr(
            rp,
            "invoke_run_funnel",
            lambda *_a, **_k: (
                rp.EXIT_SUCCESS,
                "pipeline status=no_input_available",
                "no_input_available",
                [],
            ),
        )

        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="manual_cli")
        assert code == rp.EXIT_SUCCESS
        assert log_path.is_file()
        record = json.loads((run_dir / "run_record.json").read_text(encoding="utf-8"))
        assert record["status"] == "SUCCESS"
        assert record["trigger"] == "manual_cli"

    def test_invoke_accepts_input_ready(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        locks = tmp_path / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(locks))
        body = json.dumps(
            {"status": "input_ready", "clipping_job": {"job_id": "job_1"}}
        ).encode("utf-8")

        class _Resp:
            status = 200

            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        log = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=_Resp()) as urlopen:
            code, detail, status, job_ids = rp.invoke_run_funnel(
                "dev", "funnel_x", log, run_id="run_test", environment="dev"
            )
        assert code == rp.EXIT_SUCCESS
        assert "input_ready" in detail
        assert status == "input_ready"
        assert job_ids == ["job_1"]
        assert urlopen.call_args.kwargs.get("timeout") == rp.DEFAULT_RUN_FUNNEL_HTTP_TIMEOUT_SEC
        assert "timeout_sec=900" in log.getvalue()

    def test_run_funnel_http_timeout_env_override(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RUN_FUNNEL_HTTP_TIMEOUT_SEC", "600")
        assert rp.run_funnel_http_timeout_sec() == 600.0
        monkeypatch.setenv("RUN_FUNNEL_HTTP_TIMEOUT_SEC", "0")
        monkeypatch.setenv("OPS_UI_FUNNEL_RUN_TIMEOUT_SEC", "450")
        assert rp.run_funnel_http_timeout_sec() == 450.0
        monkeypatch.delenv("RUN_FUNNEL_HTTP_TIMEOUT_SEC", raising=False)
        monkeypatch.delenv("OPS_UI_FUNNEL_RUN_TIMEOUT_SEC", raising=False)
        assert rp.run_funnel_http_timeout_sec() == rp.DEFAULT_RUN_FUNNEL_HTTP_TIMEOUT_SEC

    def test_lock_extension_points_round_trip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import execution_lock as el

        monkeypatch.setattr(el, "REPO_ROOT", tmp_path)
        ctx = rp.PipelineRunContext(
            environment="dev",
            env_label="DEVELOPMENT",
            funnel_id="f",
            trigger="test",
            run_id="run_x",
            run_dir=tmp_path / "run_x",
            log_path=tmp_path / "run_x" / "run.log",
        )
        ctx.run_dir.mkdir(parents=True)
        ok, detail = rp.acquire_execution_lock(ctx)
        assert ok is True
        assert ctx.lock_acquired is True
        assert "acquired" in detail
        release_detail = rp.release_execution_lock(ctx)
        assert "released" in release_detail
        assert ctx.lock_acquired is False


class TestRunFunnelDailyDelegates:
    def test_daily_script_points_at_run_scheduled(self):
        text = (REPO_ROOT / "deploy/scripts/run-funnel-daily.sh").read_text(encoding="utf-8")
        assert "scripts/ops/run-scheduled.sh" in text
