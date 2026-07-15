"""Validate the repo-root dev input video fixture when present."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "input" / "test_server_video.mp4"
GENERATOR = REPO_ROOT / "input" / "generate_test_server_video.sh"

requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not installed",
)


@requires_ffmpeg
def test_test_server_video_fixture_is_probeable() -> None:
    if not FIXTURE.is_file():
        pytest.skip(f"missing fixture: run {GENERATOR.relative_to(REPO_ROOT)} first")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(FIXTURE),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert float(result.stdout.strip()) > 0
