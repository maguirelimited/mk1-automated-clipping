from __future__ import annotations

from typing import Any

from flask import Flask, jsonify, request

from . import PIPELINE_NAME
from .config import load_settings
from .service import (
    load_job_payload_from_path,
    make_store,
    publish_due,
    register_and_process_from_payload,
    register_from_payload,
    retry_upload_job,
    route_and_prepare_upload_job,
    schedule_due_upload_jobs,
    schedule_upload_job,
)

app = Flask(__name__)


def _store():
    return make_store(load_settings())


def _payload() -> dict[str, Any]:
    data = request.get_json(silent=True) if request.is_json else {}
    return data if isinstance(data, dict) else {}


def _fail(message: str, *, status_code: int = 400):
    return jsonify({"success": False, "pipeline": PIPELINE_NAME, "error": message}), status_code


def _log_schedule_result(result: dict[str, Any]) -> None:
    processing = result.get("processing") if isinstance(result.get("processing"), dict) else {}
    schedule = processing.get("schedule") if isinstance(processing.get("schedule"), dict) else {}
    schedule_results = schedule.get("results") if isinstance(schedule.get("results"), list) else []
    for item in schedule_results:
        if not isinstance(item, dict):
            continue
        if item.get("scheduled") is True:
            print(
                "[output-funnel] scheduled "
                f"upload_job_id={item.get('upload_job_id')} "
                f"publish_at={item.get('platform_publish_at') or item.get('scheduled_at')}",
                flush=True,
            )
        else:
            print(
                "[output-funnel] schedule skipped "
                f"upload_job_id={item.get('upload_job_id')} "
                f"reason={item.get('reason')}",
                flush=True,
            )


@app.get("/healthz")
def healthz():
    store = _store()
    return jsonify({"success": True, "pipeline": PIPELINE_NAME, "database_path": store.db_path})


@app.post("/registrations/from-job")
def register_from_job():
    data = _payload()
    try:
        if data.get("payload") and isinstance(data["payload"], dict):
            job_payload = data["payload"]
        elif data.get("report_path"):
            job_payload = load_job_payload_from_path(str(data["report_path"]))
        else:
            job_payload = data
        platforms = data.get("platforms") if isinstance(data.get("platforms"), list) else None
        result = register_and_process_from_payload(job_payload, store=_store(), platforms=platforms)
        _log_schedule_result(result)
    except Exception as exc:
        return _fail(str(exc), status_code=400)
    return jsonify({"success": True, "pipeline": PIPELINE_NAME, **result})


@app.get("/queue")
def list_queue():
    status = request.args.get("status")
    try:
        limit = int(request.args.get("limit") or "100")
    except ValueError:
        return _fail("limit must be an integer")
    jobs = _store().list_upload_jobs(status=status, limit=limit)
    return jsonify({"success": True, "pipeline": PIPELINE_NAME, "count": len(jobs), "jobs": jobs})


@app.get("/queue/<int:upload_job_id>")
def get_queue_item(upload_job_id: int):
    store = _store()
    job = store.get_upload_job(upload_job_id)
    if job is None:
        return _fail("Upload job not found", status_code=404)
    attempts = store.attempts_for_job(upload_job_id)
    return jsonify({"success": True, "pipeline": PIPELINE_NAME, "job": job, "attempts": attempts})


@app.post("/queue/<int:upload_job_id>/route")
def route_queue_item(upload_job_id: int):
    result = route_and_prepare_upload_job(upload_job_id, store=_store())
    return jsonify({"success": bool(result.get("routed")), "pipeline": PIPELINE_NAME, **result})


@app.post("/queue/<int:upload_job_id>/schedule")
def schedule_queue_item(upload_job_id: int):
    result = schedule_upload_job(upload_job_id, store=_store())
    return jsonify({"success": bool(result.get("scheduled")), "pipeline": PIPELINE_NAME, **result})


@app.post("/queue/schedule-due")
def schedule_due():
    data = _payload()
    limit = int(data.get("limit") or 50)
    result = schedule_due_upload_jobs(store=_store(), limit=limit)
    _log_schedule_result({"processing": {"schedule": result}})
    return jsonify({"success": True, "pipeline": PIPELINE_NAME, **result})


@app.post("/queue/publish-due")
def publish_due_route():
    data = _payload()
    limit = int(data.get("limit") or 10)
    result = publish_due(store=_store(), limit=limit)
    return jsonify({"success": True, "pipeline": PIPELINE_NAME, **result})


@app.post("/queue/<int:upload_job_id>/retry")
def retry_queue_item(upload_job_id: int):
    result = retry_upload_job(upload_job_id, store=_store())
    return jsonify({"success": bool(result.get("retry")), "pipeline": PIPELINE_NAME, **result})


def main() -> None:
    app.run(host="127.0.0.1", port=5055)


if __name__ == "__main__":
    main()
