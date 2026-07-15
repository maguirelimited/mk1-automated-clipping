"""
tests/config/test_config_schema.py

Tests for the config schema validator (scripts/config/validate_config.py).

Run with: video-automation/.venv/bin/python -m pytest tests/config/ -v

The validator is imported directly (no subprocess) so tests run fast and
error messages are inspectable in Python.
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from validate_config import validate_config_tree, validate_storage_policy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REAL_CONFIG_ROOT = Path(__file__).resolve().parents[2] / "config"


def _write(tmp_path: Path, rel: str, content: str) -> None:
    """Write a YAML string to a path inside tmp_path."""
    dest = tmp_path / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(textwrap.dedent(content))


def _copy_real_tree(tmp_path: Path) -> None:
    """
    Copy the real config/ tree into tmp_path so individual files can be
    overwritten without touching the actual repo.
    """
    import shutil
    if (tmp_path / "config").exists():
        shutil.rmtree(tmp_path / "config")
    shutil.copytree(REAL_CONFIG_ROOT, tmp_path / "config")


def _assert_no_errors(errors: list[str]) -> None:
    assert errors == [], "Expected no errors but got:\n" + "\n".join(f"  - {e}" for e in errors)


def _assert_error_containing(errors: list[str], *fragments: str) -> None:
    combined = "\n".join(errors)
    for fragment in fragments:
        assert fragment in combined, (
            f"Expected error containing {fragment!r} but got:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )


# ---------------------------------------------------------------------------
# Minimal valid config tree builders
# ---------------------------------------------------------------------------

VALID_DEFAULT = """\
    version: 1
    selection:
      mode: balanced
      max_clips: 6
      min_overall_potential: 7
      min_confidence: 0.6
      exploration_ratio: 0.15
    posting:
      uploads_per_day: 4
    uploading:
      enabled: false
    logging:
      level: INFO
    captions:
      safe_zone:
        top_px: 180
        bottom_px: 320
        left_px: 80
        right_px: 80
      layout:
        font_family: Arial
        font_size: 64
        max_lines: 2
        max_chars_per_line: 32
        max_chars_per_caption: 42
"""

VALID_DEV_ENV = """\
    environment:
      name: development
    paths:
      data_root: data/dev
      jobs_root: jobs/dev
      outputs_root: outputs/dev
      logs_root: logs/dev
      reports_root: reports/dev
      database_path: database/dev.db
    uploading:
      enabled: false
    runtime:
      require_production_secrets: false
"""

VALID_PROD_ENV = """\
    environment:
      name: production
    paths:
      data_root: data/prod
      jobs_root: jobs/prod
      outputs_root: outputs/prod
      logs_root: logs/prod
      reports_root: reports/prod
      database_path: database/prod.db
    uploading:
      enabled: true
    runtime:
      require_production_secrets: true
"""

VALID_STORAGE = """\
    storage:
      retention:
        enabled: false
        source_videos_days: 7
        transcripts_days: 30
        raw_candidate_pools_days: 30
        processing_reports_days: 90
        selection_results_days: 90
        post_processing_reports_days: 90
        clip_metadata_days: 180
        intermediate_renders_days: 14
        temporary_files_days: 3
        logs_days: 30
        run_records_days: 90
        config_snapshots_days: 180
        database_backups_days: 30
        successful_job_artifacts_days: 14
        failed_job_artifacts_days: 90
      disk_pressure:
        warning_percent: 80
        urgent_percent: 90
        critical_percent: 95
        reject_new_jobs_percent: 98
      schedule:
        enabled: false
        mode: disabled
        frequency: daily
      log_rotation:
        enabled: true
        max_bytes: 104857600
        backup_count: 8
        compress: true
        journal:
          system_max_use: 500M
          runtime_max_use: 100M
          max_file_sec: 1month
      database_backup:
        enabled: true
        verify_integrity: true
        location: backups/{{env}}/database
      allowed_delete_roots:
        - jobs
        - logs
        - reports
        - data
        - backups
      protected_artifact_types:
        - final_clip
        - database
      auto_delete_final_clips_prod: false
      allow_final_clip_auto_deletion_opt_in: false
"""

VALID_SYSTEM = f"""\
    system:
      max_concurrent_jobs: 1
      retry_count: 0
      health_check_interval_seconds: 60
    ai:
      processing_model: placeholder-local-model
{VALID_STORAGE}    services:
      restart_policy: on-failure
"""

VALID_FUNNEL_BUSINESS = """\
    funnel:
      id: business
      name: Business
      preset: growth
      enabled: true
    sources:
      channels: []
      rules: []
    selection:
      preferred_topics: []
      blocked_topics: []
    platforms:
      enabled:
        - youtube
"""

VALID_PLATFORM_YOUTUBE = """\
    platform:
      id: youtube
      name: YouTube Shorts
      enabled: true
    uploading:
      enabled: false
    format:
      aspect_ratio: "9:16"
      width: 1080
      height: 1920
      max_duration_seconds: 60
      title_max_length: 100
      caption_max_length: 5000
    accounts:
      default_channel_id: null
    posting:
      default_hashtags: []
      posting_windows: []
"""

VALID_PLATFORM_TIKTOK = """\
    platform:
      id: tiktok
      name: TikTok
      enabled: false
    uploading:
      enabled: false
    format:
      aspect_ratio: "9:16"
      width: 1080
      height: 1920
      max_duration_seconds: 60
      title_max_length: 150
      caption_max_length: 2200
    accounts:
      default_account_id: null
    posting:
      default_hashtags: []
      posting_windows: []
"""

VALID_PLATFORM_INSTAGRAM = """\
    platform:
      id: instagram
      name: Instagram Reels
      enabled: false
    uploading:
      enabled: false
    format:
      aspect_ratio: "9:16"
      width: 1080
      height: 1920
      max_duration_seconds: 90
      title_max_length: 0
      caption_max_length: 2200
    accounts:
      default_account_id: null
    posting:
      default_hashtags: []
      posting_windows: []
"""

VALID_PLATFORM_FACEBOOK = """\
    platform:
      id: facebook
      name: Facebook Reels
      enabled: false
    uploading:
      enabled: false
    format:
      aspect_ratio: "9:16"
      width: 1080
      height: 1920
      max_duration_seconds: 60
      title_max_length: 0
      caption_max_length: 63206
    accounts:
      default_page_id: null
    posting:
      default_hashtags: []
      posting_windows: []
"""

VALID_PLATFORM_X = """\
    platform:
      id: x
      name: X
      enabled: false
    uploading:
      enabled: false
    format:
      aspect_ratio: "9:16"
      width: 1080
      height: 1920
      max_duration_seconds: 60
      title_max_length: 0
      caption_max_length: 280
    accounts:
      default_account_id: null
    posting:
      default_hashtags: []
      posting_windows: []
"""

VALID_PRESET_BALANCED = """\
    preset:
      id: balanced
      name: Balanced
    selection:
      mode: balanced
      max_clips: 6
      min_overall_potential: 7
      min_confidence: 0.6
      exploration_ratio: 0.15
    posting:
      uploads_per_day: 4
    post_processing:
      conveyor:
        - render_clip_v1
        - platform_safe_format_v1
        - validation_v1
        - metadata_writer_v1
"""

VALID_PRESET_GROWTH = """\
    preset:
      id: growth
      name: Growth
    selection:
      mode: growth
      max_clips: 6
      min_overall_potential: 7
      min_confidence: 0.55
      exploration_ratio: 0.2
    posting:
      uploads_per_day: 4
    post_processing:
      conveyor:
        - render_clip_v1
        - platform_safe_format_v1
        - validation_v1
        - metadata_writer_v1
"""

VALID_PRESET_MAXIMUM_QUALITY = """\
    preset:
      id: maximum_quality
      name: Maximum Quality
    selection:
      mode: maximum_quality
      max_clips: 4
      min_overall_potential: 8
      min_confidence: 0.75
      exploration_ratio: 0.05
    posting:
      uploads_per_day: 2
    post_processing:
      conveyor:
        - render_clip_v1
        - platform_safe_format_v1
        - validation_v1
        - metadata_writer_v1
"""


def _write_valid_tree(root: Path) -> None:
    """Write a minimal but fully valid config tree under root."""
    _write(root, "defaults/default.yaml", VALID_DEFAULT)
    _write(root, "environments/dev.yaml", VALID_DEV_ENV)
    _write(root, "environments/prod.yaml", VALID_PROD_ENV)
    _write(root, "system/system.yaml", VALID_SYSTEM)
    _write(root, "funnels/business.yaml", VALID_FUNNEL_BUSINESS)
    _write(root, "platforms/youtube.yaml", VALID_PLATFORM_YOUTUBE)
    _write(root, "platforms/tiktok.yaml", VALID_PLATFORM_TIKTOK)
    _write(root, "platforms/instagram.yaml", VALID_PLATFORM_INSTAGRAM)
    _write(root, "platforms/facebook.yaml", VALID_PLATFORM_FACEBOOK)
    _write(root, "platforms/x.yaml", VALID_PLATFORM_X)
    _write(root, "presets/balanced.yaml", VALID_PRESET_BALANCED)
    _write(root, "presets/growth.yaml", VALID_PRESET_GROWTH)
    _write(root, "presets/maximum_quality.yaml", VALID_PRESET_MAXIMUM_QUALITY)


# ---------------------------------------------------------------------------
# Tests: real config tree
# ---------------------------------------------------------------------------


class TestRealConfigTree:
    """The actual committed config/ tree must pass validation."""

    def test_real_config_passes(self):
        errors = validate_config_tree(REAL_CONFIG_ROOT)
        _assert_no_errors(errors)


# ---------------------------------------------------------------------------
# Tests: valid minimal tree
# ---------------------------------------------------------------------------


class TestValidMinimalTree:
    def test_minimal_valid_tree_passes(self, tmp_path):
        _write_valid_tree(tmp_path)
        errors = validate_config_tree(tmp_path)
        _assert_no_errors(errors)


# ---------------------------------------------------------------------------
# Tests: missing required files
# ---------------------------------------------------------------------------


class TestMissingFiles:
    @pytest.mark.parametrize("missing_rel", [
        "defaults/default.yaml",
        "environments/dev.yaml",
        "environments/prod.yaml",
        "system/system.yaml",
        "funnels/business.yaml",
        "platforms/youtube.yaml",
        "platforms/tiktok.yaml",
        "platforms/instagram.yaml",
        "platforms/facebook.yaml",
        "platforms/x.yaml",
        "presets/balanced.yaml",
        "presets/growth.yaml",
        "presets/maximum_quality.yaml",
    ])
    def test_missing_required_file_fails(self, tmp_path, missing_rel):
        _write_valid_tree(tmp_path)
        (tmp_path / missing_rel).unlink()
        errors = validate_config_tree(tmp_path)
        assert any("missing" in e and missing_rel in e for e in errors), (
            f"Expected error about missing {missing_rel!r}, got: {errors}"
        )


# ---------------------------------------------------------------------------
# Tests: invalid YAML
# ---------------------------------------------------------------------------


class TestInvalidYaml:
    def test_bad_yaml_fails_with_filename(self, tmp_path):
        _write_valid_tree(tmp_path)
        (tmp_path / "defaults/default.yaml").write_text("key: [unclosed bracket\n")
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "defaults/default.yaml")

    def test_bad_yaml_in_platform_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        (tmp_path / "platforms/youtube.yaml").write_text(": bad: yaml: :\n")
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "platforms/youtube.yaml")


# ---------------------------------------------------------------------------
# Tests: dev/prod path separation
# ---------------------------------------------------------------------------


class TestPathSeparation:
    def test_dev_path_in_prod_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "environments/dev.yaml", """\
            environment:
              name: development
            paths:
              data_root: data/prod
              jobs_root: jobs/dev
              outputs_root: outputs/dev
              logs_root: logs/dev
              reports_root: reports/dev
              database_path: database/dev.db
            uploading:
              enabled: false
            runtime:
              require_production_secrets: false
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "prod")

    def test_prod_path_in_dev_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "environments/prod.yaml", """\
            environment:
              name: production
            paths:
              data_root: data/dev
              jobs_root: jobs/prod
              outputs_root: outputs/prod
              logs_root: logs/prod
              reports_root: reports/prod
              database_path: database/prod.db
            uploading:
              enabled: true
            runtime:
              require_production_secrets: true
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "dev")

    def test_equal_db_paths_fail(self, tmp_path):
        _write_valid_tree(tmp_path)
        shared_db = "database/shared.db"
        for env_file, env_name, uploads, secrets in [
            ("environments/dev.yaml", "development", "false", "false"),
            ("environments/prod.yaml", "production", "true", "true"),
        ]:
            _write(tmp_path, env_file, f"""\
                environment:
                  name: {env_name}
                paths:
                  data_root: data/{'dev' if env_name == 'development' else 'prod'}
                  jobs_root: jobs/{'dev' if env_name == 'development' else 'prod'}
                  outputs_root: outputs/{'dev' if env_name == 'development' else 'prod'}
                  logs_root: logs/{'dev' if env_name == 'development' else 'prod'}
                  reports_root: reports/{'dev' if env_name == 'development' else 'prod'}
                  database_path: {shared_db}
                uploading:
                  enabled: {uploads}
                runtime:
                  require_production_secrets: {secrets}
            """)
        errors = validate_config_tree(tmp_path)
        # Both env files have the same database_path AND it's not scoped correctly.
        assert errors, "Expected at least one error for shared db path"

    def test_dev_scoped_path_required(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "environments/dev.yaml", """\
            environment:
              name: development
            paths:
              data_root: data/scratch
              jobs_root: jobs/dev
              outputs_root: outputs/dev
              logs_root: logs/dev
              reports_root: reports/dev
              database_path: database/dev.db
            uploading:
              enabled: false
            runtime:
              require_production_secrets: false
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "data_root", "dev")


# ---------------------------------------------------------------------------
# Tests: upload safety
# ---------------------------------------------------------------------------


class TestUploadSafety:
    def test_dev_uploads_enabled_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "environments/dev.yaml", """\
            environment:
              name: development
            paths:
              data_root: data/dev
              jobs_root: jobs/dev
              outputs_root: outputs/dev
              logs_root: logs/dev
              reports_root: reports/dev
              database_path: database/dev.db
            uploading:
              enabled: true
            runtime:
              require_production_secrets: false
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "uploading.enabled", "dev.yaml")

    def test_final_clip_auto_delete_prod_enabled_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        storage = yaml.safe_load(textwrap.dedent(VALID_STORAGE))
        storage["storage"]["auto_delete_final_clips_prod"] = True
        storage["storage"]["allow_final_clip_auto_deletion_opt_in"] = False
        system = {
            "system": {
                "max_concurrent_jobs": 1,
                "retry_count": 0,
                "health_check_interval_seconds": 60,
            },
            "ai": {"processing_model": "placeholder-local-model"},
            **storage,
            "services": {"restart_policy": "on-failure"},
        }
        (tmp_path / "system" / "system.yaml").write_text(
            yaml.dump(system, default_flow_style=False)
        )
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "auto_delete_final_clips_prod")


# ---------------------------------------------------------------------------
# Tests: storage retention policy (configuration only)
# ---------------------------------------------------------------------------


def _write_system_storage(tmp_path: Path, mutate) -> None:
    """Write system.yaml with VALID_STORAGE, applying mutate(storage_dict)."""
    storage = yaml.safe_load(textwrap.dedent(VALID_STORAGE))
    mutate(storage["storage"])
    system = {
        "system": {
            "max_concurrent_jobs": 1,
            "retry_count": 0,
            "health_check_interval_seconds": 60,
        },
        "ai": {"processing_model": "placeholder-local-model"},
        **storage,
        "services": {"restart_policy": "on-failure"},
    }
    (tmp_path / "system" / "system.yaml").write_text(
        yaml.dump(system, default_flow_style=False)
    )


class TestStorageRetentionPolicy:
    def test_real_config_tree_valid(self):
        errors = validate_config_tree(REAL_CONFIG_ROOT)
        _assert_no_errors(errors)

    def test_negative_retention_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write_system_storage(
            tmp_path, lambda s: s["retention"].__setitem__("source_videos_days", -1)
        )
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "source_videos_days")

    def test_failed_shorter_than_successful_fails(self, tmp_path):
        _write_valid_tree(tmp_path)

        def mutate(storage):
            storage["retention"]["successful_job_artifacts_days"] = 30
            storage["retention"]["failed_job_artifacts_days"] = 14

        _write_system_storage(tmp_path, mutate)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "failed_job_artifacts_days")

    def test_empty_roots_when_retention_enabled_fails(self, tmp_path):
        _write_valid_tree(tmp_path)

        def mutate(storage):
            storage["retention"]["enabled"] = True
            storage["allowed_delete_roots"] = []

        _write_system_storage(tmp_path, mutate)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "allowed_delete_roots")

    def test_auto_delete_with_explicit_opt_in_passes(self, tmp_path):
        _write_valid_tree(tmp_path)

        def mutate(storage):
            storage["auto_delete_final_clips_prod"] = True
            storage["allow_final_clip_auto_deletion_opt_in"] = True
            storage["protected_artifact_types"] = ["database"]

        _write_system_storage(tmp_path, mutate)
        errors = validate_config_tree(tmp_path)
        _assert_no_errors(errors)

    def test_protected_types_must_include_database(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write_system_storage(
            tmp_path, lambda s: s.__setitem__("protected_artifact_types", ["final_clip"])
        )
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "database")

    def test_protected_types_must_include_final_clip_by_default(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write_system_storage(
            tmp_path, lambda s: s.__setitem__("protected_artifact_types", ["database"])
        )
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "final_clip")

    def test_warning_not_less_than_urgent_fails(self, tmp_path):
        _write_valid_tree(tmp_path)

        def mutate(storage):
            storage["disk_pressure"]["warning_percent"] = 90
            storage["disk_pressure"]["urgent_percent"] = 90

        _write_system_storage(tmp_path, mutate)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "disk_pressure")

    def test_validate_storage_policy_direct(self):
        storage = yaml.safe_load(textwrap.dedent(VALID_STORAGE))["storage"]
        errors: list[str] = []
        validate_storage_policy(storage, errors, "direct", require_complete=True)
        _assert_no_errors(errors)


# ---------------------------------------------------------------------------
# Tests: cross-reference validation
# ---------------------------------------------------------------------------


class TestCrossReferences:
    def test_funnel_references_missing_preset_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "funnels/business.yaml", """\
            funnel:
              id: business
              name: Business
              preset: nonexistent_preset
              enabled: true
            sources:
              channels: []
              rules: []
            selection:
              preferred_topics: []
              blocked_topics: []
            platforms:
              enabled:
                - youtube
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "nonexistent_preset")

    def test_funnel_references_missing_platform_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "funnels/business.yaml", """\
            funnel:
              id: business
              name: Business
              preset: growth
              enabled: true
            sources:
              channels: []
              rules: []
            selection:
              preferred_topics: []
              blocked_topics: []
            platforms:
              enabled:
                - snapchat
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "snapchat")

    def test_platform_id_mismatch_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "platforms/youtube.yaml", """\
            platform:
              id: tiktok
              name: YouTube Shorts
              enabled: true
            uploading:
              enabled: false
            format:
              aspect_ratio: "9:16"
              width: 1080
              height: 1920
              max_duration_seconds: 60
              title_max_length: 100
              caption_max_length: 5000
            accounts:
              default_channel_id: null
            posting:
              default_hashtags: []
              posting_windows: []
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "platform.id", "youtube")

    def test_preset_id_mismatch_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "presets/balanced.yaml", """\
            preset:
              id: growth
              name: Balanced
            selection:
              mode: balanced
              max_clips: 6
              min_overall_potential: 7
              min_confidence: 0.6
              exploration_ratio: 0.15
            posting:
              uploads_per_day: 4
            post_processing:
              conveyor:
                - render_clip_v1
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "preset.id", "balanced")


# ---------------------------------------------------------------------------
# Tests: numeric validation
# ---------------------------------------------------------------------------


class TestNumericValidation:
    def test_confidence_above_one_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "defaults/default.yaml", """\
            version: 1
            selection:
              mode: balanced
              max_clips: 6
              min_overall_potential: 7
              min_confidence: 1.5
              exploration_ratio: 0.15
            posting:
              uploads_per_day: 4
            uploading:
              enabled: false
            logging:
              level: INFO
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "min_confidence")

    def test_confidence_below_zero_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "defaults/default.yaml", """\
            version: 1
            selection:
              mode: balanced
              max_clips: 6
              min_overall_potential: 7
              min_confidence: -0.1
              exploration_ratio: 0.15
            posting:
              uploads_per_day: 4
            uploading:
              enabled: false
            logging:
              level: INFO
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "min_confidence")

    def test_disk_pressure_wrong_order_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        storage = yaml.safe_load(textwrap.dedent(VALID_STORAGE))
        storage["storage"]["disk_pressure"] = {
            "warning_percent": 90,
            "urgent_percent": 80,
            "critical_percent": 95,
            "reject_new_jobs_percent": 98,
        }
        system = {
            "system": {
                "max_concurrent_jobs": 1,
                "retry_count": 0,
                "health_check_interval_seconds": 60,
            },
            "ai": {"processing_model": "placeholder-local-model"},
            **storage,
            "services": {"restart_policy": "on-failure"},
        }
        (tmp_path / "system" / "system.yaml").write_text(
            yaml.dump(system, default_flow_style=False)
        )
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "disk_pressure")

    def test_max_clips_zero_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "defaults/default.yaml", """\
            version: 1
            selection:
              mode: balanced
              max_clips: 0
              min_overall_potential: 7
              min_confidence: 0.6
              exploration_ratio: 0.15
            posting:
              uploads_per_day: 4
            uploading:
              enabled: false
            logging:
              level: INFO
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "max_clips")


# ---------------------------------------------------------------------------
# Tests: secret safety
# ---------------------------------------------------------------------------


class TestSecretSafety:
    def test_non_empty_api_key_value_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "defaults/default.yaml", """\
            version: 1
            selection:
              mode: balanced
              max_clips: 6
              min_overall_potential: 7
              min_confidence: 0.6
              exploration_ratio: 0.15
            posting:
              uploads_per_day: 4
            uploading:
              enabled: false
            logging:
              level: INFO
            api_key: sk-abcdefghijklmnopqrst
        """)
        errors = validate_config_tree(tmp_path)
        # Two errors expected: unknown top-level key 'api_key' AND secret value
        assert errors, "Expected at least one error for secret-looking content"
        combined = "\n".join(errors)
        assert "api_key" in combined or "secret" in combined.lower()

    def test_real_credential_value_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "platforms/youtube.yaml", """\
            platform:
              id: youtube
              name: YouTube Shorts
              enabled: true
            uploading:
              enabled: false
            format:
              aspect_ratio: "9:16"
              width: 1080
              height: 1920
              max_duration_seconds: 60
              title_max_length: 100
              caption_max_length: 5000
            accounts:
              default_channel_id: "sk-abcdefghijklmnopqrstuvwxyz"
            posting:
              default_hashtags: []
              posting_windows: []
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "credential")

    def test_null_account_id_passes(self, tmp_path):
        """null account IDs must not trigger secret detection."""
        _write_valid_tree(tmp_path)
        # The default youtube.yaml has default_channel_id: null — should pass.
        errors = validate_config_tree(tmp_path)
        _assert_error_containing  # just ensure real tree passes
        _assert_no_errors(errors)


# ---------------------------------------------------------------------------
# Tests: unknown top-level keys
# ---------------------------------------------------------------------------


class TestUnknownKeys:
    def test_unknown_top_level_key_in_defaults_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "defaults/default.yaml", """\
            version: 1
            selection:
              mode: balanced
              max_clips: 6
              min_overall_potential: 7
              min_confidence: 0.6
              exploration_ratio: 0.15
            posting:
              uploads_per_day: 4
            uploading:
              enabled: false
            logging:
              level: INFO
            mystery_section:
              value: 42
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "mystery_section")

    def test_unknown_top_level_key_in_system_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        storage = yaml.safe_load(textwrap.dedent(VALID_STORAGE))
        system = {
            "system": {
                "max_concurrent_jobs": 1,
                "retry_count": 0,
                "health_check_interval_seconds": 60,
            },
            "ai": {"processing_model": "placeholder-local-model"},
            **storage,
            "services": {"restart_policy": "on-failure"},
            "mystery_section": {},
        }
        (tmp_path / "system" / "system.yaml").write_text(
            yaml.dump(system, default_flow_style=False)
        )
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "mystery_section")


# ---------------------------------------------------------------------------
# Tests: environment name rules
# ---------------------------------------------------------------------------


class TestEnvironmentNames:
    def test_dev_with_wrong_name_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "environments/dev.yaml", """\
            environment:
              name: production
            paths:
              data_root: data/dev
              jobs_root: jobs/dev
              outputs_root: outputs/dev
              logs_root: logs/dev
              reports_root: reports/dev
              database_path: database/dev.db
            uploading:
              enabled: false
            runtime:
              require_production_secrets: false
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "environment.name", "development", "dev.yaml")

    def test_prod_require_production_secrets_false_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "environments/prod.yaml", """\
            environment:
              name: production
            paths:
              data_root: data/prod
              jobs_root: jobs/prod
              outputs_root: outputs/prod
              logs_root: logs/prod
              reports_root: reports/prod
              database_path: database/prod.db
            uploading:
              enabled: true
            runtime:
              require_production_secrets: false
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "require_production_secrets", "prod.yaml")


# ---------------------------------------------------------------------------
# Tests: database path
# ---------------------------------------------------------------------------


class TestDatabasePath:
    def test_database_path_without_db_extension_fails(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "environments/dev.yaml", """\
            environment:
              name: development
            paths:
              data_root: data/dev
              jobs_root: jobs/dev
              outputs_root: outputs/dev
              logs_root: logs/dev
              reports_root: reports/dev
              database_path: database/dev-no-extension
            uploading:
              enabled: false
            runtime:
              require_production_secrets: false
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "database_path", ".db")


# ---------------------------------------------------------------------------
# Tests: preset and platform id/file consistency
# ---------------------------------------------------------------------------


class TestIdConsistency:
    def test_preset_conveyor_must_be_list_of_strings(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "presets/balanced.yaml", """\
            preset:
              id: balanced
              name: Balanced
            selection:
              mode: balanced
              max_clips: 6
              min_overall_potential: 7
              min_confidence: 0.6
              exploration_ratio: 0.15
            posting:
              uploads_per_day: 4
            post_processing:
              conveyor: not_a_list
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "conveyor")

    def test_platform_uploading_enabled_is_boolean(self, tmp_path):
        _write_valid_tree(tmp_path)
        _write(tmp_path, "platforms/youtube.yaml", """\
            platform:
              id: youtube
              name: YouTube Shorts
              enabled: true
            uploading:
              enabled: "yes"
            format:
              aspect_ratio: "9:16"
              width: 1080
              height: 1920
              max_duration_seconds: 60
              title_max_length: 100
              caption_max_length: 5000
            accounts:
              default_channel_id: null
            posting:
              default_hashtags: []
              posting_windows: []
        """)
        errors = validate_config_tree(tmp_path)
        _assert_error_containing(errors, "uploading.enabled")
