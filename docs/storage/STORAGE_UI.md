# Operations UI Storage View (Phase 11)

Read-only Storage page in the Operations UI.

Route: `/ops/storage`

The page presents operational storage state. It does **not** run retention,
rotation, backup, or deletion.

## What operators see

* Overall storage health badge
* Disk usage (percent, used/free/capacity, pressure level)
* Storage roots (data, jobs, logs, reports, database, backups)
* Latest retention status (scheduled record + report summary)
* Latest database backup status (integrity, size, location)
* Latest log rotation status
* Informational warnings (disk pressure, stale/failed retention or backup)
* Allowlisted report downloads

## Backend integration

| UI section | Source |
| --- | --- |
| Disk | `storage.disk_pressure.evaluate_disk_pressure` (+ health disk when present) |
| Paths | `EnvironmentStatePaths` / `resolve_backup_dir` |
| Retention report | `storage.retention_report.load_latest_retention_report` |
| Scheduled retention | `storage.retention_schedule.load_latest_scheduled_retention` |
| Database backup | `storage.database_backup.load_latest_backup_record` |
| Log rotation | `storage.log_rotation.load_latest_rotation_record` |

Report downloads use `/ops/storage/artifact/<kind>` and only serve known records
under environment roots.

## Explicit non-goals

* No delete / cleanup / apply / backup-now buttons
* No filesystem browser
* No duplicated retention or disk-pressure logic
