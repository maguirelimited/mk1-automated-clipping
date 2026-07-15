#!/usr/bin/env python3
"""Dry-run-only cleanup control for scripts/ops/cleanup.sh.

Deletion is deferred until Storage & Data Management retention planning exists.
This module must not delete files.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from ops_readonly import (  # noqa: E402
    REPO_ROOT,
    canonical_env,
    ensure_config_scripts_on_path,
    env_label,
    mk04_env,
)


def retention_planner_available() -> bool:
    """Retention dry-run planner is available (Phase 4). Apply mode is not."""
    return True


def resolve_known_dirs(canonical: str) -> dict[str, Path]:
    ensure_config_scripts_on_path()
    from config_manager import ConfigManager  # noqa: PLC0415
    from state_paths import EnvironmentStatePaths  # noqa: PLC0415

    resolved = ConfigManager.load(environment=canonical, config_root=REPO_ROOT / "config")
    state = EnvironmentStatePaths.from_resolved_config(resolved)
    token = mk04_env(canonical)
    return {
        "data_root": state.data_root,
        "jobs_root": state.jobs_root,
        "logs_root": state.logs_root,
        "reports_root": state.reports_root,
        "caches_root": state.caches_root,
        "outputs_root": state.outputs_root,
        "backup_root": REPO_ROOT / "backups" / token,
    }


def _dir_summary(path: Path) -> str:
    if not path.exists():
        return "missing"
    if not path.is_dir():
        return "not a directory"
    try:
        entries = list(path.iterdir())
    except OSError as exc:
        return f"unreadable ({exc.__class__.__name__})"
    files = sum(1 for p in entries if p.is_file())
    dirs = sum(1 for p in entries if p.is_dir())
    return f"present ({files} files, {dirs} dirs at top level)"


def render_dry_run(mk04_env_token: str) -> str:
    canonical = canonical_env(mk04_env_token)
    dirs = resolve_known_dirs(canonical)
    disk_root = dirs["data_root"] if dirs["data_root"].exists() else REPO_ROOT
    try:
        usage = shutil.disk_usage(disk_root)
        disk_line = (
            f"Disk usage ({disk_root}): "
            f"{usage.used // (1024 * 1024)} MiB used / "
            f"{usage.total // (1024 * 1024)} MiB total "
            f"({int(usage.used * 100 / usage.total)}%)"
        )
    except OSError:
        disk_line = "Disk usage: unavailable"

    known_lines = [
        f"  data:    {_dir_summary(dirs['data_root'])} — {dirs['data_root']}",
        f"  jobs:    {_dir_summary(dirs['jobs_root'])} — {dirs['jobs_root']}",
        f"  logs:    {_dir_summary(dirs['logs_root'])} — {dirs['logs_root']}",
        f"  reports: {_dir_summary(dirs['reports_root'])} — {dirs['reports_root']}",
        f"  cache:   {_dir_summary(dirs['caches_root'])} — {dirs['caches_root']}",
        f"  backups: {_dir_summary(dirs['backup_root'])} — {dirs['backup_root']}",
        f"  outputs: present (media/clips not scanned) — {dirs['outputs_root']}",
    ]

    return "\n".join(
        [
            "Cleanup dry-run",
            "",
            f"Environment: {env_label(canonical)}",
            "",
            "Retention engine: not yet available",
            "Deletion mode: disabled",
            "Would delete: 0 files",
            "No files deleted.",
            "",
            disk_line,
            "Known directories:",
            *known_lines,
            "",
            "Next:",
            "Implement Storage & Data Management retention planner before cleanup apply.",
        ]
    )


def render_apply_refused(mk04_env_token: str) -> str:
    canonical = canonical_env(mk04_env_token)
    return "\n".join(
        [
            "Cleanup apply is not implemented yet.",
            "",
            f"Environment: {env_label(canonical)}",
            "",
            "Reason:",
            "Safe deletion requires Storage & Data Management retention planning.",
            "",
            "No files deleted.",
        ]
    )


def run_cleanup(mk04_env_token: str, *, mode: str) -> int:
    if mode == "apply":
        print(render_apply_refused(mk04_env_token), file=sys.stderr)
        return 1
    if mode != "dry-run":
        print("Error: cleanup requires --dry-run or --apply.", file=sys.stderr)
        return 1
    print(render_dry_run(mk04_env_token))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Environment-scoped cleanup (dry-run only for now)")
    parser.add_argument("environment", help="dev or prod")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Show cleanup status without deleting")
    group.add_argument("--apply", action="store_true", help="Apply cleanup (refused until retention planner exists)")
    args = parser.parse_args(argv)
    mode = "apply" if args.apply else "dry-run"
    try:
        return run_cleanup(args.environment, mode=mode)
    except Exception as exc:
        print(f"Error: cleanup failed ({exc})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
