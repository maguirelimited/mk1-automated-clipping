from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from .config import BASE_DIR, ServiceConfig, Settings
from .http_client import call_json


STAGE_DEFINITIONS: tuple[tuple[str, str, str | None], ...] = (
    ("queued", "Queued", None),
    ("transcription", "Transcription", "transcription_ms"),
    ("selection", "Selection", "selection_ms"),
    ("clipping", "Clipping", "clipping_ms"),
    ("chunking", "Chunking", "chunking_ms"),
    ("handoff", "Output funnel handoff", "handoff_ms"),
    ("complete", "Complete", "total_ms"),
)

ARTIFACT_KEYS = (
    "transcript",
    "transcript_payload",
    "selection",
    "report",
    "analytics",
    "review",
)

FFMPEG_HINT = re.compile(r"(ffmpeg|ffprobe|whisper|RUNNING FFMPEG|RUNNING WHISPER)", re.I)
TRACEBACK_HINT = re.compile(r"(Traceback \(most recent|File \".+\", line )")


def default_input_ledger_dir() -> Path:
    raw = os.environ.get("OPS_INPUT_LEDGER_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return BASE_DIR / "source-input" / "input_service" / "data" / "state" / "input_jobs"


def default_output_funnel_db() -> Path:
    raw = os.environ.get("OPS_OUTPUT_FUNNEL_DB", "").strip()
    if raw:
        return Path(raw).expanduser()
    return BASE_DIR / "output-funnel" / "data" / "output_funnel.sqlite3"


def pipeline_stage_rows(
    *,
    status: str,
    current_stage: str,
    stage_timings: dict[str, Any],
) -> list[dict[str, Any]]:
    timings = stage_timings if isinstance(stage_timings, dict) else {}
    status_l = str(status or "").lower()
    current = str(current_stage or "").strip().lower()
    rows: list[dict[str, Any]] = []
    reached_current = status_l in {"success", "failed"} or not current

    for key, label, timing_key in STAGE_DEFINITIONS:
        ms = timings.get(timing_key) if timing_key else None
        if key == "queued":
            state = "done" if status_l not in {"", "unknown"} else "active"
        elif key == "complete":
            state = "done" if status_l == "success" else ("failed" if status_l == "failed" else "pending")
        elif ms is not None:
            state = "done"
        elif not reached_current and current == key:
            state = "active"
            reached_current = True
        elif reached_current:
            state = "pending"
        else:
            state = "pending"
        rows.append(
            {
                "key": key,
                "label": label,
                "state": state,
                "duration_ms": ms,
                "duration_label": _format_ms(ms),
            }
        )
    return rows


def _format_ms(value: Any) -> str:
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return "—"
    if ms < 1000:
        return f"{ms:.0f} ms"
    return f"{ms / 1000:.2f} s"


def load_input_ledger_record(input_id: str, *, ledger_dir: Path | None = None) -> dict[str, Any] | None:
    clean = str(input_id or "").strip()
    if not clean or ".." in clean or "/" in clean:
        return None
    root = ledger_dir or default_input_ledger_dir()
    path = root / f"{clean}.json"
    if not path.is_file():
        return {"available": False, "input_id": clean, "ledger_path": str(path), "reason": "record not found"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "input_id": clean, "ledger_path": str(path), "reason": str(exc)}
    if not isinstance(payload, dict):
        return {"available": False, "input_id": clean, "ledger_path": str(path), "reason": "invalid record"}
    return {
        "available": True,
        "input_id": clean,
        "ledger_path": str(path),
        "state": payload.get("state"),
        "funnel_id": payload.get("funnel_id"),
        "source_url": payload.get("source_url") or payload.get("url"),
        "file_path": payload.get("file_path"),
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
        "error": payload.get("error"),
        "result": payload.get("result"),
        "record": payload,
    }


def funnel_context(debug: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    policy = debug.get("policy_resolution") if isinstance(debug.get("policy_resolution"), dict) else {}
    funnel = job.get("funnel") if isinstance(job.get("funnel"), dict) else {}
    if not funnel and isinstance(debug.get("funnel"), dict):
        funnel = debug["funnel"]
    return {
        "funnel_id": job.get("funnel_id") or funnel.get("funnel_id") or policy.get("funnel_id"),
        "pipeline_profile": policy.get("pipeline_profile") or policy.get("resolved_pipeline_profile"),
        "funnel_config": policy.get("funnel_config") or policy.get("resolved_funnel_config"),
        "selection_source": policy.get("selection_source"),
        "models_effective": policy.get("models_effective"),
        "policy_resolution": policy,
        "funnel_record": funnel,
    }


def read_local_text(path: str, *, max_bytes: int = 250_000) -> dict[str, Any]:
    clean = str(path or "").strip()
    if not clean:
        return {"ok": False, "error": "empty path"}
    resolved = Path(clean).expanduser().resolve()
    if not resolved.is_file():
        return {"ok": False, "error": "file not found", "path": str(resolved)}
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": str(resolved)}
    truncated = size > max_bytes
    read_size = max_bytes if truncated else size
    try:
        raw = resolved.read_bytes()[:read_size]
        text = raw.decode("utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": str(resolved)}
    return {
        "ok": True,
        "path": str(resolved),
        "size_bytes": size,
        "truncated": truncated,
        "text": text,
    }


def load_artifact_json(path: str, *, max_chars: int = 80_000) -> dict[str, Any]:
    loaded = read_local_text(path, max_bytes=max_chars * 4)
    if not loaded.get("ok"):
        return loaded
    text = str(loaded.get("text") or "")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid JSON: {exc}", "path": loaded.get("path")}
    pretty = json.dumps(payload, indent=2, default=str)
    if len(pretty) > max_chars:
        pretty = pretty[:max_chars] + "\n… [truncated for UI]"
    return {
        "ok": True,
        "path": loaded.get("path"),
        "size_bytes": loaded.get("size_bytes"),
        "truncated": loaded.get("truncated") or len(pretty) >= max_chars,
        "text": pretty,
        "payload": payload,
    }


def transcript_view(debug: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    stats = debug.get("transcript_stats") if isinstance(debug.get("transcript_stats"), dict) else {}
    artifact = artifacts.get("transcript_payload") or artifacts.get("transcript")
    path = ""
    if isinstance(artifact, dict):
        path = str(artifact.get("path") or "")
    if not path:
        path = str(stats.get("artifact_path") or "")
    view: dict[str, Any] = {"stats": stats, "path": path, "segments": [], "preview_text": ""}
    if not path:
        return view
    loaded = load_artifact_json(path, max_chars=120_000)
    if not loaded.get("ok"):
        view["error"] = loaded.get("error")
        return view
    payload = loaded.get("payload")
    if not isinstance(payload, dict):
        view["error"] = "transcript payload is not an object"
        return view
    segments = payload.get("segments") if isinstance(payload.get("segments"), list) else []
    view["segments"] = [
        {
            "start": row.get("start"),
            "end": row.get("end"),
            "text": row.get("text"),
        }
        for row in segments[:200]
        if isinstance(row, dict)
    ]
    text = str(payload.get("text") or payload.get("full_text") or "")
    if len(text) > 4000:
        view["preview_text"] = text[:4000] + "\n… [truncated]"
    else:
        view["preview_text"] = text
    view["json_text"] = loaded.get("text")
    return view


def artifact_views(artifacts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    views: dict[str, dict[str, Any]] = {}
    if not isinstance(artifacts, dict):
        return views
    for key in ARTIFACT_KEYS:
        meta = artifacts.get(key)
        if not isinstance(meta, dict) or not meta.get("exists"):
            continue
        path = str(meta.get("path") or "")
        if not path:
            continue
        if key in {"transcript", "transcript_payload"}:
            continue
        if path.endswith(".json"):
            views[key] = load_artifact_json(path)
        else:
            views[key] = read_local_text(path, max_bytes=40_000)
    return views


def clip_rows(clips: list[Any], artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    clip_files = artifacts.get("clip_files") if isinstance(artifacts.get("clip_files"), list) else []
    by_name = {
        os.path.basename(str(item.get("path") or "")): item
        for item in clip_files
        if isinstance(item, dict)
    }
    rows: list[dict[str, Any]] = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        file_name = str(clip.get("clip_file") or clip.get("job_clip_path") or "")
        artifact = by_name.get(os.path.basename(file_name)) if file_name else None
        rows.append(
            {
                "title": clip.get("title") or clip.get("clip_id") or "—",
                "score": clip.get("composite_score"),
                "start": clip.get("start"),
                "end": clip.get("end"),
                "path": (artifact or {}).get("path") or clip.get("clip_path") or clip.get("job_clip_path"),
                "size_bytes": (artifact or {}).get("size_bytes"),
                "exists": (artifact or {}).get("exists", bool(clip.get("clip_path"))),
                "validation": clip.get("clip_validation"),
            }
        )
    return rows


def ffmpeg_output_lines(errors: list[Any], warnings: list[Any]) -> list[str]:
    lines: list[str] = []
    for bucket in (errors, warnings):
        if not isinstance(bucket, list):
            continue
        for item in bucket:
            if isinstance(item, str):
                if FFMPEG_HINT.search(item):
                    lines.append(item)
                continue
            if not isinstance(item, dict):
                continue
            chunks = [
                str(item.get("message") or ""),
                str(item.get("log_detail") or ""),
                str(item.get("details") or ""),
            ]
            stderr = item.get("stderr")
            if isinstance(stderr, str):
                chunks.append(stderr)
            elif isinstance(stderr, dict):
                chunks.append(json.dumps(stderr, default=str))
            details = item.get("details")
            if isinstance(details, dict):
                chunks.append(json.dumps(details, default=str))
            blob = "\n".join(part for part in chunks if part)
            if FFMPEG_HINT.search(blob):
                lines.append(blob.strip())
    deduped: list[str] = []
    seen: set[str] = set()
    for line in lines:
        key = line[:240]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(line)
    return deduped[:12]


def traceback_lines(errors: list[Any], warnings: list[Any]) -> list[str]:
    lines: list[str] = []
    for bucket in (errors, warnings):
        if not isinstance(bucket, list):
            continue
        for item in bucket:
            text = json.dumps(item, indent=2, default=str) if isinstance(item, dict) else str(item)
            if TRACEBACK_HINT.search(text):
                lines.append(text)
    return lines[:8]


def filter_log_text(text: str, query: str) -> tuple[str, int, int]:
    raw = str(text or "")
    q = str(query or "").strip()
    if not q:
        return raw, raw.count("\n") + (1 if raw else 0), 0
    lines = raw.splitlines()
    q_lower = q.lower()
    matched = [line for line in lines if q_lower in line.lower()]
    return "\n".join(matched), len(lines), len(matched)


def fetch_service_doctor(service: ServiceConfig, *, timeout: float) -> dict[str, Any]:
    ok, payload, status = call_json(service, "/doctor", timeout=timeout)
    return {
        "service": service.key,
        "label": service.label,
        "ok": ok and bool(payload.get("ok", ok)),
        "http_ok": ok,
        "status_code": status,
        "payload": payload,
        "checks": payload.get("checks") if isinstance(payload.get("checks"), list) else [],
        "error": payload.get("error"),
    }


def output_funnel_db_check(db_path: Path | None = None) -> dict[str, Any]:
    path = (db_path or default_output_funnel_db()).expanduser()
    out: dict[str, Any] = {
        "name": "output_funnel_sqlite",
        "path": str(path),
        "exists": path.is_file(),
        "ok": False,
        "detail": "",
    }
    if not path.is_file():
        out["detail"] = "database file not found"
        return out
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            detail = str(row[0]) if row else "unknown"
            out["ok"] = detail.lower() == "ok"
            out["detail"] = detail
            count_row = conn.execute("SELECT COUNT(*) FROM upload_jobs").fetchone()
            out["upload_job_count"] = int(count_row[0]) if count_row else None
        finally:
            conn.close()
    except Exception as exc:
        out["detail"] = str(exc)
    return out


def collect_health_reports(settings: Settings) -> dict[str, Any]:
    doctors: list[dict[str, Any]] = []
    for svc in settings.services:
        if svc.key == "output-funnel":
            ok, payload, status = call_json(svc, "/healthz", timeout=settings.service_timeout_sec)
            db_path = Path(str(payload.get("database_path") or default_output_funnel_db()))
            db_check = output_funnel_db_check(db_path)
            doctors.append(
                {
                    "service": svc.key,
                    "label": svc.label,
                    "ok": ok and db_check.get("ok"),
                    "http_ok": ok,
                    "status_code": status,
                    "payload": payload,
                    "checks": [
                        {
                            "name": "healthz",
                            "ok": ok,
                            "detail": "reachable" if ok else str(payload.get("error") or "unreachable"),
                        },
                        {
                            "name": db_check["name"],
                            "ok": db_check.get("ok"),
                            "detail": db_check.get("detail"),
                        },
                    ],
                    "db_check": db_check,
                }
            )
            continue
        doctors.append(fetch_service_doctor(svc, timeout=settings.service_timeout_sec))
    return {"doctors": doctors, "all_ok": all(bool(item.get("ok")) for item in doctors)}
