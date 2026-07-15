"""Tests for scripts/ops/restart.sh (Remote Operations Prompt 6)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTART_SH = REPO_ROOT / "scripts" / "ops" / "restart.sh"
OPS_DIR = REPO_ROOT / "scripts" / "ops"


def _run_bash(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", str(RESTART_SH), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=merged,
        timeout=120,
    )


class TestRestartScriptInterface:
    def test_help_flags_exit_zero(self):
        for flag in ("--help", "-h"):
            result = _run_bash(flag)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "Usage:" in result.stdout

    def test_missing_args_fails(self):
        result = _run_bash()
        assert result.returncode != 0

    def test_invalid_env_fails(self):
        result = _run_bash("staging", "worker")
        assert result.returncode != 0

    def test_invalid_target_fails(self):
        result = _run_bash("prod", "unknown-service")
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "invalid target" in combined.lower() or "unknown restart target" in combined.lower()

    def test_prod_all_without_confirm_fails(self):
        result = _run_bash("prod", "all")
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "--confirm" in combined


class TestRestartDryRun:
    def test_dev_worker_dry_run(self):
        result = _run_bash("dev", "worker", "--dry-run")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Remote Operations Restart" in result.stdout
        assert "Dry-run only" in result.stdout
        assert "mk04-video-automation.service" in result.stdout
        assert "Would restart worker" in result.stdout

    def test_prod_all_dry_run(self):
        result = _run_bash("prod", "all", "--dry-run")
        assert result.returncode == 0
        assert "Would restart api" in result.stdout
        assert "Would restart worker" in result.stdout


class TestRestartMapping:
    def test_resolve_restart_targets(self):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        from ops_readonly import resolve_restart_targets

        pairs = resolve_restart_targets("api")
        assert pairs == [("api", "mk04-source-input.service")]

        all_pairs = resolve_restart_targets("all")
        names = [name for name, _ in all_pairs]
        assert "api" in names
        assert "worker" in names
        assert "ai" in names


class TestRestartAuthBatching:
    def test_systemctl_privileged_never_uses_bare_systemctl(self, monkeypatch):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        import restart_service as rs

        monkeypatch.setattr(rs, "systemctl_available", lambda: True)
        monkeypatch.setattr(rs, "shutil_which_sudo", lambda: "/usr/bin/sudo")
        monkeypatch.setattr(rs.os, "geteuid", lambda: 1000)
        seen: list[list[str]] = []

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run_command(cmd, timeout=None):
            seen.append(list(cmd))
            return R()

        monkeypatch.setattr(rs, "run_command", fake_run_command)
        ok, _detail = rs.systemctl_restart_units(
            [
                "mk04-source-input.service",
                "mk04-video-automation.service",
                "mk04-output-funnel.service",
                "mk04-ai-service.service",
                "mk04-ops-ui.service",
            ]
        )
        assert ok
        assert len(seen) == 1
        assert seen[0][:3] == ["sudo", "-n", "systemctl"]
        assert seen[0][3] == "restart"
        assert "mk04-source-input.service" in seen[0]
        assert "mk04-ops-ui.service" in seen[0]
        # Never a bare per-unit systemctl that would trigger Polkit.
        assert not any(c[0] == "systemctl" for c in seen)

    def test_ensure_auth_cached_skips_sudo_v(self, monkeypatch):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        import restart_service as rs

        monkeypatch.setattr(rs, "systemctl_available", lambda: True)
        monkeypatch.setattr(rs, "shutil_which_sudo", lambda: "/usr/bin/sudo")
        monkeypatch.setattr(rs.os, "geteuid", lambda: 1000)
        calls: list[list[str]] = []

        def fake_run(cmd, check=False, capture_output=False, text=False):
            calls.append(list(cmd))
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        monkeypatch.setattr(rs.subprocess, "run", fake_run)
        rs.ensure_systemctl_authorization(interactive=True)
        assert calls == [["sudo", "-n", "true"]]
        assert not any(c == ["sudo", "-v"] for c in calls)

    def test_ensure_auth_cold_session_runs_sudo_v_once(self, monkeypatch):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        import restart_service as rs

        monkeypatch.setattr(rs, "systemctl_available", lambda: True)
        monkeypatch.setattr(rs, "shutil_which_sudo", lambda: "/usr/bin/sudo")
        monkeypatch.setattr(rs.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(rs.sys.stdin, "isatty", lambda: True)
        calls: list[list[str]] = []
        n_probe = {"n": 0}

        def fake_run(cmd, check=False, capture_output=False, text=False):
            calls.append(list(cmd))
            if cmd == ["sudo", "-n", "true"]:
                n_probe["n"] += 1
                # First probe: cold cache. Second probe after sudo -v: ok.
                code = 1 if n_probe["n"] == 1 else 0
                return type("R", (), {"returncode": code, "stdout": "", "stderr": ""})()
            if cmd == ["sudo", "-v"]:
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            raise AssertionError(f"unexpected cmd {cmd}")

        monkeypatch.setattr(rs.subprocess, "run", fake_run)
        rs.ensure_systemctl_authorization(interactive=True)
        assert calls.count(["sudo", "-v"]) == 1
        assert calls.count(["sudo", "-n", "true"]) == 2
        assert calls[0] == ["sudo", "-n", "true"]
        assert calls[1] == ["sudo", "-v"]

    def test_execute_start_auth_failure_skips_systemctl(self, monkeypatch):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        import restart_service as rs

        starts: list[list[str]] = []

        def boom(*, interactive=True):
            raise rs.AuthorizationError("no privilege")

        monkeypatch.setattr(rs, "ensure_systemctl_authorization", boom)
        monkeypatch.setattr(
            rs,
            "systemctl_start_units",
            lambda units: starts.append(list(units)) or (True, "should-not-run"),
        )
        monkeypatch.setattr(
            rs,
            "resolve_restart_targets",
            lambda _t: [
                ("api", "mk04-source-input.service"),
                ("worker", "mk04-video-automation.service"),
                ("output-funnel", "mk04-output-funnel.service"),
                ("ai", "mk04-ai-service.service"),
                ("ops-ui", "mk04-ops-ui.service"),
            ],
        )
        code = rs.execute_start("prod", "all", skip_health=True)
        assert code == 1
        assert starts == []

    def test_execute_start_one_batched_noninteractive_call(self, monkeypatch):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        import restart_service as rs

        auth = {"n": 0}
        seen: list[list[str]] = []

        monkeypatch.setattr(rs, "systemctl_available", lambda: True)
        monkeypatch.setattr(rs, "shutil_which_sudo", lambda: "/usr/bin/sudo")
        monkeypatch.setattr(rs.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(
            rs,
            "ensure_systemctl_authorization",
            lambda interactive=True: auth.__setitem__("n", auth["n"] + 1),
        )

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run_command(cmd, timeout=None):
            seen.append(list(cmd))
            return R()

        monkeypatch.setattr(rs, "run_command", fake_run_command)
        monkeypatch.setattr(
            rs,
            "resolve_restart_targets",
            lambda _t: [
                ("api", "mk04-source-input.service"),
                ("worker", "mk04-video-automation.service"),
                ("output-funnel", "mk04-output-funnel.service"),
                ("ai", "mk04-ai-service.service"),
                ("ops-ui", "mk04-ops-ui.service"),
            ],
        )
        monkeypatch.setattr(
            rs,
            "run_health_check_with_retry",
            lambda *_a, **_k: (0, "PASS", "Overall PASS"),
        )
        code = rs.execute_start("prod", "all")
        assert code == 0
        assert auth["n"] == 1
        assert len(seen) == 1
        assert seen[0][:4] == ["sudo", "-n", "systemctl", "start"]
        for unit in (
            "mk04-source-input.service",
            "mk04-video-automation.service",
            "mk04-output-funnel.service",
            "mk04-ai-service.service",
            "mk04-ops-ui.service",
        ):
            assert unit in seen[0]
        assert not any(c[0] == "systemctl" for c in seen)

    def test_execute_restart_skip_health_ignores_warn(self, monkeypatch):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        import restart_service as rs

        health_calls = {"n": 0}
        monkeypatch.setattr(rs, "ensure_systemctl_authorization", lambda interactive=True: None)
        monkeypatch.setattr(
            rs,
            "systemctl_restart_units",
            lambda units: (True, "sudo -n systemctl restart " + " ".join(units)),
        )
        monkeypatch.setattr(
            rs,
            "run_health_check_with_retry",
            lambda *_a, **_k: health_calls.__setitem__("n", health_calls["n"] + 1)
            or (1, "WARN", "Overall WARN"),
        )
        monkeypatch.setattr(
            rs,
            "resolve_restart_targets",
            lambda _t: [("api", "mk04-source-input.service")],
        )
        code = rs.execute_restart("prod", "api", confirm=True, skip_health=True)
        assert code == 0
        assert health_calls["n"] == 0

    def test_execute_restart_authorizes_once(self, monkeypatch):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        import restart_service as rs

        auth = {"n": 0}
        monkeypatch.setattr(
            rs,
            "ensure_systemctl_authorization",
            lambda interactive=True: auth.__setitem__("n", auth["n"] + 1),
        )
        monkeypatch.setattr(
            rs,
            "systemctl_restart_units",
            lambda units: (True, f"sudo -n systemctl restart {' '.join(units)}"),
        )
        monkeypatch.setattr(
            rs,
            "run_health_check_with_retry",
            lambda *_a, **_k: (0, "PASS", "Overall PASS"),
        )
        monkeypatch.setattr(
            rs,
            "resolve_restart_targets",
            lambda _t: [
                ("api", "mk04-source-input.service"),
                ("worker", "mk04-video-automation.service"),
            ],
        )
        code = rs.execute_restart("prod", "all", confirm=True)
        assert code == 0
        assert auth["n"] == 1

    def test_health_retry_accepts_delayed_ready(self, monkeypatch):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        import restart_service as rs

        states = iter(
            [
                (1, "FAIL", "Overall FAIL"),
                (1, "FAIL", "Overall FAIL"),
                (0, "PASS", "Overall PASS"),
            ]
        )
        monkeypatch.setattr(rs, "run_health_check", lambda _e: next(states))
        monkeypatch.setattr(rs.time, "sleep", lambda _s: None)
        code, overall, _out = rs.run_health_check_with_retry(
            "prod", initial_wait_sec=0, total_sec=30, interval_sec=0
        )
        assert code == 0
        assert overall == "PASS"

    def test_health_retry_persistent_failure(self, monkeypatch):
        if str(OPS_DIR) not in sys.path:
            sys.path.insert(0, str(OPS_DIR))
        import restart_service as rs

        monkeypatch.setattr(
            rs, "run_health_check", lambda _e: (1, "FAIL", "API FAIL\nOverall FAIL")
        )
        monkeypatch.setattr(rs.time, "sleep", lambda _s: None)
        # Force immediate deadline expiry after first check.
        start = [0.0]

        def mono():
            start[0] += 100
            return start[0]

        monkeypatch.setattr(rs.time, "monotonic", mono)
        code, overall, out = rs.run_health_check_with_retry(
            "prod", initial_wait_sec=0, total_sec=1, interval_sec=0
        )
        assert code != 0
        assert overall == "FAIL"
        assert "FAIL" in out
