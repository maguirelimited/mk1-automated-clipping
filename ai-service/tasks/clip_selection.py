from __future__ import annotations

from copy import deepcopy
from typing import Any

from model_client import OllamaModelClient
from output_validation import validate_model_output, validate_with_one_repair
from task_router import AITaskError
from transcript_chunking import (
    ChunkingOptions,
    TranscriptChunkingError,
    build_section_clip_selection_prompt,
    chunk_transcript_context,
)


DEFAULT_FINAL_REASON = "Selected strongest candidates across bounded transcript sections."
NO_CANDIDATES_REASON = "No valid clip candidates found across transcript sections."

# 0-10 rubric components every candidate must score. `overall` is the model's
# judgement after weighing the others, not a forced average.
RUBRIC_COMPONENTS = (
    "hook_strength",
    "standalone_context",
    "insight_value",
    "retention_potential",
    "natural_ending",
    "overall",
)


def run_clip_selection(
    *,
    payload: dict[str, Any],
    settings: Any,
    prompt_text: str,
    schema: dict[str, Any],
    model_client: Any | None = None,
) -> dict[str, Any]:
    source_context = _source_context_from_payload(payload)
    options = _chunking_options_from_source(source_context)
    try:
        sections = chunk_transcript_context(source_context, options)
    except TranscriptChunkingError as exc:
        raise AITaskError(exc.code, exc.message, status_code=400) from exc

    try:
        client = model_client or OllamaModelClient(settings)
    except Exception as exc:
        raise AITaskError("MODEL_CALL_FAILED", f"Could not initialise model client: {exc}", status_code=502) from exc
    valid_candidates: list[dict[str, Any]] = []
    valid_section_count = 0
    failed_section_count = 0
    model_failure_count = 0

    for section in sections:
        section_prompt = build_section_clip_selection_prompt(
            prompt_text,
            section,
            options.candidate_cap_per_section,
        )
        try:
            response = client.generate(section_prompt)
        except Exception:
            failed_section_count += 1
            model_failure_count += 1
            continue
        if getattr(response, "error", None):
            failed_section_count += 1
            model_failure_count += 1
            continue

        validation = validate_model_output(getattr(response, "text", None), schema)
        if not validation.ok:
            try:
                validation = validate_with_one_repair(
                    raw_text=getattr(response, "text", None),
                    schema=schema,
                    model_client=client,
                )
            except Exception:
                failed_section_count += 1
                continue
        if not validation.ok or not isinstance(validation.parsed_output, dict):
            failed_section_count += 1
            continue

        valid_section_count += 1
        section_output = validation.parsed_output
        for candidate in section_output.get("candidates") or []:
            sane = _sane_candidate(candidate, section, source_context)
            if sane is not None:
                valid_candidates.append(sane)

    if valid_candidates:
        final_candidates = _aggregate_candidates(valid_candidates, options.final_candidate_cap)
        return {
            "usable": True,
            "confidence": _aggregate_confidence(final_candidates),
            "reason": DEFAULT_FINAL_REASON,
            "candidates": final_candidates,
        }

    if valid_section_count > 0:
        return {
            "usable": False,
            "confidence": 0.0,
            "reason": NO_CANDIDATES_REASON,
            "candidates": [],
        }

    if failed_section_count > 0 and model_failure_count == failed_section_count:
        raise AITaskError("MODEL_CALL_FAILED", "All section model calls failed.", status_code=502)

    raise AITaskError("MODEL_OUTPUT_INVALID", "No valid section model outputs were produced.", status_code=502)


def _source_context_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    task_input = payload.get("input")
    if not isinstance(task_input, dict):
        raise AITaskError("INVALID_TASK_INPUT", "clip_selection input must be an object.", status_code=400)
    source = deepcopy(task_input)
    source.setdefault("job_id", payload.get("job_id"))
    if payload.get("funnel_id") and not source.get("funnel_id"):
        source["funnel_id"] = payload.get("funnel_id")
    return source


def _chunking_options_from_source(source_context: dict[str, Any]) -> ChunkingOptions:
    raw_options = source_context.get("chunking_options")
    if not isinstance(raw_options, dict):
        return ChunkingOptions()
    return ChunkingOptions(
        section_size_seconds=float(raw_options.get("section_size_seconds", 300.0)),
        section_overlap_seconds=float(raw_options.get("section_overlap_seconds", 20.0)),
        candidate_cap_per_section=int(raw_options.get("candidate_cap_per_section", 2)),
        final_candidate_cap=int(raw_options.get("final_candidate_cap", 8)),
        preferred_clip_length_seconds=raw_options.get("preferred_clip_length_seconds"),
    )


def _sane_candidate(
    candidate: Any,
    section: dict[str, Any],
    source_context: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    start = candidate.get("start_seconds")
    end = candidate.get("end_seconds")
    reason = candidate.get("reason")
    scores = _sane_scores(candidate.get("scores"))
    duration = source_context.get("duration_seconds")
    if not _is_number(start) or not _is_number(end) or scores is None:
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None

    start_f = float(start)
    end_f = float(end)
    duration_f = float(duration) if _is_number(duration) else 0.0
    if start_f < 0 or end_f <= start_f or end_f > duration_f:
        return None
    if not _overlaps_section(start_f, end_f, section):
        return None

    return {
        "start_seconds": start_f,
        "end_seconds": end_f,
        "scores": scores,
        "reason": reason.strip(),
    }


def _sane_scores(scores: Any) -> dict[str, float] | None:
    if not isinstance(scores, dict):
        return None
    clean: dict[str, float] = {}
    for component in RUBRIC_COMPONENTS:
        value = scores.get(component)
        if not _is_number(value):
            return None
        value_f = float(value)
        if value_f < 0 or value_f > 10:
            return None
        clean[component] = value_f
    return clean


def _overlaps_section(start_seconds: float, end_seconds: float, section: dict[str, Any]) -> bool:
    section_start = section.get("section_start")
    section_end = section.get("section_end")
    if not _is_number(section_start) or not _is_number(section_end):
        return True
    return start_seconds < float(section_end) and end_seconds > float(section_start)


def _aggregate_candidates(candidates: list[dict[str, Any]], final_candidate_cap: int) -> list[dict[str, Any]]:
    return sorted(candidates, key=_overall_score, reverse=True)[:final_candidate_cap]


def _aggregate_confidence(candidates: list[dict[str, Any]]) -> float:
    if not candidates:
        return 0.0
    top_score = max(_overall_score(candidate) for candidate in candidates)
    return round(min(max(top_score / 10.0, 0.0), 1.0), 4)


def _overall_score(candidate: dict[str, Any]) -> float:
    scores = candidate.get("scores")
    overall = scores.get("overall") if isinstance(scores, dict) else None
    return float(overall) if _is_number(overall) else 0.0


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
