"""Tests for scripts/ops/status.sh (Remote Operations Prompt 3)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STATUS_SH = REPO_ROOT / "scripts" / "ops" / "status.sh"


def _run_bash(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(STATUS_SH), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=merged,
        timeout=120,
    )


class TestStatusScriptInterface:
    def test_help_flags_exit_zero(self):
        for flag in ("--help", "-h"):
            result = _run_bash(flag)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "Usage:" in result.stdout

    def test_missing_env_fails(self):
        result = _run_bash()
        assert result.returncode != 0
        combined = (result.stdout + result.stderr).lower()
        assert "usage" in combined or "required" in combined

    def test_invalid_env_fails(self):
        result = _run_bash("staging")
        assert result.returncode != 0
        combined = (result.stdout + result.stderr).lower()
        assert "invalid environment" in combined or "expected dev or prod" in combined


class TestStatusScriptOutput:
    def test_dev_status_runs_read_only(self):
        result = _run_bash("dev")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Remote Operations Status" in result.stdout
        assert "Environment:        DEVELOPMENT" in result.stdout
        assert "Boot readiness:" in result.stdout
        assert "Overall status:" in result.stdout

    def test_prod_status_runs_read_only(self):
        result = _run_bash("prod")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Environment:        PRODUCTION" in result.stdout
        assert "Queue pending:" in result.stdout
        assert "not yet available" in result.stdout

    def test_status_does_not_print_secrets_markers(self):
        result = _run_bash("dev")
        combined = result.stdout + result.stderr
        assert "BEGIN OPENSSH PRIVATE KEY" not in combined
        assert "BEGIN RSA PRIVATE KEY" not in combined
