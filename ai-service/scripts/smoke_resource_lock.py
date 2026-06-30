from __future__ import annotations

import sys
import tempfile
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

import app as app_module  # noqa: E402
from decision_logging import DecisionLogger  # noqa: E402
from resource_lock import (  # noqa: E402
    LocalModelResourceLock,
    MODEL_RESOURCE_LOCK,
    ResourceBusyError,
)


class FakeModelResponse:
    def __init__(self, text=None, error=None, model_used="fake-model", provider="ollama"):
        self.text = text
        self.error = error
        self.model_used = model_used
        self.provider = provider


class FakeReachableClient:
    """Stand-in for OllamaModelClient that reports a healthy reachable backend."""

    def __init__(self, settings=None):
        del settings

    def backend_status(self):
        return {"backend_reachable": True, "model_available": True, "error": None}

    def generate(self, prompt):
        del prompt
        return FakeModelResponse(text='{"status":"model_ok"}')


def _clip_payload():
    return {
        "task_type": "clip_selection",
        "job_id": "job_lock_test",
        "funnel_id": "business_ai",
        "input": {
            "job_id": "job_lock_test",
            "duration_seconds": 60,
            "transcript": "A short transcript for the lock smoke test.",
            "segments": [{"start": 0.0, "end": 10.0, "text": "Hello world."}],
        },
        "prompt_version": "clip_selection_v1",
        "schema_version": "clip_candidates_v1",
    }


def _test_lock_unit():
    lock = LocalModelResourceLock()
    assert lock.is_held() is False
    assert lock.try_acquire() is True
    assert lock.is_held() is True
    assert lock.try_acquire() is False  # already held, non-blocking
    lock.release()
    assert lock.is_held() is False

    # guard() raises AI_BUSY while held, and releases afterward.
    assert lock.try_acquire() is True
    try:
        with lock.guard():
            raise AssertionError("guard should have raised ResourceBusyError")
    except ResourceBusyError as exc:
        assert exc.code == "AI_BUSY"
        assert exc.status_code == 503
    lock.release()
    assert lock.is_held() is False


def _test_lock_releases_after_exception():
    lock = LocalModelResourceLock()
    try:
        with lock.guard():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert lock.is_held() is False, "lock must release on unexpected exception"


def _test_health_does_not_require_lock(flask_client):
    assert MODEL_RESOURCE_LOCK.try_acquire() is True
    try:
        response = flask_client.get("/health")
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["service"] == "ok"
    finally:
        MODEL_RESOURCE_LOCK.release()


def _test_heavy_task_busy_while_locked(flask_client):
    with tempfile.TemporaryDirectory() as tmp:
        original_logger = app_module.DECISION_LOGGER
        app_module.DECISION_LOGGER = DecisionLogger(
            log_path=Path(tmp) / "ai_decisions.jsonl",
            artifact_dir=Path(tmp) / "artifacts",
        )
        assert MODEL_RESOURCE_LOCK.try_acquire() is True
        try:
            response = flask_client.post("/ai/run", json=_clip_payload())
        finally:
            MODEL_RESOURCE_LOCK.release()
            app_module.DECISION_LOGGER = original_logger
        body = response.get_json()
        assert response.status_code == 503, body
        assert body["status"] == "error", body
        assert body["error"]["code"] == "AI_BUSY", body
        # AI_BUSY responses are logged after envelope validation where possible.
        log_path = Path(tmp) / "ai_decisions.jsonl"
        assert log_path.is_file(), "AI_BUSY response should still be logged"
    # Lock is free again after the busy response.
    assert MODEL_RESOURCE_LOCK.is_held() is False


def _test_diagnostics_respects_lock(flask_client):
    original_client = app_module.OllamaModelClient
    app_module.OllamaModelClient = FakeReachableClient
    try:
        # Lock free -> diagnostics runs the (faked) generation.
        ok_response = flask_client.get("/diagnostics/model")
        assert ok_response.status_code == 200, ok_response.get_json()
        assert ok_response.get_json()["status"] == "ok"

        # Lock held -> diagnostics returns AI_BUSY without generating.
        assert MODEL_RESOURCE_LOCK.try_acquire() is True
        try:
            busy_response = flask_client.get("/diagnostics/model")
        finally:
            MODEL_RESOURCE_LOCK.release()
        busy_body = busy_response.get_json()
        assert busy_response.status_code == 503, busy_body
        assert busy_body["error"]["code"] == "AI_BUSY", busy_body
    finally:
        app_module.OllamaModelClient = original_client


def main() -> None:
    flask_client = app_module.app.test_client()
    _test_lock_unit()
    _test_lock_releases_after_exception()
    _test_health_does_not_require_lock(flask_client)
    _test_heavy_task_busy_while_locked(flask_client)
    _test_diagnostics_respects_lock(flask_client)
    # Global lock must be free at the end so other endpoints are unaffected.
    assert MODEL_RESOURCE_LOCK.is_held() is False
    print("resource_lock_smoke_ok")


if __name__ == "__main__":
    main()
