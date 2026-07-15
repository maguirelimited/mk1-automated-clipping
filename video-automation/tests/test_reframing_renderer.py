"""Focused tests for reframing.renderer — segmented face-track crop rendering."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from reframing.renderer import (
    DEFAULT_SEGMENT_CROP_CHANGE_THRESHOLD_PX,
    REASON_CROP_REPORT_NOT_USABLE,
    build_concat_command,
    build_face_track_render_plan,
    build_segment_command,
    build_segment_intervals,
    face_track_render_metadata,
    plan_render_segments_from_crop_path,
    render_face_track_crop,
    validate_smoothed_crop_for_render,
    write_concat_list,
)
from reframing.types import (
    CropPathReport,
    CropPathSample,
    CropRect,
    FaceTrackRenderResult,
    RenderSegment,
)


def _cmd_available(name: str) -> bool:
    return shutil.which(name) is not None


FFTOOLS_AVAILABLE = _cmd_available("ffmpeg") and _cmd_available("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    not FFTOOLS_AVAILABLE,
    reason="ffmpeg/ffprobe not installed — skipping renderer integration tests",
)


def _crop(x: int, y: int, w: int = 608, h: int = 1080) -> CropRect:
    return CropRect(x=x, y=y, width=w, height=h)


def _sample(
    frame_index: int,
    *,
    x: int,
    y: int = 0,
    held: bool = False,
    fps: float = 2.0,
) -> CropPathSample:
    return CropPathSample(
        timestamp_sec=frame_index / fps,
        frame_index=frame_index,
        crop=_crop(x, y),
        held=held,
    )


def _usable_smoothed_report(
    samples: list[CropPathSample],
    *,
    source_width: int = 1920,
    source_height: int = 1080,
) -> CropPathReport:
    return CropPathReport(
        ok=True,
        usable=True,
        input_path="/in.mp4",
        source_width=source_width,
        source_height=source_height,
        crop_width=608,
        crop_height=1080,
        smoothed=True,
        smoothing_method="deadzone_ema_velocity_cap",
        samples=samples,
    )


def test_unusable_smoothed_crop_report_fails_validation():
    report = CropPathReport(ok=False, usable=False, input_path="/in.mp4")
    assert validate_smoothed_crop_for_render(report, target_width=1080, target_height=1920) == (
        REASON_CROP_REPORT_NOT_USABLE
    )


def test_segment_intervals_hold_first_crop_from_zero():
    samples = [_sample(0, x=100), _sample(1, x=200), _sample(2, x=300)]
    segments = build_segment_intervals(samples, clip_duration_sec=2.0)
    assert len(segments) == 3
    assert segments[0].start_sec == 0.0
    assert segments[0].end_sec == 0.5
    assert segments[0].crop.x == 100
    assert segments[1].start_sec == 0.5
    assert segments[1].end_sec == 1.0
    assert segments[2].start_sec == 1.0
    assert segments[2].end_sec == 2.0


def test_segment_intervals_hold_last_crop_until_clip_end():
    samples = [_sample(0, x=100), _sample(1, x=200)]
    segments = build_segment_intervals(samples, clip_duration_sec=3.0)
    assert segments[-1].end_sec == 3.0


def test_first_crop_held_from_zero_when_first_sample_after_zero():
    samples = [
        CropPathSample(timestamp_sec=0.5, frame_index=1, crop=_crop(400, 0), held=False),
        CropPathSample(timestamp_sec=1.0, frame_index=2, crop=_crop(420, 0), held=False),
    ]
    segments = build_segment_intervals(samples, clip_duration_sec=2.0)
    assert segments[0].start_sec == 0.0
    assert segments[0].end_sec == 1.0
    assert segments[0].crop.x == 400


def test_build_segment_command_contains_crop_and_scale():
    segment = RenderSegment(
        start_sec=0.0,
        end_sec=0.5,
        crop=_crop(656, 0),
        sample_index=0,
    )
    cmd = build_segment_command(
        input_path="/in.mp4",
        output_path="/seg.mp4",
        segment=segment,
        target_w=1080,
        target_h=1920,
        config={"video_codec": "libx264", "audio_codec": "aac", "ffmpeg_preset": "veryfast"},
        input_has_audio=True,
    )
    cmd_str = " ".join(cmd)
    assert "crop=608:1080:656:0" in cmd_str
    assert "scale=1080:1920" in cmd_str
    assert "-map 0:a?" in cmd_str


def test_build_segment_command_without_audio():
    segment = RenderSegment(
        start_sec=0.0,
        end_sec=0.5,
        crop=_crop(100, 0),
        sample_index=0,
    )
    cmd = build_segment_command(
        input_path="/in.mp4",
        output_path="/seg.mp4",
        segment=segment,
        target_w=1080,
        target_h=1920,
        config={"video_codec": "libx264", "audio_codec": "aac", "ffmpeg_preset": "veryfast"},
        input_has_audio=False,
    )
    assert "-an" in cmd


def test_write_concat_list(tmp_path):
    seg_a = str(tmp_path / "a.mp4")
    seg_b = str(tmp_path / "b.mp4")
    list_path = str(tmp_path / "concat.txt")
    write_concat_list(list_path, [seg_a, seg_b])
    content = (tmp_path / "concat.txt").read_text(encoding="utf-8")
    assert f"file '{seg_a}'" in content
    assert f"file '{seg_b}'" in content


def test_build_concat_command():
    cmd = build_concat_command(
        concat_list_path="/tmp/concat.txt",
        output_path="/out.mp4",
        config={"video_codec": "libx264", "audio_codec": "aac", "ffmpeg_preset": "veryfast"},
        input_has_audio=True,
    )
    cmd_str = " ".join(cmd)
    assert "-f concat" in cmd_str
    assert "-safe 0" in cmd_str


def test_render_face_track_crop_structured_failure(tmp_path):
    report = _usable_smoothed_report([_sample(0, x=100)])
    result = render_face_track_crop(
        input_path=str(tmp_path / "missing.mp4"),
        output_path=str(tmp_path / "out.mp4"),
        smoothed_crop_report=report,
        tmp_dir=str(tmp_path),
        input_has_audio=True,
        clip_duration_sec=1.0,
    )
    assert result.ok is False
    assert result.reason is not None


def test_face_track_render_metadata_success():
    result = FaceTrackRenderResult(
        ok=True,
        output_path="/out.mp4",
        segments_planned=40,
        segments_rendered=18,
        segments_merged=22,
        unique_crop_rects_before_merge=15,
        unique_crop_rects_after_merge=12,
        segment_crop_change_threshold_px=4,
        crop_renderer="segmented_ffmpeg",
    )
    meta = face_track_render_metadata(render_result=result)
    assert meta["face_track_render_attempted"] is True
    assert meta["face_track_rendered"] is True
    assert meta["segments_planned"] == 40
    assert meta["segments_rendered"] == 18
    assert meta["segments_merged"] == 22
    assert meta["unique_crop_rects_before_merge"] == 15
    assert meta["unique_crop_rects_after_merge"] == 12
    assert meta["segment_crop_change_threshold_px"] == 4
    assert meta["crop_renderer"] == "segmented_ffmpeg"


def test_identical_adjacent_crop_samples_merge_into_one_segment():
    samples = [
        _sample(0, x=778),
        _sample(1, x=778),
        _sample(2, x=778),
    ]
    segments, stats = plan_render_segments_from_crop_path(samples, clip_duration_sec=1.5)
    assert len(segments) == 1
    assert segments[0].start_sec == 0.0
    assert segments[0].end_sec == 1.5
    assert segments[0].crop.x == 778
    assert stats.segments_planned == 3
    assert stats.segments_rendered == 1
    assert stats.segments_merged == 2


def test_near_identical_crop_samples_within_threshold_merge():
    samples = [
        _sample(0, x=100),
        _sample(1, x=102),
        _sample(2, x=103),
    ]
    segments, stats = plan_render_segments_from_crop_path(
        samples,
        clip_duration_sec=1.5,
        segment_crop_change_threshold_px=4,
    )
    assert len(segments) == 1
    assert segments[0].crop.x == 100
    assert stats.segments_merged == 2


def test_crop_samples_beyond_threshold_create_new_segment():
    samples = [
        _sample(0, x=100),
        _sample(1, x=110),
    ]
    segments, stats = plan_render_segments_from_crop_path(
        samples,
        clip_duration_sec=1.0,
        segment_crop_change_threshold_px=4,
    )
    assert len(segments) == 2
    assert segments[0].crop.x == 100
    assert segments[1].crop.x == 110
    assert stats.segments_merged == 0


def test_segment_start_end_times_are_correct_after_merge():
    samples = [
        _sample(0, x=500),
        _sample(1, x=500),
        _sample(2, x=600),
        _sample(3, x=600),
    ]
    segments, _stats = plan_render_segments_from_crop_path(samples, clip_duration_sec=2.0)
    assert len(segments) == 2
    assert segments[0].start_sec == 0.0
    assert segments[0].end_sec == 1.0
    assert segments[1].start_sec == 1.0
    assert segments[1].end_sec == 2.0


def test_final_segment_reaches_expected_clip_duration():
    samples = [_sample(0, x=100), _sample(1, x=100), _sample(2, x=100)]
    segments, _stats = plan_render_segments_from_crop_path(samples, clip_duration_sec=3.0)
    assert segments[-1].end_sec == 3.0


def test_plan_metadata_records_planned_rendered_merged_counts():
    samples = [_sample(i, x=100 if i < 3 else 200) for i in range(5)]
    _segments, stats = plan_render_segments_from_crop_path(samples, clip_duration_sec=2.5)
    assert stats.segments_planned == 5
    assert stats.segments_rendered == 2
    assert stats.segments_merged == 3
    assert stats.unique_crop_rects_before_merge == 2
    assert stats.unique_crop_rects_after_merge == 2
    assert stats.segment_crop_change_threshold_px == DEFAULT_SEGMENT_CROP_CHANGE_THRESHOLD_PX


def test_render_face_track_crop_does_not_call_ffmpeg_per_sample_when_merged(tmp_path):
    samples = [_sample(i, x=778) for i in range(8)]
    report = _usable_smoothed_report(samples)
    input_path = tmp_path / "input.mp4"
    input_path.write_bytes(b"fake")
    output_path = tmp_path / "output.mp4"
    output_path.write_bytes(b"out")

    ffmpeg_calls: list[list[str]] = []

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        ffmpeg_calls.append(cmd)
        if "concat" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        segment_index = len(ffmpeg_calls) - 1
        segment_path = cmd[-1]
        Path(segment_path).write_bytes(b"seg")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    result = render_face_track_crop(
        input_path=str(input_path),
        output_path=str(output_path),
        smoothed_crop_report=report,
        tmp_dir=str(tmp_path / "work"),
        input_has_audio=True,
        clip_duration_sec=4.0,
        run_ffmpeg=fake_runner,
    )
    assert result.ok is True
    assert result.segments_planned == 8
    assert result.segments_rendered == 1
    assert result.segments_merged == 7
    segment_calls = [cmd for cmd in ffmpeg_calls if "concat" not in cmd]
    assert len(segment_calls) == 1


def test_render_face_track_crop_cleans_up_temp_segments(tmp_path):
    samples = [_sample(0, x=100), _sample(1, x=100)]
    report = _usable_smoothed_report(samples)
    input_path = tmp_path / "input.mp4"
    input_path.write_bytes(b"fake")
    output_path = tmp_path / "output.mp4"
    work_dir = tmp_path / "work"

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if "concat" not in cmd:
            Path(cmd[-1]).write_bytes(b"seg")
        else:
            output_path.write_bytes(b"out")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    render_face_track_crop(
        input_path=str(input_path),
        output_path=str(output_path),
        smoothed_crop_report=report,
        tmp_dir=str(work_dir),
        input_has_audio=True,
        clip_duration_sec=1.0,
        config={"cleanup_temp_segments": True},
        run_ffmpeg=fake_runner,
    )
    segment_root = work_dir / "face_track_render"
    assert not segment_root.exists() or not any(segment_root.rglob("segment_*.mp4"))


def test_build_face_track_render_plan_rejects_out_of_bounds_crop():
    report = _usable_smoothed_report([_sample(0, x=2000)])
    segments, stats, error = build_face_track_render_plan(report, clip_duration_sec=1.0)
    assert segments == []
    assert stats is None
    assert error is not None


@requires_ffmpeg
def test_render_face_track_crop_integration(tmp_path):
    input_path = str(tmp_path / "input.mp4")
    output_path = str(tmp_path / "output.mp4")
    duration = 2.0

    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            str(duration),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            input_path,
        ],
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr

    report = _usable_smoothed_report(
        [
            _sample(0, x=16, y=0, fps=2.0),
            _sample(1, x=20, y=0, fps=2.0),
            _sample(2, x=24, y=0, fps=2.0),
            _sample(3, x=28, y=0, fps=2.0),
        ],
        source_width=640,
        source_height=360,
    )
    report = CropPathReport(
        ok=report.ok,
        usable=report.usable,
        input_path=input_path,
        source_width=640,
        source_height=360,
        crop_width=202,
        crop_height=360,
        target_width=1080,
        target_height=1920,
        target_aspect=9 / 16,
        smoothed=True,
        smoothing_method="deadzone_ema_velocity_cap",
        samples=[
            CropPathSample(
                timestamp_sec=i / 2.0,
                frame_index=i,
                crop=CropRect(x=16 + i * 4, y=0, width=202, height=360),
                held=False,
            )
            for i in range(4)
        ],
    )

    result = render_face_track_crop(
        input_path=input_path,
        output_path=output_path,
        smoothed_crop_report=report,
        tmp_dir=str(tmp_path / "work"),
        input_has_audio=True,
        clip_duration_sec=duration,
        config={"cleanup_temp_segments": True},
    )
    assert result.ok is True, result.message
    assert os.path.isfile(output_path)
    assert os.path.getsize(output_path) > 0

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            output_path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0
    width, height = probe.stdout.strip().split(",")
    assert int(width) == 1080
    assert int(height) == 1920
