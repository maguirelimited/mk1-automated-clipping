from __future__ import annotations

import os
import sys

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from processing_contracts import (  # noqa: E402
    PROCESSING_REPORT_SCHEMA_VERSION,
    PROCESSING_VERSION,
    validate_processing_report,
)
from processing_diagnostics import build_processing_diagnostics_report  # noqa: E402


def _candidate(candidate_id: str) -> dict:
    return {
        "candidate_local_id": candidate_id,
        "source_section_id": "section_0001",
        "start_sec": 100.0,
        "end_sec": 140.0,
        "duration_sec": 40.0,
    }


def _batch() -> dict:
    return {
        "schema_version": "section_candidate_discovery_batch_v1",
        "sections_received": 3,
        "sections_processed": 2,
        "usable_sections": 1,
        "rejected_sections": 1,
        "candidates_discovered": 1,
        "duplicates_removed": 1,
        "section_results": [
            {
                "section_id": "section_0001",
                "usable": True,
                "candidates": [_candidate("cand_1"), _candidate("cand_2")],
                "prompt_metadata": {
                    "base_prompt_version": "section_candidate_discovery_base_v1",
                    "requested_funnel_id": "business",
                    "resolved_funnel_id": "business",
                    "funnel_rules_version": "business_v1",
                },
            },
            {
                "section_id": "section_0002",
                "usable": False,
                "candidates": [],
            },
        ],
        "rejected_candidates": [
            {
                "source_section_id": "section_0001",
                "candidate_local_id": "bad_1",
                "rejection_reasons": ["duration_too_short"],
            }
        ],
        "duplicate_removals": [
            {
                "removed_candidate_id": "dup_1",
                "kept_candidate_id": "cand_1",
                "reason": "timestamp_duplicate",
                "selection_reason": "higher_overall_potential",
            }
        ],
        "warnings": ["all_candidates_removed_as_timestamp_duplicates"],
        "failed_sections": [
            {
                "section_id": "section_0003",
                "error_code": "MODEL_JSON_INVALID",
                "error_reason": "Model output was not strict JSON.",
            }
        ],
    }


def test_processing_diagnostics_report_validates():
    report = build_processing_diagnostics_report(
        job_id="job_123",
        discovery_batch=_batch(),
        created_at="2026-06-30T12:00:00+00:00",
    )

    validate_processing_report(report)
    assert report["schema_version"] == PROCESSING_REPORT_SCHEMA_VERSION
    assert report["processing_version"] == PROCESSING_VERSION


def test_zero_candidate_sections_are_counted_as_rejected_not_failed():
    report = build_processing_diagnostics_report(
        job_id="job_123",
        discovery_batch={
            **_batch(),
            "section_results": [{"section_id": "section_0001", "usable": False, "candidates": []}],
            "failed_sections": [],
            "rejected_candidates": [],
            "duplicate_removals": [],
        },
        created_at="2026-06-30T12:00:00+00:00",
    )

    assert report["usable_sections"] == 0
    assert report["rejected_sections"] == 1
    assert report["failed_sections"] == []


def test_usable_and_rejected_sections_are_counted_from_actual_results():
    report = build_processing_diagnostics_report(
        job_id="job_123",
        discovery_batch=_batch(),
        created_at="2026-06-30T12:00:00+00:00",
    )

    assert report["sections_analysed"] == 3
    assert report["usable_sections"] == 1
    assert report["rejected_sections"] == 1


def test_failed_sections_are_preserved():
    report = build_processing_diagnostics_report(
        job_id="job_123",
        discovery_batch=_batch(),
        created_at="2026-06-30T12:00:00+00:00",
    )

    assert report["failed_sections"][0]["section_id"] == "section_0003"
    assert report["failed_sections"][0]["error_code"] == "MODEL_JSON_INVALID"


def test_candidate_counts_match_actual_discovered_and_final_candidates():
    report = build_processing_diagnostics_report(
        job_id="job_123",
        discovery_batch=_batch(),
        created_at="2026-06-30T12:00:00+00:00",
    )

    assert report["final_candidate_count"] == 2
    assert report["candidates_rejected_by_boundary"] == 1
    assert report["duplicates_removed"] == 1
    assert report["candidates_discovered"] == 4


def test_boundary_rejection_and_duplicate_counts_are_included():
    report = build_processing_diagnostics_report(
        job_id="job_123",
        discovery_batch=_batch(),
        created_at="2026-06-30T12:00:00+00:00",
    )

    assert report["candidates_rejected_by_boundary"] == 1
    assert report["duplicates_removed"] == 1


def test_transcript_and_processing_warnings_are_preserved():
    report = build_processing_diagnostics_report(
        job_id="job_123",
        discovery_batch={**_batch(), "transcript_warnings": ["poor_punctuation"]},
        processing_warnings=["manual_processing_warning"],
        created_at="2026-06-30T12:00:00+00:00",
    )

    assert report["transcript_warnings"] == ["poor_punctuation"]
    assert "all_candidates_removed_as_timestamp_duplicates" in report["processing_warnings"]
    assert "manual_processing_warning" in report["processing_warnings"]


def test_common_rejection_reasons_are_derived_from_boundary_and_failures():
    report = build_processing_diagnostics_report(
        job_id="job_123",
        discovery_batch=_batch(),
        created_at="2026-06-30T12:00:00+00:00",
    )

    assert "duration_too_short" in report["common_rejection_reasons"]
    assert "MODEL_JSON_INVALID" in report["common_rejection_reasons"]


def test_prompt_metadata_from_funnel_routing_is_preserved():
    report = build_processing_diagnostics_report(
        job_id="job_123",
        discovery_batch=_batch(),
        created_at="2026-06-30T12:00:00+00:00",
    )

    assert report["funnel_id"] == "business"
    assert report["prompt_metadata"]["resolved_funnel_id"] == "business"
    assert report["prompt_metadata"]["funnel_rules_version"] == "business_v1"


def test_report_generation_requires_no_post_processing_or_rendering(monkeypatch):
    original_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name in {"clip_video", "output_funnel"} or name.startswith("output_funnel."):
            raise AssertionError("diagnostics should not import rendering/output-funnel code")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    report = build_processing_diagnostics_report(
        job_id="job_123",
        discovery_batch=_batch(),
        created_at="2026-06-30T12:00:00+00:00",
    )

    assert report["job_id"] == "job_123"
