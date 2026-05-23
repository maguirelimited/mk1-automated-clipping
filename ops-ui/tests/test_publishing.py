from __future__ import annotations

from ops_ui.publishing import (
    filter_upload_jobs,
    publish_confirmation,
    queue_stats,
    upload_latency,
)


def test_filter_upload_jobs_by_status_and_query() -> None:
    jobs = [
        {"id": 1, "status": "planned", "platform": "youtube_shorts", "channel_id": "yt_a", "source_title": "Alpha"},
        {"id": 2, "status": "failed_upload", "platform": "youtube_shorts", "channel_id": "yt_b", "last_error": "quota"},
    ]
    filtered = filter_upload_jobs(jobs, status="planned", q="alpha")
    assert len(filtered) == 1
    assert filtered[0]["id"] == 1


def test_queue_stats_counts_backlog_and_failures() -> None:
    jobs = [
        {"status": "planned"},
        {"status": "uploading"},
        {"status": "failed_retryable"},
        {"status": "uploaded_scheduled", "uploaded_at": "2026-05-23T12:00:00Z"},
    ]
    stats = queue_stats(jobs)
    assert stats["backlog"] == 2
    assert stats["failed_uploads"] == 1
    assert stats["in_flight"] == 1


def test_upload_latency_duration() -> None:
    job = {
        "upload_started_at": "2026-05-23T10:00:00Z",
        "uploaded_at": "2026-05-23T10:00:45Z",
        "upload_at": "2026-05-23T09:30:00Z",
    }
    latency = upload_latency(job)
    assert latency["upload_duration_sec"] == 45.0
    assert latency["queue_wait_sec"] == 30 * 60


def test_publish_confirmation_for_uploaded_scheduled() -> None:
    job = {
        "status": "uploaded_scheduled",
        "platform_video_id": "yt_abc",
        "platform_state": "private_scheduled",
    }
    assert "scheduled on platform" in publish_confirmation(job)
