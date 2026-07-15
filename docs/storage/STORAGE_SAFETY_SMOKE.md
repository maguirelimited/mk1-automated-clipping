# Storage Safety & Integration Smoke Test

**Phase 12** — final validation milestone for Storage & Data Management.

Proves the completed storage subsystem is **safe for production**: it deletes only
what policy allows while protecting operational evidence and business outputs.
Does not add storage features or duplicate retention logic.

## What it validates

| Area | Checks |
| --- | --- |
| **End-to-end retention** | Dry-run identifies eligible artifacts without deleting; apply deletes only eligible; reports and reclaimed bytes |
| **Safety rules** | Active jobs, failed-job extended retention, unknown files, final clips, unuploaded outputs, live database, allowed roots only, symlinks, real-path validation |
| **Disk pressure** | Warning / urgent / critical / reject-new-jobs thresholds; blocked run evidence |
| **Scheduled retention** | Dry-run and apply modes, report generation, failure recording, no protected artifact removal |
| **Log rotation** | Active logs preserved; rotated logs classified; retention expiry on rotated logs; failure recording |
| **Database backup** | Backup creation, live DB protection, metadata records, failure without data loss |
| **Operations UI** | Storage page context reflects backend records (disk, retention, backup, rotation, links) |
| **Regression** | UI does not import apply/planner; required modules and ops entrypoints present |

## Run

```bash
# Full integration suite (isolated temp repos — safe everywhere)
video-automation/.venv/bin/python -m pytest tests/smoke/test_storage_safety_smoke.py -q

# CLI wrapper (static checks + pytest + optional live read-only records)
python scripts/smoke/smoke_storage.py
python scripts/smoke/smoke_storage.py --env dev
python scripts/smoke/smoke_storage.py --env prod
```

Exit codes:

| Exit | Meaning |
| --- | --- |
| `0` | PASS or WARN (SKIPs allowed) |
| `1` | FAIL |
| `2` | Usage error |

Reports:

```text
reports/<env|all>/storage_smoke/smoke_<token>_<timestamp>.json
reports/<env|all>/storage_smoke/latest.json
```

## Manual verification (live host)

After scheduled maintenance windows have run at least once:

```bash
python scripts/smoke/smoke_storage.py --env prod

# Read-only operator view
curl -s http://127.0.0.1:<ops-ui-port>/ops/storage | head

# Latest operational records (no mutation)
cat data/prod/storage/scheduled_retention_latest.json
cat data/prod/storage/log_rotation_latest.json
cat data/prod/storage/database_backup_latest.json
cat reports/prod/retention/latest.json
```

Confirm:

1. Disk usage level on Storage page matches `health.sh` / `evaluate_disk_pressure`.
2. Retention scheduled mode matches config (`dry_run` in production by default).
3. Database backup latest record references a file under `backups/prod/database/`.
4. No protected artifact types appear in apply `deletions` with outcome `DELETED`.

## Architectural constraints

* Validation only — no new storage architecture
* Single source of truth per concern (planner, apply, disk pressure, rotation, backup)
* Operations UI is read-only over existing loaders
* Smoke uses real module interfaces with isolated filesystem fixtures

## Subsystem complete

If this smoke exits 0 and manual checks above pass, Storage & Data Management is
production-ready for operational use.
