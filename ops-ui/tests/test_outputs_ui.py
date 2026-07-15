"""Output Browser UI tests (Phase 10)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ops_ui.app import create_app
from ops_ui.config import ServiceConfig, Settings


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


_RUN_CLIP = {
    "clip_id": "clip_a",
    "job_id": "job_1",
    "run_id": "run_1",
    "preview_available": True,
    "media_path": "/ops/outputs/job_1/clip_a/media",
    "duration_seconds": 42.0,
    "funnel": "business",
    "created_at": "2026-07-04T12:00:00Z",
    "title_or_hook": "Stop scrolling for this",
    "score": 8.4,
}

_RUN_CLIP_OTHER = {
    **_RUN_CLIP,
    "clip_id": "clip_b",
    "job_id": "job_2",
    "media_path": "/ops/outputs/job_2/clip_b/media",
    "title_or_hook": "Other run clip",
}

_RUN_CLIP_SPARSE = {
    "clip_id": "clip_sparse",
    "job_id": "job_sparse",
    "run_id": "run_1",
    "preview_available": False,
    "media_path": None,
    "duration_seconds": 18.0,
    "funnel": "business",
    "created_at": "2026-07-04T12:01:00Z",
}

_RUN_SUMMARY = SimpleNamespace(
    run_id="run_1",
    status="SUCCESS",
    started_at="2026-07-04T11:00:00Z",
    finished_at="2026-07-04T12:00:00Z",
    funnel_id="business",
)

_RUN_SELECTOR = SimpleNamespace(
    run_id="run_1",
    status="SUCCESS",
    started_at="2026-07-04T11:00:00Z",
    finished_at="2026-07-04T12:00:00Z",
    funnel_id="business",
)

_RUN_SELECTOR_OLD = SimpleNamespace(
    run_id="run_old",
    status="SUCCESS",
    started_at="2026-07-03T10:00:00Z",
    finished_at="2026-07-03T11:00:00Z",
    funnel_id="business",
)


def _run_summary(*, run_id: str = "run_1"):
    return SimpleNamespace(
        run_id=run_id,
        status="SUCCESS",
        started_at="2026-07-04T11:00:00Z",
        finished_at="2026-07-04T12:00:00Z",
        funnel_id="business",
        to_dict=lambda: {
            "run_id": run_id,
            "status": "SUCCESS",
            "started_at": "2026-07-04T11:00:00Z",
            "finished_at": "2026-07-04T12:00:00Z",
            "funnel_id": "business",
        },
    )


def _job_detail(*, run_id: str = "run_1", state: str = "completed"):
    summary = SimpleNamespace(run_id=run_id, state=state)
    return SimpleNamespace(summary=summary)


def _list_context_mocks(
    *,
    latest_run_id: str | None = "run_1",
    clips: list | None = None,
    run_summaries: list | None = None,
):
    clips = [_RUN_CLIP] if clips is None else clips
    run_summaries = run_summaries or [_RUN_SELECTOR, _RUN_SELECTOR_OLD]
    return (
        mock.patch(
            "ops_ui.outputs_ui.latest_run_id_with_clips",
            return_value=latest_run_id,
        ),
        mock.patch(
            "ops_ui.outputs_ui.list_clips_for_run",
            return_value=clips,
        ),
        mock.patch(
            "ops_ui.outputs_ui.get_run_summary",
            side_effect=lambda _token, run_id: _run_summary(run_id=run_id),
        ),
        mock.patch(
            "ops_ui.outputs_ui.list_run_summaries",
            return_value=run_summaries,
        ),
    )


class TestOutputsRunReviewUi:
    def test_defaults_to_latest_run_with_clips(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        latest, list_clips, _run, _selector = _list_context_mocks()
        with latest, list_clips as list_mock, _run, _selector:
            response = app.test_client().get("/ops/outputs")
        assert response.status_code == 200
        body = response.data
        assert b"Outputs" in body
        assert b"run_1" in body
        assert b"Stop scrolling for this" in body
        assert b"42.0s" in body or b"42s" in body
        assert b"8.4" in body
        assert b"/ops/outputs/job_1/clip_a/media" in body
        assert b"Latest clips (all recent jobs)" not in body
        list_mock.assert_called_once()
        assert list_mock.call_args.args[1] == "run_1"

    def test_run_id_query_selects_run(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.outputs_ui.latest_run_id_with_clips", return_value="run_1") as latest_mock:
            with mock.patch(
                "ops_ui.outputs_ui.list_clips_for_run",
                return_value=[_RUN_CLIP_OTHER],
            ) as list_mock:
                with mock.patch(
                    "ops_ui.outputs_ui.get_run_summary",
                    side_effect=lambda _token, run_id: _run_summary(run_id=run_id),
                ):
                    with mock.patch(
                        "ops_ui.outputs_ui.list_run_summaries",
                        return_value=[_RUN_SELECTOR, _RUN_SELECTOR_OLD],
                    ):
                        response = app.test_client().get("/ops/outputs?run_id=run_old")
        assert response.status_code == 200
        list_mock.assert_called_once_with(mock.ANY, "run_old")
        assert b"Other run clip" in response.data
        latest_mock.assert_not_called()

    def test_clips_scoped_to_requested_run_only(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        _, list_clips, _run, _selector = _list_context_mocks(clips=[_RUN_CLIP])
        with mock.patch("ops_ui.outputs_ui.latest_run_id_with_clips", return_value="run_1"), list_clips, _run, _selector:
            body = app.test_client().get("/ops/outputs?run_id=run_1").data
        assert b"clip_b" not in body
        assert b"Stop scrolling for this" in body

    def test_run_selector_lists_recent_runs(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        _, list_clips, _run, selector = _list_context_mocks()
        with mock.patch("ops_ui.outputs_ui.latest_run_id_with_clips", return_value="run_1"), list_clips, _run, selector:
            body = app.test_client().get("/ops/outputs").data
        assert b'name="run_id"' in body
        assert b"run_1" in body
        assert b"run_old" in body
        assert b"SUCCESS" in body
        assert b"Recent runs" not in body
        assert b"recent_runs" not in body

    def test_no_runs_with_clips_empty_state(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.outputs_ui.latest_run_id_with_clips", return_value=None):
            with mock.patch("ops_ui.outputs_ui.list_recent_output_clips", return_value=[]):
                body = app.test_client().get("/ops/outputs").data
        assert b"No successful runs found yet" in body

    def test_selected_run_without_clips(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        _, list_clips, _run, _selector = _list_context_mocks(clips=[])
        with mock.patch("ops_ui.outputs_ui.latest_run_id_with_clips", return_value="run_1"), list_clips, _run, _selector:
            body = app.test_client().get("/ops/outputs").data
        assert b"No clips produced for this run" in body

    def test_missing_title_score_duration_tolerated(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        _, list_clips, _run, _selector = _list_context_mocks(clips=[_RUN_CLIP_SPARSE])
        with mock.patch("ops_ui.outputs_ui.latest_run_id_with_clips", return_value="run_1"), list_clips, _run, _selector:
            body = app.test_client().get("/ops/outputs").data
        assert b"Untitled clip" in body
        assert b"18.0s" in body or b"18s" in body
        assert b"<dt>Score</dt>" not in body
        assert b"Preview unavailable" in body

    def test_no_validation_posting_reframe_or_review_controls(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        _, list_clips, _run, _selector = _list_context_mocks()
        with mock.patch("ops_ui.outputs_ui.latest_run_id_with_clips", return_value="run_1"), list_clips, _run, _selector:
            body = app.test_client().get("/ops/outputs").data
        for token in (
            b"validation",
            b"posting",
            b"Reframe",
            b"approve",
            b"reject",
            b"Inspect clip",
            b"does not approve",
        ):
            assert token not in body

    def test_job_id_deep_link_filters_clips(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        job_clips = [_RUN_CLIP]
        with mock.patch("ops_ui.outputs_ui.get_job_detail", return_value=_job_detail()):
            with mock.patch(
                "ops_ui.outputs_ui.list_clips_for_job",
                return_value=job_clips,
            ) as list_job_mock:
                body = app.test_client().get("/ops/outputs?job_id=job_1").data
        list_job_mock.assert_called_once_with(mock.ANY, "job_1")
        assert b"Showing clips for job" in body
        assert b"job_1" in body
        assert b"Stop scrolling for this" in body

    def test_defaults_to_recent_jobs_when_no_pipeline_run(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.outputs_ui.latest_run_id_with_clips", return_value=None):
            with mock.patch(
                "ops_ui.outputs_ui.list_recent_output_clips",
                return_value=[_RUN_CLIP],
            ) as recent_mock:
                body = app.test_client().get("/ops/outputs").data
        recent_mock.assert_called_once()
        assert b"Stop scrolling for this" in body

    def test_funnel_id_query_scopes_outputs(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        funnel_clip = {**_RUN_CLIP, "funnel": "gta_clips_002"}
        with mock.patch("ops_ui.outputs_ui.latest_run_id_with_clips") as latest_mock:
            with mock.patch(
                "ops_ui.outputs_ui.list_clips_for_funnel",
                return_value=[funnel_clip],
            ) as funnel_mock:
                with mock.patch(
                    "ops_ui.outputs_ui.latest_job_id_for_funnel",
                    return_value="job_gta",
                ):
                    body = app.test_client().get("/ops/outputs?funnel_id=gta_clips_002").data
        latest_mock.assert_not_called()
        funnel_mock.assert_called_once_with(mock.ANY, "gta_clips_002")
        assert b"gta_clips_002" in body
        assert b"Stop scrolling for this" in body

    def test_funnel_without_clips_shows_latest_job(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.outputs_ui.list_clips_for_funnel", return_value=[]):
            with mock.patch(
                "ops_ui.outputs_ui.latest_job_id_for_funnel",
                return_value="job_failed_gta",
            ):
                body = app.test_client().get("/ops/outputs?funnel_id=gta_clips_002").data
        assert b"No clips produced for funnel" in body
        assert b"job_failed_gta" in body



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
    "metadata_reference": {
        "artifact_type": "clip_metadata",
        "exists": False,
        "path": "jobs/dev/job_1/post_processing/metadata",
    },
}


def _reframe_summary(**overrides):
    base = {
        "available": True,
        "reframe_mode": "auto",
        "format_strategy": "blurred_background_fit_foreground",
        "face_track_test_enabled": True,
        "face_track_attempted": True,
        "face_track_used": False,
        "face_track_eligibility_reason": "leading_no_face_gap",
        "module_status": "PASS",
    }
    base.update(overrides)
    return base


class TestOutputDetailUi:
    def test_output_detail_reframing_section_face_track_used(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        detail = {
            "clip": _CLIP,
            "job_id": "job_1",
            "metadata_summary": {"available": False, "detail": "Not available"},
            "validation_summary": {"state": "passed", "detail": "Not available"},
            "reframe_summary": _reframe_summary(
                face_track_used=True,
                face_track_eligible=True,
                format_strategy="face_track_crop",
                face_track_eligibility_reason="eligible",
                face_coverage_pct=100.0,
            ),
            "module_results": [],
            "related_reports": [],
            "media_path": "/ops/outputs/job_1/clip_a/media",
        }
        with mock.patch("ops_ui.outputs_ui.get_clip_detail", return_value=detail):
            with mock.patch(
                "ops_ui.outputs_ui.get_job_detail",
                return_value=_job_detail(run_id="run_1"),
            ):
                response = app.test_client().get("/ops/outputs/job_1/clip_a")
        assert response.status_code == 200
        body = response.data
        assert b"Reframing" in body
        assert b"Face-track: Used" in body
        assert b"face coverage pct" in body

    def test_output_detail_reframing_disabled(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        detail = {
            "clip": _CLIP,
            "job_id": "job_1",
            "metadata_summary": {"available": False, "detail": "Not available"},
            "validation_summary": {"state": "passed", "detail": "Not available"},
            "reframe_summary": _reframe_summary(
                face_track_test_enabled=False,
                face_track_attempted=False,
                face_track_skip_reason="face_track_test_disabled",
            ),
            "module_results": [],
            "related_reports": [],
            "media_path": None,
        }
        with mock.patch("ops_ui.outputs_ui.get_clip_detail", return_value=detail):
            with mock.patch(
                "ops_ui.outputs_ui.get_job_detail",
                return_value=_job_detail(run_id=""),
            ):
                response = app.test_client().get("/ops/outputs/job_1/clip_a")
        assert response.status_code == 200
        body = response.data
        assert b"Disabled" in body
        assert b"face_track_test_disabled" in body

    def test_output_detail_flags_inconsistent_metadata(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        detail = {
            "clip": _CLIP,
            "job_id": "job_1",
            "metadata_summary": {"available": False, "detail": "Not available"},
            "validation_summary": {"state": "passed", "detail": "Not available"},
            "reframe_summary": _reframe_summary(
                face_track_used=True,
                face_track_eligible=False,
                format_strategy="face_track_crop",
            ),
            "module_results": [],
            "related_reports": [],
            "media_path": None,
        }
        with mock.patch("ops_ui.outputs_ui.get_clip_detail", return_value=detail):
            with mock.patch(
                "ops_ui.outputs_ui.get_job_detail",
                return_value=_job_detail(run_id=""),
            ):
                response = app.test_client().get("/ops/outputs/job_1/clip_a")
        assert response.status_code == 200
        body = response.data
        assert b"Metadata warnings" in body
        assert b"face_track_eligible is false" in body

    def test_output_detail_and_loop_links(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        detail = {
            "clip": _CLIP,
            "job_id": "job_1",
            "metadata_summary": {
                "available": True,
                "title": "Sample title",
                "candidate_id": "cand_1",
                "duration_sec": 42,
                "validation_result": "passed",
                "path": "jobs/dev/job_1/post_processing/metadata/clip_a.json",
            },
            "validation_summary": {"state": "passed", "detail": "Not available"},
            "module_results": [],
            "related_reports": [
                {
                    "report_type": "processing_report",
                    "exists": False,
                    "path": "jobs/dev/job_1/processing_report.json",
                }
            ],
            "media_path": "/ops/outputs/job_1/clip_a/media",
        }
        with mock.patch("ops_ui.outputs_ui.get_clip_detail", return_value=detail):
            with mock.patch(
                "ops_ui.outputs_ui.get_job_detail",
                return_value=_job_detail(run_id="run_1"),
            ):
                response = app.test_client().get("/ops/outputs/job_1/clip_a")
        assert response.status_code == 200
        body = response.data
        assert b"clip_a" in body
        assert b"Sample title" in body
        assert b"Preview" in body
        assert b"Operator Console" in body
        assert b"/ops/jobs/job_1" in body
        assert b"/ops/runs/run_1" in body
        assert b"Legacy clip review" not in body
        assert b"does not gate publishing" in body
        assert b"Related reports" in body

    def test_output_detail_without_run_context(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        detail = {
            "clip": {**_CLIP, "preview_available": False, "exists": False},
            "job_id": "job_1",
            "metadata_summary": {"available": False, "detail": "Not available"},
            "validation_summary": {"state": "failed", "detail": "validation error"},
            "module_results": [],
            "related_reports": [],
            "media_path": None,
        }
        with mock.patch("ops_ui.outputs_ui.get_clip_detail", return_value=detail):
            with mock.patch(
                "ops_ui.outputs_ui.get_job_detail",
                return_value=_job_detail(run_id=""),
            ):
                response = app.test_client().get("/ops/outputs/job_1/clip_a")
        assert response.status_code == 200
        body = response.data
        assert b"Preview unavailable" in body
        assert b"missing on disk" in body
        assert b"/ops/runs/" not in body
        assert b"/ops/failures" in body

    def test_outputs_api_endpoint(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch(
            "ops_ui.observability.outputs_list_payload",
            return_value={"environment": "dev", "outputs": [_CLIP], "count": 1, "schema_version": 1},
        ):
            response = app.test_client().get("/outputs")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["data"]["count"] == 1
        assert payload["data"]["outputs"][0]["clip_id"] == "clip_a"

    def test_missing_output_redirects(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch("ops_ui.outputs_ui.get_clip_detail", return_value=None):
            response = app.test_client().get("/ops/outputs/job_x/clip_y")
        assert response.status_code in {302, 301}
        assert "/ops/outputs" in (response.headers.get("Location") or "")

    def test_output_media_route(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        media_path = tmp_path / "clip_a.mp4"
        media_path.write_bytes(b"fake-video")
        with mock.patch(
            "ops_ui.media.resolve_clip_media_path",
            return_value=media_path,
        ):
            response = app.test_client().get("/ops/outputs/job_1/clip_a/media")
        assert response.status_code == 200


class TestOutputsLinkCleanup:
    def test_shell_nav_excludes_legacy_clip_review(self, tmp_path: Path) -> None:
        from ops_ui.shell import SHELL_NAV_LEGACY, build_shell_context

        labels = [item["label"] for item in build_shell_context(_settings(tmp_path))["shell_nav_legacy"]]
        assert "Legacy clip review" not in labels
        assert labels == [label for _, label, _ in SHELL_NAV_LEGACY]

    def test_console_quick_links_point_to_outputs(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        body = app.test_client().get("/ops").data
        daily_loop = body.split(b"Daily loop", 1)[1].split(b"Diagnostics", 1)[0]
        assert b'href="/ops/outputs"' in daily_loop
        assert b"/clip-review" not in daily_loop

    def test_clip_review_get_redirects_to_outputs(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        response = app.test_client().get("/clip-review", follow_redirects=False)
        assert response.status_code in {302, 301}
        assert "/ops/outputs" in (response.headers.get("Location") or "")

    def test_clip_review_detail_redirect_prefers_run_id(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch(
            "ops_ui.outputs_ui.get_job_detail",
            return_value=_job_detail(run_id="run_1"),
        ):
            response = app.test_client().get("/clip-review/job_1/clip_a", follow_redirects=False)
        assert response.status_code in {302, 301}
        location = response.headers.get("Location") or ""
        assert "run_id=run_1" in location

    def test_clip_review_detail_falls_back_to_job_id(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        with mock.patch(
            "ops_ui.outputs_ui.get_job_detail",
            return_value=_job_detail(run_id=""),
        ):
            response = app.test_client().get("/clip-review/job_1/clip_a", follow_redirects=False)
        assert response.status_code in {302, 301}
        location = response.headers.get("Location") or ""
        assert "job_id=job_1" in location

    def test_clip_review_post_approve_returns_gone(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        response = app.test_client().post(
            "/clip-review/job_1/clip_a/approve",
            follow_redirects=False,
        )
        assert response.status_code == 410
        assert "retired" in (response.get_data(as_text=True) or "").lower()
