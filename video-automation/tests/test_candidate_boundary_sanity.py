from __future__ import annotations

import math
import os
import sys

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from candidate_boundary_sanity import (  # noqa: E402
    BoundarySanityConfig,
    REJECTION_DURATION_TOO_LONG,
    REJECTION_DURATION_TOO_SHORT,
    REJECTION_END_BEFORE_START,
    REJECTION_INVALID_TIMESTAMP,
    REJECTION_NO_TRANSCRIPT_COVERAGE,
    REJECTION_NON_NUMERIC_TIMESTAMP,
    REJECTION_OUTSIDE_SECTION_BOUNDS,
    REJECTION_OUTSIDE_TRANSCRIPT_BOUNDS,
    REJECTION_OUTSIDE_VIDEO_BOUNDS,
    WARNING_END_MAY_BE_MID_IDEA,
    WARNING_LOW_CONTEXT_AFTER,
    WARNING_NEAR_SECTION_EDGE,
    WARNING_START_MAY_BE_MID_SENTENCE,
    apply_boundary_sanity,
)


def _section() -> dict:
    return {
        "section_id": "section_0001",
        "start_sec": 100.0,
        "end_sec": 260.0,
        "duration_sec": 160.0,
        "text": "\n".join(
            [
                "[100.000 -> 120.000] This is clean context.",
                "[120.000 -> 170.000] and this candidate starts in the middle of a sentence",
                "[170.000 -> 220.000] This is follow-up context.",
            ]
        ),
        "source_segment_refs": [
            {"segment_index": 1, "start_sec": 100.0, "end_sec": 120.0},
            {"segment_index": 2, "start_sec": 120.0, "end_sec": 170.0},
            {"segment_index": 3, "start_sec": 170.0, "end_sec": 220.0},
        ],
        "overlap": {
            "has_previous_overlap": False,
            "has_next_overlap": False,
            "overlap_before_sec": 0.0,
            "overlap_after_sec": 0.0,
        },
        "metadata": {"transcript_start_sec": 0.0, "transcript_end_sec": 300.0},
    }


def _candidate(**overrides) -> dict:
    candidate = {
        "candidate_local_id": "section_0001_candidate_0001",
        "source_section_id": "section_0001",
        "start_sec": 120.0,
        "end_sec": 170.0,
        "duration_sec": 50.0,
        "hook_text": "The surprising thing about this business is simple.",
        "core_idea_summary": "The speaker explains a standalone business lesson.",
        "why_candidate_has_potential": "It is understandable without broader podcast context.",
        "archetype": "business_lesson",
        "scores": {
            "hook_strength": 8,
            "standalone_context": 7,
            "insight_value": 8,
            "retention_potential": 7,
            "natural_ending": 7,
            "overall_potential": 8,
        },
        "confidence": 0.72,
        "warnings": [],
        "transcript_quality_flags": ["poor_punctuation"],
    }
    candidate.update(overrides)
    return candidate


def test_valid_candidate_with_sane_timestamps_passes_boundary_sanity():
    result = apply_boundary_sanity(_candidate(), _section())

    assert result.accepted is True
    assert result.rejection_reasons == ()


def test_duration_sec_is_calculated_from_start_and_end():
    result = apply_boundary_sanity(_candidate(duration_sec=999.0), _section())

    assert result.accepted is True
    assert result.candidate["duration_sec"] == pytest.approx(50.0)


def test_missing_start_sec_rejects_candidate():
    candidate = _candidate()
    del candidate["start_sec"]

    result = apply_boundary_sanity(candidate, _section())

    assert result.accepted is False
    assert REJECTION_INVALID_TIMESTAMP in result.rejection_reasons


def test_missing_end_sec_rejects_candidate():
    candidate = _candidate()
    del candidate["end_sec"]

    result = apply_boundary_sanity(candidate, _section())

    assert result.accepted is False
    assert REJECTION_INVALID_TIMESTAMP in result.rejection_reasons


def test_non_numeric_start_sec_rejects_candidate():
    result = apply_boundary_sanity(_candidate(start_sec="soon"), _section())

    assert result.accepted is False
    assert REJECTION_NON_NUMERIC_TIMESTAMP in result.rejection_reasons


def test_non_numeric_end_sec_rejects_candidate():
    result = apply_boundary_sanity(_candidate(end_sec="later"), _section())

    assert result.accepted is False
    assert REJECTION_NON_NUMERIC_TIMESTAMP in result.rejection_reasons


def test_negative_start_sec_rejects_candidate():
    result = apply_boundary_sanity(_candidate(start_sec=-1.0, end_sec=30.0), _section())

    assert result.accepted is False
    assert REJECTION_INVALID_TIMESTAMP in result.rejection_reasons


def test_end_before_start_rejects_candidate():
    result = apply_boundary_sanity(_candidate(start_sec=130.0, end_sec=130.0), _section())

    assert result.accepted is False
    assert REJECTION_END_BEFORE_START in result.rejection_reasons


def test_nan_timestamp_rejects_candidate():
    result = apply_boundary_sanity(_candidate(start_sec=math.nan), _section())

    assert result.accepted is False
    assert REJECTION_NON_NUMERIC_TIMESTAMP in result.rejection_reasons


def test_infinite_timestamp_rejects_candidate():
    result = apply_boundary_sanity(_candidate(end_sec=math.inf), _section())

    assert result.accepted is False
    assert REJECTION_NON_NUMERIC_TIMESTAMP in result.rejection_reasons


def test_too_short_candidate_rejects_candidate():
    result = apply_boundary_sanity(_candidate(start_sec=120.0, end_sec=130.0), _section())

    assert result.accepted is False
    assert REJECTION_DURATION_TOO_SHORT in result.rejection_reasons


def test_too_long_candidate_rejects_candidate():
    result = apply_boundary_sanity(
        _candidate(start_sec=100.0, end_sec=240.0),
        _section(),
        BoundarySanityConfig(max_candidate_duration_sec=120.0),
    )

    assert result.accepted is False
    assert REJECTION_DURATION_TOO_LONG in result.rejection_reasons


def test_candidate_outside_section_bounds_rejects_candidate():
    result = apply_boundary_sanity(_candidate(start_sec=90.0, end_sec=140.0), _section())

    assert result.accepted is False
    assert REJECTION_OUTSIDE_SECTION_BOUNDS in result.rejection_reasons


def test_candidate_outside_transcript_bounds_rejects_when_bounds_available():
    result = apply_boundary_sanity(
        _candidate(start_sec=120.0, end_sec=170.0),
        _section(),
        BoundarySanityConfig(transcript_start_sec=0.0, transcript_end_sec=150.0),
    )

    assert result.accepted is False
    assert REJECTION_OUTSIDE_TRANSCRIPT_BOUNDS in result.rejection_reasons


def test_candidate_outside_video_bounds_rejects_when_video_duration_available():
    result = apply_boundary_sanity(
        _candidate(start_sec=120.0, end_sec=170.0),
        _section(),
        BoundarySanityConfig(video_duration_sec=150.0),
    )

    assert result.accepted is False
    assert REJECTION_OUTSIDE_VIDEO_BOUNDS in result.rejection_reasons


def test_candidate_with_no_transcript_coverage_rejects_candidate():
    result = apply_boundary_sanity(_candidate(start_sec=230.0, end_sec=250.0), _section())

    assert result.accepted is False
    assert REJECTION_NO_TRANSCRIPT_COVERAGE in result.rejection_reasons


def test_candidate_near_section_edge_gets_warning_not_rejection():
    result = apply_boundary_sanity(_candidate(start_sec=100.0, end_sec=130.0), _section())

    assert result.accepted is True
    assert WARNING_NEAR_SECTION_EDGE in result.warnings


def test_likely_mid_sentence_start_gets_boundary_warning():
    result = apply_boundary_sanity(_candidate(start_sec=125.0, end_sec=170.0), _section())

    assert result.accepted is True
    assert WARNING_START_MAY_BE_MID_SENTENCE in result.warnings


def test_likely_mid_idea_ending_gets_boundary_warning():
    result = apply_boundary_sanity(_candidate(start_sec=120.0, end_sec=150.0), _section())

    assert result.accepted is True
    assert WARNING_END_MAY_BE_MID_IDEA in result.warnings


def test_accepted_candidate_preserves_existing_candidate_fields_and_appends_warnings():
    result = apply_boundary_sanity(
        _candidate(warnings=["existing_warning"], start_sec=125.0, end_sec=170.0),
        _section(),
    )

    assert result.accepted is True
    assert result.candidate["scores"]["overall_potential"] == 8
    assert result.candidate["hook_text"]
    assert result.candidate["archetype"] == "business_lesson"
    assert result.candidate["transcript_quality_flags"] == ["poor_punctuation"]
    assert "existing_warning" in result.candidate["warnings"]
    assert WARNING_START_MAY_BE_MID_SENTENCE in result.candidate["warnings"]


def test_low_context_after_gets_warning_when_no_following_coverage():
    result = apply_boundary_sanity(_candidate(start_sec=170.0, end_sec=220.0), _section())

    assert result.accepted is True
    assert WARNING_LOW_CONTEXT_AFTER in result.warnings
