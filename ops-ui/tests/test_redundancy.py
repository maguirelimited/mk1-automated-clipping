"""Redundancy / canonical-path labelling tests (Prompt 11)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings
from ops_ui.shell import SHELL_NAV_LEGACY, build_shell_context


def _settings(tmp_path: Path) -> Settings:
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
        environment="dev",
        services=(
            ServiceConfig(
                key="source-input",
                label="source-input",
                base_url="http://127.0.0.1:9",
                systemd_unit="mk04-source-input.service",
            ),
        ),
    )


class TestCanonicalLabelling:
    def test_legacy_nav_labels_distinguish_modern_paths(self, tmp_path: Path) -> None:
        ctx = build_shell_context(_settings(tmp_path))
        labels = [item["label"] for item in ctx["shell_nav_legacy"]]
        assert labels == [label for _, label, _ in SHELL_NAV_LEGACY]
        assert "Legacy failed jobs" in labels
        assert "Legacy clip review" not in labels
        assert "Legacy settings" in labels
        assert "Failures" not in labels

    def test_modern_failures_page_labels(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch(
            "ops_ui.failures_ui.failures_payload",
            return_value={
                "total_failures": 0,
                "failed_jobs": 0,
                "failed_runs": 0,
                "distinct_groups": 0,
                "groups": [],
            },
        ):
            body = app.test_client().get("/ops/failures").data
        assert b"What failed" in body
        main = body.split(b'class="legacy-nav"', 1)[0]
        assert b"/failed" not in main
        assert b"Legacy failed jobs" not in main

    def test_legacy_failed_jobs_page_points_to_modern_failures(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.app._video_jobs", return_value=[]):
            with mock.patch("ops_ui.app._upload_jobs", return_value=[]):
                body = app.test_client().get("/failed").data
        assert b"Legacy failed jobs" in body
        assert b"/ops/failures" in body
        assert b"Operator Console" in body

    def test_legacy_clip_review_redirects_to_outputs(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        response = app.test_client().get("/clip-review")
        assert response.status_code in {302, 301}
        assert "/ops/outputs" in (response.headers.get("Location") or "")

    def test_legacy_clip_review_detail_redirects_with_run_id(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        job_detail = mock.Mock()
        job_detail.summary = mock.Mock(run_id="run_1")
        with mock.patch("ops_ui.outputs_ui.get_job_detail", return_value=job_detail):
            response = app.test_client().get("/clip-review/job_a/clip_1")
        assert response.status_code in {302, 301}
        location = response.headers.get("Location") or ""
        assert "run_id=run_1" in location
        assert "/ops/outputs" in location

    def test_diagnostic_logs_is_not_labelled_legacy(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        body = app.test_client().get("/logs").data
        assert b"Diagnostic" in body
        assert b"Advanced / legacy" not in body.split(b"Diagnostic", 1)[0]
        assert b"/ops/failures" in body

    def test_configuration_distinguishes_legacy_settings(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.config_ui.build_config_view", return_value=None):
            body = app.test_client().get("/ops/configuration").data
        assert b"Legacy settings" in body
        assert b"/settings" in body

    def test_console_safe_actions_still_present(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        body = app.test_client().get("/ops").data
        assert b"Safe actions" in body
        assert b"Run pipeline" in body
        assert b'name="csrf_token"' in body
        assert b"Refresh health" in body

    def test_primary_nav_unchanged(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        body = app.test_client().get("/ops").data
        assert body.index(b'href="/ops"') < body.index(b'href="/ops/outputs"')
        assert b'href="/ops/failures"' in body
        assert b'href="/health"' in body
