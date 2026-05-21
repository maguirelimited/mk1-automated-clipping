from __future__ import annotations

from typing import Any

from .adapters.base import PlatformAdapter
from .adapters.youtube import YouTubeAdapter
from .config import load_channel_profiles, load_settings
from .models import PublishResult, UploadStatus
from .store import OutputStore
from .time_utils import now_iso


def default_adapters() -> dict[str, PlatformAdapter]:
    return {"youtube_shorts": YouTubeAdapter()}


def publish_due_jobs(
    store: OutputStore,
    *,
    profiles: list[dict[str, Any]] | None = None,
    adapters: dict[str, PlatformAdapter] | None = None,
    limit: int = 10,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    if max_attempts is None:
        settings = load_settings()
        publisher_cfg = settings.get("publisher") if isinstance(settings.get("publisher"), dict) else {}
        max_attempts = int(publisher_cfg.get("max_attempts") or 3)
    active_profiles = profiles if profiles is not None else load_channel_profiles()
    active_adapters = adapters if adapters is not None else default_adapters()

    claimed = store.claim_due_jobs(now=now_iso(), limit=limit)
    results: list[dict[str, Any]] = []
    for job in claimed:
        result = publish_one_job(
            store,
            int(job["id"]),
            profiles=active_profiles,
            adapters=active_adapters,
            max_attempts=max_attempts,
        )
        results.append(result)
    return {"count": len(results), "results": results}


def publish_one_job(
    store: OutputStore,
    upload_job_id: int,
    *,
    profiles: list[dict[str, Any]],
    adapters: dict[str, PlatformAdapter],
    max_attempts: int = 3,
) -> dict[str, Any]:
    upload_job = store.get_upload_job(upload_job_id)
    if upload_job is None:
        return {"upload_job_id": upload_job_id, "published": False, "reason": "upload_job_not_found"}
    source_clip = store.get_source_clip(int(upload_job["clip_pk"]))
    if source_clip is None:
        return {"upload_job_id": upload_job_id, "published": False, "reason": "source_clip_not_found"}
    profile = _profile_by_channel(profiles, str(upload_job.get("channel_id") or ""))
    if profile is None:
        _fail(store, upload_job_id, "profile_not_found", retryable=False)
        return {"upload_job_id": upload_job_id, "published": False, "reason": "profile_not_found"}
    adapter = adapters.get(str(upload_job.get("platform") or ""))
    if adapter is None:
        _fail(store, upload_job_id, "adapter_not_found", retryable=False)
        return {"upload_job_id": upload_job_id, "published": False, "reason": "adapter_not_found"}

    result = adapter.publish(upload_job, source_clip, profile)
    _record_publish_result(store, upload_job_id, upload_job, result, max_attempts=max_attempts)
    return {
        "upload_job_id": upload_job_id,
        "published": result.ok,
        "status": result.status,
        "platform_asset_id": result.platform_asset_id,
        "error_category": result.error_category,
        "error_message": result.error_message,
    }


def _record_publish_result(
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
            "scheduled_at": upload_job.get("scheduled_at"),
        },
        response_summary=result.response,
        error_category=result.error_category,
        error_message=result.error_message,
        retryable=result.retryable,
    )
    if result.ok:
        store.update_upload_job(
            upload_job_id,
            status=result.status,
            platform_asset_id=result.platform_asset_id,
            last_error=None,
        )
        return
    next_attempt_count = int(upload_job.get("attempt_count") or 0) + 1
    status = (
        UploadStatus.FAILED_RETRYABLE
        if result.retryable and next_attempt_count < max_attempts
        else UploadStatus.FAILED_TERMINAL
    )
    store.update_upload_job(upload_job_id, status=status, last_error=result.error_message)


def _fail(store: OutputStore, upload_job_id: int, reason: str, *, retryable: bool) -> None:
    status = UploadStatus.FAILED_RETRYABLE if retryable else UploadStatus.FAILED_TERMINAL
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
