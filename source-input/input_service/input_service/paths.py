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

CONFIG_DIR: Path = Path(os.environ.get("INPUT_SERVICE_CONFIG_DIR", ROOT / "config")).expanduser().resolve()
FUNNELS_FILE: Path = CONFIG_DIR / "funnels.json"

DATA_DIR: Path = Path(os.environ.get("INPUT_SERVICE_DATA_DIR", ROOT / "data")).expanduser().resolve()
INPUTS_DIR: Path = DATA_DIR / "inputs"
READY_DIR: Path = INPUTS_DIR / "ready"
REJECTED_DIR: Path = INPUTS_DIR / "rejected"

STATE_DIR: Path = DATA_DIR / "state"
SEEN_FILE: Path = STATE_DIR / "seen_urls.json"
RUN_LOCK_FILE: Path = STATE_DIR / "run.lock"

TMP_DIR: Path = DATA_DIR / "tmp"


def _video_automation_project_parent() -> Path:
    """Directory that contains the ``video-automation`` folder (e.g. VAmk0.4).

    Walks upward from ``ROOT`` until ``video-automation/config/pipeline_config.json``
    exists, so this still works when ``INPUT_SERVICE_ROOT`` is set to an absolute
    path (``ROOT.parent.parent`` alone is not reliable).

    Override with ``VIDEO_AUTOMATION_PROJECT_ROOT`` (absolute path to parent of
    ``video-automation``).
    """
    env_root = os.environ.get("VIDEO_AUTOMATION_PROJECT_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()

    p = ROOT.resolve()
    for _ in range(10):
        va_dir = p / "video-automation"
        cfg = va_dir / "config" / "pipeline_config.json"
        if va_dir.is_dir() and cfg.is_file():
            return p
        if p.parent == p:
            break
        p = p.parent

    # Legacy layout: .../source-input/input_service → monorepo is two levels up.
    return ROOT.parent.parent.resolve()


def video_automation_inputs_dir() -> Path:
    """Directory where the clipping service (video-automation) reads raw inputs.

    Default: ``<repo>/video-automation/input`` (same as ``paths.input_folder`` in
    ``video-automation/config/pipeline_config.json``).

    Override with ``VIDEO_AUTOMATION_INPUT_DIR`` (absolute path to the input dir).
    """
    override = os.environ.get("VIDEO_AUTOMATION_INPUT_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    parent = _video_automation_project_parent()
    return (parent / "video-automation" / "input").resolve()


def clipping_input_video_path(funnel_id: str, *, input_id: str | None = None) -> Path:
    """Immutable file path the clipping service can load through the input ledger.

    New handoffs are keyed by ``input_id`` so a later run for the same funnel
    cannot overwrite media that an already queued clipping job still references.
    """
    safe_id = str(funnel_id or "").strip()
    if not safe_id:
        raise ValueError("funnel_id required for clipping_input_video_path")
    safe_input_id = str(input_id or "").strip()
    if safe_input_id:
        return video_automation_inputs_dir() / f"{safe_input_id}_{safe_id}_source.mp4"
    return video_automation_inputs_dir() / f"{safe_id}_source.mp4"


def ensure_dirs() -> None:
    """Create all expected directories if missing. Safe to call repeatedly."""
    for path in (CONFIG_DIR, READY_DIR, REJECTED_DIR, STATE_DIR, TMP_DIR):
        path.mkdir(parents=True, exist_ok=True)


def ready_video_path(funnel_id: str, *, input_id: str | None = None) -> Path:
    """Legacy local ready-input location: ``data/inputs/ready/<funnel_id>/source.mp4``.

    Used as a fallback when copying to ``video_automation_inputs_dir()`` fails.
    """
    safe_input_id = str(input_id or "").strip()
    if safe_input_id:
        return READY_DIR / funnel_id / f"{safe_input_id}.mp4"
    return READY_DIR / funnel_id / "source.mp4"


def funnel_tmp_dir(funnel_id: str) -> Path:
    """Per-funnel temp dir for in-flight downloads."""
    return TMP_DIR / funnel_id


def funnel_rejected_dir(funnel_id: str) -> Path:
    """Per-funnel rejected dir for failed-validation media."""
    return REJECTED_DIR / funnel_id
