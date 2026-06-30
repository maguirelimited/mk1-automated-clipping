from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


EMPTY_MODEL_OUTPUT = "EMPTY_MODEL_OUTPUT"
JSON_PARSE_FAILED = "JSON_PARSE_FAILED"
JSON_SCHEMA_VALIDATION_FAILED = "JSON_SCHEMA_VALIDATION_FAILED"
INVALID_JSON_SCHEMA = "INVALID_JSON_SCHEMA"
JSON_REPAIR_FAILED = "JSON_REPAIR_FAILED"


class ModelClientProtocol(Protocol):
    def generate(self, prompt: str) -> Any:
        ...


@dataclass(frozen=True)
class OutputValidationResult:
    ok: bool
    parsed_output: Any | None
    error_code: str | None
    error_message: str | None
    raw_text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "parsed_output": self.parsed_output,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "raw_text": self.raw_text,
        }


def validate_model_output(raw_text: str | None, schema: dict[str, Any]) -> OutputValidationResult:
    text = raw_text or ""
    if not text.strip():
        return _failure(EMPTY_MODEL_OUTPUT, "Model output was empty.", text)

    extracted = extract_first_json_object(text)
    if extracted is None:
        return _failure(JSON_PARSE_FAILED, "No complete JSON object found in model output.", text)

    try:
        parsed = json.loads(extracted)
    except json.JSONDecodeError as exc:
        return _failure(JSON_PARSE_FAILED, f"Model output JSON could not be parsed: {exc.msg}.", text)

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return _failure(INVALID_JSON_SCHEMA, f"Loaded schema is not a valid JSON Schema: {exc.message}.", text)

    try:
        Draft202012Validator(schema).validate(parsed)
    except ValidationError as exc:
        return _failure(JSON_SCHEMA_VALIDATION_FAILED, _schema_error_message(exc), text)

    return OutputValidationResult(
        ok=True,
        parsed_output=parsed,
        error_code=None,
        error_message=None,
        raw_text=text,
    )


def validate_with_one_repair(
    *,
    raw_text: str | None,
    schema: dict[str, Any],
    model_client: ModelClientProtocol,
) -> OutputValidationResult:
    initial = validate_model_output(raw_text, schema)
    if initial.ok:
        return initial

    repair_prompt = build_repair_prompt(
        original_output=initial.raw_text,
        schema=schema,
        validation_error=initial,
    )
    response = model_client.generate(repair_prompt)
    response_error = getattr(response, "error", None)
    response_text = getattr(response, "text", None)
    if response_error:
        return _failure(JSON_REPAIR_FAILED, f"Repair model call failed: {response_error}", response_text or "")

    repaired = validate_model_output(response_text, schema)
    if repaired.ok:
        return repaired

    return repaired


def build_repair_prompt(
    *,
    original_output: str,
    schema: dict[str, Any],
    validation_error: OutputValidationResult,
) -> str:
    schema_json = json.dumps(schema, indent=2, sort_keys=True)
    return (
        "Return only valid JSON.\n"
        "Match the provided JSON Schema exactly.\n"
        "Do not include markdown.\n"
        "Do not include explanation.\n"
        "Repair the supplied invalid output.\n\n"
        f"Validation error code: {validation_error.error_code}\n"
        f"Validation error message: {validation_error.error_message}\n\n"
        "JSON Schema:\n"
        f"{schema_json}\n\n"
        "Invalid output:\n"
        f"{original_output}"
    )


def extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        extracted = _extract_object_from(text, start)
        if extracted is not None:
            return extracted
        start = text.find("{", start + 1)
    return None


def _extract_object_from(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
            if depth < 0:
                return None
    return None


def _schema_error_message(exc: ValidationError) -> str:
    path = ".".join(str(part) for part in exc.path)
    if path:
        return f"JSON output failed schema validation at {path}: {exc.message}."
    return f"JSON output failed schema validation: {exc.message}."


def _failure(code: str, message: str, raw_text: str) -> OutputValidationResult:
    return OutputValidationResult(
        ok=False,
        parsed_output=None,
        error_code=code,
        error_message=message,
        raw_text=raw_text,
    )
