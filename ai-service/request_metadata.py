from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any


REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class RequestMetadataError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class RunMetadata:
    request_id: str
    input_hash: str | None
    output_hash: str | None
    reusable_result_key: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "reusable_result_key": self.reusable_result_key,
        }


def generate_request_id() -> str:
    return str(uuid.uuid4())


def validate_request_id(request_id: str) -> str:
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise RequestMetadataError(
            "INVALID_REQUEST_ID",
            "request_id contains unsupported characters or is too long.",
        )
    return request_id


def resolve_request_id(payload: dict[str, Any]) -> str:
    supplied = payload.get("request_id")
    if supplied is None:
        return generate_request_id()
    if not isinstance(supplied, str) or not supplied.strip():
        raise RequestMetadataError(
            "INVALID_REQUEST_ID",
            "request_id must be a non-empty string when supplied.",
        )
    return validate_request_id(supplied.strip())


def canonicalize_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_json(value: Any) -> str:
    canonical = canonicalize_json(value)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def generate_input_hash(
    payload: dict[str, Any],
    *,
    model_configured: str,
    provider: str,
) -> str:
    hash_input: dict[str, Any] = {
        "task_type": payload.get("task_type"),
        "job_id": payload.get("job_id"),
        "input": payload.get("input"),
        "prompt_version": payload.get("prompt_version"),
        "schema_version": payload.get("schema_version"),
        "model_configured": model_configured,
        "provider": provider,
    }
    if "funnel_id" in payload:
        hash_input["funnel_id"] = payload.get("funnel_id")
    if "model_preference" in payload:
        hash_input["model_preference"] = payload.get("model_preference")
    return sha256_json(hash_input)


def generate_output_hash(result: Any) -> str | None:
    if result is None:
        return None
    return sha256_json(result)


def build_reusable_result_key(
    *,
    task_type: str,
    input_hash: str,
    prompt_version: str,
    schema_version: str,
    model_used: str,
    provider: str,
) -> dict[str, Any]:
    return {
        "task_type": task_type,
        "input_hash": input_hash,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "model_used": model_used,
        "provider": provider,
    }


def build_run_metadata(
    *,
    request_id: str,
    payload: dict[str, Any],
    model_used: str,
    provider: str,
    result: Any | None = None,
    include_hashes: bool = True,
) -> RunMetadata:
    if not include_hashes:
        return RunMetadata(
            request_id=request_id,
            input_hash=None,
            output_hash=None,
            reusable_result_key=None,
        )

    input_hash = generate_input_hash(
        payload,
        model_configured=model_used,
        provider=provider,
    )
    output_hash = generate_output_hash(result)
    reusable_result_key = build_reusable_result_key(
        task_type=str(payload.get("task_type") or ""),
        input_hash=input_hash,
        prompt_version=str(payload.get("prompt_version") or ""),
        schema_version=str(payload.get("schema_version") or ""),
        model_used=model_used,
        provider=provider,
    )
    return RunMetadata(
        request_id=request_id,
        input_hash=input_hash,
        output_hash=output_hash,
        reusable_result_key=reusable_result_key,
    )
