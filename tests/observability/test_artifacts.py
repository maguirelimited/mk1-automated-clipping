"""Tests for the job artifact resolver (Phase 4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ops"))

import run_records as rr  # noqa: E402
from observability.artifacts import resolve_job_artifacts  # noqa: E402


@pytest.fixture
def env_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)
    import observability.index as index_mod
    import observability.artifacts as artifacts_mod

    monkeypatch.setattr(index_mod, "REPO_ROOT", tmp_path)
    jobs_root = tmp_path / "jobs" / "dev"
    jobs_root.mkdir(parents=True)
    monkeypatch.setattr(index_mod, "_jobs_root_for", lambda _env: jobs_root)
    monkeypatch.setattr(artifacts_mod, "_jobs_root_for", lambda _env: jobs_root)
    return {"jobs": jobs_root, "root": tmp_path}


def _write_job(
    env_roots: dict,
    *,
    job_id: str,
    status: str = "completed",
    files: dict[str, str] | None = None,
    binary_files: dict[str, bytes] | None = None,
) -> Path:
    job_dir = env_roots["jobs"] / job_id
    job_dir.mkdir(parents=True)
    report = {
        "job_id": job_id,
        "status": status,
        "current_stage": "validation" if status == "failed" else "posting",
        "errors": ["boom"] if status == "failed" else [],
        "warnings": [],
        "clips": [],
        "execution_context": {
            "environment": "development",
            "funnel_id": "business",
            "platform_id": "youtube",
            "preset_id": "growth",
        },
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


def _by_type(payload: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in payload["artifacts"]:
        out.setdefault(item["artifact_type"], []).append(item)
    return out


class TestArtifactResolver:
    def test_complete_artifact_set(self, env_roots):
        _write_job(
            env_roots,
            job_id="job_full",
            files={
                "transcript.json": "{}",
                "raw_candidate_pool.json": "{}",
                "processing_report.json": "{}",
                "post_processing/selection/selection_result.json": "{}",
                "post_processing/reports/post_processing_report.json": "{}",
                "post_processing/metadata/clip1_metadata_writer_v1.json": "{}",
                "job.log": "log line\n",
            },
            binary_files={"clips/clip1.mp4": b"fake"},
        )
        payload = resolve_job_artifacts("dev", "job_full")
        assert payload is not None
        assert payload["environment"] == "dev"
        assert payload["job_id"] == "job_full"
        assert payload["missing_count"] == 0
        by_type = _by_type(payload)
        for artifact_type in (
            "transcript",
            "raw_candidate_pool",
            "processing_report",
            "selection_result",
            "post_processing_report",
            "clip_metadata",
            "output_clip",
            "job_log",
        ):
            assert artifact_type in by_type
            assert all(item["exists"] for item in by_type[artifact_type])
            for item in by_type[artifact_type]:
                assert item["path"].startswith("jobs/dev/job_full/")
                assert not item["path"].startswith("/")
                assert item["size_bytes"] is not None
                assert item["created_at"] is not None
        assert payload["logs"][0]["path"] == "jobs/dev/job_full/job.log"
        # Stable display order for UI consumers.
        order = [item["artifact_type"] for item in payload["artifacts"]]
        assert order == [
            "transcript",
            "raw_candidate_pool",
            "processing_report",
            "selection_result",
            "post_processing_report",
            "clip_metadata",
            "output_clip",
            "job_log",
        ]

    def test_partially_populated_job(self, env_roots):
        _write_job(
            env_roots,
            job_id="job_partial",
            files={"transcript.json": "{}", "processing_report.json": "{}"},
        )
        payload = resolve_job_artifacts("dev", "job_partial")
        by_type = _by_type(payload)
        assert by_type["transcript"][0]["exists"] is True
        assert by_type["processing_report"][0]["exists"] is True
        assert by_type["raw_candidate_pool"][0]["exists"] is False
        assert by_type["selection_result"][0]["exists"] is False
        assert by_type["post_processing_report"][0]["exists"] is False
        assert by_type["clip_metadata"][0]["exists"] is False
        assert by_type["output_clip"][0]["exists"] is False
        assert by_type["job_log"][0]["exists"] is False
        assert payload["present_count"] == 2
        assert payload["missing_count"] >= 5

    def test_missing_reports_clips_and_logs(self, env_roots):
        _write_job(env_roots, job_id="job_empty")
        payload = resolve_job_artifacts("dev", "job_empty")
        assert payload is not None
        assert payload["present_count"] == 0
        for item in payload["artifacts"]:
            assert item["exists"] is False
            assert item["detail"] in {"not found", "no clip metadata files", "no output clips"}
            assert item["path"].startswith("jobs/dev/job_empty/")
        assert payload["logs"][0]["path"] is None

    def test_failed_and_successful_jobs(self, env_roots):
        _write_job(
            env_roots,
            job_id="job_fail",
            status="failed",
            files={"transcript.json": "{}"},
        )
        _write_job(
            env_roots,
            job_id="job_ok",
            status="completed",
            files={"transcript.json": "{}"},
            binary_files={"clips/out.mp4": b"x"},
        )
        fail_payload = resolve_job_artifacts("dev", "job_fail")
        ok_payload = resolve_job_artifacts("dev", "job_ok")
        assert fail_payload is not None and ok_payload is not None
        assert _by_type(fail_payload)["transcript"][0]["exists"] is True
        assert _by_type(ok_payload)["output_clip"][0]["exists"] is True

    def test_selection_json_fallback(self, env_roots):
        _write_job(
            env_roots,
            job_id="job_sel",
            files={"selection.json": "{}"},
        )
        payload = resolve_job_artifacts("dev", "job_sel")
        sel = _by_type(payload)["selection_result"][0]
        assert sel["exists"] is True
        assert sel["path"] == "jobs/dev/job_sel/selection.json"

    def test_nonexistent_job_returns_none(self, env_roots):
        assert resolve_job_artifacts("dev", "job_missing") is None

    def test_invalid_job_id_and_path_traversal(self, env_roots):
        _write_job(env_roots, job_id="job_safe")
        assert resolve_job_artifacts("dev", "../etc/passwd") is None
        assert resolve_job_artifacts("dev", "job_safe/../job_safe") is None
        assert resolve_job_artifacts("dev", "job_safe/../../prod/secret") is None
        assert resolve_job_artifacts("dev", "") is None

    def test_environment_scoping(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        _write_job(env_roots, job_id="job_dev_only", files={"transcript.json": "{}"})
        prod_jobs = env_roots["root"] / "jobs" / "prod"
        prod_jobs.mkdir(parents=True)
        import observability.index as index_mod
        import observability.artifacts as artifacts_mod

        def _jobs_root(env: str) -> Path:
            token = "prod" if env in {"prod", "production"} else "dev"
            return env_roots["root"] / "jobs" / token

        monkeypatch.setattr(index_mod, "_jobs_root_for", _jobs_root)
        monkeypatch.setattr(artifacts_mod, "_jobs_root_for", _jobs_root)
        assert resolve_job_artifacts("prod", "job_dev_only") is None
        assert resolve_job_artifacts("dev", "job_dev_only") is not None

    def test_no_absolute_paths_exposed(self, env_roots):
        _write_job(
            env_roots,
            job_id="job_paths",
            files={"transcript.json": "{}", "job.log": "x"},
            binary_files={"clips/a.mp4": b"1"},
        )
        payload = resolve_job_artifacts("dev", "job_paths")
        text = json.dumps(payload)
        assert "/home/" not in text
        assert str(env_roots["root"]) not in text
        for item in payload["artifacts"]:
            if item["path"]:
                assert not item["path"].startswith("/")
                assert item["path"].startswith("jobs/dev/")
