# Database Backup Rotation (Phase 10)

Safe local SQLite backups for the optional ConfigManager placeholder database
(`paths.database_path`). This is **not** the output-funnel or Ops UI database,
and is **not** replication, remote backup, or disaster-recovery orchestration.

```text
Live database (never a retention candidate)
    ↓
SQLite backup API (read-only source)
    ↓
backups/<env>/database/db_<env>_<timestamp>.sqlite3
    ↓
Artifact classifier → database_backup
    ↓
Retention (database_backups_days)
```

## Configuration

`storage.database_backup`:

| Key | Meaning |
| --- | --- |
| `enabled` | Whether scheduled/manual backup runs create snapshots |
| `verify_integrity` | Run `PRAGMA integrity_check` before publishing |
| `location` | Directory template (`{env}` → `dev` / `prod`) |

Expiry uses **`storage.retention.database_backups_days`**. There is no separate
backup deletion policy.

`backups` is included in `storage.allowed_delete_roots` so retention may remove
**backup files only**. The live database path is always classified as
`database` and remains protected.

## Backup process

1. Load config; skip if disabled.
2. Verify `paths.database_path` exists.
3. Write to a temporary file under the backup directory.
4. Use the SQLite backup API with a **read-only** source connection.
5. Optionally run integrity verification on the temp file.
6. Atomically promote temp → final `db_<env>_<timestamp>.sqlite3`.
7. Write sidecar manifest JSON.
8. Record outcome under `data/<env>/storage/database_backup_latest.json`.

On failure:

* the live database is never modified
* partial temp files are removed
* previous successful backups are left intact
* failure is recorded with an explicit reason

## Scheduling

Reuses the Reliability & Recovery cron mechanism:

```text
cron
    ↓
scripts/ops/run-database-backup.sh <env>
    ↓
scripts/ops/run_database_backup.py
    ↓
storage.database_backup.run_database_backup
```

Production cron: `0 3 * * *` (before log rotation and scheduled retention).

Manual:

```bash
./scripts/ops/run-database-backup.sh prod
```

## Observability

Each run writes:

* `data/<env>/storage/database_backup_latest.json`
* append to `data/<env>/storage/database_backup_history.jsonl`

Fields include timestamp, database path, backup path, size, backup count,
integrity result, and `retention_database_backups_days`.

## Relationship with retention

| Concern | Owner |
| --- | --- |
| Create snapshots | Database backup |
| Classify backup files | Artifact classifier (`database_backup`) |
| Protect live database | Classifier + `protected_artifact_types` + apply safety |
| Delete expired backups | Retention (`database_backups_days`) |

Operational tar.gz backups from `scripts/ops/backup_control.py` remain a
separate, broader operational archive. Dedicated SQLite snapshots use the
`db_*` naming under `backups/<env>/database/`.

## Restore expectations (high level)

A successful backup is a standalone SQLite file. Restore is an operator action:

1. Stop writers that use the live database.
2. Replace (or copy aside) the live database with the chosen backup file.
3. Restart services and verify health.

This phase does not automate restore.

## Operational recommendations

1. Keep `verify_integrity: true` in production.
2. Confirm scheduled retention dry-run reports include aged `database_backup`
   files before enabling retention apply.
3. Inspect `database_backup_latest.json` after failures; prior backups should
   still be present.
