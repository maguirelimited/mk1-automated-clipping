"""Read-only observability JSON endpoints (Operations & Observability).

Populates Phase 1 contract models from scripts/ops infrastructure.
Responses use the versioned API envelope; contract payloads live under ``data``.

``GET /health`` is a deliberate compatibility boundary: browsers (Accept: text/html)
receive the HTML doctor/readiness diagnostic page; API clients and the default
test client receive the JSON envelope unchanged. No route shadowing — one handler.
Does not expose secrets.
"""

from __future__ import annotations

import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from .config import Settings

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from observability.envelope import (  # noqa: E402
    not_found_error,
    observability_envelope,
)
from observability.artifacts import resolve_job_artifacts  # noqa: E402
from observability.index import (  # noqa: E402
    get_job_detail,
    get_run_summary,
    jobs_list_payload,
    runs_list_payload,
)
from observability.config_view import build_config_view  # noqa: E402
from observability.failures import (  # noqa: E402
    failure_group_payload,
    failures_payload,
)
from observability.outputs import (  # noqa: E402
    get_clip_detail,
    outputs_list_payload,
    resolve_clip_media_path,
)
from observability.logs import (  # noqa: E402
    build_job_logs_payload,
    build_service_logs_payload,
    default_log_limit,
)
from observability.models import SystemHealth, SystemStatus  # noqa: E402
from observability.populate import (  # noqa: E402
    build_system_health,
    build_system_status,
    services_payload,
)
from observability.schemas import CONTRACT_SCHEMA_VERSION  # noqa: E402


def _mk04_env_token(settings: Settings) -> str:
    env = (settings.environment or "dev").strip().lower()
    if env in {"production", "prod"}:
        return "prod"
    return "dev"


def _wants_health_html() -> bool:
    """Browsers prefer text/html; API clients and the test client default to JSON."""
    accept = request.accept_mimetypes
    return accept["text/html"] >= accept["application/json"] and accept["text/html"] > 0


def _render_health_page(settings: Settings):
    from .diagnostics import collect_health_reports, default_input_ledger_dir
    from .system import machine_stats, storage_usage

    report = collect_health_reports(settings)
    machine = machine_stats()
    storage = storage_usage(settings)
    return render_template(
        "health.html",
        report=report,
        machine=machine,
        storage=storage,
        input_ledger_dir=default_input_ledger_dir(),
    )


def _lines_query() -> int | None:
    raw = request.args.get("lines")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default_log_limit()


def register_observability_routes(app: Flask, settings: Settings) -> None:
    """Register observability JSON routes on the Ops UI app."""

    @app.get("/health")
    def health():
        if _wants_health_html():
            return _render_health_page(settings)
        env = _mk04_env_token(settings)
        try:
            payload = build_system_health(env).to_dict()
        except Exception as exc:
            payload = SystemHealth(
                overall="FAIL",
                environment=env,
                readiness_failures=[f"health endpoint failed: {exc.__class__.__name__}"],
            ).to_dict()
        return jsonify(observability_envelope(payload)), 200

    @app.get("/status")
    def observability_status():
        env = _mk04_env_token(settings)
        try:
            payload = build_system_status(env).to_dict()
        except Exception as exc:
            payload = SystemStatus(
                environment=env,
                state="unknown",
                current_activity=f"status endpoint failed: {exc.__class__.__name__}",
            ).to_dict()
        return jsonify(observability_envelope(payload)), 200

    @app.get("/services")
    def observability_services():
        env = _mk04_env_token(settings)
        try:
            payload = services_payload(env)
        except Exception:
            payload = {
                "environment": env,
                "checked_at": None,
                "services": [],
                "schema_version": CONTRACT_SCHEMA_VERSION,
            }
        return jsonify(observability_envelope(payload)), 200

    @app.get("/runs")
    def observability_runs():
        env = _mk04_env_token(settings)
        try:
            payload = runs_list_payload(env)
        except Exception as exc:
            payload = {
                "environment": env,
                "runs": [],
                "count": 0,
                "schema_version": CONTRACT_SCHEMA_VERSION,
                "detail": f"runs index failed: {exc.__class__.__name__}",
            }
        return jsonify(observability_envelope(payload)), 200

    @app.get("/runs/<run_id>")
    def observability_run_detail(run_id: str):
        env = _mk04_env_token(settings)
        try:
            summary = get_run_summary(env, run_id)
        except Exception:
            summary = None
        if summary is None:
            return (
                jsonify(
                    observability_envelope(
                        None,
                        error=not_found_error(resource="run", resource_id=run_id),
                    )
                ),
                404,
            )
        return jsonify(observability_envelope(summary.to_dict())), 200

    @app.get("/jobs")
    def observability_jobs():
        env = _mk04_env_token(settings)
        try:
            payload = jobs_list_payload(env)
        except Exception as exc:
            payload = {
                "environment": env,
                "jobs": [],
                "count": 0,
                "schema_version": CONTRACT_SCHEMA_VERSION,
                "detail": f"jobs index failed: {exc.__class__.__name__}",
            }
        return jsonify(observability_envelope(payload)), 200

    @app.get("/jobs/<job_id>")
    def observability_job_detail(job_id: str):
        env = _mk04_env_token(settings)
        try:
            detail = get_job_detail(env, job_id)
        except Exception:
            detail = None
        if detail is None:
            return (
                jsonify(
                    observability_envelope(
                        None,
                        error=not_found_error(resource="job", resource_id=job_id),
                    )
                ),
                404,
            )
        return jsonify(observability_envelope(detail.to_dict())), 200

    @app.get("/jobs/<job_id>/artifacts")
    def observability_job_artifacts(job_id: str):
        env = _mk04_env_token(settings)
        try:
            payload = resolve_job_artifacts(env, job_id)
        except Exception:
            payload = None
        if payload is None:
            return (
                jsonify(
                    observability_envelope(
                        None,
                        error=not_found_error(resource="job", resource_id=job_id),
                    )
                ),
                404,
            )
        return jsonify(observability_envelope(payload)), 200

    def _service_logs(mode: str):
        env = _mk04_env_token(settings)
        try:
            payload = build_service_logs_payload(env, mode, lines=_lines_query())
        except Exception as exc:
            payload = {
                "environment": env,
                "source": mode,
                "status": "unavailable",
                "limit": default_log_limit(),
                "count": 0,
                "entries": [],
                "origin": f"logs failed: {exc.__class__.__name__}",
                "schema_version": CONTRACT_SCHEMA_VERSION,
            }
        return jsonify(observability_envelope(payload)), 200

    @app.get("/logs/api")
    def observability_logs_api():
        return _service_logs("api")

    @app.get("/logs/worker")
    def observability_logs_worker():
        return _service_logs("worker")

    @app.get("/logs/ai")
    def observability_logs_ai():
        return _service_logs("ai")

    @app.get("/logs/scheduler")
    def observability_logs_scheduler():
        return _service_logs("scheduler")

    @app.get("/logs/errors")
    def observability_logs_errors():
        return _service_logs("errors")

    @app.get("/jobs/<job_id>/logs")
    def observability_job_logs(job_id: str):
        env = _mk04_env_token(settings)
        try:
            payload = build_job_logs_payload(env, job_id, lines=_lines_query())
        except Exception:
            payload = None
        if payload is None:
            return (
                jsonify(
                    observability_envelope(
                        None,
                        error=not_found_error(resource="job", resource_id=job_id),
                    )
                ),
                404,
            )
        return jsonify(observability_envelope(payload)), 200

    @app.get("/outputs")
    def observability_outputs():
        env = _mk04_env_token(settings)
        job_id = request.args.get("job_id") or None
        try:
            payload = outputs_list_payload(env, job_id=job_id)
        except Exception as exc:
            payload = {
                "environment": env,
                "outputs": [],
                "count": 0,
                "job_id": job_id,
                "schema_version": CONTRACT_SCHEMA_VERSION,
                "detail": f"outputs index failed: {exc.__class__.__name__}",
            }
        return jsonify(observability_envelope(payload)), 200

    @app.get("/outputs/<job_id>/<clip_id>")
    def observability_output_detail(job_id: str, clip_id: str):
        env = _mk04_env_token(settings)
        try:
            payload = get_clip_detail(env, job_id, clip_id)
        except Exception:
            payload = None
        if payload is None:
            return (
                jsonify(
                    observability_envelope(
                        None,
                        error=not_found_error(
                            resource="output",
                            resource_id=f"{job_id}/{clip_id}",
                        ),
                    )
                ),
                404,
            )
        return jsonify(observability_envelope(payload)), 200

    @app.get("/outputs/<job_id>/<clip_id>/media")
    def observability_output_media(job_id: str, clip_id: str):
        env = _mk04_env_token(settings)
        path = resolve_clip_media_path(env, job_id, clip_id)
        if path is None:
            return (
                jsonify(
                    observability_envelope(
                        None,
                        error=not_found_error(
                            resource="output_media",
                            resource_id=f"{job_id}/{clip_id}",
                        ),
                    )
                ),
                404,
            )
        return send_file(path, conditional=True)

    @app.get("/config/current")
    def observability_config_current():
        env = _mk04_env_token(settings)
        funnel_id = str(request.args.get("funnel_id") or "business")
        platform_id = str(request.args.get("platform_id") or "youtube")
        try:
            payload = build_config_view(
                env,
                funnel_id=funnel_id,
                platform_id=platform_id,
            )
        except Exception as exc:
            payload = {
                "environment": env,
                "validation": {
                    "state": "FAIL",
                    "message": f"config view failed: {exc.__class__.__name__}",
                    "errors": [],
                },
                "resolved_config_available": False,
                "resolved_config": {},
                "schema_version": CONTRACT_SCHEMA_VERSION,
            }
        return jsonify(observability_envelope(payload)), 200

    @app.get("/failures")
    def observability_failures():
        env = _mk04_env_token(settings)
        try:
            payload = failures_payload(env)
        except Exception as exc:
            payload = {
                "environment": env,
                "total_failures": 0,
                "failed_jobs": 0,
                "failed_runs": 0,
                "distinct_groups": 0,
                "groups": [],
                "schema_version": CONTRACT_SCHEMA_VERSION,
                "detail": f"failures index failed: {exc.__class__.__name__}",
            }
        return jsonify(observability_envelope(payload)), 200

    @app.get("/failures/<path:group_key>")
    def observability_failure_group(group_key: str):
        env = _mk04_env_token(settings)
        try:
            payload = failure_group_payload(env, group_key)
        except Exception:
            payload = None
        if payload is None:
            return (
                jsonify(
                    observability_envelope(
                        None,
                        error=not_found_error(resource="failure_group", resource_id=group_key),
                    )
                ),
                404,
            )
        return jsonify(observability_envelope(payload)), 200
