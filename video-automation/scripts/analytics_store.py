"""Lightweight analytics event store for future closed-loop optimisation.

This module deliberately avoids a database in mk0.4. It writes:
* per-job ``analytics.json`` snapshots beside ``report.json``
* append-only JSONL events under configured ``paths.analytics_folder``

Future jobs can backfill performance metrics by appending feedback events that
reference ``job_id`` + ``clip_id``.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any


ANALYTICS_SCHEMA_VERSION = "mk0.4.analytics.v1"
RUN_EVENT_TYPE = "pipeline_run_completed"
CLIP_EVENT_TYPE = "clip_generated"
FEEDBACK_EVENT_TYPE = "clip_feedback_observed"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _atomic_write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=os.path.dirname(path),
        prefix=".analytics.",
        suffix=".tmp",
        delete=False,
    ) as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        tmp_path = f.name
    os.replace(tmp_path, path)


def _append_jsonl(path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def analytics_paths(analytics_root: str) -> dict[str, str]:
    root = os.path.abspath(analytics_root)
    return {
        "root": root,
        "events_jsonl": os.path.join(root, "events.jsonl"),
        "feedback_jsonl": os.path.join(root, "feedback.jsonl"),
    }


def clip_id_for(job_id: str, index: int) -> str:
    return f"{job_id}_clip_{index:02d}"


def build_clip_analytics(job_id: str, index: int, clip: dict[str, Any]) -> dict[str, Any]:
    clip_id = str(clip.get("clip_id") or clip_id_for(job_id, index))
    selection_keys = (
        "start",
        "end",
        "duration_sec",
        "title",
        "hook",
        "caption",
        "reason",
        "scores",
        "composite_score",
    )
    output_keys = (
        "clip_file",
        "clip_url",
        "clip_path",
        "job_clip_path",
        "clip_validation",
    )
    return {
        "schema_version": ANALYTICS_SCHEMA_VERSION,
        "event_type": CLIP_EVENT_TYPE,
        "clip_id": clip_id,
        "job_id": job_id,
        "clip_index": index,
        "selection": {k: _jsonable(clip.get(k)) for k in selection_keys if k in clip},
        "output": {k: _jsonable(clip.get(k)) for k in output_keys if k in clip},
        "feedback_join_key": {
            "job_id": job_id,
            "clip_id": clip_id,
            "clip_file": clip.get("clip_file"),
        },
    }


def build_run_analytics(report: dict[str, Any]) -> dict[str, Any]:
    job_id = str(report.get("job_id") or "")
    clips_raw = report.get("clips") or []
    clips = [
        build_clip_analytics(job_id, idx, dict(clip))
        for idx, clip in enumerate(clips_raw, start=1)
        if isinstance(clip, dict)
    ]
    policy = report.get("policy_resolution") if isinstance(report.get("policy_resolution"), dict) else {}
    return {
        "schema_version": ANALYTICS_SCHEMA_VERSION,
        "event_type": RUN_EVENT_TYPE,
        "job_id": job_id,
        "status": report.get("status"),
        "created_at": report.get("created_at"),
        "completed_at": report.get("completed_at"),
        "source_video": {
            "name": report.get("input_video_name"),
            "path": report.get("input_video_path"),
            "duration_sec": report.get("video_duration_sec"),
        },
        "pipeline": {
            "policy_resolution": policy,
            "stage_timings_ms": report.get("stage_timings_ms") or {},
            "chunked": bool(report.get("chunked")),
            "chunking": report.get("chunking") or None,
            "warnings": report.get("warnings") or [],
            "errors": report.get("errors") or [],
        },
        "selection_context": {
            "selection_resolved": policy.get("selection_resolved") if isinstance(policy, dict) else None,
            "models_resolved": policy.get("models_resolved") if isinstance(policy, dict) else None,
            "pipeline_profile_resolved": policy.get("pipeline_profile_resolved") if isinstance(policy, dict) else None,
        },
        "clips": clips,
        "clip_count": len(clips),
        "feedback_contract": {
            "join_keys": ["job_id", "clip_id"],
            "feedback_endpoint": "/analytics/feedback",
            "metrics_object_reserved_for": [
                "views",
                "likes",
                "comments",
                "shares",
                "saves",
                "watch_time_sec",
                "avg_view_duration_sec",
                "completion_rate",
            ],
        },
    }


def persist_run_analytics(
    *,
    report: dict[str, Any],
    analytics_root: str,
    job_analytics_path: str,
) -> dict[str, str]:
    paths = analytics_paths(analytics_root)
    snapshot = build_run_analytics(report)
    _atomic_write_json(job_analytics_path, snapshot)

    run_event = dict(snapshot)
    run_event["stored_at"] = now_iso()
    # Avoid duplicating full clip payload in the run-level JSONL event; each
    # clip has its own append-only event.
    run_event["clips"] = [
        {"clip_id": c["clip_id"], "clip_index": c["clip_index"]} for c in snapshot["clips"]
    ]
    _append_jsonl(paths["events_jsonl"], run_event)

    for clip_event in snapshot["clips"]:
        event = dict(clip_event)
        event["stored_at"] = now_iso()
        _append_jsonl(paths["events_jsonl"], event)

    return {
        "analytics_path": job_analytics_path,
        "events_jsonl": paths["events_jsonl"],
    }


def build_feedback_event(payload: dict[str, Any]) -> dict[str, Any]:
    job_id = str(payload.get("job_id") or "").strip()
    clip_id = str(payload.get("clip_id") or "").strip()
    metrics = payload.get("metrics") or payload.get("performance") or {}
    if not job_id:
        raise ValueError("job_id is required")
    if not clip_id:
        raise ValueError("clip_id is required")
    if not isinstance(metrics, dict):
        raise ValueError("metrics/performance must be an object when provided")

    return {
        "schema_version": ANALYTICS_SCHEMA_VERSION,
        "event_type": FEEDBACK_EVENT_TYPE,
        "feedback_event_id": uuid.uuid4().hex,
        "observed_at": str(payload.get("observed_at") or now_iso()),
        "stored_at": now_iso(),
        "job_id": job_id,
        "clip_id": clip_id,
        "platform": str(payload.get("platform") or "").strip() or None,
        "posted_url": str(payload.get("posted_url") or "").strip() or None,
        "metrics": {str(k): _jsonable(v) for k, v in metrics.items()},
        "notes": str(payload.get("notes") or "").strip() or None,
        "raw": _jsonable(payload),
    }


def persist_feedback_event(*, payload: dict[str, Any], analytics_root: str) -> dict[str, Any]:
    event = build_feedback_event(payload)
    _append_jsonl(analytics_paths(analytics_root)["feedback_jsonl"], event)
    return event
