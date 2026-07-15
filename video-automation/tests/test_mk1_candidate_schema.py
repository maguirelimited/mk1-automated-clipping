"""MK1 canonical candidate schema contract tests (Prompt 5)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import processing_contracts as contracts  # noqa: E402
from render_clip_v1 import RenderClipV1Module  # noqa: E402
from selection_gate_v1 import run_selection_gate_v1  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DOC = REPO_ROOT / "video-automation" / "context" / "mk1_candidate_schema.md"


def _valid_candidate(**overrides) -> dict:
    candidate = {
        "candidate_id": contracts.make_candidate_id(
            job_id="job_schema",
            source_section_id="section_0001",
            start_sec=10.0,
            end_sec=40.0,
        ),
        "source_section_id": "section_0001",
        "start_sec": 10.0,
        "end_sec": 40.0,
        "duration_sec": 30.0,
        "hook_text": "Opening hook.",
        "core_idea_summary": "Core idea.",
        "why_candidate_has_potential": "Strong standalone moment.",
        "archetype": "valuable_insight",
        "confidence": 0.8,
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
    candidate.update(overrides)
    return candidate


def _valid_pool(*, candidates: list[dict] | None = None) -> dict:
    return {
        "schema_version": contracts.RAW_CANDIDATE_POOL_SCHEMA_VERSION,
        "job_id": "job_schema",
        "source_video_path": "/fixture/source.mp4",
        "transcript_path": "/fixture/transcript.json",
        "processing_version": contracts.PROCESSING_VERSION,
        "funnel_id": "business",
        "created_at": "2026-06-30T12:00:00+00:00",
        "candidates": list(candidates or []),
        "diagnostics": {},
    }


def test_mk1_candidate_schema_doc_exists():
    assert SCHEMA_DOC.is_file()
    text = SCHEMA_DOC.read_text(encoding="utf-8")
    assert "mk1_candidate_v1" in text
    assert "validate_mk1_candidate" in text


def test_canonical_required_fields_match_pool_contract():
    assert contracts.CANONICAL_MK1_CANDIDATE_REQUIRED_FIELDS == contracts.CANDIDATE_REQUIRED_FIELDS
    assert set(contracts.MK1_CANDIDATE_SCORE_FIELDS) == set(contracts.REQUIRED_SCORE_FIELDS)


def test_validate_mk1_candidate_accepts_valid_candidate():
    contracts.validate_mk1_candidate(_valid_candidate())


def test_validate_mk1_candidate_rejects_missing_candidate_id():
    candidate = _valid_candidate()
    del candidate["candidate_id"]
    with pytest.raises(contracts.ProcessingContractValidationError) as exc:
        contracts.validate_mk1_candidate(candidate)
    assert "candidate_id" in str(exc.value)


def test_validate_mk1_candidate_rejects_duration_mismatch():
    candidate = _valid_candidate(duration_sec=29.0)
    with pytest.raises(contracts.ProcessingContractValidationError) as exc:
        contracts.validate_mk1_candidate(candidate)
    assert "duration_sec must match" in str(exc.value)


def test_canonical_schema_defines_no_composite_score_field():
    assert "composite_score" not in contracts.CANONICAL_MK1_CANDIDATE_REQUIRED_FIELDS
    assert "composite_score" not in contracts.MK1_CANDIDATE_SCORE_FIELDS


def test_selection_gate_accepts_canonical_pool_candidates():
    pool = _valid_pool(candidates=[_valid_candidate()])
    contracts.validate_raw_candidate_pool(pool)
    result = run_selection_gate_v1(pool, job_metadata={"job_id": "job_schema"})
    assert result["status"] == "SELECTION_COMPLETE"
    assert len(result["selected_candidates"]) == 1
    selected = result["selected_candidates"][0]
    assert selected["source_candidate"] == pool["candidates"][0]


def test_render_clip_v1_receives_required_fields_from_selected_entry(tmp_path):
    candidate = _valid_candidate()
    pool = _valid_pool(candidates=[candidate])
    gate = run_selection_gate_v1(pool, job_metadata={"job_id": "job_schema"})
    selected = gate["selected_candidates"][0]

    for field in contracts.MK1_CANDIDATE_RENDER_REQUIRED_FIELDS:
        assert selected.get(field) is not None

    source = tmp_path / "source.mp4"
    source.write_bytes(b"\x00" * 64)
    module = RenderClipV1Module()
    result = module.run(
        {
            "selected_candidate": selected,
            "source_video_path": str(source),
        },
        input_path=str(source),
    )
    assert result["metadata"]["failure_code"] != "missing_candidate_id"
    assert result["metadata"]["failure_code"] != "missing_selected_candidate"


def test_pool_assembly_candidate_has_required_fields_after_integration():
    from processing_integration import build_pool_candidate_from_discovery  # noqa: E402

    discovery = {
        "candidate_local_id": "section_0001_candidate_0001",
        "source_section_id": "section_0001",
        "start_sec": 12.0,
        "end_sec": 52.0,
        "duration_sec": 40.0,
        "hook_text": "Hook",
        "core_idea_summary": "Summary",
        "why_candidate_has_potential": "Potential",
        "archetype": "story",
        "confidence": 0.75,
        "scores": _valid_candidate()["scores"],
        "warnings": [],
        "transcript_quality_flags": [],
    }
    pool_candidate = build_pool_candidate_from_discovery(discovery, "job_schema")
    contracts.validate_mk1_candidate(pool_candidate)
    assert pool_candidate["candidate_id"].startswith("cand_")
