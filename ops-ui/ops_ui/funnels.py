from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import BASE_DIR, Settings
from .diagnostics import default_input_ledger_dir, filter_log_text
from .funnel_management.registry import FunnelNotFoundError, FunnelRegistry, FunnelRegistryError
from .funnel_management.dependency_paths import resolve_funnel_dependency_paths
from .funnel_management.readiness_summary import build_simple_funnel_status
from .funnel_management.validation import (
    FunnelValidationIssue,
    FunnelValidationReport,
    FunnelValidator,
    operational_state_label,
    readiness_label,
)
from .http_client import call_json
from .outputs_ui import outputs_page_href
from .recovery import is_failed_upload
from .store import ControlStore
from .system import journal_logs


FUNNEL_PAUSED_PREFIX = "funnel_paused:"

ACTIVE_VIDEO_STATUSES = frozenset({"queued", "running"})
ACTIVE_UPLOAD_STATUSES = frozenset(
    {
        "registered",
        "routed",
        "planned",
        "pending_upload",
        "uploading",
        "queued",
    }
)
QUEUE_UPLOAD_STATUSES = frozenset(
    {
        "registered",
        "routed",
        "planned",
        "pending_upload",
        "queued",
    }
)


def funnel_pause_key(funnel_id: str) -> str:
    return f"{FUNNEL_PAUSED_PREFIX}{funnel_id}"


def is_funnel_paused(store: ControlStore, funnel_id: str) -> bool:
    return store.get_control_bool(funnel_pause_key(funnel_id))


def set_funnel_paused(store: ControlStore, funnel_id: str, paused: bool) -> None:
    store.set_control_bool(funnel_pause_key(funnel_id), paused)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _channel_profiles_path() -> Path:
    raw = os.environ.get("OUTPUT_FUNNEL_CHANNELS", "").strip()
    if raw:
        return Path(raw).expanduser()
    return BASE_DIR / "output-funnel" / "config" / "channels.example.json"


def load_channel_profiles() -> list[dict[str, Any]]:
    payload = _read_json_object(_channel_profiles_path())
    if not payload:
        return []
    channels = payload.get("channels")
    if not isinstance(channels, list):
        return []
    return [dict(item) for item in channels if isinstance(item, dict)]


def load_clip_funnel_config(pipeline_profile: str) -> dict[str, Any] | None:
    fid = str(pipeline_profile or "").strip()
    if not fid:
        return None
    raw_dir = os.environ.get("FUNNEL_CONFIG_DIR") or os.environ.get("VIDEO_FUNNELS_CONFIG_DIR", "")
    if raw_dir.strip():
        return _read_json_object(Path(raw_dir).expanduser() / f"{fid}.json")
    path = BASE_DIR / "video-automation" / "config" / "funnels" / f"{fid}.json"
    return _read_json_object(path)


def _profiles_for_funnel(funnel_id: str, profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for profile in profiles:
        routing = profile.get("routing") if isinstance(profile.get("routing"), dict) else {}
        accepted = routing.get("accepted_funnel_ids")
        if isinstance(accepted, list) and funnel_id in accepted:
            matched.append(profile)
    return matched


def _schedule_summary(profile: dict[str, Any]) -> str:
    cadence = profile.get("cadence") if isinstance(profile.get("cadence"), dict) else {}
    parts: list[str] = []
    tz = cadence.get("timezone")
    if tz:
        parts.append(str(tz))
    windows = cadence.get("allowed_windows")
    if isinstance(windows, list) and windows:
        window_bits: list[str] = []
        for window in windows[:3]:
            if isinstance(window, dict):
                window_bits.append(f"{window.get('start', '?')}–{window.get('end', '?')}")
        if window_bits:
            parts.append("windows " + ", ".join(window_bits))
    gap = cadence.get("min_gap_minutes")
    if gap is not None:
        parts.append(f"gap {gap}m")
    lead = cadence.get("default_lead_minutes")
    if lead is not None:
        parts.append(f"lead {lead}m")
    max_day = cadence.get("max_uploads_per_day")
    if max_day is not None:
        parts.append(f"max {max_day}/day")
    return " · ".join(parts) if parts else "—"


def _upload_timing_rows(profile: dict[str, Any]) -> list[tuple[str, str]]:
    cadence = profile.get("cadence") if isinstance(profile.get("cadence"), dict) else {}
    rows: list[tuple[str, str]] = []
    for key, label in (
        ("timezone", "Timezone"),
        ("min_gap_minutes", "Min gap (minutes)"),
        ("default_lead_minutes", "Upload lead (minutes)"),
        ("max_uploads_per_day", "Max uploads / day"),
    ):
        value = cadence.get(key)
        if value is not None and str(value).strip() != "":
            rows.append((label, str(value)))
    windows = cadence.get("allowed_windows")
    if isinstance(windows, list) and windows:
        bits = []
        for window in windows:
            if isinstance(window, dict):
                bits.append(f"{window.get('start', '?')}–{window.get('end', '?')}")
        if bits:
            rows.append(("Allowed windows", ", ".join(bits)))
    return rows


def scan_input_ledger(ledger_dir: Path | None = None) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Map input_id → funnel_id and collect per-funnel run rows from ledger files."""
    root = (ledger_dir or default_input_ledger_dir()).expanduser()
    input_funnel: dict[str, str] = {}
    runs: list[dict[str, Any]] = []
    if not root.is_dir():
        return input_funnel, runs
    for path in sorted(root.glob("input_*.json"), reverse=True):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        funnel_id = str(raw.get("funnel_id") or "").strip()
        input_id = str(raw.get("input_id") or path.stem).strip()
        if input_id and funnel_id:
            input_funnel[input_id] = funnel_id
        state = str(raw.get("state") or raw.get("status") or "").strip().lower()
        runs.append(
            {
                "funnel_id": funnel_id or "—",
                "input_id": input_id,
                "created_at": raw.get("created_at"),
                "state": state or "unknown",
                "success": state in {"ready", "input_ready", "stored", "complete", "success"},
                "source_url": raw.get("source_url") or raw.get("url"),
                "title": raw.get("title") or raw.get("source_title"),
            }
        )
    return input_funnel, runs


def _derive_health(
    *,
    config_active: bool,
    paused: bool,
    ingestion_paused: bool,
    failure_count: int,
    last_run_ok: bool | None,
) -> str:
    if not config_active:
        return "inactive"
    if paused or ingestion_paused:
        return "paused"
    if failure_count >= 5 or last_run_ok is False:
        return "bad"
    if failure_count > 0 or last_run_ok is None:
        return "warn"
    return "ok"


def _operator_status(*, config_active: bool, paused: bool, ingestion_paused: bool) -> str:
    if not config_active:
        return "disabled"
    if paused:
        return "paused"
    if ingestion_paused:
        return "ingestion_paused"
    return "live"


def build_funnel_rows(
    *,
    settings: Settings,
    store: ControlStore,
    source_funnels: list[dict[str, Any]],
    video_jobs: list[dict[str, Any]],
    upload_jobs: list[dict[str, Any]],
    ingestion_paused: bool,
    input_funnel_map: dict[str, str],
    input_runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    profiles = load_channel_profiles()
    by_funnel_runs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in input_runs:
        fid = str(run.get("funnel_id") or "").strip()
        if fid and fid != "—":
            by_funnel_runs[fid].append(run)

    video_by_funnel: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in video_jobs:
        input_id = str(job.get("input_id") or "").strip()
        fid = input_funnel_map.get(input_id, "")
        if not fid:
            continue
        video_by_funnel[fid].append(job)

    upload_by_funnel: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in upload_jobs:
        fid = str(job.get("funnel_id") or "").strip()
        if fid:
            upload_by_funnel[fid].append(job)

    config_by_id = {
        str(f.get("funnel_id") or ""): f for f in source_funnels if isinstance(f, dict) and f.get("funnel_id")
    }
    all_ids = sorted(set(config_by_id) | set(by_funnel_runs) | set(video_by_funnel) | set(upload_by_funnel))

    rows: list[dict[str, Any]] = []
    for funnel_id in all_ids:
        cfg = config_by_id.get(funnel_id, {})
        paused = is_funnel_paused(store, funnel_id)
        config_active = bool(cfg.get("active", True)) if cfg else True
        runs = by_funnel_runs.get(funnel_id, [])
        runs_sorted = sorted(runs, key=lambda r: str(r.get("created_at") or ""), reverse=True)
        last_run = runs_sorted[0] if runs_sorted else None
        last_success = next((r for r in runs_sorted if r.get("success")), None)

        trigger_runs = [
            r
            for r in runs_sorted
            if str(r.get("state") or "") not in {"", "unknown"}
        ]

        videos = video_by_funnel.get(funnel_id, [])
        uploads = upload_by_funnel.get(funnel_id, [])
        video_failures = sum(
            1
            for j in videos
            if str(j.get("status") or "").lower() == "failed" or int(j.get("error_count") or 0) > 0
        )
        upload_failures = sum(1 for j in uploads if is_failed_upload(j))
        failure_count = video_failures + upload_failures

        active_sources = []
        for src in cfg.get("sources") if isinstance(cfg.get("sources"), list) else []:
            if isinstance(src, dict) and src.get("active") is not False:
                active_sources.append(
                    {
                        "label": src.get("label") or src.get("source_id") or "source",
                        "url": src.get("url"),
                        "max_videos_per_source": src.get("max_videos_per_source"),
                    }
                )

        posting = cfg.get("posting_config") if isinstance(cfg.get("posting_config"), dict) else {}
        platforms_cfg = posting.get("platforms") if isinstance(posting.get("platforms"), list) else []

        clip_cfg = load_clip_funnel_config(str(cfg.get("pipeline_profile") or funnel_id))
        selection = {}
        if clip_cfg and isinstance(clip_cfg.get("selection"), dict):
            selection = clip_cfg["selection"]
        max_clips = selection.get("max_clips")
        platform_flags = (
            clip_cfg.get("platforms")
            if clip_cfg and isinstance(clip_cfg.get("platforms"), dict)
            else {}
        )

        matched_profiles = _profiles_for_funnel(funnel_id, profiles)
        schedule_bits = [_schedule_summary(p) for p in matched_profiles]
        upload_timing: list[tuple[str, str]] = []
        max_uploads_day: str | None = None
        for profile in matched_profiles:
            upload_timing.extend(_upload_timing_rows(profile))
            cadence = profile.get("cadence") if isinstance(profile.get("cadence"), dict) else {}
            if cadence.get("max_uploads_per_day") is not None:
                max_uploads_day = str(cadence.get("max_uploads_per_day"))

        platform_targets: list[str] = []
        for p in platforms_cfg:
            if isinstance(p, str) and p.strip():
                platform_targets.append(p.strip())
        for profile in matched_profiles:
            plat = str(profile.get("platform") or "").strip()
            if plat and plat not in platform_targets:
                platform_targets.append(plat)
        for name, enabled in platform_flags.items():
            if enabled and str(name) not in platform_targets:
                platform_targets.append(str(name))

        rows.append(
            {
                "funnel_id": funnel_id,
                "angle": cfg.get("angle") or "—",
                "config_active": config_active,
                "paused": paused,
                "operator_status": _operator_status(
                    config_active=config_active,
                    paused=paused,
                    ingestion_paused=ingestion_paused,
                ),
                "health": _derive_health(
                    config_active=config_active,
                    paused=paused,
                    ingestion_paused=ingestion_paused,
                    failure_count=failure_count,
                    last_run_ok=bool(last_run.get("success")) if last_run else None,
                ),
                "pipeline_profile": cfg.get("pipeline_profile") or funnel_id,
                "source_type": cfg.get("source_type") or "—",
                "max_downloads_per_run": cfg.get("max_downloads_per_run"),
                "duration_range": (
                    f"{cfg.get('min_duration_minutes', '?')}–{cfg.get('max_duration_minutes', '?')} min"
                    if cfg
                    else "—"
                ),
                "active_sources": active_sources,
                "platform_targets": platform_targets or ["—"],
                "posting_mode": posting.get("mode") or "—",
                "posting_enabled": posting.get("enabled"),
                "max_clips": max_clips if max_clips is not None else "—",
                "max_uploads_per_day": max_uploads_day or "—",
                "schedule_summary": "; ".join(schedule_bits) if schedule_bits else "—",
                "upload_timing": upload_timing[:12],
                "last_run_at": (last_run or {}).get("created_at") or "—",
                "last_success_at": (last_success or {}).get("created_at") or "—",
                "last_run_state": (last_run or {}).get("state") or "—",
                "failure_count": failure_count,
                "video_failure_count": video_failures,
                "upload_failure_count": upload_failures,
                "queue_depth": sum(
                    1 for j in uploads if str(j.get("status") or "").lower() in QUEUE_UPLOAD_STATUSES
                ),
                "active_video_jobs": sum(
                    1 for j in videos if str(j.get("status") or "").lower() in ACTIVE_VIDEO_STATUSES
                ),
                "active_upload_jobs": sum(
                    1 for j in uploads if str(j.get("status") or "").lower() in ACTIVE_UPLOAD_STATUSES
                ),
                "ledger_runs": trigger_runs[:8],
                "can_run": config_active and not paused and not ingestion_paused,
            }
        )
    return rows


def trigger_history(store: ControlStore, *, limit: int = 40) -> list[dict[str, Any]]:
    actions = store.recent_actions(limit=limit)
    return [a for a in actions if str(a.get("action") or "") == "run-funnel"]


def funnel_log_snippet(settings: Settings, funnel_id: str, *, max_lines: int = 40) -> str:
    svc = next((s for s in settings.services if s.key == "source-input"), None)
    if svc is None or not funnel_id:
        return "source-input not configured."
    raw = journal_logs(svc.systemd_unit, min(settings.journal_lines * 4, 320))
    filtered, total, matched = filter_log_text(raw, funnel_id)
    if matched == 0:
        return f"No journal lines matched {funnel_id!r} in the last {total} lines."
    lines = filtered.splitlines()
    if len(lines) > max_lines:
        return "\n".join(lines[-max_lines:])
    return filtered


def funnel_feature_matrix(rows: list[dict[str, Any]], *, has_global_controls: bool) -> list[dict[str, Any]]:
    """Checklist items for the template — marks coverage from computed rows."""

    def _all_have(key: str) -> bool:
        return bool(rows) and all(row.get(key) not in (None, "", "—", []) for row in rows)

    return [
        {"group": "Funnel Control", "features": [
            {"label": "Enable/disable funnels", "status": "partial", "note": "Shows config `active`; edit source-input funnels.json to change"},
            {"label": "Pause/resume funnels", "status": "done" if has_global_controls else "partial"},
            {"label": "Manual trigger", "status": "done"},
            {"label": "Funnel status visibility", "status": "done" if _all_have("operator_status") else "partial"},
        ]},
        {"group": "Funnel Visibility", "features": [
            {"label": "Funnel health", "status": "done" if _all_have("health") else "partial"},
            {"label": "Last run timestamp", "status": "done"},
            {"label": "Last successful run", "status": "done"},
            {"label": "Failure count per funnel", "status": "done"},
            {"label": "Active source visibility", "status": "done"},
        ]},
        {"group": "Funnel Configuration", "features": [
            {"label": "Schedule visibility", "status": "done"},
            {"label": "Platform targets", "status": "done"},
            {"label": "Pipeline profile selection", "status": "partial", "note": "Read-only; change in config files"},
            {"label": "Max clips/day", "status": "done"},
            {"label": "Upload timing settings", "status": "done"},
        ]},
        {"group": "Funnel Operations", "features": [
            {"label": "Trigger history", "status": "done"},
            {"label": "Funnel logs", "status": "done"},
            {"label": "Queue depth per funnel", "status": "done"},
            {"label": "Active jobs per funnel", "status": "done"},
        ]},
    ]


def load_funnel_board(
    settings: Settings,
    store: ControlStore,
    *,
    ingestion_paused: bool,
    video_limit: int = 100,
    upload_limit: int = 200,
) -> dict[str, Any]:
    source_funnels = _source_funnels(settings)
    video_jobs = _video_jobs(settings, limit=video_limit)
    upload_jobs = _upload_jobs(settings, limit=upload_limit)
    input_map, input_runs = scan_input_ledger()
    rows = build_funnel_rows(
        settings=settings,
        store=store,
        source_funnels=source_funnels,
        video_jobs=video_jobs,
        upload_jobs=upload_jobs,
        ingestion_paused=ingestion_paused,
        input_funnel_map=input_map,
        input_runs=input_runs,
    )
    return {
        "rows": rows,
        "trigger_history": trigger_history(store),
        "feature_matrix": funnel_feature_matrix(rows, has_global_controls=True),
        "channel_profiles_path": str(_channel_profiles_path()),
        "source_funnel_count": len(source_funnels),
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


def _video_jobs(settings: Settings, *, limit: int) -> list[dict[str, Any]]:
    svc = next((s for s in settings.services if s.key == "video-automation"), None)
    if svc is None:
        return []
    ok, payload, _status = call_json(svc, f"/jobs?limit={limit}", timeout=settings.service_timeout_sec)
    if not ok:
        return []
    jobs = payload.get("jobs")
    return jobs if isinstance(jobs, list) else []


def _upload_jobs(settings: Settings, *, limit: int) -> list[dict[str, Any]]:
    svc = next((s for s in settings.services if s.key == "output-funnel"), None)
    if svc is None:
        return []
    ok, payload, _status = call_json(svc, f"/queue?limit={limit}", timeout=settings.service_timeout_sec)
    if not ok:
        return []
    jobs = payload.get("jobs")
    return jobs if isinstance(jobs, list) else []


def _optional_file(path: Path) -> Path | None:
    return path if path.is_file() else None


def _optional_dir(path: Path) -> Path | None:
    return path if path.is_dir() else None


def _source_funnels_config_path() -> Path | None:
    raw = os.environ.get("SOURCE_INPUT_FUNNELS", "").strip()
    if raw:
        return _optional_file(Path(raw).expanduser())
    return _optional_file(BASE_DIR / "source-input" / "input_service" / "config" / "funnels.json")


def _video_funnels_dir() -> Path | None:
    raw = os.environ.get("FUNNEL_CONFIG_DIR") or os.environ.get("VIDEO_FUNNELS_CONFIG_DIR", "")
    if raw.strip():
        return _optional_dir(Path(raw).expanduser())
    return _optional_dir(BASE_DIR / "video-automation" / "config" / "funnels")


def _video_pipeline_profiles_path() -> Path | None:
    raw = os.environ.get("VIDEO_PIPELINE_PROFILES", "").strip()
    if raw:
        return _optional_file(Path(raw).expanduser())
    return _optional_file(BASE_DIR / "video-automation" / "config" / "video_pipeline_profiles.json")


def _output_channels_config_path() -> Path | None:
    raw = os.environ.get("OUTPUT_FUNNEL_CHANNELS", "").strip()
    if raw:
        return _optional_file(Path(raw).expanduser())
    for candidate in (
        BASE_DIR / "output-funnel" / "config" / "channels.json",
        BASE_DIR / "output-funnel" / "config" / "channels.example.json",
    ):
        found = _optional_file(candidate)
        if found is not None:
            return found
    return None


def ai_rule_registry_path() -> Path | None:
    """Public accessor for the funnel rule registry JSON path."""
    path = resolve_funnel_dependency_paths().ai_rule_registry_path
    return path if path is not None and path.is_file() else path


def _ai_rule_registry_path() -> Path | None:
    raw = os.environ.get("AI_FUNNEL_RULE_REGISTRY", "").strip()
    if raw:
        return _optional_file(Path(raw).expanduser())
    return _optional_file(BASE_DIR / "ai-service" / "config" / "funnel_rule_registry.json")


def _ai_prompts_dir() -> Path | None:
    raw = os.environ.get("AI_FUNNEL_RULES_DIR", "").strip()
    if raw:
        return _optional_dir(Path(raw).expanduser())
    return _optional_dir(BASE_DIR / "ai-service" / "prompts" / "funnel_rules")


def _config_manager_funnels_dir() -> Path | None:
    raw = os.environ.get("CONFIG_MANAGER_FUNNELS_DIR", "").strip()
    if raw:
        return _optional_dir(Path(raw).expanduser())
    return _optional_dir(BASE_DIR / "config" / "funnels")


def build_funnel_validator() -> FunnelValidator:
    """Build a validator using the same dependency paths as sync."""
    deps = resolve_funnel_dependency_paths()
    return FunnelValidator(
        source_funnels_path=deps.source_funnels_path,
        video_funnels_dir=deps.video_funnels_dir,
        video_pipeline_profiles_path=deps.video_pipeline_profiles_path,
        output_channels_path=deps.output_channels_path,
        ai_rule_registry_path=deps.ai_rule_registry_path,
        ai_prompts_dir=deps.ai_prompts_dir,
        config_manager_funnels_dir=deps.config_manager_funnels_dir,
    )


def _validation_issue_summary(*, error_count: int, warning_count: int) -> str:
    parts: list[str] = []
    if error_count:
        label = "error" if error_count == 1 else "errors"
        parts.append(f"{error_count} {label}")
    if warning_count:
        label = "warning" if warning_count == 1 else "warnings"
        parts.append(f"{warning_count} {label}")
    return ", ".join(parts) if parts else "OK"


def _compact_ops_row(
    ops: dict[str, Any] | None,
    *,
    funnel_id: str,
    store: ControlStore,
    ingestion_paused: bool,
    enabled: bool,
) -> dict[str, Any]:
    paused = bool(ops.get("paused")) if ops else is_funnel_paused(store, funnel_id)
    if ops is not None:
        can_run = bool(ops.get("can_run"))
        return {
            "available": True,
            "paused": paused,
            "can_run": can_run,
            "health": ops.get("health") or "—",
            "last_run_at": ops.get("last_run_at") or "—",
            "last_success_at": ops.get("last_success_at") or "—",
            "failure_count": ops.get("failure_count", 0),
            "queue_depth": ops.get("queue_depth", 0),
            "active_video_jobs": ops.get("active_video_jobs", 0),
            "active_upload_jobs": ops.get("active_upload_jobs", 0),
        }
    return {
        "available": False,
        "paused": paused,
        "can_run": enabled and not paused and not ingestion_paused,
        "health": "—",
        "last_run_at": "—",
        "last_success_at": "—",
        "failure_count": 0,
        "queue_depth": 0,
        "active_video_jobs": 0,
        "active_upload_jobs": 0,
    }


def build_canonical_funnel_list_rows(
    *,
    registry: FunnelRegistry,
    validator: FunnelValidator,
    store: ControlStore,
    ingestion_paused: bool,
    ops_rows_by_id: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build list-page rows from registry funnels, validation, and optional ops overlay."""
    ops_rows_by_id = ops_rows_by_id or {}
    rows: list[dict[str, Any]] = []
    for funnel in registry.list_funnels():
        report = validator.validate_funnel(funnel)
        funnel_id = funnel.identity.funnel_id
        ops = ops_rows_by_id.get(funnel_id)
        target_platforms = list(funnel.distribution.target_platforms)
        rows.append(
            {
                "funnel_id": funnel_id,
                "display_name": funnel.identity.display_name,
                "category": funnel.identity.category or "—",
                "environment": funnel.identity.environment,
                "status": funnel.identity.status,
                "enabled": funnel.identity.enabled,
                "source_count": len(funnel.acquisition.sources),
                "target_platforms": target_platforms,
                "target_platforms_display": ", ".join(target_platforms) if target_platforms else "—",
                "route_count": len(funnel.distribution.channel_routes),
                "readiness_status": report.status,
                "sync_ready": report.sync_ready,
                "processing_ready": report.processing_ready,
                "processing_label": readiness_label(report.processing_state),
                "runnable": report.runnable,
                "error_count": len(report.errors),
                "warning_count": len(report.warnings),
                "validation_summary": _validation_issue_summary(
                    error_count=len(report.errors),
                    warning_count=len(report.warnings),
                ),
                "ops": _compact_ops_row(
                    ops,
                    funnel_id=funnel_id,
                    store=store,
                    ingestion_paused=ingestion_paused,
                    enabled=funnel.identity.enabled,
                ),
            }
        )
    return rows


def load_canonical_funnel_page(
    settings: Settings,
    store: ControlStore,
    *,
    ingestion_paused: bool,
    registry_dir: Path | None = None,
) -> dict[str, Any]:
    """Load canonical funnel list data for the /funnels page."""
    registry = FunnelRegistry(registry_dir)
    validator = build_funnel_validator()

    ops_rows_by_id: dict[str, dict[str, Any]] = {}
    trigger_history_rows: list[dict[str, Any]] = []
    ops_available = False
    try:
        board = load_funnel_board(settings, store, ingestion_paused=ingestion_paused)
        ops_rows_by_id = {
            str(row.get("funnel_id") or ""): row
            for row in board.get("rows", [])
            if isinstance(row, dict) and row.get("funnel_id")
        }
        trigger_history_rows = board.get("trigger_history") or []
        ops_available = True
    except Exception:
        ops_available = False

    rows = build_canonical_funnel_list_rows(
        registry=registry,
        validator=validator,
        store=store,
        ingestion_paused=ingestion_paused,
        ops_rows_by_id=ops_rows_by_id,
    )

    return {
        "rows": rows,
        "empty_registry": not rows,
        "registry_path": str(registry.registry_dir),
        "ops_available": ops_available,
        "trigger_history": trigger_history_rows,
    }


class FunnelDetailNotFoundError(Exception):
    """Raised when a canonical funnel is not in the registry."""


def _validation_issue_dict(issue: FunnelValidationIssue) -> dict[str, Any]:
    return {
        "code": issue.code,
        "message": issue.message,
        "severity": issue.severity,
        "section": issue.section,
        "field": issue.field,
        "source": issue.source,
    }


def _validation_view(report: FunnelValidationReport) -> dict[str, Any]:
    return {
        "status": report.status,
        "runnable": report.runnable,
        "valid_config": report.valid_config,
        "dependencies_ok": report.dependencies_ok,
        "sync_ready": report.sync_ready,
        "processing_ready": report.processing_ready,
        "posting_ready": report.posting_ready,
        "sync_state": report.sync_state,
        "processing_state": report.processing_state,
        "posting_state": report.posting_state,
        "sync_label": readiness_label(report.sync_state),
        "processing_label": readiness_label(report.processing_state),
        "posting_label": readiness_label(report.posting_state),
        "checked_at": report.checked_at,
        "error_count": len(report.errors),
        "warning_count": len(report.warnings),
        "info_count": len(report.info),
        "errors": [_validation_issue_dict(issue) for issue in report.errors],
        "warnings": [_validation_issue_dict(issue) for issue in report.warnings],
        "info": [_validation_issue_dict(issue) for issue in report.info],
    }


def _detail_ops_view(
    ops: dict[str, Any] | None,
    *,
    funnel_id: str,
    store: ControlStore,
    ingestion_paused: bool,
    enabled: bool,
) -> dict[str, Any]:
    compact = _compact_ops_row(
        ops,
        funnel_id=funnel_id,
        store=store,
        ingestion_paused=ingestion_paused,
        enabled=enabled,
    )
    if ops is not None:
        compact["operator_status"] = ops.get("operator_status") or "—"
        compact["last_run_state"] = ops.get("last_run_state") or "—"
    else:
        compact["operator_status"] = "paused" if compact["paused"] else "—"
        compact["last_run_state"] = "—"
    return compact


def load_canonical_funnel_detail(
    funnel_id: str,
    settings: Settings,
    store: ControlStore,
    *,
    ingestion_paused: bool,
    registry_dir: Path | None = None,
) -> dict[str, Any]:
    """Load read-only detail view data for one canonical funnel."""
    clean_id = str(funnel_id or "").strip()
    if not clean_id:
        raise FunnelDetailNotFoundError("Funnel ID is required.")

    registry = FunnelRegistry(registry_dir)
    try:
        funnel = registry.get_funnel(clean_id)
    except FunnelNotFoundError as exc:
        raise FunnelDetailNotFoundError(str(exc)) from exc

    validator = build_funnel_validator()
    report = validator.validate_funnel(funnel)

    ops_row: dict[str, Any] | None = None
    trigger_history_rows: list[dict[str, Any]] = []
    ops_available = False
    try:
        board = load_funnel_board(settings, store, ingestion_paused=ingestion_paused)
        ops_by_id = {
            str(row.get("funnel_id") or ""): row
            for row in board.get("rows", [])
            if isinstance(row, dict) and row.get("funnel_id")
        }
        ops_row = ops_by_id.get(clean_id)
        trigger_history_rows = [
            action
            for action in board.get("trigger_history") or []
            if isinstance(action, dict) and str(action.get("target") or "") == clean_id
        ]
        ops_available = True
    except Exception:
        ops_available = False

    identity = funnel.identity
    processing = funnel.processing
    distribution = funnel.distribution

    return {
        "funnel_id": clean_id,
        "identity": {
            "funnel_id": identity.funnel_id,
            "display_name": identity.display_name,
            "description": identity.description or "—",
            "category": identity.category or "—",
            "enabled": identity.enabled,
            "environment": identity.environment,
            "status": identity.status,
            "template_source": identity.template_source or "—",
            "created_at": identity.created_at,
            "updated_at": identity.updated_at,
            "operator_note": identity.operator_note or "—",
        },
        "acquisition": {
            "source_type": funnel.acquisition.source_type,
            "min_duration_minutes": funnel.acquisition.min_duration_minutes,
            "max_duration_minutes": funnel.acquisition.max_duration_minutes,
            "max_downloads_per_run": funnel.acquisition.max_downloads_per_run,
            "sources": [
                {
                    "source_id": source.source_id,
                    "label": source.label,
                    "url": source.url,
                    "source_type": source.source_type,
                    "active": source.active,
                    "max_videos_per_source": source.max_videos_per_source,
                    "hydrate_missing_duration": source.hydrate_missing_duration,
                    "title_allowlist": list(source.title_allowlist),
                    "title_blocklist": list(source.title_blocklist),
                    "title_allowlist_display": ", ".join(source.title_allowlist) or "—",
                    "title_blocklist_display": ", ".join(source.title_blocklist) or "—",
                }
                for source in funnel.acquisition.sources
            ],
        },
        "processing": {
            "pipeline_profile": processing.pipeline_profile,
            "ai_rule_profile": processing.ai_rules.ai_rule_profile,
            "max_clips": processing.selection.max_clips,
            "min_clip_duration_sec": processing.selection.min_clip_duration_sec,
            "max_clip_duration_sec": processing.selection.max_clip_duration_sec,
            "max_overlap_sec": processing.selection.max_overlap_sec,
            "filename_prefix": processing.output.filename_prefix,
            "delivery_mode": processing.output.delivery_mode,
            "platforms": [
                {"name": name, "enabled": bool(enabled)}
                for name, enabled in sorted(processing.platforms.items())
            ],
        },
        "distribution": {
            "posting_enabled": distribution.posting_enabled,
            "posting_mode": distribution.posting_mode,
            "target_platforms": list(distribution.target_platforms),
            "target_platforms_display": ", ".join(distribution.target_platforms) or "—",
            "channel_routes": [
                {
                    "channel_id": route.channel_id,
                    "platform": route.platform,
                    "enabled": route.enabled,
                }
                for route in distribution.channel_routes
            ],
        },
        "mappings": {
            "config_manager_funnel_id": funnel.mappings.config_manager_funnel_id or "—",
        },
        "validation": _validation_view(report),
        "simple_status": build_simple_funnel_status(
            posting_enabled=distribution.posting_enabled,
            identity_status=identity.status,
            identity_enabled=identity.enabled,
            report=report,
            ops=_compact_ops_row(
                ops_row,
                funnel_id=clean_id,
                store=store,
                ingestion_paused=ingestion_paused,
                enabled=identity.enabled,
            ),
        ),
        "operational_label": operational_state_label(
            report=report,
            identity_enabled=identity.enabled,
            identity_status=identity.status,
            paused=_compact_ops_row(
                ops_row,
                funnel_id=clean_id,
                store=store,
                ingestion_paused=ingestion_paused,
                enabled=identity.enabled,
            )["paused"],
        ),
        "ops": _detail_ops_view(
            ops_row,
            funnel_id=clean_id,
            store=store,
            ingestion_paused=ingestion_paused,
            enabled=identity.enabled,
        ),
        "ops_available": ops_available,
        "trigger_history": trigger_history_rows[:10],
        "registry_path": str(registry.registry_dir),
        "outputs_href": outputs_page_href(funnel_id=clean_id),
        "jobs_href": f"/ops/jobs?funnel={clean_id}",
    }


def _console_funnel_option_label(row: dict[str, Any], *, env_token: str) -> str:
    funnel_id = str(row.get("funnel_id") or "")
    display = str(row.get("display_name") or funnel_id)
    funnel_env = str(row.get("environment") or "").strip().lower()
    if funnel_env and funnel_env != env_token and env_token in {"dev", "prod"}:
        return f"{display} ({funnel_env})"
    return display


def _console_funnel_disabled_hint(
    row: dict[str, Any],
    *,
    env_token: str,
    ingestion_paused: bool,
) -> str | None:
    if ingestion_paused:
        return "ingestion paused"
    if not row.get("enabled"):
        return "disabled"
    funnel_env = str(row.get("environment") or "").strip().lower()
    if funnel_env and funnel_env != env_token and env_token in {"dev", "prod"}:
        return f"{funnel_env} only"
    ops = row.get("ops") if isinstance(row.get("ops"), dict) else {}
    if ops.get("paused"):
        return "paused"
    if not row.get("runnable"):
        return "not runnable"
    if ops.get("available") and not ops.get("can_run"):
        return "cannot run"
    return None


def _console_funnel_option_from_row(
    row: dict[str, Any],
    *,
    env_token: str,
    ingestion_paused: bool,
) -> dict[str, Any]:
    hint = _console_funnel_disabled_hint(
        row, env_token=env_token, ingestion_paused=ingestion_paused
    )
    return {
        "funnel_id": str(row.get("funnel_id") or ""),
        "label": _console_funnel_option_label(row, env_token=env_token),
        "disabled": hint is not None,
        "disabled_hint": hint,
        "selected": False,
    }


def _console_funnel_option_from_source(
    source: dict[str, Any],
    *,
    ingestion_paused: bool,
) -> dict[str, Any]:
    funnel_id = str(source.get("funnel_id") or "").strip()
    active = bool(source.get("active", True))
    hint = "ingestion paused" if ingestion_paused else ("inactive" if not active else None)
    return {
        "funnel_id": funnel_id,
        "label": funnel_id,
        "disabled": hint is not None,
        "disabled_hint": hint,
        "selected": False,
    }


def _pick_console_default_funnel(options: list[dict[str, Any]]) -> str:
    for opt in options:
        if not opt.get("disabled"):
            opt["selected"] = True
            return str(opt.get("funnel_id") or "")
    if options:
        options[0]["selected"] = True
        return str(options[0].get("funnel_id") or "")
    return ""


def load_console_funnel_context(
    settings: Settings,
    store: ControlStore,
    *,
    ingestion_paused: bool,
    env_token: str | None = None,
    registry_dir: Path | None = None,
) -> dict[str, Any]:
    """Compact canonical funnel data for the Operator Console."""
    token = (env_token or settings.environment or "dev").strip().lower()

    try:
        page = load_canonical_funnel_page(
            settings,
            store,
            ingestion_paused=ingestion_paused,
            registry_dir=registry_dir,
        )
    except Exception:
        page = None

    rows: list[dict[str, Any]] = []
    registry_path = ""
    ops_available = False
    if isinstance(page, dict):
        rows = page.get("rows") or []
        registry_path = str(page.get("registry_path") or "")
        ops_available = bool(page.get("ops_available"))

    options = [
        _console_funnel_option_from_row(
            row, env_token=token, ingestion_paused=ingestion_paused
        )
        for row in rows
        if row.get("funnel_id")
    ]

    if not options:
        for source in _source_funnels(settings):
            if not isinstance(source, dict):
                continue
            funnel_id = str(source.get("funnel_id") or "").strip()
            if funnel_id:
                options.append(
                    _console_funnel_option_from_source(
                        source, ingestion_paused=ingestion_paused
                    )
                )

    default_id = _pick_console_default_funnel(options) if options else ""

    summary_rows = [
        {
            "funnel_id": str(row.get("funnel_id") or ""),
            "display_name": str(row.get("display_name") or row.get("funnel_id") or ""),
            "readiness_status": str(row.get("readiness_status") or "—"),
            "runnable": bool(row.get("runnable")),
            "enabled": bool(row.get("enabled")),
            "source_count": row.get("source_count", 0),
            "target_platforms_display": str(row.get("target_platforms_display") or "—"),
            "validation_summary": str(row.get("validation_summary") or "—"),
            "ops": row.get("ops") if isinstance(row.get("ops"), dict) else {},
        }
        for row in rows
    ]

    return {
        "console_funnel_options": options,
        "console_default_funnel_id": default_id,
        "console_funnel_rows": summary_rows,
        "console_funnels_empty": not summary_rows and not options,
        "console_funnels_registry_path": registry_path,
        "console_funnels_ops_available": ops_available,
        "console_ingestion_paused": ingestion_paused,
    }
