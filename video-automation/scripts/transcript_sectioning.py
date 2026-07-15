"""MK1 Transcript Presentation — deterministic fixed-partition sectioning.

This is the canonical MK1 stage between WhisperX output and Discovery. It turns
timestamped Whisper segments into bounded, inspectable sections using:

- fixed partition target duration (tunable default, not a proven optimum)
- fixed overlap between neighbouring sections (tunable default)
- Whisper segment-boundary snapping (never mid-segment cuts)
- deterministic processing with no AI partitioning

This module does not call any model, discover candidates, or affect rendering.
Sentence-boundary snapping is a possible future deterministic enhancement.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from numbers import Number
from typing import Any

from mk04_utils import now_iso, write_json

TRANSCRIPT_SECTIONS_SCHEMA_VERSION = "transcript_sections_v1"
TRANSCRIPT_SECTIONS_FILENAME = "transcript_sections.json"

# MK1 fixed-partition presentation strategy identifier (artifact metadata).
MK1_PRESENTATION_STRATEGY = "mk1_fixed_partition_v1"

# Tunable implementation defaults — chosen around the local model's usable context
# window (target) and intended max clip length (~120s, overlap). Not benchmarked.
DEFAULT_TARGET_SECTION_DURATION_SEC = 300.0
DEFAULT_MAX_SECTION_DURATION_SEC = 420.0
DEFAULT_OVERLAP_SEC = 60.0
DEFAULT_MIN_SECTION_DURATION_SEC = 60.0
SECTION_DURATION_TOLERANCE_SEC = 0.001

SECTION_REQUIRED_FIELDS = (
    "section_id",
    "start_sec",
    "end_sec",
    "duration_sec",
    "text",
    "source_transcript_path",
    "source_segment_refs",
    "overlap",
    "metadata",
)

OVERLAP_REQUIRED_FIELDS = (
    "has_previous_overlap",
    "has_next_overlap",
    "overlap_before_sec",
    "overlap_after_sec",
)

SECTION_REF_REQUIRED_FIELDS = ("segment_index", "start_sec", "end_sec")

TRANSCRIPT_SECTIONS_ARTIFACT_REQUIRED_FIELDS = (
    "schema_version",
    "job_id",
    "source_transcript_path",
    "created_at",
    "sectioning_config",
    "sections",
)


class TranscriptSectioningError(RuntimeError):
    """Raised for cleanly classified transcript sectioning failures."""

    def __init__(self, code: str, message: str, errors: list[str] | None = None):
        self.code = code
        self.message = message
        self.errors = list(errors or [])
        detail = f"{message}: {'; '.join(self.errors)}" if self.errors else message
        super().__init__(detail)


@dataclass(frozen=True)
class TranscriptSectioningConfig:
    """MK1 transcript presentation settings (fixed partition + fixed overlap)."""

    target_section_duration_sec: float = DEFAULT_TARGET_SECTION_DURATION_SEC
    max_section_duration_sec: float = DEFAULT_MAX_SECTION_DURATION_SEC
    overlap_sec: float = DEFAULT_OVERLAP_SEC
    min_section_duration_sec: float = DEFAULT_MIN_SECTION_DURATION_SEC

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class _TranscriptSegment:
    segment_index: int
    start_sec: float
    end_sec: float
    text: str


def default_sectioning_config() -> TranscriptSectioningConfig:
    return TranscriptSectioningConfig()


def apply_default_sectioning_config(
    config: dict[str, Any] | TranscriptSectioningConfig | None = None,
) -> TranscriptSectioningConfig:
    if config is None:
        resolved = TranscriptSectioningConfig()
    elif isinstance(config, TranscriptSectioningConfig):
        resolved = config
    elif isinstance(config, dict):
        resolved = TranscriptSectioningConfig(
            target_section_duration_sec=float(
                config.get(
                    "target_section_duration_sec",
                    DEFAULT_TARGET_SECTION_DURATION_SEC,
                )
            ),
            max_section_duration_sec=float(
                config.get(
                    "max_section_duration_sec",
                    DEFAULT_MAX_SECTION_DURATION_SEC,
                )
            ),
            overlap_sec=float(config.get("overlap_sec", DEFAULT_OVERLAP_SEC)),
            min_section_duration_sec=float(
                config.get(
                    "min_section_duration_sec",
                    DEFAULT_MIN_SECTION_DURATION_SEC,
                )
            ),
        )
    else:
        raise TranscriptSectioningError(
            "INVALID_SECTIONING_CONFIG",
            "sectioning config must be an object or TranscriptSectioningConfig.",
        )
    validate_sectioning_config(resolved)
    return resolved


def validate_sectioning_config(config: TranscriptSectioningConfig) -> None:
    errors: list[str] = []
    if not _is_number(config.target_section_duration_sec) or config.target_section_duration_sec <= 0:
        errors.append("target_section_duration_sec must be > 0")
    if not _is_number(config.max_section_duration_sec) or config.max_section_duration_sec <= 0:
        errors.append("max_section_duration_sec must be > 0")
    if (
        _is_number(config.target_section_duration_sec)
        and _is_number(config.max_section_duration_sec)
        and config.max_section_duration_sec < config.target_section_duration_sec
    ):
        errors.append("max_section_duration_sec must be >= target_section_duration_sec")
    if not _is_number(config.overlap_sec) or config.overlap_sec < 0:
        errors.append("overlap_sec must be >= 0")
    if (
        _is_number(config.overlap_sec)
        and _is_number(config.target_section_duration_sec)
        and config.overlap_sec >= config.target_section_duration_sec
    ):
        errors.append("overlap_sec must be less than target_section_duration_sec")
    if not _is_number(config.min_section_duration_sec) or config.min_section_duration_sec < 0:
        errors.append("min_section_duration_sec must be >= 0")
    if errors:
        raise TranscriptSectioningError(
            "INVALID_SECTIONING_CONFIG",
            "Transcript sectioning config is invalid.",
            errors,
        )


def make_section_id(section_index: int) -> str:
    if section_index < 1:
        raise TranscriptSectioningError(
            "INVALID_SECTION_INDEX", "section_index must be >= 1."
        )
    return f"section_{section_index:04d}"


def section_transcript_file(
    transcript_path: str,
    config: dict[str, Any] | TranscriptSectioningConfig | None = None,
) -> list[dict[str, Any]]:
    with open(transcript_path, "r", encoding="utf-8") as handle:
        transcript = json.load(handle)
    return section_transcript(
        transcript,
        source_transcript_path=os.path.abspath(transcript_path),
        config=config,
    )


def section_transcript(
    transcript: dict[str, Any],
    *,
    source_transcript_path: str = "",
    config: dict[str, Any] | TranscriptSectioningConfig | None = None,
) -> list[dict[str, Any]]:
    resolved_config = apply_default_sectioning_config(config)
    segments = _normalize_transcript_segments(transcript)
    sections = _build_sections(
        segments=segments,
        source_transcript_path=str(source_transcript_path or ""),
        config=resolved_config,
    )
    validate_transcript_sections(sections, config=resolved_config)
    return sections


def build_transcript_sections_artifact(
    *,
    job_id: str,
    source_transcript_path: str,
    sections: list[dict[str, Any]],
    sectioning_config: dict[str, Any] | TranscriptSectioningConfig | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    resolved_config = apply_default_sectioning_config(sectioning_config)
    payload: dict[str, Any] = {
        "schema_version": TRANSCRIPT_SECTIONS_SCHEMA_VERSION,
        "job_id": job_id,
        "source_transcript_path": source_transcript_path,
        "created_at": created_at or now_iso(),
        "presentation": {
            "strategy": MK1_PRESENTATION_STRATEGY,
            "section_count": len(sections),
            "source_segment_count": _source_segment_count_from_sections(sections),
            "partition_target_sec": resolved_config.target_section_duration_sec,
            "overlap_sec": resolved_config.overlap_sec,
        },
        "sectioning_config": resolved_config.as_dict(),
        "sections": list(sections),
    }
    validate_transcript_sections_artifact(payload)
    return payload


def transcript_sections_path(job_dir: str) -> str:
    return os.path.join(job_dir, TRANSCRIPT_SECTIONS_FILENAME)


def write_transcript_sections(job_dir: str, payload: dict[str, Any]) -> str:
    validate_transcript_sections_artifact(payload)
    path = transcript_sections_path(job_dir)
    write_json(path, payload)
    return path


def read_transcript_sections(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    validate_transcript_sections_artifact(payload)
    return payload


def validate_transcript_sections_artifact(payload: Any) -> None:
    errors: list[str] = []
    if not isinstance(payload, dict):
        raise TranscriptSectioningError(
            "INVALID_TRANSCRIPT_SECTIONS_ARTIFACT",
            "transcript sections artifact must be an object.",
        )
    _check_required_fields(
        payload,
        TRANSCRIPT_SECTIONS_ARTIFACT_REQUIRED_FIELDS,
        "root",
        errors,
    )
    if payload.get("schema_version") != TRANSCRIPT_SECTIONS_SCHEMA_VERSION:
        errors.append(
            f"root.schema_version must equal {TRANSCRIPT_SECTIONS_SCHEMA_VERSION!r}"
        )
    if not _is_non_empty_string(payload.get("job_id")):
        errors.append("root.job_id must be a non-empty string")
    if "source_transcript_path" in payload and not isinstance(
        payload.get("source_transcript_path"), str
    ):
        errors.append("root.source_transcript_path must be a string")
    if not _is_iso_timestamp(payload.get("created_at")):
        errors.append("root.created_at must be a non-empty ISO timestamp string")
    config: TranscriptSectioningConfig | None = None
    if "sectioning_config" in payload:
        try:
            config = apply_default_sectioning_config(payload.get("sectioning_config"))
        except TranscriptSectioningError as exc:
            errors.extend(f"root.sectioning_config.{err}" for err in exc.errors)
            if not exc.errors:
                errors.append(f"root.sectioning_config: {exc.message}")
    if "sections" in payload and not isinstance(payload.get("sections"), list):
        errors.append("root.sections must be a list")

    if not errors and isinstance(payload.get("sections"), list):
        try:
            validate_transcript_sections(payload["sections"], config=config)
        except TranscriptSectioningError as exc:
            errors.extend(exc.errors or [exc.message])

    if errors:
        raise TranscriptSectioningError(
            "INVALID_TRANSCRIPT_SECTIONS_ARTIFACT",
            "transcript sections artifact validation failed.",
            errors,
        )


def validate_transcript_sections(
    sections: Any,
    *,
    config: dict[str, Any] | TranscriptSectioningConfig | None = None,
) -> None:
    errors: list[str] = []
    if not isinstance(sections, list):
        raise TranscriptSectioningError(
            "INVALID_TRANSCRIPT_SECTIONS",
            "sections must be a list.",
        )
    resolved_config = apply_default_sectioning_config(config)
    previous_start: float | None = None
    for index, section in enumerate(sections):
        _validate_section(
            section,
            f"sections[{index}]",
            resolved_config=resolved_config,
            errors=errors,
        )
        if isinstance(section, dict) and _is_number(section.get("start_sec")):
            start_sec = float(section["start_sec"])
            if previous_start is not None and start_sec + SECTION_DURATION_TOLERANCE_SEC < previous_start:
                errors.append("sections must be ordered by start_sec")
            previous_start = start_sec

    if errors:
        raise TranscriptSectioningError(
            "INVALID_TRANSCRIPT_SECTIONS",
            "transcript section validation failed.",
            errors,
        )


def validate_transcript_section(
    section: Any,
    *,
    config: dict[str, Any] | TranscriptSectioningConfig | None = None,
) -> None:
    errors: list[str] = []
    _validate_section(
        section,
        "section",
        resolved_config=apply_default_sectioning_config(config),
        errors=errors,
    )
    if errors:
        raise TranscriptSectioningError(
            "INVALID_TRANSCRIPT_SECTION",
            "transcript section validation failed.",
            errors,
        )


def _normalize_transcript_segments(transcript: dict[str, Any]) -> list[_TranscriptSegment]:
    if not isinstance(transcript, dict):
        raise TranscriptSectioningError(
            "INVALID_TRANSCRIPT",
            "Transcript payload must be a JSON object.",
        )
    if "segments" not in transcript:
        raise TranscriptSectioningError(
            "MISSING_SEGMENTS",
            "Transcript payload must include a segments list.",
        )
    raw_segments = transcript.get("segments")
    if not isinstance(raw_segments, list):
        raise TranscriptSectioningError(
            "MISSING_SEGMENTS",
            "Transcript segments must be a list.",
        )
    if not raw_segments:
        raise TranscriptSectioningError(
            "EMPTY_TRANSCRIPT",
            "Transcript segments list is empty.",
        )

    segments: list[_TranscriptSegment] = []
    previous_start: float | None = None
    for index, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            raise TranscriptSectioningError(
                "INVALID_SEGMENT",
                f"Transcript segment {index} must be an object.",
            )
        start = raw.get("start")
        end = raw.get("end")
        if not _is_number(start) or not _is_number(end):
            raise TranscriptSectioningError(
                "INVALID_SEGMENT_TIMESTAMPS",
                f"Transcript segment {index} start/end must be numeric.",
            )
        start_sec = float(start)
        end_sec = float(end)
        if end_sec <= start_sec:
            raise TranscriptSectioningError(
                "INVALID_SEGMENT_TIMESTAMPS",
                f"Transcript segment {index} end must be greater than start.",
            )
        if previous_start is not None and start_sec + SECTION_DURATION_TOLERANCE_SEC < previous_start:
            raise TranscriptSectioningError(
                "INVALID_SEGMENT_ORDER",
                f"Transcript segment {index} starts before the previous segment.",
            )
        previous_start = start_sec

        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        segments.append(
            _TranscriptSegment(
                segment_index=index,
                start_sec=start_sec,
                end_sec=end_sec,
                text=" ".join(text.split()),
            )
        )

    if not segments:
        raise TranscriptSectioningError(
            "NO_USABLE_TEXT",
            "Transcript has no timestamped segments with usable text.",
        )
    return segments


def _source_segment_count_from_sections(sections: list[dict[str, Any]]) -> int:
    max_index = -1
    for section in sections:
        refs = section.get("source_segment_refs")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            segment_index = ref.get("segment_index")
            if isinstance(segment_index, int) and not isinstance(segment_index, bool):
                max_index = max(max_index, segment_index)
    return max_index + 1 if max_index >= 0 else 0


def _build_sections(
    *,
    segments: list[_TranscriptSegment],
    source_transcript_path: str,
    config: TranscriptSectioningConfig,
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    final_transcript_end = segments[-1].end_sec
    start_index = 0
    section_number = 1

    while start_index < len(segments):
        section_segments, next_index = _collect_section_segments(
            segments=segments,
            start_index=start_index,
            final_transcript_end=final_transcript_end,
            config=config,
        )
        if not section_segments:
            break

        section = _make_section(
            section_number=section_number,
            section_segments=section_segments,
            source_transcript_path=source_transcript_path,
            config=config,
        )
        sections.append(section)

        if next_index >= len(segments):
            break
        overlap_start_index = _overlap_start_index(
            section_segments=section_segments,
            section_end=section["end_sec"],
            overlap_sec=config.overlap_sec,
            fallback_next_index=next_index,
        )
        if overlap_start_index <= start_index:
            overlap_start_index = next_index
        start_index = overlap_start_index
        section_number += 1

    _apply_overlap_metadata(sections)
    return sections


def _collect_section_segments(
    *,
    segments: list[_TranscriptSegment],
    start_index: int,
    final_transcript_end: float,
    config: TranscriptSectioningConfig,
) -> tuple[list[_TranscriptSegment], int]:
    section_segments: list[_TranscriptSegment] = []
    section_start = segments[start_index].start_sec
    index = start_index
    exceeded_max_with_single_segment = False

    while index < len(segments):
        segment = segments[index]
        proposed_duration = segment.end_sec - section_start
        if section_segments and proposed_duration > config.max_section_duration_sec + SECTION_DURATION_TOLERANCE_SEC:
            break
        if not section_segments and proposed_duration > config.max_section_duration_sec + SECTION_DURATION_TOLERANCE_SEC:
            exceeded_max_with_single_segment = True
        section_segments.append(segment)
        index += 1
        current_duration = section_segments[-1].end_sec - section_start
        if current_duration >= config.target_section_duration_sec:
            if (
                index < len(segments)
                and final_transcript_end - section_start <= config.max_section_duration_sec
                and final_transcript_end - segments[index].start_sec < config.min_section_duration_sec
            ):
                section_segments.extend(segments[index:])
                index = len(segments)
            break
        if exceeded_max_with_single_segment:
            break

    return section_segments, index


def _make_section(
    *,
    section_number: int,
    section_segments: list[_TranscriptSegment],
    source_transcript_path: str,
    config: TranscriptSectioningConfig,
) -> dict[str, Any]:
    start_sec = section_segments[0].start_sec
    end_sec = section_segments[-1].end_sec
    duration_sec = end_sec - start_sec
    metadata: dict[str, Any] = {}
    if duration_sec > config.max_section_duration_sec + SECTION_DURATION_TOLERANCE_SEC:
        metadata["duration_exceeds_max_reason"] = "single_source_segment_exceeds_max"
    return {
        "section_id": make_section_id(section_number),
        "start_sec": start_sec,
        "end_sec": end_sec,
        "duration_sec": duration_sec,
        "text": _section_text(section_segments),
        "source_transcript_path": source_transcript_path,
        "source_segment_refs": [
            {
                "segment_index": segment.segment_index,
                "start_sec": segment.start_sec,
                "end_sec": segment.end_sec,
            }
            for segment in section_segments
        ],
        "overlap": {
            "has_previous_overlap": False,
            "has_next_overlap": False,
            "overlap_before_sec": 0.0,
            "overlap_after_sec": 0.0,
        },
        "metadata": metadata,
    }


def _section_text(section_segments: list[_TranscriptSegment]) -> str:
    return "\n".join(
        f"[{segment.start_sec:.3f} -> {segment.end_sec:.3f}] {segment.text}"
        for segment in section_segments
    )


def _overlap_start_index(
    *,
    section_segments: list[_TranscriptSegment],
    section_end: float,
    overlap_sec: float,
    fallback_next_index: int,
) -> int:
    if overlap_sec <= 0:
        return fallback_next_index
    threshold = section_end - overlap_sec
    for offset, segment in enumerate(section_segments):
        if segment.end_sec > threshold + SECTION_DURATION_TOLERANCE_SEC:
            return fallback_next_index - len(section_segments) + offset
    return fallback_next_index


def _apply_overlap_metadata(sections: list[dict[str, Any]]) -> None:
    for index, section in enumerate(sections):
        previous_section = sections[index - 1] if index > 0 else None
        next_section = sections[index + 1] if index + 1 < len(sections) else None

        overlap_before = 0.0
        if previous_section is not None:
            overlap_before = max(
                0.0,
                min(float(previous_section["end_sec"]), float(section["end_sec"]))
                - max(float(previous_section["start_sec"]), float(section["start_sec"])),
            )

        overlap_after = 0.0
        if next_section is not None:
            overlap_after = max(
                0.0,
                min(float(section["end_sec"]), float(next_section["end_sec"]))
                - max(float(section["start_sec"]), float(next_section["start_sec"])),
            )

        section["overlap"] = {
            "has_previous_overlap": overlap_before > SECTION_DURATION_TOLERANCE_SEC,
            "has_next_overlap": overlap_after > SECTION_DURATION_TOLERANCE_SEC,
            "overlap_before_sec": overlap_before,
            "overlap_after_sec": overlap_after,
        }


def _validate_section(
    section: Any,
    path: str,
    *,
    resolved_config: TranscriptSectioningConfig,
    errors: list[str],
) -> None:
    if not isinstance(section, dict):
        errors.append(f"{path} must be an object")
        return

    _check_required_fields(section, SECTION_REQUIRED_FIELDS, path, errors)
    if not _is_non_empty_string(section.get("section_id")):
        errors.append(f"{path}.section_id must be a non-empty string")

    start = section.get("start_sec")
    end = section.get("end_sec")
    duration = section.get("duration_sec")
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
        if abs(float(duration) - expected_duration) > SECTION_DURATION_TOLERANCE_SEC:
            errors.append(
                f"{path}.duration_sec must match end_sec - start_sec within "
                f"{SECTION_DURATION_TOLERANCE_SEC:g}s"
            )
        metadata = section.get("metadata") if isinstance(section.get("metadata"), dict) else {}
        if (
            float(duration) > resolved_config.max_section_duration_sec + SECTION_DURATION_TOLERANCE_SEC
            and not metadata.get("duration_exceeds_max_reason")
        ):
            errors.append(
                f"{path}.duration_sec exceeds max_section_duration_sec without documented reason"
            )

    if "text" in section and not isinstance(section.get("text"), str):
        errors.append(f"{path}.text must be a string")
    elif isinstance(section.get("text"), str) and not section["text"].strip():
        errors.append(f"{path}.text must be non-empty")
    if "source_transcript_path" in section and not isinstance(
        section.get("source_transcript_path"), str
    ):
        errors.append(f"{path}.source_transcript_path must be a string")
    if "source_segment_refs" in section:
        _validate_source_segment_refs(section.get("source_segment_refs"), path, errors)
    if "overlap" in section:
        _validate_overlap(section.get("overlap"), path, errors)
    if "metadata" in section and not isinstance(section.get("metadata"), dict):
        errors.append(f"{path}.metadata must be an object")


def _validate_source_segment_refs(refs: Any, path: str, errors: list[str]) -> None:
    if not isinstance(refs, list):
        errors.append(f"{path}.source_segment_refs must be a list")
        return
    for ref_index, ref in enumerate(refs):
        ref_path = f"{path}.source_segment_refs[{ref_index}]"
        if not isinstance(ref, dict):
            errors.append(f"{ref_path} must be an object")
            continue
        _check_required_fields(ref, SECTION_REF_REQUIRED_FIELDS, ref_path, errors)
        if "segment_index" in ref and (
            not isinstance(ref.get("segment_index"), int)
            or isinstance(ref.get("segment_index"), bool)
            or ref["segment_index"] < 0
        ):
            errors.append(f"{ref_path}.segment_index must be a non-negative integer")
        if "start_sec" in ref and not _is_number(ref.get("start_sec")):
            errors.append(f"{ref_path}.start_sec must be numeric")
        if "end_sec" in ref and not _is_number(ref.get("end_sec")):
            errors.append(f"{ref_path}.end_sec must be numeric")
        if _is_number(ref.get("start_sec")) and _is_number(ref.get("end_sec")) and float(ref["end_sec"]) <= float(ref["start_sec"]):
            errors.append(f"{ref_path}.end_sec must be greater than start_sec")


def _validate_overlap(overlap: Any, path: str, errors: list[str]) -> None:
    if not isinstance(overlap, dict):
        errors.append(f"{path}.overlap must be an object")
        return
    _check_required_fields(overlap, OVERLAP_REQUIRED_FIELDS, f"{path}.overlap", errors)
    for field in ("has_previous_overlap", "has_next_overlap"):
        if field in overlap and not isinstance(overlap.get(field), bool):
            errors.append(f"{path}.overlap.{field} must be a boolean")
    for field in ("overlap_before_sec", "overlap_after_sec"):
        if field in overlap and (
            not _is_number(overlap.get(field)) or float(overlap[field]) < 0
        ):
            errors.append(f"{path}.overlap.{field} must be a non-negative number")


def _check_required_fields(
    payload: dict[str, Any],
    required_fields: tuple[str, ...],
    path: str,
    errors: list[str],
) -> None:
    for field in required_fields:
        if field not in payload:
            errors.append(f"{path}.{field} is required")


def _is_number(value: Any) -> bool:
    return isinstance(value, Number) and not isinstance(value, bool) and math.isfinite(float(value))


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_iso_timestamp(value: Any) -> bool:
    if not _is_non_empty_string(value):
        return False
    try:
        datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True
