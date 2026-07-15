from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

VIDEO_SCRIPTS = SERVICE_DIR.parent / "video-automation" / "scripts"
if str(VIDEO_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(VIDEO_SCRIPTS))

import section_candidate_discovery as va_discovery  # noqa: E402

from section_discovery_semantics import (  # noqa: E402
    ALL_REJECTED_REASON,
    is_placeholder_candidate_id,
    is_placeholder_text,
    normalize_section_discovery_result,
    stable_candidate_local_id,
)
from versioned_assets import load_schema  # noqa: E402


SCHEMA = load_schema("section_candidate_discovery_v1")


def _section(**overrides) -> dict:
    base = {
        "section_id": "section_0002",
        "start_sec": 300.0,
        "end_sec": 600.0,
        "duration_sec": 300.0,
        "text": "Business discussion.",
    }
    base.update(overrides)
    return base


def _scores() -> dict:
    return {
        "hook_strength": 7,
        "standalone_context": 7,
        "insight_value": 8,
        "retention_potential": 7,
        "natural_ending": 6,
        "overall_potential": 7,
    }


def _candidate(**overrides) -> dict:
    base = {
        "candidate_local_id": "123456789",
        "source_section_id": "example_section_id",
        "start_sec": 388.81,
        "end_sec": 390.891,
        "duration_sec": 2.081,
        "hook_text": "Most of them are franchised.",
        "core_idea_summary": "The speaker discusses franchising in the industry.",
        "why_candidate_has_potential": "Standalone insight about how the market operates.",
        "archetype": "business_lesson",
        "scores": _scores(),
        "confidence": 0.85,
        "warnings": [],
        "transcript_quality_flags": [],
    }
    base.update(overrides)
    return base


def _parsed(**overrides) -> dict:
    base = {
        "schema_version": "section_candidate_discovery_v1",
        "section_id": "example_section_id",
        "usable": True,
        "confidence": 0.9,
        "reason": "Valid section with high confidence.",
        "warnings": [],
        "transcript_quality_flags": [],
        "candidates": [_candidate()],
    }
    base.update(overrides)
    return base


def _normalize(parsed: dict, **kwargs) -> dict:
    return normalize_section_discovery_result(
        parsed=parsed,
        section=_section(**kwargs.pop("section", {})),
        config=kwargs.pop("config", {"min_candidate_duration_sec": 15.0, "max_candidate_duration_sec": 120.0}),
        job_id=kwargs.pop("job_id", "job_20260630T171907Z_df11d570"),
        schema=SCHEMA,
        **kwargs,
    )


def test_wrong_section_id_is_normalised():
    result = _normalize(_parsed())

    assert result["section_id"] == "section_0002"
    Draft202012Validator(SCHEMA).validate(result)


def test_missing_source_section_id_is_normalised_on_valid_candidate():
    result = _normalize(
        _parsed(
            candidates=[
                _candidate(
                    start_sec=320.0,
                    end_sec=360.0,
                    duration_sec=40.0,
                    candidate_local_id="section_0002_candidate_0001",
                )
            ]
        )
    )

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["source_section_id"] == "section_0002"
    assert result["usable"] is True


def test_duration_sec_is_recalculated():
    result = _normalize(
        _parsed(
            candidates=[
                _candidate(
                    start_sec=320.0,
                    end_sec=360.0,
                    duration_sec=999.0,
                    candidate_local_id="section_0002_candidate_0001",
                )
            ]
        )
    )

    assert result["candidates"][0]["duration_sec"] == pytest.approx(40.0)


def test_two_second_candidate_rejected_when_min_duration_is_15():
    result = _normalize(_parsed())

    assert result["usable"] is False
    assert result["candidates"] == []
    assert result["reason"] == ALL_REJECTED_REASON
    assert any("duration_below_min" in w for w in result["warnings"])


def test_placeholder_candidate_id_is_replaced_for_otherwise_valid_candidate():
    result = _normalize(
        _parsed(
            candidates=[
                _candidate(
                    candidate_local_id="123456789",
                    start_sec=320.0,
                    end_sec=360.0,
                    duration_sec=40.0,
                )
            ]
        )
    )

    assert len(result["candidates"]) == 1
    local_id = result["candidates"][0]["candidate_local_id"]
    assert local_id != "123456789"
    assert local_id.startswith("section_0002_c_")
    assert (
        stable_candidate_local_id("job_20260630T171907Z_df11d570", "section_0002", 320.0, 360.0)
        == local_id
    )


def test_all_rejected_candidates_produce_usable_false_with_empty_candidates():
    result = _normalize(
        _parsed(
            candidates=[
                _candidate(start_sec=320.0, end_sec=325.0, duration_sec=5.0),
                _candidate(start_sec=330.0, end_sec=335.0, duration_sec=5.0),
            ]
        )
    )

    assert result["usable"] is False
    assert result["candidates"] == []
    assert result["reason"] == ALL_REJECTED_REASON
    Draft202012Validator(SCHEMA).validate(result)


def test_valid_candidate_survives_normalisation_and_passes_video_automation_validation():
    parsed = _parsed(
        section_id="example_section_id",
        candidates=[
            _candidate(
                candidate_local_id="example_section_id_candidate_0001",
                start_sec=320.0,
                end_sec=360.0,
                duration_sec=40.0,
            )
        ],
    )
    result = _normalize(parsed)

    assert result["usable"] is True
    assert len(result["candidates"]) == 1
    Draft202012Validator(SCHEMA).validate(result)

    va_discovery.validate_section_discovery_result(
        result,
        section=_section(),
        config=va_discovery.CandidateDiscoveryConfig(
            min_candidate_duration_sec=15.0,
            max_candidate_duration_sec=120.0,
        ),
    )


def test_placeholder_text_fields_reject_candidate():
    result = _normalize(
        _parsed(
            candidates=[
                _candidate(
                    start_sec=320.0,
                    end_sec=360.0,
                    duration_sec=40.0,
                    hook_text="the opening spoken idea",
                    candidate_local_id="section_0002_candidate_0001",
                )
            ]
        )
    )

    assert result["usable"] is False
    assert result["candidates"] == []
    assert any("placeholder_hook_text" in w for w in result["warnings"])


def test_out_of_bounds_candidate_is_rejected():
    result = _normalize(
        _parsed(
            candidates=[
                _candidate(
                    start_sec=250.0,
                    end_sec=290.0,
                    duration_sec=40.0,
                    candidate_local_id="section_0002_candidate_0001",
                )
            ]
        )
    )

    assert result["usable"] is False
    assert any("start_outside_section" in w for w in result["warnings"])


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("123456789", True),
        ("example_section_id_candidate_0001", True),
        ("section_0002_candidate_0001", False),
        ("section_0002_c_320000_360000_ab12cd34ef", False),
        ("", True),
    ],
)
def test_placeholder_candidate_id_detection(value, expected):
    assert is_placeholder_candidate_id(value, section_id="section_0002") is expected


def test_placeholder_text_detection():
    assert is_placeholder_text("the opening spoken idea") is True
    assert is_placeholder_text("Real transcript-backed hook.") is False


def test_run_section_candidate_discovery_applies_normalisation(monkeypatch):
    from tasks.section_candidate_discovery import run_section_candidate_discovery
    from versioned_assets import load_prompt

    model_json = json.dumps(
        _parsed(
            candidates=[
                _candidate(
                    candidate_local_id="123456789",
                    start_sec=320.0,
                    end_sec=360.0,
                    duration_sec=40.0,
                )
            ]
        )
    )

    class FakeResponse:
        text = model_json
        error = None

    class FakeClient:
        def generate(self, prompt: str):
            return FakeResponse()

    payload = {
        "task_type": "section_candidate_discovery",
        "job_id": "job_semantic_test",
        "input": {"section": _section(), "config": {"min_candidate_duration_sec": 15.0}},
        "prompt_version": "section_candidate_discovery_base_v1",
        "schema_version": "section_candidate_discovery_v1",
    }

    result = run_section_candidate_discovery(
        payload=payload,
        settings=object(),
        prompt_text=load_prompt("section_candidate_discovery_base_v1"),
        schema=SCHEMA,
        model_client=FakeClient(),
    )

    assert result["section_id"] == "section_0002"
    assert result["usable"] is True
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["source_section_id"] == "section_0002"
