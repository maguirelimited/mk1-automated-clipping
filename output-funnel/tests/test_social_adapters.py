from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from output_funnel.adapters.common import HttpResult
from output_funnel.adapters.facebook_reels import FacebookReelsAdapter
from output_funnel.adapters.instagram_reels import InstagramReelsAdapter
from output_funnel.adapters.x import XAdapter
from output_funnel.adapters.base import PlatformAdapter
from output_funnel.metadata import normalize_metadata
from output_funnel.models import FailureClass, PublishResult, PublishState, UploadStatus
from output_funnel.publisher import upload_due_jobs
from output_funnel.registry import register_job_payload
from output_funnel.service import compute_upload_window
from output_funnel.store import OutputStore
from output_funnel.time_utils import to_utc_iso


class FakeHttp:
    def __init__(self, responses: list[HttpResult]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> HttpResult:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self.responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> HttpResult:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self.responses.pop(0)


class SuccessAdapter(PlatformAdapter):
    platform = "x"
    adapter_version = "test-adapter"
    api_version = "test-api"

    def publish(self, upload_job, source_clip, profile):
        return PublishResult(
            ok=True,
            status=UploadStatus.UPLOADED_SCHEDULED,
            platform_asset_id="remote_123",
            response={"id": "remote_123"},
            raw_response={"raw": {"id": "remote_123"}},
            remote_ids={"remote_id": "remote_123"},
            publish_state=PublishState.PUBLISHED,
            platform_state="published",
            adapter_version=self.adapter_version,
            api_version=self.api_version,
        )


class ShouldNotRunAdapter(PlatformAdapter):
    platform = "x"

    def publish(self, upload_job, source_clip, profile):
        raise AssertionError("adapter should not run while account is blocked")


X_PROFILE = {
    "channel_id": "x_account",
    "brand_name": "X Account",
    "platform": "x",
    "enabled": True,
    "routing": {"accepted_funnel_ids": ["business_clips_test"], "required_platform": "x"},
    "metadata_style": {"x_text_template": "{caption} {hashtags}", "default_hashtags": ["#AI"], "max_hashtags": 1},
}


def _payload(clip_path: Path, *, platform: str = "x") -> dict[str, Any]:
    return {
        "job_id": "job_social_001",
        "status": "success",
        "clips": [
            {
                "clip_id": "clip_1",
                "start": "00:00:01.000",
                "end": "00:00:31.000",
                "duration_sec": 30.0,
                "job_clip_path": str(clip_path),
                "title": "A social clip",
                "hook": "Hook",
                "caption": "Caption",
                "clip_validation": {"ok": True},
                "funnel_id": "business_clips_test",
            }
        ],
        "enabled_platforms": [platform],
    }


def _store_with_planned_x_job(monkeypatch, tmp_path: Path) -> tuple[OutputStore, int]:
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path, platform="x"))
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    store.update_upload_job(
        upload_job_id,
        status=UploadStatus.PLANNED,
        channel_id="x_account",
        normalized_title="A social clip",
        normalized_description="Caption #AI",
        publish_at=to_utc_iso(datetime.now(UTC) - timedelta(seconds=1)),
        platform_publish_at=to_utc_iso(datetime.now(UTC) - timedelta(seconds=1)),
        upload_at=to_utc_iso(datetime.now(UTC) - timedelta(seconds=1)),
        upload_deadline=to_utc_iso(datetime.now(UTC) + timedelta(minutes=15)),
    )
    return store, upload_job_id


def test_immediate_publish_platforms_upload_at_publish_time():
    publish_at = "2026-05-22T18:00:00Z"

    x_upload_at, x_deadline = compute_upload_window(publish_at=publish_at, platform="x", settings={"x": {}})
    ig_upload_at, ig_deadline = compute_upload_window(
        publish_at=publish_at, platform="instagram_reels", settings={"instagram": {}}
    )

    assert x_upload_at == publish_at
    assert ig_upload_at == publish_at
    assert x_deadline > publish_at
    assert ig_deadline > publish_at


def test_facebook_reels_can_upload_immediately_for_native_scheduling():
    publish_at = to_utc_iso(datetime.now(UTC) + timedelta(hours=4))

    upload_at, deadline = compute_upload_window(
        publish_at=publish_at,
        platform="facebook_reels",
        settings={"facebook": {"scheduled_publish_mode": "platform_native", "upload_timing": "immediate_after_validation"}},
    )

    assert upload_at < deadline < publish_at
    assert datetime.fromisoformat(upload_at.replace("Z", "+00:00")) <= datetime.now(UTC) + timedelta(seconds=2)


def test_metadata_uses_x_limits_and_template():
    result = normalize_metadata(
        {"title": "T" * 500, "caption": "C" * 500},
        {
            "platform": "x",
            "metadata_style": {"x_text_template": "{caption} {hashtags}", "default_hashtags": ["#AI"], "max_hashtags": 1},
        },
    )

    assert len(result.title) <= 280
    assert len(result.description) <= 280
    assert result.hashtags == ["#AI"]


def test_upload_records_lease_audit_metrics_and_versions(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MK04_ENV", "prod")
    monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
    store, upload_job_id = _store_with_planned_x_job(monkeypatch, tmp_path)

    result = upload_due_jobs(store, profiles=[X_PROFILE], adapters={"x": SuccessAdapter()}, limit=1)

    assert result["uploaded"] == 1
    job = store.get_upload_job(upload_job_id)
    assert job["lease_token"] is None
    assert job["platform_asset_id"] == "remote_123"
    assert job["publish_state"] == "published"
    assert job["adapter_version"] == "test-adapter"
    assert job["remote_ids"] == {"remote_id": "remote_123"}
    attempts = store.attempts_for_job(upload_job_id)
    assert attempts[0]["raw_response_json"]
    assert attempts[0]["adapter_version"] == "test-adapter"
    assert store.upload_audit_events(upload_job_id)


def test_account_block_defers_one_profile_without_running_adapter(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MK04_ENV", "prod")
    monkeypatch.setenv("MK04_UPLOAD_MODE", "real")
    store, upload_job_id = _store_with_planned_x_job(monkeypatch, tmp_path)
    store.record_account_failure(
        platform="x",
        channel_id="x_account",
        failure_class=FailureClass.RATE_LIMITED,
        retry_after_seconds=300,
    )

    result = upload_due_jobs(store, profiles=[X_PROFILE], adapters={"x": ShouldNotRunAdapter()}, limit=1)

    assert result["uploaded"] == 0
    assert result["results"][0]["reason"] == "account_rate_limited"
    assert store.get_upload_job(upload_job_id)["status"] == UploadStatus.PENDING_UPLOAD


def test_x_adapter_uploads_media_and_posts(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("X_TOKEN", "token")
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    http = FakeHttp(
        [
            HttpResult(200, {"media_id_string": "media_1"}),
            HttpResult(200, {}),
            HttpResult(200, {}),
            HttpResult(201, {"data": {"id": "tweet_1"}}),
        ]
    )

    result = XAdapter(http_client=http).publish(
        {
            "id": 1,
            "platform": "x",
            "channel_id": "x_account",
            "normalized_description": "Caption",
            "job_clip_path": str(media),
        },
        {"job_clip_path": str(media)},
        {"credentials": {"access_token_env": "X_TOKEN"}},
    )

    assert result.ok is True
    assert result.platform_asset_id == "tweet_1"
    assert result.remote_ids["x_media_id"] == "media_1"
    assert http.calls[-1]["url"].endswith("/2/tweets")


def test_facebook_reels_adapter_schedules_finish(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("FB_TOKEN", "token")
    monkeypatch.setenv("FB_PAGE_ID", "page_1")
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    publish_at = to_utc_iso(datetime.now(UTC) + timedelta(hours=3))
    http = FakeHttp(
        [
            HttpResult(200, {"video_id": "video_1", "upload_url": "https://upload.facebook.test/video_1"}),
            HttpResult(200, {"success": True}),
            HttpResult(200, {"post_id": "post_1"}),
        ]
    )

    result = FacebookReelsAdapter(http_client=http).publish(
        {
            "id": 1,
            "platform": "facebook_reels",
            "normalized_title": "Title",
            "normalized_description": "Caption",
            "platform_publish_at": publish_at,
            "job_clip_path": str(media),
        },
        {"job_clip_path": str(media)},
        {"credentials": {"access_token_env": "FB_TOKEN", "page_id_env": "FB_PAGE_ID"}},
    )

    assert result.ok is True
    assert result.publish_state == PublishState.SCHEDULED
    assert result.remote_ids["facebook_video_id"] == "video_1"
    assert http.calls[-1]["params"]["video_state"] == "SCHEDULED"


def test_instagram_reels_adapter_resumable_upload_and_publish(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("IG_TOKEN", "token")
    monkeypatch.setenv("IG_USER_ID", "ig_1")
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"video")
    http = FakeHttp(
        [
            HttpResult(200, {"id": "container_1"}),
            HttpResult(200, {"success": True}),
            HttpResult(200, {"status_code": "FINISHED"}),
            HttpResult(200, {"id": "media_1"}),
        ]
    )

    result = InstagramReelsAdapter(http_client=http).publish(
        {
            "id": 1,
            "platform": "instagram_reels",
            "normalized_description": "Caption",
            "job_clip_path": str(media),
        },
        {"job_clip_path": str(media)},
        {"credentials": {"access_token_env": "IG_TOKEN", "ig_user_id_env": "IG_USER_ID"}},
    )

    assert result.ok is True
    assert result.publish_state == PublishState.PUBLISHED
    assert result.remote_ids["instagram_container_id"] == "container_1"
    assert http.calls[-1]["url"].endswith("/ig_1/media_publish")
