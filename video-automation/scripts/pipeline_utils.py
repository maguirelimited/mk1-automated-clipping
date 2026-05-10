"""Shared helpers for clip pipeline parsing and segment post-processing."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

DEBUG_MODE_LOG_PATH = os.environ.get("DEBUG_MODE_LOG_PATH", "").strip()
DEBUG_MODE_SESSION_ID = "c9492c"


def _debug_mode_log(hypothesis_id: str, location: str, message: str, data: dict):
    if not DEBUG_MODE_LOG_PATH:
        return
    payload = {
        "sessionId": DEBUG_MODE_SESSION_ID,
        "runId": "pipeline-utils",
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


def parse_time_to_seconds(s: str) -> float:
    """Parse HH:MM:SS, MM:SS, or SS (and optional fractional seconds) to seconds."""
    t = s.strip()
    if not t:
        raise ValueError("Empty time string")
    if re.match(r"^\d+(\.\d+)?$", t):
        return float(t)
    parts = t.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"Invalid time format: {s!r}") from e
    if len(nums) == 3:
        h, m, sec = nums
        return h * 3600 + m * 60 + sec
    if len(nums) == 2:
        m, sec = nums
        return m * 60 + sec
    raise ValueError(f"Invalid time format: {s!r}")


def validate_segment_times(start: str, end: str) -> tuple[float, float]:
    """Ensure start and end parse and start < end. Returns (start_sec, end_sec)."""
    a = parse_time_to_seconds(start)
    b = parse_time_to_seconds(end)
    if a >= b:
        raise ValueError(f"start must be before end (got start={start!r}, end={end!r})")
    return a, b


def extract_json_object(text: str) -> str:
    """Take the first top-level JSON object substring from text (handles extra prose)."""
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in model output")
    depth = 0
    in_string = False
    escape = False
    quote: str | None = None
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_string = False
                quote = None
            continue
        if ch in "\"'":
            in_string = True
            quote = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("Unbalanced braces in JSON object")


def extract_json_array(text: str) -> str:
    """Take the first top-level JSON array substring from text (handles extra prose)."""
    start = text.find("[")
    if start == -1:
        raise ValueError("No JSON array found in model output")
    depth = 0
    in_string = False
    escape = False
    quote: str | None = None
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_string = False
                quote = None
            continue
        if ch in "\"'":
            in_string = True
            quote = ch
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("Unbalanced brackets in JSON array")


def parse_selection_payload(raw: str) -> Any:
    """Parse selection stdout: JSON object, array, or fenced / prose-wrapped JSON."""
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        probe = cleaned or raw
        array_index = probe.find("[")
        object_index = probe.find("{")
        try_array_first = array_index != -1 and (
            object_index == -1 or array_index < object_index
        )
        extractors = (
            (extract_json_array, extract_json_object)
            if try_array_first
            else (extract_json_object, extract_json_array)
        )
        last_error: Exception | None = None
        for extractor in extractors:
            try:
                fragment = extractor(probe)
                return json.loads(fragment)
            except (ValueError, json.JSONDecodeError) as e:
                last_error = e
        raise ValueError("Could not parse selection payload as JSON") from last_error


_SCORE_KEYS = (
    "hook_strength",
    "clarity_standalone",
    "engagement_potential",
    "minimal_filler",
)


def score_dimensions_from_segment(segment: dict[str, Any]) -> dict[str, float]:
    """Normalize model score dimensions to floats in 0..10 (empty if none valid)."""
    raw = segment.get("scores")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k in _SCORE_KEYS:
        v = raw.get(k)
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if 0.0 <= x <= 10.0:
            out[k] = x
    return out


def composite_clip_score(scores: dict[str, float]) -> float | None:
    """Weighted composite for ranking (no extra LLM). Returns None if no scores."""
    if not scores:
        return None
    h = scores.get("hook_strength", 5.0)
    c = scores.get("clarity_standalone", 5.0)
    e = scores.get("engagement_potential", 5.0)
    f = scores.get("minimal_filler", 5.0)
    return 0.35 * h + 0.30 * e + 0.25 * c + 0.10 * f


def normalize_segments(payload: Any) -> list[dict[str, Any]]:
    """Normalize selector output into a list of segment dicts."""
    if isinstance(payload, dict):
        inner = payload.get("clips")
        if isinstance(inner, list):
            payload = inner
        elif "start" in payload and "end" in payload:
            payload = [payload]
        else:
            raise ValueError("Selection object must include 'clips' array or start/end times")
    if not isinstance(payload, list) or not payload:
        raise ValueError("Selection payload must be a non-empty object or list")

    normalized: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Each segment must be an object")
        if "start" not in item or "end" not in item:
            raise ValueError("Each segment must include 'start' and 'end'")

        start_raw = str(item["start"]).strip()
        end_raw = str(item["end"]).strip()
        try:
            start_norm = _format_seconds_hhmmss(parse_time_to_seconds(start_raw))
            end_norm = _format_seconds_hhmmss(parse_time_to_seconds(end_raw))
        except ValueError:
            start_norm = start_raw
            end_norm = end_raw

        segment: dict[str, Any] = {
            "start": start_norm,
            "end": end_norm,
        }
        reason = item.get("reason")
        if reason is None:
            reason = item.get("selection_reason")
        if reason is not None and str(reason).strip():
            segment["reason"] = str(reason).strip()

        for meta_key in ("title", "hook", "caption"):
            val = item.get(meta_key)
            if val is not None and str(val).strip():
                segment[meta_key] = str(val).strip()

        raw_scores = item.get("scores")
        if isinstance(raw_scores, dict):
            segment["scores"] = dict(raw_scores)

        if "score" in item and item["score"] is not None:
            try:
                segment["score"] = float(item["score"])
            except (TypeError, ValueError):
                pass
        normalized.append(segment)
    return normalized


def _format_seconds_hhmmss(seconds: float) -> str:
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


def _prepare_candidates(
    segments: list[dict[str, Any]],
    *,
    min_duration_sec: float,
    max_duration_sec: float,
    min_tolerance: float,
    max_tolerance: float,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    invalid_time_count = 0
    out_of_duration_count = 0
    effective_min = max(0.1, min_duration_sec * min_tolerance)
    effective_max = max_duration_sec * max_tolerance
    below_min_count = 0
    above_max_count = 0
    for segment in segments:
        start = str(segment["start"]).strip()
        end = str(segment["end"]).strip()
        try:
            start_sec, end_sec = validate_segment_times(start, end)
        except ValueError:
            invalid_time_count += 1
            continue
        duration = end_sec - start_sec
        if duration < effective_min:
            out_of_duration_count += 1
            below_min_count += 1
            continue
        if duration > effective_max:
            out_of_duration_count += 1
            above_max_count += 1
            continue
        row = dict(segment)
        row["_start_sec"] = start_sec
        row["_end_sec"] = end_sec
        row["_duration_sec"] = duration
        candidates.append(row)
    # #region agent log
    _debug_mode_log(
        "H6-overfiltered-postprocess",
        "pipeline_utils.py:_prepare_candidates",
        "candidate filtering summary",
        {
            "input_segments": len(segments),
            "candidates_kept": len(candidates),
            "invalid_time_count": invalid_time_count,
            "out_of_duration_count": out_of_duration_count,
            "below_min_count": below_min_count,
            "above_max_count": above_max_count,
            "min_duration_sec": min_duration_sec,
            "max_duration_sec": max_duration_sec,
            "effective_min_duration_sec": round(effective_min, 3),
            "effective_max_duration_sec": round(effective_max, 3),
            "min_tolerance": min_tolerance,
            "max_tolerance": max_tolerance,
        },
    )
    # #endregion
    return candidates


def _rank_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _rank_key(s: dict[str, Any]) -> tuple[float, float, float]:
        dims = score_dimensions_from_segment(s)
        comp = composite_clip_score(dims)
        rank = -(comp if comp is not None else 0.0)
        return (rank, float(s["_start_sec"]), float(s["_end_sec"]))

    return sorted(candidates, key=_rank_key)


def _is_duplicate_segment(
    segment: dict[str, Any],
    accepted: list[dict[str, Any]],
    *,
    dedupe_threshold_sec: float,
) -> bool:
    for existing in accepted:
        start_delta = abs(float(segment["_start_sec"]) - float(existing["_start_sec"]))
        end_delta = abs(float(segment["_end_sec"]) - float(existing["_end_sec"]))
        if start_delta <= dedupe_threshold_sec and end_delta <= dedupe_threshold_sec:
            return True
    return False


def _overlap_excess(segment: dict[str, Any], accepted: list[dict[str, Any]]) -> float:
    if not accepted:
        return 0.0
    segment_start = float(segment["_start_sec"])
    max_end = max(float(item["_end_sec"]) for item in accepted)
    return max(0.0, max_end - segment_start)


def _apply_overlap_limit(
    segment: dict[str, Any],
    accepted: list[dict[str, Any]],
    *,
    max_overlap_sec: float,
    min_duration_sec: float,
) -> dict[str, Any] | None:
    if not accepted:
        return segment
    overlap = _overlap_excess(segment, accepted)
    if overlap <= max_overlap_sec:
        return segment

    max_end = max(float(item["_end_sec"]) for item in accepted)
    adjusted_start = max_end - max_overlap_sec
    if adjusted_start >= float(segment["_end_sec"]):
        return None

    updated = dict(segment)
    updated["_start_sec"] = adjusted_start
    updated["_duration_sec"] = float(updated["_end_sec"]) - adjusted_start
    if updated["_duration_sec"] < min_duration_sec:
        return None
    updated["start"] = _format_seconds_hhmmss(adjusted_start)
    return updated


def _finalize_segments(filtered: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for segment in filtered:
        clean = {k: v for k, v in segment.items() if not k.startswith("_")}
        clean["duration_sec"] = round(float(segment["_duration_sec"]), 3)
        dims = score_dimensions_from_segment(segment)
        comp = composite_clip_score(dims)
        if dims:
            clean["scores"] = {k: round(dims[k], 1) for k in _SCORE_KEYS if k in dims}
        if comp is not None:
            clean["composite_score"] = round(comp, 2)
        output.append(clean)
    return output


def postprocess_segments(
    segments: list[dict[str, Any]],
    *,
    max_clips: int = 5,
    min_duration_sec: float = 10.0,
    max_duration_sec: float = 30.0,
    max_overlap_sec: float = 2.0,
    dedupe_threshold_sec: float = 2.0,
    min_tolerance: float = 0.7,
    max_tolerance: float = 1.3,
) -> list[dict[str, Any]]:
    """Validate, sort, trim overlap, and deduplicate candidate segments."""
    if max_clips < 1:
        raise ValueError("max_clips must be >= 1")
    if min_duration_sec <= 0 or max_duration_sec <= 0:
        raise ValueError("duration constraints must be positive")
    if min_duration_sec > max_duration_sec:
        raise ValueError("min_duration_sec cannot exceed max_duration_sec")
    if max_overlap_sec < 0:
        raise ValueError("max_overlap_sec cannot be negative")
    if min_tolerance <= 0 or max_tolerance <= 0:
        raise ValueError("duration tolerances must be positive")

    candidates = _rank_candidates(
        _prepare_candidates(
            segments,
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
            min_tolerance=min_tolerance,
            max_tolerance=max_tolerance,
        )
    )
    filtered: list[dict[str, Any]] = []
    for segment in candidates:
        if _is_duplicate_segment(
            segment, filtered, dedupe_threshold_sec=dedupe_threshold_sec
        ):
            continue

        adjusted = _apply_overlap_limit(
            segment,
            filtered,
            max_overlap_sec=max_overlap_sec,
            min_duration_sec=min_duration_sec,
        )
        if adjusted is None:
            continue

        filtered.append(adjusted)
        if len(filtered) >= max_clips:
            break

    return _finalize_segments(filtered)


def make_script_success(script: str, **payload: Any) -> str:
    body = {"ok": True, "script": script}
    body.update(payload)
    return json.dumps(body)


def make_script_error(script: str, message: str, **payload: Any) -> str:
    body = {"ok": False, "script": script, "error": message}
    body.update(payload)
    return json.dumps(body)
