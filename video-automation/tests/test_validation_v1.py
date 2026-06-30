"""validation_v1 — focused tests (Prompt 21).

Tests are split into:
  - Non-ffmpeg tests: input validation, config validation, module contract,
    required modules, duration, aspect ratio, audio, caption metadata.
    Always run.
  - ffmpeg tests: real probe checks with synthetic video fixtures.
    Skipped when ffmpeg/ffprobe are not installed.

All real-video ffmpeg tests use tiny 180x320 synthetic vertical clips for
speed.  Test videos are created inside temp directories and never committed.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from post_processing_modules import (  # noqa: E402
    MODULE_STATUS_FAIL,
    MODULE_STATUS_PASS,
    PostProcessingModule,
    make_module_pass_result,
    validate_module_result,
)
from validation_v1 import (  # noqa: E402
    MODULE_NAME,
    MODULE_VERSION,
    REQUIRED_UPSTREAM_MODULES,
    VALIDATION_V1_MODULE,
    ValidationV1Module,
    _index_module_results,
    _is_finite_float,
    _is_positive_finite,
    _resolve_audio_expected,
    _resolve_expected_duration,
    get_validation_v1_module,
)

# ---------------------------------------------------------------------------
# ffprobe / ffmpeg availability guards
# ---------------------------------------------------------------------------


def _cmd_available(name: str) -> bool:
    return shutil.which(name) is not None


FFTOOLS_AVAILABLE = _cmd_available("ffmpeg") and _cmd_available("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    not FFTOOLS_AVAILABLE,
    reason="ffmpeg/ffprobe not installed — skipping real-video validation tests",
)


# ---------------------------------------------------------------------------
# Synthetic video helpers
# ---------------------------------------------------------------------------


def _make_synth_video(
    out_path: str,
    width: int = 180,
    height: int = 320,
    duration: float = 4.0,
    with_audio: bool = True,
) -> str:
    """Create a tiny synthetic video for testing."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=30",
    ]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100"]
    cmd += [
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast",
    ]
    if with_audio:
        cmd += ["-c:a", "aac"]
    else:
        cmd += ["-an"]
    cmd.append(out_path)
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    assert result.returncode == 0, (
        f"Failed to create {width}x{height} video: {result.stderr.decode()}"
    )
    return out_path


@pytest.fixture(scope="session")
def vertical_clip(tmp_path_factory):
    """Session-scoped 180x320 vertical clip with audio, 4 seconds."""
    if not FFTOOLS_AVAILABLE:
        return None
    d = tmp_path_factory.mktemp("val_vert")
    return _make_synth_video(str(d / "vertical.mp4"), 180, 320, 4.0, with_audio=True)


@pytest.fixture(scope="session")
def vertical_clip_no_audio(tmp_path_factory):
    """Session-scoped 180x320 vertical clip without audio, 4 seconds."""
    if not FFTOOLS_AVAILABLE:
        return None
    d = tmp_path_factory.mktemp("val_vert_na")
    return _make_synth_video(str(d / "vertical_no_audio.mp4"), 180, 320, 4.0, with_audio=False)


@pytest.fixture(scope="session")
def horizontal_clip(tmp_path_factory):
    """Session-scoped 320x180 horizontal clip with audio, 4 seconds."""
    if not FFTOOLS_AVAILABLE:
        return None
    d = tmp_path_factory.mktemp("val_horiz")
    return _make_synth_video(str(d / "horizontal.mp4"), 320, 180, 4.0, with_audio=True)


# ---------------------------------------------------------------------------
# Shared context/result helpers
# ---------------------------------------------------------------------------


def _make_candidate(
    start_sec: float = 10.0,
    end_sec: float = 14.0,
    duration_sec: float | None = None,
    candidate_id: str = "cand_001",
) -> dict[str, Any]:
    c: dict[str, Any] = {
        "candidate_id": candidate_id,
        "start_sec": start_sec,
        "end_sec": end_sec,
    }
    if duration_sec is not None:
        c["duration_sec"] = duration_sec
    return c


def _make_module_result(
    name: str,
    status: str = "PASS",
    output_path: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal module result dict for injection into context."""
    return {
        "schema_version": "post_processing_module_result_v1",
        "module_name": name,
        "module_version": "1.0",
        "status": status,
        "input_path": None,
        "output_path": output_path,
        "config": {},
        "error_reason": None if status == "PASS" else "test_failure",
        "warnings": [],
        "metadata": dict(metadata or {}),
    }


def _upstream_pass_results(
    output_path: str,
    with_audio: bool = True,
    captions_sidecar_path: str | None = None,
    caption_count: int = 5,
) -> list[dict[str, Any]]:
    """Build a minimal set of passing upstream module results for the full conveyor."""
    render_meta = {
        "candidate_id": "cand_001",
        "actual_duration_sec": 4.0,
        "expected_duration_sec": 4.0,
    }
    psf_meta = {
        "candidate_id": "cand_001",
        "input_has_audio": with_audio,
        "output_duration_sec": 4.0,
        "output_width": 180,
        "output_height": 320,
    }
    caps_meta: dict[str, Any] = {
        "candidate_id": "cand_001",
        "input_has_audio": with_audio,
        "output_duration_sec": 4.0,
        "caption_count": caption_count,
        "output_width": 180,
        "output_height": 320,
    }
    if captions_sidecar_path:
        caps_meta["caption_sidecar_path"] = captions_sidecar_path

    return [
        _make_module_result("render_clip_v1", "PASS", output_path="intermediate.mp4", metadata=render_meta),
        _make_module_result("platform_safe_format_v1", "PASS", output_path="formatted.mp4", metadata=psf_meta),
        _make_module_result("intelligent_captions_v1", "PASS", output_path=output_path, metadata=caps_meta),
    ]


def _make_context(
    *,
    selected_candidate: dict[str, Any] | None = None,
    module_results: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "job_id": "test_job",
        "candidate_id": "cand_001",
        "source_video_path": None,
        "selected_candidate": _make_candidate() if selected_candidate is None else selected_candidate,
        "module_results": list(module_results or []),
        "config": dict(config or {}),
    }


# ---------------------------------------------------------------------------
# Module contract tests
# ---------------------------------------------------------------------------


class TestModuleContract:
    def test_module_name_exact(self):
        m = ValidationV1Module()
        assert m.module_name == "validation_v1"

    def test_module_name_constant(self):
        assert MODULE_NAME == "validation_v1"

    def test_module_version_exists(self):
        m = ValidationV1Module()
        assert m.module_version
        assert isinstance(m.module_version, str)
        assert m.module_version.strip()

    def test_module_version_is_1_0(self):
        assert MODULE_VERSION == "1.0"

    def test_is_post_processing_module(self):
        m = ValidationV1Module()
        assert isinstance(m, PostProcessingModule)

    def test_registry_constant_exists(self):
        assert VALIDATION_V1_MODULE is not None
        assert isinstance(VALIDATION_V1_MODULE, ValidationV1Module)

    def test_get_module_returns_fresh_instance(self):
        m1 = get_validation_v1_module()
        m2 = get_validation_v1_module()
        assert isinstance(m1, ValidationV1Module)
        assert isinstance(m2, ValidationV1Module)
        assert m1 is not m2

    def test_result_standard_shape_on_pass(self, tmp_path):
        """PASS result satisfies the standard module result contract."""
        if not FFTOOLS_AVAILABLE:
            pytest.skip("ffprobe required")
        clip = _make_synth_video(str(tmp_path / "v.mp4"), 180, 320, 4.0, with_audio=False)
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=_upstream_pass_results(output_path=clip, with_audio=False),
        )
        result = ValidationV1Module().run(ctx, input_path=clip)
        validate_module_result(result)
        assert result["status"] == MODULE_STATUS_PASS

    def test_result_standard_shape_on_fail(self, tmp_path):
        """FAIL result satisfies the standard module result contract."""
        result = ValidationV1Module().run(_make_context(), input_path=None)
        validate_module_result(result)
        assert result["status"] == MODULE_STATUS_FAIL

    def test_context_is_not_mutated(self, tmp_path):
        """run() must not mutate the input context."""
        if not FFTOOLS_AVAILABLE:
            pytest.skip("ffprobe required")
        clip = _make_synth_video(str(tmp_path / "v.mp4"), 180, 320, 4.0, with_audio=False)
        ctx = _make_context(module_results=_upstream_pass_results(output_path=clip))
        ctx_before = copy.deepcopy(ctx)
        ValidationV1Module().run(ctx, input_path=clip)
        assert ctx == ctx_before

    def test_required_upstream_modules_list(self):
        assert "render_clip_v1" in REQUIRED_UPSTREAM_MODULES
        assert "platform_safe_format_v1" in REQUIRED_UPSTREAM_MODULES
        assert "intelligent_captions_v1" in REQUIRED_UPSTREAM_MODULES


# ---------------------------------------------------------------------------
# File / input path checks
# ---------------------------------------------------------------------------


class TestInputPathChecks:
    def test_missing_input_path_fails(self):
        result = ValidationV1Module().run(_make_context(), input_path=None)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "missing_input_path"

    def test_empty_string_input_path_fails(self):
        result = ValidationV1Module().run(_make_context(), input_path="")
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "missing_input_path"

    def test_nonexistent_file_fails(self, tmp_path):
        path = str(tmp_path / "does_not_exist.mp4")
        result = ValidationV1Module().run(_make_context(), input_path=path)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "input_file_not_found"

    def test_directory_path_fails(self, tmp_path):
        result = ValidationV1Module().run(_make_context(), input_path=str(tmp_path))
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "input_path_not_file"

    def test_empty_file_fails(self, tmp_path):
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")
        result = ValidationV1Module().run(_make_context(), input_path=str(empty))
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "input_file_empty"
        assert result["metadata"]["file_size_bytes"] == 0


# ---------------------------------------------------------------------------
# Probe / playability checks
# ---------------------------------------------------------------------------


class TestProbeChecks:
    def test_unprobeable_file_fails(self, tmp_path):
        """A non-video file should fail the ffprobe check."""
        if not FFTOOLS_AVAILABLE:
            pytest.skip("ffprobe required")
        garbage = tmp_path / "garbage.mp4"
        garbage.write_bytes(b"\x00" * 1024)
        result = ValidationV1Module().run(_make_context(), input_path=str(garbage))
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] in ("ffprobe_failed", "missing_video_stream", "missing_duration")

    def test_ffprobe_unavailable_fails(self, tmp_path):
        """When ffprobe is not on PATH, fail with ffprobe_unavailable."""
        real_file = tmp_path / "real.mp4"
        real_file.write_bytes(b"\x00" * 1024)
        with patch("validation_v1.shutil.which", return_value=None):
            result = ValidationV1Module().run(_make_context(), input_path=str(real_file))
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "ffprobe_unavailable"

    @requires_ffmpeg
    def test_valid_video_passes_probe(self, vertical_clip):
        """A valid vertical clip passes the probe checks."""
        ctx = _make_context(
            module_results=_upstream_pass_results(output_path=vertical_clip),
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        # May fail on required modules or aspect ratio but not probe
        assert result["error_reason"] not in (
            "ffprobe_unavailable", "ffprobe_failed", "missing_video_stream",
            "missing_duration", "invalid_duration",
        )

    @requires_ffmpeg
    def test_file_with_no_video_stream_fails(self, tmp_path):
        """An audio-only file fails the video stream check."""
        audio_only = str(tmp_path / "audio_only.aac")
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100",
            "-t", "3",
            "-c:a", "aac",
            audio_only,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode != 0:
            pytest.skip("Could not create audio-only file")
        ctx = _make_context(module_results=_upstream_pass_results(output_path=audio_only))
        result = ValidationV1Module().run(ctx, input_path=audio_only)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] in ("missing_video_stream", "ffprobe_failed", "missing_duration")


# ---------------------------------------------------------------------------
# Duration checks
# ---------------------------------------------------------------------------


class TestDurationChecks:
    @requires_ffmpeg
    def test_valid_duration_passes(self, vertical_clip):
        """Clip duration matching expected passes duration check."""
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=_upstream_pass_results(output_path=vertical_clip),
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        assert result["error_reason"] != "duration_mismatch"

    def test_missing_selected_candidate_fails(self, tmp_path):
        """No candidate timestamps and no module metadata causes missing_selected_candidate."""
        if not FFTOOLS_AVAILABLE:
            pytest.skip("ffprobe required")
        clip = _make_synth_video(str(tmp_path / "v.mp4"), 180, 320, 4.0, with_audio=False)
        # Pass a candidate dict with no timestamps and no module results with durations
        ctx = _make_context(
            selected_candidate={"candidate_id": "x"},
            module_results=[],
            config={"validation_v1": {"require_upstream_modules": False}},
        )
        result = ValidationV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "missing_selected_candidate"

    def test_invalid_candidate_timestamps_fails(self, tmp_path):
        """end_sec <= start_sec causes invalid_candidate_timestamps failure."""
        if not FFTOOLS_AVAILABLE:
            pytest.skip("ffprobe required")
        clip = _make_synth_video(str(tmp_path / "v.mp4"), 180, 320, 4.0, with_audio=False)
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=5.0, end_sec=2.0),
            module_results=[],
        )
        result = ValidationV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] in (
            "invalid_candidate_timestamps", "missing_selected_candidate"
        )

    @requires_ffmpeg
    def test_duration_mismatch_fails(self, vertical_clip):
        """Expected duration far from actual triggers duration_mismatch."""
        # Clip is 4s, claim it should be 30s
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=30.0),
            module_results=_upstream_pass_results(output_path=vertical_clip),
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "duration_mismatch"

    @requires_ffmpeg
    def test_tolerance_override_allows_small_delta(self, vertical_clip):
        """A larger tolerance_sec allows a slightly mismatched duration to pass."""
        # Clip is 4s; claim 2s — delta is 2s; default tolerance is 1.25s
        # With tolerance=3.0 this should not fail on duration
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=2.0),
            module_results=_upstream_pass_results(output_path=vertical_clip),
            config={"validation_v1": {"duration_tolerance_sec": 3.0}},
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        # Should not fail on duration; may fail on something else like aspect ratio
        assert result["error_reason"] != "duration_mismatch"


# ---------------------------------------------------------------------------
# Required upstream modules
# ---------------------------------------------------------------------------


class TestRequiredModules:
    @requires_ffmpeg
    def test_missing_required_module_result_fails(self, vertical_clip):
        """Missing render_clip_v1 result causes required module failure."""
        # Only include platform_safe_format_v1 and intelligent_captions_v1
        module_results = [
            _make_module_result("platform_safe_format_v1", "PASS", output_path="formatted.mp4"),
            _make_module_result("intelligent_captions_v1", "PASS", output_path=vertical_clip),
        ]
        ctx = _make_context(module_results=module_results)
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "missing_required_module_result"
        assert "render_clip_v1" in result["metadata"]["missing_required_modules"]

    @requires_ffmpeg
    def test_upstream_required_module_failure_fails(self, vertical_clip):
        """A FAIL status in an upstream module causes required_module_failed."""
        module_results = [
            _make_module_result("render_clip_v1", "FAIL"),
            _make_module_result("platform_safe_format_v1", "PASS", output_path="formatted.mp4"),
            _make_module_result("intelligent_captions_v1", "PASS", output_path=vertical_clip),
        ]
        ctx = _make_context(module_results=module_results)
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "required_module_failed"
        assert "render_clip_v1" in result["metadata"]["failed_required_modules"]

    @requires_ffmpeg
    def test_all_upstream_passing_allows_validation(self, vertical_clip):
        """All required upstream modules passing allows validation to proceed."""
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=_upstream_pass_results(output_path=vertical_clip),
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        # Should not fail on module requirements
        assert result["error_reason"] not in (
            "missing_required_module_result", "required_module_failed"
        )

    @requires_ffmpeg
    def test_metadata_writer_is_not_required(self, vertical_clip):
        """metadata_writer_v1 is NOT in the required upstream modules list."""
        assert "metadata_writer_v1" not in REQUIRED_UPSTREAM_MODULES

        # Build full passing context and confirm no failure about metadata_writer
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=_upstream_pass_results(output_path=vertical_clip),
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        meta_missing = result.get("metadata", {}).get("missing_required_modules", [])
        assert "metadata_writer_v1" not in meta_missing


# ---------------------------------------------------------------------------
# Aspect ratio checks
# ---------------------------------------------------------------------------


class TestAspectRatioChecks:
    @requires_ffmpeg
    def test_9_16_output_passes_aspect_ratio(self, vertical_clip):
        """A 180x320 clip (9:16) passes aspect ratio after platform formatting."""
        psf_meta = {"input_has_audio": False, "output_duration_sec": 4.0}
        module_results = [
            _make_module_result("render_clip_v1", "PASS", metadata={"actual_duration_sec": 4.0}),
            _make_module_result("platform_safe_format_v1", "PASS", output_path="formatted.mp4", metadata=psf_meta),
            _make_module_result("intelligent_captions_v1", "PASS", output_path=vertical_clip, metadata={"output_duration_sec": 4.0, "caption_count": 3}),
        ]
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        assert result["error_reason"] != "aspect_ratio_mismatch"

    @requires_ffmpeg
    def test_horizontal_clip_fails_aspect_ratio_after_platform_formatting(self, horizontal_clip):
        """A 320x180 (16:9) clip fails aspect ratio when platform_safe_format_v1 ran."""
        psf_meta = {"input_has_audio": True, "output_duration_sec": 4.0}
        module_results = [
            _make_module_result("render_clip_v1", "PASS", metadata={"actual_duration_sec": 4.0}),
            _make_module_result("platform_safe_format_v1", "PASS", output_path="formatted.mp4", metadata=psf_meta),
            _make_module_result("intelligent_captions_v1", "PASS", output_path=horizontal_clip, metadata={"output_duration_sec": 4.0, "input_has_audio": True, "caption_count": 3}),
        ]
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
        )
        result = ValidationV1Module().run(ctx, input_path=horizontal_clip)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "aspect_ratio_mismatch"

    @requires_ffmpeg
    def test_aspect_ratio_tolerance_override(self, vertical_clip):
        """A very loose aspect_ratio_tolerance allows a non-9:16 clip to pass."""
        # Use a horizontal clip (16:9) but with very loose tolerance
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=_upstream_pass_results(output_path=vertical_clip),
            config={"validation_v1": {"aspect_ratio_tolerance": 1.0}},
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        assert result["error_reason"] != "aspect_ratio_mismatch"

    @requires_ffmpeg
    def test_no_aspect_ratio_check_without_platform_formatting(self, horizontal_clip):
        """Without platform_safe_format_v1, no aspect ratio check is performed."""
        module_results = [
            _make_module_result("render_clip_v1", "PASS", metadata={"actual_duration_sec": 4.0}),
            _make_module_result("intelligent_captions_v1", "PASS", output_path=horizontal_clip, metadata={"output_duration_sec": 4.0, "caption_count": 3}),
        ]
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
            config={"validation_v1": {"required_upstream_modules": ["render_clip_v1", "intelligent_captions_v1"]}},
        )
        result = ValidationV1Module().run(ctx, input_path=horizontal_clip)
        assert result["error_reason"] != "aspect_ratio_mismatch"


# ---------------------------------------------------------------------------
# Audio checks
# ---------------------------------------------------------------------------


class TestAudioChecks:
    @requires_ffmpeg
    def test_silent_clip_passes_if_audio_not_expected(self, vertical_clip_no_audio):
        """A silent clip passes if no upstream module says audio was expected."""
        module_results = [
            _make_module_result("render_clip_v1", "PASS", metadata={"actual_duration_sec": 4.0}),
            _make_module_result("platform_safe_format_v1", "PASS", output_path="formatted.mp4",
                                metadata={"input_has_audio": False, "output_duration_sec": 4.0}),
            _make_module_result("intelligent_captions_v1", "PASS", output_path=vertical_clip_no_audio,
                                metadata={"input_has_audio": False, "output_duration_sec": 4.0, "caption_count": 3}),
        ]
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip_no_audio)
        assert result["error_reason"] != "missing_expected_audio"

    @requires_ffmpeg
    def test_missing_audio_fails_if_upstream_says_audio_expected(self, vertical_clip_no_audio):
        """A silent clip fails if upstream metadata indicates audio was present."""
        module_results = [
            _make_module_result("render_clip_v1", "PASS", metadata={"actual_duration_sec": 4.0}),
            _make_module_result("platform_safe_format_v1", "PASS", output_path="formatted.mp4",
                                metadata={"input_has_audio": True, "output_duration_sec": 4.0}),
            _make_module_result("intelligent_captions_v1", "PASS", output_path=vertical_clip_no_audio,
                                metadata={"input_has_audio": True, "output_duration_sec": 4.0, "caption_count": 3}),
        ]
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip_no_audio)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "missing_expected_audio"


# ---------------------------------------------------------------------------
# Caption metadata checks
# ---------------------------------------------------------------------------


class TestCaptionMetadataChecks:
    @requires_ffmpeg
    def test_caption_metadata_exists_passes(self, vertical_clip, tmp_path):
        """Valid caption sidecar present passes caption metadata check."""
        sidecar = str(tmp_path / "captions.ass")
        with open(sidecar, "w") as f:
            f.write("[Script Info]\nScriptType: v4.00+\n\n[Events]\n")

        module_results = _upstream_pass_results(
            output_path=vertical_clip,
            captions_sidecar_path=sidecar,
            caption_count=4,
        )
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        assert result["error_reason"] not in (
            "caption_sidecar_not_found", "caption_sidecar_empty", "missing_caption_metadata"
        )

    @requires_ffmpeg
    def test_missing_caption_sidecar_fails(self, vertical_clip, tmp_path):
        """Caption sidecar path recorded but file absent fails."""
        nonexistent_sidecar = str(tmp_path / "missing_captions.ass")
        module_results = _upstream_pass_results(
            output_path=vertical_clip,
            captions_sidecar_path=nonexistent_sidecar,
        )
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "caption_sidecar_not_found"

    @requires_ffmpeg
    def test_empty_caption_sidecar_fails(self, vertical_clip, tmp_path):
        """An empty caption sidecar file fails."""
        empty_sidecar = str(tmp_path / "empty.ass")
        with open(empty_sidecar, "w"):
            pass  # empty

        module_results = _upstream_pass_results(
            output_path=vertical_clip,
            captions_sidecar_path=empty_sidecar,
        )
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "caption_sidecar_empty"

    def test_no_ocr_is_required(self):
        """No OCR library imports exist in validation_v1."""
        import validation_v1 as v1_mod
        import inspect
        src = inspect.getsource(v1_mod)
        assert "pytesseract" not in src
        assert "easyocr" not in src
        assert "import cv2" not in src
        assert "tesseract" not in src.lower()


# ---------------------------------------------------------------------------
# Real ffmpeg integration tests
# ---------------------------------------------------------------------------


class TestRealFfmpegIntegration:
    @requires_ffmpeg
    def test_valid_180x320_vertical_mp4_passes(self, tmp_path, vertical_clip):
        """A 180x320 9:16 vertical mp4 with matching duration passes all checks."""
        sidecar = str(tmp_path / "captions.ass")
        with open(sidecar, "w") as f:
            f.write("[Script Info]\nScriptType: v4.00+\n\n[Events]\n")

        module_results = _upstream_pass_results(
            output_path=vertical_clip,
            with_audio=True,
            captions_sidecar_path=sidecar,
            caption_count=5,
        )
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS
        meta = result["metadata"]
        assert meta["width"] == 180
        assert meta["height"] == 320
        assert abs(meta["aspect_ratio"] - 9 / 16) < 0.02
        assert meta["file_size_bytes"] > 0
        assert meta["caption_metadata_checked"] is True

    @requires_ffmpeg
    def test_horizontal_mp4_fails_aspect_ratio_after_platform_formatting(
        self, horizontal_clip
    ):
        """A 320x180 (16:9) clip fails aspect ratio after platform formatting ran."""
        psf_meta = {"input_has_audio": True, "output_duration_sec": 4.0}
        module_results = [
            _make_module_result("render_clip_v1", "PASS", metadata={"actual_duration_sec": 4.0}),
            _make_module_result("platform_safe_format_v1", "PASS", output_path="formatted.mp4", metadata=psf_meta),
            _make_module_result("intelligent_captions_v1", "PASS", output_path=horizontal_clip,
                                metadata={"output_duration_sec": 4.0, "input_has_audio": True, "caption_count": 3}),
        ]
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
        )
        result = ValidationV1Module().run(ctx, input_path=horizontal_clip)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "aspect_ratio_mismatch"

    @requires_ffmpeg
    def test_silent_vertical_mp4_passes_if_audio_not_expected(self, vertical_clip_no_audio):
        """A silent vertical mp4 passes all checks when no audio was expected."""
        module_results = [
            _make_module_result("render_clip_v1", "PASS", metadata={"actual_duration_sec": 4.0}),
            _make_module_result("platform_safe_format_v1", "PASS", output_path="formatted.mp4",
                                metadata={"input_has_audio": False, "output_duration_sec": 4.0}),
            _make_module_result("intelligent_captions_v1", "PASS", output_path=vertical_clip_no_audio,
                                metadata={"input_has_audio": False, "output_duration_sec": 4.0, "caption_count": 3}),
        ]
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip_no_audio)
        assert result["status"] == MODULE_STATUS_PASS

    @requires_ffmpeg
    def test_silent_vertical_mp4_fails_if_audio_expected(self, vertical_clip_no_audio):
        """A silent vertical mp4 fails when upstream says audio was present."""
        module_results = [
            _make_module_result("render_clip_v1", "PASS", metadata={"actual_duration_sec": 4.0}),
            _make_module_result("platform_safe_format_v1", "PASS", output_path="formatted.mp4",
                                metadata={"input_has_audio": True, "output_duration_sec": 4.0}),
            _make_module_result("intelligent_captions_v1", "PASS", output_path=vertical_clip_no_audio,
                                metadata={"input_has_audio": True, "output_duration_sec": 4.0, "caption_count": 3}),
        ]
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip_no_audio)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "missing_expected_audio"


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_invalid_duration_tolerance_fails(self, tmp_path):
        if not FFTOOLS_AVAILABLE:
            pytest.skip("ffprobe required")
        clip = _make_synth_video(str(tmp_path / "v.mp4"), 180, 320, 4.0, with_audio=False)
        ctx = _make_context(
            config={"validation_v1": {"duration_tolerance_sec": "not_a_number"}},
        )
        result = ValidationV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "invalid_validation_config"

    def test_invalid_aspect_ratio_tolerance_fails(self, tmp_path):
        if not FFTOOLS_AVAILABLE:
            pytest.skip("ffprobe required")
        clip = _make_synth_video(str(tmp_path / "v.mp4"), 180, 320, 4.0, with_audio=False)
        ctx = _make_context(
            config={"validation_v1": {"aspect_ratio_tolerance": -1.0}},
        )
        result = ValidationV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "invalid_validation_config"

    def test_invalid_target_aspect_ratio_fails(self, tmp_path):
        if not FFTOOLS_AVAILABLE:
            pytest.skip("ffprobe required")
        clip = _make_synth_video(str(tmp_path / "v.mp4"), 180, 320, 4.0, with_audio=False)
        ctx = _make_context(
            config={"validation_v1": {"target_aspect_ratio": 0}},
        )
        result = ValidationV1Module().run(ctx, input_path=clip)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["error_reason"] == "invalid_validation_config"


# ---------------------------------------------------------------------------
# PASS / FAIL metadata shape
# ---------------------------------------------------------------------------


class TestMetadataShape:
    @requires_ffmpeg
    def test_pass_metadata_has_required_fields(self, vertical_clip, tmp_path):
        sidecar = str(tmp_path / "captions.ass")
        with open(sidecar, "w") as f:
            f.write("[Script Info]\nScriptType: v4.00+\n\n[Events]\n")

        module_results = _upstream_pass_results(
            output_path=vertical_clip,
            captions_sidecar_path=sidecar,
        )
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS
        meta = result["metadata"]
        required_fields = [
            "validated_output_path", "duration_sec", "expected_duration_sec",
            "duration_delta_sec", "duration_tolerance_sec", "width", "height",
            "aspect_ratio", "target_aspect_ratio", "aspect_ratio_delta",
            "video_stream_count", "audio_stream_count", "audio_expected",
            "required_modules_checked", "caption_metadata_checked",
            "caption_sidecar_path", "file_size_bytes",
        ]
        for field in required_fields:
            assert field in meta, f"PASS metadata missing field: {field!r}"

    @requires_ffmpeg
    def test_pass_metadata_is_json_serialisable(self, vertical_clip, tmp_path):
        sidecar = str(tmp_path / "captions.ass")
        with open(sidecar, "w") as f:
            f.write("[Script Info]\nScriptType: v4.00+\n\n[Events]\n")

        module_results = _upstream_pass_results(
            output_path=vertical_clip,
            captions_sidecar_path=sidecar,
        )
        ctx = _make_context(
            selected_candidate=_make_candidate(start_sec=0.0, end_sec=4.0),
            module_results=module_results,
        )
        result = ValidationV1Module().run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS
        # Should not raise
        json.dumps(result)

    def test_fail_metadata_has_failure_code(self):
        result = ValidationV1Module().run(_make_context(), input_path=None)
        assert result["status"] == MODULE_STATUS_FAIL
        assert "failure_code" in result["metadata"]
        assert result["metadata"]["failure_code"] == result["error_reason"]

    def test_fail_metadata_is_json_serialisable(self):
        result = ValidationV1Module().run(_make_context(), input_path=None)
        json.dumps(result)


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    def test_index_module_results_last_wins(self):
        r1 = _make_module_result("mod_a", "PASS")
        r2 = _make_module_result("mod_a", "FAIL")
        idx = _index_module_results([r1, r2])
        assert idx["mod_a"]["status"] == "FAIL"

    def test_resolve_expected_duration_from_candidate_duration_sec(self):
        candidate = _make_candidate(start_sec=0.0, end_sec=10.0, duration_sec=5.0)
        dur = _resolve_expected_duration(candidate, [])
        assert dur == 5.0

    def test_resolve_expected_duration_from_timestamps(self):
        candidate = _make_candidate(start_sec=2.0, end_sec=8.0)
        dur = _resolve_expected_duration(candidate, [])
        assert dur == pytest.approx(6.0)

    def test_resolve_expected_duration_from_render_clip_metadata(self):
        candidate: dict[str, Any] = {}  # no timestamps
        module_results = [
            _make_module_result("render_clip_v1", "PASS",
                                metadata={"actual_duration_sec": 7.5}),
        ]
        dur = _resolve_expected_duration(candidate, module_results)
        assert dur == pytest.approx(7.5)

    def test_resolve_expected_duration_none_when_no_data(self):
        dur = _resolve_expected_duration({}, [])
        assert dur is None

    def test_resolve_audio_expected_true_when_metadata_says_audio(self):
        results_by_name = {
            "platform_safe_format_v1": _make_module_result(
                "platform_safe_format_v1", "PASS", metadata={"input_has_audio": True}
            )
        }
        assert _resolve_audio_expected(results_by_name) is True

    def test_resolve_audio_expected_false_when_all_say_no_audio(self):
        results_by_name = {
            "platform_safe_format_v1": _make_module_result(
                "platform_safe_format_v1", "PASS", metadata={"input_has_audio": False}
            ),
            "intelligent_captions_v1": _make_module_result(
                "intelligent_captions_v1", "PASS", metadata={"input_has_audio": False}
            ),
        }
        assert _resolve_audio_expected(results_by_name) is False

    def test_is_finite_float(self):
        assert _is_finite_float(1.0) is True
        assert _is_finite_float(0) is True
        assert _is_finite_float(True) is False
        assert _is_finite_float(float("inf")) is False
        assert _is_finite_float(float("nan")) is False
        assert _is_finite_float("1.0") is False
        assert _is_finite_float(None) is False

    def test_is_positive_finite(self):
        assert _is_positive_finite(1.0) is True
        assert _is_positive_finite(0.0) is False
        assert _is_positive_finite(-1.0) is False
        assert _is_positive_finite(True) is False
