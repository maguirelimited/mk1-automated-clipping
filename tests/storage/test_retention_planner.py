"""Tests for Storage Phase 4 — retention dry-run planner."""

from __future__ import annotations

import json
import os
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from config_manager import ConfigManager
from storage.retention_planner import RetentionPlanner, run_retention_dry_run
from storage.retention_report import format_terminal_summary

FIXED_NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


def _write(root: Path, rel: str, content: str = "x") -> Path:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(textwrap.dedent(content) if content != "x" else content)
    return dest


def _write_bytes(root: Path, rel: str, data: bytes = b"data") -> Path:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def _touch_age(path: Path, *, days: float, now: datetime = FIXED_NOW) -> None:
    ts = now.timestamp() - days * 86400
    os.utime(path, (ts, ts))


def _build_config_tree(config_root: Path, *, retention_enabled: bool = False) -> None:
    enabled = "true" if retention_enabled else "false"
    _write(
        config_root,
        "defaults/default.yaml",
        """
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
        """,
    )
    for env_name, token, uploading, secrets in (
        ("development", "dev", False, False),
        ("production", "prod", True, True),
    ):
        filename = "dev.yaml" if token == "dev" else "prod.yaml"
        _write(
            config_root,
            f"environments/{filename}",
            f"""
            environment:
              name: {env_name}
            paths:
              data_root: data/{token}
              jobs_root: jobs/{token}
              outputs_root: outputs/{token}
              logs_root: logs/{token}
              reports_root: reports/{token}
              database_path: database/{token}.db
            uploading:
              enabled: {str(uploading).lower()}
            runtime:
              require_production_secrets: {str(secrets).lower()}
            storage:
              retention:
                enabled: {enabled}
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
            """,
        )
    _write(
        config_root,
        "system/system.yaml",
        f"""
        system:
          max_concurrent_jobs: 1
          retry_count: 0
          health_check_interval_seconds: 60
        ai:
          processing_model: placeholder-local-model
        storage:
          retention:
            enabled: {enabled}
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
        """,
    )
    _write(
        config_root,
        "funnels/business.yaml",
        """
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
        """,
    )
    for pid in ("youtube", "tiktok", "instagram", "facebook", "x"):
        _write(
            config_root,
            f"platforms/{pid}.yaml",
            f"""
            platform:
              id: {pid}
              name: {pid}
              enabled: true
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
            """,
        )
    for pid in ("balanced", "growth", "maximum_quality"):
        _write(
            config_root,
            f"presets/{pid}.yaml",
            f"""
            preset:
              id: {pid}
              name: {pid}
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
            """,
        )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _build_config_tree(tmp_path / "config", retention_enabled=False)
    return tmp_path


@pytest.fixture
def repo_enabled(tmp_path: Path) -> Path:
    _build_config_tree(tmp_path / "config", retention_enabled=True)
    return tmp_path


def _resolved(repo: Path, environment: str = "dev"):
    return ConfigManager.load(
        environment=environment,
        funnel_id="business",
        platform_id="youtube",
        config_root=repo / "config",
    )


def _job(repo: Path, token: str, job_id: str, *, status: str) -> Path:
    job_dir = repo / "jobs" / token / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "report.json").write_text(
        json.dumps({"job_id": job_id, "status": status}),
        encoding="utf-8",
    )
    return job_dir


class TestRetentionDryRun:
    def test_expired_source_video_eligible_when_enabled(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_old", status="completed")
        video = _write_bytes(job_dir, "input_source.mp4", b"x" * 1000)
        _touch_age(video, days=10)

        report = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        eligible = [f for f in report.eligible_files if f.artifact_type == "source_video"]
        assert len(eligible) == 1
        assert eligible[0].reason == "expired_source_video"
        assert report.bytes_reclaimable == 1000

    def test_not_expired_source_protected(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_new", status="completed")
        video = _write_bytes(job_dir, "input_source.mp4")
        _touch_age(video, days=2)

        report = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        protected = [f for f in report.protected_files if f.path.endswith("input_source.mp4")]
        assert len(protected) == 1
        assert protected[0].reason == "not_expired"

    def test_retention_disabled_protects_expired(self, repo: Path):
        job_dir = _job(repo, "dev", "job_old", status="completed")
        video = _write_bytes(job_dir, "input_source.mp4")
        _touch_age(video, days=30)

        report = RetentionPlanner(_resolved(repo), now=FIXED_NOW).plan_dry_run()
        assert report.retention_enabled is False
        reasons = {f.reason for f in report.protected_files if "input_source" in f.path}
        assert "retention_policy_disabled" in reasons
        assert report.eligible_count == 0

    def test_unknown_artifact_bucket(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_x", status="completed")
        mystery = _write_bytes(job_dir, "mystery.bin")
        _touch_age(mystery, days=30)

        report = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        assert len(report.unknown_files) == 1
        assert report.unknown_files[0].reason == "unknown_artifact_type"

    def test_active_job_protected(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_run", status="running")
        video = _write_bytes(job_dir, "input_source.mp4")
        _touch_age(video, days=30)

        report = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        protected = [f for f in report.protected_files if "input_source" in f.path]
        assert protected[0].reason == "active_job"

    def test_failed_job_kept_longer(self, repo_enabled: Path):
        ok_dir = _job(repo_enabled, "dev", "job_ok", status="completed")
        fail_dir = _job(repo_enabled, "dev", "job_fail", status="failed")
        ok_vid = _write_bytes(ok_dir, "input_source.mp4")
        fail_vid = _write_bytes(fail_dir, "input_source.mp4")
        _touch_age(ok_vid, days=10)
        _touch_age(fail_vid, days=10)

        report = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        by_path = {f.path: f for f in report.eligible_files + report.protected_files}
        assert by_path[str(ok_vid.resolve())].disposition == "eligible"
        assert by_path[str(fail_vid.resolve())].disposition == "protected"
        assert by_path[str(fail_vid.resolve())].reason == "failed_job"

    def test_prod_final_clip_default_protected(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "prod", "job_clip", status="completed")
        clip = _write_bytes(job_dir, "clips/final.mp4", b"clip")
        _touch_age(clip, days=400)

        report = RetentionPlanner(_resolved(repo_enabled, "prod"), now=FIXED_NOW).plan_dry_run()
        finals = [f for f in report.protected_files if f.artifact_type == "final_clip"]
        assert len(finals) == 1
        assert finals[0].reason == "final_clip_default_protected"

    def test_outside_allowed_root_protected(self, repo_enabled: Path):
        run_dir = repo_enabled / "runs" / "dev" / "run_1"
        record = _write(run_dir, "run_record.json", "{}")
        _touch_age(record, days=400)

        report = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        protected = [f for f in report.protected_files if f.artifact_type == "run_record"]
        assert protected[0].reason == "outside_allowed_root"

    def test_database_always_protected(self, repo_enabled: Path):
        db = _write_bytes(repo_enabled, "database/dev.db", b"db")
        _touch_age(db, days=400)

        report = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        db_rows = [f for f in report.protected_files if f.artifact_type == "database"]
        assert db_rows[0].reason == "protected_type"

    def test_writes_json_report(self, repo_enabled: Path):
        report, path = run_retention_dry_run(
            _resolved(repo_enabled),
            now=FIXED_NOW,
            report_dir=repo_enabled / "reports" / "dev" / "retention",
        )
        assert path.is_file()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["mode"] == "dry-run"
        assert payload["retention_run_id"] == report.retention_run_id
        assert "policy_version" in payload
        assert payload["files_considered"] >= 0

    def test_terminal_summary_readable(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_t", status="completed")
        tmp = _write_bytes(job_dir, "post_processing/tmp/scratch.txt")
        _touch_age(tmp, days=5)
        report = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        text = format_terminal_summary(report)
        assert "Retention Preview:" in text
        assert "No files deleted." in text
        assert "Estimated reclaimable space:" in text

    def test_deterministic_plan(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_d", status="completed")
        log = _write(job_dir, "job.log", "log")
        _touch_age(log, days=40)
        planner = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW)
        a = planner.plan_dry_run().to_dict()
        b = planner.plan_dry_run().to_dict()
        a.pop("finished_at", None)
        b.pop("finished_at", None)
        assert a == b

    def test_dry_run_does_not_delete_files(self, repo_enabled: Path, monkeypatch):
        job_dir = _job(repo_enabled, "dev", "job_del", status="completed")
        video = _write_bytes(job_dir, "input_source.mp4", b"keep-me")
        _touch_age(video, days=30)

        def _fail_unlink(*_args, **_kwargs):
            raise AssertionError("planner must not delete files")

        monkeypatch.setattr(Path, "unlink", _fail_unlink, raising=False)
        report = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        assert video.read_bytes() == b"keep-me"
        assert report.eligible_count >= 1

    def test_expired_temp_file_reason(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_tmp", status="completed")
        tmp = _write_bytes(job_dir, "post_processing/tmp/old.txt")
        _touch_age(tmp, days=5)
        report = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        eligible = [f for f in report.eligible_files if f.artifact_type == "temporary_file"]
        assert eligible[0].reason == "expired_temp_file"

    def test_dev_prod_environment_in_report(self, repo_enabled: Path):
        dev_report = RetentionPlanner(_resolved(repo_enabled, "dev"), now=FIXED_NOW).plan_dry_run()
        prod_report = RetentionPlanner(
            _resolved(repo_enabled, "prod"), now=FIXED_NOW
        ).plan_dry_run()
        assert dev_report.environment == "development"
        assert prod_report.environment == "production"
