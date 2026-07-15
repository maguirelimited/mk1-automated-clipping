"""MK1 processing config default alignment (Prompt 13).

Lightweight tests that import only ``processing_config`` — no Flask app import.
"""

from __future__ import annotations

from ops_ui.processing_config import (
    PROCESSING_CONFIG_FIELDS_BY_NAME,
    effective_config,
)


def test_processing_config_field_defaults_match_mk1_code():
    assert PROCESSING_CONFIG_FIELDS_BY_NAME["section_overlap_sec"].default == 60.0
    assert PROCESSING_CONFIG_FIELDS_BY_NAME["max_candidates_per_section"].default == 5
    assert PROCESSING_CONFIG_FIELDS_BY_NAME["processing_pipeline_mode"].default == "legacy"


def test_processing_mk1_defaults_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("PROCESSING_SECTION_OVERLAP_SEC", raising=False)
    monkeypatch.delenv("PROCESSING_MAX_CANDIDATES_PER_SECTION", raising=False)
    effective = effective_config({})
    assert effective["section_overlap_sec"] == 60.0
    assert effective["max_candidates_per_section"] == 5
    assert effective["processing_pipeline_mode"] == "legacy"


def test_processing_explicit_saved_values_override_defaults() -> None:
    saved = {"section_overlap_sec": "30", "max_candidates_per_section": "3"}
    effective = effective_config(saved)
    assert effective["section_overlap_sec"] == 30.0
    assert effective["max_candidates_per_section"] == 3
