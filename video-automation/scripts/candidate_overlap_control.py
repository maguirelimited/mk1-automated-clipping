"""Timestamp-only duplicate control for raw candidates.

This helper removes duplicate versions of the same timestamp moment after
boundary sanity has accepted candidates. It deliberately avoids semantic/topic
dedupe and does not rank final clips for publication.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

DEFAULT_MIN_SHORTER_OVERLAP_RATIO = 0.80
DEFAULT_MIN_UNION_IOU = 0.65
DEFAULT_MIN_CONTAINMENT_UNION_IOU = 0.50
BOUNDARY_WARNING_PREFIX = "boundary_"


@dataclass(frozen=True)
class CandidateOverlapConfig:
    min_shorter_overlap_ratio: float = DEFAULT_MIN_SHORTER_OVERLAP_RATIO
    min_union_iou: float = DEFAULT_MIN_UNION_IOU
    min_containment_union_iou: float = DEFAULT_MIN_CONTAINMENT_UNION_IOU


@dataclass(frozen=True)
class CandidateOverlapControlResult:
    kept_candidates: tuple[dict[str, Any], ...]
    duplicate_removals: tuple[dict[str, Any], ...]


class CandidateOverlapControlError(ValueError):
    """Raised when overlap control receives malformed post-boundary candidates."""


def control_candidate_overlap(
    candidates: list[dict[str, Any]],
    config: CandidateOverlapConfig | None = None,
) -> CandidateOverlapControlResult:
    cfg = config or CandidateOverlapConfig()
    kept: list[dict[str, Any]] = []
    duplicate_removals: list[dict[str, Any]] = []

    for candidate in candidates:
        clean_candidate = _validated_candidate(candidate)
        duplicate_indexes = [
            index
            for index, kept_candidate in enumerate(kept)
            if are_timestamp_duplicates(clean_candidate, kept_candidate, cfg)
        ]
        if not duplicate_indexes:
            kept.append(clean_candidate)
            continue

        group = [clean_candidate, *(kept[index] for index in duplicate_indexes)]
        winner = _best_candidate(group)
        kept_ids_to_remove = set(duplicate_indexes)
        next_kept = [
            kept_candidate
            for index, kept_candidate in enumerate(kept)
            if index not in kept_ids_to_remove
        ]
        next_kept.append(winner)
        kept = sorted(next_kept, key=lambda item: (_start_sec(item), _candidate_id(item)))

        for loser in group:
            if loser is winner:
                continue
            duplicate_removals.append(_duplicate_record(removed=loser, kept=winner))

    return CandidateOverlapControlResult(
        kept_candidates=tuple(kept),
        duplicate_removals=tuple(duplicate_removals),
    )


def are_timestamp_duplicates(
    a: dict[str, Any],
    b: dict[str, Any],
    config: CandidateOverlapConfig | None = None,
) -> bool:
    cfg = config or CandidateOverlapConfig()
    overlap = overlap_duration(a, b)
    if overlap <= 0:
        return False
    shorter = min(_duration(a), _duration(b))
    shorter_ratio = overlap / shorter if shorter > 0 else 0.0
    iou = union_iou(a, b)
    return iou >= cfg.min_union_iou or (
        shorter_ratio >= cfg.min_shorter_overlap_ratio
        and iou >= cfg.min_containment_union_iou
    )


def overlap_duration(a: dict[str, Any], b: dict[str, Any]) -> float:
    return max(0.0, min(_end_sec(a), _end_sec(b)) - max(_start_sec(a), _start_sec(b)))


def shorter_overlap_ratio(a: dict[str, Any], b: dict[str, Any]) -> float:
    shorter = min(_duration(a), _duration(b))
    return overlap_duration(a, b) / shorter if shorter > 0 else 0.0


def union_iou(a: dict[str, Any], b: dict[str, Any]) -> float:
    overlap = overlap_duration(a, b)
    union = max(_end_sec(a), _end_sec(b)) - min(_start_sec(a), _start_sec(b))
    return overlap / union if union > 0 else 0.0


def _validated_candidate(candidate: Any) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise CandidateOverlapControlError("candidate must be an object")
    start = _finite_float(candidate.get("start_sec"))
    end = _finite_float(candidate.get("end_sec"))
    if start is None or end is None or end <= start:
        raise CandidateOverlapControlError("candidate timestamps must be finite and ordered")
    out = dict(candidate)
    out["start_sec"] = start
    out["end_sec"] = end
    out["duration_sec"] = end - start
    return out


def _duplicate_record(removed: dict[str, Any], kept: dict[str, Any]) -> dict[str, Any]:
    return {
        "removed_candidate_id": _candidate_id(removed),
        "kept_candidate_id": _candidate_id(kept),
        "removed_source_section_id": str(removed.get("source_section_id") or ""),
        "kept_source_section_id": str(kept.get("source_section_id") or ""),
        "removed_start_sec": _start_sec(removed),
        "removed_end_sec": _end_sec(removed),
        "kept_start_sec": _start_sec(kept),
        "kept_end_sec": _end_sec(kept),
        "reason": "timestamp_duplicate",
        "overlap_ratio": round(shorter_overlap_ratio(removed, kept), 6),
        "selection_reason": _selection_reason(kept, removed),
    }


def _best_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(candidates, key=_quality_key)[0]


def _selection_reason(kept: dict[str, Any], removed: dict[str, Any]) -> str:
    comparisons = (
        ("higher_overall_potential", _overall_potential),
        ("higher_confidence", _confidence),
        ("fewer_transcript_quality_flags", lambda c: -_transcript_quality_flag_count(c)),
        ("fewer_boundary_warnings", lambda c: -_boundary_warning_count(c)),
        ("higher_natural_ending", lambda c: _score(c, "natural_ending")),
        ("higher_standalone_context", lambda c: _score(c, "standalone_context")),
        ("longer_duration", _duration),
    )
    for reason, getter in comparisons:
        if getter(kept) > getter(removed):
            return reason
        if getter(kept) < getter(removed):
            return "deterministic_tie_break"
    return "deterministic_tie_break"


def _quality_key(candidate: dict[str, Any]) -> tuple:
    return (
        -_overall_potential(candidate),
        -_confidence(candidate),
        _transcript_quality_flag_count(candidate),
        _boundary_warning_count(candidate),
        -_score(candidate, "natural_ending"),
        -_score(candidate, "standalone_context"),
        -_duration(candidate),
        _start_sec(candidate),
        _candidate_id(candidate),
    )


def _candidate_id(candidate: dict[str, Any]) -> str:
    raw = candidate.get("candidate_id") or candidate.get("candidate_local_id") or ""
    return str(raw)


def _overall_potential(candidate: dict[str, Any]) -> float:
    return _score(candidate, "overall_potential")


def _score(candidate: dict[str, Any], field: str) -> float:
    scores = candidate.get("scores")
    if not isinstance(scores, dict):
        return 0.0
    value = _finite_float(scores.get(field))
    return value if value is not None else 0.0


def _confidence(candidate: dict[str, Any]) -> float:
    value = _finite_float(candidate.get("confidence"))
    return value if value is not None else 0.0


def _transcript_quality_flag_count(candidate: dict[str, Any]) -> int:
    flags = candidate.get("transcript_quality_flags")
    return len(flags) if isinstance(flags, list) else 0


def _boundary_warning_count(candidate: dict[str, Any]) -> int:
    warnings = candidate.get("warnings")
    if not isinstance(warnings, list):
        return 0
    return sum(1 for warning in warnings if isinstance(warning, str) and warning.startswith(BOUNDARY_WARNING_PREFIX))


def _duration(candidate: dict[str, Any]) -> float:
    return _end_sec(candidate) - _start_sec(candidate)


def _start_sec(candidate: dict[str, Any]) -> float:
    value = _finite_float(candidate.get("start_sec"))
    if value is None:
        raise CandidateOverlapControlError("candidate start_sec must be finite")
    return value


def _end_sec(candidate: dict[str, Any]) -> float:
    value = _finite_float(candidate.get("end_sec"))
    if value is None:
        raise CandidateOverlapControlError("candidate end_sec must be finite")
    return value


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed
