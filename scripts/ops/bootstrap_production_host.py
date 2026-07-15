#!/usr/bin/env python3
"""Idempotent first-production-host bootstrap orchestrator (Prompt 7).

Creates prod-only host identity/paths, seeds config, delegates promotion and
component bootstrap to canonical scripts, installs systemd units and operator
commands. Never installs cron, never runs a content pipeline, never enables
real uploads. Never prints secret values.

Phases (run individually or as a sequence):
  prepare-host | seed-config | promote | component-bootstrap |
  reconcile-permissions | install-services | install-commands | verify

Tests must pass ``--path-prefix`` (temporary root) and mocked account/systemd
runners — never against the real host.
"""

from __future__ import annotations

import argparse
import getpass
import grp
import json
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = Path(__file__).resolve().parent

PHASES: tuple[str, ...] = (
    "prepare-host",
    "seed-config",
    "promote",
    "component-bootstrap",
    "reconcile-permissions",
    "install-services",
    "install-commands",
    "verify",
)

PROTECTED_SUFFIXES = (
    "/etc/mk04/dev",
    "/var/lib/mk04/dev",
    "/var/log/mk04/dev",
)

PROD_PORTS = {
    "INPUT_SERVICE_PORT": "5060",
    "VIDEO_AUTOMATION_PORT": "5050",
    "OUTPUT_FUNNEL_PORT": "5055",
    "OPS_UI_PORT": "5070",
    "AI_SERVICE_PORT": "5075",
}

DEV_PORT_MARKERS = ("5160", "5150", "5155", "5170", "5175")

INTERNAL_SECRETS = (
    "INPUT_SERVICE_SECRET",
    "VIDEO_AUTOMATION_SECRET",
    "OUTPUT_FUNNEL_SECRET",
    "OPS_UI_OPERATOR_PASSWORD",
    "OPS_UI_SECRET_KEY",
)

# Exact permission contract (Pause Point 2 repair).
CONFIG_FILE_MODE = 0o0640
RUNTIME_CONTROL_MODE = 0o0660
LOCK_ARTIFACT_MODE = 0o0660
ETC_DIR_MODE = 0o0750

KNOWN_LOCK_ARTIFACTS = (
    "promotion.lock",
    "global_pipeline.lock",
    "production_turnstile.lock",
    "gate_status.json",
)

NEVER_GENERATE = frozenset(
    {
        "OPENAI_API_KEY",
        "YT_DLP_COOKIES_PATH",
        "MFM_BUSINESS_AI_YT_TOKEN_FILE",
        "MFM_BUSINESS_AI_YT_CLIENT_SECRET_FILE",
    }
)

PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "change_me",
        "replace-me",
        "replace_me",
        "todo",
        "tbd",
        "xxx",
        "your-key-here",
        "your_key_here",
        "insert-key-here",
        "sk-placeholder",
        "example",
        "none",
        "null",
        "mk04-ops-ui-dev-secret-change-me",
    }
)

SYSTEMD_UNITS = (
    "mk04-source-input.service",
    "mk04-video-automation.service",
    "mk04-output-funnel.service",
    "mk04-ai-service.service",
    "mk04-ops-ui.service",
)

# Enable/start order: shared Ollama first (if present), then AI, then pipeline peers, then UI.
SERVICE_START_ORDER = (
    "ollama.service",  # existing shared dependency; soft
    "mk04-ai-service.service",
    "mk04-source-input.service",
    "mk04-video-automation.service",
    "mk04-output-funnel.service",
    "mk04-ops-ui.service",
)

SEED_FILES = (
    ("deploy/env/prod/env.example", "env"),
    ("deploy/env/prod/funnels.json", "source-input/funnels.json"),
    ("deploy/env/prod/pipeline_config.json", "video-automation/pipeline_config.json"),
    ("deploy/env/prod/video_pipeline_profiles.json", "video-automation/video_pipeline_profiles.json"),
    ("deploy/env/prod/settings.json", "output-funnel/settings.json"),
    ("deploy/env/prod/channels.json", "output-funnel/channels.json"),
)


class BootstrapError(RuntimeError):
    """Host bootstrap failed; later phases must not continue."""


@dataclass(frozen=True)
class HostLayout:
    """Absolute host path layout (real host or path-prefix sandbox)."""

    opt_prod: Path
    etc_prod: Path
    var_lib_prod: Path
    var_log_prod: Path
    locks: Path
    systemd_dir: Path
    commands_dir: Path
    prefix: Path | None = None

    @classmethod
    def production(cls, *, commands_dir: Path | None = None) -> HostLayout:
        return cls(
            opt_prod=Path("/opt/mk04/prod"),
            etc_prod=Path("/etc/mk04/prod"),
            var_lib_prod=Path("/var/lib/mk04/prod"),
            var_log_prod=Path("/var/log/mk04/prod"),
            locks=Path("/var/lib/mk04/locks"),
            systemd_dir=Path("/etc/systemd/system"),
            commands_dir=commands_dir or Path("/usr/local/bin"),
            prefix=None,
        )

    @classmethod
    def under_prefix(cls, prefix: Path, *, commands_dir: Path | None = None) -> HostLayout:
        root = prefix.resolve()
        return cls(
            opt_prod=root / "opt" / "mk04" / "prod",
            etc_prod=root / "etc" / "mk04" / "prod",
            var_lib_prod=root / "var" / "lib" / "mk04" / "prod",
            var_log_prod=root / "var" / "log" / "mk04" / "prod",
            locks=root / "var" / "lib" / "mk04" / "locks",
            systemd_dir=root / "etc" / "systemd" / "system",
            commands_dir=commands_dir or (root / "usr" / "local" / "bin"),
            prefix=root,
        )

    @property
    def opt_releases(self) -> Path:
        return self.opt_prod / "releases"

    @property
    def opt_bundles(self) -> Path:
        return self.opt_prod / "dependency-bundles"

    @property
    def opt_current(self) -> Path:
        return self.opt_prod / "current"

    @property
    def etc_credentials(self) -> Path:
        return self.etc_prod / "credentials"

    @property
    def etc_services(self) -> Path:
        return self.etc_prod / "services"

    @property
    def var_credentials(self) -> Path:
        return self.var_lib_prod / "credentials"

    @property
    def var_data(self) -> Path:
        return self.var_lib_prod / "data"

    @property
    def var_data_cache(self) -> Path:
        """Canonical runtime cache leaf used by health/boot write probes."""
        return self.var_data / "cache"

    @property
    def var_ops_ui(self) -> Path:
        return self.var_lib_prod / "ops-ui"

    def protected_paths(self) -> tuple[Path, ...]:
        if self.prefix is None:
            return tuple(Path(p) for p in PROTECTED_SUFFIXES)
        return tuple(self.prefix / p.lstrip("/") for p in PROTECTED_SUFFIXES)

    def prepare_host_paths(self) -> list[tuple[Path, int, str]]:
        """
        Exact directories to create during prepare-host.

        Each entry: (path, mode, ownership_role)
          ownership_role: 'opt' | 'runtime' | 'log' | 'etc' | 'locks'
        """
        return [
            # locks FIRST — before /etc/mk04/prod (production_installation_present marker)
            (self.locks, 0o2775, "locks"),
            (self.opt_prod, 0o2775, "opt"),
            (self.opt_releases, 0o2775, "opt"),
            (self.opt_bundles, 0o2775, "opt"),
            (self.var_lib_prod, 0o2775, "runtime"),
            (self.var_credentials, 0o2770, "runtime"),
            (self.var_data, 0o2775, "runtime"),
            # Health/boot write probe target (EnvironmentStatePaths.caches_root).
            (self.var_data_cache, 0o2775, "runtime"),
            (self.var_ops_ui, 0o2775, "runtime"),
            (self.var_lib_prod / "source-input", 0o2775, "runtime"),
            (self.var_lib_prod / "video-automation", 0o2775, "runtime"),
            (self.var_lib_prod / "output-funnel", 0o2775, "runtime"),
            (self.var_lib_prod / "database", 0o2775, "runtime"),
            (self.var_lib_prod / "runs", 0o2775, "runtime"),
            (self.var_lib_prod / "reports", 0o2775, "runtime"),
            (self.var_log_prod, 0o2775, "log"),
            (self.var_log_prod / "video-automation", 0o2775, "log"),
            (self.var_log_prod / "output-funnel", 0o2775, "log"),
            (self.var_log_prod / "ops-ui", 0o2775, "log"),
            (self.var_log_prod / "ai-service", 0o2775, "log"),
            (self.var_log_prod / "watchdog", 0o2775, "log"),
            # Marker directory — AFTER locks
            (self.etc_prod, 0o0750, "etc"),
            (self.etc_credentials, 0o0750, "etc"),
            (self.etc_services, 0o0750, "etc"),
            (self.etc_prod / "source-input", 0o0750, "etc"),
            (self.etc_prod / "video-automation", 0o0750, "etc"),
            (self.etc_prod / "video-automation" / "funnels", 0o0750, "etc"),
            (self.etc_prod / "output-funnel", 0o0750, "etc"),
        ]


@dataclass
class BootstrapOptions:
    dev_root: Path
    operator: str = "maguireltd"
    service_user: str = "mk04"
    service_group: str = "mk04"
    dry_run: bool = False
    apply: bool = False
    phases: tuple[str, ...] = PHASES
    layout: HostLayout = field(default_factory=HostLayout.production)
    commands_target: Path | None = None
    allow_first_bootstrap: bool = True
    stop_before: str | None = None  # phase name: refuse to run that phase and later
    run_cmd: Callable[..., subprocess.CompletedProcess[str]] | None = None
    secret_factory: Callable[[], str] | None = None
    skip_account: bool = False  # tests: directories only
    skip_external: bool = False  # tests: do not invoke promote/bootstrap/systemctl


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _log(msg: str) -> None:
    print(msg, flush=True)


def _is_placeholder(value: str) -> bool:
    token = (value or "").strip()
    if not token:
        return True
    return token.lower() in PLACEHOLDER_VALUES


def _normalize_path(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def assert_not_protected(path: Path, layout: HostLayout) -> None:
    target = _normalize_path(path)
    for protected in layout.protected_paths():
        try:
            prot = protected.resolve()
        except OSError:
            prot = protected
        if target == prot or prot in target.parents:
            raise BootstrapError(f"refusing to mutate protected development path: {target}")


def assert_exact_path_operation(path: Path, *, recursive: bool = False) -> None:
    if recursive:
        raise BootstrapError(
            f"refusing recursive ownership/mode operation on {path} "
            "(exact path operations only)"
        )
    text = str(path).replace("\\", "/")
    # Refuse bare parents that would imply recursive intent on mixed trees.
    forbidden_parents = {
        "/var/lib/mk04",
        "/etc/mk04",
        "/var/log/mk04",
        "/opt/mk04",
    }
    if text.rstrip("/") in forbidden_parents:
        raise BootstrapError(
            f"refusing to chown/chmod parent tree {path}; operate on exact prod children only"
        )


def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def write_env_file_atomic(path: Path, updates: dict[str, str], *, create_from: Path | None = None) -> list[str]:
    """
    Merge key updates into an env file without logging values.

    Returns list of keys that were newly set (names only).
    """
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        existing = parse_env_file(path)
    elif create_from is not None and create_from.is_file():
        text = create_from.read_text(encoding="utf-8")
        existing = parse_env_file(create_from)
    else:
        text = ""
        existing = {}

    changed: dict[str, str] = {}
    applied: list[str] = []
    for key, new_value in updates.items():
        prior = existing.get(key, "")
        if prior and not _is_placeholder(prior):
            continue
        changed[key] = new_value
        applied.append(key)

    if not changed and path.is_file():
        return []

    lines = text.splitlines(keepends=True) if text else []
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"

    remaining = dict(changed)
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                new_lines.append(f"{key}={remaining.pop(key)}\n")
                continue
        new_lines.append(line if line.endswith("\n") else line + "\n")
    for key, value in remaining.items():
        new_lines.append(f"{key}={value}\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(new_lines)
        os.chmod(tmp_path, 0o0640)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return applied


def apply_owner_mode(
    path: Path,
    *,
    uid: int | None,
    gid: int | None,
    mode: int,
    allow_skip_chown: bool = False,
) -> str:
    """Set exact mode and ownership on one path. Never recursive."""
    assert_exact_path_operation(path, recursive=False)
    os.chmod(path, mode)
    if uid is None and gid is None:
        return f"mode {oct(mode)} {path}"
    try:
        os.chown(
            path,
            uid if uid is not None else -1,
            gid if gid is not None else -1,
        )
    except OSError as exc:
        if allow_skip_chown:
            return f"mode {oct(mode)} {path} (chown skipped: {exc})"
        raise BootstrapError(
            f"cannot set ownership on {path} to {uid}:{gid}: {exc}"
        ) from exc
    # Re-apply mode after chown (some filesystems reset bits).
    os.chmod(path, mode)
    if path.stat().st_mode & stat.S_IWOTH:
        raise BootstrapError(f"path is world-writable after permission apply: {path}")
    return f"owner/mode {uid}:{gid} {oct(mode)} {path}"


def resolve_ids(
    service_user: str, service_group: str
) -> tuple[int | None, int | None, int | None]:
    """Return (root_uid, service_uid, service_gid). Missing entries are None."""
    root_uid = 0
    try:
        root_uid = pwd.getpwnam("root").pw_uid
    except KeyError:
        root_uid = 0
    svc_uid, svc_gid = _uid_gid(service_user, service_group)
    return root_uid, svc_uid, svc_gid


def iter_seeded_config_files(layout: HostLayout) -> list[Path]:
    files: list[Path] = []
    env = layout.etc_prod / "env"
    if env.is_file():
        files.append(env)
    for rel in (
        "source-input/funnels.json",
        "video-automation/pipeline_config.json",
        "video-automation/video_pipeline_profiles.json",
        "output-funnel/settings.json",
        "output-funnel/channels.json",
    ):
        path = layout.etc_prod / rel
        if path.is_file():
            files.append(path)
    funnels = layout.etc_prod / "video-automation" / "funnels"
    if funnels.is_dir():
        files.extend(sorted(funnels.glob("*.json")))
    return files


def reconcile_config_file_permissions(
    layout: HostLayout,
    *,
    root_uid: int,
    service_gid: int | None,
    dry_run: bool,
    allow_skip_chown: bool,
) -> list[str]:
    notes: list[str] = []
    for path in iter_seeded_config_files(layout):
        if dry_run:
            notes.append(f"dry-run would set root:mk04 {oct(CONFIG_FILE_MODE)} {path}")
            continue
        before = path.read_bytes()
        notes.append(
            apply_owner_mode(
                path,
                uid=root_uid,
                gid=service_gid,
                mode=CONFIG_FILE_MODE,
                allow_skip_chown=allow_skip_chown,
            )
        )
        after = path.read_bytes()
        if before != after:
            raise BootstrapError(f"permission reconcile mutated config contents: {path}")
    # Parent etc dirs stay root:mk04 0750 (exact, non-recursive).
    for path, mode, role in layout.prepare_host_paths():
        if role != "etc" or not path.is_dir():
            continue
        if dry_run:
            notes.append(f"dry-run would ensure etc dir {path} {oct(ETC_DIR_MODE)}")
            continue
        notes.append(
            apply_owner_mode(
                path,
                uid=root_uid,
                gid=service_gid,
                mode=ETC_DIR_MODE,
                allow_skip_chown=allow_skip_chown,
            )
        )
    return notes


def reconcile_runtime_control_permissions(
    layout: HostLayout,
    *,
    service_uid: int | None,
    service_gid: int | None,
    dry_run: bool,
    allow_skip_chown: bool,
    enforce_values: bool = True,
) -> list[str]:
    notes: list[str] = []
    targets = (
        layout.var_data / "control_state.json",
        layout.var_ops_ui / "controls.json",
    )
    for path in targets:
        if not path.is_file():
            notes.append(f"runtime control absent (skip): {path}")
            continue
        if dry_run:
            notes.append(
                f"dry-run would set mk04:mk04 {oct(RUNTIME_CONTROL_MODE)} {path}"
            )
            continue
        before = path.read_bytes()
        notes.append(
            apply_owner_mode(
                path,
                uid=service_uid,
                gid=service_gid,
                mode=RUNTIME_CONTROL_MODE,
                allow_skip_chown=allow_skip_chown,
            )
        )
        after = path.read_bytes()
        if before != after:
            raise BootstrapError(f"permission reconcile mutated control contents: {path}")

    if enforce_values and not dry_run:
        cs = layout.var_data / "control_state.json"
        if cs.is_file():
            payload = json.loads(cs.read_text(encoding="utf-8"))
            if payload.get("uploads_disabled") is not True:
                raise BootstrapError("control_state uploads_disabled must be true")
            if payload.get("scheduler_disabled") is not True:
                raise BootstrapError("control_state scheduler_disabled must be true")
        controls = layout.var_ops_ui / "controls.json"
        if controls.is_file():
            payload = json.loads(controls.read_text(encoding="utf-8"))
            if payload.get("uploads_paused") is not True:
                raise BootstrapError("controls.json uploads_paused must be true")
        notes.append("runtime control safety values confirmed")
    return notes


def reconcile_lock_artifacts(
    layout: HostLayout,
    *,
    service_uid: int | None,
    service_gid: int | None,
    dry_run: bool,
    allow_skip_chown: bool,
) -> list[str]:
    notes: list[str] = []
    locks = layout.locks
    if not locks.is_dir():
        notes.append(f"locks dir absent: {locks}")
        return notes
    if dry_run:
        notes.append(
            f"dry-run would ensure locks dir mk04:mk04 02775 and artifacts {oct(LOCK_ARTIFACT_MODE)}"
        )
        return notes

    notes.append(
        apply_owner_mode(
            locks,
            uid=service_uid,
            gid=service_gid,
            mode=0o2775,
            allow_skip_chown=allow_skip_chown,
        )
    )
    for name in KNOWN_LOCK_ARTIFACTS:
        path = locks / name
        if not path.is_file():
            continue
        notes.append(
            apply_owner_mode(
                path,
                uid=service_uid,
                gid=service_gid,
                mode=LOCK_ARTIFACT_MODE,
                allow_skip_chown=allow_skip_chown,
            )
        )
    return notes


def assert_permission_contract(
    layout: HostLayout,
    *,
    service_user: str,
    service_group: str,
    skip_identity_probe: bool = False,
) -> list[str]:
    """Postcondition checks before install-services. No content mutation."""
    notes: list[str] = []
    errors: list[str] = []
    root_uid, svc_uid, svc_gid = resolve_ids(service_user, service_group)

    for path in iter_seeded_config_files(layout):
        st = path.stat()
        mode = stat.S_IMODE(st.st_mode)
        if mode != CONFIG_FILE_MODE:
            errors.append(f"{path}: mode {oct(mode)} != {oct(CONFIG_FILE_MODE)}")
        if st.st_mode & stat.S_IWOTH:
            errors.append(f"{path}: world-writable")
        if not skip_identity_probe:
            if svc_gid is not None and st.st_gid != svc_gid:
                errors.append(f"{path}: gid {st.st_gid} != {svc_gid}")
            if root_uid is not None and st.st_uid != root_uid:
                errors.append(f"{path}: uid {st.st_uid} != root({root_uid})")
        if not (st.st_mode & stat.S_IRGRP):
            errors.append(f"{path}: not group-readable")

    for path in (
        layout.var_data / "control_state.json",
        layout.var_ops_ui / "controls.json",
    ):
        if not path.is_file():
            errors.append(f"missing runtime control: {path}")
            continue
        st = path.stat()
        mode = stat.S_IMODE(st.st_mode)
        if mode != RUNTIME_CONTROL_MODE:
            errors.append(f"{path}: mode {oct(mode)} != {oct(RUNTIME_CONTROL_MODE)}")
        if st.st_mode & stat.S_IWOTH:
            errors.append(f"{path}: world-writable")
        if not skip_identity_probe:
            if svc_uid is not None and st.st_uid != svc_uid:
                errors.append(f"{path}: uid {st.st_uid} != {svc_uid}")
            if svc_gid is not None and st.st_gid != svc_gid:
                errors.append(f"{path}: gid {st.st_gid} != {svc_gid}")
        if not (st.st_mode & stat.S_IRUSR and st.st_mode & stat.S_IWUSR):
            errors.append(f"{path}: owner must read/write")
        if not (st.st_mode & stat.S_IRGRP and st.st_mode & stat.S_IWGRP):
            errors.append(f"{path}: group must read/write")

    if not layout.locks.is_dir():
        errors.append(f"locks missing: {layout.locks}")
    else:
        st = layout.locks.stat()
        if not (st.st_mode & stat.S_ISGID):
            errors.append(f"{layout.locks}: setgid missing")
        if not (st.st_mode & stat.S_IWGRP):
            errors.append(f"{layout.locks}: group write missing")
        if st.st_mode & stat.S_IWOTH:
            errors.append(f"{layout.locks}: world-writable")
        for name in KNOWN_LOCK_ARTIFACTS:
            path = layout.locks / name
            if not path.is_file():
                continue
            stf = path.stat()
            mode = stat.S_IMODE(stf.st_mode)
            if mode != LOCK_ARTIFACT_MODE:
                errors.append(f"{path}: mode {oct(mode)} != {oct(LOCK_ARTIFACT_MODE)}")
            if stf.st_mode & stat.S_IWOTH:
                errors.append(f"{path}: world-writable")
            if not (stf.st_mode & stat.S_IWGRP):
                errors.append(f"{path}: group write missing (O_RDWR required)")
            try:
                import fcntl

                fd = os.open(str(path), os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
                notes.append(f"lock O_RDWR flock ok: {path.name}")
            except OSError as exc:
                if not skip_identity_probe:
                    errors.append(f"{path}: O_RDWR flock failed ({exc})")

    if errors:
        raise BootstrapError(
            "permission contract unmet; refusing install-services:\n  - "
            + "\n  - ".join(errors)
        )
    notes.append("permission contract postconditions: ok")
    return notes


def phase_reconcile_permissions(opts: BootstrapOptions) -> list[str]:
    notes: list[str] = []
    allow_skip = bool(opts.skip_account)
    root_uid, svc_uid, svc_gid = resolve_ids(opts.service_user, opts.service_group)
    if not opts.dry_run and not allow_skip and (svc_uid is None or svc_gid is None):
        raise BootstrapError(
            f"cannot resolve {opts.service_user}:{opts.service_group} for permission reconcile"
        )

    notes.extend(
        reconcile_config_file_permissions(
            opts.layout,
            root_uid=root_uid if root_uid is not None else 0,
            service_gid=svc_gid,
            dry_run=opts.dry_run,
            allow_skip_chown=allow_skip,
        )
    )
    notes.extend(
        reconcile_runtime_control_permissions(
            opts.layout,
            service_uid=svc_uid,
            service_gid=svc_gid,
            dry_run=opts.dry_run,
            allow_skip_chown=allow_skip,
            enforce_values=True,
        )
    )
    notes.extend(
        reconcile_lock_artifacts(
            opts.layout,
            service_uid=svc_uid,
            service_gid=svc_gid,
            dry_run=opts.dry_run,
            allow_skip_chown=allow_skip,
        )
    )
    if not opts.dry_run:
        notes.extend(
            assert_permission_contract(
                opts.layout,
                service_user=opts.service_user,
                service_group=opts.service_group,
                skip_identity_probe=allow_skip,
            )
        )
    else:
        notes.append("dry-run: skipped permission contract assertion")
    return notes


def seed_file_if_absent(
    source: Path,
    dest: Path,
    *,
    dry_run: bool,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
    allow_skip_chown: bool = False,
) -> str:
    if dest.exists():
        if not dry_run:
            apply_owner_mode(
                dest,
                uid=owner_uid if owner_uid is not None else 0,
                gid=owner_gid,
                mode=CONFIG_FILE_MODE,
                allow_skip_chown=allow_skip_chown,
            )
        return f"preserve existing {dest}"
    if not source.is_file():
        raise BootstrapError(f"seed source missing: {source}")
    if dry_run:
        return f"dry-run would create {dest} from {source.name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    apply_owner_mode(
        dest,
        uid=owner_uid if owner_uid is not None else 0,
        gid=owner_gid,
        mode=CONFIG_FILE_MODE,
        allow_skip_chown=allow_skip_chown,
    )
    return f"created {dest}"


# Deny-only automation keys reconciled into existing production settings.json.
# Intervals and unrelated keys are preserved; secrets/credentials are never touched.
OUTPUT_FUNNEL_SAFETY_WORKER_KEYS = (
    "automation.plan_worker.enabled",
    "automation.upload_worker.enabled",
    "automation.auto_upload",
)


def _set_nested(data: dict[str, Any], dotted: str, value: Any) -> bool:
    """Set dotted path; return True if the stored value changed."""
    parts = dotted.split(".")
    cur: Any = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    leaf = parts[-1]
    prior = cur.get(leaf)
    if prior is value or prior == value:
        return False
    cur[leaf] = value
    return True


def reconcile_output_funnel_settings_safety(
    path: Path,
    *,
    dry_run: bool,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
    allow_skip_chown: bool = False,
) -> list[str]:
    """Force deny-only worker switches on existing settings without clobbering the rest."""
    if not path.is_file():
        return [f"output-funnel settings absent; skip safety reconcile: {path}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"cannot parse output-funnel settings {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BootstrapError(f"output-funnel settings must be a JSON object: {path}")

    desired: dict[str, Any] = {
        "automation.plan_worker.enabled": False,
        "automation.upload_worker.enabled": False,
        "automation.auto_upload": False,
    }
    changed: list[str] = []
    for key, value in desired.items():
        if _set_nested(payload, key, value):
            changed.append(f"{key}={json.dumps(value)}")

    if not changed:
        return [f"output-funnel settings safety keys already deny-only: {path}"]

    if dry_run:
        return ["dry-run would reconcile output-funnel settings: " + ", ".join(changed)]

    text = json.dumps(payload, indent=2) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    apply_owner_mode(
        path,
        uid=owner_uid if owner_uid is not None else 0,
        gid=owner_gid,
        mode=CONFIG_FILE_MODE,
        allow_skip_chown=allow_skip_chown,
    )
    return ["reconciled output-funnel settings safety keys: " + ", ".join(changed)]


def _load_prod_uploading_enabled(opts: BootstrapOptions) -> bool | None:
    """Return uploading.enabled from prod.yaml, or None if unreadable."""
    candidates = [
        opts.layout.opt_current / "config" / "environments" / "prod.yaml",
        opts.dev_root / "config" / "environments" / "prod.yaml",
    ]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return None
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        text = path.read_text(encoding="utf-8")
        match = re.search(
            r"(?m)^uploading:\s*\n(?:[ \t]+.+\n)*?[ \t]+enabled:\s*(true|false)\s*$",
            text,
        )
        if not match:
            return None
        return match.group(1).lower() == "true"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    uploading = data.get("uploading") if isinstance(data, dict) else None
    if not isinstance(uploading, dict) or "enabled" not in uploading:
        return None
    return bool(uploading.get("enabled"))


def assert_pre_systemd_safety(opts: BootstrapOptions) -> list[str]:
    """Fail closed before install-services unless upload/scheduler/worker deny posture holds.

    Never prints secret values — only key names and non-secret safety booleans/flags.
    """
    layout = opts.layout
    errors: list[str] = []
    notes: list[str] = []

    env_path = layout.etc_prod / "env"
    if not env_path.is_file():
        raise BootstrapError(
            "pre-systemd safety gate failed: missing production env "
            f"({env_path}); refuse install-services"
        )
    values = parse_env_file(env_path)
    required_env = {
        "MK04_UPLOAD_MODE": "dry_run",
        "MK04_SCHEDULER_MODE": "manual",
        "OUTPUT_FUNNEL_PLAN_WORKER_ENABLED": "0",
        "OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED": "0",
        "OUTPUT_FUNNEL_AUTO_UPLOAD": "0",
    }
    for key, expected in required_env.items():
        got = (values.get(key) or "").strip()
        if got != expected:
            errors.append(f"env {key} must be {expected!r} (got {got!r})")

    settings_path = layout.etc_prod / "output-funnel" / "settings.json"
    if not settings_path.is_file():
        errors.append(f"missing output-funnel settings: {settings_path}")
    else:
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"unreadable output-funnel settings ({exc.__class__.__name__})")
            settings = {}
        automation = settings.get("automation") if isinstance(settings, dict) else None
        if not isinstance(automation, dict):
            errors.append("settings.automation missing or invalid")
        else:
            if automation.get("auto_upload") is not False:
                errors.append("settings automation.auto_upload must be false")
            for worker in ("plan_worker", "upload_worker"):
                block = automation.get(worker)
                if not isinstance(block, dict) or block.get("enabled") is not False:
                    errors.append(f"settings automation.{worker}.enabled must be false")

    uploading_enabled = _load_prod_uploading_enabled(opts)
    if uploading_enabled is None:
        errors.append("could not resolve config/environments/prod.yaml uploading.enabled")
    elif uploading_enabled is not False:
        errors.append("uploading.enabled must be false")
    else:
        notes.append("uploading.enabled=false")

    cs = layout.var_data / "control_state.json"
    if not cs.is_file():
        errors.append(f"missing control_state: {cs}")
    else:
        try:
            state = json.loads(cs.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append("unreadable control_state")
            state = {}
        if state.get("uploads_disabled") is not True:
            errors.append("uploads_disabled must be true")
        if state.get("scheduler_disabled") is not True:
            errors.append("scheduler_disabled must be true")

    controls = layout.var_ops_ui / "controls.json"
    if not controls.is_file():
        errors.append(f"missing controls.json: {controls}")
    else:
        try:
            ctrl = json.loads(controls.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append("unreadable controls.json")
            ctrl = {}
        if ctrl.get("uploads_paused") is not True:
            errors.append("uploads_paused must be true")

    if errors:
        raise BootstrapError(
            "pre-systemd safety gate failed; refusing install-services:\n  - "
            + "\n  - ".join(errors)
        )
    notes.append("pre-systemd safety gate: ok (workers/scheduler/uploads deny-only)")
    return notes


class AccountManager:
    """System account/group operations (mockable)."""

    def __init__(self, run_cmd: Callable[..., subprocess.CompletedProcess[str]] | None = None):
        self._run = run_cmd or _default_run

    def group_exists(self, name: str) -> bool:
        try:
            grp.getgrnam(name)
            return True
        except KeyError:
            return False

    def user_exists(self, name: str) -> bool:
        try:
            pwd.getpwnam(name)
            return True
        except KeyError:
            return False

    def user_in_group(self, user: str, group: str) -> bool:
        try:
            g = grp.getgrnam(group)
        except KeyError:
            return False
        if user in g.gr_mem:
            return True
        try:
            return pwd.getpwnam(user).pw_gid == g.gr_gid
        except KeyError:
            return False

    def ensure_group(self, name: str, *, dry_run: bool) -> str:
        if self.group_exists(name):
            return f"group present: {name}"
        if dry_run:
            return f"dry-run would create group: {name}"
        self._run(["groupadd", "--system", name], check=True)
        return f"created group: {name}"

    def ensure_system_user(self, name: str, group: str, *, dry_run: bool) -> str:
        if self.user_exists(name):
            return f"user present: {name}"
        if dry_run:
            return f"dry-run would create system user: {name} (nologin, no password)"
        self._run(
            [
                "useradd",
                "--system",
                "--gid",
                group,
                "--home-dir",
                f"/var/lib/mk04",
                "--shell",
                "/usr/sbin/nologin",
                "--no-create-home",
                name,
            ],
            check=True,
        )
        return f"created system user: {name} (shell=/usr/sbin/nologin)"

    def ensure_operator_in_group(self, operator: str, group: str, *, dry_run: bool) -> str:
        if not self.user_exists(operator):
            raise BootstrapError(f"operator user does not exist: {operator}")
        if self.user_in_group(operator, group):
            return f"operator {operator} already in group {group}"
        if dry_run:
            return f"dry-run would add {operator} to group {group}"
        self._run(["usermod", "-aG", group, operator], check=True)
        return f"added {operator} to group {group}"


def _default_run(
    cmd: Sequence[str],
    *,
    check: bool = False,
    capture_output: bool = True,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        check=check,
        capture_output=capture_output,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else None,
    )


def _uid_gid(user: str, group: str) -> tuple[int | None, int | None]:
    uid = gid = None
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        pass
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        pass
    return uid, gid


def install_exact_dir(
    path: Path,
    *,
    mode: int,
    uid: int | None,
    gid: int | None,
    layout: HostLayout,
    dry_run: bool,
) -> str:
    assert_not_protected(path, layout)
    assert_exact_path_operation(path, recursive=False)
    if dry_run:
        return f"dry-run would ensure dir {path} mode={oct(mode)}"
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    if uid is not None and gid is not None:
        os.chown(path, uid, gid)
    elif gid is not None:
        os.chown(path, -1, gid)
    # Verify no world-write on locks/etc
    st = path.stat()
    if st.st_mode & stat.S_IWOTH:
        raise BootstrapError(f"path is world-writable after install: {path}")
    return f"ensured {path} mode={oct(mode & 0o7777)}"


def role_ownership(
    role: str, service_user: str, service_group: str
) -> tuple[str | None, str | None]:
    if role == "opt":
        return ("root", service_group)
    if role == "runtime":
        return (service_user, service_group)
    if role == "log":
        return (service_user, service_group)
    if role == "etc":
        return ("root", service_group)
    if role == "locks":
        return (service_user, service_group)
    return (None, None)


def verify_lock_access(locks: Path, *, as_user: str | None = None) -> None:
    """Non-destructive open/flock probe."""
    import fcntl

    probe = locks / ".bootstrap_lock_probe"
    try:
        fd = os.open(str(probe), os.O_CREAT | os.O_RDWR, 0o0660)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError as exc:
        who = as_user or getpass.getuser()
        raise BootstrapError(f"lock directory not writable for {who}: {locks} ({exc})") from exc


def validate_seeded_env(env_path: Path) -> list[str]:
    errors: list[str] = []
    values = parse_env_file(env_path)
    for key, expected in PROD_PORTS.items():
        got = values.get(key, "")
        if got != expected:
            errors.append(f"{key} must be {expected}, got {got!r}")
    for key, value in values.items():
        for marker in DEV_PORT_MARKERS:
            if marker in value:
                errors.append(f"dev port leakage in {key}")
                break
        if "/mk04/dev" in value or "/data/dev" in value:
            errors.append(f"dev path leakage in {key}")
    if values.get("MK04_SCHEDULER_MODE", "").strip().lower() != "manual":
        errors.append("MK04_SCHEDULER_MODE must be manual")
    for key in (
        "OUTPUT_FUNNEL_PLAN_WORKER_ENABLED",
        "OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED",
        "OUTPUT_FUNNEL_AUTO_UPLOAD",
    ):
        if values.get(key, "1").strip() not in {"0", "false", "False"}:
            errors.append(f"{key} must be 0")
    if values.get("MK04_UPLOAD_MODE", "").strip().lower() != "dry_run":
        errors.append("MK04_UPLOAD_MODE must be dry_run")
    return errors


def generate_internal_secrets(
    env_path: Path,
    *,
    dry_run: bool,
    factory: Callable[[], str] | None,
) -> list[str]:
    """Generate missing internal secrets. Returns key names only — never values."""
    factory = factory or (lambda: secrets.token_urlsafe(32))
    existing = parse_env_file(env_path) if env_path.is_file() else {}
    updates: dict[str, str] = {}
    for key in INTERNAL_SECRETS:
        if key in NEVER_GENERATE:
            continue
        prior = existing.get(key, "")
        if prior and not _is_placeholder(prior):
            continue
        updates[key] = factory()
    if dry_run:
        return [f"dry-run would set {k}" for k in updates]
    if not updates:
        return []
    applied = write_env_file_atomic(env_path, updates)
    try:
        os.chmod(env_path, 0o0640)
    except OSError:
        pass
    return [f"initialized secret: {k}" for k in applied]


def initialize_runtime_controls(
    layout: HostLayout,
    *,
    dry_run: bool,
    service_uid: int | None = None,
    service_gid: int | None = None,
    allow_skip_chown: bool = False,
) -> list[str]:
    notes: list[str] = []
    data_root = layout.var_data
    controls = layout.var_ops_ui / "controls.json"
    control_state = data_root / "control_state.json"
    if dry_run:
        return [
            f"dry-run would set uploads_disabled=true in {control_state}",
            f"dry-run would set uploads_paused=true in {controls}",
            "dry-run would set scheduler_disabled=true",
        ]

    data_root.mkdir(parents=True, exist_ok=True)
    layout.var_ops_ui.mkdir(parents=True, exist_ok=True)

    # control_state.json — uploads + scheduler
    state: dict[str, Any] = {}
    if control_state.is_file():
        try:
            state = json.loads(control_state.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, json.JSONDecodeError):
            state = {}
    state.update(
        {
            "environment": "prod",
            "uploads_disabled": True,
            "scheduler_disabled": True,
            "updated_at": _utc_now(),
            "updated_by": getpass.getuser(),
            "reason": "first_production_bootstrap",
        }
    )
    _atomic_json(
        control_state,
        state,
        uid=service_uid,
        gid=service_gid,
        mode=RUNTIME_CONTROL_MODE,
        allow_skip_chown=allow_skip_chown,
    )
    notes.append(f"wrote uploads_disabled=true, scheduler_disabled=true → {control_state}")

    ctrl: dict[str, Any] = {}
    if controls.is_file():
        try:
            ctrl = json.loads(controls.read_text(encoding="utf-8"))
            if not isinstance(ctrl, dict):
                ctrl = {}
        except (OSError, json.JSONDecodeError):
            ctrl = {}
    ctrl["uploads_paused"] = True
    _atomic_json(
        controls,
        ctrl,
        uid=service_uid,
        gid=service_gid,
        mode=RUNTIME_CONTROL_MODE,
        allow_skip_chown=allow_skip_chown,
    )
    notes.append(f"wrote uploads_paused=true → {controls}")
    return notes


def _atomic_json(
    path: Path,
    payload: dict[str, Any],
    *,
    uid: int | None = None,
    gid: int | None = None,
    mode: int = RUNTIME_CONTROL_MODE,
    allow_skip_chown: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".json.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    apply_owner_mode(
        path,
        uid=uid,
        gid=gid,
        mode=mode,
        allow_skip_chown=allow_skip_chown,
    )


def phase_prepare_host(opts: BootstrapOptions) -> list[str]:
    notes: list[str] = []
    layout = opts.layout
    accounts = AccountManager(opts.run_cmd)

    if not opts.skip_account:
        notes.append(accounts.ensure_group(opts.service_group, dry_run=opts.dry_run))
        notes.append(
            accounts.ensure_system_user(
                opts.service_user, opts.service_group, dry_run=opts.dry_run
            )
        )
        notes.append(
            accounts.ensure_operator_in_group(
                opts.operator, opts.service_group, dry_run=opts.dry_run
            )
        )
        if not opts.dry_run:
            notes.append(
                f"NOTE: {opts.operator} must re-login or run `newgrp {opts.service_group}` "
                "before group access is available in existing shells."
            )

    uid, gid = (None, None)
    if not opts.dry_run and not opts.skip_account:
        uid, gid = _uid_gid(opts.service_user, opts.service_group)
        if gid is None:
            raise BootstrapError(f"group {opts.service_group} not resolvable after ensure")

    # Ordering: locks before etc/prod marker
    created_locks = False
    for path, mode, role in layout.prepare_host_paths():
        owner_user, owner_group = role_ownership(role, opts.service_user, opts.service_group)
        path_uid = path_gid = None
        if not opts.dry_run and not opts.skip_account:
            if owner_user == "root":
                path_uid = 0
            elif owner_user:
                path_uid = pwd.getpwnam(owner_user).pw_uid if accounts.user_exists(owner_user) else uid
            if owner_group:
                path_gid = (
                    grp.getgrnam(owner_group).gr_gid
                    if accounts.group_exists(owner_group)
                    else gid
                )
        elif opts.skip_account and not opts.dry_run:
            # Test sandbox: create dirs without chown
            path_uid = path_gid = None

        if path == layout.locks:
            created_locks = True
        if path == layout.etc_prod and not created_locks and not layout.locks.exists() and not opts.dry_run:
            raise BootstrapError("internal error: locks must be created before /etc/mk04/prod")

        notes.append(
            install_exact_dir(
                path,
                mode=mode,
                uid=path_uid,
                gid=path_gid,
                layout=layout,
                dry_run=opts.dry_run,
            )
        )

    if not opts.dry_run:
        verify_lock_access(layout.locks)
        notes.append(f"lock flock probe ok: {layout.locks}")

        # Confirm production_installation_present forces shared locks (when not prefixed)
        if layout.prefix is None:
            sys.path.insert(0, str(OPS_DIR))
            import execution_gate as eg  # noqa: PLC0415

            if not eg.production_installation_present():
                raise BootstrapError(
                    "expected production_installation_present after creating /etc/mk04/prod"
                )
            # Clear explicit override for the check
            prior = os.environ.pop("MK04_SHARED_LOCK_ROOT", None)
            try:
                resolved = eg.resolve_shared_lock_root(environment="dev", allow_dev_fallback=True)
                if resolved != layout.locks.resolve():
                    raise BootstrapError(
                        f"dev lock root must be {layout.locks}, got {resolved}"
                    )
                notes.append(
                    "production_installation_present forces shared lock root "
                    f"{layout.locks} (no repo fallback)"
                )
            finally:
                if prior is not None:
                    os.environ["MK04_SHARED_LOCK_ROOT"] = prior

    notes.append("cron: NOT installed (explicit exclusion)")
    notes.append("scheduler: remains manual/uninstalled")
    notes.append("uploads: remain disabled (config seed + controls in later phases)")
    return notes


def phase_seed_config(opts: BootstrapOptions) -> list[str]:
    notes: list[str] = []
    layout = opts.layout
    root = opts.dev_root
    allow_skip = bool(opts.skip_account)
    root_uid, _svc_uid, svc_gid = resolve_ids(opts.service_user, opts.service_group)

    if not layout.locks.exists() and not opts.dry_run:
        raise BootstrapError("locks directory missing; run prepare-host first")

    for rel_src, rel_dest in SEED_FILES:
        source = root / rel_src
        dest = layout.etc_prod / rel_dest
        notes.append(
            seed_file_if_absent(
                source,
                dest,
                dry_run=opts.dry_run,
                owner_uid=root_uid,
                owner_gid=svc_gid,
                allow_skip_chown=allow_skip,
            )
        )

    # Reconcile deny-only worker switches even when settings.json already exists.
    notes.extend(
        reconcile_output_funnel_settings_safety(
            layout.etc_prod / "output-funnel" / "settings.json",
            dry_run=opts.dry_run,
            owner_uid=root_uid,
            owner_gid=svc_gid,
            allow_skip_chown=allow_skip,
        )
    )

    # Funnel JSON copies (only missing)
    funnels_src = root / "video-automation" / "config" / "funnels"
    funnels_dest = layout.etc_prod / "video-automation" / "funnels"
    if funnels_src.is_dir():
        if not opts.dry_run:
            funnels_dest.mkdir(parents=True, exist_ok=True)
        for src in sorted(funnels_src.glob("*.json")):
            dest = funnels_dest / src.name
            notes.append(
                seed_file_if_absent(
                    src,
                    dest,
                    dry_run=opts.dry_run,
                    owner_uid=root_uid,
                    owner_gid=svc_gid,
                    allow_skip_chown=allow_skip,
                )
            )

    env_path = layout.etc_prod / "env"
    if opts.dry_run and not env_path.is_file():
        notes.append("dry-run: skip secret generation until env exists")
        return notes

    # Force safety keys even if operator had an older template
    safety = {
        "MK04_SCHEDULER_MODE": "manual",
        "OUTPUT_FUNNEL_PLAN_WORKER_ENABLED": "0",
        "OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED": "0",
        "OUTPUT_FUNNEL_AUTO_UPLOAD": "0",
        "MK04_UPLOAD_MODE": "dry_run",
        "MK04_ENV": "prod",
        "MK04_CODE_ROOT": "/opt/mk04/prod/current",
        "MK04_CONFIG_ROOT": "/etc/mk04/prod",
        "MK04_RUNTIME_ROOT": "/var/lib/mk04/prod",
        "MK04_LOG_ROOT": "/var/log/mk04/prod",
        **PROD_PORTS,
    }
    if opts.dry_run:
        notes.append("dry-run would enforce safety keys in env")
    else:
        # Only set safety keys when placeholder/missing — but always force the
        # bootstrap safety toggles listed above (they are not secrets).
        existing = parse_env_file(env_path)
        force_updates = dict(safety)
        # Preserve non-placeholder path roots only if they already match prod layout
        applied = write_env_file_atomic(env_path, force_updates)
        notes.append(f"enforced safety keys ({len(applied)} updated): " + ", ".join(applied) if applied else "safety keys already set")
        # write_env_file_atomic skips non-placeholder — force overwrite for known safety toggles
        text = env_path.read_text(encoding="utf-8")
        for key, value in (
            ("MK04_SCHEDULER_MODE", "manual"),
            ("OUTPUT_FUNNEL_PLAN_WORKER_ENABLED", "0"),
            ("OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED", "0"),
            ("OUTPUT_FUNNEL_AUTO_UPLOAD", "0"),
            ("MK04_UPLOAD_MODE", "dry_run"),
        ):
            pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
            if pattern.search(text):
                text = pattern.sub(f"{key}={value}", text)
            else:
                text = text.rstrip() + f"\n{key}={value}\n"
        env_path.write_text(text, encoding="utf-8")
        apply_owner_mode(
            env_path,
            uid=root_uid,
            gid=svc_gid,
            mode=CONFIG_FILE_MODE,
            allow_skip_chown=allow_skip,
        )

        notes.extend(
            generate_internal_secrets(
                env_path, dry_run=False, factory=opts.secret_factory
            )
        )
        apply_owner_mode(
            env_path,
            uid=root_uid,
            gid=svc_gid,
            mode=CONFIG_FILE_MODE,
            allow_skip_chown=allow_skip,
        )

        errors = validate_seeded_env(env_path)
        if errors:
            raise BootstrapError("seeded env validation failed:\n  - " + "\n  - ".join(errors))
        notes.append("seeded env port/safety validation: ok")

        # Conditional production secrets validator (no platform creds required)
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "config"))
        from production_secrets import validate_production_secrets  # noqa: PLC0415

        env_map = parse_env_file(env_path)
        result = validate_production_secrets(
            require_production_secrets=True,
            uploading_enabled=False,
            upload_mode="dry_run",
            channels_path=layout.etc_prod / "output-funnel" / "channels.json",
            environ=env_map,
        )
        if not result.ok:
            raise BootstrapError(
                "production secret validation failed (names only): "
                + "; ".join(result.errors)
            )
        notes.append(
            "production_secrets validation: ok "
            f"(required={result.required_names or 'none'}; warnings={len(result.warnings)})"
        )
        notes.append(
            "Ops UI password retrieval (local, after bootstrap — not printed here):\n"
            "  sudo grep '^OPS_UI_OPERATOR_PASSWORD=' /etc/mk04/prod/env\n"
            "To rotate later: set a new value in that file (mode 0640) and restart mk04-ops-ui."
        )
        notes.extend(
            reconcile_config_file_permissions(
                layout,
                root_uid=root_uid if root_uid is not None else 0,
                service_gid=svc_gid,
                dry_run=False,
                allow_skip_chown=allow_skip,
            )
        )
    notes.append("platform credentials: NOT connected")
    notes.append("cron: NOT installed")
    return notes


def phase_promote(opts: BootstrapOptions) -> list[str]:
    if opts.skip_external:
        return ["skip_external: promote not invoked"]
    if opts.dry_run:
        return [
            "dry-run would run: deploy/scripts/promote-to-prod.sh "
            f"--source {opts.dev_root} --no-restart --allow-first-bootstrap "
            f"--prod-base {opts.layout.opt_prod}"
        ]
    script = opts.dev_root / "deploy" / "scripts" / "promote-to-prod.sh"
    if not script.is_file():
        raise BootstrapError(f"promoter missing: {script}")
    env = os.environ.copy()
    env["MK04_SHARED_LOCK_ROOT"] = str(opts.layout.locks)
    cmd = [
        str(script),
        "--source",
        str(opts.dev_root),
        "--no-restart",
        "--allow-first-bootstrap",
        "--prod-base",
        str(opts.layout.opt_prod),
    ]
    run = opts.run_cmd or _default_run
    _log(f"Running promoter: {' '.join(cmd)}")
    result = run(cmd, check=False, env=env, cwd=str(opts.dev_root))
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        # Never echo secrets; promoter should not print any.
        raise BootstrapError(
            f"promotion failed (exit {result.returncode}). "
            "Staging/release evidence left for diagnosis. "
            "Do not manually create current.\n"
            f"stdout_tail={stdout[-2000:]}\nstderr_tail={stderr[-2000:]}"
        )
    return [
        "promotion completed with --no-restart --allow-first-bootstrap",
        f"current → {opts.layout.opt_current}",
    ]


def phase_component_bootstrap(opts: BootstrapOptions) -> list[str]:
    notes: list[str] = []
    allow_skip = bool(opts.skip_account)
    _root_uid, svc_uid, svc_gid = resolve_ids(opts.service_user, opts.service_group)
    control_kwargs = dict(
        service_uid=svc_uid,
        service_gid=svc_gid,
        allow_skip_chown=allow_skip,
    )
    if opts.skip_external:
        notes.extend(
            initialize_runtime_controls(
                opts.layout, dry_run=opts.dry_run, **control_kwargs
            )
        )
        return ["skip_external: component bootstrap not invoked", *notes]
    if opts.dry_run:
        return [
            f"dry-run would run: {opts.layout.opt_current}/deploy/scripts/bootstrap.sh prod",
            *initialize_runtime_controls(opts.layout, dry_run=True, **control_kwargs),
        ]
    current = opts.layout.opt_current
    if not current.exists():
        raise BootstrapError(f"production current missing; promote first: {current}")
    script = current / "deploy" / "scripts" / "bootstrap.sh"
    if not script.is_file():
        raise BootstrapError(f"bootstrap.sh missing in current: {script}")
    env = os.environ.copy()
    env["MK04_ENV"] = "prod"
    # When using path-prefix sandboxes, point roots via env if needed — real host uses defaults.
    run = opts.run_cmd or _default_run
    result = run([str(script), "prod"], check=False, env=env, cwd=str(current))
    if result.returncode != 0:
        raise BootstrapError(
            f"component bootstrap failed (exit {result.returncode}). "
            f"stderr_tail={(result.stderr or '')[-2000:]}"
        )
    notes.append("component bootstrap completed (venvs preserved if symlinked)")
    notes.extend(
        initialize_runtime_controls(opts.layout, dry_run=False, **control_kwargs)
    )
    notes.append("upload workers / plan worker remain off via seeded env")
    notes.append("cron: still absent")
    return notes


SERVICE_ACTIVE_WAIT_SEC = 60
SERVICE_ACTIVE_POLL_SEC = 1


def _systemctl_show(run: Callable[..., subprocess.CompletedProcess[str]], unit: str) -> dict[str, str]:
    result = run(
        ["systemctl", "show", unit, "-p", "ActiveState", "-p", "UnitFileState", "-p", "SubState"],
        check=False,
    )
    out: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def _capture_unit_pre_states(
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, dict[str, str]]:
    states: dict[str, dict[str, str]] = {}
    for unit in SYSTEMD_UNITS:
        shown = _systemctl_show(run, unit)
        active = (shown.get("ActiveState") or "").strip() or "unknown"
        enabled = (shown.get("UnitFileState") or "").strip() or "unknown"
        states[unit] = {
            "active": active,
            "enabled": enabled,
            "sub": (shown.get("SubState") or "").strip(),
        }
    return states


def _wait_for_unit_active(
    run: Callable[..., subprocess.CompletedProcess[str]],
    unit: str,
    *,
    timeout_sec: float = SERVICE_ACTIVE_WAIT_SEC,
    poll_sec: float = SERVICE_ACTIVE_POLL_SEC,
) -> str:
    """Poll until active, or until a terminal failure / timeout.

    Never treats transient ``activating`` as success. Never returns success for
    failed/auto-restart/reloading after the wait budget.
    """
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    last = "unknown"
    while True:
        shown = _systemctl_show(run, unit)
        last = (shown.get("ActiveState") or "").strip() or "unknown"
        sub = (shown.get("SubState") or "").strip()
        if last == "active" and sub not in {"auto-restart", "final-sigterm", "final-sigkill"}:
            return "active"
        if last in {"failed", "inactive", "deactivating"} and time.monotonic() >= deadline:
            return last
        if last == "failed":
            # Fail fast on hard failure; do not wait out a restart loop forever.
            if sub in {"failed", "dead", "exit-code", "signal", "core-dump"}:
                return "failed"
        if time.monotonic() >= deadline:
            return last
        time.sleep(max(0.2, float(poll_sec)))


def _rollback_install_services(
    run: Callable[..., subprocess.CompletedProcess[str]],
    *,
    touched: Sequence[str],
    pre_states: Mapping[str, Mapping[str, str]],
    notes: list[str],
) -> None:
    """Stop/disable units newly enabled by a failed install; restore priors otherwise.

    Unit files are left in place for diagnosis. Previously healthy enabled units
    are not disabled.
    """
    for unit in reversed(list(touched)):
        pre = pre_states.get(unit, {})
        was_enabled = pre.get("enabled") in {"enabled", "static", "linked", "linked-runtime"}
        was_active = pre.get("active") == "active"
        if was_enabled:
            if was_active:
                run(["systemctl", "restart", unit], check=False)
                notes.append(f"rollback: left {unit} enabled; attempted restart to prior active state")
            else:
                run(["systemctl", "stop", unit], check=False)
                notes.append(f"rollback: left {unit} enabled; stopped after failed install attempt")
            continue
        run(["systemctl", "disable", "--now", unit], check=False)
        notes.append(f"rollback: stopped and disabled {unit} (newly enabled this attempt)")


def phase_install_services(opts: BootstrapOptions) -> list[str]:
    notes: list[str] = []
    layout = opts.layout
    if opts.dry_run:
        for unit in SYSTEMD_UNITS:
            notes.append(f"dry-run would install {unit} → {layout.systemd_dir / unit}")
        notes.append("dry-run would systemctl daemon-reload")
        notes.append("dry-run would enable/start services (no cron, no pipeline)")
        if (layout.etc_prod / "env").is_file():
            notes.extend(assert_pre_systemd_safety(opts))
        else:
            notes.append("dry-run: skip pre-systemd safety gate (env not present yet)")
        return notes

    notes.extend(
        assert_permission_contract(
            layout,
            service_user=opts.service_user,
            service_group=opts.service_group,
            skip_identity_probe=bool(opts.skip_account),
        )
    )
    notes.extend(assert_pre_systemd_safety(opts))

    unit_src_root = opts.dev_root / "deploy" / "systemd"
    if opts.layout.opt_current.exists():
        alt = opts.layout.opt_current / "deploy" / "systemd"
        if alt.is_dir():
            unit_src_root = alt

    if opts.skip_external:
        # Still copy unit files into sandbox systemd dir for tests
        layout.systemd_dir.mkdir(parents=True, exist_ok=True)
        for unit in SYSTEMD_UNITS:
            src = unit_src_root / unit
            if not src.is_file():
                raise BootstrapError(f"unit template missing: {src}")
            dest = layout.systemd_dir / unit
            shutil.copy2(src, dest)
            text = dest.read_text(encoding="utf-8")
            _assert_unit_text(text, unit)
            notes.append(f"installed unit file (no systemctl): {dest}")
        return notes

    run = opts.run_cmd or _default_run
    layout.systemd_dir.mkdir(parents=True, exist_ok=True)
    for unit in SYSTEMD_UNITS:
        src = unit_src_root / unit
        if not src.is_file():
            raise BootstrapError(f"unit template missing: {src}")
        text = src.read_text(encoding="utf-8")
        _assert_unit_text(text, unit)
        dest = layout.systemd_dir / unit
        shutil.copy2(src, dest)
        notes.append(f"installed {dest}")

    run(["systemctl", "daemon-reload"], check=True)
    notes.append("systemctl daemon-reload ok")

    pre_states = _capture_unit_pre_states(run)
    notes.append(
        "recorded pre-install unit states: "
        + ", ".join(
            f"{u}={pre_states[u]['enabled']}/{pre_states[u]['active']}" for u in SYSTEMD_UNITS
        )
    )

    # Ensure shared Ollama if present — do not install a second instance.
    ollama = run(["systemctl", "list-unit-files", "ollama.service"], check=False)
    if ollama.returncode == 0 and "ollama.service" in (ollama.stdout or ""):
        run(["systemctl", "start", "ollama.service"], check=False)
        notes.append("started existing ollama.service (shared dependency)")
    else:
        notes.append("ollama.service not installed as a unit; ai-service may start it best-effort")

    touched: list[str] = []
    try:
        for unit in SERVICE_START_ORDER:
            if unit == "ollama.service":
                continue
            run(["systemctl", "enable", "--now", unit], check=True)
            touched.append(unit)
            notes.append(f"enabled and start requested: {unit}")
            state = _wait_for_unit_active(run, unit)
            if state != "active":
                raise BootstrapError(
                    f"service not active after start wait: {unit} (state={state!r})"
                )
            notes.append(f"active: {unit}")

        # Final gate: never claim success while any unit is failed/restarting.
        for unit in SYSTEMD_UNITS:
            shown = _systemctl_show(run, unit)
            active = (shown.get("ActiveState") or "").strip()
            sub = (shown.get("SubState") or "").strip()
            if active != "active" or sub in {"auto-restart", "final-sigterm", "final-sigkill"}:
                raise BootstrapError(
                    f"service not healthy after install: {unit} "
                    f"(ActiveState={active!r} SubState={sub!r})"
                )
    except Exception as exc:
        notes.append(f"install-services failed: {exc}")
        _rollback_install_services(run, touched=touched, pre_states=pre_states, notes=notes)
        notes.append("unit files preserved under systemd for diagnosis")
        if isinstance(exc, BootstrapError):
            raise
        raise BootstrapError(f"install-services failed: {exc}") from exc

    notes.append("no cron unit installed")
    notes.append("no pipeline triggered by service install")
    return notes


def _assert_unit_text(text: str, unit: str) -> None:
    if "EnvironmentFile=-/etc/mk04/prod/env" in text:
        raise BootstrapError(
            f"{unit} primary EnvironmentFile must be mandatory "
            "(EnvironmentFile=/etc/mk04/prod/env), not optional"
        )
    required = (
        "User=mk04",
        "Group=mk04",
        "/opt/mk04/prod/current",
        "EnvironmentFile=/etc/mk04/prod/env",
    )
    for token in required:
        if token not in text:
            raise BootstrapError(f"{unit} missing required token {token!r}")
    if not re.search(
        r"(?m)^EnvironmentFile=-/etc/mk04/prod/services/[A-Za-z0-9_.-]+\.env\s*$",
        text,
    ):
        raise BootstrapError(
            f"{unit} must keep optional service override "
            "EnvironmentFile=-/etc/mk04/prod/services/<service>.env"
        )
    for bad in ("/etc/mk04/dev", "/var/lib/mk04/dev", ":5160", ":5150", ":5155", ":5170", ":5175"):
        if bad in text:
            raise BootstrapError(f"{unit} contains forbidden reference {bad!r}")


def phase_install_commands(opts: BootstrapOptions) -> list[str]:
    target = opts.commands_target or opts.layout.commands_dir
    script = opts.dev_root / "deploy" / "scripts" / "install-operator-commands.sh"
    if opts.dry_run:
        return [
            f"dry-run would run install-operator-commands.sh --target-dir {target} "
            f"--dev-root {opts.dev_root}"
        ]
    if opts.skip_external:
        # Delegate still, but against sandbox target — installer is safe.
        pass
    run = opts.run_cmd or _default_run
    cmd = [
        str(script),
        "--target-dir",
        str(target),
        "--dev-root",
        str(opts.dev_root),
    ]
    result = run(cmd, check=False)
    if result.returncode != 0:
        raise BootstrapError(
            f"command installer failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout or '')[-1500:]}"
        )
    notes = [f"installed operator commands into {target}"]
    # Non-mutating validation only
    check = run([str(script), "--target-dir", str(target), "--check"], check=False)
    if check.returncode != 0:
        raise BootstrapError("install-operator-commands --check failed")
    notes.append("installer --check: ok")
    for name in ("dev", "prod", "promote"):
        help_r = run([str(target / name), "--help"], check=False)
        if help_r.returncode != 0:
            raise BootstrapError(f"{name} --help failed")
        notes.append(f"{name} --help: ok")
    return notes


def phase_verify(opts: BootstrapOptions) -> list[str]:
    notes: list[str] = []
    layout = opts.layout
    if opts.dry_run:
        return ["dry-run verify: no host reads required beyond planning"]

    if not layout.locks.is_dir():
        raise BootstrapError(f"locks missing: {layout.locks}")
    notes.append(f"locks present: {layout.locks}")

    if layout.opt_current.exists():
        if layout.opt_current.is_symlink() or layout.prefix is not None:
            notes.append(f"current present: {layout.opt_current}")
        else:
            # real host expects symlink into releases
            if layout.prefix is None and not layout.opt_current.is_symlink():
                # allow directory during skip_external tests
                notes.append(f"current present (not symlink in sandbox): {layout.opt_current}")
            else:
                notes.append(f"current symlink: {layout.opt_current} → {os.readlink(layout.opt_current)}")
    else:
        notes.append("current not yet present (promote phase pending)")

    env_path = layout.etc_prod / "env"
    if env_path.is_file():
        errors = validate_seeded_env(env_path)
        if errors:
            raise BootstrapError("verify env failed: " + "; ".join(errors))
        notes.append("env safety/ports: ok")
        for key in INTERNAL_SECRETS:
            val = parse_env_file(env_path).get(key, "")
            notes.append(f"secret {key}: {'set' if val and not _is_placeholder(val) else 'MISSING'}")

    cs = layout.var_data / "control_state.json"
    if cs.is_file():
        payload = json.loads(cs.read_text(encoding="utf-8"))
        if payload.get("uploads_disabled") is not True:
            raise BootstrapError("uploads_disabled is not true")
        if payload.get("scheduler_disabled") is not True:
            raise BootstrapError("scheduler_disabled is not true")
        notes.append("control_state: uploads_disabled + scheduler_disabled")

    controls = layout.var_ops_ui / "controls.json"
    if controls.is_file():
        payload = json.loads(controls.read_text(encoding="utf-8"))
        if payload.get("uploads_paused") is not True:
            raise BootstrapError("uploads_paused is not true")
        notes.append("controls: uploads_paused=true")

    # Cron absence
    cron_paths = [
        Path("/etc/cron.d/mk04"),
        Path("/etc/cron.d/mk04.cron.d"),
    ]
    if layout.prefix is not None:
        cron_paths = [layout.prefix / p.lstrip("/") for p in ("/etc/cron.d/mk04",)]
    for cron in cron_paths:
        if cron.exists():
            raise BootstrapError(f"unexpected cron entry present: {cron}")
    notes.append("cron: absent")

    for protected in layout.protected_paths():
        notes.append(f"protected path not targeted: {protected}")

    notes.append("no content pipeline invoked by bootstrap")
    notes.append("no platform API calls invoked by bootstrap")
    return notes


PHASE_HANDLERS: dict[str, Callable[[BootstrapOptions], list[str]]] = {
    "prepare-host": phase_prepare_host,
    "seed-config": phase_seed_config,
    "promote": phase_promote,
    "component-bootstrap": phase_component_bootstrap,
    "reconcile-permissions": phase_reconcile_permissions,
    "install-services": phase_install_services,
    "install-commands": phase_install_commands,
    "verify": phase_verify,
}


def plan_summary(opts: BootstrapOptions) -> str:
    layout = opts.layout
    lines = [
        "=== Production host bootstrap plan ===",
        f"dev checkout:     {opts.dev_root}",
        f"operator:         {opts.operator}",
        f"service user/group: {opts.service_user}:{opts.service_group}",
        f"dry_run:          {opts.dry_run}",
        f"phases:           {', '.join(opts.phases)}",
        "",
        "Paths to create (exact; no recursive parent chown):",
    ]
    for path, mode, role in layout.prepare_host_paths():
        lines.append(f"  {path}  mode={oct(mode)} role={role}")
    lines.extend(
        [
            "",
            "Protected (never recursively modified):",
            *[f"  {p}" for p in layout.protected_paths()],
            "  repository data/dev, runs/dev, jobs/dev",
            "",
            "Exclusions:",
            "  - cron: NOT installed",
            "  - uploads: remain disabled (YAML false, dry_run, runtime, paused)",
            "  - scheduler: manual / not installed",
            "  - content pipeline: will NOT run",
            "  - platform APIs: will NOT be called",
            "  - repository ownership / ~/.local/bin: unchanged",
        ]
    )
    return "\n".join(lines)


def run_bootstrap(opts: BootstrapOptions) -> int:
    if not opts.dry_run and not opts.apply:
        raise BootstrapError("refusing mutation without --apply (or pass --dry-run)")
    if opts.dry_run and opts.apply:
        _log("NOTE: --dry-run takes precedence; no writes will be performed")

    # Effective dry_run
    effective = replace(opts, dry_run=True) if opts.dry_run else opts

    _log(plan_summary(effective))
    _log("")

    for phase in effective.phases:
        if effective.stop_before and phase == effective.stop_before:
            _log(f"STOP before phase: {phase} (pause point)")
            return 0
        if phase not in PHASE_HANDLERS:
            raise BootstrapError(f"unknown phase: {phase}")
        _log(f"--- phase: {phase} ---")
        notes = PHASE_HANDLERS[phase](effective)
        for note in notes:
            # Redact any accidental secret-looking assignments
            safe = re.sub(
                r"((?:SECRET|PASSWORD|TOKEN|KEY)=)\S+",
                r"\1***",
                note,
                flags=re.I,
            )
            _log(f"  {safe}")
        _log(f"--- phase complete: {phase} ---")
    _log("Bootstrap orchestration finished.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="First production host bootstrap (idempotent, phased)",
    )
    p.add_argument("--operator", default="maguireltd", help="Human operator added to mk04 group")
    p.add_argument(
        "--dev-root",
        type=Path,
        default=REPO_ROOT,
        help="Absolute development checkout (default: this repository)",
    )
    p.add_argument("--service-user", default="mk04")
    p.add_argument("--service-group", default="mk04")
    p.add_argument("--dry-run", action="store_true", help="Plan only; no writes")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Required for real mutation (with explicit phase selection)",
    )
    p.add_argument(
        "--phase",
        action="append",
        dest="phases",
        choices=PHASES,
        help="Run only these phases (repeatable). Default: all.",
    )
    p.add_argument(
        "--stop-before",
        choices=PHASES,
        help="Stop before this phase (operator pause point)",
    )
    p.add_argument(
        "--path-prefix",
        type=Path,
        help="Sandbox all host paths under this prefix (tests only)",
    )
    p.add_argument(
        "--commands-target",
        type=Path,
        help="Operator command install directory (default /usr/local/bin)",
    )
    p.add_argument(
        "--skip-account",
        action="store_true",
        help="Skip user/group mutations (tests / already prepared)",
    )
    p.add_argument(
        "--skip-external",
        action="store_true",
        help="Skip promote/bootstrap/systemctl invocation (unit tests)",
    )
    p.add_argument("--plan-only", action="store_true", help="Print plan and exit 0")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dev_root = args.dev_root.expanduser().resolve()
    if not (dev_root / "deploy" / "scripts" / "promote-to-prod.sh").is_file():
        print(f"ERROR: invalid --dev-root: {dev_root}", file=sys.stderr)
        return 2

    if args.path_prefix:
        layout = HostLayout.under_prefix(
            args.path_prefix.expanduser().resolve(),
            commands_dir=args.commands_target,
        )
    else:
        layout = HostLayout.production(commands_dir=args.commands_target)

    phases = tuple(args.phases) if args.phases else PHASES
    opts = BootstrapOptions(
        dev_root=dev_root,
        operator=args.operator,
        service_user=args.service_user,
        service_group=args.service_group,
        dry_run=bool(args.dry_run or args.plan_only),
        apply=bool(args.apply),
        phases=phases,
        layout=layout,
        commands_target=args.commands_target or layout.commands_dir,
        stop_before=args.stop_before,
        skip_account=bool(args.skip_account or args.path_prefix),
        skip_external=bool(args.skip_external or args.path_prefix),
    )

    if args.plan_only:
        print(plan_summary(opts))
        return 0

    try:
        return run_bootstrap(opts)
    except BootstrapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
