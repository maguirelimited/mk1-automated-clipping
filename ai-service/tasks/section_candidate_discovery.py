from __future__ import annotations

import json
from typing import Any

from model_client import OllamaModelClient
from output_validation import validate_model_output, validate_with_one_repair
from task_router import AITaskError


def run_section_candidate_discovery(
    *,
    payload: dict[str, Any],
    settings: Any,
    prompt_text: str,
    schema: dict[str, Any],
    model_client: Any | None = None,
) -> dict[str, Any]:
    task_input = payload.get("input")
    if not isinstance(task_input, dict):
        raise AITaskError(
            "INVALID_TASK_INPUT",
            "section_candidate_discovery input must be an object.",
            status_code=400,
        )
    section = task_input.get("section")
    if not isinstance(section, dict):
        raise AITaskError(
            "INVALID_TASK_INPUT",
            "section_candidate_discovery input.section must be an object.",
            status_code=400,
        )

    try:
        client = model_client or OllamaModelClient(settings)
    except Exception as exc:
        raise AITaskError(
            "MODEL_CALL_FAILED",
            f"Could not initialise model client: {exc}",
            status_code=502,
        ) from exc

    prompt = build_section_candidate_discovery_prompt(
        prompt_text=prompt_text,
        section=section,
        config=task_input.get("config") if isinstance(task_input.get("config"), dict) else {},
    )
    try:
        response = client.generate(prompt)
    except Exception as exc:
        raise AITaskError("MODEL_CALL_FAILED", f"Model call failed: {exc}", status_code=502) from exc
    if getattr(response, "error", None):
        raise AITaskError(
            "MODEL_CALL_FAILED",
            f"Model call failed: {getattr(response, 'error')}",
            status_code=502,
        )

    raw_text = getattr(response, "text", None)
    validation = validate_model_output(raw_text, schema)
    if not validation.ok:
        try:
            validation = validate_with_one_repair(
                raw_text=raw_text,
                schema=schema,
                model_client=client,
            )
        except Exception as exc:
            raise AITaskError(
                "MODEL_OUTPUT_INVALID",
                f"Model output repair failed: {exc}",
                status_code=502,
            ) from exc
    if not validation.ok or not isinstance(validation.parsed_output, dict):
        raise AITaskError(
            validation.error_code or "MODEL_OUTPUT_INVALID",
            validation.error_message or "Model output did not match section discovery schema.",
            status_code=502,
        )
    return validation.parsed_output


def build_section_candidate_discovery_prompt(
    *,
    prompt_text: str,
    section: dict[str, Any],
    config: dict[str, Any],
) -> str:
    context = {
        "section": section,
        "config": config,
    }
    return "\n\n".join(
        [
            prompt_text.strip(),
            "REQUEST CONTEXT - JSON:",
            json.dumps(context, indent=2, sort_keys=True),
            "Return the JSON object only.",
        ]
    )
