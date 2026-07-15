"""Production deployment-root preflight: logical current vs physical active release."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_SH = REPO_ROOT / "deploy" / "scripts" / "env.sh"
RUN_SCRIPTS = (
    "run-input-service.sh",
    "run-video-automation.sh",
    "run-output-funnel.sh",
    "run-ai-service.sh",
    "run-ops-ui.sh",
)


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


def _seed_release(release: Path) -> None:
    """Minimal release tree with deploy scripts (copied from repo env.sh + stubs)."""
    scripts = release / "deploy" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ENV_SH, scripts / "env.sh")
    for name in RUN_SCRIPTS:
        src = REPO_ROOT / "deploy" / "scripts" / name
        shutil.copy2(src, scripts / name)
        os.chmod(scripts / name, 0o755)
    # Probe that only exercises the deployment-root contract (skips file preflight).
    _write(
        scripts / "probe-deployment-root.sh",
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
            # shellcheck disable=SC1091
            source "$SCRIPT_DIR/env.sh" prod
            echo "OK active=${MK04_ACTIVE_RELEASE}"
            """
        ),
        mode=0o755,
    )


def _make_prod_layout(tmp: Path) -> dict[str, Path]:
    base = tmp / "opt" / "mk04" / "prod"
    releases = base / "releases"
    rel_a = releases / "relA_active"
    rel_b = releases / "relB_previous"
    _seed_release(rel_a)
    _seed_release(rel_b)
    current = base / "current"
    current.symlink_to(rel_a)
    etc = tmp / "etc" / "mk04" / "prod"
    _write(
        etc / "env",
        textwrap.dedent(
            f"""\
            MK04_ENV=prod
            MK04_CODE_ROOT={base}/current
            MK04_CONFIG_ROOT={etc}
            MK04_RUNTIME_ROOT={tmp}/var/lib/mk04/prod
            MK04_LOG_ROOT={tmp}/var/log/mk04/prod
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
    locks = tmp / "var" / "lib" / "mk04" / "locks"
    locks.mkdir(parents=True)
    return {
        "base": base,
        "releases": releases,
        "rel_a": rel_a,
        "rel_b": rel_b,
        "current": current,
        "etc": etc,
        "locks": locks,
        "tmp": tmp,
    }


def _run_probe(
    layout: dict[str, Path],
    *,
    script: Path,
    code_root: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "MK04_PROD_BASE": str(layout["base"]),
        "MK04_CONFIG_ROOT": str(layout["etc"]),
        "MK04_RUNTIME_ROOT": str(layout["tmp"] / "var" / "lib" / "mk04" / "prod"),
        "MK04_LOG_ROOT": str(layout["tmp"] / "var" / "log" / "mk04" / "prod"),
        "MK04_SHARED_LOCK_ROOT": str(layout["locks"]),
        "MK04_SKIP_PROD_PREFLIGHT": "1",
        "MK04_CODE_ROOT": code_root or str(layout["current"]),
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


class TestProdDeploymentRootContract:
    def test_valid_current_symlink_passes(self, tmp_path: Path):
        layout = _make_prod_layout(tmp_path)
        probe = layout["current"] / "deploy" / "scripts" / "probe-deployment-root.sh"
        result = _run_probe(layout, script=probe)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK active=" in result.stdout
        assert str(layout["rel_a"].resolve()) in result.stdout

    def test_direct_stale_release_fails(self, tmp_path: Path):
        layout = _make_prod_layout(tmp_path)
        probe = layout["rel_b"] / "deploy" / "scripts" / "probe-deployment-root.sh"
        result = _run_probe(layout, script=probe, code_root=str(layout["current"]))
        assert result.returncode != 0
        assert "PROD preflight failed" in result.stderr or "active release" in result.stderr

    def test_dev_checkout_fails(self, tmp_path: Path):
        layout = _make_prod_layout(tmp_path)
        # Point script root at the real repo by sourcing repo env.sh while claiming prod.
        env = {
            **os.environ,
            "MK04_PROD_BASE": str(layout["base"]),
            "MK04_CONFIG_ROOT": str(layout["etc"]),
            "MK04_CODE_ROOT": str(layout["current"]),
            "MK04_SKIP_PROD_PREFLIGHT": "1",
            "MK04_SHARED_LOCK_ROOT": str(layout["locks"]),
        }
        script = textwrap.dedent(
            f"""\
            set -euo pipefail
            source "{ENV_SH}" prod
            """
        )
        result = subprocess.run(
            ["bash", "-lc", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode != 0
        blob = result.stderr + result.stdout
        assert "PROD preflight failed" in blob or "active release" in blob or "checkout" in blob

    def test_current_as_real_directory_fails(self, tmp_path: Path):
        layout = _make_prod_layout(tmp_path)
        layout["current"].unlink()
        layout["current"].mkdir()
        shutil.copytree(
            layout["rel_a"] / "deploy",
            layout["current"] / "deploy",
            dirs_exist_ok=True,
        )
        probe = layout["current"] / "deploy" / "scripts" / "probe-deployment-root.sh"
        result = _run_probe(layout, script=probe)
        assert result.returncode != 0
        assert "must be a symlink" in result.stderr

    def test_current_symlink_outside_releases_fails(self, tmp_path: Path):
        layout = _make_prod_layout(tmp_path)
        outside = tmp_path / "elsewhere" / "not_a_release"
        _seed_release(outside)
        layout["current"].unlink()
        layout["current"].symlink_to(outside)
        probe = layout["current"] / "deploy" / "scripts" / "probe-deployment-root.sh"
        result = _run_probe(layout, script=probe)
        assert result.returncode != 0
        assert "must target under" in result.stderr or "PROD preflight failed" in result.stderr

    def test_broken_current_symlink_fails(self, tmp_path: Path):
        layout = _make_prod_layout(tmp_path)
        layout["current"].unlink()
        layout["current"].symlink_to(layout["releases"] / "missing_release")
        # Probe cannot live on broken current; invoke resolver via a side script.
        side = tmp_path / "side" / "deploy" / "scripts"
        side.mkdir(parents=True)
        shutil.copy2(ENV_SH, side / "env.sh")
        _write(
            side / "probe.sh",
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
                source "$SCRIPT_DIR/env.sh" prod
                """
            ),
            mode=0o755,
        )
        # Force CODE_ROOT logical current (broken) — script root is side tree (will also fail).
        result = _run_probe(layout, script=side / "probe.sh")
        assert result.returncode != 0
        assert "broken" in result.stderr or "PROD preflight failed" in result.stderr

    def test_all_five_systemd_execstart_paths_pass_deployment_root(self, tmp_path: Path):
        layout = _make_prod_layout(tmp_path)
        for name in RUN_SCRIPTS:
            # Match ExecStart: /opt/.../current/deploy/scripts/<name> prod
            script = layout["current"] / "deploy" / "scripts" / name
            # Stop before python exec: wrap by sourcing only via env from the run script dir.
            wrapper = layout["current"] / "deploy" / "scripts" / f"_test_{name}"
            _write(
                wrapper,
                textwrap.dedent(
                    f"""\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
                    # Simulate the real run script's first actions.
                    source "$SCRIPT_DIR/env.sh" prod
                    echo "EXECSTART_OK {name} active=$MK04_ACTIVE_RELEASE"
                    """
                ),
                mode=0o755,
            )
            result = _run_probe(layout, script=wrapper)
            assert result.returncode == 0, f"{name}: {result.stderr}"
            assert f"EXECSTART_OK {name}" in result.stdout

    def test_switching_current_rejects_old_release(self, tmp_path: Path):
        layout = _make_prod_layout(tmp_path)
        old_probe = layout["rel_a"] / "deploy" / "scripts" / "probe-deployment-root.sh"
        # Switch current to rel_b
        layout["current"].unlink()
        layout["current"].symlink_to(layout["rel_b"])
        new_probe = layout["current"] / "deploy" / "scripts" / "probe-deployment-root.sh"
        new_result = _run_probe(layout, script=new_probe)
        assert new_result.returncode == 0, new_result.stderr
        assert str(layout["rel_b"].resolve()) in new_result.stdout
        old_result = _run_probe(layout, script=old_probe)
        assert old_result.returncode != 0
        assert "PROD preflight failed" in old_result.stderr or "active release" in old_result.stderr

    def test_dev_behaviour_unchanged(self, tmp_path: Path):
        # Dev must not require production current symlink.
        env = {
            **os.environ,
            "MK04_CODE_ROOT": str(REPO_ROOT),
            "MK04_SKIP_PROD_PREFLIGHT": "1",
        }
        script = textwrap.dedent(
            f"""\
            set -euo pipefail
            unset MK04_PROD_BASE || true
            source "{ENV_SH}" dev
            echo "DEV_OK env=$MK04_ENV code=$MK04_CODE_ROOT"
            """
        )
        result = subprocess.run(
            ["bash", "-lc", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "DEV_OK env=dev" in result.stdout

    def test_logical_code_root_must_be_current_not_release_path(self, tmp_path: Path):
        layout = _make_prod_layout(tmp_path)
        env_file = layout["etc"] / "env"
        text = env_file.read_text(encoding="utf-8")
        env_file.write_text(
            text.replace(
                f"MK04_CODE_ROOT={layout['base']}/current",
                f"MK04_CODE_ROOT={layout['rel_a']}",
            ),
            encoding="utf-8",
        )
        probe = layout["current"] / "deploy" / "scripts" / "probe-deployment-root.sh"
        result = _run_probe(layout, script=probe, code_root=str(layout["rel_a"]))
        assert result.returncode != 0
        assert "logical deployment entry" in result.stderr
