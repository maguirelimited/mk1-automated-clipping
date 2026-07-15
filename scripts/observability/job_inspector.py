"""Build a presentation-ready JobDetail for the Job Inspector.

Aggregates existing report.json, artifact references, and known report files.
Does not invent business logic or inspect arbitrary paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .artifacts import resolve_job_artifacts
from .index import (
    _env_token,
    _execution_context,
    _find_job_dir,
    _is_safe_id,
    _job_summary_from_report,
    _jobs_root_for,
    _optional_str,
    _read_report,
    _warnings_from_report,
)
from .models import (
    ArtifactReference,
    ClipSummary,
    FailureSummary,
    JobDetail,
    LogReference,
    StageTimelineEntry,
)
from .populate import sanitize_detail
from .schemas import CONTRACT_SCHEMA_VERSION, STAGE_NAMES

_NOT_AVAILABLE = "Not available"

# Map report current_stage / timings keys onto canonical STAGE_NAMES.
_STAGE_ALIASES: dict[str, str] = {
    "queued": "source",
    "download": "source",
    "downloaded": "source",
    "input": "source",
    "transcribe": "transcript",
    "transcription": "transcript",
    "discover": "processing",
    "discovery": "processing",
    "candidate_discovery": "processing",
    "select": "selection",
    "render": "rendering",
    "format": "formatting",
    "caption": "captions",
    "validate": "validation",
    "upload": "posting",
    "publish": "posting",
    "output": "output_registration",
    "registration": "output_registration",
}


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _artifact_map(artifacts: list[ArtifactReference]) -> dict[str, list[ArtifactReference]]:
    out: dict[str, list[ArtifactReference]] = {}
    for artifact in artifacts:
        out.setdefault(artifact.artifact_type, []).append(artifact)
    return out


def _first_existing(artifacts: list[ArtifactReference]) -> ArtifactReference | None:
    for artifact in artifacts:
        if artifact.exists and artifact.path:
            return artifact
    return artifacts[0] if artifacts else None


def _resolve_local_path(job_dir: Path, relative_path: str | None, *, token: str, job_id: str) -> Path | None:
    """Map jobs/<env>/<job_id>/... to a path under job_dir."""
    if not relative_path:
        return None
    prefix = f"jobs/{token}/{job_id}/"
    text = relative_path.replace("\\", "/")
    if not text.startswith(prefix):
        return None
    suffix = text[len(prefix) :]
    if not suffix or ".." in suffix.split("/"):
        return None
    target = (job_dir / suffix).resolve()
    try:
        target.relative_to(job_dir.resolve())
    except ValueError:
        return None
    return target


def _load_report_json(
    job_dir: Path,
    artifacts: list[ArtifactReference],
    *,
    token: str,
    job_id: str,
) -> dict[str, Any] | None:
    existing = _first_existing(artifacts)
    if existing is None:
        return None
    path = _resolve_local_path(job_dir, existing.path, token=token, job_id=job_id)
    if path is None:
        return None
    return _safe_read_json(path)


def _metric(data: dict[str, Any] | None, *keys: str) -> Any:
    if not data:
        return None
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _report_summaries(
    *,
    by_type: dict[str, list[ArtifactReference]],
    processing: dict[str, Any] | None,
    post_processing: dict[str, Any] | None,
    selection: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []

    def _entry(
        report_type: str,
        payload: dict[str, Any] | None,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        refs = by_type.get(report_type) or []
        ref = _first_existing(refs) or (refs[0] if refs else None)
        available = payload is not None
        return {
            "report_type": report_type,
            "available": available,
            "path": ref.path if ref else None,
            "metrics": metrics if available else {},
            "detail": None if available else _NOT_AVAILABLE,
        }

    proc_metrics: dict[str, Any] = {}
    if processing is not None:
        for key in (
            "sections_analysed",
            "usable_sections",
            "rejected_sections",
            "candidates_discovered",
        ):
            value = _metric(processing, key)
            if value is not None:
                proc_metrics[key] = value
        if "candidates_discovered" not in proc_metrics:
            candidates = processing.get("candidates")
            if isinstance(candidates, list):
                proc_metrics["candidates_discovered"] = len(candidates)
        warnings = processing.get("warnings")
        if isinstance(warnings, list):
            proc_metrics["warnings"] = len(warnings)
    summaries.append(_entry("processing_report", processing, proc_metrics))

    post_metrics: dict[str, Any] = {}
    if post_processing is not None:
        for key in (
            "raw_candidates_received",
            "candidates_selected",
            "reserve_candidates",
            "candidates_rejected",
            "clips_rendered",
            "clips_passed",
            "clips_failed",
            "modules_run",
            "failed_modules",
        ):
            value = _metric(post_processing, key)
            if value is not None:
                if key in {"modules_run", "failed_modules"} and isinstance(value, list):
                    post_metrics[key] = len(value)
                    if key == "failed_modules":
                        post_metrics["failed_module_names"] = [
                            str(m.get("module_name") or m.get("module") or m)
                            for m in value
                            if isinstance(m, dict) or m
                        ]
                else:
                    post_metrics[key] = value
    summaries.append(_entry("post_processing_report", post_processing, post_metrics))

    sel_metrics: dict[str, Any] = {}
    if selection is not None:
        for key in ("selected", "rejected", "reserve", "candidates_selected", "candidates_rejected"):
            value = _metric(selection, key)
            if value is not None:
                if isinstance(value, list):
                    sel_metrics[key] = len(value)
                else:
                    sel_metrics[key] = value
    summaries.append(_entry("selection_result", selection, sel_metrics))

    pool_refs = by_type.get("raw_candidate_pool") or []
    pool_ref = _first_existing(pool_refs) or (pool_refs[0] if pool_refs else None)
    summaries.append(
        {
            "report_type": "raw_candidate_pool",
            "available": bool(pool_ref and pool_ref.exists),
            "path": pool_ref.path if pool_ref else None,
            "metrics": {},
            "detail": None if (pool_ref and pool_ref.exists) else _NOT_AVAILABLE,
        }
    )
    return summaries


def _normalize_stage(name: str) -> str | None:
    text = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not text:
        return None
    if text in STAGE_NAMES:
        return text
    return _STAGE_ALIASES.get(text)


def _stage_index(name: str | None) -> int:
    if not name:
        return -1
    canonical = _normalize_stage(name)
    if canonical is None:
        return -1
    try:
        return STAGE_NAMES.index(canonical)
    except ValueError:
        return -1


def _build_timeline(
    report: dict[str, Any],
    *,
    summary_state: str,
    current_stage: str | None,
    processing: dict[str, Any] | None,
    post_processing: dict[str, Any] | None,
    by_type: dict[str, list[ArtifactReference]],
) -> list[StageTimelineEntry]:
    details: dict[str, str] = {}

    if by_type.get("transcript") and any(a.exists for a in by_type["transcript"]):
        details["transcript"] = "Completed"
    if by_type.get("raw_candidate_pool") and any(a.exists for a in by_type["raw_candidate_pool"]):
        details.setdefault("processing", "Candidate pool present")

    if processing is not None:
        discovered = _metric(processing, "candidates_discovered")
        if discovered is None:
            candidates = processing.get("candidates")
            if isinstance(candidates, list):
                discovered = len(candidates)
        if discovered is not None:
            details["processing"] = f"{discovered} candidates discovered"
        analysed = _metric(processing, "sections_analysed")
        if analysed is not None and "processing" not in details:
            details["processing"] = f"{analysed} sections analysed"

    if post_processing is not None:
        selected = _metric(post_processing, "candidates_selected")
        reserve = _metric(post_processing, "reserve_candidates")
        rejected = _metric(post_processing, "candidates_rejected")
        parts = []
        if selected is not None:
            parts.append(f"{selected} selected")
        if reserve is not None:
            parts.append(f"{reserve} reserve")
        if rejected is not None:
            parts.append(f"{rejected} rejected")
        if parts:
            details["selection"] = " · ".join(parts)
        rendered = _metric(post_processing, "clips_rendered")
        if rendered is not None:
            details["rendering"] = f"{rendered} rendered"
        passed = _metric(post_processing, "clips_passed")
        failed = _metric(post_processing, "clips_failed")
        if passed is not None:
            details["validation"] = f"{passed} passed"
            details["formatting"] = f"{passed} passed"
            details["captions"] = f"{passed} passed"
        if failed is not None and int(failed) > 0:
            details["validation"] = (
                f"{passed or 0} passed · {failed} failed"
                if passed is not None
                else f"{failed} failed"
            )

    clip_count = len([a for a in (by_type.get("output_clip") or []) if a.exists])
    if clip_count:
        details.setdefault("rendering", f"{clip_count} output clip(s)")
        details.setdefault("output_registration", f"{clip_count} output(s)")

    timings = report.get("stage_timings_ms")
    timed_stages: set[str] = set()
    if isinstance(timings, dict):
        for key in timings:
            canonical = _normalize_stage(str(key))
            if canonical:
                timed_stages.add(canonical)

    current = _normalize_stage(current_stage or "")
    current_idx = _stage_index(current)
    state = (summary_state or "").lower()

    entries: list[StageTimelineEntry] = []
    for index, stage in enumerate(STAGE_NAMES):
        detail = details.get(stage) or _NOT_AVAILABLE
        result = "pending"

        if stage in timed_stages:
            result = "completed"
        elif current_idx >= 0:
            if index < current_idx:
                result = "completed"
            elif index == current_idx:
                if state == "failed":
                    result = "failed"
                elif state == "running":
                    result = "running"
                elif state == "completed":
                    result = "completed"
                elif state == "queued":
                    result = "pending"
                else:
                    result = "running" if state else "unknown"
            else:
                result = "pending"
        elif state == "completed":
            result = "completed" if stage in details else "unknown"
        elif state == "failed" and stage == (current or ""):
            result = "failed"
        elif state == "queued" and stage == "source":
            result = "pending"

        if result == "pending" and detail == _NOT_AVAILABLE and stage not in details:
            detail = _NOT_AVAILABLE
        elif result == "completed" and detail == _NOT_AVAILABLE:
            detail = "Completed"

        entries.append(StageTimelineEntry(stage=stage, result=result, detail=detail))
    return entries


def _failures(
    report: dict[str, Any],
    *,
    summary_failure: FailureSummary | None,
    post_processing: dict[str, Any] | None,
    post_path: str | None,
) -> list[FailureSummary]:
    failures: list[FailureSummary] = []
    if summary_failure is not None:
        if not summary_failure.suggested_next_inspection_target and post_path:
            summary_failure.suggested_next_inspection_target = post_path
        failures.append(summary_failure)

    if post_processing is not None:
        for item in post_processing.get("failed_modules") or []:
            if not isinstance(item, dict):
                continue
            module = str(item.get("module_name") or item.get("module") or "module")
            reason = str(
                item.get("error")
                or item.get("message")
                or item.get("reason")
                or "module failed"
            )
            failures.append(
                FailureSummary(
                    component=module,
                    reason=sanitize_detail(reason) or reason,
                    severity="fail",
                    stage=str(item.get("stage") or "post_processing"),
                    suggested_next_inspection_target=post_path
                    or "post_processing_report.json",
                )
            )
    return failures


def _output_summary(
    summary_outputs,
    *,
    post_processing: dict[str, Any] | None,
    clip_artifacts: list[ArtifactReference],
) -> dict[str, Any]:
    existing_clips = [a for a in clip_artifacts if a.exists]
    # Prefer post-processing report counts when present; report.json clips may be empty.
    outputs_produced = summary_outputs.outputs_produced
    clips_passed = summary_outputs.clips_passed
    clips_failed = summary_outputs.clips_failed
    if post_processing is not None:
        rendered = _metric(post_processing, "clips_rendered")
        if rendered is not None:
            outputs_produced = int(rendered)
        passed = _metric(post_processing, "clips_passed")
        if passed is not None:
            clips_passed = int(passed)
        failed = _metric(post_processing, "clips_failed")
        if failed is not None:
            clips_failed = int(failed)
    if outputs_produced is None and existing_clips:
        outputs_produced = len(existing_clips)

    validation_state = _NOT_AVAILABLE
    if clips_failed is not None and int(clips_failed) > 0:
        validation_state = "failed"
    elif clips_passed is not None:
        validation_state = "passed"

    return {
        "outputs_produced": outputs_produced,
        "clips_passed": clips_passed,
        "clips_failed": clips_failed,
        "validation_state": validation_state,
        "posting_state": _NOT_AVAILABLE,
    }


def _clip_summaries(
    clip_artifacts: list[ArtifactReference],
    *,
    job_id: str,
    token: str,
    funnel: str | None,
    platform: str | None,
    preset: str | None,
) -> list[ClipSummary]:
    # Prefer shared output indexer so Job Inspector and Output Browser agree.
    from .outputs import list_clip_summaries

    clips = list_clip_summaries(token, job_id=job_id, limit=100)
    if clips:
        return clips
    # Fallback when indexer finds nothing but artifacts exist.
    out: list[ClipSummary] = []
    for index, artifact in enumerate(clip_artifacts):
        if not artifact.exists:
            continue
        name = Path(artifact.path or f"clip_{index}").stem
        out.append(
            ClipSummary(
                clip_id=name,
                job_id=job_id,
                validation_state="unknown",
                posting_state="unknown",
                output_path=artifact.path,
                environment=token,
                funnel=funnel,
                platform=platform,
                preset=preset,
                preview_available=bool(artifact.path and artifact.path.endswith((".mp4", ".mov", ".webm", ".mkv", ".m4v"))),
                exists=True,
                created_at=artifact.created_at,
                size_bytes=artifact.size_bytes,
            )
        )
    return out


def build_job_detail(mk04_env_token: str, job_id: str) -> JobDetail | None:
    """Aggregate existing observability sources into one JobDetail."""
    if not _is_safe_id(job_id):
        return None

    token = _env_token(mk04_env_token)
    jobs_root = _jobs_root_for(token)
    job_dir = _find_job_dir(jobs_root, job_id)
    if job_dir is None:
        return None
    report = _read_report(job_dir)
    if report is None:
        return None

    summary = _job_summary_from_report(report, token=token, job_dir=job_dir)
    ctx = _execution_context(report, job_dir)
    trigger = _optional_str(ctx.get("trigger")) if ctx else None

    artifact_payload = resolve_job_artifacts(token, job_id) or {}
    artifacts: list[ArtifactReference] = []
    for item in artifact_payload.get("artifacts") or []:
        if isinstance(item, dict):
            ref = ArtifactReference.from_dict(item)
            if ref is not None:
                artifacts.append(ref)

    logs: list[LogReference] = []
    for item in artifact_payload.get("logs") or []:
        if isinstance(item, dict):
            log_ref = LogReference.from_dict(item)
            if log_ref is not None:
                logs.append(log_ref)
    if not logs:
        logs = [
            LogReference(
                source="job",
                path=None,
                job_id=summary.job_id,
                run_id=summary.run_id,
                detail="job log not found",
            )
        ]

    by_type = _artifact_map(artifacts)
    processing = _load_report_json(
        job_dir, by_type.get("processing_report") or [], token=token, job_id=summary.job_id
    )
    post_processing = _load_report_json(
        job_dir,
        by_type.get("post_processing_report") or [],
        token=token,
        job_id=summary.job_id,
    )
    selection = _load_report_json(
        job_dir, by_type.get("selection_result") or [], token=token, job_id=summary.job_id
    )

    # Enrich outputs from reports when report.json lacks counts.
    if summary.outputs.candidates_discovered is None and processing is not None:
        discovered = _metric(processing, "candidates_discovered")
        if discovered is None:
            candidates = processing.get("candidates")
            if isinstance(candidates, list):
                discovered = len(candidates)
        if discovered is not None:
            summary.outputs.candidates_discovered = int(discovered)
    if post_processing is not None:
        if summary.outputs.clips_passed is None:
            value = _metric(post_processing, "clips_passed")
            if value is not None:
                summary.outputs.clips_passed = int(value)
        if summary.outputs.clips_failed is None:
            value = _metric(post_processing, "clips_failed")
            if value is not None:
                summary.outputs.clips_failed = int(value)
        if summary.outputs.outputs_produced is None:
            value = _metric(post_processing, "clips_rendered")
            if value is not None:
                summary.outputs.outputs_produced = int(value)

    report_artifacts = [
        a
        for a in artifacts
        if a.artifact_type
        in {
            "processing_report",
            "post_processing_report",
            "raw_candidate_pool",
            "selection_result",
        }
    ]
    post_ref = _first_existing(by_type.get("post_processing_report") or [])
    post_path = post_ref.path if post_ref else None

    summary_failure = summary.failure_summary
    failures = _failures(
        report,
        summary_failure=summary_failure,
        post_processing=post_processing,
        post_path=post_path,
    )
    warnings = _warnings_from_report(report)
    if processing is not None:
        for warning in processing.get("warnings") or []:
            text = str(warning)
            if text.strip():
                warnings.append(
                    FailureSummary(
                        component="processing",
                        reason=sanitize_detail(text) or text,
                        severity="warn",
                        stage="processing",
                    )
                )

    clip_artifacts = by_type.get("output_clip") or []
    clips = _clip_summaries(
        clip_artifacts,
        job_id=summary.job_id,
        token=token,
        funnel=summary.funnel,
        platform=summary.platform,
        preset=summary.preset,
    )

    return JobDetail(
        job_id=summary.job_id,
        summary=summary,
        stage_timeline=_build_timeline(
            report,
            summary_state=summary.state,
            current_stage=summary.stage,
            processing=processing,
            post_processing=post_processing,
            by_type=by_type,
        ),
        artifacts=artifacts,
        reports=report_artifacts,
        logs=logs,
        warnings=warnings,
        failures=failures,
        clips=clips,
        created_at=_optional_str(report.get("created_at")),
        started_at=_optional_str(report.get("started_at")),
        finished_at=_optional_str(report.get("completed_at")),
        trigger=trigger,
        report_summaries=_report_summaries(
            by_type=by_type,
            processing=processing,
            post_processing=post_processing,
            selection=selection,
        ),
        output_summary=_output_summary(
            summary.outputs,
            post_processing=post_processing,
            clip_artifacts=clip_artifacts,
        ),
        schema_version=CONTRACT_SCHEMA_VERSION,
    )
