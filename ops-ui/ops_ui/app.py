from __future__ import annotations

import json
import os
from collections import Counter
from typing import Any

from flask import Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, url_for

from .ai_config import (
    AI_CONFIG_FIELDS,
    effective_config,
    parse_form,
    source_for,
)
from .ai_status import ai_diagnostics, ai_health
from .processing_config import (
    fields_view as processing_fields_view,
    parse_form as parse_processing_form,
)
from .post_processing_config import (
    fields_view as post_processing_fields_view,
    parse_form as parse_post_processing_form,
)
from .ai_config import (
    AI_CONFIG_FIELDS,
    effective_config,
    parse_form,
    source_for,
)
from .ai_status import ai_diagnostics, ai_health
from .processing_config import (
    fields_view as processing_fields_view,
    parse_form as parse_processing_form,
)
from .post_processing_config import (
    fields_view as post_processing_fields_view,
    parse_form as parse_post_processing_form,
)
from .environment_summary import (
    banner_text,
    build_environment_summary,
    load_job_execution_context,
    redact_dict,
)
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
    default_input_ledger_dir,
    ffmpeg_output_lines,
    filter_log_text,
    funnel_context,
    load_input_ledger_record,
    pipeline_stage_rows,
    traceback_lines,
    transcript_view,
)
from .clip_review import REVIEW_PENDING, submit_operator_feedback
from .control_export import HUMAN_APPROVAL_REQUIRED, PUBLISH_APPROVED_ONLY
from .funnel_management.acquisition_sources import (
    ACQUISITION_SOURCE_TYPE_LABELS,
    CANONICAL_ACQUISITION_SOURCE_TYPES,
    PER_SOURCE_TYPE_LABELS,
    ALLOWED_PER_SOURCE_TYPES,
    source_url_placeholder,
)
from .funnel_management.clone import (
    FunnelCloneError,
    clone_form_defaults,
    form_values_from_request as clone_form_values_from_request,
    parse_funnel_clone_form,
    save_cloned_funnel_in_registry,
    source_summary,
)
from .funnel_management.edit import (
    FunnelEditError,
    edit_form_from_funnel,
    form_values_from_request as edit_form_values_from_request,
    save_edited_funnel_in_registry,
)
from .funnel_management.create_defaults import BASELINE_TEMPLATE_ID
from .funnel_management.readiness_summary import build_simple_funnel_status, sync_outcome_message
from .funnel_management.create import (
    FunnelCreateError,
    create_funnel_in_registry,
    form_values_from_request,
    parse_funnel_create_form,
)
from .funnel_management.funnel_rule_registry_ops import list_registry_profile_ids
from .funnel_management.funnel_templates import list_funnel_templates
from .funnel_management.registry import FunnelNotFoundError, FunnelRegistry, FunnelRegistryError
from .funnel_management.schema import (
    ALLOWED_CONFIG_MANAGER_PRESETS,
    ALLOWED_DELIVERY_MODES,
    ALLOWED_PLATFORMS,
    ALLOWED_POSTING_MODES,
    ALLOWED_STATUSES,
    DEFAULT_CONFIG_MANAGER_PRESET,
    DEFAULT_MAX_VIDEOS_PER_SOURCE,
)
from .funnel_management.sync import FunnelSyncError, FunnelSynchronizer
from .funnel_management.sync_workflow import (
    FunnelSyncWorkflowError,
    build_changed_files_flash,
    build_sync_readiness_context,
    default_sync_environment,
    normalize_sync_environment,
    parse_sync_apply_form,
    resolve_sync_paths,
    sync_page_context,
)
from .funnel_management.validation import FunnelValidator
from .funnels import (
    FunnelDetailNotFoundError,
    ai_rule_registry_path,
    build_funnel_validator,
    funnel_log_snippet,
    is_funnel_paused,
    load_canonical_funnel_detail,
    load_canonical_funnel_page,
    load_funnel_board,
    set_funnel_paused,
)
from .auth.audit import AuditLogger
from .auth.routes import register_auth
from .auth.session import validate_csrf
from .controls import (
    ALL_ACTIONS,
    HIGH_RISK_ACTIONS,
    action_label,
    execute_control_action,
)
from .observability import register_observability_routes
from .lists import (
    build_job_detail_context,
    build_jobs_list_context,
    build_run_detail_context,
    build_runs_list_context,
)
from .config_ui import build_configuration_context
from .storage_ui import build_storage_context, resolve_storage_artifact
from .failures_ui import build_failure_group_context, build_failures_list_context
from .outputs_ui import build_output_detail_context, build_outputs_list_context, outputs_redirect_target
from .overview import build_overview_context
from .media import stream_clip_review_media, stream_output_clip
from .shell import build_shell_context
from .store import ControlStore
from .system import (
    cleanup_preview,
    journal_logs,
    machine_stats,
    run_retention_cleanup,
    service_action,
    service_status,
    storage_usage,
    _summarize_sweeper_output,
)


CONTROL_INGESTION_PAUSED = "ingestion_paused"
CONTROL_UPLOADS_PAUSED = "uploads_paused"


def _ensure_runtime_env(settings: Settings) -> None:
    """Align ConfigManager path resolution with this app's Settings.

    Assign (do not setdefault) so an explicitly configured app always wins over
    inherited process environment from a previous import or shell.
    """
    os.environ["MK04_RUNTIME_ROOT"] = str(settings.runtime_root)
    os.environ["MK04_ENV"] = settings.environment


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings()
    _ensure_runtime_env(settings)
    app = Flask(
        __name__,
        template_folder="../templates",
        static_folder="../static",
    )
    store = ControlStore(settings.control_db_path, controls_file=settings.controls_file)
    store.init_db()
    store._sync_controls_file()
    register_auth(app, settings=settings, store=store)
    register_observability_routes(app, settings)

    @app.context_processor
    def _runtime_context() -> dict[str, Any]:
        env_summary = build_environment_summary(settings)
        runtime = _runtime_summary(settings, env_summary)
        # Single shared fetch of /health + /status for the shell and pages.
        g.shell_context = build_shell_context(settings)
        return {
            "runtime": runtime,
            "env_summary": env_summary,
            "env_banner_text": banner_text(env_summary),
            "default_max_videos_per_source": DEFAULT_MAX_VIDEOS_PER_SOURCE,
            **g.shell_context,
        }

    @app.get("/api/environment")
    @app.get("/api/config-summary")
    def api_environment_summary():
        return jsonify(build_environment_summary(settings))

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
            "testing",
            "runnable",
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
            "warning",
            "incomplete",
            "draft",
            "pending_sync",
        }:
            return "warn"
        if "fail" in status or status in {
            "inactive",
            "disabled",
            "degraded",
            "missed_upload_window",
            "bad",
            "rejected",
            "invalid",
            "broken",
            "archived",
            "blocked",
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
    def root():
        return redirect(url_for("ops_overview"))

    @app.get("/ops")
    def ops_overview():
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        overview = build_overview_context(settings, shell=shell)
        return render_template("ops_overview.html", **overview)

    @app.post("/ops/actions/<action>")
    def ops_action(action: str):
        """Authenticated control actions — invoke existing ops layer only."""
        action = (action or "").strip()
        if action not in ALL_ACTIONS:
            flash("Unknown action.", "bad")
            return redirect(url_for("ops_overview"))

        if settings.auth_enabled and not validate_csrf(request.form.get("csrf_token")):
            flash("Invalid security token.", "bad")
            return redirect(url_for("ops_overview"))

        restart_target = str(request.form.get("restart_target") or "").strip()
        funnel_id = str(request.form.get("funnel_id") or "").strip()
        confirmed = str(request.form.get("confirm") or "").strip().lower() == "yes"

        if action in HIGH_RISK_ACTIONS and not confirmed:
            return render_template(
                "ops_action_confirm.html",
                action=action,
                action_label=action_label(action),
                restart_target=restart_target,
                funnel_id=funnel_id if action.startswith("run_pipeline") else "",
            )

        result = execute_control_action(
            settings,
            action,
            confirmed=confirmed or action not in HIGH_RISK_ACTIONS,
            restart_target=restart_target,
            funnel_id=funnel_id,
        )
        audit = AuditLogger(store)
        audit.record(
            f"control.{action}",
            target=restart_target or funnel_id or settings.environment,
            ok=result.ok,
            message=result.message,
        )
        flash(result.message, "ok" if result.ok else "bad")
        return redirect(url_for("ops_overview"))

    @app.get("/ops/runs")
    def ops_runs():
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        ctx = build_runs_list_context(
            settings,
            shell=shell,
            status=str(request.args.get("status") or ""),
            trigger=str(request.args.get("trigger") or ""),
            funnel=str(request.args.get("funnel") or ""),
        )
        return render_template("ops_runs.html", **ctx)

    @app.get("/ops/runs/<run_id>")
    def ops_run_detail(run_id: str):
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        ctx = build_run_detail_context(settings, run_id, shell=shell)
        if ctx is None:
            flash(f"Run not found: {run_id}", "bad")
            return redirect(url_for("ops_runs"))
        return render_template("ops_run_detail.html", **ctx)

    @app.get("/ops/jobs")
    def ops_jobs():
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        ctx = build_jobs_list_context(
            settings,
            shell=shell,
            state=str(request.args.get("state") or ""),
            funnel=str(request.args.get("funnel") or ""),
            platform=str(request.args.get("platform") or ""),
            run_id=str(request.args.get("run_id") or ""),
        )
        return render_template("ops_jobs.html", **ctx)

    @app.get("/ops/jobs/<job_id>")
    def ops_job_detail(job_id: str):
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        ctx = build_job_detail_context(settings, job_id, shell=shell)
        if ctx is None:
            flash(f"Job not found: {job_id}", "bad")
            return redirect(url_for("ops_jobs"))
        return render_template("ops_job_detail.html", **ctx)

    @app.get("/ops/outputs")
    def ops_outputs():
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        ctx = build_outputs_list_context(
            settings,
            shell=shell,
            run_id=str(request.args.get("run_id") or "") or None,
            job_id=str(request.args.get("job_id") or "") or None,
            funnel_id=str(request.args.get("funnel_id") or "") or None,
        )
        return render_template("ops_outputs.html", **ctx)

    @app.get("/ops/outputs/<job_id>/<clip_id>")
    def ops_output_detail(job_id: str, clip_id: str):
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        ctx = build_output_detail_context(settings, job_id, clip_id, shell=shell)
        if ctx is None:
            flash(f"Output not found: {job_id}/{clip_id}", "bad")
            return redirect(url_for("ops_outputs"))
        return render_template("ops_output_detail.html", **ctx)

    @app.get("/ops/outputs/<job_id>/<clip_id>/media")
    def ops_output_media(job_id: str, clip_id: str):
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        env_token = str(shell.get("shell_env_token") or settings.environment or "dev")
        download = str(request.args.get("download") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        return stream_output_clip(
            settings,
            env_token=env_token,
            job_id=job_id,
            clip_id=clip_id,
            download=download,
        )

    @app.get("/ops/failures")
    def ops_failures():
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        ctx = build_failures_list_context(settings, shell=shell)
        return render_template("ops_failures.html", **ctx)

    @app.get("/ops/failures/<path:group_key>")
    def ops_failure_group(group_key: str):
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        ctx = build_failure_group_context(settings, group_key, shell=shell)
        if ctx is None:
            flash(f"Failure group not found: {group_key}", "bad")
            return redirect(url_for("ops_failures"))
        return render_template("ops_failure_group.html", **ctx)

    @app.get("/ops/configuration")
    def ops_configuration():
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        ctx = build_configuration_context(
            settings,
            shell=shell,
            funnel_id=str(request.args.get("funnel_id") or "business"),
            platform_id=str(request.args.get("platform_id") or "youtube"),
        )
        return render_template("ops_configuration.html", **ctx)

    @app.get("/ops/storage")
    def ops_storage():
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        ctx = build_storage_context(settings, shell=shell)
        return render_template("ops_storage.html", **ctx)

    @app.get("/ops/storage/artifact/<kind>")
    def ops_storage_artifact(kind: str):
        """Serve allowlisted storage reports only (no filesystem browser)."""
        from flask import abort, send_file

        path = resolve_storage_artifact(settings, kind)
        if path is None:
            abort(404)
        return send_file(
            path,
            mimetype="application/json",
            as_attachment=True,
            download_name=path.name,
        )

    @app.get("/dashboard")
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
        env_summary = build_environment_summary(settings)
        execution_context = load_job_execution_context(
            job_id,
            None if env_summary.get("jobs_root") == "not_available" else str(env_summary["jobs_root"]),
            report_payload=debug,
        )
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
            execution_context=execution_context,
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

    def _funnel_redirect(funnel_id: str) -> str:
        next_page = str(request.form.get("next") or "").strip()
        if next_page == "funnel_detail":
            return url_for("funnel_detail_page", funnel_id=funnel_id)
        return url_for("funnels_page")

    @app.get("/funnels")
    def funnels_page():
        controls = _control_state(store)
        page = load_canonical_funnel_page(
            settings,
            store,
            ingestion_paused=controls[CONTROL_INGESTION_PAUSED],
        )
        log_funnel_id = str(request.args.get("log") or "").strip()
        log_snippet = funnel_log_snippet(settings, log_funnel_id) if log_funnel_id else ""
        return render_template(
            "funnels.html",
            controls=controls,
            rows=page["rows"],
            empty_registry=page["empty_registry"],
            registry_path=page["registry_path"],
            ops_available=page["ops_available"],
            trigger_history=page["trigger_history"],
            log_funnel_id=log_funnel_id,
            log_snippet=log_snippet,
        )

    def _ai_profile_form_context() -> dict[str, Any]:
        return {
            "allowed_config_manager_presets": sorted(ALLOWED_CONFIG_MANAGER_PRESETS),
            "default_config_manager_preset": DEFAULT_CONFIG_MANAGER_PRESET,
            **_acquisition_form_context(),
        }

    def _acquisition_form_context() -> dict[str, Any]:
        return {
            "acquisition_source_types": [
                {"value": value, "label": ACQUISITION_SOURCE_TYPE_LABELS[value]}
                for value in CANONICAL_ACQUISITION_SOURCE_TYPES
            ],
            "per_source_types": [
                {"value": value, "label": PER_SOURCE_TYPE_LABELS[value]}
                for value in sorted(ALLOWED_PER_SOURCE_TYPES)
            ],
            "source_url_placeholder_channel": source_url_placeholder("youtube_channel"),
            "source_url_placeholder_playlist": source_url_placeholder("youtube_playlist"),
        }

    @app.get("/funnels/new")
    def funnel_create_page():
        controls = _control_state(store)
        templates = list_funnel_templates()
        default_template = templates[0].template_id if templates else BASELINE_TEMPLATE_ID
        return render_template(
            "funnel_new.html",
            controls=controls,
            templates=templates,
            form={
                "template_id": default_template,
                "funnel_id": "",
                "display_name": "",
                "description": "",
                "category": "",
                "source_type": "youtube_channel",
                "source_urls": "",
            },
            errors=(),
            **_ai_profile_form_context(),
        )

    @app.post("/funnels/new")
    def funnel_create_submit():
        controls = _control_state(store)
        templates = list_funnel_templates()
        form_values = form_values_from_request(request.form)

        if settings.auth_enabled and not validate_csrf(request.form.get("csrf_token")):
            flash("Invalid security token.", "bad")
            return render_template(
                "funnel_new.html",
                controls=controls,
                templates=templates,
                form=form_values,
                errors=("Invalid security token.",),
                **_ai_profile_form_context(),
            )

        parsed, errors = parse_funnel_create_form(request.form)
        if parsed is None or errors:
            return render_template(
                "funnel_new.html",
                controls=controls,
                templates=templates,
                form=form_values,
                errors=tuple(errors),
                **_ai_profile_form_context(),
            )

        registry = FunnelRegistry()
        try:
            funnel = create_funnel_in_registry(parsed, registry)
        except FunnelCreateError as exc:
            return render_template(
                "funnel_new.html",
                controls=controls,
                templates=templates,
                form=form_values,
                errors=(str(exc),),
                **_ai_profile_form_context(),
            )

        flash(
            f"Created funnel {funnel.identity.display_name!r}. Next: open the funnel and click "
            f"Sync runtime config, then Run test.",
            "ok",
        )
        return redirect(url_for("funnel_detail_page", funnel_id=funnel.identity.funnel_id))

    @app.get("/funnels/<funnel_id>")
    def funnel_detail_page(funnel_id: str):
        controls = _control_state(store)
        try:
            detail = load_canonical_funnel_detail(
                funnel_id,
                settings,
                store,
                ingestion_paused=controls[CONTROL_INGESTION_PAUSED],
            )
        except FunnelDetailNotFoundError:
            abort(404)
        except FunnelRegistryError as exc:
            return render_template(
                "funnel_detail.html",
                controls=controls,
                load_error=str(exc),
                funnel_id=funnel_id,
            )
        return render_template(
            "funnel_detail.html",
            controls=controls,
            detail=detail,
            load_error=None,
            funnel_id=detail["funnel_id"],
        )

    def _load_registry_funnel(funnel_id: str):
        try:
            return FunnelRegistry().get_funnel(funnel_id)
        except FunnelNotFoundError:
            abort(404)
        except FunnelRegistryError as exc:
            flash(str(exc), "bad")
            abort(404)

    @app.get("/funnels/<funnel_id>/clone")
    def funnel_clone_page(funnel_id: str):
        controls = _control_state(store)
        source = _load_registry_funnel(funnel_id)
        return render_template(
            "funnel_clone.html",
            controls=controls,
            source=source_summary(source),
            form=clone_form_defaults(source),
            errors=(),
        )

    @app.post("/funnels/<funnel_id>/clone")
    def funnel_clone_submit(funnel_id: str):
        controls = _control_state(store)
        source = _load_registry_funnel(funnel_id)
        summary = source_summary(source)
        form_values = clone_form_values_from_request(request.form)

        if settings.auth_enabled and not validate_csrf(request.form.get("csrf_token")):
            flash("Invalid security token.", "bad")
            return render_template(
                "funnel_clone.html",
                controls=controls,
                source=summary,
                form=form_values,
                errors=("Invalid security token.",),
            )

        parsed, errors = parse_funnel_clone_form(request.form, source_funnel_id=funnel_id)
        if parsed is None or errors:
            return render_template(
                "funnel_clone.html",
                controls=controls,
                source=summary,
                form=form_values,
                errors=tuple(errors),
            )

        registry = FunnelRegistry()
        try:
            cloned = save_cloned_funnel_in_registry(source, parsed, registry)
        except FunnelCloneError as exc:
            return render_template(
                "funnel_clone.html",
                controls=controls,
                source=summary,
                form=form_values,
                errors=(str(exc),),
            )

        flash(
            f"Cloned funnel {cloned.identity.display_name!r} saved as draft in the canonical registry.",
            "ok",
        )
        return redirect(url_for("funnel_detail_page", funnel_id=cloned.identity.funnel_id))

    def _edit_form_context(form: dict, errors: tuple[str, ...] = ()) -> dict:
        config_manager_funnel_id = str(form.get("config_manager_funnel_id") or form.get("funnel_id") or "").strip()
        return {
            "form": form,
            "errors": errors,
            "allowed_statuses": sorted(ALLOWED_STATUSES),
            "allowed_platforms": sorted(ALLOWED_PLATFORMS),
            "allowed_posting_modes": sorted(ALLOWED_POSTING_MODES),
            "allowed_delivery_modes": sorted(ALLOWED_DELIVERY_MODES),
            "ai_profile_options": list_registry_profile_ids(ai_rule_registry_path()),
            "allowed_config_manager_presets": sorted(ALLOWED_CONFIG_MANAGER_PRESETS),
            "config_manager_yaml_hint": (
                f"config/funnels/{config_manager_funnel_id}.yaml"
                if config_manager_funnel_id
                else "config/funnels/<funnel_id>.yaml"
            ),
            **_acquisition_form_context(),
        }

    @app.get("/funnels/<funnel_id>/edit")
    def funnel_edit_page(funnel_id: str):
        controls = _control_state(store)
        existing = _load_registry_funnel(funnel_id)
        return render_template(
            "funnel_edit.html",
            controls=controls,
            **_edit_form_context(edit_form_from_funnel(existing)),
        )

    @app.post("/funnels/<funnel_id>/edit")
    def funnel_edit_submit(funnel_id: str):
        controls = _control_state(store)
        existing = _load_registry_funnel(funnel_id)
        form_values = edit_form_values_from_request(request.form)

        if settings.auth_enabled and not validate_csrf(request.form.get("csrf_token")):
            flash("Invalid security token.", "bad")
            return render_template(
                "funnel_edit.html",
                controls=controls,
                **_edit_form_context(form_values, ("Invalid security token.",)),
            )

        registry = FunnelRegistry()
        try:
            updated = save_edited_funnel_in_registry(existing, request.form, registry)
        except FunnelEditError as exc:
            return render_template(
                "funnel_edit.html",
                controls=controls,
                **_edit_form_context(form_values, (str(exc),)),
            )

        report = FunnelValidator().validate_funnel(updated)
        flash_message = "Funnel saved to registry. Runtime configs have not been synchronised yet."
        if report.errors or report.warnings:
            flash_message += (
                f" Saved, but validation still reports {len(report.errors)} error(s)"
                f" and {len(report.warnings)} warning(s)."
            )
        flash(flash_message, "ok")
        return redirect(url_for("funnel_detail_page", funnel_id=updated.identity.funnel_id))

    def _render_sync_page(
        funnel,
        *,
        environment: str | None = None,
        form_errors: tuple[str, ...] = (),
        applied: bool = False,
    ):
        controls = _control_state(store)
        env_errors: list[str] = list(form_errors)
        selected = environment or default_sync_environment(settings)
        try:
            selected = normalize_sync_environment(selected)
        except FunnelSyncWorkflowError as exc:
            env_errors.append(str(exc))
            selected = default_sync_environment(settings)

        env_paths = resolve_sync_paths(selected)
        report = FunnelSynchronizer(env_paths.to_target_paths()).build_plan(funnel)
        validation_report = build_funnel_validator().validate_funnel(funnel)
        return render_template(
            "funnel_sync.html",
            controls=controls,
            **sync_page_context(
                funnel_id=funnel.identity.funnel_id,
                display_name=funnel.identity.display_name,
                environment=selected,
                env_paths=env_paths,
                report=report,
                validation_report=validation_report,
                form_errors=tuple(env_errors),
                applied=applied,
            ),
        )

    @app.get("/funnels/<funnel_id>/sync")
    def funnel_sync_page(funnel_id: str):
        funnel = _load_registry_funnel(funnel_id)
        environment = request.args.get("environment")
        return _render_sync_page(funnel, environment=environment)

    @app.post("/funnels/<funnel_id>/sync")
    def funnel_sync_submit(funnel_id: str):
        funnel = _load_registry_funnel(funnel_id)

        if settings.auth_enabled and not validate_csrf(request.form.get("csrf_token")):
            flash("Invalid security token.", "bad")
            return _render_sync_page(
                funnel,
                environment=str(request.form.get("environment") or ""),
                form_errors=("Invalid security token.",),
            )

        parsed, form_errors = parse_sync_apply_form(request.form, funnel_id=funnel_id)
        if parsed is None:
            return _render_sync_page(
                funnel,
                environment=str(request.form.get("environment") or ""),
                form_errors=tuple(form_errors),
            )

        env_paths = resolve_sync_paths(parsed.environment)
        synchronizer = FunnelSynchronizer(env_paths.to_target_paths())
        preview = synchronizer.build_plan(funnel)
        if not preview.ok:
            return _render_sync_page(
                funnel,
                environment=parsed.environment,
                form_errors=("Sync is blocked by plan errors. Fix issues before applying.",),
            )

        try:
            report = synchronizer.apply(
                funnel,
                backup=parsed.backup_requested,
            )
        except FunnelSyncError as exc:
            return _render_sync_page(
                funnel,
                environment=parsed.environment,
                form_errors=(str(exc),),
            )

        validation_report = build_funnel_validator().validate_funnel(funnel)
        readiness = build_sync_readiness_context(validation_report, report)
        after_ready = readiness.get("processing_after_apply_state") == "ready"
        flash(
            sync_outcome_message(
                applied=True,
                sync_ok=report.ok,
                report=validation_report,
                after_apply_processing_ready=after_ready,
            ),
            "ok" if report.ok else "bad",
        )
        return redirect(url_for("funnel_detail_page", funnel_id=funnel.identity.funnel_id))

    @app.post("/funnels/<funnel_id>/pause")
    def pause_funnel(funnel_id: str):
        set_funnel_paused(store, funnel_id, True)
        store.log_action("funnel-pause", funnel_id, ok=True, message="paused")
        flash(f"Funnel {funnel_id} paused (manual runs blocked).", "ok")
        return redirect(_funnel_redirect(funnel_id))

    @app.post("/funnels/<funnel_id>/resume")
    def resume_funnel(funnel_id: str):
        set_funnel_paused(store, funnel_id, False)
        store.log_action("funnel-resume", funnel_id, ok=True, message="resumed")
        flash(f"Funnel {funnel_id} resumed.", "ok")
        return redirect(_funnel_redirect(funnel_id))

    # Legacy Clip Review — GET redirects to Outputs. Fake approval POST routes return
    # 410 Gone; feedback/requeue remain for now. See /ops/outputs for MK1 review.
    _CLIP_REVIEW_APPROVAL_RETIRED = (
        "Clip Review approve/reject/flag controls were retired; they did not gate "
        "publishing. Use /ops/outputs for run-centric clip review."
    )
    _CLIP_REVIEW_POLICY_CONTROLS_RETIRED = (
        "Clip Review human_approval_required and publish_approved_only toggles were "
        "retired; they were not enforced by any service."
    )

    def _clip_review_retired_gone(message: str) -> Response:
        return Response(message, status=410, mimetype="text/plain; charset=utf-8")

    @app.get("/clip-review")
    def clip_review():
        return redirect(url_for("ops_outputs"))

    @app.get("/clip-review/<job_id>/<clip_id>")
    def clip_review_detail(job_id: str, clip_id: str):
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        return redirect(outputs_redirect_target(settings, shell=shell, job_id=job_id))

    @app.get("/clip-review/media/<job_id>/<path:clip_file>")
    def clip_review_media(job_id: str, clip_file: str):
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        env_token = str(shell.get("shell_env_token") or settings.environment or "dev")
        download = str(request.args.get("download") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        return stream_clip_review_media(
            settings,
            env_token=env_token,
            job_id=job_id,
            clip_file=clip_file,
            download=download,
        )

    @app.post("/clip-review/<job_id>/<clip_id>/approve")
    def clip_review_approve(job_id: str, clip_id: str):
        del job_id, clip_id
        return _clip_review_retired_gone(_CLIP_REVIEW_APPROVAL_RETIRED)

    @app.post("/clip-review/<job_id>/<clip_id>/reject")
    def clip_review_reject(job_id: str, clip_id: str):
        del job_id, clip_id
        return _clip_review_retired_gone(_CLIP_REVIEW_APPROVAL_RETIRED)

    @app.post("/clip-review/<job_id>/<clip_id>/flag")
    def clip_review_flag(job_id: str, clip_id: str):
        del job_id, clip_id
        return _clip_review_retired_gone(_CLIP_REVIEW_APPROVAL_RETIRED)

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
            return redirect(outputs_redirect_target(settings, shell=getattr(g, "shell_context", None) or build_shell_context(settings), job_id=job_id))
        ok, detail, _status = call_json(svc, f"/jobs/{job_id}/debug", timeout=settings.service_timeout_sec)
        input_id = ""
        if ok:
            job_block = detail.get("job") if isinstance(detail.get("job"), dict) else {}
            input_id = str(job_block.get("input_id") or "").strip()
        if not input_id:
            flash(f"Cannot rerun source job {job_id}: no input_id on job report.", "bad")
            return redirect(outputs_redirect_target(settings, shell=getattr(g, "shell_context", None) or build_shell_context(settings), job_id=job_id))
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
        del control, state
        return _clip_review_retired_gone(_CLIP_REVIEW_POLICY_CONTROLS_RETIRED)

    def _clip_review_redirect(job_id: str, clip_id: str):
        shell = getattr(g, "shell_context", None) or build_shell_context(settings)
        target = outputs_redirect_target(settings, shell=shell, job_id=job_id)
        return redirect(target)

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

    def _recovery_context(*, cleanup_preview_result: dict[str, Any] | None = None) -> dict[str, Any]:
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
        return {
            "controls": controls,
            "status": status,
            "retryable_upload_count": len(retryable_uploads),
            "actions": store.recent_actions(limit=10),
            "cleanup_media_days": _env_int("MEDIA_RETENTION_DAYS", 5),
            "cleanup_metadata_days": _env_int("RETENTION_DAYS", 14),
            "cleanup_running_block": _video_jobs_running(video_jobs),
            "cleanup_preview": cleanup_preview_result,
        }

    @app.get("/recovery")
    def recovery():
        return render_template("recovery.html", **_recovery_context())

    def _effective_ai_service_url(saved: dict[str, str] | None = None) -> str:
        """URL for ai-service health/diagnostics probes (matches AI settings effective value)."""
        if saved is None:
            saved = store.get_ai_config()
        source = source_for("ai_service_url", saved)
        if source in {"ui", "env"}:
            return str(effective_config(saved)["ai_service_url"])
        return settings.ai_service_url

    def _ai_settings_context(diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
        saved = store.get_ai_config()
        effective = effective_config(saved)
        probe_url = _effective_ai_service_url(saved)
        fields = [
            {
                "name": field.name,
                "label": field.label,
                "kind": field.kind,
                "choices": field.choices,
                "help": field.help,
                "value": effective.get(field.name),
                "source": source_for(field.name, saved),
                "env_var": field.env_var,
            }
            for field in AI_CONFIG_FIELDS
        ]
        health = ai_health(probe_url, timeout=settings.service_timeout_sec)
        return {
            "ai_fields": fields,
            "ai_effective": effective,
            "ai_service_url": probe_url,
            "ai_backend": effective.get("clip_selection_backend"),
            "ai_health": health,
            "ai_diagnostics": diagnostics,
            **_pipeline_settings_context(),
        }

    def _grouped_fields(view: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        index: dict[str, dict[str, Any]] = {}
        for field in view:
            name = str(field.get("group") or "")
            bucket = index.get(name)
            if bucket is None:
                bucket = {"name": name, "fields": []}
                index[name] = bucket
                groups.append(bucket)
            bucket["fields"].append(field)
        return groups

    def _pipeline_settings_context() -> dict[str, Any]:
        processing_saved = store.get_processing_config()
        post_saved = store.get_post_processing_config()
        processing_view = processing_fields_view(processing_saved)
        post_view = post_processing_fields_view(post_saved)
        return {
            "processing_field_groups": _grouped_fields(processing_view),
            "post_processing_field_groups": _grouped_fields(post_view),
        }

    @app.get("/settings")
    def settings_page():
        svc = service_by_key("video-automation")
        transcription: dict[str, Any] = {}
        service_error = ""
        if svc is None:
            service_error = "video-automation is not configured."
        else:
            ok, payload, status = call_json(
                svc, "/config/transcription", timeout=settings.service_timeout_sec
            )
            if ok:
                transcription = payload
            else:
                service_error = str(payload.get("error") or f"HTTP {status}")
        return render_template(
            "settings.html",
            transcription=transcription,
            service_error=service_error,
            **_ai_settings_context(),
        )

    @app.post("/settings/ai")
    def update_ai_settings():
        values, errors = parse_form(request.form)
        if errors:
            for message in errors:
                flash(message, "bad")
            store.log_action("ai-config", "rejected", ok=False, message="; ".join(errors)[:500])
            return redirect(url_for("settings_page"))
        if not values:
            flash("No local AI settings were submitted.", "bad")
            return redirect(url_for("settings_page"))
        store.set_ai_config(values)
        backend = values.get("clip_selection_backend") or store.get_ai_config().get(
            "clip_selection_backend", "openai"
        )
        store.log_action("ai-config", "saved", ok=True, message=f"backend={backend}")
        note = ""
        if backend == "ai_service":
            note = (
                " Clip selection now routes to the local ai-service (no OpenAI "
                "fallback). Ensure ai-service and Ollama are running."
            )
        flash(f"Local AI settings saved to controls.json.{note}", "ok")
        return redirect(url_for("settings_page"))

    @app.post("/settings/processing")
    def update_processing_settings():
        values, errors = parse_processing_form(request.form)
        if errors:
            for message in errors:
                flash(message, "bad")
            store.log_action("processing-config", "rejected", ok=False, message="; ".join(errors)[:500])
            return redirect(url_for("settings_page"))
        if not values:
            flash("No processing settings were submitted.", "bad")
            return redirect(url_for("settings_page"))
        store.set_processing_config(values)
        mode = values.get("processing_pipeline_mode") or store.get_processing_config().get(
            "processing_pipeline_mode", "legacy"
        )
        store.log_action("processing-config", "saved", ok=True, message=f"mode={mode}")
        note = ""
        if mode == "mk1":
            note = (
                " Pipeline mode is mk1: new jobs run transcript sectioning + "
                "candidate discovery via the local ai-service."
            )
        flash(f"Processing settings saved to controls.json.{note}", "ok")
        return redirect(url_for("settings_page"))

    @app.post("/settings/post-processing")
    def update_post_processing_settings():
        values, errors = parse_post_processing_form(request.form)
        if errors:
            for message in errors:
                flash(message, "bad")
            store.log_action(
                "post-processing-config", "rejected", ok=False, message="; ".join(errors)[:500]
            )
            return redirect(url_for("settings_page"))
        if not values:
            flash("No post-processing settings were submitted.", "bad")
            return redirect(url_for("settings_page"))
        store.set_post_processing_config(values)
        mode = values.get("selection_mode") or store.get_post_processing_config().get(
            "selection_mode", "balanced"
        )
        store.log_action("post-processing-config", "saved", ok=True, message=f"selection_mode={mode}")
        flash(
            "Post-processing settings saved to controls.json. They apply to new "
            "mk1 jobs (selection gate + universal conveyor).",
            "ok",
        )
        return redirect(url_for("settings_page"))

    @app.post("/settings/ai/test")
    def test_ai_model():
        probe_url = _effective_ai_service_url()
        diagnostics = ai_diagnostics(probe_url, timeout=settings.ai_diagnostics_timeout_sec)
        store.log_action(
            "ai-diagnostics",
            probe_url,
            ok=bool(diagnostics.get("ok")),
            message=str(diagnostics.get("error") or diagnostics.get("status") or ""),
        )
        if diagnostics.get("ok"):
            flash("Model diagnostic passed: the local model returned valid output.", "ok")
        else:
            flash(
                "Model diagnostic did not pass: "
                + str(diagnostics.get("error") or diagnostics.get("status") or "unknown"),
                "bad",
            )
        svc = service_by_key("video-automation")
        transcription: dict[str, Any] = {}
        service_error = ""
        if svc is None:
            service_error = "video-automation is not configured."
        else:
            ok, payload, status = call_json(
                svc, "/config/transcription", timeout=settings.service_timeout_sec
            )
            if ok:
                transcription = payload
            else:
                service_error = str(payload.get("error") or f"HTTP {status}")
        return render_template(
            "settings.html",
            transcription=transcription,
            service_error=service_error,
            **_ai_settings_context(diagnostics=diagnostics),
        )

    @app.post("/settings/transcription")
    def update_transcription_settings():
        svc = service_by_key("video-automation")
        if svc is None:
            flash("video-automation is not configured.", "bad")
            return redirect(url_for("settings_page"))
        model = str(request.form.get("whisperx_model") or "").strip()
        if not model:
            flash("Choose a WhisperX model first.", "bad")
            return redirect(url_for("settings_page"))
        ok, payload, status = call_json(
            svc,
            "/config/transcription",
            method="POST",
            payload={"whisperx_model": model},
            timeout=settings.service_timeout_sec,
        )
        if ok:
            active = str(payload.get("whisperx_model") or model)
            store.log_action("transcription-model", model, ok=True, message=active)
            flash(
                f"WhisperX model saved: {active}. New transcription jobs will use it.",
                "ok",
            )
        else:
            message = str(payload.get("error") or f"HTTP {status}")
            store.log_action("transcription-model", model, ok=False, message=message)
            flash(f"Could not save WhisperX model: {message}", "bad")
        return redirect(url_for("settings_page"))

    @app.post("/cleanup/preview")
    def cleanup_preview_route():
        media_days, media_err = _parse_ttl(request.form.get("media_days"), "media")
        metadata_days, meta_err = _parse_ttl(request.form.get("metadata_days"), "metadata")
        error = media_err or meta_err
        if error:
            flash(error, "bad")
            return render_template("recovery.html", **_recovery_context())
        preview = cleanup_preview(settings, media_days=media_days, metadata_days=metadata_days)
        return render_template("recovery.html", **_recovery_context(cleanup_preview_result=preview))

    @app.post("/cleanup/run")
    def cleanup_run():
        media_days, media_err = _parse_ttl(request.form.get("media_days"), "media")
        metadata_days, meta_err = _parse_ttl(request.form.get("metadata_days"), "metadata")
        error = media_err or meta_err
        if error:
            store.log_action("cleanup", "blocked", ok=False, message=error)
            flash(error, "bad")
            return redirect(url_for("recovery"))

        # Echo-back confirmation: without confirm=1, re-show the preview rather
        # than delete, so execution only ever runs what was previewed.
        if str(request.form.get("confirm") or "").strip() != "1":
            preview = cleanup_preview(settings, media_days=media_days, metadata_days=metadata_days)
            flash("Review the preview below, then click the delete button to confirm.", "warn")
            return render_template("recovery.html", **_recovery_context(cleanup_preview_result=preview))

        # Hard guard: never delete while a clip job is running.
        if _video_jobs_running(_video_jobs(settings, limit=100)):
            message = "Refusing cleanup: a video job is currently running."
            store.log_action("cleanup", settings.environment, ok=False, message=message)
            flash(message, "bad")
            return redirect(url_for("recovery"))

        result = run_retention_cleanup(settings, media_days=media_days, metadata_days=metadata_days)
        summary = _summarize_sweeper_output(result.message)
        log_message = summary or (result.message[:500] if result.message else f"rc={result.returncode}")
        store.log_action(
            "cleanup",
            f"{settings.environment} media={media_days}d meta={metadata_days}d",
            ok=result.ok,
            message=log_message,
        )
        if result.ok:
            flash(
                f"Cleanup finished ({summary or 'no summary line'}). Preview totals are estimates.",
                "ok",
            )
        else:
            flash(f"Cleanup failed: {result.message or ('rc=' + str(result.returncode))}", "bad")
        return redirect(url_for("recovery"))

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
            if next_page == "funnel_detail":
                return redirect(url_for("funnel_detail_page", funnel_id=funnel_id))
            return redirect(url_for("funnels_page") if next_page == "funnels" else _redirect_back())
        # Route through the canonical execution coordinator (admission + wait).
        env = "prod" if settings.environment in {"prod", "production"} else "dev"
        action = "run_pipeline_prod" if env == "prod" else "run_pipeline_dev"
        result = execute_control_action(
            settings,
            action,
            confirmed=(env == "prod"),
            funnel_id=funnel_id,
        )
        store.log_action(
            "run-funnel",
            funnel_id,
            ok=result.ok,
            message=result.message,
        )
        flash(f"Funnel {funnel_id}: {result.message}", "ok" if result.ok else "bad")
        if next_page == "funnel_detail":
            return redirect(url_for("funnel_detail_page", funnel_id=funnel_id))
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

    @app.post("/recovery/video/<job_id>/cancel")
    def cancel_video_job(job_id: str):
        svc = service_by_key("video-automation")
        if svc is None:
            flash("video-automation service is not configured.", "bad")
            return redirect(url_for("recovery"))
        ok, payload, status = call_json(
            svc,
            f"/jobs/{job_id}/cancel",
            method="POST",
            timeout=settings.service_timeout_sec,
        )
        message = str(payload.get("status") or payload.get("error") or payload.get("message") or f"HTTP {status}")
        store.log_action("cancel-video", job_id, ok=ok, message=message)
        flash(
            f"Cancelled {job_id} — marked failed (operator_cancel)."
            if ok
            else f"Cancel failed for {job_id}: {message}",
            "ok" if ok else "bad",
        )
        return redirect(url_for("recovery"))

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


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def _parse_ttl(raw: Any, label: str) -> tuple[int, str | None]:
    """Validate a TTL form field: must be an integer of at least 1 day."""
    text = str(raw if raw is not None else "").strip()
    try:
        value = int(text)
    except (TypeError, ValueError):
        return 0, f"Invalid {label} retention days: {text or 'empty'} (whole number >= 1 required)."
    if value < 1:
        return 0, f"Refusing non-positive {label} retention: {value} (must be >= 1)."
    return value, None


def _video_jobs_running(video_jobs: list[dict[str, Any]]) -> bool:
    return any(str(job.get("status") or "").lower() == "running" for job in video_jobs)


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def _parse_ttl(raw: Any, label: str) -> tuple[int, str | None]:
    """Validate a TTL form field: must be an integer of at least 1 day."""
    text = str(raw if raw is not None else "").strip()
    try:
        value = int(text)
    except (TypeError, ValueError):
        return 0, f"Invalid {label} retention days: {text or 'empty'} (whole number >= 1 required)."
    if value < 1:
        return 0, f"Refusing non-positive {label} retention: {value} (must be >= 1)."
    return value, None


def _video_jobs_running(video_jobs: list[dict[str, Any]]) -> bool:
    return any(str(job.get("status") or "").lower() == "running" for job in video_jobs)


def _control_state(store: ControlStore) -> dict[str, bool]:
    return {
        CONTROL_INGESTION_PAUSED: store.get_control_bool(CONTROL_INGESTION_PAUSED),
        CONTROL_UPLOADS_PAUSED: store.get_control_bool(CONTROL_UPLOADS_PAUSED),
        HUMAN_APPROVAL_REQUIRED: store.get_control_bool(HUMAN_APPROVAL_REQUIRED),
        PUBLISH_APPROVED_ONLY: store.get_control_bool(PUBLISH_APPROVED_ONLY),
    }


def _runtime_summary(settings: Settings, env_summary: dict[str, Any] | None = None) -> dict[str, str]:
    summary = env_summary or {}
    return {
        "environment": settings.environment,
        "environment_label": str(summary.get("environment_label") or settings.environment.upper()),
        "is_production": "true" if summary.get("is_production") else "false",
        "upload_mode": settings.upload_mode,
        "scheduler_mode": settings.scheduler_mode,
        "code_root": str(settings.code_root),
        "config_root": str(settings.config_root),
        "runtime_root": str(settings.runtime_root),
        "log_root": str(settings.log_root),
        "controls_file": str(settings.controls_file),
        "control_db_path": str(settings.control_db_path),
        "posting_config_label": str(summary.get("posting_config_label") or "Posting config: unknown"),
        "runtime_upload_control_label": str(
            summary.get("runtime_upload_control_label") or "Runtime upload kill switch: not implemented yet"
        ),
        "config_validation_state": str(summary.get("config_validation_state") or "unknown"),
        "funnel_id": str(summary.get("funnel_id") or "not_available"),
        "platform_id": str(summary.get("platform_id") or "not_available"),
        "preset_id": str(summary.get("preset_id") or "not_available"),
        "jobs_root": str(summary.get("jobs_root") or "not_available"),
        "outputs_root": str(summary.get("outputs_root") or "not_available"),
        "database_path": str(summary.get("database_path") or "not_available"),
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


if __name__ == "__main__":
    # Prefer ``python -m ops_ui`` (ops_ui/__main__.py). This path remains for
    # direct ``python -m ops_ui.app`` invocations and does not run at import.
    cfg = load_settings()
    create_app(cfg).run(host=cfg.host, port=cfg.port, debug=False)
