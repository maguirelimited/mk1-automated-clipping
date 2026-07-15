"""Tests for failure aggregation (Phase 11)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from observability.failures import (  # noqa: E402
    failure_group_payload,
    failures_payload,
    list_failure_groups,
)
from observability.models import (  # noqa: E402
    FailureGroup,
    FailureSummary,
    JobSummary,
    RunSummary,
    ServiceStatus,
)


def _run(run_id: str, status: str, reason: str, trigger: str = "scheduled") -> RunSummary:
    return RunSummary(
        run_id=run_id,
        environment="dev",
        trigger=trigger,
        status=status,
        started_at="2026-07-04T00:00:00Z",
        finished_at="2026-07-04T00:01:00Z",
        failure_summary=FailureSummary(
            component="pipeline_run",
            reason=reason,
            severity="warn" if status == "SKIPPED" else "fail",
            suggested_next_inspection_target=f"runs/dev/{run_id}/run.log",
        ),
    )


def _job(job_id: str, stage: str, reason: str, module: str = "job") -> JobSummary:
    return JobSummary(
        job_id=job_id,
        state="failed",
        environment="dev",
        stage=stage,
        failure_summary=FailureSummary(
            component=module,
            reason=reason,
            severity="fail",
            stage=stage,
            timestamp="2026-07-04T00:02:00Z",
            suggested_next_inspection_target="Inspect post_processing_report.json",
        ),
    )


class TestFailureAggregation:
    def test_groups_repeated_failures(self):
        runs = [
            _run("run_1", "FAIL", "boot readiness NOT READY"),
            _run("run_2", "FAIL", "boot readiness NOT READY"),
            _run("run_3", "SKIPPED", "execution lock held"),
        ]
        jobs = [
            _job("job_a", "captions", "Missing transcript segment", "intelligent_captions_v1"),
            _job("job_b", "captions", "Missing transcript segment", "intelligent_captions_v1"),
        ]
        services = [
            ServiceStatus(service_name="worker", health="FAIL", detail="inactive"),
            ServiceStatus(service_name="api", health="PASS"),
        ]

        with mock.patch("observability.failures.list_run_summaries", return_value=runs):
            with mock.patch("observability.failures.list_job_summaries", return_value=jobs):
                with mock.patch(
                    "observability.failures.get_job_detail", return_value=None
                ):
                    with mock.patch(
                        "observability.failures.build_service_statuses",
                        return_value=services,
                    ):
                        with mock.patch(
                            "observability.failures.list_clip_summaries",
                            return_value=[],
                        ):
                            groups = list_failure_groups("dev")

        by_key = {g.group_key: g for g in groups}
        assert "reason:boot_readiness_not_ready" in by_key
        assert by_key["reason:boot_readiness_not_ready"].count == 2
        assert "pipeline_stage:captions" in by_key
        assert by_key["pipeline_stage:captions"].count == 2
        assert "service:worker" in by_key
        assert "job:job_a" in by_key
        assert "run:run_3" in by_key
        assert by_key["run:run_3"].severity == "WARN"

    def test_empty_failure_state(self):
        with mock.patch("observability.failures.list_run_summaries", return_value=[]):
            with mock.patch("observability.failures.list_job_summaries", return_value=[]):
                with mock.patch(
                    "observability.failures.build_service_statuses", return_value=[]
                ):
                    with mock.patch(
                        "observability.failures.list_clip_summaries", return_value=[]
                    ):
                        payload = failures_payload("dev")
        assert payload["total_failures"] == 0
        assert payload["groups"] == []
        assert payload["failed_jobs"] == 0

    def test_group_detail_payload(self):
        group = FailureGroup(
            group_key="job:job_a",
            category="Job",
            name="job_a",
            count=1,
            severity="ERROR",
            affected_jobs=["job_a"],
            representative_reason="failed",
            suggested_next_inspection_target="Open Job job_a",
        )
        with mock.patch(
            "observability.failures.list_failure_groups", return_value=[group]
        ):
            payload = failure_group_payload("dev", "job:job_a")
        assert payload is not None
        assert payload["group"]["name"] == "job_a"
        assert payload["related_jobs"][0]["path"] == "/ops/jobs/job_a"

    def test_unknown_group_returns_none(self):
        with mock.patch("observability.failures.list_failure_groups", return_value=[]):
            assert failure_group_payload("dev", "missing:group") is None
            assert failure_group_payload("dev", "../etc/passwd") is None
