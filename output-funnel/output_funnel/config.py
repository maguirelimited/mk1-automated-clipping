from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = BASE_DIR / "config"
VALID_UPLOAD_MODES = {"dry_run", "real"}
PROD_CONFIG_ROOT = Path("/etc/mk04/prod")
PROD_RUNTIME_ROOT = Path("/var/lib/mk04/prod")


def _load_json(path: str | os.PathLike[str]) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Config must be a JSON object: {path}")
    return payload


def config_dir() -> Path:
    raw = os.environ.get("OUTPUT_FUNNEL_CONFIG_DIR", "").strip()
    if runtime_environment() == "prod" and not raw:
        raise RuntimeError("OUTPUT_FUNNEL_CONFIG_DIR is required when MK04_ENV=prod")
    return Path(raw).expanduser().resolve() if raw else DEFAULT_CONFIG_DIR


def load_settings(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    env_path = os.environ.get("OUTPUT_FUNNEL_SETTINGS", "").strip()
    if path is not None:
        settings_path = Path(path)
    elif env_path:
        settings_path = Path(env_path)
    elif runtime_environment() == "prod":
        raise RuntimeError("OUTPUT_FUNNEL_SETTINGS is required when MK04_ENV=prod")
    else:
        settings_path = config_dir() / "settings.example.json"
    _assert_prod_path("OUTPUT_FUNNEL_SETTINGS", settings_path, PROD_CONFIG_ROOT)
    return _load_json(settings_path)


def load_channel_profiles(path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    env_path = os.environ.get("OUTPUT_FUNNEL_CHANNELS", "").strip()
    if path is not None:
        channels_path = Path(path)
    elif env_path:
        channels_path = Path(env_path)
    elif runtime_environment() == "prod":
        raise RuntimeError("OUTPUT_FUNNEL_CHANNELS is required when MK04_ENV=prod")
    else:
        channels_path = config_dir() / "channels.example.json"
    _assert_prod_path("OUTPUT_FUNNEL_CHANNELS", channels_path, PROD_CONFIG_ROOT)
    payload = _load_json(channels_path)
    profiles = payload.get("channels")
    if not isinstance(profiles, list):
        raise ValueError("Channel config requires a `channels` list")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(profiles):
        if not isinstance(item, dict):
            raise ValueError(f"Channel profile at index {index} must be an object")
        out.append(dict(item))
    return out


def database_path(settings: dict[str, Any] | None = None) -> str:
    cfg = settings if isinstance(settings, dict) else load_settings()
    raw = str(os.environ.get("OUTPUT_FUNNEL_DB") or cfg.get("database_path") or "")
    if runtime_environment() == "prod" and not raw:
        raise RuntimeError("OUTPUT_FUNNEL_DB or settings.database_path is required when MK04_ENV=prod")
    if not raw:
        raw = "data/output_funnel.sqlite3"
    path = Path(raw).expanduser()
    if not path.is_absolute():
        if runtime_environment() == "prod":
            raise RuntimeError(f"OUTPUT_FUNNEL_DB must be absolute when MK04_ENV=prod: {raw}")
        path = BASE_DIR / path
    _assert_prod_path("OUTPUT_FUNNEL_DB", path, PROD_RUNTIME_ROOT)
    return str(path.resolve())


def runtime_environment() -> str:
    return os.environ.get("MK04_ENV", "dev").strip().lower() or "dev"


def upload_mode() -> str:
    raw = os.environ.get("MK04_UPLOAD_MODE", "dry_run").strip().lower() or "dry_run"
    if raw not in VALID_UPLOAD_MODES:
        raise ValueError(f"Invalid MK04_UPLOAD_MODE={raw!r}; expected dry_run or real")
    if raw == "real" and runtime_environment() != "prod":
        raise RuntimeError("MK04_UPLOAD_MODE=real is only allowed when MK04_ENV=prod")
    return raw


def _assert_prod_path(name: str, path: str | os.PathLike[str], root: Path) -> None:
    if runtime_environment() != "prod":
        return
    resolved = Path(path).expanduser().resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise RuntimeError(f"{name}={resolved} must be under {root_resolved} when MK04_ENV=prod")
