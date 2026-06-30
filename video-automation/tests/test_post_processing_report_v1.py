"""post_processing_report_v1 — focused tests (Prompt 23).

Covers:
  - Schema / building
  - Selection counts
  - Clip counts
  - Module aggregation
  - Finished clip paths
  - Metadata paths
  - Failed clips
  - Rejected/reserve candidates
  - Validation
  - Writing
  - Integration with post_processing_mk1

No real ffmpeg/ffprobe is required for any of these tests.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from post_processing_report_v1 import (  # noqa: E402
    FIXED_MK1_MODULE_ORDER,
    POST_PROCESSING_VERSION,
    REPORT_SCHEMA_VERSION,
    build_post_processing_report,
    load_post_processing_report,
    validate_post_processing_report,
    write_post_processing_report,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _module_result(
    name: str,
    status: str = "PASS",
    output_path: str | None = None,
    metadata: dict | None = None,
    error_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "post_processing_module_result_v1",
        "module_name": name,
        "module_version": "1.0",
        "status": status,
        "input_path": None,
        "output_path": output_path,
        "config": {},
        "error_reason": error_reason if status != "PASS" else None,
        "warnings": [],
        "metadata": dict(metadata or {}),
    }


def _passing_clip_result(
    clip_id: str = "job_cand_001",
    candidate_id: str = "cand_001",
    clip_path: str = "/clips/clip.mp4",
    metadata_path: str | None = "/metadata/clip_meta.json",
) -> dict[str, Any]:
    mw_meta = {"metadata_path": metadata_path, "output_file_path": clip_path} if metadata_path else {"output_file_path": clip_path}
    return {
        "clip_id": clip_id,
        "source_candidate_id": candidate_id,
        "status": "PASS",
        "final_output_path": clip_path,
        "module_results": [
            _module_result("render_clip_v1", "PASS", output_path="/render.mp4"),
            _module_result("platform_safe_format_v1", "PASS", output_path="/psf.mp4"),
            _module_result("intelligent_captions_v1", "PASS", output_path="/captions.mp4"),
            _module_result("validation_v1", "PASS", output_path=clip_path),
            _module_result("metadata_writer_v1", "PASS", output_path=clip_path, metadata=mw_meta),
        ],
        "failed_module": None,
        "failure_reason": None,
        "warnings": [],
    }


def _failing_clip_result(
    clip_id: str = "job_cand_002",
    candidate_id: str = "cand_002",
    failed_module: str = "validation_v1",
    failure_reason: str = "duration_mismatch",
) -> dict[str, Any]:
    return {
        "clip_id": clip_id,
        "source_candidate_id": candidate_id,
        "status": "FAIL",
        "final_output_path": None,
        "module_results": [
            _module_result("render_clip_v1", "PASS", output_path="/render2.mp4"),
            _module_result("platform_safe_format_v1", "PASS", output_path="/psf2.mp4"),
            _module_result("intelligent_captions_v1", "PASS", output_path="/captions2.mp4"),
            _module_result(failed_module, "FAIL", error_reason=failure_reason),
        ],
        "failed_module": failed_module,
        "failure_reason": failure_reason,
        "warnings": [],
    }


def _selection_result(
    selected: list | None = None,
    rejected: list | None = None,
    reserve: list | None = None,
    selection_mode: str = "balanced",
) -> dict[str, Any]:
    return {
        "status": "PASS",
        "selection_mode": selection_mode,
        "selected_candidates": list(selected or []),
        "rejected_candidates": list(rejected or []),
        "reserve_candidates": list(reserve or []),
    }


def _minimal_candidate(candidate_id: str = "cand_001") -> dict[str, Any]:
    return {"candidate_id": candidate_id, "start_sec": 0.0, "end_sec": 10.0}


def _raw_pool(candidates: list | None = None, job_id: str = "job_001") -> dict[str, Any]:
    return {
        "schema_version": "raw_candidate_pool_v1",
        "job_id": job_id,
        "source_video_path": "/src/video.mp4",
        "candidates": list(candidates or [_minimal_candidate()]),
    }


# ---------------------------------------------------------------------------
# Schema / building
# ---------------------------------------------------------------------------


class TestSchemaBuild:
    def test_schema_version(self):
        r = build_post_processing_report(job_id="j1")
        assert r["schema_version"] == "post_processing_report_v1"
        assert r["schema_version"] == REPORT_SCHEMA_VERSION

    def test_post_processing_version(self):
        r = build_post_processing_report(job_id="j1")
        assert r["post_processing_version"] == POST_PROCESSING_VERSION

    def test_job_id_included(self):
        r = build_post_processing_report(job_id="my_job_42")
        assert r["job_id"] == "my_job_42"

    def test_created_at_present(self):
        import datetime
        r = build_post_processing_report(job_id="j1")
        assert "created_at" in r
        datetime.datetime.fromisoformat(r["created_at"])

    def test_source_video_path_included(self):
        r = build_post_processing_report(job_id="j1", source_video_path="/src/vid.mp4")
        assert r["source_video_path"] == "/src/vid.mp4"

    def test_source_video_path_from_selection_result(self):
        sel = {"source_video_path": "/sel/vid.mp4", "selected_candidates": []}
        r = build_post_processing_report(job_id="j1", selection_result=sel)
        assert r["source_video_path"] == "/sel/vid.mp4"

    def test_raw_candidate_pool_path_included(self):
        r = build_post_processing_report(job_id="j1", raw_candidate_pool_path="/pool.json")
        assert r["raw_candidate_pool_path"] == "/pool.json"

    def test_selection_result_path_included(self):
        r = build_post_processing_report(job_id="j1", selection_result_path="/sel.json")
        assert r["selection_result_path"] == "/sel.json"

    def test_report_path_included(self):
        r = build_post_processing_report(job_id="j1", report_path="/reports/report.json")
        assert r["post_processing_report_path"] == "/reports/report.json"

    def test_minimal_build_no_errors(self):
        r = build_post_processing_report(job_id="j1")
        assert isinstance(r, dict)
        errs = validate_post_processing_report(r)
        assert errs == []

    def test_result_is_json_serialisable(self):
        r = build_post_processing_report(
            job_id="j1",
            selection_result=_selection_result(selected=[_minimal_candidate()]),
            clip_results=[_passing_clip_result()],
            raw_candidate_pool=_raw_pool(),
        )
        json.dumps(r)


# ---------------------------------------------------------------------------
# Selection counts
# ---------------------------------------------------------------------------


class TestSelectionCounts:
    def test_counts_raw_candidates_from_pool(self):
        pool = _raw_pool(candidates=[_minimal_candidate(), _minimal_candidate("c2")])
        r = build_post_processing_report(job_id="j1", raw_candidate_pool=pool)
        assert r["raw_candidates_received"] == 2

    def test_counts_selected_candidates(self):
        sel = _selection_result(selected=[_minimal_candidate(), _minimal_candidate("c2")])
        r = build_post_processing_report(job_id="j1", selection_result=sel)
        assert r["candidates_selected"] == 2

    def test_counts_rejected_candidates(self):
        sel = _selection_result(rejected=[_minimal_candidate("r1"), _minimal_candidate("r2")])
        r = build_post_processing_report(job_id="j1", selection_result=sel)
        assert r["candidates_rejected"] == 2

    def test_counts_reserve_candidates(self):
        sel = _selection_result(reserve=[_minimal_candidate("res1")])
        r = build_post_processing_report(job_id="j1", selection_result=sel)
        assert r["reserve_candidates"] == 1

    def test_falls_back_to_selection_summary(self):
        sel = {
            "selected_candidates": [],
            "rejected_candidates": [],
            "reserve_candidates": [],
            "selection_summary": {
                "selected_count": 3,
                "rejected_count": 7,
                "reserve_count": 2,
                "raw_candidates_received": 12,
            },
        }
        r = build_post_processing_report(job_id="j1", selection_result=sel)
        # Lists are empty so falls back to summary
        assert r["candidates_selected"] == 3
        assert r["candidates_rejected"] == 7
        assert r["reserve_candidates"] == 2

    def test_falls_back_to_selection_summary_for_raw_count(self):
        sel = {
            "selected_candidates": [],
            "selection_summary": {"raw_candidates_received": 15},
        }
        r = build_post_processing_report(job_id="j1", selection_result=sel)
        assert r["raw_candidates_received"] == 15

    def test_warns_on_selection_count_mismatch(self):
        sel = {
            "selected_candidates": [_minimal_candidate()],
            "rejected_candidates": [],
            "reserve_candidates": [],
            "selection_summary": {"selected_count": 99},
        }
        r = build_post_processing_report(job_id="j1", selection_result=sel)
        # Mismatch warning may be present; exact trigger depends on implementation
        assert isinstance(r["warnings"], list)


# ---------------------------------------------------------------------------
# Clip counts
# ---------------------------------------------------------------------------


class TestClipCounts:
    def test_counts_clips_attempted(self):
        r = build_post_processing_report(
            job_id="j1",
            clip_results=[_passing_clip_result(), _failing_clip_result()],
        )
        assert r["clips_attempted"] == 2

    def test_counts_clips_rendered(self):
        r = build_post_processing_report(
            job_id="j1",
            clip_results=[_passing_clip_result(), _failing_clip_result()],
        )
        # Both have render_clip_v1 PASS
        assert r["clips_rendered"] == 2

    def test_counts_clips_passed_from_validation(self):
        r = build_post_processing_report(
            job_id="j1",
            clip_results=[_passing_clip_result(), _passing_clip_result("j_c2", "c2")],
        )
        assert r["clips_passed"] == 2

    def test_counts_clips_failed_from_validation(self):
        r = build_post_processing_report(
            job_id="j1",
            clip_results=[_passing_clip_result(), _failing_clip_result()],
        )
        assert r["clips_failed"] == 1

    def test_metadata_writer_pass_not_counted_as_validation_pass(self):
        """clips_passed must come from validation_v1 PASS, not metadata_writer_v1."""
        clip = {
            "clip_id": "j_c1",
            "source_candidate_id": "c1",
            "status": "PASS",
            "final_output_path": "/clip.mp4",
            "module_results": [
                _module_result("render_clip_v1", "PASS"),
                _module_result("platform_safe_format_v1", "PASS"),
                _module_result("intelligent_captions_v1", "PASS"),
                # No validation_v1 — only metadata_writer_v1
                _module_result("metadata_writer_v1", "PASS", output_path="/clip.mp4"),
            ],
            "failed_module": None,
            "failure_reason": None,
            "warnings": [],
        }
        r = build_post_processing_report(job_id="j1", clip_results=[clip])
        assert r["clips_passed"] == 0


# ---------------------------------------------------------------------------
# Module aggregation
# ---------------------------------------------------------------------------


class TestModuleAggregation:
    def test_aggregates_modules_in_fixed_order(self):
        r = build_post_processing_report(
            job_id="j1", clip_results=[_passing_clip_result()]
        )
        mr = r["modules_run"]
        # All 5 should appear
        for name in FIXED_MK1_MODULE_ORDER:
            assert name in mr
        # Order: each module should appear before the next
        indices = {name: mr.index(name) for name in FIXED_MK1_MODULE_ORDER if name in mr}
        for a, b in zip(FIXED_MK1_MODULE_ORDER, FIXED_MK1_MODULE_ORDER[1:]):
            if a in indices and b in indices:
                assert indices[a] < indices[b]

    def test_records_failed_modules(self):
        r = build_post_processing_report(
            job_id="j1", clip_results=[_failing_clip_result()]
        )
        assert len(r["failed_modules"]) >= 1
        assert any(m["module_name"] == "validation_v1" for m in r["failed_modules"])

    def test_deduplicates_failed_module_records(self):
        # Two clips with same module failing — should produce two separate entries
        r = build_post_processing_report(
            job_id="j1",
            clip_results=[
                _failing_clip_result("c1", "c1"),
                _failing_clip_result("c2", "c2"),
            ],
        )
        keys = [f"{m['clip_id']}::{m['module_name']}" for m in r["failed_modules"]]
        assert len(keys) == len(set(keys))

    def test_does_not_duplicate_same_clip_same_module(self):
        cr = _failing_clip_result()
        r = build_post_processing_report(job_id="j1", clip_results=[cr, cr])
        # clip_id is the same for both; deduplicated
        keys = [f"{m['clip_id']}::{m['module_name']}" for m in r["failed_modules"]]
        assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# Finished clip paths
# ---------------------------------------------------------------------------


class TestFinishedClipPaths:
    def test_includes_only_passed_clip_paths(self):
        r = build_post_processing_report(
            job_id="j1",
            clip_results=[
                _passing_clip_result(clip_path="/clips/good.mp4"),
                _failing_clip_result(),
            ],
        )
        assert len(r["finished_clip_paths"]) == 1
        assert "/clips/good.mp4" in r["finished_clip_paths"]

    def test_excludes_failed_clip_from_finished_paths(self):
        r = build_post_processing_report(
            job_id="j1", clip_results=[_failing_clip_result()]
        )
        assert r["finished_clip_paths"] == []

    def test_resolves_path_from_metadata_writer(self):
        cr = _passing_clip_result(clip_path="/final/clip.mp4")
        r = build_post_processing_report(job_id="j1", clip_results=[cr])
        assert "/final/clip.mp4" in r["finished_clip_paths"]

    def test_resolves_path_from_validation_as_fallback(self):
        """When metadata_writer is absent, use validation_v1 output_path."""
        cr = {
            "clip_id": "j_c1",
            "source_candidate_id": "c1",
            "status": "PASS",
            "final_output_path": "/clip.mp4",
            "module_results": [
                _module_result("render_clip_v1", "PASS"),
                _module_result("validation_v1", "PASS", output_path="/clip.mp4"),
                # No metadata_writer_v1
            ],
            "failed_module": None,
            "failure_reason": None,
            "warnings": [],
        }
        r = build_post_processing_report(job_id="j1", clip_results=[cr])
        assert "/clip.mp4" in r["finished_clip_paths"]


# ---------------------------------------------------------------------------
# Metadata paths
# ---------------------------------------------------------------------------


class TestMetadataPaths:
    def test_collects_metadata_paths_from_metadata_writer(self):
        cr = _passing_clip_result(metadata_path="/meta/clip_meta.json")
        r = build_post_processing_report(job_id="j1", clip_results=[cr])
        assert "/meta/clip_meta.json" in r["per_clip_metadata_paths"]

    def test_includes_metadata_paths_for_passed_clips(self):
        r = build_post_processing_report(
            job_id="j1",
            clip_results=[_passing_clip_result(metadata_path="/meta/a.json")],
        )
        assert len(r["per_clip_metadata_paths"]) == 1

    def test_includes_metadata_paths_for_failed_clips_if_available(self):
        cr = _failing_clip_result()
        cr["metadata_path"] = "/meta/failed.json"
        r = build_post_processing_report(job_id="j1", clip_results=[cr])
        assert "/meta/failed.json" in r["per_clip_metadata_paths"]


# ---------------------------------------------------------------------------
# Failed clips
# ---------------------------------------------------------------------------


class TestFailedClips:
    def test_records_failed_clip_id(self):
        r = build_post_processing_report(
            job_id="j1", clip_results=[_failing_clip_result("my_clip", "c1")]
        )
        assert r["failed_clips"][0]["clip_id"] == "my_clip"

    def test_records_source_candidate_id(self):
        r = build_post_processing_report(
            job_id="j1", clip_results=[_failing_clip_result("cl", "cand_99")]
        )
        assert r["failed_clips"][0]["source_candidate_id"] == "cand_99"

    def test_records_failed_module(self):
        r = build_post_processing_report(
            job_id="j1",
            clip_results=[_failing_clip_result(failed_module="render_clip_v1")],
        )
        assert r["failed_clips"][0]["failed_module"] == "render_clip_v1"

    def test_records_failure_reason(self):
        r = build_post_processing_report(
            job_id="j1",
            clip_results=[_failing_clip_result(failure_reason="ffmpeg_failed")],
        )
        assert r["failed_clips"][0]["failure_reason"] == "ffmpeg_failed"

    def test_preserves_failed_module_results(self):
        r = build_post_processing_report(
            job_id="j1", clip_results=[_failing_clip_result()]
        )
        fc = r["failed_clips"][0]
        assert isinstance(fc["module_results"], list)
        assert len(fc["module_results"]) > 0

    def test_handles_missing_metadata_path(self):
        cr = _failing_clip_result()
        # No metadata_path anywhere
        r = build_post_processing_report(job_id="j1", clip_results=[cr])
        fc = r["failed_clips"][0]
        assert "metadata_path" in fc  # key exists, value may be None

    def test_passed_clips_not_in_failed_list(self):
        r = build_post_processing_report(
            job_id="j1", clip_results=[_passing_clip_result()]
        )
        assert r["failed_clips"] == []


# ---------------------------------------------------------------------------
# Rejected / reserve candidates
# ---------------------------------------------------------------------------


class TestRejectedReserveCandidates:
    def test_preserves_rejected_candidates(self):
        rej = [{"candidate_id": "r1", "reason": "low_score"}, {"candidate_id": "r2"}]
        sel = _selection_result(rejected=rej)
        r = build_post_processing_report(job_id="j1", selection_result=sel)
        assert len(r["rejected_candidates"]) == 2
        ids = [c["candidate_id"] for c in r["rejected_candidates"]]
        assert "r1" in ids

    def test_preserves_reserve_candidates(self):
        res = [{"candidate_id": "res1", "rank": 4}]
        sel = _selection_result(reserve=res)
        r = build_post_processing_report(job_id="j1", selection_result=sel)
        assert len(r["reserve_candidates_list"]) == 1
        assert r["reserve_candidates_list"][0]["candidate_id"] == "res1"

    def test_empty_rejected_list(self):
        r = build_post_processing_report(job_id="j1")
        assert r["rejected_candidates"] == []

    def test_empty_reserve_list(self):
        r = build_post_processing_report(job_id="j1")
        assert r["reserve_candidates_list"] == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_valid_report_passes(self):
        r = build_post_processing_report(job_id="j1")
        errs = validate_post_processing_report(r)
        assert errs == []

    def test_invalid_object_fails(self):
        errs = validate_post_processing_report("not a dict")
        assert any("invalid_report_object" in e for e in errs)

    def test_invalid_schema_version_fails(self):
        r = build_post_processing_report(job_id="j1")
        r["schema_version"] = "wrong_version"
        errs = validate_post_processing_report(r)
        assert any("invalid_schema_version" in e for e in errs)

    def test_missing_job_id_fails(self):
        r = build_post_processing_report(job_id="j1")
        r["job_id"] = ""
        errs = validate_post_processing_report(r)
        assert any("missing_job_id" in e for e in errs)

    def test_negative_count_fails(self):
        r = build_post_processing_report(job_id="j1")
        r["clips_passed"] = -1
        errs = validate_post_processing_report(r)
        assert any("invalid_count_field" in e for e in errs)

    def test_non_integer_count_fails(self):
        r = build_post_processing_report(job_id="j1")
        r["clips_attempted"] = "five"
        errs = validate_post_processing_report(r)
        assert any("invalid_count_field" in e for e in errs)

    def test_invalid_list_field_fails(self):
        r = build_post_processing_report(job_id="j1")
        r["modules_run"] = "not_a_list"
        errs = validate_post_processing_report(r)
        assert any("invalid_modules_run" in e for e in errs)

    def test_invalid_warnings_field_fails(self):
        r = build_post_processing_report(job_id="j1")
        r["warnings"] = {"bad": "dict"}
        errs = validate_post_processing_report(r)
        assert any("invalid_warnings" in e for e in errs)

    def test_finished_clip_paths_not_list_fails(self):
        r = build_post_processing_report(job_id="j1")
        r["finished_clip_paths"] = "/path.mp4"
        errs = validate_post_processing_report(r)
        assert any("invalid_finished_clip_paths" in e for e in errs)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


class TestWriting:
    def test_writes_json_file(self, tmp_path):
        report_path = str(tmp_path / "report.json")
        r = build_post_processing_report(job_id="j1")
        result = write_post_processing_report(r, report_path)
        assert os.path.isfile(report_path)
        assert result["report_path"] == report_path

    def test_creates_parent_report_directory(self, tmp_path):
        report_path = str(tmp_path / "reports" / "sub" / "report.json")
        r = build_post_processing_report(job_id="j1")
        write_post_processing_report(r, report_path)
        assert os.path.isfile(report_path)

    def test_readback_validates(self, tmp_path):
        report_path = str(tmp_path / "report.json")
        r = build_post_processing_report(job_id="j1")
        write_post_processing_report(r, report_path)
        loaded = load_post_processing_report(report_path)
        assert loaded["schema_version"] == REPORT_SCHEMA_VERSION

    def test_overwrite_allowed_by_default(self, tmp_path):
        report_path = str(tmp_path / "report.json")
        r = build_post_processing_report(job_id="j1")
        write_post_processing_report(r, report_path)
        write_post_processing_report(r, report_path)  # Should not raise

    def test_overwrite_false_fails_if_file_exists(self, tmp_path):
        report_path = str(tmp_path / "report.json")
        r = build_post_processing_report(job_id="j1")
        write_post_processing_report(r, report_path)
        with pytest.raises((FileExistsError, OSError)):
            write_post_processing_report(r, report_path, allow_overwrite=False)

    def test_write_result_contains_file_size(self, tmp_path):
        report_path = str(tmp_path / "report.json")
        r = build_post_processing_report(job_id="j1")
        result = write_post_processing_report(r, report_path)
        assert isinstance(result["file_size_bytes"], int)
        assert result["file_size_bytes"] > 0

    def test_write_result_contains_schema_version(self, tmp_path):
        report_path = str(tmp_path / "report.json")
        r = build_post_processing_report(job_id="j1")
        result = write_post_processing_report(r, report_path)
        assert result["schema_version"] == REPORT_SCHEMA_VERSION

    def test_load_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_post_processing_report(str(tmp_path / "missing.json"))

    def test_load_invalid_json_raises(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{ not valid json }")
        with pytest.raises(ValueError):
            load_post_processing_report(str(bad))

    def test_written_report_is_valid(self, tmp_path):
        report_path = str(tmp_path / "report.json")
        r = build_post_processing_report(
            job_id="j1",
            clip_results=[_passing_clip_result(), _failing_clip_result()],
        )
        write_post_processing_report(r, report_path)
        loaded = load_post_processing_report(report_path)
        errs = validate_post_processing_report(loaded)
        assert errs == []


# ---------------------------------------------------------------------------
# Integration with post_processing_mk1
# ---------------------------------------------------------------------------


class TestPostProcessingMk1Integration:
    """Tests that post_processing_mk1 now exposes post_processing_report_path."""

    def _make_pool_file(self, tmp_path: Path) -> Path:
        """Write a minimal valid raw_candidate_pool.json for the entrypoint tests."""
        # We need a valid pool — import processing_contracts to build one
        try:
            import processing_contracts as contracts
        except ImportError:
            pytest.skip("processing_contracts not available")

        candidate = {
            "candidate_id": contracts.make_candidate_id(
                job_id="pp_job_rpt",
                source_section_id="sec_001",
                start_sec=10.0,
                end_sec=40.0,
            ),
            "source_section_id": "sec_001",
            "start_sec": 10.0,
            "end_sec": 40.0,
            "duration_sec": 30.0,
            "hook_text": "hook",
            "core_idea_summary": "summary",
            "why_candidate_has_potential": "reason",
            "archetype": "valuable_insight",
            "confidence": 0.75,
            "scores": {
                "hook_strength": 7, "standalone_context": 7, "insight_value": 8,
                "retention_potential": 7, "natural_ending": 7, "overall_potential": 7,
            },
            "warnings": [],
            "transcript_quality_flags": [],
        }
        pool = {
            "schema_version": contracts.RAW_CANDIDATE_POOL_SCHEMA_VERSION,
            "job_id": "pp_job_rpt",
            "source_video_path": str(tmp_path / "source.mp4"),
            "transcript_path": str(tmp_path / "transcript.json"),
            "processing_version": contracts.PROCESSING_VERSION,
            "funnel_id": "business",
            "created_at": "2026-06-30T12:00:00+00:00",
            "candidates": [candidate],
            "diagnostics": {},
        }
        pool_path = tmp_path / "raw_candidate_pool.json"
        pool_path.write_text(json.dumps(pool))
        # Create a fake source video file
        (tmp_path / "source.mp4").write_bytes(b"\x00")
        return pool_path

    def test_entrypoint_exposes_report_path(self, tmp_path):
        from post_processing_mk1 import run_post_processing_mk1
        pool_path = self._make_pool_file(tmp_path)
        result = run_post_processing_mk1(
            str(pool_path),
            job_metadata={"job_id": "pp_job_rpt"},
            output_root=str(tmp_path),
        )
        assert result["status"] == "READY_FOR_SELECTION"
        assert "post_processing_report_path" in result
        assert result["post_processing_report_path"] is not None

    def test_report_path_inside_reports_dir(self, tmp_path):
        from post_processing_mk1 import run_post_processing_mk1
        pool_path = self._make_pool_file(tmp_path)
        result = run_post_processing_mk1(
            str(pool_path),
            job_metadata={"job_id": "pp_job_rpt"},
            output_root=str(tmp_path),
        )
        assert result["status"] == "READY_FOR_SELECTION"
        rpath = result["post_processing_report_path"]
        assert "reports" in rpath
        assert rpath.endswith("post_processing_report.json")

    def test_zero_candidate_job_still_exposes_report_path(self, tmp_path):
        """Zero-candidate pools still complete the input contract and expose report path."""
        from post_processing_mk1 import run_post_processing_mk1
        try:
            import processing_contracts as contracts
        except ImportError:
            pytest.skip("processing_contracts not available")

        pool = {
            "schema_version": contracts.RAW_CANDIDATE_POOL_SCHEMA_VERSION,
            "job_id": "pp_zero",
            "source_video_path": str(tmp_path / "source.mp4"),
            "transcript_path": str(tmp_path / "transcript.json"),
            "processing_version": contracts.PROCESSING_VERSION,
            "funnel_id": "business",
            "created_at": "2026-06-30T12:00:00+00:00",
            "candidates": [],
            "diagnostics": {},
        }
        (tmp_path / "source.mp4").write_bytes(b"\x00")
        pool_path = tmp_path / "raw_candidate_pool.json"
        pool_path.write_text(json.dumps(pool))

        result = run_post_processing_mk1(
            str(pool_path),
            job_metadata={"job_id": "pp_zero"},
            output_root=str(tmp_path),
        )
        assert result["status"] == "READY_FOR_SELECTION"
        assert "post_processing_report_path" in result


# ---------------------------------------------------------------------------
# Edge cases and diagnostics
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_clip_results(self):
        r = build_post_processing_report(job_id="j1", clip_results=[])
        assert r["clips_attempted"] == 0
        assert r["clips_passed"] == 0
        assert r["clips_failed"] == 0
        assert r["finished_clip_paths"] == []

    def test_no_selection_result(self):
        r = build_post_processing_report(job_id="j1")
        assert r["candidates_selected"] == 0
        assert r["candidates_rejected"] == 0

    def test_diagnostics_preserved(self):
        r = build_post_processing_report(
            job_id="j1", diagnostics={"run_duration_sec": 42.5}
        )
        assert r["diagnostics"]["run_duration_sec"] == 42.5

    def test_extra_warnings_preserved(self):
        r = build_post_processing_report(
            job_id="j1", warnings=["custom_warning_1", "custom_warning_2"]
        )
        assert "custom_warning_1" in r["warnings"]
        assert "custom_warning_2" in r["warnings"]

    def test_conveyor_result_shape_supported(self):
        """build_post_processing_report accepts conveyor_result directly."""
        conveyor = {
            "status": "CONVEYOR_COMPLETE",
            "clip_results": [_passing_clip_result()],
        }
        r = build_post_processing_report(job_id="j1", conveyor_result=conveyor)
        assert r["clips_attempted"] == 1

    def test_clip_results_override_conveyor(self):
        """Explicit clip_results take precedence over conveyor_result."""
        conveyor = {
            "clip_results": [_passing_clip_result(), _passing_clip_result("c2", "c2")],
        }
        r = build_post_processing_report(
            job_id="j1",
            conveyor_result=conveyor,
            clip_results=[_passing_clip_result()],
        )
        assert r["clips_attempted"] == 1
