from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import transcript_sectioning as sectioning  # noqa: E402


def _segments(count: int, *, step: float = 10.0, duration: float = 10.0) -> list[dict]:
    return [
        {
            "start": index * step,
            "end": index * step + duration,
            "text": f"Segment {index}",
        }
        for index in range(count)
    ]


def _transcript(count: int, *, step: float = 10.0, duration: float = 10.0) -> dict:
    return {"segments": _segments(count, step=step, duration=duration)}


def _test_config() -> sectioning.TranscriptSectioningConfig:
    return sectioning.TranscriptSectioningConfig(
        target_section_duration_sec=60.0,
        max_section_duration_sec=90.0,
        overlap_sec=20.0,
        min_section_duration_sec=15.0,
    )


def test_short_transcript_creates_one_valid_section():
    sections = sectioning.section_transcript(
        _transcript(3),
        source_transcript_path="/tmp/transcript.json",
        config=_test_config(),
    )

    assert len(sections) == 1
    assert sections[0]["section_id"] == "section_0001"
    assert "Segment 0" in sections[0]["text"]
    sectioning.validate_transcript_sections(sections, config=_test_config())


def test_long_transcript_creates_multiple_sections():
    sections = sectioning.section_transcript(
        _transcript(14),
        source_transcript_path="/tmp/transcript.json",
        config=_test_config(),
    )

    assert len(sections) > 1
    assert all(section["duration_sec"] <= 90.001 for section in sections)


def test_section_ids_are_stable_and_ordered():
    first = sectioning.section_transcript(_transcript(14), config=_test_config())
    second = sectioning.section_transcript(_transcript(14), config=_test_config())

    assert [s["section_id"] for s in first] == [s["section_id"] for s in second]
    assert [s["section_id"] for s in first] == [
        f"section_{index:04d}" for index in range(1, len(first) + 1)
    ]


def test_section_start_end_duration_values_are_valid():
    sections = sectioning.section_transcript(_transcript(8), config=_test_config())

    for section in sections:
        assert section["end_sec"] > section["start_sec"]
        assert section["duration_sec"] == pytest.approx(
            section["end_sec"] - section["start_sec"]
        )


def test_source_segment_references_are_preserved():
    sections = sectioning.section_transcript(_transcript(8), config=_test_config())
    refs = sections[0]["source_segment_refs"]

    assert refs[0] == {"segment_index": 0, "start_sec": 0.0, "end_sec": 10.0}
    assert all(set(ref) == {"segment_index", "start_sec", "end_sec"} for ref in refs)


def test_overlap_metadata_is_present():
    sections = sectioning.section_transcript(_transcript(14), config=_test_config())

    for section in sections:
        assert set(section["overlap"]) == {
            "has_previous_overlap",
            "has_next_overlap",
            "overlap_before_sec",
            "overlap_after_sec",
        }


def test_neighbouring_long_sections_overlap_when_configured():
    sections = sectioning.section_transcript(_transcript(14), config=_test_config())

    assert sections[0]["overlap"]["has_next_overlap"] is True
    assert sections[1]["overlap"]["has_previous_overlap"] is True
    assert sections[0]["overlap"]["overlap_after_sec"] > 0
    assert sections[1]["overlap"]["overlap_before_sec"] > 0
    assert sections[1]["start_sec"] < sections[0]["end_sec"]


def test_empty_transcript_fails_cleanly():
    with pytest.raises(sectioning.TranscriptSectioningError) as exc:
        sectioning.section_transcript({"segments": []}, config=_test_config())

    assert exc.value.code == "EMPTY_TRANSCRIPT"


def test_malformed_segment_timestamps_fail_cleanly():
    with pytest.raises(sectioning.TranscriptSectioningError) as exc:
        sectioning.section_transcript(
            {"segments": [{"start": 10.0, "end": 5.0, "text": "bad"}]},
            config=_test_config(),
        )

    assert exc.value.code == "INVALID_SEGMENT_TIMESTAMPS"
    assert "end must be greater" in str(exc.value)


def test_missing_segment_text_is_handled_safely():
    transcript = {
        "segments": [
            {"start": 0.0, "end": 10.0},
            {"start": 10.0, "end": 20.0, "text": "Useful words"},
            {"start": 20.0, "end": 30.0, "text": "   "},
        ]
    }

    sections = sectioning.section_transcript(transcript, config=_test_config())

    assert len(sections) == 1
    assert "Useful words" in sections[0]["text"]
    assert sections[0]["source_segment_refs"] == [
        {"segment_index": 1, "start_sec": 10.0, "end_sec": 20.0}
    ]


def test_sections_validate_successfully():
    sections = sectioning.section_transcript(_transcript(14), config=_test_config())

    sectioning.validate_transcript_sections(sections, config=_test_config())
    sectioning.validate_transcript_section(sections[0], config=_test_config())


def test_invalid_section_duration_fails_validation():
    sections = sectioning.section_transcript(_transcript(3), config=_test_config())
    broken = copy.deepcopy(sections[0])
    broken["duration_sec"] = broken["duration_sec"] + 1

    with pytest.raises(sectioning.TranscriptSectioningError) as exc:
        sectioning.validate_transcript_section(broken, config=_test_config())

    assert "duration_sec must match" in str(exc.value)


def test_transcript_sections_write_read_helper_works(tmp_path: Path):
    sections = sectioning.section_transcript(
        _transcript(3),
        source_transcript_path="/tmp/transcript.json",
        config=_test_config(),
    )
    artifact = sectioning.build_transcript_sections_artifact(
        job_id="job_123",
        source_transcript_path="/tmp/transcript.json",
        sections=sections,
        sectioning_config=_test_config(),
        created_at="2026-06-30T12:00:00+00:00",
    )

    path = sectioning.write_transcript_sections(str(tmp_path), artifact)
    reloaded = sectioning.read_transcript_sections(path)

    assert Path(path).name == sectioning.TRANSCRIPT_SECTIONS_FILENAME
    assert reloaded["schema_version"] == sectioning.TRANSCRIPT_SECTIONS_SCHEMA_VERSION
    assert reloaded["sections"][0]["section_id"] == "section_0001"


def test_sectioning_does_not_require_a_real_video_file(tmp_path: Path):
    sections = sectioning.section_transcript_file(
        _write_transcript_file(tmp_path, _transcript(2)),
        config=_test_config(),
    )

    assert len(sections) == 1


def test_sectioning_does_not_call_the_ai_service(monkeypatch: pytest.MonkeyPatch):
    original_import = __import__

    def guarded_import(name, *args, **kwargs):
        if name in {"ai_service_client", "openai"}:
            raise AssertionError("AI service should not be imported during sectioning")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded_import)

    sections = sectioning.section_transcript(_transcript(2), config=_test_config())

    assert len(sections) == 1


def _write_transcript_file(tmp_root: Path, payload: dict) -> str:
    tmp_root.mkdir(parents=True, exist_ok=True)
    path = tmp_root / "mk1_sectioning_test_transcript.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)
