"""Canonical runtime dependency path resolution for funnel validation and sync."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import BASE_DIR

# scripts/config is added to sys.path by ops_ui.config on import.
from environment_names import EnvironmentNameError, normalize_runtime_env, resolve_mk04_env


class FunnelDependencyPathError(ValueError):
    """Raised when dependency path inputs are invalid."""


@dataclass(frozen=True)
class FunnelDependencyPaths:
    """Resolved on-disk locations for funnel runtime dependencies."""

    environment: str
    source_funnels_path: Path | None
    video_funnels_dir: Path | None
    video_pipeline_profiles_path: Path | None
    output_channels_path: Path | None
    ai_rule_registry_path: Path | None
    ai_prompts_dir: Path | None
    config_manager_funnels_dir: Path | None


def normalize_funnel_environment(raw: str | None) -> str:
    try:
        if raw is None or str(raw).strip() == "":
            return resolve_mk04_env(environ_value=os.environ.get("MK04_ENV"), default="dev")
        return normalize_runtime_env(raw)
    except EnvironmentNameError as exc:
        raise FunnelDependencyPathError(
            f"Invalid funnel environment {raw!r}. Expected dev or prod."
        ) from exc


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _config_root(environment: str) -> Path | None:
    explicit = _env_path("MK04_CONFIG_ROOT")
    if explicit is not None:
        return explicit.resolve()
    if environment == "prod":
        return None
    return None


def resolve_funnel_dependency_paths(*, environment: str | None = None) -> FunnelDependencyPaths:
    """Resolve dependency paths used by validation and sync (single source of truth)."""
    env = normalize_funnel_environment(environment)
    config_root = _config_root(env)

    source_funnels = _env_path("SOURCE_INPUT_FUNNELS")
    if source_funnels is None:
        source_dir = _env_path("INPUT_SERVICE_CONFIG_DIR")
        if source_dir is not None:
            source_funnels = source_dir / "funnels.json"
        elif config_root is not None:
            source_funnels = config_root / "source-input" / "funnels.json"
        elif env == "dev":
            source_funnels = BASE_DIR / "source-input" / "input_service" / "config" / "funnels.json"
        else:
            source_funnels = None

    video_dir = _env_path("FUNNEL_CONFIG_DIR") or _env_path("VIDEO_FUNNELS_CONFIG_DIR")
    if video_dir is None and config_root is not None:
        video_dir = config_root / "video-automation" / "funnels"
    elif video_dir is None and env == "dev":
        video_dir = BASE_DIR / "video-automation" / "config" / "funnels"

    pipeline_profiles = (
        _env_path("VIDEO_PIPELINE_PROFILES_PATH")
        or _env_path("VIDEO_PIPELINE_PROFILES")
    )
    if pipeline_profiles is None and config_root is not None:
        pipeline_profiles = config_root / "video-automation" / "video_pipeline_profiles.json"
    elif pipeline_profiles is None and env == "dev":
        pipeline_profiles = BASE_DIR / "video-automation" / "config" / "video_pipeline_profiles.json"

    channels = _env_path("OUTPUT_FUNNEL_CHANNELS")
    if channels is None and config_root is not None:
        channels = config_root / "output-funnel" / "channels.json"
    elif channels is None and env == "dev":
        for candidate in (
            BASE_DIR / "output-funnel" / "config" / "channels.json",
            BASE_DIR / "output-funnel" / "config" / "channels.example.json",
        ):
            if candidate.is_file():
                channels = candidate
                break

    ai_registry = _env_path("AI_FUNNEL_RULE_REGISTRY")
    if ai_registry is None:
        candidate = BASE_DIR / "ai-service" / "config" / "funnel_rule_registry.json"
        ai_registry = candidate

    ai_prompts = _env_path("AI_FUNNEL_RULES_DIR")
    if ai_prompts is None:
        ai_prompts = BASE_DIR / "ai-service" / "prompts" / "funnel_rules"

    config_manager = _env_path("CONFIG_MANAGER_FUNNELS_DIR")
    if config_manager is None:
        config_manager = BASE_DIR / "config" / "funnels"

    return FunnelDependencyPaths(
        environment=env,
        source_funnels_path=source_funnels,
        video_funnels_dir=video_dir,
        video_pipeline_profiles_path=pipeline_profiles,
        output_channels_path=channels,
        ai_rule_registry_path=ai_registry,
        ai_prompts_dir=ai_prompts,
        config_manager_funnels_dir=config_manager,
    )
