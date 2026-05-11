import json
import os
import subprocess
import sys
import uuid
import base64
import tempfile
import time
import mimetypes
import re
import shutil
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "scripts"))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from chunk_pipeline import (
    ffmpeg_extract_segment,
    merge_whisper_json_files,
    plan_wallclock_chunks,
    should_use_chunked_transcription,
    whisper_json_for_video,
    write_merged_whisper_json,
)
from analytics_store import persist_feedback_event, persist_run_analytics
from pipeline_utils import (
    normalize_segments,
    parse_selection_payload,
    resolved_pipeline_config_path,
    resolve_pipeline_run_policy,
    parse_time_to_seconds,
    postprocess_segments,
)
from mk04_utils import (
    categorize_error,
    create_job_paths,
    ensure_paths,
    ffprobe_duration_sec,
    load_config,
    maybe_copy,
    normalize_transcript_payload,
    require_timed_transcript_payload,
    validate_and_repair_selection,
    write_json,
    write_review,
    now_iso,
)

app = Flask(__name__)
PYTHON = sys.executable
PIPELINE_NAME = "mk0.4"
DEBUG_LOG_PATH = os.environ.get("DEBUG_LOG_PATH", "").strip()
DEBUG_SESSION_ID = "04601b"
AGENT_DEBUG_LOG_PATH = os.environ.get("AGENT_DEBUG_LOG_PATH", "").strip()
AGENT_DEBUG_SESSION_ID = "35c21b"
DEBUG_MODE_LOG_PATH = os.environ.get("DEBUG_MODE_LOG_PATH", "").strip()
DEBUG_MODE_SESSION_ID = "c9492c"


def _debug_log(hypothesis_id: str, location: str, message: str, data: dict):
    if not DEBUG_LOG_PATH:
        return
    payload = {
        "sessionId": DEBUG_SESSION_ID,
        "runId": PIPELINE_NAME,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception:
        pass


def _agent_debug_log(
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, object],
):
    if not AGENT_DEBUG_LOG_PATH:
        return
    payload = {
        "sessionId": AGENT_DEBUG_SESSION_ID,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(AGENT_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception:
        pass


def _debug_mode_log(
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, object],
):
    if not DEBUG_MODE_LOG_PATH:
        return
    payload = {
        "sessionId": DEBUG_MODE_SESSION_ID,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        with open(DEBUG_MODE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except Exception:
        pass


CURSOR_DEBUG789_PATH = "/Users/anthonymaguire/VAmk0.4/.cursor/debug-789d41.log"
CURSOR_DEBUG789_SESSION = "789d41"


def _cursor_debug789(
    hypothesis_id: str, location: str, message: str, data: dict
) -> None:
    # #region agent log
    try:
        payload = {
            "sessionId": CURSOR_DEBUG789_SESSION,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open(CURSOR_DEBUG789_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        pass
    # #endregion


def _parse_n8n_webhook_error(body: str) -> dict[str, str]:
    """Extract structured fields from n8n JSON error bodies (e.g. 404 webhook)."""
    raw = (body or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(obj, dict):
        return {}
    out: dict[str, str] = {}
    for k in ("message", "hint", "code"):
        v = obj.get(k)
        if v is not None and str(v).strip():
            out[k] = str(v).strip()
    return out


def _lift_n8n_wrapped_video_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Lift ``video`` / ``video_path`` / ``funnel_id`` from common n8n wrappers.

    n8n items often look like ``{ "json": { "video_path": "...", ... } }`` when
    the HTTP Request node sends the whole item; without lifting, only ``selection``
    might be merged at the top and ``video_path`` stays nested.
    """
    if not isinstance(data, dict):
        return {}
    out = dict(data)
    lift_keys = ("video", "video_path", "funnel_id")
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


def _resolve_input_video_path(video_name: str) -> tuple[str, str]:
    config = load_config()
    input_root = ensure_paths(config)["input"]
    os.makedirs(input_root, exist_ok=True)

    normalized_name = os.path.basename(str(video_name or "").strip())
    if not normalized_name:
        normalized_name = f"upload_{uuid.uuid4().hex[:10]}.mp4"

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


def _build_multipart_form(
    fields: dict[str, str], files: list[tuple[str, str, bytes, str]]
) -> tuple[bytes, str]:
    boundary = f"----mk03-{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    for field_name, filename, blob, content_type in files:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8")
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(blob)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _post_clips_to_n8n_once(
    *,
    webhook_url: str,
    fields: dict[str, str],
    files: list[tuple[str, str, bytes, str]],
    timeout_sec: float,
) -> tuple[int, str]:
    payload, content_type = _build_multipart_form(fields, files)
    req = urlrequest.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout_sec) as resp:
            status = int(resp.status)
            body_text = resp.read(2000).decode("utf-8", errors="replace")
            return status, body_text
    except HTTPError as e:
        try:
            raw = e.read(2000)
            body_text = raw.decode("utf-8", errors="replace")
        except Exception:
            body_text = str(e)
        return int(e.code), body_text
    except URLError as e:
        raise RuntimeError(f"n8n delivery failed: {e}") from e


def _post_clips_to_n8n(
    *,
    webhook_url: str,
    clips: list[dict[str, object]],
    source_video_path: str,
    include_files: bool,
    run_id: str,
    max_attempts: int,
    timeout_sec: float,
    backoff_sec: float,
) -> dict[str, object]:
    clip_count = len(clips)
    fields: dict[str, str] = {
        "pipeline": PIPELINE_NAME,
        "run_id": run_id,
        "source_video_name": os.path.basename(source_video_path),
        "clip_count": str(clip_count),
        "clips_json": json.dumps(clips),
    }
    files: list[tuple[str, str, bytes, str]] = []
    if include_files:
        for idx, clip in enumerate(clips, start=1):
            clip_path = str(clip.get("clip_path") or "").strip()
            if not clip_path or not os.path.isfile(clip_path):
                continue
            with open(clip_path, "rb") as f:
                blob = f.read()
            ctype = mimetypes.guess_type(clip_path)[0] or "application/octet-stream"
            files.append((f"clip_{idx:02d}", os.path.basename(clip_path), blob, ctype))

    expected_files = clip_count if include_files else 0
    last_status = 0
    last_body = ""
    last_err: str | None = None
    attempts_used = 0

    # #region agent log
    _debug_log(
        "H-webhook-url",
        "app.py:_post_clips_to_n8n",
        "n8n multipart POST target",
        {
            "webhook_url": webhook_url,
            "clip_count": clip_count,
            "include_files": include_files,
            "multipart_files_built": len(files),
        },
    )
    # #endregion

    for attempt in range(1, max(1, max_attempts) + 1):
        attempts_used = attempt
        try:
            status, last_body = _post_clips_to_n8n_once(
                webhook_url=webhook_url,
                fields=fields,
                files=files,
                timeout_sec=timeout_sec,
            )
            last_status = status
            ok_http = 200 <= status < 300
            if ok_http:
                files_sent = len(files)
                mismatch = bool(
                    include_files and files_sent != clip_count and clip_count > 0
                )
                return {
                    "ok": True,
                    "skipped": False,
                    "status_code": status,
                    "clip_count": clip_count,
                    "files_sent": files_sent,
                    "expected_files": expected_files,
                    "file_count_mismatch": mismatch,
                    "run_id": run_id,
                    "response_excerpt": last_body,
                    "attempts_used": attempts_used,
                }
            # Do not retry permanent client errors
            if 400 <= status < 500:
                n8n_err = _parse_n8n_webhook_error(last_body)
                err_msg = f"webhook returned HTTP {status}"
                if n8n_err.get("message"):
                    err_msg = f"{err_msg}: {n8n_err['message']}"
                row: dict[str, object] = {
                    "ok": False,
                    "skipped": False,
                    "status_code": status,
                    "clip_count": clip_count,
                    "files_sent": len(files),
                    "expected_files": expected_files,
                    "run_id": run_id,
                    "error": err_msg,
                    "response_excerpt": last_body,
                    "attempts_used": attempts_used,
                }
                if n8n_err:
                    row["n8n_error"] = n8n_err
                return row
            last_err = f"HTTP {status}"
        except RuntimeError as e:
            last_err = str(e)

        if attempt < max(1, max_attempts):
            delay = backoff_sec * (2 ** (attempt - 1))
            time.sleep(delay)

    return {
        "ok": False,
        "skipped": False,
        "status_code": last_status or None,
        "clip_count": clip_count,
        "files_sent": len(files),
        "expected_files": expected_files,
        "run_id": run_id,
        "error": last_err or "n8n delivery failed after retries",
        "response_excerpt": last_body,
        "attempts_used": attempts_used,
    }


def resolve_http_policy_bundle(
    *,
    selection_blob: dict[str, Any] | None,
    pipeline_blob: dict[str, Any] | None,
    pipeline_profile_hint: Any,
) -> dict[str, Any]:
    """Merge repo config + profiles + env + HTTP into one auditable bundle."""

    cfg = load_config()
    cfg_abs = resolved_pipeline_config_path()
    pf = (
        pipeline_profile_hint.strip()
        if isinstance(pipeline_profile_hint, str) and pipeline_profile_hint.strip()
        else None
    )
    return resolve_pipeline_run_policy(
        pipeline_config_abs=cfg_abs,
        pipeline_config=cfg,
        pipeline_profile=pf,
        request_pipeline_blob=pipeline_blob,
        request_selection_blob=selection_blob or {},
    )


def _run_pipeline(video_path: str, policy_bundle: dict[str, Any]):
    config = load_config()
    resolved_paths = ensure_paths(config)
    input_root = resolved_paths["input"]
    temp_root = resolved_paths["temp"]
    output_root = resolved_paths["output"]

    audit_plain = dict(policy_bundle.get("policy_audit") or {})
    selection_policy = dict(policy_bundle["selection"])
    models_eff_mb = dict(policy_bundle.get("models_effective") or {})

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

    job = create_job_paths(config, video_path)
    warnings: list[dict[str, object]] = []
    stage_ms: dict[str, int] = {}
    created_at = now_iso()
    total_started = time.perf_counter()
    report: dict[str, object] = {
        "job_id": job["job_id"],
        "input_video_path": os.path.abspath(video_path),
        "input_video_name": os.path.basename(video_path),
        "video_duration_sec": ffprobe_duration_sec(video_path),
        "transcript_path": None,
        "selection_path": job["selection_path"],
        "analytics_path": job["analytics_path"],
        "status": "running",
        "created_at": created_at,
        "completed_at": None,
        "errors": [],
        "warnings": warnings,
        "stage_timings_ms": stage_ms,
        "clips": [],
        "policy_resolution": audit_plain,
    }

    for notice in audit_plain.get("warnings", []):
        warnings.append(
            categorize_error("configuration", "policy_notice", str(notice), None),
        )

    chunk_scratch_dirs: list[str] = []
    chunk_sidecar_whisper_paths: list[str] = []

    maybe_copy(video_path, job["input_copy_path"])

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
            report["status"] = "failed"
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
                report["status"] = "failed"
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
                    report["status"] = "failed"
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
                report["status"] = "failed"
                return _fail(
                    "Transcript rejected: missing timed Whisper segments.",
                    log_detail=str(exc),
                    status_code=422,
                )
            write_json(job["normalized_transcript_path"], transcript_payload)

            t1 = time.perf_counter()
            result = subprocess.run(
                [
                    PYTHON,
                    script_select,
                    transcript_path,
                    json.dumps(
                        {
                            "max_clips": max_clips,
                            "min_duration_sec": min_duration_sec,
                            "max_duration_sec": max_duration_sec,
                            "max_overlap_sec": max_overlap_sec,
                            "video_duration_sec": report["video_duration_sec"],
                            "include_reasons": include_reasons,
                            "include_clip_metadata": include_clip_metadata,
                            "selection_model": selection_model_used,
                        }
                    ),
                ],
                capture_output=True,
                text=True,
            )
            stage_ms["selection_ms"] = int((time.perf_counter() - t1) * 1000)
            if result.returncode != 0:
                err = categorize_error(
                    "selection",
                    "selection_error",
                    "Selection failed",
                    result.stderr or result.stdout,
                )
                report["errors"] = [err]
                report["status"] = "failed"
                return _fail(
                    "Selection failed",
                    log_detail=err["details"],
                    status_code=_selection_subprocess_http_status(str(err.get("details", ""))),
                )

            raw_out = result.stdout.strip()
            try:
                processed_segments = _parse_segments_from_selector_output(
                    raw_out,
                    selector_max_clips=max_clips,
                    min_duration_sec=min_duration_sec,
                    max_duration_sec=max_duration_sec,
                    max_overlap_sec=max_overlap_sec,
                    video_duration_sec=report["video_duration_sec"],
                )
            except (ValueError, json.JSONDecodeError) as e:
                err = categorize_error(
                    "selection_validation",
                    "selection_error",
                    "Invalid selection output JSON",
                    str(e),
                )
                report["errors"] = [err]
                report["status"] = "failed"
                return _fail("Invalid selection output", log_detail=str(e), status_code=500)

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
                report["status"] = "failed"
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

            zip_paths_offsets: list[tuple[str, float]] = []
            t0 = time.perf_counter()
            for idx, (start_sec, dur_sec) in enumerate(specs):
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
                    report["status"] = "failed"
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
                    report["status"] = "failed"
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
                    report["status"] = "failed"
                    return _fail(
                        "Transcript not created (chunked)",
                        log_detail=whisper_path,
                        status_code=500,
                    )
                chunk_sidecar_whisper_paths.append(whisper_path)
                zip_paths_offsets.append((whisper_path, float(start_sec)))

            stage_ms["transcription_ms"] = int((time.perf_counter() - t0) * 1000)

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
                report["status"] = "failed"
                return _fail(
                    "Merged transcript rejected: missing timed segments.",
                    log_detail=str(exc),
                    status_code=422,
                )
            write_json(job["normalized_transcript_path"], transcript_payload)

            t1 = time.perf_counter()
            combined_segments: list[dict] = []
            for idx, (start_sec, dur_sec) in enumerate(specs):
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
                    report["status"] = "failed"
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
                    report["status"] = "failed"
                    return _fail(
                        "Chunk transcript rejected before selection.",
                        log_detail=str(exc),
                        status_code=422,
                    )
                sel_opts = {
                    "max_clips": per_chunk_budget,
                    "min_duration_sec": min_duration_sec,
                    "max_duration_sec": max_duration_sec,
                    "max_overlap_sec": max_overlap_sec,
                    "video_duration_sec": chunk_dur,
                    "include_reasons": include_reasons,
                    "include_clip_metadata": include_clip_metadata,
                    "selection_model": selection_model_used,
                    "timeline_offset_sec": float(start_sec),
                    "is_chunk_slice": True,
                }
                result = subprocess.run(
                    [
                        PYTHON,
                        script_select,
                        whisper_path_chunk,
                        json.dumps(sel_opts),
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    err = categorize_error(
                        "selection",
                        "selection_error",
                        f"Selection failed for chunk {idx}",
                        result.stderr or result.stdout,
                    )
                    report["errors"] = [err]
                    report["status"] = "failed"
                    return _fail(
                        "Selection failed (chunked)",
                        log_detail=err["details"],
                        status_code=_selection_subprocess_http_status(str(err.get("details", ""))),
                    )
                raw_out = result.stdout.strip()
                try:
                    part = _parse_segments_from_selector_output(
                        raw_out,
                        selector_max_clips=per_chunk_budget,
                        min_duration_sec=min_duration_sec,
                        max_duration_sec=max_duration_sec,
                        max_overlap_sec=max_overlap_sec,
                        video_duration_sec=chunk_dur,
                    )
                except (ValueError, json.JSONDecodeError) as e:
                    err = categorize_error(
                        "selection_validation",
                        "selection_error",
                        f"Invalid selection output JSON (chunk {idx})",
                        str(e),
                    )
                    report["errors"] = [err]
                    report["status"] = "failed"
                    return _fail("Invalid selection output", log_detail=str(e), status_code=500)
                combined_segments.extend(part)

            stage_ms["selection_ms"] = int((time.perf_counter() - t1) * 1000)

            try:
                processed_segments = postprocess_segments(
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
                report["status"] = "failed"
                return _fail("Invalid selection output", log_detail=str(e), status_code=500)

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
            report["status"] = "failed"
            return _fail("No valid clips selected after timestamp validation", status_code=422)

        write_json(
            job["selection_path"],
            {
                "clips": validated_segments,
                "validation_warnings": validation_issues,
            },
        )

        t2 = time.perf_counter()
        clips: list[dict[str, object]] = []
        for index, segment in enumerate(validated_segments, start=1):
            start = str(segment["start"]).strip()
            end = str(segment["end"]).strip()
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
                report["status"] = "failed"
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
                report["status"] = "failed"
                return _fail("Clipping failed: missing result envelope", log_detail=str(err["details"]), status_code=500)
            if envelope.get("ok") is not True or str(envelope.get("script", "")).strip() != "clip_video":
                err = categorize_error(
                    "clipping",
                    "clipping_error",
                    "Clipping result envelope was not successful for clip_video.",
                    {"envelope": envelope, "stderr": clip.stderr or clip.stdout},
                )
                report["errors"] = [err]
                report["status"] = "failed"
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
                report["status"] = "failed"
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
                report["status"] = "failed"
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
        report["completed_at"] = now_iso()
        stage_ms["total_ms"] = int((time.perf_counter() - total_started) * 1000)
        write_json(job["report_path"], report)
        write_review(job["review_path"], report)

        run_id = uuid.uuid4().hex
        response: dict[str, object] = {
            "success": True,
            "pipeline": PIPELINE_NAME,
            "run_id": run_id,
            "source_video": os.path.basename(video_path),
            "video_basename": filename,
            "clips": clips,
            "delivery_mode": "pull_from_output_endpoint",
            "job_id": job["job_id"],
            "job_dir": job["job_dir"],
            "report_path": job["report_path"],
            "review_path": job["review_path"],
            "analytics_path": job["analytics_path"],
            "transcript_payload_path": job["normalized_transcript_path"],
            "selection_path": job["selection_path"],
            "policy_resolution": audit_plain,
        }
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
            if report.get("completed_at") is None:
                report["completed_at"] = now_iso()
                if report.get("status") == "running":
                    report["status"] = "failed"
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


@app.route("/upload", methods=["POST"])
def upload_video():
    try:
        upload_started_ms = int(time.time() * 1000)
        debug_run_id = f"run-{uuid.uuid4().hex[:8]}"
        # #region agent log
        _debug_mode_log(
            debug_run_id,
            "H1-endpoint-mismatch",
            "app.py:upload_video",
            "upload endpoint received request",
            {
                "content_type": request.content_type or "",
                "has_video_file": bool(request.files.get("video_file")),
                "form_keys": sorted(list(request.form.keys())),
            },
        )
        # #endregion
        # #region agent log
        _agent_debug_log(
            "general-debug",
            "H13-ingress-upload",
            "app.py:upload_video",
            "upload endpoint hit",
            {
                "content_type": request.content_type,
                "has_video_file": bool(request.files.get("video_file")),
                "form_keys": sorted(list(request.form.keys())),
            },
        )
        # #endregion
        upload = request.files.get("video_file")
        if upload is None:
            return _fail("Missing video_file", status_code=400)

        requested_name = str(request.form.get("video_name") or upload.filename or "").strip()
        video_name, video_path = _resolve_input_video_path(requested_name)
        upload.save(video_path)

        if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
            return _fail("Uploaded file is empty", status_code=400)

        # #region agent log
        _debug_mode_log(
            debug_run_id,
            "H5-response-late-or-missing",
            "app.py:upload_video",
            "upload endpoint returning success",
            {
                "video_name": video_name,
                "elapsed_ms": int(time.time() * 1000) - upload_started_ms,
                "size_bytes": os.path.getsize(video_path),
            },
        )
        # #endregion
        return jsonify(
            {
                "success": True,
                "pipeline": PIPELINE_NAME,
                "video": video_name,
                "video_path": video_path,
            }
        )
    except ValueError as e:
        return _fail(str(e), status_code=400)
    except Exception as e:
        print("[upload] unexpected error:", repr(e), flush=True)
        return _fail("Upload failed", log_detail=repr(e), status_code=500)


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


@app.route("/process", methods=["POST"])
def process():
    # #region agent log
    try:
        _raw_len = request.content_length
    except Exception:
        _raw_len = None
    _cursor_debug789(
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
        _cursor_debug789(
            "H6",
            "app.py:process",
            "raw json body prefix",
            {"raw_prefix": raw_preview},
        )
        # #endregion
        payload = request.get_json(silent=True) or {}
        keys_before = sorted(payload.keys())
        payload = _lift_n8n_wrapped_video_fields(payload)
        keys_after = sorted(payload.keys())
        if keys_before != keys_after:
            # #region agent log
            _cursor_debug789(
                "H6",
                "app.py:process",
                "lifted fields from nested n8n wrapper",
                {"keys_before": keys_before, "keys_after": keys_after},
            )
            # #endregion
        raw_video = payload.get("video")
        raw_path = payload.get("video_path")
        video_arg: str | None = None
        video_from_path = False
        if raw_video is not None and str(raw_video).strip():
            video_arg = str(raw_video).strip()
        elif raw_path is not None and str(raw_path).strip():
            video_arg = os.path.basename(str(raw_path).strip().rstrip("/"))
            video_from_path = True
            # #region agent log
            _cursor_debug789(
                "H4",
                "app.py:process",
                "derived video basename from video_path",
                {
                    "video_path_prefix": str(raw_path)[:280],
                    "video_arg": video_arg,
                },
            )
            # #endregion
        if not video_arg:
            # #region agent log
            _cursor_debug789(
                "H4",
                "app.py:process",
                "fail missing video and video_path",
                {"payload_keys": sorted(payload.keys())},
            )
            # #endregion
            return _fail(
                "Missing usable 'video' or 'video_path' in the JSON body (after unwrapping common n8n keys: "
                "json, body, data, item). Send run-funnel's video_path, e.g. in the HTTP node use a JSON body with "
                "\"video_path\": \"={{ $json.video_path }}\" on the same item that received /run-funnel output, "
                "or merge that field into the object next to \"selection\". Undefined n8n expressions are omitted by JSON.stringify.",
                status_code=400,
            )
        selection_policy = payload.get("selection", {}) or {}
        # #region agent log
        try:
            _cfg = load_config()
            _input_root_dbg = ensure_paths(_cfg)["input"]
        except Exception as _e:
            _input_root_dbg = f"<ensure_paths_error:{_e}>"
        _cursor_debug789(
            "H4",
            "app.py:process",
            "json parsed",
            {
                "video_arg": str(video_arg)[:500],
                "video_from_video_path": video_from_path,
                "payload_keys": sorted(payload.keys()),
                "payload_empty": payload == {},
                "input_root_resolved": _input_root_dbg,
            },
        )
        # #endregion
        if not isinstance(selection_policy, dict):
            # #region agent log
            _cursor_debug789(
                "H3",
                "app.py:process",
                "fail invalid selection type",
                {"detail": repr(selection_policy)[:300]},
            )
            # #endregion
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
            # #region agent log
            _cursor_debug789(
                "H3",
                "app.py:process",
                "fail pipeline not object",
                {"pipe_type": type(pipe_raw).__name__},
            )
            # #endregion
            return _fail("`pipeline` must be a JSON object when provided", status_code=400)

        prof_hint = payload.get("pipeline_profile")
        if prof_hint is None:
            prof_hint = payload.get("funnel_id")

        try:
            bundle = resolve_http_policy_bundle(
                selection_blob=selection_policy,
                pipeline_blob=pipe_blob,
                pipeline_profile_hint=prof_hint,
            )
        except ValueError as exc:
            # #region agent log
            _cursor_debug789(
                "H3",
                "app.py:process",
                "fail policy bundle",
                {"error": str(exc)[:500]},
            )
            # #endregion
            return _fail("Invalid pipeline policy resolution", log_detail=str(exc), status_code=400)

        _, video_path = _resolve_input_video_path(str(video_arg))
        # #region agent log
        _cursor_debug789(
            "H2",
            "app.py:process",
            "video path resolved",
            {
                "video_arg": str(video_arg)[:500],
                "video_path": video_path,
                "isfile": os.path.isfile(video_path),
            },
        )
        # #endregion
        if not os.path.isfile(video_path):
            # #region agent log
            _cursor_debug789(
                "H2",
                "app.py:process",
                "fail input not found",
                {"video_path": video_path},
            )
            # #endregion
            return _fail(
                "Input video not found for /process. Upload first via /upload, or use /process-inline to provide the video directly.",
                log_detail=video_path,
                status_code=400,
            )
        # #region agent log
        _cursor_debug789(
            "H5",
            "app.py:process",
            "starting _run_pipeline",
            {"video_path": video_path},
        )
        # #endregion
        return _run_pipeline(video_path, bundle)

    except Exception as e:
        print("[process] unexpected error:", repr(e), flush=True)
        # #region agent log
        _cursor_debug789(
            "H5",
            "app.py:process",
            "exception",
            {"error": repr(e)[:800]},
        )
        # #endregion
        return _fail("Processing failed", log_detail=repr(e), status_code=500)


@app.route("/process-inline", methods=["POST"])
def process_inline():
    tmp_path = None
    started_ms = int(time.time() * 1000)
    debug_run_id = f"run-{uuid.uuid4().hex[:8]}"
    try:
        # #region agent log
        _debug_mode_log(
            debug_run_id,
            "H1-endpoint-mismatch",
            "app.py:process_inline",
            "process-inline endpoint received request",
            {
                "content_type": request.content_type or "",
                "has_video_file": bool(request.files.get("video_file")),
                "has_json_body": bool(request.get_json(silent=True) or {}),
                "form_keys": sorted(list(request.form.keys())),
            },
        )
        # #endregion
        # #region agent log
        _agent_debug_log(
            "general-debug",
            "H14-ingress-process-inline",
            "app.py:process_inline",
            "process-inline endpoint hit",
            {
                "content_type": request.content_type,
                "has_video_file": bool(request.files.get("video_file")),
                "has_json_body": bool(request.get_json(silent=True) or {}),
                "form_keys": sorted(list(request.form.keys())),
            },
        )
        # #endregion
        input_root = ensure_paths(load_config())["input"]
        upload = request.files.get("video_file")
        payload = request.get_json(silent=True) or {}

        # process-inline is intentionally blocking (upload + full pipeline in one request).
        # Require explicit opt-in to avoid accidental long-running n8n upload requests.
        inline_opt_in_raw = request.form.get("allow_blocking_inline")
        if inline_opt_in_raw is None:
            inline_opt_in_raw = payload.get("allow_blocking_inline")
        inline_opt_in = str(inline_opt_in_raw or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not inline_opt_in:
            # #region agent log
            _debug_mode_log(
                debug_run_id,
                "H1-endpoint-mismatch",
                "app.py:process_inline",
                "rejected process-inline without explicit opt-in",
                {
                    "has_video_file": bool(upload),
                    "form_keys": sorted(list(request.form.keys())),
                    "payload_keys": (
                        sorted(list(payload.keys()))
                        if isinstance(payload, dict)
                        else []
                    ),
                },
            )
            # #endregion
            return _fail(
                "Endpoint /process-inline is synchronous and long-running. "
                "Use /upload then /process for non-blocking n8n flows, "
                "or set allow_blocking_inline=true to opt in.",
                status_code=400,
            )

        if upload is not None:
            selection_raw = request.form.get("selection")
            if not isinstance(selection_raw, str) or not selection_raw.strip():
                selection_policy = {}
            else:
                try:
                    selection_policy = json.loads(selection_raw)
                except json.JSONDecodeError as e:
                    return _fail("Invalid selection policy", log_detail=str(e), status_code=400)

            if not isinstance(selection_policy, dict):
                return _fail("Invalid selection policy", log_detail=repr(selection_policy), status_code=400)

            multipart_pipe_blob: dict[str, Any] = {}
            pipe_form = request.form.get("pipeline")
            if isinstance(pipe_form, str) and pipe_form.strip():
                try:
                    parsed_pb = json.loads(pipe_form)
                except json.JSONDecodeError as e:
                    return _fail("Invalid multipart `pipeline` JSON", log_detail=str(e), status_code=400)
                if not isinstance(parsed_pb, dict):
                    return _fail("multipart field `pipeline` must be a JSON object", status_code=400)
                multipart_pipe_blob = parsed_pb

            multipart_prof = (
                request.form.get("pipeline_profile")
                or request.form.get("funnel_id")
                or payload.get("pipeline_profile")
                or payload.get("funnel_id")
            )

            try:
                multipart_bundle = resolve_http_policy_bundle(
                    selection_blob=selection_policy,
                    pipeline_blob=multipart_pipe_blob,
                    pipeline_profile_hint=multipart_prof,
                )
            except ValueError as exc:
                return _fail("Invalid pipeline policy resolution", log_detail=str(exc), status_code=400)

            video_name = str(
                request.form.get("video_name") or upload.filename or "upload.mp4"
            )
            _, ext = os.path.splitext(video_name)
            suffix = ext if ext else ".mp4"

            # #region agent log
            _debug_log(
                "H-new-upload",
                "server/app.py:process_inline",
                "Received multipart upload payload",
                {
                    "video_name": video_name,
                    "selection_keys": sorted(selection_policy.keys()),
                    "content_type": request.content_type,
                },
            )
            # #endregion

            with tempfile.NamedTemporaryFile(
                mode="wb",
                suffix=suffix,
                prefix="n8n_upload_",
                dir=input_root,
                delete=False,
            ) as tmp:
                upload.save(tmp)
                tmp_path = os.path.abspath(tmp.name)

            # #region agent log
            _debug_log(
                "H-new-upload",
                "server/app.py:process_inline",
                "Wrote multipart upload to temp file",
                {"tmp_path": tmp_path, "size_bytes": os.path.getsize(tmp_path)},
            )
            # #endregion

            result = _run_pipeline(tmp_path, multipart_bundle)
            # #region agent log
            _debug_mode_log(
                debug_run_id,
                "H3-multipart-vs-json-path",
                "app.py:process_inline",
                "multipart branch completed pipeline",
                {
                    "elapsed_ms": int(time.time() * 1000) - started_ms,
                    "tmp_path": tmp_path,
                },
            )
            # #endregion
            # #region agent log
            _agent_debug_log(
                "general-debug",
                "H16-process-inline-return",
                "app.py:process_inline",
                "process-inline returning multipart flow response",
                {
                    "elapsed_ms": int(time.time() * 1000) - started_ms,
                    "tmp_path": tmp_path,
                },
            )
            # #endregion
            return result

        selection_policy = payload.get("selection", {}) or {}
        if not isinstance(selection_policy, dict):
            return _fail("Invalid selection policy", log_detail=repr(selection_policy), status_code=400)

        pipe_raw = payload.get("pipeline")
        if pipe_raw is None:
            inline_pipe: dict[str, Any] = {}
        elif isinstance(pipe_raw, dict):
            inline_pipe = pipe_raw
        else:
            return _fail("`pipeline` must be a JSON object when provided", status_code=400)

        inline_prof_hint = payload.get("pipeline_profile")
        if inline_prof_hint is None:
            inline_prof_hint = payload.get("funnel_id")

        try:
            inline_bundle = resolve_http_policy_bundle(
                selection_blob=selection_policy,
                pipeline_blob=inline_pipe,
                pipeline_profile_hint=inline_prof_hint,
            )
        except ValueError as exc:
            return _fail("Invalid pipeline policy resolution", log_detail=str(exc), status_code=400)

        video_name = str(payload.get("video_name") or payload.get("video") or "upload.mp4")
        video_b64 = payload.get("video_b64")
        if not isinstance(video_b64, str) or not video_b64.strip():
            return _fail("Missing video_b64", status_code=400)

        _, ext = os.path.splitext(video_name)
        suffix = ext if ext else ".mp4"

        # #region agent log
        _debug_log(
            "H-new-upload",
            "server/app.py:process_inline",
            "Received inline processing payload",
            {"video_name": video_name, "has_b64": bool(video_b64), "suffix": suffix},
        )
        # #endregion

        raw_bytes = base64.b64decode(video_b64, validate=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=suffix,
            prefix="n8n_upload_",
            dir=input_root,
            delete=False,
        ) as tmp:
            tmp.write(raw_bytes)
            tmp_path = os.path.abspath(tmp.name)

        # #region agent log
        _debug_log(
            "H-new-upload",
            "server/app.py:process_inline",
            "Wrote inline upload to temp file",
            {"tmp_path": tmp_path, "size_bytes": len(raw_bytes)},
        )
        # #endregion

        result = _run_pipeline(tmp_path, inline_bundle)
        # #region agent log
        _debug_mode_log(
            debug_run_id,
            "H3-multipart-vs-json-path",
            "app.py:process_inline",
            "json-base64 branch completed pipeline",
            {
                "elapsed_ms": int(time.time() * 1000) - started_ms,
                "tmp_path": tmp_path,
            },
        )
        # #endregion
        # #region agent log
        _agent_debug_log(
            "general-debug",
            "H16-process-inline-return",
            "app.py:process_inline",
            "process-inline returning base64 flow response",
            {
                "elapsed_ms": int(time.time() * 1000) - started_ms,
                "tmp_path": tmp_path,
            },
        )
        # #endregion
        return result
    except Exception as e:
        # #region agent log
        _debug_log(
            "H-new-upload",
            "server/app.py:process_inline",
            "Inline processing exception",
            {"error": repr(e)},
        )
        # #endregion
        print("[process-inline] unexpected error:", repr(e), flush=True)
        return _fail("Processing failed", log_detail=repr(e), status_code=500)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception as exc:
                print(
                    "[process-inline_cleanup] Removing temp upload failed:",
                    tmp_path,
                    repr(exc),
                    flush=True,
                )


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
    paths = ensure_paths(config)
    checks: list[dict[str, object]] = []

    def _check(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": ok, "detail": detail})

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

    probe_url = request.args.get("probe_url", "").strip()
    if probe_url:
        try:
            req = urlrequest.Request(probe_url, method="GET")
            with urlrequest.urlopen(req, timeout=5) as resp:
                _check("n8n_probe", 200 <= int(resp.status) < 400, f"HTTP {resp.status}")
        except Exception as e:
            _check("n8n_probe", False, str(e))

    all_ok = all(bool(c["ok"]) for c in checks)
    status_code = 200 if all_ok else 500
    return jsonify({"ok": all_ok, "checks": checks, "pipeline": PIPELINE_NAME}), status_code


if __name__ == "__main__":
    host = os.environ.get("VIDEO_AUTOMATION_HOST", "0.0.0.0")
    port = int(os.environ.get("VIDEO_AUTOMATION_PORT", "5050"))
    debug = os.environ.get("VIDEO_AUTOMATION_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
