from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .ai_config import AI_CONFIG_STORE_PREFIX
from .control_export import INGESTION_PAUSED, UPLOADS_PAUSED, export_control_flags
from .post_processing_config import POST_PROCESSING_CONFIG_STORE_PREFIX
from .processing_config import PROCESSING_CONFIG_STORE_PREFIX


class ControlStore:
    """Small UI-owned store for control-plane state and an operator audit trail."""

    def __init__(self, db_path: Path, *, controls_file: Path | None = None):
        self.db_path = db_path
        self.controls_file = controls_file

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # WAL keeps the audit + clip-review writes from blocking dashboard
        # reads when both happen at once. ``synchronous=NORMAL`` is the
        # standard WAL pairing; the cost is unaffected by control-state
        # write volume.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS controls (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS action_log (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  action TEXT NOT NULL,
                  target TEXT NOT NULL DEFAULT '',
                  ok INTEGER NOT NULL,
                  message TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS clip_reviews (
                  clip_key TEXT PRIMARY KEY,
                  job_id TEXT NOT NULL,
                  clip_id TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending',
                  flagged_high_quality INTEGER NOT NULL DEFAULT 0,
                  feedback_notes TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def get_controls(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM controls").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def get_control_bool(self, key: str, *, default: bool = False) -> bool:
        raw = self.get_controls().get(key)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def set_control_bool(self, key: str, value: bool) -> None:
        self._put_control(key, "1" if value else "0")
        if self.controls_file is not None:
            self._sync_controls_file()

    def _put_control(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO controls (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                  value = excluded.value,
                  updated_at = excluded.updated_at
                """,
                (key, value),
            )

    def get_ai_config(self) -> dict[str, str]:
        """Return saved local-AI overrides keyed by bare field name."""
        out: dict[str, str] = {}
        for key, value in self.get_controls().items():
            if key.startswith(AI_CONFIG_STORE_PREFIX):
                out[key[len(AI_CONFIG_STORE_PREFIX):]] = value
        return out

    def set_ai_config(self, values: dict[str, str]) -> None:
        """Persist local-AI overrides (bare field names) and resync the file."""
        for name, value in values.items():
            self._put_control(f"{AI_CONFIG_STORE_PREFIX}{name}", str(value))
        if self.controls_file is not None:
            self._sync_controls_file()

    def _get_namespaced(self, prefix: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, value in self.get_controls().items():
            if key.startswith(prefix):
                out[key[len(prefix):]] = value
        return out

    def _set_namespaced(self, prefix: str, values: dict[str, str]) -> None:
        for name, value in values.items():
            self._put_control(f"{prefix}{name}", str(value))
        if self.controls_file is not None:
            self._sync_controls_file()

    def get_processing_config(self) -> dict[str, str]:
        """Return saved processing-phase overrides keyed by bare field name."""
        return self._get_namespaced(PROCESSING_CONFIG_STORE_PREFIX)

    def set_processing_config(self, values: dict[str, str]) -> None:
        """Persist processing-phase overrides (bare field names) and resync."""
        self._set_namespaced(PROCESSING_CONFIG_STORE_PREFIX, values)

    def get_post_processing_config(self) -> dict[str, str]:
        """Return saved post-processing overrides keyed by bare field name."""
        return self._get_namespaced(POST_PROCESSING_CONFIG_STORE_PREFIX)

    def set_post_processing_config(self, values: dict[str, str]) -> None:
        """Persist post-processing overrides (bare field names) and resync."""
        self._set_namespaced(POST_PROCESSING_CONFIG_STORE_PREFIX, values)

    def _sync_controls_file(self) -> None:
        if self.controls_file is None:
            return
        from .control_export import HUMAN_APPROVAL_REQUIRED, PUBLISH_APPROVED_ONLY

        export_control_flags(
            self.controls_file,
            ingestion_paused=self.get_control_bool(INGESTION_PAUSED),
            uploads_paused=self.get_control_bool(UPLOADS_PAUSED),
            human_approval_required=self.get_control_bool(HUMAN_APPROVAL_REQUIRED),
            publish_approved_only=self.get_control_bool(PUBLISH_APPROVED_ONLY),
            ai_config=self.get_ai_config(),
            processing_config=self.get_processing_config(),
            post_processing_config=self.get_post_processing_config(),
        )

    def get_clip_review(self, job_id: str, clip_id: str) -> dict[str, Any] | None:
        key = f"{job_id}::{clip_id}"
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT clip_key, job_id, clip_id, status, flagged_high_quality,
                       feedback_notes, updated_at
                FROM clip_reviews
                WHERE clip_key = ?
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "clip_key": str(row["clip_key"]),
            "job_id": str(row["job_id"]),
            "clip_id": str(row["clip_id"]),
            "status": str(row["status"]),
            "flagged_high_quality": bool(row["flagged_high_quality"]),
            "feedback_notes": str(row["feedback_notes"]),
            "updated_at": str(row["updated_at"]),
        }

    def set_clip_review(
        self,
        job_id: str,
        clip_id: str,
        *,
        status: str,
        flagged_high_quality: bool | None = None,
        feedback_notes: str | None = None,
    ) -> dict[str, Any]:
        key = f"{job_id}::{clip_id}"
        existing = self.get_clip_review(job_id, clip_id)
        flagged = (
            flagged_high_quality
            if flagged_high_quality is not None
            else bool((existing or {}).get("flagged_high_quality"))
        )
        notes = (
            feedback_notes
            if feedback_notes is not None
            else str((existing or {}).get("feedback_notes") or "")
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO clip_reviews (
                  clip_key, job_id, clip_id, status, flagged_high_quality,
                  feedback_notes, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(clip_key) DO UPDATE SET
                  status = excluded.status,
                  flagged_high_quality = excluded.flagged_high_quality,
                  feedback_notes = excluded.feedback_notes,
                  updated_at = excluded.updated_at
                """,
                (key, job_id, clip_id, status, 1 if flagged else 0, notes[:4000]),
            )
        review = self.get_clip_review(job_id, clip_id)
        return review or {
            "clip_key": key,
            "job_id": job_id,
            "clip_id": clip_id,
            "status": status,
            "flagged_high_quality": flagged,
            "feedback_notes": notes,
        }

    def log_action(self, action: str, target: str, *, ok: bool, message: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO action_log (action, target, ok, message) VALUES (?, ?, ?, ?)",
                (action, target, 1 if ok else 0, message[:2000]),
            )

    def recent_actions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT action, target, ok, message, created_at
                FROM action_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

