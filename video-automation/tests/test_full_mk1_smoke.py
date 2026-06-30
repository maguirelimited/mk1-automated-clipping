"""Full MK1 synthetic smoke test.

Runs the smoke harness with generated media and verifies the end-to-end artifact
summary. This is intentionally small and deterministic; it does not claim a
real-video production smoke.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SMOKE_SCRIPT = SCRIPTS_DIR / "smoke_full_mk1_pipeline.py"


@pytest.mark.smoke
def test_full_mk1_synthetic_smoke(tmp_path):
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe not installed")

    summary_path = tmp_path / "summary.json"
    work_dir = tmp_path / "work"

    result = subprocess.run(
        [
            sys.executable,
            str(SMOKE_SCRIPT),
            "--work-dir",
            str(work_dir),
            "--job-id",
            "pytest_full_mk1_smoke",
            "--summary-json",
            str(summary_path),
            "--duration-sec",
            "5.0",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "FULL_MK1_SMOKE_PASSED" in result.stdout

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "passed"
    assert summary["source_type"] == "synthetic"
    assert summary["real_video_smoke"] is False
    assert summary["synthetic_smoke"] is True

    for key in (
        "raw_candidate_pool_path",
        "processing_report_path",
        "selection_result_path",
        "post_processing_report_path",
        "output_funnel_handoff_path",
    ):
        assert Path(summary[key]).is_file(), key

    assert summary["finished_clip_paths"]
    assert summary["per_clip_metadata_paths"]
    for clip_path in summary["finished_clip_paths"]:
        path = Path(clip_path)
        assert path.is_file()
        assert path.stat().st_size > 0
    for metadata_path in summary["per_clip_metadata_paths"]:
        path = Path(metadata_path)
        assert path.is_file()
        metadata = json.loads(path.read_text(encoding="utf-8"))
        assert metadata["output_file_path"] in summary["finished_clip_paths"]

    handoff = json.loads(
        Path(summary["output_funnel_handoff_path"]).read_text(encoding="utf-8")
    )
    assert handoff["schema_version"] == "output_funnel_handoff_v1"
    assert handoff["status"] == "READY_FOR_OUTPUT_FUNNEL"
    assert handoff["finished_clip_paths"] == summary["finished_clip_paths"]
