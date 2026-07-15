from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import candidate_processing as cp  # noqa: E402
import processing_contracts as contracts  # noqa: E402
import processing_integration as integration  # noqa: E402
import section_candidate_discovery as discovery  # noqa: E402
import selection_gate_v1 as selection_gate  # noqa: E402


def _section(section_id: str = "section_0001") -> dict:
    return {
        "section_id": section_id,
        "start_sec": 100.0,
        "end_sec": 260.0,
        "duration_sec": 160.0,
        "text": "[100.000 -> 160.000] A useful business lesson.",
        "source_transcript_path": "/tmp/transcript.json",
        "source_segment_refs": [
            {"segment_index": 1, "start_sec": 100.0, "end_sec": 160.0}
        ],
        "overlap": {
            "has_previous_overlap": False,
            "has_next_overlap": False,
            "overlap_before_sec": 0.0,
            "overlap_after_sec": 0.0,
        },
        "metadata": {},
    }


def _candidate(index: int = 1, *, section_id: str = "section_0001", **overrides) -> dict:
    candidate = {
        "candidate_local_id": f"{section_id}_candidate_{index:04d}",
        "source_section_id": section_id,
        "start_sec": 120.0 + index,
        "end_sec": 160.0 + index,
        "duration_sec": 40.0,
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
        "transcript_quality_flags": [],
    }
    candidate.update(overrides)
    return candidate


def _discovery_batch(*, candidates: list[dict] | None = None) -> dict:
    return {
        "schema_version": discovery.SECTION_DISCOVERY_BATCH_SCHEMA_VERSION,
        "sections_received": 1,
        "sections_processed": 1,
        "usable_sections": 1,
        "rejected_sections": 0,
        "candidates_discovered": len(candidates or [_candidate()]),
        "duplicates_removed": 0,
        "section_results": [
            {
                "schema_version": discovery.SECTION_DISCOVERY_SCHEMA_VERSION,
                "section_id": "section_0001",
                "usable": True,
                "confidence": 0.74,
                "reason": "Standalone lesson.",
                "warnings": [],
                "transcript_quality_flags": [],
                "candidates": list(candidates if candidates is not None else [_candidate()]),
            }
        ],
        "rejected_candidates": [],
        "duplicate_removals": [],
        "warnings": [],
        "failed_sections": [],
    }


def _config(**overrides) -> discovery.CandidateDiscoveryConfig:
    base = {
        "fail_fast": False,
        "max_candidates_per_section": 3,
        "min_candidate_duration_sec": 15.0,
        "max_candidate_duration_sec": 120.0,
    }
    base.update(overrides)
    return discovery.CandidateDiscoveryConfig(**base)


def test_run_candidate_processing_is_clear_entry_point():
    processed = cp.run_candidate_processing(
        _discovery_batch(),
        [_section()],
        config=_config(),
    )
    assert processed["section_results"][0]["candidates"]
    assert processed["_candidate_processing_summary"]["strategy"] == cp.MK1_CANDIDATE_PROCESSING_STRATEGY


def test_boundary_sanity_rejects_out_of_bounds_candidate():
    bad = _candidate(start_sec=90.0, end_sec=130.0, duration_sec=40.0)
    processed = cp.run_candidate_processing(
        _discovery_batch(candidates=[bad]),
        [_section()],
        config=_config(),
    )

    assert processed["section_results"][0]["candidates"] == []
    assert len(processed["rejected_candidates"]) == 1
    assert "outside_section_bounds" in processed["rejected_candidates"][0]["rejection_reasons"]


def test_overlap_control_dedupes_timestamp_duplicates_across_sections():
    duplicate_a = _candidate(1, section_id="section_0001", start_sec=120.0, end_sec=160.0, duration_sec=40.0)
    duplicate_b = _candidate(1, section_id="section_0002", start_sec=121.0, end_sec=161.0, duration_sec=40.0)
    batch = {
        **_discovery_batch(candidates=[duplicate_a]),
        "sections_received": 2,
        "sections_processed": 2,
        "section_results": [
            _discovery_batch(candidates=[duplicate_a])["section_results"][0],
            {
                **_discovery_batch(candidates=[duplicate_b])["section_results"][0],
                "section_id": "section_0002",
            },
        ],
    }
    processed = cp.run_candidate_processing(
        batch,
        [_section("section_0001"), _section("section_0002")],
        config=_config(),
    )

    assert processed["duplicates_removed"] == 1
    assert len(processed["duplicate_removals"]) == 1
    assert processed["candidates_discovered"] == 1


def test_candidate_processing_does_not_call_ai(monkeypatch: pytest.MonkeyPatch):
    original_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name in {"ai_service_client", "openai"}:
            raise AssertionError("AI service should not be imported during candidate processing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    cp.run_candidate_processing(_discovery_batch(), [_section()], config=_config())


def test_candidate_processing_does_not_import_selection_gate():
    source = Path(SCRIPTS_DIR, "candidate_processing.py").read_text(encoding="utf-8")
    assert "selection_gate" not in source


def test_candidate_processing_artifact_write_read(tmp_path: Path):
    processed = cp.run_candidate_processing(_discovery_batch(), [_section()], config=_config())
    artifact = cp.build_candidate_processing_artifact(
        job_id="job_123",
        source_section_candidate_discovery_path="/tmp/section_candidate_discovery.json",
        processed_batch=processed,
        created_at="2026-06-30T12:00:00+00:00",
    )
    path = cp.write_candidate_processing(str(tmp_path), artifact)
    reloaded = cp.read_candidate_processing(path)

    assert Path(path).name == cp.CANDIDATE_PROCESSING_FILENAME
    assert reloaded["processing"]["strategy"] == cp.MK1_CANDIDATE_PROCESSING_STRATEGY
    assert reloaded["processing"]["output_candidate_count"] >= 1


def test_processed_batch_builds_valid_mk1_pool_and_selection_accepts():
    processed = cp.run_candidate_processing(_discovery_batch(), [_section()], config=_config())
    pool_candidates = integration.collect_candidates_from_batch(processed, "job_123")
    pool = contracts.build_raw_candidate_pool(
        job_id="job_123",
        source_video_path="/tmp/source.mp4",
        transcript_path="/tmp/transcript.json",
        funnel_id="business",
        candidates=pool_candidates,
        created_at="2026-06-30T12:00:00+00:00",
    )
    contracts.validate_raw_candidate_pool(pool)
    gate_result = selection_gate.run_selection_gate_v1(pool)
    assert gate_result["selected_candidates"] or gate_result["reserve_candidates"]
