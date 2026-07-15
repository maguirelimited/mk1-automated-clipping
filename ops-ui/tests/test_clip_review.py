from __future__ import annotations

import json
from pathlib import Path

from ops_ui.app import create_app
from ops_ui.clip_review import (
    REVIEW_APPROVED,
    enrich_clip_row,
    transcript_snippet_for_clip,
    platform_targets_for_job,
)
from ops_ui.config import ServiceConfig, Settings
from ops_ui.store import ControlStore


def test_transcript_snippet_for_clip_overlaps_segments() -> None:
    segments = [
        {"start": 0.0, "end": 5.0, "text": "Intro line."},
        {"start": 5.0, "end": 12.0, "text": "Core insight here."},
        {"start": 20.0, "end": 30.0, "text": "Later topic."},
    ]
    snippet = transcript_snippet_for_clip(
        segments,
        start="00:00:04.000",
        end="00:00:10.000",
    )
    assert "Intro line" in snippet
    assert "Core insight" in snippet
    assert "Later topic" not in snippet


def test_enrich_clip_row_includes_quality_fields() -> None:
    row = enrich_clip_row(
        job_id="job_test",
        job={"input_id": "input_1", "completed_at": "2026-05-23T00:00:00Z"},
        clip={
            "clip_id": "job_test_clip_01",
            "title": "Great moment",
            "hook": "Stop scrolling",
            "reason": "Strong opener",
            "start": "00:00:01.000",
            "end": "00:00:31.000",
            "duration_sec": 30,
            "composite_score": 8.4,
            "clip_file": "clip_01.mp4",
        },
        review={"status": REVIEW_APPROVED, "flagged_high_quality": True, "feedback_notes": "ship it"},
        input_source={
            "available": True,
            "source_url": "https://example.com/watch?v=abc",
            "record": {
                "funnel_policy": {
                    "posting_config": {"platforms": ["youtube_shorts"]},
                }
            },
        },
    )
    assert row["review_status"] == REVIEW_APPROVED
    assert row["hook"] == "Stop scrolling"
    assert row["reason"] == "Strong opener"
    assert "youtube_shorts" in row["platform_targets"]
    assert row["source_reference"].startswith("https://")


def test_clip_review_page_and_store(tmp_path: Path) -> None:
    settings = Settings(
        host="127.0.0.1",
        port=5070,
        data_dir=tmp_path,
        control_db_path=tmp_path / "ops.sqlite3",
        controls_file=tmp_path / "controls.json",
        service_timeout_sec=0.01,
        journal_lines=1,
        funnel_run_timeout_sec=1.0,
        stuck_running_sec=7200.0,
        stuck_queued_sec=1800.0,
        stuck_uploading_sec=1800.0,
        services=(
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-video-automation.service",
            ),
        ),
    )
    app = create_app(settings)
    client = app.test_client()
    store = ControlStore(settings.control_db_path, controls_file=settings.controls_file)
    store.init_db()
    store.set_clip_review("job_a", "clip_1", status="approved")

    response = client.get("/clip-review", follow_redirects=False)
    assert response.status_code in {302, 301}
    assert "/ops/outputs" in (response.headers.get("Location") or "")

    controls = json.loads(settings.controls_file.read_text(encoding="utf-8"))
    assert "human_approval_required" in controls


def test_clip_review_post_approve_returns_gone(tmp_path: Path) -> None:
    settings = Settings(
        host="127.0.0.1",
        port=5070,
        data_dir=tmp_path,
        control_db_path=tmp_path / "ops.sqlite3",
        controls_file=tmp_path / "controls.json",
        service_timeout_sec=0.01,
        journal_lines=1,
        funnel_run_timeout_sec=1.0,
        stuck_running_sec=7200.0,
        stuck_queued_sec=1800.0,
        stuck_uploading_sec=1800.0,
        services=(
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-video-automation.service",
            ),
        ),
    )
    app = create_app(settings)
    client = app.test_client()
    store = ControlStore(settings.control_db_path, controls_file=settings.controls_file)
    store.init_db()

    response = client.post("/clip-review/job_a/clip_1/approve", follow_redirects=False)
    assert response.status_code == 410
    assert "retired" in (response.get_data(as_text=True) or "").lower()
    assert store.get_clip_review("job_a", "clip_1") is None


def test_clip_review_post_reject_and_flag_return_gone(tmp_path: Path) -> None:
    settings = Settings(
        host="127.0.0.1",
        port=5070,
        data_dir=tmp_path,
        control_db_path=tmp_path / "ops.sqlite3",
        controls_file=tmp_path / "controls.json",
        service_timeout_sec=0.01,
        journal_lines=1,
        funnel_run_timeout_sec=1.0,
        stuck_running_sec=7200.0,
        stuck_queued_sec=1800.0,
        stuck_uploading_sec=1800.0,
        services=(
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-video-automation.service",
            ),
        ),
    )
    app = create_app(settings)
    client = app.test_client()

    for path in (
        "/clip-review/job_a/clip_1/reject",
        "/clip-review/job_a/clip_1/flag",
    ):
        response = client.post(path, follow_redirects=False)
        assert response.status_code == 410
        assert "retired" in (response.get_data(as_text=True) or "").lower()


def test_clip_review_policy_control_toggle_returns_gone(tmp_path: Path) -> None:
    settings = Settings(
        host="127.0.0.1",
        port=5070,
        data_dir=tmp_path,
        control_db_path=tmp_path / "ops.sqlite3",
        controls_file=tmp_path / "controls.json",
        service_timeout_sec=0.01,
        journal_lines=1,
        funnel_run_timeout_sec=1.0,
        stuck_running_sec=7200.0,
        stuck_queued_sec=1800.0,
        stuck_uploading_sec=1800.0,
        services=(
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-video-automation.service",
            ),
        ),
    )
    app = create_app(settings)
    client = app.test_client()
    store = ControlStore(settings.control_db_path, controls_file=settings.controls_file)
    store.init_db()
    before = json.loads(settings.controls_file.read_text(encoding="utf-8"))

    for control in ("human_approval_required", "publish_approved_only"):
        response = client.post(f"/clip-review/controls/{control}/on", follow_redirects=False)
        assert response.status_code == 410
        assert "retired" in (response.get_data(as_text=True) or "").lower()

    after = json.loads(settings.controls_file.read_text(encoding="utf-8"))
    assert after == before
