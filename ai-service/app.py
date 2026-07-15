from __future__ import annotations

from typing import Any

from flask import Flask, jsonify, request

from config import load_settings
from decision_logging import DecisionLogger
from model_client import ModelClientError, OllamaModelClient
from request_metadata import (
    RequestMetadataError,
    RunMetadata,
    build_run_metadata,
    generate_request_id,
    resolve_request_id,
)
from resource_lock import MODEL_RESOURCE_LOCK, ResourceBusyError
from task_router import AITaskError, TaskNotImplementedError, TaskRouter, UnknownTaskError
from versioned_assets import VersionedAssetError, load_prompt, load_schema


SERVICE_NAME = "ai-service"
DIAGNOSTIC_PROMPT = 'Return only this JSON: {"status":"model_ok"}'

app = Flask(__name__)
DECISION_LOGGER = DecisionLogger()


@app.get("/health")
def health():
    try:
        settings = load_settings()
        backend = _backend_status(settings)
        status = "ok" if backend["backend_reachable"] and backend["model_available"] else "degraded"
        payload = {
            "service": "ok",
            "status": status,
            "provider": settings.provider,
            "provider_loaded": backend["provider_loaded"],
            "model_configured": settings.model,
            "backend_reachable": backend["backend_reachable"],
            "model_available": backend["model_available"],
        }
        if backend.get("error"):
            payload["error"] = backend["error"]
        return jsonify(payload), 200
    except Exception as exc:
        return jsonify(_internal_error_payload(exc)), 500


@app.get("/diagnostics/model")
def diagnostics_model():
    try:
        settings = load_settings()
        backend = _backend_status(settings)
        payload = {
            "service": SERVICE_NAME,
            "status": "degraded",
            "provider": settings.provider,
            "model_configured": settings.model,
            "model_used": settings.model,
            "backend_reachable": backend["backend_reachable"],
            "model_available": backend["model_available"],
            "response_text": None,
            "error": backend.get("error"),
        }
        if not backend["provider_loaded"]:
            payload["error"] = backend.get("error") or f"Unsupported provider: {settings.provider}"
            return jsonify(payload), 400
        if not backend["backend_reachable"] or not backend["model_available"]:
            payload["error"] = backend.get("error") or "Configured model is not available from the backend."
            return jsonify(payload), 502

        client = OllamaModelClient(settings)
        try:
            with MODEL_RESOURCE_LOCK.guard():
                response = client.generate(DIAGNOSTIC_PROMPT)
        except ResourceBusyError as exc:
            return jsonify(_ai_busy_diagnostics_payload(settings, exc)), exc.status_code
        payload["model_used"] = response.model_used
        payload["response_text"] = response.text
        payload["error"] = response.error
        if response.error:
            return jsonify(payload), 502

        payload["status"] = "ok"
        return jsonify(payload), 200
    except Exception as exc:
        return jsonify(_internal_error_payload(exc)), 500


@app.post("/ai/run")
def run_ai_task():
    try:
        settings = load_settings()
        payload = request.get_json(silent=True) if request.is_json else None
        if not isinstance(payload, dict):
            envelope = _response_envelope({}, settings, request_id=generate_request_id())
            envelope.update(
                {
                    "status": "error",
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": "Request body must be a valid JSON object.",
                    },
                    "result": None,
                }
            )
            return jsonify(envelope), 400

        try:
            request_id = resolve_request_id(payload)
        except RequestMetadataError as exc:
            envelope = _response_envelope(payload, settings)
            envelope.update(
                {
                    "status": "error",
                    "error": {"code": exc.code, "message": exc.message},
                    "result": None,
                }
            )
            return jsonify(envelope), 400

        validation_error = _validate_run_request(payload)
        envelope = _response_envelope(
            payload,
            settings,
            metadata=build_run_metadata(
                request_id=request_id,
                payload=payload,
                model_used=settings.model,
                provider=settings.provider,
                include_hashes=False,
            ),
        )
        if validation_error is not None:
            envelope.update(
                {
                    "status": "error",
                    "error": validation_error,
                    "result": None,
                }
            )
            return jsonify(envelope), 400

        metadata = build_run_metadata(
            request_id=request_id,
            payload=payload,
            model_used=settings.model,
            provider=settings.provider,
        )
        envelope = _response_envelope(payload, settings, metadata=metadata)

        try:
            prompt_text = load_prompt(str(payload["prompt_version"]).strip())
            schema = load_schema(str(payload["schema_version"]).strip())
        except VersionedAssetError as exc:
            envelope.update(
                {
                    "status": "error",
                    "error": {"code": exc.code, "message": exc.message},
                    "result": None,
                }
            )
            return _jsonify_logged(payload, envelope, exc.status_code)

        envelope["prompt_loaded"] = True
        envelope["schema_loaded"] = True

        task_type = str(payload["task_type"]).strip()
        router = TaskRouter()
        heavy_task = task_type in router.implemented_task_types()

        try:
            result = _route_task(
                router,
                task_type,
                payload,
                settings=settings,
                prompt_text=prompt_text,
                schema=schema,
                heavy_task=heavy_task,
            )
        except ResourceBusyError as exc:
            envelope.update(
                {
                    "status": "error",
                    "error": {"code": exc.code, "message": exc.message},
                    "result": None,
                }
            )
            return _jsonify_logged(payload, envelope, exc.status_code)
        except UnknownTaskError as exc:
            envelope.update(
                {
                    "status": "error",
                    "error": {"code": exc.code, "message": exc.message},
                    "result": None,
                }
            )
            return _jsonify_logged(payload, envelope, 422)
        except AITaskError as exc:
            envelope.update(
                {
                    "status": "error",
                    "error": {"code": exc.code, "message": exc.message},
                    "result": None,
                }
            )
            return _jsonify_logged(payload, envelope, exc.status_code)
        except TaskNotImplementedError as exc:
            envelope.update(
                {
                    "status": "error",
                    "error": {"code": exc.code, "message": exc.message},
                    "result": None,
                }
            )
            return _jsonify_logged(payload, envelope, 501)

        metadata = build_run_metadata(
            request_id=request_id,
            payload=payload,
            model_used=settings.model,
            provider=settings.provider,
            result=result,
        )
        envelope = _response_envelope(payload, settings, metadata=metadata)
        envelope["prompt_loaded"] = True
        envelope["schema_loaded"] = True
        envelope.update({"status": "ok", "result": result})
        return _jsonify_logged(payload, envelope, 200)
    except Exception as exc:
        return jsonify(_internal_error_payload(exc)), 500


def _route_task(
    router: TaskRouter,
    task_type: str,
    payload: dict[str, Any],
    *,
    settings,
    prompt_text: str,
    schema: dict[str, Any],
    heavy_task: bool,
) -> dict[str, Any]:
    """Route a task, holding the local-model lock only for heavy tasks.

    Heavy (implemented, model-backed) tasks acquire the one-at-a-time lock and
    raise ``ResourceBusyError`` if it is already held. Recognised-but-unimplemented
    and unknown task types do not touch the lock and keep their existing behaviour.
    """
    if not heavy_task:
        return router.route(
            task_type,
            payload,
            settings=settings,
            prompt_text=prompt_text,
            schema=schema,
        )
    with MODEL_RESOURCE_LOCK.guard():
        return router.route(
            task_type,
            payload,
            settings=settings,
            prompt_text=prompt_text,
            schema=schema,
        )


def _ai_busy_diagnostics_payload(settings, exc: ResourceBusyError) -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "status": "error",
        "provider": settings.provider,
        "model_configured": settings.model,
        "model_used": settings.model,
        "backend_reachable": True,
        "model_available": True,
        "response_text": None,
        "error": {"code": exc.code, "message": exc.message},
    }


def _backend_status(settings) -> dict:
    try:
        client = OllamaModelClient(settings)
    except ModelClientError as exc:
        return {
            "provider_loaded": False,
            "backend_reachable": False,
            "model_available": False,
            "error": exc.message,
        }

    status = client.backend_status()
    return {
        "provider_loaded": True,
        "backend_reachable": bool(status.get("backend_reachable")),
        "model_available": bool(status.get("model_available")),
        "error": status.get("error"),
    }


def _validate_run_request(payload: dict[str, Any]) -> dict[str, str] | None:
    required = ("task_type", "job_id", "input", "prompt_version", "schema_version")
    for field in required:
        if field not in payload:
            return {
                "code": "INVALID_REQUEST",
                "message": f"Missing required field: {field}.",
            }

    for field in ("task_type", "job_id", "prompt_version", "schema_version"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            return {
                "code": "INVALID_REQUEST",
                "message": f"{field} must be a non-empty string.",
            }

    if not isinstance(payload.get("input"), dict):
        return {
            "code": "INVALID_REQUEST",
            "message": "input must be a JSON object.",
        }

    return None


def _response_envelope(
    payload: dict[str, Any],
    settings,
    *,
    metadata: RunMetadata | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    metadata_fields = metadata.as_dict() if metadata is not None else {
        "request_id": request_id,
        "input_hash": None,
        "output_hash": None,
        "reusable_result_key": None,
    }
    return {
        "task_type": _string_or_none(payload.get("task_type")),
        "job_id": _string_or_none(payload.get("job_id")),
        "funnel_id": _string_or_none(payload.get("funnel_id")),
        "model_used": settings.model,
        "provider": settings.provider,
        "prompt_version": _string_or_none(payload.get("prompt_version")),
        "schema_version": _string_or_none(payload.get("schema_version")),
        **metadata_fields,
    }


def _jsonify_logged(request_payload: dict[str, Any], response_payload: dict[str, Any], status_code: int):
    log_result = DECISION_LOGGER.write(
        request_payload=request_payload,
        response_payload=response_payload,
    )
    if not log_result.ok and log_result.warning is not None:
        response_payload = dict(response_payload)
        warnings = list(response_payload.get("warnings") or [])
        warnings.append(log_result.warning)
        response_payload["warnings"] = warnings
    return jsonify(response_payload), status_code


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip()
    return None


def _internal_error_payload(exc: Exception) -> dict:
    return {
        "service": "error",
        "status": "error",
        "provider": None,
        "model_configured": None,
        "backend_reachable": False,
        "model_available": False,
        "error": f"ai-service internal error: {exc}",
    }


if __name__ == "__main__":
    import sys
    from pathlib import Path

    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        scripts_dir = candidate / "scripts"
        if (scripts_dir / "http_access_log.py").is_file():
            text = str(scripts_dir)
            if text not in sys.path:
                sys.path.insert(0, text)
            break
    from http_access_log import configure_quiet_http_access_logging

    configure_quiet_http_access_logging(service_label="ai-service")
    settings = load_settings()
    app.run(host=settings.service_host, port=settings.service_port, debug=settings.service_debug)
