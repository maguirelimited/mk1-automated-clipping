"""Tests for scripts/http_access_log.py."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from http_access_log import (  # noqa: E402
    QuietAccessLogFilter,
    configure_quiet_http_access_logging,
    should_quiet_access_log,
)


def test_should_quiet_health_and_static_paths():
    line = '127.0.0.1 - - [05/Jul/2026 00:04:24] "GET /healthz HTTP/1.1" 200 -'
    assert should_quiet_access_log(line, ("/healthz", "/health", "/static/"))
    assert should_quiet_access_log(
        '127.0.0.1 - - [05/Jul/2026 00:04:24] "GET /static/ops.css HTTP/1.1" 304 -',
        ("/static/",),
    )
    assert not should_quiet_access_log(
        '127.0.0.1 - - [05/Jul/2026 00:04:24] "POST /run-funnel HTTP/1.1" 200 -',
        ("/healthz",),
    )


def test_should_quiet_colored_health_response():
    line = (
        '127.0.0.1 - - [05/Jul/2026 00:15:45] '
        '"\x1b[31m\x1b[1mGET /health HTTP/1.1\x1b[0m" 401 -'
    )
    assert should_quiet_access_log(line, ("/health", "/healthz"))
    line = '127.0.0.1 - - [05/Jul/2026 00:04:24] "GET /health?probe=1 HTTP/1.1" 401 -'
    assert should_quiet_access_log(line, ("/health",))


def test_quiet_filter_blocks_record():
    record = logging.LogRecord(
        name="werkzeug",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='127.0.0.1 - - [05/Jul/2026 00:04:24] "GET /healthz HTTP/1.1" 200 -',
        args=(),
        exc_info=None,
    )
    assert QuietAccessLogFilter(("/healthz",)).filter(record) is False


def test_configure_adds_service_prefix(monkeypatch):
    monkeypatch.delenv("MK04_HTTP_ACCESS_LOG", raising=False)
    configure_quiet_http_access_logging(service_label="ops-ui")
    logger = logging.getLogger("werkzeug")
    assert logger.propagate is False
    assert logger.handlers
    formatter = logger.handlers[0].formatter
    assert formatter is not None
    record = logging.LogRecord(
        name="werkzeug",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='127.0.0.1 - - [05/Jul/2026 00:04:24] "POST /run-funnel HTTP/1.1" 200 -',
        args=(),
        exc_info=None,
    )
    assert formatter.format(record).startswith("[ops-ui] ")
