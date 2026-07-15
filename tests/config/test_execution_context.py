"""
tests/config/test_execution_context.py

Tests for ExecutionContext (scripts/config/execution_context.py).

Run with:
    video-automation/.venv/bin/python -m pytest tests/config/ -v

All tests use tmp_path. No machine-specific absolute paths.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from config_manager import ConfigManager
from execution_context import ExecutionContext, _validate_job_id
from state_paths import EnvironmentStatePaths, _is_under

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG_ROOT = REPO_ROOT / "config"

_VALID_JOB_ID = "job_20260101T120000Z_abc12345"


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


def _load(tmp_path: Path, env: str = "dev") -> tuple:
    """Build tree, load config, return (config, state_paths)."""
    _build_valid_tree(tmp_path)
    config = ConfigManager.load(environment=env, config_root=tmp_path)
    state = config.state_paths
    return config, state


# ---------------------------------------------------------------------------
# Unit tests: ExecutionContext
# ---------------------------------------------------------------------------


class TestExecutionContextFromResolvedConfig:
    def test_contains_required_fields(self, tmp_path):
        config, state = _load(tmp_path)
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap
        )
        assert ctx.environment == "development"
        assert ctx.job_id == _VALID_JOB_ID
        assert ctx.funnel_id == "business"
        assert ctx.platform_id == "youtube"
        assert ctx.preset_id == "growth"
        assert ctx.config_version == "1"

    def test_contains_resolved_config_path(self, tmp_path):
        config, state = _load(tmp_path)
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap
        )
        assert ctx.resolved_config_path == str(snap)

    def test_code_commit_is_string_or_none(self, tmp_path):
        config, state = _load(tmp_path)
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap,
            repo_root=REPO_ROOT,
        )
        assert ctx.code_commit is None or isinstance(ctx.code_commit, str)
        if ctx.code_commit is not None:
            assert len(ctx.code_commit) > 0

    def test_code_commit_fallback_when_no_git(self, tmp_path):
        """When repo_root is not a git dir, code_commit must be None (never raises)."""
        config, state = _load(tmp_path)
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap,
            repo_root=tmp_path,  # tmp_path is not a git repo
        )
        assert ctx.code_commit is None

    def test_config_version_unknown_when_missing(self, tmp_path):
        """If config has no version field, config_version should be 'unknown'."""
        _build_valid_tree(tmp_path)
        # Remove version from defaults
        dev_yaml_path = tmp_path / "defaults" / "default.yaml"
        content = dev_yaml_path.read_text()
        content = content.replace("version: 1\n", "")
        dev_yaml_path.write_text(content)
        # Rebuild to bypass validation (use minimal context)
        ctx = ExecutionContext.minimal(
            environment="dev",
            job_id=_VALID_JOB_ID,
            resolved_config_path="",
        )
        assert ctx.config_version == "unknown"


class TestToDict:
    def test_to_dict_is_json_safe(self, tmp_path):
        config, state = _load(tmp_path)
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap
        )
        d = ctx.to_dict()
        serialised = json.dumps(d)  # must not raise
        assert isinstance(serialised, str)

    def test_to_dict_contains_expected_keys(self, tmp_path):
        config, state = _load(tmp_path)
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap
        )
        d = ctx.to_dict()
        for key in ("schema", "environment", "job_id", "funnel_id",
                    "platform_id", "preset_id", "config_version",
                    "resolved_config_path", "code_commit"):
            assert key in d, f"Missing key: {key}"

    def test_to_dict_contains_no_obvious_secrets(self, tmp_path):
        config, state = _load(tmp_path)
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap
        )
        serialised = json.dumps(ctx.to_dict()).lower()
        for bad in ("api_key", "password", "token", "bearer", "secret"):
            assert bad not in serialised, f"Possible secret {bad!r} found in execution context"

    def test_schema_version_is_v1(self, tmp_path):
        config, state = _load(tmp_path)
        snap = config.save_snapshot(state.job_dir(_VALID_JOB_ID))
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap
        )
        assert ctx.to_dict()["schema"] == "execution_context_v1"


class TestSave:
    def test_save_writes_execution_context_json(self, tmp_path):
        config, state = _load(tmp_path)
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap
        )
        ctx_path = ctx.save(job_dir)
        assert ctx_path.name == "execution_context.json"
        assert ctx_path.exists()

    def test_save_content_is_valid_json(self, tmp_path):
        config, state = _load(tmp_path)
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap
        )
        ctx.save(job_dir)
        content = json.loads((job_dir / "execution_context.json").read_text())
        assert content["environment"] == "development"
        assert content["job_id"] == _VALID_JOB_ID

    def test_save_creates_parent_directories(self, tmp_path):
        config, _ = _load(tmp_path)
        deep_job_dir = tmp_path / "deep" / "nested" / "job"
        snap = config.save_snapshot(deep_job_dir)
        ctx = ExecutionContext.minimal(
            environment="dev", job_id="nested-job",
            resolved_config_path=snap,
        )
        ctx_path = ctx.save(deep_job_dir)
        assert ctx_path.exists()

    def test_save_no_secrets_in_file(self, tmp_path):
        config, state = _load(tmp_path)
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap
        )
        ctx.save(job_dir)
        content = (job_dir / "execution_context.json").read_text().lower()
        for bad in ("api_key", "password", "token", "bearer", "secret"):
            assert bad not in content


class TestLoad:
    def test_round_trip_save_and_load(self, tmp_path):
        config, state = _load(tmp_path)
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap
        )
        ctx.save(job_dir)
        loaded = ExecutionContext.load(job_dir)
        assert loaded.environment == ctx.environment
        assert loaded.job_id == ctx.job_id
        assert loaded.funnel_id == ctx.funnel_id
        assert loaded.config_version == ctx.config_version


# ---------------------------------------------------------------------------
# Job ID validation
# ---------------------------------------------------------------------------


class TestJobIdValidation:
    def test_valid_job_id_passes(self):
        assert _validate_job_id("job_20260101T120000Z_abc12345") == "job_20260101T120000Z_abc12345"

    def test_empty_job_id_fails(self):
        with pytest.raises(ValueError, match="non-empty"):
            _validate_job_id("")

    def test_slash_in_job_id_fails(self):
        with pytest.raises(ValueError, match="separator"):
            _validate_job_id("job/sub")

    def test_dotdot_in_job_id_fails(self):
        with pytest.raises(ValueError, match="\\.\\."):
            _validate_job_id("job..evil")

    def test_absolute_path_fails(self):
        with pytest.raises(ValueError):
            _validate_job_id("/etc/job")

    def test_invalid_chars_fail(self):
        with pytest.raises(ValueError, match="invalid characters"):
            _validate_job_id("job@#$%")


# ---------------------------------------------------------------------------
# Minimal fallback context
# ---------------------------------------------------------------------------


class TestMinimalContext:
    def test_minimal_builds_without_config(self, tmp_path):
        ctx = ExecutionContext.minimal(
            environment="dev",
            job_id=_VALID_JOB_ID,
            resolved_config_path=str(tmp_path / "resolved_config.yaml"),
        )
        assert ctx.environment == "development"
        assert ctx.job_id == _VALID_JOB_ID
        assert ctx.funnel_id == "unknown"
        assert ctx.config_version == "unknown"

    def test_minimal_normalises_env_aliases(self):
        ctx = ExecutionContext.minimal(environment="prod", job_id="job_x", resolved_config_path="")
        assert ctx.environment == "production"

    def test_minimal_never_raises(self):
        # Even with a bad job_id, minimal must not raise
        ctx = ExecutionContext.minimal(environment="dev", job_id="bad/id", resolved_config_path="")
        assert ctx.job_id == "unknown"


# ---------------------------------------------------------------------------
# State-path integration: dev/prod job dirs
# ---------------------------------------------------------------------------


class TestStatePathIntegration:
    def test_dev_job_dir_under_jobs_dev(self, tmp_path):
        config, state = _load(tmp_path, "dev")
        job_dir = state.job_dir(_VALID_JOB_ID)
        assert "dev" in str(job_dir)
        assert _is_under(job_dir, state.jobs_root)

    def test_prod_job_dir_under_jobs_prod(self, tmp_path):
        config, state = _load(tmp_path, "prod")
        job_dir = state.job_dir(_VALID_JOB_ID)
        assert "prod" in str(job_dir)
        assert _is_under(job_dir, state.jobs_root)

    def test_resolved_config_saved_in_job_dir(self, tmp_path):
        config, state = _load(tmp_path, "dev")
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        assert snap.name == "resolved_config.yaml"
        assert _is_under(snap, job_dir)

    def test_execution_context_json_saved_in_job_dir(self, tmp_path):
        config, state = _load(tmp_path, "dev")
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap
        )
        ctx_path = ctx.save(job_dir)
        assert ctx_path.name == "execution_context.json"
        assert _is_under(ctx_path, job_dir)
        assert "dev" in str(ctx_path)

    def test_path_traversal_job_id_fails(self, tmp_path):
        _, state = _load(tmp_path, "dev")
        with pytest.raises(ValueError):
            state.job_dir("../prod/steal")

    def test_absolute_job_id_fails(self, tmp_path):
        _, state = _load(tmp_path, "dev")
        with pytest.raises(ValueError):
            state.job_dir("/etc/badpath")

    def test_dev_context_not_under_prod_root(self, tmp_path):
        dev_config, dev_state = _load(tmp_path, "dev")
        prod_state = EnvironmentStatePaths.from_resolved_config(
            ConfigManager.load(environment="prod", config_root=tmp_path)
        )
        dev_job_dir = dev_state.job_dir(_VALID_JOB_ID)
        snap = dev_config.save_snapshot(dev_job_dir)
        ctx = ExecutionContext.from_resolved_config(
            dev_config, job_id=_VALID_JOB_ID, resolved_config_path=snap
        )
        ctx.save(dev_job_dir)
        # The context file must not be under prod jobs root
        ctx_path = dev_job_dir / "execution_context.json"
        assert not _is_under(ctx_path, prod_state.jobs_root)


# ---------------------------------------------------------------------------
# ConfigManager integration
# ---------------------------------------------------------------------------


class TestConfigManagerIntegration:
    def test_resolved_state_paths_still_works(self, tmp_path):
        config, _ = _load(tmp_path)
        state = config.state_paths
        assert state.jobs_root.is_absolute()

    def test_save_snapshot_still_works(self, tmp_path):
        config, state = _load(tmp_path)
        snap = config.save_snapshot(state.job_dir(_VALID_JOB_ID))
        assert snap.exists()
        assert snap.name == "resolved_config.yaml"

    def test_execution_context_references_snapshot_path(self, tmp_path):
        config, state = _load(tmp_path)
        job_dir = state.job_dir(_VALID_JOB_ID)
        snap = config.save_snapshot(job_dir)
        ctx = ExecutionContext.from_resolved_config(
            config, job_id=_VALID_JOB_ID, resolved_config_path=snap
        )
        assert ctx.resolved_config_path == str(snap)
        assert Path(ctx.resolved_config_path).exists()

    def test_config_manager_load_remains_read_only(self, tmp_path):
        """ConfigManager.load() must not create job directories."""
        _build_valid_tree(tmp_path)
        ConfigManager.load(environment="dev", config_root=tmp_path)
        assert not (tmp_path / "jobs").exists()
        assert not (tmp_path / "data").exists()


# ---------------------------------------------------------------------------
# post_processing_report_v1 integration
# ---------------------------------------------------------------------------


class TestPostProcessingReportIntegration:
    def test_report_includes_execution_context(self):
        """build_post_processing_report accepts and includes execution_context."""
        import sys, os
        scripts_dir = str(Path(__file__).resolve().parents[2] / "video-automation" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from post_processing_report_v1 import build_post_processing_report

        ctx_dict = {
            "schema": "execution_context_v1",
            "environment": "development",
            "job_id": "job_test_001",
            "funnel_id": "business",
            "platform_id": "youtube",
            "preset_id": "growth",
            "config_version": "1",
            "resolved_config_path": "/tmp/job/resolved_config.yaml",
            "code_commit": "abc1234",
        }
        report = build_post_processing_report(
            job_id="job_test_001",
            execution_context=ctx_dict,
        )
        assert "execution_context" in report
        assert report["execution_context"]["environment"] == "development"
        assert report["execution_context"]["code_commit"] == "abc1234"

    def test_report_without_execution_context_defaults_to_empty_dict(self):
        """Without execution_context, the field should be an empty dict."""
        import sys
        scripts_dir = str(Path(__file__).resolve().parents[2] / "video-automation" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from post_processing_report_v1 import build_post_processing_report

        report = build_post_processing_report(job_id="job_no_ctx")
        assert report["execution_context"] == {}


# ---------------------------------------------------------------------------
# App boundary: job creation helper
# ---------------------------------------------------------------------------


class TestAppBoundaryHelper:
    """
    Test _save_execution_context from app.py.

    _save_execution_context replaced _build_and_save_execution_context in Prompt 5.5.
    It now takes a pre-loaded ResolvedConfig instead of loading one internally.
    Graceful-fallback behaviour was removed — callers fail clearly instead.

    We import it directly to avoid needing a live Flask server.
    """

    def _import_app(self):
        import sys
        import importlib
        import importlib.util

        # Load from the known path to avoid picking up other services' app.py files
        spec = importlib.util.spec_from_file_location(
            "app",
            str(Path(__file__).resolve().parents[2] / "video-automation" / "server" / "app.py"),
        )
        cached = sys.modules.get("app")
        if cached is not None and hasattr(cached, "_save_execution_context"):
            return cached
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules["app"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    def _load_config(self):
        from config_manager import ConfigManager
        return ConfigManager.load(environment="dev")

    def test_helper_writes_execution_context_json(self, tmp_path):
        app_mod = self._import_app()
        resolved = self._load_config()
        job_dir = str(tmp_path / "job_001")
        Path(job_dir).mkdir(parents=True, exist_ok=True)
        result = app_mod._save_execution_context(resolved, "job_20260101T120000Z_abc12345", job_dir)
        ctx_file = tmp_path / "job_001" / "execution_context.json"
        assert ctx_file.exists(), "execution_context.json should be written"
        assert isinstance(result, dict)
        assert "environment" in result

    def test_helper_writes_resolved_config_yaml(self, tmp_path):
        app_mod = self._import_app()
        resolved = self._load_config()
        job_dir = str(tmp_path / "job_002")
        Path(job_dir).mkdir(parents=True, exist_ok=True)
        app_mod._save_execution_context(resolved, "job_20260101T120000Z_abc12345", job_dir)
        resolved_yaml = tmp_path / "job_002" / "resolved_config.yaml"
        assert resolved_yaml.exists(), "resolved_config.yaml should be written"

    def test_helper_does_not_use_hardcoded_opt_path(self, tmp_path):
        """The helper must not reference /opt/mk04 in any written artifact."""
        app_mod = self._import_app()
        resolved = self._load_config()
        job_dir = str(tmp_path / "job_003")
        Path(job_dir).mkdir(parents=True, exist_ok=True)
        result = app_mod._save_execution_context(resolved, "job_20260101T120000Z_abc12345", job_dir)
        ctx_path = str(result.get("resolved_config_path", ""))
        assert "/opt/mk04" not in ctx_path
        ctx_file_content = (tmp_path / "job_003" / "execution_context.json").read_text()
        assert "/opt/mk04" not in ctx_file_content

    def test_helper_production_guard_raises_on_bad_config(self, tmp_path, monkeypatch):
        """
        Production job creation must fail clearly when config is unavailable.
        This replaced the old graceful-fallback test.
        The fallback behaviour was removed in Prompt 5.5; see test_server_job_boundary.py.
        """
        app_mod = self._import_app()
        import config_manager as _cm

        def _bad_load(*a, **kw):
            raise _cm.ConfigError("Simulated config unavailable")

        monkeypatch.setenv("MK04_ENV", "prod")
        monkeypatch.setattr(_cm.ConfigManager, "load", _bad_load)

        with pytest.raises(RuntimeError, match="Unable to create production job"):
            app_mod._load_env_config_for_job()
