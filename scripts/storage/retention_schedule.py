"""Scheduled retention runner (Storage & Data Management Phase 8).

Decides *when* retention runs. Reuses the existing dry-run planner and safe
apply executor — does not implement retention policy or deletion logic.

Does **not** trigger retention from disk pressure.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

_SCRIPTS_CONFIG = Path(__file__).resolve().parents[1] / "config"
if str(_SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_CONFIG))

from config_manager import ResolvedConfig  # noqa: E402
from state_paths import EnvironmentStatePaths  # noqa: E402

from .retention_apply import run_retention_apply
from .retention_planner import RetentionPlanner, run_retention_dry_run

SCHEDULE_RECORD_SCHEMA_VERSION = 1
LATEST_RECORD_NAME = "scheduled_retention_latest.json"
HISTORY_RECORD_NAME = "scheduled_retention_history.jsonl"

SCHEDULE_MODES = frozenset({"disabled", "dry_run", "apply"})
SCHEDULE_FREQUENCIES = frozenset({"daily", "weekly"})

STATUS_SUCCESS = "SUCCESS"
STATUS_SKIPPED = "SKIPPED"
STATUS_FAIL = "FAIL"

EXIT_SUCCESS = 0
EXIT_FAIL = 1
EXIT_CONFIG = 3


@dataclass(frozen=True)
class RetentionScheduleConfig:
    enabled: bool
    mode: str
    frequency: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScheduledRetentionResult:
    status: str
    mode: str
    environment: str
    schedule_enabled: bool
    retention_enabled: bool
    duration_seconds: float
    report_path: str | None = None
    reason: str | None = None
    detail: str | None = None
    exit_code: int = EXIT_SUCCESS
    timestamp: str = ""
    trigger: str = "scheduled"
    schema_version: int = SCHEDULE_RECORD_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env_token(resolved: ResolvedConfig) -> str:
    return "prod" if resolved.environment == "production" else "dev"


def load_retention_schedule_config(resolved: ResolvedConfig) -> RetentionScheduleConfig:
    enabled = resolved.get("storage.schedule.enabled")
    mode = resolved.get("storage.schedule.mode")
    frequency = resolved.get("storage.schedule.frequency")
    if not isinstance(enabled, bool):
        raise ValueError("storage.schedule.enabled must be a boolean")
    if not isinstance(mode, str) or mode not in SCHEDULE_MODES:
        raise ValueError(
            f"storage.schedule.mode must be one of {sorted(SCHEDULE_MODES)}, got {mode!r}"
        )
    if not isinstance(frequency, str) or frequency not in SCHEDULE_FREQUENCIES:
        raise ValueError(
            f"storage.schedule.frequency must be one of {sorted(SCHEDULE_FREQUENCIES)}, "
            f"got {frequency!r}"
        )
    return RetentionScheduleConfig(enabled=enabled, mode=mode, frequency=frequency)


def schedule_records_dir(
    resolved: ResolvedConfig,
    *,
    data_root: Path | None = None,
) -> Path:
    if data_root is not None:
        return data_root / "storage"
    state = EnvironmentStatePaths.from_resolved_config(resolved)
    return state.data_root / "storage"


def write_scheduled_retention_record(
    result: ScheduledRetentionResult,
    *,
    records_dir: Path,
) -> Path:
    """Write latest pointer and append history. Failures are never silent."""
    records_dir.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    latest_path = records_dir / LATEST_RECORD_NAME
    latest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    history_path = records_dir / HISTORY_RECORD_NAME
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")
    return latest_path


def load_latest_scheduled_retention(
    *,
    records_dir: Path,
) -> dict[str, Any] | None:
    path = records_dir / LATEST_RECORD_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _result(
    *,
    status: str,
    mode: str,
    environment: str,
    schedule: RetentionScheduleConfig,
    retention_enabled: bool,
    duration_seconds: float,
    exit_code: int,
    report_path: str | None = None,
    reason: str | None = None,
    detail: str | None = None,
    timestamp: str | None = None,
) -> ScheduledRetentionResult:
    return ScheduledRetentionResult(
        status=status,
        mode=mode,
        environment=environment,
        schedule_enabled=schedule.enabled,
        retention_enabled=retention_enabled,
        duration_seconds=round(duration_seconds, 3),
        report_path=report_path,
        reason=reason,
        detail=detail,
        exit_code=exit_code,
        timestamp=timestamp or _utc_now_iso(),
    )


def run_scheduled_retention(
    resolved: ResolvedConfig,
    *,
    now: datetime | None = None,
    report_dir: Path | None = None,
    records_dir: Path | None = None,
    dry_run_fn: Callable[..., tuple[Any, Path]] | None = None,
    apply_fn: Callable[..., tuple[Any, Path]] | None = None,
    plan_fn: Callable[..., Any] | None = None,
) -> ScheduledRetentionResult:
    """Execute scheduled retention according to config.

    Reuses ``run_retention_dry_run`` / ``run_retention_apply`` (or injectable
    callables for tests). Never deletes files itself.
    """
    started = time.monotonic()
    environment = _env_token(resolved)
    retention_enabled = bool(resolved.get("storage.retention.enabled"))
    records = records_dir or schedule_records_dir(resolved)

    try:
        schedule = load_retention_schedule_config(resolved)
    except ValueError as exc:
        result = _result(
            status=STATUS_FAIL,
            mode="unknown",
            environment=environment,
            schedule=RetentionScheduleConfig(enabled=False, mode="disabled", frequency="daily"),
            retention_enabled=retention_enabled,
            duration_seconds=time.monotonic() - started,
            exit_code=EXIT_CONFIG,
            reason=str(exc),
            detail=str(exc),
        )
        write_scheduled_retention_record(result, records_dir=records)
        return result

    if not schedule.enabled or schedule.mode == "disabled":
        reason = (
            "scheduled retention disabled by config"
            if not schedule.enabled
            else "scheduled retention mode is disabled"
        )
        result = _result(
            status=STATUS_SKIPPED,
            mode=schedule.mode,
            environment=environment,
            schedule=schedule,
            retention_enabled=retention_enabled,
            duration_seconds=time.monotonic() - started,
            exit_code=EXIT_SUCCESS,
            reason=reason,
            detail=reason,
        )
        write_scheduled_retention_record(result, records_dir=records)
        return result

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    try:
        if schedule.mode == "dry_run":
            runner = dry_run_fn or run_retention_dry_run
            report, path = runner(resolved, now=moment, report_dir=report_dir)
            result = _result(
                status=STATUS_SUCCESS,
                mode=schedule.mode,
                environment=environment,
                schedule=schedule,
                retention_enabled=retention_enabled,
                duration_seconds=time.monotonic() - started,
                exit_code=EXIT_SUCCESS,
                report_path=str(path),
                detail=f"scheduled dry-run completed: {path}",
            )
            write_scheduled_retention_record(result, records_dir=records)
            return result

        # mode == apply
        if not retention_enabled:
            reason = (
                "scheduled apply refused: storage.retention.enabled is false"
            )
            result = _result(
                status=STATUS_FAIL,
                mode=schedule.mode,
                environment=environment,
                schedule=schedule,
                retention_enabled=retention_enabled,
                duration_seconds=time.monotonic() - started,
                exit_code=EXIT_FAIL,
                reason=reason,
                detail=reason,
            )
            write_scheduled_retention_record(result, records_dir=records)
            return result

        planner = plan_fn or (lambda cfg, **kwargs: RetentionPlanner(cfg, **kwargs).plan_dry_run())
        plan = planner(resolved, now=moment)
        apply_runner = apply_fn or run_retention_apply
        apply_report, path = apply_runner(
            resolved,
            plan,
            now=moment,
            report_dir=report_dir,
        )
        result = _result(
            status=STATUS_SUCCESS,
            mode=schedule.mode,
            environment=environment,
            schedule=schedule,
            retention_enabled=retention_enabled,
            duration_seconds=time.monotonic() - started,
            exit_code=EXIT_SUCCESS,
            report_path=str(path),
            detail=f"scheduled apply completed: {path}",
        )
        # Attach apply summary counts when available.
        if hasattr(apply_report, "files_deleted"):
            result.detail = (
                f"scheduled apply completed: deleted={apply_report.files_deleted} "
                f"report={path}"
            )
        write_scheduled_retention_record(result, records_dir=records)
        return result
    except Exception as exc:  # noqa: BLE001 — record any scheduled failure
        reason = f"scheduled retention {schedule.mode} failed: {exc}"
        result = _result(
            status=STATUS_FAIL,
            mode=schedule.mode,
            environment=environment,
            schedule=schedule,
            retention_enabled=retention_enabled,
            duration_seconds=time.monotonic() - started,
            exit_code=EXIT_FAIL,
            reason=reason,
            detail=reason,
        )
        write_scheduled_retention_record(result, records_dir=records)
        return result
