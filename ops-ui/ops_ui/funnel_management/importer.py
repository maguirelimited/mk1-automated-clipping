"""Read-only import from existing subsystem configs into canonical funnels."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .acquisition_sources import denormalize_canonical_acquisition_source_type
from .registry import FunnelRegistry
from .schema import (
    ALLOWED_PLATFORMS,
    ALLOWED_POSTING_MODES,
    CanonicalFunnel,
    CanonicalFunnelSchemaError,
    load_canonical_funnel,
)

_FUNNEL_ID_RE = re.compile(r"^[a-z0-9_]+$")


class FunnelImportError(Exception):
    """Raised when existing configs cannot be imported into a canonical funnel."""


@dataclass(frozen=True)
class FunnelImportReport:
    funnel: CanonicalFunnel
    source_paths: dict[str, str | None]
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass
class ExistingFunnelImporter:
    """Import canonical funnels from fragmented runtime configuration files."""

    source_funnels_path: Path
    video_funnels_dir: Path
    output_channels_path: Path | None = None
    ai_rules_path: Path | None = None
    config_manager_funnels_dir: Path | None = None
    environment: str = "dev"

    def __post_init__(self) -> None:
        env = str(self.environment or "dev").strip().lower()
        if env not in {"dev", "prod"}:
            raise FunnelImportError(
                f"environment must be 'dev' or 'prod', got {self.environment!r}"
            )
        self.environment = env
        self.source_funnels_path = Path(self.source_funnels_path).expanduser()
        self.video_funnels_dir = Path(self.video_funnels_dir).expanduser()
        if self.output_channels_path is not None:
            self.output_channels_path = Path(self.output_channels_path).expanduser()
        if self.ai_rules_path is not None:
            self.ai_rules_path = Path(self.ai_rules_path).expanduser()
        if self.config_manager_funnels_dir is not None:
            self.config_manager_funnels_dir = Path(self.config_manager_funnels_dir).expanduser()

    def import_funnel(self, funnel_id: str) -> FunnelImportReport:
        """Build a canonical funnel from existing subsystem config files."""
        clean_id = _validate_funnel_id(funnel_id)
        warnings: list[str] = []
        notes: list[str] = []
        source_paths: dict[str, str | None] = {
            "source_funnels": str(self.source_funnels_path),
            "video_funnel": str(self.video_funnels_dir / f"{clean_id}.json"),
            "output_channels": str(self.output_channels_path) if self.output_channels_path else None,
            "ai_rules": str(self.ai_rules_path) if self.ai_rules_path else None,
            "config_manager_funnels_dir": (
                str(self.config_manager_funnels_dir) if self.config_manager_funnels_dir else None
            ),
        }

        acquisition_entry = _load_source_funnel_entry(self.source_funnels_path, clean_id)
        video_entry = _load_video_funnel_entry(self.video_funnels_dir, clean_id)

        ai_rule_profile = _resolve_ai_rule_profile(
            clean_id,
            ai_rules_path=self.ai_rules_path,
            warnings=warnings,
            notes=notes,
        )

        display_name = _choose_display_name(acquisition_entry, video_entry)
        description = _optional_text(acquisition_entry.get("angle"))
        enabled = bool(acquisition_entry.get("active", True))
        status = "active" if enabled else "draft"
        now = _utc_now_iso()

        acquisition = _build_acquisition(acquisition_entry)
        processing = _build_processing(
            acquisition_entry,
            video_entry,
            ai_rule_profile=ai_rule_profile,
            funnel_id=clean_id,
        )
        distribution, distribution_warnings, distribution_notes = _build_distribution(
            acquisition_entry,
            processing_platforms=processing["platforms"],
            funnel_id=clean_id,
            output_channels_path=self.output_channels_path,
        )
        warnings.extend(distribution_warnings)
        notes.extend(distribution_notes)

        config_manager_funnel_id = _infer_config_manager_funnel_id(
            ai_rule_profile=ai_rule_profile,
            config_manager_funnels_dir=self.config_manager_funnels_dir,
            warnings=warnings,
            notes=notes,
        )

        if acquisition_entry.get("posting_config"):
            warnings.append(
                "Source-input posting_config was present but is not canonical distribution config."
            )
        if acquisition_entry.get("analytics_config"):
            notes.append("Source-input analytics_config was ignored (out of scope for canonical import).")

        payload = {
            "schema_version": 1,
            "identity": {
                "funnel_id": clean_id,
                "display_name": display_name,
                "description": description,
                "category": ai_rule_profile,
                "enabled": enabled,
                "environment": self.environment,
                "status": status,
                "template_source": None,
                "created_at": now,
                "updated_at": now,
                "operator_note": None,
            },
            "acquisition": acquisition,
            "processing": processing,
            "distribution": distribution,
            "mappings": {
                "config_manager_funnel_id": config_manager_funnel_id,
            },
        }

        try:
            funnel = load_canonical_funnel(payload)
        except CanonicalFunnelSchemaError as exc:
            raise FunnelImportError(f"Imported funnel failed schema validation: {exc}") from exc

        return FunnelImportReport(
            funnel=funnel,
            source_paths=source_paths,
            warnings=tuple(warnings),
            notes=tuple(notes),
        )

    def import_to_registry(
        self,
        funnel_id: str,
        registry: FunnelRegistry,
        *,
        overwrite: bool = False,
    ) -> FunnelImportReport:
        """Import a funnel and save it to the local canonical registry only."""
        report = self.import_funnel(funnel_id)
        registry.save_funnel(report.funnel, overwrite=overwrite)
        return report


def _validate_funnel_id(funnel_id: str) -> str:
    if not isinstance(funnel_id, str) or not funnel_id.strip():
        raise FunnelImportError("funnel_id must be a non-empty string")
    clean = funnel_id.strip()
    if not _FUNNEL_ID_RE.match(clean):
        raise FunnelImportError(
            "funnel_id must contain only lowercase letters, numbers, and underscores"
        )
    return clean


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _load_json(path: Path, *, label: str) -> Any:
    if not path.is_file():
        raise FunnelImportError(f"Missing {label}: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FunnelImportError(f"Malformed JSON in {label} ({path.name}): {exc.msg}") from exc


def _load_source_funnel_entry(path: Path, funnel_id: str) -> dict[str, Any]:
    raw = _load_json(path, label="source-input funnels config")
    if not isinstance(raw, list):
        raise FunnelImportError(f"Source-input funnels config must be a list: {path}")
    for item in raw:
        if isinstance(item, dict) and str(item.get("funnel_id") or "").strip() == funnel_id:
            return dict(item)
    raise FunnelImportError(
        f"Funnel {funnel_id!r} not found in source-input funnels config: {path}"
    )


def _load_video_funnel_entry(video_dir: Path, funnel_id: str) -> dict[str, Any]:
    path = video_dir / f"{funnel_id}.json"
    raw = _load_json(path, label="video-automation funnel config")
    if not isinstance(raw, dict):
        raise FunnelImportError(f"Video funnel config must be an object: {path}")
    file_id = str(raw.get("funnel_id") or "").strip()
    if file_id and file_id != funnel_id:
        raise FunnelImportError(
            f"Video funnel file {path.name!r} contains funnel_id {file_id!r}, "
            f"expected {funnel_id!r}"
        )
    return raw


def _choose_display_name(acquisition: dict[str, Any], video: dict[str, Any]) -> str:
    video_name = _optional_text(video.get("funnel_name"))
    if video_name:
        return video_name
    angle = _optional_text(acquisition.get("angle"))
    if angle:
        return angle.title()
    return str(acquisition.get("funnel_id") or "Imported Funnel")


def _build_acquisition(entry: dict[str, Any]) -> dict[str, Any]:
    sources_raw = entry.get("sources")
    if not isinstance(sources_raw, list):
        raise FunnelImportError("Acquisition config is missing a sources list")

    sources: list[dict[str, Any]] = []
    for index, item in enumerate(sources_raw):
        if not isinstance(item, dict):
            raise FunnelImportError(f"acquisition.sources[{index}] must be an object")
        source = {
            "source_id": item.get("source_id"),
            "label": item.get("label"),
            "url": item.get("url"),
            "source_type": item.get("source_type"),
            "active": item.get("active"),
            "max_videos_per_source": item.get("max_videos_per_source"),
            "hydrate_missing_duration": item.get("hydrate_missing_duration", True),
            "title_allowlist": item.get("title_allowlist", []),
            "title_blocklist": item.get("title_blocklist", []),
        }
        sources.append(source)

    return {
        "source_type": denormalize_canonical_acquisition_source_type(
            str(entry.get("source_type") or "")
        ),
        "sources": sources,
        "min_duration_minutes": entry.get("min_duration_minutes"),
        "max_duration_minutes": entry.get("max_duration_minutes"),
        "max_downloads_per_run": entry.get("max_downloads_per_run"),
    }


def _build_processing(
    acquisition: dict[str, Any],
    video: dict[str, Any],
    *,
    ai_rule_profile: str,
    funnel_id: str,
) -> dict[str, Any]:
    selection_raw = video.get("selection") if isinstance(video.get("selection"), dict) else {}
    output_raw = video.get("output") if isinstance(video.get("output"), dict) else {}
    platforms_raw = video.get("platforms") if isinstance(video.get("platforms"), dict) else {}

    platforms: dict[str, bool] = {name: False for name in sorted(ALLOWED_PLATFORMS)}
    for key, value in platforms_raw.items():
        if key in ALLOWED_PLATFORMS:
            platforms[key] = bool(value)

    pipeline_profile = _optional_text(acquisition.get("pipeline_profile")) or funnel_id

    delivery_mode = _optional_text(output_raw.get("delivery_mode")) or "pull_from_output_endpoint"

    return {
        "pipeline_profile": pipeline_profile,
        "ai_rules": {"ai_rule_profile": ai_rule_profile},
        "selection": {
            "max_clips": selection_raw.get("max_clips"),
            "min_clip_duration_sec": selection_raw.get("min_duration_sec"),
            "max_clip_duration_sec": selection_raw.get("max_duration_sec"),
            "max_overlap_sec": selection_raw.get("max_overlap_sec"),
        },
        "output": {
            "filename_prefix": output_raw.get("filename_prefix"),
            "delivery_mode": delivery_mode,
        },
        "platforms": platforms,
    }


def _build_distribution(
    acquisition: dict[str, Any],
    *,
    processing_platforms: dict[str, bool],
    funnel_id: str,
    output_channels_path: Path | None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    warnings: list[str] = []
    notes: list[str] = []

    posting_config = acquisition.get("posting_config")
    posting_enabled = False
    posting_mode = "manual_review"

    if isinstance(posting_config, dict):
        if posting_config.get("enabled") is True:
            posting_enabled = True
        mode = _optional_text(posting_config.get("mode"))
        if mode in ALLOWED_POSTING_MODES:
            posting_mode = mode
        elif mode:
            warnings.append(
                f"Source-input posting_config.mode {mode!r} is not a supported canonical posting_mode; "
                "using manual_review."
            )
    else:
        warnings.append(
            "No source-input posting_config found; distribution.posting_enabled defaults to false."
        )

    channel_routes: list[dict[str, Any]] = []
    if output_channels_path is None:
        warnings.append("No output channel config path provided.")
    elif not output_channels_path.is_file():
        warnings.append(f"Output channel config not found: {output_channels_path}")
    else:
        raw = _load_json(output_channels_path, label="output channel config")
        channels = raw.get("channels") if isinstance(raw, dict) else None
        if not isinstance(channels, list):
            warnings.append("Output channel config did not contain a channels list.")
        else:
            for channel in channels:
                if not isinstance(channel, dict):
                    continue
                routing = channel.get("routing") if isinstance(channel.get("routing"), dict) else {}
                accepted = routing.get("accepted_funnel_ids")
                if not isinstance(accepted, list) or funnel_id not in accepted:
                    continue
                platform = _optional_text(channel.get("platform"))
                channel_id = _optional_text(channel.get("channel_id"))
                if not platform or not channel_id:
                    warnings.append(
                        f"Skipped channel route with missing channel_id/platform for funnel {funnel_id!r}."
                    )
                    continue
                channel_routes.append(
                    {
                        "channel_id": channel_id,
                        "platform": platform,
                        "enabled": bool(channel.get("enabled", True)),
                    }
                )

    if output_channels_path is not None and not channel_routes:
        warnings.append("No channel route accepts this funnel.")

    target_platforms: list[str] = []
    for name, enabled in processing_platforms.items():
        if enabled and name not in target_platforms:
            target_platforms.append(name)
    for route in channel_routes:
        platform = route["platform"]
        if platform not in target_platforms:
            target_platforms.append(platform)

    if not posting_enabled and channel_routes:
        notes.append(
            "Channel routes were imported, but posting_enabled remained false because "
            "source-input posting_config.enabled was not true."
        )

    return (
        {
            "posting_enabled": posting_enabled,
            "posting_mode": posting_mode,
            "target_platforms": target_platforms,
            "channel_routes": channel_routes,
        },
        warnings,
        notes,
    )


def _load_funnel_rule_aliases(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FunnelImportError(f"Missing AI rules module: {path}")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        raise FunnelImportError(f"Could not parse AI rules module {path}: {exc}") from exc

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "FUNNEL_RULE_ALIASES":
                value = ast.literal_eval(node.value)
                if not isinstance(value, dict):
                    raise FunnelImportError("FUNNEL_RULE_ALIASES must be a dict")
                return {str(k): str(v) for k, v in value.items()}
    raise FunnelImportError(f"FUNNEL_RULE_ALIASES not found in {path}")


def _resolve_ai_rule_profile(
    funnel_id: str,
    *,
    ai_rules_path: Path | None,
    warnings: list[str],
    notes: list[str],
) -> str:
    if ai_rules_path is None:
        raise FunnelImportError(
            "AI rules path not provided; cannot resolve processing.ai_rules.ai_rule_profile"
        )
    aliases = _load_funnel_rule_aliases(ai_rules_path)
    key = funnel_id.strip().lower()
    profile = aliases.get(key) or aliases.get(funnel_id)
    if not profile:
        raise FunnelImportError(
            f"No AI rule profile alias found for funnel {funnel_id!r} in {ai_rules_path.name}"
        )
    notes.append("AI alias was inferred from hardcoded ai-service mapping.")
    if funnel_id in aliases:
        warnings.append("AI rule profile was resolved from FUNNEL_RULE_ALIASES in ai-service code.")
    return profile


def _infer_config_manager_funnel_id(
    *,
    ai_rule_profile: str,
    config_manager_funnels_dir: Path | None,
    warnings: list[str],
    notes: list[str],
) -> str | None:
    candidate = _optional_text(ai_rule_profile)
    if candidate and config_manager_funnels_dir and config_manager_funnels_dir.is_dir():
        yaml_path = config_manager_funnels_dir / f"{candidate}.yaml"
        if yaml_path.is_file():
            notes.append(f"ConfigManager mapping inferred from {yaml_path.name}.")
            return candidate
        warnings.append(
            f"ConfigManager funnel file not found for ai_rule_profile {candidate!r} "
            f"under {config_manager_funnels_dir}."
        )
        return candidate

    if candidate:
        notes.append(
            f"ConfigManager mapping set to ai_rule_profile {candidate!r} without YAML verification."
        )
        return candidate

    warnings.append("ConfigManager mapping could not be inferred.")
    return None
