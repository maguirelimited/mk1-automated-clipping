import json
import os
import hmac
import subprocess
import sys
import uuid
import time
import re
import shutil
import threading
import queue
from datetime import datetime, timezone
from typing import Any
from urllib import request as urlrequest

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.abspath(os.path.join(BASE_DIR, ".."))


def _load_project_env_file() -> None:
    """Load ``video-automation/.env`` when present (does not override existing env)."""
    if os.environ.get("MK04_ENV", "dev").strip().lower() == "prod":
        return
    env_path = os.path.join(_PROJECT_DIR, ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if not key or key in os.environ:
                    continue
                value = value.strip().strip('"').strip("'")
                os.environ[key] = value
    except OSError:
        pass


_load_project_env_file()

_SCRIPTS_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "scripts"))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
_SOURCE_INPUT_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "..", "source-input", "input_service"))
if _SOURCE_INPUT_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_INPUT_DIR)

from chunk_pipeline import (
    ffmpeg_extract_segment,
    merge_whisper_json_files,
    plan_wallclock_chunks,
    should_use_chunked_transcription,
    whisper_json_for_video,
    write_merged_whisper_json,
)
from analytics_store import persist_feedback_event, persist_run_analytics
from funnel_config import sanitize_funnel_config_basename
from input_service.duplicate_store import DuplicateStore
from pipeline_utils import (
    normalize_segments,
    parse_selection_payload,
    resolved_pipeline_config_path,
    resolve_run_policy,
    parse_time_to_seconds,
    postprocess_segments,
)
from mk04_utils import (
    build_funnel_job_record,
    categorize_error,
    create_job_paths,
    effective_temp_cleanup_policy,
    ensure_paths,
    ffprobe_duration_sec,
    load_config,
    maybe_copy,
    normalize_transcript_payload,
    require_timed_transcript_payload,
    resolve_paths,
    validate_and_repair_selection,
    write_json,
    write_review,
    now_iso,
)
from pipeline_debug_ndjson import (
    write_debug_agent,
    write_debug_main,
    write_debug_mode,
    write_diagnostic,
)
from input_service import ledger as input_ledger

app = Flask(__name__)
PYTHON = sys.executable
# Stable identifier returned in JSON (`pipeline`, `/healthz`); keep unchanged for integrations.
PIPELINE_NAME = "mk0.4"
_JOB_QUEUE: "queue.Queue[dict[str, Any]]" = queue.Queue()
_JOB_WORKERS_STARTED = False
_JOB_WORKERS_LOCK = threading.Lock()
_JOB_RECOVERY_DONE = False


def _assert_supported_prod_launch() -> None:
    if os.environ.get("MK04_ENV", "dev").strip().lower() != "prod":
        return
    required = {
        "MK04_ROOT": "/opt/mk04/prod/current",
        "MK04_CONFIG_ROOT": "/etc/mk04/prod",
        "MK04_RUNTIME_ROOT": "/var/lib/mk04/prod",
        "MK04_LOG_ROOT": "/var/log/mk04/prod",
        "PIPELINE_CONFIG_PATH": "/etc/mk04/prod",
        "VIDEO_PIPELINE_PROFILES_PATH": "/etc/mk04/prod",
        "FUNNEL_CONFIG_DIR": "/etc/mk04/prod",
        "VIDEO_AUTOMATION_INPUT_DIR": "/var/lib/mk04/prod",
        "OUTPUT_FUNNEL_URL": "http://127.0.0.1:5055",
    }
    for name, root in required.items():
        raw = os.environ.get(name, "").strip()
        if not raw:
            raise RuntimeError(f"{name} is required when MK04_ENV=prod; use deploy/scripts/run-video-automation.sh prod")
        if root.startswith("http"):
            if raw != root:
                raise RuntimeError(f"{name} must be {root} when MK04_ENV=prod (got {raw})")
            continue
        resolved = os.path.abspath(os.path.expanduser(raw))
        root_path = os.path.abspath(root)
        if resolved != root_path and not resolved.startswith(root_path + os.sep):
            raise RuntimeError(f"{name}={resolved} must be under {root_path} when MK04_ENV=prod")


_assert_supported_prod_launch()


def _secret_configured(env_name: str) -> str:
    return os.environ.get(env_name, "").strip()


def _check_shared_secret(env_name: str, header_name: str) -> tuple[Any, int] | None:
    expected = _secret_configured(env_name)
    if not expected:
        return None
    provided = (request.headers.get(header_name) or "").strip()
    if not hmac.compare_digest(provided, expected):
        return _fail(
            f"unauthorized: missing or invalid {header_name}",
            status_code=401,
        )
    return None


@app.before_request
def _require_video_automation_secret():
    if request.endpoint in {"healthz"}:
        return None
    return _check_shared_secret("VIDEO_AUTOMATION_SECRET", "X-Video-Automation-Secret")


def _debug_log(hypothesis_id: str, location: str, message: str, data: dict):
    write_debug_main(PIPELINE_NAME, hypothesis_id, location, message, data)


def _agent_debug_log(
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, object],
):
    write_debug_agent(run_id, hypothesis_id, location, message, data)


def _debug_mode_log(
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, object],
):
    write_debug_mode(run_id, hypothesis_id, location, message, data)


def _pipeline_diagnostic_log(
    hypothesis_id: str, location: str, message: str, data: dict
) -> None:
    """Ingress/process breadcrumbs when ``PIPELINE_DIAGNOSTIC_LOG_PATH`` is set."""
    write_diagnostic(PIPELINE_NAME, hypothesis_id, location, message, data)


def _lift_adapter_wrapped_video_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Lift process boundary fields from common automation-wrapper payloads.

    Some automation tools send ``{ "json": { "video_path": "...", ... } }`` or
    similar wrapper objects; without lifting, the input fields stay nested.
    """
    if not isinstance(data, dict):
        return {}
    out = dict(data)
    lift_keys = ("video", "video_path", "input_id", "job_id", "funnel_id", "funnel_config")
    for wrap in ("json", "body", "data", "item"):
        if wrap not in out:
            continue
        inner: Any = out[wrap]
        if isinstance(inner, str) and inner.strip().startswith("{"):
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                continue
        if not isinstance(inner, dict):
            continue
        for lk in lift_keys:
            if lk not in out and inner.get(lk) not in (None, ""):
                val = inner[lk]
                if val is not None and str(val).strip():
                    out[lk] = val
        nested = inner.get("json")
        if isinstance(nested, dict):
            for lk in lift_keys:
                if lk not in out and nested.get(lk) not in (None, ""):
                    val = nested[lk]
                    if val is not None and str(val).strip():
                        out[lk] = val
    return out


def _selection_subprocess_http_status(detail: str | None) -> int:
    """Selection ran but produced no valid clips (tunable via HTTP selection + profile)."""
    d = str(detail or "")
    if "SELECTOR_REJECTED_AFTER_POSTFILTER" in d:
        return 422
    return 500


class SelectorCallError(RuntimeError):
    def __init__(self, message: str, details: str, *, status_code: int):
        super().__init__(message)
        self.details = details
        self.status_code = status_code


def _progress_enabled() -> bool:
    return os.environ.get("PIPELINE_PROGRESS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _progress(message: str, *, job_id: str | None = None) -> None:
    """Human-readable pipeline breadcrumbs on the app server terminal."""
    if not _progress_enabled():
        return
    prefix = "[pipeline]"
    if job_id:
        prefix = f"[pipeline job={job_id}]"
    print(f"{prefix} {message}", flush=True)


def _output_funnel_handoff_enabled() -> bool:
    raw = os.environ.get("OUTPUT_FUNNEL_HANDOFF_ENABLED", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _output_funnel_url() -> str | None:
    raw = os.environ.get("OUTPUT_FUNNEL_URL", "http://127.0.0.1:5055").strip()
    return raw.rstrip("/") if raw else None


def _output_funnel_timeout_sec() -> float:
    raw = os.environ.get("OUTPUT_FUNNEL_HANDOFF_TIMEOUT_SEC", "2").strip()
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 2.0


def _agent_debug_log(
    *,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, Any] | None = None,
    run_id: str = "pre-fix",
) -> None:
    # #region agent log
    try:
        payload = {
            "sessionId": "8aae3e",
            "timestamp": int(time.time() * 1000),
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "runId": run_id,
        }
        with open(
            "/Users/anthonymaguire/VAmk0.4/.cursor/debug-8aae3e.log",
            "a",
            encoding="utf-8",
        ) as handle:
            handle.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        pass
    # #endregion


def _try_output_funnel_handoff(report: dict[str, Any], *, report_path: str) -> dict[str, Any]:
    """Best-effort downstream registration; never blocks successful clip output."""
    if not _output_funnel_handoff_enabled():
        return {"enabled": False, "ok": False, "reason": "disabled"}
    base_url = _output_funnel_url()
    if not base_url:
        return {"enabled": False, "ok": False, "reason": "missing_output_funnel_url"}
    funnel = report.get("funnel") if isinstance(report.get("funnel"), dict) else {}
    _agent_debug_log(
        hypothesis_id="A",
        location="app.py:_try_output_funnel_handoff",
        message="handoff payload funnel context",
        data={
            "job_id": report.get("job_id"),
            "top_level_funnel_id": report.get("funnel_id"),
            "funnel_record_funnel_id": funnel.get("funnel_id"),
            "clip_count": len(report.get("clips") or []),
            "handoff_url": base_url,
        },
    )
    payload = {
        "report_path": os.path.abspath(report_path),
        "payload": report,
    }
    try:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        secret = os.environ.get("OUTPUT_FUNNEL_SECRET", "").strip()
        if secret:
            headers["X-Output-Funnel-Secret"] = secret
        req = urlrequest.Request(
            f"{base_url}/registrations/from-job",
            data=body,
            headers=headers,
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=_output_funnel_timeout_sec()) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text or "{}")
            except json.JSONDecodeError:
                parsed = {"raw_response": text[:500]}
            ok = 200 <= int(resp.status) < 300 and parsed.get("success") is not False
            processing = parsed.get("processing") if isinstance(parsed.get("processing"), dict) else {}
            schedule = processing.get("schedule") if isinstance(processing.get("schedule"), dict) else {}
            _agent_debug_log(
                hypothesis_id="C",
                location="app.py:_try_output_funnel_handoff",
                message="handoff response received",
                data={
                    "ok": ok,
                    "status_code": int(resp.status),
                    "has_processing": bool(processing),
                    "auto_schedule_enabled": processing.get("auto_schedule_enabled"),
                    "schedule_results": schedule.get("results"),
                },
            )
            return {
                "enabled": True,
                "ok": ok,
                "url": base_url,
                "status_code": int(resp.status),
                "response": parsed,
                "at": now_iso(),
            }
    except Exception as exc:
        _agent_debug_log(
            hypothesis_id="C",
            location="app.py:_try_output_funnel_handoff",
            message="handoff request failed",
            data={"error": repr(exc), "handoff_url": base_url},
        )
        return {
            "enabled": True,
            "ok": False,
            "url": base_url,
            "error": repr(exc),
            "at": now_iso(),
        }


def _output_funnel_schedule_lines(handoff: dict[str, Any]) -> list[str]:
    response = handoff.get("response") if isinstance(handoff.get("response"), dict) else {}
    processing = response.get("processing") if isinstance(response.get("processing"), dict) else {}
    schedule = processing.get("schedule") if isinstance(processing.get("schedule"), dict) else {}
    results = schedule.get("results") if isinstance(schedule.get("results"), list) else []
    lines: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        upload_job_id = item.get("upload_job_id")
        if item.get("scheduled") is True:
            publish_at = item.get("platform_publish_at") or item.get("scheduled_at")
            lines.append(f"upload_job_id={upload_job_id} publish_at={publish_at}")
        else:
            lines.append(f"upload_job_id={upload_job_id} schedule_failed={item.get('reason')}")
    return lines


def _fail(message: str, *, log_detail=None, status_code=500):
    if log_detail is not None:
        print(f"[process] {message}: {log_detail}", flush=True)
    else:
        print(f"[process] {message}", flush=True)
    body = {"success": False, "error": message, "pipeline": PIPELINE_NAME}
    return jsonify(body), status_code


def _parse_segments_from_selector_output(
    raw_out: str,
    *,
    selector_max_clips: int,
    min_duration_sec: float,
    max_duration_sec: float,
    max_overlap_sec: float,
    video_duration_sec: float | None,
) -> list[dict]:
    stripped = raw_out.strip()
    envelope = _parse_script_envelope(stripped)
    if envelope and isinstance(envelope.get("clips"), list):
        env_clips = envelope["clips"]
        if isinstance(env_clips, list) and len(env_clips) == 0:
            return []
        return normalize_segments(env_clips)
    parsed = parse_selection_payload(stripped)
    return postprocess_segments(
        normalize_segments(parsed),
        max_clips=selector_max_clips,
        min_duration_sec=min_duration_sec,
        max_duration_sec=max_duration_sec,
        max_overlap_sec=max_overlap_sec,
        video_duration_sec=video_duration_sec,
    )


def _parse_script_envelope(text: str) -> dict[str, object] | None:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "ok" in parsed and "script" in parsed:
            return parsed
    return None


def _selector_prompt_stats_from_output(raw_out: str) -> dict[str, object] | None:
    envelope = _parse_script_envelope(raw_out)
    if envelope and isinstance(envelope.get("selector_prompt"), dict):
        return dict(envelope["selector_prompt"])
    return None


_SELECTOR_HARD_MAX_TRANSCRIPT_CHARS = 120_000
_SELECTOR_HARD_MAX_SEGMENT_LINES = 500
_SELECTOR_SAFE_MAX_TRANSCRIPT_CHARS = 90_000
_SELECTOR_SAFE_MAX_SEGMENT_LINES = 450


def _format_selector_ts(total_sec: float) -> str:
    total = max(0.0, float(total_sec))
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _timed_transcript_segments(transcript_payload: dict[str, Any]) -> list[dict[str, object]]:
    segments = transcript_payload.get("segments")
    if not isinstance(segments, list):
        return []
    out: list[dict[str, object]] = []
    for row in segments:
        if not isinstance(row, dict):
            continue
        try:
            start = float(row.get("start"))
            end = float(row.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        out.append({"start": start, "end": end, "text": str(row.get("text") or "").strip()})
    return out


def _selector_line_chars(segment: dict[str, object], *, window_start_sec: float = 0.0) -> int:
    start = float(segment["start"]) - float(window_start_sec)
    end = float(segment["end"]) - float(window_start_sec)
    text = str(segment.get("text") or "").strip() or "(no voiced text)"
    return len(f"[{_format_selector_ts(start)} -> {_format_selector_ts(end)}] {text}") + 1


def _selector_prompt_budget(segments: list[dict[str, object]]) -> dict[str, object]:
    return {
        "segment_count": len(segments),
        "estimated_prompt_chars": sum(_selector_line_chars(seg) for seg in segments),
        "safe_max_transcript_chars": _SELECTOR_SAFE_MAX_TRANSCRIPT_CHARS,
        "safe_max_segment_lines": _SELECTOR_SAFE_MAX_SEGMENT_LINES,
        "hard_max_transcript_chars": _SELECTOR_HARD_MAX_TRANSCRIPT_CHARS,
        "hard_max_segment_lines": _SELECTOR_HARD_MAX_SEGMENT_LINES,
    }


def _plan_selector_windows(transcript_payload: dict[str, Any]) -> tuple[list[dict[str, object]], dict[str, object]]:
    segments = _timed_transcript_segments(transcript_payload)
    budget = _selector_prompt_budget(segments)
    if (
        len(segments) <= _SELECTOR_SAFE_MAX_SEGMENT_LINES
        and int(budget["estimated_prompt_chars"]) <= _SELECTOR_SAFE_MAX_TRANSCRIPT_CHARS
    ):
        window = {
            "index": 0,
            "start_sec": float(segments[0]["start"]) if segments else 0.0,
            "end_sec": float(segments[-1]["end"]) if segments else 0.0,
            "segments": segments,
            "uses_original_transcript": True,
        }
        return [window], {**budget, "window_count": 1, "segmented": False}

    windows: list[dict[str, object]] = []
    current: list[dict[str, object]] = []
    current_chars = 0
    current_start = 0.0
    for segment in segments:
        if not current:
            current_start = float(segment["start"])
            current_chars = 0
        next_chars = _selector_line_chars(segment, window_start_sec=current_start)
        would_exceed_lines = len(current) >= _SELECTOR_SAFE_MAX_SEGMENT_LINES
        would_exceed_chars = current_chars + next_chars > _SELECTOR_SAFE_MAX_TRANSCRIPT_CHARS
        if current and (would_exceed_lines or would_exceed_chars):
            windows.append(
                {
                    "index": len(windows),
                    "start_sec": current_start,
                    "end_sec": float(current[-1]["end"]),
                    "segments": current,
                    "uses_original_transcript": False,
                    "estimated_prompt_chars": current_chars,
                }
            )
            current = []
            current_start = float(segment["start"])
            current_chars = 0
            next_chars = _selector_line_chars(segment, window_start_sec=current_start)
        current.append(segment)
        current_chars += next_chars
    if current:
        windows.append(
            {
                "index": len(windows),
                "start_sec": current_start,
                "end_sec": float(current[-1]["end"]),
                "segments": current,
                "uses_original_transcript": False,
                "estimated_prompt_chars": current_chars,
            }
        )
    return windows, {**budget, "window_count": len(windows), "segmented": len(windows) > 1}


def _write_selector_window_transcript(
    window: dict[str, object],
    *,
    temp_root: str,
    filename: str,
    job_id: str,
) -> str:
    window_start = float(window["start_sec"])
    local_segments: list[dict[str, object]] = []
    texts: list[str] = []
    for idx, segment in enumerate(window.get("segments") or []):
        if not isinstance(segment, dict):
            continue
        start = max(0.0, float(segment["start"]) - window_start)
        end = max(start, float(segment["end"]) - window_start)
        text = str(segment.get("text") or "").strip()
        if text:
            texts.append(text)
        local_segments.append({"id": idx, "start": start, "end": end, "text": text})
    path = os.path.abspath(
        os.path.join(temp_root, f"{filename}_{job_id}_selector_w{int(window['index']):03d}.json")
    )
    write_json(
        path,
        {
            "text": " ".join(texts).strip(),
            "segments": local_segments,
            "duration": max(0.001, float(window["end_sec"]) - window_start),
        },
    )
    return path


def _run_selector_subprocess(
    *,
    script_select: str,
    transcript_path: str,
    selection_options: dict[str, object],
    selector_max_clips: int,
    min_duration_sec: float,
    max_duration_sec: float,
    max_overlap_sec: float,
    video_duration_sec: float | None,
    label: str,
) -> tuple[list[dict], dict[str, object] | None]:
    result = subprocess.run(
        [PYTHON, script_select, transcript_path, json.dumps(selection_options)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        details = result.stderr or result.stdout
        raise SelectorCallError(
            f"Selection failed for {label}",
            details,
            status_code=_selection_subprocess_http_status(str(details)),
        )
    raw_out = result.stdout.strip()
    try:
        segments = _parse_segments_from_selector_output(
            raw_out,
            selector_max_clips=selector_max_clips,
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
            max_overlap_sec=max_overlap_sec,
            video_duration_sec=video_duration_sec,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        raise SelectorCallError(
            f"Invalid selection output JSON ({label})",
            str(exc),
            status_code=500,
        ) from exc
    return segments, _selector_prompt_stats_from_output(raw_out)


def _record_selector_prompt_warning(
    *,
    warnings: list[dict[str, object]],
    label: str,
    prompt_stats: dict[str, object] | None,
) -> bool:
    if not prompt_stats:
        return False
    truncated = bool(prompt_stats.get("truncated_by_segment_limit")) or bool(
        prompt_stats.get("truncated_by_char_limit")
    )
    if truncated:
        warnings.append(
            categorize_error(
                "selection",
                "selector_prompt_truncated",
                "Selector prompt guardrail was hit; transcript coverage may be incomplete for this window.",
                {"selector_call": label, "selector_prompt": prompt_stats},
            )
        )
    return truncated


def _select_candidates_from_transcript(
    *,
    script_select: str,
    transcript_path: str,
    transcript_payload: dict[str, Any],
    temp_root: str,
    filename: str,
    job_id: str,
    max_clips: int,
    min_duration_sec: float,
    max_duration_sec: float,
    max_overlap_sec: float,
    include_reasons: bool,
    include_clip_metadata: bool,
    selection_model_used: str,
    source_video_duration_sec: float,
    selector_video_duration_sec: float,
    base_timeline_offset_sec: float = 0.0,
    selector_window_paths: list[str] | None = None,
    warnings: list[dict[str, object]] | None = None,
    context_label: str = "transcript",
    allow_empty_segmented_windows: bool = True,
) -> tuple[list[dict], dict[str, object]]:
    warnings_ref = warnings if warnings is not None else []
    planned_windows, plan = _plan_selector_windows(transcript_payload)
    candidates: list[dict] = []
    selector_calls: list[dict[str, object]] = []
    prompt_truncation_count = 0
    empty_window_count = 0
    segmented = bool(plan.get("segmented"))
    for window in planned_windows:
        window_index = int(window["index"])
        window_start = float(window["start_sec"])
        window_end = float(window["end_sec"])
        use_original = bool(window.get("uses_original_transcript")) and not segmented
        call_path = transcript_path
        timeline_offset = float(base_timeline_offset_sec)
        selector_duration = float(selector_video_duration_sec)
        if not use_original:
            call_path = _write_selector_window_transcript(
                window,
                temp_root=temp_root,
                filename=filename,
                job_id=job_id,
            )
            if selector_window_paths is not None:
                selector_window_paths.append(call_path)
            timeline_offset = float(base_timeline_offset_sec) + window_start
            selector_duration = max(0.001, window_end - window_start)

        label = f"{context_label}:window_{window_index}"
        opts: dict[str, object] = {
            "max_clips": max_clips,
            "min_duration_sec": min_duration_sec,
            "max_duration_sec": max_duration_sec,
            "max_overlap_sec": max_overlap_sec,
            "video_duration_sec": selector_duration,
            "include_reasons": include_reasons,
            "include_clip_metadata": include_clip_metadata,
            "selection_model": selection_model_used,
        }
        if timeline_offset > 0 or not use_original:
            opts["timeline_offset_sec"] = timeline_offset
            opts["is_chunk_slice"] = True
        try:
            part, prompt_stats = _run_selector_subprocess(
                script_select=script_select,
                transcript_path=call_path,
                selection_options=opts,
                selector_max_clips=max_clips,
                min_duration_sec=min_duration_sec,
                max_duration_sec=max_duration_sec,
                max_overlap_sec=max_overlap_sec,
                video_duration_sec=source_video_duration_sec,
                label=label,
            )
        except SelectorCallError as exc:
            if (
                allow_empty_segmented_windows
                and segmented
                and "SELECTOR_REJECTED_AFTER_POSTFILTER" in str(exc.details)
            ):
                empty_window_count += 1
                warnings_ref.append(
                    categorize_error(
                        "selection",
                        "selector_window_empty",
                        "No clips survived selector post-filter for this transcript window.",
                        {"selector_call": label, "details": exc.details},
                    )
                )
                selector_calls.append(
                    {
                        "label": label,
                        "window_index": window_index,
                        "candidate_count": 0,
                        "empty": True,
                        "start_sec": window_start,
                        "end_sec": window_end,
                    }
                )
                continue
            raise
        if _record_selector_prompt_warning(
            warnings=warnings_ref, label=label, prompt_stats=prompt_stats
        ):
            prompt_truncation_count += 1
        candidates.extend(part)
        selector_calls.append(
            {
                "label": label,
                "window_index": window_index,
                "candidate_count": len(part),
                "start_sec": window_start,
                "end_sec": window_end,
                "timeline_offset_sec": timeline_offset,
                "selector_duration_sec": selector_duration,
                "used_original_transcript": use_original,
                "selector_prompt": prompt_stats,
            }
        )
    summary = {
        **plan,
        "context_label": context_label,
        "selector_call_count": len(selector_calls),
        "candidate_count": len(candidates),
        "prompt_truncation_count": prompt_truncation_count,
        "empty_window_count": empty_window_count,
        "windows": selector_calls,
    }
    return candidates, summary


def _aggregate_selector_candidates(
    candidates: list[dict],
    *,
    max_clips: int,
    min_duration_sec: float,
    max_duration_sec: float,
    max_overlap_sec: float,
    video_duration_sec: float | None,
) -> list[dict]:
    return postprocess_segments(
        candidates,
        max_clips=max_clips,
        min_duration_sec=min_duration_sec,
        max_duration_sec=max_duration_sec,
        max_overlap_sec=max_overlap_sec,
        video_duration_sec=video_duration_sec,
    )


def _resolve_input_video_path(video_name: str) -> tuple[str, str]:
    config = load_config()
    input_root = ensure_paths(config)["input"]
    os.makedirs(input_root, exist_ok=True)

    normalized_name = os.path.basename(str(video_name or "").strip())
    if not normalized_name:
        normalized_name = f"input_{uuid.uuid4().hex[:10]}.mp4"

    video_path = os.path.normpath(os.path.join(input_root, normalized_name))
    if not video_path.startswith(input_root + os.sep):
        raise ValueError("Invalid video path")

    return normalized_name, video_path


def _resolve_output_clip_path(clip_name: str) -> tuple[str, str]:
    config = load_config()
    output_root = ensure_paths(config)["output"]
    os.makedirs(output_root, exist_ok=True)

    normalized_name = os.path.basename(str(clip_name or "").strip())
    if not normalized_name:
        raise ValueError("Missing clip filename")

    clip_path = os.path.normpath(os.path.join(output_root, normalized_name))
    if not clip_path.startswith(output_root + os.sep):
        raise ValueError("Invalid clip path")
    return normalized_name, clip_path


def _save_uploaded_video_file(file_storage: Any) -> tuple[str, str]:
    raw_name = getattr(file_storage, "filename", "") or ""
    safe_name = secure_filename(raw_name)
    if not safe_name:
        safe_name = f"upload_{uuid.uuid4().hex[:10]}.mp4"
    input_root = ensure_paths(load_config())["input"]
    os.makedirs(input_root, exist_ok=True)
    stem, ext = os.path.splitext(safe_name)
    candidate = safe_name
    dest = os.path.abspath(os.path.join(input_root, candidate))
    while os.path.exists(dest):
        candidate = f"{stem}_{uuid.uuid4().hex[:8]}{ext or '.mp4'}"
        dest = os.path.abspath(os.path.join(input_root, candidate))
    if not dest.startswith(os.path.abspath(input_root) + os.sep):
        raise ValueError("Invalid upload filename")
    file_storage.save(dest)
    return candidate, dest


def _request_payload() -> dict[str, Any]:
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        return payload if isinstance(payload, dict) else {}
    form_payload: dict[str, Any] = {}
    for key, value in request.form.items():
        text = str(value).strip()
        if key in ("selection", "pipeline", "funnel_config") and text.startswith(("{", "[")):
            try:
                form_payload[key] = json.loads(text)
                continue
            except json.JSONDecodeError:
                pass
        form_payload[key] = value
    return form_payload


def _resolve_job_video_input(payload: dict[str, Any]) -> tuple[str | None, str, str, dict[str, Any] | None]:
    config = load_config()
    input_root = os.path.abspath(ensure_paths(config)["input"])
    file_storage = request.files.get("video_file") or request.files.get("file")
    if file_storage is not None and getattr(file_storage, "filename", ""):
        _, video_path = _save_uploaded_video_file(file_storage)
        return None, os.path.abspath(video_path), "upload", None

    raw_input_id = payload.get("input_id") or payload.get("source_input_id")
    input_id = str(raw_input_id).strip() if raw_input_id is not None else ""
    if input_id:
        ledger_record = input_ledger.load_record(input_id)
        ledger_path = input_ledger.resolve_file_path(input_id)
        return input_id, os.path.abspath(str(ledger_path)), "input_ledger", ledger_record

    raw_video = payload.get("video")
    raw_path = payload.get("video_path") or payload.get("path")
    if raw_video is not None and str(raw_video).strip():
        video_arg = str(raw_video).strip()
    elif raw_path is not None and str(raw_path).strip():
        video_arg = str(raw_path).strip()
    else:
        raise ValueError("Missing input video. Send multipart `video_file`, JSON `video`, `video_path`, or `input_id`.")

    if os.path.isabs(video_arg) or os.sep in video_arg or (os.altsep and os.altsep in video_arg):
        candidate = os.path.abspath(os.path.expanduser(video_arg))
        if not _is_inside(input_root, candidate):
            raise ValueError("Existing video paths must stay inside the configured input folder.")
        return None, candidate, "input_folder_path", None

    _, input_video_path = _resolve_input_video_path(os.path.basename(video_arg.rstrip("/")))
    return None, os.path.abspath(input_video_path), "input_folder", None


_JOB_ID_RE = re.compile(r"^job_\d{8}T\d{6}Z_[a-f0-9]{8}$")
_DEFAULT_JOBS_LIMIT = 25
_MAX_JOBS_LIMIT = 100


def _inspection_urls(job_id: str) -> dict[str, str]:
    return {
        "job_url": f"/jobs/{job_id}",
        "status_url": f"/jobs/{job_id}",
        "outputs_url": f"/jobs/{job_id}/outputs",
        "debug_url": f"/jobs/{job_id}/debug",
    }


def _jobs_root_readonly() -> str:
    return os.path.abspath(resolve_paths(load_config())["jobs"])


def _new_job_id() -> str:
    return f"job_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"


def _valid_job_id(job_id: str) -> bool:
    return bool(_JOB_ID_RE.fullmatch(str(job_id or "").strip()))


def _is_inside(root: str, path: str) -> bool:
    try:
        return (
            os.path.commonpath([os.path.abspath(root), os.path.abspath(path)])
            == os.path.abspath(root)
        )
    except ValueError:
        return False


def _load_json_object(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _parse_jobs_limit(raw: str | None) -> int:
    if raw is None or str(raw).strip() == "":
        return _DEFAULT_JOBS_LIMIT
    try:
        limit = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return min(limit, _MAX_JOBS_LIMIT)


def _timestamp_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _artifact_file(path: str) -> dict[str, object]:
    exists = os.path.isfile(path)
    out: dict[str, object] = {"path": os.path.abspath(path), "exists": exists}
    if exists:
        try:
            out["size_bytes"] = os.path.getsize(path)
        except OSError:
            pass
    return out


def _current_stage_from_report(report: dict[str, Any]) -> str:
    explicit = report.get("current_stage")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    status = str(report.get("status") or "").strip()
    if status in ("queued", "success", "failed"):
        return status
    timings = report.get("stage_timings_ms") if isinstance(report.get("stage_timings_ms"), dict) else {}
    clips = report.get("clips") if isinstance(report.get("clips"), list) else []
    if clips:
        return "complete"
    if "selection_ms" in timings:
        return "clipping"
    if "transcription_ms" in timings:
        return "selection"
    if status == "running":
        return "transcription"
    return status or "unknown"


def _job_artifacts(job_dir: str) -> dict[str, object]:
    clips_dir = os.path.join(job_dir, "clips")
    clip_files: list[dict[str, object]] = []
    if os.path.isdir(clips_dir):
        try:
            for entry in os.scandir(clips_dir):
                if entry.is_file(follow_symlinks=False):
                    clip_files.append(_artifact_file(entry.path))
        except OSError:
            clip_files = []
    clip_files.sort(key=lambda x: str(x.get("path") or ""))
    return {
        "job_dir": os.path.abspath(job_dir),
        "report": _artifact_file(os.path.join(job_dir, "report.json")),
        "review": _artifact_file(os.path.join(job_dir, "review.md")),
        "transcript": _artifact_file(os.path.join(job_dir, "transcript.json")),
        "transcript_payload": _artifact_file(os.path.join(job_dir, "transcript_payload.json")),
        "selection": _artifact_file(os.path.join(job_dir, "selection.json")),
        "analytics": _artifact_file(os.path.join(job_dir, "analytics.json")),
        "clips_dir": {"path": os.path.abspath(clips_dir), "exists": os.path.isdir(clips_dir)},
        "clip_files": clip_files,
    }


def _job_summary(report: dict[str, Any], job_dir: str) -> dict[str, object]:
    clips = report.get("clips") if isinstance(report.get("clips"), list) else []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    artifacts = _job_artifacts(job_dir)
    job_id = str(report.get("job_id") or "")
    summary: dict[str, object] = {
        "job_id": job_id,
        "input_video_name": report.get("input_video_name"),
        "source_video": report.get("input_video_name"),
        "status": report.get("status"),
        "current_stage": _current_stage_from_report(report),
        "created_at": report.get("created_at"),
        "started_at": report.get("started_at"),
        "completed_at": report.get("completed_at"),
        "input_id": report.get("input_id"),
        "clip_count": len(clips),
        "warning_count": len(warnings),
        "error_count": len(errors),
        "artifacts": {
            "report_exists": bool((artifacts["report"] or {}).get("exists")),
            "review_exists": bool((artifacts["review"] or {}).get("exists")),
            "selection_exists": bool((artifacts["selection"] or {}).get("exists")),
            "transcript_payload_exists": bool((artifacts["transcript_payload"] or {}).get("exists")),
            "analytics_exists": bool((artifacts["analytics"] or {}).get("exists")),
            "clip_file_count": len(artifacts.get("clip_files") or []),
        },
    }
    if job_id:
        summary.update(_inspection_urls(job_id))
    return summary


def _iter_job_reports() -> list[tuple[str, str, dict[str, Any]]]:
    jobs_root = _jobs_root_readonly()
    if not os.path.isdir(jobs_root):
        return []
    records: list[tuple[str, str, dict[str, Any]]] = []
    try:
        entries = list(os.scandir(jobs_root))
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        job_dir = os.path.abspath(entry.path)
        if not _is_inside(jobs_root, job_dir):
            continue
        report_path = os.path.join(job_dir, "report.json")
        report = _load_json_object(report_path)
        if report is None:
            continue
        records.append((job_dir, report_path, report))
    return records


def _job_sort_key(record: tuple[str, str, dict[str, Any]]) -> float:
    job_dir, report_path, report = record
    for key in ("created_at", "completed_at"):
        ts = _timestamp_epoch(report.get(key))
        if ts is not None:
            return ts
    try:
        return os.path.getmtime(report_path)
    except OSError:
        try:
            return os.path.getmtime(job_dir)
        except OSError:
            return 0.0


def _find_job_report(job_id: str) -> tuple[str, str, dict[str, Any]] | str | None:
    if not _valid_job_id(job_id):
        return "invalid"
    matches = [
        record for record in _iter_job_reports() if str(record[2].get("job_id") or "") == job_id
    ]
    if not matches:
        return None
    if len(matches) > 1:
        return "ambiguous"
    return matches[0]


def _transcript_stats(job_dir: str) -> dict[str, object]:
    stats_path = os.path.join(job_dir, "transcript_payload.json")
    if not os.path.isfile(stats_path):
        stats_path = os.path.join(job_dir, "transcript.json")
    payload = _load_json_object(stats_path)
    if payload is None:
        return {"available": False}
    segments = payload.get("segments") if isinstance(payload.get("segments"), list) else []
    starts: list[float] = []
    ends: list[float] = []
    for row in segments:
        if not isinstance(row, dict):
            continue
        try:
            start = float(row.get("start"))
            end = float(row.get("end"))
        except (TypeError, ValueError):
            continue
        if end > start:
            starts.append(start)
            ends.append(end)
    text = str(payload.get("text") or payload.get("full_text") or "")
    return {
        "available": True,
        "artifact_path": os.path.abspath(stats_path),
        "segment_count": len(segments),
        "timed_segment_count": len(ends),
        "text_char_count": len(text),
        "language": payload.get("language"),
        "duration_sec": payload.get("duration"),
        "first_segment_start_sec": min(starts) if starts else None,
        "last_segment_end_sec": max(ends) if ends else None,
    }


def _compact_clip(item: Any) -> dict[str, object]:
    if not isinstance(item, dict):
        return {}
    keep = (
        "clip_id",
        "clip_index",
        "start",
        "end",
        "duration_sec",
        "clip_file",
        "clip_url",
        "clip_path",
        "job_clip_path",
        "title",
        "hook",
        "caption",
        "reason",
        "scores",
        "composite_score",
        "clip_validation",
    )
    return {key: item[key] for key in keep if key in item}


def _selection_summary(job_dir: str) -> dict[str, object]:
    selection_path = os.path.join(job_dir, "selection.json")
    payload = _load_json_object(selection_path)
    if payload is None:
        return {"available": False}
    clips = payload.get("clips") if isinstance(payload.get("clips"), list) else []
    warnings = (
        payload.get("validation_warnings")
        if isinstance(payload.get("validation_warnings"), list)
        else []
    )
    return {
        "available": True,
        "artifact_path": os.path.abspath(selection_path),
        "clip_count": len(clips),
        "validation_warning_count": len(warnings),
        "clips": [_compact_clip(c) for c in clips],
        "validation_warnings": warnings,
    }


def _clip_validation_issues(clips: list[Any]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    for idx, clip in enumerate(clips, start=1):
        if not isinstance(clip, dict):
            continue
        validation = clip.get("clip_validation")
        if isinstance(validation, dict):
            if validation.get("ok") is False:
                issues.append(
                    {
                        "clip_index": clip.get("clip_index", idx),
                        "clip_id": clip.get("clip_id"),
                        "validation": validation,
                    }
                )
        else:
            issues.append(
                {
                    "clip_index": clip.get("clip_index", idx),
                    "clip_id": clip.get("clip_id"),
                    "issue": "missing_clip_validation",
                }
            )
    return issues


def _job_debug_summary(job_dir: str, report: dict[str, Any]) -> dict[str, object]:
    clips = report.get("clips") if isinstance(report.get("clips"), list) else []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    summary = _job_summary(report, job_dir)
    return {
        "success": True,
        "pipeline": PIPELINE_NAME,
        "job": summary,
        "status": report.get("status"),
        "current_stage": _current_stage_from_report(report),
        "errors": errors,
        "warnings": warnings,
        "stage_timings_ms": report.get("stage_timings_ms") or {},
        "clips": [_compact_clip(c) for c in clips],
        "clip_validation_issues": _clip_validation_issues(clips),
        "artifacts": _job_artifacts(job_dir),
        "transcript_stats": _transcript_stats(job_dir),
        "selection_summary": _selection_summary(job_dir),
        "selector": report.get("selector") or {},
        "policy_resolution": report.get("policy_resolution") or {},
        "chunked": report.get("chunked", False),
        "chunking": report.get("chunking"),
    }


def _job_status_payload(job_dir: str, report: dict[str, Any]) -> dict[str, object]:
    clips = report.get("clips") if isinstance(report.get("clips"), list) else []
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    return {
        "success": str(report.get("status") or "") != "failed",
        "pipeline": PIPELINE_NAME,
        "job_id": report.get("job_id"),
        "status": report.get("status"),
        "current_stage": _current_stage_from_report(report),
        "created_at": report.get("created_at"),
        "started_at": report.get("started_at"),
        "completed_at": report.get("completed_at"),
        "input_id": report.get("input_id"),
        "input_video_name": report.get("input_video_name"),
        "source_video": report.get("input_video_name"),
        "errors": errors,
        "warnings": warnings,
        "timings": report.get("stage_timings_ms") or {},
        "stage_timings_ms": report.get("stage_timings_ms") or {},
        "artifacts": _job_artifacts(job_dir),
        "clips": [_compact_clip(c) for c in clips] if report.get("status") == "success" else [],
        **_inspection_urls(str(report.get("job_id") or "")),
    }


def _job_outputs_payload(job_dir: str, report: dict[str, Any]) -> dict[str, object]:
    status = str(report.get("status") or "")
    clips = report.get("clips") if isinstance(report.get("clips"), list) else []
    artifacts = _job_artifacts(job_dir)
    return {
        "success": status == "success",
        "pipeline": PIPELINE_NAME,
        "job_id": report.get("job_id"),
        "status": status,
        "current_stage": _current_stage_from_report(report),
        "ready": status == "success",
        "clips": [_compact_clip(c) for c in clips] if status == "success" else [],
        "metadata": {
            "clip_count": len(clips) if status == "success" else 0,
            "input_video_name": report.get("input_video_name"),
            "video_duration_sec": report.get("video_duration_sec"),
            "policy_resolution": report.get("policy_resolution") or {},
            "selector": report.get("selector") or {},
            "chunked": report.get("chunked", False),
            "chunking": report.get("chunking"),
            "timings": report.get("stage_timings_ms") or {},
        },
        "artifacts": artifacts,
    }


def _async_config() -> dict[str, object]:
    cfg = load_config()
    worker_cfg = cfg.get("async_worker") if isinstance(cfg.get("async_worker"), dict) else {}
    max_jobs_raw = worker_cfg.get("max_concurrent_jobs", cfg.get("max_concurrent_jobs", 1))
    try:
        max_jobs = int(max_jobs_raw)
    except (TypeError, ValueError):
        max_jobs = 1
    return {
        "enabled": bool(worker_cfg.get("enabled", cfg.get("async_worker_enabled", True))),
        "max_concurrent_jobs": max(1, max_jobs),
        "job_store_type": str(worker_cfg.get("job_store_type", cfg.get("job_store_type", "json")) or "json"),
    }


def _create_queued_report(
    *,
    job_id: str,
    job: dict[str, str],
    video_path: str,
    input_id: str | None,
    input_source: str,
    policy_bundle: dict[str, Any],
) -> dict[str, Any]:
    audit_plain = dict(policy_bundle.get("policy_audit") or {})
    selection_policy = dict(policy_bundle.get("selection") or {})
    funnel_ops_raw = policy_bundle.get("funnel_ops")
    report: dict[str, Any] = {
        "job_id": job_id,
        "input_id": input_id,
        "input_source": input_source,
        "input_video_path": os.path.abspath(video_path),
        "input_video_name": os.path.basename(video_path),
        "video_duration_sec": None,
        "transcript_path": None,
        "selection_path": job["selection_path"],
        "analytics_path": job["analytics_path"],
        "status": "queued",
        "current_stage": "queued",
        "created_at": now_iso(),
        "started_at": None,
        "completed_at": None,
        "errors": [],
        "warnings": [],
        "stage_timings_ms": {},
        "clips": [],
        "policy_resolution": audit_plain,
        "job_store_type": "json",
        "worker": _async_config(),
        "funnel": build_funnel_job_record(
            funnel_ops=funnel_ops_raw if isinstance(funnel_ops_raw, dict) else None,
            resolved_selection=selection_policy,
            policy_audit=audit_plain,
        ),
    }
    write_json(job["report_path"], report)
    write_review(job["review_path"], report)
    maybe_copy(video_path, job["input_copy_path"])
    return report


def _persist_job_task(job: dict[str, str], task: dict[str, Any]) -> None:
    payload = {
        "job_id": task["job_id"],
        "job": task["job"],
        "video_path": task["video_path"],
        "input_id": task.get("input_id"),
        "input_source": task.get("input_source"),
        "input_ledger_state": task.get("input_ledger_state"),
        "policy_bundle": task.get("policy_bundle") or {},
        "created_at": now_iso(),
    }
    write_json(job["task_path"], payload)


def _load_recoverable_tasks() -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for _job_dir, _report_path, report in _iter_job_reports():
        status = str(report.get("status") or "")
        if status not in {"queued", "running"}:
            continue
        task_path = os.path.join(_job_dir, "task.json")
        task = _load_json_object(task_path)
        if not isinstance(task, dict):
            continue
        video_path = os.path.abspath(str(task.get("video_path") or ""))
        if not video_path or not os.path.isfile(video_path):
            errors = report.get("errors") if isinstance(report.get("errors"), list) else []
            errors.append(
                categorize_error(
                    "pipeline",
                    "recovery_input_missing",
                    "Queued job could not be recovered because its input file is missing.",
                    {"video_path": video_path, "task_path": task_path},
                )
            )
            report.update(
                {
                    "status": "failed",
                    "current_stage": "failed",
                    "completed_at": now_iso(),
                    "errors": errors,
                }
            )
            write_json(_report_path, report)
            continue
        task["video_path"] = video_path
        tasks.append(task)
    tasks.sort(key=lambda item: str(item.get("created_at") or ""))
    return tasks


def _recover_pending_jobs_once() -> int:
    global _JOB_RECOVERY_DONE
    if _JOB_RECOVERY_DONE:
        return 0
    _JOB_RECOVERY_DONE = True
    recovered = 0
    for task in _load_recoverable_tasks():
        job = task.get("job")
        if not isinstance(job, dict):
            continue
        _update_job_report(
            job,
            {
                "status": "queued",
                "current_stage": "queued",
                "recovered_at": now_iso(),
            },
        )
        _JOB_QUEUE.put(task)
        recovered += 1
    if recovered:
        _progress(f"Recovered {recovered} queued/running job(s) from disk")
    return recovered


def _update_job_report(job: dict[str, str], updates: dict[str, Any]) -> dict[str, Any]:
    report = _load_json_object(job["report_path"]) or {}
    report.update(updates)
    write_json(job["report_path"], report)
    try:
        write_review(job["review_path"], report)
    except Exception as exc:
        print("[jobs] review update failed:", repr(exc), flush=True)
    return report


def _sync_input_ledger_terminal(input_id: str | None, response_body: dict[str, Any], status_code: int) -> None:
    if not input_id:
        return
    try:
        if 200 <= status_code < 300 and response_body.get("success") is True:
            try:
                completed_record = input_ledger.load_record(input_id)
                meta = completed_record.get("source_metadata")
                if not isinstance(meta, dict):
                    meta = {}
                DuplicateStore().mark_seen(
                    video_id=str(meta.get("video_id") or "") or None,
                    url=str(completed_record.get("source_url") or "") or None,
                )
            except Exception as exc:
                print("[jobs] seen store final success update failed:", repr(exc), flush=True)
            input_ledger.mark_succeeded(
                input_id,
                {
                    "pipeline_job_id": response_body.get("job_id"),
                    "run_id": response_body.get("run_id"),
                    "clip_count": len(response_body.get("clips") or []),
                },
            )
        else:
            input_ledger.mark_failed(
                input_id,
                response_body.get("error") or f"pipeline_http_status:{status_code}",
                {
                    "pipeline_job_id": response_body.get("job_id"),
                    "run_id": response_body.get("run_id"),
                    "status_code": status_code,
                },
            )
    except input_ledger.LedgerError:
        print("[jobs] input ledger terminal update failed", flush=True)


def _execute_job(task: dict[str, Any]) -> None:
    job = task["job"]
    job_id = str(task["job_id"])
    input_id = task.get("input_id")
    video_path = str(task["video_path"])
    _progress(
        f"Worker picked up job — {os.path.basename(video_path)}"
        + (f" (input_id={input_id})" if input_id else ""),
        job_id=job_id,
    )
    try:
        _update_job_report(
            job,
            {
                "status": "running",
                "current_stage": "transcription",
                "started_at": now_iso(),
            },
        )
        if input_id:
            try:
                input_ledger.mark_processing(str(input_id))
            except input_ledger.LedgerError as exc:
                raise RuntimeError(f"Input ledger update failed: {exc}") from exc
        with app.app_context():
            response = _run_pipeline(
                video_path,
                task["policy_bundle"],
                input_id=str(input_id) if input_id else None,
                job_id=job_id,
            )
        status_code = response[1] if isinstance(response, tuple) and len(response) > 1 else 200
        response_obj = response[0] if isinstance(response, tuple) else response
        try:
            body = response_obj.get_json(silent=True) or {}
        except Exception:
            body = {}
        report = _load_json_object(job["report_path"]) or {}
        if isinstance(status_code, int) and status_code >= 400 and report.get("status") not in (
            "success",
            "failed",
        ):
            errors = report.get("errors") if isinstance(report.get("errors"), list) else []
            errors.append(
                categorize_error(
                    "pipeline",
                    "http_error",
                    str(body.get("error") or "Pipeline returned an error response"),
                    {"status_code": status_code},
                )
            )
            report.update(
                {
                    "status": "failed",
                    "current_stage": "failed",
                    "completed_at": now_iso(),
                    "errors": errors,
                }
            )
            write_json(job["report_path"], report)
            write_review(job["review_path"], report)
        _sync_input_ledger_terminal(str(input_id) if input_id else None, body, int(status_code))
        if body.get("success"):
            clip_n = len(body.get("clips") or [])
            _progress(f"Job finished OK — {clip_n} clip(s)", job_id=job_id)
        else:
            _progress(
                f"Job finished with error: {body.get('error') or 'unknown'}",
                job_id=job_id,
            )
    except Exception as exc:
        err = categorize_error("pipeline", "worker_exception", "Job worker failed", repr(exc))
        report = _load_json_object(job["report_path"]) or {}
        errors = report.get("errors") if isinstance(report.get("errors"), list) else []
        errors.append(err)
        report.update(
            {
                "status": "failed",
                "current_stage": "failed",
                "completed_at": now_iso(),
                "errors": errors,
            }
        )
        write_json(job["report_path"], report)
        write_review(job["review_path"], report)
        if input_id:
            try:
                input_ledger.mark_failed(str(input_id), f"pipeline_exception: {exc}")
            except input_ledger.LedgerError:
                print("[jobs] input ledger exception update failed", flush=True)
        _progress(f"Job crashed: {exc}", job_id=job_id)
    finally:
        _JOB_QUEUE.task_done()


def _job_worker_loop() -> None:
    while True:
        task = _JOB_QUEUE.get()
        _execute_job(task)


def _ensure_job_workers_started(*, recover_pending: bool = True) -> None:
    global _JOB_WORKERS_STARTED
    cfg = _async_config()
    if not bool(cfg["enabled"]):
        return
    with _JOB_WORKERS_LOCK:
        desired = int(cfg["max_concurrent_jobs"])
        existing = [t for t in threading.enumerate() if t.name.startswith("mk04-job-worker-")]
        if _JOB_WORKERS_STARTED and len(existing) >= desired:
            return
        for idx in range(len(existing), desired):
            thread = threading.Thread(
                target=_job_worker_loop,
                name=f"mk04-job-worker-{idx + 1}",
                daemon=True,
            )
            thread.start()
        _JOB_WORKERS_STARTED = True
    if recover_pending:
        _recover_pending_jobs_once()


def _enqueue_job(task: dict[str, Any], *, recover_pending: bool = True) -> None:
    _ensure_job_workers_started(recover_pending=recover_pending)
    if not bool(_async_config()["enabled"]):
        raise RuntimeError("Async worker is disabled in configuration")
    job = task.get("job")
    if isinstance(job, dict):
        _persist_job_task(job, task)
    _JOB_QUEUE.put(task)


def _mark_report_failed(report: dict[str, object]) -> None:
    report["status"] = "failed"
    report["current_stage"] = "failed"


def resolve_http_policy_bundle(
    *,
    selection_blob: dict[str, Any] | None,
    pipeline_blob: dict[str, Any] | None,
    pipeline_profile_hint: Any,
    http_funnel_id: str | None = None,
    http_funnel_config: Any = None,
) -> dict[str, Any]:
    """Merge repo config + profiles + funnel + env + HTTP into one auditable bundle."""

    cfg = load_config()
    cfg_abs = resolved_pipeline_config_path()
    pf = (
        pipeline_profile_hint.strip()
        if isinstance(pipeline_profile_hint, str) and pipeline_profile_hint.strip()
        else None
    )
    hf = (
        http_funnel_id.strip()
        if isinstance(http_funnel_id, str) and http_funnel_id.strip()
        else None
    )
    return resolve_run_policy(
        pipeline_config_abs=cfg_abs,
        pipeline_config=cfg,
        pipeline_profile=pf,
        request_pipeline_blob=pipeline_blob,
        request_selection_blob=selection_blob or {},
        http_funnel_id=hf,
        http_funnel_config=http_funnel_config,
    )


def _run_pipeline(
    video_path: str,
    policy_bundle: dict[str, Any],
    *,
    input_id: str | None = None,
    job_id: str | None = None,
):
    config = load_config()
    resolved_paths = ensure_paths(config)
    input_root = resolved_paths["input"]
    temp_root = resolved_paths["temp"]
    output_root = resolved_paths["output"]

    audit_plain = dict(policy_bundle.get("policy_audit") or {})
    selection_policy = dict(policy_bundle["selection"])
    models_eff_mb = dict(policy_bundle.get("models_effective") or {})
    funnel_ops_raw = policy_bundle.get("funnel_ops")
    filename_prefix = ""
    delivery_mode = "pull_from_output_endpoint"
    if isinstance(funnel_ops_raw, dict):
        out_meta = funnel_ops_raw.get("output")
        if isinstance(out_meta, dict):
            fp = out_meta.get("filename_prefix")
            if isinstance(fp, str) and fp.strip():
                filename_prefix = fp.strip()
            dm = out_meta.get("delivery_mode")
            if isinstance(dm, str) and dm.strip():
                delivery_mode = dm.strip()

    transcription_env = dict(os.environ)
    whisper_override = str(models_eff_mb.get("whisper_model") or "").strip()
    if whisper_override:
        transcription_env["WHISPER_MODEL"] = whisper_override
    selection_model_used = str(models_eff_mb.get("selection_model") or "").strip()
    if not selection_model_used:
        selection_model_used = (
            str(config.get("models", {}).get("selection_model", "") or "").strip()
            or "gpt-4o-mini"
        )

    max_clips = int(selection_policy["max_clips"])
    min_duration_sec = float(selection_policy["min_duration_sec"])
    max_duration_sec = float(selection_policy["max_duration_sec"])
    max_overlap_sec = float(selection_policy["max_overlap_sec"])
    include_reasons = bool(selection_policy["include_reasons"])
    include_clip_metadata = bool(selection_policy["include_clip_metadata"])

    if not os.path.exists(video_path):
        return _fail("Input video not found", log_detail=video_path, status_code=400)

    script_transcribe = os.path.join(BASE_DIR, "..", "scripts", "transcribe_video.py")
    script_select = os.path.join(BASE_DIR, "..", "scripts", "select_clip.py")
    script_clip = os.path.join(BASE_DIR, "..", "scripts", "clip_video.py")
    filename = os.path.splitext(os.path.basename(video_path))[0]
    transcript_path = os.path.abspath(os.path.join(temp_root, f"{filename}.json"))

    job = create_job_paths(config, video_path, job_id=job_id)
    warnings: list[dict[str, object]] = []
    stage_ms: dict[str, int] = {}
    created_at = now_iso()
    total_started = time.perf_counter()
    report: dict[str, object] = {
        "job_id": job["job_id"],
        "input_id": input_id,
        "input_video_path": os.path.abspath(video_path),
        "input_video_name": os.path.basename(video_path),
        "video_duration_sec": ffprobe_duration_sec(video_path),
        "transcript_path": None,
        "selection_path": job["selection_path"],
        "analytics_path": job["analytics_path"],
        "status": "running",
        "current_stage": "transcription",
        "created_at": created_at,
        "started_at": created_at,
        "completed_at": None,
        "errors": [],
        "warnings": warnings,
        "stage_timings_ms": stage_ms,
        "clips": [],
        "policy_resolution": audit_plain,
        "funnel": build_funnel_job_record(
            funnel_ops=funnel_ops_raw if isinstance(funnel_ops_raw, dict) else None,
            resolved_selection=selection_policy,
            policy_audit=audit_plain,
        ),
    }

    for notice in audit_plain.get("warnings", []):
        warnings.append(
            categorize_error("configuration", "policy_notice", str(notice), None),
        )

    chunk_scratch_dirs: list[str] = []
    chunk_sidecar_whisper_paths: list[str] = []
    selector_window_paths: list[str] = []

    maybe_copy(video_path, job["input_copy_path"])
    jid = str(job["job_id"])
    dur = report.get("video_duration_sec")
    dur_note = f", {float(dur):.0f}s" if isinstance(dur, (int, float)) else ""
    _progress(f"Starting — {os.path.basename(video_path)}{dur_note}", job_id=jid)

    try:
        if report["video_duration_sec"] is None:
            err = categorize_error(
                "prerequisites",
                "ffprobe_error",
                "Video duration unavailable — pipeline refuses to run clip selection/clipping "
                "without a definitive media length.",
                {"input_video_path": os.path.abspath(video_path)},
            )
            report["errors"] = [err]
            _mark_report_failed(report)
            return _fail(
                "TIMESTAMP_PIPELINE_REJECTED unavailable_video_duration: ffprobe did not "
                "return duration for input; fix the container/codec/path or ffmpeg install.",
                log_detail=os.path.abspath(video_path),
                status_code=422,
            )

        chunk_eff = dict(policy_bundle.get("chunking_effective") or {})
        chunk_cfg = chunk_eff if chunk_eff else None
        vd = report["video_duration_sec"]
        use_chunks = should_use_chunked_transcription(chunk_cfg, vd)

        if not use_chunks:
            _progress("Transcribing (Whisper)…", job_id=jid)
            report["current_stage"] = "transcription"
            t0 = time.perf_counter()
            transcribe = subprocess.run(
                [PYTHON, script_transcribe, video_path],
                capture_output=True,
                text=True,
                env=transcription_env,
            )
            stage_ms["transcription_ms"] = int((time.perf_counter() - t0) * 1000)
            if transcribe.returncode != 0:
                err = categorize_error(
                    "transcription",
                    "transcription_error",
                    "Transcription failed",
                    transcribe.stderr or transcribe.stdout,
                )
                report["errors"] = [err]
                _mark_report_failed(report)
                return _fail("Transcription failed", log_detail=err["details"], status_code=500)

            if not os.path.exists(transcript_path):
                env = _parse_script_envelope(transcribe.stdout)
                if env and isinstance(env.get("transcript_path"), str):
                    transcript_path = str(env["transcript_path"])
                if not os.path.exists(transcript_path):
                    err = categorize_error(
                        "transcription",
                        "file_error",
                        "Transcript not created",
                        transcript_path,
                    )
                    report["errors"] = [err]
                    _mark_report_failed(report)
                    return _fail(
                        "Transcript not created", log_detail=transcript_path, status_code=500
                    )

            maybe_copy(transcript_path, job["transcript_copy_path"])
            report["transcript_path"] = job["transcript_copy_path"]
            transcript_payload = normalize_transcript_payload(transcript_path)
            try:
                require_timed_transcript_payload(transcript_payload)
            except ValueError as exc:
                err = categorize_error(
                    "transcription",
                    "timestamp_contract_error",
                    "Whisper transcript has no timed segments required for selection.",
                    {"path": transcript_path, "detail": str(exc)},
                )
                report["errors"] = [err]
                _mark_report_failed(report)
                return _fail(
                    "Transcript rejected: missing timed Whisper segments.",
                    log_detail=str(exc),
                    status_code=422,
                )
            write_json(job["normalized_transcript_path"], transcript_payload)
            _progress("Transcription done — selecting clips…", job_id=jid)

            t1 = time.perf_counter()
            report["current_stage"] = "selection"
            try:
                candidate_segments, selector_summary = _select_candidates_from_transcript(
                    script_select=script_select,
                    transcript_path=transcript_path,
                    transcript_payload=transcript_payload,
                    temp_root=temp_root,
                    filename=filename,
                    job_id=job["job_id"],
                    max_clips=max_clips,
                    min_duration_sec=min_duration_sec,
                    max_duration_sec=max_duration_sec,
                    max_overlap_sec=max_overlap_sec,
                    include_reasons=include_reasons,
                    include_clip_metadata=include_clip_metadata,
                    selection_model_used=selection_model_used,
                    source_video_duration_sec=float(report["video_duration_sec"]),
                    selector_video_duration_sec=float(report["video_duration_sec"]),
                    selector_window_paths=selector_window_paths,
                    warnings=warnings,
                    context_label="full_transcript",
                    allow_empty_segmented_windows=True,
                )
                if selector_summary.get("segmented"):
                    processed_segments = _aggregate_selector_candidates(
                        candidate_segments,
                        max_clips=max_clips,
                        min_duration_sec=min_duration_sec,
                        max_duration_sec=max_duration_sec,
                        max_overlap_sec=max_overlap_sec,
                        video_duration_sec=report["video_duration_sec"],
                    )
                else:
                    processed_segments = candidate_segments
            except SelectorCallError as exc:
                err = categorize_error(
                    "selection",
                    "selection_error",
                    str(exc),
                    exc.details,
                )
                report["errors"] = [err]
                _mark_report_failed(report)
                return _fail(
                    "Selection failed",
                    log_detail=err["details"],
                    status_code=exc.status_code,
                )
            except ValueError as e:
                err = categorize_error(
                    "selection_validation",
                    "selection_error",
                    "Invalid aggregated selection segments",
                    str(e),
                )
                report["errors"] = [err]
                _mark_report_failed(report)
                return _fail("Invalid selection output", log_detail=str(e), status_code=500)
            stage_ms["selection_ms"] = int((time.perf_counter() - t1) * 1000)
            report["selector"] = selector_summary
            _progress(
                f"Selection done — {len(processed_segments)} segment(s) to clip",
                job_id=jid,
            )

        else:
            report["chunked"] = True
            vdur = float(vd if vd is not None else 0.0)
            chunk_target = float((chunk_cfg or {}).get("chunk_target_sec") or 1200)
            max_per_chunk_cfg = int((chunk_cfg or {}).get("max_clips_per_chunk") or max_clips)
            specs = plan_wallclock_chunks(vdur, chunk_target)
            num_chunks = len(specs)
            if num_chunks < 1:
                err = categorize_error(
                    "transcription",
                    "chunking_error",
                    "Chunk plan empty for long video",
                    {"video_duration_sec": vd},
                )
                report["errors"] = [err]
                _mark_report_failed(report)
                return _fail("Chunking failed", log_detail=err["details"], status_code=500)

            per_chunk_budget = max(
                1, min(max_per_chunk_cfg, (max_clips + num_chunks - 1) // num_chunks)
            )
            chunk_job_dir = os.path.join(temp_root, f"{filename}_{job['job_id']}_chunks")
            os.makedirs(chunk_job_dir, exist_ok=True)
            chunk_scratch_dirs.append(chunk_job_dir)

            report["chunking"] = {
                "threshold_sec": float((chunk_cfg or {}).get("threshold_sec") or 3600),
                "chunk_target_sec": chunk_target,
                "chunk_count": num_chunks,
                "max_clips_per_chunk": per_chunk_budget,
                "specs_sec": [
                    {"start_sec": float(s), "duration_sec": float(d)} for s, d in specs
                ],
            }
            _progress(
                f"Long video — {num_chunks} chunk(s); transcribing each…",
                job_id=jid,
            )
            report["current_stage"] = "transcription"

            zip_paths_offsets: list[tuple[str, float]] = []
            t0 = time.perf_counter()
            for idx, (start_sec, dur_sec) in enumerate(specs):
                _progress(
                    f"Chunk {idx + 1}/{num_chunks}: extract + transcribe…",
                    job_id=jid,
                )
                chunk_vid = os.path.join(
                    chunk_job_dir, f"c_{idx:03d}_{job['job_id']}.mp4"
                )
                try:
                    ffmpeg_extract_segment(video_path, chunk_vid, start_sec, dur_sec)
                except Exception as exc:
                    err = categorize_error(
                        "transcription",
                        "chunking_error",
                        "Failed to extract video chunk",
                        {"index": idx, "error": repr(exc)},
                    )
                    report["errors"] = [err]
                    _mark_report_failed(report)
                    return _fail("Chunk extract failed", log_detail=repr(exc), status_code=500)

                transcribe = subprocess.run(
                    [PYTHON, script_transcribe, chunk_vid],
                    capture_output=True,
                    text=True,
                    env=transcription_env,
                )
                if transcribe.returncode != 0:
                    err = categorize_error(
                        "transcription",
                        "transcription_error",
                        f"Transcription failed for chunk {idx}",
                        transcribe.stderr or transcribe.stdout,
                    )
                    report["errors"] = [err]
                    _mark_report_failed(report)
                    return _fail(
                        "Transcription failed (chunked)",
                        log_detail=err["details"],
                        status_code=500,
                    )

                whisper_path = whisper_json_for_video(chunk_vid, temp_root)
                if not os.path.isfile(whisper_path):
                    env = _parse_script_envelope(transcribe.stdout)
                    if env and isinstance(env.get("transcript_path"), str):
                        whisper_path = str(env["transcript_path"])
                if not os.path.isfile(whisper_path):
                    err = categorize_error(
                        "transcription",
                        "file_error",
                        f"Transcript not created for chunk {idx}",
                        whisper_path,
                    )
                    report["errors"] = [err]
                    _mark_report_failed(report)
                    return _fail(
                        "Transcript not created (chunked)",
                        log_detail=whisper_path,
                        status_code=500,
                    )
                chunk_sidecar_whisper_paths.append(whisper_path)
                zip_paths_offsets.append((whisper_path, float(start_sec)))

            stage_ms["transcription_ms"] = int((time.perf_counter() - t0) * 1000)
            _progress("All chunks transcribed — merging transcript…", job_id=jid)

            merged_path = os.path.abspath(
                os.path.join(temp_root, f"{filename}_{job['job_id']}_merged.json")
            )
            merged_payload = merge_whisper_json_files(zip_paths_offsets, vdur)
            write_merged_whisper_json(merged_path, merged_payload)
            transcript_path = merged_path

            maybe_copy(transcript_path, job["transcript_copy_path"])
            report["transcript_path"] = job["transcript_copy_path"]
            transcript_payload = normalize_transcript_payload(transcript_path)
            try:
                require_timed_transcript_payload(transcript_payload)
            except ValueError as exc:
                err = categorize_error(
                    "transcription",
                    "timestamp_contract_error",
                    "Merged chunked transcript has no timed Whisper segments.",
                    {"path": transcript_path, "detail": str(exc)},
                )
                report["errors"] = [err]
                _mark_report_failed(report)
                return _fail(
                    "Merged transcript rejected: missing timed segments.",
                    log_detail=str(exc),
                    status_code=422,
                )
            write_json(job["normalized_transcript_path"], transcript_payload)
            _progress("Merged transcript ready — selecting clips per chunk…", job_id=jid)

            t1 = time.perf_counter()
            report["current_stage"] = "selection"
            combined_segments: list[dict] = []
            chunk_selector_summaries: list[dict[str, object]] = []
            for idx, (start_sec, dur_sec) in enumerate(specs):
                _progress(
                    f"Chunk {idx + 1}/{num_chunks}: selecting clips…",
                    job_id=jid,
                )
                whisper_path_chunk = zip_paths_offsets[idx][0]
                chunk_vid = os.path.join(
                    chunk_job_dir, f"c_{idx:03d}_{job['job_id']}.mp4"
                )
                chunk_dur = ffprobe_duration_sec(chunk_vid)
                if chunk_dur is None or chunk_dur <= 0:
                    err = categorize_error(
                        "transcription",
                        "ffprobe_error",
                        f"Chunk {idx}: could not read a positive duration for extracted slice.",
                        {"chunk_video_path": chunk_vid},
                    )
                    report["errors"] = [err]
                    _mark_report_failed(report)
                    return _fail(
                        "TIMESTAMP_PIPELINE_REJECTED unavailable_chunk_duration: ffprobe "
                        "did not return duration for extracted chunk.",
                        log_detail=str(chunk_vid),
                        status_code=422,
                    )
                try:
                    slice_payload = normalize_transcript_payload(whisper_path_chunk)
                    require_timed_transcript_payload(slice_payload)
                except ValueError as exc:
                    err = categorize_error(
                        "transcription",
                        "timestamp_contract_error",
                        f"Chunk {idx} Whisper JSON rejected: no timed segments.",
                        {"path": whisper_path_chunk, "detail": str(exc)},
                    )
                    report["errors"] = [err]
                    _mark_report_failed(report)
                    return _fail(
                        "Chunk transcript rejected before selection.",
                        log_detail=str(exc),
                        status_code=422,
                    )
                try:
                    part, selector_summary = _select_candidates_from_transcript(
                        script_select=script_select,
                        transcript_path=whisper_path_chunk,
                        transcript_payload=slice_payload,
                        temp_root=temp_root,
                        filename=f"{filename}_c{idx:03d}",
                        job_id=job["job_id"],
                        max_clips=per_chunk_budget,
                        min_duration_sec=min_duration_sec,
                        max_duration_sec=max_duration_sec,
                        max_overlap_sec=max_overlap_sec,
                        include_reasons=include_reasons,
                        include_clip_metadata=include_clip_metadata,
                        selection_model_used=selection_model_used,
                        source_video_duration_sec=float(report["video_duration_sec"]),
                        selector_video_duration_sec=float(chunk_dur),
                        base_timeline_offset_sec=float(start_sec),
                        selector_window_paths=selector_window_paths,
                        warnings=warnings,
                        context_label=f"video_chunk_{idx}",
                        allow_empty_segmented_windows=True,
                    )
                except SelectorCallError as exc:
                    err = categorize_error(
                        "selection",
                        "selection_error",
                        f"Selection failed for chunk {idx}",
                        exc.details,
                    )
                    report["errors"] = [err]
                    _mark_report_failed(report)
                    return _fail(
                        "Selection failed (chunked)",
                        log_detail=err["details"],
                        status_code=exc.status_code,
                    )
                combined_segments.extend(part)
                chunk_selector_summaries.append(selector_summary)

            stage_ms["selection_ms"] = int((time.perf_counter() - t1) * 1000)
            report["selector"] = {
                "segmented": True,
                "mode": "video_chunks",
                "chunk_count": len(chunk_selector_summaries),
                "selector_call_count": sum(
                    int(s.get("selector_call_count") or 0) for s in chunk_selector_summaries
                ),
                "candidate_count": len(combined_segments),
                "prompt_truncation_count": sum(
                    int(s.get("prompt_truncation_count") or 0) for s in chunk_selector_summaries
                ),
                "chunks": chunk_selector_summaries,
            }

            try:
                processed_segments = _aggregate_selector_candidates(
                    combined_segments,
                    max_clips=max_clips,
                    min_duration_sec=min_duration_sec,
                    max_duration_sec=max_duration_sec,
                    max_overlap_sec=max_overlap_sec,
                    video_duration_sec=report["video_duration_sec"],
                )
            except ValueError as e:
                err = categorize_error(
                    "selection_validation",
                    "selection_error",
                    "Invalid combined selection segments",
                    str(e),
                )
                report["errors"] = [err]
                _mark_report_failed(report)
                return _fail("Invalid selection output", log_detail=str(e), status_code=500)
            _progress(
                f"Selection done — {len(processed_segments)} segment(s) to clip",
                job_id=jid,
            )

        validated_segments, validation_issues = validate_and_repair_selection(
            processed_segments,
            transcript_payload=transcript_payload,
            video_duration_sec=report["video_duration_sec"],
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
        )
        warnings.extend(validation_issues)
        if not validated_segments:
            err = categorize_error(
                "selection_validation",
                "timestamp_error",
                "No valid clips selected after validation",
                validation_issues,
            )
            report["errors"] = [err]
            _mark_report_failed(report)
            return _fail("No valid clips selected after timestamp validation", status_code=422)

        write_json(
            job["selection_path"],
            {
                "clips": validated_segments,
                "validation_warnings": validation_issues,
            },
        )

        clip_total = len(validated_segments)
        _progress(f"Clipping {clip_total} segment(s) with FFmpeg…", job_id=jid)
        report["current_stage"] = "clipping"
        t2 = time.perf_counter()
        clips: list[dict[str, object]] = []
        for index, segment in enumerate(validated_segments, start=1):
            start = str(segment["start"]).strip()
            end = str(segment["end"]).strip()
            _progress(f"Clip {index}/{clip_total} ({start}–{end})", job_id=jid)
            if filename_prefix:
                clip_name = f"{filename_prefix}_clip_{index:02d}_{uuid.uuid4().hex[:8]}.mp4"
            else:
                clip_name = f"{filename}_clip_{index:02d}_{uuid.uuid4().hex[:8]}.mp4"
            clip_path = os.path.join(output_root, clip_name)
            clip = subprocess.run(
                [PYTHON, script_clip, video_path, start, end, clip_path],
                capture_output=True,
                text=True,
            )
            if clip.returncode != 0:
                err = categorize_error(
                    "clipping",
                    "clipping_error",
                    "Clipping failed",
                    clip.stderr or clip.stdout,
                )
                report["errors"] = [err]
                _mark_report_failed(report)
                return _fail("Clipping failed", log_detail=err["details"], status_code=500)

            envelope = _parse_script_envelope(clip.stdout)
            if envelope is None:
                err = categorize_error(
                    "clipping",
                    "clipping_error",
                    "Clipping subprocess did not emit a parseable JSON result envelope.",
                    clip.stderr or clip.stdout,
                )
                report["errors"] = [err]
                _mark_report_failed(report)
                return _fail("Clipping failed: missing result envelope", log_detail=str(err["details"]), status_code=500)
            if envelope.get("ok") is not True or str(envelope.get("script", "")).strip() != "clip_video":
                err = categorize_error(
                    "clipping",
                    "clipping_error",
                    "Clipping result envelope was not successful for clip_video.",
                    {"envelope": envelope, "stderr": clip.stderr or clip.stdout},
                )
                report["errors"] = [err]
                _mark_report_failed(report)
                return _fail("Clipping failed: invalid script envelope", log_detail=str(envelope), status_code=500)

            clip_validation = envelope.get("clip_validation")
            if not isinstance(clip_validation, dict) or clip_validation.get("ok") is not True:
                err = categorize_error(
                    "clipping",
                    "clipping_validation_error",
                    "Clipping subprocess did not return a usable clip_validation report.",
                    {"envelope": envelope, "stderr": clip.stderr or clip.stdout},
                )
                report["errors"] = [err]
                _mark_report_failed(report)
                return _fail(
                    "Clipping failed: output validation report missing",
                    log_detail=str(clip.stderr or ""),
                    status_code=500,
                )

            raw_out_path = envelope.get("output_path")
            if isinstance(raw_out_path, str) and raw_out_path.strip():
                resolved_path = os.path.abspath(raw_out_path.strip())
            else:
                resolved_path = os.path.abspath(clip_path)

            if not os.path.isfile(resolved_path) or os.path.getsize(resolved_path) < 256:
                err = categorize_error(
                    "clipping",
                    "clipping_error",
                    "Clipping produced no usable output file after validation envelope.",
                    {"path": resolved_path, "exists": os.path.isfile(resolved_path)},
                )
                report["errors"] = [err]
                _mark_report_failed(report)
                return _fail("Clipping failed: output file missing or too small", log_detail=str(resolved_path), status_code=500)

            job_clip_path = os.path.join(job["clips_dir"], os.path.basename(resolved_path))
            maybe_copy(resolved_path, job_clip_path)

            clip_payload: dict[str, object] = {
                "clip_id": f"{job['job_id']}_clip_{index:02d}",
                "start": start,
                "end": end,
                "clip_path": resolved_path,
                "job_clip_path": job_clip_path,
                "clip_file": os.path.basename(resolved_path),
                "clip_url": f"/output/{os.path.basename(resolved_path)}",
                "duration_sec": segment.get("duration_sec"),
                "clip_validation": clip_validation,
            }
            if include_clip_metadata:
                for key in ("title", "hook", "caption", "scores", "composite_score", "reason"):
                    if segment.get(key) is not None:
                        clip_payload[key] = segment[key]
            elif include_reasons and segment.get("reason"):
                clip_payload["reason"] = segment["reason"]
            clips.append(clip_payload)
        stage_ms["clipping_ms"] = int((time.perf_counter() - t2) * 1000)

        report["clips"] = clips
        report["status"] = "success"
        report["current_stage"] = "success"
        report["completed_at"] = now_iso()
        stage_ms["total_ms"] = int((time.perf_counter() - total_started) * 1000)
        write_json(job["report_path"], report)
        write_review(job["review_path"], report)
        _progress(f"Done — {len(clips)} clip(s) ready", job_id=jid)
        handoff = _try_output_funnel_handoff(report, report_path=job["report_path"])
        report["output_funnel_handoff"] = handoff
        write_json(job["report_path"], report)
        if handoff.get("ok") is True:
            _progress("Output funnel handoff registered", job_id=jid)
            for line in _output_funnel_schedule_lines(handoff):
                _progress(f"Output funnel scheduled — {line}", job_id=jid)
        elif handoff.get("enabled") is True:
            warnings.append(
                categorize_error(
                    "output_funnel",
                    "handoff_unavailable",
                    "Output funnel handoff failed; clips remain available in video-automation outputs.",
                    handoff,
                )
            )
            report["warnings"] = warnings
            write_json(job["report_path"], report)
            _progress("Output funnel unavailable; clips remain ready locally", job_id=jid)

        run_id = uuid.uuid4().hex
        funnel_record = report.get("funnel")
        response: dict[str, object] = {
            "success": True,
            "pipeline": PIPELINE_NAME,
            "run_id": run_id,
            "input_id": input_id,
            "source_video": os.path.basename(video_path),
            "video_basename": filename,
            "clips": clips,
            "delivery_mode": delivery_mode,
            "job_id": job["job_id"],
            "job_dir": job["job_dir"],
            "report_path": job["report_path"],
            "review_path": job["review_path"],
            "analytics_path": job["analytics_path"],
            "transcript_payload_path": job["normalized_transcript_path"],
            "selection_path": job["selection_path"],
            "policy_resolution": audit_plain,
            **_inspection_urls(job["job_id"]),
        }
        if isinstance(funnel_record, dict) and funnel_record.get("funnel_id"):
            response["funnel"] = funnel_record
            response["funnel_id"] = funnel_record.get("funnel_id")
            response["funnel_name"] = funnel_record.get("funnel_name")
            response["enabled_platforms"] = funnel_record.get("enabled_platforms") or []
            response["funnel_policy_summary"] = funnel_record.get("funnel_policy_summary") or {}
        else:
            response["funnel"] = None
            response["funnel_id"] = None
            response["funnel_name"] = None
            response["enabled_platforms"] = []
            response["funnel_policy_summary"] = {}
        if report.get("chunked"):
            response["chunked"] = True
            if report.get("chunking"):
                response["chunking"] = report["chunking"]
        if len(clips) == 1:
            c0 = clips[0]
            response["start"] = c0["start"]
            response["end"] = c0["end"]
            response["clip_path"] = c0["clip_path"]
            for key in ("title", "hook", "caption", "reason", "scores", "composite_score"):
                if key in c0:
                    response[key] = c0[key]
        return jsonify(response)
    finally:
        try:
            report["funnel"] = build_funnel_job_record(
                funnel_ops=funnel_ops_raw if isinstance(funnel_ops_raw, dict) else None,
                resolved_selection=selection_policy,
                policy_audit=dict(policy_bundle.get("policy_audit") or {}),
            )
            if report.get("completed_at") is None:
                report["completed_at"] = now_iso()
                if report.get("status") in ("running", "queued"):
                    _mark_report_failed(report)
                    report["errors"] = report.get("errors") or [
                        categorize_error(
                            "pipeline",
                            "api_error",
                            "Pipeline ended before success response",
                            None,
                        )
                    ]
                stage_ms["total_ms"] = int((time.perf_counter() - total_started) * 1000)
            write_json(job["report_path"], report)
            write_review(job["review_path"], report)
            try:
                analytics_files = persist_run_analytics(
                    report=report,
                    analytics_root=resolved_paths["analytics"],
                    job_analytics_path=job["analytics_path"],
                )
                report["analytics"] = analytics_files
                write_json(job["report_path"], report)
            except Exception as exc:
                warnings.append(
                    categorize_error(
                        "analytics",
                        "analytics_write_error",
                        "Failed to persist analytics snapshot/events.",
                        repr(exc),
                    )
                )
                write_json(job["report_path"], report)
        except Exception as exc:
            print(
                "[pipeline_finalize] Failed to finalize report/analytics:",
                repr(exc),
                flush=True,
            )
        temp_policy = effective_temp_cleanup_policy(config)
        failed_run = report.get("status") == "failed"
        skip_intermediate_cleanup = temp_policy == "debug_retain_all" or (
            temp_policy == "retain_on_failure" and failed_run
        )
        report["artifact_retention"] = {
            "temp_policy": temp_policy,
            "skipped_intermediate_cleanup": skip_intermediate_cleanup,
        }
        try:
            write_json(job["report_path"], report)
        except Exception as exc:
            print(
                "[pipeline_finalize] Failed to persist artifact_retention metadata:",
                repr(exc),
                flush=True,
            )

        if not skip_intermediate_cleanup:
            for maybe_path, root in ((video_path, input_root), (transcript_path, temp_root)):
                try:
                    abs_path = os.path.abspath(maybe_path)
                    if abs_path.startswith(root + os.sep) and os.path.isfile(abs_path):
                        os.remove(abs_path)
                except Exception as exc:
                    print(
                        "[pipeline_cleanup] Removing temp/input artifact failed:",
                        maybe_path,
                        repr(exc),
                        flush=True,
                    )
            for dpath in chunk_scratch_dirs:
                try:
                    if os.path.isdir(dpath):
                        shutil.rmtree(dpath, ignore_errors=True)
                except Exception as exc:
                    print(
                        "[pipeline_cleanup] Removing chunk scratch dir failed:",
                        dpath,
                        repr(exc),
                        flush=True,
                    )
            for wpath in chunk_sidecar_whisper_paths:
                try:
                    abs_w = os.path.abspath(wpath)
                    if abs_w.startswith(temp_root + os.sep) and os.path.isfile(abs_w):
                        os.remove(abs_w)
                except Exception as exc:
                    print(
                        "[pipeline_cleanup] Removing chunk whisper sidecar failed:",
                        wpath,
                        repr(exc),
                        flush=True,
                    )


@app.route("/output/<path:clip_file>", methods=["GET"])
def get_output_clip(clip_file: str):
    try:
        resolved_name, resolved_path = _resolve_output_clip_path(clip_file)
        if not os.path.isfile(resolved_path):
            return _fail("Clip not found", log_detail=resolved_path, status_code=404)
        output_root = ensure_paths(load_config())["output"]
        return send_from_directory(output_root, resolved_name, as_attachment=True)
    except ValueError as e:
        return _fail(str(e), status_code=400)
    except Exception as e:
        print("[output] unexpected error:", repr(e), flush=True)
        return _fail("Output fetch failed", log_detail=repr(e), status_code=500)


@app.route("/jobs", methods=["GET"])
def list_jobs():
    try:
        limit = _parse_jobs_limit(request.args.get("limit"))
    except ValueError as exc:
        return _fail("Invalid limit", log_detail=str(exc), status_code=400)
    try:
        records = sorted(_iter_job_reports(), key=_job_sort_key, reverse=True)
        jobs = [_job_summary(report, job_dir) for job_dir, _, report in records[:limit]]
        return jsonify(
            {
                "success": True,
                "pipeline": PIPELINE_NAME,
                "jobs": jobs,
                "count": len(jobs),
                "limit": limit,
            }
        )
    except Exception as exc:
        print("[jobs] list error:", repr(exc), flush=True)
        return _fail("Job listing failed", log_detail=repr(exc), status_code=500)


@app.route("/jobs", methods=["POST"])
def create_job():
    try:
        return _create_job_from_payload(_request_payload())
    except Exception as exc:
        print("[jobs] create error:", repr(exc), flush=True)
        return _fail("Job creation failed", log_detail=repr(exc), status_code=500)


@app.route("/jobs/<job_id>", methods=["GET"])
def get_job_report(job_id: str):
    match = _find_job_report(job_id)
    if match == "invalid":
        return _fail("Invalid job_id", status_code=400)
    if match == "ambiguous":
        return _fail("Ambiguous job_id", status_code=409)
    if match is None:
        return _fail("Job not found", status_code=404)
    job_dir, _, report = match
    return jsonify(_job_status_payload(job_dir, report))


@app.route("/jobs/<job_id>/outputs", methods=["GET"])
def get_job_outputs(job_id: str):
    match = _find_job_report(job_id)
    if match == "invalid":
        return _fail("Invalid job_id", status_code=400)
    if match == "ambiguous":
        return _fail("Ambiguous job_id", status_code=409)
    if match is None:
        return _fail("Job not found", status_code=404)
    job_dir, _, report = match
    payload = _job_outputs_payload(job_dir, report)
    if payload["ready"] is not True:
        status = str(payload.get("status") or "")
        code = 409 if status in ("queued", "running") else 422
        return jsonify(payload), code
    return jsonify(payload)


@app.route("/jobs/<job_id>/debug", methods=["GET"])
def get_job_debug(job_id: str):
    match = _find_job_report(job_id)
    if match == "invalid":
        return _fail("Invalid job_id", status_code=400)
    if match == "ambiguous":
        return _fail("Ambiguous job_id", status_code=409)
    if match is None:
        return _fail("Job not found", status_code=404)
    job_dir, _, report = match
    return jsonify(_job_debug_summary(job_dir, report))


def _process_pipeline_json_payload(payload: dict[str, Any], *, route_label: str):
    """Handle a parsed JSON body for ``/process`` and ``/process-inline`` (video must exist under input/)."""
    raw_input_id = payload.get("input_id") or payload.get("job_id")
    input_id = str(raw_input_id).strip() if raw_input_id is not None else ""
    raw_video = payload.get("video")
    raw_path = payload.get("video_path")
    video_arg: str | None = None
    video_from_path = False
    ledger_record: dict[str, Any] | None = None
    if input_id:
        try:
            ledger_record = input_ledger.load_record(input_id)
            ledger_path = input_ledger.resolve_file_path(input_id)
        except input_ledger.LedgerError as exc:
            _pipeline_diagnostic_log(
                "H4",
                f"app.py:{route_label}",
                "fail input ledger lookup",
                {"input_id": input_id, "error": str(exc)[:500]},
            )
            return _fail("Input ledger lookup failed", log_detail=str(exc), status_code=400)
        video_arg = str(ledger_path)
        _pipeline_diagnostic_log(
            "H4",
            f"app.py:{route_label}",
            "resolved video from input ledger",
            {
                "input_id": input_id,
                "ledger_state": ledger_record.get("state"),
                "video_path": str(ledger_path),
            },
        )
    elif raw_video is not None and str(raw_video).strip():
        video_arg = str(raw_video).strip()
    elif raw_path is not None and str(raw_path).strip():
        video_arg = os.path.basename(str(raw_path).strip().rstrip("/"))
        video_from_path = True
        _pipeline_diagnostic_log(
            "H4",
            f"app.py:{route_label}",
            "derived video basename from video_path",
            {
                "video_path_prefix": str(raw_path)[:280],
                "video_arg": video_arg,
            },
        )
    if not video_arg:
        _pipeline_diagnostic_log(
            "H4",
            f"app.py:{route_label}",
            "fail missing input_id, video and video_path",
            {"payload_keys": sorted(payload.keys())},
        )
        return _fail(
            "Missing usable 'input_id', 'video' or 'video_path' in the JSON body (after unwrapping common automation keys: "
            "json, body, data, item). Prefer sending run-funnel's input_id, e.g. use a JSON body with "
            "\"input_id\": \"={{ $json.input_id }}\" on the same item that received /run-funnel output, "
            "or merge that field into the object next to \"selection\".",
            status_code=400,
        )
    selection_policy = payload.get("selection", {}) or {}
    try:
        _cfg = load_config()
        _input_root_dbg = ensure_paths(_cfg)["input"]
    except Exception as _e:
        _input_root_dbg = f"<ensure_paths_error:{_e}>"
    _pipeline_diagnostic_log(
        "H4",
        f"app.py:{route_label}",
        "json parsed",
        {
            "video_arg": str(video_arg)[:500],
            "video_from_video_path": video_from_path,
            "input_id": input_id or None,
            "input_ledger_state": (ledger_record or {}).get("state"),
            "payload_keys": sorted(payload.keys()),
            "payload_empty": payload == {},
            "input_root_resolved": _input_root_dbg,
        },
    )
    if not isinstance(selection_policy, dict):
        _pipeline_diagnostic_log(
            "H3",
            f"app.py:{route_label}",
            "fail invalid selection type",
            {"detail": repr(selection_policy)[:300]},
        )
        return _fail(
            "Invalid selection policy",
            log_detail=repr(selection_policy),
            status_code=400,
        )

    pipe_raw = payload.get("pipeline")
    if pipe_raw is None:
        pipe_blob: dict[str, Any] = {}
    elif isinstance(pipe_raw, dict):
        pipe_blob = pipe_raw
    else:
        _pipeline_diagnostic_log(
            "H3",
            f"app.py:{route_label}",
            "fail pipeline not object",
            {"pipe_type": type(pipe_raw).__name__},
        )
        return _fail("`pipeline` must be a JSON object when provided", status_code=400)

    pp = payload.get("pipeline_profile")
    prof_hint = pp.strip() if isinstance(pp, str) and pp.strip() else None
    if prof_hint is None:
        fid_catalog = payload.get("funnel_id")
        if isinstance(fid_catalog, str) and fid_catalog.strip():
            prof_hint = fid_catalog.strip()
    if prof_hint is None:
        fc_hint = payload.get("funnel_config")
        if fc_hint is not None:
            try:
                prof_hint = sanitize_funnel_config_basename(fc_hint)
            except ValueError:
                prof_hint = None

    fid_body = payload.get("funnel_id")
    http_funnel_id = fid_body.strip() if isinstance(fid_body, str) and fid_body.strip() else None
    fc_body = payload.get("funnel_config")

    try:
        bundle = resolve_http_policy_bundle(
            selection_blob=selection_policy,
            pipeline_blob=pipe_blob,
            pipeline_profile_hint=prof_hint,
            http_funnel_id=http_funnel_id,
            http_funnel_config=fc_body,
        )
    except ValueError as exc:
        _pipeline_diagnostic_log(
            "H3",
            f"app.py:{route_label}",
            "fail policy bundle",
            {"error": str(exc)[:500]},
        )
        return _fail("Invalid pipeline policy resolution", log_detail=str(exc), status_code=400)

    if input_id:
        video_path = os.path.abspath(str(video_arg))
    else:
        _, video_path = _resolve_input_video_path(str(video_arg))
    _pipeline_diagnostic_log(
        "H2",
        f"app.py:{route_label}",
        "video path resolved",
        {
            "video_arg": str(video_arg)[:500],
            "video_path": video_path,
            "isfile": os.path.isfile(video_path),
        },
    )
    if not os.path.isfile(video_path):
        _pipeline_diagnostic_log(
            "H2",
            f"app.py:{route_label}",
            "fail input not found",
            {"video_path": video_path},
        )
        if input_id:
            try:
                input_ledger.mark_failed(input_id, f"input_video_not_found: {video_path}")
            except input_ledger.LedgerError:
                print("[process] input ledger missing-file update failed", flush=True)
        return _fail(
            "Input video not found for /process. Copy or move the source video into the configured input folder, then send its basename as `video`.",
            log_detail=video_path,
            status_code=400,
        )
    _pipeline_diagnostic_log(
        "H5",
        f"app.py:{route_label}",
        "starting _run_pipeline",
        {"video_path": video_path, "input_id": input_id or None},
    )
    if input_id:
        try:
            input_ledger.mark_processing(input_id)
        except input_ledger.LedgerError as exc:
            _pipeline_diagnostic_log(
                "H5",
                f"app.py:{route_label}",
                "fail mark input processing",
                {"input_id": input_id, "error": str(exc)[:500]},
            )
            return _fail("Input ledger update failed", log_detail=str(exc), status_code=500)
    try:
        response = _run_pipeline(video_path, bundle, input_id=input_id or None)
    except Exception as exc:
        if input_id:
            try:
                input_ledger.mark_failed(input_id, f"pipeline_exception: {exc}")
            except input_ledger.LedgerError:
                print("[process] input ledger exception update failed", flush=True)
        raise
    if input_id:
        try:
            status_code = response[1] if isinstance(response, tuple) and len(response) > 1 else 200
            response_obj = response[0] if isinstance(response, tuple) else response
            body: dict[str, Any] = {}
            try:
                body = response_obj.get_json(silent=True) or {}
            except Exception:
                body = {}
            if isinstance(status_code, int) and 200 <= status_code < 300 and body.get("success") is True:
                try:
                    completed_record = input_ledger.load_record(input_id)
                    meta = completed_record.get("source_metadata")
                    if not isinstance(meta, dict):
                        meta = {}
                    DuplicateStore().mark_seen(
                        video_id=str(meta.get("video_id") or "") or None,
                        url=str(completed_record.get("source_url") or "") or None,
                    )
                except Exception as exc:
                    print(
                        "[process] seen store final success update failed:",
                        repr(exc),
                        flush=True,
                    )
                input_ledger.mark_succeeded(
                    input_id,
                    {
                        "pipeline_job_id": body.get("job_id"),
                        "run_id": body.get("run_id"),
                        "clip_count": len(body.get("clips") or []),
                    },
                )
            else:
                input_ledger.mark_failed(
                    input_id,
                    body.get("error") or f"pipeline_http_status:{status_code}",
                    {
                        "pipeline_job_id": body.get("job_id"),
                        "run_id": body.get("run_id"),
                        "status_code": status_code,
                    },
                )
        except input_ledger.LedgerError:
            print("[process] input ledger terminal update failed", flush=True)
    return response


def _create_job_from_payload(payload: dict[str, Any], *, compatibility_route: str | None = None):
    payload = _lift_adapter_wrapped_video_fields(payload)
    try:
        input_id, video_path, input_source, ledger_record = _resolve_job_video_input(payload)
    except input_ledger.LedgerError as exc:
        return _fail("Input ledger lookup failed", log_detail=str(exc), status_code=400)
    except ValueError as exc:
        return _fail(str(exc), status_code=400)
    except Exception as exc:
        print("[jobs] input resolution failed:", repr(exc), flush=True)
        return _fail("Input video resolution failed", log_detail=repr(exc), status_code=500)

    if not os.path.isfile(video_path):
        job_id = _new_job_id()
        config = load_config()
        job = create_job_paths(config, video_path, job_id=job_id)
        err = categorize_error(
            "prerequisites",
            "input_video_not_found",
            "Input video not found",
            {"input_video_path": os.path.abspath(video_path), "input_source": input_source},
        )
        report = {
            "job_id": job_id,
            "input_id": input_id,
            "input_source": input_source,
            "input_video_path": os.path.abspath(video_path),
            "input_video_name": os.path.basename(video_path),
            "status": "failed",
            "current_stage": "failed",
            "created_at": now_iso(),
            "started_at": None,
            "completed_at": now_iso(),
            "errors": [err],
            "warnings": [],
            "stage_timings_ms": {},
            "clips": [],
            "job_store_type": "json",
        }
        write_json(job["report_path"], report)
        write_review(job["review_path"], report)
        if input_id:
            try:
                input_ledger.mark_failed(input_id, f"input_video_not_found: {video_path}")
            except input_ledger.LedgerError:
                print("[jobs] input ledger missing-file update failed", flush=True)
        body = _job_status_payload(job["job_dir"], report)
        body["success"] = False
        body["error"] = "Input video not found"
        return jsonify(body), 400

    selection_policy = payload.get("selection", {}) or {}
    if not isinstance(selection_policy, dict):
        return _fail("Invalid selection policy", log_detail=repr(selection_policy), status_code=400)

    pipe_raw = payload.get("pipeline")
    if pipe_raw is None:
        pipe_blob: dict[str, Any] = {}
    elif isinstance(pipe_raw, dict):
        pipe_blob = pipe_raw
    else:
        return _fail("`pipeline` must be a JSON object when provided", status_code=400)

    pp = payload.get("pipeline_profile")
    prof_hint = pp.strip() if isinstance(pp, str) and pp.strip() else None
    if prof_hint is None:
        fid_catalog = payload.get("funnel_id")
        if isinstance(fid_catalog, str) and fid_catalog.strip():
            prof_hint = fid_catalog.strip()
    if prof_hint is None:
        fc_hint = payload.get("funnel_config")
        if fc_hint is not None:
            try:
                prof_hint = sanitize_funnel_config_basename(fc_hint)
            except ValueError:
                prof_hint = None

    fid_body = payload.get("funnel_id")
    http_funnel_id = fid_body.strip() if isinstance(fid_body, str) and fid_body.strip() else None
    fc_body = payload.get("funnel_config")

    try:
        bundle = resolve_http_policy_bundle(
            selection_blob=selection_policy,
            pipeline_blob=pipe_blob,
            pipeline_profile_hint=prof_hint,
            http_funnel_id=http_funnel_id,
            http_funnel_config=fc_body,
        )
    except ValueError as exc:
        return _fail("Invalid pipeline policy resolution", log_detail=str(exc), status_code=400)

    job_id = _new_job_id()
    config = load_config()
    job = create_job_paths(config, video_path, job_id=job_id)
    report = _create_queued_report(
        job_id=job_id,
        job=job,
        video_path=video_path,
        input_id=input_id,
        input_source=input_source,
        policy_bundle=bundle,
    )
    if compatibility_route:
        report["compatibility_route"] = compatibility_route
        report["deprecated_endpoint"] = compatibility_route
        write_json(job["report_path"], report)
        write_review(job["review_path"], report)

    task = {
        "job_id": job_id,
        "job": job,
        "video_path": os.path.abspath(video_path),
        "input_id": input_id,
        "input_source": input_source,
        "input_ledger_state": (ledger_record or {}).get("state"),
        "policy_bundle": bundle,
    }
    try:
        _persist_job_task(job, task)
        _ensure_job_workers_started(recover_pending=False)
        _enqueue_job(task)
        _progress(
            f"Queued — {os.path.basename(video_path)} (poll GET /jobs/{job_id})",
            job_id=job_id,
        )
    except Exception as exc:
        err = categorize_error("pipeline", "queue_error", "Failed to enqueue job", repr(exc))
        errors = report.get("errors") if isinstance(report.get("errors"), list) else []
        errors.append(err)
        report.update(
            {
                "status": "failed",
                "current_stage": "failed",
                "completed_at": now_iso(),
                "errors": errors,
            }
        )
        write_json(job["report_path"], report)
        write_review(job["review_path"], report)
        return _fail("Failed to enqueue job", log_detail=repr(exc), status_code=500)

    body = {
        "success": True,
        "pipeline": PIPELINE_NAME,
        "job_id": job_id,
        "status": "queued",
        "current_stage": "queued",
        "status_url": f"/jobs/{job_id}",
        "outputs_url": f"/jobs/{job_id}/outputs",
        "job_url": f"/jobs/{job_id}",
        "input_id": input_id,
        "source_video": os.path.basename(video_path),
    }
    if compatibility_route:
        body["deprecated"] = True
        body["deprecated_endpoint"] = compatibility_route
        body["message"] = f"{compatibility_route} is deprecated; use POST /jobs and poll status_url."
    return jsonify(body), 202


@app.route("/process", methods=["POST"])
def process():
    # #region agent log
    try:
        _raw_len = request.content_length
    except Exception:
        _raw_len = None
    _pipeline_diagnostic_log(
        "H1",
        "app.py:process",
        "request received",
        {
            "content_type": request.content_type or "",
            "content_length": _raw_len,
            "remote_addr": getattr(request, "remote_addr", None),
        },
    )
    # #endregion
    try:
        raw_preview = (request.get_data(cache=True, as_text=True) or "")[:400]
        # #region agent log
        _pipeline_diagnostic_log(
            "H6",
            "app.py:process",
            "raw json body prefix",
            {"raw_prefix": raw_preview},
        )
        # #endregion
        payload = request.get_json(silent=True) or {}
        keys_before = sorted(payload.keys())
        payload = _lift_adapter_wrapped_video_fields(payload)
        keys_after = sorted(payload.keys())
        if keys_before != keys_after:
            # #region agent log
            _pipeline_diagnostic_log(
                "H6",
                "app.py:process",
                "lifted fields from nested automation wrapper",
                {"keys_before": keys_before, "keys_after": keys_after},
            )
            # #endregion
        return _create_job_from_payload(payload, compatibility_route="/process")

    except Exception as e:
        print("[process] unexpected error:", repr(e), flush=True)
        # #region agent log
        _pipeline_diagnostic_log(
            "H5",
            "app.py:process",
            "exception",
            {"error": repr(e)[:800]},
        )
        # #endregion
        return _fail("Processing failed", log_detail=repr(e), status_code=500)


@app.route("/process-inline", methods=["POST"])
def process_inline():
    """Deprecated compatibility wrapper around ``POST /jobs``."""
    try:
        payload = request.get_json(silent=True) or {}
        payload = _lift_adapter_wrapped_video_fields(payload)
        return _create_job_from_payload(payload, compatibility_route="/process-inline")
    except Exception as e:
        print("[process-inline] unexpected error:", repr(e), flush=True)
        return _fail("Processing failed", log_detail=repr(e), status_code=500)


@app.route("/upload", methods=["POST"])
def upload():
    """Optional upload helper; ``POST /jobs`` can upload and enqueue directly."""
    try:
        file_storage = request.files.get("video_file") or request.files.get("file")
        if file_storage is None or not getattr(file_storage, "filename", ""):
            return _fail("Missing multipart video_file", status_code=400)
        filename, video_path = _save_uploaded_video_file(file_storage)
        return jsonify(
            {
                "success": True,
                "pipeline": PIPELINE_NAME,
                "video": filename,
                "video_path": video_path,
                "deprecated": True,
                "message": "POST /jobs accepts video_file directly; /upload is optional.",
            }
        )
    except ValueError as exc:
        return _fail(str(exc), status_code=400)
    except Exception as exc:
        print("[upload] unexpected error:", repr(exc), flush=True)
        return _fail("Upload failed", log_detail=repr(exc), status_code=500)


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"ok": True, "pipeline": PIPELINE_NAME})


@app.route("/analytics/feedback", methods=["POST"])
def analytics_feedback():
    try:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return _fail("Invalid feedback payload", status_code=400)
        paths = ensure_paths(load_config())
        event = persist_feedback_event(payload=payload, analytics_root=paths["analytics"])
        return jsonify(
            {
                "success": True,
                "pipeline": PIPELINE_NAME,
                "status": "feedback_recorded",
                "feedback_event_id": event["feedback_event_id"],
                "job_id": event["job_id"],
                "clip_id": event["clip_id"],
            }
        )
    except ValueError as exc:
        return _fail("Invalid feedback payload", log_detail=str(exc), status_code=400)
    except Exception as exc:
        print("[analytics] feedback error:", repr(exc), flush=True)
        return _fail("Feedback persistence failed", log_detail=repr(exc), status_code=500)


@app.route("/doctor", methods=["GET"])
def doctor():
    config = load_config()
    paths = resolve_paths(config)
    checks: list[dict[str, object]] = []

    def _check(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": ok, "detail": detail})

    def _is_writable_dir(path: str) -> bool:
        return os.path.isdir(path) and os.access(path, os.W_OK | os.X_OK)

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
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    whisper_path = shutil.which("whisper")
    _check("ffmpeg", bool(ffmpeg_path), ffmpeg_path or "Not found in PATH")
    _check("ffprobe", bool(ffprobe_path), ffprobe_path or "Not found in PATH")
    _check("whisper", bool(whisper_path), whisper_path or "Not found in PATH")
    _check(
        "OPENAI_API_KEY",
        bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "Set" if os.environ.get("OPENAI_API_KEY", "").strip() else "Missing",
    )
    for key, path in paths.items():
        _check(f"path:{key}", os.path.isdir(path), path)
        _check(f"path_writable:{key}", _is_writable_dir(path), path)

    config_path = resolved_pipeline_config_path()
    profiles_path = os.environ.get("VIDEO_PIPELINE_PROFILES_PATH", "").strip()
    if not profiles_path:
        profiles_path = os.path.join(os.path.dirname(config_path), "video_pipeline_profiles.json")
    _check("config:pipeline_config", os.path.isfile(config_path), config_path)
    _check("config:video_pipeline_profiles", os.path.isfile(profiles_path), profiles_path)
    _check(
        "config:default_pipeline_profile",
        bool((config.get("defaults") or {}).get("pipeline_profile")),
        str((config.get("defaults") or {}).get("pipeline_profile") or "Not set"),
    )

    adapter_probe_url = request.args.get("adapter_probe_url", "").strip()
    if adapter_probe_url:
        try:
            req = urlrequest.Request(adapter_probe_url, method="GET")
            with urlrequest.urlopen(req, timeout=5) as resp:
                _check("adapter_probe", 200 <= int(resp.status) < 400, f"HTTP {resp.status}")
        except Exception as e:
            _check("adapter_probe", False, str(e))

    all_ok = all(bool(c["ok"]) for c in checks)
    # Always HTTP 200 so load balancers and external probes treat the process as
    # reachable; use JSON "ok" for readiness (deploy/scripts/doctor.sh checks "ok").
    return jsonify({"ok": all_ok, "checks": checks, "pipeline": PIPELINE_NAME}), 200


if __name__ == "__main__":
    host = os.environ.get("VIDEO_AUTOMATION_HOST", "0.0.0.0")
    port = int(os.environ.get("VIDEO_AUTOMATION_PORT", "5050"))
    debug = os.environ.get("VIDEO_AUTOMATION_DEBUG", "0") == "1"
    env = os.environ.get("MK04_ENV", "dev")
    upload_mode = os.environ.get("MK04_UPLOAD_MODE", "dry_run")
    print(
        "[video-automation] "
        f"ENV={env.upper()} upload_mode={upload_mode} "
        f"config={os.environ.get('PIPELINE_CONFIG_PATH', '')} "
        f"funnels={os.environ.get('FUNNEL_CONFIG_DIR', '')} "
        f"output_funnel={os.environ.get('OUTPUT_FUNNEL_URL', '')} "
        f"port={port}",
        flush=True,
    )
    app.run(host=host, port=port, debug=debug)
