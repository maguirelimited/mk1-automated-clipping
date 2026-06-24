"""Tests for the background plan worker in ``output_funnel.app``.

Parallels ``test_upload_worker.py``. The plan worker exists so a transient
handoff failure between video-automation and output-funnel does not strand
clips in ``registered`` / ``routed``.
"""
from __future__ import annotations

import threading

import pytest

from output_funnel import app as app_module


class _StopWorker(Exception):
    """Break the worker loop cleanly from inside fake_sleep."""


@pytest.fixture(autouse=True)
def _reset_worker_state():
    app_module._PLAN_WORKER_STARTED = False
    yield
    app_module._PLAN_WORKER_STARTED = False


def _drain_thread(thread: threading.Thread, *, stop_event: threading.Event, timeout: float = 1.0):
    stop_event.set()
    thread.join(timeout=timeout)


def test_plan_worker_disabled_does_not_spawn_thread():
    settings = {"automation": {"plan_worker": {"enabled": False, "interval_seconds": 300}}}

    thread = app_module.start_plan_worker(settings=settings)

    assert thread is None
    assert app_module._PLAN_WORKER_STARTED is False


def test_plan_worker_enabled_fires_plan_fn_repeatedly():
    settings = {
        "automation": {
            "plan_worker": {"enabled": True, "interval_seconds": 60},
            "schedule_limit": 7,
        }
    }
    call_count = {"n": 0}
    seen_three = threading.Event()
    stop_event = threading.Event()

    def fake_plan():
        call_count["n"] += 1
        if call_count["n"] >= 3:
            seen_three.set()
        return {"count": 0}

    def fake_sleep(_interval):
        if stop_event.is_set():
            raise _StopWorker("stop")

    thread = app_module.start_plan_worker(
        settings=settings, plan_fn=fake_plan, sleep_fn=fake_sleep
    )

    assert thread is not None
    assert seen_three.wait(timeout=2.0), (
        f"expected plan_fn to be called repeatedly; saw {call_count['n']} call(s)"
    )
    _drain_thread(thread, stop_event=stop_event)
    assert call_count["n"] >= 3


def test_plan_worker_idempotent_when_called_twice():
    settings = {"automation": {"plan_worker": {"enabled": True, "interval_seconds": 60}}}
    stop_event = threading.Event()

    def fake_plan():
        return {"count": 0}

    def fake_sleep(_interval):
        if stop_event.is_set():
            raise _StopWorker("stop")

    first = app_module.start_plan_worker(
        settings=settings, plan_fn=fake_plan, sleep_fn=fake_sleep
    )
    second = app_module.start_plan_worker(
        settings=settings, plan_fn=fake_plan, sleep_fn=fake_sleep
    )

    assert first is not None
    assert second is None
    _drain_thread(first, stop_event=stop_event)


def test_plan_worker_continues_when_plan_fn_raises():
    settings = {"automation": {"plan_worker": {"enabled": True, "interval_seconds": 60}}}
    call_count = {"n": 0}
    seen_three = threading.Event()
    stop_event = threading.Event()

    def fake_plan():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated transient failure")
        if call_count["n"] >= 3:
            seen_three.set()
        return {"count": 0}

    def fake_sleep(_interval):
        if stop_event.is_set():
            raise _StopWorker("stop")

    thread = app_module.start_plan_worker(
        settings=settings, plan_fn=fake_plan, sleep_fn=fake_sleep
    )

    assert thread is not None
    assert seen_three.wait(timeout=2.0), (
        f"worker should survive an exception in plan_fn; saw {call_count['n']} call(s)"
    )
    _drain_thread(thread, stop_event=stop_event)


def test_resolve_plan_config_clamps_interval_to_minimum():
    settings = {"automation": {"plan_worker": {"enabled": True, "interval_seconds": 5}}}

    cfg = app_module._resolve_plan_worker_config(settings)

    assert cfg["interval_seconds"] == app_module._PLAN_WORKER_MIN_INTERVAL_SEC


def test_resolve_plan_config_env_override(monkeypatch):
    monkeypatch.setenv("OUTPUT_FUNNEL_PLAN_WORKER_ENABLED", "1")
    monkeypatch.setenv("OUTPUT_FUNNEL_PLAN_WORKER_INTERVAL", "120")
    monkeypatch.setenv("OUTPUT_FUNNEL_AUTO_SCHEDULE_LIMIT", "9")
    settings = {"automation": {"plan_worker": {"enabled": False, "interval_seconds": 300}}}

    cfg = app_module._resolve_plan_worker_config(settings)

    assert cfg["enabled"] is True
    assert cfg["interval_seconds"] == 120
    assert cfg["limit"] == 9
