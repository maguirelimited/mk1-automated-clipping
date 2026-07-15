"""Smoke tests for Configuration & Deployment end-to-end validation (Prompt 9)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "smoke" / "smoke_config_deployment.py"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "config"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "smoke"))
sys.path.insert(0, str(REPO_ROOT / "ops-ui"))

import smoke_config_deployment as smoke  # noqa: E402


def _python() -> str:
    venv = REPO_ROOT / "video-automation" / ".venv" / "bin" / "python"
    return str(venv) if venv.is_file() else sys.executable


def _copy_config_tree(dest_repo: Path) -> None:
    shutil.copytree(REPO_ROOT / "config", dest_repo / "config", dirs_exist_ok=True)


class TestSmokeScriptCLI:
    def test_dev_smoke_passes_in_temp_repo(self, tmp_path: Path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        _copy_config_tree(repo)
        code = smoke.main(["--env", "dev", "--repo-root", str(repo), "--skip-shell"])
        out = capsys.readouterr().out
        assert code == 0
        assert "CONFIG_DEPLOYMENT_SMOKE_PASSED" in out
        jobs = list((repo / "jobs" / "dev").glob("smoke_config_dev_*"))
        assert jobs

    def test_prod_smoke_passes_local_safe(self, tmp_path: Path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        _copy_config_tree(repo)
        code = smoke.main(["--env", "prod", "--repo-root", str(repo), "--skip-shell"])
        out = capsys.readouterr().out
        assert code == 0
        assert "CONFIG_DEPLOYMENT_SMOKE_PASSED" in out

    def test_both_runs_dev_and_prod(self, tmp_path: Path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        _copy_config_tree(repo)
        code = smoke.main(["--both", "--repo-root", str(repo), "--skip-shell"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Dev:" in out
        assert "Prod:" in out
        assert "Isolation:" in out

    def test_failure_prints_failed_banner(self, tmp_path: Path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        _copy_config_tree(repo)
        with mock.patch.object(smoke, "run_environment_smoke") as mocked:
            mocked.return_value = smoke.EnvSmokeResult(
                mk04_env="dev",
                canonical_env="development",
                job_id="x",
                checks=[smoke.CheckResult("broken", False, "simulated")],
            )
            code = smoke.main(["--env", "dev", "--repo-root", str(repo), "--skip-shell"])
        out = capsys.readouterr().out
        assert code == 1
        assert "CONFIG_DEPLOYMENT_SMOKE_FAILED" in out

    def test_default_runs_dev_only(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _copy_config_tree(repo)
        with mock.patch.object(smoke, "run_environment_smoke") as mocked:
            mocked.return_value = smoke.EnvSmokeResult(
                mk04_env="dev",
                canonical_env="development",
                job_id="x",
                checks=[smoke.CheckResult("ok", True)],
            )
            with mock.patch.object(smoke, "run_invalid_production_config_smoke", return_value=[]):
                smoke.main(["--repo-root", str(repo), "--skip-shell"])
        assert mocked.call_count == 1
        assert mocked.call_args.kwargs.get("mk04_env") == "dev" or mocked.call_args[0][0] == "dev"


class TestIsolation:
    def test_dev_smoke_not_under_prod_roots(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _copy_config_tree(repo)
        dev_result = smoke.run_environment_smoke(
            "dev",
            repo_root=repo,
            config_root=repo / "config",
            skip_shell=True,
            job_id="smoke_config_dev_test001",
        )
        _, dev_state = smoke._load_config("dev", repo / "config")
        _, prod_state = smoke._load_config("prod", repo / "config")
        checks = smoke.verify_dev_did_not_touch_prod(
            dev_result.job_id,
            dev_state,
            prod_state,
        )
        assert all(c.passed for c in checks)

    def test_prod_smoke_not_under_dev_roots(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _copy_config_tree(repo)
        prod_result = smoke.run_environment_smoke(
            "prod",
            repo_root=repo,
            config_root=repo / "config",
            skip_shell=True,
            job_id="smoke_config_prod_test001",
        )
        _, dev_state = smoke._load_config("dev", repo / "config")
        _, prod_state = smoke._load_config("prod", repo / "config")
        checks = smoke.verify_prod_did_not_touch_dev(
            prod_result.job_id,
            dev_state,
            prod_state,
        )
        assert all(c.passed for c in checks)

    def test_dev_prod_database_paths_differ(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _copy_config_tree(repo)
        _, dev_state = smoke._load_config("dev", repo / "config")
        _, prod_state = smoke._load_config("prod", repo / "config")
        assert dev_state.database_path != prod_state.database_path
        assert dev_state.outputs_root != prod_state.outputs_root


class TestInvalidConfig:
    def test_synthetic_invalid_prod_config_fails(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _copy_config_tree(repo)
        checks = smoke.run_invalid_production_config_smoke(repo)
        assert any(c.name == "invalid prod ConfigManager fails" and c.passed for c in checks)
        assert any(c.name == "invalid prod config validator fails" and c.passed for c in checks)

    def test_invalid_config_does_not_create_real_job_dir(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _copy_config_tree(repo)
        before = set((repo / "jobs" / "prod").glob("*")) if (repo / "jobs" / "prod").exists() else set()
        smoke.run_invalid_production_config_smoke(repo)
        after = set((repo / "jobs" / "prod").glob("*")) if (repo / "jobs" / "prod").exists() else set()
        assert before == after


class TestSnapshotContext:
    def test_snapshot_and_context_saved(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _copy_config_tree(repo)
        result = smoke.run_environment_smoke(
            "dev",
            repo_root=repo,
            config_root=repo / "config",
            skip_shell=True,
            job_id="smoke_config_dev_ctx001",
        )
        assert result.job_dir is not None
        checks = smoke._verify_snapshot_and_context(
            job_dir=result.job_dir,
            job_id=result.job_id,
            canonical_env="development",
            config=smoke._load_config("dev", repo / "config")[0],
        )
        assert all(c.passed for c in checks)

    def test_no_secrets_in_snapshot(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _copy_config_tree(repo)
        result = smoke.run_environment_smoke(
            "dev",
            repo_root=repo,
            config_root=repo / "config",
            skip_shell=True,
            job_id="smoke_config_dev_sec001",
        )
        snap = (result.job_dir / "resolved_config.yaml").read_text(encoding="utf-8").lower()
        assert "api_key:" not in snap
        assert "password:" not in snap


class TestUIUpdateRunSmoke:
    def test_ui_summary_development(self):
        checks = smoke._verify_ui_summary("dev", REPO_ROOT)
        assert any(c.name == "Ops UI environment label" and c.passed for c in checks)

    def test_ui_summary_production(self):
        checks = smoke._verify_ui_summary("prod", REPO_ROOT)
        assert any(c.name == "Ops UI environment label" and c.passed for c in checks)

    def test_update_dev_check_only(self):
        checks = smoke._verify_update_check_only("dev", REPO_ROOT)
        assert all(c.passed for c in checks)

    def test_update_prod_check_only(self):
        checks = smoke._verify_update_check_only("prod", REPO_ROOT)
        assert all(c.passed for c in checks)

    def test_run_check_only_no_hang(self):
        checks = smoke._verify_run_check_only("dev", REPO_ROOT)
        assert all(c.passed for c in checks)

    def test_run_sh_check_only_prod(self):
        env = os.environ.copy()
        env["MK04_SKIP_PROD_PREFLIGHT"] = "1"
        proc = subprocess.run(
            ["bash", str(REPO_ROOT / "run.sh"), "--env", "prod", "--check-only"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert proc.returncode == 0
        assert "check-only" in (proc.stdout + proc.stderr).lower()


class TestSmokeScriptSubprocess:
    def test_cli_dev_subprocess(self):
        proc = subprocess.run(
            [_python(), str(SMOKE_SCRIPT), "--env", "dev", "--skip-shell"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "CONFIG_DEPLOYMENT_SMOKE_PASSED" in proc.stdout
