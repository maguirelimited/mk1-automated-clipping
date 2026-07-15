"""Unit tests for observability contract models."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from observability import (  # noqa: E402
    ACTIVITY_STATES,
    ARTIFACT_TYPES,
    HEALTH_RESULTS,
    JOB_STATES,
    RUN_STATUSES,
    RUN_TRIGGERS,
    ArtifactReference,
    ClipSummary,
    ConfigSummary,
    FailureSummary,
    JobDetail,
    JobSummary,
    LogReference,
    RunSummary,
    ServiceStatus,
    SystemHealth,
    SystemStatus,
)
from observability.models import (  # noqa: E402
    ActiveRunRef,
    DiskState,
    ExecutionLockSummary,
    JobOutputs,
    QueueSummary,
    RecentActivitySummary,
    SchedulerStateSummary,
    StageTimelineEntry,
    UploadStateSummary,
)


REQUIRED_SCHEMAS = (
    ServiceStatus,
    SystemHealth,
    SystemStatus,
    RunSummary,
    JobSummary,
    JobDetail,
    ArtifactReference,
    ClipSummary,
    FailureSummary,
    ConfigSummary,
    LogReference,
)


class TestSchemaDefinitions:
    def test_all_required_schemas_are_importable(self):
        for schema in REQUIRED_SCHEMAS:
            assert schema is not None

    def test_vocabularies_are_non_empty(self):
        for vocab in (
            HEALTH_RESULTS,
            ACTIVITY_STATES,
            RUN_STATUSES,
            RUN_TRIGGERS,
            JOB_STATES,
            ARTIFACT_TYPES,
        ):
            assert len(vocab) >= 2


class TestRoundTrip:
    def test_service_status_round_trip(self):
        original = ServiceStatus(
            service_name="ai_service",
            state="active",
            health="PASS",
            last_checked_at="2026-07-04T00:00:00Z",
            restart_count=2,
            last_restart_at="2026-07-03T12:00:00Z",
            detail="http ready",
            unit_name="mk04-ai-service.service",
        )
        restored = ServiceStatus.from_dict(original.to_dict())
        assert restored == original

    def test_system_health_round_trip(self):
        original = SystemHealth(
            overall="PASS",
            environment="prod",
            disk=DiskState(status="PASS", usage_percent=68.0, detail="ok"),
            upload=UploadStateSummary(enabled=True, status="enabled"),
            scheduler=SchedulerStateSummary(effective="enabled", status="enabled"),
            services=[
                ServiceStatus(service_name="worker", state="active", health="PASS"),
            ],
            readiness_failures=[],
            execution_lock=ExecutionLockSummary(present=False),
            boot_readiness="READY",
            checked_at="2026-07-04T00:00:00Z",
        )
        restored = SystemHealth.from_dict(original.to_dict())
        assert restored.to_dict() == original.to_dict()

    def test_system_status_round_trip(self):
        original = SystemStatus(
            environment="prod",
            state="running",
            active_run=ActiveRunRef(
                run_id="run_20260704T000000Z_scheduled",
                trigger="scheduled",
                status="RUNNING",
            ),
            queue=QueueSummary(pending=1, running=1, failed=0),
            current_activity="pipeline run in progress",
            recent_summary=RecentActivitySummary(runs=1, jobs_completed=0, window="today"),
            checked_at="2026-07-04T00:00:00Z",
        )
        restored = SystemStatus.from_dict(original.to_dict())
        assert restored.to_dict() == original.to_dict()

    def test_run_summary_round_trip(self):
        original = RunSummary(
            run_id="run_1",
            environment="dev",
            trigger="manual_cli",
            status="FAIL",
            started_at="2026-07-04T00:00:00Z",
            finished_at="2026-07-04T00:01:00Z",
            duration_seconds=60.0,
            jobs_started=1,
            jobs_completed=0,
            jobs_failed=1,
            failure_summary=FailureSummary(
                component="pipeline_run",
                reason="boot not ready",
                severity="fail",
                suggested_next_inspection_target="runs/dev/run_1/run.log",
            ),
            log_path="runs/dev/run_1/run.log",
        )
        restored = RunSummary.from_dict(original.to_dict())
        assert restored.to_dict() == original.to_dict()

    def test_job_detail_round_trip_with_empty_optional_collections(self):
        summary = JobSummary(
            job_id="job_abc",
            state="failed",
            funnel="business",
            platform="youtube",
            preset="growth",
            stage="captions",
            runtime_seconds=12.5,
            outputs=JobOutputs(candidates_discovered=3, clips_passed=0, clips_failed=1),
        )
        original = JobDetail(job_id="job_abc", summary=summary)
        restored = JobDetail.from_dict(original.to_dict())
        assert restored.to_dict() == original.to_dict()
        assert restored.stage_timeline == []
        assert restored.artifacts == []
        assert restored.clips == []

    def test_job_detail_with_populated_references(self):
        summary = JobSummary(job_id="job_abc", state="completed")
        original = JobDetail(
            job_id="job_abc",
            summary=summary,
            stage_timeline=[
                StageTimelineEntry(stage="source", result="completed"),
                StageTimelineEntry(stage="transcript", result="completed"),
            ],
            artifacts=[
                ArtifactReference(
                    artifact_type="processing_report",
                    path="/jobs/job_abc/processing_report.json",
                    exists=True,
                    job_id="job_abc",
                    size_bytes=1024,
                )
            ],
            reports=[
                ArtifactReference.missing(
                    "post_processing_report",
                    job_id="job_abc",
                    path="/jobs/job_abc/post_processing_report.json",
                )
            ],
            logs=[LogReference(source="job", job_id="job_abc", path="/jobs/job_abc/job.log")],
            warnings=[
                FailureSummary(
                    component="selection",
                    reason="low confidence candidate",
                    severity="warn",
                    stage="selection",
                )
            ],
            failures=[],
            clips=[
                ClipSummary(
                    clip_id="clip_001",
                    job_id="job_abc",
                    source_candidate="cand_01",
                    validation_state="passed",
                    posting_state="pending",
                )
            ],
        )
        restored = JobDetail.from_dict(original.to_dict())
        assert restored.to_dict() == original.to_dict()


class TestMissingArtifacts:
    def test_missing_artifact_does_not_raise(self):
        ref = ArtifactReference.missing(
            "transcript",
            path="/jobs/job_x/transcript.json",
            environment="prod",
            job_id="job_x",
        )
        assert ref.exists is False
        assert ref.artifact_type == "transcript"
        assert ref.detail == "not found"
        payload = ref.to_dict()
        assert payload["exists"] is False
        restored = ArtifactReference.from_dict(payload)
        assert restored is not None
        assert restored.exists is False

    def test_from_dict_tolerates_partial_missing_fields(self):
        ref = ArtifactReference.from_dict({"artifact_type": "run_log"})
        assert ref is not None
        assert ref.exists is False
        assert ref.path is None
        assert ref.size_bytes is None


class TestConfigSummarySecrets:
    def test_to_dict_only_emits_allowlisted_fields(self):
        summary = ConfigSummary(
            environment="prod",
            active_preset="growth",
            funnel="business",
            platform="youtube",
            upload=UploadStateSummary(enabled=True, status="enabled"),
            scheduler=SchedulerStateSummary(effective="enabled", status="enabled"),
        )
        payload = summary.to_dict()
        assert set(payload.keys()) == {
            "environment",
            "active_preset",
            "funnel",
            "platform",
            "upload",
            "scheduler",
            "schema_version",
        }
        assert "password" not in payload
        assert "api_key" not in payload
        assert "token" not in payload

    def test_round_trip_preserves_operational_fields_only(self):
        original = ConfigSummary(
            environment="dev",
            active_preset="balanced",
            funnel="business",
            platform="tiktok",
        )
        restored = ConfigSummary.from_dict(original.to_dict())
        assert restored.to_dict() == original.to_dict()


class TestFailureAndLogReferences:
    def test_failure_summary_from_empty_dict_is_none(self):
        assert FailureSummary.from_dict({}) is None
        assert FailureSummary.from_dict(None) is None

    def test_log_reference_without_source_is_none(self):
        assert LogReference.from_dict({"path": "/var/log/x"}) is None

    def test_clip_summary_with_missing_metadata(self):
        clip = ClipSummary(
            clip_id="c1",
            job_id="j1",
            metadata_reference=ArtifactReference.missing(
                "clip_metadata", job_id="j1", path="/missing.json"
            ),
        )
        payload = clip.to_dict()
        assert payload["metadata_reference"]["exists"] is False
        restored = ClipSummary.from_dict(payload)
        assert restored.metadata_reference is not None
        assert restored.metadata_reference.exists is False
