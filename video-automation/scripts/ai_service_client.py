"""Boring, explicit HTTP client for the local `ai-service` clip-selection task.

This client owns one job only: turn a transcript context into an `ai-service`
`POST /ai/run` call for ``task_type=clip_selection`` and classify the reply into
a small, explicit set of outcomes.

It deliberately does NOT:
  - own job state, retries, or reports (video-automation owns those),
  - fabricate clip candidates when the AI service fails,
  - fall back to a cloud/inline model (MK1 has no cloud fallback yet),
  - mutate ai-service state.

`ai-service` owns the local-model lock; `video-automation` owns job truth and
retries. ``AI_BUSY`` is surfaced as a retryable outcome so the existing
video-automation job/retry system can retry later.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


DEFAULT_AI_SERVICE_URL = "http://127.0.0.1:5075"
DEFAULT_AI_SERVICE_TIMEOUT_SECONDS = 180.0
DEFAULT_PROMPT_VERSION = "clip_selection_v2"
DEFAULT_SCHEMA_VERSION = "clip_candidates_v2"

# Explicit outcome categories. The caller maps these onto its own job truth.
OUTCOME_USABLE = "usable"            # usable=true with candidates -> continue pipeline
OUTCOME_NO_CLIP = "no_clip"          # usable=false -> controlled no-clip, do not force a bad clip
OUTCOME_BUSY = "busy"                # AI_BUSY (HTTP 503) -> retryable through video-automation
OUTCOME_AI_FAILURE = "ai_failure"    # model/output/validation/transport error -> controlled failure


class AiServiceConfigError(ValueError):
    """Raised when the request cannot even be built (bad local input)."""


@dataclass
class AiServiceResult:
    """Normalised result of one clip-selection call to ai-service."""

    outcome: str
    retryable: bool = False
    candidates: list[dict[str, Any]] = field(default_factory=list)
    status_code: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    request_id: str | None = None
    raw_response: dict[str, Any] | None = None

    @property
    def usable(self) -> bool:
        return self.outcome == OUTCOME_USABLE

    @property
    def no_clip(self) -> bool:
        return self.outcome == OUTCOME_NO_CLIP

    @property
    def busy(self) -> bool:
        return self.outcome == OUTCOME_BUSY

    @property
    def ai_failure(self) -> bool:
        return self.outcome == OUTCOME_AI_FAILURE

    def summary(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "retryable": self.retryable,
            "candidate_count": len(self.candidates),
            "status_code": self.status_code,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "request_id": self.request_id,
        }


def ai_service_url() -> str:
    """Resolve the ai-service base URL: Ops UI saved value -> env -> default."""
    try:
        from ai_settings import resolve_ai_service_url

        return resolve_ai_service_url()
    except Exception:
        raw = os.environ.get("AI_SERVICE_URL", "").strip()
        return (raw or DEFAULT_AI_SERVICE_URL).rstrip("/")


def ai_service_timeout_seconds() -> float:
    """Resolve the ai-service timeout: Ops UI saved value -> env -> default."""
    try:
        from ai_settings import resolve_ai_service_timeout_seconds

        return resolve_ai_service_timeout_seconds()
    except Exception:
        raw = os.environ.get("AI_SERVICE_TIMEOUT_SECONDS", "").strip()
        if not raw:
            return DEFAULT_AI_SERVICE_TIMEOUT_SECONDS
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_AI_SERVICE_TIMEOUT_SECONDS
        return value if value > 0 else DEFAULT_AI_SERVICE_TIMEOUT_SECONDS


def build_clip_selection_request(
    *,
    job_id: str,
    task_input: dict[str, Any],
    funnel_id: str | None = None,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
    request_id: str | None = None,
    model_preference: str | None = None,
) -> dict[str, Any]:
    """Build the `/ai/run` request envelope for a clip-selection call."""
    if not isinstance(job_id, str) or not job_id.strip():
        raise AiServiceConfigError("ai-service clip_selection requires a non-empty job_id")
    if not isinstance(task_input, dict):
        raise AiServiceConfigError("ai-service clip_selection requires an input object")

    envelope: dict[str, Any] = {
        "task_type": "clip_selection",
        "job_id": job_id.strip(),
        "input": task_input,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
    }
    if funnel_id and str(funnel_id).strip():
        envelope["funnel_id"] = str(funnel_id).strip()
    if request_id and str(request_id).strip():
        envelope["request_id"] = str(request_id).strip()
    if model_preference and str(model_preference).strip():
        envelope["model_preference"] = str(model_preference).strip()
    return envelope


def request_clip_selection(
    *,
    job_id: str,
    task_input: dict[str, Any],
    funnel_id: str | None = None,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
    request_id: str | None = None,
    model_preference: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float | None = None,
    transport: Transport | None = None,
) -> AiServiceResult:
    """Run one clip-selection judgement call against ai-service.

    Returns an :class:`AiServiceResult`. Never raises for transport/HTTP/AI
    failures: those are mapped to ``OUTCOME_AI_FAILURE`` (or ``OUTCOME_BUSY`` for
    AI_BUSY). Only local request-construction problems raise.
    """
    envelope = build_clip_selection_request(
        job_id=job_id,
        task_input=task_input,
        funnel_id=funnel_id,
        prompt_version=prompt_version,
        schema_version=schema_version,
        request_id=request_id,
        model_preference=model_preference,
    )
    url = (base_url.rstrip("/") if base_url else ai_service_url()) + "/ai/run"
    timeout = timeout_seconds if timeout_seconds and timeout_seconds > 0 else ai_service_timeout_seconds()
    send = transport or _urllib_transport

    try:
        status_code, body_text = send(url, envelope, timeout)
    except _TransportError as exc:
        return AiServiceResult(
            outcome=OUTCOME_AI_FAILURE,
            retryable=False,
            status_code=None,
            error_code=exc.code,
            error_message=exc.message,
        )

    return _classify_response(status_code, body_text)


def _classify_response(status_code: int, body_text: str) -> AiServiceResult:
    try:
        body = json.loads(body_text or "")
    except (json.JSONDecodeError, ValueError):
        return AiServiceResult(
            outcome=OUTCOME_AI_FAILURE,
            retryable=False,
            status_code=status_code,
            error_code="AI_SERVICE_NON_JSON",
            error_message=f"ai-service returned non-JSON body (HTTP {status_code}).",
        )
    if not isinstance(body, dict):
        return AiServiceResult(
            outcome=OUTCOME_AI_FAILURE,
            retryable=False,
            status_code=status_code,
            error_code="AI_SERVICE_BAD_SHAPE",
            error_message="ai-service response was not a JSON object.",
        )

    request_id = body.get("request_id") if isinstance(body.get("request_id"), str) else None
    error_obj = body.get("error") if isinstance(body.get("error"), dict) else None
    error_code = str(error_obj.get("code")) if error_obj and error_obj.get("code") else None
    error_message = str(error_obj.get("message")) if error_obj and error_obj.get("message") else None

    if status_code == 503 or error_code == "AI_BUSY":
        return AiServiceResult(
            outcome=OUTCOME_BUSY,
            retryable=True,
            status_code=status_code,
            error_code=error_code or "AI_BUSY",
            error_message=error_message or "Local AI model is busy. Retry later.",
            request_id=request_id,
            raw_response=body,
        )

    if status_code == 200 and body.get("status") == "ok":
        result = body.get("result")
        if not isinstance(result, dict):
            return AiServiceResult(
                outcome=OUTCOME_AI_FAILURE,
                retryable=False,
                status_code=status_code,
                error_code="AI_SERVICE_BAD_RESULT",
                error_message="ai-service returned ok without a result object.",
                request_id=request_id,
                raw_response=body,
            )
        if result.get("usable") is True:
            candidates = result.get("candidates")
            candidates = candidates if isinstance(candidates, list) else []
            if not candidates:
                # usable=true with no candidates is contradictory -> treat as no clip.
                return AiServiceResult(
                    outcome=OUTCOME_NO_CLIP,
                    retryable=False,
                    status_code=status_code,
                    request_id=request_id,
                    raw_response=body,
                )
            return AiServiceResult(
                outcome=OUTCOME_USABLE,
                retryable=False,
                candidates=candidates,
                status_code=status_code,
                request_id=request_id,
                raw_response=body,
            )
        return AiServiceResult(
            outcome=OUTCOME_NO_CLIP,
            retryable=False,
            status_code=status_code,
            request_id=request_id,
            raw_response=body,
        )

    # Everything else (4xx input/task errors, 5xx ai-service errors,
    # MODEL_CALL_FAILED, MODEL_OUTPUT_INVALID, validation errors) is a
    # controlled AI failure. video-automation keeps logs/report for debugging.
    return AiServiceResult(
        outcome=OUTCOME_AI_FAILURE,
        retryable=False,
        status_code=status_code,
        error_code=error_code or f"AI_SERVICE_HTTP_{status_code}",
        error_message=error_message or f"ai-service returned HTTP {status_code}.",
        request_id=request_id,
        raw_response=body,
    )


class _TransportError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# A transport takes (url, json_body, timeout) and returns (status_code, body_text).
Transport = Callable[[str, dict[str, Any], float], "tuple[int, str]"]


def _urllib_transport(url: str, body: dict[str, Any], timeout: float) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), text
    except urllib.error.HTTPError as exc:
        # HTTP errors still carry a JSON body we want to classify (e.g. 503 AI_BUSY).
        try:
            text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return int(exc.code), text
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise _TransportError(
            "AI_SERVICE_UNREACHABLE",
            f"Could not reach ai-service at {url}: {reason}",
        ) from exc
    except TimeoutError as exc:
        raise _TransportError(
            "AI_SERVICE_TIMEOUT",
            f"ai-service request timed out after {timeout:g}s: {exc}",
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive catch-all
        raise _TransportError(
            "AI_SERVICE_TRANSPORT_ERROR",
            f"Unexpected error calling ai-service: {exc!r}",
        ) from exc
