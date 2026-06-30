"""intelligent_captions_v1 — third module in the fixed MK1 universal conveyor.

Takes the platform-safe 9:16 clip from platform_safe_format_v1 and burns
deterministic timed captions into a captioned output video.

Caption pipeline:
    platform-safe 9:16 clip
        ↓
    resolve transcript/word timing from existing artifacts
        ↓
    chunk into readable caption blocks
        ↓
    generate ASS subtitle sidecar file
        ↓
    burn captions with ffmpeg subtitles filter
        ↓
    captioned platform-safe clip

This module deliberately does NOT:
- perform 9:16 formatting or re-encode for platform format
- rerun WhisperX transcription
- call AI/LLM services
- implement final validation (that is validation_v1's responsibility)
- write per-clip metadata files or post_processing_report.json
- register output funnels
- perform face/object tracking, creative reframing, or audio normalisation
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from typing import Any

from post_processing_modules import (
    PostProcessingModule,
    make_module_fail_result,
    make_module_pass_result,
)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

MODULE_NAME = "intelligent_captions_v1"
MODULE_VERSION = "1.0"

FFMPEG_TIMEOUT_SEC = 180
FFPROBE_TIMEOUT_SEC = 30

# ---------------------------------------------------------------------------
# Default config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: dict[str, Any] = {
    "output_ext": ".mp4",
    "subtitle_format": "ass",
    "font_family": "Arial",
    "font_size": 64,
    "font_bold": True,
    "font_color": "white",
    "outline_color": "black",
    "outline_width": 4,
    "shadow": 1,
    "max_lines": 2,
    "max_chars_per_line": 32,
    "max_chars_per_caption": 42,
    "min_caption_duration_sec": 0.45,
    "max_caption_duration_sec": 2.2,
    "duration_tolerance_sec": 1.0,
    "safe_zone_top_px": 180,
    "safe_zone_bottom_px": 320,
    "safe_zone_left_px": 80,
    "safe_zone_right_px": 80,
    "caption_y_px": None,
    "enable_keyword_highlighting": False,
    "highlight_words": [],
    "highlight_numbers": False,
    "overwrite": True,
    "ffmpeg_preset": "veryfast",
    "video_codec": "libx264",
    "audio_codec": "aac",
}

# ---------------------------------------------------------------------------
# Public module class
# ---------------------------------------------------------------------------


class IntelligentCaptionsV1Module(PostProcessingModule):
    """Real MK1 intelligent captions module.

    Burns deterministic timed captions from existing transcript artifacts into
    the platform-safe 9:16 clip produced by platform_safe_format_v1.

    Plugs directly into :func:`run_module_chain` and
    :func:`run_fixed_mk1_universal_conveyor` as the third module.
    """

    module_name = MODULE_NAME
    module_version = MODULE_VERSION

    def run(
        self,
        context: dict[str, Any],
        *,
        input_path: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged_config = {**_DEFAULT_CONFIG, **(config or {}), **(context.get("config") or {})}

        # Best-effort candidate_id for early failure results
        candidate_id: str | None = _extract_candidate_id(context)

        # ------------------------------------------------------------------
        # 1. Validate config
        # ------------------------------------------------------------------
        config_err = _validate_caption_config(merged_config)
        if config_err:
            return _fail(
                "invalid_caption_config",
                config_err,
                candidate_id=candidate_id,
                input_path=input_path,
            )

        # ------------------------------------------------------------------
        # 2. Validate input path / file
        # ------------------------------------------------------------------
        if not input_path or not str(input_path).strip():
            return _fail(
                "missing_input_path",
                "input_path is missing or empty",
                candidate_id=candidate_id,
                input_path=input_path,
            )
        input_path = str(input_path)

        if not os.path.exists(input_path):
            return _fail(
                "input_file_not_found",
                f"input file does not exist: {input_path}",
                candidate_id=candidate_id,
                input_path=input_path,
            )
        if not os.path.isfile(input_path):
            return _fail(
                "input_path_not_file",
                f"input path is not a regular file: {input_path}",
                candidate_id=candidate_id,
                input_path=input_path,
            )
        if os.path.getsize(input_path) == 0:
            return _fail(
                "input_file_empty",
                f"input file is empty: {input_path}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        # ------------------------------------------------------------------
        # 3. Probe input
        # ------------------------------------------------------------------
        try:
            input_info = _probe_video_info(input_path)
        except Exception as exc:
            return _fail(
                "input_probe_failed",
                f"could not probe input file: {exc}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        if input_info is None:
            return _fail(
                "input_probe_failed",
                f"ffprobe returned no usable information for: {input_path}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        if input_info["width"] <= 0 or input_info["height"] <= 0:
            return _fail(
                "missing_video_stream",
                (
                    f"input has no valid video stream "
                    f"(probed w={input_info['width']} h={input_info['height']})"
                ),
                candidate_id=candidate_id,
                input_path=input_path,
            )

        input_w = input_info["width"]
        input_h = input_info["height"]
        input_duration = input_info["duration_sec"]
        input_has_audio = input_info["has_audio"]

        # ------------------------------------------------------------------
        # 4. Validate selected_candidate
        # ------------------------------------------------------------------
        selected = context.get("selected_candidate")
        if not isinstance(selected, dict) or not selected:
            return _fail(
                "missing_selected_candidate",
                "context is missing a valid selected_candidate dict",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        selected_candidate_id = selected.get("candidate_id")
        if not selected_candidate_id or not str(selected_candidate_id).strip():
            return _fail(
                "missing_candidate_id",
                "selected_candidate is missing a non-empty candidate_id",
                candidate_id=candidate_id,
                input_path=input_path,
            )
        candidate_id = str(selected_candidate_id)

        start_sec = selected.get("start_sec")
        end_sec = selected.get("end_sec")

        if start_sec is None or end_sec is None:
            return _fail(
                "missing_candidate_timestamps",
                "selected_candidate is missing start_sec or end_sec",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        if not _is_finite_float(start_sec) or not _is_finite_float(end_sec):
            return _fail(
                "invalid_candidate_timestamps",
                f"start_sec or end_sec is not a valid finite number: "
                f"start_sec={start_sec!r}, end_sec={end_sec!r}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        start_sec = float(start_sec)
        end_sec = float(end_sec)

        if start_sec < 0 or end_sec <= start_sec:
            return _fail(
                "invalid_candidate_timestamps",
                f"invalid timestamp range: start_sec={start_sec}, end_sec={end_sec}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        # ------------------------------------------------------------------
        # 5. Resolve caption data
        # ------------------------------------------------------------------
        caption_source = _resolve_caption_source(context, selected, merged_config)
        if caption_source is None:
            return _fail(
                "missing_caption_data",
                "no usable transcript/caption timing data found in context or config",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        source_type, source_data = caption_source

        # ------------------------------------------------------------------
        # 6. Generate caption chunks
        # ------------------------------------------------------------------
        try:
            if source_type == "words":
                chunks = _chunk_from_words(source_data, start_sec, end_sec, merged_config)
            else:
                chunks = _chunk_from_segments(source_data, start_sec, end_sec, merged_config)
        except Exception as exc:
            return _fail(
                "unexpected_caption_error",
                f"exception during caption chunking: {exc}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        if not chunks:
            return _fail(
                "missing_caption_data",
                "caption chunking produced no usable captions within the candidate range",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        # Validate all chunk timings and text
        for i, chunk in enumerate(chunks):
            if not _is_finite_float(chunk.get("start_sec")) or not _is_finite_float(chunk.get("end_sec")):
                return _fail(
                    "invalid_caption_timing",
                    f"chunk {i} has non-finite timing: {chunk.get('start_sec')!r}, {chunk.get('end_sec')!r}",
                    candidate_id=candidate_id,
                    input_path=input_path,
                )
            if float(chunk["end_sec"]) <= float(chunk["start_sec"]):
                return _fail(
                    "invalid_caption_timing",
                    f"chunk {i} has end_sec ({chunk['end_sec']}) <= start_sec ({chunk['start_sec']})",
                    candidate_id=candidate_id,
                    input_path=input_path,
                )
            if not chunk.get("lines") or not any(str(ln).strip() for ln in chunk["lines"]):
                return _fail(
                    "empty_caption_text",
                    f"chunk {i} has empty or blank caption text",
                    candidate_id=candidate_id,
                    input_path=input_path,
                )

        # ------------------------------------------------------------------
        # 7. Resolve output paths
        # ------------------------------------------------------------------
        job_id: str = str(context.get("job_id") or "job_unknown")
        clip_dir: str = str(
            context.get("clip_dir") or os.path.join(os.path.dirname(input_path), "captioned")
        )
        tmp_dir: str = str(context.get("tmp_dir") or clip_dir)

        output_path = _make_output_path(
            clip_dir,
            job_id,
            candidate_id,
            ext=str(merged_config.get("output_ext", ".mp4")),
        )
        sidecar_path = _make_sidecar_path(tmp_dir, job_id, candidate_id)

        overwrite = bool(merged_config.get("overwrite", True))
        if os.path.exists(output_path) and not overwrite:
            return _fail(
                "output_exists",
                f"output file already exists and overwrite=false: {output_path}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        try:
            os.makedirs(clip_dir, exist_ok=True)
            os.makedirs(tmp_dir, exist_ok=True)
        except OSError as exc:
            return _fail(
                "unexpected_caption_error",
                f"could not create output directories: {exc}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        # ------------------------------------------------------------------
        # 8. Resolve safe zones
        # ------------------------------------------------------------------
        safe_zones, _target_w, _target_h = _resolve_safe_zones(context, merged_config)

        # ------------------------------------------------------------------
        # 9. Generate ASS sidecar file
        # ------------------------------------------------------------------
        try:
            ass_content = _generate_ass_content(
                chunks,
                merged_config,
                safe_zones,
                play_res_x=input_w,
                play_res_y=input_h,
            )
        except Exception as exc:
            return _fail(
                "unexpected_caption_error",
                f"exception during ASS generation: {exc}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        try:
            with open(sidecar_path, "w", encoding="utf-8") as fh:
                fh.write(ass_content)
        except Exception as exc:
            return _fail(
                "subtitle_write_failed",
                f"failed to write ASS sidecar file: {exc}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        if not os.path.isfile(sidecar_path) or os.path.getsize(sidecar_path) == 0:
            return _fail(
                "subtitle_write_failed",
                f"ASS sidecar is missing or empty after write: {sidecar_path}",
                candidate_id=candidate_id,
                input_path=input_path,
            )

        # ------------------------------------------------------------------
        # 10. Build and run ffmpeg
        # ------------------------------------------------------------------
        ffmpeg_cmd = _build_caption_command(
            input_path=input_path,
            ass_path=sidecar_path,
            output_path=output_path,
            config=merged_config,
            input_has_audio=input_has_audio,
        )
        cmd_summary = " ".join(str(a) for a in ffmpeg_cmd)

        try:
            proc = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                timeout=FFMPEG_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return _fail(
                "ffmpeg_failed",
                f"ffmpeg timed out after {FFMPEG_TIMEOUT_SEC}s",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=None,
                ffmpeg_stderr_tail="timeout",
                ffmpeg_command_summary=cmd_summary,
            )
        except Exception as exc:
            return _fail(
                "unexpected_caption_error",
                f"unexpected error launching ffmpeg: {exc}",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_command_summary=cmd_summary,
            )

        if proc.returncode != 0:
            stderr_tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-800:]
            return _fail(
                "ffmpeg_failed",
                f"ffmpeg exited with code {proc.returncode}",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=proc.returncode,
                ffmpeg_stderr_tail=stderr_tail or "(no output)",
                ffmpeg_command_summary=cmd_summary,
            )

        # ------------------------------------------------------------------
        # 11. Verify output
        # ------------------------------------------------------------------
        if not os.path.isfile(output_path):
            return _fail(
                "output_missing",
                f"ffmpeg succeeded but output is absent: {output_path}",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        output_size = os.path.getsize(output_path)
        if output_size == 0:
            return _fail(
                "output_empty",
                f"output file exists but is empty: {output_path}",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        try:
            out_info = _probe_video_info(output_path)
        except Exception as exc:
            return _fail(
                "output_probe_failed",
                f"could not probe output file: {exc}",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        if out_info is None or out_info["width"] <= 0:
            return _fail(
                "output_missing_video_stream",
                f"output has no valid video stream: {output_path}",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        if out_info["width"] != input_w or out_info["height"] != input_h:
            return _fail(
                "dimension_mismatch",
                (
                    f"output dimensions {out_info['width']}x{out_info['height']} "
                    f"do not match input {input_w}x{input_h}"
                ),
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        if input_has_audio and not out_info["has_audio"]:
            return _fail(
                "output_missing_audio",
                "input had audio but output has no audio stream",
                candidate_id=candidate_id,
                input_path=input_path,
                ffmpeg_returncode=0,
                ffmpeg_command_summary=cmd_summary,
            )

        duration_delta: float = 0.0
        if input_duration is not None and out_info["duration_sec"] is not None:
            duration_delta = abs(out_info["duration_sec"] - input_duration)
            tolerance = float(merged_config.get("duration_tolerance_sec", 1.0))
            if duration_delta > tolerance:
                return _fail(
                    "duration_mismatch",
                    (
                        f"output duration {out_info['duration_sec']:.3f}s differs from "
                        f"input {input_duration:.3f}s by {duration_delta:.3f}s "
                        f"(tolerance {tolerance:.3f}s)"
                    ),
                    candidate_id=candidate_id,
                    input_path=input_path,
                    ffmpeg_returncode=0,
                    ffmpeg_command_summary=cmd_summary,
                )

        # ------------------------------------------------------------------
        # 12. Return PASS result
        # ------------------------------------------------------------------
        caption_text_chars = sum(len(c.get("text", "")) for c in chunks)
        caption_style = {
            "font_family": merged_config["font_family"],
            "font_size": merged_config["font_size"],
            "font_bold": merged_config["font_bold"],
            "font_color": merged_config["font_color"],
            "outline_color": merged_config["outline_color"],
            "outline_width": merged_config["outline_width"],
            "shadow": merged_config["shadow"],
            "max_lines": merged_config["max_lines"],
            "max_chars_per_line": merged_config["max_chars_per_line"],
        }

        return make_module_pass_result(
            MODULE_NAME,
            MODULE_VERSION,
            input_path=input_path,
            output_path=output_path,
            config=merged_config,
            metadata={
                "candidate_id": candidate_id,
                "input_width": input_w,
                "input_height": input_h,
                "input_duration_sec": round(input_duration, 3) if input_duration is not None else None,
                "input_has_audio": input_has_audio,
                "output_width": out_info["width"],
                "output_height": out_info["height"],
                "output_duration_sec": (
                    round(out_info["duration_sec"], 3)
                    if out_info["duration_sec"] is not None
                    else None
                ),
                "duration_delta_sec": round(duration_delta, 3),
                "caption_format": "ass",
                "caption_sidecar_path": sidecar_path,
                "caption_count": len(chunks),
                "caption_text_chars": caption_text_chars,
                "caption_safe_zone": safe_zones,
                "caption_style": caption_style,
                "keyword_highlighting_enabled": bool(
                    merged_config.get("enable_keyword_highlighting", False)
                ),
                "highlighted_word_count": 0,
                "ffmpeg_command_summary": cmd_summary,
                "output_file_size_bytes": output_size,
            },
        )


# ---------------------------------------------------------------------------
# Conveyor registry helpers
# ---------------------------------------------------------------------------

INTELLIGENT_CAPTIONS_V1_MODULE = IntelligentCaptionsV1Module()


def get_intelligent_captions_v1_module() -> IntelligentCaptionsV1Module:
    """Return a fresh IntelligentCaptionsV1Module instance for the conveyor registry."""
    return IntelligentCaptionsV1Module()


# ---------------------------------------------------------------------------
# Caption data resolution
# ---------------------------------------------------------------------------


def _resolve_caption_source(
    context: dict[str, Any],
    candidate: dict[str, Any],
    config: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]] | None:
    """Resolve caption text/timing data from existing artifacts.

    Returns ``(source_type, data)`` where ``source_type`` is ``"words"`` or
    ``"segments"``, and ``data`` is a non-empty list.  Returns ``None`` if no
    usable timing data is found.

    Priority order:
      1. source_candidate.caption_segments
      2. source_candidate.transcript_segments
      3. source_candidate.words
      4. selected_candidate.caption_segments
      5. selected_candidate.transcript_segments
      6. selected_candidate.words
      7. transcript_path from context["config"]
      8. transcript_path from context["selection_result"] metadata
    """
    src = candidate.get("source_candidate") or {}

    for obj in (src, candidate):
        for key in ("caption_segments", "transcript_segments", "segments"):
            val = obj.get(key)
            if isinstance(val, list) and val:
                return ("segments", val)
        val = obj.get("words")
        if isinstance(val, list) and val:
            return ("words", val)

    # Transcript path sources
    for _source in _iter_transcript_paths(context):
        result = _try_load_transcript(_source)
        if result is not None:
            return result

    return None


def _iter_transcript_paths(context: dict[str, Any]):
    """Yield candidate transcript file paths from the context."""
    cfg = context.get("config") or {}
    t = cfg.get("transcript_path")
    if isinstance(t, str) and t.strip():
        yield t.strip()

    sel = context.get("selection_result") or {}
    for sub in (sel, sel.get("metadata") or {}):
        if not isinstance(sub, dict):
            continue
        t = sub.get("transcript_path")
        if isinstance(t, str) and t.strip():
            yield t.strip()

    meta = context.get("job_metadata") or {}
    if isinstance(meta, dict):
        t = meta.get("transcript_path")
        if isinstance(t, str) and t.strip():
            yield t.strip()


def _try_load_transcript(path: str) -> tuple[str, list[dict[str, Any]]] | None:
    """Attempt to load a transcript file and return (source_type, data) or None."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None

        words = data.get("words")
        if isinstance(words, list) and words:
            return ("words", words)

        segments = data.get("segments")
        if isinstance(segments, list) and segments:
            return ("segments", segments)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Caption chunking — word-level (preferred)
# ---------------------------------------------------------------------------


def _chunk_from_words(
    words: list[dict[str, Any]],
    candidate_start: float,
    candidate_end: float,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Produce caption chunks from word-level timing data.

    Words outside the candidate range are skipped.  Timings are made relative
    to the clip start (``candidate_start``).
    """
    max_chars = int(config.get("max_chars_per_caption", 42))
    max_lines = int(config.get("max_lines", 2))
    max_chars_per_line = int(config.get("max_chars_per_line", 32))
    min_dur = float(config.get("min_caption_duration_sec", 0.45))
    max_dur = float(config.get("max_caption_duration_sec", 2.2))
    clip_duration = candidate_end - candidate_start

    # Filter and relativise words
    clip_words: list[dict[str, Any]] = []
    for w in words:
        if not isinstance(w, dict):
            continue
        try:
            ws = float(w["start"])
            we = float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        word_text = str(w.get("word") or "").strip()
        if not word_text:
            continue
        if we <= candidate_start or ws >= candidate_end:
            continue
        rel_start = max(0.0, ws - candidate_start)
        rel_end = min(clip_duration, we - candidate_start)
        if rel_end <= rel_start:
            continue
        clip_words.append({"start": rel_start, "end": rel_end, "word": word_text})

    if not clip_words:
        return []

    chunks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    for word in clip_words:
        current.append(word)
        current_text = " ".join(cw["word"] for cw in current)

        if len(current_text) > max_chars and len(current) > 1:
            # Emit without last word, restart with it
            emit = current[:-1]
            chunk = _make_chunk_from_words(
                emit, max_lines, max_chars_per_line, min_dur, max_dur, clip_duration
            )
            if chunk:
                chunks.append(chunk)
            current = [word]

    if current:
        chunk = _make_chunk_from_words(
            current, max_lines, max_chars_per_line, min_dur, max_dur, clip_duration
        )
        if chunk:
            chunks.append(chunk)

    return chunks


def _make_chunk_from_words(
    words: list[dict[str, Any]],
    max_lines: int,
    max_chars_per_line: int,
    min_dur: float,
    max_dur: float,
    clip_duration: float,
) -> dict[str, Any] | None:
    """Build a single caption chunk dict from a word list."""
    text = " ".join(w["word"] for w in words).strip()
    if not text:
        return None

    start = float(words[0]["start"])
    end = float(words[-1]["end"])

    # Enforce min duration
    if end - start < min_dur:
        end = min(start + min_dur, clip_duration)

    # Enforce max duration
    if end - start > max_dur:
        end = start + max_dur

    if end <= start:
        return None

    lines = _break_into_lines(text, max_lines, max_chars_per_line)
    return {"start_sec": start, "end_sec": end, "lines": lines, "text": text}


# ---------------------------------------------------------------------------
# Caption chunking — segment-level (fallback)
# ---------------------------------------------------------------------------


def _chunk_from_segments(
    segments: list[dict[str, Any]],
    candidate_start: float,
    candidate_end: float,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Produce caption chunks from transcript segment data.

    If a segment also contains word-level timing, it delegates to the
    word-level chunker for that segment.  Otherwise the segment text is split
    proportionally.
    """
    max_chars = int(config.get("max_chars_per_caption", 42))
    max_lines = int(config.get("max_lines", 2))
    max_chars_per_line = int(config.get("max_chars_per_line", 32))
    min_dur = float(config.get("min_caption_duration_sec", 0.45))
    max_dur = float(config.get("max_caption_duration_sec", 2.2))
    clip_duration = candidate_end - candidate_start

    chunks: list[dict[str, Any]] = []

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            seg_start = float(seg["start"])
            seg_end = float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue

        if seg_end <= candidate_start or seg_start >= candidate_end:
            continue

        rel_start = max(0.0, seg_start - candidate_start)
        rel_end = min(clip_duration, seg_end - candidate_start)
        if rel_end <= rel_start:
            continue

        # If segment has word-level timing, delegate
        seg_words = seg.get("words")
        if isinstance(seg_words, list) and seg_words:
            sub = _chunk_from_words(seg_words, candidate_start, candidate_end, config)
            chunks.extend(sub)
            continue

        text = str(seg.get("text") or "").strip()
        if not text:
            continue

        seg_dur = rel_end - rel_start

        if len(text) <= max_chars:
            actual_end = rel_end
            if seg_dur < min_dur:
                actual_end = min(rel_start + min_dur, clip_duration)
            elif seg_dur > max_dur:
                actual_end = rel_start + max_dur
            lines = _break_into_lines(text, max_lines, max_chars_per_line)
            chunks.append(
                {"start_sec": rel_start, "end_sec": actual_end, "lines": lines, "text": text}
            )
        else:
            # Split into sub-chunks with proportional timing
            sub_texts = _split_text_to_chunks(text, max_chars)
            if not sub_texts:
                continue
            time_per_sub = seg_dur / len(sub_texts)
            for i, sub_text in enumerate(sub_texts):
                sub_start = rel_start + i * time_per_sub
                sub_end = rel_start + (i + 1) * time_per_sub
                sub_dur = sub_end - sub_start
                actual_end = sub_end
                if sub_dur < min_dur:
                    actual_end = min(sub_start + min_dur, clip_duration)
                elif sub_dur > max_dur:
                    actual_end = sub_start + max_dur
                lines = _break_into_lines(sub_text, max_lines, max_chars_per_line)
                chunks.append(
                    {
                        "start_sec": sub_start,
                        "end_sec": actual_end,
                        "lines": lines,
                        "text": sub_text,
                    }
                )

    return chunks


def _split_text_to_chunks(text: str, max_chars: int) -> list[str]:
    """Split text into sub-strings of at most max_chars, breaking on word boundaries."""
    words = text.split()
    if not words:
        return []
    result: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if current and len(candidate) > max_chars:
            result.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        result.append(" ".join(current))
    return result


# ---------------------------------------------------------------------------
# Line breaking
# ---------------------------------------------------------------------------


def _break_into_lines(text: str, max_lines: int, max_chars_per_line: int) -> list[str]:
    """Break text into at most max_lines lines of at most max_chars_per_line chars each.

    Uses a greedy word-wrap algorithm.  Words that are too long to fit on a
    single line are placed on their own line.
    """
    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current: list[str] = []
    current_len = 0

    for word in words:
        if not current:
            current = [word]
            current_len = len(word)
        elif current_len + 1 + len(word) <= max_chars_per_line:
            current.append(word)
            current_len += 1 + len(word)
        elif len(lines) < max_lines - 1:
            # Start a new line (still under max_lines)
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            # Already on last line — append anyway to avoid losing words
            current.append(word)
            current_len += 1 + len(word)

    if current:
        lines.append(" ".join(current))

    return lines


# ---------------------------------------------------------------------------
# ASS subtitle generation
# ---------------------------------------------------------------------------


def _generate_ass_content(
    chunks: list[dict[str, Any]],
    config: dict[str, Any],
    safe_zones: dict[str, Any],
    *,
    play_res_x: int = 1080,
    play_res_y: int = 1920,
) -> str:
    """Generate ASS subtitle file content for the given caption chunks.

    ASS colour format: ``&HAABBGGRR`` (alpha, blue, green, red, all hex).
    """
    font_family = str(config.get("font_family", "Arial"))
    font_size = int(config.get("font_size", 64))
    font_bold = bool(config.get("font_bold", True))
    outline_width = int(config.get("outline_width", 4))
    shadow = int(config.get("shadow", 1))

    # ASS colours (BGR with alpha prefix)
    primary_color = "&H00FFFFFF"  # white, opaque
    secondary_color = "&H00000000"  # black
    outline_color_ass = "&H00000000"  # black outline
    back_color = "&H80000000"  # semi-transparent black shadow

    bold_flag = -1 if font_bold else 0

    margin_l = int(config.get("safe_zone_left_px", 80))
    margin_r = int(config.get("safe_zone_right_px", 80))

    # Vertical position: MarginV is distance from the BOTTOM edge (Alignment=2)
    caption_y_px = config.get("caption_y_px")
    if caption_y_px is not None and _is_finite_float(caption_y_px):
        # caption_y_px is from the top; MarginV is from the bottom
        margin_v = max(0, int(play_res_y) - int(caption_y_px))
    else:
        bottom_px = int(safe_zones.get("bottom_margin_px", 320))
        # Place caption at: play_res_y - bottom_px - 200 (from top)
        # → MarginV = bottom_px + 200
        margin_v = bottom_px + 200

    # Clamp margin_v to be at least safe_zone_bottom_px
    min_margin_v = int(config.get("safe_zone_bottom_px", 320))
    margin_v = max(margin_v, min_margin_v)

    style_line = (
        f"Style: Default,{font_family},{font_size},"
        f"{primary_color},{secondary_color},{outline_color_ass},{back_color},"
        f"{bold_flag},0,0,0,100,100,0,0,1,{outline_width},{shadow},"
        f"2,{margin_l},{margin_r},{margin_v},1"
    )

    script_lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        "Collisions: Normal",
        "Timer: 100.0000",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        style_line,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for chunk in chunks:
        start_str = _ass_time(float(chunk["start_sec"]))
        end_str = _ass_time(float(chunk["end_sec"]))
        # Join lines with \N (ASS hard line-break)
        text_parts = [_escape_ass_text(str(ln)) for ln in chunk["lines"] if str(ln).strip()]
        text = r"\N".join(text_parts)
        script_lines.append(
            f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{text}"
        )

    return "\n".join(script_lines) + "\n"


def _ass_time(seconds: float) -> str:
    """Convert seconds to ASS time string ``H:MM:SS.CC`` (centiseconds)."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    s_int = int(s)
    cs = int(round((s - s_int) * 100))
    if cs >= 100:
        cs = 0
        s_int += 1
    if s_int >= 60:
        s_int = 0
        m += 1
    if m >= 60:
        m = 0
        h += 1
    return f"{h}:{m:02d}:{s_int:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    """Escape ASS special characters in subtitle text.

    Handles backslash, curly braces (override tag delimiters).
    """
    text = text.replace("\\", "\\\\")
    text = text.replace("{", r"\{").replace("}", r"\}")
    return text


# ---------------------------------------------------------------------------
# Safe-zone resolution
# ---------------------------------------------------------------------------


def _resolve_safe_zones(
    context: dict[str, Any],
    config: dict[str, Any],
) -> tuple[dict[str, Any], int, int]:
    """Return ``(safe_zones, target_width, target_height)``.

    Looks for a ``platform_safe_format_v1`` result in
    ``context["module_results"]`` first, then falls back to config defaults.
    """
    module_results = context.get("module_results") or []
    for result in module_results:
        if not isinstance(result, dict):
            continue
        if result.get("module_name") == "platform_safe_format_v1":
            meta = result.get("metadata") or {}
            sz = meta.get("safe_zones")
            tw = meta.get("target_width", 1080)
            th = meta.get("target_height", 1920)
            if isinstance(sz, dict) and sz:
                return (sz, int(tw), int(th))

    # Fall back to config values
    target_width = int(config.get("target_width", 1080))
    target_height = int(config.get("target_height", 1920))
    top = int(config.get("safe_zone_top_px", 180))
    bottom = int(config.get("safe_zone_bottom_px", 320))
    left = int(config.get("safe_zone_left_px", 80))
    right = int(config.get("safe_zone_right_px", 80))
    safe_zones: dict[str, Any] = {
        "top_margin_px": top,
        "bottom_margin_px": bottom,
        "left_margin_px": left,
        "right_margin_px": right,
        "caption_safe_y_min_px": top * 2,
        "caption_safe_y_max_px": target_height - bottom * 2,
    }
    return (safe_zones, target_width, target_height)


# ---------------------------------------------------------------------------
# ffmpeg command builder
# ---------------------------------------------------------------------------


def _build_caption_command(
    *,
    input_path: str,
    ass_path: str,
    output_path: str,
    config: dict[str, Any],
    input_has_audio: bool,
) -> list[str]:
    """Build the ffmpeg args list for subtitle burn-in.

    Uses the ``subtitles`` filter with an ASS file.  Does not perform any
    scaling, cropping, or audio normalisation.
    """
    video_codec = str(config.get("video_codec", "libx264"))
    audio_codec = str(config.get("audio_codec", "aac"))
    preset = str(config.get("ffmpeg_preset", "veryfast"))

    escaped_ass = _escape_path_for_filter(ass_path)
    vf_value = f"subtitles={escaped_ass}"

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", vf_value,
        "-c:v", video_codec,
        "-preset", preset,
    ]

    if input_has_audio:
        cmd += ["-c:a", audio_codec]
    else:
        cmd += ["-an"]

    cmd.append(output_path)
    return cmd


def _escape_path_for_filter(path: str) -> str:
    """Escape a file path for use as an ffmpeg filter option value.

    Only characters that have special meaning in ffmpeg filter syntax need
    escaping.  On Linux, paths generated with :func:`_safe_filename_part` will
    not contain any of these, but we escape defensively.
    """
    path = path.replace("\\", "\\\\")
    path = path.replace(":", "\\:")
    path = path.replace("'", "\\'")
    return path


# ---------------------------------------------------------------------------
# Output path helpers
# ---------------------------------------------------------------------------


def _make_output_path(clip_dir: str, job_id: str, candidate_id: str, *, ext: str = ".mp4") -> str:
    safe_job = _safe_filename_part(job_id)
    safe_cand = _safe_filename_part(candidate_id)
    filename = f"{safe_job}_{safe_cand}_intelligent_captions_v1{ext}"
    return os.path.join(clip_dir, filename)


def _make_sidecar_path(tmp_dir: str, job_id: str, candidate_id: str) -> str:
    safe_job = _safe_filename_part(job_id)
    safe_cand = _safe_filename_part(candidate_id)
    filename = f"{safe_job}_{safe_cand}_intelligent_captions_v1.ass"
    return os.path.join(tmp_dir, filename)


def _safe_filename_part(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(value))


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------

_VideoInfo = dict[str, Any]  # width, height, duration_sec, has_audio


def _probe_video_info(path: str) -> _VideoInfo | None:
    """Probe a video file and return ``{width, height, duration_sec, has_audio}``.

    Returns ``None`` if the probe fails entirely.  On partial results the dict
    is still returned with ``None`` for missing fields.
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-hide_banner",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SEC,
        )
    except Exception:
        return None

    if proc.returncode != 0:
        return None

    try:
        data: dict[str, Any] = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    streams = data.get("streams") or []
    width = 0
    height = 0
    has_audio = False

    for s in streams:
        if not isinstance(s, dict):
            continue
        codec_type = str(s.get("codec_type") or "")
        if codec_type == "video" and width == 0:
            try:
                width = int(s.get("width") or 0)
                height = int(s.get("height") or 0)
            except (TypeError, ValueError):
                pass
        elif codec_type == "audio":
            has_audio = True

    duration_sec: float | None = None
    fmt = data.get("format")
    if isinstance(fmt, dict) and fmt.get("duration") is not None:
        try:
            d = float(fmt["duration"])
            if math.isfinite(d) and d > 0:
                duration_sec = d
        except (TypeError, ValueError):
            pass

    return {"width": width, "height": height, "duration_sec": duration_sec, "has_audio": has_audio}


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def _validate_caption_config(config: dict[str, Any]) -> str | None:
    """Return an error string if config is invalid, else None."""
    max_lines = config.get("max_lines")
    if not isinstance(max_lines, int) or isinstance(max_lines, bool) or max_lines < 1 or max_lines > 2:
        return f"max_lines must be 1 or 2 for MK1, got {max_lines!r}"

    font_size = config.get("font_size")
    if not isinstance(font_size, int) or isinstance(font_size, bool) or font_size < 8 or font_size > 500:
        return f"font_size must be between 8 and 500, got {font_size!r}"

    min_dur = config.get("min_caption_duration_sec")
    max_dur = config.get("max_caption_duration_sec")

    if not _is_finite_float(min_dur) or float(min_dur) <= 0:
        return f"min_caption_duration_sec must be a positive number, got {min_dur!r}"
    if not _is_finite_float(max_dur) or float(max_dur) <= 0:
        return f"max_caption_duration_sec must be a positive number, got {max_dur!r}"
    if float(max_dur) <= float(min_dur):
        return (
            f"max_caption_duration_sec ({max_dur}) must be greater than "
            f"min_caption_duration_sec ({min_dur})"
        )

    tolerance = config.get("duration_tolerance_sec")
    if not _is_finite_float(tolerance) or float(tolerance) < 0:
        return f"duration_tolerance_sec must be a non-negative number, got {tolerance!r}"

    output_ext = config.get("output_ext")
    if not isinstance(output_ext, str) or not output_ext.startswith("."):
        return f"output_ext must be a string starting with '.', got {output_ext!r}"

    highlight_words = config.get("highlight_words")
    if highlight_words is not None and not isinstance(highlight_words, list):
        return f"highlight_words must be a list, got {type(highlight_words).__name__}"

    return None


def _is_finite_float(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------


def _extract_candidate_id(context: dict[str, Any]) -> str | None:
    """Best-effort extraction of candidate_id from context."""
    try:
        cand = context.get("selected_candidate") or {}
        if isinstance(cand, dict):
            cid = cand.get("candidate_id")
            if cid:
                return str(cid)
        cid = context.get("candidate_id")
        if cid:
            return str(cid)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Failure result helper
# ---------------------------------------------------------------------------


def _fail(
    failure_code: str,
    message: str,
    *,
    candidate_id: str | None,
    input_path: str | None,
    ffmpeg_returncode: int | None = None,
    ffmpeg_stderr_tail: str | None = None,
    ffmpeg_command_summary: str | None = None,
) -> dict[str, Any]:
    """Build a standard FAIL module result for this module."""
    metadata: dict[str, Any] = {
        "candidate_id": candidate_id,
        "failure_code": failure_code,
    }
    if ffmpeg_returncode is not None:
        metadata["ffmpeg_returncode"] = ffmpeg_returncode
    if ffmpeg_stderr_tail is not None:
        metadata["ffmpeg_stderr_tail"] = ffmpeg_stderr_tail
    if ffmpeg_command_summary is not None:
        metadata["ffmpeg_command_summary"] = ffmpeg_command_summary

    return make_module_fail_result(
        MODULE_NAME,
        MODULE_VERSION,
        message,
        input_path=input_path,
        output_path=None,
        metadata=metadata,
    )
