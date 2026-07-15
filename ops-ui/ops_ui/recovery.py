from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import Settings
from .control_export import read_controls_file
from .http_client import call_json

FAILED_UPLOAD_STATUSES = frozenset(
    {
        "failed_upload",
        "failed_retryable",
        "failed_terminal",
        "missed_upload_window",
    }
)
DEAD_LETTER_STATUSES = frozenset({"failed_terminal", "missed_upload_window"})
RETRYABLE_UPLOAD_STATUSES = frozenset(
    {"failed_upload", "failed_retryable", "failed_terminal"}
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


def _age_seconds(value: Any, *, now: datetime | None = None) -> float | None:
    ts = _parse_iso(value)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current - ts).total_seconds())


def is_failed_video(job: dict[str, Any]) -> bool:
    status = str(job.get("status") or "").lower()
    return status == "failed" or int(job.get("error_count") or 0) > 0


def is_failed_upload(job: dict[str, Any]) -> bool:
    status = str(job.get("status") or "").lower()
    return status in FAILED_UPLOAD_STATUSES or bool(job.get("last_error"))


def is_dead_letter_upload(job: dict[str, Any]) -> bool:
    return str(job.get("status") or "").lower() in DEAD_LETTER_STATUSES


def can_retry_upload(job: dict[str, Any]) -> bool:
    return str(job.get("status") or "").lower() in RETRYABLE_UPLOAD_STATUSES


def build_video_failure(
    job: dict[str, Any],
    *,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    errors: list[Any] = []
    if detail:
        errors = detail.get("errors") if isinstance(detail.get("errors"), list) else []
    reason = ""
    if errors:
        first = errors[0]
        reason = str(first) if isinstance(first, str) else str(first.get("message") or first)
    row = {
        "service": "video-automation",
        "id": job.get("job_id"),
        "status": job.get("status"),
        "stage": job.get("current_stage") or "unknown",
        "reason": reason or f"{job.get('error_count') or 0} error(s) recorded",
        "retry_count": int(job.get("error_count") or 0),
        "failed_at": job.get("completed_at") or job.get("created_at"),
        "created_at": job.get("created_at"),
        "input_id": job.get("input_id"),
        "source": job.get("input_video_name") or job.get("source_video"),
        "can_rerun": bool(job.get("input_id")),
    }
    if detail and not row["input_id"]:
        job_block = detail.get("job") if isinstance(detail.get("job"), dict) else {}
        row["input_id"] = job_block.get("input_id")
        row["can_rerun"] = bool(row["input_id"])
    return row


def build_upload_failure(job: dict[str, Any]) -> dict[str, Any]:
    status = str(job.get("status") or "").lower()
    return {
        "service": "output-funnel",
        "id": job.get("id"),
        "status": status,
        "stage": status,
        "reason": job.get("last_error") or job.get("source_title") or "",
        "retry_count": int(job.get("attempt_count") or 0),
        "failed_at": job.get("updated_at") or job.get("created_at"),
        "created_at": job.get("created_at"),
        "platform": job.get("platform"),
        "channel_id": job.get("channel_id"),
        "title": job.get("normalized_title") or job.get("source_title") or job.get("clip_id"),
        "can_retry_upload": can_retry_upload(job),
        "is_dead_letter": is_dead_letter_upload(job),
    }


def fetch_video_detail(settings: Settings, job_id: str) -> dict[str, Any] | None:
    svc = next((s for s in settings.services if s.key == "video-automation"), None)
    if svc is None:
        return None
    ok, payload, _status = call_json(
        svc,
        f"/jobs/{job_id}/debug",
        timeout=settings.service_timeout_sec,
    )
    return payload if ok else None


def collect_failed_jobs(
    video_jobs: list[dict[str, Any]],
    upload_jobs: list[dict[str, Any]],
    *,
    settings: Settings,
    enrich_video: bool = True,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for job in video_jobs:
        if not is_failed_video(job):
            continue
        detail = fetch_video_detail(settings, str(job.get("job_id") or "")) if enrich_video else None
        rows.append(build_video_failure(job, detail=detail))
    for job in upload_jobs:
        if not is_failed_upload(job):
            continue
        rows.append(build_upload_failure(job))
    rows.sort(key=lambda item: str(item.get("failed_at") or ""), reverse=True)
    return rows


def collect_dead_letter(upload_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [build_upload_failure(job) for job in upload_jobs if is_dead_letter_upload(job)]
    rows.sort(key=lambda item: str(item.get("failed_at") or ""), reverse=True)
    return rows


def detect_stuck_video_jobs(
    jobs: list[dict[str, Any]],
    *,
    running_threshold_sec: float,
    queued_threshold_sec: float,
) -> list[dict[str, Any]]:
    stuck: list[dict[str, Any]] = []
    for job in jobs:
        status = str(job.get("status") or "").lower()
        if status == "running":
            age = _age_seconds(
                job.get("heartbeat_at") or job.get("started_at") or job.get("created_at")
            )
            threshold = running_threshold_sec
        elif status == "queued":
            age = _age_seconds(job.get("created_at"))
            threshold = queued_threshold_sec
        else:
            continue
        if age is None or age < threshold:
            continue
        stuck.append(
            {
                "service": "video-automation",
                "id": job.get("job_id"),
                "status": status,
                "stage": job.get("current_stage"),
                "age_seconds": int(age),
                "since": job.get("heartbeat_at")
                or job.get("started_at")
                or job.get("created_at"),
                "detail": f"possibly stuck — no status change for {int(age // 60)}+ min (heuristic)",
                "can_cancel": True,
            }
        )
    return stuck


def detect_stuck_upload_jobs(
    jobs: list[dict[str, Any]],
    *,
    uploading_threshold_sec: float,
) -> list[dict[str, Any]]:
    stuck: list[dict[str, Any]] = []
    for job in jobs:
        status = str(job.get("status") or "").lower()
        if status != "uploading":
            continue
        age = _age_seconds(job.get("updated_at") or job.get("created_at"))
        if age is None or age < uploading_threshold_sec:
            continue
        stuck.append(
            {
                "service": "output-funnel",
                "id": job.get("id"),
                "status": status,
                "stage": status,
                "age_seconds": int(age),
                "since": job.get("updated_at") or job.get("created_at"),
                "detail": job.get("last_error") or f"possibly stuck — uploading for {int(age // 60)}+ min",
            }
        )
    return stuck


def persisted_queue_jobs(video_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Jobs that were queued before restart and are still waiting."""
    recovered: list[dict[str, Any]] = []
    for job in video_jobs:
        status = str(job.get("status") or "").lower()
        if status != "queued":
            continue
        age = _age_seconds(job.get("created_at"))
        if age is None or age < 120:
            continue
        recovered.append(
            {
                "service": "video-automation",
                "id": job.get("job_id"),
                "status": status,
                "stage": job.get("current_stage") or "queued",
                "since": job.get("created_at"),
                "detail": "persisted on disk; worker should pick this up after restart",
            }
        )
    return recovered


def build_recovery_status(
    *,
    settings: Settings,
    video_jobs: list[dict[str, Any]],
    upload_jobs: list[dict[str, Any]],
    controls_file_exists: bool,
    controls_file: dict[str, Any],
    ui_controls: dict[str, bool],
) -> dict[str, Any]:
    output_svc = next((s for s in settings.services if s.key == "output-funnel"), None)
    db_path = ""
    worker_note = "unknown"
    if output_svc is not None:
        ok, health, _code = call_json(output_svc, "/healthz", timeout=settings.service_timeout_sec)
        if ok:
            db_path = str(health.get("database_path") or "")
    stuck_video = detect_stuck_video_jobs(
        video_jobs,
        running_threshold_sec=settings.stuck_running_sec,
        queued_threshold_sec=settings.stuck_queued_sec,
    )
    stuck_upload = detect_stuck_upload_jobs(
        upload_jobs,
        uploading_threshold_sec=settings.stuck_uploading_sec,
    )
    return {
        "controls_file": str(settings.controls_file),
        "controls_file_exists": controls_file_exists,
        "controls_file_values": controls_file,
        "ui_controls": ui_controls,
        "ingestion_enforced": bool(controls_file.get("ingestion_paused")),
        "uploads_enforced": bool(controls_file.get("uploads_paused")),
        "database_path": db_path,
        "upload_worker_note": worker_note,
        "persisted_queued": persisted_queue_jobs(video_jobs),
        "stuck_jobs": stuck_video + stuck_upload,
        "dead_letter_count": sum(1 for job in upload_jobs if is_dead_letter_upload(job)),
        "auto_terminal_note": (
            "output-funnel marks uploads failed_terminal after publisher max_attempts "
            "(see publisher.max_attempts in output-funnel settings)."
        ),
        "stuck_running_sec": settings.stuck_running_sec,
        "stuck_queued_sec": settings.stuck_queued_sec,
        "stuck_uploading_sec": settings.stuck_uploading_sec,
    }
