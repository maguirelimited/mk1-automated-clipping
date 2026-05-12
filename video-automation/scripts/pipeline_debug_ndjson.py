"""Optional NDJSON debug sinks (one JSON object per line).

These hooks are **off** unless the corresponding ``*_PATH`` environment variable
is set. They share shape for operational tooling:

``sessionId``, ``runId``, ``hypothesisId``, ``location``, ``message``, ``data``, ``timestamp``.

If ``PIPELINE_DEBUG_SESSION_ID`` is set, it overrides every sink's default
``sessionId`` (useful for correlating across processes).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Mapping

# Fallback session IDs preserved for continuity with existing log parsers.
_SESSION_FALLBACK: dict[str, str] = {
    "main": "04601b",
    "agent": "35c21b",
    "mode": "c9492c",
    "cursor": "789d41",
}


def _resolve_session_id(role: str) -> str:
    override = (os.environ.get("PIPELINE_DEBUG_SESSION_ID") or "").strip()
    if override:
        return override
    return _SESSION_FALLBACK.get(role, "mk04-debug")


def write_ndjson_sink(
    path_env_var: str,
    *,
    session_role: str,
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: Mapping[str, Any],
) -> None:
    path = (os.environ.get(path_env_var) or "").strip()
    if not path:
        return
    payload = {
        "sessionId": _resolve_session_id(session_role),
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": dict(data),
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        pass


def write_debug_main(
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: Mapping[str, Any],
) -> None:
    write_ndjson_sink(
        "DEBUG_LOG_PATH",
        session_role="main",
        run_id=run_id,
        hypothesis_id=hypothesis_id,
        location=location,
        message=message,
        data=data,
    )


def write_debug_agent(
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: Mapping[str, Any],
) -> None:
    write_ndjson_sink(
        "AGENT_DEBUG_LOG_PATH",
        session_role="agent",
        run_id=run_id,
        hypothesis_id=hypothesis_id,
        location=location,
        message=message,
        data=data,
    )


def write_debug_mode(
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: Mapping[str, Any],
) -> None:
    write_ndjson_sink(
        "DEBUG_MODE_LOG_PATH",
        session_role="mode",
        run_id=run_id,
        hypothesis_id=hypothesis_id,
        location=location,
        message=message,
        data=data,
    )


def write_diagnostic(
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: Mapping[str, Any],
) -> None:
    """Portable substitute for legacy hard-coded IDE diagnostic paths.

    Set ``PIPELINE_DIAGNOSTIC_LOG_PATH`` to enable (JSON lines, same schema).
    """
    write_ndjson_sink(
        "PIPELINE_DIAGNOSTIC_LOG_PATH",
        session_role="cursor",
        run_id=run_id,
        hypothesis_id=hypothesis_id,
        location=location,
        message=message,
        data=data,
    )
