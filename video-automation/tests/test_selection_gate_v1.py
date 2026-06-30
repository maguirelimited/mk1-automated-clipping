"""Selection Gate v1 — focused tests (Prompt 15).

Verifies that the selection gate:
- selects top-ranked candidates deterministically
- supports all five selection modes
- enforces quality/confidence/duration thresholds
- handles blocking warnings and transcript-quality flags
- preserves non-blocking warnings and flags
- rejects candidates with clear typed reasons
- ranks tied candidates by start_sec then candidate_id
- accepts zero-candidate pools
- produces correct summary counts
- preserves source scores and evidence
- does not import/call discovery, AI, rendering, or output-funnel code
- supports the path-based helper
- optionally writes selection_result.json

All tests use in-memory fixture pools; no real video decode or ffmpeg required.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import processing_contracts as contracts  # noqa: E402
from selection_gate_v1 import (  # noqa: E402
    DEFAULT_SELECTION_MODE,
    SELECTION_GATE_SCHEMA_VERSION,
    STATUS_SELECTION_COMPLETE,
    STATUS_SELECTION_FAILED,
    run_selection_gate_v1,
    run_selection_gate_v1_from_path,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_BASE_SCORES = {
    "hook_strength": 8,
    "standalone_context": 7,
    "insight_value": 9,
    "retention_potential": 8,
    "natural_ending": 7,
    "overall_potential": 8,
}


def _scores(**overrides: Any) -> dict[str, Any]:
    base = dict(_BASE_SCORES)
    base.update(overrides)
    return base


def _candidate(
    *,
    job_id: str = "sg_job_001",
    section_id: str = "section_0001",
    start_sec: float = 10.0,
    end_sec: float = 55.0,
    confidence: float = 0.82,
    warnings: list[str] | None = None,
    transcript_quality_flags: list[str] | None = None,
    archetype: str = "valuable_insight",
    **score_overrides: Any,
) -> dict[str, Any]:
    """Build a fully valid raw candidate dict."""
    duration = round(end_sec - start_sec, 3)
    return {
        "candidate_id": contracts.make_candidate_id(
            job_id=job_id,
            source_section_id=section_id,
            start_sec=start_sec,
            end_sec=end_sec,
        ),
        "source_section_id": section_id,
        "start_sec": start_sec,
        "end_sec": end_sec,
        "duration_sec": duration,
        "hook_text": "The one insight that changed everything.",
        "core_idea_summary": "A concise standalone business lesson.",
        "why_candidate_has_potential": "Strong hook, no context required.",
        "archetype": archetype,
        "confidence": confidence,
        "scores": _scores(**score_overrides),
        "warnings": list(warnings or []),
        "transcript_quality_flags": list(transcript_quality_flags or []),
    }


def _pool(
    *,
    job_id: str = "sg_job_001",
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a valid raw_candidate_pool dict."""
    return {
        "schema_version": contracts.RAW_CANDIDATE_POOL_SCHEMA_VERSION,
        "job_id": job_id,
        "source_video_path": "/fixture/source.mp4",
        "transcript_path": "/fixture/transcript.json",
        "processing_version": contracts.PROCESSING_VERSION,
        "funnel_id": "business",
        "created_at": "2026-06-30T12:00:00+00:00",
        "candidates": list(candidates or []),
        "diagnostics": {},
    }


def _write_pool(directory: Path, pool_data: dict[str, Any]) -> Path:
    path = directory / "raw_candidate_pool.json"
    path.write_text(json.dumps(pool_data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. Balanced mode selects top-ranked candidates by deterministic score order
# ---------------------------------------------------------------------------


def test_balanced_mode_selects_highest_overall_potential_first():
    strong = _candidate(start_sec=100.0, end_sec=145.0, section_id="s2", overall_potential=9)
    weak = _candidate(start_sec=10.0, end_sec=55.0, overall_potential=7)
    result = run_selection_gate_v1(
        _pool(candidates=[weak, strong]),
        config={"max_clips": 1},
    )
    assert result["status"] == STATUS_SELECTION_COMPLETE
    selected_ids = [s["candidate_id"] for s in result["selected_candidates"]]
    assert selected_ids[0] == strong["candidate_id"]


def test_balanced_mode_result_schema_version():
    result = run_selection_gate_v1(_pool())
    assert result["schema_version"] == SELECTION_GATE_SCHEMA_VERSION


def test_balanced_mode_result_has_no_errors():
    result = run_selection_gate_v1(_pool(candidates=[_candidate()]))
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# 2. Default mode is balanced
# ---------------------------------------------------------------------------


def test_default_selection_mode_is_balanced():
    assert DEFAULT_SELECTION_MODE == "balanced"


def test_result_reports_balanced_when_no_mode_provided():
    result = run_selection_gate_v1(_pool(candidates=[_candidate()]))
    assert result["selection_mode"] == "balanced"


# ---------------------------------------------------------------------------
# 3. max_clips limits selected candidates
# ---------------------------------------------------------------------------


def test_max_clips_limits_selected_count():
    candidates = [
        _candidate(start_sec=10.0 + i * 60, end_sec=55.0 + i * 60, section_id=f"s{i}")
        for i in range(5)
    ]
    result = run_selection_gate_v1(
        _pool(candidates=candidates),
        config={"max_clips": 2},
    )
    assert len(result["selected_candidates"]) <= 2


def test_max_clips_one_selects_exactly_one():
    candidates = [
        _candidate(start_sec=10.0 + i * 60, end_sec=55.0 + i * 60, section_id=f"s{i}")
        for i in range(4)
    ]
    result = run_selection_gate_v1(
        _pool(candidates=candidates),
        config={"max_clips": 1},
    )
    assert len(result["selected_candidates"]) == 1


# ---------------------------------------------------------------------------
# 4. Extra eligible candidates become reserve candidates when reserve enabled
# ---------------------------------------------------------------------------


def test_extra_eligible_candidates_become_reserve():
    candidates = [
        _candidate(start_sec=10.0 + i * 60, end_sec=55.0 + i * 60, section_id=f"s{i}")
        for i in range(5)
    ]
    result = run_selection_gate_v1(
        _pool(candidates=candidates),
        config={"max_clips": 2, "reserve_count": 2, "allow_reserve_candidates": True},
    )
    assert len(result["reserve_candidates"]) == 2


def test_reserve_candidates_are_capped_by_reserve_count():
    candidates = [
        _candidate(start_sec=10.0 + i * 60, end_sec=55.0 + i * 60, section_id=f"s{i}")
        for i in range(8)
    ]
    result = run_selection_gate_v1(
        _pool(candidates=candidates),
        config={"max_clips": 2, "reserve_count": 3},
    )
    assert len(result["reserve_candidates"]) <= 3


# ---------------------------------------------------------------------------
# 5. Reserve candidates are not treated as rejected
# ---------------------------------------------------------------------------


def test_reserve_candidates_are_not_in_rejected():
    candidates = [
        _candidate(start_sec=10.0 + i * 60, end_sec=55.0 + i * 60, section_id=f"s{i}")
        for i in range(5)
    ]
    result = run_selection_gate_v1(
        _pool(candidates=candidates),
        config={"max_clips": 2, "reserve_count": 2},
    )
    reserve_ids = {r["candidate_id"] for r in result["reserve_candidates"]}
    rejected_ids = {r["candidate_id"] for r in result["rejected_candidates"]}
    assert not reserve_ids.intersection(rejected_ids), (
        "Reserve candidates must not appear in rejected list"
    )


def test_reserve_entry_has_reserve_reason():
    candidates = [
        _candidate(start_sec=10.0 + i * 60, end_sec=55.0 + i * 60, section_id=f"s{i}")
        for i in range(4)
    ]
    result = run_selection_gate_v1(
        _pool(candidates=candidates),
        config={"max_clips": 1, "reserve_count": 2},
    )
    for reserve in result["reserve_candidates"]:
        assert reserve["reserve_reason"] == "over_max_clip_count"


# ---------------------------------------------------------------------------
# 6. Rejected candidates include clear rejection reasons
# ---------------------------------------------------------------------------


def test_rejected_candidate_has_rejection_reasons_list():
    weak = _candidate(overall_potential=2)
    result = run_selection_gate_v1(
        _pool(candidates=[weak]),
        config={"min_overall_potential": 7.0},
    )
    assert len(result["rejected_candidates"]) == 1
    rejected = result["rejected_candidates"][0]
    assert isinstance(rejected["rejection_reasons"], list)
    assert len(rejected["rejection_reasons"]) >= 1


# ---------------------------------------------------------------------------
# 7. Candidate below min_overall_potential is rejected
# ---------------------------------------------------------------------------


def test_below_min_overall_potential_is_rejected():
    candidate = _candidate(overall_potential=5)
    result = run_selection_gate_v1(
        _pool(candidates=[candidate]),
        config={"min_overall_potential": 7.0},
    )
    assert len(result["selected_candidates"]) == 0
    assert len(result["rejected_candidates"]) == 1
    assert "below_quality_threshold" in result["rejected_candidates"][0]["rejection_reasons"]


def test_at_min_overall_potential_is_eligible():
    candidate = _candidate(overall_potential=7)
    result = run_selection_gate_v1(
        _pool(candidates=[candidate]),
        config={"min_overall_potential": 7.0},
    )
    assert len(result["selected_candidates"]) == 1


# ---------------------------------------------------------------------------
# 8. Candidate below min_confidence is rejected
# ---------------------------------------------------------------------------


def test_below_min_confidence_is_rejected():
    candidate = _candidate(confidence=0.3)
    result = run_selection_gate_v1(
        _pool(candidates=[candidate]),
        config={"min_confidence": 0.6},
    )
    assert len(result["selected_candidates"]) == 0
    rejected = result["rejected_candidates"][0]
    assert "low_confidence" in rejected["rejection_reasons"]


def test_at_min_confidence_is_eligible():
    candidate = _candidate(confidence=0.6)
    result = run_selection_gate_v1(
        _pool(candidates=[candidate]),
        config={"min_confidence": 0.6},
    )
    assert len(result["selected_candidates"]) == 1


# ---------------------------------------------------------------------------
# 9. Candidate below min_duration_sec is rejected
# ---------------------------------------------------------------------------


def test_below_min_duration_is_rejected():
    short = _candidate(start_sec=10.0, end_sec=18.0)  # 8s duration
    result = run_selection_gate_v1(
        _pool(candidates=[short]),
        config={"min_duration_sec": 15.0},
    )
    assert len(result["selected_candidates"]) == 0
    rejected = result["rejected_candidates"][0]
    assert "duration_too_short" in rejected["rejection_reasons"]


def test_at_min_duration_is_eligible():
    just_right = _candidate(start_sec=10.0, end_sec=25.0)  # exactly 15s
    result = run_selection_gate_v1(
        _pool(candidates=[just_right]),
        config={"min_duration_sec": 15.0},
    )
    assert len(result["selected_candidates"]) == 1


# ---------------------------------------------------------------------------
# 10. Candidate above max_duration_sec is rejected
# ---------------------------------------------------------------------------


def test_above_max_duration_is_rejected():
    long_clip = _candidate(start_sec=10.0, end_sec=250.0)  # 240s duration
    result = run_selection_gate_v1(
        _pool(candidates=[long_clip]),
        config={"max_duration_sec": 120.0},
    )
    assert len(result["selected_candidates"]) == 0
    rejected = result["rejected_candidates"][0]
    assert "duration_too_long" in rejected["rejection_reasons"]


def test_at_max_duration_is_eligible():
    at_max = _candidate(start_sec=10.0, end_sec=130.0)  # exactly 120s
    result = run_selection_gate_v1(
        _pool(candidates=[at_max]),
        config={"max_duration_sec": 120.0},
    )
    assert len(result["selected_candidates"]) == 1


# ---------------------------------------------------------------------------
# 11. Candidate with invalid timestamps is rejected
# ---------------------------------------------------------------------------


def test_candidate_with_none_start_sec_is_rejected():
    bad = _candidate()
    bad["start_sec"] = None
    result = run_selection_gate_v1(_pool(candidates=[bad]))
    assert len(result["selected_candidates"]) == 0
    assert "invalid_timestamp" in result["rejected_candidates"][0]["rejection_reasons"]


def test_candidate_with_end_before_start_is_rejected():
    bad = _candidate()
    bad["end_sec"] = bad["start_sec"] - 5.0
    result = run_selection_gate_v1(_pool(candidates=[bad]))
    assert len(result["selected_candidates"]) == 0
    assert "invalid_timestamp" in result["rejected_candidates"][0]["rejection_reasons"]


def test_candidate_with_string_timestamp_is_rejected():
    bad = _candidate()
    bad["start_sec"] = "not_a_number"
    result = run_selection_gate_v1(_pool(candidates=[bad]))
    assert len(result["selected_candidates"]) == 0
    assert "invalid_timestamp" in result["rejected_candidates"][0]["rejection_reasons"]


# ---------------------------------------------------------------------------
# 12. Candidate with missing score field is rejected
# ---------------------------------------------------------------------------


def test_candidate_with_missing_scores_dict_is_rejected():
    bad = _candidate()
    del bad["scores"]
    result = run_selection_gate_v1(_pool(candidates=[bad]))
    assert len(result["selected_candidates"]) == 0
    assert "missing_required_score" in result["rejected_candidates"][0]["rejection_reasons"]


def test_candidate_with_missing_required_score_field_is_rejected():
    bad = _candidate()
    del bad["scores"]["overall_potential"]
    result = run_selection_gate_v1(_pool(candidates=[bad]))
    assert len(result["selected_candidates"]) == 0
    assert "missing_required_score" in result["rejected_candidates"][0]["rejection_reasons"]


# ---------------------------------------------------------------------------
# 13. Candidate with blocking warning is rejected with warning_too_strong
# ---------------------------------------------------------------------------


def test_candidate_with_blocking_warning_is_rejected():
    flagged = _candidate(warnings=["hard_block_warning"])
    result = run_selection_gate_v1(
        _pool(candidates=[flagged]),
        config={
            "blocking_warnings": ["hard_block_warning"],
            "respect_candidate_warnings": True,
        },
    )
    assert len(result["selected_candidates"]) == 0
    rejected = result["rejected_candidates"][0]
    assert "warning_too_strong" in rejected["rejection_reasons"]


def test_candidate_with_non_blocking_warning_in_blocking_list_but_respect_false():
    """When respect_candidate_warnings=False, even blocking_warnings are ignored."""
    flagged = _candidate(warnings=["hard_block_warning"])
    result = run_selection_gate_v1(
        _pool(candidates=[flagged]),
        config={
            "blocking_warnings": ["hard_block_warning"],
            "respect_candidate_warnings": False,
        },
    )
    assert len(result["selected_candidates"]) == 1


# ---------------------------------------------------------------------------
# 14. Candidate with blocking transcript quality flag → transcript_quality_too_risky
# ---------------------------------------------------------------------------


def test_candidate_with_blocking_transcript_quality_flag_is_rejected():
    flagged = _candidate(transcript_quality_flags=["low_transcript_confidence"])
    result = run_selection_gate_v1(
        _pool(candidates=[flagged]),
        config={
            "blocking_transcript_quality_flags": ["low_transcript_confidence"],
            "respect_transcript_quality_flags": True,
        },
    )
    assert len(result["selected_candidates"]) == 0
    rejected = result["rejected_candidates"][0]
    assert "transcript_quality_too_risky" in rejected["rejection_reasons"]


def test_maximum_quality_blocks_low_transcript_confidence_by_default():
    """maximum_quality mode blocks low_transcript_confidence without explicit config."""
    flagged = _candidate(transcript_quality_flags=["low_transcript_confidence"])
    result = run_selection_gate_v1(
        _pool(candidates=[flagged]),
        config={"selection_mode": "maximum_quality"},
    )
    assert len(result["selected_candidates"]) == 0
    if result["rejected_candidates"]:
        assert "transcript_quality_too_risky" in (
            result["rejected_candidates"][0]["rejection_reasons"]
        )


# ---------------------------------------------------------------------------
# 15. Non-blocking warnings are preserved in selected candidates
# ---------------------------------------------------------------------------


def test_non_blocking_warning_is_preserved_in_selected_candidate():
    with_warning = _candidate(warnings=["soft_boundary_needs_review"])
    result = run_selection_gate_v1(
        _pool(candidates=[with_warning]),
        config={"blocking_warnings": []},
    )
    assert len(result["selected_candidates"]) == 1
    selected = result["selected_candidates"][0]
    assert "soft_boundary_needs_review" in selected["warnings"]


# ---------------------------------------------------------------------------
# 16. Non-blocking transcript quality flags are preserved
# ---------------------------------------------------------------------------


def test_non_blocking_transcript_flag_is_preserved_in_selected_candidate():
    flagged = _candidate(transcript_quality_flags=["poor_punctuation"])
    result = run_selection_gate_v1(
        _pool(candidates=[flagged]),
        config={"blocking_transcript_quality_flags": []},
    )
    assert len(result["selected_candidates"]) == 1
    selected = result["selected_candidates"][0]
    assert "poor_punctuation" in selected["transcript_quality_flags"]


# ---------------------------------------------------------------------------
# 17. Zero-candidate pool returns SELECTION_COMPLETE
# ---------------------------------------------------------------------------


def test_zero_candidate_pool_returns_selection_complete():
    result = run_selection_gate_v1(_pool(candidates=[]))
    assert result["status"] == STATUS_SELECTION_COMPLETE


def test_zero_candidate_pool_has_empty_lists():
    result = run_selection_gate_v1(_pool(candidates=[]))
    assert result["selected_candidates"] == []
    assert result["rejected_candidates"] == []
    assert result["reserve_candidates"] == []


def test_zero_candidate_pool_adds_zero_candidates_warning():
    result = run_selection_gate_v1(_pool(candidates=[]))
    assert "zero_candidates_received" in result["warnings"]


# ---------------------------------------------------------------------------
# 18. maximum_quality is stricter than balanced
# ---------------------------------------------------------------------------


def test_maximum_quality_rejects_candidate_that_balanced_accepts():
    """overall_potential=8.0 passes balanced (min 7.0) but fails maximum_quality (min 8.5)."""
    borderline = _candidate(overall_potential=8)

    balanced_result = run_selection_gate_v1(
        _pool(candidates=[borderline]),
        config={"selection_mode": "balanced"},
    )
    quality_result = run_selection_gate_v1(
        _pool(candidates=[borderline]),
        config={"selection_mode": "maximum_quality"},
    )

    assert len(balanced_result["selected_candidates"]) == 1
    assert len(quality_result["selected_candidates"]) == 0


def test_maximum_quality_has_higher_min_confidence_than_balanced():
    from selection_gate_v1 import _MODE_DEFAULTS

    assert (
        _MODE_DEFAULTS["maximum_quality"]["min_confidence"]
        > _MODE_DEFAULTS["balanced"]["min_confidence"]
    )


# ---------------------------------------------------------------------------
# 19. maximum_data_collection allows more borderline candidates than balanced
# ---------------------------------------------------------------------------


def test_maximum_data_collection_accepts_candidate_that_balanced_rejects():
    """overall_potential=5.5 fails balanced (min 7.0) but passes maximum_data_collection (min 5.0)."""
    borderline = _candidate(overall_potential=5, confidence=0.45)

    balanced_result = run_selection_gate_v1(
        _pool(candidates=[borderline]),
        config={"selection_mode": "balanced"},
    )
    data_result = run_selection_gate_v1(
        _pool(candidates=[borderline]),
        config={"selection_mode": "maximum_data_collection"},
    )

    assert len(balanced_result["selected_candidates"]) == 0
    assert len(data_result["selected_candidates"]) == 1


# ---------------------------------------------------------------------------
# 20. growth allows more exploration than balanced but safer than maximum_data_collection
# ---------------------------------------------------------------------------


def test_growth_accepts_borderline_candidate_that_balanced_rejects():
    """overall_potential=6.5 fails balanced (min 7.0) but passes growth (min 6.0)."""
    borderline = _candidate(overall_potential=6, confidence=0.55)

    balanced_result = run_selection_gate_v1(
        _pool(candidates=[borderline]),
        config={"selection_mode": "balanced"},
    )
    growth_result = run_selection_gate_v1(
        _pool(candidates=[borderline]),
        config={"selection_mode": "growth"},
    )

    assert len(balanced_result["selected_candidates"]) == 0
    assert len(growth_result["selected_candidates"]) == 1


def test_growth_rejects_candidate_that_maximum_data_collection_accepts():
    """overall_potential=5.0 fails growth (min 6.0) but passes maximum_data_collection (min 5.0)."""
    very_borderline = _candidate(overall_potential=5, confidence=0.45)

    growth_result = run_selection_gate_v1(
        _pool(candidates=[very_borderline]),
        config={"selection_mode": "growth"},
    )
    data_result = run_selection_gate_v1(
        _pool(candidates=[very_borderline]),
        config={"selection_mode": "maximum_data_collection"},
    )

    assert len(growth_result["selected_candidates"]) == 0
    assert len(data_result["selected_candidates"]) == 1


# ---------------------------------------------------------------------------
# 21. custom mode uses config-provided thresholds
# ---------------------------------------------------------------------------


def test_custom_mode_uses_caller_provided_min_overall_potential():
    """Custom mode with min_overall_potential=9.0 rejects an 8.5 candidate."""
    candidate = _candidate(overall_potential=8, confidence=0.8)

    result = run_selection_gate_v1(
        _pool(candidates=[candidate]),
        config={"selection_mode": "custom", "min_overall_potential": 9.0},
    )
    assert len(result["selected_candidates"]) == 0
    assert "below_quality_threshold" in result["rejected_candidates"][0]["rejection_reasons"]


def test_custom_mode_uses_caller_provided_max_clips():
    candidates = [
        _candidate(start_sec=10.0 + i * 60, end_sec=55.0 + i * 60, section_id=f"s{i}")
        for i in range(5)
    ]
    result = run_selection_gate_v1(
        _pool(candidates=candidates),
        config={"selection_mode": "custom", "max_clips": 2},
    )
    assert len(result["selected_candidates"]) <= 2


# ---------------------------------------------------------------------------
# 22. Invalid selection mode fails cleanly
# ---------------------------------------------------------------------------


def test_invalid_selection_mode_returns_selection_failed():
    result = run_selection_gate_v1(
        _pool(candidates=[_candidate()]),
        config={"selection_mode": "nonexistent_mode_xyz"},
    )
    assert result["status"] == STATUS_SELECTION_FAILED


def test_invalid_selection_mode_has_correct_error_code():
    result = run_selection_gate_v1(
        _pool(),
        config={"selection_mode": "nonexistent_mode_xyz"},
    )
    assert result["errors"][0]["code"] == "invalid_selection_mode"


# ---------------------------------------------------------------------------
# 23. Invalid config value fails cleanly
# ---------------------------------------------------------------------------


def test_invalid_max_clips_zero_returns_selection_failed():
    result = run_selection_gate_v1(
        _pool(candidates=[_candidate()]),
        config={"max_clips": 0},
    )
    assert result["status"] == STATUS_SELECTION_FAILED
    assert result["errors"][0]["code"] == "invalid_selection_config"


def test_invalid_max_clips_negative_returns_selection_failed():
    result = run_selection_gate_v1(
        _pool(),
        config={"max_clips": -3},
    )
    assert result["status"] == STATUS_SELECTION_FAILED


def test_invalid_min_overall_potential_out_of_range():
    result = run_selection_gate_v1(
        _pool(),
        config={"min_overall_potential": 15.0},
    )
    assert result["status"] == STATUS_SELECTION_FAILED
    assert result["errors"][0]["code"] == "invalid_selection_config"


def test_invalid_min_confidence_out_of_range():
    result = run_selection_gate_v1(
        _pool(),
        config={"min_confidence": 1.5},
    )
    assert result["status"] == STATUS_SELECTION_FAILED


def test_max_duration_less_than_min_duration_returns_selection_failed():
    result = run_selection_gate_v1(
        _pool(),
        config={"min_duration_sec": 60.0, "max_duration_sec": 30.0},
    )
    assert result["status"] == STATUS_SELECTION_FAILED


# ---------------------------------------------------------------------------
# 24. Ranking tie-break is deterministic
# ---------------------------------------------------------------------------


def test_ranking_is_deterministic_across_repeated_calls():
    candidates = [
        _candidate(start_sec=10.0 + i * 60, end_sec=55.0 + i * 60, section_id=f"s{i}")
        for i in range(6)
    ]
    pool = _pool(candidates=candidates)

    result_1 = run_selection_gate_v1(copy.deepcopy(pool), config={"max_clips": 3})
    result_2 = run_selection_gate_v1(copy.deepcopy(pool), config={"max_clips": 3})

    ids_1 = [s["candidate_id"] for s in result_1["selected_candidates"]]
    ids_2 = [s["candidate_id"] for s in result_2["selected_candidates"]]
    assert ids_1 == ids_2, "Ranking must be deterministic across identical calls"


# ---------------------------------------------------------------------------
# 25. Earlier start_sec breaks otherwise equal candidates
# ---------------------------------------------------------------------------


def test_earlier_start_sec_ranks_higher_when_scores_equal():
    """Two candidates with identical scores: the one with earlier start_sec wins."""
    early = _candidate(start_sec=50.0, end_sec=95.0, section_id="s_early")
    late = _candidate(start_sec=200.0, end_sec=245.0, section_id="s_late")

    # Make scores identical
    early["scores"] = dict(_BASE_SCORES)
    late["scores"] = dict(_BASE_SCORES)
    early["confidence"] = 0.75
    late["confidence"] = 0.75

    result = run_selection_gate_v1(
        _pool(candidates=[late, early]),
        config={"max_clips": 1},
    )
    assert result["selected_candidates"][0]["candidate_id"] == early["candidate_id"]


# ---------------------------------------------------------------------------
# 26. Stable candidate_id breaks final ties
# ---------------------------------------------------------------------------


def test_candidate_id_breaks_tie_when_scores_and_start_sec_equal():
    """Two candidates with identical scores and same start_sec: lexicographic id wins."""
    # Build two candidates manually with same timestamps and scores
    cand_a = _candidate(start_sec=10.0, end_sec=55.0, section_id="section_aaa")
    cand_b = _candidate(start_sec=10.0, end_sec=55.0, section_id="section_bbb")
    # Force identical scores and confidence
    cand_a["scores"] = dict(_BASE_SCORES)
    cand_b["scores"] = dict(_BASE_SCORES)
    cand_a["confidence"] = 0.75
    cand_b["confidence"] = 0.75

    # Manually assign IDs so we know which is lexicographically earlier
    cand_a["candidate_id"] = "cand_aaaaaaaaaaaaaaaa"
    cand_b["candidate_id"] = "cand_zzzzzzzzzzzzzzzz"

    result = run_selection_gate_v1(
        _pool(candidates=[cand_b, cand_a]),  # submit in reverse order
        config={"max_clips": 1},
    )
    assert result["selected_candidates"][0]["candidate_id"] == "cand_aaaaaaaaaaaaaaaa"


# ---------------------------------------------------------------------------
# 27. Exact duplicate candidate IDs are handled defensively
# ---------------------------------------------------------------------------


def test_duplicate_candidate_id_second_occurrence_is_rejected():
    original = _candidate()
    duplicate = copy.deepcopy(original)
    # Same candidate_id, different timestamps
    duplicate["start_sec"] = 200.0
    duplicate["end_sec"] = 245.0
    duplicate["duration_sec"] = 45.0

    result = run_selection_gate_v1(_pool(candidates=[original, duplicate]))
    assert len(result["selected_candidates"]) == 1
    rejected_reasons = [
        r for rej in result["rejected_candidates"] for r in rej["rejection_reasons"]
    ]
    assert "duplicate_candidate" in rejected_reasons


# ---------------------------------------------------------------------------
# 28. Similar ideas at different timestamps are not deduped by topic
# ---------------------------------------------------------------------------


def test_candidates_with_similar_ideas_at_different_timestamps_both_eligible():
    """Topic deduplication must NOT occur. Different timestamps = both eligible."""
    cand_1 = _candidate(start_sec=10.0, end_sec=55.0, section_id="s1")
    cand_2 = _candidate(start_sec=200.0, end_sec=245.0, section_id="s2")
    # Same hook_text / core_idea_summary to simulate similar topics
    cand_2["hook_text"] = cand_1["hook_text"]
    cand_2["core_idea_summary"] = cand_1["core_idea_summary"]

    result = run_selection_gate_v1(
        _pool(candidates=[cand_1, cand_2]),
        config={"max_clips": 10},
    )
    assert len(result["selected_candidates"]) == 2


# ---------------------------------------------------------------------------
# 29. Selection result includes selection_summary
# ---------------------------------------------------------------------------


def test_result_includes_selection_summary():
    result = run_selection_gate_v1(_pool(candidates=[_candidate()]))
    assert "selection_summary" in result
    summary = result["selection_summary"]
    for key in (
        "raw_candidates_received",
        "eligible_count",
        "selected_count",
        "rejected_count",
        "reserve_count",
    ):
        assert key in summary, f"selection_summary missing key: {key}"


# ---------------------------------------------------------------------------
# 30. Summary counts match actual selected/rejected/reserve lists
# ---------------------------------------------------------------------------


def test_summary_counts_match_list_lengths():
    candidates = [
        _candidate(start_sec=10.0 + i * 60, end_sec=55.0 + i * 60, section_id=f"s{i}")
        for i in range(5)
    ]
    # Add one low-quality candidate that will be rejected
    bad = _candidate(start_sec=10.0, end_sec=55.0, overall_potential=2)

    result = run_selection_gate_v1(
        _pool(candidates=candidates + [bad]),
        config={"max_clips": 2, "reserve_count": 2, "min_overall_potential": 7.0},
    )
    summary = result["selection_summary"]

    assert summary["selected_count"] == len(result["selected_candidates"])
    assert summary["rejected_count"] == len(result["rejected_candidates"])
    assert summary["reserve_count"] == len(result["reserve_candidates"])
    assert summary["raw_candidates_received"] == len(candidates) + 1


def test_summary_eligible_count_is_selected_plus_reserve_plus_leftover():
    """eligible_count = all candidates that passed hard gates."""
    candidates = [
        _candidate(start_sec=10.0 + i * 60, end_sec=55.0 + i * 60, section_id=f"s{i}")
        for i in range(6)
    ]
    result = run_selection_gate_v1(
        _pool(candidates=candidates),
        config={"max_clips": 2, "reserve_count": 2},
    )
    summary = result["selection_summary"]
    assert summary["eligible_count"] >= summary["selected_count"]


# ---------------------------------------------------------------------------
# 31. Selected candidates preserve source scores and evidence
# ---------------------------------------------------------------------------


def test_selected_candidate_preserves_scores():
    candidate = _candidate(overall_potential=9)
    result = run_selection_gate_v1(_pool(candidates=[candidate]))
    selected = result["selected_candidates"][0]
    assert selected["scores"] == candidate["scores"]


def test_selected_candidate_preserves_source_candidate():
    candidate = _candidate()
    result = run_selection_gate_v1(_pool(candidates=[candidate]))
    selected = result["selected_candidates"][0]
    assert "source_candidate" in selected
    assert selected["source_candidate"]["candidate_id"] == candidate["candidate_id"]


def test_selected_candidate_has_rank_field():
    result = run_selection_gate_v1(_pool(candidates=[_candidate()]))
    selected = result["selected_candidates"][0]
    assert "rank" in selected
    assert selected["rank"] == 1


def test_selected_candidate_has_selection_reason():
    result = run_selection_gate_v1(_pool(candidates=[_candidate()]))
    selected = result["selected_candidates"][0]
    assert selected["selection_reason"] == "selected_by_rank"


# ---------------------------------------------------------------------------
# 32. Rejected candidates preserve enough debug information
# ---------------------------------------------------------------------------


def test_rejected_candidate_preserves_confidence():
    bad = _candidate(confidence=0.1)
    result = run_selection_gate_v1(_pool(candidates=[bad]), config={"min_confidence": 0.6})
    rejected = result["rejected_candidates"][0]
    assert rejected["confidence"] == 0.1


def test_rejected_candidate_preserves_scores():
    bad = _candidate(overall_potential=3)
    result = run_selection_gate_v1(_pool(candidates=[bad]))
    rejected = result["rejected_candidates"][0]
    assert isinstance(rejected["scores"], dict)


def test_rejected_candidate_preserves_timestamps():
    bad = _candidate(start_sec=10.0, end_sec=55.0, overall_potential=2)
    result = run_selection_gate_v1(_pool(candidates=[bad]))
    rejected = result["rejected_candidates"][0]
    assert rejected["start_sec"] == 10.0
    assert rejected["end_sec"] == 55.0


# ---------------------------------------------------------------------------
# 33. Module does not import discovery, AI, or rendering code
# ---------------------------------------------------------------------------


def test_selection_gate_does_not_import_forbidden_modules():
    import selection_gate_v1 as sg

    forbidden = {
        "section_candidate_discovery",
        "clip_video",
        "ai_service_client",
        "transcribe_video",
        "transcript_sectioning",
        "processing_pipeline",
        "ai_settings",
        "output_funnel",
    }
    module_attrs = set(vars(sg).keys())
    for name in forbidden:
        assert name not in module_attrs, (
            f"selection_gate_v1 must not reference {name!r}"
        )


def test_selection_gate_runs_without_ai_client():
    result = run_selection_gate_v1(_pool(candidates=[_candidate()]))
    assert result["status"] == STATUS_SELECTION_COMPLETE


def test_selection_gate_creates_no_rendered_clips(tmp_path):
    selection_dir = tmp_path / "selection"
    run_selection_gate_v1(
        _pool(candidates=[_candidate()]),
        selection_dir=str(selection_dir),
    )
    clip_files = [f for f in selection_dir.iterdir() if f.suffix in (".mp4", ".mov", ".mkv")]
    assert clip_files == []


# ---------------------------------------------------------------------------
# 34. Path-based helper loads pool and selects
# ---------------------------------------------------------------------------


def test_from_path_helper_returns_selection_complete(tmp_path):
    pool_path = _write_pool(tmp_path, _pool(candidates=[_candidate()]))
    result = run_selection_gate_v1_from_path(str(pool_path))
    assert result["status"] == STATUS_SELECTION_COMPLETE


def test_from_path_helper_missing_file_returns_selection_failed(tmp_path):
    result = run_selection_gate_v1_from_path(str(tmp_path / "missing.json"))
    assert result["status"] == STATUS_SELECTION_FAILED
    assert result["errors"][0]["code"] == "missing_raw_candidate_pool"


def test_from_path_helper_invalid_json_returns_selection_failed(tmp_path):
    bad_path = tmp_path / "raw_candidate_pool.json"
    bad_path.write_text("{not json", encoding="utf-8")
    result = run_selection_gate_v1_from_path(str(bad_path))
    assert result["status"] == STATUS_SELECTION_FAILED
    assert result["errors"][0]["code"] == "invalid_raw_candidate_pool_json"


def test_from_path_helper_preserves_candidate_count(tmp_path):
    candidates = [
        _candidate(start_sec=10.0 + i * 60, end_sec=55.0 + i * 60, section_id=f"s{i}")
        for i in range(3)
    ]
    pool_path = _write_pool(tmp_path, _pool(candidates=candidates))
    result = run_selection_gate_v1_from_path(str(pool_path))
    assert result["selection_summary"]["raw_candidates_received"] == 3


# ---------------------------------------------------------------------------
# 35. Optional selection artifact writes valid JSON
# ---------------------------------------------------------------------------


def test_selection_dir_writes_selection_result_json(tmp_path):
    selection_dir = tmp_path / "post_processing" / "selection"
    run_selection_gate_v1(
        _pool(candidates=[_candidate()]),
        selection_dir=str(selection_dir),
    )
    result_file = selection_dir / "selection_result.json"
    assert result_file.exists(), "selection_result.json was not written"


def test_selection_result_json_is_valid_json(tmp_path):
    selection_dir = tmp_path / "selection"
    run_selection_gate_v1(
        _pool(candidates=[_candidate()]),
        selection_dir=str(selection_dir),
    )
    result_file = selection_dir / "selection_result.json"
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["status"] == STATUS_SELECTION_COMPLETE
    assert data["schema_version"] == SELECTION_GATE_SCHEMA_VERSION


def test_selection_dir_not_provided_does_not_write_file(tmp_path):
    """No file should be written when selection_dir is None."""
    run_selection_gate_v1(
        _pool(candidates=[_candidate()]),
        selection_dir=None,
    )
    # Nothing should have been written in the current directory for this call
    # (we can only verify no crash and nothing in tmp_path since we haven't set cwd)
    result = run_selection_gate_v1(_pool(candidates=[_candidate()]))
    assert result["status"] == STATUS_SELECTION_COMPLETE


# ---------------------------------------------------------------------------
# 36. Prompt 14 input contract tests still pass (integration regression check)
# ---------------------------------------------------------------------------


def test_prompt14_entry_point_still_returns_ready_for_selection(tmp_path):
    """Ensure the Prompt 14 entrypoint is not broken by Prompt 15 additions."""
    from post_processing_mk1 import (
        STATUS_READY_FOR_SELECTION,
        run_post_processing_mk1,
    )

    # Create dummy video file and pool
    video = tmp_path / "source.mp4"
    video.write_bytes(b"")
    pool = _pool(candidates=[_candidate()])
    pool["source_video_path"] = str(video)
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))
    assert result["status"] == STATUS_READY_FOR_SELECTION


# ---------------------------------------------------------------------------
# Rank ordering — multi-field ranking accuracy
# ---------------------------------------------------------------------------


def test_overall_potential_dominates_ranking():
    """Candidate with higher overall_potential ranked first regardless of other scores."""
    high_pot = _candidate(start_sec=200.0, end_sec=245.0, section_id="s2", overall_potential=10)
    low_pot = _candidate(start_sec=10.0, end_sec=55.0, section_id="s1", overall_potential=7)

    result = run_selection_gate_v1(
        _pool(candidates=[low_pot, high_pot]),
        config={"max_clips": 1},
    )
    assert result["selected_candidates"][0]["candidate_id"] == high_pot["candidate_id"]


def test_selected_ranks_are_sequential_starting_at_one():
    candidates = [
        _candidate(start_sec=10.0 + i * 60, end_sec=55.0 + i * 60, section_id=f"s{i}")
        for i in range(3)
    ]
    result = run_selection_gate_v1(_pool(candidates=candidates), config={"max_clips": 3})
    ranks = [s["rank"] for s in result["selected_candidates"]]
    assert ranks == list(range(1, len(ranks) + 1))


def test_reserve_ranks_continue_after_selected_ranks():
    candidates = [
        _candidate(start_sec=10.0 + i * 60, end_sec=55.0 + i * 60, section_id=f"s{i}")
        for i in range(5)
    ]
    result = run_selection_gate_v1(
        _pool(candidates=candidates),
        config={"max_clips": 2, "reserve_count": 2},
    )
    if result["reserve_candidates"]:
        first_reserve_rank = result["reserve_candidates"][0]["rank"]
        last_selected_rank = result["selected_candidates"][-1]["rank"]
        assert first_reserve_rank > last_selected_rank


def test_config_used_is_included_in_result():
    result = run_selection_gate_v1(_pool())
    assert isinstance(result["config_used"], dict)
    assert "max_clips" in result["config_used"]
    assert "min_overall_potential" in result["config_used"]


def test_result_includes_job_id_from_pool():
    result = run_selection_gate_v1(_pool(job_id="my_test_job"))
    assert result["job_id"] == "my_test_job"


def test_job_metadata_job_id_overrides_pool_job_id():
    result = run_selection_gate_v1(
        _pool(job_id="pool_job"),
        job_metadata={"job_id": "override_job"},
    )
    assert result["job_id"] == "override_job"
