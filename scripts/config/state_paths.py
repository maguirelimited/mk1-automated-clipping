#!/usr/bin/env python3
"""
scripts/config/state_paths.py

EnvironmentStatePaths — typed, environment-scoped operational state paths.

Builds from a ResolvedConfig and acts as the single authoritative source of
truth for where jobs, outputs, logs, reports, clips, transcripts, caches, and
the database live for a given environment.

Safety contract:
    dev  state paths are only ever under dev-scoped roots
    prod state paths are only ever under prod-scoped roots
    dev  must never write to any prod path
    prod must never write to any dev path
    path traversal cannot escape environment roots

Usage:
    config = ConfigManager.load(environment="dev")
    state  = EnvironmentStatePaths.from_resolved_config(config)

    state.jobs_root          # Path
    state.job_dir("job_001") # Path  (validates the job_id)
    state.assert_within_environment(some_path)  # raises if outside env roots
    state.ensure_directories()  # creates env-scoped dirs (only call explicitly)

Upload state documentation (Prompt 3 ambiguity clarification):
    ConfigManager exposes config-level upload state only.

    Final intended precedence for effective real posting:
        config uploading.enabled AND NOT runtime kill switch (data/<env>/control_state.json)

    Runtime kill switch is implemented via scripts/ops/disable-uploads.sh and
    output-funnel runtime_upload_control.py. ConfigManager does not read
    control_state.json — config and runtime layers stay separate.

    This means: if the environment says uploads are enabled, a platform config
    with uploading.enabled: false does NOT block uploads — it is a lower-priority
    default that the environment overrides. Platform upload state is relevant only
    when the environment layer does not specify it explicitly.

    Do not add logic that allows platform uploading.enabled: false to veto the
    environment layer unless a future plan deliberately inverts this precedence.

Run with: video-automation/.venv/bin/python
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config_manager import ResolvedConfig

# ---------------------------------------------------------------------------
# Path-traversal safety
# ---------------------------------------------------------------------------

# Reject job IDs that contain path separators or look like traversal attempts.
# Allows alphanumeric, underscores, hyphens, and dots (for date-formatted IDs).
# Does NOT allow slashes, backslashes, or sequences like "..".
_SAFE_JOB_ID_RE = re.compile(r'^[A-Za-z0-9_\-\.]+$')


def _validate_job_id(job_id: str) -> str:
    """Validate and return a safe job_id, raising ValueError on invalid input."""
    if not job_id:
        raise ValueError("job_id must not be empty")
    if not isinstance(job_id, str):
        raise ValueError(f"job_id must be a string, got {type(job_id).__name__}")
    if "/" in job_id or "\\" in job_id:
        raise ValueError(
            f"job_id must not contain path separators: {job_id!r}"
        )
    if ".." in job_id:
        raise ValueError(
            f"job_id must not contain '..' sequences: {job_id!r}"
        )
    if not _SAFE_JOB_ID_RE.fullmatch(job_id):
        raise ValueError(
            f"job_id contains invalid characters: {job_id!r}. "
            "Allowed: alphanumeric, underscore, hyphen, dot."
        )
    if Path(job_id).is_absolute():
        raise ValueError(f"job_id must not be an absolute path: {job_id!r}")
    return job_id


# ---------------------------------------------------------------------------
# EnvironmentStatePaths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvironmentStatePaths:
    """
    Typed, immutable, environment-scoped operational state paths.

    All paths are absolute and resolved. The environment field determines
    which roots are allowed — dev paths never overlap prod paths.

    Path authority: ConfigManager → runtime_paths.resolve_canonical_paths
    (explicit overrides → MK04_RUNTIME_ROOT → YAML → dev-only fallbacks).
    """

    environment: str       # "development" | "production"
    data_root: Path
    jobs_root: Path
    outputs_root: Path
    logs_root: Path
    reports_root: Path
    database_path: Path
    runs_root: Path
    control_state_file: Path
    clips_root: Path
    transcripts_root: Path
    caches_root: Path

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_resolved_config(cls, config: "ResolvedConfig") -> "EnvironmentStatePaths":
        """
        Build from a ResolvedConfig (produced by ConfigManager.load()).

        Derives clips, transcripts, and caches from the existing resolved paths
        rather than adding new config keys:

            clips_root       = outputs_root / "clips"
            transcripts_root = data_root / "transcripts"
            caches_root      = data_root / "cache"
        """
        p = config.paths
        runs = getattr(p, "runs_root", None)
        control = getattr(p, "control_state_file", None)
        if runs is None:
            token = "prod" if config.environment == "production" else "dev"
            runs = (config._repo_root / "runs" / token).resolve()  # noqa: SLF001
        if control is None:
            control = (p.data_root / "control_state.json").resolve()
        return cls(
            environment=config.environment,
            data_root=p.data_root,
            jobs_root=p.jobs_root,
            outputs_root=p.outputs_root,
            logs_root=p.logs_root,
            reports_root=p.reports_root,
            database_path=p.database_path,
            runs_root=Path(runs),
            control_state_file=Path(control),
            clips_root=p.outputs_root / "clips",
            transcripts_root=p.data_root / "transcripts",
            caches_root=p.data_root / "cache",
        )

    # ------------------------------------------------------------------
    # Environment scope guard
    # ------------------------------------------------------------------

    @property
    def _allowed_roots(self) -> tuple[Path, ...]:
        """
        All directory roots that are owned by this environment.

        Any path that should be written by this environment must fall under
        one of these roots (or be the database_path itself).
        """
        return (
            self.data_root,
            self.jobs_root,
            self.outputs_root,
            self.logs_root,
            self.reports_root,
            self.runs_root,
            self.database_path.parent,
        )

    def is_within_environment(self, path: Path) -> bool:
        """
        Return True if path is inside one of this environment's allowed roots.

        Resolves the path to its real absolute form before checking,
        so symlinks cannot escape the boundary.
        """
        resolved = Path(path).resolve()
        return any(
            _is_under(resolved, root) for root in self._allowed_roots
        )

    def assert_within_environment(self, path: Path) -> Path:
        """
        Assert path is within this environment's roots.

        Returns the resolved path on success.
        Raises ValueError with a clear message on failure.
        """
        resolved = Path(path).resolve()
        if not self.is_within_environment(resolved):
            env_label = "development" if self.environment == "development" else "production"
            roots = ", ".join(str(r) for r in self._allowed_roots)
            raise ValueError(
                f"Unsafe state path for {env_label}: "
                f"{resolved} is outside allowed {env_label} roots "
                f"({roots})."
            )
        return resolved

    # ------------------------------------------------------------------
    # Directory creation (only through explicit call)
    # ------------------------------------------------------------------

    def ensure_directories(self) -> None:
        """
        Create all environment-scoped state directories.

        Must be called explicitly — ConfigManager.load() does NOT call this.
        Only creates directories under this environment's roots.

        Created directories:
            data_root
            jobs_root
            outputs_root
            logs_root
            reports_root
            clips_root
            transcripts_root
            caches_root
            database_path.parent   (but not the database file itself)
        """
        dirs = [
            self.data_root,
            self.jobs_root,
            self.outputs_root,
            self.logs_root,
            self.reports_root,
            self.runs_root,
            self.clips_root,
            self.transcripts_root,
            self.caches_root,
            self.database_path.parent,
        ]
        for d in dirs:
            # Safety check: every directory must be inside an allowed root.
            # This prevents a misconfigured path from creating dirs elsewhere.
            self.assert_within_environment(d)
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Job-specific path helpers
    # ------------------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        """
        Return the directory for a specific job.

        job_id must be a safe, non-traversal identifier.
        The resulting path is validated to be inside jobs_root.

        Raises ValueError for invalid job_id or path-traversal attempts.
        """
        _validate_job_id(job_id)
        target = (self.jobs_root / job_id).resolve()
        # Guard: must stay inside jobs_root
        if not _is_under(target, self.jobs_root):
            raise ValueError(
                f"Resolved job path {target} escapes jobs_root {self.jobs_root}. "
                f"Refusing unsafe job ID: {job_id!r}"
            )
        return target

    def report_dir(self, job_id: str | None = None) -> Path:
        """
        Return the reports directory, optionally scoped to a job_id.

        If job_id is provided it is validated before use.
        """
        if job_id is None:
            return self.reports_root
        _validate_job_id(job_id)
        target = (self.reports_root / job_id).resolve()
        if not _is_under(target, self.reports_root):
            raise ValueError(
                f"Resolved report path {target} escapes reports_root {self.reports_root}."
            )
        return target

    def output_dir(self, job_id: str | None = None) -> Path:
        """
        Return the outputs directory, optionally scoped to a job_id.
        """
        if job_id is None:
            return self.outputs_root
        _validate_job_id(job_id)
        target = (self.outputs_root / job_id).resolve()
        if not _is_under(target, self.outputs_root):
            raise ValueError(
                f"Resolved output path {target} escapes outputs_root {self.outputs_root}."
            )
        return target

    def log_file(self, name: str) -> Path:
        """
        Return the path for a named log file inside logs_root.

        name must not contain path separators or traversal sequences.
        """
        if not name:
            raise ValueError("log file name must not be empty")
        if "/" in name or "\\" in name:
            raise ValueError(f"log file name must not contain path separators: {name!r}")
        if ".." in name:
            raise ValueError(f"log file name must not contain '..' sequences: {name!r}")
        target = (self.logs_root / name).resolve()
        if not _is_under(target, self.logs_root):
            raise ValueError(
                f"Resolved log path {target} escapes logs_root {self.logs_root}."
            )
        return target

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def as_table(self) -> str:
        """Return a human-readable state path table for this environment."""
        rows = [
            ("State type",     "Path"),
            ("-" * 16,         "-" * 60),
            ("data_root",      str(self.data_root)),
            ("jobs_root",      str(self.jobs_root)),
            ("outputs_root",   str(self.outputs_root)),
            ("logs_root",      str(self.logs_root)),
            ("reports_root",   str(self.reports_root)),
            ("runs_root",      str(self.runs_root)),
            ("database",       str(self.database_path)),
            ("control_state",  str(self.control_state_file)),
            ("clips",          str(self.clips_root)),
            ("transcripts",    str(self.transcripts_root)),
            ("caches",         str(self.caches_root)),
        ]
        return "\n".join(f"  {label:<18} {value}" for label, value in rows)

    def as_dict(self) -> dict[str, str]:
        return {
            "environment":     self.environment,
            "data_root":       str(self.data_root),
            "jobs_root":       str(self.jobs_root),
            "outputs_root":    str(self.outputs_root),
            "logs_root":       str(self.logs_root),
            "reports_root":    str(self.reports_root),
            "runs_root":       str(self.runs_root),
            "database_path":   str(self.database_path),
            "control_state_file": str(self.control_state_file),
            "clips_root":      str(self.clips_root),
            "transcripts_root": str(self.transcripts_root),
            "caches_root":     str(self.caches_root),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_under(path: Path, root: Path) -> bool:
    """
    Return True if path is equal to root or a descendant of root.

    Both path and root should be resolved absolute paths.
    Uses Path.relative_to() which never follows symlinks — the resolved
    absolute path is what matters, not the on-disk target of any symlink.
    """
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
