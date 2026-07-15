"""Regression tests for post-processing conveyor config resolution.

Ensures top-level global fields (e.g. transcript_path) survive when a nested
conveyor_config is present, while module-specific conveyor settings still apply.
"""

from __future__ import annotations

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
    _resolve_conveyor_config,
    run_post_processing_mk1,
)
from post_processing_modules import (  # noqa: E402
    PostProcessingModule,
    make_module_pass_result,
)


class _ConfigCaptureModule(PostProcessingModule):
    """Records context.config on each run for wiring assertions."""

    module_version = "1.0"

    def __init__(self, module_name: str, captured: list[dict[str, Any]]):
        self.module_name = module_name
        self._captured = captured

    def run(self, context, *, input_path=None, config=None):
        if self.module_name == "intelligent_captions_v1":
            self._captured.append(dict(context.get("config") or {}))

        output_path = str(input_path or "")
        if self.module_name in {
            "render_clip_v1",
            "platform_safe_format_v1",
            "intelligent_captions_v1",
        }:
            candidate_id = str(context.get("candidate_id") or "unknown")
            output_path = str(
                Path(context["clip_dir"]) / f"{candidate_id}_{self.module_name}.mp4"
            )
            Path(output_path).write_bytes(b"stub video bytes")

        metadata: dict[str, Any] = {}
        if self.module_name == "metadata_writer_v1":
            candidate_id = str(context.get("candidate_id") or "unknown")
            metadata_path = str(
                Path(context["metadata_dir"])
                / f"{context['job_id']}_{candidate_id}_metadata_writer_v1.json"
            )
            Path(metadata_path).write_text("{}", encoding="utf-8")
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


def _candidate(candidate_id: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "source_section_id": f"section_{candidate_id}",
        "start_sec": 10.0,
        "end_sec": 50.0,
        "duration_sec": 40.0,
        "hook_text": "A strong hook starts here.",
        "core_idea_summary": "A concise standalone idea.",
        "why_candidate_has_potential": "Clear payoff and complete context.",
        "archetype": "valuable_insight",
        "confidence": 0.9,
        "scores": {
            "hook_strength": 8,
            "standalone_context": 8,
            "insight_value": 8,
            "retention_potential": 8,
            "natural_ending": 8,
            "overall_potential": 8.5,
        },
        "warnings": [],
        "transcript_quality_flags": [],
    }


def _pool(tmp_path: Path) -> tuple[Path, Path]:
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"stub source video")
    transcript = tmp_path / "transcript.json"
    transcript.write_text("{}", encoding="utf-8")
    processing_report = tmp_path / "processing_report.json"
    processing_report.write_text("{}", encoding="utf-8")

    payload = contracts.build_raw_candidate_pool(
        job_id="job_conveyor_cfg",
        source_video_path=str(source_video),
        transcript_path=str(transcript),
        funnel_id="business",
        candidates=[_candidate("cand_cfg")],
        diagnostics={},
        created_at="2026-06-30T12:00:00+00:00",
    )
    pool_path = tmp_path / "raw_candidate_pool.json"
    pool_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return pool_path, processing_report


def test_resolve_conveyor_config_without_nested_config_unchanged():
    config = {
        "execute_post_processing": True,
        "module_registry": {"render_clip_v1": object()},
        "selection_config": {"max_clips": 3},
        "conveyor_config": "not-a-dict",
        "processing_report_path": "/tmp/report.json",
        "output_root": "/tmp/out",
        "transcript_path": "/tmp/transcript.json",
        "transcript_payload_path": "/tmp/payload.json",
        "source_video_path": "/tmp/source.mp4",
        "job_id": "job_123",
        "funnel_id": "business",
        "font_size": 72,
    }

    resolved = _resolve_conveyor_config(config)

    assert resolved == {
        "transcript_path": "/tmp/transcript.json",
        "transcript_payload_path": "/tmp/payload.json",
        "source_video_path": "/tmp/source.mp4",
        "job_id": "job_123",
        "funnel_id": "business",
        "font_size": 72,
    }


def test_resolve_conveyor_config_merges_global_fields_with_nested_config():
    config = {
        "execute_post_processing": True,
        "output_root": "/tmp/out",
        "transcript_path": "/tmp/transcript.json",
        "transcript_payload_path": "/tmp/payload.json",
        "source_video_path": "/tmp/source.mp4",
        "job_id": "job_123",
        "funnel_id": "business",
        "conveyor_config": {
            "font_size": 48,
            "target_width": 1080,
        },
    }

    resolved = _resolve_conveyor_config(config)

    assert resolved["transcript_path"] == "/tmp/transcript.json"
    assert resolved["transcript_payload_path"] == "/tmp/payload.json"
    assert resolved["source_video_path"] == "/tmp/source.mp4"
    assert resolved["job_id"] == "job_123"
    assert resolved["funnel_id"] == "business"
    assert resolved["font_size"] == 48
    assert resolved["target_width"] == 1080
    assert "execute_post_processing" not in resolved
    assert "output_root" not in resolved


def test_resolve_conveyor_config_nested_overrides_module_settings():
    config = {
        "transcript_path": "/tmp/transcript.json",
        "font_size": 99,
        "conveyor_config": {
            "font_size": 48,
            "target_height": 1920,
        },
    }

    resolved = _resolve_conveyor_config(config)

    assert resolved["transcript_path"] == "/tmp/transcript.json"
    assert resolved["font_size"] == 48
    assert resolved["target_height"] == 1920


def test_intelligent_captions_receives_transcript_path_when_conveyor_config_present(
    tmp_path,
):
    pool_path, processing_report = _pool(tmp_path)
    transcript_path = str(tmp_path / "transcript.json")
    captured_configs: list[dict[str, Any]] = []

    registry = {
        name: _ConfigCaptureModule(name, captured_configs)
        for name in FIXED_MK1_CONVEYOR_MODULES
    }

    result = run_post_processing_mk1(
        str(pool_path),
        output_root=str(tmp_path),
        job_metadata={
            "job_id": "job_conveyor_cfg",
            "funnel_id": "business",
            "processing_report_path": str(processing_report),
        },
        config={
            "execute_post_processing": True,
            "transcript_path": transcript_path,
            "transcript_payload_path": str(tmp_path / "payload.json"),
            "source_video_path": str(tmp_path / "source.mp4"),
            "job_id": "job_conveyor_cfg",
            "funnel_id": "business",
            "module_registry": registry,
            "selection_config": {
                "selection_mode": "custom",
                "max_clips": 5,
                "reserve_count": 0,
                "min_overall_potential": 7.0,
                "min_confidence": 0.6,
                "min_duration_sec": 1.0,
                "max_duration_sec": 120.0,
            },
            "conveyor_config": {
                "font_size": 48,
                "target_width": 1080,
                "target_height": 1920,
            },
        },
    )

    assert result["status"] == "POST_PROCESSING_COMPLETE"
    assert len(captured_configs) == 1
    assert captured_configs[0]["transcript_path"] == transcript_path
    assert captured_configs[0]["font_size"] == 48
    assert captured_configs[0]["target_width"] == 1080
