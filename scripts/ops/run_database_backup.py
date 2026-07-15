#!/usr/bin/env python3
"""Database backup entrypoint (Storage Phase 10).

Invoked by scripts/ops/run-database-backup.sh (cron or manual).
Creates a SQLite snapshot; retention removes expired backups.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ops_readonly import (  # noqa: E402
    REPO_ROOT,
    canonical_env,
    ensure_config_scripts_on_path,
    env_label,
    mk04_env,
)

ensure_config_scripts_on_path()
_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_manager import ConfigError, ConfigManager  # noqa: E402
from storage.database_backup import (  # noqa: E402
    EXIT_CONFIG,
    EXIT_FAIL,
    EXIT_SUCCESS,
    run_database_backup,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a SQLite database backup")
    parser.add_argument("environment", help="dev or prod")
    parser.add_argument(
        "--config-root",
        type=Path,
        default=REPO_ROOT / "config",
        help="Config root (default: repo config/)",
    )
    args = parser.parse_args(argv)

    try:
        canonical = canonical_env(args.environment)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    try:
        resolved = ConfigManager.load(
            environment=canonical,
            funnel_id="business",
            platform_id="youtube",
            config_root=args.config_root,
        )
    except ConfigError as exc:
        print(f"Error: config validation failed: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    result = run_database_backup(resolved)
    token = mk04_env(canonical)
    print(f"environment={env_label(canonical)} ({token})")
    print(f"status={result.status}")
    if result.database_path:
        print(f"database_path={result.database_path}")
    if result.backup_path:
        print(f"backup_path={result.backup_path}")
    if result.backup_size_bytes is not None:
        print(f"backup_size_bytes={result.backup_size_bytes}")
    print(f"backup_count={result.backup_count}")
    if result.integrity_ok is not None:
        print(f"integrity_ok={result.integrity_ok}")
    print(f"duration_seconds={result.duration_seconds}")
    if result.retention_database_backups_days is not None:
        print(f"retention_database_backups_days={result.retention_database_backups_days}")
    if result.reason:
        print(f"reason={result.reason}")
    if result.detail:
        print(f"detail={result.detail}")

    if result.status == "FAIL":
        return result.exit_code or EXIT_FAIL
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
