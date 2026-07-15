"""
tests/config/test_platform_format_config.py

Tests for Prompt 6B: Platform formatting and caption layout config integration.

Run with:
    video-automation/.venv/bin/python -m pytest tests/config/test_platform_format_config.py -v
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_CONFIG_DIR = REPO_ROOT / "scripts" / "config"
if str(SCRIPTS_CONFIG_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_CONFIG_DIR))

_VA_SCRIPTS = REPO_ROOT / "video-automation" / "scripts"
if str(_VA_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_VA_SCRIPTS))

from execution_context import (  # noqa: E402
    ResolvedConfigLoadError,
    extract_conveyor_config_from_resolved,
    load_resolved_config_for_job,
)
from validate_config import validate_config_tree  # noqa: E402


def _write(root: Path, rel: str, content: str) -> None:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(textwrap.dedent(content))


RESOLVED_CONFIG_SAMPLE = {
    "version": 1,
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


class TestExtractConveyorConfigFromResolved:
    def test_maps_format_dimensions(self) -> None:
        out = extract_conveyor_config_from_resolved(RESOLVED_CONFIG_SAMPLE)
        assert out["target_width"] == 1080
        assert out["target_height"] == 1920
        assert out["platform_aspect_ratio"] == "9:16"
        assert out["platform_max_duration_seconds"] == 60
        assert out["platform_title_max_length"] == 100
        assert out["platform_caption_max_length"] == 5000

    def test_maps_face_track_test_enabled_when_present(self) -> None:
        resolved = {
            **RESOLVED_CONFIG_SAMPLE,
            "format": {
                **RESOLVED_CONFIG_SAMPLE["format"],
                "face_track_test_enabled": True,
            },
        }
        out = extract_conveyor_config_from_resolved(resolved)
        assert out["face_track_test_enabled"] is True

    def test_omits_face_track_test_enabled_when_absent(self) -> None:
        out = extract_conveyor_config_from_resolved(RESOLVED_CONFIG_SAMPLE)
        assert "face_track_test_enabled" not in out

    def test_maps_format_reframe_mode_when_present(self) -> None:
        resolved = {
            **RESOLVED_CONFIG_SAMPLE,
            "format": {
                **RESOLVED_CONFIG_SAMPLE["format"],
                "reframe_mode": "auto",
            },
        }
        out = extract_conveyor_config_from_resolved(resolved)
        assert out["reframe_mode"] == "auto"

    def test_omits_reframe_mode_when_absent(self) -> None:
        out = extract_conveyor_config_from_resolved(RESOLVED_CONFIG_SAMPLE)
        assert "reframe_mode" not in out

    def test_maps_caption_safe_zone_and_layout(self) -> None:
        out = extract_conveyor_config_from_resolved(RESOLVED_CONFIG_SAMPLE)
        assert out["safe_zone_top_px"] == 180
        assert out["safe_zone_bottom_px"] == 320
        assert out["safe_zone_left_px"] == 80
        assert out["safe_zone_right_px"] == 80
        assert out["font_family"] == "Arial"
        assert out["font_size"] == 64
        assert out["max_lines"] == 2
        assert out["max_chars_per_line"] == 32
        assert out["max_chars_per_caption"] == 42

    def test_empty_sections_return_empty_dict(self) -> None:
        assert extract_conveyor_config_from_resolved({}) == {}

    def test_does_not_merge_selection_duration(self) -> None:
        """platform max_duration must stay separate from selection filter."""
        resolved = {
            **RESOLVED_CONFIG_SAMPLE,
            "selection": {"max_duration_sec": 120},
        }
        out = extract_conveyor_config_from_resolved(resolved)
        assert out["platform_max_duration_seconds"] == 60
        assert "max_duration_sec" not in out


class TestPlatformFormatSchemaValidation:
    def test_real_config_tree_passes(self) -> None:
        errors = validate_config_tree(REPO_ROOT / "config")
        assert errors == [], "\n".join(errors)

    def test_invalid_format_width_fails(self, tmp_path: Path) -> None:
        import shutil

        shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
        platform = tmp_path / "config" / "platforms" / "youtube.yaml"
        data = yaml.safe_load(platform.read_text(encoding="utf-8"))
        data["format"]["width"] = 0
        platform.write_text(yaml.dump(data), encoding="utf-8")
        errors = validate_config_tree(tmp_path / "config")
        assert any("format.width" in e for e in errors)

    def test_invalid_caption_font_size_fails(self, tmp_path: Path) -> None:
        import shutil

        shutil.copytree(REPO_ROOT / "config", tmp_path / "config")
        defaults = tmp_path / "config" / "defaults" / "default.yaml"
        data = yaml.safe_load(defaults.read_text(encoding="utf-8"))
        data["captions"]["layout"]["font_size"] = 0
        defaults.write_text(yaml.dump(data), encoding="utf-8")
        errors = validate_config_tree(tmp_path / "config")
        assert any("captions.layout.font_size" in e for e in errors)


class TestModuleConfigConsumption:
    def test_platform_safe_format_uses_config_dimensions(self) -> None:
        from platform_safe_format_v1 import (  # noqa: PLC0415
            PlatformSafeFormatV1Module,
            _DEFAULT_CONFIG,
            _validate_format_config,
        )

        module_config = {
            **_DEFAULT_CONFIG,
            **extract_conveyor_config_from_resolved(RESOLVED_CONFIG_SAMPLE),
        }
        assert _validate_format_config(module_config) is None
        assert module_config["target_width"] == 1080
        assert module_config["target_height"] == 1920

        module = PlatformSafeFormatV1Module()
        ctx = {"config": extract_conveyor_config_from_resolved(RESOLVED_CONFIG_SAMPLE)}
        merged = {**_DEFAULT_CONFIG, **ctx["config"]}
        assert merged["target_width"] == 1080
        assert merged["safe_zone_bottom_px"] == 320
        assert module.module_name == "platform_safe_format_v1"

    def test_intelligent_captions_uses_config_layout(self) -> None:
        from intelligent_captions_v1 import (  # noqa: PLC0415
            _DEFAULT_CONFIG,
            _validate_caption_config,
        )

        module_config = {
            **_DEFAULT_CONFIG,
            **extract_conveyor_config_from_resolved(RESOLVED_CONFIG_SAMPLE),
        }
        assert _validate_caption_config(module_config) is None
        assert module_config["font_size"] == 64
        assert module_config["max_lines"] == 2

    def test_config_matching_defaults_produces_same_merge_as_legacy(self) -> None:
        from platform_safe_format_v1 import _DEFAULT_CONFIG  # noqa: PLC0415
        from intelligent_captions_v1 import _DEFAULT_CONFIG as CAP_DEFAULT  # noqa: PLC0415

        extracted = extract_conveyor_config_from_resolved(RESOLVED_CONFIG_SAMPLE)
        fmt_merged = {**_DEFAULT_CONFIG, **extracted}
        cap_merged = {**CAP_DEFAULT, **extracted}

        assert fmt_merged["target_width"] == _DEFAULT_CONFIG["target_width"]
        assert fmt_merged["target_height"] == _DEFAULT_CONFIG["target_height"]
        assert fmt_merged["safe_zone_top_px"] == _DEFAULT_CONFIG["safe_zone_top_px"]
        assert cap_merged["font_size"] == CAP_DEFAULT["font_size"]
        assert cap_merged["max_lines"] == CAP_DEFAULT["max_lines"]


class TestResolvedConfigLoadingRegression:
    def test_loads_format_and_captions_from_snapshot(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job_cfg"
        job_dir.mkdir()
        (job_dir / "resolved_config.yaml").write_text(yaml.dump(RESOLVED_CONFIG_SAMPLE))

        loaded = load_resolved_config_for_job(job_dir)
        assert loaded is not None
        assert loaded["format"]["width"] == 1080
        assert loaded["captions"]["layout"]["font_size"] == 64

    def test_missing_snapshot_returns_none(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "legacy"
        job_dir.mkdir()
        assert load_resolved_config_for_job(job_dir) is None

    def test_malformed_snapshot_still_fails(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "broken"
        job_dir.mkdir()
        (job_dir / "resolved_config.yaml").write_text("key: [unclosed")
        with pytest.raises(ResolvedConfigLoadError):
            load_resolved_config_for_job(job_dir)
