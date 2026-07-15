"""Runs and Jobs list/detail page tests (Phase 8)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.lists import (
    build_job_detail_context,
    build_jobs_list_context,
    build_run_detail_context,
    build_runs_list_context,
)


def _settings(tmp_path: Path, *, environment: str = "dev") -> Settings:
    return Settings(
        host="127.0.0.1",
        port=5070,
        data_dir=tmp_path,
        control_db_path=tmp_path / "ops.sqlite3",
        controls_file=tmp_path / "controls.json",
        service_timeout_sec=0.01,
        journal_lines=1,
        funnel_run_timeout_sec=1.0,
        stuck_running_sec=7200.0,
        stuck_queued_sec=1800.0,
        stuck_uploading_sec=1800.0,
        environment=environment,
        services=(
            ServiceConfig(
                key="source-input",
                label="source-input",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-source-input.service",
            ),
        ),
    )


_SHELL = {
    "shell_connected": True,
    "shell_env_token": "dev",
    "shell_environment_label": "DEVELOPMENT",
}


_RUNS = [
    {
        "run_id": "run_ok",
        "status": "SUCCESS",
        "trigger": "manual_cli",
        "funnel_id": "business",
        "started_at": "2026-07-04T00:00:00Z",
        "finished_at": "2026-07-04T00:01:00Z",
        "duration_seconds": 60,
    },
    {
        "run_id": "run_fail",
        "status": "FAIL",
        "trigger": "scheduled",
        "funnel_id": "business",
        "started_at": "2026-07-04T01:00:00Z",
        "finished_at": "2026-07-04T01:00:01Z",
        "duration_seconds": 1,
    },
    {
        "run_id": "run_skip",
        "status": "SKIPPED",
        "trigger": "scheduled",
        "funnel_id": "growth",
        "started_at": "2026-07-04T02:00:00Z",
        "finished_at": "2026-07-04T02:00:00Z",
        "duration_seconds": 0,
    },
]

_JOBS = [
    {
        "job_id": "job_ok",
        "state": "completed",
        "stage": "posting",
        "funnel": "business",
        "platform": "youtube",
        "preset": "growth",
        "runtime_seconds": 12,
        "run_id": "run_ok",
        "outputs": {"outputs_produced": 2, "clips_passed": 2},
    },
    {
        "job_id": "job_fail",
        "state": "failed",
        "stage": "captions",
        "funnel": "business",
        "platform": "tiktok",
        "preset": "growth",
        "runtime_seconds": 8,
        "run_id": "run_fail",
    },
    {
        "job_id": "job_run",
        "state": "running",
        "stage": "processing",
        "funnel": "growth",
        "platform": "youtube",
        "preset": "balanced",
        "runtime_seconds": 3,
        "run_id": "run_active",
    },
]


class TestRunsListContext:
    def test_filters_status_trigger_funnel(self, tmp_path: Path) -> None:
        with mock.patch(
            "ops_ui.lists.runs_list_payload",
            return_value={"runs": _RUNS},
        ):
            failed = build_runs_list_context(
                _settings(tmp_path), shell=_SHELL, status="failed"
            )
            scheduled = build_runs_list_context(
                _settings(tmp_path), shell=_SHELL, trigger="scheduled"
            )
            funnel = build_runs_list_context(
                _settings(tmp_path), shell=_SHELL, funnel="growth"
            )
        assert [r["run_id"] for r in failed["runs"]] == ["run_fail"]
        assert [r["run_id"] for r in scheduled["runs"]] == ["run_fail", "run_skip"]
        assert [r["run_id"] for r in funnel["runs"]] == ["run_skip"]

    def test_empty_runs(self, tmp_path: Path) -> None:
        with mock.patch("ops_ui.lists.runs_list_payload", return_value={"runs": []}):
            ctx = build_runs_list_context(_settings(tmp_path), shell=_SHELL)
        assert ctx["runs_empty"] is True


class TestJobsListContext:
    def test_filters_state_funnel_platform(self, tmp_path: Path) -> None:
        with mock.patch(
            "ops_ui.lists.jobs_list_payload",
            return_value={"jobs": _JOBS},
        ):
            failed = build_jobs_list_context(
                _settings(tmp_path), shell=_SHELL, state="failed"
            )
            funnel = build_jobs_list_context(
                _settings(tmp_path), shell=_SHELL, funnel="growth"
            )
            platform = build_jobs_list_context(
                _settings(tmp_path), shell=_SHELL, platform="youtube"
            )
        assert [j["job_id"] for j in failed["jobs"]] == ["job_fail"]
        assert [j["job_id"] for j in funnel["jobs"]] == ["job_run"]
        assert [j["job_id"] for j in platform["jobs"]] == ["job_ok", "job_run"]


class TestListRendering:
    def test_runs_page_renders_filters_and_links(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch(
            "ops_ui.lists.runs_list_payload",
            return_value={"runs": _RUNS},
        ):
            response = app.test_client().get("/ops/runs?status=failed")
        assert response.status_code == 200
        body = response.data
        assert b"Runs" in body
        assert b"Which pipeline runs happened" in body
        assert b"Operator Console" in body
        assert b"filter-bar" in body
        assert b"run_fail" in body
        assert b"/ops/runs/run_fail" in body
        assert b"run_ok" not in body

    def test_jobs_page_renders_filters_and_links(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch(
            "ops_ui.lists.jobs_list_payload",
            return_value={"jobs": _JOBS},
        ):
            response = app.test_client().get("/ops/jobs?state=completed")
        assert response.status_code == 200
        body = response.data
        assert b"Jobs" in body
        assert b"Which work units exist" in body
        assert b"Operator Console" in body
        assert b"job_ok" in body
        assert b"/ops/jobs/job_ok" in body
        assert b"job_fail" not in body

    def test_run_detail_page(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        summary = mock.Mock()
        summary.run_id = "run_fail"
        summary.to_dict.return_value = {
            **_RUNS[1],
            "environment": "dev",
            "jobs_started": 1,
            "jobs_completed": 0,
            "jobs_failed": 1,
            "log_path": "runs/dev/run_fail/run.log",
            "failure_summary": {"reason": "boot not ready"},
        }
        with mock.patch("ops_ui.lists.get_run_summary", return_value=summary):
            with mock.patch(
                "ops_ui.lists.jobs_list_payload",
                return_value={"jobs": _JOBS},
            ):
                response = app.test_client().get("/ops/runs/run_fail")
        assert response.status_code == 200
        body = response.data
        assert b"Run " in body
        assert b"run_fail" in body
        assert b"What happened during this run" in body
        assert b"boot not ready" in body
        assert b"Related jobs" in body
        assert b"job_fail" in body
        assert b"Next steps" in body
        assert b"Inspect failures" in body
        assert b"Output shortcuts" in body

    def test_run_detail_without_related_jobs(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        summary = mock.Mock()
        summary.run_id = "run_orphan"
        summary.to_dict.return_value = {
            "run_id": "run_orphan",
            "status": "SUCCESS",
            "trigger": "manual_cli",
            "jobs_started": 0,
            "jobs_completed": 0,
            "jobs_failed": 0,
        }
        with mock.patch("ops_ui.lists.get_run_summary", return_value=summary):
            with mock.patch(
                "ops_ui.lists.jobs_list_payload",
                return_value={"jobs": _JOBS},
            ):
                response = app.test_client().get("/ops/runs/run_orphan")
        assert response.status_code == 200
        assert b"No jobs are linked to this run" in response.data
        assert b"Browse jobs" in response.data

    def test_jobs_filter_by_run_id(self, tmp_path: Path) -> None:
        with mock.patch(
            "ops_ui.lists.jobs_list_payload",
            return_value={"jobs": _JOBS},
        ):
            ctx = build_jobs_list_context(
                _settings(tmp_path), shell=_SHELL, run_id="run_ok"
            )
        assert [j["job_id"] for j in ctx["jobs"]] == ["job_ok"]
        assert ctx["jobs_filter_run_id"] == "run_ok"

    def test_job_detail_page(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        detail = mock.Mock()
        detail.job_id = "job_fail"
        detail.summary = mock.Mock(run_id="run_fail")
        detail.to_dict.return_value = {
            "job_id": "job_fail",
            "summary": {
                **_JOBS[1],
                "run_id": "run_fail",
                "environment": "dev",
                "failure_summary": {"reason": "captions failed"},
            },
            "stage_timeline": [
                {"stage": "captions", "result": "failed", "detail": "Not available"}
            ],
            "artifacts": [
                {
                    "artifact_type": "transcript",
                    "exists": False,
                    "path": "jobs/dev/job_fail/transcript.json",
                }
            ],
            "reports": [],
            "logs": [{"source": "job", "path": None}],
            "warnings": [],
            "failures": [
                {
                    "component": "intelligent_captions_v1",
                    "reason": "captions failed",
                    "stage": "captions",
                    "suggested_next_inspection_target": "post_processing_report.json",
                }
            ],
            "clips": [],
            "created_at": "2026-07-04T00:00:00Z",
            "started_at": None,
            "finished_at": None,
            "trigger": "manual_cli",
            "report_summaries": [
                {
                    "report_type": "processing_report",
                    "available": False,
                    "path": "jobs/dev/job_fail/processing_report.json",
                    "metrics": {},
                    "detail": "Not available",
                }
            ],
            "output_summary": {
                "outputs_produced": 0,
                "validation_state": "failed",
                "posting_state": "Not available",
                "clips_passed": 0,
                "clips_failed": 1,
            },
        }
        with mock.patch("ops_ui.lists.get_job_detail", return_value=detail):
            response = app.test_client().get("/ops/jobs/job_fail")
        assert response.status_code == 200
        assert b"Job Inspector" in response.data
        assert b"job_fail" in response.data
        assert b"Operator Console" in response.data
        assert b"run_fail" in response.data
        assert b"Failures" in response.data
        assert b"captions failed" in response.data
        assert b"Artifacts" in response.data
        assert b"Pipeline timeline" in response.data
        assert b"No failures recorded." not in response.data
        assert b"Not available" in response.data

    def test_job_detail_shows_reframe_summary(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        detail = mock.Mock()
        detail.job_id = "job_ok"
        detail.summary = mock.Mock(run_id="run_ok")
        detail.to_dict.return_value = {
            "job_id": "job_ok",
            "summary": {**_JOBS[0], "run_id": "run_ok", "environment": "dev"},
            "stage_timeline": [],
            "artifacts": [],
            "reports": [],
            "logs": [],
            "warnings": [],
            "failures": [],
            "clips": [
                {
                    "clip_id": "clip_ft",
                    "output_path": "jobs/dev/job_ok/clips/clip_ft.mp4",
                    "reframe_summary": {
                        "available": True,
                        "reframe_mode": "auto",
                        "format_strategy": "face_track_crop",
                        "face_track_used": True,
                        "face_track_eligible": True,
                        "face_track_eligibility_reason": "eligible",
                        "module_status": "PASS",
                    },
                },
                {
                    "clip_id": "clip_blur",
                    "output_path": "jobs/dev/job_ok/clips/clip_blur.mp4",
                    "reframe_summary": {
                        "available": True,
                        "reframe_mode": "auto",
                        "format_strategy": "blurred_background_fit_foreground",
                        "face_track_attempted": True,
                        "face_track_used": False,
                        "face_track_eligibility_reason": "long_no_face_gap",
                        "module_status": "PASS",
                    },
                },
            ],
            "created_at": "2026-07-04T00:00:00Z",
            "started_at": None,
            "finished_at": None,
            "trigger": "manual_cli",
            "report_summaries": [],
            "output_summary": {
                "outputs_produced": 2,
                "validation_state": "passed",
                "posting_state": "unknown",
                "clips_passed": 2,
                "clips_failed": 0,
            },
        }
        with mock.patch("ops_ui.lists.get_job_detail", return_value=detail):
            response = app.test_client().get("/ops/jobs/job_ok")
        assert response.status_code == 200
        assert b"/ops/outputs?run_id=run_ok" in response.data
        body = response.data
        assert b"1 face-track" in body
        assert b"1 blur fallback" in body
        assert b"Face-track" in body
        assert b"long_no_face_gap" in body

    def test_missing_run_redirects(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.lists.get_run_summary", return_value=None):
            response = app.test_client().get("/ops/runs/missing")
        assert response.status_code in {302, 301}
        assert "/ops/runs" in (response.headers.get("Location") or "")

    def test_overview_still_uses_partials(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        response = app.test_client().get("/ops")
        assert response.status_code == 200
        assert b"Operator Console" in response.data
        assert b"Overall health" in response.data
        assert b"Automation" in response.data
