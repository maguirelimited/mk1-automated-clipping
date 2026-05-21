from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from typing import Any

from .models import MetadataResult, PreflightResult, SourceClip, UploadStatus
from .time_utils import now_iso

SCHEMA_VERSION = 1


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class OutputStore:
    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)

    def connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS clips (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_job_id TEXT NOT NULL,
                  clip_id TEXT NOT NULL,
                  clip_index INTEGER,
                  start TEXT,
                  end TEXT,
                  duration_sec REAL,
                  clip_file TEXT,
                  clip_path TEXT,
                  job_clip_path TEXT,
                  title TEXT,
                  hook TEXT,
                  caption TEXT,
                  reason TEXT,
                  scores_json TEXT NOT NULL DEFAULT '{}',
                  composite_score REAL,
                  clip_validation_json TEXT NOT NULL DEFAULT '{}',
                  source_payload_json TEXT NOT NULL DEFAULT '{}',
                  preflight_status TEXT,
                  preflight_json TEXT,
                  created_at TEXT NOT NULL,
                  UNIQUE(source_job_id, clip_id)
                );

                CREATE TABLE IF NOT EXISTS upload_jobs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  clip_pk INTEGER NOT NULL REFERENCES clips(id),
                  platform TEXT NOT NULL,
                  channel_id TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL,
                  normalized_title TEXT,
                  normalized_description TEXT,
                  normalized_hashtags_json TEXT NOT NULL DEFAULT '[]',
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  scheduled_at TEXT,
                  platform_publish_at TEXT,
                  attempt_count INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT,
                  platform_asset_id TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(clip_pk, platform, channel_id)
                );

                CREATE TABLE IF NOT EXISTS publish_attempts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  upload_job_id INTEGER NOT NULL REFERENCES upload_jobs(id),
                  attempted_at TEXT NOT NULL,
                  status TEXT NOT NULL,
                  request_json TEXT NOT NULL DEFAULT '{}',
                  response_json TEXT NOT NULL DEFAULT '{}',
                  error_category TEXT,
                  error_message TEXT,
                  retryable INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_upload_jobs_status
                  ON upload_jobs(status, scheduled_at);
                CREATE INDEX IF NOT EXISTS idx_upload_jobs_channel_day
                  ON upload_jobs(platform, channel_id, scheduled_at);
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    def register_source_clip(self, clip: SourceClip, preflight: PreflightResult | None = None) -> tuple[int, bool]:
        created_at = now_iso()
        preflight_status = None if preflight is None else ("passed" if preflight.ok else "failed")
        preflight_json = None if preflight is None else _json_dumps(preflight.__dict__)
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO clips (
                  source_job_id, clip_id, clip_index, start, end, duration_sec,
                  clip_file, clip_path, job_clip_path, title, hook, caption,
                  reason, scores_json, composite_score, clip_validation_json,
                  source_payload_json, preflight_status, preflight_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clip.source_job_id,
                    clip.clip_id,
                    clip.clip_index,
                    clip.start,
                    clip.end,
                    clip.duration_sec,
                    clip.clip_file,
                    clip.clip_path,
                    clip.job_clip_path,
                    clip.title,
                    clip.hook,
                    clip.caption,
                    clip.reason,
                    _json_dumps(clip.scores),
                    clip.composite_score,
                    _json_dumps(clip.clip_validation),
                    _json_dumps(clip.source_payload),
                    preflight_status,
                    preflight_json,
                    created_at,
                ),
            )
            if cur.rowcount:
                return int(cur.lastrowid), True
            row = conn.execute(
                "SELECT id FROM clips WHERE source_job_id = ? AND clip_id = ?",
                (clip.source_job_id, clip.clip_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to register or load source clip")
            return int(row["id"]), False

    def update_preflight_once(self, clip_pk: int, preflight: PreflightResult) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE clips
                SET preflight_status = COALESCE(preflight_status, ?),
                    preflight_json = COALESCE(preflight_json, ?)
                WHERE id = ?
                """,
                ("passed" if preflight.ok else "failed", _json_dumps(preflight.__dict__), clip_pk),
            )

    def create_upload_job(
        self,
        *,
        clip_pk: int,
        platform: str,
        channel_id: str | None = None,
        status: str = UploadStatus.REGISTERED,
    ) -> tuple[int, bool]:
        ts = now_iso()
        channel_key = channel_id or ""
        with self.connect() as conn:
            if not channel_key:
                existing = conn.execute(
                    """
                    SELECT id FROM upload_jobs
                    WHERE clip_pk = ? AND platform = ?
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (clip_pk, platform),
                ).fetchone()
                if existing is not None:
                    return int(existing["id"]), False
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO upload_jobs (
                  clip_pk, platform, channel_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (clip_pk, platform, channel_key, status, ts, ts),
            )
            if cur.rowcount:
                return int(cur.lastrowid), True
            row = conn.execute(
                """
                SELECT id FROM upload_jobs
                WHERE clip_pk = ? AND platform = ? AND channel_id = ?
                """,
                (clip_pk, platform, channel_key),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to create or load upload job")
            return int(row["id"]), False

    def list_upload_jobs(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = """
            SELECT uj.*, c.source_job_id, c.clip_id, c.title AS source_title, c.hook, c.caption,
                   c.duration_sec, c.clip_path, c.job_clip_path, c.composite_score
            FROM upload_jobs uj
            JOIN clips c ON c.id = uj.clip_pk
        """
        params: list[Any] = []
        if status:
            sql += " WHERE uj.status = ?"
            params.append(status)
        sql += " ORDER BY COALESCE(uj.scheduled_at, uj.created_at) ASC, uj.id ASC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._upload_job_from_row(row) for row in rows]

    def get_upload_job(self, upload_job_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT uj.*, c.source_job_id, c.clip_id, c.title AS source_title, c.hook, c.caption,
                       c.duration_sec, c.clip_path, c.job_clip_path, c.composite_score,
                       c.scores_json, c.clip_validation_json, c.preflight_json
                FROM upload_jobs uj
                JOIN clips c ON c.id = uj.clip_pk
                WHERE uj.id = ?
                """,
                (upload_job_id,),
            ).fetchone()
        return None if row is None else self._upload_job_from_row(row)

    def get_source_clip(self, clip_pk: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_pk,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["scores"] = _json_loads(data.pop("scores_json", None), {})
        data["clip_validation"] = _json_loads(data.pop("clip_validation_json", None), {})
        data["source_payload"] = _json_loads(data.pop("source_payload_json", None), {})
        data["preflight"] = _json_loads(data.pop("preflight_json", None), None)
        return data

    def update_upload_job(self, upload_job_id: int, **updates: Any) -> None:
        if not updates:
            return
        allowed = {
            "platform",
            "channel_id",
            "status",
            "normalized_title",
            "normalized_description",
            "normalized_hashtags_json",
            "metadata_json",
            "scheduled_at",
            "platform_publish_at",
            "attempt_count",
            "last_error",
            "platform_asset_id",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in updates.items():
            if key not in allowed:
                raise ValueError(f"Unknown upload job column: {key}")
            sets.append(f"{key} = ?")
            params.append(value)
        sets.append("updated_at = ?")
        params.append(now_iso())
        params.append(upload_job_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE upload_jobs SET {', '.join(sets)} WHERE id = ?", params)

    def set_routed(
        self,
        upload_job_id: int,
        *,
        channel_id: str,
        metadata: MetadataResult,
        profile_snapshot: dict[str, Any],
    ) -> None:
        self.update_upload_job(
            upload_job_id,
            channel_id=channel_id,
            status=UploadStatus.ROUTED,
            normalized_title=metadata.title,
            normalized_description=metadata.description,
            normalized_hashtags_json=_json_dumps(metadata.hashtags),
            metadata_json=_json_dumps(
                {
                    "metadata_issues": metadata.issues,
                    "profile_snapshot": profile_snapshot,
                }
            ),
            platform_publish_at=metadata.publish_at,
        )

    def set_scheduled(self, upload_job_id: int, *, scheduled_at: str, platform_publish_at: str) -> None:
        self.update_upload_job(
            upload_job_id,
            status=UploadStatus.SCHEDULED,
            scheduled_at=scheduled_at,
            platform_publish_at=platform_publish_at,
        )

    def claim_due_jobs(self, *, now: str, limit: int = 10) -> list[dict[str, Any]]:
        claimed_ids: list[int] = []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM upload_jobs
                WHERE status = ? AND scheduled_at <= ?
                ORDER BY scheduled_at ASC, id ASC
                LIMIT ?
                """,
                (UploadStatus.SCHEDULED, now, max(1, int(limit))),
            ).fetchall()
            for row in rows:
                cur = conn.execute(
                    """
                    UPDATE upload_jobs
                    SET status = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (UploadStatus.PUBLISHING, now_iso(), int(row["id"]), UploadStatus.SCHEDULED),
                )
                if cur.rowcount:
                    claimed_ids.append(int(row["id"]))
        return [job for job_id in claimed_ids if (job := self.get_upload_job(job_id)) is not None]

    def record_attempt(
        self,
        upload_job_id: int,
        *,
        status: str,
        request_summary: dict[str, Any] | None = None,
        response_summary: dict[str, Any] | None = None,
        error_category: str | None = None,
        error_message: str | None = None,
        retryable: bool = False,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO publish_attempts (
                  upload_job_id, attempted_at, status, request_json, response_json,
                  error_category, error_message, retryable
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    upload_job_id,
                    now_iso(),
                    status,
                    _json_dumps(request_summary or {}),
                    _json_dumps(response_summary or {}),
                    error_category,
                    error_message,
                    1 if retryable else 0,
                ),
            )
            conn.execute(
                """
                UPDATE upload_jobs
                SET attempt_count = attempt_count + 1,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error_message, now_iso(), upload_job_id),
            )

    def attempts_for_job(self, upload_job_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM publish_attempts
                WHERE upload_job_id = ?
                ORDER BY attempted_at DESC, id DESC
                """,
                (upload_job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def existing_scheduled_times(
        self,
        *,
        platform: str,
        channel_id: str,
        statuses: Iterable[str] = (UploadStatus.SCHEDULED, UploadStatus.PUBLISHING, UploadStatus.SCHEDULED_ON_PLATFORM),
    ) -> list[str]:
        placeholders = ", ".join("?" for _ in statuses)
        params: list[Any] = [platform, channel_id, *list(statuses)]
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT scheduled_at FROM upload_jobs
                WHERE platform = ? AND channel_id = ?
                  AND scheduled_at IS NOT NULL
                  AND status IN ({placeholders})
                ORDER BY scheduled_at ASC
                """,
                params,
            ).fetchall()
        return [str(row["scheduled_at"]) for row in rows if row["scheduled_at"]]

    @staticmethod
    def _upload_job_from_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["normalized_hashtags"] = _json_loads(data.pop("normalized_hashtags_json", None), [])
        data["metadata"] = _json_loads(data.pop("metadata_json", None), {})
        for key in ("scores_json", "clip_validation_json", "preflight_json"):
            if key in data:
                data[key[:-5] if key.endswith("_json") else key] = _json_loads(data.pop(key), {})
        return data
