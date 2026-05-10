"""Orchestrate one funnel run end-to-end.

Implements the flow described in ``source-input-context.txt``:

    run_funnel(funnel_id):
        load funnel
        check approved sources
        build candidate list
        sort candidates newest first
        for each candidate:
            skip if duplicate
            skip if invalid duration/type
            try download
            validate file
            if valid:
                store as ready input
                mark as seen
                return input_ready
            if invalid:
                reject and try next candidate
        return no_input_available
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import paths
from .candidate_filter import filter_candidates
from .downloader import DownloadFailed, download_candidate
from .duplicate_store import DuplicateStore
from .funnel_loader import (
    Funnel,
    FunnelError,
    FunnelInactiveError,
    FunnelInvalidError,
    FunnelNotFoundError,
    load_funnel,
)
from .media_validator import ValidationError, validate_media
from .source_checker import SourceCheckError, check_sources
from .storage import StorageError, reject_file, store_ready


log = logging.getLogger(__name__)


def _success(funnel_id: str, video_path: Path, source_url: str, title: str) -> dict[str, Any]:
    return {
        "success": True,
        "status": "input_ready",
        "funnel_id": funnel_id,
        "video_path": str(video_path),
        "source_url": source_url,
        "title": title,
    }


def _no_input(funnel_id: str, reason: str) -> dict[str, Any]:
    return {
        "success": True,
        "status": "no_input_available",
        "funnel_id": funnel_id,
        "reason": reason,
    }


def _failed(funnel_id: str | None, error: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "status": "failed",
        "error": error,
    }
    if funnel_id:
        payload["funnel_id"] = funnel_id
    return payload


def run_funnel(funnel_id: str) -> dict[str, Any]:
    """Run one funnel and return a JSON-serialisable response dict.

    This function never raises; all problems are mapped to a structured
    response (``input_ready`` / ``no_input_available`` / ``failed``).
    """
    paths.ensure_dirs()

    # ---- load funnel ------------------------------------------------------
    try:
        funnel: Funnel = load_funnel(funnel_id)
    except FunnelNotFoundError as exc:
        return _failed(funnel_id, f"unknown_funnel: {exc}")
    except FunnelInactiveError as exc:
        return _failed(funnel_id, f"inactive_funnel: {exc}")
    except FunnelInvalidError as exc:
        return _failed(funnel_id, f"invalid_funnel: {exc}")
    except FunnelError as exc:  # safety net
        return _failed(funnel_id, f"funnel_error: {exc}")

    # ---- check sources ----------------------------------------------------
    try:
        candidates = check_sources(funnel.sources)
    except SourceCheckError as exc:
        return _failed(funnel.funnel_id, f"source_check_failed: {exc}")
    except Exception as exc:  # pragma: no cover - last-resort guard
        log.exception("Unexpected source check error")
        return _failed(funnel.funnel_id, f"source_check_failed: {exc}")

    if not candidates:
        return _no_input(funnel.funnel_id, "No candidate videos found in approved sources.")

    # ---- filter + sort ----------------------------------------------------
    seen = DuplicateStore()
    valid, rejected = filter_candidates(candidates, funnel, seen)

    if not valid:
        if rejected and all(r.reason == "duplicate" for r in rejected):
            reason = "All candidate videos have already been used."
        elif rejected:
            reason = "No valid non-duplicate video found."
        else:
            reason = "No candidate videos passed the filter."
        return _no_input(funnel.funnel_id, reason)

    # ---- download / validate / store loop --------------------------------
    last_error: str | None = None

    for cand in valid:
        try:
            dl = download_candidate(cand, funnel_id=funnel.funnel_id)
        except DownloadFailed as exc:
            log.warning("Download failed for %s: %s", cand.url, exc)
            last_error = f"download_failed: {exc}"
            continue
        except Exception as exc:  # pragma: no cover - last-resort guard
            log.exception("Unexpected download error for %s", cand.url)
            last_error = f"download_failed: {exc}"
            continue

        # validate
        try:
            validate_media(dl.file_path, funnel)
        except ValidationError as exc:
            log.warning("Validation failed for %s: %s", dl.file_path, exc)
            reject_file(dl.file_path, funnel.funnel_id, reason=str(exc))
            last_error = f"validation_failed: {exc}"
            continue
        except Exception as exc:  # pragma: no cover
            log.exception("Unexpected validation error for %s", dl.file_path)
            reject_file(dl.file_path, funnel.funnel_id, reason=str(exc))
            last_error = f"validation_failed: {exc}"
            continue

        # store
        try:
            ready_path = store_ready(dl.file_path, funnel.funnel_id)
        except StorageError as exc:
            log.error("Storage failed for %s: %s", dl.file_path, exc)
            return _failed(funnel.funnel_id, f"storage_failed: {exc}")

        # mark seen ONLY after success
        try:
            seen.mark_seen(video_id=cand.video_id, url=cand.url)
        except Exception as exc:  # pragma: no cover
            log.exception("Failed to update seen store after success")
            # The video is on disk and ready; surface the success but include a note.
            payload = _success(funnel.funnel_id, ready_path, cand.url, cand.title)
            payload["warning"] = f"seen_store_update_failed: {exc}"
            return payload

        return _success(funnel.funnel_id, ready_path, cand.url, cand.title)

    # Every valid candidate failed download or validation.
    return _no_input(
        funnel.funnel_id,
        last_error or "No valid non-duplicate video found.",
    )
