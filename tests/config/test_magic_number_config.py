"""
tests/config/test_magic_number_config.py

Tests for Prompt 6A: Config-driven magic number replacement.

Covers:
  - load_resolved_config_for_job()
  - Selection config from resolved_config.yaml
  - Post-processing conveyor list from resolved_config.yaml
  - Platform formatting values in conveyor_config
  - Regression: execution_context still works alongside resolved config
  - Legacy job compatibility (no resolved_config.yaml)

Run with:
    video-automation/.venv/bin/python -m pytest tests/config/test_magic_number_config.py -v
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_CONFIG_DIR = REPO_ROOT / "scripts" / "config"
if str(SCRIPTS_CONFIG_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_CONFIG_DIR))

from execution_context import (  # noqa: E402
    ResolvedConfigLoadError,
    load_resolved_config_for_job,
)

# ---------------------------------------------------------------------------
# Conveyor module import (video-automation context)
# ---------------------------------------------------------------------------

_VA_SCRIPTS = REPO_ROOT / "video-automation" / "scripts"
_VA_SERVER = REPO_ROOT / "video-automation" / "server"

def _add_va_paths() -> None:
    for p in (_VA_SCRIPTS, _VA_SERVER):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_CONVEYOR = [
    "render_clip_v1",
    "platform_safe_format_v1",
    "intelligent_captions_v1",
    "validation_v1",
    "metadata_writer_v1",
]


def _write(root: Path, rel: str, content: str) -> None:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(textwrap.dedent(content))


def _write_resolved_config(job_dir: Path, extra: dict | None = None) -> None:
    cfg = {
        "version": 1,
        "selection": {
            "mode": "balanced",
            "max_clips": 6,
            "min_overall_potential": 7.0,
            "min_confidence": 0.6,
            "exploration_ratio": 0.15,
        },
        "post_processing": {
            "conveyor": list(DEFAULT_CONVEYOR),
        },
        "format": {
            "aspect_ratio": "9:16",
            "width": 1080,
            "height": 1920,
            "max_duration_seconds": 60,
            "title_max_length": 100,
            "caption_max_length": 5000,
        },
        "captions": {
            "safe_zone": {
                "top_px": 180,
                "bottom_px": 320,
                "left_px": 80,
                "right_px": 80,
            },
            "layout": {
                "font_family": "Arial",
                "font_size": 64,
                "max_lines": 2,
                "max_chars_per_line": 32,
                "max_chars_per_caption": 42,
            },
        },
    }
    if extra:
        cfg.update(extra)
    (job_dir / "resolved_config.yaml").write_text(yaml.dump(cfg))


# ===========================================================================
# load_resolved_config_for_job tests
# ===========================================================================


class TestLoadResolvedConfigForJob:
    def test_reads_resolved_config_yaml(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_abc"
        job_dir.mkdir()
        _write_resolved_config(job_dir)

        result = load_resolved_config_for_job(job_dir)

        assert result is not None
        assert isinstance(result, dict)
        assert result["selection"]["max_clips"] == 6
        assert result["selection"]["mode"] == "balanced"

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "legacy_job"
        job_dir.mkdir()
        # No resolved_config.yaml present (legacy job)
        result = load_resolved_config_for_job(job_dir)
        assert result is None

    def test_raises_for_malformed_yaml(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_bad"
        job_dir.mkdir()
        # Unbalanced brackets is genuinely invalid YAML that PyYAML rejects.
        (job_dir / "resolved_config.yaml").write_text("key: [unclosed bracket")

        with pytest.raises(ResolvedConfigLoadError, match="invalid"):
            load_resolved_config_for_job(job_dir)

    def test_raises_when_yaml_is_not_mapping(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_list"
        job_dir.mkdir()
        (job_dir / "resolved_config.yaml").write_text("- item1\n- item2\n")

        with pytest.raises(ResolvedConfigLoadError, match="expected mapping"):
            load_resolved_config_for_job(job_dir)

    def test_raises_when_yaml_is_scalar(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_scalar"
        job_dir.mkdir()
        (job_dir / "resolved_config.yaml").write_text("just a string\n")

        with pytest.raises(ResolvedConfigLoadError, match="expected mapping"):
            load_resolved_config_for_job(job_dir)

    def test_error_includes_path(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_path"
        job_dir.mkdir()
        (job_dir / "resolved_config.yaml").write_text("key: [unclosed")

        with pytest.raises(ResolvedConfigLoadError) as exc_info:
            load_resolved_config_for_job(job_dir)

        assert exc_info.value.path == job_dir / "resolved_config.yaml"
        assert "resolved_config.yaml" in str(exc_info.value)

    def test_does_not_mutate_filesystem(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_ro"
        job_dir.mkdir()
        _write_resolved_config(job_dir)
        before = list(job_dir.iterdir())

        load_resolved_config_for_job(job_dir)

        after = list(job_dir.iterdir())
        assert before == after

    def test_accepts_string_path(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_str"
        job_dir.mkdir()
        _write_resolved_config(job_dir)
        result = load_resolved_config_for_job(str(job_dir))
        assert result is not None

    def test_returns_plain_dict(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_type"
        job_dir.mkdir()
        _write_resolved_config(job_dir)
        result = load_resolved_config_for_job(job_dir)
        assert type(result) is dict


# ===========================================================================
# Selection config override tests
# ===========================================================================


class TestSelectionConfigOverride:
    """Test that selection thresholds come from resolved config when available."""

    def _build_resolved_config_with_selection(
        self, job_dir: Path, **selection_overrides: Any
    ) -> None:
        sel = {
            "mode": "balanced",
            "max_clips": 6,
            "min_overall_potential": 7.0,
            "min_confidence": 0.6,
            "exploration_ratio": 0.15,
            **selection_overrides,
        }
        cfg = {
            "version": 1,
            "selection": sel,
            "post_processing": {"conveyor": list(DEFAULT_CONVEYOR)},
        }
        (job_dir / "resolved_config.yaml").write_text(yaml.dump(cfg))

    def test_max_clips_from_resolved_config(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_sel"
        job_dir.mkdir()
        self._build_resolved_config_with_selection(job_dir, max_clips=4)

        resolved = load_resolved_config_for_job(job_dir)
        assert resolved is not None
        assert resolved["selection"]["max_clips"] == 4

    def test_min_confidence_from_resolved_config(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_conf"
        job_dir.mkdir()
        self._build_resolved_config_with_selection(job_dir, min_confidence=0.75)

        resolved = load_resolved_config_for_job(job_dir)
        assert resolved is not None
        assert resolved["selection"]["min_confidence"] == 0.75

    def test_min_overall_potential_from_resolved_config(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_pot"
        job_dir.mkdir()
        self._build_resolved_config_with_selection(job_dir, min_overall_potential=8.5)

        resolved = load_resolved_config_for_job(job_dir)
        assert resolved is not None
        assert resolved["selection"]["min_overall_potential"] == 8.5

    def test_exploration_ratio_from_resolved_config(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_exp"
        job_dir.mkdir()
        self._build_resolved_config_with_selection(job_dir, exploration_ratio=0.25)

        resolved = load_resolved_config_for_job(job_dir)
        assert resolved is not None
        assert resolved["selection"]["exploration_ratio"] == 0.25

    def test_legacy_job_no_resolved_config(self, tmp_path: Path) -> None:
        """Legacy jobs without resolved_config.yaml return None — existing defaults apply."""
        job_dir = tmp_path / "legacy"
        job_dir.mkdir()
        result = load_resolved_config_for_job(job_dir)
        assert result is None

    def test_selection_override_applied_to_existing_dict(self, tmp_path: Path) -> None:
        """Simulate what app.py does: override mk1_settings dict with resolved config values."""
        job_dir = tmp_path / "job_override"
        job_dir.mkdir()
        self._build_resolved_config_with_selection(job_dir, max_clips=2, min_confidence=0.9)

        legacy_selection = {
            "mode": "balanced",
            "max_clips": 6,
            "min_overall_potential": 7.0,
            "min_confidence": 0.6,
            "exploration_ratio": 0.15,
            "reserve_count": 3,
        }
        resolved = load_resolved_config_for_job(job_dir)
        assert resolved is not None

        # This mirrors the override logic in app.py
        _SELECTION_KEYS = (
            "mode", "max_clips", "min_overall_potential",
            "min_confidence", "exploration_ratio",
        )
        _sel = resolved.get("selection") or {}
        for k in _SELECTION_KEYS:
            if k in _sel:
                legacy_selection[k] = _sel[k]

        assert legacy_selection["max_clips"] == 2
        assert legacy_selection["min_confidence"] == 0.9
        # Other legacy keys preserved
        assert legacy_selection["reserve_count"] == 3


# ===========================================================================
# Conveyor config tests
# ===========================================================================


class TestConveyorConfigFromResolvedConfig:
    """Test that post_processing.conveyor comes from resolved config when available."""

    def test_conveyor_list_from_resolved_config(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_conv"
        job_dir.mkdir()
        _write_resolved_config(job_dir)

        resolved = load_resolved_config_for_job(job_dir)
        assert resolved is not None
        assert resolved["post_processing"]["conveyor"] == DEFAULT_CONVEYOR

    def test_legacy_job_has_no_conveyor_in_resolved_config(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "legacy"
        job_dir.mkdir()
        result = load_resolved_config_for_job(job_dir)
        assert result is None

    def test_config_conveyor_order_is_preserved(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_order"
        job_dir.mkdir()
        custom_order = [
            "render_clip_v1",
            "intelligent_captions_v1",  # captions before format (unusual but config-specified)
            "platform_safe_format_v1",
            "validation_v1",
            "metadata_writer_v1",
        ]
        cfg = {
            "version": 1,
            "selection": {"mode": "balanced", "max_clips": 6,
                          "min_overall_potential": 7.0, "min_confidence": 0.6,
                          "exploration_ratio": 0.15},
            "post_processing": {"conveyor": custom_order},
        }
        (job_dir / "resolved_config.yaml").write_text(yaml.dump(cfg))

        resolved = load_resolved_config_for_job(job_dir)
        assert resolved is not None
        assert resolved["post_processing"]["conveyor"] == custom_order


class TestConveyorModuleResolution:
    """Test run_fixed_mk1_universal_conveyor with config-driven module list."""

    def setup_method(self) -> None:
        _add_va_paths()
        from post_processing_conveyor import run_fixed_mk1_universal_conveyor  # noqa: PLC0415
        self._run = run_fixed_mk1_universal_conveyor

    def _minimal_registry(self) -> dict[str, Any]:
        """Build a minimal mock module registry."""
        mods = {}
        for name in DEFAULT_CONVEYOR:
            m = MagicMock()
            m.name = name
            m.run = MagicMock(return_value={"status": "PASS", "module_name": name})
            mods[name] = m
        return mods

    def _minimal_selection_result(self) -> dict[str, Any]:
        return {
            "job_id": "job_test_conveyor",
            "selection_mode": "balanced",
            "selected_candidates": [],
        }

    def test_unknown_module_in_config_list_fails_clearly(self) -> None:
        registry = self._minimal_registry()
        result = self._run(
            selection_result=self._minimal_selection_result(),
            source_video_path="/dev/null",
            job_metadata={
                "job_id": "job_unknown_mod",
                "conveyor_module_list": ["render_clip_v1", "nonexistent_module_v99"],
            },
            config={},
            module_registry=registry,
        )
        assert result["status"] == "CONVEYOR_FAILED"
        errors = result.get("errors") or []
        assert any(e.get("code") == "unknown_conveyor_module" for e in errors)

    def test_legacy_job_uses_fixed_list(self) -> None:
        """When conveyor_module_list is absent, FIXED_MK1_CONVEYOR_MODULES is used."""
        registry = self._minimal_registry()
        result = self._run(
            selection_result=self._minimal_selection_result(),
            source_video_path="/dev/null",
            job_metadata={"job_id": "job_legacy"},
            config={},
            module_registry=registry,
        )
        # With no selected_candidates, conveyor should complete (not fail)
        assert result["status"] == "CONVEYOR_COMPLETE"

    def test_none_conveyor_list_uses_fixed_list(self) -> None:
        registry = self._minimal_registry()
        result = self._run(
            selection_result=self._minimal_selection_result(),
            source_video_path="/dev/null",
            job_metadata={"job_id": "job_none_list", "conveyor_module_list": None},
            config={},
            module_registry=registry,
        )
        assert result["status"] == "CONVEYOR_COMPLETE"

    def test_config_conveyor_list_overrides_fixed(self) -> None:
        """Config-specified list subset still works when all modules present."""
        registry = self._minimal_registry()
        # A valid subset of known modules
        result = self._run(
            selection_result=self._minimal_selection_result(),
            source_video_path="/dev/null",
            job_metadata={
                "job_id": "job_subset",
                "conveyor_module_list": DEFAULT_CONVEYOR[:3],  # first 3 modules
            },
            config={},
            module_registry=registry,
        )
        # No candidates → COMPLETE without running modules
        assert result["status"] == "CONVEYOR_COMPLETE"


# ===========================================================================
# Platform formatting config tests
# ===========================================================================


class TestPlatformFormattingConfig:
    """Test that platform format values are carried in resolved config."""

    def test_format_values_in_resolved_config(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_fmt"
        job_dir.mkdir()
        _write_resolved_config(job_dir)

        resolved = load_resolved_config_for_job(job_dir)
        assert resolved is not None
        fmt = resolved.get("format") or {}
        assert fmt["aspect_ratio"] == "9:16"
        assert fmt["max_duration_seconds"] == 60
        assert fmt["title_max_length"] == 100
        assert fmt["caption_max_length"] == 5000

    def test_format_values_do_not_change_selection_thresholds(self, tmp_path: Path) -> None:
        """platform.format.max_duration_seconds (60) is a platform upload limit,
        NOT the same as selection.max_duration_sec (120) which is a candidate filter."""
        job_dir = tmp_path / "job_dur"
        job_dir.mkdir()
        _write_resolved_config(job_dir)

        resolved = load_resolved_config_for_job(job_dir)
        assert resolved is not None
        # Platform limit ≠ selection filter — different concepts, different keys
        assert resolved["format"]["max_duration_seconds"] == 60
        # Selection config does NOT include platform max_duration_seconds
        sel = resolved.get("selection") or {}
        assert "max_duration_seconds" not in sel


# ===========================================================================
# Regression: execution context still works alongside resolved config
# ===========================================================================


class TestRegressionArtifacts:
    """Ensure prior Prompt 5.x behaviour is preserved."""

    def test_both_files_can_coexist(self, tmp_path: Path) -> None:
        """resolved_config.yaml and execution_context.json can both be present."""
        import json as _json  # noqa: PLC0415
        from execution_context import load_execution_context_for_job  # noqa: PLC0415

        job_dir = tmp_path / "job_both"
        job_dir.mkdir()
        _write_resolved_config(job_dir)
        ctx = {
            "environment": "dev",
            "job_id": "job_both_abc",
            "funnel_id": "business",
            "platform_id": "youtube",
            "preset_id": "balanced",
            "config_version": "1",
        }
        (job_dir / "execution_context.json").write_text(_json.dumps(ctx))

        exec_ctx = load_execution_context_for_job(job_dir)
        resolved = load_resolved_config_for_job(job_dir)

        assert exec_ctx is not None
        assert exec_ctx["job_id"] == "job_both_abc"
        assert resolved is not None
        assert resolved["selection"]["max_clips"] == 6

    def test_resolved_config_is_read_only(self, tmp_path: Path) -> None:
        """load_resolved_config_for_job must not write any files."""
        job_dir = tmp_path / "job_ro_check"
        job_dir.mkdir()
        _write_resolved_config(job_dir)
        before = {f.name for f in job_dir.iterdir()}

        load_resolved_config_for_job(job_dir)

        after = {f.name for f in job_dir.iterdir()}
        assert before == after


# ===========================================================================
# App pipeline resolved-config failure behaviour (Prompt 6A.5)
# ===========================================================================


def _import_server_app():
    import importlib.util as _ilu

    cached = sys.modules.get("app")
    if cached is not None and hasattr(cached, "_run_mk1_pipeline_after_transcript"):
        return cached

    spec = _ilu.spec_from_file_location(
        "app",
        str(REPO_ROOT / "video-automation" / "server" / "app.py"),
    )
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["app"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _make_pipeline_job(tmp_path: Path) -> dict[str, str]:
    job_dir = tmp_path / "job"
    clips_dir = job_dir / "clips"
    clips_dir.mkdir(parents=True)
    return {
        "job_id": "job_test",
        "job_dir": str(job_dir),
        "clips_dir": str(clips_dir),
        "report_path": str(job_dir / "report.json"),
        "review_path": str(job_dir / "review.md"),
        "analytics_path": str(job_dir / "analytics.json"),
        "normalized_transcript_path": str(job_dir / "transcript_payload.json"),
    }


class TestAppResolvedConfigFailure:
    def test_legacy_job_without_resolved_config_loads_none(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "legacy_job"
        job_dir.mkdir()
        assert load_resolved_config_for_job(job_dir) is None

    def test_malformed_resolved_config_fails_pipeline_clearly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app_mod = _import_server_app()
        job = _make_pipeline_job(tmp_path)
        job_dir = Path(job["job_dir"])
        (job_dir / "resolved_config.yaml").write_text("key: [unclosed")

        pipeline_called = {"value": False}

        def _should_not_run(**_kwargs: Any) -> None:
            pipeline_called["value"] = True
            raise AssertionError("run_processing_pipeline should not be called")

        monkeypatch.setattr(app_mod, "run_processing_pipeline", _should_not_run)

        report: dict[str, Any] = {
            "job_id": "job_test",
            "funnel": {"funnel_id": "business"},
            "clips": [],
            "warnings": [],
        }

        with app_mod.app.app_context():
            resp = app_mod._run_mk1_pipeline_after_transcript(
                report=report,
                job=job,
                jid="job_test",
                warnings=[],
                stage_ms={},
                total_started=0.0,
                video_path=str(tmp_path / "source.mp4"),
                transcript_path=str(tmp_path / "transcript.json"),
                transcript_payload={"segments": []},
                funnel_id="business",
                output_root=str(tmp_path / "output"),
                filename="source",
                filename_prefix="biz",
                delivery_mode="pull_from_output_endpoint",
                input_id="input_1",
                audit_plain={},
            )

        if isinstance(resp, tuple):
            resp, status_code = resp
        else:
            status_code = resp.status_code
        data = resp.get_json()
        assert status_code == 500
        assert data["success"] is False
        assert "invalid" in data["error"].lower()
        assert report["status"] == "failed"
        assert report["errors"][0]["category"] == "configuration_error"
        assert "resolved_config.yaml" in report["errors"][0]["message"]
        assert pipeline_called["value"] is False

        report_on_disk = json.loads(Path(job["report_path"]).read_text(encoding="utf-8"))
        assert report_on_disk["status"] == "failed"
        assert report_on_disk["errors"][0]["category"] == "configuration_error"

    def test_config_backed_job_with_execution_context_and_malformed_config_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app_mod = _import_server_app()
        job = _make_pipeline_job(tmp_path)
        job_dir = Path(job["job_dir"])
        (job_dir / "resolved_config.yaml").write_text("not: valid: yaml: [")
        (job_dir / "execution_context.json").write_text(
            json.dumps(
                {
                    "environment": "development",
                    "job_id": "job_test",
                    "funnel_id": "business",
                    "platform_id": "youtube",
                    "preset_id": "balanced",
                    "config_version": "1",
                }
            )
        )

        monkeypatch.setattr(
            app_mod,
            "run_processing_pipeline",
            lambda **_kw: (_ for _ in ()).throw(
                AssertionError("run_processing_pipeline should not be called")
            ),
        )

        report: dict[str, Any] = {
            "job_id": "job_test",
            "funnel": {"funnel_id": "business"},
            "clips": [],
            "warnings": [],
        }

        with app_mod.app.app_context():
            resp = app_mod._run_mk1_pipeline_after_transcript(
                report=report,
                job=job,
                jid="job_test",
                warnings=[],
                stage_ms={},
                total_started=0.0,
                video_path=str(tmp_path / "source.mp4"),
                transcript_path=str(tmp_path / "transcript.json"),
                transcript_payload={"segments": []},
                funnel_id="business",
                output_root=str(tmp_path / "output"),
                filename="source",
                filename_prefix="biz",
                delivery_mode="pull_from_output_endpoint",
                input_id="input_1",
                audit_plain={},
            )

        if isinstance(resp, tuple):
            resp, status_code = resp
        else:
            status_code = resp.status_code
        data = resp.get_json()
        assert status_code == 500
        assert data["success"] is False
        assert report["status"] == "failed"
        assert not report.get("warnings")

    def test_valid_resolved_config_allows_pipeline_to_continue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import types

        app_mod = _import_server_app()
        job = _make_pipeline_job(tmp_path)
        _write_resolved_config(Path(job["job_dir"]))

        processing_result = types.SimpleNamespace(
            raw_candidate_pool_path="/tmp/pool.json",
            processing_report_path="/tmp/proc_report.json",
            sections_analysed=1,
            usable_sections=1,
            rejected_sections=0,
            failed_sections_count=0,
            final_candidate_count=0,
            duplicates_removed=0,
        )
        monkeypatch.setattr(
            app_mod, "run_processing_pipeline", lambda **_kw: processing_result
        )
        monkeypatch.setattr(
            app_mod.mk1_settings, "resolve_post_processing_enabled", lambda: False
        )

        report: dict[str, Any] = {
            "job_id": "job_test",
            "funnel": {"funnel_id": "business"},
            "clips": [],
            "warnings": [],
        }

        with app_mod.app.app_context():
            resp = app_mod._run_mk1_pipeline_after_transcript(
                report=report,
                job=job,
                jid="job_test",
                warnings=[],
                stage_ms={},
                total_started=0.0,
                video_path=str(tmp_path / "source.mp4"),
                transcript_path=str(tmp_path / "transcript.json"),
                transcript_payload={"segments": []},
                funnel_id="business",
                output_root=str(tmp_path / "output"),
                filename="source",
                filename_prefix="biz",
                delivery_mode="pull_from_output_endpoint",
                input_id="input_1",
                audit_plain={},
            )

        data = resp.get_json()
        assert data["success"] is True
        assert report["status"] != "failed"
