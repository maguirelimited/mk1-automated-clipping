"""MK1 Evaluation stage formalisation tests for selection_gate_v1."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import processing_contracts as contracts  # noqa: E402
import selection_gate_v1 as evaluation  # noqa: E402


def _scores(**overrides) -> dict:
    base = {
        "hook_strength": 8,
        "standalone_context": 7,
        "insight_value": 8,
        "retention_potential": 7,
        "natural_ending": 7,
        "overall_potential": 8,
    }
    base.update(overrides)
    return base


def _candidate(
    index: int = 1,
    *,
    job_id: str = "job_eval",
    section_id: str = "section_0001",
    **overrides,
) -> dict:
    start = 100.0 + index
    end = 140.0 + index
    candidate = {
        "candidate_id": contracts.make_candidate_id(
            job_id=job_id,
            source_section_id=section_id,
            start_sec=start,
            end_sec=end,
            candidate_index=index,
        ),
        "source_section_id": section_id,
        "start_sec": start,
        "end_sec": end,
        "duration_sec": end - start,
        "hook_text": f"Hook {index}",
        "core_idea_summary": f"Summary {index}",
        "why_candidate_has_potential": f"Potential {index}",
        "archetype": "business_lesson",
        "confidence": 0.8,
        "scores": _scores(overall_potential=8 - (index % 2)),
        "warnings": [],
        "transcript_quality_flags": [],
    }
    candidate.update(overrides)
    return candidate


def _pool(*candidates: dict) -> dict:
    return contracts.build_raw_candidate_pool(
        job_id="job_eval",
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        funnel_id="business",
        candidates=list(candidates),
        created_at="2026-06-30T12:00:00+00:00",
    )


def test_selection_gate_is_mk1_evaluation_baseline():
    source = Path(SCRIPTS_DIR, "selection_gate_v1.py").read_text(encoding="utf-8")
    assert "MK1 Evaluation stage" in source
    assert evaluation.MK1_EVALUATION_STRATEGY == "mk1_selection_gate_evaluation_v1"
    assert evaluation.run_mk1_evaluation is evaluation.run_selection_gate_v1


def test_evaluation_result_includes_strategy_metadata():
    result = evaluation.run_selection_gate_v1(_pool(_candidate(1), _candidate(2)))

    meta = result["evaluation"]
    assert meta["strategy"] == evaluation.MK1_EVALUATION_STRATEGY
    assert meta["mode"] == evaluation.DEFAULT_SELECTION_MODE
    assert meta["input_candidate_count"] == 2
    assert meta["selected_count"] == len(result["selected_candidates"])
    assert meta["reserve_count"] == len(result["reserve_candidates"])
    assert meta["rejected_count"] == len(result["rejected_candidates"])


def test_evaluation_accepts_mk1_candidate_v1_pool():
    pool = _pool(_candidate(1))
    contracts.validate_raw_candidate_pool(pool)
    result = evaluation.run_selection_gate_v1(pool)
    assert result["status"] == evaluation.STATUS_SELECTION_COMPLETE


def test_selected_entries_preserve_render_required_fields():
    result = evaluation.run_selection_gate_v1(_pool(_candidate(1)))
    selected = result["selected_candidates"][0]

    for field in ("candidate_id", "start_sec", "end_sec", "rank", "source_candidate"):
        assert field in selected
    assert selected["end_sec"] > selected["start_sec"]
    assert selected["source_candidate"]["candidate_id"] == selected["candidate_id"]


def test_evaluation_does_not_call_ai(monkeypatch: pytest.MonkeyPatch):
    original_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name in {"ai_service_client", "openai"}:
            raise AssertionError("AI service should not be imported during Evaluation")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    evaluation.run_selection_gate_v1(_pool(_candidate(1)))


def test_evaluation_does_not_import_candidate_processing():
    source = Path(SCRIPTS_DIR, "selection_gate_v1.py").read_text(encoding="utf-8")
    assert "candidate_processing" not in source
    assert "candidate_boundary_sanity" not in source
    assert "candidate_overlap_control" not in source


def test_evaluation_does_not_add_composite_weighted_score():
    result = evaluation.run_selection_gate_v1(_pool(_candidate(1)))
    payload = str(result)
    assert "composite_score" not in payload
    assert "weighted" not in payload.lower()
    selected = result["selected_candidates"][0]
    assert set(selected["scores"]) == set(contracts.REQUIRED_SCORE_FIELDS)


def test_evaluation_is_deterministic_for_same_pool():
    pool = _pool(_candidate(1), _candidate(2), _candidate(3))
    first = evaluation.run_selection_gate_v1(pool)
    second = evaluation.run_selection_gate_v1(pool)

    first_ids = [entry["candidate_id"] for entry in first["selected_candidates"]]
    second_ids = [entry["candidate_id"] for entry in second["selected_candidates"]]
    assert first_ids == second_ids


def test_failed_evaluation_includes_evaluation_metadata():
    result = evaluation.run_selection_gate_v1({"candidates": "not-a-list"})
    assert result["status"] == evaluation.STATUS_SELECTION_FAILED
    assert result["evaluation"]["strategy"] == evaluation.MK1_EVALUATION_STRATEGY


def test_mk1_has_no_ai_evaluation_prompt_module():
    ai_service = Path(SCRIPTS_DIR).parents[1] / "ai-service" / "prompts"
    assert not list(ai_service.glob("*evaluation*"))


def test_evaluation_module_docstring_states_no_ai():
    source = Path(SCRIPTS_DIR, "selection_gate_v1.py").read_text(encoding="utf-8")
    assert "does not call AI" in source or "call AI/LLM" in source
    assert "MK1 Evaluation" in source
