"""Runs and Jobs list/detail page context from the observability backend.

Read-only filters applied in-process to existing index payloads.
No new backend endpoints or health logic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .config import Settings
from .shell import _mk04_env_token, build_shell_context

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from observability.index import (  # noqa: E402
    get_job_detail,
    get_run_summary,
    jobs_list_payload,
    runs_list_payload,
)
from .reframe_status import (  # noqa: E402
    aggregate_reframe_counts,
    build_reframe_display,
    format_reframe_aggregate_summary,
)
from .outputs_ui import outputs_page_href  # noqa: E402

_RUN_STATUS_FILTERS = {
    "running": {"RUNNING"},
    "completed": {"SUCCESS"},
    "failed": {"FAIL"},
    "skipped": {"SKIPPED"},
}

_RUN_TRIGGER_FILTERS = {
    "scheduled": {"scheduled"},
    "manual": {"manual_cli", "operations_ui", "remote_ssh", "test"},
}

_JOB_STATE_FILTERS = {
    "running": {"running"},
    "completed": {"completed"},
    "failed": {"failed"},
    "skipped": {"cancelled"},
    "queued": {"queued"},
}


def _option(value: str, label: str | None = None) -> dict[str, str]:
    return {"value": value, "label": label or value}


def _unique_sorted(values: list[str]) -> list[str]:
    return sorted({v for v in values if v})


def _filter_runs(
    runs: list[dict[str, Any]],
    *,
    status: str,
    trigger: str,
    funnel: str,
) -> list[dict[str, Any]]:
    out = runs
    if status:
        allowed = _RUN_STATUS_FILTERS.get(status, {status.upper()})
        out = [r for r in out if str(r.get("status") or "") in allowed]
    if trigger:
        allowed = _RUN_TRIGGER_FILTERS.get(trigger, {trigger})
        out = [r for r in out if str(r.get("trigger") or "") in allowed]
    if funnel:
        out = [r for r in out if str(r.get("funnel_id") or "") == funnel]
    return out


def _filter_jobs(
    jobs: list[dict[str, Any]],
    *,
    state: str,
    funnel: str,
    platform: str,
    run_id: str = "",
) -> list[dict[str, Any]]:
    out = jobs
    if state:
        allowed = _JOB_STATE_FILTERS.get(state, {state})
        out = [j for j in out if str(j.get("state") or "") in allowed]
    if funnel:
        out = [j for j in out if str(j.get("funnel") or "") == funnel]
    if platform:
        out = [j for j in out if str(j.get("platform") or "") == platform]
    if run_id:
        out = [j for j in out if str(j.get("run_id") or "") == run_id]
    return out


def _job_state_tone(state: str | None) -> str:
    value = (state or "").strip().lower()
    if value == "completed":
        return "ok"
    if value == "failed":
        return "bad"
    if value in {"running", "queued", "needs_attention"}:
        return "warn"
    return "muted"


def _job_likely_has_outputs(job: dict[str, Any]) -> bool:
    outputs = job.get("outputs")
    if isinstance(outputs, dict):
        produced = outputs.get("outputs_produced")
        passed = outputs.get("clips_passed")
        if produced is not None and int(produced) > 0:
            return True
        if passed is not None and int(passed) > 0:
            return True
    return str(job.get("state") or "").lower() == "completed"


def _enrich_job_row(job: dict[str, Any]) -> dict[str, Any]:
    run_id = str(job.get("run_id") or "").strip()
    job_id = str(job.get("job_id") or "").strip()
    if _job_likely_has_outputs(job):
        job["outputs_href"] = outputs_page_href(run_id=run_id, job_id=job_id)
    else:
        job["outputs_href"] = None
    return job


def _related_jobs_for_run(
    jobs: list[dict[str, Any]], run_id: str
) -> list[dict[str, Any]]:
    """Jobs associated with a run via report execution_context.run_id."""
    rows: list[dict[str, Any]] = []
    for job in jobs:
        if str(job.get("run_id") or "") != run_id:
            continue
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue
        has_outputs = _job_likely_has_outputs(job)
        rows.append(
            {
                "job_id": job_id,
                "state": job.get("state") or "unknown",
                "state_tone": _job_state_tone(str(job.get("state") or "")),
                "funnel": job.get("funnel"),
                "platform": job.get("platform"),
                "stage": job.get("stage"),
                "inspector_href": f"/ops/jobs/{job_id}",
                "outputs_href": (
                    outputs_page_href(run_id=run_id)
                    if has_outputs and run_id
                    else None
                ),
            }
        )
    return rows


def _run_next_steps(
    run: dict[str, Any],
    related_jobs: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Operator next-step links for run detail."""
    run_id = str(run.get("run_id") or "")
    status = str(run.get("status") or "").upper()
    steps: list[dict[str, str]] = []

    if run_id:
        steps.append({"label": "Operator Console", "href": "/ops"})

    if status == "RUNNING":
        steps.append({"label": "View running jobs", "href": f"/ops/jobs?{urlencode({'state': 'running'})}"})
        if related_jobs:
            steps.append(
                {
                    "label": "Jobs for this run",
                    "href": f"/ops/jobs?{urlencode({'run_id': run_id})}",
                }
            )
    elif status == "FAIL":
        steps.append({"label": "Inspect failures", "href": "/ops/failures"})
        steps.append({"label": "View failed jobs", "href": f"/ops/jobs?{urlencode({'state': 'failed'})}"})
        if related_jobs:
            failed = [j for j in related_jobs if j.get("state") == "failed"]
            if failed:
                steps.append(
                    {
                        "label": "Open failed job",
                        "href": failed[0]["inspector_href"],
                    }
                )
    elif status in {"SUCCESS", "SKIPPED"} or int(run.get("jobs_completed") or 0) > 0:
        if run_id:
            steps.append(
                {
                    "label": "View outputs",
                    "href": outputs_page_href(run_id=run_id),
                }
            )
        else:
            steps.append({"label": "View outputs", "href": outputs_page_href()})
        if related_jobs:
            steps.append(
                {
                    "label": "Jobs for this run",
                    "href": f"/ops/jobs?{urlencode({'run_id': run_id})}",
                }
            )

    return steps


def _run_output_shortcuts(
    related_jobs: list[dict[str, Any]],
    *,
    run_id: str = "",
) -> list[dict[str, str]]:
    if run_id:
        return [{"label": "View outputs for this run", "href": outputs_page_href(run_id=run_id)}]
    shortcuts: list[dict[str, str]] = []
    for job in related_jobs:
        href = job.get("outputs_href")
        if href:
            shortcuts.append(
                {"label": f"Outputs for {job['job_id']}", "href": href}
            )
    if shortcuts:
        return shortcuts
    return [{"label": "Browse outputs", "href": outputs_page_href()}]


def build_runs_list_context(
    settings: Settings,
    *,
    shell: dict[str, Any] | None = None,
    status: str = "",
    trigger: str = "",
    funnel: str = "",
) -> dict[str, Any]:
    shell_ctx = shell if shell is not None else build_shell_context(settings)
    token = str(shell_ctx.get("shell_env_token") or _mk04_env_token(settings))
    connected = bool(shell_ctx.get("shell_connected"))

    runs: list[dict[str, Any]] = []
    runs_error: str | None = None
    if connected:
        try:
            payload = runs_list_payload(token)
            runs = [r for r in (payload.get("runs") or []) if isinstance(r, dict)]
        except Exception as exc:
            runs_error = exc.__class__.__name__

    status = (status or "").strip().lower()
    trigger = (trigger or "").strip().lower()
    funnel = (funnel or "").strip()

    funnels = _unique_sorted([str(r.get("funnel_id") or "") for r in runs])
    filtered = _filter_runs(runs, status=status, trigger=trigger, funnel=funnel)

    fields = [
        {
            "name": "status",
            "label": "Status",
            "selected": status,
            "options": [
                _option("running", "Running"),
                _option("completed", "Completed"),
                _option("failed", "Failed"),
                _option("skipped", "Skipped"),
            ],
        },
        {
            "name": "trigger",
            "label": "Trigger",
            "selected": trigger,
            "options": [
                _option("scheduled", "Scheduled"),
                _option("manual", "Manual"),
            ],
        },
        {
            "name": "funnel",
            "label": "Funnel",
            "selected": funnel,
            "options": [_option(f) for f in funnels],
        },
    ]

    return {
        **shell_ctx,
        "list_connected": connected,
        "runs": filtered,
        "runs_empty": connected and not filtered and not runs_error,
        "runs_error": runs_error,
        "runs_total": len(runs),
        "runs_filtered_count": len(filtered),
        "filter_fields": fields,
        "filter_action": "/ops/runs",
        "filter_clear_url": "/ops/runs",
        "filters_active": bool(status or trigger or funnel),
    }


def build_jobs_list_context(
    settings: Settings,
    *,
    shell: dict[str, Any] | None = None,
    state: str = "",
    funnel: str = "",
    platform: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    shell_ctx = shell if shell is not None else build_shell_context(settings)
    token = str(shell_ctx.get("shell_env_token") or _mk04_env_token(settings))
    connected = bool(shell_ctx.get("shell_connected"))

    jobs: list[dict[str, Any]] = []
    jobs_error: str | None = None
    if connected:
        try:
            payload = jobs_list_payload(token)
            jobs = [j for j in (payload.get("jobs") or []) if isinstance(j, dict)]
        except Exception as exc:
            jobs_error = exc.__class__.__name__

    state = (state or "").strip().lower()
    funnel = (funnel or "").strip()
    platform = (platform or "").strip().lower()
    run_id = (run_id or "").strip()

    funnels = _unique_sorted([str(j.get("funnel") or "") for j in jobs])
    platforms = _unique_sorted([str(j.get("platform") or "") for j in jobs])
    filtered = [
        _enrich_job_row(dict(j))
        for j in _filter_jobs(
            jobs, state=state, funnel=funnel, platform=platform, run_id=run_id
        )
    ]

    fields = [
        {
            "name": "state",
            "label": "State",
            "selected": state,
            "options": [
                _option("running", "Running"),
                _option("completed", "Completed"),
                _option("failed", "Failed"),
                _option("queued", "Queued"),
                _option("skipped", "Skipped"),
            ],
        },
        {
            "name": "funnel",
            "label": "Funnel",
            "selected": funnel,
            "options": [_option(f) for f in funnels],
        },
        {
            "name": "platform",
            "label": "Platform",
            "selected": platform,
            "options": [_option(p) for p in platforms],
        },
    ]

    return {
        **shell_ctx,
        "list_connected": connected,
        "jobs": filtered,
        "jobs_empty": connected and not filtered and not jobs_error,
        "jobs_error": jobs_error,
        "jobs_total": len(jobs),
        "jobs_filtered_count": len(filtered),
        "filter_fields": fields,
        "filter_action": "/ops/jobs",
        "filter_clear_url": "/ops/jobs",
        "filters_active": bool(state or funnel or platform or run_id),
        "jobs_filter_run_id": run_id,
    }


def build_run_detail_context(
    settings: Settings,
    run_id: str,
    *,
    shell: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    shell_ctx = shell if shell is not None else build_shell_context(settings)
    token = str(shell_ctx.get("shell_env_token") or _mk04_env_token(settings))
    try:
        summary = get_run_summary(token, run_id)
    except Exception:
        return None
    if summary is None:
        return None
    run = summary.to_dict()

    all_jobs: list[dict[str, Any]] = []
    jobs_load_error: str | None = None
    if shell_ctx.get("shell_connected"):
        try:
            payload = jobs_list_payload(token)
            all_jobs = [
                j for j in (payload.get("jobs") or []) if isinstance(j, dict)
            ]
        except Exception as exc:
            jobs_load_error = exc.__class__.__name__

    related_jobs = _related_jobs_for_run(all_jobs, summary.run_id)
    return {
        **shell_ctx,
        "run": run,
        "run_id": summary.run_id,
        "run_related_jobs": related_jobs,
        "run_related_jobs_fallback_href": (
            f"/ops/jobs?{urlencode({'run_id': summary.run_id})}"
            if summary.run_id
            else "/ops/jobs"
        ),
        "run_jobs_load_error": jobs_load_error,
        "run_next_steps": _run_next_steps(run, related_jobs),
        "run_output_shortcuts": _run_output_shortcuts(related_jobs, run_id=summary.run_id),
    }


def build_job_detail_context(
    settings: Settings,
    job_id: str,
    *,
    shell: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    shell_ctx = shell if shell is not None else build_shell_context(settings)
    token = str(shell_ctx.get("shell_env_token") or _mk04_env_token(settings))
    try:
        detail = get_job_detail(token, job_id)
    except Exception:
        return None
    if detail is None:
        return None
    job = detail.to_dict()
    clips = job.get("clips") if isinstance(job.get("clips"), list) else []
    reframe_displays: list[dict[str, Any]] = []
    enriched_clips: list[dict[str, Any]] = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        reframe = build_reframe_display(
            clip.get("reframe_summary")
            if isinstance(clip.get("reframe_summary"), dict)
            else {"available": False}
        )
        enriched = dict(clip)
        enriched["reframe"] = reframe
        enriched_clips.append(enriched)
        reframe_displays.append(reframe)
    job["clips"] = enriched_clips
    reframe_counts = aggregate_reframe_counts(reframe_displays)
    job_reframe_summary = format_reframe_aggregate_summary(reframe_counts)

    summary = job.get("summary") if isinstance(job.get("summary"), dict) else {}
    run_id = str(summary.get("run_id") or "").strip()
    state = str(summary.get("state") or "").lower()
    loop_links: list[dict[str, str]] = [
        {"label": "Operator Console", "href": "/ops"},
        {"label": "All jobs", "href": "/ops/jobs"},
    ]
    if run_id:
        loop_links.append({"label": f"Run {run_id}", "href": f"/ops/runs/{run_id}"})
    loop_links.append(
        {
            "label": "Outputs for this job",
            "href": outputs_page_href(run_id=run_id, job_id=detail.job_id),
        }
    )
    if state == "failed":
        loop_links.append({"label": "Failures", "href": "/ops/failures"})
    loop_links.append(
        {"label": "Service logs", "href": f"/logs?{urlencode({'job': detail.job_id})}"}
    )
    loop_links.append(
        {
            "label": "Legacy job debug",
            "href": f"/jobs/video/{detail.job_id}",
        }
    )
    # UI renders the observability JobDetail object only.
    return {
        **shell_ctx,
        "job": job,
        "job_id": detail.job_id,
        "job_loop_links": loop_links,
        "job_run_id": run_id or None,
        "job_outputs_href": outputs_page_href(run_id=run_id, job_id=detail.job_id),
        "job_reframe_summary": job_reframe_summary,
        "job_reframe_counts": reframe_counts,
    }
