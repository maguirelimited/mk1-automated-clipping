"""Full MK1 production-style smoke harness.

Runs the designed MK1 path with a source video and transcript handoff:
processing_pipeline -> raw_candidate_pool.json -> post_processing_mk1 with
execute_post_processing=True -> real fixed MK1 modules -> reports -> local
output-funnel handoff artifact.

If no source video is provided, the harness generates a short synthetic video.
That verifies wiring and media module contracts, but it is not a real-video
production smoke.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import processing_contracts as contracts
from post_processing_mk1 import (
    OUTPUT_FUNNEL_HANDOFF_SCHEMA_VERSION,
    STATUS_POST_PROCESSING_COMPLETE,
    STATUS_POST_PROCESSING_PARTIAL,
    STATUS_READY_FOR_OUTPUT_FUNNEL,
    run_post_processing_mk1,
)
from processing_pipeline import run_processing_pipeline


class SmokeFailure(RuntimeError):
    """Raised when the smoke detects a blocking MK1 failure."""


class SmokeDiscoveryClient:
    """Deterministic local discovery client for smoke verification."""

    def __init__(self, *, start_sec: float, end_sec: float):
        self.start_sec = start_sec
        self.end_sec = end_sec

    def discover_section(self, section: dict[str, Any], *, config: Any) -> dict[str, Any]:
        section_id = str(section.get("section_id") or "section_0001")
        duration_sec = round(self.end_sec - self.start_sec, 3)
        return {
            "schema_version": "section_candidate_discovery_v1",
            "section_id": section_id,
            "usable": True,
            "confidence": 0.92,
            "reason": "Synthetic smoke candidate with complete local context.",
            "warnings": [],
            "transcript_quality_flags": [],
            "prompt_metadata": {
                "base_prompt_version": "section_candidate_discovery_base_v1",
                "requested_funnel_id": "business",
                "resolved_funnel_id": "business",
                "funnel_rules_version": "business_v1",
            },
            "candidates": [
                {
                    "candidate_local_id": f"{section_id}_smoke_candidate_001",
                    "source_section_id": section_id,
                    "start_sec": self.start_sec,
                    "end_sec": self.end_sec,
                    "duration_sec": duration_sec,
                    "hook_text": "A complete MK1 smoke candidate starts here.",
                    "core_idea_summary": "The pipeline should produce a finished vertical clip.",
                    "why_candidate_has_potential": (
                        "The candidate has deterministic timestamps, transcript text, "
                        "and sufficient duration for the real post-processing modules."
                    ),
                    "archetype": "valuable_insight",
                    "confidence": 0.92,
                    "scores": {
                        "hook_strength": 8,
                        "standalone_context": 8,
                        "insight_value": 8,
                        "retention_potential": 8,
                        "natural_ending": 8,
                        "overall_potential": 9,
                    },
                    "warnings": [],
                    "transcript_quality_flags": [],
                }
            ],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run full MK1 smoke verification.")
    parser.add_argument("--source-video", help="Source video path. If omitted, synthetic video is generated.")
    parser.add_argument("--transcript-path", help="Existing transcript JSON path. If omitted, synthetic transcript is generated.")
    parser.add_argument("--work-dir", help="Directory for smoke artifacts. Defaults to a temporary directory.")
    parser.add_argument("--job-id", default="full_mk1_smoke", help="Job ID for generated artifacts.")
    parser.add_argument("--funnel-id", default="business", help="Funnel ID for processing and handoff metadata.")
    parser.add_argument("--summary-json", help="Optional path to write a machine-readable smoke summary.")
    parser.add_argument("--duration-sec", type=float, default=6.0, help="Synthetic source duration when generated.")
    args = parser.parse_args(argv)

    try:
        summary = run_smoke(args)
    except SmokeFailure as exc:
        print(f"FULL_MK1_SMOKE_FAILED: {exc}", file=sys.stderr)
        return 1

    if args.summary_json:
        _write_json(Path(args.summary_json), summary)

    print("FULL_MK1_SMOKE_PASSED")
    print(f"source_type: {summary['source_type']}")
    print(f"real_video_smoke: {summary['real_video_smoke']}")
    print(f"synthetic_smoke: {summary['synthetic_smoke']}")
    print(f"job_dir: {summary['job_dir']}")
    print(f"finished_clips: {len(summary['finished_clip_paths'])}")
    print(f"metadata_files: {len(summary['per_clip_metadata_paths'])}")
    print(f"post_processing_status: {summary['post_processing_status']}")
    print(f"output_funnel_handoff_path: {summary['output_funnel_handoff_path']}")
    return 0


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    _require_tool("ffmpeg")
    _require_tool("ffprobe")

    work_root = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="mk1_smoke_"))
    work_root.mkdir(parents=True, exist_ok=True)
    job_dir = work_root / args.job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    source_type = "real" if args.source_video else "synthetic"
    source_video = Path(args.source_video).resolve() if args.source_video else job_dir / "synthetic_source.mp4"
    if args.source_video:
        _require_file(source_video, "source video")
    else:
        _generate_synthetic_video(source_video, duration_sec=args.duration_sec)

    duration_sec = _probe_duration(source_video)
    if duration_sec <= 0:
        raise SmokeFailure(f"source video duration is not probeable: {source_video}")

    transcript_path = Path(args.transcript_path).resolve() if args.transcript_path else job_dir / "transcript.json"
    if args.transcript_path:
        _require_file(transcript_path, "transcript")
        transcript = _read_json(transcript_path)
    else:
        transcript = _build_synthetic_transcript(duration_sec)
        _write_json(transcript_path, transcript)

    candidate_start = min(0.5, max(0.0, duration_sec - 2.0))
    candidate_end = min(duration_sec - 0.25, candidate_start + 3.0)
    if candidate_end - candidate_start < 1.0:
        raise SmokeFailure("source video is too short for a valid smoke candidate")

    processing = run_processing_pipeline(
        job_id=args.job_id,
        job_dir=str(job_dir),
        transcript=transcript,
        transcript_path=str(transcript_path),
        source_video_path=str(source_video),
        funnel_id=args.funnel_id,
        ai_client=SmokeDiscoveryClient(
            start_sec=round(candidate_start, 3),
            end_sec=round(candidate_end, 3),
        ),
        discovery_config={
            "max_candidates_per_section": 1,
            "min_candidate_duration_sec": 1.0,
            "max_candidate_duration_sec": 30.0,
            "fail_fast": True,
        },
        sectioning_config={
            "target_section_duration_sec": max(1.0, duration_sec),
            "max_section_duration_sec": max(1.0, duration_sec + 1.0),
            "overlap_sec": 0.0,
            "min_section_duration_sec": 1.0,
        },
        created_at="2026-06-30T12:00:00+00:00",
    )

    raw_candidate_pool_path = Path(processing.raw_candidate_pool_path)
    processing_report_path = Path(processing.processing_report_path)
    raw_pool = _verify_raw_candidate_pool(raw_candidate_pool_path)
    processing_report = _verify_processing_report(processing_report_path)
    if len(raw_pool.get("candidates") or []) < 1:
        raise SmokeFailure("processing produced no raw candidates")

    post_result = run_post_processing_mk1(
        str(raw_candidate_pool_path),
        output_root=str(job_dir),
        job_metadata={
            "job_id": args.job_id,
            "funnel_id": args.funnel_id,
            "processing_report_path": str(processing_report_path),
        },
        config={
            "execute_post_processing": True,
            "transcript_path": str(transcript_path),
            "selection_config": {
                "selection_mode": "custom",
                "max_clips": 1,
                "reserve_count": 0,
                "min_overall_potential": 1.0,
                "min_confidence": 0.1,
                "min_duration_sec": 1.0,
                "max_duration_sec": 30.0,
                "respect_candidate_warnings": True,
                "respect_transcript_quality_flags": True,
                "allow_reserve_candidates": False,
            },
        },
    )

    if post_result.get("status") not in {
        STATUS_POST_PROCESSING_COMPLETE,
        STATUS_POST_PROCESSING_PARTIAL,
    }:
        raise SmokeFailure(
            "post-processing did not complete with finished output: "
            + json.dumps(
                {
                    "status": post_result.get("status"),
                    "errors": post_result.get("errors"),
                    "warnings": post_result.get("warnings"),
                },
                default=str,
            )
        )

    selection_result_path = Path(str(post_result.get("selection_result_path") or ""))
    post_processing_report_path = Path(str(post_result.get("post_processing_report_path") or ""))
    handoff_path = Path(str(post_result.get("output_funnel_handoff_path") or ""))

    selection_result = _verify_selection_result(selection_result_path)
    report = _verify_post_processing_report(post_processing_report_path)
    handoff = _verify_output_funnel_handoff(handoff_path)

    finished_clip_paths = list(post_result.get("finished_clip_paths") or [])
    per_clip_metadata_paths = list(post_result.get("per_clip_metadata_paths") or [])
    if not finished_clip_paths:
        raise SmokeFailure("post-processing produced no finished clips")
    if not per_clip_metadata_paths:
        raise SmokeFailure("post-processing produced no per-clip metadata")

    passed_clip_paths = _verify_finished_clips_and_metadata(
        finished_clip_paths,
        per_clip_metadata_paths,
        report,
    )
    _verify_handoff_matches_passed_clips(handoff, passed_clip_paths, report)

    return {
        "status": "passed",
        "source_type": source_type,
        "real_video_smoke": source_type == "real",
        "synthetic_smoke": source_type == "synthetic",
        "job_id": args.job_id,
        "job_dir": str(job_dir),
        "source_video_path": str(source_video),
        "transcript_path": str(transcript_path),
        "raw_candidate_pool_path": str(raw_candidate_pool_path),
        "processing_report_path": str(processing_report_path),
        "selection_result_path": str(selection_result_path),
        "post_processing_report_path": str(post_processing_report_path),
        "output_funnel_handoff_path": str(handoff_path),
        "finished_clip_paths": finished_clip_paths,
        "per_clip_metadata_paths": per_clip_metadata_paths,
        "failed_clips": list(report.get("failed_clips") or []),
        "post_processing_status": post_result.get("status"),
        "raw_candidate_count": len(raw_pool.get("candidates") or []),
        "selected_count": len(selection_result.get("selected_candidates") or []),
        "processing_report_schema_version": processing_report.get("schema_version"),
    }


def _require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise SmokeFailure(f"required tool is not on PATH: {name}")


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise SmokeFailure(f"{label} not found: {path}")
    if path.stat().st_size <= 0:
        raise SmokeFailure(f"{label} is empty: {path}")


def _generate_synthetic_video(path: Path, *, duration_sec: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x240:rate=30",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1000:sample_rate=44100",
        "-t",
        f"{duration_sec:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if result.returncode != 0:
        raise SmokeFailure(f"failed to create synthetic video: {result.stderr[-1000:]}")
    _require_file(path, "synthetic source video")


def _build_synthetic_transcript(duration_sec: float) -> dict[str, Any]:
    words_text = [
        "This",
        "complete",
        "MK1",
        "smoke",
        "test",
        "verifies",
        "processing",
        "rendering",
        "captions",
        "validation",
        "metadata",
        "and",
        "handoff",
    ]
    words: list[dict[str, Any]] = []
    start = 0.35
    step = 0.32
    for index, word in enumerate(words_text):
        word_start = round(start + index * step, 3)
        word_end = round(min(word_start + 0.25, duration_sec - 0.2), 3)
        if word_end > word_start:
            words.append({"word": word, "start": word_start, "end": word_end})

    return {
        "text": " ".join(words_text),
        "duration": duration_sec,
        "segments": [
            {
                "start": 0.0,
                "end": round(duration_sec, 3),
                "text": " ".join(words_text),
                "words": words,
            }
        ],
        "words": words,
    }


def _verify_raw_candidate_pool(path: Path) -> dict[str, Any]:
    _require_file(path, "raw_candidate_pool.json")
    data = _read_json(path)
    contracts.validate_raw_candidate_pool(data)
    return data


def _verify_processing_report(path: Path) -> dict[str, Any]:
    _require_file(path, "processing_report.json")
    data = _read_json(path)
    contracts.validate_processing_report(data)
    return data


def _verify_selection_result(path: Path) -> dict[str, Any]:
    _require_file(path, "selection_result.json")
    data = _read_json(path)
    if data.get("schema_version") != "selection_gate_v1":
        raise SmokeFailure("selection_result.json has wrong schema_version")
    if data.get("status") != "SELECTION_COMPLETE":
        raise SmokeFailure("selection_result.json status is not SELECTION_COMPLETE")
    if not data.get("selected_candidates"):
        raise SmokeFailure("selection_result.json has no selected candidates")
    return data


def _verify_post_processing_report(path: Path) -> dict[str, Any]:
    _require_file(path, "post_processing_report.json")
    data = _read_json(path)
    if data.get("schema_version") != "post_processing_report_v1":
        raise SmokeFailure("post_processing_report.json has wrong schema_version")
    for key in (
        "raw_candidates_received",
        "candidates_selected",
        "clips_attempted",
        "clips_passed",
        "clips_failed",
    ):
        if not isinstance(data.get(key), int) or data[key] < 0:
            raise SmokeFailure(f"post_processing_report.json has invalid count: {key}")
    if data["clips_passed"] != len(data.get("finished_clip_paths") or []):
        raise SmokeFailure("finished clip count does not match clips_passed")
    if data["clips_failed"] != len(data.get("failed_clips") or []):
        raise SmokeFailure("failed clip count does not match failed_clips")
    if not isinstance(data.get("per_clip_metadata_paths"), list):
        raise SmokeFailure("post_processing_report.json missing metadata path list")
    if not isinstance(data.get("rejected_candidates"), list):
        raise SmokeFailure("post_processing_report.json missing rejected candidates list")
    if not isinstance(data.get("reserve_candidates_list"), list):
        raise SmokeFailure("post_processing_report.json missing reserve candidates list")
    return data


def _verify_output_funnel_handoff(path: Path) -> dict[str, Any]:
    _require_file(path, "output_funnel_handoff.json")
    data = _read_json(path)
    if data.get("schema_version") != OUTPUT_FUNNEL_HANDOFF_SCHEMA_VERSION:
        raise SmokeFailure("output_funnel_handoff.json has wrong schema_version")
    if data.get("status") != STATUS_READY_FOR_OUTPUT_FUNNEL:
        raise SmokeFailure("output_funnel_handoff.json status is not READY_FOR_OUTPUT_FUNNEL")
    if not isinstance(data.get("finished_clip_paths"), list):
        raise SmokeFailure("output_funnel_handoff.json missing finished_clip_paths")
    return data


def _verify_finished_clips_and_metadata(
    finished_clip_paths: list[str],
    metadata_paths: list[str],
    report: dict[str, Any],
) -> list[str]:
    metadata_by_output: dict[str, dict[str, Any]] = {}
    for metadata_path in metadata_paths:
        path = Path(metadata_path)
        _require_file(path, "per-clip metadata JSON")
        metadata = _read_json(path)
        output_path = str(metadata.get("output_file_path") or "")
        if not output_path:
            raise SmokeFailure(f"metadata does not reference output_file_path: {path}")
        metadata_by_output[output_path] = metadata

    passed_paths: list[str] = []
    for clip_path in finished_clip_paths:
        path = Path(clip_path)
        _require_file(path, "finished clip MP4")
        info = _probe_video(path)
        if not info.get("has_video"):
            raise SmokeFailure(f"finished clip has no video stream: {path}")
        aspect = float(info["width"]) / float(info["height"])
        if abs(aspect - (9 / 16)) > 0.02:
            raise SmokeFailure(f"finished clip is not 9:16: {path} aspect={aspect:.6f}")
        metadata = metadata_by_output.get(clip_path)
        if metadata is None:
            raise SmokeFailure(f"no metadata JSON references finished clip: {clip_path}")
        if metadata.get("validation_result") != "PASS":
            raise SmokeFailure(f"metadata validation_result is not PASS: {metadata.get('metadata_path')}")
        passed_paths.append(clip_path)

    report_finished = set(report.get("finished_clip_paths") or [])
    if set(passed_paths) != report_finished:
        raise SmokeFailure("report finished_clip_paths do not match verified passed clips")
    return passed_paths


def _verify_handoff_matches_passed_clips(
    handoff: dict[str, Any],
    passed_clip_paths: list[str],
    report: dict[str, Any],
) -> None:
    handoff_paths = set(handoff.get("finished_clip_paths") or [])
    passed_paths = set(passed_clip_paths)
    if handoff_paths != passed_paths:
        raise SmokeFailure("output-funnel handoff paths do not match validation-passed clips")

    failed_paths = {
        str(item.get("output_file_path"))
        for item in (report.get("failed_clips") or [])
        if isinstance(item, dict) and item.get("output_file_path")
    }
    if handoff_paths.intersection(failed_paths):
        raise SmokeFailure("output-funnel handoff includes failed clip output")


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def _probe_video(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise SmokeFailure(f"ffprobe could not read finished clip: {path}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        return {"has_video": False}
    stream = streams[0]
    return {
        "has_video": True,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
    }


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SmokeFailure(f"JSON file is not an object: {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
