from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from task_router import AITaskError  # noqa: E402
from tasks.section_candidate_discovery import (  # noqa: E402
    BASE_PROMPT_VERSION,
    FUNNEL_RULE_ALIASES,
    FUNNEL_RULE_VERSIONS,
    build_section_candidate_discovery_prompt,
    resolve_funnel_id,
    resolve_prompt_metadata,
)


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
    ("funnel_id", "resolved"),
    [
        ("business", "business"),
        ("finance", "finance"),
        ("sport", "sport"),
        ("comedy", "comedy"),
        ("mfm_business_ai_001", "business"),
    ],
)
def test_explicit_funnel_id_resolves_expected_rules(funnel_id, resolved):
    assert resolve_funnel_id(funnel_id) == resolved
    assert FUNNEL_RULE_VERSIONS[resolved].endswith("_v1")


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
    assert FUNNEL_RULE_ALIASES["mfm_business_ai_001"] == "business"
    assert FUNNEL_RULE_ALIASES["sports"] == "sport"
