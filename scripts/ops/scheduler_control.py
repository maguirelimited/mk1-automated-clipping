#!/usr/bin/env python3
"""Canonical scheduler operational control (Reliability & Recovery Phase 10).

Operator interface:
  ./scripts/ops/stop-scheduler.sh <env>
  ./scripts/ops/start-scheduler.sh <env> [--confirm]
  ./scripts/ops/scheduler-status.sh <env>

These scripts toggle runtime control in data/<env>/control_state.json.
They do not install/uninstall cron, kill running pipelines, or implement
readiness/locks/run records — run-pipeline.sh owns execution behaviour.

Underlying schedule mechanism (cron today; systemd timer later) is reported
but not manipulated here, so mechanism migration stays behind this layer.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from ops_readonly import (  # noqa: E402
    canonical_env,
    compute_effective_scheduler,
    env_label,
    inspect_underlying_scheduler,
    load_runtime_scheduler_control,
    mk04_env,
    scheduled_runs_allowed,
    REPO_ROOT,
)
from upload_control import (  # noqa: E402
    resolve_data_root,
    write_control_state_atomic,
)

REASON_STOP = "manual_remote_stop"
REASON_START = "manual_remote_start"

# Operator-facing control surface (single path — do not add alternate controls).
CONTROL_SCRIPTS = (
    "scripts/ops/stop-scheduler.sh",
    "scripts/ops/start-scheduler.sh",
    "scripts/ops/scheduler-status.sh",
)

SCHEDULED_ENTRYPOINT = (
    "scripts/ops/run-scheduled.sh → scripts/ops/run-pipeline.sh --trigger scheduled"
)


def resolve_data_root_for_gate(mk04_env_token: str) -> Path:
    try:
        return resolve_data_root(canonical_env(mk04_env_token))
    except Exception:
        root = os.environ.get("MK04_ROOT") or os.environ.get("MK04_CODE_ROOT") or str(REPO_ROOT)
        return Path(root) / "data" / mk04_env(canonical_env(mk04_env_token))


def _yes_no(value: bool | None) -> str:
    if value is True:
        return "YES"
    if value is False:
        return "NO"
    return "UNKNOWN"


def _underlying_active_label(active: bool | None) -> str:
    if active is True:
        return "YES"
    if active is False:
        return "NO"
    return "unknown"


def render_stop_start_status(
    *,
    canonical: str,
    runtime_disabled: bool | None,
    action: str,
) -> str:
    lines = [
        "Scheduler control updated",
        "",
        f"Environment: {env_label(canonical)}",
        f"Scheduler disabled by runtime control: {_yes_no(runtime_disabled)}",
        f"New scheduled runs allowed: {_yes_no(runtime_disabled is not True)}",
        "",
    ]
    if action == "stop":
        lines.extend(
            [
                "New scheduled runs are blocked (run-scheduled → run-pipeline SKIPPED).",
                "Running pipeline jobs were not interrupted.",
                "Cron/timer install was not changed.",
                "Uploads were not changed.",
                "Control: ./scripts/ops/start-scheduler.sh <env> [--confirm]",
            ]
        )
    else:
        lines.extend(
            [
                "Scheduled triggers may proceed again (subject to readiness and lock).",
                "No pipeline run was triggered by this command.",
                "Cron/timer install was not changed.",
                "Uploads were not changed.",
                "Control: ./scripts/ops/stop-scheduler.sh <env>",
            ]
        )
    return "\n".join(lines)


def render_scheduler_status(*, mk04_env_token: str, data_root: Path) -> str:
    canonical = canonical_env(mk04_env_token)
    runtime_disabled, runtime_detail = load_runtime_scheduler_control(data_root)
    allowed, allowed_detail = scheduled_runs_allowed(data_root)
    underlying = inspect_underlying_scheduler(mk04_env_token, REPO_ROOT)
    effective, effective_detail = compute_effective_scheduler(
        runtime_disabled,
        underlying,
        mk04_env_token=mk04_env_token,
    )

    if runtime_disabled is None and runtime_detail == "runtime control file not present":
        runtime_line = "Runtime scheduler disabled: NO (runtime control file not present)"
    else:
        runtime_line = f"Runtime scheduler disabled: {_yes_no(runtime_disabled)}"
        if runtime_detail and runtime_disabled is None:
            runtime_line += f" ({runtime_detail})"

    lines = [
        "Scheduler Status",
        "",
        f"Environment: {env_label(canonical)}",
        runtime_line,
        f"New scheduled runs allowed: {_yes_no(allowed)}",
        f"Underlying scheduler mechanism: {underlying.mechanism}",
        f"Underlying scheduler active: {_underlying_active_label(underlying.active)}",
        f"Effective scheduler state: {effective}",
        f"Pipeline entrypoint: {SCHEDULED_ENTRYPOINT}",
        "Operational controls: stop-scheduler / start-scheduler / scheduler-status",
        "Stop does not kill running pipelines or remove cron/timer install.",
    ]
    detail = effective_detail or underlying.detail or allowed_detail
    if detail:
        lines.append(f"Detail: {detail}")
    return "\n".join(lines)


def set_runtime_scheduler_disabled(
    mk04_env_token: str,
    *,
    disabled: bool,
    reason: str,
    require_prod_confirm: bool = False,
    confirmed: bool = False,
) -> int:
    canonical = canonical_env(mk04_env_token)
    is_production = canonical == "production"

    if require_prod_confirm and is_production and not confirmed:
        print(
            "Refusing to start production scheduler without --confirm.\n"
            "Example: ./scripts/ops/start-scheduler.sh prod --confirm",
            file=sys.stderr,
        )
        return 1

    try:
        data_root = resolve_data_root(canonical)
    except Exception as exc:
        print(f"Error: could not resolve environment data root ({exc})", file=sys.stderr)
        return 1

    expected_env = mk04_env(canonical)
    if data_root.parts[-1] != expected_env and expected_env not in str(data_root):
        print(
            f"Error: refusing to write control state outside {expected_env} data root: {data_root}",
            file=sys.stderr,
        )
        return 1

    updates = {
        "environment": expected_env,
        "scheduler_disabled": disabled,
        "scheduler_updated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "scheduler_updated_by": getpass.getuser(),
        "scheduler_reason": reason,
    }
    write_control_state_atomic(data_root, updates)

    runtime_disabled, _ = load_runtime_scheduler_control(data_root)
    action = "stop" if disabled else "start"
    print(render_stop_start_status(
        canonical=canonical,
        runtime_disabled=runtime_disabled,
        action=action,
    ))
    return 0


def gate_scheduled_run(mk04_env_token: str) -> int:
    """Runtime-control gate only (used by diagnostics / legacy callers).

    Production scheduled execution goes through run-pipeline.sh, which applies
    this same runtime check plus readiness, lock, and run records. This gate
    intentionally does **not** duplicate readiness — that belongs to run-pipeline.
    """
    data_root = resolve_data_root_for_gate(mk04_env_token)
    allowed, detail = scheduled_runs_allowed(data_root)
    if not allowed:
        print(f"scheduled run skipped: {detail}")
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Canonical scheduler operational control (stop/start/status)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    stop_parser = sub.add_parser("stop", help="Block new scheduled runs via runtime control")
    stop_parser.add_argument("environment", help="dev or prod")

    start_parser = sub.add_parser("start", help="Allow scheduled runs again")
    start_parser.add_argument("environment", help="dev or prod")
    start_parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required to start scheduler in production",
    )

    status_parser = sub.add_parser("status", help="Report scheduler control state")
    status_parser.add_argument("environment", help="dev or prod")

    gate_parser = sub.add_parser(
        "gate",
        help="Check runtime scheduler disable only (run-pipeline owns full execution gates)",
    )
    gate_parser.add_argument("environment", help="dev or prod")

    args = parser.parse_args(argv)
    if args.command == "stop":
        return set_runtime_scheduler_disabled(args.environment, disabled=True, reason=REASON_STOP)
    if args.command == "start":
        return set_runtime_scheduler_disabled(
            args.environment,
            disabled=False,
            reason=REASON_START,
            require_prod_confirm=True,
            confirmed=bool(args.confirm),
        )
    if args.command == "status":
        try:
            data_root = resolve_data_root(canonical_env(args.environment))
        except Exception as exc:
            print(f"Error: could not resolve environment data root ({exc})", file=sys.stderr)
            return 1
        print(render_scheduler_status(mk04_env_token=args.environment, data_root=data_root))
        return 0
    return gate_scheduled_run(args.environment)


if __name__ == "__main__":
    raise SystemExit(main())
