from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from output_funnel.registry import register_job_payload
from output_funnel.scheduler import next_scheduled_time
from output_funnel.service import (
    register_and_process_from_payload,
    route_and_prepare_upload_job,
    schedule_due_upload_jobs,
    schedule_upload_job,
)
from output_funnel.store import OutputStore


PROFILE = {
    "channel_id": "yt_business",
    "brand_name": "Business Shorts",
    "platform": "youtube_shorts",
    "enabled": True,
    "priority": 1,
    "routing": {
        "accepted_funnel_ids": ["business_clips_test"],
        "min_composite_score": 5,
        "required_platform": "youtube_shorts",
    },
    "cadence": {
        "timezone": "UTC",
        "min_gap_minutes": 180,
        "max_uploads_per_day": 2,
        "default_lead_minutes": 60,
        "allowed_windows": [{"start": "09:00", "end": "21:00"}],
    },
    "metadata_style": {
        "default_hashtags": ["#Shorts", "#Business", "#ThisTagIsWayTooLongButStillValid"],
        "max_hashtags": 2,
        "description_template": "{caption}\n\n{hashtags}",
        "privacy_status": "private",
    },
}


def _store_with_job(monkeypatch, tmp_path: Path) -> tuple[OutputStore, int]:
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(
        store,
        {
            "job_id": "job_20260521T120000Z_deadbeef",
            "status": "success",
            "clips": [
                {
                    "clip_id": "clip_1",
                    "start": "00:00:01.000",
                    "end": "00:00:31.000",
                    "duration_sec": 30.0,
                    "job_clip_path": str(clip_path),
                    "title": "A title " * 30,
                    "hook": "Hook",
                    "caption": "Caption",
                    "composite_score": 8.0,
                    "clip_validation": {"ok": True},
                    "funnel_id": "business_clips_test",
                }
            ],
            "enabled_platforms": ["youtube_shorts"],
        },
    )
    return store, int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])


def _job_payload(clip_path: Path) -> dict:
    return {
        "job_id": "job_20260521T120000Z_deadbeef",
        "status": "success",
        "clips": [
            {
                "clip_id": "clip_1",
                "start": "00:00:01.000",
                "end": "00:00:31.000",
                "duration_sec": 30.0,
                "job_clip_path": str(clip_path),
                "title": "A title " * 30,
                "hook": "Hook",
                "caption": "Caption",
                "composite_score": 8.0,
                "clip_validation": {"ok": True},
                "funnel_id": "business_clips_test",
            }
        ],
        "enabled_platforms": ["youtube_shorts"],
    }


def test_route_normalizes_metadata_without_mutating_source(monkeypatch, tmp_path: Path):
    store, upload_job_id = _store_with_job(monkeypatch, tmp_path)
    source_before = store.get_source_clip(int(store.get_upload_job(upload_job_id)["clip_pk"]))

    result = route_and_prepare_upload_job(upload_job_id, store=store, profiles=[PROFILE])

    assert result["routed"] is True
    job = store.get_upload_job(upload_job_id)
    assert job["channel_id"] == "yt_business"
    assert len(job["normalized_title"]) <= 100
    assert job["normalized_hashtags"] == ["#Shorts", "#Business"]
    assert store.get_source_clip(int(job["clip_pk"])) == source_before


def test_schedule_assigns_staggered_future_slot(monkeypatch, tmp_path: Path):
    store, upload_job_id = _store_with_job(monkeypatch, tmp_path)

    result = schedule_upload_job(upload_job_id, store=store, profiles=[PROFILE])

    assert result["planned"] is True
    assert result["scheduled"] is True
    job = store.get_upload_job(upload_job_id)
    assert job["status"] == "planned"
    assert job["publish_at"] == result["publish_at"]
    assert job["platform_publish_at"] == result["publish_at"]
    assert job["upload_at"] == result["upload_at"]
    assert job["upload_deadline"] == result["upload_deadline"]
    assert job["upload_at"] < job["publish_at"]
    assert job["upload_deadline"] < job["publish_at"]
    assert job["upload_at"] <= job["upload_deadline"]
    assert job["scheduled_at"] == job["publish_at"]


def test_registration_auto_schedules_but_does_not_publish_by_default(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    monkeypatch.setattr("output_funnel.service.load_channel_profiles", lambda: [PROFILE])
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()

    result = register_and_process_from_payload(
        _job_payload(clip_path),
        store=store,
        settings={
            "database_path": str(tmp_path / "queue.sqlite3"),
            "preflight": {"duration_tolerance_sec": 1.0},
            "automation": {"auto_schedule": True, "auto_publish": False},
        },
    )

    assert result["processing"]["auto_schedule_enabled"] is True
    assert result["processing"]["auto_publish_enabled"] is False
    assert result["processing"]["auto_upload_enabled"] is False
    job = store.list_upload_jobs()[0]
    assert job["status"] == "planned"
    assert job["platform_asset_id"] is None
    assert job["uploaded_at"] is None
    assert job["publish_at"]
    assert job["upload_at"]


def test_registration_auto_schedule_can_be_disabled(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    monkeypatch.setattr("output_funnel.service.load_channel_profiles", lambda: [PROFILE])
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()

    result = register_and_process_from_payload(
        _job_payload(clip_path),
        store=store,
        settings={
            "database_path": str(tmp_path / "queue.sqlite3"),
            "preflight": {"duration_tolerance_sec": 1.0},
            "automation": {"auto_schedule": False, "auto_publish": False},
        },
    )

    assert result["processing"]["auto_schedule_enabled"] is False
    assert "schedule" not in result["processing"]
    assert store.list_upload_jobs()[0]["status"] == "registered"


def test_batch_schedule_uses_configured_limit(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    monkeypatch.setattr("output_funnel.service.load_channel_profiles", lambda: [PROFILE])
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    for index in range(3):
        clip_path = tmp_path / f"clip_{index}.mp4"
        clip_path.write_bytes(b"fake-video")
        payload = _job_payload(clip_path)
        payload["job_id"] = f"job_20260521T12000{index}Z_deadbeef"
        payload["clips"][0]["clip_id"] = f"clip_{index}"
        register_job_payload(store, payload)

    result = schedule_due_upload_jobs(
        store=store,
        settings={
            "database_path": str(tmp_path / "queue.sqlite3"),
            "automation": {"schedule_limit": 2},
        },
    )

    assert result["count"] == 2
    jobs = store.list_upload_jobs(limit=10)
    assert sum(1 for job in jobs if job["status"] == "planned") == 2
    assert sum(1 for job in jobs if job["status"] == "registered") == 1


def test_scheduler_defaults_fill_missing_channel_cadence():
    profile = {
        "channel_id": "yt_business",
        "platform": "youtube_shorts",
        "enabled": True,
        "cadence": {"allowed_windows": [{"start": "00:00", "end": "23:59"}]},
    }

    scheduled = next_scheduled_time(
        profile,
        [],
        defaults={
            "default_timezone": "UTC",
            "default_lead_minutes": 30,
            "default_min_gap_minutes": 45,
            "default_max_uploads_per_day": 1,
        },
        now=datetime(2026, 5, 21, 8, 0, tzinfo=UTC),
    )

    assert scheduled == "2026-05-21T08:30:00Z"


def test_next_scheduled_time_respects_gap_and_day_limit():
    existing = ["2026-05-21T09:00:00Z", "2026-05-21T12:00:00Z"]

    scheduled = next_scheduled_time(
        PROFILE,
        existing,
        now=datetime(2026, 5, 21, 8, 0, tzinfo=UTC),
    )

    assert scheduled == "2026-05-22T09:00:00Z"
