"""Acquisition source scan-depth defaults aligned with source-input."""

from __future__ import annotations

from ops_ui.funnel_management.create import parse_funnel_create_form
from ops_ui.funnel_management.create_defaults import DEFAULT_CREATE_MAX_VIDEOS_PER_SOURCE
from ops_ui.funnel_management.schema import DEFAULT_MAX_VIDEOS_PER_SOURCE


def test_default_max_videos_per_source_matches_source_input() -> None:
    assert DEFAULT_MAX_VIDEOS_PER_SOURCE == 25
    assert DEFAULT_CREATE_MAX_VIDEOS_PER_SOURCE == DEFAULT_MAX_VIDEOS_PER_SOURCE


def test_create_form_applies_default_max_videos_in_registry() -> None:
    parsed, errors = parse_funnel_create_form(
        {
            "template_id": "baseline_stream_clips",
            "funnel_id": "scan_depth_test_001",
            "display_name": "Scan Depth Test",
            "category": "general",
            "source_type": "youtube_channel",
            "source_urls": "https://www.youtube.com/@example/videos",
        }
    )
    assert errors == []
    assert parsed is not None

