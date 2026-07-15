"""Import the repo-canonical environment_names helper for output-funnel."""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_scripts_config_on_path() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    scripts_config = repo_root / "scripts" / "config"
    text = str(scripts_config)
    if text not in sys.path:
        sys.path.insert(0, text)


_ensure_scripts_config_on_path()

from environment_names import (  # noqa: E402
    EnvironmentNameError,
    is_production_env,
    normalize_runtime_env,
    resolve_mk04_env,
)

__all__ = [
    "EnvironmentNameError",
    "is_production_env",
    "normalize_runtime_env",
    "resolve_mk04_env",
]
