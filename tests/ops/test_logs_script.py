"""Tests for scripts/ops/logs.sh and logs_report.py (Remote Operations Prompt 5)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOGS_SH = REPO_ROOT / "scripts" / "ops" / "logs.sh"


def _run_bash(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(LOGS_SH), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=merged,
        timeout=120,
    )


class TestLogsScriptInterface:
    def test_help_flags_exit_zero(self):
        for flag in ("--help", "-h"):
            result = _run_bash(flag)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "Usage:" in result.stdout

    def test_missing_args_fails(self):
        result = _run_bash()
        assert result.returncode != 0

    def test_invalid_env_fails(self):
        result = _run_bash("staging", "api")
        assert result.returncode != 0

    def test_invalid_mode_fails(self):
        result = _run_bash("dev", "unknown-mode")
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "invalid mode" in combined.lower()


class TestLogsScriptOutput:
    def test_dev_api_runs(self):
        result = _run_bash("dev", "api")
        assert "Remote Operations Logs" in result.stdout
        assert "Environment: DEVELOPMENT" in result.stdout
        assert "Mode: api" in result.stdout
        assert result.returncode in {0, 1}

    def test_dev_errors_runs(self):
        result = _run_bash("dev", "errors")
        assert "Mode: errors" in result.stdout
        assert result.returncode in {0, 1}

    def test_lines_option(self):
        result = _run_bash("dev", "errors", "--lines", "50")
        assert "Lines: 50" in result.stdout

    def test_lines_clamped_message(self):
        result = _run_bash("dev", "errors", "--lines", "5000")
        assert "Lines: 1000" in result.stdout
        assert "clamped" in result.stderr.lower()

    def test_no_private_key_markers(self):
        result = _run_bash("dev", "worker")
        combined = result.stdout + result.stderr
        assert "BEGIN OPENSSH PRIVATE KEY" not in combined


class TestLogsRedaction:
    def test_redact_secrets(self):
        import sys

        ops_dir = str(REPO_ROOT / "scripts" / "ops")
        if ops_dir not in sys.path:
            sys.path.insert(0, ops_dir)
        from logs_report import redact_line

        assert "<redacted>" in redact_line("OPENAI_API_KEY=sk-abc123secret")
        assert "<redacted>" in redact_line("Authorization: Bearer abc.def.ghi")
        assert "<redacted>" in redact_line("password=hunter2")
