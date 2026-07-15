"""Unit tests for the canonical funnel registry (Funnel Management MK1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops_ui.funnel_management.registry import (
    DuplicateFunnelError,
    FunnelNotFoundError,
    FunnelRegistry,
    FunnelRegistryError,
    FunnelRegistryPathError,
)
from ops_ui.funnel_management.schema import load_canonical_funnel
from tests.test_canonical_funnel_schema import _valid_funnel


def _sample_funnel():
    return load_canonical_funnel(_valid_funnel())


class TestBasicRegistryOperations:
    def test_save_creates_funnel_json(self, tmp_path: Path) -> None:
        registry = FunnelRegistry(tmp_path)
        path = registry.save_funnel(_sample_funnel())
        assert path == tmp_path / "mfm_business_ai_001.json"
        assert path.is_file()

    def test_get_funnel_returns_saved_funnel(self, tmp_path: Path) -> None:
        registry = FunnelRegistry(tmp_path)
        original = _sample_funnel()
        registry.save_funnel(original)
        loaded = registry.get_funnel("mfm_business_ai_001")
        assert loaded.identity.display_name == original.identity.display_name
        assert loaded.processing.selection.max_clips == original.processing.selection.max_clips

    def test_list_funnels_sorted_by_id(self, tmp_path: Path) -> None:
        registry = FunnelRegistry(tmp_path)
        first = _sample_funnel()
        registry.save_funnel(first)

        second_data = _valid_funnel()
        second_data["identity"]["funnel_id"] = "aaa_demo_funnel"
        second_data["identity"]["display_name"] = "AAA Demo"
        second_data["processing"]["pipeline_profile"] = "aaa_demo_funnel"
        registry.save_funnel(load_canonical_funnel(second_data))

        ids = [funnel.identity.funnel_id for funnel in registry.list_funnels()]
        assert ids == ["aaa_demo_funnel", "mfm_business_ai_001"]

    def test_exists_true_and_false(self, tmp_path: Path) -> None:
        registry = FunnelRegistry(tmp_path)
        assert registry.exists("mfm_business_ai_001") is False
        registry.save_funnel(_sample_funnel())
        assert registry.exists("mfm_business_ai_001") is True
        assert registry.exists("missing_funnel") is False


class TestDuplicateProtection:
    def test_save_without_overwrite_fails(self, tmp_path: Path) -> None:
        registry = FunnelRegistry(tmp_path)
        registry.save_funnel(_sample_funnel())
        with pytest.raises(DuplicateFunnelError, match="already exists"):
            registry.save_funnel(_sample_funnel(), overwrite=False)

    def test_save_with_overwrite_succeeds(self, tmp_path: Path) -> None:
        registry = FunnelRegistry(tmp_path)
        funnel = _sample_funnel()
        registry.save_funnel(funnel)
        updated_data = _valid_funnel()
        updated_data["identity"]["display_name"] = "Updated Name"
        updated = load_canonical_funnel(updated_data)
        path = registry.save_funnel(updated, overwrite=True)
        reloaded = registry.get_funnel("mfm_business_ai_001")
        assert reloaded.identity.display_name == "Updated Name"
        assert path.exists()


class TestSchemaIntegration:
    def test_invalid_schema_file_fails_on_load(self, tmp_path: Path) -> None:
        path = tmp_path / "mfm_business_ai_001.json"
        path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        registry = FunnelRegistry(tmp_path)
        with pytest.raises(FunnelRegistryError, match="Invalid canonical funnel"):
            registry.load_file(path)

    def test_unknown_fields_fail_through_registry(self, tmp_path: Path) -> None:
        payload = _valid_funnel()
        payload["readiness"] = {"readiness_status": "ready"}
        path = tmp_path / "mfm_business_ai_001.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        registry = FunnelRegistry(tmp_path)
        with pytest.raises(FunnelRegistryError, match="Unknown field"):
            registry.load_file(path)

    def test_missing_required_fields_fail(self, tmp_path: Path) -> None:
        payload = _valid_funnel()
        del payload["processing"]["ai_rules"]["ai_rule_profile"]
        path = tmp_path / "mfm_business_ai_001.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        registry = FunnelRegistry(tmp_path)
        with pytest.raises(FunnelRegistryError, match="ai_rule_profile"):
            registry.load_file(path)


class TestPathSafety:
    @pytest.mark.parametrize(
        "unsafe_id",
        [
            "../evil",
            "evil/path",
            "/absolute/path",
            "mfm-business-ai",
            "MFM_BUSINESS_AI",
            "evil.json/other",
        ],
    )
    def test_unsafe_ids_fail(self, tmp_path: Path, unsafe_id: str) -> None:
        registry = FunnelRegistry(tmp_path)
        with pytest.raises(FunnelRegistryPathError):
            registry.get_funnel(unsafe_id)
        with pytest.raises(FunnelRegistryPathError):
            registry.exists(unsafe_id)

    def test_mismatched_filename_and_identity_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "wrong_id.json"
        path.write_text(json.dumps(_valid_funnel()), encoding="utf-8")
        registry = FunnelRegistry(tmp_path)
        with pytest.raises(FunnelRegistryPathError, match="does not match"):
            registry.load_file(path)

    def test_get_missing_funnel_raises_not_found(self, tmp_path: Path) -> None:
        registry = FunnelRegistry(tmp_path)
        with pytest.raises(FunnelNotFoundError, match="not found"):
            registry.get_funnel("mfm_business_ai_001")


class TestStorageHygiene:
    def test_non_json_files_are_ignored(self, tmp_path: Path) -> None:
        registry = FunnelRegistry(tmp_path)
        registry.save_funnel(_sample_funnel())
        (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
        funnels = registry.list_funnels()
        assert len(funnels) == 1
        assert funnels[0].identity.funnel_id == "mfm_business_ai_001"

    def test_json_output_is_stable(self, tmp_path: Path) -> None:
        registry = FunnelRegistry(tmp_path)
        registry.save_funnel(_sample_funnel())
        text = (tmp_path / "mfm_business_ai_001.json").read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert '"schema_version": 1' in text
        assert '"funnel_id": "mfm_business_ai_001"' in text

    def test_registry_creates_directory_on_save(self, tmp_path: Path) -> None:
        registry_dir = tmp_path / "nested" / "registry"
        registry = FunnelRegistry(registry_dir)
        assert not registry_dir.exists()
        registry.save_funnel(_sample_funnel())
        assert registry_dir.is_dir()


class TestForbiddenFields:
    @pytest.mark.parametrize(
        "field_name,payload",
        [
            ("readiness", {"readiness_status": "ready"}),
            ("operations", {"can_edit": True}),
            ("pause_state", {"funnel_paused": True}),
            ("queue_depth", {"value": 3}),
            ("analytics", {"views": 100}),
        ],
    )
    def test_forbidden_top_level_fields_fail(
        self, tmp_path: Path, field_name: str, payload: dict
    ) -> None:
        body = _valid_funnel()
        body[field_name] = payload
        path = tmp_path / "mfm_business_ai_001.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        registry = FunnelRegistry(tmp_path)
        with pytest.raises(FunnelRegistryError):
            registry.load_file(path)
