"""Derived funnel validation and readiness reporting (Funnel Management MK1).

Readiness is computed at validation time only — never persisted to canonical config.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .funnel_rule_registry_ops import (
    FunnelRuleRegistryOpsError,
    get_profile_rules_version,
    load_registry_document,
    validate_profile_and_prompt,
    validate_profile_id,
)
from .dependency_paths import resolve_funnel_dependency_paths
from .schema import (
    ALLOWED_PLATFORMS,
    CanonicalFunnel,
    CanonicalFunnelSchemaError,
    DEFAULT_PROMPT_MANAGED,
    dump_canonical_funnel,
    load_canonical_funnel,
)
from .config_manager_sync import resolve_config_manager_funnel_id


class FunnelValidationSeverity:
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class FunnelValidationIssue:
    code: str
    message: str
    severity: str
    section: str | None = None
    field: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class FunnelValidationReport:
    funnel_id: str
    valid_config: bool
    dependencies_ok: bool
    runnable: bool
    status: str
    sync_ready: bool
    processing_ready: bool
    posting_ready: bool
    sync_state: str
    processing_state: str
    posting_state: str
    errors: tuple[FunnelValidationIssue, ...]
    warnings: tuple[FunnelValidationIssue, ...]
    info: tuple[FunnelValidationIssue, ...]
    checked_at: str


_RUNNABLE_STATUSES = frozenset({"active", "testing"})

_SYNC_BLOCKING_CODES = frozenset(
    {
        "invalid_schema",
        "no_acquisition_sources",
        "missing_active_source",
        "invalid_ai_rule_profile",
        "missing_custom_prompt_text",
    }
)

_PENDING_SYNC_CODES = frozenset(
    {
        "source_input_pending_sync",
        "video_config_pending_sync",
        "ai_registry_pending_sync",
        "ai_prompt_pending_sync",
        "config_manager_yaml_pending_sync",
    }
)

PENDING_SYNC_CODES = _PENDING_SYNC_CODES

_PROCESSING_ERROR_CODES = frozenset(
    {
        "source_input_not_found",
        "processing_funnel_id_mismatch",
        "ai_rule_profile_mismatch",
        "missing_ai_prompt_file",
        "missing_ai_rule_alias",
        "ai_registry_unreadable",
        "missing_pipeline_profile",
        "invalid_processing_config",
    }
)

_POSTING_ERROR_CODES = frozenset(
    {
        "missing_output_route",
        "channel_route_not_found",
        "channel_platform_mismatch",
        "channel_route_not_accepting_funnel",
        "platform_without_route",
    }
)


@dataclass
class FunnelValidator:
    """Validate canonical funnel configuration and runtime dependency references."""

    source_funnels_path: Path | None = None
    video_funnels_dir: Path | None = None
    video_pipeline_profiles_path: Path | None = None
    output_channels_path: Path | None = None
    ai_rule_registry_path: Path | None = None
    ai_prompts_dir: Path | None = None
    config_manager_funnels_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.source_funnels_path is not None:
            self.source_funnels_path = Path(self.source_funnels_path).expanduser()
        if self.video_funnels_dir is not None:
            self.video_funnels_dir = Path(self.video_funnels_dir).expanduser()
        if self.video_pipeline_profiles_path is not None:
            self.video_pipeline_profiles_path = Path(self.video_pipeline_profiles_path).expanduser()
        if self.output_channels_path is not None:
            self.output_channels_path = Path(self.output_channels_path).expanduser()
        if self.ai_rule_registry_path is not None:
            self.ai_rule_registry_path = Path(self.ai_rule_registry_path).expanduser()
        if self.ai_prompts_dir is not None:
            self.ai_prompts_dir = Path(self.ai_prompts_dir).expanduser()
        if self.config_manager_funnels_dir is not None:
            self.config_manager_funnels_dir = Path(self.config_manager_funnels_dir).expanduser()

    def validate_funnel(self, funnel: CanonicalFunnel | dict[str, Any]) -> FunnelValidationReport:
        """Validate configuration and optional dependency references for a funnel."""
        checked_at = _utc_now_iso()
        errors: list[FunnelValidationIssue] = []
        warnings: list[FunnelValidationIssue] = []
        info: list[FunnelValidationIssue] = []

        parsed, funnel_id, valid_config = self._parse_funnel_input(funnel, errors)
        if parsed is None:
            return _build_report(
                funnel_id=funnel_id,
                valid_config=False,
                errors=errors,
                warnings=warnings,
                info=info,
                checked_at=checked_at,
                identity_enabled=False,
                identity_status="invalid",
                posting_enabled=False,
            )

        self._validate_config(parsed, errors, warnings)
        config_ok = not errors

        self._validate_acquisition_dependencies(parsed, errors, warnings, info)
        self._validate_processing_dependencies(parsed, errors, warnings, info)
        self._validate_ai_dependencies(parsed, errors, warnings, info)
        self._validate_distribution_dependencies(parsed, errors, warnings, info)
        self._validate_config_manager_dependencies(parsed, errors, warnings, info)

        dependency_errors = [issue for issue in errors if issue.code != "invalid_schema"]
        dependencies_ok = config_ok and not dependency_errors

        return _build_report(
            funnel_id=parsed.identity.funnel_id,
            valid_config=config_ok,
            errors=errors,
            warnings=warnings,
            info=info,
            checked_at=checked_at,
            identity_enabled=parsed.identity.enabled,
            identity_status=parsed.identity.status,
            posting_enabled=parsed.distribution.posting_enabled,
            dependencies_ok=dependencies_ok,
        )

    def _parse_funnel_input(
        self,
        funnel: CanonicalFunnel | dict[str, Any],
        errors: list[FunnelValidationIssue],
    ) -> tuple[CanonicalFunnel | None, str, bool]:
        funnel_id = "unknown"
        if isinstance(funnel, CanonicalFunnel):
            funnel_id = funnel.identity.funnel_id
            try:
                parsed = load_canonical_funnel(dump_canonical_funnel(funnel))
            except CanonicalFunnelSchemaError as exc:
                errors.append(
                    _issue(
                        "invalid_schema",
                        f"Canonical funnel configuration is invalid: {exc}",
                        FunnelValidationSeverity.ERROR,
                        section="schema",
                    )
                )
                return None, funnel_id, False
            return parsed, funnel_id, True

        if not isinstance(funnel, dict):
            errors.append(
                _issue(
                    "invalid_schema",
                    "Canonical funnel must be an object.",
                    FunnelValidationSeverity.ERROR,
                    section="schema",
                )
            )
            return None, funnel_id, False

        identity = funnel.get("identity")
        if isinstance(identity, dict):
            raw_id = identity.get("funnel_id")
            if isinstance(raw_id, str) and raw_id.strip():
                funnel_id = raw_id.strip()

        try:
            parsed = load_canonical_funnel(funnel)
        except CanonicalFunnelSchemaError as exc:
            errors.append(
                _issue(
                    "invalid_schema",
                    f"Canonical funnel configuration is invalid: {exc}",
                    FunnelValidationSeverity.ERROR,
                    section="schema",
                    field=str(exc),
                )
            )
            return None, funnel_id, False

        return parsed, parsed.identity.funnel_id, True

    def _validate_config(
        self,
        funnel: CanonicalFunnel,
        errors: list[FunnelValidationIssue],
        warnings: list[FunnelValidationIssue],
    ) -> None:
        if not funnel.acquisition.sources:
            errors.append(
                _issue(
                    "no_acquisition_sources",
                    "No acquisition sources are configured.",
                    FunnelValidationSeverity.ERROR,
                    section="acquisition",
                    field="sources",
                )
            )
            return

        active_sources = [source for source in funnel.acquisition.sources if source.active]
        if not active_sources:
            errors.append(
                _issue(
                    "missing_active_source",
                    "No active source is configured.",
                    FunnelValidationSeverity.ERROR,
                    section="acquisition",
                    field="sources",
                )
            )

        if funnel.distribution.posting_enabled and not funnel.distribution.channel_routes:
            errors.append(
                _issue(
                    "missing_output_route",
                    "Posting is enabled but no output channel routes are configured.",
                    FunnelValidationSeverity.ERROR,
                    section="distribution",
                    field="channel_routes",
                )
            )

    def _validate_acquisition_dependencies(
        self,
        funnel: CanonicalFunnel,
        errors: list[FunnelValidationIssue],
        warnings: list[FunnelValidationIssue],
        info: list[FunnelValidationIssue],
    ) -> None:
        if self.source_funnels_path is None:
            info.append(
                _issue(
                    "source_input_not_checked",
                    "Source-input dependencies were not checked because no path was provided.",
                    FunnelValidationSeverity.INFO,
                    section="acquisition",
                    source=None,
                )
            )
            return

        path = self.source_funnels_path
        if not path.is_file():
            errors.append(
                _issue(
                    "source_input_not_found",
                    "Source-input funnel configuration file was not found.",
                    FunnelValidationSeverity.ERROR,
                    section="acquisition",
                    source=str(path),
                )
            )
            return

        try:
            raw = _load_json(path)
        except _ValidationLoadError as exc:
            errors.append(
                _issue(
                    "source_input_not_found",
                    str(exc),
                    FunnelValidationSeverity.ERROR,
                    section="acquisition",
                    source=str(path),
                )
            )
            return

        entry = _find_source_funnel_entry(raw, funnel.identity.funnel_id)
        if entry is None:
            warnings.append(
                _issue(
                    "source_input_pending_sync",
                    f"Source-input config does not contain funnel {funnel.identity.funnel_id!r}; sync will add it.",
                    FunnelValidationSeverity.WARNING,
                    section="acquisition",
                    source=str(path),
                )
            )
            return

        source_active = bool(entry.get("active", True))
        if source_active != funnel.identity.enabled:
            warnings.append(
                _issue(
                    "source_input_active_mismatch",
                    "Source-input active flag does not match canonical identity.enabled.",
                    FunnelValidationSeverity.WARNING,
                    section="identity",
                    field="enabled",
                    source=str(path),
                )
            )

        if entry.get("posting_config"):
            warnings.append(
                _issue(
                    "source_input_posting_config_present",
                    "Source-input posting_config is present but distribution is canonical; verify distribution settings.",
                    FunnelValidationSeverity.WARNING,
                    section="distribution",
                    source=str(path),
                )
            )
        if entry.get("analytics_config"):
            info.append(
                _issue(
                    "source_input_analytics_present",
                    "Source-input analytics_config was ignored during validation (out of scope for canonical config).",
                    FunnelValidationSeverity.INFO,
                    section="acquisition",
                    source=str(path),
                )
            )

    def _validate_processing_dependencies(
        self,
        funnel: CanonicalFunnel,
        errors: list[FunnelValidationIssue],
        warnings: list[FunnelValidationIssue],
        info: list[FunnelValidationIssue],
    ) -> None:
        if self.video_funnels_dir is None:
            warnings.append(
                _issue(
                    "processing_dependencies_not_checked",
                    "Video processing dependencies were not checked because no path was provided.",
                    FunnelValidationSeverity.WARNING,
                    section="processing",
                )
            )
        else:
            video_path = self.video_funnels_dir / f"{funnel.identity.funnel_id}.json"
            if not video_path.is_file():
                warnings.append(
                    _issue(
                        "video_config_pending_sync",
                        f"Video processing config is missing; sync will create {video_path.name}.",
                        FunnelValidationSeverity.WARNING,
                        section="processing",
                        source=str(video_path),
                    )
                )
            else:
                try:
                    video_entry = _load_json(video_path)
                except _ValidationLoadError as exc:
                    errors.append(
                        _issue(
                            "invalid_processing_config",
                            str(exc),
                            FunnelValidationSeverity.ERROR,
                            section="processing",
                            source=str(video_path),
                        )
                    )
                    video_entry = None

                if isinstance(video_entry, dict):
                    file_id = str(video_entry.get("funnel_id") or "").strip()
                    if file_id and file_id != funnel.identity.funnel_id:
                        errors.append(
                            _issue(
                                "processing_funnel_id_mismatch",
                                f"Processing config funnel ID {file_id!r} does not match {funnel.identity.funnel_id!r}.",
                                FunnelValidationSeverity.ERROR,
                                section="processing",
                                field="funnel_id",
                                source=str(video_path),
                            )
                        )

                    video_platforms = video_entry.get("platforms")
                    if isinstance(video_platforms, dict):
                        video_enabled = {
                            name
                            for name, enabled in video_platforms.items()
                            if name in ALLOWED_PLATFORMS and bool(enabled)
                        }
                        canonical_targets = set(funnel.distribution.target_platforms)
                        if video_enabled != canonical_targets:
                            warnings.append(
                                _issue(
                                    "processing_platform_mismatch",
                                    "Video processing platform flags differ from canonical distribution target platforms.",
                                    FunnelValidationSeverity.WARNING,
                                    section="processing",
                                    field="platforms",
                                    source=str(video_path),
                                )
                            )

        if self.video_pipeline_profiles_path is None:
            info.append(
                _issue(
                    "pipeline_profiles_not_checked",
                    "Pipeline profile dependencies were not checked because no path was provided.",
                    FunnelValidationSeverity.INFO,
                    section="processing",
                )
            )
        else:
            profile = funnel.processing.pipeline_profile
            if profile == "business_podcasts_001":
                warnings.append(
                    _issue(
                        "legacy_pipeline_profile",
                        "Pipeline profile business_podcasts_001 is a legacy profile reference.",
                        FunnelValidationSeverity.WARNING,
                        section="processing",
                        field="pipeline_profile",
                    )
                )

            if profile != funnel.identity.funnel_id:
                profiles_path = self.video_pipeline_profiles_path
                if not profiles_path.is_file():
                    errors.append(
                        _issue(
                            "missing_pipeline_profile",
                            "Pipeline profiles file was not found.",
                            FunnelValidationSeverity.ERROR,
                            section="processing",
                            source=str(profiles_path),
                        )
                    )
                else:
                    try:
                        profiles_raw = _load_json(profiles_path)
                    except _ValidationLoadError as exc:
                        errors.append(
                            _issue(
                                "missing_pipeline_profile",
                                str(exc),
                                FunnelValidationSeverity.ERROR,
                                section="processing",
                                source=str(profiles_path),
                            )
                        )
                        profiles_raw = None

                    profiles = (
                        profiles_raw.get("profiles")
                        if isinstance(profiles_raw, dict)
                        else None
                    )
                    if isinstance(profiles, dict) and profile not in profiles:
                        errors.append(
                            _issue(
                                "missing_pipeline_profile",
                                f"Pipeline profile {profile!r} was not found in video pipeline profiles.",
                                FunnelValidationSeverity.ERROR,
                                section="processing",
                                field="pipeline_profile",
                                source=str(profiles_path),
                            )
                        )

    def _validate_ai_dependencies(
        self,
        funnel: CanonicalFunnel,
        errors: list[FunnelValidationIssue],
        warnings: list[FunnelValidationIssue],
        info: list[FunnelValidationIssue],
    ) -> None:
        ai_rules = funnel.processing.ai_rules
        expected_profile = ai_rules.ai_rule_profile
        prompt_managed = ai_rules.prompt_managed or DEFAULT_PROMPT_MANAGED
        funnel_id = funnel.identity.funnel_id

        if prompt_managed == "custom":
            profile_error = validate_profile_id(expected_profile)
            if profile_error:
                errors.append(
                    _issue(
                        "invalid_ai_rule_profile",
                        profile_error,
                        FunnelValidationSeverity.ERROR,
                        section="processing",
                        field="ai_rules.ai_rule_profile",
                    )
                )
                return
            if not str(ai_rules.prompt_text or "").strip():
                errors.append(
                    _issue(
                        "missing_custom_prompt_text",
                        "Custom AI profile requires prompt text.",
                        FunnelValidationSeverity.ERROR,
                        section="processing",
                        field="ai_rules.prompt_text",
                    )
                )
                return

            if self.ai_rule_registry_path is None:
                warnings.append(
                    _issue(
                        "ai_dependencies_not_checked",
                        "Custom AI profile will be created during sync; registry path was not provided for validation.",
                        FunnelValidationSeverity.WARNING,
                        section="processing",
                        field="ai_rules.ai_rule_profile",
                    )
                )
                return

            if not self.ai_rule_registry_path.is_file():
                warnings.append(
                    _issue(
                        "ai_registry_pending_sync",
                        "AI rule registry file is missing; sync will create profile and alias entries.",
                        FunnelValidationSeverity.WARNING,
                        section="processing",
                        source=str(self.ai_rule_registry_path),
                    )
                )
                return

            try:
                registry = load_registry_document(self.ai_rule_registry_path)
            except FunnelRuleRegistryOpsError as exc:
                message = str(exc)
                code = (
                    "ai_registry_unreadable"
                    if "not readable" in message.lower() or "could not be read" in message.lower()
                    else "missing_ai_rule_alias"
                )
                errors.append(
                    _issue(
                        code,
                        message,
                        FunnelValidationSeverity.ERROR,
                        section="processing",
                        source=str(self.ai_rule_registry_path),
                    )
                )
                return

            aliases = registry.get("aliases")
            resolved = None
            if isinstance(aliases, dict):
                resolved = aliases.get(funnel_id) or aliases.get(funnel_id.lower())
            if resolved != expected_profile:
                warnings.append(
                    _issue(
                        "ai_registry_pending_sync",
                        f"AI alias for {funnel_id!r} will be created or updated during sync.",
                        FunnelValidationSeverity.WARNING,
                        section="processing",
                        field="ai_rules.ai_rule_profile",
                        source=str(self.ai_rule_registry_path),
                    )
                )

            rules_version = get_profile_rules_version(registry, expected_profile)
            if rules_version is None:
                warnings.append(
                    _issue(
                        "ai_registry_pending_sync",
                        f"AI profile {expected_profile!r} will be created during sync.",
                        FunnelValidationSeverity.WARNING,
                        section="processing",
                        field="ai_rules.ai_rule_profile",
                        source=str(self.ai_rule_registry_path),
                    )
                )
                return

            if self.ai_prompts_dir is None:
                info.append(
                    _issue(
                        "ai_prompts_not_checked",
                        "AI prompt file dependencies were not checked because no prompts directory was provided.",
                        FunnelValidationSeverity.INFO,
                        section="processing",
                    )
                )
                return

            prompt_path = self.ai_prompts_dir / f"{rules_version}.txt"
            if not prompt_path.is_file():
                warnings.append(
                    _issue(
                        "ai_prompt_pending_sync",
                        f"Custom AI prompt file {prompt_path.name} will be created during sync.",
                        FunnelValidationSeverity.WARNING,
                        section="processing",
                        source=str(prompt_path),
                    )
                )
            return

        if self.ai_rule_registry_path is None:
            warnings.append(
                _issue(
                    "ai_dependencies_not_checked",
                    "AI rule dependencies were not checked because no registry path was provided.",
                    FunnelValidationSeverity.WARNING,
                    section="processing",
                    field="ai_rules.ai_rule_profile",
                )
            )
            return

        if not self.ai_rule_registry_path.is_file():
            errors.append(
                _issue(
                    "missing_ai_rule_alias",
                    "AI rule registry file was not found.",
                    FunnelValidationSeverity.ERROR,
                    section="processing",
                    source=str(self.ai_rule_registry_path),
                )
            )
            return

        try:
            registry = load_registry_document(self.ai_rule_registry_path)
        except FunnelRuleRegistryOpsError as exc:
            message = str(exc)
            code = (
                "ai_registry_unreadable"
                if "not readable" in message.lower() or "could not be read" in message.lower()
                else "missing_ai_rule_alias"
            )
            errors.append(
                _issue(
                    code,
                    message,
                    FunnelValidationSeverity.ERROR,
                    section="processing",
                    source=str(self.ai_rule_registry_path),
                )
            )
            return

        aliases = registry.get("aliases")
        resolved = None
        if isinstance(aliases, dict):
            resolved = aliases.get(funnel_id) or aliases.get(funnel_id.lower())
        if not resolved:
            warnings.append(
                _issue(
                    "ai_registry_pending_sync",
                    f"AI alias for funnel {funnel_id!r} is missing; sync will map it to profile {expected_profile!r}.",
                    FunnelValidationSeverity.WARNING,
                    section="processing",
                    field="ai_rules.ai_rule_profile",
                    source=str(self.ai_rule_registry_path),
                )
            )
            return

        if resolved != expected_profile:
            errors.append(
                _issue(
                    "ai_rule_profile_mismatch",
                    f"AI alias resolves to {resolved!r} but canonical ai_rule_profile is {expected_profile!r}.",
                    FunnelValidationSeverity.ERROR,
                    section="processing",
                    field="ai_rules.ai_rule_profile",
                    source=str(self.ai_rule_registry_path),
                )
            )
            return

        profile_errors = validate_profile_and_prompt(
            registry,
            profile_id=expected_profile,
            prompts_dir=self.ai_prompts_dir,
        )
        for message in profile_errors:
            errors.append(
                _issue(
                    "missing_ai_prompt_file",
                    message,
                    FunnelValidationSeverity.ERROR,
                    section="processing",
                    field="ai_rules.ai_rule_profile",
                    source=str(self.ai_rule_registry_path),
                )
            )

    def _validate_distribution_dependencies(
        self,
        funnel: CanonicalFunnel,
        errors: list[FunnelValidationIssue],
        warnings: list[FunnelValidationIssue],
        info: list[FunnelValidationIssue],
    ) -> None:
        posting_enabled = funnel.distribution.posting_enabled

        if not posting_enabled:
            warnings.append(
                _issue(
                    "posting_disabled",
                    "Posting is disabled for this funnel.",
                    FunnelValidationSeverity.WARNING,
                    section="distribution",
                    field="posting_enabled",
                )
            )

        if self.output_channels_path is None:
            warnings.append(
                _issue(
                    "output_channels_not_checked",
                    "Output routing dependencies were not checked because no path was provided.",
                    FunnelValidationSeverity.WARNING,
                    section="distribution",
                )
            )
            return

        path = self.output_channels_path
        if not path.is_file():
            issue = _issue(
                "missing_output_route",
                "Output channel configuration file was not found.",
                FunnelValidationSeverity.ERROR if posting_enabled else FunnelValidationSeverity.WARNING,
                section="distribution",
                source=str(path),
            )
            if posting_enabled:
                errors.append(issue)
            else:
                warnings.append(issue)
            return

        try:
            raw = _load_json(path)
        except _ValidationLoadError as exc:
            issue = _issue(
                "missing_output_route",
                str(exc),
                FunnelValidationSeverity.ERROR if posting_enabled else FunnelValidationSeverity.WARNING,
                section="distribution",
                source=str(path),
            )
            if posting_enabled:
                errors.append(issue)
            else:
                warnings.append(issue)
            return

        channels = raw.get("channels") if isinstance(raw, dict) else None
        if not isinstance(channels, list):
            issue = _issue(
                "missing_output_route",
                "Output channel configuration did not contain a channels list.",
                FunnelValidationSeverity.ERROR if posting_enabled else FunnelValidationSeverity.WARNING,
                section="distribution",
                source=str(path),
            )
            if posting_enabled:
                errors.append(issue)
            else:
                warnings.append(issue)
            return

        channel_index = {
            str(item.get("channel_id")).strip(): item
            for item in channels
            if isinstance(item, dict) and str(item.get("channel_id") or "").strip()
        }

        accepting_enabled_routes = 0
        for route in funnel.distribution.channel_routes:
            channel = channel_index.get(route.channel_id)
            if channel is None:
                issue = _issue(
                    "channel_route_not_found",
                    f"Output channel route {route.channel_id!r} was not found.",
                    FunnelValidationSeverity.ERROR if posting_enabled else FunnelValidationSeverity.WARNING,
                    section="distribution",
                    field="channel_routes",
                    source=str(path),
                )
                if posting_enabled:
                    errors.append(issue)
                else:
                    warnings.append(issue)
                continue

            channel_platform = str(channel.get("platform") or "").strip()
            if channel_platform and channel_platform != route.platform:
                issue = _issue(
                    "channel_platform_mismatch",
                    f"Channel route {route.channel_id!r} platform does not match the output channel config.",
                    FunnelValidationSeverity.ERROR if posting_enabled else FunnelValidationSeverity.WARNING,
                    section="distribution",
                    field="platform",
                    source=str(path),
                )
                if posting_enabled:
                    errors.append(issue)
                else:
                    warnings.append(issue)

            routing = channel.get("routing") if isinstance(channel.get("routing"), dict) else {}
            accepted = routing.get("accepted_funnel_ids")
            accepted_ids = accepted if isinstance(accepted, list) else []
            if funnel.identity.funnel_id not in accepted_ids:
                issue = _issue(
                    "channel_route_not_accepting_funnel",
                    f"No output route accepts funnel {funnel.identity.funnel_id!r} on channel {route.channel_id!r}.",
                    FunnelValidationSeverity.ERROR if posting_enabled else FunnelValidationSeverity.WARNING,
                    section="distribution",
                    field="channel_routes",
                    source=str(path),
                )
                if posting_enabled:
                    errors.append(issue)
                else:
                    warnings.append(issue)
            elif route.enabled:
                accepting_enabled_routes += 1

        routed_platforms = {
            route.platform
            for route in funnel.distribution.channel_routes
            if route.enabled
        }
        for platform in funnel.distribution.target_platforms:
            if platform not in routed_platforms:
                severity = (
                    FunnelValidationSeverity.ERROR
                    if posting_enabled
                    else FunnelValidationSeverity.WARNING
                )
                issue = _issue(
                    "platform_without_route",
                    f"Target platform {platform!r} has no enabled output channel route.",
                    severity,
                    section="distribution",
                    field="target_platforms",
                    source=str(path),
                )
                if severity == FunnelValidationSeverity.ERROR:
                    errors.append(issue)
                else:
                    warnings.append(issue)

        if posting_enabled and accepting_enabled_routes == 0:
            errors.append(
                _issue(
                    "missing_output_route",
                    "No output route accepts this funnel while posting is enabled.",
                    FunnelValidationSeverity.ERROR,
                    section="distribution",
                    field="channel_routes",
                    source=str(path),
                )
            )
        elif not posting_enabled and not funnel.distribution.channel_routes:
            warnings.append(
                _issue(
                    "missing_output_route",
                    "No output channel routes are configured.",
                    FunnelValidationSeverity.WARNING,
                    section="distribution",
                    field="channel_routes",
                    source=str(path),
                )
            )

    def _validate_config_manager_dependencies(
        self,
        funnel: CanonicalFunnel,
        errors: list[FunnelValidationIssue],
        warnings: list[FunnelValidationIssue],
        info: list[FunnelValidationIssue],
    ) -> None:
        mapping = resolve_config_manager_funnel_id(funnel)

        if self.config_manager_funnels_dir is None:
            info.append(
                _issue(
                    "config_manager_not_checked",
                    "ConfigManager mapping was not checked because no path was provided.",
                    FunnelValidationSeverity.INFO,
                    section="mappings",
                )
            )
            return

        yaml_path = self.config_manager_funnels_dir / f"{mapping}.yaml"
        if not yaml_path.is_file():
            warnings.append(
                _issue(
                    "config_manager_yaml_pending_sync",
                    f"ConfigManager funnel file for mapping {mapping!r} will be created during sync.",
                    FunnelValidationSeverity.WARNING,
                    section="mappings",
                    field="config_manager_funnel_id",
                    source=str(yaml_path),
                )
            )


class _ValidationLoadError(Exception):
    pass


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _issue(
    code: str,
    message: str,
    severity: str,
    *,
    section: str | None = None,
    field: str | None = None,
    source: str | None = None,
) -> FunnelValidationIssue:
    return FunnelValidationIssue(
        code=code,
        message=message,
        severity=severity,
        section=section,
        field=field,
        source=source,
    )


def _build_report(
    *,
    funnel_id: str,
    valid_config: bool,
    errors: list[FunnelValidationIssue],
    warnings: list[FunnelValidationIssue],
    info: list[FunnelValidationIssue],
    checked_at: str,
    identity_enabled: bool,
    identity_status: str,
    posting_enabled: bool,
    dependencies_ok: bool | None = None,
) -> FunnelValidationReport:
    error_codes = {issue.code for issue in errors}
    warning_codes = {issue.code for issue in warnings}

    processing_errors = error_codes.intersection(_PROCESSING_ERROR_CODES)
    processing_sync_ready = valid_config and not error_codes.intersection(
        _SYNC_BLOCKING_CODES | _PROCESSING_ERROR_CODES
    )
    posting_sync_blockers = (
        error_codes.intersection(_POSTING_ERROR_CODES) if posting_enabled else set()
    )
    sync_ready = processing_sync_ready and not posting_sync_blockers

    pending_sync = warning_codes.intersection(_PENDING_SYNC_CODES)
    processing_ready = processing_sync_ready and not processing_errors and not pending_sync

    if not posting_enabled:
        posting_ready = processing_ready
        posting_state = "disabled"
    else:
        posting_errors = error_codes.intersection(_POSTING_ERROR_CODES)
        posting_ready = processing_ready and not posting_errors
        posting_state = "ready" if posting_ready else "blocked"

    sync_state = "ready" if sync_ready else "blocked"
    if processing_ready:
        processing_state = "ready"
    elif sync_ready and pending_sync:
        processing_state = "pending_sync"
    else:
        processing_state = "blocked"

    if dependencies_ok is None:
        dependency_errors = [issue for issue in errors if issue.code != "invalid_schema"]
        dependencies_ok = valid_config and not dependency_errors

    if not valid_config:
        status = "invalid"
    elif not processing_ready:
        status = "incomplete" if sync_ready else "invalid"
    elif not posting_ready and posting_enabled:
        status = "incomplete"
    elif warnings:
        status = "warning"
    else:
        status = "ready"

    runnable = (
        processing_ready
        and identity_enabled
        and identity_status in _RUNNABLE_STATUSES
    )

    return FunnelValidationReport(
        funnel_id=funnel_id,
        valid_config=valid_config,
        dependencies_ok=dependencies_ok,
        runnable=runnable,
        status=status,
        sync_ready=sync_ready,
        processing_ready=processing_ready,
        posting_ready=posting_ready,
        sync_state=sync_state,
        processing_state=processing_state,
        posting_state=posting_state,
        errors=tuple(errors),
        warnings=tuple(warnings),
        info=tuple(info),
        checked_at=checked_at,
    )


def readiness_label(state: str) -> str:
    """Human-readable readiness state label for templates."""
    return {
        "ready": "Ready",
        "blocked": "Blocked",
        "pending_sync": "Pending sync",
        "disabled": "Disabled",
    }.get(state, state.replace("_", " ").title())


def operational_state_label(
    *,
    report: FunnelValidationReport,
    identity_enabled: bool,
    identity_status: str,
    paused: bool,
) -> str:
    """Operational run label separate from configuration readiness."""
    if paused:
        return "Paused"
    if identity_status == "draft":
        return "Draft"
    if not identity_enabled:
        return "Disabled"
    if report.runnable:
        return "Runnable"
    if report.processing_ready and identity_status in _RUNNABLE_STATUSES:
        return "Blocked"
    if identity_status not in _RUNNABLE_STATUSES:
        return identity_status.replace("_", " ").title()
    return "Blocked"


def pending_sync_issue_messages(report: FunnelValidationReport) -> list[str]:
    return [issue.message for issue in report.warnings if issue.code in _PENDING_SYNC_CODES]


POSTING_ONLY_WARNING_CODES = frozenset(
    _POSTING_ERROR_CODES
    | {
        "posting_disabled",
        "output_channels_not_checked",
        "processing_platform_mismatch",
    }
)


def processing_blocker_issues(report: FunnelValidationReport) -> tuple[FunnelValidationIssue, ...]:
    """Canonical processing issues that block a test run."""
    blockers: list[FunnelValidationIssue] = []
    for issue in report.errors:
        if issue.code in _POSTING_ERROR_CODES:
            continue
        blockers.append(issue)
    for issue in report.warnings:
        if issue.code in _PENDING_SYNC_CODES:
            blockers.append(issue)
    return tuple(blockers)


def test_run_available(*, report: FunnelValidationReport, can_run: bool) -> bool:
    """Canonical Run Test availability."""
    return report.runnable and can_run


def processing_ready_after_sync(
    validation_report: FunnelValidationReport,
    sync_report: FunnelSyncReport,
    *,
    resolved_pending_codes: set[str] | None = None,
) -> bool:
    """Predict processing readiness after a successful sync apply."""
    if validation_report.processing_ready:
        return True
    if not sync_report.ok or not validation_report.sync_ready:
        return False
    error_codes = {issue.code for issue in validation_report.errors}
    if error_codes.intersection(_PROCESSING_ERROR_CODES):
        return False
    pending = {issue.code for issue in validation_report.warnings if issue.code in _PENDING_SYNC_CODES}
    if not pending:
        return False
    if resolved_pending_codes is None:
        return False
    return pending.issubset(resolved_pending_codes)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise _ValidationLoadError(f"Malformed JSON in {path.name}: {exc.msg}") from exc


def _find_source_funnel_entry(raw: Any, funnel_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, list):
        return None
    for item in raw:
        if isinstance(item, dict) and str(item.get("funnel_id") or "").strip() == funnel_id:
            return item
    return None
