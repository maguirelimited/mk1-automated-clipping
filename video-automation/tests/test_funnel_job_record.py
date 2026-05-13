"""Funnel metadata on job reports / API shape (no full pipeline run)."""

import os
import sys

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from mk04_utils import build_funnel_job_record, write_review  # noqa: E402


def test_build_funnel_job_record_includes_resolved_fields():
    audit = {
        "funnel_resolution": {
            "funnel_resolve_source": "defaults.default_funnel_id",
            "funnel_config_applied": True,
            "funnel_config_path": "/abs/config/funnels/demo.json",
        },
        "pipeline_profile_resolved": "pf1",
        "selection_key_sources": {"max_clips": "funnel_config"},
    }
    funnel_ops = {
        "funnel_id": "demo",
        "funnel_name": "Demo funnel",
        "platforms": {"tiktok": True, "instagram_reels": False, "youtube_shorts": False, "x": False},
        "output": {"filename_prefix": "acme", "delivery_mode": "pull_from_output_endpoint"},
    }
    sel = {
        "max_clips": 4,
        "min_duration_sec": 10.0,
        "max_duration_sec": 55.0,
        "max_overlap_sec": 1.0,
        "include_reasons": False,
        "include_clip_metadata": True,
    }
    rec = build_funnel_job_record(
        funnel_ops=funnel_ops, resolved_selection=sel, policy_audit=audit
    )
    assert rec is not None
    assert rec["funnel_id"] == "demo"
    assert rec["enabled_platforms"] == ["tiktok"]
    assert rec["resolved_selection"]["max_clips"] == 4
    assert rec["resolved_output"]["filename_prefix"] == "acme"
    assert rec["funnel_policy_summary"]["funnel_config_path"].endswith("demo.json")


def test_build_funnel_job_record_returns_none_without_funnel():
    assert (
        build_funnel_job_record(
            funnel_ops=None,
            resolved_selection={"max_clips": 1, "min_duration_sec": 1, "max_duration_sec": 2, "max_overlap_sec": 0},
            policy_audit={},
        )
        is None
    )


def test_write_review_includes_funnel_section(tmp_path):
    report = {
        "job_id": "j1",
        "input_video_name": "v.mp4",
        "status": "success",
        "clips": [],
        "funnel": build_funnel_job_record(
            funnel_ops={
                "funnel_id": "f1",
                "funnel_name": "F one",
                "platforms": {"tiktok": True, "instagram_reels": True, "youtube_shorts": False, "x": False},
                "output": {"filename_prefix": "pfx", "delivery_mode": "pull_from_output_endpoint"},
            },
            resolved_selection={
                "max_clips": 2,
                "min_duration_sec": 5.0,
                "max_duration_sec": 30.0,
                "max_overlap_sec": 0.0,
                "include_reasons": False,
                "include_clip_metadata": True,
            },
            policy_audit={
                "funnel_resolution": {
                    "funnel_resolve_source": "http_funnel_id",
                    "funnel_config_path": "/x/f1.json",
                }
            },
        ),
    }
    path = str(tmp_path / "review.md")
    write_review(path, report)
    text = open(path, encoding="utf-8").read()
    assert "## Funnel" in text
    assert "f1" in text
    assert "F one" in text
    assert "tiktok" in text and "instagram_reels" in text
    assert "max_clips: `2`" in text
    assert "filename_prefix: `pfx`" in text
