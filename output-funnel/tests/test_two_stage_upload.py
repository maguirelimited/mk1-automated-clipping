"""Tests for the two-stage YouTube scheduling architecture.

Stage 1 ("plan") picks ``publish_at`` (audience-facing release time) using
channel cadence rules, and derives ``upload_at`` / ``upload_deadline``
(when our system should attempt the actual upload).

Stage 2 ("upload-due") uploads jobs whose ``upload_at`` has been reached.
For YouTube-native scheduling this should happen as soon as practical after
validation while still setting YouTube's native ``publishAt``. Jobs whose
``upload_deadline`` has passed are marked as ``missed_upload_window`` instead
of uploading them late as if they were fresh posts.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from output_funnel.adapters.base import PlatformAdapter
from output_funnel.models import PublishResult, UploadStatus
from output_funnel.publisher import upload_due_jobs
from output_funnel.registry import register_job_payload
from output_funnel.service import (
    backfill_legacy_rows,
    compute_upload_window,
    plan_upload_job,
    publish_due,
    retry_upload_job,
    upload_due,
)
from output_funnel.store import OutputStore
from output_funnel.time_utils import to_utc_iso


PROFILE = {
    "channel_id": "yt_business",
    "brand_name": "Business Shorts",
    "platform": "youtube_shorts",
    "enabled": True,
    "priority": 1,
    "routing": {
        "accepted_funnel_ids": ["business_clips_test"],
        "min_composite_score": 0,
        "required_platform": "youtube_shorts",
    },
    "cadence": {
        "timezone": "UTC",
        "min_gap_minutes": 60,
        "max_uploads_per_day": 6,
        "default_lead_minutes": 240,
        "allowed_windows": [{"start": "00:00", "end": "23:59"}],
    },
    "metadata_style": {
        "default_hashtags": ["#Shorts"],
        "privacy_status": "private",
    },
}

SETTINGS = {
    "preflight": {"duration_tolerance_sec": 1.0},
    "scheduler": {
        "default_timezone": "UTC",
        "default_lead_minutes": 240,
        "default_min_gap_minutes": 60,
        "default_max_uploads_per_day": 6,
    },
    "youtube": {
        "scheduled_publish_mode": "platform_native",
        "upload_timing": "immediate_after_validation",
        "upload_lead_minutes": 90,
        "upload_safety_buffer_minutes": 20,
        "min_publish_at_lead_minutes": 15,
        "max_schedule_horizon_days": 14,
    },
    "publisher": {"max_attempts": 3},
    "automation": {"auto_schedule": True, "auto_upload": False},
}


class CaptureAdapter(PlatformAdapter):
    """Records the upload_job snapshot at the moment publish() is called."""

    platform = "youtube_shorts"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def publish(self, upload_job, source_clip, profile):
        self.calls.append({"upload_job": dict(upload_job), "profile": dict(profile)})
        return PublishResult(
            ok=True,
            status=UploadStatus.UPLOADED_SCHEDULED,
            platform_asset_id="yt_capture_123",
            scheduled_at=upload_job["platform_publish_at"],
            response={"id": "yt_capture_123"},
        )


def _payload(clip_path: Path) -> dict[str, Any]:
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
                "title": "Title",
                "hook": "Hook",
                "caption": "Caption",
                "clip_validation": {"ok": True},
                "funnel_id": "business_clips_test",
            }
        ],
        "enabled_platforms": ["youtube_shorts"],
    }


def _store_with_planned_job(monkeypatch, tmp_path: Path) -> tuple[OutputStore, int]:
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path))
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    plan_upload_job(upload_job_id, store=store, profiles=[PROFILE], settings=SETTINGS)
    return store, upload_job_id


def test_compute_upload_window_uploads_immediately_for_youtube_native_scheduling():
    publish_at = to_utc_iso(datetime.now(UTC) + timedelta(hours=4))
    upload_at, deadline = compute_upload_window(
        publish_at=publish_at,
        platform="youtube_shorts",
        settings=SETTINGS,
    )
    upload_dt = datetime.fromisoformat(upload_at.replace("Z", "+00:00"))
    assert upload_dt <= datetime.now(UTC) + timedelta(seconds=2)
    assert upload_at < deadline < publish_at


def test_compute_upload_window_preserves_legacy_lead_window_when_configured():
    publish_at = "2026-05-22T18:00:00Z"
    upload_at, deadline = compute_upload_window(
        publish_at=publish_at,
        platform="youtube_shorts",
        settings={
            "youtube": {
                "scheduled_publish_mode": "platform_native",
                "upload_timing": "lead_window",
                "upload_lead_minutes": 90,
                "upload_safety_buffer_minutes": 20,
            }
        },
    )
    assert upload_at == "2026-05-22T16:30:00Z"
    assert deadline == "2026-05-22T17:40:00Z"
    assert upload_at < deadline < publish_at


def test_compute_upload_window_respects_max_schedule_horizon_for_youtube():
    publish_at_dt = datetime.now(UTC) + timedelta(days=30)
    publish_at = to_utc_iso(publish_at_dt)
    upload_at, deadline = compute_upload_window(
        publish_at=publish_at,
        platform="youtube_shorts",
        settings={
            "youtube": {
                "scheduled_publish_mode": "platform_native",
                "upload_timing": "immediate_after_validation",
                "upload_safety_buffer_minutes": 20,
                "max_schedule_horizon_days": 14,
            }
        },
    )
    expected_earliest = publish_at_dt - timedelta(days=14)
    upload_dt = datetime.fromisoformat(upload_at.replace("Z", "+00:00"))
    assert abs((upload_dt - expected_earliest).total_seconds()) < 1
    assert upload_at < deadline < publish_at


def test_compute_upload_window_enforces_minimum_safety_buffer():
    publish_at = "2026-05-22T18:00:00Z"
    upload_at, deadline = compute_upload_window(
        publish_at=publish_at,
        platform="youtube_shorts",
        settings={"youtube": {"upload_lead_minutes": 5, "upload_safety_buffer_minutes": 1}},
    )
    delta = datetime.fromisoformat(publish_at.replace("Z", "+00:00")) - datetime.fromisoformat(
        deadline.replace("Z", "+00:00")
    )
    assert delta >= timedelta(minutes=20)
    assert upload_at < deadline


def test_plan_sets_publish_at_and_upload_at_and_status_planned(monkeypatch, tmp_path: Path):
    store, upload_job_id = _store_with_planned_job(monkeypatch, tmp_path)
    job = store.get_upload_job(upload_job_id)

    assert job["status"] == "planned"
    assert job["publish_at"]
    assert job["platform_publish_at"] == job["publish_at"]
    assert job["upload_at"]
    assert job["upload_deadline"]
    upload_dt = datetime.fromisoformat(job["upload_at"].replace("Z", "+00:00"))
    assert upload_dt <= datetime.now(UTC) + timedelta(seconds=2)
    assert job["upload_at"] < job["upload_deadline"] < job["publish_at"]
    assert job["scheduled_at"] == job["publish_at"], (
        "scheduled_at is a deprecated mirror of publish_at, not upload_at"
    )
    assert job["uploaded_at"] is None
    assert job["platform_video_id"] is None


def test_upload_due_does_not_claim_when_upload_at_in_future(monkeypatch, tmp_path: Path):
    store, _upload_job_id = _store_with_planned_job(monkeypatch, tmp_path)
    job = store.list_upload_jobs()[0]
    store.update_upload_job(
        int(job["id"]),
        upload_at=to_utc_iso(datetime.now(UTC) + timedelta(minutes=30)),
    )
    capture = CaptureAdapter()

    result = upload_due_jobs(
        store, profiles=[PROFILE], adapters={"youtube_shorts": capture}
    )

    assert result["count"] == 0
    assert capture.calls == []


def test_upload_due_claims_when_upload_at_has_passed(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MK04_ENV", "prod")
    monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
    store, upload_job_id = _store_with_planned_job(monkeypatch, tmp_path)
    publish_future = to_utc_iso(datetime.now(UTC) + timedelta(hours=2))
    deadline = to_utc_iso(datetime.now(UTC) + timedelta(hours=1, minutes=40))
    upload_ready = to_utc_iso(datetime.now(UTC) - timedelta(minutes=1))
    store.update_upload_job(
        upload_job_id,
        publish_at=publish_future,
        platform_publish_at=publish_future,
        upload_deadline=deadline,
        upload_at=upload_ready,
        scheduled_at=publish_future,
    )
    capture = CaptureAdapter()

    result = upload_due_jobs(
        store, profiles=[PROFILE], adapters={"youtube_shorts": capture}
    )

    assert result["uploaded"] == 1
    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert call["upload_job"]["platform_publish_at"] == publish_future
    job = store.get_upload_job(upload_job_id)
    assert job["status"] == "uploaded_scheduled"
    assert job["platform_video_id"] == "yt_capture_123"
    assert job["uploaded_at"]


def test_upload_due_marks_missed_when_deadline_has_passed(monkeypatch, tmp_path: Path):
    store, upload_job_id = _store_with_planned_job(monkeypatch, tmp_path)
    publish_past = to_utc_iso(datetime.now(UTC) - timedelta(minutes=5))
    store.update_upload_job(
        upload_job_id,
        publish_at=publish_past,
        platform_publish_at=publish_past,
        upload_at=to_utc_iso(datetime.now(UTC) - timedelta(hours=2)),
        scheduled_at=publish_past,
        upload_deadline=to_utc_iso(datetime.now(UTC) - timedelta(minutes=30)),
    )
    capture = CaptureAdapter()

    result = upload_due_jobs(
        store, profiles=[PROFILE], adapters={"youtube_shorts": capture}
    )

    assert capture.calls == []
    assert result["missed"] == 1
    assert result["uploaded"] == 0
    job = store.get_upload_job(upload_job_id)
    assert job["status"] == "missed_upload_window"


def test_legacy_publish_due_route_calls_upload_due(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MK04_ENV", "prod")
    monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
    store, upload_job_id = _store_with_planned_job(monkeypatch, tmp_path)
    upload_ready = to_utc_iso(datetime.now(UTC) - timedelta(minutes=1))
    publish_future = to_utc_iso(datetime.now(UTC) + timedelta(hours=2))
    deadline = to_utc_iso(datetime.now(UTC) + timedelta(hours=1, minutes=40))
    store.update_upload_job(
        upload_job_id,
        publish_at=publish_future,
        platform_publish_at=publish_future,
        upload_at=upload_ready,
        scheduled_at=publish_future,
        upload_deadline=deadline,
    )
    capture = CaptureAdapter()
    monkeypatch.setattr("output_funnel.publisher.load_channel_profiles", lambda: [PROFILE])
    monkeypatch.setattr(
        "output_funnel.publisher.default_adapters",
        lambda: {"youtube_shorts": capture},
    )
    monkeypatch.setattr("output_funnel.publisher.load_settings", lambda: SETTINGS)

    result = publish_due(store=store, limit=5)

    assert result["uploaded"] == 1
    assert len(capture.calls) == 1
    assert store.get_upload_job(upload_job_id)["status"] == "uploaded_scheduled"


def test_backfill_migrates_legacy_scheduled_rows_to_planned(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path))
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    future_publish = to_utc_iso(datetime.now(UTC) + timedelta(hours=4))
    with store.connect() as conn:
        conn.execute(
            "UPDATE upload_jobs SET status = 'scheduled', scheduled_at = ?, "
            "publish_at = NULL, upload_at = NULL, upload_deadline = NULL WHERE id = ?",
            (future_publish, upload_job_id),
        )

    backfill = backfill_legacy_rows(store=store, settings=SETTINGS)

    assert backfill["count"] >= 1
    job = store.get_upload_job(upload_job_id)
    assert job["status"] == "planned"
    assert job["publish_at"] == future_publish
    assert job["upload_at"]
    assert job["upload_deadline"]
    assert job["upload_at"] < job["publish_at"]


def test_backfill_marks_overdue_legacy_rows_missed(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path))
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    past = to_utc_iso(datetime.now(UTC) - timedelta(hours=2))
    with store.connect() as conn:
        conn.execute(
            "UPDATE upload_jobs SET status = 'scheduled', scheduled_at = ?, "
            "publish_at = NULL, upload_at = NULL, upload_deadline = NULL WHERE id = ?",
            (past, upload_job_id),
        )

    backfill = backfill_legacy_rows(store=store, settings=SETTINGS)

    assert backfill["count"] >= 1
    job = store.get_upload_job(upload_job_id)
    assert job["status"] == "missed_upload_window"


def test_retry_from_failed_retryable_goes_to_pending_upload(monkeypatch, tmp_path: Path):
    store, upload_job_id = _store_with_planned_job(monkeypatch, tmp_path)
    store.update_upload_job(
        upload_job_id,
        status=UploadStatus.FAILED_RETRYABLE,
        last_error="temporary network error",
    )

    result = retry_upload_job(upload_job_id, store=store)

    assert result["retry"] is True
    assert store.get_upload_job(upload_job_id)["status"] == "pending_upload"


def test_retry_from_failed_upload_goes_back_to_planned(monkeypatch, tmp_path: Path):
    store, upload_job_id = _store_with_planned_job(monkeypatch, tmp_path)
    store.update_upload_job(
        upload_job_id,
        status=UploadStatus.FAILED_UPLOAD,
        last_error="permanent",
    )

    result = retry_upload_job(upload_job_id, store=store)

    assert result["retry"] is True
    assert store.get_upload_job(upload_job_id)["status"] == "planned"


def test_pending_upload_jobs_are_also_claimed_when_window_open(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MK04_ENV", "prod")
    monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
    store, upload_job_id = _store_with_planned_job(monkeypatch, tmp_path)
    publish_future = to_utc_iso(datetime.now(UTC) + timedelta(hours=2))
    deadline = to_utc_iso(datetime.now(UTC) + timedelta(hours=1, minutes=40))
    upload_ready = to_utc_iso(datetime.now(UTC) - timedelta(minutes=1))
    store.update_upload_job(
        upload_job_id,
        status=UploadStatus.PENDING_UPLOAD,
        publish_at=publish_future,
        platform_publish_at=publish_future,
        upload_at=upload_ready,
        scheduled_at=publish_future,
        upload_deadline=deadline,
    )
    capture = CaptureAdapter()

    result = upload_due_jobs(
        store, profiles=[PROFILE], adapters={"youtube_shorts": capture}
    )

    assert result["uploaded"] == 1
    assert len(capture.calls) == 1
    assert store.get_upload_job(upload_job_id)["status"] == "uploaded_scheduled"
    events = store.publication_status_events(upload_job_id)
    assert any(
        event["from_status"] == "pending_upload" and event["to_status"] == "uploading"
        for event in events
    )


def test_dry_run_upload_gate_skips_adapter_and_records_safe_result(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MK04_ENV", "dev")
    monkeypatch.setenv("MK04_UPLOAD_MODE", "dry_run")
    store, upload_job_id = _store_with_planned_job(monkeypatch, tmp_path)
    publish_future = to_utc_iso(datetime.now(UTC) + timedelta(hours=2))
    deadline = to_utc_iso(datetime.now(UTC) + timedelta(hours=1, minutes=40))
    upload_ready = to_utc_iso(datetime.now(UTC) - timedelta(minutes=1))
    store.update_upload_job(
        upload_job_id,
        publish_at=publish_future,
        platform_publish_at=publish_future,
        upload_deadline=deadline,
        upload_at=upload_ready,
        scheduled_at=publish_future,
    )
    capture = CaptureAdapter()

    result = upload_due_jobs(
        store, profiles=[PROFILE], adapters={"youtube_shorts": capture}
    )

    assert result["uploaded"] == 1
    assert capture.calls == []
    item = result["results"][0]
    assert item["upload_mode"] == "dry_run"
    assert item["environment"] == "dev"
    assert str(item["platform_asset_id"]).startswith("dry_run_youtube_shorts_")
    attempts = store.attempts_for_job(upload_job_id)
    response = attempts[0]["response_json"]
    assert '"dry_run": true' in response


def test_real_upload_mode_refuses_non_prod(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MK04_ENV", "dev")
    monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
    store, upload_job_id = _store_with_planned_job(monkeypatch, tmp_path)
    publish_future = to_utc_iso(datetime.now(UTC) + timedelta(hours=2))
    deadline = to_utc_iso(datetime.now(UTC) + timedelta(hours=1, minutes=40))
    upload_ready = to_utc_iso(datetime.now(UTC) - timedelta(minutes=1))
    store.update_upload_job(
        upload_job_id,
        publish_at=publish_future,
        platform_publish_at=publish_future,
        upload_deadline=deadline,
        upload_at=upload_ready,
        scheduled_at=publish_future,
    )

    try:
        upload_due_jobs(store, profiles=[PROFILE], adapters={"youtube_shorts": CaptureAdapter()})
    except RuntimeError as exc:
        assert "only allowed" in str(exc)
    else:
        raise AssertionError("MK04_UPLOAD_MODE=real should be refused outside prod")


def test_youtube_adapter_validation_rejects_publish_at_too_close_to_now(monkeypatch, tmp_path: Path):
    """Sanity: even outside the orchestrator, the YouTube adapter still
    requires publish_at to be safely in the future (YouTube API enforces
    this; we bake it in). This was working before; this test guards regressions.
    """
    from output_funnel.adapters.youtube import YouTubeAdapter
    from output_funnel.preflight import preferred_media_path  # noqa: F401

    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path))
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    plan_upload_job(upload_job_id, store=store, profiles=[PROFILE], settings=SETTINGS)
    too_soon = to_utc_iso(datetime.now(UTC) + timedelta(minutes=5))
    store.update_upload_job(
        upload_job_id,
        publish_at=too_soon,
        platform_publish_at=too_soon,
        status=UploadStatus.UPLOADING,
    )

    adapter = YouTubeAdapter()
    job = store.get_upload_job(upload_job_id)
    clip = store.get_source_clip(int(job["clip_pk"]))
    out = adapter.publish(job, clip, PROFILE)
    assert out.ok is False
    assert out.error_category == "youtube_validation_error"
    assert "publish_at_not_safely_future" in (out.error_message or "")


def test_youtube_adapter_verifies_scheduled_upload_state(monkeypatch, tmp_path: Path):
    from output_funnel.adapters.youtube import YouTubeAdapter

    class _InsertRequest:
        def execute(self):
            return {
                "id": "yt_verified_123",
                "snippet": {"title": "Title"},
                "status": {
                    "privacyStatus": "private",
                    "publishAt": publish_at,
                    "uploadStatus": "uploaded",
                },
            }

    class _Videos:
        def insert(self, **kwargs):
            self.kwargs = kwargs
            return _InsertRequest()

    class _YouTube:
        def __init__(self):
            self.videos_resource = _Videos()

        def videos(self):
            return self.videos_resource

    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path))
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    publish_at = to_utc_iso(datetime.now(UTC) + timedelta(hours=4))
    store.update_upload_job(
        upload_job_id,
        status=UploadStatus.UPLOADING,
        channel_id="yt_business",
        normalized_title="Title",
        normalized_description="Description",
        publish_at=publish_at,
        platform_publish_at=publish_at,
    )

    fake_youtube = _YouTube()
    job = store.get_upload_job(upload_job_id)
    clip = store.get_source_clip(int(job["clip_pk"]))
    out = YouTubeAdapter(youtube_service=fake_youtube).publish(job, clip, PROFILE)

    assert out.ok is True
    assert out.platform_asset_id == "yt_verified_123"
    body = fake_youtube.videos_resource.kwargs["body"]
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["publishAt"] == publish_at


def test_youtube_adapter_rejects_publish_at_mismatch(monkeypatch, tmp_path: Path):
    from output_funnel.adapters.youtube import YouTubeAdapter

    class _InsertRequest:
        def execute(self):
            return {
                "id": "yt_bad_state_123",
                "status": {
                    "privacyStatus": "private",
                    "publishAt": to_utc_iso(datetime.now(UTC) + timedelta(hours=6)),
                },
            }

    class _YouTube:
        def videos(self):
            class _Videos:
                def insert(self, **_kwargs):
                    return _InsertRequest()

            return _Videos()

    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path))
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    publish_at = to_utc_iso(datetime.now(UTC) + timedelta(hours=4))
    store.update_upload_job(
        upload_job_id,
        status=UploadStatus.UPLOADING,
        channel_id="yt_business",
        normalized_title="Title",
        normalized_description="Description",
        publish_at=publish_at,
        platform_publish_at=publish_at,
    )

    job = store.get_upload_job(upload_job_id)
    clip = store.get_source_clip(int(job["clip_pk"]))
    out = YouTubeAdapter(youtube_service=_YouTube()).publish(job, clip, PROFILE)

    assert out.ok is False
    assert out.error_category == "youtube_state_verification_failed"
    assert "publish_at_mismatch" in (out.error_message or "")
