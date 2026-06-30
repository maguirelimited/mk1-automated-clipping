import json
import os
import sys
from pathlib import Path

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from chunk_pipeline import merge_whisper_json_files, plan_wallclock_chunks
from pipeline_utils import parse_time_to_seconds, shift_segments_wallclock


def test_plan_wallclock_chunks_covers_duration():
    specs = plan_wallclock_chunks(3700.0, 1800.0)
    assert len(specs) == 3
    assert specs[0] == (0.0, 1800.0)
    assert specs[1] == (1800.0, 1800.0)
    assert specs[2] == (3600.0, 100.0)


def test_merge_whisper_offsets_segment_times(tmp_path: Path):
    a = {
        "text": "hello",
        "segments": [{"start": 0.5, "end": 2.5, "text": "hey"}],
    }
    b = {
        "text": "world",
        "segments": [{"start": 10.0, "end": 12.0, "text": "yo"}],
    }
    pa = tmp_path / "a.json"
    pb = tmp_path / "b.json"
    pa.write_text(json.dumps(a), encoding="utf-8")
    pb.write_text(json.dumps(b), encoding="utf-8")

    merged = merge_whisper_json_files([(str(pa), 0.0), (str(pb), 100.0)], 200.0)
    segs = merged["segments"]
    assert len(segs) == 2
    assert segs[0]["start"] == 0.5
    assert segs[1]["start"] == 110.0
    assert merged["duration"] == 200.0


def test_merge_whisper_sorts_boundary_overlap(tmp_path: Path):
    """Chunk boundaries can produce out-of-order segments when WhisperX re-segments
    the overlap region.  The merged output must be monotonically ordered by start
    so that transcript_sectioning does not raise INVALID_SEGMENT_ORDER."""
    # Simulate two chunks whose boundary segments interleave:
    #   chunk 0 (offset 0):    seg at 2401.077
    #   chunk 1 (offset 2400): seg at 0.191  → wall-clock 2400.191
    # Without sorting, 2401.077 comes before 2400.191.
    chunk0 = {
        "text": "But that is venture money.",
        "segments": [{"start": 2401.077, "end": 2402.841, "text": "But that is venture money."}],
    }
    chunk1 = {
        "text": "Which is crazy.",
        "segments": [{"start": 0.191, "end": 4.099, "text": "Which is crazy."}],
    }
    p0 = tmp_path / "c0.json"
    p1 = tmp_path / "c1.json"
    p0.write_text(json.dumps(chunk0), encoding="utf-8")
    p1.write_text(json.dumps(chunk1), encoding="utf-8")

    merged = merge_whisper_json_files([(str(p0), 0.0), (str(p1), 2400.0)], 4083.0)
    segs = merged["segments"]
    starts = [s["start"] for s in segs]
    assert starts == sorted(starts), f"segments not sorted by start: {starts}"
    # sequential ids must be reassigned after sort
    assert [s["id"] for s in segs] == list(range(len(segs)))


def test_shift_segments_wallclock():
    seg = [{"start": "00:01:05.000", "end": "00:01:10.500", "duration_sec": 5.5}]
    shifted = shift_segments_wallclock(seg, 3600.0)
    assert parse_time_to_seconds(shifted[0]["start"]) == 3600 + 65
    assert parse_time_to_seconds(shifted[0]["end"]) == 3600 + 70.5
