"""Tests for runtime upload kill switch (Remote Operations Prompt 7)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"
DISABLE_SH = OPS_DIR / "disable-uploads.sh"
ENABLE_SH = OPS_DIR / "enable-uploads.sh"


def _run_bash(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _import_upload_control():
    if str(OPS_DIR) not in sys.path:
        sys.path.insert(0, str(OPS_DIR))
    import upload_control  # noqa: PLC0415

    return upload_control


class TestEffectiveUploadLogic:
    def test_effective_upload_state(self):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        from ops_readonly import compute_effective_upload

        assert compute_effective_upload(True, True) == (False, "blocked by runtime control")
        assert compute_effective_upload(False, False) == (False, "blocked by config")
        assert compute_effective_upload(True, False) == (True, "config enabled and runtime control allows")
        assert compute_effective_upload(True, None)[0] is None
        assert compute_effective_upload(False, None) == (False, "blocked by config")


class TestUploadControlWrites:
    def test_disable_writes_uploads_disabled_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        upload_control = _import_upload_control()
        dev_root = tmp_path / "data" / "dev"
        monkeypatch.setattr(upload_control, "resolve_data_root", lambda _canonical: dev_root)
        monkeypatch.setattr(upload_control, "_load_config_upload_enabled", lambda _canonical: (False, ""))

        assert upload_control.set_runtime_uploads_disabled("dev", disabled=True, reason="test") == 0
        payload = json.loads((dev_root / "control_state.json").read_text(encoding="utf-8"))
        assert payload["uploads_disabled"] is True
        assert payload["environment"] == "dev"

    def test_enable_writes_uploads_disabled_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        upload_control = _import_upload_control()
        prod_root = tmp_path / "data" / "prod"
        prod_root.mkdir(parents=True)
        (prod_root / "control_state.json").write_text(
            json.dumps({"uploads_disabled": True, "maintenance_mode": True}),
            encoding="utf-8",
        )
        monkeypatch.setattr(upload_control, "resolve_data_root", lambda _canonical: prod_root)
        monkeypatch.setattr(upload_control, "_load_config_upload_enabled", lambda _canonical: (True, ""))

        assert (
            upload_control.set_runtime_uploads_disabled(
                "prod",
                disabled=False,
                reason="test",
                require_prod_confirm=True,
                confirmed=True,
            )
            == 0
        )
        payload = json.loads((prod_root / "control_state.json").read_text(encoding="utf-8"))
        assert payload["uploads_disabled"] is False
        assert payload["maintenance_mode"] is True

    def test_dev_and_prod_control_paths_are_separate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        upload_control = _import_upload_control()
        dev_root = tmp_path / "data" / "dev"
        prod_root = tmp_path / "data" / "prod"

        def _resolve(canonical: str) -> Path:
            return dev_root if canonical == "development" else prod_root

        monkeypatch.setattr(upload_control, "resolve_data_root", _resolve)
        monkeypatch.setattr(upload_control, "_load_config_upload_enabled", lambda _canonical: (False, ""))

        upload_control.set_runtime_uploads_disabled("dev", disabled=True, reason="test")
        upload_control.set_runtime_uploads_disabled(
            "prod",
            disabled=False,
            reason="test",
            require_prod_confirm=True,
            confirmed=True,
        )

        dev_payload = json.loads((dev_root / "control_state.json").read_text(encoding="utf-8"))
        prod_payload = json.loads((prod_root / "control_state.json").read_text(encoding="utf-8"))
        assert dev_payload["uploads_disabled"] is True
        assert prod_payload["uploads_disabled"] is False

    def test_enable_does_not_force_config_upload_enabled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
        upload_control = _import_upload_control()
        prod_root = tmp_path / "data" / "prod"
        monkeypatch.setattr(upload_control, "resolve_data_root", lambda _canonical: prod_root)
        monkeypatch.setattr(upload_control, "_load_config_upload_enabled", lambda _canonical: (False, ""))

        upload_control.set_runtime_uploads_disabled(
            "prod",
            disabled=False,
            reason="test",
            require_prod_confirm=True,
            confirmed=True,
        )
        output = capsys.readouterr().out
        assert "Runtime uploads disabled: NO" in output
        assert "Config upload enabled: NO" in output
        assert "Effective real posting: NO" in output


class TestUploadControlProdConfirm:
    def test_prod_enable_without_confirm_fails(self, monkeypatch: pytest.MonkeyPatch):
        upload_control = _import_upload_control()
        monkeypatch.setattr(
            upload_control,
            "resolve_data_root",
            lambda _canonical: (_ for _ in ()).throw(AssertionError("should not resolve")),
        )

        rc = upload_control.set_runtime_uploads_disabled(
            "prod",
            disabled=False,
            reason="test",
            require_prod_confirm=True,
            confirmed=False,
        )
        assert rc == 1

    def test_enable_uploads_prod_without_confirm_fails(self):
        result = _run_bash(ENABLE_SH, "prod")
        assert result.returncode != 0
        assert "--confirm" in result.stderr or "--confirm" in result.stdout


class TestUploadControlShellInterface:
    def test_help_flags(self):
        for script in (DISABLE_SH, ENABLE_SH):
            for flag in ("--help", "-h"):
                result = _run_bash(script, flag)
                assert result.returncode == 0, result.stdout + result.stderr
                assert "Usage:" in result.stdout

    def test_missing_env_fails(self):
        assert _run_bash(DISABLE_SH).returncode != 0
        assert _run_bash(ENABLE_SH).returncode != 0

    def test_invalid_env_fails(self):
        assert _run_bash(DISABLE_SH, "staging").returncode != 0
        assert _run_bash(ENABLE_SH, "staging").returncode != 0

    def test_scripts_do_not_delete_jobs_or_clips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        upload_control = _import_upload_control()
        dev_root = tmp_path / "data" / "dev"
        jobs_root = tmp_path / "jobs" / "dev" / "job_test"
        clips_root = tmp_path / "outputs" / "dev" / "clips"
        jobs_root.mkdir(parents=True)
        clips_root.mkdir(parents=True)
        (jobs_root / "task.json").write_text("{}", encoding="utf-8")
        (clips_root / "clip.mp4").write_bytes(b"clip")

        monkeypatch.setattr(upload_control, "resolve_data_root", lambda _canonical: dev_root)
        monkeypatch.setattr(upload_control, "_load_config_upload_enabled", lambda _canonical: (False, ""))

        upload_control.set_runtime_uploads_disabled("dev", disabled=True, reason="test")
        upload_control.set_runtime_uploads_disabled("dev", disabled=False, reason="test")

        assert (jobs_root / "task.json").is_file()
        assert (clips_root / "clip.mp4").is_file()
        assert (dev_root / "control_state.json").is_file()
