"""Tests for bounded observability logs backend (Phase 5)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ops"))

import run_records as rr  # noqa: E402
from logs_report import LogResult, redact_line  # noqa: E402
from observability.logs import (  # noqa: E402
    build_job_logs_payload,
    build_service_logs_payload,
)
from observability.models import LogEntry  # noqa: E402


@pytest.fixture
def env_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rr, "REPO_ROOT", tmp_path)
    import observability.index as index_mod
    import observability.artifacts as artifacts_mod
    import observability.logs as logs_mod

    monkeypatch.setattr(index_mod, "REPO_ROOT", tmp_path)
    jobs_root = tmp_path / "jobs" / "dev"
    jobs_root.mkdir(parents=True)
    monkeypatch.setattr(index_mod, "_jobs_root_for", lambda _env: jobs_root)
    monkeypatch.setattr(artifacts_mod, "_jobs_root_for", lambda _env: jobs_root)
    monkeypatch.setattr(logs_mod, "_jobs_root_for", lambda _env: jobs_root)
    return {"jobs": jobs_root, "root": tmp_path}


def _write_job_with_log(env_roots: dict, *, job_id: str, log_text: str | None) -> None:
    job_dir = env_roots["jobs"] / job_id
    job_dir.mkdir(parents=True)
    report = {
        "job_id": job_id,
        "status": "failed",
        "current_stage": "validation",
        "errors": [],
        "warnings": [],
        "clips": [],
        "execution_context": {"environment": "development"},
    }
    (job_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    if log_text is not None:
        (job_dir / "job.log").write_text(log_text, encoding="utf-8")


class TestSecretRedaction:
    def test_redacts_api_keys_and_tokens(self):
        line = "OPENAI_API_KEY=sk-abc123456789 Authorization: Bearer secret-token password=hunter2"
        redacted = redact_line(line)
        assert "sk-abc123456789" not in redacted
        assert "secret-token" not in redacted
        assert "hunter2" not in redacted
        assert "<redacted>" in redacted


class TestServiceLogs:
    def test_service_modes_return_structured_payload(self, monkeypatch: pytest.MonkeyPatch):
        import observability.logs as logs_mod

        def _fake_state(_env: str):
            return object(), None

        def _fake_fetch(mode, state, token, *, lines):
            return LogResult(
                source=f"journalctl unit mk04-{mode}.service",
                lines=[
                    "2026-07-04T12:00:00+00:00 host svc[1]: INFO started",
                    "2026-07-04T12:00:01+00:00 host svc[1]: ERROR boom OPENAI_API_KEY=sk-secretvalue99",
                ],
            )

        monkeypatch.setattr(logs_mod, "load_state", _fake_state)
        monkeypatch.setattr(logs_mod, "fetch_service_logs", _fake_fetch)

        for mode in ("api", "worker", "ai", "scheduler"):
            payload = build_service_logs_payload("dev", mode, lines=50)
            assert payload["environment"] == "dev"
            assert payload["source"] == mode
            assert payload["status"] == "ok"
            assert payload["limit"] == 50
            assert payload["count"] == 2
            assert payload["entries"][0]["severity"] == "info"
            assert payload["entries"][1]["severity"] == "error"
            assert "sk-secretvalue99" not in payload["entries"][1]["message"]
            assert not str(payload.get("origin") or "").startswith("/")

    def test_errors_mode_uses_error_fetcher(self, monkeypatch: pytest.MonkeyPatch):
        import observability.logs as logs_mod

        monkeypatch.setattr(logs_mod, "load_state", lambda _e: (object(), None))

        def _fake_errors(state, token, *, lines):
            return LogResult(
                source="journalctl",
                lines=["2026-07-04T12:00:00Z host svc: CRITICAL failure"],
            )

        monkeypatch.setattr(logs_mod, "fetch_errors", _fake_errors)
        payload = build_service_logs_payload("dev", "errors", lines=10)
        assert payload["source"] == "errors"
        assert payload["entries"][0]["severity"] == "error"

    def test_empty_and_unavailable(self, monkeypatch: pytest.MonkeyPatch):
        import observability.logs as logs_mod

        monkeypatch.setattr(logs_mod, "load_state", lambda _e: (object(), None))
        monkeypatch.setattr(
            logs_mod,
            "fetch_service_logs",
            lambda *a, **k: LogResult(source="journalctl", lines=[], empty=True),
        )
        empty = build_service_logs_payload("dev", "api")
        assert empty["status"] == "empty"
        assert empty["entries"] == []

        monkeypatch.setattr(
            logs_mod,
            "fetch_service_logs",
            lambda *a, **k: LogResult(source="journalctl", lines=[], unavailable=True),
        )
        missing = build_service_logs_payload("dev", "api")
        assert missing["status"] == "unavailable"

    def test_lines_are_bounded(self, monkeypatch: pytest.MonkeyPatch):
        import observability.logs as logs_mod

        monkeypatch.setattr(logs_mod, "load_state", lambda _e: (object(), None))

        def _fake_fetch(mode, state, token, *, lines):
            return LogResult(
                source="journalctl",
                lines=[f"line {i}" for i in range(lines)],
            )

        monkeypatch.setattr(logs_mod, "fetch_service_logs", _fake_fetch)
        payload = build_service_logs_payload("dev", "worker", lines=5)
        assert payload["limit"] == 5
        assert payload["count"] == 5

        clamped = build_service_logs_payload("dev", "worker", lines=50_000)
        assert clamped["limit"] == 1000
        assert clamped["count"] == 1000


class TestJobLogs:
    def test_job_logs_from_artifact_path(self, env_roots):
        _write_job_with_log(
            env_roots,
            job_id="job_logs",
            log_text=(
                "INFO start\n"
                "ERROR failed password=supersecret\n"
                "done\n"
            ),
        )
        payload = build_job_logs_payload("dev", "job_logs", lines=10)
        assert payload is not None
        assert payload["job_id"] == "job_logs"
        assert payload["status"] == "ok"
        assert payload["count"] == 3
        assert payload["log_reference"]["path"] == "jobs/dev/job_logs/job.log"
        assert "supersecret" not in json.dumps(payload)
        assert payload["entries"][1]["severity"] == "error"

    def test_missing_log_file_is_empty_not_404(self, env_roots):
        _write_job_with_log(env_roots, job_id="job_nolog", log_text=None)
        payload = build_job_logs_payload("dev", "job_nolog")
        assert payload is not None
        assert payload["status"] == "empty"
        assert payload["entries"] == []

    def test_nonexistent_and_invalid_job_ids(self, env_roots):
        assert build_job_logs_payload("dev", "job_missing") is None
        assert build_job_logs_payload("dev", "../etc/passwd") is None
        assert build_job_logs_payload("dev", "job/../x") is None

    def test_environment_scoping(self, env_roots, monkeypatch: pytest.MonkeyPatch):
        _write_job_with_log(env_roots, job_id="job_dev_only", log_text="ok\n")
        import observability.index as index_mod
        import observability.artifacts as artifacts_mod
        import observability.logs as logs_mod

        def _jobs_root(env: str) -> Path:
            token = "prod" if env in {"prod", "production"} else "dev"
            return env_roots["root"] / "jobs" / token

        for mod in (index_mod, artifacts_mod, logs_mod):
            monkeypatch.setattr(mod, "_jobs_root_for", _jobs_root)
        (env_roots["root"] / "jobs" / "prod").mkdir(parents=True, exist_ok=True)
        assert build_job_logs_payload("prod", "job_dev_only") is None
        assert build_job_logs_payload("dev", "job_dev_only") is not None


class TestLogEntryModel:
    def test_round_trip(self):
        entry = LogEntry(
            message="hello",
            source="api",
            timestamp="2026-07-04T00:00:00Z",
            severity="info",
        )
        restored = LogEntry.from_dict(entry.to_dict())
        assert restored == entry
