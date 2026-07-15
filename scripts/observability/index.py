"""Run and job indexing for observability endpoints (Phase 3).

Reads existing run records and environment-scoped job ``report.json`` files.
Does not invent state, resolve artifacts, or scan arbitrary directories.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contract import run_summary_from_run_record_dict
from .models import (
    ArtifactReference,
    FailureSummary,
    JobDetail,
    JobOutputs,
    JobSummary,
    LogReference,
    RunSummary,
    StageTimelineEntry,
)
from .populate import sanitize_detail
from .schemas import CONTRACT_SCHEMA_VERSION

_OPS_DIR = Path(__file__).resolve().parent.parent / "ops"
if str(_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_OPS_DIR))

from ops_readonly import REPO_ROOT, canonical_env, ensure_config_scripts_on_path, mk04_env  # noqa: E402
from run_records import (  # noqa: E402
    list_run_dirs,
    read_record,
    record_path_for,
    run_dir_for,
)

ensure_config_scripts_on_path()
from config_manager import ConfigError, ConfigManager  # noqa: E402
from state_paths import EnvironmentStatePaths  # noqa: E402

DEFAULT_RUN_LIMIT = 50
DEFAULT_JOB_LIMIT = 50

_FAIL_STATUSES = frozenset({"failed", "failure", "error", "cancelled", "canceled"})
_SUCCESS_STATUSES = frozenset({"completed", "success", "done", "succeeded"})
_RUNNING_STATUSES = frozenset({"running", "in_progress", "processing"})
_QUEUED_STATUSES = frozenset({"queued", "pending"})

_SAFE_ID_RE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-."
)


def _env_token(mk04_env_token: str) -> str:
    return mk04_env(canonical_env(mk04_env_token))


def _is_safe_id(value: str) -> bool:
    text = (value or "").strip()
    if not text or ".." in text or "/" in text or "\\" in text:
        return False
    return all(ch in _SAFE_ID_RE_CHARS for ch in text)


# Module-level warning when development falls back to repository jobs/.
JOBS_ROOT_FALLBACK_WARNING: str | None = None


def _jobs_root_for(mk04_env_token: str) -> Path:
    """Resolve jobs root via ConfigManager path authority.

    Production never silently falls back to repository jobs/prod.
    Development may fall back for local tests and sets JOBS_ROOT_FALLBACK_WARNING.
    """
    global JOBS_ROOT_FALLBACK_WARNING
    JOBS_ROOT_FALLBACK_WARNING = None
    token = _env_token(mk04_env_token)
    try:
        resolved = ConfigManager.load(
            environment=canonical_env(mk04_env_token),
            config_root=REPO_ROOT / "config",
        )
        return EnvironmentStatePaths.from_resolved_config(resolved).jobs_root
    except ConfigError as exc:
        if token == "prod" or str(mk04_env_token).lower() in {"prod", "production"}:
            raise RuntimeError(
                f"production jobs root unavailable (no repository fallback): {exc}"
            ) from exc
        JOBS_ROOT_FALLBACK_WARNING = (
            f"Using repository jobs/{token} fallback because ConfigManager failed: {exc}"
        )
        return REPO_ROOT / "jobs" / token


def _safe_run_log_ref(token: str, run_id: str) -> str:
    return f"runs/{token}/{run_id}/run.log"


def _safe_path_ref(raw: str | None, *, token: str) -> str | None:
    """Return an environment-relative reference; drop unrestricted absolute paths."""
    if not raw:
        return None
    text = str(raw).strip().replace("\\", "/")
    if not text:
        return None
    marker = f"/{token}/"
    if marker in text:
        # Keep from environment segment onward: runs/dev/... or jobs/dev/...
        for prefix in ("runs/", "jobs/", "logs/", "reports/", "outputs/", "data/"):
            idx = text.find(prefix)
            if idx >= 0:
                return text[idx:]
        # Fallback: token-relative suffix.
        idx = text.find(marker)
        return text[idx + 1 :]
    if text.startswith(("runs/", "jobs/", "logs/", "reports/", "outputs/", "data/")):
        return text
    if text.startswith("/"):
        return None
    return text


def _finalize_run_summary(summary: RunSummary, *, token: str) -> RunSummary:
    """Apply safe path references without changing field meanings."""
    if summary.run_id:
        summary.log_path = _safe_run_log_ref(token, summary.run_id)
    else:
        summary.log_path = _safe_path_ref(summary.log_path, token=token)
    summary.report_paths = [
        ref
        for ref in (_safe_path_ref(p, token=token) for p in summary.report_paths)
        if ref
    ]
    if summary.failure_summary is not None:
        target = summary.failure_summary.suggested_next_inspection_target
        summary.failure_summary.suggested_next_inspection_target = (
            _safe_path_ref(target, token=token) or summary.log_path
        )
        summary.failure_summary.reason = (
            sanitize_detail(summary.failure_summary.reason)
            or summary.failure_summary.reason
        )
    # Ensure environment token is the short form used by ops (dev/prod).
    if summary.environment in {"development", "production"}:
        summary.environment = token
    elif not summary.environment:
        summary.environment = token
    return summary


def list_run_summaries(
    mk04_env_token: str,
    *,
    limit: int = DEFAULT_RUN_LIMIT,
) -> list[RunSummary]:
    """Recent runs for an environment, most recent first."""
    token = _env_token(mk04_env_token)
    limit = max(0, min(int(limit), 200))
    summaries: list[RunSummary] = []
    for run_dir in list_run_dirs(token)[:limit]:
        record = read_record(run_dir)
        if record is None:
            continue
        summary = run_summary_from_run_record_dict(record.to_dict())
        summaries.append(_finalize_run_summary(summary, token=token))
    return summaries


def get_run_summary(mk04_env_token: str, run_id: str) -> RunSummary | None:
    """One run by id, or None if missing / invalid id."""
    if not _is_safe_id(run_id):
        return None
    token = _env_token(mk04_env_token)
    run_dir = run_dir_for(token, run_id)
    if not record_path_for(run_dir).is_file():
        return None
    record = read_record(run_dir)
    if record is None:
        return None
    return _finalize_run_summary(
        run_summary_from_run_record_dict(record.to_dict()),
        token=token,
    )


def _map_job_state(status: str | None) -> str:
    value = (status or "").strip().lower()
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


def _parse_iso(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
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


def _runtime_seconds(report: dict[str, Any]) -> float | None:
    started = _parse_iso(report.get("started_at"))
    ended = _parse_iso(report.get("completed_at"))
    if started is None:
        return None
    if ended is None:
        if _map_job_state(str(report.get("status") or "")) == "running":
            ended = datetime.now(UTC)
        else:
            return None
    return max(0.0, (ended - started).total_seconds())


def _execution_context(report: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    ctx = report.get("execution_context")
    if isinstance(ctx, dict) and ctx:
        return ctx
    path = job_dir / "execution_context.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _job_outputs(report: dict[str, Any]) -> JobOutputs:
    clips = report.get("clips")
    outputs_produced: int | None = None
    if isinstance(clips, list):
        outputs_produced = len(clips)

    candidates = report.get("candidates_discovered")
    if candidates is None:
        pool = report.get("raw_candidate_count")
        candidates = pool
    try:
        candidates_i = int(candidates) if candidates is not None else None
    except (TypeError, ValueError):
        candidates_i = None

    clips_passed = report.get("clips_passed")
    clips_failed = report.get("clips_failed")
    try:
        clips_passed_i = int(clips_passed) if clips_passed is not None else None
    except (TypeError, ValueError):
        clips_passed_i = None
    try:
        clips_failed_i = int(clips_failed) if clips_failed is not None else None
    except (TypeError, ValueError):
        clips_failed_i = None

    if clips_passed_i is None and outputs_produced is not None:
        state = _map_job_state(str(report.get("status") or ""))
        if state == "completed":
            clips_passed_i = outputs_produced

    return JobOutputs(
        candidates_discovered=candidates_i,
        clips_passed=clips_passed_i,
        clips_failed=clips_failed_i,
        outputs_produced=outputs_produced,
    )


def _failure_from_report(report: dict[str, Any]) -> FailureSummary | None:
    errors = report.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            reason = str(first.get("message") or first.get("reason") or first.get("error") or first)
            component = str(first.get("component") or first.get("module") or "job")
            stage = first.get("stage")
        else:
            reason = str(first)
            component = "job"
            stage = report.get("current_stage")
        return FailureSummary(
            component=component,
            reason=sanitize_detail(reason) or reason,
            severity="fail",
            stage=str(stage) if stage else None,
            timestamp=_optional_str(report.get("completed_at") or report.get("started_at")),
            suggested_next_inspection_target=None,
        )

    status = _map_job_state(str(report.get("status") or ""))
    if status == "failed":
        return FailureSummary(
            component="job",
            reason="job failed",
            severity="fail",
            stage=_optional_str(report.get("current_stage")),
            timestamp=_optional_str(report.get("completed_at")),
        )
    return None


def _warnings_from_report(report: dict[str, Any]) -> list[FailureSummary]:
    warnings = report.get("warnings")
    if not isinstance(warnings, list):
        return []
    out: list[FailureSummary] = []
    for item in warnings:
        if isinstance(item, dict):
            reason = str(item.get("message") or item.get("reason") or item.get("warning") or item)
            component = str(item.get("component") or item.get("module") or "job")
            stage = item.get("stage")
        else:
            reason = str(item)
            component = "job"
            stage = report.get("current_stage")
        if not reason.strip():
            continue
        out.append(
            FailureSummary(
                component=component,
                reason=sanitize_detail(reason) or reason,
                severity="warn",
                stage=str(stage) if stage else None,
                timestamp=None,
            )
        )
    return out


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def funnel_id_from_job_report(
    report: dict[str, Any],
    *,
    job_dir: Path | None = None,
) -> str | None:
    """Best-effort funnel id from a job report (and optional execution_context.json)."""
    funnel: str | None = None
    funnel_block = report.get("funnel")
    if isinstance(funnel_block, dict):
        funnel = _optional_str(funnel_block.get("funnel_id"))

    ctx: dict[str, Any] = {}
    if job_dir is not None:
        ctx = _execution_context(report, job_dir)
    else:
        raw = report.get("execution_context")
        if isinstance(raw, dict):
            ctx = raw
    if ctx:
        funnel = funnel or _optional_str(ctx.get("funnel_id"))
    return funnel


def _job_summary_from_report(
    report: dict[str, Any],
    *,
    token: str,
    job_dir: Path,
) -> JobSummary:
    ctx = _execution_context(report, job_dir)
    job_id = str(report.get("job_id") or job_dir.name)
    funnel = None
    platform = None
    preset = None
    run_id = None
    env = token

    funnel_block = report.get("funnel")
    if isinstance(funnel_block, dict):
        funnel = _optional_str(funnel_block.get("funnel_id"))

    if ctx:
        funnel = funnel or _optional_str(ctx.get("funnel_id"))
        platform = _optional_str(ctx.get("platform_id"))
        preset = _optional_str(ctx.get("preset_id"))
        run_id = _optional_str(ctx.get("run_id"))
        ctx_env = _optional_str(ctx.get("environment"))
        if ctx_env in {"development", "dev"}:
            env = "dev"
        elif ctx_env in {"production", "prod"}:
            env = "prod"

    return JobSummary(
        job_id=job_id,
        state=_map_job_state(str(report.get("status") or "")),
        environment=env,
        run_id=run_id,
        funnel=funnel,
        platform=platform,
        preset=preset,
        stage=_optional_str(report.get("current_stage")),
        runtime_seconds=_runtime_seconds(report),
        outputs=_job_outputs(report),
        failure_summary=_failure_from_report(report),
        schema_version=CONTRACT_SCHEMA_VERSION,
    )


def _read_report(job_dir: Path) -> dict[str, Any] | None:
    path = job_dir / "report.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _list_job_dirs(jobs_root: Path, *, limit: int) -> list[Path]:
    """Direct children of jobs_root that have report.json (same pattern as status_report)."""
    if not jobs_root.is_dir():
        return []
    dirs = [p for p in jobs_root.iterdir() if p.is_dir() and (p / "report.json").is_file()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return dirs[:limit]


def list_job_summaries(
    mk04_env_token: str,
    *,
    limit: int = DEFAULT_JOB_LIMIT,
) -> list[JobSummary]:
    """Recent jobs for an environment from jobs/<env>/*/report.json."""
    token = _env_token(mk04_env_token)
    limit = max(0, min(int(limit), 200))
    jobs_root = _jobs_root_for(token)
    summaries: list[JobSummary] = []
    for job_dir in _list_job_dirs(jobs_root, limit=limit):
        report = _read_report(job_dir)
        if report is None:
            continue
        summaries.append(_job_summary_from_report(report, token=token, job_dir=job_dir))
    return summaries


def _job_dir_rank(job_dir: Path) -> tuple[int, int, int, float]:
    """Prefer successful terminal jobs with clips over stale in-progress stubs."""
    report = _read_report(job_dir) or {}
    status = str(report.get("status") or "").lower()
    clips = report.get("clips")
    clip_count = len(clips) if isinstance(clips, list) else 0
    success = 1 if status == "success" else 0
    terminal = 1 if status in {"success", "failed", "completed", "cancelled"} else 0
    try:
        mtime = float(job_dir.stat().st_mtime)
    except OSError:
        mtime = 0.0
    return (success, terminal, clip_count, mtime)


def _find_job_dir(jobs_root: Path, job_id: str) -> Path | None:
    """Locate a job directory by id under jobs_root.

    Supports env-aware folders (``<job_id>/``) and legacy video-automation
    folders named ``<input_stem>_<job_id>`` by matching ``report.json`` job_id.
    When multiple folders share the same job_id, prefer the successful copy
    with clips over a stale queued/running stub.
    """
    if not _is_safe_id(job_id):
        return None
    if not jobs_root.is_dir():
        return None

    matches: list[Path] = []
    direct = jobs_root / job_id
    if (direct / "report.json").is_file():
        matches.append(direct)

    for entry in jobs_root.iterdir():
        if not entry.is_dir() or entry in matches:
            continue
        report_path = entry / "report.json"
        if not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if isinstance(report, dict) and str(report.get("job_id") or "") == job_id:
            matches.append(entry)

    if not matches:
        return None
    return max(matches, key=_job_dir_rank)


def _stage_timeline(report: dict[str, Any]) -> list[StageTimelineEntry]:
    timings = report.get("stage_timings_ms")
    status = _map_job_state(str(report.get("status") or ""))
    current = _optional_str(report.get("current_stage")) or "unknown"

    if isinstance(timings, dict) and timings:
        entries: list[StageTimelineEntry] = []
        for stage_name in timings:
            stage = str(stage_name)
            result = "completed"
            if stage == current and status == "running":
                result = "running"
            elif stage == current and status == "failed":
                result = "failed"
            entries.append(StageTimelineEntry(stage=stage, result=result))
        return entries

    if status == "queued":
        result = "pending"
    elif status == "running":
        result = "running"
    elif status == "failed":
        result = "failed"
    elif status == "completed":
        result = "completed"
    elif status == "cancelled":
        result = "skipped"
    else:
        result = "unknown"
    return [StageTimelineEntry(stage=current, result=result)]


def get_job_detail(mk04_env_token: str, job_id: str) -> JobDetail | None:
    """One JobDetail for the Job Inspector (observability aggregation layer)."""
    # Lazy import avoids circular dependency with artifacts/job_inspector.
    from .job_inspector import build_job_detail

    return build_job_detail(mk04_env_token, job_id)


def runs_list_payload(mk04_env_token: str, *, limit: int = DEFAULT_RUN_LIMIT) -> dict[str, Any]:
    token = _env_token(mk04_env_token)
    runs = list_run_summaries(token, limit=limit)
    return {
        "environment": token,
        "runs": [run.to_dict() for run in runs],
        "count": len(runs),
        "schema_version": CONTRACT_SCHEMA_VERSION,
    }


def jobs_list_payload(mk04_env_token: str, *, limit: int = DEFAULT_JOB_LIMIT) -> dict[str, Any]:
    token = _env_token(mk04_env_token)
    jobs = list_job_summaries(token, limit=limit)
    return {
        "environment": token,
        "jobs": [job.to_dict() for job in jobs],
        "count": len(jobs),
        "schema_version": CONTRACT_SCHEMA_VERSION,
    }
