"""Read-only runtime upload kill switch from data/<env>/control_state.json.

Configuration upload authority and the combined real-upload decision live in
upload_authority.py. This module keeps the control_state.json reader and
re-exports the public gate helpers used by publisher/app.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .env_names import resolve_mk04_env


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def control_state_path() -> Path:
    override = os.environ.get("MK04_CONTROL_STATE_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    data_env = resolve_mk04_env(environ_value=os.environ.get("MK04_ENV"), default="dev")
    data_root = os.environ.get("MK04_DATA_ROOT", "").strip()
    if data_root:
        return Path(data_root) / "control_state.json"
    # Prefer shared path authority when scripts/config is importable.
    try:
        scripts_config = _repo_root() / "scripts" / "config"
        if str(scripts_config) not in sys.path:
            sys.path.insert(0, str(scripts_config))
        from runtime_paths import control_state_path_for_env  # noqa: PLC0415

        return control_state_path_for_env(data_env, repo_root=_repo_root())
    except Exception:
        return _repo_root() / "data" / data_env / "control_state.json"


def _read_control_state() -> dict:
    path = control_state_path()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def runtime_uploads_disabled() -> bool:
    payload = _read_control_state()
    if "uploads_disabled" in payload:
        return bool(payload.get("uploads_disabled"))
    if "uploads_enabled" in payload:
        return not bool(payload.get("uploads_enabled"))
    return False


def config_upload_enabled() -> bool:
    from .upload_authority import config_upload_enabled as _config_upload_enabled

    return _config_upload_enabled()


def upload_block_reason() -> str | None:
    """
    Deny-only gate for real uploads.

    Includes YAML config, runtime uploads_disabled, and Ops UI uploads_paused.
    Returns None only when a real platform API call is permitted.
    """
    from .upload_authority import evaluate_real_upload_decision

    decision = evaluate_real_upload_decision()
    if decision.allow_real_api:
        return None
    # When mode is dry_run, callers typically branch before this; still report
    # the structured reason for status surfaces.
    return decision.block_reason
