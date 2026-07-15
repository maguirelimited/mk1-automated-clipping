"""Observability contract helpers.

Adapters map existing infrastructure shapes (run records, lock inspections,
upload/scheduler operational state) onto contract models without reading the
filesystem or depending on Flask / UI code.
"""

from __future__ import annotations

from typing import Any, Mapping

from .models import (
    ConfigSummary,
    FailureSummary,
    RunSummary,
    SchedulerStateSummary,
    UploadStateSummary,
)
from .schemas import (
    CONFIG_SUMMARY_ALLOWED_FIELDS,
    CONTRACT_SCHEMA_VERSION,
    SECRET_FIELD_NAME_TOKENS,
)

__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "assert_config_summary_safe",
    "config_summary_from_operational_state",
    "is_secret_field_name",
    "run_summary_from_run_record_dict",
]


def is_secret_field_name(name: str) -> bool:
    """Return True if a field name looks like a secret or credential."""
    normalized = str(name or "").strip().lower().replace("-", "_")
    if not normalized:
        return False
    if normalized in SECRET_FIELD_NAME_TOKENS:
        return True
    for token in SECRET_FIELD_NAME_TOKENS:
        if token in normalized:
            return True
    return False


def assert_config_summary_safe(payload: Mapping[str, Any]) -> None:
    """Raise ValueError if a ConfigSummary payload contains secrets or unknown fields.

    Callers serializing ConfigSummary for SSH, JSON endpoints, or the UI should
    run this (or rely on ConfigSummary.to_dict, which only emits allowlisted keys).
    """
    if not isinstance(payload, Mapping):
        raise ValueError("ConfigSummary payload must be a mapping")

    unknown = set(payload.keys()) - CONFIG_SUMMARY_ALLOWED_FIELDS
    if unknown:
        raise ValueError(
            f"ConfigSummary contains non-allowlisted fields: {sorted(unknown)}"
        )

    _assert_no_secret_keys(payload, path="ConfigSummary")


def _assert_no_secret_keys(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_str = str(key)
            if is_secret_field_name(key_str):
                raise ValueError(f"Secret-like field not permitted at {path}.{key_str}")
            _assert_no_secret_keys(child, path=f"{path}.{key_str}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_secret_keys(child, path=f"{path}[{index}]")


def config_summary_from_operational_state(
    *,
    environment: str,
    active_preset: str | None = None,
    funnel: str | None = None,
    platform: str | None = None,
    upload: UploadStateSummary | Mapping[str, Any] | None = None,
    scheduler: SchedulerStateSummary | Mapping[str, Any] | None = None,
) -> ConfigSummary:
    """Build a secret-safe ConfigSummary from operational (non-secret) state only.

    Rejects secret-like keys if upload/scheduler are supplied as mappings.
    Does not accept or forward raw environment variables.
    """
    upload_summary = _coerce_upload(upload)
    scheduler_summary = _coerce_scheduler(scheduler)
    summary = ConfigSummary(
        environment=str(environment or ""),
        active_preset=_clean_optional(active_preset),
        funnel=_clean_optional(funnel),
        platform=_clean_optional(platform),
        upload=upload_summary,
        scheduler=scheduler_summary,
        schema_version=CONTRACT_SCHEMA_VERSION,
    )
    assert_config_summary_safe(summary.to_dict())
    return summary


def run_summary_from_run_record_dict(record: Mapping[str, Any]) -> RunSummary:
    """Map a run_record.json-shaped dict onto RunSummary.

    Accepts the canonical Reliability & Recovery run record fields without
    importing scripts/ops/run_records.py (keeps the contract package independent
    of ops script path layout).
    """
    if not isinstance(record, Mapping):
        raise TypeError("run record must be a mapping")

    failure_reason = record.get("failure_reason")
    failure_summary: FailureSummary | None = None
    if failure_reason:
        status = str(record.get("status") or "")
        severity = "warn" if status == "SKIPPED" else "fail"
        failure_summary = FailureSummary(
            component="pipeline_run",
            reason=str(failure_reason),
            severity=severity,
            stage=None,
            timestamp=_optional_str(record.get("finished_at") or record.get("started_at")),
            suggested_next_inspection_target=_optional_str(record.get("log_path")),
        )

    reports = record.get("report_paths") or []
    if not isinstance(reports, list):
        reports = []

    duration = record.get("duration_seconds")
    try:
        duration_f = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_f = None

    return RunSummary(
        run_id=str(record.get("run_id") or ""),
        environment=str(record.get("environment") or ""),
        trigger=str(record.get("trigger") or ""),
        status=str(record.get("status") or "UNKNOWN"),
        started_at=_optional_str(record.get("started_at")),
        finished_at=_optional_str(record.get("finished_at")),
        duration_seconds=duration_f,
        jobs_started=_int_or_zero(record.get("jobs_started")),
        jobs_completed=_int_or_zero(record.get("jobs_completed")),
        jobs_failed=_int_or_zero(record.get("jobs_failed")),
        funnel_id=_optional_str(record.get("funnel_id")),
        failure_summary=failure_summary,
        log_path=_optional_str(record.get("log_path")),
        report_paths=[str(p) for p in reports],
        schema_version=CONTRACT_SCHEMA_VERSION,
    )


def _coerce_upload(
    value: UploadStateSummary | Mapping[str, Any] | None,
) -> UploadStateSummary:
    if value is None:
        return UploadStateSummary()
    if isinstance(value, UploadStateSummary):
        return value
    if isinstance(value, Mapping):
        _assert_no_secret_keys(value, path="upload")
        return UploadStateSummary.from_dict(dict(value))
    raise TypeError("upload must be UploadStateSummary or mapping")


def _coerce_scheduler(
    value: SchedulerStateSummary | Mapping[str, Any] | None,
) -> SchedulerStateSummary:
    if value is None:
        return SchedulerStateSummary()
    if isinstance(value, SchedulerStateSummary):
        return value
    if isinstance(value, Mapping):
        _assert_no_secret_keys(value, path="scheduler")
        return SchedulerStateSummary.from_dict(dict(value))
    raise TypeError("scheduler must be SchedulerStateSummary or mapping")


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_str(value: Any) -> str | None:
    return _clean_optional(value)


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
