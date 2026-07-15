"""Tests for Storage Phase 5 — safe retention apply."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from config_manager import ConfigManager
from storage.retention_apply import RetentionApplyExecutor, run_retention_apply
from storage.retention_report import (
    RetentionFileDecision,
    RetentionPlanReport,
    load_plan_report,
)

FIXED_NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]
RETENTION_CLI = REPO_ROOT / "scripts" / "retention.py"
PYTHON = REPO_ROOT / "video-automation" / ".venv" / "bin" / "python"


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


def _plan(
    eligible: list[RetentionFileDecision],
    *,
    environment: str = "development",
) -> RetentionPlanReport:
    return RetentionPlanReport(
        retention_run_id="retention_test_plan",
        environment=environment,
        mode="dry-run",
        policy_version="storage.retention.v1",
        retention_enabled=True,
        started_at="2026-07-04T12:00:00Z",
        finished_at="2026-07-04T12:00:00Z",
        eligible_files=eligible,
    )


def _eligible(path: Path, artifact_type: str, **kwargs) -> RetentionFileDecision:
    # Preserve symlink path identity — do not resolve before apply safety checks.
    stored_path = str(path)
    size = kwargs.get("size_bytes")
    if size is None and path.exists():
        try:
            size = path.lstat().st_size
        except OSError:
            size = None
    return RetentionFileDecision(
        path=stored_path,
        artifact_type=artifact_type,
        disposition="eligible",
        reason=f"expired_{artifact_type}",
        size_bytes=size,
        job_id=kwargs.get("job_id"),
        current_state=kwargs.get("current_state", "completed"),
    )


class TestRetentionApply:
    def test_successful_deletion(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_del")
        target = _write_bytes(job_dir, "post_processing/tmp/old.txt", b"remove-me")
        plan = _plan([_eligible(target, "temporary_file", job_id="job_del")])
        report = RetentionApplyExecutor(
            _resolved(repo_enabled), plan, now=FIXED_NOW
        ).execute()
        assert not target.exists()
        assert report.successful_deletions == 1
        assert report.bytes_reclaimed == len(b"remove-me")
        assert report.deletions[0].outcome == "DELETED"

    def test_planner_mismatch_skipped(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_mm")
        target = _write(job_dir, "transcript.json", "{}")
        # Plan claims source_video but file is transcript.
        bad = _eligible(target, "source_video", job_id="job_mm")
        report = RetentionApplyExecutor(
            _resolved(repo_enabled), _plan([bad]), now=FIXED_NOW
        ).execute()
        assert target.exists()
        assert report.skipped_deletions == 1
        assert report.deletions[0].skip_reason == "planner_mismatch"

    def test_outside_allowed_root_skipped(self, repo_enabled: Path):
        run_file = _write(repo_enabled, "runs/dev/run_1/run_record.json", "{}")
        decision = _eligible(run_file, "run_record")
        report = RetentionApplyExecutor(
            _resolved(repo_enabled), _plan([decision]), now=FIXED_NOW
        ).execute()
        assert run_file.exists()
        assert report.deletions[0].skip_reason == "outside_allowed_root"

    def test_symlink_rejected(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_sym")
        real = _write_bytes(job_dir, "post_processing/tmp/real.txt", b"x")
        link = job_dir / "post_processing/tmp/link.txt"
        link.symlink_to(real)
        report = RetentionApplyExecutor(
            _resolved(repo_enabled),
            _plan([_eligible(link, "temporary_file", job_id="job_sym")]),
            now=FIXED_NOW,
        ).execute()
        assert real.exists()
        assert report.deletions[0].skip_reason == "symlink_detected"

    def test_database_never_deleted(self, repo_enabled: Path):
        db = _write_bytes(repo_enabled, "database/dev.db", b"sqlite")
        report = RetentionApplyExecutor(
            _resolved(repo_enabled),
            _plan([_eligible(db, "database")]),
            now=FIXED_NOW,
        ).execute()
        assert db.exists()
        assert report.deletions[0].skip_reason == "protected_type"

    def test_final_clip_production_protected(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "prod", "job_clip", status="completed")
        clip = _write_bytes(job_dir, "clips/final.mp4", b"clip")
        report = RetentionApplyExecutor(
            _resolved(repo_enabled, "prod"),
            _plan([_eligible(clip, "final_clip", job_id="job_clip")], environment="production"),
            now=FIXED_NOW,
        ).execute()
        assert clip.exists()
        assert report.deletions[0].skip_reason == "final_clip_default_protected"

    def test_active_job_becomes_protected(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_active", status="running")
        target = _write_bytes(job_dir, "post_processing/tmp/x.txt", b"x")
        report = RetentionApplyExecutor(
            _resolved(repo_enabled),
            _plan([_eligible(target, "temporary_file", job_id="job_active", current_state="completed")]),
            now=FIXED_NOW,
        ).execute()
        assert target.exists()
        assert report.deletions[0].skip_reason == "active_job"

    def test_missing_file_skipped(self, repo_enabled: Path):
        missing = repo_enabled / "jobs/dev/job_x/post_processing/tmp/gone.txt"
        report = RetentionApplyExecutor(
            _resolved(repo_enabled),
            _plan([_eligible(missing, "temporary_file", job_id="job_x")]),
            now=FIXED_NOW,
        ).execute()
        assert report.deletions[0].skip_reason == "file_not_found"

    def test_partial_failure_continues(self, repo_enabled: Path, monkeypatch):
        job_dir = _job(repo_enabled, "dev", "job_partial")
        ok = _write_bytes(job_dir, "post_processing/tmp/ok.txt", b"ok")
        bad = _write_bytes(job_dir, "post_processing/tmp/bad.txt", b"bad")
        real_unlink = Path.unlink

        def _unlink(self, missing_ok: bool = False) -> None:  # noqa: ANN001
            if self.name == "bad.txt":
                raise PermissionError("permission denied")
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", _unlink)

        plan = _plan(
            [
                _eligible(ok, "temporary_file", job_id="job_partial"),
                _eligible(bad, "temporary_file", job_id="job_partial"),
            ]
        )
        report = RetentionApplyExecutor(
            _resolved(repo_enabled), plan, now=FIXED_NOW
        ).execute()

        assert not ok.exists()
        assert bad.exists()
        assert report.successful_deletions == 1
        assert report.failed_deletions == 1
        outcomes = {d.original_path: d.outcome for d in report.deletions}
        assert outcomes[str(ok)] == "DELETED"
        assert outcomes[str(bad)] == "FAILED"

    def test_apply_writes_separate_report(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_r")
        target = _write_bytes(job_dir, "post_processing/tmp/t.txt", b"t")
        plan = _plan([_eligible(target, "temporary_file", job_id="job_r")])
        report, path = run_retention_apply(
            _resolved(repo_enabled),
            plan,
            now=FIXED_NOW,
            report_dir=repo_enabled / "reports/dev/retention",
        )
        assert path.name.endswith("_apply.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["mode"] == "apply"
        assert payload["source_plan_id"] == plan.retention_run_id
        assert report.successful_deletions == 1

    def test_load_plan_report_roundtrip(self, repo_enabled: Path, tmp_path: Path):
        plan = _plan([])
        dry_path = tmp_path / "dry.json"
        plan.write_json(dry_path)
        loaded = load_plan_report(dry_path)
        assert loaded.retention_run_id == plan.retention_run_id
        assert loaded.mode == "dry-run"

    def test_retention_disabled_refuses_apply(self, tmp_path: Path):
        _build_config_tree(tmp_path / "config", retention_enabled=False)
        resolved = _resolved(tmp_path, "dev")
        report = RetentionApplyExecutor(
            _resolved(tmp_path), _plan([]), now=FIXED_NOW
        ).execute()
        assert "storage.retention.enabled is false" in report.errors[0]
        assert report.successful_deletions == 0

    def test_deletion_logging_fields(self, repo_enabled: Path):
        job_dir = _job(repo_enabled, "dev", "job_log")
        target = _write_bytes(job_dir, "post_processing/tmp/logged.txt", b"zz")
        report = RetentionApplyExecutor(
            _resolved(repo_enabled),
            _plan([_eligible(target, "temporary_file", job_id="job_log")]),
            now=FIXED_NOW,
        ).execute()
        entry = report.deletions[0].to_dict()
        for key in (
            "timestamp",
            "environment",
            "artifact_type",
            "original_path",
            "resolved_path",
            "size_bytes",
            "planner_reason",
            "outcome",
        ):
            assert key in entry


class TestRetentionApplyCLI:
    def test_prod_apply_requires_confirm_production(self, repo_enabled: Path):
        result = subprocess.run(
            [
                str(PYTHON),
                str(RETENTION_CLI),
                "--apply",
                "prod",
                "--config-root",
                str(repo_enabled / "config"),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0
        assert "confirm-production" in result.stderr.lower()
        assert "No files deleted" in result.stderr

    def test_dev_apply_requires_confirm(self, repo_enabled: Path):
        result = subprocess.run(
            [
                str(PYTHON),
                str(RETENTION_CLI),
                "--apply",
                "dev",
                "--config-root",
                str(repo_enabled / "config"),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0
        assert "--confirm" in result.stderr

    def test_dev_apply_with_confirm_succeeds_empty_plan(self, repo_enabled: Path):
        result = subprocess.run(
            [
                str(PYTHON),
                str(RETENTION_CLI),
                "--apply",
                "dev",
                "--confirm",
                "--config-root",
                str(repo_enabled / "config"),
                "--report-dir",
                str(repo_enabled / "reports/dev/retention"),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        assert "Apply report written" in result.stdout
