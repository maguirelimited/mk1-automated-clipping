"""Prompt 4 repair: ungated-jobs contract, architecture, env.sh fallback."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"
VA_SERVER = REPO_ROOT / "video-automation" / "server"
sys.path.insert(0, str(OPS_DIR))

import execution_gate as eg  # noqa: E402


def _cleared_lock_env(**extra: str) -> dict[str, str]:
    """Subprocess env with inherited live lock roots removed."""
    env = {k: v for k, v in os.environ.items() if k != "MK04_SHARED_LOCK_ROOT"}
    env.pop("MK04_SHARED_LOCK_ROOT", None)
    env.pop("MK04_PRODUCTION_INSTALLED", None)
    env.update(extra)
    return env


def test_env_sh_dev_leaves_shared_root_unset_without_deployed(tmp_path: Path) -> None:
    """Pre-bootstrap: env.sh must not force a lock root when deployed probe is absent.

    Hermetic: never inspects real /opt, /etc, or /var/lib — uses MK04_DEPLOYED_LOCK_ROOT.
    """
    deployed_probe = tmp_path / "deployed_locks_absent"
    code_root = tmp_path / "code"
    code_root.mkdir()
    script = f"""
set -euo pipefail
unset MK04_SHARED_LOCK_ROOT || true
unset MK04_PRODUCTION_INSTALLED || true
export MK04_DEPLOYED_LOCK_ROOT="{deployed_probe}"
export MK04_CODE_ROOT="{REPO_ROOT}"
source "{REPO_ROOT}/deploy/scripts/env.sh" dev
if [[ -n "${{MK04_SHARED_LOCK_ROOT:-}}" ]]; then
  echo "UNEXPECTED_SET=$MK04_SHARED_LOCK_ROOT"
  exit 1
fi
echo "UNSET_OK"
export MK04_CODE_ROOT="{code_root}"
export MK04_ENV=dev
unset MK04_SHARED_LOCK_ROOT || true
"{REPO_ROOT}/video-automation/.venv/bin/python" - <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, r"{OPS_DIR}")
import execution_gate as eg
os.environ["MK04_ENV"] = "dev"
os.environ.pop("MK04_SHARED_LOCK_ROOT", None)
os.environ["MK04_CODE_ROOT"] = r"{code_root}"
eg.DEFAULT_DEPLOYED_SHARED_ROOT = Path(r"{deployed_probe}")
eg.production_installation_present = lambda: False
root = eg.ensure_shared_lock_root()
assert root == (Path(r"{code_root}") / ".mk04_locks").resolve()
assert root.is_dir()
print("FALLBACK_OK")
PY
"""
    result = subprocess.run(
        ["bash", "-lc", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        env=_cleared_lock_env(
            MK04_CODE_ROOT=str(REPO_ROOT),
            MK04_DEPLOYED_LOCK_ROOT=str(deployed_probe),
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UNSET_OK" in result.stdout
    assert "FALLBACK_OK" in result.stdout
    assert "/opt/" not in result.stdout
    assert "/var/lib/" not in result.stdout


def test_env_sh_dev_uses_writable_simulated_deployed_lock_root(tmp_path: Path) -> None:
    """Post-bootstrap simulation: writable MK04_DEPLOYED_LOCK_ROOT is selected for dev."""
    deployed = tmp_path / "deployed_locks"
    deployed.mkdir()
    script = f"""
set -euo pipefail
unset MK04_SHARED_LOCK_ROOT || true
unset MK04_PRODUCTION_INSTALLED || true
export MK04_DEPLOYED_LOCK_ROOT="{deployed}"
export MK04_CODE_ROOT="{REPO_ROOT}"
source "{REPO_ROOT}/deploy/scripts/env.sh" dev
if [[ "${{MK04_SHARED_LOCK_ROOT:-}}" != "{deployed}" ]]; then
  echo "UNEXPECTED_SET=${{MK04_SHARED_LOCK_ROOT:-}}"
  exit 1
fi
echo "DEPLOYED_OK=$MK04_SHARED_LOCK_ROOT"
"""
    result = subprocess.run(
        ["bash", "-lc", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        env=_cleared_lock_env(
            MK04_CODE_ROOT=str(REPO_ROOT),
            MK04_DEPLOYED_LOCK_ROOT=str(deployed),
        ),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"DEPLOYED_OK={deployed}" in result.stdout
    assert "/var/lib/mk04/locks" not in result.stdout


def test_live_routes_do_not_call_run_pipeline_outside_execute_job() -> None:
    source = (VA_SERVER / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    execute_fn = None
    run_pipeline_fn = None
    callers: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if node.name == "_execute_job":
                execute_fn = node
            if node.name == "_run_pipeline":
                run_pipeline_fn = node
    assert execute_fn is not None
    assert run_pipeline_fn is not None
    assert "_process_pipeline_json_payload" not in source

    class CallFinder(ast.NodeVisitor):
        def __init__(self) -> None:
            self.current: str | None = None

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            prev = self.current
            self.current = node.name
            self.generic_visit(node)
            self.current = prev

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "_run_pipeline" and self.current and self.current != "_run_pipeline":
                callers.append(self.current)
            self.generic_visit(node)

    CallFinder().visit(tree)
    assert callers == ["_execute_job"], f"unexpected _run_pipeline callers: {callers}"


class TestUngatedJobsContract:
    def test_rejected_in_prod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sys.path.insert(0, str(VA_SERVER))
        import app as server_app

        monkeypatch.setenv("MK04_ENV", "prod")
        monkeypatch.setenv("MK04_ALLOW_UNGATED_JOBS", "1")
        monkeypatch.setenv("MK04_TEST_MODE", "1")
        err = server_app._ungated_jobs_config_error()
        assert err is not None
        assert "not allowed" in err
        assert server_app._ungated_jobs_allowed() is False

    def test_rejected_in_normal_dev(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sys.path.insert(0, str(VA_SERVER))
        import app as server_app

        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.setenv("MK04_ALLOW_UNGATED_JOBS", "1")
        monkeypatch.delenv("MK04_TEST_MODE", raising=False)
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
        assert server_app._ungated_jobs_allowed() is False

    def test_allowed_only_in_dev_test_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sys.path.insert(0, str(VA_SERVER))
        import app as server_app

        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.setenv("MK04_ALLOW_UNGATED_JOBS", "1")
        monkeypatch.setenv("MK04_TEST_MODE", "1")
        assert server_app._ungated_jobs_config_error() is None
        assert server_app._ungated_jobs_allowed() is True

    def test_worker_still_acquires_global_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sys.path.insert(0, str(VA_SERVER))
        import app as server_app

        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.setenv("MK04_ALLOW_UNGATED_JOBS", "1")
        monkeypatch.setenv("MK04_TEST_MODE", "1")
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(tmp_path / "locks"))
        (tmp_path / "locks").mkdir()

        acquired: list[str] = []

        class _Heavy:
            def release(self) -> None:
                return None

        def fake_acquire(**kwargs):
            acquired.append(kwargs.get("job_id") or "x")
            return _Heavy()

        monkeypatch.setattr(
            "execution_gate.acquire_global_pipeline_lock",
            fake_acquire,
        )

        job_dir = tmp_path / "job"
        job_dir.mkdir()
        report = job_dir / "report.json"
        review = job_dir / "review.md"
        report.write_text("{}", encoding="utf-8")
        review.write_text("", encoding="utf-8")
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")

        def fake_run_pipeline(*_a, **_k):
            class R:
                def get_json(self, silent=True):
                    return {"success": True, "clips": []}

            return R(), 200

        monkeypatch.setattr(server_app, "_run_pipeline", fake_run_pipeline)
        monkeypatch.setattr(server_app, "_sync_input_ledger_terminal", lambda *_a, **_k: None)
        monkeypatch.setattr(server_app, "_update_job_report", lambda job, updates: updates)
        monkeypatch.setattr(server_app, "_load_json_object", lambda *_a, **_k: {})
        monkeypatch.setattr(server_app, "write_json", lambda *_a, **_k: None)
        monkeypatch.setattr(server_app, "write_review", lambda *_a, **_k: None)
        monkeypatch.setattr(server_app, "_progress", lambda *_a, **_k: None)

        # Avoid queue.task_done imbalance in isolated call.
        class _Q:
            def task_done(self):
                return None

            def put(self, *_a, **_k):
                return None

        monkeypatch.setattr(server_app, "_JOB_QUEUE", _Q())

        task = {
            "job_id": "job_flag",
            "job": {
                "report_path": str(report),
                "review_path": str(review),
                "job_dir": str(job_dir),
            },
            "video_path": str(video),
            "input_id": None,
            "policy_bundle": {},
            "orchestration_context": None,
        }
        server_app._execute_job(task)
        assert acquired == ["job_flag"]

    def test_flag_job_cannot_parallel_heavy_with_other_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        locks = tmp_path / "locks"
        locks.mkdir()
        monkeypatch.setenv("MK04_SHARED_LOCK_ROOT", str(locks))
        monkeypatch.setenv("MK04_ENV", "dev")
        monkeypatch.setenv("MK04_ALLOW_UNGATED_JOBS", "1")
        monkeypatch.setenv("MK04_TEST_MODE", "1")
        other = eg.acquire_global_pipeline_lock(
            environment="prod",
            run_id="prod_hold",
            job_id="j_prod",
            shared_root=locks,
            blocking=True,
        )
        with pytest.raises(eg.GateError, match="busy"):
            eg.acquire_global_pipeline_lock(
                environment="dev",
                run_id="dev_flag",
                job_id="j_dev",
                shared_root=locks,
                blocking=False,
            )
        other.release()
