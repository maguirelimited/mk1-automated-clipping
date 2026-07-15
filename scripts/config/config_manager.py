#!/usr/bin/env python3
"""
scripts/config/config_manager.py

ConfigManager — the single normal interface for loading the file-based
production infrastructure configuration.

Usage (CLI):
    python scripts/config/config_manager.py --env dev --funnel business --platform youtube --print-summary
    python scripts/config/config_manager.py --env prod --funnel business --platform youtube --print-summary

Usage (Python):
    from config_manager import ConfigManager
    resolved = ConfigManager.load(environment="dev", funnel_id="business", platform_id="youtube")
    print(resolved.uploading_enabled)
    print(resolved.get("selection.max_clips"))

Python environment:
    Run with video-automation/.venv/bin/python (has PyYAML 6.0).
    PyYAML is declared in video-automation/requirements.txt.

Responsibilities:
    - Resolve environment (explicit arg > MK04_ENV > default: development)
    - Load and deep-merge config layers in order:
        defaults → system → environment → funnel → platform → preset
    - Validate the config tree and merged storage policy via validate_config.py
    - Expose a ResolvedConfig object with typed accessors
    - Save per-job resolved config snapshots

Not responsible for:
    - Choosing funnels, monetisation strategy, or creative direction
    - Runtime upload kill switch (future: data/<env>/control_state.json)
    - Storage deletion / retention execution
    - Scheduler control
    - Mutating UI controls
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    print(
        "ERROR: PyYAML is not installed.\n"
        "Run with: video-automation/.venv/bin/python scripts/config/config_manager.py",
        file=sys.stderr,
    )
    sys.exit(2)

# ---------------------------------------------------------------------------
# Import shared validation logic
# ---------------------------------------------------------------------------

_SCRIPTS_CONFIG = Path(__file__).resolve().parent
if str(_SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_CONFIG))

from validate_config import validate_config_tree, validate_storage_policy  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

from environment_names import (  # noqa: E402
    ACCEPTED_ALIASES as _SAFE_ENVS,
    EnvironmentNameError,
    to_config_environment,
)

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or is invalid."""


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------


def _deep_merge(base: Any, override: Any) -> Any:
    """
    Recursively merge override into base.

    Rules:
      - dict + dict  → recurse into each key
      - list in override → replace (not append)
      - scalar in override → replace
      - override None → preserves base value (explicit null treated as absent)

    Returns a new object; does not mutate inputs.
    """
    if isinstance(base, dict) and isinstance(override, dict):
        result = copy.deepcopy(base)
        for k, v in override.items():
            if v is None and k in result:
                # Explicit null in override preserves the base value rather than
                # wiping it. This prevents a platform file with `enabled: null`
                # from accidentally disabling an already-set default.
                continue
            if k in result:
                result[k] = _deep_merge(result[k], v)
            else:
                result[k] = copy.deepcopy(v)
        return result
    # For scalars and lists: override replaces base entirely.
    return copy.deepcopy(override) if override is not None else copy.deepcopy(base)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _find_repo_root() -> Path:
    """
    Walk up from this script to find the repo root (contains config/).
    Raises ConfigError if not found.
    """
    candidate = Path(__file__).resolve()
    for parent in [candidate, *candidate.parents]:
        if (parent / "config" / "environments" / "dev.yaml").exists():
            return parent
    raise ConfigError(
        "Cannot locate repo root (looked for config/environments/dev.yaml). "
        "Run from inside the repository."
    )


def _resolve_path(raw: str, repo_root: Path) -> Path:
    """
    Resolve a raw config path value to an absolute path.
    If already absolute, return as-is.
    Otherwise, resolve relative to repo_root.
    """
    p = Path(raw)
    return p if p.is_absolute() else (repo_root / p).resolve()


def _runtime_root() -> Path | None:
    """Deployed runtime data root (see deploy/env/*/env.example)."""
    raw = os.environ.get("MK04_RUNTIME_ROOT", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


# ---------------------------------------------------------------------------
# YAML loading helpers
# ---------------------------------------------------------------------------


def _load_yaml_file(path: Path) -> dict:
    """Load a YAML file and return the parsed dict. Raises ConfigError on failure."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"{path}: expected a YAML mapping at top level, got {type(data).__name__}"
        )
    return data


# ---------------------------------------------------------------------------
# ResolvedConfig
# ---------------------------------------------------------------------------


@dataclass
class ResolvedPaths:
    """Absolute resolved filesystem paths from the merged config."""
    data_root: Path
    jobs_root: Path
    outputs_root: Path
    logs_root: Path
    reports_root: Path
    database_path: Path
    runs_root: Path
    control_state_file: Path

    def as_dict(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.__dict__.items()}


def _apply_runtime_path_overrides(
    paths: ResolvedPaths,
    *,
    environment: str,
    repo_root: Path,
) -> ResolvedPaths:
    """
    Apply the canonical path-authority contract (see runtime_paths.py).

    Precedence: explicit MK04_* overrides → MK04_RUNTIME_ROOT layout → YAML.
    Production never uses development-only repository fallbacks.
    """
    from runtime_paths import PathAuthorityError, resolve_canonical_paths  # noqa: PLC0415

    try:
        canonical = resolve_canonical_paths(
            environment=environment,
            repo_root=repo_root,
            yaml_data_root=paths.data_root,
            yaml_jobs_root=paths.jobs_root,
            yaml_outputs_root=paths.outputs_root,
            yaml_logs_root=paths.logs_root,
            yaml_reports_root=paths.reports_root,
            yaml_database_path=paths.database_path,
            yaml_runs_root=paths.runs_root,
        )
    except PathAuthorityError as exc:
        raise ConfigError(str(exc)) from exc
    return ResolvedPaths(
        data_root=canonical.data_root,
        jobs_root=canonical.jobs_root,
        outputs_root=canonical.outputs_root,
        logs_root=canonical.logs_root,
        reports_root=canonical.reports_root,
        database_path=canonical.database_path,
        runs_root=canonical.runs_root,
        control_state_file=canonical.control_state_file,
    )


@dataclass
class ResolvedConfig:
    """
    The result of ConfigManager.load().

    Provides typed accessors over the merged configuration and supports
    per-job snapshot saving.

    Upload effective state precedence (documented; kill-switch not yet implemented):
        runtime kill switch (data/<env>/control_state.json)   [FUTURE — Remote Administration]
            overrides
        environment uploading.enabled
            overrides
        platform uploading.enabled

    This precedence is implemented directly in the `uploading_enabled` property
    using the stored environment layer, rather than reading from the mechanical
    merge output (which gives platform the last word due to merge order).
    """

    environment: str         # "development" | "production"
    funnel_id: str
    platform_id: str
    preset_id: str
    data: dict               # fully merged config dict
    paths: ResolvedPaths
    _config_root: Path = field(repr=False)
    _repo_root: Path = field(repr=False)
    _env_layer: dict = field(repr=False)        # raw environment config layer
    _platform_layer: dict = field(repr=False)   # raw platform config layer

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def uploading_enabled(self) -> bool:
        """
        Config-level effective upload state.

        Precedence (config layer only — runtime kill switch not yet implemented):
            environment uploading.enabled      ← highest priority at config level
                overrides
            platform uploading.enabled
                overrides
            defaults uploading.enabled

        We read from the stored environment layer directly rather than the
        merged dict, because the mechanical merge gives platform the last word
        (platform is loaded after environment). The environment must win.

        The runtime kill switch (data/<env>/control_state.json, written by
        disable-uploads.sh) sits above all of these and will be implemented
        in the Remote Administration prompt.
        """
        # 1. Environment layer takes priority
        env_v = self._get_nested(self._env_layer, "uploading", "enabled")
        if isinstance(env_v, bool):
            return env_v

        # 2. Platform layer fallback
        plat_v = self._get_nested(self._platform_layer, "uploading", "enabled")
        if isinstance(plat_v, bool):
            return plat_v

        # 3. Merged (includes defaults)
        v = self._get_nested(self.data, "uploading", "enabled")
        if isinstance(v, bool):
            return v

        return False  # safe default

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """
        Access a merged config value by dot-notation key.

        Example:
            resolved.get("selection.max_clips")
            resolved.get("storage.disk_pressure.warning_percent")
        """
        keys = dotted_key.split(".")
        node = self.data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    def to_dict(self) -> dict:
        """Return the merged config as a plain dict (deep copy)."""
        return copy.deepcopy(self.data)

    @property
    def state_paths(self) -> "EnvironmentStatePaths":
        """
        Return an EnvironmentStatePaths for this resolved config.

        Convenience wrapper around EnvironmentStatePaths.from_resolved_config(self).

        Upload state note — platform uploading.enabled is lower priority:
            Final intended precedence once runtime kill switch is implemented:
                runtime kill switch (data/<env>/control_state.json)   [NOT YET IMPLEMENTED]
                    >
                environment uploading.enabled
                    >
                platform uploading.enabled

            A platform with uploading.enabled: false does NOT block production
            uploads when the environment says uploads are enabled. Platform upload
            state is a lower-priority default, not a veto.
        """
        # Import lazily to avoid circular imports at module load time.
        from state_paths import EnvironmentStatePaths as _ESP  # noqa: PLC0415
        return _ESP.from_resolved_config(self)

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def save_snapshot(self, job_dir: str | Path) -> Path:
        """
        Save the exact resolved config used for a run to:
            <job_dir>/resolved_config.yaml

        Creates parent directories if needed.
        Refuses to write outside the resolved job_dir (path-traversal guard).
        Does not include secrets (secrets never appear in config YAML).

        Returns the path of the written snapshot file.
        """
        job_dir = Path(job_dir).resolve()
        target = (job_dir / "resolved_config.yaml").resolve()

        # Path-traversal guard: target must be directly inside job_dir.
        try:
            target.relative_to(job_dir)
        except ValueError:
            raise ConfigError(
                f"Snapshot path {target} is not inside the requested job directory {job_dir}. "
                "Refusing to write."
            )

        job_dir.mkdir(parents=True, exist_ok=True)

        snapshot = {
            "snapshot_meta": {
                "environment": self.environment,
                "funnel_id": self.funnel_id,
                "platform_id": self.platform_id,
                "preset_id": self.preset_id,
                "config_root": str(self._config_root),
            },
            "resolved_paths": self.paths.as_dict(),
            "resolved_config": self.to_dict(),
        }

        with open(target, "w", encoding="utf-8") as fh:
            yaml.dump(snapshot, fh, default_flow_style=False, allow_unicode=True)

        return target

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_nested(data: dict, *keys: str) -> Any:
        node = data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return None
            node = node[k]
        return node


# ---------------------------------------------------------------------------
# ConfigManager
# ---------------------------------------------------------------------------


class ConfigManager:
    """
    Single interface for loading the file-based configuration system.

    ConfigManager resolves configuration; it does not make business decisions.

    Allowed:
        selecting which env/funnel/platform/preset files to load
        merging config layers
        validating the merged result
        exposing typed config values

    Not allowed:
        choosing which funnel to run
        choosing monetisation strategy
        altering creative direction
        dynamically selecting platforms based on performance
    """

    @classmethod
    def load(
        cls,
        environment: str | None = None,
        funnel_id: str = "business",
        platform_id: str = "youtube",
        preset_id: str | None = None,
        config_root: str | Path | None = None,
    ) -> ResolvedConfig:
        """
        Load, merge, validate, and return a ResolvedConfig.

        Environment selection priority:
            1. explicit `environment` argument
            2. MK04_ENV environment variable
            3. default: "development"

        Raises ConfigError if:
            - environment is invalid
            - any required config file is missing
            - config fails validation
            - funnel / platform / preset does not exist
        """
        # 1. Resolve environment
        env_str = cls._resolve_environment(environment)

        # 2. Locate config root
        if config_root is not None:
            config_root_path = Path(config_root).resolve()
        else:
            repo_root = _find_repo_root()
            config_root_path = repo_root / "config"

        if not config_root_path.is_dir():
            raise ConfigError(f"Config root does not exist: {config_root_path}")

        # 3. Validate the config tree before loading anything
        errors = validate_config_tree(config_root_path)
        if errors:
            msg = "Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            raise ConfigError(msg)

        # 4. Load layers
        env_filename = "dev.yaml" if env_str == "development" else "prod.yaml"

        layers = cls._load_layers(
            config_root_path, env_filename, funnel_id, platform_id
        )

        # 5. Determine preset_id (explicit arg > funnel.preset)
        if preset_id is None:
            funnel_data = layers["funnel"]
            preset_id = cls._get_nested(funnel_data, "funnel", "preset")
            if not preset_id:
                raise ConfigError(
                    f"Funnel '{funnel_id}' does not specify a preset in 'funnel.preset'. "
                    "Pass preset_id explicitly."
                )

        preset_path = config_root_path / "presets" / f"{preset_id}.yaml"
        if not preset_path.exists():
            raise ConfigError(
                f"Preset '{preset_id}' not found: {preset_path}. "
                f"Available presets: {cls._list_ids(config_root_path / 'presets')}"
            )
        layers["preset"] = _load_yaml_file(preset_path)

        # 6. Deep-merge in layer order
        merged = cls._merge_layers(layers)

        # 7. Validate merged storage policy (env may override system values)
        storage_errors: list[str] = []
        validate_storage_policy(
            merged.get("storage"),
            storage_errors,
            "merged config",
            require_complete=True,
        )
        if storage_errors:
            msg = "Merged storage policy validation failed:\n" + "\n".join(
                f"  - {e}" for e in storage_errors
            )
            raise ConfigError(msg)

        # 8. Resolve repo root for path resolution
        repo_root = config_root_path.parent

        # 9. Resolve paths (YAML defaults + path-authority contract)
        resolved_paths = _apply_runtime_path_overrides(
            cls._resolve_paths(merged, repo_root),
            environment=env_str,
            repo_root=repo_root,
        )

        return ResolvedConfig(
            environment=env_str,
            funnel_id=funnel_id,
            platform_id=platform_id,
            preset_id=preset_id,
            data=merged,
            paths=resolved_paths,
            _config_root=config_root_path,
            _repo_root=repo_root,
            _env_layer=layers["environment"],
            _platform_layer=layers["platform"],
        )

    # ------------------------------------------------------------------
    # Environment resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_environment(explicit: str | None) -> str:
        """
        Resolve environment string with priority:
            explicit argument > MK04_ENV env var > development default

        Returns ConfigManager form: "development" | "production".
        Aliases (dev/prod/development/production) normalize via
        environment_names. Never silently defaults to production.
        """
        raw = explicit if explicit is not None else os.environ.get("MK04_ENV")

        if raw is None or str(raw).strip() == "":
            return "development"

        try:
            return to_config_environment(raw)
        except EnvironmentNameError as exc:
            raise ConfigError(
                f"Invalid environment {raw!r}. "
                f"Expected one of: {', '.join(_SAFE_ENVS)}."
            ) from exc

    # ------------------------------------------------------------------
    # Layer loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_layers(
        config_root: Path,
        env_filename: str,
        funnel_id: str,
        platform_id: str,
    ) -> dict[str, dict]:
        """Load all layers except preset (preset_id may not be known yet)."""
        def _require(path: Path, label: str) -> dict:
            if not path.exists():
                raise ConfigError(f"{label} config not found: {path}")
            return _load_yaml_file(path)

        funnel_path = config_root / "funnels" / f"{funnel_id}.yaml"
        if not funnel_path.exists():
            available = ConfigManager._list_ids(config_root / "funnels")
            raise ConfigError(
                f"Funnel '{funnel_id}' not found: {funnel_path}. "
                f"Available funnels: {available}"
            )

        platform_path = config_root / "platforms" / f"{platform_id}.yaml"
        if not platform_path.exists():
            available = ConfigManager._list_ids(config_root / "platforms")
            raise ConfigError(
                f"Platform '{platform_id}' not found: {platform_path}. "
                f"Available platforms: {available}"
            )

        return {
            "defaults": _require(config_root / "defaults" / "default.yaml", "defaults"),
            "environment": _require(config_root / "environments" / env_filename, "environment"),
            "system": _require(config_root / "system" / "system.yaml", "system"),
            "funnel": _require(funnel_path, f"funnel '{funnel_id}'"),
            "platform": _require(platform_path, f"platform '{platform_id}'"),
        }

    # ------------------------------------------------------------------
    # Layer merge
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_layers(layers: dict[str, dict]) -> dict:
        """
        Merge layers in order: defaults → system → environment → funnel → platform → preset.

        Environment is after system so dev/prod may override storage retention
        policy and other operational settings without duplicating the full system
        file. Paths and runtime flags remain environment-owned.
        """
        order = ["defaults", "system", "environment", "funnel", "platform", "preset"]
        merged: dict = {}
        for key in order:
            if key in layers:
                merged = _deep_merge(merged, layers[key])
        return merged


    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_paths(merged: dict, repo_root: Path) -> ResolvedPaths:
        paths_cfg = merged.get("paths", {})

        def _rp(key: str) -> Path:
            raw = paths_cfg.get(key)
            if not raw:
                raise ConfigError(
                    f"Merged config is missing 'paths.{key}'. "
                    "Check environment config files."
                )
            return _resolve_path(str(raw), repo_root)

        data_root = _rp("data_root")
        jobs_root = _rp("jobs_root")
        outputs_root = _rp("outputs_root")
        logs_root = _rp("logs_root")
        reports_root = _rp("reports_root")
        database_path = _rp("database_path")
        # runs_root is not a YAML key yet — derive from data_root sibling convention.
        env_name = str(merged.get("environment", {}).get("name") or "")
        token = "prod" if env_name.lower() in {"production", "prod"} else "dev"
        runs_root = (repo_root / "runs" / token).resolve()
        return ResolvedPaths(
            data_root=data_root,
            jobs_root=jobs_root,
            outputs_root=outputs_root,
            logs_root=logs_root,
            reports_root=reports_root,
            database_path=database_path,
            runs_root=runs_root,
            control_state_file=(data_root / "control_state.json").resolve(),
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _get_nested(data: dict, *keys: str) -> Any:
        node = data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return None
            node = node[k]
        return node

    @staticmethod
    def _list_ids(directory: Path) -> list[str]:
        """Return stem names of all YAML files in a directory."""
        if not directory.is_dir():
            return []
        return sorted(p.stem for p in directory.glob("*.yaml"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(resolved: ResolvedConfig) -> None:
    print(f"Environment:      {resolved.environment}")
    print(f"Funnel:           {resolved.funnel_id}")
    print(f"Platform:         {resolved.platform_id}")
    print(f"Preset:           {resolved.preset_id}")
    print(f"Uploading enabled: {resolved.uploading_enabled}")
    print(f"  (Note: runtime kill switch not yet checked — see Remote Administration plan)")
    print(f"Jobs root:        {resolved.paths.jobs_root}")
    print(f"Outputs root:     {resolved.paths.outputs_root}")
    print(f"Logs root:        {resolved.paths.logs_root}")
    print(f"Database:         {resolved.paths.database_path}")
    print(f"Config validation: PASS")


def _print_json_summary(resolved: ResolvedConfig) -> None:
    """Print a bounded, secret-safe JSON summary."""
    summary = {
        "environment": resolved.environment,
        "funnel_id": resolved.funnel_id,
        "platform_id": resolved.platform_id,
        "preset_id": resolved.preset_id,
        "uploading_enabled": resolved.uploading_enabled,
        "paths": resolved.paths.as_dict(),
        "selection": resolved.get("selection"),
        "posting": resolved.get("posting"),
        "post_processing": resolved.get("post_processing"),
    }
    print(json.dumps(summary, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load and inspect the production infrastructure config."
    )
    parser.add_argument("--env", default=None, help="Environment: dev, prod (default: development)")
    parser.add_argument("--funnel", default="business", help="Funnel ID (default: business)")
    parser.add_argument("--platform", default="youtube", help="Platform ID (default: youtube)")
    parser.add_argument("--preset", default=None, help="Preset ID (default: from funnel config)")
    parser.add_argument("--config-root", default=None, help="Config root directory (default: auto)")
    parser.add_argument("--print-summary", action="store_true", help="Print human-readable summary")
    parser.add_argument("--print-json", action="store_true", help="Print bounded JSON summary")
    args = parser.parse_args(argv)

    if not args.print_summary and not args.print_json:
        args.print_summary = True  # default to summary

    try:
        resolved = ConfigManager.load(
            environment=args.env,
            funnel_id=args.funnel,
            platform_id=args.platform,
            preset_id=args.preset,
            config_root=args.config_root,
        )
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.print_summary:
        _print_summary(resolved)
    if args.print_json:
        _print_json_summary(resolved)

    return 0


if __name__ == "__main__":
    sys.exit(main())
