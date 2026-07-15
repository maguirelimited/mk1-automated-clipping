"""Terminal-friendly logging helpers for source-input.

Default local runs print short stage lines via ``emit_progress`` / ``emit_stage``.
Set ``INPUT_SERVICE_VERBOSE=1`` to restore detailed INFO logs for debugging.
"""

from __future__ import annotations

import logging
import os

_TRUTHY = {"1", "true", "yes", "on"}

# Chatty modules that flood the terminal at INFO during funnel runs.
_QUIET_WHEN_NORMAL: tuple[str, ...] = (
    "input_service.yt_dlp_cookies",
    "input_service.source_checker",
    "input_service.downloader",
    "input_service.runner",
    "input_service.storage",
    "input_service.clipping_client",
)


def verbose_enabled() -> bool:
    return os.environ.get("INPUT_SERVICE_VERBOSE", "0").strip().lower() in _TRUTHY


def detail(logger: logging.Logger, msg: str, *args: object, **kwargs: object) -> None:
    """Log at INFO when verbose, otherwise DEBUG (hidden by default level)."""
    if verbose_enabled():
        logger.info(msg, *args, **kwargs)
    else:
        logger.debug(msg, *args, **kwargs)


def configure_service_logging() -> None:
    """Apply module log levels after ``logging.basicConfig`` in app startup."""
    if verbose_enabled():
        return
    for name in _QUIET_WHEN_NORMAL:
        logging.getLogger(name).setLevel(logging.WARNING)
