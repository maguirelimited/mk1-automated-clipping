"""Move a validated media file into the funnel's ready input location.

The output path is fixed and predictable so n8n / the clipping service can
always find it: ``data/inputs/ready/<funnel_id>/source.mp4``.

We replace any existing ready file atomically (write to a sibling temp
filename and ``os.replace``), so a simultaneous reader never sees a
half-written file.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from . import paths


log = logging.getLogger(__name__)


class StorageError(Exception):
    pass


def store_ready(downloaded_file: Path, funnel_id: str) -> Path:
    """Move ``downloaded_file`` into the ready slot for ``funnel_id``.

    Returns the destination path. Raises ``StorageError`` on failure.
    """
    if not downloaded_file.exists():
        raise StorageError(f"Source file does not exist: {downloaded_file}")

    dest = paths.ready_video_path(funnel_id)
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

    log.info("Stored ready input for %s at %s", funnel_id, dest)
    return dest


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
