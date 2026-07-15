"""Canonical mutable-state path authority (Prompt 3).

Precedence (highest first):
  1. Explicit validated path override (MK04_*_ROOT / MK04_*_PATH env vars)
  2. Deployed environment/runtime configuration (MK04_RUNTIME_ROOT / MK04_LOG_ROOT)
  3. Environment YAML defaults (relative to repo/code root)
  4. Safe development-only fallback (never used for production)

Production must never reach step 4. Missing required runtime roots in a
deployed production context fail closed.

Development hybrid (when MK04_RUNTIME_ROOT is set via deploy/env.sh):
  - jobs/outputs → /var/lib/mk04/dev/video-automation/{jobs,output}
  - data/runs/reports/database/control → repository paths (unless overridden)

Production (when MK04_RUNTIME_ROOT is set):
  - all mutable categories resolve under /var/lib/mk04/prod (logs under
    /var/log/mk04/prod via MK04_LOG_ROOT)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PathAuthorityError(RuntimeError):
    """Path resolution failed closed."""


# Explicit override env vars (step 1).
ENV_DATA_ROOT = "MK04_DATA_ROOT"
ENV_JOBS_ROOT = "MK04_JOBS_ROOT"
ENV_OUTPUTS_ROOT = "MK04_OUTPUTS_ROOT"
ENV_RUNS_ROOT = "MK04_RUNS_ROOT"
ENV_REPORTS_ROOT = "MK04_REPORTS_ROOT"
ENV_LOGS_ROOT = "MK04_LOGS_ROOT"  # also accepts MK04_LOG_ROOT
ENV_DATABASE_PATH = "MK04_DATABASE_PATH"
ENV_CONTROL_STATE_FILE = "MK04_CONTROL_STATE_FILE"
ENV_RUNTIME_ROOT = "MK04_RUNTIME_ROOT"
ENV_LOG_ROOT = "MK04_LOG_ROOT"


@dataclass(frozen=True)
class CanonicalPaths:
    """Absolute resolved mutable-state paths for one environment."""

    environment: str  # development | production
    data_root: Path
    jobs_root: Path
    outputs_root: Path
    runs_root: Path
    reports_root: Path
    logs_root: Path
    database_path: Path
    control_state_file: Path
    source: str  # short description of which authority layer won

    def as_dict(self) -> dict[str, str]:
        return {
            "environment": self.environment,
            "data_root": str(self.data_root),
            "jobs_root": str(self.jobs_root),
            "outputs_root": str(self.outputs_root),
            "runs_root": str(self.runs_root),
            "reports_root": str(self.reports_root),
            "logs_root": str(self.logs_root),
            "database_path": str(self.database_path),
            "control_state_file": str(self.control_state_file),
            "source": self.source,
        }


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _is_production(environment: str) -> bool:
    return str(environment).strip().lower() in {"production", "prod"}


def _finalized_release_leaf(path: Path) -> str | None:
    """Return the releases/<leaf> name when path is exactly that directory.

    Only finalized release directories count. Pre-finalization promotion snapshots
    such as ``releases/.staging-<id>`` (and any other ``releases/.*`` name) are
    not finalized leaves.
    """
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return None
    text = str(resolved).replace("\\", "/")
    prefix = "/opt/mk04/prod/releases/"
    if not text.startswith(prefix):
        return None
    leaf = text[len(prefix) :]
    if not leaf or "/" in leaf:
        return None
    if leaf.startswith("."):
        return None
    return leaf


def _is_finalized_production_release_dir(path: Path) -> bool:
    """True for ``/opt/mk04/prod/releases/<finalized-id>`` only (not ``.staging-*``)."""
    return _finalized_release_leaf(path) is not None


def _deployed_production_context(repo_root: Path) -> bool:
    """True when this looks like an activated / deployed production code root.

    Classification:
    - ``/opt/mk04/prod/current`` (logical) → deployed
    - ``current`` resolving to a finalized ``releases/<id>`` → deployed
    - finalized ``releases/<id>`` (no leading ``.``) → deployed
    - ``releases/.staging-<id>`` (and other ``releases/.*``) → NOT deployed
    - checkouts / paths outside the production release tree → NOT deployed
      (unless ``MK04_REQUIRE_RUNTIME_PATHS`` is set)
    """
    try:
        resolved = repo_root.resolve()
    except OSError:
        return False
    text = str(resolved).replace("\\", "/")
    if text == "/opt/mk04/prod/current":
        return True
    if _is_finalized_production_release_dir(resolved):
        return True
    try:
        current = Path("/opt/mk04/prod/current")
        if current.exists() and current.resolve() == resolved:
            # Only when current points at a finalized release (never .staging-*).
            return _is_finalized_production_release_dir(resolved)
    except OSError:
        pass
    code = os.environ.get("MK04_CODE_ROOT", "").strip()
    if code:
        try:
            code_path = Path(code).expanduser()
            code_text = str(code_path).replace("\\", "/").rstrip("/")
            if code_text == "/opt/mk04/prod/current":
                # Env advertises logical current — only when this root is that entry
                # or the active finalized release it resolves to.
                if text == "/opt/mk04/prod/current":
                    return True
                try:
                    current = Path("/opt/mk04/prod/current")
                    if current.exists() and current.resolve() == resolved:
                        return _is_finalized_production_release_dir(resolved)
                except OSError:
                    pass
            elif code_path.resolve() == resolved:
                if text == "/opt/mk04/prod/current" or _is_finalized_production_release_dir(
                    resolved
                ):
                    return True
        except OSError:
            pass
    return os.environ.get("MK04_REQUIRE_RUNTIME_PATHS", "").strip() in {"1", "true", "yes"}


def _reject_repo_prod_fallback(
    *,
    environment: str,
    path: Path,
    repo_root: Path,
    label: str,
) -> None:
    if not _is_production(environment):
        return
    try:
        repo = repo_root.resolve()
        resolved = path.resolve()
    except OSError as exc:
        raise PathAuthorityError(f"{label}: cannot resolve path: {exc}") from exc

    # Forbidden silent fallbacks into the code tree's prod YAML defaults.
    forbidden_suffixes = (
        Path("data") / "prod",
        Path("jobs") / "prod",
        Path("runs") / "prod",
        Path("outputs") / "prod",
        Path("reports") / "prod",
        Path("logs") / "prod",
        Path("database") / "prod.db",
    )
    for suffix in forbidden_suffixes:
        candidate = (repo / suffix).resolve()
        if resolved == candidate or candidate in resolved.parents or resolved == candidate:
            # Only reject when runtime root was expected / deployed, or path is
            # exactly the repo YAML default while MK04_RUNTIME_ROOT is set.
            runtime = _env_path(ENV_RUNTIME_ROOT)
            if runtime is not None or _deployed_production_context(repo_root):
                raise PathAuthorityError(
                    f"production {label} must not use repository fallback {candidate}; "
                    f"got {resolved}"
                )


def resolve_canonical_paths(
    *,
    environment: str,
    repo_root: Path,
    yaml_data_root: Path,
    yaml_jobs_root: Path,
    yaml_outputs_root: Path,
    yaml_logs_root: Path,
    yaml_reports_root: Path,
    yaml_database_path: Path,
    yaml_runs_root: Path | None = None,
) -> CanonicalPaths:
    """
    Apply the path-authority contract to YAML-resolved paths.

    ``yaml_*`` paths are already absolute (ConfigManager step 3 defaults).
    """
    prod = _is_production(environment)
    token = "prod" if prod else "dev"
    repo_root = repo_root.resolve()

    default_runs = yaml_runs_root or (repo_root / "runs" / token).resolve()

    # --- Step 1: explicit overrides ---
    data = _env_path(ENV_DATA_ROOT)
    jobs = _env_path(ENV_JOBS_ROOT)
    outputs = _env_path(ENV_OUTPUTS_ROOT)
    runs = _env_path(ENV_RUNS_ROOT)
    reports = _env_path(ENV_REPORTS_ROOT)
    logs = _env_path(ENV_LOGS_ROOT) or _env_path(ENV_LOG_ROOT)
    database = _env_path(ENV_DATABASE_PATH)
    control = _env_path(ENV_CONTROL_STATE_FILE)
    runtime = _env_path(ENV_RUNTIME_ROOT)

    sources: list[str] = []

    # --- Step 2: deployed runtime layout ---
    if runtime is not None:
        video = (runtime / "video-automation").resolve()
        if jobs is None:
            jobs = (video / "jobs").resolve()
            sources.append("runtime:jobs")
        if outputs is None:
            outputs = (video / "output").resolve()
            sources.append("runtime:outputs")

        if prod:
            # Strict production: mutable orchestration under runtime root.
            if data is None:
                data = (runtime / "data").resolve()
                sources.append("runtime:data")
            if runs is None:
                runs = (runtime / "runs").resolve()
                sources.append("runtime:runs")
            if reports is None:
                reports = (runtime / "reports").resolve()
                sources.append("runtime:reports")
            if database is None:
                database = (runtime / "database" / "prod.db").resolve()
                sources.append("runtime:database")
            if logs is None:
                log_root = _env_path(ENV_LOG_ROOT)
                logs = (log_root if log_root is not None else (runtime / "logs")).resolve()
                sources.append("runtime:logs")
        else:
            # Hybrid development: keep YAML/repo orchestration defaults.
            sources.append("runtime:jobs+outputs(hybrid)")

    elif prod and _deployed_production_context(repo_root):
        raise PathAuthorityError(
            "MK04_RUNTIME_ROOT is required for deployed production path resolution"
        )

    # --- Step 3: YAML defaults ---
    if data is None:
        data = yaml_data_root.resolve()
        sources.append("yaml:data")
    if jobs is None:
        jobs = yaml_jobs_root.resolve()
        sources.append("yaml:jobs")
    if outputs is None:
        outputs = yaml_outputs_root.resolve()
        sources.append("yaml:outputs")
    if runs is None:
        runs = default_runs.resolve()
        sources.append("yaml:runs")
    if reports is None:
        reports = yaml_reports_root.resolve()
        sources.append("yaml:reports")
    if logs is None:
        logs = yaml_logs_root.resolve()
        sources.append("yaml:logs")
    if database is None:
        database = yaml_database_path.resolve()
        sources.append("yaml:database")

    if control is None:
        control = (data / "control_state.json").resolve()
        sources.append("derived:control_state")

    # Production: reject repository fallbacks when runtime/deployed context applies.
    for label, path in (
        ("data_root", data),
        ("jobs_root", jobs),
        ("outputs_root", outputs),
        ("runs_root", runs),
        ("reports_root", reports),
        ("logs_root", logs),
        ("database_path", database),
    ):
        _reject_repo_prod_fallback(
            environment=environment,
            path=path,
            repo_root=repo_root,
            label=label,
        )

    def _under(root: Path, path: Path) -> bool:
        resolved = path.resolve()
        root_r = root.resolve()
        return resolved == root_r or root_r in resolved.parents

    if prod and runtime is not None:
        for label, path in (
            ("data_root", data),
            ("jobs_root", jobs),
            ("outputs_root", outputs),
            ("runs_root", runs),
            ("reports_root", reports),
            ("database_path", database),
            ("control_state_file", control),
        ):
            if not _under(runtime, path):
                raise PathAuthorityError(
                    f"production {label}={path.resolve()} must be under runtime root {runtime}"
                )
        log_env = _env_path(ENV_LOG_ROOT)
        if log_env is not None:
            if not _under(log_env, logs):
                raise PathAuthorityError(
                    f"production logs_root={logs.resolve()} must be under MK04_LOG_ROOT={log_env}"
                )
        elif not _under(runtime, logs):
            raise PathAuthorityError(
                f"production logs_root={logs.resolve()} must be under runtime root {runtime}"
            )

    return CanonicalPaths(
        environment="production" if prod else "development",
        data_root=data,
        jobs_root=jobs,
        outputs_root=outputs,
        runs_root=runs,
        reports_root=reports,
        logs_root=logs,
        database_path=database,
        control_state_file=control,
        source="+".join(sources) if sources else "yaml",
    )


def control_state_path_for_env(
    environment: str,
    *,
    repo_root: Path | None = None,
    data_root: Path | None = None,
) -> Path:
    """Resolve control_state.json using the path authority contract."""
    explicit = _env_path(ENV_CONTROL_STATE_FILE)
    if explicit is not None:
        return explicit
    if data_root is not None:
        return (data_root / "control_state.json").resolve()

    # Lightweight resolution without full ConfigManager (ops scripts / funnel).
    root = repo_root
    if root is None:
        # Walk for config/ like ConfigManager.
        here = Path(__file__).resolve()
        for parent in [here, *here.parents]:
            if (parent / "config" / "environments" / "dev.yaml").exists():
                root = parent
                break
        if root is None:
            root = Path(__file__).resolve().parents[2]

    prod = _is_production(environment)
    token = "prod" if prod else "dev"
    data_override = _env_path(ENV_DATA_ROOT)
    if data_override is not None:
        return (data_override / "control_state.json").resolve()
    runtime = _env_path(ENV_RUNTIME_ROOT)
    if prod and runtime is not None:
        return (runtime / "data" / "control_state.json").resolve()
    return (root / "data" / token / "control_state.json").resolve()


def pipeline_jobs_output_from_config(pipeline_config: dict[str, Any]) -> tuple[Path, Path]:
    """Extract absolute jobs/output folders from a pipeline_config.json dict."""
    paths = pipeline_config.get("paths") if isinstance(pipeline_config, dict) else None
    if not isinstance(paths, dict):
        raise PathAuthorityError("pipeline config missing paths mapping")
    jobs = paths.get("jobs_folder") or paths.get("jobs")
    output = paths.get("output_folder") or paths.get("output")
    if not jobs or not output:
        raise PathAuthorityError("pipeline config missing jobs_folder/output_folder")
    return Path(str(jobs)).expanduser().resolve(), Path(str(output)).expanduser().resolve()


def assert_pipeline_config_agrees(
    *,
    config_manager_jobs: Path,
    config_manager_outputs: Path,
    pipeline_jobs: Path,
    pipeline_outputs: Path,
    production: bool,
) -> None:
    """Require ConfigManager and PIPELINE_CONFIG_PATH roots to match."""
    cj = config_manager_jobs.resolve()
    co = config_manager_outputs.resolve()
    pj = pipeline_jobs.resolve()
    po = pipeline_outputs.resolve()
    mismatches: list[str] = []
    if cj != pj:
        mismatches.append(f"jobs: ConfigManager={cj} pipeline={pj}")
    if co != po:
        mismatches.append(f"outputs: ConfigManager={co} pipeline={po}")
    if not mismatches:
        return
    msg = "PIPELINE_CONFIG_PATH and ConfigManager path mismatch: " + "; ".join(mismatches)
    if production:
        raise PathAuthorityError(msg)
    raise PathAuthorityError(msg)
