"""Tests for one-word operator commands (Prompt 6).

Uses temporary checkouts, prod bases, configs, and bin dirs only.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "scripts" / "ops"
if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))

import manual_funnel as mf  # noqa: E402
import operator_commands as oc  # noqa: E402


def _write_funnels(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return path


def _funnel(fid: str, *, active: bool = True) -> dict:
    return {
        "funnel_id": fid,
        "angle": "test",
        "source_type": "youtube_channels",
        "sources": [
            {
                "source_id": f"{fid}_src",
                "label": fid,
                "source_type": "youtube_channel",
                "url": "https://www.youtube.com/@x/videos",
                "active": True,
                "max_videos_per_source": 1,
                "hydrate_missing_duration": True,
                "title_blocklist": [],
            }
        ],
        "min_duration_minutes": 1,
        "max_duration_minutes": 180,
        "max_downloads_per_run": 1,
        "posting_config": {"enabled": False, "mode": "manual_review", "platforms": []},
        "analytics_config": {"enabled": False, "event_namespace": fid, "webhook_url": "", "track_fields": []},
        "active": active,
    }


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base = REPO_ROOT / ".tmp_operator_tests" / tmp_path.name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    monkeypatch.chdir(base)
    monkeypatch.delenv("RUN_FUNNEL_ID", raising=False)
    monkeypatch.delenv("SOURCE_INPUT_FUNNELS", raising=False)
    monkeypatch.delenv("MK04_DEV_ROOT", raising=False)
    monkeypatch.delenv("MK04_OPERATOR_META", raising=False)
    monkeypatch.delenv("MK04_OPERATOR_IN_RELEASE", raising=False)
    return base


class TestManualFunnelResolver:
    def test_explicit_wins(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        path = _write_funnels(
            workspace / "funnels.json",
            [_funnel("alpha"), _funnel("beta")],
        )
        monkeypatch.setenv("RUN_FUNNEL_ID", "beta")
        res = mf.resolve_manual_funnel(
            environment="dev", explicit_id="alpha", funnels_file=path
        )
        assert res.funnel_id == "alpha"
        assert res.source == "explicit"

    def test_run_funnel_id(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        path = _write_funnels(workspace / "funnels.json", [_funnel("alpha"), _funnel("beta")])
        monkeypatch.setenv("RUN_FUNNEL_ID", "beta")
        res = mf.resolve_manual_funnel(environment="dev", funnels_file=path)
        assert res.funnel_id == "beta"
        assert res.source == "run_funnel_id"

    def test_unique_active(self, workspace: Path):
        path = _write_funnels(
            workspace / "funnels.json",
            [_funnel("only_one"), _funnel("off", active=False)],
        )
        res = mf.resolve_manual_funnel(environment="dev", funnels_file=path)
        assert res.funnel_id == "only_one"
        assert res.source == "unique_active"

    def test_zero_active_fails(self, workspace: Path):
        path = _write_funnels(workspace / "funnels.json", [_funnel("x", active=False)])
        with pytest.raises(mf.ManualFunnelError, match="No active funnels"):
            mf.resolve_manual_funnel(environment="dev", funnels_file=path)

    def test_multiple_active_fails_lists_ids(self, workspace: Path):
        path = _write_funnels(
            workspace / "funnels.json",
            [_funnel("one"), _funnel("two")],
        )
        with pytest.raises(mf.ManualFunnelError, match="one") as exc:
            mf.resolve_manual_funnel(environment="dev", funnels_file=path)
        assert "two" in str(exc.value)

    def test_dev_prod_different_active(self, workspace: Path):
        dev = _write_funnels(workspace / "dev.json", [_funnel("dev_only")])
        prod = _write_funnels(workspace / "prod.json", [_funnel("prod_only")])
        assert (
            mf.resolve_manual_funnel(environment="dev", funnels_file=dev).funnel_id
            == "dev_only"
        )
        assert (
            mf.resolve_manual_funnel(environment="prod", funnels_file=prod).funnel_id
            == "prod_only"
        )

    def test_unknown_explicit_fails(self, workspace: Path):
        path = _write_funnels(workspace / "funnels.json", [_funnel("real")])
        with pytest.raises(mf.ManualFunnelError, match="Unknown funnel_id"):
            mf.resolve_manual_funnel(environment="dev", explicit_id="nope", funnels_file=path)

    def test_inactive_explicit_warns_but_allows(self, workspace: Path):
        path = _write_funnels(workspace / "funnels.json", [_funnel("dormant", active=False)])
        res = mf.resolve_manual_funnel(
            environment="dev", explicit_id="dormant", funnels_file=path
        )
        assert res.active is False
        assert res.warning


class TestOperatorDevProdPromote:
    def _mini_dev_root(self, workspace: Path) -> Path:
        """Point MK04_DEV_ROOT at the real repo (valid checkout) for marker files."""
        return REPO_ROOT

    def test_bare_dev_starts_stack_not_pipeline(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        captured: list[list[str]] = []

        def fake_run(cmd, check=False, cwd=None, capture_output=False, text=False):
            captured.append(list(cmd))
            # First call is health (capture); second would be run.sh
            if capture_output:
                return mock.Mock(returncode=1, stdout="Overall FAIL\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(oc.subprocess, "run", fake_run)
        code = oc.cmd_dev([])
        assert code == 0
        joined = [" ".join(c) for c in captured]
        assert any("run.sh" in j and "--env" in j and "dev" in j for j in joined)
        assert not any("run-pipeline.sh" in j for j in joined)

    def test_bare_dev_skips_start_when_healthy(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        captured: list[list[str]] = []

        def fake_run(cmd, check=False, cwd=None, capture_output=False, text=False):
            captured.append(list(cmd))
            return mock.Mock(returncode=0, stdout="Overall PASS\n", stderr="")

        monkeypatch.setattr(oc.subprocess, "run", fake_run)
        assert oc.cmd_dev([]) == 0
        assert len(captured) == 1
        assert "health.sh" in captured[0][1]
        assert not any("run.sh" in c[1] for c in captured if len(c) > 1)
        assert not any("run-pipeline" in " ".join(c) for c in captured)

    def test_dev_run_invokes_runner(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        path = _write_funnels(workspace / "funnels.json", [_funnel("clip_a")])
        monkeypatch.setenv("SOURCE_INPUT_FUNNELS", str(path))
        captured: dict = {}

        def fake_run(cmd, check=False):
            captured["cmd"] = list(cmd)
            return mock.Mock(returncode=0)

        monkeypatch.setattr(oc.subprocess, "run", fake_run)
        code = oc.cmd_dev(["run", "clip_a"])
        assert code == 0
        assert captured["cmd"][0] == "bash"
        assert captured["cmd"][1].endswith("scripts/ops/run-pipeline.sh")
        assert "dev" in captured["cmd"]
        assert "--funnel-id" in captured["cmd"]
        assert "clip_a" in captured["cmd"]
        assert "--trigger" in captured["cmd"]
        assert "manual_cli" in captured["cmd"]

    def test_dev_run_not_ready_prints_guidance(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        path = _write_funnels(workspace / "funnels.json", [_funnel("clip_a")])
        monkeypatch.setenv("SOURCE_INPUT_FUNNELS", str(path))
        monkeypatch.setattr(
            oc.subprocess,
            "run",
            lambda *a, **k: mock.Mock(returncode=4),
        )
        code = oc.cmd_dev(["run", "clip_a"])
        assert code == 4
        err = capsys.readouterr().err
        assert "dev" in err.lower()

    def test_gate_refusal_exit_6(self, workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys):
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        path = _write_funnels(workspace / "funnels.json", [_funnel("clip_a")])
        monkeypatch.setenv("SOURCE_INPUT_FUNNELS", str(path))
        monkeypatch.setattr(
            oc.subprocess,
            "run",
            lambda *a, **k: mock.Mock(returncode=6),
        )
        code = oc.cmd_dev(["run", "clip_a"])
        assert code == 6
        assert "gate" in capsys.readouterr().err.lower()

    def _fake_prod_current(self, workspace: Path) -> Path:
        base = workspace / "prod"
        releases = base / "releases" / "rel1"
        releases.mkdir(parents=True)
        # Minimal runner script + operator module copy for re-exec avoidance
        runner = releases / "scripts" / "ops" / "run-pipeline.sh"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        runner.chmod(0o755)
        # Link operator_commands into release so prod uses release tree
        shutil.copy2(OPS / "operator_commands.py", runner.parent / "operator_commands.py")
        shutil.copy2(OPS / "manual_funnel.py", runner.parent / "manual_funnel.py")
        shutil.copy2(OPS / "restart_service.py", runner.parent / "restart_service.py")
        # promote_release helpers needed for resolve — import from DEV via path
        current = base / "current"
        current.symlink_to(Path("releases/rel1"))
        return base

    def test_bare_prod_starts_stack_not_pipeline(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ):
        prod_base = self._fake_prod_current(workspace)
        monkeypatch.setenv("MK04_PROD_BASE", str(prod_base))
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        monkeypatch.setenv("MK04_OPERATOR_IN_RELEASE", "1")
        calls: dict = {"start": 0, "pipeline": 0}

        def fake_start(*_a, **_k):
            calls["start"] += 1
            return 0

        def fake_run(cmd, check=False, env=None, **_kwargs):
            if any("run-pipeline" in str(x) for x in cmd):
                calls["pipeline"] += 1
            return mock.Mock(returncode=0)

        monkeypatch.setattr(oc, "start_prod_stack", lambda release: fake_start())
        monkeypatch.setattr(oc.subprocess, "run", fake_run)
        assert oc.cmd_prod([]) == 0
        assert calls["start"] == 1
        assert calls["pipeline"] == 0

    def test_bare_prod_uses_execute_start_auth_path(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Bare prod must go through execute_start (auth-then-batched-start)."""
        prod_base = self._fake_prod_current(workspace)
        monkeypatch.setenv("MK04_PROD_BASE", str(prod_base))
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        seen: dict = {"execute_start": 0}

        def fake_execute_start(env, target="all", **kwargs):
            seen["execute_start"] += 1
            seen["env"] = env
            seen["target"] = target
            return 0

        import restart_service as rs

        monkeypatch.setattr(rs, "execute_start", fake_execute_start)
        assert oc.start_prod_stack(prod_base / "releases" / "rel1") == 0
        assert seen["execute_start"] == 1
        assert seen["env"] == "prod"
        assert seen["target"] == "all"
        # Contract: start path never shells out to the pipeline runner.
        src = Path(oc.__file__).read_text(encoding="utf-8")
        start_fn = src.split("def start_prod_stack", 1)[1].split("\ndef ", 1)[0]
        assert "execute_start" in start_fn
        assert "run-pipeline" not in start_fn

    def test_bare_prod_does_not_enable_uploads_or_scheduler(self):
        text = Path(oc.__file__).read_text(encoding="utf-8")
        assert "enable-uploads" not in text
        assert "start-scheduler" not in text
        assert "enable_uploads" not in text
        assert "start_scheduler" not in text

    def test_prod_run_uses_current_runner(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ):
        prod_base = self._fake_prod_current(workspace)
        monkeypatch.setenv("MK04_PROD_BASE", str(prod_base))
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        path = _write_funnels(workspace / "funnels.json", [_funnel("prod_f")])
        monkeypatch.setenv("SOURCE_INPUT_FUNNELS", str(path))
        monkeypatch.setenv("MK04_OPERATOR_IN_RELEASE", "1")
        captured: dict = {}

        def fake_run(cmd, check=False, env=None):
            captured["cmd"] = list(cmd)
            return mock.Mock(returncode=0)

        monkeypatch.setattr(oc.subprocess, "run", fake_run)
        monkeypatch.setattr(oc, "_real_upload_armed", lambda: False)
        code = oc.cmd_prod(["run", "prod_f"])
        assert code == 0
        assert str(prod_base / "releases" / "rel1") in captured["cmd"][1]
        assert "prod" in captured["cmd"]
        assert "manual_cli" in captured["cmd"]

    def test_missing_current_fails(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MK04_PROD_BASE", str(workspace / "empty_prod"))
        (workspace / "empty_prod").mkdir()
        with pytest.raises(oc.OperatorError, match="not promoted|bootstrap"):
            oc.resolve_prod_current()

    def test_external_symlink_rejected(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        base = workspace / "prod"
        releases = base / "releases"
        releases.mkdir(parents=True)
        outside = workspace / "outside"
        outside.mkdir()
        (outside / "scripts" / "ops").mkdir(parents=True)
        (outside / "scripts" / "ops" / "run-pipeline.sh").write_text("#!/bin/bash\n", encoding="utf-8")
        (base / "current").symlink_to(outside)
        monkeypatch.setenv("MK04_PROD_BASE", str(base))
        with pytest.raises(oc.OperatorError, match="must resolve under|release"):
            oc.resolve_prod_current()

    def test_dry_run_no_live_confirm(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        prod_base = self._fake_prod_current(workspace)
        monkeypatch.setenv("MK04_PROD_BASE", str(prod_base))
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        monkeypatch.setenv("MK04_OPERATOR_IN_RELEASE", "1")
        path = _write_funnels(workspace / "funnels.json", [_funnel("prod_f")])
        monkeypatch.setenv("SOURCE_INPUT_FUNNELS", str(path))
        monkeypatch.setattr(oc, "_real_upload_armed", lambda: False)
        monkeypatch.setattr(oc.subprocess, "run", lambda *a, **k: mock.Mock(returncode=0))
        # Would raise if confirmation were incorrectly required
        assert oc.cmd_prod(["run", "prod_f"]) == 0

    def test_real_upload_requires_confirm_noninteractive(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ):
        prod_base = self._fake_prod_current(workspace)
        monkeypatch.setenv("MK04_PROD_BASE", str(prod_base))
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        monkeypatch.setenv("MK04_OPERATOR_IN_RELEASE", "1")
        path = _write_funnels(workspace / "funnels.json", [_funnel("prod_f")])
        monkeypatch.setenv("SOURCE_INPUT_FUNNELS", str(path))
        monkeypatch.setattr(oc, "_real_upload_armed", lambda: True)
        monkeypatch.setattr(oc.sys.stdin, "isatty", lambda: False)
        with pytest.raises(oc.OperatorError, match="--confirm-live"):
            oc.cmd_prod(["run", "prod_f"])

    def test_confirm_live_does_not_change_upload_config(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ):
        prod_base = self._fake_prod_current(workspace)
        monkeypatch.setenv("MK04_PROD_BASE", str(prod_base))
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        monkeypatch.setenv("MK04_OPERATOR_IN_RELEASE", "1")
        monkeypatch.setenv("MK04_UPLOAD_MODE", "dry_run")
        path = _write_funnels(workspace / "funnels.json", [_funnel("prod_f")])
        monkeypatch.setenv("SOURCE_INPUT_FUNNELS", str(path))
        monkeypatch.setattr(oc, "_real_upload_armed", lambda: True)
        monkeypatch.setattr(oc.subprocess, "run", lambda *a, **k: mock.Mock(returncode=0))
        before = os.environ.get("MK04_UPLOAD_MODE")
        assert oc.cmd_prod(["run", "prod_f", "--confirm-live"]) == 0
        assert os.environ.get("MK04_UPLOAD_MODE") == before

    def test_promote_uses_dev_promoter(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        # Pretend current exists so bare promote isn't blocked — still mock exec
        prod_base = workspace / "prod"
        (prod_base / "releases" / "r1").mkdir(parents=True)
        (prod_base / "current").symlink_to("releases/r1")
        monkeypatch.setenv("MK04_PROD_BASE", str(prod_base))
        captured: dict = {}

        def fake_run(cmd, check=False):
            captured["cmd"] = list(cmd)
            return mock.Mock(returncode=0)

        monkeypatch.setattr(oc.subprocess, "run", fake_run)
        assert oc.cmd_promote(["--dry-run"]) == 0
        assert captured["cmd"][1].endswith("deploy/scripts/promote-to-prod.sh")
        assert "--source" in captured["cmd"]
        assert str(REPO_ROOT) in captured["cmd"]
        assert "--dry-run" in captured["cmd"]

    def test_bare_prebootstrap_promote_refuses(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        monkeypatch.setenv("MK04_PROD_BASE", str(workspace / "no_prod"))
        (workspace / "no_prod").mkdir()
        with pytest.raises(oc.OperatorError, match="--no-restart"):
            oc.cmd_promote([])

    def test_invalid_mk04_dev_root(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MK04_DEV_ROOT", str(workspace / "not_a_checkout"))
        (workspace / "not_a_checkout").mkdir()
        with pytest.raises(oc.OperatorError, match="MK04_DEV_ROOT"):
            oc.discover_dev_root()

    def test_cwd_independent(self, workspace: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        path = _write_funnels(workspace / "funnels.json", [_funnel("clip_a")])
        monkeypatch.setenv("SOURCE_INPUT_FUNNELS", str(path))
        elsewhere = workspace / "somewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setattr(oc.subprocess, "run", lambda *a, **k: mock.Mock(returncode=0))
        assert oc.cmd_dev(["run", "clip_a"]) == 0

    def test_legacy_bare_funnel_arg_refused(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("MK04_DEV_ROOT", str(REPO_ROOT))
        with pytest.raises(oc.OperatorError, match="dev run"):
            oc.cmd_dev(["clip_a"])


class TestInstaller:
    def test_install_uninstall_idempotent(self, workspace: Path):
        target = workspace / "bin"
        target.mkdir()
        script = REPO_ROOT / "deploy" / "scripts" / "install-operator-commands.sh"
        env = os.environ.copy()
        cmd = [
            "bash",
            str(script),
            "--target-dir",
            str(target),
            "--dev-root",
            str(REPO_ROOT),
        ]
        assert subprocess.run(cmd, check=False, env=env).returncode == 0
        for name in ("dev", "prod", "promote"):
            path = target / name
            assert path.is_file()
            assert os.access(path, os.X_OK)
            text = path.read_text(encoding="utf-8")
            assert "mk04-operator-command" in text
            assert "gta_clips_002" not in text
            assert "business" not in text or "operator_commands" in text
        # Idempotent
        assert subprocess.run(cmd, check=False, env=env).returncode == 0
        # Unrelated file refused
        foreign = target / "dev"
        # Replace with foreign content
        foreign.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
        foreign.chmod(0o755)
        bad = subprocess.run(cmd, check=False, env=env, capture_output=True, text=True)
        assert bad.returncode != 0
        assert "unrelated" in (bad.stderr + bad.stdout).lower()
        # Restore owned wrapper then uninstall
        assert subprocess.run(
            ["bash", str(script), "--target-dir", str(target), "--dev-root", str(REPO_ROOT), "--uninstall"],
            check=False,
        ).returncode == 0
        # Reinstall cleanly then uninstall owned only
        foreign.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
        assert subprocess.run(cmd, check=False, env=env).returncode != 0  # still blocked
        # Remove foreign, install, add foreign sibling, uninstall keeps foreign sibling
        foreign.unlink()
        assert subprocess.run(cmd, check=False, env=env).returncode == 0
        sibling = target / "othercmd"
        sibling.write_text("#!/bin/sh\n", encoding="utf-8")
        assert subprocess.run(
            ["bash", str(script), "--target-dir", str(target), "--uninstall"],
            check=False,
        ).returncode == 0
        assert not (target / "dev").exists()
        assert sibling.exists()

    def test_no_hardcoded_funnel_in_wrappers(self, workspace: Path):
        target = workspace / "bin2"
        target.mkdir()
        script = REPO_ROOT / "deploy" / "scripts" / "install-operator-commands.sh"
        assert (
            subprocess.run(
                [
                    "bash",
                    str(script),
                    "--target-dir",
                    str(target),
                    "--dev-root",
                    str(REPO_ROOT),
                ],
                check=False,
            ).returncode
            == 0
        )
        blob = "".join((target / n).read_text(encoding="utf-8") for n in ("dev", "prod", "promote"))
        assert "gta_clips_002" not in blob
        assert 'funnel_id or "business"' not in blob


class TestOpsUiFallbackRemoved:
    def test_controls_uses_resolver(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        sys.path.insert(0, str(REPO_ROOT / "ops-ui"))
        from ops_ui.config import ServiceConfig, Settings
        from ops_ui.controls import execute_control_action

        settings = Settings(
            host="127.0.0.1",
            port=5070,
            data_dir=tmp_path,
            control_db_path=tmp_path / "ops.sqlite3",
            controls_file=tmp_path / "controls.json",
            service_timeout_sec=0.01,
            journal_lines=1,
            funnel_run_timeout_sec=1.0,
            stuck_running_sec=100.0,
            stuck_queued_sec=50.0,
            stuck_uploading_sec=50.0,
            environment="dev",
            services=(
                ServiceConfig(
                    key="video-automation",
                    label="video-automation",
                    base_url="http://127.0.0.1:9",
                    systemd_unit="x",
                ),
            ),
        )
        path = _write_funnels(tmp_path / "funnels.json", [_funnel("ui_funnel")])
        monkeypatch.setenv("SOURCE_INPUT_FUNNELS", str(path))
        monkeypatch.setenv("MK04_ENV", "dev")
        with mock.patch("ops_ui.controls.run_pipeline", return_value=0) as fn:
            result = execute_control_action(settings, "run_pipeline_dev", funnel_id="")
        assert result.ok is True
        assert fn.call_args.kwargs["funnel_id"] == "ui_funnel"
        assert fn.call_args.kwargs["trigger"] == "operations_ui"

    def test_no_business_literal_in_pipeline_paths(self):
        controls = (REPO_ROOT / "ops-ui" / "ops_ui" / "controls.py").read_text(encoding="utf-8")
        # Pipeline paths must not fall back to literal business.
        assert 'funnel_id or "business"' not in controls
        assert 'funnel_id=funnel_id or "business"' not in controls
        app = (REPO_ROOT / "ops-ui" / "ops_ui" / "app.py").read_text(encoding="utf-8")
        assert 'or "business").strip() or "business"' not in app


class TestCronDocs:
    def test_cron_uses_run_scheduled_not_prod_wrapper(self):
        for rel in ("deploy/cron/mk04.crontab", "deploy/cron/mk04.cron.d"):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert "run-scheduled.sh" in text
            # Must not tell cron to call the one-word prod wrapper
            assert "/usr/local/bin/prod" not in text
            assert "\nprod " not in text
