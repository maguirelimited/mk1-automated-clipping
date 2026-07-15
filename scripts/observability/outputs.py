"""Output clip indexing for the Output Browser (Phase 10).

Discovers clips via existing job index + artifact resolver.
Does not invent a parallel registry or scan arbitrary directories.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .artifacts import resolve_job_artifacts
from .index import (
    DEFAULT_JOB_LIMIT,
    _env_token,
    _find_job_dir,
    _is_safe_id,
    _job_dir_rank,
    _job_summary_from_report,
    _jobs_root_for,
    _list_job_dirs,
    _optional_str,
    _read_report,
    funnel_id_from_job_report,
    list_run_summaries,
)
from .models import ArtifactReference, ClipSummary
from .reframe_summary import extract_reframe_summary_from_metadata_payload
from .schemas import CONTRACT_SCHEMA_VERSION

_NOT_AVAILABLE = "Not available"
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".mkv", ".m4v"})
DEFAULT_OUTPUT_LIMIT = 50
DEFAULT_RECENT_RUN_LIMIT = 5
_OPS_MEDIA_PATH = "/ops/outputs/{job_id}/{clip_id}/media"


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _recent_run_windows(
    mk04_env_token: str,
    *,
    run_limit: int,
) -> list[tuple[datetime, datetime | None, str]]:
    """Time windows for the N most recent pipeline runs (newest first).

    Each window is ``[started_at, next_newer_run.started_at)``. The newest run
    has no upper bound so clipping jobs that finish after run SUCCESS still match.
    """
    if run_limit <= 0:
        return []
    try:
        from run_records import list_run_dirs, read_record
    except Exception:
        return []

    runs: list[tuple[datetime, str]] = []
    for run_dir in list_run_dirs(mk04_env_token)[:run_limit]:
        record = read_record(run_dir)
        if record is None:
            continue
        started = _parse_iso_datetime(record.started_at)
        if started is None:
            continue
        runs.append((started, str(record.run_id or run_dir.name)))

    if not runs:
        return []

    runs.sort(key=lambda item: item[0], reverse=True)
    windows: list[tuple[datetime, datetime | None, str]] = []
    for index, (started, run_id) in enumerate(runs):
        upper = runs[index - 1][0] if index > 0 else None
        windows.append((started, upper, run_id))
    return windows


def _report_run_id(report: dict[str, Any]) -> str | None:
    ctx = report.get("execution_context")
    if isinstance(ctx, dict):
        run_id = str(ctx.get("run_id") or "").strip()
        if run_id:
            return run_id
    run_id = str(report.get("run_id") or "").strip()
    return run_id or None


def _job_within_recent_runs(
    report: dict[str, Any],
    windows: list[tuple[datetime, datetime | None, str]],
) -> bool:
    if not windows:
        return False

    run_id = _report_run_id(report)
    if run_id:
        allowed = {window_run_id for _, _, window_run_id in windows if window_run_id}
        if run_id in allowed:
            return True

    for key in ("completed_at", "started_at", "created_at"):
        parsed = _parse_iso_datetime(report.get(key))
        if parsed is None:
            continue
        for started, upper, _window_run_id in windows:
            if parsed < started:
                continue
            if upper is not None and parsed >= upper:
                continue
            return True
    return False


def _utc_iso_from_mtime(path: Path) -> str | None:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return None
    return (
        datetime.fromtimestamp(ts, tz=UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _size_bytes(path: Path) -> int | None:
    try:
        return int(path.stat().st_size)
    except OSError:
        return None


def _resolve_under_job(
    job_dir: Path,
    relative_path: str | None,
    *,
    token: str,
    job_id: str,
) -> Path | None:
    if not relative_path:
        return None
    prefix = f"jobs/{token}/{job_id}/"
    text = relative_path.replace("\\", "/")
    if not text.startswith(prefix):
        return None
    suffix = text[len(prefix) :]
    if not suffix or ".." in suffix.split("/"):
        return None
    target = (job_dir / suffix).resolve()
    try:
        target.relative_to(job_dir.resolve())
    except ValueError:
        return None
    return target


def _media_roots_for(mk04_env_token: str) -> list[Path]:
    """Allowlisted roots for on-disk clip preview paths."""
    token = _env_token(mk04_env_token)
    roots: list[Path] = [_jobs_root_for(token).resolve()]
    try:
        from config_manager import ConfigManager
        from state_paths import EnvironmentStatePaths

        from .index import REPO_ROOT, canonical_env

        resolved = ConfigManager.load(
            environment=canonical_env(mk04_env_token),
            config_root=REPO_ROOT / "config",
        )
        state = EnvironmentStatePaths.from_resolved_config(resolved)
        roots.extend(
            [
                state.outputs_root.resolve(),
                state.clips_root.resolve(),
                (REPO_ROOT / "video-automation" / "output").resolve(),
                (REPO_ROOT / "output").resolve(),
            ]
        )
    except Exception:
        pass
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        text = str(root)
        if text in seen:
            continue
        seen.add(text)
        unique.append(root)
    return unique


def _is_allowed_media_path(path: Path, allowed_roots: list[Path]) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False
    if not resolved.is_file():
        return False
    if resolved.suffix.lower() not in _VIDEO_EXTENSIONS:
        return False
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _report_clip_entries(report: dict[str, Any]) -> list[dict[str, Any]]:
    clips = report.get("clips")
    if not isinstance(clips, list):
        return []
    return [clip for clip in clips if isinstance(clip, dict)]


def _report_clip_id(clip: dict[str, Any], *, job_id: str, index: int) -> str:
    clip_id = str(clip.get("clip_id") or "").strip()
    if clip_id:
        return clip_id
    clip_index = clip.get("clip_index")
    if clip_index is not None:
        return str(clip_index)
    return f"{job_id}_clip_{index:02d}"


def _resolve_report_clip_file(
    job_dir: Path,
    clip: dict[str, Any],
    *,
    token: str,
    job_id: str,
    allowed_roots: list[Path],
) -> Path | None:
    clip_file = os.path.basename(str(clip.get("clip_file") or ""))
    candidates: list[Path] = []
    for key in ("job_clip_path", "clip_path"):
        raw = str(clip.get(key) or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path.is_file() and path.suffix.lower() in _VIDEO_EXTENSIONS:
            if path.is_absolute():
                return path.resolve()
            if _is_allowed_media_path(path, allowed_roots):
                return path.resolve()
    if clip_file:
        candidates.extend(
            [
                job_dir / "clips" / clip_file,
                job_dir / "post_processing" / "clips" / clip_file,
            ]
        )
        relative = f"jobs/{token}/{job_id}/clips/{clip_file}"
        under_job = _resolve_under_job(job_dir, relative, token=token, job_id=job_id)
        if under_job is not None:
            candidates.append(under_job)
    for candidate in candidates:
        if _is_allowed_media_path(candidate, allowed_roots):
            return candidate.expanduser().resolve()
    return None


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _match_metadata(
    clip_id: str,
    metadata_artifacts: list[ArtifactReference],
) -> ArtifactReference | None:
    """Prefer metadata whose filename contains the clip id."""
    existing = [a for a in metadata_artifacts if a.exists and a.path]
    if not existing:
        missing = [a for a in metadata_artifacts if not a.exists]
        return missing[0] if missing else None
    needle = clip_id.lower()
    for artifact in existing:
        name = Path(artifact.path or "").name.lower()
        if needle and needle in name:
            return artifact
    if len(existing) == 1:
        return existing[0]
    return None


def _clip_from_artifact(
    artifact: ArtifactReference,
    *,
    job_id: str,
    token: str,
    funnel: str | None,
    platform: str | None,
    preset: str | None,
    validation_state: str,
    posting_state: str,
    metadata_artifacts: list[ArtifactReference],
    job_dir: Path,
) -> ClipSummary:
    clip_id = Path(artifact.path or "clip").stem
    meta = _match_metadata(clip_id, metadata_artifacts)
    if meta is None:
        meta = ArtifactReference.missing(
            "clip_metadata",
            path=f"jobs/{token}/{job_id}/post_processing/metadata",
            environment=token,
            job_id=job_id,
            detail="no clip metadata files",
        )

    local = _resolve_under_job(job_dir, artifact.path, token=token, job_id=job_id)
    exists = bool(local and local.is_file())
    created_at = artifact.created_at
    size_bytes = artifact.size_bytes
    duration_seconds = None
    source_candidate = None
    warnings: list[str] = []
    clip_validation = validation_state
    clip_posting = posting_state
    reframe_summary: dict[str, Any] | None = None

    if meta.exists and meta.path:
        meta_path = _resolve_under_job(job_dir, meta.path, token=token, job_id=job_id)
        payload = _safe_read_json(meta_path) if meta_path else None
        if payload:
            reframe_summary = extract_reframe_summary_from_metadata_payload(payload)
            source_candidate = _optional_str(
                payload.get("candidate_id")
                or payload.get("source_candidate")
                or payload.get("source_candidate_id")
            )
            duration_raw = payload.get("duration_sec") or payload.get("duration_seconds")
            try:
                duration_seconds = float(duration_raw) if duration_raw is not None else None
            except (TypeError, ValueError):
                duration_seconds = None
            validation = payload.get("validation_result") or payload.get("validation_state")
            if validation:
                clip_validation = str(validation)
            posting = payload.get("posting_state") or payload.get("publish_state")
            if posting:
                clip_posting = str(posting)
            for warning in payload.get("warnings") or []:
                text = str(warning).strip()
                if text:
                    warnings.append(text)
            if payload.get("clip_id"):
                clip_id = str(payload.get("clip_id"))

    if local is not None and local.is_file():
        created_at = created_at or _utc_iso_from_mtime(local)
        size_bytes = size_bytes if size_bytes is not None else _size_bytes(local)
    elif artifact.exists and not exists:
        warnings.append("output file missing on disk")

    preview_available = bool(
        exists and local is not None and local.suffix.lower() in _VIDEO_EXTENSIONS
    )

    return ClipSummary(
        clip_id=clip_id,
        job_id=job_id,
        source_candidate=source_candidate,
        validation_state=clip_validation or "unknown",
        posting_state=clip_posting or "unknown",
        metadata_reference=meta,
        output_path=artifact.path,
        platform=platform,
        funnel=funnel,
        environment=token,
        preset=preset,
        preview_available=preview_available,
        exists=exists,
        created_at=created_at,
        duration_seconds=duration_seconds,
        size_bytes=size_bytes,
        warnings=warnings,
        reframe_summary=reframe_summary,
    )


def _clip_from_report_entry(
    clip: dict[str, Any],
    *,
    job_id: str,
    token: str,
    funnel: str | None,
    platform: str | None,
    preset: str | None,
    validation_state: str,
    metadata_artifacts: list[ArtifactReference],
    job_dir: Path,
    allowed_roots: list[Path],
    index: int,
) -> ClipSummary | None:
    clip_id = _report_clip_id(clip, job_id=job_id, index=index)
    local = _resolve_report_clip_file(
        job_dir,
        clip,
        token=token,
        job_id=job_id,
        allowed_roots=allowed_roots,
    )
    exists = local is not None
    clip_file = os.path.basename(str(clip.get("clip_file") or ""))
    output_path = (
        f"jobs/{token}/{job_id}/clips/{clip_file}"
        if clip_file
        else str(clip.get("clip_path") or "")
    )
    meta = _match_metadata(clip_id, metadata_artifacts)
    if meta is None:
        meta = ArtifactReference.missing(
            "clip_metadata",
            path=f"jobs/{token}/{job_id}/post_processing/metadata",
            environment=token,
            job_id=job_id,
            detail="no clip metadata files",
        )

    validation = clip.get("validation_result") or clip.get("validation_state")
    warnings: list[str] = []
    if clip_file and not exists:
        warnings.append("output file missing on disk")

    if meta.exists and meta.path:
        meta_path = _resolve_under_job(job_dir, meta.path, token=token, job_id=job_id)
        payload = _safe_read_json(meta_path) if meta_path else None
        if payload and payload.get("clip_id"):
            clip_id = str(payload.get("clip_id"))

    preview_available = exists
    created_at = _utc_iso_from_mtime(local) if local is not None else None
    size_bytes = _size_bytes(local) if local is not None else None

    duration_value = None
    duration_raw = clip.get("duration_sec")
    if duration_raw is not None:
        try:
            duration_value = float(duration_raw)
        except (TypeError, ValueError):
            duration_value = None

    return ClipSummary(
        clip_id=clip_id,
        job_id=job_id,
        source_candidate=_optional_str(clip.get("source_candidate_id")),
        validation_state=str(validation or validation_state or "unknown"),
        posting_state="unknown",
        metadata_reference=meta,
        output_path=output_path or None,
        platform=platform,
        funnel=funnel,
        environment=token,
        preset=preset,
        preview_available=preview_available,
        exists=exists,
        created_at=created_at,
        duration_seconds=duration_value,
        size_bytes=size_bytes,
        warnings=warnings,
    )


def _clips_for_job(mk04_env_token: str, job_id: str) -> list[ClipSummary]:
    token = _env_token(mk04_env_token)
    jobs_root = _jobs_root_for(token)
    job_dir = _find_job_dir(jobs_root, job_id)
    if job_dir is None:
        return []
    report = _read_report(job_dir)
    if report is None:
        return []

    summary = _job_summary_from_report(report, token=token, job_dir=job_dir)
    artifacts_payload = resolve_job_artifacts(token, job_id) or {}
    clip_artifacts: list[ArtifactReference] = []
    metadata_artifacts: list[ArtifactReference] = []
    for item in artifacts_payload.get("artifacts") or []:
        if not isinstance(item, dict):
            continue
        ref = ArtifactReference.from_dict(item)
        if ref is None:
            continue
        if ref.artifact_type == "output_clip":
            clip_artifacts.append(ref)
        elif ref.artifact_type == "clip_metadata":
            metadata_artifacts.append(ref)

    # Only real clip files (skip the single missing placeholder).
    existing_or_named = [
        a for a in clip_artifacts if a.exists or (a.path and a.path.endswith(tuple(_VIDEO_EXTENSIONS)))
    ]

    validation_state = "unknown"
    if summary.outputs.clips_failed and int(summary.outputs.clips_failed) > 0:
        validation_state = "failed"
    elif summary.outputs.clips_passed is not None:
        validation_state = "passed"

    clips: list[ClipSummary] = []
    allowed_roots = _media_roots_for(token)
    report_entries = _report_clip_entries(report)
    if report_entries:
        for index, entry in enumerate(report_entries, start=1):
            clip = _clip_from_report_entry(
                entry,
                job_id=summary.job_id,
                token=token,
                funnel=summary.funnel,
                platform=summary.platform,
                preset=summary.preset,
                validation_state=validation_state,
                metadata_artifacts=metadata_artifacts,
                job_dir=job_dir,
                allowed_roots=allowed_roots,
                index=index,
            )
            if clip is not None:
                clips.append(clip)
        return clips

    if existing_or_named:
        for artifact in existing_or_named:
            if not artifact.exists and artifact.detail in {"no output clips", "not found"}:
                continue
            clips.append(
                _clip_from_artifact(
                    artifact,
                    job_id=summary.job_id,
                    token=token,
                    funnel=summary.funnel,
                    platform=summary.platform,
                    preset=summary.preset,
                    validation_state=validation_state,
                    posting_state="unknown",
                    metadata_artifacts=metadata_artifacts,
                    job_dir=job_dir,
                )
            )
        return _dedupe_artifact_clips(clips)
    return clips


def _artifact_clip_rank(clip: ClipSummary) -> tuple[int, str]:
    """Prefer final captioned outputs over intermediate render stages."""
    path = (clip.output_path or "").lower()
    if "intelligent_captions" in path:
        stage = 3
    elif "platform_safe_format" in path:
        stage = 2
    elif "render_clip" in path:
        stage = 1
    else:
        stage = 0
    return (stage, clip.created_at or "")


def _dedupe_artifact_clips(clips: list[ClipSummary]) -> list[ClipSummary]:
    """Collapse intermediate stage files that resolve to the same clip_id."""
    best: dict[str, ClipSummary] = {}
    order: list[str] = []
    for clip in clips:
        key = str(clip.clip_id or "").strip() or f"anon:{id(clip)}"
        previous = best.get(key)
        if previous is None:
            best[key] = clip
            order.append(key)
            continue
        if _artifact_clip_rank(clip) > _artifact_clip_rank(previous):
            best[key] = clip
    return [best[key] for key in order]


_RUN_SUCCESS_STATUS = "SUCCESS"


def latest_successful_run_id(mk04_env_token: str, *, limit: int = 20) -> str | None:
    """Return the most recent pipeline run id with status SUCCESS, or None.

    Ordering follows ``list_run_summaries`` (run directory names sorted
    descending via ``run_records.list_run_dirs``). Tests assume run ids are
    assigned so newer runs sort first; started_at is not used for ordering.
    """
    token = _env_token(mk04_env_token)
    scan_limit = max(1, min(int(limit), 200))
    for summary in list_run_summaries(token, limit=scan_limit):
        if str(summary.status or "").upper() != _RUN_SUCCESS_STATUS:
            continue
        run_id = str(summary.run_id or "").strip()
        if run_id:
            return run_id
    return None


def latest_run_id_with_clips(mk04_env_token: str, *, limit: int = 20) -> str | None:
    """Most recent pipeline run that has at least one discoverable output clip.

    Scans newest runs first regardless of SUCCESS/FAIL. Timed-out orchestration
    runs can still own clipping jobs via time-window association, so operators
    reviewing Outputs see the latest run's clips without a manual run switch.
    Falls back to ``latest_successful_run_id`` when no run has clips yet.
    """
    token = _env_token(mk04_env_token)
    scan_limit = max(1, min(int(limit), 200))
    for summary in list_run_summaries(token, limit=scan_limit):
        run_id = str(summary.run_id or "").strip()
        if not run_id or not _is_safe_id(run_id):
            continue
        if list_clips_for_run(token, run_id):
            return run_id
    return latest_successful_run_id(token, limit=scan_limit)


def _run_time_window_for_id(
    token: str,
    run_id: str,
) -> tuple[datetime, datetime | None] | None:
    """Return [started_at, next_newer_run.started_at) for associating async clipping jobs."""
    try:
        from run_records import list_run_dirs, read_record, run_dir_for
    except Exception:
        return None

    run_dir = run_dir_for(token, run_id)
    record = read_record(run_dir)
    if record is None:
        return None
    started = _parse_iso_datetime(record.started_at)
    if started is None:
        return None

    upper: datetime | None = None
    for other_dir in list_run_dirs(token):
        if other_dir.name == run_id:
            continue
        other = read_record(other_dir)
        if other is None:
            continue
        other_started = _parse_iso_datetime(other.started_at)
        if other_started is None or other_started <= started:
            continue
        if upper is None or other_started < upper:
            upper = other_started
    return started, upper


def _clipping_job_ids_from_run_log(token: str, run_id: str) -> list[str]:
    """Parse clipping_job.job_id values recorded in the pipeline run log."""
    try:
        from run_records import run_dir_for
    except Exception:
        return []

    log_path = run_dir_for(token, run_id) / "run.log"
    if not log_path.is_file():
        return []

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    job_ids: list[str] = []
    seen: set[str] = set()
    marker = '"clipping_job"'
    needle = '"job_id":"'
    for line in text.splitlines():
        if marker not in line:
            continue
        start = 0
        while True:
            idx = line.find(needle, start)
            if idx < 0:
                break
            value_start = idx + len(needle)
            value_end = line.find('"', value_start)
            if value_end < 0:
                break
            job_id = line[value_start:value_end].strip()
            start = value_end + 1
            if not job_id or not _is_safe_id(job_id) or job_id in seen:
                continue
            seen.add(job_id)
            job_ids.append(job_id)
    return job_ids


def _job_started_within_run_window(
    report: dict[str, Any],
    window: tuple[datetime, datetime | None],
) -> bool:
    started, upper = window
    for key in ("started_at", "created_at"):
        parsed = _parse_iso_datetime(report.get(key))
        if parsed is None:
            continue
        if parsed < started:
            continue
        if upper is not None and parsed >= upper:
            continue
        return True
    return False


def _job_ids_for_run_id(
    token: str,
    run_id: str,
    *,
    job_limit: int | None = None,
) -> list[str]:
    """Job ids linked to a pipeline run (report run_id, run log, or time window)."""
    if not _is_safe_id(run_id):
        return []
    jobs_root = _jobs_root_for(token)
    if not jobs_root.is_dir():
        return []

    log_job_ids = set(_clipping_job_ids_from_run_log(token, run_id))
    time_window = _run_time_window_for_id(token, run_id)

    by_job: dict[str, list[Path]] = {}
    for entry in jobs_root.iterdir():
        if not entry.is_dir():
            continue
        report = _read_report(entry)
        if report is None:
            continue
        job_id = str(report.get("job_id") or entry.name).strip()
        if not job_id:
            continue

        matched = _report_run_id(report) == run_id or job_id in log_job_ids
        if not matched and time_window is not None:
            matched = _job_started_within_run_window(report, time_window)

        if not matched:
            continue
        by_job.setdefault(job_id, []).append(entry)

    ranked: list[tuple[str, Path]] = []
    for job_id, dirs in by_job.items():
        ranked.append((job_id, max(dirs, key=_job_dir_rank)))

    def _mtime(path: Path) -> float:
        try:
            return float(path.stat().st_mtime)
        except OSError:
            return 0.0

    ranked.sort(key=lambda item: _mtime(item[1]), reverse=True)
    job_ids = [job_id for job_id, _ in ranked]
    if job_limit is not None:
        job_ids = job_ids[: max(0, int(job_limit))]
    return job_ids


def _report_entry_for_clip_id(
    token: str,
    job_id: str,
    clip_id: str,
) -> dict[str, Any] | None:
    jobs_root = _jobs_root_for(token)
    job_dir = _find_job_dir(jobs_root, job_id)
    if job_dir is None:
        return None
    report = _read_report(job_dir)
    if report is None:
        return None
    for index, entry in enumerate(_report_clip_entries(report), start=1):
        if _report_clip_id(entry, job_id=job_id, index=index) == clip_id:
            return entry
    return None


def _report_entry_for_clip_summary(
    clip: ClipSummary,
    *,
    token: str,
    job_id: str,
) -> dict[str, Any] | None:
    """Match a ClipSummary back to its report.json clip row when ids differ."""
    entry = _report_entry_for_clip_id(token, job_id, clip.clip_id)
    if entry is not None:
        return entry

    jobs_root = _jobs_root_for(token)
    job_dir = _find_job_dir(jobs_root, job_id)
    if job_dir is None:
        return None
    report = _read_report(job_dir)
    if report is None:
        return None
    entries = _report_clip_entries(report)
    if len(entries) == 1:
        return entries[0]

    output_name = os.path.basename(str(clip.output_path or ""))
    if output_name:
        for candidate in entries:
            clip_file = os.path.basename(str(candidate.get("clip_file") or ""))
            if clip_file and clip_file == output_name:
                return candidate
    return None


def _metadata_payload_for_clip(
    clip: ClipSummary,
    *,
    token: str,
    job_id: str,
) -> dict[str, Any] | None:
    meta_ref = clip.metadata_reference
    if meta_ref is None or not meta_ref.exists or not meta_ref.path:
        return None
    jobs_root = _jobs_root_for(token)
    job_dir = _find_job_dir(jobs_root, job_id)
    if job_dir is None:
        return None
    meta_path = _resolve_under_job(job_dir, meta_ref.path, token=token, job_id=job_id)
    return _safe_read_json(meta_path) if meta_path else None


def _title_or_hook_for_clip(
    clip: ClipSummary,
    *,
    token: str,
    job_id: str,
) -> str | None:
    entry = _report_entry_for_clip_summary(clip, token=token, job_id=job_id)
    if entry is not None:
        for key in ("title", "hook"):
            value = _optional_str(entry.get(key))
            if value:
                return value
    payload = _metadata_payload_for_clip(clip, token=token, job_id=job_id)
    if payload is not None:
        for key in ("title", "hook"):
            value = _optional_str(payload.get(key))
            if value:
                return value
    return None


def _score_for_clip(
    clip: ClipSummary,
    *,
    token: str,
    job_id: str,
) -> Any | None:
    entry = _report_entry_for_clip_summary(clip, token=token, job_id=job_id)
    if entry is not None:
        score = _score_from_mapping(entry)
        if score is not None:
            return score
    payload = _metadata_payload_for_clip(clip, token=token, job_id=job_id)
    if payload is not None:
        return _score_from_mapping(payload)
    return None


def _score_from_mapping(data: dict[str, Any]) -> Any | None:
    composite = data.get("composite_score")
    if composite is not None:
        return composite
    score = data.get("score")
    if score is not None:
        return score
    scores = data.get("scores")
    if isinstance(scores, dict) and scores:
        parts = [f"{key}={scores[key]}" for key in sorted(scores.keys())[:4]]
        return ", ".join(parts)
    return None


def _enrich_run_review_clip(
    clip: ClipSummary,
    *,
    run_id: str,
    token: str,
) -> dict[str, Any]:
    job_id = clip.job_id
    clip_id = clip.clip_id
    preview_available = clip.preview_available or (
        resolve_clip_media_path(token, job_id, clip_id) is not None
    )
    payload: dict[str, Any] = {
        "clip_id": clip_id,
        "job_id": job_id,
        "run_id": run_id,
        "preview_available": preview_available,
        "media_path": (
            _OPS_MEDIA_PATH.format(job_id=job_id, clip_id=clip_id)
            if preview_available
            else None
        ),
        "duration_seconds": clip.duration_seconds,
        "funnel": clip.funnel,
        "created_at": clip.created_at,
    }
    title_or_hook = _title_or_hook_for_clip(clip, token=token, job_id=job_id)
    if title_or_hook is not None:
        payload["title_or_hook"] = title_or_hook
    score = _score_for_clip(clip, token=token, job_id=job_id)
    if score is not None:
        payload["score"] = score
    return payload


def _run_id_for_job(token: str, job_id: str) -> str:
    """Best-effort run id from a job report (empty for standalone dev/test jobs)."""
    jobs_root = _jobs_root_for(token)
    job_dir = _find_job_dir(jobs_root, job_id)
    if job_dir is None:
        return ""
    report = _read_report(job_dir)
    if report is None:
        return ""
    return str(_report_run_id(report) or "")


def list_job_ids_for_funnel(
    mk04_env_token: str,
    funnel_id: str,
    *,
    limit: int = 20,
) -> list[str]:
    """Recent job ids whose report matches funnel_id (newest jobs first)."""
    token = _env_token(mk04_env_token)
    clean_funnel = (funnel_id or "").strip()
    if not _is_safe_id(clean_funnel):
        return []

    limit = max(0, min(int(limit), 200))
    jobs_root = _jobs_root_for(token)
    job_ids: list[str] = []
    for job_dir in _list_job_dirs(jobs_root, limit=DEFAULT_JOB_LIMIT):
        report = _read_report(job_dir)
        if report is None:
            continue
        if funnel_id_from_job_report(report, job_dir=job_dir) != clean_funnel:
            continue
        job_id = str(report.get("job_id") or job_dir.name)
        if not _is_safe_id(job_id):
            continue
        job_ids.append(job_id)
        if len(job_ids) >= limit:
            break
    return job_ids


def latest_job_id_for_funnel(mk04_env_token: str, funnel_id: str) -> str | None:
    """Most recent job id for a funnel, or None when no matching jobs exist."""
    ids = list_job_ids_for_funnel(mk04_env_token, funnel_id, limit=1)
    return ids[0] if ids else None


def list_clips_for_funnel(
    mk04_env_token: str,
    funnel_id: str,
    *,
    job_limit: int = 10,
) -> list[dict[str, Any]]:
    """Clips from recent jobs for one funnel (standalone test runs included)."""
    clips: list[dict[str, Any]] = []
    for job_id in list_job_ids_for_funnel(mk04_env_token, funnel_id, limit=job_limit):
        clips.extend(list_clips_for_job(mk04_env_token, job_id))
    clips.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return clips


def list_clips_for_job(mk04_env_token: str, job_id: str) -> list[dict[str, Any]]:
    """Clips for one job id (supports legacy ``<input>_<job_id>`` job folders)."""
    token = _env_token(mk04_env_token)
    if not _is_safe_id(job_id):
        return []

    run_id = _run_id_for_job(token, job_id)
    clips: list[dict[str, Any]] = []
    for summary in _clips_for_job(token, job_id):
        clips.append(_enrich_run_review_clip(summary, run_id=run_id, token=token))
    clips.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return clips


def list_recent_output_clips(
    mk04_env_token: str,
    *,
    limit: int = DEFAULT_OUTPUT_LIMIT,
) -> list[dict[str, Any]]:
    """Recent clips across jobs when no pipeline run is selected (dev/test handoffs)."""
    token = _env_token(mk04_env_token)
    rows: list[dict[str, Any]] = []
    for summary in list_clip_summaries(token, limit=limit):
        run_id = _run_id_for_job(token, summary.job_id)
        rows.append(_enrich_run_review_clip(summary, run_id=run_id, token=token))
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return rows


def list_clips_for_run(
    mk04_env_token: str,
    run_id: str,
    *,
    job_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Clips produced by jobs tied to a single run id (exact match, no time windows)."""
    token = _env_token(mk04_env_token)
    if not _is_safe_id(run_id):
        return []

    clips: list[dict[str, Any]] = []
    for job_id in _job_ids_for_run_id(token, run_id, job_limit=job_limit):
        for summary in _clips_for_job(token, job_id):
            clips.append(_enrich_run_review_clip(summary, run_id=run_id, token=token))

    clips.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return clips


def run_clips_list_payload(
    mk04_env_token: str,
    run_id: str,
    *,
    job_limit: int | None = None,
) -> dict[str, Any]:
    """Structured payload for run-scoped clip review (future Outputs UI)."""
    token = _env_token(mk04_env_token)
    clips = list_clips_for_run(token, run_id, job_limit=job_limit)
    return {
        "environment": token,
        "run_id": run_id,
        "clips": clips,
        "count": len(clips),
        "schema_version": CONTRACT_SCHEMA_VERSION,
    }


def list_clip_summaries(
    mk04_env_token: str,
    *,
    limit: int = DEFAULT_OUTPUT_LIMIT,
    job_id: str | None = None,
    recent_run_limit: int | None = None,
) -> list[ClipSummary]:
    """Recent clips across jobs, newest jobs first."""
    token = _env_token(mk04_env_token)
    limit = max(0, min(int(limit), 200))
    if job_id:
        if not _is_safe_id(job_id):
            return []
        return _clips_for_job(token, job_id)[:limit]

    recent_windows = (
        _recent_run_windows(token, run_limit=recent_run_limit)
        if recent_run_limit
        else None
    )

    jobs_root = _jobs_root_for(token)
    clips: list[ClipSummary] = []
    seen_job_ids: set[str] = set()
    for job_dir in _list_job_dirs(jobs_root, limit=DEFAULT_JOB_LIMIT):
        report = _read_report(job_dir)
        if report is None:
            continue
        if recent_windows is not None and not _job_within_recent_runs(report, recent_windows):
            continue
        jid = str(report.get("job_id") or job_dir.name)
        if jid in seen_job_ids:
            continue
        seen_job_ids.add(jid)
        clips.extend(_clips_for_job(token, jid))
        if len(clips) >= limit:
            break
    clips.sort(key=lambda c: c.created_at or "", reverse=True)
    return clips[:limit]


def get_clip_summary(
    mk04_env_token: str,
    job_id: str,
    clip_id: str,
) -> ClipSummary | None:
    if not _is_safe_id(job_id) or not _is_safe_id(clip_id):
        return None
    for clip in _clips_for_job(mk04_env_token, job_id):
        if clip.clip_id == clip_id:
            return clip
    return None


def get_clip_detail(
    mk04_env_token: str,
    job_id: str,
    clip_id: str,
) -> dict[str, Any] | None:
    """Structured clip detail for Output Browser detail view."""
    clip = get_clip_summary(mk04_env_token, job_id, clip_id)
    if clip is None:
        return None

    token = _env_token(mk04_env_token)
    jobs_root = _jobs_root_for(token)
    job_dir = _find_job_dir(jobs_root, job_id)
    metadata_summary: dict[str, Any] = {"available": False, "detail": _NOT_AVAILABLE}
    validation_summary: dict[str, Any] = {
        "state": clip.validation_state or _NOT_AVAILABLE,
        "detail": _NOT_AVAILABLE,
    }
    module_results: list[Any] = []
    related_reports: list[dict[str, Any]] = []
    reframe_summary: dict[str, Any] = (
        dict(clip.reframe_summary)
        if isinstance(clip.reframe_summary, dict)
        else {"available": False}
    )

    if job_dir is not None and clip.metadata_reference and clip.metadata_reference.exists:
        meta_path = _resolve_under_job(
            job_dir,
            clip.metadata_reference.path,
            token=token,
            job_id=job_id,
        )
        payload = _safe_read_json(meta_path) if meta_path else None
        if payload:
            metadata_summary = {
                "available": True,
                "path": clip.metadata_reference.path,
                "title": payload.get("title") or payload.get("hook") or _NOT_AVAILABLE,
                "candidate_id": payload.get("candidate_id")
                or payload.get("source_candidate")
                or clip.source_candidate
                or _NOT_AVAILABLE,
                "duration_sec": payload.get("duration_sec")
                or payload.get("duration_seconds")
                or clip.duration_seconds
                or _NOT_AVAILABLE,
                "validation_result": payload.get("validation_result")
                or payload.get("validation_state")
                or clip.validation_state,
                "warnings": payload.get("warnings") or clip.warnings,
            }
            validation_summary = {
                "state": metadata_summary["validation_result"],
                "detail": payload.get("failure_reason")
                or payload.get("validation_detail")
                or _NOT_AVAILABLE,
            }
            modules = payload.get("module_results") or payload.get("modules_applied")
            if isinstance(modules, list):
                module_results = modules
            reframe_summary = extract_reframe_summary_from_metadata_payload(payload)
        else:
            reframe_summary = {"available": False}
    else:
        reframe_summary = {"available": False}

    artifacts_payload = resolve_job_artifacts(token, job_id) or {}
    for item in artifacts_payload.get("artifacts") or []:
        if not isinstance(item, dict):
            continue
        if item.get("artifact_type") in {
            "processing_report",
            "post_processing_report",
            "selection_result",
            "raw_candidate_pool",
        }:
            related_reports.append(
                {
                    "report_type": item.get("artifact_type"),
                    "exists": bool(item.get("exists")),
                    "path": item.get("path") or _NOT_AVAILABLE,
                }
            )

    preview_available = clip.preview_available or (
        resolve_clip_media_path(mk04_env_token, job_id, clip_id) is not None
    )
    clip_payload = clip.to_dict()
    clip_payload["preview_available"] = preview_available

    return {
        "clip": clip_payload,
        "job_id": job_id,
        "environment": token,
        "metadata_summary": metadata_summary,
        "validation_summary": validation_summary,
        "module_results": module_results,
        "reframe_summary": reframe_summary,
        "related_reports": related_reports,
        "related_job_path": f"/ops/jobs/{job_id}",
        "media_path": (
            _OPS_MEDIA_PATH.format(job_id=job_id, clip_id=clip_id)
            if preview_available
            else None
        ),
        "schema_version": CONTRACT_SCHEMA_VERSION,
    }


def outputs_list_payload(
    mk04_env_token: str,
    *,
    limit: int = DEFAULT_OUTPUT_LIMIT,
    job_id: str | None = None,
    recent_run_limit: int | None = None,
) -> dict[str, Any]:
    token = _env_token(mk04_env_token)
    clips = list_clip_summaries(
        token,
        limit=limit,
        job_id=job_id,
        recent_run_limit=recent_run_limit,
    )
    return {
        "environment": token,
        "outputs": [clip.to_dict() for clip in clips],
        "count": len(clips),
        "job_id": job_id,
        "recent_run_limit": recent_run_limit,
        "schema_version": CONTRACT_SCHEMA_VERSION,
    }


def _resolve_media_path_for_report_clip(
    mk04_env_token: str,
    job_id: str,
    clip_id: str,
) -> Path | None:
    token = _env_token(mk04_env_token)
    jobs_root = _jobs_root_for(token)
    job_dir = _find_job_dir(jobs_root, job_id)
    if job_dir is None:
        return None
    report = _read_report(job_dir)
    if report is None:
        return None
    allowed_roots = _media_roots_for(token)
    for index, entry in enumerate(_report_clip_entries(report), start=1):
        entry_id = _report_clip_id(entry, job_id=job_id, index=index)
        if entry_id != clip_id:
            continue
        return _resolve_report_clip_file(
            job_dir,
            entry,
            token=token,
            job_id=job_id,
            allowed_roots=allowed_roots,
        )
    return None


def resolve_clip_media_path(
    mk04_env_token: str,
    job_id: str,
    clip_id: str,
) -> Path | None:
    """Return a safe on-disk path for preview streaming, or None."""
    if not _is_safe_id(job_id) or not _is_safe_id(clip_id):
        return None

    clip = get_clip_summary(mk04_env_token, job_id, clip_id)
    if clip is not None and clip.preview_available and clip.output_path:
        token = _env_token(mk04_env_token)
        jobs_root = _jobs_root_for(token)
        job_dir = _find_job_dir(jobs_root, job_id)
        if job_dir is not None:
            path = _resolve_under_job(job_dir, clip.output_path, token=token, job_id=job_id)
            allowed_roots = _media_roots_for(token)
            if path is not None and _is_allowed_media_path(path, allowed_roots):
                return path

    return _resolve_media_path_for_report_clip(mk04_env_token, job_id, clip_id)


def resolve_clip_media_by_filename(
    mk04_env_token: str,
    job_id: str,
    clip_file: str,
) -> Path | None:
    """Resolve a clip preview path from a report clip filename."""
    if not _is_safe_id(job_id):
        return None
    safe_name = os.path.basename(str(clip_file or ""))
    if not safe_name or safe_name != str(clip_file or ""):
        return None

    token = _env_token(mk04_env_token)
    jobs_root = _jobs_root_for(token)
    job_dir = _find_job_dir(jobs_root, job_id)
    if job_dir is None:
        return None
    report = _read_report(job_dir)
    if report is None:
        return None
    allowed_roots = _media_roots_for(token)
    for entry in _report_clip_entries(report):
        entry_file = os.path.basename(str(entry.get("clip_file") or ""))
        if entry_file != safe_name:
            continue
        return _resolve_report_clip_file(
            job_dir,
            entry,
            token=token,
            job_id=job_id,
            allowed_roots=allowed_roots,
        )
    candidate = job_dir / "clips" / safe_name
    if _is_allowed_media_path(candidate, allowed_roots):
        return candidate.resolve()
    return None


def report_clip_filename(
    mk04_env_token: str,
    job_id: str,
    clip_id: str,
) -> str | None:
    """Return the served clip filename for proxy fallback, if known."""
    token = _env_token(mk04_env_token)
    jobs_root = _jobs_root_for(token)
    job_dir = _find_job_dir(jobs_root, job_id)
    if job_dir is None:
        return None
    report = _read_report(job_dir)
    if report is None:
        return None
    for index, entry in enumerate(_report_clip_entries(report), start=1):
        entry_id = _report_clip_id(entry, job_id=job_id, index=index)
        if entry_id != clip_id:
            continue
        clip_file = os.path.basename(str(entry.get("clip_file") or ""))
        return clip_file or None
    return None
