from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

from .models import MetadataResult, PreflightResult, SourceClip, UploadStatus, canonical_status
from .time_utils import now_iso

SCHEMA_VERSION = 7


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    digest = sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def _future_iso(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=max(1, int(seconds)))).isoformat().replace("+00:00", "Z")


def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _editorial_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in ("funnel_id", "editorial", "ai_metadata", "tags", "topics"):
        if key in payload:
            metadata[key] = payload[key]
    return metadata


def _asset_path_from_clip_row(row: sqlite3.Row) -> str | None:
    for key in ("job_clip_path", "clip_path"):
        value = row[key] if key in row.keys() else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class OutputStore:
    """SQLite store for the mk1 durable output-funnel model.

    Physical table names still preserve older API language in a few places:
    ``upload_jobs`` is the mk1 publications table, and ``publish_attempts`` is
    the publication-attempt history table. Compatibility views expose the newer
    names while existing CLI/API callers continue to work.
    """

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)

    def connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets readers and a single writer proceed concurrently. The plan
        # worker, upload worker, and HTTP handlers all touch this database; in
        # the default rollback-journal mode they serialise and surface
        # ``database is locked`` errors on slow disk. WAL eliminates almost
        # all of that contention. ``synchronous=NORMAL`` is the standard WAL
        # companion: fsync only on checkpoint, safe against crashes (just not
        # against bare-metal power loss mid-write, which is acceptable here).
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_jobs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  durable_id TEXT UNIQUE,
                  source_job_id TEXT NOT NULL UNIQUE,
                  status TEXT,
                  source_video_path TEXT,
                  transcript_path TEXT,
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transcripts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  transcript_id TEXT NOT NULL UNIQUE,
                  source_job_pk INTEGER REFERENCES source_jobs(id),
                  clip_pk INTEGER REFERENCES clips(id),
                  transcript_type TEXT NOT NULL DEFAULT 'source',
                  language TEXT,
                  status TEXT NOT NULL DEFAULT 'available',
                  path TEXT,
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS clips (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  durable_id TEXT UNIQUE,
                  import_key TEXT UNIQUE,
                  source_job_pk INTEGER REFERENCES source_jobs(id),
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
                  transcript_path TEXT,
                  scores_json TEXT NOT NULL DEFAULT '{}',
                  composite_score REAL,
                  clip_validation_json TEXT NOT NULL DEFAULT '{}',
                  editorial_metadata_json TEXT NOT NULL DEFAULT '{}',
                  source_payload_json TEXT NOT NULL DEFAULT '{}',
                  preflight_status TEXT,
                  preflight_json TEXT,
                  created_at TEXT NOT NULL,
                  UNIQUE(source_job_id, clip_id)
                );

                CREATE TABLE IF NOT EXISTS clip_variants (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  variant_id TEXT NOT NULL UNIQUE,
                  clip_pk INTEGER NOT NULL REFERENCES clips(id),
                  variant_type TEXT NOT NULL DEFAULT 'default',
                  status TEXT NOT NULL DEFAULT 'ready',
                  platform TEXT,
                  asset_pk INTEGER REFERENCES assets(id),
                  rendered_asset_path TEXT,
                  render_fingerprint TEXT,
                  format_json TEXT NOT NULL DEFAULT '{}',
                  editorial_json TEXT NOT NULL DEFAULT '{}',
                  render_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(clip_pk, variant_type, platform, render_fingerprint)
                );

                CREATE TABLE IF NOT EXISTS assets (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  asset_id TEXT NOT NULL UNIQUE,
                  asset_type TEXT NOT NULL,
                  storage_status TEXT NOT NULL DEFAULT 'available',
                  path TEXT,
                  checksum TEXT,
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS publication_targets (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  target_id TEXT NOT NULL UNIQUE,
                  platform TEXT NOT NULL,
                  channel_id TEXT NOT NULL DEFAULT '',
                  display_name TEXT,
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(platform, channel_id)
                );

                CREATE TABLE IF NOT EXISTS upload_jobs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  publication_id TEXT UNIQUE,
                  clip_pk INTEGER NOT NULL REFERENCES clips(id),
                  variant_pk INTEGER REFERENCES clip_variants(id),
                  idempotency_key TEXT UNIQUE,
                  target_pk INTEGER REFERENCES publication_targets(id),
                  platform TEXT NOT NULL,
                  channel_id TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL,
                  normalized_title TEXT,
                  normalized_description TEXT,
                  normalized_hashtags_json TEXT NOT NULL DEFAULT '[]',
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  publish_at TEXT,
                  platform_publish_at TEXT,
                  upload_at TEXT,
                  upload_deadline TEXT,
                  upload_started_at TEXT,
                  uploaded_at TEXT,
                  scheduled_at TEXT,
                  attempt_count INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT,
                  platform_asset_id TEXT,
                  platform_video_id TEXT,
                  platform_state TEXT,
                  publish_state TEXT,
                  remote_ids_json TEXT NOT NULL DEFAULT '{}',
                  adapter_version TEXT,
                  api_version TEXT,
                  lease_owner TEXT,
                  lease_token TEXT,
                  lease_heartbeat_at TEXT,
                  lease_expires_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS clip_metrics (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  clip_pk INTEGER NOT NULL REFERENCES clips(id),
                  metric_name TEXT NOT NULL,
                  metric_value REAL,
                  metric_json TEXT NOT NULL DEFAULT '{}',
                  source TEXT,
                  observed_at TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  metric_unit TEXT,
                  window_start TEXT,
                  window_end TEXT,
                  dimensions_json TEXT NOT NULL DEFAULT '{}',
                  UNIQUE(clip_pk, metric_name, source, observed_at)
                );

                CREATE TABLE IF NOT EXISTS publication_metrics (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  upload_job_id INTEGER NOT NULL REFERENCES upload_jobs(id),
                  publication_id TEXT,
                  metric_name TEXT NOT NULL,
                  metric_value REAL,
                  metric_json TEXT NOT NULL DEFAULT '{}',
                  source TEXT,
                  observed_at TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  metric_unit TEXT,
                  window_start TEXT,
                  window_end TEXT,
                  dimensions_json TEXT NOT NULL DEFAULT '{}',
                  UNIQUE(upload_job_id, metric_name, source, observed_at)
                );

                CREATE TABLE IF NOT EXISTS analytics_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_type TEXT NOT NULL,
                  occurred_at TEXT NOT NULL,
                  clip_pk INTEGER REFERENCES clips(id),
                  upload_job_id INTEGER REFERENCES upload_jobs(id),
                  publication_id TEXT,
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS publish_attempts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  upload_job_id INTEGER NOT NULL REFERENCES upload_jobs(id),
                  publication_id TEXT,
                  attempted_at TEXT NOT NULL,
                  status TEXT NOT NULL,
                  request_json TEXT NOT NULL DEFAULT '{}',
                  response_json TEXT NOT NULL DEFAULT '{}',
                  raw_response_json TEXT NOT NULL DEFAULT '{}',
                  remote_ids_json TEXT NOT NULL DEFAULT '{}',
                  failure_class TEXT,
                  adapter_version TEXT,
                  api_version TEXT,
                  lease_token TEXT,
                  duration_ms INTEGER,
                  error_category TEXT,
                  error_message TEXT,
                  retryable INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS upload_audit_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  upload_job_id INTEGER NOT NULL REFERENCES upload_jobs(id),
                  publication_id TEXT,
                  platform TEXT NOT NULL,
                  channel_id TEXT NOT NULL DEFAULT '',
                  attempt_number INTEGER,
                  lease_token TEXT,
                  event_type TEXT NOT NULL,
                  result TEXT,
                  remote_ids_json TEXT NOT NULL DEFAULT '{}',
                  failure_class TEXT,
                  adapter_version TEXT,
                  api_version TEXT,
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  occurred_at TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_states (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  platform TEXT NOT NULL,
                  channel_id TEXT NOT NULL,
                  rate_limited_until TEXT,
                  circuit_open_until TEXT,
                  circuit_failure_count INTEGER NOT NULL DEFAULT 0,
                  paused_reason TEXT,
                  token_source TEXT,
                  token_expires_at TEXT,
                  token_last_checked_at TEXT,
                  token_last_refresh_at TEXT,
                  token_last_refresh_error TEXT,
                  last_failure_class TEXT,
                  last_failure_at TEXT,
                  updated_at TEXT NOT NULL,
                  UNIQUE(platform, channel_id)
                );

                CREATE TABLE IF NOT EXISTS publication_status_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  upload_job_id INTEGER NOT NULL REFERENCES upload_jobs(id),
                  publication_id TEXT,
                  from_status TEXT,
                  to_status TEXT NOT NULL,
                  reason TEXT,
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  occurred_at TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS variant_status_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  variant_pk INTEGER NOT NULL REFERENCES clip_variants(id),
                  variant_id TEXT,
                  from_status TEXT,
                  to_status TEXT NOT NULL,
                  reason TEXT,
                  payload_json TEXT NOT NULL DEFAULT '{}',
                  occurred_at TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                """
            )
            self._migrate_upload_jobs_v2(conn)
            self._migrate_core_v3(conn)
            self._migrate_core_v4(conn)
            self._migrate_core_v5(conn)
            self._migrate_core_v6(conn)
            self._migrate_core_v7(conn)
            self._ensure_indexes(conn)
            self._refresh_publications_view(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    @staticmethod
    def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}

    def _ensure_indexes(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_upload_jobs_status
              ON upload_jobs(status, upload_at);
            CREATE INDEX IF NOT EXISTS idx_upload_jobs_channel_day
              ON upload_jobs(platform, channel_id, publish_at);
            CREATE INDEX IF NOT EXISTS idx_upload_jobs_legacy_sched
              ON upload_jobs(status, scheduled_at);
            CREATE INDEX IF NOT EXISTS idx_clips_source_job_pk
              ON clips(source_job_pk);
            CREATE INDEX IF NOT EXISTS idx_clip_variants_clip
              ON clip_variants(clip_pk, status);
            CREATE INDEX IF NOT EXISTS idx_assets_path
              ON assets(path);
            CREATE INDEX IF NOT EXISTS idx_publication_targets_platform_channel
              ON publication_targets(platform, channel_id);
            CREATE INDEX IF NOT EXISTS idx_upload_jobs_variant
              ON upload_jobs(variant_pk);
            CREATE INDEX IF NOT EXISTS idx_upload_jobs_target
              ON upload_jobs(target_pk);
            CREATE INDEX IF NOT EXISTS idx_clip_metrics_clip_observed
              ON clip_metrics(clip_pk, observed_at);
            CREATE INDEX IF NOT EXISTS idx_publication_metrics_job_observed
              ON publication_metrics(upload_job_id, observed_at);
            CREATE INDEX IF NOT EXISTS idx_publication_metrics_publication
              ON publication_metrics(publication_id, observed_at);
            CREATE INDEX IF NOT EXISTS idx_analytics_events_type_time
              ON analytics_events(event_type, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_publication_status_events_job
              ON publication_status_events(upload_job_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_variant_status_events_variant
              ON variant_status_events(variant_pk, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_upload_jobs_lease
              ON upload_jobs(status, lease_expires_at);
            CREATE INDEX IF NOT EXISTS idx_upload_audit_job
              ON upload_audit_events(upload_job_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_upload_audit_account
              ON upload_audit_events(platform, channel_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_account_states_platform_channel
              ON account_states(platform, channel_id);
            """
        )

    def _migrate_upload_jobs_v2(self, conn: sqlite3.Connection) -> None:
        cols = self._existing_columns(conn, "upload_jobs")
        additions: list[tuple[str, str]] = [
            ("publish_at", "TEXT"),
            ("platform_publish_at", "TEXT"),
            ("upload_at", "TEXT"),
            ("upload_deadline", "TEXT"),
            ("upload_started_at", "TEXT"),
            ("uploaded_at", "TEXT"),
            ("platform_video_id", "TEXT"),
            ("platform_state", "TEXT"),
        ]
        for name, ddl_type in additions:
            if name not in cols:
                conn.execute(f"ALTER TABLE upload_jobs ADD COLUMN {name} {ddl_type}")

        cols = self._existing_columns(conn, "upload_jobs")
        if "scheduled_at" in cols and "publish_at" in cols:
            conn.execute(
                """
                UPDATE upload_jobs
                SET publish_at = scheduled_at
                WHERE publish_at IS NULL
                  AND scheduled_at IS NOT NULL
                  AND status IN ('scheduled', 'publishing', 'scheduled_on_platform', 'planned',
                                 'pending_upload', 'uploading', 'uploaded_scheduled', 'published')
                """
            )
        if "platform_publish_at" in cols and "publish_at" in cols:
            conn.execute(
                """
                UPDATE upload_jobs
                SET platform_publish_at = publish_at
                WHERE (platform_publish_at IS NULL OR platform_publish_at = '')
                  AND publish_at IS NOT NULL
                """
            )
        if "platform_asset_id" in cols and "platform_video_id" in cols:
            conn.execute(
                """
                UPDATE upload_jobs
                SET platform_video_id = platform_asset_id
                WHERE platform_video_id IS NULL
                  AND platform_asset_id IS NOT NULL
                """
            )

    def _migrate_core_v3(self, conn: sqlite3.Connection) -> None:
        clip_cols = self._existing_columns(conn, "clips")
        clip_additions: list[tuple[str, str]] = [
            ("durable_id", "TEXT"),
            ("source_job_pk", "INTEGER REFERENCES source_jobs(id)"),
            ("transcript_path", "TEXT"),
            ("editorial_metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ]
        for name, ddl_type in clip_additions:
            if name not in clip_cols:
                conn.execute(f"ALTER TABLE clips ADD COLUMN {name} {ddl_type}")

        upload_cols = self._existing_columns(conn, "upload_jobs")
        if "publication_id" not in upload_cols:
            conn.execute("ALTER TABLE upload_jobs ADD COLUMN publication_id TEXT")

        rows = conn.execute(
            "SELECT id, source_job_id, clip_id FROM clips WHERE durable_id IS NULL OR durable_id = ''"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE clips SET durable_id = ? WHERE id = ?",
                (_stable_id("clip", row["source_job_id"], row["clip_id"]), int(row["id"])),
            )

        conn.execute(
            """
            INSERT OR IGNORE INTO source_jobs (
              source_job_id, status, payload_json, metadata_json, created_at, updated_at
            )
            SELECT DISTINCT source_job_id, 'registered', '{}', '{}', created_at, created_at
            FROM clips
            WHERE source_job_id IS NOT NULL AND source_job_id != ''
            """
        )
        conn.execute(
            """
            UPDATE clips
            SET source_job_pk = (
              SELECT sj.id FROM source_jobs sj WHERE sj.source_job_id = clips.source_job_id
            )
            WHERE source_job_pk IS NULL
            """
        )

        pub_rows = conn.execute(
            """
            SELECT uj.id, c.durable_id, uj.platform, uj.channel_id
            FROM upload_jobs uj
            JOIN clips c ON c.id = uj.clip_pk
            WHERE uj.publication_id IS NULL OR uj.publication_id = ''
            """
        ).fetchall()
        for row in pub_rows:
            conn.execute(
                "UPDATE upload_jobs SET publication_id = ? WHERE id = ?",
                (
                    _stable_id(
                        "pub",
                        row["durable_id"],
                        row["platform"],
                        row["channel_id"] or "",
                    ),
                    int(row["id"]),
                ),
            )

        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_clips_durable_id ON clips(durable_id)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_upload_jobs_publication_id ON upload_jobs(publication_id)"
        )

    def _migrate_core_v4(self, conn: sqlite3.Connection) -> None:
        source_cols = self._existing_columns(conn, "source_jobs")
        if "durable_id" not in source_cols:
            conn.execute("ALTER TABLE source_jobs ADD COLUMN durable_id TEXT")
        rows = conn.execute(
            "SELECT id, source_job_id FROM source_jobs WHERE durable_id IS NULL OR durable_id = ''"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE source_jobs SET durable_id = ? WHERE id = ?",
                (_stable_id("src", row["source_job_id"]), int(row["id"])),
            )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_source_jobs_durable_id ON source_jobs(durable_id)")

        attempt_cols = self._existing_columns(conn, "publish_attempts")
        if "publication_id" not in attempt_cols:
            conn.execute("ALTER TABLE publish_attempts ADD COLUMN publication_id TEXT")
        conn.execute(
            """
            UPDATE publish_attempts
            SET publication_id = (
              SELECT uj.publication_id FROM upload_jobs uj WHERE uj.id = publish_attempts.upload_job_id
            )
            WHERE publication_id IS NULL OR publication_id = ''
            """
        )

    def _migrate_core_v5(self, conn: sqlite3.Connection) -> None:
        upload_cols = self._existing_columns(conn, "upload_jobs")
        upload_additions: list[tuple[str, str]] = [
            ("variant_pk", "INTEGER REFERENCES clip_variants(id)"),
            ("idempotency_key", "TEXT"),
        ]
        for name, ddl_type in upload_additions:
            if name not in upload_cols:
                conn.execute(f"ALTER TABLE upload_jobs ADD COLUMN {name} {ddl_type}")
        self._rebuild_upload_jobs_without_legacy_unique(conn)

        clip_rows = conn.execute(
            """
            SELECT id, durable_id, job_clip_path, clip_path, source_payload_json, created_at
            FROM clips
            """
        ).fetchall()
        for row in clip_rows:
            variant_id = _stable_id("var", row["durable_id"], "default")
            asset_path = _asset_path_from_clip_row(row)
            editorial = _json_loads(row["source_payload_json"], {})
            conn.execute(
                """
                INSERT OR IGNORE INTO clip_variants (
                  variant_id, clip_pk, variant_type, status, rendered_asset_path,
                  render_fingerprint, format_json, editorial_json, render_json,
                  created_at, updated_at
                ) VALUES (?, ?, 'default', 'ready', ?, 'default', '{}', ?, '{}', ?, ?)
                """,
                (
                    variant_id,
                    int(row["id"]),
                    asset_path,
                    _json_dumps(_editorial_metadata(editorial if isinstance(editorial, dict) else {})),
                    row["created_at"],
                    row["created_at"],
                ),
            )

        conn.execute(
            """
            UPDATE upload_jobs
            SET variant_pk = (
              SELECT cv.id
              FROM clip_variants cv
              WHERE cv.clip_pk = upload_jobs.clip_pk
                AND cv.variant_type = 'default'
                AND COALESCE(cv.platform, '') = ''
              ORDER BY cv.id ASC
              LIMIT 1
            )
            WHERE variant_pk IS NULL
            """
        )
        conn.execute(
            """
            UPDATE upload_jobs
            SET idempotency_key = 'registration:' || COALESCE(variant_pk, clip_pk) || ':' || platform || ':' || COALESCE(channel_id, '')
            WHERE idempotency_key IS NULL OR idempotency_key = ''
            """
        )
        missing_publications = conn.execute(
            "SELECT id FROM upload_jobs WHERE publication_id IS NULL OR publication_id = ''"
        ).fetchall()
        for row in missing_publications:
            conn.execute(
                "UPDATE upload_jobs SET publication_id = ? WHERE id = ?",
                (_new_id("pub"), int(row["id"])),
            )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_upload_jobs_publication_id ON upload_jobs(publication_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_upload_jobs_idempotency_key ON upload_jobs(idempotency_key)")

        metric_cols = self._existing_columns(conn, "publication_metrics")
        if "publication_id" not in metric_cols:
            conn.execute("ALTER TABLE publication_metrics ADD COLUMN publication_id TEXT")
        conn.execute(
            """
            UPDATE publication_metrics
            SET publication_id = (
              SELECT uj.publication_id FROM upload_jobs uj WHERE uj.id = publication_metrics.upload_job_id
            )
            WHERE publication_id IS NULL OR publication_id = ''
            """
        )

    def _migrate_core_v6(self, conn: sqlite3.Connection) -> None:
        self._ensure_columns(
            conn,
            "clips",
            [("import_key", "TEXT")],
        )
        conn.execute(
            """
            UPDATE clips
            SET import_key = source_job_id || ':' || clip_id
            WHERE import_key IS NULL OR import_key = ''
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_clips_import_key ON clips(import_key)")

        self._ensure_columns(
            conn,
            "clip_variants",
            [("asset_pk", "INTEGER REFERENCES assets(id)")],
        )
        self._ensure_columns(
            conn,
            "upload_jobs",
            [("target_pk", "INTEGER REFERENCES publication_targets(id)")],
        )
        self._ensure_columns(
            conn,
            "clip_metrics",
            [
                ("metric_unit", "TEXT"),
                ("window_start", "TEXT"),
                ("window_end", "TEXT"),
                ("dimensions_json", "TEXT NOT NULL DEFAULT '{}'"),
            ],
        )
        self._ensure_columns(
            conn,
            "publication_metrics",
            [
                ("metric_unit", "TEXT"),
                ("window_start", "TEXT"),
                ("window_end", "TEXT"),
                ("dimensions_json", "TEXT NOT NULL DEFAULT '{}'"),
            ],
        )

        for row in conn.execute(
            """
            SELECT id, source_job_pk, transcript_path, created_at
            FROM clips
            WHERE transcript_path IS NOT NULL AND transcript_path != ''
            """
        ).fetchall():
            self._ensure_transcript_row(
                conn,
                source_job_pk=row["source_job_pk"],
                clip_pk=int(row["id"]),
                path=row["transcript_path"],
                transcript_type="clip",
                created_at=row["created_at"],
            )
        for row in conn.execute(
            """
            SELECT id, transcript_path, created_at
            FROM source_jobs
            WHERE transcript_path IS NOT NULL AND transcript_path != ''
            """
        ).fetchall():
            self._ensure_transcript_row(
                conn,
                source_job_pk=int(row["id"]),
                clip_pk=None,
                path=row["transcript_path"],
                transcript_type="source",
                created_at=row["created_at"],
            )

        for row in conn.execute(
            """
            SELECT id, rendered_asset_path, created_at
            FROM clip_variants
            WHERE rendered_asset_path IS NOT NULL AND rendered_asset_path != ''
              AND asset_pk IS NULL
            """
        ).fetchall():
            asset_pk = self._ensure_asset_row(
                conn,
                path=row["rendered_asset_path"],
                asset_type="rendered_video",
                created_at=row["created_at"],
            )
            conn.execute("UPDATE clip_variants SET asset_pk = ? WHERE id = ?", (asset_pk, int(row["id"])))

        for row in conn.execute(
            """
            SELECT id, platform, channel_id, created_at
            FROM upload_jobs
            WHERE target_pk IS NULL
            """
        ).fetchall():
            target_pk = self._ensure_publication_target_row(
                conn,
                platform=row["platform"],
                channel_id=row["channel_id"] or "",
                created_at=row["created_at"],
            )
            conn.execute("UPDATE upload_jobs SET target_pk = ? WHERE id = ?", (target_pk, int(row["id"])))

        conn.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_source_job ON transcripts(source_job_pk)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_transcripts_clip ON transcripts(clip_pk)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_path ON assets(path)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_publication_targets_platform_channel ON publication_targets(platform, channel_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_jobs_target ON upload_jobs(target_pk)")

    def _migrate_core_v7(self, conn: sqlite3.Connection) -> None:
        self._ensure_columns(
            conn,
            "upload_jobs",
            [
                ("publish_state", "TEXT"),
                ("remote_ids_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("adapter_version", "TEXT"),
                ("api_version", "TEXT"),
                ("lease_owner", "TEXT"),
                ("lease_token", "TEXT"),
                ("lease_heartbeat_at", "TEXT"),
                ("lease_expires_at", "TEXT"),
            ],
        )
        self._ensure_columns(
            conn,
            "publish_attempts",
            [
                ("raw_response_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("remote_ids_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("failure_class", "TEXT"),
                ("adapter_version", "TEXT"),
                ("api_version", "TEXT"),
                ("lease_token", "TEXT"),
                ("duration_ms", "INTEGER"),
            ],
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS upload_audit_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              upload_job_id INTEGER NOT NULL REFERENCES upload_jobs(id),
              publication_id TEXT,
              platform TEXT NOT NULL,
              channel_id TEXT NOT NULL DEFAULT '',
              attempt_number INTEGER,
              lease_token TEXT,
              event_type TEXT NOT NULL,
              result TEXT,
              remote_ids_json TEXT NOT NULL DEFAULT '{}',
              failure_class TEXT,
              adapter_version TEXT,
              api_version TEXT,
              payload_json TEXT NOT NULL DEFAULT '{}',
              occurred_at TEXT NOT NULL,
              created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_states (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              platform TEXT NOT NULL,
              channel_id TEXT NOT NULL,
              rate_limited_until TEXT,
              circuit_open_until TEXT,
              circuit_failure_count INTEGER NOT NULL DEFAULT 0,
              paused_reason TEXT,
              token_source TEXT,
              token_expires_at TEXT,
              token_last_checked_at TEXT,
              token_last_refresh_at TEXT,
              token_last_refresh_error TEXT,
              last_failure_class TEXT,
              last_failure_at TEXT,
              updated_at TEXT NOT NULL,
              UNIQUE(platform, channel_id)
            )
            """
        )

    def _ensure_columns(
        self,
        conn: sqlite3.Connection,
        table: str,
        additions: list[tuple[str, str]],
    ) -> None:
        cols = self._existing_columns(conn, table)
        for name, ddl_type in additions:
            if name not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")

    def _ensure_asset_row(
        self,
        conn: sqlite3.Connection,
        *,
        path: str | None,
        asset_type: str,
        created_at: str,
        checksum: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int | None:
        if not path:
            return None
        asset_id = _stable_id("asset", asset_type, path)
        conn.execute(
            """
            INSERT OR IGNORE INTO assets (
              asset_id, asset_type, storage_status, path, checksum,
              metadata_json, created_at, updated_at
            ) VALUES (?, ?, 'available', ?, ?, ?, ?, ?)
            """,
            (asset_id, asset_type, path, checksum, _json_dumps(metadata or {}), created_at, created_at),
        )
        row = conn.execute("SELECT id FROM assets WHERE asset_id = ?", (asset_id,)).fetchone()
        return None if row is None else int(row["id"])

    def _ensure_publication_target_row(
        self,
        conn: sqlite3.Connection,
        *,
        platform: str,
        channel_id: str,
        created_at: str,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        target_id = _stable_id("target", platform, channel_id)
        conn.execute(
            """
            INSERT OR IGNORE INTO publication_targets (
              target_id, platform, channel_id, display_name,
              metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (target_id, platform, channel_id, display_name, _json_dumps(metadata or {}), created_at, created_at),
        )
        row = conn.execute("SELECT id FROM publication_targets WHERE target_id = ?", (target_id,)).fetchone()
        if row is None:
            raise RuntimeError("Failed to create or load publication target")
        return int(row["id"])

    def _ensure_transcript_row(
        self,
        conn: sqlite3.Connection,
        *,
        source_job_pk: int | None,
        clip_pk: int | None,
        path: str | None,
        transcript_type: str,
        created_at: str,
        language: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int | None:
        if not path:
            return None
        transcript_id = _stable_id("tx", transcript_type, source_job_pk, clip_pk, path)
        conn.execute(
            """
            INSERT OR IGNORE INTO transcripts (
              transcript_id, source_job_pk, clip_pk, transcript_type,
              language, status, path, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'available', ?, ?, ?, ?)
            """,
            (
                transcript_id,
                source_job_pk,
                clip_pk,
                transcript_type,
                language,
                path,
                _json_dumps(metadata or {}),
                created_at,
                created_at,
            ),
        )
        row = conn.execute("SELECT id FROM transcripts WHERE transcript_id = ?", (transcript_id,)).fetchone()
        return None if row is None else int(row["id"])

    def _rebuild_upload_jobs_without_legacy_unique(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'upload_jobs'"
        ).fetchone()
        table_sql = "" if row is None else str(row["sql"] or "")
        if "UNIQUE(clip_pk, platform, channel_id)" not in table_sql:
            return
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS upload_jobs_v5 (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              publication_id TEXT UNIQUE,
              clip_pk INTEGER NOT NULL REFERENCES clips(id),
              variant_pk INTEGER REFERENCES clip_variants(id),
              idempotency_key TEXT UNIQUE,
              platform TEXT NOT NULL,
              channel_id TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL,
              normalized_title TEXT,
              normalized_description TEXT,
              normalized_hashtags_json TEXT NOT NULL DEFAULT '[]',
              metadata_json TEXT NOT NULL DEFAULT '{}',
              publish_at TEXT,
              platform_publish_at TEXT,
              upload_at TEXT,
              upload_deadline TEXT,
              upload_started_at TEXT,
              uploaded_at TEXT,
              scheduled_at TEXT,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              platform_asset_id TEXT,
              platform_video_id TEXT,
              platform_state TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            INSERT OR IGNORE INTO upload_jobs_v5 (
              id, publication_id, clip_pk, variant_pk, idempotency_key,
              platform, channel_id, status, normalized_title, normalized_description,
              normalized_hashtags_json, metadata_json, publish_at, platform_publish_at,
              upload_at, upload_deadline, upload_started_at, uploaded_at, scheduled_at,
              attempt_count, last_error, platform_asset_id, platform_video_id,
              platform_state, created_at, updated_at
            )
            SELECT
              id, publication_id, clip_pk, variant_pk, idempotency_key,
              platform, channel_id, status, normalized_title, normalized_description,
              normalized_hashtags_json, metadata_json, publish_at, platform_publish_at,
              upload_at, upload_deadline, upload_started_at, uploaded_at, scheduled_at,
              attempt_count, last_error, platform_asset_id, platform_video_id,
              platform_state, created_at, updated_at
            FROM upload_jobs;

            DROP TABLE upload_jobs;
            ALTER TABLE upload_jobs_v5 RENAME TO upload_jobs;
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_jobs_status ON upload_jobs(status, upload_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_jobs_channel_day ON upload_jobs(platform, channel_id, publish_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_jobs_legacy_sched ON upload_jobs(status, scheduled_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_jobs_variant ON upload_jobs(variant_pk)")

        event_cols = self._existing_columns(conn, "analytics_events")
        if "publication_id" not in event_cols:
            conn.execute("ALTER TABLE analytics_events ADD COLUMN publication_id TEXT")
        conn.execute(
            """
            UPDATE analytics_events
            SET publication_id = (
              SELECT uj.publication_id FROM upload_jobs uj WHERE uj.id = analytics_events.upload_job_id
            )
            WHERE upload_job_id IS NOT NULL
              AND (publication_id IS NULL OR publication_id = '')
            """
        )

    def _refresh_publications_view(self, conn: sqlite3.Connection) -> None:
        conn.execute("DROP VIEW IF EXISTS publications")
        conn.execute("DROP VIEW IF EXISTS publication_attempts")
        conn.execute(
            """
            CREATE VIEW publications AS
            SELECT
              id,
              publication_id,
              clip_pk,
              variant_pk,
              idempotency_key,
              platform,
              channel_id,
              status,
              normalized_title,
              normalized_description,
              normalized_hashtags_json,
              metadata_json,
              publish_at,
              platform_publish_at,
              upload_at,
              upload_deadline,
              upload_started_at,
              uploaded_at,
              scheduled_at,
              attempt_count,
              last_error,
              platform_asset_id,
              platform_video_id,
              platform_state,
              created_at,
              updated_at
            FROM upload_jobs
            """
        )
        conn.execute(
            """
            CREATE VIEW publication_attempts AS
            SELECT
              id,
              upload_job_id,
              publication_id,
              attempted_at,
              status,
              request_json,
              response_json,
              error_category,
              error_message,
              retryable
            FROM publish_attempts
            """
        )

    def register_source_job(self, payload: dict[str, Any]) -> tuple[int, bool]:
        source_job_id = str(payload.get("job_id") or "").strip()
        if not source_job_id:
            raise ValueError("Job payload requires `job_id`")
        ts = now_iso()
        durable_id = _stable_id("src", source_job_id)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        source_video_path = _first_text(
            payload,
            "source_video_path",
            "video_path",
            "input_path",
            "media_path",
        )
        transcript_path = _first_text(
            payload,
            "transcript_path",
            "source_transcript_path",
        )
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO source_jobs (
                  durable_id, source_job_id, status, source_video_path, transcript_path,
                  payload_json, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    durable_id,
                    source_job_id,
                    str(payload.get("status") or "registered"),
                    source_video_path,
                    transcript_path,
                    _json_dumps(payload),
                    _json_dumps(metadata),
                    ts,
                    ts,
                ),
            )
            if cur.rowcount:
                source_job_pk = int(cur.lastrowid)
                if transcript_path:
                    self._ensure_transcript_row(
                        conn,
                        source_job_pk=source_job_pk,
                        clip_pk=None,
                        path=transcript_path,
                        transcript_type="source",
                        created_at=ts,
                    )
                return source_job_pk, True
            conn.execute(
                """
                UPDATE source_jobs
                SET durable_id = COALESCE(durable_id, ?),
                    status = ?,
                    source_video_path = COALESCE(?, source_video_path),
                    transcript_path = COALESCE(?, transcript_path),
                    payload_json = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE source_job_id = ?
                """,
                (
                    durable_id,
                    str(payload.get("status") or "registered"),
                    source_video_path,
                    transcript_path,
                    _json_dumps(payload),
                    _json_dumps(metadata),
                    ts,
                    source_job_id,
                ),
            )
            row = conn.execute(
                "SELECT id FROM source_jobs WHERE source_job_id = ?",
                (source_job_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to register or load source job")
            if transcript_path:
                self._ensure_transcript_row(
                    conn,
                    source_job_pk=int(row["id"]),
                    clip_pk=None,
                    path=transcript_path,
                    transcript_type="source",
                    created_at=ts,
                )
            return int(row["id"]), False

    def register_source_clip(self, clip: SourceClip, preflight: PreflightResult | None = None) -> tuple[int, bool]:
        created_at = now_iso()
        preflight_status = None if preflight is None else ("passed" if preflight.ok else "failed")
        preflight_json = None if preflight is None else _json_dumps(preflight.__dict__)
        durable_id = _stable_id("clip", clip.source_job_id, clip.clip_id)
        with self.connect() as conn:
            source_job = conn.execute(
                "SELECT id FROM source_jobs WHERE source_job_id = ?",
                (clip.source_job_id,),
            ).fetchone()
            source_job_pk = None if source_job is None else int(source_job["id"])
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO clips (
                  durable_id, import_key, source_job_pk, source_job_id, clip_id, clip_index, start, end, duration_sec,
                  clip_file, clip_path, job_clip_path, title, hook, caption,
                  reason, transcript_path, scores_json, composite_score, clip_validation_json,
                  editorial_metadata_json,
                  source_payload_json, preflight_status, preflight_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    durable_id,
                    f"{clip.source_job_id}:{clip.clip_id}",
                    source_job_pk,
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
                    _first_text(clip.source_payload, "transcript_path", "clip_transcript_path"),
                    _json_dumps(clip.scores),
                    clip.composite_score,
                    _json_dumps(clip.clip_validation),
                    _json_dumps(_editorial_metadata(clip.source_payload)),
                    _json_dumps(clip.source_payload),
                    preflight_status,
                    preflight_json,
                    created_at,
                ),
            )
            if cur.rowcount:
                clip_pk = int(cur.lastrowid)
                transcript_path = _first_text(clip.source_payload, "transcript_path", "clip_transcript_path")
                if transcript_path:
                    self._ensure_transcript_row(
                        conn,
                        source_job_pk=source_job_pk,
                        clip_pk=clip_pk,
                        path=transcript_path,
                        transcript_type="clip",
                        created_at=created_at,
                    )
                return clip_pk, True
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

    def ensure_default_variant(self, clip_pk: int) -> tuple[int, bool]:
        ts = now_iso()
        with self.connect() as conn:
            clip = conn.execute(
                """
                SELECT id, durable_id, source_payload_json, job_clip_path, clip_path, created_at
                FROM clips
                WHERE id = ?
                """,
                (clip_pk,),
            ).fetchone()
            if clip is None:
                raise RuntimeError("Cannot create variant for missing clip")
            variant_id = _stable_id("var", clip["durable_id"], "default")
            asset_path = _asset_path_from_clip_row(clip)
            source_payload = _json_loads(clip["source_payload_json"], {})
            asset_pk = self._ensure_asset_row(
                conn,
                path=asset_path,
                asset_type="rendered_video",
                created_at=ts,
            )
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO clip_variants (
                  variant_id, clip_pk, variant_type, status, asset_pk, rendered_asset_path,
                  render_fingerprint, format_json, editorial_json, render_json,
                  created_at, updated_at
                ) VALUES (?, ?, 'default', 'ready', ?, ?, 'default', '{}', ?, '{}', ?, ?)
                """,
                (
                    variant_id,
                    clip_pk,
                    asset_pk,
                    asset_path,
                    _json_dumps(_editorial_metadata(source_payload if isinstance(source_payload, dict) else {})),
                    ts,
                    ts,
                ),
            )
            if cur.rowcount:
                variant_pk = int(cur.lastrowid)
                self._record_variant_status_event(
                    conn,
                    variant_pk,
                    from_status=None,
                    to_status="ready",
                    reason="default_variant_created",
                )
                return variant_pk, True
            row = conn.execute(
                """
                SELECT id FROM clip_variants
                WHERE clip_pk = ? AND variant_type = 'default' AND COALESCE(platform, '') = ''
                ORDER BY id ASC
                LIMIT 1
                """,
                (clip_pk,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to create or load default clip variant")
            return int(row["id"]), False

    def create_clip_variant(
        self,
        *,
        clip_pk: int,
        variant_type: str = "custom",
        status: str = "ready",
        platform: str | None = None,
        rendered_asset_path: str | None = None,
        render_fingerprint: str | None = None,
        format: dict[str, Any] | None = None,
        editorial: dict[str, Any] | None = None,
        render: dict[str, Any] | None = None,
    ) -> tuple[int, bool]:
        ts = now_iso()
        fingerprint = render_fingerprint or _new_id("render")
        variant_id = _stable_id(
            "var",
            clip_pk,
            variant_type,
            platform or "",
            fingerprint,
        )
        with self.connect() as conn:
            clip = conn.execute("SELECT id FROM clips WHERE id = ?", (clip_pk,)).fetchone()
            if clip is None:
                raise RuntimeError("Cannot create variant for missing clip")
            asset_pk = self._ensure_asset_row(
                conn,
                path=rendered_asset_path,
                asset_type="rendered_video",
                created_at=ts,
            )
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO clip_variants (
                  variant_id, clip_pk, variant_type, status, platform,
                  asset_pk, rendered_asset_path, render_fingerprint, format_json,
                  editorial_json, render_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    variant_id,
                    clip_pk,
                    variant_type,
                    status,
                    platform,
                    asset_pk,
                    rendered_asset_path,
                    fingerprint,
                    _json_dumps(format or {}),
                    _json_dumps(editorial or {}),
                    _json_dumps(render or {}),
                    ts,
                    ts,
                ),
            )
            if cur.rowcount:
                variant_pk = int(cur.lastrowid)
                self._record_variant_status_event(
                    conn,
                    variant_pk,
                    from_status=None,
                    to_status=status,
                    reason="variant_created",
                )
                return variant_pk, True
            row = conn.execute(
                """
                SELECT id FROM clip_variants
                WHERE clip_pk = ?
                  AND variant_type = ?
                  AND COALESCE(platform, '') = COALESCE(?, '')
                  AND render_fingerprint = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (clip_pk, variant_type, platform, fingerprint),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to create or load clip variant")
            return int(row["id"]), False

    def get_clip_variant(self, variant_pk: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM clip_variants WHERE id = ?", (variant_pk,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["format"] = _json_loads(data.pop("format_json", None), {})
        data["editorial"] = _json_loads(data.pop("editorial_json", None), {})
        data["render"] = _json_loads(data.pop("render_json", None), {})
        return data

    def _record_variant_status_event(
        self,
        conn: sqlite3.Connection,
        variant_pk: int,
        *,
        from_status: str | None,
        to_status: str,
        reason: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        row = conn.execute(
            "SELECT variant_id FROM clip_variants WHERE id = ?",
            (variant_pk,),
        ).fetchone()
        ts = now_iso()
        conn.execute(
            """
            INSERT INTO variant_status_events (
              variant_pk, variant_id, from_status, to_status,
              reason, payload_json, occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                variant_pk,
                None if row is None else row["variant_id"],
                from_status,
                to_status,
                reason,
                _json_dumps(payload or {}),
                ts,
                ts,
            ),
        )

    def update_clip_variant(self, variant_pk: int, **updates: Any) -> None:
        if not updates:
            return
        allowed = {
            "status",
            "asset_pk",
            "rendered_asset_path",
            "render_fingerprint",
            "format_json",
            "editorial_json",
            "render_json",
        }
        sets: list[str] = []
        params: list[Any] = []
        from_status: str | None = None
        with self.connect() as conn:
            if "status" in updates:
                row = conn.execute("SELECT status FROM clip_variants WHERE id = ?", (variant_pk,)).fetchone()
                if row is not None:
                    from_status = row["status"]
            for key, value in updates.items():
                if key not in allowed:
                    raise ValueError(f"Unknown clip variant column: {key}")
                sets.append(f"{key} = ?")
                params.append(value)
            sets.append("updated_at = ?")
            params.append(now_iso())
            params.append(variant_pk)
            conn.execute(f"UPDATE clip_variants SET {', '.join(sets)} WHERE id = ?", params)
            if "status" in updates and updates["status"] != from_status:
                self._record_variant_status_event(
                    conn,
                    variant_pk,
                    from_status=from_status,
                    to_status=str(updates["status"]),
                )

    def variant_status_events(self, variant_pk: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM variant_status_events
                WHERE variant_pk = ?
                ORDER BY occurred_at ASC, id ASC
                """,
                (variant_pk,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_upload_job(
        self,
        *,
        clip_pk: int,
        variant_pk: int | None = None,
        platform: str,
        channel_id: str | None = None,
        status: str = UploadStatus.REGISTERED,
        idempotency_key: str | None = None,
    ) -> tuple[int, bool]:
        ts = now_iso()
        channel_key = channel_id or ""
        active_variant_pk = variant_pk
        if active_variant_pk is None:
            active_variant_pk, _ = self.ensure_default_variant(clip_pk)
        dedupe_key = idempotency_key or f"registration:{active_variant_pk}:{platform}:{channel_key}"
        with self.connect() as conn:
            clip = conn.execute(
                "SELECT durable_id, source_job_id, clip_id FROM clips WHERE id = ?",
                (clip_pk,),
            ).fetchone()
            if clip is None:
                raise RuntimeError("Cannot create upload job for missing clip")
            target_pk = self._ensure_publication_target_row(
                conn,
                platform=platform,
                channel_id=channel_key,
                created_at=ts,
            )
            existing = conn.execute(
                """
                SELECT id FROM upload_jobs
                WHERE idempotency_key = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (dedupe_key,),
            ).fetchone()
            if existing is not None:
                return int(existing["id"]), False
            publication_id = _new_id("pub")
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO upload_jobs (
                  publication_id, clip_pk, variant_pk, idempotency_key,
                  target_pk, platform, channel_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_id,
                    clip_pk,
                    active_variant_pk,
                    dedupe_key,
                    target_pk,
                    platform,
                    channel_key,
                    status,
                    ts,
                    ts,
                ),
            )
            if cur.rowcount:
                upload_job_id = int(cur.lastrowid)
                self._record_publication_status_event(
                    conn,
                    upload_job_id,
                    from_status=None,
                    to_status=status,
                    reason="publication_created",
                )
                return upload_job_id, True
            row = conn.execute(
                """
                SELECT id FROM upload_jobs
                WHERE idempotency_key = ?
                """,
                (dedupe_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to create or load upload job")
            return int(row["id"]), False

    def list_upload_jobs(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = """
            SELECT uj.*, c.durable_id, c.source_job_pk, c.source_job_id, c.clip_id,
                   c.title AS source_title, c.hook, c.caption,
                   c.duration_sec, c.clip_path, c.job_clip_path, c.composite_score,
                   cv.variant_id, cv.variant_type, cv.status AS variant_status,
                   cv.rendered_asset_path, cv.render_fingerprint,
                   cv.format_json AS variant_format_json,
                   cv.editorial_json AS variant_editorial_json,
                   cv.render_json AS variant_render_json
            FROM upload_jobs uj
            JOIN clips c ON c.id = uj.clip_pk
            LEFT JOIN clip_variants cv ON cv.id = uj.variant_pk
        """
        params: list[Any] = []
        if status:
            sql += " WHERE uj.status = ?"
            params.append(canonical_status(status) or status)
        sql += (
            " ORDER BY COALESCE(uj.publish_at, uj.scheduled_at, uj.upload_at, uj.created_at) ASC,"
            " uj.id ASC LIMIT ?"
        )
        params.append(max(1, min(int(limit), 500)))
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._upload_job_from_row(row) for row in rows]

    def get_upload_job(self, upload_job_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT uj.*, c.durable_id, c.source_job_pk, c.source_job_id, c.clip_id,
                       c.title AS source_title, c.hook, c.caption,
                       c.duration_sec, c.clip_path, c.job_clip_path, c.composite_score,
                       c.scores_json, c.clip_validation_json, c.editorial_metadata_json, c.preflight_json,
                       cv.variant_id, cv.variant_type, cv.status AS variant_status,
                       cv.rendered_asset_path, cv.render_fingerprint,
                       cv.format_json AS variant_format_json,
                       cv.editorial_json AS variant_editorial_json,
                       cv.render_json AS variant_render_json
                FROM upload_jobs uj
                JOIN clips c ON c.id = uj.clip_pk
                LEFT JOIN clip_variants cv ON cv.id = uj.variant_pk
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
        data["editorial_metadata"] = _json_loads(data.pop("editorial_metadata_json", None), {})
        data["source_payload"] = _json_loads(data.pop("source_payload_json", None), {})
        data["preflight"] = _json_loads(data.pop("preflight_json", None), None)
        return data

    def update_upload_job(self, upload_job_id: int, **updates: Any) -> None:
        if not updates:
            return
        allowed = {
            "platform",
            "publication_id",
            "variant_pk",
            "idempotency_key",
            "target_pk",
            "channel_id",
            "status",
            "normalized_title",
            "normalized_description",
            "normalized_hashtags_json",
            "metadata_json",
            "publish_at",
            "platform_publish_at",
            "upload_at",
            "upload_deadline",
            "upload_started_at",
            "uploaded_at",
            "scheduled_at",
            "attempt_count",
            "last_error",
            "platform_asset_id",
            "platform_video_id",
            "platform_state",
            "publish_state",
            "remote_ids_json",
            "adapter_version",
            "api_version",
            "lease_owner",
            "lease_token",
            "lease_heartbeat_at",
            "lease_expires_at",
        }
        if "status" in updates:
            canonical = canonical_status(updates["status"])
            if canonical is not None:
                updates["status"] = canonical
        sets: list[str] = []
        params: list[Any] = []
        from_status: str | None = None
        for key, value in updates.items():
            if key not in allowed:
                raise ValueError(f"Unknown upload job column: {key}")
            sets.append(f"{key} = ?")
            params.append(value)
        sets.append("updated_at = ?")
        params.append(now_iso())
        params.append(upload_job_id)
        with self.connect() as conn:
            if "status" in updates:
                row = conn.execute(
                    "SELECT status FROM upload_jobs WHERE id = ?",
                    (upload_job_id,),
                ).fetchone()
                if row is not None:
                    from_status = row["status"]
            conn.execute(f"UPDATE upload_jobs SET {', '.join(sets)} WHERE id = ?", params)
            if "status" in updates and updates["status"] != from_status:
                self._record_publication_status_event(
                    conn,
                    upload_job_id,
                    from_status=from_status,
                    to_status=str(updates["status"]),
                )

    def _record_publication_status_event(
        self,
        conn: sqlite3.Connection,
        upload_job_id: int,
        *,
        from_status: str | None,
        to_status: str,
        reason: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        job = conn.execute(
            "SELECT publication_id FROM upload_jobs WHERE id = ?",
            (upload_job_id,),
        ).fetchone()
        ts = now_iso()
        conn.execute(
            """
            INSERT INTO publication_status_events (
              upload_job_id, publication_id, from_status, to_status,
              reason, payload_json, occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                upload_job_id,
                None if job is None else job["publication_id"],
                from_status,
                to_status,
                reason,
                _json_dumps(payload or {}),
                ts,
                ts,
            ),
        )

    def set_routed(
        self,
        upload_job_id: int,
        *,
        channel_id: str,
        metadata: MetadataResult,
        profile_snapshot: dict[str, Any],
    ) -> None:
        job = self.get_upload_job(upload_job_id)
        publication_id = None
        target_pk = None
        if job is not None:
            clip_durable_id = job.get("durable_id") or _stable_id(
                "clip", job.get("source_job_id"), job.get("clip_id")
            )
            publication_id = job.get("publication_id")
        with self.connect() as conn:
            target_pk = self._ensure_publication_target_row(
                conn,
                platform=str((job or {}).get("platform") or ""),
                channel_id=channel_id,
                created_at=now_iso(),
                metadata={"profile_snapshot": profile_snapshot},
            )
        self.update_upload_job(
            upload_job_id,
            channel_id=channel_id,
            target_pk=target_pk,
            publication_id=publication_id,
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

    def set_planned(
        self,
        upload_job_id: int,
        *,
        publish_at: str,
        upload_at: str,
        upload_deadline: str,
        platform_publish_at: str | None = None,
    ) -> None:
        self.update_upload_job(
            upload_job_id,
            status=UploadStatus.PLANNED,
            publish_at=publish_at,
            platform_publish_at=platform_publish_at or publish_at,
            upload_at=upload_at,
            upload_deadline=upload_deadline,
            scheduled_at=publish_at,
        )

    def set_uploaded_scheduled(
        self,
        upload_job_id: int,
        *,
        platform_video_id: str | None,
        uploaded_at: str | None = None,
        platform_state: str | None = "private_scheduled",
        publish_state: str | None = None,
        remote_ids: dict[str, Any] | None = None,
        adapter_version: str | None = None,
        api_version: str | None = None,
        lease_token: str | None = None,
    ) -> bool:
        if lease_token:
            job = self.get_upload_job(upload_job_id)
            if job is None or str(job.get("lease_token") or "") != str(lease_token):
                return False
        self.update_upload_job(
            upload_job_id,
            status=UploadStatus.UPLOADED_SCHEDULED,
            uploaded_at=uploaded_at or now_iso(),
            platform_video_id=platform_video_id,
            platform_asset_id=platform_video_id,
            platform_state=platform_state,
            publish_state=publish_state,
            remote_ids_json=_json_dumps(remote_ids or {}),
            adapter_version=adapter_version,
            api_version=api_version,
            lease_owner=None,
            lease_token=None,
            lease_heartbeat_at=None,
            lease_expires_at=None,
            last_error=None,
        )
        return True

    def mark_uploading(self, upload_job_id: int) -> None:
        self.update_upload_job(
            upload_job_id,
            status=UploadStatus.UPLOADING,
            upload_started_at=now_iso(),
        )

    def heartbeat_upload_lease(self, upload_job_id: int, lease_token: str, *, lease_seconds: int = 1800) -> bool:
        now = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE upload_jobs
                SET lease_heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = ? AND lease_token = ?
                """,
                (
                    now,
                    _future_iso(lease_seconds),
                    now,
                    upload_job_id,
                    UploadStatus.UPLOADING,
                    lease_token,
                ),
            )
            return bool(cur.rowcount)

    def release_upload_lease(self, upload_job_id: int, lease_token: str | None = None) -> bool:
        params: list[Any] = [now_iso(), upload_job_id]
        lease_clause = ""
        if lease_token:
            lease_clause = " AND lease_token = ?"
            params.append(lease_token)
        with self.connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE upload_jobs
                SET lease_owner = NULL, lease_token = NULL,
                    lease_heartbeat_at = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE id = ?{lease_clause}
                """,
                params,
            )
            return bool(cur.rowcount)

    def mark_missed_upload_window(self, upload_job_id: int, *, reason: str = "upload_deadline_passed") -> None:
        self.update_upload_job(
            upload_job_id,
            status=UploadStatus.MISSED_UPLOAD_WINDOW,
            last_error=reason,
        )

    def claim_upload_due_jobs(
        self,
        *,
        now: str,
        limit: int = 10,
        lease_owner: str = "upload_worker",
        lease_seconds: int = 1800,
    ) -> list[dict[str, Any]]:
        """Claim jobs whose upload window has opened.

        A job is eligible if its status is ``planned`` (no upload attempt yet)
        or ``pending_upload`` (a previous retryable failure waiting to be
        retried within the deadline), and its ``upload_at`` is at or before
        ``now``. Jobs past their ``upload_deadline`` are intentionally NOT
        claimed here — the caller should sweep them with
        :meth:`list_overdue_uploads` and mark them ``missed_upload_window``
        first.

        Note: ``scheduled_at`` is the deprecated legacy mirror of
        ``publish_at`` and is NOT used to derive upload-eligibility.
        """
        claimed_ids: list[int] = []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, status FROM upload_jobs
                WHERE status IN (?, ?)
                  AND upload_at IS NOT NULL
                  AND upload_at <= ?
                  AND (upload_deadline IS NULL OR upload_deadline > ?)
                ORDER BY upload_at ASC, id ASC
                LIMIT ?
                """,
                (
                    UploadStatus.PLANNED,
                    UploadStatus.PENDING_UPLOAD,
                    now,
                    now,
                    max(1, int(limit)),
                ),
            ).fetchall()
            for row in rows:
                lease_token = _new_id("lease")
                heartbeat_at = now_iso()
                lease_expires_at = _future_iso(lease_seconds)
                cur = conn.execute(
                    """
                    UPDATE upload_jobs
                    SET status = ?, upload_started_at = ?, lease_owner = ?,
                        lease_token = ?, lease_heartbeat_at = ?, lease_expires_at = ?,
                        updated_at = ?
                    WHERE id = ? AND status IN (?, ?)
                    """,
                    (
                        UploadStatus.UPLOADING,
                        heartbeat_at,
                        lease_owner,
                        lease_token,
                        heartbeat_at,
                        lease_expires_at,
                        heartbeat_at,
                        int(row["id"]),
                        UploadStatus.PLANNED,
                        UploadStatus.PENDING_UPLOAD,
                    ),
                )
                if cur.rowcount:
                    self._record_publication_status_event(
                        conn,
                        int(row["id"]),
                        from_status=row["status"],
                        to_status=UploadStatus.UPLOADING,
                        reason="upload_due_claimed",
                    )
                    claimed_ids.append(int(row["id"]))
        return [job for job_id in claimed_ids if (job := self.get_upload_job(job_id)) is not None]

    def list_stalled_jobs(
        self,
        *,
        now: str,
        registered_cutoff: str | None = None,
        routed_cutoff: str | None = None,
        uploading_cutoff: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return upload_jobs that have been in an intermediate status for too long.

        Stalled is defined per-status:

        - ``registered`` / ``routed``: ``updated_at`` is older than the cutoff
          (no progression to ``planned``; usually a missed handoff or
          routing dead-end).
        - ``uploading``: ``upload_started_at`` is older than the cutoff (a
          worker claimed the row but did not finish; e.g. crash mid-upload).

        Callers (the watchdog, ops-ui) compute the ISO cutoff strings from
        configured thresholds and pass them in. ``now`` is included only as a
        debug echo on the returned rows.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if registered_cutoff is not None:
            clauses.append("(uj.status = ? AND uj.updated_at <= ?)")
            params.extend([UploadStatus.REGISTERED, registered_cutoff])
        if routed_cutoff is not None:
            clauses.append("(uj.status = ? AND uj.updated_at <= ?)")
            params.extend([UploadStatus.ROUTED, routed_cutoff])
        if uploading_cutoff is not None:
            clauses.append(
                "(uj.status = ? AND COALESCE(uj.upload_started_at, uj.updated_at) <= ?)"
            )
            params.extend([UploadStatus.UPLOADING, uploading_cutoff])
        if not clauses:
            return []
        params.append(max(1, int(limit)))
        sql = (
            """
            SELECT uj.*, c.source_job_id, c.clip_id, c.title AS source_title,
                   c.duration_sec, c.composite_score
            FROM upload_jobs uj
            JOIN clips c ON c.id = uj.clip_pk
            WHERE """
            + " OR ".join(clauses)
            + " ORDER BY uj.updated_at ASC, uj.id ASC LIMIT ?"
        )
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._upload_job_from_row(row) for row in rows]

    def list_expired_upload_leases(self, *, now: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT uj.*, c.source_job_id, c.clip_id, c.title AS source_title,
                       c.duration_sec, c.composite_score
                FROM upload_jobs uj
                JOIN clips c ON c.id = uj.clip_pk
                WHERE uj.status = ?
                  AND uj.lease_expires_at IS NOT NULL
                  AND uj.lease_expires_at <= ?
                ORDER BY uj.lease_expires_at ASC, uj.id ASC
                LIMIT ?
                """,
                (UploadStatus.UPLOADING, now, max(1, int(limit))),
            ).fetchall()
        return [self._upload_job_from_row(row) for row in rows]

    def return_expired_upload_lease(
        self,
        upload_job_id: int,
        lease_token: str,
        *,
        status: str = UploadStatus.PENDING_UPLOAD,
        reason: str = "lease_expired_after_reconciliation",
    ) -> bool:
        now = now_iso()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status FROM upload_jobs WHERE id = ? AND lease_token = ?",
                (upload_job_id, lease_token),
            ).fetchone()
            cur = conn.execute(
                """
                UPDATE upload_jobs
                SET status = ?, lease_owner = NULL, lease_token = NULL,
                    lease_heartbeat_at = NULL, lease_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ? AND status = ? AND lease_token = ?
                """,
                (
                    status,
                    reason,
                    now,
                    upload_job_id,
                    UploadStatus.UPLOADING,
                    lease_token,
                ),
            )
            if cur.rowcount:
                self._record_publication_status_event(
                    conn,
                    upload_job_id,
                    from_status=None if row is None else row["status"],
                    to_status=status,
                    reason=reason,
                )
            return bool(cur.rowcount)

    def list_overdue_uploads(self, *, now: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return planned/pending jobs whose upload_deadline is already past."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT uj.*, c.source_job_id, c.clip_id, c.title AS source_title, c.hook, c.caption,
                       c.duration_sec, c.clip_path, c.job_clip_path, c.composite_score
                FROM upload_jobs uj
                JOIN clips c ON c.id = uj.clip_pk
                WHERE uj.status IN (?, ?)
                  AND uj.upload_deadline IS NOT NULL
                  AND uj.upload_deadline <= ?
                ORDER BY uj.upload_deadline ASC, uj.id ASC
                LIMIT ?
                """,
                (
                    UploadStatus.PLANNED,
                    UploadStatus.PENDING_UPLOAD,
                    now,
                    max(1, int(limit)),
                ),
            ).fetchall()
        return [self._upload_job_from_row(row) for row in rows]

    def record_attempt(
        self,
        upload_job_id: int,
        *,
        status: str,
        request_summary: dict[str, Any] | None = None,
        response_summary: dict[str, Any] | None = None,
        raw_response: dict[str, Any] | None = None,
        remote_ids: dict[str, Any] | None = None,
        failure_class: str | None = None,
        adapter_version: str | None = None,
        api_version: str | None = None,
        lease_token: str | None = None,
        duration_ms: int | None = None,
        error_category: str | None = None,
        error_message: str | None = None,
        retryable: bool = False,
    ) -> None:
        with self.connect() as conn:
            job = conn.execute(
                "SELECT publication_id, platform, channel_id, attempt_count FROM upload_jobs WHERE id = ?",
                (upload_job_id,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO publish_attempts (
                  upload_job_id, publication_id, attempted_at, status, request_json, response_json,
                  raw_response_json, remote_ids_json, failure_class, adapter_version, api_version,
                  lease_token, duration_ms, error_category, error_message, retryable
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    upload_job_id,
                    None if job is None else job["publication_id"],
                    now_iso(),
                    status,
                    _json_dumps(request_summary or {}),
                    _json_dumps(response_summary or {}),
                    _json_dumps(raw_response or {}),
                    _json_dumps(remote_ids or {}),
                    failure_class,
                    adapter_version,
                    api_version,
                    lease_token,
                    duration_ms,
                    error_category,
                    error_message,
                    1 if retryable else 0,
                ),
            )
            attempt_number = 1 if job is None else int(job["attempt_count"] or 0) + 1
            self._record_upload_audit_event(
                conn,
                upload_job_id,
                publication_id=None if job is None else job["publication_id"],
                platform="" if job is None else str(job["platform"] or ""),
                channel_id="" if job is None else str(job["channel_id"] or ""),
                attempt_number=attempt_number,
                lease_token=lease_token,
                event_type="upload_attempt",
                result=status,
                remote_ids=remote_ids,
                failure_class=failure_class,
                adapter_version=adapter_version,
                api_version=api_version,
                payload={
                    "request": request_summary or {},
                    "response": response_summary or {},
                    "error_category": error_category,
                    "error_message": error_message,
                    "retryable": retryable,
                    "duration_ms": duration_ms,
                },
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

    def _record_upload_audit_event(
        self,
        conn: sqlite3.Connection,
        upload_job_id: int,
        *,
        publication_id: str | None,
        platform: str,
        channel_id: str,
        attempt_number: int | None,
        lease_token: str | None,
        event_type: str,
        result: str | None = None,
        remote_ids: dict[str, Any] | None = None,
        failure_class: str | None = None,
        adapter_version: str | None = None,
        api_version: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        ts = now_iso()
        conn.execute(
            """
            INSERT INTO upload_audit_events (
              upload_job_id, publication_id, platform, channel_id, attempt_number,
              lease_token, event_type, result, remote_ids_json, failure_class,
              adapter_version, api_version, payload_json, occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                upload_job_id,
                publication_id,
                platform,
                channel_id,
                attempt_number,
                lease_token,
                event_type,
                result,
                _json_dumps(remote_ids or {}),
                failure_class,
                adapter_version,
                api_version,
                _json_dumps(payload or {}),
                ts,
                ts,
            ),
        )

    def upload_audit_events(self, upload_job_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM upload_audit_events
                WHERE upload_job_id = ?
                ORDER BY occurred_at ASC, id ASC
                """,
                (upload_job_id,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["remote_ids"] = _json_loads(data.pop("remote_ids_json", None), {})
            data["payload"] = _json_loads(data.pop("payload_json", None), {})
            out.append(data)
        return out

    def account_state(self, *, platform: str, channel_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM account_states WHERE platform = ? AND channel_id = ?",
                (platform, channel_id),
            ).fetchone()
        return None if row is None else dict(row)

    def upsert_account_state(
        self,
        *,
        platform: str,
        channel_id: str,
        rate_limited_until: str | None = None,
        circuit_open_until: str | None = None,
        circuit_failure_count: int | None = None,
        paused_reason: str | None = None,
        token_source: str | None = None,
        token_expires_at: str | None = None,
        token_last_checked_at: str | None = None,
        token_last_refresh_at: str | None = None,
        token_last_refresh_error: str | None = None,
        last_failure_class: str | None = None,
        last_failure_at: str | None = None,
    ) -> None:
        current = self.account_state(platform=platform, channel_id=channel_id) or {}
        now = now_iso()
        values = {
            "rate_limited_until": rate_limited_until if rate_limited_until is not None else current.get("rate_limited_until"),
            "circuit_open_until": circuit_open_until if circuit_open_until is not None else current.get("circuit_open_until"),
            "circuit_failure_count": (
                circuit_failure_count if circuit_failure_count is not None else int(current.get("circuit_failure_count") or 0)
            ),
            "paused_reason": paused_reason if paused_reason is not None else current.get("paused_reason"),
            "token_source": token_source if token_source is not None else current.get("token_source"),
            "token_expires_at": token_expires_at if token_expires_at is not None else current.get("token_expires_at"),
            "token_last_checked_at": (
                token_last_checked_at if token_last_checked_at is not None else current.get("token_last_checked_at")
            ),
            "token_last_refresh_at": (
                token_last_refresh_at if token_last_refresh_at is not None else current.get("token_last_refresh_at")
            ),
            "token_last_refresh_error": (
                token_last_refresh_error if token_last_refresh_error is not None else current.get("token_last_refresh_error")
            ),
            "last_failure_class": last_failure_class if last_failure_class is not None else current.get("last_failure_class"),
            "last_failure_at": last_failure_at if last_failure_at is not None else current.get("last_failure_at"),
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO account_states (
                  platform, channel_id, rate_limited_until, circuit_open_until,
                  circuit_failure_count, paused_reason, token_source, token_expires_at,
                  token_last_checked_at, token_last_refresh_at, token_last_refresh_error,
                  last_failure_class, last_failure_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, channel_id) DO UPDATE SET
                  rate_limited_until = excluded.rate_limited_until,
                  circuit_open_until = excluded.circuit_open_until,
                  circuit_failure_count = excluded.circuit_failure_count,
                  paused_reason = excluded.paused_reason,
                  token_source = excluded.token_source,
                  token_expires_at = excluded.token_expires_at,
                  token_last_checked_at = excluded.token_last_checked_at,
                  token_last_refresh_at = excluded.token_last_refresh_at,
                  token_last_refresh_error = excluded.token_last_refresh_error,
                  last_failure_class = excluded.last_failure_class,
                  last_failure_at = excluded.last_failure_at,
                  updated_at = excluded.updated_at
                """,
                (
                    platform,
                    channel_id,
                    values["rate_limited_until"],
                    values["circuit_open_until"],
                    values["circuit_failure_count"],
                    values["paused_reason"],
                    values["token_source"],
                    values["token_expires_at"],
                    values["token_last_checked_at"],
                    values["token_last_refresh_at"],
                    values["token_last_refresh_error"],
                    values["last_failure_class"],
                    values["last_failure_at"],
                    now,
                ),
            )

    def record_account_success(self, *, platform: str, channel_id: str) -> None:
        self.upsert_account_state(
            platform=platform,
            channel_id=channel_id,
            rate_limited_until="",
            circuit_open_until="",
            circuit_failure_count=0,
            paused_reason="",
            last_failure_class="",
            last_failure_at="",
        )

    def record_account_failure(
        self,
        *,
        platform: str,
        channel_id: str,
        failure_class: str | None,
        retry_after_seconds: int | None = None,
        circuit_threshold: int = 3,
        circuit_pause_seconds: int = 900,
    ) -> None:
        current = self.account_state(platform=platform, channel_id=channel_id) or {}
        failure_count = int(current.get("circuit_failure_count") or 0) + 1
        rate_limited_until = None
        circuit_open_until = None
        paused_reason = current.get("paused_reason")
        if retry_after_seconds is not None and retry_after_seconds > 0:
            rate_limited_until = _future_iso(retry_after_seconds)
            paused_reason = "rate_limited"
        if failure_count >= max(1, circuit_threshold):
            circuit_open_until = _future_iso(circuit_pause_seconds)
            paused_reason = f"circuit_open:{failure_class or 'unknown'}"
        self.upsert_account_state(
            platform=platform,
            channel_id=channel_id,
            rate_limited_until=rate_limited_until,
            circuit_open_until=circuit_open_until,
            circuit_failure_count=failure_count,
            paused_reason=paused_reason,
            last_failure_class=failure_class,
            last_failure_at=now_iso(),
        )

    def record_clip_metric(
        self,
        clip_pk: int,
        *,
        metric_name: str,
        metric_value: float | None = None,
        metric: dict[str, Any] | None = None,
        source: str | None = None,
        observed_at: str | None = None,
        metric_unit: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        dimensions: dict[str, Any] | None = None,
    ) -> int:
        observed = observed_at or now_iso()
        created = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR REPLACE INTO clip_metrics (
                  clip_pk, metric_name, metric_value, metric_json, source, observed_at,
                  created_at, metric_unit, window_start, window_end, dimensions_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clip_pk,
                    metric_name,
                    metric_value,
                    _json_dumps(metric or {}),
                    source,
                    observed,
                    created,
                    metric_unit,
                    window_start,
                    window_end,
                    _json_dumps(dimensions or {}),
                ),
            )
            return int(cur.lastrowid)

    def record_publication_metric(
        self,
        upload_job_id: int,
        *,
        metric_name: str,
        metric_value: float | None = None,
        metric: dict[str, Any] | None = None,
        source: str | None = None,
        observed_at: str | None = None,
        metric_unit: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        dimensions: dict[str, Any] | None = None,
    ) -> int:
        observed = observed_at or now_iso()
        created = now_iso()
        with self.connect() as conn:
            job = conn.execute(
                "SELECT publication_id FROM upload_jobs WHERE id = ?",
                (upload_job_id,),
            ).fetchone()
            cur = conn.execute(
                """
                INSERT OR REPLACE INTO publication_metrics (
                  upload_job_id, publication_id, metric_name, metric_value,
                  metric_json, source, observed_at, created_at, metric_unit,
                  window_start, window_end, dimensions_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    upload_job_id,
                    None if job is None else job["publication_id"],
                    metric_name,
                    metric_value,
                    _json_dumps(metric or {}),
                    source,
                    observed,
                    created,
                    metric_unit,
                    window_start,
                    window_end,
                    _json_dumps(dimensions or {}),
                ),
            )
            return int(cur.lastrowid)

    def record_analytics_event(
        self,
        event_type: str,
        *,
        occurred_at: str | None = None,
        clip_pk: int | None = None,
        upload_job_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        ts = now_iso()
        with self.connect() as conn:
            publication_id = None
            if upload_job_id is not None:
                job = conn.execute(
                    "SELECT publication_id FROM upload_jobs WHERE id = ?",
                    (upload_job_id,),
                ).fetchone()
                publication_id = None if job is None else job["publication_id"]
            cur = conn.execute(
                """
                INSERT INTO analytics_events (
                  event_type, occurred_at, clip_pk, upload_job_id, publication_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    occurred_at or ts,
                    clip_pk,
                    upload_job_id,
                    publication_id,
                    _json_dumps(payload or {}),
                    ts,
                ),
            )
            return int(cur.lastrowid)

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

    def publication_status_events(self, upload_job_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM publication_status_events
                WHERE upload_job_id = ?
                ORDER BY occurred_at ASC, id ASC
                """,
                (upload_job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def existing_publish_times(
        self,
        *,
        platform: str,
        channel_id: str,
        statuses: Iterable[str] = (
            UploadStatus.PLANNED,
            UploadStatus.PENDING_UPLOAD,
            UploadStatus.UPLOADING,
            UploadStatus.UPLOADED_SCHEDULED,
            UploadStatus.PUBLISHED,
        ),
    ) -> list[str]:
        statuses_list = list(statuses)
        placeholders = ", ".join("?" for _ in statuses_list)
        params: list[Any] = [platform, channel_id, *statuses_list]
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT COALESCE(publish_at, scheduled_at) AS pat FROM upload_jobs
                WHERE platform = ? AND channel_id = ?
                  AND COALESCE(publish_at, scheduled_at) IS NOT NULL
                  AND status IN ({placeholders})
                ORDER BY pat ASC
                """,
                params,
            ).fetchall()
        return [str(row["pat"]) for row in rows if row["pat"]]

    def existing_scheduled_times(
        self,
        *,
        platform: str,
        channel_id: str,
        statuses: Iterable[str] | None = None,
    ) -> list[str]:
        """Deprecated: use `existing_publish_times`. Kept for backward compat."""
        if statuses is None:
            return self.existing_publish_times(platform=platform, channel_id=channel_id)
        return self.existing_publish_times(
            platform=platform, channel_id=channel_id, statuses=statuses
        )

    @staticmethod
    def _upload_job_from_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["normalized_hashtags"] = _json_loads(data.pop("normalized_hashtags_json", None), [])
        data["metadata"] = _json_loads(data.pop("metadata_json", None), {})
        data["remote_ids"] = _json_loads(data.pop("remote_ids_json", None), {})
        for key in ("scores_json", "clip_validation_json", "editorial_metadata_json", "preflight_json"):
            if key in data:
                data[key[:-5] if key.endswith("_json") else key] = _json_loads(data.pop(key), {})
        for key in ("variant_format_json", "variant_editorial_json", "variant_render_json"):
            if key in data:
                data[key[:-5] if key.endswith("_json") else key] = _json_loads(data.pop(key), {})
        return data
