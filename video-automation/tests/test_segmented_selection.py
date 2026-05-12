from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "server"))
SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
for path in (SERVER_DIR, SCRIPTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import app as server_app  # noqa: E402


def _payload(segment_count: int, *, text_size: int = 20, step: float = 10.0) -> dict:
    text = "x" * text_size
    return {
        "segments": [
            {"start": i * step, "end": i * step + 5.0, "text": text}
            for i in range(segment_count)
        ]
    }


def test_short_transcript_uses_single_original_selector_window():
    windows, summary = server_app._plan_selector_windows(_payload(3))

    assert summary["segmented"] is False
    assert summary["window_count"] == 1
    assert windows[0]["uses_original_transcript"] is True
    assert len(windows[0]["segments"]) == 3


def test_long_transcript_splits_into_multiple_selector_windows():
    windows, summary = server_app._plan_selector_windows(_payload(901))

    assert summary["segmented"] is True
    assert summary["window_count"] == 3
    assert [len(w["segments"]) for w in windows] == [450, 450, 1]
    assert all(len(w["segments"]) <= 450 for w in windows)


def test_segmented_selection_preserves_absolute_timestamps(
    monkeypatch, tmp_path: Path
):
    transcript_path = tmp_path / "full.json"
    transcript_path.write_text(json.dumps(_payload(451)), encoding="utf-8")
    starts_seen: list[float] = []

    def fake_run(cmd, capture_output, text):
        opts = json.loads(cmd[-1])
        offset = float(opts.get("timeline_offset_sec") or 0.0)
        starts_seen.append(offset)
        start_h = int(offset // 3600)
        start_m = int((offset % 3600) // 60)
        start_s = int(offset % 60)
        end = offset + 30.0
        end_h = int(end // 3600)
        end_m = int((end % 3600) // 60)
        end_s = int(end % 60)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "script": "select_clip",
                    "clips": [
                        {
                            "start": f"{start_h:02d}:{start_m:02d}:{start_s:02d}.000",
                            "end": f"{end_h:02d}:{end_m:02d}:{end_s:02d}.000",
                            "scores": {"hook_strength": 8},
                        }
                    ],
                    "selector_prompt": {
                        "truncated_by_segment_limit": False,
                        "truncated_by_char_limit": False,
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(server_app.subprocess, "run", fake_run)
    candidates, summary = server_app._select_candidates_from_transcript(
        script_select="/fake/select_clip.py",
        transcript_path=str(transcript_path),
        transcript_payload=_payload(451),
        temp_root=str(tmp_path),
        filename="source",
        job_id="job_20260512T120000Z_abcdef12",
        max_clips=3,
        min_duration_sec=5.0,
        max_duration_sec=60.0,
        max_overlap_sec=2.0,
        include_reasons=False,
        include_clip_metadata=True,
        selection_model_used="gpt-test",
        source_video_duration_sec=5000.0,
        selector_video_duration_sec=5000.0,
        selector_window_paths=[],
        warnings=[],
    )

    assert summary["segmented"] is True
    assert summary["selector_call_count"] == 2
    assert starts_seen == [0.0, 4500.0]
    assert candidates[0]["start"] == "00:00:00"
    assert candidates[1]["start"] == "01:15:00"
    assert candidates[1]["end"] == "01:15:30"


def test_aggregated_candidates_are_ranked_deduped_and_limited():
    candidates = [
        {
            "start": "00:00:10",
            "end": "00:00:40",
            "scores": {"hook_strength": 6, "engagement_potential": 6},
        },
        {
            "start": "00:00:10.500",
            "end": "00:00:40.500",
            "scores": {"hook_strength": 10, "engagement_potential": 10},
        },
        {
            "start": "00:02:00",
            "end": "00:02:30",
            "scores": {"hook_strength": 8, "engagement_potential": 8},
        },
    ]

    out = server_app._aggregate_selector_candidates(
        candidates,
        max_clips=2,
        min_duration_sec=20.0,
        max_duration_sec=60.0,
        max_overlap_sec=2.0,
        video_duration_sec=300.0,
    )

    assert len(out) == 2
    assert out[0]["start"] == "00:00:10.500"
    assert out[1]["start"] == "00:02:00"


def test_selector_truncation_is_reported_as_warning():
    warnings: list[dict[str, object]] = []
    did_warn = server_app._record_selector_prompt_warning(
        warnings=warnings,
        label="window_0",
        prompt_stats={
            "truncated_by_segment_limit": True,
            "truncated_by_char_limit": False,
            "timestamped_lines_available": 600,
            "timestamped_lines_used": 500,
        },
    )

    assert did_warn is True
    assert len(warnings) == 1
    assert warnings[0]["category"] == "selector_prompt_truncated"
    assert warnings[0]["details"]["selector_call"] == "window_0"
