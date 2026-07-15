"""Source-input operator pause gate (ingestion only).

Canonical module: ``input_service.control_gate``. Do not add a sibling
``control_gate.py`` beside ``app.py`` — imports resolve through this package.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from shared.controls_file import read_controls_json, resolve_controls_path  # noqa: E402


def controls_file_path() -> Path:
    return resolve_controls_path()


def read_controls() -> dict[str, Any]:
    return read_controls_json()


def ingestion_paused() -> bool:
    return bool(read_controls().get("ingestion_paused"))
