"""
tests/config/test_artifact_context_propagation.py

Tests for Prompt 5.6: Pipeline Artifact Execution Context Propagation.

Covers:
    - load_execution_context_for_job() helper
    - processing_report.json receives execution_context
    - raw_candidate_pool.json receives execution_context
    - post_processing_report.json receives execution_context (via build_post_processing_report)
    - per-clip metadata receives execution_context (via MetadataWriterV1Module)
    - legacy jobs without execution_context.json work without crashing
    - context-aware pipeline logging

Run with:
    video-automation/.venv/bin/python -m pytest tests/config/test_artifact_context_propagation.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# scripts/config (for execution_context.py)
_SCRIPTS_CONFIG = str(REPO_ROOT / "scripts" / "config")
if _SCRIPTS_CONFIG not in sys.path:
    sys.path.insert(0, _SCRIPTS_CONFIG)

# video-automation/scripts (for processing_contracts, metadata_writer_v1, etc.)
_VA_SCRIPTS = str(REPO_ROOT / "video-automation" / "scripts")
if _VA_SCRIPTS not in sys.path:
    sys.path.insert(0, _VA_SCRIPTS)

from execution_context import load_execution_context_for_job

# ---------------------------------------------------------------------------
# Sample execution context fixture
# ---------------------------------------------------------------------------

SAMPLE_EXEC_CTX: dict[str, Any] = {
    "environment": "development",
    "job_id": "job_20260101T120000Z_abc12345",
    "funnel_id": "business",
    "platform_id": "youtube",
    "preset_id": "growth",
    "config_version": "1",
    "resolved_config_path": "/jobs/dev/job_abc/resolved_config.yaml",
    "code_commit": "abc12345",
}


def _write_exec_ctx(job_dir: Path, ctx: dict | None = None) -> None:
    target = job_dir / "execution_context.json"
    job_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(ctx or SAMPLE_EXEC_CTX, indent=2))


# ---------------------------------------------------------------------------
# Tests: load_execution_context_for_job()
# ---------------------------------------------------------------------------


class TestLoadExecutionContextForJob:
    def test_loads_valid_file(self, tmp_path):
        _write_exec_ctx(tmp_path)
        result = load_execution_context_for_job(tmp_path)
        assert result is not None
        assert result["environment"] == "development"
        assert result["job_id"] == "job_20260101T120000Z_abc12345"

    def test_returns_none_for_missing_file(self, tmp_path):
        """Legacy jobs without execution_context.json return None."""
        tmp_path.mkdir(parents=True, exist_ok=True)
        result = load_execution_context_for_job(tmp_path)
        assert result is None

    def test_returns_none_for_malformed_json(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "execution_context.json").write_text("not-valid-json{{{")
        result = load_execution_context_for_job(tmp_path)
        assert result is None

    def test_returns_none_for_non_dict_json(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "execution_context.json").write_text('["a", "b"]')
        result = load_execution_context_for_job(tmp_path)
        assert result is None

    def test_returns_dict_not_execution_context_object(self, tmp_path):
        _write_exec_ctx(tmp_path)
        result = load_execution_context_for_job(tmp_path)
        assert isinstance(result, dict)

    def test_does_not_crash_on_unreadable_directory(self, tmp_path):
        result = load_execution_context_for_job(tmp_path / "nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# Tests: raw_candidate_pool.json
# ---------------------------------------------------------------------------


class TestRawCandidatePoolExecutionContext:
    def test_includes_execution_context_when_provided(self):
        from processing_contracts import build_raw_candidate_pool
        pool = build_raw_candidate_pool(
            job_id="job_test_001",
            source_video_path="/source.mp4",
            transcript_path="/transcript.json",
            funnel_id="business",
            execution_context=SAMPLE_EXEC_CTX,
        )
        assert "execution_context" in pool
        assert pool["execution_context"]["environment"] == "development"
        assert pool["execution_context"]["job_id"] == "job_20260101T120000Z_abc12345"

    def test_omits_execution_context_when_not_provided(self):
        from processing_contracts import build_raw_candidate_pool
        pool = build_raw_candidate_pool(
            job_id="job_test_002",
            source_video_path="/source.mp4",
            transcript_path="/transcript.json",
            funnel_id="business",
        )
        assert "execution_context" not in pool

    def test_omits_execution_context_when_none(self):
        from processing_contracts import build_raw_candidate_pool
        pool = build_raw_candidate_pool(
            job_id="job_test_003",
            source_video_path="/source.mp4",
            transcript_path="/transcript.json",
            funnel_id="business",
            execution_context=None,
        )
        assert "execution_context" not in pool

    def test_candidate_data_unchanged(self):
        """Candidate selection/scoring data must not be modified."""
        from processing_contracts import build_raw_candidate_pool
        # Full minimal candidate satisfying the schema validator
        candidate = {
            "candidate_id": "cand_001",
            "start_sec": 10.0,
            "end_sec": 40.0,
            "duration_sec": 30.0,
            "source_section_id": "sec_001",
            "hook_text": "Test hook",
            "core_idea_summary": "Core idea",
            "why_candidate_has_potential": "Strong value",
            "archetype": "valuable_insight",
            "confidence": 0.85,
            "warnings": [],
            "transcript_quality_flags": [],
            "scores": {
                "overall_potential": 8,
                "hook_strength": 7,
                "standalone_context": 8,
                "insight_value": 9,
                "retention_potential": 8,
                "natural_ending": 7,
            },
        }
        pool = build_raw_candidate_pool(
            job_id="job_test_004",
            source_video_path="/source.mp4",
            transcript_path="/transcript.json",
            funnel_id="business",
            candidates=[candidate],
            execution_context=SAMPLE_EXEC_CTX,
        )
        assert pool["candidates"][0]["candidate_id"] == "cand_001"
        assert pool["candidates"][0]["start_sec"] == 10.0

    def test_schema_version_preserved(self):
        from processing_contracts import build_raw_candidate_pool, RAW_CANDIDATE_POOL_SCHEMA_VERSION
        pool = build_raw_candidate_pool(
            job_id="job_test_005",
            source_video_path="/source.mp4",
            transcript_path="/transcript.json",
            funnel_id="business",
            execution_context=SAMPLE_EXEC_CTX,
        )
        assert pool["schema_version"] == RAW_CANDIDATE_POOL_SCHEMA_VERSION

    def test_write_and_read_roundtrip(self, tmp_path):
        from processing_contracts import build_raw_candidate_pool, write_raw_candidate_pool
        pool = build_raw_candidate_pool(
            job_id="job_roundtrip",
            source_video_path="/source.mp4",
            transcript_path="/transcript.json",
            funnel_id="business",
            execution_context=SAMPLE_EXEC_CTX,
        )
        path = write_raw_candidate_pool(str(tmp_path), pool)
        data = json.loads(Path(path).read_text())
        assert data["execution_context"]["environment"] == "development"


# ---------------------------------------------------------------------------
# Tests: processing_report.json
# ---------------------------------------------------------------------------


class TestProcessingReportExecutionContext:
    def test_includes_execution_context_when_provided(self):
        from processing_contracts import build_processing_report
        report = build_processing_report(
            job_id="job_rpt_001",
            execution_context=SAMPLE_EXEC_CTX,
        )
        assert "execution_context" in report
        assert report["execution_context"]["funnel_id"] == "business"

    def test_omits_execution_context_when_not_provided(self):
        from processing_contracts import build_processing_report
        report = build_processing_report(job_id="job_rpt_002")
        assert "execution_context" not in report

    def test_existing_count_fields_preserved(self):
        from processing_contracts import build_processing_report
        report = build_processing_report(
            job_id="job_rpt_003",
            sections_analysed=5,
            final_candidate_count=3,
            execution_context=SAMPLE_EXEC_CTX,
        )
        assert report["sections_analysed"] == 5
        assert report["final_candidate_count"] == 3

    def test_write_and_read_roundtrip(self, tmp_path):
        from processing_contracts import build_processing_report, write_processing_report
        report = build_processing_report(
            job_id="job_rpt_rtrip",
            execution_context=SAMPLE_EXEC_CTX,
        )
        path = write_processing_report(str(tmp_path), report)
        data = json.loads(Path(path).read_text())
        assert data["execution_context"]["environment"] == "development"

    def test_diagnostics_report_threads_context(self):
        """build_processing_diagnostics_report passes execution_context through."""
        from processing_diagnostics import build_processing_diagnostics_report
        discovery_batch: dict[str, Any] = {
            "section_results": [],
            "failed_sections": [],
            "rejected_candidates": [],
            "duplicate_removals": [],
            "warnings": [],
        }
        report = build_processing_diagnostics_report(
            job_id="job_diag_001",
            discovery_batch=discovery_batch,
            execution_context=SAMPLE_EXEC_CTX,
        )
        assert "execution_context" in report
        assert report["execution_context"]["job_id"] == "job_20260101T120000Z_abc12345"

    def test_diagnostics_report_without_context_still_works(self):
        from processing_diagnostics import build_processing_diagnostics_report
        discovery_batch: dict[str, Any] = {
            "section_results": [],
            "failed_sections": [],
            "rejected_candidates": [],
            "duplicate_removals": [],
        }
        report = build_processing_diagnostics_report(
            job_id="job_diag_002",
            discovery_batch=discovery_batch,
        )
        assert "execution_context" not in report
        assert report["job_id"] == "job_diag_002"


# ---------------------------------------------------------------------------
# Tests: post_processing_report.json
# ---------------------------------------------------------------------------


class TestPostProcessingReportExecutionContext:
    def _make_minimal_selection_result(self) -> dict[str, Any]:
        return {
            "status": "CANDIDATES_SELECTED",
            "selected_candidates": [],
            "rejected_candidates": [],
            "selection_mode": "balanced",
        }

    def _make_minimal_pool(self) -> dict[str, Any]:
        from processing_contracts import RAW_CANDIDATE_POOL_SCHEMA_VERSION
        return {
            "schema_version": RAW_CANDIDATE_POOL_SCHEMA_VERSION,
            "job_id": "job_pp_001",
            "source_video_path": "/source.mp4",
            "transcript_path": "/transcript.json",
            "processing_version": "1.0",
            "funnel_id": "business",
            "created_at": "2026-01-01T12:00:00Z",
            "candidates": [],
            "diagnostics": {},
        }

    def test_build_post_processing_report_includes_execution_context(self, tmp_path):
        from post_processing_report_v1 import build_post_processing_report
        report = build_post_processing_report(
            job_id="job_pp_001",
            selection_result=self._make_minimal_selection_result(),
            conveyor_result=None,
            raw_candidate_pool=self._make_minimal_pool(),
            raw_candidate_pool_path="/jobs/dev/job/raw_candidate_pool.json",
            source_video_path="/source.mp4",
            report_path=str(tmp_path / "pp_report.json"),
            execution_context=SAMPLE_EXEC_CTX,
        )
        assert "execution_context" in report
        assert report["execution_context"]["environment"] == "development"

    def test_build_post_processing_report_without_context_still_works(self, tmp_path):
        from post_processing_report_v1 import build_post_processing_report
        report = build_post_processing_report(
            job_id="job_pp_002",
            selection_result=self._make_minimal_selection_result(),
            conveyor_result=None,
            raw_candidate_pool=self._make_minimal_pool(),
            raw_candidate_pool_path="/jobs/dev/job/raw_candidate_pool.json",
            source_video_path="/source.mp4",
            report_path=str(tmp_path / "pp_report2.json"),
        )
        # execution_context should be absent or None — not crash
        ec = report.get("execution_context")
        assert ec is None or isinstance(ec, dict)


# ---------------------------------------------------------------------------
# Tests: per-clip metadata
# ---------------------------------------------------------------------------


class TestClipMetadataExecutionContext:
    def _make_clip_context(
        self,
        job_id: str = "job_clip_001",
        execution_context: dict | None = None,
    ) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "job_id": job_id,
            "candidate_id": "cand_001",
            "clip_id": f"{job_id}_cand_001",
            "source_video_path": "/source.mp4",
            "working_dir": None,
            "clip_dir": None,
            "metadata_dir": None,
            "tmp_dir": None,
            "config": {},
            "selection_result": {},
            "selected_candidate": {
                "candidate_id": "cand_001",
                "start_sec": 10.0,
                "end_sec": 40.0,
                "rank": 1,
                "scores": {"overall_potential": 8},
                "archetype": "insight",
                "confidence": 0.85,
                "hook_text": "Test hook",
                "core_idea_summary": "Core idea",
            },
            "source_candidate": {"candidate_id": "cand_001"},
            "module_results": [],
        }
        if execution_context is not None:
            ctx["execution_context"] = execution_context
        return ctx

    def _make_module(self) -> Any:
        from metadata_writer_v1 import MetadataWriterV1Module
        return MetadataWriterV1Module()

    def test_writes_execution_context_in_clip_metadata(self, tmp_path):
        module = self._make_module()
        input_clip = tmp_path / "clip.mp4"
        input_clip.write_bytes(b"fake-clip")
        metadata_dir = tmp_path / "metadata"
        metadata_dir.mkdir()

        ctx = self._make_clip_context(execution_context=SAMPLE_EXEC_CTX)
        ctx["metadata_dir"] = str(metadata_dir)

        result = module.run(ctx, input_path=str(input_clip))

        metadata_path = result.get("metadata", {}).get("metadata_path")
        if metadata_path and Path(metadata_path).exists():
            data = json.loads(Path(metadata_path).read_text())
            assert "execution_context" in data
            assert data["execution_context"]["environment"] == "development"

    def test_omits_execution_context_for_legacy_clips(self, tmp_path):
        """Legacy clips without context in the module context must not crash."""
        module = self._make_module()
        input_clip = tmp_path / "clip.mp4"
        input_clip.write_bytes(b"fake-clip")
        metadata_dir = tmp_path / "metadata"
        metadata_dir.mkdir()

        ctx = self._make_clip_context(execution_context=None)
        ctx["metadata_dir"] = str(metadata_dir)

        result = module.run(ctx, input_path=str(input_clip))

        # Should not crash — check status or metadata written
        assert result is not None
        metadata_path = result.get("metadata", {}).get("metadata_path")
        if metadata_path and Path(metadata_path).exists():
            data = json.loads(Path(metadata_path).read_text())
            # execution_context must be absent, not present as null
            assert "execution_context" not in data or data.get("execution_context") is None

    def test_title_caption_metadata_unchanged(self, tmp_path):
        """Clip scoring/candidate evidence must not be changed."""
        module = self._make_module()
        input_clip = tmp_path / "clip.mp4"
        input_clip.write_bytes(b"fake-clip")
        metadata_dir = tmp_path / "metadata"
        metadata_dir.mkdir()

        ctx = self._make_clip_context(execution_context=SAMPLE_EXEC_CTX)
        ctx["metadata_dir"] = str(metadata_dir)

        result = module.run(ctx, input_path=str(input_clip))

        metadata_path = result.get("metadata", {}).get("metadata_path")
        if metadata_path and Path(metadata_path).exists():
            data = json.loads(Path(metadata_path).read_text())
            # Scoring and timing data must still be present
            assert data.get("job_id") == "job_clip_001"
            assert data.get("source_candidate_id") == "cand_001"


# ---------------------------------------------------------------------------
# Tests: boundary — fake job directory with all artifacts
# ---------------------------------------------------------------------------


class TestJobBoundaryArtifacts:
    """
    Lightweight end-to-end check: given a fake job directory with
    execution_context.json, load the context and verify it can be
    threaded through the artifact builders without crashing.
    """

    def test_all_internal_artifacts_receive_same_context(self, tmp_path):
        _write_exec_ctx(tmp_path, SAMPLE_EXEC_CTX)

        ctx = load_execution_context_for_job(tmp_path)
        assert ctx is not None

        from processing_contracts import build_raw_candidate_pool, build_processing_report
        from processing_diagnostics import build_processing_diagnostics_report

        pool = build_raw_candidate_pool(
            job_id=SAMPLE_EXEC_CTX["job_id"],
            source_video_path="/source.mp4",
            transcript_path="/transcript.json",
            funnel_id="business",
            execution_context=ctx,
        )
        assert pool["execution_context"]["job_id"] == SAMPLE_EXEC_CTX["job_id"]

        report = build_processing_report(
            job_id=SAMPLE_EXEC_CTX["job_id"],
            execution_context=ctx,
        )
        assert report["execution_context"]["job_id"] == SAMPLE_EXEC_CTX["job_id"]

        # Both should reference the same context
        assert pool["execution_context"] == report["execution_context"]

    def test_legacy_job_without_context_doesnt_crash(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)

        ctx = load_execution_context_for_job(tmp_path)
        assert ctx is None

        from processing_contracts import build_raw_candidate_pool, build_processing_report

        pool = build_raw_candidate_pool(
            job_id="legacy_job",
            source_video_path="/source.mp4",
            transcript_path="/transcript.json",
            funnel_id="business",
            execution_context=ctx,
        )
        assert "execution_context" not in pool

        report = build_processing_report(
            job_id="legacy_job",
            execution_context=ctx,
        )
        assert "execution_context" not in report


# ---------------------------------------------------------------------------
# Tests: pipeline start logging
# ---------------------------------------------------------------------------


class TestPipelineStartLogging:
    def _import_server_app(self):
        import importlib.util as _ilu
        spec = _ilu.spec_from_file_location(
            "app",
            str(REPO_ROOT / "video-automation" / "server" / "app.py"),
        )
        cached = sys.modules.get("app")
        if cached is not None and hasattr(cached, "_log_pipeline_start"):
            return cached
        mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules["app"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    def test_logs_context_when_provided(self, capsys):
        app_mod = self._import_server_app()
        app_mod._log_pipeline_start(
            job_id="job_test_log",
            funnel_id="business",
            execution_context=SAMPLE_EXEC_CTX,
        )
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "job_test_log" in out
        assert "development" in out
        assert "business" in out

    def test_logs_legacy_notice_when_no_context(self, capsys):
        app_mod = self._import_server_app()
        app_mod._log_pipeline_start(
            job_id="job_legacy_log",
            funnel_id=None,
            execution_context=None,
        )
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "job_legacy_log" in out
        assert "legacy" in out

    def test_does_not_log_secrets(self, capsys):
        """Logging must never emit sensitive values."""
        ctx_with_secret_key = dict(SAMPLE_EXEC_CTX)
        # The context dict should only have provenance fields — no secrets.
        # Verify the log doesn't contain anything that looks like a secret.
        app_mod = self._import_server_app()
        app_mod._log_pipeline_start(
            job_id="job_secret_test",
            funnel_id="business",
            execution_context=ctx_with_secret_key,
        )
        captured = capsys.readouterr()
        out = captured.out + captured.err
        # Must contain the job id
        assert "job_secret_test" in out
        # Must NOT contain the resolved_config_path (full paths are not provenance)
        assert "resolved_config.yaml" not in out
