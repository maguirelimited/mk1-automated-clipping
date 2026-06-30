from __future__ import annotations

import sys
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from transcript_chunking import (  # noqa: E402
    DEFAULT_CANDIDATE_CAP_PER_SECTION,
    DEFAULT_FINAL_CANDIDATE_CAP,
    DEFAULT_SECTION_OVERLAP_SECONDS,
    DEFAULT_SECTION_SIZE_SECONDS,
    ChunkingOptions,
    TranscriptChunkingError,
    apply_default_chunking_options,
    build_section_clip_selection_prompt,
    build_section_scoring_config,
    chunk_transcript_context,
    validate_chunking_options,
)
from transcript_context import validate_transcript_context  # noqa: E402


def _source_context() -> dict:
    return {
        "job_id": "job_123",
        "video_title": "Example Podcast Episode",
        "source_channel": "Example Channel",
        "funnel_id": "business_ai",
        "duration_seconds": 130,
        "transcript": "Full fallback transcript.",
        "segments": [
            {"start": 0.0, "end": 10.0, "text": "Opening segment."},
            {"start": 45.0, "end": 55.0, "text": "First useful point."},
            {"start": 55.0, "end": 65.0, "text": "Overlap useful point."},
            {"start": 90.0, "end": 100.0, "text": "Second useful point."},
        ],
        "speakers": [],
        "previous_context_summary": "Earlier context.",
        "funnel_rules": {
            "target_audience": "business/productivity audience",
            "preferred_clip_length_seconds": [35, 75],
            "avoid": ["inside jokes"],
        },
    }


def main() -> None:
    defaults = apply_default_chunking_options()
    assert defaults.section_size_seconds == DEFAULT_SECTION_SIZE_SECONDS
    assert defaults.section_overlap_seconds == DEFAULT_SECTION_OVERLAP_SECONDS
    assert defaults.candidate_cap_per_section == DEFAULT_CANDIDATE_CAP_PER_SECTION
    assert defaults.final_candidate_cap == DEFAULT_FINAL_CANDIDATE_CAP

    try:
        validate_chunking_options(ChunkingOptions(section_size_seconds=20, section_overlap_seconds=20))
        raise AssertionError("expected invalid overlap")
    except TranscriptChunkingError as exc:
        assert exc.code == "INVALID_CHUNKING_OPTIONS"

    try:
        validate_chunking_options(ChunkingOptions(candidate_cap_per_section=4))
        raise AssertionError("expected invalid candidate cap")
    except TranscriptChunkingError as exc:
        assert exc.code == "INVALID_CHUNKING_OPTIONS"

    options = ChunkingOptions(section_size_seconds=60, section_overlap_seconds=20, candidate_cap_per_section=2)
    sections = chunk_transcript_context(_source_context(), options)
    assert len(sections) == 3
    assert sections[0]["section_start"] == 0.0
    assert sections[0]["section_end"] == 60.0
    assert sections[1]["section_start"] == 40.0
    assert sections[1]["section_end"] == 100.0
    assert sections[2]["section_start"] == 80.0
    assert sections[2]["section_end"] == 130.0
    assert all(section["section_end"] <= 130 for section in sections)
    assert all(validate_transcript_context(section).ok for section in sections)
    assert sections[0]["funnel_rules"]["avoid"] == ["inside jokes"]
    assert sections[0]["previous_context_summary"] == "Earlier context."

    empty_window_source = _source_context()
    empty_window_source["segments"] = [{"start": 0.0, "end": 5.0, "text": "Only first window has text."}]
    sections = chunk_transcript_context(empty_window_source, options)
    assert len(sections) == 1
    assert sections[0]["section_start"] == 0.0

    fallback_source = _source_context()
    del fallback_source["segments"]
    sections = chunk_transcript_context(fallback_source, options)
    assert len(sections) == 1
    assert sections[0]["section_start"] == 0.0
    assert sections[0]["section_end"] == 130.0
    assert sections[0]["transcript"] == "Full fallback transcript."
    assert validate_transcript_context(sections[0]).ok

    scoring = build_section_scoring_config(sections[0], options)
    assert scoring.candidate_cap_per_section == 2
    assert scoring.preferred_clip_length_seconds == [35.0, 75.0]

    prompt = build_section_clip_selection_prompt("BASE PROMPT", sections[0], 2)
    assert "BASE PROMPT" in prompt
    assert "Return at most 2 candidate" in prompt
    assert "Preferred clip length in seconds: 35.0-75.0" in prompt
    assert "UNTRUSTED" in prompt
    assert "<transcript>" in prompt
    assert "Return only JSON matching the schema" in prompt

    print("transcript_chunking_smoke_ok")


if __name__ == "__main__":
    main()
