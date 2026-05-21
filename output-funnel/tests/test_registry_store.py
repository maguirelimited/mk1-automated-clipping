from __future__ import annotations

from pathlib import Path

from output_funnel.registry import register_job_payload
from output_funnel.store import OutputStore


def _payload(clip_path: Path) -> dict:
    return {
        "job_id": "job_20260521T120000Z_deadbeef",
        "status": "success",
        "clips": [
            {
                "clip_id": "clip_1",
                "clip_index": 1,
                "start": "00:00:01.000",
                "end": "00:00:31.000",
                "duration_sec": 30.0,
                "job_clip_path": str(clip_path),
                "title": "A strong business lesson",
                "hook": "This changed the company",
                "caption": "A short caption",
                "scores": {"hook_strength": 8},
                "composite_score": 8.0,
                "clip_validation": {"ok": True},
                "funnel_id": "business_clips_test",
            }
        ],
        "enabled_platforms": ["youtube_shorts"],
    }


def test_register_payload_dedupes_source_clip_and_upload_job(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()

    first = register_job_payload(store, _payload(clip_path))
    second = register_job_payload(store, _payload(clip_path))

    assert first["registered"][0]["clip_created"] is True
    assert second["registered"][0]["clip_created"] is False
    jobs = store.list_upload_jobs()
    assert len(jobs) == 1
    assert jobs[0]["platform"] == "youtube_shorts"


def test_source_clip_is_immutable_after_duplicate_registration(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()

    register_job_payload(store, _payload(clip_path))
    changed = _payload(clip_path)
    changed["clips"][0]["title"] = "Changed title"
    register_job_payload(store, changed)

    job = store.list_upload_jobs()[0]
    source = store.get_source_clip(int(job["clip_pk"]))
    assert source is not None
    assert source["title"] == "A strong business lesson"


def test_registration_dedupes_upload_job_after_routing(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()

    result = register_job_payload(store, _payload(clip_path))
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    store.update_upload_job(upload_job_id, channel_id="yt_business", status="routed")
    register_job_payload(store, _payload(clip_path))

    jobs = store.list_upload_jobs()
    assert len(jobs) == 1
    assert jobs[0]["channel_id"] == "yt_business"


def test_report_level_funnel_id_is_copied_to_source_clip(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    payload = _payload(clip_path)
    payload["funnel"] = {"funnel_id": "mfm_business_ai_001"}
    del payload["clips"][0]["funnel_id"]

    result = register_job_payload(store, payload)

    source = store.get_source_clip(int(result["registered"][0]["clip_pk"]))
    assert source["source_payload"]["funnel_id"] == "mfm_business_ai_001"


def test_preflight_rejects_bad_clip_without_upload_job(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "missing.mp4"
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: None)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()

    result = register_job_payload(store, _payload(clip_path))

    assert result["registered"][0]["preflight_ok"] is False
    assert "media_file_missing" in result["registered"][0]["preflight_issues"]
    assert store.list_upload_jobs() == []
