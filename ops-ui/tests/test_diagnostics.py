from __future__ import annotations

import json
from pathlib import Path

from ops_ui.diagnostics import (
    artifact_views,
    ffmpeg_output_lines,
    filter_log_text,
    load_input_ledger_record,
    output_funnel_db_check,
    pipeline_stage_rows,
    transcript_view,
)


def test_pipeline_stage_rows_marks_active_stage() -> None:
    rows = pipeline_stage_rows(
        status="running",
        current_stage="selection",
        stage_timings={"transcription_ms": 1200},
    )
    by_key = {row["key"]: row["state"] for row in rows}
    assert by_key["transcription"] == "done"
    assert by_key["selection"] == "active"
    assert by_key["clipping"] == "pending"


def test_filter_log_text_matches_case_insensitive() -> None:
    text, total, matched = filter_log_text("FFmpeg failed\nother line\n", "ffmpeg")
    assert matched == 1
    assert total == 2
    assert "FFmpeg failed" in text


def test_load_input_ledger_record_reads_json(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "input_jobs"
    ledger_dir.mkdir()
    record = {"input_id": "input_test", "state": "downloaded", "funnel_id": "podcast_01"}
    (ledger_dir / "input_test.json").write_text(json.dumps(record), encoding="utf-8")
    loaded = load_input_ledger_record("input_test", ledger_dir=ledger_dir)
    assert loaded is not None
    assert loaded["available"] is True
    assert loaded["funnel_id"] == "podcast_01"


def test_ffmpeg_output_lines_extracts_stderr(tmp_path: Path) -> None:
    errors = [
        {
            "category": "clip_error",
            "message": "ffmpeg failed",
            "stderr": "RUNNING FFMPEG...\nInvalid data",
        }
    ]
    lines = ffmpeg_output_lines(errors, [])
    assert len(lines) == 1
    assert "RUNNING FFMPEG" in lines[0]


def test_transcript_view_reads_segments(tmp_path: Path) -> None:
    path = tmp_path / "transcript_payload.json"
    path.write_text(
        json.dumps(
            {
                "text": "hello world",
                "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}],
            }
        ),
        encoding="utf-8",
    )
    debug = {"transcript_stats": {"available": True, "segment_count": 1}}
    artifacts = {"transcript_payload": {"path": str(path), "exists": True}}
    view = transcript_view(debug, artifacts)
    assert view["segments"][0]["text"] == "hello"
    assert "hello world" in view["preview_text"]


def test_output_funnel_db_check_on_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "queue.sqlite3"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE upload_jobs (id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'planned')"
    )
    conn.execute("INSERT INTO upload_jobs (status) VALUES ('planned')")
    conn.commit()
    conn.close()
    result = output_funnel_db_check(db_path)
    assert result["ok"] is True
    assert result["upload_job_count"] == 1


def test_artifact_views_skips_missing_files(tmp_path: Path) -> None:
    artifacts = {"selection": {"path": str(tmp_path / "missing.json"), "exists": False}}
    views = artifact_views(artifacts)
    assert views == {}
