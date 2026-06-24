"""Tests for ``GET /admin/last-upload``.

The watchdog uses this endpoint to alarm when the queue has pending jobs but
nothing has uploaded recently — the canonical "is the pipeline actually
producing?" sensor.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from output_funnel import app as app_module
from output_funnel.models import UploadStatus
from output_funnel.registry import register_job_payload
from output_funnel.store import OutputStore


def _payload(clip_path: Path) -> dict[str, Any]:
    return {
        "job_id": "job_last_upload_test",
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


def _store_with_one_pending(monkeypatch, tmp_path: Path) -> OutputStore:
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    register_job_payload(store, _payload(clip_path))
    return store


@pytest.fixture
def client(monkeypatch, tmp_path):
    store = _store_with_one_pending(monkeypatch, tmp_path)
    monkeypatch.setattr(app_module, "_store", lambda: store)
    monkeypatch.delenv("OUTPUT_FUNNEL_SECRET", raising=False)
    return app_module.app.test_client(), store


def test_last_upload_reports_no_uploads_with_pending_queue(client):
    test_client, _store = client

    resp = test_client.get("/admin/last-upload")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["success"] is True
    assert body["last_upload_at"] is None
    assert body["pending_count"] >= 1


def test_last_upload_returns_most_recent_published(client):
    test_client, store = client
    with store.connect() as conn:
        conn.execute(
            """
            UPDATE upload_jobs
            SET status = ?, uploaded_at = ?, updated_at = ?
            WHERE id = (SELECT id FROM upload_jobs LIMIT 1)
            """,
            (UploadStatus.PUBLISHED, "2026-05-20T12:00:00Z", "2026-05-20T12:00:00Z"),
        )

    resp = test_client.get("/admin/last-upload")
    body = resp.get_json()

    assert resp.status_code == 200
    assert body["last_upload_at"] == "2026-05-20T12:00:00Z"
    assert body["pending_count"] == 0
