#!/usr/bin/env python3
"""Runtime upload kill switch for scripts/ops/disable-uploads.sh and enable-uploads.sh."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops_readonly import (  # noqa: E402
    canonical_env,
    compute_effective_upload,
    ensure_config_scripts_on_path,
    env_label,
    load_runtime_upload_control,
    mk04_env,
    REPO_ROOT,
)

REASON_DISABLE = "manual_remote_disable"
REASON_ENABLE = "manual_remote_enable"


def control_state_path(data_root: Path) -> Path:
    return data_root / "control_state.json"


def read_control_state(data_root: Path) -> dict[str, Any]:
    path = control_state_path(data_root)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_control_state_atomic(data_root: Path, updates: dict[str, Any]) -> Path:
    data_root.mkdir(parents=True, exist_ok=True)
    path = control_state_path(data_root)
    existing = read_control_state(data_root)
    merged = {**existing, **updates}
    fd, tmp_name = tempfile.mkstemp(prefix=".control_state.", suffix=".tmp", dir=data_root)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return path


def _yes_no(value: bool | None) -> str:
    if value is True:
        return "YES"
    if value is False:
        return "NO"
    return "UNKNOWN"


def _load_config_upload_enabled(canonical: str) -> tuple[bool, str]:
    ensure_config_scripts_on_path()
    try:
        from config_manager import ConfigManager  # noqa: PLC0415
        from state_paths import EnvironmentStatePaths  # noqa: PLC0415
    except ImportError as exc:
        return False, f"config unavailable ({exc.__class__.__name__})"

    try:
        resolved = ConfigManager.load(environment=canonical, config_root=REPO_ROOT / "config")
    except Exception as exc:
        return False, f"config load failed ({exc.__class__.__name__})"

    _ = EnvironmentStatePaths.from_resolved_config(resolved)
    return bool(resolved.uploading_enabled), ""


def resolve_data_root(canonical: str) -> Path:
    ensure_config_scripts_on_path()
    from config_manager import ConfigManager  # noqa: PLC0415
    from state_paths import EnvironmentStatePaths  # noqa: PLC0415

    resolved = ConfigManager.load(environment=canonical, config_root=REPO_ROOT / "config")
    return EnvironmentStatePaths.from_resolved_config(resolved).data_root


def render_status(
    *,
    canonical: str,
    config_upload_enabled: bool,
    runtime_disabled: bool | None,
    action: str,
) -> str:
    can_upload, _ = compute_effective_upload(config_upload_enabled, runtime_disabled)
    lines = [
        "Upload control updated" if action else "Upload control status",
        "",
        f"Environment: {env_label(canonical)}",
        f"Runtime uploads disabled: {_yes_no(runtime_disabled)}",
        f"Config upload enabled: {_yes_no(config_upload_enabled)}",
        f"Effective real posting: {_yes_no(can_upload)}",
        "",
    ]
    if action == "disable":
        lines.extend(
            [
                "Scheduler unchanged.",
                "Processing unchanged.",
                "No jobs or clips were deleted.",
            ]
        )
    elif action == "enable":
        lines.extend(
            [
                "No upload was triggered.",
                "Scheduler unchanged.",
            ]
        )
    return "\n".join(lines)


def set_runtime_uploads_disabled(
    mk04_env_token: str,
    *,
    disabled: bool,
    reason: str,
    require_prod_confirm: bool = False,
    confirmed: bool = False,
) -> int:
    canonical = canonical_env(mk04_env_token)
    is_production = canonical == "production"

    if require_prod_confirm and is_production and not confirmed:
        print(
            "Refusing to enable production uploads without --confirm.\n"
            "Example: ./scripts/ops/enable-uploads.sh prod --confirm",
            file=sys.stderr,
        )
        return 1

    try:
        data_root = resolve_data_root(canonical)
    except Exception as exc:
        print(f"Error: could not resolve environment data root ({exc})", file=sys.stderr)
        return 1

    expected_env = mk04_env(canonical)
    if data_root.parts[-1] != expected_env and expected_env not in str(data_root):
        print(
            f"Error: refusing to write control state outside {expected_env} data root: {data_root}",
            file=sys.stderr,
        )
        return 1

    updates = {
        "environment": expected_env,
        "uploads_disabled": disabled,
        "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "updated_by": getpass.getuser(),
        "reason": reason,
    }
    write_control_state_atomic(data_root, updates)

    config_enabled, _ = _load_config_upload_enabled(canonical)
    runtime_disabled, _ = load_runtime_upload_control(data_root)
    action = "disable" if disabled else "enable"
    print(render_status(
        canonical=canonical,
        config_upload_enabled=config_enabled,
        runtime_disabled=runtime_disabled,
        action=action,
    ))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Runtime upload kill switch control")
    sub = parser.add_subparsers(dest="command", required=True)

    disable_parser = sub.add_parser("disable", help="Disable runtime uploads")
    disable_parser.add_argument("environment", help="dev or prod")

    enable_parser = sub.add_parser("enable", help="Clear runtime upload disable")
    enable_parser.add_argument("environment", help="dev or prod")
    enable_parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required to enable uploads in production",
    )

    args = parser.parse_args(argv)
    if args.command == "disable":
        return set_runtime_uploads_disabled(
            args.environment,
            disabled=True,
            reason=REASON_DISABLE,
        )
    return set_runtime_uploads_disabled(
        args.environment,
        disabled=False,
        reason=REASON_ENABLE,
        require_prod_confirm=True,
        confirmed=bool(args.confirm),
    )


if __name__ == "__main__":
    raise SystemExit(main())
