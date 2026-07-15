from __future__ import annotations

import logging
import os
import time
from dataclasses import replace
from typing import Any

from .adapters.base import PlatformAdapter
from .adapters.common import token_health
from .adapters.facebook_reels import FacebookReelsAdapter
from .adapters.instagram_reels import InstagramReelsAdapter
from .adapters.x import XAdapter
from .adapters.youtube import YouTubeAdapter
from .config import load_channel_profiles, load_settings, runtime_environment, upload_mode
from .models import FailureClass, PublishResult, PublishState, UploadStatus
from .runtime_upload_control import upload_block_reason
from .store import OutputStore
from .time_utils import now_iso, now_utc, parse_iso_datetime
from .upload_authority import assert_real_upload_permitted

log = logging.getLogger("output_funnel.uploader")


def default_adapters() -> dict[str, PlatformAdapter]:
    return {
        "youtube_shorts": YouTubeAdapter(),
        "x": XAdapter(),
        "facebook_reels": FacebookReelsAdapter(),
        "instagram_reels": InstagramReelsAdapter(),
    }


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
    if max_attempts is None and profiles is None:
        settings = load_settings()
        publisher_cfg = settings.get("publisher") if isinstance(settings.get("publisher"), dict) else {}
        max_attempts = int(publisher_cfg.get("max_attempts") or 3)
    elif max_attempts is None:
        max_attempts = 3
    upload_mode()
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

    lease_owner = f"pid:{os.getpid()}"
    publisher_cfg = {}
    if max_attempts is not None:
        try:
            settings_for_lease = load_settings()
            publisher_cfg = settings_for_lease.get("publisher") if isinstance(settings_for_lease.get("publisher"), dict) else {}
        except Exception:
            publisher_cfg = {}
    lease_seconds = int(publisher_cfg.get("lease_seconds") or 1800)
    if upload_mode() == "real":
        block = upload_block_reason()
        if block:
            return {
                "count": 0,
                "uploaded": 0,
                "missed": len(missed_results),
                "results": list(missed_results),
                "skipped": True,
                "reason": block,
            }
    claimed = store.claim_upload_due_jobs(now=now_str, limit=limit, lease_owner=lease_owner, lease_seconds=lease_seconds)
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
    adapters: dict[str, PlatformAdapter] | None = None,
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
    mode = upload_mode()
    account_block = _account_block_reason(store, upload_job)
    if account_block:
        _defer_account_blocked(store, upload_job_id, upload_job, account_block)
        return {
            "upload_job_id": upload_job_id,
            "uploaded": False,
            "reason": account_block["reason"],
            "retry_at": account_block.get("retry_at"),
        }
    if mode == "real":
        block = upload_block_reason()
        if block:
            _release_upload_blocked(store, upload_job_id, upload_job, block)
            return {
                "upload_job_id": upload_job_id,
                "uploaded": False,
                "reason": block,
                "upload_mode": mode,
                "environment": runtime_environment(),
            }
    if mode == "dry_run":
        result = _dry_run_result(upload_job)
    else:
        active_adapters = adapters if adapters is not None else default_adapters()
        adapter = active_adapters.get(str(upload_job.get("platform") or ""))
        if adapter is None:
            _fail(store, upload_job_id, "adapter_not_found", retryable=False)
            return {"upload_job_id": upload_job_id, "uploaded": False, "reason": "adapter_not_found"}
        health = token_health(profile)
        store.upsert_account_state(
            platform=str(upload_job.get("platform") or ""),
            channel_id=str(upload_job.get("channel_id") or ""),
            token_source=health.token_source,
            token_expires_at=health.expires_at,
            token_last_checked_at=now_iso(),
            token_last_refresh_error=health.error_message if not health.ok else "",
        )
        if not health.ok:
            result = PublishResult(
                ok=False,
                status=UploadStatus.FAILED_TERMINAL,
                failure_class=health.failure_class,
                error_category="token_health_failed",
                error_message=health.error_message,
                retryable=False,
            )
        else:
            start = time.monotonic()
            try:
                # Final gate immediately before any platform API call.
                assert_real_upload_permitted()
                reconciled = adapter.reconcile(upload_job, source_clip, profile)
                if reconciled is None:
                    assert_real_upload_permitted()
                    result = adapter.publish(upload_job, source_clip, profile)
                else:
                    result = reconciled
            except Exception as exc:
                if str(exc).startswith("real upload denied:"):
                    _release_upload_blocked(
                        store,
                        upload_job_id,
                        upload_job,
                        str(exc).removeprefix("real upload denied:").strip() or "uploads_blocked",
                    )
                    return {
                        "upload_job_id": upload_job_id,
                        "uploaded": False,
                        "reason": str(exc).removeprefix("real upload denied:").strip() or "uploads_blocked",
                        "upload_mode": mode,
                        "environment": runtime_environment(),
                    }
                failure_class = (
                    FailureClass.AUTHENTICATION_FAILURE
                    if "token" in str(exc).lower() or "auth" in str(exc).lower()
                    else FailureClass.RETRYABLE
                )
                result = PublishResult(
                    ok=False,
                    status=UploadStatus.FAILED_RETRYABLE if failure_class == FailureClass.RETRYABLE else UploadStatus.FAILED_TERMINAL,
                    failure_class=failure_class,
                    error_category="adapter_exception",
                    error_message=str(exc),
                    publish_state=PublishState.FAILED,
                    platform_state="failed",
                    adapter_version=getattr(adapter, "adapter_version", None),
                    api_version=getattr(adapter, "api_version", None),
                    retryable=failure_class == FailureClass.RETRYABLE,
                )
            if result.duration_ms is None:
                result = _with_duration(result, int((time.monotonic() - start) * 1000))
    _record_upload_result(store, upload_job_id, upload_job, result, max_attempts=max_attempts)
    return {
        "upload_job_id": upload_job_id,
        "uploaded": result.ok,
        "upload_mode": mode,
        "environment": runtime_environment(),
        "status": result.status,
        "platform_video_id": result.platform_asset_id,
        "platform_asset_id": result.platform_asset_id,
        "publish_at": upload_job.get("publish_at") or upload_job.get("platform_publish_at"),
        "error_category": result.error_category,
        "error_message": result.error_message,
        "failure_class": result.failure_class,
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
        raw_response=result.raw_response,
        remote_ids=result.remote_ids,
        failure_class=result.failure_class,
        adapter_version=result.adapter_version,
        api_version=result.api_version,
        lease_token=str(upload_job.get("lease_token") or "") or None,
        duration_ms=result.duration_ms,
        error_category=result.error_category,
        error_message=result.error_message,
        retryable=result.retryable,
    )
    _record_metrics(store, upload_job_id, upload_job, result)
    if result.ok:
        completed = store.set_uploaded_scheduled(
            upload_job_id,
            platform_video_id=result.platform_asset_id,
            platform_state=result.platform_state,
            publish_state=result.publish_state or PublishState.SCHEDULED,
            remote_ids=result.remote_ids,
            adapter_version=result.adapter_version,
            api_version=result.api_version,
            lease_token=str(upload_job.get("lease_token") or "") or None,
        )
        if not completed:
            store.update_upload_job(
                upload_job_id,
                status=UploadStatus.FAILED_RETRYABLE,
                last_error="stale_upload_lease_completion_rejected",
            )
        store.record_account_success(
            platform=str(upload_job.get("platform") or ""),
            channel_id=str(upload_job.get("channel_id") or ""),
        )
        return
    next_attempt_count = int(upload_job.get("attempt_count") or 0) + 1
    if result.retryable and next_attempt_count < max_attempts:
        status = UploadStatus.FAILED_RETRYABLE
    else:
        status = UploadStatus.FAILED_UPLOAD
    store.update_upload_job(
        upload_job_id,
        status=status,
        last_error=result.error_message,
        remote_ids_json=_jsonish(result.remote_ids),
        platform_state=result.platform_state,
        publish_state=result.publish_state or PublishState.FAILED,
        adapter_version=result.adapter_version,
        api_version=result.api_version,
        lease_owner=None,
        lease_token=None,
        lease_heartbeat_at=None,
        lease_expires_at=None,
    )
    store.record_account_failure(
        platform=str(upload_job.get("platform") or ""),
        channel_id=str(upload_job.get("channel_id") or ""),
        failure_class=result.failure_class,
        retry_after_seconds=_retry_after_from_result(result),
    )


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


def _account_block_reason(store: OutputStore, upload_job: dict[str, Any]) -> dict[str, Any] | None:
    state = store.account_state(
        platform=str(upload_job.get("platform") or ""),
        channel_id=str(upload_job.get("channel_id") or ""),
    )
    if not state:
        return None
    now = now_utc()
    for key, reason in (("rate_limited_until", "account_rate_limited"), ("circuit_open_until", "account_circuit_open")):
        dt = parse_iso_datetime(str(state.get(key) or ""))
        if dt is not None and dt > now:
            return {"reason": reason, "retry_at": state.get(key)}
    return None


def _release_upload_blocked(
    store: OutputStore,
    upload_job_id: int,
    upload_job: dict[str, Any],
    reason: str,
) -> None:
    store.update_upload_job(
        upload_job_id,
        status=UploadStatus.PENDING_UPLOAD,
        last_error=reason,
        lease_owner=None,
        lease_token=None,
        lease_heartbeat_at=None,
        lease_expires_at=None,
    )


def _defer_account_blocked(
    store: OutputStore,
    upload_job_id: int,
    upload_job: dict[str, Any],
    block: dict[str, Any],
) -> None:
    store.record_attempt(
        upload_job_id,
        status=UploadStatus.FAILED_RETRYABLE,
        request_summary={
            "platform": upload_job.get("platform"),
            "channel_id": upload_job.get("channel_id"),
        },
        response_summary={"blocked": True, **block},
        failure_class=FailureClass.RATE_LIMITED if block["reason"] == "account_rate_limited" else FailureClass.RETRYABLE,
        lease_token=str(upload_job.get("lease_token") or "") or None,
        error_category=block["reason"],
        error_message=block["reason"],
        retryable=True,
    )
    store.update_upload_job(
        upload_job_id,
        status=UploadStatus.PENDING_UPLOAD,
        last_error=block["reason"],
        lease_owner=None,
        lease_token=None,
        lease_heartbeat_at=None,
        lease_expires_at=None,
    )


def _record_metrics(store: OutputStore, upload_job_id: int, upload_job: dict[str, Any], result: PublishResult) -> None:
    platform = str(upload_job.get("platform") or "")
    channel_id = str(upload_job.get("channel_id") or "")
    source = f"{platform}:{channel_id}"
    if result.duration_ms is not None:
        store.record_publication_metric(
            upload_job_id,
            metric_name="upload_duration",
            metric_value=float(result.duration_ms),
            metric_unit="ms",
            source=source,
        )
    store.record_publication_metric(
        upload_job_id,
        metric_name="upload_success" if result.ok else "upload_failure",
        metric_value=1.0,
        metric_unit="count",
        source=source,
        dimensions={"failure_class": result.failure_class or ""},
    )
    if result.failure_class == FailureClass.RATE_LIMITED:
        store.record_publication_metric(
            upload_job_id,
            metric_name="rate_limit_count",
            metric_value=1.0,
            metric_unit="count",
            source=source,
        )


def _retry_after_from_result(result: PublishResult) -> int | None:
    value = result.response.get("retry_after_seconds") or result.raw_response.get("retry_after_seconds")
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _jsonish(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value or {}, sort_keys=True)


def _with_duration(result: PublishResult, duration_ms: int) -> PublishResult:
    return replace(result, duration_ms=duration_ms)


def _dry_run_result(upload_job: dict[str, Any]) -> PublishResult:
    upload_job_id = upload_job.get("id")
    platform = str(upload_job.get("platform") or "platform")
    asset_id = f"dry_run_{platform}_{upload_job_id}"
    return PublishResult(
        ok=True,
        status=UploadStatus.UPLOADED_SCHEDULED,
        platform_asset_id=asset_id,
        scheduled_at=str(upload_job.get("platform_publish_at") or upload_job.get("publish_at") or ""),
        response={
            "dry_run": True,
            "environment": runtime_environment(),
            "upload_mode": "dry_run",
            "platform": platform,
            "channel_id": upload_job.get("channel_id"),
            "publish_at": upload_job.get("publish_at") or upload_job.get("platform_publish_at"),
        },
        raw_response={"dry_run": True},
        remote_ids={"dry_run_asset_id": asset_id},
        publish_state=PublishState.SCHEDULED,
        platform_state="dry_run",
        adapter_version="dry_run",
        api_version="dry_run",
        retryable=False,
    )


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
    adapters: dict[str, PlatformAdapter] | None = None,
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
