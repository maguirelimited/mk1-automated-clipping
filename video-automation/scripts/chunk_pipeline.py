"""Helpers for splitting long-form video into FFmpeg chunks before Whisper / selection."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


def load_chunk_config(config: dict[str, Any]) -> dict[str, Any] | None:
    raw = config.get("chunking")
    return raw if isinstance(raw, dict) else None


def should_use_chunked_transcription(
    chunk_cfg: dict[str, Any] | None, video_duration_sec: float | None
) -> bool:
    if not chunk_cfg or not bool(chunk_cfg.get("enabled")):
        return False
    if video_duration_sec is None or video_duration_sec <= 0:
        return False
    thresh = float(chunk_cfg.get("threshold_sec") or 3600)
    return video_duration_sec > thresh


def plan_wallclock_chunks(
    video_duration_sec: float, chunk_target_sec: float
) -> list[tuple[float, float]]:
    """Return contiguous (start_sec, duration_sec) windows covering the full timeline."""
    if chunk_target_sec < 60:
        chunk_target_sec = 60.0
    specs: list[tuple[float, float]] = []
    pos = 0.0
    while pos + 1e-6 < video_duration_sec:
        dur = min(chunk_target_sec, video_duration_sec - pos)
        if dur < 0.05:
            break
        specs.append((pos, dur))
        pos += dur
    return specs


def whisper_json_for_video(video_path: str, output_dir: str) -> str:
    stem = Path(video_path).stem
    return os.path.abspath(os.path.join(output_dir, f"{stem}.json"))


def merge_whisper_json_files(
    files_and_offsets: list[tuple[str, float]],
    total_duration_sec: float,
) -> dict[str, Any]:
    merged_segments: list[dict[str, Any]] = []
    merged_words: list[dict[str, Any]] = []
    texts: list[str] = []
    sid = 0
    language = ""
    engine = ""
    for path, offset_sec in files_and_offsets:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        lang = data.get("language")
        if isinstance(lang, str) and lang.strip():
            language = lang.strip()
        raw_engine = str(data.get("engine") or "").strip()
        if raw_engine:
            engine = raw_engine
        part_text = str(data.get("text") or "").strip()
        if part_text:
            texts.append(part_text)
        segs_raw = data.get("segments") or []
        if not isinstance(segs_raw, list):
            continue
        for row in segs_raw:
            if not isinstance(row, dict):
                continue
            try:
                s = float(row.get("start", 0)) + float(offset_sec)
                e = float(row.get("end", 0)) + float(offset_sec)
            except (TypeError, ValueError):
                continue
            if e <= s:
                continue
            tx = str(row.get("text") or "").strip()
            segment: dict[str, Any] = {"id": sid, "start": s, "end": e, "text": tx}
            words_raw = row.get("words")
            if isinstance(words_raw, list):
                segment_words: list[dict[str, Any]] = []
                for word_row in words_raw:
                    if not isinstance(word_row, dict):
                        continue
                    try:
                        ws = float(word_row.get("start", 0)) + float(offset_sec)
                        we = float(word_row.get("end", 0)) + float(offset_sec)
                    except (TypeError, ValueError):
                        continue
                    if we <= ws:
                        continue
                    word = str(word_row.get("word") or "").strip()
                    if not word:
                        continue
                    word_obj: dict[str, Any] = {"start": ws, "end": we, "word": word}
                    score = word_row.get("score")
                    if score is not None:
                        try:
                            word_obj["score"] = float(score)
                        except (TypeError, ValueError):
                            pass
                    segment_words.append(word_obj)
                    merged_words.append(dict(word_obj))
                if segment_words:
                    segment["words"] = segment_words
            merged_segments.append(segment)
            sid += 1
        top_words_raw = data.get("words")
        if isinstance(top_words_raw, list) and not merged_words:
            for word_row in top_words_raw:
                if not isinstance(word_row, dict):
                    continue
                try:
                    ws = float(word_row.get("start", 0)) + float(offset_sec)
                    we = float(word_row.get("end", 0)) + float(offset_sec)
                except (TypeError, ValueError):
                    continue
                if we <= ws:
                    continue
                word = str(word_row.get("word") or "").strip()
                if not word:
                    continue
                word_obj = {"start": ws, "end": we, "word": word}
                score = word_row.get("score")
                if score is not None:
                    try:
                        word_obj["score"] = float(score)
                    except (TypeError, ValueError):
                        pass
                merged_words.append(word_obj)
    # Sort by start time: WhisperX re-segments the overlap region around chunk
    # boundaries, so a later chunk's first segment can precede the previous
    # chunk's last segment by a small margin.  Sorting gives downstream
    # consumers (e.g. transcript_sectioning) a monotonically ordered timeline.
    merged_segments.sort(key=lambda s: float(s.get("start", 0)))
    merged_words.sort(key=lambda w: float(w.get("start", 0)))
    # Reassign sequential ids after sort.
    for i, seg in enumerate(merged_segments):
        seg["id"] = i
    payload: dict[str, Any] = {
        "text": " ".join(texts).strip(),
        "segments": merged_segments,
        "language": language,
        "duration": float(total_duration_sec),
    }
    if engine:
        payload["engine"] = engine
    if merged_words:
        payload["words"] = merged_words
    return payload


def write_merged_whisper_json(path: str, payload: dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def ffmpeg_extract_segment(
    input_video: str,
    output_video: str,
    start_sec: float,
    duration_sec: float,
    *,
    timeout_sec: float = 7200,
) -> None:
    parent = os.path.dirname(os.path.abspath(output_video))
    if parent:
        os.makedirs(parent, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(max(0.0, float(start_sec))),
        "-i",
        input_video,
        "-t",
        str(max(0.05, float(duration_sec))),
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        output_video,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=float(timeout_sec))
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg segment extract failed: {proc.stderr or proc.stdout or 'unknown'}"
        )
    if not os.path.isfile(output_video) or os.path.getsize(output_video) == 0:
        raise RuntimeError("ffmpeg produced empty chunk file")
