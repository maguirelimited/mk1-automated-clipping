"""Fixed MK1 Universal Conveyor — focused tests (Prompt 17).

Verifies the fixed conveyor orchestrator:
- Fixed module list (exactly 5 modules, stable order)
- Module registry / dependency injection
- Missing required modules → CONVEYOR_FAILED
- Per-candidate chain execution with dummy modules
- Input path forwarding
- Output path forwarding between modules
- Per-clip pass / fail recording
- Failed clip does not crash the conveyor
- Summary counts
- Zero selected candidates
- Invalid selection result handling
- Job ID resolution
- Deterministic clip IDs
- Module context contents
- JSON serializability
- No imports of ffmpeg / captioning / AI / output-funnel code

All tests use dummy modules only.  No real video files or ffmpeg required.
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
    PostProcessingModule,
    make_module_fail_result,
    make_module_pass_result,
)
from post_processing_conveyor import (  # noqa: E402
    CLIP_STATUS_FAIL,
    CLIP_STATUS_PASS,
    CONVEYOR_SCHEMA_VERSION,
    CONVEYOR_STATUS_COMPLETE,
    CONVEYOR_STATUS_FAILED,
    FIXED_MK1_CONVEYOR_MODULES,
    run_fixed_mk1_universal_conveyor,
)


# ---------------------------------------------------------------------------
# Dummy modules
# ---------------------------------------------------------------------------


class _DummyPassModule(PostProcessingModule):
    """Pass module that appends its name to the output path so tests can track it."""

    def __init__(self, name: str, version: str = "1.0"):
        self.module_name = name
        self.module_version = version

    def run(self, context, *, input_path=None, config=None):
        out = f"{input_path}.{self.module_name}.out" if input_path else f"/tmp/{self.module_name}.out"
        return make_module_pass_result(
            self.module_name,
            self.module_version,
            input_path=input_path,
            output_path=out,
        )


class _DummyFailModule(PostProcessingModule):
    """Fail module that always fails with a controlled reason."""

    def __init__(self, name: str, reason: str = "dummy_module_failed", version: str = "1.0"):
        self.module_name = name
        self.module_version = version
        self._reason = reason

    def run(self, context, *, input_path=None, config=None):
        return make_module_fail_result(
            self.module_name,
            self.module_version,
            self._reason,
            input_path=input_path,
        )


def _make_pass_registry() -> dict[str, Any]:
    """Return a registry where all five fixed modules are dummy pass modules."""
    return {name: _DummyPassModule(name) for name in FIXED_MK1_CONVEYOR_MODULES}


def _make_registry_with_fail(failing_name: str, reason: str = "dummy_failed") -> dict[str, Any]:
    """Return a registry where one module fails and the rest pass."""
    registry = _make_pass_registry()
    registry[failing_name] = _DummyFailModule(failing_name, reason=reason)
    return registry


# ---------------------------------------------------------------------------
# Minimal valid selection result builder
# ---------------------------------------------------------------------------


def _make_candidate(
    candidate_id: str = "cand_001",
    rank: int = 1,
    start_sec: float = 10.0,
    end_sec: float = 70.0,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "rank": rank,
        "start_sec": start_sec,
        "end_sec": end_sec,
        "duration_sec": end_sec - start_sec,
        "confidence": 0.85,
        "scores": {"overall_potential": 8.5},
        "selection_reason": "selected_by_rank",
        "warnings": [],
        "transcript_quality_flags": [],
        "source_candidate": {},
    }


def _make_selection_result(
    candidates: list[dict[str, Any]] | None = None,
    job_id: str = "job_test_001",
) -> dict[str, Any]:
    if candidates is None:
        candidates = [_make_candidate()]
    return {
        "schema_version": "selection_gate_v1",
        "job_id": job_id,
        "status": "SELECTION_COMPLETE",
        "selected_candidates": candidates,
        "rejected_candidates": [],
        "reserve_candidates": [],
        "selection_summary": {
            "raw_candidates_received": len(candidates),
            "eligible_count": len(candidates),
            "selected_count": len(candidates),
            "rejected_count": 0,
            "reserve_count": 0,
        },
        "warnings": [],
        "errors": [],
    }


SOURCE_VIDEO = "/fake/source.mp4"
JOB_META = {"job_id": "job_test_001"}


# ===========================================================================
# 1–4: Fixed module list
# ===========================================================================


def test_fixed_module_list_has_exactly_five():
    assert len(FIXED_MK1_CONVEYOR_MODULES) == 5


def test_fixed_module_order_is_exact():
    assert FIXED_MK1_CONVEYOR_MODULES == [
        "render_clip_v1",
        "platform_safe_format_v1",
        "intelligent_captions_v1",
        "validation_v1",
        "metadata_writer_v1",
    ]


def test_conveyor_ignores_config_based_reorder():
    """Config cannot change MK1 module order — any order hint is ignored."""
    sr = _make_selection_result()
    config = {"module_order": ["validation_v1", "render_clip_v1"]}
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        config=config,
        module_registry=_make_pass_registry(),
    )
    # Conveyor still completes (order not altered)
    assert result["status"] == CONVEYOR_STATUS_COMPLETE
    assert result["required_modules"] == FIXED_MK1_CONVEYOR_MODULES


def test_all_five_modules_required():
    """Every required module must be present in the registry."""
    for name in FIXED_MK1_CONVEYOR_MODULES:
        partial_registry = {n: _DummyPassModule(n) for n in FIXED_MK1_CONVEYOR_MODULES if n != name}
        result = run_fixed_mk1_universal_conveyor(
            _make_selection_result(),
            source_video_path=SOURCE_VIDEO,
            job_metadata=JOB_META,
            module_registry=partial_registry,
        )
        assert result["status"] == CONVEYOR_STATUS_FAILED


# ===========================================================================
# 5–6: Missing required module
# ===========================================================================


def test_missing_required_module_returns_conveyor_failed():
    result = run_fixed_mk1_universal_conveyor(
        _make_selection_result(),
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry={},
    )
    assert result["status"] == CONVEYOR_STATUS_FAILED


def test_missing_module_error_lists_missing_names():
    partial = {
        "render_clip_v1": _DummyPassModule("render_clip_v1"),
    }
    result = run_fixed_mk1_universal_conveyor(
        _make_selection_result(),
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=partial,
    )
    assert result["status"] == CONVEYOR_STATUS_FAILED
    missing = result.get("missing_modules", [])
    assert "platform_safe_format_v1" in missing
    assert "intelligent_captions_v1" in missing
    assert "validation_v1" in missing
    assert "metadata_writer_v1" in missing


# ===========================================================================
# 7–8: One and multiple candidates
# ===========================================================================


def test_one_candidate_runs_all_five_dummy_modules():
    sr = _make_selection_result([_make_candidate("cand_a")])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert result["status"] == CONVEYOR_STATUS_COMPLETE
    assert len(result["clip_results"]) == 1
    clip = result["clip_results"][0]
    assert clip["status"] == CLIP_STATUS_PASS
    assert len(clip["module_results"]) == 5
    module_names = [r["module_name"] for r in clip["module_results"]]
    assert module_names == FIXED_MK1_CONVEYOR_MODULES


def test_multiple_candidates_each_run_five_modules():
    candidates = [_make_candidate(f"cand_{i}", rank=i) for i in range(1, 4)]
    sr = _make_selection_result(candidates)
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert result["status"] == CONVEYOR_STATUS_COMPLETE
    assert len(result["clip_results"]) == 3
    for clip in result["clip_results"]:
        assert clip["status"] == CLIP_STATUS_PASS
        assert len(clip["module_results"]) == 5


# ===========================================================================
# 9–12: Input/output path forwarding
# ===========================================================================


def test_first_module_receives_source_video_path():
    sr = _make_selection_result([_make_candidate("cand_a")])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    clip = result["clip_results"][0]
    first_result = clip["module_results"][0]
    assert first_result["input_path"] == SOURCE_VIDEO


def test_output_path_forwarded_between_modules():
    sr = _make_selection_result([_make_candidate("cand_a")])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    clip = result["clip_results"][0]
    module_results = clip["module_results"]
    for i in range(1, len(module_results)):
        prev_output = module_results[i - 1]["output_path"]
        curr_input = module_results[i]["input_path"]
        assert curr_input == prev_output


def test_per_clip_module_results_are_in_order():
    sr = _make_selection_result([_make_candidate("cand_a")])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    clip = result["clip_results"][0]
    names = [r["module_name"] for r in clip["module_results"]]
    assert names == FIXED_MK1_CONVEYOR_MODULES


def test_per_clip_final_output_path_is_last_module_output():
    sr = _make_selection_result([_make_candidate("cand_a")])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    clip = result["clip_results"][0]
    expected = clip["module_results"][-1]["output_path"]
    assert clip["final_output_path"] == expected


# ===========================================================================
# 13–17: Per-clip failure
# ===========================================================================


def test_per_clip_failure_is_recorded_when_module_fails():
    registry = _make_registry_with_fail("render_clip_v1", reason="render_failed")
    sr = _make_selection_result([_make_candidate("cand_a")])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=registry,
    )
    assert result["status"] == CONVEYOR_STATUS_COMPLETE
    clip = result["clip_results"][0]
    assert clip["status"] == CLIP_STATUS_FAIL


def test_failed_clip_does_not_crash_conveyor():
    registry = _make_registry_with_fail("render_clip_v1")
    sr = _make_selection_result([_make_candidate("cand_fail")])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=registry,
    )
    # Conveyor itself completes cleanly even though the clip failed
    assert result["status"] == CONVEYOR_STATUS_COMPLETE


class _FailOnceModule(PostProcessingModule):
    """Fails on the first call, passes on all subsequent calls."""

    def __init__(self, name: str, reason: str = "first_call_failed", version: str = "1.0"):
        self.module_name = name
        self.module_version = version
        self._reason = reason
        self._call_count = 0

    def run(self, context, *, input_path=None, config=None):
        self._call_count += 1
        if self._call_count == 1:
            return make_module_fail_result(
                self.module_name,
                self.module_version,
                self._reason,
                input_path=input_path,
            )
        out = f"{input_path}.{self.module_name}.out" if input_path else f"/tmp/{self.module_name}.out"
        return make_module_pass_result(
            self.module_name,
            self.module_version,
            input_path=input_path,
            output_path=out,
        )


def test_conveyor_continues_after_one_candidate_fails():
    registry: dict[str, Any] = {}
    # render_clip_v1 fails for the first candidate, passes for the second
    registry["render_clip_v1"] = _FailOnceModule("render_clip_v1", reason="render_failed")
    for name in FIXED_MK1_CONVEYOR_MODULES[1:]:
        registry[name] = _DummyPassModule(name)

    candidates = [_make_candidate("cand_fail", rank=1), _make_candidate("cand_pass", rank=2)]
    sr = _make_selection_result(candidates)
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=registry,
    )
    assert result["status"] == CONVEYOR_STATUS_COMPLETE
    statuses = [c["status"] for c in result["clip_results"]]
    assert CLIP_STATUS_FAIL in statuses
    assert CLIP_STATUS_PASS in statuses


def test_failed_module_name_is_recorded():
    registry = _make_registry_with_fail("intelligent_captions_v1")
    sr = _make_selection_result([_make_candidate("cand_a")])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=registry,
    )
    clip = result["clip_results"][0]
    assert clip["failed_module"] == "intelligent_captions_v1"


def test_failure_reason_is_recorded():
    registry = _make_registry_with_fail("validation_v1", reason="validation_failed_reason")
    sr = _make_selection_result([_make_candidate("cand_a")])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=registry,
    )
    clip = result["clip_results"][0]
    assert clip["failure_reason"] is not None
    assert "validation_failed_reason" in clip["failure_reason"]


# ===========================================================================
# 18–20: Summary counts
# ===========================================================================


def test_summary_counts_clips_attempted():
    candidates = [_make_candidate(f"c{i}", rank=i) for i in range(1, 4)]
    sr = _make_selection_result(candidates)
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert result["summary"]["clips_attempted"] == 3


def test_summary_counts_clips_passed():
    candidates = [_make_candidate(f"c{i}", rank=i) for i in range(1, 4)]
    sr = _make_selection_result(candidates)
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert result["summary"]["clips_passed"] == 3
    assert result["summary"]["clips_failed"] == 0


def test_summary_counts_clips_failed():
    # Use _FailOnceModule so first candidate fails, second passes
    registry: dict[str, Any] = _make_pass_registry()
    registry["render_clip_v1"] = _FailOnceModule("render_clip_v1", reason="render_failed")
    candidates = [
        _make_candidate("cand_fail", rank=1),
        _make_candidate("cand_pass", rank=2),
    ]
    sr = _make_selection_result(candidates)
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=registry,
    )
    assert result["summary"]["clips_failed"] == 1
    assert result["summary"]["clips_passed"] == 1


# ===========================================================================
# 21–22: Zero selected candidates
# ===========================================================================


def test_zero_selected_candidates_returns_conveyor_complete():
    sr = _make_selection_result([])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert result["status"] == CONVEYOR_STATUS_COMPLETE
    assert result["clip_results"] == []
    assert result["summary"]["clips_attempted"] == 0


def test_zero_selected_candidates_adds_warning():
    sr = _make_selection_result([])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert "zero_selected_candidates" in result["warnings"]


# ===========================================================================
# 23–25: Invalid selection result
# ===========================================================================


def test_invalid_selection_result_not_dict_fails_cleanly():
    result = run_fixed_mk1_universal_conveyor(
        "not_a_dict",
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert result["status"] == CONVEYOR_STATUS_FAILED


def test_missing_selected_candidates_key_fails_cleanly():
    result = run_fixed_mk1_universal_conveyor(
        {"job_id": "job_test_001"},
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert result["status"] == CONVEYOR_STATUS_FAILED


def test_non_list_selected_candidates_fails_cleanly():
    result = run_fixed_mk1_universal_conveyor(
        {"job_id": "job_test_001", "selected_candidates": "not_a_list"},
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert result["status"] == CONVEYOR_STATUS_FAILED


# ===========================================================================
# 26: Non-dict candidate entry
# ===========================================================================


def test_non_dict_candidate_entry_produces_controlled_clip_failure():
    sr = {
        "job_id": "job_test_001",
        "selected_candidates": ["not_a_dict"],
    }
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    # Conveyor itself must not crash
    assert result["status"] == CONVEYOR_STATUS_COMPLETE
    assert len(result["clip_results"]) == 1
    assert result["clip_results"][0]["status"] == CLIP_STATUS_FAIL


# ===========================================================================
# 27–29: Job ID resolution
# ===========================================================================


def test_job_id_resolves_from_job_metadata_first():
    sr = _make_selection_result(job_id="sr_job_id")
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata={"job_id": "meta_job_id"},
        module_registry=_make_pass_registry(),
    )
    assert result["job_id"] == "meta_job_id"


def test_job_id_falls_back_to_selection_result():
    sr = _make_selection_result(job_id="fallback_job_id")
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata={},
        module_registry=_make_pass_registry(),
    )
    assert result["job_id"] == "fallback_job_id"


def test_missing_job_id_fails_cleanly():
    sr = {
        "selected_candidates": [],
        # no job_id key
    }
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata={},
        module_registry=_make_pass_registry(),
    )
    assert result["status"] == CONVEYOR_STATUS_FAILED


# ===========================================================================
# 30–31: Deterministic clip IDs
# ===========================================================================


def test_clip_ids_are_stable_across_repeated_runs():
    sr = _make_selection_result([_make_candidate("cand_x", rank=1)])
    result1 = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    result2 = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert result1["clip_results"][0]["clip_id"] == result2["clip_results"][0]["clip_id"]


def test_clip_id_includes_candidate_id():
    sr = _make_selection_result([_make_candidate("cand_abc", rank=1)])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    clip_id = result["clip_results"][0]["clip_id"]
    assert "cand_abc" in clip_id


# ===========================================================================
# 32–37: Module context contents
# ===========================================================================


class _ContextCaptureModule(PostProcessingModule):
    """Stores the context it receives so tests can inspect it."""
    module_name = "render_clip_v1"
    module_version = "1.0"
    captured: dict[str, Any] | None = None

    def run(self, context, *, input_path=None, config=None):
        _ContextCaptureModule.captured = copy.deepcopy(context)
        return make_module_pass_result(
            self.module_name,
            self.module_version,
            input_path=input_path,
            output_path=f"{input_path}.render.out" if input_path else "/tmp/render.out",
        )


def _make_context_capture_registry() -> dict[str, Any]:
    registry: dict[str, Any] = _make_pass_registry()
    registry["render_clip_v1"] = _ContextCaptureModule()
    return registry


def test_module_context_includes_job_id():
    sr = _make_selection_result([_make_candidate("cand_ctx")])
    _ContextCaptureModule.captured = None
    run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_context_capture_registry(),
    )
    assert _ContextCaptureModule.captured is not None
    assert _ContextCaptureModule.captured["job_id"] == "job_test_001"


def test_module_context_includes_candidate_id():
    sr = _make_selection_result([_make_candidate("cand_ctx")])
    _ContextCaptureModule.captured = None
    run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_context_capture_registry(),
    )
    assert _ContextCaptureModule.captured["candidate_id"] == "cand_ctx"


def test_module_context_includes_source_video_path():
    sr = _make_selection_result([_make_candidate("cand_ctx")])
    _ContextCaptureModule.captured = None
    run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_context_capture_registry(),
    )
    assert _ContextCaptureModule.captured["source_video_path"] == SOURCE_VIDEO


def test_module_context_includes_selected_candidate():
    cand = _make_candidate("cand_ctx")
    sr = _make_selection_result([cand])
    _ContextCaptureModule.captured = None
    run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_context_capture_registry(),
    )
    ctx_cand = _ContextCaptureModule.captured["selected_candidate"]
    assert ctx_cand["candidate_id"] == "cand_ctx"


def test_module_context_includes_selection_result():
    sr = _make_selection_result([_make_candidate("cand_ctx")])
    _ContextCaptureModule.captured = None
    run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_context_capture_registry(),
    )
    # selection_result is a deep copy placed in context
    assert "selected_candidates" in _ContextCaptureModule.captured["selection_result"]


def test_module_context_includes_configured_directories():
    sr = _make_selection_result([_make_candidate("cand_ctx")])
    _ContextCaptureModule.captured = None
    dirs = {
        "post_processing_root": "/tmp/pp_root",
        "clips": "/tmp/pp_root/clips",
        "metadata": "/tmp/pp_root/metadata",
        "tmp": "/tmp/pp_root/tmp",
    }
    run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        directories=dirs,
        module_registry=_make_context_capture_registry(),
    )
    ctx = _ContextCaptureModule.captured
    assert ctx["clip_dir"] == "/tmp/pp_root/clips"
    assert ctx["metadata_dir"] == "/tmp/pp_root/metadata"
    assert ctx["tmp_dir"] == "/tmp/pp_root/tmp"


# ===========================================================================
# 38: Conveyor does not mutate selection_result
# ===========================================================================


def test_conveyor_does_not_mutate_selection_result():
    sr = _make_selection_result([_make_candidate("cand_a")])
    original = copy.deepcopy(sr)
    run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert sr == original


# ===========================================================================
# 39: JSON serializability
# ===========================================================================


def test_conveyor_result_is_json_serializable():
    sr = _make_selection_result([_make_candidate("cand_a")])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    serialized = json.dumps(result)
    assert isinstance(serialized, str)
    parsed = json.loads(serialized)
    assert parsed["status"] == CONVEYOR_STATUS_COMPLETE


def test_conveyor_failed_result_is_json_serializable():
    result = run_fixed_mk1_universal_conveyor(
        "not_a_dict",
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    serialized = json.dumps(result)
    assert isinstance(serialized, str)


# ===========================================================================
# 40–43: No forbidden imports
# ===========================================================================


def test_conveyor_does_not_import_ffmpeg_rendering_code():
    import post_processing_conveyor as conveyor_mod
    # ffmpeg / subprocess wrappers should not be imported at module level
    forbidden = {"ffmpeg", "subprocess", "clip_video"}
    module_names = set(vars(conveyor_mod).keys())
    assert forbidden.isdisjoint(module_names)


def test_conveyor_does_not_import_captioning_code():
    import post_processing_conveyor as conveyor_mod
    module_names = set(vars(conveyor_mod).keys())
    assert "captions" not in module_names
    assert "subtitles" not in module_names


def test_conveyor_does_not_import_ai_service_code():
    import post_processing_conveyor as conveyor_mod
    module_names = set(vars(conveyor_mod).keys())
    assert "ai_service" not in module_names
    assert "model_client" not in module_names


def test_conveyor_does_not_import_output_funnel_code():
    import post_processing_conveyor as conveyor_mod
    module_names = set(vars(conveyor_mod).keys())
    assert "output_funnel" not in module_names
    assert "register_funnel" not in module_names


# ===========================================================================
# 44–46: Prompt 14 / 15 / 16 regression (import-only checks)
# ===========================================================================


def test_prompt14_module_still_importable():
    import post_processing_mk1  # noqa: F401


def test_prompt15_module_still_importable():
    import selection_gate_v1  # noqa: F401


def test_prompt16_module_still_importable():
    import post_processing_modules  # noqa: F401


# ===========================================================================
# Extra: schema version in result
# ===========================================================================


def test_result_schema_version_is_correct():
    sr = _make_selection_result([_make_candidate()])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert result["schema_version"] == CONVEYOR_SCHEMA_VERSION


def test_failed_result_schema_version_is_correct():
    result = run_fixed_mk1_universal_conveyor(
        "bad_input",
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert result["schema_version"] == CONVEYOR_SCHEMA_VERSION


# ===========================================================================
# Extra: required_modules field always mirrors FIXED_MK1_CONVEYOR_MODULES
# ===========================================================================


def test_required_modules_field_mirrors_constant():
    sr = _make_selection_result([_make_candidate()])
    result = run_fixed_mk1_universal_conveyor(
        sr,
        source_video_path=SOURCE_VIDEO,
        job_metadata=JOB_META,
        module_registry=_make_pass_registry(),
    )
    assert result["required_modules"] == FIXED_MK1_CONVEYOR_MODULES
