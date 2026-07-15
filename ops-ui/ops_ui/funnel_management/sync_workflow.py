"""Operator-facing sync workflow helpers (Funnel Management MK1)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..config import Settings
from ..shell import _mk04_env_token
from .dependency_paths import (
    FunnelDependencyPathError,
    FunnelDependencyPaths,
    normalize_funnel_environment,
    resolve_funnel_dependency_paths,
)
from .sync import FunnelSyncFileChange, FunnelSyncReport, FunnelSyncTargetPaths
from .validation import (
    FunnelValidationReport,
    pending_sync_issue_messages,
    processing_ready_after_sync,
    readiness_label,
)

ALLOWED_SYNC_ENVIRONMENTS = frozenset({"dev", "prod"})

_SYNC_TARGET_PENDING_CODE = {
    "source_input_funnels": "source_input_pending_sync",
    "video_funnel_json": "video_config_pending_sync",
    "funnel_rule_registry": "ai_registry_pending_sync",
    "ai_prompt_file": "ai_prompt_pending_sync",
    "config_manager_yaml": "config_manager_yaml_pending_sync",
}


class FunnelSyncWorkflowError(FunnelDependencyPathError):
    """Raised when sync workflow inputs or path resolution fail."""


def normalize_sync_environment(raw: str | None) -> str:
    try:
        return normalize_funnel_environment(raw)
    except FunnelDependencyPathError as exc:
        if "Invalid funnel environment" in str(exc):
            raise FunnelSyncWorkflowError(
                f"Invalid sync environment {raw!r}. Expected dev or prod."
            ) from exc
        raise FunnelSyncWorkflowError(str(exc)) from exc


@dataclass(frozen=True)
class SyncEnvironmentPaths:
    environment: str
    source_funnels_path: Path | None
    video_funnels_dir: Path | None
    output_channels_path: Path | None
    ai_rule_registry_path: Path | None
    ai_prompts_dir: Path | None
    config_manager_funnels_dir: Path | None
    path_kind: str
    warnings: tuple[str, ...]

    def to_target_paths(self) -> FunnelSyncTargetPaths:
        return FunnelSyncTargetPaths(
            source_funnels_path=self.source_funnels_path,
            video_funnels_dir=self.video_funnels_dir,
            output_channels_path=self.output_channels_path,
            ai_rule_registry_path=self.ai_rule_registry_path,
            ai_prompts_dir=self.ai_prompts_dir,
            config_manager_funnels_dir=self.config_manager_funnels_dir,
        )


@dataclass(frozen=True)
class SyncApplyForm:
    environment: str
    understand_confirmed: bool
    prod_confirmed: bool
    backup_requested: bool


def default_sync_environment(settings: Settings) -> str:
    """Default sync environment from app settings."""
    return _mk04_env_token(settings)


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _sync_path_kind(deps: FunnelDependencyPaths) -> str:
    if _env_path("MK04_CONFIG_ROOT") or _env_path("INPUT_SERVICE_CONFIG_DIR") or _env_path("OUTPUT_FUNNEL_CHANNELS"):
        return "runtime"
    if deps.environment == "prod":
        return "unconfigured"
    return "repository"


def resolve_sync_paths(environment: str) -> SyncEnvironmentPaths:
    """Resolve explicit sync target paths for the selected environment."""
    env = normalize_sync_environment(environment)
    warnings: list[str] = []
    deps = resolve_funnel_dependency_paths(environment=env)

    if env == "prod" and deps.source_funnels_path is None:
        warnings.append(
            "Production config root is not configured. Set MK04_CONFIG_ROOT or explicit "
            "INPUT_SERVICE_CONFIG_DIR / FUNNEL_CONFIG_DIR / OUTPUT_FUNNEL_CHANNELS."
        )
    if env == "prod":
        warnings.append("Production sync is high risk. Review paths carefully before applying.")
    if deps.source_funnels_path is None:
        warnings.append("Source-input funnels.json path could not be resolved.")

    return SyncEnvironmentPaths(
        environment=env,
        source_funnels_path=deps.source_funnels_path,
        video_funnels_dir=deps.video_funnels_dir,
        output_channels_path=deps.output_channels_path,
        ai_rule_registry_path=deps.ai_rule_registry_path,
        ai_prompts_dir=deps.ai_prompts_dir,
        config_manager_funnels_dir=deps.config_manager_funnels_dir,
        path_kind=_sync_path_kind(deps),
        warnings=tuple(warnings),
    )


def change_summary(change: FunnelSyncFileChange) -> str:
    """Compact operator-facing summary for one planned change."""
    if change.action == "error":
        return change.messages[0] if change.messages else "Error"
    if change.target == "source_input_funnels":
        if change.action == "create":
            return "source-input funnels.json: add new funnel entry"
        if change.action == "update":
            return "source-input funnels.json: update existing funnel entry"
        if change.action == "unchanged":
            return "source-input funnels.json: unchanged"
    if change.target == "video_funnel_json":
        if change.action == "create":
            return f"video funnel JSON: create {change.path.name}"
        if change.action == "update":
            return f"video funnel JSON: update {change.path.name}"
        if change.action == "unchanged":
            return f"video funnel JSON: {change.path.name} unchanged"
    if change.target == "output_channels":
        if change.action == "update":
            return "output-funnel channels.json: patch accepted_funnel_ids"
        if change.action == "unchanged":
            return "output-funnel channels.json: routing already includes funnel"
    if change.target == "funnel_rule_registry":
        if change.action == "create":
            return "AI registry: create profile/alias entries"
        if change.action == "update":
            return "AI registry: update profile/alias entries"
        if change.action == "unchanged":
            return "AI registry: unchanged"
        if change.action == "skipped":
            return "AI registry: skipped"
    if change.target == "ai_prompt_file":
        if change.action == "create":
            return f"AI prompt: create {change.path.name}"
        if change.action == "update":
            return f"AI prompt: update {change.path.name}"
        if change.action == "unchanged":
            return f"AI prompt: {change.path.name} unchanged"
        if change.action == "skipped":
            return "AI prompt: skipped (builtin profile)"
    if change.target == "config_manager_yaml":
        if change.action == "create":
            return f"ConfigManager YAML: create {change.path.name}"
        if change.action == "update":
            return f"ConfigManager YAML: update {change.path.name}"
        if change.action == "unchanged":
            return f"ConfigManager YAML: {change.path.name} unchanged"
        if change.action == "skipped":
            return "ConfigManager YAML: skipped"
    return f"{change.target}: {change.action}"


def parse_sync_apply_form(
    form: Mapping[str, Any],
    *,
    funnel_id: str,
) -> tuple[SyncApplyForm | None, list[str]]:
    errors: list[str] = []
    try:
        environment = normalize_sync_environment(form.get("environment"))
    except FunnelSyncWorkflowError as exc:
        return None, [str(exc)]

    understand = form.get("confirm_understand") in ("on", "true", "1", "yes")
    if not understand:
        errors.append("Confirm that you understand runtime config files will be written.")

    prod_confirmed = True
    if environment == "prod":
        typed = str(form.get("prod_confirm") or "").strip()
        if typed != funnel_id:
            errors.append(f"Type the funnel ID {funnel_id!r} to confirm production sync.")
            prod_confirmed = False

    backup_requested = form.get("request_backup") in ("on", "true", "1", "yes")

    if errors:
        return None, errors

    return (
        SyncApplyForm(
            environment=environment,
            understand_confirmed=understand,
            prod_confirmed=prod_confirmed,
            backup_requested=backup_requested or environment == "prod",
        ),
        [],
    )


def build_changed_files_flash(report: FunnelSyncReport) -> str:
    changed = [
        change_summary(item)
        for item in report.changes
        if item.changed and item.action in {"create", "update"}
    ]
    if not changed:
        return "Config sync completed with no file changes."
    return "Config sync applied: " + "; ".join(changed)


def build_sync_readiness_context(
    validation_report: FunnelValidationReport,
    sync_report: FunnelSyncReport,
) -> dict[str, Any]:
    """Summarise how sync apply affects processing readiness."""
    pending_codes = {
        issue.code
        for issue in validation_report.warnings
        if issue.code in _SYNC_TARGET_PENDING_CODE.values()
    }
    resolved_codes: set[str] = set()
    resolution_rows: list[str] = []
    for change in sync_report.changes:
        if not change.changed or change.action not in {"create", "update"}:
            continue
        pending_code = _SYNC_TARGET_PENDING_CODE.get(change.target)
        if pending_code is None:
            continue
        resolved_codes.add(pending_code)
        resolution_rows.append(change_summary(change))

    for message in pending_sync_issue_messages(validation_report):
        if message not in resolution_rows:
            resolution_rows.append(message)

    after_apply_ready = processing_ready_after_sync(
        validation_report,
        sync_report,
        resolved_pending_codes=resolved_codes,
    )

    after_apply_state = "ready" if after_apply_ready else validation_report.processing_state

    return {
        "sync_state": validation_report.sync_state,
        "sync_label": readiness_label(validation_report.sync_state),
        "processing_state": validation_report.processing_state,
        "processing_label": readiness_label(validation_report.processing_state),
        "processing_after_apply_state": after_apply_state,
        "processing_after_apply_label": readiness_label(after_apply_state),
        "resolution_rows": resolution_rows,
    }


def sync_page_context(
    *,
    funnel_id: str,
    display_name: str,
    environment: str,
    env_paths: SyncEnvironmentPaths,
    report: FunnelSyncReport,
    validation_report: FunnelValidationReport | None = None,
    form_errors: tuple[str, ...] = (),
    applied: bool = False,
) -> dict[str, Any]:
    """Template context for funnel_sync.html."""
    path_rows = [
        ("Source-input funnels", env_paths.source_funnels_path),
        ("Video funnels dir", env_paths.video_funnels_dir),
        ("Output channels", env_paths.output_channels_path),
        ("AI rule registry", env_paths.ai_rule_registry_path),
        ("AI prompts dir", env_paths.ai_prompts_dir),
        ("ConfigManager funnels", env_paths.config_manager_funnels_dir),
    ]
    return {
        "funnel_id": funnel_id,
        "display_name": display_name,
        "environment": environment,
        "environment_is_prod": environment == "prod",
        "path_kind": env_paths.path_kind,
        "path_rows": path_rows,
        "env_warnings": env_paths.warnings,
        "report": report,
        "readiness": (
            build_sync_readiness_context(validation_report, report)
            if validation_report is not None
            else None
        ),
        "change_summaries": [(change, change_summary(change)) for change in report.changes],
        "apply_allowed": report.ok and report.changed,
        "form_errors": form_errors,
        "applied": applied,
    }
