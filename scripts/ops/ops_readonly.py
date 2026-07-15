#!/usr/bin/env python3
"""Shared read-only helpers for scripts/ops status and health collectors."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_CONFIG = REPO_ROOT / "scripts" / "config"

if str(SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_CONFIG))

from environment_names import (  # noqa: E402
    EnvironmentNameError,
    env_label as _env_label_from_names,
    to_config_environment,
    to_runtime_env,
)

SERVICE_LABELS: dict[str, str] = {
    "mk04-ai-service": "AI service",
    "mk04-source-input": "API",
    "mk04-video-automation": "Worker",
    "mk04-ops-ui": "Operations UI",
    "mk04-output-funnel": "Output funnel",
}

SERVICE_ORDER = [
    "AI service",
    "API",
    "Worker",
    "Operations UI",
    "Output funnel",
]

DEFAULT_SCHEDULER_MODE = {
    "dev": "manual",
    "prod": "manual",  # overridden by MK04_SCHEDULER_MODE when set (env.sh / /etc)
}

# Canonical production roots (service runners / env.sh). Hermetic tests may use
# alternate roots when MK04_SKIP_PROD_PREFLIGHT=1, but never code/releases trees.
CANONICAL_PROD_ROOTS = {
    "MK04_CODE_ROOT": "/opt/mk04/prod/current",
    "MK04_RUNTIME_ROOT": "/var/lib/mk04/prod",
    "MK04_LOG_ROOT": "/var/log/mk04/prod",
    "MK04_CONFIG_ROOT": "/etc/mk04/prod",
    "MK04_SHARED_LOCK_ROOT": "/var/lib/mk04/locks",
}

DEFAULT_PORTS = {
    "dev": {
        "input": 5160,
        "video": 5150,
        "output": 5155,
        "ops": 5170,
        "ai": 5175,
    },
    "prod": {
        "input": 5060,
        "video": 5050,
        "output": 5055,
        "ops": 5070,
        "ai": 5075,
    },
}

RUNTIME_ENV_FILES = {
    "dev": Path("/etc/mk04/dev/env"),
    "prod": Path("/etc/mk04/prod/env"),
}

ENV_EXAMPLES = {
    "dev": REPO_ROOT / "deploy/env/dev/env.example",
    "prod": REPO_ROOT / "deploy/env/prod/env.example",
}

# Log/restart command modes -> systemd units.
# source-input is the API-facing ingestion service (POST /run-funnel, GET /healthz).
LOG_MODE_UNITS: dict[str, str] = {
    "api": "mk04-source-input.service",
    "worker": "mk04-video-automation.service",
    "ai": "mk04-ai-service.service",
    "output-funnel": "mk04-output-funnel.service",
    "ops-ui": "mk04-ops-ui.service",
}

# Restart targets use the same unit map as logs/status/health.
RESTART_TARGETS: dict[str, str] = dict(LOG_MODE_UNITS)

RESTART_ALL_ORDER = ["api", "worker", "output-funnel", "ai", "ops-ui"]

RESTART_REQUIRED_TARGETS = frozenset({"api", "worker", "ai", "all"})
RESTART_OPTIONAL_TARGETS = frozenset({"ops-ui", "output-funnel"})
RESTART_VALID_TARGETS = RESTART_REQUIRED_TARGETS | RESTART_OPTIONAL_TARGETS

LOG_MODES = frozenset(
    {"api", "worker", "ai", "scheduler", "errors", "today", "output-funnel", "ops-ui"}
)

DEFAULT_LOG_LINES = 200
MAX_LOG_LINES = 1000

# Scheduler scripts logged via cron/logger; user-facing mode stays "scheduler".
SCHEDULER_LOG_MARKERS = (
    "mk04",
    "run-funnel-daily",
    "watchdog.sh",
    "retention-sweeper",
    "handoff_sweeper",
)


@dataclass
class Line:
    label: str
    value: str
    detail: str = ""
    severity: str = "info"  # info | warn | fail


def ensure_config_scripts_on_path() -> None:
    scripts_config_str = str(SCRIPTS_CONFIG)
    import sys

    if scripts_config_str not in sys.path:
        sys.path.insert(0, scripts_config_str)


def canonical_env(raw: str) -> str:
    """Normalize any alias to ConfigManager form: development | production."""
    try:
        return to_config_environment(raw)
    except EnvironmentNameError as exc:
        raise ValueError(f"invalid environment: {raw!r}") from exc


def env_label(canonical: str) -> str:
    return _env_label_from_names(canonical)


def mk04_env(canonical: str) -> str:
    """Map ConfigManager or alias form to runtime token: dev | prod."""
    try:
        return to_runtime_env(canonical)
    except EnvironmentNameError as exc:
        raise ValueError(f"invalid environment: {canonical!r}") from exc


def run_command(args: list[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_commit(repo_root: Path = REPO_ROOT) -> tuple[str, str]:
    result = run_command(["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"])
    if result is None or result.returncode != 0:
        return "unknown", "git rev-parse unavailable"
    commit = result.stdout.strip()
    return commit or "unknown", ""


def load_update_status(data_root: Path) -> tuple[str, str]:
    path = data_root / "last_update_status.json"
    if not path.is_file():
        return "unknown", "no update status file found"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "unknown", f"could not read update status ({exc.__class__.__name__})"
    if not isinstance(payload, dict):
        return "unknown", "update status file is not a JSON object"
    raw = str(payload.get("status", "unknown")).strip().lower()
    if raw in {"success", "pass", "passed"}:
        return "PASS", ""
    if raw in {"failure", "fail", "failed"}:
        return "FAIL", ""
    return "unknown", f"unrecognised update status value: {raw or 'empty'}"


def load_runtime_upload_control(data_root: Path) -> tuple[bool | None, str]:
    path = data_root / "control_state.json"
    if not path.is_file():
        return None, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"could not read runtime control state ({exc.__class__.__name__})"
    if not isinstance(payload, dict):
        return None, "runtime control state is not a JSON object"
    if "uploads_disabled" in payload:
        return bool(payload.get("uploads_disabled")), ""
    if "uploads_enabled" in payload:
        return not bool(payload.get("uploads_enabled")), ""
    return None, "runtime control state has no uploads flag"


def compute_effective_upload(
    config_upload_enabled: bool,
    runtime_disabled: bool | None,
) -> tuple[bool | None, str]:
    """Return (can_upload, detail). None means effective state is unknown."""
    if runtime_disabled is True:
        return False, "blocked by runtime control"
    if not config_upload_enabled:
        return False, "blocked by config"
    if runtime_disabled is False:
        return True, "config enabled and runtime control allows"
    if config_upload_enabled:
        return None, "config enabled; runtime control state unknown"
    return False, "blocked by config"


def load_runtime_scheduler_control(data_root: Path) -> tuple[bool | None, str]:
    """Return (scheduler_disabled, detail). None means no explicit runtime flag."""
    path = data_root / "control_state.json"
    if not path.is_file():
        return None, "runtime control file not present"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"could not read runtime control state ({exc.__class__.__name__})"
    if not isinstance(payload, dict):
        return None, "runtime control state is not a JSON object"
    if "scheduler_disabled" in payload:
        return bool(payload.get("scheduler_disabled")), ""
    if "scheduler_enabled" in payload:
        return not bool(payload.get("scheduler_enabled")), ""
    return None, "runtime control file not present"


def scheduled_runs_allowed(data_root: Path) -> tuple[bool, str]:
    """Whether new *scheduled* pipeline runs may proceed (runtime control only).

    Canonical control plane: stop-scheduler / start-scheduler write
    data/<env>/control_state.json. Does not inspect readiness, locks, or cron
    install — those are enforced by run-pipeline.sh and the host scheduler.
    """
    runtime_disabled, detail = load_runtime_scheduler_control(data_root)
    if runtime_disabled is True:
        return False, "scheduler disabled by runtime control (stop-scheduler)"
    if runtime_disabled is False:
        return True, "scheduler enabled by runtime control (start-scheduler)"
    if detail:
        return True, detail
    return True, "scheduler runtime control allows scheduled runs"


@dataclass
class UnderlyingScheduler:
    mechanism: str
    active: bool | None
    detail: str


def scheduler_mode_for(mk04_env_token: str) -> str:
    """Return effective scheduler mode: env MK04_SCHEDULER_MODE wins over defaults."""
    raw = (os.environ.get("MK04_SCHEDULER_MODE") or "").strip().lower()
    if raw in {"manual", "autonomous", "cron"}:
        token = mk04_env(canonical_env(mk04_env_token))
        if raw == "cron":
            return "manual" if token == "dev" else "autonomous"
        return raw
    if raw:
        return raw
    return DEFAULT_SCHEDULER_MODE.get(mk04_env(canonical_env(mk04_env_token)), "unknown")


def path_under_code_or_releases(path: Path, *, code_root: Path | None = None) -> bool:
    """True when path resolves beneath current or releases (runtime data must not live here)."""
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False
    text = str(resolved).replace("\\", "/")
    if text.startswith("/opt/mk04/prod/releases/") or text.startswith("/opt/mk04/prod/current/"):
        return True

    prod_base_raw = (os.environ.get("MK04_PROD_BASE") or "/opt/mk04/prod").strip()
    try:
        base = Path(prod_base_raw).expanduser().resolve()
        releases = (base / "releases").resolve()
        if releases == resolved or releases in resolved.parents:
            return True
        current = base / "current"
        if current.exists() or current.is_symlink():
            active = current.resolve()
            if active in resolved.parents:
                return True
    except OSError:
        pass

    root = code_root
    if root is None:
        raw_code = (os.environ.get("MK04_CODE_ROOT") or "").strip()
        if raw_code:
            root = Path(raw_code)
    if root is not None:
        try:
            code = root.expanduser().resolve()
            if code in resolved.parents:
                return True
        except OSError:
            pass
    return False


def production_runtime_authority() -> tuple[bool, str]:
    """Validate production runtime path authority before any health write probe.

    When MK04_SKIP_PROD_PREFLIGHT=1 (hermetic tests), require roots to be set and
    outside code/releases rather than exact host canonical paths.
    """
    skip = (os.environ.get("MK04_SKIP_PROD_PREFLIGHT") or "").strip() in {"1", "true", "yes"}
    missing: list[str] = []
    mismatched: list[str] = []
    for name, expected in CANONICAL_PROD_ROOTS.items():
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            missing.append(name)
            continue
        path = Path(raw).expanduser()
        # Runtime/log/config/lock must never resolve under current/ or releases/.
        if name != "MK04_CODE_ROOT" and path_under_code_or_releases(path):
            mismatched.append(f"{name} under code/releases ({raw})")
            continue
        if not skip:
            try:
                if name == "MK04_CODE_ROOT":
                    # Logical entry must be the current symlink path (not a release path).
                    if str(path).replace("\\", "/").rstrip("/") != expected:
                        mismatched.append(f"{name} expected {expected}, got {raw}")
                elif path.resolve() != Path(expected).resolve():
                    mismatched.append(f"{name} expected {expected}, got {raw}")
            except OSError:
                mismatched.append(f"{name} unresolvable ({raw})")
    if missing:
        return False, "production runtime authority missing: " + ", ".join(missing)
    if mismatched:
        return False, "production runtime authority mismatched: " + "; ".join(mismatched)
    return True, "canonical production runtime roots established"


def probe_runtime_cache_write(
    cache_dir: Path,
    *,
    probe_prefix: str = ".health_write_probe",
) -> tuple[bool, str]:
    """Write a unique temp probe under an existing cache dir; always attempt cleanup.

    Parent cache_dir must already exist — this never creates directories.
    """
    if path_under_code_or_releases(cache_dir):
        return False, f"refusing write probe under code/releases: {cache_dir}"
    if not cache_dir.is_dir():
        return False, f"runtime cache directory missing (not creating): {cache_dir}"

    import uuid  # local import keeps module import light

    probe = cache_dir / f"{probe_prefix}_{os.getpid()}_{uuid.uuid4().hex}"
    try:
        probe.write_text("ok", encoding="utf-8")
    except OSError as exc:
        return False, f"write failed at {probe}: {str(exc)[:120]}"

    try:
        probe.unlink()
    except OSError as exc:
        return False, f"write succeeded but cleanup failed at {probe}: {str(exc)[:120]}"

    if probe.exists():
        return False, f"write succeeded but probe still present after cleanup: {probe}"
    return True, f"writable under {cache_dir}"


def mk04_schedule_configured(*, repo_root: Path = REPO_ROOT) -> tuple[bool, str]:
    """True only when an MK04-specific host schedule artifact is present."""
    cron_dropin = Path("/etc/cron.d/mk04")
    if cron_dropin.is_file():
        return True, str(cron_dropin)
    timers_dir = Path("/etc/systemd/system")
    if timers_dir.is_dir():
        found = sorted(timers_dir.glob("mk04-*.timer"))
        if found:
            return True, str(found[0])
    return False, "no MK04 cron drop-in or mk04-*.timer installed"


def inspect_underlying_scheduler(mk04_env_token: str, repo_root: Path = REPO_ROOT) -> UnderlyingScheduler:
    mode = scheduler_mode_for(mk04_env_token)
    if mode == "manual":
        return UnderlyingScheduler(
            mechanism="manual",
            active=False,
            detail="manual scheduler mode; no autonomous MK04 schedule expected",
        )

    configured, artifact = mk04_schedule_configured(repo_root=repo_root)
    if not configured:
        # Do not treat the generic system cron daemon as an MK04 schedule.
        return UnderlyingScheduler(
            mechanism="none",
            active=False,
            detail="no MK04 timer/cron artifact configured",
        )

    cron_state = "unknown"
    if systemctl_available():
        for unit in ("cron.service", "crond.service"):
            result = run_command(["systemctl", "is-active", unit])
            if result is None:
                continue
            stderr = " ".join(result.stderr.strip().split())
            if systemd_not_running(stderr):
                return UnderlyingScheduler(
                    mechanism="cron",
                    active=None,
                    detail="systemd not running on this host",
                )
            if result.returncode in {0, 3}:
                cron_state = result.stdout.strip().lower() or "unknown"
                if cron_state == "active":
                    break

    active = True if cron_state == "active" else False if cron_state in {"inactive", "failed"} else None
    detail = f"MK04 schedule artifact present ({artifact})"
    if cron_state != "active":
        detail = f"{detail}; host cron service is {cron_state or 'unknown'}"
    return UnderlyingScheduler(mechanism="cron", active=active, detail=detail)


def compute_effective_scheduler(
    runtime_disabled: bool | None,
    underlying: UnderlyingScheduler,
    *,
    mk04_env_token: str,
) -> tuple[str, str]:
    """Return (state, detail) where state is enabled|disabled|unknown."""
    if runtime_disabled is True:
        return "disabled", "disabled by runtime control"

    mode = scheduler_mode_for(mk04_env_token)
    if mode == "manual":
        return "disabled", "manual scheduler mode; autonomous scheduling not armed"

    if runtime_disabled is False:
        prefix = "enabled by runtime control"
    else:
        prefix = "no runtime scheduler override"

    if underlying.active is True:
        return "enabled", f"{prefix}; {underlying.mechanism} active"
    if underlying.active is False:
        return "disabled", f"{prefix}; {underlying.detail}"
    return "unknown", f"{prefix}; {underlying.detail}"


def discover_service_units(repo_root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    units_dir = repo_root / "deploy" / "systemd"
    discovered: list[tuple[str, str]] = []
    if not units_dir.is_dir():
        return discovered
    for path in sorted(units_dir.glob("mk04-*.service")):
        stem = path.name.removesuffix(".service")
        label = SERVICE_LABELS.get(stem, stem.replace("mk04-", "").replace("-", " ").title())
        discovered.append((label, path.name))
    return discovered


def unit_description(unit_path: Path) -> str:
    try:
        for line in unit_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("Description="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return ""
    return ""


def systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def systemd_not_running(stderr: str) -> bool:
    lowered = stderr.lower()
    return "not been booted with systemd" in lowered or "host is down" in lowered


def systemd_unit_status(unit: str) -> tuple[str, str, str]:
    if not systemctl_available():
        return "unknown", "systemctl not available", "info"
    result = run_command(["systemctl", "is-active", unit])
    if result is None:
        return "unknown", "systemctl check failed", "info"
    state = result.stdout.strip().lower()
    stderr = " ".join(result.stderr.strip().split())
    if systemd_not_running(stderr):
        return "not yet available", "systemd not running on this host", "info"
    if state == "active":
        return "PASS", "", "info"
    if state in {"inactive", "failed", "deactivating"}:
        detail = stderr or f"systemd reports {state or 'inactive'}"
        return "FAIL", detail[:120], "fail"
    if "could not be found" in stderr.lower() or state == "unknown":
        return "not yet available", "unit not installed on this host", "info"
    detail = (stderr or f"systemd reports {state or 'unknown'}")[:120]
    return "unknown", detail, "warn"


def http_probe(url: str, *, timeout: float = 2.5) -> tuple[bool, str]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if 200 <= response.status < 300:
                return True, f"HTTP {response.status}"
            return False, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)[:120]


def _port_from_env(*names: str, default: int) -> int:
    for name in names:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return default


def service_health_urls(mk04_env_token: str) -> dict[str, str]:
    """HTTP readiness URLs for always-running production services."""
    token = mk04_env(canonical_env(mk04_env_token))
    defaults = DEFAULT_PORTS[token]
    input_port = _port_from_env("INPUT_SERVICE_PORT", default=defaults["input"])
    video_port = _port_from_env(
        "VIDEO_AUTOMATION_PORT",
        "VIDEO_SERVICE_PORT",
        default=defaults["video"],
    )
    output_port = _port_from_env("OUTPUT_FUNNEL_PORT", default=defaults["output"])
    ai_port = _port_from_env("AI_SERVICE_PORT", default=defaults["ai"])
    ops_port = _port_from_env("OPS_UI_PORT", default=defaults["ops"])

    def _host(*names: str) -> str:
        for name in names:
            value = (os.environ.get(name) or "").strip()
            if value and value not in {"0.0.0.0", "::"}:
                return value
        return "127.0.0.1"

    input_host = _host("INPUT_SERVICE_HOST")
    video_host = _host("VIDEO_AUTOMATION_HOST", "VIDEO_SERVICE_HOST")
    output_host = _host("OUTPUT_FUNNEL_HOST")
    ai_host = _host("AI_SERVICE_HOST")
    ops_host = _host("OPS_UI_HOST")

    return {
        "API": f"http://{input_host}:{input_port}/healthz",
        "Worker": f"http://{video_host}:{video_port}/healthz",
        "Output funnel": f"http://{output_host}:{output_port}/healthz",
        "AI service": f"http://{ai_host}:{ai_port}/health",
        "Operations UI": f"http://{ops_host}:{ops_port}/health",
    }


# Services that must respond before a scheduled production run may start.
# AI and Operations UI are optional: they may be down without blocking the gate.
SCHEDULER_REQUIRED_READINESS = ("API", "Worker", "Output funnel")


@dataclass
class ReadinessProbe:
    label: str
    ready: bool
    detail: str
    required: bool


@dataclass
class SchedulerReadiness:
    """Readiness for scheduled pipeline operation (not mere process start)."""

    ready: bool
    reasons: list[str]
    probes: list[ReadinessProbe]


def evaluate_scheduler_readiness(
    mk04_env_token: str,
    *,
    probe_fn: Any | None = None,
) -> SchedulerReadiness:
    """Probe HTTP health endpoints to decide if scheduled runs may proceed.

    Process startup order alone is not enough: a unit may be active while its
    HTTP listener is not yet serving. AI service and Operations UI are probed
    for visibility but do not block the gate (optional / independent).
    """
    probe = probe_fn or http_probe
    urls = service_health_urls(mk04_env_token)
    probes: list[ReadinessProbe] = []
    reasons: list[str] = []

    for label in ("API", "Worker", "Output funnel", "AI service", "Operations UI"):
        required = label in SCHEDULER_REQUIRED_READINESS
        url = urls[label]
        ok, detail = probe(url)
        probes.append(
            ReadinessProbe(
                label=label,
                ready=ok,
                detail=f"{url} ({detail})",
                required=required,
            )
        )
        if required and not ok:
            reasons.append(f"{label} not ready: {detail}")

    return SchedulerReadiness(ready=not reasons, reasons=reasons, probes=probes)


def disk_usage_percent(path: Path) -> tuple[int | None, str]:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return None, str(exc)
    if usage.total <= 0:
        return None, "total disk size is zero"
    return int(round(usage.used / usage.total * 100)), ""


def format_bytes(num: int) -> str:
    gib = num / (1024**3)
    if gib >= 10:
        return f"{gib:.0f}G"
    if gib >= 1:
        return f"{gib:.1f}G"
    mib = num / (1024**2)
    return f"{mib:.0f}M"


def gpu_visibility_check(*, timeout: float = 5.0) -> tuple[str, str]:
    if shutil.which("nvidia-smi") is None:
        return "not available", "nvidia-smi not found"
    result = run_command(["nvidia-smi", "-L"], timeout=timeout)
    if result is None or result.returncode != 0:
        detail = (result.stderr.strip() if result else "nvidia-smi failed")[:120]
        return "WARN", detail or "nvidia-smi failed"
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return "WARN", "nvidia-smi returned no GPUs"
    return "PASS", f"{len(lines)} GPU(s) visible"


def sort_service_lines(lines: list[Line]) -> list[Line]:
    order_index = {name: idx for idx, name in enumerate(SERVICE_ORDER)}
    return sorted(lines, key=lambda line: order_index.get(line.label, 99))


def restart_unit_file_exists(unit: str, repo_root: Path = REPO_ROOT) -> bool:
    stem = unit.removesuffix(".service")
    return (repo_root / "deploy" / "systemd" / f"{stem}.service").is_file()


def resolve_restart_targets(target: str) -> list[tuple[str, str]]:
    """Return ordered (target_name, systemd_unit) pairs for a restart target."""
    token = target.strip().lower()
    if token == "all":
        pairs: list[tuple[str, str]] = []
        for name in RESTART_ALL_ORDER:
            unit = RESTART_TARGETS.get(name)
            if unit and restart_unit_file_exists(unit):
                pairs.append((name, unit))
        return pairs
    unit = RESTART_TARGETS.get(token)
    if not unit:
        raise ValueError(f"unknown restart target: {target!r}")
    if not restart_unit_file_exists(unit):
        raise ValueError(f"systemd unit file missing in repo for target {token!r}: {unit}")
    return [(token, unit)]
