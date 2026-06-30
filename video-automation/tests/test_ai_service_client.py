from __future__ import annotations

import json
import os
import sys

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import ai_service_client as client  # noqa: E402


def _transport(status_code: int, body):
    text = body if isinstance(body, str) else json.dumps(body)

    def _send(url, envelope, timeout):
        del url, envelope, timeout
        return status_code, text

    return _send


def _input():
    return {
        "job_id": "job_123",
        "duration_seconds": 60,
        "transcript": "A short transcript.",
        "segments": [{"start": 0.0, "end": 10.0, "text": "Hello."}],
    }


def _call(transport):
    return client.request_clip_selection(
        job_id="job_123",
        task_input=_input(),
        funnel_id="business_ai",
        transport=transport,
    )


def test_build_request_envelope_has_expected_fields():
    envelope = client.build_clip_selection_request(
        job_id="job_123",
        task_input=_input(),
        funnel_id="business_ai",
    )
    assert envelope["task_type"] == "clip_selection"
    assert envelope["job_id"] == "job_123"
    assert envelope["funnel_id"] == "business_ai"
    assert envelope["prompt_version"] == "clip_selection_v2"
    assert envelope["schema_version"] == "clip_candidates_v2"
    assert isinstance(envelope["input"], dict)


def test_build_request_rejects_empty_job_id():
    with pytest.raises(client.AiServiceConfigError):
        client.build_clip_selection_request(job_id="  ", task_input=_input())


def test_usable_true_response_is_accepted():
    candidates = [
        {
            "start_seconds": 10.0,
            "end_seconds": 50.0,
            "scores": {
                "hook_strength": 8.0,
                "standalone_context": 8.0,
                "insight_value": 8.0,
                "retention_potential": 8.0,
                "natural_ending": 8.0,
                "overall": 8.0,
            },
            "reason": "strong",
        }
    ]
    result = _call(
        _transport(
            200,
            {
                "status": "ok",
                "request_id": "req-1",
                "result": {"usable": True, "candidates": candidates},
            },
        )
    )
    assert result.outcome == client.OUTCOME_USABLE
    assert result.usable is True
    assert result.retryable is False
    assert result.candidates == candidates
    assert result.request_id == "req-1"


def test_usable_false_response_is_no_clip():
    result = _call(
        _transport(
            200,
            {"status": "ok", "result": {"usable": False, "candidates": []}},
        )
    )
    assert result.outcome == client.OUTCOME_NO_CLIP
    assert result.no_clip is True
    assert result.retryable is False
    assert result.candidates == []


def test_usable_true_without_candidates_is_no_clip():
    result = _call(
        _transport(200, {"status": "ok", "result": {"usable": True, "candidates": []}})
    )
    assert result.outcome == client.OUTCOME_NO_CLIP


def test_ai_busy_503_is_retryable():
    result = _call(
        _transport(
            503,
            {"status": "error", "error": {"code": "AI_BUSY", "message": "busy"}},
        )
    )
    assert result.outcome == client.OUTCOME_BUSY
    assert result.busy is True
    assert result.retryable is True
    assert result.error_code == "AI_BUSY"


def test_connection_refused_is_controlled_failure():
    def _refuse(url, envelope, timeout):
        del url, envelope, timeout
        raise client._TransportError("AI_SERVICE_UNREACHABLE", "connection refused")

    result = client.request_clip_selection(
        job_id="job_123", task_input=_input(), transport=_refuse
    )
    assert result.outcome == client.OUTCOME_AI_FAILURE
    assert result.ai_failure is True
    assert result.retryable is False
    assert result.error_code == "AI_SERVICE_UNREACHABLE"


def test_non_json_response_is_controlled_failure():
    result = _call(_transport(200, "<html>not json</html>"))
    assert result.outcome == client.OUTCOME_AI_FAILURE
    assert result.error_code == "AI_SERVICE_NON_JSON"


def test_4xx_task_error_is_controlled_failure():
    result = _call(
        _transport(
            400,
            {"status": "error", "error": {"code": "INVALID_REQUEST", "message": "bad"}},
        )
    )
    assert result.outcome == client.OUTCOME_AI_FAILURE
    assert result.retryable is False
    assert result.error_code == "INVALID_REQUEST"


def test_5xx_model_failure_is_controlled_failure():
    result = _call(
        _transport(
            502,
            {"status": "error", "error": {"code": "MODEL_CALL_FAILED", "message": "down"}},
        )
    )
    assert result.outcome == client.OUTCOME_AI_FAILURE
    assert result.error_code == "MODEL_CALL_FAILED"


def test_build_ai_service_input_omits_out_of_range_final_cap(tmp_path):
    # ai-service rejects final_candidate_cap outside 5..10. The built input must
    # not derive it from max_clips (which can be < 5, e.g. selection example uses 3).
    import select_clip

    transcript = tmp_path / "t.json"
    transcript.write_text(
        json.dumps(
            {
                "full_text": "hello world",
                "segments": [
                    {"start": 0.0, "end": 5.0, "text": "hello"},
                    {"start": 5.0, "end": 10.0, "text": "world"},
                ],
                "duration": 10.0,
            }
        ),
        encoding="utf-8",
    )
    task_input = select_clip._build_ai_service_input(
        str(transcript),
        {"min_duration_sec": 5, "max_duration_sec": 30, "max_clips": 3},
        job_id="job_1",
        duration_seconds=10.0,
    )
    assert "final_candidate_cap" not in task_input["chunking_options"]
    assert task_input["chunking_options"]["preferred_clip_length_seconds"] == [5.0, 30.0]
    assert task_input["segments"]
    assert task_input["duration_seconds"] == 10.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
