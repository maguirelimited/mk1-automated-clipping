from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

FAILED_UPLOAD_STATUSES = frozenset(
    {
        "failed_upload",
        "failed_retryable",
        "failed_terminal",
        "missed_upload_window",
    }
)

BACKLOG_STATUSES = frozenset(
    {
        "registered",
        "routed",
        "planned",
        "pending_upload",
        "uploading",
    }
)

CANCELLABLE_STATUSES = frozenset(
    {
        "registered",
        "routed",
        "planned",
        "pending_upload",
        "failed_retryable",
        "failed_upload",
        "failed_terminal",
    }
)

RESCHEDULABLE_STATUSES = frozenset(
    {
        "planned",
        "pending_upload",
        "failed_retryable",
        "failed_upload",
        "failed_terminal",
        "routed",
    }
)

MANUAL_UPLOAD_STATUSES = frozenset(
    {
        "planned",
        "pending_upload",
    }
)


def _parse_iso(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _duration_label(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def upload_latency(job: dict[str, Any]) -> dict[str, Any]:
    """Derive upload timing visibility from job timestamps."""
    started = _parse_iso(job.get("upload_started_at"))
    finished = _parse_iso(job.get("uploaded_at"))
    upload_at = _parse_iso(job.get("upload_at"))
    now = datetime.now(timezone.utc)
    upload_seconds = None
    if started and finished:
        upload_seconds = max(0.0, (finished - started).total_seconds())
    elif started:
        upload_seconds = max(0.0, (now - started).total_seconds())
    wait_seconds = None
    if upload_at and started:
        wait_seconds = max(0.0, (started - upload_at).total_seconds())
    return {
        "upload_duration_label": _duration_label(upload_seconds),
        "upload_duration_sec": upload_seconds,
        "queue_wait_label": _duration_label(wait_seconds),
        "queue_wait_sec": wait_seconds,
        "upload_started_at": job.get("upload_started_at"),
        "uploaded_at": job.get("uploaded_at"),
    }


def publish_confirmation(job: dict[str, Any]) -> str:
    status = str(job.get("status") or "").lower()
    platform_state = str(job.get("platform_state") or "").strip()
    asset = job.get("platform_video_id") or job.get("platform_asset_id")
    if status == "published":
        return "published"
    if status == "uploaded_scheduled" and asset:
        if platform_state:
            return f"scheduled on platform ({platform_state})"
        return "uploaded; awaiting platform publish time"
    if status == "uploading":
        return "upload in progress"
    if status in FAILED_UPLOAD_STATUSES:
        return "not confirmed"
    if status in {"planned", "pending_upload"}:
        return "not yet uploaded"
    return status or "unknown"


def filter_upload_jobs(
    jobs: list[dict[str, Any]],
    *,
    status: str = "",
    platform: str = "",
    channel: str = "",
    q: str = "",
) -> list[dict[str, Any]]:
    status_key = status.strip().lower()
    platform_key = platform.strip().lower()
    channel_key = channel.strip().lower()
    query = q.strip().lower()
    out: list[dict[str, Any]] = []
    for job in jobs:
        job_status = str(job.get("status") or "").lower()
        if status_key and job_status != status_key:
            continue
        job_platform = str(job.get("platform") or "").lower()
        if platform_key and platform_key not in job_platform:
            continue
        job_channel = str(job.get("channel_id") or "").lower()
        if channel_key and channel_key not in job_channel:
            continue
        if query:
            haystack = " ".join(
                str(part or "")
                for part in (
                    job.get("id"),
                    job.get("normalized_title"),
                    job.get("source_title"),
                    job.get("clip_id"),
                    job.get("last_error"),
                    job.get("platform_asset_id"),
                    job.get("platform_video_id"),
                )
            ).lower()
            if query not in haystack:
                continue
        out.append(job)
    return out


def queue_stats(jobs: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_uploaded = 0
    failed = 0
    backlog = 0
    in_flight = 0
    for job in jobs:
        status = str(job.get("status") or "").lower()
        if status in FAILED_UPLOAD_STATUSES:
            failed += 1
        if status in BACKLOG_STATUSES:
            backlog += 1
        if status == "uploading":
            in_flight += 1
        uploaded_at = _parse_iso(job.get("uploaded_at"))
        if uploaded_at and uploaded_at >= day_start:
            daily_uploaded += 1
    window_hours = 24
    window_start = current - timedelta(hours=window_hours)
    recent_uploads = 0
    for job in jobs:
        uploaded_at = _parse_iso(job.get("uploaded_at"))
        if uploaded_at and uploaded_at >= window_start:
            recent_uploads += 1
    rate_per_hour = round(recent_uploads / window_hours, 2) if window_hours else 0.0
    return {
        "daily_uploads": daily_uploaded,
        "failed_uploads": failed,
        "backlog": backlog,
        "in_flight": in_flight,
        "total_visible": len(jobs),
        "uploads_last_24h": recent_uploads,
        "upload_rate_per_hour": rate_per_hour,
    }


def distinct_filter_values(jobs: list[dict[str, Any]]) -> dict[str, list[str]]:
    statuses: set[str] = set()
    platforms: set[str] = set()
    channels: set[str] = set()
    for job in jobs:
        if job.get("status"):
            statuses.add(str(job["status"]))
        if job.get("platform"):
            platforms.add(str(job["platform"]))
        if job.get("channel_id"):
            channels.add(str(job["channel_id"]))
    return {
        "statuses": sorted(statuses),
        "platforms": sorted(platforms),
        "channels": sorted(channels),
    }


def enrich_upload_row(job: dict[str, Any]) -> dict[str, Any]:
    latency = upload_latency(job)
    return {
        **job,
        "latency": latency,
        "publish_confirmation": publish_confirmation(job),
        "platform_video_id": job.get("platform_video_id") or job.get("platform_asset_id"),
        "can_cancel": str(job.get("status") or "").lower() in CANCELLABLE_STATUSES,
        "can_reschedule": str(job.get("status") or "").lower() in RESCHEDULABLE_STATUSES,
        "can_manual_upload": str(job.get("status") or "").lower() in MANUAL_UPLOAD_STATUSES,
    }
