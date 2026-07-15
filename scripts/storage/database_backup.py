"""Database backup for Storage & Data Management (Phase 10).

Creates consistent SQLite snapshots without modifying the live database.
Backup files are normal ``database_backup`` artifacts; retention owns expiry
via ``storage.retention.database_backups_days``.

Does **not** implement replication, remote backup, or separate deletion logic.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

_SCRIPTS_CONFIG = Path(__file__).resolve().parents[1] / "config"
if str(_SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_CONFIG))

from config_manager import ResolvedConfig  # noqa: E402
from state_paths import EnvironmentStatePaths  # noqa: E402

BACKUP_RECORD_SCHEMA_VERSION = 1
LATEST_RECORD_NAME = "database_backup_latest.json"
HISTORY_RECORD_NAME = "database_backup_history.jsonl"

STATUS_SUCCESS = "SUCCESS"
STATUS_SKIPPED = "SKIPPED"
STATUS_FAIL = "FAIL"

EXIT_SUCCESS = 0
EXIT_FAIL = 1
EXIT_CONFIG = 3


@dataclass(frozen=True)
class DatabaseBackupConfig:
    enabled: bool
    verify_integrity: bool
    location: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DatabaseBackupResult:
    status: str
    environment: str
    enabled: bool
    duration_seconds: float = 0.0
    database_path: str | None = None
    backup_path: str | None = None
    manifest_path: str | None = None
    backup_size_bytes: int | None = None
    backup_count: int = 0
    integrity_ok: bool | None = None
    reason: str | None = None
    detail: str | None = None
    exit_code: int = EXIT_SUCCESS
    timestamp: str = ""
    retention_database_backups_days: int | None = None
    schema_version: int = BACKUP_RECORD_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _utc_iso(moment: datetime | None = None) -> str:
    value = moment or _utc_now()
    return value.isoformat().replace("+00:00", "Z")


def _env_token(resolved: ResolvedConfig) -> str:
    return "prod" if resolved.environment == "production" else "dev"


def load_database_backup_config(resolved: ResolvedConfig) -> DatabaseBackupConfig:
    enabled = resolved.get("storage.database_backup.enabled")
    verify = resolved.get("storage.database_backup.verify_integrity")
    location = resolved.get("storage.database_backup.location")
    if not isinstance(enabled, bool):
        raise ValueError("storage.database_backup.enabled must be a boolean")
    if not isinstance(verify, bool):
        raise ValueError("storage.database_backup.verify_integrity must be a boolean")
    if not isinstance(location, str) or not location.strip():
        raise ValueError("storage.database_backup.location must be a non-empty string")
    return DatabaseBackupConfig(
        enabled=enabled,
        verify_integrity=verify,
        location=location.strip(),
    )


def resolve_backup_dir(resolved: ResolvedConfig, *, location: str | None = None) -> Path:
    """Resolve backup directory from config location template."""
    token = _env_token(resolved)
    template = location
    if template is None:
        template = str(resolved.get("storage.database_backup.location") or "")
    rendered = template.replace("{env}", token)
    path = Path(rendered)
    if path.is_absolute():
        return path
    return (resolved._repo_root / path).resolve()


def backup_records_dir(
    resolved: ResolvedConfig,
    *,
    data_root: Path | None = None,
) -> Path:
    if data_root is not None:
        return data_root / "storage"
    state = EnvironmentStatePaths.from_resolved_config(resolved)
    return state.data_root / "storage"


def write_backup_record(result: DatabaseBackupResult, *, records_dir: Path) -> Path:
    records_dir.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    latest = records_dir / LATEST_RECORD_NAME
    latest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    history = records_dir / HISTORY_RECORD_NAME
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")
    return latest


def load_latest_backup_record(*, records_dir: Path) -> dict[str, Any] | None:
    path = records_dir / LATEST_RECORD_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def count_database_backups(backup_dir: Path) -> int:
    if not backup_dir.is_dir():
        return 0
    return sum(
        1
        for path in backup_dir.iterdir()
        if path.is_file() and _is_database_backup_file(path.name)
    )


def _is_database_backup_file(name: str) -> bool:
    lower = name.lower()
    if not lower.startswith("db_"):
        return False
    return lower.endswith(".sqlite3") or lower.endswith(".db")


def verify_sqlite_integrity(path: Path) -> tuple[bool, str]:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return False, f"open failed: {exc}"
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        message = str(row[0]) if row else "no result"
        ok = message.lower() == "ok"
        return ok, message
    except sqlite3.Error as exc:
        return False, f"integrity_check failed: {exc}"
    finally:
        conn.close()


def create_sqlite_backup(
    source_db: Path,
    dest_db: Path,
    *,
    connect_fn: Callable[[str], sqlite3.Connection] | None = None,
) -> None:
    """Copy ``source_db`` to ``dest_db`` using the SQLite backup API.

    Opens the source read-only and never writes to it. Writes only to ``dest_db``.
    """
    connect = connect_fn or sqlite3.connect
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    if dest_db.exists():
        dest_db.unlink()

    source: sqlite3.Connection | None = None
    dest: sqlite3.Connection | None = None
    try:
        # Read-only URI avoids accidental writes to the live database.
        source = connect(f"file:{source_db}?mode=ro", uri=True, timeout=30.0)
        dest = connect(str(dest_db), timeout=30.0)
        source.backup(dest)
        dest.commit()
    finally:
        if dest is not None:
            dest.close()
        if source is not None:
            source.close()


def run_database_backup(
    resolved: ResolvedConfig,
    *,
    now: datetime | None = None,
    records_dir: Path | None = None,
    backup_dir: Path | None = None,
    create_fn: Callable[[Path, Path], None] | None = None,
    verify_fn: Callable[[Path], tuple[bool, str]] | None = None,
) -> DatabaseBackupResult:
    """Create one database backup and record the outcome."""
    started = time.monotonic()
    environment = _env_token(resolved)
    state = EnvironmentStatePaths.from_resolved_config(resolved)
    database_path = state.database_path
    retention_days = resolved.get("storage.retention.database_backups_days")
    if isinstance(retention_days, bool) or not isinstance(retention_days, int):
        retention_days = None

    records = records_dir or backup_records_dir(resolved)

    try:
        config = load_database_backup_config(resolved)
    except ValueError as exc:
        result = DatabaseBackupResult(
            status=STATUS_FAIL,
            environment=environment,
            enabled=False,
            duration_seconds=round(time.monotonic() - started, 3),
            database_path=str(database_path),
            reason=str(exc),
            detail=str(exc),
            exit_code=EXIT_CONFIG,
            timestamp=_utc_iso(),
            retention_database_backups_days=retention_days,
        )
        write_backup_record(result, records_dir=records)
        return result

    if not config.enabled:
        result = DatabaseBackupResult(
            status=STATUS_SKIPPED,
            environment=environment,
            enabled=False,
            duration_seconds=round(time.monotonic() - started, 3),
            database_path=str(database_path),
            reason="database backup disabled by config",
            detail="database backup disabled by config",
            exit_code=EXIT_SUCCESS,
            timestamp=_utc_iso(),
            retention_database_backups_days=retention_days,
            backup_count=count_database_backups(
                backup_dir or resolve_backup_dir(resolved, location=config.location)
            ),
        )
        write_backup_record(result, records_dir=records)
        return result

    target_dir = backup_dir or resolve_backup_dir(resolved, location=config.location)
    existing_count = count_database_backups(target_dir)

    if not database_path.is_file():
        result = DatabaseBackupResult(
            status=STATUS_FAIL,
            environment=environment,
            enabled=True,
            duration_seconds=round(time.monotonic() - started, 3),
            database_path=str(database_path),
            backup_count=existing_count,
            reason=f"database does not exist: {database_path}",
            detail=f"database does not exist: {database_path}",
            exit_code=EXIT_FAIL,
            timestamp=_utc_iso(),
            retention_database_backups_days=retention_days,
        )
        write_backup_record(result, records_dir=records)
        return result

    moment = now or _utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    backup_id = f"db_{environment}_{stamp}"
    final_path = target_dir / f"{backup_id}.sqlite3"
    manifest_path = target_dir / f"{backup_id}.manifest.json"
    temp_path = target_dir / f".{backup_id}.sqlite3.tmp"

    creator = create_fn or create_sqlite_backup
    verifier = verify_fn or verify_sqlite_integrity

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if temp_path.exists():
            temp_path.unlink()
        creator(database_path, temp_path)
        if not temp_path.is_file() or temp_path.stat().st_size <= 0:
            raise RuntimeError("backup file missing or empty after create")

        integrity_ok: bool | None = None
        integrity_detail = "skipped"
        if config.verify_integrity:
            integrity_ok, integrity_detail = verifier(temp_path)
            if not integrity_ok:
                raise RuntimeError(f"backup integrity check failed: {integrity_detail}")

        # Atomic publish: only promote after successful create (+ optional verify).
        if final_path.exists():
            final_path.unlink()
        temp_path.replace(final_path)

        size = final_path.stat().st_size
        manifest = {
            "schema_version": BACKUP_RECORD_SCHEMA_VERSION,
            "backup_id": backup_id,
            "environment": environment,
            "source_database": str(database_path),
            "backup_path": str(final_path),
            "backup_size_bytes": size,
            "created_at": _utc_iso(moment),
            "verify_integrity": config.verify_integrity,
            "integrity_ok": integrity_ok,
            "integrity_detail": integrity_detail,
            "retention_database_backups_days": retention_days,
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        result = DatabaseBackupResult(
            status=STATUS_SUCCESS,
            environment=environment,
            enabled=True,
            duration_seconds=round(time.monotonic() - started, 3),
            database_path=str(database_path),
            backup_path=str(final_path),
            manifest_path=str(manifest_path),
            backup_size_bytes=size,
            backup_count=count_database_backups(target_dir),
            integrity_ok=integrity_ok,
            detail=(
                f"backup created: {final_path} "
                f"(integrity={integrity_detail}); "
                f"expiry owned by retention (database_backups_days={retention_days})"
            ),
            exit_code=EXIT_SUCCESS,
            timestamp=_utc_iso(moment),
            retention_database_backups_days=retention_days,
        )
        write_backup_record(result, records_dir=records)
        return result
    except Exception as exc:  # noqa: BLE001 — never damage live DB; record failure
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        # Never delete previously successful backups on failure.
        result = DatabaseBackupResult(
            status=STATUS_FAIL,
            environment=environment,
            enabled=True,
            duration_seconds=round(time.monotonic() - started, 3),
            database_path=str(database_path),
            backup_count=count_database_backups(target_dir),
            reason=str(exc),
            detail=str(exc),
            exit_code=EXIT_FAIL,
            timestamp=_utc_iso(),
            retention_database_backups_days=retention_days,
        )
        write_backup_record(result, records_dir=records)
        return result
