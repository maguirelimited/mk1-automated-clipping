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
    video_duration_sec: float | None = None,
    duration_policy: str = "strict",
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    invalid_time_count = 0
    out_of_duration_count = 0
    out_of_video_count = 0
    if duration_policy == "llm_primary":
        # Safety net only — select_clip prompt enforces [min_duration_sec, max_duration_sec].
        effective_min = 0.5
        effective_max = max(
            max_duration_sec * 1.5,
            min_duration_sec * 25,
            float(min_duration_sec) + float(max_duration_sec),
        )
    else:
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
        if video_duration_sec is not None and (
            start_sec >= video_duration_sec or end_sec > video_duration_sec
        ):
            out_of_video_count += 1
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
            "duration_policy": duration_policy,
            "input_segments": len(segments),
            "candidates_kept": len(candidates),
            "invalid_time_count": invalid_time_count,
            "out_of_duration_count": out_of_duration_count,
            "out_of_video_count": out_of_video_count,
            "below_min_count": below_min_count,
            "above_max_count": above_max_count,
            "min_duration_sec": min_duration_sec,
            "max_duration_sec": max_duration_sec,
            "effective_min_duration_sec": round(effective_min, 3),
            "effective_max_duration_sec": round(effective_max, 3),
            "min_tolerance": min_tolerance,
            "max_tolerance": max_tolerance,
            "video_duration_sec": video_duration_sec,
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


def shift_segments_wallclock(
    segments: list[dict[str, Any]], offset_sec: float
) -> list[dict[str, Any]]:
    """Add offset_sec to every segment start/end timestamp (numeric wall-clock merge)."""
    if offset_sec == 0.0:
        return segments
    shifted: list[dict[str, Any]] = []
    for seg in segments:
        updated = dict(seg)
        try:
            a = parse_time_to_seconds(str(seg.get("start", "")).strip())
            b = parse_time_to_seconds(str(seg.get("end", "")).strip())
        except ValueError:
            shifted.append(seg)
            continue
        na = max(0.0, a + float(offset_sec))
        nb = max(0.0, b + float(offset_sec))
        if nb <= na:
            continue
        updated["start"] = _format_seconds_hhmmss(na)
        updated["end"] = _format_seconds_hhmmss(nb)
        if "duration_sec" in updated:
            try:
                updated["duration_sec"] = round(nb - na, 3)
            except (TypeError, ValueError):
                pass
        shifted.append(updated)
    return shifted


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
    video_duration_sec: float | None = None,
    duration_policy: str = "strict",
    overlap_min_duration_sec: float | None = None,
) -> list[dict[str, Any]]:
    """Validate, sort, trim overlap, and deduplicate candidate segments.

    ``duration_policy``:
      - ``strict`` (default): drop candidates outside tolerant duration window.
      - ``llm_primary``: only drop obviously invalid lengths (safety net); the
        selector LLM is trusted for ``min_duration_sec`` / ``max_duration_sec``.

    ``overlap_min_duration_sec``: when set, overlap trimming must keep at least this
    many seconds (defaults to ``min_duration_sec``).
    """
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
    if duration_policy not in ("strict", "llm_primary"):
        raise ValueError("duration_policy must be 'strict' or 'llm_primary'")

    overlap_min = (
        float(overlap_min_duration_sec)
        if overlap_min_duration_sec is not None
        else float(min_duration_sec)
    )

    candidates = _rank_candidates(
        _prepare_candidates(
            segments,
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
            min_tolerance=min_tolerance,
            max_tolerance=max_tolerance,
            video_duration_sec=video_duration_sec,
            duration_policy=duration_policy,
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
            min_duration_sec=overlap_min,
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


_DEFAULT_PIPELINE_CONFIG_ABS = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "pipeline_config.json")
)
_VIDEO_PROFILES_FILENAME = "video_pipeline_profiles.json"
_RUNTIME_ENV_JSON_VAR = "VIDEO_PIPELINE_RUNTIME_JSON"
_PROFILES_PATH_ENV_VAR = "VIDEO_PIPELINE_PROFILES_PATH"
_MK04_WHISPER_ENV_VAR = "MK04_WHISPER_MODEL"
_MK04_SELECTION_MODEL_ENV_VAR = "MK04_SELECTION_MODEL"

POLICY_MERGE_CHAIN_DOC = (
    "baseline: pipeline_config.json (paths + selection/chunking/models defaults) "
    "< funnel profile referenced by HTTP pipeline_profile|funnel_id OR by "
    "defaults.pipeline_profile in pipeline_config.json (same-named entry under profiles.*) "
    f"< {_RUNTIME_ENV_JSON_VAR} < {_MK04_WHISPER_ENV_VAR} / {_MK04_SELECTION_MODEL_ENV_VAR} "
    "< HTTP `pipeline` object < HTTP `selection` object"
)


def resolved_pipeline_config_path() -> str:
    return os.path.abspath(os.environ.get("PIPELINE_CONFIG_PATH", _DEFAULT_PIPELINE_CONFIG_ABS))


def _profiles_catalog_path(pipeline_config_abs: str) -> str:
    alt = os.environ.get(_PROFILES_PATH_ENV_VAR, "").strip()
    if alt:
        return os.path.abspath(alt)
    return os.path.join(os.path.dirname(os.path.abspath(pipeline_config_abs)), _VIDEO_PROFILES_FILENAME)


def _load_json_profiles_file(path: str) -> tuple[dict[str, Any], str | None]:
    if not path or not os.path.isfile(path):
        return {}, ("missing_profiles_file" if path else "empty_profiles_path")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        raise ValueError(f"profiles file unreadable or invalid JSON: {path}") from None
    if not isinstance(data, dict):
        raise ValueError(f"profiles file must be a JSON object: {path}")
    profiles = data.get("profiles")
    if profiles is None:
        return {}, None
    if not isinstance(profiles, dict):
        raise ValueError(f'"profiles" must be an object in profiles catalog ({path})')
    return profiles, None


def _normalize_selection_shard(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    aliases = {
        "min_clip_duration_sec": "min_duration_sec",
        "max_clip_duration_sec": "max_duration_sec",
    }
    allowed = frozenset(
        {
            "max_clips",
            "min_duration_sec",
            "max_duration_sec",
            "max_overlap_sec",
            "include_reasons",
            "include_clip_metadata",
        }
    )
    for k_old, val in raw.items():
        key = aliases.get(str(k_old), str(k_old))
        if key not in allowed:
            continue
        if val is None:
            continue
        try:
            if key in frozenset({"include_reasons", "include_clip_metadata"}):
                out[key] = bool(val)
            elif key == "max_clips":
                out[key] = int(val)
            else:
                out[key] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def _selection_from_pipeline_config(selection_cfg: Any) -> dict[str, Any]:
    s = selection_cfg if isinstance(selection_cfg, dict) else {}
    min_d_raw = s.get("min_duration_sec")
    if min_d_raw is None:
        min_d_raw = s.get("min_clip_duration_sec", 5)
    max_d_raw = s.get("max_duration_sec")
    if max_d_raw is None:
        max_d_raw = s.get("max_clip_duration_sec", 30)
    return {
        "max_clips": int(s.get("max_clips", 5)),
        "min_duration_sec": float(min_d_raw),
        "max_duration_sec": float(max_d_raw),
        "max_overlap_sec": float(s.get("max_overlap_sec", 2)),
        "include_reasons": bool(s.get("include_reasons", False)),
        "include_clip_metadata": bool(s.get("include_clip_metadata", True)),
    }


def _merge_overlay_with_trace(
    effective: dict[str, Any],
    trace: dict[str, str],
    tag: str,
    overlay_shard: dict[str, Any],
) -> None:
    for k, v in overlay_shard.items():
        effective[k] = v
        trace[k] = tag


def _merge_models_with_trace(
    effective: dict[str, Any],
    trace: dict[str, str],
    tag: str,
    overlay_shard: Any,
) -> None:
    if not isinstance(overlay_shard, dict):
        return
    for key in ("whisper_model", "selection_model"):
        if key not in overlay_shard or overlay_shard[key] is None:
            continue
        v = overlay_shard[key]
        if isinstance(v, str) and not v.strip():
            continue
        effective[key] = str(v).strip() if isinstance(v, str) else str(v)
        trace[key] = tag


def _merge_chunk_with_trace(
    effective: dict[str, Any],
    trace: dict[str, str],
    tag: str,
    overlay_shard: Any,
) -> None:
    if not isinstance(overlay_shard, dict):
        return
    for key in ("enabled", "threshold_sec", "chunk_target_sec", "max_clips_per_chunk"):
        if key not in overlay_shard:
            continue
        v = overlay_shard[key]
        if v is None:
            continue
        if key == "enabled":
            effective[key] = bool(v)
            trace[key] = tag
        elif key == "max_clips_per_chunk":
            try:
                effective[key] = int(v)
                trace[key] = tag
            except (TypeError, ValueError):
                continue
        else:
            try:
                effective[key] = float(v)
                trace[key] = tag
            except (TypeError, ValueError):
                continue


def _finalize_selection(sel: dict[str, Any]) -> dict[str, Any]:
    sel = dict(sel)
    sel["max_clips"] = int(sel["max_clips"])
    sel["min_duration_sec"] = float(sel["min_duration_sec"])
    sel["max_duration_sec"] = float(sel["max_duration_sec"])
    sel["max_overlap_sec"] = float(sel["max_overlap_sec"])
    sel["include_reasons"] = bool(sel.get("include_reasons", False))
    sel["include_clip_metadata"] = bool(sel.get("include_clip_metadata", True))

    if sel["max_clips"] < 1:
        sel["max_clips"] = 1
    if sel["min_duration_sec"] <= 0 or sel["max_duration_sec"] <= 0:
        raise ValueError("Resolved selection duration constraints must be positive")
    if sel["min_duration_sec"] > sel["max_duration_sec"]:
        raise ValueError(
            "Resolved min_duration_sec cannot exceed max_duration_sec — reconcile "
            "pipeline_config.json, profiles, VIDEO_PIPELINE_RUNTIME_JSON, and HTTP overrides."
        )
    if sel["max_overlap_sec"] < 0:
        raise ValueError("Resolved max_overlap_sec cannot be negative")
    return sel


def _audit_keys_matching_source(trace: dict[str, str], source_tag: str) -> list[str]:
    return sorted(k for k, v in trace.items() if v == source_tag)


def _http_runtime_override_audit(
    sel_trace: dict[str, str],
    chunk_trace: dict[str, str],
    models_trace: dict[str, str],
) -> dict[str, object]:
    http_selection = _audit_keys_matching_source(sel_trace, "http_selection")
    pipe_sel = _audit_keys_matching_source(sel_trace, "http_pipeline.selection")
    pipe_chunk = _audit_keys_matching_source(chunk_trace, "http_pipeline.chunking")
    pipe_models = _audit_keys_matching_source(models_trace, "http_pipeline.models")
    return {
        "selection_from_http_body": http_selection,
        "selection_from_http_pipeline_object": pipe_sel,
        "chunking_from_http_pipeline_object": pipe_chunk,
        "models_from_http_pipeline_object": pipe_models,
        "had_any_http_execution_overrides": bool(
            http_selection or pipe_sel or pipe_chunk or pipe_models
        ),
    }


def _infra_env_override_audit(
    sel_trace: dict[str, str], chunk_trace: dict[str, str], models_trace: dict[str, str]
) -> dict[str, list[str]]:
    return {
        "selection": _audit_keys_matching_source(sel_trace, _RUNTIME_ENV_JSON_VAR),
        "chunking": _audit_keys_matching_source(chunk_trace, _RUNTIME_ENV_JSON_VAR),
        "models": _audit_keys_matching_source(models_trace, _RUNTIME_ENV_JSON_VAR),
        "models_whisper_env": _audit_keys_matching_source(models_trace, _MK04_WHISPER_ENV_VAR),
        "models_selection_env": _audit_keys_matching_source(
            models_trace, _MK04_SELECTION_MODEL_ENV_VAR
        ),
    }


def resolve_pipeline_run_policy(
    *,
    pipeline_config_abs: str,
    pipeline_config: dict[str, Any],
    pipeline_profile: str | None = None,
    request_pipeline_blob: dict[str, Any] | None = None,
    request_selection_blob: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a single auditable resolved policy bundle for one HTTP invocation."""

    warnings_list: list[str] = []

    sel_trace: dict[str, str] = {}
    chunk_trace: dict[str, str] = {}
    models_trace: dict[str, str] = {}

    base_sel = _selection_from_pipeline_config(pipeline_config.get("selection"))
    selection_effective = dict(base_sel)
    for k in base_sel:
        sel_trace[k] = "pipeline_config.json"

    base_chunk_raw = pipeline_config.get("chunking")
    chunk_effective: dict[str, Any] = (
        dict(base_chunk_raw) if isinstance(base_chunk_raw, dict) else {}
    )
    for k in chunk_effective.keys():
        chunk_trace[str(k)] = "pipeline_config.json"

    models_eff_dict = pipeline_config.get("models")
    models_eff: dict[str, Any] = dict(models_eff_dict) if isinstance(models_eff_dict, dict) else {}
    models_eff.setdefault("whisper_model", "tiny")
    models_eff.setdefault("selection_model", "gpt-4o-mini")
    for mk in models_eff.keys():
        models_trace[str(mk)] = "pipeline_config.json"

    profiles_catalog, pf_note = _load_json_profiles_file(
        _profiles_catalog_path(pipeline_config_abs)
    )
    pf_path_note = _profiles_catalog_path(pipeline_config_abs)
    if pf_note == "missing_profiles_file":
        warnings_list.append(
            "No profiles catalog file on disk yet — pipeline_profile lookups are ignored."
        )

    pipeline_profile_requested_http = (
        str(pipeline_profile).strip()
        if isinstance(pipeline_profile, str) and pipeline_profile.strip()
        else None
    )
    defaults_obj = pipeline_config.get("defaults")
    config_default_pf: str | None = None
    if isinstance(defaults_obj, dict):
        raw_pf = defaults_obj.get("pipeline_profile")
        if isinstance(raw_pf, str) and raw_pf.strip():
            config_default_pf = raw_pf.strip()

    effective_lookup_name = pipeline_profile_requested_http
    applied_config_default_pf = False
    if effective_lookup_name is None and config_default_pf:
        effective_lookup_name = config_default_pf
        applied_config_default_pf = True

    chosen_profile_name: str | None = None

    profile_root: dict[str, Any] = {}
    if effective_lookup_name:
        node = profiles_catalog.get(effective_lookup_name)
        if not isinstance(node, dict):
            origin = (
                "HTTP funnel hint"
                if pipeline_profile_requested_http
                else "defaults.pipeline_profile in pipeline_config.json"
            )
            warnings_list.append(
                f'Profile {effective_lookup_name!r} missing under "profiles" in {pf_path_note!r} '
                f'(source={origin}).'
            )
            chosen_profile_name = None
        else:
            profile_root = dict(node)
            chosen_profile_name = effective_lookup_name

    _merge_overlay_with_trace(
        selection_effective,
        sel_trace,
        "profile",
        _normalize_selection_shard(profile_root.get("selection")),
    )
    _merge_chunk_with_trace(chunk_effective, chunk_trace, "profile", profile_root.get("chunking"))
    _merge_models_with_trace(models_eff, models_trace, "profile", profile_root.get("models"))

    raw_rt = os.environ.get(_RUNTIME_ENV_JSON_VAR, "").strip()
    if raw_rt:
        try:
            rt_obj = json.loads(raw_rt)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{_RUNTIME_ENV_JSON_VAR} must contain valid JSON when set") from exc
        if not isinstance(rt_obj, dict):
            raise ValueError(f"{_RUNTIME_ENV_JSON_VAR} must deserialize to a JSON object")
        _merge_overlay_with_trace(
            selection_effective,
            sel_trace,
            _RUNTIME_ENV_JSON_VAR,
            _normalize_selection_shard(rt_obj.get("selection")),
        )
        _merge_chunk_with_trace(chunk_effective, chunk_trace, _RUNTIME_ENV_JSON_VAR, rt_obj.get("chunking"))
        _merge_models_with_trace(models_eff, models_trace, _RUNTIME_ENV_JSON_VAR, rt_obj.get("models"))

    ww = os.environ.get(_MK04_WHISPER_ENV_VAR, "").strip()
    if ww:
        _merge_models_with_trace(
            models_eff,
            models_trace,
            _MK04_WHISPER_ENV_VAR,
            {"whisper_model": ww},
        )
    ss = os.environ.get(_MK04_SELECTION_MODEL_ENV_VAR, "").strip()
    if ss:
        _merge_models_with_trace(
            models_eff,
            models_trace,
            _MK04_SELECTION_MODEL_ENV_VAR,
            {"selection_model": ss},
        )

    http_pipe = dict(request_pipeline_blob) if isinstance(request_pipeline_blob, dict) else {}
    _merge_overlay_with_trace(
        selection_effective,
        sel_trace,
        "http_pipeline.selection",
        _normalize_selection_shard(http_pipe.get("selection")),
    )
    _merge_chunk_with_trace(
        chunk_effective,
        chunk_trace,
        "http_pipeline.chunking",
        http_pipe.get("chunking"),
    )
    _merge_models_with_trace(
        models_eff,
        models_trace,
        "http_pipeline.models",
        http_pipe.get("models"),
    )

    merged_req_sel = _normalize_selection_shard(request_selection_blob)
    _merge_overlay_with_trace(selection_effective, sel_trace, "http_selection", merged_req_sel)

    selection_final = _finalize_selection(selection_effective)

    http_execution_audit = _http_runtime_override_audit(sel_trace, chunk_trace, models_trace)
    infra_execution_audit = _infra_env_override_audit(sel_trace, chunk_trace, models_trace)
    infra_env_touch = any(bool(v) for v in infra_execution_audit.values())

    pf_resolve_src = (
        "http"
        if pipeline_profile_requested_http
        else ("config_default" if applied_config_default_pf else "none")
    )

    if http_execution_audit["had_any_http_execution_overrides"]:
        warnings_list.append(
            "HTTP `selection` or `pipeline` supplied execution-layer overrides beyond "
            "repo catalog defaults — inspect `http_execution_overrides` in policy_resolution."
        )
    if infra_env_touch:
        warnings_list.append(
            "Infra env overrides detected (VIDEO_PIPELINE_RUNTIME_JSON / MK04_*) "
            "— pin them beside the deployment manifest for deterministic mk1 clones."
        )

    deterministic_without_http_or_infra_env = (
        not http_execution_audit["had_any_http_execution_overrides"]
        and not infra_env_touch
    )

    audit = {
        "precedence_documentation": POLICY_MERGE_CHAIN_DOC,
        "pipeline_profile_requested_http": pipeline_profile_requested_http,
        "pipeline_profile_apply_config_default_used": applied_config_default_pf,
        "pipeline_profile_config_default_value": config_default_pf,
        "pipeline_profile_resolve_source": pf_resolve_src,
        "pipeline_profile_resolved": chosen_profile_name,
        "deterministic_without_http_or_infra_env": deterministic_without_http_or_infra_env,
        "profiles_catalog_path": pf_path_note,
        "warnings": warnings_list,
        "http_execution_overrides": http_execution_audit,
        "infra_env_overrides_present": infra_execution_audit,
        "selection_resolved": selection_final,
        "selection_key_sources": dict(sel_trace),
        "chunking_resolved": dict(chunk_effective),
        "chunking_key_sources": dict(chunk_trace),
        "models_resolved": dict(models_eff),
        "models_key_sources": dict(models_trace),
    }

    return {
        "selection": selection_final,
        "chunking_effective": dict(chunk_effective),
        "models_effective": dict(models_eff),
        "policy_audit": audit,
    }
