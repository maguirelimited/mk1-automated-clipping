"""Centralised filesystem paths for the input service.

All other modules import paths from here so the layout stays consistent and
easy to override in tests / deployments via the ``INPUT_SERVICE_ROOT`` env var.
"""

from __future__ import annotations

import os
from pathlib import Path


def _project_root() -> Path:
    override = os.environ.get("INPUT_SERVICE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    # paths.py lives at: <root>/input_service/paths.py
    return Path(__file__).resolve().parent.parent


ROOT: Path = _project_root()

CONFIG_DIR: Path = ROOT / "config"
FUNNELS_FILE: Path = CONFIG_DIR / "funnels.json"

DATA_DIR: Path = ROOT / "data"
INPUTS_DIR: Path = DATA_DIR / "inputs"
READY_DIR: Path = INPUTS_DIR / "ready"
REJECTED_DIR: Path = INPUTS_DIR / "rejected"

STATE_DIR: Path = DATA_DIR / "state"
SEEN_FILE: Path = STATE_DIR / "seen_urls.json"
RUN_LOCK_FILE: Path = STATE_DIR / "run.lock"

TMP_DIR: Path = DATA_DIR / "tmp"


def ensure_dirs() -> None:
    """Create all expected directories if missing. Safe to call repeatedly."""
    for path in (CONFIG_DIR, READY_DIR, REJECTED_DIR, STATE_DIR, TMP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def ready_video_path(funnel_id: str) -> Path:
    """Predictable ready-input location for a funnel: ``ready/<funnel_id>/source.mp4``."""
    return READY_DIR / funnel_id / "source.mp4"


def funnel_tmp_dir(funnel_id: str) -> Path:
    """Per-funnel temp dir for in-flight downloads."""
    return TMP_DIR / funnel_id


def funnel_rejected_dir(funnel_id: str) -> Path:
    """Per-funnel rejected dir for failed-validation media."""
    return REJECTED_DIR / funnel_id
