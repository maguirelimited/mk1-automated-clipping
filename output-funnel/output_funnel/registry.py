from __future__ import annotations

from typing import Any

from .models import SourceClip
from .preflight import run_preflight
from .store import OutputStore


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _source_job_id(payload: dict[str, Any], report_path: str | None = None) -> str:
    raw = payload.get("job_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if report_path:
        return report_path
    raise ValueError("Job payload requires `job_id`")


def clips_from_job_payload(payload: dict[str, Any], *, report_path: str | None = None) -> list[SourceClip]:
    source_job_id = _source_job_id(payload, report_path)
    funnel_id: str | None = None
    if isinstance(payload.get("funnel_id"), str) and str(payload["funnel_id"]).strip():
        funnel_id = str(payload["funnel_id"]).strip()
    funnel = payload.get("funnel")
    if funnel_id is None and isinstance(funnel, dict):
        raw_funnel_id = funnel.get("funnel_id")
        if isinstance(raw_funnel_id, str) and raw_funnel_id.strip():
            funnel_id = raw_funnel_id.strip()
    raw_clips = payload.get("clips") if isinstance(payload.get("clips"), list) else []
    clips: list[SourceClip] = []
    for index, item in enumerate(raw_clips, start=1):
        if not isinstance(item, dict):
            continue
        clip_id = str(item.get("clip_id") or f"{source_job_id}_clip_{index:02d}").strip()
        if not clip_id:
            continue
        scores = item.get("scores") if isinstance(item.get("scores"), dict) else {}
        validation = item.get("clip_validation") if isinstance(item.get("clip_validation"), dict) else {}
        source_payload = dict(item)
        if funnel_id and "funnel_id" not in source_payload:
            source_payload["funnel_id"] = funnel_id
        clips.append(
            SourceClip(
                source_job_id=source_job_id,
                clip_id=clip_id,
                clip_index=_int_or_none(item.get("clip_index")) or index,
                start=str(item.get("start")).strip() if item.get("start") is not None else None,
                end=str(item.get("end")).strip() if item.get("end") is not None else None,
                duration_sec=_float_or_none(item.get("duration_sec")),
                clip_file=str(item.get("clip_file")).strip() if item.get("clip_file") else None,
                clip_path=str(item.get("clip_path")).strip() if item.get("clip_path") else None,
                job_clip_path=str(item.get("job_clip_path")).strip() if item.get("job_clip_path") else None,
                title=str(item.get("title")).strip() if item.get("title") else None,
                hook=str(item.get("hook")).strip() if item.get("hook") else None,
                caption=str(item.get("caption")).strip() if item.get("caption") else None,
                reason=str(item.get("reason")).strip() if item.get("reason") else None,
                scores=dict(scores),
                composite_score=_float_or_none(item.get("composite_score")),
                clip_validation=dict(validation),
                source_payload=source_payload,
            )
        )
    return clips


def enabled_platforms_from_payload(payload: dict[str, Any]) -> list[str]:
    candidates: list[Any] = []
    if isinstance(payload.get("enabled_platforms"), list):
        candidates = payload["enabled_platforms"]
    funnel = payload.get("funnel")
    if isinstance(funnel, dict) and isinstance(funnel.get("enabled_platforms"), list):
        candidates = funnel["enabled_platforms"]
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        policy = metadata.get("policy_resolution")
        if isinstance(policy, dict):
            funnel_ops = policy.get("funnel_ops")
            if isinstance(funnel_ops, dict):
                platforms = funnel_ops.get("platforms")
                if isinstance(platforms, dict):
                    candidates = [k for k, v in platforms.items() if v is True]
    out: list[str] = []
    for item in candidates:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip())
    return out or ["youtube_shorts"]


def register_job_payload(
    store: OutputStore,
    payload: dict[str, Any],
    *,
    platforms: list[str] | None = None,
    duration_tolerance_sec: float = 1.0,
) -> dict[str, Any]:
    source_job_id = _source_job_id(payload)
    if str(payload.get("status") or "success") != "success" and payload.get("ready") is not True:
        raise ValueError("Only successful/ready job outputs can be registered")

    selected_platforms = platforms or enabled_platforms_from_payload(payload)
    registered: list[dict[str, Any]] = []
    for clip in clips_from_job_payload(payload):
        preflight = run_preflight(clip, duration_tolerance_sec=duration_tolerance_sec)
        clip_pk, clip_created = store.register_source_clip(clip, preflight)
        upload_jobs: list[dict[str, Any]] = []
        if preflight.ok:
            for platform in selected_platforms:
                upload_job_id, created = store.create_upload_job(clip_pk=clip_pk, platform=platform)
                upload_jobs.append({"upload_job_id": upload_job_id, "platform": platform, "created": created})
        registered.append(
            {
                "clip_pk": clip_pk,
                "clip_id": clip.clip_id,
                "clip_created": clip_created,
                "preflight_ok": preflight.ok,
                "preflight_issues": preflight.issues,
                "upload_jobs": upload_jobs,
            }
        )
    return {
        "source_job_id": source_job_id,
        "clip_count": len(registered),
        "registered": registered,
    }
