"""Post-processing module framework — focused tests (Prompt 16).

Verifies the universal module framework:
- standard module result contract (PASS / FAIL / SKIPPED)
- required fields and defaults
- module result validation
- module context construction
- base module class interface
- chain helper ordering, path passing, failure handling
- controlled exception conversion
- no imports of rendering / captioning / AI / output-funnel code

All tests use dummy module functions/classes.
No real video files, ffmpeg, AI service, or transcript data required.
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
    MODULE_CHAIN_RESULT_SCHEMA_VERSION,
    MODULE_RESULT_SCHEMA_VERSION,
    MODULE_STATUS_FAIL,
    MODULE_STATUS_PASS,
    MODULE_STATUS_SKIPPED,
    VALID_MODULE_STATUSES,
    CHAIN_STATUS_FAIL,
    CHAIN_STATUS_PASS,
    ModuleResultValidationError,
    PostProcessingModule,
    make_module_context,
    make_module_fail_result,
    make_module_pass_result,
    make_module_skipped_result,
    run_module_chain,
    validate_module_result,
)


# ---------------------------------------------------------------------------
# Dummy module helpers for tests
# ---------------------------------------------------------------------------


class _PassModule(PostProcessingModule):
    """Dummy module that always passes and echoes input_path → output_path."""

    module_name = "dummy_pass_module_v1"
    module_version = "1.0"

    def __init__(self, output_path: str | None = "/tmp/dummy_output.mp4"):
        self._output_path = output_path

    def run(self, context, *, input_path=None, config=None):
        return make_module_pass_result(
            self.module_name,
            self.module_version,
            input_path=input_path,
            output_path=self._output_path,
        )


class _FailModule(PostProcessingModule):
    """Dummy module that always fails."""

    module_name = "dummy_fail_module_v1"
    module_version = "1.0"

    def run(self, context, *, input_path=None, config=None):
        return make_module_fail_result(
            self.module_name,
            self.module_version,
            error_reason="intentional_test_failure",
            input_path=input_path,
        )


class _SkipModule(PostProcessingModule):
    """Dummy module that always skips."""

    module_name = "dummy_skip_module_v1"
    module_version = "1.0"

    def run(self, context, *, input_path=None, config=None):
        return make_module_skipped_result(
            self.module_name,
            self.module_version,
            reason="test_skip",
        )


class _RaisingModule(PostProcessingModule):
    """Dummy module that raises an exception."""

    module_name = "dummy_raising_module_v1"
    module_version = "1.0"

    def run(self, context, *, input_path=None, config=None):
        raise RuntimeError("unexpected_crash_in_module")


def _simple_context(**kwargs) -> dict[str, Any]:
    return make_module_context(job_id="test_job_001", **kwargs)


# ---------------------------------------------------------------------------
# 1. PASS result has required fields
# ---------------------------------------------------------------------------


def test_pass_result_has_all_required_fields():
    result = make_module_pass_result("mod_v1", "1.0")
    for field in (
        "schema_version",
        "module_name",
        "module_version",
        "status",
        "input_path",
        "output_path",
        "config",
        "error_reason",
        "warnings",
        "metadata",
    ):
        assert field in result, f"PASS result missing field: {field}"


def test_pass_result_status_is_pass():
    result = make_module_pass_result("mod_v1", "1.0")
    assert result["status"] == MODULE_STATUS_PASS


def test_pass_result_schema_version():
    result = make_module_pass_result("mod_v1", "1.0")
    assert result["schema_version"] == MODULE_RESULT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 2. FAIL result has required fields
# ---------------------------------------------------------------------------


def test_fail_result_has_all_required_fields():
    result = make_module_fail_result("mod_v1", "1.0", "something went wrong")
    for field in (
        "schema_version",
        "module_name",
        "module_version",
        "status",
        "input_path",
        "output_path",
        "config",
        "error_reason",
        "warnings",
        "metadata",
    ):
        assert field in result, f"FAIL result missing field: {field}"


def test_fail_result_status_is_fail():
    result = make_module_fail_result("mod_v1", "1.0", "oops")
    assert result["status"] == MODULE_STATUS_FAIL


# ---------------------------------------------------------------------------
# 3. SKIPPED result has required fields
# ---------------------------------------------------------------------------


def test_skipped_result_has_all_required_fields():
    result = make_module_skipped_result("mod_v1", "1.0")
    for field in (
        "schema_version",
        "module_name",
        "module_version",
        "status",
        "input_path",
        "output_path",
        "config",
        "error_reason",
        "warnings",
        "metadata",
    ):
        assert field in result, f"SKIPPED result missing field: {field}"


def test_skipped_result_status_is_skipped():
    result = make_module_skipped_result("mod_v1", "1.0")
    assert result["status"] == MODULE_STATUS_SKIPPED


# ---------------------------------------------------------------------------
# 4. Result status must be one of PASS, FAIL, SKIPPED
# ---------------------------------------------------------------------------


def test_valid_statuses_are_pass_fail_skipped():
    assert MODULE_STATUS_PASS in VALID_MODULE_STATUSES
    assert MODULE_STATUS_FAIL in VALID_MODULE_STATUSES
    assert MODULE_STATUS_SKIPPED in VALID_MODULE_STATUSES
    assert len(VALID_MODULE_STATUSES) == 3


def test_pass_fail_skipped_constants_are_correct_strings():
    assert MODULE_STATUS_PASS == "PASS"
    assert MODULE_STATUS_FAIL == "FAIL"
    assert MODULE_STATUS_SKIPPED == "SKIPPED"


# ---------------------------------------------------------------------------
# 5. config defaults to {}
# ---------------------------------------------------------------------------


def test_pass_result_config_defaults_to_empty_dict():
    result = make_module_pass_result("mod_v1", "1.0")
    assert result["config"] == {}


def test_fail_result_config_defaults_to_empty_dict():
    result = make_module_fail_result("mod_v1", "1.0", "err")
    assert result["config"] == {}


def test_skipped_result_config_defaults_to_empty_dict():
    result = make_module_skipped_result("mod_v1", "1.0")
    assert result["config"] == {}


# ---------------------------------------------------------------------------
# 6. warnings defaults to []
# ---------------------------------------------------------------------------


def test_pass_result_warnings_defaults_to_empty_list():
    result = make_module_pass_result("mod_v1", "1.0")
    assert result["warnings"] == []


def test_fail_result_warnings_defaults_to_empty_list():
    result = make_module_fail_result("mod_v1", "1.0", "err")
    assert result["warnings"] == []


# ---------------------------------------------------------------------------
# 7. metadata defaults to {}
# ---------------------------------------------------------------------------


def test_pass_result_metadata_defaults_to_empty_dict():
    result = make_module_pass_result("mod_v1", "1.0")
    assert result["metadata"] == {}


def test_fail_result_metadata_defaults_to_empty_dict():
    result = make_module_fail_result("mod_v1", "1.0", "err")
    assert result["metadata"] == {}


# ---------------------------------------------------------------------------
# 8. FAIL result includes error_reason
# ---------------------------------------------------------------------------


def test_fail_result_error_reason_is_set():
    result = make_module_fail_result("mod_v1", "1.0", "disk_full")
    assert result["error_reason"] == "disk_full"


def test_pass_result_error_reason_is_none():
    result = make_module_pass_result("mod_v1", "1.0")
    assert result["error_reason"] is None


def test_skipped_result_error_reason_is_none():
    result = make_module_skipped_result("mod_v1", "1.0")
    assert result["error_reason"] is None


# ---------------------------------------------------------------------------
# 9. PASS result can include input_path and output_path
# ---------------------------------------------------------------------------


def test_pass_result_stores_input_and_output_paths():
    result = make_module_pass_result(
        "mod_v1", "1.0",
        input_path="/input/clip.mp4",
        output_path="/output/clip_formatted.mp4",
    )
    assert result["input_path"] == "/input/clip.mp4"
    assert result["output_path"] == "/output/clip_formatted.mp4"


def test_pass_result_input_output_paths_default_to_none():
    result = make_module_pass_result("mod_v1", "1.0")
    assert result["input_path"] is None
    assert result["output_path"] is None


# ---------------------------------------------------------------------------
# 10. SKIPPED result can include warning/reason metadata
# ---------------------------------------------------------------------------


def test_skipped_result_reason_stored_in_metadata():
    result = make_module_skipped_result("mod_v1", "1.0", reason="not_applicable")
    assert result["metadata"]["skip_reason"] == "not_applicable"


def test_skipped_result_warnings_can_be_set():
    result = make_module_skipped_result(
        "mod_v1", "1.0", warnings=["platform_not_supported"]
    )
    assert "platform_not_supported" in result["warnings"]


# ---------------------------------------------------------------------------
# 11. Module result is JSON serializable
# ---------------------------------------------------------------------------


def test_pass_result_is_json_serializable():
    result = make_module_pass_result(
        "mod_v1", "1.0",
        input_path="/in.mp4", output_path="/out.mp4",
        config={"key": "val"}, warnings=["w1"], metadata={"m": 1},
    )
    serialized = json.dumps(result)
    loaded = json.loads(serialized)
    assert loaded["status"] == MODULE_STATUS_PASS


def test_fail_result_is_json_serializable():
    result = make_module_fail_result("mod_v1", "1.0", "something_broke")
    serialized = json.dumps(result)
    loaded = json.loads(serialized)
    assert loaded["error_reason"] == "something_broke"


def test_skipped_result_is_json_serializable():
    result = make_module_skipped_result("mod_v1", "1.0", reason="skip")
    serialized = json.dumps(result)
    loaded = json.loads(serialized)
    assert loaded["status"] == MODULE_STATUS_SKIPPED


# ---------------------------------------------------------------------------
# 12. Module context can be constructed with job/candidate/path/config data
# ---------------------------------------------------------------------------


def test_module_context_has_required_job_id():
    ctx = make_module_context(job_id="job_abc")
    assert ctx["job_id"] == "job_abc"


def test_module_context_stores_candidate_id():
    ctx = make_module_context(job_id="j1", candidate_id="cand_xyz")
    assert ctx["candidate_id"] == "cand_xyz"


def test_module_context_stores_all_path_fields():
    ctx = make_module_context(
        job_id="j1",
        source_video_path="/src/video.mp4",
        working_dir="/work",
        clip_dir="/work/clips",
        metadata_dir="/work/metadata",
        tmp_dir="/work/tmp",
    )
    assert ctx["source_video_path"] == "/src/video.mp4"
    assert ctx["working_dir"] == "/work"
    assert ctx["clip_dir"] == "/work/clips"
    assert ctx["metadata_dir"] == "/work/metadata"
    assert ctx["tmp_dir"] == "/work/tmp"


def test_module_context_stores_config():
    ctx = make_module_context(job_id="j1", config={"max_clips": 5})
    assert ctx["config"]["max_clips"] == 5


def test_module_context_stores_selection_result():
    selection = {"status": "SELECTION_COMPLETE", "selected_candidates": []}
    ctx = make_module_context(job_id="j1", selection_result=selection)
    assert ctx["selection_result"]["status"] == "SELECTION_COMPLETE"


def test_module_context_stores_selected_candidate():
    cand = {"candidate_id": "cand_001", "start_sec": 10.0}
    ctx = make_module_context(job_id="j1", selected_candidate=cand)
    assert ctx["selected_candidate"]["candidate_id"] == "cand_001"


def test_module_context_module_results_defaults_to_empty_list():
    ctx = make_module_context(job_id="j1")
    assert ctx["module_results"] == []


# ---------------------------------------------------------------------------
# 13. Module context does not require transcript discovery data
# ---------------------------------------------------------------------------


def test_module_context_can_be_created_without_transcript_data():
    ctx = make_module_context(job_id="j1")
    assert "transcript" not in ctx
    assert "sections" not in ctx
    assert "section_discovery" not in ctx


# ---------------------------------------------------------------------------
# 14. Module context does not require AI service data
# ---------------------------------------------------------------------------


def test_module_context_has_no_ai_service_fields():
    ctx = make_module_context(job_id="j1")
    for forbidden in ("ai_client", "ai_service", "openai", "ollama", "llm"):
        assert forbidden not in ctx, f"context must not contain {forbidden!r}"


# ---------------------------------------------------------------------------
# 15. Chain runs modules in order
# ---------------------------------------------------------------------------


def test_chain_runs_all_modules():
    calls: list[str] = []

    class Tracker(PostProcessingModule):
        module_name = "tracker_v1"
        module_version = "1.0"

        def __init__(self, name):
            self._name = name

        def run(self, context, *, input_path=None, config=None):
            calls.append(self._name)
            return make_module_pass_result(self.module_name, self.module_version)

    chain_result = run_module_chain(
        [Tracker("first"), Tracker("second"), Tracker("third")],
        _simple_context(),
    )
    assert chain_result["status"] == CHAIN_STATUS_PASS
    assert calls == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# 16. Chain passes previous output path to next module
# ---------------------------------------------------------------------------


def test_chain_passes_output_path_to_next_module():
    received_inputs: list[str | None] = []

    class PathEchoModule(PostProcessingModule):
        module_name = "path_echo_v1"
        module_version = "1.0"

        def __init__(self, output_path):
            self._output_path = output_path

        def run(self, context, *, input_path=None, config=None):
            received_inputs.append(input_path)
            return make_module_pass_result(
                self.module_name, self.module_version,
                input_path=input_path,
                output_path=self._output_path,
            )

    run_module_chain(
        [
            PathEchoModule("/step1_output.mp4"),
            PathEchoModule("/step2_output.mp4"),
        ],
        _simple_context(),
        initial_input_path="/initial.mp4",
    )

    assert received_inputs[0] == "/initial.mp4"
    assert received_inputs[1] == "/step1_output.mp4"


# ---------------------------------------------------------------------------
# 17. Chain preserves all module results
# ---------------------------------------------------------------------------


def test_chain_preserves_all_module_results():
    mods = [_PassModule(f"/out_{i}.mp4") for i in range(3)]
    result = run_module_chain(mods, _simple_context())
    assert len(result["module_results"]) == 3


def test_chain_result_module_results_are_in_order():
    names: list[str] = []

    class NamedModule(PostProcessingModule):
        module_version = "1.0"

        def __init__(self, name):
            self.module_name = name

        def run(self, context, *, input_path=None, config=None):
            return make_module_pass_result(self.module_name, self.module_version)

    modules = [NamedModule(f"mod_{i}") for i in range(4)]
    result = run_module_chain(modules, _simple_context())
    returned_names = [r["module_name"] for r in result["module_results"]]
    assert returned_names == [f"mod_{i}" for i in range(4)]


def test_chain_passes_prior_module_results_to_later_modules():
    observed_lengths: list[int] = []

    class ObservingModule(PostProcessingModule):
        module_version = "1.0"

        def __init__(self, name: str):
            self.module_name = name

        def run(self, context, *, input_path=None, config=None):
            observed_lengths.append(len(context.get("module_results") or []))
            return make_module_pass_result(self.module_name, self.module_version)

    result = run_module_chain(
        [ObservingModule("first"), ObservingModule("second"), ObservingModule("third")],
        _simple_context(),
    )

    assert result["status"] == CHAIN_STATUS_PASS
    assert observed_lengths == [0, 1, 2]


# ---------------------------------------------------------------------------
# 18. Chain final output path equals last passing module output path
# ---------------------------------------------------------------------------


def test_chain_final_output_path_is_last_module_output():
    mods = [
        _PassModule("/step1.mp4"),
        _PassModule("/step2.mp4"),
        _PassModule("/final.mp4"),
    ]
    result = run_module_chain(mods, _simple_context())
    assert result["final_output_path"] == "/final.mp4"


def test_chain_final_output_path_is_none_when_no_module_sets_output():
    mods = [_PassModule(None)]
    result = run_module_chain(mods, _simple_context())
    assert result["final_output_path"] is None


# ---------------------------------------------------------------------------
# 19. Chain stops on required module failure
# ---------------------------------------------------------------------------


def test_chain_stops_after_fail_module():
    call_count = [0]

    class CountingPass(PostProcessingModule):
        module_name = "counting_pass_v1"
        module_version = "1.0"

        def run(self, context, *, input_path=None, config=None):
            call_count[0] += 1
            return make_module_pass_result(self.module_name, self.module_version)

    result = run_module_chain(
        [_FailModule(), CountingPass(), CountingPass()],
        _simple_context(),
    )
    assert result["status"] == CHAIN_STATUS_FAIL
    assert call_count[0] == 0, "Modules after the failing module must not be called"


def test_chain_fail_result_has_correct_status():
    result = run_module_chain([_FailModule()], _simple_context())
    assert result["status"] == CHAIN_STATUS_FAIL


# ---------------------------------------------------------------------------
# 20. Chain records failed module name
# ---------------------------------------------------------------------------


def test_chain_records_failed_module_name():
    result = run_module_chain([_FailModule()], _simple_context())
    assert result["failed_module"] == _FailModule.module_name


def test_chain_failed_module_is_none_when_all_pass():
    result = run_module_chain([_PassModule()], _simple_context())
    assert result["failed_module"] is None


# ---------------------------------------------------------------------------
# 21. Chain records error reason
# ---------------------------------------------------------------------------


def test_chain_errors_list_non_empty_on_failure():
    result = run_module_chain([_FailModule()], _simple_context())
    assert len(result["errors"]) >= 1


def test_chain_error_entry_contains_reason():
    result = run_module_chain([_FailModule()], _simple_context())
    error = result["errors"][0]
    assert "reason" in error
    assert isinstance(error["reason"], str)


def test_chain_fail_result_error_reason_matches_module_error_reason():
    result = run_module_chain([_FailModule()], _simple_context())
    assert result["errors"][0]["reason"] == "intentional_test_failure"


# ---------------------------------------------------------------------------
# 22. Chain treats required SKIPPED as failure by default
# ---------------------------------------------------------------------------


def test_chain_treats_skipped_as_failure_by_default():
    result = run_module_chain([_SkipModule()], _simple_context())
    assert result["status"] == CHAIN_STATUS_FAIL


def test_chain_skipped_fail_error_reason_is_required_module_skipped():
    result = run_module_chain([_SkipModule()], _simple_context())
    assert result["errors"][0]["reason"] == "required_module_skipped"


# ---------------------------------------------------------------------------
# 23. Chain can allow optional skipped module if implemented
# ---------------------------------------------------------------------------


def test_chain_allows_skipped_module_when_allow_skipped_true():
    result = run_module_chain(
        [_SkipModule()],
        _simple_context(),
        allow_skipped=True,
    )
    assert result["status"] == CHAIN_STATUS_PASS


def test_chain_continues_after_allowed_skip():
    call_count = [0]

    class AfterSkip(PostProcessingModule):
        module_name = "after_skip_v1"
        module_version = "1.0"

        def run(self, context, *, input_path=None, config=None):
            call_count[0] += 1
            return make_module_pass_result(self.module_name, self.module_version)

    run_module_chain(
        [_SkipModule(), AfterSkip()],
        _simple_context(),
        allow_skipped=True,
    )
    assert call_count[0] == 1, "Module after allowed skip must be executed"


# ---------------------------------------------------------------------------
# 24. Chain rejects invalid module result status
# ---------------------------------------------------------------------------


def test_chain_rejects_invalid_status_string():
    def bad_module(context, *, input_path=None, config=None):
        return {
            "schema_version": MODULE_RESULT_SCHEMA_VERSION,
            "module_name": "bad_v1",
            "module_version": "1.0",
            "status": "NOT_A_VALID_STATUS",
            "input_path": None,
            "output_path": None,
            "config": {},
            "error_reason": None,
            "warnings": [],
            "metadata": {},
        }

    bad_module.module_name = "bad_v1"
    result = run_module_chain([bad_module], _simple_context())
    assert result["status"] == CHAIN_STATUS_FAIL


# ---------------------------------------------------------------------------
# 25. Chain converts expected module exception into controlled FAIL result
# ---------------------------------------------------------------------------


def test_chain_converts_exception_to_fail_result():
    result = run_module_chain([_RaisingModule()], _simple_context())
    assert result["status"] == CHAIN_STATUS_FAIL


def test_chain_exception_error_reason_contains_exception_info():
    result = run_module_chain([_RaisingModule()], _simple_context())
    error = result["errors"][0]
    assert "RuntimeError" in error["reason"] or "unexpected_crash_in_module" in error["reason"]


def test_chain_exception_is_recorded_as_module_fail_result():
    result = run_module_chain([_RaisingModule()], _simple_context())
    # The failing module result should appear in module_results with FAIL status
    fail_results = [
        r for r in result["module_results"] if r["status"] == MODULE_STATUS_FAIL
    ]
    assert len(fail_results) == 1


# ---------------------------------------------------------------------------
# 26. Chain preserves warnings from module results
# ---------------------------------------------------------------------------


def test_chain_accumulates_warnings_from_all_modules():
    class WarnModule(PostProcessingModule):
        module_name = "warn_mod_v1"
        module_version = "1.0"

        def __init__(self, warning: str):
            self._warning = warning

        def run(self, context, *, input_path=None, config=None):
            return make_module_pass_result(
                self.module_name,
                self.module_version,
                warnings=[self._warning],
            )

    result = run_module_chain(
        [WarnModule("warn_one"), WarnModule("warn_two")],
        _simple_context(),
    )
    assert "warn_one" in result["warnings"]
    assert "warn_two" in result["warnings"]


# ---------------------------------------------------------------------------
# 27. Chain returns PASS when all required modules pass
# ---------------------------------------------------------------------------


def test_chain_returns_pass_when_all_modules_pass():
    mods = [_PassModule() for _ in range(4)]
    result = run_module_chain(mods, _simple_context())
    assert result["status"] == CHAIN_STATUS_PASS


def test_chain_pass_result_has_no_errors():
    mods = [_PassModule("/out.mp4")]
    result = run_module_chain(mods, _simple_context())
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# 28. Chain returns FAIL when a required module fails
# ---------------------------------------------------------------------------


def test_chain_returns_fail_when_one_module_fails():
    result = run_module_chain(
        [_PassModule(), _FailModule(), _PassModule()],
        _simple_context(),
    )
    assert result["status"] == CHAIN_STATUS_FAIL


def test_chain_records_only_results_up_to_and_including_fail():
    result = run_module_chain(
        [_PassModule(), _FailModule(), _PassModule()],
        _simple_context(),
    )
    # Only two results should exist (pass + fail); third was never called
    assert len(result["module_results"]) == 2


# ---------------------------------------------------------------------------
# 29. Chain handles empty module list predictably
# ---------------------------------------------------------------------------


def test_empty_module_list_returns_pass():
    result = run_module_chain([], _simple_context())
    assert result["status"] == CHAIN_STATUS_PASS


def test_empty_module_list_has_empty_module_results():
    result = run_module_chain([], _simple_context())
    assert result["module_results"] == []


def test_empty_module_list_final_output_path_is_none():
    result = run_module_chain([], _simple_context())
    assert result["final_output_path"] is None


# ---------------------------------------------------------------------------
# 30. Chain does not mutate the input context
# ---------------------------------------------------------------------------


def test_chain_does_not_mutate_input_context():
    ctx = make_module_context(job_id="j1", config={"key": "original"})
    ctx_copy = copy.deepcopy(ctx)

    run_module_chain([_PassModule()], ctx)

    assert ctx == ctx_copy, "run_module_chain must not mutate the input context"


# ---------------------------------------------------------------------------
# 31-34. Framework does not import forbidden modules
# ---------------------------------------------------------------------------


def test_framework_does_not_import_rendering_code():
    import post_processing_modules as ppm

    forbidden = {"clip_video", "ffmpeg", "moviepy", "opencv"}
    module_attrs = set(vars(ppm).keys())
    for name in forbidden:
        assert name not in module_attrs, (
            f"post_processing_modules must not reference {name!r}"
        )


def test_framework_does_not_import_captioning_code():
    import post_processing_modules as ppm

    for name in ("intelligent_captions", "caption", "whisper", "transcribe"):
        assert name not in vars(ppm), (
            f"post_processing_modules must not reference {name!r}"
        )


def test_framework_does_not_import_ai_service_code():
    import post_processing_modules as ppm

    for name in ("ai_service_client", "ai_settings", "openai", "ollama", "llm"):
        assert name not in vars(ppm), (
            f"post_processing_modules must not reference {name!r}"
        )


def test_framework_does_not_import_output_funnel_code():
    import post_processing_modules as ppm

    for name in ("output_funnel", "publish", "upload", "distribution"):
        assert name not in vars(ppm), (
            f"post_processing_modules must not reference {name!r}"
        )


# ---------------------------------------------------------------------------
# 35. Prompt 14 tests still pass (integration regression check)
# ---------------------------------------------------------------------------


def test_prompt14_entrypoint_still_works(tmp_path):
    """Ensure post_processing_mk1 entrypoint is not broken."""
    import json as _json

    import processing_contracts as _c
    from post_processing_mk1 import STATUS_READY_FOR_SELECTION, run_post_processing_mk1

    video = tmp_path / "source.mp4"
    video.write_bytes(b"")
    pool = {
        "schema_version": _c.RAW_CANDIDATE_POOL_SCHEMA_VERSION,
        "job_id": "p16_test_job",
        "source_video_path": str(video),
        "transcript_path": "/fixture/t.json",
        "processing_version": _c.PROCESSING_VERSION,
        "funnel_id": "business",
        "created_at": "2026-06-30T12:00:00+00:00",
        "candidates": [],
        "diagnostics": {},
    }
    pool_path = tmp_path / "raw_candidate_pool.json"
    pool_path.write_text(_json.dumps(pool), encoding="utf-8")

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))
    assert result["status"] == STATUS_READY_FOR_SELECTION


# ---------------------------------------------------------------------------
# 36. Prompt 15 tests still pass (integration regression check)
# ---------------------------------------------------------------------------


def test_prompt15_selection_gate_still_works():
    """Ensure selection_gate_v1 is not broken."""
    import processing_contracts as _c
    from selection_gate_v1 import STATUS_SELECTION_COMPLETE, run_selection_gate_v1

    pool = {
        "schema_version": _c.RAW_CANDIDATE_POOL_SCHEMA_VERSION,
        "job_id": "p16_test_job",
        "source_video_path": "/fixture/source.mp4",
        "transcript_path": "/fixture/t.json",
        "processing_version": _c.PROCESSING_VERSION,
        "funnel_id": "business",
        "created_at": "2026-06-30T12:00:00+00:00",
        "candidates": [],
        "diagnostics": {},
    }
    result = run_selection_gate_v1(pool)
    assert result["status"] == STATUS_SELECTION_COMPLETE


# ---------------------------------------------------------------------------
# validate_module_result tests
# ---------------------------------------------------------------------------


def test_validate_passes_for_valid_pass_result():
    result = make_module_pass_result("mod_v1", "1.0")
    validate_module_result(result)  # must not raise


def test_validate_passes_for_valid_fail_result():
    result = make_module_fail_result("mod_v1", "1.0", "test_error")
    validate_module_result(result)  # must not raise


def test_validate_passes_for_valid_skipped_result():
    result = make_module_skipped_result("mod_v1", "1.0")
    validate_module_result(result)  # must not raise


def test_validate_raises_for_invalid_status():
    result = make_module_pass_result("mod_v1", "1.0")
    result["status"] = "INVALID"
    with pytest.raises(ModuleResultValidationError):
        validate_module_result(result)


def test_validate_raises_for_non_dict():
    with pytest.raises(ModuleResultValidationError):
        validate_module_result("not a dict")


def test_validate_raises_for_missing_required_field():
    result = make_module_pass_result("mod_v1", "1.0")
    del result["module_name"]
    with pytest.raises(ModuleResultValidationError):
        validate_module_result(result)


def test_validate_raises_for_fail_result_with_no_error_reason():
    result = make_module_fail_result("mod_v1", "1.0", "err")
    result["error_reason"] = None
    with pytest.raises(ModuleResultValidationError):
        validate_module_result(result)


def test_validate_raises_for_config_not_dict():
    result = make_module_pass_result("mod_v1", "1.0")
    result["config"] = "not_a_dict"
    with pytest.raises(ModuleResultValidationError):
        validate_module_result(result)


def test_validate_raises_for_warnings_not_list():
    result = make_module_pass_result("mod_v1", "1.0")
    result["warnings"] = "not_a_list"
    with pytest.raises(ModuleResultValidationError):
        validate_module_result(result)


# ---------------------------------------------------------------------------
# Base class tests
# ---------------------------------------------------------------------------


def test_base_module_run_raises_not_implemented():
    mod = PostProcessingModule()
    with pytest.raises(NotImplementedError):
        mod.run(_simple_context())


def test_subclass_can_override_module_name_and_version():
    class MyMod(PostProcessingModule):
        module_name = "my_custom_mod_v1"
        module_version = "2.3"

        def run(self, context, *, input_path=None, config=None):
            return make_module_pass_result(self.module_name, self.module_version)

    mod = MyMod()
    assert mod.module_name == "my_custom_mod_v1"
    assert mod.module_version == "2.3"


def test_chain_works_with_callable_function_module():
    """Chain should also accept plain callables, not just class instances."""

    def my_func_module(context, *, input_path=None, config=None):
        return make_module_pass_result("func_mod_v1", "1.0", output_path="/func_out.mp4")

    my_func_module.module_name = "func_mod_v1"

    result = run_module_chain([my_func_module], _simple_context())
    assert result["status"] == CHAIN_STATUS_PASS
    assert result["final_output_path"] == "/func_out.mp4"


def test_chain_schema_version():
    result = run_module_chain([], _simple_context())
    assert result["schema_version"] == MODULE_CHAIN_RESULT_SCHEMA_VERSION
