"""Orchestrate one funnel run end-to-end.

Implements the flow described in ``source-input-context.txt``:

    run_funnel(funnel_id):
        load funnel
        check approved sources
        for each newest-first source candidate:
            skip if seen
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

import json
import logging
import os
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import paths
from .candidate_filter import filter_candidates
from .clipping_client import enqueue_clipping_job
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
from .source_checker import (
    DEFAULT_MAX_VIDEOS_PER_SOURCE,
    SourceCheckError,
    iter_source_candidates,
)
from .log_util import detail, verbose_enabled
from .storage import StorageError, reject_file, store_ready


log = logging.getLogger(__name__)

_DEBUG_LOG = Path("/Users/anthonymaguire/VAmk0.4/.cursor/debug-291a3a.log")


def _progress_enabled() -> bool:
    return os.environ.get("INPUT_PROGRESS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def emit_progress(message: str, *, funnel_id: str | None = None) -> None:
    """Short status lines on the input service terminal while a funnel runs."""
    if not _progress_enabled():
        return
    prefix = "[input]"
    if funnel_id:
        prefix = f"[input funnel={funnel_id}]"
    print(f"{prefix} {message}", flush=True)


def emit_stage(stage: str, *, funnel_id: str | None = None, note: str | None = None) -> None:
    """Print a single-word pipeline stage; add detail only when verbose."""
    if note and verbose_enabled():
        emit_progress(f"{stage} — {note}", funnel_id=funnel_id)
    else:
        emit_progress(stage, funnel_id=funnel_id)


# #region agent log
def _agent_debug(
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, Any],
    *,
    run_id: str = "pre-fix",
) -> None:
    try:
        with _DEBUG_LOG.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "sessionId": "291a3a",
                        "runId": run_id,
                        "hypothesisId": hypothesis_id,
                        "location": location,
                        "message": message,
                        "data": data,
                        "timestamp": int(time.time() * 1000),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError:
        pass


# #endregion


def _candidate_scan_limit(funnel: Funnel) -> int:
    """How many channel videos to consider per run (newest first).

    Uses each source's ``max_videos_per_source`` (funnel config), not
    ``max_downloads_per_run``, so we can skip past already-seen uploads.
    """
    limits: list[int] = []
    for src in funnel.source_configs:
        if isinstance(src, str):
            limits.append(DEFAULT_MAX_VIDEOS_PER_SOURCE)
            continue
        if not getattr(src, "active", True):
            continue
        per = getattr(src, "max_videos_per_source", None)
        limits.append(int(per) if per else DEFAULT_MAX_VIDEOS_PER_SOURCE)
    return max(limits) if limits else DEFAULT_MAX_VIDEOS_PER_SOURCE


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


def run_funnel(
    funnel_id: str,
    *,
    orchestration_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one funnel and return a JSON-serialisable response dict.

    This function never raises; all problems are mapped to a structured
    response (``input_ready`` / ``no_input_available`` / ``failed``).
    """
    paths.ensure_dirs()
    detail(log, "Funnel started: funnel_id=%s", funnel_id)
    emit_stage("config", funnel_id=funnel_id)

    # ---- load funnel ------------------------------------------------------
    try:
        funnel: Funnel = load_funnel(funnel_id)
        emit_stage(
            "scanning",
            funnel_id=funnel.funnel_id,
            note=f"{len(funnel.source_configs)} source(s)",
        )
        detail(
            log,
            "Funnel loaded: funnel_id=%s sources=%s duration_min=%s-%s max_downloads=%s",
            funnel.funnel_id,
            len(funnel.source_configs),
            funnel.min_duration_minutes,
            funnel.max_duration_minutes,
            funnel.max_downloads_per_run,
        )
    except FunnelNotFoundError as exc:
        detail(log, "Final funnel status: funnel_id=%s status=failed error=unknown_funnel", funnel_id)
        return _failed(funnel_id, f"unknown_funnel: {exc}")
    except FunnelInactiveError as exc:
        detail(log, "Final funnel status: funnel_id=%s status=failed error=inactive_funnel", funnel_id)
        return _failed(funnel_id, f"inactive_funnel: {exc}")
    except FunnelInvalidError as exc:
        detail(log, "Final funnel status: funnel_id=%s status=failed error=invalid_funnel", funnel_id)
        return _failed(funnel_id, f"invalid_funnel: {exc}")
    except FunnelError as exc:  # safety net
        detail(log, "Final funnel status: funnel_id=%s status=failed error=funnel_error", funnel_id)
        return _failed(funnel_id, f"funnel_error: {exc}")

    # ---- check sources ----------------------------------------------------
    scan_limit = _candidate_scan_limit(funnel)
    # #region agent log
    _agent_debug(
        "A",
        "runner.py:run_funnel",
        "candidate_scan_limit",
        {
            "funnel_id": funnel.funnel_id,
            "scan_limit": scan_limit,
            "legacy_cap": max(1, funnel.max_downloads_per_run * 5),
            "max_downloads_per_run": funnel.max_downloads_per_run,
        },
    )
    # #endregion
    # ---- scan / filter / download one candidate at a time -----------------
    seen = DuplicateStore()
    last_error: str | None = None
    scanned = 0
    valid_count = 0
    ledger_blocked = 0
    rejection_counts: Counter[str] = Counter()

    try:
        candidates_iter = iter_source_candidates(
            funnel.source_configs,
            max_per_source=scan_limit,
            seen=seen,
        )
        for cand in candidates_iter:
            scanned += 1
            valid, rejected = filter_candidates([cand], funnel, seen)
            if rejected:
                reason = rejected[0].reason
                rejection_counts[reason] += 1
                detail(
                    log,
                    "Candidate rejected: funnel_id=%s url=%s reason=%s",
                    funnel.funnel_id,
                    cand.url,
                    reason,
                )
                continue
            if ledger.source_has_non_failed_record(video_id=cand.video_id, url=cand.url):
                ledger_blocked += 1
                detail(
                    log,
                    "Candidate skipped by ledger: funnel_id=%s url=%s",
                    funnel.funnel_id,
                    cand.url,
                )
                continue
            valid_count += 1
            cand = valid[0]
            emit_stage(
                "candidate",
                funnel_id=funnel.funnel_id,
                note=f"{valid_count} after {scanned} checked",
            )

            # ---- download / validate / store selected candidate -----------
            title_preview = (cand.title or "untitled")[:72]
            emit_stage("select", funnel_id=funnel.funnel_id, note=title_preview)
            detail(
                log,
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
            emit_stage("downloading", funnel_id=funnel.funnel_id)
            try:
                dl = download_candidate(cand, funnel_id=funnel.funnel_id)
            except DownloadFailed as exc:
                emit_stage("retry", funnel_id=funnel.funnel_id, note="download failed")
                log.warning("Download failed for %s: %s", cand.url, exc)
                detail(
                    log,
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
                detail(
                    log,
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
            detail(log, "Download success path: funnel_id=%s path=%s", funnel.funnel_id, dl.file_path)
            emit_stage("validating", funnel_id=funnel.funnel_id)

            # validate
            try:
                validate_media(dl.file_path, funnel)
            except ValidationError as exc:
                emit_stage("retry", funnel_id=funnel.funnel_id, note="validation failed")
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

            emit_stage("storing", funnel_id=funnel.funnel_id)
            # store (video-automation input dir first; local READY_DIR as fallback)
            try:
                ready_path = store_ready(dl.file_path, funnel.funnel_id, input_id=input_id)
            except StorageError as exc:
                log.error("Storage failed for %s: %s", dl.file_path, exc)
                try:
                    ledger.mark_failed(input_id, f"storage_failed: {exc}")
                except ledger.LedgerError:
                    log.exception("Failed to mark input ledger storage failure")
                detail(
                    log,
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
            emit_stage("handoff", funnel_id=funnel.funnel_id)

            clipping = enqueue_clipping_job(
                input_id=input_id,
                funnel_id=funnel.funnel_id,
                pipeline_profile=funnel.pipeline_profile,
                orchestration_context=orchestration_context,
            )
            if not clipping.get("success"):
                err = str(clipping.get("error") or "clipping_enqueue_failed")
                log.error(
                    "Clipping enqueue failed: funnel_id=%s input_id=%s error=%s",
                    funnel.funnel_id,
                    input_id,
                    err,
                )
                try:
                    ledger.mark_failed(input_id, f"clipping_enqueue_failed: {err}")
                except ledger.LedgerError:
                    log.exception("Failed to mark input ledger after clipping enqueue failure")
                return _failed(funnel.funnel_id, f"clipping_enqueue_failed: {err}")

            detail(
                log,
                "Final funnel status: funnel_id=%s status=input_ready input_id=%s "
                "video_path=%s clipping_job_id=%s",
                funnel.funnel_id,
                input_id,
                ready_path,
                clipping.get("job_id"),
            )
            clip_job = clipping.get("job_id") or "?"
            emit_stage(
                "ready",
                funnel_id=funnel.funnel_id,
                note=f"input_id={input_id} clip_job={clip_job}",
            )
            payload = _success(funnel, ready_path, cand, record)
            payload["clipping_job"] = {
                "job_id": clipping.get("job_id"),
                "status": clipping.get("status"),
                "status_url": clipping.get("status_url"),
                "outputs_url": clipping.get("outputs_url"),
            }
            return payload
    except SourceCheckError as exc:
        detail(
            log,
            "Final funnel status: funnel_id=%s status=failed error=source_check_failed reason=%s",
            funnel.funnel_id,
            exc,
        )
        return _failed(funnel.funnel_id, f"source_check_failed: {exc}")
    except Exception as exc:  # pragma: no cover - last-resort guard
        log.exception("Unexpected source check error")
        detail(
            log,
            "Final funnel status: funnel_id=%s status=failed error=source_check_failed reason=%s",
            funnel.funnel_id,
            exc,
        )
        return _failed(funnel.funnel_id, f"source_check_failed: {exc}")

    # #region agent log
    _agent_debug(
        "B",
        "runner.py:run_funnel",
        "candidate_filter_summary",
        {
            "funnel_id": funnel.funnel_id,
            "scanned": scanned,
            "valid": valid_count,
            "rejected": sum(rejection_counts.values()),
            "ledger_blocked": ledger_blocked,
            "rejection_counts": dict(rejection_counts),
            "seen_store_size": len(seen.reload().video_ids),
        },
    )
    # #endregion

    if scanned == 0:
        reason = "No candidate videos found in approved sources."
    elif rejection_counts and all(reason == "duplicate" for reason in rejection_counts):
        reason = (
            f"All {sum(rejection_counts.values())} scanned candidate videos have already been used "
            f"(scanned up to {scan_limit} newest per source)."
        )
    elif ledger_blocked and not rejection_counts and not last_error:
        reason = "All valid candidates already have active or completed input ledger records."
    else:
        reason = last_error or "No valid non-duplicate video found."
    detail(
        log,
        "Final funnel status: funnel_id=%s status=no_input_available reason=%s",
        funnel.funnel_id,
        reason,
    )
    emit_stage("empty", funnel_id=funnel.funnel_id, note=reason[:80] if verbose_enabled() else None)
    return _no_input(funnel, reason)
