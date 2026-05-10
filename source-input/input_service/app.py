"""Flask HTTP entrypoint for the Mk1 input service.

Exposes:

    POST /run-funnel    body: {"funnel_id": "..."}
    GET  /healthz       liveness probe

Concurrency: only one ``/run-funnel`` may execute at a time. A second
concurrent request returns HTTP 409 with a structured ``already_running``
response so n8n can react cleanly.

Optional shared-secret auth: set the ``INPUT_SERVICE_SECRET`` env var and
n8n will need to send the same value as the ``X-Input-Service-Secret``
header. If the env var is unset, no auth is enforced (mk1 localhost use).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from flask import Flask, jsonify, request

from input_service import paths
from input_service.runner import run_funnel


logging.basicConfig(
    level=os.environ.get("INPUT_SERVICE_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("input_service.app")


_RUN_LOCK = threading.Lock()


def _failed(funnel_id: str | None, error: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"success": False, "status": "failed", "error": error}
    if funnel_id:
        payload["funnel_id"] = funnel_id
    return payload


def _check_secret() -> tuple[dict, int] | None:
    expected = os.environ.get("INPUT_SERVICE_SECRET", "").strip()
    if not expected:
        return None
    provided = (request.headers.get("X-Input-Service-Secret") or "").strip()
    if provided != expected:
        return _failed(None, "unauthorized: missing or invalid X-Input-Service-Secret"), 401
    return None


def create_app() -> Flask:
    paths.ensure_dirs()
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "service": "input_service"})

    @app.post("/run-funnel")
    def run_funnel_endpoint():
        # Auth (optional)
        auth_err = _check_secret()
        if auth_err is not None:
            body, code = auth_err
            return jsonify(body), code

        # Parse body
        try:
            data = request.get_json(force=True, silent=False) or {}
        except Exception as exc:
            return jsonify(_failed(None, f"invalid_json: {exc}")), 400

        if not isinstance(data, dict):
            return jsonify(_failed(None, "invalid_body: expected JSON object")), 400

        funnel_id = data.get("funnel_id")
        if not funnel_id or not isinstance(funnel_id, str):
            return jsonify(_failed(None, "missing_funnel_id")), 400

        # Single-run lock
        acquired = _RUN_LOCK.acquire(blocking=False)
        if not acquired:
            return (
                jsonify(
                    {
                        "success": False,
                        "status": "already_running",
                        "funnel_id": funnel_id,
                        "error": "another run is already in progress",
                    }
                ),
                409,
            )

        try:
            log.info("run_funnel start funnel_id=%s", funnel_id)
            result = run_funnel(funnel_id)
            log.info("run_funnel end funnel_id=%s status=%s", funnel_id, result.get("status"))
            http_code = 200 if result.get("success") else 500
            # For "no_input_available" we still return 200 (success=true).
            return jsonify(result), http_code
        except Exception as exc:  # pragma: no cover - last-resort guard
            log.exception("run_funnel crashed")
            return jsonify(_failed(funnel_id, f"unexpected_error: {exc}")), 500
        finally:
            _RUN_LOCK.release()

    return app


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("INPUT_SERVICE_HOST", "127.0.0.1")
    port = int(os.environ.get("INPUT_SERVICE_PORT", "5060"))
    debug = os.environ.get("INPUT_SERVICE_DEBUG", "0") == "1"
    log.info("Starting input_service on %s:%s (debug=%s)", host, port, debug)
    app.run(host=host, port=port, debug=debug, threaded=True)
