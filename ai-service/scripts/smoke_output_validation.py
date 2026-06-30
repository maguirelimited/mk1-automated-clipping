from __future__ import annotations

import json
import sys
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from output_validation import (  # noqa: E402
    INVALID_JSON_SCHEMA,
    JSON_PARSE_FAILED,
    JSON_SCHEMA_VALIDATION_FAILED,
    validate_model_output,
    validate_with_one_repair,
)
from versioned_assets import load_schema  # noqa: E402


class FakeModelResponse:
    def __init__(self, text: str | None, error: str | None = None):
        self.text = text
        self.error = error


class FakeRepairClient:
    def __init__(self, repaired_text: str):
        self.repaired_text = repaired_text
        self.calls = 0

    def generate(self, prompt: str) -> FakeModelResponse:
        assert "Return only valid JSON" in prompt
        self.calls += 1
        return FakeModelResponse(self.repaired_text)


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


def main() -> None:
    schema = load_schema("clip_candidates_v2")

    usable_true = {
        "usable": True,
        "confidence": 0.78,
        "reason": "Strong standalone candidate worth passing forward.",
        "candidates": [
            {
                "start_seconds": 130.0,
                "end_seconds": 190.0,
                "scores": _scores(8.1, hook_strength=9.0, natural_ending=7.0),
                "reason": "Clear and useful insight worth passing forward.",
            }
        ],
    }
    result = validate_model_output(json.dumps(usable_true), schema)
    assert result.ok, result.as_dict()

    score_out_of_range = {
        "usable": True,
        "confidence": 0.8,
        "reason": "Score component above the allowed maximum.",
        "candidates": [
            {
                "start_seconds": 130.0,
                "end_seconds": 190.0,
                "scores": _scores(8.0, hook_strength=11.0),
                "reason": "hook_strength score exceeds 10.",
            }
        ],
    }
    result = validate_model_output(json.dumps(score_out_of_range), schema)
    assert not result.ok and result.error_code == JSON_SCHEMA_VALIDATION_FAILED, result.as_dict()

    missing_component = {
        "usable": True,
        "confidence": 0.8,
        "reason": "Candidate missing a required score component.",
        "candidates": [
            {
                "start_seconds": 130.0,
                "end_seconds": 190.0,
                "scores": {
                    "hook_strength": 8.0,
                    "standalone_context": 8.0,
                    "insight_value": 8.0,
                    "retention_potential": 8.0,
                    "overall": 8.0,
                },
                "reason": "natural_ending is missing.",
            }
        ],
    }
    result = validate_model_output(json.dumps(missing_component), schema)
    assert not result.ok and result.error_code == JSON_SCHEMA_VALIDATION_FAILED, result.as_dict()

    extra_score_field = {
        "usable": True,
        "confidence": 0.8,
        "reason": "Candidate scores include an unexpected field.",
        "candidates": [
            {
                "start_seconds": 130.0,
                "end_seconds": 190.0,
                "scores": _scores(8.0, novelty=8.0),
                "reason": "novelty is not an allowed score component.",
            }
        ],
    }
    result = validate_model_output(json.dumps(extra_score_field), schema)
    assert not result.ok and result.error_code == JSON_SCHEMA_VALIDATION_FAILED, result.as_dict()

    usable_false = {
        "usable": False,
        "confidence": 0.31,
        "reason": "No strong standalone clip found.",
        "candidates": [],
    }
    result = validate_model_output(json.dumps(usable_false), schema)
    assert result.ok, result.as_dict()

    result = validate_model_output("This is not JSON.", schema)
    assert not result.ok and result.error_code == JSON_PARSE_FAILED, result.as_dict()

    schema_invalid_output = {
        "usable": True,
        "confidence": 1.4,
        "reason": "Confidence is out of range.",
        "candidates": [],
    }
    result = validate_model_output(json.dumps(schema_invalid_output), schema)
    assert not result.ok and result.error_code == JSON_SCHEMA_VALIDATION_FAILED, result.as_dict()

    markdown_wrapped = f"```json\n{json.dumps(usable_false)}\n```"
    result = validate_model_output(markdown_wrapped, schema)
    assert result.ok, result.as_dict()

    repair_client = FakeRepairClient(json.dumps(usable_false))
    result = validate_with_one_repair(
        raw_text="not json",
        schema=schema,
        model_client=repair_client,
    )
    assert result.ok, result.as_dict()
    assert repair_client.calls == 1

    invalid_schema = {"type": "definitely-not-a-json-schema-type"}
    result = validate_model_output(json.dumps(usable_false), invalid_schema)
    assert not result.ok and result.error_code == INVALID_JSON_SCHEMA, result.as_dict()

    print("output_validation_smoke_ok")


if __name__ == "__main__":
    main()
