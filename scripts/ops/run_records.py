#!/usr/bin/env python3
"""Pipeline run records (Reliability & Recovery Phase 8).

Canonical history of every pipeline execution under:

  runs/<env>/<run_id>/run_record.json
  runs/<env>/<run_id>/run.log

Consumed later by Operations & Observability. Not UI-specific state.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops_readonly import REPO_ROOT, canonical_env, git_commit, mk04_env

SCHEMA_VERSION = 1

STATUS_RUNNING = "RUNNING"
STATUS_SUCCESS = "SUCCESS"
STATUS_FAIL = "FAIL"
STATUS_SKIPPED = "SKIPPED"

TERMINAL_STATUSES = frozenset({STATUS_SUCCESS, STATUS_FAIL, STATUS_SKIPPED})
ALLOWED_TRIGGERS = frozenset(
    {"scheduled", "manual_cli", "operations_ui", "remote_ssh", "test"}
)


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _utc_iso(dt: datetime | None = None) -> str:
    value = dt or _utc_now()
    return value.isoformat().replace("+00:00", "Z")


def _parse_iso(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def runs_root_for_env(mk04_env_token: str, *, repo_root: Path | None = None) -> Path:
    """Resolve runs root via path-authority contract when ConfigManager is available."""
    import os
    import sys

    from ops_readonly import SCRIPTS_CONFIG

    token = mk04_env(canonical_env(mk04_env_token))
    root = repo_root if repo_root is not None else REPO_ROOT

    explicit = os.environ.get("MK04_RUNS_ROOT", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()

    config_root = root / "config"
    if config_root.is_dir() and (config_root / "environments").is_dir():
        if str(SCRIPTS_CONFIG) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_CONFIG))
        try:
            from config_manager import ConfigManager  # noqa: PLC0415

            resolved = ConfigManager.load(
                environment=canonical_env(mk04_env_token),
                config_root=config_root,
            )
            runs = getattr(resolved.paths, "runs_root", None)
            if runs is not None:
                return Path(runs).resolve()
        except Exception:
            if token == "prod" and (
                os.environ.get("MK04_RUNTIME_ROOT", "").strip()
                or os.environ.get("MK04_REQUIRE_RUNTIME_PATHS", "").strip()
                in {"1", "true", "yes"}
            ):
                raise

    return (root / "runs" / token).resolve()


def run_dir_for(mk04_env_token: str, run_id: str, *, repo_root: Path | None = None) -> Path:
    return runs_root_for_env(mk04_env_token, repo_root=repo_root) / run_id


def record_path_for(run_dir: Path) -> Path:
    return run_dir / "run_record.json"


@dataclass
class RunRecord:
    """Canonical pipeline run record."""

    run_id: str
    environment: str
    trigger: str
    status: str
    started_at: str
    log_path: str
    funnel_id: str = ""
    finished_at: str | None = None
    duration_seconds: float | None = None
    failure_reason: str | None = None
    jobs_started: int = 0
    jobs_completed: int = 0
    jobs_failed: int = 0
    report_paths: list[str] = field(default_factory=list)
    code_commit: str | None = None
    config_snapshot_path: str | None = None
    exit_code: int | None = None
    detail: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        report_paths = data.get("report_paths") or []
        if not isinstance(report_paths, list):
            report_paths = []
        return cls(
            run_id=str(data.get("run_id") or ""),
            environment=str(data.get("environment") or ""),
            trigger=str(data.get("trigger") or ""),
            status=str(data.get("status") or STATUS_RUNNING),
            started_at=str(data.get("started_at") or ""),
            log_path=str(data.get("log_path") or ""),
            funnel_id=str(data.get("funnel_id") or ""),
            finished_at=data.get("finished_at"),
            duration_seconds=data.get("duration_seconds"),
            failure_reason=data.get("failure_reason"),
            jobs_started=int(data.get("jobs_started") or 0),
            jobs_completed=int(data.get("jobs_completed") or 0),
            jobs_failed=int(data.get("jobs_failed") or 0),
            report_paths=[str(p) for p in report_paths],
            code_commit=data.get("code_commit"),
            config_snapshot_path=data.get("config_snapshot_path"),
            exit_code=data.get("exit_code"),
            detail=data.get("detail"),
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        )


def compute_duration_seconds(started_at: str, finished_at: str) -> float | None:
    start = _parse_iso(started_at)
    end = _parse_iso(finished_at)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def write_record(run_dir: Path, record: RunRecord) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = record_path_for(run_dir)
    path.write_text(json.dumps(record.to_dict(), indent=2) + "\n", encoding="utf-8")
    return path


def read_record(run_dir: Path) -> RunRecord | None:
    path = record_path_for(run_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return RunRecord.from_dict(data)


def resolve_code_commit(repo_root: Path | None = None) -> str | None:
    root = repo_root if repo_root is not None else REPO_ROOT
    commit, _detail = git_commit(root)
    if not commit or commit == "unknown":
        return None
    return commit


def write_config_snapshot(run_dir: Path, resolved: Any) -> str | None:
    """Write resolved_config.yaml via ResolvedConfig.save_snapshot when available."""
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        if hasattr(resolved, "save_snapshot"):
            path = resolved.save_snapshot(run_dir)
            return str(path)
    except Exception:
        return None
    return None


def create_running_record(
    *,
    run_dir: Path,
    run_id: str,
    environment: str,
    trigger: str,
    funnel_id: str,
    log_path: Path,
    code_commit: str | None = None,
    config_snapshot_path: str | None = None,
    started_at: str | None = None,
) -> RunRecord:
    """Create the single in-progress record for a run (after lock acquired)."""
    existing = read_record(run_dir)
    if existing is not None and existing.status in TERMINAL_STATUSES:
        # Never overwrite a terminal record with RUNNING.
        return existing

    record = RunRecord(
        run_id=run_id,
        environment=environment,
        trigger=trigger,
        status=STATUS_RUNNING,
        started_at=started_at or _utc_iso(),
        log_path=str(log_path),
        funnel_id=funnel_id,
        code_commit=code_commit if code_commit is not None else resolve_code_commit(),
        config_snapshot_path=config_snapshot_path,
    )
    write_record(run_dir, record)
    return record


def finalize_record(
    run_dir: Path,
    *,
    status: str,
    exit_code: int,
    failure_reason: str | None = None,
    detail: str | None = None,
    jobs_started: int | None = None,
    jobs_completed: int | None = None,
    jobs_failed: int | None = None,
    report_paths: list[str] | None = None,
    finished_at: str | None = None,
) -> RunRecord:
    """Move the run record to a terminal status. Idempotent for same terminal status."""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"status must be terminal, got {status!r}")

    existing = read_record(run_dir)
    end = finished_at or _utc_iso()

    if existing is None:
        record = RunRecord(
            run_id=run_dir.name,
            environment="",
            trigger="",
            status=status,
            started_at=end,
            finished_at=end,
            duration_seconds=0.0,
            log_path=str(run_dir / "run.log"),
            failure_reason=failure_reason,
            detail=detail,
            exit_code=exit_code,
            jobs_started=jobs_started or 0,
            jobs_completed=jobs_completed or 0,
            jobs_failed=jobs_failed or 0,
            report_paths=list(report_paths or []),
            code_commit=resolve_code_commit(),
        )
        write_record(run_dir, record)
        return record

    if existing.status in TERMINAL_STATUSES:
        # Already terminal — do not reopen or duplicate.
        return existing

    existing.status = status
    existing.finished_at = end
    existing.duration_seconds = compute_duration_seconds(existing.started_at, end)
    existing.exit_code = exit_code
    existing.failure_reason = failure_reason
    existing.detail = detail
    if jobs_started is not None:
        existing.jobs_started = jobs_started
    if jobs_completed is not None:
        existing.jobs_completed = jobs_completed
    if jobs_failed is not None:
        existing.jobs_failed = jobs_failed
    if report_paths is not None:
        existing.report_paths = list(report_paths)
    write_record(run_dir, existing)
    return existing


def write_terminal_record(
    *,
    run_dir: Path,
    run_id: str,
    environment: str,
    trigger: str,
    funnel_id: str,
    log_path: Path,
    status: str,
    exit_code: int,
    failure_reason: str | None = None,
    detail: str | None = None,
    jobs_started: int = 0,
    jobs_completed: int = 0,
    jobs_failed: int = 0,
    report_paths: list[str] | None = None,
    code_commit: str | None = None,
    config_snapshot_path: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> RunRecord:
    """Create a record that is already terminal (early skip/fail before lock)."""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"status must be terminal, got {status!r}")

    start = started_at or _utc_iso()
    end = finished_at or start
    record = RunRecord(
        run_id=run_id,
        environment=environment,
        trigger=trigger,
        status=status,
        started_at=start,
        finished_at=end,
        duration_seconds=compute_duration_seconds(start, end),
        log_path=str(log_path),
        funnel_id=funnel_id,
        failure_reason=failure_reason,
        detail=detail,
        exit_code=exit_code,
        jobs_started=jobs_started,
        jobs_completed=jobs_completed,
        jobs_failed=jobs_failed,
        report_paths=list(report_paths or []),
        code_commit=code_commit if code_commit is not None else resolve_code_commit(),
        config_snapshot_path=config_snapshot_path,
    )
    write_record(run_dir, record)
    return record


def ensure_terminal(
    run_dir: Path,
    *,
    status: str = STATUS_FAIL,
    exit_code: int = 1,
    failure_reason: str = "run ended without explicit finalisation",
) -> RunRecord | None:
    """If a RUNNING record remains, force it to a terminal state."""
    existing = read_record(run_dir)
    if existing is None:
        return None
    if existing.status in TERMINAL_STATUSES:
        return existing
    return finalize_record(
        run_dir,
        status=status,
        exit_code=exit_code,
        failure_reason=failure_reason,
        detail=failure_reason,
    )


def list_run_dirs(mk04_env_token: str, *, repo_root: Path | None = None) -> list[Path]:
    root = runs_root_for_env(mk04_env_token, repo_root=repo_root)
    if not root.is_dir():
        return []
    dirs = [p for p in root.iterdir() if p.is_dir() and record_path_for(p).is_file()]
    return sorted(dirs, key=lambda p: p.name, reverse=True)


def latest_run_record(
    mk04_env_token: str,
    *,
    repo_root: Path | None = None,
) -> RunRecord | None:
    dirs = list_run_dirs(mk04_env_token, repo_root=repo_root)
    if not dirs:
        return None
    return read_record(dirs[0])


def job_counts_for_pipeline_status(pipeline_status: str) -> tuple[int, int, int]:
    """Map POST /run-funnel status to orchestration-level job counters."""
    if pipeline_status == "input_ready":
        return 1, 1, 0
    if pipeline_status == "no_input_available":
        return 0, 0, 0
    return 0, 0, 0
