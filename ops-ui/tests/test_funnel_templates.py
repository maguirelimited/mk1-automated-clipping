"""Unit tests for built-in funnel templates (Funnel Management MK1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops_ui.funnel_management.funnel_templates import (
    FunnelTemplateError,
    build_funnel_from_template,
    get_funnel_template,
    list_funnel_templates,
)
from ops_ui.funnel_management.registry import FunnelRegistry
from ops_ui.funnel_management.schema import dump_canonical_funnel


FUNNEL_ID = "new_podcast_funnel_001"
TEMPLATE_ID = "youtube_podcast_basic"

SAMPLE_SOURCE = {
    "source_id": "example_channel",
    "label": "Example Channel",
    "url": "https://www.youtube.com/@example/videos",
    "source_type": "youtube_channel",
    "active": True,
    "max_videos_per_source": 5,
    "hydrate_missing_duration": True,
    "title_allowlist": [],
    "title_blocklist": [],
}


class TestCatalogue:
    def test_list_returns_builtin_templates(self) -> None:
        templates = list_funnel_templates()
        assert len(templates) >= 4
        ids = [template.template_id for template in templates]
        assert ids[0] == "baseline_stream_clips"
        assert len(ids) == len(set(ids))

    def test_get_baseline_stream_clips(self) -> None:
        template = get_funnel_template("baseline_stream_clips")
        assert template.template_id == "baseline_stream_clips"
        assert template.defaults["processing"]["ai_rule_profile"] == "business"
        assert template.defaults["distribution"]["posting_enabled"] is False

    def test_get_youtube_podcast_basic(self) -> None:
        template = get_funnel_template("youtube_podcast_basic")
        assert template.template_id == "youtube_podcast_basic"
        assert template.defaults["processing"]["ai_rule_profile"] == "business"

    def test_get_unknown_template_fails(self) -> None:
        with pytest.raises(FunnelTemplateError, match="Unknown funnel template"):
            get_funnel_template("missing_template")


class TestDraftGeneration:
    def test_build_from_podcast_template(self) -> None:
        funnel = build_funnel_from_template(
            TEMPLATE_ID,
            funnel_id=FUNNEL_ID,
            display_name="New Podcast Funnel",
            environment="dev",
        )
        assert funnel.identity.funnel_id == FUNNEL_ID
        assert funnel.identity.display_name == "New Podcast Funnel"
        assert funnel.identity.status == "draft"
        assert funnel.identity.enabled is False
        assert funnel.identity.template_source == TEMPLATE_ID
        assert funnel.processing.pipeline_profile == FUNNEL_ID
        assert funnel.processing.ai_rules.ai_rule_profile == "business"
        assert funnel.distribution.posting_enabled is False
        assert funnel.distribution.channel_routes == ()
        assert funnel.acquisition.sources == ()

    def test_dump_serialises(self) -> None:
        funnel = build_funnel_from_template(
            TEMPLATE_ID,
            funnel_id=FUNNEL_ID,
            display_name="New Podcast Funnel",
            environment="dev",
        )
        dumped = dump_canonical_funnel(funnel)
        assert dumped["identity"]["funnel_id"] == FUNNEL_ID
        assert dumped["processing"]["ai_rules"]["ai_rule_profile"] == "business"

    def test_all_builtin_templates_generate_valid_funnels(self) -> None:
        for index, template in enumerate(list_funnel_templates()):
            funnel = build_funnel_from_template(
                template.template_id,
                funnel_id=f"template_test_{index}",
                display_name=template.display_name,
                environment="dev",
            )
            assert funnel.identity.status == "draft"
            assert funnel.identity.enabled is False


class TestOverrides:
    def test_sources_override(self) -> None:
        funnel = build_funnel_from_template(
            TEMPLATE_ID,
            funnel_id=FUNNEL_ID,
            display_name="New Podcast Funnel",
            environment="dev",
            sources=[SAMPLE_SOURCE],
        )
        assert len(funnel.acquisition.sources) == 1
        assert funnel.acquisition.sources[0].source_id == "example_channel"

    def test_channel_routes_override(self) -> None:
        routes = [
            {
                "channel_id": "primary_channel",
                "platform": "youtube_shorts",
                "enabled": True,
            }
        ]
        funnel = build_funnel_from_template(
            TEMPLATE_ID,
            funnel_id=FUNNEL_ID,
            display_name="New Podcast Funnel",
            environment="dev",
            channel_routes=routes,
        )
        assert len(funnel.distribution.channel_routes) == 1
        assert funnel.distribution.channel_routes[0].channel_id == "primary_channel"
        assert funnel.distribution.posting_enabled is False

    def test_target_platforms_override(self) -> None:
        funnel = build_funnel_from_template(
            TEMPLATE_ID,
            funnel_id=FUNNEL_ID,
            display_name="New Podcast Funnel",
            environment="dev",
            target_platforms=["youtube_shorts"],
        )
        assert funnel.distribution.target_platforms == ("youtube_shorts",)

    def test_category_and_description_override(self) -> None:
        funnel = build_funnel_from_template(
            TEMPLATE_ID,
            funnel_id=FUNNEL_ID,
            display_name="New Podcast Funnel",
            environment="dev",
            category="finance",
            description="Custom description",
        )
        assert funnel.identity.category == "finance"
        assert funnel.identity.description == "Custom description"


class TestInvalidInput:
    def test_invalid_funnel_id_fails(self) -> None:
        with pytest.raises(FunnelTemplateError, match="funnel_id"):
            build_funnel_from_template(
                TEMPLATE_ID,
                funnel_id="Bad-ID",
                display_name="Name",
                environment="dev",
            )

    def test_invalid_environment_fails(self) -> None:
        with pytest.raises(FunnelTemplateError, match="environment"):
            build_funnel_from_template(
                TEMPLATE_ID,
                funnel_id=FUNNEL_ID,
                display_name="Name",
                environment="staging",
            )

    def test_invalid_target_platform_fails(self) -> None:
        with pytest.raises(FunnelTemplateError, match="schema validation"):
            build_funnel_from_template(
                TEMPLATE_ID,
                funnel_id=FUNNEL_ID,
                display_name="Name",
                environment="dev",
                target_platforms=["snapchat"],
            )

    def test_invalid_source_fails_schema(self) -> None:
        with pytest.raises(FunnelTemplateError, match="schema validation"):
            build_funnel_from_template(
                TEMPLATE_ID,
                funnel_id=FUNNEL_ID,
                display_name="Name",
                environment="dev",
                sources=[{"source_id": "x"}],
            )


class TestScopeProtection:
    def test_generated_funnel_has_no_forbidden_fields(self) -> None:
        funnel = build_funnel_from_template(
            TEMPLATE_ID,
            funnel_id=FUNNEL_ID,
            display_name="New Podcast Funnel",
            environment="dev",
            sources=[SAMPLE_SOURCE],
        )
        dumped = dump_canonical_funnel(funnel)
        forbidden = {
            "readiness",
            "operations",
            "pause_state",
            "queue_depth",
            "analytics",
            "revenue",
            "prompt_text",
            "credentials",
            "oauth",
        }
        assert forbidden.isdisjoint(dumped.keys())
        payload = json.dumps(dumped)
        assert "prompt_text" not in payload
        assert "oauth" not in payload.lower()

    def test_build_does_not_write_registry_or_runtime_files(self, tmp_path: Path) -> None:
        registry_dir = tmp_path / "registry"
        registry_dir.mkdir()
        runtime_file = tmp_path / "runtime.json"
        runtime_file.write_text("{}", encoding="utf-8")

        build_funnel_from_template(
            TEMPLATE_ID,
            funnel_id=FUNNEL_ID,
            display_name="New Podcast Funnel",
            environment="dev",
        )

        assert list(registry_dir.glob("*.json")) == []
        assert runtime_file.read_text(encoding="utf-8") == "{}"
        registry = FunnelRegistry(registry_dir)
        assert registry.list_funnels() == []

    def test_youtube_playlist_template_exists(self) -> None:
        template = get_funnel_template("youtube_playlist_basic")
        assert template.source_type == "youtube_playlist"
