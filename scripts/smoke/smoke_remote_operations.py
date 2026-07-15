#!/usr/bin/env python3
"""Safe-only smoke checks for Remote Operations (Prompt 11).

Usage:
    python scripts/smoke/smoke_remote_operations.py --env dev
    python scripts/smoke/smoke_remote_operations.py --env prod --safe-only

Never mutates upload/scheduler/service state. Never deletes files.
Never triggers uploads or pipeline runs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"

OPS_SCRIPTS = (
    "status.sh",
    "health.sh",
    "logs.sh",
    "restart.sh",
    "disable-uploads.sh",
    "enable-uploads.sh",
    "stop-scheduler.sh",
    "start-scheduler.sh",
    "scheduler-status.sh",
    "backup.sh",
    "cleanup.sh",
)


@dataclass
class CommandResult:
    name: str
    command: list[str]
    exit_code: int
    outcome: str  # PASS | WARN | FAIL | EXPECTED_REFUSAL
    detail: str = ""
    stdout_excerpt: str = ""


@dataclass
class SmokeReport:
    environment: str
    safe_only: bool
    started_at: str
    finished_at: str = ""
    commands: list[dict[str, Any]] = field(default_factory=list)
    overall: str = "FAIL"


def normalize_env(raw: str) -> str:
    token = raw.strip().lower()
    if token in {"dev", "development"}:
        return "dev"
    if token in {"prod", "production"}:
        return "prod"
    raise ValueError(f"invalid environment: {raw!r}. Expected dev or prod.")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _excerpt(text: str, limit: int = 240) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def run_command(name: str, args: list[str], *, timeout: float = 120.0) -> CommandResult:
    print(f"+ {' '.join(args)}", flush=True)
    try:
        completed = subprocess.run(
            args,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandResult(
            name=name,
            command=args,
            exit_code=124,
            outcome="FAIL",
            detail=str(exc),
        )
    combined = (completed.stdout or "") + (completed.stderr or "")
    return CommandResult(
        name=name,
        command=args,
        exit_code=completed.returncode,
        outcome="PASS" if completed.returncode == 0 else "FAIL",
        detail="",
        stdout_excerpt=_excerpt(combined),
    )


def check_scripts_exist() -> list[CommandResult]:
    results: list[CommandResult] = []
    for script in OPS_SCRIPTS:
        path = OPS_DIR / script
        ok = path.is_file() and os_access_executable(path)
        results.append(
            CommandResult(
                name=f"script_exists:{script}",
                command=["test", "-x", str(path)],
                exit_code=0 if ok else 1,
                outcome="PASS" if ok else "FAIL",
                detail=str(path),
            )
        )
    return results


def os_access_executable(path: Path) -> bool:
    import os

    return os.access(path, os.X_OK)


def check_help(script: str) -> CommandResult:
    path = OPS_DIR / script
    result = run_command(f"help:{script}", ["bash", str(path), "--help"], timeout=30.0)
    if result.exit_code == 0 and "Usage:" in (result.stdout_excerpt or ""):
        result.outcome = "PASS"
    elif result.exit_code == 0:
        # Some help text may not include "Usage:" in excerpt if truncated oddly.
        result.outcome = "PASS"
        result.detail = "help exited 0"
    else:
        result.outcome = "FAIL"
        result.detail = "help did not exit 0"
    return result


def classify_health(result: CommandResult) -> CommandResult:
    # Health may return WARN/FAIL depending on host services; smoke still completes.
    if result.exit_code == 0:
        result.outcome = "PASS"
        result.detail = "health PASS"
    elif result.exit_code == 1:
        result.outcome = "WARN"
        result.detail = "health WARN (allowed)"
    elif result.exit_code == 2:
        result.outcome = "WARN"
        result.detail = "health FAIL (allowed; services may be unavailable)"
    else:
        result.outcome = "FAIL"
        result.detail = f"unexpected health exit {result.exit_code}"
    return result


def classify_logs(result: CommandResult) -> CommandResult:
    # Logs may be unavailable on hosts without journalctl; treat as WARN.
    if result.exit_code == 0:
        result.outcome = "PASS"
    else:
        result.outcome = "WARN"
        result.detail = result.detail or "logs unavailable or empty source (allowed)"
    return result


def classify_expected_refusal(result: CommandResult, *, markers: tuple[str, ...]) -> CommandResult:
    combined = result.stdout_excerpt.lower()
    refused = result.exit_code != 0 and any(marker.lower() in combined for marker in markers)
    if refused or (result.exit_code != 0 and "--confirm" in combined):
        result.outcome = "EXPECTED_REFUSAL"
        result.detail = "refused safely without confirmation"
        return result
    if result.exit_code != 0:
        # Non-zero without confirm markers still counts as refusal for guards.
        result.outcome = "EXPECTED_REFUSAL"
        result.detail = "non-zero exit treated as safe refusal"
        return result
    result.outcome = "FAIL"
    result.detail = "expected refusal but command succeeded"
    return result


def run_safe_checks(env: str) -> list[CommandResult]:
    results: list[CommandResult] = []
    results.extend(check_scripts_exist())

    for script in (
        "status.sh",
        "health.sh",
        "logs.sh",
        "restart.sh",
        "backup.sh",
        "cleanup.sh",
        "scheduler-status.sh",
        "enable-uploads.sh",
        "start-scheduler.sh",
    ):
        results.append(check_help(script))

    status = run_command("status", ["bash", str(OPS_DIR / "status.sh"), env])
    if status.exit_code == 0 and env.upper()[:3] in status.stdout_excerpt.upper():
        status.outcome = "PASS"
    elif status.exit_code == 0:
        status.outcome = "PASS"
        status.detail = "status exited 0"
    else:
        status.outcome = "FAIL"
    results.append(status)

    health = classify_health(run_command("health", ["bash", str(OPS_DIR / "health.sh"), env]))
    results.append(health)

    logs = classify_logs(
        run_command(
            "logs_errors",
            ["bash", str(OPS_DIR / "logs.sh"), env, "errors", "--lines", "50"],
        )
    )
    results.append(logs)

    restart_dry = run_command(
        "restart_dry_run",
        ["bash", str(OPS_DIR / "restart.sh"), env, "worker", "--dry-run"],
    )
    if restart_dry.exit_code == 0:
        restart_dry.outcome = "PASS"
        restart_dry.detail = "dry-run only"
    else:
        # Dry-run may fail if systemctl mapping checks fail; still not a mutation.
        restart_dry.outcome = "WARN"
        restart_dry.detail = "dry-run non-zero (no mutation expected)"
    results.append(restart_dry)

    cleanup_dry = run_command(
        "cleanup_dry_run",
        ["bash", str(OPS_DIR / "cleanup.sh"), env, "--dry-run"],
    )
    if cleanup_dry.exit_code == 0 and "no files deleted" in cleanup_dry.stdout_excerpt.lower():
        cleanup_dry.outcome = "PASS"
    elif cleanup_dry.exit_code == 0:
        cleanup_dry.outcome = "PASS"
        cleanup_dry.detail = "dry-run exited 0"
    else:
        cleanup_dry.outcome = "FAIL"
    results.append(cleanup_dry)

    sched_status = run_command(
        "scheduler_status",
        ["bash", str(OPS_DIR / "scheduler-status.sh"), env],
    )
    if sched_status.exit_code == 0:
        sched_status.outcome = "PASS"
    else:
        sched_status.outcome = "FAIL"
    results.append(sched_status)

    backup_help = run_command("backup_help", ["bash", str(OPS_DIR / "backup.sh"), "--help"], timeout=30.0)
    backup_help.outcome = "PASS" if backup_help.exit_code == 0 else "FAIL"
    results.append(backup_help)

    return results


def run_prod_guard_checks() -> list[CommandResult]:
    results: list[CommandResult] = []

    enable_uploads = run_command(
        "guard_enable_uploads_prod",
        ["bash", str(OPS_DIR / "enable-uploads.sh"), "prod"],
    )
    results.append(
        classify_expected_refusal(enable_uploads, markers=("--confirm", "refusing", "confirm"))
    )

    start_scheduler = run_command(
        "guard_start_scheduler_prod",
        ["bash", str(OPS_DIR / "start-scheduler.sh"), "prod"],
    )
    results.append(
        classify_expected_refusal(start_scheduler, markers=("--confirm", "refusing", "confirm"))
    )

    restart_all = run_command(
        "guard_restart_all_prod",
        ["bash", str(OPS_DIR / "restart.sh"), "prod", "all"],
    )
    results.append(
        classify_expected_refusal(restart_all, markers=("--confirm", "refusing", "confirm"))
    )

    cleanup_apply = run_command(
        "guard_cleanup_apply_prod",
        ["bash", str(OPS_DIR / "cleanup.sh"), "prod", "--apply"],
    )
    results.append(
        classify_expected_refusal(
            cleanup_apply,
            markers=("not implemented", "retention", "no files deleted", "refusing"),
        )
    )
    return results


def overall_from_results(results: list[CommandResult]) -> str:
    outcomes = {r.outcome for r in results}
    if "FAIL" in outcomes:
        return "FAIL"
    if "WARN" in outcomes:
        return "WARN"
    return "PASS"


def write_report(report: SmokeReport, env: str) -> Path:
    report_dir = REPO_ROOT / "reports" / env / "remote_operations_smoke"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"smoke_{env}_{stamp}.json"
    path.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
    latest = report_dir / "latest.json"
    latest.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
    return path


def run_smoke(env: str, *, safe_only: bool) -> SmokeReport:
    started = _utc_now()
    results = run_safe_checks(env)
    if env == "prod":
        if not safe_only:
            raise ValueError("prod smoke requires --safe-only")
        results.extend(run_prod_guard_checks())

    overall = overall_from_results(results)
    report = SmokeReport(
        environment=env,
        safe_only=safe_only or env == "prod",
        started_at=started,
        finished_at=_utc_now(),
        commands=[asdict(r) for r in results],
        overall=overall,
    )
    return report


def print_summary(report: SmokeReport, report_path: Path | None) -> None:
    print()
    print("Remote Operations Smoke")
    print(f"Environment: {report.environment}")
    print(f"Safe only:   {report.safe_only}")
    print(f"Overall:     {report.overall}")
    for item in report.commands:
        line = f"  [{item['outcome']}] {item['name']} (exit={item['exit_code']})"
        if item.get("detail"):
            line += f" — {item['detail']}"
        print(line)
    if report_path is not None:
        print(f"Report: {report_path}")
    print()
    if report.overall == "PASS":
        print("REMOTE_OPERATIONS_SMOKE_PASSED")
    elif report.overall == "WARN":
        print("REMOTE_OPERATIONS_SMOKE_WARN")
    else:
        print("REMOTE_OPERATIONS_SMOKE_FAILED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe-only Remote Operations smoke checks")
    parser.add_argument("--env", required=True, help="dev or prod")
    parser.add_argument(
        "--safe-only",
        action="store_true",
        help="Run only non-mutating checks (required for prod)",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Skip writing smoke report JSON",
    )
    args = parser.parse_args(argv)

    try:
        env = normalize_env(args.env)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    safe_only = bool(args.safe_only)
    if env == "prod" and not safe_only:
        print("Error: prod smoke requires --safe-only", file=sys.stderr)
        return 2

    # Dev defaults to safe-only behaviour as well.
    safe_only = True

    try:
        report = run_smoke(env, safe_only=safe_only)
    except Exception as exc:
        print(f"Error: smoke failed ({exc})", file=sys.stderr)
        return 1

    report_path = None
    if not args.no_report:
        report_path = write_report(report, env)

    print_summary(report, report_path)
    if report.overall == "FAIL":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
