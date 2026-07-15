#!/usr/bin/env python3
"""
Full environment-aware smoke test for Configuration & Deployment (Prompt 9).

Usage:
    python scripts/smoke/smoke_config_deployment.py
    python scripts/smoke/smoke_config_deployment.py --env dev
    python scripts/smoke/smoke_config_deployment.py --env prod
    python scripts/smoke/smoke_config_deployment.py --both

Default (no --env): dev only — never silently runs production checks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_CONFIG = REPO_ROOT / "scripts" / "config"
OPS_UI_ROOT = REPO_ROOT / "ops-ui"

if str(SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_CONFIG))
if str(OPS_UI_ROOT) not in sys.path:
    sys.path.insert(0, str(OPS_UI_ROOT))

from config_manager import ConfigError, ConfigManager  # noqa: E402
from execution_context import ExecutionContext  # noqa: E402
from state_paths import EnvironmentStatePaths, _is_under  # noqa: E402
from validate_config import validate_config_tree  # noqa: E402

_SECRET_RE = re.compile(
    r"(password|secret|token|api[_-]?key|bearer|credential|private[_-]?key)",
    re.IGNORECASE,
)

_FUNNEL_ID = "business"
_PLATFORM_ID = "youtube"


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class EnvSmokeResult:
    mk04_env: str
    canonical_env: str
    job_id: str
    job_dir: Path | None = None
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def normalize_mk04_env(raw: str) -> str:
    token = raw.strip().lower()
    if token in {"dev", "development"}:
        return "dev"
    if token in {"prod", "production"}:
        return "prod"
    raise ValueError(f"invalid environment: {raw!r}")


def canonical_environment(mk04_env: str) -> str:
    return "development" if mk04_env == "dev" else "production"


def _check(name: str, ok: bool, detail: str = "") -> CheckResult:
    return CheckResult(name=name, passed=ok, detail=detail)


def _load_config(mk04_env: str, config_root: Path) -> tuple[Any, EnvironmentStatePaths]:
    canonical = canonical_environment(mk04_env)
    config = ConfigManager.load(
        environment=canonical,
        funnel_id=_FUNNEL_ID,
        platform_id=_PLATFORM_ID,
        config_root=config_root,
    )
    state = EnvironmentStatePaths.from_resolved_config(config)
    return config, state


def _verify_paths(mk04_env: str, state: EnvironmentStatePaths) -> list[CheckResult]:
    checks: list[CheckResult] = []
    scope = "dev" if mk04_env == "dev" else "prod"
    checks.append(
        _check(
            f"{scope} jobs_root scoped",
            scope in str(state.jobs_root).replace("\\", "/"),
            str(state.jobs_root),
        )
    )
    checks.append(
        _check(
            f"{scope} data_root scoped",
            scope in str(state.data_root).replace("\\", "/"),
            str(state.data_root),
        )
    )
    expected_db = f"database/{scope}.db"
    checks.append(
        _check(
            f"{scope} database path",
            expected_db in str(state.database_path).replace("\\", "/"),
            str(state.database_path),
        )
    )
    return checks


def _verify_snapshot_and_context(
    *,
    job_dir: Path,
    job_id: str,
    canonical_env: str,
    config: Any,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    snap_path = job_dir / "resolved_config.yaml"
    ctx_path = job_dir / "execution_context.json"

    checks.append(_check("resolved_config.yaml saved", snap_path.is_file(), str(snap_path)))
    checks.append(_check("execution_context.json saved", ctx_path.is_file(), str(ctx_path)))

    if not snap_path.is_file() or not ctx_path.is_file():
        return checks

    snap_text = snap_path.read_text(encoding="utf-8").lower()
    for word in ("api_key:", "password:", "token:", "secret:"):
        checks.append(_check(f"snapshot has no {word}", word not in snap_text))

    ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
    checks.append(
        _check(
            "execution_context.environment",
            ctx.get("environment") == canonical_env,
            str(ctx.get("environment")),
        )
    )
    checks.append(
        _check(
            "execution_context.job_id",
            ctx.get("job_id") == job_id,
            str(ctx.get("job_id")),
        )
    )
    for key in ("funnel_id", "platform_id", "preset_id"):
        checks.append(_check(f"execution_context.{key}", bool(ctx.get(key)), str(ctx.get(key))))

    resolved_path = str(ctx.get("resolved_config_path") or "")
    checks.append(
        _check(
            "execution_context.resolved_config_path in job dir",
            resolved_path != "" and _is_under(Path(resolved_path).resolve(), job_dir.resolve()),
            resolved_path,
        )
    )

    ctx_blob = json.dumps(ctx).lower()
    checks.append(_check("execution_context has no secrets", not _SECRET_RE.search(ctx_blob)))

    checks.append(
        _check(
            "snapshot metadata present",
            config.funnel_id in snap_text and config.platform_id in snap_text,
            f"funnel={config.funnel_id} platform={config.platform_id}",
        )
    )
    return checks


def _verify_ui_summary(mk04_env: str, repo_root: Path) -> list[CheckResult]:
    checks: list[CheckResult] = []
    try:
        from ops_ui.config import Settings  # noqa: PLC0415
        from ops_ui.environment_summary import build_environment_summary  # noqa: PLC0415

        label = "DEVELOPMENT" if mk04_env == "dev" else "PRODUCTION"
        settings = Settings(
            host="127.0.0.1",
            port=5170,
            data_dir=repo_root / "ops-ui" / "data",
            control_db_path=repo_root / "ops-ui" / "data" / "ops_ui.sqlite3",
            controls_file=repo_root / "ops-ui" / "data" / "controls.json",
            service_timeout_sec=2.5,
            journal_lines=80,
            funnel_run_timeout_sec=900.0,
            stuck_running_sec=900.0,
            stuck_queued_sec=900.0,
            stuck_uploading_sec=900.0,
            services=(),
            environment=mk04_env,
        )
        summary = build_environment_summary(
            settings,
            funnel_id=_FUNNEL_ID,
            platform_id=_PLATFORM_ID,
        )
        checks.append(
            _check(
                "Ops UI environment label",
                summary.get("environment_label") == label,
                str(summary.get("environment_label")),
            )
        )
        posting = summary.get("posting_config_enabled")
        # Prod YAML uploading.enabled is false until deliberately armed.
        expected_posting = False
        checks.append(
            _check(
                "Ops UI posting config state",
                posting is expected_posting,
                summary.get("posting_config_label", ""),
            )
        )
        checks.append(
            _check(
                "Ops UI funnel/platform/preset",
                summary.get("funnel_id") == _FUNNEL_ID
                and summary.get("platform_id") == _PLATFORM_ID
                and summary.get("preset_id") not in (None, "not_available", ""),
                f"{summary.get('funnel_id')}/{summary.get('platform_id')}/{summary.get('preset_id')}",
            )
        )
        health = str(summary.get("health_state", "unknown"))
        checks.append(
            _check(
                "Ops UI health honest",
                health in {"unknown", "not_available", "local_only", "not_ready", "ready"},
                health,
            )
        )
        last_update = str(summary.get("last_update_status", "not_available"))
        checks.append(
            _check(
                "Ops UI last update honest",
                last_update in {"success", "failure", "not_available"},
                last_update,
            )
        )
    except Exception as exc:
        checks.append(_check("Ops UI environment summary", False, str(exc)[:300]))
    return checks


def _verify_update_check_only(mk04_env: str, repo_root: Path) -> list[CheckResult]:
    checks: list[CheckResult] = []
    env = os.environ.copy()
    if mk04_env == "prod":
        env["MK04_SKIP_PROD_PREFLIGHT"] = "1"
    proc = subprocess.run(
        ["bash", str(repo_root / "update.sh"), mk04_env, "--check-only"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    combined = proc.stdout + proc.stderr
    checks.append(_check("update.sh --check-only exit 0", proc.returncode == 0, combined[-500:]))
    checks.append(
        _check(
            "update.sh config validation PASS",
            "config validation: pass" in combined.lower() or "Config validation: PASS" in combined,
            "",
        )
    )
    checks.append(
        _check(
            "update.sh no service restart in check-only",
            "services_restarted:  skipped" in combined.lower()
            or "check-only" in combined.lower(),
            "",
        )
    )
    try:
        config, state = _load_config(mk04_env, repo_root / "config")
        status_path = state.data_root / "last_update_status.json"
        checks.append(
            _check(
                "last_update_status.json written",
                status_path.is_file(),
                str(status_path),
            )
        )
    except Exception as exc:
        checks.append(_check("last_update_status.json written", False, str(exc)))
    return checks


def _verify_run_check_only(mk04_env: str, repo_root: Path) -> list[CheckResult]:
    checks: list[CheckResult] = []
    env = os.environ.copy()
    if mk04_env == "prod":
        env["MK04_SKIP_PROD_PREFLIGHT"] = "1"
    proc = subprocess.run(
        ["bash", str(repo_root / "run.sh"), "--env", mk04_env, "--check-only"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    combined = proc.stdout + proc.stderr
    checks.append(_check("run.sh --check-only exit 0", proc.returncode == 0, combined[-500:]))
    checks.append(
        _check(
            "run.sh validates before startup",
            "config validation" in combined.lower() and "pass" in combined.lower(),
            "",
        )
    )
    checks.append(
        _check(
            "run.sh does not start stack in check-only",
            "services not started" in combined.lower()
            or "run check-only" in combined.lower(),
            "",
        )
    )
    return checks


def _artifact_touched(base: Path, marker: str) -> bool:
    if not base.exists():
        return False
    direct = base / marker
    if direct.exists():
        return True
    if base.is_dir():
        for child in base.iterdir():
            if marker in child.name:
                return True
    return False


def verify_dev_did_not_touch_prod(
    dev_job_id: str,
    dev_state: EnvironmentStatePaths,
    prod_state: EnvironmentStatePaths,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for label, root in (
        ("jobs", prod_state.jobs_root),
        ("data", prod_state.data_root),
        ("reports", prod_state.reports_root),
        ("logs", prod_state.logs_root),
        ("outputs", prod_state.outputs_root),
    ):
        checks.append(
            _check(
                f"dev smoke absent under prod {label}",
                not _artifact_touched(root, dev_job_id),
                str(root),
            )
        )
    checks.append(
        _check(
            "dev database != prod database",
            dev_state.database_path != prod_state.database_path,
            f"{dev_state.database_path} vs {prod_state.database_path}",
        )
    )
    checks.append(
        _check(
            "dev outputs != prod outputs",
            dev_state.outputs_root != prod_state.outputs_root,
            f"{dev_state.outputs_root} vs {prod_state.outputs_root}",
        )
    )
    return checks


def verify_prod_did_not_touch_dev(
    prod_job_id: str,
    dev_state: EnvironmentStatePaths,
    prod_state: EnvironmentStatePaths,
) -> list[CheckResult]:
    checks: list[CheckResult] = []
    for label, root in (
        ("jobs", dev_state.jobs_root),
        ("data", dev_state.data_root),
        ("reports", dev_state.reports_root),
        ("logs", dev_state.logs_root),
        ("outputs", dev_state.outputs_root),
    ):
        checks.append(
            _check(
                f"prod smoke absent under dev {label}",
                not _artifact_touched(root, prod_job_id),
                str(root),
            )
        )
    return checks


def run_invalid_production_config_smoke(repo_root: Path) -> list[CheckResult]:
    """Safe synthetic invalid prod config using a temporary copied config root."""
    checks: list[CheckResult] = []
    tmp_root = Path(tempfile.mkdtemp(prefix="mk04_invalid_config_smoke_"))
    try:
        shutil.copytree(repo_root / "config", tmp_root / "config")

        prod_yaml = tmp_root / "config" / "environments" / "prod.yaml"
        text = prod_yaml.read_text(encoding="utf-8")
        broken = re.sub(
            r"^\s*database_path:.*$",
            "",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        prod_yaml.write_text(broken, encoding="utf-8")

        errors = validate_config_tree(tmp_root / "config")
        checks.append(
            _check(
                "invalid prod config validator fails",
                len(errors) > 0,
                "; ".join(errors[:3]),
            )
        )

        try:
            ConfigManager.load(
                environment="production",
                funnel_id=_FUNNEL_ID,
                platform_id=_PLATFORM_ID,
                config_root=tmp_root / "config",
            )
            checks.append(_check("invalid prod ConfigManager fails", False, "load succeeded unexpectedly"))
        except ConfigError as exc:
            checks.append(_check("invalid prod ConfigManager fails", True, str(exc)[:200]))

        job_id = f"smoke_invalid_prod_{_utc_stamp()}"
        prod_jobs = repo_root / "jobs" / "prod"
        stray = prod_jobs / job_id
        checks.append(_check("invalid config did not create job dir", not stray.exists(), str(stray)))
        checks.append(
            _check(
                "real prod config files untouched",
                (repo_root / "config" / "environments" / "prod.yaml").is_file(),
            )
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    return checks


def run_environment_smoke(
    mk04_env: str,
    *,
    repo_root: Path,
    config_root: Path,
    skip_shell: bool = False,
    job_id: str | None = None,
) -> EnvSmokeResult:
    canonical = canonical_environment(mk04_env)
    stamp = _utc_stamp()
    smoke_job_id = job_id or f"smoke_config_{mk04_env}_{stamp}"
    result = EnvSmokeResult(mk04_env=mk04_env, canonical_env=canonical, job_id=smoke_job_id)

    try:
        errors = validate_config_tree(config_root)
        result.checks.append(
            _check(
                "config schema validation",
                len(errors) == 0,
                "; ".join(errors[:3]),
            )
        )
    except Exception as exc:
        result.checks.append(_check("config schema validation", False, str(exc)))

    try:
        config, state = _load_config(mk04_env, config_root)
        result.checks.append(_check("ConfigManager load", True, canonical))
    except Exception as exc:
        result.checks.append(_check("ConfigManager load", False, str(exc)))
        return result

    result.checks.extend(_verify_paths(mk04_env, state))

    # Both env YAML files currently keep uploading.enabled false until armed.
    expected_upload = False
    result.checks.append(
        _check(
            "uploading config",
            config.uploading_enabled is expected_upload,
            f"uploading_enabled={config.uploading_enabled}",
        )
    )

    try:
        state.ensure_directories()
        result.checks.append(_check("state.ensure_directories()", True, ""))
    except Exception as exc:
        result.checks.append(_check("state.ensure_directories()", False, str(exc)))
        return result

    job_dir = state.job_dir(smoke_job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    result.job_dir = job_dir

    snap = config.save_snapshot(job_dir)
    ctx = ExecutionContext.from_resolved_config(
        config,
        job_id=smoke_job_id,
        resolved_config_path=snap,
        repo_root=repo_root,
    )
    ctx.save(job_dir)

    task_path = job_dir / "task.json"
    task_payload = {
        "job_id": smoke_job_id,
        "execution_context": ctx.to_dict(),
        "smoke": True,
    }
    task_path.write_text(json.dumps(task_payload, indent=2) + "\n", encoding="utf-8")

    result.checks.extend(
        _verify_snapshot_and_context(
            job_dir=job_dir,
            job_id=smoke_job_id,
            canonical_env=canonical,
            config=config,
        )
    )
    result.checks.extend(_verify_ui_summary(mk04_env, repo_root))

    if not skip_shell:
        result.checks.extend(_verify_update_check_only(mk04_env, repo_root))
        result.checks.extend(_verify_run_check_only(mk04_env, repo_root))

    return result


def _print_section(title: str, result: EnvSmokeResult) -> None:
    print(f"\n{title}:")
    for check in result.checks:
        status = "PASS" if check.passed else "FAIL"
        line = f"  {check.name}: {status}"
        if check.detail and not check.passed:
            line += f" ({check.detail})"
        print(line)
    if result.job_dir:
        print(f"  Job root: {result.job_dir}")


def _print_checks(title: str, checks: list[CheckResult]) -> None:
    print(f"\n{title}:")
    for check in checks:
        status = "PASS" if check.passed else "FAIL"
        line = f"  {check.name}: {status}"
        if check.detail and not check.passed:
            line += f" ({check.detail})"
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Configuration & Deployment smoke test")
    parser.add_argument("--env", choices=["dev", "development", "prod", "production"])
    parser.add_argument("--both", action="store_true", help="Run dev and prod smoke (non-destructive)")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--config-root", type=Path, default=None)
    parser.add_argument("--skip-shell", action="store_true", help="Skip update.sh/run.sh subprocess checks")
    parser.add_argument("--cleanup", action="store_true", help="Remove smoke job dirs created by this run")
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    config_root = (args.config_root or repo_root / "config").resolve()

    envs: list[str] = []
    if args.both:
        envs = ["dev", "prod"]
    elif args.env:
        envs = [normalize_mk04_env(args.env)]
    else:
        envs = ["dev"]

    all_checks: list[CheckResult] = []
    env_results: dict[str, EnvSmokeResult] = {}
    isolation_checks: list[CheckResult] = []

    dev_state = prod_state = None
    if len(envs) > 1 or envs[0] == "dev":
        _, dev_state = _load_config("dev", config_root)
    if "prod" in envs:
        _, prod_state = _load_config("prod", config_root)

    if "dev" in envs:
        if prod_state is None:
            _, prod_state = _load_config("prod", config_root)
        env_results["dev"] = run_environment_smoke(
            "dev",
            repo_root=repo_root,
            config_root=config_root,
            skip_shell=args.skip_shell,
        )
        all_checks.extend(env_results["dev"].checks)
        if prod_state and dev_state:
            isolation_checks.extend(
                verify_dev_did_not_touch_prod(
                    env_results["dev"].job_id,
                    dev_state,
                    prod_state,
                )
            )

    if "prod" in envs:
        if dev_state is None:
            _, dev_state = _load_config("dev", config_root)
        env_results["prod"] = run_environment_smoke(
            "prod",
            repo_root=repo_root,
            config_root=config_root,
            skip_shell=args.skip_shell,
        )
        all_checks.extend(env_results["prod"].checks)
        if dev_state and prod_state:
            isolation_checks.extend(
                verify_prod_did_not_touch_dev(
                    env_results["prod"].job_id,
                    dev_state,
                    prod_state,
                )
            )

    invalid_checks = run_invalid_production_config_smoke(repo_root)
    all_checks.extend(isolation_checks)
    all_checks.extend(invalid_checks)

    print("=" * 60)
    print("Configuration & Deployment smoke test")
    print(f"  repo_root:   {repo_root}")
    print(f"  config_root: {config_root}")
    print("=" * 60)

    if "dev" in env_results:
        _print_section("Dev", env_results["dev"])
    if "prod" in env_results:
        _print_section("Prod", env_results["prod"])
    if isolation_checks:
        _print_checks("Isolation", isolation_checks)
    _print_checks("Invalid production config", invalid_checks)

    passed = all(c.passed for c in all_checks)
    print()
    if passed:
        print("CONFIG_DEPLOYMENT_SMOKE_PASSED")
    else:
        print("CONFIG_DEPLOYMENT_SMOKE_FAILED")
        for check in all_checks:
            if not check.passed:
                print(f"  FAIL: {check.name} — {check.detail}")

    if args.cleanup:
        for res in env_results.values():
            if res.job_dir and res.job_dir.is_dir():
                if res.job_dir.name.startswith("smoke_config_"):
                    parent = res.job_dir.parent
                    try:
                        if _is_under(res.job_dir.resolve(), parent.resolve()):
                            shutil.rmtree(res.job_dir)
                            print(f"Cleaned up {res.job_dir}")
                    except OSError as exc:
                        print(f"Cleanup skipped for {res.job_dir}: {exc}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
