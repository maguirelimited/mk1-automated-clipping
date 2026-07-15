#!/usr/bin/env python3
"""Reliability & Recovery end-to-end smoke (Phase 11).

Validates that existing Reliability components work together via real interfaces.
Does not redesign architecture or add production features.

Usage:
    python scripts/smoke/smoke_reliability.py --env dev
    python scripts/smoke/smoke_reliability.py --env prod --confirm

Dev may toggle scheduler control and restore it. Prod requires --confirm for
any control mutation. Service kill/restart remains in smoke_restart_recovery.py.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
CRONTAB = REPO_ROOT / "deploy" / "cron" / "mk04.crontab"

if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

from boot_verification import build_boot_verification  # noqa: E402
from execution_lock import (  # noqa: E402
    acquire_lock,
    build_lock_payload,
    inspect_execution_lock,
    release_lock,
)
from ops_readonly import scheduled_runs_allowed  # noqa: E402
from upload_control import resolve_data_root  # noqa: E402

DEFAULT_FUNNEL_ID = "mfm_business_ai_001"

REQUIRED_SCRIPTS = (
    "run-pipeline.sh",
    "run-scheduled.sh",
    "stop-scheduler.sh",
    "start-scheduler.sh",
    "scheduler-status.sh",
    "status.sh",
    "health.sh",
    "boot_verification.py",
    "run_pipeline.py",
    "execution_lock.py",
    "run_records.py",
    "scheduler_control.py",
)

REQUIRED_UNITS = (
    "mk04-source-input.service",
    "mk04-video-automation.service",
    "mk04-output-funnel.service",
    "mk04-ai-service.service",
    "mk04-ops-ui.service",
)

STATUS_MARKERS = (
    "Boot readiness",
    "Scheduler",
    "Execution lock",
    "Last pipeline run",
)

HEALTH_MARKERS = (
    "Boot Verification",
    "Boot readiness",
    "Scheduler",
    "Execution lock",
    "Last pipeline run",
)


@dataclass
class CheckResult:
    name: str
    outcome: str  # PASS | WARN | FAIL | SKIP
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SmokeReport:
    environment: str
    started_at: str
    finished_at: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
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


def _run(
    args: list[str],
    *,
    timeout: float = 180.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=merged,
    )


def _bash_ops(script: str, *args: str, timeout: float = 180.0) -> subprocess.CompletedProcess[str]:
    return _run(["bash", str(OPS_DIR / script), *args], timeout=timeout)


def check_scripts_present() -> list[CheckResult]:
    results: list[CheckResult] = []
    for name in REQUIRED_SCRIPTS:
        path = OPS_DIR / name
        ok = path.is_file()
        results.append(
            CheckResult(
                name=f"artifact:{name}",
                outcome="PASS" if ok else "FAIL",
                detail=str(path),
            )
        )
    return results


def check_unit_restart_policy() -> list[CheckResult]:
    results: list[CheckResult] = []
    for unit in REQUIRED_UNITS:
        path = SYSTEMD_DIR / unit
        if not path.is_file():
            results.append(CheckResult(f"unit:{unit}", "FAIL", "missing"))
            continue
        text = path.read_text(encoding="utf-8")
        ok = "Restart=always" in text and "RestartSec=5" in text
        results.append(
            CheckResult(
                f"unit_policy:{unit}",
                "PASS" if ok else "FAIL",
                "Restart=always RestartSec=5" if ok else "policy mismatch",
            )
        )
    return results


def check_cron_uses_shared_entrypoint() -> CheckResult:
    text = CRONTAB.read_text(encoding="utf-8")
    ok = "scripts/ops/run-scheduled.sh" in text
    no_direct = all(
        "/run-funnel" not in line
        for line in text.splitlines()
        if "run-scheduled.sh" in line and not line.strip().startswith("#")
    )
    if ok and no_direct:
        return CheckResult("cron_entrypoint", "PASS", "cron → run-scheduled.sh")
    return CheckResult("cron_entrypoint", "FAIL", "crontab does not use run-scheduled.sh only")


def check_boot_verification(env: str) -> CheckResult:
    report = build_boot_verification(env)
    if report.overall not in {"READY", "NOT READY"}:
        return CheckResult("boot_verification", "FAIL", f"unexpected overall={report.overall}")
    # Both outcomes are valid; consistency is what matters.
    required_fail = any(c.required and c.result == "FAIL" for c in report.components)
    consistent = (report.overall == "NOT READY") == required_fail
    if not consistent:
        return CheckResult(
            "boot_verification",
            "FAIL",
            f"overall={report.overall} inconsistent with required failures",
        )
    return CheckResult(
        "boot_verification",
        "PASS",
        f"overall={report.overall} components={len(report.components)}",
        meta={"overall": report.overall},
    )


def check_status_health_markers(env: str) -> list[CheckResult]:
    results: list[CheckResult] = []
    status = _bash_ops("status.sh", env)
    health = _bash_ops("health.sh", env)
    status_out = status.stdout + status.stderr
    health_out = health.stdout + health.stderr

    missing_status = [m for m in STATUS_MARKERS if m not in status_out]
    results.append(
        CheckResult(
            "status_markers",
            "PASS" if not missing_status and status.returncode == 0 else "FAIL",
            "ok" if not missing_status else f"missing {missing_status}",
        )
    )

    missing_health = [m for m in HEALTH_MARKERS if m not in health_out]
    # health may exit 1/2 when NOT READY — still must print markers.
    results.append(
        CheckResult(
            "health_markers",
            "PASS" if not missing_health else "FAIL",
            "ok" if not missing_health else f"missing {missing_health}",
            meta={"exit_code": health.returncode},
        )
    )
    return results


def check_scheduler_control(env: str, *, allow_mutate: bool) -> list[CheckResult]:
    results: list[CheckResult] = []
    status_before = _bash_ops("scheduler-status.sh", env)
    results.append(
        CheckResult(
            "scheduler_status_runs",
            "PASS" if status_before.returncode == 0 else "FAIL",
            "entrypoint and controls present"
            if "stop-scheduler" in status_before.stdout
            and "run-scheduled.sh" in status_before.stdout
            else status_before.stdout[:200],
        )
    )

    if not allow_mutate:
        results.append(
            CheckResult(
                "scheduler_control_cycle",
                "SKIP",
                "mutation skipped (prod requires --confirm)",
            )
        )
        return results

    data_root = resolve_data_root("development" if env == "dev" else "production")
    allowed_before, _ = scheduled_runs_allowed(data_root)

    stop = _bash_ops("stop-scheduler.sh", env)
    if stop.returncode != 0:
        results.append(CheckResult("scheduler_stop", "FAIL", stop.stderr[:200]))
        return results
    results.append(CheckResult("scheduler_stop", "PASS", "stop-scheduler ok"))

    status_stopped = _bash_ops("scheduler-status.sh", env)
    stopped_ok = (
        status_stopped.returncode == 0
        and "New scheduled runs allowed: NO" in status_stopped.stdout
    )
    results.append(
        CheckResult(
            "scheduler_status_stopped",
            "PASS" if stopped_ok else "FAIL",
            "allowed=NO" if stopped_ok else status_stopped.stdout[:200],
        )
    )

    allowed_stopped, _ = scheduled_runs_allowed(data_root)
    results.append(
        CheckResult(
            "scheduler_runtime_flag",
            "PASS" if allowed_stopped is False else "FAIL",
            "scheduled_runs_allowed=False",
        )
    )

    start_args = ["start-scheduler.sh", env]
    if env == "prod":
        start_args.append("--confirm")
    start = _bash_ops(*start_args)
    if start.returncode != 0:
        results.append(CheckResult("scheduler_start", "FAIL", start.stderr[:200]))
        return results
    results.append(CheckResult("scheduler_start", "PASS", "start-scheduler ok"))

    status_started = _bash_ops("scheduler-status.sh", env)
    started_ok = (
        status_started.returncode == 0
        and "New scheduled runs allowed: YES" in status_started.stdout
    )
    results.append(
        CheckResult(
            "scheduler_status_started",
            "PASS" if started_ok else "FAIL",
            "allowed=YES" if started_ok else status_started.stdout[:200],
        )
    )

    # Restore prior runtime preference if we started from a disabled state.
    if not allowed_before:
        _bash_ops("stop-scheduler.sh", env)

    return results


def _parse_run_id(output: str) -> str | None:
    for line in output.splitlines():
        if line.startswith("run_id="):
            return line.split("=", 1)[1].strip() or None
    return None


def _record_for_run_id(env: str, run_id: str | None):
    if not run_id:
        return None
    from run_records import read_record, run_dir_for

    return read_record(run_dir_for(env, run_id))


def check_pipeline_and_records(env: str, funnel_id: str) -> list[CheckResult]:
    results: list[CheckResult] = []

    # Manual entrypoint
    manual = _bash_ops(
        "run-pipeline.sh",
        env,
        "--funnel-id",
        funnel_id,
        "--trigger",
        "test",
        timeout=180.0,
    )
    manual_out = manual.stdout + manual.stderr
    # Expect a terminal outcome and a run record path.
    has_record = "record_path=" in manual_out or "run_id=" in manual_out
    results.append(
        CheckResult(
            "pipeline_manual_entrypoint",
            "PASS" if has_record and manual.returncode in {0, 1, 3, 4, 5} else "FAIL",
            f"exit={manual.returncode}",
            meta={"exit_code": manual.returncode},
        )
    )

    manual_id = _parse_run_id(manual_out)
    record = _record_for_run_id(env, manual_id)
    if record is None:
        results.append(CheckResult("run_record_manual", "FAIL", "no run record after manual run"))
    else:
        terminal = record.status in {"SUCCESS", "FAIL", "SKIPPED"}
        results.append(
            CheckResult(
                "run_record_manual",
                "PASS" if terminal and record.trigger == "test" else "FAIL",
                f"status={record.status} trigger={record.trigger}",
            )
        )
        log_path = Path(record.log_path)
        results.append(
            CheckResult(
                "run_log_manual",
                "PASS" if log_path.is_file() else "FAIL",
                str(log_path),
            )
        )
        # Readiness failure path should leave FAIL with reason.
        if manual.returncode == 4:
            results.append(
                CheckResult(
                    "run_record_readiness_failure",
                    "PASS"
                    if record.status == "FAIL" and record.failure_reason
                    else "FAIL",
                    record.failure_reason or "missing failure_reason",
                )
            )

    # Scheduled entrypoint (same path, different trigger)
    scheduled = _bash_ops("run-scheduled.sh", env, funnel_id, timeout=180.0)
    scheduled_out = scheduled.stdout + scheduled.stderr
    results.append(
        CheckResult(
            "pipeline_scheduled_entrypoint",
            "PASS"
            if "scheduled trigger" in scheduled_out and scheduled.returncode in {0, 1, 3, 4, 5}
            else "FAIL",
            f"exit={scheduled.returncode}",
        )
    )
    scheduled_id = _parse_run_id(scheduled_out)
    record2 = _record_for_run_id(env, scheduled_id)
    if record2 is None:
        results.append(CheckResult("run_record_scheduled", "FAIL", "no record"))
    else:
        results.append(
            CheckResult(
                "run_record_scheduled",
                "PASS" if record2.trigger == "scheduled" else "FAIL",
                f"trigger={record2.trigger} status={record2.status}",
            )
        )

    return results


def check_execution_lock(env: str, funnel_id: str, *, boot_ready: bool) -> list[CheckResult]:
    results: list[CheckResult] = []
    # Use a dedicated smoke run_id so we never steal a live production lock owner.
    smoke_run_id = f"run_smoke_lock_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    payload = build_lock_payload(
        environment=env,
        run_id=smoke_run_id,
        trigger="test",
        funnel_id=funnel_id,
    )

    # If a lock is already held by someone else, do not clobber it.
    existing = inspect_execution_lock(env)
    if existing.present and not existing.stale:
        results.append(
            CheckResult(
                "execution_lock_acquire",
                "SKIP",
                "active lock already present; not overriding",
                meta={"detail": existing.detail},
            )
        )
        results.append(
            CheckResult(
                "execution_lock_inspect_active",
                "PASS",
                existing.detail,
            )
        )
        return results

    if existing.present and existing.stale:
        results.append(
            CheckResult(
                "execution_lock_stale_visible",
                "PASS",
                existing.detail,
            )
        )
        # Do not auto-clear; skip acquire tests.
        results.append(
            CheckResult(
                "execution_lock_acquire",
                "SKIP",
                "stale lock present and not auto-cleared (by design)",
            )
        )
        return results

    ok, detail, _ = acquire_lock(env, payload)
    results.append(
        CheckResult(
            "execution_lock_acquire",
            "PASS" if ok else "FAIL",
            detail,
        )
    )
    if not ok:
        return results

    inspection = inspect_execution_lock(env)
    results.append(
        CheckResult(
            "execution_lock_inspect",
            "PASS" if inspection.present and not inspection.stale else "FAIL",
            inspection.detail,
        )
    )

    if boot_ready:
        blocked = _bash_ops(
            "run-pipeline.sh",
            env,
            "--funnel-id",
            funnel_id,
            "--trigger",
            "test",
            timeout=180.0,
        )
        record = _record_for_run_id(env, _parse_run_id(blocked.stdout + blocked.stderr))
        skip_ok = (
            blocked.returncode == 5
            and record is not None
            and record.status == "SKIPPED"
            and record.failure_reason
            and "lock" in record.failure_reason.lower()
        )
        results.append(
            CheckResult(
                "execution_lock_blocks_pipeline",
                "PASS" if skip_ok else "FAIL",
                f"exit={blocked.returncode} status={getattr(record, 'status', None)}",
            )
        )
    else:
        results.append(
            CheckResult(
                "execution_lock_blocks_pipeline",
                "SKIP",
                "boot NOT READY; lock module verified, pipeline skip path needs READY host",
            )
        )

    # Stale detection: write an aged lock payload and inspect (without deleting).
    released, release_detail = release_lock(env, run_id=smoke_run_id, pid=payload.pid)
    results.append(
        CheckResult(
            "execution_lock_release",
            "PASS" if released else "FAIL",
            release_detail,
        )
    )

    # Synthetic stale inspection using a temporary dead-pid lock in a subprocess-safe way:
    # acquire, then rewrite file with dead pid + old timestamp, inspect, then remove as owner
    # only if we still own — for smoke we write then unlink ourselves after inspect.
    stale_id = f"{smoke_run_id}_stale"
    stale_payload = build_lock_payload(
        environment=env,
        run_id=stale_id,
        trigger="test",
        funnel_id=funnel_id,
        pid=999_999_999,
        started_at=(datetime.now(UTC) - timedelta(hours=12))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        stale_after_hours=6,
    )
    # Only if no lock present.
    if not inspect_execution_lock(env).present:
        ok_stale, _, _ = acquire_lock(env, stale_payload)
        if ok_stale:
            # Overwrite with stale metadata (same path) — acquire already wrote payload.
            # Force stale fields on disk.
            from execution_lock import lock_path_for_env

            path = lock_path_for_env(env)
            path.write_text(json.dumps(stale_payload.to_dict(), indent=2) + "\n", encoding="utf-8")
            stale_insp = inspect_execution_lock(env)
            results.append(
                CheckResult(
                    "execution_lock_stale_detection",
                    "PASS" if stale_insp.stale else "FAIL",
                    stale_insp.detail,
                )
            )
            # Clean up smoke stale lock (explicit smoke cleanup, not production auto-clear policy).
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                results.append(
                    CheckResult("execution_lock_stale_cleanup", "WARN", str(exc))
                )
        else:
            results.append(
                CheckResult("execution_lock_stale_detection", "SKIP", "could not place stale lock")
            )
    else:
        results.append(
            CheckResult("execution_lock_stale_detection", "SKIP", "lock present after release?")
        )

    return results


def check_scheduler_stop_blocks_scheduled(
    env: str,
    funnel_id: str,
    *,
    allow_mutate: bool,
    boot_ready: bool,
) -> list[CheckResult]:
    if not allow_mutate:
        return [
            CheckResult(
                "scheduler_stop_blocks_scheduled",
                "SKIP",
                "requires control mutation (--confirm on prod)",
            )
        ]
    if not boot_ready:
        return [
            CheckResult(
                "scheduler_stop_blocks_scheduled",
                "SKIP",
                "boot NOT READY; control cycle already validated; SKIPPED path needs READY",
            )
        ]

    results: list[CheckResult] = []
    canonical = "development" if env == "dev" else "production"
    data_root = resolve_data_root(canonical)
    allowed_before, _ = scheduled_runs_allowed(data_root)

    stop = _bash_ops("stop-scheduler.sh", env)
    if stop.returncode != 0:
        return [CheckResult("scheduler_stop_blocks_scheduled", "FAIL", "stop failed")]

    run = _bash_ops("run-scheduled.sh", env, funnel_id, timeout=180.0)
    record = _record_for_run_id(env, _parse_run_id(run.stdout + run.stderr))
    ok = (
        run.returncode == 0
        and record is not None
        and record.status == "SKIPPED"
        and record.trigger == "scheduled"
    )
    results.append(
        CheckResult(
            "scheduler_stop_blocks_scheduled",
            "PASS" if ok else "FAIL",
            f"exit={run.returncode} status={getattr(record, 'status', None)}",
        )
    )

    start_args = ["start-scheduler.sh", env]
    if env == "prod":
        start_args.append("--confirm")
    _bash_ops(*start_args)
    if not allowed_before:
        _bash_ops("stop-scheduler.sh", env)
    return results


def overall_from_checks(checks: list[CheckResult]) -> str:
    outcomes = {c.outcome for c in checks}
    if "FAIL" in outcomes:
        return "FAIL"
    if "WARN" in outcomes or "SKIP" in outcomes:
        return "WARN"
    return "PASS"


def write_report(report: SmokeReport, env: str) -> Path:
    report_dir = REPO_ROOT / "reports" / env / "reliability_smoke"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"smoke_{env}_{stamp}.json"
    payload = json.dumps(asdict(report), indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")
    (report_dir / "latest.json").write_text(payload, encoding="utf-8")
    return path


def print_summary(report: SmokeReport, report_path: Path | None) -> None:
    print()
    print("Reliability & Recovery Smoke")
    print(f"Environment: {report.environment}")
    print(f"Overall:     {report.overall}")
    for item in report.checks:
        line = f"  [{item['outcome']}] {item['name']}"
        if item.get("detail"):
            line += f" — {item['detail']}"
        print(line)
    if report_path is not None:
        print(f"Report: {report_path}")
    print()
    if report.overall == "PASS":
        print("RELIABILITY_SMOKE_PASSED")
    elif report.overall == "WARN":
        print("RELIABILITY_SMOKE_WARN")
    else:
        print("RELIABILITY_SMOKE_FAILED")


def run_smoke(env: str, *, confirm: bool, funnel_id: str) -> SmokeReport:
    started = _utc_now()
    checks: list[CheckResult] = []

    checks.extend(check_scripts_present())
    checks.extend(check_unit_restart_policy())
    checks.append(check_cron_uses_shared_entrypoint())

    boot = check_boot_verification(env)
    checks.append(boot)
    boot_ready = boot.meta.get("overall") == "READY"

    checks.extend(check_status_health_markers(env))

    allow_mutate = env == "dev" or confirm
    checks.extend(check_scheduler_control(env, allow_mutate=allow_mutate))
    checks.extend(check_pipeline_and_records(env, funnel_id))
    checks.extend(check_execution_lock(env, funnel_id, boot_ready=bool(boot_ready)))
    checks.extend(
        check_scheduler_stop_blocks_scheduled(
            env,
            funnel_id,
            allow_mutate=allow_mutate,
            boot_ready=bool(boot_ready),
        )
    )

    # Point operators at restart-recovery smoke for kill tests.
    checks.append(
        CheckResult(
            "restart_recovery_reference",
            "PASS",
            "use scripts/smoke/smoke_restart_recovery.py --execute for live kill/recover",
        )
    )

    return SmokeReport(
        environment=env,
        started_at=started,
        finished_at=_utc_now(),
        checks=[asdict(c) for c in checks],
        overall=overall_from_checks(checks),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="End-to-end Reliability & Recovery smoke validation",
    )
    parser.add_argument("--env", required=True, help="dev or prod")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Allow scheduler control mutations on prod",
    )
    parser.add_argument(
        "--funnel-id",
        default=os.environ.get("RELIABILITY_SMOKE_FUNNEL_ID", DEFAULT_FUNNEL_ID),
        help=f"Funnel id for pipeline probes (default {DEFAULT_FUNNEL_ID})",
    )
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args(argv)

    try:
        env = normalize_env(args.env)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if env == "prod" and not args.confirm:
        print(
            "Note: prod smoke will not mutate scheduler control without --confirm.",
            flush=True,
        )

    try:
        report = run_smoke(env, confirm=bool(args.confirm), funnel_id=args.funnel_id.strip())
    except Exception as exc:
        print(f"Error: smoke failed ({exc})", file=sys.stderr)
        return 1

    report_path = None
    if not args.no_report:
        report_path = write_report(report, env)

    print_summary(report, report_path)
    return 1 if report.overall == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
