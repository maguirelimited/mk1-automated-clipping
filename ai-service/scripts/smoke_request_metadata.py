from __future__ import annotations

import sys
import tempfile
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

import app as app_module  # noqa: E402
from config import load_settings  # noqa: E402
from decision_logging import DecisionLogger  # noqa: E402
from request_metadata import generate_input_hash  # noqa: E402


def main() -> None:
    settings = load_settings()
    base_payload = {
        "task_type": "clip_selection",
        "job_id": "job_123",
        "funnel_id": "business_ai",
        "input": {"b": 2, "a": 1},
        "prompt_version": "clip_selection_v1",
        "schema_version": "clip_candidates_v1",
        "model_preference": "local_default",
    }
    reordered_payload = {
        "schema_version": "clip_candidates_v1",
        "prompt_version": "clip_selection_v1",
        "input": {"a": 1, "b": 2},
        "funnel_id": "business_ai",
        "job_id": "job_123",
        "model_preference": "local_default",
        "task_type": "clip_selection",
    }
    changed_payload = {
        **base_payload,
        "input": {"a": 1, "b": 3},
    }

    base_hash = generate_input_hash(
        base_payload,
        model_configured=settings.model,
        provider=settings.provider,
    )
    reordered_hash = generate_input_hash(
        reordered_payload,
        model_configured=settings.model,
        provider=settings.provider,
    )
    changed_hash = generate_input_hash(
        changed_payload,
        model_configured=settings.model,
        provider=settings.provider,
    )
    assert base_hash == reordered_hash
    assert base_hash != changed_hash

    client = app_module.app.test_client()

    unimplemented_payload = {**base_payload, "task_type": "quality_inspection"}
    unimplemented_hash = generate_input_hash(
        unimplemented_payload,
        model_configured=settings.model,
        provider=settings.provider,
    )

    with tempfile.TemporaryDirectory() as tmp:
        original_logger = app_module.DECISION_LOGGER
        try:
            app_module.DECISION_LOGGER = DecisionLogger(
                log_path=Path(tmp) / "ai_decisions.jsonl",
                artifact_dir=Path(tmp) / "artifacts",
            )
            response = client.post("/ai/run", json=unimplemented_payload)
        finally:
            app_module.DECISION_LOGGER = original_logger
        body = response.get_json()
        assert response.status_code == 501, body
        assert body["request_id"]
        assert body["input_hash"] == unimplemented_hash
        assert body["output_hash"] is None
        assert body["reusable_result_key"]["input_hash"] == unimplemented_hash
        assert body["reusable_result_key"]["task_type"] == "quality_inspection"
        assert body["error"]["code"] == "TASK_NOT_IMPLEMENTED"

    supplied_request_id = "job_123.quality_inspection.1"
    with tempfile.TemporaryDirectory() as tmp:
        original_logger = app_module.DECISION_LOGGER
        try:
            app_module.DECISION_LOGGER = DecisionLogger(
                log_path=Path(tmp) / "ai_decisions.jsonl",
                artifact_dir=Path(tmp) / "artifacts",
            )
            response = client.post("/ai/run", json={**unimplemented_payload, "request_id": supplied_request_id})
        finally:
            app_module.DECISION_LOGGER = original_logger
    body = response.get_json()
    assert response.status_code == 501, body
    assert body["request_id"] == supplied_request_id
    assert body["input_hash"] == unimplemented_hash

    response = client.post("/ai/run", json={**unimplemented_payload, "request_id": "../unsafe"})
    body = response.get_json()
    assert response.status_code == 400, body
    assert body["error"]["code"] == "INVALID_REQUEST_ID"
    assert body["input_hash"] is None
    assert body["reusable_result_key"] is None

    print("request_metadata_smoke_ok")


if __name__ == "__main__":
    main()
