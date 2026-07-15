from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from funnel_rule_registry import get_funnel_rule_aliases, resolve_rules_version
from task_router import AITaskError

BASE_PROMPT_VERSION = "section_candidate_discovery_base_v1"
DEFAULT_FUNNEL_ID = "business"

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
FUNNEL_RULES_DIR = PROMPTS_DIR / "funnel_rules"


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
        if model_client is None:
            from model_client import OllamaModelClient

            client = OllamaModelClient(settings)
        else:
            client = model_client
    except Exception as exc:
        raise AITaskError(
            "MODEL_CALL_FAILED",
            f"Could not initialise model client: {exc}",
            status_code=502,
        ) from exc

    prompt_metadata = resolve_prompt_metadata(payload)
    prompt = build_section_candidate_discovery_prompt(
        prompt_text=prompt_text,
        section=section,
        config=task_input.get("config") if isinstance(task_input.get("config"), dict) else {},
        prompt_metadata=prompt_metadata,
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

    from output_validation import validate_model_output, validate_with_one_repair

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

    from section_discovery_semantics import normalize_section_discovery_result

    config = task_input.get("config") if isinstance(task_input.get("config"), dict) else {}
    result = normalize_section_discovery_result(
        parsed=dict(validation.parsed_output),
        section=section,
        config=config,
        job_id=str(payload.get("job_id") or ""),
        schema=schema,
    )
    result["prompt_metadata"] = prompt_metadata
    return result


def build_section_candidate_discovery_prompt(
    *,
    prompt_text: str,
    section: dict[str, Any],
    config: dict[str, Any],
    prompt_metadata: dict[str, Any],
) -> str:
    context = {
        "section": section,
        "config": config,
        "prompt_metadata": prompt_metadata,
    }
    return "\n\n".join(
        [
            prompt_text.strip(),
            "RESOLVED FUNNEL JUDGEMENT RULES:",
            _load_funnel_rules_text(str(prompt_metadata["funnel_rules_version"])),
            "REQUEST CONTEXT - JSON:",
            json.dumps(context, indent=2, sort_keys=True),
            "Return the JSON object only.",
        ]
    )


def resolve_prompt_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    task_input = payload.get("input")
    task_input = task_input if isinstance(task_input, dict) else {}
    config = task_input.get("config")
    config = config if isinstance(config, dict) else {}
    requested = _first_non_empty(
        payload.get("funnel_id"),
        task_input.get("funnel_id"),
        config.get("funnel_id"),
    )
    resolved = resolve_funnel_id(requested)
    rules_version = resolve_rules_version(resolved)
    return {
        "base_prompt_version": str(payload.get("prompt_version") or BASE_PROMPT_VERSION),
        "requested_funnel_id": requested,
        "resolved_funnel_id": resolved,
        "funnel_rules_version": rules_version,
    }


def resolve_funnel_id(funnel_id: Any) -> str:
    if funnel_id is None or (isinstance(funnel_id, str) and not funnel_id.strip()):
        return DEFAULT_FUNNEL_ID
    raw = str(funnel_id).strip().lower()
    aliases = get_funnel_rule_aliases()
    resolved = aliases.get(raw)
    if resolved is None:
        raise AITaskError(
            "UNKNOWN_FUNNEL_ID",
            f"Unknown funnel_id {funnel_id!r}. Supported funnel IDs: {', '.join(sorted(aliases))}.",
            status_code=400,
        )
    return resolved


def _load_funnel_rules_text(rules_version: str) -> str:
    path = (FUNNEL_RULES_DIR / f"{rules_version}.txt").resolve()
    root = FUNNEL_RULES_DIR.resolve()
    if path.parent != root:
        raise AITaskError(
            "INVALID_FUNNEL_RULES_VERSION",
            "Funnel rules must be loaded from the configured funnel rules directory.",
            status_code=500,
        )
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise AITaskError(
            "FUNNEL_RULES_NOT_FOUND",
            f"Funnel rules file not found: {rules_version}",
            status_code=500,
        ) from exc


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
