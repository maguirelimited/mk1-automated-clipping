from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

import app as app_module  # noqa: E402
from decision_logging import DecisionLogResult, DecisionLogger  # noqa: E402


class FailingLogger:
    def write(self, *, request_payload, response_payload):
        del request_payload, response_payload
        return DecisionLogResult(
            ok=False,
            input_artifact_path=None,
            output_artifact_path=None,
            warning={
                "code": "DECISION_LOG_WRITE_FAILED",
                "message": "AI result returned but decision log could not be written.",
            },
        )


def main() -> None:
    long_transcript = "This transcript should live in the artifact only. " * 40
    request_payload = {
        "task_type": "clip_selection",
        "job_id": "job_123",
        "funnel_id": "business_ai",
        "input": {
            "job_id": "job_123",
            "duration_seconds": 120,
            "transcript": long_transcript,
        },
        "prompt_version": "clip_selection_v1",
        "schema_version": "clip_candidates_v1",
    }
    response_payload = {
        "request_id": "request.1",
        "job_id": "job_123",
        "task_type": "clip_selection",
        "funnel_id": "business_ai",
        "input_hash": "sha256:input",
        "output_hash": "sha256:output",
        "model_used": "qwen2.5:14b-instruct",
        "provider": "ollama",
        "prompt_version": "clip_selection_v1",
        "schema_version": "clip_candidates_v1",
        "status": "ok",
        "result": {
            "usable": False,
            "confidence": 0.0,
            "reason": "No valid clip candidates found.",
            "candidates": [],
        },
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        logger = DecisionLogger(
            log_path=tmp_path / "ai_decisions.jsonl",
            artifact_dir=tmp_path / "artifacts",
            preview_string_limit=40,
        )
        result = logger.write(request_payload=request_payload, response_payload=response_payload)
        assert result.ok, result.warning
        assert result.input_artifact_path
        assert result.output_artifact_path

        input_artifact = Path(result.input_artifact_path)
        output_artifact = Path(result.output_artifact_path)
        assert input_artifact.is_file()
        assert output_artifact.is_file()
        assert long_transcript in input_artifact.read_text(encoding="utf-8")
        assert response_payload["result"]["reason"] in output_artifact.read_text(encoding="utf-8")

        lines = (tmp_path / "ai_decisions.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        line = json.loads(lines[0])
        for key in (
            "request_id",
            "job_id",
            "task_type",
            "funnel_id",
            "input_hash",
            "output_hash",
            "model_used",
            "provider",
            "prompt_version",
            "schema_version",
            "timestamp",
            "status",
            "input_preview",
            "output_preview",
            "input_artifact_path",
            "output_artifact_path",
        ):
            assert key in line, key
        assert line["ai_result"] is not None
        assert line["final_decision"] is None
        assert line["performance"] is None
        line_text = json.dumps(line, sort_keys=True)
        assert long_transcript not in line_text
        assert "type" in line["input_preview"]["input"]["transcript"]
        assert line["input_preview"]["input"]["transcript"]["type"] == "transcript"
        assert line["input_preview"]["input"]["transcript"]["length"] == len(long_transcript)

    original_logger = app_module.DECISION_LOGGER
    try:
        app_module.DECISION_LOGGER = FailingLogger()
        with app_module.app.app_context():
            response, status_code = app_module._jsonify_logged(
                {"task_type": "clip_selection"},
                {"request_id": "request.2", "status": "ok", "result": {}},
                200,
            )
            payload = response.get_json()
        assert status_code == 200
        assert payload["warnings"][0]["code"] == "DECISION_LOG_WRITE_FAILED"
    finally:
        app_module.DECISION_LOGGER = original_logger

    print("decision_logging_smoke_ok")


if __name__ == "__main__":
    main()
