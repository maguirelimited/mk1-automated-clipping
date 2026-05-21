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

import json
import logging
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from input_service import paths
from input_service.funnel_loader import FunnelInvalidError, list_funnels
from input_service.clipping_client import probe_clipping_health, video_automation_base_url
from input_service.runner import emit_progress, run_funnel


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

    @app.get("/doctor")
    def doctor():
        auth_err = _check_secret()
        if auth_err is not None:
            body, code = auth_err
            return jsonify(body), code

        checks: list[dict[str, object]] = []

        def _check(name: str, ok: bool, detail: str):
            checks.append({"name": name, "ok": ok, "detail": detail})

        def _is_writable_dir(path: Path) -> bool:
            return path.is_dir() and os.access(path, os.W_OK | os.X_OK)

        _check("python_executable", bool(sys.executable), sys.executable or "unknown")
        _check("python_prefix", True, sys.prefix)
        _check(
            "python_venv",
            True,
            os.environ.get("VIRTUAL_ENV", "") or "not running inside a virtualenv",
        )
        try:
            import flask  # noqa: F401

            _check("flask_import", True, "import ok")
        except Exception as exc:
            _check("flask_import", False, repr(exc))
        _check("ffmpeg", bool(shutil.which("ffmpeg")), shutil.which("ffmpeg") or "Not found")
        _check("ffprobe", bool(shutil.which("ffprobe")), shutil.which("ffprobe") or "Not found")
        try:
            import yt_dlp  # noqa: F401

            _check("yt-dlp", True, "import ok")
        except Exception as exc:
            _check("yt-dlp", False, repr(exc))

        cookies_browser = os.environ.get("YT_DLP_COOKIES_FROM_BROWSER", "").strip()
        cookies_raw = os.environ.get("YT_DLP_COOKIES_PATH", "").strip()
        if cookies_browser:
            _check(
                "yt_dlp_cookie_mode",
                True,
                f"browser:{cookies_browser.split(':', 1)[0]}",
            )
        elif cookies_raw:
            _check("yt_dlp_cookie_mode", True, "cookies.txt")
        else:
            _check("yt_dlp_cookie_mode", True, "none")

        if cookies_raw:
            cp = Path(cookies_raw).expanduser()
            _check(
                "yt_dlp_cookies_file",
                cp.is_file(),
                str(cp.resolve()) if cp.is_file() else f"missing: {cp}",
            )
        else:
            _check(
                "yt_dlp_cookies_file",
                True,
                "YT_DLP_COOKIES_PATH unset (optional; helps with YouTube bot checks)",
            )

        js_runtime = os.environ.get("YT_DLP_JS_RUNTIME", "").strip().lower()
        use_deno = os.environ.get("YT_DLP_USE_DENO", "").strip().lower()
        deno_enabled = js_runtime == "deno" or use_deno in {"1", "true", "yes", "on", "deno"}
        _check(
            "yt_dlp_js_runtime",
            True,
            "deno" if deno_enabled else "yt-dlp default",
        )

        for name, path in (
            ("root", paths.ROOT),
            ("config_dir", paths.CONFIG_DIR),
            ("funnels_file", paths.FUNNELS_FILE),
            ("clipping_input_dir", paths.video_automation_inputs_dir()),
            ("ready_dir", paths.READY_DIR),
            ("rejected_dir", paths.REJECTED_DIR),
            ("state_dir", paths.STATE_DIR),
            ("tmp_dir", paths.TMP_DIR),
        ):
            exists = path.exists() if name == "funnels_file" else path.is_dir()
            detail = str(path)
            _check(f"path:{name}", exists, detail)
            if name in {"clipping_input_dir", "ready_dir", "rejected_dir", "state_dir", "tmp_dir"}:
                _check(f"path_writable:{name}", _is_writable_dir(path), str(path))

        try:
            funnels_manifest = list_funnels(include_inactive=True)
            active_count = sum(1 for f in funnels_manifest if f.get("active"))
            _check(
                "funnels_config",
                True,
                f"{len(funnels_manifest)} total, {active_count} active",
            )
        except FunnelInvalidError as exc:
            _check("funnels_config", False, str(exc))

        clip_ok, clip_detail = probe_clipping_health()
        _check("video_automation_healthz", clip_ok, clip_detail)
        _check(
            "video_automation_base_url",
            True,
            video_automation_base_url(),
        )

        all_ok = all(bool(c["ok"]) for c in checks)
        # Always HTTP 200 when this handler runs; JSON "ok" reflects readiness.
        return jsonify({"ok": all_ok, "service": "input_service", "checks": checks}), 200

    @app.get("/funnels")
    def funnels():
        auth_err = _check_secret()
        if auth_err is not None:
            body, code = auth_err
            return jsonify(body), code

        include_inactive = str(request.args.get("include_inactive", "1")).lower() not in (
            "0",
            "false",
            "no",
        )
        try:
            return jsonify(
                {
                    "success": True,
                    "status": "funnels_ready",
                    "funnels": list_funnels(include_inactive=include_inactive),
                }
            )
        except FunnelInvalidError as exc:
            return jsonify(_failed(None, f"invalid_funnels_config: {exc}")), 500

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
            emit_progress("POST /run-funnel accepted", funnel_id=funnel_id)
            result = run_funnel(funnel_id)
            emit_progress(
                f"Run finished — status={result.get('status')}",
                funnel_id=funnel_id,
            )
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
