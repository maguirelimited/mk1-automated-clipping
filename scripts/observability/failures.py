"""Failure aggregation for the Failures page (Phase 11).

Aggregates existing observability sources only. No new failure registry.
"""

from __future__ import annotations

import re
from typing import Any

from .index import (
    DEFAULT_JOB_LIMIT,
    DEFAULT_RUN_LIMIT,
    _env_token,
    get_job_detail,
    list_job_summaries,
    list_run_summaries,
)
from .models import FailureGroup
from .outputs import list_clip_summaries
from .populate import build_service_statuses
from .schemas import CONTRACT_SCHEMA_VERSION

_NOT_AVAILABLE = "Not available"
_MAX_DETAIL_JOBS = 15
_MAX_AFFECTED = 20

_SEVERITY_MAP = {
    "info": "INFO",
    "warn": "WARN",
    "warning": "WARN",
    "fail": "ERROR",
    "failed": "ERROR",
    "error": "ERROR",
    "critical": "CRITICAL",
    "FAIL": "ERROR",
    "WARN": "WARN",
    "PASS": "INFO",
}


def _slug(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", (text or "").strip().lower())
    value = value.strip("_")
    return value[:80] or "unknown"


def _group_key(category: str, name: str) -> str:
    return f"{_slug(category)}:{_slug(name)}"


def _severity(raw: str | None, *, default: str = "ERROR") -> str:
    if not raw:
        return default
    return _SEVERITY_MAP.get(str(raw).strip(), _SEVERITY_MAP.get(str(raw).strip().lower(), default))


def _min_ts(a: str | None, b: str | None) -> str | None:
    values = [v for v in (a, b) if v]
    if not values:
        return None
    return min(values)


def _max_ts(a: str | None, b: str | None) -> str | None:
    values = [v for v in (a, b) if v]
    if not values:
        return None
    return max(values)


def _suggest_for_stage(stage: str | None) -> str:
    text = (stage or "").strip().lower()
    if text in {"transcript", "processing", "source"}:
        return "Inspect processing_report.json"
    if text in {"selection", "rendering", "formatting", "captions", "validation"}:
        return "Inspect post_processing_report.json"
    if text in {"posting", "output_registration"}:
        return "Inspect job outputs and posting state"
    return "Open related job"


def _suggest_for_service(service_name: str) -> str:
    mapping = {
        "api": "View API log",
        "worker": "View worker log",
        "ai_service": "View AI service log",
        "scheduler": "View scheduler log",
        "operations_ui": "View Operations UI log",
        "output_funnel": "View output-funnel log",
    }
    return mapping.get(service_name, "View service health")


def _add_occurrence(
    groups: dict[str, FailureGroup],
    *,
    category: str,
    name: str,
    reason: str,
    severity: str,
    timestamp: str | None,
    job_id: str | None = None,
    run_id: str | None = None,
    stage: str | None = None,
    module: str | None = None,
    suggested: str | None = None,
) -> None:
    key = _group_key(category, name)
    group = groups.get(key)
    if group is None:
        group = FailureGroup(
            group_key=key,
            category=category,
            name=name,
            count=0,
            severity=severity,
            representative_reason=reason or _NOT_AVAILABLE,
            suggested_next_inspection_target=suggested or _NOT_AVAILABLE,
            affected_stage=stage,
            affected_module=module,
        )
        groups[key] = group

    group.count += 1
    group.severity = _rank_severity(group.severity, severity)
    group.first_occurrence = _min_ts(group.first_occurrence, timestamp)
    group.latest_occurrence = _max_ts(group.latest_occurrence, timestamp)
    if job_id and job_id not in group.affected_jobs:
        group.affected_jobs.append(job_id)
        group.affected_jobs = group.affected_jobs[:_MAX_AFFECTED]
    if run_id and run_id not in group.affected_runs:
        group.affected_runs.append(run_id)
        group.affected_runs = group.affected_runs[:_MAX_AFFECTED]
    if stage and not group.affected_stage:
        group.affected_stage = stage
    if module and not group.affected_module:
        group.affected_module = module
    if reason and (
        group.representative_reason in {"", _NOT_AVAILABLE}
        or len(reason) > len(group.representative_reason)
    ):
        # Keep a stable representative: prefer first non-empty, else longer detail.
        if group.representative_reason in {"", _NOT_AVAILABLE}:
            group.representative_reason = reason
    if suggested and group.suggested_next_inspection_target == _NOT_AVAILABLE:
        group.suggested_next_inspection_target = suggested


def _rank_severity(current: str, incoming: str) -> str:
    order = {"INFO": 0, "WARN": 1, "ERROR": 2, "CRITICAL": 3}
    return current if order.get(current, 0) >= order.get(incoming, 0) else incoming


def _collect_from_runs(groups: dict[str, FailureGroup], token: str) -> tuple[int, set[str]]:
    failed_runs = 0
    run_ids: set[str] = set()
    for run in list_run_summaries(token, limit=DEFAULT_RUN_LIMIT):
        if run.status not in {"FAIL", "SKIPPED"}:
            continue
        failed_runs += 1
        run_ids.add(run.run_id)
        reason = (
            run.failure_summary.reason
            if run.failure_summary and run.failure_summary.reason
            else (run.status or "run failed")
        )
        severity = "WARN" if run.status == "SKIPPED" else "ERROR"
        suggested = (
            run.failure_summary.suggested_next_inspection_target
            if run.failure_summary and run.failure_summary.suggested_next_inspection_target
            else f"Open run {run.run_id}"
        )
        timestamp = run.finished_at or run.started_at
        _add_occurrence(
            groups,
            category="Run",
            name=run.run_id,
            reason=reason,
            severity=severity,
            timestamp=timestamp,
            run_id=run.run_id,
            suggested=suggested,
        )
        _add_occurrence(
            groups,
            category="Reason",
            name=reason[:120],
            reason=reason,
            severity=severity,
            timestamp=timestamp,
            run_id=run.run_id,
            suggested=suggested,
        )
        if run.trigger:
            _add_occurrence(
                groups,
                category="Trigger",
                name=run.trigger,
                reason=reason,
                severity=severity,
                timestamp=timestamp,
                run_id=run.run_id,
                suggested=suggested,
            )
    return failed_runs, run_ids


def _collect_from_jobs(groups: dict[str, FailureGroup], token: str) -> tuple[int, set[str]]:
    failed_jobs = 0
    job_ids: set[str] = set()
    failed_summaries = [
        job
        for job in list_job_summaries(token, limit=DEFAULT_JOB_LIMIT)
        if job.state == "failed"
    ]
    failed_jobs = len(failed_summaries)

    for job in failed_summaries:
        job_ids.add(job.job_id)
        reason = (
            job.failure_summary.reason
            if job.failure_summary and job.failure_summary.reason
            else "job failed"
        )
        stage = job.stage or (
            job.failure_summary.stage if job.failure_summary else None
        )
        module = (
            job.failure_summary.component
            if job.failure_summary and job.failure_summary.component not in {"job", "unknown"}
            else None
        )
        suggested = (
            job.failure_summary.suggested_next_inspection_target
            if job.failure_summary and job.failure_summary.suggested_next_inspection_target
            else (f"Open Job {job.job_id}")
        )
        if suggested == f"Open Job {job.job_id}" and stage:
            suggested = _suggest_for_stage(stage)
        timestamp = None
        if job.failure_summary:
            timestamp = job.failure_summary.timestamp

        _add_occurrence(
            groups,
            category="Job",
            name=job.job_id,
            reason=reason,
            severity="ERROR",
            timestamp=timestamp,
            job_id=job.job_id,
            run_id=job.run_id,
            stage=stage,
            module=module,
            suggested=suggested,
        )
        if stage:
            _add_occurrence(
                groups,
                category="Pipeline Stage",
                name=stage,
                reason=reason,
                severity="ERROR",
                timestamp=timestamp,
                job_id=job.job_id,
                run_id=job.run_id,
                stage=stage,
                module=module,
                suggested=_suggest_for_stage(stage),
            )
        _add_occurrence(
            groups,
            category="Reason",
            name=reason[:120],
            reason=reason,
            severity="ERROR",
            timestamp=timestamp,
            job_id=job.job_id,
            run_id=job.run_id,
            stage=stage,
            module=module,
            suggested=suggested,
        )

    # Module-level detail from a bounded set of failed jobs.
    for job in failed_summaries[:_MAX_DETAIL_JOBS]:
        try:
            detail = get_job_detail(token, job.job_id)
        except Exception:
            detail = None
        if detail is None:
            continue
        for failure in detail.failures:
            module = failure.component or "unknown"
            reason = failure.reason or "module failed"
            stage = failure.stage or job.stage
            suggested = (
                failure.suggested_next_inspection_target
                or _suggest_for_stage(stage)
            )
            _add_occurrence(
                groups,
                category="Module",
                name=module,
                reason=reason,
                severity=_severity(failure.severity),
                timestamp=failure.timestamp,
                job_id=job.job_id,
                run_id=job.run_id or detail.summary.run_id,
                stage=stage,
                module=module,
                suggested=suggested,
            )
            if stage:
                _add_occurrence(
                    groups,
                    category="Pipeline Stage",
                    name=stage,
                    reason=reason,
                    severity=_severity(failure.severity),
                    timestamp=failure.timestamp,
                    job_id=job.job_id,
                    run_id=job.run_id or detail.summary.run_id,
                    stage=stage,
                    module=module,
                    suggested=_suggest_for_stage(stage),
                )
    return failed_jobs, job_ids


def _collect_from_services(groups: dict[str, FailureGroup], token: str) -> None:
    try:
        services = build_service_statuses(token)
    except Exception:
        return
    for service in services:
        if str(service.health or "").upper() != "FAIL":
            continue
        reason = service.detail or f"{service.service_name} unhealthy"
        _add_occurrence(
            groups,
            category="Service",
            name=service.service_name,
            reason=reason,
            severity="ERROR",
            timestamp=service.last_checked_at,
            suggested=_suggest_for_service(service.service_name),
        )


def _collect_from_outputs(groups: dict[str, FailureGroup], token: str) -> None:
    try:
        clips = list_clip_summaries(token, limit=50)
    except Exception:
        return
    for clip in clips:
        if str(clip.validation_state or "").lower() != "failed":
            continue
        reason = f"Output validation failed for {clip.clip_id}"
        _add_occurrence(
            groups,
            category="Output Validation",
            name=clip.clip_id,
            reason=reason,
            severity="ERROR",
            timestamp=clip.created_at,
            job_id=clip.job_id,
            suggested=f"Open Job {clip.job_id}",
        )


def list_failure_groups(mk04_env_token: str) -> list[FailureGroup]:
    token = _env_token(mk04_env_token)
    groups: dict[str, FailureGroup] = {}
    _collect_from_runs(groups, token)
    _collect_from_jobs(groups, token)
    _collect_from_services(groups, token)
    _collect_from_outputs(groups, token)
    ordered = list(groups.values())
    ordered.sort(
        key=lambda g: (
            {"CRITICAL": 0, "ERROR": 1, "WARN": 2, "INFO": 3}.get(g.severity, 9),
            -(g.count or 0),
            g.latest_occurrence or "",
        )
    )
    return ordered


def get_failure_group(mk04_env_token: str, group_key: str) -> FailureGroup | None:
    key = (group_key or "").strip()
    if not key or ".." in key or "/" in key:
        return None
    for group in list_failure_groups(mk04_env_token):
        if group.group_key == key:
            return group
    return None


def failures_payload(mk04_env_token: str) -> dict[str, Any]:
    token = _env_token(mk04_env_token)
    groups = list_failure_groups(token)
    failed_jobs = {
        job_id for group in groups for job_id in group.affected_jobs
    }
    failed_runs = {
        run_id for group in groups for run_id in group.affected_runs
    }
    # Prefer counts from source categories when present.
    job_groups = [g for g in groups if g.category == "Job"]
    run_groups = [g for g in groups if g.category == "Run"]
    return {
        "environment": token,
        "total_failures": sum(g.count for g in groups),
        "failed_jobs": len(job_groups) or len(failed_jobs),
        "failed_runs": len(run_groups) or len(failed_runs),
        "distinct_groups": len(groups),
        "groups": [g.to_dict() for g in groups],
        "schema_version": CONTRACT_SCHEMA_VERSION,
    }


def failure_group_payload(mk04_env_token: str, group_key: str) -> dict[str, Any] | None:
    token = _env_token(mk04_env_token)
    group = get_failure_group(token, group_key)
    if group is None:
        return None
    return {
        "environment": token,
        "group": group.to_dict(),
        "related_jobs": [
            {"job_id": job_id, "path": f"/ops/jobs/{job_id}"}
            for job_id in group.affected_jobs
        ],
        "related_runs": [
            {"run_id": run_id, "path": f"/ops/runs/{run_id}"}
            for run_id in group.affected_runs
        ],
        "schema_version": CONTRACT_SCHEMA_VERSION,
    }
