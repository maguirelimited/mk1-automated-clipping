"""Tests for input_service.log_util."""

from __future__ import annotations

import logging

from input_service.log_util import configure_service_logging, detail, verbose_enabled


def test_verbose_disabled_by_default(monkeypatch):
    monkeypatch.delenv("INPUT_SERVICE_VERBOSE", raising=False)
    assert verbose_enabled() is False


def test_detail_logs_at_debug_when_not_verbose(monkeypatch, caplog):
    monkeypatch.setenv("INPUT_SERVICE_VERBOSE", "0")
    logger = logging.getLogger("input_service.test_detail")
    logger.setLevel(logging.DEBUG)
    with caplog.at_level(logging.DEBUG, logger="input_service.test_detail"):
        detail(logger, "hidden detail %s", "x")
    assert "hidden detail x" in caplog.text


def test_configure_service_logging_quiet_modules(monkeypatch):
    monkeypatch.setenv("INPUT_SERVICE_VERBOSE", "0")
    configure_service_logging()
    assert logging.getLogger("input_service.source_checker").level == logging.WARNING
