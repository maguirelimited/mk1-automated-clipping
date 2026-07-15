"""Bounded, read-only log access for observability endpoints (Phase 5).

Reuses scripts/ops/logs_report.py (journalctl + file tails) and the job
artifact resolver. Does not introduce a parallel logging system.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from .artifacts import resolve_job_artifacts
from .index import (
    _env_token,
    _find_job_dir,
    _is_safe_id,
    _jobs_root_for,
)
from .models import LogEntry, LogReference
from .schemas import CONTRACT_SCHEMA_VERSION

_OPS_DIR = Path(__file__).resolve().parent.parent / "ops"
if str(_OPS_DIR) not in sys.path:
    sys.path.insert(0, str(_OPS_DIR))

from logs_report import (  # noqa: E402
    ERROR_LINE_RE,
    clamp_lines,
    fetch_errors,
    fetch_service_logs,
    load_state,
    read_tail_lines,
    redact_line,
)
from ops_readonly import DEFAULT_LOG_LINES, MAX_LOG_LINES  # noqa: E402

# journalctl --output=short-iso: "2026-07-04T12:00:00+00:00 host unit[pid]: msg"
_JOURNAL_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)?)\s+\S+\s+(?P<rest>.*)$"
)
_SEVERITY_ERROR_RE = ERROR_LINE_RE
_SEVERITY_WARN_RE = re.compile(r"\bWARN(?:ING)?\b", re.IGNORECASE)
_SEVERITY_INFO_RE = re.compile(r"\bINFO\b", re.IGNORECASE)
_SEVERITY_DEBUG_RE = re.compile(r"\bDEBUG\b", re.IGNORECASE)

SERVICE_LOG_SOURCES = frozenset({"api", "worker", "ai", "scheduler", "errors"})


def _infer_severity(message: str) -> str | None:
    if _SEVERITY_ERROR_RE.search(message):
        return "error"
    if _SEVERITY_WARN_RE.search(message):
        return "warn"
    if _SEVERITY_INFO_RE.search(message):
        return "info"
    if _SEVERITY_DEBUG_RE.search(message):
        return "debug"
    return None


def _parse_line(raw: str, *, source: str) -> LogEntry:
    text = redact_line(raw.rstrip("\n"))
    timestamp = None
    message = text
    match = _JOURNAL_LINE_RE.match(text)
    if match:
        timestamp = match.group("ts")
        if timestamp.endswith("+00:00"):
            timestamp = timestamp[:-6] + "Z"
        message = match.group("rest").strip() or text
    return LogEntry(
        message=message,
        source=source,
        timestamp=timestamp,
        severity=_infer_severity(message),
    )


def _status_for_result(*, unavailable: bool, empty: bool, count: int) -> str:
    if unavailable:
        return "unavailable"
    if empty or count == 0:
        return "empty"
    return "ok"


def _payload(
    *,
    environment: str,
    source: str,
    entries: list[LogEntry],
    limit: int,
    status: str,
    origin: str | None = None,
    log_reference: LogReference | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "environment": environment,
        "source": source,
        "status": status,
        "limit": limit,
        "count": len(entries),
        "entries": [entry.to_dict() for entry in entries],
        "origin": origin,
        "schema_version": CONTRACT_SCHEMA_VERSION,
    }
    if job_id is not None:
        data["job_id"] = job_id
    if log_reference is not None:
        data["log_reference"] = log_reference.to_dict()
    return data


def build_service_logs_payload(
    mk04_env_token: str,
    mode: str,
    *,
    lines: int | None = None,
) -> dict[str, Any]:
    """Build bounded log payload for api|worker|ai|scheduler|errors."""
    token = _env_token(mk04_env_token)
    limit = clamp_lines(lines if lines is not None else DEFAULT_LOG_LINES)
    mode_key = mode.strip().lower()
    if mode_key not in SERVICE_LOG_SOURCES:
        return _payload(
            environment=token,
            source=mode_key,
            entries=[],
            limit=limit,
            status="unavailable",
            origin="unknown mode",
        )

    state, config_error = load_state(token)
    if state is None:
        return _payload(
            environment=token,
            source=mode_key,
            entries=[
                LogEntry(
                    message=f"config load failed: {config_error or 'unknown'}",
                    source=mode_key,
                    severity="error",
                )
            ],
            limit=limit,
            status="unavailable",
            origin="config",
        )

    if mode_key == "errors":
        result = fetch_errors(state, token, lines=limit)
    else:
        result = fetch_service_logs(mode_key, state, token, lines=limit)

    entries = [_parse_line(line, source=mode_key) for line in result.lines]

    return _payload(
        environment=token,
        source=mode_key,
        entries=entries,
        limit=limit,
        status=_status_for_result(
            unavailable=result.unavailable,
            empty=result.empty,
            count=len(entries),
        ),
        origin=_safe_origin(result.source),
    )


def _safe_origin(source: str | None) -> str | None:
    """Never expose absolute filesystem paths in origin metadata."""
    text = (source or "").strip()
    if not text:
        return None
    if text.startswith("/") or "/home/" in text or "/var/" in text or "/opt/" in text:
        return "journalctl" if "journalctl" in text else "file logs"
    return text


def _resolve_job_log_file(
    job_dir: Path,
    *,
    token: str,
    job_id: str,
) -> tuple[Path | None, LogReference]:
    """Locate job log via artifact resolver paths, constrained to job_dir."""
    payload = resolve_job_artifacts(token, job_id)
    log_ref = LogReference(source="job", job_id=job_id, path=None, detail="job log not found")
    if payload is None:
        return None, log_ref

    for item in payload.get("logs") or []:
        if isinstance(item, dict):
            parsed = LogReference.from_dict(item)
            if parsed is not None:
                log_ref = parsed
                break

    relative: str | None = None
    for item in payload.get("artifacts") or []:
        if not isinstance(item, dict):
            continue
        if item.get("artifact_type") != "job_log":
            continue
        if item.get("exists") and item.get("path"):
            relative = str(item["path"])
            break

    if not relative:
        return None, log_ref

    # path is jobs/<env>/<job_id>/...
    prefix = f"jobs/{token}/{job_id}/"
    if not relative.startswith(prefix):
        return None, log_ref
    suffix = relative[len(prefix) :]
    if not suffix or ".." in suffix.split("/"):
        return None, log_ref

    target = (job_dir / suffix).resolve()
    try:
        target.relative_to(job_dir.resolve())
    except ValueError:
        return None, log_ref
    if not target.is_file():
        return None, log_ref
    return target, log_ref


def build_job_logs_payload(
    mk04_env_token: str,
    job_id: str,
    *,
    lines: int | None = None,
) -> dict[str, Any] | None:
    """Build bounded job log payload, or None if the job cannot be resolved."""
    if not _is_safe_id(job_id):
        return None

    token = _env_token(mk04_env_token)
    jobs_root = _jobs_root_for(token)
    job_dir = _find_job_dir(jobs_root, job_id)
    if job_dir is None:
        return None

    limit = clamp_lines(lines if lines is not None else DEFAULT_LOG_LINES)
    path, log_ref = _resolve_job_log_file(job_dir, token=token, job_id=job_id)
    if path is None:
        return _payload(
            environment=token,
            source="job",
            entries=[],
            limit=limit,
            status="empty",
            origin=None,
            log_reference=log_ref,
            job_id=job_id,
        )

    raw_lines = read_tail_lines(path, max_lines=limit)
    entries = [_parse_line(line, source="job") for line in raw_lines]
    return _payload(
        environment=token,
        source="job",
        entries=entries,
        limit=limit,
        status=_status_for_result(unavailable=False, empty=not entries, count=len(entries)),
        origin=log_ref.path,
        log_reference=log_ref,
        job_id=job_id,
    )


def default_log_limit() -> int:
    return DEFAULT_LOG_LINES


def max_log_limit() -> int:
    return MAX_LOG_LINES
