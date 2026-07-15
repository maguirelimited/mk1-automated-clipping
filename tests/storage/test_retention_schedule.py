"""Tests for Storage Phase 8 — scheduled retention."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from config_manager import ConfigManager
from storage.retention_schedule import (
    STATUS_FAIL,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    load_latest_scheduled_retention,
    load_retention_schedule_config,
    run_scheduled_retention,
)
from test_retention_planner import _build_config_tree


def _resolved(repo: Path, environment: str = "dev"):
    return ConfigManager.load(
        environment=environment,
        funnel_id="business",
        platform_id="youtube",
        config_root=repo / "config",
    )


def _set_schedule(
    repo: Path,
    *,
    environment: str,
    enabled: bool,
    mode: str,
    frequency: str = "daily",
    retention_enabled: bool | None = None,
) -> None:
    import re

    token = "dev" if environment in {"dev", "development"} else "prod"
    path = repo / "config" / "environments" / f"{token}.yaml"
    text = path.read_text(encoding="utf-8")
    schedule_block = (
        f"  schedule:\n"
        f"    enabled: {str(enabled).lower()}\n"
        f"    mode: {mode}\n"
        f"    frequency: {frequency}\n"
    )
    if re.search(r"^  schedule:\n", text, flags=re.MULTILINE):
        text = re.sub(
            r"^  schedule:\n(?:    .+\n)+",
            schedule_block,
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        text = text.rstrip() + "\n" + schedule_block
    if retention_enabled is not None:
        text = re.sub(
            r"(retention:\n\s+enabled:\s*)(true|false)",
            rf"\g<1>{str(retention_enabled).lower()}",
            text,
            count=1,
        )
    path.write_text(text, encoding="utf-8")


def test_production_defaults_to_scheduled_dry_run() -> None:
    """Live production config defaults to enabled dry_run."""
    repo = Path(__file__).resolve().parents[2]
    resolved = ConfigManager.load(
        environment="production",
        funnel_id="business",
        platform_id="youtube",
        config_root=repo / "config",
    )
    schedule = load_retention_schedule_config(resolved)
    assert schedule.enabled is True
    assert schedule.mode == "dry_run"
    assert schedule.frequency == "daily"


def test_disabled_scheduling_records_skip(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    _set_schedule(tmp_path, environment="dev", enabled=False, mode="disabled")
    resolved = _resolved(tmp_path)
    records_dir = tmp_path / "data" / "dev" / "storage"

    called = {"dry_run": 0, "apply": 0}

    def dry_run_fn(*_a, **_k):
        called["dry_run"] += 1
        raise AssertionError("dry-run must not run when disabled")

    def apply_fn(*_a, **_k):
        called["apply"] += 1
        raise AssertionError("apply must not run when disabled")

    result = run_scheduled_retention(
        resolved,
        records_dir=records_dir,
        dry_run_fn=dry_run_fn,
        apply_fn=apply_fn,
    )
    assert result.status == STATUS_SKIPPED
    assert result.exit_code == 0
    assert "disabled" in (result.reason or "").lower()
    assert called == {"dry_run": 0, "apply": 0}
    latest = load_latest_scheduled_retention(records_dir=records_dir)
    assert latest is not None
    assert latest["status"] == STATUS_SKIPPED


def test_scheduled_dry_run_reuses_planner(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    _set_schedule(tmp_path, environment="prod", enabled=True, mode="dry_run")
    resolved = _resolved(tmp_path, "prod")
    records_dir = tmp_path / "data" / "prod" / "storage"
    report_path = tmp_path / "reports" / "prod" / "retention" / "plan.json"
    report_path.parent.mkdir(parents=True)

    def dry_run_fn(cfg, **_kwargs):
        assert cfg is resolved
        report_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(mode="dry-run"), report_path

    result = run_scheduled_retention(
        resolved,
        records_dir=records_dir,
        dry_run_fn=dry_run_fn,
        apply_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no apply")),
    )
    assert result.status == STATUS_SUCCESS
    assert result.mode == "dry_run"
    assert result.report_path == str(report_path)
    latest = load_latest_scheduled_retention(records_dir=records_dir)
    assert latest["report_path"] == str(report_path)
    assert latest["mode"] == "dry_run"


def test_scheduled_apply_reuses_apply_executor(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config", retention_enabled=True)
    _set_schedule(
        tmp_path,
        environment="prod",
        enabled=True,
        mode="apply",
        retention_enabled=True,
    )
    resolved = _resolved(tmp_path, "prod")
    records_dir = tmp_path / "data" / "prod" / "storage"
    apply_path = tmp_path / "reports" / "prod" / "retention" / "apply.json"
    apply_path.parent.mkdir(parents=True)
    plan = SimpleNamespace(mode="dry-run", environment="production")

    def plan_fn(cfg, **_kwargs):
        assert cfg is resolved
        return plan

    def apply_fn(cfg, received_plan, **_kwargs):
        assert cfg is resolved
        assert received_plan is plan
        apply_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(files_deleted=2), apply_path

    result = run_scheduled_retention(
        resolved,
        records_dir=records_dir,
        plan_fn=plan_fn,
        apply_fn=apply_fn,
        dry_run_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no dry_run")),
    )
    assert result.status == STATUS_SUCCESS
    assert result.mode == "apply"
    assert result.report_path == str(apply_path)
    assert "deleted=2" in (result.detail or "")


def test_scheduled_apply_requires_retention_enabled(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config", retention_enabled=False)
    _set_schedule(
        tmp_path,
        environment="prod",
        enabled=True,
        mode="apply",
        retention_enabled=False,
    )
    resolved = _resolved(tmp_path, "prod")
    records_dir = tmp_path / "data" / "prod" / "storage"

    result = run_scheduled_retention(
        resolved,
        records_dir=records_dir,
        apply_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no apply")),
    )
    assert result.status == STATUS_FAIL
    assert "retention.enabled is false" in (result.reason or "")
    latest = load_latest_scheduled_retention(records_dir=records_dir)
    assert latest["status"] == STATUS_FAIL


def test_planner_failure_is_recorded(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    _set_schedule(tmp_path, environment="prod", enabled=True, mode="dry_run")
    resolved = _resolved(tmp_path, "prod")
    records_dir = tmp_path / "data" / "prod" / "storage"

    def dry_run_fn(*_a, **_k):
        raise RuntimeError("planner boom")

    result = run_scheduled_retention(
        resolved,
        records_dir=records_dir,
        dry_run_fn=dry_run_fn,
    )
    assert result.status == STATUS_FAIL
    assert "planner boom" in (result.reason or "")
    history = (records_dir / "scheduled_retention_history.jsonl").read_text(encoding="utf-8")
    assert "planner boom" in history


def test_invalid_schedule_mode_fails_config_validation(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    path = tmp_path / "config" / "environments" / "prod.yaml"
    text = path.read_text(encoding="utf-8")
    text = text.replace("mode: disabled", "mode: auto_delete")
    text = text.replace("mode: dry_run", "mode: auto_delete")
    # Ensure schedule mode is invalid.
    if "mode: auto_delete" not in text:
        text = text.replace(
            "  schedule:\n    enabled: false\n    mode: disabled\n    frequency: daily\n",
            "  schedule:\n    enabled: true\n    mode: auto_delete\n    frequency: daily\n",
        )
    else:
        text = text.replace(
            "enabled: false\n    mode: auto_delete",
            "enabled: true\n    mode: auto_delete",
        )
    path.write_text(text, encoding="utf-8")

    from config_manager import ConfigError

    with pytest.raises(ConfigError) as exc_info:
        _resolved(tmp_path, "prod")
    assert "storage.schedule.mode" in str(exc_info.value)


def test_execution_record_fields(tmp_path: Path) -> None:
    _build_config_tree(tmp_path / "config")
    _set_schedule(tmp_path, environment="prod", enabled=True, mode="dry_run")
    resolved = _resolved(tmp_path, "prod")
    records_dir = tmp_path / "data" / "prod" / "storage"
    report_path = tmp_path / "report.json"

    def dry_run_fn(*_a, **_k):
        report_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(), report_path

    result = run_scheduled_retention(
        resolved,
        records_dir=records_dir,
        dry_run_fn=dry_run_fn,
    )
    payload = result.to_dict()
    for key in (
        "schema_version",
        "timestamp",
        "environment",
        "mode",
        "status",
        "duration_seconds",
        "report_path",
        "trigger",
    ):
        assert key in payload
    assert payload["trigger"] == "scheduled"
    assert payload["environment"] == "prod"


def test_real_dry_run_writes_retention_report(tmp_path: Path) -> None:
    """End-to-end: scheduled dry-run calls real planner and writes report."""
    _build_config_tree(tmp_path / "config")
    _set_schedule(tmp_path, environment="prod", enabled=True, mode="dry_run")
    (tmp_path / "data" / "prod").mkdir(parents=True)
    (tmp_path / "jobs" / "prod").mkdir(parents=True)
    resolved = _resolved(tmp_path, "prod")
    records_dir = tmp_path / "data" / "prod" / "storage"
    report_dir = tmp_path / "reports" / "prod" / "retention"

    result = run_scheduled_retention(
        resolved,
        records_dir=records_dir,
        report_dir=report_dir,
    )
    assert result.status == STATUS_SUCCESS
    assert result.report_path is not None
    assert Path(result.report_path).is_file()
    report = json.loads(Path(result.report_path).read_text(encoding="utf-8"))
    assert report.get("mode") == "dry-run" or "schema_version" in report
