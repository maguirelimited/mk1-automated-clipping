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
import shutil
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
from . import ledger
from .media_validator import ValidationError, validate_media
from .source_checker import SourceCheckError, check_sources
from .storage import StorageError, reject_file, store_ready


log = logging.getLogger(__name__)


def _cleanup_funnel_tmp_after_store(funnel_id: str) -> None:
    """Remove yt-dlp scratch files under ``data/tmp/<funnel_id>`` after a successful store."""
    d = paths.funnel_tmp_dir(funnel_id)
    if not d.is_dir():
        return
    try:
        shutil.rmtree(d)
    except OSError as exc:
        log.warning("Could not remove funnel tmp dir %s: %s", d, exc)


def _funnel_runtime_metadata(funnel: Funnel) -> dict[str, Any]:
    return {
        "pipeline_profile": funnel.pipeline_profile,
        "posting_config": funnel.posting_config or {},
        "analytics_config": funnel.analytics_config or {},
        "max_downloads_per_run": funnel.max_downloads_per_run,
    }


def _candidate_source_metadata(cand: Any) -> dict[str, Any]:
    return {
        "video_id": cand.video_id,
        "title": cand.title,
        "channel": cand.channel,
        "source_url": cand.source,
        "duration_seconds": cand.duration_seconds,
        "upload_date": cand.upload_date,
        "timestamp": cand.timestamp,
        "source_id": cand.extra.get("source_id"),
        "source_type": cand.extra.get("source_type"),
        "source_label": cand.extra.get("source_label"),
    }


def _funnel_policy_snapshot(funnel: Funnel) -> dict[str, Any]:
    return {
        "pipeline_profile": funnel.pipeline_profile,
        "posting_config": funnel.posting_config or {},
        "analytics_config": funnel.analytics_config or {},
        "max_downloads_per_run": funnel.max_downloads_per_run,
        "min_duration_seconds": funnel.min_duration_seconds,
        "max_duration_seconds": funnel.max_duration_seconds,
        "title_blocklist": list(getattr(funnel, "title_blocklist", ()) or ()),
        "title_allowlist": list(getattr(funnel, "title_allowlist", ()) or ()),
    }


def _filter_ledger_blocked(candidates: list[Any]) -> tuple[list[Any], int]:
    out: list[Any] = []
    blocked = 0
    for cand in candidates:
        if ledger.source_has_non_failed_record(video_id=cand.video_id, url=cand.url):
            blocked += 1
            continue
        out.append(cand)
    return out, blocked


def _success(funnel: Funnel, video_path: Path, cand: Any, record: dict[str, Any]) -> dict[str, Any]:
    input_id = str(record.get("input_id") or "")
    return {
        "success": True,
        "status": "input_ready",
        "funnel_id": funnel.funnel_id,
        "input_id": input_id,
        "job_id": input_id,
        "input_state": record.get("state"),
        "ledger_path": str(ledger.ledger_dir() / f"{input_id}.json") if input_id else None,
        "video_path": str(video_path),
        "source_url": cand.url,
        "title": cand.title,
        "source": {
            "source_id": cand.extra.get("source_id"),
            "source_type": cand.extra.get("source_type"),
            "source_label": cand.extra.get("source_label"),
            "channel": cand.channel,
        },
        **_funnel_runtime_metadata(funnel),
    }


def _no_input(funnel: Funnel, reason: str) -> dict[str, Any]:
    return {
        "success": True,
        "status": "no_input_available",
        "funnel_id": funnel.funnel_id,
        "reason": reason,
        **_funnel_runtime_metadata(funnel),
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
    log.info("Funnel started: funnel_id=%s", funnel_id)

    # ---- load funnel ------------------------------------------------------
    try:
        funnel: Funnel = load_funnel(funnel_id)
        log.info(
            "Funnel loaded: funnel_id=%s sources=%s duration_min=%s-%s max_downloads=%s",
            funnel.funnel_id,
            len(funnel.source_configs),
            funnel.min_duration_minutes,
            funnel.max_duration_minutes,
            funnel.max_downloads_per_run,
        )
    except FunnelNotFoundError as exc:
        log.info("Final funnel status: funnel_id=%s status=failed error=unknown_funnel", funnel_id)
        return _failed(funnel_id, f"unknown_funnel: {exc}")
    except FunnelInactiveError as exc:
        log.info("Final funnel status: funnel_id=%s status=failed error=inactive_funnel", funnel_id)
        return _failed(funnel_id, f"inactive_funnel: {exc}")
    except FunnelInvalidError as exc:
        log.info("Final funnel status: funnel_id=%s status=failed error=invalid_funnel", funnel_id)
        return _failed(funnel_id, f"invalid_funnel: {exc}")
    except FunnelError as exc:  # safety net
        log.info("Final funnel status: funnel_id=%s status=failed error=funnel_error", funnel_id)
        return _failed(funnel_id, f"funnel_error: {exc}")

    # ---- check sources ----------------------------------------------------
    try:
        candidates = check_sources(
            funnel.source_configs,
            max_candidates=max(1, funnel.max_downloads_per_run * 5),
        )
        log.info(
            "Candidate videos found: funnel_id=%s count=%s",
            funnel.funnel_id,
            len(candidates),
        )
    except SourceCheckError as exc:
        log.info(
            "Final funnel status: funnel_id=%s status=failed error=source_check_failed reason=%s",
            funnel.funnel_id,
            exc,
        )
        return _failed(funnel.funnel_id, f"source_check_failed: {exc}")
    except Exception as exc:  # pragma: no cover - last-resort guard
        log.exception("Unexpected source check error")
        log.info(
            "Final funnel status: funnel_id=%s status=failed error=source_check_failed reason=%s",
            funnel.funnel_id,
            exc,
        )
        return _failed(funnel.funnel_id, f"source_check_failed: {exc}")

    if not candidates:
        log.info(
            "Final funnel status: funnel_id=%s status=no_input_available reason=no_candidates",
            funnel.funnel_id,
        )
        return _no_input(funnel, "No candidate videos found in approved sources.")

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
        log.info(
            "Candidate filter result: funnel_id=%s valid=0 rejected=%s reason=%s",
            funnel.funnel_id,
            len(rejected),
            reason,
        )
        log.info(
            "Final funnel status: funnel_id=%s status=no_input_available reason=%s",
            funnel.funnel_id,
            reason,
        )
        return _no_input(funnel, reason)
    log.info(
        "Candidate filter result: funnel_id=%s valid=%s rejected=%s",
        funnel.funnel_id,
        len(valid),
        len(rejected),
    )
    valid, ledger_blocked = _filter_ledger_blocked(valid)
    if ledger_blocked:
        log.info(
            "Candidate ledger filter result: funnel_id=%s blocked=%s remaining=%s",
            funnel.funnel_id,
            ledger_blocked,
            len(valid),
        )
    if not valid:
        reason = "All valid candidates already have active or completed input ledger records."
        log.info(
            "Final funnel status: funnel_id=%s status=no_input_available reason=%s",
            funnel.funnel_id,
            reason,
        )
        return _no_input(funnel, reason)

    # ---- download / validate / store loop --------------------------------
    last_error: str | None = None

    for cand in valid:
        log.info(
            "Selected candidate URL: funnel_id=%s url=%s title=%s duration_seconds=%s",
            funnel.funnel_id,
            cand.url,
            cand.title,
            cand.duration_seconds,
        )
        try:
            record = ledger.create_record(
                funnel_id=funnel.funnel_id,
                source_url=cand.url,
                source_metadata=_candidate_source_metadata(cand),
                funnel_policy=_funnel_policy_snapshot(funnel),
                max_attempts=funnel.max_downloads_per_run,
            )
        except ledger.LedgerError as exc:
            log.exception("Failed to create input ledger record")
            return _failed(funnel.funnel_id, f"ledger_create_failed: {exc}")
        input_id = str(record["input_id"])
        try:
            dl = download_candidate(cand, funnel_id=funnel.funnel_id)
        except DownloadFailed as exc:
            log.warning("Download failed for %s: %s", cand.url, exc)
            log.info(
                "Download failure reason: funnel_id=%s url=%s reason=%s",
                funnel.funnel_id,
                cand.url,
                exc,
            )
            try:
                ledger.mark_failed(input_id, f"download_failed: {exc}")
            except ledger.LedgerError:
                log.exception("Failed to mark input ledger download failure")
            last_error = f"download_failed: {exc}"
            continue
        except Exception as exc:  # pragma: no cover - last-resort guard
            log.exception("Unexpected download error for %s", cand.url)
            log.info(
                "Download failure reason: funnel_id=%s url=%s reason=%s",
                funnel.funnel_id,
                cand.url,
                exc,
            )
            try:
                ledger.mark_failed(input_id, f"download_failed: {exc}")
            except ledger.LedgerError:
                log.exception("Failed to mark input ledger download failure")
            last_error = f"download_failed: {exc}"
            continue
        log.info("Download success path: funnel_id=%s path=%s", funnel.funnel_id, dl.file_path)

        # validate
        try:
            validate_media(dl.file_path, funnel)
        except ValidationError as exc:
            log.warning("Validation failed for %s: %s", dl.file_path, exc)
            reject_file(dl.file_path, funnel.funnel_id, reason=str(exc))
            try:
                ledger.mark_failed(input_id, f"validation_failed: {exc}")
            except ledger.LedgerError:
                log.exception("Failed to mark input ledger validation failure")
            last_error = f"validation_failed: {exc}"
            continue
        except Exception as exc:  # pragma: no cover
            log.exception("Unexpected validation error for %s", dl.file_path)
            reject_file(dl.file_path, funnel.funnel_id, reason=str(exc))
            try:
                ledger.mark_failed(input_id, f"validation_failed: {exc}")
            except ledger.LedgerError:
                log.exception("Failed to mark input ledger validation failure")
            last_error = f"validation_failed: {exc}"
            continue

        # store (video-automation input dir first; local READY_DIR as fallback)
        try:
            ready_path = store_ready(dl.file_path, funnel.funnel_id)
        except StorageError as exc:
            log.error("Storage failed for %s: %s", dl.file_path, exc)
            try:
                ledger.mark_failed(input_id, f"storage_failed: {exc}")
            except ledger.LedgerError:
                log.exception("Failed to mark input ledger storage failure")
            log.info(
                "Final funnel status: funnel_id=%s status=failed error=storage_failed reason=%s",
                funnel.funnel_id,
                exc,
            )
            return _failed(funnel.funnel_id, f"storage_failed: {exc}")

        ready_path = ready_path.resolve()
        if not ready_path.is_file():
            try:
                ledger.mark_failed(
                    input_id,
                    f"storage_verification_failed: file missing at {ready_path}",
                )
            except ledger.LedgerError:
                log.exception("Failed to mark input ledger storage verification failure")
            return _failed(
                funnel.funnel_id,
                f"storage_verification_failed: file missing at {ready_path}",
            )
        try:
            record = ledger.mark_downloaded(input_id, ready_path)
        except ledger.LedgerError as exc:
            log.exception("Failed to mark input ledger downloaded")
            return _failed(funnel.funnel_id, f"ledger_update_failed: {exc}")

        _cleanup_funnel_tmp_after_store(funnel.funnel_id)

        log.info(
            "Final funnel status: funnel_id=%s status=input_ready input_id=%s video_path=%s",
            funnel.funnel_id,
            input_id,
            ready_path,
        )
        return _success(funnel, ready_path, cand, record)

    # Every valid candidate failed download or validation.
    log.info(
        "Final funnel status: funnel_id=%s status=no_input_available reason=%s",
        funnel.funnel_id,
        last_error or "No valid non-duplicate video found.",
    )
    return _no_input(
        funnel,
        last_error or "No valid non-duplicate video found.",
    )
