from __future__ import annotations

import json
from collections import Counter
from typing import Any

from flask import Flask, flash, redirect, render_template, request, url_for

from .config import ServiceConfig, Settings, load_settings
from .control_export import read_controls_file
from .http_client import call_json
from .publishing import (
    CANCELLABLE_STATUSES,
    MANUAL_UPLOAD_STATUSES,
    RESCHEDULABLE_STATUSES,
    distinct_filter_values,
    enrich_upload_row,
    filter_upload_jobs,
    publish_confirmation,
    queue_stats,
    upload_latency,
)
from .recovery import (
    RETRYABLE_UPLOAD_STATUSES,
    build_recovery_status,
    can_retry_upload,
    collect_dead_letter,
    collect_failed_jobs,
    is_failed_upload,
)
from .diagnostics import (
    artifact_views,
    clip_rows,
    collect_health_reports,
    default_input_ledger_dir,
    ffmpeg_output_lines,
    filter_log_text,
    funnel_context,
    load_input_ledger_record,
    pipeline_stage_rows,
    traceback_lines,
    transcript_view,
)
from .clip_review import (
    REVIEW_APPROVED,
    REVIEW_FLAGGED,
    REVIEW_PENDING,
    REVIEW_REJECTED,
    load_clip_inspection,
    load_review_queue,
    submit_operator_feedback,
)
from .control_export import HUMAN_APPROVAL_REQUIRED, PUBLISH_APPROVED_ONLY
from .funnels import (
    funnel_log_snippet,
    is_funnel_paused,
    load_funnel_board,
    set_funnel_paused,
)
from .store import ControlStore
from .system import journal_logs, machine_stats, service_action, service_status


CONTROL_INGESTION_PAUSED = "ingestion_paused"
CONTROL_UPLOADS_PAUSED = "uploads_paused"


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings()
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )
    app.secret_key = "mk04-local-ops-ui"
    store = ControlStore(settings.control_db_path, controls_file=settings.controls_file)
    store.init_db()
    store._sync_controls_file()

    @app.context_processor
    def _runtime_context() -> dict[str, Any]:
        return {"runtime": _runtime_summary(settings)}

    @app.template_filter("bytes")
    def _bytes(value: Any) -> str:
        try:
            size = float(value)
        except (TypeError, ValueError):
            return "unknown"
        for suffix in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024:
                return f"{size:.1f} {suffix}"
            size /= 1024
        return f"{size:.1f} PB"

    @app.template_filter("status_class")
    def _status_class(value: Any) -> str:
        status = str(value or "").lower()
        if status in {
            "active",
            "success",
            "succeeded",
            "uploaded_scheduled",
            "published",
            "ok",
            "live",
            "ready",
            "input_ready",
            "approved",
        }:
            return "ok"
        if status in {
            "running",
            "queued",
            "registered",
            "routed",
            "planned",
            "pending_upload",
            "uploading",
            "paused",
            "warn",
            "partial",
            "ingestion_paused",
        }:
            return "warn"
        if "fail" in status or status in {
            "inactive",
            "disabled",
            "degraded",
            "missed_upload_window",
            "bad",
            "rejected",
        } or status.endswith("_paused"):
            return "bad"
        return "muted"

    def service_by_key(key: str) -> ServiceConfig | None:
        return next((svc for svc in settings.services if svc.key == key), None)

    def _redirect_back(default: str = "dashboard") -> str:
        target = str(request.form.get("next") or request.args.get("next") or "").strip()
        if target in {"dashboard", "failed_jobs", "recovery", "publishing", "clip_review"}:
            return url_for(target)
        return url_for(default)

    @app.get("/")
    def dashboard():
        controls = _control_state(store)
        services = [_service_card(svc, settings) for svc in settings.services]
        funnels = _source_funnels(settings)
        video_jobs = _video_jobs(settings)
        upload_jobs = _upload_jobs(settings)
        return render_template(
            "dashboard.html",
            controls=controls,
            services=services,
            funnels=funnels,
            video_jobs=video_jobs,
            video_counts=_counts(video_jobs, "status"),
            upload_jobs=upload_jobs,
            upload_counts=_counts(upload_jobs, "status"),
            failures=_recent_failures(video_jobs, upload_jobs),
            actions=store.recent_actions(),
            machine=machine_stats(),
        )

    @app.get("/jobs/video/<job_id>")
    def video_job_detail(job_id: str):
        svc = service_by_key("video-automation")
        if svc is None:
            flash("video-automation is not configured.", "bad")
            return redirect(url_for("failed_jobs"))
        ok, debug, _status = call_json(svc, f"/jobs/{job_id}/debug", timeout=settings.service_timeout_sec)
        if not ok:
            flash(str(debug.get("error") or "Job debug not available"), "bad")
            return redirect(url_for("failed_jobs"))
        job = debug.get("job") if isinstance(debug.get("job"), dict) else {}
        errors = debug.get("errors") if isinstance(debug.get("errors"), list) else []
        warnings = debug.get("warnings") if isinstance(debug.get("warnings"), list) else []
        clips = debug.get("clips") if isinstance(debug.get("clips"), list) else []
        timings = debug.get("stage_timings_ms") if isinstance(debug.get("stage_timings_ms"), dict) else {}
        artifacts = debug.get("artifacts") if isinstance(debug.get("artifacts"), dict) else {}
        input_id = str(job.get("input_id") or "").strip()
        input_source = load_input_ledger_record(input_id) if input_id else None
        funnel = funnel_context(debug, job)
        job_logs = _job_journal_snippet(settings, job_id)
        return render_template(
            "job_detail.html",
            kind="clip",
            job_id=job_id,
            status=str(debug.get("status") or job.get("status") or "unknown"),
            status_rows=_video_status_rows(job, debug, funnel=funnel, input_source=input_source),
            pipeline_stages=pipeline_stage_rows(
                status=str(debug.get("status") or job.get("status") or ""),
                current_stage=str(debug.get("current_stage") or job.get("current_stage") or ""),
                stage_timings=timings,
            ),
            stage_timings=timings,
            errors=_format_json_lines(errors),
            warnings=_format_json_lines(warnings),
            tracebacks=traceback_lines(errors, warnings),
            ffmpeg_output=ffmpeg_output_lines(errors, warnings),
            clips=clip_rows(clips, artifacts),
            artifacts=artifacts,
            artifact_views=artifact_views(artifacts),
            transcript=transcript_view(debug, artifacts),
            selection_summary=debug.get("selection_summary") if isinstance(debug.get("selection_summary"), dict) else {},
            funnel=funnel,
            input_source=input_source,
            job_logs=job_logs,
            debug_json=json.dumps(debug, indent=2, default=str)[:12000],
            can_rerun=bool(input_id),
            can_retry_upload=False,
        )

    @app.get("/jobs/upload/<int:upload_job_id>")
    def upload_job_detail(upload_job_id: int):
        svc = service_by_key("output-funnel")
        if svc is None:
            flash("output-funnel is not configured.", "bad")
            return redirect(url_for("failed_jobs"))
        ok, payload, _status = call_json(
            svc,
            f"/queue/{upload_job_id}",
            timeout=settings.service_timeout_sec,
        )
        if not ok:
            flash(str(payload.get("error") or "Upload job not found"), "bad")
            return redirect(url_for("failed_jobs"))
        job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
        attempts = payload.get("attempts") if isinstance(payload.get("attempts"), list) else []
        from .recovery import can_retry_upload

        job_status = str(job.get("status") or "unknown")
        return render_template(
            "job_detail.html",
            kind="upload",
            job_id=str(upload_job_id),
            status=job_status,
            status_rows=_upload_status_rows(job, attempts),
            pipeline_stages=[],
            stage_timings={},
            errors=str(job.get("last_error") or ""),
            warnings="",
            tracebacks=[],
            ffmpeg_output=[],
            clips=[],
            artifacts={},
            artifact_views={},
            transcript={"stats": {}, "segments": [], "preview_text": ""},
            selection_summary={},
            funnel={
                "funnel_id": job.get("funnel_id"),
                "pipeline_profile": job.get("pipeline_profile"),
                "platform": job.get("platform"),
                "channel_id": job.get("channel_id"),
            },
            input_source=None,
            job_logs=_job_journal_snippet(settings, str(upload_job_id)),
            debug_json=json.dumps(payload, indent=2, default=str)[:12000],
            can_rerun=False,
            can_retry_upload=can_retry_upload(job),
            publish_confirmation=publish_confirmation(job),
            upload_latency=upload_latency(job),
            upload_attempts=attempts,
            platform_video_id=job.get("platform_video_id") or job.get("platform_asset_id"),
            platform_state=job.get("platform_state"),
            can_cancel=job_status.lower() in CANCELLABLE_STATUSES,
            can_reschedule=job_status.lower() in RESCHEDULABLE_STATUSES,
            can_manual_upload=job_status.lower() in MANUAL_UPLOAD_STATUSES
            and not _control_state(store)[CONTROL_UPLOADS_PAUSED],
            uploads_paused=_control_state(store)[CONTROL_UPLOADS_PAUSED],
        )

    @app.get("/funnels")
    def funnels_page():
        controls = _control_state(store)
        board = load_funnel_board(settings, store, ingestion_paused=controls[CONTROL_INGESTION_PAUSED])
        log_funnel_id = str(request.args.get("log") or "").strip()
        log_snippet = funnel_log_snippet(settings, log_funnel_id) if log_funnel_id else ""
        return render_template(
            "funnels.html",
            controls=controls,
            rows=board["rows"],
            trigger_history=board["trigger_history"],
            feature_matrix=board["feature_matrix"],
            channel_profiles_path=board["channel_profiles_path"],
            log_funnel_id=log_funnel_id,
            log_snippet=log_snippet,
        )

    @app.post("/funnels/<funnel_id>/pause")
    def pause_funnel(funnel_id: str):
        set_funnel_paused(store, funnel_id, True)
        store.log_action("funnel-pause", funnel_id, ok=True, message="paused")
        flash(f"Funnel {funnel_id} paused (manual runs blocked).", "ok")
        return redirect(url_for("funnels_page"))

    @app.post("/funnels/<funnel_id>/resume")
    def resume_funnel(funnel_id: str):
        set_funnel_paused(store, funnel_id, False)
        store.log_action("funnel-resume", funnel_id, ok=True, message="resumed")
        flash(f"Funnel {funnel_id} resumed.", "ok")
        return redirect(url_for("funnels_page"))

    @app.get("/clip-review")
    def clip_review():
        status_filter = str(request.args.get("status") or "").strip().lower()
        board = load_review_queue(settings, store, status_filter=status_filter)
        controls = _control_state(store)
        return render_template(
            "clip_review.html",
            controls=controls,
            clips=board["clips"],
            counts=board["counts"],
            total_clips=board.get("total_clips", 0),
            status_filter=status_filter or "all",
            service_ok=board.get("ok"),
            service_error=board.get("error"),
        )

    @app.get("/clip-review/<job_id>/<clip_id>")
    def clip_review_detail(job_id: str, clip_id: str):
        row = load_clip_inspection(settings, store, job_id=job_id, clip_id=clip_id)
        if row is None:
            flash("Clip not found or video-automation debug unavailable.", "bad")
            return redirect(url_for("clip_review"))
        controls = _control_state(store)
        return render_template(
            "clip_review_detail.html",
            clip=row,
            controls=controls,
            job_detail_url=url_for("video_job_detail", job_id=job_id),
        )

    @app.get("/clip-review/media/<job_id>/<path:clip_file>")
    def clip_review_media(job_id: str, clip_file: str):
        from flask import Response
        import os
        from urllib import error, request as urlrequest

        svc = service_by_key("video-automation")
        if svc is None:
            return Response("video-automation not configured", status=503)
        safe_name = os.path.basename(str(clip_file or ""))
        if not safe_name or safe_name != clip_file:
            return Response("invalid clip file", status=400)
        url = svc.base_url.rstrip("/") + f"/output/{safe_name}"
        headers = {"Accept": "video/*,*/*"}
        if svc.secret_env and svc.secret_header:
            secret = os.environ.get(svc.secret_env, "").strip()
            if secret:
                headers[svc.secret_header] = secret
        req = urlrequest.Request(url, headers=headers, method="GET")
        try:
            upstream = urlrequest.urlopen(req, timeout=max(settings.service_timeout_sec, 15.0))
        except error.HTTPError as exc:
            return Response(exc.read(), status=exc.code, content_type=exc.headers.get_content_type())
        except Exception as exc:
            return Response(str(exc), status=502)
        data = upstream.read()
        content_type = upstream.headers.get("Content-Type") or "video/mp4"
        return Response(data, mimetype=content_type)

    @app.post("/clip-review/<job_id>/<clip_id>/approve")
    def clip_review_approve(job_id: str, clip_id: str):
        store.set_clip_review(job_id, clip_id, status=REVIEW_APPROVED)
        store.log_action("clip-approve", f"{job_id}/{clip_id}", ok=True)
        flash(
            f"Review metadata saved: {clip_id} marked approved. Publishing is not blocked.",
            "ok",
        )
        return _clip_review_redirect(job_id, clip_id)

    @app.post("/clip-review/<job_id>/<clip_id>/reject")
    def clip_review_reject(job_id: str, clip_id: str):
        store.set_clip_review(job_id, clip_id, status=REVIEW_REJECTED)
        store.log_action("clip-reject", f"{job_id}/{clip_id}", ok=True)
        flash(
            f"Review metadata saved: {clip_id} marked rejected. Publishing is not blocked.",
            "ok",
        )
        return _clip_review_redirect(job_id, clip_id)

    @app.post("/clip-review/<job_id>/<clip_id>/flag")
    def clip_review_flag(job_id: str, clip_id: str):
        flagged = str(request.form.get("flagged") or "1").strip().lower() in {"1", "true", "on", "yes"}
        review = store.get_clip_review(job_id, clip_id)
        status = str((review or {}).get("status") or REVIEW_PENDING)
        if flagged and status == REVIEW_PENDING:
            status = REVIEW_FLAGGED
        store.set_clip_review(job_id, clip_id, status=status, flagged_high_quality=flagged)
        store.log_action("clip-flag", f"{job_id}/{clip_id}", ok=True, message="on" if flagged else "off")
        flash(
            f"Clip {clip_id}: {'flagged as high quality' if flagged else 'high-quality flag cleared'}.",
            "ok",
        )
        return _clip_review_redirect(job_id, clip_id)

    @app.post("/clip-review/<job_id>/<clip_id>/feedback")
    def clip_review_feedback(job_id: str, clip_id: str):
        notes = str(request.form.get("feedback_notes") or "").strip()
        review = store.get_clip_review(job_id, clip_id)
        status = str((review or {}).get("status") or REVIEW_PENDING)
        flagged = bool((review or {}).get("flagged_high_quality"))
        store.set_clip_review(
            job_id,
            clip_id,
            status=status,
            feedback_notes=notes,
            flagged_high_quality=flagged,
        )
        ok, message = submit_operator_feedback(
            settings,
            job_id=job_id,
            clip_id=clip_id,
            notes=notes,
            review_status=status,
            flagged=flagged,
        )
        store.log_action("clip-feedback", f"{job_id}/{clip_id}", ok=ok, message=message)
        flash(
            f"Feedback {'saved' if ok else 'stored locally; analytics API: ' + message}.",
            "ok" if ok else "bad",
        )
        return _clip_review_redirect(job_id, clip_id)

    @app.post("/clip-review/<job_id>/<clip_id>/requeue")
    def clip_review_requeue(job_id: str, clip_id: str):
        svc = service_by_key("video-automation")
        if svc is None:
            flash("video-automation is not configured.", "bad")
            return redirect(url_for("clip_review_detail", job_id=job_id, clip_id=clip_id))
        ok, detail, _status = call_json(svc, f"/jobs/{job_id}/debug", timeout=settings.service_timeout_sec)
        input_id = ""
        if ok:
            job_block = detail.get("job") if isinstance(detail.get("job"), dict) else {}
            input_id = str(job_block.get("input_id") or "").strip()
        if not input_id:
            flash(f"Cannot rerun source job {job_id}: no input_id on job report.", "bad")
            return redirect(url_for("clip_review_detail", job_id=job_id, clip_id=clip_id))
        ok, payload, status = call_json(
            svc,
            "/jobs",
            method="POST",
            payload={"input_id": input_id},
            timeout=30.0,
        )
        new_job = str(payload.get("job_id") or "")
        message = new_job or str(payload.get("error") or payload.get("message") or f"HTTP {status}")
        store.log_action("clip-rerun-source", f"{job_id}/{clip_id}", ok=ok and bool(new_job), message=message)
        flash(
            f"New clipping job from stored input: {job_id} → {message}. Does not regenerate this clip alone.",
            "ok" if ok and new_job else "bad",
        )
        if ok and new_job:
            return redirect(url_for("video_job_detail", job_id=new_job))
        return redirect(url_for("clip_review_detail", job_id=job_id, clip_id=clip_id))

    @app.post("/clip-review/controls/<control>/<state>")
    def clip_review_control(control: str, state: str):
        if control not in {HUMAN_APPROVAL_REQUIRED, PUBLISH_APPROVED_ONLY}:
            flash(f"Unknown clip-review control: {control}", "bad")
            return redirect(url_for("clip_review"))
        enabled = state == "on"
        store.set_control_bool(control, enabled)
        label = (
            "Human approval policy flag"
            if control == HUMAN_APPROVAL_REQUIRED
            else "Publish-approved-only policy flag"
        )
        store.log_action("clip-review-control", control, ok=True, message="on" if enabled else "off")
        flash(
            f"{label} set to {'on' if enabled else 'off'} in controls.json. Not enforced by services yet.",
            "ok",
        )
        return redirect(url_for("clip_review"))

    def _clip_review_redirect(job_id: str, clip_id: str):
        target = str(request.form.get("next") or "").strip()
        if target == "queue":
            return redirect(url_for("clip_review"))
        return redirect(url_for("clip_review_detail", job_id=job_id, clip_id=clip_id))

    @app.get("/publishing")
    def publishing():
        limit = min(max(int(request.args.get("limit") or 200), 1), 500)
        raw_jobs = _upload_jobs(settings, limit=limit)
        filtered = filter_upload_jobs(
            raw_jobs,
            status=str(request.args.get("status") or ""),
            platform=str(request.args.get("platform") or ""),
            channel=str(request.args.get("channel") or ""),
            q=str(request.args.get("q") or ""),
        )
        rows = [enrich_upload_row(job) for job in filtered]
        controls = _control_state(store)
        return render_template(
            "publishing.html",
            controls=controls,
            jobs=rows,
            stats=queue_stats(raw_jobs),
            filters=distinct_filter_values(raw_jobs),
            active_filters={
                "status": str(request.args.get("status") or ""),
                "platform": str(request.args.get("platform") or ""),
                "channel": str(request.args.get("channel") or ""),
                "q": str(request.args.get("q") or ""),
            },
            limit=limit,
            total_loaded=len(raw_jobs),
        )

    @app.get("/failed")
    def failed_jobs():
        video_jobs = _video_jobs(settings, limit=100)
        upload_jobs = _upload_jobs(settings, limit=200)
        failures = collect_failed_jobs(video_jobs, upload_jobs, settings=settings)
        dead_letter = collect_dead_letter(upload_jobs)
        return render_template(
            "failed_jobs.html",
            failures=failures,
            dead_letter=dead_letter,
            failed_count=len(failures),
            dead_letter_count=len(dead_letter),
        )

    @app.get("/recovery")
    def recovery():
        controls = _control_state(store)
        controls_file = read_controls_file(settings.controls_file)
        video_jobs = _video_jobs(settings, limit=100)
        upload_jobs = _upload_jobs(settings, limit=200)
        status = build_recovery_status(
            settings=settings,
            video_jobs=video_jobs,
            upload_jobs=upload_jobs,
            controls_file_exists=settings.controls_file.is_file(),
            controls_file=controls_file,
            ui_controls=controls,
        )
        retryable_uploads = [
            job for job in upload_jobs if str(job.get("status") or "").lower() in RETRYABLE_UPLOAD_STATUSES
        ]
        return render_template(
            "recovery.html",
            controls=controls,
            status=status,
            retryable_upload_count=len(retryable_uploads),
            actions=store.recent_actions(limit=10),
        )

    @app.get("/health")
    def health():
        report = collect_health_reports(settings)
        machine = machine_stats()
        return render_template(
            "health.html",
            report=report,
            machine=machine,
            input_ledger_dir=default_input_ledger_dir(),
        )

    @app.get("/logs")
    def logs():
        query = str(request.args.get("q") or "").strip()
        job_filter = str(request.args.get("job") or "").strip()
        combined_query = " ".join(part for part in (job_filter, query) if part).strip()
        units = []
        for svc in settings.services:
            raw = journal_logs(svc.systemd_unit, settings.journal_lines)
            filtered, total_lines, matched_lines = filter_log_text(raw, combined_query)
            units.append(
                {
                    "label": svc.label,
                    "unit": svc.systemd_unit,
                    "text": filtered,
                    "total_lines": total_lines,
                    "matched_lines": matched_lines,
                }
            )
        return render_template(
            "logs.html",
            units=units,
            query=query,
            job_filter=job_filter,
            journal_lines=settings.journal_lines,
        )

    @app.get("/logs/download")
    def download_logs():
        query = str(request.args.get("q") or "").strip()
        job_filter = str(request.args.get("job") or "").strip()
        combined_query = " ".join(part for part in (job_filter, query) if part).strip()
        chunks: list[str] = []
        for svc in settings.services:
            raw = journal_logs(svc.systemd_unit, settings.journal_lines)
            filtered, _, _ = filter_log_text(raw, combined_query)
            chunks.append(f"===== {svc.label} ({svc.systemd_unit}) =====\n{filtered}\n")
        body = "\n".join(chunks)
        from flask import Response

        filename = "mk04-logs.txt" if not combined_query else f"mk04-logs-{combined_query[:40].replace(' ', '_')}.txt"
        return Response(
            body,
            mimetype="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/services/<service_key>/<action>")
    def service_control(service_key: str, action: str):
        svc = service_by_key(service_key)
        if svc is None:
            flash(f"Unknown service: {service_key}", "bad")
            return redirect(_redirect_back())
        result = service_action(svc.systemd_unit, action)
        store.log_action(f"service:{action}", svc.key, ok=result.ok, message=result.message)
        flash(result.message or f"{action} {svc.label}: {'ok' if result.ok else 'failed'}", "ok" if result.ok else "bad")
        return redirect(_redirect_back())

    @app.post("/controls/<control>/<state>")
    def set_control(control: str, state: str):
        if control not in {CONTROL_INGESTION_PAUSED, CONTROL_UPLOADS_PAUSED}:
            flash(f"Unknown control: {control}", "bad")
            return redirect(_redirect_back())
        paused = state == "pause"
        store.set_control_bool(control, paused)
        label = "Emergency stop uploads" if control == CONTROL_UPLOADS_PAUSED and paused else control.replace("_", " ")
        store.log_action("control", control, ok=True, message="paused" if paused else "resumed")
        scope = (
            "blocks POST /run-funnel"
            if control == CONTROL_INGESTION_PAUSED
            else "blocks upload-due + upload worker"
        )
        flash(f"{label} {'on' if paused else 'off'} ({scope})", "ok")
        return redirect(_redirect_back("recovery"))

    @app.post("/funnels/run")
    def run_funnel():
        if store.get_control_bool(CONTROL_INGESTION_PAUSED):
            flash("Ingestion is paused globally; resume before running a funnel.", "bad")
            return redirect(_redirect_back())
        funnel_id = str(request.form.get("funnel_id") or "").strip()
        if not funnel_id:
            flash("Choose a funnel first.", "bad")
            return redirect(_redirect_back())
        next_page = str(request.form.get("next") or "").strip()
        if is_funnel_paused(store, funnel_id):
            flash(f"Funnel {funnel_id} is paused; resume it on the Funnels page first.", "bad")
            return redirect(url_for("funnels_page") if next_page == "funnels" else _redirect_back())
        svc = service_by_key("source-input")
        if svc is None:
            flash("source-input service is not configured.", "bad")
            return redirect(_redirect_back())
        ok, payload, status = call_json(
            svc,
            "/run-funnel",
            method="POST",
            payload={"funnel_id": funnel_id},
            timeout=settings.funnel_run_timeout_sec,
        )
        message = str(payload.get("status") or payload.get("error") or f"HTTP {status}")
        store.log_action("run-funnel", funnel_id, ok=ok and bool(payload.get("success", ok)), message=message)
        flash(f"Funnel {funnel_id}: {message}", "ok" if ok else "bad")
        if next_page == "funnels":
            return redirect(url_for("funnels_page"))
        return redirect(_redirect_back())

    @app.post("/output/plan-due")
    def plan_due():
        svc = service_by_key("output-funnel")
        if svc is None:
            flash("output-funnel service is not configured.", "bad")
            return redirect(_redirect_back())
        ok, payload, status = call_json(svc, "/queue/plan-due", method="POST", payload={}, timeout=20.0)
        message = str(payload.get("count") or payload.get("planned_count") or payload.get("error") or f"HTTP {status}")
        store.log_action("plan-due", "output-funnel", ok=ok, message=message)
        flash(f"Plan due uploads: {message}", "ok" if ok else "bad")
        return redirect(_redirect_back())

    @app.post("/publishing/upload/<int:upload_job_id>")
    def manual_upload(upload_job_id: int):
        if store.get_control_bool(CONTROL_UPLOADS_PAUSED):
            flash("Uploads are emergency-stopped globally.", "bad")
            return redirect(url_for("upload_job_detail", upload_job_id=upload_job_id))
        svc = service_by_key("output-funnel")
        if svc is None:
            flash("output-funnel service is not configured.", "bad")
            return redirect(url_for("upload_job_detail", upload_job_id=upload_job_id))
        ok, payload, status = call_json(
            svc,
            f"/queue/{upload_job_id}/upload",
            method="POST",
            payload={},
            timeout=120.0,
        )
        message = str(
            payload.get("platform_video_id")
            or payload.get("reason")
            or payload.get("error")
            or f"HTTP {status}"
        )
        succeeded = ok and bool(payload.get("uploaded"))
        store.log_action("manual-upload", str(upload_job_id), ok=succeeded, message=message)
        flash(
            f"Upload {upload_job_id}: {'uploaded' if succeeded else message}",
            "ok" if succeeded else "bad",
        )
        return redirect(url_for("upload_job_detail", upload_job_id=upload_job_id))

    @app.post("/publishing/cancel/<int:upload_job_id>")
    def cancel_upload(upload_job_id: int):
        svc = service_by_key("output-funnel")
        if svc is None:
            flash("output-funnel service is not configured.", "bad")
            return redirect(url_for("upload_job_detail", upload_job_id=upload_job_id))
        ok, payload, status = call_json(
            svc,
            f"/queue/{upload_job_id}/cancel",
            method="POST",
            payload={},
            timeout=20.0,
        )
        message = str(payload.get("reason") or payload.get("error") or f"HTTP {status}")
        succeeded = ok and bool(payload.get("cancelled"))
        store.log_action("cancel-upload", str(upload_job_id), ok=succeeded, message=message)
        flash(
            f"Upload {upload_job_id}: {'cancelled' if succeeded else message}",
            "ok" if succeeded else "bad",
        )
        target = str(request.form.get("next") or "").strip()
        if target == "publishing":
            return redirect(url_for("publishing"))
        return redirect(url_for("upload_job_detail", upload_job_id=upload_job_id))

    @app.post("/publishing/reschedule/<int:upload_job_id>")
    def reschedule_upload(upload_job_id: int):
        publish_at = str(request.form.get("publish_at") or "").strip()
        if not publish_at:
            flash("Enter a publish time (ISO UTC, e.g. 2026-05-24T18:00:00Z).", "bad")
            return redirect(url_for("upload_job_detail", upload_job_id=upload_job_id))
        svc = service_by_key("output-funnel")
        if svc is None:
            flash("output-funnel service is not configured.", "bad")
            return redirect(url_for("upload_job_detail", upload_job_id=upload_job_id))
        ok, payload, status = call_json(
            svc,
            f"/queue/{upload_job_id}/reschedule",
            method="POST",
            payload={"publish_at": publish_at},
            timeout=20.0,
        )
        message = str(
            payload.get("publish_at")
            or payload.get("reason")
            or payload.get("error")
            or f"HTTP {status}"
        )
        succeeded = ok and bool(payload.get("rescheduled"))
        store.log_action("reschedule-upload", str(upload_job_id), ok=succeeded, message=message)
        flash(
            f"Upload {upload_job_id}: {'rescheduled to ' + message if succeeded else message}",
            "ok" if succeeded else "bad",
        )
        target = str(request.form.get("next") or "").strip()
        if target == "publishing":
            return redirect(url_for("publishing"))
        return redirect(url_for("upload_job_detail", upload_job_id=upload_job_id))

    @app.post("/output/upload-due")
    def upload_due():
        if store.get_control_bool(CONTROL_UPLOADS_PAUSED):
            flash("Uploads are emergency-stopped globally.", "bad")
            return redirect(_redirect_back())
        svc = service_by_key("output-funnel")
        if svc is None:
            flash("output-funnel service is not configured.", "bad")
            return redirect(_redirect_back())
        ok, payload, status = call_json(svc, "/queue/upload-due", method="POST", payload={}, timeout=60.0)
        uploaded = payload.get("uploaded")
        message = str(uploaded if uploaded is not None else payload.get("count") or payload.get("error") or f"HTTP {status}")
        store.log_action("upload-due", "output-funnel", ok=ok, message=message)
        flash(f"Upload due jobs: {message}", "ok" if ok else "bad")
        return redirect(_redirect_back())

    @app.post("/recovery/upload/<int:upload_job_id>/retry")
    def retry_upload(upload_job_id: int):
        svc = service_by_key("output-funnel")
        if svc is None:
            flash("output-funnel service is not configured.", "bad")
            return redirect(url_for("failed_jobs"))
        ok, payload, status = call_json(
            svc,
            f"/queue/{upload_job_id}/retry",
            method="POST",
            payload={},
            timeout=20.0,
        )
        message = str(payload.get("reason") or payload.get("error") or f"HTTP {status}")
        succeeded = ok and bool(payload.get("success")) and bool(payload.get("retry"))
        store.log_action("retry-upload", str(upload_job_id), ok=succeeded, message=message)
        flash(
            f"Upload {upload_job_id}: {'re-queued' if succeeded else message}",
            "ok" if succeeded else "bad",
        )
        return redirect(url_for("failed_jobs"))

    @app.post("/recovery/uploads/retry-all")
    def retry_all_uploads():
        svc = service_by_key("output-funnel")
        if svc is None:
            flash("output-funnel service is not configured.", "bad")
            return redirect(url_for("failed_jobs"))
        upload_jobs = _upload_jobs(settings, limit=200)
        retried = 0
        skipped = 0
        for job in upload_jobs:
            if not can_retry_upload(job):
                skipped += 1
                continue
            job_id = int(job["id"])
            ok, payload, _status = call_json(
                svc,
                f"/queue/{job_id}/retry",
                method="POST",
                payload={},
                timeout=20.0,
            )
            if ok and bool(payload.get("retry")):
                retried += 1
            else:
                skipped += 1
        store.log_action("retry-upload-bulk", "output-funnel", ok=retried > 0, message=f"retried={retried} skipped={skipped}")
        flash(f"Bulk upload retry finished: {retried} re-queued, {skipped} skipped.", "ok" if retried else "bad")
        return redirect(url_for("failed_jobs"))

    @app.post("/recovery/video/<job_id>/retry")
    def retry_video_job(job_id: str):
        svc = service_by_key("video-automation")
        if svc is None:
            flash("video-automation service is not configured.", "bad")
            return redirect(url_for("failed_jobs"))
        ok, detail, _status = call_json(svc, f"/jobs/{job_id}", timeout=settings.service_timeout_sec)
        input_id = str(detail.get("input_id") or "").strip() if ok else ""
        if not input_id:
            ok, debug, _status = call_json(svc, f"/jobs/{job_id}/debug", timeout=settings.service_timeout_sec)
            if ok:
                job_block = debug.get("job") if isinstance(debug.get("job"), dict) else {}
                input_id = str(job_block.get("input_id") or "").strip()
        if not input_id:
            flash(f"Cannot retry {job_id}: no input_id on the job report.", "bad")
            return redirect(url_for("failed_jobs"))
        ok, payload, status = call_json(
            svc,
            "/jobs",
            method="POST",
            payload={"input_id": input_id},
            timeout=30.0,
        )
        new_job = str(payload.get("job_id") or "")
        message = new_job or str(payload.get("error") or payload.get("message") or f"HTTP {status}")
        store.log_action("retry-video", job_id, ok=ok and bool(new_job), message=message)
        flash(
            f"Rerun clipping from {job_id} → {message} (reuses stored input, no re-download)"
            if ok and new_job
            else f"Rerun failed: {message}",
            "ok" if ok and new_job else "bad",
        )
        return redirect(url_for("failed_jobs"))

    return app


def _control_state(store: ControlStore) -> dict[str, bool]:
    return {
        CONTROL_INGESTION_PAUSED: store.get_control_bool(CONTROL_INGESTION_PAUSED),
        CONTROL_UPLOADS_PAUSED: store.get_control_bool(CONTROL_UPLOADS_PAUSED),
        HUMAN_APPROVAL_REQUIRED: store.get_control_bool(HUMAN_APPROVAL_REQUIRED),
        PUBLISH_APPROVED_ONLY: store.get_control_bool(PUBLISH_APPROVED_ONLY),
    }


def _runtime_summary(settings: Settings) -> dict[str, str]:
    return {
        "environment": settings.environment,
        "upload_mode": settings.upload_mode,
        "scheduler_mode": settings.scheduler_mode,
        "code_root": str(settings.code_root),
        "config_root": str(settings.config_root),
        "runtime_root": str(settings.runtime_root),
        "log_root": str(settings.log_root),
        "controls_file": str(settings.controls_file),
        "control_db_path": str(settings.control_db_path),
    }


def _service_card(service: ServiceConfig, settings: Settings) -> dict[str, Any]:
    http_ok, health, status_code = call_json(
        service,
        "/healthz",
        timeout=settings.service_timeout_sec,
    )
    status = service_status(service.systemd_unit)
    return {
        "key": service.key,
        "label": service.label,
        "base_url": service.base_url,
        "unit": service.systemd_unit,
        "http_ok": http_ok,
        "status_code": status_code,
        "health": health,
        "systemd": status,
    }


def _source_funnels(settings: Settings) -> list[dict[str, Any]]:
    svc = next((s for s in settings.services if s.key == "source-input"), None)
    if svc is None:
        return []
    ok, payload, _status = call_json(svc, "/funnels", timeout=settings.service_timeout_sec)
    if not ok:
        return []
    funnels = payload.get("funnels")
    return funnels if isinstance(funnels, list) else []


def _video_jobs(settings: Settings, *, limit: int = 50) -> list[dict[str, Any]]:
    svc = next((s for s in settings.services if s.key == "video-automation"), None)
    if svc is None:
        return []
    ok, payload, _status = call_json(svc, f"/jobs?limit={limit}", timeout=settings.service_timeout_sec)
    if not ok:
        return []
    jobs = payload.get("jobs")
    return jobs if isinstance(jobs, list) else []


def _upload_jobs(settings: Settings, *, limit: int = 100) -> list[dict[str, Any]]:
    svc = next((s for s in settings.services if s.key == "output-funnel"), None)
    if svc is None:
        return []
    ok, payload, _status = call_json(svc, f"/queue?limit={limit}", timeout=settings.service_timeout_sec)
    if not ok:
        return []
    jobs = payload.get("jobs")
    return jobs if isinstance(jobs, list) else []


def _counts(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(Counter(str(item.get(key) or "unknown") for item in items))


def _format_json_lines(items: list[Any]) -> str:
    if not items:
        return ""
    return "\n".join(json.dumps(item, indent=2, default=str) for item in items[:20])


def _job_journal_snippet(settings: Settings, needle: str) -> str:
    if not needle:
        return ""
    svc = next((s for s in settings.services if s.key == "video-automation"), None)
    if svc is None:
        return ""
    raw = journal_logs(svc.systemd_unit, min(settings.journal_lines * 3, 300))
    filtered, _, matched = filter_log_text(raw, needle)
    if matched == 0:
        return f"No journal lines matched {needle!r} in the last {min(settings.journal_lines * 3, 300)} lines."
    return filtered


def _video_status_rows(
    job: dict[str, Any],
    debug: dict[str, Any],
    *,
    funnel: dict[str, Any] | None = None,
    input_source: dict[str, Any] | None = None,
) -> list[tuple[str, str]]:
    transcript = debug.get("transcript_stats") if isinstance(debug.get("transcript_stats"), dict) else {}
    selection = debug.get("selection_summary") if isinstance(debug.get("selection_summary"), dict) else {}
    funnel = funnel or {}
    rows = [
        ("Stage", str(debug.get("current_stage") or job.get("current_stage") or "—")),
        ("Input ID", str(job.get("input_id") or "—")),
        ("Source file", str(job.get("input_video_name") or job.get("source_video") or "—")),
        ("Funnel", str(funnel.get("funnel_id") or "—")),
        ("Pipeline profile", str(funnel.get("pipeline_profile") or "—")),
        ("Created", str(job.get("created_at") or "—")),
        ("Started", str(job.get("started_at") or debug.get("started_at") or "—")),
        ("Completed", str(job.get("completed_at") or "—")),
        ("Clips", str(len(debug.get("clips") or []))),
    ]
    if input_source and input_source.get("available"):
        rows.append(("Input ledger", f"{input_source.get('state')} ({input_source.get('funnel_id') or 'no funnel'})"))
        if input_source.get("source_url"):
            rows.append(("Source URL", str(input_source.get("source_url"))))
    elif input_source:
        rows.append(("Input ledger", str(input_source.get("reason") or "unavailable")))
    if transcript.get("available"):
        rows.append(("Transcript", f"{transcript.get('segment_count')} segments, {transcript.get('text_char_count')} chars"))
    if selection.get("available"):
        rows.append(("Selection", f"{selection.get('clip_count')} clips, {selection.get('validation_warning_count')} warnings"))
    return rows


def _upload_status_rows(job: dict[str, Any], attempts: list[dict[str, Any]]) -> list[tuple[str, str]]:
    latency = upload_latency(job)
    return [
        ("Status", str(job.get("status") or "—")),
        ("Platform", str(job.get("platform") or "—")),
        ("Channel", str(job.get("channel_id") or "—")),
        ("Publish confirmation", publish_confirmation(job)),
        ("Platform video ID", str(job.get("platform_video_id") or job.get("platform_asset_id") or "—")),
        ("Platform state", str(job.get("platform_state") or "—")),
        ("Title", str(job.get("normalized_title") or job.get("source_title") or "—")),
        ("Retries", str(job.get("attempt_count") or 0)),
        ("Publish at", str(job.get("publish_at") or job.get("platform_publish_at") or "—")),
        ("Upload at", str(job.get("upload_at") or "—")),
        ("Upload deadline", str(job.get("upload_deadline") or "—")),
        ("Upload started", str(job.get("upload_started_at") or "—")),
        ("Uploaded at", str(job.get("uploaded_at") or "—")),
        ("Upload duration", latency["upload_duration_label"]),
        ("Queue wait", latency["queue_wait_label"]),
        ("Last error", str(job.get("last_error") or "—")),
        ("Updated", str(job.get("updated_at") or "—")),
        ("Created", str(job.get("created_at") or "—")),
        ("Attempt log rows", str(len(attempts))),
    ]


def _recent_failures(video_jobs: list[dict[str, Any]], upload_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for job in video_jobs:
        if str(job.get("status") or "").lower() == "failed" or int(job.get("error_count") or 0) > 0:
            failures.append(
                {
                    "service": "video-automation",
                    "id": job.get("job_id"),
                    "status": job.get("status"),
                    "detail": f"{job.get('current_stage') or 'failed'} ({job.get('error_count') or 0} errors)",
                    "time": job.get("completed_at") or job.get("created_at"),
                }
            )
    for job in upload_jobs:
        if is_failed_upload(job):
            failures.append(
                {
                    "service": "output-funnel",
                    "id": job.get("id") or job.get("publication_id"),
                    "status": job.get("status"),
                    "detail": job.get("last_error") or job.get("source_title") or "",
                    "time": job.get("updated_at") or job.get("created_at"),
                }
            )
    failures.sort(key=lambda item: str(item.get("time") or ""), reverse=True)
    return failures[:12]


app = create_app()


if __name__ == "__main__":
    cfg = load_settings()
    app.run(host=cfg.host, port=cfg.port, debug=False)
