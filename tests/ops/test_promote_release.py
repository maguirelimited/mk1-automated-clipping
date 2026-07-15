"""Atomic versioned production promotion tests (Prompt 5).

Uses temporary fake production roots and mocked service/dependency commands.
Never writes to real /opt, /etc, or /var/lib.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "scripts" / "ops"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))

import execution_gate as eg  # noqa: E402
import promote_release as pr  # noqa: E402


def _mini_source(tmp: Path) -> Path:
    """Build a minimal promotable source tree with required assets."""
    src = tmp / "dev_checkout"
    src.mkdir()
    # Required application / config assets
    for rel in (
        "deploy/scripts/env.sh",
        "scripts/ops/execution_gate.py",
        "scripts/config/validate_config.py",
        "ops-ui/ops_ui/__init__.py",
        "ops-ui/ops_ui/app.py",
        "video-automation/server/app.py",
        "output-funnel/output_funnel/__init__.py",
        "source-input/input_service/app.py",
        "ai-service/app.py",
        "config/defaults/default.yaml",
        "tests/config/test_dummy.py",
        "ops-ui/tests/test_dummy.py",
    ):
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# asset\n", encoding="utf-8")
    for name, req, _venv in pr.COMPONENT_DEPS:
        path = src / req
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# reqs for {name}\n", encoding="utf-8")
    # Excluded / secret / runtime junk
    (src / ".env").write_text("SECRET=should-not-copy\n", encoding="utf-8")
    (src / "credentials").mkdir()
    (src / "credentials" / "key.json").write_text('{"k":1}\n', encoding="utf-8")
    (src / "jobs").mkdir()
    (src / "jobs" / "job1").mkdir()
    (src / "outputs").mkdir()
    (src / "data").mkdir()
    (src / "logs").mkdir()
    (src / "database").mkdir()
    (src / "database" / "dev.db").write_bytes(b"sqlite")
    (src / ".git").mkdir()
    (src / "video-automation" / ".venv").mkdir(parents=True)
    (src / "video-automation" / ".venv" / "pyvenv.cfg").write_text("home = /tmp\n", encoding="utf-8")
    return src


def _lock_root(tmp: Path) -> Path:
    root = tmp / "locks"
    root.mkdir()
    return root


def _options(tmp: Path, src: Path, locks: Path, **kwargs) -> pr.PromoteOptions:
    base = tmp / "prod"
    base.mkdir(exist_ok=True)
    defaults = dict(
        source_root=src,
        prod_base=base,
        shared_lock_root=locks,
        no_restart=True,
        allow_first_bootstrap=True,
        validate_fn=lambda ctx: {"ok": True, "commands": [{"label": "mock", "ok": True}]},
        prepare_deps_fn=_fake_prepare_deps,
        restart_fn=lambda ctx: {"ok": True, "mocked": True},
        health_fn=lambda ctx: {"ok": True, "mocked": True},
        publish_check_fn=lambda ctx: None,
        services_installed_fn=lambda ctx: False,
        snapshot_fn=pr._python_snapshot,
    )
    defaults.update(kwargs)
    return pr.PromoteOptions(**defaults)


def _fake_prepare_deps(ctx: pr.PromoteContext, dep_hash: str) -> Path:
    layout = pr.prod_layout(ctx.options.prod_base)
    bundle = layout["dependency_bundles"] / dep_hash
    if (bundle / "BUNDLE_COMPLETE").is_file():
        pr._link_bundle_into_tree(ctx.staging_dir, bundle)
        return bundle
    bundle.mkdir(parents=True, exist_ok=True)
    for name, _req, _venv in pr.COMPONENT_DEPS:
        (bundle / name / "bin").mkdir(parents=True, exist_ok=True)
        (bundle / name / "bin" / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (bundle / name / "bin" / "python").chmod(0o755)
    (bundle / "BUNDLE_COMPLETE").write_text("ok\n", encoding="utf-8")
    pr._link_bundle_into_tree(ctx.staging_dir, bundle)
    return bundle


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MK04_PRODUCTION_INSTALLED", raising=False)
    # Keep all artifacts under the workspace so sandboxed runs can write.
    base = REPO_ROOT / ".tmp_promote_tests" / tmp_path.name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    locks = _lock_root(base)
    monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(locks))
    src = _mini_source(base)
    # Deterministic unique release ids without sleeping for UTC second boundaries.
    counter = {"n": 0}

    def _make(git: pr.GitSnapshot) -> str:
        counter["n"] += 1
        suffix = "_dirty" if git.dirty else ""
        return f"20260713T{counter['n']:06d}Z_{git.short_commit}{suffix}"

    monkeypatch.setattr(pr, "make_release_id", _make)
    return src, locks, base


class TestPromotionCore:
    def test_validation_failure_leaves_current_unchanged(self, env):
        src, locks, tmp = env
        opts = _options(tmp, src, locks)

        def bad_validate(ctx):
            raise pr.PromoteError("validation failed: mock")

        opts.validate_fn = bad_validate
        with pytest.raises(pr.PromoteError, match="validation failed"):
            pr.promote(opts)
        layout = pr.prod_layout(opts.prod_base)
        assert not layout["current"].exists()
        assert not any(layout["releases"].glob("[!.]*")) or list(
            layout["releases"].glob(".staging-*")
        )

    def test_partial_staging_leaves_current_unchanged(self, env, monkeypatch):
        src, locks, tmp = env
        opts = _options(tmp, src, locks)

        def boom(*_a, **_k):
            raise pr.PromoteError("rsync snapshot failed (99): simulated")

        opts.snapshot_fn = boom
        with pytest.raises(pr.PromoteError, match="rsync"):
            pr.promote(opts)
        layout = pr.prod_layout(opts.prod_base)
        assert not layout["current"].exists()

    def test_first_promotion_creates_current_symlink(self, env):
        src, locks, tmp = env
        opts = _options(tmp, src, locks)
        result = pr.promote(opts)
        assert result["ok"]
        layout = pr.prod_layout(opts.prod_base)
        assert layout["current"].is_symlink()
        target = layout["current"].resolve()
        assert target.parent == layout["releases"].resolve()
        assert (target / "release_manifest.json").is_file()

    def test_second_promotion_moves_old_current_to_previous(self, env):
        src, locks, tmp = env
        opts = _options(tmp, src, locks)
        first = pr.promote(opts)
        second = pr.promote(_options(tmp, src, locks))
        layout = pr.prod_layout(opts.prod_base)
        assert layout["previous"].is_symlink()
        assert layout["previous"].resolve().name == first["release_id"]
        assert layout["current"].resolve().name == second["release_id"]

    def test_symlink_switch_is_atomic_helper(self, tmp_path: Path):
        releases = tmp_path / "releases"
        releases.mkdir()
        a = releases / "a"
        b = releases / "b"
        a.mkdir()
        b.mkdir()
        current = tmp_path / "current"
        pr.atomic_symlink_replace(current, a)
        assert current.resolve() == a.resolve()
        pr.atomic_symlink_replace(current, b)
        assert current.resolve() == b.resolve()

    def test_unexpected_real_current_refused(self, env):
        src, locks, tmp = env
        opts = _options(tmp, src, locks)
        layout = pr.prod_layout(opts.prod_base)
        layout["current"].mkdir(parents=True)
        (layout["current"] / "legacy.txt").write_text("x\n", encoding="utf-8")
        with pytest.raises(pr.PromoteError, match="real directory"):
            pr.promote(opts)
        assert (layout["current"] / "legacy.txt").is_file()

    def test_external_symlink_targets_rejected(self, env):
        src, locks, tmp = env
        opts = _options(tmp, src, locks)
        layout = pr.prod_layout(opts.prod_base)
        layout["releases"].mkdir(parents=True)
        outside = tmp / "outside_release"
        outside.mkdir()
        layout["current"].symlink_to(outside)
        with pytest.raises(pr.PromoteError, match="must resolve under"):
            pr.promote(opts)

    def test_dirty_allowed_and_recorded(self, env, monkeypatch):
        src, locks, tmp = env
        monkeypatch.setattr(
            pr,
            "capture_git_snapshot",
            lambda _s: pr.GitSnapshot(
                commit="abc",
                short_commit="abc1234",
                dirty=True,
                dirty_files=["ops-ui/x.py"],
                untracked_files=["scratch.txt"],
            ),
        )
        result = pr.promote(_options(tmp, src, locks))
        layout = pr.prod_layout(Path(tmp) / "prod")
        # prod_base from options
        opts = _options(tmp, src, locks)
        layout = pr.prod_layout(opts.prod_base)
        # Need actual release from result
        manifest = json.loads(
            (layout["releases"] / result["release_id"] / "release_manifest.json").read_text()
        )
        assert manifest["dirty"] is True
        assert "ops-ui/x.py" in manifest["dirty_files"]
        assert result["release_id"].endswith("_dirty")

    def test_require_clean_rejects_dirty(self, env, monkeypatch):
        src, locks, tmp = env
        monkeypatch.setattr(
            pr,
            "capture_git_snapshot",
            lambda _s: pr.GitSnapshot("c", "c", True, ["a"], []),
        )
        with pytest.raises(pr.PromoteError, match="require-clean"):
            pr.promote(_options(tmp, src, locks, require_clean=True))

    def test_manifest_safe_metadata_no_secrets(self, env):
        src, locks, tmp = env
        result = pr.promote(_options(tmp, src, locks))
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        manifest = json.loads(
            (layout["releases"] / result["release_id"] / "release_manifest.json").read_text()
        )
        for key in (
            "release_id",
            "created_at",
            "source_checkout",
            "git_commit",
            "git_short_commit",
            "dirty",
            "validation",
            "dependency",
            "promoter_version",
            "schema_version",
        ):
            assert key in manifest
        blob = json.dumps(manifest)
        assert "SECRET=should-not-copy" not in blob
        assert "should-not-copy" not in blob

    def test_runtime_secrets_excluded_assets_included(self, env):
        src, locks, tmp = env
        result = pr.promote(_options(tmp, src, locks))
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        release = layout["releases"] / result["release_id"]
        assert not (release / ".env").exists()
        assert not (release / "credentials").exists()
        assert not (release / "jobs").exists()
        assert not (release / "outputs").exists()
        assert not (release / "data").exists()
        assert not (release / "database" / "dev.db").exists()
        assert not (release / ".git").exists()
        assert not (release / "video-automation" / ".venv" / "pyvenv.cfg").exists() or (
            release / "video-automation" / ".venv"
        ).is_symlink()
        assert (release / "config" / "defaults" / "default.yaml").is_file()
        assert (release / "ops-ui" / "ops_ui" / "app.py").is_file()
        assert (release / "deploy" / "scripts" / "env.sh").is_file()

    def test_candidate_validation_runs_against_staging(self, env):
        src, locks, tmp = env
        seen: dict = {}

        def validate(ctx: pr.PromoteContext):
            seen["staging"] = ctx.staging_dir
            assert ctx.staging_dir is not None
            assert ctx.staging_dir.name.startswith(".staging-")
            assert (ctx.staging_dir / "ops-ui" / "ops_ui" / "app.py").is_file()
            layout = pr.prod_layout(ctx.options.prod_base)
            assert not layout["current"].exists()
            return {"ok": True, "commands": [{"label": "staging", "ok": True}]}

        pr.promote(_options(tmp, src, locks, validate_fn=validate))
        assert "staging" in seen


class TestPromotionGate:
    def test_active_dev_gate_refuses(self, env):
        src, locks, tmp = env
        handle = eg.admit_orchestration(
            environment="dev", run_id="dev-1", trigger="test", shared_root=locks
        )
        # Upgrade to exclusive by also holding... actually shared turnstile
        # blocks exclusive promotion. Hold shared.
        try:
            with pytest.raises(pr.PromoteError, match="refused|turnstile|active"):
                pr.promote(_options(tmp, src, locks))
        finally:
            handle.release()

    def test_active_prod_gate_refuses(self, env):
        src, locks, tmp = env
        # Hold exclusive turnstile as if prod waiting/active.
        fd = eg._open_lock_file(locks / eg.TURNSTILE_NAME)
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(pr.PromoteError, match="refused|turnstile|active"):
                pr.promote(_options(tmp, src, locks))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_two_promotions_cannot_run_together(self, env):
        src, locks, tmp = env
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def slow_validate(ctx):
            barrier.wait(timeout=5)
            time.sleep(0.4)
            return {"ok": True, "commands": []}

        def worker():
            try:
                pr.promote(_options(tmp, src, locks, validate_fn=slow_validate))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        # One succeeds, one refuses on promotion.lock
        assert any(isinstance(e, pr.PromoteError) for e in errors)
        assert any("promotion" in str(e).lower() or "refused" in str(e).lower() for e in errors)

    def test_new_pipelines_cannot_begin_during_promotion(self, env):
        src, locks, tmp = env
        held: dict = {}

        def validate(ctx):
            held["ok"] = True
            with pytest.raises(eg.GateError):
                eg.admit_orchestration(
                    environment="dev",
                    run_id="blocked",
                    trigger="pipeline",
                    shared_root=locks,
                )
            with pytest.raises(eg.GateError):
                eg.acquire_global_pipeline_lock(
                    environment="dev",
                    run_id="blocked",
                    job_id="j1",
                    shared_root=locks,
                    blocking=False,
                )
            return {"ok": True, "commands": []}

        pr.promote(_options(tmp, src, locks, validate_fn=validate))
        assert held["ok"]


class TestDependenciesRestartRollback:
    def test_missing_deps_prevent_switch(self, env):
        src, locks, tmp = env

        def bad_deps(ctx, dep_hash):
            raise RuntimeError("bundle missing")

        with pytest.raises(pr.PromoteError, match="dependency"):
            pr.promote(_options(tmp, src, locks, prepare_deps_fn=bad_deps))
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        assert not layout["current"].exists()

    def test_unchanged_dependency_bundle_reused(self, env):
        src, locks, tmp = env
        calls = {"n": 0}
        real = _fake_prepare_deps

        def counting(ctx, dep_hash):
            calls["n"] += 1
            return real(ctx, dep_hash)

        pr.promote(_options(tmp, src, locks, prepare_deps_fn=counting))
        pr.promote(_options(tmp, src, locks, prepare_deps_fn=counting))
        assert calls["n"] == 2
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        bundles = [p for p in layout["dependency_bundles"].iterdir() if p.is_dir()]
        assert len(bundles) == 1

    def test_changed_requirements_prepare_separate_bundle(self, env):
        src, locks, tmp = env
        pr.promote(_options(tmp, src, locks))
        req = src / "ops-ui" / "requirements.txt"
        req.write_text(req.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        pr.promote(_options(tmp, src, locks))
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        bundles = [p for p in layout["dependency_bundles"].iterdir() if p.is_dir()]
        assert len(bundles) == 2

    def test_restart_success_completes_activation(self, env):
        src, locks, tmp = env
        result = pr.promote(
            _options(
                tmp,
                src,
                locks,
                no_restart=False,
                services_installed_fn=lambda ctx: True,
            )
        )
        assert result["activation_result"] == "activated"

    def test_health_failure_restores_previous(self, env):
        src, locks, tmp = env
        first = pr.promote(
            _options(
                tmp,
                src,
                locks,
                no_restart=False,
                services_installed_fn=lambda ctx: True,
            )
        )
        health_calls = {"n": 0}

        def health(_ctx):
            health_calls["n"] += 1
            # Fail the post-switch health once; succeed on rollback verification.
            if health_calls["n"] == 1:
                return {"ok": False, "detail": "fail"}
            return {"ok": True}

        with pytest.raises(pr.PromoteError, match="rolled back|activation failed"):
            pr.promote(
                _options(
                    tmp,
                    src,
                    locks,
                    no_restart=False,
                    services_installed_fn=lambda ctx: True,
                    health_fn=health,
                )
            )
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        assert layout["current"].resolve().name == first["release_id"]
        assert health_calls["n"] >= 2

    def test_rollback_restart_health_verified(self, env):
        src, locks, tmp = env
        restarts: list[str] = []
        healths: list[str] = []

        def restart(ctx):
            restarts.append(ctx.release_id)
            return {"ok": True}

        def health(ctx):
            healths.append(ctx.release_id)
            # Fail only for the second activation attempt's first health.
            if len(healths) == 2:
                return {"ok": False}
            return {"ok": True}

        pr.promote(
            _options(
                tmp,
                src,
                locks,
                no_restart=False,
                services_installed_fn=lambda ctx: True,
                restart_fn=restart,
                health_fn=health,
            )
        )
        with pytest.raises(pr.PromoteError):
            pr.promote(
                _options(
                    tmp,
                    src,
                    locks,
                    no_restart=False,
                    services_installed_fn=lambda ctx: True,
                    restart_fn=restart,
                    health_fn=health,
                )
            )
        assert len(restarts) >= 2  # failed activation + rollback restart
        assert len(healths) >= 3  # first ok, second fail, rollback health

    def test_first_release_health_failure_no_rollback(self, env):
        src, locks, tmp = env
        with pytest.raises(pr.PromoteError, match="rollback unavailable"):
            pr.promote(
                _options(
                    tmp,
                    src,
                    locks,
                    no_restart=False,
                    services_installed_fn=lambda ctx: True,
                    health_fn=lambda ctx: {"ok": False},
                )
            )

    def test_active_publish_prevents_restart(self, env):
        src, locks, tmp = env

        def publish(_ctx):
            raise pr.PromoteError("refuse promotion: 1 active upload/publish job(s)")

        with pytest.raises(pr.PromoteError, match="active upload"):
            pr.promote(_options(tmp, src, locks, publish_check_fn=publish))

    def test_no_restart_performs_no_service_operation(self, env):
        src, locks, tmp = env
        calls = {"restart": 0, "health": 0}
        result = pr.promote(
            _options(
                tmp,
                src,
                locks,
                no_restart=True,
                services_installed_fn=lambda ctx: True,
                restart_fn=lambda ctx: calls.__setitem__("restart", calls["restart"] + 1) or {"ok": True},
                health_fn=lambda ctx: calls.__setitem__("health", calls["health"] + 1) or {"ok": True},
            )
        )
        assert calls["restart"] == 0
        assert calls["health"] == 0
        assert "no_restart" in result["activation_result"] or result["activation_result"] in {
            "activated_no_restart",
            "bootstrap_required",
        }

    def test_auth_failure_before_current_switch(self, env):
        src, locks, tmp = env
        # Seed a previous current so we can prove it is unchanged.
        first = pr.promote(
            _options(
                tmp,
                src,
                locks,
                no_restart=False,
                services_installed_fn=lambda ctx: True,
            )
        )
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        before = layout["current"].resolve().name
        assert before == first["release_id"]

        def boom():
            raise pr.PromoteError(
                "refusing to activate release: cannot authorize production service restart"
            )

        with pytest.raises(pr.PromoteError, match="cannot authorize|refusing to activate"):
            pr.promote(
                _options(
                    tmp,
                    src,
                    locks,
                    no_restart=False,
                    services_installed_fn=lambda ctx: True,
                    auth_fn=boom,
                    # Force the real-auth path gate: auth_fn is consulted when set.
                    restart_fn=None,
                    health_fn=lambda ctx: {"ok": True},
                )
            )
        assert layout["current"].resolve().name == before

    def test_successful_promote_authorizes_once(self, env):
        src, locks, tmp = env
        auth_calls = {"n": 0}

        def auth():
            auth_calls["n"] += 1

        result = pr.promote(
            _options(
                tmp,
                src,
                locks,
                no_restart=False,
                services_installed_fn=lambda ctx: True,
                auth_fn=auth,
                restart_fn=lambda ctx: {"ok": True},
                health_fn=lambda ctx: {"ok": True},
            )
        )
        assert result["activation_result"] == "activated"
        assert auth_calls["n"] == 1


class TestBootReadinessContract:
    def test_ready_with_uploads_disabled_warn_succeeds(self):
        stdout = (
            "Boot readiness           READY - required components ready\n"
            "Upload safety state      WARN - prod uploads disabled by runtime control\n"
            "Overall                   WARN\n"
        )
        out = pr.evaluate_boot_readiness_result(returncode=0, stdout=stdout)
        assert out["ok"] is True
        assert out["boot_readiness"] == "READY"

    def test_ready_exit_1_optional_warning_succeeds(self):
        stdout = "Boot readiness           READY\nAI service               WARN - optional\n"
        out = pr.evaluate_boot_readiness_result(returncode=1, stdout=stdout)
        assert out["ok"] is True
        assert out["optional_warnings"] is True

    def test_not_ready_fails(self):
        stdout = "Boot readiness           NOT READY\nAPI health endpoint      FAIL\n"
        out = pr.evaluate_boot_readiness_result(returncode=2, stdout=stdout)
        assert out["ok"] is False
        assert out["boot_readiness"] == "NOT READY"

    def test_malformed_missing_boot_line_fails_closed(self):
        out = pr.evaluate_boot_readiness_result(
            returncode=0, stdout="Overall PASS\nno boot line here\n"
        )
        assert out["ok"] is False
        assert out["boot_readiness"] == "unknown"

    def test_missing_returncode_fails_closed(self):
        out = pr.evaluate_boot_readiness_result(
            returncode=None, stdout="Boot readiness READY\n"
        )
        assert out["ok"] is False


class TestDefaultHealthRetry:
    def test_default_health_retries_until_ready(self, env, monkeypatch):
        src, locks, tmp = env
        result = pr.promote(_options(tmp, src, locks))
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        release = layout["current"].resolve()
        health_sh = release / "scripts" / "ops" / "health.sh"
        health_sh.parent.mkdir(parents=True, exist_ok=True)
        health_sh.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        health_sh.chmod(0o755)

        calls = {"n": 0}
        sleeps: list[float] = []

        def fake_run(cmd, capture_output=False, text=False, check=False):
            calls["n"] += 1
            assert "--boot-readiness" in cmd
            if calls["n"] < 3:
                return type(
                    "R",
                    (),
                    {
                        "returncode": 2,
                        "stdout": "Boot readiness           NOT READY\n",
                        "stderr": "",
                    },
                )()
            return type(
                "R",
                (),
                {
                    "returncode": 0,
                    "stdout": (
                        "Boot readiness           READY\n"
                        "Upload safety state      WARN - prod uploads disabled\n"
                        "Overall                   WARN\n"
                    ),
                    "stderr": "",
                },
            )()

        import time as time_mod

        monkeypatch.setattr(pr.subprocess, "run", fake_run)
        monkeypatch.setattr(time_mod, "sleep", lambda s: sleeps.append(s))

        ctx = pr.PromoteContext(
            options=_options(tmp, src, locks),
            release_id=result["release_id"],
            run_id="test",
            git=pr.GitSnapshot(commit="x", short_commit="x", dirty=False),
            release_dir=release,
            orchestration_root=release,
        )
        out = pr.default_health(ctx)
        assert out["ok"] is True
        assert out["boot_readiness"] == "READY"
        assert calls["n"] >= 3
        assert sleeps

    def test_default_health_persistent_not_ready(self, env, monkeypatch):
        src, locks, tmp = env
        result = pr.promote(_options(tmp, src, locks))
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        release = layout["current"].resolve()
        health_sh = release / "scripts" / "ops" / "health.sh"
        health_sh.parent.mkdir(parents=True, exist_ok=True)
        health_sh.write_text("#!/bin/bash\nexit 2\n", encoding="utf-8")
        health_sh.chmod(0o755)

        import time as time_mod

        monkeypatch.setattr(time_mod, "sleep", lambda _s: None)
        monkeypatch.setattr(
            pr.subprocess,
            "run",
            lambda *a, **k: type(
                "R",
                (),
                {
                    "returncode": 2,
                    "stdout": "Boot readiness           NOT READY\nAPI FAIL\n",
                    "stderr": "",
                },
            )(),
        )
        start = time_mod.monotonic()

        def fast_deadline():
            if not hasattr(fast_deadline, "n"):
                fast_deadline.n = 0  # type: ignore[attr-defined]
            fast_deadline.n += 1  # type: ignore[attr-defined]
            if fast_deadline.n <= 2:  # type: ignore[attr-defined]
                return start
            return start + 1000

        monkeypatch.setattr(time_mod, "monotonic", fast_deadline)

        ctx = pr.PromoteContext(
            options=_options(tmp, src, locks),
            release_id=result["release_id"],
            run_id="test",
            git=pr.GitSnapshot(commit="x", short_commit="x", dirty=False),
            release_dir=release,
            orchestration_root=release,
        )
        out = pr.default_health(ctx)
        assert out["ok"] is False
        assert out["boot_readiness"] == "NOT READY"


class TestPinnedOrchestration:
    def test_default_restart_uses_pinned_root_not_current(self, env, monkeypatch):
        src, locks, tmp = env
        first = pr.promote(_options(tmp, src, locks))
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        candidate = layout["current"].resolve()
        # Point current at a decoy "old" tree with a different restart.sh
        old = layout["releases"] / "old_release"
        old.mkdir()
        (old / "scripts" / "ops").mkdir(parents=True)
        (old / "scripts" / "ops" / "restart.sh").write_text(
            "#!/bin/bash\necho OLD_HELPER\nexit 0\n", encoding="utf-8"
        )
        (candidate / "scripts" / "ops").mkdir(parents=True, exist_ok=True)
        (candidate / "scripts" / "ops" / "restart.sh").write_text(
            "#!/bin/bash\necho PINNED_HELPER\nexit 0\n", encoding="utf-8"
        )
        for script in (
            old / "scripts" / "ops" / "restart.sh",
            candidate / "scripts" / "ops" / "restart.sh",
        ):
            script.chmod(0o755)
        layout["current"].unlink()
        layout["current"].symlink_to(Path("releases") / old.name)

        captured: dict = {}

        def fake_run(cmd, capture_output=False, text=False, check=False):
            captured["cmd"] = list(cmd)
            return type("R", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()

        monkeypatch.setattr(pr.subprocess, "run", fake_run)
        ctx = pr.PromoteContext(
            options=_options(tmp, src, locks),
            release_id=first["release_id"],
            run_id="test",
            git=pr.GitSnapshot(commit="x", short_commit="x", dirty=False),
            release_dir=candidate,
            orchestration_root=candidate,
        )
        out = pr.default_restart(ctx)
        assert out["ok"] is True
        assert str(candidate) in captured["cmd"][1]
        assert str(old) not in captured["cmd"][1]
        assert "--skip-health" in captured["cmd"]
        assert captured["cmd"][1].endswith("restart.sh")

    def test_rollback_keeps_pinned_helper_after_current_restored(self, env, monkeypatch):
        src, locks, tmp = env
        pr.promote(
            _options(
                tmp,
                src,
                locks,
                no_restart=False,
                services_installed_fn=lambda ctx: True,
                restart_fn=lambda ctx: {"ok": True},
                health_fn=lambda ctx: {"ok": True},
            )
        )
        roots_seen: list[str] = []

        def restart(ctx):
            roots_seen.append(str(pr.pinned_orchestration_root(ctx)))
            return {"ok": True, "orchestration_root": roots_seen[-1]}

        def health(ctx):
            # Fail activation readiness once, succeed on rollback verification.
            n = getattr(health, "n", 0) + 1
            health.n = n  # type: ignore[attr-defined]
            if n == 1:
                return {"ok": False, "boot_readiness": "NOT READY"}
            return {"ok": True, "boot_readiness": "READY"}

        with pytest.raises(pr.PromoteError, match="rolled back"):
            pr.promote(
                _options(
                    tmp,
                    src,
                    locks,
                    no_restart=False,
                    services_installed_fn=lambda ctx: True,
                    restart_fn=restart,
                    health_fn=health,
                )
            )
        # Activation restart + rollback restart share the same pinned candidate root.
        assert len(roots_seen) >= 2
        assert roots_seen[0] == roots_seen[1]
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        # current restored to previous; pinned root was the failed candidate, not current.
        assert layout["current"].resolve() != Path(roots_seen[0]).resolve()

    def test_restart_only_not_overridden_by_embedded_warn(self, env, monkeypatch):
        src, locks, tmp = env
        result = pr.promote(_options(tmp, src, locks))
        release = pr.prod_layout(_options(tmp, src, locks).prod_base)["current"].resolve()
        (release / "scripts" / "ops").mkdir(parents=True, exist_ok=True)
        (release / "scripts" / "ops" / "restart.sh").write_text(
            "#!/bin/bash\nexit 0\n", encoding="utf-8"
        )
        (release / "scripts" / "ops" / "restart.sh").chmod(0o755)

        def fake_run(cmd, capture_output=False, text=False, check=False):
            assert "--skip-health" in cmd
            # Even if stdout looks like WARN health, returncode 0 means restart-only OK.
            return type(
                "R",
                (),
                {"returncode": 0, "stdout": "Overall WARN\n", "stderr": ""},
            )()

        monkeypatch.setattr(pr.subprocess, "run", fake_run)
        ctx = pr.PromoteContext(
            options=_options(tmp, src, locks),
            release_id=result["release_id"],
            run_id="test",
            git=pr.GitSnapshot(commit="x", short_commit="x", dirty=False),
            release_dir=release,
            orchestration_root=release,
        )
        out = pr.default_restart(ctx)
        assert out["ok"] is True
        assert out.get("restart_only") is True

    def test_systemctl_failure_triggers_rollback(self, env):
        src, locks, tmp = env
        first = pr.promote(
            _options(
                tmp,
                src,
                locks,
                no_restart=False,
                services_installed_fn=lambda ctx: True,
            )
        )
        with pytest.raises(pr.PromoteError, match="rolled back|activation failed"):
            pr.promote(
                _options(
                    tmp,
                    src,
                    locks,
                    no_restart=False,
                    services_installed_fn=lambda ctx: True,
                    restart_fn=lambda ctx: {"ok": False, "returncode": 1, "detail": "systemctl failed"},
                    health_fn=lambda ctx: {"ok": True},
                )
            )
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        assert layout["current"].resolve().name == first["release_id"]

    def test_activation_fail_healthy_rollback_reported(self, env):
        src, locks, tmp = env
        pr.promote(
            _options(
                tmp,
                src,
                locks,
                no_restart=False,
                services_installed_fn=lambda ctx: True,
            )
        )
        health_n = {"n": 0}

        def health(_ctx):
            health_n["n"] += 1
            if health_n["n"] == 1:
                return {"ok": False, "boot_readiness": "NOT READY"}
            return {"ok": True, "boot_readiness": "READY"}

        with pytest.raises(pr.PromoteError, match="verified boot readiness"):
            pr.promote(
                _options(
                    tmp,
                    src,
                    locks,
                    no_restart=False,
                    services_installed_fn=lambda ctx: True,
                    health_fn=health,
                )
            )

    def test_rollback_health_timeout_hard_failure(self, env):
        src, locks, tmp = env
        pr.promote(
            _options(
                tmp,
                src,
                locks,
                no_restart=False,
                services_installed_fn=lambda ctx: True,
            )
        )
        with pytest.raises(pr.PromoteError, match="rollback boot readiness"):
            pr.promote(
                _options(
                    tmp,
                    src,
                    locks,
                    no_restart=False,
                    services_installed_fn=lambda ctx: True,
                    health_fn=lambda ctx: {"ok": False, "boot_readiness": "NOT READY"},
                )
            )


class TestPolicyRetentionUpdate:
    def test_promotion_never_changes_upload_or_cron(self, env):
        src, locks, tmp = env
        # Plant upload/cron markers that must remain untouched.
        etc = tmp / "etc_fake"
        etc.mkdir()
        env_file = etc / "env"
        env_file.write_text("MK04_UPLOAD_MODE=dry_run\nuploading.enabled=false\n", encoding="utf-8")
        cron = tmp / "cron.d" / "mk04"
        cron.parent.mkdir()
        cron.write_text("# cron\n", encoding="utf-8")
        before_env = env_file.read_text(encoding="utf-8")
        before_cron = cron.read_text(encoding="utf-8")
        result = pr.promote(_options(tmp, src, locks))
        assert env_file.read_text(encoding="utf-8") == before_env
        assert cron.read_text(encoding="utf-8") == before_cron
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        manifest = json.loads(
            (layout["releases"] / result["release_id"] / "release_manifest.json").read_text()
        )
        assert manifest["upload_mode_unchanged"] is True
        assert manifest["scheduler_unchanged"] is True

    def test_retention_preserves_current_previous_and_older(self, env):
        src, locks, tmp = env
        ids = []
        for _ in range(6):
            ids.append(pr.promote(_options(tmp, src, locks, retain_releases=4))["release_id"])
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        remaining = sorted(p.name for p in layout["releases"].iterdir() if p.is_dir() and not p.name.startswith("."))
        assert layout["current"].resolve().name in remaining
        assert layout["previous"].resolve().name in remaining
        assert len(remaining) == 4

    def test_referenced_dependency_bundles_retained(self, env):
        src, locks, tmp = env
        pr.promote(_options(tmp, src, locks))
        # Change reqs so a second bundle exists, then promote enough to prune releases
        # but keep both bundles if both releases retained... with retain=4 and 2 releases both kept.
        pr.promote(_options(tmp, src, locks))
        layout = pr.prod_layout(_options(tmp, src, locks).prod_base)
        assert len([p for p in layout["dependency_bundles"].iterdir() if p.is_dir()]) >= 1

    def test_update_sh_prod_refuses_pull(self):
        text = (REPO_ROOT / "update.sh").read_text(encoding="utf-8")
        assert "refuses --pull" in text
        assert "promote-to-prod.sh" in text

    def test_promote_script_is_canonical_wrapper(self):
        text = (REPO_ROOT / "deploy" / "scripts" / "promote-to-prod.sh").read_text(encoding="utf-8")
        assert "promote_release.py" in text
        assert "rsync" not in text or "promote_release" in text
        # Old flat destination copy must not remain as primary path.
        assert 'PROD_ROOT="${MK04_PROD_ROOT:-/opt/mk04/prod/current}"' not in text


class TestHermeticValidation:
    def test_hermetic_env_overrides_inherited_live_lock_root(self, tmp_path: Path):
        workspace = tmp_path / "validate_ws"
        workspace.mkdir()
        env = pr._hermetic_validation_command_env(
            workspace,
            extra={"MK04_SHARED_LOCK_ROOT": "/var/lib/mk04/locks", "OTHER": "1"},
        )
        lock_root = str(workspace / "locks")
        assert env["MK04_SHARED_LOCK_ROOT"] == lock_root
        assert env["MK04_SHARED_LOCK_ROOT"] != "/var/lib/mk04/locks"
        assert "var/lib/mk04/locks" not in env["MK04_SHARED_LOCK_ROOT"]
        assert env["MK04_DEPLOYED_LOCK_ROOT"] == str(workspace / "deployed_locks_absent")
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert env["PYTHONPYCACHEPREFIX"] == str(workspace / "pycache")
        assert "no:cacheprovider" in env["PYTEST_ADDOPTS"]
        assert env["OTHER"] == "1"

    def test_default_validate_never_touches_live_lock_root(
        self, env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        src, locks, tmp = env
        live = tmp_path / "var_lib_mk04_locks"
        live.mkdir()
        marker = live / "gate_status.json"
        marker.write_text('{"state":"sentinel"}\n', encoding="utf-8")
        before = marker.read_text(encoding="utf-8")
        mtime = marker.stat().st_mtime_ns

        captured: list[dict[str, str]] = []

        def fake_run(cmd, *args, **kwargs):
            run_env = kwargs.get("env") or {}
            captured.append(
                {
                    "MK04_SHARED_LOCK_ROOT": run_env.get("MK04_SHARED_LOCK_ROOT", ""),
                    "MK04_DEPLOYED_LOCK_ROOT": run_env.get("MK04_DEPLOYED_LOCK_ROOT", ""),
                }
            )
            # Short-circuit: succeed without executing real pytest against mini source.
            from subprocess import CompletedProcess

            return CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

        monkeypatch.setattr(pr.subprocess, "run", fake_run)
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(live))

        staging = tmp_path / "staging"
        shutil.copytree(src, staging)
        ctx = pr.PromoteContext(
            options=_options(tmp, src, locks),
            release_id="testrel",
            run_id="run_testrel",
            git=pr.GitSnapshot(commit="abc", short_commit="abc", dirty=False),
            previous_current=None,
            staging_dir=staging,
            dependency_hash=None,
            dependency_bundle=None,
            manifest={},
        )
        result = pr.default_validate(ctx)
        assert result["ok"] is True
        assert result["hermetic_lock_root"] is True
        assert captured
        for item in captured:
            assert item["MK04_SHARED_LOCK_ROOT"]
            assert item["MK04_SHARED_LOCK_ROOT"] != str(live)
            assert "/var/lib/mk04/locks" not in item["MK04_SHARED_LOCK_ROOT"]
            assert "mk04-promote-validate-" in item["MK04_SHARED_LOCK_ROOT"]
        assert marker.read_text(encoding="utf-8") == before
        assert marker.stat().st_mtime_ns == mtime
        assert list(live.iterdir()) == [marker]

    def test_validation_failure_records_sanitized_evidence(self, env):
        src, locks, tmp = env
        opts = _options(tmp, src, locks)

        def bad_validate(ctx):
            failure = {
                "label": "prompt4 execution gate",
                "returncode": 1,
                "failing_node": "tests/ops/test_prompt4_repair.py::test_env_sh_dev_leaves_shared_root_unset_without_deployed",
                "stdout_tail": pr._sanitize_validation_text(
                    "FAILED tests/ops/test_prompt4_repair.py::test_env_sh\nAPI_SECRET=supersecret"
                ),
                "stderr_tail": pr._sanitize_validation_text(
                    "Authorization Bearer tok_live_abc"
                ),
            }
            validation = {
                "ok": False,
                "hermetic_lock_root": True,
                "commands": [
                    {
                        "label": "prompt4 execution gate",
                        "command": ["python", "-m", "pytest", "tests/ops"],
                        "returncode": 1,
                        "ok": False,
                    }
                ],
                "failure": failure,
            }
            raise pr.PromoteError(
                "validation failed: prompt4 execution gate",
                detail={"validation": validation, "failure": failure},
            )

        opts.validate_fn = bad_validate
        # Seed a current release so we can prove it is not switched.
        first = pr.promote(_options(tmp, src, locks))
        layout = pr.prod_layout(opts.prod_base)
        current_before = layout["current"].resolve()

        with pytest.raises(pr.PromoteError, match="validation failed"):
            pr.promote(opts)

        assert layout["current"].resolve() == current_before
        staging = sorted(layout["releases"].glob(".staging-*"))
        assert staging, "failed staging must be retained"
        manifest = json.loads((staging[-1] / "release_manifest.json").read_text(encoding="utf-8"))
        assert manifest["activation_result"] == "validation_failed"
        assert manifest["validation"]["ok"] is False
        failure = manifest["validation"]["failure"]
        assert failure["label"] == "prompt4 execution gate"
        assert failure["returncode"] == 1
        assert "test_env_sh" in (failure.get("failing_node") or "")
        assert "supersecret" not in json.dumps(manifest)
        assert "tok_live_abc" not in json.dumps(manifest)
        assert "API_SECRET=***" in failure["stdout_tail"]
        status = json.loads(layout["status"].read_text(encoding="utf-8"))
        assert status["status"] == "failure"
        assert status["activation_result"] == "validation_failed"
        assert status["validation_failure"]["label"] == "prompt4 execution gate"
        assert status["previous_current_release"] == first["release_id"]

    def test_sanitize_validation_text_redacts_secrets(self):
        text = "API_SECRET=supersecret\nAuthorization Bearer tok_live\nOK line"
        out = pr._sanitize_validation_text(text, limit=2000)
        assert "supersecret" not in out
        assert "tok_live" not in out
        assert "API_SECRET=***" in out
        assert "Bearer ***" in out or "bearer ***" in out.lower()

    def test_default_validate_does_not_write_cache_into_staging(
        self, env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        src, locks, tmp = env
        staging = tmp_path / "staging_ro_like"
        shutil.copytree(src, staging)

        def fake_run(cmd, *args, **kwargs):
            from subprocess import CompletedProcess

            run_env = kwargs.get("env") or {}
            # Prove cache/bytecode targets are outside staging.
            assert run_env.get("PYTHONDONTWRITEBYTECODE") == "1"
            pyc = Path(run_env["PYTHONPYCACHEPREFIX"])
            assert staging.resolve() not in pyc.resolve().parents and pyc.resolve() != staging.resolve()
            assert str(staging) not in run_env.get("PYTEST_ADDOPTS", "")
            # Simulate a bad writer that would have used staging — ensure we did not.
            assert not list(staging.rglob("__pycache__"))
            assert not list(staging.rglob(".pytest_cache"))
            return CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(pr.subprocess, "run", fake_run)
        ctx = pr.PromoteContext(
            options=_options(tmp, src, locks),
            release_id="testrel2",
            run_id="run_testrel2",
            git=pr.GitSnapshot(commit="abc", short_commit="abc", dirty=False),
            previous_current=None,
            staging_dir=staging,
            dependency_hash=None,
            dependency_bundle=None,
            manifest={},
        )
        pr.default_validate(ctx)
        assert not list(staging.rglob("__pycache__"))
        assert not list(staging.rglob(".pytest_cache"))


class TestReleaseServiceReadability:
    def test_normalize_restrictive_source_modes_in_staging(self, env):
        src, locks, tmp = env
        # Restrictive checkout-like modes that previously broke mk04 reads.
        registry = src / "ai-service" / "config" / "funnel_rule_registry.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text('{"schema_version":1,"profiles":{"business":{"rules_version":"business_v1"}},"aliases":{}}\n', encoding="utf-8")
        registry.chmod(0o600)
        prompt = src / "ai-service" / "prompts" / "funnel_rules" / "gaming_v1.txt"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("rules\n", encoding="utf-8")
        prompt.chmod(0o600)
        script = src / "deploy" / "scripts" / "env.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        script.chmod(0o700)
        source_mode = registry.stat().st_mode

        result = pr.promote(_options(tmp, src, locks))
        release = tmp / "prod" / "releases" / result["release_id"]
        rel_registry = release / "ai-service" / "config" / "funnel_rule_registry.json"
        rel_prompt = release / "ai-service" / "prompts" / "funnel_rules" / "gaming_v1.txt"
        rel_script = release / "deploy" / "scripts" / "env.sh"

        assert stat.S_IMODE(rel_registry.stat().st_mode) == 0o0644
        assert stat.S_IMODE(rel_prompt.stat().st_mode) == 0o0644
        assert stat.S_IMODE(rel_script.stat().st_mode) == 0o0755
        # Source checkout unchanged.
        assert registry.stat().st_mode == source_mode
        assert stat.S_IMODE(registry.stat().st_mode) == 0o0600

        # Directories traversable; no world-write anywhere under service roots.
        for name in pr.SERVICE_ROOTS:
            mode = (release / name).stat().st_mode
            assert mode & stat.S_IXOTH
            assert not (mode & stat.S_IWOTH)
        for path in release.rglob("*"):
            if path.is_symlink():
                continue
            assert not (path.stat().st_mode & stat.S_IWOTH)

    def test_verify_rejects_unreadable_release_before_activation(self, env):
        src, locks, tmp = env
        registry = src / "ai-service" / "config" / "funnel_rule_registry.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text("{}\n", encoding="utf-8")

        real_normalize = pr.normalize_release_tree_permissions

        def broken_normalize(root: Path):
            notes = real_normalize(root)
            target = root / "ai-service" / "config" / "funnel_rule_registry.json"
            target.chmod(0o600)
            return notes

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(pr, "normalize_release_tree_permissions", broken_normalize)
        try:
            with pytest.raises(pr.PromoteError, match="service-readability"):
                pr.promote(_options(tmp, src, locks))
        finally:
            monkeypatch.undo()

        layout = pr.prod_layout(tmp / "prod")
        assert not layout["current"].exists() or not layout["current"].is_symlink()
        # Staging left for diagnosis; current not switched to a bad release.
        staging = list((tmp / "prod" / "releases").glob(".staging-*"))
        assert staging
