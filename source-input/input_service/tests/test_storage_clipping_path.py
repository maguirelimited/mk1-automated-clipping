"""Integration: input service stores to the same folder video-automation uses for input."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# video-automation project root (sibling of source-input)
MONO_ROOT = ROOT.parent.parent
VA_ROOT = MONO_ROOT / "video-automation"
VA_PIPELINE_CONFIG = VA_ROOT / "config" / "pipeline_config.json"


class StorageClippingPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_va_input = os.environ.pop("VIDEO_AUTOMATION_INPUT_DIR", None)
        self._prev_va_proj = os.environ.pop("VIDEO_AUTOMATION_PROJECT_ROOT", None)

    def tearDown(self) -> None:
        if self._prev_va_input is not None:
            os.environ["VIDEO_AUTOMATION_INPUT_DIR"] = self._prev_va_input
        else:
            os.environ.pop("VIDEO_AUTOMATION_INPUT_DIR", None)
        if self._prev_va_proj is not None:
            os.environ["VIDEO_AUTOMATION_PROJECT_ROOT"] = self._prev_va_proj
        else:
            os.environ.pop("VIDEO_AUTOMATION_PROJECT_ROOT", None)

    def test_default_clipping_dir_matches_pipeline_config(self):
        from input_service import paths

        expected = (MONO_ROOT / "video-automation" / "input").resolve()
        self.assertEqual(paths.video_automation_inputs_dir(), expected)

        if not VA_PIPELINE_CONFIG.is_file():
            self.skipTest(f"Missing pipeline config: {VA_PIPELINE_CONFIG}")

        cfg = json.loads(VA_PIPELINE_CONFIG.read_text(encoding="utf-8"))
        input_rel = str(cfg.get("paths", {}).get("input_folder", ""))
        self.assertEqual(
            input_rel,
            "input",
            "video-automation input_folder must stay aligned with input_service",
        )

        # Same absolute folder mk04_utils would use (scripts live in video-automation/scripts)
        sys.path.insert(0, str(VA_ROOT / "scripts"))
        try:
            import importlib

            import mk04_utils

            importlib.reload(mk04_utils)
        except ImportError:
            self.skipTest("mk04_utils not importable (video-automation venv?)")

        resolved = mk04_utils.ensure_paths(mk04_utils.load_config())["input"]
        self.assertEqual(Path(resolved).resolve(), expected)

    def test_store_ready_writes_under_video_automation_input(self):
        os.environ.pop("VIDEO_AUTOMATION_INPUT_DIR", None)

        from input_service.storage import store_ready

        funnel_id = "_smoketest_storage_verify"
        input_id = "input_20260522T120000Z_abc123ef"
        dest_expected = (
            MONO_ROOT / "video-automation" / "input" / f"{input_id}_{funnel_id}_source.mp4"
        )

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(b"\x00\x00\x00\x18ftypmp42\x00" + b"\x00" * 100)
            src = Path(tmp.name)

        try:
            out = store_ready(src, funnel_id, input_id=input_id)
            self.assertEqual(out.resolve(), dest_expected.resolve())
            self.assertTrue(out.is_file(), f"missing {out}")
            self.assertGreater(out.stat().st_size, 0)
        finally:
            if dest_expected.exists():
                dest_expected.unlink()

    def test_video_automation_project_parent_is_monorepo_root(self):
        from input_service.paths import _video_automation_project_parent

        self.assertEqual(_video_automation_project_parent(), MONO_ROOT.resolve())


if __name__ == "__main__":
    unittest.main()
