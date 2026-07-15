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
from ai_service_client import (
    AiServiceConfigError,
    request_clip_selection,
)
from ai_settings import resolve_clip_selection_backend

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


def _resolve_selection_backend(selection_options: dict) -> str:
    """Resolve which clip-selection judgement backend to use.

    Default is ``ai_service`` (local ai-service via Ollama). Set ``openai`` to
    use the legacy inline OpenAI path instead. Resolution order: per-run
    ``selection_backend`` option -> Ops UI saved setting (controls.json) ->
    ``CLIP_SELECTION_BACKEND`` env var -> default ``ai_service``.
    There is no cloud fallback in MK1: when ``ai_service`` is selected, OpenAI is
    not used.
    """
    raw = selection_options.get("selection_backend")
    per_run = str(raw).strip() if isinstance(raw, str) and raw.strip() else None
    return resolve_clip_selection_backend(per_run)


def _build_ai_service_input(
    transcript_path: str,
    selection_options: dict,
    *,
    job_id: str,
    duration_seconds: float,
) -> dict:
    """Build the ai-service clip_selection `input` package from a transcript file."""
    payload = normalize_transcript_payload(transcript_path)
    require_timed_transcript_payload(payload)

    segments: list[dict] = []
    for row in payload.get("segments") or []:
        if not isinstance(row, dict):
            continue
        try:
            start = float(row.get("start"))
            end = float(row.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        segments.append({"start": start, "end": end, "text": str(row.get("text") or "").strip()})

    transcript_text = str(payload.get("full_text") or payload.get("text") or "").strip()
    if not transcript_text:
        transcript_text = "\n".join(seg["text"] for seg in segments if seg["text"]).strip()

    min_duration = float(selection_options.get("min_duration_sec", 5))
    max_duration = float(selection_options.get("max_duration_sec", 30))

    # Note: ai-service constrains final_candidate_cap to 5..10, so it is NOT
    # derived from max_clips (which can be < 5). ai-service uses its default cap;
    # video-automation enforces the real max_clips during postprocess_segments.
    task_input: dict = {
        "job_id": job_id,
        "duration_seconds": duration_seconds,
        "transcript": transcript_text,
        "segments": segments,
        "funnel_rules": {
            "preferred_clip_length_seconds": [min_duration, max_duration],
        },
        "chunking_options": {
            "preferred_clip_length_seconds": [min_duration, max_duration],
        },
    }
    funnel_id = selection_options.get("funnel_id")
    if isinstance(funnel_id, str) and funnel_id.strip():
        task_input["funnel_id"] = funnel_id.strip()
    return task_input


def _ai_service_candidates_to_segments(candidates: list[dict]) -> list[dict]:
    """Map ai-service clip candidates onto selector segment dicts (seconds).

    ai-service (clip_candidates_v2) scores each candidate against a 0-10 rubric
    and orders candidates by ``scores.overall``. ``pipeline_utils.postprocess_segments``
    re-ranks by its own ``scores`` dimension dict, so the ai-service overall score
    is projected onto those dimensions to preserve ai-service ranking through
    postprocessing. The legacy scalar ``score`` (clip_candidates_v1) is still
    accepted as a fallback.
    """
    segments: list[dict] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        start = candidate.get("start_seconds")
        end = candidate.get("end_seconds")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        segment: dict = {"start": float(start), "end": float(end)}
        reason = candidate.get("reason")
        if isinstance(reason, str) and reason.strip():
            segment["reason"] = reason.strip()
        overall = _ai_service_overall_score(candidate)
        if overall is not None:
            score_f = max(0.0, min(10.0, overall))
            segment["score"] = score_f
            segment["scores"] = {
                "hook_strength": score_f,
                "clarity_standalone": score_f,
                "engagement_potential": score_f,
                "minimal_filler": score_f,
            }
        segments.append(segment)
    return segments


def _ai_service_overall_score(candidate: dict) -> float | None:
    """Pull the candidate's overall 0-10 score from the v2 ``scores`` object,
    falling back to the legacy scalar ``score`` field for v1 candidates."""
    scores = candidate.get("scores")
    if isinstance(scores, dict):
        overall = scores.get("overall")
        if isinstance(overall, (int, float)) and not isinstance(overall, bool):
            return float(overall)
    legacy = candidate.get("score")
    if isinstance(legacy, (int, float)) and not isinstance(legacy, bool):
        return float(legacy)
    return None


def _select_segments_via_ai_service(
    transcript_path: str, selection_options: dict
) -> tuple[list[dict], dict[str, object]]:
    config = load_config()
    sel_cfg = config.get("selection", {}) if isinstance(config.get("selection"), dict) else {}
    max_clips = int(selection_options.get("max_clips", 5))
    min_duration_sec = float(
        selection_options.get("min_duration_sec", sel_cfg.get("min_clip_duration_sec", 5))
    )
    max_duration_sec = float(
        selection_options.get("max_duration_sec", sel_cfg.get("max_clip_duration_sec", 30))
    )
    max_overlap_sec = float(
        selection_options.get("max_overlap_sec", sel_cfg.get("max_overlap_sec", 2))
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
        raise ValueError("VIDEO_DURATION_REQUIRED: video_duration_sec must be > 0.")

    try:
        timeline_offset_sec = float(selection_options.get("timeline_offset_sec") or 0)
    except (TypeError, ValueError):
        timeline_offset_sec = 0.0
    if timeline_offset_sec < 0:
        timeline_offset_sec = 0.0

    job_id = str(selection_options.get("job_id") or "video-automation-selection").strip()
    try:
        task_input = _build_ai_service_input(
            transcript_path,
            {
                **selection_options,
                "min_duration_sec": min_duration_sec,
                "max_duration_sec": max_duration_sec,
                "max_clips": max_clips,
            },
            job_id=job_id,
            duration_seconds=video_duration_sec,
        )
    except AiServiceConfigError as exc:
        raise ValueError(f"AI_SERVICE_FAILED bad_request: {exc}") from exc

    result = request_clip_selection(
        job_id=job_id,
        task_input=task_input,
        funnel_id=selection_options.get("funnel_id"),
        model_preference=selection_options.get("selection_model"),
    )

    write_debug_mode(
        "select-clip",
        "H8-ai-service-selection",
        "select_clip.py:_select_segments_via_ai_service",
        "ai-service clip_selection outcome",
        result.summary(),
    )

    if result.busy:
        # AI_BUSY is retryable: video-automation owns the retry/job mechanism.
        raise ValueError(
            f"AI_SERVICE_BUSY: {result.error_message or 'Local AI model is busy.'}"
        )
    if result.no_clip:
        # Controlled no-clip outcome — never force a bad clip.
        raise ValueError(
            "SELECTOR_REJECTED_AFTER_POSTFILTER ai_service_no_clip: ai-service judged "
            "the transcript and found no strong standalone clip."
        )
    if result.ai_failure:
        raise ValueError(
            f"AI_SERVICE_FAILED {result.error_code or 'error'}: "
            f"{result.error_message or 'ai-service clip selection failed.'}"
        )

    segments = _ai_service_candidates_to_segments(result.candidates)
    if not segments:
        raise ValueError(
            "SELECTOR_REJECTED_AFTER_POSTFILTER ai_service_no_clip: ai-service returned "
            "no usable candidates."
        )

    overlap_floor = max(0.5, float(min_duration_sec) * 0.35)
    processed = postprocess_segments(
        normalize_segments(segments),
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
    if not processed:
        raise ValueError(
            "SELECTOR_REJECTED_AFTER_POSTFILTER ai_service_postfilter: no ai-service "
            "candidates survived duration/overlap/in-bounds filtering."
        )

    prompt_stats: dict[str, object] = {
        "selection_backend": "ai_service",
        "ai_service_request_id": result.request_id,
        "ai_service_candidate_count": len(result.candidates),
    }
    return processed, prompt_stats


def run_selection_with_metadata(
    transcript_path: str, selection_options: dict | None = None
) -> tuple[list[dict], dict[str, object]]:
    path = os.path.abspath(transcript_path)
    if not os.path.exists(path):
        raise ValueError(f"Transcript file not found: {path}")

    options = selection_options or {}
    if _resolve_selection_backend(options) == "ai_service":
        return _select_segments_via_ai_service(path, options)

    _api_key = os.environ.get("OPENAI_API_KEY")
    if not _api_key or not str(_api_key).strip():
        raise ValueError("OPENAI_API_KEY is not set. Export it before running selection.")
    client = OpenAI(api_key=_api_key)

    transcript, prompt_stats = _load_transcript_with_stats(path)
    return _select_segments(transcript, options, client), prompt_stats


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
