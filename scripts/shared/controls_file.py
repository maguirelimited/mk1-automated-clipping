"""Raw controls.json path resolution and JSON loading.

This module answers only:
  - Where is controls.json?
  - Can I load it as a dictionary?

It deliberately does NOT interpret pause flags, ai_config blocks, or
precedence between UI values and environment variables.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONTROLS_REL = Path("ops-ui") / "data" / "controls.json"


def ensure_scripts_on_sys_path() -> Path:
    """Add ``<repo>/scripts`` to ``sys.path`` so ``shared.*`` imports work."""
    scripts_dir = _REPO_ROOT / "scripts"
    scripts_str = str(scripts_dir)
    if scripts_str not in sys.path:
        sys.path.insert(0, scripts_str)
    return scripts_dir


def resolve_controls_path(*, repo_root: Path | None = None) -> Path:
    """Return the controls file path (``MK04_CONTROLS_FILE`` or repo default)."""
    raw = os.environ.get("MK04_CONTROLS_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    root = _REPO_ROOT if repo_root is None else repo_root
    return root / _DEFAULT_CONTROLS_REL


def read_controls_json_at(path: Path) -> dict[str, Any]:
    """Load controls JSON from an explicit path; return ``{}`` on any problem."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_controls_json(*, path: Path | None = None, repo_root: Path | None = None) -> dict[str, Any]:
    """Resolve the controls path (unless ``path`` is given) and load JSON safely."""
    return read_controls_json_at(path or resolve_controls_path(repo_root=repo_root))
