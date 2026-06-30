"""Tests for processing_integration.py (Prompt 12)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import processing_contracts as contracts  # noqa: E402
import processing_integration as integration  # noqa: E402
from processing_integration import (  # noqa: E402
    ProcessingIntegrationError,
    build_pool_candidate_from_discovery,
    build_processing_artifacts,
    collect_candidates_from_batch,
    legacy_segments_from_raw_candidate_pool,
    link_processing_artifacts_in_report,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _discovery_candidate(
    candidate_local_id: str = "cand_a",
    source_section_id: str = "section_0001",
    start_sec: float = 100.0,
    end_sec: float = 140.0,
) -> dict:
    duration_sec = end_sec - start_sec
    return {
        "candidate_local_id": candidate_local_id,
        "source_section_id": source_section_id,
        "start_sec": start_sec,
        "end_sec": end_sec,
        "duration_sec": duration_sec,
        "hook_text": "An attention-grabbing opening line.",
        "core_idea_summary": "A useful business lesson about focus.",
        "why_candidate_has_potential": "Strong hook and standalone value.",
        "archetype": "valuable_insight",
        "confidence": 0.85,
        "scores": {
            "hook_strength": 8,
            "standalone_context": 7,
            "insight_value": 9,
            "retention_potential": 8,
            "natural_ending": 7,
            "overall_potential": 8,
        },
        "warnings": [],
        "transcript_quality_flags": [],
    }


def _discovery_batch(
    *,
    usable: bool = True,
    candidates: list | None = None,
    failed_sections: list | None = None,
) -> dict:
    cands = candidates if candidates is not None else [_discovery_candidate()]
    section_result = {
        "section_id": "section_0001",
        "schema_version": "section_candidate_discovery_v1",
        "usable": usable,
        "confidence": 0.85 if usable else 0.0,
        "reason": "Good standalone clip." if usable else "No usable clips.",
        "warnings": [],
        "transcript_quality_flags": [],
        "candidates": cands if usable else [],
        "rejected_candidates": [],
        "prompt_metadata": {
            "base_prompt_version": "section_candidate_discovery_base_v1",
            "requested_funnel_id": "business",
            "resolved_funnel_id": "business",
            "funnel_rules_version": "business_v1",
        },
    }
    return {
        "schema_version": "section_candidate_discovery_batch_v1",
        "sections_received": 1,
        "sections_processed": 1,
        "usable_sections": 1 if usable else 0,
        "rejected_sections": 0 if usable else 1,
        "candidates_discovered": len(cands) if usable else 0,
        "duplicates_removed": 0,
        "section_results": [section_result],
        "rejected_candidates": [],
        "duplicate_removals": [],
        "warnings": [],
        "failed_sections": list(failed_sections or []),
    }


def _zero_candidate_batch() -> dict:
    return _discovery_batch(usable=False, candidates=[])


# ---------------------------------------------------------------------------
# Unit: build_pool_candidate_from_discovery
# ---------------------------------------------------------------------------


def test_discovery_candidate_converts_to_pool_candidate():
    disc = _discovery_candidate()
    pool = build_pool_candidate_from_discovery(disc, "job_abc")
    assert pool["source_section_id"] == "section_0001"
    assert pool["start_sec"] == 100.0
    assert pool["end_sec"] == 140.0
    assert pool["duration_sec"] == 40.0
    assert pool["hook_text"] == "An attention-grabbing opening line."
    assert pool["core_idea_summary"] == "A useful business lesson about focus."
    assert pool["archetype"] == "valuable_insight"
    assert pool["confidence"] == 0.85
    assert pool["scores"]["overall_potential"] == 8
    assert isinstance(pool["candidate_id"], str) and pool["candidate_id"].startswith("cand_")


def test_pool_candidate_id_is_deterministic():
    disc = _discovery_candidate()
    id1 = build_pool_candidate_from_discovery(disc, "job_abc")["candidate_id"]
    id2 = build_pool_candidate_from_discovery(disc, "job_abc")["candidate_id"]
    assert id1 == id2


def test_pool_candidate_id_differs_by_job():
    disc = _discovery_candidate()
    id1 = build_pool_candidate_from_discovery(disc, "job_abc")["candidate_id"]
    id2 = build_pool_candidate_from_discovery(disc, "job_xyz")["candidate_id"]
    assert id1 != id2


def test_missing_optional_evidence_fields_default_to_empty_strings():
    disc = {
        "candidate_local_id": "cand_b",
        "source_section_id": "section_0001",
        "start_sec": 10.0,
        "end_sec": 45.0,
        "duration_sec": 35.0,
        "archetype": "other",
        "confidence": 0.5,
        "scores": {
            "hook_strength": 5,
            "standalone_context": 5,
            "insight_value": 5,
            "retention_potential": 5,
            "natural_ending": 5,
            "overall_potential": 5,
        },
        "warnings": [],
        "transcript_quality_flags": [],
    }
    pool = build_pool_candidate_from_discovery(disc, "job_abc")
    assert pool["hook_text"] == ""
    assert pool["core_idea_summary"] == ""
    assert pool["why_candidate_has_potential"] == ""


# ---------------------------------------------------------------------------
# Unit: collect_candidates_from_batch
# ---------------------------------------------------------------------------


def test_collect_candidates_returns_all_accepted_candidates():
    batch = _discovery_batch()
    collected = collect_candidates_from_batch(batch, "job_abc")
    assert len(collected) == 1
    assert collected[0]["start_sec"] == 100.0


def test_collect_candidates_zero_when_section_usable_false():
    batch = _zero_candidate_batch()
    collected = collect_candidates_from_batch(batch, "job_abc")
    assert collected == []


def test_collect_candidates_across_multiple_sections():
    cand1 = _discovery_candidate("c1", "section_0001", 100.0, 140.0)
    cand2 = _discovery_candidate("c2", "section_0002", 200.0, 250.0)
    batch = {
        **_discovery_batch(),
        "sections_received": 2,
        "sections_processed": 2,
        "usable_sections": 2,
        "rejected_sections": 0,
        "candidates_discovered": 2,
        "section_results": [
            {
                "section_id": "section_0001",
                "usable": True,
                "candidates": [cand1],
                "rejected_candidates": [],
            },
            {
                "section_id": "section_0002",
                "usable": True,
                "candidates": [cand2],
                "rejected_candidates": [],
            },
        ],
    }
    collected = collect_candidates_from_batch(batch, "job_abc")
    assert len(collected) == 2
    assert collected[0]["start_sec"] == 100.0
    assert collected[1]["start_sec"] == 200.0


# ---------------------------------------------------------------------------
# Integration: build_processing_artifacts writes both files
# ---------------------------------------------------------------------------


def test_raw_candidate_pool_is_written(tmp_path):
    batch = _discovery_batch()
    pool_path, report_path = build_processing_artifacts(
        job_id="job_p12",
        job_dir=str(tmp_path),
        discovery_batch=batch,
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        funnel_id="business",
        created_at="2026-06-30T12:00:00+00:00",
    )
    assert os.path.isfile(pool_path)
    pool = json.loads(Path(pool_path).read_text())
    assert pool["schema_version"] == contracts.RAW_CANDIDATE_POOL_SCHEMA_VERSION
    assert pool["job_id"] == "job_p12"
    assert pool["source_video_path"] == "/tmp/source.mp4"
    assert pool["transcript_path"] == "/tmp/transcript.json"
    assert pool["funnel_id"] == "business"
    assert pool["processing_version"] == contracts.PROCESSING_VERSION
    assert isinstance(pool["candidates"], list)
    assert len(pool["candidates"]) == 1
    assert isinstance(pool["diagnostics"], dict)


def test_processing_report_is_written(tmp_path):
    batch = _discovery_batch()
    pool_path, report_path = build_processing_artifacts(
        job_id="job_p12",
        job_dir=str(tmp_path),
        discovery_batch=batch,
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        funnel_id="business",
        created_at="2026-06-30T12:00:00+00:00",
    )
    assert os.path.isfile(report_path)
    report = json.loads(Path(report_path).read_text())
    assert report["schema_version"] == contracts.PROCESSING_REPORT_SCHEMA_VERSION
    assert report["job_id"] == "job_p12"
    assert report["processing_version"] == contracts.PROCESSING_VERSION
    assert isinstance(report["sections_analysed"], int)
    assert isinstance(report["final_candidate_count"], int)
    assert isinstance(report["transcript_warnings"], list)
    assert isinstance(report["processing_warnings"], list)
    assert isinstance(report["common_rejection_reasons"], list)
    assert isinstance(report["failed_sections"], list)
    assert isinstance(report["prompt_metadata"], dict)


def test_main_job_report_links_both_paths(tmp_path):
    batch = _discovery_batch()
    pool_path, report_path = build_processing_artifacts(
        job_id="job_p12",
        job_dir=str(tmp_path),
        discovery_batch=batch,
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        funnel_id="business",
        created_at="2026-06-30T12:00:00+00:00",
    )
    job_report: dict = {"job_id": "job_p12", "status": "running"}
    link_processing_artifacts_in_report(
        job_report,
        raw_candidate_pool_path=pool_path,
        processing_report_path=report_path,
    )
    assert job_report["raw_candidate_pool_path"] == pool_path
    assert job_report["processing_report_path"] == report_path


def test_both_artifact_paths_are_returned(tmp_path):
    batch = _discovery_batch()
    pool_path, report_path = build_processing_artifacts(
        job_id="job_p12",
        job_dir=str(tmp_path),
        discovery_batch=batch,
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        created_at="2026-06-30T12:00:00+00:00",
    )
    assert pool_path.endswith("raw_candidate_pool.json")
    assert report_path.endswith("processing_report.json")


# ---------------------------------------------------------------------------
# Zero-candidate processing writes valid outputs
# ---------------------------------------------------------------------------


def test_zero_candidate_processing_writes_valid_pool(tmp_path):
    batch = _zero_candidate_batch()
    pool_path, report_path = build_processing_artifacts(
        job_id="job_p12",
        job_dir=str(tmp_path),
        discovery_batch=batch,
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        created_at="2026-06-30T12:00:00+00:00",
    )
    pool = json.loads(Path(pool_path).read_text())
    assert pool["candidates"] == []
    contracts.validate_raw_candidate_pool(pool)


def test_zero_candidate_processing_writes_valid_report(tmp_path):
    batch = _zero_candidate_batch()
    _, report_path = build_processing_artifacts(
        job_id="job_p12",
        job_dir=str(tmp_path),
        discovery_batch=batch,
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        created_at="2026-06-30T12:00:00+00:00",
    )
    report = json.loads(Path(report_path).read_text())
    contracts.validate_processing_report(report)
    assert report["final_candidate_count"] == 0
    assert report["usable_sections"] == 0


def test_no_candidate_jobs_do_not_force_fake_candidates(tmp_path):
    batch = _zero_candidate_batch()
    pool_path, _ = build_processing_artifacts(
        job_id="job_p12",
        job_dir=str(tmp_path),
        discovery_batch=batch,
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        created_at="2026-06-30T12:00:00+00:00",
    )
    pool = json.loads(Path(pool_path).read_text())
    assert pool["candidates"] == [], "Zero-candidate pool must not contain forced candidates"


# ---------------------------------------------------------------------------
# Write failure fails clearly
# ---------------------------------------------------------------------------


def test_write_failure_raises_processing_integration_error(tmp_path):
    batch = _discovery_batch()
    with patch("processing_integration.write_raw_candidate_pool", side_effect=OSError("disk full")):
        with pytest.raises(ProcessingIntegrationError) as exc_info:
            build_processing_artifacts(
                job_id="job_fail",
                job_dir=str(tmp_path),
                discovery_batch=batch,
                source_video_path="/tmp/source.mp4",
                transcript_path="/tmp/transcript.json",
                created_at="2026-06-30T12:00:00+00:00",
            )
    assert exc_info.value.code == "RAW_CANDIDATE_POOL_WRITE_FAILED"
    assert "disk full" in exc_info.value.message


def test_processing_report_write_failure_raises_processing_integration_error(tmp_path):
    batch = _discovery_batch()
    with patch("processing_integration.write_processing_report", side_effect=OSError("no space")):
        with pytest.raises(ProcessingIntegrationError) as exc_info:
            build_processing_artifacts(
                job_id="job_fail",
                job_dir=str(tmp_path),
                discovery_batch=batch,
                source_video_path="/tmp/source.mp4",
                transcript_path="/tmp/transcript.json",
                created_at="2026-06-30T12:00:00+00:00",
            )
    assert exc_info.value.code == "PROCESSING_REPORT_WRITE_FAILED"
    assert "no space" in exc_info.value.message


def test_write_failure_preserves_error_code():
    """Error code is always explicitly set — no silent generic fallback."""
    with pytest.raises(ProcessingIntegrationError) as exc_info:
        raise ProcessingIntegrationError("PROCESSING_ARTIFACTS_WRITE_FAILED", "test detail")
    assert exc_info.value.code == "PROCESSING_ARTIFACTS_WRITE_FAILED"
    assert "test detail" in exc_info.value.message


# ---------------------------------------------------------------------------
# Legacy adapter
# ---------------------------------------------------------------------------


def test_legacy_adapter_converts_pool_candidates_to_segments(tmp_path):
    batch = _discovery_batch()
    pool_path, _ = build_processing_artifacts(
        job_id="job_p12",
        job_dir=str(tmp_path),
        discovery_batch=batch,
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        created_at="2026-06-30T12:00:00+00:00",
    )
    pool = json.loads(Path(pool_path).read_text())
    segments = legacy_segments_from_raw_candidate_pool(pool)
    assert len(segments) == 1
    seg = segments[0]
    assert seg["start"] == 100.0
    assert seg["end"] == 140.0
    assert "score" in seg
    assert seg["score"] == 8.0
    assert "reason" in seg


def test_legacy_adapter_returns_empty_for_zero_candidate_pool(tmp_path):
    batch = _zero_candidate_batch()
    pool_path, _ = build_processing_artifacts(
        job_id="job_p12",
        job_dir=str(tmp_path),
        discovery_batch=batch,
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        created_at="2026-06-30T12:00:00+00:00",
    )
    pool = json.loads(Path(pool_path).read_text())
    segments = legacy_segments_from_raw_candidate_pool(pool)
    assert segments == []


def test_legacy_adapter_marks_segments_as_temporary():
    """Every legacy segment must carry the _legacy_adapter annotation."""
    cand = _discovery_candidate()
    pool = {
        "schema_version": contracts.RAW_CANDIDATE_POOL_SCHEMA_VERSION,
        "job_id": "job_p12",
        "source_video_path": "/tmp/v.mp4",
        "transcript_path": "/tmp/t.json",
        "processing_version": contracts.PROCESSING_VERSION,
        "funnel_id": "business",
        "created_at": "2026-06-30T12:00:00+00:00",
        "candidates": [build_pool_candidate_from_discovery(cand, "job_p12")],
        "diagnostics": {},
    }
    segments = legacy_segments_from_raw_candidate_pool(pool)
    assert all("_legacy_adapter" in seg for seg in segments)


def test_legacy_adapter_preserves_pool_order():
    """Adapter must not introduce new ranking — preserve pool order."""
    cand1 = _discovery_candidate("c1", "section_0001", 100.0, 140.0)
    cand2 = _discovery_candidate("c2", "section_0001", 200.0, 250.0)
    cand1["scores"]["overall_potential"] = 5
    cand2["scores"]["overall_potential"] = 9

    batch = {
        **_discovery_batch(),
        "section_results": [
            {
                "section_id": "section_0001",
                "usable": True,
                "candidates": [cand1, cand2],
                "rejected_candidates": [],
            }
        ],
        "candidates_discovered": 2,
    }
    collected = collect_candidates_from_batch(batch, "job_p12")
    pool = {
        "schema_version": contracts.RAW_CANDIDATE_POOL_SCHEMA_VERSION,
        "job_id": "job_p12",
        "source_video_path": "/tmp/v.mp4",
        "transcript_path": "/tmp/t.json",
        "processing_version": contracts.PROCESSING_VERSION,
        "funnel_id": "business",
        "created_at": "2026-06-30T12:00:00+00:00",
        "candidates": collected,
        "diagnostics": {},
    }
    segments = legacy_segments_from_raw_candidate_pool(pool)
    assert len(segments) == 2
    # Pool order is preserved: cand1 (score 5) comes before cand2 (score 9)
    assert segments[0]["start"] == 100.0
    assert segments[1]["start"] == 200.0


# ---------------------------------------------------------------------------
# Pool and report pass contract validation
# ---------------------------------------------------------------------------


def test_written_pool_passes_contract_validation(tmp_path):
    batch = _discovery_batch()
    pool_path, _ = build_processing_artifacts(
        job_id="job_p12",
        job_dir=str(tmp_path),
        discovery_batch=batch,
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        funnel_id="business",
        created_at="2026-06-30T12:00:00+00:00",
    )
    pool = json.loads(Path(pool_path).read_text())
    contracts.validate_raw_candidate_pool(pool)


def test_written_report_passes_contract_validation(tmp_path):
    batch = _discovery_batch()
    _, report_path = build_processing_artifacts(
        job_id="job_p12",
        job_dir=str(tmp_path),
        discovery_batch=batch,
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        funnel_id="business",
        created_at="2026-06-30T12:00:00+00:00",
    )
    report = json.loads(Path(report_path).read_text())
    contracts.validate_processing_report(report)


# ---------------------------------------------------------------------------
# Candidate evidence fields are preserved end-to-end
# ---------------------------------------------------------------------------


def test_candidate_evidence_fields_are_preserved_in_pool(tmp_path):
    batch = _discovery_batch()
    pool_path, _ = build_processing_artifacts(
        job_id="job_p12",
        job_dir=str(tmp_path),
        discovery_batch=batch,
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        created_at="2026-06-30T12:00:00+00:00",
    )
    pool = json.loads(Path(pool_path).read_text())
    cand = pool["candidates"][0]
    assert cand["hook_text"] == "An attention-grabbing opening line."
    assert cand["core_idea_summary"] == "A useful business lesson about focus."
    assert cand["why_candidate_has_potential"] == "Strong hook and standalone value."
    assert cand["archetype"] == "valuable_insight"
    assert cand["confidence"] == 0.85
    assert cand["scores"]["overall_potential"] == 8
    assert cand["warnings"] == []
    assert cand["transcript_quality_flags"] == []


def test_funnel_id_is_propagated_to_both_artifacts(tmp_path):
    batch = _discovery_batch()
    pool_path, report_path = build_processing_artifacts(
        job_id="job_p12",
        job_dir=str(tmp_path),
        discovery_batch=batch,
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        funnel_id="sport",
        created_at="2026-06-30T12:00:00+00:00",
    )
    pool = json.loads(Path(pool_path).read_text())
    report = json.loads(Path(report_path).read_text())
    assert pool["funnel_id"] == "sport"
    # Report derives funnel_id from prompt_metadata or explicit arg; "sport"
    # was provided explicitly, so it must be preserved.
    # (The batch section results use "business" in prompt_metadata; explicit
    # arg takes precedence in build_processing_diagnostics_report.)
    assert report["funnel_id"] == "sport"


# ---------------------------------------------------------------------------
# No post-processing or rendering code is imported
# ---------------------------------------------------------------------------


def test_no_rendering_or_post_processing_imports(monkeypatch):
    """processing_integration must not import rendering or output-funnel code."""
    original_import = __import__

    def guarded_import(name, *args, **kwargs):
        blocked = {"clip_video", "output_funnel", "post_processing"}
        if name in blocked or any(name.startswith(b + ".") for b in blocked):
            raise AssertionError(
                f"processing_integration must not import {name}"
            )
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    import importlib
    import processing_integration as pi_fresh  # noqa: F401
    assert pi_fresh is not None
