"""Regression: synced funnel aliases resolve via funnel_rule_registry.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from funnel_rule_registry import reload_funnel_rule_registry  # noqa: E402
from tasks.section_candidate_discovery import (  # noqa: E402
    resolve_funnel_id,
    resolve_prompt_metadata,
)


@pytest.fixture(autouse=True)
def _reset_funnel_rule_registry():
    yield
    reload_funnel_rule_registry()


def _payload(funnel_id: str) -> dict:
    return {
        "task_type": "section_candidate_discovery",
        "job_id": "job_gta_test",
        "funnel_id": funnel_id,
        "input": {
            "section": {
                "section_id": "section_0001",
                "start_sec": 0.0,
                "end_sec": 60.0,
                "text": "Example section.",
            },
            "config": {},
        },
        "prompt_version": "section_candidate_discovery_base_v1",
        "schema_version": "section_candidate_discovery_v1",
    }


def test_synced_funnel_alias_from_registry_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Adding an alias to the registry file should not require task code edits."""
    registry_path = tmp_path / "funnel_rule_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "aliases": {
                    "business": "business",
                    "demo_gaming_funnel_001": "business",
                },
                "profiles": {
                    "business": {"managed": "builtin", "rules_version": "business_v1"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_FUNNEL_RULE_REGISTRY", str(registry_path))
    reload_funnel_rule_registry(registry_path)

    assert resolve_funnel_id("demo_gaming_funnel_001") == "business"
    metadata = resolve_prompt_metadata(_payload("demo_gaming_funnel_001"))
    assert metadata["requested_funnel_id"] == "demo_gaming_funnel_001"
    assert metadata["resolved_funnel_id"] == "business"
    assert metadata["funnel_rules_version"] == "business_v1"


def test_gta_clips_002_uses_gta_streamer_clips_profile_from_registry_file():
    """Committed registry maps gta_clips_002 to gta_streamer_clips, not business."""
    reload_funnel_rule_registry()

    assert resolve_funnel_id("gta_clips_002") == "gta_streamer_clips"
    metadata = resolve_prompt_metadata(_payload("gta_clips_002"))
    assert metadata["requested_funnel_id"] == "gta_clips_002"
    assert metadata["resolved_funnel_id"] == "gta_streamer_clips"
    assert metadata["funnel_rules_version"] == "gta_streamer_clips_v1"
    assert metadata["resolved_funnel_id"] != "business"
    assert metadata["funnel_rules_version"] != "business_v1"
