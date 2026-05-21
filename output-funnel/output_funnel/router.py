from __future__ import annotations

from typing import Any

from .models import RouteResult, UploadStatus
from .store import OutputStore


def _score(source_clip: dict[str, Any]) -> float:
    raw = source_clip.get("composite_score")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    scores = source_clip.get("scores")
    if isinstance(scores, dict):
        vals = []
        for value in scores.values():
            try:
                vals.append(float(value))
            except (TypeError, ValueError):
                continue
        if vals:
            return sum(vals) / len(vals)
    return 0.0


def _funnel_id(source_clip: dict[str, Any]) -> str | None:
    payload = source_clip.get("source_payload")
    if isinstance(payload, dict):
        raw = payload.get("funnel_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def profile_matches(source_clip: dict[str, Any], upload_job: dict[str, Any], profile: dict[str, Any]) -> tuple[bool, str | None]:
    if profile.get("enabled") is not True:
        return False, "profile_disabled"
    if str(profile.get("platform") or "") != str(upload_job.get("platform") or ""):
        return False, "platform_mismatch"
    routing = profile.get("routing") if isinstance(profile.get("routing"), dict) else {}
    required_platform = routing.get("required_platform")
    if required_platform and str(required_platform) != str(upload_job.get("platform") or ""):
        return False, "required_platform_mismatch"
    accepted_funnels = routing.get("accepted_funnel_ids")
    fid = _funnel_id(source_clip)
    if isinstance(accepted_funnels, list) and accepted_funnels:
        if fid not in accepted_funnels:
            return False, "funnel_not_accepted"
    min_score = routing.get("min_composite_score")
    if min_score is not None:
        try:
            if _score(source_clip) < float(min_score):
                return False, "score_below_threshold"
        except (TypeError, ValueError):
            return False, "invalid_profile_score_threshold"
    return True, None


def route_upload_job(
    store: OutputStore,
    upload_job_id: int,
    profiles: list[dict[str, Any]],
) -> RouteResult:
    upload_job = store.get_upload_job(upload_job_id)
    if upload_job is None:
        return RouteResult(False, reason="upload_job_not_found")
    source_clip = store.get_source_clip(int(upload_job["clip_pk"]))
    if source_clip is None:
        return RouteResult(False, reason="source_clip_not_found")
    preflight = source_clip.get("preflight")
    if isinstance(preflight, dict) and preflight.get("ok") is not True:
        return RouteResult(False, reason="preflight_failed")

    sorted_profiles = sorted(profiles, key=lambda p: int(p.get("priority") or 1000))
    last_reason: str | None = None
    for profile in sorted_profiles:
        matched, reason = profile_matches(source_clip, upload_job, profile)
        if matched:
            channel_id = str(profile.get("channel_id") or "").strip()
            if not channel_id:
                return RouteResult(False, reason="profile_missing_channel_id")
            return RouteResult(True, channel_id=channel_id, profile=profile)
        last_reason = reason
    return RouteResult(False, reason=last_reason or "no_matching_profile")


def mark_routing_failure(store: OutputStore, upload_job_id: int, reason: str) -> None:
    store.update_upload_job(
        upload_job_id,
        status=UploadStatus.FAILED_TERMINAL,
        last_error=f"routing_failed:{reason}",
    )
