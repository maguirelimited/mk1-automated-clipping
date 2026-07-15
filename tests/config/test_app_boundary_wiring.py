"""
tests/config/test_app_boundary_wiring.py

Application-boundary wiring for ConfigManager / ExecutionContext in
video-automation/server/app.py.

Covers:
  - Job creation persists execution_context.json + resolved_config.yaml
  - Provenance (environment / funnel / job_id)
  - Production fail-closed when config cannot be resolved
  - Snapshot load order / malformed / missing-prod / legacy-dev
  - Recovery preserves existing snapshots
  - Canonical job-creation routes use the persistence helpers
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_CONFIG = REPO_ROOT / "scripts" / "config"
if str(SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_CONFIG))

from config_manager import ConfigManager  # noqa: E402
from execution_context import load_resolved_config_for_job  # noqa: E402


def _import_server_app():
    import importlib.util as _ilu

    cached = sys.modules.get("app")
    if cached is not None and hasattr(cached, "_save_execution_context"):
        return cached

    spec = _ilu.spec_from_file_location(
        "app",
        str(REPO_ROOT / "video-automation" / "server" / "app.py"),
    )
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["app"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _make_pipeline_job(tmp_path: Path) -> dict[str, str]:
    job_dir = tmp_path / "job"
    clips_dir = job_dir / "clips"
    clips_dir.mkdir(parents=True)
    return {
        "job_id": "job_test",
        "job_dir": str(job_dir),
        "clips_dir": str(clips_dir),
        "report_path": str(job_dir / "report.json"),
        "review_path": str(job_dir / "review.md"),
        "analytics_path": str(job_dir / "analytics.json"),
        "normalized_transcript_path": str(job_dir / "transcript_payload.json"),
    }


def _write_flat_resolved(job_dir: Path) -> None:
    cfg = {
        "version": 1,
        "selection": {
            "mode": "balanced",
            "max_clips": 4,
            "min_overall_potential": 7.0,
            "min_confidence": 0.6,
            "exploration_ratio": 0.15,
        },
        "post_processing": {"conveyor": ["render_clip_v1", "validation_v1"]},
    }
    (job_dir / "resolved_config.yaml").write_text(yaml.dump(cfg), encoding="utf-8")


class TestJobArtifactPersistence:
    def test_context_backed_job_writes_both_artifacts(self, tmp_path: Path) -> None:
        app_mod = _import_server_app()
        resolved = ConfigManager.load(environment="dev")
        job_dir = tmp_path / "job_artifacts"
        job_dir.mkdir()
        ctx = app_mod._save_execution_context(
            resolved, "job_20260101T120000Z_abc12345", str(job_dir)
        )
        assert (job_dir / "execution_context.json").is_file()
        assert (job_dir / "resolved_config.yaml").is_file()
        assert ctx["environment"] == "development"
        assert ctx["job_id"] == "job_20260101T120000Z_abc12345"
        assert ctx["funnel_id"] == "business"

    def test_artifacts_contain_environment_funnel_provenance(
        self, tmp_path: Path
    ) -> None:
        app_mod = _import_server_app()
        resolved = ConfigManager.load(
            environment="dev", funnel_id="business", platform_id="youtube"
        )
        job_dir = tmp_path / "job_prov"
        job_dir.mkdir()
        ctx = app_mod._save_execution_context(
            resolved, "job_20260101T120000Z_prov0001", str(job_dir)
        )
        disk = json.loads((job_dir / "execution_context.json").read_text(encoding="utf-8"))
        snap = yaml.safe_load((job_dir / "resolved_config.yaml").read_text(encoding="utf-8"))
        assert disk["environment"] == "development"
        assert disk["funnel_id"] == "business"
        assert disk["job_id"] == "job_20260101T120000Z_prov0001"
        assert snap["snapshot_meta"]["environment"] == "development"
        assert snap["snapshot_meta"]["funnel_id"] == "business"
        assert ctx["platform_id"] == "youtube"


class TestSecretSafeStartupLogging:
    def test_startup_log_omits_secret_like_values(self, capsys: pytest.CaptureFixture[str]) -> None:
        app_mod = _import_server_app()
        ctx = {
            "environment": "development",
            "job_id": "job_secret_safe",
            "funnel_id": "business",
            "platform_id": "youtube",
            "preset_id": "balanced",
            "config_version": "1",
            "resolved_config_path": "/jobs/dev/job/resolved_config.yaml",
            "code_commit": "deadbeef",
            "api_token": "SUPER_SECRET_TOKEN_VALUE",
        }
        app_mod._log_pipeline_start(
            job_id="job_secret_safe",
            funnel_id="business",
            execution_context=ctx,
        )
        out = capsys.readouterr().out + capsys.readouterr().err
        assert "job_secret_safe" in out
        assert "development" in out
        assert "SUPER_SECRET_TOKEN_VALUE" not in out
        assert "resolved_config.yaml" not in out
        assert "api_token" not in out


class TestProductionJobCreationGuard:
    def test_production_load_raises_when_config_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app_mod = _import_server_app()
        import config_manager as _cm

        def _bad_load(*_a, **_kw):
            raise _cm.ConfigError("Simulated config unavailable")

        monkeypatch.setenv("MK04_ENV", "prod")
        monkeypatch.setattr(_cm.ConfigManager, "load", _bad_load)
        with pytest.raises(RuntimeError, match="Unable to create production job"):
            app_mod._load_env_config_for_job(funnel_id="business")


class TestResolvedConfigPipelineBoundary:
    def test_valid_snapshot_loaded_before_pipeline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app_mod = _import_server_app()
        job = _make_pipeline_job(tmp_path)
        _write_flat_resolved(Path(job["job_dir"]))
        (Path(job["job_dir"]) / "execution_context.json").write_text(
            json.dumps(
                {
                    "environment": "development",
                    "job_id": "job_test",
                    "funnel_id": "business",
                    "platform_id": "youtube",
                    "preset_id": "balanced",
                    "config_version": "1",
                    "resolved_config_path": str(Path(job["job_dir"]) / "resolved_config.yaml"),
                    "code_commit": None,
                }
            ),
            encoding="utf-8",
        )

        seen: dict[str, Any] = {}

        def _capture_pipeline(**kwargs: Any):
            seen["called"] = True
            seen["execution_context"] = kwargs.get("execution_context")
            return types.SimpleNamespace(
                raw_candidate_pool_path="/tmp/pool.json",
                processing_report_path="/tmp/proc_report.json",
                sections_analysed=1,
                usable_sections=1,
                rejected_sections=0,
                failed_sections_count=0,
                final_candidate_count=0,
                duplicates_removed=0,
            )

        monkeypatch.setattr(app_mod, "run_processing_pipeline", _capture_pipeline)
        monkeypatch.setattr(
            app_mod.mk1_settings, "resolve_post_processing_enabled", lambda: False
        )

        report: dict[str, Any] = {
            "job_id": "job_test",
            "funnel": {"funnel_id": "business"},
            "clips": [],
            "warnings": [],
        }
        with app_mod.app.app_context():
            resp = app_mod._run_mk1_pipeline_after_transcript(
                report=report,
                job=job,
                jid="job_test",
                warnings=[],
                stage_ms={},
                total_started=0.0,
                video_path=str(tmp_path / "source.mp4"),
                transcript_path=str(tmp_path / "transcript.json"),
                transcript_payload={"segments": []},
                funnel_id="business",
                output_root=str(tmp_path / "output"),
                filename="source",
                filename_prefix="biz",
                delivery_mode="pull_from_output_endpoint",
                input_id="input_1",
                audit_plain={},
            )
        assert resp.get_json()["success"] is True
        assert seen.get("called") is True
        assert seen["execution_context"]["environment"] == "development"
        assert report["execution_context"]["funnel_id"] == "business"

    def test_missing_production_snapshot_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app_mod = _import_server_app()
        job = _make_pipeline_job(tmp_path)
        (Path(job["job_dir"]) / "execution_context.json").write_text(
            json.dumps(
                {
                    "environment": "production",
                    "job_id": "job_test",
                    "funnel_id": "business",
                    "platform_id": "youtube",
                    "preset_id": "balanced",
                    "config_version": "1",
                    "resolved_config_path": "",
                    "code_commit": None,
                }
            ),
            encoding="utf-8",
        )
        pipeline_called = {"value": False}

        def _should_not_run(**_kwargs: Any) -> None:
            pipeline_called["value"] = True
            raise AssertionError("run_processing_pipeline should not be called")

        monkeypatch.setattr(app_mod, "run_processing_pipeline", _should_not_run)
        report: dict[str, Any] = {
            "job_id": "job_test",
            "mk04_environment": "prod",
            "funnel": {"funnel_id": "business"},
            "clips": [],
            "warnings": [],
        }
        with app_mod.app.app_context():
            resp = app_mod._run_mk1_pipeline_after_transcript(
                report=report,
                job=job,
                jid="job_test",
                warnings=[],
                stage_ms={},
                total_started=0.0,
                video_path=str(tmp_path / "source.mp4"),
                transcript_path=str(tmp_path / "transcript.json"),
                transcript_payload={"segments": []},
                funnel_id="business",
                output_root=str(tmp_path / "output"),
                filename="source",
                filename_prefix="biz",
                delivery_mode="pull_from_output_endpoint",
                input_id="input_1",
                audit_plain={},
            )
        if isinstance(resp, tuple):
            resp, status_code = resp
        else:
            status_code = resp.status_code
        data = resp.get_json()
        assert status_code == 500
        assert data["success"] is False
        assert pipeline_called["value"] is False
        assert report["status"] == "failed"
        assert report["errors"][0]["category"] == "configuration_error"

    def test_legacy_dev_job_uses_compatibility_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        app_mod = _import_server_app()
        job = _make_pipeline_job(tmp_path)
        # No resolved_config.yaml and no execution_context.json — legacy.
        monkeypatch.setenv("MK04_ENV", "dev")

        def _ok_pipeline(**_kwargs: Any):
            return types.SimpleNamespace(
                raw_candidate_pool_path="/tmp/pool.json",
                processing_report_path="/tmp/proc_report.json",
                sections_analysed=0,
                usable_sections=0,
                rejected_sections=0,
                failed_sections_count=0,
                final_candidate_count=0,
                duplicates_removed=0,
            )

        monkeypatch.setattr(app_mod, "run_processing_pipeline", _ok_pipeline)
        monkeypatch.setattr(
            app_mod.mk1_settings, "resolve_post_processing_enabled", lambda: False
        )
        report: dict[str, Any] = {
            "job_id": "job_test",
            "funnel": {"funnel_id": "business"},
            "clips": [],
            "warnings": [],
        }
        with app_mod.app.app_context():
            resp = app_mod._run_mk1_pipeline_after_transcript(
                report=report,
                job=job,
                jid="job_test",
                warnings=[],
                stage_ms={},
                total_started=0.0,
                video_path=str(tmp_path / "source.mp4"),
                transcript_path=str(tmp_path / "transcript.json"),
                transcript_payload={"segments": []},
                funnel_id="business",
                output_root=str(tmp_path / "output"),
                filename="source",
                filename_prefix="biz",
                delivery_mode="pull_from_output_endpoint",
                input_id="input_1",
                audit_plain={},
            )
        assert resp.get_json()["success"] is True
        out = capsys.readouterr().out
        assert "legacy" in out.lower()
        assert report.get("status") != "failed"


class TestRecoveryPreservesSnapshot:
    def test_recovery_does_not_rewrite_snapshot(self, tmp_path: Path) -> None:
        app_mod = _import_server_app()
        jobs_root = tmp_path / "jobs"
        job_id = "job_20260101T120000Z_recover1"
        job_dir = jobs_root / f"source_{job_id}"
        clips = job_dir / "clips"
        clips.mkdir(parents=True)
        original_yaml = "version: 1\nselection:\n  max_clips: 3\n"
        original_ctx = {
            "environment": "development",
            "job_id": job_id,
            "funnel_id": "business",
            "platform_id": "youtube",
            "preset_id": "balanced",
            "config_version": "1",
            "resolved_config_path": str(job_dir / "resolved_config.yaml"),
            "code_commit": "abc1234",
        }
        (job_dir / "resolved_config.yaml").write_text(original_yaml, encoding="utf-8")
        (job_dir / "execution_context.json").write_text(
            json.dumps(original_ctx), encoding="utf-8"
        )
        video = tmp_path / "recover.mp4"
        video.write_bytes(b"fake")
        job = {
            "job_id": job_id,
            "job_dir": str(job_dir),
            "clips_dir": str(clips),
            "report_path": str(job_dir / "report.json"),
            "review_path": str(job_dir / "review.md"),
            "task_path": str(job_dir / "task.json"),
            "analytics_path": str(job_dir / "analytics.json"),
            "selection_path": str(job_dir / "selection.json"),
            "input_copy_path": str(job_dir / "input_recover.mp4"),
        }
        report = {
            "job_id": job_id,
            "status": "queued",
            "current_stage": "queued",
            "created_at": "2026-01-01T12:00:00Z",
            "errors": [],
            "warnings": [],
            "clips": [],
        }
        (job_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (job_dir / "review.md").write_text("# review\n", encoding="utf-8")
        task = {
            "job_id": job_id,
            "job": job,
            "video_path": str(video),
            "policy_bundle": {"selection": {}, "policy_audit": {}},
            "created_at": "2026-01-01T12:00:00Z",
            "execution_context": original_ctx,
        }
        (job_dir / "task.json").write_text(json.dumps(task), encoding="utf-8")

        # Point job iteration at tmp jobs root.
        app_mod._JOB_RECOVERY_DONE = False
        while True:
            try:
                app_mod._JOB_QUEUE.get_nowait()
                app_mod._JOB_QUEUE.task_done()
            except Exception:
                break

        def _iter():
            yield str(job_dir), str(job_dir / "report.json"), report

        # Patch iteration and ensure recovery requeues without rewriting artifacts.
        original_iter = app_mod._iter_job_reports
        app_mod._iter_job_reports = _iter  # type: ignore[assignment]
        try:
            count = app_mod._recover_pending_jobs_once()
        finally:
            app_mod._iter_job_reports = original_iter  # type: ignore[assignment]

        assert count == 1
        assert (job_dir / "resolved_config.yaml").read_text(encoding="utf-8") == original_yaml
        assert json.loads((job_dir / "execution_context.json").read_text(encoding="utf-8")) == original_ctx
        # Sanity: loader still sees the original snapshot.
        loaded = load_resolved_config_for_job(job_dir)
        assert loaded is not None
        assert loaded["selection"]["max_clips"] == 3


class TestCanonicalJobCreationBoundary:
    def test_create_job_from_payload_calls_save_helpers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app_mod = _import_server_app()
        jobs = tmp_path / "jobs"
        jobs.mkdir()
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        video = input_dir / "clip.mp4"
        video.write_bytes(b"fake")

        cfg_path = tmp_path / "pipeline_config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "paths": {
                        "input_folder": str(input_dir),
                        "output_folder": str(tmp_path / "output"),
                        "temp_folder": str(tmp_path / "temp"),
                        "jobs_folder": str(jobs),
                        "analytics_folder": str(tmp_path / "analytics"),
                    },
                    "selection": {},
                    "models": {},
                    "chunking": {},
                    "async_worker": {
                        "enabled": True,
                        "max_concurrent_jobs": 1,
                        "job_store_type": "json",
                    },
                }
            ),
            encoding="utf-8",
        )
        for name in ("output", "temp", "analytics"):
            (tmp_path / name).mkdir(exist_ok=True)

        monkeypatch.setenv("PIPELINE_CONFIG_PATH", str(cfg_path))
        monkeypatch.setenv("MK04_ALLOW_UNGATED_JOBS", "1")
        monkeypatch.setenv("MK04_TEST_MODE", "1")
        monkeypatch.setenv("MK04_ENV", "dev")

        calls: dict[str, Any] = {"load": 0, "save": 0}

        real_load = app_mod._load_env_config_for_job
        real_save = app_mod._save_execution_context

        def _wrap_load(**kwargs):
            calls["load"] += 1
            return real_load(**kwargs)

        def _wrap_save(resolved, job_id, job_dir):
            calls["save"] += 1
            return real_save(resolved, job_id, job_dir)

        monkeypatch.setattr(app_mod, "_load_env_config_for_job", _wrap_load)
        monkeypatch.setattr(app_mod, "_save_execution_context", _wrap_save)
        monkeypatch.setattr(app_mod, "_ensure_job_workers_started", lambda **_k: None)
        monkeypatch.setattr(app_mod, "_enqueue_job", lambda *_a, **_k: None)

        with app_mod.app.test_request_context(
            "/jobs",
            method="POST",
            json={"video_path": str(video), "funnel_id": "business"},
        ):
            resp = app_mod._create_job_from_payload(
                {"video_path": str(video), "funnel_id": "business"}
            )
        if isinstance(resp, tuple):
            body, status = resp
        else:
            body, status = resp, resp.status_code
        data = body.get_json()
        assert status == 202
        assert data["success"] is True
        assert calls["load"] == 1
        assert calls["save"] == 1
        job_id = data["job_id"]
        # Find the job directory that was created.
        matches = list(jobs.glob(f"*_{job_id}"))
        assert len(matches) == 1
        job_dir = matches[0]
        assert (job_dir / "execution_context.json").is_file()
        assert (job_dir / "resolved_config.yaml").is_file()
        ctx = json.loads((job_dir / "execution_context.json").read_text(encoding="utf-8"))
        assert ctx["environment"] == "development"
        assert ctx["funnel_id"] == "business"

    def test_process_and_process_inline_routes_use_create_job_boundary(self) -> None:
        """Source of truth: /process and /process-inline call _create_job_from_payload."""
        app_mod = _import_server_app()
        import inspect

        process_src = inspect.getsource(app_mod.process)
        inline_src = inspect.getsource(app_mod.process_inline)
        jobs_src = inspect.getsource(app_mod.create_job)
        assert "_create_job_from_payload" in process_src
        assert "_create_job_from_payload" in inline_src
        assert "_create_job_from_payload" in jobs_src
        create_src = inspect.getsource(app_mod._create_job_from_payload)
        assert "_save_execution_context" in create_src
        assert "_load_env_config_for_job" in create_src


class TestMalformedNotTreatedAsLegacy:
    def test_malformed_is_not_legacy_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Malformed file must raise/fail — never follow the missing-legacy path."""
        app_mod = _import_server_app()
        job = _make_pipeline_job(tmp_path)
        (Path(job["job_dir"]) / "resolved_config.yaml").write_text(
            "key: [unclosed", encoding="utf-8"
        )
        monkeypatch.setenv("MK04_ENV", "dev")
        pipeline_called = {"value": False}

        def _should_not_run(**_kwargs: Any) -> None:
            pipeline_called["value"] = True
            raise AssertionError("should not run")

        monkeypatch.setattr(app_mod, "run_processing_pipeline", _should_not_run)
        report: dict[str, Any] = {
            "job_id": "job_test",
            "funnel": {"funnel_id": "business"},
            "clips": [],
            "warnings": [],
        }
        with app_mod.app.app_context():
            resp = app_mod._run_mk1_pipeline_after_transcript(
                report=report,
                job=job,
                jid="job_test",
                warnings=[],
                stage_ms={},
                total_started=0.0,
                video_path=str(tmp_path / "source.mp4"),
                transcript_path=str(tmp_path / "transcript.json"),
                transcript_payload={"segments": []},
                funnel_id="business",
                output_root=str(tmp_path / "output"),
                filename="source",
                filename_prefix="biz",
                delivery_mode="pull_from_output_endpoint",
                input_id="input_1",
                audit_plain={},
            )
        if isinstance(resp, tuple):
            _body, status = resp
        else:
            status = resp.status_code
        assert status == 500
        assert pipeline_called["value"] is False
        assert report["errors"][0]["category"] == "configuration_error"
        assert "invalid" in report["errors"][0]["message"].lower()
