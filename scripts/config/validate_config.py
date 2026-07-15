#!/usr/bin/env python3
"""
scripts/config/validate_config.py

Config schema validator for the production infrastructure configuration layer.

Usage:
    python scripts/config/validate_config.py
    python scripts/config/validate_config.py --config-root /path/to/config

Exit codes:
    0  All config files are valid.
    1  One or more validation errors found.
    2  Unexpected error (parse / IO failure outside normal validation).

This script validates structure and safety only.
It does not implement ConfigManager, upload behaviour, kill switches,
storage deletion, or any runtime behaviour.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    print(
        "ERROR: PyYAML is not installed in this environment.\n"
        "Run with: video-automation/.venv/bin/python scripts/config/validate_config.py",
        file=sys.stderr,
    )
    sys.exit(2)

# ---------------------------------------------------------------------------
# Required files
# ---------------------------------------------------------------------------

REQUIRED_FILES = [
    "defaults/default.yaml",
    "environments/dev.yaml",
    "environments/prod.yaml",
    "system/system.yaml",
    "funnels/business.yaml",
    "platforms/youtube.yaml",
    "platforms/tiktok.yaml",
    "platforms/instagram.yaml",
    "platforms/facebook.yaml",
    "platforms/x.yaml",
    "presets/balanced.yaml",
    "presets/growth.yaml",
    "presets/maximum_quality.yaml",
]

# ---------------------------------------------------------------------------
# Secret patterns
# Secret scanning rejects YAML files that contain real-looking secret values.
# We check key names AND non-null/non-empty string values that look like secrets.
# ---------------------------------------------------------------------------

_SECRET_KEY_PATTERN = re.compile(
    r"\b(password|secret|token|api_key|access_key|private_key|cookie|session|bearer)\b",
    re.IGNORECASE,
)

# Pattern for values that look like real credentials: long alphanumeric strings,
# JWT-like strings, or known API key prefixes.
_SECRET_VALUE_PATTERN = re.compile(
    r"(^sk-[A-Za-z0-9]{20,}|^ya29\.|^ey[A-Za-z0-9]{20,}|^ghp_|^xoxb-|^Bearer\s)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> tuple[Any, str | None]:
    """Return (parsed_data, error_string). error_string is None on success."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data, None
    except yaml.YAMLError as exc:
        return None, f"{path}: invalid YAML — {exc}"
    except OSError as exc:
        return None, f"{path}: cannot read — {exc}"


def _get(data: dict, *keys: str) -> Any:
    """Safely traverse nested dict. Returns None if any key is missing."""
    node = data
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def _require(data: dict, path_str: str, errors: list[str], file_label: str) -> Any:
    """
    Require a dot-notation key to be present (not None).
    Returns the value, or None if missing (error already appended).
    """
    keys = path_str.split(".")
    value = _get(data, *keys)
    if value is None:
        errors.append(f"{file_label}: missing required field '{path_str}'")
    return value


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _is_list(v: Any) -> bool:
    return isinstance(v, list)


def _scan_secrets(data: Any, path: str, errors: list[str], file_label: str) -> None:
    """
    Recursively scan a parsed YAML structure for secret-looking keys or values.
    Fails on:
      - A key name matching the secret key pattern whose value is a non-empty string.
      - A string value matching the known-credential pattern (regardless of key).
    """
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(k, str) and _SECRET_KEY_PATTERN.search(k):
                if isinstance(v, str) and v.strip():
                    errors.append(
                        f"{file_label}: key '{path}.{k}' looks like a secret field "
                        f"with a non-empty value — secrets must not be committed to YAML config"
                    )
            if isinstance(v, str) and _SECRET_VALUE_PATTERN.search(v):
                errors.append(
                    f"{file_label}: value at '{path}.{k}' looks like a real credential — "
                    f"secrets must not be committed to YAML config"
                )
            _scan_secrets(v, f"{path}.{k}", errors, file_label)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            _scan_secrets(item, f"{path}[{i}]", errors, file_label)


# ---------------------------------------------------------------------------
# Allowed top-level keys per file type
# Reject unknown top-level sections to catch mistakes early.
# ---------------------------------------------------------------------------

_ALLOWED_DEFAULTS_KEYS = {"version", "selection", "posting", "uploading", "logging", "captions"}
_ALLOWED_ENV_KEYS = {"environment", "paths", "uploading", "runtime", "storage"}
_ALLOWED_SYSTEM_KEYS = {"system", "ai", "storage", "services"}

# Retention day-count fields (policy only — no engine consumes these yet).
_RETENTION_DAY_KEYS = (
    "source_videos_days",
    "transcripts_days",
    "raw_candidate_pools_days",
    "processing_reports_days",
    "selection_results_days",
    "post_processing_reports_days",
    "clip_metadata_days",
    "intermediate_renders_days",
    "temporary_files_days",
    "logs_days",
    "run_records_days",
    "config_snapshots_days",
    "database_backups_days",
    "successful_job_artifacts_days",
    "failed_job_artifacts_days",
)

_DISK_PRESSURE_KEYS = (
    "warning_percent",
    "urgent_percent",
    "critical_percent",
    "reject_new_jobs_percent",
)

_ALLOWED_RETENTION_KEYS = frozenset({"enabled", *_RETENTION_DAY_KEYS})
_SCHEDULE_MODES = frozenset({"disabled", "dry_run", "apply"})
_SCHEDULE_FREQUENCIES = frozenset({"daily", "weekly"})
_SCHEDULE_KEYS = ("enabled", "mode", "frequency")
_ALLOWED_SCHEDULE_KEYS = frozenset(_SCHEDULE_KEYS)
_LOG_ROTATION_KEYS = ("enabled", "max_bytes", "backup_count", "compress", "journal")
_ALLOWED_LOG_ROTATION_KEYS = frozenset(_LOG_ROTATION_KEYS)
_JOURNAL_KEYS = ("system_max_use", "runtime_max_use", "max_file_sec")
_ALLOWED_JOURNAL_KEYS = frozenset(_JOURNAL_KEYS)
_DATABASE_BACKUP_KEYS = ("enabled", "verify_integrity", "location")
_ALLOWED_DATABASE_BACKUP_KEYS = frozenset(_DATABASE_BACKUP_KEYS)
_ALLOWED_DELETE_ROOT_KEYS = frozenset({"jobs", "logs", "reports", "data", "backups"})
_ALLOWED_STORAGE_KEYS = frozenset(
    {
        "retention",
        "disk_pressure",
        "schedule",
        "log_rotation",
        "database_backup",
        "allowed_delete_roots",
        "protected_artifact_types",
        "auto_delete_final_clips_prod",
        "allow_final_clip_auto_deletion_opt_in",
    }
)
_REQUIRED_PROTECTED_TYPES = frozenset({"final_clip", "database"})

_ALLOWED_FUNNEL_KEYS = {"funnel", "sources", "selection", "platforms"}
_ALLOWED_PLATFORM_KEYS = {"platform", "uploading", "format", "accounts", "posting"}
_ALLOWED_PRESET_KEYS = {"preset", "selection", "posting", "post_processing"}


def _check_unknown_keys(
    data: dict, allowed: set[str], errors: list[str], file_label: str
) -> None:
    if not isinstance(data, dict):
        return
    for k in data:
        if k not in allowed:
            errors.append(
                f"{file_label}: unknown top-level key '{k}' — "
                f"allowed keys are: {sorted(allowed)}"
            )


# ---------------------------------------------------------------------------
# Per-type validators
# ---------------------------------------------------------------------------


def _validate_defaults(data: dict, errors: list[str], label: str) -> None:
    _check_unknown_keys(data, _ALLOWED_DEFAULTS_KEYS, errors, label)

    version = _require(data, "version", errors, label)
    if version is not None and not _is_int(version):
        errors.append(f"{label}: 'version' must be an integer, got {version!r}")

    mode = _require(data, "selection.mode", errors, label)
    if mode is not None and not _is_nonempty_str(mode):
        errors.append(f"{label}: 'selection.mode' must be a non-empty string")

    max_clips = _require(data, "selection.max_clips", errors, label)
    if max_clips is not None and (not _is_int(max_clips) or max_clips <= 0):
        errors.append(f"{label}: 'selection.max_clips' must be an integer > 0, got {max_clips!r}")

    pot = _require(data, "selection.min_overall_potential", errors, label)
    if pot is not None and (not _is_number(pot) or pot < 0):
        errors.append(
            f"{label}: 'selection.min_overall_potential' must be a number >= 0, got {pot!r}"
        )

    conf = _require(data, "selection.min_confidence", errors, label)
    if conf is not None and (not _is_number(conf) or not (0 <= conf <= 1)):
        errors.append(
            f"{label}: 'selection.min_confidence' must be a number between 0 and 1, got {conf!r}"
        )

    ratio = _require(data, "selection.exploration_ratio", errors, label)
    if ratio is not None and (not _is_number(ratio) or not (0 <= ratio <= 1)):
        errors.append(
            f"{label}: 'selection.exploration_ratio' must be a number between 0 and 1, got {ratio!r}"
        )

    uploads_per_day = _require(data, "posting.uploads_per_day", errors, label)
    if uploads_per_day is not None and (not _is_int(uploads_per_day) or uploads_per_day < 0):
        errors.append(
            f"{label}: 'posting.uploads_per_day' must be an integer >= 0, got {uploads_per_day!r}"
        )

    uploading_enabled = _require(data, "uploading.enabled", errors, label)
    if uploading_enabled is not None and not _is_bool(uploading_enabled):
        errors.append(f"{label}: 'uploading.enabled' must be a boolean, got {uploading_enabled!r}")

    log_level = _require(data, "logging.level", errors, label)
    if log_level is not None and not _is_nonempty_str(log_level):
        errors.append(f"{label}: 'logging.level' must be a non-empty string")

    captions = _get(data, "captions")
    if captions is not None:
        _validate_captions(captions, errors, label)


def _validate_captions(captions: Any, errors: list[str], label: str) -> None:
    if not isinstance(captions, dict):
        errors.append(f"{label}: 'captions' must be a mapping, got {type(captions).__name__}")
        return

    _check_unknown_keys(captions, {"safe_zone", "layout"}, errors, f"{label}.captions")

    safe_zone = captions.get("safe_zone")
    if safe_zone is not None:
        if not isinstance(safe_zone, dict):
            errors.append(
                f"{label}: 'captions.safe_zone' must be a mapping, got {type(safe_zone).__name__}"
            )
        else:
            _check_unknown_keys(
                safe_zone,
                {"top_px", "bottom_px", "left_px", "right_px"},
                errors,
                f"{label}.captions.safe_zone",
            )
            for key in ("top_px", "bottom_px", "left_px", "right_px"):
                value = safe_zone.get(key)
                if value is not None and (not _is_int(value) or value <= 0):
                    errors.append(
                        f"{label}: 'captions.safe_zone.{key}' must be an integer > 0, got {value!r}"
                    )

    layout = captions.get("layout")
    if layout is not None:
        if not isinstance(layout, dict):
            errors.append(
                f"{label}: 'captions.layout' must be a mapping, got {type(layout).__name__}"
            )
        else:
            _check_unknown_keys(
                layout,
                {
                    "font_family",
                    "font_size",
                    "max_lines",
                    "max_chars_per_line",
                    "max_chars_per_caption",
                },
                errors,
                f"{label}.captions.layout",
            )
            font_size = layout.get("font_size")
            if font_size is not None and (not _is_int(font_size) or font_size <= 0):
                errors.append(
                    f"{label}: 'captions.layout.font_size' must be an integer > 0, got {font_size!r}"
                )
            max_lines = layout.get("max_lines")
            if max_lines is not None and (not _is_int(max_lines) or max_lines <= 0):
                errors.append(
                    f"{label}: 'captions.layout.max_lines' must be an integer > 0, got {max_lines!r}"
                )
            for key in ("max_chars_per_line", "max_chars_per_caption"):
                value = layout.get(key)
                if value is not None and (not _is_int(value) or value <= 0):
                    errors.append(
                        f"{label}: 'captions.layout.{key}' must be an integer > 0, got {value!r}"
                    )
            font_family = layout.get("font_family")
            if font_family is not None and not _is_nonempty_str(font_family):
                errors.append(
                    f"{label}: 'captions.layout.font_family' must be a non-empty string"
                )


def _validate_environment(
    data: dict, filename: str, errors: list[str], label: str
) -> None:
    _check_unknown_keys(data, _ALLOWED_ENV_KEYS, errors, label)

    is_dev = filename == "dev.yaml"
    is_prod = filename == "prod.yaml"

    env_name = _require(data, "environment.name", errors, label)
    if env_name is not None:
        expected = "development" if is_dev else "production"
        if env_name != expected:
            errors.append(
                f"{label}: 'environment.name' must be '{expected}' for {filename}, got {env_name!r}"
            )

    required_paths = [
        "paths.data_root",
        "paths.jobs_root",
        "paths.outputs_root",
        "paths.logs_root",
        "paths.reports_root",
        "paths.database_path",
    ]
    path_values: dict[str, str] = {}
    for path_key in required_paths:
        v = _require(data, path_key, errors, label)
        if v is not None:
            if not _is_nonempty_str(v):
                errors.append(f"{label}: '{path_key}' must be a non-empty string, got {v!r}")
            else:
                path_values[path_key] = v

    # database_path must end with .db
    db = path_values.get("paths.database_path")
    if db and not db.endswith(".db"):
        errors.append(f"{label}: 'paths.database_path' must end with '.db', got {db!r}")

    # Path scoping: dev must use dev-scoped paths, prod must use prod-scoped paths.
    # We check that each path contains the correct scope token and does NOT
    # contain the opposite scope token (preventing cross-contamination).
    scope = "dev" if is_dev else "prod"
    opposite = "prod" if is_dev else "dev"
    for path_key, path_val in path_values.items():
        parts = Path(path_val).parts
        has_scope = any(scope in p for p in parts)
        has_opposite = any(
            # Match whole path component to avoid 'dev' matching inside 'development'
            p == opposite or p.startswith(f"{opposite}/") or p.startswith(f"{opposite}.")
            for p in parts
        )
        if not has_scope:
            errors.append(
                f"{label}: '{path_key}' must be scoped to '{scope}/', got {path_val!r}"
            )
        if has_opposite:
            errors.append(
                f"{label}: '{path_key}' for {filename} must not point into '{opposite}/', "
                f"got {path_val!r}"
            )

    uploading_enabled = _require(data, "uploading.enabled", errors, label)
    if uploading_enabled is not None and not _is_bool(uploading_enabled):
        errors.append(f"{label}: 'uploading.enabled' must be a boolean")

    # Dev must have uploads disabled
    if is_dev and uploading_enabled is True:
        errors.append(
            f"{label}: 'uploading.enabled' must be false in dev.yaml — "
            f"dev must never post real content"
        )

    req_secrets = _require(data, "runtime.require_production_secrets", errors, label)
    if req_secrets is not None and not _is_bool(req_secrets):
        errors.append(f"{label}: 'runtime.require_production_secrets' must be a boolean")

    if is_dev and req_secrets is True:
        errors.append(
            f"{label}: 'runtime.require_production_secrets' must be false in dev.yaml"
        )
    if is_prod and req_secrets is False:
        errors.append(
            f"{label}: 'runtime.require_production_secrets' must be true in prod.yaml"
        )

    # Optional storage overrides (merged after system.yaml). Validate present keys only.
    storage = data.get("storage")
    if storage is not None:
        validate_storage_policy(storage, errors, label, require_complete=False)


def validate_storage_policy(
    storage: Any,
    errors: list[str],
    label: str,
    *,
    require_complete: bool = True,
) -> None:
    """
    Validate storage retention policy configuration.

    Policy only — does not scan disks, classify artifacts, or delete anything.
    When require_complete is True (system.yaml / merged config), all fields are
    required. When False (environment overrides), only present fields are checked.
    """
    if not isinstance(storage, dict):
        errors.append(f"{label}: 'storage' must be a mapping, got {type(storage).__name__}")
        return

    for key in storage:
        if key not in _ALLOWED_STORAGE_KEYS:
            errors.append(
                f"{label}: unknown storage key '{key}' — "
                f"allowed keys are: {sorted(_ALLOWED_STORAGE_KEYS)}"
            )

    retention = storage.get("retention")
    if retention is None:
        if require_complete:
            errors.append(f"{label}: missing required section 'storage.retention'")
    elif not isinstance(retention, dict):
        errors.append(
            f"{label}: 'storage.retention' must be a mapping, got {type(retention).__name__}"
        )
    else:
        for key in retention:
            if key not in _ALLOWED_RETENTION_KEYS:
                errors.append(
                    f"{label}: unknown retention key 'storage.retention.{key}' — "
                    f"allowed keys are: {sorted(_ALLOWED_RETENTION_KEYS)}"
                )

        enabled = retention.get("enabled")
        if "enabled" in retention:
            if not _is_bool(enabled):
                errors.append(
                    f"{label}: 'storage.retention.enabled' must be a boolean, got {enabled!r}"
                )
        elif require_complete:
            errors.append(f"{label}: missing required field 'storage.retention.enabled'")
            enabled = None

        for key in _RETENTION_DAY_KEYS:
            if key not in retention:
                if require_complete:
                    errors.append(f"{label}: missing required field 'storage.retention.{key}'")
                continue
            v = retention[key]
            if not _is_int(v) or v < 0:
                errors.append(
                    f"{label}: 'storage.retention.{key}' must be an integer >= 0, got {v!r}"
                )

        successful = retention.get("successful_job_artifacts_days")
        failed = retention.get("failed_job_artifacts_days")
        if (
            _is_int(successful)
            and successful >= 0
            and _is_int(failed)
            and failed >= 0
            and failed <= successful
        ):
            errors.append(
                f"{label}: 'storage.retention.failed_job_artifacts_days' ({failed}) "
                f"must be strictly greater than "
                f"'storage.retention.successful_job_artifacts_days' ({successful}) — "
                f"failed-job artifacts must be retained longer than successful-job artifacts"
            )

        # allowed_delete_roots emptiness is checked below when enabled is true.

    dp = storage.get("disk_pressure")
    if dp is None:
        if require_complete:
            errors.append(f"{label}: missing required section 'storage.disk_pressure'")
    elif not isinstance(dp, dict):
        errors.append(
            f"{label}: 'storage.disk_pressure' must be a mapping, got {type(dp).__name__}"
        )
    else:
        for key in dp:
            if key not in _DISK_PRESSURE_KEYS:
                errors.append(
                    f"{label}: unknown disk_pressure key 'storage.disk_pressure.{key}'"
                )

        present_keys = [k for k in _DISK_PRESSURE_KEYS if k in dp]
        # Complete policies and partial overrides that set any threshold must
        # supply all four values so ordering can be checked.
        need_all = require_complete or bool(present_keys)
        dp_values: list[float] = []
        for key in _DISK_PRESSURE_KEYS:
            if key not in dp:
                if need_all:
                    errors.append(
                        f"{label}: missing 'storage.disk_pressure.{key}'"
                        + (
                            " (all disk_pressure thresholds must be set together)"
                            if not require_complete
                            else ""
                        )
                    )
                continue
            v = dp[key]
            if not _is_number(v) or not (0 < v <= 100):
                errors.append(
                    f"{label}: 'storage.disk_pressure.{key}' must be a number > 0 and <= 100, "
                    f"got {v!r}"
                )
            else:
                dp_values.append(float(v))

        if len(dp_values) == len(_DISK_PRESSURE_KEYS):
            warning, urgent, critical, reject = dp_values
            if not (warning < urgent < critical < reject):
                errors.append(
                    f"{label}: disk_pressure thresholds must be strictly ascending: "
                    f"warning ({warning}) < urgent ({urgent}) < critical ({critical}) "
                    f"< reject_new_jobs ({reject})"
                )

    schedule = storage.get("schedule")
    if schedule is None:
        if require_complete:
            errors.append(f"{label}: missing required section 'storage.schedule'")
    elif not isinstance(schedule, dict):
        errors.append(
            f"{label}: 'storage.schedule' must be a mapping, got {type(schedule).__name__}"
        )
    else:
        for key in schedule:
            if key not in _ALLOWED_SCHEDULE_KEYS:
                errors.append(
                    f"{label}: unknown schedule key 'storage.schedule.{key}' — "
                    f"allowed keys are: {sorted(_ALLOWED_SCHEDULE_KEYS)}"
                )

        present_schedule_keys = [k for k in _SCHEDULE_KEYS if k in schedule]
        need_all_schedule = require_complete or bool(present_schedule_keys)
        for key in _SCHEDULE_KEYS:
            if key not in schedule:
                if need_all_schedule:
                    errors.append(
                        f"{label}: missing 'storage.schedule.{key}'"
                        + (
                            " (all schedule fields must be set together)"
                            if not require_complete
                            else ""
                        )
                    )
                continue
            value = schedule[key]
            if key == "enabled":
                if not _is_bool(value):
                    errors.append(
                        f"{label}: 'storage.schedule.enabled' must be a boolean, got {value!r}"
                    )
            elif key == "mode":
                if not isinstance(value, str) or value not in _SCHEDULE_MODES:
                    errors.append(
                        f"{label}: 'storage.schedule.mode' must be one of "
                        f"{sorted(_SCHEDULE_MODES)}, got {value!r}"
                    )
            elif key == "frequency":
                if not isinstance(value, str) or value not in _SCHEDULE_FREQUENCIES:
                    errors.append(
                        f"{label}: 'storage.schedule.frequency' must be one of "
                        f"{sorted(_SCHEDULE_FREQUENCIES)}, got {value!r}"
                    )

        # Scheduled apply is an explicit opt-in; mode=apply with retention
        # disabled is allowed in config but refused at runtime with a record.

    log_rotation = storage.get("log_rotation")
    if log_rotation is None:
        if require_complete:
            errors.append(f"{label}: missing required section 'storage.log_rotation'")
    elif not isinstance(log_rotation, dict):
        errors.append(
            f"{label}: 'storage.log_rotation' must be a mapping, "
            f"got {type(log_rotation).__name__}"
        )
    else:
        for key in log_rotation:
            if key not in _ALLOWED_LOG_ROTATION_KEYS:
                errors.append(
                    f"{label}: unknown log_rotation key 'storage.log_rotation.{key}' — "
                    f"allowed keys are: {sorted(_ALLOWED_LOG_ROTATION_KEYS)}"
                )

        present_lr = [k for k in _LOG_ROTATION_KEYS if k in log_rotation]
        need_all_lr = require_complete or bool(present_lr)
        for key in ("enabled", "max_bytes", "backup_count", "compress"):
            if key not in log_rotation:
                if need_all_lr:
                    errors.append(
                        f"{label}: missing 'storage.log_rotation.{key}'"
                        + (
                            " (all log_rotation fields must be set together)"
                            if not require_complete
                            else ""
                        )
                    )
                continue
            value = log_rotation[key]
            if key in {"enabled", "compress"}:
                if not _is_bool(value):
                    errors.append(
                        f"{label}: 'storage.log_rotation.{key}' must be a boolean, "
                        f"got {value!r}"
                    )
            elif key == "max_bytes":
                if not _is_int(value) or value < 1:
                    errors.append(
                        f"{label}: 'storage.log_rotation.max_bytes' must be an integer "
                        f">= 1, got {value!r}"
                    )
            elif key == "backup_count":
                if not _is_int(value) or value < 1:
                    errors.append(
                        f"{label}: 'storage.log_rotation.backup_count' must be an "
                        f"integer >= 1, got {value!r}"
                    )

        journal = log_rotation.get("journal")
        if journal is None:
            if need_all_lr:
                errors.append(f"{label}: missing 'storage.log_rotation.journal'")
        elif not isinstance(journal, dict):
            errors.append(
                f"{label}: 'storage.log_rotation.journal' must be a mapping, "
                f"got {type(journal).__name__}"
            )
        else:
            for key in journal:
                if key not in _ALLOWED_JOURNAL_KEYS:
                    errors.append(
                        f"{label}: unknown journal key "
                        f"'storage.log_rotation.journal.{key}'"
                    )
            for key in _JOURNAL_KEYS:
                if key not in journal:
                    if need_all_lr:
                        errors.append(
                            f"{label}: missing 'storage.log_rotation.journal.{key}'"
                        )
                    continue
                value = journal[key]
                if not _is_nonempty_str(value):
                    errors.append(
                        f"{label}: 'storage.log_rotation.journal.{key}' must be a "
                        f"non-empty string, got {value!r}"
                    )

    database_backup = storage.get("database_backup")
    if database_backup is None:
        if require_complete:
            errors.append(f"{label}: missing required section 'storage.database_backup'")
    elif not isinstance(database_backup, dict):
        errors.append(
            f"{label}: 'storage.database_backup' must be a mapping, "
            f"got {type(database_backup).__name__}"
        )
    else:
        for key in database_backup:
            if key not in _ALLOWED_DATABASE_BACKUP_KEYS:
                errors.append(
                    f"{label}: unknown database_backup key "
                    f"'storage.database_backup.{key}' — "
                    f"allowed keys are: {sorted(_ALLOWED_DATABASE_BACKUP_KEYS)}"
                )
        present_db = [k for k in _DATABASE_BACKUP_KEYS if k in database_backup]
        need_all_db = require_complete or bool(present_db)
        for key in _DATABASE_BACKUP_KEYS:
            if key not in database_backup:
                if need_all_db:
                    errors.append(
                        f"{label}: missing 'storage.database_backup.{key}'"
                        + (
                            " (all database_backup fields must be set together)"
                            if not require_complete
                            else ""
                        )
                    )
                continue
            value = database_backup[key]
            if key in {"enabled", "verify_integrity"}:
                if not _is_bool(value):
                    errors.append(
                        f"{label}: 'storage.database_backup.{key}' must be a boolean, "
                        f"got {value!r}"
                    )
            elif key == "location":
                if not _is_nonempty_str(value):
                    errors.append(
                        f"{label}: 'storage.database_backup.location' must be a "
                        f"non-empty string, got {value!r}"
                    )

    roots = storage.get("allowed_delete_roots")
    if roots is None:
        if require_complete:
            errors.append(f"{label}: missing required field 'storage.allowed_delete_roots'")
    elif not _is_list(roots):
        errors.append(
            f"{label}: 'storage.allowed_delete_roots' must be a list, got {type(roots).__name__}"
        )
    else:
        for i, root in enumerate(roots):
            if not _is_nonempty_str(root):
                errors.append(
                    f"{label}: 'storage.allowed_delete_roots[{i}]' must be a non-empty string, "
                    f"got {root!r}"
                )
            elif root not in _ALLOWED_DELETE_ROOT_KEYS:
                errors.append(
                    f"{label}: unknown allowed_delete_roots entry {root!r} — "
                    f"allowed keys are: {sorted(_ALLOWED_DELETE_ROOT_KEYS)}"
                )

    retention_enabled = False
    if isinstance(retention, dict) and retention.get("enabled") is True:
        retention_enabled = True
    if retention_enabled:
        if not _is_list(roots) or len(roots) == 0:
            errors.append(
                f"{label}: 'storage.allowed_delete_roots' must be a non-empty list when "
                f"'storage.retention.enabled' is true"
            )

    protected = storage.get("protected_artifact_types")
    if protected is None:
        if require_complete:
            errors.append(
                f"{label}: missing required field 'storage.protected_artifact_types'"
            )
    elif not _is_list(protected):
        errors.append(
            f"{label}: 'storage.protected_artifact_types' must be a list, "
            f"got {type(protected).__name__}"
        )
    else:
        for i, item in enumerate(protected):
            if not _is_nonempty_str(item):
                errors.append(
                    f"{label}: 'storage.protected_artifact_types[{i}]' must be a non-empty string, "
                    f"got {item!r}"
                )

    auto_delete = storage.get("auto_delete_final_clips_prod")
    opt_in = storage.get("allow_final_clip_auto_deletion_opt_in")
    if "auto_delete_final_clips_prod" in storage:
        if not _is_bool(auto_delete):
            errors.append(
                f"{label}: 'storage.auto_delete_final_clips_prod' must be a boolean, "
                f"got {auto_delete!r}"
            )
    elif require_complete:
        errors.append(
            f"{label}: missing required field 'storage.auto_delete_final_clips_prod'"
        )
        auto_delete = None

    if "allow_final_clip_auto_deletion_opt_in" in storage:
        if not _is_bool(opt_in):
            errors.append(
                f"{label}: 'storage.allow_final_clip_auto_deletion_opt_in' must be a boolean, "
                f"got {opt_in!r}"
            )
    elif require_complete:
        errors.append(
            f"{label}: missing required field 'storage.allow_final_clip_auto_deletion_opt_in'"
        )
        opt_in = None

    if auto_delete is True and opt_in is not True:
        errors.append(
            f"{label}: 'storage.auto_delete_final_clips_prod' is true without "
            f"'storage.allow_final_clip_auto_deletion_opt_in: true' — "
            f"production final-clip auto-deletion requires explicit opt-in. "
            f"Final clips are business outputs and are protected by default."
        )

    if _is_list(protected):
        protected_set = {p for p in protected if _is_nonempty_str(p)}
        finals_may_be_unprotected = auto_delete is True and opt_in is True
        if "database" not in protected_set:
            errors.append(
                f"{label}: 'storage.protected_artifact_types' must include 'database' "
                f"(active databases must never be treated as deletable retention targets)"
            )
        if "final_clip" not in protected_set and not finals_may_be_unprotected:
            errors.append(
                f"{label}: 'storage.protected_artifact_types' must include 'final_clip' "
                f"unless production final-clip auto-deletion is explicitly opted in"
            )


def _validate_system(data: dict, errors: list[str], label: str) -> None:
    _check_unknown_keys(data, _ALLOWED_SYSTEM_KEYS, errors, label)

    max_jobs = _require(data, "system.max_concurrent_jobs", errors, label)
    if max_jobs is not None and (not _is_int(max_jobs) or max_jobs <= 0):
        errors.append(
            f"{label}: 'system.max_concurrent_jobs' must be an integer > 0, got {max_jobs!r}"
        )

    retry = _require(data, "system.retry_count", errors, label)
    if retry is not None and (not _is_int(retry) or retry < 0):
        errors.append(
            f"{label}: 'system.retry_count' must be an integer >= 0, got {retry!r}"
        )

    hc = _require(data, "system.health_check_interval_seconds", errors, label)
    if hc is not None and (not _is_int(hc) or hc <= 0):
        errors.append(
            f"{label}: 'system.health_check_interval_seconds' must be an integer > 0, got {hc!r}"
        )

    model = _require(data, "ai.processing_model", errors, label)
    if model is not None and not _is_nonempty_str(model):
        errors.append(f"{label}: 'ai.processing_model' must be a non-empty string")

    restart = _require(data, "services.restart_policy", errors, label)
    if restart is not None and not _is_nonempty_str(restart):
        errors.append(f"{label}: 'services.restart_policy' must be a non-empty string")

    storage = data.get("storage")
    if storage is None:
        errors.append(f"{label}: missing required section 'storage'")
    else:
        validate_storage_policy(storage, errors, label, require_complete=True)



def _validate_funnel(
    data: dict,
    available_preset_ids: set[str],
    available_platform_ids: set[str],
    errors: list[str],
    label: str,
) -> None:
    _check_unknown_keys(data, _ALLOWED_FUNNEL_KEYS, errors, label)

    funnel_id = _require(data, "funnel.id", errors, label)
    if funnel_id is not None and not _is_nonempty_str(funnel_id):
        errors.append(f"{label}: 'funnel.id' must be a non-empty string")

    funnel_name = _require(data, "funnel.name", errors, label)
    if funnel_name is not None and not _is_nonempty_str(funnel_name):
        errors.append(f"{label}: 'funnel.name' must be a non-empty string")

    preset = _require(data, "funnel.preset", errors, label)
    if preset is not None:
        if not _is_nonempty_str(preset):
            errors.append(f"{label}: 'funnel.preset' must be a non-empty string")
        elif preset not in available_preset_ids:
            errors.append(
                f"{label}: 'funnel.preset' references missing preset '{preset}' — "
                f"available presets: {sorted(available_preset_ids)}"
            )

    enabled = _require(data, "funnel.enabled", errors, label)
    if enabled is not None and not _is_bool(enabled):
        errors.append(f"{label}: 'funnel.enabled' must be a boolean")

    channels = _require(data, "sources.channels", errors, label)
    if channels is not None and not _is_list(channels):
        errors.append(f"{label}: 'sources.channels' must be a list")

    rules = _require(data, "sources.rules", errors, label)
    if rules is not None and not _is_list(rules):
        errors.append(f"{label}: 'sources.rules' must be a list")

    preferred = _require(data, "selection.preferred_topics", errors, label)
    if preferred is not None and not _is_list(preferred):
        errors.append(f"{label}: 'selection.preferred_topics' must be a list")

    blocked = _require(data, "selection.blocked_topics", errors, label)
    if blocked is not None and not _is_list(blocked):
        errors.append(f"{label}: 'selection.blocked_topics' must be a list")

    platforms_enabled = _require(data, "platforms.enabled", errors, label)
    if platforms_enabled is not None:
        if not _is_list(platforms_enabled):
            errors.append(f"{label}: 'platforms.enabled' must be a list")
        else:
            for pid in platforms_enabled:
                if pid not in available_platform_ids:
                    errors.append(
                        f"{label}: 'platforms.enabled' references missing platform '{pid}' — "
                        f"available platforms: {sorted(available_platform_ids)}"
                    )


def _validate_platform(data: dict, filename: str, errors: list[str], label: str) -> None:
    """
    Upload precedence (documented here, not implemented):
        runtime kill switch (data/<env>/control_state.json)
            > environment uploading.enabled
            > platform uploading.enabled
    """
    _check_unknown_keys(data, _ALLOWED_PLATFORM_KEYS, errors, label)

    expected_id = Path(filename).stem
    platform_id = _require(data, "platform.id", errors, label)
    if platform_id is not None:
        if not _is_nonempty_str(platform_id):
            errors.append(f"{label}: 'platform.id' must be a non-empty string")
        elif platform_id != expected_id:
            errors.append(
                f"{label}: 'platform.id' must match filename stem '{expected_id}', got '{platform_id}'"
            )

    platform_name = _require(data, "platform.name", errors, label)
    if platform_name is not None and not _is_nonempty_str(platform_name):
        errors.append(f"{label}: 'platform.name' must be a non-empty string")

    for bool_field in ("platform.enabled", "uploading.enabled"):
        v = _require(data, bool_field, errors, label)
        if v is not None and not _is_bool(v):
            errors.append(f"{label}: '{bool_field}' must be a boolean")

    aspect = _require(data, "format.aspect_ratio", errors, label)
    if aspect is not None and not _is_nonempty_str(aspect):
        errors.append(f"{label}: 'format.aspect_ratio' must be a non-empty string")

    width = _require(data, "format.width", errors, label)
    if width is not None and (not _is_int(width) or width <= 0):
        errors.append(f"{label}: 'format.width' must be an integer > 0, got {width!r}")

    height = _require(data, "format.height", errors, label)
    if height is not None and (not _is_int(height) or height <= 0):
        errors.append(f"{label}: 'format.height' must be an integer > 0, got {height!r}")

    max_dur = _require(data, "format.max_duration_seconds", errors, label)
    if max_dur is not None and (not _is_int(max_dur) or max_dur <= 0):
        errors.append(
            f"{label}: 'format.max_duration_seconds' must be an integer > 0, got {max_dur!r}"
        )

    # title_max_length: integer >= 0 (0 = platform has no title field)
    title_len = _require(data, "format.title_max_length", errors, label)
    if title_len is not None and (not _is_int(title_len) or title_len < 0):
        errors.append(
            f"{label}: 'format.title_max_length' must be an integer >= 0 "
            f"(0 means the platform has no title field), got {title_len!r}"
        )

    cap_len = _require(data, "format.caption_max_length", errors, label)
    if cap_len is not None and (not _is_int(cap_len) or cap_len <= 0):
        errors.append(
            f"{label}: 'format.caption_max_length' must be an integer > 0, got {cap_len!r}"
        )

    # accounts: must be a dict; any account-ID values must be null or string.
    # Note: different platforms use different account key names
    # (default_channel_id, default_account_id, default_page_id).
    # We require accounts to be a non-null dict but do not mandate a single key name.
    accounts = _get(data, "accounts")
    if accounts is None:
        errors.append(f"{label}: missing required section 'accounts'")
    elif not isinstance(accounts, dict):
        errors.append(f"{label}: 'accounts' must be a mapping, got {type(accounts).__name__}")
    else:
        if len(accounts) == 0:
            errors.append(f"{label}: 'accounts' must have at least one account-ID key")
        for k, v in accounts.items():
            if v is not None and not isinstance(v, str):
                errors.append(
                    f"{label}: 'accounts.{k}' must be null or a string, got {type(v).__name__!r}"
                )


def _validate_preset(data: dict, filename: str, errors: list[str], label: str) -> None:
    _check_unknown_keys(data, _ALLOWED_PRESET_KEYS, errors, label)

    expected_id = Path(filename).stem
    preset_id = _require(data, "preset.id", errors, label)
    if preset_id is not None:
        if not _is_nonempty_str(preset_id):
            errors.append(f"{label}: 'preset.id' must be a non-empty string")
        elif preset_id != expected_id:
            errors.append(
                f"{label}: 'preset.id' must match filename stem '{expected_id}', got '{preset_id}'"
            )

    preset_name = _require(data, "preset.name", errors, label)
    if preset_name is not None and not _is_nonempty_str(preset_name):
        errors.append(f"{label}: 'preset.name' must be a non-empty string")

    mode = _require(data, "selection.mode", errors, label)
    if mode is not None and not _is_nonempty_str(mode):
        errors.append(f"{label}: 'selection.mode' must be a non-empty string")

    max_clips = _require(data, "selection.max_clips", errors, label)
    if max_clips is not None and (not _is_int(max_clips) or max_clips <= 0):
        errors.append(f"{label}: 'selection.max_clips' must be an integer > 0, got {max_clips!r}")

    pot = _require(data, "selection.min_overall_potential", errors, label)
    if pot is not None and (not _is_number(pot) or pot < 0):
        errors.append(
            f"{label}: 'selection.min_overall_potential' must be a number >= 0, got {pot!r}"
        )

    conf = _require(data, "selection.min_confidence", errors, label)
    if conf is not None and (not _is_number(conf) or not (0 <= conf <= 1)):
        errors.append(
            f"{label}: 'selection.min_confidence' must be between 0 and 1, got {conf!r}"
        )

    ratio = _require(data, "selection.exploration_ratio", errors, label)
    if ratio is not None and (not _is_number(ratio) or not (0 <= ratio <= 1)):
        errors.append(
            f"{label}: 'selection.exploration_ratio' must be between 0 and 1, got {ratio!r}"
        )

    uploads_per_day = _require(data, "posting.uploads_per_day", errors, label)
    if uploads_per_day is not None and (not _is_int(uploads_per_day) or uploads_per_day < 0):
        errors.append(
            f"{label}: 'posting.uploads_per_day' must be an integer >= 0, got {uploads_per_day!r}"
        )

    conveyor = _require(data, "post_processing.conveyor", errors, label)
    if conveyor is not None:
        if not _is_list(conveyor):
            errors.append(f"{label}: 'post_processing.conveyor' must be a list")
        else:
            for i, item in enumerate(conveyor):
                if not isinstance(item, str):
                    errors.append(
                        f"{label}: 'post_processing.conveyor[{i}]' must be a string, got {type(item).__name__!r}"
                    )


# ---------------------------------------------------------------------------
# Cross-environment path safety
# ---------------------------------------------------------------------------


def _validate_path_separation(
    dev_data: dict, prod_data: dict, errors: list[str]
) -> None:
    """Ensure dev and prod paths are not equal to each other."""
    path_keys = [
        "paths.data_root",
        "paths.jobs_root",
        "paths.outputs_root",
        "paths.logs_root",
        "paths.reports_root",
        "paths.database_path",
    ]
    for key in path_keys:
        keys = key.split(".")
        dev_val = _get(dev_data, *keys)
        prod_val = _get(prod_data, *keys)
        if dev_val and prod_val and dev_val == prod_val:
            errors.append(
                f"environments: dev and prod share the same path for '{key}': {dev_val!r} — "
                f"dev and prod state must be completely separate"
            )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def validate_config_tree(config_root: Path) -> list[str]:
    """
    Validate the entire config tree at config_root.

    Returns a list of human-readable error strings.
    An empty list means all config is valid.
    """
    errors: list[str] = []
    config_root = Path(config_root)

    # 1. Required files exist
    loaded: dict[str, Any] = {}
    for rel in REQUIRED_FILES:
        path = config_root / rel
        if not path.exists():
            errors.append(f"config/{rel}: required file is missing")
            continue
        data, err = _load_yaml(path)
        if err:
            errors.append(err)
            continue
        loaded[rel] = data

    # If we couldn't load core files, return early — downstream checks will
    # produce confusing cascading errors.
    if errors:
        return errors

    # 2. Build available preset/platform ID sets for cross-reference checks
    available_preset_ids: set[str] = set()
    for rel, data in loaded.items():
        if rel.startswith("presets/") and isinstance(data, dict):
            pid = _get(data, "preset", "id")
            if pid:
                available_preset_ids.add(pid)

    available_platform_ids: set[str] = set()
    for rel, data in loaded.items():
        if rel.startswith("platforms/") and isinstance(data, dict):
            pid = _get(data, "platform", "id")
            if pid:
                available_platform_ids.add(pid)

    # 3. Validate each file
    for rel, data in loaded.items():
        label = f"config/{rel}"

        if not isinstance(data, dict):
            errors.append(f"{label}: top-level structure must be a YAML mapping, got {type(data).__name__}")
            continue

        if rel == "defaults/default.yaml":
            _validate_defaults(data, errors, label)

        elif rel.startswith("environments/"):
            filename = Path(rel).name
            _validate_environment(data, filename, errors, label)

        elif rel == "system/system.yaml":
            _validate_system(data, errors, label)

        elif rel.startswith("funnels/"):
            _validate_funnel(data, available_preset_ids, available_platform_ids, errors, label)

        elif rel.startswith("platforms/"):
            _validate_platform(data, Path(rel).name, errors, label)

        elif rel.startswith("presets/"):
            _validate_preset(data, Path(rel).name, errors, label)

        # Secret scan on every file
        _scan_secrets(data, label, errors, label)

    # 4. Cross-environment path separation
    if "environments/dev.yaml" in loaded and "environments/prod.yaml" in loaded:
        _validate_path_separation(
            loaded["environments/dev.yaml"],
            loaded["environments/prod.yaml"],
            errors,
        )

    return errors


def _find_config_root() -> Path:
    """
    Locate the config/ directory relative to this script or the current
    working directory. Supports being called from the repo root or from
    scripts/config/.
    """
    candidates = [
        Path.cwd() / "config",
        Path(__file__).resolve().parents[2] / "config",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError(
        "Cannot locate config/ directory. "
        "Run from the repo root or pass --config-root explicitly."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the production infrastructure config tree."
    )
    parser.add_argument(
        "--config-root",
        default=None,
        help="Path to the config/ directory (default: auto-detect from repo root)",
    )
    args = parser.parse_args(argv)

    if args.config_root:
        config_root = Path(args.config_root)
    else:
        try:
            config_root = _find_config_root()
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    errors = validate_config_tree(config_root)

    if not errors:
        print(f"Config validation passed. ({config_root})")
        return 0

    print(f"Config validation failed: ({config_root})")
    for err in errors:
        print(f"  - {err}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
