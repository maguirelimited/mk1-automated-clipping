"""Unit tests for funnel rule registry Ops UI helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ops_ui.funnel_management.funnel_rule_registry_ops import (
    FunnelRuleRegistryOpsError,
    load_registry_document,
    plan_alias_patch,
)


def _sample_registry(*, aliases: dict[str, str] | None = None) -> dict:
    return {
        "schema_version": 1,
        "profiles": {
            "business": {"rules_version": "business_v1", "managed": "builtin"},
        },
        "aliases": aliases if aliases is not None else {},
    }


class TestLoadRegistryDocument:
    def test_loads_valid_registry(self, tmp_path: Path) -> None:
        path = tmp_path / "funnel_rule_registry.json"
        path.write_text(json.dumps(_sample_registry()), encoding="utf-8")
        doc = load_registry_document(path)
        assert doc["profiles"]["business"]["rules_version"] == "business_v1"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FunnelRuleRegistryOpsError, match="missing"):
            load_registry_document(tmp_path / "missing.json")

    def test_permission_denied_raises_ops_error(self, tmp_path: Path) -> None:
        path = tmp_path / "funnel_rule_registry.json"
        path.write_text(json.dumps(_sample_registry()), encoding="utf-8")
        os.chmod(path, 0o000)
        try:
            with pytest.raises(FunnelRuleRegistryOpsError, match="not readable"):
                load_registry_document(path)
        finally:
            os.chmod(path, 0o644)


class TestPlanAliasPatch:
    def test_create_alias(self) -> None:
        action, after, changed, messages = plan_alias_patch(
            _sample_registry(),
            funnel_id="new_funnel_001",
            profile_id="business",
        )
        assert action == "create"
        assert changed is True
        assert after["aliases"]["new_funnel_001"] == "business"
        assert "Create alias" in messages[0]

    def test_unknown_profile_errors(self) -> None:
        action, _after, changed, messages = plan_alias_patch(
            _sample_registry(),
            funnel_id="new_funnel_001",
            profile_id="gaming",
        )
        assert action == "error"
        assert changed is False
        assert "not found" in messages[0]
