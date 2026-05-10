import json
import os
import sys
import time
from openai import OpenAI

from pipeline_utils import (
    make_script_error,
    make_script_success,
    normalize_segments,
    parse_selection_payload,
    postprocess_segments,
    validate_segment_times,
)
from mk04_utils import load_config, normalize_transcript_payload

_SELECTION_SYSTEM = """You are an expert short-form video editor. You choose clips that would perform well as standalone posts (TikTok, Reels, Shorts).

You must respond with a single JSON object only (no markdown), matching the user schema."""

_MAX_TRANSCRIPT_CHARS = 120_000
_MAX_SEGMENT_LINES = 500
DEBUG_MODE_LOG_PATH = os.environ.get("DEBUG_MODE_LOG_PATH", "").strip()
DEBUG_MODE_SESSION_ID = "c9492c"


def _debug_mode_log(hypothesis_id: str, location: str, message: str, data: dict):
    if not DEBUG_MODE_LOG_PATH:
        return
    payload = {
        "sessionId": DEBUG_MODE_SESSION_ID,
        "runId": "select-clip",
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


def _format_ts(total_sec: float) -> str:
    total = max(0.0, float(total_sec))
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _load_transcript(path: str) -> str:
    payload = normalize_transcript_payload(path)
    transcript = str(payload.get("full_text") or "")
    segments = payload.get("segments")
    timestamped_lines: list[str] = []
    if isinstance(segments, list):
        for row in segments[:_MAX_SEGMENT_LINES]:
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
            if not text:
                continue
            timestamped_lines.append(f"[{_format_ts(start)} -> {_format_ts(end)}] {text}")
    if timestamped_lines:
        transcript = "\n".join(timestamped_lines)
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        transcript = (
            transcript[:_MAX_TRANSCRIPT_CHARS]
            + "\n\n[Transcript truncated for length; select only from visible timestamped lines.]"
        )
    # #region agent log
    _debug_mode_log(
        "H1-untimed-transcript",
        "select_clip.py:_load_transcript",
        "loaded transcript payload characteristics",
        {
            "has_text": isinstance(payload.get("full_text"), str),
            "has_segments": isinstance(payload.get("segments"), list),
            "segments_count": len(payload.get("segments") or []),
            "returned_text_chars": len(transcript),
            "timestamped_lines_used": len(timestamped_lines),
        },
    )
    # #endregion
    return transcript


def _select_segments(transcript: str, selection_options: dict, client: OpenAI) -> list[dict]:
    config = load_config()
    sel_cfg = config.get("selection", {}) if isinstance(config.get("selection"), dict) else {}
    max_clips = int(selection_options.get("max_clips", 5))
    min_duration_sec = float(
        selection_options.get(
            "min_duration_sec",
            sel_cfg.get("min_clip_duration_sec", 5),
        )
    )
    max_duration_sec = float(
        selection_options.get(
            "max_duration_sec",
            sel_cfg.get("max_clip_duration_sec", 30),
        )
    )
    max_overlap_sec = float(
        selection_options.get(
            "max_overlap_sec",
            sel_cfg.get("max_overlap_sec", 2),
        )
    )

    user_prompt = f"""Analyze the timestamped transcript and return the BEST up to {max_clips} clips for short-form video.

## What makes a high-quality clip
1. **Hook (first seconds)**: The `start` timestamp must begin at a strong, complete spoken line—curiosity, tension, contrarian claim, or a crisp promise. Do not start mid-thought, mid-word, or on filler ("so", "um", "anyway", "like", "you know").
2. **Standalone value**: A viewer who knows nothing should still get a complete mini-idea (setup → payoff or one tight lesson).
3. **Engagement**: Contrast, specificity, emotion, story beat, or a clear actionable insight—not generic platitudes.
4. **Minimal filler**: Prefer dense segments; penalize long hedging, repetition, or off-topic ramble inside the window.

## Hard constraints
- Return **1** to **{max_clips}** clips in the `clips` array (fewer is fine if the transcript cannot support more strong clips).
- Each clip duration must be between **{min_duration_sec}** and **{max_duration_sec}** seconds (inclusive), based on `start` and `end`.
- `start` and `end` must match timestamps that appear in the provided transcript lines (use **HH:MM:SS**).
- Do not invent times. If no suitable timestamped window exists, return fewer clips.
- Pick **non-overlapping** windows when possible; slight overlap only if each clip is clearly stronger that way.
- If your first `start` guess lands weakly (filler/mid-thought), shift `start` FORWARD to the next strong complete sentence or hook. Never move `start` backward and never move so far that the hook is lost.

## Scoring (honest self-rating)
For each clip, fill `scores` with integers **1–10**:
- `hook_strength`: How strong is the opening for scroll-stopping?
- `clarity_standalone`: How clear and self-contained is the idea?
- `engagement_potential`: Likelihood of comments/saves/shares (specificity, emotion, novelty).
- `minimal_filler`: **10** = almost no filler; **1** = lots of dead air / hedging inside the clip.

## Metadata (required for every clip)
- `reason`: One short sentence: why this clip works (editorial, not generic praise).
- `title`: A punchy title for the video (≤ 80 characters), title case or sentence case—no clickbait lies.
- `hook`: One line that works as the **spoken or on-screen hook in the first 1–3 seconds** (can differ slightly from `title`; ≤ 120 characters).
- `caption`: **1–2 short lines** for social caption (use a single newline between line 1 and line 2 if needed; ≤ 240 characters total).

## Output shape (JSON object only)
{{
  "clips": [
    {{
      "start": "HH:MM:SS",
      "end": "HH:MM:SS",
      "reason": "string",
      "title": "string",
      "hook": "string",
      "caption": "string",
      "scores": {{
        "hook_strength": 0,
        "clarity_standalone": 0,
        "engagement_potential": 0,
        "minimal_filler": 0
      }}
    }}
  ]
}}

Transcript:
{transcript}
"""

    selection_model = (
        config.get("models", {}).get("selection_model", "gpt-4o-mini") or "gpt-4o-mini"
    )
    _api_key = os.environ.get("OPENAI_API_KEY")
    if not _api_key or not str(_api_key).strip():
        raise ValueError("OPENAI_API_KEY is not set. Export it before running selection.")

    try:
        response = client.chat.completions.create(
            model=selection_model,
            messages=[
                {"role": "system", "content": _SELECTION_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.35,
        )
    except Exception as e:
        raise RuntimeError(f"OpenAI API error: {e}") from e

    output = response.choices[0].message.content

    if not output:
        raise ValueError("Model returned empty output")

    payload = parse_selection_payload(output)
    # #region agent log
    _debug_mode_log(
        "H7-model-output-shape",
        "select_clip.py:_select_segments",
        "model output parse summary",
        {
            "output_chars": len(output),
            "payload_type": type(payload).__name__,
            "payload_clip_count": (
                len(payload.get("clips"))
                if isinstance(payload, dict) and isinstance(payload.get("clips"), list)
                else (len(payload) if isinstance(payload, list) else 0)
            ),
        },
    )
    # #endregion
    segments = normalize_segments(payload)
    # #region agent log
    _debug_mode_log(
        "H7-model-output-shape",
        "select_clip.py:_select_segments",
        "normalized segments before postprocess",
        {
            "normalized_count": len(segments),
            "normalized_segments_preview": segments[:10],
            "min_duration_sec": min_duration_sec,
            "max_duration_sec": max_duration_sec,
            "max_overlap_sec": max_overlap_sec,
        },
    )
    # #endregion
    processed = postprocess_segments(
        segments,
        max_clips=max_clips,
        min_duration_sec=min_duration_sec,
        max_duration_sec=max_duration_sec,
        max_overlap_sec=max_overlap_sec,
    )
    # #region agent log
    _debug_mode_log(
        "H2-model-invented-timestamps",
        "select_clip.py:_select_segments",
        "selection output timestamp range",
        {
            "selected_count": len(processed),
            "selected_starts": [str(item.get("start", "")) for item in processed[:5]],
            "selected_ends": [str(item.get("end", "")) for item in processed[:5]],
        },
    )
    # #endregion
    # #region agent log
    _debug_mode_log(
        "H6-overfiltered-postprocess",
        "select_clip.py:_select_segments",
        "segments after postprocess",
        {
            "processed_count": len(processed),
            "processed_segments_preview": processed[:10],
        },
    )
    # #endregion
    if not processed:
        fallback = None
        for segment in segments:
            try:
                validate_segment_times(str(segment.get("start", "")), str(segment.get("end", "")))
            except ValueError:
                continue
            fallback = dict(segment)
            break
        if fallback is not None:
            _debug_mode_log(
                "H6-overfiltered-postprocess",
                "select_clip.py:_select_segments",
                "postprocess empty; using first normalized valid fallback segment",
                {"fallback_segment": fallback},
            )
            return [fallback]
        raise ValueError("No valid segments after post-processing")
    return processed


def run_selection(transcript_path: str, selection_options: dict | None = None) -> list[dict]:
    path = os.path.abspath(transcript_path)
    if not os.path.exists(path):
        raise ValueError(f"Transcript file not found: {path}")

    _api_key = os.environ.get("OPENAI_API_KEY")
    if not _api_key or not str(_api_key).strip():
        raise ValueError("OPENAI_API_KEY is not set. Export it before running selection.")
    client = OpenAI(api_key=_api_key)

    transcript = _load_transcript(path)
    return _select_segments(transcript, selection_options or {}, client)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) < 1:
            raise ValueError("No transcript path provided")
        selection_options = {}
        if len(args) >= 2:
            selection_options = json.loads(args[1])
        processed = run_selection(args[0], selection_options)
        print(json.dumps(processed))
        print(make_script_success("select_clip", clips=processed))
        return 0
    except Exception as e:
        print(f"[ERROR] Selection failed: {e}", file=sys.stderr)
        print(make_script_error("select_clip", str(e)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
