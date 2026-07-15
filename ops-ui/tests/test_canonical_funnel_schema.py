"""Unit tests for the canonical funnel schema (Funnel Management MK1)."""

from __future__ import annotations

import copy
import pytest

from ops_ui.funnel_management.schema import (
    CanonicalFunnelSchemaError,
    dump_canonical_funnel,
    load_canonical_funnel,
)


def _valid_funnel(*, include_pipeline_profile: bool = True) -> dict:
    processing = {
        "ai_rules": {"ai_rule_profile": "business"},
        "selection": {
            "max_clips": 6,
            "min_clip_duration_sec": 20,
            "max_clip_duration_sec": 90,
            "max_overlap_sec": 5,
        },
        "output": {
            "filename_prefix": "mfm_business_ai",
            "delivery_mode": "handoff",
        },
        "platforms": {
            "youtube_shorts": True,
            "tiktok": True,
            "instagram_reels": True,
            "facebook_reels": False,
            "x": False,
        },
    }
    if include_pipeline_profile:
        processing["pipeline_profile"] = "mfm_business_ai_001"

    return {
        "schema_version": 1,
        "identity": {
            "funnel_id": "mfm_business_ai_001",
            "display_name": "MFM Business AI",
            "description": "Business and AI podcast clipping funnel",
            "category": "business",
            "enabled": True,
            "environment": "prod",
            "status": "active",
            "template_source": None,
            "created_at": "2026-07-04T00:00:00Z",
            "updated_at": "2026-07-04T00:00:00Z",
            "operator_note": None,
        },
        "acquisition": {
            "source_type": "youtube_channel",
            "sources": [
                {
                    "source_id": "my_first_million",
                    "label": "My First Million",
                    "url": "https://www.youtube.com/@MyFirstMillionPod",
                    "source_type": "youtube_channel",
                    "active": True,
                    "max_videos_per_source": 5,
                    "hydrate_missing_duration": True,
                    "title_allowlist": [],
                    "title_blocklist": [],
                }
            ],
            "min_duration_minutes": 20,
            "max_duration_minutes": 180,
            "max_downloads_per_run": 1,
        },
        "processing": processing,
        "distribution": {
            "posting_enabled": True,
            "posting_mode": "manual_review",
            "target_platforms": ["youtube_shorts", "tiktok", "instagram_reels"],
            "channel_routes": [
                {
                    "channel_id": "mfm_business_ai_primary",
                    "platform": "youtube_shorts",
                    "enabled": True,
                }
            ],
        },
        "mappings": {
            "config_manager_funnel_id": "business",
        },
    }


class TestValidSchema:
    def test_complete_valid_funnel_parses(self) -> None:
        funnel = load_canonical_funnel(_valid_funnel())
        assert funnel.identity.funnel_id == "mfm_business_ai_001"
        assert funnel.processing.ai_rules.ai_rule_profile == "business"
        assert funnel.mappings.config_manager_funnel_id == "business"

    def test_dump_serialises_to_plain_dict(self) -> None:
        funnel = load_canonical_funnel(_valid_funnel())
        dumped = dump_canonical_funnel(funnel)
        assert isinstance(dumped, dict)
        assert dumped["schema_version"] == 1
        assert dumped["identity"]["funnel_id"] == "mfm_business_ai_001"

    def test_round_trip_preserves_values(self) -> None:
        original = _valid_funnel()
        funnel = load_canonical_funnel(original)
        dumped = dump_canonical_funnel(funnel)
        again = load_canonical_funnel(dumped)
        assert again.identity.funnel_id == funnel.identity.funnel_id
        assert again.processing.selection.max_clips == funnel.processing.selection.max_clips
        assert again.distribution.channel_routes[0].channel_id == "mfm_business_ai_primary"

    def test_pipeline_profile_defaults_to_funnel_id(self) -> None:
        data = _valid_funnel(include_pipeline_profile=False)
        funnel = load_canonical_funnel(data)
        assert funnel.processing.pipeline_profile == "mfm_business_ai_001"

    def test_source_defaults_for_optional_lists_and_hydrate(self) -> None:
        data = _valid_funnel()
        source = data["acquisition"]["sources"][0]
        del source["title_allowlist"]
        del source["title_blocklist"]
        del source["hydrate_missing_duration"]
        funnel = load_canonical_funnel(data)
        parsed_source = funnel.acquisition.sources[0]
        assert parsed_source.title_allowlist == ()
        assert parsed_source.title_blocklist == ()
        assert parsed_source.hydrate_missing_duration is True


class TestStrictUnknownFields:
    def test_unknown_top_level_field_fails(self) -> None:
        data = _valid_funnel()
        data["readiness"] = {"readiness_status": "ready"}
        with pytest.raises(CanonicalFunnelSchemaError, match="Unknown field"):
            load_canonical_funnel(data)

    def test_unknown_nested_field_fails(self) -> None:
        data = _valid_funnel()
        data["identity"]["pause_state"] = True
        with pytest.raises(CanonicalFunnelSchemaError, match="Unknown field"):
            load_canonical_funnel(data)

    def test_unknown_platform_key_fails(self) -> None:
        data = _valid_funnel()
        data["processing"]["platforms"]["snapchat"] = True
        with pytest.raises(CanonicalFunnelSchemaError, match="Unknown field"):
            load_canonical_funnel(data)


class TestRequiredFields:
    @pytest.mark.parametrize(
        "mutator,match",
        [
            (lambda d: d.pop("identity"), "identity"),
            (lambda d: d.pop("acquisition"), "acquisition"),
            (lambda d: d.pop("processing"), "processing"),
            (lambda d: d.pop("distribution"), "distribution"),
            (lambda d: d.pop("mappings"), "mappings"),
        ],
    )
    def test_missing_section_fails(self, mutator, match: str) -> None:
        data = _valid_funnel()
        mutator(data)
        with pytest.raises(CanonicalFunnelSchemaError, match=match):
            load_canonical_funnel(data)

    def test_missing_identity_funnel_id_fails(self) -> None:
        data = _valid_funnel()
        del data["identity"]["funnel_id"]
        with pytest.raises(CanonicalFunnelSchemaError, match="identity.funnel_id"):
            load_canonical_funnel(data)

    def test_missing_display_name_fails(self) -> None:
        data = _valid_funnel()
        del data["identity"]["display_name"]
        with pytest.raises(CanonicalFunnelSchemaError, match="identity.display_name"):
            load_canonical_funnel(data)

    def test_missing_ai_rule_profile_fails(self) -> None:
        data = _valid_funnel()
        del data["processing"]["ai_rules"]["ai_rule_profile"]
        with pytest.raises(CanonicalFunnelSchemaError, match="ai_rule_profile"):
            load_canonical_funnel(data)


class TestValueValidation:
    def test_invalid_funnel_id_fails(self) -> None:
        data = _valid_funnel()
        data["identity"]["funnel_id"] = "Bad-ID"
        with pytest.raises(CanonicalFunnelSchemaError, match="identity.funnel_id"):
            load_canonical_funnel(data)

    def test_funnel_id_with_slash_fails(self) -> None:
        data = _valid_funnel()
        data["identity"]["funnel_id"] = "../escape"
        with pytest.raises(CanonicalFunnelSchemaError, match="identity.funnel_id"):
            load_canonical_funnel(data)

    def test_empty_display_name_fails(self) -> None:
        data = _valid_funnel()
        data["identity"]["display_name"] = "   "
        with pytest.raises(CanonicalFunnelSchemaError, match="display_name"):
            load_canonical_funnel(data)

    def test_invalid_environment_fails(self) -> None:
        data = _valid_funnel()
        data["identity"]["environment"] = "staging"
        with pytest.raises(CanonicalFunnelSchemaError, match="environment"):
            load_canonical_funnel(data)

    def test_invalid_status_fails(self) -> None:
        data = _valid_funnel()
        data["identity"]["status"] = "healthy"
        with pytest.raises(CanonicalFunnelSchemaError, match="status"):
            load_canonical_funnel(data)

    def test_invalid_duration_bounds_fail(self) -> None:
        data = _valid_funnel()
        data["acquisition"]["min_duration_minutes"] = 200
        data["acquisition"]["max_duration_minutes"] = 100
        with pytest.raises(CanonicalFunnelSchemaError, match="min_duration_minutes"):
            load_canonical_funnel(data)

    def test_invalid_acquisition_source_type_fails(self) -> None:
        data = _valid_funnel()
        data["acquisition"]["source_type"] = "yt_dlp_collection"
        with pytest.raises(CanonicalFunnelSchemaError, match="acquisition.source_type"):
            load_canonical_funnel(data)

    def test_invalid_per_source_type_fails(self) -> None:
        data = _valid_funnel()
        data["acquisition"]["sources"][0]["source_type"] = "youtube_channels"
        with pytest.raises(CanonicalFunnelSchemaError, match="source_type"):
            load_canonical_funnel(data)

    def test_playlist_source_types_validate(self) -> None:
        data = _valid_funnel()
        data["acquisition"]["source_type"] = "youtube_playlist"
        data["acquisition"]["sources"][0]["source_type"] = "youtube_playlist"
        data["acquisition"]["sources"][0]["url"] = "https://www.youtube.com/playlist?list=PLtest"
        funnel = load_canonical_funnel(data)
        assert funnel.acquisition.source_type == "youtube_playlist"
        assert funnel.acquisition.sources[0].source_type == "youtube_playlist"

    def test_invalid_clip_duration_bounds_fail(self) -> None:
        data = _valid_funnel()
        data["processing"]["selection"]["min_clip_duration_sec"] = 90
        data["processing"]["selection"]["max_clip_duration_sec"] = 20
        with pytest.raises(CanonicalFunnelSchemaError, match="min_clip_duration_sec"):
            load_canonical_funnel(data)

    def test_invalid_posting_mode_fails(self) -> None:
        data = _valid_funnel()
        data["distribution"]["posting_mode"] = "immediate"
        with pytest.raises(CanonicalFunnelSchemaError, match="posting_mode"):
            load_canonical_funnel(data)

    def test_unsupported_target_platform_fails(self) -> None:
        data = _valid_funnel()
        data["distribution"]["target_platforms"] = ["snapchat"]
        with pytest.raises(CanonicalFunnelSchemaError, match="target_platforms"):
            load_canonical_funnel(data)

    def test_unsafe_filename_prefix_fails(self) -> None:
        data = _valid_funnel()
        data["processing"]["output"]["filename_prefix"] = "../bad"
        with pytest.raises(CanonicalFunnelSchemaError, match="filename_prefix"):
            load_canonical_funnel(data)

    def test_unsupported_schema_version_fails(self) -> None:
        data = _valid_funnel()
        data["schema_version"] = 2
        with pytest.raises(CanonicalFunnelSchemaError, match="Unsupported schema_version"):
            load_canonical_funnel(data)


class TestExplicitExclusions:
    @pytest.mark.parametrize(
        "field_name,payload",
        [
            ("readiness", {"readiness_status": "ready"}),
            ("operations", {"can_edit": True}),
            ("pause_state", {"funnel_paused": True}),
            ("run_counts", {"queue_depth": 3}),
            ("analytics", {"views": 100}),
            ("prompt_text", {"prompt": "select clips"}),
            ("oauth", {"token_file": "/secrets/token.json"}),
        ],
    )
    def test_excluded_top_level_fields_fail(self, field_name: str, payload: dict) -> None:
        data = _valid_funnel()
        data[field_name] = payload
        with pytest.raises(CanonicalFunnelSchemaError, match="Unknown field"):
            load_canonical_funnel(data)

    def test_prompt_text_in_processing_fails(self) -> None:
        data = _valid_funnel()
        data["processing"]["prompt_text"] = "full prompt"
        with pytest.raises(CanonicalFunnelSchemaError, match="Unknown field"):
            load_canonical_funnel(data)

    def test_oauth_in_channel_route_fails(self) -> None:
        data = _valid_funnel()
        data["distribution"]["channel_routes"][0]["credentials"] = {"token_file_env": "X"}
        with pytest.raises(CanonicalFunnelSchemaError, match="Unknown field"):
            load_canonical_funnel(data)
