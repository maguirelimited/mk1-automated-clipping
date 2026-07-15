"""Processing-phase configuration contract for the Ops UI.

These operator-facing settings control the MK1 processing pipeline that turns a
transcript into a raw candidate pool: transcript sectioning and section
candidate discovery. They are persisted into ``controls.json`` under a
``processing_config`` block which ``video-automation`` reads via
``processing_settings.py``.

Defaults here mirror the service-side defaults exactly:
- transcript sectioning: ``transcript_sectioning.TranscriptSectioningConfig``
- candidate discovery: ``section_candidate_discovery.CandidateDiscoveryConfig``
- pipeline mode: legacy single-pass selection by default, ``mk1`` opts the live
  server into the new processing -> post-processing pipeline.
"""

from __future__ import annotations

from typing import Any

from .settings_fields import (
    ConfigField,
    effective_config as _effective_config,
    fields_view as _fields_view,
    parse_form as _parse_form,
    source_for as _source_for,
)

PROCESSING_CONFIG_STORE_PREFIX = "processing_config."
PROCESSING_CONFIG_FILE_KEY = "processing_config"

PROCESSING_PIPELINE_MODES = ("legacy", "mk1")


PROCESSING_CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField(
        name="processing_pipeline_mode",
        label="Processing pipeline mode",
        kind="choice",
        default="legacy",
        env_var="PROCESSING_PIPELINE_MODE",
        choices=PROCESSING_PIPELINE_MODES,
        group="Pipeline",
        help=(
            "legacy = current single-pass clip selection + clipping (default). "
            "mk1 = run the new processing -> post-processing pipeline "
            "(transcript sectioning, candidate discovery, selection gate, "
            "universal conveyor). Requires the local ai-service for discovery."
        ),
    ),
    ConfigField(
        name="section_target_duration_sec",
        label="Section target duration (s)",
        kind="float",
        default=300.0,
        env_var="PROCESSING_SECTION_TARGET_DURATION_SEC",
        minimum=10.0,
        maximum=3600.0,
        group="Transcript sectioning",
        help="Preferred length of each bounded transcript section analysed independently.",
    ),
    ConfigField(
        name="section_max_duration_sec",
        label="Section max duration (s)",
        kind="float",
        default=420.0,
        env_var="PROCESSING_SECTION_MAX_DURATION_SEC",
        minimum=10.0,
        maximum=7200.0,
        group="Transcript sectioning",
        help="Hard upper bound on a single section's length (must be >= target).",
    ),
    ConfigField(
        name="section_overlap_sec",
        label="Section overlap (s)",
        kind="float",
        default=60.0,
        env_var="PROCESSING_SECTION_OVERLAP_SEC",
        minimum=0.0,
        maximum=600.0,
        group="Transcript sectioning",
        help=(
            "Overlap between neighbouring sections so candidates near a boundary "
            "are not lost. MK1 default is 60s (tunable, not benchmarked)."
        ),
    ),
    ConfigField(
        name="section_min_duration_sec",
        label="Section min duration (s)",
        kind="float",
        default=60.0,
        env_var="PROCESSING_SECTION_MIN_DURATION_SEC",
        minimum=0.0,
        maximum=3600.0,
        group="Transcript sectioning",
        help="Minimum length for a standalone section before merging with neighbours.",
    ),
    ConfigField(
        name="max_candidates_per_section",
        label="Max candidates per section",
        kind="int",
        default=5,
        env_var="PROCESSING_MAX_CANDIDATES_PER_SECTION",
        minimum=1.0,
        maximum=10.0,
        group="Candidate discovery",
        help=(
            "Upper bound on raw candidates the model may surface from one section. "
            "MK1 recall-oriented default is 5 (safety bound, not a selection limit)."
        ),
    ),
    ConfigField(
        name="min_candidate_duration_sec",
        label="Min candidate duration (s)",
        kind="float",
        default=15.0,
        env_var="PROCESSING_MIN_CANDIDATE_DURATION_SEC",
        minimum=1.0,
        maximum=600.0,
        group="Candidate discovery",
        help="Candidates shorter than this are dropped during discovery.",
    ),
    ConfigField(
        name="max_candidate_duration_sec",
        label="Max candidate duration (s)",
        kind="float",
        default=120.0,
        env_var="PROCESSING_MAX_CANDIDATE_DURATION_SEC",
        minimum=1.0,
        maximum=1800.0,
        group="Candidate discovery",
        help="Candidates longer than this are dropped during discovery (must be >= min).",
    ),
    ConfigField(
        name="discovery_fail_fast",
        label="Discovery fail-fast",
        kind="bool",
        default=False,
        env_var="PROCESSING_DISCOVERY_FAIL_FAST",
        group="Candidate discovery",
        help=(
            "When on, the first failed section aborts discovery. When off, "
            "failed sections are recorded and processing continues."
        ),
    ),
)

PROCESSING_CONFIG_FIELDS_BY_NAME = {f.name: f for f in PROCESSING_CONFIG_FIELDS}


def effective_config(saved: dict[str, str]) -> dict[str, Any]:
    return _effective_config(PROCESSING_CONFIG_FIELDS, saved)


def source_for(field_name: str, saved: dict[str, str]) -> str:
    return _source_for(PROCESSING_CONFIG_FIELDS_BY_NAME, field_name, saved)


def parse_form(form: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    return _parse_form(PROCESSING_CONFIG_FIELDS, form)


def fields_view(saved: dict[str, str]) -> list[dict[str, Any]]:
    return _fields_view(PROCESSING_CONFIG_FIELDS, PROCESSING_CONFIG_FIELDS_BY_NAME, saved)
