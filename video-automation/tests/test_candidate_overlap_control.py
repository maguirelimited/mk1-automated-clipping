from __future__ import annotations

import os
import sys

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from candidate_overlap_control import (  # noqa: E402
    CandidateOverlapControlError,
    are_timestamp_duplicates,
    control_candidate_overlap,
    shorter_overlap_ratio,
    union_iou,
)


def _scores(**overrides) -> dict:
    scores = {
        "hook_strength": 8,
        "standalone_context": 7,
        "insight_value": 8,
        "retention_potential": 7,
        "natural_ending": 7,
        "overall_potential": 8,
    }
    scores.update(overrides)
    return scores


def _candidate(candidate_id: str, start: float, end: float, **overrides) -> dict:
    candidate = {
        "candidate_local_id": candidate_id,
        "source_section_id": "section_0001",
        "start_sec": start,
        "end_sec": end,
        "duration_sec": end - start,
        "hook_text": "The surprising thing about this business is simple.",
        "core_idea_summary": "The speaker explains a standalone business lesson.",
        "why_candidate_has_potential": "It is understandable without broader podcast context.",
        "archetype": "business_lesson",
        "scores": _scores(),
        "confidence": 0.72,
        "warnings": [],
        "transcript_quality_flags": [],
    }
    candidate.update(overrides)
    return candidate


def test_non_overlapping_candidates_are_all_preserved():
    candidates = [_candidate("a", 100, 140), _candidate("b", 200, 240)]

    result = control_candidate_overlap(candidates)

    assert [c["candidate_local_id"] for c in result.kept_candidates] == ["a", "b"]
    assert result.duplicate_removals == ()


def test_lightly_overlapping_candidates_below_threshold_are_preserved():
    a = _candidate("a", 100, 160)
    b = _candidate("b", 150, 210)

    result = control_candidate_overlap([a, b])

    assert are_timestamp_duplicates(a, b) is False
    assert len(result.kept_candidates) == 2


def test_heavily_overlapping_candidates_are_deduped():
    result = control_candidate_overlap([
        _candidate("a", 100, 160),
        _candidate("b", 110, 165),
    ])

    assert len(result.kept_candidates) == 1
    assert len(result.duplicate_removals) == 1


def test_near_identical_ranges_are_deduped():
    result = control_candidate_overlap([
        _candidate("a", 120, 180),
        _candidate("b", 122, 181),
    ])

    assert len(result.kept_candidates) == 1
    assert result.duplicate_removals[0]["reason"] == "timestamp_duplicate"


def test_exact_same_timestamp_ranges_are_deduped():
    result = control_candidate_overlap([
        _candidate("a", 120, 180),
        _candidate("b", 120, 180),
    ])

    assert len(result.kept_candidates) == 1
    assert len(result.duplicate_removals) == 1


def test_duplicate_with_higher_overall_potential_is_kept():
    result = control_candidate_overlap([
        _candidate("low", 120, 180, scores=_scores(overall_potential=6)),
        _candidate("high", 122, 181, scores=_scores(overall_potential=9)),
    ])

    assert result.kept_candidates[0]["candidate_local_id"] == "high"
    assert result.duplicate_removals[0]["selection_reason"] == "higher_overall_potential"


def test_duplicate_with_equal_overall_but_higher_confidence_is_kept():
    result = control_candidate_overlap([
        _candidate("low", 120, 180, confidence=0.4),
        _candidate("high", 122, 181, confidence=0.9),
    ])

    assert result.kept_candidates[0]["candidate_local_id"] == "high"
    assert result.duplicate_removals[0]["selection_reason"] == "higher_confidence"


def test_duplicate_with_fewer_transcript_quality_flags_is_kept():
    result = control_candidate_overlap([
        _candidate("noisy", 120, 180, transcript_quality_flags=["poor_punctuation"]),
        _candidate("clean", 122, 181, transcript_quality_flags=[]),
    ])

    assert result.kept_candidates[0]["candidate_local_id"] == "clean"
    assert result.duplicate_removals[0]["selection_reason"] == "fewer_transcript_quality_flags"


def test_duplicate_with_fewer_boundary_warnings_is_kept():
    result = control_candidate_overlap([
        _candidate("warned", 120, 180, warnings=["boundary_low_context_before"]),
        _candidate("clean", 122, 181, warnings=[]),
    ])

    assert result.kept_candidates[0]["candidate_local_id"] == "clean"
    assert result.duplicate_removals[0]["selection_reason"] == "fewer_boundary_warnings"


def test_duplicate_with_better_natural_ending_is_kept():
    result = control_candidate_overlap([
        _candidate("weak", 120, 180, scores=_scores(natural_ending=4)),
        _candidate("strong", 122, 181, scores=_scores(natural_ending=9)),
    ])

    assert result.kept_candidates[0]["candidate_local_id"] == "strong"
    assert result.duplicate_removals[0]["selection_reason"] == "higher_natural_ending"


def test_duplicate_with_better_standalone_context_is_kept():
    result = control_candidate_overlap([
        _candidate("weak", 120, 180, scores=_scores(standalone_context=4, natural_ending=7)),
        _candidate("strong", 122, 181, scores=_scores(standalone_context=9, natural_ending=7)),
    ])

    assert result.kept_candidates[0]["candidate_local_id"] == "strong"
    assert result.duplicate_removals[0]["selection_reason"] == "higher_standalone_context"


def test_deterministic_tie_breaker_is_stable():
    result = control_candidate_overlap([
        _candidate("b", 120, 180),
        _candidate("a", 120, 180),
    ])

    assert result.kept_candidates[0]["candidate_local_id"] == "a"
    assert result.duplicate_removals[0]["selection_reason"] == "deterministic_tie_break"


def test_duplicate_removal_metadata_records_ids_and_overlap_ratio():
    result = control_candidate_overlap([
        _candidate("a", 120, 180, source_section_id="section_0001"),
        _candidate("b", 122, 181, source_section_id="section_0002"),
    ])
    removal = result.duplicate_removals[0]

    assert removal["removed_candidate_id"]
    assert removal["kept_candidate_id"]
    assert removal["removed_source_section_id"]
    assert removal["kept_source_section_id"]
    assert removal["overlap_ratio"] == pytest.approx(round(shorter_overlap_ratio(_candidate("a", 120, 180), _candidate("b", 122, 181)), 6))
    assert union_iou(_candidate("a", 120, 180), _candidate("b", 122, 181)) > 0.65


def test_duplicate_removal_count_is_correct():
    result = control_candidate_overlap([
        _candidate("a", 120, 180),
        _candidate("b", 122, 181),
        _candidate("c", 300, 340),
    ])

    assert len(result.duplicate_removals) == 1
    assert len(result.kept_candidates) == 2


def test_similar_ideas_at_different_timestamps_are_preserved():
    a = _candidate("a", 120, 165, core_idea_summary="Same summary", archetype="story")
    b = _candidate("b", 420, 465, core_idea_summary="Same summary", archetype="story")

    result = control_candidate_overlap([a, b])

    assert len(result.kept_candidates) == 2


def test_same_archetype_alone_does_not_dedupe():
    result = control_candidate_overlap([
        _candidate("a", 100, 140, archetype="business_lesson"),
        _candidate("b", 145, 185, archetype="business_lesson"),
    ])

    assert len(result.kept_candidates) == 2


def test_same_summary_alone_does_not_dedupe():
    result = control_candidate_overlap([
        _candidate("a", 100, 140, core_idea_summary="Same summary"),
        _candidate("b", 145, 185, core_idea_summary="Same summary"),
    ])

    assert len(result.kept_candidates) == 2


def test_candidates_from_overlapping_sections_dedupe_by_timestamp():
    result = control_candidate_overlap([
        _candidate("a", 100, 160, source_section_id="section_0001"),
        _candidate("b", 102, 161, source_section_id="section_0002"),
    ])

    assert len(result.kept_candidates) == 1
    assert len(result.duplicate_removals) == 1


def test_candidates_from_same_section_with_distinct_timestamps_are_preserved():
    result = control_candidate_overlap([
        _candidate("a", 100, 140, source_section_id="section_0001"),
        _candidate("b", 150, 190, source_section_id="section_0001"),
    ])

    assert len(result.kept_candidates) == 2


def test_malformed_candidate_input_fails_cleanly():
    with pytest.raises(CandidateOverlapControlError):
        control_candidate_overlap([_candidate("a", 100, 140), {"start_sec": "bad"}])
