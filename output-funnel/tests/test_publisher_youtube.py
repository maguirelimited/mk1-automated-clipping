from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from output_funnel.adapters.base import PlatformAdapter
from output_funnel.models import PublishResult, UploadStatus
from output_funnel.publisher import publish_one_job
from output_funnel.registry import register_job_payload
from output_funnel.service import register_and_process_from_payload, schedule_upload_job
from output_funnel.store import OutputStore
from output_funnel.time_utils import to_utc_iso


PROFILE = {
    "channel_id": "yt_business",
    "brand_name": "Business Shorts",
    "platform": "youtube_shorts",
    "enabled": True,
    "priority": 1,
    "routing": {"accepted_funnel_ids": ["business_clips_test"], "required_platform": "youtube_shorts"},
    "cadence": {
        "timezone": "UTC",
        "min_gap_minutes": 60,
        "max_uploads_per_day": 3,
        "default_lead_minutes": 180,
        "allowed_windows": [{"start": "00:00", "end": "23:59"}],
    },
    "metadata_style": {"privacy_status": "private", "default_hashtags": ["#Shorts"]},
}


class SuccessAdapter(PlatformAdapter):
    platform = "youtube_shorts"

    def publish(self, upload_job, source_clip, profile):
        return PublishResult(
            ok=True,
            status=UploadStatus.SCHEDULED_ON_PLATFORM,
            platform_asset_id="yt_123",
            scheduled_at=upload_job["platform_publish_at"],
            response={"id": "yt_123"},
        )


class RetryableAdapter(PlatformAdapter):
    platform = "youtube_shorts"

    def publish(self, upload_job, source_clip, profile):
        return PublishResult(
            ok=False,
            status=UploadStatus.FAILED_RETRYABLE,
            error_category="temporary",
            error_message="temporary outage",
            retryable=True,
        )


def _scheduled_job(monkeypatch, tmp_path: Path) -> tuple[OutputStore, int]:
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
                    "title": "Title",
                    "hook": "Hook",
                    "caption": "Caption",
                    "clip_validation": {"ok": True},
                    "funnel_id": "business_clips_test",
                }
            ],
            "enabled_platforms": ["youtube_shorts"],
        },
    )
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    schedule_upload_job(
        upload_job_id,
        store=store,
        profiles=[PROFILE],
        settings={
            "scheduler": {
                "default_timezone": "UTC",
                "default_lead_minutes": 180,
                "default_min_gap_minutes": 60,
                "default_max_uploads_per_day": 3,
            },
            "youtube": {
                "scheduled_publish_mode": "platform_native",
                "upload_timing": "lead_window",
                "upload_lead_minutes": 90,
                "upload_safety_buffer_minutes": 20,
            },
        },
    )
    publish_future = to_utc_iso(datetime.now(UTC) + timedelta(hours=4))
    upload_future = to_utc_iso(datetime.now(UTC) + timedelta(hours=2))
    deadline = to_utc_iso(datetime.now(UTC) + timedelta(hours=3, minutes=40))
    store.update_upload_job(
        upload_job_id,
        status=UploadStatus.UPLOADING,
        publish_at=publish_future,
        platform_publish_at=publish_future,
        upload_at=upload_future,
        scheduled_at=publish_future,
        upload_deadline=deadline,
    )
    return store, upload_job_id


def test_publish_success_records_platform_asset_id(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MK04_ENV", "prod")
    monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
    store, upload_job_id = _scheduled_job(monkeypatch, tmp_path)

    result = publish_one_job(
        store,
        upload_job_id,
        profiles=[PROFILE],
        adapters={"youtube_shorts": SuccessAdapter()},
    )

    assert result["published"] is True
    assert result["uploaded"] is True
    job = store.get_upload_job(upload_job_id)
    assert job["status"] == "uploaded_scheduled"
    assert job["platform_video_id"] == "yt_123"
    assert job["platform_asset_id"] == "yt_123"
    assert job["uploaded_at"]
    assert len(store.attempts_for_job(upload_job_id)) == 1


def test_publish_retryable_failure_records_attempt(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MK04_ENV", "prod")
    monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
    store, upload_job_id = _scheduled_job(monkeypatch, tmp_path)

    result = publish_one_job(
        store,
        upload_job_id,
        profiles=[PROFILE],
        adapters={"youtube_shorts": RetryableAdapter()},
        max_attempts=3,
    )

    assert result["published"] is False
    assert result["uploaded"] is False
    job = store.get_upload_job(upload_job_id)
    assert job["status"] == "failed_retryable"
    attempts = store.attempts_for_job(upload_job_id)
    assert attempts[0]["error_message"] == "temporary outage"


def test_registration_auto_publish_requires_explicit_enable(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    monkeypatch.setattr("output_funnel.service.load_channel_profiles", lambda: [PROFILE])
    monkeypatch.setattr(
        "output_funnel.publisher.default_adapters",
        lambda: {"youtube_shorts": SuccessAdapter()},
    )
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()

    result = register_and_process_from_payload(
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
                    "title": "Title",
                    "hook": "Hook",
                    "caption": "Caption",
                    "clip_validation": {"ok": True},
                    "funnel_id": "business_clips_test",
                }
            ],
            "enabled_platforms": ["youtube_shorts"],
        },
        store=store,
        settings={
            "database_path": str(tmp_path / "queue.sqlite3"),
            "preflight": {"duration_tolerance_sec": 1.0},
            "youtube": {
                "scheduled_publish_mode": "platform_native",
                "upload_timing": "lead_window",
                "upload_lead_minutes": 90,
                "upload_safety_buffer_minutes": 20,
            },
            "automation": {"auto_schedule": True, "auto_publish": True, "publish_limit": 1},
            "publisher": {"max_attempts": 3},
        },
    )

    assert result["processing"]["auto_publish_enabled"] is True
    assert result["processing"]["auto_upload_enabled"] is True
    assert result["processing"]["publish"]["count"] == 0
    assert result["processing"]["upload"]["count"] == 0
    job = store.list_upload_jobs()[0]
    assert job["status"] == "planned"
    assert job["platform_asset_id"] is None
    assert job["uploaded_at"] is None
    assert job["publish_at"]
    assert job["upload_at"]
    assert job["upload_deadline"]
