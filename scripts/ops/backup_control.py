#!/usr/bin/env python3
"""Environment-scoped operational backup for scripts/ops/backup.sh."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ops_readonly import (  # noqa: E402
    REPO_ROOT,
    canonical_env,
    ensure_config_scripts_on_path,
    env_label,
    mk04_env,
)

# Small operational files only.
INCLUDE_SUFFIXES = {".json", ".yaml", ".yml", ".md", ".txt", ".log", ".db", ".sqlite", ".sqlite3"}
MEDIA_SUFFIXES = {
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".avi",
    ".m4v",
    ".wav",
    ".mp3",
    ".aac",
    ".flac",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
}
EXCLUDED_DIR_NAMES = {
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "cache",
    "clips",
    "renders",
    "temp",
    "tmp",
}
EXCLUDED_NAME_MARKERS = (
    ".env",
    "credentials",
    "oauth",
    "token",
    "private_key",
    "id_rsa",
    "id_ed25519",
)
EXCLUDED_PATTERNS = [
    "*.env",
    ".env*",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "cache/",
    "clips/",
    "source videos / final clips / intermediate renders",
    "private keys / tokens / OAuth credential files",
]
MAX_LOG_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024


@dataclass
class BackupPlan:
    environment: str
    mk04_env: str
    backup_id: str
    backup_dir: Path
    archive_path: Path
    candidates: list[Path] = field(default_factory=list)
    skipped_paths: list[dict[str, str]] = field(default_factory=list)
    included_paths: list[str] = field(default_factory=list)


def resolve_env_paths(canonical: str) -> dict[str, Path]:
    ensure_config_scripts_on_path()
    from config_manager import ConfigManager  # noqa: PLC0415
    from state_paths import EnvironmentStatePaths  # noqa: PLC0415

    resolved = ConfigManager.load(environment=canonical, config_root=REPO_ROOT / "config")
    state = EnvironmentStatePaths.from_resolved_config(resolved)
    token = mk04_env(canonical)
    return {
        "data_root": state.data_root,
        "jobs_root": state.jobs_root,
        "logs_root": state.logs_root,
        "reports_root": state.reports_root,
        "database_path": state.database_path,
        "outputs_root": state.outputs_root,
        "runs_root": REPO_ROOT / "runs" / token,
        "backup_root": REPO_ROOT / "backups" / token,
    }


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_safe_source(path: Path, allowed_roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if path.is_symlink():
        # Allow only if the symlink target stays inside an approved root.
        if not any(_is_under(resolved, root) for root in allowed_roots):
            return False
    return any(_is_under(resolved, root) for root in allowed_roots)


def _name_is_secret_like(name: str) -> bool:
    lowered = name.lower()
    if lowered == ".env" or lowered.startswith(".env.") or lowered.endswith(".env"):
        return True
    return any(marker in lowered for marker in EXCLUDED_NAME_MARKERS)


def _should_skip_dir(name: str) -> bool:
    return name in EXCLUDED_DIR_NAMES or name.startswith(".")


def _should_include_file(path: Path, *, is_log: bool) -> bool:
    name = path.name
    if _name_is_secret_like(name):
        return False
    suffix = path.suffix.lower()
    if suffix in MEDIA_SUFFIXES:
        return False
    if name == "resolved_config.yaml" or name == "resolved_config.yml":
        return True
    if suffix not in INCLUDE_SUFFIXES:
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    limit = MAX_LOG_BYTES if is_log else MAX_FILE_BYTES
    return size <= limit


def _collect_json_tree(root: Path, allowed_roots: list[Path], plan: BackupPlan) -> None:
    if not root.exists():
        plan.skipped_paths.append({"path": _rel(root), "reason": "missing"})
        return
    if not root.is_dir():
        plan.skipped_paths.append({"path": _rel(root), "reason": "not a directory"})
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        current = Path(dirpath)
        if not _is_safe_source(current, allowed_roots):
            dirnames[:] = []
            continue
        for filename in filenames:
            path = current / filename
            if path.is_symlink():
                plan.skipped_paths.append({"path": _rel(path), "reason": "symlink skipped"})
                continue
            if not _should_include_file(path, is_log=False):
                continue
            if not _is_safe_source(path, allowed_roots):
                plan.skipped_paths.append({"path": _rel(path), "reason": "outside approved roots"})
                continue
            plan.candidates.append(path)


def _collect_logs(logs_root: Path, allowed_roots: list[Path], plan: BackupPlan) -> None:
    if not logs_root.exists():
        plan.skipped_paths.append({"path": _rel(logs_root), "reason": "missing"})
        return
    if not logs_root.is_dir():
        plan.skipped_paths.append({"path": _rel(logs_root), "reason": "not a directory"})
        return
    for dirpath, dirnames, filenames in os.walk(logs_root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        current = Path(dirpath)
        if not _is_safe_source(current, allowed_roots):
            dirnames[:] = []
            continue
        for filename in filenames:
            path = current / filename
            if path.is_symlink():
                continue
            if not _should_include_file(path, is_log=True):
                continue
            if not _is_safe_source(path, allowed_roots):
                continue
            plan.candidates.append(path)


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return str(path)


def build_backup_plan(mk04_env_token: str, *, now: datetime | None = None) -> BackupPlan:
    canonical = canonical_env(mk04_env_token)
    token = mk04_env(canonical)
    paths = resolve_env_paths(canonical)
    stamp = (now or datetime.now(UTC)).replace(microsecond=0)
    backup_id = f"backup_{token}_{stamp.strftime('%Y-%m-%dT%H%M%SZ')}"
    backup_dir = paths["backup_root"]
    plan = BackupPlan(
        environment=canonical,
        mk04_env=token,
        backup_id=backup_id,
        backup_dir=backup_dir,
        archive_path=backup_dir / f"{backup_id}.tar.gz",
    )

    allowed_roots = [
        paths["data_root"],
        paths["jobs_root"],
        paths["logs_root"],
        paths["reports_root"],
        paths["runs_root"],
        paths["database_path"].parent,
    ]

    # Explicit small operational files.
    control_state = paths["data_root"] / "control_state.json"
    if control_state.is_file() and _is_safe_source(control_state, allowed_roots):
        plan.candidates.append(control_state)
    else:
        plan.skipped_paths.append(
            {
                "path": _rel(control_state),
                "reason": "missing" if not control_state.exists() else "unsafe or unreadable",
            }
        )

    db_path = paths["database_path"]
    if db_path.is_file() and _is_safe_source(db_path, allowed_roots):
        if db_path.stat().st_size <= MAX_FILE_BYTES * 20:
            plan.candidates.append(db_path)
        else:
            plan.skipped_paths.append({"path": _rel(db_path), "reason": "database too large for small backup"})
    else:
        plan.skipped_paths.append(
            {
                "path": _rel(db_path),
                "reason": "missing" if not db_path.exists() else "unsafe or unreadable",
            }
        )

    # Never include outputs/media by default.
    plan.skipped_paths.append(
        {
            "path": _rel(paths["outputs_root"]),
            "reason": "excluded by default (media/clips/outputs)",
        }
    )

    _collect_json_tree(paths["jobs_root"], allowed_roots, plan)
    _collect_json_tree(paths["runs_root"], allowed_roots, plan)
    _collect_json_tree(paths["reports_root"], allowed_roots, plan)
    _collect_logs(paths["logs_root"], allowed_roots, plan)

    # Deduplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in plan.candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    plan.candidates = unique
    return plan


def create_backup(mk04_env_token: str, *, now: datetime | None = None) -> int:
    plan = build_backup_plan(mk04_env_token, now=now)
    plan.backup_dir.mkdir(parents=True, exist_ok=True)

    created_at = (now or datetime.now(UTC)).replace(microsecond=0)
    manifest: dict[str, Any] = {
        "backup_id": plan.backup_id,
        "environment": plan.mk04_env,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "included_paths": [],
        "skipped_paths": plan.skipped_paths,
        "excluded_patterns": EXCLUDED_PATTERNS,
        "bytes_written": 0,
    }

    # Write archive to a temp file in the backup dir, then replace atomically.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{plan.backup_id}.", suffix=".tar.gz", dir=plan.backup_dir)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with tarfile.open(tmp_path, "w:gz") as archive:
            for path in plan.candidates:
                arcname = _rel(path)
                try:
                    archive.add(path, arcname=arcname, recursive=False)
                except OSError as exc:
                    plan.skipped_paths.append(
                        {"path": arcname, "reason": f"read failed ({exc.__class__.__name__})"}
                    )
                    continue
                manifest["included_paths"].append(arcname)
                plan.included_paths.append(arcname)

            manifest["skipped_paths"] = plan.skipped_paths
            manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
            manifest_info = tarfile.TarInfo(name="manifest.json")
            manifest_info.size = len(manifest_bytes)
            manifest_info.mtime = int(created_at.timestamp())
            archive.addfile(manifest_info, io.BytesIO(manifest_bytes))

        os.replace(tmp_path, plan.archive_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    bytes_written = plan.archive_path.stat().st_size
    # Rewrite manifest inside is already done; update on-disk companion manifest for easy inspection.
    manifest["bytes_written"] = bytes_written
    manifest_path = plan.backup_dir / f"{plan.backup_id}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        "\n".join(
            [
                "Backup created",
                "",
                f"Environment: {env_label(plan.environment)}",
                f"Backup id: {plan.backup_id}",
                f"Archive: {plan.archive_path}",
                f"Files included: {len(manifest['included_paths'])}",
                f"Paths skipped: {len(manifest['skipped_paths'])}",
                f"Bytes written: {bytes_written}",
                "",
                "No files deleted.",
                "No jobs changed.",
                "No uploads triggered.",
            ]
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create environment-scoped operational backup")
    parser.add_argument("environment", help="dev or prod")
    args = parser.parse_args(argv)
    try:
        return create_backup(args.environment)
    except Exception as exc:
        print(f"Error: backup failed ({exc})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
