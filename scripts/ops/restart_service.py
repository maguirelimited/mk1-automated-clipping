#!/usr/bin/env python3
"""Controlled systemd restart helper for scripts/ops/restart.sh."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass

from ops_readonly import (  # noqa: E402
    REPO_ROOT,
    RESTART_VALID_TARGETS,
    canonical_env,
    env_label,
    mk04_env,
    resolve_restart_targets,
    run_command,
    systemctl_available,
)

HEALTH_WAIT_SECONDS = 3
HEALTH_RETRY_TOTAL_SECONDS = 45
HEALTH_RETRY_INTERVAL_SECONDS = 3


class AuthorizationError(RuntimeError):
    """Raised when systemctl privilege cannot be established."""


@dataclass
class RestartStep:
    target: str
    unit: str
    status: str
    detail: str = ""


def ensure_systemctl_authorization(*, interactive: bool = True) -> None:
    """Establish restart privilege once (sudo credential cache), or no-op as root.

    Does not read, store, pipe, or log the password. Uses ``sudo -v`` once when
    a TTY is available so later ``sudo -n systemctl ...`` calls do not each
    trigger a separate Polkit/password prompt.
    """
    if os.geteuid() == 0:
        return
    if not systemctl_available():
        raise AuthorizationError("systemctl not available")
    if shutil_which_sudo() is None:
        raise AuthorizationError("sudo not available for privileged systemctl")

    probe = subprocess.run(
        ["sudo", "-n", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return

    if not interactive or not sys.stdin.isatty():
        raise AuthorizationError(
            "Privileged systemctl access is not available. "
            "Run `sudo -v` in this terminal (once), then retry."
        )

    print(
        "Authorization required once for service restart "
        "(will not prompt per service)...",
        flush=True,
    )
    # Inherit stdin/stdout/stderr so the operator can authenticate once.
    validate = subprocess.run(["sudo", "-v"], check=False)
    if validate.returncode != 0:
        raise AuthorizationError(
            "Failed to establish sudo authorization for systemctl restart."
        )

    probe = subprocess.run(
        ["sudo", "-n", "true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise AuthorizationError(
            "sudo authorization did not stick; refuse to restart without "
            "non-interactive privilege."
        )


def shutil_which_sudo() -> str | None:
    import shutil

    return shutil.which("sudo")


def systemctl_privileged(args: list[str], *, timeout: float = 120.0) -> tuple[bool, str]:
    """Run ``systemctl <args>`` as root or via ``sudo -n`` (never bare Polkit)."""
    if not systemctl_available():
        return False, "systemctl not available"
    if os.geteuid() == 0:
        cmd = ["systemctl", *args]
    else:
        if shutil_which_sudo() is None:
            return False, "sudo not available"
        cmd = ["sudo", "-n", "systemctl", *args]
    result = run_command(cmd, timeout=timeout)
    if result is None:
        return False, f"systemctl timed out: {' '.join(args)}"
    if result.returncode == 0:
        return True, " ".join(cmd)
    stderr = " ".join((result.stderr or "").strip().split())
    stdout = " ".join((result.stdout or "").strip().split())
    return False, stderr or stdout or f"systemctl failed: {' '.join(args)}"


def systemctl_restart(unit: str) -> tuple[bool, str]:
    """Restart one unit via the privileged path (kept for single-target callers)."""
    return systemctl_privileged(["restart", unit], timeout=60.0)


def systemctl_restart_units(units: list[str]) -> tuple[bool, str]:
    """Restart many units in one systemctl invocation (one auth context)."""
    if not units:
        return True, "no units"
    return systemctl_privileged(["restart", *units], timeout=max(60.0, 30.0 * len(units)))


def systemctl_start_units(units: list[str]) -> tuple[bool, str]:
    """Start many units in one systemctl invocation (idempotent for active units)."""
    if not units:
        return True, "no units"
    return systemctl_privileged(["start", *units], timeout=max(60.0, 30.0 * len(units)))


def systemctl_is_active(unit: str) -> bool:
    result = run_command(["systemctl", "is-active", unit], timeout=10.0)
    return bool(result and result.returncode == 0 and result.stdout.strip() == "active")


def run_health_check(mk04_env_token: str) -> tuple[int, str, str]:
    health_sh = REPO_ROOT / "scripts" / "ops" / "health.sh"
    try:
        completed = subprocess.run(
            ["bash", str(health_sh), mk04_env_token],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 2, "FAIL", str(exc)

    output = completed.stdout.strip()
    overall = "unknown"
    for line in output.splitlines():
        if line.startswith("Overall"):
            overall = line.split(None, 1)[-1].strip() if len(line.split()) > 1 else "unknown"
            break
    return completed.returncode, overall, output


def run_health_check_with_retry(
    mk04_env_token: str,
    *,
    initial_wait_sec: float = HEALTH_WAIT_SECONDS,
    total_sec: float = HEALTH_RETRY_TOTAL_SECONDS,
    interval_sec: float = HEALTH_RETRY_INTERVAL_SECONDS,
) -> tuple[int, str, str]:
    """Poll supported health.sh until PASS or the bounded grace period elapses."""
    if initial_wait_sec > 0:
        time.sleep(initial_wait_sec)
    deadline = time.monotonic() + max(0.0, total_sec)
    last: tuple[int, str, str] = (2, "FAIL", "health check not run")
    while True:
        last = run_health_check(mk04_env_token)
        if last[0] == 0:
            return last
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return last
        time.sleep(min(interval_sec, remaining))


def logs_suggestion(mk04_env_token: str, target: str) -> str:
    if target == "all":
        return f"./scripts/ops/logs.sh {mk04_env_token} errors"
    return f"./scripts/ops/logs.sh {mk04_env_token} {target}"


def execute_start(
    mk04_env_token: str,
    target: str = "all",
    *,
    dry_run: bool = False,
    skip_health: bool = False,
) -> int:
    """Idempotent ``systemctl start`` for production units (no bounce restart).

    Does not enable uploads or the scheduler. Batches all units into one
    privileged ``sudo -n systemctl start …`` call after a single authorization.
    """
    canonical = canonical_env(mk04_env_token)
    label = env_label(canonical)

    try:
        targets = resolve_restart_targets(target)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not targets:
        print("No start targets resolved from repo systemd units.", file=sys.stderr)
        return 1

    print("Remote Operations Start", flush=True)
    print("", flush=True)
    print(f"Environment: {label}", flush=True)
    print(f"Target: {target}", flush=True)
    print("Units:", flush=True)
    for name, unit in targets:
        print(f"  - {name}: {unit}", flush=True)
    print("", flush=True)

    if dry_run:
        print("Dry-run only. No services were started.", flush=True)
        for name, unit in targets:
            print(f"Would start {name} ({unit})", flush=True)
        return 0

    try:
        ensure_systemctl_authorization(interactive=True)
    except AuthorizationError as exc:
        print("Start command: FAIL", flush=True)
        print(f"Reason: {exc}", flush=True)
        return 1

    units = [unit for _name, unit in targets]
    print(f"Starting {len(units)} unit(s) in one systemctl call...", flush=True)
    ok, detail = systemctl_start_units(units)
    if ok:
        print("Start command: PASS", flush=True)
        print(f"Detail: {detail}", flush=True)
    else:
        print("Start command: FAIL", flush=True)
        print(f"Reason: {detail}", flush=True)
        return 1

    if skip_health:
        return 0

    print("", flush=True)
    print(
        f"Waiting up to {HEALTH_WAIT_SECONDS + HEALTH_RETRY_TOTAL_SECONDS}s "
        "for health readiness...",
        flush=True,
    )
    print("Running health check...", flush=True)
    health_code, overall, health_output = run_health_check_with_retry(mk04_env_token)
    if health_output:
        print("", flush=True)
        print(health_output, flush=True)
    print("", flush=True)
    print(f"Overall: {overall}", flush=True)
    return 0 if health_code == 0 else health_code


def execute_restart(
    mk04_env_token: str,
    target: str,
    *,
    dry_run: bool = False,
    confirm: bool = False,
    skip_health: bool = False,
) -> int:
    canonical = canonical_env(mk04_env_token)
    label = env_label(canonical)
    is_production = canonical == "production"

    if target == "all" and is_production and not confirm and not dry_run:
        print("Remote Operations Restart", flush=True)
        print("", flush=True)
        print(f"Environment: {label}", flush=True)
        print("Target: all", flush=True)
        print("", flush=True)
        print("Refused: production restart of all services requires --confirm.", flush=True)
        print("Example: ./scripts/ops/restart.sh prod all --confirm", flush=True)
        return 1

    try:
        targets = resolve_restart_targets(target)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not targets:
        print("No restart targets resolved from repo systemd units.", file=sys.stderr)
        return 1

    print("Remote Operations Restart", flush=True)
    print("", flush=True)
    print(f"Environment: {label}", flush=True)
    print(f"Target: {target}", flush=True)
    if len(targets) == 1:
        print(f"Unit: {targets[0][1]}", flush=True)
    else:
        print("Units:", flush=True)
        for name, unit in targets:
            print(f"  - {name}: {unit}", flush=True)
    print("", flush=True)

    if dry_run:
        print("Dry-run only. No services were restarted.", flush=True)
        for name, unit in targets:
            print(f"Would restart {name} ({unit})", flush=True)
        return 0

    try:
        ensure_systemctl_authorization(interactive=True)
    except AuthorizationError as exc:
        print(f"Restart command: FAIL", flush=True)
        print(f"Reason: {exc}", flush=True)
        print("Suggested next command:", flush=True)
        print(logs_suggestion(mk04_env_token, target), flush=True)
        return 1

    units = [unit for _name, unit in targets]
    print(f"Restarting {len(units)} unit(s) in one systemctl call...", flush=True)
    ok, detail = systemctl_restart_units(units)
    steps: list[RestartStep] = []
    if ok:
        for name, unit in targets:
            steps.append(RestartStep(name, unit, "PASS", detail))
        print("Restart command: PASS", flush=True)
        print(f"Detail: {detail}", flush=True)
    else:
        for name, unit in targets:
            steps.append(RestartStep(name, unit, "FAIL", detail))
        print("Restart command: FAIL", flush=True)
        print(f"Reason: {detail}", flush=True)
        print("", flush=True)
        print("Suggested next command:", flush=True)
        print(logs_suggestion(mk04_env_token, target), flush=True)
        return 1

    if skip_health:
        return 0

    print("", flush=True)
    print(
        f"Waiting up to {HEALTH_WAIT_SECONDS + HEALTH_RETRY_TOTAL_SECONDS}s "
        "for health readiness...",
        flush=True,
    )
    print("Running health check...", flush=True)
    health_code, overall, health_output = run_health_check_with_retry(mk04_env_token)
    if health_output:
        print("", flush=True)
        print(health_output, flush=True)
    print("", flush=True)
    print(f"Overall: {overall}", flush=True)
    print("", flush=True)
    print("Suggested next command:", flush=True)
    print(logs_suggestion(mk04_env_token, target), flush=True)

    if health_code != 0:
        return health_code
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Restart mk04 services and run health check")
    parser.add_argument("environment", help="dev or prod")
    parser.add_argument("target", help="api|worker|ai|all|ops-ui|output-funnel")
    parser.add_argument("--dry-run", action="store_true", help="Print planned restarts only")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required for prod all restarts",
    )
    parser.add_argument(
        "--skip-health",
        action="store_true",
        help="Restart only; skip the embedded post-restart health check",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if not argv:
        argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help"}:
        print(
            "Usage: restart_service.py <dev|prod> <target> [--dry-run] [--confirm]\n"
            "Targets: api, worker, ai, all, ops-ui, output-funnel"
        )
        return 0 if argv and argv[0] in {"-h", "--help"} else 1

    try:
        args = parse_args(argv)
        canonical_env(args.environment)
    except SystemExit as exc:
        raise exc
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    target = args.target.strip().lower()
    if target not in RESTART_VALID_TARGETS:
        print(
            f"Invalid target: {target!r}. Expected one of: {', '.join(sorted(RESTART_VALID_TARGETS))}",
            file=sys.stderr,
        )
        return 1

    return execute_restart(
        mk04_env(canonical_env(args.environment)),
        target,
        dry_run=args.dry_run,
        confirm=args.confirm,
        skip_health=bool(args.skip_health),
    )


if __name__ == "__main__":
    raise SystemExit(main())
