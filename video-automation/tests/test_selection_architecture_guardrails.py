"""Lightweight guardrails for the MK1-first selection upgrade direction.

These tests document and protect agreed architecture boundaries. They do not
assert runtime selection behaviour beyond stable mode constants.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARDRAILS_PATH = (
    REPO_ROOT / "system-context" / "selection-upgrade" / "architecture-guardrails.md"
)

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import processing_settings  # noqa: E402


def _guardrails_text() -> str:
    assert GUARDRAILS_PATH.is_file(), (
        f"Canonical architecture guardrails note missing: {GUARDRAILS_PATH}"
    )
    return GUARDRAILS_PATH.read_text(encoding="utf-8")


def test_architecture_guardrails_note_exists():
    text = _guardrails_text()
    assert "MK1 Selection Architecture Guardrails" in text


def test_guardrails_declare_mk1_first_route():
    text = _guardrails_text()
    assert "MK1-first formalisation" in text
    assert "Do not unify legacy and MK1" in text or "Do not unify legacy and MK1 in the same" in text


def test_guardrails_document_stage_responsibilities():
    text = _guardrails_text()
    for heading in (
        "Transcript Presentation",
        "Discovery",
        "Candidate Object",
        "Candidate Processing",
        "Evaluation",
        "Rendering",
    ):
        assert heading in text


def test_guardrails_document_no_touch_areas():
    text = _guardrails_text()
    for marker in (
        "select_clip.py",
        "clip_video.py",
        "postprocess_segments",
        "clip_selection.py",
        "transcript_chunking.py",
        "Default pipeline mode",
    ):
        assert marker in text


def test_guardrails_forbid_arbitrary_weighted_scoring():
    text = _guardrails_text()
    assert "Do not reintroduce arbitrary weighted rubric scoring" in text


def test_pipeline_modes_remain_explicit_and_separate():
    assert set(processing_settings.PROCESSING_PIPELINE_MODES) == {"legacy", "mk1"}


def test_guardrails_document_evaluation_strategy():
    text = _guardrails_text()
    assert "mk1_selection_gate_evaluation_v1" in text
    assert "post_processing_mk1.py" in text


def test_guardrails_document_mk1_ai_prompt_boundaries():
    text = _guardrails_text()
    assert "MK1 AI prompt boundaries" in text
    assert "no MK1 AI Evaluation prompt" in text.lower() or "no mk1 ai evaluation prompt" in text.lower()
    assert "section_candidate_discovery_base_v1" in text


def test_default_pipeline_mode_remains_legacy(monkeypatch, tmp_path):
    monkeypatch.delenv("PROCESSING_PIPELINE_MODE", raising=False)
    monkeypatch.setenv("MK04_CONTROLS_FILE", str(tmp_path / "missing.json"))
    mod = importlib.reload(processing_settings)
    assert mod.DEFAULT_PIPELINE_MODE == "legacy"
    assert mod.resolve_pipeline_mode() == "legacy"
