from __future__ import annotations

import hmac
import logging
import os
import threading
import time
from typing import Any

from flask import Flask, jsonify, request

from . import PIPELINE_NAME
from .config import load_settings, runtime_environment, upload_mode
from .publisher import upload_one_job
from .service import (
    backfill_legacy_rows,
    cancel_upload_job,
    find_stalled_jobs,
    load_job_payload_from_path,
    make_store,
    plan_due_upload_jobs,
    plan_upload_job,
    publish_due,
    register_and_process_from_payload,
    register_from_payload,  # noqa: F401  (kept for backward-compat import surface)
    reschedule_upload_job,
    retry_upload_job,
    route_and_prepare_upload_job,
    schedule_due_upload_jobs,
    schedule_upload_job,
    upload_due,
)
from .config import load_channel_profiles

app = Flask(__name__)
log = logging.getLogger("output_funnel.app")

_UPLOAD_WORKER_STARTED = False
_UPLOAD_WORKER_LOCK = threading.Lock()
_UPLOAD_WORKER_MIN_INTERVAL_SEC = 15

_PLAN_WORKER_STARTED = False
_PLAN_WORKER_LOCK = threading.Lock()
# Planning is cheap; allow a faster tick than uploads but never busy-loop.
_PLAN_WORKER_MIN_INTERVAL_SEC = 30


def _store():
    return make_store(load_settings())


def _payload() -> dict[str, Any]:
    data = request.get_json(silent=True) if request.is_json else {}
    return data if isinstance(data, dict) else {}


def _fail(message: str, *, status_code: int = 400):
    return jsonify({"success": False, "pipeline": PIPELINE_NAME, "error": message}), status_code


def _check_secret() -> tuple[Any, int] | None:
    expected = os.environ.get("OUTPUT_FUNNEL_SECRET", "").strip()
    if not expected:
        return None
    provided = (request.headers.get("X-Output-Funnel-Secret") or "").strip()
    if not hmac.compare_digest(provided, expected):
        return _fail(
            "unauthorized: missing or invalid X-Output-Funnel-Secret",
            status_code=401,
        )
    return None


@app.before_request
def _require_output_funnel_secret():
    if request.endpoint == "healthz":
        return None
    return _check_secret()


def _log_schedule_result(result: dict[str, Any]) -> None:
    processing = result.get("processing") if isinstance(result.get("processing"), dict) else {}
    schedule = processing.get("schedule") if isinstance(processing.get("schedule"), dict) else {}
    schedule_results = schedule.get("results") if isinstance(schedule.get("results"), list) else []
    for item in schedule_results:
        if not isinstance(item, dict):
            continue
        if item.get("planned") is True or item.get("scheduled") is True:
            print(
                "[output-funnel] planned "
                f"upload_job_id={item.get('upload_job_id')} "
                f"publish_at={item.get('publish_at') or item.get('platform_publish_at')} "
                f"upload_at={item.get('upload_at')} "
                f"upload_deadline={item.get('upload_deadline')}",
                flush=True,
            )
        else:
            print(
                "[output-funnel] plan skipped "
                f"upload_job_id={item.get('upload_job_id')} "
                f"reason={item.get('reason')}",
                flush=True,
            )


def _log_upload_result(result: dict[str, Any]) -> None:
    results = result.get("results") if isinstance(result.get("results"), list) else []
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("uploaded") is True:
            print(
                f"[output-funnel env={runtime_environment()} upload_mode={item.get('upload_mode') or upload_mode()}] uploaded "
                f"upload_job_id={item.get('upload_job_id')} "
                f"platform_video_id={item.get('platform_video_id') or item.get('platform_asset_id')} "
                f"publish_at={item.get('publish_at')}",
                flush=True,
            )
        else:
            print(
                f"[output-funnel env={runtime_environment()} upload_mode={item.get('upload_mode') or upload_mode()}] upload skipped "
                f"upload_job_id={item.get('upload_job_id')} "
                f"reason={item.get('reason') or item.get('status')}",
                flush=True,
            )


@app.get("/healthz")
def healthz():
    store = _store()
    return jsonify(
        {
            "success": True,
            "pipeline": PIPELINE_NAME,
            "environment": runtime_environment(),
            "upload_mode": upload_mode(),
            "database_path": store.db_path,
        }
    )


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


@app.post("/queue/<int:upload_job_id>/plan")
def plan_queue_item(upload_job_id: int):
    result = plan_upload_job(upload_job_id, store=_store())
    return jsonify(
        {
            "success": bool(result.get("planned")),
            "pipeline": PIPELINE_NAME,
            **result,
        }
    )


@app.post("/queue/<int:upload_job_id>/schedule")
def schedule_queue_item(upload_job_id: int):
    """Deprecated: alias of /queue/<id>/plan."""
    log.info("DEPRECATED route /queue/<id>/schedule called; alias of /plan")
    result = schedule_upload_job(upload_job_id, store=_store())
    return jsonify(
        {
            "success": bool(result.get("planned") or result.get("scheduled")),
            "pipeline": PIPELINE_NAME,
            **result,
        }
    )


@app.post("/queue/plan-due")
def plan_due_route():
    data = _payload()
    limit = int(data["limit"]) if data.get("limit") is not None else None
    result = plan_due_upload_jobs(store=_store(), limit=limit)
    _log_schedule_result({"processing": {"schedule": result}})
    return jsonify({"success": True, "pipeline": PIPELINE_NAME, **result})


@app.post("/queue/schedule-due")
def schedule_due():
    """Deprecated: alias of /queue/plan-due."""
    log.info("DEPRECATED route /queue/schedule-due called; alias of /plan-due")
    data = _payload()
    limit = int(data["limit"]) if data.get("limit") is not None else None
    result = schedule_due_upload_jobs(store=_store(), limit=limit)
    _log_schedule_result({"processing": {"schedule": result}})
    return jsonify({"success": True, "pipeline": PIPELINE_NAME, **result})


@app.post("/queue/upload-due")
def upload_due_route():
    from .control_gate import uploads_paused

    if uploads_paused():
        return jsonify(
            {
                "success": False,
                "pipeline": PIPELINE_NAME,
                "error": "uploads_paused: uploads are stopped by operator controls",
                "count": 0,
                "uploaded": 0,
            }
        ), 503
    data = _payload()
    limit = int(data.get("limit") or 10)
    result = upload_due(store=_store(), limit=limit)
    _log_upload_result(result)
    return jsonify({"success": True, "pipeline": PIPELINE_NAME, **result})


@app.post("/queue/publish-due")
def publish_due_route():
    """Deprecated: alias of /queue/upload-due.

    Kept so existing n8n flows and scripts continue to work. The behaviour
    is now to upload videos before their planned public publish time, with
    YouTube's native `publishAt` set to the planned `publish_at`.
    """
    log.warning(
        "DEPRECATED route /queue/publish-due called; rerouting to upload-due. "
        "Update callers to POST /queue/upload-due."
    )
    data = _payload()
    limit = int(data.get("limit") or 10)
    result = publish_due(store=_store(), limit=limit)
    _log_upload_result(result)
    return jsonify({"success": True, "pipeline": PIPELINE_NAME, **result})


@app.post("/queue/<int:upload_job_id>/retry")
def retry_queue_item(upload_job_id: int):
    result = retry_upload_job(upload_job_id, store=_store())
    return jsonify({"success": bool(result.get("retry")), "pipeline": PIPELINE_NAME, **result})


@app.post("/queue/<int:upload_job_id>/upload")
def upload_queue_item(upload_job_id: int):
    from .control_gate import uploads_paused

    if uploads_paused():
        return jsonify(
            {
                "success": False,
                "pipeline": PIPELINE_NAME,
                "error": "uploads_paused: uploads are stopped by operator controls",
            }
        ), 503
    result = upload_one_job(_store(), upload_job_id, profiles=load_channel_profiles())
    _log_upload_result({"results": [result]})
    return jsonify({"success": bool(result.get("uploaded")), "pipeline": PIPELINE_NAME, **result})


@app.post("/queue/<int:upload_job_id>/cancel")
def cancel_queue_item(upload_job_id: int):
    result = cancel_upload_job(upload_job_id, store=_store())
    return jsonify({"success": bool(result.get("cancelled")), "pipeline": PIPELINE_NAME, **result})


@app.post("/queue/<int:upload_job_id>/reschedule")
def reschedule_queue_item(upload_job_id: int):
    data = _payload()
    publish_at = str(data.get("publish_at") or "").strip()
    if not publish_at:
        return _fail("publish_at is required")
    result = reschedule_upload_job(upload_job_id, publish_at, store=_store())
    return jsonify({"success": bool(result.get("rescheduled")), "pipeline": PIPELINE_NAME, **result})


@app.post("/admin/backfill-legacy")
def backfill_route():
    """One-off backfill for pre-v2 rows (idempotent)."""
    result = backfill_legacy_rows(store=_store())
    return jsonify({"success": True, "pipeline": PIPELINE_NAME, **result})


@app.get("/admin/stalled-jobs")
def stalled_jobs_route():
    """Report upload_jobs stuck in registered/routed/uploading.

    Drives the watchdog: a non-zero ``count`` is an autonomy alarm. Thresholds
    live in ``settings.json`` under ``stalled_jobs``; ops-ui already exposes a
    related set of stuck heuristics on its `/recovery` page.
    """
    try:
        limit = int(request.args.get("limit") or "100")
    except ValueError:
        return _fail("limit must be an integer")
    result = find_stalled_jobs(store=_store(), limit=limit)
    return jsonify({"success": True, "pipeline": PIPELINE_NAME, **result})


@app.get("/admin/last-upload")
def last_upload_route():
    """Return the most recent successful upload timestamp + pending queue size.

    Drives the watchdog's "pipeline produced something recently" assertion.
    Without this, a healthy-looking system can silently stop uploading for
    days (cookies expired, OAuth dead, OpenAI quota, etc.) and nothing
    notices. Returns ``last_upload_at: null`` if nothing has ever uploaded.
    """
    store = _store()
    with store.connect() as conn:
        row = conn.execute(
            """
            SELECT MAX(COALESCE(uploaded_at, updated_at)) AS last_upload_at
            FROM upload_jobs
            WHERE status IN ('uploaded_scheduled', 'published')
            """
        ).fetchone()
        pending_row = conn.execute(
            """
            SELECT COUNT(*) AS pending
            FROM upload_jobs
            WHERE status IN ('registered', 'routed', 'planned', 'pending_upload')
            """
        ).fetchone()
    return jsonify(
        {
            "success": True,
            "pipeline": PIPELINE_NAME,
            "last_upload_at": row["last_upload_at"] if row else None,
            "pending_count": int(pending_row["pending"]) if pending_row else 0,
        }
    )


def _resolve_upload_worker_config(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve effective upload-worker config from settings + env.

    Lookup order:
      1. ``automation.upload_worker`` block (preferred home for the toggle)
      2. top-level ``upload_worker`` block (legacy / alternative)
      3. environment variables: ``OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED``,
         ``OUTPUT_FUNNEL_UPLOAD_WORKER_INTERVAL``,
         ``OUTPUT_FUNNEL_AUTO_UPLOAD_LIMIT`` (already used by upload_due)
    """
    cfg = settings or load_settings()
    automation_cfg = cfg.get("automation") if isinstance(cfg.get("automation"), dict) else {}
    worker_cfg: dict[str, Any] = {}
    top_level = cfg.get("upload_worker")
    if isinstance(top_level, dict):
        worker_cfg = {**worker_cfg, **top_level}
    nested = automation_cfg.get("upload_worker") if isinstance(automation_cfg, dict) else None
    if isinstance(nested, dict):
        worker_cfg = {**worker_cfg, **nested}

    env_enabled = os.environ.get("OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED", "").strip().lower()
    if env_enabled:
        enabled = env_enabled in ("1", "true", "yes", "on")
    else:
        enabled = bool(worker_cfg.get("enabled", False))

    raw_interval = (
        os.environ.get("OUTPUT_FUNNEL_UPLOAD_WORKER_INTERVAL")
        or worker_cfg.get("interval_seconds")
        or 60
    )
    try:
        interval = int(raw_interval)
    except (TypeError, ValueError):
        interval = 60
    interval = max(_UPLOAD_WORKER_MIN_INTERVAL_SEC, interval)

    raw_limit = (
        os.environ.get("OUTPUT_FUNNEL_AUTO_UPLOAD_LIMIT")
        or automation_cfg.get("upload_limit")
        or 1
    )
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 1
    limit = max(1, limit)

    return {"enabled": enabled, "interval_seconds": interval, "limit": limit}


def _resolve_plan_worker_config(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve effective plan-worker config from settings + env.

    Same lookup shape as :func:`_resolve_upload_worker_config`. Env vars:
    ``OUTPUT_FUNNEL_PLAN_WORKER_ENABLED``,
    ``OUTPUT_FUNNEL_PLAN_WORKER_INTERVAL``,
    ``OUTPUT_FUNNEL_AUTO_SCHEDULE_LIMIT`` (already used by plan_due).
    """
    cfg = settings or load_settings()
    automation_cfg = cfg.get("automation") if isinstance(cfg.get("automation"), dict) else {}
    worker_cfg: dict[str, Any] = {}
    top_level = cfg.get("plan_worker")
    if isinstance(top_level, dict):
        worker_cfg = {**worker_cfg, **top_level}
    nested = automation_cfg.get("plan_worker") if isinstance(automation_cfg, dict) else None
    if isinstance(nested, dict):
        worker_cfg = {**worker_cfg, **nested}

    env_enabled = os.environ.get("OUTPUT_FUNNEL_PLAN_WORKER_ENABLED", "").strip().lower()
    if env_enabled:
        enabled = env_enabled in ("1", "true", "yes", "on")
    else:
        enabled = bool(worker_cfg.get("enabled", False))

    raw_interval = (
        os.environ.get("OUTPUT_FUNNEL_PLAN_WORKER_INTERVAL")
        or worker_cfg.get("interval_seconds")
        or 300
    )
    try:
        interval = int(raw_interval)
    except (TypeError, ValueError):
        interval = 300
    interval = max(_PLAN_WORKER_MIN_INTERVAL_SEC, interval)

    raw_limit = (
        os.environ.get("OUTPUT_FUNNEL_AUTO_SCHEDULE_LIMIT")
        or automation_cfg.get("schedule_limit")
        or 50
    )
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, limit)

    return {"enabled": enabled, "interval_seconds": interval, "limit": limit}


def start_plan_worker(
    *,
    settings: dict[str, Any] | None = None,
    plan_fn: Any = None,
    sleep_fn: Any = None,
) -> threading.Thread | None:
    """Spawn the background plan worker thread, if enabled.

    The plan worker exists so a one-off failed handoff (e.g. transient network
    blip between video-automation and output-funnel) does not strand clips in
    ``registered`` / ``routed``. It periodically calls
    :func:`plan_due_upload_jobs`, which is idempotent on already-planned rows.

    Idempotent across calls; returns the thread (or None if disabled / already
    running). ``plan_fn`` and ``sleep_fn`` are injected for testing.
    """
    global _PLAN_WORKER_STARTED
    cfg = _resolve_plan_worker_config(settings)
    if not cfg["enabled"]:
        log.info("plan worker disabled")
        return None

    with _PLAN_WORKER_LOCK:
        if _PLAN_WORKER_STARTED:
            log.info("plan worker already running; ignoring start request")
            return None
        _PLAN_WORKER_STARTED = True

    interval = cfg["interval_seconds"]
    limit = cfg["limit"]
    do_plan = plan_fn or (lambda: plan_due_upload_jobs(store=_store(), limit=limit))
    do_sleep = sleep_fn or time.sleep

    def _loop() -> None:
        log.info(
            "plan_worker started env=%s upload_mode=%s interval=%ds limit=%d",
            runtime_environment(),
            upload_mode(),
            interval,
            limit,
        )
        print(
            f"[output-funnel env={runtime_environment()} upload_mode={upload_mode()}] plan_worker started interval={interval}s limit={limit}",
            flush=True,
        )
        while True:
            try:
                result = do_plan()
                if isinstance(result, dict) and result.get("count"):
                    _log_schedule_result({"processing": {"schedule": result}})
            except Exception:
                log.exception("plan worker tick failed")
            try:
                do_sleep(interval)
            except Exception:
                log.exception("plan worker sleep interrupted; exiting loop")
                return

    thread = threading.Thread(target=_loop, name="output_funnel_plan_worker", daemon=True)
    thread.start()
    return thread


def start_upload_worker(
    *,
    settings: dict[str, Any] | None = None,
    upload_fn: Any = None,
    sleep_fn: Any = None,
) -> threading.Thread | None:
    """Spawn the background upload worker thread, if enabled.

    Idempotent: a second call while the worker is running is a no-op.
    Returns the spawned thread (or None if disabled / already running).

    ``upload_fn`` and ``sleep_fn`` are injected for testing; production
    code uses :func:`upload_due` and :func:`time.sleep` respectively.
    """
    global _UPLOAD_WORKER_STARTED
    cfg = _resolve_upload_worker_config(settings)
    if not cfg["enabled"]:
        log.info("upload worker disabled")
        return None

    with _UPLOAD_WORKER_LOCK:
        if _UPLOAD_WORKER_STARTED:
            log.info("upload worker already running; ignoring start request")
            return None
        _UPLOAD_WORKER_STARTED = True

    interval = cfg["interval_seconds"]
    limit = cfg["limit"]
    do_upload = upload_fn or (lambda: upload_due(store=_store(), limit=limit))
    do_sleep = sleep_fn or time.sleep

    def _loop() -> None:
        log.info(
            "upload_worker started env=%s upload_mode=%s interval=%ds limit=%d",
            runtime_environment(),
            upload_mode(),
            interval,
            limit,
        )
        print(
            f"[output-funnel env={runtime_environment()} upload_mode={upload_mode()}] upload_worker started interval={interval}s limit={limit}",
            flush=True,
        )
        while True:
            try:
                from .control_gate import uploads_paused

                if uploads_paused():
                    log.info("upload worker tick skipped env=%s upload_mode=%s reason=uploads_paused", runtime_environment(), upload_mode())
                else:
                    result = do_upload()
                    if isinstance(result, dict) and result.get("count"):
                        _log_upload_result(result)
            except Exception:
                log.exception("upload worker tick failed")
            try:
                do_sleep(interval)
            except Exception:
                log.exception("upload worker sleep interrupted; exiting loop")
                return

    thread = threading.Thread(target=_loop, name="output_funnel_upload_worker", daemon=True)
    thread.start()
    return thread


def main() -> None:
    cfg = load_settings()
    mode = upload_mode()
    start_plan_worker(settings=cfg)
    start_upload_worker(settings=cfg)
    host = os.environ.get("OUTPUT_FUNNEL_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("OUTPUT_FUNNEL_PORT", "5055"))
    except ValueError:
        port = 5055
    print(
        f"[output-funnel] ENV={runtime_environment().upper()} upload_mode={mode} settings={os.environ.get('OUTPUT_FUNNEL_SETTINGS', '')} db={os.environ.get('OUTPUT_FUNNEL_DB', '')} port={port}",
        flush=True,
    )
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
