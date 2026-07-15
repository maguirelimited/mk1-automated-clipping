"""Configuration synchronisation layer (Funnel Management MK1).

Projects canonical registry funnels onto existing runtime config files.
Backend-only: no UI routes, no automatic sync after create/edit/clone.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .acquisition_sources import normalize_runtime_acquisition_source_type
from .config_manager_sync import plan_config_manager_yaml, resolve_config_manager_funnel_id
from .funnel_rule_registry_ops import (
    FunnelRuleRegistryOpsError,
    default_registry_document,
    derive_rules_version,
    load_registry_document,
    plan_builtin_registry_sync,
    plan_custom_registry_sync,
    plan_prompt_file_sync,
)
from .schema import CanonicalFunnel, DEFAULT_PROMPT_MANAGED, dump_canonical_funnel

_FUNNEL_ID_RE = re.compile(r"^[a-z0-9_]+$")

VIDEO_PLATFORMS = frozenset({"tiktok", "instagram_reels", "youtube_shorts", "x"})
CANONICAL_ONLY_PLATFORMS = frozenset({"facebook_reels"})

SOURCE_INPUT_OWNED_KEYS = frozenset(
    {
        "funnel_id",
        "angle",
        "source_type",
        "sources",
        "min_duration_minutes",
        "max_duration_minutes",
        "max_downloads_per_run",
        "active",
        "pipeline_profile",
    }
)

VIDEO_OWNED_KEYS = frozenset(
    {
        "schema_version",
        "funnel_id",
        "funnel_name",
        "platforms",
        "selection",
        "output",
    }
)


class FunnelSyncError(Exception):
    """Raised when synchronisation cannot proceed safely."""


@dataclass(frozen=True)
class FunnelSyncTargetPaths:
    source_funnels_path: Path | None
    video_funnels_dir: Path | None
    output_channels_path: Path | None
    ai_rule_registry_path: Path | None = None
    ai_prompts_dir: Path | None = None
    config_manager_funnels_dir: Path | None = None


@dataclass
class FunnelSyncFileChange:
    target: str
    path: Path
    action: str
    before: dict[str, Any] | list[Any] | None
    after: dict[str, Any] | list[Any] | None
    changed: bool
    messages: list[str] = field(default_factory=list)
    before_text: str | None = None
    after_text: str | None = None


@dataclass
class FunnelSyncReport:
    funnel_id: str
    dry_run: bool
    ok: bool
    changed: bool
    changes: list[FunnelSyncFileChange]
    errors: list[str]
    warnings: list[str]


def _utc_stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y%m%dT%H%M%SZ")


def _validate_funnel_id(funnel_id: str) -> str:
    if not isinstance(funnel_id, str) or not funnel_id.strip():
        raise FunnelSyncError("funnel_id must be a non-empty string")
    clean = funnel_id.strip()
    if not _FUNNEL_ID_RE.match(clean):
        raise FunnelSyncError(
            "funnel_id must contain only lowercase letters, numbers, and underscores"
        )
    return clean


def _load_json_file(path: Path) -> Any:
    if not path.is_file():
        raise FunnelSyncError(f"Config file is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FunnelSyncError(f"Invalid JSON in {path.name}: {exc.msg}") from exc


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.stem}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    if not encoded.endswith("\n"):
        encoded += "\n"

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.stem}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _backup_file(path: Path) -> Path:
    backup_path = path.with_name(f"{path.name}.bak.{_utc_stamp()}")
    shutil.copy2(path, backup_path)
    return backup_path


def _load_registry_for_sync(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.is_file():
        return default_registry_document(), True
    try:
        return load_registry_document(path), False
    except FunnelRuleRegistryOpsError as exc:
        raise FunnelSyncError(str(exc)) from exc


def _json_equal(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _source_entry_from_funnel(
    funnel: CanonicalFunnel,
    *,
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    identity = funnel.identity
    acq = funnel.acquisition
    angle = (identity.description or identity.display_name or identity.funnel_id).strip()

    sources: list[dict[str, Any]] = []
    for source in acq.sources:
        sources.append(
            {
                "source_id": source.source_id,
                "label": source.label,
                "url": source.url,
                "source_type": source.source_type,
                "active": source.active,
                "max_videos_per_source": source.max_videos_per_source,
                "hydrate_missing_duration": source.hydrate_missing_duration,
                "title_allowlist": list(source.title_allowlist),
                "title_blocklist": list(source.title_blocklist),
            }
        )

    entry: dict[str, Any] = {
        "funnel_id": identity.funnel_id,
        "angle": angle,
        "source_type": normalize_runtime_acquisition_source_type(acq.source_type),
        "sources": sources,
        "min_duration_minutes": acq.min_duration_minutes,
        "max_duration_minutes": acq.max_duration_minutes,
        "max_downloads_per_run": acq.max_downloads_per_run,
        "active": identity.enabled,
        "pipeline_profile": funnel.processing.pipeline_profile,
    }

    if existing:
        for key in ("posting_config", "analytics_config"):
            if key in existing:
                entry[key] = existing[key]
        for key, value in existing.items():
            if key not in entry and key not in SOURCE_INPUT_OWNED_KEYS:
                entry[key] = value

    return entry


def _patch_source_funnels_list(
    existing: list[Any],
    funnel: CanonicalFunnel,
) -> tuple[list[Any], str, list[str]]:
    funnel_id = funnel.identity.funnel_id
    messages: list[str] = []
    found = False
    updated: list[Any] = []

    for item in existing:
        if not isinstance(item, dict):
            updated.append(item)
            continue
        if str(item.get("funnel_id") or "").strip() == funnel_id:
            new_entry = _source_entry_from_funnel(funnel, existing=item)
            updated.append(new_entry)
            found = True
        else:
            updated.append(item)

    if not found:
        updated.append(_source_entry_from_funnel(funnel, existing=None))
        action = "create"
        messages.append(f"Append new funnel entry for {funnel_id!r}.")
    else:
        action = "update"
        messages.append(f"Update existing funnel entry for {funnel_id!r}.")

    return updated, action, messages


def _map_delivery_mode(mode: str) -> tuple[str, list[str]]:
    messages: list[str] = []
    if mode == "handoff":
        messages.append("Mapped canonical delivery_mode handoff to pull_from_output_endpoint.")
        return "pull_from_output_endpoint", messages
    if mode == "pull_from_output_endpoint":
        return mode, messages
    raise FunnelSyncError(
        f"Unsupported delivery_mode {mode!r}; expected handoff or pull_from_output_endpoint."
    )


def _video_platforms_from_funnel(funnel: CanonicalFunnel) -> tuple[dict[str, bool], list[str]]:
    warnings: list[str] = []
    platforms = {name: False for name in sorted(VIDEO_PLATFORMS)}
    for name in VIDEO_PLATFORMS:
        platforms[name] = bool(funnel.processing.platforms.get(name, False))
    if funnel.processing.platforms.get("facebook_reels"):
        warnings.append(
            "facebook_reels is canonical distribution intent but is not written to video-automation config."
        )
    return platforms, warnings


def _video_funnel_from_canonical(
    funnel: CanonicalFunnel,
    *,
    existing: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    messages: list[str] = []
    delivery_mode, delivery_msgs = _map_delivery_mode(funnel.processing.output.delivery_mode)
    messages.extend(delivery_msgs)
    platforms, platform_warnings = _video_platforms_from_funnel(funnel)
    messages.extend(platform_warnings)

    selection = funnel.processing.selection
    output = funnel.processing.output

    projected: dict[str, Any] = {
        "schema_version": 1,
        "funnel_id": funnel.identity.funnel_id,
        "funnel_name": funnel.identity.display_name,
        "platforms": platforms,
        "selection": {
            "max_clips": selection.max_clips,
            "min_duration_sec": selection.min_clip_duration_sec,
            "max_duration_sec": selection.max_clip_duration_sec,
            "max_overlap_sec": selection.max_overlap_sec,
        },
        "output": {
            "filename_prefix": output.filename_prefix,
            "delivery_mode": delivery_mode,
        },
    }

    if existing:
        for key, value in existing.items():
            if key not in VIDEO_OWNED_KEYS:
                projected[key] = value

    return projected, messages


def _validate_video_projection(data: dict[str, Any], *, funnel_id: str) -> None:
    file_id = str(data.get("funnel_id") or "").strip()
    if file_id != funnel_id:
        raise FunnelSyncError(
            f"Video funnel file has mismatched funnel_id {file_id!r}; expected {funnel_id!r}."
        )
    if not str(data.get("funnel_name") or "").strip():
        raise FunnelSyncError("Video funnel projection requires funnel_name.")
    platforms = data.get("platforms")
    if not isinstance(platforms, dict):
        raise FunnelSyncError("Video funnel projection requires platforms object.")
    for key in platforms:
        if key not in VIDEO_PLATFORMS:
            raise FunnelSyncError(f"Unsupported video platform key {key!r}.")
    selection = data.get("selection")
    if not isinstance(selection, dict):
        raise FunnelSyncError("Video funnel projection requires selection object.")
    for required in ("max_clips", "min_duration_sec", "max_duration_sec", "max_overlap_sec"):
        if required not in selection:
            raise FunnelSyncError(f"Video funnel selection missing {required}.")
    output = data.get("output")
    if not isinstance(output, dict):
        raise FunnelSyncError("Video funnel projection requires output object.")
    mode = str(output.get("delivery_mode") or "").strip()
    if mode != "pull_from_output_endpoint":
        raise FunnelSyncError(f"Video funnel delivery_mode must be pull_from_output_endpoint, got {mode!r}.")


def _resolve_video_funnel_path(video_funnels_dir: Path, funnel_id: str) -> Path:
    safe_id = _validate_funnel_id(funnel_id)
    root = video_funnels_dir.expanduser().resolve()
    path = (root / f"{safe_id}.json").resolve()
    if path.parent != root:
        raise FunnelSyncError(f"Unsafe video funnel path for {funnel_id!r}")
    return path


def _patch_output_channels(
    channels_doc: dict[str, Any],
    funnel: CanonicalFunnel,
) -> tuple[dict[str, Any], list[str], list[str]]:
    errors: list[str] = []
    messages: list[str] = []
    channels = channels_doc.get("channels")
    if not isinstance(channels, list):
        raise FunnelSyncError("Output channels config requires a channels list.")

    channel_index: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(channels):
        if not isinstance(item, dict):
            continue
        channel_id = str(item.get("channel_id") or "").strip()
        if channel_id:
            channel_index[channel_id] = item

    funnel_id = funnel.identity.funnel_id
    patched_count = 0

    for route in funnel.distribution.channel_routes:
        if not route.enabled:
            messages.append(
                f"Skipped disabled canonical route {route.channel_id!r} for accepted_funnel_ids patch."
            )
            continue

        channel = channel_index.get(route.channel_id)
        if channel is None:
            errors.append(f"Output channel {route.channel_id!r} not found.")
            continue

        channel_platform = str(channel.get("platform") or "").strip()
        if channel_platform != route.platform:
            errors.append(
                f"Output channel {route.channel_id!r} platform mismatch: "
                f"channel has {channel_platform!r}, canonical route has {route.platform!r}."
            )
            continue

        routing = channel.get("routing")
        if not isinstance(routing, dict):
            routing = {}
            channel["routing"] = routing

        accepted_raw = routing.get("accepted_funnel_ids")
        accepted = list(accepted_raw) if isinstance(accepted_raw, list) else []
        if funnel_id not in accepted:
            accepted.append(funnel_id)
            routing["accepted_funnel_ids"] = accepted
            patched_count += 1
            messages.append(
                f"Add {funnel_id!r} to routing.accepted_funnel_ids on channel {route.channel_id!r}."
            )
        else:
            messages.append(
                f"Channel {route.channel_id!r} already accepts {funnel_id!r}."
            )

    if errors:
        raise FunnelSyncError("; ".join(errors))

    if patched_count:
        messages.insert(0, f"Patch accepted_funnel_ids on {patched_count} channel route(s).")
    else:
        messages.insert(0, "No accepted_funnel_ids changes required.")

    return channels_doc, messages, errors


class FunnelSynchronizer:
    """Project canonical funnels onto runtime configuration files."""

    def __init__(self, paths: FunnelSyncTargetPaths) -> None:
        self.paths = FunnelSyncTargetPaths(
            source_funnels_path=(
                None
                if paths.source_funnels_path is None
                else Path(paths.source_funnels_path).expanduser()
            ),
            video_funnels_dir=(
                None
                if paths.video_funnels_dir is None
                else Path(paths.video_funnels_dir).expanduser()
            ),
            output_channels_path=(
                None
                if paths.output_channels_path is None
                else Path(paths.output_channels_path).expanduser()
            ),
            ai_rule_registry_path=(
                None
                if paths.ai_rule_registry_path is None
                else Path(paths.ai_rule_registry_path).expanduser()
            ),
            ai_prompts_dir=(
                None if paths.ai_prompts_dir is None else Path(paths.ai_prompts_dir).expanduser()
            ),
            config_manager_funnels_dir=(
                None
                if paths.config_manager_funnels_dir is None
                else Path(paths.config_manager_funnels_dir).expanduser()
            ),
        )

    def build_plan(self, funnel: CanonicalFunnel) -> FunnelSyncReport:
        """Build a dry-run synchronisation plan without writing files."""
        return self._build_report(funnel, dry_run=True, apply_writes=False, backup=False)

    def apply(
        self,
        funnel: CanonicalFunnel,
        *,
        backup: bool = False,
    ) -> FunnelSyncReport:
        """Apply synchronisation when the plan has no blocking errors."""
        report = self._build_report(funnel, dry_run=False, apply_writes=True, backup=backup)
        return report

    def _build_report(
        self,
        funnel: CanonicalFunnel,
        *,
        dry_run: bool,
        apply_writes: bool,
        backup: bool,
    ) -> FunnelSyncReport:
        funnel_id = funnel.identity.funnel_id
        errors: list[str] = []
        warnings: list[str] = []
        changes: list[FunnelSyncFileChange] = []

        if funnel.processing.pipeline_profile != funnel_id:
            warnings.append(
                f"Pipeline profile {funnel.processing.pipeline_profile!r} differs from funnel_id; "
                "shared profile files are not written by sync."
            )

        if funnel.acquisition.max_downloads_per_run != 1:
            errors.append("source-input max_downloads_per_run must be 1.")

        changes.extend(self._plan_source_input(funnel, errors))
        changes.extend(self._plan_video_funnel(funnel, errors, warnings))
        changes.extend(self._plan_output_channels(funnel, errors, warnings))
        changes.extend(self._plan_funnel_rule_registry(funnel, errors, warnings))
        changes.extend(self._plan_ai_prompt_file(funnel, errors))
        changes.extend(self._plan_config_manager_yaml(funnel, errors, warnings))

        blocking = list(errors)
        for change in changes:
            if change.action == "error":
                blocking.extend(change.messages)

        ok = not blocking
        changed = any(
            item.changed and item.action in {"create", "update"} for item in changes
        )

        if apply_writes:
            if not ok:
                raise FunnelSyncError(
                    "Refusing to apply sync because blocking errors exist: "
                    + "; ".join(blocking)
                )
            if backup:
                for change in changes:
                    if change.changed and change.action in {"create", "update"} and change.path.is_file():
                        backup_path = _backup_file(change.path)
                        change.messages.append(f"Backup written to {backup_path.name}.")
            for change in changes:
                if not change.changed or change.action not in {"create", "update"}:
                    continue
                if change.after is not None:
                    _write_json_atomic(change.path, change.after)
                elif change.after_text is not None:
                    _write_text_atomic(change.path, change.after_text)

        return FunnelSyncReport(
            funnel_id=funnel_id,
            dry_run=dry_run,
            ok=ok,
            changed=changed,
            changes=changes,
            errors=blocking,
            warnings=warnings,
        )

    def _plan_source_input(
        self,
        funnel: CanonicalFunnel,
        errors: list[str],
    ) -> list[FunnelSyncFileChange]:
        path = self.paths.source_funnels_path
        if path is None:
            errors.append("source-input funnels path not configured.")
            return [
                FunnelSyncFileChange(
                    target="source_input_funnels",
                    path=Path("."),
                    action="error",
                    before=None,
                    after=None,
                    changed=False,
                    messages=["source-input funnels path not configured."],
                )
            ]

        try:
            before_raw = _load_json_file(path)
        except FunnelSyncError as exc:
            errors.append(str(exc))
            return [
                FunnelSyncFileChange(
                    target="source_input_funnels",
                    path=path,
                    action="error",
                    before=None,
                    after=None,
                    changed=False,
                    messages=[str(exc)],
                )
            ]

        if not isinstance(before_raw, list):
            message = "source-input funnels.json must be a JSON list."
            errors.append(message)
            return [
                FunnelSyncFileChange(
                    target="source_input_funnels",
                    path=path,
                    action="error",
                    before=before_raw if isinstance(before_raw, list) else None,
                    after=None,
                    changed=False,
                    messages=[message],
                )
            ]

        before: list[Any] = before_raw
        try:
            after, action, messages = _patch_source_funnels_list(before, funnel)
        except FunnelSyncError as exc:
            errors.append(str(exc))
            return [
                FunnelSyncFileChange(
                    target="source_input_funnels",
                    path=path,
                    action="error",
                    before=before,
                    after=None,
                    changed=False,
                    messages=[str(exc)],
                )
            ]

        changed = not _json_equal(before, after)
        if not changed:
            action = "unchanged"

        return [
            FunnelSyncFileChange(
                target="source_input_funnels",
                path=path,
                action=action,
                before=before,
                after=after,
                changed=changed,
                messages=messages,
            )
        ]

    def _plan_video_funnel(
        self,
        funnel: CanonicalFunnel,
        errors: list[str],
        warnings: list[str],
    ) -> list[FunnelSyncFileChange]:
        video_dir = self.paths.video_funnels_dir
        if video_dir is None:
            errors.append("video funnels directory not configured.")
            return [
                FunnelSyncFileChange(
                    target="video_funnel_json",
                    path=Path("."),
                    action="error",
                    before=None,
                    after=None,
                    changed=False,
                    messages=["video funnels directory not configured."],
                )
            ]

        funnel_id = funnel.identity.funnel_id
        try:
            path = _resolve_video_funnel_path(video_dir, funnel_id)
        except FunnelSyncError as exc:
            errors.append(str(exc))
            return [
                FunnelSyncFileChange(
                    target="video_funnel_json",
                    path=video_dir / f"{funnel_id}.json",
                    action="error",
                    before=None,
                    after=None,
                    changed=False,
                    messages=[str(exc)],
                )
            ]

        before: dict[str, Any] | None = None
        if path.is_file():
            try:
                raw = _load_json_file(path)
            except FunnelSyncError as exc:
                errors.append(str(exc))
                return [
                    FunnelSyncFileChange(
                        target="video_funnel_json",
                        path=path,
                        action="error",
                        before=None,
                        after=None,
                        changed=False,
                        messages=[str(exc)],
                    )
                ]
            if not isinstance(raw, dict):
                message = "Video funnel config must be a JSON object."
                errors.append(message)
                return [
                    FunnelSyncFileChange(
                        target="video_funnel_json",
                        path=path,
                        action="error",
                        before=None,
                        after=None,
                        changed=False,
                        messages=[message],
                    )
                ]
            existing_id = str(raw.get("funnel_id") or "").strip()
            if existing_id and existing_id != funnel_id:
                message = (
                    f"Video funnel file has mismatched funnel_id {existing_id!r}; "
                    f"expected {funnel_id!r}."
                )
                errors.append(message)
                return [
                    FunnelSyncFileChange(
                        target="video_funnel_json",
                        path=path,
                        action="error",
                        before=raw,
                        after=None,
                        changed=False,
                        messages=[message],
                    )
                ]
            before = raw

        try:
            after, messages = _video_funnel_from_canonical(funnel, existing=before)
            _validate_video_projection(after, funnel_id=funnel_id)
        except FunnelSyncError as exc:
            errors.append(str(exc))
            return [
                FunnelSyncFileChange(
                    target="video_funnel_json",
                    path=path,
                    action="error",
                    before=before,
                    after=None,
                    changed=False,
                    messages=[str(exc)],
                )
            ]

        warnings.extend(msg for msg in messages if "facebook_reels" in msg or "handoff" in msg)

        changed = before is None or not _json_equal(before, after)
        action = "create" if before is None else ("update" if changed else "unchanged")
        if before is None:
            messages.insert(0, f"Create video funnel file {path.name}.")
        elif changed:
            messages.insert(0, f"Update video funnel file {path.name}.")
        else:
            messages.insert(0, f"Video funnel file {path.name} is already up to date.")

        return [
            FunnelSyncFileChange(
                target="video_funnel_json",
                path=path,
                action=action,
                before=before,
                after=after,
                changed=changed,
                messages=messages,
            )
        ]

    def _plan_output_channels(
        self,
        funnel: CanonicalFunnel,
        errors: list[str],
        warnings: list[str],
    ) -> list[FunnelSyncFileChange]:
        path = self.paths.output_channels_path
        if not funnel.distribution.posting_enabled:
            message = "Output channel routing skipped (posting disabled)."
            warnings.append(message)
            return [
                FunnelSyncFileChange(
                    target="output_channels",
                    path=path if path is not None else Path("."),
                    action="skipped",
                    before=None,
                    after=None,
                    changed=False,
                    messages=[message],
                )
            ]

        if path is None:
            errors.append("output channels path not configured.")
            return [
                FunnelSyncFileChange(
                    target="output_channels",
                    path=Path("."),
                    action="error",
                    before=None,
                    after=None,
                    changed=False,
                    messages=["output channels path not configured."],
                )
            ]

        try:
            before_raw = _load_json_file(path)
        except FunnelSyncError as exc:
            errors.append(str(exc))
            return [
                FunnelSyncFileChange(
                    target="output_channels",
                    path=path,
                    action="error",
                    before=None,
                    after=None,
                    changed=False,
                    messages=[str(exc)],
                )
            ]

        if not isinstance(before_raw, dict):
            message = "Output channels config must be a JSON object."
            errors.append(message)
            return [
                FunnelSyncFileChange(
                    target="output_channels",
                    path=path,
                    action="error",
                    before=None,
                    after=None,
                    changed=False,
                    messages=[message],
                )
            ]

        before = before_raw
        try:
            after, messages, _ = _patch_output_channels(json.loads(json.dumps(before)), funnel)
        except FunnelSyncError as exc:
            errors.append(str(exc))
            return [
                FunnelSyncFileChange(
                    target="output_channels",
                    path=path,
                    action="error",
                    before=before,
                    after=None,
                    changed=False,
                    messages=[str(exc)],
                )
            ]

        changed = not _json_equal(before, after)
        action = "update" if changed else "unchanged"
        if not changed:
            messages.insert(0, "Output channels routing already includes required funnel IDs.")

        return [
            FunnelSyncFileChange(
                target="output_channels",
                path=path,
                action=action,
                before=before,
                after=after,
                changed=changed,
                messages=messages,
            )
        ]

    def _plan_funnel_rule_registry(
        self,
        funnel: CanonicalFunnel,
        errors: list[str],
        warnings: list[str],
    ) -> list[FunnelSyncFileChange]:
        path = self.paths.ai_rule_registry_path
        if path is None:
            warnings.append(
                "AI registry was not updated because ai_rule_registry_path was not supplied."
            )
            return [
                FunnelSyncFileChange(
                    target="funnel_rule_registry",
                    path=Path("."),
                    action="skipped",
                    before=None,
                    after=None,
                    changed=False,
                    messages=["AI registry sync skipped (path not supplied)."],
                )
            ]

        ai_rules = funnel.processing.ai_rules
        profile_id = ai_rules.ai_rule_profile
        funnel_id = funnel.identity.funnel_id
        prompt_managed = ai_rules.prompt_managed or DEFAULT_PROMPT_MANAGED

        try:
            before_registry, registry_missing = _load_registry_for_sync(path)
        except FunnelSyncError as exc:
            errors.append(str(exc))
            return [
                FunnelSyncFileChange(
                    target="funnel_rule_registry",
                    path=path,
                    action="error",
                    before=None,
                    after=None,
                    changed=False,
                    messages=[str(exc)],
                )
            ]

        if prompt_managed == "builtin":
            action, after_registry, changed, messages = plan_builtin_registry_sync(
                before_registry,
                funnel_id=funnel_id,
                profile_id=profile_id,
                prompts_dir=self.paths.ai_prompts_dir,
            )
        else:
            rules_version = derive_rules_version(profile_id)
            action, after_registry, changed, messages = plan_custom_registry_sync(
                before_registry,
                funnel_id=funnel_id,
                profile_id=profile_id,
                rules_version=rules_version,
            )

        if action == "error":
            errors.extend(messages)
        elif registry_missing and changed:
            action = "create"
            messages.insert(0, f"Create funnel rule registry at {path.name}.")
        elif registry_missing and not changed:
            action = "unchanged"

        return [
            FunnelSyncFileChange(
                target="funnel_rule_registry",
                path=path,
                action=action,
                before=before_registry if not registry_missing else None,
                after=after_registry,
                changed=changed or registry_missing,
                messages=messages,
            )
        ]

    def _plan_ai_prompt_file(
        self,
        funnel: CanonicalFunnel,
        errors: list[str],
    ) -> list[FunnelSyncFileChange]:
        ai_rules = funnel.processing.ai_rules
        prompt_managed = ai_rules.prompt_managed or DEFAULT_PROMPT_MANAGED
        profile_id = ai_rules.ai_rule_profile
        rules_version = derive_rules_version(profile_id)

        registry_path = self.paths.ai_rule_registry_path
        registry: dict[str, Any]
        if registry_path is not None and registry_path.is_file():
            try:
                registry, _ = _load_registry_for_sync(registry_path)
            except FunnelSyncError:
                registry = default_registry_document()
        else:
            registry = default_registry_document()

        action, prompt_path, before_text, after_text, changed, messages = plan_prompt_file_sync(
            prompts_dir=self.paths.ai_prompts_dir,
            rules_version=rules_version,
            prompt_text=ai_rules.prompt_text or "",
            prompt_managed=prompt_managed,
            registry=registry,
            profile_id=profile_id,
        )

        if action == "error":
            errors.extend(messages)
            return [
                FunnelSyncFileChange(
                    target="ai_prompt_file",
                    path=prompt_path or Path("."),
                    action="error",
                    before=None,
                    after=None,
                    changed=False,
                    messages=messages,
                    before_text=before_text,
                    after_text=after_text or None,
                )
            ]

        if action == "skipped":
            return [
                FunnelSyncFileChange(
                    target="ai_prompt_file",
                    path=prompt_path or Path("."),
                    action="skipped",
                    before=None,
                    after=None,
                    changed=False,
                    messages=messages,
                )
            ]

        return [
            FunnelSyncFileChange(
                target="ai_prompt_file",
                path=prompt_path or Path("."),
                action=action,
                before=None,
                after=None,
                changed=changed,
                messages=messages,
                before_text=before_text,
                after_text=after_text,
            )
        ]

    def _plan_config_manager_yaml(
        self,
        funnel: CanonicalFunnel,
        errors: list[str],
        warnings: list[str],
    ) -> list[FunnelSyncFileChange]:
        config_dir = self.paths.config_manager_funnels_dir
        funnel_id = resolve_config_manager_funnel_id(funnel)
        yaml_path = (config_dir / f"{funnel_id}.yaml") if config_dir is not None else Path(f"{funnel_id}.yaml")

        if config_dir is None:
            warnings.append(
                "ConfigManager YAML was not written because config_manager_funnels_dir was not supplied."
            )
            return [
                FunnelSyncFileChange(
                    target="config_manager_yaml",
                    path=yaml_path,
                    action="skipped",
                    before=None,
                    after=None,
                    changed=False,
                    messages=["ConfigManager YAML sync skipped (directory not supplied)."],
                )
            ]

        action, before_text, after_text, changed, messages = plan_config_manager_yaml(funnel, yaml_path)
        if action == "error":
            errors.extend(messages)
            return [
                FunnelSyncFileChange(
                    target="config_manager_yaml",
                    path=yaml_path,
                    action="error",
                    before=None,
                    after=None,
                    changed=False,
                    messages=messages,
                    before_text=before_text,
                    after_text=after_text or None,
                )
            ]

        return [
            FunnelSyncFileChange(
                target="config_manager_yaml",
                path=yaml_path,
                action=action,
                before=None,
                after=None,
                changed=changed,
                messages=messages,
                before_text=before_text,
                after_text=after_text,
            )
        ]
