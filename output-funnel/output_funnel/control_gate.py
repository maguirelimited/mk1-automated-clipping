from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def controls_file_path() -> Path:
    raw = os.environ.get("MK04_CONTROLS_FILE", "").strip()
    if raw:
        path = Path(raw).expanduser()
        if os.environ.get("MK04_ENV", "dev").strip().lower() == "prod":
            resolved = path.resolve()
            root = Path("/var/lib/mk04/prod").resolve()
            if resolved != root and root not in resolved.parents:
                raise RuntimeError(f"MK04_CONTROLS_FILE={resolved} must be under {root} when MK04_ENV=prod")
        return path
    if os.environ.get("MK04_ENV", "dev").strip().lower() == "prod":
        raise RuntimeError("MK04_CONTROLS_FILE is required when MK04_ENV=prod")
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "ops-ui" / "data" / "controls.json"


def read_controls() -> dict[str, Any]:
    path = controls_file_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def uploads_paused() -> bool:
    return bool(read_controls().get("uploads_paused"))
