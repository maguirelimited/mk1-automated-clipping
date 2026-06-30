"""WhisperX transcription backend for video-automation."""

from __future__ import annotations

import gc
import json
import os
import sys
from typing import Any

from mk04_utils import ensure_paths, load_config
from pipeline_debug_ndjson import write_debug_agent

DEFAULT_WHISPERX_MODEL = "medium"
DEFAULT_WHISPERX_LANGUAGE = "en"
DEFAULT_WHISPERX_DEVICE = "cuda"
DEFAULT_WHISPERX_COMPUTE_TYPE = "float16"
DEFAULT_WHISPERX_BATCH_SIZE = 8

ALLOWED_WHISPERX_MODELS = ("tiny", "base", "small", "medium", "large-v2", "large-v3")


class WhisperXConfigError(RuntimeError):
    """Raised for invalid WhisperX settings, before any model is loaded."""


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def resolve_whisperx_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve WhisperX runtime settings from env and config.

    Precedence (highest first):
      model        WHISPERX_MODEL > MK04_WHISPER_MODEL > models.whisperx_model
                   > models.whisper_model > "medium"
      language     WHISPERX_LANGUAGE > transcription.language > "en"
      device       WHISPERX_DEVICE > transcription.device > "cuda"
      compute_type WHISPERX_COMPUTE_TYPE > transcription.compute_type > "float16"
      batch_size   WHISPERX_BATCH_SIZE > transcription.batch_size > 8

    Raises ``WhisperXConfigError`` for an unsupported model or a non-positive
    batch size, so callers fail before loading WhisperX.
    """
    cfg = config if isinstance(config, dict) else load_config()
    models = cfg.get("models") if isinstance(cfg.get("models"), dict) else {}
    transcription = (
        cfg.get("transcription") if isinstance(cfg.get("transcription"), dict) else {}
    )

    model = _first_nonempty(
        os.environ.get("WHISPERX_MODEL"),
        os.environ.get("MK04_WHISPER_MODEL"),
        models.get("whisperx_model"),
        models.get("whisper_model"),
        DEFAULT_WHISPERX_MODEL,
    )
    if model not in ALLOWED_WHISPERX_MODELS:
        raise WhisperXConfigError(
            f"Invalid WhisperX model {model!r}. Allowed models: "
            f"{', '.join(ALLOWED_WHISPERX_MODELS)}."
        )

    language = _first_nonempty(
        os.environ.get("WHISPERX_LANGUAGE"),
        transcription.get("language"),
        DEFAULT_WHISPERX_LANGUAGE,
    )
    device = _first_nonempty(
        os.environ.get("WHISPERX_DEVICE"),
        transcription.get("device"),
        DEFAULT_WHISPERX_DEVICE,
    )
    compute_type = _first_nonempty(
        os.environ.get("WHISPERX_COMPUTE_TYPE"),
        transcription.get("compute_type"),
        DEFAULT_WHISPERX_COMPUTE_TYPE,
    )

    batch_raw: Any = _first_nonempty(os.environ.get("WHISPERX_BATCH_SIZE"))
    if not batch_raw:
        cfg_batch = transcription.get("batch_size")
        batch_raw = cfg_batch if cfg_batch is not None else DEFAULT_WHISPERX_BATCH_SIZE
    try:
        batch_size = int(str(batch_raw).strip())
    except (TypeError, ValueError) as exc:
        raise WhisperXConfigError(
            f"Invalid WhisperX batch_size {batch_raw!r}: must be a positive integer."
        ) from exc
    if batch_size <= 0:
        raise WhisperXConfigError(
            f"Invalid WhisperX batch_size {batch_size}: must be a positive integer."
        )

    return {
        "model": model,
        "language": language,
        "device": device,
        "compute_type": compute_type,
        "batch_size": batch_size,
    }


def _derive_transcript_path(input_video: str, output_dir: str) -> str:
    stem = os.path.splitext(os.path.basename(input_video))[0]
    return os.path.join(output_dir, f"{stem}.json")


def _normalize_word(raw: dict[str, Any]) -> dict[str, Any] | None:
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


def _normalize_segment(raw: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        start = float(raw.get("start"))
        end = float(raw.get("end"))
    except (TypeError, ValueError):
        return None
    if end <= start:
        return None
    text = str(raw.get("text") or "").strip()
    words_raw = raw.get("words")
    words: list[dict[str, Any]] = []
    if isinstance(words_raw, list):
        for row in words_raw:
            word = _normalize_word(row) if isinstance(row, dict) else None
            if word:
                words.append(word)
    if not text and words:
        text = " ".join(w["word"] for w in words).strip()
    if not text:
        return None
    segment: dict[str, Any] = {"start": start, "end": end, "text": text}
    if words:
        segment["words"] = words
    return segment


def normalize_whisperx_result(
    result: dict[str, Any], *, default_language: str = DEFAULT_WHISPERX_LANGUAGE
) -> dict[str, Any]:
    """Normalize WhisperX output into the pipeline transcript JSON contract."""
    language = str(result.get("language") or default_language).strip() or default_language
    segments_raw = result.get("segments")
    segments: list[dict[str, Any]] = []
    flat_words: list[dict[str, Any]] = []
    if isinstance(segments_raw, list):
        for row in segments_raw:
            segment = _normalize_segment(row) if isinstance(row, dict) else None
            if not segment:
                continue
            segments.append(segment)
            for word in segment.get("words") or []:
                flat_words.append(dict(word))

    text = str(result.get("text") or "").strip()
    if not text and segments:
        text = " ".join(s["text"] for s in segments if s.get("text")).strip()

    top_words_raw = result.get("words")
    if isinstance(top_words_raw, list) and not flat_words:
        for row in top_words_raw:
            word = _normalize_word(row) if isinstance(row, dict) else None
            if word:
                flat_words.append(word)

    payload: dict[str, Any] = {
        "engine": "whisperx",
        "language": language,
        "text": text,
        "segments": segments,
    }
    if flat_words:
        payload["words"] = flat_words
    if segments:
        payload["duration"] = max(float(seg["end"]) for seg in segments)
    return payload


def _write_transcript(path: str, payload: dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def run_whisperx_transcription(input_video: str) -> str:
    config = load_config()
    settings = resolve_whisperx_settings(config)
    model_name = settings["model"]
    language = settings["language"]
    device = settings["device"]
    compute_type = settings["compute_type"]
    batch_size = settings["batch_size"]

    video_path = os.path.abspath(input_video)
    output_dir = ensure_paths(config)["temp"]
    transcript_path = _derive_transcript_path(video_path, output_dir)

    if not os.path.exists(video_path):
        raise ValueError(f"Input video not found: {video_path}")

    write_debug_agent(
        "transcribe-whisperx",
        "H9-whisperx-entry",
        "transcribe_whisperx.py:run_whisperx_transcription",
        "entered WhisperX transcription runner",
        {
            "video_path": video_path,
            "output_dir": output_dir,
            "model": model_name,
            "language": language,
            "device": device,
            "compute_type": compute_type,
            "batch_size": batch_size,
        },
    )

    print(
        f"[WhisperX] Loading model={model_name} device={device} "
        f"compute_type={compute_type} batch_size={batch_size}",
        file=sys.stderr,
    )

    try:
        import torch
        import whisperx
    except ImportError as exc:
        raise RuntimeError(
            "WhisperX is not installed. Install video-automation requirements "
            "(pip install -r requirements.txt)."
        ) from exc

    model = None
    align_model = None
    try:
        model = whisperx.load_model(
            model_name,
            device,
            compute_type=compute_type,
        )
        audio = whisperx.load_audio(video_path)
        result = model.transcribe(
            audio,
            batch_size=batch_size,
            language=language,
        )

        detected_language = str(result.get("language") or language).strip()
        align_model, metadata = whisperx.load_align_model(
            language_code=detected_language,
            device=device,
        )
        result = whisperx.align(
            result["segments"],
            align_model,
            metadata,
            audio,
            device,
            return_char_alignments=False,
        )
        if isinstance(result, dict):
            result.setdefault("language", detected_language)

        payload = normalize_whisperx_result(
            result if isinstance(result, dict) else {},
            default_language=language,
        )
        if not payload.get("segments"):
            raise RuntimeError(
                "WhisperX produced no timed segments after alignment "
                f"(language={detected_language})."
            )

        _write_transcript(transcript_path, payload)
        print(f"[WhisperX] Wrote transcript: {transcript_path}", file=sys.stderr)
        return transcript_path
    except Exception as exc:
        print(f"[WhisperX] Transcription failed: {exc}", file=sys.stderr)
        raise RuntimeError(f"WhisperX transcription failed: {exc}") from exc
    finally:
        del model
        del align_model
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
