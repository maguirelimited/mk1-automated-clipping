"""Post-processing MK1 — input contract tests (Prompt 14).

Verifies that the post-processing entrypoint:
- correctly loads and validates the raw candidate pool
- resolves the source video path
- creates the output directory structure
- returns READY_FOR_SELECTION for valid inputs
- fails cleanly with INPUT_CONTRACT_FAILED for controlled failure cases
- never mutates raw_candidate_pool.json
- does not call discovery, AI, or rendering code

All tests use temporary files and directories; no real video decode is needed.
"""

from __future__ import annotations

import hashlib
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
from post_processing_mk1 import (  # noqa: E402
    POST_PROCESSING_ENTRYPOINT_SCHEMA_VERSION,
    POST_PROCESSING_VERSION,
    STATUS_INPUT_CONTRACT_FAILED,
    STATUS_READY_FOR_SELECTION,
    run_post_processing_mk1,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _valid_candidate(
    *,
    job_id: str = "pp_job_001",
    section_id: str = "section_0001",
    start_sec: float = 10.0,
    end_sec: float = 55.0,
) -> dict[str, Any]:
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
        "duration_sec": round(end_sec - start_sec, 3),
        "hook_text": "The one insight that changed everything.",
        "core_idea_summary": "A concise standalone business lesson.",
        "why_candidate_has_potential": "Strong hook, no context required, clear payoff.",
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
        "warnings": [],
        "transcript_quality_flags": [],
    }


def _valid_pool(
    *,
    job_id: str = "pp_job_001",
    source_video_path: str = "/fixture/source.mp4",
    candidates: list[dict] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": contracts.RAW_CANDIDATE_POOL_SCHEMA_VERSION,
        "job_id": job_id,
        "source_video_path": source_video_path,
        "transcript_path": "/fixture/transcript.json",
        "processing_version": contracts.PROCESSING_VERSION,
        "funnel_id": "business",
        "created_at": "2026-06-30T12:00:00+00:00",
        "candidates": list(candidates or []),
        "diagnostics": {},
    }


def _write_pool(directory: Path, pool: dict[str, Any]) -> Path:
    """Write a pool dict to raw_candidate_pool.json in the given directory."""
    path = directory / "raw_candidate_pool.json"
    path.write_text(json.dumps(pool), encoding="utf-8")
    return path


def _dummy_video(directory: Path, name: str = "source.mp4") -> Path:
    """Create a zero-byte dummy source video file."""
    path = directory / name
    path.write_bytes(b"")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 1. Valid raw candidate pool → READY_FOR_SELECTION
# ---------------------------------------------------------------------------


def test_valid_pool_returns_ready_for_selection(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video), candidates=[_valid_candidate()])
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(
        str(pool_path),
        output_root=str(tmp_path),
    )

    assert result["status"] == STATUS_READY_FOR_SELECTION


def test_valid_pool_result_includes_schema_version(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["schema_version"] == POST_PROCESSING_ENTRYPOINT_SCHEMA_VERSION


def test_valid_pool_result_includes_post_processing_version(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["post_processing_version"] == POST_PROCESSING_VERSION


def test_valid_pool_result_has_no_errors(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["errors"] == []


# ---------------------------------------------------------------------------
# 2. Zero-candidate pool → READY_FOR_SELECTION with warning
# ---------------------------------------------------------------------------


def test_zero_candidate_pool_returns_ready_for_selection(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video), candidates=[])
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["status"] == STATUS_READY_FOR_SELECTION


def test_zero_candidate_pool_adds_zero_candidates_warning(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video), candidates=[])
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert "zero_candidates_received" in result["warnings"]


def test_zero_candidate_pool_raw_candidates_received_is_zero(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video), candidates=[])
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["raw_candidates_received"] == 0


# ---------------------------------------------------------------------------
# 3. Missing raw candidate pool file → INPUT_CONTRACT_FAILED
# ---------------------------------------------------------------------------


def test_missing_pool_file_returns_input_contract_failed(tmp_path):
    missing_path = str(tmp_path / "does_not_exist.json")

    result = run_post_processing_mk1(missing_path, output_root=str(tmp_path))

    assert result["status"] == STATUS_INPUT_CONTRACT_FAILED


def test_missing_pool_file_has_correct_error_code(tmp_path):
    missing_path = str(tmp_path / "does_not_exist.json")

    result = run_post_processing_mk1(missing_path, output_root=str(tmp_path))

    assert result["error_code"] == "missing_raw_candidate_pool"


def test_missing_pool_file_errors_list_is_non_empty(tmp_path):
    missing_path = str(tmp_path / "does_not_exist.json")

    result = run_post_processing_mk1(missing_path, output_root=str(tmp_path))

    assert len(result["errors"]) >= 1
    assert result["errors"][0]["code"] == "missing_raw_candidate_pool"


# ---------------------------------------------------------------------------
# 4. Invalid JSON pool → INPUT_CONTRACT_FAILED
# ---------------------------------------------------------------------------


def test_invalid_json_pool_returns_input_contract_failed(tmp_path):
    pool_path = tmp_path / "raw_candidate_pool.json"
    pool_path.write_text("{not valid json", encoding="utf-8")

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["status"] == STATUS_INPUT_CONTRACT_FAILED


def test_invalid_json_pool_has_correct_error_code(tmp_path):
    pool_path = tmp_path / "raw_candidate_pool.json"
    pool_path.write_text("{not valid json", encoding="utf-8")

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["error_code"] == "invalid_raw_candidate_pool_json"


# ---------------------------------------------------------------------------
# 5. Invalid schema/version → INPUT_CONTRACT_FAILED
# ---------------------------------------------------------------------------


def test_wrong_schema_version_returns_input_contract_failed(tmp_path):
    pool = _valid_pool()
    pool["schema_version"] = "wrong_version_xyz"
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["status"] == STATUS_INPUT_CONTRACT_FAILED


def test_wrong_schema_version_has_correct_error_code(tmp_path):
    pool = _valid_pool()
    pool["schema_version"] = "wrong_version_xyz"
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["error_code"] == "invalid_raw_candidate_pool_schema"


def test_missing_required_pool_field_returns_input_contract_failed(tmp_path):
    pool = _valid_pool()
    del pool["job_id"]
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["status"] == STATUS_INPUT_CONTRACT_FAILED
    assert result["error_code"] == "invalid_raw_candidate_pool_schema"


def test_candidates_not_a_list_returns_input_contract_failed(tmp_path):
    pool = _valid_pool()
    pool["candidates"] = "not a list"
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["status"] == STATUS_INPUT_CONTRACT_FAILED


# ---------------------------------------------------------------------------
# 6. Missing source video → INPUT_CONTRACT_FAILED
# ---------------------------------------------------------------------------


def test_missing_source_video_path_argument_and_pool_returns_failed(tmp_path):
    pool = _valid_pool(source_video_path="/nonexistent/video.mp4")
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["status"] == STATUS_INPUT_CONTRACT_FAILED
    assert result["error_code"] == "missing_source_video"


def test_explicit_missing_source_video_argument_returns_failed(tmp_path):
    pool = _valid_pool(source_video_path="/nonexistent/fallback.mp4")
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(
        str(pool_path),
        source_video_path="/nonexistent/explicit.mp4",
        output_root=str(tmp_path),
    )

    assert result["status"] == STATUS_INPUT_CONTRACT_FAILED
    assert result["error_code"] == "missing_source_video"


def test_no_source_video_anywhere_uses_missing_source_video_code(tmp_path):
    pool = _valid_pool()
    del pool["source_video_path"]
    pool["source_video_path"] = "/nonexistent/video.mp4"
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["status"] == STATUS_INPUT_CONTRACT_FAILED


# ---------------------------------------------------------------------------
# 7. Source video path is a directory → INPUT_CONTRACT_FAILED
# ---------------------------------------------------------------------------


def test_source_video_is_directory_returns_input_contract_failed(tmp_path):
    video_dir = tmp_path / "not_a_video_file"
    video_dir.mkdir()
    pool = _valid_pool(source_video_path=str(video_dir))
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["status"] == STATUS_INPUT_CONTRACT_FAILED


def test_source_video_is_directory_has_correct_error_code(tmp_path):
    video_dir = tmp_path / "not_a_video_file"
    video_dir.mkdir()
    pool = _valid_pool(source_video_path=str(video_dir))
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["error_code"] == "source_video_not_a_file"


# ---------------------------------------------------------------------------
# 8. Output directory structure is created
# ---------------------------------------------------------------------------


def test_output_directories_are_created(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)
    output_root = tmp_path / "output"

    run_post_processing_mk1(str(pool_path), output_root=str(output_root))

    assert (output_root / "post_processing").is_dir()
    assert (output_root / "post_processing" / "selection").is_dir()
    assert (output_root / "post_processing" / "clips").is_dir()
    assert (output_root / "post_processing" / "metadata").is_dir()
    assert (output_root / "post_processing" / "reports").is_dir()
    assert (output_root / "post_processing" / "tmp").is_dir()


def test_result_directories_dict_contains_all_expected_keys(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    dirs = result["directories"]
    for key in (
        "post_processing_root",
        "selection",
        "clips",
        "metadata",
        "reports",
        "tmp",
    ):
        assert key in dirs, f"directories.{key} missing from result"


def test_output_directories_are_idempotent(tmp_path):
    """Running the entrypoint twice must not fail even if dirs already exist."""
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)

    run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))
    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["status"] == STATUS_READY_FOR_SELECTION


# ---------------------------------------------------------------------------
# 9. raw_candidates_received count matches the pool
# ---------------------------------------------------------------------------


def test_raw_candidates_received_matches_pool_count(tmp_path):
    video = _dummy_video(tmp_path)
    candidates = [
        _valid_candidate(start_sec=10.0, end_sec=55.0),
        _valid_candidate(section_id="section_0002", start_sec=100.0, end_sec=145.0),
        _valid_candidate(section_id="section_0003", start_sec=200.0, end_sec=245.0),
    ]
    pool = _valid_pool(source_video_path=str(video), candidates=candidates)
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["raw_candidates_received"] == 3


def test_raw_candidates_received_is_zero_for_empty_pool(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video), candidates=[])
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["raw_candidates_received"] == 0


def test_raw_candidates_received_is_one_for_single_candidate(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(
        source_video_path=str(video), candidates=[_valid_candidate()]
    )
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["raw_candidates_received"] == 1


# ---------------------------------------------------------------------------
# 10. raw_candidate_pool.json is not mutated
# ---------------------------------------------------------------------------


def test_pool_file_not_mutated_by_entrypoint(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video), candidates=[_valid_candidate()])
    pool_path = _write_pool(tmp_path, pool)

    digest_before = _sha256(pool_path)
    run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))
    digest_after = _sha256(pool_path)

    assert digest_before == digest_after, (
        "raw_candidate_pool.json was mutated by run_post_processing_mk1"
    )


def test_pool_file_bytes_identical_after_entrypoint(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)

    bytes_before = pool_path.read_bytes()
    run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))
    bytes_after = pool_path.read_bytes()

    assert bytes_before == bytes_after, (
        "raw_candidate_pool.json bytes changed after entrypoint call"
    )


def test_pool_file_not_mutated_even_on_failure(tmp_path):
    """Pool file must not be modified even when the entrypoint returns a failure."""
    pool = _valid_pool(source_video_path="/nonexistent/video.mp4")
    pool_path = _write_pool(tmp_path, pool)

    digest_before = _sha256(pool_path)
    run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))
    digest_after = _sha256(pool_path)

    assert digest_before == digest_after


# ---------------------------------------------------------------------------
# 11. Entrypoint does not call discovery, AI, or rendering code
# ---------------------------------------------------------------------------


def test_entrypoint_does_not_import_discovery_or_ai_modules():
    """post_processing_mk1 must not import discovery, AI, or rendering modules."""
    import post_processing_mk1 as pp

    forbidden_names = {
        "section_candidate_discovery",
        "clip_video",
        "ai_service_client",
        "transcribe_video",
        "transcript_sectioning",
        "processing_pipeline",
        "ai_settings",
    }
    module_attrs = set(vars(pp).keys())
    for name in forbidden_names:
        assert name not in module_attrs, (
            f"post_processing_mk1 must not reference {name!r}"
        )


def test_entrypoint_succeeds_without_any_ai_client(tmp_path):
    """Running the entrypoint must not require an AI client of any kind."""
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)

    # No ai_client parameter — must succeed without it
    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["status"] == STATUS_READY_FOR_SELECTION


def test_entrypoint_creates_no_rendered_clips(tmp_path):
    """Running the entrypoint must not create any rendered clip files."""
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video), candidates=[_valid_candidate()])
    pool_path = _write_pool(tmp_path, pool)
    output_root = tmp_path / "output"

    run_post_processing_mk1(str(pool_path), output_root=str(output_root))

    clips_dir = output_root / "post_processing" / "clips"
    clip_files = list(clips_dir.iterdir()) if clips_dir.exists() else []
    assert clip_files == [], f"Rendered clip files unexpectedly found: {clip_files}"


def test_entrypoint_creates_no_post_processing_report(tmp_path):
    """Running the entrypoint must not write a post_processing_report.json yet."""
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)
    output_root = tmp_path / "output"

    run_post_processing_mk1(str(pool_path), output_root=str(output_root))

    forbidden = [
        output_root / "post_processing_report.json",
        output_root / "post_processing" / "post_processing_report.json",
        output_root / "post_processing" / "reports" / "post_processing_report.json",
    ]
    for path in forbidden:
        assert not path.exists(), f"Forbidden artifact found: {path}"


# ---------------------------------------------------------------------------
# 12. Job metadata and config are accepted and reflected
# ---------------------------------------------------------------------------


def test_job_id_from_metadata_overrides_pool_job_id(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(job_id="pool_job_id", source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(
        str(pool_path),
        job_metadata={"job_id": "explicit_job_id"},
        output_root=str(tmp_path),
    )

    assert result["job_id"] == "explicit_job_id"


def test_job_id_falls_back_to_pool_job_id(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(job_id="pool_job_from_pool", source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(
        str(pool_path),
        job_metadata={},
        output_root=str(tmp_path),
    )

    assert result["job_id"] == "pool_job_from_pool"


def test_job_metadata_is_preserved_in_result(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)
    metadata = {"job_id": "pp_job_001", "funnel_id": "finance", "run_env": "test"}

    result = run_post_processing_mk1(
        str(pool_path),
        job_metadata=metadata,
        output_root=str(tmp_path),
    )

    assert result["job_metadata"]["funnel_id"] == "finance"
    assert result["job_metadata"]["run_env"] == "test"


def test_config_is_preserved_in_result(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)
    config = {"max_clips": 5, "quality_threshold": 0.7}

    result = run_post_processing_mk1(
        str(pool_path),
        config=config,
        output_root=str(tmp_path),
    )

    assert result["config"]["max_clips"] == 5
    assert result["config"]["quality_threshold"] == 0.7


def test_explicit_source_video_path_takes_precedence_over_pool(tmp_path):
    fallback_video = _dummy_video(tmp_path, "fallback.mp4")
    explicit_video = _dummy_video(tmp_path, "explicit.mp4")
    pool = _valid_pool(source_video_path=str(fallback_video))
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(
        str(pool_path),
        source_video_path=str(explicit_video),
        output_root=str(tmp_path),
    )

    assert result["status"] == STATUS_READY_FOR_SELECTION
    assert result["source_video_path"] == str(explicit_video)


def test_source_video_path_falls_back_to_pool_field(tmp_path):
    video = _dummy_video(tmp_path, "pool_video.mp4")
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)

    # No explicit source_video_path — must fall back to pool field
    result = run_post_processing_mk1(
        str(pool_path),
        output_root=str(tmp_path),
    )

    assert result["status"] == STATUS_READY_FOR_SELECTION
    assert result["source_video_path"] == str(video)


def test_result_includes_raw_candidate_pool_path(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["raw_candidate_pool_path"] == str(pool_path)


def test_result_includes_output_root(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["output_root"] == str(tmp_path)


def test_result_includes_job_id(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video))
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["job_id"] == "pp_job_001"


# ---------------------------------------------------------------------------
# Failure result shape validation
# ---------------------------------------------------------------------------


def test_failure_result_has_correct_schema_version(tmp_path):
    missing = str(tmp_path / "nope.json")

    result = run_post_processing_mk1(missing, output_root=str(tmp_path))

    assert result["schema_version"] == POST_PROCESSING_ENTRYPOINT_SCHEMA_VERSION


def test_failure_result_has_errors_list(tmp_path):
    missing = str(tmp_path / "nope.json")

    result = run_post_processing_mk1(missing, output_root=str(tmp_path))

    assert isinstance(result["errors"], list)
    assert len(result["errors"]) >= 1


def test_failure_result_error_entry_has_code_and_message(tmp_path):
    missing = str(tmp_path / "nope.json")

    result = run_post_processing_mk1(missing, output_root=str(tmp_path))

    error = result["errors"][0]
    assert "code" in error
    assert "message" in error
    assert isinstance(error["code"], str)
    assert isinstance(error["message"], str)


def test_no_error_warns_for_valid_input(tmp_path):
    video = _dummy_video(tmp_path)
    pool = _valid_pool(source_video_path=str(video), candidates=[_valid_candidate()])
    pool_path = _write_pool(tmp_path, pool)

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["warnings"] == []
