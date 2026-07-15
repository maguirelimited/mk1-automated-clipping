"""Log rotation for Storage & Data Management (Phase 9).

Bounds active project log files. Rotated logs remain normal artifacts under
``logs/<env>/`` so the existing retention engine applies ``logs_days``.

Does **not** implement retention expiry, central logging, or journal deletion.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

_SCRIPTS_CONFIG = Path(__file__).resolve().parents[1] / "config"
if str(_SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_CONFIG))

from config_manager import ResolvedConfig  # noqa: E402
from state_paths import EnvironmentStatePaths  # noqa: E402

ROTATION_RECORD_SCHEMA_VERSION = 1
LATEST_RECORD_NAME = "log_rotation_latest.json"
HISTORY_RECORD_NAME = "log_rotation_history.jsonl"

# Active log suffixes under logs_root.
_ACTIVE_SUFFIXES = (".log", ".ndjson", ".jsonl")
# Rotated names: name.ext.N or name.ext.N.gz
_ROTATED_RE = re.compile(
    r"^(?P<stem>.+\.(?:log|ndjson|jsonl))\.(?P<index>\d+)(?P<gz>\.gz)?$"
)

STATUS_SUCCESS = "SUCCESS"
STATUS_SKIPPED = "SKIPPED"
STATUS_FAIL = "FAIL"
STATUS_PARTIAL = "PARTIAL"

EXIT_SUCCESS = 0
EXIT_FAIL = 1
EXIT_CONFIG = 3


@dataclass(frozen=True)
class LogRotationConfig:
    enabled: bool
    max_bytes: int
    backup_count: int
    compress: bool
    journal_system_max_use: str
    journal_runtime_max_use: str
    journal_max_file_sec: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FileRotationAction:
    path: str
    action: str
    detail: str | None = None
    size_bytes: int | None = None


@dataclass
class LogRotationResult:
    status: str
    environment: str
    enabled: bool
    duration_seconds: float = 0.0
    active_log_count: int = 0
    rotated_count: int = 0
    compressed_count: int = 0
    failure_count: int = 0
    active_log_sizes: dict[str, int] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    reason: str | None = None
    detail: str | None = None
    exit_code: int = EXIT_SUCCESS
    timestamp: str = ""
    retention_logs_days: int | None = None
    schema_version: int = ROTATION_RECORD_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env_token(resolved: ResolvedConfig) -> str:
    return "prod" if resolved.environment == "production" else "dev"


def load_log_rotation_config(resolved: ResolvedConfig) -> LogRotationConfig:
    enabled = resolved.get("storage.log_rotation.enabled")
    max_bytes = resolved.get("storage.log_rotation.max_bytes")
    backup_count = resolved.get("storage.log_rotation.backup_count")
    compress = resolved.get("storage.log_rotation.compress")
    journal_system = resolved.get("storage.log_rotation.journal.system_max_use")
    journal_runtime = resolved.get("storage.log_rotation.journal.runtime_max_use")
    journal_max_file = resolved.get("storage.log_rotation.journal.max_file_sec")

    if not isinstance(enabled, bool):
        raise ValueError("storage.log_rotation.enabled must be a boolean")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ValueError("storage.log_rotation.max_bytes must be an integer >= 1")
    if (
        not isinstance(backup_count, int)
        or isinstance(backup_count, bool)
        or backup_count < 1
    ):
        raise ValueError("storage.log_rotation.backup_count must be an integer >= 1")
    if not isinstance(compress, bool):
        raise ValueError("storage.log_rotation.compress must be a boolean")
    for label, value in (
        ("journal.system_max_use", journal_system),
        ("journal.runtime_max_use", journal_runtime),
        ("journal.max_file_sec", journal_max_file),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"storage.log_rotation.{label} must be a non-empty string")

    return LogRotationConfig(
        enabled=enabled,
        max_bytes=max_bytes,
        backup_count=backup_count,
        compress=compress,
        journal_system_max_use=str(journal_system).strip(),
        journal_runtime_max_use=str(journal_runtime).strip(),
        journal_max_file_sec=str(journal_max_file).strip(),
    )


def is_active_log_name(name: str) -> bool:
    lower = name.lower()
    if _ROTATED_RE.match(lower):
        return False
    return any(lower.endswith(suffix) for suffix in _ACTIVE_SUFFIXES)


def is_rotated_log_name(name: str) -> bool:
    return _ROTATED_RE.match(name.lower()) is not None


def discover_active_logs(logs_root: Path) -> list[Path]:
    if not logs_root.is_dir():
        return []
    found: list[Path] = []
    for path in logs_root.rglob("*"):
        if not path.is_file():
            continue
        if is_active_log_name(path.name):
            found.append(path)
    return sorted(found)


def _rotated_path(active: Path, index: int, *, compressed: bool) -> Path:
    suffix = f".{index}.gz" if compressed else f".{index}"
    return active.with_name(active.name + suffix)


def _existing_rotated(active: Path, index: int) -> Path | None:
    plain = _rotated_path(active, index, compressed=False)
    gzipped = _rotated_path(active, index, compressed=True)
    if plain.is_file():
        return plain
    if gzipped.is_file():
        return gzipped
    return None


def _compress_file(source: Path) -> Path:
    target = source.with_name(source.name + ".gz")
    if target.exists():
        target.unlink()
    with source.open("rb") as src, gzip.open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    source.unlink()
    return target


def rotate_active_log(
    active: Path,
    *,
    max_bytes: int,
    backup_count: int,
    compress: bool,
    copy_fn: Callable[[Path, Path], None] | None = None,
    truncate_fn: Callable[[Path], None] | None = None,
) -> list[FileRotationAction]:
    """Rotate one active log if it exceeds ``max_bytes``.

    Uses copy-then-truncate so open writers keep the active path and failures
    never truncate without a successful archive copy.
    """
    actions: list[FileRotationAction] = []
    if not active.is_file():
        return actions

    size = active.stat().st_size
    if size < max_bytes:
        return actions

    copy = copy_fn or shutil.copy2

    def truncate(path: Path) -> None:
        if truncate_fn is not None:
            truncate_fn(path)
            return
        with path.open("wb"):
            pass

    # Remove oldest generation beyond backup_count.
    oldest = _existing_rotated(active, backup_count)
    if oldest is not None:
        oldest.unlink()
        actions.append(
            FileRotationAction(
                path=str(oldest),
                action="removed_overflow",
                detail=f"exceeded backup_count={backup_count}",
            )
        )

    # Shift generations upward: (n-1) -> n
    for index in range(backup_count - 1, 0, -1):
        current = _existing_rotated(active, index)
        if current is None:
            continue
        target_compressed = compress and index + 1 >= 2
        # delaycompress: keep .1 uncompressed; compress when shifting to .2+
        if current.suffix == ".gz":
            target = _rotated_path(active, index + 1, compressed=True)
            if target.exists():
                target.unlink()
            current.rename(target)
            actions.append(
                FileRotationAction(path=str(target), action="shifted", detail=f"from .{index}.gz")
            )
        else:
            if target_compressed:
                # Shift then compress into .N.gz
                interim = _rotated_path(active, index + 1, compressed=False)
                if interim.exists():
                    interim.unlink()
                current.rename(interim)
                compressed_path = _compress_file(interim)
                actions.append(
                    FileRotationAction(
                        path=str(compressed_path),
                        action="shifted_and_compressed",
                        detail=f"from .{index}",
                    )
                )
            else:
                target = _rotated_path(active, index + 1, compressed=False)
                if target.exists():
                    target.unlink()
                current.rename(target)
                actions.append(
                    FileRotationAction(
                        path=str(target),
                        action="shifted",
                        detail=f"from .{index}",
                    )
                )

    # Archive active via copy, then truncate active only on success.
    archive = _rotated_path(active, 1, compressed=False)
    if archive.exists():
        archive.unlink()
    temp = active.with_name(active.name + ".rotating.tmp")
    if temp.exists():
        temp.unlink()
    try:
        copy(active, temp)
        temp.replace(archive)
    except Exception:
        if temp.exists():
            temp.unlink(missing_ok=True)
        raise

    try:
        truncate(active)
    except Exception:
        # Active preserved (still full); archive also exists — no data loss.
        raise RuntimeError(
            f"archived {archive} but failed to truncate active log {active}; "
            "active log preserved"
        )

    actions.append(
        FileRotationAction(
            path=str(archive),
            action="rotated",
            detail="copytruncate",
            size_bytes=size,
        )
    )
    return actions


def render_journald_dropin(config: LogRotationConfig) -> str:
    """Render a journald drop-in. Install under /etc/systemd/journald.conf.d/."""
    return (
        "# Generated by mk04 storage.log_rotation — do not edit by hand.\n"
        "# Install: deploy/scripts/install-log-rotation.sh\n"
        "[Journal]\n"
        f"SystemMaxUse={config.journal_system_max_use}\n"
        f"RuntimeMaxUse={config.journal_runtime_max_use}\n"
        f"MaxFileSec={config.journal_max_file_sec}\n"
    )


def render_logrotate_config(
    config: LogRotationConfig,
    *,
    env_token: str = "*",
) -> str:
    """Render host logrotate rules for deploy file sinks (not project logs_root)."""
    size_mb = max(1, config.max_bytes // (1024 * 1024))
    compress_line = "compress" if config.compress else "nocompress"
    return (
        f"# Generated by mk04 storage.log_rotation (env={env_token}).\n"
        f"# Project logs under logs/<env>/ are rotated by scripts/storage/log_rotation.py.\n"
        f"# Expired rotated logs are removed by the retention engine (logs_days).\n"
        f"\n"
        f"/var/log/mk04/{env_token}/*/*.log\n"
        f"/var/log/mk04/{env_token}/*/*.ndjson\n"
        f"/var/log/mk04/{env_token}/*/*.jsonl\n"
        f"{{\n"
        f"    daily\n"
        f"    rotate {config.backup_count}\n"
        f"    size {size_mb}M\n"
        f"    {compress_line}\n"
        f"    delaycompress\n"
        f"    missingok\n"
        f"    notifempty\n"
        f"    copytruncate\n"
        f"}}\n"
    )


def rotation_records_dir(
    resolved: ResolvedConfig,
    *,
    data_root: Path | None = None,
) -> Path:
    if data_root is not None:
        return data_root / "storage"
    state = EnvironmentStatePaths.from_resolved_config(resolved)
    return state.data_root / "storage"


def write_rotation_record(result: LogRotationResult, *, records_dir: Path) -> Path:
    records_dir.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    latest = records_dir / LATEST_RECORD_NAME
    latest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    history = records_dir / HISTORY_RECORD_NAME
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")
    return latest


def load_latest_rotation_record(*, records_dir: Path) -> dict[str, Any] | None:
    path = records_dir / LATEST_RECORD_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def run_log_rotation(
    resolved: ResolvedConfig,
    *,
    logs_root: Path | None = None,
    records_dir: Path | None = None,
    rotate_fn: Callable[..., list[FileRotationAction]] | None = None,
) -> LogRotationResult:
    """Rotate oversized active logs under the environment logs root."""
    import time

    started = time.monotonic()
    environment = _env_token(resolved)
    retention_logs_days = resolved.get("storage.retention.logs_days")
    if isinstance(retention_logs_days, bool) or not isinstance(retention_logs_days, int):
        retention_logs_days = None

    records = records_dir or rotation_records_dir(resolved)

    try:
        config = load_log_rotation_config(resolved)
    except ValueError as exc:
        result = LogRotationResult(
            status=STATUS_FAIL,
            environment=environment,
            enabled=False,
            duration_seconds=round(time.monotonic() - started, 3),
            reason=str(exc),
            detail=str(exc),
            exit_code=EXIT_CONFIG,
            timestamp=_utc_now_iso(),
            retention_logs_days=retention_logs_days,
        )
        write_rotation_record(result, records_dir=records)
        return result

    if not config.enabled:
        result = LogRotationResult(
            status=STATUS_SKIPPED,
            environment=environment,
            enabled=False,
            duration_seconds=round(time.monotonic() - started, 3),
            reason="log rotation disabled by config",
            detail="log rotation disabled by config",
            exit_code=EXIT_SUCCESS,
            timestamp=_utc_now_iso(),
            retention_logs_days=retention_logs_days,
        )
        write_rotation_record(result, records_dir=records)
        return result

    state = EnvironmentStatePaths.from_resolved_config(resolved)
    root = logs_root if logs_root is not None else state.logs_root
    active_logs = discover_active_logs(root)
    rotator = rotate_fn or rotate_active_log

    actions: list[FileRotationAction] = []
    failures: list[str] = []
    active_sizes: dict[str, int] = {}
    rotated_count = 0
    compressed_count = 0

    for active in active_logs:
        try:
            size = active.stat().st_size
        except OSError as exc:
            failures.append(f"{active}: {exc}")
            continue
        active_sizes[str(active)] = size
        try:
            file_actions = rotator(
                active,
                max_bytes=config.max_bytes,
                backup_count=config.backup_count,
                compress=config.compress,
            )
        except Exception as exc:  # noqa: BLE001 — preserve active log, record failure
            failures.append(f"{active}: {exc}")
            continue
        for action in file_actions:
            actions.append(action)
            if action.action == "rotated":
                rotated_count += 1
            if action.action == "shifted_and_compressed":
                compressed_count += 1

    if failures and rotated_count == 0 and not actions:
        status = STATUS_FAIL
        exit_code = EXIT_FAIL
    elif failures:
        status = STATUS_PARTIAL
        exit_code = EXIT_SUCCESS
    else:
        status = STATUS_SUCCESS
        exit_code = EXIT_SUCCESS

    result = LogRotationResult(
        status=status,
        environment=environment,
        enabled=True,
        duration_seconds=round(time.monotonic() - started, 3),
        active_log_count=len(active_logs),
        rotated_count=rotated_count,
        compressed_count=compressed_count,
        failure_count=len(failures),
        active_log_sizes=active_sizes,
        actions=[asdict(a) for a in actions],
        failures=failures,
        reason="; ".join(failures) if failures else None,
        detail=(
            f"active={len(active_logs)} rotated={rotated_count} "
            f"compressed={compressed_count} failures={len(failures)}; "
            f"expired rotated logs are removed by retention (logs_days="
            f"{retention_logs_days})"
        ),
        exit_code=exit_code,
        timestamp=_utc_now_iso(),
        retention_logs_days=retention_logs_days,
    )
    write_rotation_record(result, records_dir=records)
    return result
