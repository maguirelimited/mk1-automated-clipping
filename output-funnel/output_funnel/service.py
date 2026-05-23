from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from .config import database_path, load_channel_profiles, load_settings
from .metadata import normalize_metadata
from .models import UploadStatus
from .publisher import upload_due_jobs, publish_due_jobs
from .registry import register_job_payload
from .router import mark_routing_failure, route_upload_job
from .scheduler import next_scheduled_time
from .debug_log import agent_debug_log
from .store import OutputStore
from .time_utils import now_utc, parse_iso_datetime, to_utc_iso

log = logging.getLogger("output_funnel.service")


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
    funnel = payload.get("funnel") if isinstance(payload.get("funnel"), dict) else {}
    agent_debug_log(
        hypothesis_id="A",
        location="service.py:register_and_process_from_payload",
        message="registration payload funnel context",
        data={
            "job_id": payload.get("job_id"),
            "top_level_funnel_id": payload.get("funnel_id"),
            "funnel_record_funnel_id": funnel.get("funnel_id"),
            "clip_count": len(payload.get("clips") or []),
        },
    )
    result = register_from_payload(payload, store=active_store, settings=cfg, platforms=platforms)
    automation = _automation_settings(cfg)
    agent_debug_log(
        hypothesis_id="B",
        location="service.py:register_and_process_from_payload",
        message="automation settings resolved",
        data={
            "auto_schedule": automation["auto_schedule"],
            "auto_upload": automation["auto_upload"],
            "schedule_limit": automation["schedule_limit"],
            "env_auto_schedule": os.environ.get("OUTPUT_FUNNEL_AUTO_SCHEDULE"),
        },
    )
    process_result: dict[str, Any] = {
        "auto_schedule_enabled": automation["auto_schedule"],
        "auto_publish_enabled": automation["auto_upload"],
        "auto_upload_enabled": automation["auto_upload"],
    }
    if automation["auto_schedule"]:
        process_result["schedule"] = plan_due_upload_jobs(
            store=active_store,
            settings=cfg,
            limit=automation["schedule_limit"],
        )
        agent_debug_log(
            hypothesis_id="E",
            location="service.py:register_and_process_from_payload",
            message="auto-schedule batch finished",
            data={"schedule": process_result["schedule"]},
        )
    if automation["auto_upload"]:
        process_result["publish"] = upload_due(
            store=active_store,
            limit=automation["upload_limit"],
        )
        process_result["upload"] = process_result["publish"]
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


def plan_upload_job(
    upload_job_id: int,
    *,
    store: OutputStore | None = None,
    profiles: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan publication and upload windows for a single upload job.

    The output of this stage is **strategic**: it picks the public release
    time on the platform using channel cadence rules, and then derives
    when our system should actually attempt the upload (earlier than the
    public release time, with a safety buffer before the YouTube-imposed
    minimum-15-minutes-in-the-future rule).
    """
    cfg = settings or load_settings()
    active_store = store or make_store(cfg)
    job = active_store.get_upload_job(upload_job_id)
    if job is None:
        return {
            "upload_job_id": upload_job_id,
            "planned": False,
            "scheduled": False,
            "reason": "upload_job_not_found",
        }
    if job["status"] == UploadStatus.REGISTERED:
        routed = route_and_prepare_upload_job(upload_job_id, store=active_store, profiles=profiles)
        if not routed.get("routed"):
            agent_debug_log(
                hypothesis_id="D",
                location="service.py:plan_upload_job",
                message="routing failed before plan",
                data={"upload_job_id": upload_job_id, "reason": routed.get("reason")},
            )
            return {
                "upload_job_id": upload_job_id,
                "planned": False,
                "scheduled": False,
                "reason": routed.get("reason"),
            }
        job = active_store.get_upload_job(upload_job_id)
    if job is None or job["status"] != UploadStatus.ROUTED:
        reason = f"invalid_status:{None if job is None else job['status']}"
        return {"upload_job_id": upload_job_id, "planned": False, "scheduled": False, "reason": reason}

    profile = _profile_by_channel(profiles or load_channel_profiles(), str(job["channel_id"]))
    if profile is None:
        return {
            "upload_job_id": upload_job_id,
            "planned": False,
            "scheduled": False,
            "reason": "profile_not_found",
        }
    existing = active_store.existing_publish_times(
        platform=str(job["platform"]),
        channel_id=str(job["channel_id"]),
    )
    publish_at = next_scheduled_time(profile, existing, defaults=_scheduler_defaults(cfg))
    upload_at, upload_deadline = compute_upload_window(
        publish_at=publish_at,
        platform=str(job["platform"]),
        settings=cfg,
        profile=profile,
    )
    active_store.set_planned(
        upload_job_id,
        publish_at=publish_at,
        upload_at=upload_at,
        upload_deadline=upload_deadline,
        platform_publish_at=publish_at,
    )
    agent_debug_log(
        hypothesis_id="E",
        location="service.py:plan_upload_job",
        message="upload job planned",
        data={
            "upload_job_id": upload_job_id,
            "channel_id": job.get("channel_id"),
            "publish_at": publish_at,
            "upload_at": upload_at,
            "upload_deadline": upload_deadline,
        },
    )
    return {
        "upload_job_id": upload_job_id,
        "planned": True,
        "scheduled": True,
        "publish_at": publish_at,
        "upload_at": upload_at,
        "upload_deadline": upload_deadline,
        "platform_publish_at": publish_at,
        "scheduled_at": upload_at,
    }


def schedule_upload_job(
    upload_job_id: int,
    *,
    store: OutputStore | None = None,
    profiles: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backward-compat alias for `plan_upload_job`.

    Existing callers (HTTP /queue/<id>/schedule, CLI `schedule`) keep working.
    """
    return plan_upload_job(
        upload_job_id, store=store, profiles=profiles, settings=settings
    )


def plan_due_upload_jobs(
    *,
    store: OutputStore | None = None,
    profiles: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    cfg = settings or load_settings()
    active_store = store or make_store(cfg)
    schedule_limit = _schedule_limit(cfg, limit)
    jobs = active_store.list_upload_jobs(status=UploadStatus.REGISTERED, limit=schedule_limit)
    jobs.extend(active_store.list_upload_jobs(status=UploadStatus.ROUTED, limit=schedule_limit))
    results = [
        plan_upload_job(int(job["id"]), store=active_store, profiles=profiles, settings=cfg)
        for job in jobs[:schedule_limit]
    ]
    return {"count": len(results), "results": results}


def schedule_due_upload_jobs(
    *,
    store: OutputStore | None = None,
    profiles: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Backward-compat alias for `plan_due_upload_jobs`."""
    return plan_due_upload_jobs(
        store=store, profiles=profiles, settings=settings, limit=limit
    )


def cancel_upload_job(
    upload_job_id: int,
    *,
    store: OutputStore | None = None,
) -> dict[str, Any]:
    """Mark a non-terminal upload job as cancelled."""
    active_store = store or make_store()
    job = active_store.get_upload_job(upload_job_id)
    if job is None:
        return {"upload_job_id": upload_job_id, "cancelled": False, "reason": "upload_job_not_found"}
    status = str(job.get("status") or "")
    if status in {
        UploadStatus.UPLOADING,
        UploadStatus.UPLOADED_SCHEDULED,
        UploadStatus.PUBLISHED,
        UploadStatus.CANCELLED,
        UploadStatus.MISSED_UPLOAD_WINDOW,
    }:
        return {
            "upload_job_id": upload_job_id,
            "cancelled": False,
            "reason": f"invalid_status:{status}",
        }
    active_store.update_upload_job(
        upload_job_id,
        status=UploadStatus.CANCELLED,
        last_error="cancelled_by_operator",
    )
    return {"upload_job_id": upload_job_id, "cancelled": True}


def reschedule_upload_job(
    upload_job_id: int,
    publish_at: str,
    *,
    store: OutputStore | None = None,
    profiles: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Set an explicit publish_at and recompute the upload window."""
    cfg = settings or load_settings()
    active_store = store or make_store(cfg)
    job = active_store.get_upload_job(upload_job_id)
    if job is None:
        return {
            "upload_job_id": upload_job_id,
            "rescheduled": False,
            "reason": "upload_job_not_found",
        }
    status = str(job.get("status") or "")
    if status not in {
        UploadStatus.ROUTED,
        UploadStatus.PLANNED,
        UploadStatus.PENDING_UPLOAD,
        UploadStatus.FAILED_RETRYABLE,
        UploadStatus.FAILED_UPLOAD,
        UploadStatus.FAILED_TERMINAL,
    }:
        return {
            "upload_job_id": upload_job_id,
            "rescheduled": False,
            "reason": f"invalid_status:{status}",
        }
    pub_dt = parse_iso_datetime(publish_at)
    if pub_dt is None:
        return {
            "upload_job_id": upload_job_id,
            "rescheduled": False,
            "reason": "invalid_publish_at",
        }
    normalized_publish_at = to_utc_iso(pub_dt)
    profile = _profile_by_channel(
        profiles or load_channel_profiles(),
        str(job.get("channel_id") or ""),
    )
    upload_at, upload_deadline = compute_upload_window(
        publish_at=normalized_publish_at,
        platform=str(job["platform"]),
        settings=cfg,
        profile=profile,
    )
    active_store.set_planned(
        upload_job_id,
        publish_at=normalized_publish_at,
        upload_at=upload_at,
        upload_deadline=upload_deadline,
        platform_publish_at=normalized_publish_at,
    )
    return {
        "upload_job_id": upload_job_id,
        "rescheduled": True,
        "publish_at": normalized_publish_at,
        "upload_at": upload_at,
        "upload_deadline": upload_deadline,
    }


def retry_upload_job(
    upload_job_id: int,
    *,
    store: OutputStore | None = None,
) -> dict[str, Any]:
    """Put a failed upload job back into the upload queue.

    Status transitions:
      - ``failed_retryable`` → ``pending_upload`` (transient retry within
        the existing upload window; the deadline check still applies)
      - ``failed_upload`` / ``failed_terminal`` → ``planned`` (treat as a
        fresh planned job; if its deadline has passed the next upload-due
        tick will move it to ``missed_upload_window``)
      - any other status → no-op

    If no plan exists yet (no publish_at / scheduled_at), the job is sent
    back to ``registered`` so it can be planned from scratch.
    """
    active_store = store or make_store()
    job = active_store.get_upload_job(upload_job_id)
    if job is None:
        return {"upload_job_id": upload_job_id, "retry": False, "reason": "upload_job_not_found"}
    status = str(job.get("status") or "")
    retryable_states = (
        UploadStatus.FAILED_RETRYABLE,
        UploadStatus.FAILED_TERMINAL,
        UploadStatus.FAILED_UPLOAD,
    )
    if status not in retryable_states:
        return {
            "upload_job_id": upload_job_id,
            "retry": False,
            "reason": f"invalid_status:{job.get('status')}",
        }
    has_plan = bool(job.get("publish_at") or job.get("scheduled_at"))
    if not has_plan:
        active_store.update_upload_job(
            upload_job_id,
            status=UploadStatus.REGISTERED,
            last_error=None,
        )
    elif status == UploadStatus.FAILED_RETRYABLE:
        active_store.update_upload_job(
            upload_job_id,
            status=UploadStatus.PENDING_UPLOAD,
            last_error=None,
        )
    else:
        active_store.update_upload_job(
            upload_job_id,
            status=UploadStatus.PLANNED,
            last_error=None,
        )
    return {"upload_job_id": upload_job_id, "retry": True}


def upload_due(
    *,
    store: OutputStore | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    active_store = store or make_store()
    return upload_due_jobs(active_store, limit=limit)


def publish_due(
    *,
    store: OutputStore | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Deprecated alias for `upload_due`. See publisher.publish_due_jobs."""
    active_store = store or make_store()
    return publish_due_jobs(active_store, limit=limit)


def compute_upload_window(
    *,
    publish_at: str,
    platform: str,
    settings: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return ``(upload_at, upload_deadline)`` ISO strings for a publish_at.

    Resolution order for lead/buffer minutes: per-profile `upload` block →
    settings.<platform>.upload_lead_minutes → settings defaults → hardcoded
    fallback. The deadline is always at least 16 minutes before publish_at
    because YouTube requires publish_at to be more than 15 minutes in the
    future at the moment of upload.
    """
    pub_dt = parse_iso_datetime(publish_at)
    if pub_dt is None:
        raise ValueError(f"compute_upload_window: invalid publish_at {publish_at!r}")
    profile_upload = (profile or {}).get("upload") if isinstance((profile or {}).get("upload"), dict) else {}
    platform_cfg = settings.get(_platform_settings_key(platform))
    platform_cfg = platform_cfg if isinstance(platform_cfg, dict) else {}

    lead_minutes = _int_or_default(
        profile_upload.get("lead_minutes")
        or platform_cfg.get("upload_lead_minutes")
        or os.environ.get(f"OUTPUT_FUNNEL_{platform.upper()}_UPLOAD_LEAD_MINUTES"),
        90,
    )
    safety_minutes = _int_or_default(
        profile_upload.get("safety_buffer_minutes")
        or platform_cfg.get("upload_safety_buffer_minutes")
        or os.environ.get(f"OUTPUT_FUNNEL_{platform.upper()}_UPLOAD_SAFETY_MINUTES"),
        20,
    )
    safety_minutes = max(safety_minutes, 20)
    if lead_minutes <= safety_minutes:
        lead_minutes = safety_minutes + 30

    upload_dt = pub_dt - timedelta(minutes=lead_minutes)
    deadline_dt = pub_dt - timedelta(minutes=safety_minutes)
    return to_utc_iso(upload_dt), to_utc_iso(deadline_dt)


def _platform_settings_key(platform: str) -> str:
    if platform.startswith("youtube"):
        return "youtube"
    if platform.startswith("tiktok"):
        return "tiktok"
    if platform.startswith("instagram"):
        return "instagram"
    return platform


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _int_or_default(value: Any, default: int) -> int:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    return _int_or_default(os.environ.get(name) or default, default)


def _automation_settings(settings: dict[str, Any]) -> dict[str, Any]:
    raw = settings.get("automation") if isinstance(settings.get("automation"), dict) else {}
    auto_schedule = _env_bool("OUTPUT_FUNNEL_AUTO_SCHEDULE", bool(raw.get("auto_schedule", True)))
    auto_upload_default = bool(raw.get("auto_upload", raw.get("auto_publish", False)))
    auto_upload = _env_bool(
        "OUTPUT_FUNNEL_AUTO_UPLOAD",
        _env_bool("OUTPUT_FUNNEL_AUTO_PUBLISH", auto_upload_default),
    )
    schedule_limit = _env_int(
        "OUTPUT_FUNNEL_AUTO_SCHEDULE_LIMIT",
        _int_or_default(raw.get("schedule_limit"), 50),
    )
    upload_limit = _env_int(
        "OUTPUT_FUNNEL_AUTO_UPLOAD_LIMIT",
        _env_int(
            "OUTPUT_FUNNEL_AUTO_PUBLISH_LIMIT",
            _int_or_default(raw.get("upload_limit", raw.get("publish_limit")), 1),
        ),
    )
    return {
        "auto_schedule": auto_schedule,
        "auto_upload": auto_upload,
        "schedule_limit": max(1, schedule_limit),
        "upload_limit": max(1, upload_limit),
    }


def _schedule_limit(settings: dict[str, Any], override: int | None = None) -> int:
    if override is not None:
        return max(1, _int_or_default(override, 50))
    return int(_automation_settings(settings)["schedule_limit"])


def _scheduler_defaults(settings: dict[str, Any]) -> dict[str, Any]:
    raw = settings.get("scheduler") if isinstance(settings.get("scheduler"), dict) else {}
    timezone = os.environ.get("OUTPUT_FUNNEL_SCHEDULE_TIMEZONE") or raw.get("default_timezone") or "UTC"
    return {
        "timezone": timezone,
        "default_lead_minutes": _env_int(
            "OUTPUT_FUNNEL_SCHEDULE_LEAD_MINUTES",
            _int_or_default(raw.get("default_lead_minutes"), 180),
        ),
        "min_gap_minutes": _env_int(
            "OUTPUT_FUNNEL_SCHEDULE_MIN_GAP_MINUTES",
            _int_or_default(raw.get("default_min_gap_minutes"), 180),
        ),
        "max_uploads_per_day": _env_int(
            "OUTPUT_FUNNEL_SCHEDULE_MAX_UPLOADS_PER_DAY",
            _int_or_default(raw.get("default_max_uploads_per_day"), 3),
        ),
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
        "upload",
    }
    return {key: profile[key] for key in allowed if key in profile}


def backfill_legacy_rows(
    *,
    store: OutputStore | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Migrate pre-v2 rows to the new two-stage model in place.

    For each row still in legacy state (`scheduled` etc.) or whose new
    fields are unset, derive `publish_at`/`upload_at`/`upload_deadline`
    from the old `scheduled_at` and the configured YouTube lead, and
    transition the status to either `planned` (publish_at safely in the
    future) or `missed_upload_window` (deadline already past).
    """
    cfg = settings or load_settings()
    active_store = store or make_store(cfg)
    now = now_utc()
    affected: list[dict[str, Any]] = []
    target_statuses = ("scheduled", UploadStatus.PLANNED, UploadStatus.PENDING_UPLOAD)
    placeholders = ", ".join("?" for _ in target_statuses)
    with active_store.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, platform, status, publish_at, upload_at, upload_deadline,
                   platform_publish_at, scheduled_at, channel_id
            FROM upload_jobs
            WHERE status IN ({placeholders})
            """,
            target_statuses,
        ).fetchall()
    for row in rows:
        publish_at = row["publish_at"] or row["platform_publish_at"] or row["scheduled_at"]
        if not publish_at:
            continue
        pub_dt = parse_iso_datetime(publish_at)
        if pub_dt is None:
            continue
        try:
            upload_at, deadline = compute_upload_window(
                publish_at=publish_at,
                platform=str(row["platform"] or "youtube_shorts"),
                settings=cfg,
            )
        except ValueError:
            continue
        deadline_dt = parse_iso_datetime(deadline)
        if deadline_dt is None:
            continue
        if deadline_dt <= now:
            active_store.update_upload_job(
                int(row["id"]),
                status=UploadStatus.MISSED_UPLOAD_WINDOW,
                publish_at=publish_at,
                platform_publish_at=publish_at,
                upload_at=upload_at,
                upload_deadline=deadline,
                last_error="missed_upload_window_on_backfill",
            )
            affected.append(
                {"upload_job_id": int(row["id"]), "action": "missed_upload_window", "publish_at": publish_at}
            )
        else:
            active_store.set_planned(
                int(row["id"]),
                publish_at=publish_at,
                upload_at=upload_at,
                upload_deadline=deadline,
                platform_publish_at=publish_at,
            )
            affected.append(
                {
                    "upload_job_id": int(row["id"]),
                    "action": "planned",
                    "publish_at": publish_at,
                    "upload_at": upload_at,
                    "upload_deadline": deadline,
                }
            )
    return {"count": len(affected), "results": affected}
