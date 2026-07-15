"""Safe retention apply executor (Storage & Data Management Phase 5).

Executes a previously built dry-run plan. Performs per-file safety validation
immediately before deletion. Does not re-evaluate retention policy.

Does **not** implement scheduling, disk-pressure deletion, or automatic retention.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCRIPTS_CONFIG = Path(__file__).resolve().parents[1] / "config"
if str(_SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_CONFIG))

from config_manager import ResolvedConfig  # noqa: E402

from .artifact_classifier import ArtifactClassifier
from .retention_report import (
    DeletionRecord,
    RetentionApplyReport,
    RetentionFileDecision,
    RetentionPlanReport,
    new_retention_run_id,
    protection_summary_from_decisions,
)
from .retention_safety import is_within_any_root, resolve_allowed_roots

_ACTIVE_JOB_STATES = frozenset({"running", "queued"})
_NEVER_DELETE_TYPES = frozenset({"unknown", "database"})


@dataclass(frozen=True)
class SafetyCheckResult:
    ok: bool
    skip_reason: str | None = None
    resolved_path: str | None = None
    size_bytes: int | None = None
    current_artifact_type: str | None = None


@dataclass
class RetentionApplyExecutor:
    """Conservative executor for planner-approved deletions only."""

    resolved: ResolvedConfig
    plan: RetentionPlanReport
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=UTC)
        if self.plan.mode != "dry-run":
            raise ValueError("apply executor requires a dry-run retention plan")
        if self.plan.environment != self.resolved.environment:
            raise ValueError(
                f"plan environment {self.plan.environment!r} does not match "
                f"resolved config {self.resolved.environment!r}"
            )
        self._classifier = ArtifactClassifier(self.resolved, now=self.now)
        self._state = self.resolved.state_paths
        env_token = "prod" if self.resolved.environment == "production" else "dev"
        backups_root = (Path(self.resolved._repo_root) / "backups" / env_token).resolve()
        raw_roots = self.resolved.get("storage.allowed_delete_roots") or []
        self._allowed_keys = [
            str(item) for item in raw_roots if isinstance(item, str) and item.strip()
        ]
        self._allowed_roots = resolve_allowed_roots(
            self._state,
            self._allowed_keys,
            backups_root=backups_root,
        )
        self._protected_types = {
            str(item)
            for item in (self.resolved.get("storage.protected_artifact_types") or [])
            if isinstance(item, str) and item.strip()
        }
        self._auto_delete_final_clips_prod = bool(
            self.resolved.get("storage.auto_delete_final_clips_prod")
        )
        self._final_clip_opt_in = bool(
            self.resolved.get("storage.allow_final_clip_auto_deletion_opt_in")
        )

    def execute(self) -> RetentionApplyReport:
        started = self.now
        apply_id = new_retention_run_id(now=started)
        report = RetentionApplyReport(
            retention_run_id=apply_id,
            source_plan_id=self.plan.retention_run_id,
            environment=self.resolved.environment,
            mode="apply",
            policy_version=self.plan.policy_version,
            planner_version=self.plan.planner_version,
            started_at=_iso(started),
            finished_at="",
            planned_deletions=len(self.plan.eligible_files),
            files_considered=self.plan.files_considered,
            files_protected=self.plan.protected_count,
            files_unknown=self.plan.unknown_count,
            bytes_reclaimable=self.plan.bytes_reclaimable,
            bytes_considered=self.plan.bytes_considered
            or sum(
                max(0, int(item.size_bytes or 0))
                for item in self.plan.eligible_files
            ),
            protection_summary=protection_summary_from_decisions(
                self.plan.protected_files,
                self.plan.unknown_files,
            ),
        )

        if not bool(self.resolved.get("storage.retention.enabled")):
            report.errors.append("storage.retention.enabled is false — apply refused")
            report.finished_at = _iso(datetime.now(UTC))
            report.finalize_summaries()
            return report

        for planned in sorted(self.plan.eligible_files, key=lambda item: item.path):
            record = self._attempt_deletion(planned)
            report.deletions.append(record)
            if record.outcome == "DELETED":
                report.successful_deletions += 1
                if record.size_bytes is not None:
                    report.bytes_reclaimed += max(0, record.size_bytes)
            elif record.outcome == "SKIPPED":
                report.skipped_deletions += 1
                if record.size_bytes is not None:
                    report.skipped_bytes += max(0, record.size_bytes)
            else:
                report.failed_deletions += 1

        finished = datetime.now(UTC)
        report.finished_at = _iso(finished)
        report.execution_duration_seconds = max(
            0.0, (finished - started).total_seconds()
        )
        report.finalize_summaries()
        return report

    def _attempt_deletion(self, planned: RetentionFileDecision) -> DeletionRecord:
        ts = _iso(datetime.now(UTC))
        age = planned.age_seconds

        safety = self._validate_safety(planned)
        if not safety.ok:
            return DeletionRecord(
                timestamp=ts,
                environment=self.resolved.environment,
                artifact_type=planned.artifact_type,
                original_path=planned.path,
                resolved_path=safety.resolved_path,
                size_bytes=safety.size_bytes if safety.size_bytes is not None else planned.size_bytes,
                planner_reason=planned.reason,
                outcome="SKIPPED",
                skip_reason=safety.skip_reason,
                age_seconds=age,
            )

        target = Path(safety.resolved_path)  # type: ignore[arg-type]
        size = safety.size_bytes

        try:
            target.unlink()
        except FileNotFoundError:
            return DeletionRecord(
                timestamp=ts,
                environment=self.resolved.environment,
                artifact_type=planned.artifact_type,
                original_path=planned.path,
                resolved_path=str(target),
                size_bytes=size,
                planner_reason=planned.reason,
                outcome="SKIPPED",
                skip_reason="file_not_found",
                age_seconds=age,
            )
        except OSError as exc:
            return DeletionRecord(
                timestamp=ts,
                environment=self.resolved.environment,
                artifact_type=planned.artifact_type,
                original_path=planned.path,
                resolved_path=str(target),
                size_bytes=size,
                planner_reason=planned.reason,
                outcome="FAILED",
                skip_reason="filesystem_error",
                error=str(exc),
                age_seconds=age,
            )

        return DeletionRecord(
            timestamp=ts,
            environment=self.resolved.environment,
            artifact_type=planned.artifact_type,
            original_path=planned.path,
            resolved_path=str(target),
            size_bytes=size,
            planner_reason=planned.reason,
            outcome="DELETED",
            age_seconds=age,
        )

    def _validate_safety(self, planned: RetentionFileDecision) -> SafetyCheckResult:
        if planned.disposition != "eligible":
            return SafetyCheckResult(False, skip_reason="planner_mismatch")

        if planned.artifact_type in _NEVER_DELETE_TYPES:
            return SafetyCheckResult(False, skip_reason="protected_type")

        if self._is_final_clip_blocked(planned.artifact_type):
            return SafetyCheckResult(False, skip_reason="final_clip_default_protected")

        if planned.artifact_type in self._protected_types:
            return SafetyCheckResult(False, skip_reason="protected_type")

        try:
            candidate = Path(planned.path)
        except (TypeError, ValueError):
            return SafetyCheckResult(False, skip_reason="invalid_path")

        if candidate.is_symlink():
            return SafetyCheckResult(False, skip_reason="symlink_detected")

        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return SafetyCheckResult(False, skip_reason="path_resolve_failed")

        if resolved.is_symlink():
            return SafetyCheckResult(False, skip_reason="symlink_detected")

        if not resolved.is_file():
            return SafetyCheckResult(
                False,
                skip_reason="file_not_found",
                resolved_path=str(resolved),
            )

        if not is_within_any_root(resolved, self._allowed_roots):
            return SafetyCheckResult(
                False,
                skip_reason="outside_allowed_root",
                resolved_path=str(resolved),
            )

        try:
            size = int(resolved.stat().st_size)
        except OSError:
            size = planned.size_bytes

        current = self._classifier.classify(resolved)
        if current.environment != self.resolved.environment:
            return SafetyCheckResult(
                False,
                skip_reason="environment_mismatch",
                resolved_path=str(resolved),
                size_bytes=size,
                current_artifact_type=current.artifact_type,
            )

        if current.artifact_type == "unknown":
            return SafetyCheckResult(
                False,
                skip_reason="unknown_artifact_type",
                resolved_path=str(resolved),
                size_bytes=size,
                current_artifact_type=current.artifact_type,
            )

        if current.artifact_type != planned.artifact_type:
            return SafetyCheckResult(
                False,
                skip_reason="planner_mismatch",
                resolved_path=str(resolved),
                size_bytes=size,
                current_artifact_type=current.artifact_type,
            )

        if current.artifact_type in _NEVER_DELETE_TYPES:
            return SafetyCheckResult(
                False,
                skip_reason="protected_database" if current.artifact_type == "database" else "protected_type",
                resolved_path=str(resolved),
                size_bytes=size,
                current_artifact_type=current.artifact_type,
            )

        if self._is_final_clip_blocked(current.artifact_type):
            return SafetyCheckResult(
                False,
                skip_reason="final_clip_default_protected",
                resolved_path=str(resolved),
                size_bytes=size,
                current_artifact_type=current.artifact_type,
            )

        if current.artifact_type in self._protected_types:
            return SafetyCheckResult(
                False,
                skip_reason="protected_type",
                resolved_path=str(resolved),
                size_bytes=size,
                current_artifact_type=current.artifact_type,
            )

        if (
            current.current_state in _ACTIVE_JOB_STATES
            or "active_job" in current.protection_flags
        ):
            return SafetyCheckResult(
                False,
                skip_reason="active_job",
                resolved_path=str(resolved),
                size_bytes=size,
                current_artifact_type=current.artifact_type,
            )

        return SafetyCheckResult(
            True,
            resolved_path=str(resolved),
            size_bytes=size,
            current_artifact_type=current.artifact_type,
        )

    def _is_final_clip_blocked(self, artifact_type: str) -> bool:
        if artifact_type != "final_clip":
            return False
        if self.resolved.environment != "production":
            return False
        return not (self._auto_delete_final_clips_prod and self._final_clip_opt_in)


def run_retention_apply(
    resolved: ResolvedConfig,
    plan: RetentionPlanReport,
    *,
    now: datetime | None = None,
    report_dir: Path | None = None,
) -> tuple[RetentionApplyReport, Path]:
    """Execute apply mode and write a new apply report (never overwrites dry-run)."""
    executor = RetentionApplyExecutor(resolved, plan, now=now or datetime.now(UTC))
    report = executor.execute()

    if report_dir is None:
        report_dir = resolved.state_paths.reports_root / "retention"

    report_path = report_dir / f"{report.retention_run_id}_apply.json"
    report.write_json(report_path)
    return report, report_path


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")
