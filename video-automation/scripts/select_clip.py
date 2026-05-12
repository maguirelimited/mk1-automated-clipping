import json
import os
import sys
from openai import OpenAI

from pipeline_utils import (
    make_script_error,
    make_script_success,
    normalize_segments,
    parse_selection_payload,
    postprocess_segments,
    shift_segments_wallclock,
)
from mk04_utils import (
    load_config,
    normalize_transcript_payload,
    require_timed_transcript_payload,
)
from pipeline_debug_ndjson import write_debug_mode

_SELECTION_SYSTEM_INTRO = """You are an expert short-form video editor. You choose clips that would perform well as standalone posts (TikTok, Reels, Shorts).

You must respond with a single JSON object only (no markdown), matching the user schema."""


def _build_selection_system(min_duration_sec: float, max_duration_sec: float) -> str:
    return (
        _SELECTION_SYSTEM_INTRO
        + f"""

**Mandatory duration rule (primary enforcement):** for every clip, duration = end minus start (seconds from your timestamps) MUST satisfy {min_duration_sec:g} ≤ duration ≤ {max_duration_sec:g}. Before returning JSON, verify each clip numerically. If no bracket-aligned window fits, return fewer clips — never output a clip outside this range."""
    )


_MAX_TRANSCRIPT_CHARS = 120_000
_MAX_SEGMENT_LINES = 500


def _format_ts(total_sec: float) -> str:
    total = max(0.0, float(total_sec))
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _load_transcript_with_stats(path: str) -> tuple[str, dict[str, object]]:
    payload = normalize_transcript_payload(path)
    require_timed_transcript_payload(payload)
    segments = payload.get("segments")
    timestamped_lines: list[str] = []
    timestamped_lines_available = 0
    if not isinstance(segments, list):
        raise ValueError(
            "TIMESTAMP_TRANSCRIPT_REJECTED segments_invalid: segments must be a non-empty list."
        )

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
        text = str(row.get("text") or "").strip()
        label = text if text else "(no voiced text)"
        timestamped_lines_available += 1
        if len(timestamped_lines) < _MAX_SEGMENT_LINES:
            timestamped_lines.append(f"[{_format_ts(start)} -> {_format_ts(end)}] {label}")

    if not timestamped_lines:
        raise ValueError(
            "TIMESTAMP_TRANSCRIPT_REJECTED no_prompt_lines: No timestamped Whisper lines "
            "could be built despite segments being present."
        )

    transcript = "\n".join(timestamped_lines)
    chars_before_truncation = len(transcript)
    truncated_by_chars = len(transcript) > _MAX_TRANSCRIPT_CHARS
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        transcript = (
            transcript[:_MAX_TRANSCRIPT_CHARS]
            + "\n\n[Transcript truncated for length; select only from visible timestamped lines.]"
        )
    stats: dict[str, object] = {
        "max_transcript_chars": _MAX_TRANSCRIPT_CHARS,
        "max_segment_lines": _MAX_SEGMENT_LINES,
        "segments_count": len(payload.get("segments") or []),
        "timestamped_lines_available": timestamped_lines_available,
        "timestamped_lines_used": len(timestamped_lines),
        "chars_before_truncation": chars_before_truncation,
        "returned_text_chars": len(transcript),
        "truncated_by_segment_limit": timestamped_lines_available > len(timestamped_lines),
        "truncated_by_char_limit": truncated_by_chars,
    }
    write_debug_mode(
        "select-clip",
        "H1-untimed-transcript",
        "select_clip.py:_load_transcript",
        "loaded transcript payload characteristics",
        {
            "has_text": isinstance(payload.get("full_text"), str),
            "has_segments": isinstance(payload.get("segments"), list),
            **stats,
        },
    )
    return transcript, stats


def _load_transcript(path: str) -> str:
    transcript, _ = _load_transcript_with_stats(path)
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

    raw_video_duration = selection_options.get("video_duration_sec")
    try:
        video_duration_sec = float(raw_video_duration)
    except (TypeError, ValueError):
        raise ValueError(
            "VIDEO_DURATION_REQUIRED: selection_options must include numeric "
            "`video_duration_sec` from ffprobe."
        ) from None
    if video_duration_sec <= 0:
        raise ValueError(
            "VIDEO_DURATION_REQUIRED: video_duration_sec must be > 0."
        )

    duration_constraint = (
        f"- The source video is exactly {video_duration_sec:.3f} seconds long. "
        f"Every `start` and `end` must satisfy 0 \u2264 start < end \u2264 {video_duration_sec:.3f}. "
        "Selecting any timestamp beyond this is invalid and the clip will be discarded.\n"
    )

    try:
        timeline_offset_sec = float(selection_options.get("timeline_offset_sec") or 0)
    except (TypeError, ValueError):
        timeline_offset_sec = 0.0
    if timeline_offset_sec < 0:
        timeline_offset_sec = 0.0

    chunk_intro = ""
    if selection_options.get("is_chunk_slice"):
        chunk_intro = (
            "- **Slice context**: This transcript is one contiguous slice of a longer recording — "
            "bracket timestamps are measured from the slice start (not necessarily 00:00:00 "
            "in the published video filename).\n"
        )

    duration_callout = (
        f"## Duration (primary constraint — obey before all else)\n"
        f"- Select **only** clips between **{min_duration_sec:g}** and **{max_duration_sec:g}** seconds long "
        f"(inclusive), where duration = end time minus start time in **seconds**.\n"
        f"- **Select only clips that are between {min_duration_sec:g} and {max_duration_sec:g} seconds long.**\n"
        "- Use only timestamps copied from the transcript brackets; after choosing `start` and `end`, "
        "recompute duration and drop or adjust the clip if it falls outside this window.\n\n"
    )

    user_prompt = f"""Analyze the timestamped transcript and return the BEST up to {max_clips} clips for short-form video.

{duration_callout}{chunk_intro}
## What makes a high-quality clip
1. **Hook (first seconds)**: The `start` timestamp must begin at a strong, complete spoken line—curiosity, tension, contrarian claim, or a crisp promise. Do not start mid-thought, mid-word, or on filler ("so", "um", "anyway", "like", "you know").
2. **Standalone value**: A viewer who knows nothing should still get a complete mini-idea (setup → payoff or one tight lesson).
3. **Engagement**: Contrast, specificity, emotion, story beat, or a clear actionable insight—not generic platitudes.
4. **Minimal filler**: Prefer dense segments; penalize long hedging, repetition, or off-topic ramble inside the window.

## Hard constraints
{duration_constraint}- **`start` and `end` must be copied verbatim** from the `[HH:MM:SS.sss -> HH:MM:SS.sss]` brackets — never invent timestamps absent from those bracket headers.
- Return **1** to **{max_clips}** clips in the `clips` array (fewer is fine if the transcript cannot support more strong clips).
- Each clip duration must be between **{min_duration_sec:g}** and **{max_duration_sec:g}** seconds (inclusive), based on `start` and `end` — same bounds as the duration section above; these are enforced in your output before any server-side filtering.
- `start` and `end` must use the **HH:MM:SS.sss** format from the bracketed transcript lines. Use the exact start and end timestamps shown there; do not round.
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
      "start": "HH:MM:SS.sss",
      "end": "HH:MM:SS.sss",
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

    cfg_model_raw = (
        config.get("models", {}).get("selection_model", "gpt-4o-mini") or "gpt-4o-mini"
    )
    opt_raw = selection_options.get("selection_model")
    selection_model = str(opt_raw).strip() if opt_raw not in (None, "") else ""
    if not selection_model:
        selection_model = str(cfg_model_raw).strip() or "gpt-4o-mini"
    _api_key = os.environ.get("OPENAI_API_KEY")
    if not _api_key or not str(_api_key).strip():
        raise ValueError("OPENAI_API_KEY is not set. Export it before running selection.")

    try:
        response = client.chat.completions.create(
            model=selection_model,
            messages=[
                {"role": "system", "content": _build_selection_system(min_duration_sec, max_duration_sec)},
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
    write_debug_mode(
        "select-clip",
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
    write_debug_mode(
        "select-clip",
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
    overlap_floor = max(0.5, float(min_duration_sec) * 0.35)
    processed = postprocess_segments(
        segments,
        max_clips=max_clips,
        min_duration_sec=min_duration_sec,
        max_duration_sec=max_duration_sec,
        max_overlap_sec=max_overlap_sec,
        video_duration_sec=video_duration_sec,
        duration_policy="llm_primary",
        overlap_min_duration_sec=overlap_floor,
    )

    if timeline_offset_sec > 0:
        processed = shift_segments_wallclock(processed, timeline_offset_sec)
    # #region agent log
    write_debug_mode(
        "select-clip",
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
    write_debug_mode(
        "select-clip",
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
        raise ValueError(
            "SELECTOR_REJECTED_AFTER_POSTFILTER: No clips survived duration, overlap, and "
            "in-bounds filtering — broaden the transcript or loosen selection bounds."
        )
    return processed


def run_selection_with_metadata(
    transcript_path: str, selection_options: dict | None = None
) -> tuple[list[dict], dict[str, object]]:
    path = os.path.abspath(transcript_path)
    if not os.path.exists(path):
        raise ValueError(f"Transcript file not found: {path}")

    _api_key = os.environ.get("OPENAI_API_KEY")
    if not _api_key or not str(_api_key).strip():
        raise ValueError("OPENAI_API_KEY is not set. Export it before running selection.")
    client = OpenAI(api_key=_api_key)

    transcript, prompt_stats = _load_transcript_with_stats(path)
    return _select_segments(transcript, selection_options or {}, client), prompt_stats


def run_selection(transcript_path: str, selection_options: dict | None = None) -> list[dict]:
    processed, _ = run_selection_with_metadata(transcript_path, selection_options)
    return processed


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) < 1:
            raise ValueError("No transcript path provided")
        selection_options = {}
        if len(args) >= 2:
            selection_options = json.loads(args[1])
        processed, prompt_stats = run_selection_with_metadata(args[0], selection_options)
        print(make_script_success("select_clip", clips=processed, selector_prompt=prompt_stats))
        return 0
    except Exception as e:
        print(f"[ERROR] Selection failed: {e}", file=sys.stderr)
        print(make_script_error("select_clip", str(e)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
