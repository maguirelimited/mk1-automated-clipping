import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import clip_video


def _ok_completed() -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")


class ClipVideoBoundCheckTests(unittest.TestCase):
    """Bug 6 lock-in. `clip_video.run_clip` checks bounds against ffprobe duration.

    Out-of-range timestamps must raise ValueError; `end_sec == input_duration_sec`
    passes the bound check when ffprobe succeeds; unknown or non-positive input
    duration must fail loudly (no ffmpeg)."
    """

    INPUT_DURATION_SEC = 100.0

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.write(b"fake bytes")
        tmp.close()
        self.input_path = tmp.name
        self.output_path = self.input_path + ".clip.mp4"

    def tearDown(self):
        for path in (self.input_path, self.output_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    @patch("clip_video.ffprobe_duration_sec")
    def test_rejects_when_end_exceeds_input_duration(self, mock_ffprobe):
        mock_ffprobe.return_value = self.INPUT_DURATION_SEC

        with self.assertRaises(ValueError) as ctx:
            clip_video.run_clip(
                self.input_path,
                "00:01:30",
                "00:01:50",
                output_path=self.output_path,
            )

        self.assertIn("exceed input video duration", str(ctx.exception))

    @patch("clip_video.ffprobe_duration_sec")
    def test_rejects_when_start_exceeds_input_duration(self, mock_ffprobe):
        mock_ffprobe.return_value = self.INPUT_DURATION_SEC

        with self.assertRaises(ValueError) as ctx:
            clip_video.run_clip(
                self.input_path,
                "00:02:00",
                "00:02:10",
                output_path=self.output_path,
            )

        self.assertIn("exceed input video duration", str(ctx.exception))

    @patch("clip_video.subprocess.run")
    @patch("clip_video.ffprobe_duration_sec")
    def test_boundary_end_equals_input_duration_passes_bound_check(
        self,
        mock_ffprobe,
        mock_subprocess,
    ):
        mock_ffprobe.return_value = self.INPUT_DURATION_SEC
        mock_subprocess.return_value = _ok_completed()

        with self.assertRaises(Exception) as ctx:
            clip_video.run_clip(
                self.input_path,
                "00:01:30",
                "00:01:40",
                output_path=self.output_path,
            )

        self.assertNotIn(
            "exceed input video duration",
            str(ctx.exception),
            "bound check must pass when end_sec == input_duration_sec",
        )
        mock_subprocess.assert_called_once()

    @patch("clip_video.subprocess.run")
    @patch("clip_video.ffprobe_duration_sec")
    def test_rejects_when_input_duration_unknown(self, mock_ffprobe, mock_subprocess):
        mock_ffprobe.return_value = None

        with self.assertRaises(ValueError) as ctx:
            clip_video.run_clip(
                self.input_path,
                "00:05:00",
                "00:05:10",
                output_path=self.output_path,
            )

        self.assertIn(
            "CLIP_REJECTED unavailable_input_duration",
            str(ctx.exception),
        )
        mock_subprocess.assert_not_called()


if __name__ == "__main__":
    unittest.main()
