"""Section-level raw candidate discovery helpers.

This module discovers raw clip opportunities inside transcript sections. It does
not score candidates, choose final clips, render media, or register output-funnel
artifacts.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ai_service_client import (
    Transport,
    _TransportError,
    _urllib_transport,
    ai_service_timeout_seconds,
    ai_service_url,
)
from mk04_utils import now_iso, write_json

SECTION_DISCOVERY_SCHEMA_VERSION = "section_candidate_discovery_v1"
SECTION_DISCOVERY_BATCH_SCHEMA_VERSION = "section_candidate_discovery_batch_v1"
SECTION_DISCOVERY_ARTIFACT_FILENAME = "section_candidate_discovery.json"
DEFAULT_PROMPT_VERSION = "section_candidate_discovery_v1"
DEFAULT_SCHEMA_VERSION = "section_candidate_discovery_v1"

DEFAULT_FAIL_FAST = False
DEFAULT_MAX_CANDIDATES_PER_SECTION = 3
DEFAULT_MIN_CANDIDATE_DURATION_SEC = 15.0
DEFAULT_MAX_CANDIDATE_DURATION_SEC = 120.0
TIMESTAMP_TOLERANCE_SEC = 0.001

SECTION_RESULT_REQUIRED_FIELDS = (
    "schema_version",
    "section_id",
    "usable",
    "confidence",
    "reason",
    "warnings",
    "candidates",
)

CANDIDATE_REQUIRED_FIELDS = (
    "candidate_local_id",
    "source_section_id",
    "start_sec",
    "end_sec",
    "duration_sec",
    "hook_text",
    "core_idea_summary",
    "why_candidate_has_potential",
    "confidence",
    "warnings",
)

BATCH_COUNT_FIELDS = (
    "sections_received",
    "sections_processed",
    "usable_sections",
    "rejected_sections",
    "candidates_discovered",
)

BATCH_REQUIRED_FIELDS = (
    "schema_version",
    *BATCH_COUNT_FIELDS,
    "section_results",
    "warnings",
    "failed_sections",
)

ARTIFACT_REQUIRED_FIELDS = (
    "schema_version",
    "job_id",
    "source_transcript_sections_path",
    "created_at",
    "discovery_version",
    "config",
    *BATCH_COUNT_FIELDS,
    "section_results",
    "warnings",
    "failed_sections",
)


class SectionCandidateDiscoveryError(RuntimeError):
    """Raised for cleanly classified section discovery failures."""

    def __init__(
        self,
        code: str,
        message: str,
        errors: list[str] | None = None,
        *,
        raw_text: str | None = None,
    ):
        self.code = code
        self.message = message
        self.errors = list(errors or [])
        self.raw_text = raw_text
        detail = f"{message}: {'; '.join(self.errors)}" if self.errors else message
        super().__init__(detail)


@dataclass(frozen=True)
class CandidateDiscoveryConfig:
    fail_fast: bool = DEFAULT_FAIL_FAST
    max_candidates_per_section: int = DEFAULT_MAX_CANDIDATES_PER_SECTION
    min_candidate_duration_sec: float = DEFAULT_MIN_CANDIDATE_DURATION_SEC
    max_candidate_duration_sec: float = DEFAULT_MAX_CANDIDATE_DURATION_SEC

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AiServiceSectionDiscoveryClient:
    """Small adapter for the local ai-service section discovery task."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        transport: Transport | None = None,
        job_id: str = "section-candidate-discovery",
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        schema_version: str = DEFAULT_SCHEMA_VERSION,
    ):
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.transport = transport or _urllib_transport
        self.job_id = job_id
        self.prompt_version = prompt_version
        self.schema_version = schema_version

    def discover_section(
        self,
        section: dict[str, Any],
        *,
        config: CandidateDiscoveryConfig,
    ) -> dict[str, Any]:
        envelope = {
            "task_type": "section_candidate_discovery",
            "job_id": self.job_id,
            "input": {"section": section, "config": config.as_dict()},
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
        }
        url = (self.base_url.rstrip("/") if self.base_url else ai_service_url()) + "/ai/run"
        timeout = (
            self.timeout_seconds
            if self.timeout_seconds and self.timeout_seconds > 0
            else ai_service_timeout_seconds()
        )
        try:
            status_code, body_text = self.transport(url, envelope, timeout)
        except _TransportError as exc:
            raise SectionCandidateDiscoveryError(exc.code, exc.message) from exc

        try:
            body = json.loads(body_text or "")
        except json.JSONDecodeError as exc:
            raise SectionCandidateDiscoveryError(
                "AI_SERVICE_NON_JSON",
                f"ai-service returned non-JSON body (HTTP {status_code}).",
                raw_text=body_text,
            ) from exc
        if not isinstance(body, dict):
            raise SectionCandidateDiscoveryError(
                "AI_SERVICE_BAD_SHAPE",
                "ai-service response was not a JSON object.",
                raw_text=body_text,
            )
        if status_code != 200 or body.get("status") != "ok":
            error = body.get("error") if isinstance(body.get("error"), dict) else {}
            code = str(error.get("code") or f"AI_SERVICE_HTTP_{status_code}")
            message = str(error.get("message") or f"ai-service returned HTTP {status_code}.")
            raise SectionCandidateDiscoveryError(code, message, raw_text=body_text)
        result = body.get("result")
        if not isinstance(result, dict):
            raise SectionCandidateDiscoveryError(
                "AI_SERVICE_BAD_RESULT",
                "ai-service returned ok without a result object.",
                raw_text=body_text,
            )
        return result


def default_discovery_config() -> CandidateDiscoveryConfig:
    return CandidateDiscoveryConfig()


def apply_default_discovery_config(
    config: dict[str, Any] | CandidateDiscoveryConfig | None = None,
) -> CandidateDiscoveryConfig:
    if config is None:
        resolved = CandidateDiscoveryConfig()
    elif isinstance(config, CandidateDiscoveryConfig):
        resolved = config
    elif isinstance(config, dict):
        resolved = CandidateDiscoveryConfig(
            fail_fast=bool(config.get("fail_fast", DEFAULT_FAIL_FAST)),
            max_candidates_per_section=int(
                config.get(
                    "max_candidates_per_section",
                    DEFAULT_MAX_CANDIDATES_PER_SECTION,
                )
            ),
            min_candidate_duration_sec=float(
                config.get(
                    "min_candidate_duration_sec",
                    DEFAULT_MIN_CANDIDATE_DURATION_SEC,
                )
            ),
            max_candidate_duration_sec=float(
                config.get(
                    "max_candidate_duration_sec",
                    DEFAULT_MAX_CANDIDATE_DURATION_SEC,
                )
            ),
        )
    else:
        raise SectionCandidateDiscoveryError(
            "INVALID_DISCOVERY_CONFIG",
            "discovery config must be an object or CandidateDiscoveryConfig.",
        )
    validate_discovery_config(resolved)
    return resolved


def validate_discovery_config(config: CandidateDiscoveryConfig) -> None:
    errors: list[str] = []
    if not isinstance(config.fail_fast, bool):
        errors.append("fail_fast must be boolean")
    if (
        not isinstance(config.max_candidates_per_section, int)
        or isinstance(config.max_candidates_per_section, bool)
        or config.max_candidates_per_section < 0
    ):
        errors.append("max_candidates_per_section must be a non-negative integer")
    if (
        not _is_number(config.min_candidate_duration_sec)
        or config.min_candidate_duration_sec <= 0
    ):
        errors.append("min_candidate_duration_sec must be > 0")
    if (
        not _is_number(config.max_candidate_duration_sec)
        or config.max_candidate_duration_sec <= 0
    ):
        errors.append("max_candidate_duration_sec must be > 0")
    if (
        _is_number(config.min_candidate_duration_sec)
        and _is_number(config.max_candidate_duration_sec)
        and config.max_candidate_duration_sec < config.min_candidate_duration_sec
    ):
        errors.append("max_candidate_duration_sec must be >= min_candidate_duration_sec")
    if errors:
        raise SectionCandidateDiscoveryError(
            "INVALID_DISCOVERY_CONFIG",
            "Candidate discovery config is invalid.",
            errors,
        )


def section_candidate_discovery_path(job_dir: str) -> str:
    return os.path.join(job_dir, SECTION_DISCOVERY_ARTIFACT_FILENAME)


def load_default_discovery_prompt() -> str:
    prompt_path = (
        Path(__file__).resolve().parents[2]
        / "ai-service"
        / "prompts"
        / "section_candidate_discovery_v1.txt"
    )
    return prompt_path.read_text(encoding="utf-8")


def build_section_discovery_prompt(
    section: dict[str, Any],
    *,
    config: dict[str, Any] | CandidateDiscoveryConfig | None = None,
    prompt_template: str | None = None,
) -> str:
    resolved_config = apply_default_discovery_config(config)
    prompt = prompt_template if prompt_template is not None else load_default_discovery_prompt()
    context = {
        "section": section,
        "config": resolved_config.as_dict(),
    }
    return "\n\n".join(
        [
            prompt.strip(),
            "REQUEST CONTEXT - JSON:",
            json.dumps(context, indent=2, sort_keys=True),
            "Return strict JSON only.",
        ]
    )


def discover_candidates_for_section(
    section: dict[str, Any],
    *,
    ai_client: Any | None = None,
    config: dict[str, Any] | CandidateDiscoveryConfig | None = None,
    prompt_template: str | None = None,
) -> dict[str, Any]:
    resolved_config = apply_default_discovery_config(config)
    client = ai_client or AiServiceSectionDiscoveryClient()
    if hasattr(client, "discover_section"):
        result = client.discover_section(section, config=resolved_config)
    else:
        prompt = build_section_discovery_prompt(
            section,
            config=resolved_config,
            prompt_template=prompt_template,
        )
        response = client.generate(prompt)
        result = parse_section_discovery_model_response(response)

    capped = _apply_candidate_cap(result, resolved_config)
    validate_section_discovery_result(capped, section=section, config=resolved_config)
    return capped


def discover_candidates_for_sections(
    sections: list[dict[str, Any]],
    *,
    ai_client: Any | None = None,
    config: dict[str, Any] | CandidateDiscoveryConfig | None = None,
    prompt_template: str | None = None,
) -> dict[str, Any]:
    resolved_config = apply_default_discovery_config(config)
    section_results: list[dict[str, Any]] = []
    failed_sections: list[dict[str, Any]] = []
    warnings: list[str] = []

    for section in sections:
        try:
            result = discover_candidates_for_section(
                section,
                ai_client=ai_client,
                config=resolved_config,
                prompt_template=prompt_template,
            )
            section_results.append(result)
        except SectionCandidateDiscoveryError as exc:
            failed_sections.append(_failed_section_record(section, exc))
            if resolved_config.fail_fast:
                warnings.append("fail_fast_stopped_after_section_failure")
                break

    batch = {
        "schema_version": SECTION_DISCOVERY_BATCH_SCHEMA_VERSION,
        "sections_received": len(sections),
        "sections_processed": len(section_results),
        "usable_sections": sum(1 for result in section_results if result.get("usable") is True),
        "rejected_sections": sum(1 for result in section_results if result.get("usable") is False),
        "candidates_discovered": sum(
            len(result.get("candidates") or []) for result in section_results
        ),
        "section_results": section_results,
        "warnings": warnings,
        "failed_sections": failed_sections,
    }
    validate_section_discovery_batch(batch)
    return batch


def parse_section_discovery_model_response(response: Any) -> dict[str, Any]:
    error = getattr(response, "error", None)
    if error:
        raise SectionCandidateDiscoveryError(
            "MODEL_CALL_FAILED",
            f"Model call failed: {error}",
        )
    text = response if isinstance(response, str) else getattr(response, "text", None)
    if not isinstance(text, str) or not text.strip():
        raise SectionCandidateDiscoveryError(
            "MODEL_OUTPUT_EMPTY",
            "Model output was empty.",
            raw_text=text if isinstance(text, str) else None,
        )
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise SectionCandidateDiscoveryError(
            "MODEL_JSON_INVALID",
            f"Model output was not strict JSON: {exc.msg}.",
            raw_text=text,
        ) from exc
    if not isinstance(parsed, dict):
        raise SectionCandidateDiscoveryError(
            "MODEL_OUTPUT_BAD_SHAPE",
            "Model output must be a JSON object.",
            raw_text=text,
        )
    return parsed


def validate_section_discovery_result(
    result: Any,
    *,
    section: dict[str, Any],
    config: dict[str, Any] | CandidateDiscoveryConfig | None = None,
) -> None:
    errors: list[str] = []
    resolved_config = apply_default_discovery_config(config)
    if not isinstance(result, dict):
        raise SectionCandidateDiscoveryError(
            "INVALID_SECTION_DISCOVERY_RESULT",
            "section discovery result must be an object.",
        )
    _check_required_fields(result, SECTION_RESULT_REQUIRED_FIELDS, "result", errors)
    section_id = section.get("section_id")
    if result.get("schema_version") != SECTION_DISCOVERY_SCHEMA_VERSION:
        errors.append(
            f"result.schema_version must equal {SECTION_DISCOVERY_SCHEMA_VERSION!r}"
        )
    if not _is_non_empty_string(result.get("section_id")):
        errors.append("result.section_id must be a non-empty string")
    elif result.get("section_id") != section_id:
        errors.append("result.section_id must match input section_id")
    if not isinstance(result.get("usable"), bool):
        errors.append("result.usable must be boolean")
    if not _is_number(result.get("confidence")) or not 0.0 <= float(result["confidence"]) <= 1.0:
        errors.append("result.confidence must be numeric and within 0-1")
    if "reason" in result and not isinstance(result.get("reason"), str):
        errors.append("result.reason must be a string")
    _validate_string_list(result.get("warnings"), "result.warnings", errors)
    candidates = result.get("candidates")
    if not isinstance(candidates, list):
        errors.append("result.candidates must be a list")
    else:
        for index, candidate in enumerate(candidates):
            _validate_discovered_candidate(
                candidate,
                f"result.candidates[{index}]",
                section=section,
                config=resolved_config,
                errors=errors,
            )
    if result.get("usable") is False and isinstance(candidates, list) and candidates:
        errors.append("result.candidates must be empty when usable is false")
    if result.get("usable") is True and isinstance(candidates, list) and not candidates:
        warnings = result.get("warnings")
        reason = result.get("reason")
        if not (isinstance(warnings, list) and warnings) and not (
            isinstance(reason, str) and reason.strip()
        ):
            errors.append(
                "result usable=true with no candidates needs a clear reason or warning"
            )

    if errors:
        raise SectionCandidateDiscoveryError(
            "INVALID_SECTION_DISCOVERY_RESULT",
            "section discovery result validation failed.",
            errors,
        )


def validate_section_discovery_batch(batch: Any) -> None:
    errors: list[str] = []
    if not isinstance(batch, dict):
        raise SectionCandidateDiscoveryError(
            "INVALID_SECTION_DISCOVERY_BATCH",
            "section discovery batch must be an object.",
        )
    _check_required_fields(batch, BATCH_REQUIRED_FIELDS, "batch", errors)
    if batch.get("schema_version") != SECTION_DISCOVERY_BATCH_SCHEMA_VERSION:
        errors.append(
            f"batch.schema_version must equal {SECTION_DISCOVERY_BATCH_SCHEMA_VERSION!r}"
        )
    for field in BATCH_COUNT_FIELDS:
        if field in batch and not _is_non_negative_int(batch.get(field)):
            errors.append(f"batch.{field} must be a non-negative integer")
    if "section_results" in batch and not isinstance(batch.get("section_results"), list):
        errors.append("batch.section_results must be a list")
    if "warnings" in batch:
        _validate_string_list(batch.get("warnings"), "batch.warnings", errors)
    if "failed_sections" in batch and not isinstance(batch.get("failed_sections"), list):
        errors.append("batch.failed_sections must be a list")
    if errors:
        raise SectionCandidateDiscoveryError(
            "INVALID_SECTION_DISCOVERY_BATCH",
            "section discovery batch validation failed.",
            errors,
        )


def build_section_candidate_discovery_artifact(
    *,
    job_id: str,
    source_transcript_sections_path: str,
    batch_result: dict[str, Any],
    config: dict[str, Any] | CandidateDiscoveryConfig | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    validate_section_discovery_batch(batch_result)
    resolved_config = apply_default_discovery_config(config)
    payload = {
        "schema_version": SECTION_DISCOVERY_BATCH_SCHEMA_VERSION,
        "job_id": job_id,
        "source_transcript_sections_path": source_transcript_sections_path,
        "created_at": created_at or now_iso(),
        "discovery_version": SECTION_DISCOVERY_SCHEMA_VERSION,
        "config": resolved_config.as_dict(),
        "sections_received": batch_result["sections_received"],
        "sections_processed": batch_result["sections_processed"],
        "usable_sections": batch_result["usable_sections"],
        "rejected_sections": batch_result["rejected_sections"],
        "candidates_discovered": batch_result["candidates_discovered"],
        "section_results": list(batch_result["section_results"]),
        "warnings": list(batch_result["warnings"]),
        "failed_sections": list(batch_result["failed_sections"]),
    }
    validate_section_candidate_discovery_artifact(payload)
    return payload


def write_section_candidate_discovery(job_dir: str, payload: dict[str, Any]) -> str:
    validate_section_candidate_discovery_artifact(payload)
    path = section_candidate_discovery_path(job_dir)
    write_json(path, payload)
    return path


def read_section_candidate_discovery(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    validate_section_candidate_discovery_artifact(payload)
    return payload


def validate_section_candidate_discovery_artifact(payload: Any) -> None:
    errors: list[str] = []
    if not isinstance(payload, dict):
        raise SectionCandidateDiscoveryError(
            "INVALID_SECTION_DISCOVERY_ARTIFACT",
            "section discovery artifact must be an object.",
        )
    _check_required_fields(payload, ARTIFACT_REQUIRED_FIELDS, "artifact", errors)
    if payload.get("schema_version") != SECTION_DISCOVERY_BATCH_SCHEMA_VERSION:
        errors.append(
            f"artifact.schema_version must equal {SECTION_DISCOVERY_BATCH_SCHEMA_VERSION!r}"
        )
    if not _is_non_empty_string(payload.get("job_id")):
        errors.append("artifact.job_id must be a non-empty string")
    if "source_transcript_sections_path" in payload and not isinstance(
        payload.get("source_transcript_sections_path"), str
    ):
        errors.append("artifact.source_transcript_sections_path must be a string")
    if not _is_iso_timestamp(payload.get("created_at")):
        errors.append("artifact.created_at must be a non-empty ISO timestamp string")
    if payload.get("discovery_version") != SECTION_DISCOVERY_SCHEMA_VERSION:
        errors.append(
            f"artifact.discovery_version must equal {SECTION_DISCOVERY_SCHEMA_VERSION!r}"
        )
    if "config" in payload:
        try:
            apply_default_discovery_config(payload.get("config"))
        except SectionCandidateDiscoveryError as exc:
            errors.extend(f"artifact.config.{err}" for err in exc.errors)
    if not errors:
        try:
            validate_section_discovery_batch(
                {field: payload[field] for field in BATCH_REQUIRED_FIELDS}
            )
        except SectionCandidateDiscoveryError as exc:
            errors.extend(exc.errors or [exc.message])
    if errors:
        raise SectionCandidateDiscoveryError(
            "INVALID_SECTION_DISCOVERY_ARTIFACT",
            "section discovery artifact validation failed.",
            errors,
        )


def _apply_candidate_cap(
    result: dict[str, Any],
    config: CandidateDiscoveryConfig,
) -> dict[str, Any]:
    out = dict(result)
    candidates = out.get("candidates")
    if not isinstance(candidates, list):
        return out
    if len(candidates) <= config.max_candidates_per_section:
        return out
    out["candidates"] = candidates[: config.max_candidates_per_section]
    warnings = out.get("warnings") if isinstance(out.get("warnings"), list) else []
    out["warnings"] = [
        *warnings,
        "max_candidates_per_section_applied",
    ]
    return out


def _validate_discovered_candidate(
    candidate: Any,
    path: str,
    *,
    section: dict[str, Any],
    config: CandidateDiscoveryConfig,
    errors: list[str],
) -> None:
    if not isinstance(candidate, dict):
        errors.append(f"{path} must be an object")
        return
    _check_required_fields(candidate, CANDIDATE_REQUIRED_FIELDS, path, errors)
    if not _is_non_empty_string(candidate.get("candidate_local_id")):
        errors.append(f"{path}.candidate_local_id must be a non-empty string")
    section_id = section.get("section_id")
    if candidate.get("source_section_id") != section_id:
        errors.append(f"{path}.source_section_id must match input section_id")

    start = candidate.get("start_sec")
    end = candidate.get("end_sec")
    duration = candidate.get("duration_sec")
    start_ok = _is_number(start)
    end_ok = _is_number(end)
    duration_ok = _is_number(duration)
    if not start_ok:
        errors.append(f"{path}.start_sec must be numeric")
    if not end_ok:
        errors.append(f"{path}.end_sec must be numeric")
    if start_ok and end_ok and float(end) <= float(start):
        errors.append(f"{path}.end_sec must be greater than start_sec")
    if not duration_ok:
        errors.append(f"{path}.duration_sec must be numeric")
    if start_ok and end_ok and duration_ok:
        expected_duration = float(end) - float(start)
        if abs(float(duration) - expected_duration) > TIMESTAMP_TOLERANCE_SEC:
            errors.append(
                f"{path}.duration_sec must match end_sec - start_sec within "
                f"{TIMESTAMP_TOLERANCE_SEC:g}s"
            )
        if float(duration) < config.min_candidate_duration_sec - TIMESTAMP_TOLERANCE_SEC:
            errors.append(f"{path}.duration_sec must be >= min_candidate_duration_sec")
        if float(duration) > config.max_candidate_duration_sec + TIMESTAMP_TOLERANCE_SEC:
            errors.append(f"{path}.duration_sec must be <= max_candidate_duration_sec")
    if start_ok and end_ok:
        section_start = section.get("start_sec")
        section_end = section.get("end_sec")
        if _is_number(section_start) and float(start) < float(section_start) - TIMESTAMP_TOLERANCE_SEC:
            errors.append(f"{path}.start_sec must stay inside section bounds")
        if _is_number(section_end) and float(end) > float(section_end) + TIMESTAMP_TOLERANCE_SEC:
            errors.append(f"{path}.end_sec must stay inside section bounds")

    for field in ("hook_text", "core_idea_summary", "why_candidate_has_potential"):
        if field in candidate and not isinstance(candidate.get(field), str):
            errors.append(f"{path}.{field} must be a string")
    if not _is_number(candidate.get("confidence")) or not 0.0 <= float(candidate["confidence"]) <= 1.0:
        errors.append(f"{path}.confidence must be numeric and within 0-1")
    _validate_string_list(candidate.get("warnings"), f"{path}.warnings", errors)


def _failed_section_record(
    section: dict[str, Any],
    exc: SectionCandidateDiscoveryError,
) -> dict[str, Any]:
    record = {
        "section_id": str(section.get("section_id") or ""),
        "error_code": exc.code,
        "error_reason": exc.message,
    }
    if exc.raw_text:
        record["raw_response_snippet"] = exc.raw_text[:500]
    return record


def _validate_string_list(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, list):
        errors.append(f"{path} must be a list")
        return
    if not all(isinstance(item, str) for item in value):
        errors.append(f"{path} must contain only strings")


def _check_required_fields(
    payload: dict[str, Any],
    required_fields: tuple[str, ...],
    path: str,
    errors: list[str],
) -> None:
    for field in required_fields:
        if field not in payload:
            errors.append(f"{path}.{field} is required")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_iso_timestamp(value: Any) -> bool:
    if not _is_non_empty_string(value):
        return False
    try:
        datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True
