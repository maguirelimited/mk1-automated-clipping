"""Tests for run/job indexing (Phase 3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
OPS_DIR = REPO_ROOT / "scripts" / "ops"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(OPS_DIR))

import run_records as rr  # noqa: E402
from observability.index import (  # noqa: E402
    _find_job_dir,
    get_job_detail,
    get_run_summary,
    jobs_list_payload,
    list_job_summaries,
    list_run_summaries,
    runs_list_payload,
)
from observability.models import JobDetail, RunSummary  # noqa: E402


@pytest.fixture
def env_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)
    import observability.index as index_mod

    monkeypatch.setattr(index_mod, "REPO_ROOT", tmp_path)

    runs_root = tmp_path / "runs" / "dev"
    jobs_root = tmp_path / "jobs" / "dev"
    runs_root.mkdir(parents=True)
    jobs_root.mkdir(parents=True)

    # Avoid ConfigManager dependency on real config for jobs_root.
    monkeypatch.setattr(index_mod, "_jobs_root_for", lambda _env: jobs_root)
    return {"runs": runs_root, "jobs": jobs_root, "root": tmp_path}


def _write_run(
    env_roots: dict,
    *,
    run_id: str,
    status: str,
    trigger: str = "manual_cli",
    failure_reason: str | None = None,
) -> None:
    run_dir = env_roots["runs"] / run_id
    run_dir.mkdir(parents=True)
    record = rr.RunRecord(
        run_id=run_id,
        environment="dev",
        trigger=trigger,
        status=status,
        started_at="2026-07-04T00:00:00Z",
        finished_at="2026-07-04T00:01:00Z",
        duration_seconds=60.0,
        failure_reason=failure_reason,
        jobs_started=1 if status != "SKIPPED" else 0,
        jobs_completed=1 if status == "SUCCESS" else 0,
        jobs_failed=1 if status == "FAIL" else 0,
        log_path=str(run_dir / "run.log"),
        funnel_id="business",
        report_paths=[],
    )
    rr.write_record(run_dir, record)
    (run_dir / "run.log").write_text("log\n", encoding="utf-8")


def _write_job(
    env_roots: dict,
    *,
    job_id: str,
    status: str,
    stage: str,
    errors: list | None = None,
    warnings: list | None = None,
    clips: list | None = None,
) -> None:
    job_dir = env_roots["jobs"] / job_id
    job_dir.mkdir(parents=True)
    report = {
        "job_id": job_id,
        "status": status,
        "current_stage": stage,
        "created_at": "2026-07-04T00:00:00Z",
        "started_at": "2026-07-04T00:00:10Z",
        "completed_at": "2026-07-04T00:02:00Z" if status != "running" else None,
        "errors": errors or [],
        "warnings": warnings or [],
        "clips": clips or [],
        "stage_timings_ms": {"source": 10, "transcript": 20} if status != "queued" else {},
        "execution_context": {
            "environment": "development",
            "funnel_id": "business",
            "platform_id": "youtube",
            "preset_id": "growth",
        },
        "funnel": {"funnel_id": "business"},
    }
    (job_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")


class TestRunIndex:
    def test_lists_runs_most_recent_first(self, env_roots):
        _write_run(env_roots, run_id="run_20260704T000001Z_manual_cli", status="SUCCESS")
        _write_run(
            env_roots,
            run_id="run_20260704T000002Z_scheduled",
            status="SKIPPED",
            trigger="scheduled",
            failure_reason="execution lock held",
        )
        _write_run(
            env_roots,
            run_id="run_20260704T000003Z_manual_cli",
            status="FAIL",
            failure_reason="boot readiness NOT READY",
        )

        runs = list_run_summaries("dev")
        assert [r.run_id for r in runs] == [
            "run_20260704T000003Z_manual_cli",
            "run_20260704T000002Z_scheduled",
            "run_20260704T000001Z_manual_cli",
        ]
        assert runs[0].status == "FAIL"
        assert runs[0].failure_summary is not None
        assert runs[1].status == "SKIPPED"
        assert runs[1].failure_summary is not None
        assert runs[1].failure_summary.severity == "warn"
        assert runs[0].log_path == "runs/dev/run_20260704T000003Z_manual_cli/run.log"
        assert "/home/" not in (runs[0].log_path or "")

    def test_get_run_summary(self, env_roots):
        _write_run(env_roots, run_id="run_ok", status="SUCCESS")
        summary = get_run_summary("dev", "run_ok")
        assert isinstance(summary, RunSummary)
        assert summary.run_id == "run_ok"
        assert summary.environment == "dev"
        assert summary.trigger == "manual_cli"

    def test_missing_run_returns_none(self, env_roots):
        assert get_run_summary("dev", "run_missing") is None
        assert get_run_summary("dev", "../etc/passwd") is None

    def test_runs_payload_is_environment_scoped(self, env_roots):
        _write_run(env_roots, run_id="run_dev_only", status="SUCCESS")
        payload = runs_list_payload("dev")
        assert payload["environment"] == "dev"
        assert payload["count"] == 1
        assert payload["runs"][0]["run_id"] == "run_dev_only"


class TestJobIndex:
    def test_lists_jobs_and_failed_jobs(self, env_roots):
        _write_job(
            env_roots,
            job_id="job_ok",
            status="completed",
            stage="posting",
            clips=[{"clip_id": "c1"}],
        )
        _write_job(
            env_roots,
            job_id="job_fail",
            status="failed",
            stage="captions",
            errors=[{"message": "caption module failed", "component": "captions"}],
        )

        jobs = list_job_summaries("dev")
        by_id = {j.job_id: j for j in jobs}
        assert "job_ok" in by_id
        assert "job_fail" in by_id
        assert by_id["job_ok"].state == "completed"
        assert by_id["job_ok"].funnel == "business"
        assert by_id["job_ok"].platform == "youtube"
        assert by_id["job_ok"].preset == "growth"
        assert by_id["job_ok"].outputs.outputs_produced == 1
        assert by_id["job_fail"].state == "failed"
        assert by_id["job_fail"].failure_summary is not None
        assert "caption" in by_id["job_fail"].failure_summary.reason

    def test_job_detail_includes_artifact_references(self, env_roots):
        _write_job(
            env_roots,
            job_id="job_detail",
            status="failed",
            stage="validation",
            errors=["validation failed"],
            warnings=["low confidence"],
        )
        detail = get_job_detail("dev", "job_detail")
        assert isinstance(detail, JobDetail)
        assert detail.job_id == "job_detail"
        assert detail.summary.state == "failed"
        assert detail.stage_timeline
        assert len(detail.stage_timeline) >= 8
        assert detail.artifacts
        assert any(a.artifact_type == "transcript" and not a.exists for a in detail.artifacts)
        assert detail.report_summaries
        assert detail.output_summary is not None
        assert detail.failures
        assert detail.warnings
        assert detail.logs
        assert detail.logs[0].source == "job"

    def test_missing_job_returns_none(self, env_roots):
        assert get_job_detail("dev", "job_missing") is None
        assert get_job_detail("dev", "../secret") is None

    def test_find_job_dir_supports_legacy_folder_names(self, env_roots):
        job_id = "job_legacy_001"
        legacy_dir = env_roots["jobs"] / f"input_source_{job_id}"
        legacy_dir.mkdir(parents=True)
        report = {
            "job_id": job_id,
            "status": "success",
            "current_stage": "success",
            "clips": [{"clip_id": f"{job_id}_clip_01"}],
        }
        (legacy_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
        assert _find_job_dir(env_roots["jobs"], job_id) == legacy_dir
        assert get_job_detail("dev", job_id) is not None

    def test_find_job_dir_prefers_successful_copy_over_running_stub(self, env_roots):
        job_id = "job_dup_001"
        stub_dir = env_roots["jobs"] / job_id
        stub_dir.mkdir(parents=True)
        (stub_dir / "report.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "status": "running",
                    "current_stage": "transcribe",
                    "clips": [],
                }
            ),
            encoding="utf-8",
        )
        legacy_dir = env_roots["jobs"] / f"input_source_{job_id}"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "report.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "status": "success",
                    "current_stage": "success",
                    "clips": [{"clip_id": f"{job_id}_clip_01", "clip_file": "clip.mp4"}],
                }
            ),
            encoding="utf-8",
        )
        assert _find_job_dir(env_roots["jobs"], job_id) == legacy_dir

    def test_jobs_payload_environment(self, env_roots):
        _write_job(env_roots, job_id="job_a", status="queued", stage="queued")
        payload = jobs_list_payload("dev")
        assert payload["environment"] == "dev"
        assert payload["count"] == 1
