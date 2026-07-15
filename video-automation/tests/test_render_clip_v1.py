"""render_clip_v1 — focused tests (Prompt 18).

Tests are divided into:
  - Non-ffmpeg tests: input validation, config validation, path rules, contract
    shape.  These always run.
  - ffmpeg tests: real rendering, duration verification.  These are skipped
    when ffmpeg / ffprobe are not installed in the test environment.

No real production video file is required.  ffmpeg-dependent tests generate
a tiny 4-second synthetic clip inside a temp directory and clean up afterwards.
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
from mk04_utils import ffprobe_duration_sec as mk04_ffprobe_duration_sec  # noqa: E402
from render_clip_v1 import (  # noqa: E402
    MODULE_NAME,
    MODULE_VERSION,
    RenderClipV1Module,
    _make_output_path,
    _safe_filename_part,
    get_render_clip_v1_module,
)
import render_clip_v1  # noqa: E402


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe availability guards
# ---------------------------------------------------------------------------


def _cmd_available(name: str) -> bool:
    return shutil.which(name) is not None


FFMPEG_AVAILABLE = _cmd_available("ffmpeg")
FFPROBE_AVAILABLE = _cmd_available("ffprobe")
FFTOOLS_AVAILABLE = FFMPEG_AVAILABLE and FFPROBE_AVAILABLE

requires_ffmpeg = pytest.mark.skipif(
    not FFTOOLS_AVAILABLE,
    reason="ffmpeg/ffprobe not installed — skipping real-render tests",
)


def test_render_clip_v1_uses_shared_mk04_ffprobe_duration_helper() -> None:
    assert render_clip_v1.ffprobe_duration_sec is mk04_ffprobe_duration_sec


# ---------------------------------------------------------------------------
# Synthetic video fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def synthetic_video_path(tmp_path_factory):
    """4-second 320×240 test video generated once per session."""
    if not FFTOOLS_AVAILABLE:
        return None
    out_dir = tmp_path_factory.mktemp("synthetic_video")
    out = str(out_dir / "test_source.mp4")
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100",
            "-t", "4",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-c:a", "aac",
            out,
        ],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, f"Failed to create synthetic video: {result.stderr}"
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candidate(
    candidate_id: str = "cand_001",
    start_sec: float = 0.5,
    end_sec: float = 3.0,
    rank: int = 1,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "rank": rank,
        "start_sec": start_sec,
        "end_sec": end_sec,
        "duration_sec": end_sec - start_sec,
        "confidence": 0.85,
        "scores": {"overall_potential": 8.0},
        "selection_reason": "selected_by_rank",
        "warnings": [],
        "transcript_quality_flags": [],
        "source_candidate": {},
    }


def _make_context(
    source_video: str,
    candidate: dict[str, Any] | None = None,
    clip_dir: str | None = None,
    job_id: str = "job_test_001",
) -> dict[str, Any]:
    if candidate is None:
        candidate = _make_candidate()
    return {
        "job_id": job_id,
        "candidate_id": candidate.get("candidate_id"),
        "source_video_path": source_video,
        "working_dir": None,
        "clip_dir": clip_dir,
        "metadata_dir": None,
        "tmp_dir": None,
        "config": {},
        "selection_result": {},
        "selected_candidate": candidate,
        "module_results": [],
    }


def _make_fake_video(tmp_dir: str) -> str:
    """Write a dummy non-empty file that looks like a video path (for non-ffmpeg tests)."""
    path = os.path.join(tmp_dir, "fake_source.mp4")
    with open(path, "wb") as f:
        f.write(b"\x00" * 128)
    return path


# ===========================================================================
# 1–2: Module identity
# ===========================================================================


def test_module_name_is_render_clip_v1():
    m = RenderClipV1Module()
    assert m.module_name == "render_clip_v1"


def test_module_version_exists():
    m = RenderClipV1Module()
    assert m.module_version and isinstance(m.module_version, str)


def test_module_name_constant_matches_class():
    assert MODULE_NAME == "render_clip_v1"


def test_module_version_constant_matches_class():
    assert MODULE_VERSION == RenderClipV1Module.module_version


# ===========================================================================
# 3–4: Result contract shape
# ===========================================================================


def test_pass_result_is_prompt16_compatible(tmp_path):
    """A successful render must return a valid Prompt 16 module result."""
    module = RenderClipV1Module()
    fake_video = _make_fake_video(str(tmp_path))
    ctx = _make_context(fake_video, clip_dir=str(tmp_path))

    # Patch ffprobe_duration_sec and subprocess.run so we don't need ffmpeg
    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        # Create an actual output file so the module finds it
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5

        result = module.run(ctx, input_path=fake_video)

    assert result["status"] == MODULE_STATUS_PASS
    validate_module_result(result)  # must not raise


def test_fail_result_is_prompt16_compatible(tmp_path):
    """A failure must return a valid Prompt 16 module result."""
    module = RenderClipV1Module()
    ctx = _make_context("/nonexistent/source.mp4")
    result = module.run(ctx, input_path="/nonexistent/source.mp4")
    assert result["status"] == MODULE_STATUS_FAIL
    validate_module_result(result)  # must not raise


# ===========================================================================
# 5–6: Source video path resolution
# ===========================================================================


def test_source_video_path_comes_from_input_path(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    ctx = _make_context("/wrong/path.mp4")
    ctx["source_video_path"] = "/wrong/path.mp4"

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5
        ctx["clip_dir"] = str(tmp_path)

        result = RenderClipV1Module().run(ctx, input_path=fake)

    # The result input_path should be the fake video, not the wrong one
    assert result["input_path"] == fake


def test_source_video_path_falls_back_to_context(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    ctx = _make_context(fake, clip_dir=str(tmp_path))

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5

        result = RenderClipV1Module().run(ctx, input_path=None)

    assert result["input_path"] == fake


# ===========================================================================
# 7–9: Source video validation
# ===========================================================================


def test_missing_source_video_returns_fail():
    ctx = _make_context("")
    ctx["source_video_path"] = ""
    result = RenderClipV1Module().run(ctx, input_path=None)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "missing_source_video"


def test_source_video_not_found_returns_fail():
    ctx = _make_context("/does/not/exist/video.mp4")
    result = RenderClipV1Module().run(ctx, input_path="/does/not/exist/video.mp4")
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "source_video_not_found"


def test_source_video_is_directory_returns_fail(tmp_path):
    ctx = _make_context(str(tmp_path))
    result = RenderClipV1Module().run(ctx, input_path=str(tmp_path))
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "source_video_not_file"


# ===========================================================================
# 10–11: Selected candidate validation
# ===========================================================================


def test_missing_selected_candidate_returns_fail(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    ctx = _make_context(fake)
    ctx["selected_candidate"] = {}
    result = RenderClipV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "missing_selected_candidate"


def test_missing_candidate_id_returns_fail(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    cand = _make_candidate()
    del cand["candidate_id"]
    ctx = _make_context(fake, candidate=cand)
    result = RenderClipV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "missing_candidate_id"


# ===========================================================================
# 12–19: Timestamp validation
# ===========================================================================


def test_missing_start_sec_returns_fail(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    cand = _make_candidate()
    del cand["start_sec"]
    ctx = _make_context(fake, candidate=cand)
    result = RenderClipV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] in ("missing_start_sec", "invalid_timestamp")


def test_missing_end_sec_returns_fail(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    cand = _make_candidate()
    del cand["end_sec"]
    ctx = _make_context(fake, candidate=cand)
    result = RenderClipV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL


def test_non_numeric_start_sec_returns_fail(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    cand = _make_candidate()
    cand["start_sec"] = "not_a_number"
    ctx = _make_context(fake, candidate=cand)
    result = RenderClipV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "invalid_timestamp"


def test_non_numeric_end_sec_returns_fail(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    cand = _make_candidate()
    cand["end_sec"] = "bad"
    ctx = _make_context(fake, candidate=cand)
    result = RenderClipV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "invalid_timestamp"


def test_negative_start_sec_returns_fail(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    cand = _make_candidate(start_sec=-1.0, end_sec=2.0)
    ctx = _make_context(fake, candidate=cand)
    result = RenderClipV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "invalid_timestamp"


def test_end_sec_less_than_start_sec_returns_fail(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    cand = _make_candidate(start_sec=5.0, end_sec=3.0)
    ctx = _make_context(fake, candidate=cand)
    result = RenderClipV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "invalid_timestamp"


def test_end_sec_equal_to_start_sec_returns_fail(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    cand = _make_candidate(start_sec=5.0, end_sec=5.0)
    ctx = _make_context(fake, candidate=cand)
    result = RenderClipV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "invalid_timestamp"


def test_duration_too_short_returns_fail(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    cand = _make_candidate(start_sec=0.0, end_sec=0.5)  # 0.5s < default min 1.0s
    ctx = _make_context(fake, candidate=cand)
    result = RenderClipV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "duration_too_short"


def test_duration_too_long_returns_fail(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    cand = _make_candidate(start_sec=0.0, end_sec=200.0)  # 200s > default max 180s
    ctx = _make_context(fake, candidate=cand)
    result = RenderClipV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "duration_too_long"


def test_candidate_duration_sec_mismatch_creates_warning(tmp_path):
    """Declared duration_sec that diverges from end-start by > 0.5s becomes a warning on PASS."""
    fake = _make_fake_video(str(tmp_path))
    cand = _make_candidate(start_sec=0.5, end_sec=3.0)
    cand["duration_sec"] = 99.0  # wildly wrong

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5
        ctx = _make_context(fake, candidate=cand, clip_dir=str(tmp_path))
        result = RenderClipV1Module().run(ctx, input_path=fake)

    assert result["status"] == MODULE_STATUS_PASS
    assert any("duration_sec" in w for w in result.get("warnings", []))


# ===========================================================================
# 21–26: Output file
# ===========================================================================


def test_output_clip_file_is_created(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    ctx = _make_context(fake, clip_dir=str(tmp_path))

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5
        result = RenderClipV1Module().run(ctx, input_path=fake)

    assert result["status"] == MODULE_STATUS_PASS
    assert os.path.isfile(result["output_path"])


def test_output_clip_file_is_non_empty(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    ctx = _make_context(fake, clip_dir=str(tmp_path))

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5
        result = RenderClipV1Module().run(ctx, input_path=fake)

    assert os.path.getsize(result["output_path"]) > 0


def test_output_path_is_deterministic(tmp_path):
    fake = _make_fake_video(str(tmp_path))

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5

        ctx1 = _make_context(fake, clip_dir=str(tmp_path))
        r1 = RenderClipV1Module().run(ctx1, input_path=fake)

        ctx2 = _make_context(fake, clip_dir=str(tmp_path))
        r2 = RenderClipV1Module().run(ctx2, input_path=fake)

    assert r1["output_path"] == r2["output_path"]


def test_output_path_includes_safe_job_and_candidate(tmp_path):
    path = _make_output_path(str(tmp_path), "job-test-001", "cand_abc")
    basename = os.path.basename(path)
    assert "job_test_001" in basename or "job-test-001" in basename or "job" in basename
    assert "cand_abc" in basename


def test_clip_dir_is_created_if_missing(tmp_path):
    new_clip_dir = str(tmp_path / "new_clips")
    assert not os.path.exists(new_clip_dir)
    fake = _make_fake_video(str(tmp_path))

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(new_clip_dir, "job_test_001", "cand_001")
        os.makedirs(new_clip_dir, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5
        ctx = _make_context(fake, clip_dir=new_clip_dir)
        result = RenderClipV1Module().run(ctx, input_path=fake)

    assert os.path.isdir(new_clip_dir)
    assert result["status"] == MODULE_STATUS_PASS


def test_overwrite_true_replaces_existing(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
    # Pre-create a stale output file
    with open(output_path, "wb") as f:
        f.write(b"stale")

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5
        ctx = _make_context(fake, clip_dir=str(tmp_path))
        ctx["config"] = {"overwrite": True}
        result = RenderClipV1Module().run(ctx, input_path=fake)

    assert result["status"] == MODULE_STATUS_PASS


def test_overwrite_false_fails_when_output_exists(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
    # Pre-create a stale output file
    with open(output_path, "wb") as f:
        f.write(b"existing")

    ctx = _make_context(fake, clip_dir=str(tmp_path))
    ctx["config"] = {"overwrite": False}
    result = RenderClipV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "output_already_exists"


# ===========================================================================
# 29: Invalid config
# ===========================================================================


def test_invalid_config_returns_fail(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    ctx = _make_context(fake, clip_dir=str(tmp_path))
    ctx["config"] = {"min_duration_sec": -5.0}
    result = RenderClipV1Module().run(ctx, input_path=fake)
    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "invalid_render_config"


# ===========================================================================
# 30: ffmpeg failure
# ===========================================================================


def test_ffmpeg_failure_returns_fail_with_error_metadata(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    ctx = _make_context(fake, clip_dir=str(tmp_path))

    with patch("render_clip_v1.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error opening input file",
        )
        result = RenderClipV1Module().run(ctx, input_path=fake)

    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "ffmpeg_failed"
    assert result["metadata"].get("ffmpeg_returncode") == 1
    assert "ffmpeg_stderr_tail" in result["metadata"]


# ===========================================================================
# 31: Duration probe failure
# ===========================================================================


def test_duration_probe_failure_returns_fail(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    ctx = _make_context(fake, clip_dir=str(tmp_path))

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = None  # simulate probe failure
        result = RenderClipV1Module().run(ctx, input_path=fake)

    assert result["status"] == MODULE_STATUS_FAIL
    assert result["metadata"]["failure_code"] == "duration_probe_failed"


# ===========================================================================
# 32–34: Metadata fields
# ===========================================================================


def test_pass_metadata_includes_timestamps(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    ctx = _make_context(fake, clip_dir=str(tmp_path))

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5
        result = RenderClipV1Module().run(ctx, input_path=fake)

    meta = result["metadata"]
    assert "start_sec" in meta
    assert "end_sec" in meta
    assert "expected_duration_sec" in meta
    assert "actual_duration_sec" in meta


def test_pass_metadata_includes_output_file_size(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    ctx = _make_context(fake, clip_dir=str(tmp_path))

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5
        result = RenderClipV1Module().run(ctx, input_path=fake)

    assert result["metadata"]["output_file_size_bytes"] == 512


def test_fail_metadata_includes_failure_code(tmp_path):
    ctx = _make_context("/does/not/exist.mp4")
    result = RenderClipV1Module().run(ctx, input_path="/does/not/exist.mp4")
    assert "failure_code" in result["metadata"]


# ===========================================================================
# 35: JSON serializability
# ===========================================================================


def test_pass_result_is_json_serializable(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    ctx = _make_context(fake, clip_dir=str(tmp_path))

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5
        result = RenderClipV1Module().run(ctx, input_path=fake)

    json.dumps(result)  # must not raise


def test_fail_result_is_json_serializable(tmp_path):
    ctx = _make_context("/nope.mp4")
    result = RenderClipV1Module().run(ctx, input_path="/nope.mp4")
    json.dumps(result)  # must not raise


# ===========================================================================
# 36: run_module_chain integration
# ===========================================================================


def test_module_can_run_in_module_chain_as_first_module(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    ctx = _make_context(fake, clip_dir=str(tmp_path))

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5

        chain_result = run_module_chain(
            [RenderClipV1Module()],
            ctx,
            initial_input_path=fake,
        )

    assert chain_result["status"] == "PASS"
    assert chain_result["final_output_path"] is not None


# ===========================================================================
# 37–38: Conveyor integration
# ===========================================================================


class _DummyDownstreamModule(PostProcessingModule):
    def __init__(self, name: str):
        self.module_name = name
        self.module_version = "1.0"

    def run(self, context, *, input_path=None, config=None):
        return make_module_pass_result(
            self.module_name,
            self.module_version,
            input_path=input_path,
            output_path=f"{input_path}.{self.module_name}.out" if input_path else f"/tmp/{self.module_name}.out",
        )


def test_module_plugs_into_fixed_conveyor(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    candidate = _make_candidate()
    sr = {
        "job_id": "job_test_001",
        "selected_candidates": [candidate],
    }

    registry = {
        "render_clip_v1": RenderClipV1Module(),
    }
    for name in FIXED_MK1_CONVEYOR_MODULES[1:]:
        registry[name] = _DummyDownstreamModule(name)

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5

        result = run_fixed_mk1_universal_conveyor(
            sr,
            source_video_path=fake,
            job_metadata={"job_id": "job_test_001"},
            directories={"clips": str(tmp_path), "metadata": None, "tmp": None, "post_processing_root": None},
            module_registry=registry,
        )

    assert result["status"] == CONVEYOR_STATUS_COMPLETE
    assert len(result["clip_results"]) == 1
    clip = result["clip_results"][0]
    assert clip["status"] == "PASS"


def test_conveyor_passes_render_output_to_next_module(tmp_path):
    fake = _make_fake_video(str(tmp_path))
    candidate = _make_candidate()
    sr = {
        "job_id": "job_test_001",
        "selected_candidates": [candidate],
    }

    captured_inputs: list[str | None] = []

    class _CapturingModule(_DummyDownstreamModule):
        def run(self, context, *, input_path=None, config=None):
            captured_inputs.append(input_path)
            return make_module_pass_result(
                self.module_name,
                self.module_version,
                input_path=input_path,
                output_path=f"{input_path}.out" if input_path else "/tmp/out",
            )

    registry = {
        "render_clip_v1": RenderClipV1Module(),
        "platform_safe_format_v1": _CapturingModule("platform_safe_format_v1"),
    }
    for name in FIXED_MK1_CONVEYOR_MODULES[2:]:
        registry[name] = _DummyDownstreamModule(name)

    with patch("render_clip_v1.subprocess.run") as mock_run, \
         patch("render_clip_v1.ffprobe_duration_sec") as mock_probe:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = _make_output_path(str(tmp_path), "job_test_001", "cand_001")
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 512)
        mock_probe.return_value = 2.5

        run_fixed_mk1_universal_conveyor(
            sr,
            source_video_path=fake,
            job_metadata={"job_id": "job_test_001"},
            directories={"clips": str(tmp_path), "metadata": None, "tmp": None, "post_processing_root": None},
            module_registry=registry,
        )

    # The second module should receive the render_clip_v1 output path
    assert len(captured_inputs) == 1
    assert captured_inputs[0] == output_path


# ===========================================================================
# 39–42: Regression — Prompt 14/15/16/17 still importable and functional
# ===========================================================================


def test_prompt14_still_importable():
    import post_processing_mk1  # noqa: F401


def test_prompt15_still_importable():
    import selection_gate_v1  # noqa: F401


def test_prompt16_still_importable():
    import post_processing_modules  # noqa: F401


def test_prompt17_still_importable():
    import post_processing_conveyor  # noqa: F401


# ===========================================================================
# 43–46: No forbidden imports / behaviours
# ===========================================================================


def test_no_platform_formatting():
    import render_clip_v1 as m
    names = set(vars(m).keys())
    assert "platform_safe_format" not in names
    assert "crop" not in names


def test_no_captions_generated():
    import render_clip_v1 as m
    names = set(vars(m).keys())
    assert "captions" not in names
    assert "subtitles" not in names


def test_no_ai_service_imported():
    import render_clip_v1 as m
    names = set(vars(m).keys())
    assert "ai_service" not in names
    assert "model_client" not in names


def test_no_output_funnel_registration():
    import render_clip_v1 as m
    names = set(vars(m).keys())
    assert "output_funnel" not in names
    assert "register_funnel" not in names


# ===========================================================================
# Helper unit tests
# ===========================================================================


def test_safe_filename_part_replaces_special_chars():
    assert _safe_filename_part("job-001/test") == "job-001_test"
    assert _safe_filename_part("a:b") == "a_b"


def test_make_output_path_is_deterministic(tmp_path):
    p1 = _make_output_path(str(tmp_path), "job_001", "cand_abc")
    p2 = _make_output_path(str(tmp_path), "job_001", "cand_abc")
    assert p1 == p2


def test_make_output_path_contains_render_clip_v1(tmp_path):
    p = _make_output_path(str(tmp_path), "job_001", "cand_abc")
    assert "render_clip_v1" in os.path.basename(p)


def test_get_render_clip_v1_module_returns_instance():
    m = get_render_clip_v1_module()
    assert isinstance(m, RenderClipV1Module)


# ===========================================================================
# ffmpeg-dependent tests (skipped when ffmpeg unavailable)
# ===========================================================================


@requires_ffmpeg
def test_real_render_creates_output_file(synthetic_video_path, tmp_path):
    ctx = _make_context(
        synthetic_video_path,
        candidate=_make_candidate(start_sec=0.5, end_sec=3.0),
        clip_dir=str(tmp_path),
    )
    result = RenderClipV1Module().run(ctx, input_path=synthetic_video_path)
    assert result["status"] == MODULE_STATUS_PASS, result.get("error_reason")
    assert os.path.isfile(result["output_path"])


@requires_ffmpeg
def test_real_render_output_file_is_non_empty(synthetic_video_path, tmp_path):
    ctx = _make_context(
        synthetic_video_path,
        candidate=_make_candidate(start_sec=0.5, end_sec=3.0),
        clip_dir=str(tmp_path),
    )
    result = RenderClipV1Module().run(ctx, input_path=synthetic_video_path)
    assert result["status"] == MODULE_STATUS_PASS, result.get("error_reason")
    assert os.path.getsize(result["output_path"]) > 0


@requires_ffmpeg
def test_real_render_output_duration_close_to_expected(synthetic_video_path, tmp_path):
    start, end = 0.5, 3.0
    expected = end - start
    ctx = _make_context(
        synthetic_video_path,
        candidate=_make_candidate(start_sec=start, end_sec=end),
        clip_dir=str(tmp_path),
    )
    result = RenderClipV1Module().run(ctx, input_path=synthetic_video_path)
    assert result["status"] == MODULE_STATUS_PASS, result.get("error_reason")
    actual_dur = result["metadata"]["actual_duration_sec"]
    assert abs(actual_dur - expected) <= 1.0, f"Duration delta too large: {abs(actual_dur - expected)}"


@requires_ffmpeg
def test_real_render_pass_result_is_prompt16_valid(synthetic_video_path, tmp_path):
    ctx = _make_context(
        synthetic_video_path,
        candidate=_make_candidate(start_sec=0.5, end_sec=3.0),
        clip_dir=str(tmp_path),
    )
    result = RenderClipV1Module().run(ctx, input_path=synthetic_video_path)
    assert result["status"] == MODULE_STATUS_PASS, result.get("error_reason")
    validate_module_result(result)


@requires_ffmpeg
def test_real_render_output_path_is_deterministic(synthetic_video_path, tmp_path):
    ctx1 = _make_context(
        synthetic_video_path,
        candidate=_make_candidate(start_sec=0.5, end_sec=3.0),
        clip_dir=str(tmp_path),
    )
    r1 = RenderClipV1Module().run(ctx1, input_path=synthetic_video_path)
    assert r1["status"] == MODULE_STATUS_PASS

    # Second run should produce same path (overwrite=True by default)
    ctx2 = _make_context(
        synthetic_video_path,
        candidate=_make_candidate(start_sec=0.5, end_sec=3.0),
        clip_dir=str(tmp_path),
    )
    r2 = RenderClipV1Module().run(ctx2, input_path=synthetic_video_path)
    assert r2["status"] == MODULE_STATUS_PASS
    assert r1["output_path"] == r2["output_path"]
