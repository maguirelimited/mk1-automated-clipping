"""Tests for Storage Phase 10 — database backup rotation."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from config_manager import ConfigManager
from storage.artifact_classifier import ArtifactClassifier
from storage.database_backup import (
    STATUS_FAIL,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    create_sqlite_backup,
    load_latest_backup_record,
    load_database_backup_config,
    run_database_backup,
)
from storage.retention_planner import RetentionPlanner
from test_retention_planner import FIXED_NOW, _build_config_tree, _touch_age


def _resolved(repo: Path, environment: str = "dev"):
    return ConfigManager.load(
        environment=environment,
        funnel_id="business",
        platform_id="youtube",
        config_root=repo / "config",
    )


def _set_database_backup(
    repo: Path,
    *,
    environment: str = "dev",
    enabled: bool = True,
    verify_integrity: bool = True,
    location: str = "backups/{env}/database",
) -> None:
    import re

    token = "dev" if environment in {"dev", "development"} else "prod"
    path = repo / "config" / "environments" / f"{token}.yaml"
    text = path.read_text(encoding="utf-8")
    block = (
        f"  database_backup:\n"
        f"    enabled: {str(enabled).lower()}\n"
        f"    verify_integrity: {str(verify_integrity).lower()}\n"
        f"    location: {location}\n"
    )
    if re.search(r"^  database_backup:\n", text, flags=re.MULTILINE):
        text = re.sub(
            r"^  database_backup:\n(?:    .+\n)+",
            block,
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        text = text.rstrip() + "\n" + block
    path.write_text(text, encoding="utf-8")


def _make_sqlite(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO items (name) VALUES ('alpha'), ('beta')")
        conn.commit()
    finally:
        conn.close()


def test_successful_backup_creates_file_and_metadata(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    _set_database_backup(tmp_path, enabled=True)
    db_path = tmp_path / "database" / "dev.db"
    _make_sqlite(db_path)
    resolved = _resolved(tmp_path)
    records_dir = tmp_path / "data" / "dev" / "storage"
    backup_dir = tmp_path / "backups" / "dev" / "database"
    moment = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)

    result = run_database_backup(
        resolved,
        now=moment,
        records_dir=records_dir,
        backup_dir=backup_dir,
    )
    assert result.status == STATUS_SUCCESS
    assert result.backup_path is not None
    backup = Path(result.backup_path)
    assert backup.is_file()
    assert backup.stat().st_size > 0
    assert result.manifest_path is not None
    manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
    assert manifest["integrity_ok"] is True
    assert manifest["source_database"] == str(db_path)

    # Live DB unchanged and readable.
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT name FROM items ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == [("alpha",), ("beta",)]

    # Backup is a usable SQLite file.
    conn = sqlite3.connect(str(backup))
    try:
        rows = conn.execute("SELECT name FROM items ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == [("alpha",), ("beta",)]

    latest = load_latest_backup_record(records_dir=records_dir)
    assert latest is not None
    assert latest["status"] == STATUS_SUCCESS
    assert latest["backup_path"] == str(backup)


def test_missing_database_records_failure(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    _set_database_backup(tmp_path, enabled=True)
    resolved = _resolved(tmp_path)
    records_dir = tmp_path / "data" / "dev" / "storage"
    backup_dir = tmp_path / "backups" / "dev" / "database"

    result = run_database_backup(
        resolved,
        records_dir=records_dir,
        backup_dir=backup_dir,
    )
    assert result.status == STATUS_FAIL
    assert "does not exist" in (result.reason or "")
    assert list(backup_dir.glob("db_*.sqlite3")) == []
    latest = load_latest_backup_record(records_dir=records_dir)
    assert latest["status"] == STATUS_FAIL


def test_failed_backup_preserves_previous_success(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    _set_database_backup(tmp_path, enabled=True)
    db_path = tmp_path / "database" / "dev.db"
    _make_sqlite(db_path)
    resolved = _resolved(tmp_path)
    records_dir = tmp_path / "data" / "dev" / "storage"
    backup_dir = tmp_path / "backups" / "dev" / "database"

    first = run_database_backup(
        resolved,
        now=datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC),
        records_dir=records_dir,
        backup_dir=backup_dir,
    )
    assert first.status == STATUS_SUCCESS
    previous = Path(first.backup_path)
    assert previous.is_file()

    def boom(_src: Path, _dest: Path) -> None:
        raise RuntimeError("disk full")

    second = run_database_backup(
        resolved,
        now=datetime(2026, 7, 4, 13, 0, 0, tzinfo=UTC),
        records_dir=records_dir,
        backup_dir=backup_dir,
        create_fn=boom,
    )
    assert second.status == STATUS_FAIL
    assert previous.is_file()
    assert previous.read_bytes()  # still intact
    assert not list(backup_dir.glob("*.tmp"))


def test_disabled_backup_skips(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    _set_database_backup(tmp_path, enabled=False)
    resolved = _resolved(tmp_path)
    records_dir = tmp_path / "data" / "dev" / "storage"
    result = run_database_backup(resolved, records_dir=records_dir)
    assert result.status == STATUS_SKIPPED
    latest = load_latest_backup_record(records_dir=records_dir)
    assert latest["status"] == STATUS_SKIPPED


def test_config_validation_requires_location(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    path = tmp_path / "config" / "environments" / "dev.yaml"
    text = path.read_text(encoding="utf-8")
    text = text.replace("location: backups/{env}/database\n", "location: ''\n")
    path.write_text(text, encoding="utf-8")

    from config_manager import ConfigError

    with pytest.raises(ConfigError) as exc_info:
        _resolved(tmp_path)
    assert "database_backup.location" in str(exc_info.value)


def test_backup_classified_as_database_backup(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    resolved = _resolved(tmp_path)
    backup = tmp_path / "backups" / "dev" / "database" / "db_dev_20260704T120000Z.sqlite3"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"sqlite")
    manifest = backup.with_name(backup.name.replace(".sqlite3", ".manifest.json"))
    manifest.write_text("{}", encoding="utf-8")

    classifier = ArtifactClassifier(resolved, now=FIXED_NOW)
    assert classifier.classify(backup).artifact_type == "database_backup"
    assert classifier.classify(manifest).artifact_type == "database_backup"


def test_active_database_is_never_database_backup(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    db_path = tmp_path / "database" / "dev.db"
    _make_sqlite(db_path)
    resolved = _resolved(tmp_path)
    classifier = ArtifactClassifier(resolved, now=FIXED_NOW)
    record = classifier.classify(db_path)
    assert record.artifact_type == "database"


def test_retention_applies_database_backups_days(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config", retention_enabled=True)
    resolved = _resolved(tmp_path)
    backup = tmp_path / "backups" / "dev" / "database" / "db_dev_old.sqlite3"
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"x" * 32)
    _touch_age(backup, days=40)

    plan = RetentionPlanner(resolved, now=FIXED_NOW).plan_dry_run()
    decisions = [d for d in plan.eligible_files if d.path.endswith("db_dev_old.sqlite3")]
    assert len(decisions) == 1
    assert decisions[0].artifact_type == "database_backup"
    assert decisions[0].retention_days == 30


def test_active_database_not_eligible_for_retention(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config", retention_enabled=True)
    db_path = tmp_path / "database" / "dev.db"
    _make_sqlite(db_path)
    _touch_age(db_path, days=400)
    resolved = _resolved(tmp_path)
    plan = RetentionPlanner(resolved, now=FIXED_NOW).plan_dry_run()
    db_decisions = [d for d in plan.protected_files if d.path.endswith("dev.db")]
    assert db_decisions
    assert all(d.disposition == "protected" for d in db_decisions)
    assert all(d.artifact_type == "database" for d in db_decisions)


def test_integrity_failure_does_not_publish_backup(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    _set_database_backup(tmp_path, enabled=True, verify_integrity=True)
    db_path = tmp_path / "database" / "dev.db"
    _make_sqlite(db_path)
    resolved = _resolved(tmp_path)
    records_dir = tmp_path / "data" / "dev" / "storage"
    backup_dir = tmp_path / "backups" / "dev" / "database"

    def bad_verify(_path: Path):
        return False, "not ok"

    result = run_database_backup(
        resolved,
        records_dir=records_dir,
        backup_dir=backup_dir,
        verify_fn=bad_verify,
    )
    assert result.status == STATUS_FAIL
    assert "integrity" in (result.reason or "").lower()
    assert list(backup_dir.glob("db_*.sqlite3")) == []
    assert list(backup_dir.glob("*.tmp")) == []


def test_create_sqlite_backup_does_not_write_source(tmp_path: Path) -> None:
    source = tmp_path / "live.db"
    dest = tmp_path / "copy.sqlite3"
    _make_sqlite(source)
    before = source.read_bytes()
    create_sqlite_backup(source, dest)
    assert source.read_bytes() == before
    assert dest.is_file()


def test_load_config_from_resolved(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    resolved = _resolved(tmp_path)
    config = load_database_backup_config(resolved)
    assert config.enabled is True
    assert config.verify_integrity is True
    assert "{env}" in config.location
