"""Tests for per-source candidate scan limits in runner."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from input_service.downloader import DownloadResult  # noqa: E402
from input_service.duplicate_store import DuplicateStore  # noqa: E402
from input_service.funnel_loader import SourceDefinition  # noqa: E402
from input_service.media_validator import ValidationError  # noqa: E402
from input_service.runner import _candidate_scan_limit, run_funnel  # noqa: E402
from input_service.source_checker import (  # noqa: E402
    DEFAULT_MAX_VIDEOS_PER_SOURCE,
    Candidate,
    iter_source_candidates,
)


class _FakeFunnel:
    def __init__(self, source_configs):
        self.source_configs = source_configs


class _RunnableFunnel(_FakeFunnel):
    funnel_id = "demo_funnel"
    pipeline_profile = "demo_profile"
    min_duration_minutes = 20
    max_duration_minutes = 120
    max_downloads_per_run = 1
    title_blocklist = ()
    title_allowlist = ()
    posting_config = {}
    analytics_config = {}
    active = True

    @property
    def min_duration_seconds(self):
        return self.min_duration_minutes * 60

    @property
    def max_duration_seconds(self):
        return self.max_duration_minutes * 60


class RunnerScanLimitTests(unittest.TestCase):
    def test_uses_source_max_not_downloads_per_run(self):
        src = SourceDefinition(
            source_id="ch1",
            label="Channel",
            source_type="youtube_channel",
            url="https://www.youtube.com/@example/videos",
            active=True,
            max_videos_per_source=25,
        )
        funnel = _FakeFunnel((src,))
        self.assertEqual(_candidate_scan_limit(funnel), 25)
        self.assertNotEqual(_candidate_scan_limit(funnel), 5)

    def test_default_when_no_per_source_limit(self):
        src = SourceDefinition(
            source_id="ch1",
            label="Channel",
            source_type="youtube_channel",
            url="https://www.youtube.com/@example/videos",
            active=True,
            max_videos_per_source=None,
        )
        funnel = _FakeFunnel((src,))
        self.assertEqual(_candidate_scan_limit(funnel), DEFAULT_MAX_VIDEOS_PER_SOURCE)

    def test_run_funnel_moves_to_next_candidate_after_validation_failure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            src = SourceDefinition(
                source_id="ch1",
                label="Channel",
                source_type="youtube_channel",
                url="https://www.youtube.com/@example/videos",
                active=True,
                max_videos_per_source=25,
            )
            funnel = _RunnableFunnel((src,))
            candidates = [
                Candidate(
                    video_id="bad",
                    url="https://www.youtube.com/watch?v=bad",
                    title="Bad candidate",
                    source=src.url,
                    duration_seconds=60 * 60,
                ),
                Candidate(
                    video_id="good",
                    url="https://www.youtube.com/watch?v=good",
                    title="Good candidate",
                    source=src.url,
                    duration_seconds=60 * 60,
                ),
            ]
            download_path = tmp_path / "download.mp4"
            download_path.write_bytes(b"video")
            ready_path = tmp_path / "ready.mp4"
            ready_path.write_bytes(b"ready")
            seen_store = DuplicateStore(file=tmp_path / "seen.json")

            download = Mock(
                side_effect=lambda cand, *, funnel_id: DownloadResult(
                    file_path=download_path,
                    candidate=cand,
                )
            )

            with patch("input_service.runner.paths.ensure_dirs"), patch(
                "input_service.runner.load_funnel", return_value=funnel
            ), patch("input_service.runner.DuplicateStore", return_value=seen_store), patch(
                "input_service.runner.iter_source_candidates", return_value=iter(candidates)
            ), patch(
                "input_service.runner.download_candidate", download
            ), patch(
                "input_service.runner.validate_media",
                side_effect=[ValidationError("bad media"), None],
            ), patch(
                "input_service.runner.reject_file"
            ), patch(
                "input_service.runner.store_ready", return_value=ready_path
            ), patch(
                "input_service.runner.enqueue_clipping_job",
                return_value={"success": True, "job_id": "clip_1", "status": "queued"},
            ), patch.dict(
                "os.environ", {"INPUT_JOB_LEDGER_DIR": str(tmp_path / "jobs")}
            ):
                result = run_funnel("demo_funnel")

            self.assertTrue(result["success"])
            self.assertEqual(result["status"], "input_ready")
            self.assertEqual(result["source_url"], "https://www.youtube.com/watch?v=good")
            self.assertEqual([call.args[0].video_id for call in download.call_args_list], ["bad", "good"])
            self.assertFalse(seen_store.is_seen(video_id="good", url="https://www.youtube.com/watch?v=good"))

    def test_iter_source_candidates_skips_seen_url_before_yielding(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            seen_store = DuplicateStore(file=tmp_path / "seen.json")
            seen_store.mark_seen(video_id="seen", url="https://www.youtube.com/watch?v=seen")
            src = SourceDefinition(
                source_id="ch1",
                label="Channel",
                source_type="youtube_channel",
                url="https://www.youtube.com/@example/videos",
                active=True,
                max_videos_per_source=2,
            )

            def flat_one(_url, index):
                video_id = "seen" if index == 1 else "fresh"
                return {
                    "entries": [
                        {
                            "id": video_id,
                            "title": video_id,
                            "duration": 60 * 60,
                            "upload_date": "20260520",
                        }
                    ]
                }

            with patch("input_service.source_checker._flat_extract_one", side_effect=flat_one):
                candidates = list(iter_source_candidates([src], seen=seen_store))

            self.assertEqual([cand.video_id for cand in candidates], ["fresh"])


if __name__ == "__main__":
    unittest.main()
