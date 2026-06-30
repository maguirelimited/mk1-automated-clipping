"""Build lightweight processing diagnostics from section discovery results."""

from __future__ import annotations

from typing import Any

from processing_contracts import PROCESSING_VERSION, build_processing_report


def build_processing_diagnostics_report(
    *,
    job_id: str,
    discovery_batch: dict[str, Any],
    funnel_id: str | None = None,
    transcript_warnings: list[Any] | None = None,
    processing_warnings: list[Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    section_results = _list_of_dicts(discovery_batch.get("section_results"))
    failed_sections = _list_of_dicts(discovery_batch.get("failed_sections"))
    rejected_candidates = _list_of_dicts(discovery_batch.get("rejected_candidates"))
    duplicate_removals = _list_of_dicts(discovery_batch.get("duplicate_removals"))

    final_candidate_count = sum(
        len(result.get("candidates") or [])
        for result in section_results
        if isinstance(result.get("candidates"), list)
    )
    duplicates_removed = _non_negative_int_or_len(
        discovery_batch.get("duplicates_removed"), duplicate_removals
    )
    candidates_rejected_by_boundary = len(rejected_candidates)
    candidates_discovered = (
        final_candidate_count + duplicates_removed + candidates_rejected_by_boundary
    )

    report_processing_warnings = [
        *_string_list(discovery_batch.get("warnings")),
        *_string_list(processing_warnings),
    ]

    return build_processing_report(
        job_id=job_id,
        processing_version=PROCESSING_VERSION,
        funnel_id=_resolve_funnel_id(funnel_id, section_results),
        sections_analysed=_non_negative_int_or_default(
            discovery_batch.get("sections_received"),
            len(section_results) + len(failed_sections),
        ),
        usable_sections=sum(1 for result in section_results if result.get("usable") is True),
        rejected_sections=sum(1 for result in section_results if result.get("usable") is False),
        failed_sections=failed_sections,
        candidates_discovered=candidates_discovered,
        candidates_rejected_by_boundary=candidates_rejected_by_boundary,
        duplicates_removed=duplicates_removed,
        final_candidate_count=final_candidate_count,
        transcript_warnings=list(transcript_warnings or discovery_batch.get("transcript_warnings") or []),
        processing_warnings=report_processing_warnings,
        common_rejection_reasons=_common_rejection_reasons(
            rejected_candidates=rejected_candidates,
            failed_sections=failed_sections,
        ),
        prompt_metadata=_prompt_metadata(section_results),
        created_at=created_at,
    )


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _non_negative_int_or_len(value: Any, fallback_list: list[Any]) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else len(fallback_list)


def _non_negative_int_or_default(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def _common_rejection_reasons(
    *,
    rejected_candidates: list[dict[str, Any]],
    failed_sections: list[dict[str, Any]],
) -> list[str]:
    reasons: list[str] = []
    for candidate in rejected_candidates:
        raw = candidate.get("rejection_reasons")
        if isinstance(raw, list):
            reasons.extend(item for item in raw if isinstance(item, str))
    for failure in failed_sections:
        for key in ("error_code", "error_reason", "reason"):
            raw = failure.get(key)
            if isinstance(raw, str) and raw.strip():
                reasons.append(raw.strip())
                break
    return _dedupe(reasons)


def _prompt_metadata(section_results: list[dict[str, Any]]) -> dict[str, Any]:
    metadata_items = [
        item.get("prompt_metadata")
        for item in section_results
        if isinstance(item.get("prompt_metadata"), dict)
    ]
    if not metadata_items:
        return {}
    first = dict(metadata_items[0])
    if all(item == first for item in metadata_items):
        return first
    return {"section_results": [dict(item) for item in metadata_items]}


def _resolve_funnel_id(
    explicit_funnel_id: str | None,
    section_results: list[dict[str, Any]],
) -> str | None:
    if isinstance(explicit_funnel_id, str) and explicit_funnel_id.strip():
        return explicit_funnel_id.strip()
    metadata = _prompt_metadata(section_results)
    resolved = metadata.get("resolved_funnel_id")
    if isinstance(resolved, str) and resolved.strip():
        return resolved.strip()
    return None


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
