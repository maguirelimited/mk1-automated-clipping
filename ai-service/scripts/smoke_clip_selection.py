from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

import app as app_module  # noqa: E402
from config import load_settings  # noqa: E402
from decision_logging import DecisionLogger  # noqa: E402
from task_router import AITaskError  # noqa: E402
from tasks import clip_selection  # noqa: E402
from tasks.clip_selection import run_clip_selection  # noqa: E402
from versioned_assets import load_prompt, load_schema  # noqa: E402


class FakeModelResponse:
    def __init__(self, text: str | None = None, error: str | None = None):
        self.text = text
        self.error = error


class QueueModelClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> FakeModelResponse:
        self.prompts.append(prompt)
        if not self.responses:
            return FakeModelResponse(error="no fake response queued")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeOllamaFactory:
    def __init__(self, client: QueueModelClient):
        self.client = client

    def __call__(self, settings):
        del settings
        return self.client


def _scores(overall, **overrides):
    components = {
        "hook_strength": overall,
        "standalone_context": overall,
        "insight_value": overall,
        "retention_potential": overall,
        "natural_ending": overall,
        "overall": overall,
    }
    components.update(overrides)
    return components


def _section_output(candidates, *, usable=True):
    return FakeModelResponse(
        json.dumps(
            {
                "usable": usable,
                "confidence": 0.8 if usable else 0.2,
                "reason": "section result",
                "candidates": candidates,
            }
        )
    )


def _usable_false_output():
    return _section_output([], usable=False)


def _payload(*, segments=True, final_candidate_cap=8):
    source = {
        "job_id": "job_123",
        "video_title": "Example Podcast Episode",
        "source_channel": "Example Channel",
        "funnel_id": "business_ai",
        "duration_seconds": 130,
        "transcript": "Full fallback transcript.",
        "previous_context_summary": "",
        "funnel_rules": {
            "target_audience": "business/productivity audience",
            "preferred_clip_length_seconds": [35, 75],
            "avoid": ["inside jokes"],
        },
        "chunking_options": {
            "section_size_seconds": 60,
            "section_overlap_seconds": 20,
            "candidate_cap_per_section": 2,
            "final_candidate_cap": final_candidate_cap,
        },
    }
    if segments:
        source["segments"] = [
            {"start": 0.0, "end": 10.0, "text": "Opening segment."},
            {"start": 45.0, "end": 55.0, "text": "First useful point."},
            {"start": 90.0, "end": 100.0, "text": "Second useful point."},
        ]
    return {
        "task_type": "clip_selection",
        "job_id": "job_123",
        "funnel_id": "business_ai",
        "input": source,
        "prompt_version": "clip_selection_v2",
        "schema_version": "clip_candidates_v2",
        "model_preference": "local_default",
    }


def _run(payload, client):
    return run_clip_selection(
        payload=payload,
        settings=load_settings(),
        prompt_text=load_prompt("clip_selection_v2"),
        schema=load_schema("clip_candidates_v2"),
        model_client=client,
    )


def main() -> None:
    many_candidates = [
        {
            "start_seconds": float(i),
            "end_seconds": float(i + 40),
            "scores": _scores(float(i % 10)),
            "reason": f"candidate {i}",
        }
        for i in range(1, 11)
    ]
    invalid_candidates = [
        {"start_seconds": -1.0, "end_seconds": 30.0, "scores": _scores(9.9), "reason": "negative start"},
        {"start_seconds": 20.0, "end_seconds": 10.0, "scores": _scores(9.8), "reason": "bad order"},
        {"start_seconds": 20.0, "end_seconds": 200.0, "scores": _scores(9.7), "reason": "past duration"},
        {"start_seconds": 20.0, "end_seconds": 50.0, "scores": _scores(8.0), "reason": ""},
    ]
    client = QueueModelClient(
        [
            _section_output(many_candidates),
            _usable_false_output(),
            _section_output(invalid_candidates),
        ]
    )
    result = _run(_payload(final_candidate_cap=5), client)
    assert result["usable"] is True, result
    assert len(result["candidates"]) == 5, result
    for candidate in result["candidates"]:
        assert set(candidate["scores"]) == {
            "hook_strength",
            "standalone_context",
            "insight_value",
            "retention_potential",
            "natural_ending",
            "overall",
        }, result
    overalls = [candidate["scores"]["overall"] for candidate in result["candidates"]]
    assert overalls == sorted(overalls, reverse=True), result
    assert all(candidate["start_seconds"] >= 0 for candidate in result["candidates"])
    assert client.prompts and "Return at most 2 candidate" in client.prompts[0]

    client = QueueModelClient([_usable_false_output(), _usable_false_output(), _usable_false_output()])
    result = _run(_payload(), client)
    assert result == {
        "usable": False,
        "confidence": 0.0,
        "reason": "No valid clip candidates found across transcript sections.",
        "candidates": [],
    }

    client = QueueModelClient([FakeModelResponse(error="backend offline"), FakeModelResponse(error="backend offline"), FakeModelResponse(error="backend offline")])
    try:
        _run(_payload(), client)
        raise AssertionError("expected model failure")
    except AITaskError as exc:
        assert exc.code == "MODEL_CALL_FAILED"

    repaired_candidate = {
        "start_seconds": 10.0,
        "end_seconds": 50.0,
        "scores": _scores(8.5),
        "reason": "repaired candidate",
    }
    client = QueueModelClient([FakeModelResponse("not json"), _section_output([repaired_candidate])])
    result = _run(_payload(segments=False), client)
    assert result["usable"] is True, result
    assert result["candidates"][0]["reason"] == "repaired candidate"
    assert len(client.prompts) == 2
    assert "Repair the supplied invalid output" in client.prompts[1]

    flask_client = app_module.app.test_client()
    with tempfile.TemporaryDirectory() as tmp:
        fake_client = QueueModelClient([_section_output([repaired_candidate])])
        original_factory = clip_selection.OllamaModelClient
        original_logger = app_module.DECISION_LOGGER
        try:
            clip_selection.OllamaModelClient = FakeOllamaFactory(fake_client)
            app_module.DECISION_LOGGER = DecisionLogger(
                log_path=Path(tmp) / "ai_decisions.jsonl",
                artifact_dir=Path(tmp) / "artifacts",
            )
            response = flask_client.post("/ai/run", json=_payload(segments=False))
        finally:
            clip_selection.OllamaModelClient = original_factory
            app_module.DECISION_LOGGER = original_logger
        body = response.get_json()
        assert response.status_code == 200, body
        assert body["status"] == "ok", body
        assert body["result"]["usable"] is True, body
        assert body["output_hash"], body
        log_path = Path(tmp) / "ai_decisions.jsonl"
        assert log_path.is_file()
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        log_entry = json.loads(lines[0])
        assert log_entry["request_id"] == body["request_id"]
        assert Path(log_entry["input_artifact_path"]).is_file()
        assert Path(log_entry["output_artifact_path"]).is_file()

    with tempfile.TemporaryDirectory() as tmp:
        original_logger = app_module.DECISION_LOGGER
        try:
            app_module.DECISION_LOGGER = DecisionLogger(
                log_path=Path(tmp) / "ai_decisions.jsonl",
                artifact_dir=Path(tmp) / "artifacts",
            )
            response = flask_client.post(
                "/ai/run",
                json={**_payload(segments=False), "task_type": "quality_inspection"},
            )
        finally:
            app_module.DECISION_LOGGER = original_logger
    body = response.get_json()
    assert response.status_code == 501, body
    assert body["error"]["code"] == "TASK_NOT_IMPLEMENTED", body

    print("clip_selection_smoke_ok")


if __name__ == "__main__":
    main()
