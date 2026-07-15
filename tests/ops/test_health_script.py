"""Tests for scripts/ops/health.sh (Remote Operations Prompt 4)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HEALTH_SH = REPO_ROOT / "scripts" / "ops" / "health.sh"


def _run_bash(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(HEALTH_SH), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=merged,
        timeout=120,
    )


class TestHealthScriptInterface:
    def test_help_flags_exit_zero(self):
        for flag in ("--help", "-h"):
            result = _run_bash(flag)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "Exit codes:" in result.stdout

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


class TestHealthScriptOutput:
    def test_dev_health_runs(self):
        result = _run_bash("dev")
        assert result.stdout
        assert "Boot Verification:" in result.stdout
        assert "Boot readiness" in result.stdout
        assert "Remote Operations Health Check" in result.stdout
        assert "Environment: DEVELOPMENT" in result.stdout
        assert "Config validation" in result.stdout
        assert "Overall" in result.stdout
        assert result.returncode in {0, 1, 2}

    def test_prod_health_requires_env_sh_or_fails_cleanly(self):
        """Without a deploy current tree, prod health must fail before write probes."""
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("MK04_")
        }
        env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
        env["MK04_PROD_BASE"] = "/tmp/mk04-health-missing-prod-base-does-not-exist"
        result = _run_bash("prod", env=env)
        combined = result.stdout + result.stderr
        assert result.returncode == 2
        assert "production env.sh missing" in combined or "MK04_RUNTIME_ROOT" in combined
        assert "BEGIN OPENSSH PRIVATE KEY" not in combined

    def test_health_help_documents_exit_codes(self):
        result = _run_bash("--help")
        assert "0  Overall PASS" in result.stdout
        assert "1  Overall WARN" in result.stdout
        assert "2  Overall FAIL" in result.stdout

    def test_no_private_key_markers(self):
        result = _run_bash("dev")
        combined = result.stdout + result.stderr
        assert "BEGIN OPENSSH PRIVATE KEY" not in combined
        assert "BEGIN RSA PRIVATE KEY" not in combined
