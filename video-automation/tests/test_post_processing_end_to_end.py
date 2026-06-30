"""Post-processing MK1 end-to-end integration tests (Prompt 24).

These tests run the real post-processing integration boundary:
raw_candidate_pool.json -> selection_gate_v1 -> fixed conveyor -> report ->
local output-funnel handoff artifact.

The conveyor modules are lightweight stubs so the tests stay focused on wiring
and failure semantics rather than ffmpeg runtime.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import processing_contracts as contracts  # noqa: E402
from post_processing_conveyor import FIXED_MK1_CONVEYOR_MODULES  # noqa: E402
from post_processing_mk1 import (  # noqa: E402
    OUTPUT_FUNNEL_HANDOFF_SCHEMA_VERSION,
    STATUS_NO_CANDIDATES_SELECTED,
    STATUS_POST_PROCESSING_COMPLETE,
    STATUS_POST_PROCESSING_FAILED,
    STATUS_POST_PROCESSING_PARTIAL,
    STATUS_READY_FOR_OUTPUT_FUNNEL,
    STATUS_READY_FOR_SELECTION,
    run_post_processing_mk1,
)
from post_processing_modules import (  # noqa: E402
    PostProcessingModule,
    make_module_fail_result,
    make_module_pass_result,
)


class _StubMk1Module(PostProcessingModule):
    """Small conveyor module stub that can pass or fail per candidate."""

    module_version = "1.0"

    def __init__(self, module_name: str, *, fail_candidate_ids: set[str] | None = None):
        self.module_name = module_name
        self._fail_candidate_ids = set(fail_candidate_ids or set())

    def run(self, context, *, input_path=None, config=None):
        candidate_id = str(context.get("candidate_id") or "")
        output_path = str(input_path or "")

        if candidate_id in self._fail_candidate_ids:
            return make_module_fail_result(
                self.module_name,
                self.module_version,
                f"{self.module_name}_failed_for_{candidate_id}",
                input_path=input_path,
            )

        if self.module_name in {
            "render_clip_v1",
            "platform_safe_format_v1",
            "intelligent_captions_v1",
        }:
            output_path = str(
                Path(context["clip_dir"]) / f"{candidate_id}_{self.module_name}.mp4"
            )
            Path(output_path).write_bytes(b"stub video bytes")

        metadata: dict[str, Any] = {}
        if self.module_name == "metadata_writer_v1":
            metadata_path = str(
                Path(context["metadata_dir"])
                / f"{context['job_id']}_{candidate_id}_metadata_writer_v1.json"
            )
            Path(metadata_path).write_text(
                json.dumps(
                    {
                        "schema_version": "clip_metadata_v1",
                        "clip_id": f"{context['job_id']}_{candidate_id}",
                        "job_id": context["job_id"],
                        "source_candidate_id": candidate_id,
                        "output_file_path": output_path,
                        "metadata_path": metadata_path,
                    }
                ),
                encoding="utf-8",
            )
            metadata = {
                "metadata_path": metadata_path,
                "output_file_path": output_path,
            }

        return make_module_pass_result(
            self.module_name,
            self.module_version,
            input_path=input_path,
            output_path=output_path,
            metadata=metadata,
        )


def _stub_registry(*, fail_validation_for: set[str] | None = None) -> dict[str, Any]:
    registry: dict[str, Any] = {}
    for name in FIXED_MK1_CONVEYOR_MODULES:
        fail_ids = fail_validation_for if name == "validation_v1" else None
        registry[name] = _StubMk1Module(name, fail_candidate_ids=fail_ids)
    return registry


def _candidate(
    candidate_id: str,
    *,
    start_sec: float = 10.0,
    end_sec: float = 50.0,
    overall_potential: float = 8.5,
    confidence: float = 0.9,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "source_section_id": f"section_{candidate_id}",
        "start_sec": start_sec,
        "end_sec": end_sec,
        "duration_sec": round(end_sec - start_sec, 3),
        "hook_text": "A strong hook starts here.",
        "core_idea_summary": "A concise standalone idea.",
        "why_candidate_has_potential": "Clear payoff and complete context.",
        "archetype": "valuable_insight",
        "confidence": confidence,
        "scores": {
            "hook_strength": 8,
            "standalone_context": 8,
            "insight_value": 8,
            "retention_potential": 8,
            "natural_ending": 8,
            "overall_potential": overall_potential,
        },
        "warnings": [],
        "transcript_quality_flags": [],
    }


def _pool(
    tmp_path: Path,
    *,
    candidates: list[dict[str, Any]],
    job_id: str = "job_prompt_24",
    funnel_id: str = "business",
) -> tuple[dict[str, Any], Path, Path, Path]:
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"stub source video")
    transcript = tmp_path / "transcript.json"
    transcript.write_text("{}", encoding="utf-8")
    processing_report = tmp_path / "processing_report.json"
    processing_report.write_text("{}", encoding="utf-8")

    payload = contracts.build_raw_candidate_pool(
        job_id=job_id,
        source_video_path=str(source_video),
        transcript_path=str(transcript),
        funnel_id=funnel_id,
        candidates=candidates,
        diagnostics={},
        created_at="2026-06-30T12:00:00+00:00",
    )
    pool_path = tmp_path / "raw_candidate_pool.json"
    pool_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload, pool_path, source_video, processing_report


def _execute(
    tmp_path: Path,
    pool_path: Path,
    processing_report: Path,
    *,
    registry: dict[str, Any] | None = None,
    selection_config: dict[str, Any] | None = None,
):
    return run_post_processing_mk1(
        str(pool_path),
        output_root=str(tmp_path),
        job_metadata={
            "job_id": "job_prompt_24",
            "funnel_id": "business",
            "processing_report_path": str(processing_report),
        },
        config={
            "execute_post_processing": True,
            "module_registry": registry or _stub_registry(),
            "selection_config": {
                "selection_mode": "custom",
                "max_clips": 5,
                "reserve_count": 1,
                "min_overall_potential": 7.0,
                "min_confidence": 0.6,
                "min_duration_sec": 1.0,
                "max_duration_sec": 120.0,
                **(selection_config or {}),
            },
        },
    )


def test_execute_post_processing_runs_selection_conveyor_report_and_handoff(tmp_path):
    original_pool, pool_path, _source_video, processing_report = _pool(
        tmp_path,
        candidates=[_candidate("cand_success")],
    )
    original_pool_copy = copy.deepcopy(original_pool)

    result = _execute(tmp_path, pool_path, processing_report)

    assert result["status"] == STATUS_POST_PROCESSING_COMPLETE
    assert Path(result["selection_result_path"]).is_file()
    assert Path(result["post_processing_report_path"]).is_file()
    assert Path(result["output_funnel_handoff_path"]).is_file()
    assert len(result["finished_clip_paths"]) == 1
    assert len(result["per_clip_metadata_paths"]) == 1
    assert Path(result["per_clip_metadata_paths"][0]).is_file()
    assert json.loads(pool_path.read_text(encoding="utf-8")) == original_pool_copy

    report = json.loads(Path(result["post_processing_report_path"]).read_text())
    assert report["raw_candidates_received"] == 1
    assert report["candidates_selected"] == 1
    assert report["clips_passed"] == 1
    assert report["failed_clips"] == []

    handoff = json.loads(Path(result["output_funnel_handoff_path"]).read_text())
    assert handoff["schema_version"] == OUTPUT_FUNNEL_HANDOFF_SCHEMA_VERSION
    assert handoff["status"] == STATUS_READY_FOR_OUTPUT_FUNNEL
    assert handoff["job_id"] == "job_prompt_24"
    assert handoff["funnel_id"] == "business"
    assert handoff["finished_clip_paths"] == result["finished_clip_paths"]
    assert handoff["per_clip_metadata_paths"] == result["per_clip_metadata_paths"]
    assert handoff["post_processing_report_path"] == result["post_processing_report_path"]
    assert handoff["processing_report_path"] == str(processing_report)
    assert handoff["raw_candidate_pool_path"] == str(pool_path)


def test_zero_candidate_pool_writes_report_and_empty_handoff(tmp_path):
    _pool_payload, pool_path, _source_video, processing_report = _pool(
        tmp_path,
        candidates=[],
    )

    result = _execute(tmp_path, pool_path, processing_report)

    assert result["status"] == STATUS_NO_CANDIDATES_SELECTED
    assert result["finished_clip_paths"] == []
    assert Path(result["selection_result_path"]).is_file()
    assert Path(result["post_processing_report_path"]).is_file()
    handoff = json.loads(Path(result["output_funnel_handoff_path"]).read_text())
    assert handoff["finished_clip_paths"] == []


def test_rejected_candidates_do_not_crash_and_are_reported(tmp_path):
    _pool_payload, pool_path, _source_video, processing_report = _pool(
        tmp_path,
        candidates=[_candidate("cand_rejected", overall_potential=5.0)],
    )

    result = _execute(tmp_path, pool_path, processing_report)

    assert result["status"] == STATUS_NO_CANDIDATES_SELECTED
    assert result["finished_clip_paths"] == []
    assert len(result["rejected_candidates"]) == 1
    report = json.loads(Path(result["post_processing_report_path"]).read_text())
    assert report["candidates_rejected"] == 1
    assert report["rejected_candidates"][0]["candidate_id"] == "cand_rejected"


def test_selection_gate_failure_is_controlled_and_writes_report(tmp_path):
    _pool_payload, pool_path, _source_video, processing_report = _pool(
        tmp_path,
        candidates=[_candidate("cand_selected")],
    )

    result = _execute(
        tmp_path,
        pool_path,
        processing_report,
        selection_config={"max_clips": 0},
    )

    assert result["status"] == STATUS_POST_PROCESSING_FAILED
    assert Path(result["selection_result_path"]).is_file()
    assert Path(result["post_processing_report_path"]).is_file()
    assert result["finished_clip_paths"] == []
    assert result["errors"][0]["code"] == "invalid_selection_config"


def test_conveyor_partial_failure_reports_failed_clip_and_handoff_only_passed(tmp_path):
    _pool_payload, pool_path, _source_video, processing_report = _pool(
        tmp_path,
        candidates=[
            _candidate("cand_pass", overall_potential=9.0, start_sec=10.0, end_sec=45.0),
            _candidate("cand_fail", overall_potential=8.5, start_sec=50.0, end_sec=85.0),
        ],
    )

    result = _execute(
        tmp_path,
        pool_path,
        processing_report,
        registry=_stub_registry(fail_validation_for={"cand_fail"}),
    )

    assert result["status"] == STATUS_POST_PROCESSING_PARTIAL
    assert len(result["finished_clip_paths"]) == 1
    assert len(result["failed_clips"]) == 1
    assert result["failed_clips"][0]["source_candidate_id"] == "cand_fail"

    report = json.loads(Path(result["post_processing_report_path"]).read_text())
    assert report["clips_passed"] == 1
    assert report["clips_failed"] == 1

    handoff = json.loads(Path(result["output_funnel_handoff_path"]).read_text())
    assert handoff["finished_clip_paths"] == result["finished_clip_paths"]
    assert all("cand_fail" not in path for path in handoff["finished_clip_paths"])


def test_ready_for_selection_contract_still_default_when_execution_disabled(tmp_path):
    _pool_payload, pool_path, _source_video, _processing_report = _pool(
        tmp_path,
        candidates=[_candidate("cand_ready")],
    )

    result = run_post_processing_mk1(str(pool_path), output_root=str(tmp_path))

    assert result["status"] == STATUS_READY_FOR_SELECTION
    assert not (tmp_path / "post_processing" / "selection" / "selection_result.json").exists()
    assert not (
        tmp_path / "post_processing" / "reports" / "post_processing_report.json"
    ).exists()
