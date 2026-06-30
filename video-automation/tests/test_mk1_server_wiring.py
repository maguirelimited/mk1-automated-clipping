from __future__ import annotations

import json
import os
import sys
import types

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "server"))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import app as server_app  # noqa: E402


def _make_job(tmp_path):
    job_dir = tmp_path / "job"
    clips_dir = job_dir / "clips"
    clips_dir.mkdir(parents=True)
    return {
        "job_id": "job_test",
        "job_dir": str(job_dir),
        "clips_dir": str(clips_dir),
        "report_path": str(job_dir / "report.json"),
        "review_path": str(job_dir / "review.md"),
        "analytics_path": str(job_dir / "analytics.json"),
        "normalized_transcript_path": str(job_dir / "transcript_payload.json"),
    }


def _patch_pipeline(monkeypatch, *, finished_clip, metadata_path):
    processing_result = types.SimpleNamespace(
        raw_candidate_pool_path="/tmp/pool.json",
        processing_report_path="/tmp/proc_report.json",
        sections_analysed=2,
        usable_sections=2,
        rejected_sections=0,
        failed_sections_count=0,
        final_candidate_count=3,
        duplicates_removed=0,
    )
    monkeypatch.setattr(server_app, "run_processing_pipeline", lambda **kw: processing_result)
    monkeypatch.setattr(
        server_app,
        "run_post_processing_mk1",
        lambda *a, **kw: {
            "status": server_app.STATUS_POST_PROCESSING_COMPLETE,
            "finished_clip_paths": [finished_clip],
            "per_clip_metadata_paths": [metadata_path],
            "post_processing_report_path": "/tmp/pp_report.json",
            "selection_result_path": "/tmp/selection.json",
            "post_processing_report": {
                "candidates_selected": 1,
                "clips_passed": 1,
                "clips_failed": 0,
            },
            "warnings": [],
        },
    )
    # Avoid any network handoff in tests.
    monkeypatch.setattr(
        server_app,
        "_try_output_funnel_handoff",
        lambda report, report_path: {"enabled": False, "ok": False, "reason": "disabled"},
    )
    # Discovery client is constructed but never called (processing is patched).
    monkeypatch.setattr(server_app, "AiServiceSectionDiscoveryClient", lambda **kw: object())


def test_mk1_wiring_maps_finished_clips(monkeypatch, tmp_path):
    src = tmp_path / "finished_clip_001.mp4"
    src.write_bytes(b"\x00" * 1024)
    metadata_path = tmp_path / "clip_001.metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "output_file_path": str(src),
                "input_start_sec": 12.0,
                "input_end_sec": 45.0,
                "input_duration_sec": 33.0,
                "source_candidate_id": "cand_001",
                "validation_result": "PASS",
                "selection_mode": "balanced",
            }
        ),
        encoding="utf-8",
    )
    _patch_pipeline(monkeypatch, finished_clip=str(src), metadata_path=str(metadata_path))
    monkeypatch.setattr(server_app.mk1_settings, "resolve_post_processing_enabled", lambda: True)

    output_root = tmp_path / "output"
    output_root.mkdir()
    job = _make_job(tmp_path)
    report = {"job_id": "job_test", "funnel": {"funnel_id": "business"}, "clips": [], "warnings": []}

    with server_app.app.app_context():
        resp = server_app._run_mk1_pipeline_after_transcript(
            report=report,
            job=job,
            jid="job_test",
            warnings=[],
            stage_ms={},
            total_started=0.0,
            video_path=str(tmp_path / "source.mp4"),
            transcript_path=str(tmp_path / "transcript.json"),
            transcript_payload={"segments": []},
            funnel_id="business",
            output_root=str(output_root),
            filename="source",
            filename_prefix="biz",
            delivery_mode="pull_from_output_endpoint",
            input_id="input_1",
            audit_plain={},
        )

    data = resp.get_json()
    assert data["success"] is True
    assert data["pipeline_mode"] == "mk1"
    assert len(data["clips"]) == 1
    clip = data["clips"][0]
    assert clip["start"] == 12.0 and clip["end"] == 45.0
    assert clip["clip_file"].startswith("biz_clip_01_")
    assert clip["clip_url"].startswith("/output/biz_clip_01_")
    # Clip was copied into the served output folder and the job clips dir.
    served = output_root / clip["clip_file"]
    assert served.is_file()
    assert (tmp_path / "job" / "clips" / clip["clip_file"]).is_file()
    assert report["status"] == "success"
    assert report["post_processing_summary"]["clips_passed"] == 1


def test_mk1_wiring_post_processing_disabled(monkeypatch, tmp_path):
    _patch_pipeline(monkeypatch, finished_clip=str(tmp_path / "x.mp4"), metadata_path=str(tmp_path / "x.json"))
    monkeypatch.setattr(server_app.mk1_settings, "resolve_post_processing_enabled", lambda: False)

    output_root = tmp_path / "output"
    output_root.mkdir()
    job = _make_job(tmp_path)
    report = {"job_id": "job_test", "funnel": {"funnel_id": "business"}, "clips": [], "warnings": []}

    with server_app.app.app_context():
        resp = server_app._run_mk1_pipeline_after_transcript(
            report=report,
            job=job,
            jid="job_test",
            warnings=[],
            stage_ms={},
            total_started=0.0,
            video_path=str(tmp_path / "source.mp4"),
            transcript_path=str(tmp_path / "transcript.json"),
            transcript_payload={"segments": []},
            funnel_id="business",
            output_root=str(output_root),
            filename="source",
            filename_prefix="",
            delivery_mode="pull_from_output_endpoint",
            input_id=None,
            audit_plain={},
        )
    data = resp.get_json()
    assert data["success"] is True
    assert data["post_processing"] == "disabled"
    assert data["clips"] == []
    assert report["current_stage"] == "processing_only"
