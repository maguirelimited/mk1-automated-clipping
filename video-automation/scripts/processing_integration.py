"""Wire processing outputs into the video-automation job structure.

This module saves the raw candidate pool and processing report that result from
section candidate discovery, and provides path-linking helpers so the main job
report can reference both artifacts.

It also contains a single temporary legacy adapter that converts raw candidate
pool output into the segment format expected by the existing render path.  That
adapter is explicitly marked as TEMPORARY and must not become a new selection
system.
"""

from __future__ import annotations

from typing import Any

from mk04_utils import now_iso
from processing_contracts import (
    PROCESSING_VERSION,
    RAW_CANDIDATE_POOL_SCHEMA_VERSION,
    build_raw_candidate_pool,
    make_candidate_id,
    write_processing_report,
    write_raw_candidate_pool,
)
from processing_diagnostics import build_processing_diagnostics_report


class ProcessingIntegrationError(RuntimeError):
    """Raised when processing artifacts cannot be written or validated."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def build_pool_candidate_from_discovery(
    candidate: dict[str, Any],
    job_id: str,
    *,
    candidate_index: int | None = None,
) -> dict[str, Any]:
    """Convert a section_candidate_discovery candidate into a raw pool candidate.

    Field mapping is direct.  Missing evidence text fields default to empty
    strings; missing archetype defaults to ``other``.  These defaults preserve
    a valid pool entry without inventing new evidence.
    """
    source_section_id = str(candidate.get("source_section_id") or "")
    start_sec = float(candidate["start_sec"])
    end_sec = float(candidate["end_sec"])
    candidate_id = make_candidate_id(
        job_id=job_id,
        source_section_id=source_section_id,
        start_sec=start_sec,
        end_sec=end_sec,
        candidate_index=candidate_index,
    )
    return {
        "candidate_id": candidate_id,
        "source_section_id": source_section_id,
        "start_sec": start_sec,
        "end_sec": end_sec,
        "duration_sec": float(candidate["duration_sec"]),
        "hook_text": str(candidate.get("hook_text") or ""),
        "core_idea_summary": str(candidate.get("core_idea_summary") or ""),
        "why_candidate_has_potential": str(
            candidate.get("why_candidate_has_potential") or ""
        ),
        "archetype": str(candidate.get("archetype") or "other"),
        "confidence": float(candidate.get("confidence") or 0.0),
        "scores": dict(candidate.get("scores") or {}),
        "warnings": list(candidate.get("warnings") or []),
        "transcript_quality_flags": list(
            candidate.get("transcript_quality_flags") or []
        ),
    }


def collect_candidates_from_batch(
    batch: dict[str, Any],
    job_id: str,
) -> list[dict[str, Any]]:
    """Collect all accepted candidates from a section discovery batch.

    Iterates over ``section_results`` and converts each candidate using
    :func:`build_pool_candidate_from_discovery`.  Sections with
    ``usable=False`` contribute no candidates (correct: zero-candidate sections
    are preserved as rejected counts in the report, not forced into the pool).
    """
    section_results = batch.get("section_results")
    if not isinstance(section_results, list):
        return []
    pool_candidates: list[dict[str, Any]] = []
    for result in section_results:
        if not isinstance(result, dict):
            continue
        candidates = result.get("candidates")
        if not isinstance(candidates, list):
            continue
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                continue
            try:
                pool_candidate = build_pool_candidate_from_discovery(
                    candidate, job_id, candidate_index=index
                )
                pool_candidates.append(pool_candidate)
            except (KeyError, TypeError, ValueError):
                continue
    return pool_candidates


def build_processing_artifacts(
    *,
    job_id: str,
    job_dir: str,
    discovery_batch: dict[str, Any],
    source_video_path: str,
    transcript_path: str,
    funnel_id: str | None = None,
    transcript_warnings: list[Any] | None = None,
    processing_warnings: list[Any] | None = None,
    created_at: str | None = None,
) -> tuple[str, str]:
    """Build and write both processing artifacts to ``job_dir``.

    Returns ``(raw_candidate_pool_path, processing_report_path)``.

    Raises :class:`ProcessingIntegrationError` if either artifact cannot be
    written or validated.  This is a deliberate hard failure: missing processing
    artifacts are not silently skipped.
    """
    ts = created_at or now_iso()

    # Collect candidates (empty list is valid — zero-candidate pools are allowed)
    try:
        candidates = collect_candidates_from_batch(discovery_batch, job_id)
    except Exception as exc:
        raise ProcessingIntegrationError(
            "CANDIDATE_COLLECTION_FAILED",
            f"Failed to collect candidates from discovery batch: {exc}",
        ) from exc

    # Build lightweight diagnostics summary for the pool payload
    diagnostics = _build_pool_diagnostics_summary(discovery_batch)

    # Build and write raw_candidate_pool.json
    try:
        pool = build_raw_candidate_pool(
            job_id=job_id,
            source_video_path=source_video_path,
            transcript_path=transcript_path,
            funnel_id=funnel_id or "",
            candidates=candidates,
            diagnostics=diagnostics,
            processing_version=PROCESSING_VERSION,
            created_at=ts,
        )
        pool_path = write_raw_candidate_pool(job_dir, pool)
    except ProcessingIntegrationError:
        raise
    except Exception as exc:
        raise ProcessingIntegrationError(
            "RAW_CANDIDATE_POOL_WRITE_FAILED",
            f"Could not write raw_candidate_pool.json: {exc}",
        ) from exc

    # Build and write processing_report.json
    try:
        report = build_processing_diagnostics_report(
            job_id=job_id,
            discovery_batch=discovery_batch,
            funnel_id=funnel_id,
            transcript_warnings=transcript_warnings,
            processing_warnings=processing_warnings,
            created_at=ts,
        )
        report_path = write_processing_report(job_dir, report)
    except ProcessingIntegrationError:
        raise
    except Exception as exc:
        raise ProcessingIntegrationError(
            "PROCESSING_REPORT_WRITE_FAILED",
            f"Could not write processing_report.json: {exc}",
        ) from exc

    return pool_path, report_path


def link_processing_artifacts_in_report(
    report: dict[str, Any],
    *,
    raw_candidate_pool_path: str,
    processing_report_path: str,
) -> None:
    """Write processing artifact paths into an existing job report dict.

    Mutates ``report`` in-place.  Call this after
    :func:`build_processing_artifacts` succeeds and before the report is
    persisted to disk.
    """
    report["raw_candidate_pool_path"] = raw_candidate_pool_path
    report["processing_report_path"] = processing_report_path


# ---------------------------------------------------------------------------
# TEMPORARY — Legacy compatibility adapter
# ---------------------------------------------------------------------------
#
# The existing render path (select_clip.py / postprocess_segments) expects a
# list of dicts with ``start`` and ``end`` keys (float seconds).  Until final
# selection is implemented as its own post-processing stage, this adapter
# provides the minimal mapping needed to avoid breaking the current path.
#
# Rules:
#   - Do NOT change ranking strategy: order follows the pool order (which
#     preserves the section-discovery ordering).
#   - Do NOT add new ranking logic beyond the simplest score projection.
#   - Do NOT use this adapter as the final selection system.
#   - Mark any future replacement clearly and delete this function once a
#     proper post-processing final-selection stage is in place.
# ---------------------------------------------------------------------------

_LEGACY_ADAPTER_NOTE = (
    "TEMPORARY_LEGACY_ADAPTER: converts raw_candidate_pool to legacy segment format. "
    "This must be replaced by a proper post-processing final-selection stage."
)


def legacy_segments_from_raw_candidate_pool(
    pool: dict[str, Any],
) -> list[dict[str, Any]]:
    """TEMPORARY: Map raw candidate pool candidates onto the legacy segment format.

    Produces ``[{start: float, end: float, ...}]`` dicts accepted by
    ``postprocess_segments`` in ``pipeline_utils``.  Preserves pool ordering
    (no new ranking strategy introduced).

    This adapter is explicitly temporary and must not become the final
    selection system.
    """
    candidates = pool.get("candidates") if isinstance(pool, dict) else None
    if not isinstance(candidates, list):
        return []

    segments: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        start = candidate.get("start_sec")
        end = candidate.get("end_sec")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        if float(end) <= float(start):
            continue

        segment: dict[str, Any] = {
            "start": float(start),
            "end": float(end),
            "_legacy_adapter": _LEGACY_ADAPTER_NOTE,
        }

        # Project overall_potential onto the legacy score dimensions so that
        # postprocess_segments can rank candidates by the processing judgement.
        scores = candidate.get("scores")
        if isinstance(scores, dict):
            raw_overall = scores.get("overall_potential")
            if isinstance(raw_overall, (int, float)) and not isinstance(raw_overall, bool):
                score_f = max(0.0, min(10.0, float(raw_overall)))
                segment["score"] = score_f
                segment["scores"] = {
                    "hook_strength": score_f,
                    "clarity_standalone": score_f,
                    "engagement_potential": score_f,
                    "minimal_filler": score_f,
                }

        # Use hook_text or core_idea_summary as the legacy reason field.
        hook = candidate.get("hook_text")
        summary = candidate.get("core_idea_summary")
        reason = hook if isinstance(hook, str) and hook.strip() else (
            summary if isinstance(summary, str) and summary.strip() else None
        )
        if reason:
            segment["reason"] = reason.strip()

        segments.append(segment)

    return segments


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_pool_diagnostics_summary(batch: dict[str, Any]) -> dict[str, Any]:
    """Return a lightweight summary dict for the raw_candidate_pool diagnostics field."""
    return {
        "schema_version": RAW_CANDIDATE_POOL_SCHEMA_VERSION,
        "processing_version": PROCESSING_VERSION,
        "sections_received": _safe_int(batch.get("sections_received")),
        "sections_processed": _safe_int(batch.get("sections_processed")),
        "usable_sections": _safe_int(batch.get("usable_sections")),
        "rejected_sections": _safe_int(batch.get("rejected_sections")),
        "candidates_discovered": _safe_int(batch.get("candidates_discovered")),
        "duplicates_removed": _safe_int(batch.get("duplicates_removed")),
        "failed_sections_count": len(
            [s for s in (batch.get("failed_sections") or []) if isinstance(s, dict)]
        ),
    }


def _safe_int(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0
