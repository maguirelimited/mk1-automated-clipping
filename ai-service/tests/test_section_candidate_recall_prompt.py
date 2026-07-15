from __future__ import annotations

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from tasks.section_candidate_discovery import (  # noqa: E402
    BASE_PROMPT_VERSION,
    build_section_candidate_discovery_prompt,
    resolve_prompt_metadata,
)

PROMPT_PATH = SERVICE_DIR / "prompts" / f"{BASE_PROMPT_VERSION}.txt"


def _load_base_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _payload() -> dict:
    return {
        "task_type": "section_candidate_discovery",
        "job_id": "job_123",
        "input": {
            "section": {
                "section_id": "section_0001",
                "start_sec": 0.0,
                "end_sec": 300.0,
                "text": "Example section text.",
            },
            "config": {"max_candidates_per_section": 5},
        },
        "prompt_version": BASE_PROMPT_VERSION,
        "schema_version": "section_candidate_discovery_v1",
    }


def test_discovery_prompt_describes_candidate_discovery_not_final_selection():
    prompt = _load_base_prompt()

    assert "candidate discovery" in prompt.lower()
    assert "not final clip selection" in prompt.lower() or "not final clip selection or evaluation" in prompt.lower()
    assert "genuine short-form potential" in prompt.lower()


def test_discovery_prompt_forbids_final_render_decisions():
    prompt = _load_base_prompt().lower()

    assert "do not decide which clips should be rendered" in prompt
    assert "do not decide which clips should be published" in prompt
    assert "do not rank candidates globally" in prompt


def test_discovery_prompt_is_transcript_evidence_only():
    prompt = _load_base_prompt().lower()

    assert "transcript-only evidence" in prompt
    assert "visual quality" in prompt
    assert "facial expressions" in prompt
    assert "do not claim knowledge" in prompt


def test_discovery_prompt_is_recall_oriented():
    prompt = _load_base_prompt().lower()

    assert "recall-oriented" in prompt or "recall-oriented discovery" in prompt
    assert "false positives can be filtered later" in prompt
    assert "false negatives are lost permanently" in prompt


def test_discovery_prompt_treats_cap_as_safety_maximum_not_target():
    prompt = _load_base_prompt().lower()

    assert "config.max_candidates_per_section" in prompt
    assert "safety maximum" in prompt
    assert "not a target quota" in prompt


def test_discovery_prompt_is_section_local():
    prompt = _load_base_prompt().lower()

    assert "this section only" in prompt or "inside this section only" in prompt
    assert "do not compare" in prompt and "other sections" in prompt


def test_discovery_prompt_names_mk1_strategy():
    prompt = _load_base_prompt().lower()
    assert "mk1_recall_oriented_discovery_v1" in prompt


def test_built_prompt_includes_base_recall_instructions():
    prompt = build_section_candidate_discovery_prompt(
        prompt_text=_load_base_prompt(),
        section={"section_id": "section_0001", "text": "Example."},
        config={"max_candidates_per_section": 5},
        prompt_metadata=resolve_prompt_metadata(_payload()),
    )

    assert "recall-oriented candidate discovery" in prompt.lower()
    assert "REQUEST CONTEXT - JSON:" in prompt
