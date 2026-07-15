#!/usr/bin/env python3
"""Write data/<env>/last_update_status.json using EnvironmentStatePaths."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_CONFIG = REPO_ROOT / "scripts" / "config"
SCRIPTS_OPS = REPO_ROOT / "scripts" / "ops"
if str(SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_CONFIG))
if str(SCRIPTS_OPS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_OPS))

from config_manager import ConfigManager  # noqa: E402
from ops_readonly import canonical_env  # noqa: E402


def _iso_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write last_update_status.json")
    parser.add_argument("--env", required=True, help="dev|prod|development|production")
    parser.add_argument("--status", required=True, choices=["success", "failure"])
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--commit", default="")
    parser.add_argument("--config-validation", default="unknown")
    parser.add_argument("--tests", default="unknown")
    parser.add_argument("--services-restarted", default="skipped")
    parser.add_argument("--health-check", default="not_available")
    parser.add_argument("--message", default="")
    args = parser.parse_args(argv)

    canonical = canonical_env(args.env)
    resolved = ConfigManager.load(
        environment=canonical,
        config_root=REPO_ROOT / "config",
    )
    data_root = resolved.state_paths.data_root
    data_root.mkdir(parents=True, exist_ok=True)
    path = data_root / "last_update_status.json"

    payload: dict[str, object] = {
        "environment": canonical,
        "environment_label": "DEVELOPMENT" if canonical == "development" else "PRODUCTION",
        "status": args.status,
        "started_at": args.started_at,
        "finished_at": _iso_now(),
        "commit": args.commit,
        "config_validation": args.config_validation,
        "tests": args.tests,
        "services_restarted": args.services_restarted,
        "health_check": args.health_check,
    }
    if args.message:
        payload["message"] = args.message[:500]

    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
