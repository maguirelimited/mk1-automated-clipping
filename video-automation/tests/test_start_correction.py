"""Selection validation replaces legacy `_correct_segment_start` / `_correct_segment_end`.

Those helpers silently adjusted LLM timestamps against Whisper. The pipeline now uses
`mk04_utils.validate_and_repair_selection`: clips must lie inside merged Whisper time
coverage, respect tolerant duration bounds, and stay within video duration — invalid
clips are dropped with structured issues instead of being auto-corrected.
"""

import os
import sys
import unittest


SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from mk04_utils import validate_and_repair_selection


def _payload_with_segments(*segments: dict) -> dict:
    return {
        "full_text": "x",
        "segments": list(segments),
    }


class ValidateAndRepairSelectionTests(unittest.TestCase):
    """Lock-in for post-selection validation (successor to start/end correction)."""

    VIDEO_DUR = 300.0
    MIN_D = 5.0
    MAX_D = 30.0

    def test_accepts_clip_fully_inside_single_whisper_span(self):
        payload = _payload_with_segments({"start": 0.0, "end": 30.0, "text": "a"})
        valid, issues = validate_and_repair_selection(
            [{"start": "00:00:10", "end": "00:00:20"}],
            transcript_payload=payload,
            video_duration_sec=self.VIDEO_DUR,
            min_duration_sec=self.MIN_D,
            max_duration_sec=self.MAX_D,
        )
        self.assertEqual(len(valid), 1)
        self.assertEqual(issues, [])
        self.assertEqual(valid[0]["duration_sec"], 10.0)

    def test_rejects_clip_spanning_gap_between_whisper_segments(self):
        """No silent realignment: interval must be contained in one cover region."""
        payload = _payload_with_segments(
            {"start": 0.0, "end": 5.0, "text": "first"},
            {"start": 15.0, "end": 25.0, "text": "second"},
        )
        valid, issues = validate_and_repair_selection(
            [{"start": "00:00:02", "end": "00:00:20"}],
            transcript_payload=payload,
            video_duration_sec=self.VIDEO_DUR,
            min_duration_sec=self.MIN_D,
            max_duration_sec=self.MAX_D,
        )
        self.assertEqual(valid, [])
        self.assertTrue(
            any(
                i.get("message") == "Clip interval is not contained in Whisper transcript time coverage."
                for i in issues
            ),
        )

    def test_rejects_clip_when_duration_below_effective_minimum(self):
        payload = _payload_with_segments({"start": 0.0, "end": 20.0, "text": "a"})
        valid, issues = validate_and_repair_selection(
            [{"start": "00:00:00", "end": "00:00:03"}],
            transcript_payload=payload,
            video_duration_sec=self.VIDEO_DUR,
            min_duration_sec=self.MIN_D,
            max_duration_sec=self.MAX_D,
        )
        self.assertEqual(valid, [])
        self.assertTrue(any("Clip duration out of range" in (i.get("message") or "") for i in issues))

    def test_rejects_clip_when_duration_above_effective_maximum(self):
        payload = _payload_with_segments({"start": 0.0, "end": 120.0, "text": "a"})
        valid, issues = validate_and_repair_selection(
            [{"start": "00:00:00", "end": "00:01:00"}],
            transcript_payload=payload,
            video_duration_sec=self.VIDEO_DUR,
            min_duration_sec=self.MIN_D,
            max_duration_sec=self.MAX_D,
        )
        self.assertEqual(valid, [])
        self.assertTrue(any("Clip duration out of range" in (i.get("message") or "") for i in issues))

    def test_rejects_missing_start_or_end(self):
        payload = _payload_with_segments({"start": 0.0, "end": 20.0, "text": "a"})
        valid, issues = validate_and_repair_selection(
            [{"end": "00:00:10"}],
            transcript_payload=payload,
            video_duration_sec=self.VIDEO_DUR,
            min_duration_sec=self.MIN_D,
            max_duration_sec=self.MAX_D,
        )
        self.assertEqual(valid, [])
        self.assertTrue(any(i.get("message") == "Missing start/end" for i in issues))

    def test_rejects_unparseable_timestamps(self):
        payload = _payload_with_segments({"start": 0.0, "end": 20.0, "text": "a"})
        valid, issues = validate_and_repair_selection(
            [{"start": "not-a-time", "end": "00:00:15"}],
            transcript_payload=payload,
            video_duration_sec=self.VIDEO_DUR,
            min_duration_sec=self.MIN_D,
            max_duration_sec=self.MAX_D,
        )
        self.assertEqual(valid, [])
        self.assertTrue(any(i.get("message") == "Unparseable timestamp" for i in issues))

    def test_keeps_valid_and_drops_invalid_in_same_batch(self):
        payload = _payload_with_segments({"start": 0.0, "end": 60.0, "text": "block"})
        valid, issues = validate_and_repair_selection(
            [
                {"start": "00:00:10", "end": "00:00:20"},
                {"start": "00:10:00", "end": "00:10:10"},
            ],
            transcript_payload=payload,
            video_duration_sec=self.VIDEO_DUR,
            min_duration_sec=self.MIN_D,
            max_duration_sec=self.MAX_D,
        )
        self.assertEqual(len(valid), 1)
        self.assertEqual(parse_time(valid[0]["start"]), 10.0)
        self.assertEqual(len(issues), 1)


def parse_time(s: str) -> float:
    from pipeline_utils import parse_time_to_seconds

    return parse_time_to_seconds(s)


if __name__ == "__main__":
    unittest.main()
