"""Artifact classification for Storage & Data Management (Phase 3).

Answers only: "What artifact is this?"

Does **not**:
  - delete files
  - plan deletion
  - scan for expired artifacts
  - evaluate retention periods
  - implement dry-run or apply modes

Classification is deterministic, environment-scoped, and policy-neutral.
Unknown paths remain ``unknown`` rather than being guessed.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCRIPTS_CONFIG = Path(__file__).resolve().parents[1] / "config"
if str(_SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_CONFIG))

from config_manager import ResolvedConfig  # noqa: E402
from state_paths import EnvironmentStatePaths  # noqa: E402

from .artifact_record import ArtifactRecord, DeletionEligibility
from .artifact_types import OWNER_BY_TYPE

# ---------------------------------------------------------------------------
# Constants (structural conventions from STORAGE_INVENTORY.md — not policy)
# ---------------------------------------------------------------------------

_CLIP_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".mkv", ".m4v"})
_SOURCE_EXTENSIONS = _CLIP_EXTENSIONS | {".avi", ".mpeg", ".mpg", ".ts", ".m2ts"}
_METADATA_SUFFIXES = ("_metadata_writer_v1.json", "_metadata.json")

_FAIL_STATUSES = frozenset({"failed", "failure", "error", "cancelled", "canceled"})
_RUNNING_STATUSES = frozenset({"running", "in_progress", "processing"})
_QUEUED_STATUSES = frozenset({"queued", "pending", "waiting"})
_SUCCESS_STATUSES = frozenset({"completed", "success", "succeeded", "ok", "passed"})

# Exact job-root filenames → type (directory + filename, no guessing by extension alone).
_JOB_ROOT_EXACT: dict[str, tuple[str, str]] = {
    "transcript.json": ("transcript", "directory_and_filename"),
    "transcript_payload.json": ("transcript", "directory_and_filename"),
    "raw_candidate_pool.json": ("raw_candidate_pool", "directory_and_filename"),
    "processing_report.json": ("processing_report", "directory_and_filename"),
    "transcript_sections.json": ("processing_report", "directory_and_filename"),
    "section_candidate_discovery.json": ("processing_report", "directory_and_filename"),
    "candidate_processing.json": ("processing_report", "directory_and_filename"),
    "selection.json": ("selection_result", "directory_and_filename"),
    "post_processing_report.json": ("post_processing_report", "directory_and_filename"),
    "report.json": ("job_report", "directory_and_filename"),
    "execution_context.json": ("execution_context", "directory_and_filename"),
    "resolved_config.yaml": ("config_snapshot", "directory_and_filename"),
    "resolved_config.yml": ("config_snapshot", "directory_and_filename"),
    "job.log": ("job_log", "directory_and_filename"),
    "pipeline.log": ("job_log", "directory_and_filename"),
}

_RUN_DIR_EXACT: dict[str, tuple[str, str]] = {
    "run_record.json": ("run_record", "directory_and_filename"),
    "run.log": ("job_log", "directory_and_filename"),
    "resolved_config.yaml": ("config_snapshot", "directory_and_filename"),
    "resolved_config.yml": ("config_snapshot", "directory_and_filename"),
}

_DATA_ROOT_EXACT: dict[str, tuple[str, str]] = {
    "control_state.json": ("control_state", "directory_and_filename"),
    "last_update_status.json": ("last_update_status", "directory_and_filename"),
    "pipeline_execution.lock": ("pipeline_execution_lock", "directory_and_filename"),
}


@dataclass(frozen=True)
class _Match:
    artifact_type: str
    classification_source: str
    job_id: str | None = None
    run_id: str | None = None
    notes: tuple[str, ...] = ()


@dataclass
class _JobContext:
    job_id: str
    run_id: str | None
    state: str
    report: dict[str, Any] | None


class ArtifactClassifier:
    """Environment-scoped artifact classifier.

    Construct from a ``ResolvedConfig`` so paths and protected types come from
    ConfigManager — never hardcoded environment roots.
    """

    def __init__(
        self,
        resolved: ResolvedConfig,
        *,
        now: datetime | None = None,
    ) -> None:
        self._resolved = resolved
        self._state: EnvironmentStatePaths = resolved.state_paths
        self._environment = resolved.environment
        self._env_token = "dev" if self._environment == "development" else "prod"
        self._repo_root = Path(resolved._repo_root).resolve()
        self._runs_root = (self._repo_root / "runs" / self._env_token).resolve()
        self._backups_root = (self._repo_root / "backups" / self._env_token).resolve()
        protected = resolved.get("storage.protected_artifact_types") or []
        self._protected_types = {
            str(item) for item in protected if isinstance(item, str) and item.strip()
        }
        self._now = now or datetime.now(UTC)
        if self._now.tzinfo is None:
            self._now = self._now.replace(tzinfo=UTC)
        self._job_ctx_cache: dict[str, _JobContext] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(self, path: str | Path) -> ArtifactRecord:
        """Classify a single path within this environment.

        Never raises for unknown or missing files. Paths outside environment
        roots are reported as ``unknown`` with an environment-boundary note.
        """
        notes: list[str] = []
        try:
            candidate = Path(path)
        except (TypeError, ValueError):
            return self._build_record(
                artifact_type="unknown",
                path=str(path),
                classification_source="unclassified",
                notes=("invalid_path",),
                exists=False,
            )

        # Resolve when possible; keep original string if resolution fails.
        try:
            resolved_path = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            resolved_path = candidate
            notes.append("path_resolve_failed")

        exists = resolved_path.is_file() or resolved_path.is_dir()
        if not exists:
            notes.append("path_does_not_exist")

        if not self._is_in_scope(resolved_path):
            notes.append("outside_environment_roots")
            return self._build_record(
                artifact_type="unknown",
                path=str(resolved_path),
                classification_source="unclassified",
                notes=tuple(notes),
                exists=exists,
            )

        match = self._match(resolved_path)
        job_ctx: _JobContext | None = None
        run_id = match.run_id
        job_id = match.job_id
        current_state: str | None = None

        if job_id:
            job_ctx = self._job_context(job_id)
            current_state = job_ctx.state
            if run_id is None:
                run_id = job_ctx.run_id

        # Prefer run_id from execution context / report over path alone.
        if job_ctx and job_ctx.run_id and run_id is None:
            run_id = job_ctx.run_id

        all_notes = tuple(notes) + match.notes
        return self._build_record(
            artifact_type=match.artifact_type,
            path=str(resolved_path),
            classification_source=match.classification_source,
            job_id=job_id,
            run_id=run_id,
            current_state=current_state,
            notes=all_notes,
            exists=exists,
            stat_path=resolved_path if exists else None,
        )

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------

    def _is_in_scope(self, path: Path) -> bool:
        if self._state.is_within_environment(path):
            return True
        if _is_under(path, self._runs_root):
            return True
        if _is_under(path, self._backups_root):
            return True
        return False

    # ------------------------------------------------------------------
    # Matching (deterministic, ordered by specificity)
    # ------------------------------------------------------------------

    def _match(self, path: Path) -> _Match:
        # 1. Exact database path (active DB — never guess by extension alone).
        if path.resolve() == self._state.database_path.resolve():
            return _Match("database", "path_identity")

        # 2. Backups
        if _is_under(path, self._backups_root):
            return self._match_backup(path)

        # 3. Runs
        if _is_under(path, self._runs_root):
            return self._match_run(path)

        # 4. Jobs
        if _is_under(path, self._state.jobs_root):
            return self._match_job(path)

        # 5. Global outputs clips
        if _is_under(path, self._state.clips_root):
            return self._match_global_clip(path)

        # 6. Transcripts archive root
        if _is_under(path, self._state.transcripts_root):
            return self._match_transcript_archive(path)

        # 7. Cache / temporary
        if _is_under(path, self._state.caches_root):
            if path.is_file() or path.suffix:
                return _Match("temporary_file", "directory_location")
            return _Match("unknown", "directory_location", notes=("directory_not_file",))

        # 8. Data root exact files
        if _is_under(path, self._state.data_root):
            return self._match_data_root(path)

        # 9. Service logs
        if _is_under(path, self._state.logs_root):
            return self._match_service_log(path)

        # 10. Reports root — no stable naming convention beyond job-local reports.
        if _is_under(path, self._state.reports_root):
            return _Match(
                "unknown",
                "directory_location",
                notes=("reports_root_untyped",),
            )

        # 11. Outputs root (non-clips)
        if _is_under(path, self._state.outputs_root):
            return _Match(
                "unknown",
                "directory_location",
                notes=("outputs_root_untyped",),
            )

        return _Match("unknown", "unclassified")

    def _match_backup(self, path: Path) -> _Match:
        name = path.name
        # Operational archive backups (Reliability backup_control).
        if name.startswith("backup_") and (
            name.endswith(".tar.gz") or name.endswith(".manifest.json")
        ):
            return _Match("database_backup", "directory_and_filename")
        # Dedicated SQLite database backups (Storage Phase 10).
        if name.startswith("db_") and (
            name.endswith(".sqlite3")
            or name.endswith(".db")
            or name.endswith(".manifest.json")
        ):
            return _Match("database_backup", "directory_and_filename")
        return _Match(
            "unknown",
            "directory_location",
            notes=("backup_root_untyped",),
        )

    def _match_run(self, path: Path) -> _Match:
        rel = _relative_parts(path, self._runs_root)
        if not rel:
            return _Match("unknown", "directory_location")
        run_id = rel[0]
        if len(rel) == 1:
            return _Match(
                "unknown",
                "directory_location",
                run_id=run_id,
                notes=("run_directory",),
            )
        filename = rel[-1]
        if filename in _RUN_DIR_EXACT:
            artifact_type, source = _RUN_DIR_EXACT[filename]
            return _Match(artifact_type, source, run_id=run_id)
        return _Match(
            "unknown",
            "directory_location",
            run_id=run_id,
            notes=("run_path_untyped",),
        )

    def _match_job(self, path: Path) -> _Match:
        rel = _relative_parts(path, self._state.jobs_root)
        if not rel:
            return _Match("unknown", "directory_location")
        job_id = rel[0]
        if len(rel) == 1:
            return _Match(
                "unknown",
                "directory_location",
                job_id=job_id,
                notes=("job_directory",),
            )

        parts = rel[1:]
        filename = parts[-1]

        # Job-root exact names.
        if len(parts) == 1 and filename in _JOB_ROOT_EXACT:
            artifact_type, source = _JOB_ROOT_EXACT[filename]
            return _Match(artifact_type, source, job_id=job_id)

        # Source video: input_<name> at job root with media extension.
        if len(parts) == 1 and filename.startswith("input_"):
            if Path(filename).suffix.lower() in _SOURCE_EXTENSIONS:
                return _Match("source_video", "directory_and_filename", job_id=job_id)
            return _Match(
                "unknown",
                "directory_and_filename",
                job_id=job_id,
                notes=("input_prefix_non_media",),
            )

        # Selection result (preferred path).
        if parts == ("post_processing", "selection", "selection_result.json"):
            return _Match(
                "selection_result",
                "directory_and_filename",
                job_id=job_id,
            )

        # Post-processing report (preferred path).
        if parts == (
            "post_processing",
            "reports",
            "post_processing_report.json",
        ):
            return _Match(
                "post_processing_report",
                "directory_and_filename",
                job_id=job_id,
            )

        # Related handoff under reports.
        if (
            len(parts) == 3
            and parts[0] == "post_processing"
            and parts[1] == "reports"
            and filename == "output_funnel_handoff.json"
        ):
            return _Match(
                "post_processing_report",
                "directory_and_filename",
                job_id=job_id,
                notes=("handoff_report",),
            )

        # Clip metadata.
        if (
            len(parts) == 3
            and parts[0] == "post_processing"
            and parts[1] == "metadata"
            and _is_metadata_filename(filename)
        ):
            return _Match("clip_metadata", "directory_and_filename", job_id=job_id)

        # Temporary / intermediate under post_processing/tmp.
        if len(parts) >= 2 and parts[0] == "post_processing" and parts[1] == "tmp":
            if path.is_dir() and len(parts) == 2:
                return _Match(
                    "unknown",
                    "directory_location",
                    job_id=job_id,
                    notes=("tmp_directory",),
                )
            # Media in tmp is intermediate; other files are temporary.
            if Path(filename).suffix.lower() in _CLIP_EXTENSIONS:
                return _Match(
                    "intermediate_render",
                    "directory_location",
                    job_id=job_id,
                )
            return _Match("temporary_file", "directory_location", job_id=job_id)

        # Final / stage clips.
        if _is_clip_filename(filename):
            if parts[0] == "clips" and len(parts) == 2:
                return _Match("final_clip", "directory_and_filename", job_id=job_id)
            if (
                len(parts) == 3
                and parts[0] == "post_processing"
                and parts[1] == "clips"
            ):
                stage = _clip_stage_type(filename)
                return _Match(stage, "directory_and_filename", job_id=job_id)
            # Global-style under job but unknown subdir — do not guess.
            return _Match(
                "unknown",
                "filename",
                job_id=job_id,
                notes=("media_outside_known_clip_dirs",),
            )

        # Job-root *.log fallback only for exact known names (already handled).
        # Do not classify arbitrary .log by extension alone.
        return _Match(
            "unknown",
            "unclassified",
            job_id=job_id,
            notes=("job_path_untyped",),
        )

    def _match_global_clip(self, path: Path) -> _Match:
        if path.is_dir():
            return _Match(
                "unknown",
                "directory_location",
                notes=("clips_directory",),
            )
        if _is_clip_filename(path.name):
            return _Match("final_clip", "directory_and_filename")
        return _Match(
            "unknown",
            "directory_location",
            notes=("clips_root_non_media",),
        )

    def _match_transcript_archive(self, path: Path) -> _Match:
        if path.is_dir():
            return _Match(
                "unknown",
                "directory_location",
                notes=("transcripts_directory",),
            )
        # Only exact known transcript names — not every file under the tree.
        if path.name in {"transcript.json", "transcript_payload.json"}:
            return _Match("transcript", "directory_and_filename")
        return _Match(
            "unknown",
            "directory_location",
            notes=("transcripts_root_untyped",),
        )

    def _match_data_root(self, path: Path) -> _Match:
        rel = _relative_parts(path, self._state.data_root)
        if len(rel) == 1 and rel[0] in _DATA_ROOT_EXACT:
            artifact_type, source = _DATA_ROOT_EXACT[rel[0]]
            return _Match(artifact_type, source)
        return _Match(
            "unknown",
            "directory_location",
            notes=("data_root_untyped",),
        )

    def _match_service_log(self, path: Path) -> _Match:
        if path.is_dir():
            return _Match(
                "unknown",
                "directory_location",
                notes=("logs_directory",),
            )
        # Files under logs/<env>/ are service logs by inventory location.
        # Includes active logs and rotated generations (name.log.1, name.log.1.gz).
        name = path.name.lower()
        log_like = (
            name.endswith(".log")
            or name.endswith(".log.txt")
            or name.endswith(".ndjson")
            or name.endswith(".jsonl")
            or ".log." in name
            or ".ndjson." in name
            or ".jsonl." in name
        )
        if log_like:
            return _Match("service_log", "directory_and_filename")
        return _Match(
            "unknown",
            "directory_location",
            notes=("logs_root_untyped",),
        )

    # ------------------------------------------------------------------
    # Job metadata (report / execution context)
    # ------------------------------------------------------------------

    def _job_context(self, job_id: str) -> _JobContext:
        cached = self._job_ctx_cache.get(job_id)
        if cached is not None:
            return cached

        job_dir = self._state.jobs_root / job_id
        report = _read_json(job_dir / "report.json")
        ctx = _read_json(job_dir / "execution_context.json")
        if ctx is None and isinstance(report, dict):
            maybe = report.get("execution_context")
            if isinstance(maybe, dict):
                ctx = maybe

        run_id: str | None = None
        if isinstance(ctx, dict):
            raw = ctx.get("run_id")
            if isinstance(raw, str) and raw.strip():
                run_id = raw.strip()

        state = "unknown"
        if isinstance(report, dict):
            state = _map_job_state(report.get("status"))
            if run_id is None:
                raw_run = report.get("run_id")
                if isinstance(raw_run, str) and raw_run.strip():
                    run_id = raw_run.strip()

        result = _JobContext(job_id=job_id, run_id=run_id, state=state, report=report)
        self._job_ctx_cache[job_id] = result
        return result

    # ------------------------------------------------------------------
    # Record assembly
    # ------------------------------------------------------------------

    def _build_record(
        self,
        *,
        artifact_type: str,
        path: str,
        classification_source: str,
        job_id: str | None = None,
        run_id: str | None = None,
        current_state: str | None = None,
        notes: tuple[str, ...] = (),
        exists: bool = False,
        stat_path: Path | None = None,
    ) -> ArtifactRecord:
        size_bytes: int | None = None
        created_at: str | None = None
        modified_at: str | None = None
        age_seconds: float | None = None

        if stat_path is not None and stat_path.exists():
            try:
                st = stat_path.stat()
                size_bytes = int(st.st_size) if stat_path.is_file() else None
                modified_at = _utc_iso(st.st_mtime)
                # Prefer birth time when the platform exposes it.
                birth = getattr(st, "st_birthtime", None)
                created_at = _utc_iso(birth if birth is not None else st.st_ctime)
                age_seconds = max(0.0, self._now.timestamp() - float(st.st_mtime))
            except OSError:
                notes = notes + ("stat_failed",)

        flags = self._protection_flags(
            artifact_type=artifact_type,
            current_state=current_state,
        )
        eligibility = self._deletion_eligibility(
            artifact_type=artifact_type,
            protection_flags=flags,
        )

        return ArtifactRecord(
            artifact_type=artifact_type,
            path=path,
            environment=self._environment,
            job_id=job_id,
            run_id=run_id,
            owner=OWNER_BY_TYPE.get(artifact_type),
            size_bytes=size_bytes,
            created_at=created_at,
            modified_at=modified_at,
            age_seconds=age_seconds,
            current_state=current_state,
            classification_source=classification_source,
            protection_flags=flags,
            deletion_eligibility=eligibility,
            notes=notes,
            exists=exists,
        )

    def _protection_flags(
        self,
        *,
        artifact_type: str,
        current_state: str | None,
    ) -> tuple[str, ...]:
        flags: list[str] = []
        if artifact_type == "unknown":
            flags.append("unknown")
        if artifact_type == "final_clip":
            flags.append("final_clip")
        if artifact_type == "database":
            flags.append("database")
        if artifact_type in self._protected_types:
            flags.append("protected_type")
        if current_state in {"running", "queued"}:
            flags.append("active_job")
        if current_state == "failed":
            flags.append("failed_job")
        # Stable order for determinism.
        order = (
            "active_job",
            "failed_job",
            "final_clip",
            "database",
            "protected_type",
            "unknown",
        )
        return tuple(flag for flag in order if flag in flags)

    def _deletion_eligibility(
        self,
        *,
        artifact_type: str,
        protection_flags: tuple[str, ...],
    ) -> DeletionEligibility:
        """Descriptive eligibility only — no retention policy evaluation."""
        if "active_job" in protection_flags:
            return DeletionEligibility(eligible="false", reason="active_job")
        if "database" in protection_flags:
            return DeletionEligibility(eligible="false", reason="database")
        if "final_clip" in protection_flags:
            return DeletionEligibility(eligible="false", reason="final_clip")
        if "protected_type" in protection_flags:
            return DeletionEligibility(eligible="false", reason="protected_type")
        if "unknown" in protection_flags or artifact_type == "unknown":
            return DeletionEligibility(eligible="false", reason="unknown")
        # Planner (Phase 4) owns retention decisions, including failed_job longevity.
        if "failed_job" in protection_flags:
            return DeletionEligibility(eligible="unknown", reason="failed_job")
        return DeletionEligibility(eligible="unknown", reason="planner_not_implemented")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    try:
        return path.resolve().relative_to(root.resolve()).parts
    except (ValueError, OSError):
        return ()


def _is_clip_filename(name: str) -> bool:
    return Path(name).suffix.lower() in _CLIP_EXTENSIONS


def _is_metadata_filename(name: str) -> bool:
    return any(name.endswith(suffix) for suffix in _METADATA_SUFFIXES)


def _clip_stage_type(filename: str) -> str:
    """Classify post_processing/clips media using explicit name markers only."""
    lower = filename.lower()
    # Captioned before formatted: a file may include both markers in tests.
    if "captioned" in lower:
        return "captioned_clip"
    if "formatted" in lower:
        return "formatted_clip"
    # Default for known clip dirs: inventory/observability treat these as finals.
    return "final_clip"


def _map_job_state(status: Any) -> str:
    value = str(status or "").strip().lower()
    if value in _QUEUED_STATUSES:
        return "queued"
    if value in _RUNNING_STATUSES:
        return "running"
    if value in _SUCCESS_STATUSES:
        return "completed"
    if value in {"cancelled", "canceled"}:
        return "cancelled"
    if value in _FAIL_STATUSES:
        return "failed"
    if not value:
        return "unknown"
    return "unknown"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _utc_iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return (
        datetime.fromtimestamp(float(timestamp), tz=UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def classify_artifact(
    path: str | Path,
    *,
    resolved: ResolvedConfig,
    now: datetime | None = None,
) -> ArtifactRecord:
    """Classify ``path`` using an existing ResolvedConfig."""
    return ArtifactClassifier(resolved, now=now).classify(path)
