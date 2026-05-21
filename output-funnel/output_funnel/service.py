from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import database_path, load_channel_profiles, load_settings
from .metadata import normalize_metadata
from .models import UploadStatus
from .publisher import publish_due_jobs
from .registry import register_job_payload
from .router import mark_routing_failure, route_upload_job
from .scheduler import next_scheduled_time
from .store import OutputStore


def make_store(settings: dict[str, Any] | None = None) -> OutputStore:
    store = OutputStore(database_path(settings))
    store.init_db()
    return store


def load_job_payload_from_path(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Job payload must be a JSON object")
    return payload


def register_from_payload(
    payload: dict[str, Any],
    *,
    store: OutputStore | None = None,
    settings: dict[str, Any] | None = None,
    platforms: list[str] | None = None,
) -> dict[str, Any]:
    cfg = settings or load_settings()
    preflight_cfg = cfg.get("preflight") if isinstance(cfg.get("preflight"), dict) else {}
    tolerance = float(preflight_cfg.get("duration_tolerance_sec") or 1.0)
    active_store = store or make_store(cfg)
    return register_job_payload(
        active_store,
        payload,
        platforms=platforms,
        duration_tolerance_sec=tolerance,
    )


def register_and_process_from_payload(
    payload: dict[str, Any],
    *,
    store: OutputStore | None = None,
    settings: dict[str, Any] | None = None,
    platforms: list[str] | None = None,
) -> dict[str, Any]:
    cfg = settings or load_settings()
    active_store = store or make_store(cfg)
    result = register_from_payload(payload, store=active_store, settings=cfg, platforms=platforms)
    automation = _automation_settings(cfg)
    process_result: dict[str, Any] = {
        "auto_schedule_enabled": automation["auto_schedule"],
        "auto_publish_enabled": automation["auto_publish"],
    }
    if automation["auto_schedule"]:
        process_result["schedule"] = schedule_due_upload_jobs(
            store=active_store,
            limit=automation["schedule_limit"],
        )
    if automation["auto_publish"]:
        process_result["publish"] = publish_due(
            store=active_store,
            limit=automation["publish_limit"],
        )
    result["processing"] = process_result
    return result


def route_and_prepare_upload_job(
    upload_job_id: int,
    *,
    store: OutputStore | None = None,
    profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    active_store = store or make_store()
    channel_profiles = profiles if profiles is not None else load_channel_profiles()
    route = route_upload_job(active_store, upload_job_id, channel_profiles)
    if not route.matched or not route.profile or not route.channel_id:
        mark_routing_failure(active_store, upload_job_id, route.reason or "no_matching_profile")
        return {"upload_job_id": upload_job_id, "routed": False, "reason": route.reason}

    upload_job = active_store.get_upload_job(upload_job_id)
    if upload_job is None:
        return {"upload_job_id": upload_job_id, "routed": False, "reason": "upload_job_not_found"}
    source_clip = active_store.get_source_clip(int(upload_job["clip_pk"]))
    if source_clip is None:
        return {"upload_job_id": upload_job_id, "routed": False, "reason": "source_clip_not_found"}

    metadata = normalize_metadata(source_clip, route.profile)
    active_store.set_routed(
        upload_job_id,
        channel_id=route.channel_id,
        metadata=metadata,
        profile_snapshot=_safe_profile_snapshot(route.profile),
    )
    return {
        "upload_job_id": upload_job_id,
        "routed": True,
        "channel_id": route.channel_id,
        "metadata_issues": metadata.issues,
    }


def schedule_upload_job(
    upload_job_id: int,
    *,
    store: OutputStore | None = None,
    profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    active_store = store or make_store()
    job = active_store.get_upload_job(upload_job_id)
    if job is None:
        return {"upload_job_id": upload_job_id, "scheduled": False, "reason": "upload_job_not_found"}
    if job["status"] == UploadStatus.REGISTERED:
        routed = route_and_prepare_upload_job(upload_job_id, store=active_store, profiles=profiles)
        if not routed.get("routed"):
            return {"upload_job_id": upload_job_id, "scheduled": False, "reason": routed.get("reason")}
        job = active_store.get_upload_job(upload_job_id)
    if job is None or job["status"] != UploadStatus.ROUTED:
        return {
            "upload_job_id": upload_job_id,
            "scheduled": False,
            "reason": f"invalid_status:{None if job is None else job['status']}",
        }

    profile = _profile_by_channel(profiles or load_channel_profiles(), str(job["channel_id"]))
    if profile is None:
        return {"upload_job_id": upload_job_id, "scheduled": False, "reason": "profile_not_found"}
    existing = active_store.existing_scheduled_times(
        platform=str(job["platform"]),
        channel_id=str(job["channel_id"]),
    )
    scheduled_at = next_scheduled_time(profile, existing)
    active_store.set_scheduled(
        upload_job_id,
        scheduled_at=scheduled_at,
        platform_publish_at=scheduled_at,
    )
    return {"upload_job_id": upload_job_id, "scheduled": True, "scheduled_at": scheduled_at}


def schedule_due_upload_jobs(
    *,
    store: OutputStore | None = None,
    profiles: list[dict[str, Any]] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    active_store = store or make_store()
    jobs = active_store.list_upload_jobs(status=UploadStatus.REGISTERED, limit=limit)
    jobs.extend(active_store.list_upload_jobs(status=UploadStatus.ROUTED, limit=limit))
    results = [
        schedule_upload_job(int(job["id"]), store=active_store, profiles=profiles)
        for job in jobs[: max(1, int(limit))]
    ]
    return {"count": len(results), "results": results}


def retry_upload_job(
    upload_job_id: int,
    *,
    store: OutputStore | None = None,
) -> dict[str, Any]:
    active_store = store or make_store()
    job = active_store.get_upload_job(upload_job_id)
    if job is None:
        return {"upload_job_id": upload_job_id, "retry": False, "reason": "upload_job_not_found"}
    if str(job.get("status") or "") not in (UploadStatus.FAILED_RETRYABLE, UploadStatus.FAILED_TERMINAL):
        return {
            "upload_job_id": upload_job_id,
            "retry": False,
            "reason": f"invalid_status:{job.get('status')}",
        }
    if job.get("scheduled_at"):
        active_store.update_upload_job(
            upload_job_id,
            status=UploadStatus.SCHEDULED,
            last_error=None,
        )
    else:
        active_store.update_upload_job(
            upload_job_id,
            status=UploadStatus.REGISTERED,
            last_error=None,
        )
    return {"upload_job_id": upload_job_id, "retry": True}


def publish_due(
    *,
    store: OutputStore | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    active_store = store or make_store()
    return publish_due_jobs(active_store, limit=limit)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _automation_settings(settings: dict[str, Any]) -> dict[str, Any]:
    raw = settings.get("automation") if isinstance(settings.get("automation"), dict) else {}
    auto_schedule = _env_bool("OUTPUT_FUNNEL_AUTO_SCHEDULE", bool(raw.get("auto_schedule", True)))
    auto_publish = _env_bool("OUTPUT_FUNNEL_AUTO_PUBLISH", bool(raw.get("auto_publish", False)))
    try:
        schedule_limit = int(os.environ.get("OUTPUT_FUNNEL_AUTO_SCHEDULE_LIMIT") or raw.get("schedule_limit") or 50)
    except (TypeError, ValueError):
        schedule_limit = 50
    try:
        publish_limit = int(os.environ.get("OUTPUT_FUNNEL_AUTO_PUBLISH_LIMIT") or raw.get("publish_limit") or 1)
    except (TypeError, ValueError):
        publish_limit = 1
    return {
        "auto_schedule": auto_schedule,
        "auto_publish": auto_publish,
        "schedule_limit": max(1, schedule_limit),
        "publish_limit": max(1, publish_limit),
    }


def _profile_by_channel(profiles: list[dict[str, Any]], channel_id: str) -> dict[str, Any] | None:
    for profile in profiles:
        if str(profile.get("channel_id") or "") == channel_id:
            return profile
    return None


def _safe_profile_snapshot(profile: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "channel_id",
        "brand_name",
        "platform",
        "priority",
        "routing",
        "cadence",
        "metadata_style",
    }
    return {key: profile[key] for key in allowed if key in profile}
