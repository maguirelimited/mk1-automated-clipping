#!/usr/bin/env python3
"""End-to-end smoke checks for Funnel Management MK1.

Exercises template → registry → clone → edit → validate → sync preview →
sync apply against temporary fixture config only. Does not touch production
runtime config, deploy trees, or live service state by default.

Usage:
    ops-ui/.venv/bin/python scripts/smoke/smoke_funnel_management.py
    cd ops-ui && .venv/bin/pytest tests/smoke/test_funnel_management_smoke.py -q
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_UI_ROOT = REPO_ROOT / "ops-ui"

if str(OPS_UI_ROOT) not in sys.path:
    sys.path.insert(0, str(OPS_UI_ROOT))

# Fixture funnel IDs — do not use mfm_business_ai_001 as a write target.
CREATED_FUNNEL_ID = "smoke_created_funnel_001"
CLONE_FUNNEL_ID = "smoke_clone_funnel_001"
SOURCE_FUNNEL_ID = "smoke_source_funnel_001"
UNRELATED_SOURCE_ID = "smoke_unrelated_funnel_001"
TEMPLATE_FUNNEL_ID = "smoke_template_funnel_001"
UNRELATED_VIDEO_ID = "smoke_unrelated_video_001"
CHANNEL_ID = "smoke_youtube_primary"
OTHER_CHANNEL_ID = "smoke_other_channel"
AI_PROFILE = "business"
TEMPLATE_ID = "youtube_podcast_basic"


@dataclass
class CheckResult:
    name: str
    outcome: str  # PASS | WARN | FAIL | SKIP
    detail: str = ""


@dataclass
class SmokeReport:
    mode: str
    temp_root: str
    started_at: str
    finished_at: str = ""
    steps_completed: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    overall: str = "FAIL"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def overall_from_checks(checks: list[CheckResult]) -> str:
    if any(c.outcome == "FAIL" for c in checks):
        return "FAIL"
    if any(c.outcome == "WARN" for c in checks):
        return "WARN"
    return "PASS"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot(paths: list[Path]) -> dict[str, str]:
    return {str(path): _sha256(path) for path in paths if path.is_file()}


def _source_fixture() -> list[dict[str, Any]]:
    return [
        {
            "funnel_id": UNRELATED_SOURCE_ID,
            "angle": "unrelated active funnel",
            "source_type": "youtube_channels",
            "sources": [],
            "min_duration_minutes": 20,
            "max_duration_minutes": 180,
            "max_downloads_per_run": 1,
            "active": True,
            "posting_config": {"enabled": True, "mode": "manual_review"},
            "analytics_config": {"enabled": True, "event_namespace": "unrelated"},
        },
        {
            "funnel_id": TEMPLATE_FUNNEL_ID,
            "angle": "inactive template",
            "source_type": "youtube_channels",
            "sources": [],
            "min_duration_minutes": 15,
            "max_duration_minutes": 120,
            "max_downloads_per_run": 1,
            "active": False,
            "posting_config": {"enabled": False, "mode": "manual_review"},
            "analytics_config": {"enabled": False, "event_namespace": "template"},
        },
        {
            "funnel_id": SOURCE_FUNNEL_ID,
            "angle": "existing source funnel",
            "source_type": "youtube_channels",
            "sources": [],
            "min_duration_minutes": 25,
            "max_duration_minutes": 180,
            "max_downloads_per_run": 1,
            "active": False,
            "posting_config": {"enabled": False, "mode": "manual_review"},
            "analytics_config": {"enabled": False, "event_namespace": "source"},
        },
    ]


def _channels_fixture() -> dict[str, Any]:
    return {
        "channels": [
            {
                "channel_id": CHANNEL_ID,
                "brand_name": "Smoke YouTube Primary",
                "platform": "youtube_shorts",
                "enabled": True,
                "priority": 10,
                "credentials": {"token_file_env": "SMOKE_YT_TOKEN_FILE"},
                "routing": {
                    "accepted_funnel_ids": [UNRELATED_SOURCE_ID],
                    "min_composite_score": 0,
                    "required_platform": "youtube_shorts",
                },
                "cadence": {"timezone": "UTC", "min_gap_minutes": 120},
                "metadata_style": {"default_hashtags": ["#SmokeTest"]},
            },
            {
                "channel_id": OTHER_CHANNEL_ID,
                "brand_name": "Smoke Other Channel",
                "platform": "youtube_shorts",
                "enabled": True,
                "priority": 5,
                "credentials": {"token_file_env": "SMOKE_OTHER_TOKEN_FILE"},
                "routing": {
                    "accepted_funnel_ids": ["smoke_other_accepted_001"],
                    "min_composite_score": 0,
                    "required_platform": "youtube_shorts",
                },
                "cadence": {"timezone": "UTC", "min_gap_minutes": 240},
                "metadata_style": {"default_hashtags": ["#Other"]},
            },
        ]
    }


def _ai_registry_fixture() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profiles": {
            AI_PROFILE: {"rules_version": "business_v1", "managed": "builtin"},
        },
        "aliases": {
            SOURCE_FUNNEL_ID: AI_PROFILE,
        },
    }


@dataclass
class FixtureLayout:
    root: Path
    registry_dir: Path
    source_funnels: Path
    video_dir: Path
    channels: Path
    ai_registry: Path
    ai_prompts: Path
    config_manager_dir: Path
    pipeline_config: Path
    video_profiles: Path
    guard_files: list[Path]

    def apply_env(self) -> None:
        os.environ["OPS_FUNNEL_REGISTRY_DIR"] = str(self.registry_dir)
        os.environ["MK04_CONFIG_ROOT"] = str(self.root / "fixtures")
        os.environ["MK04_ENV"] = "dev"
        os.environ["INPUT_SERVICE_CONFIG_DIR"] = str(self.source_funnels.parent)
        os.environ["FUNNEL_CONFIG_DIR"] = str(self.video_dir)
        os.environ["OUTPUT_FUNNEL_CHANNELS"] = str(self.channels)
        os.environ["AI_FUNNEL_RULE_REGISTRY"] = str(self.ai_registry)
        os.environ["AI_FUNNEL_RULES_DIR"] = str(self.ai_prompts)
        os.environ["CONFIG_MANAGER_FUNNELS_DIR"] = str(self.config_manager_dir)

    def validator(self):
        from ops_ui.funnel_management.validation import FunnelValidator

        return FunnelValidator(
            source_funnels_path=self.source_funnels,
            video_funnels_dir=self.video_dir,
            video_pipeline_profiles_path=self.video_profiles,
            output_channels_path=self.channels,
            ai_rule_registry_path=self.ai_registry,
            ai_prompts_dir=self.ai_prompts,
            config_manager_funnels_dir=self.config_manager_dir,
        )

    def synchronizer(self):
        from ops_ui.funnel_management.sync import FunnelSyncTargetPaths, FunnelSynchronizer

        return FunnelSynchronizer(
            FunnelSyncTargetPaths(
                source_funnels_path=self.source_funnels,
                video_funnels_dir=self.video_dir,
                output_channels_path=self.channels,
                ai_rule_registry_path=self.ai_registry,
                ai_prompts_dir=self.ai_prompts,
                config_manager_funnels_dir=self.config_manager_dir,
            )
        )


def build_fixture_layout(root: Path) -> FixtureLayout:
    registry_dir = root / "registry"
    registry_dir.mkdir(parents=True)

    fixtures = root / "fixtures"
    source_dir = fixtures / "source-input"
    source_dir.mkdir(parents=True)
    source_funnels = source_dir / "funnels.json"
    source_funnels.write_text(json.dumps(_source_fixture(), indent=2), encoding="utf-8")

    video_dir = fixtures / "video-automation" / "funnels"
    video_dir.mkdir(parents=True)
    unrelated_video = {
        "funnel_id": UNRELATED_VIDEO_ID,
        "platforms": {"youtube_shorts": True},
    }
    (video_dir / f"{UNRELATED_VIDEO_ID}.json").write_text(
        json.dumps(unrelated_video, indent=2),
        encoding="utf-8",
    )

    channels_path = fixtures / "output-funnel" / "channels.json"
    channels_path.parent.mkdir(parents=True)
    channels_path.write_text(json.dumps(_channels_fixture(), indent=2), encoding="utf-8")

    ai_dir = fixtures / "ai-service"
    ai_dir.mkdir(parents=True)
    ai_registry = ai_dir / "config" / "funnel_rule_registry.json"
    ai_registry.parent.mkdir(parents=True, exist_ok=True)
    ai_registry.write_text(json.dumps(_ai_registry_fixture(), indent=2) + "\n", encoding="utf-8")
    ai_prompts = ai_dir / "prompts" / "funnel_rules"
    ai_prompts.mkdir(parents=True)
    (ai_prompts / "business_v1.txt").write_text("smoke prompt rules\n", encoding="utf-8")

    config_manager_dir = fixtures / "config" / "funnels"
    config_manager_dir.mkdir(parents=True)
    (config_manager_dir / f"{AI_PROFILE}.yaml").write_text(
        "funnel:\n  id: business\n",
        encoding="utf-8",
    )

    decoy_dir = root / "decoy"
    decoy_dir.mkdir(parents=True)
    pipeline_config = decoy_dir / "pipeline_config.json"
    pipeline_config.write_text('{"smoke_guard": true}\n', encoding="utf-8")
    video_profiles = decoy_dir / "video_pipeline_profiles.json"
    video_profiles.write_text('{"profiles": {}}\n', encoding="utf-8")

    guard_files = [
        pipeline_config,
        video_profiles,
    ]

    return FixtureLayout(
        root=root,
        registry_dir=registry_dir,
        source_funnels=source_funnels,
        video_dir=video_dir,
        channels=channels_path,
        ai_registry=ai_registry,
        ai_prompts=ai_prompts,
        config_manager_dir=config_manager_dir,
        pipeline_config=pipeline_config,
        video_profiles=video_profiles,
        guard_files=guard_files,
    )


def _edit_post_data(form: dict[str, Any]) -> dict[str, str]:
    data: dict[str, str] = {
        "funnel_id": form["funnel_id"],
        "display_name": form["display_name"],
        "description": form["description"],
        "category": form["category"],
        "status": form["status"],
        "environment": form["environment"],
        "operator_note": form["operator_note"],
        "created_at": form["created_at"],
        "template_source": form["template_source"],
        "acquisition_source_type": form["acquisition_source_type"],
        "min_duration_minutes": form["min_duration_minutes"],
        "max_duration_minutes": form["max_duration_minutes"],
        "max_downloads_per_run": form["max_downloads_per_run"],
        "pipeline_profile": form["pipeline_profile"],
        "ai_rule_profile": form["ai_rule_profile"],
        "max_clips": form["max_clips"],
        "min_clip_duration_sec": form["min_clip_duration_sec"],
        "max_clip_duration_sec": form["max_clip_duration_sec"],
        "max_overlap_sec": form["max_overlap_sec"],
        "filename_prefix": form["filename_prefix"],
        "delivery_mode": form["delivery_mode"],
        "posting_mode": form["posting_mode"],
        "config_manager_funnel_id": form["config_manager_funnel_id"],
        "source_count": str(len(form["sources"])),
        "route_count": str(len(form["routes"])),
    }
    if form.get("enabled"):
        data["enabled"] = "on"
    if form.get("posting_enabled"):
        data["posting_enabled"] = "on"
    for platform, value in form.get("platforms", {}).items():
        if value:
            data[f"platform_{platform}"] = "on"
    for platform, value in form.get("target_platforms", {}).items():
        if value:
            data[f"target_{platform}"] = "on"
    for index, source in enumerate(form["sources"]):
        prefix = f"source_{index}_"
        for key, value in source.items():
            if key == "active" and value:
                data[f"{prefix}{key}"] = "on"
            elif key == "hydrate_missing_duration" and value:
                data[f"{prefix}{key}"] = "on"
            elif key not in {"active", "hydrate_missing_duration", "remove"}:
                data[f"{prefix}{key}"] = value
    for index, route in enumerate(form["routes"]):
        prefix = f"route_{index}_"
        for key, value in route.items():
            if key == "enabled" and value:
                data[f"{prefix}{key}"] = "on"
            elif key != "remove":
                data[f"{prefix}{key}"] = value
    return data


def _edited_form(form: dict[str, Any]) -> dict[str, Any]:
    updated = dict(form)
    updated["display_name"] = "Smoke Edited Funnel"
    updated["status"] = "testing"
    updated["enabled"] = "on"
    updated["posting_enabled"] = ""
    updated["posting_mode"] = "manual_review"
    updated["ai_rule_profile"] = AI_PROFILE
    updated["pipeline_profile"] = CLONE_FUNNEL_ID
    updated["filename_prefix"] = CLONE_FUNNEL_ID
    updated["min_duration_minutes"] = "20"
    updated["max_duration_minutes"] = "180"
    updated["max_downloads_per_run"] = "1"
    updated["config_manager_funnel_id"] = AI_PROFILE
    updated["platforms"] = {platform: ("on" if platform == "youtube_shorts" else "") for platform in form["platforms"]}
    updated["target_platforms"] = {
        platform: ("on" if platform == "youtube_shorts" else "") for platform in form["target_platforms"]
    }
    updated["sources"] = [
        {
            "source_id": "smoke_source_channel",
            "label": "Smoke Source Channel",
            "url": "https://www.youtube.com/@SmokeTestChannel/videos",
            "source_type": "youtube_channel",
            "active": "on",
            "max_videos_per_source": "5",
            "hydrate_missing_duration": "on",
            "title_allowlist": "Smoke",
            "title_blocklist": "",
            "remove": "",
        }
    ]
    updated["routes"] = [
        {
            "channel_id": CHANNEL_ID,
            "platform": "youtube_shorts",
            "enabled": "on",
            "remove": "",
        }
    ]
    return updated


def _warning_codes(report) -> set[str]:
    return {issue.code for issue in report.warnings}


def _issue_codes(report) -> set[str]:
    return {issue.code for issue in report.errors}


def run_fixture_smoke(
    *,
    temp_root: Path | None = None,
    verbose: bool = False,
) -> tuple[SmokeReport, FixtureLayout | None]:
    from ops_ui.funnel_management.clone import clone_canonical_funnel
    from ops_ui.funnel_management.edit import edit_form_from_funnel, update_funnel_from_form
    from ops_ui.funnel_management.funnel_templates import build_funnel_from_template
    from ops_ui.funnel_management.registry import FunnelRegistry
    from ops_ui.funnel_management.schema import dump_canonical_funnel

    started = _utc_now()
    checks: list[CheckResult] = []
    steps: list[str] = []
    changed_files: list[str] = []

    cleanup_root = temp_root is None
    root = temp_root or Path(tempfile.mkdtemp(prefix="funnel-mgmt-smoke-"))
    layout = build_fixture_layout(root)
    layout.apply_env()
    guard_before = _snapshot(layout.guard_files)
    runtime_before = {
        "source": layout.source_funnels.read_text(encoding="utf-8"),
        "channels": layout.channels.read_text(encoding="utf-8"),
        "video_files": sorted(p.name for p in layout.video_dir.glob("*.json")),
    }

    registry = FunnelRegistry(layout.registry_dir)
    validator = layout.validator()
    synchronizer = layout.synchronizer()

    try:
        # Step 1 — template build
        draft = build_funnel_from_template(
            TEMPLATE_ID,
            funnel_id=CREATED_FUNNEL_ID,
            display_name="Smoke Created Funnel",
            environment="dev",
            description="Smoke test created funnel",
            category="general",
        )
        if draft.identity.status != "draft" or draft.identity.enabled or draft.distribution.posting_enabled:
            checks.append(CheckResult("step1_template_defaults", "FAIL", "Draft safety defaults wrong"))
        else:
            checks.append(CheckResult("step1_template_defaults", "PASS"))
        steps.append("template_build")

        # Step 2 — registry save
        registry.save_funnel(draft)
        listed = registry.list_funnels()
        if CREATED_FUNNEL_ID not in {item.identity.funnel_id for item in listed}:
            checks.append(CheckResult("step2_registry_save", "FAIL", "Created funnel not listed"))
        else:
            checks.append(CheckResult("step2_registry_save", "PASS"))
        if layout.source_funnels.read_text(encoding="utf-8") != runtime_before["source"]:
            checks.append(CheckResult("step2_no_runtime_write", "FAIL", "Runtime source changed on registry save"))
        else:
            checks.append(CheckResult("step2_no_runtime_write", "PASS"))
        steps.append("registry_save")

        created = registry.get_funnel(CREATED_FUNNEL_ID)
        source_before_clone = dump_canonical_funnel(created)

        # Step 3 — clone
        cloned = clone_canonical_funnel(
            created,
            new_funnel_id=CLONE_FUNNEL_ID,
            display_name="Smoke Clone Funnel",
            environment="dev",
        )
        registry.save_funnel(cloned)
        if cloned.identity.status != "draft" or cloned.identity.enabled or cloned.distribution.posting_enabled:
            checks.append(CheckResult("step3_clone_safety", "FAIL", "Clone safety overrides wrong"))
        else:
            checks.append(CheckResult("step3_clone_safety", "PASS"))
        if dump_canonical_funnel(registry.get_funnel(CREATED_FUNNEL_ID)) != source_before_clone:
            checks.append(CheckResult("step3_source_unchanged", "FAIL", "Source funnel mutated"))
        else:
            checks.append(CheckResult("step3_source_unchanged", "PASS"))
        steps.append("clone")

        # Step 4 — edit
        clone_before = registry.get_funnel(CLONE_FUNNEL_ID)
        created_at = clone_before.identity.created_at
        template_source = clone_before.identity.template_source
        updated_at_before = clone_before.identity.updated_at
        edit_form = _edited_form(edit_form_from_funnel(clone_before))
        edited, edit_errors = update_funnel_from_form(clone_before, _edit_post_data(edit_form))
        if edited is None or edit_errors:
            checks.append(CheckResult("step4_edit", "FAIL", "; ".join(edit_errors)))
        else:
            registry.save_funnel(edited, overwrite=True)
            saved = registry.get_funnel(CLONE_FUNNEL_ID)
            if saved.identity.created_at != created_at or saved.identity.template_source != template_source:
                checks.append(CheckResult("step4_immutable_fields", "FAIL", "Immutable fields changed"))
            elif (
                saved.identity.updated_at == updated_at_before
                and saved.identity.display_name == clone_before.identity.display_name
            ):
                checks.append(CheckResult("step4_updated_at", "FAIL", "Edit did not persist"))
            else:
                checks.append(CheckResult("step4_edit", "PASS"))
        steps.append("edit")

        edited_funnel = registry.get_funnel(CLONE_FUNNEL_ID)

        # Step 5 — validate before sync
        before_report = validator.validate_funnel(edited_funnel)
        before_warnings = _warning_codes(before_report)
        if not before_report.sync_ready:
            checks.append(CheckResult("step5_sync_ready", "FAIL", "Expected sync-ready before apply"))
        else:
            checks.append(CheckResult("step5_sync_ready", "PASS"))
        if before_report.processing_ready:
            checks.append(CheckResult("step5_not_processing_ready", "FAIL", "Should not be processing-ready before sync"))
        else:
            checks.append(CheckResult("step5_not_processing_ready", "PASS"))
        expected_pending = {"source_input_pending_sync", "video_config_pending_sync", "ai_registry_pending_sync"}
        if not expected_pending.issubset(before_warnings):
            checks.append(
                CheckResult(
                    "step5_pre_sync_validation",
                    "FAIL",
                    f"Expected pending-sync warnings {sorted(expected_pending)}, got {sorted(before_warnings)}",
                )
            )
        else:
            checks.append(CheckResult("step5_pre_sync_validation", "PASS"))
        steps.append("validate_before_sync")

        # Step 6 — dry-run plan
        source_before_plan = layout.source_funnels.read_text(encoding="utf-8")
        plan = synchronizer.build_plan(edited_funnel)
        if not plan.dry_run or not plan.ok:
            checks.append(CheckResult("step6_sync_plan", "FAIL", "; ".join(plan.errors)))
        else:
            targets = {change.target for change in plan.changes}
            expected = {
                "source_input_funnels",
                "video_funnel_json",
                "output_channels",
                "funnel_rule_registry",
                "config_manager_yaml",
            }
            if not expected.issubset(targets):
                checks.append(CheckResult("step6_sync_plan_targets", "FAIL", f"Missing targets: {expected - targets}"))
            else:
                checks.append(CheckResult("step6_sync_plan", "PASS"))
        if layout.source_funnels.read_text(encoding="utf-8") != source_before_plan:
            checks.append(CheckResult("step6_plan_no_write", "FAIL", "build_plan wrote files"))
        else:
            checks.append(CheckResult("step6_plan_no_write", "PASS"))
        steps.append("sync_plan")

        # Step 7 — apply sync
        apply_report = synchronizer.apply(edited_funnel, backup=True)
        if not apply_report.ok:
            checks.append(CheckResult("step7_apply", "FAIL", "; ".join(apply_report.errors)))
        else:
            checks.append(CheckResult("step7_apply", "PASS"))
        changed_files.extend(
            str(change.path)
            for change in apply_report.changes
            if change.changed and change.action in {"create", "update"}
        )
        backups = list(layout.source_funnels.parent.glob("funnels.json.bak.*"))
        if not backups:
            checks.append(CheckResult("step7_backup", "FAIL", "Expected backup file"))
        else:
            checks.append(CheckResult("step7_backup", "PASS"))

        source_after = json.loads(layout.source_funnels.read_text(encoding="utf-8"))
        source_ids = {item["funnel_id"] for item in source_after}
        if CLONE_FUNNEL_ID not in source_ids:
            checks.append(CheckResult("step7_source_added", "FAIL", "Clone funnel missing from source-input"))
        elif not {UNRELATED_SOURCE_ID, TEMPLATE_FUNNEL_ID, SOURCE_FUNNEL_ID}.issubset(source_ids):
            checks.append(CheckResult("step7_source_preserved", "FAIL", "Unrelated/template entries lost"))
        else:
            template_entry = next(item for item in source_after if item["funnel_id"] == TEMPLATE_FUNNEL_ID)
            if template_entry.get("active") is not False:
                checks.append(CheckResult("step7_template_preserved", "FAIL", "Template active flag changed"))
            else:
                checks.append(CheckResult("step7_source_projection", "PASS"))

        video_path = layout.video_dir / f"{CLONE_FUNNEL_ID}.json"
        if not video_path.is_file():
            checks.append(CheckResult("step7_video_created", "FAIL", "Video funnel JSON not created"))
        elif UNRELATED_VIDEO_ID not in {p.stem for p in layout.video_dir.glob("*.json")}:
            checks.append(CheckResult("step7_video_preserved", "FAIL", "Unrelated video file missing"))
        else:
            checks.append(CheckResult("step7_video_created", "PASS"))

        channels_after = json.loads(layout.channels.read_text(encoding="utf-8"))
        primary = next(ch for ch in channels_after["channels"] if ch["channel_id"] == CHANNEL_ID)
        other = next(ch for ch in channels_after["channels"] if ch["channel_id"] == OTHER_CHANNEL_ID)
        before_channels = json.loads(runtime_before["channels"])
        before_primary = next(ch for ch in before_channels["channels"] if ch["channel_id"] == CHANNEL_ID)
        before_other = next(ch for ch in before_channels["channels"] if ch["channel_id"] == OTHER_CHANNEL_ID)
        if CLONE_FUNNEL_ID not in primary["routing"]["accepted_funnel_ids"]:
            checks.append(CheckResult("step7_routing_patch", "FAIL", "accepted_funnel_ids not patched"))
        elif primary["credentials"] != before_primary["credentials"]:
            checks.append(CheckResult("step7_credentials_preserved", "FAIL", "Credentials changed"))
        elif primary["cadence"] != before_primary["cadence"]:
            checks.append(CheckResult("step7_cadence_preserved", "FAIL", "Cadence changed"))
        elif primary["metadata_style"] != before_primary["metadata_style"]:
            checks.append(CheckResult("step7_metadata_preserved", "FAIL", "Metadata changed"))
        elif other["routing"]["accepted_funnel_ids"] != before_other["routing"]["accepted_funnel_ids"]:
            checks.append(CheckResult("step7_other_routing_preserved", "FAIL", "Other channel routing changed"))
        else:
            checks.append(CheckResult("step7_output_projection", "PASS"))
        steps.append("sync_apply")

        # Step 8 — validate after sync
        after_report = validator.validate_funnel(edited_funnel)
        if not after_report.processing_ready:
            checks.append(
                CheckResult(
                    "step8_processing_ready",
                    "FAIL",
                    f"Expected processing-ready after sync; errors={[issue.code for issue in after_report.errors]}",
                )
            )
        else:
            checks.append(CheckResult("step8_processing_ready", "PASS"))
        if after_report.processing_state != "ready":
            checks.append(
                CheckResult(
                    "step8_post_sync_validation",
                    "FAIL",
                    f"processing_state={after_report.processing_state}",
                )
            )
        else:
            checks.append(CheckResult("step8_post_sync_validation", "PASS"))
        steps.append("validate_after_sync")

        # Step 9 — UI route smoke
        ui_checks = _run_ui_route_smoke(root, layout)
        checks.extend(ui_checks)
        steps.append("ui_routes")

        # Step 10 — forbidden writes
        guard_after = _snapshot(layout.guard_files)
        if guard_before != guard_after:
            checks.append(CheckResult("step10_guard_files", "FAIL", "Guard/decoy files changed"))
        else:
            checks.append(CheckResult("step10_guard_files", "PASS"))
        registry_doc = json.loads(layout.ai_registry.read_text(encoding="utf-8"))
        if registry_doc["aliases"].get(CLONE_FUNNEL_ID) != AI_PROFILE:
            checks.append(CheckResult("step10_registry_alias", "FAIL", "Clone alias not written to registry"))
        else:
            checks.append(CheckResult("step10_registry_alias", "PASS"))
        if not (layout.ai_prompts / "business_v1.txt").is_file():
            checks.append(CheckResult("step10_prompt_preserved", "FAIL", "Builtin prompt file missing"))
        else:
            checks.append(CheckResult("step10_prompt_preserved", "PASS"))
        steps.append("forbidden_writes")

    except Exception as exc:  # pragma: no cover - surfaced as FAIL check
        checks.append(CheckResult("fixture_smoke_exception", "FAIL", f"{exc.__class__.__name__}: {exc}"))

    report = SmokeReport(
        mode="fixture",
        temp_root=str(root),
        started_at=started,
        finished_at=_utc_now(),
        steps_completed=steps,
        files_changed=sorted(set(changed_files)),
        checks=[asdict(item) for item in checks],
        overall=overall_from_checks(checks),
    )

    if cleanup_root and report.overall != "FAIL":
        # Keep temp dir on failure for inspection; caller can pass --keep-temp
        pass

    return report, layout if not cleanup_root or verbose else layout


def _run_ui_route_smoke(root: Path, layout: FixtureLayout) -> list[CheckResult]:
    from ops_ui.app import create_app
    from ops_ui.config import Settings

    layout.apply_env()
    settings = Settings(
        host="127.0.0.1",
        port=5070,
        data_dir=root / "ops-data",
        control_db_path=root / "ops-data" / "ops.sqlite3",
        controls_file=root / "ops-data" / "controls.json",
        service_timeout_sec=0.01,
        journal_lines=1,
        funnel_run_timeout_sec=1.0,
        stuck_running_sec=7200.0,
        stuck_queued_sec=1800.0,
        stuck_uploading_sec=1800.0,
        auth_enabled=True,
        operator_password="smoke-pass",
        secret_key="smoke-funnel-secret",
        environment="dev",
        services=(),
    )
    (root / "ops-data").mkdir(parents=True, exist_ok=True)
    client = create_app(settings).test_client()

    login = client.get("/login")
    token = login.get_data(as_text=True).split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    client.post(
        "/login",
        data={"password": "smoke-pass", "csrf_token": token, "next": f"/funnels/{CLONE_FUNNEL_ID}"},
    )

    checks: list[CheckResult] = []
    list_page = client.get("/funnels")
    if list_page.status_code != 200 or CLONE_FUNNEL_ID.encode() not in list_page.data:
        checks.append(CheckResult("ui_funnels_list", "FAIL", f"status={list_page.status_code}"))
    else:
        checks.append(CheckResult("ui_funnels_list", "PASS"))

    detail = client.get(f"/funnels/{CLONE_FUNNEL_ID}")
    if detail.status_code != 200 or b"Smoke Edited Funnel" not in detail.data:
        checks.append(CheckResult("ui_funnel_detail", "FAIL", f"status={detail.status_code}"))
    else:
        checks.append(CheckResult("ui_funnel_detail", "PASS"))

    edit_page = client.get(f"/funnels/{CLONE_FUNNEL_ID}/edit")
    if edit_page.status_code != 200:
        checks.append(CheckResult("ui_funnel_edit", "FAIL", f"status={edit_page.status_code}"))
    else:
        checks.append(CheckResult("ui_funnel_edit", "PASS"))

    source_before = layout.source_funnels.read_text(encoding="utf-8")
    sync_get = client.get(f"/funnels/{CLONE_FUNNEL_ID}/sync")
    if sync_get.status_code != 200 or b"Sync Config" not in sync_get.data:
        checks.append(CheckResult("ui_sync_preview", "FAIL", f"status={sync_get.status_code}"))
    elif layout.source_funnels.read_text(encoding="utf-8") != source_before:
        checks.append(CheckResult("ui_sync_get_no_write", "FAIL", "GET sync wrote files"))
    else:
        checks.append(CheckResult("ui_sync_preview", "PASS"))
        checks.append(CheckResult("ui_sync_get_no_write", "PASS"))

    sync_csrf = sync_get.get_data(as_text=True).split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    sync_post = client.post(
        f"/funnels/{CLONE_FUNNEL_ID}/sync",
        data={"environment": "dev", "csrf_token": sync_csrf},
    )
    if sync_post.status_code != 200 or b"runtime config files will be written" not in sync_post.data:
        checks.append(CheckResult("ui_sync_post_requires_confirm", "FAIL", f"status={sync_post.status_code}"))
    else:
        checks.append(CheckResult("ui_sync_post_requires_confirm", "PASS"))

    bad_csrf = client.post(
        f"/funnels/{CLONE_FUNNEL_ID}/sync",
        data={"environment": "dev", "confirm_understand": "on", "csrf_token": "bad"},
    )
    if bad_csrf.status_code != 200 or b"Invalid security token" not in bad_csrf.data:
        checks.append(CheckResult("ui_sync_csrf", "FAIL", "CSRF not enforced"))
    else:
        checks.append(CheckResult("ui_sync_csrf", "PASS"))

    return checks


def run_live_read_only(*, verbose: bool = False) -> SmokeReport:
    from ops_ui.funnel_management.registry import FunnelRegistry, default_registry_dir
    from ops_ui.funnel_management.sync_workflow import resolve_sync_paths

    started = _utc_now()
    checks: list[CheckResult] = []
    steps = ["live_read_only_paths"]

    try:
        for environment in ("dev", "prod"):
            paths = resolve_sync_paths(environment)
            detail = (
                f"{environment}: source={paths.source_funnels_path}, "
                f"video={paths.video_funnels_dir}, channels={paths.output_channels_path}"
            )
            if environment == "prod" and paths.path_kind == "unconfigured":
                checks.append(CheckResult(f"live_paths_{environment}", "WARN", detail))
            else:
                checks.append(CheckResult(f"live_paths_{environment}", "PASS", detail))

        registry_dir = default_registry_dir()
        registry = FunnelRegistry(registry_dir)
        funnels = registry.list_funnels()
        if not funnels:
            checks.append(CheckResult("live_registry", "WARN", f"No funnels in {registry_dir}"))
        else:
            checks.append(CheckResult("live_registry", "PASS", f"{len(funnels)} funnel(s) in registry"))
            sample = funnels[0].identity.funnel_id
            funnel = registry.get_funnel(sample)
            env_paths = resolve_sync_paths("dev")
            from ops_ui.funnel_management.sync import FunnelSynchronizer

            plan = FunnelSynchronizer(env_paths.to_target_paths()).build_plan(funnel)
            if plan.dry_run and plan.ok:
                checks.append(CheckResult("live_sync_preview", "PASS", f"Preview ok for {sample}"))
            else:
                checks.append(
                    CheckResult(
                        "live_sync_preview",
                        "WARN",
                        f"Preview blocked for {sample}: {'; '.join(plan.errors[:3])}",
                    )
                )
            steps.append("live_sync_preview")
    except Exception as exc:
        checks.append(CheckResult("live_read_only_exception", "FAIL", str(exc)))

    return SmokeReport(
        mode="live-read-only",
        temp_root="",
        started_at=started,
        finished_at=_utc_now(),
        steps_completed=steps,
        checks=[asdict(item) for item in checks],
        overall=overall_from_checks(checks),
    )


def render_report(report: SmokeReport) -> str:
    lines = [
        "Funnel Management Smoke Report",
        "",
        f"Mode:      {report.mode}",
        f"Temp root: {report.temp_root or '(none)'}",
        f"Started:   {report.started_at}",
        f"Finished:  {report.finished_at}",
        f"Overall:   {report.overall}",
        "",
        "Steps completed:",
    ]
    for step in report.steps_completed:
        lines.append(f"  - {step}")
    if report.files_changed:
        lines.extend(["", "Files changed:"])
        for path in report.files_changed:
            lines.append(f"  - {path}")
    lines.extend(["", "Checks:"])
    for item in report.checks:
        detail = f" — {item['detail']}" if item.get("detail") else ""
        lines.append(f"  [{item['outcome']}] {item['name']}{detail}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Funnel Management MK1 smoke test")
    parser.add_argument(
        "--live-read-only",
        action="store_true",
        help="Inspect configured paths and run sync preview only (no writes)",
    )
    parser.add_argument("--keep-temp", action="store_true", help="Print temp root and do not delete on success")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    parser.add_argument("--no-report", action="store_true", help="Suppress human-readable report")
    args = parser.parse_args(argv)

    if args.live_read_only:
        report = run_live_read_only(verbose=args.verbose)
    else:
        report, _layout = run_fixture_smoke(verbose=args.verbose or args.keep_temp)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
    elif not args.no_report:
        print(render_report(report))
        if args.keep_temp and report.temp_root:
            print(f"\nTemp root preserved: {report.temp_root}")

    if report.overall == "FAIL":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
