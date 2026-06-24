from __future__ import annotations

import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import BASE_DIR, Settings
from .diagnostics import default_input_ledger_dir, filter_log_text
from .http_client import call_json
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
