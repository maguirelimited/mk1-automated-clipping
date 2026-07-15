"""Shared fixtures for Storage Safety & Integration smoke (Phase 12).

Builds isolated repo trees that exercise retention, rotation, backup, and UI
loaders together. Reuses the Phase 4/5 config tree from test_retention_planner.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from config_manager import ConfigManager
from test_retention_planner import (
    FIXED_NOW,
    _build_config_tree,
    _job,
    _touch_age,
    _write,
    _write_bytes,
)

__all__ = [
    "FIXED_NOW",
    "SafetyScenario",
    "build_resolved",
    "make_sqlite_db",
    "populate_safety_scenario",
    "set_schedule",
    "touch_age",
    "usage_percent",
]


def touch_age(path: Path, *, days: float, now: datetime = FIXED_NOW) -> None:
    _touch_age(path, days=days, now=now)


def build_resolved(repo: Path, environment: str = "dev"):
    return ConfigManager.load(
        environment=environment,
        funnel_id="business",
        platform_id="youtube",
        config_root=repo / "config",
    )


def usage_percent(percent: float, *, total: int = 1000) -> SimpleNamespace:
    used = int(total * percent / 100)
    return SimpleNamespace(total=total, used=used, free=total - used)


def set_schedule(
    repo: Path,
    *,
    environment: str,
    enabled: bool,
    mode: str,
    frequency: str = "daily",
    retention_enabled: bool | None = None,
) -> None:
    token = "dev" if environment in {"dev", "development"} else "prod"
    path = repo / "config" / "environments" / f"{token}.yaml"
    text = path.read_text(encoding="utf-8")
    schedule_block = (
        f"  schedule:\n"
        f"    enabled: {str(enabled).lower()}\n"
        f"    mode: {mode}\n"
        f"    frequency: {frequency}\n"
    )
    if re.search(r"^  schedule:\n", text, flags=re.MULTILINE):
        text = re.sub(
            r"^  schedule:\n(?:    .+\n)+",
            schedule_block,
            text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        text = text.rstrip() + "\n" + schedule_block
    if retention_enabled is not None:
        text = re.sub(
            r"(retention:\n\s+enabled:\s*)(true|false)",
            rf"\g<1>{str(retention_enabled).lower()}",
            text,
            count=1,
        )
    path.write_text(text, encoding="utf-8")


def make_sqlite_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany("INSERT INTO items (name) VALUES (?)", [("alpha",), ("beta",)])
        conn.commit()
    finally:
        conn.close()


@dataclass(frozen=True)
class SafetyScenario:
    """Paths and expectations for the comprehensive safety scenario."""

    eligible_temp: Path
    protected_active: Path
    protected_failed: Path
    protected_unknown: Path
    protected_final_clip: Path
    protected_database: Path
    protected_outside_root: Path
    protected_unuploaded: Path
    symlink_link: Path
    symlink_real: Path
    eligible_size: int


def populate_safety_scenario(
    repo: Path,
    *,
    retention_enabled: bool = True,
) -> SafetyScenario:
    """Populate one repo with artifacts covering every hard safety rule."""
    _build_config_tree(repo / "config", retention_enabled=retention_enabled)

    # Eligible: expired temporary file on completed job (dev).
    ok_dir = _job(repo, "dev", "job_ok", status="completed")
    eligible = _write_bytes(ok_dir, "post_processing/tmp/expired.txt", b"delete-me")
    touch_age(eligible, days=5)

    # Active job: same age but running status.
    active_dir = _job(repo, "dev", "job_active", status="running")
    protected_active = _write_bytes(active_dir, "post_processing/tmp/active.txt", b"keep")
    touch_age(protected_active, days=5)

    # Failed job: longer retention window than successful.
    fail_dir = _job(repo, "dev", "job_fail", status="failed")
    protected_failed = _write_bytes(fail_dir, "input_source.mp4", b"fail-video")
    touch_age(protected_failed, days=10)

    # Unknown artifact type.
    unknown_dir = _job(repo, "dev", "job_unknown", status="completed")
    protected_unknown = _write_bytes(unknown_dir, "mystery.bin", b"?")
    touch_age(protected_unknown, days=30)

    # Production final clip default protected.
    clip_dir = _job(repo, "prod", "job_clip", status="completed")
    protected_final_clip = _write_bytes(clip_dir, "clips/final.mp4", b"clip")
    touch_age(protected_final_clip, days=400)

    # Live database always protected.
    protected_database = _write_bytes(repo, "database/dev.db", b"sqlite-header")
    touch_age(protected_database, days=400)

    # Outside allowed delete root.
    protected_outside_root = _write(repo, "runs/dev/run_1/run_record.json", "{}")
    touch_age(protected_outside_root, days=400)

    # Unuploaded clip on development (upload check runs when not production-default-protected).
    upload_dir = _job(repo, "dev", "job_upload", status="completed")
    protected_unuploaded = _write_bytes(upload_dir, "clips/final.mp4", b"unuploaded")
    touch_age(protected_unuploaded, days=400)
    other_clip = upload_dir / "clips/other.mp4"
    other_clip.write_bytes(b"listed")
    handoff = upload_dir / "post_processing/reports/output_funnel_handoff.json"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(
        json.dumps({"finished_clip_paths": [str(other_clip.resolve())]}),
        encoding="utf-8",
    )

    # Symlink must not be followed for deletion (link is the planned target).
    sym_dir = _job(repo, "dev", "job_sym", status="completed")
    symlink_real = _write_bytes(sym_dir, "post_processing/tmp/real.txt", b"real")
    symlink_link = sym_dir / "post_processing/tmp/link.txt"
    symlink_link.symlink_to(symlink_real)
    # Age only the symlink path identity; real file stays recent so planner targets link.
    touch_age(symlink_link, days=5)

    return SafetyScenario(
        eligible_temp=eligible,
        protected_active=protected_active,
        protected_failed=protected_failed,
        protected_unknown=protected_unknown,
        protected_final_clip=protected_final_clip,
        protected_database=protected_database,
        protected_outside_root=protected_outside_root,
        protected_unuploaded=protected_unuploaded,
        symlink_link=symlink_link,
        symlink_real=symlink_real,
        eligible_size=len(b"delete-me"),
    )
