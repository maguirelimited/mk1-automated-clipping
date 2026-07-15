"""Move a validated media file into the clipping service input folder.

Primary destination is ``video_automation_inputs_dir()`` (typically
``<repo>/video-automation/input``) with an immutable ``input_id``-keyed name
(see ``paths.clipping_input_video_path``) for video-automation ``POST /jobs``
via ``input_id``.

Legacy fallback (unchanged layout): if the primary store fails and the download
file still exists, we store under ``data/inputs/ready/<funnel_id>/source.mp4``
as before.

We replace any existing file atomically (write to a sibling ``*.incoming`` and
``os.replace``), so a simultaneous reader never sees a half-written file.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from . import paths
from .log_util import detail


log = logging.getLogger(__name__)


class StorageError(Exception):
    pass


def _atomic_store_ready(downloaded_file: Path, dest: Path) -> Path:
    """Move ``downloaded_file`` to ``dest`` atomically. Returns resolved ``dest``."""
    if not downloaded_file.exists():
        raise StorageError(f"Source file does not exist: {downloaded_file}")

    dest.parent.mkdir(parents=True, exist_ok=True)

    staging = dest.with_suffix(dest.suffix + ".incoming")
    try:
        if staging.exists():
            staging.unlink()
        # Move (rename) when on the same filesystem; otherwise copy + remove.
        try:
            os.replace(downloaded_file, staging)
        except OSError:
            shutil.copy2(downloaded_file, staging)
            try:
                downloaded_file.unlink()
            except OSError:
                pass
        os.replace(staging, dest)
    except OSError as exc:
        # Best-effort cleanup of staging file if it's still around.
        try:
            if staging.exists():
                staging.unlink()
        except OSError:
            pass
        raise StorageError(f"Failed to store ready file at {dest}: {exc}") from exc

    resolved = dest.resolve()
    if not resolved.is_file():
        raise StorageError(f"Ready file missing after store: {resolved}")
    if resolved.stat().st_size <= 0:
        raise StorageError(f"Ready file is empty: {resolved}")

    detail(log, "Stored ready input at %s", resolved)
    return resolved


def store_ready(downloaded_file: Path, funnel_id: str, *, input_id: str | None = None) -> Path:
    """Move ``downloaded_file`` into the immutable clipping input slot.

    Returns the absolute destination path. Raises ``StorageError`` on failure.
    """
    primary = paths.clipping_input_video_path(funnel_id, input_id=input_id)
    try:
        return _atomic_store_ready(downloaded_file, primary)
    except StorageError as exc:
        # Legacy fallback — keep local ready dir behaviour if shared folder fails.
        log.warning(
            "Primary store to video-automation failed (dest=%s): %s; "
            "falling back to local READY_DIR",
            primary,
            exc,
        )
        if not downloaded_file.exists():
            raise StorageError(
                "Primary store failed and the source download file is no longer present: "
                f"{exc}"
            ) from exc
        fallback = paths.ready_video_path(funnel_id, input_id=input_id)
        return _atomic_store_ready(downloaded_file, fallback)


def reject_file(downloaded_file: Path, funnel_id: str, *, reason: str) -> Path | None:
    """Optionally archive a rejected file under ``data/inputs/rejected/<funnel_id>/``.

    Best-effort: if the file can't be moved, we just delete it. Returns the
    archived path if we kept it, otherwise ``None``.
    """
    if not downloaded_file.exists():
        return None

    rejected_dir = paths.funnel_rejected_dir(funnel_id)
    rejected_dir.mkdir(parents=True, exist_ok=True)
    dest = rejected_dir / downloaded_file.name
    try:
        os.replace(downloaded_file, dest)
        # Tiny sidecar so it's obvious why it was rejected.
        sidecar = dest.with_suffix(dest.suffix + ".reason.txt")
        sidecar.write_text(reason, encoding="utf-8")
        return dest
    except OSError as exc:
        log.warning("Could not archive rejected file %s: %s", downloaded_file, exc)
        try:
            downloaded_file.unlink()
        except OSError:
            pass
        return None
