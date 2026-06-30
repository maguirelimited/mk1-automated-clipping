from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any

from pipeline_utils import parse_time_to_seconds

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "pipeline_config.json")

DEFAULT_COVER_GAP_MERGE_SEC = 2.0


def _resolve_cover_gap_merge_sec() -> float:
    """Max silence (seconds) bridged when merging segments into speech blocks.

    Word-aligned engines (WhisperX) emit a real gap at every speech pause, so the
    legacy 0.25s value shattered coverage and rejected normal multi-second clips.
    Bridging up to this many seconds tolerates natural pauses while still breaking
    coverage across genuine silence (the guard against hallucinated timestamps).
    Override with the TRANSCRIPT_COVER_GAP_MERGE_SEC environment variable.
    """
    raw = (os.environ.get("TRANSCRIPT_COVER_GAP_MERGE_SEC") or "").strip()
    if not raw:
        return DEFAULT_COVER_GAP_MERGE_SEC
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_COVER_GAP_MERGE_SEC
    if not math.isfinite(val) or val <= 0:
        return DEFAULT_COVER_GAP_MERGE_SEC
    return val


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pipeline_config_path() -> str:
    """Single source of truth for the active pipeline config file location."""
    return os.environ.get("PIPELINE_CONFIG_PATH", DEFAULT_CONFIG_PATH)


def load_config() -> dict[str, Any]:
    with open(pipeline_config_path(), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg


def save_config_atomic(config: dict[str, Any]) -> str:
    """Persist the full pipeline config atomically (write temp + os.replace).

    Writes to a temp file in the same directory and atomically renames it over
    the target, so the config is never left partially written. Returns the path.
    """
    path = pipeline_config_path()
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".pipeline_config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


def abs_from_project(path_like: str) -> str:
    if os.path.isabs(path_like):
        return os.path.abspath(path_like)
    return os.path.abspath(os.path.join(PROJECT_ROOT, path_like))


def resolve_paths(config: dict[str, Any]) -> dict[str, str]:
    paths = config.get("paths", {})
    return {
        "input": abs_from_project(str(paths.get("input_folder", "input"))),
        "output": abs_from_project(str(paths.get("output_folder", "output"))),
        "temp": abs_from_project(str(paths.get("temp_folder", "temp"))),
        "jobs": abs_from_project(str(paths.get("jobs_folder", "jobs"))),
        "analytics": abs_from_project(str(paths.get("analytics_folder", "analytics"))),
    }


def ensure_paths(config: dict[str, Any]) -> dict[str, str]:
    resolved = resolve_paths(config)
    for path in resolved.values():
        os.makedirs(path, exist_ok=True)
    return resolved


DEFAULT_FFPROBE_TIMEOUT_SEC = 30


def ffprobe_run(
    ffprobe_argv: list[str],
    *,
    timeout_sec: int = DEFAULT_FFPROBE_TIMEOUT_SEC,
) -> subprocess.CompletedProcess[str]:
    """Run ffprobe with consistent capture settings (shared by all probes)."""
    return subprocess.run(
        ["ffprobe", *ffprobe_argv],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )


def ffprobe_duration_sec(path: str, *, timeout_sec: int = DEFAULT_FFPROBE_TIMEOUT_SEC) -> float | None:
    """Return container ``format.duration`` as float, or ``None`` if unreadable."""
    try:
        p = ffprobe_run(
            [
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            timeout_sec=timeout_sec,
        )
        if p.returncode != 0 or not (p.stdout or "").strip():
            return None
        return float((p.stdout or "").strip())
    except Exception:
        return None


def ffprobe_demux_json(path: str, *, timeout_sec: int = DEFAULT_FFPROBE_TIMEOUT_SEC) -> dict[str, Any]:
    """JSON demux probe (format + streams) used for clip output validation."""
    p = ffprobe_run(
        [
            "-v",
            "error",
            "-hide_banner",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ],
        timeout_sec=timeout_sec,
    )
    if p.returncode != 0:
        tail = (p.stderr or "").strip() or (p.stdout or "").strip()
        raise RuntimeError(
            f"CLIP_REJECTED ffprobe_demux_failed: {(tail[:800] + ('…' if len(tail) > 800 else ''))}"
        )
    try:
        data = json.loads(p.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "CLIP_REJECTED ffprobe_json_invalid: demux probe output was not JSON"
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError("CLIP_REJECTED ffprobe_json_invalid: expected JSON object root")
    return data


def resolve_transcribe_engine() -> str:
    """Return transcription backend: ``whisperx`` or legacy ``whisper`` CLI."""
    raw = (os.environ.get("TRANSCRIBE_ENGINE") or "whisper").strip().lower()
    if raw == "whisperx":
        return "whisperx"
    return "whisper"


def resolve_whisper_model_for_transcription(config: dict[str, Any] | None = None) -> str:
    """Resolve Whisper CLI ``--model`` name.

    Precedence: ``WHISPER_MODEL`` environment variable (set by the Flask worker
    from resolved policy) > ``config['models']['whisper_model']`` > ``tiny``.

    Tradeoffs (rule of thumb): ``tiny`` is fastest and lowest resource use but
    weaker transcription and segment timestamps; ``base`` / ``small`` improve
    boundary quality at higher CPU/GPU cost; larger models scale cost steeply.
    """
    env_m = (os.environ.get("WHISPER_MODEL") or "").strip()
    if env_m:
        return env_m
    cfg = config if isinstance(config, dict) else load_config()
    models = cfg.get("models") if isinstance(cfg.get("models"), dict) else {}
    raw = str(models.get("whisper_model") or "").strip()
    return raw or "tiny"


def effective_temp_cleanup_policy(config: dict[str, Any]) -> str:
    """Return ``temp_policy`` for intermediate artifact cleanup.

    - ``default``: remove temp/input scratch files after every run (historical behaviour).
    - ``retain_on_failure``: skip removal when the job ended with ``status=failed``.
    - ``debug_retain_all``: never remove listed scratch artifacts (disk-heavy).
    """
    ar = config.get("artifact_retention")
    if not isinstance(ar, dict):
        return "default"
    raw = str(ar.get("temp_policy") or "default").strip().lower()
    if raw in ("default", "retain_on_failure", "debug_retain_all"):
        return raw
    return "default"


def categorize_error(stage: str, category: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "stage": stage,
        "category": category,
        "message": message,
        "details": details,
        "at": now_iso(),
    }


def _normalize_transcript_word(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    word = str(raw.get("word") or "").strip()
    if not word:
        return None
    try:
        start = float(raw["start"])
        end = float(raw["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if end <= start:
        return None
    out: dict[str, Any] = {"start": start, "end": end, "word": word}
    score = raw.get("score")
    if score is not None:
        try:
            out["score"] = float(score)
        except (TypeError, ValueError):
            pass
    return out


def normalize_transcript_payload(transcript_path: str) -> dict[str, Any]:
    with open(transcript_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    segments_raw = data.get("segments")
    segments: list[dict[str, Any]] = []
    flat_words: list[dict[str, Any]] = []
    if isinstance(segments_raw, list):
        for row in segments_raw:
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
            segment: dict[str, Any] = {"start": start, "end": end, "text": text}
            words_raw = row.get("words")
            if isinstance(words_raw, list):
                segment_words: list[dict[str, Any]] = []
                for word_row in words_raw:
                    word = _normalize_transcript_word(word_row) if isinstance(word_row, dict) else None
                    if word:
                        segment_words.append(word)
                        flat_words.append(dict(word))
                if segment_words:
                    segment["words"] = segment_words
            segments.append(segment)

    top_words_raw = data.get("words")
    if isinstance(top_words_raw, list) and not flat_words:
        for row in top_words_raw:
            word = _normalize_transcript_word(row) if isinstance(row, dict) else None
            if word:
                flat_words.append(word)

    transcript_text = str(data.get("text") or "").strip()
    if not transcript_text and segments:
        transcript_text = " ".join(s["text"] for s in segments if s["text"]).strip()

    duration = data.get("duration")
    duration_sec: float | None = None
    try:
        if duration is not None:
            duration_sec = float(duration)
    except (TypeError, ValueError):
        duration_sec = None
    if duration_sec is None and segments:
        duration_sec = max(float(seg["end"]) for seg in segments)

    payload: dict[str, Any] = {
        "full_text": transcript_text,
        "segments": segments,
        "source_transcript_path": os.path.abspath(transcript_path),
        "duration_sec": duration_sec,
    }
    engine = str(data.get("engine") or "").strip()
    if engine:
        payload["engine"] = engine
    language = str(data.get("language") or "").strip()
    if language:
        payload["language"] = language
    if flat_words:
        payload["words"] = flat_words
    return payload


def merged_transcript_cover_regions(
    transcript_payload: dict[str, Any], *, gap_merge_sec: float = DEFAULT_COVER_GAP_MERGE_SEC
) -> list[tuple[float, float]]:
    """Merge Whisper segment timelines into contiguous cover intervals (speech blocks).

    Gaps narrower than gap_merge_sec are bridged — natural speech pauses and
    punctuation splits. Wider gaps break coverage so hallucinated timestamps across
    silence are rejected later.
    """
    raw = transcript_payload.get("segments") or []
    intervals: list[tuple[float, float]] = []
    if not isinstance(raw, list):
        return []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            s = float(row["start"])  # type: ignore[arg-type]
            e = float(row["end"])  # type: ignore[arg-type]
        except (KeyError, TypeError, ValueError):
            continue
        if e <= s:
            continue
        intervals.append((s, e))
    intervals.sort(key=lambda x: x[0])
    if not intervals:
        return []

    merged: list[list[float]] = [[intervals[0][0], intervals[0][1]]]
    for s, e in intervals[1:]:
        if s > merged[-1][1] + gap_merge_sec:
            merged.append([s, e])
        else:
            merged[-1][1] = max(merged[-1][1], e)
    return [(float(a), float(b)) for a, b in merged]


def clip_inside_transcript_cover(
    start_sec: float,
    end_sec: float,
    regions: list[tuple[float, float]],
    *,
    eps: float = 1e-3,
) -> bool:
    if end_sec <= start_sec:
        return False
    for rs, re in regions:
        if rs - eps <= start_sec and end_sec <= re + eps:
            return True
    return False


def require_timed_transcript_payload(transcript_payload: dict[str, Any]) -> None:
    """Raise if we cannot derive timestamped Whisper segments — never fall back silently."""
    segments = transcript_payload.get("segments")
    if not isinstance(segments, list) or len(segments) == 0:
        raise ValueError(
            "TIMESTAMP_TRANSCRIPT_REJECTED no_usable_segments: Whisper JSON contained no timed "
            "segments (segments[] missing or empty after normalization)."
        )


def validate_and_repair_selection(
    segments: list[dict[str, Any]],
    *,
    transcript_payload: dict[str, Any],
    video_duration_sec: float,
    min_duration_sec: float,
    max_duration_sec: float,
    min_tolerance: float = 0.7,
    max_tolerance: float = 1.3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    effective_min = max(0.1, min_duration_sec * min_tolerance)
    effective_max = max_duration_sec * max_tolerance

    if not math.isfinite(video_duration_sec) or video_duration_sec <= 0:
        fatal = categorize_error(
            "selection_validation",
            "timestamp_error",
            "Video duration invalid or unavailable — refusing to validate clip timestamps.",
            {"video_duration_sec": video_duration_sec},
        )
        return [], [fatal]

    cover_regions = merged_transcript_cover_regions(
        transcript_payload, gap_merge_sec=_resolve_cover_gap_merge_sec()
    )
    if not cover_regions:
        fatal = categorize_error(
            "selection_validation",
            "timestamp_error",
            "Transcript has no mergeable Whisper time coverage — cannot validate clip timestamps deterministically.",
            {"segment_count": len(transcript_payload.get("segments") or [])},
        )
        return [], [fatal]

    for idx, seg in enumerate(segments, start=1):
        start_txt = str(seg.get("start", "")).strip()
        end_txt = str(seg.get("end", "")).strip()
        if not start_txt or not end_txt:
            issues.append(categorize_error("selection_validation", "timestamp_error", "Missing start/end", {"index": idx}))
            continue
        try:
            start_sec = parse_time_to_seconds(start_txt)
            end_sec = parse_time_to_seconds(end_txt)
        except ValueError as e:
            issues.append(categorize_error("selection_validation", "timestamp_error", "Unparseable timestamp", {"index": idx, "error": str(e)}))
            continue

        if start_sec >= end_sec:
            issues.append(categorize_error("selection_validation", "timestamp_error", "start must be before end", {"index": idx, "start": start_txt, "end": end_txt}))
            continue

        duration = end_sec - start_sec
        if duration < effective_min or duration > effective_max:
            issues.append(categorize_error("selection_validation", "timestamp_error", "Clip duration out of range", {"index": idx, "duration_sec": duration, "effective_min_duration_sec": round(effective_min, 3), "effective_max_duration_sec": round(effective_max, 3)}))
            continue

        if start_sec < 0 or end_sec > video_duration_sec:
            issues.append(categorize_error("selection_validation", "timestamp_error", "Clip timestamps exceed source video duration", {"index": idx, "video_duration_sec": video_duration_sec}))
            continue

        if not clip_inside_transcript_cover(start_sec, end_sec, cover_regions):
            issues.append(
                categorize_error(
                    "selection_validation",
                    "timestamp_error",
                    "Clip interval is not contained in Whisper transcript time coverage.",
                    {"index": idx, "start_sec": start_sec, "end_sec": end_sec},
                )
            )
            continue

        grounded = dict(seg)
        grounded["start"] = _format_hhmmss(start_sec)
        grounded["end"] = _format_hhmmss(end_sec)
        grounded["duration_sec"] = round(end_sec - start_sec, 3)
        valid.append(grounded)
    return valid, issues


def create_job_paths(
    config: dict[str, Any], video_path: str, *, job_id: str | None = None
) -> dict[str, str]:
    paths = ensure_paths(config)
    video_name = os.path.basename(video_path)
    stem = os.path.splitext(video_name)[0]
    if job_id is None:
        job_id = f"job_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    job_dir = os.path.join(paths["jobs"], f"{stem}_{job_id}")
    clips_dir = os.path.join(job_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)
    return {
        "job_id": job_id,
        "job_dir": job_dir,
        "clips_dir": clips_dir,
        "input_copy_path": os.path.join(job_dir, f"input_{video_name}"),
        "transcript_copy_path": os.path.join(job_dir, "transcript.json"),
        "normalized_transcript_path": os.path.join(job_dir, "transcript_payload.json"),
        "selection_path": os.path.join(job_dir, "selection.json"),
        "transcript_sections_path": os.path.join(job_dir, "transcript_sections.json"),
        "section_candidate_discovery_path": os.path.join(
            job_dir, "section_candidate_discovery.json"
        ),
        "raw_candidate_pool_path": os.path.join(job_dir, "raw_candidate_pool.json"),
        "processing_report_path": os.path.join(job_dir, "processing_report.json"),
        "report_path": os.path.join(job_dir, "report.json"),
        "task_path": os.path.join(job_dir, "task.json"),
        "analytics_path": os.path.join(job_dir, "analytics.json"),
        "review_path": os.path.join(job_dir, "review.md"),
    }


def maybe_copy(src: str, dst: str) -> None:
    if src and os.path.isfile(src):
        shutil.copy2(src, dst)


def build_funnel_job_record(
    *,
    funnel_ops: dict[str, Any] | None,
    resolved_selection: dict[str, Any],
    policy_audit: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Shape funnel fields for ``report.json`` / API (None when no content funnel was applied)."""
    if not isinstance(funnel_ops, dict):
        return None
    fid = funnel_ops.get("funnel_id")
    if not isinstance(fid, str) or not fid.strip():
        return None
    audit = policy_audit if isinstance(policy_audit, dict) else {}
    fr = audit.get("funnel_resolution") if isinstance(audit.get("funnel_resolution"), dict) else {}
    platforms = funnel_ops.get("platforms")
    enabled_platforms: list[str] = []
    if isinstance(platforms, dict):
        enabled_platforms = sorted(k for k, v in platforms.items() if v is True)
    out = funnel_ops.get("output") if isinstance(funnel_ops.get("output"), dict) else {}
    res_sel = {
        "max_clips": int(resolved_selection["max_clips"]),
        "min_duration_sec": float(resolved_selection["min_duration_sec"]),
        "max_duration_sec": float(resolved_selection["max_duration_sec"]),
        "max_overlap_sec": float(resolved_selection["max_overlap_sec"]),
        "include_reasons": bool(resolved_selection.get("include_reasons", False)),
        "include_clip_metadata": bool(resolved_selection.get("include_clip_metadata", True)),
    }
    res_out = {
        "filename_prefix": str(out.get("filename_prefix", "") or ""),
        "delivery_mode": str(out.get("delivery_mode", "") or "pull_from_output_endpoint"),
    }
    policy_summary: dict[str, Any] = {
        "funnel_resolve_source": fr.get("funnel_resolve_source"),
        "funnel_config_applied": fr.get("funnel_config_applied"),
        "funnel_config_path": fr.get("funnel_config_path"),
        "pipeline_profile_resolved": audit.get("pipeline_profile_resolved"),
        "selection_key_sources": audit.get("selection_key_sources"),
    }
    return {
        "funnel_id": fid.strip(),
        "funnel_name": str(funnel_ops.get("funnel_name") or "").strip() or None,
        "enabled_platforms": enabled_platforms,
        "platforms": dict(platforms) if isinstance(platforms, dict) else {},
        "resolved_selection": res_sel,
        "resolved_output": res_out,
        "funnel_policy_summary": policy_summary,
    }


def write_json(path: str, payload: Any, *, sort_keys: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=os.path.dirname(path),
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
    ) as f:
        json.dump(payload, f, indent=2, sort_keys=sort_keys)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        tmp_path = f.name
    os.replace(tmp_path, path)


def write_review(path: str, report: dict[str, Any]) -> None:
    lines = [
        f"# Job Review: {report.get('job_id', '')}",
        "",
        f"- Source Video: `{report.get('input_video_name', '')}`",
        f"- Job ID: `{report.get('job_id', '')}`",
        f"- Status: `{report.get('status', '')}`",
        "",
    ]
    funnel = report.get("funnel")
    if isinstance(funnel, dict) and funnel.get("funnel_id"):
        lines.extend(
            [
                "## Funnel",
                "",
                f"- **funnel_id:** `{funnel.get('funnel_id', '')}`",
                f"- **funnel_name:** {funnel.get('funnel_name') or '—'}",
                f"- **enabled_platforms:** {', '.join(funnel.get('enabled_platforms') or []) or '—'}",
                "",
                "### Resolved selection",
                "",
            ]
        )
        rs = funnel.get("resolved_selection") or {}
        if isinstance(rs, dict):
            lines.extend(
                [
                    f"- max_clips: `{rs.get('max_clips')}`",
                    f"- min_duration_sec: `{rs.get('min_duration_sec')}`",
                    f"- max_duration_sec: `{rs.get('max_duration_sec')}`",
                    f"- max_overlap_sec: `{rs.get('max_overlap_sec')}`",
                    f"- include_clip_metadata: `{rs.get('include_clip_metadata')}`",
                    f"- include_reasons: `{rs.get('include_reasons')}`",
                    "",
                ]
            )
        ro = funnel.get("resolved_output") or {}
        if isinstance(ro, dict):
            lines.extend(
                [
                    "### Resolved output",
                    "",
                    f"- filename_prefix: `{ro.get('filename_prefix', '') or '—'}`",
                    f"- delivery_mode: `{ro.get('delivery_mode', '')}`",
                    "",
                ]
            )
        summ = funnel.get("funnel_policy_summary") or {}
        if isinstance(summ, dict):
            if summ.get("funnel_resolve_source"):
                lines.append(f"- **resolve_source:** `{summ.get('funnel_resolve_source')}`")
            if summ.get("funnel_config_path"):
                lines.append(f"- **funnel_config_path:** `{summ.get('funnel_config_path')}`")
            lines.append("")
    lines.extend(
        [
            "## Selected Clips",
        ]
    )
    clips = report.get("clips") or []
    if clips:
        for idx, clip in enumerate(clips, start=1):
            lines.extend(
                [
                    f"- Clip {idx}: `{clip.get('start')}` -> `{clip.get('end')}` ({clip.get('duration_sec')}s)",
                    f"  - Path: `{clip.get('clip_path')}`",
                ]
            )
            if clip.get("reason"):
                lines.append(f"  - Reason: {clip.get('reason')}")
            if clip.get("composite_score") is not None:
                lines.append(f"  - Score: {clip.get('composite_score')}")
    else:
        lines.append("- None")

    warnings = report.get("warnings") or []
    if warnings:
        lines.extend(["", "## Warnings/Errors"])
        for warning in warnings:
            lines.append(
                f"- [{warning.get('category', 'error')}] {warning.get('message', '')}"
            )

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _format_hhmmss(seconds: float) -> str:
    whole = int(seconds)
    h = whole // 3600
    m = (whole % 3600) // 60
    s = whole % 60
    frac = round(seconds - whole, 3)
    if frac <= 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    ms = int(round(frac * 1000))
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
