"""Tests for Storage Phase 7 — disk pressure checks."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from config_manager import ConfigManager
from storage.disk_pressure import (
    DiskPressureLevel,
    DiskPressureThresholds,
    can_start_new_job,
    classify_disk_pressure,
    evaluate_disk_pressure,
    format_health_detail,
    health_result_for_level,
    load_disk_pressure_thresholds,
    record_disk_pressure_block,
)

_STORAGE_TESTS = Path(__file__).resolve().parent
if str(_STORAGE_TESTS) not in sys.path:
    sys.path.insert(0, str(_STORAGE_TESTS))
from test_retention_planner import _build_config_tree  # noqa: E402

DEFAULT_THRESHOLDS = DiskPressureThresholds(
    warning_percent=80,
    urgent_percent=90,
    critical_percent=95,
    reject_new_jobs_percent=98,
)


def _usage(total: int, used: int) -> SimpleNamespace:
    return SimpleNamespace(total=total, used=used, free=total - used)


def _resolved(repo: Path, environment: str = "dev"):
    return ConfigManager.load(
        environment=environment,
        funnel_id="business",
        platform_id="youtube",
        config_root=repo / "config",
    )


@pytest.mark.parametrize(
    ("percent", "expected"),
    [
        (0, DiskPressureLevel.NORMAL),
        (79.9, DiskPressureLevel.NORMAL),
        (80, DiskPressureLevel.WARNING),
        (89.9, DiskPressureLevel.WARNING),
        (90, DiskPressureLevel.URGENT),
        (94.9, DiskPressureLevel.URGENT),
        (95, DiskPressureLevel.CRITICAL),
        (97.9, DiskPressureLevel.CRITICAL),
        (98, DiskPressureLevel.REJECT_NEW_JOBS),
        (100, DiskPressureLevel.REJECT_NEW_JOBS),
    ],
)
def test_classify_disk_pressure_levels(percent: float, expected: DiskPressureLevel) -> None:
    assert classify_disk_pressure(percent, DEFAULT_THRESHOLDS) == expected


def test_classify_is_deterministic() -> None:
    first = classify_disk_pressure(91.2, DEFAULT_THRESHOLDS)
    second = classify_disk_pressure(91.2, DEFAULT_THRESHOLDS)
    assert first == second == DiskPressureLevel.URGENT


def test_load_thresholds_from_resolved_config(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    resolved = _resolved(tmp_path)
    thresholds = load_disk_pressure_thresholds(resolved)
    assert thresholds == DEFAULT_THRESHOLDS


def test_custom_thresholds_drive_classification(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    _build_config_tree(config_root)
    dev_yaml = config_root / "environments" / "dev.yaml"
    dev_yaml.write_text(
        dev_yaml.read_text(encoding="utf-8")
        + textwrap.dedent(
            """
            storage:
              disk_pressure:
                warning_percent: 70
                urgent_percent: 75
                critical_percent: 80
                reject_new_jobs_percent: 85
            """
        ),
        encoding="utf-8",
    )
    resolved = _resolved(tmp_path)
    thresholds = load_disk_pressure_thresholds(resolved)
    assert classify_disk_pressure(84, thresholds) == DiskPressureLevel.CRITICAL
    assert classify_disk_pressure(85, thresholds) == DiskPressureLevel.REJECT_NEW_JOBS


@pytest.mark.parametrize(
    ("used_percent", "level"),
    [
        (50, DiskPressureLevel.NORMAL),
        (82, DiskPressureLevel.WARNING),
        (91, DiskPressureLevel.URGENT),
        (96, DiskPressureLevel.CRITICAL),
        (99, DiskPressureLevel.REJECT_NEW_JOBS),
    ],
)
def test_evaluate_disk_pressure_mocked(
    tmp_path: Path,
    used_percent: int,
    level: DiskPressureLevel,
) -> None:
    _build_config_tree(tmp_path / "config")
    resolved = _resolved(tmp_path)
    data_root = tmp_path / "data" / "dev"
    data_root.mkdir(parents=True)

    def disk_usage_fn(_path: Path) -> SimpleNamespace:
        return _usage(1000, used_percent * 10)

    status = evaluate_disk_pressure(
        resolved,
        path=data_root,
        disk_usage_fn=disk_usage_fn,
    )
    assert status.level == level
    assert status.snapshot is not None
    assert status.snapshot.usage_percent == float(used_percent)
    assert status.snapshot.total_bytes == 1000
    assert status.retention_recommended == (
        level in {DiskPressureLevel.CRITICAL, DiskPressureLevel.REJECT_NEW_JOBS}
    )


def test_can_start_new_job_allows_dev_at_reject_level(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    resolved = _resolved(tmp_path)

    def disk_usage_fn(_path: Path) -> SimpleNamespace:
        return _usage(1000, 990)

    gate = can_start_new_job("dev", resolved, disk_usage_fn=disk_usage_fn)
    assert gate.allowed is True
    assert gate.is_production is False
    assert gate.status.level == DiskPressureLevel.REJECT_NEW_JOBS


def test_can_start_new_job_rejects_production_at_reject_threshold(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    resolved = _resolved(tmp_path, "prod")

    def disk_usage_fn(_path: Path) -> SimpleNamespace:
        return _usage(1000, 980)

    gate = can_start_new_job("prod", resolved, disk_usage_fn=disk_usage_fn)
    assert gate.allowed is False
    assert gate.is_production is True
    assert gate.status.level == DiskPressureLevel.REJECT_NEW_JOBS
    assert gate.reason is not None
    assert "98%" in gate.reason
    assert "exceeds production reject threshold" in gate.reason


def test_can_start_new_job_allows_production_below_reject(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    resolved = _resolved(tmp_path, "prod")

    def disk_usage_fn(_path: Path) -> SimpleNamespace:
        return _usage(1000, 960)

    gate = can_start_new_job("prod", resolved, disk_usage_fn=disk_usage_fn)
    assert gate.allowed is True
    assert gate.status.level == DiskPressureLevel.CRITICAL


def test_record_disk_pressure_block_appends_jsonl(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    resolved = _resolved(tmp_path, "prod")

    def disk_usage_fn(_path: Path) -> SimpleNamespace:
        return _usage(1000, 990)

    status = evaluate_disk_pressure(resolved, disk_usage_fn=disk_usage_fn)
    path = record_disk_pressure_block(
        environment="prod",
        status=status,
        reason="disk usage 99.0% exceeds production reject threshold 98%",
        repo_root=tmp_path,
        data_root=tmp_path / "data" / "prod",
        run_id="run_test",
        trigger="scheduled",
        funnel_id="business",
        timestamp="2026-07-04T12:00:00Z",
    )
    assert path.is_file()
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["schema_version"] == 1
    assert record["environment"] == "prod"
    assert record["usage_percent"] == 99.0
    assert record["reject_threshold_percent"] == 98
    assert record["pressure_level"] == "REJECT_NEW_JOBS"
    assert record["run_id"] == "run_test"
    assert record["trigger"] == "scheduled"


def test_health_result_mapping() -> None:
    assert health_result_for_level(DiskPressureLevel.NORMAL) == ("PASS", "info")
    assert health_result_for_level(DiskPressureLevel.WARNING) == ("WARN", "warn")
    assert health_result_for_level(DiskPressureLevel.URGENT) == ("WARN", "warn")
    assert health_result_for_level(DiskPressureLevel.CRITICAL) == ("FAIL", "fail")
    assert health_result_for_level(DiskPressureLevel.REJECT_NEW_JOBS) == ("FAIL", "fail")


def test_format_health_detail_includes_storage_state(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    resolved = _resolved(tmp_path)

    def disk_usage_fn(_path: Path) -> SimpleNamespace:
        return _usage(1000, 870)

    status = evaluate_disk_pressure(resolved, disk_usage_fn=disk_usage_fn)
    detail = format_health_detail(status)
    assert "storage_state=WARNING" in detail
    assert "87.0% used" in detail


def test_health_integration_disk_pressure_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    ops_dir = Path(__file__).resolve().parents[2] / "scripts" / "ops"
    if str(ops_dir) not in sys.path:
        sys.path.insert(0, str(ops_dir))

    from state_paths import EnvironmentStatePaths

    import health_report as hr

    _build_config_tree(tmp_path / "config")
    resolved = _resolved(tmp_path)
    state = EnvironmentStatePaths.from_resolved_config(resolved)
    data_root = tmp_path / "data" / "dev"
    data_root.mkdir(parents=True)

    def disk_usage_fn(_path: Path) -> SimpleNamespace:
        return _usage(1000, 870)

    monkeypatch.setattr(
        "storage.disk_pressure.shutil.disk_usage",
        disk_usage_fn,
    )

    check = hr._disk_pressure(resolved, state)
    assert check.result == "WARN"
    assert "storage_state=WARNING" in (check.detail or "")
    assert "87.0%" in (check.detail or "")


def test_run_pipeline_blocks_production_on_disk_pressure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    ops_dir = Path(__file__).resolve().parents[2] / "scripts" / "ops"
    if str(ops_dir) not in sys.path:
        sys.path.insert(0, str(ops_dir))

    import execution_lock as el
    import run_pipeline as rp
    import run_records as rr

    monkeypatch.setattr(el, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(rp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)

    _build_config_tree(tmp_path / "config")
    (tmp_path / "data" / "prod").mkdir(parents=True)
    resolved = _resolved(tmp_path, "prod")

    monkeypatch.setattr(
        rp,
        "validate_config",
        lambda _env: (rp.EXIT_SUCCESS, "config ok", resolved),
    )
    monkeypatch.setattr(rp, "write_config_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(
        rp,
        "check_boot_readiness",
        lambda _env: (rp.EXIT_SUCCESS, "boot readiness READY"),
    )

    def disk_usage_fn(_path: Path) -> SimpleNamespace:
        return _usage(1000, 990)

    monkeypatch.setattr(
        "storage.disk_pressure.shutil.disk_usage",
        disk_usage_fn,
    )

    code = rp.run_pipeline("prod", funnel_id="business", trigger="scheduled")
    assert code == rp.EXIT_SUCCESS
    run_dirs = list((tmp_path / "runs" / "prod").iterdir())
    assert len(run_dirs) == 1
    record = json.loads((run_dirs[0] / "run_record.json").read_text(encoding="utf-8"))
    assert record["status"] == "SKIPPED"
    assert "reject threshold" in (record["failure_reason"] or "").lower()
    block_file = tmp_path / "data" / "prod" / "storage" / "disk_pressure_blocks.jsonl"
    assert block_file.is_file()
