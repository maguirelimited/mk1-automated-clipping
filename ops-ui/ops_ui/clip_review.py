from __future__ import annotations

import re
from typing import Any

from .config import Settings, ServiceConfig
from .diagnostics import load_input_ledger_record, transcript_view
from .funnels import load_clip_funnel_config
from .http_client import call_json


REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
REVIEW_FLAGGED = "flagged"

_TIME_RE = re.compile(
    r"^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?$"
)


def clip_key(job_id: str, clip_id: str) -> str:
    return f"{job_id}::{clip_id}"


def parse_time_to_seconds(value: Any) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    match = _TIME_RE.match(raw)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    millis = int((match.group(4) or "0").ljust(3, "0")[:3])
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def transcript_snippet_for_clip(
    segments: list[dict[str, Any]],
    *,
    start: Any,
    end: Any,
    max_chars: int = 600,
) -> str:
    start_sec = parse_time_to_seconds(start)
    end_sec = parse_time_to_seconds(end)
    if start_sec is None or end_sec is None:
        return ""
    lines: list[str] = []
    for row in segments:
        if not isinstance(row, dict):
            continue
        seg_start = row.get("start")
        seg_end = row.get("end")
        try:
            s0 = float(seg_start)
            s1 = float(seg_end)
        except (TypeError, ValueError):
            continue
        if s1 < start_sec or s0 > end_sec:
            continue
        text = str(row.get("text") or "").strip()
        if text:
            lines.append(text)
    blob = " ".join(lines).strip()
    if len(blob) > max_chars:
        return blob[:max_chars] + "…"
    return blob


def ai_score_label(clip: dict[str, Any]) -> str:
    if clip.get("composite_score") is not None:
        return str(clip.get("composite_score"))
    scores = clip.get("scores")
    if isinstance(scores, dict) and scores:
        parts = [f"{key}={scores[key]}" for key in sorted(scores.keys())[:4]]
        return ", ".join(parts)
    if clip.get("score") is not None:
        return str(clip.get("score"))
    return "—"


def platform_targets_for_job(
    *,
    policy: dict[str, Any] | None,
    input_source: dict[str, Any] | None,
    pipeline_profile: str,
) -> list[str]:
    targets: list[str] = []
    if input_source and input_source.get("available"):
        record = input_source.get("record")
        if isinstance(record, dict):
            funnel_policy = record.get("funnel_policy")
            if isinstance(funnel_policy, dict):
                posting = funnel_policy.get("posting_config")
                if isinstance(posting, dict):
                    platforms = posting.get("platforms")
                    if isinstance(platforms, list):
                        targets.extend(str(p) for p in platforms if str(p).strip())
    if policy:
        posting = policy.get("posting_config")
        if isinstance(posting, dict):
            platforms = posting.get("platforms")
            if isinstance(platforms, list):
                for platform in platforms:
                    label = str(platform).strip()
                    if label and label not in targets:
                        targets.append(label)
    profile = load_clip_funnel_config(pipeline_profile)
    if profile:
        posting = profile.get("posting_config")
        if isinstance(posting, dict):
            platforms = posting.get("platforms")
            if isinstance(platforms, list):
                for platform in platforms:
                    label = str(platform).strip()
                    if label and label not in targets:
                        targets.append(label)
    return targets


def enrich_clip_row(
    *,
    job_id: str,
    job: dict[str, Any],
    clip: dict[str, Any],
    review: dict[str, Any] | None,
    debug: dict[str, Any] | None = None,
    input_source: dict[str, Any] | None = None,
    video_svc: ServiceConfig | None = None,
) -> dict[str, Any]:
    clip_id = str(clip.get("clip_id") or clip.get("clip_index") or "").strip()
    if not clip_id:
        clip_id = f"{job_id}_clip_{clip.get('clip_index', 0)}"
    status = str((review or {}).get("status") or REVIEW_PENDING)
    policy = {}
    if debug and isinstance(debug.get("policy_resolution"), dict):
        policy = debug["policy_resolution"]
    elif isinstance(job.get("policy_resolution"), dict):
        policy = job["policy_resolution"]
    pipeline_profile = str(
        policy.get("pipeline_profile")
        or policy.get("resolved_pipeline_profile")
        or job.get("pipeline_profile")
        or ""
    ).strip()
    funnel_id = str(policy.get("funnel_id") or job.get("funnel_id") or "").strip()
    if not funnel_id and input_source and input_source.get("available"):
        funnel_id = str(input_source.get("funnel_id") or "")
    transcript_segments: list[dict[str, Any]] = []
    if debug:
        artifacts = debug.get("artifacts") if isinstance(debug.get("artifacts"), dict) else {}
        transcript = transcript_view(debug, artifacts)
        transcript_segments = transcript.get("segments") if isinstance(transcript.get("segments"), list) else []
    snippet = transcript_snippet_for_clip(
        transcript_segments,
        start=clip.get("start"),
        end=clip.get("end"),
    )
    clip_file = str(clip.get("clip_file") or "").strip()
    preview_url = ""
    if video_svc and clip_file:
        preview_url = f"/clip-review/media/{job_id}/{clip_file}"
    source_ref = ""
    if input_source and input_source.get("available"):
        source_ref = str(input_source.get("source_url") or input_source.get("file_path") or "")
    if not source_ref:
        source_ref = str(job.get("input_video_name") or job.get("source_video") or "")
    duration = clip.get("duration_sec")
    if duration is None:
        start_sec = parse_time_to_seconds(clip.get("start"))
        end_sec = parse_time_to_seconds(clip.get("end"))
        if start_sec is not None and end_sec is not None and end_sec > start_sec:
            duration = round(end_sec - start_sec, 2)
    return {
        "key": clip_key(job_id, clip_id),
        "job_id": job_id,
        "clip_id": clip_id,
        "title": clip.get("title") or "—",
        "hook": clip.get("hook") or "",
        "caption": clip.get("caption") or "",
        "reason": clip.get("reason") or clip.get("selection_reason") or "",
        "ai_score": ai_score_label(clip),
        "start": clip.get("start") or "—",
        "end": clip.get("end") or "—",
        "duration_sec": duration,
        "duration_label": f"{duration}s" if duration is not None else "—",
        "review_status": status,
        "flagged_high_quality": bool((review or {}).get("flagged_high_quality")),
        "feedback_notes": str((review or {}).get("feedback_notes") or ""),
        "platform_targets": platform_targets_for_job(
            policy=policy,
            input_source=input_source,
            pipeline_profile=pipeline_profile,
        ),
        "funnel_id": funnel_id or "—",
        "pipeline_profile": pipeline_profile or "—",
        "source_reference": source_ref or "—",
        "transcript_snippet": snippet or "—",
        "preview_url": preview_url,
        "clip_file": clip_file,
        "clip_path": str(
            clip.get("job_clip_path") or clip.get("clip_path") or clip_file or ""
        ).strip()
        or "—",
        "clip_validation": clip.get("clip_validation"),
        "completed_at": job.get("completed_at") or job.get("created_at"),
        "input_id": str(job.get("input_id") or ""),
        "can_requeue": bool(job.get("input_id")),
    }


def load_review_queue(
    settings: Settings,
    store: Any,
    *,
    status_filter: str = "",
    job_limit: int = 60,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    svc = next((s for s in settings.services if s.key == "video-automation"), None)
    if svc is None:
        return {
            "ok": False,
            "error": "video-automation is not configured",
            "clips": [],
            "counts": _empty_counts(),
        }
    timeout = timeout_sec if timeout_sec is not None else max(settings.service_timeout_sec, 8.0)
    ok, payload, _status = call_json(svc, f"/jobs?limit={job_limit}", timeout=timeout)
    if not ok:
        return {
            "ok": False,
            "error": str(payload.get("error") or "could not list jobs"),
            "clips": [],
            "counts": _empty_counts(),
        }
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else []
    clips: list[dict[str, Any]] = []
    for summary in jobs:
        if not isinstance(summary, dict):
            continue
        if str(summary.get("status") or "").lower() != "success":
            continue
        if int(summary.get("clip_count") or 0) <= 0:
            continue
        job_id = str(summary.get("job_id") or "").strip()
        if not job_id:
            continue
        job_ok, job_payload, _ = call_json(svc, f"/jobs/{job_id}", timeout=timeout)
        if not job_ok:
            continue
        job_clips = job_payload.get("clips") if isinstance(job_payload.get("clips"), list) else []
        input_id = str(job_payload.get("input_id") or "").strip()
        input_source = load_input_ledger_record(input_id) if input_id else None
        for clip in job_clips:
            if not isinstance(clip, dict):
                continue
            clip_id = str(clip.get("clip_id") or clip.get("clip_index") or "").strip()
            if not clip_id:
                clip_id = f"{job_id}_clip_{clip.get('clip_index', 0)}"
            review = store.get_clip_review(job_id, clip_id)
            row = enrich_clip_row(
                job_id=job_id,
                job=job_payload,
                clip=clip,
                review=review,
                input_source=input_source,
                video_svc=svc,
            )
            clips.append(row)
    clips.sort(key=lambda row: str(row.get("completed_at") or ""), reverse=True)
    counts = _count_by_status(clips)
    filtered = _filter_clips(clips, status_filter)
    return {
        "ok": True,
        "error": "",
        "clips": filtered,
        "counts": counts,
        "total_clips": len(clips),
    }


def load_clip_inspection(
    settings: Settings,
    store: Any,
    *,
    job_id: str,
    clip_id: str,
    timeout_sec: float | None = None,
) -> dict[str, Any] | None:
    svc = next((s for s in settings.services if s.key == "video-automation"), None)
    if svc is None:
        return None
    timeout = timeout_sec if timeout_sec is not None else max(settings.service_timeout_sec, 10.0)
    ok, debug, _status = call_json(svc, f"/jobs/{job_id}/debug", timeout=timeout)
    if not ok:
        return None
    job = debug.get("job") if isinstance(debug.get("job"), dict) else {}
    clips = debug.get("clips") if isinstance(debug.get("clips"), list) else []
    match = None
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        cid = str(clip.get("clip_id") or clip.get("clip_index") or "").strip()
        if cid == clip_id or str(clip.get("clip_index")) == clip_id:
            match = clip
            break
    if match is None:
        return None
    input_id = str(job.get("input_id") or debug.get("input_id") or "").strip()
    input_source = load_input_ledger_record(input_id) if input_id else None
    review = store.get_clip_review(job_id, clip_id)
    row = enrich_clip_row(
        job_id=job_id,
        job=job,
        clip=match,
        review=review,
        debug=debug,
        input_source=input_source,
        video_svc=svc,
    )
    selection = debug.get("selection_summary") if isinstance(debug.get("selection_summary"), dict) else {}
    row["selection_summary"] = selection
    row["selector"] = debug.get("selector") if isinstance(debug.get("selector"), dict) else {}
    return row


def submit_operator_feedback(
    settings: Settings,
    *,
    job_id: str,
    clip_id: str,
    notes: str,
    review_status: str,
    flagged: bool,
) -> tuple[bool, str]:
    svc = next((s for s in settings.services if s.key == "video-automation"), None)
    if svc is None:
        return False, "video-automation is not configured"
    payload = {
        "job_id": job_id,
        "clip_id": clip_id,
        "notes": notes,
        "metrics": {
            "operator_review_status": review_status,
            "flagged_high_quality": flagged,
        },
    }
    ok, body, _status = call_json(
        svc,
        "/analytics/feedback",
        method="POST",
        payload=payload,
        timeout=max(settings.service_timeout_sec, 5.0),
    )
    if ok and body.get("success"):
        return True, str(body.get("feedback_event_id") or "recorded")
    return False, str(body.get("error") or body.get("message") or "feedback failed")


def _empty_counts() -> dict[str, int]:
    return {
        "pending": 0,
        "approved": 0,
        "rejected": 0,
        "flagged": 0,
        "all": 0,
    }


def _count_by_status(clips: list[dict[str, Any]]) -> dict[str, int]:
    counts = _empty_counts()
    for clip in clips:
        status = str(clip.get("review_status") or REVIEW_PENDING)
        if status in {"pending", "approved", "rejected"}:
            counts[status] += 1
        if clip.get("flagged_high_quality") or status == REVIEW_FLAGGED:
            counts["flagged"] += 1
        counts["all"] += 1
    return counts


def _filter_clips(clips: list[dict[str, Any]], status_filter: str) -> list[dict[str, Any]]:
    clean = str(status_filter or "").strip().lower()
    if not clean or clean == "all":
        return clips
    if clean == "awaiting":
        return [c for c in clips if str(c.get("review_status")) == REVIEW_PENDING]
    if clean == "flagged":
        return [
            c
            for c in clips
            if c.get("flagged_high_quality") or str(c.get("review_status")) == REVIEW_FLAGGED
        ]
    return [c for c in clips if str(c.get("review_status")) == clean]
