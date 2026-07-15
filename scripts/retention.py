#!/usr/bin/env python3
"""Retention planner and safe apply CLI (Storage & Data Management).

Usage:
    python scripts/retention.py --dry-run dev
    python scripts/retention.py --dry-run prod
    python scripts/retention.py --apply dev --confirm
    python scripts/retention.py --apply prod --confirm-production
    python scripts/retention.py --apply dev --confirm --plan-report reports/dev/retention/retention_*.json

Dry-run evaluates policy and writes a plan report. Apply executes an approved
plan with per-file safety checks. Production apply requires --confirm-production.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPTS_CONFIG = SCRIPTS_DIR / "config"
SCRIPTS_OPS = SCRIPTS_DIR / "ops"

for path in (SCRIPTS_CONFIG, SCRIPTS_OPS, SCRIPTS_DIR):
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)

from config_manager import ConfigError, ConfigManager  # noqa: E402
from ops_readonly import canonical_env, env_label, mk04_env  # noqa: E402
from storage.retention_apply import run_retention_apply  # noqa: E402
from storage.retention_planner import RetentionPlanner, run_retention_dry_run  # noqa: E402
from storage.retention_report import (  # noqa: E402
    format_apply_terminal_summary,
    format_terminal_summary,
    load_plan_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Storage retention planner and safe apply",
    )
    parser.add_argument(
        "environment",
        nargs="?",
        help="dev or prod (optional; defaults to MK04_ENV)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview retention decisions without deleting files",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Execute a retention plan with per-file safety checks",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required confirmation flag for development apply",
    )
    parser.add_argument(
        "--confirm-production",
        action="store_true",
        help="Required confirmation flag for production apply",
    )
    parser.add_argument(
        "--plan-report",
        type=Path,
        default=None,
        help="Existing dry-run plan JSON to apply (optional; otherwise plan is built at apply time)",
    )
    parser.add_argument(
        "--config-root",
        type=Path,
        default=REPO_ROOT / "config",
        help="Config root (default: repo config/)",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Override retention report directory",
    )
    args = parser.parse_args(argv)

    env_token = args.environment
    if not env_token:
        import os

        env_token = os.environ.get("MK04_ENV", "dev")
    canonical = canonical_env(env_token)
    is_production = canonical == "production"

    try:
        resolved = ConfigManager.load(
            environment=canonical,
            funnel_id="business",
            platform_id="youtube",
            config_root=args.config_root,
        )
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        try:
            report, report_path = run_retention_dry_run(
                resolved,
                report_dir=args.report_dir,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Error: retention dry-run failed ({exc})", file=sys.stderr)
            return 1
        print(format_terminal_summary(report))
        print("")
        print(f"Report written: {report_path}")
        print(f"Environment: {env_label(canonical)} ({mk04_env(canonical)})")
        return 0

    # Apply mode
    if is_production:
        if not args.confirm_production:
            print(
                "Error: production apply requires --confirm-production.\n"
                "No files deleted.",
                file=sys.stderr,
            )
            return 1
    elif not args.confirm:
        print(
            "Error: development apply requires --confirm.\n"
            "No files deleted.",
            file=sys.stderr,
        )
        return 1

    if not bool(resolved.get("storage.retention.enabled")):
        print(
            "Error: storage.retention.enabled is false — apply refused.\n"
            "Run dry-run first and enable retention in config before apply.\n"
            "No files deleted.",
            file=sys.stderr,
        )
        return 1

    try:
        if args.plan_report is not None:
            plan = load_plan_report(args.plan_report.resolve())
            if plan.environment != resolved.environment:
                print(
                    f"Error: plan environment {plan.environment!r} does not match "
                    f"{resolved.environment!r}.\nNo files deleted.",
                    file=sys.stderr,
                )
                return 1
        else:
            plan = RetentionPlanner(resolved).plan_dry_run()

        apply_report, apply_path = run_retention_apply(
            resolved,
            plan,
            report_dir=args.report_dir,
        )
    except ValueError as exc:
        print(f"Error: {exc}\nNo files deleted.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Error: retention apply failed ({exc})\nNo files deleted.", file=sys.stderr)
        return 1

    print(format_apply_terminal_summary(apply_report))
    print("")
    print(f"Apply report written: {apply_path}")
    print(f"Environment: {env_label(canonical)} ({mk04_env(canonical)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
