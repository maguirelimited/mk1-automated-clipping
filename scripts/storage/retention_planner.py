"""Retention dry-run planner for Storage & Data Management (Phase 4).

Consumes ``ArtifactRecord`` instances from ``ArtifactClassifier`` and evaluates
configured retention policy. Produces an explainable plan and JSON report.

Does **not** delete files, implement apply mode, disk pressure, or scheduling.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

_SCRIPTS_CONFIG = Path(__file__).resolve().parents[1] / "config"
if str(_SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_CONFIG))

from config_manager import ResolvedConfig  # noqa: E402
from state_paths import EnvironmentStatePaths  # noqa: E402

from .artifact_classifier import ArtifactClassifier
from .artifact_record import ArtifactRecord
from .retention_report import (
    RetentionFileDecision,
    RetentionPlanReport,
    new_retention_run_id,
)

# Config retention key per artifact type (policy mapping — not classification).
_RETENTION_DAYS_KEY_BY_TYPE: dict[str, str] = {
    "source_video": "source_videos_days",
    "transcript": "transcripts_days",
    "raw_candidate_pool": "raw_candidate_pools_days",
    "processing_report": "processing_reports_days",
    "selection_result": "selection_results_days",
    "post_processing_report": "post_processing_reports_days",
    "clip_metadata": "clip_metadata_days",
    "intermediate_render": "intermediate_renders_days",
    "formatted_clip": "intermediate_renders_days",
    "captioned_clip": "intermediate_renders_days",
    "temporary_file": "temporary_files_days",
    "job_log": "logs_days",
    "service_log": "logs_days",
    "run_record": "run_records_days",
    "config_snapshot": "config_snapshots_days",
    "database_backup": "database_backups_days",
}

# Types always protected regardless of age (operational / safety).
_ALWAYS_PROTECTED_TYPES = frozenset(
    {
        "database",
        "control_state",
        "pipeline_execution_lock",
        "execution_context",
        "last_update_status",
        "ops_ui_controls",
        "input_ledger_record",
    }
)

# Types without an explicit retention period in policy — never guess.
_NO_RETENTION_POLICY_TYPES = frozenset(
    {
        "job_report",
    }
)

_ACTIVE_JOB_STATES = frozenset({"running", "queued"})

_SECONDS_PER_DAY = 86400.0


@dataclass
class _JobUploadContext:
    """Upload evidence from output-funnel handoff (when present)."""

    handoff_present: bool
    confirmed_paths: frozenset[str] = frozenset()


@dataclass
class RetentionPlanner:
    """Policy evaluator for retention dry-run."""

    resolved: ResolvedConfig
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=UTC)
        self._classifier = ArtifactClassifier(self.resolved, now=self.now)
        self._state = self.resolved.state_paths
        self._env_token = "dev" if self.resolved.environment == "development" else "prod"
        self._repo_root = Path(self.resolved._repo_root).resolve()
        self._runs_root = (self._repo_root / "runs" / self._env_token).resolve()
        self._backups_root = (self._repo_root / "backups" / self._env_token).resolve()
        self._retention_cfg: dict[str, Any] = self.resolved.get("storage.retention") or {}
        self._retention_enabled = bool(self._retention_cfg.get("enabled"))
        raw_roots = self.resolved.get("storage.allowed_delete_roots") or []
        self._allowed_root_keys = [
            str(item) for item in raw_roots if isinstance(item, str) and item.strip()
        ]
        self._allowed_roots = self._resolve_allowed_roots()
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
        self._upload_cache: dict[str, _JobUploadContext] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan_dry_run(self) -> RetentionPlanReport:
        started = self.now
        run_id = new_retention_run_id(now=started)
        report = RetentionPlanReport(
            retention_run_id=run_id,
            environment=self.resolved.environment,
            mode="dry-run",
            policy_version=self._policy_version(),
            retention_enabled=self._retention_enabled,
            started_at=_iso(started),
            finished_at="",
        )

        decisions: list[RetentionFileDecision] = []
        seen_paths: set[str] = set()

        for path in self._discover_file_paths():
            key = str(path.resolve())
            if key in seen_paths:
                continue
            seen_paths.add(key)

            try:
                record = self._classifier.classify(path)
            except Exception as exc:  # noqa: BLE001 — planner must not crash
                report.errors.append(f"classify failed for {path}: {exc}")
                continue

            if not record.exists:
                continue

            report.files_considered += 1
            decisions.append(self._evaluate(record))

        self._finalize_report(report, decisions, finished=datetime.now(UTC))
        return report

    # ------------------------------------------------------------------
    # Discovery (enumerate candidates — not expired-file scanning)
    # ------------------------------------------------------------------

    def _discover_file_paths(self) -> Iterator[Path]:
        roots = [
            self._state.jobs_root,
            self._state.logs_root,
            self._state.reports_root,
            self._state.data_root,
            self._state.outputs_root,
            self._runs_root,
            self._backups_root,
        ]
        db = self._state.database_path
        if db.is_file():
            yield db

        for root in roots:
            if not root.is_dir():
                continue
            yield from self._walk_files(root)

    def _walk_files(self, root: Path) -> Iterator[Path]:
        try:
            for dirpath, dirnames, filenames in os.walk(
                root,
                topdown=True,
                followlinks=False,
            ):
                # Do not follow symlinked directories.
                dirnames[:] = [
                    name
                    for name in dirnames
                    if not (Path(dirpath) / name).is_symlink()
                ]
                for name in filenames:
                    path = Path(dirpath) / name
                    if path.is_symlink():
                        continue
                    if path.is_file():
                        yield path
        except OSError as exc:
            # Record at plan level via empty yield; caller may note unreadable root.
            return

    # ------------------------------------------------------------------
    # Policy evaluation
    # ------------------------------------------------------------------

    def _evaluate(self, record: ArtifactRecord) -> RetentionFileDecision:
        artifact_type = record.artifact_type
        path = Path(record.path)

        if artifact_type == "unknown":
            return self._decision(
                record,
                disposition="unknown",
                reason="unknown_artifact_type",
            )

        if artifact_type in _ALWAYS_PROTECTED_TYPES:
            return self._decision(
                record,
                disposition="protected",
                reason="protected_type",
            )

        if artifact_type in self._protected_types and artifact_type != "final_clip":
            return self._decision(
                record,
                disposition="protected",
                reason="protected_type",
            )

        if record.current_state in _ACTIVE_JOB_STATES or "active_job" in record.protection_flags:
            return self._decision(
                record,
                disposition="protected",
                reason="active_job",
            )

        if not self._path_in_allowed_root(path):
            return self._decision(
                record,
                disposition="protected",
                reason="outside_allowed_root",
            )

        if artifact_type == "final_clip":
            return self._evaluate_final_clip(record)

        if record.current_state == "failed" or "failed_job" in record.protection_flags:
            return self._evaluate_with_retention_days(
                record,
                retention_days=self._retention_int("failed_job_artifacts_days"),
                failed_job=True,
            )

        policy_key = _RETENTION_DAYS_KEY_BY_TYPE.get(artifact_type)
        if artifact_type in _NO_RETENTION_POLICY_TYPES or policy_key is None:
            return self._decision(
                record,
                disposition="protected",
                reason="no_retention_policy_for_type",
            )

        return self._evaluate_with_retention_days(
            record,
            retention_days=self._retention_int(policy_key),
            failed_job=False,
        )

    def _evaluate_final_clip(self, record: ArtifactRecord) -> RetentionFileDecision:
        is_production = self.resolved.environment == "production"

        if is_production and not (
            self._auto_delete_final_clips_prod and self._final_clip_opt_in
        ):
            return self._decision(
                record,
                disposition="protected",
                reason="final_clip_default_protected",
            )

        upload_state = self._upload_state(record)
        if upload_state == "unknown":
            return self._decision(
                record,
                disposition="protected",
                reason="upload_state_unknown",
            )
        if upload_state != "confirmed":
            return self._decision(
                record,
                disposition="protected",
                reason="upload_not_confirmed",
            )

        backup_state = self._backup_state(record)
        if backup_state == "unknown":
            return self._decision(
                record,
                disposition="protected",
                reason="backup_state_unknown",
            )
        if backup_state != "confirmed":
            return self._decision(
                record,
                disposition="protected",
                reason="backup_not_confirmed",
            )

        # Explicit opt-in path only: evaluate age against clip_metadata period
        # as the conservative business-output retention anchor.
        return self._evaluate_with_retention_days(
            record,
            retention_days=self._retention_int("clip_metadata_days"),
            failed_job=False,
            deletion_reason_prefix="expired_final_clip",
        )

    def _evaluate_with_retention_days(
        self,
        record: ArtifactRecord,
        *,
        retention_days: int | None,
        failed_job: bool,
        deletion_reason_prefix: str | None = None,
    ) -> RetentionFileDecision:
        if retention_days is None:
            return self._decision(
                record,
                disposition="protected",
                reason="retention_policy_missing",
                retention_days=None,
            )

        age_seconds = record.age_seconds
        if age_seconds is None:
            return self._decision(
                record,
                disposition="protected",
                reason="age_unknown",
                retention_days=retention_days,
            )

        if age_seconds < retention_days * _SECONDS_PER_DAY:
            reason = "failed_job" if failed_job else "not_expired"
            return self._decision(
                record,
                disposition="protected",
                reason=reason,
                retention_days=retention_days,
            )

        if not self._retention_enabled:
            return self._decision(
                record,
                disposition="protected",
                reason="retention_policy_disabled",
                retention_days=retention_days,
            )

        reason = deletion_reason_prefix or self._expired_reason(record.artifact_type)
        return self._decision(
            record,
            disposition="eligible",
            reason=reason,
            retention_days=retention_days,
        )

    def _expired_reason(self, artifact_type: str) -> str:
        if artifact_type in {"job_log", "service_log"}:
            return "expired_log"
        if artifact_type == "temporary_file":
            return "expired_temp_file"
        if artifact_type == "intermediate_render":
            return "expired_intermediate_render"
        if artifact_type == "source_video":
            return "expired_source_video"
        return f"expired_{artifact_type}"

    def _decision(
        self,
        record: ArtifactRecord,
        *,
        disposition: str,
        reason: str,
        retention_days: int | None = None,
    ) -> RetentionFileDecision:
        return RetentionFileDecision(
            path=record.path,
            artifact_type=record.artifact_type,
            disposition=disposition,  # type: ignore[arg-type]
            reason=reason,
            size_bytes=record.size_bytes,
            job_id=record.job_id,
            run_id=record.run_id,
            age_seconds=record.age_seconds,
            retention_days=retention_days,
            current_state=record.current_state,
        )

    # ------------------------------------------------------------------
    # Upload / backup evidence (no guessing)
    # ------------------------------------------------------------------

    def _upload_state(self, record: ArtifactRecord) -> str:
        """Return confirmed | not_uploaded | unknown."""
        if not record.job_id:
            return "unknown"
        ctx = self._job_upload_context(record.job_id)
        if not ctx.handoff_present:
            return "unknown"
        resolved = str(Path(record.path).resolve())
        if resolved in ctx.confirmed_paths:
            return "confirmed"
        # Handoff exists but this clip path is not listed.
        return "not_uploaded"

    def _backup_state(self, record: ArtifactRecord) -> str:
        """Return confirmed | not_backed_up | unknown.

        Operational backups exclude media by design — final clips are not in
        manifests. Without explicit media-backup evidence, state stays unknown.
        """
        if record.artifact_type != "final_clip":
            return "unknown"
        # Manifests intentionally exclude clips; do not infer backup from them.
        return "unknown"

    def _job_upload_context(self, job_id: str) -> _JobUploadContext:
        cached = self._upload_cache.get(job_id)
        if cached is not None:
            return cached

        handoff_path = (
            self._state.jobs_root / job_id / "post_processing/reports/output_funnel_handoff.json"
        )
        if not handoff_path.is_file():
            ctx = _JobUploadContext(handoff_present=False)
            self._upload_cache[job_id] = ctx
            return ctx

        try:
            data = json.loads(handoff_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            ctx = _JobUploadContext(handoff_present=True, confirmed_paths=frozenset())
            self._upload_cache[job_id] = ctx
            return ctx

        paths: set[str] = set()
        if isinstance(data, dict):
            for raw in data.get("finished_clip_paths") or []:
                if isinstance(raw, str) and raw.strip():
                    try:
                        paths.add(str(Path(raw).resolve()))
                    except OSError:
                        paths.add(raw.strip())

        ctx = _JobUploadContext(handoff_present=True, confirmed_paths=frozenset(paths))
        self._upload_cache[job_id] = ctx
        return ctx

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_allowed_roots(self) -> dict[str, Path]:
        mapping = {
            "jobs": self._state.jobs_root,
            "logs": self._state.logs_root,
            "reports": self._state.reports_root,
            "data": self._state.data_root,
            "backups": self._backups_root,
        }
        return {
            key: mapping[key].resolve()
            for key in self._allowed_root_keys
            if key in mapping
        }

    def _path_in_allowed_root(self, path: Path) -> bool:
        resolved = path.resolve()
        for root in self._allowed_roots.values():
            if _is_under(resolved, root):
                return True
        return False

    def _retention_int(self, key: str) -> int | None:
        value = self._retention_cfg.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return None

    def _policy_version(self) -> str:
        defaults_version = self.resolved.get("version")
        version = defaults_version if isinstance(defaults_version, int) else 1
        return f"storage.retention.v{version}"

    def _finalize_report(
        self,
        report: RetentionPlanReport,
        decisions: list[RetentionFileDecision],
        *,
        finished: datetime,
    ) -> None:
        report.finished_at = _iso(finished)

        for decision in sorted(decisions, key=lambda d: (d.disposition, d.path)):
            if decision.disposition == "eligible":
                report.eligible_files.append(decision)
                report.deletion_reasons[decision.reason] = (
                    report.deletion_reasons.get(decision.reason, 0) + 1
                )
                if decision.size_bytes is not None:
                    report.bytes_reclaimable += max(0, decision.size_bytes)
            elif decision.disposition == "unknown":
                report.unknown_files.append(decision)
            else:
                report.protected_files.append(decision)
                report.protection_reasons[decision.reason] = (
                    report.protection_reasons.get(decision.reason, 0) + 1
                )

        report.finalize_summaries()


def run_retention_dry_run(
    resolved: ResolvedConfig,
    *,
    now: datetime | None = None,
    report_dir: Path | None = None,
) -> tuple[RetentionPlanReport, Path]:
    """Execute dry-run planning, write JSON report, return report and path."""
    planner = RetentionPlanner(resolved, now=now or datetime.now(UTC))
    report = planner.plan_dry_run()

    if report_dir is None:
        state = resolved.state_paths
        report_dir = state.reports_root / "retention"

    # Unique filename — never overwrite historical reports.
    report_path = report_dir / f"{report.retention_run_id}.json"
    report.write_json(report_path)
    return report, report_path


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")
