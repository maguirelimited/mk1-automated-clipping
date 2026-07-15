"""Versioned processing handoff contracts for post-processing.

This module intentionally does not discover candidates or change the current
render flow. It defines, validates, and writes the processing artifacts that
later post-processing stages consume.

Canonical MK1 candidate schema
------------------------------
The canonical MK1 candidate object is the ``candidates[]`` entry inside
``raw_candidate_pool.json`` (schema ``raw_candidate_pool_v1``). There is one
MK1 candidate shape from pool assembly through Evaluation; see
``video-automation/context/mk1_candidate_schema.md``.

Use :func:`validate_mk1_candidate` to validate a single candidate dict.
"""

from __future__ import annotations

import hashlib
import math
import os
from datetime import datetime, timezone
from numbers import Number
from typing import Any

from mk04_utils import write_json

RAW_CANDIDATE_POOL_SCHEMA_VERSION = "raw_candidate_pool_v1"
PROCESSING_REPORT_SCHEMA_VERSION = "processing_report_v1"
PROCESSING_VERSION = "processing_mk1_v1"

# Canonical MK1 candidate schema identifier. The candidate object shape is the
# raw_candidate_pool_v1 candidates[] entry — not a separate competing schema.
CANONICAL_MK1_CANDIDATE_SCHEMA_VERSION = "mk1_candidate_v1"

RAW_CANDIDATE_POOL_FILENAME = "raw_candidate_pool.json"
PROCESSING_REPORT_FILENAME = "processing_report.json"

REQUIRED_SCORE_FIELDS = (
    "hook_strength",
    "standalone_context",
    "insight_value",
    "retention_potential",
    "natural_ending",
    "overall_potential",
)

CANDIDATE_EVIDENCE_TEXT_FIELDS = (
    "hook_text",
    "core_idea_summary",
    "why_candidate_has_potential",
)

CANDIDATE_EVIDENCE_FIELDS = (
    "source_section_id",
    *CANDIDATE_EVIDENCE_TEXT_FIELDS,
    "scores",
    "confidence",
    "warnings",
)

ALLOWED_CANDIDATE_ARCHETYPES = (
    "valuable_insight",
    "funny_moment",
    "controversial_opinion",
    "story",
    "explanation",
    "emotional_moment",
    "surprising_fact",
    "tactical_advice",
    "business_lesson",
    "strong_quote",
    "other",
)

ALLOWED_ARCHETYPES = frozenset(ALLOWED_CANDIDATE_ARCHETYPES)

ALLOWED_TRANSCRIPT_QUALITY_FLAG_VALUES = (
    "low_transcript_confidence",
    "speaker_confusion",
    "missing_words",
    "timestamp_uncertainty",
    "unclear_audio",
    "poor_punctuation",
)

ALLOWED_TRANSCRIPT_QUALITY_FLAGS = frozenset(ALLOWED_TRANSCRIPT_QUALITY_FLAG_VALUES)

RAW_CANDIDATE_POOL_REQUIRED_FIELDS = (
    "schema_version",
    "job_id",
    "source_video_path",
    "transcript_path",
    "processing_version",
    "funnel_id",
    "created_at",
    "candidates",
    "diagnostics",
)

CANDIDATE_REQUIRED_FIELDS = (
    "candidate_id",
    "source_section_id",
    "start_sec",
    "end_sec",
    "duration_sec",
    "hook_text",
    "core_idea_summary",
    "why_candidate_has_potential",
    "archetype",
    "confidence",
    "scores",
    "warnings",
    "transcript_quality_flags",
)

# Alias: canonical MK1 candidate required fields match pool candidate fields.
CANONICAL_MK1_CANDIDATE_REQUIRED_FIELDS = CANDIDATE_REQUIRED_FIELDS

# Fields required by render_clip_v1 from a selected candidate entry.
MK1_CANDIDATE_RENDER_REQUIRED_FIELDS = (
    "candidate_id",
    "start_sec",
    "end_sec",
)

# Score components on the canonical candidate (model-provided attributes, 0-10).
MK1_CANDIDATE_SCORE_FIELDS = REQUIRED_SCORE_FIELDS

PROCESSING_REPORT_COUNT_FIELDS = (
    "sections_analysed",
    "usable_sections",
    "rejected_sections",
    "candidates_discovered",
    "candidates_rejected_by_boundary",
    "duplicates_removed",
    "final_candidate_count",
)

PROCESSING_REPORT_LIST_FIELDS = (
    "transcript_warnings",
    "processing_warnings",
    "common_rejection_reasons",
    "failed_sections",
)

PROCESSING_REPORT_REQUIRED_FIELDS = (
    "schema_version",
    "job_id",
    "processing_version",
    "funnel_id",
    *PROCESSING_REPORT_COUNT_FIELDS,
    *PROCESSING_REPORT_LIST_FIELDS,
    "prompt_metadata",
    "created_at",
)

DURATION_TOLERANCE_SEC = 0.001


class ProcessingContractValidationError(ValueError):
    """Raised when a processing handoff artifact does not match its contract."""

    def __init__(self, artifact_name: str, errors: list[str]):
        self.artifact_name = artifact_name
        self.errors = errors
        joined = "; ".join(errors)
        super().__init__(f"{artifact_name} validation failed: {joined}")


def make_candidate_id(
    *,
    job_id: str,
    source_section_id: str,
    start_sec: float,
    end_sec: float,
    candidate_index: int | None = None,
) -> str:
    """Return a deterministic candidate ID from stable job/section/timestamp values."""

    parts = [
        str(job_id).strip(),
        str(source_section_id).strip(),
        _format_id_time(start_sec),
        _format_id_time(end_sec),
    ]
    if candidate_index is not None:
        parts.append(str(int(candidate_index)))
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"cand_{digest}"


def build_raw_candidate_pool(
    *,
    job_id: str,
    source_video_path: str,
    transcript_path: str,
    funnel_id: str,
    candidates: list[dict[str, Any]] | None = None,
    diagnostics: dict[str, Any] | None = None,
    processing_version: str = PROCESSING_VERSION,
    created_at: str | None = None,
    execution_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a raw candidate pool payload with no discovery side effects."""

    payload: dict[str, Any] = {
        "schema_version": RAW_CANDIDATE_POOL_SCHEMA_VERSION,
        "job_id": job_id,
        "source_video_path": source_video_path,
        "transcript_path": transcript_path,
        "processing_version": processing_version,
        "funnel_id": funnel_id,
        "created_at": created_at or _now_iso(),
        "candidates": list(candidates or []),
        "diagnostics": dict(diagnostics or {}),
    }
    validate_raw_candidate_pool(payload)
    # Add execution_context after validation — validators do not reject unknown fields.
    # Legacy jobs without context omit this field rather than including null.
    if execution_context is not None:
        payload["execution_context"] = dict(execution_context)
    return payload


def build_processing_report(
    *,
    job_id: str,
    processing_version: str = PROCESSING_VERSION,
    funnel_id: str | None = None,
    sections_analysed: int = 0,
    usable_sections: int = 0,
    rejected_sections: int = 0,
    candidates_discovered: int = 0,
    candidates_rejected_by_boundary: int = 0,
    duplicates_removed: int = 0,
    final_candidate_count: int = 0,
    transcript_warnings: list[Any] | None = None,
    processing_warnings: list[Any] | None = None,
    common_rejection_reasons: list[Any] | None = None,
    failed_sections: list[Any] | None = None,
    prompt_metadata: dict[str, Any] | None = None,
    created_at: str | None = None,
    execution_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a processing report payload with default zero counts."""

    payload: dict[str, Any] = {
        "schema_version": PROCESSING_REPORT_SCHEMA_VERSION,
        "job_id": job_id,
        "processing_version": processing_version,
        "funnel_id": funnel_id,
        "sections_analysed": sections_analysed,
        "usable_sections": usable_sections,
        "rejected_sections": rejected_sections,
        "candidates_discovered": candidates_discovered,
        "candidates_rejected_by_boundary": candidates_rejected_by_boundary,
        "duplicates_removed": duplicates_removed,
        "final_candidate_count": final_candidate_count,
        "transcript_warnings": list(transcript_warnings or []),
        "processing_warnings": list(processing_warnings or []),
        "common_rejection_reasons": list(common_rejection_reasons or []),
        "failed_sections": list(failed_sections or []),
        "prompt_metadata": dict(prompt_metadata or {}),
        "created_at": created_at or _now_iso(),
    }
    validate_processing_report(payload)
    # Add execution_context after validation — validators do not reject unknown fields.
    if execution_context is not None:
        payload["execution_context"] = dict(execution_context)
    return payload


def validate_raw_candidate_pool(payload: Any) -> None:
    errors: list[str] = []
    if not isinstance(payload, dict):
        raise ProcessingContractValidationError(
            RAW_CANDIDATE_POOL_FILENAME, ["payload must be an object"]
        )

    _check_required_fields(payload, RAW_CANDIDATE_POOL_REQUIRED_FIELDS, "root", errors)
    if payload.get("schema_version") != RAW_CANDIDATE_POOL_SCHEMA_VERSION:
        errors.append(
            f"root.schema_version must equal {RAW_CANDIDATE_POOL_SCHEMA_VERSION!r}"
        )
    if not _is_non_empty_string(payload.get("job_id")):
        errors.append("root.job_id must be a non-empty string")
    for field in ("source_video_path", "transcript_path", "processing_version", "funnel_id"):
        if field in payload and not isinstance(payload.get(field), str):
            errors.append(f"root.{field} must be a string")
    if not _is_iso_timestamp(payload.get("created_at")):
        errors.append("root.created_at must be a non-empty ISO timestamp string")
    if "candidates" in payload and not isinstance(payload.get("candidates"), list):
        errors.append("root.candidates must be a list")
    if "diagnostics" in payload and not isinstance(payload.get("diagnostics"), dict):
        errors.append("root.diagnostics must be an object")

    candidates = payload.get("candidates")
    if isinstance(candidates, list):
        for index, candidate in enumerate(candidates):
            _validate_candidate(candidate, f"candidates[{index}]", errors)

    if errors:
        raise ProcessingContractValidationError(RAW_CANDIDATE_POOL_FILENAME, errors)


def validate_processing_report(payload: Any) -> None:
    errors: list[str] = []
    if not isinstance(payload, dict):
        raise ProcessingContractValidationError(
            PROCESSING_REPORT_FILENAME, ["payload must be an object"]
        )

    _check_required_fields(payload, PROCESSING_REPORT_REQUIRED_FIELDS, "root", errors)
    if payload.get("schema_version") != PROCESSING_REPORT_SCHEMA_VERSION:
        errors.append(
            f"root.schema_version must equal {PROCESSING_REPORT_SCHEMA_VERSION!r}"
        )
    if not _is_non_empty_string(payload.get("job_id")):
        errors.append("root.job_id must be a non-empty string")
    if "processing_version" in payload and not _is_non_empty_string(payload.get("processing_version")):
        errors.append("root.processing_version must be a non-empty string")
    if "funnel_id" in payload and payload.get("funnel_id") is not None and not isinstance(payload.get("funnel_id"), str):
        errors.append("root.funnel_id must be a string or null")

    for field in PROCESSING_REPORT_COUNT_FIELDS:
        if field in payload and not _is_non_negative_int(payload.get(field)):
            errors.append(f"root.{field} must be a non-negative integer")
    for field in PROCESSING_REPORT_LIST_FIELDS:
        if field in payload and not isinstance(payload.get(field), list):
            errors.append(f"root.{field} must be a list")
    if "prompt_metadata" in payload and not isinstance(payload.get("prompt_metadata"), dict):
        errors.append("root.prompt_metadata must be an object")
    if "created_at" in payload and not _is_iso_timestamp(payload.get("created_at")):
        errors.append("root.created_at must be a non-empty ISO timestamp string")

    if errors:
        raise ProcessingContractValidationError(PROCESSING_REPORT_FILENAME, errors)


def raw_candidate_pool_path(job_dir: str) -> str:
    return os.path.join(job_dir, RAW_CANDIDATE_POOL_FILENAME)


def processing_report_path(job_dir: str) -> str:
    return os.path.join(job_dir, PROCESSING_REPORT_FILENAME)


def write_raw_candidate_pool(job_dir: str, payload: dict[str, Any]) -> str:
    validate_raw_candidate_pool(payload)
    path = raw_candidate_pool_path(job_dir)
    write_json(path, payload)
    return path


def write_processing_report(job_dir: str, payload: dict[str, Any]) -> str:
    validate_processing_report(payload)
    path = processing_report_path(job_dir)
    write_json(path, payload)
    return path


def validate_mk1_candidate(candidate: Any, *, path: str = "candidate") -> None:
    """Validate one canonical MK1 candidate object.

    Raises :class:`ProcessingContractValidationError` when the candidate does
    not match the ``raw_candidate_pool_v1`` candidate contract.
    """
    errors: list[str] = []
    _validate_candidate(candidate, path, errors)
    if errors:
        raise ProcessingContractValidationError("mk1_candidate", errors)


def _validate_candidate(candidate: Any, path: str, errors: list[str]) -> None:
    if not isinstance(candidate, dict):
        errors.append(f"{path} must be an object")
        return

    _check_required_fields(candidate, CANDIDATE_REQUIRED_FIELDS, path, errors)
    if not _is_non_empty_string(candidate.get("candidate_id")):
        errors.append(f"{path}.candidate_id must be a non-empty string")
    if not _is_non_empty_string(candidate.get("source_section_id")):
        errors.append(f"{path}.source_section_id must be a non-empty string")

    start = candidate.get("start_sec")
    end = candidate.get("end_sec")
    duration = candidate.get("duration_sec")
    start_ok = _is_number(start)
    end_ok = _is_number(end)
    duration_ok = _is_number(duration)
    if not start_ok:
        errors.append(f"{path}.start_sec must be numeric")
    if not end_ok:
        errors.append(f"{path}.end_sec must be numeric")
    if start_ok and end_ok and float(end) <= float(start):
        errors.append(f"{path}.end_sec must be greater than start_sec")
    if not duration_ok:
        errors.append(f"{path}.duration_sec must be numeric")
    if start_ok and end_ok and duration_ok:
        expected_duration = float(end) - float(start)
        if abs(float(duration) - expected_duration) > DURATION_TOLERANCE_SEC:
            errors.append(
                f"{path}.duration_sec must match end_sec - start_sec within "
                f"{DURATION_TOLERANCE_SEC:g}s"
            )

    for field in ("hook_text", "core_idea_summary", "why_candidate_has_potential"):
        if field in candidate and not isinstance(candidate.get(field), str):
            errors.append(f"{path}.{field} must be a string")

    archetype = candidate.get("archetype")
    if archetype not in ALLOWED_ARCHETYPES:
        errors.append(f"{path}.archetype must be one of {sorted(ALLOWED_ARCHETYPES)}")

    confidence = candidate.get("confidence")
    if not _is_number(confidence) or not 0.0 <= float(confidence) <= 1.0:
        errors.append(f"{path}.confidence must be numeric and within 0-1")

    scores = candidate.get("scores")
    if not isinstance(scores, dict):
        errors.append(f"{path}.scores must be an object")
    else:
        _validate_scores(scores, f"{path}.scores", errors)

    warnings = candidate.get("warnings")
    if not isinstance(warnings, list):
        errors.append(f"{path}.warnings must be a list")
    elif not all(isinstance(item, str) for item in warnings):
        errors.append(f"{path}.warnings must contain only strings")

    flags = candidate.get("transcript_quality_flags")
    if not isinstance(flags, list):
        errors.append(f"{path}.transcript_quality_flags must be a list")
    else:
        for flag_index, flag in enumerate(flags):
            if flag not in ALLOWED_TRANSCRIPT_QUALITY_FLAGS:
                errors.append(
                    f"{path}.transcript_quality_flags[{flag_index}] must be one of "
                    f"{sorted(ALLOWED_TRANSCRIPT_QUALITY_FLAGS)}"
                )


def _validate_scores(scores: dict[str, Any], path: str, errors: list[str]) -> None:
    for field in REQUIRED_SCORE_FIELDS:
        if field not in scores:
            errors.append(f"{path}.{field} is required")
            continue
        value = scores.get(field)
        if not _is_number(value) or not 0.0 <= float(value) <= 10.0:
            errors.append(f"{path}.{field} must be numeric and within 0-10")


def _check_required_fields(
    payload: dict[str, Any],
    required_fields: tuple[str, ...],
    path: str,
    errors: list[str],
) -> None:
    for field in required_fields:
        if field not in payload:
            errors.append(f"{path}.{field} is required")


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_number(value: Any) -> bool:
    return isinstance(value, Number) and not isinstance(value, bool) and math.isfinite(float(value))


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_iso_timestamp(value: Any) -> bool:
    if not _is_non_empty_string(value):
        return False
    text = str(value).strip()
    if "T" not in text:
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_id_time(value: float) -> str:
    return f"{float(value):.3f}"
