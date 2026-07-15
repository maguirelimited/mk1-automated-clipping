"""Safe control actions that invoke the existing operational layer.

One implementation of operational behaviour (scripts/ops). Multiple interfaces
(SSH + Operations UI). No duplicated restart/scheduler/upload/pipeline logic.
"""

from __future__ import annotations

import io
import json
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Settings

_SCRIPTS_OPS = Path(__file__).resolve().parents[2] / "scripts" / "ops"
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
for _path in (_SCRIPTS_OPS, _SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from observability.config_view import build_config_view  # noqa: E402
from observability.populate import build_system_health  # noqa: E402
from restart_service import execute_restart  # noqa: E402
from run_pipeline import run_pipeline  # noqa: E402
from manual_funnel import ManualFunnelError, resolve_manual_funnel  # noqa: E402
from scheduler_control import (  # noqa: E402
    REASON_START as SCHED_REASON_START,
    REASON_STOP as SCHED_REASON_STOP,
    set_runtime_scheduler_disabled,
)
from upload_control import (  # noqa: E402
    REASON_DISABLE as UPLOAD_REASON_DISABLE,
    REASON_ENABLE as UPLOAD_REASON_ENABLE,
    set_runtime_uploads_disabled,
)

# Lower-risk: execute immediately after CSRF.
LOW_RISK_ACTIONS = frozenset(
    {
        "refresh_health",
        "validate_config",
        "stop_scheduler",
        "start_scheduler",
        "disable_uploads",
        "run_pipeline_dev",
    }
)

# Higher-risk: require explicit confirm=yes in the form.
HIGH_RISK_ACTIONS = frozenset(
    {
        "enable_uploads",
        "restart_service",
        "restart_all",
        "run_pipeline_prod",
    }
)

ALL_ACTIONS = LOW_RISK_ACTIONS | HIGH_RISK_ACTIONS

RESTART_TARGETS = frozenset({"api", "worker", "ai", "ops-ui", "output-funnel"})


@dataclass
class ActionResult:
    ok: bool
    action: str
    message: str
    detail: str = ""


def _env_token(settings: Settings) -> str:
    env = (settings.environment or "dev").strip().lower()
    return "prod" if env in {"prod", "production"} else "dev"


def _capture(fn: Callable[[], int]) -> tuple[int, str]:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = int(fn())
    text = (out.getvalue() + err.getvalue()).strip()
    return code, text


def _sanitize_message(text: str) -> str:
    # Avoid leaking secrets from unexpected tool output.
    lowered = text.lower()
    for token in ("password=", "api_key=", "authorization:", "bearer ", "secret="):
        if token in lowered:
            return "Action completed with redacted operational output."
    # Bound size for flash messages.
    return text[:1500] if text else "Action completed."


def _summarize_run_pipeline_output(code: int, text: str, funnel_id: str) -> str:
    """Turn verbose run_pipeline stdout into a short operator-facing summary."""
    funnel = (funnel_id or "pipeline").strip()

    run_id = ""
    match = re.search(r"^run_id=(.+)$", text, re.MULTILINE)
    if match:
        run_id = match.group(1).strip()

    record_status = ""
    match = re.search(r"^status=(SUCCESS|FAIL|SKIPPED|RUNNING)$", text, re.MULTILINE)
    if match:
        record_status = match.group(1)

    pipeline_status = ""
    match = re.search(r"pipeline status=(\w+)", text)
    if match:
        pipeline_status = match.group(1)

    reason = ""
    match = re.search(r"response HTTP \d+: (\{.*\})", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict):
                reason = str(data.get("reason") or "").strip()
                if not pipeline_status:
                    pipeline_status = str(data.get("status") or "").strip()
        except json.JSONDecodeError:
            pass

    if code != 0 or record_status == "FAIL":
        match = re.search(r"^Error: (.+)$", text, re.MULTILINE)
        if match:
            return f"Pipeline run failed: {match.group(1).strip()}"
        match = re.search(r"run-funnel HTTP (\d+)", text)
        if match:
            return f"Pipeline run failed: source-input returned HTTP {match.group(1)}."
        return "Pipeline run failed. Check Runs for details."

    if record_status == "SKIPPED":
        lowered = text.lower()
        if "execution lock" in lowered or "lock held" in lowered:
            return "Pipeline run skipped — another run is already in progress."
        if "scheduler" in lowered and "disabled" in lowered:
            return "Pipeline run skipped — scheduler is disabled."
        if "disk pressure" in lowered:
            return "Pipeline run skipped — disk pressure blocked new jobs."
        return "Pipeline run skipped."

    if pipeline_status == "no_input_available":
        detail = reason or "all sources already have active or completed jobs."
        message = f"Run finished ({funnel}). No new videos to process — {detail}"
    elif pipeline_status == "input_ready":
        message = f"Run finished ({funnel}). New input queued for video processing."
    else:
        message = f"Run finished ({funnel})."

    if run_id:
        message += f" Run: {run_id}."
    return message


def execute_control_action(
    settings: Settings,
    action: str,
    *,
    confirmed: bool = False,
    restart_target: str = "",
    funnel_id: str = "",
) -> ActionResult:
    """Dispatch to existing ops implementations. Never runs arbitrary shell."""
    action = (action or "").strip()
    if action not in ALL_ACTIONS:
        return ActionResult(False, action or "unknown", "Unknown action.")

    if action in HIGH_RISK_ACTIONS and not confirmed:
        return ActionResult(
            False,
            action,
            "Confirmation required for this high-risk action.",
            detail="missing_confirmation",
        )

    env = _env_token(settings)

    def _resolve_pipeline_funnel() -> str | ActionResult:
        explicit = (funnel_id or "").strip()
        try:
            resolved = resolve_manual_funnel(
                environment=env,
                explicit_id=explicit or None,
            )
        except ManualFunnelError as exc:
            return ActionResult(False, action, str(exc), detail="funnel_resolve_failed")
        return resolved.funnel_id

    if action == "refresh_health":
        try:
            health = build_system_health(env)
            return ActionResult(
                True,
                action,
                f"Health refreshed: overall={health.overall}, boot={health.boot_readiness or 'unknown'}.",
            )
        except Exception as exc:
            return ActionResult(False, action, f"Health refresh failed: {exc.__class__.__name__}")

    if action == "validate_config":
        try:
            view = build_config_view(env, funnel_id=funnel_id)
            state = (view.get("validation") or {}).get("state", "unknown")
            message = (view.get("validation") or {}).get("message", "")
            ok = str(state).upper() == "PASS"
            return ActionResult(ok, action, f"Config validation: {state}. {message}".strip())
        except Exception as exc:
            return ActionResult(False, action, f"Config validation failed: {exc.__class__.__name__}")

    if action == "stop_scheduler":
        code, text = _capture(
            lambda: set_runtime_scheduler_disabled(
                env,
                disabled=True,
                reason=SCHED_REASON_STOP,
            )
        )
        return ActionResult(code == 0, action, _sanitize_message(text) if text else "Scheduler stop requested.")

    if action == "start_scheduler":
        # Authenticated UI operator satisfies the ops-layer confirm gate.
        code, text = _capture(
            lambda: set_runtime_scheduler_disabled(
                env,
                disabled=False,
                reason=SCHED_REASON_START,
                require_prod_confirm=True,
                confirmed=True,
            )
        )
        return ActionResult(code == 0, action, _sanitize_message(text) if text else "Scheduler start requested.")

    if action == "disable_uploads":
        code, text = _capture(
            lambda: set_runtime_uploads_disabled(
                env,
                disabled=True,
                reason=UPLOAD_REASON_DISABLE,
            )
        )
        return ActionResult(code == 0, action, _sanitize_message(text) if text else "Uploads disabled.")

    if action == "enable_uploads":
        code, text = _capture(
            lambda: set_runtime_uploads_disabled(
                env,
                disabled=False,
                reason=UPLOAD_REASON_ENABLE,
                require_prod_confirm=True,
                confirmed=True,
            )
        )
        return ActionResult(code == 0, action, _sanitize_message(text) if text else "Uploads enabled.")

    if action == "restart_service":
        target = (restart_target or "").strip().lower()
        if target not in RESTART_TARGETS:
            return ActionResult(
                False,
                action,
                f"Invalid restart target. Expected one of: {', '.join(sorted(RESTART_TARGETS))}.",
            )
        code, text = _capture(
            lambda: execute_restart(env, target, dry_run=False, confirm=True)
        )
        return ActionResult(
            code == 0,
            action,
            _sanitize_message(text) if text else f"Restart {target} requested.",
        )

    if action == "restart_all":
        code, text = _capture(
            lambda: execute_restart(env, "all", dry_run=False, confirm=True)
        )
        return ActionResult(
            code == 0,
            action,
            _sanitize_message(text) if text else "Restart all services requested.",
        )

    if action == "run_pipeline_dev":
        if env != "dev":
            return ActionResult(
                False,
                action,
                "Development run is only available when MK04_ENV is dev.",
            )
        resolved = _resolve_pipeline_funnel()
        if isinstance(resolved, ActionResult):
            return resolved
        code, text = _capture(
            lambda: run_pipeline(env, funnel_id=resolved, trigger="operations_ui")
        )
        message = (
            _summarize_run_pipeline_output(code, text, resolved)
            if text
            else "Development pipeline run finished."
        )
        return ActionResult(code == 0, action, message)

    if action == "run_pipeline_prod":
        if env != "prod":
            return ActionResult(
                False,
                action,
                "Production run is only available when MK04_ENV is prod.",
            )
        resolved = _resolve_pipeline_funnel()
        if isinstance(resolved, ActionResult):
            return resolved
        code, text = _capture(
            lambda: run_pipeline(env, funnel_id=resolved, trigger="operations_ui")
        )
        message = (
            _summarize_run_pipeline_output(code, text, resolved)
            if text
            else "Production pipeline run finished."
        )
        return ActionResult(code == 0, action, message)

    return ActionResult(False, action, "Unknown action.")


def action_label(action: str) -> str:
    return {
        "refresh_health": "Refresh health",
        "validate_config": "Validate config",
        "stop_scheduler": "Stop scheduler",
        "start_scheduler": "Start scheduler",
        "disable_uploads": "Disable uploads",
        "enable_uploads": "Enable uploads",
        "restart_service": "Restart service",
        "restart_all": "Restart all services",
        "run_pipeline_dev": "Trigger development run",
        "run_pipeline_prod": "Trigger production run",
    }.get(action, action)


def action_risk(action: str) -> str:
    if action in HIGH_RISK_ACTIONS:
        return "high"
    if action in LOW_RISK_ACTIONS:
        return "low"
    return "unknown"
