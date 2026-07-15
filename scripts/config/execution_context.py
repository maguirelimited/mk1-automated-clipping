#!/usr/bin/env python3
"""
scripts/config/execution_context.py

ExecutionContext — a stable, immutable record attached to every job.

Purpose:
    Every job should carry a stable execution context from creation through
    reports, metadata, logs, and resolved config snapshots.

    This allows any artifact (report, clip metadata, post-processing output)
    to explain exactly where it came from:
        - which environment it ran in
        - which funnel / platform / preset config was active
        - what version of the code was running
        - which config snapshot applies

Usage:
    config  = ConfigManager.load(environment="dev")
    snap    = config.save_snapshot(job_dir)
    ctx     = ExecutionContext.from_resolved_config(config, job_id=job_id, resolved_config_path=snap)
    ctx.save(job_dir)          # writes execution_context.json
    ctx.to_dict()              # JSON-safe dict (no secrets)

The execution context is intentionally small. It is NOT:
    - a full config dump (see resolved_config.yaml for that)
    - a strategy engine choice
    - a copy of secrets

Python environment:
    Run with video-automation/.venv/bin/python
    PyYAML declared in video-automation/requirements.txt
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from environment_names import EnvironmentNameError, to_config_environment

if TYPE_CHECKING:
    from config_manager import ResolvedConfig

# ---------------------------------------------------------------------------
# Job ID validation (same safety rules as state_paths.py)
# ---------------------------------------------------------------------------

_SAFE_JOB_ID_RE = re.compile(r'^[A-Za-z0-9_\-\.]+$')


def _validate_job_id(job_id: str) -> str:
    if not job_id or not isinstance(job_id, str):
        raise ValueError("job_id must be a non-empty string")
    if "/" in job_id or "\\" in job_id:
        raise ValueError(f"job_id must not contain path separators: {job_id!r}")
    if ".." in job_id:
        raise ValueError(f"job_id must not contain '..' sequences: {job_id!r}")
    if not _SAFE_JOB_ID_RE.fullmatch(job_id):
        raise ValueError(
            f"job_id contains invalid characters: {job_id!r}. "
            "Allowed: alphanumeric, underscore, hyphen, dot."
        )
    if Path(job_id).is_absolute():
        raise ValueError(f"job_id must not be an absolute path: {job_id!r}")
    return job_id


# ---------------------------------------------------------------------------
# Git commit detection
# ---------------------------------------------------------------------------

def _detect_code_commit(repo_root: Path | str | None = None) -> str | None:
    """
    Return the short Git commit SHA for the current HEAD.

    Returns None if Git is unavailable or the directory is not a Git repo.
    Never raises — job creation must not fail due to Git being unavailable.
    """
    try:
        cwd = str(repo_root) if repo_root else None
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
        )
        sha = result.stdout.strip()
        if result.returncode == 0 and sha:
            return sha
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# ExecutionContext
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionContext:
    """
    Immutable execution context for a single job run.

    Fields:
        environment          "development" | "production"
        job_id               Unique job identifier (validated, no traversal)
        funnel_id            Active funnel config ID
        platform_id          Active platform config ID
        preset_id            Active preset config ID
        config_version       Version field from defaults/default.yaml, or "unknown"
        resolved_config_path Path to the saved resolved_config.yaml for this job
        code_commit          Short Git SHA of HEAD, or None if Git unavailable

    Does NOT contain:
        - secrets, tokens, API keys, passwords
        - full config dump (see resolved_config.yaml)
        - creative or business strategy decisions
    """
    environment: str
    job_id: str
    funnel_id: str
    platform_id: str
    preset_id: str
    config_version: str
    resolved_config_path: str
    code_commit: str | None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_resolved_config(
        cls,
        config: "ResolvedConfig",
        *,
        job_id: str,
        resolved_config_path: str | Path,
        repo_root: str | Path | None = None,
    ) -> "ExecutionContext":
        """
        Build an ExecutionContext from a ResolvedConfig.

        Args:
            config:                A loaded ResolvedConfig from ConfigManager.
            job_id:                The job identifier (will be validated).
            resolved_config_path:  Path to the saved resolved_config.yaml artifact.
            repo_root:             Repo root for git rev-parse (auto-detected if None).
        """
        _validate_job_id(job_id)

        version_raw = config.get("version")
        config_version = str(version_raw) if version_raw is not None else "unknown"

        commit = _detect_code_commit(repo_root)

        return cls(
            environment=config.environment,
            job_id=job_id,
            funnel_id=config.funnel_id,
            platform_id=config.platform_id,
            preset_id=config.preset_id,
            config_version=config_version,
            resolved_config_path=str(resolved_config_path),
            code_commit=commit,
        )

    @classmethod
    def minimal(
        cls,
        *,
        environment: str,
        job_id: str,
        resolved_config_path: str | Path = "",
        repo_root: str | Path | None = None,
    ) -> "ExecutionContext":
        """
        Build a minimal fallback context when ConfigManager is not available.

        Used in environments where the new config system has not been fully wired
        (e.g. tests that monkeypatch PIPELINE_CONFIG_PATH, legacy startup).

        Never fails. Falls back to safe defaults.
        """
        try:
            _validate_job_id(job_id)
        except ValueError:
            job_id = "unknown"

        commit = _detect_code_commit(repo_root)
        try:
            env_normalised = to_config_environment(environment)
        except EnvironmentNameError:
            # Preserve unknown tokens for diagnostics rather than inventing prod.
            env_normalised = environment

        return cls(
            environment=env_normalised,
            job_id=job_id,
            funnel_id="unknown",
            platform_id="unknown",
            preset_id="unknown",
            config_version="unknown",
            resolved_config_path=str(resolved_config_path),
            code_commit=commit,
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """
        Return a JSON-safe dict representation.

        Safe to log, persist, include in reports.
        Does not contain secrets.
        """
        return {
            "schema": "execution_context_v1",
            "environment": self.environment,
            "job_id": self.job_id,
            "funnel_id": self.funnel_id,
            "platform_id": self.platform_id,
            "preset_id": self.preset_id,
            "config_version": self.config_version,
            "resolved_config_path": self.resolved_config_path,
            "code_commit": self.code_commit,
        }

    def save(self, job_dir: str | Path) -> Path:
        """
        Save execution_context.json into job_dir.

        Creates parent directories if needed.
        Path-traversal guard: target must be inside resolved job_dir.

        Returns the path of the written file.
        """
        job_dir = Path(job_dir).resolve()
        target = (job_dir / "execution_context.json").resolve()

        try:
            target.relative_to(job_dir)
        except ValueError:
            raise ValueError(
                f"execution_context.json target {target} is outside "
                f"job_dir {job_dir}. Refusing to write."
            )

        job_dir.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.write("\n")

        return target

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, job_dir: str | Path) -> "ExecutionContext":
        """
        Load an execution context from a job directory.

        Raises FileNotFoundError if execution_context.json does not exist.
        Raises ValueError if the file is malformed.
        """
        path = Path(job_dir) / "execution_context.json"
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed execution_context.json at {path}: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(f"execution_context.json at {path} is not a JSON object")

        return cls(
            environment=str(data.get("environment") or "unknown"),
            job_id=str(data.get("job_id") or "unknown"),
            funnel_id=str(data.get("funnel_id") or "unknown"),
            platform_id=str(data.get("platform_id") or "unknown"),
            preset_id=str(data.get("preset_id") or "unknown"),
            config_version=str(data.get("config_version") or "unknown"),
            resolved_config_path=str(data.get("resolved_config_path") or ""),
            code_commit=data.get("code_commit"),
        )


# ---------------------------------------------------------------------------
# Resolved config loading errors
# ---------------------------------------------------------------------------


class ResolvedConfigLoadError(Exception):
    """Raised when resolved_config.yaml exists but cannot be safely loaded."""

    def __init__(self, message: str, *, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        super().__init__(message)


# ---------------------------------------------------------------------------
# Standalone helper for pipeline artifact use
# ---------------------------------------------------------------------------


def extract_conveyor_config_from_resolved(resolved: dict[str, Any]) -> dict[str, Any]:
    """
    Map resolved_config.yaml format/captions sections to flat conveyor module keys.

    Used by the pipeline to pass platform formatting and caption layout values into
    post-processing modules without those modules reading YAML directly.

    Only keys present in the resolved config are included.  Callers merge the
    result over legacy ``processing_settings`` defaults.

    Mapping (config → module key):
        format.width              → target_width
        format.height             → target_height
        format.aspect_ratio       → platform_aspect_ratio
        format.reframe_mode       → reframe_mode
        format.face_track_test_enabled → face_track_test_enabled
        format.max_duration_seconds → platform_max_duration_seconds
        format.title_max_length   → platform_title_max_length
        format.caption_max_length → platform_caption_max_length
        captions.safe_zone.*_px   → safe_zone_*_px
        captions.layout.*         → font_size, max_lines, etc.
    """
    out: dict[str, Any] = {}

    fmt = resolved.get("format")
    if isinstance(fmt, dict):
        if "width" in fmt:
            out["target_width"] = fmt["width"]
        if "height" in fmt:
            out["target_height"] = fmt["height"]
        if "aspect_ratio" in fmt:
            out["platform_aspect_ratio"] = fmt["aspect_ratio"]
        if "reframe_mode" in fmt:
            mode = fmt["reframe_mode"]
            if isinstance(mode, str) and mode.strip():
                out["reframe_mode"] = mode.strip()
        if "face_track_test_enabled" in fmt:
            value = fmt["face_track_test_enabled"]
            if isinstance(value, bool):
                out["face_track_test_enabled"] = value
            elif isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"1", "true", "yes", "on"}:
                    out["face_track_test_enabled"] = True
                elif normalized in {"0", "false", "no", "off"}:
                    out["face_track_test_enabled"] = False
        if "max_duration_seconds" in fmt:
            out["platform_max_duration_seconds"] = fmt["max_duration_seconds"]
        if "title_max_length" in fmt:
            out["platform_title_max_length"] = fmt["title_max_length"]
        if "caption_max_length" in fmt:
            out["platform_caption_max_length"] = fmt["caption_max_length"]

    captions = resolved.get("captions")
    if isinstance(captions, dict):
        safe_zone = captions.get("safe_zone")
        if isinstance(safe_zone, dict):
            for src, dst in (
                ("top_px", "safe_zone_top_px"),
                ("bottom_px", "safe_zone_bottom_px"),
                ("left_px", "safe_zone_left_px"),
                ("right_px", "safe_zone_right_px"),
            ):
                if src in safe_zone:
                    out[dst] = safe_zone[src]

        layout = captions.get("layout")
        if isinstance(layout, dict):
            for src, dst in (
                ("font_family", "font_family"),
                ("font_size", "font_size"),
                ("max_lines", "max_lines"),
                ("max_chars_per_line", "max_chars_per_line"),
                ("max_chars_per_caption", "max_chars_per_caption"),
            ):
                if src in layout:
                    out[dst] = layout[src]

    return out


def load_resolved_config_for_job(job_dir: str | Path) -> dict[str, Any] | None:
    """
    Load resolved_config.yaml from job_dir and return as a plain dict.

    This is the preferred source for behavioural config values (selection
    thresholds, conveyor module list, platform format values) inside the
    pipeline.  The resolved config was saved at job-creation time and
    represents the exact merged config that was active when the job was
    created.

    Source priority (per Prompt 6A spec):
        resolved_config.yaml in job directory   ← preferred
            ↓
        ConfigManager (re-load)                 ← only if snapshot absent
            ↓
        legacy defaults                         ← for old jobs

    Rules:
        - Returns a plain dict for jobs that have resolved_config.yaml.
        - Returns None silently for legacy jobs that predate the config system.
        - Raises ResolvedConfigLoadError when the file is present but broken.
        - Never mutates the filesystem.
        - Never reads secrets.
        - Never reconstructs config from multiple YAML files inside modules.

    Args:
        job_dir: Path to the job directory.

    Returns:
        A plain dict or None.

    Raises:
        ResolvedConfigLoadError: If the file is present but cannot be parsed safely.
    """
    path = Path(job_dir) / "resolved_config.yaml"
    if not path.exists():
        return None

    try:
        import yaml as _yaml
    except ImportError as exc:
        raise ResolvedConfigLoadError(
            f"Resolved config snapshot exists but is invalid: {path} "
            f"(PyYAML unavailable: {exc!r})",
            path=path,
        ) from exc

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ResolvedConfigLoadError(
            f"Resolved config snapshot exists but is invalid: {path} (unreadable: {exc!r})",
            path=path,
        ) from exc

    try:
        raw = _yaml.safe_load(text)
    except _yaml.YAMLError as exc:
        raise ResolvedConfigLoadError(
            f"Resolved config snapshot exists but is invalid: {path} (malformed YAML: {exc!r})",
            path=path,
        ) from exc

    if not isinstance(raw, dict):
        got = type(raw).__name__ if raw is not None else "null"
        raise ResolvedConfigLoadError(
            f"Resolved config snapshot exists but is invalid: {path} "
            f"(expected mapping, got {got})",
            path=path,
        )

    return raw


def load_execution_context_for_job(job_dir: str | Path) -> dict[str, Any] | None:
    """
    Load execution_context.json from job_dir and return as a JSON-safe dict.

    Designed for pipeline artifact writers that need to include execution
    provenance but must not crash on legacy jobs that predate the context system.

    Rules:
        - Returns a dict for new jobs with execution_context.json present.
        - Returns None for legacy jobs without the file (silent, not an error).
        - Returns None for malformed/unreadable files (logs a warning).
        - Never invents fake context.
        - Never reads config files to reconstruct context.
        - Never includes secrets.

    Args:
        job_dir: Path to the job directory.

    Returns:
        A JSON-safe dict or None.
    """
    path = Path(job_dir) / "execution_context.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        import sys as _sys
        print(
            f"[execution_context] WARNING: could not read {path}: {exc!r}. "
            "Omitting execution_context from artifact.",
            file=_sys.stderr,
            flush=True,
        )
        return None
    if not isinstance(data, dict):
        return None
    return data
