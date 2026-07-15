"""Tests for per-environment pipeline execution lock (Prompt 4 OS advisory)."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"
sys.path.insert(0, str(OPS_DIR))

import execution_lock as el  # noqa: E402
import run_pipeline as rp  # noqa: E402


@pytest.fixture
def lock_root(tmp_path: Path) -> Path:
    return tmp_path


class TestAcquireRelease:
    def test_successful_acquisition(self, lock_root: Path):
        payload = el.build_lock_payload(
            environment="dev",
            run_id="run_a",
            trigger="manual_cli",
            funnel_id="f1",
            pid=os.getpid(),
        )
        ok, detail, blocking = el.acquire_lock("dev", payload, repo_root=lock_root)
        assert ok is True
        assert blocking is None
        assert "acquired" in detail
        path = el.lock_path_for_env("dev", repo_root=lock_root)
        meta = el.meta_path_for_env("dev", repo_root=lock_root)
        assert path.is_file()
        assert meta.is_file()
        data = json.loads(meta.read_text(encoding="utf-8"))
        assert data["environment"] == "dev"
        assert data["run_id"] == "run_a"
        assert data["trigger"] == "manual_cli"
        assert data["pid"] == os.getpid()
        assert "started_at" in data

        released, release_detail = el.release_lock(
            "dev", run_id="run_a", pid=os.getpid(), repo_root=lock_root
        )
        assert released is True
        assert "released" in release_detail
        assert not meta.exists()
        inspection = el.inspect_execution_lock("dev", repo_root=lock_root)
        assert inspection.present is False
        assert inspection.os_lock_held is False

    def test_concurrent_acquisition_fails(self, lock_root: Path):
        first = el.build_lock_payload(
            environment="dev",
            run_id="run_a",
            trigger="scheduled",
            pid=os.getpid(),
        )
        assert el.acquire_lock("dev", first, repo_root=lock_root)[0] is True

        second = el.build_lock_payload(
            environment="dev",
            run_id="run_b",
            trigger="manual_cli",
            pid=os.getpid() + 1,
        )
        ok, detail, blocking = el.acquire_lock("dev", second, repo_root=lock_root)
        assert ok is False
        assert "not acquired" in detail
        assert blocking is not None
        assert blocking.present is True
        assert blocking.os_lock_held is True
        assert blocking.payload is not None
        assert blocking.payload.run_id == "run_a"

    def test_release_after_failure_path(self, lock_root: Path):
        payload = el.build_lock_payload(
            environment="prod",
            run_id="run_fail",
            trigger="manual_cli",
            pid=os.getpid(),
        )
        assert el.acquire_lock("prod", payload, repo_root=lock_root)[0] is True
        ok, _ = el.release_lock(
            "prod", run_id="run_fail", pid=os.getpid(), repo_root=lock_root
        )
        assert ok is True
        assert not el.meta_path_for_env("prod", repo_root=lock_root).exists()

    def test_release_refuses_wrong_owner(self, lock_root: Path):
        payload = el.build_lock_payload(
            environment="dev",
            run_id="run_a",
            trigger="manual_cli",
            pid=os.getpid(),
        )
        assert el.acquire_lock("dev", payload, repo_root=lock_root)[0] is True
        ok, detail = el.release_lock(
            "dev", run_id="run_other", pid=os.getpid(), repo_root=lock_root
        )
        assert ok is False
        assert "not releasing" in detail
        assert el.meta_path_for_env("dev", repo_root=lock_root).is_file()


class TestStaleDetection:
    def test_stale_metadata_does_not_block(self, lock_root: Path):
        payload = el.build_lock_payload(
            environment="dev",
            run_id="run_dead",
            trigger="scheduled",
            pid=999_999_999,
            started_at=datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        )
        path = el.lock_path_for_env("dev", repo_root=lock_root)
        meta = el.meta_path_for_env("dev", repo_root=lock_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        meta.write_text(json.dumps(payload.to_dict()), encoding="utf-8")

        inspection = el.inspect_execution_lock("dev", repo_root=lock_root)
        assert inspection.present is False
        assert inspection.stale is True
        assert inspection.metadata_authoritative is False
        assert inspection.os_lock_held is False

        other = el.build_lock_payload(
            environment="dev",
            run_id="run_new",
            trigger="manual_cli",
            pid=os.getpid(),
        )
        ok, detail, _blocking = el.acquire_lock("dev", other, repo_root=lock_root)
        assert ok is True
        assert "acquired" in detail

    def test_stale_when_age_exceeded(self, lock_root: Path):
        old = (datetime.now(UTC) - timedelta(hours=10)).replace(microsecond=0)
        payload = el.build_lock_payload(
            environment="dev",
            run_id="run_old",
            trigger="scheduled",
            pid=os.getpid(),
            started_at=old.isoformat().replace("+00:00", "Z"),
            stale_after_hours=6,
        )
        stale, reasons = el.evaluate_staleness(payload)
        assert stale is True
        assert any("stale_after_hours" in r for r in reasons)


class TestDevProdIsolation:
    def test_independent_dev_and_prod_locks(self, lock_root: Path):
        dev_payload = el.build_lock_payload(
            environment="dev",
            run_id="run_dev",
            trigger="manual_cli",
            pid=os.getpid(),
        )
        prod_payload = el.build_lock_payload(
            environment="prod",
            run_id="run_prod",
            trigger="scheduled",
            pid=os.getpid(),
        )
        assert el.acquire_lock("dev", dev_payload, repo_root=lock_root)[0] is True
        assert el.acquire_lock("prod", prod_payload, repo_root=lock_root)[0] is True
        assert el.lock_path_for_env("dev", repo_root=lock_root) != el.lock_path_for_env(
            "prod", repo_root=lock_root
        )


def _mock_pipeline_gates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import run_records as rr

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
    monkeypatch.setattr(
        rp,
        "check_scheduled_runtime_gate",
        lambda _env, _trigger: ("proceed", "ok"),
    )
    monkeypatch.setattr(el, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(rp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(rp, "write_config_snapshot", lambda *_a, **_k: None)
    shared = tmp_path / "shared_locks"
    shared.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(shared))


class TestRunPipelineLockIntegration:
    def test_second_run_skipped_with_record(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _mock_pipeline_gates(monkeypatch, tmp_path)

        runs_root = tmp_path / "runs" / "dev"
        runs_root.mkdir(parents=True)

        def prepare(env, *, funnel_id, trigger):
            run_id = f"run_{len(list(runs_root.iterdir()))}_{trigger}"
            run_dir = runs_root / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            return rp.PipelineRunContext(
                environment="dev",
                env_label="DEVELOPMENT",
                funnel_id=funnel_id,
                trigger=trigger,
                run_id=run_id,
                run_dir=run_dir,
                log_path=run_dir / "run.log",
            )

        monkeypatch.setattr(rp, "prepare_run_context", prepare)
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

        holder = el.build_lock_payload(
            environment="dev",
            run_id="run_holder",
            trigger="scheduled",
            pid=os.getpid(),
        )
        assert el.acquire_lock("dev", holder, repo_root=tmp_path)[0] is True

        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="manual_cli")
        assert code == rp.EXIT_LOCK_HELD

        skipped_dirs = [p for p in runs_root.iterdir() if p.is_dir()]
        assert skipped_dirs
        record = json.loads((skipped_dirs[0] / "run_record.json").read_text(encoding="utf-8"))
        assert record["status"] == "SKIPPED"
        reason = (record.get("failure_reason") or record.get("detail") or "").lower()
        assert "lock" in reason
        assert el.inspect_execution_lock("dev", repo_root=tmp_path).os_lock_held is True

    def test_lock_released_after_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _mock_pipeline_gates(monkeypatch, tmp_path)

        run_dir = tmp_path / "runs" / "dev" / "run_ok"
        run_dir.mkdir(parents=True)
        ctx = rp.PipelineRunContext(
            environment="dev",
            env_label="DEVELOPMENT",
            funnel_id="funnel_x",
            trigger="manual_cli",
            run_id="run_ok",
            run_dir=run_dir,
            log_path=run_dir / "run.log",
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
        assert el.inspect_execution_lock("dev", repo_root=tmp_path).os_lock_held is False

    def test_lock_released_after_pipeline_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        _mock_pipeline_gates(monkeypatch, tmp_path)

        run_dir = tmp_path / "runs" / "dev" / "run_fail"
        run_dir.mkdir(parents=True)
        ctx = rp.PipelineRunContext(
            environment="dev",
            env_label="DEVELOPMENT",
            funnel_id="funnel_x",
            trigger="manual_cli",
            run_id="run_fail",
            run_dir=run_dir,
            log_path=run_dir / "run.log",
        )
        monkeypatch.setattr(rp, "prepare_run_context", lambda *_a, **_k: ctx)
        monkeypatch.setattr(
            rp,
            "invoke_run_funnel",
            lambda *_a, **_k: (rp.EXIT_PIPELINE_FAIL, "run-funnel HTTP 500", "", []),
        )

        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="manual_cli")
        assert code == rp.EXIT_PIPELINE_FAIL
        assert el.inspect_execution_lock("dev", repo_root=tmp_path).os_lock_held is False
