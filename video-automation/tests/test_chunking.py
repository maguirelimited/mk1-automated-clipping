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


def test_shift_segments_wallclock():
    seg = [{"start": "00:01:05.000", "end": "00:01:10.500", "duration_sec": 5.5}]
    shifted = shift_segments_wallclock(seg, 3600.0)
    assert parse_time_to_seconds(shifted[0]["start"]) == 3600 + 65
    assert parse_time_to_seconds(shifted[0]["end"]) == 3600 + 70.5
