"""Tests for JobDetail builder used by the Job Inspector."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ops"))

import run_records as rr  # noqa: E402
from observability.job_inspector import build_job_detail  # noqa: E402
from observability.models import JobDetail  # noqa: E402


@pytest.fixture
def env_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)
    import observability.index as index_mod
    import observability.artifacts as artifacts_mod
    import observability.job_inspector as inspector_mod

    monkeypatch.setattr(index_mod, "REPO_ROOT", tmp_path)
    jobs_root = tmp_path / "jobs" / "dev"
    jobs_root.mkdir(parents=True)
    for mod in (index_mod, artifacts_mod, inspector_mod):
        monkeypatch.setattr(mod, "_jobs_root_for", lambda _env, root=jobs_root: root)
    return {"jobs": jobs_root, "root": tmp_path}


def _write_job(
    env_roots: dict,
    *,
    job_id: str,
    status: str = "completed",
    stage: str = "posting",
    errors: list | None = None,
    files: dict[str, str] | None = None,
    binary_files: dict[str, bytes] | None = None,
) -> Path:
    job_dir = env_roots["jobs"] / job_id
    job_dir.mkdir(parents=True)
    report = {
        "job_id": job_id,
        "status": status,
        "current_stage": stage,
        "created_at": "2026-07-04T00:00:00Z",
        "started_at": "2026-07-04T00:00:10Z",
        "completed_at": "2026-07-04T00:05:00Z" if status != "running" else None,
        "errors": errors or [],
        "warnings": ["low confidence"] if status == "failed" else [],
        "clips": [],
        "stage_timings_ms": {"source": 1, "transcript": 2} if status != "queued" else {},
        "execution_context": {
            "environment": "development",
            "funnel_id": "business",
            "platform_id": "youtube",
            "preset_id": "growth",
            "trigger": "manual_cli",
        },
        "funnel": {"funnel_id": "business"},
    }
    (job_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    for relative, content in (files or {}).items():
        path = job_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for relative, content in (binary_files or {}).items():
        path = job_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return job_dir


class TestJobInspectorBuilder:
    def test_successful_job_timeline_and_reports(self, env_roots):
        _write_job(
            env_roots,
            job_id="job_ok",
            status="completed",
            stage="posting",
            files={
                "transcript.json": "{}",
                "processing_report.json": json.dumps(
                    {
                        "candidates_discovered": 17,
                        "sections_analysed": 4,
                        "warnings": ["w1"],
                    }
                ),
                "post_processing/reports/post_processing_report.json": json.dumps(
                    {
                        "candidates_selected": 5,
                        "reserve_candidates": 3,
                        "candidates_rejected": 9,
                        "clips_rendered": 5,
                        "clips_passed": 5,
                        "clips_failed": 0,
                        "modules_run": ["render_clip_v1", "validation_v1"],
                        "failed_modules": [],
                    }
                ),
                "job.log": "ok\n",
            },
            binary_files={"clips/clip_a.mp4": b"x"},
        )
        detail = build_job_detail("dev", "job_ok")
        assert isinstance(detail, JobDetail)
        assert detail.summary.state == "completed"
        assert detail.trigger == "manual_cli"
        assert detail.created_at is not None
        stages = {s.stage: s for s in detail.stage_timeline}
        assert "source" in stages
        assert "processing" in stages
        assert "17 candidates discovered" in (stages["processing"].detail or "")
        assert "5 selected" in (stages["selection"].detail or "")
        assert any(a.artifact_type == "transcript" and a.exists for a in detail.artifacts)
        proc = next(r for r in detail.report_summaries if r["report_type"] == "processing_report")
        assert proc["available"] is True
        assert proc["metrics"]["candidates_discovered"] == 17
        assert detail.output_summary is not None
        assert detail.output_summary["outputs_produced"] == 5
        assert detail.output_summary["validation_state"] == "passed"
        assert detail.failures == []
        assert detail.clips

    def test_failed_job_surfaces_failure_and_missing_reports(self, env_roots):
        _write_job(
            env_roots,
            job_id="job_fail",
            status="failed",
            stage="captions",
            errors=[{"message": "Missing transcript segment", "module": "intelligent_captions_v1", "stage": "captions"}],
            files={
                "post_processing/reports/post_processing_report.json": json.dumps(
                    {
                        "failed_modules": [
                            {
                                "module_name": "intelligent_captions_v1",
                                "error": "Missing transcript segment",
                                "stage": "captions",
                            }
                        ],
                        "clips_passed": 0,
                        "clips_failed": 1,
                    }
                )
            },
        )
        detail = build_job_detail("dev", "job_fail")
        assert detail is not None
        assert detail.summary.state == "failed"
        assert detail.failures
        assert any("Missing transcript segment" in f.reason for f in detail.failures)
        captions = next(s for s in detail.stage_timeline if s.stage == "captions")
        assert captions.result == "failed"
        proc = next(r for r in detail.report_summaries if r["report_type"] == "processing_report")
        assert proc["available"] is False
        assert proc["detail"] == "Not available"
        assert any(a.artifact_type == "transcript" and not a.exists for a in detail.artifacts)

    def test_missing_job_returns_none(self, env_roots):
        assert build_job_detail("dev", "missing") is None
        assert build_job_detail("dev", "../etc/passwd") is None

    def test_partial_job_degrades_gracefully(self, env_roots):
        _write_job(env_roots, job_id="job_partial", status="queued", stage="queued")
        detail = build_job_detail("dev", "job_partial")
        assert detail is not None
        assert detail.stage_timeline
        assert all(s.detail for s in detail.stage_timeline)
        assert detail.output_summary is not None
        assert detail.output_summary["posting_state"] == "Not available"
