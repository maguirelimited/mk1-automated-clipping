#!/usr/bin/env python3
"""Scheduled retention entrypoint (Storage Phase 8).

Invoked by scripts/ops/run-scheduled-retention.sh (cron or manual).
Loads config and delegates to storage.retention_schedule.run_scheduled_retention,
which reuses the existing planner and apply executor.
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
from storage.retention_schedule import (  # noqa: E402
    EXIT_CONFIG,
    EXIT_FAIL,
    EXIT_SUCCESS,
    run_scheduled_retention,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run scheduled retention for an environment",
    )
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

    result = run_scheduled_retention(resolved)
    token = mk04_env(canonical)
    print(f"environment={env_label(canonical)} ({token})")
    print(f"status={result.status}")
    print(f"mode={result.mode}")
    print(f"duration_seconds={result.duration_seconds}")
    if result.report_path:
        print(f"report_path={result.report_path}")
    if result.reason:
        print(f"reason={result.reason}")
    if result.detail and result.detail != result.reason:
        print(f"detail={result.detail}")

    if result.status == "FAIL":
        return result.exit_code or EXIT_FAIL
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
