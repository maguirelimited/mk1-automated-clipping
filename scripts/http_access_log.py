"""Filter noisy Werkzeug HTTP access logs for local multi-service runs.

Health probes, static assets, and other high-frequency read-only endpoints
drown out pipeline progress lines when every Flask service logs to one terminal.

Set ``MK04_HTTP_ACCESS_LOG=1`` to restore full Werkzeug access logging.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Iterable

DEFAULT_QUIET_PATH_PREFIXES: tuple[str, ...] = (
    "/healthz",
    "/health",
    "/static/",
    "/favicon.ico",
)

OPS_UI_QUIET_PATH_PREFIXES: tuple[str, ...] = DEFAULT_QUIET_PATH_PREFIXES + (
    "/status",
    "/services",
)

_REQUEST_PATH = re.compile(r'"[A-Z]+ ([^ ]+) HTTP/')
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _enabled() -> bool:
    return os.environ.get("MK04_HTTP_ACCESS_LOG", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _normalize_access_log_message(message: str) -> str:
    """Werkzeug colourises status codes in access logs; strip before parsing."""
    return _ANSI_ESCAPE.sub("", message)


def _request_path(message: str) -> str | None:
    match = _REQUEST_PATH.search(_normalize_access_log_message(message))
    if match is None:
        return None
    return match.group(1).split("?", 1)[0]


def should_quiet_access_log(message: str, quiet_prefixes: Iterable[str]) -> bool:
    """Return True when an access-log line should be suppressed."""
    path = _request_path(message)
    if path is None:
        return False
    return any(path == prefix or path.startswith(prefix) for prefix in quiet_prefixes)


class QuietAccessLogFilter(logging.Filter):
    def __init__(self, quiet_prefixes: tuple[str, ...]) -> None:
        super().__init__()
        self._quiet_prefixes = quiet_prefixes

    def filter(self, record: logging.LogRecord) -> bool:
        return not should_quiet_access_log(record.getMessage(), self._quiet_prefixes)


class _ServiceAccessLogFormatter(logging.Formatter):
    def __init__(self, service_label: str | None) -> None:
        super().__init__()
        self._service_label = (service_label or "").strip()

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if self._service_label:
            return f"[{self._service_label}] {message}"
        return message


def configure_quiet_http_access_logging(
    *,
    quiet_prefixes: tuple[str, ...] | None = None,
    service_label: str | None = None,
) -> None:
    """Install a single Werkzeug access logger with quiet-path filtering."""
    if _enabled():
        return

    prefixes = quiet_prefixes or DEFAULT_QUIET_PATH_PREFIXES
    logger = logging.getLogger("werkzeug")
    logger.propagate = False
    logger.setLevel(logging.INFO)

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_ServiceAccessLogFormatter(service_label))
    handler.addFilter(QuietAccessLogFilter(prefixes))
    logger.addHandler(handler)


def ensure_scripts_on_path(start: str) -> None:
    """Add the repo ``scripts/`` directory to ``sys.path`` when needed."""
    from pathlib import Path

    here = Path(start).resolve().parent
    for candidate in (here, *here.parents):
        scripts_dir = candidate / "scripts"
        if (scripts_dir / "http_access_log.py").is_file():
            text = str(scripts_dir)
            if text not in sys.path:
                sys.path.insert(0, text)
            return
    raise ImportError("Could not locate repo scripts/ directory")
