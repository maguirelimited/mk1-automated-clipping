"""Operator workflow smoke tests (Prompt 12).

End-to-end client navigation through the modern /ops daily loop, diagnostics,
legacy boundaries, and safe controls — using mocked observability data only.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.overview import build_overview_context


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
            ServiceConfig(
                key="video-automation",
                label="video-automation",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-video-automation.service",
            ),
        ),
    )


def _health(**overrides):
    data = {
        "overall": "PASS",
        "environment": "dev",
        "boot_readiness": "READY",
        "disk": {"status": "PASS", "usage_percent": 42.0, "detail": "ok"},
        "upload": {"enabled": True, "status": "pass", "detail": "ok"},
        "scheduler": {"effective": "enabled", "status": "pass", "detail": "ok"},
        "services": [],
        "readiness_failures": [],
        "execution_lock": {"present": False, "stale": False},
    }
    data.update(overrides)
    return data


def _status(**overrides):
    data = {
        "environment": "dev",
        "state": "idle",
        "active_run": None,
        "current_activity": "idle",
        "queue": {"pending": 0, "running": 0, "failed": 0},
        "recent_summary": {"runs": 0},
    }
    data.update(overrides)
    return data


def _shell(**overrides):
    base = {
        "shell_connected": True,
        "shell_is_production": False,
        "shell_env_token": "dev",
        "shell_environment_label": "DEVELOPMENT",
        "shell_environment_css": "development",
        "shell_overall": "PASS",
        "shell_activity": "idle",
        "shell_upload": "disabled",
        "shell_scheduler": "manual",
        "shell_health_data": _health(),
        "shell_status_data": _status(),
    }
    base.update(overrides)
    return base


_MODERN_OPS_ROUTES = (
    "/ops",
    "/ops/runs",
    "/ops/jobs",
    "/ops/outputs",
    "/ops/failures",
    "/ops/storage",
    "/ops/configuration",
)

_LEGACY_ROUTES = (
    "/dashboard",
    "/failed",
    "/settings",
    "/recovery",
)

_DIAGNOSTIC_ROUTES = (
    "/health",
    "/logs",
)


class TestCleanIdleWorkflow:
    """Scenario 1 — clean idle operator console."""

    def test_console_idle_state(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        health = mock.Mock()
        health.to_dict.return_value = _health()
        status = mock.Mock()
        status.to_dict.return_value = _status()
        with mock.patch("ops_ui.shell.build_system_health", return_value=health):
            with mock.patch("ops_ui.shell.build_system_status", return_value=status):
                with mock.patch(
                    "ops_ui.overview.services_payload", return_value={"services": []}
                ):
                    with mock.patch(
                        "ops_ui.overview.runs_list_payload", return_value={"runs": []}
                    ):
                        body = app.test_client().get("/ops").data

        assert b"Operator Console" in body
        assert b"Overall health" in body
        assert b"Needs attention" in body
        assert b"Nothing needs attention right now." in body
        assert b"Current activity" in body
        assert b"Last run" in body
        assert b"Safe actions" in body
        assert b"Daily loop" in body
        # Not a metrics dashboard — no resource/recent-runs tables on console.
        assert b"Recent runs" not in body
        assert b"Resources" not in body
        # Primary nav starts with Console.
        assert body.index(b'href="/ops"') < body.index(b'href="/ops/outputs"')

    def test_daily_loop_links_on_console(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        body = app.test_client().get("/ops").data
        for href in (b"/ops/outputs", b"/ops/runs", b"/ops/jobs", b"/ops/failures"):
            assert href in body


class TestFailedLastRunWorkflow:
    """Scenario 2 — failed last run attention → run detail → console."""

    def test_attention_links_to_real_run_id(self, tmp_path: Path) -> None:
        with mock.patch("ops_ui.overview.services_payload", return_value={"services": []}):
            with mock.patch(
                "ops_ui.overview.runs_list_payload",
                return_value={
                    "runs": [
                        {
                            "run_id": "run_bad",
                            "status": "FAIL",
                            "failure_summary": {"reason": "worker down"},
                            "jobs_failed": 1,
                        }
                    ]
                },
            ):
                ctx = build_overview_context(_settings(tmp_path), shell=_shell())
        item = next(i for i in ctx["console_attention"] if i["title"] == "Last run failed")
        assert item["href"] == "/ops/runs/run_bad"

    def test_failed_run_without_id_does_not_invent_link(self, tmp_path: Path) -> None:
        with mock.patch("ops_ui.overview.services_payload", return_value={"services": []}):
            with mock.patch(
                "ops_ui.overview.runs_list_payload",
                return_value={"runs": [{"status": "FAIL", "failure_summary": {"reason": "x"}}]},
            ):
                ctx = build_overview_context(_settings(tmp_path), shell=_shell())
        titles = {i["title"] for i in ctx["console_attention"]}
        assert "Last run failed" not in titles

    def test_failed_run_workflow_pages(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        health = mock.Mock()
        health.to_dict.return_value = _health()
        status = mock.Mock()
        status.to_dict.return_value = _status()
        runs = {
            "runs": [
                {
                    "run_id": "run_bad",
                    "status": "FAIL",
                    "failure_summary": {"reason": "worker down"},
                    "jobs_failed": 1,
                    "jobs_completed": 0,
                }
            ]
        }
        summary = mock.Mock()
        summary.run_id = "run_bad"
        summary.to_dict.return_value = {
            "run_id": "run_bad",
            "status": "FAIL",
            "failure_summary": {"reason": "worker down"},
            "jobs_started": 1,
            "jobs_completed": 0,
            "jobs_failed": 1,
        }
        jobs = {
            "jobs": [
                {
                    "job_id": "job_fail",
                    "state": "failed",
                    "run_id": "run_bad",
                }
            ]
        }
        with mock.patch("ops_ui.shell.build_system_health", return_value=health):
            with mock.patch("ops_ui.shell.build_system_status", return_value=status):
                with mock.patch("ops_ui.overview.services_payload", return_value={"services": []}):
                    with mock.patch("ops_ui.overview.runs_list_payload", return_value=runs):
                        console = app.test_client().get("/ops").data
        assert b"Last run failed" in console
        assert b"/ops/runs/run_bad" in console

        with mock.patch("ops_ui.lists.get_run_summary", return_value=summary):
            with mock.patch("ops_ui.lists.jobs_list_payload", return_value=jobs):
                detail = app.test_client().get("/ops/runs/run_bad").data
        assert b"run_bad" in detail
        assert b"worker down" in detail
        assert b"Related jobs" in detail
        assert b"job_fail" in detail
        assert b"Inspect failures" in detail
        assert b"Operator Console" in detail


class TestFailedJobWorkflow:
    """Scenario 3 — failures triage → group → job inspector."""

    _GROUP = {
        "group_key": "pipeline_stage:captions",
        "category": "Pipeline Stage",
        "name": "captions",
        "count": 1,
        "severity": "ERROR",
        "latest_occurrence": "2026-07-04T00:02:00Z",
        "representative_reason": "Missing transcript segment",
        "suggested_next_inspection_target": "Inspect post_processing_report.json",
        "affected_jobs": ["job_a"],
        "affected_runs": [],
    }

    def test_failures_to_job_inspector(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        payload = {
            "total_failures": 1,
            "failed_jobs": 1,
            "failed_runs": 0,
            "distinct_groups": 1,
            "groups": [self._GROUP],
        }
        job_detail = mock.Mock()
        job_detail.job_id = "job_a"
        job_detail.summary = mock.Mock(run_id="run_1")
        job_detail.to_dict.return_value = {
            "job_id": "job_a",
            "summary": {"job_id": "job_a", "state": "failed", "run_id": "run_1"},
            "stage_timeline": [],
            "artifacts": [],
            "reports": [],
            "logs": [],
            "warnings": [],
            "failures": [],
            "clips": [],
            "report_summaries": [],
            "output_summary": {},
        }
        with mock.patch("ops_ui.failures_ui.failures_payload", return_value=payload):
            failures = app.test_client().get("/ops/failures").data
        assert b"What failed" in failures
        assert b"Open in Jobs" in failures
        assert b"/ops/jobs?state=failed" in failures
        main = failures.split(b'class="legacy-nav"', 1)[0]
        assert b"/failed" not in main

        with mock.patch(
            "ops_ui.failures_ui.failure_group_payload",
            return_value={
                "group": self._GROUP,
                "related_jobs": [{"job_id": "job_a", "path": "/ops/jobs/job_a"}],
                "related_runs": [],
            },
        ):
            group = app.test_client().get("/ops/failures/pipeline_stage:captions").data
        assert b"/ops/jobs/job_a" in group
        assert b"Operator Console" in group

        with mock.patch("ops_ui.lists.get_job_detail", return_value=job_detail):
            job = app.test_client().get("/ops/jobs/job_a").data
        assert b"Job Inspector" in job
        assert b"Operator Console" in job

    def test_legacy_failed_jobs_page_is_labelled(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.app._video_jobs", return_value=[]):
            with mock.patch("ops_ui.app._upload_jobs", return_value=[]):
                body = app.test_client().get("/failed").data
        assert b"Legacy failed jobs" in body
        assert b"Advanced / legacy" in body
        assert b"/ops/failures" in body


class TestOutputInspectionWorkflow:
    """Scenario 4 — outputs list → detail → job/run → console."""

    _CLIP = {
        "clip_id": "clip_a",
        "job_id": "job_1",
        "funnel": "business",
        "platform": "youtube",
        "validation_state": "passed",
        "posting_state": "unknown",
        "output_path": "jobs/dev/job_1/clips/clip_a.mp4",
        "preview_available": True,
        "exists": True,
        "created_at": "2026-07-04T12:00:00Z",
        "metadata_reference": {"artifact_type": "clip_metadata", "exists": False, "path": ""},
    }

    def test_output_inspection_path(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        job_detail = SimpleNamespace(
            summary=SimpleNamespace(run_id="run_1", state="completed")
        )
        clip_detail = {
            "clip": self._CLIP,
            "job_id": "job_1",
            "metadata_summary": {"available": True, "title": "Sample"},
            "validation_summary": {"state": "passed", "detail": "ok"},
            "module_results": [],
            "related_reports": [],
            "media_path": "/ops/outputs/job_1/clip_a/media",
        }
        with mock.patch(
            "ops_ui.outputs_ui.latest_run_id_with_clips",
            return_value="run_1",
        ):
            with mock.patch(
                "ops_ui.outputs_ui.list_clips_for_run",
                return_value=[
                    {
                        "clip_id": "clip_a",
                        "job_id": "job_1",
                        "run_id": "run_1",
                        "preview_available": True,
                        "media_path": "/ops/outputs/job_1/clip_a/media",
                        "title_or_hook": "Sample",
                    }
                ],
            ):
                with mock.patch(
                    "ops_ui.outputs_ui.get_run_summary",
                    return_value=SimpleNamespace(
                        run_id="run_1",
                        status="SUCCESS",
                        started_at="2026-07-04T11:00:00Z",
                        finished_at="2026-07-04T12:00:00Z",
                        funnel_id="business",
                        to_dict=lambda: {
                            "run_id": "run_1",
                            "finished_at": "2026-07-04T12:00:00Z",
                            "funnel_id": "business",
                        },
                    ),
                ):
                    with mock.patch(
                        "ops_ui.outputs_ui.list_run_summaries",
                        return_value=[SimpleNamespace(run_id="run_1", status="SUCCESS", finished_at="2026-07-04T12:00:00Z", started_at="", funnel_id="business")],
                    ):
                        listing = app.test_client().get("/ops/outputs").data
        assert b"Outputs" in listing
        assert b"/ops/outputs/job_1/clip_a/media" in listing
        legacy_section = listing.split(b"Advanced / Legacy", 1)[-1]
        assert b"Legacy clip review" not in legacy_section

        with mock.patch("ops_ui.outputs_ui.get_clip_detail", return_value=clip_detail):
            with mock.patch("ops_ui.outputs_ui.get_job_detail", return_value=job_detail):
                detail = app.test_client().get("/ops/outputs/job_1/clip_a").data
        assert b"Preview" in detail
        assert b"/ops/jobs/job_1" in detail
        assert b"/ops/runs/run_1" in detail
        assert b"Operator Console" in detail


class TestDiagnosticsWorkflow:
    """Scenario 5 — diagnostic pages link back to console."""

    def test_diagnostic_pages_render_and_return(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        browser_accept = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        for path in ("/ops/storage", "/ops/configuration", "/health", "/logs"):
            headers = browser_accept if path == "/health" else {}
            body = app.test_client().get(path, headers=headers).data
            assert body, f"{path} returned empty body"
            assert b"Operator Console" in body or b"/ops" in body

        storage = app.test_client().get("/ops/storage").data
        assert b"Is storage safe" in storage or b"Storage" in storage

        with mock.patch("ops_ui.config_ui.build_config_view", return_value=None):
            config = app.test_client().get("/ops/configuration").data
        assert b"Legacy settings" in config

        health = app.test_client().get(
            "/health",
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        ).data
        assert b"Diagnostic" in health
        assert b"Advanced / legacy" not in health.split(b"Diagnostic", 1)[0]
        assert b"Console" in health or b'href="/ops"' in health

        logs = app.test_client().get("/logs").data
        assert b"Diagnostic" in logs


class TestLegacyAdvancedWorkflow:
    """Scenario 6 — legacy pages remain reachable with notices."""

    def test_legacy_pages_show_notices(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.app._video_jobs", return_value=[]):
            with mock.patch("ops_ui.app._upload_jobs", return_value=[]):
                failed = app.test_client().get("/failed").data
        assert b"Advanced / legacy" in failed
        assert b"/ops/failures" in failed

        clip = app.test_client().get("/clip-review", follow_redirects=False)
        assert clip.status_code in {302, 301}
        assert "/ops/outputs" in (clip.headers.get("Location") or "")

        dashboard = app.test_client().get("/dashboard").data
        assert b"Advanced / legacy" in dashboard
        assert b"Mission Control" in dashboard

        settings = app.test_client().get("/settings").data
        assert b"Advanced / legacy" in settings
        assert b"/ops/configuration" in settings


class TestSafeControlsWorkflow:
    """Scenario 7 — safe controls protected and canonically placed."""

    def test_run_pipeline_only_on_console(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        pages = (
            "/ops/runs",
            "/ops/jobs",
            "/ops/outputs",
            "/ops/failures",
            "/dashboard",
            "/failed",
        )
        for path in pages:
            body = app.test_client().get(path).data
            assert b"Run pipeline" not in body, f"Run pipeline found on {path}"

        console = app.test_client().get("/ops").data
        assert len(re.findall(rb">Run pipeline<", console)) >= 1
        assert b'name="csrf_token"' in console
        assert b"Validate config" in console
        assert b"Read-only state" in console or b"Configuration" in console

    def test_risky_actions_marked(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        body = app.test_client().get("/ops").data
        assert b"risk-high" in body
        assert b"Enable uploads" in body
        assert b"Restart worker" in body or b"Restart API" in body


class TestAllPagesRender:
    """Smoke — modern, diagnostic, and legacy routes still render."""

    def test_modern_ops_pages_render(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.failures_ui.failures_payload", return_value={"total_failures": 0, "failed_jobs": 0, "failed_runs": 0, "distinct_groups": 0, "groups": []}):
            with mock.patch("ops_ui.outputs_ui.latest_run_id_with_clips", return_value=None):
                with mock.patch("ops_ui.lists.runs_list_payload", return_value={"runs": []}):
                    with mock.patch("ops_ui.lists.jobs_list_payload", return_value={"jobs": []}):
                        for path in _MODERN_OPS_ROUTES:
                            assert app.test_client().get(path).status_code == 200, path

    def test_legacy_and_diagnostic_pages_render(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.app._video_jobs", return_value=[]):
            with mock.patch("ops_ui.app._upload_jobs", return_value=[]):
                for path in _LEGACY_ROUTES:
                    assert app.test_client().get(path).status_code == 200, path
        clip_review = app.test_client().get("/clip-review", follow_redirects=False)
        assert clip_review.status_code in {302, 301}
        assert "/ops/outputs" in (clip_review.headers.get("Location") or "")
        for path in _DIAGNOSTIC_ROUTES:
            if path == "/health":
                response = app.test_client().get(
                    path,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                    },
                )
            else:
                response = app.test_client().get(path)
            assert response.status_code == 200, path

    def test_health_json_contract_unchanged(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        response = app.test_client().get("/health")
        assert response.status_code == 200
        assert response.is_json
        payload = response.get_json()
        assert "data" in payload
        assert "schema_version" in payload
