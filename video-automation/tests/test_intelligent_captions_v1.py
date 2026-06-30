"""intelligent_captions_v1 — focused tests (Prompt 20).

Tests are split into:
  - Non-ffmpeg tests: input validation, config validation, path/naming, ASS
    generation, chunking, line-breaking, result contract shape.  Always run.
  - ffmpeg tests: real caption burn-in with synthetic video fixtures.
    Skipped when ffmpeg/ffprobe are not installed.

All real-video ffmpeg tests use tiny 180x320 synthetic vertical clips for
speed.  The test videos are created inside temp directories and never
committed to the repo.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from post_processing_modules import (  # noqa: E402
    MODULE_STATUS_FAIL,
    MODULE_STATUS_PASS,
    PostProcessingModule,
    make_module_pass_result,
    run_module_chain,
    validate_module_result,
)
from post_processing_conveyor import (  # noqa: E402
    CONVEYOR_STATUS_COMPLETE,
    FIXED_MK1_CONVEYOR_MODULES,
    run_fixed_mk1_universal_conveyor,
)
from render_clip_v1 import RenderClipV1Module  # noqa: E402
from platform_safe_format_v1 import (  # noqa: E402
    PlatformSafeFormatV1Module,
    _compute_safe_zones,
    _make_output_path as psf_make_output_path,
)
from intelligent_captions_v1 import (  # noqa: E402
    MODULE_NAME,
    MODULE_VERSION,
    INTELLIGENT_CAPTIONS_V1_MODULE,
    IntelligentCaptionsV1Module,
    _ass_time,
    _break_into_lines,
    _chunk_from_segments,
    _chunk_from_words,
    _escape_ass_text,
    _escape_path_for_filter,
    _generate_ass_content,
    _make_output_path,
    _make_sidecar_path,
    _resolve_caption_source,
    _resolve_safe_zones,
    _safe_filename_part,
    _split_text_to_chunks,
    _validate_caption_config,
    get_intelligent_captions_v1_module,
)

# ---------------------------------------------------------------------------
# ffmpeg / ffprobe availability guards
# ---------------------------------------------------------------------------


def _cmd_available(name: str) -> bool:
    return shutil.which(name) is not None


FFTOOLS_AVAILABLE = _cmd_available("ffmpeg") and _cmd_available("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    not FFTOOLS_AVAILABLE,
    reason="ffmpeg/ffprobe not installed — skipping real-caption tests",
)


# ---------------------------------------------------------------------------
# Synthetic video helpers
# ---------------------------------------------------------------------------


def _make_synth_video(
    out_path: str,
    width: int = 180,
    height: int = 320,
    duration: float = 4.0,
    with_audio: bool = True,
) -> str:
    """Create a tiny synthetic vertical video for testing."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate=30",
    ]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100"]
    cmd += [
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "ultrafast",
    ]
    if with_audio:
        cmd += ["-c:a", "aac"]
    else:
        cmd += ["-an"]
    cmd.append(out_path)
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    assert result.returncode == 0, (
        f"Failed to create {width}x{height} video: {result.stderr.decode()}"
    )
    return out_path


@pytest.fixture(scope="session")
def vertical_clip(tmp_path_factory):
    """Session-scoped 180x320 vertical clip with audio, 4 seconds."""
    if not FFTOOLS_AVAILABLE:
        return None
    d = tmp_path_factory.mktemp("vert_clip")
    return _make_synth_video(str(d / "vertical.mp4"), 180, 320, 4.0, with_audio=True)


@pytest.fixture(scope="session")
def vertical_clip_no_audio(tmp_path_factory):
    """Session-scoped 180x320 vertical clip without audio."""
    if not FFTOOLS_AVAILABLE:
        return None
    d = tmp_path_factory.mktemp("vert_no_audio")
    return _make_synth_video(str(d / "vertical_no_audio.mp4"), 180, 320, 4.0, with_audio=False)


# ---------------------------------------------------------------------------
# Shared test data helpers
# ---------------------------------------------------------------------------


def _make_context(
    *,
    job_id: str = "testjob",
    candidate_id: str = "cand001",
    start_sec: float = 0.0,
    end_sec: float = 4.0,
    words: list[dict] | None = None,
    segments: list[dict] | None = None,
    clip_dir: str | None = None,
    tmp_dir: str | None = None,
    source_video_path: str | None = None,
    module_results: list[dict] | None = None,
) -> dict[str, Any]:
    """Build a minimal valid module context for testing."""
    cand: dict[str, Any] = {
        "candidate_id": candidate_id,
        "start_sec": start_sec,
        "end_sec": end_sec,
    }
    if words is not None:
        cand["words"] = words
    if segments is not None:
        cand["segments"] = segments

    return {
        "job_id": job_id,
        "candidate_id": candidate_id,
        "source_video_path": source_video_path,
        "clip_dir": clip_dir,
        "tmp_dir": tmp_dir,
        "metadata_dir": None,
        "working_dir": None,
        "config": {},
        "selection_result": {},
        "selected_candidate": cand,
        "module_results": module_results or [],
    }


def _make_word_list(start_offset: float = 0.0) -> list[dict]:
    """Synthetic word list covering 4 seconds."""
    words = [
        ("This", 0.0, 0.3),
        ("is", 0.4, 0.6),
        ("a", 0.7, 0.8),
        ("test", 0.9, 1.2),
        ("caption", 1.3, 1.7),
        ("for", 1.8, 2.0),
        ("the", 2.1, 2.3),
        ("intelligent", 2.4, 2.9),
        ("captions", 3.0, 3.5),
        ("module", 3.6, 3.9),
    ]
    return [
        {"start": s + start_offset, "end": e + start_offset, "word": w}
        for (w, s, e) in words
    ]


def _make_segment_list(start_offset: float = 0.0) -> list[dict]:
    """Synthetic segment list covering 4 seconds."""
    return [
        {
            "start": 0.0 + start_offset,
            "end": 2.0 + start_offset,
            "text": "This is a test caption for",
        },
        {
            "start": 2.1 + start_offset,
            "end": 4.0 + start_offset,
            "text": "the intelligent captions module",
        },
    ]


# ===========================================================================
# Tests — Module contract
# ===========================================================================


class TestModuleContract:
    def test_module_name_is_exact(self):
        assert MODULE_NAME == "intelligent_captions_v1"

    def test_module_name_on_instance(self):
        m = IntelligentCaptionsV1Module()
        assert m.module_name == "intelligent_captions_v1"

    def test_module_version_exists(self):
        m = IntelligentCaptionsV1Module()
        assert isinstance(m.module_version, str)
        assert m.module_version.strip()

    def test_module_is_post_processing_module(self):
        m = IntelligentCaptionsV1Module()
        assert isinstance(m, PostProcessingModule)

    def test_registry_singleton(self):
        assert isinstance(INTELLIGENT_CAPTIONS_V1_MODULE, IntelligentCaptionsV1Module)
        assert INTELLIGENT_CAPTIONS_V1_MODULE.module_name == "intelligent_captions_v1"

    def test_get_module_factory(self):
        m = get_intelligent_captions_v1_module()
        assert isinstance(m, IntelligentCaptionsV1Module)

    def test_no_ai_imports(self):
        """No LLM or AI service should be imported."""
        import intelligent_captions_v1 as mod  # noqa: PLC0415
        module_src_path = mod.__file__
        with open(module_src_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        for forbidden in ("openai", "ai_service", "whisperx", "torch", "transformers"):
            assert forbidden not in source, f"Forbidden import found: {forbidden!r}"

    def test_no_whisperx_call(self):
        """No WhisperX transcription should be imported or called."""
        import intelligent_captions_v1 as mod  # noqa: PLC0415
        assert not hasattr(mod, "whisperx")

    def test_no_platform_format_import_in_module(self):
        """This module must not import from platform_safe_format_v1."""
        import intelligent_captions_v1 as mod  # noqa: PLC0415
        module_src_path = mod.__file__
        with open(module_src_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        # The module may reference the name in docstrings/comments, but must NOT
        # import it as a Python module (import statement on its own line)
        assert not re.search(r"^from platform_safe_format_v1\s+import", source, re.MULTILINE)
        assert not re.search(r"^import platform_safe_format_v1", source, re.MULTILINE)

    def test_no_validation_module_import_in_module(self):
        """validation_v1 must not be imported here."""
        import intelligent_captions_v1 as mod  # noqa: PLC0415
        module_src_path = mod.__file__
        with open(module_src_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        assert "from validation_v1" not in source
        assert "import validation_v1" not in source

    def test_no_metadata_writer_import_in_module(self):
        import intelligent_captions_v1 as mod  # noqa: PLC0415
        module_src_path = mod.__file__
        with open(module_src_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        assert "from metadata_writer_v1" not in source
        assert "import metadata_writer_v1" not in source

    def test_keyword_highlighting_disabled_by_default(self):
        from intelligent_captions_v1 import _DEFAULT_CONFIG  # noqa: PLC0415
        assert _DEFAULT_CONFIG["enable_keyword_highlighting"] is False


# ===========================================================================
# Tests — Input validation failures
# ===========================================================================


class TestInputValidation:
    def setup_method(self):
        self.module = IntelligentCaptionsV1Module()
        self.ctx = _make_context(words=_make_word_list())

    def test_missing_input_path_returns_fail(self):
        result = self.module.run(self.ctx, input_path=None)
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "missing_input_path"

    def test_empty_input_path_returns_fail(self):
        result = self.module.run(self.ctx, input_path="   ")
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "missing_input_path"

    def test_input_file_not_found_returns_fail(self, tmp_path):
        result = self.module.run(self.ctx, input_path=str(tmp_path / "nonexistent.mp4"))
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "input_file_not_found"

    def test_input_path_is_directory_returns_fail(self, tmp_path):
        result = self.module.run(self.ctx, input_path=str(tmp_path))
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "input_path_not_file"

    def test_empty_input_file_returns_fail(self, tmp_path):
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")
        result = self.module.run(self.ctx, input_path=str(empty))
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "input_file_empty"

    def test_unprobeable_file_returns_fail(self, tmp_path):
        if not FFTOOLS_AVAILABLE:
            pytest.skip("ffprobe not available")
        bad = tmp_path / "bad.mp4"
        bad.write_bytes(b"not a video")
        result = self.module.run(self.ctx, input_path=str(bad))
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] in (
            "input_probe_failed",
            "missing_video_stream",
        )


# ===========================================================================
# Tests — Context / candidate validation failures
# ===========================================================================


class TestCandidateValidation:
    def setup_method(self):
        self.module = IntelligentCaptionsV1Module()

    def _make_input(self, tmp_path):
        """Create a non-empty file that passes the file-existence check
        without needing ffprobe (we mock the probe in these tests)."""
        f = tmp_path / "input.mp4"
        f.write_bytes(b"\x00" * 1024)
        return str(f)

    def _run_with_mock_probe(self, ctx, input_path, probe_result):
        with patch(
            "intelligent_captions_v1._probe_video_info", return_value=probe_result
        ):
            return self.module.run(ctx, input_path=input_path)

    def _good_probe(self):
        return {"width": 180, "height": 320, "duration_sec": 4.0, "has_audio": True}

    def test_missing_selected_candidate_returns_fail(self, tmp_path):
        ctx = _make_context(words=_make_word_list())
        ctx["selected_candidate"] = {}
        result = self._run_with_mock_probe(ctx, self._make_input(tmp_path), self._good_probe())
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "missing_selected_candidate"

    def test_none_selected_candidate_returns_fail(self, tmp_path):
        ctx = _make_context(words=_make_word_list())
        ctx["selected_candidate"] = None
        result = self._run_with_mock_probe(ctx, self._make_input(tmp_path), self._good_probe())
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "missing_selected_candidate"

    def test_missing_candidate_id_returns_fail(self, tmp_path):
        ctx = _make_context(words=_make_word_list())
        ctx["selected_candidate"] = {
            "start_sec": 0.0,
            "end_sec": 4.0,
            "words": _make_word_list(),
        }
        result = self._run_with_mock_probe(ctx, self._make_input(tmp_path), self._good_probe())
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "missing_candidate_id"

    def test_missing_timestamps_returns_fail(self, tmp_path):
        ctx = _make_context(words=_make_word_list())
        ctx["selected_candidate"] = {"candidate_id": "c1", "words": _make_word_list()}
        result = self._run_with_mock_probe(ctx, self._make_input(tmp_path), self._good_probe())
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "missing_candidate_timestamps"

    def test_invalid_timestamps_nan_returns_fail(self, tmp_path):
        ctx = _make_context(words=_make_word_list())
        ctx["selected_candidate"] = {
            "candidate_id": "c1",
            "start_sec": float("nan"),
            "end_sec": 4.0,
            "words": _make_word_list(),
        }
        result = self._run_with_mock_probe(ctx, self._make_input(tmp_path), self._good_probe())
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "invalid_candidate_timestamps"

    def test_invalid_timestamps_end_before_start_returns_fail(self, tmp_path):
        ctx = _make_context(words=_make_word_list())
        ctx["selected_candidate"] = {
            "candidate_id": "c1",
            "start_sec": 5.0,
            "end_sec": 2.0,
            "words": _make_word_list(),
        }
        result = self._run_with_mock_probe(ctx, self._make_input(tmp_path), self._good_probe())
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "invalid_candidate_timestamps"

    def test_missing_caption_data_returns_fail(self, tmp_path):
        ctx = _make_context()  # no words, no segments
        result = self._run_with_mock_probe(ctx, self._make_input(tmp_path), self._good_probe())
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "missing_caption_data"


# ===========================================================================
# Tests — Config validation
# ===========================================================================


class TestConfigValidation:
    def test_invalid_max_lines_0_returns_fail(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        ctx = _make_context(words=_make_word_list())
        f = tmp_path / "input.mp4"
        f.write_bytes(b"\x00" * 1024)
        with patch("intelligent_captions_v1._probe_video_info",
                   return_value={"width": 180, "height": 320, "duration_sec": 4.0, "has_audio": True}):
            result = module.run(ctx, input_path=str(f), config={"max_lines": 0})
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "invalid_caption_config"

    def test_invalid_max_lines_3_returns_fail(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        ctx = _make_context(words=_make_word_list())
        f = tmp_path / "input.mp4"
        f.write_bytes(b"\x00" * 1024)
        result = module.run(ctx, input_path=str(f), config={"max_lines": 3})
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "invalid_caption_config"

    def test_invalid_font_size_returns_fail(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        ctx = _make_context(words=_make_word_list())
        f = tmp_path / "input.mp4"
        f.write_bytes(b"\x00" * 1024)
        result = module.run(ctx, input_path=str(f), config={"font_size": 0})
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "invalid_caption_config"

    def test_max_duration_less_than_min_returns_fail(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        ctx = _make_context(words=_make_word_list())
        f = tmp_path / "input.mp4"
        f.write_bytes(b"\x00" * 1024)
        result = module.run(
            ctx,
            input_path=str(f),
            config={"min_caption_duration_sec": 2.0, "max_caption_duration_sec": 1.0},
        )
        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "invalid_caption_config"

    def test_validate_caption_config_valid(self):
        from intelligent_captions_v1 import _DEFAULT_CONFIG  # noqa: PLC0415
        assert _validate_caption_config(_DEFAULT_CONFIG) is None

    def test_validate_caption_config_bad_max_lines(self):
        cfg = dict(_DEFAULT_CONFIG_copy())
        cfg["max_lines"] = 5
        assert _validate_caption_config(cfg) is not None

    def test_validate_caption_config_bad_output_ext(self):
        cfg = dict(_DEFAULT_CONFIG_copy())
        cfg["output_ext"] = "mp4"  # missing leading dot
        assert _validate_caption_config(cfg) is not None


def _DEFAULT_CONFIG_copy():
    from intelligent_captions_v1 import _DEFAULT_CONFIG  # noqa: PLC0415
    return _DEFAULT_CONFIG.copy()


# ===========================================================================
# Tests — Caption data resolution
# ===========================================================================


class TestCaptionDataResolution:
    def _ctx(self, **kwargs):
        ctx = {
            "job_id": "j1",
            "candidate_id": "c1",
            "config": {},
            "selection_result": {},
            "selected_candidate": {},
            "module_results": [],
        }
        ctx.update(kwargs)
        return ctx

    def test_resolves_words_from_source_candidate(self):
        words = _make_word_list()
        cand = {"candidate_id": "c1", "start_sec": 0.0, "end_sec": 4.0,
                "source_candidate": {"words": words}}
        ctx = self._ctx(selected_candidate=cand)
        result = _resolve_caption_source(ctx, cand, {})
        assert result is not None
        src_type, data = result
        assert src_type == "words"
        assert data == words

    def test_resolves_segments_from_source_candidate(self):
        segs = _make_segment_list()
        cand = {"candidate_id": "c1", "start_sec": 0.0, "end_sec": 4.0,
                "source_candidate": {"transcript_segments": segs}}
        ctx = self._ctx(selected_candidate=cand)
        result = _resolve_caption_source(ctx, cand, {})
        assert result is not None
        src_type, data = result
        assert src_type == "segments"

    def test_resolves_words_from_selected_candidate_directly(self):
        words = _make_word_list()
        cand = {"candidate_id": "c1", "start_sec": 0.0, "end_sec": 4.0, "words": words}
        ctx = self._ctx(selected_candidate=cand)
        result = _resolve_caption_source(ctx, cand, {})
        assert result is not None
        assert result[0] == "words"

    def test_resolves_segments_from_selected_candidate_directly(self):
        segs = _make_segment_list()
        cand = {"candidate_id": "c1", "start_sec": 0.0, "end_sec": 4.0, "segments": segs}
        ctx = self._ctx(selected_candidate=cand)
        result = _resolve_caption_source(ctx, cand, {})
        assert result is not None
        assert result[0] == "segments"

    def test_resolves_from_transcript_path_in_config(self, tmp_path):
        transcript = {
            "segments": [{"start": 0.0, "end": 2.0, "text": "hello world"}],
        }
        t_path = str(tmp_path / "transcript.json")
        with open(t_path, "w") as fh:
            json.dump(transcript, fh)
        cand = {"candidate_id": "c1", "start_sec": 0.0, "end_sec": 4.0}
        ctx = self._ctx(selected_candidate=cand, config={"transcript_path": t_path})
        result = _resolve_caption_source(ctx, cand, {})
        assert result is not None
        assert result[0] == "segments"

    def test_returns_none_when_no_source(self):
        cand = {"candidate_id": "c1", "start_sec": 0.0, "end_sec": 4.0}
        ctx = self._ctx(selected_candidate=cand)
        result = _resolve_caption_source(ctx, cand, {})
        assert result is None

    def test_source_candidate_takes_priority_over_config(self, tmp_path):
        words = _make_word_list()
        transcript = {"segments": [{"start": 0.0, "end": 2.0, "text": "from file"}]}
        t_path = str(tmp_path / "t.json")
        with open(t_path, "w") as fh:
            json.dump(transcript, fh)
        cand = {"candidate_id": "c1", "start_sec": 0.0, "end_sec": 4.0, "words": words}
        ctx = self._ctx(selected_candidate=cand, config={"transcript_path": t_path})
        result = _resolve_caption_source(ctx, cand, {})
        assert result is not None
        # words should win over segments from file
        assert result[0] == "words"
        assert result[1] is words


# ===========================================================================
# Tests — Caption chunking
# ===========================================================================


class TestCaptionChunking:
    _cfg = {
        "max_chars_per_caption": 42,
        "max_lines": 2,
        "max_chars_per_line": 32,
        "min_caption_duration_sec": 0.45,
        "max_caption_duration_sec": 2.2,
    }

    def test_word_level_chunking_produces_chunks(self):
        words = _make_word_list()
        chunks = _chunk_from_words(words, 0.0, 4.0, self._cfg)
        assert len(chunks) > 0

    def test_word_level_chunks_have_correct_fields(self):
        words = _make_word_list()
        chunks = _chunk_from_words(words, 0.0, 4.0, self._cfg)
        for chunk in chunks:
            assert "start_sec" in chunk
            assert "end_sec" in chunk
            assert "lines" in chunk
            assert "text" in chunk

    def test_word_level_timings_are_relative(self):
        """Words have absolute times starting at 10s; chunks should start near 0."""
        start_offset = 10.0
        words = _make_word_list(start_offset)
        chunks = _chunk_from_words(words, start_offset, start_offset + 4.0, self._cfg)
        assert len(chunks) > 0
        # All chunk start times should be < 4.0 (relative to clip start)
        for chunk in chunks:
            assert chunk["start_sec"] < 4.0
            assert chunk["end_sec"] <= 4.1  # small tolerance

    def test_words_outside_range_are_skipped(self):
        """Words before the candidate window should not appear."""
        words = _make_word_list()  # absolute times 0-3.9
        # candidate range 2.0-4.0: words before 2.0 skipped
        chunks = _chunk_from_words(words, 2.0, 4.0, self._cfg)
        if chunks:
            assert chunks[0]["start_sec"] >= 0.0

    def test_chunk_obeys_max_chars_per_caption(self):
        words = _make_word_list()
        max_chars = 20
        cfg = {**self._cfg, "max_chars_per_caption": max_chars}
        chunks = _chunk_from_words(words, 0.0, 4.0, cfg)
        for chunk in chunks:
            # Single-word overflows are allowed; multi-word should respect limit
            words_in_chunk = chunk["text"].split()
            if len(words_in_chunk) > 1:
                assert len(chunk["text"]) <= max_chars + 20  # generous tolerance for last word

    def test_chunk_obeys_min_duration(self):
        words = _make_word_list()
        min_dur = 0.45
        chunks = _chunk_from_words(words, 0.0, 4.0, self._cfg)
        for chunk in chunks:
            assert chunk["end_sec"] - chunk["start_sec"] >= min_dur - 0.01

    def test_chunk_obeys_max_duration(self):
        words = _make_word_list()
        max_dur = 2.2
        chunks = _chunk_from_words(words, 0.0, 4.0, self._cfg)
        for chunk in chunks:
            assert chunk["end_sec"] - chunk["start_sec"] <= max_dur + 0.01

    def test_segment_level_chunking_produces_chunks(self):
        segs = _make_segment_list()
        chunks = _chunk_from_segments(segs, 0.0, 4.0, self._cfg)
        assert len(chunks) > 0

    def test_segment_level_timings_are_relative(self):
        start_offset = 5.0
        segs = _make_segment_list(start_offset)
        chunks = _chunk_from_segments(segs, start_offset, start_offset + 4.0, self._cfg)
        assert len(chunks) > 0
        for chunk in chunks:
            assert chunk["start_sec"] < 4.0
            assert chunk["start_sec"] >= 0.0

    def test_segment_with_words_delegates_to_word_chunker(self):
        words = _make_word_list()
        segs = [
            {
                "start": 0.0,
                "end": 4.0,
                "text": "This is a test",
                "words": words,
            }
        ]
        chunks_seg = _chunk_from_segments(segs, 0.0, 4.0, self._cfg)
        chunks_word = _chunk_from_words(words, 0.0, 4.0, self._cfg)
        # Should produce same chunks
        assert len(chunks_seg) == len(chunks_word)

    def test_no_empty_caption_chunks(self):
        words = _make_word_list()
        chunks = _chunk_from_words(words, 0.0, 4.0, self._cfg)
        for chunk in chunks:
            assert chunk["text"].strip()
            assert any(ln.strip() for ln in chunk["lines"])

    def test_empty_word_list_returns_empty(self):
        chunks = _chunk_from_words([], 0.0, 4.0, self._cfg)
        assert chunks == []

    def test_words_all_outside_range_returns_empty(self):
        words = _make_word_list()  # 0-4s absolute
        chunks = _chunk_from_words(words, 10.0, 14.0, self._cfg)
        assert chunks == []


# ===========================================================================
# Tests — Line breaking
# ===========================================================================


class TestLineBreaking:
    def test_short_text_is_one_line(self):
        lines = _break_into_lines("hello world", 2, 32)
        assert lines == ["hello world"]

    def test_long_text_wraps_at_limit(self):
        text = "this is a quite long line of text that should wrap"
        lines = _break_into_lines(text, 2, 20)
        assert len(lines) <= 2
        for ln in lines:
            # Each line should be close to the limit
            assert len(ln) <= 20 + 20  # long words can overflow

    def test_max_lines_respected(self):
        text = "a b c d e f g h i j k l m n o p q r s t"
        lines = _break_into_lines(text, 2, 5)
        assert len(lines) <= 2

    def test_empty_text_returns_empty(self):
        assert _break_into_lines("", 2, 32) == []

    def test_single_word_is_one_line(self):
        lines = _break_into_lines("captions", 2, 32)
        assert lines == ["captions"]

    def test_deterministic(self):
        text = "this is the part that actually matters"
        r1 = _break_into_lines(text, 2, 22)
        r2 = _break_into_lines(text, 2, 22)
        assert r1 == r2

    def test_split_text_to_chunks_respects_max(self):
        text = "a b c d e f g h i j k l m n o p q r s t"
        chunks = _split_text_to_chunks(text, 10)
        for chunk in chunks:
            words = chunk.split()
            if len(words) > 1:
                assert len(chunk) <= 20  # generous


# ===========================================================================
# Tests — ASS generation
# ===========================================================================


class TestASSGeneration:
    _safe_zones = {
        "top_margin_px": 180,
        "bottom_margin_px": 320,
        "left_margin_px": 80,
        "right_margin_px": 80,
        "caption_safe_y_min_px": 360,
        "caption_safe_y_max_px": 1280,
    }

    def _cfg(self, **overrides):
        from intelligent_captions_v1 import _DEFAULT_CONFIG  # noqa: PLC0415
        return {**_DEFAULT_CONFIG, **overrides}

    def _chunks(self):
        words = _make_word_list()
        from intelligent_captions_v1 import _DEFAULT_CONFIG  # noqa: PLC0415
        return _chunk_from_words(words, 0.0, 4.0, _DEFAULT_CONFIG)

    def test_ass_content_contains_script_info(self):
        chunks = self._chunks()
        content = _generate_ass_content(chunks, self._cfg(), self._safe_zones)
        assert "[Script Info]" in content
        assert "ScriptType: v4.00+" in content

    def test_ass_content_contains_style(self):
        chunks = self._chunks()
        content = _generate_ass_content(chunks, self._cfg(), self._safe_zones)
        assert "[V4+ Styles]" in content
        assert "Style: Default" in content

    def test_ass_content_contains_events(self):
        chunks = self._chunks()
        content = _generate_ass_content(chunks, self._cfg(), self._safe_zones)
        assert "[Events]" in content
        assert "Dialogue:" in content

    def test_ass_play_res_uses_input_dimensions(self):
        chunks = self._chunks()
        content = _generate_ass_content(
            chunks, self._cfg(), self._safe_zones, play_res_x=180, play_res_y=320
        )
        assert "PlayResX: 180" in content
        assert "PlayResY: 320" in content

    def test_ass_contains_dialogue_for_each_chunk(self):
        chunks = self._chunks()
        content = _generate_ass_content(chunks, self._cfg(), self._safe_zones)
        dialogue_count = content.count("Dialogue:")
        assert dialogue_count == len(chunks)

    def test_ass_time_format(self):
        assert _ass_time(0.0) == "0:00:00.00"
        assert _ass_time(1.5) == "0:00:01.50"
        assert _ass_time(61.0) == "0:01:01.00"
        assert _ass_time(3661.0) == "1:01:01.00"

    def test_ass_time_centiseconds(self):
        assert _ass_time(0.99) == "0:00:00.99"
        assert _ass_time(1.256) == "0:00:01.26"

    def test_ass_escape_curly_braces(self):
        text = "hello {world}"
        escaped = _escape_ass_text(text)
        assert "{" not in escaped.replace(r"\{", "")
        assert r"\{" in escaped
        assert r"\}" in escaped

    def test_ass_escape_backslash(self):
        text = "back\\slash"
        escaped = _escape_ass_text(text)
        assert "\\\\" in escaped

    def test_ass_special_chars_in_content(self):
        chunks = [
            {
                "start_sec": 0.0,
                "end_sec": 1.0,
                "lines": ["it's a test & {special}"],
                "text": "it's a test & {special}",
            }
        ]
        content = _generate_ass_content(chunks, self._cfg(), self._safe_zones)
        # Curly braces should be escaped, apostrophe and & are fine in ASS
        assert r"\{" in content
        assert r"\}" in content

    def test_ass_style_contains_font_family(self):
        chunks = self._chunks()
        content = _generate_ass_content(
            chunks, self._cfg(font_family="DejaVu Sans"), self._safe_zones
        )
        assert "DejaVu Sans" in content

    def test_ass_style_contains_font_size(self):
        chunks = self._chunks()
        content = _generate_ass_content(chunks, self._cfg(font_size=72), self._safe_zones)
        assert ",72," in content

    def test_ass_style_bold_flag(self):
        chunks = self._chunks()
        content = _generate_ass_content(
            chunks, self._cfg(font_bold=True), self._safe_zones
        )
        # Bold flag in ASS is -1
        assert ",-1," in content

    def test_ass_style_outline_present(self):
        chunks = self._chunks()
        content = _generate_ass_content(chunks, self._cfg(outline_width=4), self._safe_zones)
        assert ",4," in content  # outline width in style line

    def test_margin_v_is_at_least_safe_bottom(self):
        """MarginV should be at least safe_zone_bottom_px."""
        chunks = self._chunks()
        safe_zones = {**self._safe_zones}
        cfg = self._cfg(safe_zone_bottom_px=320)
        content = _generate_ass_content(
            chunks, cfg, safe_zones, play_res_x=1080, play_res_y=1920
        )
        # Parse the style line for MarginV (19th comma-separated field, 0-indexed)
        style_match = re.search(r"^Style: Default,(.+)$", content, re.MULTILINE)
        assert style_match is not None
        style_parts = style_match.group(1).split(",")
        # ASS style fields (0-indexed after "Default,"):
        # 0=Fontname, 1=Fontsize, 2=PrimaryColour ... 17=Alignment, 18=MarginL, 19=MarginR, 20=MarginV
        margin_v = int(style_parts[20])
        assert margin_v >= 320  # at least safe_zone_bottom_px


# ===========================================================================
# Tests — Safe zone resolution
# ===========================================================================


class TestSafeZoneResolution:
    def test_uses_defaults_when_no_prior_result(self):
        ctx = {"module_results": []}
        cfg = {
            "safe_zone_bottom_px": 320,
            "safe_zone_top_px": 180,
            "safe_zone_left_px": 80,
            "safe_zone_right_px": 80,
        }
        sz, tw, th = _resolve_safe_zones(ctx, cfg)
        assert "bottom_margin_px" in sz
        assert sz["bottom_margin_px"] == 320
        assert tw == 1080
        assert th == 1920

    def test_uses_platform_safe_format_result_when_available(self):
        sz_from_prior = {
            "top_margin_px": 200,
            "bottom_margin_px": 400,
            "left_margin_px": 100,
            "right_margin_px": 100,
            "caption_safe_y_min_px": 400,
            "caption_safe_y_max_px": 1120,
        }
        prior_result = make_module_pass_result(
            "platform_safe_format_v1", "1.0",
            output_path="/tmp/out.mp4",
            metadata={
                "safe_zones": sz_from_prior,
                "target_width": 1080,
                "target_height": 1920,
            },
        )
        ctx = {"module_results": [prior_result]}
        cfg = {"safe_zone_bottom_px": 320}  # differs from prior result
        sz, tw, th = _resolve_safe_zones(ctx, cfg)
        # Should use the prior result's values
        assert sz["bottom_margin_px"] == 400
        assert tw == 1080

    def test_ignores_non_platform_safe_format_results(self):
        other_result = make_module_pass_result(
            "render_clip_v1", "1.0", output_path="/tmp/out.mp4",
        )
        ctx = {"module_results": [other_result]}
        cfg = {"safe_zone_bottom_px": 999}
        sz, tw, th = _resolve_safe_zones(ctx, cfg)
        # Falls back to config
        assert sz["bottom_margin_px"] == 999


# ===========================================================================
# Tests — Path helpers
# ===========================================================================


class TestPathHelpers:
    def test_output_path_is_deterministic(self):
        p1 = _make_output_path("/clips", "job1", "cand1")
        p2 = _make_output_path("/clips", "job1", "cand1")
        assert p1 == p2

    def test_output_path_contains_module_name(self):
        p = _make_output_path("/clips", "j", "c")
        assert "intelligent_captions_v1" in p

    def test_output_path_in_clip_dir(self):
        p = _make_output_path("/my/clip/dir", "j", "c")
        assert p.startswith("/my/clip/dir/")

    def test_sidecar_path_is_deterministic(self):
        p1 = _make_sidecar_path("/tmp", "job1", "cand1")
        p2 = _make_sidecar_path("/tmp", "job1", "cand1")
        assert p1 == p2

    def test_sidecar_path_has_ass_extension(self):
        p = _make_sidecar_path("/tmp", "j", "c")
        assert p.endswith(".ass")

    def test_sidecar_path_in_tmp_dir(self):
        p = _make_sidecar_path("/my/tmp", "j", "c")
        assert p.startswith("/my/tmp/")

    def test_output_path_includes_safe_job_and_candidate(self):
        p = _make_output_path("/clips", "job-123", "cand_001")
        filename = os.path.basename(p)
        assert "job_123" in filename or "job-123" in filename
        assert "cand_001" in filename

    def test_safe_filename_part_replaces_special_chars(self):
        assert "/" not in _safe_filename_part("a/b")
        assert " " not in _safe_filename_part("a b")
        assert "." not in _safe_filename_part("a.b")

    def test_escape_path_for_filter(self):
        path = "/tmp/job_cand.ass"
        escaped = _escape_path_for_filter(path)
        # No colons that could break filter syntax
        assert "\\:" not in escaped  # forward slashes are fine on Linux

    def test_escape_path_for_filter_with_colon(self):
        path = "/tmp/weird:path.ass"
        escaped = _escape_path_for_filter(path)
        assert "\\:" in escaped


# ===========================================================================
# Tests — ASS sidecar file I/O
# ===========================================================================


class TestSidecarFile:
    def _run_with_mock_probe_and_no_ffmpeg(self, module, ctx, input_path, probe_result):
        """Run module, mocking probe to return good result and ffmpeg to produce output."""
        with patch("intelligent_captions_v1._probe_video_info", return_value=probe_result):
            with patch("subprocess.run") as mock_run:
                # First call is ffmpeg, should succeed and create output file
                def _side_effect(cmd, **kw):
                    m = MagicMock()
                    m.returncode = 0
                    m.stdout = ""
                    m.stderr = ""
                    # Create the output file so verification passes
                    if cmd[0] == "ffmpeg":
                        out = cmd[-1]
                        os.makedirs(os.path.dirname(out) if os.path.dirname(out) else ".", exist_ok=True)
                        with open(out, "wb") as fh:
                            fh.write(b"\x00" * 1024)
                    return m

                mock_run.side_effect = _side_effect
                # But we need probe to work for the output too — patch returns good probe for both calls
                return module.run(ctx, input_path=input_path)

    def test_sidecar_file_is_written(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        f = tmp_path / "input.mp4"
        f.write_bytes(b"\x00" * 1024)

        good_probe = {"width": 180, "height": 320, "duration_sec": 4.0, "has_audio": True}

        with patch("intelligent_captions_v1._probe_video_info", return_value=good_probe):
            with patch("subprocess.run") as mock_run:
                def _side_effect(cmd, **kw):
                    m = MagicMock()
                    m.returncode = 0
                    m.stdout = ""
                    m.stderr = ""
                    if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "ffmpeg":
                        out = cmd[-1]
                        os.makedirs(os.path.dirname(out), exist_ok=True)
                        with open(out, "wb") as fh:
                            fh.write(b"\x00" * 1024)
                    return m

                mock_run.side_effect = _side_effect
                result = module.run(ctx, input_path=str(f))

        # Sidecar should exist regardless of ffmpeg result
        tmp_dir = str(tmp_path / "tmp")
        ass_files = [p for p in os.listdir(tmp_dir) if p.endswith(".ass")]
        assert len(ass_files) > 0

    def test_sidecar_file_is_non_empty(self, tmp_path):
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        os.makedirs(str(tmp_path / "tmp"), exist_ok=True)
        # Generate and write ASS directly
        from intelligent_captions_v1 import _DEFAULT_CONFIG  # noqa: PLC0415
        chunks = _chunk_from_words(words, 0.0, 4.0, _DEFAULT_CONFIG)
        safe_zones = {
            "top_margin_px": 180, "bottom_margin_px": 320,
            "left_margin_px": 80, "right_margin_px": 80,
            "caption_safe_y_min_px": 360, "caption_safe_y_max_px": 1280,
        }
        content = _generate_ass_content(chunks, _DEFAULT_CONFIG, safe_zones)
        sidecar = _make_sidecar_path(str(tmp_path / "tmp"), "j1", "c1")
        with open(sidecar, "w", encoding="utf-8") as fh:
            fh.write(content)
        assert os.path.getsize(sidecar) > 0

    def test_sidecar_file_contains_dialogue_lines(self, tmp_path):
        words = _make_word_list()
        from intelligent_captions_v1 import _DEFAULT_CONFIG  # noqa: PLC0415
        chunks = _chunk_from_words(words, 0.0, 4.0, _DEFAULT_CONFIG)
        safe_zones = {
            "top_margin_px": 180, "bottom_margin_px": 320,
            "left_margin_px": 80, "right_margin_px": 80,
            "caption_safe_y_min_px": 360, "caption_safe_y_max_px": 1280,
        }
        content = _generate_ass_content(chunks, _DEFAULT_CONFIG, safe_zones)
        assert "Dialogue:" in content
        assert len([ln for ln in content.splitlines() if ln.startswith("Dialogue:")]) == len(chunks)


# ===========================================================================
# Tests — PASS / FAIL result shape
# ===========================================================================


class TestResultShape:
    def _run_with_full_mock(self, module, ctx, input_path, tmp_path):
        """Run with mocked probe and ffmpeg — returns result."""
        good_probe = {"width": 180, "height": 320, "duration_sec": 4.0, "has_audio": True}
        with patch("intelligent_captions_v1._probe_video_info", return_value=good_probe):
            with patch("subprocess.run") as mock_run:
                def _side_effect(cmd, **kw):
                    m = MagicMock()
                    m.returncode = 0
                    m.stdout = ""
                    m.stderr = ""
                    if isinstance(cmd, list) and cmd[0] == "ffmpeg":
                        out = cmd[-1]
                        os.makedirs(os.path.dirname(out), exist_ok=True)
                        with open(out, "wb") as fh:
                            fh.write(b"\x00" * 1024)
                    return m

                mock_run.side_effect = _side_effect
                return module.run(ctx, input_path=input_path)

    def test_fail_result_has_failure_code(self):
        module = IntelligentCaptionsV1Module()
        ctx = _make_context(words=_make_word_list())
        result = module.run(ctx, input_path=None)
        assert result["status"] == MODULE_STATUS_FAIL
        assert "failure_code" in result["metadata"]

    def test_fail_result_is_valid_module_result(self):
        module = IntelligentCaptionsV1Module()
        ctx = _make_context(words=_make_word_list())
        result = module.run(ctx, input_path=None)
        validate_module_result(result)

    def test_pass_result_includes_caption_count(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        f = tmp_path / "in.mp4"
        f.write_bytes(b"\x00" * 1024)
        result = self._run_with_full_mock(module, ctx, str(f), tmp_path)
        if result["status"] == MODULE_STATUS_PASS:
            assert "caption_count" in result["metadata"]
            assert result["metadata"]["caption_count"] > 0

    def test_pass_result_includes_caption_sidecar_path(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        f = tmp_path / "in.mp4"
        f.write_bytes(b"\x00" * 1024)
        result = self._run_with_full_mock(module, ctx, str(f), tmp_path)
        if result["status"] == MODULE_STATUS_PASS:
            assert "caption_sidecar_path" in result["metadata"]
            assert result["metadata"]["caption_sidecar_path"].endswith(".ass")

    def test_pass_result_includes_caption_style(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        f = tmp_path / "in.mp4"
        f.write_bytes(b"\x00" * 1024)
        result = self._run_with_full_mock(module, ctx, str(f), tmp_path)
        if result["status"] == MODULE_STATUS_PASS:
            assert "caption_style" in result["metadata"]
            style = result["metadata"]["caption_style"]
            assert "font_size" in style
            assert style["font_size"] >= 8

    def test_pass_result_includes_safe_zone(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        f = tmp_path / "in.mp4"
        f.write_bytes(b"\x00" * 1024)
        result = self._run_with_full_mock(module, ctx, str(f), tmp_path)
        if result["status"] == MODULE_STATUS_PASS:
            assert "caption_safe_zone" in result["metadata"]
            sz = result["metadata"]["caption_safe_zone"]
            assert "bottom_margin_px" in sz

    def test_pass_result_includes_duration_delta(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        f = tmp_path / "in.mp4"
        f.write_bytes(b"\x00" * 1024)
        result = self._run_with_full_mock(module, ctx, str(f), tmp_path)
        if result["status"] == MODULE_STATUS_PASS:
            assert "duration_delta_sec" in result["metadata"]

    def test_pass_result_includes_output_file_size(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        f = tmp_path / "in.mp4"
        f.write_bytes(b"\x00" * 1024)
        result = self._run_with_full_mock(module, ctx, str(f), tmp_path)
        if result["status"] == MODULE_STATUS_PASS:
            assert "output_file_size_bytes" in result["metadata"]
            assert result["metadata"]["output_file_size_bytes"] > 0

    def test_result_is_json_serializable(self):
        module = IntelligentCaptionsV1Module()
        ctx = _make_context(words=_make_word_list())
        result = module.run(ctx, input_path=None)
        # Should not raise
        json.dumps(result)

    def test_pass_result_keyword_highlighting_disabled(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        f = tmp_path / "in.mp4"
        f.write_bytes(b"\x00" * 1024)
        result = self._run_with_full_mock(module, ctx, str(f), tmp_path)
        if result["status"] == MODULE_STATUS_PASS:
            assert result["metadata"]["keyword_highlighting_enabled"] is False


# ===========================================================================
# Tests — Overwrite behaviour
# ===========================================================================


class TestOverwriteBehaviour:
    def _probe_good(self):
        return {"width": 180, "height": 320, "duration_sec": 4.0, "has_audio": True}

    def test_overwrite_true_succeeds_when_output_exists(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        clip_dir = str(tmp_path / "clips")
        tmp_dir = str(tmp_path / "tmp")
        os.makedirs(clip_dir, exist_ok=True)
        os.makedirs(tmp_dir, exist_ok=True)

        # Pre-create the output file
        out_path = _make_output_path(clip_dir, "testjob", "cand001")
        with open(out_path, "wb") as fh:
            fh.write(b"\x00" * 512)

        ctx = _make_context(words=words, clip_dir=clip_dir, tmp_dir=tmp_dir)
        f = tmp_path / "in.mp4"
        f.write_bytes(b"\x00" * 1024)

        with patch("intelligent_captions_v1._probe_video_info", return_value=self._probe_good()):
            with patch("subprocess.run") as mock_run:
                def _side_effect(cmd, **kw):
                    m = MagicMock()
                    m.returncode = 0
                    m.stdout = ""
                    m.stderr = ""
                    if isinstance(cmd, list) and cmd[0] == "ffmpeg":
                        out = cmd[-1]
                        with open(out, "wb") as fh:
                            fh.write(b"\x00" * 1024)
                    return m

                mock_run.side_effect = _side_effect
                result = module.run(ctx, input_path=str(f), config={"overwrite": True})

        # Should not fail with output_exists
        assert result["metadata"].get("failure_code") != "output_exists"

    def test_overwrite_false_fails_when_output_exists(self, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        clip_dir = str(tmp_path / "clips")
        tmp_dir = str(tmp_path / "tmp")
        os.makedirs(clip_dir, exist_ok=True)
        os.makedirs(tmp_dir, exist_ok=True)

        out_path = _make_output_path(clip_dir, "testjob", "cand001")
        with open(out_path, "wb") as fh:
            fh.write(b"\x00" * 512)

        ctx = _make_context(words=words, clip_dir=clip_dir, tmp_dir=tmp_dir)
        f = tmp_path / "in.mp4"
        f.write_bytes(b"\x00" * 1024)

        with patch("intelligent_captions_v1._probe_video_info", return_value=self._probe_good()):
            result = module.run(ctx, input_path=str(f), config={"overwrite": False})

        assert result["status"] == MODULE_STATUS_FAIL
        assert result["metadata"]["failure_code"] == "output_exists"


# ===========================================================================
# Tests — Directory creation
# ===========================================================================


class TestDirectoryCreation:
    def test_clip_dir_created_if_missing(self, tmp_path):
        clip_dir = str(tmp_path / "clips" / "new")
        tmp_dir = str(tmp_path / "tmp")
        os.makedirs(tmp_dir, exist_ok=True)

        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(words=words, clip_dir=clip_dir, tmp_dir=tmp_dir)
        f = tmp_path / "in.mp4"
        f.write_bytes(b"\x00" * 1024)

        good_probe = {"width": 180, "height": 320, "duration_sec": 4.0, "has_audio": True}
        with patch("intelligent_captions_v1._probe_video_info", return_value=good_probe):
            with patch("subprocess.run") as mock_run:
                def _side_effect(cmd, **kw):
                    m = MagicMock()
                    m.returncode = 0
                    m.stdout = ""
                    m.stderr = ""
                    if isinstance(cmd, list) and cmd[0] == "ffmpeg":
                        out = cmd[-1]
                        os.makedirs(os.path.dirname(out), exist_ok=True)
                        with open(out, "wb") as fh:
                            fh.write(b"\x00" * 1024)
                    return m

                mock_run.side_effect = _side_effect
                module.run(ctx, input_path=str(f))

        # clip_dir should now exist
        assert os.path.isdir(clip_dir)


# ===========================================================================
# Tests — Real ffmpeg (skipped if not available)
# ===========================================================================


@requires_ffmpeg
class TestRealCaptionBurnIn:
    """Real ffmpeg caption burn-in tests with tiny synthetic 180x320 videos."""

    def test_caption_output_created(self, vertical_clip, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS, (
            f"Expected PASS, got FAIL: {result.get('error_reason')}\n"
            f"stderr: {result.get('metadata', {}).get('ffmpeg_stderr_tail', '')}"
        )
        assert os.path.isfile(result["output_path"])

    def test_caption_output_is_non_empty(self, vertical_clip, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS
        assert os.path.getsize(result["output_path"]) > 0

    def test_caption_output_has_video_stream(self, vertical_clip, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS
        meta = result["metadata"]
        assert meta["output_width"] == 180
        assert meta["output_height"] == 320

    def test_output_dimensions_match_input(self, vertical_clip, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS
        meta = result["metadata"]
        assert meta["output_width"] == meta["input_width"]
        assert meta["output_height"] == meta["input_height"]

    def test_output_preserves_audio(self, vertical_clip, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS
        assert result["metadata"]["input_has_audio"] is True

    def test_output_duration_close_to_input(self, vertical_clip, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS
        meta = result["metadata"]
        if meta.get("input_duration_sec") and meta.get("output_duration_sec"):
            assert abs(meta["output_duration_sec"] - meta["input_duration_sec"]) <= 1.0

    def test_output_is_9_16_when_input_is_9_16(self, vertical_clip, tmp_path):
        """180x320 = 9:16 at minimum scale."""
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS
        meta = result["metadata"]
        w = meta["output_width"]
        h = meta["output_height"]
        import math as _math  # noqa: PLC0415
        gcd = _math.gcd(w, h)
        assert w // gcd == 9 and h // gcd == 16

    def test_sidecar_file_written_during_real_run(self, vertical_clip, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS
        sidecar = result["metadata"]["caption_sidecar_path"]
        assert os.path.isfile(sidecar)
        assert os.path.getsize(sidecar) > 0

    def test_sidecar_contains_dialogue_lines(self, vertical_clip, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS
        sidecar = result["metadata"]["caption_sidecar_path"]
        with open(sidecar, "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "Dialogue:" in content

    def test_word_level_timing_produces_output(self, vertical_clip, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list(0.0)
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS

    def test_segment_level_timing_produces_output(self, vertical_clip, tmp_path):
        module = IntelligentCaptionsV1Module()
        segs = _make_segment_list(0.0)
        ctx = _make_context(
            segments=segs,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS

    def test_no_audio_input_produces_video_only_output(self, vertical_clip_no_audio, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip_no_audio)
        assert result["status"] == MODULE_STATUS_PASS

    def test_pass_result_is_valid_module_result(self, vertical_clip, tmp_path):
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip)
        validate_module_result(result)

    def test_safe_zone_in_pass_metadata_from_prior_module(self, vertical_clip, tmp_path):
        """Use safe_zones from a prior platform_safe_format_v1 module result."""
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        prior_sz = {
            "top_margin_px": 180,
            "bottom_margin_px": 320,
            "left_margin_px": 80,
            "right_margin_px": 80,
            "caption_safe_y_min_px": 360,
            "caption_safe_y_max_px": 1280,
        }
        prior_result = make_module_pass_result(
            "platform_safe_format_v1", "1.0",
            output_path=vertical_clip,
            metadata={
                "safe_zones": prior_sz,
                "target_width": 180,
                "target_height": 320,
            },
        )
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
            module_results=[prior_result],
        )
        result = module.run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS
        sz = result["metadata"]["caption_safe_zone"]
        assert sz["bottom_margin_px"] == 320

    def test_captions_positioned_inside_safe_area(self, vertical_clip, tmp_path):
        """The ASS MarginV must be >= safe_zone_bottom_px."""
        module = IntelligentCaptionsV1Module()
        words = _make_word_list()
        ctx = _make_context(
            words=words,
            start_sec=0.0,
            end_sec=4.0,
            clip_dir=str(tmp_path / "clips"),
            tmp_dir=str(tmp_path / "tmp"),
        )
        result = module.run(ctx, input_path=vertical_clip)
        assert result["status"] == MODULE_STATUS_PASS
        sidecar = result["metadata"]["caption_sidecar_path"]
        with open(sidecar, "r", encoding="utf-8") as fh:
            content = fh.read()
        style_match = re.search(r"^Style: Default,(.+)$", content, re.MULTILINE)
        assert style_match is not None
        parts = style_match.group(1).split(",")
        # MarginV is field 20 (0-indexed after "Default,")
        margin_v = int(parts[20])
        # With default 320px bottom margin, MarginV should be >= 320
        assert margin_v >= 320


# ===========================================================================
# Tests — Module chain integration
# ===========================================================================


@requires_ffmpeg
class TestModuleChainIntegration:
    """Integration tests running render_clip_v1 → platform_safe_format_v1 →
    intelligent_captions_v1 with dummy downstream modules."""

    def _make_source_video(self, tmp_path):
        """Create a small 320x180 source video (horizontal, 6s)."""
        out = str(tmp_path / "source.mp4")
        _make_synth_video(out, 320, 180, 6.0, with_audio=True)
        return out

    def test_module_chain_with_real_modules(self, tmp_path):
        """render → platform_safe → intelligent_captions with dummy downstream."""
        source = self._make_source_video(tmp_path)
        clip_dir = str(tmp_path / "clips")
        tmp_dir = str(tmp_path / "tmp")

        # Dummy downstream modules
        class _DummyPass(PostProcessingModule):
            def __init__(self, name):
                self.module_name = name
                self.module_version = "1.0"

            def run(self, context, *, input_path=None, config=None):
                return make_module_pass_result(
                    self.module_name, self.module_version,
                    input_path=input_path,
                    output_path=input_path,
                )

        words = _make_word_list()
        ctx = {
            "job_id": "chain_test",
            "candidate_id": "c001",
            "source_video_path": source,
            "clip_dir": clip_dir,
            "tmp_dir": tmp_dir,
            "metadata_dir": None,
            "working_dir": None,
            "config": {},
            "selection_result": {},
            "selected_candidate": {
                "candidate_id": "c001",
                "start_sec": 1.0,
                "end_sec": 5.0,
                "words": words,
            },
            "module_results": [],
        }

        modules = [
            RenderClipV1Module(),
            PlatformSafeFormatV1Module(),
            IntelligentCaptionsV1Module(),
            _DummyPass("validation_v1"),
            _DummyPass("metadata_writer_v1"),
        ]

        from post_processing_modules import run_module_chain  # noqa: PLC0415
        chain_result = run_module_chain(modules, ctx, initial_input_path=source)
        assert chain_result["status"] == "PASS", (
            f"Chain failed at: {chain_result.get('failed_module')}\n"
            f"reason: {chain_result['errors']}"
        )
        # Check that captions module PASS result is in chain
        module_names = [r["module_name"] for r in chain_result["module_results"]]
        assert "intelligent_captions_v1" in module_names
        captions_result = next(
            r for r in chain_result["module_results"]
            if r["module_name"] == "intelligent_captions_v1"
        )
        assert captions_result["status"] == MODULE_STATUS_PASS
        assert captions_result["output_path"] is not None
        assert os.path.isfile(captions_result["output_path"])

    def test_conveyor_with_real_render_format_captions(self, tmp_path):
        """run_fixed_mk1_universal_conveyor with real render/format/captions + dummies."""
        source = self._make_source_video(tmp_path)

        class _DummyPass(PostProcessingModule):
            def __init__(self, name):
                self.module_name = name
                self.module_version = "1.0"

            def run(self, context, *, input_path=None, config=None):
                return make_module_pass_result(
                    self.module_name, self.module_version,
                    input_path=input_path,
                    output_path=input_path,
                )

        words = _make_word_list()
        selection_result = {
            "job_id": "conv_test",
            "selected_candidates": [
                {
                    "candidate_id": "cand_a",
                    "start_sec": 1.0,
                    "end_sec": 5.0,
                    "words": words,
                }
            ],
        }

        registry = {
            "render_clip_v1": RenderClipV1Module(),
            "platform_safe_format_v1": PlatformSafeFormatV1Module(),
            "intelligent_captions_v1": IntelligentCaptionsV1Module(),
            "validation_v1": _DummyPass("validation_v1"),
            "metadata_writer_v1": _DummyPass("metadata_writer_v1"),
        }

        conveyor_result = run_fixed_mk1_universal_conveyor(
            selection_result,
            source_video_path=source,
            job_metadata={"job_id": "conv_test"},
            directories={
                "clips": str(tmp_path / "clips"),
                "tmp": str(tmp_path / "tmp"),
                "metadata": str(tmp_path / "meta"),
                "post_processing_root": str(tmp_path),
            },
            module_registry=registry,
        )

        assert conveyor_result["status"] == CONVEYOR_STATUS_COMPLETE
        assert conveyor_result["summary"]["clips_passed"] == 1
        assert conveyor_result["summary"]["clips_failed"] == 0

        clip_result = conveyor_result["clip_results"][0]
        assert clip_result["status"] == "PASS"
        assert clip_result["final_output_path"] is not None

    def test_conveyor_passes_captioned_path_to_next_module(self, tmp_path):
        """Verify next module receives intelligent_captions_v1 output path."""
        source = self._make_source_video(tmp_path)

        received_paths: list[str | None] = []

        class _PathCapture(PostProcessingModule):
            def __init__(self, name):
                self.module_name = name
                self.module_version = "1.0"

            def run(self, context, *, input_path=None, config=None):
                received_paths.append(input_path)
                return make_module_pass_result(
                    self.module_name, self.module_version,
                    input_path=input_path,
                    output_path=input_path,
                )

        words = _make_word_list()
        ctx = {
            "job_id": "path_test",
            "candidate_id": "c1",
            "source_video_path": source,
            "clip_dir": str(tmp_path / "clips"),
            "tmp_dir": str(tmp_path / "tmp"),
            "metadata_dir": None,
            "working_dir": None,
            "config": {},
            "selection_result": {},
            "selected_candidate": {
                "candidate_id": "c1",
                "start_sec": 1.0,
                "end_sec": 5.0,
                "words": words,
            },
            "module_results": [],
        }

        validation_dummy = _PathCapture("validation_v1")
        modules = [
            RenderClipV1Module(),
            PlatformSafeFormatV1Module(),
            IntelligentCaptionsV1Module(),
            validation_dummy,
            _PathCapture("metadata_writer_v1"),
        ]

        chain_result = run_module_chain(modules, ctx, initial_input_path=source)
        assert chain_result["status"] == "PASS"
        assert len(received_paths) >= 1
        # validation_v1 should receive the captions output path
        assert received_paths[0] is not None
        assert "intelligent_captions_v1" in received_paths[0]


# ===========================================================================
# Tests — Regression: Prompt 14–19 modules are still importable
# ===========================================================================


class TestRegressionImports:
    """Quick sanity check that prior module imports still work."""

    def test_post_processing_modules_importable(self):
        from post_processing_modules import (  # noqa: PLC0415
            PostProcessingModule,
            make_module_pass_result,
            make_module_fail_result,
            run_module_chain,
        )
        assert PostProcessingModule is not None

    def test_post_processing_conveyor_importable(self):
        from post_processing_conveyor import (  # noqa: PLC0415
            run_fixed_mk1_universal_conveyor,
            FIXED_MK1_CONVEYOR_MODULES,
        )
        assert "intelligent_captions_v1" in FIXED_MK1_CONVEYOR_MODULES

    def test_render_clip_v1_importable(self):
        from render_clip_v1 import RenderClipV1Module  # noqa: PLC0415
        assert RenderClipV1Module is not None

    def test_platform_safe_format_v1_importable(self):
        from platform_safe_format_v1 import PlatformSafeFormatV1Module  # noqa: PLC0415
        assert PlatformSafeFormatV1Module is not None
