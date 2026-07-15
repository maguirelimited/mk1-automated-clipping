"""Tests for Storage Phase 9 — log rotation."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from config_manager import ConfigManager
from storage.artifact_classifier import ArtifactClassifier
from storage.log_rotation import (
    STATUS_FAIL,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    discover_active_logs,
    is_active_log_name,
    is_rotated_log_name,
    load_latest_rotation_record,
    load_log_rotation_config,
    render_journald_dropin,
    render_logrotate_config,
    rotate_active_log,
    run_log_rotation,
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


def _set_log_rotation(
    repo: Path,
    *,
    environment: str = "dev",
    enabled: bool = True,
    max_bytes: int = 100,
    backup_count: int = 3,
    compress: bool = True,
) -> None:
    import re

    token = "dev" if environment in {"dev", "development"} else "prod"
    path = repo / "config" / "environments" / f"{token}.yaml"
    text = path.read_text(encoding="utf-8")
    block = (
        f"  log_rotation:\n"
        f"    enabled: {str(enabled).lower()}\n"
        f"    max_bytes: {max_bytes}\n"
        f"    backup_count: {backup_count}\n"
        f"    compress: {str(compress).lower()}\n"
        f"    journal:\n"
        f"      system_max_use: 500M\n"
        f"      runtime_max_use: 100M\n"
        f"      max_file_sec: 1month\n"
    )
    if re.search(r"^  log_rotation:\n", text, flags=re.MULTILINE):
        text = re.sub(
            r"^  log_rotation:\n(?:    .+\n|      .+\n)+",
            block,
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        text = text.rstrip() + "\n" + block
    path.write_text(text, encoding="utf-8")


def test_active_and_rotated_name_detection() -> None:
    assert is_active_log_name("service.log")
    assert is_active_log_name("events.ndjson")
    assert is_active_log_name("analytics.jsonl")
    assert not is_active_log_name("service.log.1")
    assert not is_active_log_name("service.log.1.gz")
    assert is_rotated_log_name("service.log.1")
    assert is_rotated_log_name("service.log.2.gz")


def test_rotation_by_size(tmp_path: Path) -> None:
    active = tmp_path / "app.log"
    active.write_bytes(b"x" * 200)
    actions = rotate_active_log(
        active,
        max_bytes=100,
        backup_count=3,
        compress=False,
    )
    assert any(a.action == "rotated" for a in actions)
    assert active.is_file()
    assert active.stat().st_size == 0
    archive = tmp_path / "app.log.1"
    assert archive.is_file()
    assert archive.stat().st_size == 200


def test_rotation_history_and_compression(tmp_path: Path) -> None:
    active = tmp_path / "app.log"
    # First rotation
    active.write_bytes(b"a" * 150)
    rotate_active_log(active, max_bytes=100, backup_count=3, compress=True)
    assert (tmp_path / "app.log.1").is_file()
    # Second rotation shifts .1 -> .2.gz (delaycompress)
    active.write_bytes(b"b" * 150)
    actions = rotate_active_log(active, max_bytes=100, backup_count=3, compress=True)
    assert (tmp_path / "app.log.1").is_file()
    assert (tmp_path / "app.log.2.gz").is_file()
    with gzip.open(tmp_path / "app.log.2.gz", "rb") as handle:
        assert handle.read() == b"a" * 150
    assert any(a.action == "shifted_and_compressed" for a in actions)
    # Third rotation removes overflow beyond backup_count
    active.write_bytes(b"c" * 150)
    rotate_active_log(active, max_bytes=100, backup_count=3, compress=True)
    active.write_bytes(b"d" * 150)
    rotate_active_log(active, max_bytes=100, backup_count=3, compress=True)
    # Only indices 1..3 should remain
    assert (tmp_path / "app.log.1").is_file()
    assert not (tmp_path / "app.log.4").exists()
    assert not (tmp_path / "app.log.4.gz").exists()


def test_below_max_bytes_not_rotated(tmp_path: Path) -> None:
    active = tmp_path / "app.log"
    active.write_bytes(b"small")
    actions = rotate_active_log(active, max_bytes=100, backup_count=3, compress=True)
    assert actions == []
    assert active.read_bytes() == b"small"


def test_truncate_failure_preserves_active_log(tmp_path: Path) -> None:
    active = tmp_path / "app.log"
    active.write_bytes(b"keep-me" * 20)

    def boom(_path: Path) -> None:
        raise OSError("truncate denied")

    with pytest.raises(RuntimeError, match="active log preserved"):
        rotate_active_log(
            active,
            max_bytes=10,
            backup_count=2,
            compress=False,
            truncate_fn=boom,
        )
    # Active content preserved
    assert active.read_bytes() == b"keep-me" * 20
    # Archive also exists (no data loss)
    assert (tmp_path / "app.log.1").is_file()


def test_disabled_rotation_records_skip(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    _set_log_rotation(tmp_path, enabled=False)
    resolved = _resolved(tmp_path)
    records_dir = tmp_path / "data" / "dev" / "storage"
    result = run_log_rotation(resolved, records_dir=records_dir)
    assert result.status == STATUS_SKIPPED
    latest = load_latest_rotation_record(records_dir=records_dir)
    assert latest is not None
    assert latest["status"] == STATUS_SKIPPED


def test_run_log_rotation_rotates_and_records(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    _set_log_rotation(tmp_path, enabled=True, max_bytes=50, backup_count=3)
    logs_root = tmp_path / "logs" / "dev"
    logs_root.mkdir(parents=True)
    active = logs_root / "worker.log"
    active.write_bytes(b"z" * 80)
    records_dir = tmp_path / "data" / "dev" / "storage"
    resolved = _resolved(tmp_path)

    result = run_log_rotation(
        resolved,
        logs_root=logs_root,
        records_dir=records_dir,
    )
    assert result.status == STATUS_SUCCESS
    assert result.rotated_count == 1
    assert active.stat().st_size == 0
    assert (logs_root / "worker.log.1").is_file()
    latest = load_latest_rotation_record(records_dir=records_dir)
    assert latest["rotated_count"] == 1
    assert str(active) in latest["active_log_sizes"]


def test_config_validation_rejects_bad_max_bytes(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    path = tmp_path / "config" / "environments" / "dev.yaml"
    text = path.read_text(encoding="utf-8")
    text = text.replace("max_bytes: 104857600", "max_bytes: 0")
    text = text.replace("max_bytes: 52428800", "max_bytes: 0")
    # Fixture may use 104857600 from schedule insert
    if "max_bytes: 0" not in text:
        text = text.replace(
            "backup_count: 8",
            "max_bytes: 0\n    backup_count: 8",
            1,
        )
    path.write_text(text, encoding="utf-8")

    from config_manager import ConfigError

    with pytest.raises(ConfigError) as exc_info:
        _resolved(tmp_path)
    assert "log_rotation.max_bytes" in str(exc_info.value)


def test_rotated_logs_are_service_log_artifacts(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    resolved = _resolved(tmp_path)
    logs_root = tmp_path / "logs" / "dev"
    logs_root.mkdir(parents=True)
    rotated = logs_root / "api.log.1.gz"
    with gzip.open(rotated, "wb") as handle:
        handle.write(b"old")
    classifier = ArtifactClassifier(resolved, now=FIXED_NOW)
    record = classifier.classify(rotated)
    assert record.artifact_type == "service_log"


def test_retention_applies_logs_days_to_rotated_logs(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config", retention_enabled=True)
    resolved = _resolved(tmp_path)
    logs_root = tmp_path / "logs" / "dev"
    logs_root.mkdir(parents=True)
    rotated = logs_root / "api.log.2.gz"
    with gzip.open(rotated, "wb") as handle:
        handle.write(b"old")
    _touch_age(rotated, days=40)

    plan = RetentionPlanner(resolved, now=FIXED_NOW).plan_dry_run()
    decisions = [d for d in plan.eligible_files if d.path.endswith("api.log.2.gz")]
    assert len(decisions) == 1
    assert decisions[0].artifact_type == "service_log"
    assert decisions[0].retention_days == 30  # fixture logs_days


def test_render_journal_and_logrotate_from_config(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    resolved = _resolved(tmp_path)
    config = load_log_rotation_config(resolved)
    journal = render_journald_dropin(config)
    assert "SystemMaxUse=" in journal
    assert "[Journal]" in journal
    logrotate = render_logrotate_config(config, env_token="prod")
    assert "copytruncate" in logrotate
    assert f"rotate {config.backup_count}" in logrotate


def test_discover_active_logs_ignores_rotated(tmp_path: Path) -> None:
    root = tmp_path / "logs"
    root.mkdir()
    (root / "a.log").write_text("x")
    (root / "a.log.1").write_text("y")
    (root / "notes.txt").write_text("z")
    found = discover_active_logs(root)
    assert [p.name for p in found] == ["a.log"]


def test_rotation_failure_recorded_without_losing_active(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    _set_log_rotation(tmp_path, enabled=True, max_bytes=10)
    logs_root = tmp_path / "logs" / "dev"
    logs_root.mkdir(parents=True)
    active = logs_root / "broken.log"
    active.write_bytes(b"payload-data")
    records_dir = tmp_path / "data" / "dev" / "storage"
    resolved = _resolved(tmp_path)

    def failing_rotate(path, **_kwargs):
        raise OSError("disk full")

    result = run_log_rotation(
        resolved,
        logs_root=logs_root,
        records_dir=records_dir,
        rotate_fn=failing_rotate,
    )
    assert result.status == STATUS_FAIL
    assert result.failure_count == 1
    assert active.read_bytes() == b"payload-data"
    latest = load_latest_rotation_record(records_dir=records_dir)
    assert latest["failure_count"] == 1
