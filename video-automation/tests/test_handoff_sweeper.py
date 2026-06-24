"""Unit tests for the handoff retry sweeper.

The sweeper is the safety net for the one-shot
``_try_output_funnel_handoff`` in ``server/app.py``. It walks
``jobs/<...>/report.json``, decides per-report whether to retry, and records
the attempt in-place. These tests use a fake ``post_fn`` so nothing hits a
real output-funnel.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import handoff_sweeper  # noqa: E402


def _write_report(jobs_dir: Path, job_id: str, payload: dict[str, Any]) -> Path:
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    report_path = job_dir / "report.json"
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return report_path


def _make_jobs_dir(tmp_path: Path) -> Path:
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    return jobs


def _success_payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "status": "success",
        "clips": [{"clip_id": "c1"}],
        "output_funnel_handoff": {"enabled": True, "ok": False, "error": "timeout"},
    }
    base.update(overrides)
    return base


def test_sweeper_retries_failed_handoff_and_records_success(tmp_path: Path):
    jobs = _make_jobs_dir(tmp_path)
    report_path = _write_report(jobs, "job_a", _success_payload())

    calls: list[dict[str, Any]] = []

    def fake_post(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "status_code": 200, "response": {"success": True}}

    summary = handoff_sweeper.sweep(
        jobs_dir=jobs,
        url="http://example.invalid:5055",
        post_fn=fake_post,
    )

    assert summary["scanned"] == 1
    assert summary["eligible"] == 1
    assert summary["succeeded"] == 1
    assert summary["failed"] == 0
    assert len(calls) == 1

    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["output_funnel_handoff"]["ok"] is True
    assert written["output_funnel_handoff"]["status_code"] == 200
    retries = written["output_funnel_handoff"]["retries"]
    assert len(retries) == 1
    assert retries[0]["by"] == "handoff_sweeper"
    assert retries[0]["ok"] is True


def test_sweeper_skips_already_ok_handoff(tmp_path: Path):
    jobs = _make_jobs_dir(tmp_path)
    _write_report(
        jobs,
        "job_b",
        _success_payload(output_funnel_handoff={"enabled": True, "ok": True}),
    )

    def fake_post(**_kwargs):
        raise AssertionError("post_fn should not be called for already-ok handoff")

    summary = handoff_sweeper.sweep(
        jobs_dir=jobs,
        url="http://example.invalid:5055",
        post_fn=fake_post,
    )

    assert summary["eligible"] == 0
    assert summary["succeeded"] == 0
    assert any(s["reason"] == "already_ok" for s in summary["skipped"])


def test_sweeper_skips_non_success_jobs(tmp_path: Path):
    jobs = _make_jobs_dir(tmp_path)
    _write_report(jobs, "job_running", {"status": "running"})
    _write_report(jobs, "job_failed", {"status": "failed"})

    def fake_post(**_kwargs):
        raise AssertionError("non-success reports must not be retried")

    summary = handoff_sweeper.sweep(
        jobs_dir=jobs,
        url="http://example.invalid:5055",
        post_fn=fake_post,
    )

    assert summary["eligible"] == 0
    reasons = {s["reason"] for s in summary["skipped"]}
    assert "not_success" in reasons


def test_sweeper_stops_after_max_attempts(tmp_path: Path):
    jobs = _make_jobs_dir(tmp_path)
    history = [{"by": "handoff_sweeper", "ok": False, "at": "x"} for _ in range(5)]
    _write_report(
        jobs,
        "job_burned",
        _success_payload(
            output_funnel_handoff={
                "enabled": True,
                "ok": False,
                "retries": history,
            }
        ),
    )

    def fake_post(**_kwargs):
        raise AssertionError("should not retry past max_attempts")

    summary = handoff_sweeper.sweep(
        jobs_dir=jobs,
        url="http://example.invalid:5055",
        max_attempts=5,
        post_fn=fake_post,
    )

    assert summary["eligible"] == 0
    assert any(s["reason"] == "max_attempts_reached" for s in summary["skipped"])


def test_sweeper_records_failure_and_grows_retries_list(tmp_path: Path):
    jobs = _make_jobs_dir(tmp_path)
    report_path = _write_report(jobs, "job_c", _success_payload())

    def fake_post(**_kwargs):
        return {"ok": False, "status_code": 503, "error": "service_unavailable"}

    summary = handoff_sweeper.sweep(
        jobs_dir=jobs,
        url="http://example.invalid:5055",
        post_fn=fake_post,
    )

    assert summary["succeeded"] == 0
    assert summary["failed"] == 1
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["output_funnel_handoff"]["ok"] is False
    assert written["output_funnel_handoff"]["status_code"] == 503
    assert len(written["output_funnel_handoff"]["retries"]) == 1


def test_sweeper_skips_old_reports(tmp_path: Path):
    jobs = _make_jobs_dir(tmp_path)
    report_path = _write_report(jobs, "job_old", _success_payload())
    old_mtime = time.time() - (25 * 3600)
    os.utime(report_path, (old_mtime, old_mtime))

    def fake_post(**_kwargs):
        raise AssertionError("must not retry ancient reports")

    summary = handoff_sweeper.sweep(
        jobs_dir=jobs,
        url="http://example.invalid:5055",
        max_age_hours=24,
        post_fn=fake_post,
    )

    assert summary["eligible"] == 0
    assert any(s["reason"] == "too_old" for s in summary["skipped"])


def test_sweeper_dry_run_does_not_call_post_or_write(tmp_path: Path):
    jobs = _make_jobs_dir(tmp_path)
    report_path = _write_report(jobs, "job_d", _success_payload())
    original_bytes = report_path.read_bytes()

    def fake_post(**_kwargs):
        raise AssertionError("dry-run must not POST")

    summary = handoff_sweeper.sweep(
        jobs_dir=jobs,
        url="http://example.invalid:5055",
        dry_run=True,
        post_fn=fake_post,
    )

    assert summary["eligible"] == 1
    assert summary["succeeded"] == 0
    assert summary["failed"] == 0
    assert report_path.read_bytes() == original_bytes
    assert any(r.get("would_retry") for r in summary["results"])


def test_sweeper_limit_caps_eligible_retries(tmp_path: Path):
    jobs = _make_jobs_dir(tmp_path)
    for i in range(5):
        _write_report(jobs, f"job_{i:02d}", _success_payload())

    def fake_post(**_kwargs):
        return {"ok": True, "status_code": 200}

    summary = handoff_sweeper.sweep(
        jobs_dir=jobs,
        url="http://example.invalid:5055",
        limit=2,
        post_fn=fake_post,
    )

    assert summary["eligible"] == 2
    assert summary["succeeded"] == 2


def test_sweeper_handles_missing_handoff_field_as_eligible(tmp_path: Path):
    jobs = _make_jobs_dir(tmp_path)
    report_path = _write_report(jobs, "job_nohandoff", {"status": "success"})

    def fake_post(**_kwargs):
        return {"ok": True, "status_code": 200}

    summary = handoff_sweeper.sweep(
        jobs_dir=jobs,
        url="http://example.invalid:5055",
        post_fn=fake_post,
    )

    assert summary["eligible"] == 1
    assert summary["succeeded"] == 1
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["output_funnel_handoff"]["ok"] is True
