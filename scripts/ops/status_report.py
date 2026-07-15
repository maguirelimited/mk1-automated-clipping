#!/usr/bin/env python3
"""Read-only operational status report for scripts/ops/status.sh."""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from boot_verification import build_boot_verification  # noqa: E402
from execution_lock import inspect_execution_lock  # noqa: E402
from run_records import latest_run_record  # noqa: E402
from ops_readonly import (  # noqa: E402
    REPO_ROOT,
    Line,
    canonical_env,
    compute_effective_scheduler,
    compute_effective_upload,
    discover_service_units,
    disk_usage_percent,
    ensure_config_scripts_on_path,
    env_label,
    format_bytes,
    git_commit,
    inspect_underlying_scheduler,
    load_runtime_scheduler_control,
    load_runtime_upload_control,
    load_update_status,
    mk04_env,
    run_command,
    scheduler_mode_for,
    sort_service_lines,
    systemd_not_running,
    systemd_unit_status,
    systemctl_available,
)

ensure_config_scripts_on_path()
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from config_manager import ConfigError, ConfigManager  # noqa: E402
from storage.disk_pressure import evaluate_disk_pressure, health_result_for_level  # noqa: E402

_FAIL_STATUSES = frozenset({"failed", "failure", "error", "cancelled", "canceled"})
_SUCCESS_STATUSES = frozenset({"completed", "success", "done", "succeeded"})
_RUNNING_STATUSES = frozenset({"running", "in_progress", "processing"})


@dataclass
class StatusReport:
    env_label: str = "UNKNOWN"
    mk04_env: str = "dev"
    lines: list[Line] = field(default_factory=list)
    service_lines: list[Line] = field(default_factory=list)
    activity_lines: list[Line] = field(default_factory=list)
    resource_lines: list[Line] = field(default_factory=list)
    overall: str = "WARN"
    config_error: str | None = None


def _posting_line(config_enabled: bool, runtime_disabled: bool | None, runtime_detail: str) -> Line:
    can_upload, detail = compute_effective_upload(config_enabled, runtime_disabled)
    merged_detail = runtime_detail or detail
    if runtime_disabled is True:
        return Line("Posting", "disabled by runtime control", merged_detail, "warn")
    if not config_enabled:
        return Line("Posting", "disabled by config", merged_detail)
    if can_upload is True:
        return Line("Posting", "enabled by config and runtime control", merged_detail)
    if runtime_disabled is None:
        return Line(
            "Posting",
            "enabled by config; runtime control not set",
            merged_detail,
            "warn",
        )
    return Line("Posting", "disabled by config", merged_detail)


def _scheduler_status(mk04_env_token: str, data_root: Path | None) -> Line:
    root = data_root if data_root is not None else REPO_ROOT / "data" / mk04_env_token
    runtime_disabled, runtime_detail = load_runtime_scheduler_control(root)
    underlying = inspect_underlying_scheduler(mk04_env_token, REPO_ROOT)
    effective, effective_detail = compute_effective_scheduler(
        runtime_disabled,
        underlying,
        mk04_env_token=mk04_env_token,
    )
    merged_detail = effective_detail or runtime_detail or underlying.detail

    if runtime_disabled is True:
        return Line(
            "Scheduler",
            "disabled by runtime control",
            (merged_detail or "stop-scheduler") + " (running jobs not interrupted)",
            "warn",
        )

    mode = scheduler_mode_for(mk04_env_token)
    if mode == "manual":
        return Line("Scheduler", "inactive (manual scheduler mode)", merged_detail)

    if effective == "enabled" and underlying.active is True:
        label = (
            "enabled by runtime control; cron active"
            if runtime_disabled is False
            else "enabled; cron active"
        )
        return Line("Scheduler", label, merged_detail)

    if underlying.mechanism == "not yet available":
        return Line("Scheduler", "not yet available", merged_detail, "warn")

    if effective == "disabled":
        severity = "warn" if runtime_disabled is not True else "warn"
        return Line("Scheduler", "disabled", merged_detail, severity)

    if effective == "unknown":
        return Line("Scheduler", "unknown", merged_detail, "warn")

    return Line("Scheduler", effective, merged_detail)


def _parse_iso_timestamp(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _job_dir_activity_rank(report: dict[str, Any], job_dir: Path) -> tuple[int, int, int, float]:
    """Prefer successful terminal jobs with clips over stale in-progress stubs."""
    status = str(report.get("status") or "").lower()
    clips = report.get("clips")
    clip_count = len(clips) if isinstance(clips, list) else 0
    success = 1 if status == "success" else 0
    terminal = 1 if status in {"success", "failed", "completed", "cancelled"} else 0
    try:
        mtime = float(job_dir.stat().st_mtime)
    except OSError:
        mtime = 0.0
    return (success, terminal, clip_count, mtime)


def _scan_job_activity(jobs_root: Path, *, max_dirs: int = 150) -> list[Line]:
    if not jobs_root.is_dir():
        return [
            Line("Queue pending", "not yet available"),
            Line("Running jobs", "not yet available"),
            Line("Failed jobs today", "not yet available"),
            Line("Last success", "not yet available"),
            Line("Last failure", "not yet available"),
        ]

    job_dirs = [p for p in jobs_root.iterdir() if p.is_dir()]
    job_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    job_dirs = job_dirs[:max_dirs]

    canonical_by_job_id: dict[str, tuple[Path, dict[str, Any]]] = {}
    for job_dir in job_dirs:
        report_path = job_dir / "report.json"
        if not report_path.is_file():
            continue
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict):
            continue
        job_id = str(report.get("job_id") or job_dir.name)
        rank = _job_dir_activity_rank(report, job_dir)
        previous = canonical_by_job_id.get(job_id)
        if previous is None or rank > _job_dir_activity_rank(previous[1], previous[0]):
            canonical_by_job_id[job_id] = (job_dir, report)

    running = 0
    failed_today = 0
    last_success: datetime | None = None
    last_failure: datetime | None = None
    today = datetime.now(timezone.utc).date()

    for _job_dir, report in canonical_by_job_id.values():
        status = str(report.get("status", "")).strip().lower()
        completed_at = _parse_iso_timestamp(report.get("completed_at"))
        started_at = _parse_iso_timestamp(report.get("started_at"))
        event_time = completed_at or started_at

        if status in _RUNNING_STATUSES:
            running += 1
        if status in _FAIL_STATUSES and event_time and event_time.date() == today:
            failed_today += 1
        if status in _SUCCESS_STATUSES and event_time:
            if last_success is None or event_time > last_success:
                last_success = event_time
        if status in _FAIL_STATUSES and event_time:
            if last_failure is None or event_time > last_failure:
                last_failure = event_time

    return [
        Line("Queue pending", "not yet available"),
        Line("Running jobs", str(running) if running else "0"),
        Line("Failed jobs today", str(failed_today)),
        Line(
            "Last success",
            last_success.isoformat().replace("+00:00", "Z") if last_success else "not yet available",
        ),
        Line(
            "Last failure",
            last_failure.isoformat().replace("+00:00", "Z") if last_failure else "not yet available",
        ),
    ]


def _disk_lines(path: Path, resolved: Any | None = None) -> list[Line]:
    if resolved is not None:
        try:
            status = evaluate_disk_pressure(resolved, path=path)
        except ValueError as exc:
            return [
                Line("Disk usage", "unknown", str(exc), "warn"),
                Line("Storage state", "unknown", str(exc), "warn"),
            ]
        if status.snapshot is None:
            detail = status.error or "disk usage unavailable"
            return [
                Line("Disk usage", "unknown", detail, "warn"),
                Line("Storage state", "unknown", detail, "warn"),
            ]
        result, severity = health_result_for_level(status.level)
        free_value = format_bytes(status.snapshot.free_bytes)
        retention_detail = (
            "retention recommended (operator-controlled)"
            if status.retention_recommended
            else ""
        )
        return [
            Line(
                "Disk usage",
                f"{status.snapshot.usage_percent:.1f}%",
                f"on {path}",
                severity if result != "PASS" else "info",
            ),
            Line("Free disk", free_value, f"on {path}", severity if result != "PASS" else "info"),
            Line(
                "Storage state",
                status.level.value,
                retention_detail,
                severity if result != "PASS" else "info",
            ),
        ]

    percent, error = disk_usage_percent(path)
    if percent is None:
        return [
            Line("Disk usage", "unknown", error, "warn"),
            Line("Free disk", "unknown", error, "warn"),
            Line("Storage state", "unknown", error, "warn"),
        ]
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return [
            Line("Disk usage", "unknown", str(exc), "warn"),
            Line("Free disk", "unknown", str(exc), "warn"),
            Line("Storage state", "unknown", str(exc), "warn"),
        ]
    severity = "warn" if percent >= 85 else "info"
    free_value = format_bytes(usage.free)
    return [
        Line("Disk usage", f"{percent}%", f"on {path}", severity),
        Line("Free disk", free_value, f"on {path}", severity),
        Line("Storage state", "unknown", "config unavailable", "warn"),
    ]


def _memory_usage() -> Line:
    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return Line("Memory usage", "unknown", "/proc/meminfo not available", "warn")
    values: dict[str, int] = {}
    try:
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            match = re.search(r"(\d+)", raw)
            if match:
                values[key.strip()] = int(match.group(1))
    except OSError as exc:
        return Line("Memory usage", "unknown", str(exc), "warn")

    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return Line("Memory usage", "unknown", "MemTotal/MemAvailable unavailable", "warn")
    used_pct = int(round((total - available) / total * 100))
    severity = "warn" if used_pct >= 90 else "info"
    return Line("Memory usage", f"{used_pct}%", "", severity)


def _cpu_usage() -> Line:
    stat_path = Path("/proc/stat")
    if not stat_path.is_file():
        return Line("CPU usage", "unknown", "/proc/stat not available", "warn")

    def _read_cpu() -> tuple[int, int] | None:
        try:
            first = stat_path.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError):
            return None
        if not first.startswith("cpu "):
            return None
        parts = [int(x) for x in first.split()[1:]]
        if len(parts) < 4:
            return None
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        return idle, total

    first = _read_cpu()
    if first is None:
        return Line("CPU usage", "unknown", "could not parse /proc/stat", "warn")
    time.sleep(0.15)
    second = _read_cpu()
    if second is None:
        return Line("CPU usage", "unknown", "could not re-read /proc/stat", "warn")
    idle_delta = second[0] - first[0]
    total_delta = second[1] - first[1]
    if total_delta <= 0:
        return Line("CPU usage", "unknown", "no CPU sample delta", "warn")
    used_pct = int(round((1 - idle_delta / total_delta) * 100))
    return Line("CPU usage", f"{used_pct}%")


def _gpu_usage() -> Line:
    import shutil

    if shutil.which("nvidia-smi") is None:
        return Line("GPU usage", "not available", "nvidia-smi not found")
    result = run_command(
        [
            "nvidia-smi",
            "--query-gpu=utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        timeout=10.0,
    )
    if result is None or result.returncode != 0:
        detail = (result.stderr.strip() if result else "nvidia-smi failed")[:120]
        return Line("GPU usage", "not available", detail)
    values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not values:
        return Line("GPU usage", "not available", "nvidia-smi returned no GPUs")
    if len(values) == 1:
        return Line("GPU usage", f"{values[0]}%")
    avg = sum(int(v) for v in values if v.isdigit()) / len(values)
    return Line("GPU usage", f"{avg:.0f}%", f"{len(values)} GPUs")


def _calculate_overall(report: StatusReport) -> str:
    if report.config_error:
        return "FAIL"
    for line in report.lines + report.service_lines + report.activity_lines + report.resource_lines:
        if line.severity == "fail" or line.value in {"FAIL", "NOT READY"}:
            return "FAIL"
    warn = False
    for line in report.lines + report.service_lines + report.activity_lines + report.resource_lines:
        if line.severity == "warn" or line.value in {"unknown", "not yet available"}:
            warn = True
            break
    for line in report.service_lines:
        if line.value in {"unknown", "not yet available"}:
            warn = True
    if warn:
        return "WARN"
    return "PASS"


def _format_line(label: str, value: str, detail: str = "") -> str:
    text = f"  {label + ':':<18} {value}"
    if detail:
        text += f" - {detail}"
    return text


def build_status_report(mk04_env_token: str) -> StatusReport:
    canonical = canonical_env(mk04_env_token)
    report = StatusReport(
        env_label=env_label(canonical),
        mk04_env=mk04_env(canonical),
    )

    commit, commit_detail = git_commit(REPO_ROOT)
    report.lines.append(Line("Code commit", commit, commit_detail))

    boot = build_boot_verification(mk04_env_token)
    boot_severity = "fail" if boot.overall == "NOT READY" else "info"
    if boot.overall == "READY" and any(c.result == "WARN" for c in boot.components):
        boot_severity = "warn"
    report.lines.append(
        Line(
            "Boot readiness",
            boot.overall,
            (
                "required components ready"
                if boot.overall == "READY"
                else "; ".join(
                    c.label for c in boot.components if c.required and c.result == "FAIL"
                )
                or "required component failed"
            ),
            boot_severity,
        )
    )

    try:
        resolved = ConfigManager.load(
            environment=canonical,
            config_root=REPO_ROOT / "config",
        )
    except ConfigError as exc:
        report.config_error = str(exc)[:300]
        report.lines.extend(
            [
                Line("Update status", "unknown", "config load failed"),
                Line("Posting", "unknown", "config load failed", "warn"),
            ]
        )
        report.service_lines = [
            Line(label, "not yet available", "config load failed")
            for label, _ in discover_service_units(REPO_ROOT)
        ]
        report.service_lines.append(
            Line("Scheduler", "not yet available", "config load failed")
        )
        report.activity_lines = _scan_job_activity(REPO_ROOT / "jobs" / report.mk04_env)
        report.resource_lines = [
            *_disk_lines(REPO_ROOT),
            _cpu_usage(),
            _memory_usage(),
            _gpu_usage(),
        ]
        report.overall = _calculate_overall(report)
        return report

    update_status, update_detail = load_update_status(resolved.state_paths.data_root)
    report.lines.append(Line("Update status", update_status, update_detail, "fail" if update_status == "FAIL" else "info"))

    runtime_disabled, runtime_detail = load_runtime_upload_control(resolved.state_paths.data_root)
    report.lines.append(_posting_line(bool(resolved.uploading_enabled), runtime_disabled, runtime_detail))

    for label, unit in discover_service_units(REPO_ROOT):
        value, detail, severity = systemd_unit_status(unit)
        report.service_lines.append(Line(label, value, detail, severity))
    report.service_lines = sort_service_lines(report.service_lines)
    report.service_lines.append(_scheduler_status(report.mk04_env, resolved.state_paths.data_root))
    lock_inspection = inspect_execution_lock(report.mk04_env)
    if not lock_inspection.present:
        report.service_lines.append(Line("Execution lock", "none", lock_inspection.detail))
    elif lock_inspection.stale:
        report.service_lines.append(
            Line("Execution lock", "stale", lock_inspection.detail, "warn")
        )
    else:
        report.service_lines.append(
            Line("Execution lock", "held", lock_inspection.detail, "warn")
        )

    last_run = latest_run_record(report.mk04_env)
    if last_run is None:
        report.activity_lines.append(Line("Last pipeline run", "none", "no run records yet"))
    else:
        severity = "warn" if last_run.status in {"FAIL", "SKIPPED"} else "info"
        report.activity_lines.append(
            Line(
                "Last pipeline run",
                last_run.status,
                f"{last_run.run_id} trigger={last_run.trigger}",
                severity,
            )
        )

    report.activity_lines.extend(_scan_job_activity(resolved.state_paths.jobs_root))

    disk_root = resolved.state_paths.data_root
    if not disk_root.exists():
        disk_root = REPO_ROOT
    report.resource_lines = [
        *_disk_lines(disk_root, resolved),
        _cpu_usage(),
        _memory_usage(),
        _gpu_usage(),
    ]

    report.overall = _calculate_overall(report)
    return report


def render_report(report: StatusReport) -> str:
    out: list[str] = ["Remote Operations Status", ""]
    out.append(f"Environment:        {report.env_label}")
    for line in report.lines:
        text = f"{line.label + ':':<18} {line.value}"
        if line.detail:
            text += f" - {line.detail}"
        out.append(text)
    out.append("")
    out.append("Services:")
    for line in report.service_lines:
        out.append(_format_line(line.label, line.value, line.detail))
    out.append("")
    out.append("Activity:")
    for line in report.activity_lines:
        out.append(_format_line(line.label, line.value, line.detail))
    out.append("")
    out.append("Resources:")
    for line in report.resource_lines:
        out.append(_format_line(line.label, line.value, line.detail))
    out.append("")
    if report.config_error:
        out.append(f"Config error:       {report.config_error}")
        out.append("")
    out.append(f"Overall status:     {report.overall}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    if not argv:
        argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help"}:
        print(
            "Usage: status_report.py <dev|prod>\n"
            "Read-only status collector used by scripts/ops/status.sh."
        )
        return 0 if argv and argv[0] in {"-h", "--help"} else 1
    try:
        canonical_env(argv[0])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    report = build_status_report(argv[0])
    print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
