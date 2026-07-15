"""Tests for backup and cleanup ops scripts (Remote Operations Prompt 9)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"
BACKUP_SH = OPS_DIR / "backup.sh"
CLEANUP_SH = OPS_DIR / "cleanup.sh"


def _run_bash(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _import_backup_control():
    if str(OPS_DIR) not in sys.path:
        sys.path.insert(0, str(OPS_DIR))
    import backup_control  # noqa: PLC0415

    return backup_control


def _import_cleanup_control():
    if str(OPS_DIR) not in sys.path:
        sys.path.insert(0, str(OPS_DIR))
    import cleanup_control  # noqa: PLC0415

    return cleanup_control


def _seed_env_tree(root: Path, token: str) -> dict[str, Path]:
    data_root = root / "data" / token
    jobs_root = root / "jobs" / token
    logs_root = root / "logs" / token
    reports_root = root / "reports" / token
    runs_root = root / "runs" / token
    outputs_root = root / "outputs" / token
    caches_root = data_root / "cache"
    database_path = root / "database" / f"{token}.db"
    backup_root = root / "backups" / token

    for path in (
        data_root,
        jobs_root / "job_1",
        logs_root,
        reports_root / "job_1",
        runs_root / "run_1",
        outputs_root / "clips",
        caches_root,
        database_path.parent,
        backup_root,
    ):
        path.mkdir(parents=True, exist_ok=True)

    (data_root / "control_state.json").write_text(
        json.dumps({"uploads_disabled": False, "scheduler_disabled": False}),
        encoding="utf-8",
    )
    (data_root / ".env").write_text("SECRET=should-not-backup\n", encoding="utf-8")
    (jobs_root / "job_1" / "task.json").write_text('{"job_id":"job_1"}', encoding="utf-8")
    (jobs_root / "job_1" / "resolved_config.yaml").write_text("environment: test\n", encoding="utf-8")
    (jobs_root / "job_1" / "__pycache__").mkdir()
    (jobs_root / "job_1" / "__pycache__" / "x.pyc").write_bytes(b"pyc")
    (jobs_root / "job_1" / "clip.mp4").write_bytes(b"not-a-real-video")
    (reports_root / "job_1" / "report.json").write_text('{"ok":true}', encoding="utf-8")
    (runs_root / "run_1" / "run.json").write_text('{"run":1}', encoding="utf-8")
    (logs_root / "worker.log").write_text("log line\n", encoding="utf-8")
    (outputs_root / "clips" / "final.mp4").write_bytes(b"clip-bytes")
    database_path.write_bytes(b"sqlite-fake")

    return {
        "data_root": data_root,
        "jobs_root": jobs_root,
        "logs_root": logs_root,
        "reports_root": reports_root,
        "database_path": database_path,
        "outputs_root": outputs_root,
        "runs_root": runs_root,
        "backup_root": backup_root,
        "caches_root": caches_root,
    }


def _patch_backup_paths(backup_control, root: Path, token: str, paths: dict[str, Path], monkeypatch):
    monkeypatch.setattr(backup_control, "REPO_ROOT", root)

    def _resolve(_canonical: str) -> dict[str, Path]:
        return paths

    monkeypatch.setattr(backup_control, "resolve_env_paths", _resolve)


class TestBackupScriptInterface:
    def test_help_flags(self):
        for flag in ("--help", "-h"):
            result = _run_bash(BACKUP_SH, flag)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "Usage:" in result.stdout

    def test_missing_env_fails(self):
        assert _run_bash(BACKUP_SH).returncode != 0

    def test_invalid_env_fails(self):
        assert _run_bash(BACKUP_SH, "staging").returncode != 0


class TestBackupControl:
    def test_backup_creates_archive_and_manifest(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        backup_control = _import_backup_control()
        paths = _seed_env_tree(tmp_path, "dev")
        _patch_backup_paths(backup_control, tmp_path, "dev", paths, monkeypatch)
        now = datetime(2026, 7, 3, 19, 0, 0, tzinfo=UTC)

        assert backup_control.create_backup("dev", now=now) == 0

        archive = paths["backup_root"] / "backup_dev_2026-07-03T190000Z.tar.gz"
        manifest_path = paths["backup_root"] / "backup_dev_2026-07-03T190000Z.manifest.json"
        assert archive.is_file()
        assert manifest_path.is_file()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["backup_id"] == "backup_dev_2026-07-03T190000Z"
        assert manifest["environment"] == "dev"
        assert "included_paths" in manifest
        assert "skipped_paths" in manifest
        assert "excluded_patterns" in manifest
        assert manifest["bytes_written"] > 0

        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        assert "manifest.json" in names
        assert "data/dev/control_state.json" in names
        assert "database/dev.db" in names
        assert "jobs/dev/job_1/task.json" in names
        assert "jobs/dev/job_1/resolved_config.yaml" in names
        assert "reports/dev/job_1/report.json" in names
        assert "runs/dev/run_1/run.json" in names
        assert "logs/dev/worker.log" in names

    def test_backup_excludes_env_pycache_and_media(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        backup_control = _import_backup_control()
        paths = _seed_env_tree(tmp_path, "dev")
        _patch_backup_paths(backup_control, tmp_path, "dev", paths, monkeypatch)

        backup_control.create_backup("dev", now=datetime(2026, 7, 3, 19, 0, 0, tzinfo=UTC))
        archive = paths["backup_root"] / "backup_dev_2026-07-03T190000Z.tar.gz"
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()

        assert "data/dev/.env" not in names
        assert not any("__pycache__" in name for name in names)
        assert not any(name.endswith(".mp4") for name in names)
        assert not any("outputs/" in name for name in names)

    def test_backup_handles_missing_optional_dirs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        backup_control = _import_backup_control()
        paths = _seed_env_tree(tmp_path, "dev")
        # Remove optional trees.
        for key in ("runs_root", "reports_root", "logs_root"):
            target = paths[key]
            if target.exists():
                for child in target.rglob("*"):
                    if child.is_file():
                        child.unlink()
                # leave empty or remove
        import shutil

        shutil.rmtree(paths["runs_root"])
        shutil.rmtree(paths["reports_root"])
        shutil.rmtree(paths["logs_root"])
        _patch_backup_paths(backup_control, tmp_path, "dev", paths, monkeypatch)

        assert backup_control.create_backup("dev", now=datetime(2026, 7, 3, 19, 0, 0, tzinfo=UTC)) == 0
        manifest = json.loads(
            (paths["backup_root"] / "backup_dev_2026-07-03T190000Z.manifest.json").read_text(encoding="utf-8")
        )
        skipped = {item["path"]: item["reason"] for item in manifest["skipped_paths"]}
        assert any("runs/dev" in path and reason == "missing" for path, reason in skipped.items())
        assert any("reports/dev" in path and reason == "missing" for path, reason in skipped.items())

    def test_dev_and_prod_backup_paths_are_separate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        backup_control = _import_backup_control()
        dev_paths = _seed_env_tree(tmp_path, "dev")
        prod_paths = _seed_env_tree(tmp_path, "prod")
        monkeypatch.setattr(backup_control, "REPO_ROOT", tmp_path)

        def _resolve(canonical: str) -> dict[str, Path]:
            return dev_paths if canonical == "development" else prod_paths

        monkeypatch.setattr(backup_control, "resolve_env_paths", _resolve)
        now = datetime(2026, 7, 3, 19, 0, 0, tzinfo=UTC)
        backup_control.create_backup("dev", now=now)
        backup_control.create_backup("prod", now=now)

        assert (dev_paths["backup_root"] / "backup_dev_2026-07-03T190000Z.tar.gz").is_file()
        assert (prod_paths["backup_root"] / "backup_prod_2026-07-03T190000Z.tar.gz").is_file()
        assert not (dev_paths["backup_root"] / "backup_prod_2026-07-03T190000Z.tar.gz").exists()


class TestCleanupScriptInterface:
    def test_help_flags(self):
        for flag in ("--help", "-h"):
            result = _run_bash(CLEANUP_SH, flag)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "Usage:" in result.stdout

    def test_missing_env_fails(self):
        assert _run_bash(CLEANUP_SH).returncode != 0

    def test_invalid_env_fails(self):
        assert _run_bash(CLEANUP_SH, "staging", "--dry-run").returncode != 0

    def test_missing_mode_fails(self):
        assert _run_bash(CLEANUP_SH, "dev").returncode != 0


class TestCleanupControl:
    def test_dry_run_exits_zero_and_deletes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        cleanup_control = _import_cleanup_control()
        paths = _seed_env_tree(tmp_path, "dev")
        marker = paths["jobs_root"] / "job_1" / "task.json"
        before = marker.read_text(encoding="utf-8")

        monkeypatch.setattr(cleanup_control, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(cleanup_control, "resolve_known_dirs", lambda _canonical: paths)

        assert cleanup_control.run_cleanup("dev", mode="dry-run") == 0
        assert marker.read_text(encoding="utf-8") == before
        assert (paths["outputs_root"] / "clips" / "final.mp4").is_file()

    def test_apply_refuses_safely(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        cleanup_control = _import_cleanup_control()
        paths = _seed_env_tree(tmp_path, "dev")
        monkeypatch.setattr(cleanup_control, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(cleanup_control, "resolve_known_dirs", lambda _canonical: paths)

        assert cleanup_control.run_cleanup("dev", mode="apply") == 1
        assert (paths["jobs_root"] / "job_1" / "task.json").is_file()

    def test_prod_apply_refuses_safely(self):
        result = _run_bash(CLEANUP_SH, "prod", "--apply")
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "not implemented" in combined.lower() or "retention" in combined.lower()
        assert "No files deleted" in combined

    def test_dry_run_shell_exits_zero(self):
        result = _run_bash(CLEANUP_SH, "dev", "--dry-run")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Cleanup dry-run" in result.stdout
        assert "Would delete: 0 files" in result.stdout
        assert "No files deleted." in result.stdout

    def test_cleanup_source_has_no_deletion_calls(self):
        sources = [
            OPS_DIR / "cleanup.sh",
            OPS_DIR / "cleanup_control.py",
        ]
        patterns = ("rm -rf", "find ", "shutil.rmtree", "os.remove", ".unlink(", "unlink(")
        for path in sources:
            text = path.read_text(encoding="utf-8")
            for pattern in patterns:
                if pattern == "find " and "find " not in text:
                    continue
                assert pattern not in text, f"{path} contains forbidden pattern {pattern!r}"
