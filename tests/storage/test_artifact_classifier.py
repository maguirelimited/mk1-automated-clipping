"""Tests for Storage Phase 3 — artifact classification (no retention/deletion)."""

from __future__ import annotations

import json
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from config_manager import ConfigManager
from storage.artifact_classifier import ArtifactClassifier, classify_artifact
from storage.artifact_types import ARTIFACT_TYPES

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


def _build_config_tree(config_root: Path) -> None:
    """Minimal valid config tree; paths resolve under config_root.parent."""
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
            """,
        )
    _write(
        config_root,
        "system/system.yaml",
        """
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
    config_root = tmp_path / "config"
    _build_config_tree(config_root)
    return tmp_path


def _classifier(repo: Path, environment: str = "dev") -> ArtifactClassifier:
    resolved = ConfigManager.load(
        environment=environment,
        funnel_id="business",
        platform_id="youtube",
        config_root=repo / "config",
    )
    return ArtifactClassifier(resolved, now=FIXED_NOW)


def _job_files(repo: Path, token: str, job_id: str, *, status: str, run_id: str) -> Path:
    job_dir = repo / "jobs" / token / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "report.json").write_text(
        json.dumps({"job_id": job_id, "status": status, "run_id": run_id}),
        encoding="utf-8",
    )
    (job_dir / "execution_context.json").write_text(
        json.dumps({"run_id": run_id, "environment": token}),
        encoding="utf-8",
    )
    return job_dir


# ---------------------------------------------------------------------------
# Known artifact types
# ---------------------------------------------------------------------------


class TestKnownArtifactTypes:
    def test_every_plan_type_classifies(self, repo: Path):
        clf = _classifier(repo)
        job_id = "job_known_001"
        job_dir = _job_files(repo, "dev", job_id, status="completed", run_id="run_1")

        cases: list[tuple[Path, str]] = [
            (_write_bytes(job_dir, "input_source.mp4"), "source_video"),
            (_write(job_dir, "transcript.json", "{}"), "transcript"),
            (_write(job_dir, "raw_candidate_pool.json", "{}"), "raw_candidate_pool"),
            (_write(job_dir, "processing_report.json", "{}"), "processing_report"),
            (
                _write(
                    job_dir,
                    "post_processing/selection/selection_result.json",
                    "{}",
                ),
                "selection_result",
            ),
            (
                _write_bytes(job_dir, "post_processing/tmp/work.mp4"),
                "intermediate_render",
            ),
            (
                _write_bytes(job_dir, "post_processing/clips/clip_formatted.mp4"),
                "formatted_clip",
            ),
            (
                _write_bytes(job_dir, "post_processing/clips/clip_captioned.mp4"),
                "captioned_clip",
            ),
            (_write_bytes(job_dir, "clips/final.mp4"), "final_clip"),
            (
                _write(
                    job_dir,
                    "post_processing/metadata/c1_metadata_writer_v1.json",
                    "{}",
                ),
                "clip_metadata",
            ),
            (
                _write(
                    job_dir,
                    "post_processing/reports/post_processing_report.json",
                    "{}",
                ),
                "post_processing_report",
            ),
            (_write(job_dir, "job.log", "log"), "job_log"),
            (_write(job_dir, "resolved_config.yaml", "x: 1"), "config_snapshot"),
            (_write(job_dir, "post_processing/tmp/scratch.txt", "t"), "temporary_file"),
        ]

        run_dir = repo / "runs" / "dev" / "run_1"
        cases.append((_write(run_dir, "run_record.json", "{}"), "run_record"))

        log_path = _write(repo, "logs/dev/video-automation.log", "line")
        cases.append((log_path, "service_log"))

        backup = _write_bytes(
            repo, "backups/dev/backup_dev_20260704T000000Z.tar.gz", b"bk"
        )
        cases.append((backup, "database_backup"))

        db = _write_bytes(repo, "database/dev.db", b"sqlite")
        cases.append((db, "database"))

        seen: set[str] = set()
        for path, expected in cases:
            record = clf.classify(path)
            assert record.artifact_type == expected, (
                f"{path}: expected {expected}, got {record.artifact_type} "
                f"(notes={record.notes})"
            )
            assert record.environment == "development"
            assert record.artifact_type in ARTIFACT_TYPES
            seen.add(expected)

        required = {
            "source_video",
            "transcript",
            "raw_candidate_pool",
            "processing_report",
            "selection_result",
            "intermediate_render",
            "formatted_clip",
            "captioned_clip",
            "final_clip",
            "clip_metadata",
            "post_processing_report",
            "run_record",
            "job_log",
            "service_log",
            "temporary_file",
            "database_backup",
            "config_snapshot",
        }
        assert required <= seen


# ---------------------------------------------------------------------------
# Unknown / invalid / boundaries
# ---------------------------------------------------------------------------


class TestUnknownAndBoundaries:
    def test_unknown_file_stays_unknown(self, repo: Path):
        clf = _classifier(repo)
        path = _write(repo, "jobs/dev/job_x/mystery.bin", "???")
        _job_files(repo, "dev", "job_x", status="completed", run_id="r")
        record = clf.classify(path)
        assert record.artifact_type == "unknown"
        assert "unknown" in record.protection_flags
        assert record.deletion_eligibility.eligible == "false"
        assert record.deletion_eligibility.reason == "unknown"

    def test_extension_alone_does_not_classify(self, repo: Path):
        clf = _classifier(repo)
        # .mp4 under reports root — not a known clip location.
        path = _write_bytes(repo, "reports/dev/random.mp4")
        record = clf.classify(path)
        assert record.artifact_type == "unknown"

    def test_missing_metadata_still_classifies_path(self, repo: Path):
        clf = _classifier(repo)
        # No report.json / execution_context — path rules still apply.
        path = _write(repo, "jobs/dev/job_orphan/transcript.json", "{}")
        record = clf.classify(path)
        assert record.artifact_type == "transcript"
        assert record.job_id == "job_orphan"
        assert record.run_id is None
        assert record.current_state == "unknown"

    def test_invalid_path_does_not_crash(self, repo: Path):
        clf = _classifier(repo)
        record = clf.classify("/nonexistent/absolute/path/foo.mp4")
        assert record.artifact_type == "unknown"
        assert "outside_environment_roots" in record.notes

    def test_dev_classifier_rejects_prod_path(self, repo: Path):
        dev_clf = _classifier(repo, "dev")
        prod_path = _write(repo, "jobs/prod/job_p/transcript.json", "{}")
        record = dev_clf.classify(prod_path)
        assert record.artifact_type == "unknown"
        assert record.environment == "development"
        assert "outside_environment_roots" in record.notes

    def test_prod_classifier_rejects_dev_path(self, repo: Path):
        prod_clf = _classifier(repo, "prod")
        dev_path = _write(repo, "jobs/dev/job_d/transcript.json", "{}")
        record = prod_clf.classify(dev_path)
        assert record.artifact_type == "unknown"
        assert record.environment == "production"
        assert "outside_environment_roots" in record.notes

    def test_environments_do_not_mix(self, repo: Path):
        _write(repo, "jobs/dev/job_d/transcript.json", "{}")
        _write(repo, "jobs/prod/job_p/transcript.json", "{}")
        dev = _classifier(repo, "dev").classify(repo / "jobs/dev/job_d/transcript.json")
        prod = _classifier(repo, "prod").classify(repo / "jobs/prod/job_p/transcript.json")
        assert dev.environment == "development"
        assert prod.environment == "production"
        assert dev.job_id == "job_d"
        assert prod.job_id == "job_p"


# ---------------------------------------------------------------------------
# Protection metadata
# ---------------------------------------------------------------------------


class TestProtectionMetadata:
    def test_active_job_protection(self, repo: Path):
        clf = _classifier(repo)
        job_id = "job_active"
        _job_files(repo, "dev", job_id, status="running", run_id="run_a")
        path = _write(repo, f"jobs/dev/{job_id}/transcript.json", "{}")
        record = clf.classify(path)
        assert "active_job" in record.protection_flags
        assert record.current_state == "running"
        assert record.deletion_eligibility.eligible == "false"
        assert record.deletion_eligibility.reason == "active_job"
        assert record.run_id == "run_a"

    def test_failed_job_metadata(self, repo: Path):
        clf = _classifier(repo)
        job_id = "job_failed"
        _job_files(repo, "dev", job_id, status="failed", run_id="run_f")
        path = _write(repo, f"jobs/dev/{job_id}/transcript.json", "{}")
        record = clf.classify(path)
        assert "failed_job" in record.protection_flags
        assert record.current_state == "failed"
        # Planner decides failed-job longevity — not this phase.
        assert record.deletion_eligibility.eligible == "unknown"
        assert record.deletion_eligibility.reason == "failed_job"

    def test_final_clip_protection(self, repo: Path):
        clf = _classifier(repo)
        job_id = "job_clip"
        _job_files(repo, "dev", job_id, status="completed", run_id="run_c")
        path = _write_bytes(repo, f"jobs/dev/{job_id}/clips/out.mp4")
        record = clf.classify(path)
        assert record.artifact_type == "final_clip"
        assert "final_clip" in record.protection_flags
        assert "protected_type" in record.protection_flags
        assert record.deletion_eligibility.eligible == "false"
        assert record.deletion_eligibility.reason == "final_clip"

    def test_database_protection(self, repo: Path):
        clf = _classifier(repo)
        path = _write_bytes(repo, "database/dev.db", b"db")
        record = clf.classify(path)
        assert record.artifact_type == "database"
        assert "database" in record.protection_flags
        assert "protected_type" in record.protection_flags
        assert record.deletion_eligibility.eligible == "false"
        assert record.deletion_eligibility.reason == "database"

    def test_completed_job_defers_to_planner(self, repo: Path):
        clf = _classifier(repo)
        job_id = "job_ok"
        _job_files(repo, "dev", job_id, status="completed", run_id="run_ok")
        path = _write(repo, f"jobs/dev/{job_id}/transcript.json", "{}")
        record = clf.classify(path)
        assert "active_job" not in record.protection_flags
        assert "failed_job" not in record.protection_flags
        assert record.deletion_eligibility.eligible == "unknown"
        assert record.deletion_eligibility.reason == "planner_not_implemented"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_classification_is_deterministic(self, repo: Path):
        clf = _classifier(repo)
        job_id = "job_det"
        _job_files(repo, "dev", job_id, status="completed", run_id="run_d")
        path = _write_bytes(repo, f"jobs/dev/{job_id}/clips/a.mp4")
        a = clf.classify(path).to_dict()
        b = clf.classify(path).to_dict()
        assert a == b

    def test_classify_artifact_helper(self, repo: Path):
        resolved = ConfigManager.load(
            environment="dev",
            funnel_id="business",
            platform_id="youtube",
            config_root=repo / "config",
        )
        path = _write(repo, "data/dev/control_state.json", "{}")
        record = classify_artifact(path, resolved=resolved, now=FIXED_NOW)
        assert record.artifact_type == "control_state"
        assert record.environment == "development"

    def test_selection_legacy_path(self, repo: Path):
        clf = _classifier(repo)
        job_id = "job_sel"
        _job_files(repo, "dev", job_id, status="completed", run_id="r")
        path = _write(repo, f"jobs/dev/{job_id}/selection.json", "{}")
        assert clf.classify(path).artifact_type == "selection_result"

    def test_global_outputs_clip(self, repo: Path):
        clf = _classifier(repo)
        path = _write_bytes(repo, "outputs/dev/clips/global.mp4")
        record = clf.classify(path)
        assert record.artifact_type == "final_clip"
        assert "final_clip" in record.protection_flags
