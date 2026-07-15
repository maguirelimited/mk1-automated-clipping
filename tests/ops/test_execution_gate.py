"""Cross-environment execution gate tests (Prompt 4).

Uses temporary lock directories and subprocesses. No WhisperX/FFmpeg/AI/uploads.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"
sys.path.insert(0, str(OPS_DIR))

import execution_gate as eg  # noqa: E402
import run_pipeline as rp  # noqa: E402


@pytest.fixture
def shared_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "locks"
    root.mkdir(parents=True)
    monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(root))
    monkeypatch.delenv("MK04_REQUIRE_RUNTIME_PATHS", raising=False)
    monkeypatch.delenv("MK04_PRODUCTION_INSTALLED", raising=False)
    monkeypatch.setenv("MK04_ENV", "dev")
    monkeypatch.setattr(eg, "production_installation_present", lambda: False)
    return root


def _hold_global(root: str, env: str, run_id: str, ready: mp.Event, release: mp.Event) -> None:
    os.environ["MK04_SHARED_LOCK_ROOT"] = root
    os.environ["MK04_ENV"] = env
    handle = eg.acquire_global_pipeline_lock(
        environment=env,
        run_id=run_id,
        job_id="job_hold",
        shared_root=Path(root),
        blocking=True,
    )
    ready.set()
    release.wait(timeout=30)
    handle.release()


def _prod_admit_wait(
    root: str,
    run_id: str,
    admitted: mp.Event,
    release: mp.Event,
    error_box: dict,
) -> None:
    try:
        os.environ["MK04_SHARED_LOCK_ROOT"] = root
        os.environ["MK04_ENV"] = "prod"
        handle = eg.admit_orchestration(
            environment="prod",
            run_id=run_id,
            trigger="test",
            shared_root=Path(root),
        )
        admitted.set()
        release.wait(timeout=30)
        handle.release()
    except Exception as exc:  # pragma: no cover
        error_box["error"] = repr(exc)


class TestSharedRootResolution:
    def test_dev_and_prod_resolve_same_configured_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        root = tmp_path / "shared"
        root.mkdir()
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(root))
        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.delenv("MK04_PRODUCTION_INSTALLED", raising=False)
        assert eg.resolve_shared_lock_root() == root.resolve()
        monkeypatch.setenv("MK04_ENV", "prod")
        assert eg.resolve_shared_lock_root() == root.resolve()

    def test_production_missing_shared_root_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        monkeypatch.setenv("MK04_ENV", "prod")
        monkeypatch.delenv("MK04_SHARED_LOCK_ROOT", raising=False)
        monkeypatch.setattr(eg, "DEFAULT_DEPLOYED_SHARED_ROOT", tmp_path / "missing_locks")
        with pytest.raises(eg.GateError, match="does not exist"):
            eg.ensure_shared_lock_root(environment="prod")

    def test_production_rejects_repo_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MK04_ENV", "prod")
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(tmp_path / ".mk04_locks"))
        with pytest.raises(eg.GateError, match="must not be a repository path"):
            eg.resolve_shared_lock_root()

    def test_dev_fallback_when_no_production(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.delenv("MK04_SHARED_LOCK_ROOT", raising=False)
        monkeypatch.delenv("MK04_PRODUCTION_INSTALLED", raising=False)
        monkeypatch.setenv("MK04_CODE_ROOT", str(tmp_path))
        monkeypatch.setattr(eg, "DEFAULT_DEPLOYED_SHARED_ROOT", tmp_path / "no_deployed")
        monkeypatch.setattr(eg, "production_installation_present", lambda: False)
        resolved = eg.resolve_shared_lock_root()
        assert resolved == (tmp_path / ".mk04_locks").resolve()
        ensured = eg.ensure_shared_lock_root()
        assert ensured.is_dir()
        assert os.access(ensured, os.W_OK)

    def test_dev_uses_deployed_when_writable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        deployed = tmp_path / "locks"
        deployed.mkdir()
        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.delenv("MK04_SHARED_LOCK_ROOT", raising=False)
        monkeypatch.setattr(eg, "DEFAULT_DEPLOYED_SHARED_ROOT", deployed)
        monkeypatch.setattr(eg, "production_installation_present", lambda: False)
        assert eg.resolve_shared_lock_root() == deployed.resolve()

    def test_dev_preserves_explicit_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        explicit = tmp_path / "custom_locks"
        explicit.mkdir()
        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(explicit))
        assert eg.resolve_shared_lock_root() == explicit.resolve()

    def test_invalid_explicit_override_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        missing = tmp_path / "missing_explicit"
        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(missing))
        monkeypatch.setattr(eg, "production_installation_present", lambda: False)
        with pytest.raises(eg.GateError, match="does not exist|not writable|unusable"):
            eg.ensure_shared_lock_root()

    def test_prod_never_uses_repo_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MK04_ENV", "prod")
        monkeypatch.delenv("MK04_SHARED_LOCK_ROOT", raising=False)
        monkeypatch.setenv("MK04_CODE_ROOT", str(tmp_path))
        monkeypatch.setattr(eg, "DEFAULT_DEPLOYED_SHARED_ROOT", tmp_path / "deployed_locks")
        monkeypatch.setattr(eg, "production_installation_present", lambda: True)
        resolved = eg.resolve_shared_lock_root()
        assert not eg._is_repo_fallback(resolved)
        with pytest.raises(eg.GateError, match="does not exist|bootstrap"):
            eg.ensure_shared_lock_root(environment="prod")

    def test_dev_fails_when_production_installed_without_access(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.delenv("MK04_SHARED_LOCK_ROOT", raising=False)
        monkeypatch.setattr(eg, "DEFAULT_DEPLOYED_SHARED_ROOT", tmp_path / "prod_locks")
        monkeypatch.setattr(eg, "production_installation_present", lambda: True)
        with pytest.raises(eg.GateError, match="production installed|bootstrap|does not exist"):
            eg.ensure_shared_lock_root(environment="dev")

    def test_permission_error_is_gate_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(tmp_path / "x"))
        monkeypatch.setattr(eg, "production_installation_present", lambda: False)

        def boom(*_a, **_k):
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "mkdir", boom)
        # Force repo-like path so ensure attempts mkdir.
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(tmp_path / ".mk04_locks"))
        with pytest.raises(eg.GateError, match="cannot create|filesystem|Permission"):
            eg.ensure_shared_lock_root()


class TestAdmissionPriority:
    def test_dev_starts_when_free(self, shared_root: Path):
        handle = eg.admit_orchestration(
            environment="dev", run_id="dev1", trigger="test", shared_root=shared_root
        )
        snap = eg.read_gate_status(shared_root=shared_root)
        assert snap.state == eg.GATE_DEV_ACTIVE
        handle.release()

    def test_prod_starts_when_free(self, shared_root: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MK04_ENV", "prod")
        handle = eg.admit_orchestration(
            environment="prod", run_id="prod1", trigger="test", shared_root=shared_root
        )
        assert handle.production_priority is True
        snap = eg.read_gate_status(shared_root=shared_root)
        assert snap.state in {eg.GATE_PROD_WAITING, eg.GATE_PROD_ACTIVE}
        handle.release()

    def test_prod_waiting_prevents_new_dev(self, shared_root: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MK04_ENV", "dev")
        # Dev admits then releases turnstile so prod can take exclusive while heavy work continues.
        heavy = eg.acquire_global_pipeline_lock(
            environment="dev",
            run_id="dev_heavy",
            job_id="j1",
            shared_root=shared_root,
        )
        monkeypatch.setenv("MK04_ENV", "prod")
        prod = eg.admit_orchestration(
            environment="prod",
            run_id="prod_wait",
            trigger="test",
            shared_root=shared_root,
        )
        assert prod.production_priority is True
        snap = eg.read_gate_status(shared_root=shared_root)
        assert snap.state in {eg.GATE_PROD_WAITING, eg.GATE_PROD_ACTIVE, eg.GATE_DEV_ACTIVE}

        monkeypatch.setenv("MK04_ENV", "dev")
        with pytest.raises(eg.GateError, match="refused"):
            eg.admit_orchestration(
                environment="dev",
                run_id="dev_late",
                trigger="test",
                shared_root=shared_root,
            )
        heavy.release()
        prod.release()

    def test_dev_refuses_while_prod_active(
        self, shared_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MK04_ENV", "prod")
        prod = eg.admit_orchestration(
            environment="prod", run_id="prod_active", trigger="test", shared_root=shared_root
        )
        heavy = eg.acquire_global_pipeline_lock(
            environment="prod",
            run_id="prod_active",
            job_id="j1",
            shared_root=shared_root,
        )
        monkeypatch.setenv("MK04_ENV", "dev")
        with pytest.raises(eg.GateError, match="refused"):
            eg.admit_orchestration(
                environment="dev", run_id="dev_blocked", trigger="test", shared_root=shared_root
            )
        heavy.release()
        prod.release()

    def test_prod_waits_behind_dev_then_continues(self, shared_root: Path):
        ready = mp.Event()
        release_dev = mp.Event()
        admitted = mp.Event()
        release_prod = mp.Event()
        manager = mp.Manager()
        error_box = manager.dict()

        # Dev holds shared turnstile briefly, then releases while keeping global.
        dev = eg.admit_orchestration(
            environment="dev", run_id="dev_run", trigger="test", shared_root=shared_root
        )
        heavy = eg.acquire_global_pipeline_lock(
            environment="dev",
            run_id="dev_run",
            job_id="j1",
            shared_root=shared_root,
        )
        dev.release_turnstile()

        proc = mp.Process(
            target=_prod_admit_wait,
            args=(str(shared_root), "prod_run", admitted, release_prod, error_box),
        )
        proc.start()
        # Prod should obtain turnstile while waiting for heavy resource (global still held by us).
        assert admitted.wait(timeout=5)
        snap = eg.read_gate_status(shared_root=shared_root)
        assert snap.owning_environment in {"prod", "dev"}
        # Later dev must be refused while prod holds turnstile.
        with pytest.raises(eg.GateError, match="refused"):
            eg.admit_orchestration(
                environment="dev",
                run_id="dev_jump",
                trigger="test",
                shared_root=shared_root,
            )
        heavy.release()
        release_prod.set()
        proc.join(timeout=10)
        assert proc.exitcode == 0
        assert "error" not in error_box


class TestHeavyWorkerExclusion:
    def test_only_one_heavy_worker(self, shared_root: Path):
        ready = mp.Event()
        release = mp.Event()
        proc = mp.Process(
            target=_hold_global,
            args=(str(shared_root), "dev", "run_a", ready, release),
        )
        proc.start()
        assert ready.wait(timeout=5)
        with pytest.raises(eg.GateError, match="busy"):
            eg.acquire_global_pipeline_lock(
                environment="prod",
                run_id="run_b",
                job_id="j2",
                shared_root=shared_root,
                blocking=False,
            )
        release.set()
        proc.join(timeout=10)
        assert proc.exitcode == 0

    def test_lock_owner_crash_releases_authority(self, shared_root: Path):
        handle = eg.acquire_global_pipeline_lock(
            environment="dev",
            run_id="crash",
            job_id="j_crash",
            shared_root=shared_root,
            blocking=True,
        )
        # Simulate process death: close the lock fd without an explicit unlock.
        # The kernel releases advisory locks when the last referencing fd is closed.
        os.close(handle.held.fd)
        handle.released = True
        recovered = eg.acquire_global_pipeline_lock(
            environment="prod",
            run_id="after_crash",
            job_id="j3",
            shared_root=shared_root,
            blocking=False,
        )
        recovered.release()

    def test_stale_gate_metadata_not_authoritative(self, shared_root: Path):
        status = shared_root / eg.STATUS_NAME
        status.write_text(
            json.dumps(
                {
                    "state": eg.GATE_PROD_ACTIVE,
                    "owning_environment": "prod",
                    "run_id": "stale",
                    "metadata_authoritative": True,
                }
            ),
            encoding="utf-8",
        )
        snap = eg.read_gate_status(shared_root=shared_root)
        assert snap.state == eg.GATE_FREE
        assert snap.metadata_authoritative is False


class TestRunPipelineSemantics:
    def _prepare(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, run_id: str = "run_x"
    ):
        import execution_lock as el
        import run_records as rr

        locks = tmp_path / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(locks))
        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.setattr(
            rp, "validate_config", lambda _e: (rp.EXIT_SUCCESS, "ok", object())
        )
        monkeypatch.setattr(
            rp, "check_boot_readiness", lambda _e: (rp.EXIT_SUCCESS, "READY")
        )
        monkeypatch.setattr(
            rp, "check_scheduled_runtime_gate", lambda _e, _t: ("proceed", "ok")
        )
        monkeypatch.setattr(el, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rp, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(rp, "write_config_snapshot", lambda *_a, **_k: None)
        run_dir = tmp_path / "runs" / "dev" / run_id
        run_dir.mkdir(parents=True)
        ctx = rp.PipelineRunContext(
            environment="dev",
            env_label="DEVELOPMENT",
            funnel_id="funnel_x",
            trigger="manual_cli",
            run_id=run_id,
            run_dir=run_dir,
            log_path=run_dir / "run.log",
        )
        monkeypatch.setattr(rp, "prepare_run_context", lambda *_a, **_k: ctx)
        return ctx

    def test_enqueue_alone_does_not_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        ctx = self._prepare(monkeypatch, tmp_path, run_id="run_wait")
        states: list[str] = []

        def fake_invoke(*_a, **_k):
            # Capture RUNNING before wait completes.
            record = json.loads((ctx.run_dir / "run_record.json").read_text(encoding="utf-8"))
            states.append(record["status"])
            return rp.EXIT_SUCCESS, "pipeline status=input_ready", "input_ready", ["job_1"]

        def fake_wait(*_a, **_k):
            record = json.loads((ctx.run_dir / "run_record.json").read_text(encoding="utf-8"))
            states.append(record["status"])
            return rp.EXIT_SUCCESS, "video jobs completed=1", 1, 1, 0

        monkeypatch.setattr(rp, "invoke_run_funnel", fake_invoke)
        monkeypatch.setattr(rp, "wait_for_video_jobs", fake_wait)
        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="manual_cli")
        assert code == rp.EXIT_SUCCESS
        assert "RUNNING" in states
        final = json.loads((ctx.run_dir / "run_record.json").read_text(encoding="utf-8"))
        assert final["status"] == "SUCCESS"

    def test_worker_failure_produces_failed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        ctx = self._prepare(monkeypatch, tmp_path, run_id="run_fail_job")
        monkeypatch.setattr(
            rp,
            "invoke_run_funnel",
            lambda *_a, **_k: (
                rp.EXIT_SUCCESS,
                "pipeline status=input_ready",
                "input_ready",
                ["job_bad"],
            ),
        )
        monkeypatch.setattr(
            rp,
            "wait_for_video_jobs",
            lambda *_a, **_k: (rp.EXIT_PIPELINE_FAIL, "video jobs failed=1", 1, 0, 1),
        )
        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="manual_cli")
        assert code == rp.EXIT_PIPELINE_FAIL
        final = json.loads((ctx.run_dir / "run_record.json").read_text(encoding="utf-8"))
        assert final["status"] == "FAIL"

    def test_gate_refuse_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shared_root: Path
    ):
        ctx = self._prepare(monkeypatch, tmp_path, run_id="run_refused")
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(shared_root))
        # Hold production turnstile exclusive so dev admission fails.
        monkeypatch.setenv("MK04_ENV", "prod")
        prod = eg.admit_orchestration(
            environment="prod",
            run_id="prod_block",
            trigger="test",
            shared_root=shared_root,
        )
        monkeypatch.setenv("MK04_ENV", "dev")
        code = rp.run_pipeline("dev", funnel_id="funnel_x", trigger="manual_cli")
        assert code == rp.EXIT_GATE_REFUSED
        final = json.loads((ctx.run_dir / "run_record.json").read_text(encoding="utf-8"))
        assert final["status"] == "SKIPPED"
        prod.release()


class TestLockFilePermissions:
    def test_open_lock_file_defeats_umask_root_0644(
        self, shared_root: Path
    ):
        """Root promotion with umask 022 must still yield group-writable locks."""
        import stat as statmod

        old = os.umask(0o022)
        try:
            path = shared_root / "promotion.lock"
            fd = eg._open_lock_file(path)
            try:
                mode = statmod.S_IMODE(os.fstat(fd).st_mode)
                assert mode == 0o0660, oct(mode)
                assert mode & 0o020, "group write required for O_RDWR by mk04 group"
                assert not (mode & 0o002), "must not be world-writable"
            finally:
                os.close(fd)
            eg._write_status(shared_root, {"state": "free", "detail": "test"})
            status = shared_root / eg.STATUS_NAME
            smode = statmod.S_IMODE(status.stat().st_mode)
            assert smode == 0o0660, oct(smode)
            assert not (smode & 0o002)
        finally:
            os.umask(old)

    def test_existing_0644_lock_hardened_on_reopen(self, shared_root: Path):
        import stat as statmod

        path = shared_root / "global_pipeline.lock"
        path.write_bytes(b"")
        os.chmod(path, 0o0644)
        fd = eg._open_lock_file(path)
        try:
            mode = statmod.S_IMODE(os.fstat(fd).st_mode)
            assert mode == 0o0660
        finally:
            os.close(fd)

    def test_non_owner_reopen_succeeds_when_harden_eperm(
        self, shared_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Group-authorized reopen: O_RDWR + flock wins; harden EPERM is not failure."""
        import errno
        import fcntl
        import stat as statmod

        path = shared_root / "promotion.lock"
        fd0 = eg._open_lock_file(path)
        os.close(fd0)
        assert statmod.S_IMODE(path.stat().st_mode) == 0o0660

        def eperm_harden(_fd: int, _path: Path) -> None:
            raise OSError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(eg, "_harden_lock_fd", eperm_harden)
        fd = eg._open_lock_file(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            mode = statmod.S_IMODE(os.fstat(fd).st_mode)
            assert mode & 0o020, "group write must remain"
            assert not (mode & 0o002)
        finally:
            os.close(fd)

    def test_harden_skips_chmod_when_not_file_owner(
        self, shared_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import stat as statmod
        from types import SimpleNamespace

        path = shared_root / "global_pipeline.lock"
        fd0 = eg._open_lock_file(path)
        os.close(fd0)

        real_fstat = os.fstat
        calls: list[str] = []

        def fake_fstat(fd: int):
            st = real_fstat(fd)
            return SimpleNamespace(
                st_uid=os.geteuid() + 1,
                st_gid=st.st_gid,
                st_mode=st.st_mode,
            )

        def track_fchmod(*_a, **_k):
            calls.append("fchmod")
            raise AssertionError("non-owner must not fchmod")

        monkeypatch.setattr(os, "fstat", fake_fstat)
        monkeypatch.setattr(os, "fchmod", track_fchmod)
        fd = os.open(str(path), os.O_RDWR)
        try:
            eg._harden_lock_fd(fd, path)
            assert calls == []
            assert statmod.S_IMODE(path.stat().st_mode) == 0o0660
        finally:
            os.close(fd)

    def test_unreadable_lock_fail_closed(self, shared_root: Path):
        path = shared_root / "global_pipeline.lock"
        path.write_bytes(b"")
        os.chmod(path, 0o0000)
        try:
            with pytest.raises(eg.GateError, match="cannot open lock file"):
                eg._open_lock_file(path)
        finally:
            os.chmod(path, 0o0660)

    def test_status_replace_preserves_group_write_no_world_write(self, shared_root: Path):
        import stat as statmod

        old = os.umask(0o022)
        try:
            eg._write_status(shared_root, {"state": "free", "detail": "first"})
            eg._write_status(shared_root, {"state": "development_active", "detail": "second"})
            status = shared_root / eg.STATUS_NAME
            mode = statmod.S_IMODE(status.stat().st_mode)
            assert mode == 0o0660, oct(mode)
            assert mode & 0o020
            assert not (mode & 0o002)
            assert "development_active" in status.read_text(encoding="utf-8")
        finally:
            os.umask(old)
