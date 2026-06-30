"""Deterministic boundary sanity checks for raw candidate timestamps.

This pass rejects obviously broken candidate timing and appends conservative
boundary warnings. It does not optimise, shift, trim, or extend timestamps.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

TIMESTAMP_TOLERANCE_SEC = 0.001
DEFAULT_MIN_CANDIDATE_DURATION_SEC = 15.0
DEFAULT_MAX_CANDIDATE_DURATION_SEC = 120.0
DEFAULT_MIN_TRANSCRIPT_COVERAGE_SEC = 2.0
DEFAULT_CONTEXT_WINDOW_SEC = 5.0
DEFAULT_SECTION_EDGE_WARNING_SEC = 2.0

REJECTION_INVALID_TIMESTAMP = "invalid_timestamp"
REJECTION_NON_NUMERIC_TIMESTAMP = "non_numeric_timestamp"
REJECTION_END_BEFORE_START = "end_before_start"
REJECTION_DURATION_TOO_SHORT = "duration_too_short"
REJECTION_DURATION_TOO_LONG = "duration_too_long"
REJECTION_OUTSIDE_SECTION_BOUNDS = "outside_section_bounds"
REJECTION_OUTSIDE_TRANSCRIPT_BOUNDS = "outside_transcript_bounds"
REJECTION_OUTSIDE_VIDEO_BOUNDS = "outside_video_bounds"
REJECTION_NO_TRANSCRIPT_COVERAGE = "no_transcript_coverage"
REJECTION_NOT_ENOUGH_CONTEXT = "not_enough_context"

WARNING_START_MAY_BE_MID_SENTENCE = "boundary_start_may_be_mid_sentence"
WARNING_END_MAY_BE_MID_IDEA = "boundary_end_may_be_mid_idea"
WARNING_LOW_CONTEXT_BEFORE = "boundary_low_context_before"
WARNING_LOW_CONTEXT_AFTER = "boundary_low_context_after"
WARNING_NEAR_SECTION_EDGE = "boundary_near_section_edge"
WARNING_TIMESTAMP_UNCERTAINTY = "boundary_timestamp_uncertainty"

_CONTINUATION_START_WORDS = {
    "and",
    "but",
    "so",
    "because",
    "then",
    "which",
    "that",
    "it",
    "he",
    "she",
    "they",
    "this",
    "those",
}
_CONTINUATION_END_WORDS = {"because", "and", "but", "so", "which", "that"}
_TIMED_LINE_RE = re.compile(
    r"^\[\s*(?P<start>-?\d+(?:\.\d+)?)\s*->\s*(?P<end>-?\d+(?:\.\d+)?)\s*\]\s*(?P<text>.*)$"
)


@dataclass(frozen=True)
class BoundarySanityConfig:
    min_candidate_duration_sec: float = DEFAULT_MIN_CANDIDATE_DURATION_SEC
    max_candidate_duration_sec: float = DEFAULT_MAX_CANDIDATE_DURATION_SEC
    transcript_start_sec: float | None = None
    transcript_end_sec: float | None = None
    video_duration_sec: float | None = None
    min_transcript_coverage_sec: float = DEFAULT_MIN_TRANSCRIPT_COVERAGE_SEC
    context_window_sec: float = DEFAULT_CONTEXT_WINDOW_SEC
    section_edge_warning_sec: float = DEFAULT_SECTION_EDGE_WARNING_SEC


@dataclass(frozen=True)
class BoundarySanityResult:
    accepted: bool
    candidate: dict[str, Any]
    rejection_reasons: tuple[str, ...]
    warnings: tuple[str, ...]


def apply_boundary_sanity(
    candidate: dict[str, Any],
    section: dict[str, Any],
    config: BoundarySanityConfig | None = None,
) -> BoundarySanityResult:
    cfg = config or BoundarySanityConfig()
    checked = dict(candidate)
    rejection_reasons: list[str] = []
    warnings: list[str] = []

    start_missing = "start_sec" not in checked
    end_missing = "end_sec" not in checked
    if start_missing or end_missing:
        return _rejected(checked, [REJECTION_INVALID_TIMESTAMP], warnings)

    start = _finite_float(checked.get("start_sec"))
    end = _finite_float(checked.get("end_sec"))
    if start is None or end is None:
        return _rejected(checked, [REJECTION_NON_NUMERIC_TIMESTAMP], warnings)
    if start < 0:
        rejection_reasons.append(REJECTION_INVALID_TIMESTAMP)
    if end <= start:
        rejection_reasons.append(REJECTION_END_BEFORE_START)
    if rejection_reasons:
        return _rejected(checked, rejection_reasons, warnings)

    duration = end - start
    checked["start_sec"] = start
    checked["end_sec"] = end
    checked["duration_sec"] = duration

    if duration < cfg.min_candidate_duration_sec - TIMESTAMP_TOLERANCE_SEC:
        rejection_reasons.append(REJECTION_DURATION_TOO_SHORT)
    if duration > cfg.max_candidate_duration_sec + TIMESTAMP_TOLERANCE_SEC:
        rejection_reasons.append(REJECTION_DURATION_TOO_LONG)

    section_start = _finite_float(section.get("start_sec"))
    section_end = _finite_float(section.get("end_sec"))
    if section_start is not None and start < section_start - TIMESTAMP_TOLERANCE_SEC:
        rejection_reasons.append(REJECTION_OUTSIDE_SECTION_BOUNDS)
    if section_end is not None and end > section_end + TIMESTAMP_TOLERANCE_SEC:
        rejection_reasons.append(REJECTION_OUTSIDE_SECTION_BOUNDS)

    transcript_start = _first_finite(cfg.transcript_start_sec, _metadata_float(section, "transcript_start_sec"))
    transcript_end = _first_finite(cfg.transcript_end_sec, _metadata_float(section, "transcript_end_sec"))
    if transcript_start is not None and start < transcript_start - TIMESTAMP_TOLERANCE_SEC:
        rejection_reasons.append(REJECTION_OUTSIDE_TRANSCRIPT_BOUNDS)
    if transcript_end is not None and end > transcript_end + TIMESTAMP_TOLERANCE_SEC:
        rejection_reasons.append(REJECTION_OUTSIDE_TRANSCRIPT_BOUNDS)

    video_duration = _finite_float(cfg.video_duration_sec)
    if video_duration is not None and end > video_duration + TIMESTAMP_TOLERANCE_SEC:
        rejection_reasons.append(REJECTION_OUTSIDE_VIDEO_BOUNDS)

    coverage_segments = _coverage_segments(section)
    if coverage_segments:
        covered_duration = _covered_duration(start, end, coverage_segments)
        if covered_duration < cfg.min_transcript_coverage_sec - TIMESTAMP_TOLERANCE_SEC:
            rejection_reasons.append(REJECTION_NO_TRANSCRIPT_COVERAGE)
        _append_context_warnings(start, end, coverage_segments, cfg, warnings)
    else:
        warnings.append(WARNING_TIMESTAMP_UNCERTAINTY)

    if section_start is not None and start <= section_start + cfg.section_edge_warning_sec:
        warnings.append(WARNING_NEAR_SECTION_EDGE)
    if section_end is not None and end >= section_end - cfg.section_edge_warning_sec:
        warnings.append(WARNING_NEAR_SECTION_EDGE)

    text_segments = _text_segments(section)
    if text_segments:
        _append_text_boundary_warnings(start, end, text_segments, warnings)

    if rejection_reasons:
        return _rejected(checked, _dedupe(rejection_reasons), warnings)

    checked["warnings"] = _merge_warnings(checked.get("warnings"), warnings)
    return BoundarySanityResult(
        accepted=True,
        candidate=checked,
        rejection_reasons=(),
        warnings=tuple(_dedupe(warnings)),
    )


def rejected_candidate_record(
    candidate: dict[str, Any],
    result: BoundarySanityResult,
) -> dict[str, Any]:
    return {
        "source_section_id": str(candidate.get("source_section_id") or ""),
        "candidate_local_id": str(candidate.get("candidate_local_id") or ""),
        "start_sec": candidate.get("start_sec"),
        "end_sec": candidate.get("end_sec"),
        "rejection_reasons": list(result.rejection_reasons),
    }


def _rejected(
    candidate: dict[str, Any],
    rejection_reasons: list[str],
    warnings: list[str],
) -> BoundarySanityResult:
    return BoundarySanityResult(
        accepted=False,
        candidate=dict(candidate),
        rejection_reasons=tuple(_dedupe(rejection_reasons)),
        warnings=tuple(_dedupe(warnings)),
    )


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _metadata_float(section: dict[str, Any], key: str) -> float | None:
    metadata = section.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return _finite_float(metadata.get(key))


def _first_finite(*values: Any) -> float | None:
    for value in values:
        parsed = _finite_float(value)
        if parsed is not None:
            return parsed
    return None


def _coverage_segments(section: dict[str, Any]) -> list[tuple[float, float]]:
    refs = section.get("source_segment_refs")
    segments: list[tuple[float, float]] = []
    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            start = _finite_float(ref.get("start_sec"))
            end = _finite_float(ref.get("end_sec"))
            if start is not None and end is not None and end > start:
                segments.append((start, end))
    if segments:
        return sorted(segments)
    return [(start, end) for start, end, _text in _text_segments(section)]


def _text_segments(section: dict[str, Any]) -> list[tuple[float, float, str]]:
    text = section.get("text")
    if not isinstance(text, str):
        return []
    out: list[tuple[float, float, str]] = []
    for line in text.splitlines():
        match = _TIMED_LINE_RE.match(line.strip())
        if not match:
            continue
        start = _finite_float(match.group("start"))
        end = _finite_float(match.group("end"))
        segment_text = str(match.group("text") or "").strip()
        if start is not None and end is not None and end > start:
            out.append((start, end, segment_text))
    return sorted(out)


def _covered_duration(start: float, end: float, segments: list[tuple[float, float]]) -> float:
    total = 0.0
    for seg_start, seg_end in segments:
        overlap = min(end, seg_end) - max(start, seg_start)
        if overlap > 0:
            total += overlap
    return total


def _append_context_warnings(
    start: float,
    end: float,
    segments: list[tuple[float, float]],
    config: BoundarySanityConfig,
    warnings: list[str],
) -> None:
    has_context_before = any(
        seg_end <= start + TIMESTAMP_TOLERANCE_SEC and start - seg_end <= config.context_window_sec
        for _seg_start, seg_end in segments
    )
    has_context_after = any(
        seg_start >= end - TIMESTAMP_TOLERANCE_SEC and seg_start - end <= config.context_window_sec
        for seg_start, _seg_end in segments
    )
    if not has_context_before:
        warnings.append(WARNING_LOW_CONTEXT_BEFORE)
    if not has_context_after:
        warnings.append(WARNING_LOW_CONTEXT_AFTER)


def _append_text_boundary_warnings(
    start: float,
    end: float,
    segments: list[tuple[float, float, str]],
    warnings: list[str],
) -> None:
    overlaps = [
        (seg_start, seg_end, text)
        for seg_start, seg_end, text in segments
        if seg_start < end and seg_end > start
    ]
    if not overlaps:
        return

    first_start, _first_end, first_text = overlaps[0]
    if start > first_start + TIMESTAMP_TOLERANCE_SEC or _starts_with_continuation(first_text):
        warnings.append(WARNING_START_MAY_BE_MID_SENTENCE)

    _last_start, last_end, last_text = overlaps[-1]
    if end < last_end - TIMESTAMP_TOLERANCE_SEC or _ends_like_continuation(last_text):
        warnings.append(WARNING_END_MAY_BE_MID_IDEA)


def _starts_with_continuation(text: str) -> bool:
    words = re.findall(r"[A-Za-z']+", text.lower())
    return bool(words and words[0] in _CONTINUATION_START_WORDS)


def _ends_like_continuation(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[-1] in ".?!":
        return False
    words = re.findall(r"[A-Za-z']+", stripped.lower())
    return not words or words[-1] in _CONTINUATION_END_WORDS


def _merge_warnings(existing: Any, additions: list[str]) -> list[str]:
    values: list[str] = []
    if isinstance(existing, list):
        values.extend(item for item in existing if isinstance(item, str))
    values.extend(additions)
    return _dedupe(values)


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
