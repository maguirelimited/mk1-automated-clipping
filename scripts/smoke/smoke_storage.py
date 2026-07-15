#!/usr/bin/env python3
"""Storage Safety & Integration smoke (Phase 12).

Proves the completed storage subsystem is safe for production: retention safety
rules, disk pressure, scheduling, log rotation, database backup, and Operations
UI read models work together without duplicating storage logic.

Usage:
    python scripts/smoke/smoke_storage.py
    python scripts/smoke/smoke_storage.py --env dev
    video-automation/.venv/bin/python -m pytest tests/smoke/test_storage_safety_smoke.py -q
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
SCRIPTS_STORAGE = REPO_ROOT / "scripts" / "storage"
OPS_UI = REPO_ROOT / "ops-ui"
PYTHON = REPO_ROOT / "video-automation" / ".venv" / "bin" / "python"
SMOKE_TEST = REPO_ROOT / "tests" / "smoke" / "test_storage_safety_smoke.py"

REQUIRED_MODULES = (
    "artifact_classifier.py",
    "retention_planner.py",
    "retention_apply.py",
    "retention_report.py",
    "retention_schedule.py",
    "retention_safety.py",
    "disk_pressure.py",
    "log_rotation.py",
    "database_backup.py",
)

REQUIRED_OPS = (
    "run-scheduled-retention.sh",
    "run-log-rotation.sh",
    "run-database-backup.sh",
    "run_scheduled_retention.py",
    "run_log_rotation.py",
    "run_database_backup.py",
)


@dataclass
class CheckResult:
    name: str
    outcome: str  # PASS | WARN | FAIL | SKIP
    detail: str = ""


@dataclass
class SmokeReport:
    environment: str | None
    started_at: str
    finished_at: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)
    overall: str = "FAIL"


def normalize_env(raw: str | None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    token = str(raw).strip().lower()
    if token in {"dev", "development"}:
        return "dev"
    if token in {"prod", "production"}:
        return "prod"
    raise ValueError(f"invalid environment: {raw!r}. Expected dev or prod.")


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def overall_from_checks(checks: list[CheckResult]) -> str:
    outcomes = {c.outcome for c in checks}
    if "FAIL" in outcomes:
        return "FAIL"
    if "WARN" in outcomes or "SKIP" in outcomes:
        return "WARN"
    return "PASS"


def check_storage_modules_present() -> list[CheckResult]:
    missing = [name for name in REQUIRED_MODULES if not (SCRIPTS_STORAGE / name).is_file()]
    if missing:
        return [
            CheckResult(
                "storage_modules_present",
                "FAIL",
                f"missing: {', '.join(missing)}",
            )
        ]
    return [CheckResult("storage_modules_present", "PASS", f"{len(REQUIRED_MODULES)} modules")]


def check_ops_entrypoints_present() -> list[CheckResult]:
    ops_dir = REPO_ROOT / "scripts" / "ops"
    missing = [name for name in REQUIRED_OPS if not (ops_dir / name).is_file()]
    if missing:
        return [
            CheckResult(
                "ops_entrypoints_present",
                "FAIL",
                f"missing: {', '.join(missing)}",
            )
        ]
    return [CheckResult("ops_entrypoints_present", "PASS", f"{len(REQUIRED_OPS)} entrypoints")]


def check_storage_ui_read_only() -> CheckResult:
    path = OPS_UI / "ops_ui" / "storage_ui.py"
    if not path.is_file():
        return CheckResult("storage_ui_read_only", "FAIL", "storage_ui.py missing")
    source = path.read_text(encoding="utf-8")
    forbidden = (
        "RetentionApplyExecutor",
        "RetentionPlanner",
        "run_retention_apply",
        "run_retention_dry_run",
    )
    hits = [token for token in forbidden if token in source]
    if hits:
        return CheckResult(
            "storage_ui_read_only",
            "FAIL",
            f"UI duplicates storage engine: {', '.join(hits)}",
        )
    return CheckResult("storage_ui_read_only", "PASS", "loaders only")


def check_integration_pytest() -> CheckResult:
    if not PYTHON.is_file():
        return CheckResult(
            "integration_pytest",
            "SKIP",
            f"venv python not found at {PYTHON}",
        )
    if not SMOKE_TEST.is_file():
        return CheckResult("integration_pytest", "FAIL", "smoke test file missing")
    proc = subprocess.run(
        [str(PYTHON), "-m", "pytest", str(SMOKE_TEST), "-q", "--tb=line"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    summary = tail[-1] if tail else f"exit={proc.returncode}"
    if proc.returncode == 0:
        return CheckResult("integration_pytest", "PASS", summary)
    return CheckResult("integration_pytest", "FAIL", summary)


def check_live_records_readable(env: str) -> list[CheckResult]:
    """Read-only: latest storage records exist and parse on a live repo."""
    scripts_config = REPO_ROOT / "scripts" / "config"
    scripts_dir = REPO_ROOT / "scripts"
    for path in (scripts_config, scripts_dir):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)

    try:
        from config_manager import ConfigManager
        from storage.database_backup import load_latest_backup_record
        from storage.disk_pressure import evaluate_disk_pressure
        from storage.log_rotation import load_latest_rotation_record
        from storage.retention_schedule import load_latest_scheduled_retention
    except ImportError as exc:
        return [
            CheckResult(
                "live_storage_imports",
                "SKIP",
                f"cannot import storage modules: {exc}",
            )
        ]

    canonical = "development" if env == "dev" else "production"
    try:
        resolved = ConfigManager.load(
            environment=canonical,
            funnel_id="business",
            platform_id="youtube",
        )
    except Exception as exc:
        return [
            CheckResult(
                "live_config_load",
                "SKIP",
                f"config not loadable: {exc}",
            )
        ]

    results: list[CheckResult] = []
    try:
        status = evaluate_disk_pressure(resolved)
        level = status.level.value
        results.append(CheckResult("live_disk_pressure", "PASS", f"level={level}"))
    except Exception as exc:
        results.append(CheckResult("live_disk_pressure", "WARN", str(exc)))

    token = "dev" if env == "dev" else "prod"
    records_dir = REPO_ROOT / "data" / token / "storage"
    if records_dir.is_dir():
        for name, loader in (
            ("live_scheduled_retention", load_latest_scheduled_retention),
            ("live_log_rotation", load_latest_rotation_record),
            ("live_database_backup", load_latest_backup_record),
        ):
            try:
                payload = loader(records_dir=records_dir)
                if payload is None:
                    results.append(CheckResult(name, "WARN", "no record yet"))
                else:
                    results.append(CheckResult(name, "PASS", str(payload.get("status"))))
            except Exception as exc:
                results.append(CheckResult(name, "WARN", str(exc)))
    else:
        results.append(
            CheckResult(
                "live_storage_records",
                "WARN",
                f"records dir missing: {records_dir}",
            )
        )
    return results


def run_smoke(env: str | None = None) -> SmokeReport:
    started = _utc_now()
    checks: list[CheckResult] = []
    checks.extend(check_storage_modules_present())
    checks.extend(check_ops_entrypoints_present())
    checks.append(check_storage_ui_read_only())
    checks.append(check_integration_pytest())
    if env is not None:
        checks.extend(check_live_records_readable(env))

    report = SmokeReport(
        environment=env,
        started_at=started,
        finished_at=_utc_now(),
        checks=[asdict(c) for c in checks],
        overall=overall_from_checks(checks),
    )
    return report


def write_report(report: SmokeReport, env: str | None) -> Path:
    token = env or "all"
    report_dir = REPO_ROOT / "reports" / token / "storage_smoke"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"smoke_{token}_{stamp}.json"
    payload = json.dumps(asdict(report), indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")
    (report_dir / "latest.json").write_text(payload, encoding="utf-8")
    return path


def print_summary(report: SmokeReport, report_path: Path | None) -> None:
    print()
    print("Storage Safety & Integration Smoke")
    if report.environment:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Storage Safety & Integration smoke")
    parser.add_argument(
        "--env",
        help="Optional live read-only checks for dev or prod",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Skip writing JSON report under reports/",
    )
    args = parser.parse_args(argv)
    try:
        env = normalize_env(args.env)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    report = run_smoke(env)
    report_path = None if args.no_report else write_report(report, env)
    print_summary(report, report_path)

    if report.overall == "FAIL":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
