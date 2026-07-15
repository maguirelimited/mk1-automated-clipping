"""Operator Console page context from the observability backend.

Read-only. Reuses shared shell health/status payloads and loads services/runs
from the same helpers as GET /services and GET /runs. No new health logic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from .config import Settings
from .control_export import INGESTION_PAUSED
from .funnels import load_console_funnel_context
from .shell import _mk04_env_token, build_shell_context
from .outputs_ui import outputs_page_href
from .store import ControlStore

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from observability.index import runs_list_payload  # noqa: E402
from observability.populate import services_payload  # noqa: E402

_SERVICE_LABELS = {
    "api": "API",
    "worker": "Worker",
    "ai_service": "AI Service",
    "operations_ui": "Operations UI",
    "scheduler": "Scheduler",
    "output_funnel": "Output Funnel",
}

_CORE_SERVICE_NAMES = frozenset(
    {"api", "worker", "ai_service", "scheduler", "output_funnel"}
)

_CONSOLE_RUN_LIMIT = 2
_CONSOLE_ATTENTION_LIMIT = 5
_ATTENTION_SEVERITY_ORDER = {"action": 0, "warning": 1, "info": 2}


def _attention_item(
    *,
    title: str,
    explanation: str,
    severity: str,
    href: str | None = None,
    action_label: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """One actionable attention row for the Operator Console."""
    return {
        "title": title,
        "explanation": explanation,
        "severity": severity,
        "href": href,
        "action_label": action_label,
        "detail": detail,
        # Legacy fields used by older tests/templates.
        "label": title,
    }


def _disk_attention(health_data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(health_data, dict):
        return None
    disk = health_data.get("disk")
    if not isinstance(disk, dict):
        return None
    status = str(disk.get("status") or "").strip().upper()
    if status not in {"WARN", "FAIL"}:
        return None
    detail = disk.get("detail") or disk.get("usage_percent")
    detail_text = str(detail) if detail is not None else None
    if status == "WARN":
        return _attention_item(
            title="Disk space low",
            explanation="Storage is approaching capacity. Review usage before the next run.",
            severity="warning",
            href="/ops/storage",
            action_label="Open storage",
            detail=detail_text,
        )
    return _attention_item(
        title="Disk space critical",
        explanation="Storage is critically low. Free space or review retention before running pipelines.",
        severity="action",
        href="/ops/storage",
        action_label="Open storage",
        detail=detail_text,
    )


def _dedupe_and_limit_attention_items(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in sorted(
        items,
        key=lambda row: (
            _ATTENTION_SEVERITY_ORDER.get(str(row.get("severity") or ""), 3),
            str(row.get("title") or ""),
        ),
    ):
        key = str(item.get("title") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(item)
    overflow = max(0, len(unique) - _CONSOLE_ATTENTION_LIMIT)
    return unique[:_CONSOLE_ATTENTION_LIMIT], overflow


def _structured_attention_items(
    *,
    connected: bool,
    health_data: dict[str, Any] | None,
    status_data: dict[str, Any] | None,
    services: list[dict[str, Any]],
    upload_label: str,
    scheduler_label: str,
    recent_runs: list[dict[str, Any]],
    is_production: bool,
    services_error: str | None = None,
    runs_error: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Actionable attention items with links. Returns (visible, overflow_count)."""
    if not connected:
        return (
            [
                _attention_item(
                    title="Observability backend disconnected",
                    explanation="Health and status data is unavailable. Values are not fabricated.",
                    severity="action",
                )
            ],
            0,
        )

    items: list[dict[str, Any]] = []
    health = health_data or {}
    status = status_data or {}

    if services_error:
        items.append(
            _attention_item(
                title="Service status unavailable",
                explanation="Could not load service health data from the observability backend.",
                severity="info",
                href="/ops/failures",
                action_label="View failures",
                detail=services_error,
            )
        )

    if runs_error:
        items.append(
            _attention_item(
                title="Run history unavailable",
                explanation="Recent run data could not be loaded. Last-run attention checks may be incomplete.",
                severity="info",
                href="/ops/runs",
                action_label="Open runs",
                detail=runs_error,
            )
        )

    overall = str(health.get("overall") or "").upper()
    if overall == "FAIL":
        items.append(
            _attention_item(
                title="System health failing",
                explanation="One or more health checks failed. Review failures before running pipelines.",
                severity="action",
                href="/ops/failures",
                action_label="Inspect failures",
            )
        )
    elif overall == "WARN":
        items.append(
            _attention_item(
                title="System health degraded",
                explanation="Health checks reported warnings. Review before the next run if unsure.",
                severity="warning",
                href="/ops/failures",
                action_label="Inspect failures",
            )
        )

    boot = health.get("boot_readiness")
    if boot and str(boot).upper() in {"NOT READY", "FAIL"}:
        items.append(
            _attention_item(
                title="System not ready to run",
                explanation="Boot readiness failed. Review configuration before starting pipelines.",
                severity="action",
                href="/ops/configuration",
                action_label="Open configuration",
                detail=str(boot),
            )
        )

    for failure in health.get("readiness_failures") or []:
        text = str(failure).strip()
        if text:
            items.append(
                _attention_item(
                    title=f"Configuration not ready: {text}",
                    explanation="A readiness check failed. Fix configuration before running pipelines.",
                    severity="action",
                    href="/ops/configuration",
                    action_label="Open configuration",
                )
            )

    for service in services:
        if str(service.get("health") or "").upper() == "FAIL":
            name = service.get("name") or "Service"
            detail = service.get("detail") or None
            items.append(
                _attention_item(
                    title=f"{name} unhealthy",
                    explanation="A core service health check failed. Inspect failures or service logs.",
                    severity="action",
                    href="/ops/failures",
                    action_label="Inspect failures",
                    detail=str(detail) if detail else None,
                )
            )

    disk_item = _disk_attention(health_data)
    if disk_item:
        items.append(disk_item)

    lock = health.get("execution_lock")
    if isinstance(lock, dict) and lock.get("present") and lock.get("stale"):
        run_id = str(lock.get("run_id") or "")
        items.append(
            _attention_item(
                title="Run lock stale",
                explanation="An execution lock appears stuck. Inspect the related run before starting another.",
                severity="action",
                href="/ops/runs" if not run_id else f"/ops/runs/{run_id}",
                action_label="Open run detail" if run_id else "Open runs",
                detail=run_id or None,
            )
        )

    if upload_label == "disabled":
        items.append(
            _attention_item(
                title="Uploads disabled",
                explanation="Posting and uploads are turned off in runtime configuration.",
                severity="warning",
                href="/ops/configuration",
                action_label="Review configuration",
            )
        )
    elif upload_label == "unknown" and is_production:
        items.append(
            _attention_item(
                title="Upload state unknown",
                explanation="Upload/posting state could not be determined in production.",
                severity="warning",
                href="/ops/configuration",
                action_label="Review configuration",
            )
        )

    if scheduler_label in {"disabled", "stopped"}:
        items.append(
            _attention_item(
                title="Scheduler not running",
                explanation="Automated scheduling is stopped. Pipelines will not run on schedule until re-enabled.",
                severity="warning",
                href="/ops/configuration",
                action_label="Review configuration",
                detail=scheduler_label,
            )
        )
    elif scheduler_label == "unknown" and is_production:
        items.append(
            _attention_item(
                title="Scheduler state unknown",
                explanation="Scheduler state could not be determined in production.",
                severity="warning",
                href="/ops/configuration",
                action_label="Review configuration",
            )
        )

    activity = str(status.get("state") or "").lower()
    if activity in {"failing", "blocked"}:
        items.append(
            _attention_item(
                title=f"Pipeline {activity}",
                explanation="Current activity reports a failing or blocked pipeline state.",
                severity="action",
                href=f"/ops/jobs?{urlencode({'state': 'failed'})}",
                action_label="View failed jobs",
            )
        )

    queue = status.get("queue") if isinstance(status.get("queue"), dict) else {}
    failed_jobs = int(queue.get("failed") or 0)
    if failed_jobs > 0:
        label = "job" if failed_jobs == 1 else "jobs"
        items.append(
            _attention_item(
                title=f"{failed_jobs} failed {label} in queue",
                explanation="Failed jobs need inspection before the next run.",
                severity="warning",
                href=f"/ops/jobs?{urlencode({'state': 'failed'})}",
                action_label="View failed jobs",
            )
        )

    active = status.get("active_run") if isinstance(status.get("active_run"), dict) else None
    active_id = str(active.get("run_id") or "") if active else ""
    if active and str(active.get("status") or "").upper() == "FAIL" and active_id:
        reason = None
        failure = active.get("failure_summary")
        if isinstance(failure, dict):
            reason = failure.get("reason")
        items.append(
            _attention_item(
                title="Active run failed",
                explanation="Open the run detail page to inspect the failed stage.",
                severity="action",
                href=f"/ops/runs/{active_id}",
                action_label="Monitor run",
                detail=str(reason) if reason else None,
            )
        )

    last_run = recent_runs[0] if recent_runs else None
    if last_run and str(last_run.get("status") or "").upper() == "FAIL":
        last_id = str(last_run.get("run_id") or "")
        if last_id and last_id != active_id:
            reason = None
            failure = last_run.get("failure_summary")
            if isinstance(failure, dict):
                reason = failure.get("reason")
            items.append(
                _attention_item(
                    title="Last run failed",
                    explanation="Open the run detail page to inspect the failed stage.",
                    severity="action",
                    href=f"/ops/runs/{last_id}",
                    action_label="Open run detail",
                    detail=str(reason) if reason else None,
                )
            )

    return _dedupe_and_limit_attention_items(items)


def _tone_for_result(value: str | None) -> str:
    text = (value or "").strip().upper()
    if text == "PASS":
        return "ok"
    if text == "WARN":
        return "warn"
    if text == "FAIL":
        return "bad"
    return "muted"


def _service_rows(services_data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not services_data:
        return []
    rows: list[dict[str, Any]] = []
    for item in services_data.get("services") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("service_name") or "unknown")
        health = str(item.get("health") or "UNKNOWN")
        rows.append(
            {
                "name": _SERVICE_LABELS.get(name, name.replace("_", " ").title()),
                "service_name": name,
                "health": health,
                "tone": _tone_for_result(health),
                "state": str(item.get("state") or "unknown"),
                "detail": item.get("detail") or "",
            }
        )
    return rows


def _health_headline(*, connected: bool, overall: str) -> tuple[str, str]:
    if not connected:
        return "Unknown / Disconnected", "bad"
    text = (overall or "").strip().upper()
    if text == "PASS":
        return "Healthy", "ok"
    if text == "WARN":
        return "Warning", "warn"
    if text == "FAIL":
        return "Action required", "bad"
    return "Unknown / Disconnected", "muted"


def _health_supporting_line(
    *,
    connected: bool,
    health_data: dict[str, Any] | None,
) -> str | None:
    if not connected:
        return "Observability unavailable — values are not fabricated."
    if not isinstance(health_data, dict):
        return None
    boot = str(health_data.get("boot_readiness") or "").strip().upper()
    if boot in {"NOT READY", "FAIL"}:
        return f"Boot readiness: {health_data.get('boot_readiness')}"
    failures = health_data.get("readiness_failures") or []
    for failure in failures:
        text = str(failure).strip()
        if text:
            return f"Readiness: {text}"
    return None


def _health_detail_href(*, connected: bool, overall: str) -> str | None:
    if not connected:
        return None
    text = (overall or "").strip().upper()
    if text in {"FAIL", "WARN"}:
        return "/ops/failures"
    return None


def _safety_upload_tone(upload_label: str, *, is_production: bool) -> str:
    label = (upload_label or "").strip().lower()
    if label == "enabled":
        return "ok"
    if label == "disabled":
        return "warn"
    if is_production:
        return "warn"
    return "muted"


def _safety_scheduler_tone(scheduler_label: str, *, is_production: bool) -> str:
    label = (scheduler_label or "").strip().lower()
    if label in {"enabled", "manual"}:
        return "ok"
    if label in {"disabled", "stopped"}:
        return "warn"
    if is_production:
        return "warn"
    return "muted"


def _nested_detail(health_data: dict[str, Any] | None, key: str) -> str | None:
    if not isinstance(health_data, dict):
        return None
    block = health_data.get(key)
    if not isinstance(block, dict):
        return None
    detail = block.get("detail")
    return str(detail) if detail else None


def _active_run_summary(status_data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not status_data:
        return None
    active = status_data.get("active_run")
    if not isinstance(active, dict) or not active.get("run_id"):
        return None
    return active


def _queue_summary(status_data: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(status_data, dict):
        return None
    queue = status_data.get("queue")
    if not isinstance(queue, dict):
        return None
    return {
        "pending": int(queue.get("pending") or 0),
        "running": int(queue.get("running") or 0),
        "failed": int(queue.get("failed") or 0),
    }


def _run_status_label(status: str | None) -> str:
    mapping = {
        "SUCCESS": "Success",
        "FAIL": "Failed",
        "RUNNING": "Running",
        "SKIPPED": "Skipped",
    }
    return mapping.get(str(status or "").strip().upper(), str(status or "Unknown"))


def _run_status_tone(status: str | None) -> str:
    text = str(status or "").strip().upper()
    if text == "SUCCESS":
        return "ok"
    if text == "FAIL":
        return "bad"
    if text in {"RUNNING", "SKIPPED"}:
        return "warn"
    return "muted"


def _find_run(recent_runs: list[dict[str, Any]], run_id: str) -> dict[str, Any] | None:
    for run in recent_runs:
        if str(run.get("run_id") or "") == run_id:
            return run
    return None


def _run_card_links(
    run: dict[str, Any],
    *,
    is_active: bool = False,
) -> list[dict[str, str]]:
    run_id = str(run.get("run_id") or "")
    status = str(run.get("status") or "").upper()
    jobs_completed = int(run.get("jobs_completed") or 0)
    jobs_failed = int(run.get("jobs_failed") or 0)
    links: list[dict[str, str]] = []

    if run_id:
        links.append(
            {
                "label": "Monitor run" if is_active or status == "RUNNING" else "Open run detail",
                "href": f"/ops/runs/{run_id}",
            }
        )

    if is_active or status == "RUNNING":
        links.append(
            {
                "label": "View jobs",
                "href": f"/ops/jobs?{urlencode({'state': 'running'})}",
            }
        )
        if run_id:
            links.append(
                {
                    "label": "Jobs for this run",
                    "href": f"/ops/jobs?{urlencode({'run_id': run_id})}",
                }
            )
    elif status == "FAIL":
        links.append({"label": "Inspect failures", "href": "/ops/failures"})
        links.append(
            {
                "label": "View failed jobs",
                "href": f"/ops/jobs?{urlencode({'state': 'failed'})}",
            }
        )
    elif status == "SUCCESS" or jobs_completed > 0:
        links.append({"label": "View outputs", "href": "/ops/outputs"})
        if run_id and (jobs_completed or jobs_failed):
            links.append(
                {
                    "label": "Jobs for this run",
                    "href": f"/ops/jobs?{urlencode({'run_id': run_id})}",
                }
            )
    elif run_id:
        links.append(
            {
                "label": "Jobs for this run",
                "href": f"/ops/jobs?{urlencode({'run_id': run_id})}",
            }
        )

    return links


def _run_card(
    *,
    title: str,
    run: dict[str, Any],
    is_active: bool = False,
) -> dict[str, Any]:
    run_id = str(run.get("run_id") or "")
    status = str(run.get("status") or "UNKNOWN")
    failure = run.get("failure_summary")
    failure_reason = None
    if isinstance(failure, dict) and failure.get("reason"):
        failure_reason = str(failure.get("reason"))
    jobs_completed = run.get("jobs_completed")
    jobs_failed = run.get("jobs_failed")
    links = _run_card_links(run, is_active=is_active)
    return {
        "title": title,
        "run_id": run_id,
        "status_label": _run_status_label(status),
        "status_tone": _run_status_tone(status),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "duration_seconds": run.get("duration_seconds"),
        "jobs_completed": jobs_completed,
        "jobs_failed": jobs_failed,
        "failure_reason": failure_reason,
        "links": links,
        "detail_href": f"/ops/runs/{run_id}" if run_id else None,
        "outputs_href": outputs_page_href(run_id=run_id)
        if run_id and (status.upper() in {"SUCCESS"} or int(jobs_completed or 0) > 0)
        else None,
    }


def _console_run_cards(
    recent_runs: list[dict[str, Any]],
    active_run: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    active_id = ""
    active_running = False
    if active_run and active_run.get("run_id"):
        active_id = str(active_run["run_id"])
        active_running = str(active_run.get("status") or "").upper() == "RUNNING"

    if active_running and active_id:
        merged = _find_run(recent_runs, active_id) or active_run
        cards.append(_run_card(title="Active run", run=merged, is_active=True))

    last_completed: dict[str, Any] | None = None
    for run in recent_runs:
        run_id = str(run.get("run_id") or "")
        status = str(run.get("status") or "").upper()
        if active_running and run_id == active_id:
            continue
        if status == "RUNNING" and active_running:
            continue
        last_completed = run
        break

    if active_running and last_completed:
        cards.append(_run_card(title="Last completed run", run=last_completed))
    elif not active_running and recent_runs:
        cards.append(_run_card(title="Last run", run=recent_runs[0]))

    return cards


def _service_summary(
    *,
    connected: bool,
    services: list[dict[str, Any]],
    services_error: str | None,
) -> dict[str, str]:
    if not connected or services_error:
        return {"text": "Service status unavailable.", "tone": "muted", "href": None}
    failing = [
        s
        for s in services
        if str(s.get("health") or "").upper() == "FAIL"
        and str(s.get("service_name") or "") in _CORE_SERVICE_NAMES
    ]
    if not failing:
        return {
            "text": "All core services healthy.",
            "tone": "ok",
            "href": None,
        }
    if len(failing) == 1:
        return {
            "text": f"1 service unhealthy: {failing[0]['name']}",
            "tone": "bad",
            "href": "/ops/failures",
        }
    names = [s["name"] for s in failing[:3]]
    text = f"{len(failing)} services unhealthy: {', '.join(names)}"
    if len(failing) > 3:
        text += f" (+{len(failing) - 3} more)"
    return {"text": text, "tone": "bad", "href": "/ops/failures"}


def _activity_inspect_href(activity: str) -> str | None:
    state = (activity or "").strip().lower()
    if state in {"failing", "blocked"}:
        return f"/ops/jobs?{urlencode({'state': 'failed'})}"
    if state == "running":
        return None
    return None


def build_overview_context(
    settings: Settings,
    *,
    shell: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build Operator Console page context. Reuses shared shell when provided."""
    shell_ctx = shell if shell is not None else build_shell_context(settings)
    token = str(shell_ctx.get("shell_env_token") or _mk04_env_token(settings))
    connected = bool(shell_ctx.get("shell_connected"))
    health_data = shell_ctx.get("shell_health_data")
    status_data = shell_ctx.get("shell_status_data")
    is_production = bool(shell_ctx.get("shell_is_production"))

    services_data: dict[str, Any] | None = None
    runs_data: dict[str, Any] | None = None
    services_error: str | None = None
    runs_error: str | None = None

    if connected:
        try:
            services_data = services_payload(token)
        except Exception as exc:
            services_error = exc.__class__.__name__
        try:
            runs_data = runs_list_payload(token, limit=_CONSOLE_RUN_LIMIT)
        except Exception as exc:
            runs_error = exc.__class__.__name__

    services = _service_rows(services_data)
    recent_runs: list[dict[str, Any]] = []
    if isinstance(runs_data, dict):
        for run in runs_data.get("runs") or []:
            if isinstance(run, dict):
                recent_runs.append(run)

    upload_label = str(shell_ctx.get("shell_upload") or "unknown")
    scheduler_label = str(shell_ctx.get("shell_scheduler") or "unknown")
    overall_raw = str(shell_ctx.get("shell_overall") or "unknown")

    health_headline, health_headline_tone = _health_headline(
        connected=connected,
        overall=overall_raw if connected else "",
    )
    health_supporting = _health_supporting_line(
        connected=connected,
        health_data=health_data if isinstance(health_data, dict) else None,
    )
    health_detail_href = _health_detail_href(connected=connected, overall=overall_raw)

    attention_items, attention_overflow = _structured_attention_items(
        connected=connected,
        health_data=health_data if isinstance(health_data, dict) else None,
        status_data=status_data if isinstance(status_data, dict) else None,
        services=services,
        upload_label=upload_label,
        scheduler_label=scheduler_label,
        recent_runs=recent_runs,
        is_production=is_production,
        services_error=services_error,
        runs_error=runs_error,
    )

    active_run = _active_run_summary(
        status_data if isinstance(status_data, dict) else None
    )
    activity = str(shell_ctx.get("shell_activity") or "unknown")
    activity_href = _activity_inspect_href(activity)
    if active_run and active_run.get("run_id"):
        activity_href = f"/ops/runs/{active_run['run_id']}"

    boot_readiness = "unknown"
    if isinstance(health_data, dict) and health_data.get("boot_readiness"):
        boot_readiness = str(health_data.get("boot_readiness"))

    service_summary = _service_summary(
        connected=connected,
        services=services,
        services_error=services_error,
    )

    store = ControlStore(settings.control_db_path, controls_file=settings.controls_file)
    store.init_db()
    funnel_ctx = load_console_funnel_context(
        settings,
        store,
        ingestion_paused=store.get_control_bool(INGESTION_PAUSED),
        env_token=token,
    )

    # Legacy string list for tests/backward compatibility.
    legacy_attention = [item["label"] for item in attention_items]
    if attention_overflow:
        legacy_attention.append(f"+ {attention_overflow} more (see Failures)")

    return {
        **shell_ctx,
        **funnel_ctx,
        "overview_connected": connected,
        "console_actions_disabled": not connected,
        "overview_overall": overall_raw if connected else "unknown",
        "overview_overall_tone": _tone_for_result(
            str(shell_ctx.get("shell_overall")) if connected else None
        ),
        "console_health_headline": health_headline,
        "console_health_headline_tone": health_headline_tone,
        "console_health_supporting": health_supporting,
        "console_health_detail_href": health_detail_href,
        "overview_boot_readiness": boot_readiness if connected else "unknown",
        "overview_environment": shell_ctx.get("shell_environment_label") or "UNKNOWN",
        "console_upload_tone": _safety_upload_tone(
            upload_label, is_production=is_production
        ),
        "console_scheduler_tone": _safety_scheduler_tone(
            scheduler_label, is_production=is_production
        ),
        "overview_activity": activity,
        "console_activity_href": activity_href,
        "overview_active_run": active_run,
        "overview_current_activity": (
            (status_data or {}).get("current_activity")
            if isinstance(status_data, dict)
            else None
        ),
        "console_queue": _queue_summary(
            status_data if isinstance(status_data, dict) else None
        ),
        "overview_services": services,
        "overview_services_empty": connected and not services and not services_error,
        "overview_services_error": services_error,
        "console_service_summary": service_summary,
        "overview_upload": upload_label,
        "overview_scheduler": scheduler_label,
        "overview_upload_detail": _nested_detail(
            health_data if isinstance(health_data, dict) else None, "upload"
        ),
        "overview_scheduler_detail": _nested_detail(
            health_data if isinstance(health_data, dict) else None, "scheduler"
        ),
        "overview_recent_runs": recent_runs,
        "console_run_cards": _console_run_cards(recent_runs, active_run),
        "console_runs_empty": connected and not recent_runs and not active_run and not runs_error,
        "overview_runs_empty": connected and not recent_runs and not runs_error,
        "overview_runs_error": runs_error,
        "console_attention": attention_items,
        "console_attention_overflow": attention_overflow,
        "overview_attention": legacy_attention,
        "overview_attention_empty": connected and not attention_items,
    }
