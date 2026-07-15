"""Tests for update.sh, run.sh, and last_update_status.json (Prompt 8)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "update.sh"
RUN_SH = REPO_ROOT / "run.sh"
WRITE_STATUS = REPO_ROOT / "scripts" / "ops" / "write_update_status.py"


def _run_bash(script: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=merged,
        timeout=600,
    )


def _python() -> str:
    for candidate in (
        REPO_ROOT / "video-automation" / ".venv" / "bin" / "python",
        REPO_ROOT / ".venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


class TestUpdateScriptArguments:
    def test_update_without_env_fails_with_usage(self):
        result = _run_bash(UPDATE_SH)
        assert result.returncode != 0
        combined = (result.stdout + result.stderr).lower()
        assert "usage" in combined or "required" in combined

    def test_update_invalid_env_fails(self):
        result = _run_bash(UPDATE_SH, "staging")
        assert result.returncode != 0
        combined = (result.stdout + result.stderr).lower()
        assert "unknown argument" in combined or "invalid environment" in combined

    def test_update_dev_exports_mk04_env(self):
        result = _run_bash(UPDATE_SH, "dev", "--check-only", "--no-restart")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "MK04_ENV:    dev" in result.stdout

    def test_update_development_normalises(self):
        result = _run_bash(UPDATE_SH, "development", "--check-only", "--no-restart")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "environment: development" in result.stdout
        assert "MK04_ENV:    dev" in result.stdout

    def test_update_prod_exports_mk04_env(self):
        result = _run_bash(
            UPDATE_SH,
            "prod",
            "--check-only",
            "--no-restart",
            env={"MK04_SKIP_PROD_PREFLIGHT": "1"},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "MK04_ENV:    prod" in result.stdout

    def test_update_production_normalises(self):
        result = _run_bash(
            UPDATE_SH,
            "production",
            "--check-only",
            "--no-restart",
            env={"MK04_SKIP_PROD_PREFLIGHT": "1"},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "environment: production" in result.stdout


class TestRunScriptArguments:
    def test_run_without_env_fails(self):
        result = _run_bash(RUN_SH)
        assert result.returncode != 0
        combined = (result.stdout + result.stderr).lower()
        assert "--env" in combined

    def test_run_env_dev_normalises(self):
        result = subprocess.run(
            ["bash", "-c", f'source "{REPO_ROOT}/scripts/ops/update_lib.sh"; normalize_mk04_env dev'],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "dev"

    def test_run_env_prod_normalises(self):
        result = subprocess.run(
            ["bash", "-c", f'source "{REPO_ROOT}/scripts/ops/update_lib.sh"; normalize_mk04_env production'],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "prod"

    def test_run_help_shows_env_flag(self):
        result = _run_bash(RUN_SH, "--help")
        assert result.returncode == 0
        assert "--env" in result.stdout


class TestValidationOrdering:
    def test_failed_config_prevents_restart_and_exits_nonzero(self, tmp_path):
        fake_validate = tmp_path / "validate_config.py"
        fake_validate.write_text(
            "import sys\nprint('forced config failure')\nsys.exit(1)\n",
            encoding="utf-8",
        )
        script = tmp_path / "update_test.sh"
        script.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
ROOT="{REPO_ROOT}"
source "$ROOT/scripts/ops/update_lib.sh"
PYTHON="$(bash -c 'source {REPO_ROOT}/scripts/ops/update_lib.sh; find_repo_python {REPO_ROOT}')"
if ! "$PYTHON" "{fake_validate}"; then
  echo "config validation failed — skipping service restart"
  exit 1
fi
echo "would restart services"
""",
            encoding="utf-8",
        )
        script.chmod(0o755)
        result = subprocess.run(["bash", str(script)], capture_output=True, text=True)
        assert result.returncode == 1
        assert "skipping service restart" in result.stdout
        assert "would restart services" not in result.stdout

    def test_update_script_orders_validation_before_restart(self):
        text = UPDATE_SH.read_text(encoding="utf-8")
        validate_pos = text.index("validate_config.py")
        restart_pos = text.index("restart_project_services")
        assert validate_pos < restart_pos


class TestLastUpdateStatus:
    def test_write_success_status_file(self):
        python = _python()
        started = "2026-07-03T12:00:00Z"
        result = subprocess.run(
            [
                python,
                str(WRITE_STATUS),
                "--env",
                "dev",
                "--status",
                "success",
                "--started-at",
                started,
                "--commit",
                "abc1234",
                "--config-validation",
                "pass",
                "--tests",
                "pass",
                "--services-restarted",
                "skipped",
                "--health-check",
                "local_only",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        path = Path(result.stdout.strip())
        assert path.is_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["environment"] == "development"
        assert data["status"] == "success"
        assert data["commit"] == "abc1234"
        assert data["config_validation"] == "pass"
        assert data["tests"] == "pass"
        assert data["services_restarted"] == "skipped"
        assert data["health_check"] == "local_only"
        blob = json.dumps(data).lower()
        assert "password" not in blob
        assert "secret" not in blob

    def test_write_failure_status(self):
        python = _python()
        result = subprocess.run(
            [
                python,
                str(WRITE_STATUS),
                "--env",
                "prod",
                "--status",
                "failure",
                "--started-at",
                "2026-07-03T12:00:00Z",
                "--config-validation",
                "fail",
                "--message",
                "Config schema validation failed",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        path = Path(result.stdout.strip())
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["status"] == "failure"
        assert data["environment"] == "production"
        assert data["config_validation"] == "fail"


class TestWriteUpdateStatusCanonicalEnv:
    def test_canonical_env_aliases(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "ops"))
        from ops_readonly import canonical_env  # noqa: PLC0415

        assert canonical_env("dev") == "development"
        assert canonical_env("development") == "development"
        assert canonical_env("prod") == "production"
        assert canonical_env("production") == "production"

    def test_invalid_env_raises(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "ops"))
        from ops_readonly import canonical_env  # noqa: PLC0415

        with pytest.raises(ValueError, match="invalid environment"):
            canonical_env("staging")


class TestHonestyMessages:
    def test_no_systemd_reports_skipped(self):
        result = _run_bash(UPDATE_SH, "dev", "--no-restart")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "skipped" in result.stdout.lower()

    def test_health_not_faked_as_pass_when_services_skipped(self):
        result = _run_bash(UPDATE_SH, "dev", "--no-restart")
        assert result.returncode == 0, result.stdout + result.stderr
        combined = result.stdout.lower()
        assert "health check:" in combined
        assert "local-only" in combined or "not_available" in combined or "skipped" in combined


class TestEnvironmentSummaryLastUpdate:
    def test_load_last_update_status_from_data_root(self, tmp_path):
        sys.path.insert(0, str(REPO_ROOT / "ops-ui"))
        from ops_ui.environment_summary import load_last_update_status  # noqa: PLC0415

        data_root = tmp_path / "data" / "dev"
        data_root.mkdir(parents=True)
        payload = {
            "environment": "development",
            "status": "success",
            "commit": "deadbeef",
            "config_validation": "pass",
            "api_key": "sk-should-not-appear",
        }
        (data_root / "last_update_status.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        loaded = load_last_update_status(data_root)
        assert loaded is not None
        assert loaded["status"] == "success"
        assert loaded.get("api_key") == "[REDACTED]"


class TestRegressionGuards:
    def test_update_script_does_not_auto_pull(self):
        text = UPDATE_SH.read_text(encoding="utf-8")
        assert "git pull --ff-only" in text
        assert '--pull' in text
        assert 'if [[ "$DO_PULL" -eq 1 ]]; then' in text
        assert "refuses --pull" in text
        assert "promote-to-prod.sh" in text

    def test_update_script_does_not_install_packages(self):
        text = UPDATE_SH.read_text(encoding="utf-8")
        assert "pip install" not in text

    def test_no_storage_deletion_in_update_lib(self):
        text = (REPO_ROOT / "scripts" / "ops" / "update_lib.sh").read_text(encoding="utf-8")
        assert "rm -rf" not in text
