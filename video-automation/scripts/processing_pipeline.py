"""Processing-only pipeline runner.

Wires transcript sectioning → section candidate discovery → processing artifact
writing into a single callable pipeline.  This is the integration point for the
processing phase as a standalone job — it does not render clips, register output
funnels, or perform post-processing.

Usage from a job flow:
    result = run_processing_pipeline(
        job_id="...",
        job_dir="...",
        transcript=transcript_payload,
        transcript_path="/path/to/transcript.json",
        source_video_path="/path/to/source.mp4",
        funnel_id="business",
        ai_client=AiServiceSectionDiscoveryClient(...),
    )
    link_processing_artifacts_in_report(
        job_report,
        raw_candidate_pool_path=result.raw_candidate_pool_path,
        processing_report_path=result.processing_report_path,
    )

This module deliberately does NOT:
- render finished clips
- register with the output funnel
- run post-processing
- perform final clip selection
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mk04_utils import now_iso
from processing_integration import (
    ProcessingIntegrationError,
    build_processing_artifacts,
    link_processing_artifacts_in_report,
)
from section_candidate_discovery import (
    CandidateDiscoveryConfig,
    apply_default_discovery_config,
    discover_candidates_for_sections,
)
from transcript_sectioning import (
    TranscriptSectioningConfig,
    apply_default_sectioning_config,
    section_transcript,
)


class ProcessingPipelineError(RuntimeError):
    """Raised for cleanly classified processing pipeline failures."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ProcessingPipelineResult:
    """Immutable summary of a completed processing pipeline run."""

    job_id: str
    raw_candidate_pool_path: str
    processing_report_path: str
    sections_analysed: int
    usable_sections: int
    rejected_sections: int
    failed_sections_count: int
    final_candidate_count: int
    duplicates_removed: int
    candidates_rejected_by_boundary: int


def run_processing_pipeline(
    *,
    job_id: str,
    job_dir: str,
    transcript: dict[str, Any],
    transcript_path: str,
    source_video_path: str,
    funnel_id: str | None = None,
    ai_client: Any | None = None,
    discovery_config: dict[str, Any] | CandidateDiscoveryConfig | None = None,
    sectioning_config: dict[str, Any] | TranscriptSectioningConfig | None = None,
    transcript_warnings: list[Any] | None = None,
    processing_warnings: list[Any] | None = None,
    created_at: str | None = None,
) -> ProcessingPipelineResult:
    """Run the full processing pipeline for a single job.

    Steps:
    1. Section the transcript (deterministic, no AI)
    2. Discover section candidates (AI required — provide ``ai_client``)
    3. Write ``raw_candidate_pool.json`` and ``processing_report.json``
    4. Return a :class:`ProcessingPipelineResult` with paths and counts

    Raises :class:`ProcessingPipelineError` on any stage failure.
    Raises :class:`ProcessingIntegrationError` if artifact writing fails.
    """
    ts = created_at or now_iso()

    # Step 1: section the transcript (deterministic)
    try:
        resolved_sectioning = apply_default_sectioning_config(sectioning_config)
        sections = section_transcript(
            transcript,
            source_transcript_path=transcript_path,
            config=resolved_sectioning,
        )
    except Exception as exc:
        raise ProcessingPipelineError(
            "TRANSCRIPT_SECTIONING_FAILED",
            f"Transcript sectioning failed: {exc}",
        ) from exc

    if not sections:
        raise ProcessingPipelineError(
            "NO_SECTIONS_PRODUCED",
            "Transcript sectioning produced no sections.",
        )

    # Step 2: section candidate discovery (AI-backed)
    try:
        resolved_discovery = apply_default_discovery_config(discovery_config)
        batch = discover_candidates_for_sections(
            sections,
            ai_client=ai_client,
            config=resolved_discovery,
            funnel_id=funnel_id,
        )
    except ProcessingPipelineError:
        raise
    except Exception as exc:
        raise ProcessingPipelineError(
            "SECTION_DISCOVERY_FAILED",
            f"Section candidate discovery failed: {exc}",
        ) from exc

    # Step 3: write processing artifacts
    pool_path, report_path = build_processing_artifacts(
        job_id=job_id,
        job_dir=job_dir,
        discovery_batch=batch,
        source_video_path=source_video_path,
        transcript_path=transcript_path,
        funnel_id=funnel_id,
        transcript_warnings=transcript_warnings,
        processing_warnings=processing_warnings,
        created_at=ts,
    )

    # Step 4: read back the written artifacts to derive counts from the ground truth
    pool = json.loads(Path(pool_path).read_text(encoding="utf-8"))
    report_data = json.loads(Path(report_path).read_text(encoding="utf-8"))

    return ProcessingPipelineResult(
        job_id=job_id,
        raw_candidate_pool_path=pool_path,
        processing_report_path=report_path,
        sections_analysed=int(report_data.get("sections_analysed") or 0),
        usable_sections=int(report_data.get("usable_sections") or 0),
        rejected_sections=int(report_data.get("rejected_sections") or 0),
        failed_sections_count=len(report_data.get("failed_sections") or []),
        final_candidate_count=len(pool.get("candidates") or []),
        duplicates_removed=int(report_data.get("duplicates_removed") or 0),
        candidates_rejected_by_boundary=int(
            report_data.get("candidates_rejected_by_boundary") or 0
        ),
    )
