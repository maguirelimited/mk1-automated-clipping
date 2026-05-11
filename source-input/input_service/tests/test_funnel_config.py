import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from input_service.candidate_filter import filter_candidates
from input_service.duplicate_store import DuplicateStore
from input_service.funnel_loader import load_funnel, list_funnels
from input_service.source_checker import Candidate
from app import create_app


def _write_funnels(tmp_path: Path, funnels: list[dict]) -> Path:
    path = tmp_path / "funnels.json"
    path.write_text(json.dumps(funnels), encoding="utf-8")
    return path


def _funnel_payload() -> dict:
    return {
        "funnel_id": "demo_funnel",
        "angle": "demo podcasts",
        "source_type": "youtube_channels",
        "pipeline_profile": "demo_profile",
        "sources": [
            {
                "source_id": "channel_a",
                "label": "Channel A",
                "source_type": "youtube_channel",
                "url": "https://www.youtube.com/@ChannelA/videos",
                "max_videos_per_source": 12,
                "title_blocklist": ["webinar"],
            }
        ],
        "min_duration_minutes": 20,
        "max_duration_minutes": 120,
        "posting_config": {
            "enabled": False,
            "mode": "manual_review",
            "platforms": ["tiktok"],
        },
        "analytics_config": {
            "enabled": True,
            "event_namespace": "demo_funnel",
            "webhook_url": "https://example.invalid/hooks/analytics",
        },
        "active": True,
    }


class FunnelConfigTests(unittest.TestCase):
    def test_load_funnel_accepts_rich_source_and_ops_config(self):
        with tempfile.TemporaryDirectory() as td:
            path = _write_funnels(Path(td), [_funnel_payload()])
            funnel = load_funnel("demo_funnel", funnels_file=path)

        self.assertEqual(funnel.pipeline_profile, "demo_profile")
        self.assertEqual(
            funnel.posting_config,
            {
                "enabled": False,
                "mode": "manual_review",
                "platforms": ["tiktok"],
            },
        )
        self.assertTrue(funnel.analytics_config["enabled"])
        self.assertEqual(funnel.sources, ("https://www.youtube.com/@ChannelA/videos",))
        source = funnel.source_configs[0]
        self.assertEqual(source.source_id, "channel_a")
        self.assertEqual(source.source_type, "youtube_channel")
        self.assertEqual(source.max_videos_per_source, 12)
        self.assertEqual(source.title_blocklist, ("webinar",))

    def test_source_specific_title_blocklist_rejects_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            path = _write_funnels(tmp_path, [_funnel_payload()])
            funnel = load_funnel("demo_funnel", funnels_file=path)
            cand = Candidate(
                video_id="v1",
                url="https://www.youtube.com/watch?v=v1",
                title="Founder webinar with a useful guest",
                source=funnel.sources[0],
                duration_seconds=60 * 60,
                extra={"title_blocklist": ["webinar"]},
            )

            valid, rejected = filter_candidates(
                [cand], funnel, DuplicateStore(file=tmp_path / "seen.json")
            )

        self.assertEqual(valid, [])
        self.assertEqual(rejected[0].reason, "title_blocked:webinar")

    def test_list_funnels_returns_manifest_for_onboarding(self):
        with tempfile.TemporaryDirectory() as td:
            payload = _funnel_payload()
            payload["active"] = False
            path = _write_funnels(Path(td), [payload])
            manifest = list_funnels(funnels_file=path, include_inactive=True)

        self.assertEqual(manifest[0]["funnel_id"], "demo_funnel")
        self.assertEqual(manifest[0]["pipeline_profile"], "demo_profile")
        self.assertEqual(manifest[0]["posting_config"]["mode"], "manual_review")
        self.assertEqual(manifest[0]["analytics_config"]["event_namespace"], "demo_funnel")
        self.assertEqual(manifest[0]["sources"][0]["source_id"], "channel_a")

    def test_pipeline_profile_defaults_to_funnel_id(self):
        with tempfile.TemporaryDirectory() as td:
            payload = _funnel_payload()
            payload.pop("pipeline_profile", None)
            path = _write_funnels(Path(td), [payload])
            funnel = load_funnel("demo_funnel", funnels_file=path)
        self.assertEqual(funnel.pipeline_profile, "demo_funnel")

    def test_doctor_reports_valid_catalog(self):
        app = create_app()
        with patch("app.list_funnels", return_value=[{"funnel_id": "demo", "active": True}]):
            resp = app.test_client().get("/doctor")
        self.assertIn(resp.status_code, (200, 500))
        payload = resp.get_json()
        self.assertEqual(payload["service"], "input_service")
        self.assertTrue(any(c["name"] == "funnels_config" for c in payload["checks"]))


if __name__ == "__main__":
    unittest.main()
