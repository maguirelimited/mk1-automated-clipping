"""
tests/config/test_state_paths.py

Tests for EnvironmentStatePaths (scripts/config/state_paths.py).

Run with:
    video-automation/.venv/bin/python -m pytest tests/config/ -v

All tests use tmp_path to avoid touching real config or filesystem state.
No test depends on machine-specific absolute paths.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from config_manager import ConfigManager
from state_paths import EnvironmentStatePaths, _is_under

# ---------------------------------------------------------------------------
# Shared tree builder (mirrors test_config_manager.py to avoid test coupling)
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, content: str) -> None:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(textwrap.dedent(content))


def _build_valid_tree(root: Path) -> None:
    _write(root, "defaults/default.yaml", """
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
    """)
    _write(root, "environments/dev.yaml", """
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
    """)
    _write(root, "environments/prod.yaml", """
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
    """)
    _write(root, "system/system.yaml", """
        system:
          max_concurrent_jobs: 1
          retry_count: 0
          health_check_interval_seconds: 60
        ai:
          processing_model: placeholder-local-model
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
        services:
          restart_policy: on-failure
    """)
    _write(root, "funnels/business.yaml", """
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
    """)
    for pid in ("youtube", "tiktok", "instagram", "facebook", "x"):
        _write(root, f"platforms/{pid}.yaml", f"""
            platform:
              id: {pid}
              name: {pid.title()}
              enabled: false
            uploading:
              enabled: false
            format:
              aspect_ratio: "9:16"
              width: 1080
              height: 1920
              max_duration_seconds: 60
              title_max_length: 100
              caption_max_length: 2200
            accounts:
              default_account_id: null
            posting:
              default_hashtags: []
              posting_windows: []
        """)
    for pid in ("balanced", "growth", "maximum_quality"):
        _write(root, f"presets/{pid}.yaml", f"""
            preset:
              id: {pid}
              name: {pid.replace("_", " ").title()}
            selection:
              mode: {pid}
              max_clips: 6
              min_overall_potential: 7
              min_confidence: 0.6
              exploration_ratio: 0.15
            posting:
              uploads_per_day: 4
            post_processing:
              conveyor:
                - render_clip_v1
                - validation_v1
        """)


def _state(tmp_path: Path, environment: str) -> EnvironmentStatePaths:
    """Build a valid config tree in tmp_path and return state paths for environment."""
    _build_valid_tree(tmp_path)
    config = ConfigManager.load(environment=environment, config_root=tmp_path)
    return config.state_paths


# ---------------------------------------------------------------------------
# State path construction
# ---------------------------------------------------------------------------


class TestStatePathConstruction:
    def test_dev_paths_are_dev_scoped(self, tmp_path):
        s = _state(tmp_path, "dev")
        assert "dev" in str(s.jobs_root)
        assert "dev" in str(s.outputs_root)
        assert "dev" in str(s.logs_root)
        assert "dev" in str(s.reports_root)
        assert "dev" in str(s.data_root)
        assert "dev" in str(s.database_path)

    def test_prod_paths_are_prod_scoped(self, tmp_path):
        s = _state(tmp_path, "prod")
        assert "prod" in str(s.jobs_root)
        assert "prod" in str(s.outputs_root)
        assert "prod" in str(s.logs_root)
        assert "prod" in str(s.reports_root)
        assert "prod" in str(s.data_root)
        assert "prod" in str(s.database_path)

    def test_dev_and_prod_database_paths_differ(self, tmp_path):
        dev = _state(tmp_path, "dev")
        prod = _state(tmp_path, "prod")
        assert dev.database_path != prod.database_path

    def test_dev_and_prod_jobs_roots_differ(self, tmp_path):
        dev = _state(tmp_path, "dev")
        prod = _state(tmp_path, "prod")
        assert dev.jobs_root != prod.jobs_root

    def test_dev_and_prod_outputs_roots_differ(self, tmp_path):
        dev = _state(tmp_path, "dev")
        prod = _state(tmp_path, "prod")
        assert dev.outputs_root != prod.outputs_root

    def test_dev_and_prod_logs_roots_differ(self, tmp_path):
        dev = _state(tmp_path, "dev")
        prod = _state(tmp_path, "prod")
        assert dev.logs_root != prod.logs_root

    def test_derived_clips_root_is_scoped(self, tmp_path):
        dev = _state(tmp_path, "dev")
        prod = _state(tmp_path, "prod")
        assert "dev" in str(dev.clips_root)
        assert "prod" in str(prod.clips_root)
        assert dev.clips_root != prod.clips_root

    def test_derived_transcripts_root_is_scoped(self, tmp_path):
        dev = _state(tmp_path, "dev")
        prod = _state(tmp_path, "prod")
        assert "dev" in str(dev.transcripts_root)
        assert "prod" in str(prod.transcripts_root)
        assert dev.transcripts_root != prod.transcripts_root

    def test_derived_caches_root_is_scoped(self, tmp_path):
        dev = _state(tmp_path, "dev")
        prod = _state(tmp_path, "prod")
        assert "dev" in str(dev.caches_root)
        assert "prod" in str(prod.caches_root)
        assert dev.caches_root != prod.caches_root

    def test_clips_under_outputs(self, tmp_path):
        s = _state(tmp_path, "dev")
        assert _is_under(s.clips_root, s.outputs_root)

    def test_transcripts_under_data(self, tmp_path):
        s = _state(tmp_path, "dev")
        assert _is_under(s.transcripts_root, s.data_root)

    def test_caches_under_data(self, tmp_path):
        s = _state(tmp_path, "dev")
        assert _is_under(s.caches_root, s.data_root)

    def test_all_paths_absolute(self, tmp_path):
        for env in ("dev", "prod"):
            s = _state(tmp_path, env)
            assert s.data_root.is_absolute()
            assert s.jobs_root.is_absolute()
            assert s.clips_root.is_absolute()
            assert s.transcripts_root.is_absolute()
            assert s.caches_root.is_absolute()


# ---------------------------------------------------------------------------
# Directory creation
# ---------------------------------------------------------------------------


class TestDirectoryCreation:
    def test_load_does_not_create_directories(self, tmp_path):
        """ConfigManager.load() must not mutate the filesystem."""
        _build_valid_tree(tmp_path)
        ConfigManager.load(environment="dev", config_root=tmp_path)
        # State directories must not have been created by load()
        assert not (tmp_path / "jobs" / "dev").exists()
        assert not (tmp_path / "data" / "dev").exists()

    def test_ensure_directories_creates_env_scoped_dirs(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        s.ensure_directories()
        assert s.jobs_root.is_dir()
        assert s.outputs_root.is_dir()
        assert s.logs_root.is_dir()
        assert s.reports_root.is_dir()
        assert s.clips_root.is_dir()
        assert s.transcripts_root.is_dir()
        assert s.caches_root.is_dir()

    def test_ensure_directories_creates_database_parent_not_db_file(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        s.ensure_directories()
        assert s.database_path.parent.is_dir()
        assert not s.database_path.exists(), "ensure_directories must not create the database file"

    def test_ensure_directories_does_not_create_prod_dirs_for_dev(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        s.ensure_directories()
        assert not (tmp_path / "jobs" / "prod").exists()
        assert not (tmp_path / "data" / "prod").exists()


# ---------------------------------------------------------------------------
# Environment guard
# ---------------------------------------------------------------------------


class TestEnvironmentGuard:
    def test_dev_rejects_prod_job_path(self, tmp_path):
        _build_valid_tree(tmp_path)
        dev = _state(tmp_path, "dev")
        prod = _state(tmp_path, "prod")
        prod_path = prod.jobs_root / "job_001"
        assert not dev.is_within_environment(prod_path)

    def test_prod_rejects_dev_job_path(self, tmp_path):
        _build_valid_tree(tmp_path)
        dev = _state(tmp_path, "dev")
        prod = _state(tmp_path, "prod")
        dev_path = dev.jobs_root / "job_001"
        assert not prod.is_within_environment(dev_path)

    def test_assert_within_environment_raises_for_prod_path_in_dev(self, tmp_path):
        _build_valid_tree(tmp_path)
        dev = _state(tmp_path, "dev")
        prod = _state(tmp_path, "prod")
        with pytest.raises(ValueError, match="development"):
            dev.assert_within_environment(prod.jobs_root / "job_001")

    def test_assert_within_environment_raises_for_dev_path_in_prod(self, tmp_path):
        _build_valid_tree(tmp_path)
        dev = _state(tmp_path, "dev")
        prod = _state(tmp_path, "prod")
        with pytest.raises(ValueError, match="production"):
            prod.assert_within_environment(dev.jobs_root / "job_001")

    def test_path_traversal_rejected(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        # A path that tries to escape via .. from within jobs/dev
        traversal = s.jobs_root / ".." / ".." / "etc" / "passwd"
        # resolve() collapses the .., so the final path should be outside allowed roots
        assert not s.is_within_environment(traversal.resolve())

    def test_absolute_external_path_rejected(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        assert not s.is_within_environment(Path("/etc/passwd"))
        assert not s.is_within_environment(Path("/tmp/some_file"))

    def test_dev_path_accepted_by_dev_guard(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        path = s.jobs_root / "job_abc"
        assert s.is_within_environment(path)

    def test_prod_path_accepted_by_prod_guard(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "prod")
        path = s.jobs_root / "job_abc"
        assert s.is_within_environment(path)


# ---------------------------------------------------------------------------
# Job path helper
# ---------------------------------------------------------------------------


class TestJobDirHelper:
    def test_valid_job_id_produces_path_under_jobs_root(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        p = s.job_dir("job_20260101T120000Z_abc12345")
        assert _is_under(p, s.jobs_root)

    def test_empty_job_id_fails(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        with pytest.raises(ValueError, match="empty"):
            s.job_dir("")

    def test_traversal_job_id_fails(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        with pytest.raises(ValueError):
            s.job_dir("../prod/job_x")

    def test_absolute_job_id_fails(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        with pytest.raises(ValueError):
            s.job_dir("/etc/job_x")

    def test_job_id_with_slash_fails(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        with pytest.raises(ValueError, match="separator"):
            s.job_dir("job/sub")

    def test_dotdot_in_job_id_fails(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        with pytest.raises(ValueError, match="\\.\\."):
            s.job_dir("job..evil")

    def test_log_file_helper(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        p = s.log_file("pipeline.log")
        assert _is_under(p, s.logs_root)

    def test_log_file_traversal_rejected(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        with pytest.raises(ValueError):
            s.log_file("../prod/something.log")

    def test_report_dir_no_job(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        assert s.report_dir() == s.reports_root

    def test_report_dir_with_job_id(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        p = s.report_dir("job_001")
        assert _is_under(p, s.reports_root)

    def test_output_dir_with_job_id(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        p = s.output_dir("job_001")
        assert _is_under(p, s.outputs_root)


# ---------------------------------------------------------------------------
# Snapshot compatibility
# ---------------------------------------------------------------------------


class TestSnapshotCompatibility:
    def test_snapshot_inside_dev_job_dir(self, tmp_path):
        _build_valid_tree(tmp_path)
        config = ConfigManager.load(environment="dev", config_root=tmp_path)
        s = config.state_paths
        job_dir = s.job_dir("test_job")
        snap = config.save_snapshot(job_dir)
        assert _is_under(snap, s.jobs_root)
        assert "dev" in str(snap)

    def test_snapshot_inside_prod_job_dir(self, tmp_path):
        _build_valid_tree(tmp_path)
        config = ConfigManager.load(environment="prod", config_root=tmp_path)
        s = config.state_paths
        job_dir = s.job_dir("test_job")
        snap = config.save_snapshot(job_dir)
        assert _is_under(snap, s.jobs_root)
        assert "prod" in str(snap)

    def test_dev_snapshot_not_under_prod_root(self, tmp_path):
        _build_valid_tree(tmp_path)
        dev_config = ConfigManager.load(environment="dev", config_root=tmp_path)
        dev_state = dev_config.state_paths
        prod_state = EnvironmentStatePaths.from_resolved_config(
            ConfigManager.load(environment="prod", config_root=tmp_path)
        )
        job_dir = dev_state.job_dir("test_job")
        snap = dev_config.save_snapshot(job_dir)
        # The dev snapshot must not be inside the prod jobs root
        assert not _is_under(snap, prod_state.jobs_root)


# ---------------------------------------------------------------------------
# Upload state / precedence documentation
# ---------------------------------------------------------------------------


class TestUploadStatePrecedence:
    def test_dev_config_level_uploading_is_false(self, tmp_path):
        _build_valid_tree(tmp_path)
        config = ConfigManager.load(environment="dev", config_root=tmp_path)
        assert config.uploading_enabled is False

    def test_prod_config_level_uploading_follows_environment(self, tmp_path):
        """
        Prod env says uploading.enabled: true.
        Platform says uploading.enabled: false.
        Environment must win — platform does not veto environment.
        """
        _build_valid_tree(tmp_path)
        config = ConfigManager.load(environment="prod", config_root=tmp_path)
        assert config.uploading_enabled is True

    def test_no_runtime_kill_switch_file_read(self, tmp_path):
        """
        Runtime kill switch is not yet implemented.
        Even if control_state.json exists, uploading_enabled must reflect
        config only.
        """
        _build_valid_tree(tmp_path)
        # Simulate a future control_state.json file
        control_dir = tmp_path / "data" / "prod"
        control_dir.mkdir(parents=True)
        (control_dir / "control_state.json").write_text('{"uploads_disabled": true}')

        config = ConfigManager.load(environment="prod", config_root=tmp_path)
        assert config.uploading_enabled is True  # kill switch not read yet

    def test_platform_does_not_veto_environment_upload(self, tmp_path):
        """
        Explicitly verify that platform uploading.enabled: false does not
        block uploads when environment says true.

        This is the Prompt 3 ambiguity fix documented in state_paths.py.
        """
        _build_valid_tree(tmp_path)
        # Platform youtube has uploading.enabled: false
        # Prod environment has uploading.enabled: true
        config = ConfigManager.load(
            environment="prod",
            platform_id="youtube",
            config_root=tmp_path,
        )
        assert config.uploading_enabled is True, (
            "Environment uploading.enabled must take precedence over platform uploading.enabled. "
            "Platform 'false' must not veto environment 'true'."
        )


# ---------------------------------------------------------------------------
# as_dict / as_table helpers
# ---------------------------------------------------------------------------


class TestIntrospection:
    def test_as_dict_contains_all_keys(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        d = s.as_dict()
        for key in ("environment", "data_root", "jobs_root", "outputs_root",
                    "logs_root", "reports_root", "database_path",
                    "clips_root", "transcripts_root", "caches_root"):
            assert key in d, f"Missing key in as_dict: {key}"

    def test_as_table_returns_string(self, tmp_path):
        _build_valid_tree(tmp_path)
        s = _state(tmp_path, "dev")
        table = s.as_table()
        assert "jobs" in table
        assert "database" in table
        assert "clips" in table
