#!/usr/bin/env python3
"""Log rotation entrypoint (Storage Phase 9).

Invoked by scripts/ops/run-log-rotation.sh (cron or manual).
Rotates active project logs; retention removes expired rotated logs.
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
from storage.log_rotation import (  # noqa: E402
    EXIT_CONFIG,
    EXIT_FAIL,
    EXIT_SUCCESS,
    run_log_rotation,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rotate active project logs")
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

    result = run_log_rotation(resolved)
    token = mk04_env(canonical)
    print(f"environment={env_label(canonical)} ({token})")
    print(f"status={result.status}")
    print(f"active_log_count={result.active_log_count}")
    print(f"rotated_count={result.rotated_count}")
    print(f"compressed_count={result.compressed_count}")
    print(f"failure_count={result.failure_count}")
    print(f"duration_seconds={result.duration_seconds}")
    if result.retention_logs_days is not None:
        print(f"retention_logs_days={result.retention_logs_days}")
    if result.reason:
        print(f"reason={result.reason}")
    if result.detail:
        print(f"detail={result.detail}")

    if result.status == "FAIL":
        return result.exit_code or EXIT_FAIL
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
