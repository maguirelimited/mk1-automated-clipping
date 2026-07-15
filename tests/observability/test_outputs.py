"""Tests for output clip indexing (Phase 10)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ops"))

import run_records as rr  # noqa: E402
from observability.outputs import (
    latest_run_id_with_clips,  # noqa: E402
    get_clip_detail,
    get_clip_summary,
    latest_job_id_for_funnel,
    latest_successful_run_id,
    list_clip_summaries,
    list_clips_for_funnel,
    list_clips_for_job,
    list_clips_for_run,
    list_job_ids_for_funnel,
    list_recent_output_clips,
    outputs_list_payload,
    resolve_clip_media_path,
    run_clips_list_payload,
)
from observability.index import _jobs_root_for  # noqa: E402


@pytest.fixture
def env_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)
    import observability.index as index_mod
    import observability.artifacts as artifacts_mod
    import observability.outputs as outputs_mod
    import observability.job_inspector as inspector_mod

    monkeypatch.setattr(index_mod, "REPO_ROOT", tmp_path)
    jobs_root = tmp_path / "jobs" / "dev"
    jobs_root.mkdir(parents=True)
    for mod in (index_mod, artifacts_mod, outputs_mod, inspector_mod):
        monkeypatch.setattr(mod, "_jobs_root_for", lambda _env, root=jobs_root: root)
    return {"jobs": jobs_root, "root": tmp_path}


def _write_job_with_clip(
    env_roots: dict,
    *,
    job_id: str,
    clip_name: str = "clip_a.mp4",
    metadata: dict | None = None,
    missing_file: bool = False,
) -> None:
    job_dir = env_roots["jobs"] / job_id
    job_dir.mkdir(parents=True)
    report = {
        "job_id": job_id,
        "status": "completed",
        "current_stage": "posting",
        "errors": [],
        "warnings": [],
        "clips": [],
        "execution_context": {
            "environment": "development",
            "funnel_id": "business",
            "platform_id": "youtube",
            "preset_id": "growth",
        },
        "funnel": {"funnel_id": "business"},
    }
    (job_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    clips_dir = job_dir / "clips"
    clips_dir.mkdir(exist_ok=True)
    clip_path = clips_dir / clip_name
    if not missing_file:
        clip_path.write_bytes(b"fake-video")
    else:
        # Leave a zero-byte marker path only in artifact resolver via empty clips dir
        # Create then delete to simulate missing after discovery — instead write nothing
        # and put a stale reference by creating file then unlinking after index... 
        # Simpler: create file for resolver exists=True then we test missing via exists flag
        # by not creating file — artifact resolver marks missing placeholder only.
        # For missing file with named path, create artifact by writing then deleting:
        clip_path.write_bytes(b"x")
        clip_path.unlink()

    if metadata is not None:
        meta_dir = job_dir / "post_processing" / "metadata"
        meta_dir.mkdir(parents=True)
        stem = Path(clip_name).stem
        (meta_dir / f"{stem}_metadata_writer_v1.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )


class TestOutputIndexing:
    def test_lists_clips_from_job_artifacts(self, env_roots):
        _write_job_with_clip(
            env_roots,
            job_id="job_out",
            metadata={
                "clip_id": "clip_a",
                "candidate_id": "cand_01",
                "duration_sec": 12.5,
                "validation_result": "passed",
                "warnings": ["soft warning"],
            },
        )
        clips = list_clip_summaries("dev")
        assert len(clips) == 1
        clip = clips[0]
        assert clip.job_id == "job_out"
        assert clip.clip_id == "clip_a"
        assert clip.funnel == "business"
        assert clip.platform == "youtube"
        assert clip.preview_available is True
        assert clip.exists is True
        assert clip.source_candidate == "cand_01"
        assert clip.validation_state == "passed"
        assert clip.duration_seconds == 12.5
        assert "soft warning" in clip.warnings
        assert clip.metadata_reference is not None
        assert clip.metadata_reference.exists is True
        assert clip.output_path and clip.output_path.startswith("jobs/dev/")

    def test_missing_metadata_is_explicit(self, env_roots):
        _write_job_with_clip(env_roots, job_id="job_nometa")
        clip = list_clip_summaries("dev")[0]
        assert clip.metadata_reference is not None
        assert clip.metadata_reference.exists is False
        assert clip.source_candidate is None

    def test_missing_clip_file(self, env_roots):
        # Create job with clip file present for artifact discovery, then remove it
        # after writing — artifact resolver checks filesystem at resolve time.
        job_dir = env_roots["jobs"] / "job_missing_file"
        job_dir.mkdir(parents=True)
        (job_dir / "report.json").write_text(
            json.dumps(
                {
                    "job_id": "job_missing_file",
                    "status": "completed",
                    "current_stage": "posting",
                    "errors": [],
                    "warnings": [],
                    "clips": [],
                    "execution_context": {
                        "environment": "development",
                        "funnel_id": "business",
                        "platform_id": "youtube",
                    },
                }
            ),
            encoding="utf-8",
        )
        clips_dir = job_dir / "clips"
        clips_dir.mkdir()
        # No file — artifact resolver returns missing placeholder only → empty list
        clips = list_clip_summaries("dev", job_id="job_missing_file")
        assert clips == []

    def test_empty_output_list(self, env_roots):
        assert list_clip_summaries("dev") == []
        payload = outputs_list_payload("dev")
        assert payload["count"] == 0
        assert payload["outputs"] == []

    def test_clip_detail_and_media_path(self, env_roots):
        _write_job_with_clip(
            env_roots,
            job_id="job_detail",
            metadata={"clip_id": "clip_a", "candidate_id": "c1", "validation_result": "passed"},
        )
        detail = get_clip_detail("dev", "job_detail", "clip_a")
        assert detail is not None
        assert detail["clip"]["clip_id"] == "clip_a"
        assert detail["metadata_summary"]["available"] is True
        assert detail["related_job_path"] == "/ops/jobs/job_detail"
        assert detail["media_path"] == "/ops/outputs/job_detail/clip_a/media"
        media = resolve_clip_media_path("dev", "job_detail", "clip_a")
        assert media is not None
        assert media.is_file()

    def test_invalid_ids(self, env_roots):
        assert get_clip_summary("dev", "../x", "y") is None
        assert get_clip_detail("dev", "job", "../x") is None
        assert resolve_clip_media_path("dev", "job", "nope") is None

    def test_report_json_clip_fallback(self, env_roots):
        job_dir = env_roots["jobs"] / "job_report_only"
        job_dir.mkdir(parents=True)
        clip_name = "input_source_clip_01_ab12cd34.mp4"
        clip_path = env_roots["root"] / "video-automation" / "output" / clip_name
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"fake-video")
        report = {
            "job_id": "job_report_only",
            "status": "success",
            "current_stage": "success",
            "errors": [],
            "warnings": [],
            "clips": [
                {
                    "clip_id": "job_report_only_clip_01",
                    "clip_file": clip_name,
                    "clip_path": str(clip_path),
                    "duration_sec": 18.2,
                    "validation_result": "passed",
                }
            ],
            "execution_context": {
                "environment": "development",
                "funnel_id": "business",
                "platform_id": "youtube",
            },
        }
        (job_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")

        clips = list_clip_summaries("dev", job_id="job_report_only")
        assert len(clips) == 1
        assert clips[0].clip_id == "job_report_only_clip_01"
        assert clips[0].preview_available is True

        media = resolve_clip_media_path("dev", "job_report_only", "job_report_only_clip_01")
        assert media is not None
        assert media.is_file()

        detail = get_clip_detail("dev", "job_report_only", "job_report_only_clip_01")
        assert detail is not None
        assert detail["media_path"] == "/ops/outputs/job_report_only/job_report_only_clip_01/media"

    def test_recent_run_limit_filters_older_jobs(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        runs_root = env_roots["root"] / "runs" / "dev"
        runs_root.mkdir(parents=True)
        recent_run = runs_root / "run_recent"
        recent_run.mkdir()
        (recent_run / "run_record.json").write_text(
            json.dumps(
                {
                    "run_id": "run_recent",
                    "environment": "dev",
                    "trigger": "manual_cli",
                    "status": "SUCCESS",
                    "started_at": "2026-07-04T20:00:00Z",
                    "finished_at": "2026-07-04T20:01:00Z",
                    "log_path": str(recent_run / "run.log"),
                }
            ),
            encoding="utf-8",
        )
        older_run = runs_root / "run_old"
        older_run.mkdir()
        (older_run / "run_record.json").write_text(
            json.dumps(
                {
                    "run_id": "run_old",
                    "environment": "dev",
                    "trigger": "manual_cli",
                    "status": "SUCCESS",
                    "started_at": "2026-07-03T10:00:00Z",
                    "finished_at": "2026-07-03T10:01:00Z",
                    "log_path": str(older_run / "run.log"),
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(rr, "REPO_ROOT", env_roots["root"])

        _write_job_with_clip(env_roots, job_id="job_recent", clip_name="recent.mp4")
        recent_report_path = env_roots["jobs"] / "job_recent" / "report.json"
        recent_report = json.loads(recent_report_path.read_text(encoding="utf-8"))
        recent_report["completed_at"] = "2026-07-04T20:05:00Z"
        recent_report_path.write_text(json.dumps(recent_report), encoding="utf-8")

        _write_job_with_clip(env_roots, job_id="job_old", clip_name="old.mp4")
        old_report_path = env_roots["jobs"] / "job_old" / "report.json"
        old_report = json.loads(old_report_path.read_text(encoding="utf-8"))
        old_report["completed_at"] = "2026-07-03T12:00:00Z"
        old_report_path.write_text(json.dumps(old_report), encoding="utf-8")

        recent_only = list_clip_summaries("dev", recent_run_limit=1)
        assert [clip.job_id for clip in recent_only] == ["job_recent"]

        all_clips = list_clip_summaries("dev", recent_run_limit=None)
        assert {clip.job_id for clip in all_clips} == {"job_recent", "job_old"}

    def test_recent_run_limit_handles_duplicate_job_dirs(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        runs_root = env_roots["root"] / "runs" / "dev"
        runs_root.mkdir(parents=True)
        recent_run = runs_root / "run_recent"
        recent_run.mkdir()
        (recent_run / "run_record.json").write_text(
            json.dumps(
                {
                    "run_id": "run_recent",
                    "environment": "dev",
                    "trigger": "operations_ui",
                    "status": "SUCCESS",
                    "started_at": "2026-07-05T00:01:49Z",
                    "finished_at": "2026-07-05T00:02:00Z",
                    "log_path": str(recent_run / "run.log"),
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(rr, "REPO_ROOT", env_roots["root"])

        job_id = "job_dup_recent"
        stub_dir = env_roots["jobs"] / job_id
        stub_dir.mkdir(parents=True)
        (stub_dir / "report.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "status": "running",
                    "current_stage": "transcribe",
                    "clips": [],
                    "created_at": "2026-07-05T00:02:08Z",
                }
            ),
            encoding="utf-8",
        )
        legacy_dir = env_roots["jobs"] / f"input_source_{job_id}"
        legacy_dir.mkdir(parents=True)
        clip_name = "recent.mp4"
        (legacy_dir / "clips").mkdir(parents=True)
        (legacy_dir / "clips" / clip_name).write_bytes(b"fake-video")
        (legacy_dir / "report.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "status": "success",
                    "current_stage": "success",
                    "created_at": "2026-07-05T00:02:08Z",
                    "completed_at": "2026-07-05T00:11:24Z",
                    "clips": [
                        {
                            "clip_id": f"{job_id}_clip_01",
                            "clip_file": clip_name,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        recent_only = list_clip_summaries("dev", recent_run_limit=1)
        assert [clip.job_id for clip in recent_only] == [job_id]


def _write_run_record(
    root: Path,
    *,
    run_id: str,
    status: str,
    started_at: str = "2026-07-04T20:00:00Z",
) -> None:
    runs_root = root / "runs" / "dev"
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_record.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "environment": "dev",
                "trigger": "manual_cli",
                "status": status,
                "started_at": started_at,
                "finished_at": started_at,
                "log_path": str(run_dir / "run.log"),
            }
        ),
        encoding="utf-8",
    )


def _write_report_clip_job(
    env_roots: dict,
    *,
    job_id: str,
    run_id: str,
    clip_name: str = "clip_a.mp4",
    clip_id: str | None = None,
    title: str | None = None,
    hook: str | None = None,
    composite_score: float | None = None,
    duration_sec: float = 12.0,
) -> None:
    job_dir = env_roots["jobs"] / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = job_dir / "clips"
    clips_dir.mkdir(exist_ok=True)
    (clips_dir / clip_name).write_bytes(b"fake-video")
    resolved_clip_id = clip_id or f"{job_id}_clip_01"
    clip_entry: dict = {
        "clip_id": resolved_clip_id,
        "clip_file": clip_name,
        "clip_path": str(clips_dir / clip_name),
        "duration_sec": duration_sec,
    }
    if title is not None:
        clip_entry["title"] = title
    if hook is not None:
        clip_entry["hook"] = hook
    if composite_score is not None:
        clip_entry["composite_score"] = composite_score
    report = {
        "job_id": job_id,
        "status": "success",
        "current_stage": "success",
        "clips": [clip_entry],
        "execution_context": {
            "environment": "development",
            "funnel_id": "business",
            "platform_id": "youtube",
            "run_id": run_id,
        },
        "completed_at": "2026-07-04T20:05:00Z",
    }
    (job_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")


class TestRunScopedOutputs:
    def test_latest_successful_run_skips_non_success(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rr, "REPO_ROOT", env_roots["root"])
        _write_run_record(env_roots["root"], run_id="run_running", status="RUNNING")
        _write_run_record(env_roots["root"], run_id="run_failed", status="FAIL")
        _write_run_record(env_roots["root"], run_id="run_skipped", status="SKIPPED")
        _write_run_record(env_roots["root"], run_id="run_success", status="SUCCESS")

        assert latest_successful_run_id("dev") == "run_success"

    def test_latest_successful_run_none_when_missing(self, env_roots):
        assert latest_successful_run_id("dev") is None

    def test_latest_successful_run_none_when_only_non_success(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rr, "REPO_ROOT", env_roots["root"])
        _write_run_record(env_roots["root"], run_id="run_fail", status="FAIL")
        assert latest_successful_run_id("dev") is None

    def test_latest_run_id_with_clips_skips_empty_newer_runs(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        from types import SimpleNamespace
        from observability import outputs as outputs_mod

        summaries = [
            SimpleNamespace(run_id="run_new_empty", status="FAIL"),
            SimpleNamespace(run_id="run_with_clips", status="FAIL"),
            SimpleNamespace(run_id="run_success", status="SUCCESS"),
        ]
        monkeypatch.setattr(outputs_mod, "list_run_summaries", lambda token, limit=20: summaries)
        monkeypatch.setattr(
            outputs_mod,
            "list_clips_for_run",
            lambda token, run_id, **kwargs: ([{"clip_id": "c1"}] if run_id == "run_with_clips" else []),
        )
        assert latest_run_id_with_clips("dev") == "run_with_clips"


    def test_list_clips_for_run_exact_match(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rr, "REPO_ROOT", env_roots["root"])
        _write_report_clip_job(env_roots, job_id="job_a", run_id="run_a", title="Clip A")
        _write_report_clip_job(env_roots, job_id="job_b", run_id="run_b", title="Clip B")

        clips = list_clips_for_run("dev", "run_a")
        assert len(clips) == 1
        assert clips[0]["job_id"] == "job_a"
        assert clips[0]["run_id"] == "run_a"
        assert clips[0]["title_or_hook"] == "Clip A"

    def test_list_clips_for_run_multiple_jobs(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rr, "REPO_ROOT", env_roots["root"])
        _write_report_clip_job(env_roots, job_id="job_one", run_id="run_multi", title="One")
        _write_report_clip_job(env_roots, job_id="job_two", run_id="run_multi", title="Two")

        clips = list_clips_for_run("dev", "run_multi")
        assert {row["job_id"] for row in clips} == {"job_one", "job_two"}
        assert all(row["run_id"] == "run_multi" for row in clips)

    def test_list_clips_for_run_empty_when_no_clips(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rr, "REPO_ROOT", env_roots["root"])
        job_dir = env_roots["jobs"] / "job_empty"
        job_dir.mkdir(parents=True)
        (job_dir / "report.json").write_text(
            json.dumps(
                {
                    "job_id": "job_empty",
                    "status": "success",
                    "clips": [],
                    "execution_context": {"run_id": "run_empty"},
                }
            ),
            encoding="utf-8",
        )
        assert list_clips_for_run("dev", "run_empty") == []

    def test_list_clips_for_run_excludes_other_runs(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rr, "REPO_ROOT", env_roots["root"])
        _write_report_clip_job(env_roots, job_id="job_keep", run_id="run_keep")
        _write_report_clip_job(env_roots, job_id="job_other", run_id="run_other")

        clips = list_clips_for_run("dev", "run_keep")
        assert [row["job_id"] for row in clips] == ["job_keep"]

    def test_score_enrichment_from_report(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rr, "REPO_ROOT", env_roots["root"])
        _write_report_clip_job(
            env_roots,
            job_id="job_scored",
            run_id="run_scored",
            composite_score=8.4,
        )
        clips = list_clips_for_run("dev", "run_scored")
        assert clips[0]["score"] == 8.4

    def test_missing_title_and_score_do_not_fail(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rr, "REPO_ROOT", env_roots["root"])
        _write_report_clip_job(env_roots, job_id="job_sparse", run_id="run_sparse")
        clips = list_clips_for_run("dev", "run_sparse")
        assert clips[0]["clip_id"]
        assert "title_or_hook" not in clips[0]
        assert "score" not in clips[0]

    def test_hook_used_when_title_missing(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rr, "REPO_ROOT", env_roots["root"])
        _write_report_clip_job(
            env_roots,
            job_id="job_hook",
            run_id="run_hook",
            hook="Stop scrolling",
        )
        clips = list_clips_for_run("dev", "run_hook")
        assert clips[0]["title_or_hook"] == "Stop scrolling"

    def test_run_clips_payload_shape(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(rr, "REPO_ROOT", env_roots["root"])
        _write_report_clip_job(env_roots, job_id="job_payload", run_id="run_payload")
        payload = run_clips_list_payload("dev", "run_payload")
        assert payload["run_id"] == "run_payload"
        assert payload["count"] == 1
        assert payload["clips"][0]["media_path"].endswith("/media")

    def test_existing_list_clip_summaries_unchanged(self, env_roots):
        _write_job_with_clip(env_roots, job_id="job_legacy")
        assert len(list_clip_summaries("dev")) == 1
        assert outputs_list_payload("dev")["count"] == 1

    def test_list_clips_for_run_matches_clipping_job_from_run_log(
        self, env_roots, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(rr, "REPO_ROOT", env_roots["root"])
        _write_run_record(env_roots["root"], run_id="run_logged", status="SUCCESS")
        run_dir = env_roots["root"] / "runs" / "dev" / "run_logged"
        (run_dir / "run.log").write_text(
            'response HTTP 200: {"clipping_job":{"job_id":"job_logged","status":"queued"}}\n',
            encoding="utf-8",
        )
        _write_report_clip_job(env_roots, job_id="job_logged", run_id="", title="From log")

        clips = list_clips_for_run("dev", "run_logged")
        assert len(clips) == 1
        assert clips[0]["job_id"] == "job_logged"
        assert clips[0]["title_or_hook"] == "From log"

    def test_clips_for_job_prefers_report_entries_over_post_processing_artifacts(
        self, env_roots
    ):
        job_id = "job_primary"
        job_dir = env_roots["jobs"] / job_id
        job_dir.mkdir(parents=True)
        primary = job_dir / "clips" / "primary_clip.mp4"
        primary.parent.mkdir(parents=True)
        primary.write_bytes(b"primary")
        post = job_dir / "post_processing" / "clips" / "post_clip.mp4"
        post.parent.mkdir(parents=True)
        post.write_bytes(b"post")
        report = {
            "job_id": job_id,
            "status": "success",
            "clips": [
                {
                    "clip_id": "primary_clip",
                    "clip_file": "primary_clip.mp4",
                    "duration_sec": 9.0,
                }
            ],
            "execution_context": {"funnel_id": "business", "platform_id": "youtube"},
        }
        (job_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")

        clips = list_clip_summaries("dev", job_id=job_id)
        assert len(clips) == 1
        assert clips[0].clip_id == "primary_clip"
        assert clips[0].output_path.endswith("clips/primary_clip.mp4")


class TestJobsRootResolution:
    def test_jobs_root_uses_runtime_video_automation_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import observability.index as index_mod

        runtime = tmp_path / "varlib" / "dev"
        (runtime / "video-automation" / "jobs").mkdir(parents=True)
        monkeypatch.setenv("MK04_RUNTIME_ROOT", str(runtime))
        # Avoid monkeypatching _jobs_root_for — exercise ConfigManager override path.
        if hasattr(index_mod, "_jobs_root_for"):
            root = index_mod._jobs_root_for("dev")
            assert root == (runtime / "video-automation" / "jobs").resolve()


class TestJobScopedOutputListing:
    def test_list_clips_for_job_legacy_folder_name(self, env_roots: dict) -> None:
        job_id = "job_20260707T234602Z_93e141c8"
        job_dir = env_roots["jobs"] / f"gta_source_{job_id}"
        job_dir.mkdir(parents=True)
        clip_name = "gta-clips-001_clip_01_933713b0.mp4"
        (job_dir / "clips").mkdir()
        (job_dir / "clips" / clip_name).write_bytes(b"fake-video")
        report = {
            "job_id": job_id,
            "status": "success",
            "clips": [
                {
                    "clip_id": f"{job_id}_clip_01",
                    "clip_file": clip_name,
                    "duration_sec": 30.0,
                }
            ],
            "funnel": {"funnel_id": "gta_clips_001", "funnel_name": "GTA Clips 001"},
        }
        (job_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")

        rows = list_clips_for_job("dev", job_id)
        assert len(rows) == 1
        assert rows[0]["job_id"] == job_id
        assert rows[0]["clip_id"] == f"{job_id}_clip_01"
        assert rows[0]["funnel"] == "gta_clips_001"

    def test_list_recent_output_clips_without_run(self, env_roots: dict) -> None:
        _write_job_with_clip(env_roots, job_id="job_recent", clip_name="recent.mp4")
        rows = list_recent_output_clips("dev", limit=5)
        assert len(rows) == 1
        assert rows[0]["job_id"] == "job_recent"


class TestFunnelScopedOutputListing:
    def test_list_job_ids_for_funnel_matches_report_funnel(self, env_roots: dict) -> None:
        _write_job_with_clip(env_roots, job_id="job_gta", clip_name="gta.mp4")
        report_path = env_roots["jobs"] / "job_gta" / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["funnel"] = {"funnel_id": "gta_clips_002"}
        report["execution_context"]["funnel_id"] = "gta_clips_002"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        _write_job_with_clip(env_roots, job_id="job_other", clip_name="other.mp4")

        assert list_job_ids_for_funnel("dev", "gta_clips_002") == ["job_gta"]
        assert latest_job_id_for_funnel("dev", "gta_clips_002") == "job_gta"

    def test_list_clips_for_funnel_aggregates_jobs(self, env_roots: dict) -> None:
        for job_id in ("job_gta_a", "job_gta_b"):
            _write_job_with_clip(env_roots, job_id=job_id, clip_name=f"{job_id}.mp4")
            report_path = env_roots["jobs"] / job_id / "report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["funnel"] = {"funnel_id": "gta_clips_002"}
            report["execution_context"]["funnel_id"] = "gta_clips_002"
            report_path.write_text(json.dumps(report), encoding="utf-8")

        rows = list_clips_for_funnel("dev", "gta_clips_002", job_limit=5)
        assert len(rows) == 2
        assert {row["job_id"] for row in rows} == {"job_gta_a", "job_gta_b"}
        assert all(row["funnel"] == "gta_clips_002" for row in rows)
