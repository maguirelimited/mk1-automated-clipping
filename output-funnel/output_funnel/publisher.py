from __future__ import annotations

import logging
from typing import Any

from .adapters.base import PlatformAdapter
from .adapters.youtube import YouTubeAdapter
from .config import load_channel_profiles, load_settings
from .models import PublishResult, UploadStatus
from .store import OutputStore
from .time_utils import now_iso, now_utc, parse_iso_datetime

log = logging.getLogger("output_funnel.uploader")


def default_adapters() -> dict[str, PlatformAdapter]:
    return {"youtube_shorts": YouTubeAdapter()}


def upload_due_jobs(
    store: OutputStore,
    *,
    profiles: list[dict[str, Any]] | None = None,
    adapters: dict[str, PlatformAdapter] | None = None,
    limit: int = 10,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Find jobs whose upload window has opened and upload them to the platform.

    Order of operations per tick:
      1. Mark anything past `upload_deadline` as `missed_upload_window` so it
         is not uploaded late as if it were a fresh post.
      2. Claim jobs where `upload_at <= now` (transitioning to `uploading`).
      3. Run the platform adapter, which for YouTube uploads as `private` with
         `publishAt` set to the planned `publish_at` so YouTube releases the
         video at that time.
    """
    if max_attempts is None:
        settings = load_settings()
        publisher_cfg = settings.get("publisher") if isinstance(settings.get("publisher"), dict) else {}
        max_attempts = int(publisher_cfg.get("max_attempts") or 3)
    active_profiles = profiles if profiles is not None else load_channel_profiles()
    active_adapters = adapters if adapters is not None else default_adapters()

    now_str = now_iso()
    overdue = store.list_overdue_uploads(now=now_str, limit=max(1, int(limit)) * 2)
    missed_results: list[dict[str, Any]] = []
    for job in overdue:
        store.mark_missed_upload_window(int(job["id"]))
        missed_results.append(
            {
                "upload_job_id": int(job["id"]),
                "uploaded": False,
                "reason": "missed_upload_window",
                "publish_at": job.get("publish_at"),
                "upload_deadline": job.get("upload_deadline"),
            }
        )

    claimed = store.claim_upload_due_jobs(now=now_str, limit=limit)
    results: list[dict[str, Any]] = list(missed_results)
    for job in claimed:
        result = upload_one_job(
            store,
            int(job["id"]),
            profiles=active_profiles,
            adapters=active_adapters,
            max_attempts=max_attempts,
        )
        results.append(result)
    return {
        "count": len(results),
        "uploaded": sum(1 for r in results if r.get("uploaded")),
        "missed": len(missed_results),
        "results": results,
    }


def upload_one_job(
    store: OutputStore,
    upload_job_id: int,
    *,
    profiles: list[dict[str, Any]],
    adapters: dict[str, PlatformAdapter],
    max_attempts: int = 3,
) -> dict[str, Any]:
    upload_job = store.get_upload_job(upload_job_id)
    if upload_job is None:
        return {"upload_job_id": upload_job_id, "uploaded": False, "reason": "upload_job_not_found"}

    deadline = parse_iso_datetime(str(upload_job.get("upload_deadline") or ""))
    if deadline is not None and deadline <= now_utc():
        store.mark_missed_upload_window(upload_job_id)
        return {
            "upload_job_id": upload_job_id,
            "uploaded": False,
            "reason": "missed_upload_window",
            "upload_deadline": upload_job.get("upload_deadline"),
        }

    source_clip = store.get_source_clip(int(upload_job["clip_pk"]))
    if source_clip is None:
        return {"upload_job_id": upload_job_id, "uploaded": False, "reason": "source_clip_not_found"}
    profile = _profile_by_channel(profiles, str(upload_job.get("channel_id") or ""))
    if profile is None:
        _fail(store, upload_job_id, "profile_not_found", retryable=False)
        return {"upload_job_id": upload_job_id, "uploaded": False, "reason": "profile_not_found"}
    adapter = adapters.get(str(upload_job.get("platform") or ""))
    if adapter is None:
        _fail(store, upload_job_id, "adapter_not_found", retryable=False)
        return {"upload_job_id": upload_job_id, "uploaded": False, "reason": "adapter_not_found"}

    result = adapter.publish(upload_job, source_clip, profile)
    _record_upload_result(store, upload_job_id, upload_job, result, max_attempts=max_attempts)
    return {
        "upload_job_id": upload_job_id,
        "uploaded": result.ok,
        "status": result.status,
        "platform_video_id": result.platform_asset_id,
        "platform_asset_id": result.platform_asset_id,
        "publish_at": upload_job.get("publish_at") or upload_job.get("platform_publish_at"),
        "error_category": result.error_category,
        "error_message": result.error_message,
    }


def _record_upload_result(
    store: OutputStore,
    upload_job_id: int,
    upload_job: dict[str, Any],
    result: PublishResult,
    *,
    max_attempts: int,
) -> None:
    store.record_attempt(
        upload_job_id,
        status=result.status,
        request_summary={
            "platform": upload_job.get("platform"),
            "channel_id": upload_job.get("channel_id"),
            "publish_at": upload_job.get("publish_at") or upload_job.get("platform_publish_at"),
            "upload_at": upload_job.get("upload_at"),
            "upload_deadline": upload_job.get("upload_deadline"),
        },
        response_summary=result.response,
        error_category=result.error_category,
        error_message=result.error_message,
        retryable=result.retryable,
    )
    if result.ok:
        store.set_uploaded_scheduled(
            upload_job_id,
            platform_video_id=result.platform_asset_id,
        )
        return
    next_attempt_count = int(upload_job.get("attempt_count") or 0) + 1
    if result.retryable and next_attempt_count < max_attempts:
        status = UploadStatus.FAILED_RETRYABLE
    else:
        status = UploadStatus.FAILED_UPLOAD
    store.update_upload_job(upload_job_id, status=status, last_error=result.error_message)


def _fail(store: OutputStore, upload_job_id: int, reason: str, *, retryable: bool) -> None:
    status = UploadStatus.FAILED_RETRYABLE if retryable else UploadStatus.FAILED_UPLOAD
    store.update_upload_job(upload_job_id, status=status, last_error=reason)
    store.record_attempt(
        upload_job_id,
        status=status,
        error_category=reason,
        error_message=reason,
        retryable=retryable,
    )


def _profile_by_channel(profiles: list[dict[str, Any]], channel_id: str) -> dict[str, Any] | None:
    for profile in profiles:
        if str(profile.get("channel_id") or "") == channel_id:
            return profile
    return None


def publish_due_jobs(
    store: OutputStore,
    *,
    profiles: list[dict[str, Any]] | None = None,
    adapters: dict[str, PlatformAdapter] | None = None,
    limit: int = 10,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Deprecated alias for `upload_due_jobs`.

    Older callers POSTing to `/queue/publish-due` or using the
    `publish-due` CLI continue to work, but the semantics are now
    "upload due" — we upload before the planned public publish time and
    let the platform schedule the public release.
    """
    log.warning(
        "publish_due_jobs is deprecated; routing to upload_due_jobs. "
        "Use /queue/upload-due or `output-funnel upload-due` going forward."
    )
    return upload_due_jobs(
        store,
        profiles=profiles,
        adapters=adapters,
        limit=limit,
        max_attempts=max_attempts,
    )


def publish_one_job(
    store: OutputStore,
    upload_job_id: int,
    *,
    profiles: list[dict[str, Any]],
    adapters: dict[str, PlatformAdapter],
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Deprecated alias for `upload_one_job`."""
    result = upload_one_job(
        store,
        upload_job_id,
        profiles=profiles,
        adapters=adapters,
        max_attempts=max_attempts,
    )
    result["published"] = result.get("uploaded", False)
    return result
