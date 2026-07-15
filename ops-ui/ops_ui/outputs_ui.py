"""Output Browser UI context from the observability outputs layer."""

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

from observability.index import get_job_detail, get_run_summary, list_run_summaries  # noqa: E402
from observability.outputs import (  # noqa: E402
    get_clip_detail,
    latest_job_id_for_funnel,
    latest_run_id_with_clips,
    list_clips_for_funnel,
    list_clips_for_job,
    list_clips_for_run,
    list_recent_output_clips,
)
from .reframe_status import build_reframe_display  # noqa: E402

def outputs_page_href(*, run_id: str = "", job_id: str = "", funnel_id: str = "") -> str:
    """Canonical Outputs list URL; prefers run_id, then job_id, then funnel_id."""
    params: dict[str, str] = {}
    if run_id:
        params["run_id"] = run_id
    elif job_id:
        params["job_id"] = job_id
    elif funnel_id:
        params["funnel_id"] = funnel_id
    if not params:
        return "/ops/outputs"
    return f"/ops/outputs?{urlencode(params)}"


def outputs_redirect_target(
    settings: Settings,
    *,
    shell: dict[str, Any] | None = None,
    job_id: str = "",
) -> str:
    """Best Outputs page for legacy clip-review redirects."""
    shell_ctx = shell if shell is not None else build_shell_context(settings)
    token = str(shell_ctx.get("shell_env_token") or _mk04_env_token(settings))
    run_id = ""
    job_id = (job_id or "").strip()
    if job_id:
        try:
            detail = get_job_detail(token, job_id)
            if detail is not None:
                run_id = str(detail.summary.run_id or "").strip()
        except Exception:
            pass
    if run_id:
        return outputs_page_href(run_id=run_id)
    if job_id:
        return outputs_page_href(job_id=job_id)
    return outputs_page_href()


_RUN_SELECTOR_LIMIT = 20


def _validation_tone(state: str | None) -> str:
    value = str(state or "").lower()
    if value == "passed":
        return "ok"
    if value == "failed":
        return "bad"
    return "muted"


def _preview_unavailable_reason(clip: dict[str, Any]) -> str | None:
    if clip.get("preview_available"):
        return None
    if not clip.get("exists"):
        return "Output file is missing on disk."
    warnings = clip.get("warnings") or []
    if isinstance(warnings, list):
        for warning in warnings:
            text = str(warning).strip()
            if text:
                return text
    if not clip.get("output_path"):
        return "No output path recorded."
    return "Preview not available for this file."


def _run_selector_label(summary: Any) -> str:
    parts: list[str] = [str(summary.run_id or "").strip()]
    status = str(summary.status or "").strip().upper()
    if status:
        parts.append(status)
    time_label = str(summary.finished_at or summary.started_at or "").strip()
    if time_label:
        parts.append(time_label)
    funnel = str(summary.funnel_id or "").strip()
    if funnel:
        parts.append(funnel)
    return " · ".join(part for part in parts if part)


def _run_selector_options(
    token: str,
    *,
    selected_run_id: str,
    limit: int = _RUN_SELECTOR_LIMIT,
) -> list[dict[str, Any]]:
    """Recent pipeline runs for the Outputs filter (newest first, any status)."""
    options: list[dict[str, Any]] = []
    try:
        summaries = list_run_summaries(token, limit=max(1, min(int(limit), 200)))
    except Exception:
        return options
    for summary in summaries:
        run_id = str(summary.run_id or "").strip()
        if not run_id:
            continue
        options.append(
            {
                "run_id": run_id,
                "label": _run_selector_label(summary),
                "selected": run_id == selected_run_id,
            }
        )
    if selected_run_id and not any(opt["selected"] for opt in options):
        options.insert(
            0,
            {
                "run_id": selected_run_id,
                "label": selected_run_id,
                "selected": True,
            },
        )
    return options


def _run_summary_dict(token: str, run_id: str) -> dict[str, Any] | None:
    try:
        summary = get_run_summary(token, run_id)
    except Exception:
        return None
    if summary is None:
        return None
    return summary.to_dict()


def build_outputs_list_context(
    settings: Settings,
    *,
    shell: dict[str, Any] | None = None,
    run_id: str | None = None,
    job_id: str | None = None,
    funnel_id: str | None = None,
) -> dict[str, Any]:
    shell_ctx = shell if shell is not None else build_shell_context(settings)
    token = str(shell_ctx.get("shell_env_token") or _mk04_env_token(settings))
    connected = bool(shell_ctx.get("shell_connected"))
    job_id = (job_id or "").strip()
    requested_run_id = (run_id or "").strip()
    requested_funnel_id = (funnel_id or "").strip()

    outputs: list[dict[str, Any]] = []
    outputs_error: str | None = None
    selected_run_id = requested_run_id
    outputs_filter_job_state: str | None = None
    outputs_funnel_latest_job_id: str | None = None

    if job_id and connected and not selected_run_id:
        try:
            detail = get_job_detail(token, job_id)
            if detail is not None:
                outputs_filter_job_state = str(detail.summary.state or "").lower() or None
                selected_run_id = str(detail.summary.run_id or "").strip()
        except Exception:
            pass

    if connected and not selected_run_id and not job_id and not requested_funnel_id:
        try:
            # Prefer the newest run that already has clips so review stays run-scoped
            # without dumping every recent job into one mixed list.
            selected_run_id = latest_run_id_with_clips(token) or ""
        except Exception as exc:
            outputs_error = exc.__class__.__name__

    run_summary: dict[str, Any] | None = None
    if connected and outputs_error is None:
        try:
            if job_id:
                outputs = list_clips_for_job(token, job_id)
            elif requested_funnel_id:
                outputs_funnel_latest_job_id = latest_job_id_for_funnel(token, requested_funnel_id)
                outputs = list_clips_for_funnel(token, requested_funnel_id)
            elif selected_run_id:
                run_summary = _run_summary_dict(token, selected_run_id)
                outputs = list_clips_for_run(token, selected_run_id)
            else:
                outputs = list_recent_output_clips(token)
        except Exception as exc:
            outputs_error = exc.__class__.__name__
            outputs = []

    selector_options: list[dict[str, Any]] = []
    if connected and selected_run_id and not requested_funnel_id:
        selector_options = _run_selector_options(
            token,
            selected_run_id=selected_run_id,
        )

    empty_kind = "none"
    if outputs_error:
        empty_kind = "error"
    elif not connected:
        empty_kind = "disconnected"
    elif not outputs:
        if requested_funnel_id:
            empty_kind = "no_funnel_clips" if outputs_funnel_latest_job_id else "no_funnel_jobs"
        else:
            empty_kind = "no_clips" if selected_run_id or job_id else "no_successful_runs"

    return {
        **shell_ctx,
        "outputs_connected": connected,
        "outputs": outputs,
        "outputs_empty": connected
        and not outputs
        and not outputs_error
        and bool(selected_run_id or job_id or requested_funnel_id),
        "outputs_empty_kind": empty_kind,
        "outputs_error": outputs_error,
        "outputs_filter_job_id": job_id,
        "outputs_filter_job_state": outputs_filter_job_state,
        "outputs_filter_funnel_id": requested_funnel_id or None,
        "outputs_funnel_latest_job_id": outputs_funnel_latest_job_id,
        "outputs_count": len(outputs),
        "outputs_run_id": selected_run_id or None,
        "outputs_run": run_summary,
        "outputs_run_selector": selector_options,
    }


def build_output_detail_context(
    settings: Settings,
    job_id: str,
    clip_id: str,
    *,
    shell: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    shell_ctx = shell if shell is not None else build_shell_context(settings)
    token = str(shell_ctx.get("shell_env_token") or _mk04_env_token(settings))
    try:
        detail = get_clip_detail(token, job_id, clip_id)
    except Exception:
        return None
    if detail is None:
        return None

    clip = detail.get("clip") if isinstance(detail.get("clip"), dict) else {}
    run_id: str | None = None
    job_state: str | None = None
    try:
        job_detail = get_job_detail(token, job_id)
        if job_detail is not None:
            run_id = str(job_detail.summary.run_id or "").strip() or None
            job_state = str(job_detail.summary.state or "").lower() or None
    except Exception:
        pass

    loop_links: list[dict[str, str]] = [
        {"label": "Operator Console", "href": "/ops"},
        {"label": "All outputs", "href": outputs_page_href()},
    ]
    if run_id:
        loop_links.append(
            {
                "label": f"Outputs for run {run_id}",
                "href": outputs_page_href(run_id=run_id),
            }
        )
    loop_links.extend(
        [
            {
                "label": f"Job {job_id}",
                "href": f"/ops/jobs/{job_id}",
            },
        ]
    )
    if run_id:
        loop_links.insert(2, {"label": f"Run {run_id}", "href": f"/ops/runs/{run_id}"})
    validation_state = str(clip.get("validation_state") or "").lower()
    if validation_state == "failed" or job_state == "failed":
        loop_links.append({"label": "Failures", "href": "/ops/failures"})

    metadata = detail.get("metadata_summary")
    metadata = metadata if isinstance(metadata, dict) else {}
    validation = detail.get("validation_summary")
    validation = validation if isinstance(validation, dict) else {}
    reframe_summary = detail.get("reframe_summary")
    reframe_summary = reframe_summary if isinstance(reframe_summary, dict) else {"available": False}
    output_reframe = build_reframe_display(reframe_summary)

    return {
        **shell_ctx,
        "output": detail,
        "clip": clip,
        "job_id": job_id,
        "clip_id": clip_id,
        "output_run_id": run_id,
        "output_job_state": job_state,
        "output_loop_links": loop_links,
        "output_preview_unavailable_reason": _preview_unavailable_reason(clip),
        "output_validation_tone": _validation_tone(
            validation.get("state") or clip.get("validation_state")
        ),
        "output_metadata_available": bool(metadata.get("available")),
        "output_metadata_title": metadata.get("title") if metadata.get("available") else None,
        "output_metadata_validation": metadata.get("validation_result")
        if metadata.get("available")
        else None,
        "output_reframe": output_reframe,
        "output_reframe_tone": output_reframe.get("tone"),
    }
