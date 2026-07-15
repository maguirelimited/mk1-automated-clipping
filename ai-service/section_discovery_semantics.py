"""Deterministic semantic normalisation for section_candidate_discovery results.

Runs after JSON-schema validation and before ai-service returns status:ok to
video-automation.  Fixes small metadata mistakes, rejects semantically invalid
candidates, and downgrades to usable=false when nothing valid remains.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from task_router import AITaskError

SCHEMA_VERSION = "section_candidate_discovery_v1"
TIMESTAMP_TOLERANCE_SEC = 0.001

DEFAULT_MIN_CANDIDATE_DURATION_SEC = 15.0
DEFAULT_MAX_CANDIDATE_DURATION_SEC = 120.0

ALL_REJECTED_REASON = "No semantically valid candidates after ai-service normalisation."

PLACEHOLDER_TEXT_EXACT = frozenset(
    {
        "the opening spoken idea",
        "short summary of the raw opportunity",
        "why this is worth passing forward for the resolved funnel",
        "short section-level judgement",
        "why this is worth passing forward",
        "opening spoken idea",
        "example hook",
        "example summary",
        "example text",
        "n/a",
        "none",
        "todo",
        "tbd",
    }
)

EVIDENCE_TEXT_FIELDS = (
    "hook_text",
    "core_idea_summary",
    "why_candidate_has_potential",
)


def normalize_section_discovery_result(
    *,
    parsed: dict[str, Any],
    section: dict[str, Any],
    config: dict[str, Any],
    job_id: str,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Normalise model output and reject semantically invalid candidates."""
    section_id = _non_empty_str(section.get("section_id"))
    if not section_id:
        raise AITaskError(
            "INVALID_TASK_INPUT",
            "section_candidate_discovery input.section.section_id must be non-empty.",
            status_code=400,
        )

    min_duration = _config_float(
        config, "min_candidate_duration_sec", DEFAULT_MIN_CANDIDATE_DURATION_SEC
    )
    max_duration = _config_float(
        config, "max_candidate_duration_sec", DEFAULT_MAX_CANDIDATE_DURATION_SEC
    )
    section_start = _optional_float(section.get("start_sec"))
    section_end = _optional_float(section.get("end_sec"))

    out = dict(parsed)
    out["schema_version"] = SCHEMA_VERSION
    out["section_id"] = section_id

    raw_candidates = out.get("candidates")
    if not isinstance(raw_candidates, list):
        raw_candidates = []

    kept: list[dict[str, Any]] = []
    rejection_warnings: list[str] = []

    for index, raw in enumerate(raw_candidates):
        if not isinstance(raw, dict):
            rejection_warnings.append(
                f"ai_service_semantic_rejection:candidate_{index}:not_an_object"
            )
            continue
        normalised, reject_reason = _normalise_and_validate_candidate(
            raw,
            index=index,
            section_id=section_id,
            job_id=job_id,
            min_duration=min_duration,
            max_duration=max_duration,
            section_start=section_start,
            section_end=section_end,
        )
        if reject_reason:
            rejection_warnings.append(reject_reason)
            continue
        kept.append(normalised)

    merged_warnings = _merge_warnings(out.get("warnings"), rejection_warnings)

    if kept:
        out["usable"] = True
        out["candidates"] = kept
        out["warnings"] = merged_warnings
        if not _non_empty_str(out.get("reason")):
            out["reason"] = "Semantically valid candidates found after ai-service normalisation."
    else:
        out["usable"] = False
        out["candidates"] = []
        out["reason"] = ALL_REJECTED_REASON
        out["warnings"] = merged_warnings
        if not _is_confidence(out.get("confidence")):
            out["confidence"] = 0.0
        elif float(out["confidence"]) > 0.3:
            out["confidence"] = 0.3

    if not isinstance(out.get("transcript_quality_flags"), list):
        out["transcript_quality_flags"] = []
    if not isinstance(out.get("warnings"), list):
        out["warnings"] = []
    if not _is_confidence(out.get("confidence")):
        out["confidence"] = 0.0 if not kept else 0.5
    if not isinstance(out.get("reason"), str):
        out["reason"] = ALL_REJECTED_REASON if not kept else ""

    _assert_schema(out, schema)
    return out


def _normalise_and_validate_candidate(
    raw: dict[str, Any],
    *,
    index: int,
    section_id: str,
    job_id: str,
    min_duration: float,
    max_duration: float,
    section_start: float | None,
    section_end: float | None,
) -> tuple[dict[str, Any] | None, str | None]:
    candidate = dict(raw)

    start = _optional_float(candidate.get("start_sec"))
    end = _optional_float(candidate.get("end_sec"))
    if start is None or end is None:
        return None, f"ai_service_semantic_rejection:candidate_{index}:non_numeric_timestamps"
    if end <= start + TIMESTAMP_TOLERANCE_SEC:
        return None, f"ai_service_semantic_rejection:candidate_{index}:end_not_after_start"

    duration = end - start

    if section_start is not None and start < section_start - TIMESTAMP_TOLERANCE_SEC:
        return None, f"ai_service_semantic_rejection:candidate_{index}:start_outside_section"
    if section_end is not None and end > section_end + TIMESTAMP_TOLERANCE_SEC:
        return None, f"ai_service_semantic_rejection:candidate_{index}:end_outside_section"

    if duration < min_duration - TIMESTAMP_TOLERANCE_SEC:
        return None, f"ai_service_semantic_rejection:candidate_{index}:duration_below_min"
    if duration > max_duration + TIMESTAMP_TOLERANCE_SEC:
        return None, f"ai_service_semantic_rejection:candidate_{index}:duration_above_max"

    for field in EVIDENCE_TEXT_FIELDS:
        if is_placeholder_text(candidate.get(field)):
            return None, f"ai_service_semantic_rejection:candidate_{index}:placeholder_{field}"

    local_id = candidate.get("candidate_local_id") or candidate.get("candidate_id")
    if is_placeholder_candidate_id(local_id, section_id=section_id):
        local_id = stable_candidate_local_id(job_id, section_id, start, end)
    elif not _non_empty_str(local_id):
        local_id = stable_candidate_local_id(job_id, section_id, start, end)

    candidate["candidate_local_id"] = str(local_id).strip()
    candidate["source_section_id"] = section_id
    candidate["start_sec"] = start
    candidate["end_sec"] = end
    candidate["duration_sec"] = duration

    return candidate, None


def stable_candidate_local_id(
    job_id: str,
    section_id: str,
    start_sec: float,
    end_sec: float,
) -> str:
    start_ms = int(round(start_sec * 1000))
    end_ms = int(round(end_sec * 1000))
    digest = hashlib.sha256(
        f"{job_id}|{section_id}|{start_ms}|{end_ms}".encode("utf-8")
    ).hexdigest()[:10]
    return f"{section_id}_c_{start_ms}_{end_ms}_{digest}"


def is_placeholder_candidate_id(value: Any, *, section_id: str) -> bool:
    if not isinstance(value, str) or not value.strip():
        return True
    token = value.strip().lower()
    if token in {"example_section_id", "example_candidate_id", "candidate_id", "id"}:
        return True
    if "example" in token and ("candidate" in token or "section" in token):
        return True
    if "placeholder" in token or token.startswith("sample_"):
        return True
    if re.fullmatch(r"\d{6,}", token):
        return True
    match = re.fullmatch(r"section_\d+_candidate_\d+", token)
    if match and not token.startswith(section_id.lower()):
        return True
    return False


def is_placeholder_text(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    text = value.strip().lower()
    if not text:
        return True
    if text in PLACEHOLDER_TEXT_EXACT:
        return True
    if text.startswith("lorem ipsum"):
        return True
    if text in {"...", "[...]", "…"}:
        return True
    return False


def _assert_schema(payload: dict[str, Any], schema: dict[str, Any]) -> None:
    try:
        Draft202012Validator(schema).validate(payload)
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.path)
        detail = f" at {path}" if path else ""
        raise AITaskError(
            "NORMALISED_OUTPUT_SCHEMA_FAILED",
            f"Normalised section discovery output failed schema validation{detail}: {exc.message}.",
            status_code=500,
        ) from exc


def _merge_warnings(existing: Any, additions: list[str]) -> list[str]:
    out: list[str] = []
    if isinstance(existing, list):
        out.extend(item for item in existing if isinstance(item, str))
    out.extend(additions)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in out:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _config_float(config: dict[str, Any], key: str, default: float) -> float:
    raw = config.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return value


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _is_confidence(value: Any) -> bool:
    number = _optional_float(value)
    return number is not None and 0.0 <= number <= 1.0


def _non_empty_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
