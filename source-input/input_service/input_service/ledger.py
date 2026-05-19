"""Durable input/job ledger shared across ingestion and video processing.

Each selected source receives a stable ``input_id`` before download work starts.
The record then moves through explicit states:

``discovered -> downloaded -> processing -> succeeded/failed``

Records are stored as one JSON file per input under ``data/state/input_jobs`` so
the source input service and video-automation service can update the same
boundary without inferring state from filenames.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths


VALID_STATES = {"discovered", "downloaded", "processing", "succeeded", "failed"}
TERMINAL_STATES = {"succeeded", "failed"}
ALLOWED_TRANSITIONS = {
    "discovered": {"downloaded", "failed"},
    "downloaded": {"processing", "failed"},
    "processing": {"succeeded", "failed"},
    # A failed record may be retried by explicitly moving it back to processing
    # if the same downloaded file is still valid.
    "failed": {"processing", "failed"},
    "succeeded": set(),
}
_INPUT_ID_RE = re.compile(r"^input_\d{8}T\d{6}Z_[a-f0-9]{8}$")
_LOCK = threading.Lock()


class LedgerError(Exception):
    pass


class LedgerNotFound(LedgerError):
    pass


class LedgerStateError(LedgerError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ledger_dir() -> Path:
    override = os.environ.get("INPUT_JOB_LEDGER_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return paths.STATE_DIR / "input_jobs"


def new_input_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"input_{stamp}_{uuid.uuid4().hex[:8]}"


def _record_path(input_id: str) -> Path:
    clean = str(input_id or "").strip()
    if not _INPUT_ID_RE.fullmatch(clean):
        raise LedgerError(f"Invalid input_id: {input_id!r}")
    return ledger_dir() / f"{clean}.json"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.stem}.",
        suffix=".tmp",
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def load_record(input_id: str) -> dict[str, Any]:
    path = _record_path(input_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LedgerNotFound(f"Input ledger record not found: {input_id}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"Could not read input ledger record {input_id}: {exc}") from exc
    if not isinstance(raw, dict):
        raise LedgerError(f"Input ledger record is not an object: {input_id}")
    return raw


def iter_records() -> list[dict[str, Any]]:
    root = ledger_dir()
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("input_*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict):
            out.append(raw)
    return out


def source_has_non_failed_record(*, video_id: str | None = None, url: str | None = None) -> bool:
    """Return true when a source has been handed off or successfully processed.

    ``discovered`` records are intentionally not blocking: if ingestion crashes
    before download/storage, the candidate can be retried.
    """
    vid = str(video_id).strip() if video_id else ""
    source_url = str(url).strip() if url else ""
    if not vid and not source_url:
        return False
    for record in iter_records():
        if str(record.get("state") or "") not in {"downloaded", "processing", "succeeded"}:
            continue
        meta = record.get("source_metadata")
        if not isinstance(meta, dict):
            meta = {}
        if vid and str(meta.get("video_id") or "") == vid:
            return True
        if source_url and str(record.get("source_url") or "") == source_url:
            return True
    return False


def create_record(
    *,
    funnel_id: str,
    source_url: str | None = None,
    source_metadata: dict[str, Any] | None = None,
    funnel_policy: dict[str, Any] | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    input_id = new_input_id()
    ts = now_iso()
    record: dict[str, Any] = {
        "schema_version": 1,
        "input_id": input_id,
        "job_id": input_id,
        "state": "discovered",
        "funnel_id": str(funnel_id),
        "source_url": source_url,
        "source_metadata": _jsonable(source_metadata or {}),
        "funnel_policy": _jsonable(funnel_policy or {}),
        "file_path": None,
        "created_at": ts,
        "updated_at": ts,
        "retry": {
            "attempt_count": 1,
            "max_attempts": max_attempts,
            "last_attempt_at": ts,
        },
        "error": None,
        "result": None,
        "state_history": [{"state": "discovered", "at": ts}],
    }
    with _LOCK:
        _atomic_write(_record_path(input_id), record)
    return record


def transition(
    input_id: str,
    state: str,
    *,
    file_path: str | Path | None = None,
    error: Any = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if state not in VALID_STATES:
        raise LedgerStateError(f"Invalid input ledger state: {state}")

    with _LOCK:
        record = load_record(input_id)
        ts = now_iso()
        previous = str(record.get("state") or "")
        if previous and state != previous and state not in ALLOWED_TRANSITIONS.get(previous, set()):
            raise LedgerStateError(f"Invalid input ledger transition: {previous} -> {state}")
        record["state"] = state
        record["updated_at"] = ts
        if file_path is not None:
            record["file_path"] = str(file_path)
        if error is not None:
            record["error"] = _jsonable(error)
        elif state not in {"failed"}:
            record["error"] = None
        if result is not None:
            record["result"] = _jsonable(result)
        history = record.get("state_history")
        if not isinstance(history, list):
            history = []
        row: dict[str, Any] = {"state": state, "at": ts}
        if previous:
            row["from_state"] = previous
        if error is not None:
            row["error"] = _jsonable(error)
        history.append(row)
        record["state_history"] = history
        _atomic_write(_record_path(input_id), record)
        return record


def mark_downloaded(input_id: str, file_path: str | Path) -> dict[str, Any]:
    return transition(input_id, "downloaded", file_path=file_path)


def mark_processing(input_id: str) -> dict[str, Any]:
    return transition(input_id, "processing")


def mark_succeeded(input_id: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
    return transition(input_id, "succeeded", result=result or {})


def mark_failed(input_id: str, error: Any, result: dict[str, Any] | None = None) -> dict[str, Any]:
    return transition(input_id, "failed", error=error, result=result or {})


def resolve_file_path(input_id: str) -> Path:
    record = load_record(input_id)
    raw_path = str(record.get("file_path") or "").strip()
    if not raw_path:
        raise LedgerStateError(f"Input ledger record has no file_path: {input_id}")
    return Path(raw_path).expanduser().resolve()
