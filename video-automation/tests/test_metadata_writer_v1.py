"""metadata_writer_v1 — focused tests (Prompt 22).

Tests are split into:
  - Module contract
  - Metadata writing (file creation, naming, overwrite)
  - Candidate metadata capture
  - Module results capture
  - Validation result mapping
  - Failed clip handling
  - Config validation
  - Conveyor integration (uses dummy modules for speed — no real ffmpeg needed)

No real ffmpeg/ffprobe is required for any of these tests.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from typing import Any

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from post_processing_modules import (  # noqa: E402
    MODULE_STATUS_FAIL,
    MODULE_STATUS_PASS,
    PostProcessingModule,
    make_module_fail_result,
    make_module_pass_result,
    validate_module_result,
)
from post_processing_conveyor import (  # noqa: E402
    CONVEYOR_STATUS_COMPLETE,
    FIXED_MK1_CONVEYOR_MODULES,
    run_fixed_mk1_universal_conveyor,
)
from metadata_writer_v1 import (  # noqa: E402
    CLIP_METADATA_SCHEMA_VERSION,
    METADATA_WRITER_V1_MODULE,
    MODULE_NAME,
    MODULE_VERSION,
    MetadataWriterV1Module,
    _deep_json_safe,
    _normalise_module_results,
    _resolve_validation_info,
    _safe_dict,
    _safe_float,
    _safe_list,
    _safe_str,
    get_metadata_writer_v1_module,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_candidate(
    candidate_id: str = "cand_001",
    start_sec: float = 10.0,
    end_sec: float = 14.0,
    duration_sec: float | None = None,
    scores: dict | None = None,
    archetype: str | None = "educational_explainer",
    confidence: float | None = 0.85,
    hook_text: str | None = "This is the hook",
    core_idea_summary: str | None = "Core summary here",
    warnings: list | None = None,
    transcript_quality_flags: list | None = None,
    rank: int | None = 1,
) -> dict[str, Any]:
    c: dict[str, Any] = {
        "candidate_id": candidate_id,
        "start_sec": start_sec,
        "end_sec": end_sec,
        "rank": rank,
    }
    if duration_sec is not None:
        c["duration_sec"] = duration_sec
    if scores is not None:
        c["scores"] = scores
    if archetype is not None:
        c["archetype"] = archetype
    if confidence is not None:
        c["confidence"] = confidence
    if hook_text is not None:
        c["hook_text"] = hook_text
    if core_idea_summary is not None:
        c["core_idea_summary"] = core_idea_summary
    if warnings is not None:
        c["warnings"] = warnings
    if transcript_quality_flags is not None:
        c["transcript_quality_flags"] = transcript_quality_flags
    return c


def _make_module_result(
    name: str,
    status: str = "PASS",
    output_path: str | None = None,
    metadata: dict | None = None,
    error_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "post_processing_module_result_v1",
        "module_name": name,
        "module_version": "1.0",
        "status": status,
        "input_path": None,
        "output_path": output_path,
        "config": {},
        "error_reason": error_reason if status != "PASS" else None,
        "warnings": [],
        "metadata": dict(metadata or {}),
    }


def _passing_upstream_results(final_clip_path: str) -> list[dict[str, Any]]:
    """Minimal PASS results for all four upstream modules."""
    return [
        _make_module_result("render_clip_v1", "PASS", output_path="render.mp4",
                            metadata={"actual_duration_sec": 4.0}),
        _make_module_result("platform_safe_format_v1", "PASS", output_path="psf.mp4",
                            metadata={"output_duration_sec": 4.0}),
        _make_module_result("intelligent_captions_v1", "PASS", output_path="captions.mp4",
                            metadata={"caption_count": 5}),
        _make_module_result("validation_v1", "PASS", output_path=final_clip_path,
                            metadata={"validated_output_path": final_clip_path}),
    ]


def _make_context(
    *,
    candidate: dict[str, Any] | None = None,
    module_results: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
    metadata_dir: str | None = None,
    clip_id: str | None = None,
    job_id: str = "job_test123",
    source_video_path: str | None = "/src/video.mp4",
) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "job_id": job_id,
        "candidate_id": (candidate or _make_candidate()).get("candidate_id"),
        "source_video_path": source_video_path,
        "selected_candidate": candidate if candidate is not None else _make_candidate(),
        "module_results": list(module_results or []),
        "config": dict(config or {}),
    }
    if metadata_dir is not None:
        ctx["metadata_dir"] = metadata_dir
    if clip_id is not None:
        ctx["clip_id"] = clip_id
    return ctx


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_module_name_exact(self):
        assert MetadataWriterV1Module().module_name == "metadata_writer_v1"

    def test_module_name_constant(self):
        assert MODULE_NAME == "metadata_writer_v1"

    def test_module_version_exists(self):
        m = MetadataWriterV1Module()
        assert m.module_version and isinstance(m.module_version, str)

    def test_module_version_is_1_0(self):
        assert MODULE_VERSION == "1.0"

    def test_is_post_processing_module(self):
        assert isinstance(MetadataWriterV1Module(), PostProcessingModule)

    def test_registry_constant_exists(self):
        assert METADATA_WRITER_V1_MODULE is not None
        assert isinstance(METADATA_WRITER_V1_MODULE, MetadataWriterV1Module)

    def test_get_module_returns_fresh_instance(self):
        m1 = get_metadata_writer_v1_module()
        m2 = get_metadata_writer_v1_module()
        assert isinstance(m1, MetadataWriterV1Module)
        assert m1 is not m2

    def test_result_standard_shape_on_pass(self, tmp_path):
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            module_results=_passing_upstream_results(str(tmp_path / "clip.mp4")),
        )
        result = MetadataWriterV1Module().run(ctx, input_path=str(tmp_path / "clip.mp4"))
        validate_module_result(result)
        assert result["status"] == MODULE_STATUS_PASS

    def test_result_standard_shape_on_fail(self, tmp_path):
        ctx = _make_context(metadata_dir=str(tmp_path))
        # No job_id and no candidate_id
        ctx["job_id"] = None
        ctx["selected_candidate"] = {}
        result = MetadataWriterV1Module().run(ctx, input_path=str(tmp_path / "clip.mp4"))
        validate_module_result(result)
        assert result["status"] == MODULE_STATUS_FAIL

    def test_context_is_not_mutated(self, tmp_path):
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            module_results=_passing_upstream_results(str(tmp_path / "clip.mp4")),
        )
        before = copy.deepcopy(ctx)
        MetadataWriterV1Module().run(ctx, input_path=str(tmp_path / "clip.mp4"))
        assert ctx == before


# ---------------------------------------------------------------------------
# Metadata writing
# ---------------------------------------------------------------------------


class TestMetadataWriting:
    def test_writes_json_file(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            module_results=_passing_upstream_results(clip),
        )
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS
        meta_path = result["metadata"]["metadata_path"]
        assert os.path.isfile(meta_path)

    def test_creates_metadata_directory_if_missing(self, tmp_path):
        missing_dir = str(tmp_path / "nested" / "metadata")
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(metadata_dir=missing_dir, module_results=_passing_upstream_results(clip))
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS
        assert os.path.isdir(missing_dir)

    def test_deterministic_filename_uses_clip_id(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(
            clip_id="myclip42",
            metadata_dir=str(tmp_path),
            module_results=_passing_upstream_results(clip),
        )
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS
        meta_path = result["metadata"]["metadata_path"]
        assert "myclip42" in os.path.basename(meta_path)
        assert "_metadata_writer_v1.json" in meta_path

    def test_fallback_filename_uses_job_and_candidate_id(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            module_results=_passing_upstream_results(clip),
        )
        # No explicit clip_id — should be built from job_id + candidate_id
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS
        meta_path = result["metadata"]["metadata_path"]
        assert "_metadata_writer_v1.json" in meta_path
        # Should contain parts of job_id and candidate_id
        filename = os.path.basename(meta_path)
        assert "job_test123" in filename or "cand_001" in filename

    def test_does_not_overwrite_by_default(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            module_results=_passing_upstream_results(clip),
        )
        # First write
        MetadataWriterV1Module().run(ctx, input_path=clip)
        # Second write — should fail
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "metadata_file_exists"

    def test_overwrite_enabled_by_config(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            module_results=_passing_upstream_results(clip),
            config={"metadata_writer_v1": {"allow_overwrite": True}},
        )
        MetadataWriterV1Module().run(ctx, input_path=clip)
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS

    def test_json_is_valid_object(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            module_results=_passing_upstream_results(clip),
        )
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        meta_path = result["metadata"]["metadata_path"]
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_output_path_preserves_clip_path(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            module_results=_passing_upstream_results(clip),
        )
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["output_path"] == clip

    def test_metadata_path_in_module_result_metadata(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            module_results=_passing_upstream_results(clip),
        )
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert "metadata_path" in result["metadata"]
        assert result["metadata"]["metadata_path"].endswith(".json")

    def test_metadata_path_also_in_written_json(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            module_results=_passing_upstream_results(clip),
        )
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        meta_path = result["metadata"]["metadata_path"]
        with open(meta_path) as f:
            data = json.load(f)
        assert data["metadata_path"] == meta_path

    def test_metadata_dir_from_context_post_processing_dirs(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(module_results=_passing_upstream_results(clip))
        ctx["post_processing_dirs"] = {"metadata": str(tmp_path / "ppd_meta")}
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS
        assert "ppd_meta" in result["metadata"]["metadata_path"]

    def test_metadata_dir_from_context_paths(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(module_results=_passing_upstream_results(clip))
        ctx["paths"] = {"metadata_dir": str(tmp_path / "paths_meta")}
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS
        assert "paths_meta" in result["metadata"]["metadata_path"]

    def test_metadata_dir_fallback_sibling_of_input(self, tmp_path):
        """When no metadata_dir, fall back to a 'metadata' dir beside the clip."""
        clip_dir = tmp_path / "clips"
        clip_dir.mkdir()
        clip = str(clip_dir / "clip.mp4")
        ctx = _make_context(module_results=_passing_upstream_results(clip))
        # No metadata_dir in context
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS
        expected_meta_dir = str(clip_dir / "metadata")
        assert result["metadata"]["metadata_path"].startswith(expected_meta_dir)


# ---------------------------------------------------------------------------
# Candidate metadata
# ---------------------------------------------------------------------------


class TestCandidateMetadata:
    def _run_and_load(self, tmp_path, candidate, **ctx_kwargs) -> dict[str, Any]:
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(
            candidate=candidate,
            metadata_dir=str(tmp_path),
            module_results=_passing_upstream_results(clip),
            **ctx_kwargs,
        )
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS, result["error_reason"]
        with open(result["metadata"]["metadata_path"]) as f:
            return json.load(f)

    def test_records_candidate_id(self, tmp_path):
        data = self._run_and_load(tmp_path, _make_candidate(candidate_id="candidate_xyz"))
        assert data["source_candidate_id"] == "candidate_xyz"

    def test_records_start_end_sec(self, tmp_path):
        data = self._run_and_load(tmp_path, _make_candidate(start_sec=5.0, end_sec=12.0))
        assert data["input_start_sec"] == 5.0
        assert data["input_end_sec"] == 12.0

    def test_records_explicit_duration(self, tmp_path):
        data = self._run_and_load(tmp_path, _make_candidate(duration_sec=7.0))
        assert data["input_duration_sec"] == 7.0

    def test_computes_duration_from_timestamps(self, tmp_path):
        cand = _make_candidate(start_sec=3.0, end_sec=9.0, duration_sec=None)
        data = self._run_and_load(tmp_path, cand)
        assert data["input_duration_sec"] == pytest.approx(6.0)

    def test_preserves_scores(self, tmp_path):
        scores = {"relevance": 0.9, "energy": 0.75}
        data = self._run_and_load(tmp_path, _make_candidate(scores=scores))
        assert data["input_candidate_scores"] == scores

    def test_preserves_archetype(self, tmp_path):
        data = self._run_and_load(tmp_path, _make_candidate(archetype="debate_highlight"))
        assert data["input_candidate_archetype"] == "debate_highlight"

    def test_preserves_confidence(self, tmp_path):
        data = self._run_and_load(tmp_path, _make_candidate(confidence=0.92))
        assert data["input_candidate_confidence"] == pytest.approx(0.92)

    def test_preserves_hook_text(self, tmp_path):
        data = self._run_and_load(tmp_path, _make_candidate(hook_text="Hook sentence here"))
        assert data["input_candidate_hook_text"] == "Hook sentence here"

    def test_preserves_core_idea_summary(self, tmp_path):
        data = self._run_and_load(tmp_path, _make_candidate(core_idea_summary="Main idea"))
        assert data["input_candidate_core_idea_summary"] == "Main idea"

    def test_preserves_warnings(self, tmp_path):
        data = self._run_and_load(tmp_path, _make_candidate(warnings=["low_energy", "partial_transcript"]))
        assert "low_energy" in data["input_candidate_warnings"]
        assert "partial_transcript" in data["input_candidate_warnings"]

    def test_preserves_transcript_quality_flags(self, tmp_path):
        data = self._run_and_load(tmp_path, _make_candidate(transcript_quality_flags=["missing_words"]))
        assert "missing_words" in data["input_candidate_transcript_quality_flags"]

    def test_missing_optional_fields_preserved_as_null(self, tmp_path):
        cand: dict[str, Any] = {"candidate_id": "bare_cand", "start_sec": 1.0, "end_sec": 5.0}
        data = self._run_and_load(tmp_path, cand)
        assert data["input_candidate_archetype"] is None
        assert data["input_candidate_confidence"] is None
        assert data["input_candidate_hook_text"] is None
        assert data["input_candidate_core_idea_summary"] is None
        assert data["input_candidate_warnings"] == []
        assert data["input_candidate_transcript_quality_flags"] == []
        assert data["input_candidate_scores"] == {}


# ---------------------------------------------------------------------------
# Module results
# ---------------------------------------------------------------------------


class TestModuleResultsCapture:
    def _run_and_load(self, tmp_path, module_results) -> dict[str, Any]:
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            module_results=module_results,
        )
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS, result
        with open(result["metadata"]["metadata_path"]) as f:
            return json.load(f)

    def test_records_all_prior_module_results(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        upstream = _passing_upstream_results(clip)
        data = self._run_and_load(tmp_path, upstream)
        result_names = [r["module_name"] for r in data["module_results"]]
        assert "render_clip_v1" in result_names
        assert "platform_safe_format_v1" in result_names
        assert "intelligent_captions_v1" in result_names
        assert "validation_v1" in result_names

    def test_records_module_versions(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        upstream = _passing_upstream_results(clip)
        data = self._run_and_load(tmp_path, upstream)
        assert "render_clip_v1" in data["module_versions"]

    def test_records_module_configs(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        upstream = _passing_upstream_results(clip)
        data = self._run_and_load(tmp_path, upstream)
        assert "metadata_writer_v1" in data["module_configs"]

    def test_modules_applied_list(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        upstream = _passing_upstream_results(clip)
        data = self._run_and_load(tmp_path, upstream)
        assert "render_clip_v1" in data["modules_applied"]
        assert "metadata_writer_v1" in data["modules_applied"]

    def test_handles_missing_optional_metadata_without_crashing(self, tmp_path):
        minimal = [
            {"schema_version": "post_processing_module_result_v1",
             "module_name": "render_clip_v1", "module_version": "1.0",
             "status": "PASS", "input_path": None, "output_path": "r.mp4",
             "config": {}, "error_reason": None, "warnings": [], "metadata": {}},
            {"schema_version": "post_processing_module_result_v1",
             "module_name": "validation_v1", "module_version": "1.0",
             "status": "PASS", "input_path": None, "output_path": "v.mp4",
             "config": {}, "error_reason": None, "warnings": [], "metadata": {}},
        ]
        data = self._run_and_load(tmp_path, minimal)
        assert isinstance(data["module_results"], list)

    def test_module_results_json_serialisable(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        upstream = _passing_upstream_results(clip)
        data = self._run_and_load(tmp_path, upstream)
        # Should be loadable without error (already tested by loading above)
        json.dumps(data)


# ---------------------------------------------------------------------------
# Validation result mapping
# ---------------------------------------------------------------------------


class TestValidationMapping:
    def _run_and_load(self, tmp_path, module_results) -> tuple[dict, dict]:
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            module_results=module_results,
        )
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS, result
        with open(result["metadata"]["metadata_path"]) as f:
            data = json.load(f)
        return result, data

    def test_validation_pass_creates_pass_result(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        _, data = self._run_and_load(tmp_path, _passing_upstream_results(clip))
        assert data["validation_result"] == "PASS"
        assert data["failure_reason"] is None
        assert data["failed_module"] is None

    def test_validation_fail_creates_fail_result(self, tmp_path):
        module_results = [
            _make_module_result("render_clip_v1", "PASS", output_path="r.mp4"),
            _make_module_result("platform_safe_format_v1", "PASS", output_path="psf.mp4"),
            _make_module_result("intelligent_captions_v1", "PASS", output_path="cap.mp4"),
            _make_module_result("validation_v1", "FAIL",
                                error_reason="duration_mismatch",
                                metadata={"failure_code": "duration_mismatch"}),
        ]
        _, data = self._run_and_load(tmp_path, module_results)
        assert data["validation_result"] == "FAIL"
        assert data["failure_reason"] == "duration_mismatch"
        assert data["failed_module"] == "validation_v1"

    def test_validation_failure_reason_preserved(self, tmp_path):
        module_results = [
            _make_module_result("validation_v1", "FAIL", error_reason="aspect_ratio_mismatch"),
        ]
        _, data = self._run_and_load(tmp_path, module_results)
        assert data["failure_reason"] == "aspect_ratio_mismatch"

    def test_missing_validation_result_creates_unknown(self, tmp_path):
        # No validation_v1 in results at all
        module_results = [
            _make_module_result("render_clip_v1", "PASS", output_path="r.mp4"),
        ]
        result, data = self._run_and_load(tmp_path, module_results)
        assert data["validation_result"] == "UNKNOWN"
        assert "missing_validation_result" in data["warnings"]

    def test_earlier_module_failure_recorded(self, tmp_path):
        module_results = [
            _make_module_result("render_clip_v1", "FAIL", error_reason="ffmpeg_failed"),
        ]
        _, data = self._run_and_load(tmp_path, module_results)
        assert data["validation_result"] == "FAIL"
        assert data["failed_module"] == "render_clip_v1"
        assert data["failure_reason"] == "ffmpeg_failed"


# ---------------------------------------------------------------------------
# Failed clips
# ---------------------------------------------------------------------------


class TestFailedClips:
    def test_metadata_writer_passes_when_clip_validation_failed(self, tmp_path):
        """Metadata writer PASS even when validation FAIL."""
        clip = str(tmp_path / "clip.mp4")
        module_results = [
            _make_module_result("render_clip_v1", "PASS", output_path="r.mp4"),
            _make_module_result("platform_safe_format_v1", "PASS", output_path="psf.mp4"),
            _make_module_result("intelligent_captions_v1", "PASS", output_path=clip),
            _make_module_result("validation_v1", "FAIL", error_reason="duration_mismatch"),
        ]
        ctx = _make_context(metadata_dir=str(tmp_path), module_results=module_results)
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS
        assert result["metadata"]["validation_result"] == "FAIL"
        assert result["metadata"]["failure_reason"] == "duration_mismatch"

    def test_metadata_contains_failed_module_and_reason(self, tmp_path):
        module_results = [
            _make_module_result("render_clip_v1", "FAIL", error_reason="missing_source_video"),
        ]
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(metadata_dir=str(tmp_path), module_results=module_results)
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS
        with open(result["metadata"]["metadata_path"]) as f:
            data = json.load(f)
        assert data["failed_module"] == "render_clip_v1"
        assert data["failure_reason"] == "missing_source_video"

    def test_metadata_writer_only_fails_when_writing_itself_fails(self, tmp_path):
        """The writer should only FAIL when it can't write; not for validation failures."""
        module_results = [
            _make_module_result("validation_v1", "FAIL", error_reason="aspect_ratio_mismatch"),
        ]
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(metadata_dir=str(tmp_path), module_results=module_results)
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        # Writer should succeed even though validation failed
        assert result["status"] == MODULE_STATUS_PASS

    def test_partial_module_results_do_not_crash(self, tmp_path):
        """Partial module results (e.g., only render ran) should not crash the writer."""
        module_results = [
            _make_module_result("render_clip_v1", "PASS", output_path="r.mp4"),
        ]
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(metadata_dir=str(tmp_path), module_results=module_results)
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS


# ---------------------------------------------------------------------------
# Required fields / schema
# ---------------------------------------------------------------------------


class TestRequiredFields:
    def _load_metadata(self, tmp_path, module_results=None) -> dict[str, Any]:
        clip = str(tmp_path / "clip.mp4")
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            module_results=module_results or _passing_upstream_results(clip),
        )
        result = MetadataWriterV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_PASS
        with open(result["metadata"]["metadata_path"]) as f:
            return json.load(f)

    def test_schema_version_is_clip_metadata_v1(self, tmp_path):
        data = self._load_metadata(tmp_path)
        assert data["schema_version"] == CLIP_METADATA_SCHEMA_VERSION
        assert data["schema_version"] == "clip_metadata_v1"

    def test_metadata_writer_version_present(self, tmp_path):
        data = self._load_metadata(tmp_path)
        assert data["metadata_writer_version"] == "1.0"

    def test_required_fields_present(self, tmp_path):
        data = self._load_metadata(tmp_path)
        required = [
            "schema_version", "metadata_writer_version", "clip_id", "job_id",
            "source_candidate_id", "output_file_path", "metadata_path",
            "input_start_sec", "input_end_sec", "input_duration_sec",
            "input_candidate_scores", "input_candidate_archetype",
            "input_candidate_confidence", "input_candidate_hook_text",
            "input_candidate_core_idea_summary", "input_candidate_warnings",
            "input_candidate_transcript_quality_flags",
            "selection_mode", "selection_rank", "selection_reason",
            "modules_applied", "module_versions", "module_configs", "module_results",
            "validation_result", "failure_reason", "failed_module",
            "warnings", "created_at",
        ]
        for field in required:
            assert field in data, f"Missing required field: {field!r}"

    def test_metadata_is_json_serialisable(self, tmp_path):
        data = self._load_metadata(tmp_path)
        json.dumps(data)  # must not raise

    def test_output_file_path_is_clip_not_json(self, tmp_path):
        clip = str(tmp_path / "clip.mp4")
        data = self._load_metadata(tmp_path)
        assert data["output_file_path"] == clip

    def test_created_at_is_iso_timestamp(self, tmp_path):
        data = self._load_metadata(tmp_path)
        import datetime
        # Should be parseable as ISO 8601
        ts = data["created_at"]
        assert isinstance(ts, str)
        datetime.datetime.fromisoformat(ts)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_invalid_indent_fails(self, tmp_path):
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            config={"metadata_writer_v1": {"indent": "bad"}},
        )
        result = MetadataWriterV1Module().run(ctx, input_path=str(tmp_path / "c.mp4"))
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "invalid_metadata_config"

    def test_invalid_allow_overwrite_fails(self, tmp_path):
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            config={"metadata_writer_v1": {"allow_overwrite": "yes"}},
        )
        result = MetadataWriterV1Module().run(ctx, input_path=str(tmp_path / "c.mp4"))
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "invalid_metadata_config"

    def test_invalid_sort_keys_fails(self, tmp_path):
        ctx = _make_context(
            metadata_dir=str(tmp_path),
            config={"metadata_writer_v1": {"sort_keys": 1}},
        )
        result = MetadataWriterV1Module().run(ctx, input_path=str(tmp_path / "c.mp4"))
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "invalid_metadata_config"

    def test_missing_clip_id_fails(self, tmp_path):
        ctx = _make_context(metadata_dir=str(tmp_path))
        ctx["job_id"] = None
        ctx["selected_candidate"] = {}
        ctx["source_candidate"] = {}
        result = MetadataWriterV1Module().run(ctx, input_path=str(tmp_path / "c.mp4"))
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "missing_clip_id"

    def test_missing_candidate_id_fails(self, tmp_path):
        ctx = _make_context(metadata_dir=str(tmp_path))
        ctx["selected_candidate"] = {"candidate_id": ""}
        result = MetadataWriterV1Module().run(ctx, input_path=str(tmp_path / "c.mp4"))
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "missing_candidate_id"

    def test_missing_metadata_dir_fails(self):
        """No metadata_dir and no input_path means no fallback dir."""
        ctx = _make_context()
        result = MetadataWriterV1Module().run(ctx, input_path=None)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] in (
            "missing_metadata_dir", "missing_input_path", "missing_clip_id",
        )


# ---------------------------------------------------------------------------
# Conveyor integration
# ---------------------------------------------------------------------------


class TestConveyorIntegration:
    """Tests using a registry of dummy modules plus the real metadata_writer_v1."""

    class _DummyPass(PostProcessingModule):
        def __init__(self, name: str):
            self.module_name = name
            self.module_version = "1.0"

        def run(self, context, *, input_path=None, config=None):
            out = f"/tmp/{self.module_name}.out"
            return make_module_pass_result(
                self.module_name, self.module_version,
                input_path=input_path, output_path=out,
            )

    def _make_registry(self, metadata_dir: str) -> dict[str, Any]:
        """Build a registry with dummies for the first four modules and real metadata_writer."""
        from metadata_writer_v1 import MetadataWriterV1Module as MW

        class MetaWriterWithDir(PostProcessingModule):
            """Inject metadata_dir via config so the real module knows where to write."""
            module_name = "metadata_writer_v1"
            module_version = "1.0"

            def run(self, context, *, input_path=None, config=None):
                # Inject metadata_dir into a copy of context
                ctx2 = dict(context)
                ctx2["metadata_dir"] = metadata_dir
                return MW().run(ctx2, input_path=input_path, config=config)

        return {
            "render_clip_v1": self._DummyPass("render_clip_v1"),
            "platform_safe_format_v1": self._DummyPass("platform_safe_format_v1"),
            "intelligent_captions_v1": self._DummyPass("intelligent_captions_v1"),
            "validation_v1": self._DummyPass("validation_v1"),
            "metadata_writer_v1": MetaWriterWithDir(),
        }

    def _selection_result(self) -> dict[str, Any]:
        return {
            "job_id": "conveyor_job",
            "status": "PASS",
            "selected_candidates": [
                _make_candidate(candidate_id="cand_conv_01"),
            ],
        }

    def test_fixed_conveyor_includes_metadata_writer(self):
        assert "metadata_writer_v1" in FIXED_MK1_CONVEYOR_MODULES

    def test_successful_conveyor_run_writes_metadata(self, tmp_path):
        registry = self._make_registry(str(tmp_path))
        result = run_fixed_mk1_universal_conveyor(
            self._selection_result(),
            source_video_path="/src/video.mp4",
            job_metadata={"job_id": "conveyor_job"},
            module_registry=registry,
        )
        assert result["status"] == CONVEYOR_STATUS_COMPLETE
        clip_result = result["clip_results"][0]
        assert clip_result["status"] == "PASS"
        # Metadata file should have been written
        written = list(tmp_path.glob("*.json"))
        assert len(written) == 1

    def test_final_conveyor_output_path_is_clip_not_json(self, tmp_path):
        registry = self._make_registry(str(tmp_path))
        result = run_fixed_mk1_universal_conveyor(
            self._selection_result(),
            source_video_path="/src/video.mp4",
            job_metadata={"job_id": "conveyor_job"},
            module_registry=registry,
        )
        final_path = result["clip_results"][0].get("final_output_path", "")
        assert not (final_path or "").endswith(".json"), (
            f"Final output path should be a clip, got: {final_path!r}"
        )

    def test_metadata_path_discoverable_from_module_result(self, tmp_path):
        registry = self._make_registry(str(tmp_path))
        result = run_fixed_mk1_universal_conveyor(
            self._selection_result(),
            source_video_path="/src/video.mp4",
            job_metadata={"job_id": "conveyor_job"},
            module_registry=registry,
        )
        module_results = result["clip_results"][0].get("module_results", [])
        mw_result = next(
            (r for r in module_results if r.get("module_name") == "metadata_writer_v1"),
            None,
        )
        assert mw_result is not None
        assert "metadata_path" in mw_result.get("metadata", {})


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    def test_resolve_validation_info_pass(self):
        results = [_make_module_result("validation_v1", "PASS")]
        vr, fr, fm = _resolve_validation_info(results)
        assert vr == "PASS"
        assert fr is None
        assert fm is None

    def test_resolve_validation_info_fail(self):
        results = [_make_module_result("validation_v1", "FAIL", error_reason="bad_duration")]
        vr, fr, fm = _resolve_validation_info(results)
        assert vr == "FAIL"
        assert fr == "bad_duration"
        assert fm == "validation_v1"

    def test_resolve_validation_info_unknown(self):
        vr, fr, fm = _resolve_validation_info([])
        assert vr == "UNKNOWN"

    def test_resolve_validation_info_earlier_fail(self):
        results = [_make_module_result("render_clip_v1", "FAIL", error_reason="no_source")]
        vr, fr, fm = _resolve_validation_info(results)
        assert vr == "FAIL"
        assert fm == "render_clip_v1"
        assert fr == "no_source"

    def test_safe_str(self):
        assert _safe_str("hello") == "hello"
        assert _safe_str("  ") is None
        assert _safe_str(None) is None
        assert _safe_str(42) == "42"

    def test_safe_float(self):
        assert _safe_float(1.5) == 1.5
        assert _safe_float(0) == 0.0
        assert _safe_float(None) is None
        assert _safe_float(True) is None
        assert _safe_float("bad") is None

    def test_safe_dict(self):
        assert _safe_dict({"a": 1}) == {"a": 1}
        assert _safe_dict(None) == {}
        assert _safe_dict("nope") == {}

    def test_safe_list(self):
        assert _safe_list([1, 2]) == [1, 2]
        assert _safe_list(None) == []
        assert _safe_list("nope") == []

    def test_normalise_module_results(self):
        results = [_make_module_result("render_clip_v1", "PASS")]
        normed = _normalise_module_results(results)
        assert isinstance(normed, list)
        assert len(normed) == 1

    def test_deep_json_safe_passes_through_primitives(self):
        assert _deep_json_safe(1) == 1
        assert _deep_json_safe("x") == "x"
        assert _deep_json_safe(None) is None

    def test_deep_json_safe_converts_non_serialisable(self):
        class Custom:
            def __str__(self):
                return "custom_repr"
        result = _deep_json_safe(Custom())
        assert result == "custom_repr"
