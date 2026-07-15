#!/usr/bin/env python3
"""Per-environment pipeline execution lock (Prompt 4 — OS advisory).

Authoritative lock: fcntl.flock on <data_root>/pipeline_execution.lock
Metadata sidecar: pipeline_execution.lock.meta.json (diagnostic only)

Process death releases the flock automatically. Stale metadata never blocks
a new acquire when no OS lock is held.

Same-environment duplicate runs are rejected (non-blocking exclusive flock).
Cross-environment coordination is handled by execution_gate.py.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ops_readonly import REPO_ROOT, SCRIPTS_CONFIG, canonical_env, mk04_env

if str(SCRIPTS_CONFIG) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_CONFIG))

DEFAULT_STALE_AFTER_HOURS = 6
LOCK_SCHEMA_VERSION = 2

# Keep open fds so flock stays held for the process lifetime.
_HELD_FDS: dict[str, int] = {}


@dataclass
class ExecutionLockPayload:
    environment: str
    run_id: str
    trigger: str
    started_at: str
    pid: int | None
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS
    schema_version: int = LOCK_SCHEMA_VERSION
    funnel_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionLockPayload:
        pid_raw = data.get("pid")
        try:
            pid = int(pid_raw) if pid_raw is not None else None
        except (TypeError, ValueError):
            pid = None
        try:
            stale_after = float(data.get("stale_after_hours", DEFAULT_STALE_AFTER_HOURS))
        except (TypeError, ValueError):
            stale_after = float(DEFAULT_STALE_AFTER_HOURS)
        return cls(
            environment=str(data.get("environment") or ""),
            run_id=str(data.get("run_id") or ""),
            trigger=str(data.get("trigger") or ""),
            started_at=str(data.get("started_at") or ""),
            pid=pid,
            stale_after_hours=stale_after,
            schema_version=int(data.get("schema_version") or LOCK_SCHEMA_VERSION),
            funnel_id=str(data.get("funnel_id") or ""),
        )


@dataclass
class LockInspection:
    environment: str
    present: bool
    path: Path
    payload: ExecutionLockPayload | None = None
    stale: bool = False
    stale_reasons: list[str] | None = None
    detail: str = ""
    os_lock_held: bool = False
    metadata_authoritative: bool = False

    def __post_init__(self) -> None:
        if self.stale_reasons is None:
            self.stale_reasons = []


def _data_root_for_env(mk04_env_token: str, *, repo_root: Path | None = None) -> Path:
    token = mk04_env(canonical_env(mk04_env_token))
    root = repo_root if repo_root is not None else REPO_ROOT

    data_env = os.environ.get("MK04_DATA_ROOT", "").strip()
    if data_env:
        return Path(data_env).expanduser().resolve()

    config_root = root / "config"
    if config_root.is_dir() and (config_root / "environments").is_dir():
        try:
            from config_manager import ConfigManager  # noqa: PLC0415

            resolved = ConfigManager.load(
                environment=canonical_env(mk04_env_token),
                config_root=config_root,
            )
            return resolved.paths.data_root
        except Exception:
            if token == "prod" and (
                os.environ.get("MK04_RUNTIME_ROOT", "").strip()
                or os.environ.get("MK04_REQUIRE_RUNTIME_PATHS", "").strip()
                in {"1", "true", "yes"}
            ):
                raise

    return (root / "data" / token).resolve()


def lock_path_for_env(mk04_env_token: str, *, repo_root: Path | None = None) -> Path:
    return _data_root_for_env(mk04_env_token, repo_root=repo_root) / "pipeline_execution.lock"


def meta_path_for_env(mk04_env_token: str, *, repo_root: Path | None = None) -> Path:
    return Path(str(lock_path_for_env(mk04_env_token, repo_root=repo_root)) + ".meta.json")


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def pid_is_alive(pid: int | None) -> bool | None:
    if pid is None or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def evaluate_staleness(
    payload: ExecutionLockPayload, *, now: datetime | None = None
) -> tuple[bool, list[str]]:
    """Heuristic staleness of *metadata* only — never authoritative alone."""
    current = now or datetime.now(UTC)
    reasons: list[str] = []
    alive = pid_is_alive(payload.pid)
    if alive is False:
        reasons.append(f"holder pid {payload.pid} is not running")
    started = _parse_iso(payload.started_at)
    if started is None:
        reasons.append("started_at missing or unparseable")
    else:
        threshold_hours = float(payload.stale_after_hours or DEFAULT_STALE_AFTER_HOURS)
        age = current - started
        if age > timedelta(hours=threshold_hours):
            reasons.append(
                f"lock age {age.total_seconds():.0f}s exceeds stale_after_hours={threshold_hours}"
            )
    return (len(reasons) > 0, reasons)


def _probe_os_lock_held(path: Path) -> bool:
    if not path.exists():
        return False
    fd = -1
    try:
        fd = os.open(str(path), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def read_lock(path: Path) -> ExecutionLockPayload | None:
    meta = Path(str(path) + ".meta.json")
    candidates = [meta, path]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            raw = candidate.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        # Ignore empty flock placeholder files.
        if "run_id" not in data and "environment" not in data:
            continue
        try:
            return ExecutionLockPayload.from_dict(data)
        except Exception:
            continue
    return None


def inspect_execution_lock(
    mk04_env_token: str, *, repo_root: Path | None = None
) -> LockInspection:
    path = lock_path_for_env(mk04_env_token, repo_root=repo_root)
    token = mk04_env(canonical_env(mk04_env_token))
    os_held = _probe_os_lock_held(path)
    payload = read_lock(path)
    if not os_held and payload is None:
        return LockInspection(
            environment=token,
            present=False,
            path=path,
            detail="no execution lock present",
            os_lock_held=False,
            metadata_authoritative=False,
        )
    if not os_held and payload is not None:
        stale, reasons = evaluate_staleness(payload)
        return LockInspection(
            environment=token,
            present=False,
            path=path,
            payload=payload,
            stale=True,
            stale_reasons=reasons or ["OS lock not held; metadata is non-authoritative"],
            detail=(
                "stale metadata only (OS lock not held); does not block new runs — "
                f"run_id={payload.run_id}"
            ),
            os_lock_held=False,
            metadata_authoritative=False,
        )
    # OS lock held.
    if payload is None:
        return LockInspection(
            environment=token,
            present=True,
            path=path,
            detail="execution lock held (metadata unavailable)",
            os_lock_held=True,
            metadata_authoritative=True,
        )
    stale, reasons = evaluate_staleness(payload)
    detail = (
        f"active execution lock run_id={payload.run_id} "
        f"trigger={payload.trigger} pid={payload.pid} "
        f"started_at={payload.started_at}"
    )
    if stale:
        detail = f"OS lock held with aged metadata: {detail}; " + "; ".join(reasons)
    return LockInspection(
        environment=token,
        present=True,
        path=path,
        payload=payload,
        stale=False,  # OS lock is authoritative; do not treat as blocking-stale
        stale_reasons=[],
        detail=detail,
        os_lock_held=True,
        metadata_authoritative=True,
    )


def build_lock_payload(
    *,
    environment: str,
    run_id: str,
    trigger: str,
    funnel_id: str = "",
    pid: int | None = None,
    started_at: str | None = None,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
) -> ExecutionLockPayload:
    return ExecutionLockPayload(
        environment=mk04_env(canonical_env(environment)),
        run_id=run_id,
        trigger=trigger,
        started_at=started_at or _utc_now_iso(),
        pid=pid if pid is not None else os.getpid(),
        stale_after_hours=stale_after_hours,
        funnel_id=funnel_id or "",
    )


def acquire_lock(
    mk04_env_token: str,
    payload: ExecutionLockPayload,
    *,
    repo_root: Path | None = None,
) -> tuple[bool, str, LockInspection | None]:
    """Acquire exclusive non-blocking flock for this environment."""
    path = lock_path_for_env(mk04_env_token, repo_root=repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        inspection = inspect_execution_lock(mk04_env_token, repo_root=repo_root)
        detail = (
            "execution lock not acquired: active run in progress; " + inspection.detail
        )
        return False, detail, inspection
    except OSError as exc:
        os.close(fd)
        return False, f"execution lock not acquired: {exc}", None

    _HELD_FDS[key] = fd
    meta = meta_path_for_env(mk04_env_token, repo_root=repo_root)
    try:
        meta.write_text(json.dumps(payload.to_dict(), indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError:
            pass
        _HELD_FDS.pop(key, None)
        return False, f"execution lock write failed: {exc}", None
    return True, f"execution lock acquired path={path}", None


def release_lock(
    mk04_env_token: str,
    *,
    run_id: str,
    pid: int | None = None,
    repo_root: Path | None = None,
) -> tuple[bool, str]:
    path = lock_path_for_env(mk04_env_token, repo_root=repo_root)
    key = str(path.resolve())
    payload = read_lock(path)
    if payload is not None and payload.run_id != run_id:
        return (
            False,
            f"execution lock owned by run_id={payload.run_id}, not releasing for run_id={run_id}",
        )
    if (
        payload is not None
        and pid is not None
        and payload.pid is not None
        and int(payload.pid) != int(pid)
    ):
        return (
            False,
            f"execution lock pid={payload.pid} does not match caller pid={pid}; not releasing",
        )

    fd = _HELD_FDS.pop(key, None)
    if fd is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except OSError as exc:
            return False, f"execution lock release failed: {exc}"
    meta = meta_path_for_env(mk04_env_token, repo_root=repo_root)
    try:
        meta.unlink(missing_ok=True)
    except OSError:
        pass
    return True, f"execution lock released path={path}"
