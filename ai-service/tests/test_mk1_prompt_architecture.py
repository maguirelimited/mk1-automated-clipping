"""MK1 staged selection prompt boundary tests."""

from __future__ import annotations

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from task_router import IMPLEMENTED_TASK_TYPES  # noqa: E402
from tasks.section_candidate_discovery import (  # noqa: E402
    BASE_PROMPT_VERSION,
    MK1_DISCOVERY_PROMPT_STRATEGY,
    build_section_candidate_discovery_prompt,
    resolve_prompt_metadata,
)

PROMPTS_DIR = SERVICE_DIR / "prompts"
DISCOVERY_PROMPT_PATH = PROMPTS_DIR / f"{BASE_PROMPT_VERSION}.txt"
LEGACY_CLIP_SELECTION_PATH = PROMPTS_DIR / "clip_selection_v2.txt"

# Imperatives Discovery must not assign to the model (case-insensitive substrings).
DISCOVERY_FORBIDDEN_IMPERATIVES = (
    "select the best clips",
    "choose the best clips",
    "choose clips to render",
    "approve clips for posting",
    "post these clips",
    "render these clips",
    "rank globally across all sections",
)


def _load_discovery_prompt() -> str:
    return DISCOVERY_PROMPT_PATH.read_text(encoding="utf-8")


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
            "config": {
                "max_candidates_per_section": 5,
                "min_candidate_duration_sec": 15.0,
                "max_candidate_duration_sec": 120.0,
            },
        },
        "prompt_version": BASE_PROMPT_VERSION,
        "schema_version": "section_candidate_discovery_v1",
    }


def test_no_mk1_evaluation_prompt_file_exists():
    evaluation_prompts = list(PROMPTS_DIR.glob("*evaluation*"))
    assert evaluation_prompts == []


def test_discovery_prompt_strategy_identity():
    prompt = _load_discovery_prompt().lower()
    assert MK1_DISCOVERY_PROMPT_STRATEGY in prompt
    assert "not evaluation" in prompt


def test_discovery_prompt_forbids_final_selection_imperatives():
    prompt = _load_discovery_prompt().lower()
    for phrase in DISCOVERY_FORBIDDEN_IMPERATIVES:
        assert phrase not in prompt, f"Discovery prompt must not instruct: {phrase!r}"


def test_discovery_prompt_defers_final_clips_to_downstream_stages():
    prompt = _load_discovery_prompt().lower()
    assert "selection_gate_v1" in prompt or "evaluation" in prompt
    assert "this stage does not" in prompt or "do not decide which clips should be rendered" in prompt


def test_built_discovery_prompt_includes_config_without_render_assumptions():
    built = build_section_candidate_discovery_prompt(
        prompt_text=_load_discovery_prompt(),
        section={"section_id": "section_0001", "text": "Example."},
        config={"max_candidates_per_section": 5},
        prompt_metadata=resolve_prompt_metadata(_payload()),
    ).lower()

    assert "max_candidates_per_section" in built
    assert "request context - json:" in built
    assert "render these clips" not in built
    assert "final clip count" not in built


def test_legacy_clip_selection_prompt_is_untouched():
    assert LEGACY_CLIP_SELECTION_PATH.is_file()
    legacy = LEGACY_CLIP_SELECTION_PATH.read_text(encoding="utf-8")
    assert "clip_selection" in legacy.lower() or "final clip-selection" in legacy.lower()
    assert "scouting pass" in legacy.lower() or "candidate discovery" in legacy.lower()


def test_mk1_ai_service_only_implements_discovery_task():
    assert IMPLEMENTED_TASK_TYPES == {"section_candidate_discovery"}
    assert "clip_selection" not in IMPLEMENTED_TASK_TYPES
