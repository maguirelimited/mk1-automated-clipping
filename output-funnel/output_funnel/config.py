from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = BASE_DIR / "config"


def _load_json(path: str | os.PathLike[str]) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Config must be a JSON object: {path}")
    return payload


def config_dir() -> Path:
    raw = os.environ.get("OUTPUT_FUNNEL_CONFIG_DIR", "").strip()
    return Path(raw).expanduser().resolve() if raw else DEFAULT_CONFIG_DIR


def load_settings(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    env_path = os.environ.get("OUTPUT_FUNNEL_SETTINGS", "").strip()
    if path is not None:
        settings_path = Path(path)
    elif env_path:
        settings_path = Path(env_path)
    else:
        settings_path = config_dir() / "settings.example.json"
    return _load_json(settings_path)


def load_channel_profiles(path: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    env_path = os.environ.get("OUTPUT_FUNNEL_CHANNELS", "").strip()
    if path is not None:
        channels_path = Path(path)
    elif env_path:
        channels_path = Path(env_path)
    else:
        channels_path = config_dir() / "channels.example.json"
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
    raw = str(cfg.get("database_path") or os.environ.get("OUTPUT_FUNNEL_DB") or "data/output_funnel.sqlite3")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path.resolve())
