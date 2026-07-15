"""Processing completion gate — Prompt 13.

Proves that the full processing pipeline (transcript sectioning → section
candidate discovery → artifact writing) produces valid, complete output that is
ready for handoff to post-processing.

All tests use deterministic fixture data; no real video or running AI service is
required.  A smoke summary is printed at the end of the test run.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import processing_contracts as contracts  # noqa: E402
from processing_integration import (  # noqa: E402
    collect_candidates_from_batch,
    legacy_segments_from_raw_candidate_pool,
    link_processing_artifacts_in_report,
)
from processing_pipeline import (  # noqa: E402
    ProcessingPipelineError,
    ProcessingPipelineResult,
    run_processing_pipeline,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

SCORES_FIELDS = (
    "hook_strength",
    "standalone_context",
    "insight_value",
    "retention_potential",
    "natural_ending",
    "overall_potential",
)


def _scores(**overrides) -> dict:
    base = {field: 7 for field in SCORES_FIELDS}
    base["overall_potential"] = 8
    base.update(overrides)
    return base


def _transcript(num_segments: int = 20, duration_sec: float = 600.0) -> dict:
    """Realistic 10-minute transcript with evenly-spaced segments."""
    step = duration_sec / num_segments
    segments = []
    for i in range(num_segments):
        start = round(i * step, 3)
        end = round(start + step - 0.5, 3)
        segments.append(
            {
                "start": start,
                "end": end,
                "text": (
                    f"Segment {i + 1}: A standalone business insight at {start:.0f}s. "
                    "The speaker explains the core principle without requiring prior context."
                ),
            }
        )
    return {
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
        "duration": duration_sec,
    }


def _section_result(
    section_id: str,
    *,
    section_start: float,
    section_end: float,
    usable: bool = True,
    num_candidates: int = 1,
    warnings: list | None = None,
    transcript_quality_flags: list | None = None,
    archetype: str = "valuable_insight",
    with_prompt_metadata: bool = True,
) -> dict:
    """Build a realistic section result for a fake discovery client."""
    candidates = []
    if usable:
        for i in range(num_candidates):
            offset = float(i) * 35.0
            start = section_start + 10.0 + offset
            end = start + 45.0
            if end > section_end:
                break
            candidates.append(
                {
                    "candidate_local_id": f"{section_id}_candidate_{i + 1:04d}",
                    "source_section_id": section_id,
                    "start_sec": start,
                    "end_sec": end,
                    "duration_sec": end - start,
                    "hook_text": f"The surprising thing about business insight #{i + 1}.",
                    "core_idea_summary": "A standalone business lesson about focus and growth.",
                    "why_candidate_has_potential": (
                        "Strong hook, clear value, no context required."
                    ),
                    "archetype": archetype,
                    "confidence": 0.78,
                    "scores": _scores(),
                    "warnings": list(warnings or []),
                    "transcript_quality_flags": list(transcript_quality_flags or []),
                }
            )

    result = {
        "schema_version": "section_candidate_discovery_v1",
        "section_id": section_id,
        "usable": usable and bool(candidates),
        "confidence": 0.78 if (usable and candidates) else 0.2,
        "reason": (
            "Strong standalone clip found."
            if (usable and candidates)
            else "No viable standalone clip in this section."
        ),
        "warnings": list(warnings or []),
        "transcript_quality_flags": list(transcript_quality_flags or []),
        "candidates": candidates,
    }
    if with_prompt_metadata:
        result["prompt_metadata"] = {
            "base_prompt_version": "section_candidate_discovery_base_v1",
            "requested_funnel_id": "business",
            "resolved_funnel_id": "business",
            "funnel_rules_version": "business_v1",
        }
    return result


class FakeDiscoveryClient:
    """Deterministic fake AI client for processing pipeline tests."""

    def __init__(
        self,
        per_section_results: dict[str, dict] | None = None,
        default_usable: bool = True,
        default_num_candidates: int = 1,
    ):
        self.per_section_results = per_section_results or {}
        self.default_usable = default_usable
        self.default_num_candidates = default_num_candidates
        self.calls: list[dict] = []

    def discover_section(self, section: dict, *, config) -> dict:
        self.calls.append({"section_id": section["section_id"], "config": config})
        section_id = section["section_id"]
        if section_id in self.per_section_results:
            return self.per_section_results[section_id]
        return _section_result(
            section_id,
            section_start=float(section.get("start_sec", 0.0)),
            section_end=float(section.get("end_sec", 300.0)),
            usable=self.default_usable,
            num_candidates=self.default_num_candidates,
        )


def _run_full_pipeline(
    tmp_path,
    *,
    num_segments: int = 20,
    duration_sec: float = 600.0,
    funnel_id: str = "business",
    ai_client: FakeDiscoveryClient | None = None,
    job_id: str = "gate_job_001",
) -> tuple[ProcessingPipelineResult, Path, Path]:
    """Helper: run the full pipeline and return (result, pool_path, report_path)."""
    transcript = _transcript(num_segments=num_segments, duration_sec=duration_sec)
    client = ai_client or FakeDiscoveryClient()
    result = run_processing_pipeline(
        job_id=job_id,
        job_dir=str(tmp_path),
        transcript=transcript,
        transcript_path="/fixture/transcript.json",
        source_video_path="/fixture/source.mp4",
        funnel_id=funnel_id,
        ai_client=client,
        created_at="2026-06-30T12:00:00+00:00",
    )
    pool_path = Path(result.raw_candidate_pool_path)
    report_path = Path(result.processing_report_path)
    return result, pool_path, report_path


# ---------------------------------------------------------------------------
# 1. Integration: artifacts are written
# ---------------------------------------------------------------------------


def test_full_pipeline_writes_raw_candidate_pool(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    assert pool_path.exists(), "raw_candidate_pool.json was not written"


def test_full_pipeline_writes_processing_report(tmp_path):
    _, _, report_path = _run_full_pipeline(tmp_path)
    assert report_path.exists(), "processing_report.json was not written"


def test_full_pipeline_writes_transcript_sections(tmp_path):
    _run_full_pipeline(tmp_path)
    sections_path = tmp_path / "transcript_sections.json"
    assert sections_path.exists(), "transcript_sections.json was not written"


def test_full_pipeline_writes_section_candidate_discovery(tmp_path):
    _run_full_pipeline(tmp_path)
    discovery_path = tmp_path / "section_candidate_discovery.json"
    assert discovery_path.exists(), "section_candidate_discovery.json was not written"


def test_full_pipeline_writes_candidate_processing(tmp_path):
    _run_full_pipeline(tmp_path)
    processing_path = tmp_path / "candidate_processing.json"
    assert processing_path.exists(), "candidate_processing.json was not written"


def test_transcript_sections_artifact_matches_sectioned_transcript(tmp_path):
    _run_full_pipeline(tmp_path, job_id="sections_job")
    sections_path = tmp_path / "transcript_sections.json"
    payload = json.loads(sections_path.read_text())
    assert payload["schema_version"] == "transcript_sections_v1"
    assert payload["job_id"] == "sections_job"
    assert isinstance(payload.get("sections"), list)
    assert len(payload["sections"]) >= 1
    assert payload["sections"][0]["section_id"].startswith("section_")


def test_discovery_artifact_links_to_transcript_sections_path(tmp_path):
    _run_full_pipeline(tmp_path)
    sections_path = tmp_path / "transcript_sections.json"
    discovery_path = tmp_path / "section_candidate_discovery.json"
    discovery = json.loads(discovery_path.read_text())
    assert discovery["schema_version"] == "section_candidate_discovery_batch_v1"
    assert discovery["source_transcript_sections_path"] == str(sections_path)


def test_pool_candidates_match_processed_batch_after_artifact_writes(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path, job_id="parity_job")
    pool = json.loads(pool_path.read_text())
    processed = json.loads((tmp_path / "candidate_processing.json").read_text())
    batch_for_pool = {
        "section_results": processed["section_results"],
        "rejected_candidates": processed.get("rejected_candidates") or [],
        "duplicate_removals": processed.get("duplicate_removals") or [],
        "duplicates_removed": processed.get("duplicates_removed") or 0,
        "sections_received": processed.get("sections_received") or 0,
        "sections_processed": processed.get("sections_processed") or 0,
        "usable_sections": processed.get("usable_sections") or 0,
        "rejected_sections": processed.get("rejected_sections") or 0,
        "candidates_discovered": processed.get("candidates_discovered") or 0,
        "warnings": processed.get("warnings") or [],
        "failed_sections": processed.get("failed_sections") or [],
    }
    expected = collect_candidates_from_batch(batch_for_pool, "parity_job")
    assert pool["candidates"] == expected


def test_full_pipeline_returns_correct_paths(tmp_path):
    result, _, _ = _run_full_pipeline(tmp_path)
    assert result.raw_candidate_pool_path.endswith("raw_candidate_pool.json")
    assert result.processing_report_path.endswith("processing_report.json")


# ---------------------------------------------------------------------------
# 2. raw_candidate_pool.json field validation
# ---------------------------------------------------------------------------


def test_pool_schema_version(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    assert pool["schema_version"] == contracts.RAW_CANDIDATE_POOL_SCHEMA_VERSION


def test_pool_job_id_present(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path, job_id="gate_job_002")
    pool = json.loads(pool_path.read_text())
    assert pool["job_id"] == "gate_job_002"


def test_pool_source_video_path_present(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    assert isinstance(pool["source_video_path"], str)


def test_pool_transcript_path_present(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    assert isinstance(pool["transcript_path"], str)


def test_pool_processing_version_present(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    assert pool["processing_version"] == contracts.PROCESSING_VERSION


def test_pool_funnel_id_preserved(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path, funnel_id="finance")
    pool = json.loads(pool_path.read_text())
    assert pool["funnel_id"] == "finance"


def test_pool_created_at_present(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    assert isinstance(pool["created_at"], str) and "T" in pool["created_at"]


def test_pool_candidates_is_list(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    assert isinstance(pool["candidates"], list)


def test_pool_has_at_least_one_candidate(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    assert len(pool["candidates"]) >= 1


def test_pool_diagnostics_is_dict(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    assert isinstance(pool["diagnostics"], dict)


# ---------------------------------------------------------------------------
# 3. Candidate field validation
# ---------------------------------------------------------------------------


def test_candidate_ids_are_non_empty(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    for cand in pool["candidates"]:
        assert isinstance(cand["candidate_id"], str) and cand["candidate_id"].strip()


def test_candidate_ids_are_stable_across_runs(tmp_path, tmp_path_factory):
    """Same inputs produce identical candidate IDs."""
    other_tmp = tmp_path_factory.mktemp("run2")
    _, pool_path_1, _ = _run_full_pipeline(tmp_path, job_id="stable_job")
    _, pool_path_2, _ = _run_full_pipeline(other_tmp, job_id="stable_job")
    ids_1 = [c["candidate_id"] for c in json.loads(pool_path_1.read_text())["candidates"]]
    ids_2 = [c["candidate_id"] for c in json.loads(pool_path_2.read_text())["candidates"]]
    assert ids_1 == ids_2


def test_candidate_timestamps_are_numeric(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    for cand in pool["candidates"]:
        assert isinstance(cand["start_sec"], (int, float)) and math.isfinite(cand["start_sec"])
        assert isinstance(cand["end_sec"], (int, float)) and math.isfinite(cand["end_sec"])


def test_candidate_duration_matches_end_minus_start(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    tolerance = contracts.DURATION_TOLERANCE_SEC
    for cand in pool["candidates"]:
        expected = cand["end_sec"] - cand["start_sec"]
        assert abs(cand["duration_sec"] - expected) <= tolerance


def test_candidate_scores_present_with_all_components(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    for cand in pool["candidates"]:
        assert isinstance(cand["scores"], dict)
        for field in SCORES_FIELDS:
            assert field in cand["scores"], f"scores.{field} missing"
            val = cand["scores"][field]
            assert isinstance(val, (int, float)) and 0 <= val <= 10


def test_candidate_evidence_fields_present(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    for cand in pool["candidates"]:
        assert isinstance(cand["hook_text"], str)
        assert isinstance(cand["core_idea_summary"], str)
        assert isinstance(cand["why_candidate_has_potential"], str)


def test_candidate_archetype_is_valid(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    for cand in pool["candidates"]:
        assert cand["archetype"] in contracts.ALLOWED_ARCHETYPES


def test_candidate_warnings_is_list_of_strings(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    for cand in pool["candidates"]:
        assert isinstance(cand["warnings"], list)
        assert all(isinstance(w, str) for w in cand["warnings"])


def test_candidate_transcript_quality_flags_are_valid(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    for cand in pool["candidates"]:
        assert isinstance(cand["transcript_quality_flags"], list)
        for flag in cand["transcript_quality_flags"]:
            assert flag in contracts.ALLOWED_TRANSCRIPT_QUALITY_FLAGS


def test_transcript_quality_flags_are_preserved_end_to_end(tmp_path):
    """Flags set by discovery are preserved through to the pool candidates."""
    section_id = "section_0001"
    client = FakeDiscoveryClient(
        per_section_results={
            section_id: _section_result(
                section_id,
                section_start=0.0,
                section_end=300.0,
                transcript_quality_flags=["low_transcript_confidence"],
            )
        }
    )
    _, pool_path, _ = _run_full_pipeline(tmp_path, ai_client=client)
    pool = json.loads(pool_path.read_text())
    flags_found = [
        f
        for cand in pool["candidates"]
        for f in cand.get("transcript_quality_flags", [])
    ]
    assert "low_transcript_confidence" in flags_found, (
        "transcript_quality_flags from discovery were not preserved in pool"
    )


def test_candidate_warnings_are_preserved_end_to_end(tmp_path):
    section_id = "section_0001"
    client = FakeDiscoveryClient(
        per_section_results={
            section_id: _section_result(
                section_id,
                section_start=0.0,
                section_end=300.0,
                warnings=["soft_boundary_needs_review"],
            )
        }
    )
    _, pool_path, _ = _run_full_pipeline(tmp_path, ai_client=client)
    pool = json.loads(pool_path.read_text())
    all_warnings = [
        w for cand in pool["candidates"] for w in cand.get("warnings", [])
    ]
    assert "soft_boundary_needs_review" in all_warnings


# ---------------------------------------------------------------------------
# 4. processing_report.json field validation
# ---------------------------------------------------------------------------


def test_report_schema_version(tmp_path):
    _, _, report_path = _run_full_pipeline(tmp_path)
    report = json.loads(report_path.read_text())
    assert report["schema_version"] == contracts.PROCESSING_REPORT_SCHEMA_VERSION


def test_report_job_id_present(tmp_path):
    _, _, report_path = _run_full_pipeline(tmp_path, job_id="gate_job_003")
    report = json.loads(report_path.read_text())
    assert report["job_id"] == "gate_job_003"


def test_report_processing_version_present(tmp_path):
    _, _, report_path = _run_full_pipeline(tmp_path)
    report = json.loads(report_path.read_text())
    assert report["processing_version"] == contracts.PROCESSING_VERSION


def test_report_sections_analysed_is_non_negative_int(tmp_path):
    _, _, report_path = _run_full_pipeline(tmp_path)
    report = json.loads(report_path.read_text())
    assert isinstance(report["sections_analysed"], int) and report["sections_analysed"] >= 0


def test_report_usable_sections_count(tmp_path):
    _, _, report_path = _run_full_pipeline(tmp_path)
    report = json.loads(report_path.read_text())
    assert isinstance(report["usable_sections"], int) and report["usable_sections"] >= 0


def test_report_candidate_counts_present(tmp_path):
    _, _, report_path = _run_full_pipeline(tmp_path)
    report = json.loads(report_path.read_text())
    for field in (
        "candidates_discovered",
        "candidates_rejected_by_boundary",
        "duplicates_removed",
        "final_candidate_count",
    ):
        assert isinstance(report[field], int) and report[field] >= 0, f"{field} missing or invalid"


def test_report_failed_sections_is_list(tmp_path):
    _, _, report_path = _run_full_pipeline(tmp_path)
    report = json.loads(report_path.read_text())
    assert isinstance(report["failed_sections"], list)


def test_report_transcript_warnings_is_list(tmp_path):
    _, _, report_path = _run_full_pipeline(tmp_path)
    report = json.loads(report_path.read_text())
    assert isinstance(report["transcript_warnings"], list)


def test_report_processing_warnings_is_list(tmp_path):
    _, _, report_path = _run_full_pipeline(tmp_path)
    report = json.loads(report_path.read_text())
    assert isinstance(report["processing_warnings"], list)


def test_report_prompt_metadata_preserved_from_discovery(tmp_path):
    """Funnel/prompt metadata surfaced by discovery reaches the report."""
    _, _, report_path = _run_full_pipeline(tmp_path, funnel_id="business")
    report = json.loads(report_path.read_text())
    assert isinstance(report["prompt_metadata"], dict)
    # The fake client sets resolved_funnel_id in prompt_metadata
    resolved = report.get("prompt_metadata", {}).get("resolved_funnel_id")
    if resolved:
        assert resolved == "business"


def test_report_created_at_present(tmp_path):
    _, _, report_path = _run_full_pipeline(tmp_path)
    report = json.loads(report_path.read_text())
    assert isinstance(report["created_at"], str) and "T" in report["created_at"]


# ---------------------------------------------------------------------------
# 5. Counts match between pool and report
# ---------------------------------------------------------------------------


def test_final_candidate_count_matches_between_pool_and_report(tmp_path):
    result, pool_path, report_path = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    report = json.loads(report_path.read_text())
    pool_count = len(pool["candidates"])
    report_count = report["final_candidate_count"]
    assert pool_count == report_count, (
        f"pool has {pool_count} candidates but report says {report_count}"
    )


def test_pipeline_result_candidate_count_matches_pool(tmp_path):
    result, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    assert result.final_candidate_count == len(pool["candidates"])


def test_pipeline_result_sections_analysed_matches_report(tmp_path):
    result, _, report_path = _run_full_pipeline(tmp_path)
    report = json.loads(report_path.read_text())
    assert result.sections_analysed == report["sections_analysed"]


# ---------------------------------------------------------------------------
# 6. Main job report links both processing artifacts
# ---------------------------------------------------------------------------


def test_link_processing_artifacts_in_report_adds_paths(tmp_path):
    result, _, _ = _run_full_pipeline(tmp_path)
    job_report: dict = {"job_id": "gate_job_001", "status": "running"}
    link_processing_artifacts_in_report(
        job_report,
        raw_candidate_pool_path=result.raw_candidate_pool_path,
        processing_report_path=result.processing_report_path,
    )
    assert job_report["raw_candidate_pool_path"] == result.raw_candidate_pool_path
    assert job_report["processing_report_path"] == result.processing_report_path


def test_linked_artifact_paths_point_to_existing_files(tmp_path):
    result, _, _ = _run_full_pipeline(tmp_path)
    job_report: dict = {}
    link_processing_artifacts_in_report(
        job_report,
        raw_candidate_pool_path=result.raw_candidate_pool_path,
        processing_report_path=result.processing_report_path,
    )
    assert Path(job_report["raw_candidate_pool_path"]).exists()
    assert Path(job_report["processing_report_path"]).exists()


# ---------------------------------------------------------------------------
# 7. Zero-candidate behaviour
# ---------------------------------------------------------------------------


def test_zero_candidate_pipeline_writes_valid_pool(tmp_path):
    """All sections usable=false: pool must be valid with empty candidates."""
    client = FakeDiscoveryClient(default_usable=False)
    _, pool_path, _ = _run_full_pipeline(tmp_path, ai_client=client)
    pool = json.loads(pool_path.read_text())
    contracts.validate_raw_candidate_pool(pool)
    assert pool["candidates"] == []


def test_zero_candidate_pipeline_writes_valid_report(tmp_path):
    client = FakeDiscoveryClient(default_usable=False)
    _, _, report_path = _run_full_pipeline(tmp_path, ai_client=client)
    report = json.loads(report_path.read_text())
    contracts.validate_processing_report(report)
    assert report["final_candidate_count"] == 0
    assert report["usable_sections"] == 0


def test_zero_candidate_pipeline_result_counts_match(tmp_path):
    client = FakeDiscoveryClient(default_usable=False)
    result, _, _ = _run_full_pipeline(tmp_path, ai_client=client)
    assert result.final_candidate_count == 0
    assert result.usable_sections == 0


def test_zero_candidate_run_does_not_force_fake_candidates(tmp_path):
    client = FakeDiscoveryClient(default_usable=False)
    _, pool_path, _ = _run_full_pipeline(tmp_path, ai_client=client)
    pool = json.loads(pool_path.read_text())
    assert pool["candidates"] == [], "Zero-candidate run must not inject fake candidates"


# ---------------------------------------------------------------------------
# 8. Clean failure behaviour
# ---------------------------------------------------------------------------


def test_pipeline_error_on_empty_transcript_segments(tmp_path):
    """Transcript with no usable segments raises ProcessingPipelineError."""
    empty_transcript = {"text": "", "segments": [], "duration": 0.0}
    with pytest.raises((ProcessingPipelineError, Exception)):
        run_processing_pipeline(
            job_id="fail_job",
            job_dir=str(tmp_path),
            transcript=empty_transcript,
            transcript_path="/fixture/empty.json",
            source_video_path="/fixture/source.mp4",
            ai_client=FakeDiscoveryClient(),
            created_at="2026-06-30T12:00:00+00:00",
        )


def test_pipeline_surfaces_discovery_failures_with_typed_error(tmp_path):
    """If the AI client raises, ProcessingPipelineError is raised."""

    class BrokenClient:
        def discover_section(self, section, *, config):
            raise RuntimeError("connection refused")

    transcript = _transcript()
    with pytest.raises(ProcessingPipelineError) as exc_info:
        run_processing_pipeline(
            job_id="fail_job",
            job_dir=str(tmp_path),
            transcript=transcript,
            transcript_path="/fixture/transcript.json",
            source_video_path="/fixture/source.mp4",
            ai_client=BrokenClient(),
            created_at="2026-06-30T12:00:00+00:00",
        )
    assert exc_info.value.code == "SECTION_DISCOVERY_FAILED"


# ---------------------------------------------------------------------------
# 9. Legacy adapter works in the full pipeline context
# ---------------------------------------------------------------------------


def test_legacy_adapter_produces_segments_from_pipeline_output(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    segments = legacy_segments_from_raw_candidate_pool(pool)
    # Each pool candidate should produce one legacy segment
    assert len(segments) == len(pool["candidates"])
    for seg in segments:
        assert "start" in seg and "end" in seg
        assert seg["end"] > seg["start"]


def test_legacy_adapter_segments_have_legacy_marker(tmp_path):
    """Every legacy segment must carry the temporary adapter annotation."""
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    segments = legacy_segments_from_raw_candidate_pool(pool)
    for seg in segments:
        assert "_legacy_adapter" in seg, "Legacy segment missing _legacy_adapter annotation"


def test_legacy_adapter_zero_candidates_returns_empty_list(tmp_path):
    client = FakeDiscoveryClient(default_usable=False)
    _, pool_path, _ = _run_full_pipeline(tmp_path, ai_client=client)
    pool = json.loads(pool_path.read_text())
    segments = legacy_segments_from_raw_candidate_pool(pool)
    assert segments == []


# ---------------------------------------------------------------------------
# 10. No post-processing artifacts created
# ---------------------------------------------------------------------------


def test_no_post_processing_artifacts_after_pipeline_run(tmp_path):
    """The processing pipeline must not create any post-processing files."""
    _run_full_pipeline(tmp_path)
    files = {f.name for f in Path(tmp_path).iterdir()}
    forbidden = {
        "post_processing_report.json",
        "finished_clips",
        "selection_gate.json",
        "final_selection.json",
    }
    overlap = files & forbidden
    assert not overlap, f"Forbidden post-processing files found: {overlap}"


def test_no_rendered_clips_directory_after_pipeline_run(tmp_path):
    """The processing pipeline must not create a clips/ directory."""
    _run_full_pipeline(tmp_path)
    clips_dir = Path(tmp_path) / "clips"
    assert not clips_dir.exists(), "clips/ directory must not be created by processing pipeline"


# ---------------------------------------------------------------------------
# 11. Both artifacts pass contract validation
# ---------------------------------------------------------------------------


def test_pool_passes_full_contract_validation(tmp_path):
    _, pool_path, _ = _run_full_pipeline(tmp_path)
    pool = json.loads(pool_path.read_text())
    contracts.validate_raw_candidate_pool(pool)


def test_report_passes_full_contract_validation(tmp_path):
    _, _, report_path = _run_full_pipeline(tmp_path)
    report = json.loads(report_path.read_text())
    contracts.validate_processing_report(report)


# ---------------------------------------------------------------------------
# 12. Mixed usable/unusable sections
# ---------------------------------------------------------------------------


def test_mixed_sections_counts_are_accurate(tmp_path):
    """One usable section + one unusable = correct counts in report."""
    client = FakeDiscoveryClient(
        per_section_results={
            "section_0001": _section_result(
                "section_0001", section_start=0.0, section_end=300.0, usable=True
            ),
            "section_0002": _section_result(
                "section_0002", section_start=270.0, section_end=600.0, usable=False
            ),
        }
    )
    _, pool_path, report_path = _run_full_pipeline(tmp_path, ai_client=client)
    pool = json.loads(pool_path.read_text())
    report = json.loads(report_path.read_text())

    # At least one usable section produced candidates
    assert len(pool["candidates"]) >= 1
    assert report["usable_sections"] >= 1
    assert report["rejected_sections"] >= 1


# ---------------------------------------------------------------------------
# Smoke report — fixture-based (no real video available)
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_processing_smoke_fixture_based(tmp_path, capsys):
    """Fixture-based processing smoke.  Prints a summary report.

    NOTE: No real source video is available in this repository at test time.
    A real-video smoke could not be run.  This fixture-based smoke uses a
    realistic 600-second transcript with 20 timed segments and validates the
    full pipeline end-to-end.
    """
    result, pool_path, report_path = _run_full_pipeline(
        tmp_path,
        job_id="smoke_gate_001",
        funnel_id="business",
    )
    pool = json.loads(pool_path.read_text())
    report = json.loads(report_path.read_text())

    summary_lines = [
        "",
        "=== Processing Completion Gate — Smoke Report ===",
        f"  Smoke type              : fixture-based (no real source video available)",
        f"  Job ID                  : {result.job_id}",
        f"  raw_candidate_pool_path : {result.raw_candidate_pool_path}",
        f"  processing_report_path  : {result.processing_report_path}",
        f"  Sections analysed       : {result.sections_analysed}",
        f"  Usable sections         : {result.usable_sections}",
        f"  Rejected sections       : {result.rejected_sections}",
        f"  Final candidates        : {result.final_candidate_count}",
        f"  Duplicates removed      : {result.duplicates_removed}",
        f"  Boundary rejections     : {result.candidates_rejected_by_boundary}",
        f"  Pool schema_version     : {pool['schema_version']}",
        f"  Report schema_version   : {report['schema_version']}",
        f"  Zero-candidate tested   : yes (separate test above)",
        f"  Real-video smoke        : NOT RUN — no suitable local source video in repo",
        "=================================================",
        "",
    ]
    print("\n".join(summary_lines))

    # Substantive assertions
    assert pool["schema_version"] == contracts.RAW_CANDIDATE_POOL_SCHEMA_VERSION
    assert report["schema_version"] == contracts.PROCESSING_REPORT_SCHEMA_VERSION
    assert result.sections_analysed >= 1
    assert isinstance(pool["candidates"], list)
    contracts.validate_raw_candidate_pool(pool)
    contracts.validate_processing_report(report)
