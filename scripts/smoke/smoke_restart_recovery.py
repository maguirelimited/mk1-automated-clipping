#!/usr/bin/env python3
"""Restart recovery smoke for Reliability & Recovery Phase 4.

Default (policy-only): verify unit files declare Restart=always / RestartSec=5.
No processes are killed.

Live mode:
    python scripts/smoke/smoke_restart_recovery.py --env dev --execute
    python scripts/smoke/smoke_restart_recovery.py --env prod --execute --confirm

Kills one service MainPID at a time (SIGKILL), waits for systemd recovery and
HTTP health. Does not recover jobs, mutate uploads/scheduler, or redesign units.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
OPS_DIR = REPO_ROOT / "scripts" / "ops"
SMOKE_DIR = Path(__file__).resolve().parent

if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

from ops_readonly import (  # noqa: E402
    evaluate_scheduler_readiness,
    http_probe,
    service_health_urls,
    systemctl_available,
)

# Prompt 4 core services (operator mode, unit stem, health label in service_health_urls).
CORE_SERVICES: tuple[dict[str, str], ...] = (
    {"mode": "ai", "unit": "mk04-ai-service.service", "label": "AI service"},
    {"mode": "api", "unit": "mk04-source-input.service", "label": "API"},
    {"mode": "worker", "unit": "mk04-video-automation.service", "label": "Worker"},
    {"mode": "ops-ui", "unit": "mk04-ops-ui.service", "label": "Operations UI"},
)

OPTIONAL_SERVICES: tuple[dict[str, str], ...] = (
    {"mode": "output-funnel", "unit": "mk04-output-funnel.service", "label": "Output funnel"},
)

EXPECTED_RESTART = "always"
EXPECTED_RESTART_SEC = 5
RECOVERY_WAIT_SECONDS = 45.0
POLL_INTERVAL_SECONDS = 0.5


@dataclass
class CheckResult:
    name: str
    outcome: str  # PASS | WARN | FAIL | SKIP
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SmokeReport:
    environment: str
    mode: str
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


def services_for_run(*, include_output_funnel: bool) -> list[dict[str, str]]:
    services = list(CORE_SERVICES)
    if include_output_funnel:
        services.extend(OPTIONAL_SERVICES)
    return services


def unit_file_path(unit: str) -> Path:
    return SYSTEMD_DIR / unit


def parse_unit_restart_policy(unit_path: Path) -> tuple[str | None, int | None, list[str]]:
    """Return (Restart, RestartSec, problems)."""
    problems: list[str] = []
    if not unit_path.is_file():
        return None, None, [f"unit file missing: {unit_path}"]

    restart: str | None = None
    restart_sec: int | None = None
    wanted_by = False
    for line in unit_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key == "Restart":
            restart = value
        elif key == "RestartSec":
            try:
                restart_sec = int(value)
            except ValueError:
                problems.append(f"RestartSec not an integer: {value!r}")
        elif key == "WantedBy" and value == "multi-user.target":
            wanted_by = True

    if restart != EXPECTED_RESTART:
        problems.append(f"Restart={restart!r} (expected {EXPECTED_RESTART!r})")
    if restart_sec != EXPECTED_RESTART_SEC:
        problems.append(f"RestartSec={restart_sec!r} (expected {EXPECTED_RESTART_SEC})")
    if not wanted_by:
        problems.append("WantedBy=multi-user.target missing")
    return restart, restart_sec, problems


def run_command(args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def systemctl_cmd(args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str] | None:
    """Try systemctl, then sudo -n systemctl."""
    if not systemctl_available():
        return None
    for prefix in ([], ["sudo", "-n"]):
        try:
            completed = run_command([*prefix, "systemctl", *args], timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            continue
        stderr = (completed.stderr or "").strip()
        if completed.returncode == 0:
            return completed
        if prefix == [] and "Interactive authentication required" in stderr:
            continue
        if prefix == [] and completed.returncode != 0:
            # Try sudo path for permission errors.
            continue
        return completed
    return None


def systemctl_show(unit: str, prop: str) -> str | None:
    completed = systemctl_cmd(["show", unit, f"-p{prop}", "--value"])
    if completed is None or completed.returncode != 0:
        return None
    return (completed.stdout or "").strip()


def systemctl_is_active(unit: str) -> str | None:
    completed = systemctl_cmd(["is-active", unit])
    if completed is None:
        return None
    return (completed.stdout or completed.stderr or "").strip().lower()


def parse_restart_usec(raw: str | None) -> int | None:
    """Parse systemd RestartUSec (e.g. 5s, 5000000) to seconds."""
    if not raw:
        return None
    token = raw.strip().lower()
    if token.endswith("ms"):
        try:
            return max(1, int(round(float(token[:-2]) / 1000.0)))
        except ValueError:
            return None
    if token.endswith("s") and not token.endswith("us"):
        try:
            return int(round(float(token[:-1])))
        except ValueError:
            return None
    if token.endswith("us"):
        try:
            return max(1, int(round(int(token[:-2]) / 1_000_000)))
        except ValueError:
            return None
    try:
        # Plain microseconds from some systemctl versions.
        usec = int(token)
        return max(1, int(round(usec / 1_000_000)))
    except ValueError:
        return None


def check_unit_file_policy(service: dict[str, str]) -> CheckResult:
    unit = service["unit"]
    path = unit_file_path(unit)
    _restart, _sec, problems = parse_unit_restart_policy(path)
    if problems:
        return CheckResult(
            name=f"policy_file:{service['mode']}",
            outcome="FAIL",
            detail="; ".join(problems),
            meta={"unit": unit, "path": str(path)},
        )
    return CheckResult(
        name=f"policy_file:{service['mode']}",
        outcome="PASS",
        detail=f"Restart={EXPECTED_RESTART} RestartSec={EXPECTED_RESTART_SEC}",
        meta={"unit": unit, "path": str(path)},
    )


def check_installed_policy(service: dict[str, str]) -> CheckResult:
    unit = service["unit"]
    name = f"policy_installed:{service['mode']}"
    if not systemctl_available():
        return CheckResult(name=name, outcome="WARN", detail="systemctl not available")

    # systemctl show returns defaults (Restart=no) for unknown units — require LoadState.
    load_state = (systemctl_show(unit, "LoadState") or "").lower()
    if load_state != "loaded":
        return CheckResult(
            name=name,
            outcome="WARN",
            detail=f"unit not installed on host (LoadState={load_state or 'unknown'}); file policy still checked",
            meta={"load_state": load_state},
        )

    active = systemctl_is_active(unit) or "unknown"
    restart = (systemctl_show(unit, "Restart") or "").lower()
    restart_usec = systemctl_show(unit, "RestartUSec")
    restart_sec = parse_restart_usec(restart_usec)
    problems: list[str] = []
    if restart != EXPECTED_RESTART:
        problems.append(f"Restart={restart!r}")
    if restart_sec is not None and restart_sec != EXPECTED_RESTART_SEC:
        problems.append(f"RestartUSec={restart_usec!r} (~{restart_sec}s)")
    if problems:
        return CheckResult(
            name=name,
            outcome="FAIL",
            detail="; ".join(problems),
            meta={"restart": restart, "restart_usec": restart_usec, "active": active},
        )
    return CheckResult(
        name=name,
        outcome="PASS",
        detail=(
            f"installed Restart={restart or EXPECTED_RESTART} "
            f"RestartUSec={restart_usec or f'{EXPECTED_RESTART_SEC}s'} "
            f"active={active}"
        ),
        meta={"active": active, "restart": restart, "restart_usec": restart_usec},
    )


def kill_main_pid(unit: str) -> tuple[bool, str, int | None]:
    pid_raw = systemctl_show(unit, "MainPID")
    if not pid_raw or pid_raw == "0":
        return False, "no MainPID", None
    try:
        pid = int(pid_raw)
    except ValueError:
        return False, f"invalid MainPID {pid_raw!r}", None

    try:
        os.kill(pid, signal.SIGKILL)
        return True, f"SIGKILL sent to pid {pid}", pid
    except PermissionError:
        completed = run_command(["sudo", "-n", "kill", "-9", str(pid)])
        if completed.returncode == 0:
            return True, f"sudo kill -9 pid {pid}", pid
        return False, f"permission denied killing pid {pid}", pid
    except ProcessLookupError:
        return True, f"pid {pid} already exited", pid
    except OSError as exc:
        return False, str(exc), pid


def wait_for_recovery(
    unit: str,
    *,
    previous_nrestarts: int | None,
    health_url: str,
    probe_fn: Callable[[str], tuple[bool, str]] = http_probe,
    wait_seconds: float = RECOVERY_WAIT_SECONDS,
) -> CheckResult:
    deadline = time.monotonic() + wait_seconds
    last_active = ""
    last_nrestarts: int | None = previous_nrestarts
    health_ok = False
    health_detail = ""

    while time.monotonic() < deadline:
        last_active = systemctl_is_active(unit) or ""
        nraw = systemctl_show(unit, "NRestarts")
        try:
            last_nrestarts = int(nraw) if nraw not in (None, "") else last_nrestarts
        except ValueError:
            pass

        if last_active == "active":
            health_ok, health_detail = probe_fn(health_url)
            nrestarts_ok = (
                previous_nrestarts is None
                or last_nrestarts is None
                or last_nrestarts > previous_nrestarts
            )
            if health_ok and nrestarts_ok:
                return CheckResult(
                    name=f"recover:{unit}",
                    outcome="PASS",
                    detail=(
                        f"active again; NRestarts {previous_nrestarts}→{last_nrestarts}; "
                        f"health {health_detail}"
                    ),
                    meta={
                        "active": last_active,
                        "nrestarts_before": previous_nrestarts,
                        "nrestarts_after": last_nrestarts,
                        "health": health_detail,
                    },
                )
        time.sleep(POLL_INTERVAL_SECONDS)

    problems: list[str] = []
    if last_active != "active":
        problems.append(f"active_state={last_active or 'unknown'}")
    if previous_nrestarts is not None and last_nrestarts is not None:
        if last_nrestarts <= previous_nrestarts:
            problems.append(
                f"NRestarts did not increase ({previous_nrestarts}→{last_nrestarts})"
            )
    if not health_ok:
        problems.append(f"health not ready ({health_detail or 'no probe'})")

    return CheckResult(
        name=f"recover:{unit}",
        outcome="FAIL",
        detail="; ".join(problems) or "recovery timed out",
        meta={
            "active": last_active,
            "nrestarts_before": previous_nrestarts,
            "nrestarts_after": last_nrestarts,
            "health": health_detail,
        },
    )


def journal_restart_visible(unit: str, *, since_iso: str) -> CheckResult:
    """Best-effort: journal should show activity around the kill/restart window."""
    name = f"journal:{unit}"
    for prefix in ([], ["sudo", "-n"]):
        try:
            result = run_command(
                [
                    *prefix,
                    "journalctl",
                    "-u",
                    unit,
                    f"--since={since_iso}",
                    "-n",
                    "50",
                    "--no-pager",
                ],
                timeout=30.0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CheckResult(name=name, outcome="WARN", detail=str(exc))
        if result.returncode == 0:
            text = (result.stdout or "").lower()
            markers = ("start", "stop", "restart", "killed", "main process", "deactivated", "began")
            if any(marker in text for marker in markers) or (result.stdout or "").strip():
                return CheckResult(
                    name=name,
                    outcome="PASS",
                    detail="journal entries present for recovery window",
                )
            return CheckResult(
                name=name,
                outcome="WARN",
                detail="journalctl returned no obvious restart markers",
            )
        stderr = (result.stderr or "").lower()
        if "interactive authentication" in stderr:
            continue
    return CheckResult(name=name, outcome="WARN", detail="journalctl unavailable")


def execute_recovery_for_service(
    env: str,
    service: dict[str, str],
    *,
    probe_fn: Callable[[str], tuple[bool, str]] = http_probe,
    kill_fn: Callable[[str], tuple[bool, str, int | None]] | None = None,
    wait_fn: Callable[..., CheckResult] | None = None,
) -> list[CheckResult]:
    unit = service["unit"]
    mode = service["mode"]
    label = service["label"]
    results: list[CheckResult] = []

    active = systemctl_is_active(unit)
    if active != "active":
        results.append(
            CheckResult(
                name=f"execute:{mode}",
                outcome="SKIP",
                detail=f"unit not active ({active}); skip kill test",
                meta={"unit": unit},
            )
        )
        return results

    nraw = systemctl_show(unit, "NRestarts")
    try:
        nrestarts_before = int(nraw) if nraw not in (None, "") else None
    except ValueError:
        nrestarts_before = None

    urls = service_health_urls(env)
    health_url = urls[label]
    since_iso = _utc_now()

    killer = kill_fn or kill_main_pid
    ok, detail, pid = killer(unit)
    results.append(
        CheckResult(
            name=f"kill:{mode}",
            outcome="PASS" if ok else "FAIL",
            detail=detail,
            meta={"unit": unit, "pid": pid, "nrestarts_before": nrestarts_before},
        )
    )
    if not ok:
        return results

    waiter = wait_fn or wait_for_recovery
    recovery = waiter(
        unit,
        previous_nrestarts=nrestarts_before,
        health_url=health_url,
        probe_fn=probe_fn,
    )
    recovery.name = f"recover:{mode}"
    results.append(recovery)
    results.append(journal_restart_visible(unit, since_iso=since_iso))
    return results


def check_readiness_after_recovery(env: str) -> CheckResult:
    readiness = evaluate_scheduler_readiness(env)
    # AI / ops-ui optional; required probes must pass if those services were recovered.
    if readiness.ready:
        return CheckResult(
            name="readiness_after_recovery",
            outcome="PASS",
            detail="required services HTTP-ready after recovery checks",
        )
    # If API/worker/output-funnel were not all in the execute set or not installed,
    # readiness may fail for unrelated reasons — treat as WARN when only optional gaps.
    required_failures = [r for r in readiness.reasons if not r.startswith("AI") and "Operations UI" not in r]
    if required_failures:
        return CheckResult(
            name="readiness_after_recovery",
            outcome="WARN",
            detail="; ".join(readiness.reasons),
        )
    return CheckResult(
        name="readiness_after_recovery",
        outcome="PASS",
        detail="required readiness ok (optional services may be down)",
    )


def run_policy_checks(services: list[dict[str, str]]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for service in services:
        results.append(check_unit_file_policy(service))
        results.append(check_installed_policy(service))
    return results


def run_execute_checks(env: str, services: list[dict[str, str]]) -> list[CheckResult]:
    results: list[CheckResult] = []
    if not systemctl_available():
        results.append(
            CheckResult(
                name="execute",
                outcome="FAIL",
                detail="systemctl not available; cannot run live recovery",
            )
        )
        return results

    for service in services:
        print(f"== execute recovery: {service['mode']} ({service['unit']})", flush=True)
        results.extend(execute_recovery_for_service(env, service))

    results.append(check_readiness_after_recovery(env))
    return results


def overall_from_checks(checks: list[CheckResult]) -> str:
    outcomes = {c.outcome for c in checks}
    if "FAIL" in outcomes:
        return "FAIL"
    if "WARN" in outcomes or "SKIP" in outcomes:
        return "WARN"
    return "PASS"


def write_report(report: SmokeReport, env: str) -> Path:
    report_dir = REPO_ROOT / "reports" / env / "restart_recovery_smoke"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"smoke_{env}_{stamp}.json"
    payload = json.dumps(asdict(report), indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")
    (report_dir / "latest.json").write_text(payload, encoding="utf-8")
    return path


def print_summary(report: SmokeReport, report_path: Path | None) -> None:
    print()
    print("Restart Recovery Smoke")
    print(f"Environment: {report.environment}")
    print(f"Mode:        {report.mode}")
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
        print("RESTART_RECOVERY_SMOKE_PASSED")
    elif report.overall == "WARN":
        print("RESTART_RECOVERY_SMOKE_WARN")
    else:
        print("RESTART_RECOVERY_SMOKE_FAILED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify systemd restart recovery policy and optional live kill tests",
    )
    parser.add_argument("--env", required=True, help="dev or prod")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Kill each active service MainPID and verify systemd recovery (live)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required with --execute on prod",
    )
    parser.add_argument(
        "--include-output-funnel",
        action="store_true",
        help="Also test mk04-output-funnel.service",
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

    if args.execute and env == "prod" and not args.confirm:
        print(
            "Error: prod live recovery requires --execute --confirm\n"
            "Example: python scripts/smoke/smoke_restart_recovery.py --env prod --execute --confirm",
            file=sys.stderr,
        )
        return 2

    services = services_for_run(include_output_funnel=bool(args.include_output_funnel))
    started = _utc_now()
    checks = run_policy_checks(services)
    mode = "policy-only"
    if args.execute:
        mode = "execute"
        checks.extend(run_execute_checks(env, services))

    report = SmokeReport(
        environment=env,
        mode=mode,
        started_at=started,
        finished_at=_utc_now(),
        checks=[asdict(c) for c in checks],
        overall=overall_from_checks(checks),
    )

    report_path = None
    if not args.no_report:
        report_path = write_report(report, env)

    print_summary(report, report_path)
    return 1 if report.overall == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
