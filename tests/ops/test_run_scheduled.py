"""Tests for thin scheduled trigger (Reliability Phase 9)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"
RUN_SCHEDULED = OPS_DIR / "run-scheduled.sh"
RUN_FUNNEL_DAILY = REPO_ROOT / "deploy" / "scripts" / "run-funnel-daily.sh"
CRONTAB = REPO_ROOT / "deploy" / "cron" / "mk04.crontab"
CRON_D = REPO_ROOT / "deploy" / "cron" / "mk04.cron.d"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=120,
    )


class TestRunScheduledInterface:
    def test_help(self):
        result = _run(RUN_SCHEDULED, "--help")
        assert result.returncode == 0
        assert "run-pipeline.sh" in result.stdout
        assert "--trigger scheduled" in result.stdout

    def test_missing_args_fails(self):
        assert _run(RUN_SCHEDULED).returncode == 2
        assert _run(RUN_SCHEDULED, "dev").returncode == 2

    def test_invokes_shared_entrypoint_with_scheduled_trigger(self):
        # NOT READY on this host is fine; we only assert the entrypoint path.
        result = _run(RUN_SCHEDULED, "dev", "mfm_business_ai_001")
        # Exit 4 = not ready, or 0/1/5 depending on host — never usage error.
        assert result.returncode in {0, 1, 3, 4, 5}
        combined = result.stdout + result.stderr
        assert "scheduled trigger" in combined
        # run-pipeline prints status/record lines or an Error from readiness/lock.
        assert (
            "run_id=" in combined
            or "Boot readiness" in combined
            or "boot readiness" in combined.lower()
            or "NOT READY" in combined
            or "status=" in combined
        )


class TestLegacyDailyWrapper:
    def test_daily_delegates_to_run_scheduled(self):
        text = RUN_FUNNEL_DAILY.read_text(encoding="utf-8")
        assert "scripts/ops/run-scheduled.sh" in text
        assert "run-pipeline" not in text or "run-scheduled" in text


class TestCronConfiguration:
    def test_crontab_uses_run_scheduled(self):
        text = CRONTAB.read_text(encoding="utf-8")
        assert "scripts/ops/run-scheduled.sh" in text
        # Pipeline line must not call /run-funnel or Python app modules.
        pipeline_lines = [
            line
            for line in text.splitlines()
            if "run-scheduled.sh" in line and not line.strip().startswith("#")
        ]
        assert pipeline_lines
        for line in pipeline_lines:
            assert "/run-funnel" not in line
            assert "section_candidate" not in line
            assert "run-funnel-daily.sh" not in line

    def test_cron_d_uses_run_scheduled(self):
        text = CRON_D.read_text(encoding="utf-8")
        assert "scripts/ops/run-scheduled.sh" in text
        assert "mk04" in text

    def test_no_duplicate_pipeline_path_in_scheduler_scripts(self):
        scheduled = RUN_SCHEDULED.read_text(encoding="utf-8")
        assert "run-pipeline.sh" in scheduled
        assert "exec" in scheduled
        assert "--trigger scheduled" in scheduled
        # Must not embed HTTP client / pipeline implementation.
        assert "curl" not in scheduled
        assert "urllib" not in scheduled
        assert "invoke_run_funnel" not in scheduled
