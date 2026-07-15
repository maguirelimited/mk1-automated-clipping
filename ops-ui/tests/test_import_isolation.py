"""Regression tests for Ops UI import isolation and launch entrypoints.

Import-side-effect checks run in a subprocess so they do not delete
``ops_ui.app`` from ``sys.modules`` mid-suite (that would leave other test
modules holding a stale ``create_app`` while patches target a new module).
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest
from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.store import ControlStore


def _isolated_settings(tmp_path: Path) -> Settings:
    return Settings(
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
        runtime_root=tmp_path / "runtime",
        services=(
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-video-automation.service",
            ),
        ),
    )


def _run_isolated(code: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Execute ``code`` in a fresh interpreter with a clean MK04 env."""
    ops_ui_root = Path(__file__).resolve().parents[1]
    run_env = os.environ.copy()
    run_env.pop("MK04_RUNTIME_ROOT", None)
    run_env.pop("MK04_ENV", None)
    run_env.pop("MK04_DATA_ROOT", None)
    run_env.pop("MK04_JOBS_ROOT", None)
    if env:
        run_env.update(env)
    # Prefer the ops-ui package under test.
    pythonpath = run_env.get("PYTHONPATH", "")
    parts = [str(ops_ui_root)]
    if pythonpath:
        parts.append(pythonpath)
    run_env["PYTHONPATH"] = os.pathsep.join(parts)
    return subprocess.run(
        [sys.executable, "-c", dedent(code)],
        capture_output=True,
        text=True,
        env=run_env,
        cwd=str(ops_ui_root.parent),
        check=False,
    )


class TestImportDoesNotPolluteRuntimeEnv:
    def test_importing_ops_ui_app_does_not_set_mk04_runtime_root(self) -> None:
        result = _run_isolated(
            """
            import os
            import ops_ui.app as app_mod
            assert "MK04_RUNTIME_ROOT" not in os.environ, os.environ.get("MK04_RUNTIME_ROOT")
            assert hasattr(app_mod, "create_app")
            assert not hasattr(app_mod, "app")
            """
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_importing_factory_does_not_create_default_runtime_dirs(
        self, tmp_path: Path
    ) -> None:
        ops_data = tmp_path / "ops_data"
        result = _run_isolated(
            f"""
            import os
            from pathlib import Path
            ops_data = Path({str(ops_data)!r})
            assert not ops_data.exists()
            import ops_ui.app  # noqa: F401
            assert "MK04_RUNTIME_ROOT" not in os.environ
            # Factory import alone must not mkdir data/db under the configured dir.
            assert not ops_data.exists()
            assert not (ops_data / "ops_ui.sqlite3").exists()
            """
            ,
            env={"OPS_UI_DATA_DIR": str(ops_data)},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert not ops_data.exists()


class TestCreateAppUsesSuppliedRuntimeRoot:
    def test_create_app_assigns_settings_runtime_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MK04_RUNTIME_ROOT", "/var/lib/mk04/dev")
        settings = _isolated_settings(tmp_path)
        create_app(settings)
        assert os.environ["MK04_RUNTIME_ROOT"] == str(settings.runtime_root)
        assert os.environ["MK04_RUNTIME_ROOT"] != "/var/lib/mk04/dev"


class TestPolicyControlRetired:
    def test_policy_control_post_returns_410_without_mutating_controls(
        self, tmp_path: Path
    ) -> None:
        from ops_ui.control_export import HUMAN_APPROVAL_REQUIRED, PUBLISH_APPROVED_ONLY

        settings = _isolated_settings(tmp_path)
        (tmp_path / "controls.json").write_text("{}", encoding="utf-8")
        app = create_app(settings)
        client = app.test_client()
        store = ControlStore(settings.control_db_path, controls_file=settings.controls_file)
        store.init_db()
        before = json.loads(settings.controls_file.read_text(encoding="utf-8"))

        for control in (HUMAN_APPROVAL_REQUIRED, PUBLISH_APPROVED_ONLY):
            response = client.post(
                f"/clip-review/controls/{control}/on", follow_redirects=False
            )
            assert response.status_code == 410
            assert "retired" in (response.get_data(as_text=True) or "").lower()

        after = json.loads(settings.controls_file.read_text(encoding="utf-8"))
        assert after == before


class TestWsgiEntrypointRemainsImportable:
    def test_wsgi_module_exports_app(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Isolate WSGI construction away from live /var/lib defaults.
        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.setenv("MK04_RUNTIME_ROOT", str(tmp_path / "runtime"))
        monkeypatch.setenv("OPS_UI_DATA_DIR", str(tmp_path / "ops_data"))
        monkeypatch.setenv("MK04_CONTROLS_FILE", str(tmp_path / "controls.json"))
        # Fresh WSGI module only — do not reload ops_ui.app in-process.
        for name in list(sys.modules):
            if name == "ops_ui.wsgi" or name.startswith("ops_ui.wsgi."):
                del sys.modules[name]
        wsgi = importlib.import_module("ops_ui.wsgi")
        assert wsgi.app is not None
        assert hasattr(wsgi.app, "test_client")

    def test_main_module_remains_importable(self) -> None:
        main = importlib.import_module("ops_ui.__main__")
        assert callable(main.main)
