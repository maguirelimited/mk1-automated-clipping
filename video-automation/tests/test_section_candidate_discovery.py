from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import section_candidate_discovery as discovery  # noqa: E402


class FakeModelResponse:
    def __init__(self, text: str | None = None, error: str | None = None):
        self.text = text
        self.error = error


class FakeModelClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> FakeModelResponse:
        self.prompts.append(prompt)
        if not self.responses:
            return FakeModelResponse(error="no response queued")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _section(section_id: str = "section_0001") -> dict:
    return {
        "section_id": section_id,
        "start_sec": 100.0,
        "end_sec": 260.0,
        "duration_sec": 160.0,
        "text": "[100.000 -> 160.000] A useful business lesson.",
        "source_transcript_path": "/tmp/transcript.json",
        "source_segment_refs": [
            {"segment_index": 1, "start_sec": 100.0, "end_sec": 160.0}
        ],
        "overlap": {
            "has_previous_overlap": False,
            "has_next_overlap": False,
            "overlap_before_sec": 0.0,
            "overlap_after_sec": 0.0,
        },
        "metadata": {},
    }


def _candidate(index: int = 1, *, section_id: str = "section_0001") -> dict:
    return {
        "candidate_local_id": f"{section_id}_candidate_{index:04d}",
        "source_section_id": section_id,
        "start_sec": 120.0 + index,
        "end_sec": 160.0 + index,
        "duration_sec": 40.0,
        "hook_text": "The surprising thing about this business is simple.",
        "core_idea_summary": "The speaker explains a standalone business lesson.",
        "why_candidate_has_potential": "It is understandable without broader podcast context.",
        "confidence": 0.72,
        "warnings": [],
    }


def _result(*, usable: bool = True, candidates: list[dict] | None = None) -> dict:
    return {
        "schema_version": discovery.SECTION_DISCOVERY_SCHEMA_VERSION,
        "section_id": "section_0001",
        "usable": usable,
        "confidence": 0.74 if usable else 0.31,
        "reason": (
            "This section contains a standalone business lesson."
            if usable
            else "No strong standalone clip found in this section."
        ),
        "warnings": [],
        "candidates": list(candidates if candidates is not None else [_candidate()]),
    }


def _response(payload: dict) -> FakeModelResponse:
    return FakeModelResponse(json.dumps(payload))


def _config(**overrides) -> discovery.CandidateDiscoveryConfig:
    base = {
        "fail_fast": False,
        "max_candidates_per_section": 3,
        "min_candidate_duration_sec": 15.0,
        "max_candidate_duration_sec": 120.0,
    }
    base.update(overrides)
    return discovery.CandidateDiscoveryConfig(**base)


def test_valid_usable_true_section_result_with_one_candidate_validates():
    discovery.validate_section_discovery_result(
        _result(),
        section=_section(),
        config=_config(),
    )


def test_valid_usable_false_result_with_zero_candidates_validates():
    discovery.validate_section_discovery_result(
        _result(usable=False, candidates=[]),
        section=_section(),
        config=_config(),
    )


def test_candidate_timestamps_outside_section_bounds_fail():
    candidate = _candidate()
    candidate["start_sec"] = 90.0
    candidate["end_sec"] = 130.0
    with pytest.raises(discovery.SectionCandidateDiscoveryError) as exc:
        discovery.validate_section_discovery_result(
            _result(candidates=[candidate]),
            section=_section(),
            config=_config(),
        )

    assert "start_sec must stay inside section bounds" in str(exc.value)


def test_invalid_candidate_duration_fails_validation():
    candidate = _candidate()
    candidate["duration_sec"] = 41.0
    with pytest.raises(discovery.SectionCandidateDiscoveryError) as exc:
        discovery.validate_section_discovery_result(
            _result(candidates=[candidate]),
            section=_section(),
            config=_config(),
        )

    assert "duration_sec must match" in str(exc.value)


def test_malformed_model_json_fails_cleanly():
    client = FakeModelClient([FakeModelResponse("not json")])

    with pytest.raises(discovery.SectionCandidateDiscoveryError) as exc:
        discovery.discover_candidates_for_section(
            _section(),
            ai_client=client,
            config=_config(),
            prompt_template="PROMPT",
        )

    assert exc.value.code == "MODEL_JSON_INVALID"


def test_batch_discovery_continues_after_failed_section_when_fail_fast_false():
    sections = [_section("section_0001"), _section("section_0002")]
    second = _result(usable=False, candidates=[])
    second["section_id"] = "section_0002"
    client = FakeModelClient([FakeModelResponse("not json"), _response(second)])

    batch = discovery.discover_candidates_for_sections(
        sections,
        ai_client=client,
        config=_config(fail_fast=False),
        prompt_template="PROMPT",
    )

    assert batch["sections_received"] == 2
    assert batch["sections_processed"] == 1
    assert len(batch["failed_sections"]) == 1
    assert batch["rejected_sections"] == 1


def test_batch_discovery_stops_after_failed_section_when_fail_fast_true():
    sections = [_section("section_0001"), _section("section_0002")]
    client = FakeModelClient([FakeModelResponse("not json"), _response(_result())])

    batch = discovery.discover_candidates_for_sections(
        sections,
        ai_client=client,
        config=_config(fail_fast=True),
        prompt_template="PROMPT",
    )

    assert batch["sections_processed"] == 0
    assert len(batch["failed_sections"]) == 1
    assert batch["warnings"] == ["fail_fast_stopped_after_section_failure"]
    assert len(client.prompts) == 1


def test_candidate_discovery_does_not_force_candidates_for_weak_sections():
    client = FakeModelClient([_response(_result(usable=False, candidates=[]))])

    result = discovery.discover_candidates_for_section(
        _section(),
        ai_client=client,
        config=_config(),
        prompt_template="PROMPT",
    )

    assert result["usable"] is False
    assert result["candidates"] == []


def test_max_candidates_per_section_is_respected():
    many = [_candidate(index) for index in range(1, 6)]
    client = FakeModelClient([_response(_result(candidates=many))])

    result = discovery.discover_candidates_for_section(
        _section(),
        ai_client=client,
        config=_config(max_candidates_per_section=2),
        prompt_template="PROMPT",
    )

    assert len(result["candidates"]) == 2
    assert result["warnings"] == ["max_candidates_per_section_applied"]


def test_discovered_candidates_preserve_source_section_id():
    client = FakeModelClient([_response(_result())])

    result = discovery.discover_candidates_for_section(
        _section(),
        ai_client=client,
        config=_config(),
        prompt_template="PROMPT",
    )

    assert result["candidates"][0]["source_section_id"] == "section_0001"


def test_aggregate_counts_are_correct():
    first = _result(candidates=[_candidate(1), _candidate(2)])
    second = _result(usable=False, candidates=[])
    second["section_id"] = "section_0002"
    client = FakeModelClient([_response(first), _response(second)])

    batch = discovery.discover_candidates_for_sections(
        [_section("section_0001"), _section("section_0002")],
        ai_client=client,
        config=_config(),
        prompt_template="PROMPT",
    )

    assert batch["sections_received"] == 2
    assert batch["sections_processed"] == 2
    assert batch["usable_sections"] == 1
    assert batch["rejected_sections"] == 1
    assert batch["candidates_discovered"] == 2
    assert batch["failed_sections"] == []


def test_artifact_write_read_works(tmp_path: Path):
    client = FakeModelClient([_response(_result())])
    batch = discovery.discover_candidates_for_sections(
        [_section()],
        ai_client=client,
        config=_config(),
        prompt_template="PROMPT",
    )
    artifact = discovery.build_section_candidate_discovery_artifact(
        job_id="job_123",
        source_transcript_sections_path="/tmp/transcript_sections.json",
        batch_result=batch,
        config=_config(),
        created_at="2026-06-30T12:00:00+00:00",
    )

    path = discovery.write_section_candidate_discovery(str(tmp_path), artifact)
    reloaded = discovery.read_section_candidate_discovery(path)

    assert Path(path).name == discovery.SECTION_DISCOVERY_ARTIFACT_FILENAME
    assert reloaded["job_id"] == "job_123"
    assert reloaded["section_results"][0]["section_id"] == "section_0001"


def test_tests_use_fake_ai_client():
    client = FakeModelClient([_response(_result())])

    discovery.discover_candidates_for_section(
        _section(),
        ai_client=client,
        config=_config(),
        prompt_template="PROMPT",
    )

    assert client.prompts
    assert "REQUEST CONTEXT - JSON" in client.prompts[0]


def test_discovery_does_not_call_rendering_or_output_funnel(
    monkeypatch: pytest.MonkeyPatch,
):
    original_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name in {"clip_video", "output_funnel"} or name.startswith("output_funnel."):
            raise AssertionError("rendering/output-funnel code should not be imported")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)
    client = FakeModelClient([_response(_result())])

    result = discovery.discover_candidates_for_section(
        _section(),
        ai_client=client,
        config=_config(),
        prompt_template="PROMPT",
    )

    assert result["usable"] is True
