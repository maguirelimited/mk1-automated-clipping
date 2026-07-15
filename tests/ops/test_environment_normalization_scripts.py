"""Tests for environment-required deploy helper scripts."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHDOG = REPO_ROOT / "deploy" / "scripts" / "watchdog.sh"
RETENTION_SWEEPER = REPO_ROOT / "deploy" / "scripts" / "retention-sweeper.sh"
RUN_FUNNEL_DAILY = REPO_ROOT / "deploy" / "scripts" / "run-funnel-daily.sh"
ENV_SH = REPO_ROOT / "deploy" / "scripts" / "env.sh"
COMMON_SH = REPO_ROOT / "scripts" / "ops" / "lib" / "common.sh"
SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"


def _run(script: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


class TestHelperScriptsRequireEnv:
    def test_watchdog_refuses_missing_env(self):
        result = _run(WATCHDOG)
        assert result.returncode != 0
        combined = (result.stdout + result.stderr).lower()
        assert "environment" in combined
        assert "prod" not in combined or "expected" in combined or "required" in combined

    def test_retention_sweeper_refuses_missing_env(self):
        result = _run(RETENTION_SWEEPER)
        assert result.returncode != 0
        combined = (result.stdout + result.stderr).lower()
        assert "environment" in combined

    def test_run_funnel_daily_refuses_missing_env(self):
        result = _run(RUN_FUNNEL_DAILY)
        assert result.returncode != 0
        combined = (result.stdout + result.stderr).lower()
        assert "environment" in combined or "usage" in combined

    def test_run_funnel_daily_refuses_env_without_funnel(self):
        result = _run(RUN_FUNNEL_DAILY, "dev")
        assert result.returncode != 0

    def test_scripts_do_not_default_to_prod(self):
        for script in (WATCHDOG, RETENTION_SWEEPER, RUN_FUNNEL_DAILY):
            text = script.read_text(encoding="utf-8")
            assert "MK04_ENV:-prod" not in text
            assert '${MK04_ENV:-prod}' not in text
            assert '"${1:-${MK04_ENV:-prod}}"' not in text


class TestShellNormalizers:
    def test_env_sh_normalizer_aliases(self):
        result = subprocess.run(
            [
                "bash",
                "-c",
                (
                    f'eval "$(sed -n "/^normalize_mk04_runtime_env()/,/^}}/p" "{ENV_SH}")"; '
                    "normalize_mk04_runtime_env development; "
                    "normalize_mk04_runtime_env production"
                ),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert result.stdout.splitlines() == ["dev", "prod"]

        bad = subprocess.run(
            [
                "bash",
                "-c",
                (
                    f'eval "$(sed -n "/^normalize_mk04_runtime_env()/,/^}}/p" "{ENV_SH}")"; '
                    "normalize_mk04_runtime_env staging"
                ),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert bad.returncode != 0
        assert "Invalid" in bad.stderr

    def test_common_sh_aliases(self):
        result = subprocess.run(
            [
                "bash",
                "-c",
                (
                    f'source "{COMMON_SH}"; '
                    "normalize_ops_env development; "
                    "normalize_ops_env production"
                ),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert result.stdout.splitlines() == ["dev", "prod"]

        bad = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{COMMON_SH}"; normalize_ops_env staging',
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert bad.returncode != 0
        assert "Invalid" in bad.stderr


class TestSystemdStillExplicitProd:
    def test_systemd_units_pass_prod_explicitly(self):
        units = list(SYSTEMD_DIR.glob("mk04-*.service"))
        assert units, "expected systemd unit files"
        for unit in units:
            text = unit.read_text(encoding="utf-8")
            assert "Environment=MK04_ENV=prod" in text
            assert " prod" in text or "prod\n" in text or ".sh prod" in text

    def test_systemd_primary_env_mandatory_overrides_optional(self):
        units = list(SYSTEMD_DIR.glob("mk04-*.service"))
        assert len(units) == 5
        for unit in units:
            text = unit.read_text(encoding="utf-8")
            assert "EnvironmentFile=/etc/mk04/prod/env" in text
            assert "EnvironmentFile=-/etc/mk04/prod/env" not in text
            assert re.search(
                r"(?m)^EnvironmentFile=-/etc/mk04/prod/services/[A-Za-z0-9_.-]+\.env\s*$",
                text,
            ), unit.name
