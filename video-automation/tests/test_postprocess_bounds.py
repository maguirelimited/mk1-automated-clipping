import os
import sys
import unittest


SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from pipeline_utils import postprocess_segments


def _seg(start: str, end: str) -> dict:
    return {"start": start, "end": end}


class PostprocessVideoDurationBoundsTests(unittest.TestCase):
    """Bounds-check behaviour added in Bug 3.

    `postprocess_segments` accepts an optional `video_duration_sec` kwarg.
    A candidate is rejected when start >= video_duration_sec OR end > video_duration_sec.
    The boundary is inclusive on the end side: end == video_duration_sec is valid.
    When video_duration_sec is None, the bounds check is skipped entirely
    (backwards compatibility).
    """

    DURATION = 100.0

    COMMON_KW = {
        "min_duration_sec": 5.0,
        "max_duration_sec": 20.0,
        "max_overlap_sec": 2.0,
    }

    def test_end_exactly_at_video_duration_passes(self):
        result = postprocess_segments(
            [_seg("00:01:30", "00:01:40")],
            video_duration_sec=self.DURATION,
            **self.COMMON_KW,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["start"], "00:01:30")
        self.assertEqual(result[0]["end"], "00:01:40")

    def test_start_exactly_at_video_duration_is_rejected(self):
        result = postprocess_segments(
            [_seg("00:01:40", "00:01:50")],
            video_duration_sec=self.DURATION,
            **self.COMMON_KW,
        )
        self.assertEqual(result, [])

    def test_end_extends_past_video_duration_is_rejected(self):
        result = postprocess_segments(
            [_seg("00:01:35", "00:01:45")],
            video_duration_sec=self.DURATION,
            **self.COMMON_KW,
        )
        self.assertEqual(result, [])

    def test_segment_entirely_past_video_duration_is_rejected(self):
        result = postprocess_segments(
            [_seg("00:02:00", "00:02:10")],
            video_duration_sec=self.DURATION,
            **self.COMMON_KW,
        )
        self.assertEqual(result, [])

    def test_video_duration_none_is_backwards_compatible(self):
        out_of_range = _seg("00:02:00", "00:02:10")
        result = postprocess_segments(
            [out_of_range],
            video_duration_sec=None,
            **self.COMMON_KW,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["start"], "00:02:00")
        self.assertEqual(result[0]["end"], "00:02:10")

    def test_video_duration_default_is_backwards_compatible(self):
        out_of_range = _seg("00:02:00", "00:02:10")
        result = postprocess_segments(
            [out_of_range],
            **self.COMMON_KW,
        )
        self.assertEqual(len(result), 1)

    def test_in_range_kept_alongside_out_of_range_dropped(self):
        result = postprocess_segments(
            [
                _seg("00:00:10", "00:00:20"),
                _seg("00:01:35", "00:01:45"),
                _seg("00:01:30", "00:01:40"),
            ],
            video_duration_sec=self.DURATION,
            **self.COMMON_KW,
        )
        starts = sorted(item["start"] for item in result)
        self.assertEqual(starts, ["00:00:10", "00:01:30"])

    def test_llm_primary_allows_shorter_clips_that_strict_would_drop(self):
        """When LLM enforces bounds, postprocess only applies a loose safety net."""
        short = _seg("00:00:10", "00:00:15")  # 5s — below min 5 in COMMON but at edge
        # Use tighter min so strict tolerances would still accept some 5s at 5*0.7=3.5 — actually min 5, 5s clip: duration 5 >= 3.5 passes strict too.
        # min 30 max 60: 5s clip fails strict badly
        kw = {
            "min_duration_sec": 30.0,
            "max_duration_sec": 60.0,
            "max_overlap_sec": 2.0,
        }
        strict_empty = postprocess_segments(
            [_seg("00:00:10", "00:00:15")],
            video_duration_sec=self.DURATION,
            **kw,
        )
        self.assertEqual(len(strict_empty), 0)
        llm_ok = postprocess_segments(
            [_seg("00:00:10", "00:00:15")],
            video_duration_sec=self.DURATION,
            duration_policy="llm_primary",
            **kw,
        )
        self.assertEqual(len(llm_ok), 1)


if __name__ == "__main__":
    unittest.main()
