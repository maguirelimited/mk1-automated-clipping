#!/usr/bin/env python3
"""Cross-environment execution gate (Prompt 4).

Uses OS advisory locks (fcntl.flock) so process death releases authority.

Shared root (deployed):
  /var/lib/mk04/locks   (or MK04_SHARED_LOCK_ROOT)

Lock files:
  production_turnstile.lock  — prod exclusive priority; dev non-blocking shared admission
  global_pipeline.lock       — exclusive while WhisperX/AI/FFmpeg/handoff runs (worker-held)
  gate_status.json           — diagnostic metadata only (never authoritative)

Production must use the deployed shared root and must never fall back to a
repository lock directory. Development may use repo/.mk04_locks only before
production is installed on the host.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


DEFAULT_DEPLOYED_SHARED_ROOT = Path("/var/lib/mk04/locks")
TURNSTILE_NAME = "production_turnstile.lock"
GLOBAL_NAME = "global_pipeline.lock"
PROMOTION_NAME = "promotion.lock"
STATUS_NAME = "gate_status.json"

GATE_FREE = "free"
GATE_DEV_ACTIVE = "development_active"
GATE_PROD_WAITING = "production_waiting"
GATE_PROD_ACTIVE = "production_active"
GATE_PROMOTION = "promotion_maintenance"

_BOOTSTRAP_HINT = (
    "complete production bootstrap so /var/lib/mk04/locks exists, "
    "is owned by mk04:mk04 (or equivalent), and is group-writable for operators"
)


class GateError(RuntimeError):
    """Admission or shared-lock configuration failed."""


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_production_env(environment: str) -> bool:
    return str(environment).strip().lower() in {"prod", "production"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _is_repo_fallback(path: Path) -> bool:
    text = str(path).replace("\\", "/")
    return "/.mk04_locks" in text or text.endswith("/.mk04_locks")


def production_installation_present() -> bool:
    """True when a production install is present on this host."""
    flag = (os.environ.get("MK04_PRODUCTION_INSTALLED") or "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    if Path("/opt/mk04/prod/current").is_dir():
        return True
    if Path("/etc/mk04/prod").is_dir():
        return True
    code = (os.environ.get("MK04_CODE_ROOT") or "").strip()
    if code:
        try:
            if Path(code).expanduser().resolve() == Path("/opt/mk04/prod/current"):
                return True
        except OSError:
            pass
    return False


def _path_is_usable_dir(path: Path) -> bool:
    try:
        return path.is_dir() and os.access(path, os.W_OK | os.R_OK | os.X_OK)
    except OSError:
        return False


def _require_usable_lock_root(path: Path, *, context: str) -> Path:
    resolved = path.expanduser()
    try:
        resolved = resolved.resolve()
    except OSError as exc:
        raise GateError(
            f"{context}: cannot resolve shared lock root {path}: {exc}"
        ) from exc
    if not resolved.exists():
        raise GateError(
            f"{context}: shared lock root does not exist: {resolved}. {_BOOTSTRAP_HINT}"
        )
    if not resolved.is_dir():
        raise GateError(f"{context}: shared lock root is not a directory: {resolved}")
    if not os.access(resolved, os.W_OK | os.R_OK | os.X_OK):
        raise GateError(
            f"{context}: shared lock root is not writable: {resolved}. {_BOOTSTRAP_HINT}"
        )
    return resolved


def _dev_repo_fallback_root() -> Path:
    code = (os.environ.get("MK04_CODE_ROOT") or "").strip()
    if code:
        try:
            return Path(code).expanduser().resolve() / ".mk04_locks"
        except OSError:
            pass
    return (_repo_root() / ".mk04_locks").resolve()


def resolve_shared_lock_root(
    *,
    allow_dev_fallback: bool = True,
    environment: str | None = None,
) -> Path:
    """
    Resolve the shared lock directory.

    Rules:
      A. Explicit MK04_SHARED_LOCK_ROOT is preserved and validated (never replaced).
      B. Production uses the deployed shared root; never a repository fallback.
      C. Development before production install may use repo/.mk04_locks when the
         deployed root is absent/unusable.
      D. Once production is installed, development must use the same deployed root.
    """
    explicit_raw = os.environ.get("MK04_SHARED_LOCK_ROOT", "").strip()
    env = (environment or os.environ.get("MK04_ENV") or "").strip().lower()
    production = env in {"prod", "production"}

    if explicit_raw:
        try:
            path = Path(explicit_raw).expanduser().resolve()
        except OSError as exc:
            raise GateError(
                f"MK04_SHARED_LOCK_ROOT is unusable ({explicit_raw}): {exc}"
            ) from exc
        if production and _is_repo_fallback(path):
            raise GateError(
                f"production MK04_SHARED_LOCK_ROOT must not be a repository path: {path}"
            )
        if (
            production_installation_present()
            and not production
            and _is_repo_fallback(path)
        ):
            raise GateError(
                "development must not use a repository lock root once production is "
                f"installed; set MK04_SHARED_LOCK_ROOT to {DEFAULT_DEPLOYED_SHARED_ROOT} "
                f"({_BOOTSTRAP_HINT})"
            )
        return path

    if production:
        return DEFAULT_DEPLOYED_SHARED_ROOT.resolve()

    deployed = DEFAULT_DEPLOYED_SHARED_ROOT
    if production_installation_present():
        return deployed.resolve()

    if _path_is_usable_dir(deployed):
        return deployed.resolve()

    if not allow_dev_fallback:
        raise GateError("MK04_SHARED_LOCK_ROOT is not configured")

    return _dev_repo_fallback_root()


def _may_create_lock_root(path: Path) -> bool:
    """Only repository/temporary fallbacks may be auto-created."""
    text = str(path).replace("\\", "/")
    return _is_repo_fallback(path) or "/tmp/" in text or text.startswith("/tmp/")


def ensure_shared_lock_root(
    root: Path | None = None,
    *,
    environment: str | None = None,
) -> Path:
    env = (environment or os.environ.get("MK04_ENV") or "").strip().lower()
    production = env in {"prod", "production"}
    try:
        if root is not None:
            try:
                resolved = Path(root).expanduser().resolve()
            except OSError as exc:
                raise GateError(f"shared lock root unusable: {exc}") from exc
        else:
            resolved = resolve_shared_lock_root(environment=environment)

        if production or (
            production_installation_present() and not _is_repo_fallback(resolved)
        ):
            return _require_usable_lock_root(
                resolved,
                context=(
                    "production shared lock root"
                    if production
                    else "development shared lock root (production installed)"
                ),
            )

        # Explicit operator/env override (non-repo) must already exist and be usable.
        explicit_raw = os.environ.get("MK04_SHARED_LOCK_ROOT", "").strip()
        if explicit_raw and not _is_repo_fallback(resolved):
            try:
                explicit_path = Path(explicit_raw).expanduser().resolve()
            except OSError as exc:
                raise GateError(
                    f"explicit MK04_SHARED_LOCK_ROOT is unusable: {exc}"
                ) from exc
            if resolved == explicit_path:
                return _require_usable_lock_root(
                    resolved, context="explicit MK04_SHARED_LOCK_ROOT"
                )

        if _is_repo_fallback(resolved) or _may_create_lock_root(resolved):
            try:
                resolved.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise GateError(
                    f"cannot create development shared lock root {resolved}: {exc}"
                ) from exc
            if not os.access(resolved, os.W_OK | os.R_OK | os.X_OK):
                raise GateError(
                    f"development shared lock root is not writable: {resolved}"
                )
            return resolved

        return _require_usable_lock_root(resolved, context="shared lock root")
    except GateError:
        raise
    except OSError as exc:
        raise GateError(f"shared lock root filesystem error: {exc}") from exc


@dataclass
class GateStatusSnapshot:
    state: str = GATE_FREE
    owning_environment: str | None = None
    run_id: str | None = None
    pid: int | None = None
    trigger: str | None = None
    requested_at: str | None = None
    job_id: str | None = None
    stage: str | None = None
    detail: str | None = None
    metadata_authoritative: bool = False
    shared_lock_root: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _HeldLock:
    path: Path
    fd: int
    mode: str  # shared | exclusive


@dataclass
class AdmissionHandle:
    """Orchestration admission: turnstile (+ optional waiting claim for prod)."""

    environment: str
    run_id: str
    trigger: str
    shared_root: Path
    turnstile: _HeldLock | None = None
    production_priority: bool = False
    released: bool = False

    def release_turnstile(self) -> None:
        """Dev releases shared turnstile after admission so prod can wait."""
        if self.turnstile is None:
            return
        try:
            fcntl.flock(self.turnstile.fd, fcntl.LOCK_UN)
            os.close(self.turnstile.fd)
        except OSError:
            pass
        self.turnstile = None
        _write_status(
            self.shared_root,
            {
                "state": GATE_DEV_ACTIVE if not self.production_priority else GATE_PROD_WAITING,
                "owning_environment": self.environment,
                "run_id": self.run_id,
                "pid": os.getpid(),
                "trigger": self.trigger,
                "requested_at": _utc_now_iso(),
                "detail": "turnstile released; heavy work may continue under global lock",
                "metadata_authoritative": False,
            },
        )

    def release(self) -> None:
        if self.released:
            return
        self.release_turnstile()
        self.released = True
        _clear_status_if_owner(self.shared_root, self.run_id)


@dataclass
class HeavyWorkHandle:
    """Worker-held exclusive global pipeline lock."""

    environment: str
    run_id: str
    job_id: str
    shared_root: Path
    held: _HeldLock
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        try:
            fcntl.flock(self.held.fd, fcntl.LOCK_UN)
            os.close(self.held.fd)
        except OSError:
            pass
        self.released = True
        _write_status(
            self.shared_root,
            {
                "state": GATE_FREE,
                "owning_environment": None,
                "run_id": None,
                "pid": None,
                "job_id": None,
                "detail": "global pipeline lock released",
                "metadata_authoritative": False,
            },
        )


def _open_lock_file(path: Path) -> int:
    """Open/create a lock file as group-writable (0660), defeating umask."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o660)
    except OSError as exc:
        raise GateError(
            f"cannot open lock file {path}: {exc}. {_BOOTSTRAP_HINT}"
        ) from exc
    try:
        _harden_lock_fd(fd, path)
    except OSError:
        # Mode hardening is best-effort when the opener lacks ownership;
        # open still succeeds so holders can proceed.
        pass
    return fd


def _harden_lock_fd(fd: int, path: Path) -> None:
    """Best-effort 0660 + parent-dir group.

    O_RDWR + flock is authoritative for lock success. Non-owners (group-authorized
    operators reopening an mk04-owned 0660 lock) must not treat expected EPERM from
    chmod/chown as a lock failure. Incorrectly configured locks (e.g. 0644 without
    group write) still fail closed when os.open(O_RDWR) raises.
    """
    try:
        st = os.fstat(fd)
    except OSError:
        return
    euid = os.geteuid()
    # Only the owner (or root) can change mode/ownership.
    if euid != 0 and st.st_uid != euid:
        return
    try:
        os.fchmod(fd, 0o660)
    except OSError:
        return
    try:
        parent_gid = path.parent.stat().st_gid
    except OSError:
        return
    try:
        if st.st_gid != parent_gid:
            os.fchown(fd, -1, parent_gid)
    except OSError:
        pass
    try:
        os.fchmod(fd, 0o660)
    except OSError:
        pass


def _write_status(root: Path, payload: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / STATUS_NAME
    data = dict(payload)
    data["shared_lock_root"] = str(root)
    data["updated_at"] = _utc_now_iso()
    tmp = path.with_suffix(".tmp")
    body = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    try:
        # Create with mode 0660 so umask cannot leave a world-writable status file.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o660)
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        try:
            parent_gid = root.stat().st_gid
        except OSError:
            parent_gid = -1
        try:
            os.chmod(path, 0o660)
        except OSError:
            pass
        if parent_gid >= 0:
            try:
                os.chown(path, -1, parent_gid)
            except OSError:
                # Non-owner writers rely on setgid lock dirs for group mk04.
                pass
        try:
            os.chmod(path, 0o660)
        except OSError:
            pass
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _clear_status_if_owner(root: Path, run_id: str) -> None:
    path = root / STATUS_NAME
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if str(data.get("run_id") or "") == run_id:
        _write_status(
            root,
            {
                "state": GATE_FREE,
                "detail": "admission released",
                "metadata_authoritative": False,
            },
        )


def read_gate_status(*, shared_root: Path | None = None) -> GateStatusSnapshot:
    root = shared_root or resolve_shared_lock_root(allow_dev_fallback=True)
    path = root / STATUS_NAME
    snap = GateStatusSnapshot(shared_lock_root=str(root))
    if not path.is_file():
        snap.state = GATE_FREE
        return snap
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        snap.state = GATE_FREE
        snap.detail = "gate status unreadable"
        return snap
    snap.state = str(data.get("state") or GATE_FREE)
    snap.owning_environment = data.get("owning_environment")
    snap.run_id = data.get("run_id")
    snap.pid = data.get("pid")
    snap.trigger = data.get("trigger")
    snap.requested_at = data.get("requested_at") or data.get("updated_at")
    snap.job_id = data.get("job_id")
    snap.stage = data.get("stage")
    snap.detail = data.get("detail")
    snap.metadata_authoritative = bool(data.get("metadata_authoritative"))
    snap.shared_lock_root = str(data.get("shared_lock_root") or root)

    # Probe whether locks are actually held (authoritative).
    turnstile_held = _probe_exclusive_held(root / TURNSTILE_NAME)
    global_held = _probe_exclusive_held(root / GLOBAL_NAME)
    if not turnstile_held and not global_held:
        if snap.state != GATE_FREE:
            snap.detail = (snap.detail or "") + " (stale metadata; no OS lock held)"
            snap.metadata_authoritative = False
            snap.state = GATE_FREE
    else:
        snap.metadata_authoritative = True
        if global_held and snap.state == GATE_FREE:
            snap.state = GATE_PROD_ACTIVE if snap.owning_environment in {"prod", "production"} else GATE_DEV_ACTIVE
        if turnstile_held and not global_held and _is_production_env(str(snap.owning_environment or "")):
            snap.state = GATE_PROD_WAITING
    return snap


def _probe_exclusive_held(path: Path) -> bool:
    """Return True if another process holds an exclusive flock on path."""
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


def admit_orchestration(
    *,
    environment: str,
    run_id: str,
    trigger: str,
    shared_root: Path | None = None,
) -> AdmissionHandle:
    """
    Apply production-turnstile admission.

    Development: non-blocking shared turnstile; refuses if prod holds exclusive.
    Production: blocking exclusive turnstile (waits for shared holders to leave).
    """
    root = ensure_shared_lock_root(shared_root, environment=environment)
    turnstile_path = root / TURNSTILE_NAME
    fd = _open_lock_file(turnstile_path)
    production = _is_production_env(environment)
    try:
        if production:
            _write_status(
                root,
                {
                    "state": GATE_PROD_WAITING,
                    "owning_environment": "prod",
                    "run_id": run_id,
                    "pid": os.getpid(),
                    "trigger": trigger,
                    "requested_at": _utc_now_iso(),
                    "detail": "production waiting for turnstile / resource",
                    "metadata_authoritative": True,
                },
            )
            fcntl.flock(fd, fcntl.LOCK_EX)  # blocks while any shared (dev) holder exists
            handle = AdmissionHandle(
                environment="prod",
                run_id=run_id,
                trigger=trigger,
                shared_root=root,
                turnstile=_HeldLock(turnstile_path, fd, "exclusive"),
                production_priority=True,
            )
            _write_status(
                root,
                {
                    "state": GATE_PROD_WAITING,
                    "owning_environment": "prod",
                    "run_id": run_id,
                    "pid": os.getpid(),
                    "trigger": trigger,
                    "requested_at": _utc_now_iso(),
                    "detail": "production holds turnstile; waiting for or running heavy work",
                    "metadata_authoritative": True,
                },
            )
            return handle

        # Development: shared, non-blocking.
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = -1
            snap = read_gate_status(shared_root=root)
            raise GateError(
                "development admission refused: production is active or waiting "
                f"(gate_state={snap.state}, run_id={snap.run_id})"
            ) from exc
        handle = AdmissionHandle(
            environment="dev",
            run_id=run_id,
            trigger=trigger,
            shared_root=root,
            turnstile=_HeldLock(turnstile_path, fd, "shared"),
            production_priority=False,
        )
        _write_status(
            root,
            {
                "state": GATE_DEV_ACTIVE,
                "owning_environment": "dev",
                "run_id": run_id,
                "pid": os.getpid(),
                "trigger": trigger,
                "requested_at": _utc_now_iso(),
                "detail": "development admitted",
                "metadata_authoritative": True,
            },
        )
        return handle
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def acquire_global_pipeline_lock(
    *,
    environment: str,
    run_id: str,
    job_id: str,
    shared_root: Path | None = None,
    blocking: bool = True,
) -> HeavyWorkHandle:
    """Acquire exclusive global pipeline lock for heavy worker processing."""
    root = ensure_shared_lock_root(shared_root, environment=environment)
    path = root / GLOBAL_NAME
    fd = _open_lock_file(path)
    flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        fcntl.flock(fd, flags)
    except BlockingIOError as exc:
        os.close(fd)
        raise GateError("global pipeline lock busy") from exc
    except Exception:
        os.close(fd)
        raise

    env_token = "prod" if _is_production_env(environment) else "dev"
    _write_status(
        root,
        {
            "state": GATE_PROD_ACTIVE if env_token == "prod" else GATE_DEV_ACTIVE,
            "owning_environment": env_token,
            "run_id": run_id,
            "pid": os.getpid(),
            "job_id": job_id,
            "stage": "heavy_processing",
            "requested_at": _utc_now_iso(),
            "detail": "global pipeline lock held by worker",
            "metadata_authoritative": True,
        },
    )
    return HeavyWorkHandle(
        environment=env_token,
        run_id=run_id,
        job_id=job_id,
        shared_root=root,
        held=_HeldLock(path, fd, "exclusive"),
    )


@contextmanager
def heavy_work_lock(
    *,
    environment: str,
    run_id: str,
    job_id: str,
    shared_root: Path | None = None,
    blocking: bool = True,
) -> Iterator[HeavyWorkHandle]:
    handle = acquire_global_pipeline_lock(
        environment=environment,
        run_id=run_id,
        job_id=job_id,
        shared_root=shared_root,
        blocking=blocking,
    )
    try:
        yield handle
    finally:
        handle.release()


@dataclass
class PromotionMaintenanceHandle:
    """Exclusive promotion/maintenance authority.

    Holds non-blocking exclusive locks on:
      - promotion.lock (serialize promoters)
      - production turnstile (block new development admission)
      - global pipeline lock (block new heavy work / close start race)
    """

    run_id: str
    shared_root: Path
    promotion: _HeldLock
    turnstile: _HeldLock
    global_lock: _HeldLock
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        for held in (self.global_lock, self.turnstile, self.promotion):
            try:
                fcntl.flock(held.fd, fcntl.LOCK_UN)
                os.close(held.fd)
            except OSError:
                pass
        self.released = True
        _clear_status_if_owner(self.shared_root, self.run_id)


def _refuse_promotion(root: Path, *, reason: str) -> GateError:
    snap = read_gate_status(shared_root=root)
    detail = (
        f"{reason} (gate_state={snap.state}, owning_environment={snap.owning_environment}, "
        f"run_id={snap.run_id}, job_id={snap.job_id}, detail={snap.detail})"
    )
    return GateError(detail)


def acquire_promotion_maintenance(
    *,
    run_id: str,
    shared_root: Path | None = None,
    trigger: str = "promote-to-prod",
) -> PromotionMaintenanceHandle:
    """Acquire non-blocking exclusive promotion/maintenance authority.

    Refuses when development or production is active/waiting, when another
    promotion holds the promotion lock, or when the global pipeline lock is
    busy (prevents a new pipeline starting after a naive status check).
    """
    root = ensure_shared_lock_root(shared_root, environment="prod")
    promotion_path = root / PROMOTION_NAME
    turnstile_path = root / TURNSTILE_NAME
    global_path = root / GLOBAL_NAME

    promo_fd = _open_lock_file(promotion_path)
    try:
        fcntl.flock(promo_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(promo_fd)
        raise _refuse_promotion(
            root, reason="promotion refused: another promotion holds promotion.lock"
        ) from exc

    turn_fd = _open_lock_file(turnstile_path)
    try:
        fcntl.flock(turn_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        try:
            fcntl.flock(promo_fd, fcntl.LOCK_UN)
            os.close(promo_fd)
        except OSError:
            pass
        os.close(turn_fd)
        raise _refuse_promotion(
            root,
            reason="promotion refused: development or production is active or waiting on turnstile",
        ) from exc

    glob_fd = _open_lock_file(global_path)
    try:
        fcntl.flock(glob_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        for fd in (turn_fd, promo_fd):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except OSError:
                pass
        os.close(glob_fd)
        raise _refuse_promotion(
            root,
            reason="promotion refused: global pipeline lock busy (active heavy work or publish)",
        ) from exc

    _write_status(
        root,
        {
            "state": GATE_PROMOTION,
            "owning_environment": "prod",
            "run_id": run_id,
            "pid": os.getpid(),
            "trigger": trigger,
            "stage": "promotion",
            "requested_at": _utc_now_iso(),
            "detail": "promotion holds exclusive maintenance authority",
            "metadata_authoritative": True,
        },
    )
    return PromotionMaintenanceHandle(
        run_id=run_id,
        shared_root=root,
        promotion=_HeldLock(promotion_path, promo_fd, "exclusive"),
        turnstile=_HeldLock(turnstile_path, turn_fd, "exclusive"),
        global_lock=_HeldLock(global_path, glob_fd, "exclusive"),
    )


@contextmanager
def promotion_maintenance(
    *,
    run_id: str,
    shared_root: Path | None = None,
    trigger: str = "promote-to-prod",
) -> Iterator[PromotionMaintenanceHandle]:
    handle = acquire_promotion_maintenance(
        run_id=run_id, shared_root=shared_root, trigger=trigger
    )
    try:
        yield handle
    finally:
        handle.release()
