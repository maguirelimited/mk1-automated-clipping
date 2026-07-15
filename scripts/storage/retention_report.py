"""Retention reports — stable operational interface (Phase 6).

Dry-run and apply reports share a versioned schema. Reports are operational
evidence: machine-readable, historical, and loadable without re-running retention.

Does not change planner or apply deletion behaviour.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

# Stable schema — extend, do not silently change.
RETENTION_REPORT_SCHEMA_VERSION = 1
PLANNER_VERSION = "retention_planner.v1"
APPLY_VERSION = "retention_apply.v1"

LATEST_POINTER_NAME = "latest.json"

Disposition = Literal["eligible", "protected", "unknown"]
Outcome = Literal["DELETED", "SKIPPED", "FAILED"]


# ---------------------------------------------------------------------------
# Per-file records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionFileDecision:
    """One artifact evaluated by the retention planner."""

    path: str
    artifact_type: str
    disposition: Disposition
    reason: str
    size_bytes: int | None = None
    job_id: str | None = None
    run_id: str | None = None
    age_seconds: float | None = None
    retention_days: int | None = None
    current_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "artifact_type": self.artifact_type,
            "disposition": self.disposition,
            "reason": self.reason,
            "size_bytes": self.size_bytes,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "age_seconds": self.age_seconds,
            "retention_days": self.retention_days,
            "current_state": self.current_state,
            "planner_reason": self.reason,
            "outcome": self.disposition,
        }


@dataclass
class DeletionRecord:
    """Structured log entry for one apply-mode deletion attempt."""

    timestamp: str
    environment: str
    artifact_type: str
    original_path: str
    resolved_path: str | None
    size_bytes: int | None
    planner_reason: str
    outcome: Outcome
    skip_reason: str | None = None
    error: str | None = None
    age_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "environment": self.environment,
            "artifact_type": self.artifact_type,
            "path": self.original_path,
            "original_path": self.original_path,
            "resolved_path": self.resolved_path,
            "size_bytes": self.size_bytes,
            "age_seconds": self.age_seconds,
            "planner_reason": self.planner_reason,
            "outcome": self.outcome,
            "skip_reason": self.skip_reason,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------


def _sum_bytes(items: list[Any], attr: str = "size_bytes") -> int:
    total = 0
    for item in items:
        value = getattr(item, attr, None)
        if value is None and isinstance(item, dict):
            value = item.get(attr)
        if isinstance(value, (int, float)):
            total += max(0, int(value))
    return total


def _duration_seconds(started_at: str, finished_at: str) -> float:
    start = _parse_iso(started_at)
    end = _parse_iso(finished_at)
    if start is None or end is None:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def _parse_iso(raw: str) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def protection_summary_from_decisions(
    protected: list[RetentionFileDecision],
    unknown: list[RetentionFileDecision] | None = None,
) -> dict[str, int]:
    """Grouped protection counts for operational summaries."""
    active_jobs: set[str] = set()
    failed_jobs: set[str] = set()
    final_clips = 0
    databases = 0
    for item in protected:
        if item.reason == "active_job" or item.current_state in {"running", "queued"}:
            if item.job_id:
                active_jobs.add(item.job_id)
            else:
                active_jobs.add(item.path)
        if item.reason == "failed_job" or item.current_state == "failed":
            if item.job_id:
                failed_jobs.add(item.job_id)
            else:
                failed_jobs.add(item.path)
        if item.artifact_type == "final_clip" or item.reason in {
            "final_clip_default_protected",
            "final_clip",
        }:
            final_clips += 1
        if item.artifact_type == "database" or item.reason in {
            "database",
            "protected_database",
        }:
            databases += 1

    unknown_count = len(unknown or [])
    return {
        "protected_active_jobs": len(active_jobs),
        "protected_failed_jobs": len(failed_jobs),
        "protected_final_clips": final_clips,
        "protected_databases": databases,
        "protected_unknown": unknown_count,
    }


def skip_summary_from_reasons(reasons: dict[str, int]) -> dict[str, int]:
    return dict(sorted(reasons.items()))


def error_summary_from_messages(errors: list[str]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for err in errors:
        key = _group_error(err)
        summary[key] = summary.get(key, 0) + 1
    return dict(sorted(summary.items()))


def error_summary_from_deletions(deletions: list[DeletionRecord]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in deletions:
        if item.outcome != "FAILED":
            continue
        key = _group_error(item.error or item.skip_reason or "filesystem_error")
        summary[key] = summary.get(key, 0) + 1
    return dict(sorted(summary.items()))


def _group_error(message: str) -> str:
    text = (message or "").lower()
    if "permission" in text:
        return "permission_denied"
    if "not found" in text or "no such file" in text:
        return "file_not_found"
    if "filesystem" in text:
        return "filesystem_error"
    if "read-only" in text or "readonly" in text:
        return "read_only_filesystem"
    if message:
        # Keep short stable keys for known skip reasons used as errors.
        token = message.strip().split(":")[0].strip()
        return token[:80] if token else "error"
    return "error"


# ---------------------------------------------------------------------------
# Dry-run report
# ---------------------------------------------------------------------------


@dataclass
class RetentionPlanReport:
    """Structured retention dry-run report."""

    retention_run_id: str
    environment: str
    mode: str
    policy_version: str
    retention_enabled: bool
    started_at: str
    finished_at: str
    files_considered: int = 0
    eligible_files: list[RetentionFileDecision] = field(default_factory=list)
    protected_files: list[RetentionFileDecision] = field(default_factory=list)
    unknown_files: list[RetentionFileDecision] = field(default_factory=list)
    bytes_reclaimable: int = 0
    deletion_reasons: dict[str, int] = field(default_factory=dict)
    protection_reasons: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    planner_version: str = PLANNER_VERSION
    duration_seconds: float = 0.0
    bytes_considered: int = 0

    @property
    def eligible_count(self) -> int:
        return len(self.eligible_files)

    @property
    def protected_count(self) -> int:
        return len(self.protected_files)

    @property
    def unknown_count(self) -> int:
        return len(self.unknown_files)

    def finalize_summaries(self) -> None:
        """Compute derived summary fields from per-file records."""
        if self.started_at and self.finished_at:
            self.duration_seconds = _duration_seconds(self.started_at, self.finished_at)
        all_files = (
            self.eligible_files + self.protected_files + self.unknown_files
        )
        self.bytes_considered = _sum_bytes(all_files)

    def to_dict(self) -> dict[str, Any]:
        self.finalize_summaries()
        protection = protection_summary_from_decisions(
            self.protected_files, self.unknown_files
        )
        skip_summary = skip_summary_from_reasons(self.protection_reasons)
        return {
            "schema_version": RETENTION_REPORT_SCHEMA_VERSION,
            "retention_run_id": self.retention_run_id,
            "environment": self.environment,
            "mode": self.mode,
            "planner_version": self.planner_version,
            "policy_version": self.policy_version,
            "retention_enabled": self.retention_enabled,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            # Summary (stable names for consumers)
            "files_considered": self.files_considered,
            "files_eligible": self.eligible_count,
            "files_deleted": 0,
            "files_protected": self.protected_count,
            "files_unknown": self.unknown_count,
            "files_skipped": 0,
            "files_failed": 0,
            # Backward-compatible aliases
            "eligible_count": self.eligible_count,
            "protected_count": self.protected_count,
            "unknown_count": self.unknown_count,
            # Space
            "bytes_considered": self.bytes_considered,
            "bytes_reclaimable": self.bytes_reclaimable,
            "bytes_reclaimed": 0,
            # Grouped summaries
            "protection_summary": protection,
            "skip_summary": skip_summary,
            "error_summary": error_summary_from_messages(self.errors),
            "deletion_reasons": dict(self.deletion_reasons),
            "protection_reasons": dict(self.protection_reasons),
            # Per-file records
            "eligible_files": [f.to_dict() for f in self.eligible_files],
            "protected_files": [f.to_dict() for f in self.protected_files],
            "unknown_files": [f.to_dict() for f in self.unknown_files],
            "errors": list(self.errors),
        }

    def write_json(self, path: Path) -> Path:
        return write_retention_report(self.to_dict(), path)


# ---------------------------------------------------------------------------
# Apply report
# ---------------------------------------------------------------------------


@dataclass
class RetentionApplyReport:
    """Structured retention apply report (separate from dry-run report)."""

    retention_run_id: str
    source_plan_id: str
    environment: str
    mode: str
    policy_version: str
    started_at: str
    finished_at: str
    planned_deletions: int = 0
    successful_deletions: int = 0
    skipped_deletions: int = 0
    failed_deletions: int = 0
    bytes_reclaimed: int = 0
    skipped_bytes: int = 0
    execution_duration_seconds: float = 0.0
    deletions: list[DeletionRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    planner_version: str = PLANNER_VERSION
    apply_version: str = APPLY_VERSION
    bytes_reclaimable: int = 0
    bytes_considered: int = 0
    protection_summary: dict[str, int] = field(default_factory=dict)
    files_protected: int = 0
    files_unknown: int = 0
    files_considered: int = 0

    @property
    def duration_seconds(self) -> float:
        return self.execution_duration_seconds

    def finalize_summaries(self) -> None:
        if self.started_at and self.finished_at and not self.execution_duration_seconds:
            self.execution_duration_seconds = _duration_seconds(
                self.started_at, self.finished_at
            )
        if not self.bytes_considered:
            self.bytes_considered = _sum_bytes(self.deletions)
        if not self.files_considered:
            self.files_considered = self.planned_deletions

    def to_dict(self) -> dict[str, Any]:
        self.finalize_summaries()
        skip_counts: dict[str, int] = {}
        for item in self.deletions:
            if item.outcome == "SKIPPED" and item.skip_reason:
                skip_counts[item.skip_reason] = skip_counts.get(item.skip_reason, 0) + 1
        return {
            "schema_version": RETENTION_REPORT_SCHEMA_VERSION,
            "retention_run_id": self.retention_run_id,
            "source_plan_id": self.source_plan_id,
            "environment": self.environment,
            "mode": self.mode,
            "planner_version": self.planner_version,
            "apply_version": self.apply_version,
            "policy_version": self.policy_version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.execution_duration_seconds,
            "execution_duration_seconds": self.execution_duration_seconds,
            # Summary
            "files_considered": self.files_considered,
            "files_eligible": self.planned_deletions,
            "files_deleted": self.successful_deletions,
            "files_protected": self.files_protected,
            "files_unknown": self.files_unknown,
            "files_skipped": self.skipped_deletions,
            "files_failed": self.failed_deletions,
            # Backward-compatible aliases
            "planned_deletions": self.planned_deletions,
            "successful_deletions": self.successful_deletions,
            "skipped_deletions": self.skipped_deletions,
            "failed_deletions": self.failed_deletions,
            # Space
            "bytes_considered": self.bytes_considered,
            "bytes_reclaimable": self.bytes_reclaimable,
            "bytes_reclaimed": self.bytes_reclaimed,
            "skipped_bytes": self.skipped_bytes,
            # Grouped summaries
            "protection_summary": dict(self.protection_summary),
            "skip_summary": skip_summary_from_reasons(skip_counts),
            "error_summary": error_summary_from_deletions(self.deletions)
            or error_summary_from_messages(self.errors),
            # Per-file records
            "deletions": [d.to_dict() for d in self.deletions],
            "errors": list(self.errors),
        }

    def write_json(self, path: Path) -> Path:
        return write_retention_report(self.to_dict(), path)


# ---------------------------------------------------------------------------
# Write / latest pointer / load API
# ---------------------------------------------------------------------------


def new_retention_run_id(*, now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    return f"retention_{stamp}"


def write_retention_report(payload: dict[str, Any], path: Path) -> Path:
    """Write a report JSON and update the latest pointer. Never overwrites history."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if "schema_version" not in payload:
        payload = {**payload, "schema_version": RETENTION_REPORT_SCHEMA_VERSION}
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_latest_pointer(path.parent, path, payload)
    return path


def _write_latest_pointer(report_dir: Path, report_path: Path, payload: dict[str, Any]) -> None:
    pointer = {
        "schema_version": RETENTION_REPORT_SCHEMA_VERSION,
        "retention_run_id": payload.get("retention_run_id"),
        "environment": payload.get("environment"),
        "mode": payload.get("mode"),
        "report_path": report_path.name,
        "written_at": payload.get("finished_at")
        or datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    (report_dir / LATEST_POINTER_NAME).write_text(
        json.dumps(pointer, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_retention_report(path: Path | str) -> dict[str, Any]:
    """Load any retention report (dry-run or apply) as a dict.

    Normalizes older Phase 4/5 reports to include schema_version and summary
    field aliases where practical.
    """
    report_path = Path(path)
    data = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid retention report: {report_path}")
    return _normalize_report_dict(data)


def load_latest_retention_report(
    report_dir: Path | str,
) -> dict[str, Any] | None:
    """Load the newest report via ``latest.json`` pointer.

    Returns None when no pointer or report file exists.
    """
    directory = Path(report_dir)
    pointer_path = directory / LATEST_POINTER_NAME
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    name = pointer.get("report_path")
    if not isinstance(name, str) or not name.strip():
        return None
    report_path = directory / name
    if not report_path.is_file():
        return None
    return load_retention_report(report_path)


def load_plan_report(path: Path) -> RetentionPlanReport:
    """Load a dry-run retention plan JSON report (typed, for apply executor)."""
    data = load_retention_report(path)
    if data.get("mode") != "dry-run":
        raise ValueError(f"not a dry-run plan report: {path} (mode={data.get('mode')!r})")
    return RetentionPlanReport(
        retention_run_id=str(data["retention_run_id"]),
        environment=str(data["environment"]),
        mode="dry-run",
        policy_version=str(data.get("policy_version", "")),
        retention_enabled=bool(data.get("retention_enabled")),
        started_at=str(data.get("started_at", "")),
        finished_at=str(data.get("finished_at", "")),
        files_considered=int(data.get("files_considered", 0)),
        eligible_files=[
            _decision_from_dict(item) for item in data.get("eligible_files", [])
        ],
        protected_files=[
            _decision_from_dict(item) for item in data.get("protected_files", [])
        ],
        unknown_files=[
            _decision_from_dict(item) for item in data.get("unknown_files", [])
        ],
        bytes_reclaimable=int(data.get("bytes_reclaimable", 0)),
        deletion_reasons=dict(data.get("deletion_reasons", {})),
        protection_reasons=dict(data.get("protection_reasons", {})),
        errors=list(data.get("errors", [])),
        planner_version=str(data.get("planner_version", PLANNER_VERSION)),
        duration_seconds=float(data.get("duration_seconds", 0.0) or 0.0),
        bytes_considered=int(data.get("bytes_considered", 0) or 0),
    )


def _normalize_report_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Fill stable summary fields for older reports."""
    out = dict(data)
    out.setdefault("schema_version", RETENTION_REPORT_SCHEMA_VERSION)
    out.setdefault("planner_version", PLANNER_VERSION)

    mode = out.get("mode")
    if mode == "dry-run":
        eligible = out.get("eligible_files") or []
        protected = out.get("protected_files") or []
        unknown = out.get("unknown_files") or []
        out.setdefault("files_eligible", out.get("eligible_count", len(eligible)))
        out.setdefault("files_deleted", 0)
        out.setdefault("files_protected", out.get("protected_count", len(protected)))
        out.setdefault("files_unknown", out.get("unknown_count", len(unknown)))
        out.setdefault("files_skipped", 0)
        out.setdefault("files_failed", 0)
        out.setdefault("bytes_reclaimed", 0)
        if "bytes_considered" not in out:
            out["bytes_considered"] = _sum_bytes_dicts(
                eligible + protected + unknown
            )
        if "duration_seconds" not in out:
            out["duration_seconds"] = _duration_seconds(
                str(out.get("started_at", "")),
                str(out.get("finished_at", "")),
            )
        if "protection_summary" not in out:
            out["protection_summary"] = protection_summary_from_decisions(
                [_decision_from_dict(i) for i in protected],
                [_decision_from_dict(i) for i in unknown],
            )
        if "skip_summary" not in out:
            out["skip_summary"] = skip_summary_from_reasons(
                dict(out.get("protection_reasons") or {})
            )
        if "error_summary" not in out:
            out["error_summary"] = error_summary_from_messages(
                list(out.get("errors") or [])
            )
    elif mode == "apply":
        out.setdefault("apply_version", APPLY_VERSION)
        out.setdefault("files_eligible", out.get("planned_deletions", 0))
        out.setdefault("files_deleted", out.get("successful_deletions", 0))
        out.setdefault("files_skipped", out.get("skipped_deletions", 0))
        out.setdefault("files_failed", out.get("failed_deletions", 0))
        out.setdefault("files_protected", 0)
        out.setdefault("files_unknown", 0)
        out.setdefault("files_considered", out.get("planned_deletions", 0))
        if "duration_seconds" not in out:
            out["duration_seconds"] = float(
                out.get("execution_duration_seconds", 0.0) or 0.0
            )
        deletions = out.get("deletions") or []
        if "bytes_considered" not in out:
            out["bytes_considered"] = _sum_bytes_dicts(deletions)
        if "skip_summary" not in out:
            counts: dict[str, int] = {}
            for item in deletions:
                if item.get("outcome") == "SKIPPED" and item.get("skip_reason"):
                    reason = str(item["skip_reason"])
                    counts[reason] = counts.get(reason, 0) + 1
            out["skip_summary"] = skip_summary_from_reasons(counts)
        if "error_summary" not in out:
            out["error_summary"] = error_summary_from_messages(
                list(out.get("errors") or [])
            )
        out.setdefault("protection_summary", {})

    return out


def _sum_bytes_dicts(items: list[dict[str, Any]]) -> int:
    total = 0
    for item in items:
        value = item.get("size_bytes")
        if isinstance(value, (int, float)):
            total += max(0, int(value))
    return total


def _decision_from_dict(data: dict[str, Any]) -> RetentionFileDecision:
    return RetentionFileDecision(
        path=str(data["path"]),
        artifact_type=str(data["artifact_type"]),
        disposition=data.get("disposition", "eligible"),  # type: ignore[arg-type]
        reason=str(data.get("reason") or data.get("planner_reason") or ""),
        size_bytes=data.get("size_bytes"),
        job_id=data.get("job_id"),
        run_id=data.get("run_id"),
        age_seconds=data.get("age_seconds"),
        retention_days=data.get("retention_days"),
        current_state=data.get("current_state"),
    )


# ---------------------------------------------------------------------------
# Terminal summaries
# ---------------------------------------------------------------------------


def format_bytes(num: int) -> str:
    if num < 0:
        num = 0
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(num)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{num} B"


def _type_label(artifact_type: str, count: int) -> str:
    labels = {
        "source_video": "source video" if count == 1 else "source videos",
        "temporary_file": "temporary file" if count == 1 else "temporary files",
        "intermediate_render": "intermediate render" if count == 1 else "intermediate renders",
        "job_log": "log" if count == 1 else "logs",
        "service_log": "log" if count == 1 else "logs",
        "final_clip": "final clip" if count == 1 else "final clips",
        "processing_report": "processing report" if count == 1 else "processing reports",
        "post_processing_report": "post-processing report"
        if count == 1
        else "post-processing reports",
        "failed_job": "failed job" if count == 1 else "failed jobs",
        "active_job": "active job" if count == 1 else "active jobs",
    }
    if artifact_type in labels:
        return labels[artifact_type]
    return artifact_type.replace("_", " ")


def _count_by_type(decisions: list[RetentionFileDecision]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in decisions:
        counts[item.artifact_type] = counts.get(item.artifact_type, 0) + 1
    return counts


def format_terminal_summary(report: RetentionPlanReport) -> str:
    """Human-readable dry-run summary for terminal output."""
    env_title = "Production" if report.environment == "production" else "Development"
    report.finalize_summaries()
    protection = protection_summary_from_decisions(
        report.protected_files, report.unknown_files
    )
    lines = [
        f"Retention Preview: {env_title}",
        "",
    ]

    if not report.retention_enabled:
        lines.append("Note: storage.retention.enabled is false — apply mode would not run.")
        lines.append("")

    eligible_by_type = _count_by_type(report.eligible_files)
    lines.append("Would delete")
    lines.append("-------------")
    if eligible_by_type:
        for artifact_type in sorted(eligible_by_type):
            count = eligible_by_type[artifact_type]
            lines.append(f"{count} {_type_label(artifact_type, count)}")
    else:
        lines.append("(none)")
    lines.append("")

    lines.append("Protected")
    lines.append("---------")
    protected_lines: list[str] = []
    if protection["protected_failed_jobs"]:
        n = protection["protected_failed_jobs"]
        protected_lines.append(f"{n} {_type_label('failed_job', n)}")
    if protection["protected_final_clips"]:
        n = protection["protected_final_clips"]
        protected_lines.append(f"{n} {_type_label('final_clip', n)}")
    if protection["protected_active_jobs"]:
        n = protection["protected_active_jobs"]
        protected_lines.append(f"{n} {_type_label('active_job', n)}")
    if protection["protected_databases"]:
        n = protection["protected_databases"]
        protected_lines.append(f"{n} database{'s' if n != 1 else ''}")
    protected_by_type = _count_by_type(report.protected_files)
    for artifact_type in sorted(protected_by_type):
        if artifact_type in {"final_clip", "database"}:
            continue
        count = protected_by_type[artifact_type]
        protected_lines.append(f"{count} {_type_label(artifact_type, count)}")
    if protected_lines:
        lines.extend(protected_lines)
    else:
        lines.append("(none)")
    lines.append("")

    if report.unknown_count:
        lines.append("Unknown")
        lines.append("-------")
        lines.append(f"{report.unknown_count} file{'s' if report.unknown_count != 1 else ''}")
        lines.append("")

    lines.append("Estimated reclaimable space:")
    lines.append(format_bytes(report.bytes_reclaimable))
    lines.append("")
    lines.append(f"Duration: {report.duration_seconds:.2f}s")
    lines.append("No files deleted.")

    if report.errors:
        lines.append("")
        lines.append("Errors:")
        for err in report.errors:
            lines.append(f"  - {err}")

    return "\n".join(lines)


def format_apply_terminal_summary(report: RetentionApplyReport) -> str:
    report.finalize_summaries()
    deleted_by_type: dict[str, int] = {}
    skip_by_reason: dict[str, int] = {}
    for item in report.deletions:
        if item.outcome == "DELETED":
            deleted_by_type[item.artifact_type] = (
                deleted_by_type.get(item.artifact_type, 0) + 1
            )
        elif item.outcome == "SKIPPED" and item.skip_reason:
            skip_by_reason[item.skip_reason] = skip_by_reason.get(item.skip_reason, 0) + 1

    lines = [
        "Retention Apply Complete",
        "",
        f"Source plan: {report.source_plan_id}",
        "",
        "Deleted:",
    ]
    if deleted_by_type:
        for artifact_type in sorted(deleted_by_type):
            count = deleted_by_type[artifact_type]
            lines.append(f"{count} {_type_label(artifact_type, count)}")
    else:
        lines.append("(none)")
    lines.append("")

    protection = report.protection_summary or {}
    lines.append("Protected:")
    protected_lines: list[str] = []
    if protection.get("protected_failed_jobs"):
        n = protection["protected_failed_jobs"]
        protected_lines.append(f"{n} {_type_label('failed_job', n)}")
    if protection.get("protected_final_clips"):
        n = protection["protected_final_clips"]
        protected_lines.append(f"{n} {_type_label('final_clip', n)}")
    if protection.get("protected_active_jobs"):
        n = protection["protected_active_jobs"]
        protected_lines.append(f"{n} {_type_label('active_job', n)}")
    if protection.get("protected_databases"):
        n = protection["protected_databases"]
        protected_lines.append(f"{n} database{'s' if n != 1 else ''}")
    if protected_lines:
        lines.extend(protected_lines)
    else:
        lines.append("(none)")
    lines.append("")

    lines.append("Skipped:")
    if skip_by_reason:
        for reason in sorted(skip_by_reason):
            count = skip_by_reason[reason]
            label = reason.replace("_", " ")
            lines.append(f"{count} {label}")
    else:
        lines.append("(none)")
    lines.append("")

    if report.failed_deletions:
        lines.append(f"Failed: {report.failed_deletions}")
        lines.append("")

    lines.append("Reclaimed:")
    lines.append(format_bytes(report.bytes_reclaimed))
    lines.append("")
    lines.append(f"Duration: {report.execution_duration_seconds:.2f}s")

    if report.errors:
        lines.extend(["", "Errors:"])
        for err in report.errors:
            lines.append(f"  - {err}")

    return "\n".join(lines)
