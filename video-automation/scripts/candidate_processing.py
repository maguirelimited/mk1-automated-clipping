"""MK1 Candidate Processing — deterministic cleanup between Discovery and Evaluation.

This stage owns candidate-level processing after recall-oriented Discovery and
before raw candidate pool assembly. It does not call AI, rank final clips, or
decide what gets rendered.

Current processors:
- boundary sanity (`candidate_boundary_sanity.py`)
- timestamp overlap / dedupe control (`candidate_overlap_control.py`)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from candidate_boundary_sanity import (
    BoundarySanityConfig,
    apply_boundary_sanity,
    rejected_candidate_record,
)
from candidate_overlap_control import (
    CandidateOverlapControlError,
    control_candidate_overlap,
)
from mk04_utils import now_iso, write_json
from section_candidate_discovery import (
    CandidateDiscoveryConfig,
    SECTION_DISCOVERY_BATCH_SCHEMA_VERSION,
    apply_default_discovery_config,
    validate_section_discovery_batch,
)

CANDIDATE_PROCESSING_SCHEMA_VERSION = "candidate_processing_v1"
CANDIDATE_PROCESSING_FILENAME = "candidate_processing.json"
MK1_CANDIDATE_PROCESSING_STRATEGY = "mk1_candidate_processing_v1"

ARTIFACT_REQUIRED_FIELDS = (
    "schema_version",
    "job_id",
    "source_section_candidate_discovery_path",
    "created_at",
    "processing",
    "sections_received",
    "sections_processed",
    "usable_sections",
    "rejected_sections",
    "candidates_discovered",
    "duplicates_removed",
    "section_results",
    "rejected_candidates",
    "duplicate_removals",
    "warnings",
    "failed_sections",
)


class CandidateProcessingError(RuntimeError):
    """Raised for cleanly classified candidate processing failures."""

    def __init__(self, code: str, message: str, errors: list[str] | None = None):
        self.code = code
        self.message = message
        self.errors = list(errors or [])
        detail = f"{message}: {'; '.join(self.errors)}" if self.errors else message
        super().__init__(detail)


@dataclass(frozen=True)
class CandidateProcessingSummary:
    strategy: str
    input_candidate_count: int
    output_candidate_count: int
    candidates_rejected_by_boundary: int
    duplicates_removed: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def candidate_processing_path(job_dir: str) -> str:
    return os.path.join(job_dir, CANDIDATE_PROCESSING_FILENAME)


def run_candidate_processing(
    discovery_batch: dict[str, Any],
    sections: list[dict[str, Any]],
    *,
    config: dict[str, Any] | CandidateDiscoveryConfig | None = None,
) -> dict[str, Any]:
    """Apply deterministic candidate processing to a Discovery batch.

    Returns a processed batch with the same shape as the post-Discovery batch
    used for pool assembly (boundary sanity + overlap/dedupe applied).
    """
    resolved_config = apply_default_discovery_config(config)
    if not isinstance(discovery_batch, dict):
        raise CandidateProcessingError(
            "INVALID_DISCOVERY_BATCH",
            "discovery batch must be an object.",
        )

    section_by_id = {
        str(section.get("section_id")): section
        for section in sections
        if isinstance(section, dict) and section.get("section_id")
    }
    input_candidate_count = _count_candidates(discovery_batch)

    batch = dict(discovery_batch)
    processed_section_results: list[dict[str, Any]] = []
    rejected_candidates: list[dict[str, Any]] = []

    for result in batch.get("section_results") or []:
        if not isinstance(result, dict):
            processed_section_results.append(result)
            continue
        section_id = str(result.get("section_id") or "")
        section = section_by_id.get(section_id)
        if section is None:
            processed_section_results.append(result)
            continue

        processed = _apply_boundary_sanity_to_result(
            result,
            section=section,
            config=resolved_config,
        )
        if resolved_config.fail_fast and processed.get("rejected_candidates"):
            raise CandidateProcessingError(
                "BOUNDARY_SANITY_FAILED",
                "Candidate failed boundary sanity.",
            )
        processed_section_results.append(processed)
        rejected_candidates.extend(
            item
            for item in (processed.get("rejected_candidates") or [])
            if isinstance(item, dict)
        )

    batch["section_results"] = processed_section_results
    batch["rejected_candidates"] = rejected_candidates
    batch = _apply_overlap_control_to_batch(batch)
    validate_section_discovery_batch(batch)

    output_candidate_count = _count_candidates(batch)
    batch["_candidate_processing_summary"] = CandidateProcessingSummary(
        strategy=MK1_CANDIDATE_PROCESSING_STRATEGY,
        input_candidate_count=input_candidate_count,
        output_candidate_count=output_candidate_count,
        candidates_rejected_by_boundary=len(rejected_candidates),
        duplicates_removed=int(batch.get("duplicates_removed") or 0),
    ).as_dict()
    return batch


def build_candidate_processing_artifact(
    *,
    job_id: str,
    source_section_candidate_discovery_path: str,
    processed_batch: dict[str, Any],
    created_at: str | None = None,
) -> dict[str, Any]:
    summary = processed_batch.get("_candidate_processing_summary")
    if not isinstance(summary, dict):
        summary = {
            "strategy": MK1_CANDIDATE_PROCESSING_STRATEGY,
            "input_candidate_count": _count_candidates(processed_batch),
            "output_candidate_count": _count_candidates(processed_batch),
            "candidates_rejected_by_boundary": len(
                processed_batch.get("rejected_candidates") or []
            ),
            "duplicates_removed": int(processed_batch.get("duplicates_removed") or 0),
        }

    payload: dict[str, Any] = {
        "schema_version": CANDIDATE_PROCESSING_SCHEMA_VERSION,
        "job_id": job_id,
        "source_section_candidate_discovery_path": source_section_candidate_discovery_path,
        "created_at": created_at or now_iso(),
        "processing": dict(summary),
        "sections_received": processed_batch.get("sections_received"),
        "sections_processed": processed_batch.get("sections_processed"),
        "usable_sections": processed_batch.get("usable_sections"),
        "rejected_sections": processed_batch.get("rejected_sections"),
        "candidates_discovered": processed_batch.get("candidates_discovered"),
        "duplicates_removed": processed_batch.get("duplicates_removed"),
        "section_results": list(processed_batch.get("section_results") or []),
        "rejected_candidates": list(processed_batch.get("rejected_candidates") or []),
        "duplicate_removals": list(processed_batch.get("duplicate_removals") or []),
        "warnings": list(processed_batch.get("warnings") or []),
        "failed_sections": list(processed_batch.get("failed_sections") or []),
    }
    validate_candidate_processing_artifact(payload)
    return payload


def write_candidate_processing(job_dir: str, payload: dict[str, Any]) -> str:
    validate_candidate_processing_artifact(payload)
    path = candidate_processing_path(job_dir)
    write_json(path, payload)
    return path


def read_candidate_processing(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    validate_candidate_processing_artifact(payload)
    return payload


def validate_candidate_processing_artifact(payload: Any) -> None:
    errors: list[str] = []
    if not isinstance(payload, dict):
        raise CandidateProcessingError(
            "INVALID_CANDIDATE_PROCESSING_ARTIFACT",
            "candidate processing artifact must be an object.",
        )
    _check_required_fields(payload, ARTIFACT_REQUIRED_FIELDS, "artifact", errors)
    if payload.get("schema_version") != CANDIDATE_PROCESSING_SCHEMA_VERSION:
        errors.append(
            f"artifact.schema_version must equal {CANDIDATE_PROCESSING_SCHEMA_VERSION!r}"
        )
    if not _is_non_empty_string(payload.get("job_id")):
        errors.append("artifact.job_id must be a non-empty string")
    if not _is_iso_timestamp(payload.get("created_at")):
        errors.append("artifact.created_at must be a non-empty ISO timestamp string")
    processing = payload.get("processing")
    if not isinstance(processing, dict):
        errors.append("artifact.processing must be an object")
    elif processing.get("strategy") != MK1_CANDIDATE_PROCESSING_STRATEGY:
        errors.append(
            f"artifact.processing.strategy must equal {MK1_CANDIDATE_PROCESSING_STRATEGY!r}"
        )
    if not errors:
        try:
            validate_section_discovery_batch(
                {
                    "schema_version": SECTION_DISCOVERY_BATCH_SCHEMA_VERSION,
                    "sections_received": payload.get("sections_received"),
                    "sections_processed": payload.get("sections_processed"),
                    "usable_sections": payload.get("usable_sections"),
                    "rejected_sections": payload.get("rejected_sections"),
                    "candidates_discovered": payload.get("candidates_discovered"),
                    "duplicates_removed": payload.get("duplicates_removed"),
                    "section_results": payload.get("section_results"),
                    "rejected_candidates": payload.get("rejected_candidates"),
                    "duplicate_removals": payload.get("duplicate_removals"),
                    "warnings": payload.get("warnings"),
                    "failed_sections": payload.get("failed_sections"),
                }
            )
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise CandidateProcessingError(
            "INVALID_CANDIDATE_PROCESSING_ARTIFACT",
            "candidate processing artifact validation failed.",
            errors,
        )


def _apply_boundary_sanity_to_result(
    result: dict[str, Any],
    *,
    section: dict[str, Any],
    config: CandidateDiscoveryConfig,
) -> dict[str, Any]:
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        return result

    boundary_config = BoundarySanityConfig(
        min_candidate_duration_sec=config.min_candidate_duration_sec,
        max_candidate_duration_sec=config.max_candidate_duration_sec,
        transcript_start_sec=config.transcript_start_sec,
        transcript_end_sec=config.transcript_end_sec,
        video_duration_sec=config.video_duration_sec,
    )
    accepted_candidates: list[dict[str, Any]] = []
    rejected_candidates: list[dict[str, Any]] = []

    for candidate in candidates:
        if not isinstance(candidate, dict):
            rejected_candidates.append(
                {
                    "source_section_id": str(section.get("section_id") or ""),
                    "candidate_local_id": "",
                    "start_sec": None,
                    "end_sec": None,
                    "rejection_reasons": ["invalid_timestamp"],
                }
            )
            continue
        sanity = apply_boundary_sanity(candidate, section, boundary_config)
        if sanity.accepted:
            accepted_candidates.append(sanity.candidate)
        else:
            rejected_candidates.append(rejected_candidate_record(candidate, sanity))

    out = dict(result)
    out["candidates"] = accepted_candidates
    if rejected_candidates:
        out["rejected_candidates"] = [
            *(item for item in (result.get("rejected_candidates") or []) if isinstance(item, dict)),
            *rejected_candidates,
        ]
        if result.get("usable") is True and not accepted_candidates:
            out["usable"] = False
            out["warnings"] = _merge_string_warnings(
                out.get("warnings"),
                ["all_candidates_rejected_by_boundary_sanity"],
            )
    else:
        out.setdefault("rejected_candidates", [])
    return out


def _apply_overlap_control_to_batch(batch: dict[str, Any]) -> dict[str, Any]:
    section_results = batch.get("section_results")
    if not isinstance(section_results, list):
        return batch

    accepted_candidates: list[dict[str, Any]] = []
    for result in section_results:
        if not isinstance(result, dict):
            continue
        for candidate in result.get("candidates") or []:
            if isinstance(candidate, dict):
                accepted_candidates.append(candidate)

    try:
        overlap_result = control_candidate_overlap(accepted_candidates)
    except CandidateOverlapControlError as exc:
        raise CandidateProcessingError(
            "OVERLAP_CONTROL_FAILED",
            f"Candidate overlap control failed: {exc}",
        ) from exc

    kept_ids = {_candidate_identifier(candidate) for candidate in overlap_result.kept_candidates}
    next_section_results: list[dict[str, Any]] = []
    for result in section_results:
        if not isinstance(result, dict):
            next_section_results.append(result)
            continue
        original_candidates = [
            candidate for candidate in (result.get("candidates") or []) if isinstance(candidate, dict)
        ]
        kept_candidates = [
            candidate
            for candidate in original_candidates
            if _candidate_identifier(candidate) in kept_ids
        ]
        updated = dict(result)
        updated["candidates"] = kept_candidates
        if result.get("usable") is True and original_candidates and not kept_candidates:
            updated["usable"] = False
            updated["warnings"] = _merge_string_warnings(
                updated.get("warnings"),
                ["all_candidates_removed_as_timestamp_duplicates"],
            )
        next_section_results.append(updated)

    out = dict(batch)
    out["section_results"] = next_section_results
    out["duplicate_removals"] = [dict(item) for item in overlap_result.duplicate_removals]
    out["duplicates_removed"] = len(overlap_result.duplicate_removals)
    out["usable_sections"] = sum(
        1 for result in next_section_results if isinstance(result, dict) and result.get("usable") is True
    )
    out["rejected_sections"] = sum(
        1 for result in next_section_results if isinstance(result, dict) and result.get("usable") is False
    )
    out["candidates_discovered"] = sum(
        len(result.get("candidates") or [])
        for result in next_section_results
        if isinstance(result, dict)
    )
    return out


def _count_candidates(batch: dict[str, Any]) -> int:
    return sum(
        len(result.get("candidates") or [])
        for result in (batch.get("section_results") or [])
        if isinstance(result, dict) and isinstance(result.get("candidates"), list)
    )


def _candidate_identifier(candidate: dict[str, Any]) -> str:
    return str(candidate.get("candidate_id") or candidate.get("candidate_local_id") or "")


def _merge_string_warnings(existing: Any, additions: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in list(existing or []) + list(additions):
        if isinstance(item, str) and item and item not in seen:
            seen.add(item)
            merged.append(item)
    return merged


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


def _is_iso_timestamp(value: Any) -> bool:
    if not _is_non_empty_string(value):
        return False
    try:
        datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True
