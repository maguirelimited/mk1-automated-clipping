"""Unit tests for the canonical funnel import layer (Funnel Management MK1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops_ui.funnel_management.importer import ExistingFunnelImporter, FunnelImportError
from ops_ui.funnel_management.registry import DuplicateFunnelError, FunnelRegistry
from ops_ui.funnel_management.schema import dump_canonical_funnel


FUNNEL_ID = "mfm_business_ai_001"

AI_RULES_SNIPPET = '''
FUNNEL_RULE_ALIASES = {
    "business": "business",
    "business_ai": "business",
    "mfm_business_ai_001": "business",
    "finance": "finance",
}
'''


def _write_fixture_tree(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    source_path = root / "funnels.json"
    source_path.write_text(
        json.dumps(
            [
                {
                    "funnel_id": FUNNEL_ID,
                    "angle": "business productivity ai leverage podcasts",
                    "source_type": "youtube_channels",
                    "pipeline_profile": FUNNEL_ID,
                    "sources": [
                        {
                            "source_id": "my_first_million",
                            "label": "My First Million",
                            "source_type": "youtube_channel",
                            "url": "https://www.youtube.com/@MyFirstMillionPod/videos",
                            "active": True,
                            "max_videos_per_source": 25,
                            "hydrate_missing_duration": True,
                            "title_blocklist": ["shorts", "clip"],
                        }
                    ],
                    "min_duration_minutes": 25,
                    "max_duration_minutes": 180,
                    "max_downloads_per_run": 1,
                    "posting_config": {
                        "enabled": False,
                        "mode": "manual_review",
                        "platforms": ["youtube_shorts"],
                    },
                    "analytics_config": {"enabled": False},
                    "active": True,
                }
            ]
        ),
        encoding="utf-8",
    )

    video_dir = root / "video_funnels"
    video_dir.mkdir(parents=True, exist_ok=True)
    video_path = video_dir / f"{FUNNEL_ID}.json"
    video_path.write_text(
        json.dumps(
            {
                "funnel_id": FUNNEL_ID,
                "funnel_name": "MFM Business AI",
                "platforms": {
                    "tiktok": False,
                    "instagram_reels": False,
                    "youtube_shorts": True,
                    "x": False,
                },
                "selection": {
                    "max_clips": 6,
                    "min_duration_sec": 15,
                    "max_duration_sec": 60,
                    "max_overlap_sec": 2,
                },
                "output": {
                    "filename_prefix": "mfm_business_ai",
                    "delivery_mode": "pull_from_output_endpoint",
                },
            }
        ),
        encoding="utf-8",
    )

    channels_path = root / "channels.json"
    channels_path.write_text(
        json.dumps(
            {
                "channels": [
                    {
                        "channel_id": "mfm_business_ai_primary",
                        "platform": "youtube_shorts",
                        "enabled": True,
                        "routing": {"accepted_funnel_ids": [FUNNEL_ID]},
                        "credentials": {"token_file_env": "SECRET"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    ai_path = root / "section_candidate_discovery.py"
    ai_path.write_text(AI_RULES_SNIPPET, encoding="utf-8")

    config_dir = root / "config_funnels"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "business.yaml").write_text(
        "funnel:\n  id: business\n  name: Business\n",
        encoding="utf-8",
    )

    return {
        "source": source_path,
        "video_dir": video_dir,
        "channels": channels_path,
        "ai_rules": ai_path,
        "config_dir": config_dir,
    }


def _importer_from_paths(
    paths: dict[str, Path],
    *,
    environment: str = "dev",
    with_channels: bool = True,
) -> ExistingFunnelImporter:
    return ExistingFunnelImporter(
        source_funnels_path=paths["source"],
        video_funnels_dir=paths["video_dir"],
        output_channels_path=paths["channels"] if with_channels else None,
        ai_rules_path=paths["ai_rules"],
        config_manager_funnels_dir=paths["config_dir"],
        environment=environment,
    )


def _importer(root: Path, *, environment: str = "dev", with_channels: bool = True) -> ExistingFunnelImporter:
    paths = _write_fixture_tree(root)
    return _importer_from_paths(paths, environment=environment, with_channels=with_channels)


class TestSuccessfulImport:
    def test_imports_complete_funnel(self, tmp_path: Path) -> None:
        report = _importer(tmp_path).import_funnel(FUNNEL_ID)
        funnel = report.funnel
        assert funnel.identity.funnel_id == FUNNEL_ID
        assert funnel.identity.display_name == "MFM Business AI"
        assert funnel.processing.pipeline_profile == FUNNEL_ID
        assert funnel.processing.ai_rules.ai_rule_profile == "business"
        assert funnel.processing.selection.max_clips == 6
        assert funnel.processing.selection.min_clip_duration_sec == 15
        assert funnel.processing.output.filename_prefix == "mfm_business_ai"
        assert funnel.distribution.channel_routes[0].channel_id == "mfm_business_ai_primary"
        assert funnel.mappings.config_manager_funnel_id == "business"

    def test_dump_serialises(self, tmp_path: Path) -> None:
        report = _importer(tmp_path).import_funnel(FUNNEL_ID)
        dumped = dump_canonical_funnel(report.funnel)
        assert dumped["identity"]["funnel_id"] == FUNNEL_ID
        assert dumped["processing"]["ai_rules"]["ai_rule_profile"] == "business"

    def test_pipeline_profile_defaults_when_missing(self, tmp_path: Path) -> None:
        paths = _write_fixture_tree(tmp_path)
        source = json.loads(paths["source"].read_text(encoding="utf-8"))
        del source[0]["pipeline_profile"]
        paths["source"].write_text(json.dumps(source), encoding="utf-8")
        report = _importer_from_paths(paths).import_funnel(FUNNEL_ID)
        assert report.funnel.processing.pipeline_profile == FUNNEL_ID

    def test_environment_prod(self, tmp_path: Path) -> None:
        report = _importer(tmp_path, environment="prod").import_funnel(FUNNEL_ID)
        assert report.funnel.identity.environment == "prod"

    def test_runtime_channel_type_imports_as_canonical_singular(self, tmp_path: Path) -> None:
        report = _importer(tmp_path).import_funnel(FUNNEL_ID)
        assert report.funnel.acquisition.source_type == "youtube_channel"

    def test_runtime_playlist_type_imports_as_canonical_singular(self, tmp_path: Path) -> None:
        paths = _write_fixture_tree(tmp_path)
        source = json.loads(paths["source"].read_text(encoding="utf-8"))
        source[0]["source_type"] = "youtube_playlists"
        source[0]["sources"] = [
            {
                "source_id": "fixture_playlist",
                "label": "Fixture Playlist",
                "source_type": "youtube_playlist",
                "url": "https://www.youtube.com/playlist?list=PLfixture",
                "active": True,
                "max_videos_per_source": 10,
                "hydrate_missing_duration": True,
                "title_allowlist": [],
                "title_blocklist": [],
            }
        ]
        paths["source"].write_text(json.dumps(source), encoding="utf-8")
        report = _importer_from_paths(paths).import_funnel(FUNNEL_ID)
        assert report.funnel.acquisition.source_type == "youtube_playlist"
        assert report.funnel.acquisition.sources[0].source_type == "youtube_playlist"


class TestMissingInvalidInputs:
    def test_missing_source_entry_fails(self, tmp_path: Path) -> None:
        paths = _write_fixture_tree(tmp_path)
        paths["source"].write_text("[]", encoding="utf-8")
        with pytest.raises(FunnelImportError, match="not found"):
            _importer_from_paths(paths).import_funnel(FUNNEL_ID)

    def test_mismatched_video_funnel_id_fails(self, tmp_path: Path) -> None:
        paths = _write_fixture_tree(tmp_path)
        video = json.loads((paths["video_dir"] / f"{FUNNEL_ID}.json").read_text(encoding="utf-8"))
        video["funnel_id"] = "other_funnel"
        (paths["video_dir"] / f"{FUNNEL_ID}.json").write_text(json.dumps(video), encoding="utf-8")
        with pytest.raises(FunnelImportError, match="contains funnel_id"):
            _importer_from_paths(paths).import_funnel(FUNNEL_ID)

    def test_missing_video_funnel_fails(self, tmp_path: Path) -> None:
        paths = _write_fixture_tree(tmp_path)
        (paths["video_dir"] / f"{FUNNEL_ID}.json").unlink()
        with pytest.raises(FunnelImportError, match="Missing video-automation"):
            _importer_from_paths(paths).import_funnel(FUNNEL_ID)

    def test_missing_ai_alias_fails(self, tmp_path: Path) -> None:
        paths = _write_fixture_tree(tmp_path)
        paths["ai_rules"].write_text('FUNNEL_RULE_ALIASES = {"other": "business"}', encoding="utf-8")
        with pytest.raises(FunnelImportError, match="No AI rule profile alias"):
            _importer_from_paths(paths).import_funnel(FUNNEL_ID)

    def test_missing_ai_rules_path_fails(self, tmp_path: Path) -> None:
        paths = _write_fixture_tree(tmp_path)
        importer = ExistingFunnelImporter(
            source_funnels_path=paths["source"],
            video_funnels_dir=paths["video_dir"],
            ai_rules_path=None,
        )
        with pytest.raises(FunnelImportError, match="AI rules path not provided"):
            importer.import_funnel(FUNNEL_ID)

    def test_malformed_json_fails(self, tmp_path: Path) -> None:
        paths = _write_fixture_tree(tmp_path)
        paths["source"].write_text("{bad json", encoding="utf-8")
        with pytest.raises(FunnelImportError, match="Malformed JSON"):
            _importer_from_paths(paths).import_funnel(FUNNEL_ID)

    def test_missing_output_channels_warns_not_crashes(self, tmp_path: Path) -> None:
        report = _importer(tmp_path, with_channels=False).import_funnel(FUNNEL_ID)
        assert any("No output channel config path provided" in warning for warning in report.warnings)
        assert report.funnel.distribution.channel_routes == ()


class TestRegistryImport:
    def test_import_to_registry_writes_only_registry(self, tmp_path: Path) -> None:
        fixture_root = tmp_path / "fixtures"
        registry_dir = tmp_path / "registry"
        paths = _write_fixture_tree(fixture_root)
        before = {
            path: path.read_text(encoding="utf-8")
            for path in paths.values()
            if isinstance(path, Path) and path.is_file()
        }

        importer = _importer_from_paths(paths)
        registry = FunnelRegistry(registry_dir)
        report = importer.import_to_registry(FUNNEL_ID, registry)
        assert (registry_dir / f"{FUNNEL_ID}.json").is_file()
        assert report.funnel.identity.funnel_id == FUNNEL_ID

        after = {
            path: path.read_text(encoding="utf-8")
            for path in paths.values()
            if isinstance(path, Path) and path.is_file()
        }
        assert before == after

    def test_duplicate_import_without_overwrite_fails(self, tmp_path: Path) -> None:
        fixture_root = tmp_path / "fixtures"
        registry_dir = tmp_path / "registry"
        importer = _importer(fixture_root)
        registry = FunnelRegistry(registry_dir)
        importer.import_to_registry(FUNNEL_ID, registry)
        with pytest.raises(DuplicateFunnelError):
            importer.import_to_registry(FUNNEL_ID, registry, overwrite=False)

    def test_duplicate_import_with_overwrite_succeeds(self, tmp_path: Path) -> None:
        fixture_root = tmp_path / "fixtures"
        registry_dir = tmp_path / "registry"
        importer = _importer(fixture_root)
        registry = FunnelRegistry(registry_dir)
        importer.import_to_registry(FUNNEL_ID, registry)
        importer.import_to_registry(FUNNEL_ID, registry, overwrite=True)
        assert registry.get_funnel(FUNNEL_ID).identity.funnel_id == FUNNEL_ID


class TestScopeProtection:
    def test_import_output_has_no_forbidden_sections(self, tmp_path: Path) -> None:
        report = _importer(tmp_path).import_funnel(FUNNEL_ID)
        dumped = dump_canonical_funnel(report.funnel)
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
        assert "readiness" not in dumped.get("identity", {})
        assert "prompt_text" not in dumped.get("processing", {})
        route = dumped["distribution"]["channel_routes"][0]
        assert "credentials" not in route
        assert "token_file_env" not in json.dumps(route)

    def test_report_is_not_readiness(self, tmp_path: Path) -> None:
        report = _importer(tmp_path).import_funnel(FUNNEL_ID)
        payload = {
            "warnings": list(report.warnings),
            "notes": list(report.notes),
            "source_paths": report.source_paths,
        }
        assert "readiness_status" not in json.dumps(payload)
