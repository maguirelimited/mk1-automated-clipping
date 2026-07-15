"""MK1 staged selection pipeline end-to-end smoke test (Prompt 11).

Validates the connected MK1 flow from synthetic transcript input through
processing artifacts, Evaluation, and render handoff — without real AI, GPU,
ffmpeg rendering, or network access.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import candidate_processing as cp  # noqa: E402
import processing_contracts as contracts  # noqa: E402
import processing_settings as settings  # noqa: E402
import section_candidate_discovery as discovery  # noqa: E402
import selection_gate_v1 as evaluation  # noqa: E402
import transcript_sectioning as presentation  # noqa: E402
from post_processing_conveyor import FIXED_MK1_CONVEYOR_MODULES  # noqa: E402
from post_processing_mk1 import (  # noqa: E402
    STATUS_POST_PROCESSING_COMPLETE,
    run_post_processing_mk1,
)
from post_processing_modules import (  # noqa: E402
    MODULE_STATUS_PASS,
    PostProcessingModule,
    make_module_pass_result,
)
from processing_integration import collect_candidates_from_batch  # noqa: E402
from processing_pipeline import run_processing_pipeline  # noqa: E402
from render_clip_v1 import RenderClipV1Module, _make_output_path  # noqa: E402

LEGACY_CLIP_SELECTION_PROMPT = (
    Path(__file__).resolve().parents[2]
    / "ai-service"
    / "prompts"
    / "clip_selection_v2.txt"
)

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
) -> dict:
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
                    "archetype": "valuable_insight",
                    "confidence": 0.78,
                    "scores": _scores(),
                    "warnings": [],
                    "transcript_quality_flags": [],
                }
            )

    return {
        "schema_version": "section_candidate_discovery_v1",
        "section_id": section_id,
        "usable": usable and bool(candidates),
        "confidence": 0.78 if (usable and candidates) else 0.2,
        "reason": (
            "Strong standalone clip found."
            if (usable and candidates)
            else "No viable standalone clip in this section."
        ),
        "warnings": [],
        "transcript_quality_flags": [],
        "candidates": candidates,
        "prompt_metadata": {
            "base_prompt_version": "section_candidate_discovery_base_v1",
            "requested_funnel_id": "business",
            "resolved_funnel_id": "business",
            "funnel_rules_version": "business_v1",
        },
    }


class FakeDiscoveryClient:
    """Deterministic fake AI client — no network or model calls."""

    def __init__(self, *, default_num_candidates: int = 1):
        self.default_num_candidates = default_num_candidates
        self.calls: list[dict] = []

    def discover_section(self, section: dict, *, config) -> dict:
        self.calls.append({"section_id": section["section_id"], "config": config})
        return _section_result(
            section["section_id"],
            section_start=float(section.get("start_sec", 0.0)),
            section_end=float(section.get("end_sec", 300.0)),
            usable=True,
            num_candidates=self.default_num_candidates,
        )


class _TrackingRenderStub(PostProcessingModule):
    """Records selected candidates handed to the render conveyor step."""

    module_version = "1.0"
    received_candidates: list[dict[str, Any]] = []

    def __init__(self, module_name: str):
        self.module_name = module_name

    def run(self, context, *, input_path=None, config=None):
        candidate_id = str(context.get("candidate_id") or "")
        if self.module_name == "render_clip_v1":
            selected = context.get("selected_candidate") or {}
            self.__class__.received_candidates.append(copy.deepcopy(selected))
            output_path = str(
                Path(context["clip_dir"]) / f"{candidate_id}_render_clip_v1.mp4"
            )
            Path(output_path).write_bytes(b"stub video bytes")
            return make_module_pass_result(
                self.module_name,
                self.module_version,
                input_path=input_path,
                output_path=output_path,
            )

        if self.module_name in {"platform_safe_format_v1", "intelligent_captions_v1"}:
            output_path = str(
                Path(context["clip_dir"]) / f"{candidate_id}_{self.module_name}.mp4"
            )
            Path(output_path).write_bytes(b"stub video bytes")
            return make_module_pass_result(
                self.module_name,
                self.module_version,
                input_path=input_path,
                output_path=output_path,
            )

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
                        "output_file_path": str(input_path or ""),
                        "metadata_path": metadata_path,
                    }
                ),
                encoding="utf-8",
            )
            return make_module_pass_result(
                self.module_name,
                self.module_version,
                input_path=input_path,
                output_path=str(input_path or ""),
                metadata={"metadata_path": metadata_path},
            )

        return make_module_pass_result(
            self.module_name,
            self.module_version,
            input_path=input_path,
            output_path=str(input_path or ""),
        )


def _stub_registry() -> dict[str, Any]:
    return {name: _TrackingRenderStub(name) for name in FIXED_MK1_CONVEYOR_MODULES}


def _run_mk1_processing_stage(
    tmp_path: Path,
    *,
    job_id: str = "mk1_smoke_job",
    source_video: Path,
) -> dict[str, Path]:
    """Run processing pipeline and return artifact paths keyed by role."""
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text(json.dumps(_transcript()), encoding="utf-8")

    client = FakeDiscoveryClient(default_num_candidates=1)
    result = run_processing_pipeline(
        job_id=job_id,
        job_dir=str(tmp_path),
        transcript=_transcript(),
        transcript_path=str(transcript_path),
        source_video_path=str(source_video),
        funnel_id="business",
        ai_client=client,
        created_at="2026-06-30T12:00:00+00:00",
    )

    assert len(client.calls) >= 1, "Discovery must receive transcript sections"

    paths = {
        "transcript_sections": tmp_path / "transcript_sections.json",
        "section_candidate_discovery": tmp_path / "section_candidate_discovery.json",
        "candidate_processing": tmp_path / "candidate_processing.json",
        "raw_candidate_pool": Path(result.raw_candidate_pool_path),
        "processing_report": Path(result.processing_report_path),
    }
    return paths


def _assert_processing_artifact_chain(paths: dict[str, Path]) -> tuple[dict, dict, dict]:
    for name, path in paths.items():
        assert path.is_file(), f"{name} artifact missing: {path}"

    sections = json.loads(paths["transcript_sections"].read_text(encoding="utf-8"))
    discovery_batch = json.loads(
        paths["section_candidate_discovery"].read_text(encoding="utf-8")
    )
    processed = json.loads(paths["candidate_processing"].read_text(encoding="utf-8"))
    pool = json.loads(paths["raw_candidate_pool"].read_text(encoding="utf-8"))
    report = json.loads(paths["processing_report"].read_text(encoding="utf-8"))

    assert sections["presentation"]["strategy"] == presentation.MK1_PRESENTATION_STRATEGY
    assert discovery_batch["discovery"]["strategy"] == discovery.MK1_DISCOVERY_STRATEGY
    assert processed["processing"]["strategy"] == cp.MK1_CANDIDATE_PROCESSING_STRATEGY

    assert discovery_batch["source_transcript_sections_path"] == str(
        paths["transcript_sections"]
    )
    assert processed["source_section_candidate_discovery_path"] == str(
        paths["section_candidate_discovery"]
    )

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
    expected_candidates = collect_candidates_from_batch(batch_for_pool, pool["job_id"])
    assert pool["candidates"] == expected_candidates
    assert len(pool["candidates"]) >= 1

    for candidate in pool["candidates"]:
        contracts.validate_mk1_candidate(candidate)

    assert report.get("candidate_processing_strategy") == cp.MK1_CANDIDATE_PROCESSING_STRATEGY

    return pool, processed, discovery_batch


def _assert_render_contract(selected: dict[str, Any], *, source_video: str) -> None:
    assert selected.get("candidate_id")
    assert isinstance(selected["start_sec"], (int, float))
    assert isinstance(selected["end_sec"], (int, float))
    assert selected["end_sec"] > selected["start_sec"]
    assert "source_candidate" in selected
    assert selected["source_candidate"]["candidate_id"] == selected["candidate_id"]

    clip_dir = str(Path(source_video).parent / "clips")
    Path(clip_dir).mkdir(parents=True, exist_ok=True)
    output_path = _make_output_path(clip_dir, "mk1_smoke_job", selected["candidate_id"])
    Path(output_path).write_bytes(b"\x00" * 512)

    module = RenderClipV1Module()
    context = {
        "job_id": "mk1_smoke_job",
        "candidate_id": selected["candidate_id"],
        "source_video_path": source_video,
        "clip_dir": clip_dir,
        "metadata_dir": str(Path(source_video).parent / "metadata"),
        "selected_candidate": selected,
        "module_results": [],
        "config": {},
        "selection_result": {},
    }

    with patch("render_clip_v1.subprocess.run") as mock_run, patch(
        "render_clip_v1.ffprobe_duration_sec"
    ) as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_probe.return_value = selected["end_sec"] - selected["start_sec"]
        result = module.run(context, input_path=source_video)

    assert result["status"] == MODULE_STATUS_PASS


@pytest.mark.smoke
def test_mk1_staged_selection_pipeline_end_to_end_smoke(tmp_path: Path):
    """Full MK1 staged flow: processing artifacts → Evaluation → render handoff."""
    _TrackingRenderStub.received_candidates = []

    job_id = "mk1_smoke_job"
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"stub source video")

    paths = _run_mk1_processing_stage(
        tmp_path,
        job_id=job_id,
        source_video=source_video,
    )
    pool, processed, discovery_batch = _assert_processing_artifact_chain(paths)

    discovery_candidate_count = sum(
        len(section.get("candidates") or [])
        for section in discovery_batch.get("section_results") or []
    )
    assert discovery_candidate_count >= len(pool["candidates"])

    post_root = tmp_path / "post_processing"
    result = run_post_processing_mk1(
        str(paths["raw_candidate_pool"]),
        source_video_path=str(source_video),
        output_root=str(post_root),
        job_metadata={
            "job_id": job_id,
            "funnel_id": "business",
            "processing_report_path": str(paths["processing_report"]),
        },
        config={
            "execute_post_processing": True,
            "module_registry": _stub_registry(),
            "selection_config": {
                "selection_mode": "custom",
                "max_clips": 5,
                "reserve_count": 1,
                "min_overall_potential": 7.0,
                "min_confidence": 0.6,
                "min_duration_sec": 1.0,
                "max_duration_sec": 120.0,
            },
        },
    )

    assert result["status"] == STATUS_POST_PROCESSING_COMPLETE

    selection_path = Path(result["selection_result_path"])
    assert selection_path.is_file()
    assert "post_processing" in str(selection_path)
    assert selection_path.name == "selection_result.json"
    assert selection_path.parent.name == "selection"

    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    assert selection["evaluation"]["strategy"] == evaluation.MK1_EVALUATION_STRATEGY
    assert isinstance(selection.get("selected_candidates"), list)
    assert isinstance(selection.get("reserve_candidates"), list)
    assert isinstance(selection.get("rejected_candidates"), list)
    assert len(selection["selected_candidates"]) >= 1

    selected_ids = {c["candidate_id"] for c in selection["selected_candidates"]}
    pool_ids = {c["candidate_id"] for c in pool["candidates"]}
    assert selected_ids.issubset(pool_ids)

    for selected in selection["selected_candidates"]:
        _assert_render_contract(selected, source_video=str(source_video))

    assert len(_TrackingRenderStub.received_candidates) >= 1
    for handed_off in _TrackingRenderStub.received_candidates:
        assert handed_off["candidate_id"] in selected_ids
        assert handed_off["end_sec"] > handed_off["start_sec"]


def test_mk1_smoke_default_pipeline_mode_remains_legacy():
    assert settings.DEFAULT_PIPELINE_MODE == "legacy"


def test_mk1_smoke_does_not_require_legacy_selection_prompt():
    assert LEGACY_CLIP_SELECTION_PROMPT.is_file()
    text = LEGACY_CLIP_SELECTION_PROMPT.read_text(encoding="utf-8")
    assert "clip_selection_v2" in text or len(text) > 100
