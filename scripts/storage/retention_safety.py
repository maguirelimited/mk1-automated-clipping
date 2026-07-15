"""Shared path safety helpers for retention apply (Phase 5).

Canonical resolved-path checks only — no string prefix matching.
"""

from __future__ import annotations

from pathlib import Path


def is_under_resolved(path: Path, root: Path) -> bool:
    """Return True when ``path`` is inside ``root`` using resolved paths."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def is_within_any_root(path: Path, roots: dict[str, Path]) -> bool:
    for root in roots.values():
        if is_under_resolved(path, root):
            return True
    return False


def resolve_allowed_roots(
    state_paths,
    allowed_keys: list[str],
    *,
    backups_root: Path | None = None,
) -> dict[str, Path]:
    """Map configured deletion-root keys to absolute resolved paths."""
    mapping: dict[str, Path] = {
        "jobs": state_paths.jobs_root,
        "logs": state_paths.logs_root,
        "reports": state_paths.reports_root,
        "data": state_paths.data_root,
    }
    if backups_root is not None:
        mapping["backups"] = backups_root
    return {
        key: mapping[key].resolve()
        for key in allowed_keys
        if key in mapping
    }
