"""Tests for pipeline run records (Reliability Phase 8)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"
sys.path.insert(0, str(OPS_DIR))

import execution_lock as el  # noqa: E402
import run_pipeline as rp  # noqa: E402
import run_records as rr  # noqa: E402


def _patch_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    monkeypatch.setattr(rr, "write_config_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(rr, "resolve_code_commit", lambda *_a, **_k: "abc1234")
    monkeypatch.setattr(rp, "resolve_code_commit", lambda *_a, **_k: "abc1234")
    monkeypatch.setattr(
        rp,
        "check_boot_readiness",
        lambda _env: (rp.EXIT_SUCCESS, "boot readiness READY"),
    )
    monkeypatch.setattr(
        rp,
        "check_scheduled_runtime_gate",
        lambda _env, _trigger: ("proceed", "ok"),
    )


def _prepare_factory(tmp_path: Path):
    counter = {"n": 0}

    def prepare(env, *, funnel_id, trigger):
        counter["n"] += 1
        run_id = f"run_{counter['n']:04d}_{trigger}"
        run_dir = tmp_path / "runs" / "dev" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return rp.PipelineRunContext(
            environment="dev",
            env_label="DEVELOPMENT",
            funnel_id=funnel_id,
            trigger=trigger,
            run_id=run_id,
            run_dir=run_dir,
            log_path=run_dir / "run.log",
            code_commit="abc1234",
        )

    return prepare


class TestRunRecordModule:
    def test_duration_and_terminal_fields(self, tmp_path: Path):
        run_dir = tmp_path / "runs" / "dev" / "run_x"
        record = rr.create_running_record(
            run_dir=run_dir,
            run_id="run_x",
            environment="dev",
            trigger="manual_cli",
            funnel_id="f1",
            log_path=run_dir / "run.log",
            code_commit="deadbeef",
            started_at="2026-07-04T00:00:00Z",
        )
        assert record.status == rr.STATUS_RUNNING
        final = rr.finalize_record(
            run_dir,
            status=rr.STATUS_SUCCESS,
            exit_code=0,
            detail="ok",
            jobs_started=1,
            jobs_completed=1,
            jobs_failed=0,
            finished_at="2026-07-04T00:00:10Z",
        )
        assert final.status == rr.STATUS_SUCCESS
        assert final.duration_seconds == 10.0
        assert final.code_commit == "deadbeef"
        assert final.finished_at == "2026-07-04T00:00:10Z"

    def test_finalize_is_idempotent(self, tmp_path: Path):
        run_dir = tmp_path / "run_y"
        rr.create_running_record(
            run_dir=run_dir,
            run_id="run_y",
            environment="dev",
            trigger="test",
            funnel_id="f",
            log_path=run_dir / "run.log",
        )
        first = rr.finalize_record(run_dir, status=rr.STATUS_FAIL, exit_code=1, failure_reason="x")
        second = rr.finalize_record(
            run_dir, status=rr.STATUS_SUCCESS, exit_code=0, detail="should not overwrite"
        )
        assert second.status == rr.STATUS_FAIL
        assert second.failure_reason == first.failure_reason

    def test_environment_separation(self, tmp_path: Path):
        dev = rr.runs_root_for_env("dev", repo_root=tmp_path)
        prod = rr.runs_root_for_env("prod", repo_root=tmp_path)
        assert dev != prod
        assert dev.name == "dev"
        assert prod.name == "prod"


class TestRunPipelineRecords:
    def test_successful_run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _patch_ready(monkeypatch, tmp_path)
        monkeypatch.setattr(rp, "prepare_run_context", _prepare_factory(tmp_path))
        monkeypatch.setattr(
            rp,
            "invoke_run_funnel",
            lambda *_a, **_k: (
                rp.EXIT_SUCCESS,
                "pipeline status=input_ready",
                "input_ready",
                ["job_abc"],
            ),
        )
        monkeypatch.setattr(
            rp,
            "wait_for_video_jobs",
            lambda *_a, **_k: (rp.EXIT_SUCCESS, "video jobs completed=1", 1, 1, 0),
        )

        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="manual_cli")
        assert code == rp.EXIT_SUCCESS
        record = rr.latest_run_record("dev", repo_root=tmp_path)
        assert record is not None
        assert record.status == rr.STATUS_SUCCESS
        assert record.trigger == "manual_cli"
        assert record.jobs_started == 1
        assert record.jobs_completed == 1
        assert record.jobs_failed == 0
        assert record.finished_at
        assert record.duration_seconds is not None
        assert record.code_commit == "abc1234"
        assert (Path(record.log_path)).is_file() or record.log_path.endswith("run.log")

    def test_failed_run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _patch_ready(monkeypatch, tmp_path)
        monkeypatch.setattr(rp, "prepare_run_context", _prepare_factory(tmp_path))
        monkeypatch.setattr(
            rp,
            "invoke_run_funnel",
            lambda *_a, **_k: (rp.EXIT_PIPELINE_FAIL, "run-funnel HTTP 500", "", []),
        )

        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="remote_ssh")
        assert code == rp.EXIT_PIPELINE_FAIL
        record = rr.latest_run_record("dev", repo_root=tmp_path)
        assert record is not None
        assert record.status == rr.STATUS_FAIL
        assert record.trigger == "remote_ssh"
        assert record.failure_reason
        assert record.exit_code == rp.EXIT_PIPELINE_FAIL

    def test_skipped_when_lock_held(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _patch_ready(monkeypatch, tmp_path)
        monkeypatch.setattr(rp, "prepare_run_context", _prepare_factory(tmp_path))
        holder = el.build_lock_payload(
            environment="dev",
            run_id="run_holder",
            trigger="scheduled",
        )
        assert el.acquire_lock("dev", holder, repo_root=tmp_path)[0]

        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="scheduled")
        assert code == rp.EXIT_LOCK_HELD
        record = rr.latest_run_record("dev", repo_root=tmp_path)
        assert record is not None
        assert record.status == rr.STATUS_SKIPPED
        assert record.trigger == "scheduled"
        assert "lock" in (record.failure_reason or "").lower()

    def test_failed_readiness(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setattr(el, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rp, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rp, "prepare_run_context", _prepare_factory(tmp_path))
        monkeypatch.setattr(
            rp,
            "validate_config",
            lambda _env: (rp.EXIT_SUCCESS, "config ok", object()),
        )
        monkeypatch.setattr(rp, "write_config_snapshot", lambda *_a, **_k: "/tmp/snap.yaml")
        monkeypatch.setattr(rr, "resolve_code_commit", lambda *_a, **_k: "abc1234")
        monkeypatch.setattr(rp, "resolve_code_commit", lambda *_a, **_k: "abc1234")
        monkeypatch.setattr(
            rp,
            "check_boot_readiness",
            lambda _env: (rp.EXIT_NOT_READY, "boot readiness NOT READY (API=FAIL)"),
        )

        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="test")
        assert code == rp.EXIT_NOT_READY
        record = rr.latest_run_record("dev", repo_root=tmp_path)
        assert record is not None
        assert record.status == rr.STATUS_FAIL
        assert record.trigger == "test"
        assert "NOT READY" in (record.failure_reason or "")
        assert record.jobs_started == 0

    def test_exception_finalises_record(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _patch_ready(monkeypatch, tmp_path)
        monkeypatch.setattr(rp, "prepare_run_context", _prepare_factory(tmp_path))

        def boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(rp, "invoke_run_funnel", boom)
        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="operations_ui")
        assert code == rp.EXIT_PIPELINE_FAIL
        record = rr.latest_run_record("dev", repo_root=tmp_path)
        assert record is not None
        assert record.status == rr.STATUS_FAIL
        assert record.trigger == "operations_ui"
        assert "boom" in (record.failure_reason or "")
        assert el.inspect_execution_lock("dev", repo_root=tmp_path).os_lock_held is False

    def test_trigger_persisted(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        _patch_ready(monkeypatch, tmp_path)
        monkeypatch.setattr(rp, "prepare_run_context", _prepare_factory(tmp_path))
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
        for trigger in ("scheduled", "manual_cli", "operations_ui", "remote_ssh", "test"):
            code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger=trigger)
            assert code == rp.EXIT_SUCCESS
            record = rr.latest_run_record("dev", repo_root=tmp_path)
            assert record is not None
            assert record.trigger == trigger
