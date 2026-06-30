from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import processing_contracts as contracts  # noqa: E402


def _valid_candidate() -> dict:
    return {
        "candidate_id": contracts.make_candidate_id(
            job_id="job_123",
            source_section_id="section_001",
            start_sec=10.0,
            end_sec=40.0,
        ),
        "source_section_id": "section_001",
        "start_sec": 10.0,
        "end_sec": 40.0,
        "duration_sec": 30.0,
        "hook_text": "This is the hook.",
        "core_idea_summary": "A concise useful idea.",
        "why_candidate_has_potential": "It stands alone and has a clear payoff.",
        "archetype": "valuable_insight",
        "confidence": 0.82,
        "scores": {
            "hook_strength": 8,
            "standalone_context": 7,
            "insight_value": 9,
            "retention_potential": 8,
            "natural_ending": 7,
            "overall_potential": 8,
        },
        "warnings": ["soft_boundary_needs_review"],
        "transcript_quality_flags": ["poor_punctuation"],
    }


def _valid_pool(*, candidates: list[dict] | None = None) -> dict:
    return {
        "schema_version": contracts.RAW_CANDIDATE_POOL_SCHEMA_VERSION,
        "job_id": "job_123",
        "source_video_path": "/tmp/source.mp4",
        "transcript_path": "/tmp/transcript.json",
        "processing_version": contracts.PROCESSING_VERSION,
        "funnel_id": "business_ai",
        "created_at": "2026-06-30T12:00:00+00:00",
        "candidates": list(candidates or []),
        "diagnostics": {},
    }


def _valid_report() -> dict:
    return {
        "schema_version": contracts.PROCESSING_REPORT_SCHEMA_VERSION,
        "job_id": "job_123",
        "sections_analysed": 0,
        "usable_sections": 0,
        "rejected_sections": 0,
        "candidates_discovered": 0,
        "candidates_after_boundary_pass": 0,
        "duplicates_removed": 0,
        "final_candidate_count": 0,
        "transcript_warnings": [],
        "processing_warnings": [],
        "common_rejection_reasons": [],
        "failed_sections": [],
    }


def _assert_invalid(payload: dict, expected_text: str) -> None:
    with pytest.raises(contracts.ProcessingContractValidationError) as exc:
        contracts.validate_raw_candidate_pool(payload)
    assert expected_text in str(exc.value)
    assert exc.value.errors


def test_valid_empty_raw_candidate_pool_validates():
    contracts.validate_raw_candidate_pool(_valid_pool())


def test_valid_raw_candidate_pool_with_one_candidate_validates():
    contracts.validate_raw_candidate_pool(_valid_pool(candidates=[_valid_candidate()]))


def test_missing_required_top_level_field_fails():
    payload = _valid_pool()
    del payload["job_id"]
    _assert_invalid(payload, "root.job_id is required")


def test_invalid_timestamp_order_fails():
    candidate = _valid_candidate()
    candidate["end_sec"] = candidate["start_sec"]
    candidate["duration_sec"] = 0.0
    _assert_invalid(_valid_pool(candidates=[candidate]), "end_sec must be greater")


def test_duration_mismatch_fails():
    candidate = _valid_candidate()
    candidate["duration_sec"] = 29.0
    _assert_invalid(_valid_pool(candidates=[candidate]), "duration_sec must match")


def test_missing_score_field_fails():
    candidate = _valid_candidate()
    del candidate["scores"]["overall_potential"]
    _assert_invalid(_valid_pool(candidates=[candidate]), "scores.overall_potential is required")


def test_score_outside_range_fails():
    candidate = _valid_candidate()
    candidate["scores"]["hook_strength"] = 11
    _assert_invalid(_valid_pool(candidates=[candidate]), "scores.hook_strength")


def test_candidate_evidence_field_constants_match_raw_candidate_contract():
    for field in contracts.CANDIDATE_EVIDENCE_FIELDS:
        assert field in contracts.CANDIDATE_REQUIRED_FIELDS
    for field in contracts.CANDIDATE_EVIDENCE_TEXT_FIELDS:
        assert field in contracts.CANDIDATE_EVIDENCE_FIELDS


def test_invalid_archetype_fails():
    candidate = _valid_candidate()
    candidate["archetype"] = "viral_magic"
    _assert_invalid(_valid_pool(candidates=[candidate]), "archetype must be one of")


def test_allowed_candidate_archetype_constant_matches_contract_validator():
    assert contracts.ALLOWED_ARCHETYPES == frozenset(contracts.ALLOWED_CANDIDATE_ARCHETYPES)
    assert "unknown" not in contracts.ALLOWED_CANDIDATE_ARCHETYPES
    for archetype in contracts.ALLOWED_CANDIDATE_ARCHETYPES:
        candidate = _valid_candidate()
        candidate["archetype"] = archetype
        contracts.validate_raw_candidate_pool(_valid_pool(candidates=[candidate]))


def test_invalid_transcript_quality_flag_fails():
    candidate = _valid_candidate()
    candidate["transcript_quality_flags"] = ["bad_flag"]
    _assert_invalid(_valid_pool(candidates=[candidate]), "transcript_quality_flags[0]")


def test_warnings_list_is_preserved():
    candidate = _valid_candidate()
    candidate["warnings"] = ["soft_boundary_needs_review", "possible_duplicate"]
    payload = _valid_pool(candidates=[candidate])

    contracts.validate_raw_candidate_pool(payload)

    assert payload["candidates"][0]["warnings"] == [
        "soft_boundary_needs_review",
        "possible_duplicate",
    ]


def test_stable_candidate_id_helper_returns_same_id_for_same_input():
    first = contracts.make_candidate_id(
        job_id="job_123",
        source_section_id="section_001",
        start_sec=10.0,
        end_sec=40.0,
    )
    second = contracts.make_candidate_id(
        job_id="job_123",
        source_section_id="section_001",
        start_sec=10.0,
        end_sec=40.0,
    )
    changed = contracts.make_candidate_id(
        job_id="job_123",
        source_section_id="section_001",
        start_sec=10.0,
        end_sec=41.0,
    )

    assert first == second
    assert first.startswith("cand_")
    assert first != changed


def test_processing_report_valid_object_validates():
    contracts.validate_processing_report(_valid_report())


def test_negative_report_count_fails():
    report = _valid_report()
    report["sections_analysed"] = -1

    with pytest.raises(contracts.ProcessingContractValidationError) as exc:
        contracts.validate_processing_report(report)

    assert "sections_analysed must be a non-negative integer" in str(exc.value)


def test_writing_helpers_write_json_that_validates_again(tmp_path: Path):
    pool = _valid_pool(candidates=[_valid_candidate()])
    report = _valid_report()

    pool_path = contracts.write_raw_candidate_pool(str(tmp_path), copy.deepcopy(pool))
    report_path = contracts.write_processing_report(str(tmp_path), copy.deepcopy(report))

    assert Path(pool_path).name == contracts.RAW_CANDIDATE_POOL_FILENAME
    assert Path(report_path).name == contracts.PROCESSING_REPORT_FILENAME

    reloaded_pool = json.loads(Path(pool_path).read_text(encoding="utf-8"))
    reloaded_report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    contracts.validate_raw_candidate_pool(reloaded_pool)
    contracts.validate_processing_report(reloaded_report)
    assert reloaded_pool["candidates"][0]["warnings"] == ["soft_boundary_needs_review"]
