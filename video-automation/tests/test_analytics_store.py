import json
import os
import sys

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from analytics_store import (  # noqa: E402
    ANALYTICS_SCHEMA_VERSION,
    build_feedback_event,
    build_run_analytics,
    persist_feedback_event,
    persist_run_analytics,
)


def _sample_report() -> dict:
    return {
        "job_id": "job_abc",
        "status": "success",
        "created_at": "2026-05-11T00:00:00+00:00",
        "completed_at": "2026-05-11T00:01:00+00:00",
        "input_video_name": "source.mp4",
        "input_video_path": "/tmp/source.mp4",
        "video_duration_sec": 120.0,
        "stage_timings_ms": {"selection_ms": 12},
        "policy_resolution": {
            "selection_resolved": {"max_clips": 1},
            "models_resolved": {"selection_model": "gpt-4o-mini"},
            "pipeline_profile_resolved": "business_podcasts_001",
        },
        "clips": [
            {
                "clip_id": "job_abc_clip_01",
                "start": "00:00:10.000",
                "end": "00:00:40.000",
                "duration_sec": 30.0,
                "title": "A strong clip",
                "hook": "Watch this",
                "caption": "Caption",
                "reason": "Good hook",
                "scores": {"hook_strength": 8},
                "composite_score": 8.5,
                "clip_file": "clip.mp4",
                "clip_url": "/output/clip.mp4",
                "clip_validation": {"ok": True, "ffprobe_duration_sec": 30.0},
            }
        ],
    }


def test_build_run_analytics_preserves_learning_metadata():
    snapshot = build_run_analytics(_sample_report())

    assert snapshot["schema_version"] == ANALYTICS_SCHEMA_VERSION
    assert snapshot["job_id"] == "job_abc"
    assert snapshot["selection_context"]["pipeline_profile_resolved"] == "business_podcasts_001"
    clip = snapshot["clips"][0]
    assert clip["clip_id"] == "job_abc_clip_01"
    assert clip["selection"]["scores"] == {"hook_strength": 8}
    assert clip["selection"]["composite_score"] == 8.5
    assert clip["output"]["clip_validation"]["ok"] is True
    assert clip["feedback_join_key"]["job_id"] == "job_abc"


def test_persist_run_analytics_writes_snapshot_and_jsonl(tmp_path):
    analytics_root = tmp_path / "analytics"
    job_path = tmp_path / "job" / "analytics.json"

    paths = persist_run_analytics(
        report=_sample_report(),
        analytics_root=str(analytics_root),
        job_analytics_path=str(job_path),
    )

    assert job_path.exists()
    assert os.path.isfile(paths["events_jsonl"])
    snapshot = json.loads(job_path.read_text(encoding="utf-8"))
    assert snapshot["clip_count"] == 1
    events = [
        json.loads(line)
        for line in open(paths["events_jsonl"], encoding="utf-8")
        if line.strip()
    ]
    assert [e["event_type"] for e in events] == [
        "pipeline_run_completed",
        "clip_generated",
    ]


def test_feedback_event_requires_join_keys_and_persists(tmp_path):
    payload = {
        "job_id": "job_abc",
        "clip_id": "job_abc_clip_01",
        "platform": "tiktok",
        "metrics": {"views": 1234, "completion_rate": 0.42},
    }

    event = build_feedback_event(payload)
    assert event["event_type"] == "clip_feedback_observed"
    assert event["metrics"]["views"] == 1234

    stored = persist_feedback_event(payload=payload, analytics_root=str(tmp_path))
    assert stored["job_id"] == "job_abc"
    lines = (tmp_path / "feedback.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["clip_id"] == "job_abc_clip_01"
