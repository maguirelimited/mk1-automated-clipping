import os
import sys
import unittest


SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "server"))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import app as server_app


class StartCorrectionTests(unittest.TestCase):
    def test_start_correction_keeps_strong_opening(self):
        segment = {"start": "00:00:00", "end": "00:00:12"}
        whisper_segments = [
            {"start": 0.0, "end": 12.0, "text": "Here is the key idea in one line."}
        ]

        out = server_app._correct_segment_start(
            segment, whisper_segments, min_duration_sec=10.0
        )

        self.assertEqual(out["start"], "00:00:00")

    def test_start_correction_shifts_forward_for_weak_opening(self):
        segment = {"start": "00:00:00", "end": "00:00:14"}
        whisper_segments = [
            {
                "start": 0.0,
                "end": 2.0,
                "text": "so here is the hook and why this works",
            }
        ]

        out = server_app._correct_segment_start(
            segment, whisper_segments, min_duration_sec=10.0
        )

        self.assertNotEqual(out["start"], "00:00:00")
        self.assertGreater(server_app.parse_time_to_seconds(out["start"]), 0.0)
        self.assertLessEqual(server_app.parse_time_to_seconds(out["start"]), 1.0)

    def test_start_correction_respects_min_duration_guard(self):
        segment = {"start": "00:00:00", "end": "00:00:10"}
        whisper_segments = [
            {
                "start": 0.0,
                "end": 10.0,
                "text": "so um anyway here is why this works and what to do next",
            }
        ]

        out = server_app._correct_segment_start(
            segment, whisper_segments, min_duration_sec=10.0
        )

        # Any forward shift would violate minimum duration in this setup.
        self.assertEqual(out["start"], "00:00:00")

    def test_end_correction_extends_incomplete_ending(self):
        segment = {"start": "00:00:00", "end": "00:00:10"}
        whisper_segments = [
            {
                "start": 0.0,
                "end": 13.0,
                "text": "This is setup and context and detail and examples and takeaways. Final line.",
            }
        ]

        out = server_app._correct_segment_end(
            segment,
            whisper_segments,
            min_duration_sec=5.0,
            max_duration_sec=14.0,
        )

        self.assertGreater(server_app.parse_time_to_seconds(out["end"]), 10.0)
        self.assertLessEqual(server_app.parse_time_to_seconds(out["end"]), 12.0)

    def test_end_correction_keeps_resolved_ending(self):
        segment = {"start": "00:00:00", "end": "00:00:10"}
        whisper_segments = [
            {"start": 0.0, "end": 10.0, "text": "That is the full idea and final result."}
        ]

        out = server_app._correct_segment_end(
            segment,
            whisper_segments,
            min_duration_sec=5.0,
            max_duration_sec=14.0,
        )

        self.assertEqual(out["end"], "00:00:10")

    def test_end_correction_respects_max_duration_guard(self):
        segment = {"start": "00:00:00", "end": "00:00:10"}
        whisper_segments = [
            {
                "start": 0.0,
                "end": 20.0,
                "text": "This is still building and payoff appears later in the timeline.",
            }
        ]

        out = server_app._correct_segment_end(
            segment,
            whisper_segments,
            min_duration_sec=5.0,
            max_duration_sec=10.0,
        )

        # Already at max duration, no forward extension allowed.
        self.assertEqual(out["end"], "00:00:10")


if __name__ == "__main__":
    unittest.main()
