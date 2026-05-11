"""Unit tests for post-encode clip mux validation (`clip_duration_tolerance_sec`,
`_assert_clip_output_matches_request`).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import clip_video


class ClipDurationToleranceTests(unittest.TestCase):
    def test_short_clip_bounds_scale(self):
        lo, hi = clip_video.clip_duration_tolerance_sec(10.0)
        self.assertAlmostEqual(lo, max(0.55, 6.2), places=3)
        self.assertAlmostEqual(hi, 11.75, places=2)

    def test_invalid_expected_returns_zero_bounds(self):
        lo, hi = clip_video.clip_duration_tolerance_sec(float("nan"))
        self.assertEqual((lo, hi), (0.0, 0.0))


MIN_VALID_DEMUX = {
    "format": {"duration": "25.980", "format_long_name": "QuickTime / MOV", "bit_rate": "500000"},
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1280,
            "height": 720,
            "duration": "25.980",
        }
    ],
}


class ClipOutputAssertionTests(unittest.TestCase):
    """Demux assertions without calling real ffprobe/ffmpeg (patched infra)."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        os.truncate(path, 15_000)
        self.clip_path = path

    def tearDown(self):
        try:
            os.unlink(self.clip_path)
        except FileNotFoundError:
            pass

    @patch.object(clip_video, "_ffmpeg_null_decode")
    @patch.object(clip_video, "_ffprobe_demux_json")
    def test_assert_accepts_matching_mux(self, mock_demux, mock_decode):
        mock_demux.return_value = MIN_VALID_DEMUX
        out = clip_video._assert_clip_output_matches_request(self.clip_path, 25.0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["codec_name"], "h264")
        self.assertAlmostEqual(float(out["ffprobe_duration_sec"]), 25.98, places=2)
        mock_decode.assert_called_once()

    @patch.object(clip_video, "_ffmpeg_null_decode")
    @patch.object(clip_video, "_ffprobe_demux_json")
    def test_assert_rejects_duration_far_short(self, mock_demux, mock_decode):
        short = dict(MIN_VALID_DEMUX)
        short["format"] = dict(short["format"])
        short["format"]["duration"] = "4.500"
        short["streams"] = [dict(short["streams"][0])]
        short["streams"][0]["duration"] = "4.500"
        mock_demux.return_value = short
        with self.assertRaises(RuntimeError) as ctx:
            clip_video._assert_clip_output_matches_request(self.clip_path, 25.0)
        self.assertIn("too_short", str(ctx.exception))
        mock_decode.assert_not_called()

    @patch.object(clip_video, "_ffmpeg_null_decode")
    @patch.object(clip_video, "_ffprobe_demux_json")
    def test_assert_rejects_no_video_stream(self, mock_demux, mock_decode):
        mock_demux.return_value = {
            "format": {"duration": "10"},
            "streams": [{"codec_type": "audio", "codec_name": "aac"}],
        }
        with self.assertRaises(RuntimeError) as ctx:
            clip_video._assert_clip_output_matches_request(self.clip_path, 9.8)
        self.assertIn("no_video_stream", str(ctx.exception))
        mock_decode.assert_not_called()


if __name__ == "__main__":
    unittest.main()
