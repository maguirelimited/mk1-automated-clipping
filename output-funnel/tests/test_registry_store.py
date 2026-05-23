from __future__ import annotations

from pathlib import Path

from output_funnel.preflight import preferred_media_path
from output_funnel.registry import register_job_payload
from output_funnel.store import OutputStore


def test_init_db_migrates_old_schema_before_creating_new_indexes(tmp_path: Path):
    db_path = tmp_path / "legacy.sqlite3"
    store = OutputStore(str(db_path))
    with store.connect() as conn:
        conn.executescript(
            """
            CREATE TABLE schema_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE clips (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_job_id TEXT NOT NULL,
              clip_id TEXT NOT NULL,
              clip_index INTEGER,
              start TEXT,
              end TEXT,
              duration_sec REAL,
              clip_file TEXT,
              clip_path TEXT,
              job_clip_path TEXT,
              title TEXT,
              hook TEXT,
              caption TEXT,
              reason TEXT,
              scores_json TEXT NOT NULL DEFAULT '{}',
              composite_score REAL,
              clip_validation_json TEXT NOT NULL DEFAULT '{}',
              source_payload_json TEXT NOT NULL DEFAULT '{}',
              preflight_status TEXT,
              preflight_json TEXT,
              created_at TEXT NOT NULL,
              UNIQUE(source_job_id, clip_id)
            );
            CREATE TABLE upload_jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              clip_pk INTEGER NOT NULL REFERENCES clips(id),
              platform TEXT NOT NULL,
              channel_id TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL,
              normalized_title TEXT,
              normalized_description TEXT,
              normalized_hashtags_json TEXT NOT NULL DEFAULT '[]',
              metadata_json TEXT NOT NULL DEFAULT '{}',
              scheduled_at TEXT,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              platform_asset_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(clip_pk, platform, channel_id)
            );
            CREATE TABLE publish_attempts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              upload_job_id INTEGER NOT NULL REFERENCES upload_jobs(id),
              attempted_at TEXT NOT NULL,
              status TEXT NOT NULL,
              request_json TEXT NOT NULL DEFAULT '{}',
              response_json TEXT NOT NULL DEFAULT '{}',
              error_category TEXT,
              error_message TEXT,
              retryable INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE analytics_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_type TEXT NOT NULL,
              occurred_at TEXT NOT NULL,
              clip_pk INTEGER REFERENCES clips(id),
              upload_job_id INTEGER REFERENCES upload_jobs(id),
              payload_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL
            );
            CREATE TABLE publication_metrics (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              upload_job_id INTEGER NOT NULL REFERENCES upload_jobs(id),
              metric_name TEXT NOT NULL,
              metric_value REAL,
              metric_json TEXT NOT NULL DEFAULT '{}',
              source TEXT,
              observed_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(upload_job_id, metric_name, source, observed_at)
            );
            """
        )

    store.init_db()

    with store.connect() as conn:
        clip_cols = store._existing_columns(conn, "clips")
        upload_cols = store._existing_columns(conn, "upload_jobs")
        assert "source_job_pk" in clip_cols
        assert "target_pk" in upload_cols
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_clips_source_job_pk'"
        ).fetchone()
    assert row is not None


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
    assert jobs[0]["durable_id"].startswith("clip_")
    assert jobs[0]["publication_id"].startswith("pub_")
    assert jobs[0]["variant_pk"] == first["registered"][0]["variant_pk"]
    assert jobs[0]["variant_type"] == "default"
    assert first["registered"][0]["variant_created"] is True
    assert second["registered"][0]["variant_created"] is False


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


def test_registration_creates_source_job_and_links_clip(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    payload = _payload(clip_path)
    payload["transcript_path"] = str(tmp_path / "transcript.json")

    result = register_job_payload(store, payload)

    assert result["source_job_pk"]
    source = store.get_source_clip(int(result["registered"][0]["clip_pk"]))
    assert source is not None
    assert source["source_job_pk"] == result["source_job_pk"]
    assert source["durable_id"].startswith("clip_")
    assert source["import_key"] == f"{source['source_job_id']}:{source['clip_id']}"


def test_transcript_rows_are_backfilled_from_source_and_clip_paths(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    payload = _payload(clip_path)
    payload["transcript_path"] = str(tmp_path / "source_transcript.json")
    payload["clips"][0]["clip_transcript_path"] = str(tmp_path / "clip_transcript.json")

    result = register_job_payload(store, payload)

    with store.connect() as conn:
        rows = conn.execute(
            "SELECT transcript_type, path FROM transcripts ORDER BY transcript_type, path"
        ).fetchall()
    assert {row["transcript_type"] for row in rows} == {"clip", "source"}
    assert str(tmp_path / "clip_transcript.json") in {row["path"] for row in rows}
    assert result["source_job_pk"]


def test_publications_view_and_status_history_track_publication_lifecycle(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path))
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    publication_id = store.get_upload_job(upload_job_id)["publication_id"]

    store.update_upload_job(upload_job_id, status="routed")
    events = store.publication_status_events(upload_job_id)

    assert [event["to_status"] for event in events] == ["registered", "routed"]
    assert {event["publication_id"] for event in events} == {publication_id}
    with store.connect() as conn:
        row = conn.execute(
            "SELECT publication_id, variant_pk FROM publications WHERE id = ?",
            (upload_job_id,),
        ).fetchone()
    assert row["publication_id"] == publication_id
    assert row["variant_pk"] == store.get_upload_job(upload_job_id)["variant_pk"]


def test_publication_attempts_view_exposes_attempt_history(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path))
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    publication_id = store.get_upload_job(upload_job_id)["publication_id"]

    store.record_attempt(
        upload_job_id,
        status="failed_retryable",
        error_category="transient",
        error_message="temporary",
        retryable=True,
    )

    with store.connect() as conn:
        row = conn.execute(
            "SELECT publication_id, status, retryable FROM publication_attempts WHERE upload_job_id = ?",
            (upload_job_id,),
        ).fetchone()
    assert row["publication_id"] == publication_id
    assert row["status"] == "failed_retryable"
    assert row["retryable"] == 1


def test_repost_publications_can_share_variant_platform_and_channel(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path))
    clip_pk = int(result["registered"][0]["clip_pk"])
    variant_pk = int(result["registered"][0]["variant_pk"])
    first_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])

    repost_job_id, created = store.create_upload_job(
        clip_pk=clip_pk,
        variant_pk=variant_pk,
        platform="youtube_shorts",
        channel_id="",
        idempotency_key="repost:test:001",
    )

    assert created is True
    jobs = store.list_upload_jobs()
    assert len(jobs) == 2
    assert repost_job_id != first_job_id
    assert len({job["publication_id"] for job in jobs}) == 2


def test_custom_variant_can_be_published_and_prefers_variant_asset(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    render_path = tmp_path / "clip_variant.mp4"
    clip_path.write_bytes(b"fake-video")
    render_path.write_bytes(b"variant-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path))
    clip_pk = int(result["registered"][0]["clip_pk"])
    variant_pk, created = store.create_clip_variant(
        clip_pk=clip_pk,
        variant_type="hook_test",
        rendered_asset_path=str(render_path),
        render_fingerprint="hook-a",
        editorial={"hook": "alternate"},
    )

    upload_job_id, job_created = store.create_upload_job(
        clip_pk=clip_pk,
        variant_pk=variant_pk,
        platform="youtube_shorts",
        idempotency_key="experiment:hook-a",
    )
    job = store.get_upload_job(upload_job_id)

    assert created is True
    assert job_created is True
    assert job["variant_pk"] == variant_pk
    assert job["variant_type"] == "hook_test"
    assert preferred_media_path(job) == str(render_path)
    with store.connect() as conn:
        asset = conn.execute(
            "SELECT asset_type, path FROM assets WHERE path = ?",
            (str(render_path),),
        ).fetchone()
    assert asset["asset_type"] == "rendered_video"


def test_publication_targets_and_variant_status_history_are_durable(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path))
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    variant_pk = int(result["registered"][0]["variant_pk"])

    store.update_clip_variant(variant_pk, status="retired")
    job = store.get_upload_job(upload_job_id)
    events = store.variant_status_events(variant_pk)

    assert job["target_pk"]
    assert [event["to_status"] for event in events] == ["ready", "retired"]
    with store.connect() as conn:
        target = conn.execute(
            "SELECT platform, channel_id FROM publication_targets WHERE id = ?",
            (job["target_pk"],),
        ).fetchone()
    assert target["platform"] == "youtube_shorts"


def test_metrics_and_analytics_records_are_durable(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"fake-video")
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: 30.0)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()
    result = register_job_payload(store, _payload(clip_path))
    clip_pk = int(result["registered"][0]["clip_pk"])
    upload_job_id = int(result["registered"][0]["upload_jobs"][0]["upload_job_id"])
    publication_id = store.get_upload_job(upload_job_id)["publication_id"]

    assert store.record_clip_metric(clip_pk, metric_name="quality_score", metric_value=8.0)
    assert store.record_publication_metric(
        upload_job_id,
        metric_name="views",
        metric_value=123.0,
        source="youtube",
        metric_unit="count",
        window_start="2026-05-22T00:00:00Z",
        window_end="2026-05-23T00:00:00Z",
        dimensions={"surface": "shorts"},
    )
    assert store.record_analytics_event(
        "publication_metric_imported",
        clip_pk=clip_pk,
        upload_job_id=upload_job_id,
        payload={"metric": "views"},
    )
    with store.connect() as conn:
        metric = conn.execute(
            "SELECT publication_id, metric_unit, dimensions_json FROM publication_metrics WHERE upload_job_id = ?",
            (upload_job_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT publication_id FROM analytics_events WHERE upload_job_id = ?",
            (upload_job_id,),
        ).fetchone()
    assert metric["publication_id"] == publication_id
    assert metric["metric_unit"] == "count"
    assert "shorts" in metric["dimensions_json"]
    assert event["publication_id"] == publication_id


def test_preflight_rejects_bad_clip_without_upload_job(monkeypatch, tmp_path: Path):
    clip_path = tmp_path / "missing.mp4"
    monkeypatch.setattr("output_funnel.preflight.ffprobe_duration_sec", lambda _path: None)
    store = OutputStore(str(tmp_path / "queue.sqlite3"))
    store.init_db()

    result = register_job_payload(store, _payload(clip_path))

    assert result["registered"][0]["preflight_ok"] is False
    assert "media_file_missing" in result["registered"][0]["preflight_issues"]
    assert store.list_upload_jobs() == []
