"""Job activity scanning tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS_OPS = Path(__file__).resolve().parents[2] / "scripts" / "ops"
if str(_SCRIPTS_OPS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_OPS))

from status_report import _scan_job_activity  # noqa: E402


def test_scan_job_activity_dedupes_duplicate_job_directories(tmp_path: Path) -> None:
    jobs_root = tmp_path / "jobs"
    jobs_root.mkdir()
    job_id = "job_20260705T153338Z_893260b7"

    stub = jobs_root / job_id
    stub.mkdir()
    (stub / "report.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "status": "running",
                "current_stage": "transcription",
                "started_at": "2026-07-05T15:33:38+00:00",
            }
        ),
        encoding="utf-8",
    )

    completed = jobs_root / f"input_example_source_{job_id}"
    completed.mkdir()
    (completed / "report.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "status": "success",
                "completed_at": "2026-07-05T15:45:01+00:00",
                "clips": [{"clip_id": "clip_01"}],
            }
        ),
        encoding="utf-8",
    )

    lines = _scan_job_activity(jobs_root)
    running = next(line for line in lines if line.label == "Running jobs")
    assert running.value == "0"
