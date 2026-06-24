"""Tests for the built-in upload worker thread in ``output_funnel.app``.

The worker is the "set one config and forget" mechanism that periodically
calls ``upload_due_jobs`` so jobs whose upload window has opened are
uploaded without any external cron / n8n loop.
"""
from __future__ import annotations

import threading
import time

import pytest

from output_funnel import app as app_module


class _StopWorker(Exception):
    """Raised by test fake_sleep to break the worker loop cleanly.

    The production worker catches ``Exception`` on sleep failure, logs it,
    and exits the loop — so using a normal Exception subclass here means
    the thread terminates without raising up to the pytest threadexception
    handler.
    """


@pytest.fixture(autouse=True)
def _reset_worker_state():
    """Reset the module-level worker flag so tests are independent."""
    app_module._UPLOAD_WORKER_STARTED = False
    yield
    app_module._UPLOAD_WORKER_STARTED = False


def _drain_thread(thread: threading.Thread, *, stop_event: threading.Event, timeout: float = 1.0):
    stop_event.set()
    thread.join(timeout=timeout)


def test_worker_disabled_does_not_spawn_thread():
    settings = {"automation": {"upload_worker": {"enabled": False, "interval_seconds": 60}}}

    thread = app_module.start_upload_worker(settings=settings)

    assert thread is None
    assert app_module._UPLOAD_WORKER_STARTED is False


def test_secret_protects_non_health_endpoints(monkeypatch):
    monkeypatch.setenv("OUTPUT_FUNNEL_SECRET", "secret-1")
    monkeypatch.setenv("MK04_UPLOAD_MODE", "dry_run")
    monkeypatch.setattr(
        app_module,
        "_store",
        lambda: type("FakeStore", (), {"db_path": "/tmp/output-funnel-test.sqlite3"})(),
    )
    client = app_module.app.test_client()

    health = client.get("/healthz")
    assert health.status_code == 200

    denied = client.get("/queue")
    assert denied.status_code == 401

    allowed = client.get("/queue", headers={"X-Output-Funnel-Secret": "secret-1"})
    assert allowed.status_code in (200, 400, 500)
    assert allowed.status_code != 401


def test_worker_enabled_fires_upload_fn_repeatedly_on_its_interval():
    settings = {
        "automation": {
            "upload_worker": {"enabled": True, "interval_seconds": 60},
            "upload_limit": 3,
        }
    }
    call_count = {"n": 0}
    upload_calls_seen = threading.Event()
    stop_event = threading.Event()

    def fake_upload():
        call_count["n"] += 1
        if call_count["n"] >= 3:
            upload_calls_seen.set()
        return {"count": 0}

    def fake_sleep(_interval):
        if stop_event.is_set():
            raise _StopWorker("worker stop requested")

    thread = app_module.start_upload_worker(
        settings=settings, upload_fn=fake_upload, sleep_fn=fake_sleep
    )

    assert thread is not None
    assert upload_calls_seen.wait(timeout=2.0), (
        f"expected upload_fn to be called repeatedly; saw {call_count['n']} call(s)"
    )
    _drain_thread(thread, stop_event=stop_event)
    assert call_count["n"] >= 3


def test_worker_is_idempotent_when_called_twice():
    settings = {"automation": {"upload_worker": {"enabled": True, "interval_seconds": 60}}}
    stop_event = threading.Event()

    def fake_upload():
        return {"count": 0}

    def fake_sleep(_interval):
        if stop_event.is_set():
            raise _StopWorker("stop")
        time.sleep(0.01)

    first = app_module.start_upload_worker(
        settings=settings, upload_fn=fake_upload, sleep_fn=fake_sleep
    )
    second = app_module.start_upload_worker(
        settings=settings, upload_fn=fake_upload, sleep_fn=fake_sleep
    )

    assert first is not None
    assert second is None, "second start should be a no-op while worker is running"
    _drain_thread(first, stop_event=stop_event)


def test_worker_continues_when_upload_fn_raises():
    settings = {"automation": {"upload_worker": {"enabled": True, "interval_seconds": 60}}}
    call_count = {"n": 0}
    saw_three_calls = threading.Event()
    stop_event = threading.Event()

    def fake_upload():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated transient failure")
        if call_count["n"] >= 3:
            saw_three_calls.set()
        return {"count": 0}

    def fake_sleep(_interval):
        if stop_event.is_set():
            raise _StopWorker("stop")

    thread = app_module.start_upload_worker(
        settings=settings, upload_fn=fake_upload, sleep_fn=fake_sleep
    )

    assert thread is not None
    assert saw_three_calls.wait(timeout=2.0), (
        f"worker should survive an exception in upload_fn; saw {call_count['n']} call(s)"
    )
    _drain_thread(thread, stop_event=stop_event)


def test_resolve_config_picks_nested_automation_block():
    settings = {
        "automation": {
            "upload_worker": {"enabled": True, "interval_seconds": 30},
            "upload_limit": 5,
        }
    }

    cfg = app_module._resolve_upload_worker_config(settings)

    assert cfg["enabled"] is True
    assert cfg["interval_seconds"] == 30
    assert cfg["limit"] == 5


def test_resolve_config_clamps_interval_to_minimum():
    settings = {"automation": {"upload_worker": {"enabled": True, "interval_seconds": 2}}}

    cfg = app_module._resolve_upload_worker_config(settings)

    assert cfg["interval_seconds"] == app_module._UPLOAD_WORKER_MIN_INTERVAL_SEC


def test_resolve_config_env_override_enabled(monkeypatch):
    monkeypatch.setenv("OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED", "1")
    monkeypatch.setenv("OUTPUT_FUNNEL_UPLOAD_WORKER_INTERVAL", "45")
    monkeypatch.setenv("OUTPUT_FUNNEL_AUTO_UPLOAD_LIMIT", "7")
    settings = {"automation": {"upload_worker": {"enabled": False, "interval_seconds": 60}}}

    cfg = app_module._resolve_upload_worker_config(settings)

    assert cfg["enabled"] is True
    assert cfg["interval_seconds"] == 45
    assert cfg["limit"] == 7


def test_resolve_config_env_override_disable(monkeypatch):
    monkeypatch.setenv("OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED", "0")
    settings = {"automation": {"upload_worker": {"enabled": True, "interval_seconds": 60}}}

    cfg = app_module._resolve_upload_worker_config(settings)

    assert cfg["enabled"] is False
