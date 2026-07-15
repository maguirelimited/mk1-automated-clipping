#!/usr/bin/env python3
"""Boot verification for Reliability & Recovery Phase 5.

Reports whether the environment is READY or NOT READY after boot / recovery.
Uses HTTP readiness for services (not process start alone). Required failures
block READY; optional services (AI, Operations UI) may WARN without blocking.

Does not implement execution locks, run records, or job recovery.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ops_readonly import (  # noqa: E402
    REPO_ROOT,
    RUNTIME_ENV_FILES,
    canonical_env,
    compute_effective_scheduler,
    ensure_config_scripts_on_path,
    env_label,
    http_probe,
    inspect_underlying_scheduler,
    load_runtime_scheduler_control,
    mk04_env,
    mk04_schedule_configured,
    path_under_code_or_releases,
    probe_runtime_cache_write,
    production_runtime_authority,
    scheduler_mode_for,
    service_health_urls,
)

ensure_config_scripts_on_path()
from config_manager import ConfigError, ConfigManager  # noqa: E402
from state_paths import EnvironmentStatePaths  # noqa: E402


@dataclass
class BootComponent:
    label: str
    result: str  # PASS | FAIL | WARN
    detail: str = ""
    required: bool = True


@dataclass
class BootVerification:
    environment: str
    env_label: str
    components: list[BootComponent] = field(default_factory=list)
    overall: str = "NOT READY"  # READY | NOT READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "env_label": self.env_label,
            "overall": self.overall,
            "components": [asdict(c) for c in self.components],
            "required_failures": [
                c.label for c in self.components if c.required and c.result == "FAIL"
            ],
            "optional_warnings": [
                c.label
                for c in self.components
                if (not c.required) and c.result in {"FAIL", "WARN"}
            ],
        }


def _component(
    label: str,
    *,
    ok: bool,
    detail: str,
    required: bool,
) -> BootComponent:
    if ok:
        return BootComponent(label, "PASS", detail, required)
    if required:
        return BootComponent(label, "FAIL", detail, required)
    return BootComponent(label, "WARN", detail, required)


def _check_config(canonical: str) -> tuple[BootComponent, Any | None]:
    try:
        resolved = ConfigManager.load(
            environment=canonical,
            config_root=REPO_ROOT / "config",
        )
    except ConfigError as exc:
        return _component("Config", ok=False, detail=str(exc)[:200], required=True), None
    return _component("Config", ok=True, detail="ConfigManager load succeeded", required=True), resolved


def _check_env_file(mk04_env_token: str, *, is_production: bool) -> BootComponent:
    path = RUNTIME_ENV_FILES[mk04_env_token]
    if path.is_file():
        return _component("Environment file", ok=True, detail=str(path), required=is_production)
    if is_production:
        return _component(
            "Environment file",
            ok=False,
            detail=f"missing required runtime env file {path}",
            required=True,
        )
    return BootComponent(
        "Environment file",
        "WARN",
        f"runtime env missing ({path}); acceptable for local dev",
        required=False,
    )


def _check_http_service(
    label: str,
    url: str,
    *,
    required: bool,
    probe_fn: Callable[[str], tuple[bool, str]],
) -> BootComponent:
    ok, detail = probe_fn(url)
    if not ok and label == "Operations UI" and detail.startswith("HTTP 401"):
        ok = True
        detail = f"{detail} (authentication required; service reachable)"
    return _component(label, ok=ok, detail=f"{url} ({detail})", required=required)


def _check_scheduler(mk04_env_token: str, data_root: Path | None) -> BootComponent:
    root = data_root if data_root is not None else REPO_ROOT / "data" / mk04_env_token
    runtime_disabled, _ = load_runtime_scheduler_control(root)
    underlying = inspect_underlying_scheduler(mk04_env_token, REPO_ROOT)
    effective, detail = compute_effective_scheduler(
        runtime_disabled,
        underlying,
        mk04_env_token=mk04_env_token,
    )
    mode = scheduler_mode_for(mk04_env_token)

    if mode == "manual":
        configured, _ = mk04_schedule_configured(repo_root=REPO_ROOT)
        bits = ["manual scheduler mode"]
        if runtime_disabled is True:
            bits.append("scheduler_disabled=true")
        if not configured:
            bits.append("no MK04 timer/cron")
        return _component(
            "Scheduler",
            ok=True,
            detail="; ".join(bits) + " (intentionally disabled)",
            required=True,
        )

    if runtime_disabled is True:
        return _component(
            "Scheduler",
            ok=True,
            detail="scheduler_disabled=true; autonomous scheduling intentionally paused",
            required=True,
        )

    configured, artifact = mk04_schedule_configured(repo_root=REPO_ROOT)
    if not configured:
        return BootComponent(
            "Scheduler",
            "WARN",
            detail or "autonomous mode configured but no MK04 timer/cron artifact",
            required=True,
        )

    if effective == "enabled" and underlying.active is True:
        return _component("Scheduler", ok=True, detail=f"MK04 schedule active ({artifact})", required=True)

    if underlying.active is False:
        return _component(
            "Scheduler",
            ok=False,
            detail=detail or f"MK04 schedule configured ({artifact}) but host cron inactive",
            required=True,
        )

    if effective == "unknown":
        return BootComponent(
            "Scheduler",
            "WARN",
            detail or "scheduler state unknown",
            required=True,
        )

    if effective == "disabled":
        return BootComponent(
            "Scheduler",
            "WARN",
            detail or "scheduler effectively disabled",
            required=True,
        )

    return BootComponent("Scheduler", "WARN", detail or "scheduler state unknown", required=True)


def _check_database(state: EnvironmentStatePaths, *, is_production: bool) -> BootComponent:
    """
    ConfigManager database_path is an optional placeholder / future metadata slot.

    It must not block production readiness. Real service databases
    (output_funnel.sqlite3, ops_ui.sqlite3) are validated by service parents.
    """
    if is_production:
        return BootComponent(
            "Config database (optional)",
            "PASS",
            "production uses runtime service DB parents (see below); "
            "optional placeholder not required",
            required=False,
        )

    db_path = state.database_path
    if path_under_code_or_releases(db_path):
        return BootComponent(
            "Config database (optional)",
            "WARN",
            f"optional placeholder must not resolve under code/releases: {db_path}",
            required=False,
        )

    parent = db_path.parent
    if not parent.exists():
        return BootComponent(
            "Config database (optional)",
            "WARN",
            f"optional placeholder DB parent missing: {parent}",
            required=False,
        )
    if not db_path.exists():
        return BootComponent(
            "Config database (optional)",
            "WARN",
            f"optional placeholder database not present: {db_path}",
            required=False,
        )
    if not os.access(db_path, os.R_OK):
        return BootComponent(
            "Config database (optional)",
            "WARN",
            f"optional placeholder database not readable: {db_path}",
            required=False,
        )
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return BootComponent(
            "Config database (optional)",
            "WARN",
            f"optional placeholder database unreadable: {str(exc)[:120]}",
            required=False,
        )
    return BootComponent(
        "Config database (optional)",
        "PASS",
        f"optional placeholder present: {db_path}",
        required=False,
    )


def _check_service_state_parents(*, is_production: bool) -> list[BootComponent]:
    """Validate runtime parent directories for real service databases when configured."""
    components: list[BootComponent] = []
    runtime = os.environ.get("MK04_RUNTIME_ROOT", "").strip()
    if not runtime:
        if is_production:
            components.append(
                _component(
                    "Runtime root",
                    ok=False,
                    detail="MK04_RUNTIME_ROOT is required in production",
                    required=True,
                )
            )
        return components

    runtime_path = Path(runtime).expanduser()
    if path_under_code_or_releases(runtime_path):
        components.append(
            _component(
                "Runtime root",
                ok=False,
                detail=f"MK04_RUNTIME_ROOT under code/releases: {runtime_path}",
                required=True,
            )
        )
        return components

    for label, env_name, default_rel in (
        ("Output funnel DB parent", "OUTPUT_FUNNEL_DB", "output-funnel/output_funnel.sqlite3"),
        ("Ops UI DB parent", "OPS_UI_DB", "ops-ui/ops_ui.sqlite3"),
    ):
        raw = os.environ.get(env_name, "").strip()
        path = Path(raw).expanduser() if raw else (runtime_path / default_rel)
        if path_under_code_or_releases(path):
            components.append(
                _component(
                    label,
                    ok=False,
                    detail=f"path under code/releases: {path}",
                    required=is_production,
                )
            )
            continue
        parent = path.parent
        if parent.exists():
            components.append(
                _component(label, ok=True, detail=str(parent), required=False)
            )
        else:
            # Optional absence must not block READY.
            components.append(
                BootComponent(
                    label,
                    "WARN",
                    f"parent directory missing: {parent}",
                    required=False,
                )
            )
    return components


def _check_output_paths(state: EnvironmentStatePaths, *, is_production: bool) -> BootComponent:
    if is_production:
        ok, detail = production_runtime_authority()
        if not ok:
            return _component("Output paths", ok=False, detail=detail, required=True)

    cache_dir = state.data_root / "cache"
    try:
        state.assert_within_environment(cache_dir)
    except ValueError as exc:
        return _component("Output paths", ok=False, detail=str(exc)[:120], required=True)

    if path_under_code_or_releases(cache_dir):
        return _component(
            "Output paths",
            ok=False,
            detail=f"refusing write under code/releases: {cache_dir}",
            required=True,
        )

    ok, detail = probe_runtime_cache_write(cache_dir, probe_prefix=".boot_write_probe")
    if not ok:
        return _component("Output paths", ok=False, detail=detail, required=True)
    return _component("Output paths", ok=True, detail=detail, required=True)


def build_boot_verification(
    mk04_env_token: str,
    *,
    probe_fn: Callable[[str], tuple[bool, str]] | None = None,
) -> BootVerification:
    """Evaluate boot readiness for the selected environment."""
    canonical = canonical_env(mk04_env_token)
    token = mk04_env(canonical)
    is_production = canonical == "production"
    probe = probe_fn or http_probe

    report = BootVerification(
        environment=token,
        env_label=env_label(canonical),
    )

    authority_ok = True
    if is_production:
        authority_ok, authority_detail = production_runtime_authority()
        report.components.append(
            _component(
                "Runtime path authority",
                ok=authority_ok,
                detail=authority_detail,
                required=True,
            )
        )

    config_component, resolved = _check_config(canonical)
    report.components.append(config_component)
    report.components.append(_check_env_file(token, is_production=is_production))

    urls = service_health_urls(token)
    # Service order: AI (optional) → API → Worker → Output funnel → Ops UI (optional)
    report.components.append(
        _check_http_service("AI service", urls["AI service"], required=False, probe_fn=probe)
    )
    report.components.append(
        _check_http_service("API", urls["API"], required=True, probe_fn=probe)
    )
    report.components.append(
        _check_http_service("Worker", urls["Worker"], required=True, probe_fn=probe)
    )
    report.components.append(
        _check_http_service(
            "Output funnel",
            urls["Output funnel"],
            required=True,
            probe_fn=probe,
        )
    )
    report.components.append(
        _check_http_service(
            "Operations UI",
            urls["Operations UI"],
            required=False,
            probe_fn=probe,
        )
    )

    data_root = None
    if resolved is not None:
        from production_secrets import validate_from_resolved_config  # noqa: PLC0415

        channels_raw = os.environ.get("OUTPUT_FUNNEL_CHANNELS", "").strip()
        channels_path = Path(channels_raw).expanduser() if channels_raw else None
        secret_result = validate_from_resolved_config(
            resolved,
            upload_mode=os.environ.get("MK04_UPLOAD_MODE", "dry_run"),
            channels_path=channels_path,
        )
        secrets_required = is_production and bool(
            resolved.get("runtime.require_production_secrets")
        )
        if secret_result.ok:
            detail = "conditional credential checks passed"
            if secret_result.warnings:
                detail = "; ".join(secret_result.warnings[:2])
            report.components.append(
                _component(
                    "Production credentials",
                    ok=True,
                    detail=detail,
                    required=secrets_required,
                )
            )
        else:
            detail = "; ".join(secret_result.errors[:3]) or "credential validation failed"
            report.components.append(
                _component(
                    "Production credentials",
                    ok=False,
                    detail=detail[:200],
                    required=secrets_required,
                )
            )

        state = EnvironmentStatePaths.from_resolved_config(resolved)
        data_root = state.data_root
        report.components.append(_check_scheduler(token, data_root))
        report.components.append(_check_database(state, is_production=is_production))
        report.components.extend(_check_service_state_parents(is_production=is_production))
        if not authority_ok:
            report.components.append(
                _component(
                    "Output paths",
                    ok=False,
                    detail="skipped: production runtime authority missing/mismatched",
                    required=True,
                )
            )
        else:
            report.components.append(
                _check_output_paths(state, is_production=is_production)
            )
    else:
        report.components.append(
            BootComponent("Scheduler", "FAIL", "config validation failed", True)
        )
        report.components.append(
            BootComponent(
                "Config database (optional)",
                "WARN",
                "config validation failed",
                False,
            )
        )
        report.components.append(
            BootComponent("Output paths", "FAIL", "config validation failed", True)
        )

    required_failed = any(c.required and c.result == "FAIL" for c in report.components)
    report.overall = "NOT READY" if required_failed else "READY"
    return report


def render_boot_verification(report: BootVerification) -> str:
    lines = [
        f"Boot Verification: {report.env_label}",
        "",
    ]
    for component in report.components:
        role = "required" if component.required else "optional"
        text = f"{component.label:<24} {component.result}"
        if component.detail:
            text += f" - {component.detail}"
        text += f" ({role})"
        lines.append(text)
    lines.extend(["", f"{'Boot readiness':<24} {report.overall}"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if not argv:
        argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help"}:
        print(
            "Usage: boot_verification.py <dev|prod>\n"
            "Read-only boot readiness report (READY / NOT READY).\n"
            "Exit codes: 0=READY, 1=READY with optional warnings, 2=NOT READY"
        )
        return 0 if argv and argv[0] in {"-h", "--help"} else 1
    try:
        canonical_env(argv[0])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 64

    report = build_boot_verification(argv[0])
    print(render_boot_verification(report))
    if report.overall == "NOT READY":
        return 2
    if any(c.result == "WARN" for c in report.components):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
