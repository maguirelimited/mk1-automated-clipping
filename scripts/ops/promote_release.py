#!/usr/bin/env python3
"""Atomic versioned production promotion (Prompt 5).

Canonical entrypoint: deploy/scripts/promote-to-prod.sh → this module.

Layout under MK04_PROD_BASE (default /opt/mk04/prod):
  current -> releases/<release_id>
  previous -> releases/<previous_release_id>
  releases/<release_id>/
  dependency-bundles/<dependency_hash>/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

OPS_DIR = Path(__file__).resolve().parent
REPO_ROOT = OPS_DIR.parents[1]
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

from execution_gate import (  # noqa: E402
    GateError,
    acquire_promotion_maintenance,
    production_installation_present,
    resolve_shared_lock_root,
)

PROMOTER_VERSION = "1.0.0"
MANIFEST_SCHEMA = 1
DEFAULT_RETAIN = 4  # current + previous + 2 older
DEFAULT_PROD_BASE = Path("/opt/mk04/prod")

# Component dependency definitions: (bundle_name, requirements_relpath, venv_relpath)
COMPONENT_DEPS: tuple[tuple[str, str, str], ...] = (
    ("source-input", "source-input/input_service/requirements.txt", "source-input/input_service/.venv"),
    ("video-automation", "video-automation/requirements-dev.txt", "video-automation/.venv"),
    ("output-funnel", "output-funnel/requirements.txt", "output-funnel/.venv"),
    ("ops-ui", "ops-ui/requirements.txt", "ops-ui/.venv"),
    ("ai-service", "ai-service/requirements.txt", "ai-service/.venv"),
)

RSYNC_EXCLUDES: tuple[str, ...] = (
    ".git/",
    ".cursor/",
    ".DS_Store",
    "**/.venv/",
    "**/__pycache__/",
    ".pytest_cache/",
    "**/.pytest_cache/",
    "*.pyc",
    "*.pyo",
    "*.log",
    "*.ndjson",
    "*.sqlite3",
    "*.sqlite",
    "*.db",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "logs/",
    "**/logs/",
    "log/",
    "**/log/",
    "tmp/",
    "temp/",
    "**/tmp/",
    "**/temp/",
    "uploads/",
    "downloads/",
    "credentials/",
    "**/uploads/",
    "**/downloads/",
    "**/credentials/",
    "**/n8n_data/",
    "**/binaryData/",
    "source-input/input_service/data/",
    "source-input/input_service/run*.json",
    "video-automation/input/",
    "video-automation/output/",
    "video-automation/jobs/",
    "video-automation/temp/",
    "video-automation/analytics/",
    "output-funnel/data/",
    "ops-ui/data/",
    "coverage/",
    "htmlcov/",
    ".coverage",
    "**/.mypy_cache/",
    "**/.ruff_cache/",
    "**/node_modules/",
    "jobs/",
    "outputs/",
    "data/",
    "reports/",
    "backups/",
    "database/",
    ".mk04_locks/",
    "**/dependency-bundles/",
    "**/releases/",
)

SECRET_KEY_RE = re.compile(
    r"(password|secret|token|api[_-]?key|credential|private[_-]?key)",
    re.IGNORECASE,
)


class PromoteError(RuntimeError):
    """Promotion failed; current must remain unchanged unless rollback ran."""

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


@dataclass
class GitSnapshot:
    commit: str
    short_commit: str
    dirty: bool
    dirty_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)


@dataclass
class PromoteOptions:
    source_root: Path
    prod_base: Path
    require_clean: bool = False
    no_restart: bool = False
    full_tests: bool = False
    dry_run: bool = False
    retain_releases: int = DEFAULT_RETAIN
    allow_first_bootstrap: bool = False
    shared_lock_root: Path | None = None
    python_bin: Path | None = None
    # Test/injection hooks (None → real implementations).
    validate_fn: Callable[["PromoteContext"], dict[str, Any]] | None = None
    prepare_deps_fn: Callable[["PromoteContext", str], Path] | None = None
    restart_fn: Callable[["PromoteContext"], dict[str, Any]] | None = None
    health_fn: Callable[["PromoteContext"], dict[str, Any]] | None = None
    auth_fn: Callable[[], None] | None = None
    publish_check_fn: Callable[["PromoteContext"], None] | None = None
    services_installed_fn: Callable[["PromoteContext"], bool] | None = None
    snapshot_fn: Callable[[Path, Path], None] | None = None


@dataclass
class PromoteContext:
    options: PromoteOptions
    release_id: str
    run_id: str
    git: GitSnapshot
    staging_dir: Path | None = None
    release_dir: Path | None = None
    # Fixed orchestration root for restart/health for the whole activate/rollback
    # transaction. Set to the finalized candidate before switching ``current``.
    # Never re-resolved via mutable ``current``.
    orchestration_root: Path | None = None
    dependency_hash: str | None = None
    dependency_bundle: Path | None = None
    previous_current: str | None = None
    manifest: dict[str, Any] = field(default_factory=dict)
    outcome: str = "incomplete"


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _utc_stamp() -> str:
    return _utc_now().strftime("%Y%m%dT%H%M%SZ")


def _utc_iso() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


def prod_layout(base: Path) -> dict[str, Path]:
    base = base.expanduser().resolve()
    return {
        "base": base,
        "releases": base / "releases",
        "current": base / "current",
        "previous": base / "previous",
        "dependency_bundles": base / "dependency-bundles",
        "status": base / "last_promotion_status.json",
    }


def capture_git_snapshot(source_root: Path) -> GitSnapshot:
    def _run(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(source_root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    if not (source_root / ".git").exists() and _run("rev-parse", "--is-inside-work-tree") != "true":
        return GitSnapshot(commit="unknown", short_commit="unknown", dirty=False)

    commit = _run("rev-parse", "HEAD") or "unknown"
    short = _run("rev-parse", "--short", "HEAD") or "unknown"
    status = _run("status", "--porcelain")
    dirty_files: list[str] = []
    untracked: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if line.startswith("??"):
            untracked.append(path)
        else:
            dirty_files.append(path)
    dirty = bool(dirty_files or untracked)
    return GitSnapshot(
        commit=commit,
        short_commit=short,
        dirty=dirty,
        dirty_files=dirty_files[:200],
        untracked_files=untracked[:200],
    )


def make_release_id(git: GitSnapshot) -> str:
    suffix = "_dirty" if git.dirty else ""
    return f"{_utc_stamp()}_{git.short_commit}{suffix}"


def compute_dependency_hash(tree: Path) -> str:
    digest = hashlib.sha256()
    for name, req_rel, _venv in COMPONENT_DEPS:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        path = tree / req_rel
        if not path.is_file():
            raise PromoteError(f"missing dependency file for {name}: {req_rel}")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:20]


def _assert_under(path: Path, root: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except OSError as exc:
        raise PromoteError(f"cannot resolve {label}: {exc}") from exc
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PromoteError(
            f"{label} must resolve under {root_resolved}, got {resolved}"
        ) from exc
    return resolved


def read_symlink_target(link: Path) -> Path | None:
    if not link.exists() and not link.is_symlink():
        return None
    if not link.is_symlink():
        raise PromoteError(
            f"{link} is a real directory (legacy flat deploy). "
            "Refuse to overwrite or delete it. Migrate manually: move the tree to "
            f"releases/<legacy_id>/ then create current -> releases/<legacy_id>."
        )
    return Path(os.readlink(link))


def resolve_release_target(link: Path, releases_root: Path) -> Path | None:
    raw = read_symlink_target(link)
    if raw is None:
        return None
    target = raw if raw.is_absolute() else (link.parent / raw)
    return _assert_under(target, releases_root, label=str(link))


def atomic_symlink_replace(link_path: Path, target: Path) -> None:
    """Create/replace symlink atomically on the same filesystem."""
    link_path.parent.mkdir(parents=True, exist_ok=True)
    # Prefer relative targets when both share the same parent base.
    try:
        rel = os.path.relpath(target, start=link_path.parent)
        link_target = rel
    except ValueError:
        link_target = str(target)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{link_path.name}.", dir=str(link_path.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.unlink()
        os.symlink(link_target, tmp_path)
        os.replace(tmp_path, link_path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def rsync_snapshot(source: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if shutil.which("rsync"):
        cmd = ["rsync", "-a", "--delete"]
        for pattern in RSYNC_EXCLUDES:
            cmd.extend(["--exclude", pattern])
        cmd.extend([f"{source}/", f"{dest}/"])
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return
        # Fall through to Python copy on rsync failure (e.g. restricted sandbox).
    _python_snapshot(source, dest)


def _path_is_excluded(rel: Path) -> bool:
    parts = rel.parts
    name = rel.name
    text = rel.as_posix()
    if name in {".git", ".cursor", ".DS_Store", ".pytest_cache", ".coverage", ".mk04_locks"}:
        return True
    if name.endswith((".pyc", ".pyo", ".log", ".ndjson", ".sqlite3", ".sqlite", ".db")):
        return True
    if name == ".env" or name.startswith(".env."):
        return True
    if name in {
        "logs",
        "log",
        "tmp",
        "temp",
        "uploads",
        "downloads",
        "credentials",
        "coverage",
        "htmlcov",
        "node_modules",
        "__pycache__",
        ".venv",
        ".mypy_cache",
        ".ruff_cache",
        "jobs",
        "outputs",
        "data",
        "reports",
        "backups",
        "database",
        "dependency-bundles",
        "releases",
        "n8n_data",
        "binaryData",
    }:
        return True
    if "source-input/input_service/data" in text:
        return True
    if text.startswith("source-input/input_service/run") and text.endswith(".json"):
        return True
    for prefix in (
        "video-automation/input",
        "video-automation/output",
        "video-automation/jobs",
        "video-automation/temp",
        "video-automation/analytics",
        "output-funnel/data",
        "ops-ui/data",
    ):
        if text == prefix or text.startswith(prefix + "/"):
            return True
    if any(part in {".venv", "__pycache__", "node_modules"} for part in parts):
        return True
    return False


def _python_snapshot(source: Path, dest: Path) -> None:
    """Same-filesystem-safe copy used when rsync is unavailable/restricted."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*"):
        rel = path.relative_to(source)
        if any(_path_is_excluded(Path(*rel.parts[: i + 1])) for i in range(len(rel.parts))):
            continue
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_symlink():
            continue
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


# ---------------------------------------------------------------------------
# Service-readability contract for shipped release trees
# ---------------------------------------------------------------------------
#
# Snapshot/rsync preserve restrictive checkout modes (e.g. 0600). systemd
# services run as mk04:mk04 and must traverse release directories and read
# non-secret code/config. Normalize modes on the staging/release tree only —
# never chmod the operator source checkout.
#
# Contract (exact-path chmod; no world-write):
#   directories:  0o0755
#   regular files: 0o0644
#   executables:   0o0755  (retain execute when source had any +x, *.sh, or #!)
RELEASE_DIR_MODE = 0o0755
RELEASE_FILE_MODE = 0o0644
RELEASE_EXEC_MODE = 0o0755

SERVICE_ROOTS = (
    "ai-service",
    "ops-ui",
    "source-input",
    "video-automation",
    "output-funnel",
)


def _should_remain_executable(path: Path, current_mode: int) -> bool:
    if current_mode & 0o111:
        return True
    if path.suffix == ".sh":
        return True
    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def normalize_release_tree_permissions(root: Path) -> list[str]:
    """Normalize shipped code/config modes under *root* for mk04 service reads.

    Mutates only paths beneath ``root`` (staging/release). Does not touch the
    promotion source checkout.
    """
    if not root.is_dir():
        raise PromoteError(f"cannot normalize permissions; missing tree: {root}")

    notes: list[str] = []
    changed = 0
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts)):
        if path.is_symlink():
            continue
        try:
            st = path.lstat()
        except OSError as exc:
            raise PromoteError(f"cannot stat release path {path}: {exc}") from exc

        if stat.S_ISDIR(st.st_mode):
            desired = RELEASE_DIR_MODE
        elif stat.S_ISREG(st.st_mode):
            desired = (
                RELEASE_EXEC_MODE
                if _should_remain_executable(path, st.st_mode)
                else RELEASE_FILE_MODE
            )
        else:
            continue

        # Never allow world-write.
        desired &= ~0o0002
        current = stat.S_IMODE(st.st_mode)
        if current == desired:
            continue
        try:
            os.chmod(path, desired)
        except OSError as exc:
            raise PromoteError(
                f"cannot normalize permissions on {path}: {exc}"
            ) from exc
        changed += 1

    notes.append(
        f"normalized release permissions under {root} "
        f"({changed} paths; dirs={oct(RELEASE_DIR_MODE)} "
        f"files={oct(RELEASE_FILE_MODE)} exec={oct(RELEASE_EXEC_MODE)})"
    )
    return notes


def _mode_world_writable(mode: int) -> bool:
    return bool(mode & 0o0002)


def _mode_service_traversable(mode: int) -> bool:
    """Group or other execute — owner-only +x is insufficient for mk04 on root-owned trees."""
    return bool(mode & 0o0011)


def _mode_service_readable(mode: int) -> bool:
    """Group or other read — owner-only 0600 is insufficient for mk04 on root-owned files."""
    return bool(mode & 0o0044)


def verify_release_service_readability(root: Path) -> None:
    """Fail closed if mk04 cannot traverse/read required shipped release paths."""
    if not root.is_dir():
        raise PromoteError(f"release tree missing for readability check: {root}")

    errors: list[str] = []
    for name in SERVICE_ROOTS:
        service_root = root / name
        if not service_root.is_dir():
            errors.append(f"missing service root: {name}")
            continue
        try:
            st = service_root.stat()
        except OSError as exc:
            errors.append(f"unstatable service root {name}: {exc}")
            continue
        mode = stat.S_IMODE(st.st_mode)
        if _mode_world_writable(mode):
            errors.append(f"world-writable directory: {name}")
        if not _mode_service_traversable(mode):
            errors.append(f"service cannot traverse directory: {name} mode={oct(mode)}")

    # Walk shipped trees (skip symlinks such as .venv → dependency-bundles).
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        # Skip test/tmp junk that may ride along in dirty trees.
        if rel.startswith((".tmp_", ".git/", "__pycache__/")) or "/__pycache__/" in rel:
            continue
        try:
            st = path.lstat()
        except OSError as exc:
            errors.append(f"unstatable path {rel}: {exc}")
            continue
        mode = stat.S_IMODE(st.st_mode)
        if _mode_world_writable(mode):
            errors.append(f"world-writable path: {rel} mode={oct(mode)}")
            continue
        if stat.S_ISDIR(st.st_mode):
            if not _mode_service_traversable(mode):
                errors.append(f"directory not traversable by service: {rel} mode={oct(mode)}")
        elif stat.S_ISREG(st.st_mode):
            if not _mode_service_readable(mode):
                errors.append(f"file not readable by service: {rel} mode={oct(mode)}")
            # Spot-check open for required AI registry when present.
            if rel == "ai-service/config/funnel_rule_registry.json":
                try:
                    with path.open("rb") as handle:
                        handle.read(1)
                except OSError as exc:
                    errors.append(f"AI registry open failed: {rel} ({exc})")

    if errors:
        preview = "; ".join(errors[:8])
        more = f" (+{len(errors) - 8} more)" if len(errors) > 8 else ""
        raise PromoteError(
            "release service-readability contract failed before activation: "
            f"{preview}{more}"
        )


def _scrub_secrets(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if SECRET_KEY_RE.search(str(key)):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = _scrub_secrets(value)
        return out
    if isinstance(obj, list):
        return [_scrub_secrets(item) for item in obj]
    return obj


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scrubbed = _scrub_secrets(dict(payload))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(scrubbed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_manifest(release_dir: Path, manifest: Mapping[str, Any]) -> None:
    write_json_atomic(release_dir / "release_manifest.json", manifest)


def find_python(source_root: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit
    for candidate in (
        source_root / "video-automation" / ".venv" / "bin" / "python",
        source_root / ".venv" / "bin" / "python",
        Path(sys.executable),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise PromoteError("no usable Python interpreter found for promotion validation")


def default_prepare_dependency_bundle(ctx: PromoteContext, dep_hash: str) -> Path:
    """Create or reuse an immutable dependency bundle; symlink into staging."""
    assert ctx.staging_dir is not None
    layout = prod_layout(ctx.options.prod_base)
    bundles = layout["dependency_bundles"]
    bundle = bundles / dep_hash
    complete = bundle / "BUNDLE_COMPLETE"
    if complete.is_file():
        _link_bundle_into_tree(ctx.staging_dir, bundle)
        return bundle

    staging_bundle = bundles / f".staging-{dep_hash}"
    if staging_bundle.exists():
        shutil.rmtree(staging_bundle)
    staging_bundle.mkdir(parents=True, exist_ok=True)

    python = find_python(ctx.options.source_root, ctx.options.python_bin)
    for name, req_rel, _venv_rel in COMPONENT_DEPS:
        component_venv = staging_bundle / name
        req = ctx.staging_dir / req_rel
        if not req.is_file():
            raise PromoteError(f"staging missing requirements for {name}: {req_rel}")
        subprocess.run([str(python), "-m", "venv", str(component_venv)], check=True)
        pip = component_venv / "bin" / "pip"
        subprocess.run([str(pip), "install", "--upgrade", "pip"], check=True)
        result = subprocess.run(
            [str(pip), "install", "-r", str(req)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise PromoteError(
                f"dependency install failed for {name}: {result.stderr.strip()[:500]}"
            )
        # Smoke import using the new venv.
        vpy = component_venv / "bin" / "python"
        smoke = subprocess.run(
            [str(vpy), "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            text=True,
            check=False,
        )
        if smoke.returncode != 0:
            raise PromoteError(f"dependency smoke failed for {name}")

    (staging_bundle / "identity.json").write_text(
        json.dumps({"dependency_hash": dep_hash, "created_at": _utc_iso()}, indent=2) + "\n",
        encoding="utf-8",
    )
    complete_tmp = staging_bundle / "BUNDLE_COMPLETE"
    complete_tmp.write_text(_utc_iso() + "\n", encoding="utf-8")
    if bundle.exists():
        raise PromoteError(f"dependency bundle race: {bundle} already exists")
    os.rename(staging_bundle, bundle)
    _link_bundle_into_tree(ctx.staging_dir, bundle)
    return bundle


def _link_bundle_into_tree(tree: Path, bundle: Path) -> None:
    for name, _req, venv_rel in COMPONENT_DEPS:
        link = tree / venv_rel
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            if link.is_symlink() or link.is_file():
                link.unlink()
            else:
                shutil.rmtree(link)
        target = bundle / name
        if not target.is_dir():
            raise PromoteError(f"dependency bundle missing component {name}: {target}")
        rel = os.path.relpath(target, start=link.parent)
        os.symlink(rel, link)


def _sanitize_validation_text(text: str, *, limit: int = 2000) -> str:
    """Bound and redact secret-like assignments; never return unlimited output."""
    scrubbed = re.sub(
        r"(?i)\b([A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|API[_-]?KEY|CREDENTIAL|PRIVATE[_-]?KEY)[A-Z0-9_]*)\s*=\s*\S+",
        r"\1=***",
        text or "",
    )
    scrubbed = re.sub(
        r"(?i)\b(authorization\s*:?\s*bearer|bearer)\s+\S+",
        r"\1 ***",
        scrubbed,
    )
    if len(scrubbed) > limit:
        return scrubbed[-limit:]
    return scrubbed


def _extract_pytest_failure_node(stdout: str, stderr: str) -> str | None:
    blob = f"{stdout}\n{stderr}"
    for pattern in (
        r"^FAILED\s+(\S+)",
        r"^ERROR\s+(\S+)",
        r" gar (\S+::\S+)\s+FAILED",
    ):
        match = re.search(pattern, blob, re.MULTILINE)
        if match:
            return match.group(1)
    return None


def _hermetic_validation_command_env(
    workspace: Path,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build env for one validation command: never inherit live lock roots."""
    lock_root = workspace / "locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    # Probe path for env.sh deployed-lock detection — absent/unwritable in hermetic runs.
    deployed_probe = workspace / "deployed_locks_absent"
    pycache = workspace / "pycache"
    pycache.mkdir(parents=True, exist_ok=True)
    cache_dir = workspace / "pytest_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    run_env = os.environ.copy()
    # Explicit overrides — must replace any inherited live /var/lib/mk04/locks.
    run_env["MK04_SHARED_LOCK_ROOT"] = str(lock_root)
    run_env["MK04_DEPLOYED_LOCK_ROOT"] = str(deployed_probe)
    run_env.pop("MK04_PRODUCTION_INSTALLED", None)
    run_env["MK04_ENV"] = "dev"
    run_env["MK04_SKIP_PROD_PREFLIGHT"] = "1"
    run_env["PYTHONDONTWRITEBYTECODE"] = "1"
    run_env["PYTHONPYCACHEPREFIX"] = str(pycache)
    # Keep pytest from writing into immutable staging trees.
    run_env["PYTEST_ADDOPTS"] = (
        f"--override-ini cache_dir={cache_dir} -p no:cacheprovider"
    )
    if extra:
        run_env.update({k: str(v) for k, v in extra.items()})
        # Re-assert hermetic lock roots after extra merges.
        run_env["MK04_SHARED_LOCK_ROOT"] = str(lock_root)
        run_env["MK04_DEPLOYED_LOCK_ROOT"] = str(deployed_probe)
    return run_env


def default_validate(ctx: PromoteContext) -> dict[str, Any]:
    """Run promotion validation against the staging snapshot (not live current)."""
    assert ctx.staging_dir is not None
    staging = ctx.staging_dir
    python = find_python(ctx.options.source_root, ctx.options.python_bin)
    results: dict[str, Any] = {
        "commands": [],
        "ok": True,
        "hermetic_lock_root": True,
    }
    workspace = Path(
        tempfile.mkdtemp(prefix="mk04-promote-validate-", dir=str(Path(tempfile.gettempdir())))
    )
    results["validation_workspace"] = str(workspace)

    def _run(label: str, cmd: Sequence[str], *, env: dict[str, str] | None = None) -> None:
        run_env = _hermetic_validation_command_env(workspace, extra=env)
        # Guardrail: never point validation at the live deployed lock root.
        lock_val = run_env.get("MK04_SHARED_LOCK_ROOT", "")
        if lock_val in {"/var/lib/mk04/locks", str(Path("/var/lib/mk04/locks"))}:
            raise PromoteError(
                "internal error: validation attempted to use live lock root",
                detail={"label": label},
            )
        if "var/lib/mk04/locks" in lock_val.replace("\\", "/"):
            raise PromoteError(
                "internal error: validation lock root looks like a live runtime path",
                detail={"label": label},
            )
        proc = subprocess.run(
            list(cmd),
            cwd=str(staging),
            env=run_env,
            capture_output=True,
            text=True,
            check=False,
        )
        stdout_tail = _sanitize_validation_text(proc.stdout or "")
        stderr_tail = _sanitize_validation_text(proc.stderr or "")
        # Record command labels and argv shape without env dumps or secret values.
        safe_cmd = [str(c) for c in cmd]
        entry: dict[str, Any] = {
            "label": label,
            "command": safe_cmd,
            "returncode": proc.returncode,
            "ok": proc.returncode == 0,
        }
        results["commands"].append(entry)
        if proc.returncode != 0:
            failing_node = _extract_pytest_failure_node(proc.stdout or "", proc.stderr or "")
            failure = {
                "label": label,
                "returncode": proc.returncode,
                "failing_node": failing_node,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
            }
            results["ok"] = False
            results["failure"] = failure
            raise PromoteError(
                f"validation failed: {label}",
                detail={"validation": results, "failure": failure},
            )

    try:
        # Syntax/import checks for service entrypoints using staging trees.
        import_checks = [
            (
                "ops-ui import",
                [str(python), "-c", "import ops_ui; from ops_ui.app import create_app"],
                {"PYTHONPATH": str(staging / "ops-ui")},
            ),
            (
                "execution_gate import",
                [str(python), "-c", "import execution_gate"],
                {"PYTHONPATH": str(staging / "scripts" / "ops")},
            ),
        ]
        for label, cmd, env in import_checks:
            _run(label, cmd, env=env)

        cache_dir = workspace / "pytest_cache"
        pytest_base = [
            str(python),
            "-m",
            "pytest",
            "--override-ini",
            f"cache_dir={cache_dir}",
            "-p",
            "no:cacheprovider",
            "-q",
        ]
        _run(
            "tests/config",
            [*pytest_base, "tests/config"],
            env={
                "PYTHONPATH": str(staging / "scripts" / "config")
                + os.pathsep
                + str(staging / "scripts" / "ops")
            },
        )
        _run(
            "ops-ui/tests",
            [*pytest_base, "ops-ui/tests"],
            env={"PYTHONPATH": str(staging / "ops-ui")},
        )
        _run(
            "prompt2 upload authority",
            [*pytest_base, "output-funnel/tests/test_upload_authority.py"],
            env={"PYTHONPATH": str(staging / "output-funnel")},
        )
        _run(
            "prompt3 runtime paths",
            [*pytest_base, "tests/config/test_runtime_paths.py"],
            env={
                "PYTHONPATH": str(staging / "scripts" / "config")
                + os.pathsep
                + str(staging / "scripts" / "ops")
            },
        )
        _run(
            "prompt4 execution gate",
            [
                *pytest_base,
                "tests/ops/test_execution_gate.py",
                "tests/ops/test_execution_lock.py",
                "tests/ops/test_prompt4_repair.py",
            ],
            env={"PYTHONPATH": str(staging / "scripts" / "ops")},
        )
        if ctx.options.full_tests:
            _run(
                "video-automation/tests",
                [*pytest_base, "video-automation/tests"],
                env={"PYTHONPATH": str(staging / "video-automation")},
            )
        return results
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def default_services_installed(ctx: PromoteContext) -> bool:
    if ctx.options.services_installed_fn is not None:
        return ctx.options.services_installed_fn(ctx)
    # Heuristic: systemd units present. Never invent success.
    try:
        proc = subprocess.run(
            ["systemctl", "list-unit-files", "mk04-ops-ui.service"],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode == 0 and "mk04-ops-ui.service" in proc.stdout
    except OSError:
        return False


def default_publish_check(ctx: PromoteContext) -> None:
    """Refuse when a real publish appears in-flight (best-effort, mockable)."""
    if ctx.options.publish_check_fn is not None:
        ctx.options.publish_check_fn(ctx)
        return
    # Without a live prod DB we cannot see uploads; global lock already held.
    # Optional probe: if MK04_RUNTIME_ROOT points at a prod DB, scan UPLOADING.
    runtime = (os.environ.get("MK04_RUNTIME_ROOT") or "").strip()
    if not runtime:
        return
    db = Path(runtime) / "output-funnel" / "output_funnel.sqlite3"
    if not db.is_file():
        # Common alternate path under data root.
        alt = Path(runtime) / ".." / ".."  # unused placeholder
        _ = alt
        return
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM upload_jobs WHERE status IN ('uploading', 'publishing')"
            ).fetchone()
        finally:
            con.close()
        if row and int(row[0]) > 0:
            raise PromoteError(
                f"refuse promotion: {row[0]} active upload/publish job(s) in {db}"
            )
    except PromoteError:
        raise
    except Exception:
        # DB schema may differ; do not block solely on probe errors.
        return


def pinned_orchestration_root(ctx: PromoteContext) -> Path:
    """Return the fixed restart/health implementation root for this promote.

    Prefer the finalized candidate release. Fall back to the source checkout only
    when a release tree is not yet available (should not happen after finalize).
    """
    if ctx.orchestration_root is not None:
        return ctx.orchestration_root.expanduser().resolve()
    if ctx.release_dir is not None:
        return ctx.release_dir.expanduser().resolve()
    return ctx.options.source_root.expanduser().resolve()


def ensure_restart_authorization(ctx: PromoteContext | None = None) -> None:
    """Establish sudo/systemctl privilege once before activation.

    Fails closed before switching ``current`` when authorization cannot be
    obtained. Uses the existing host sudo model only (no sudoers/Polkit edits).
    Imports the helper from the pinned orchestration root when available.
    """
    ops_dir = (
        pinned_orchestration_root(ctx) / "scripts" / "ops"
        if ctx is not None
        else Path(__file__).resolve().parent
    )
    if str(ops_dir) not in sys.path:
        sys.path.insert(0, str(ops_dir))
    from restart_service import (  # noqa: PLC0415
        AuthorizationError,
        ensure_systemctl_authorization,
    )

    try:
        ensure_systemctl_authorization(interactive=True)
    except AuthorizationError as exc:
        raise PromoteError(
            "refusing to activate release: cannot authorize production service "
            f"restart ({exc})"
        ) from exc


def default_restart(ctx: PromoteContext) -> dict[str, Any]:
    """Batched privileged restart using the pinned orchestration root.

    Uses ``--skip-health`` so an intentional Overall WARN (e.g. uploads disabled)
    cannot be mistaken for a systemd restart failure. Promotion runs a separate
    boot-readiness check afterward.
    """
    root = pinned_orchestration_root(ctx)
    script = root / "scripts" / "ops" / "restart.sh"
    if not script.is_file():
        return {
            "ok": False,
            "detail": f"restart.sh missing in pinned orchestration root: {root}",
            "orchestration_root": str(root),
        }
    # Authorization must already be cached (ensure_restart_authorization before
    # switch). restart.sh batches units into one privileged systemctl call.
    proc = subprocess.run(
        ["bash", str(script), "prod", "all", "--confirm", "--skip-health"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "orchestration_root": str(root),
        "restart_only": True,
        "stdout": (proc.stdout or "")[-2000:],
        "stderr": (proc.stderr or "")[-2000:],
    }


def evaluate_boot_readiness_result(
    *,
    returncode: int | None,
    stdout: str,
    stderr: str = "",
) -> dict[str, Any]:
    """Interpret ``health.sh --boot-readiness`` / boot contract for promotion.

    Success: Boot readiness READY (exit 0 or 1 with optional warnings).
    Failure: NOT READY (exit 2), missing/malformed boot result, or unexpected code.
    Does not treat Overall WARN alone as failure.
    """
    text = f"{stdout or ''}\n{stderr or ''}"
    boot_line = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("boot readiness"):
            boot_line = stripped
            break

    ready = "READY" in boot_line.upper() and "NOT READY" not in boot_line.upper()
    not_ready = "NOT READY" in boot_line.upper()

    if returncode is None:
        return {
            "ok": False,
            "boot_readiness": "unknown",
            "detail": "missing readiness return code",
            "boot_line": boot_line,
        }
    if returncode == 2 or not_ready:
        return {
            "ok": False,
            "boot_readiness": "NOT READY",
            "detail": boot_line or "boot readiness NOT READY",
            "boot_line": boot_line,
            "returncode": returncode,
        }
    if returncode in (0, 1) and ready:
        return {
            "ok": True,
            "boot_readiness": "READY",
            "detail": boot_line or "boot readiness READY",
            "boot_line": boot_line,
            "returncode": returncode,
            "optional_warnings": returncode == 1,
        }
    # Fail closed: cannot confirm READY.
    return {
        "ok": False,
        "boot_readiness": "unknown",
        "detail": (
            f"malformed or missing boot readiness result "
            f"(exit={returncode}, boot_line={boot_line!r})"
        ),
        "boot_line": boot_line,
        "returncode": returncode,
    }


def default_health(ctx: PromoteContext) -> dict[str, Any]:
    """Bounded boot-readiness verification via the pinned orchestration root.

    Uses ``health.sh --boot-readiness`` so the full human report (including
    uploads-disabled WARN) is printed, while success follows Boot readiness READY.
    """
    root = pinned_orchestration_root(ctx)
    script = root / "scripts" / "ops" / "health.sh"
    if not script.is_file():
        return {
            "ok": False,
            "detail": f"health.sh missing in pinned orchestration root: {root}",
            "orchestration_root": str(root),
            "boot_readiness": "unknown",
        }

    import time as _time

    initial_wait = 3
    total_sec = 45
    interval_sec = 3
    _time.sleep(initial_wait)
    deadline = _time.monotonic() + total_sec
    last: dict[str, Any] = {
        "ok": False,
        "returncode": 2,
        "stdout": "",
        "stderr": "health check not run",
        "orchestration_root": str(root),
        "boot_readiness": "unknown",
    }
    while True:
        proc = subprocess.run(
            ["bash", str(script), "prod", "--boot-readiness"],
            capture_output=True,
            text=True,
            check=False,
        )
        evaluated = evaluate_boot_readiness_result(
            returncode=int(proc.returncode),
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )
        last = {
            **evaluated,
            "returncode": int(proc.returncode),
            "stdout": (proc.stdout or "")[-2000:],
            "stderr": (proc.stderr or "")[-2000:],
            "orchestration_root": str(root),
        }
        if last["ok"]:
            return last
        # Fail closed immediately on malformed/missing boot contract.
        if last.get("boot_readiness") == "unknown":
            return last
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            last["detail"] = (
                f"{last.get('detail') or 'boot readiness check failed'}; "
                "readiness retry budget exhausted"
            )
            return last
        _time.sleep(min(interval_sec, remaining))

def prune_releases(layout: dict[str, Path], retain: int, *, keep_failed: Path | None = None) -> list[str]:
    """Keep current, previous, and newest older releases up to retain total."""
    releases_root = layout["releases"]
    if not releases_root.is_dir():
        return []
    protected: set[Path] = set()
    for name in ("current", "previous"):
        try:
            target = resolve_release_target(layout[name], releases_root)
        except PromoteError:
            target = None
        if target is not None:
            protected.add(target.resolve())
    if keep_failed is not None and keep_failed.exists():
        protected.add(keep_failed.resolve())

    candidates = sorted(
        [p for p in releases_root.iterdir() if p.is_dir() and not p.name.startswith(".")],
        key=lambda p: p.name,
        reverse=True,
    )
    keep_set: set[Path] = set(protected)
    for path in candidates:
        if path.resolve() in keep_set:
            continue
        if len(keep_set) >= max(2, retain):
            break
        keep_set.add(path.resolve())

    warnings: list[str] = []
    for path in candidates:
        if path.resolve() in keep_set:
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            warnings.append(f"prune failed for {path.name}: {exc}")
    _prune_unused_bundles(layout, keep_set, warnings)
    return warnings


def _prune_unused_bundles(
    layout: dict[str, Path], retained_releases: set[Path], warnings: list[str]
) -> None:
    bundles_root = layout["dependency_bundles"]
    if not bundles_root.is_dir():
        return
    referenced: set[str] = set()
    for release in retained_releases:
        manifest = release / "release_manifest.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        dep = data.get("dependency_hash") or data.get("dependency", {}).get("hash")
        if dep:
            referenced.add(str(dep))
    for bundle in bundles_root.iterdir():
        if not bundle.is_dir() or bundle.name.startswith("."):
            continue
        if bundle.name in referenced:
            continue
        # Keep incomplete staging-like names alone; ignore.
        try:
            shutil.rmtree(bundle)
        except OSError as exc:
            warnings.append(f"bundle prune failed for {bundle.name}: {exc}")


def _init_manifest(ctx: PromoteContext) -> dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA,
        "promoter_version": PROMOTER_VERSION,
        "release_id": ctx.release_id,
        "created_at": _utc_iso(),
        "source_checkout": str(ctx.options.source_root),
        "git_commit": ctx.git.commit,
        "git_short_commit": ctx.git.short_commit,
        "dirty": ctx.git.dirty,
        "dirty_files": list(ctx.git.dirty_files),
        "untracked_files": list(ctx.git.untracked_files),
        "previous_current_release": ctx.previous_current,
        "validation": {},
        "dependency": {},
        "snapshot_status": "pending",
        "finalization_status": "pending",
        "restart_requested": not ctx.options.no_restart,
        "restart_performed": False,
        "activation_result": "pending",
        "rollback": None,
        "upload_mode_unchanged": True,
        "scheduler_unchanged": True,
    }


def promote(options: PromoteOptions) -> dict[str, Any]:
    source = options.source_root.expanduser().resolve()
    layout = prod_layout(options.prod_base)
    base = layout["base"]
    # Refuse promoting from the production tree itself.
    if source == base or source == layout["current"].resolve() or layout["releases"] in source.parents:
        raise PromoteError(f"refusing to promote from production tree: {source}")
    if layout["current"].is_symlink():
        try:
            if source == layout["current"].resolve():
                raise PromoteError(f"refusing to promote from production current: {source}")
        except PromoteError:
            raise
        except OSError:
            pass

    git = capture_git_snapshot(source)
    if options.require_clean and git.dirty:
        raise PromoteError(
            "--require-clean: refusing dirty working tree "
            f"({len(git.dirty_files)} modified, {len(git.untracked_files)} untracked)"
        )

    release_id = make_release_id(git)
    run_id = f"promote-{release_id}"
    ctx = PromoteContext(options=options, release_id=release_id, run_id=run_id, git=git)
    ctx.manifest = _init_manifest(ctx)

    print("================================================================")
    print("mk04 atomic production promotion")
    print(f"source:           {source}")
    print(f"prod base:        {layout['base']}")
    print(f"release_id:       {release_id}")
    print(f"commit:           {git.short_commit} ({git.commit})")
    if git.dirty:
        print(
            f"WARNING: dirty working tree "
            f"({len(git.dirty_files)} modified, {len(git.untracked_files)} untracked)"
        )
    print(f"restart:          {'no' if options.no_restart else 'yes'}")
    print("upload mode:      unchanged")
    print("scheduler:        unchanged")
    print("================================================================")

    if options.dry_run:
        print("dry-run: would snapshot, validate, switch current →", release_id)
        return {"ok": True, "dry_run": True, "release_id": release_id}

    # Shared lock root: before prod install, allow Prompt 4 local resolution.
    shared_root = options.shared_lock_root
    if shared_root is None:
        explicit = (os.environ.get("MK04_SHARED_LOCK_ROOT") or "").strip()
        if explicit:
            shared_root = Path(explicit)
        elif production_installation_present():
            shared_root = resolve_shared_lock_root(allow_dev_fallback=False, environment="prod")
        else:
            shared_root = resolve_shared_lock_root(allow_dev_fallback=True, environment="dev")

    maintenance = None
    try:
        maintenance = acquire_promotion_maintenance(
            run_id=run_id, shared_root=shared_root, trigger="promote-to-prod"
        )
    except GateError as exc:
        raise PromoteError(str(exc)) from exc

    try:
        return _promote_under_maintenance(ctx, layout, maintenance)
    finally:
        if maintenance is not None:
            maintenance.release()


def _promote_under_maintenance(ctx: PromoteContext, layout: dict[str, Path], _maintenance: Any) -> dict[str, Any]:
    options = ctx.options
    source = options.source_root
    releases = layout["releases"]
    releases.mkdir(parents=True, exist_ok=True)
    layout["dependency_bundles"].mkdir(parents=True, exist_ok=True)

    # Detect legacy flat current.
    if layout["current"].exists() and not layout["current"].is_symlink():
        raise PromoteError(
            f"unexpected real directory at {layout['current']}; "
            "refusing to delete or overwrite. Migrate to releases/ first."
        )

    try:
        prev = resolve_release_target(layout["current"], releases)
        ctx.previous_current = prev.name if prev else None
        ctx.manifest["previous_current_release"] = ctx.previous_current
    except PromoteError:
        raise

    # Active publish safety (before switch/restart).
    publish_check = options.publish_check_fn or default_publish_check
    publish_check(ctx)

    staging = releases / f".staging-{ctx.release_id}"
    if staging.exists():
        # Only remove abandoned staging (never an active current target).
        try:
            current_target = resolve_release_target(layout["current"], releases)
        except PromoteError:
            current_target = None
        if current_target is not None and current_target.resolve() == staging.resolve():
            raise PromoteError("staging directory is unexpectedly current; aborting")
        shutil.rmtree(staging)
    ctx.staging_dir = staging

    print(f"[1/7] Snapshot → {staging}")
    snapshot = options.snapshot_fn or rsync_snapshot
    snapshot(source, staging)
    ctx.manifest["snapshot_status"] = "copied"
    # Ensure no secrets/env leaked.
    for rel in (
        ".env",
        "video-automation/.env",
        "ops-ui/.env",
        "output-funnel/.env",
        "source-input/input_service/.env",
    ):
        leaked = staging / rel
        if leaked.exists():
            leaked.unlink()

    print("[1b/7] Normalize release permissions for service readability")
    try:
        for note in normalize_release_tree_permissions(staging):
            print(f"  {note}")
        verify_release_service_readability(staging)
        ctx.manifest["permissions"] = {
            "normalized": True,
            "contract": {
                "dirs": oct(RELEASE_DIR_MODE),
                "files": oct(RELEASE_FILE_MODE),
                "exec": oct(RELEASE_EXEC_MODE),
            },
        }
    except PromoteError as exc:
        ctx.manifest["permissions"] = {"normalized": False, "error": str(exc)}
        ctx.manifest["activation_result"] = "permissions_failed"
        write_manifest(staging, ctx.manifest)
        _write_status_record(layout, ctx, ok=False)
        raise

    print("[2/7] Dependency bundle")
    dep_hash = compute_dependency_hash(staging)
    ctx.dependency_hash = dep_hash
    prepare = options.prepare_deps_fn or default_prepare_dependency_bundle
    try:
        bundle = prepare(ctx, dep_hash)
    except Exception as exc:
        ctx.manifest["dependency"] = {"hash": dep_hash, "status": "failed", "error": str(exc)}
        _write_status_record(layout, ctx, ok=False)
        raise PromoteError(f"dependency preparation failed: {exc}") from exc
    ctx.dependency_bundle = bundle
    ctx.manifest["dependency"] = {
        "hash": dep_hash,
        "bundle": str(bundle),
        "status": "ready",
        "reused": (bundle / "BUNDLE_COMPLETE").is_file(),
    }
    # Verify venv links exist before switch.
    for _name, _req, venv_rel in COMPONENT_DEPS:
        link = staging / venv_rel
        if not link.exists():
            raise PromoteError(f"missing prepared venv link before switch: {venv_rel}")

    print("[3/7] Validate staging snapshot")
    validate = options.validate_fn or default_validate
    try:
        validation = validate(ctx)
    except PromoteError as exc:
        detail = getattr(exc, "detail", None) or {}
        validation_payload = detail.get("validation")
        if isinstance(validation_payload, dict):
            ctx.manifest["validation"] = _scrub_secrets(validation_payload)
        else:
            ctx.manifest["validation"] = {
                "ok": False,
                "error": str(exc),
                "failure": _scrub_secrets(detail.get("failure") or {"label": str(exc)}),
            }
        ctx.manifest["activation_result"] = "validation_failed"
        write_manifest(staging, ctx.manifest)
        _write_status_record(layout, ctx, ok=False)
        # Leave staging for diagnosis; current untouched.
        raise
    ctx.manifest["validation"] = validation
    if not validation.get("ok", False):
        raise PromoteError("validation reported failure")

    final = releases / ctx.release_id
    if final.exists():
        raise PromoteError(f"release id already exists: {ctx.release_id}")
    print(f"[4/7] Finalize staging → releases/{ctx.release_id}")
    os.rename(staging, final)
    ctx.staging_dir = None
    ctx.release_dir = final
    ctx.manifest["finalization_status"] = "finalized"
    ctx.manifest["snapshot_status"] = "finalized"
    # Re-verify after rename; refuse activation if the contract drifted.
    try:
        verify_release_service_readability(final)
    except PromoteError as exc:
        ctx.manifest["permissions"] = {
            **(ctx.manifest.get("permissions") or {}),
            "verified_after_finalize": False,
            "error": str(exc),
        }
        ctx.manifest["activation_result"] = "permissions_failed"
        write_manifest(final, ctx.manifest)
        _write_status_record(layout, ctx, ok=False)
        raise
    write_manifest(final, ctx.manifest)

    # Pin orchestration to the finalized candidate for the entire activate /
    # rollback transaction. Restoring ``current`` must not change helpers.
    ctx.orchestration_root = final.resolve()
    ctx.manifest["orchestration_root"] = str(ctx.orchestration_root)

    old_current = None
    try:
        old_current = resolve_release_target(layout["current"], releases)
    except PromoteError:
        old_current = None

    services_installed = default_services_installed(ctx)
    if not options.no_restart and services_installed:
        print("[4b/7] Establish restart authorization (once)")
        # Real restart path must authorize before switching current. When tests
        # inject restart_fn without auth_fn, skip host privilege (mocked restart).
        if options.auth_fn is not None:
            options.auth_fn()
        elif options.restart_fn is None:
            ensure_restart_authorization(ctx)

    print("[5/7] Atomic switch current")
    atomic_symlink_replace(layout["current"], final)
    if old_current is not None:
        atomic_symlink_replace(layout["previous"], old_current)
        print(f"previous → {old_current.name}")
    else:
        print("previous → (none; first promotion)")

    services_installed = default_services_installed(ctx)
    restart_result: dict[str, Any] = {"ok": True, "skipped": True}
    health_result: dict[str, Any] = {"ok": True, "skipped": True}

    if options.no_restart:
        print("[6/7] Restart skipped (--no-restart)")
        ctx.manifest["restart_performed"] = False
        if not services_installed:
            ctx.manifest["activation_result"] = "bootstrap_required"
            print("Services not installed: bootstrap_required")
        else:
            ctx.manifest["activation_result"] = "activated_no_restart"
    else:
        if not services_installed:
            if not options.allow_first_bootstrap and old_current is None:
                # First promotion without services: require explicit bootstrap flag or --no-restart.
                _rollback_switch(layout, old_current, final, releases)
                raise PromoteError(
                    "production services are not installed. "
                    "Re-run with --no-restart for first bootstrap staging, "
                    "or --allow-first-bootstrap after units are ready."
                )
            if old_current is None:
                ctx.manifest["activation_result"] = "bootstrap_required"
                print("[6/7] No services installed (first bootstrap)")
            else:
                _rollback_switch(layout, old_current, final, releases)
                raise PromoteError(
                    "production services are not installed; refusing to claim operational success"
                )
        else:
            print("[6/7] Restart production services")
            restart = options.restart_fn or default_restart
            health = options.health_fn or default_health
            restart_result = restart(ctx)
            ctx.manifest["restart_performed"] = True
            if not restart_result.get("ok"):
                print("Restart failed — rolling back")
                return _fail_with_rollback(
                    ctx, layout, old_current, final, restart_result, None
                )
            print("[7/7] Boot readiness verification")
            health_result = health(ctx)
            if not health_result.get("ok"):
                detail = (
                    health_result.get("detail")
                    or health_result.get("stdout")
                    or health_result.get("stderr")
                    or ""
                )
                unhealthy = _summarize_unhealthy(str(detail))
                print(f"Boot readiness failed — rolling back{unhealthy}")
                return _fail_with_rollback(
                    ctx, layout, old_current, final, restart_result, health_result
                )
            if health_result.get("optional_warnings") or health_result.get("returncode") == 1:
                print(
                    "Boot readiness READY (optional warnings present; "
                    "uploads/scheduler safety warnings do not block promotion)"
                )
            ctx.manifest["activation_result"] = "activated"

    ctx.manifest["restart"] = restart_result
    ctx.manifest["health"] = health_result
    ctx.manifest["activation_result"] = ctx.manifest.get("activation_result") or "activated"
    write_manifest(final, ctx.manifest)

    warnings = []
    try:
        warnings = prune_releases(layout, options.retain_releases)
    except Exception as exc:  # pragma: no cover - prune must not fail promotion
        warnings.append(f"retention warning: {exc}")
    if warnings:
        print("Retention warnings:")
        for warning in warnings:
            print(f"  - {warning}")

    ctx.outcome = "success"
    status = _write_status_record(layout, ctx, ok=True)
    print("================================================================")
    print("Promotion SUCCESS")
    print(f"current:   {ctx.release_id}")
    print(f"previous:  {ctx.previous_current or '(none)'}")
    print(f"deps:      {ctx.dependency_hash}")
    print(f"status:    {status}")
    print("================================================================")
    return {
        "ok": True,
        "release_id": ctx.release_id,
        "previous": ctx.previous_current,
        "dependency_hash": ctx.dependency_hash,
        "activation_result": ctx.manifest["activation_result"],
        "status_path": str(status),
    }


def _summarize_unhealthy(detail: str) -> str:
    """Extract concise FAIL lines from health output for operator messaging."""
    fails = [
        line.strip()
        for line in (detail or "").splitlines()
        if "FAIL" in line.upper() or line.lower().startswith("overall")
    ]
    if not fails:
        return ""
    joined = "; ".join(fails[:8])
    if len(joined) > 400:
        joined = joined[:397] + "..."
    return f" ({joined})"


def _rollback_switch(
    layout: dict[str, Path],
    old_current: Path | None,
    failed_release: Path,
    releases: Path,
) -> None:
    if old_current is None:
        # First promotion: leave current pointing at failed candidate only if we
        # already switched — caller decides. Prefer removing current symlink.
        if layout["current"].is_symlink():
            target = Path(os.readlink(layout["current"]))
            resolved = target if target.is_absolute() else layout["current"].parent / target
            if resolved.resolve() == failed_release.resolve():
                layout["current"].unlink()
        return
    atomic_symlink_replace(layout["current"], old_current)


def _fail_with_rollback(
    ctx: PromoteContext,
    layout: dict[str, Path],
    old_current: Path | None,
    failed_release: Path,
    restart_result: dict[str, Any] | None,
    health_result: dict[str, Any] | None,
) -> dict[str, Any]:
    rollback: dict[str, Any] = {
        "attempted": True,
        "available": old_current is not None,
        "restored_release": old_current.name if old_current else None,
        "restart_ok": None,
        "health_ok": None,
    }
    if old_current is None:
        rollback["detail"] = "first promotion; automatic rollback unavailable"
        ctx.manifest["rollback"] = rollback
        ctx.manifest["activation_result"] = "failed_no_rollback"
        ctx.manifest["restart"] = restart_result
        ctx.manifest["health"] = health_result
        write_manifest(failed_release, ctx.manifest)
        _write_status_record(layout, ctx, ok=False)
        raise PromoteError(
            "activation failed on first promotion; automatic rollback unavailable. "
            "Failed candidate kept for diagnosis."
        )

    atomic_symlink_replace(layout["current"], old_current)
    # Do not promote failed candidate to previous.
    # Restart/health still use ctx.orchestration_root (finalized candidate), not
    # the restored previous ``current`` tree.
    restart = ctx.options.restart_fn or default_restart
    health = ctx.options.health_fn or default_health
    rb_restart = restart(ctx)
    rb_health = health(ctx) if rb_restart.get("ok") else {"ok": False, "skipped": True}
    rollback["restart_ok"] = bool(rb_restart.get("ok"))
    rollback["health_ok"] = bool(rb_health.get("ok"))
    rollback["orchestration_root"] = str(pinned_orchestration_root(ctx))
    rollback["restart"] = rb_restart
    rollback["health"] = rb_health
    ctx.manifest["rollback"] = rollback
    ctx.manifest["activation_result"] = "rolled_back" if rollback["health_ok"] else "rollback_health_failed"
    ctx.manifest["restart"] = restart_result
    ctx.manifest["health"] = health_result
    write_manifest(failed_release, ctx.manifest)
    _write_status_record(layout, ctx, ok=False)
    if not rollback["health_ok"]:
        rb_detail = ""
        if isinstance(rb_health, dict):
            rb_detail = _summarize_unhealthy(
                str(
                    rb_health.get("detail")
                    or rb_health.get("stdout")
                    or rb_health.get("stderr")
                    or ""
                )
            )
        raise PromoteError(
            "activation failed and rollback boot readiness verification also failed; "
            f"current left at {old_current.name}{rb_detail}"
        )
    raise PromoteError(
        f"activation failed; rolled back to {old_current.name} and verified boot readiness"
    )


def _write_status_record(layout: dict[str, Path], ctx: PromoteContext, *, ok: bool) -> Path:
    validation = ctx.manifest.get("validation")
    validation_failure = None
    if isinstance(validation, Mapping):
        raw_failure = validation.get("failure")
        if isinstance(raw_failure, Mapping):
            validation_failure = _scrub_secrets(dict(raw_failure))
        elif validation.get("ok") is False and validation.get("error"):
            validation_failure = {"label": str(validation.get("error"))}
    payload = {
        "status": "success" if ok else "failure",
        "release_id": ctx.release_id,
        "created_at": _utc_iso(),
        "source_checkout": str(ctx.options.source_root),
        "git_commit": ctx.git.commit,
        "git_short_commit": ctx.git.short_commit,
        "dirty": ctx.git.dirty,
        "activation_result": ctx.manifest.get("activation_result"),
        "dependency_hash": ctx.dependency_hash,
        "previous_current_release": ctx.previous_current,
        "upload_mode_unchanged": True,
        "scheduler_unchanged": True,
        "promoter_version": PROMOTER_VERSION,
        "validation_failure": validation_failure,
        "manifest": _scrub_secrets(ctx.manifest),
    }
    path = layout["status"]
    write_json_atomic(path, payload)
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Atomically promote the development checkout into versioned production releases."
    )
    parser.add_argument(
        "--prod-base",
        default=os.environ.get("MK04_PROD_BASE", str(DEFAULT_PROD_BASE)),
        help="Production base directory (default: /opt/mk04/prod)",
    )
    parser.add_argument(
        "--source",
        default=str(REPO_ROOT),
        help="Source development checkout",
    )
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--full-tests", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retain-releases", type=int, default=DEFAULT_RETAIN)
    parser.add_argument(
        "--allow-first-bootstrap",
        action="store_true",
        help="Allow activation when systemd units are not yet installed (first bootstrap).",
    )
    parser.add_argument(
        "--shared-lock-root",
        default=os.environ.get("MK04_SHARED_LOCK_ROOT") or None,
        help="Override shared lock root (tests / pre-bootstrap).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    options = PromoteOptions(
        source_root=Path(args.source),
        prod_base=Path(args.prod_base),
        require_clean=bool(args.require_clean),
        no_restart=bool(args.no_restart),
        full_tests=bool(args.full_tests),
        dry_run=bool(args.dry_run),
        retain_releases=max(2, int(args.retain_releases)),
        allow_first_bootstrap=bool(args.allow_first_bootstrap),
        shared_lock_root=Path(args.shared_lock_root) if args.shared_lock_root else None,
    )
    try:
        result = promote(options)
    except PromoteError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except GateError as exc:
        print(f"ERROR: gate refused promotion: {exc}", file=sys.stderr)
        return 1
    if not result.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
