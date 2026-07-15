"""Tests for first production host bootstrap orchestrator (Prompt 7).

Uses temporary path prefixes and mocked account/systemd — never the real host.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "scripts" / "ops"
sys.path.insert(0, str(OPS))

import bootstrap_production_host as bp  # noqa: E402


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "host"
    root.mkdir()
    # Pre-existing development trees that must never be touched.
    for rel in ("etc/mk04/dev", "var/lib/mk04/dev", "var/log/mk04/dev"):
        p = root / rel
        p.mkdir(parents=True)
        (p / "marker").write_text("dev-only\n", encoding="utf-8")
    return root


def _opts(
    sandbox: Path,
    *,
    dry_run: bool = False,
    phases: tuple[str, ...] = ("prepare-host",),
    secret_factory=None,
) -> bp.BootstrapOptions:
    layout = bp.HostLayout.under_prefix(sandbox)
    return bp.BootstrapOptions(
        dev_root=REPO_ROOT,
        operator="maguireltd",
        dry_run=dry_run,
        apply=not dry_run,
        phases=phases,
        layout=layout,
        commands_target=layout.commands_dir,
        skip_account=True,
        skip_external=True,
        secret_factory=secret_factory or (lambda: "unit-test-secret-value-aaaa"),
    )


def _inventory(root: Path) -> dict[str, tuple[int, str]]:
    """path → (mtime_ns, content hash-ish) for protected trees."""
    out: dict[str, tuple[int, str]] = {}
    for path in root.rglob("*"):
        if path.is_file():
            st = path.stat()
            out[str(path.relative_to(root))] = (st.st_mtime_ns, path.read_text(encoding="utf-8"))
    return out


class TestDryRun:
    def test_dry_run_performs_no_writes(self, sandbox: Path):
        before = _inventory(sandbox)
        opts = _opts(sandbox, dry_run=True, phases=bp.PHASES)
        assert bp.run_bootstrap(opts) == 0
        after = _inventory(sandbox)
        assert after == before
        assert not (sandbox / "var/lib/mk04/locks").exists()
        assert not (sandbox / "etc/mk04/prod").exists()
        assert not (sandbox / "opt/mk04/prod").exists()


class TestProdOnlyPaths:
    def test_only_prod_paths_created(self, sandbox: Path):
        opts = _opts(sandbox, phases=("prepare-host",))
        bp.run_bootstrap(opts)
        assert (sandbox / "var/lib/mk04/locks").is_dir()
        assert (sandbox / "etc/mk04/prod").is_dir()
        assert (sandbox / "opt/mk04/prod/releases").is_dir()
        assert (sandbox / "var/lib/mk04/prod").is_dir()
        assert (sandbox / "var/log/mk04/prod").is_dir()
        # Dev markers intact
        assert (sandbox / "etc/mk04/dev/marker").read_text(encoding="utf-8") == "dev-only\n"
        assert (sandbox / "var/lib/mk04/dev/marker").read_text(encoding="utf-8") == "dev-only\n"
        assert (sandbox / "var/log/mk04/dev/marker").read_text(encoding="utf-8") == "dev-only\n"

    def test_dev_trees_never_recursively_modified(self, sandbox: Path):
        before = {
            str(p): p.stat().st_mtime_ns
            for p in [
                sandbox / "etc/mk04/dev",
                sandbox / "var/lib/mk04/dev",
                sandbox / "var/log/mk04/dev",
                sandbox / "etc/mk04/dev/marker",
            ]
        }
        opts = _opts(sandbox, phases=("prepare-host", "seed-config"))
        bp.run_bootstrap(opts)
        for path, mtime in before.items():
            assert Path(path).stat().st_mtime_ns == mtime


class TestLockBeforeMarker:
    def test_lock_directory_created_before_prod_config_marker(self, sandbox: Path):
        order: list[str] = []
        real_install = bp.install_exact_dir

        def tracking_install(path, **kwargs):
            order.append(str(path))
            return real_install(path, **kwargs)

        opts = _opts(sandbox, phases=("prepare-host",))
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(bp, "install_exact_dir", tracking_install)
        try:
            bp.run_bootstrap(opts)
        finally:
            monkeypatch.undo()
        locks = str(opts.layout.locks)
        etc = str(opts.layout.etc_prod)
        assert locks in order
        assert etc in order
        assert order.index(locks) < order.index(etc)


class TestRefuseParentRecursive:
    def test_refuse_recursive_flag(self):
        with pytest.raises(bp.BootstrapError, match="recursive"):
            bp.assert_exact_path_operation(Path("/var/lib/mk04/prod"), recursive=True)

    def test_refuse_parent_tree_chmod(self):
        with pytest.raises(bp.BootstrapError, match="parent tree"):
            bp.assert_exact_path_operation(Path("/var/lib/mk04"), recursive=False)

    def test_refuse_protected_path(self, sandbox: Path):
        layout = bp.HostLayout.under_prefix(sandbox)
        with pytest.raises(bp.BootstrapError, match="protected"):
            bp.assert_not_protected(layout.protected_paths()[0] / "child", layout)


class TestUidGidLookup:
    def test_uid_gid_uses_gr_gid_not_pw_gid(self, monkeypatch: pytest.MonkeyPatch):
        """Regression: grp.struct_group exposes gr_gid, not pw_gid."""

        class FakePasswd:
            pw_uid = 4242

        class StrictGroup:
            """Mirrors grp.struct_group: gr_gid only — no pw_gid attribute."""

            gr_gid = 5757

        class TrapGroup:
            gr_gid = 5757

            @property
            def pw_gid(self) -> int:
                raise AssertionError("must use gr_gid, not pw_gid")

        assert not hasattr(StrictGroup(), "pw_gid")

        monkeypatch.setattr(bp.pwd, "getpwnam", lambda _n: FakePasswd())
        monkeypatch.setattr(bp.grp, "getgrnam", lambda _n: StrictGroup())
        uid, gid = bp._uid_gid("mk04", "mk04")
        assert uid == 4242
        assert gid == 5757

        monkeypatch.setattr(bp.grp, "getgrnam", lambda _n: TrapGroup())
        uid2, gid2 = bp._uid_gid("mk04", "mk04")
        assert uid2 == 4242
        assert gid2 == 5757


class TestIdempotentRerun:
    def test_repeated_preparation_is_safe(self, sandbox: Path):
        opts = _opts(sandbox, phases=("prepare-host", "seed-config"))
        assert bp.run_bootstrap(opts) == 0
        env1 = (sandbox / "etc/mk04/prod/env").read_text(encoding="utf-8")
        secret_line = [
            ln for ln in env1.splitlines() if ln.startswith("INPUT_SERVICE_SECRET=")
        ][0]
        assert bp.run_bootstrap(opts) == 0
        env2 = (sandbox / "etc/mk04/prod/env").read_text(encoding="utf-8")
        secret_line2 = [
            ln for ln in env2.splitlines() if ln.startswith("INPUT_SERVICE_SECRET=")
        ][0]
        assert secret_line == secret_line2


class TestSecrets:
    def test_secret_generation_without_output(self, sandbox: Path, capsys):
        opts = _opts(
            sandbox,
            phases=("prepare-host", "seed-config"),
            secret_factory=lambda: "super-secret-should-not-print-zzzz",
        )
        bp.run_bootstrap(opts)
        captured = capsys.readouterr()
        assert "super-secret-should-not-print-zzzz" not in captured.out
        assert "super-secret-should-not-print-zzzz" not in captured.err
        env = bp.parse_env_file(sandbox / "etc/mk04/prod/env")
        assert env["INPUT_SERVICE_SECRET"] == "super-secret-should-not-print-zzzz"
        assert env["OPS_UI_OPERATOR_PASSWORD"]
        assert "OPENAI_API_KEY" not in env or bp._is_placeholder(env.get("OPENAI_API_KEY", ""))

    def test_existing_secret_preservation(self, sandbox: Path):
        opts = _opts(sandbox, phases=("prepare-host", "seed-config"))
        bp.run_bootstrap(opts)
        env_path = sandbox / "etc/mk04/prod/env"
        text = env_path.read_text(encoding="utf-8")
        text = text.replace(
            "INPUT_SERVICE_SECRET=unit-test-secret-value-aaaa",
            "INPUT_SERVICE_SECRET=already-set-preserve-me",
        )
        env_path.write_text(text, encoding="utf-8")
        bp.run_bootstrap(opts)
        env = bp.parse_env_file(env_path)
        assert env["INPUT_SERVICE_SECRET"] == "already-set-preserve-me"


class TestSafetyInitialization:
    def test_upload_and_scheduler_controls(self, sandbox: Path):
        opts = _opts(
            sandbox,
            phases=("prepare-host", "seed-config", "component-bootstrap"),
        )
        bp.run_bootstrap(opts)
        env = bp.parse_env_file(sandbox / "etc/mk04/prod/env")
        assert env["MK04_SCHEDULER_MODE"] == "manual"
        assert env["OUTPUT_FUNNEL_PLAN_WORKER_ENABLED"] == "0"
        assert env["OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED"] == "0"
        assert env["OUTPUT_FUNNEL_AUTO_UPLOAD"] == "0"
        assert env["MK04_UPLOAD_MODE"] == "dry_run"
        assert env["INPUT_SERVICE_PORT"] == "5060"
        cs = json.loads((sandbox / "var/lib/mk04/prod/data/control_state.json").read_text())
        assert cs["uploads_disabled"] is True
        assert cs["scheduler_disabled"] is True
        ctrl = json.loads((sandbox / "var/lib/mk04/prod/ops-ui/controls.json").read_text())
        assert ctrl["uploads_paused"] is True

    def test_no_cron_installation(self, sandbox: Path):
        opts = _opts(sandbox, phases=bp.PHASES)
        bp.run_bootstrap(opts)
        assert not (sandbox / "etc/cron.d/mk04").exists()


class TestSystemdSeparateFromDirs:
    def test_prepare_does_not_install_units(self, sandbox: Path):
        opts = _opts(sandbox, phases=("prepare-host",))
        bp.run_bootstrap(opts)
        assert not (sandbox / "etc/systemd/system/mk04-ops-ui.service").exists()

    def test_install_services_copies_units_only_when_requested(self, sandbox: Path):
        opts = _opts(
            sandbox,
            phases=(
                "prepare-host",
                "seed-config",
                "component-bootstrap",
                "reconcile-permissions",
                "install-services",
            ),
        )
        bp.run_bootstrap(opts)
        unit = sandbox / "etc/systemd/system/mk04-ops-ui.service"
        assert unit.is_file()
        text = unit.read_text(encoding="utf-8")
        assert "User=mk04" in text
        assert "/opt/mk04/prod/current" in text
        assert "EnvironmentFile=/etc/mk04/prod/env" in text
        assert "EnvironmentFile=-/etc/mk04/prod/env" not in text
        assert "/etc/mk04/dev" not in text


class TestFailureStopsLaterPhases:
    def test_failure_stops_before_later_phases(self, sandbox: Path, monkeypatch: pytest.MonkeyPatch):
        calls: list[str] = []

        def boom(_opts):
            calls.append("prepare-host")
            raise bp.BootstrapError("simulated failure")

        def later(_opts):
            calls.append("seed-config")
            return ["should not run"]

        monkeypatch.setitem(bp.PHASE_HANDLERS, "prepare-host", boom)
        monkeypatch.setitem(bp.PHASE_HANDLERS, "seed-config", later)
        opts = _opts(sandbox, phases=("prepare-host", "seed-config"))
        with pytest.raises(bp.BootstrapError, match="simulated"):
            bp.run_bootstrap(opts)
        assert calls == ["prepare-host"]


class TestCommandInstallerIntegration:
    def test_command_installer_into_sandbox(self, sandbox: Path):
        opts = _opts(sandbox, phases=("prepare-host", "install-commands"))
        # install-commands uses real installer; skip_external does not block it
        assert bp.run_bootstrap(opts) == 0
        target = opts.layout.commands_dir
        for name in ("dev", "prod", "promote"):
            wrapper = target / name
            assert wrapper.is_file()
            assert os.access(wrapper, os.X_OK)
            text = wrapper.read_text(encoding="utf-8")
            assert "mk04-operator-command" in text
        check = subprocess.run(
            [
                str(REPO_ROOT / "deploy/scripts/install-operator-commands.sh"),
                "--target-dir",
                str(target),
                "--check",
            ],
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0


class TestPartialEmptyRecovery:
    def test_recovery_from_partial_empty_prod_directory(self, sandbox: Path):
        # Simulate empty prod dir without locks (bad partial state)
        (sandbox / "etc/mk04/prod").mkdir(parents=True)
        opts = _opts(sandbox, phases=("prepare-host", "seed-config"))
        assert bp.run_bootstrap(opts) == 0
        assert (sandbox / "var/lib/mk04/locks").is_dir()
        assert (sandbox / "etc/mk04/prod/env").is_file()
        mode = (sandbox / "var/lib/mk04/locks").stat().st_mode
        assert mode & stat.S_IWGRP
        assert not (mode & stat.S_IWOTH)


class TestApplyGate:
    def test_refuse_mutation_without_apply(self, sandbox: Path):
        layout = bp.HostLayout.under_prefix(sandbox)
        opts = bp.BootstrapOptions(
            dev_root=REPO_ROOT,
            dry_run=False,
            apply=False,
            phases=("prepare-host",),
            layout=layout,
            skip_account=True,
            skip_external=True,
        )
        with pytest.raises(bp.BootstrapError, match="--apply"):
            bp.run_bootstrap(opts)


class TestProdYamlAndTemplates:
    def test_prod_yaml_uploading_disabled(self):
        text = (REPO_ROOT / "config/environments/prod.yaml").read_text(encoding="utf-8")
        assert "uploading:" in text
        assert "enabled: false" in text

    def test_prod_env_example_bootstrap_defaults(self):
        text = (REPO_ROOT / "deploy/env/prod/env.example").read_text(encoding="utf-8")
        assert "MK04_SCHEDULER_MODE=manual" in text
        assert "OUTPUT_FUNNEL_PLAN_WORKER_ENABLED=0" in text
        assert "OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED=0" in text
        assert "OUTPUT_FUNNEL_AUTO_UPLOAD=0" in text
        assert "MK04_UPLOAD_MODE=dry_run" in text


class TestPermissionContract:
    def test_config_root_root_0640_becomes_group_readable_without_content_change(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ):
        opts = _opts(sandbox, phases=("prepare-host", "seed-config"))
        bp.run_bootstrap(opts)
        env = sandbox / "etc/mk04/prod/env"
        secret_line = [
            ln for ln in env.read_text(encoding="utf-8").splitlines()
            if ln.startswith("INPUT_SERVICE_SECRET=")
        ][0]
        before = env.read_bytes()
        os.chmod(env, 0o0640)
        # Simulate wrong ownership by recording intended repair via chown mock.
        chowns: list[tuple[str, int, int]] = []

        def fake_chown(path, uid, gid):
            chowns.append((str(path), uid, gid))

        monkeypatch.setattr(os, "chown", fake_chown)
        monkeypatch.setattr(bp, "resolve_ids", lambda *_a, **_k: (0, 994, 972))
        layout = opts.layout
        notes = bp.reconcile_config_file_permissions(
            layout,
            root_uid=0,
            service_gid=972,
            dry_run=False,
            allow_skip_chown=False,
        )
        assert notes
        assert env.read_bytes() == before
        assert secret_line in env.read_text(encoding="utf-8")
        assert stat.S_IMODE(env.stat().st_mode) == 0o0640
        assert not (env.stat().st_mode & stat.S_IWOTH)
        assert any(c[1] == 0 and c[2] == 972 and c[0].endswith("/env") for c in chowns)

    def test_runtime_controls_root_0600_become_0660_without_value_change(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ):
        opts = _opts(
            sandbox,
            phases=("prepare-host", "seed-config", "component-bootstrap"),
        )
        bp.run_bootstrap(opts)
        cs = sandbox / "var/lib/mk04/prod/data/control_state.json"
        ctrl = sandbox / "var/lib/mk04/prod/ops-ui/controls.json"
        before_cs = cs.read_bytes()
        before_ctrl = ctrl.read_bytes()
        os.chmod(cs, 0o0600)
        os.chmod(ctrl, 0o0600)
        chowns: list[tuple[str, int, int]] = []
        monkeypatch.setattr(
            os, "chown", lambda path, uid, gid: chowns.append((str(path), uid, gid))
        )
        bp.reconcile_runtime_control_permissions(
            opts.layout,
            service_uid=994,
            service_gid=972,
            dry_run=False,
            allow_skip_chown=False,
            enforce_values=True,
        )
        assert cs.read_bytes() == before_cs
        assert ctrl.read_bytes() == before_ctrl
        assert stat.S_IMODE(cs.stat().st_mode) == 0o0660
        assert stat.S_IMODE(ctrl.stat().st_mode) == 0o0660
        assert json.loads(cs.read_text())["uploads_disabled"] is True
        assert json.loads(ctrl.read_text())["uploads_paused"] is True
        assert any(c[1] == 994 and c[2] == 972 for c in chowns)

    def test_lock_artifacts_group_writable_after_reconcile(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ):
        opts = _opts(sandbox, phases=("prepare-host",))
        bp.run_bootstrap(opts)
        locks = sandbox / "var/lib/mk04/locks"
        promo = locks / "promotion.lock"
        promo.write_bytes(b"")
        os.chmod(promo, 0o0644)
        monkeypatch.setattr(os, "chown", lambda *_a, **_k: None)
        bp.reconcile_lock_artifacts(
            opts.layout,
            service_uid=994,
            service_gid=972,
            dry_run=False,
            allow_skip_chown=False,
        )
        mode = promo.stat().st_mode
        assert stat.S_IMODE(mode) == 0o0660
        assert mode & stat.S_IWGRP
        assert not (mode & stat.S_IWOTH)

    def test_reconcile_idempotent(self, sandbox: Path):
        opts = _opts(
            sandbox,
            phases=(
                "prepare-host",
                "seed-config",
                "component-bootstrap",
                "reconcile-permissions",
            ),
        )
        assert bp.run_bootstrap(opts) == 0
        env1 = (sandbox / "etc/mk04/prod/env").read_bytes()
        cs1 = (sandbox / "var/lib/mk04/prod/data/control_state.json").read_bytes()
        assert bp.run_bootstrap(opts) == 0
        assert (sandbox / "etc/mk04/prod/env").read_bytes() == env1
        assert (sandbox / "var/lib/mk04/prod/data/control_state.json").read_bytes() == cs1

    def test_no_world_writable_after_reconcile(self, sandbox: Path):
        opts = _opts(
            sandbox,
            phases=(
                "prepare-host",
                "seed-config",
                "component-bootstrap",
                "reconcile-permissions",
            ),
        )
        bp.run_bootstrap(opts)
        for path in [
            sandbox / "etc/mk04/prod/env",
            sandbox / "var/lib/mk04/prod/data/control_state.json",
            sandbox / "var/lib/mk04/prod/ops-ui/controls.json",
            sandbox / "var/lib/mk04/locks",
        ]:
            assert path.exists()
            assert not (path.stat().st_mode & stat.S_IWOTH)

    def test_install_services_fails_when_contract_unmet(self, sandbox: Path):
        opts = _opts(sandbox, phases=("prepare-host", "seed-config"))
        bp.run_bootstrap(opts)
        # Missing runtime controls → contract fail before unit install
        opts2 = _opts(sandbox, phases=("install-services",))
        with pytest.raises(bp.BootstrapError, match="permission contract unmet"):
            bp.run_bootstrap(opts2)
        assert not (sandbox / "etc/systemd/system/mk04-ops-ui.service").exists()

    def test_dry_run_reconcile_no_mutation(self, sandbox: Path):
        opts = _opts(
            sandbox,
            phases=("prepare-host", "seed-config", "component-bootstrap"),
        )
        bp.run_bootstrap(opts)
        env = sandbox / "etc/mk04/prod/env"
        before = env.read_bytes()
        os.chmod(env, 0o0600)
        dry = _opts(sandbox, dry_run=True, phases=("reconcile-permissions",))
        assert bp.run_bootstrap(dry) == 0
        assert env.read_bytes() == before
        assert stat.S_IMODE(env.stat().st_mode) == 0o0600


class TestOutputFunnelSettingsSafetyReconcile:
    def test_existing_plan_worker_true_reconciled_preserves_unrelated(
        self, sandbox: Path
    ):
        opts = _opts(sandbox, phases=("prepare-host",))
        bp.run_bootstrap(opts)
        settings_path = sandbox / "etc/mk04/prod/output-funnel/settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        original = {
            "database_path": "/var/lib/mk04/prod/output-funnel/custom.sqlite3",
            "publisher": {"max_attempts": 9},
            "channels_note": "preserve-me",
            "credential_ref": "do-not-rotate",
            "automation": {
                "auto_schedule": True,
                "schedule_limit": 42,
                "auto_upload": True,
                "upload_limit": 7,
                "plan_worker": {"enabled": True, "interval_seconds": 111},
                "upload_worker": {"enabled": True, "interval_seconds": 222},
            },
        }
        settings_path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

        seed = _opts(sandbox, phases=("seed-config",))
        bp.run_bootstrap(seed)
        after = json.loads(settings_path.read_text(encoding="utf-8"))
        assert after["database_path"] == original["database_path"]
        assert after["publisher"] == original["publisher"]
        assert after["channels_note"] == "preserve-me"
        assert after["credential_ref"] == "do-not-rotate"
        assert after["automation"]["auto_schedule"] is True
        assert after["automation"]["schedule_limit"] == 42
        assert after["automation"]["upload_limit"] == 7
        assert after["automation"]["auto_upload"] is False
        assert after["automation"]["plan_worker"] == {
            "enabled": False,
            "interval_seconds": 111,
        }
        assert after["automation"]["upload_worker"] == {
            "enabled": False,
            "interval_seconds": 222,
        }

        # Idempotent: second seed makes no further content change.
        before_bytes = settings_path.read_bytes()
        bp.run_bootstrap(seed)
        assert settings_path.read_bytes() == before_bytes

    def test_dev_settings_untouched(self):
        dev = REPO_ROOT / "deploy" / "env" / "dev" / "settings.json"
        prod = REPO_ROOT / "deploy" / "env" / "prod" / "settings.json"
        dev_data = json.loads(dev.read_text(encoding="utf-8"))
        prod_data = json.loads(prod.read_text(encoding="utf-8"))
        assert prod_data["automation"]["plan_worker"]["enabled"] is False
        assert prod_data["automation"]["upload_worker"]["enabled"] is False
        assert prod_data["automation"]["auto_upload"] is False
        # Dev file still uses its own database path and was not rewritten by this repair.
        assert "dev" in str(dev_data["database_path"])
        assert "prod" in str(prod_data["database_path"])


class TestPreSystemdSafetyGate:
    def _ready_for_install(self, sandbox: Path) -> None:
        bp.run_bootstrap(
            _opts(
                sandbox,
                phases=(
                    "prepare-host",
                    "seed-config",
                    "component-bootstrap",
                    "reconcile-permissions",
                ),
            )
        )

    def test_install_services_rejects_enabled_plan_worker(self, sandbox: Path):
        self._ready_for_install(sandbox)
        settings_path = sandbox / "etc/mk04/prod/output-funnel/settings.json"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        data["automation"]["plan_worker"]["enabled"] = True
        settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        with pytest.raises(bp.BootstrapError, match="pre-systemd safety gate failed"):
            bp.run_bootstrap(_opts(sandbox, phases=("install-services",)))
        assert not (sandbox / "etc/systemd/system/mk04-ops-ui.service").exists()

    def test_install_services_rejects_unsafe_env_worker_flag(self, sandbox: Path):
        self._ready_for_install(sandbox)
        env_path = sandbox / "etc/mk04/prod/env"
        text = env_path.read_text(encoding="utf-8")
        text = text.replace(
            "OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED=0",
            "OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED=1",
        )
        env_path.write_text(text, encoding="utf-8")
        with pytest.raises(bp.BootstrapError, match="OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED"):
            bp.run_bootstrap(_opts(sandbox, phases=("install-services",)))
        assert not (sandbox / "etc/systemd/system/mk04-source-input.service").exists()

    def test_install_services_rejects_uploads_not_paused(self, sandbox: Path):
        self._ready_for_install(sandbox)
        controls = sandbox / "var/lib/mk04/prod/ops-ui/controls.json"
        payload = json.loads(controls.read_text(encoding="utf-8"))
        payload["uploads_paused"] = False
        controls.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        with pytest.raises(bp.BootstrapError, match="uploads_paused"):
            bp.run_bootstrap(_opts(sandbox, phases=("install-services",)))

    def test_assert_unit_text_requires_mandatory_primary_env(self):
        good = (REPO_ROOT / "deploy/systemd/mk04-ops-ui.service").read_text(encoding="utf-8")
        bp._assert_unit_text(good, "mk04-ops-ui.service")
        bad = good.replace(
            "EnvironmentFile=/etc/mk04/prod/env",
            "EnvironmentFile=-/etc/mk04/prod/env",
        )
        with pytest.raises(bp.BootstrapError, match="mandatory"):
            bp._assert_unit_text(bad, "mk04-ops-ui.service")


class TestInstallServicesWaitRollback:
    def test_wait_until_active_polls_past_activating(self, monkeypatch: pytest.MonkeyPatch):
        states = iter(
            [
                {"ActiveState": "activating", "SubState": "start"},
                {"ActiveState": "activating", "SubState": "start"},
                {"ActiveState": "active", "SubState": "running"},
            ]
        )

        def fake_show(_run, _unit):
            return next(states)

        monkeypatch.setattr(bp, "_systemctl_show", fake_show)
        monkeypatch.setattr(bp.time, "sleep", lambda _s: None)
        got = bp._wait_for_unit_active(lambda *a, **k: None, "mk04-ops-ui.service", timeout_sec=5, poll_sec=0)
        assert got == "active"

    def test_wait_does_not_treat_failed_as_success(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            bp,
            "_systemctl_show",
            lambda *_a, **_k: {"ActiveState": "failed", "SubState": "failed"},
        )
        monkeypatch.setattr(bp.time, "sleep", lambda _s: None)
        got = bp._wait_for_unit_active(lambda *a, **k: None, "mk04-ops-ui.service", timeout_sec=1, poll_sec=0)
        assert got == "failed"

    def test_rollback_disables_only_newly_enabled_units(self):
        calls: list[list[str]] = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        notes: list[str] = []
        pre = {
            "mk04-ops-ui.service": {"enabled": "disabled", "active": "inactive"},
            "mk04-ai-service.service": {"enabled": "enabled", "active": "active"},
        }
        bp._rollback_install_services(
            fake_run,
            touched=["mk04-ai-service.service", "mk04-ops-ui.service"],
            pre_states=pre,
            notes=notes,
        )
        # Newly enabled ops-ui → disable --now
        assert ["systemctl", "disable", "--now", "mk04-ops-ui.service"] in calls
        # Previously healthy ai-service → restart, not disable
        assert ["systemctl", "restart", "mk04-ai-service.service"] in calls
        assert not any(c == ["systemctl", "disable", "--now", "mk04-ai-service.service"] for c in calls)
        assert any("newly enabled" in n for n in notes)
        assert any("left mk04-ai-service.service enabled" in n for n in notes)


class TestShellWrapper:
    def test_help(self):
        script = REPO_ROOT / "deploy/scripts/bootstrap-production-host.sh"
        result = subprocess.run([str(script), "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "prepare-host" in result.stdout
        assert "cron" in result.stdout.lower()


class TestRuntimeDataCacheLeaf:
    """Production health write probe requires /var/lib/mk04/prod/data/cache."""

    def test_layout_and_dry_run_include_data_cache(self, sandbox: Path):
        layout = bp.HostLayout.under_prefix(sandbox)
        paths = {p for p, _mode, _role in layout.prepare_host_paths()}
        assert layout.var_data_cache in paths
        assert layout.var_data_cache == layout.var_data / "cache"
        mode_role = {
            p: (mode, role) for p, mode, role in layout.prepare_host_paths()
        }
        assert mode_role[layout.var_data_cache] == (0o2775, "runtime")

        before = _inventory(sandbox)
        opts = _opts(sandbox, dry_run=True, phases=("prepare-host",))
        notes = bp.phase_prepare_host(opts)
        assert any("data/cache" in n for n in notes)
        assert _inventory(sandbox) == before
        assert not layout.var_data_cache.exists()

    def test_apply_creates_cache_with_setgid_mode(self, sandbox: Path):
        opts = _opts(sandbox, phases=("prepare-host",))
        assert bp.run_bootstrap(opts) == 0
        cache = sandbox / "var/lib/mk04/prod/data/cache"
        assert cache.is_dir()
        mode = cache.stat().st_mode
        assert mode & stat.S_ISGID
        assert stat.S_IMODE(mode) == 0o2775 or (mode & 0o7777) == 0o2775
        assert mode & stat.S_IWGRP
        assert not (mode & stat.S_IWOTH)

    def test_apply_idempotent_for_cache(self, sandbox: Path):
        opts = _opts(sandbox, phases=("prepare-host",))
        assert bp.run_bootstrap(opts) == 0
        cache = sandbox / "var/lib/mk04/prod/data/cache"
        marker = cache / "keep_me"
        marker.write_text("ok", encoding="utf-8")
        st1 = cache.stat()
        assert bp.run_bootstrap(opts) == 0
        assert marker.read_text(encoding="utf-8") == "ok"
        st2 = cache.stat()
        assert (st2.st_mode & 0o7777) == (st1.st_mode & 0o7777)

    def test_path_agreement_with_config_health(
        self, sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """ConfigManager / EnvironmentStatePaths / bootstrap / health share one cache path."""
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "config"))
        from config_manager import ConfigManager
        from state_paths import EnvironmentStatePaths

        layout = bp.HostLayout.production()
        expected = Path("/var/lib/mk04/prod/data/cache")
        assert layout.var_data_cache == expected

        runtime = sandbox / "var/lib/mk04/prod"
        (runtime / "video-automation").mkdir(parents=True)
        (runtime / "data" / "cache").mkdir(parents=True)
        monkeypatch.setenv("MK04_RUNTIME_ROOT", str(runtime))
        monkeypatch.setenv("MK04_LOG_ROOT", str(sandbox / "var/log/mk04/prod"))
        for key in (
            "MK04_DATA_ROOT",
            "MK04_JOBS_ROOT",
            "MK04_OUTPUTS_ROOT",
            "MK04_RUNS_ROOT",
            "MK04_REPORTS_ROOT",
            "MK04_DATABASE_PATH",
            "MK04_CONTROL_STATE_FILE",
            "MK04_REQUIRE_RUNTIME_PATHS",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.chdir(REPO_ROOT)
        resolved = ConfigManager.load(environment="prod", config_root=REPO_ROOT / "config")
        state = EnvironmentStatePaths.from_resolved_config(resolved)
        assert state.caches_root == (runtime / "data" / "cache").resolve()
        assert state.data_root / "cache" == state.caches_root
        # Health probe target is the same leaf.
        assert state.caches_root == Path(str(runtime / "data" / "cache")).resolve()
