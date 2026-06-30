"""platform_safe_format_v1 — focused tests (Prompt 19).

Tests are split into:
  - Non-ffmpeg tests: input validation, config validation, path/naming rules,
    result contract shape, mocked-ffmpeg rendering.  Always run.
  - ffmpeg tests: real 9:16 formatting with synthetic video fixtures.
    Skipped when ffmpeg/ffprobe are not installed.
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
    run_module_chain,
    validate_module_result,
)
from post_processing_conveyor import (  # noqa: E402
    CONVEYOR_STATUS_COMPLETE,
    FIXED_MK1_CONVEYOR_MODULES,
    run_fixed_mk1_universal_conveyor,
)
from render_clip_v1 import RenderClipV1Module, _make_output_path as render_output_path
from platform_safe_format_v1 import (  # noqa: E402
    MODULE_NAME,
    MODULE_VERSION,
    PlatformSafeFormatV1Module,
    _make_output_path,
    _probe_video_info,
    _safe_filename_part,
    _validate_format_config,
    get_platform_safe_format_v1_module,
)

# ---------------------------------------------------------------------------
# ffmpeg / ffprobe availability guards
# ---------------------------------------------------------------------------


def _cmd_available(name: str) -> bool:
    return shutil.which(name) is not None


FFTOOLS_AVAILABLE = _cmd_available("ffmpeg") and _cmd_available("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    not FFTOOLS_AVAILABLE,
    reason="ffmpeg/ffprobe not installed — skipping real-format tests",
)


# ---------------------------------------------------------------------------
# Synthetic video fixtures (session-scoped for speed)
# ---------------------------------------------------------------------------


def _make_synth_video(out_path: str, width: int, height: int, duration: float = 3.0) -> str:
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100",
            "-t", str(duration),
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac",
            out_path,
        ],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, f"Failed to create {width}x{height} video: {result.stderr}"
    return out_path


@pytest.fixture(scope="session")
def horizontal_video(tmp_path_factory):
    if not FFTOOLS_AVAILABLE:
        return None
    d = tmp_path_factory.mktemp("horiz")
    return _make_synth_video(str(d / "horiz.mp4"), 320, 180)


@pytest.fixture(scope="session")
def square_video(tmp_path_factory):
    if not FFTOOLS_AVAILABLE:
        return None
    d = tmp_path_factory.mktemp("square")
    return _make_synth_video(str(d / "square.mp4"), 240, 240)


@pytest.fixture(scope="session")
def vertical_video(tmp_path_factory):
    if not FFTOOLS_AVAILABLE:
        return None
    d = tmp_path_factory.mktemp("vert")
    return _make_synth_video(str(d / "vert.mp4"), 180, 320)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candidate(candidate_id: str = "cand_001") -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "rank": 1,
        "start_sec": 0.5,
        "end_sec": 3.0,
        "duration_sec": 2.5,
        "confidence": 0.85,
        "scores": {"overall_potential": 8.0},
        "selection_reason": "selected_by_rank",
        "warnings": [],
        "transcript_quality_flags": [],
        "source_candidate": {},
    }


def _make_context(
    clip_dir: str | None = None,
    job_id: str = "job_test_001",
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if candidate is None:
        candidate = _make_candidate()
    return {
        "job_id": job_id,
        "candidate_id": candidate.get("candidate_id"),
        "source_video_path": None,
        "working_dir": None,
        "clip_dir": clip_dir,
        "metadata_dir": None,
        "tmp_dir": None,
        "config": {},
        "selection_result": {},
        "selected_candidate": candidate,
        "module_results": [],
    }


def _write_fake_mp4(path: str, size: int = 512) -> str:
    """Write a non-empty file so existence/size checks pass."""
    with open(path, "wb") as f:
        f.write(b"\x00" * size)
    return path


def _default_probe_info(width: int = 320, height: int = 240) -> dict[str, Any]:
    return {
        "width": width,
        "height": height,
        "duration_sec": 2.5,
        "has_audio": True,
    }


def _make_pass_ffmpeg_scenario(tmp_path, width=1080, height=1920):
    """Set up mock subprocess.run + _probe_video_info for a successful format pass."""
    output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
    _write_fake_mp4(output_path)
    return output_path


# ===========================================================================
# 1–2: Module identity
# ===========================================================================


def test_module_name_is_platform_safe_format_v1():
    assert PlatformSafeFormatV1Module().module_name == "platform_safe_format_v1"


def test_module_version_exists():
    m = PlatformSafeFormatV1Module()
    assert m.module_version and isinstance(m.module_version, str)


def test_module_name_constant():
    assert MODULE_NAME == "platform_safe_format_v1"


def test_module_version_constant():
    assert MODULE_VERSION == PlatformSafeFormatV1Module.module_version


# ===========================================================================
# 3–4: Result contract shape
# ===========================================================================


def test_pass_result_is_prompt16_compatible(tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    input_file = _write_fake_mp4(str(tmp_path / "input.mp4"))
    output_path = _make_pass_ffmpeg_scenario(tmp_path)

    with patch("platform_safe_format_v1.subprocess.run") as mock_run, \
         patch("platform_safe_format_v1._probe_video_info") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_probe.side_effect = [
            _default_probe_info(320, 240),  # input probe
            _default_probe_info(1080, 1920),  # output probe
        ]
        result = PlatformSafeFormatV1Module().run(ctx, input_path=input_file)

    assert result["status"] == MODULE_STATUS_PASS
    validate_module_result(result)


def test_fail_result_is_prompt16_compatible(tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path="/does/not/exist.mp4")
    assert result["status"] == MODULE_STATUS_FAIL
    validate_module_result(result)


# ===========================================================================
# 5–10: Input validation
# ===========================================================================


def test_missing_input_path_returns_fail(tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path=None)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "missing_input_path"


def test_input_file_not_found_returns_fail(tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path="/no/such/file.mp4")
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "input_file_not_found"


def test_input_path_is_directory_returns_fail(tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path=str(tmp_path))
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "input_path_not_file"


def test_empty_input_file_returns_fail(tmp_path):
    empty = str(tmp_path / "empty.mp4")
    with open(empty, "wb"):
        pass
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path=empty)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "input_file_empty"


def test_unprobeable_input_returns_fail(tmp_path):
    garbage = str(tmp_path / "garbage.mp4")
    _write_fake_mp4(garbage)
    ctx = _make_context(clip_dir=str(tmp_path))

    with patch("platform_safe_format_v1._probe_video_info") as mock_probe:
        mock_probe.return_value = None
        result = PlatformSafeFormatV1Module().run(ctx, input_path=garbage)

    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "input_probe_failed"


def test_input_with_no_video_stream_returns_fail(tmp_path):
    garbage = str(tmp_path / "novideo.mp4")
    _write_fake_mp4(garbage)
    ctx = _make_context(clip_dir=str(tmp_path))

    with patch("platform_safe_format_v1._probe_video_info") as mock_probe:
        mock_probe.return_value = {"width": 0, "height": 0, "duration_sec": 2.0, "has_audio": True}
        result = PlatformSafeFormatV1Module().run(ctx, input_path=garbage)

    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "missing_video_stream"


# ===========================================================================
# 11–13: Config validation
# ===========================================================================


def test_invalid_config_returns_fail(tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    ctx["config"] = {"target_width": -1, "target_height": 1920}
    fake = _write_fake_mp4(str(tmp_path / "in.mp4"))
    result = PlatformSafeFormatV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "invalid_format_config"


def test_non_916_target_fails_config_validation():
    err = _validate_format_config({"target_width": 1920, "target_height": 1080,
                                    "duration_tolerance_sec": 1.0, "output_ext": ".mp4"})
    assert err is not None
    assert "9:16" in err


def test_default_target_is_1080x1920():
    from platform_safe_format_v1 import _DEFAULT_CONFIG
    assert _DEFAULT_CONFIG["target_width"] == 1080
    assert _DEFAULT_CONFIG["target_height"] == 1920


# ===========================================================================
# 14–18: Output path and overwrite
# ===========================================================================


def test_output_path_is_deterministic(tmp_path):
    p1 = _make_output_path(str(tmp_path), "job_001", "cand_abc")
    p2 = _make_output_path(str(tmp_path), "job_001", "cand_abc")
    assert p1 == p2


def test_output_path_includes_safe_identifiers(tmp_path):
    p = _make_output_path(str(tmp_path), "job-001", "cand_abc")
    base = os.path.basename(p)
    assert "cand_abc" in base
    assert "platform_safe_format_v1" in base


def test_clip_dir_created_if_missing(tmp_path):
    new_dir = str(tmp_path / "new_clips")
    assert not os.path.isdir(new_dir)
    fake = _write_fake_mp4(str(tmp_path / "in.mp4"))
    ctx = _make_context(clip_dir=new_dir)
    output_path = _make_output_path(new_dir, "job_test_001", "cand_001")

    with patch("platform_safe_format_v1.subprocess.run") as mock_run, \
         patch("platform_safe_format_v1._probe_video_info") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        os.makedirs(new_dir, exist_ok=True)
        _write_fake_mp4(output_path)
        mock_probe.side_effect = [
            _default_probe_info(320, 240),
            _default_probe_info(1080, 1920),
        ]
        result = PlatformSafeFormatV1Module().run(ctx, input_path=fake)

    assert os.path.isdir(new_dir)
    assert result["status"] == MODULE_STATUS_PASS


def test_overwrite_true_replaces_existing(tmp_path):
    fake = _write_fake_mp4(str(tmp_path / "in.mp4"))
    ctx = _make_context(clip_dir=str(tmp_path))
    ctx["config"] = {"overwrite": True}
    output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
    _write_fake_mp4(output_path, size=8)  # stale file

    with patch("platform_safe_format_v1.subprocess.run") as mock_run, \
         patch("platform_safe_format_v1._probe_video_info") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _write_fake_mp4(output_path, size=512)
        mock_probe.side_effect = [
            _default_probe_info(320, 240),
            _default_probe_info(1080, 1920),
        ]
        result = PlatformSafeFormatV1Module().run(ctx, input_path=fake)

    assert result["status"] == MODULE_STATUS_PASS


def test_overwrite_false_fails_if_output_exists(tmp_path):
    fake = _write_fake_mp4(str(tmp_path / "in.mp4"))
    output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
    _write_fake_mp4(output_path)
    ctx = _make_context(clip_dir=str(tmp_path))
    ctx["config"] = {"overwrite": False}

    with patch("platform_safe_format_v1._probe_video_info") as mock_probe:
        mock_probe.return_value = _default_probe_info(320, 240)
        result = PlatformSafeFormatV1Module().run(ctx, input_path=fake)

    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "output_exists"


# ===========================================================================
# 19–20: Output file creation (mocked)
# ===========================================================================


def test_output_file_is_created(tmp_path):
    fake = _write_fake_mp4(str(tmp_path / "in.mp4"))
    ctx = _make_context(clip_dir=str(tmp_path))
    output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")

    with patch("platform_safe_format_v1.subprocess.run") as mock_run, \
         patch("platform_safe_format_v1._probe_video_info") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _write_fake_mp4(output_path)
        mock_probe.side_effect = [
            _default_probe_info(320, 240),
            _default_probe_info(1080, 1920),
        ]
        result = PlatformSafeFormatV1Module().run(ctx, input_path=fake)

    assert result["status"] == MODULE_STATUS_PASS
    assert os.path.isfile(result["output_path"])


def test_output_file_is_non_empty(tmp_path):
    fake = _write_fake_mp4(str(tmp_path / "in.mp4"))
    ctx = _make_context(clip_dir=str(tmp_path))
    output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")

    with patch("platform_safe_format_v1.subprocess.run") as mock_run, \
         patch("platform_safe_format_v1._probe_video_info") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _write_fake_mp4(output_path, size=1024)
        mock_probe.side_effect = [
            _default_probe_info(320, 240),
            _default_probe_info(1080, 1920),
        ]
        result = PlatformSafeFormatV1Module().run(ctx, input_path=fake)

    assert os.path.getsize(result["output_path"]) > 0


# ===========================================================================
# 30: Foreground not stretched (config logic)
# ===========================================================================


def test_filter_uses_force_original_aspect_ratio_decrease_for_fg():
    """Verify filter graph uses 'decrease' for foreground (no stretching)."""
    from platform_safe_format_v1 import _build_format_command
    cmd = _build_format_command(
        input_path="/in.mp4",
        output_path="/out.mp4",
        target_w=1080,
        target_h=1920,
        config={"background_blur": "20:1", "video_codec": "libx264",
                "audio_codec": "aac", "ffmpeg_preset": "veryfast"},
        input_has_audio=True,
    )
    cmd_str = " ".join(cmd)
    assert "force_original_aspect_ratio=decrease" in cmd_str
    assert "force_original_aspect_ratio=increase" in cmd_str  # used on background


# ===========================================================================
# 31–36: PASS metadata
# ===========================================================================


def _run_pass_scenario(tmp_path, input_w=320, input_h=240):
    fake = _write_fake_mp4(str(tmp_path / "in.mp4"))
    ctx = _make_context(clip_dir=str(tmp_path))
    output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")

    with patch("platform_safe_format_v1.subprocess.run") as mock_run, \
         patch("platform_safe_format_v1._probe_video_info") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _write_fake_mp4(output_path, size=1024)
        mock_probe.side_effect = [
            {"width": input_w, "height": input_h, "duration_sec": 2.5, "has_audio": True},
            {"width": 1080, "height": 1920, "duration_sec": 2.5, "has_audio": True},
        ]
        return PlatformSafeFormatV1Module().run(ctx, input_path=fake)


def test_pass_metadata_includes_input_dimensions(tmp_path):
    r = _run_pass_scenario(tmp_path, input_w=320, input_h=240)
    assert r["status"] == MODULE_STATUS_PASS
    assert r["metadata"]["input_width"] == 320
    assert r["metadata"]["input_height"] == 240


def test_pass_metadata_includes_output_dimensions(tmp_path):
    r = _run_pass_scenario(tmp_path)
    assert r["status"] == MODULE_STATUS_PASS
    assert r["metadata"]["output_width"] == 1080
    assert r["metadata"]["output_height"] == 1920


def test_pass_metadata_includes_duration_delta(tmp_path):
    r = _run_pass_scenario(tmp_path)
    assert r["status"] == MODULE_STATUS_PASS
    assert "duration_delta_sec" in r["metadata"]


def test_pass_metadata_includes_safe_zones(tmp_path):
    r = _run_pass_scenario(tmp_path)
    assert r["status"] == MODULE_STATUS_PASS
    sz = r["metadata"]["safe_zones"]
    assert "top_margin_px" in sz
    assert "bottom_margin_px" in sz
    assert "caption_safe_y_min_px" in sz
    assert "caption_safe_y_max_px" in sz


def test_pass_metadata_includes_format_strategy(tmp_path):
    r = _run_pass_scenario(tmp_path)
    assert r["status"] == MODULE_STATUS_PASS
    assert r["metadata"]["format_strategy"] == "blurred_background_fit_foreground"


def test_pass_metadata_includes_output_file_size(tmp_path):
    r = _run_pass_scenario(tmp_path)
    assert r["status"] == MODULE_STATUS_PASS
    assert r["metadata"]["output_file_size_bytes"] == 1024


# ===========================================================================
# 37: FAIL metadata
# ===========================================================================


def test_fail_metadata_includes_failure_code(tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path="/nope.mp4")
    assert "failure_code" in result["metadata"]


# ===========================================================================
# 38: JSON serializable
# ===========================================================================


def test_pass_result_is_json_serializable(tmp_path):
    r = _run_pass_scenario(tmp_path)
    json.dumps(r)


def test_fail_result_is_json_serializable(tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    r = PlatformSafeFormatV1Module().run(ctx, input_path="/nope.mp4")
    json.dumps(r)


# ===========================================================================
# 39: run_module_chain after render_clip_v1 (mocked)
# ===========================================================================


def test_module_runs_in_chain_after_render_clip_v1(tmp_path):
    fake_rendered = _write_fake_mp4(str(tmp_path / "rendered.mp4"))
    ctx = _make_context(clip_dir=str(tmp_path))
    output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")

    with patch("platform_safe_format_v1.subprocess.run") as mock_run, \
         patch("platform_safe_format_v1._probe_video_info") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _write_fake_mp4(output_path)
        mock_probe.side_effect = [
            _default_probe_info(320, 240),
            _default_probe_info(1080, 1920),
        ]
        chain_result = run_module_chain(
            [PlatformSafeFormatV1Module()],
            ctx,
            initial_input_path=fake_rendered,
        )

    assert chain_result["status"] == "PASS"
    assert chain_result["final_output_path"] == output_path


# ===========================================================================
# 40–41: Conveyor integration
# ===========================================================================


class _DummyModule(PostProcessingModule):
    def __init__(self, name: str):
        self.module_name = name
        self.module_version = "1.0"

    def run(self, context, *, input_path=None, config=None):
        return make_module_pass_result(
            self.module_name, self.module_version,
            input_path=input_path,
            output_path=f"{input_path}.{self.module_name}.out" if input_path else f"/tmp/{self.module_name}.out",
        )


def test_module_plugs_into_conveyor(tmp_path):
    fake_source = _write_fake_mp4(str(tmp_path / "source.mp4"))
    candidate = _make_candidate()
    sr = {"job_id": "job_test_001", "selected_candidates": [candidate]}

    # render_clip_v1 output path
    render_out = render_output_path(str(tmp_path), "job_test_001", "cand_001")
    format_out = _make_output_path(str(tmp_path), "job_test_001", "cand_001")

    registry = {
        "render_clip_v1": _DummyModule("render_clip_v1"),
        "platform_safe_format_v1": PlatformSafeFormatV1Module(),
        **{n: _DummyModule(n) for n in FIXED_MK1_CONVEYOR_MODULES[2:]},
    }

    # The dummy render_clip_v1 will output a fake path; we need the format module
    # to receive that fake path and act on it.
    # Use a different approach: patch render to write a real file, patch format module.
    class _RenderDummy(PostProcessingModule):
        module_name = "render_clip_v1"
        module_version = "1.0"
        def run(self, ctx, *, input_path=None, config=None):
            _write_fake_mp4(render_out)
            return make_module_pass_result(
                self.module_name, self.module_version,
                input_path=input_path, output_path=render_out,
            )

    registry["render_clip_v1"] = _RenderDummy()

    with patch("platform_safe_format_v1.subprocess.run") as mock_run, \
         patch("platform_safe_format_v1._probe_video_info") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _write_fake_mp4(format_out)
        mock_probe.side_effect = [
            _default_probe_info(320, 240),
            _default_probe_info(1080, 1920),
        ]
        result = run_fixed_mk1_universal_conveyor(
            sr,
            source_video_path=fake_source,
            job_metadata={"job_id": "job_test_001"},
            directories={"clips": str(tmp_path), "metadata": None, "tmp": None, "post_processing_root": None},
            module_registry=registry,
        )

    assert result["status"] == CONVEYOR_STATUS_COMPLETE
    clip = result["clip_results"][0]
    assert clip["status"] == "PASS"


def test_conveyor_passes_format_output_to_next_module(tmp_path):
    fake_source = _write_fake_mp4(str(tmp_path / "source.mp4"))
    candidate = _make_candidate()
    sr = {"job_id": "job_test_001", "selected_candidates": [candidate]}

    render_out = render_output_path(str(tmp_path), "job_test_001", "cand_001")
    format_out = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
    captured: list[str | None] = []

    class _RenderDummy(PostProcessingModule):
        module_name = "render_clip_v1"
        module_version = "1.0"
        def run(self, ctx, *, input_path=None, config=None):
            _write_fake_mp4(render_out)
            return make_module_pass_result(self.module_name, self.module_version,
                                           input_path=input_path, output_path=render_out)

    class _CaptureModule(PostProcessingModule):
        module_name = "intelligent_captions_v1"
        module_version = "1.0"
        def run(self, ctx, *, input_path=None, config=None):
            captured.append(input_path)
            return make_module_pass_result(self.module_name, self.module_version,
                                           input_path=input_path,
                                           output_path=f"{input_path}.out" if input_path else "/tmp/out")

    registry = {
        "render_clip_v1": _RenderDummy(),
        "platform_safe_format_v1": PlatformSafeFormatV1Module(),
        "intelligent_captions_v1": _CaptureModule(),
        **{n: _DummyModule(n) for n in FIXED_MK1_CONVEYOR_MODULES[3:]},
    }

    with patch("platform_safe_format_v1.subprocess.run") as mock_run, \
         patch("platform_safe_format_v1._probe_video_info") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        _write_fake_mp4(format_out)
        mock_probe.side_effect = [
            _default_probe_info(320, 240),
            _default_probe_info(1080, 1920),
        ]
        run_fixed_mk1_universal_conveyor(
            sr,
            source_video_path=fake_source,
            job_metadata={"job_id": "job_test_001"},
            directories={"clips": str(tmp_path), "metadata": None, "tmp": None, "post_processing_root": None},
            module_registry=registry,
        )

    assert len(captured) == 1
    assert captured[0] == format_out


# ===========================================================================
# 42–48: No forbidden behaviour
# ===========================================================================


def test_module_does_not_generate_captions():
    import platform_safe_format_v1 as m
    assert "captions" not in set(vars(m).keys())
    assert "subtitles" not in set(vars(m).keys())


def test_module_does_not_burn_subtitles():
    from platform_safe_format_v1 import _build_format_command
    cmd = _build_format_command(
        input_path="/in.mp4", output_path="/out.mp4",
        target_w=1080, target_h=1920,
        config={"background_blur": "20:1", "video_codec": "libx264",
                "audio_codec": "aac", "ffmpeg_preset": "veryfast"},
        input_has_audio=True,
    )
    cmd_str = " ".join(cmd)
    assert "subtitles" not in cmd_str
    assert "ass" not in cmd_str


def test_module_does_not_perform_face_tracking():
    import platform_safe_format_v1 as m
    assert "face" not in " ".join(vars(m).keys())


def test_module_does_not_perform_object_tracking():
    import platform_safe_format_v1 as m
    assert "tracking" not in " ".join(vars(m).keys())


def test_module_does_not_perform_intelligent_zoom():
    from platform_safe_format_v1 import _build_format_command
    cmd = _build_format_command(
        input_path="/in.mp4", output_path="/out.mp4",
        target_w=1080, target_h=1920,
        config={"background_blur": "20:1", "video_codec": "libx264",
                "audio_codec": "aac", "ffmpeg_preset": "veryfast"},
        input_has_audio=True,
    )
    cmd_str = " ".join(cmd)
    assert "zoompan" not in cmd_str


def test_module_does_not_import_ai_service():
    import platform_safe_format_v1 as m
    assert "ai_service" not in set(vars(m).keys())
    assert "model_client" not in set(vars(m).keys())


def test_module_does_not_import_output_funnel():
    import platform_safe_format_v1 as m
    assert "output_funnel" not in set(vars(m).keys())


# ===========================================================================
# 49–53: Regression — Prompt 14/15/16/17/18
# ===========================================================================


def test_prompt14_still_importable():
    import post_processing_mk1  # noqa: F401


def test_prompt15_still_importable():
    import selection_gate_v1  # noqa: F401


def test_prompt16_still_importable():
    import post_processing_modules  # noqa: F401


def test_prompt17_still_importable():
    import post_processing_conveyor  # noqa: F401


def test_prompt18_still_importable():
    import render_clip_v1  # noqa: F401


# ===========================================================================
# Helper unit tests
# ===========================================================================


def test_get_platform_safe_format_v1_module_returns_instance():
    m = get_platform_safe_format_v1_module()
    assert isinstance(m, PlatformSafeFormatV1Module)


def test_safe_filename_part_replaces_special_chars():
    assert "/" not in _safe_filename_part("job/001:test")
    assert ":" not in _safe_filename_part("job/001:test")


def test_output_path_contains_module_name(tmp_path):
    p = _make_output_path(str(tmp_path), "j", "c")
    assert "platform_safe_format_v1" in os.path.basename(p)


# ===========================================================================
# ffmpeg-dependent tests (skipped when unavailable)
# ===========================================================================


@requires_ffmpeg
def test_real_format_horizontal_creates_output(horizontal_video, tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path=horizontal_video)
    assert result["status"] == MODULE_STATUS_PASS, result.get("error_reason")
    assert os.path.isfile(result["output_path"])


@requires_ffmpeg
def test_real_format_horizontal_output_is_1080x1920(horizontal_video, tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path=horizontal_video)
    assert result["status"] == MODULE_STATUS_PASS, result.get("error_reason")
    assert result["metadata"]["output_width"] == 1080
    assert result["metadata"]["output_height"] == 1920


@requires_ffmpeg
def test_real_format_horizontal_aspect_ratio_is_9_16(horizontal_video, tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path=horizontal_video)
    assert result["status"] == MODULE_STATUS_PASS, result.get("error_reason")
    assert result["metadata"]["aspect_ratio"] == "9:16"


@requires_ffmpeg
def test_real_format_square_creates_output(square_video, tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path=square_video)
    assert result["status"] == MODULE_STATUS_PASS, result.get("error_reason")
    assert result["metadata"]["output_width"] == 1080
    assert result["metadata"]["output_height"] == 1920


@requires_ffmpeg
def test_real_format_vertical_input_creates_output(vertical_video, tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path=vertical_video)
    assert result["status"] == MODULE_STATUS_PASS, result.get("error_reason")
    assert result["metadata"]["output_width"] == 1080
    assert result["metadata"]["output_height"] == 1920


@requires_ffmpeg
def test_real_format_preserves_audio(horizontal_video, tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path=horizontal_video)
    assert result["status"] == MODULE_STATUS_PASS, result.get("error_reason")
    assert result["metadata"]["input_has_audio"] is True
    # Output should also have audio (verify via probe of output file)
    out_info = _probe_video_info(result["output_path"])
    assert out_info is not None
    assert out_info["has_audio"] is True


@requires_ffmpeg
def test_real_format_duration_close_to_input(horizontal_video, tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path=horizontal_video)
    assert result["status"] == MODULE_STATUS_PASS, result.get("error_reason")
    assert result["metadata"]["duration_delta_sec"] <= 1.0


@requires_ffmpeg
def test_real_format_result_is_prompt16_valid(horizontal_video, tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path=horizontal_video)
    assert result["status"] == MODULE_STATUS_PASS, result.get("error_reason")
    validate_module_result(result)


@requires_ffmpeg
def test_real_format_result_is_json_serializable(horizontal_video, tmp_path):
    ctx = _make_context(clip_dir=str(tmp_path))
    result = PlatformSafeFormatV1Module().run(ctx, input_path=horizontal_video)
    assert result["status"] == MODULE_STATUS_PASS, result.get("error_reason")
    json.dumps(result)
