import os
import sys
import tempfile
import unittest


SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from mk04_utils import ffprobe_duration_sec, normalize_transcript_payload, require_timed_transcript_payload, validate_and_repair_selection


class FfprobeFailureContractTests(unittest.TestCase):
    """ffprobe helpers return None when duration cannot be read.

    The HTTP pipeline rejects jobs before transcription when the *main* video
    has no ffprobe duration. These tests only cover low-level probing behavior."""

    def test_ffprobe_returns_none_for_missing_file(self):
        result = ffprobe_duration_sec("/tmp/__bug4_nonexistent_file_xyz__.mp4")
        self.assertIsNone(result)

    def test_ffprobe_returns_none_for_empty_path(self):
        result = ffprobe_duration_sec("")
        self.assertIsNone(result)


class ValidateAndRepairVideoDurationContractTests(unittest.TestCase):
    """Selection validation requires a definitive video duration."""

    TRANSCRIPT = {
        "full_text": "irrelevant",
        "segments": [{"start": 0.0, "end": 200.0, "text": "irrelevant"}],
    }

    VIDEO_DUR_SEC = 90.0

    def test_rejects_nan_or_negative_duration_without_scanning_segments(self):
        valid, issues = validate_and_repair_selection(
            [{"start": "00:01:00", "end": "00:01:10"}],
            transcript_payload=self.TRANSCRIPT,
            video_duration_sec=float("nan"),
            min_duration_sec=5.0,
            max_duration_sec=20.0,
        )
        self.assertEqual(valid, [])
        self.assertEqual(len(issues), 1)
        self.assertIn("unavailable", issues[0].get("message", "").lower())

    def test_rejects_when_clip_end_past_known_video_duration(self):
        valid, issues = validate_and_repair_selection(
            [{"start": "00:01:25", "end": "00:01:35"}],
            transcript_payload=self.TRANSCRIPT,
            video_duration_sec=self.VIDEO_DUR_SEC,
            min_duration_sec=5.0,
            max_duration_sec=20.0,
        )
        self.assertEqual(valid, [])
        self.assertTrue(
            any(
                i.get("message") == "Clip timestamps exceed source video duration"
                for i in issues
            ),
        )

    def test_accepts_when_interval_inside_cover_and_inside_video_bounds(self):
        valid, issues = validate_and_repair_selection(
            [{"start": "00:01:00", "end": "00:01:10"}],
            transcript_payload=self.TRANSCRIPT,
            video_duration_sec=self.VIDEO_DUR_SEC,
            min_duration_sec=5.0,
            max_duration_sec=20.0,
        )
        self.assertEqual(len(valid), 1)
        self.assertEqual(issues, [])


class RequireTimedTranscriptTests(unittest.TestCase):
    """require_timed_transcript_payload rejects empty/normalized payloads."""

    def test_rejects_normalize_empty_segments_payload(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"text": "only text", "segments": []}')
            payload = normalize_transcript_payload(path)
            with self.assertRaises(ValueError) as ctx:
                require_timed_transcript_payload(payload)
            self.assertIn("no_usable_segments", str(ctx.exception))
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    unittest.main()
