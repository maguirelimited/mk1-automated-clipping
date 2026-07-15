"""Tests for Storage Phase 6 — retention report schema and loaders."""

from __future__ import annotations

import json
import os
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from config_manager import ConfigManager
from storage.retention_apply import RetentionApplyExecutor, run_retention_apply
from storage.retention_planner import RetentionPlanner, run_retention_dry_run
from storage.retention_report import (
    LATEST_POINTER_NAME,
    RETENTION_REPORT_SCHEMA_VERSION,
    RetentionFileDecision,
    RetentionPlanReport,
    format_apply_terminal_summary,
    format_terminal_summary,
    load_latest_retention_report,
    load_plan_report,
    load_retention_report,
)

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


def _build_config_tree(config_root: Path, *, retention_enabled: bool = True) -> None:
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


def _job(repo: Path, token: str, job_id: str, *, status: str = "completed") -> Path:
    job_dir = repo / "jobs" / token / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "report.json").write_text(
        json.dumps({"job_id": job_id, "status": status}),
        encoding="utf-8",
    )
    return job_dir


class TestRetentionReports:
    def test_dry_run_report_schema(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_r")
        video = _write_bytes(job_dir, "input_source.mp4", b"x" * 50)
        _touch_age(video, days=10)
        mystery = _write_bytes(job_dir, "mystery.bin")
        _touch_age(mystery, days=10)

        report_dir = repo_enabled / "reports/dev/retention"
        report, path = run_retention_dry_run(
            _resolved(repo_enabled),
            now=FIXED_NOW,
            report_dir=report_dir,
        )
        payload = json.loads(path.read_text(encoding="utf-8"))

        assert payload["schema_version"] == RETENTION_REPORT_SCHEMA_VERSION
        assert payload["mode"] == "dry-run"
        assert payload["planner_version"]
        assert payload["policy_version"]
        assert payload["retention_run_id"] == report.retention_run_id
        assert payload["environment"] == "development"
        assert "started_at" in payload and "finished_at" in payload
        assert payload["duration_seconds"] >= 0
        assert payload["files_considered"] >= 2
        assert payload["files_eligible"] >= 1
        assert payload["files_deleted"] == 0
        assert payload["files_protected"] >= 0
        assert payload["files_unknown"] >= 1
        assert payload["files_skipped"] == 0
        assert payload["files_failed"] == 0
        assert payload["bytes_considered"] >= 50
        assert payload["bytes_reclaimable"] >= 50
        assert payload["bytes_reclaimed"] == 0
        assert "protection_summary" in payload
        assert "skip_summary" in payload
        assert "error_summary" in payload
        assert isinstance(payload["eligible_files"], list)
        assert isinstance(payload["protected_files"], list)
        assert isinstance(payload["unknown_files"], list)

    def test_apply_report_schema(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_a")
        target = _write_bytes(job_dir, "post_processing/tmp/old.txt", b"abc")
        _touch_age(target, days=5)
        plan = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        report_dir = repo_enabled / "reports/dev/retention"
        apply_report, path = run_retention_apply(
            _resolved(repo_enabled),
            plan,
            now=FIXED_NOW,
            report_dir=report_dir,
        )
        payload = json.loads(path.read_text(encoding="utf-8"))

        assert payload["schema_version"] == RETENTION_REPORT_SCHEMA_VERSION
        assert payload["mode"] == "apply"
        assert payload["source_plan_id"] == plan.retention_run_id
        assert payload["files_eligible"] == plan.eligible_count
        assert payload["files_deleted"] == apply_report.successful_deletions
        assert payload["files_skipped"] == apply_report.skipped_deletions
        assert payload["files_failed"] == apply_report.failed_deletions
        assert "duration_seconds" in payload
        assert "skip_summary" in payload
        assert "protection_summary" in payload
        assert "error_summary" in payload
        assert isinstance(payload["deletions"], list)
        assert not path.name.endswith(".json") or "_apply.json" in path.name

    def test_latest_pointer_updates(self, repo_enabled: Path):
        report_dir = repo_enabled / "reports/dev/retention"
        job_dir = _job(repo_enabled, "dev", "job_l")
        _write_bytes(job_dir, "post_processing/tmp/a.txt", b"a")

        _, path1 = run_retention_dry_run(
            _resolved(repo_enabled), now=FIXED_NOW, report_dir=report_dir
        )
        pointer = json.loads((report_dir / LATEST_POINTER_NAME).read_text(encoding="utf-8"))
        assert pointer["report_path"] == path1.name
        assert pointer["mode"] == "dry-run"

        plan = load_plan_report(path1)
        _, path2 = run_retention_apply(
            _resolved(repo_enabled), plan, now=FIXED_NOW, report_dir=report_dir
        )
        pointer2 = json.loads((report_dir / LATEST_POINTER_NAME).read_text(encoding="utf-8"))
        assert pointer2["report_path"] == path2.name
        assert pointer2["mode"] == "apply"
        # History preserved
        assert path1.is_file()
        assert path2.is_file()

    def test_report_loader_and_latest(self, repo_enabled: Path):
        report_dir = repo_enabled / "reports/dev/retention"
        job_dir = _job(repo_enabled, "dev", "job_load")
        video = _write_bytes(job_dir, "input_source.mp4", b"z" * 20)
        _touch_age(video, days=10)

        _, path = run_retention_dry_run(
            _resolved(repo_enabled), now=FIXED_NOW, report_dir=report_dir
        )
        loaded = load_retention_report(path)
        assert loaded["schema_version"] == RETENTION_REPORT_SCHEMA_VERSION
        assert loaded["files_eligible"] >= 1

        latest = load_latest_retention_report(report_dir)
        assert latest is not None
        assert latest["retention_run_id"] == loaded["retention_run_id"]

    def test_grouped_protection_and_skip_counts(self, repo_enabled: Path):
        ok = _job(repo_enabled, "dev", "job_ok", status="completed")
        fail = _job(repo_enabled, "dev", "job_fail", status="failed")
        active = _job(repo_enabled, "dev", "job_active", status="running")
        for job_dir, days in ((ok, 10), (fail, 10), (active, 10)):
            video = _write_bytes(job_dir, "input_source.mp4", b"v")
            _touch_age(video, days=days)
        clip = _write_bytes(ok, "clips/final.mp4", b"c")
        _touch_age(clip, days=400)
        mystery = _write_bytes(ok, "mystery.bin")
        _touch_age(mystery, days=10)

        report = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        payload = report.to_dict()
        protection = payload["protection_summary"]
        assert protection["protected_failed_jobs"] >= 1
        assert protection["protected_active_jobs"] >= 1
        assert protection["protected_final_clips"] >= 1
        assert protection["protected_unknown"] >= 1
        assert "active_job" in payload["skip_summary"] or "failed_job" in payload["skip_summary"]

    def test_duration_calculation(self, repo_enabled: Path):
        report = RetentionPlanReport(
            retention_run_id="retention_test",
            environment="development",
            mode="dry-run",
            policy_version="storage.retention.v1",
            retention_enabled=True,
            started_at="2026-07-04T12:00:00Z",
            finished_at="2026-07-04T12:00:05Z",
        )
        report.finalize_summaries()
        assert report.duration_seconds == 5.0
        assert report.to_dict()["duration_seconds"] == 5.0

    def test_report_roundtrip(self, repo_enabled: Path):
        report_dir = repo_enabled / "reports/dev/retention"
        job_dir = _job(repo_enabled, "dev", "job_rt")
        video = _write_bytes(job_dir, "input_source.mp4", b"x")
        _touch_age(video, days=10)
        _, path = run_retention_dry_run(
            _resolved(repo_enabled), now=FIXED_NOW, report_dir=report_dir
        )
        typed = load_plan_report(path)
        assert typed.mode == "dry-run"
        assert typed.eligible_count >= 1
        again = load_retention_report(path)
        assert again["files_eligible"] == typed.eligible_count

    def test_history_preservation(self, repo_enabled: Path):
        report_dir = repo_enabled / "reports/dev/retention"
        paths = []
        for i in range(2):
            job_dir = _job(repo_enabled, "dev", f"job_hist_{i}")
            video = _write_bytes(job_dir, "input_source.mp4", b"x")
            _touch_age(video, days=10)
            # Distinct run ids via different now stamps
            now = datetime(2026, 7, 4, 12, i, 0, tzinfo=UTC)
            _, path = run_retention_dry_run(
                _resolved(repo_enabled), now=now, report_dir=report_dir
            )
            paths.append(path)
        assert paths[0] != paths[1]
        assert paths[0].is_file()
        assert paths[1].is_file()

    def test_per_file_records_preserved(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_pf")
        video = _write_bytes(job_dir, "input_source.mp4", b"x" * 10)
        _touch_age(video, days=10)
        report = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        payload = report.to_dict()
        entry = payload["eligible_files"][0]
        for key in ("path", "artifact_type", "size_bytes", "planner_reason", "outcome"):
            assert key in entry

    def test_legacy_report_loads(self, tmp_path: Path):
        legacy = {
            "retention_run_id": "retention_legacy",
            "environment": "development",
            "mode": "dry-run",
            "policy_version": "storage.retention.v1",
            "retention_enabled": True,
            "started_at": "2026-07-04T12:00:00Z",
            "finished_at": "2026-07-04T12:00:01Z",
            "files_considered": 1,
            "eligible_count": 1,
            "protected_count": 0,
            "unknown_count": 0,
            "bytes_reclaimable": 10,
            "eligible_files": [
                {
                    "path": "/tmp/x",
                    "artifact_type": "source_video",
                    "disposition": "eligible",
                    "reason": "expired_source_video",
                    "size_bytes": 10,
                }
            ],
            "protected_files": [],
            "unknown_files": [],
            "deletion_reasons": {"expired_source_video": 1},
            "protection_reasons": {},
            "errors": [],
        }
        path = tmp_path / "legacy.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        loaded = load_retention_report(path)
        assert loaded["schema_version"] == RETENTION_REPORT_SCHEMA_VERSION
        assert loaded["files_eligible"] == 1
        assert loaded["bytes_considered"] == 10
        assert "protection_summary" in loaded
        assert "skip_summary" in loaded

    def test_terminal_summaries(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_term")
        video = _write_bytes(job_dir, "input_source.mp4", b"x")
        _touch_age(video, days=10)
        plan = RetentionPlanner(_resolved(repo_enabled), now=FIXED_NOW).plan_dry_run()
        text = format_terminal_summary(plan)
        assert "Retention Preview:" in text
        assert "No files deleted." in text

        apply_report = RetentionApplyExecutor(
            _resolved(repo_enabled), plan, now=FIXED_NOW
        ).execute()
        apply_text = format_apply_terminal_summary(apply_report)
        assert "Retention Apply Complete" in apply_text
        assert "Reclaimed:" in apply_text
