"""Hermetic regressions for production health path authority and scheduler semantics."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"
HEALTH_SH = OPS_DIR / "health.sh"
ENV_SH = REPO_ROOT / "deploy" / "scripts" / "env.sh"

sys.path.insert(0, str(OPS_DIR))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "config"))


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


def _make_prod_layout(tmp: Path) -> dict[str, Path]:
    base = tmp / "opt" / "mk04" / "prod"
    release = base / "releases" / "relA_active"
    scripts = release / "deploy" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ENV_SH, scripts / "env.sh")
    # Minimal tree so ConfigManager can load from release when CODE_ROOT points at current.
    for rel in (
        "config/defaults/default.yaml",
        "config/environments/prod.yaml",
        "config/system/system.yaml",
    ):
        src = REPO_ROOT / rel
        if src.is_file():
            dest = release / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

    current = base / "current"
    current.symlink_to(release)

    etc = tmp / "etc" / "mk04" / "prod"
    runtime = tmp / "var" / "lib" / "mk04" / "prod"
    logs = tmp / "var" / "log" / "mk04" / "prod"
    locks = tmp / "var" / "lib" / "mk04" / "locks"
    for path in (
        runtime / "data" / "cache",
        runtime / "output-funnel",
        runtime / "ops-ui",
        logs,
        locks,
        etc,
    ):
        path.mkdir(parents=True, exist_ok=True)

    # Minimal config files referenced by prod preflight when not skipped.
    for name in (
        "settings.json",
        "channels.json",
        "pipeline_config.json",
        "pipeline_profiles.json",
    ):
        _write(etc / name, "{}\n")
    (etc / "funnels").mkdir(exist_ok=True)

    _write(
        etc / "env",
        textwrap.dedent(
            f"""\
            MK04_ENV=prod
            MK04_CODE_ROOT={base}/current
            MK04_CONFIG_ROOT={etc}
            MK04_RUNTIME_ROOT={runtime}
            MK04_LOG_ROOT={logs}
            MK04_SHARED_LOCK_ROOT={locks}
            INPUT_SERVICE_PORT=5060
            VIDEO_AUTOMATION_PORT=5050
            OUTPUT_FUNNEL_PORT=5055
            OPS_UI_PORT=5070
            AI_SERVICE_PORT=5075
            MK04_UPLOAD_MODE=dry_run
            MK04_SCHEDULER_MODE=manual
            OUTPUT_FUNNEL_PLAN_WORKER_ENABLED=0
            OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED=0
            OUTPUT_FUNNEL_AUTO_UPLOAD=0
            """
        ),
    )
    return {
        "base": base,
        "release": release,
        "current": current,
        "etc": etc,
        "runtime": runtime,
        "logs": logs,
        "locks": locks,
        "tmp": tmp,
    }


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("MK04_")
        and k
        not in {
            "OUTPUT_FUNNEL_DB",
            "OPS_UI_DB",
            "INPUT_SERVICE_PORT",
            "VIDEO_AUTOMATION_PORT",
            "OUTPUT_FUNNEL_PORT",
            "OPS_UI_PORT",
            "AI_SERVICE_PORT",
        }
    }
    env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    env["HOME"] = os.environ.get("HOME", "/tmp")
    if extra:
        env.update(extra)
    return env


class TestProductionRuntimeAuthority:
    def test_missing_runtime_fails_before_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from ops_readonly import production_runtime_authority, probe_runtime_cache_write

        monkeypatch.delenv("MK04_SKIP_PROD_PREFLIGHT", raising=False)
        for name in (
            "MK04_CODE_ROOT",
            "MK04_RUNTIME_ROOT",
            "MK04_LOG_ROOT",
            "MK04_CONFIG_ROOT",
            "MK04_SHARED_LOCK_ROOT",
        ):
            monkeypatch.delenv(name, raising=False)

        ok, detail = production_runtime_authority()
        assert ok is False
        assert "MK04_RUNTIME_ROOT" in detail

        # Even if a release-local cache existed, probe must refuse it.
        release_cache = tmp_path / "opt" / "mk04" / "prod" / "releases" / "r1" / "data" / "cache"
        release_cache.mkdir(parents=True)
        monkeypatch.setenv("MK04_PROD_BASE", str(tmp_path / "opt" / "mk04" / "prod"))
        (tmp_path / "opt" / "mk04" / "prod" / "current").symlink_to(
            tmp_path / "opt" / "mk04" / "prod" / "releases" / "r1"
        )
        poke_ok, poke_detail = probe_runtime_cache_write(release_cache)
        assert poke_ok is False
        assert "code/releases" in poke_detail
        assert list(release_cache.iterdir()) == []

    def test_write_probe_under_temp_runtime_and_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from ops_readonly import probe_runtime_cache_write

        cache = tmp_path / "var" / "lib" / "mk04" / "prod" / "data" / "cache"
        cache.mkdir(parents=True)
        monkeypatch.setenv("MK04_PROD_BASE", str(tmp_path / "opt" / "mk04" / "prod"))
        ok, detail = probe_runtime_cache_write(cache)
        assert ok is True
        assert str(cache) in detail
        assert list(cache.iterdir()) == []

    def test_missing_cache_fails_without_creating(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import health_report as hr
        from state_paths import EnvironmentStatePaths

        runtime = tmp_path / "var" / "lib" / "mk04" / "prod"
        data = runtime / "data"
        data.mkdir(parents=True)
        # cache leaf intentionally absent
        monkeypatch.setenv("MK04_PROD_BASE", str(tmp_path / "opt" / "mk04" / "prod"))
        monkeypatch.setenv("MK04_RUNTIME_ROOT", str(runtime))
        monkeypatch.setenv("MK04_CODE_ROOT", "/opt/mk04/prod/current")
        monkeypatch.setenv("MK04_LOG_ROOT", str(tmp_path / "var" / "log" / "mk04" / "prod"))
        monkeypatch.setenv("MK04_CONFIG_ROOT", str(tmp_path / "etc" / "mk04" / "prod"))
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(tmp_path / "var" / "lib" / "mk04" / "locks"))
        monkeypatch.setenv("MK04_SKIP_PROD_PREFLIGHT", "1")

        state = EnvironmentStatePaths(
            environment="production",
            data_root=data.resolve(),
            jobs_root=(runtime / "video-automation" / "jobs").resolve(),
            outputs_root=(runtime / "video-automation" / "output").resolve(),
            logs_root=(tmp_path / "var" / "log" / "mk04" / "prod").resolve(),
            reports_root=(runtime / "reports").resolve(),
            database_path=(runtime / "database" / "prod.db").resolve(),
            runs_root=(runtime / "runs").resolve(),
            control_state_file=(data / "control_state.json").resolve(),
            clips_root=(runtime / "video-automation" / "output" / "clips").resolve(),
            transcripts_root=(data / "transcripts").resolve(),
            caches_root=(data / "cache").resolve(),
        )
        for parent in (
            state.jobs_root,
            state.outputs_root,
            state.logs_root,
            state.reports_root,
            state.database_path.parent,
            state.runs_root,
            state.clips_root,
            state.transcripts_root,
        ):
            parent.mkdir(parents=True, exist_ok=True)

        check = hr._output_path_write_test(state, is_production=True)
        assert check.result == "FAIL"
        assert "missing" in check.detail.lower() or "not creating" in check.detail.lower()
        assert not (data / "cache").exists()

    def test_health_write_probe_passes_and_removes_when_cache_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import health_report as hr
        from state_paths import EnvironmentStatePaths

        runtime = tmp_path / "var" / "lib" / "mk04" / "prod"
        cache = runtime / "data" / "cache"
        cache.mkdir(parents=True)
        monkeypatch.setenv("MK04_PROD_BASE", str(tmp_path / "opt" / "mk04" / "prod"))
        monkeypatch.setenv("MK04_RUNTIME_ROOT", str(runtime))
        monkeypatch.setenv("MK04_CODE_ROOT", "/opt/mk04/prod/current")
        monkeypatch.setenv("MK04_LOG_ROOT", str(tmp_path / "var" / "log" / "mk04" / "prod"))
        monkeypatch.setenv("MK04_CONFIG_ROOT", str(tmp_path / "etc" / "mk04" / "prod"))
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(tmp_path / "var" / "lib" / "mk04" / "locks"))
        monkeypatch.setenv("MK04_SKIP_PROD_PREFLIGHT", "1")

        data = runtime / "data"
        state = EnvironmentStatePaths(
            environment="production",
            data_root=data.resolve(),
            jobs_root=(runtime / "video-automation" / "jobs").resolve(),
            outputs_root=(runtime / "video-automation" / "output").resolve(),
            logs_root=(tmp_path / "var" / "log" / "mk04" / "prod").resolve(),
            reports_root=(runtime / "reports").resolve(),
            database_path=(runtime / "database" / "prod.db").resolve(),
            runs_root=(runtime / "runs").resolve(),
            control_state_file=(data / "control_state.json").resolve(),
            clips_root=(runtime / "video-automation" / "output" / "clips").resolve(),
            transcripts_root=(data / "transcripts").resolve(),
            caches_root=cache.resolve(),
        )
        for parent in (
            state.jobs_root,
            state.outputs_root,
            state.logs_root,
            state.reports_root,
            state.database_path.parent,
            state.runs_root,
            state.clips_root,
            state.transcripts_root,
        ):
            parent.mkdir(parents=True, exist_ok=True)

        check = hr._output_path_write_test(state, is_production=True)
        assert check.result == "PASS"
        assert list(cache.iterdir()) == []
        # Must not create anything under a releases tree
        releases = tmp_path / "opt" / "mk04" / "prod" / "releases"
        assert not releases.exists() or list(releases.rglob("*")) == []

    def test_optional_db_never_under_releases(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import health_report as hr
        from state_paths import EnvironmentStatePaths

        release_db = (
            tmp_path / "opt" / "mk04" / "prod" / "releases" / "r1" / "database" / "prod.db"
        )
        release_db.parent.mkdir(parents=True)
        monkeypatch.setenv("MK04_PROD_BASE", str(tmp_path / "opt" / "mk04" / "prod"))
        (tmp_path / "opt" / "mk04" / "prod" / "current").symlink_to(
            tmp_path / "opt" / "mk04" / "prod" / "releases" / "r1"
        )

        state = EnvironmentStatePaths(
            environment="production",
            data_root=tmp_path / "data",
            jobs_root=tmp_path / "jobs",
            outputs_root=tmp_path / "outputs",
            logs_root=tmp_path / "logs",
            reports_root=tmp_path / "reports",
            database_path=release_db,
            runs_root=tmp_path / "runs",
            control_state_file=tmp_path / "control.json",
            clips_root=tmp_path / "clips",
            transcripts_root=tmp_path / "transcripts",
            caches_root=tmp_path / "caches",
        )
        # Non-production path still rejects release-local placeholder messaging.
        monkeypatch.setattr(hr, "path_under_code_or_releases", lambda p, **_: True)
        check = hr._database_access(state, is_production=False)
        assert check.result == "WARN"
        assert "code/releases" in check.detail


class TestSchedulerSemantics:
    def test_generic_cron_does_not_imply_mk04_schedule(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from ops_readonly import UnderlyingScheduler, inspect_underlying_scheduler

        monkeypatch.setenv("MK04_SCHEDULER_MODE", "autonomous")
        monkeypatch.setattr(
            "ops_readonly.mk04_schedule_configured",
            lambda **_: (False, "no MK04 cron drop-in or mk04-*.timer installed"),
        )
        monkeypatch.setattr("ops_readonly.systemctl_available", lambda: True)

        def fake_run(cmd):
            class R:
                returncode = 0
                stdout = "active"
                stderr = ""

            return R()

        monkeypatch.setattr("ops_readonly.run_command", fake_run)
        underlying = inspect_underlying_scheduler("prod")
        assert underlying.active is False
        assert underlying.mechanism == "none"
        assert "no MK04" in underlying.detail

    def test_manual_disabled_scheduler_pass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import health_report as hr

        monkeypatch.setenv("MK04_SCHEDULER_MODE", "manual")
        data_root = tmp_path / "data"
        data_root.mkdir()
        (data_root / "control_state.json").write_text(
            '{"scheduler_disabled": true}',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "ops_readonly.mk04_schedule_configured",
            lambda **_: (False, "no MK04"),
        )
        check = hr._scheduler_health("prod", data_root)
        assert check.result == "PASS"
        assert "intentionally disabled" in check.detail
        assert "scheduler_disabled=true" in check.detail


class TestOpsUiAndSecrets:
    def test_ops_ui_http_401_accepted(self, monkeypatch: pytest.MonkeyPatch):
        import health_report as hr

        monkeypatch.setattr(
            hr,
            "service_health_urls",
            lambda _env: {"Operations UI": "http://127.0.0.1:5170/health"},
        )
        monkeypatch.setattr(hr, "http_probe", lambda _url: (False, "HTTP 401"))
        monkeypatch.setattr(
            hr,
            "systemd_unit_status",
            lambda _unit: ("FAIL", "systemd reports inactive", "fail"),
        )
        check = hr._service_health("Operations UI", "mk04-ops-ui.service", "prod")
        assert check.result == "PASS"
        assert "authentication required" in check.detail

    def test_service_health_detail_has_no_secrets(self, monkeypatch: pytest.MonkeyPatch):
        import health_report as hr

        monkeypatch.setattr(
            hr,
            "service_health_urls",
            lambda _env: {"Worker": "http://127.0.0.1:5150/healthz"},
        )
        monkeypatch.setattr(hr, "http_probe", lambda _url: (True, "HTTP 200"))
        check = hr._service_health("Worker", "mk04-video-automation.service", "prod")
        combined = check.detail.lower()
        assert "password" not in combined
        assert "secret" not in combined
        assert "token=" not in combined
        assert "begin " not in combined


class TestHealthShProdEnvContract:
    def test_health_sh_prod_loads_canonical_paths_from_clean_env(self, tmp_path: Path):
        layout = _make_prod_layout(tmp_path)
        env = _clean_env(
            {
                "MK04_PROD_BASE": str(layout["base"]),
                "MK04_SKIP_PROD_PREFLIGHT": "1",
                "MK04_CONFIG_ROOT": str(layout["etc"]),
            }
        )
        # Exercise the same candidate path health.sh uses for prod.
        probe = textwrap.dedent(
            f"""\
            set -euo pipefail
            candidate="{layout["base"]}/current/deploy/scripts/env.sh"
            source "$candidate" prod
            printf 'CODE=%s\\n' "$MK04_CODE_ROOT"
            printf 'RUNTIME=%s\\n' "$MK04_RUNTIME_ROOT"
            printf 'LOG=%s\\n' "$MK04_LOG_ROOT"
            printf 'CONFIG=%s\\n' "$MK04_CONFIG_ROOT"
            printf 'LOCK=%s\\n' "$MK04_SHARED_LOCK_ROOT"
            printf 'SCHED=%s\\n' "$MK04_SCHEDULER_MODE"
            """
        )
        result = subprocess.run(
            ["bash", "-c", probe],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"CODE={layout['base']}/current" in result.stdout
        assert f"RUNTIME={layout['runtime']}" in result.stdout
        assert f"LOG={layout['logs']}" in result.stdout
        assert f"CONFIG={layout['etc']}" in result.stdout
        assert f"LOCK={layout['locks']}" in result.stdout
        assert "SCHED=manual" in result.stdout

    def test_health_report_skips_write_when_authority_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import health_report as hr

        for name in (
            "MK04_CODE_ROOT",
            "MK04_RUNTIME_ROOT",
            "MK04_LOG_ROOT",
            "MK04_CONFIG_ROOT",
            "MK04_SHARED_LOCK_ROOT",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("MK04_SKIP_PROD_PREFLIGHT", raising=False)

        # Avoid full boot/config side effects: patch boot + config validation.
        monkeypatch.setattr(
            hr,
            "build_boot_verification",
            lambda _env: type(
                "B",
                (),
                {
                    "overall": "NOT READY",
                    "components": [],
                    "env_label": "PRODUCTION",
                },
            )(),
        )
        monkeypatch.setattr(
            hr,
            "_check_config_validation",
            lambda _c: (
                hr.HealthCheck("Config validation", "FAIL", "skipped", "fail"),
                None,
            ),
        )
        monkeypatch.setattr(hr, "_check_required_env_file", lambda *_a, **_k: hr.HealthCheck("Required env file", "PASS"))
        monkeypatch.setattr(hr, "_api_health_endpoint", lambda *_a, **_k: hr.HealthCheck("API health endpoint", "PASS"))
        monkeypatch.setattr(hr, "discover_service_units", lambda: [])
        monkeypatch.setattr(
            hr,
            "_scheduler_health",
            lambda *_a, **_k: hr.HealthCheck("Scheduler", "PASS", "manual"),
        )
        monkeypatch.setattr(
            hr,
            "_execution_lock_health",
            lambda *_a, **_k: hr.HealthCheck("Execution lock", "PASS", "none"),
        )
        monkeypatch.setattr(
            hr,
            "_last_run_health",
            lambda *_a, **_k: hr.HealthCheck("Last pipeline run", "PASS", "none"),
        )

        wrote: list[Path] = []

        def _boom(state, *, is_production):  # noqa: ARG001
            wrote.append(Path("should-not-run"))
            return hr.HealthCheck("Output path write test", "PASS")

        monkeypatch.setattr(hr, "_output_path_write_test", _boom)

        report = hr.build_health_report("prod")
        labels = {c.label: c for c in report.checks}
        assert labels["Runtime path authority"].result == "FAIL"
        assert labels["Output path write test"].result == "FAIL"
        assert "skipped" in labels["Output path write test"].detail
        assert wrote == []
