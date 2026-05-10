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
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "scripts"))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pipeline_utils import (
    normalize_segments,
    parse_selection_payload,
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


def _ffprobe_duration_sec(path: str) -> float | None:
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if p.returncode != 0 or not (p.stdout or "").strip():
            return None
        return float((p.stdout or "").strip())
    except Exception:
        return None


_WEAK_OPENING_WORDS = {
    "so",
    "um",
    "uh",
    "anyway",
    "like",
    "well",
    "you",
}
_HOOK_HINT_WORDS = {
    "here",
    "this",
    "why",
    "how",
    "stop",
    "never",
    "best",
    "secret",
    "mistake",
}
_WEAK_ENDING_WORDS = {
    "so",
    "um",
    "uh",
    "anyway",
    "yeah",
    "okay",
    "ok",
    "right",
    "like",
}
_PAYOFF_HINT_WORDS = {
    "therefore",
    "because",
    "so",
    "that's",
    "that",
    "which",
    "why",
    "result",
    "lesson",
    "point",
}


def _load_whisper_segments(transcript_path: str) -> list[dict[str, object]]:
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    rows = data.get("segments")
    if not isinstance(rows, list):
        return []
    cleaned: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            start = float(row.get("start"))
            end = float(row.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        text = str(row.get("text") or "").strip()
        cleaned.append({"start": start, "end": end, "text": text})
    return cleaned


def _format_hhmmss(seconds: float) -> str:
    whole = int(seconds)
    h = whole // 3600
    m = (whole % 3600) // 60
    s = whole % 60
    frac = round(seconds - whole, 3)
    if frac <= 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    ms = int(round(frac * 1000))
    if ms == 1000:
        s += 1
        ms = 0
        if s == 60:
            s = 0
            m += 1
            if m == 60:
                m = 0
                h += 1
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _text_slice_for_time(segment: dict[str, object], t_sec: float) -> str:
    text = str(segment.get("text") or "").strip()
    if not text:
        return ""
    start = float(segment["start"])
    end = float(segment["end"])
    span = max(0.001, end - start)
    frac = min(1.0, max(0.0, (t_sec - start) / span))
    words = text.split()
    if not words:
        return text
    idx = min(len(words) - 1, max(0, int(round(frac * (len(words) - 1)))))
    return " ".join(words[idx:]).strip()


def _text_prefix_for_time(segment: dict[str, object], t_sec: float) -> str:
    text = str(segment.get("text") or "").strip()
    if not text:
        return ""
    start = float(segment["start"])
    end = float(segment["end"])
    span = max(0.001, end - start)
    frac = min(1.0, max(0.0, (t_sec - start) / span))
    words = text.split()
    if not words:
        return text
    idx = min(len(words), max(1, int(round(frac * len(words)))))
    return " ".join(words[:idx]).strip()


def _looks_weak_opening(text: str) -> bool:
    clean = (text or "").strip().lower()
    if not clean:
        return True
    first_words = re.findall(r"[a-zA-Z']+", clean)[:3]
    if not first_words:
        return True
    if first_words[0] in _WEAK_OPENING_WORDS:
        return True
    if first_words[0] == "you" and len(first_words) > 1 and first_words[1] == "know":
        return True
    # Very short/noisy lead-ins are usually bad starts.
    if len(clean) < 12:
        return True
    has_hook_hint = any(w in _HOOK_HINT_WORDS for w in first_words)
    return not has_hook_hint and clean[0].islower()


def _looks_incomplete_ending(text: str) -> bool:
    clean = (text or "").strip()
    if not clean:
        return True
    tokens = re.findall(r"[a-zA-Z']+", clean.lower())
    if not tokens:
        return True

    last_words = tokens[-3:]
    # Explicit dead-time/filler endings are weak conclusions.
    if any(w in _WEAK_ENDING_WORDS for w in last_words):
        return True

    # Mid-sentence style endings typically miss terminal punctuation.
    has_terminal_punctuation = clean.endswith((".", "!", "?"))
    if has_terminal_punctuation:
        return False

    # If there's no clear payoff/resolution signal, assume incomplete.
    has_payoff_hint = any(w in _PAYOFF_HINT_WORDS for w in tokens[-8:])
    return not has_payoff_hint


def _correct_segment_end(
    segment: dict[str, object],
    whisper_segments: list[dict[str, object]],
    *,
    min_duration_sec: float,
    max_duration_sec: float,
) -> dict[str, object]:
    start_raw = str(segment.get("start", "")).strip()
    end_raw = str(segment.get("end", "")).strip()
    try:
        start_sec = parse_time_to_seconds(start_raw)
        end_sec = parse_time_to_seconds(end_raw)
    except ValueError:
        # #region agent log
        _agent_debug_log(
            "general-debug",
            "H5-time-parse",
            "app.py:_correct_segment_end",
            "end parse failed",
            {"start": start_raw, "end": end_raw},
        )
        # #endregion
        return segment
    if end_sec <= start_sec:
        return segment

    current_duration = end_sec - start_sec
    if current_duration >= max_duration_sec:
        return segment

    end_seg = _find_segment_for_time(whisper_segments, end_sec)
    if end_seg is None:
        # #region agent log
        _debug_mode_log(
            "general-debug",
            "H4-whisper-map-miss",
            "app.py:_correct_segment_end",
            "segment end had no whisper mapping",
            {"start_sec": round(start_sec, 3), "end_sec": round(end_sec, 3)},
        )
        # #endregion
        # #region agent log
        _agent_debug_log(
            "general-debug",
            "H1-boundary-lookup",
            "app.py:_correct_segment_end",
            "no whisper segment for end time",
            {"start_sec": start_sec, "end_sec": end_sec},
        )
        # #endregion
        return segment

    end_prefix = _text_prefix_for_time(end_seg, end_sec)
    remaining_in_current_seg = float(end_seg["end"]) - end_sec
    needs_adjust = _looks_incomplete_ending(end_prefix) or remaining_in_current_seg > 0.6
    # #region agent log
    _agent_debug_log(
        "general-debug",
        "H2-end-heuristic",
        "app.py:_correct_segment_end",
        "end correction decision",
        {
            "start_sec": round(start_sec, 3),
            "end_sec": round(end_sec, 3),
            "remaining_in_seg": round(remaining_in_current_seg, 3),
            "needs_adjust": needs_adjust,
        },
    )
    # #endregion
    if not needs_adjust:
        return segment

    # Forward-only, bounded extension to complete payoff naturally.
    for delta in (0.5, 1.0, 1.5, 2.0):
        candidate_end = end_sec + delta
        if candidate_end <= start_sec:
            continue
        candidate_duration = candidate_end - start_sec
        if candidate_duration < min_duration_sec:
            continue
        if candidate_duration > max_duration_sec:
            # #region agent log
            _agent_debug_log(
                "general-debug",
                "H3-duration-guard",
                "app.py:_correct_segment_end",
                "end extension blocked by max duration",
                {
                    "start_sec": round(start_sec, 3),
                    "candidate_end": round(candidate_end, 3),
                    "max_duration_sec": max_duration_sec,
                },
            )
            # #endregion
            break

        cand_seg = _find_segment_for_time(whisper_segments, candidate_end)
        if cand_seg is None:
            continue
        cand_prefix = _text_prefix_for_time(cand_seg, candidate_end)
        cand_remaining = float(cand_seg["end"]) - candidate_end
        if _looks_incomplete_ending(cand_prefix) and cand_remaining > 0.4:
            continue

        updated = dict(segment)
        updated["end"] = _format_hhmmss(candidate_end)
        updated["duration_sec"] = round(candidate_duration, 3)
        # #region agent log
        _agent_debug_log(
            "general-debug",
            "H4-correction-apply",
            "app.py:_correct_segment_end",
            "end correction applied",
            {"start": start_raw, "old_end": end_raw, "new_end": updated["end"]},
        )
        # #endregion
        return updated
    return segment


def _find_segment_for_time(
    whisper_segments: list[dict[str, object]], t_sec: float
) -> dict[str, object] | None:
    for seg in whisper_segments:
        start = float(seg["start"])
        end = float(seg["end"])
        if start <= t_sec < end:
            return seg
    return None


def _correct_segment_start(
    segment: dict[str, object],
    whisper_segments: list[dict[str, object]],
    *,
    min_duration_sec: float,
) -> dict[str, object]:
    start_raw = str(segment.get("start", "")).strip()
    end_raw = str(segment.get("end", "")).strip()
    try:
        start_sec = parse_time_to_seconds(start_raw)
        end_sec = parse_time_to_seconds(end_raw)
    except ValueError:
        # #region agent log
        _agent_debug_log(
            "general-debug",
            "H5-time-parse",
            "app.py:_correct_segment_start",
            "start parse failed",
            {"start": start_raw, "end": end_raw},
        )
        # #endregion
        return segment
    if end_sec <= start_sec:
        return segment

    current_seg = _find_segment_for_time(whisper_segments, start_sec)
    if current_seg is None:
        # #region agent log
        _debug_mode_log(
            "general-debug",
            "H4-whisper-map-miss",
            "app.py:_correct_segment_start",
            "segment start had no whisper mapping",
            {"start_sec": round(start_sec, 3), "end_sec": round(end_sec, 3)},
        )
        # #endregion
        # #region agent log
        _agent_debug_log(
            "general-debug",
            "H1-boundary-lookup",
            "app.py:_correct_segment_start",
            "no whisper segment for start time",
            {"start_sec": start_sec, "end_sec": end_sec},
        )
        # #endregion
        return segment

    current_slice = _text_slice_for_time(current_seg, start_sec)
    offset_into_text = start_sec - float(current_seg["start"])
    needs_adjust = _looks_weak_opening(current_slice) or offset_into_text > 0.35
    # #region agent log
    _agent_debug_log(
        "general-debug",
        "H2-start-heuristic",
        "app.py:_correct_segment_start",
        "start correction decision",
        {
            "start_sec": round(start_sec, 3),
            "end_sec": round(end_sec, 3),
            "offset_into_text": round(offset_into_text, 3),
            "needs_adjust": needs_adjust,
        },
    )
    # #endregion
    if not needs_adjust:
        return segment

    # Forward-only, bounded correction window.
    for delta in (0.3, 0.5, 0.7, 1.0):
        candidate_start = start_sec + delta
        if candidate_start >= end_sec:
            break
        if (end_sec - candidate_start) < min_duration_sec:
            # #region agent log
            _agent_debug_log(
                "general-debug",
                "H3-duration-guard",
                "app.py:_correct_segment_start",
                "start shift blocked by min duration",
                {
                    "candidate_start": round(candidate_start, 3),
                    "end_sec": round(end_sec, 3),
                    "min_duration_sec": min_duration_sec,
                },
            )
            # #endregion
            continue
        cand_seg = _find_segment_for_time(whisper_segments, candidate_start)
        if cand_seg is None:
            continue
        cand_slice = _text_slice_for_time(cand_seg, candidate_start)
        if _looks_weak_opening(cand_slice):
            continue
        updated = dict(segment)
        updated["start"] = _format_hhmmss(candidate_start)
        # #region agent log
        _agent_debug_log(
            "general-debug",
            "H4-correction-apply",
            "app.py:_correct_segment_start",
            "start correction applied",
            {"old_start": start_raw, "new_start": updated["start"], "end": end_raw},
        )
        # #endregion
        return updated
    return segment


def _fail(message: str, *, log_detail=None, status_code=500):
    if log_detail is not None:
        print(f"[process] {message}: {log_detail}", flush=True)
    else:
        print(f"[process] {message}", flush=True)
    body = {"success": False, "error": message, "pipeline": PIPELINE_NAME}
    return jsonify(body), status_code


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


def _run_pipeline(video_path: str, selection_policy: dict, n8n_config: dict | None = None):
    config = load_config()
    resolved_paths = ensure_paths(config)
    input_root = resolved_paths["input"]
    temp_root = resolved_paths["temp"]
    output_root = resolved_paths["output"]

    sel_cfg = config.get("selection", {})
    max_clips = int(selection_policy.get("max_clips", 5))
    min_duration_sec = float(
        selection_policy.get("min_duration_sec", sel_cfg.get("min_clip_duration_sec", 5))
    )
    max_duration_sec = float(
        selection_policy.get("max_duration_sec", sel_cfg.get("max_clip_duration_sec", 30))
    )
    max_overlap_sec = float(
        selection_policy.get("max_overlap_sec", sel_cfg.get("max_overlap_sec", 2))
    )
    include_reasons = bool(selection_policy.get("include_reasons", False))
    include_clip_metadata = bool(selection_policy.get("include_clip_metadata", True))

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
        "status": "running",
        "created_at": created_at,
        "completed_at": None,
        "errors": [],
        "warnings": warnings,
        "stage_timings_ms": stage_ms,
        "clips": [],
    }

    maybe_copy(video_path, job["input_copy_path"])

    try:
        t0 = time.perf_counter()
        transcribe = subprocess.run(
            [PYTHON, script_transcribe, video_path], capture_output=True, text=True
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
                return _fail("Transcript not created", log_detail=transcript_path, status_code=500)

        maybe_copy(transcript_path, job["transcript_copy_path"])
        report["transcript_path"] = job["transcript_copy_path"]
        transcript_payload = normalize_transcript_payload(transcript_path)
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
                        "include_reasons": include_reasons,
                        "include_clip_metadata": include_clip_metadata,
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
            return _fail("Selection failed", log_detail=err["details"], status_code=500)

        raw_out = result.stdout.strip()
        try:
            envelope = _parse_script_envelope(raw_out)
            if envelope and isinstance(envelope.get("clips"), list):
                processed_segments = normalize_segments(envelope["clips"])
            else:
                parsed = parse_selection_payload(raw_out)
                processed_segments = postprocess_segments(
                    normalize_segments(parsed),
                    max_clips=max_clips,
                    min_duration_sec=min_duration_sec,
                    max_duration_sec=max_duration_sec,
                    max_overlap_sec=max_overlap_sec,
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
            resolved_path = (
                str(envelope["output_path"])
                if envelope and isinstance(envelope.get("output_path"), str)
                else clip_path
            )
            if not os.path.isfile(resolved_path):
                resolved_path = clip_path

            job_clip_path = os.path.join(job["clips_dir"], os.path.basename(resolved_path))
            maybe_copy(resolved_path, job_clip_path)

            clip_payload: dict[str, object] = {
                "start": start,
                "end": end,
                "clip_path": resolved_path,
                "job_clip_path": job_clip_path,
                "clip_file": os.path.basename(resolved_path),
                "clip_url": f"/output/{os.path.basename(resolved_path)}",
                "duration_sec": segment.get("duration_sec"),
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
            "transcript_payload_path": job["normalized_transcript_path"],
            "selection_path": job["selection_path"],
        }
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
        except Exception:
            pass
        for maybe_path, root in ((video_path, input_root), (transcript_path, temp_root)):
            try:
                abs_path = os.path.abspath(maybe_path)
                if abs_path.startswith(root + os.sep) and os.path.isfile(abs_path):
                    os.remove(abs_path)
            except Exception:
                pass


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
    try:
        payload = request.get_json(silent=True) or {}
        video_arg = payload.get("video", "test.mp4")
        selection_policy = payload.get("selection", {}) or {}
        if not isinstance(selection_policy, dict):
            return _fail(
                "Invalid selection policy",
                log_detail=repr(selection_policy),
                status_code=400,
            )
        _, video_path = _resolve_input_video_path(str(video_arg))
        if not os.path.isfile(video_path):
            return _fail(
                "Input video not found for /process. Upload first via /upload, or use /process-inline to provide the video directly.",
                log_detail=video_path,
                status_code=400,
            )
        return _run_pipeline(video_path, selection_policy)

    except Exception as e:
        print("[process] unexpected error:", repr(e), flush=True)
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
            n8n_raw = request.form.get("n8n")
            if not isinstance(selection_raw, str) or not selection_raw.strip():
                selection_policy = {}
            else:
                try:
                    selection_policy = json.loads(selection_raw)
                except json.JSONDecodeError as e:
                    return _fail("Invalid selection policy", log_detail=str(e), status_code=400)
            if not isinstance(n8n_raw, str) or not n8n_raw.strip():
                n8n_config = {}
            else:
                try:
                    n8n_config = json.loads(n8n_raw)
                except json.JSONDecodeError as e:
                    return _fail("Invalid n8n config", log_detail=str(e), status_code=400)

            if not isinstance(selection_policy, dict):
                return _fail("Invalid selection policy", log_detail=repr(selection_policy), status_code=400)
            if not isinstance(n8n_config, dict):
                return _fail("Invalid n8n config", log_detail=repr(n8n_config), status_code=400)

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

            result = _run_pipeline(tmp_path, selection_policy, n8n_config)
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
        n8n_config = payload.get("n8n", {}) or {}
        if not isinstance(selection_policy, dict):
            return _fail("Invalid selection policy", log_detail=repr(selection_policy), status_code=400)
        if not isinstance(n8n_config, dict):
            return _fail("Invalid n8n config", log_detail=repr(n8n_config), status_code=400)

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

        result = _run_pipeline(tmp_path, selection_policy, n8n_config)
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
            except Exception:
                pass


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"ok": True, "pipeline": PIPELINE_NAME})


@app.route("/doctor", methods=["GET"])
def doctor():
    config = load_config()
    paths = ensure_paths(config)
    checks: list[dict[str, object]] = []

    def _check(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": ok, "detail": detail})

    ffmpeg_path = shutil.which("ffmpeg")
    whisper_path = shutil.which("whisper")
    _check("ffmpeg", bool(ffmpeg_path), ffmpeg_path or "Not found in PATH")
    _check("whisper", bool(whisper_path), whisper_path or "Not found in PATH")
    _check(
        "OPENAI_API_KEY",
        bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "Set" if os.environ.get("OPENAI_API_KEY", "").strip() else "Missing",
    )
    for key, path in paths.items():
        _check(f"path:{key}", os.path.isdir(path), path)

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
    app.run(host="0.0.0.0", port=5050)
