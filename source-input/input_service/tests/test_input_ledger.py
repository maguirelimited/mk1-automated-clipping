from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from input_service import ledger  # noqa: E402


@pytest.fixture()
def isolated_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    ledger_dir = tmp_path / "input_jobs"
    monkeypatch.setenv("INPUT_JOB_LEDGER_DIR", str(ledger_dir))
    return ledger_dir


def test_ledger_records_explicit_state_transitions(isolated_ledger: Path, tmp_path: Path):
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"fake-video")

    record = ledger.create_record(
        funnel_id="business_podcasts_001",
        source_url="https://example.test/watch?v=abc",
        source_metadata={"video_id": "abc", "title": "Example"},
        funnel_policy={"pipeline_profile": "business_podcasts_001"},
        max_attempts=1,
    )

    input_id = record["input_id"]
    assert record["job_id"] == input_id
    assert record["state"] == "discovered"
    assert (isolated_ledger / f"{input_id}.json").is_file()

    downloaded = ledger.mark_downloaded(input_id, video_path)
    assert downloaded["state"] == "downloaded"
    assert downloaded["file_path"] == str(video_path)

    processing = ledger.mark_processing(input_id)
    assert processing["state"] == "processing"

    succeeded = ledger.mark_succeeded(input_id, {"pipeline_job_id": "job_1", "clip_count": 2})
    assert succeeded["state"] == "succeeded"
    assert succeeded["result"]["clip_count"] == 2
    assert [row["state"] for row in succeeded["state_history"]] == [
        "discovered",
        "downloaded",
        "processing",
        "succeeded",
    ]


def test_ledger_rejects_invalid_transition(isolated_ledger: Path):
    record = ledger.create_record(funnel_id="funnel_1")

    with pytest.raises(ledger.LedgerStateError):
        ledger.mark_processing(record["input_id"])


def test_failed_records_do_not_block_source_retry(isolated_ledger: Path):
    active = ledger.create_record(
        funnel_id="funnel_1",
        source_url="https://example.test/active",
        source_metadata={"video_id": "active"},
    )
    active_path = isolated_ledger.parent / "active.mp4"
    active_path.write_bytes(b"active")
    ledger.mark_downloaded(active["input_id"], active_path)
    failed = ledger.create_record(
        funnel_id="funnel_1",
        source_url="https://example.test/failed",
        source_metadata={"video_id": "failed"},
    )
    ledger.mark_failed(failed["input_id"], "download_failed")

    assert ledger.source_has_non_failed_record(video_id="active", url=None) is True
    assert ledger.source_has_non_failed_record(video_id="failed", url=None) is False
    assert ledger.source_has_non_failed_record(video_id=None, url="https://example.test/active") is True
