#!/usr/bin/env python3
"""Read-only health diagnostics for scripts/ops/health.sh."""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from boot_verification import (  # noqa: E402
    BootVerification,
    build_boot_verification,
    render_boot_verification,
)
from execution_lock import inspect_execution_lock  # noqa: E402
from run_records import latest_run_record  # noqa: E402
from ops_readonly import (  # noqa: E402
    DEFAULT_PORTS,
    ENV_EXAMPLES,
    REPO_ROOT,
    RUNTIME_ENV_FILES,
    canonical_env,
    compute_effective_scheduler,
    compute_effective_upload,
    discover_service_units,
    ensure_config_scripts_on_path,
    env_label,
    gpu_visibility_check,
    http_probe,
    inspect_underlying_scheduler,
    load_runtime_scheduler_control,
    load_runtime_upload_control,
    mk04_env,
    mk04_schedule_configured,
    path_under_code_or_releases,
    probe_runtime_cache_write,
    production_runtime_authority,
    run_command,
    scheduler_mode_for,
    service_health_urls,
    systemd_not_running,
    systemd_unit_status,
    systemctl_available,
    unit_description,
)

# Optional services may WARN when unreachable; they must not fail overall health.
_OPTIONAL_SERVICE_LABELS = frozenset({"AI service", "Operations UI"})

ensure_config_scripts_on_path()
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from config_manager import ConfigError, ConfigManager  # noqa: E402
from state_paths import EnvironmentStatePaths  # noqa: E402
from storage.disk_pressure import (  # noqa: E402
    evaluate_disk_pressure,
    format_health_detail,
    health_result_for_level,
)


@dataclass
class HealthCheck:
    label: str
    result: str
    detail: str = ""
    severity: str = "info"  # info | warn | fail


@dataclass
class HealthReport:
    env_label: str = "UNKNOWN"
    mk04_env: str = "dev"
    is_production: bool = False
    checks: list[HealthCheck] = field(default_factory=list)
    overall: str = "WARN"
    boot: BootVerification | None = None


def _check_config_validation(canonical: str) -> tuple[HealthCheck, Any | None]:
    try:
        resolved = ConfigManager.load(
            environment=canonical,
            config_root=REPO_ROOT / "config",
        )
    except ConfigError as exc:
        return (
            HealthCheck(
                "Config validation",
                "FAIL",
                str(exc)[:200],
                "fail",
            ),
            None,
        )
    return HealthCheck("Config validation", "PASS", "ConfigManager load succeeded"), resolved


def _check_required_env_file(mk04_env_token: str, *, is_production: bool) -> HealthCheck:
    runtime_path = RUNTIME_ENV_FILES[mk04_env_token]
    example_path = ENV_EXAMPLES[mk04_env_token]

    if runtime_path.is_file():
        return HealthCheck("Required env file", "PASS", f"present at {runtime_path}")

    if is_production:
        detail = f"missing required runtime env file {runtime_path}"
        if example_path.is_file():
            detail += f"; template exists at {example_path}"
        return HealthCheck("Required env file", "FAIL", detail, "fail")

    if example_path.is_file():
        return HealthCheck(
            "Required env file",
            "WARN",
            f"runtime env missing ({runtime_path}); repo template {example_path} exists",
            "warn",
        )
    return HealthCheck(
        "Required env file",
        "WARN",
        f"runtime env missing ({runtime_path})",
        "warn",
    )


def _api_health_endpoint(mk04_env_token: str) -> HealthCheck:
    port = DEFAULT_PORTS[mk04_env_token]["input"]
    url = f"http://127.0.0.1:{port}/healthz"
    unit_path = REPO_ROOT / "deploy/systemd/mk04-source-input.service"
    if unit_path.is_file():
        description = unit_description(unit_path)
        if description and "source-input" not in description.lower():
            return HealthCheck(
                "API health endpoint",
                "WARN",
                f"source-input unit description unexpected: {description[:80]}",
                "warn",
            )
    ok, detail = http_probe(url)
    if ok:
        return HealthCheck("API health endpoint", "PASS", detail)
    systemd_state, systemd_detail, severity = systemd_unit_status("mk04-source-input.service")
    if systemd_state == "FAIL":
        return HealthCheck("API health endpoint", "FAIL", detail or systemd_detail, "fail")
    if systemd_state == "not yet available":
        return HealthCheck(
            "API health endpoint",
            "not yet available",
            detail or "source-input service not verified on this host",
        )
    return HealthCheck("API health endpoint", "FAIL", detail or systemd_detail, severity or "fail")


def _http_service_reachable(label: str, url: str) -> tuple[bool, str]:
    """Return whether the service HTTP endpoint is reachable (process listening)."""
    ok, detail = http_probe(url)
    if ok:
        return True, detail
    # Ops UI may require auth; 401 still proves the process is up.
    if label == "Operations UI" and detail.startswith("HTTP 401"):
        return True, f"{detail} (authentication required; service reachable)"
    return False, detail


def _service_health(label: str, unit: str, mk04_env_token: str) -> HealthCheck:
    unit_path = REPO_ROOT / "deploy/systemd" / unit
    if not unit_path.is_file():
        return HealthCheck(label, "not yet available", f"unit file missing in repo: {unit}")

    expected = {
        "API": "source-input",
        "Worker": "video-automation",
        "AI service": "ai-service",
        "Operations UI": "ops-ui",
        "Output funnel": "output-funnel",
    }.get(label)
    description = unit_description(unit_path).lower()
    if expected and expected not in description and description:
        return HealthCheck(
            label,
            "WARN",
            f"unit description {description[:80]!r} does not match expected {expected}",
            "warn",
        )

    optional = label in _OPTIONAL_SERVICE_LABELS
    urls = service_health_urls(mk04_env_token)
    url = urls.get(label)
    http_detail = ""
    if url:
        reachable, http_detail = _http_service_reachable(label, url)
        if reachable:
            return HealthCheck(label, "PASS", f"{url} ({http_detail})")

    value, systemd_detail, severity = systemd_unit_status(unit)
    if value == "not yet available":
        fallback = http_detail or systemd_detail
        if optional and fallback:
            return HealthCheck(label, "WARN", fallback, "warn")
        return HealthCheck(label, "not yet available", fallback or systemd_detail)
    if value == "unknown":
        return HealthCheck(label, "unknown", http_detail or systemd_detail, "warn")

    if value == "PASS" and url:
        # systemd active but HTTP probe failed — trust HTTP for readiness.
        result = "WARN" if optional else "FAIL"
        sev = "warn" if optional else "fail"
        detail = f"{url} unreachable ({http_detail})"
        if systemd_detail:
            detail += f"; {systemd_detail}"
        return HealthCheck(label, result, detail, sev)

    detail = http_detail or systemd_detail
    if optional:
        return HealthCheck(label, "WARN", detail, "warn")
    return HealthCheck(label, value, detail, severity or "fail")


def _scheduler_health(mk04_env_token: str, data_root: Path | None) -> HealthCheck:
    root = data_root if data_root is not None else REPO_ROOT / "data" / mk04_env_token
    runtime_disabled, _ = load_runtime_scheduler_control(root)
    underlying = inspect_underlying_scheduler(mk04_env_token, REPO_ROOT)
    effective, detail = compute_effective_scheduler(
        runtime_disabled,
        underlying,
        mk04_env_token=mk04_env_token,
    )
    mode = scheduler_mode_for(mk04_env_token)

    # Intentional off: manual policy and/or runtime disable with no MK04 schedule.
    if mode == "manual":
        configured, _ = mk04_schedule_configured(repo_root=REPO_ROOT)
        bits = ["manual scheduler mode"]
        if runtime_disabled is True:
            bits.append("scheduler_disabled=true")
        if not configured:
            bits.append("no MK04 timer/cron")
        return HealthCheck(
            "Scheduler",
            "PASS",
            "; ".join(bits) + " (intentionally disabled)",
        )

    if runtime_disabled is True:
        return HealthCheck(
            "Scheduler",
            "PASS",
            "scheduler_disabled=true; autonomous scheduling intentionally paused "
            "(running jobs not interrupted)",
        )

    configured, artifact = mk04_schedule_configured(repo_root=REPO_ROOT)
    if not configured:
        return HealthCheck(
            "Scheduler",
            "WARN",
            detail or "autonomous mode configured but no MK04 timer/cron artifact",
            "warn",
        )

    if underlying.active is True:
        return HealthCheck("Scheduler", "PASS", f"MK04 schedule active ({artifact})")
    if underlying.active is False:
        return HealthCheck(
            "Scheduler",
            "FAIL",
            detail or f"MK04 schedule configured ({artifact}) but host cron inactive",
            "fail",
        )
    if effective == "disabled":
        return HealthCheck("Scheduler", "WARN", detail or "scheduler effectively disabled", "warn")
    return HealthCheck("Scheduler", "not yet available", detail or "scheduler state unknown")


def _runtime_service_db_parents(*, is_production: bool) -> HealthCheck:
    """Report runtime service DB parent health (never release-local placeholders)."""
    runtime = (os.environ.get("MK04_RUNTIME_ROOT") or "").strip()
    if not runtime:
        return HealthCheck(
            "Service database parents",
            "WARN" if not is_production else "FAIL",
            "MK04_RUNTIME_ROOT unset; cannot locate service DB parents",
            "warn" if not is_production else "fail",
        )
    runtime_path = Path(runtime).expanduser()
    if path_under_code_or_releases(runtime_path):
        return HealthCheck(
            "Service database parents",
            "FAIL",
            f"MK04_RUNTIME_ROOT under code/releases: {runtime_path}",
            "fail",
        )

    details: list[str] = []
    missing: list[str] = []
    for label, env_name, default_rel in (
        ("output-funnel", "OUTPUT_FUNNEL_DB", "output-funnel/output_funnel.sqlite3"),
        ("ops-ui", "OPS_UI_DB", "ops-ui/ops_ui.sqlite3"),
    ):
        raw = (os.environ.get(env_name) or "").strip()
        path = Path(raw).expanduser() if raw else (runtime_path / default_rel)
        if path_under_code_or_releases(path):
            missing.append(f"{label} resolves under code/releases ({path})")
            continue
        parent = path.parent
        if parent.is_dir():
            details.append(f"{label}:{parent}")
        else:
            missing.append(f"{label} parent missing: {parent}")

    if missing and is_production:
        return HealthCheck(
            "Service database parents",
            "WARN",
            "; ".join(missing),
            "warn",
        )
    if missing:
        return HealthCheck(
            "Service database parents",
            "WARN",
            "; ".join(missing),
            "warn",
        )
    return HealthCheck("Service database parents", "PASS", "; ".join(details))


def _database_access(state: EnvironmentStatePaths, *, is_production: bool) -> HealthCheck:
    """Optional ConfigManager placeholder DB — never blocks readiness; never under releases."""
    if is_production:
        # Deployed production reports runtime service DB parents instead of a
        # release-local optional placeholder path.
        return _runtime_service_db_parents(is_production=True)

    db_path = state.database_path
    if path_under_code_or_releases(db_path):
        return HealthCheck(
            "Config database (optional)",
            "WARN",
            f"optional placeholder must not resolve under code/releases: {db_path}",
            "warn",
        )

    parent = db_path.parent
    label = "Config database (optional)"

    if not parent.exists():
        return HealthCheck(
            label,
            "WARN",
            f"optional placeholder DB parent missing: {parent}",
            "warn",
        )

    if not db_path.exists():
        return HealthCheck(
            label,
            "WARN",
            f"optional placeholder database not present: {db_path}",
            "warn",
        )

    if not os.access(db_path, os.R_OK):
        return HealthCheck(
            label,
            "WARN",
            f"optional placeholder database not readable: {db_path}",
            "warn",
        )

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return HealthCheck(label, "WARN", f"optional placeholder unreadable: {str(exc)[:120]}", "warn")

    return HealthCheck(label, "PASS", f"optional placeholder present: {db_path}")


def _output_path_write_test(state: EnvironmentStatePaths, *, is_production: bool) -> HealthCheck:
    if is_production:
        ok, detail = production_runtime_authority()
        if not ok:
            return HealthCheck("Output path write test", "FAIL", detail, "fail")

    cache_dir = state.data_root / "cache"
    try:
        state.assert_within_environment(cache_dir)
    except ValueError as exc:
        return HealthCheck("Output path write test", "FAIL", str(exc)[:120], "fail")

    if path_under_code_or_releases(cache_dir):
        return HealthCheck(
            "Output path write test",
            "FAIL",
            f"refusing write under code/releases: {cache_dir}",
            "fail",
        )

    ok, detail = probe_runtime_cache_write(cache_dir, probe_prefix=".health_write_probe")
    if not ok:
        return HealthCheck("Output path write test", "FAIL", detail, "fail")
    return HealthCheck("Output path write test", "PASS", detail)


def _disk_pressure(resolved: Any, state: EnvironmentStatePaths) -> HealthCheck:
    path = state.data_root if state.data_root.exists() else REPO_ROOT
    try:
        status = evaluate_disk_pressure(resolved, path=path)
    except ValueError as exc:
        return HealthCheck("Disk pressure", "unknown", str(exc), "warn")

    if status.snapshot is None:
        return HealthCheck(
            "Disk pressure",
            "unknown",
            status.error or "disk usage unavailable",
            "warn",
        )

    result, severity = health_result_for_level(status.level)
    return HealthCheck("Disk pressure", result, format_health_detail(status), severity)


def _upload_safety_state(
    resolved: Any,
    state: EnvironmentStatePaths,
    *,
    is_production: bool,
) -> HealthCheck:
    config_enabled = bool(resolved.uploading_enabled)
    runtime_disabled, runtime_detail = load_runtime_upload_control(state.data_root)
    can_upload, effective_detail = compute_effective_upload(config_enabled, runtime_disabled)
    detail = runtime_detail or effective_detail

    if not is_production:
        if can_upload is True:
            return HealthCheck(
                "Upload safety state",
                "FAIL",
                "dev uploads effectively enabled",
                "fail",
            )
        if runtime_disabled is True:
            return HealthCheck(
                "Upload safety state",
                "WARN",
                "dev uploads disabled by runtime control",
                "warn",
            )
        return HealthCheck("Upload safety state", "PASS", "dev uploads disabled by config")

    if runtime_disabled is True:
        return HealthCheck(
            "Upload safety state",
            "WARN",
            "prod uploads disabled by runtime control",
            "warn",
        )
    if can_upload is True:
        return HealthCheck(
            "Upload safety state",
            "PASS",
            "prod uploads enabled by config and runtime control",
        )
    if can_upload is False and not config_enabled:
        return HealthCheck("Upload safety state", "PASS", "prod uploads disabled by config")
    return HealthCheck(
        "Upload safety state",
        "WARN",
        detail or "prod effective upload state unknown",
        "warn",
    )


def _execution_lock_health(mk04_env_token: str) -> HealthCheck:
    inspection = inspect_execution_lock(mk04_env_token)
    if not inspection.present:
        return HealthCheck("Execution lock", "PASS", "no execution lock present")
    if inspection.stale:
        return HealthCheck(
            "Execution lock",
            "WARN",
            inspection.detail,
            "warn",
        )
    return HealthCheck(
        "Execution lock",
        "WARN",
        inspection.detail,
        "warn",
    )


def _last_run_health(mk04_env_token: str) -> HealthCheck:
    record = latest_run_record(mk04_env_token)
    if record is None:
        return HealthCheck("Last pipeline run", "PASS", "no run records yet")
    detail = (
        f"{record.run_id} status={record.status} trigger={record.trigger}"
    )
    if record.failure_reason:
        detail += f" reason={record.failure_reason[:120]}"
    if record.status == "FAIL":
        return HealthCheck("Last pipeline run", "WARN", detail, "warn")
    if record.status == "SKIPPED":
        return HealthCheck("Last pipeline run", "WARN", detail, "warn")
    return HealthCheck("Last pipeline run", "PASS", detail)


def _calculate_overall(report: HealthReport) -> str:
    if report.boot is not None and report.boot.overall == "NOT READY":
        return "FAIL"
    if any(check.result == "FAIL" or check.severity == "fail" for check in report.checks):
        return "FAIL"
    important_unknown = {
        "Config validation",
        "Required env file",
        "API health endpoint",
        "Config database (optional)",
        "Service database parents",
        "Output path write test",
        "Upload safety state",
        "Runtime path authority",
    }
    for check in report.checks:
        if check.result in {"WARN", "unknown"} or check.severity == "warn":
            return "WARN"
        if check.result == "not yet available" and check.label in important_unknown:
            return "WARN"
        if report.is_production and check.result == "not yet available" and check.label in {
            "Scheduler",
            "Config database (optional)",
            "Service database parents",
        }:
            return "WARN"
    for check in report.checks:
        if check.result in {"not yet available", "unknown"}:
            return "WARN"
    return "PASS"


def _exit_code(overall: str) -> int:
    if overall == "FAIL":
        return 2
    if overall == "WARN":
        return 1
    return 0


def build_health_report(mk04_env_token: str) -> HealthReport:
    canonical = canonical_env(mk04_env_token)
    report = HealthReport(
        env_label=env_label(canonical),
        mk04_env=mk04_env(canonical),
        is_production=canonical == "production",
    )

    authority_ok = True
    if report.is_production:
        authority_ok, authority_detail = production_runtime_authority()
        report.checks.append(
            HealthCheck(
                "Runtime path authority",
                "PASS" if authority_ok else "FAIL",
                authority_detail,
                "info" if authority_ok else "fail",
            )
        )

    report.boot = build_boot_verification(mk04_env_token)
    boot_severity = "fail" if report.boot.overall == "NOT READY" else "info"
    if report.boot.overall == "READY" and any(c.result == "WARN" for c in report.boot.components):
        boot_severity = "warn"
    report.checks.append(
        HealthCheck(
            "Boot readiness",
            report.boot.overall,
            (
                "required components ready"
                if report.boot.overall == "READY"
                else "one or more required components failed"
            ),
            boot_severity,
        )
    )

    config_check, resolved = _check_config_validation(canonical)
    report.checks.append(config_check)

    report.checks.append(_check_required_env_file(report.mk04_env, is_production=report.is_production))
    report.checks.append(_api_health_endpoint(report.mk04_env))

    for label, unit in discover_service_units():
        if label == "API":
            continue
        report.checks.append(_service_health(label, unit, report.mk04_env))

    scheduler_data_root = None
    if resolved is not None:
        scheduler_data_root = EnvironmentStatePaths.from_resolved_config(resolved).data_root
    report.checks.append(_scheduler_health(report.mk04_env, scheduler_data_root))

    if not authority_ok:
        report.checks.extend(
            [
                HealthCheck(
                    "Service database parents",
                    "FAIL",
                    "skipped: production runtime authority missing/mismatched",
                    "fail",
                ),
                HealthCheck(
                    "Output path write test",
                    "FAIL",
                    "skipped: production runtime authority missing/mismatched",
                    "fail",
                ),
                HealthCheck("Disk pressure", "unknown", "skipped: runtime authority failed", "warn"),
                HealthCheck("Upload safety state", "FAIL", "skipped: runtime authority failed", "fail"),
            ]
        )
    elif resolved is None:
        report.checks.extend(
            [
                HealthCheck("Config database (optional)", "not yet available", "config validation failed"),
                HealthCheck("Output path write test", "not yet available", "config validation failed"),
                HealthCheck("Disk pressure", "unknown", "config validation failed", "warn"),
                HealthCheck("Upload safety state", "FAIL", "config validation failed", "fail"),
            ]
        )
    else:
        state = EnvironmentStatePaths.from_resolved_config(resolved)
        report.checks.append(_database_access(state, is_production=report.is_production))
        report.checks.append(
            _output_path_write_test(state, is_production=report.is_production)
        )
        report.checks.append(_disk_pressure(resolved, state))
        gpu_result, gpu_detail = gpu_visibility_check()
        report.checks.append(HealthCheck("GPU visible", gpu_result, gpu_detail))
        report.checks.append(
            _upload_safety_state(resolved, state, is_production=report.is_production)
        )

    report.checks.append(HealthCheck("Queue state", "not yet available"))
    report.checks.append(_execution_lock_health(report.mk04_env))
    report.checks.append(_last_run_health(report.mk04_env))

    report.overall = _calculate_overall(report)
    return report


def render_health_report(report: HealthReport) -> str:
    lines: list[str] = []
    if report.boot is not None:
        lines.append(render_boot_verification(report.boot))
        lines.append("")
    lines.extend(
        [
            "Remote Operations Health Check",
            "",
            f"Environment: {report.env_label}",
            "",
        ]
    )
    for check in report.checks:
        text = f"{check.label:<24} {check.result}"
        if check.detail:
            text += f" - {check.detail}"
        lines.append(text)
    lines.extend(["", f"Overall{' ' * 18} {report.overall}"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if not argv:
        argv = sys.argv[1:]
    boot_readiness = False
    filtered: list[str] = []
    for tok in argv:
        if tok == "--boot-readiness":
            boot_readiness = True
            continue
        filtered.append(tok)
    argv = filtered
    if not argv or argv[0] in {"-h", "--help"}:
        print(
            "Usage: health_report.py <dev|prod> [--boot-readiness]\n"
            "Read-only health collector used by scripts/ops/health.sh.\n"
            "Includes boot verification (READY / NOT READY) for core components.\n"
            "Exit codes (default): 0=PASS, 1=WARN, 2=FAIL\n"
            "Exit codes (--boot-readiness): 0=READY, 1=READY with optional warnings, "
            "2=NOT READY / missing boot result"
        )
        return 0 if argv and argv[0] in {"-h", "--help"} else 1
    try:
        canonical_env(argv[0])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 64
    report = build_health_report(argv[0])
    print(render_health_report(report))
    if boot_readiness:
        return _boot_readiness_exit_code(report)
    return _exit_code(report.overall)


def _boot_readiness_exit_code(report: HealthReport) -> int:
    """Promotion/activation gate: explicit boot contract, not Overall PASS.

    Preserves the full human health report on stdout while exiting on
    Boot readiness READY / NOT READY. Optional component WARN → exit 1 (still READY).
    """
    if report.boot is None:
        return 2
    if report.boot.overall == "NOT READY":
        return 2
    if report.boot.overall != "READY":
        return 2
    if any(c.result == "WARN" for c in report.boot.components):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
