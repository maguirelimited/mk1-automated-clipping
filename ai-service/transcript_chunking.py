from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from transcript_context import (
    build_prompt_safe_transcript_block,
    normalize_transcript_context,
    validate_transcript_context,
)


DEFAULT_SECTION_SIZE_SECONDS = 300.0
DEFAULT_SECTION_OVERLAP_SECONDS = 20.0
DEFAULT_CANDIDATE_CAP_PER_SECTION = 2
DEFAULT_FINAL_CANDIDATE_CAP = 8
DEFAULT_PREFERRED_CLIP_LENGTH_SECONDS = [35, 75]


class TranscriptChunkingError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class ChunkingOptions:
    section_size_seconds: float = DEFAULT_SECTION_SIZE_SECONDS
    section_overlap_seconds: float = DEFAULT_SECTION_OVERLAP_SECONDS
    candidate_cap_per_section: int = DEFAULT_CANDIDATE_CAP_PER_SECTION
    final_candidate_cap: int = DEFAULT_FINAL_CANDIDATE_CAP
    preferred_clip_length_seconds: list[float] | None = None


@dataclass(frozen=True)
class SectionScoringConfig:
    candidate_cap_per_section: int
    preferred_clip_length_seconds: list[float]


def apply_default_chunking_options(options: dict[str, Any] | ChunkingOptions | None = None) -> ChunkingOptions:
    if options is None:
        return ChunkingOptions()
    if isinstance(options, ChunkingOptions):
        return options
    return ChunkingOptions(
        section_size_seconds=float(options.get("section_size_seconds", DEFAULT_SECTION_SIZE_SECONDS)),
        section_overlap_seconds=float(options.get("section_overlap_seconds", DEFAULT_SECTION_OVERLAP_SECONDS)),
        candidate_cap_per_section=int(options.get("candidate_cap_per_section", DEFAULT_CANDIDATE_CAP_PER_SECTION)),
        final_candidate_cap=int(options.get("final_candidate_cap", DEFAULT_FINAL_CANDIDATE_CAP)),
        preferred_clip_length_seconds=options.get("preferred_clip_length_seconds"),
    )


def validate_chunking_options(options: ChunkingOptions) -> None:
    if options.section_size_seconds <= 0:
        raise TranscriptChunkingError("INVALID_CHUNKING_OPTIONS", "section_size_seconds must be > 0.")
    if options.section_overlap_seconds < 0:
        raise TranscriptChunkingError("INVALID_CHUNKING_OPTIONS", "section_overlap_seconds must be >= 0.")
    if options.section_overlap_seconds >= options.section_size_seconds:
        raise TranscriptChunkingError(
            "INVALID_CHUNKING_OPTIONS",
            "section_overlap_seconds must be less than section_size_seconds.",
        )
    if not 1 <= options.candidate_cap_per_section <= 3:
        raise TranscriptChunkingError(
            "INVALID_CHUNKING_OPTIONS",
            "candidate_cap_per_section must be between 1 and 3.",
        )
    if not 5 <= options.final_candidate_cap <= 10:
        raise TranscriptChunkingError("INVALID_CHUNKING_OPTIONS", "final_candidate_cap must be between 5 and 10.")
    if options.preferred_clip_length_seconds is not None:
        _validate_preferred_clip_length(options.preferred_clip_length_seconds)


def build_section_scoring_config(
    section_context: dict[str, Any],
    options: ChunkingOptions,
) -> SectionScoringConfig:
    preferred = _preferred_clip_length_for_section(section_context, options)
    return SectionScoringConfig(
        candidate_cap_per_section=options.candidate_cap_per_section,
        preferred_clip_length_seconds=preferred,
    )


def chunk_transcript_context(
    source_context: dict[str, Any],
    options: dict[str, Any] | ChunkingOptions | None = None,
) -> list[dict[str, Any]]:
    resolved_options = apply_default_chunking_options(options)
    validate_chunking_options(resolved_options)
    duration_seconds = _positive_number(source_context.get("duration_seconds"), "duration_seconds")
    segments = source_context.get("segments")
    if isinstance(segments, list) and segments:
        return _chunk_from_segments(source_context, segments, duration_seconds, resolved_options)
    return [_fallback_section_context(source_context, duration_seconds)]


def build_section_clip_selection_prompt(
    base_prompt: str,
    section_context: dict[str, Any],
    candidate_cap_per_section: int,
) -> str:
    normalized = normalize_transcript_context(section_context)
    preferred = _preferred_clip_length_for_section(
        normalized,
        ChunkingOptions(candidate_cap_per_section=candidate_cap_per_section),
    )
    funnel_rules = json.dumps(normalized.get("funnel_rules") or {}, indent=2, sort_keys=True)
    return "\n\n".join(
        [
            base_prompt.strip(),
            "SECTION-LEVEL ANALYSIS ONLY:",
            "Evaluate only this bounded transcript section.",
            f"Return at most {candidate_cap_per_section} candidate(s) for this section.",
            f"Preferred clip length in seconds: {preferred[0]}-{preferred[1]}.",
            "A section may return usable=false with an empty candidates array.",
            "No clip is better than a bad forced clip.",
            "Return only JSON matching the schema.",
            "Funnel rules:",
            funnel_rules,
            build_prompt_safe_transcript_block(normalized),
        ]
    )


def _chunk_from_segments(
    source_context: dict[str, Any],
    segments: list[Any],
    duration_seconds: float,
    options: ChunkingOptions,
) -> list[dict[str, Any]]:
    normalized_segments = [_normalize_segment(segment) for segment in segments]
    normalized_segments = [segment for segment in normalized_segments if segment is not None]
    if not normalized_segments:
        return [_fallback_section_context(source_context, duration_seconds)]

    sections: list[dict[str, Any]] = []
    step = options.section_size_seconds - options.section_overlap_seconds
    section_start = 0.0
    while section_start < duration_seconds:
        section_end = min(section_start + options.section_size_seconds, duration_seconds)
        section_segments = [
            segment
            for segment in normalized_segments
            if segment["start"] < section_end and segment["end"] > section_start and segment["text"].strip()
        ]
        transcript = " ".join(segment["text"].strip() for segment in section_segments if segment["text"].strip())
        if transcript.strip():
            section = _base_section_context(
                source_context=source_context,
                duration_seconds=duration_seconds,
                section_start=section_start,
                section_end=section_end,
                transcript=transcript,
            )
            validation = validate_transcript_context(section)
            if validation.ok and validation.context is not None:
                sections.append(validation.context)

        if section_end >= duration_seconds:
            break
        section_start += step

    if sections:
        return sections
    return [_fallback_section_context(source_context, duration_seconds)]


def _fallback_section_context(source_context: dict[str, Any], duration_seconds: float) -> dict[str, Any]:
    section = _base_section_context(
        source_context=source_context,
        duration_seconds=duration_seconds,
        section_start=0.0,
        section_end=duration_seconds,
        transcript=str(source_context.get("transcript") or ""),
    )
    validation = validate_transcript_context(section)
    if not validation.ok or validation.context is None:
        raise TranscriptChunkingError(
            validation.error_code or "INVALID_TRANSCRIPT_CONTEXT",
            validation.error_message or "Transcript context is invalid.",
        )
    return validation.context


def _base_section_context(
    *,
    source_context: dict[str, Any],
    duration_seconds: float,
    section_start: float,
    section_end: float,
    transcript: str,
) -> dict[str, Any]:
    return {
        "job_id": source_context.get("job_id"),
        "video_title": source_context.get("video_title", ""),
        "source_channel": source_context.get("source_channel", ""),
        "funnel_id": source_context.get("funnel_id", ""),
        "duration_seconds": duration_seconds,
        "section_start": float(section_start),
        "section_end": float(section_end),
        "transcript": transcript,
        "speakers": deepcopy(source_context.get("speakers", [])),
        "previous_context_summary": source_context.get("previous_context_summary", ""),
        "funnel_rules": deepcopy(source_context.get("funnel_rules", {})),
    }


def _normalize_segment(segment: Any) -> dict[str, Any] | None:
    if not isinstance(segment, dict):
        return None
    start = segment.get("start")
    end = segment.get("end")
    text = segment.get("text")
    if not _is_number(start) or not _is_number(end) or float(end) <= float(start):
        return None
    if not isinstance(text, str):
        return None
    return {
        "start": float(start),
        "end": float(end),
        "text": text,
    }


def _preferred_clip_length_for_section(
    section_context: dict[str, Any],
    options: ChunkingOptions,
) -> list[float]:
    if options.preferred_clip_length_seconds is not None:
        return [float(options.preferred_clip_length_seconds[0]), float(options.preferred_clip_length_seconds[1])]
    funnel_rules = section_context.get("funnel_rules")
    if isinstance(funnel_rules, dict):
        preferred = funnel_rules.get("preferred_clip_length_seconds")
        if isinstance(preferred, list) and len(preferred) == 2 and _is_number(preferred[0]) and _is_number(preferred[1]):
            min_length = float(preferred[0])
            max_length = float(preferred[1])
            if min_length > 0 and max_length >= min_length:
                return [min_length, max_length]
    return [float(DEFAULT_PREFERRED_CLIP_LENGTH_SECONDS[0]), float(DEFAULT_PREFERRED_CLIP_LENGTH_SECONDS[1])]


def _validate_preferred_clip_length(value: Any) -> None:
    if not isinstance(value, list) or len(value) != 2:
        raise TranscriptChunkingError(
            "INVALID_CHUNKING_OPTIONS",
            "preferred_clip_length_seconds must be a two-number list [min, max].",
        )
    min_length, max_length = value
    if not _is_number(min_length) or not _is_number(max_length):
        raise TranscriptChunkingError("INVALID_CHUNKING_OPTIONS", "preferred_clip_length_seconds must contain numbers.")
    if float(min_length) <= 0:
        raise TranscriptChunkingError("INVALID_CHUNKING_OPTIONS", "preferred clip min must be > 0.")
    if float(max_length) < float(min_length):
        raise TranscriptChunkingError("INVALID_CHUNKING_OPTIONS", "preferred clip max must be >= min.")


def _positive_number(value: Any, field_name: str) -> float:
    if not _is_number(value) or float(value) <= 0:
        raise TranscriptChunkingError("INVALID_TRANSCRIPT_CONTEXT", f"{field_name} must be a positive number.")
    return float(value)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
