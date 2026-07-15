from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from task_router import AITaskError  # noqa: E402
from funnel_rule_registry import (  # noqa: E402
    get_funnel_rule_aliases,
    reload_funnel_rule_registry,
    resolve_rules_version,
)
from tasks.section_candidate_discovery import (  # noqa: E402
    BASE_PROMPT_VERSION,
    build_section_candidate_discovery_prompt,
    resolve_funnel_id,
    resolve_prompt_metadata,
)

PROMPTS_DIR = SERVICE_DIR / "prompts"
GTA_RULES_PATH = PROMPTS_DIR / "funnel_rules" / "gta_streamer_clips_v1.txt"


@pytest.fixture(autouse=True)
def _reload_funnel_rule_registry():
    reload_funnel_rule_registry()
    yield
    reload_funnel_rule_registry()


def _payload(funnel_id=None) -> dict:
    payload = {
        "task_type": "section_candidate_discovery",
        "job_id": "job_123",
        "input": {
            "section": {
                "section_id": "section_0001",
                "start_sec": 0.0,
                "end_sec": 60.0,
                "text": "Weak section.",
            },
            "config": {},
        },
        "prompt_version": BASE_PROMPT_VERSION,
        "schema_version": "section_candidate_discovery_v1",
    }
    if funnel_id is not None:
        payload["funnel_id"] = funnel_id
    return payload


def test_missing_funnel_id_defaults_to_business_rules():
    metadata = resolve_prompt_metadata(_payload())

    assert metadata["resolved_funnel_id"] == "business"
    assert metadata["funnel_rules_version"] == "business_v1"


@pytest.mark.parametrize(
    ("funnel_id", "resolved", "rules_version"),
    [
        ("business", "business", "business_v1"),
        ("finance", "finance", "finance_v1"),
        ("sport", "sport", "sport_v1"),
        ("comedy", "comedy", "comedy_v1"),
        ("mfm_business_ai_001", "business", "business_v1"),
        ("gta_clips_002", "gta_streamer_clips", "gta_streamer_clips_v1"),
    ],
)
def test_explicit_funnel_id_resolves_expected_rules(funnel_id, resolved, rules_version):
    assert resolve_funnel_id(funnel_id) == resolved
    assert resolve_rules_version(resolved) == rules_version


def test_unknown_funnel_id_fails_clearly():
    with pytest.raises(AITaskError) as exc:
        resolve_funnel_id("unknown_funnel")

    assert exc.value.code == "UNKNOWN_FUNNEL_ID"
    assert exc.value.status_code == 400


def test_prompt_metadata_includes_base_prompt_and_funnel_rule_versions():
    metadata = resolve_prompt_metadata(_payload("finance"))

    assert metadata["base_prompt_version"] == BASE_PROMPT_VERSION
    assert metadata["requested_funnel_id"] == "finance"
    assert metadata["resolved_funnel_id"] == "finance"
    assert metadata["funnel_rules_version"] == "finance_v1"


def test_prompt_contains_only_selected_business_rule_block():
    prompt = build_section_candidate_discovery_prompt(
        prompt_text="BASE INSTRUCTIONS",
        section={"section_id": "section_0001"},
        config={},
        prompt_metadata=resolve_prompt_metadata(_payload("business")),
    )

    assert "FUNNEL: business" in prompt
    assert "FUNNEL: finance" not in prompt
    assert "FUNNEL: sport" not in prompt
    assert "FUNNEL: comedy" not in prompt


def test_prompt_contains_only_selected_finance_rule_block():
    prompt = build_section_candidate_discovery_prompt(
        prompt_text="BASE INSTRUCTIONS",
        section={"section_id": "section_0001"},
        config={},
        prompt_metadata=resolve_prompt_metadata(_payload("finance")),
    )

    assert "FUNNEL: finance" in prompt
    assert "FUNNEL: business" not in prompt
    assert "FUNNEL: sport" not in prompt
    assert "FUNNEL: comedy" not in prompt


def test_prompt_contains_only_selected_sport_rule_block():
    prompt = build_section_candidate_discovery_prompt(
        prompt_text="BASE INSTRUCTIONS",
        section={"section_id": "section_0001"},
        config={},
        prompt_metadata=resolve_prompt_metadata(_payload("sport")),
    )

    assert "FUNNEL: sport" in prompt
    assert "FUNNEL: business" not in prompt
    assert "FUNNEL: finance" not in prompt
    assert "FUNNEL: comedy" not in prompt


def test_prompt_contains_only_selected_comedy_rule_block():
    prompt = build_section_candidate_discovery_prompt(
        prompt_text="BASE INSTRUCTIONS",
        section={"section_id": "section_0001"},
        config={},
        prompt_metadata=resolve_prompt_metadata(_payload("comedy")),
    )

    assert "FUNNEL: comedy" in prompt
    assert "FUNNEL: business" not in prompt
    assert "FUNNEL: finance" not in prompt
    assert "FUNNEL: sport" not in prompt


def test_supported_aliases_are_documented():
    aliases = get_funnel_rule_aliases()
    assert aliases["mfm_business_ai_001"] == "business"
    assert aliases["sports"] == "sport"
    assert aliases["gta_clips_002"] == "gta_streamer_clips"
    assert aliases["gta_clips_002"] != "business"


def test_gta_clips_002_resolves_to_gta_streamer_clips_rules():
    metadata = resolve_prompt_metadata(_payload("gta_clips_002"))

    assert metadata["requested_funnel_id"] == "gta_clips_002"
    assert metadata["resolved_funnel_id"] == "gta_streamer_clips"
    assert metadata["funnel_rules_version"] == "gta_streamer_clips_v1"
    assert metadata["base_prompt_version"] == BASE_PROMPT_VERSION


def test_gta_streamer_clips_rules_are_injected_into_discovery_prompt():
    assert GTA_RULES_PATH.is_file()
    rules_text = GTA_RULES_PATH.read_text(encoding="utf-8").strip()
    metadata = resolve_prompt_metadata(_payload("gta_clips_002"))
    prompt = build_section_candidate_discovery_prompt(
        prompt_text="BASE INSTRUCTIONS",
        section={"section_id": "section_0001", "text": "GTA section."},
        config={"max_candidates_per_section": 5},
        prompt_metadata=metadata,
    )

    assert "RESOLVED FUNNEL JUDGEMENT RULES:" in prompt
    assert rules_text in prompt
    assert "FUNNEL: GTA streamer clips" in prompt
    assert "FUNNEL RULES VERSION: gta_streamer_clips_v1" in prompt
    assert "FUNNEL: business" not in prompt
    assert "FUNNEL: finance" not in prompt
    assert "FUNNEL: sport" not in prompt
    assert "FUNNEL: comedy" not in prompt
    assert '"requested_funnel_id": "gta_clips_002"' in prompt
    assert '"resolved_funnel_id": "gta_streamer_clips"' in prompt
    assert '"funnel_rules_version": "gta_streamer_clips_v1"' in prompt
