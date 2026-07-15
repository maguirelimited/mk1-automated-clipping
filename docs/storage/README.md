# Storage & Data Management

Documentation for intentional artifact lifecycles on the production machine.

| Document | Description |
| --- | --- |
| [STORAGE_INVENTORY.md](./STORAGE_INVENTORY.md) | **Phase 1 — Storage inventory** (locations, owners, classification labels). No deletion behaviour. |
| [ARTIFACT_CLASSIFICATION.md](./ARTIFACT_CLASSIFICATION.md) | **Phase 3 — Artifact classification** (structured `ArtifactRecord`). No retention planning or deletion. |
| [RETENTION_DRY_RUN.md](./RETENTION_DRY_RUN.md) | **Phase 4 — Retention dry-run planner** (policy evaluation + JSON report). No deletion. |
| [RETENTION_APPLY.md](./RETENTION_APPLY.md) | **Phase 5 — Safe apply mode** (executes approved plan + safety checks). |
| [RETENTION_REPORTS.md](./RETENTION_REPORTS.md) | **Phase 6 — Retention reports** (versioned schema, latest pointer, loaders). |
| [DISK_PRESSURE.md](./DISK_PRESSURE.md) | **Phase 7 — Disk pressure checks** (classification, health, production job gate). |
| [SCHEDULED_RETENTION.md](./SCHEDULED_RETENTION.md) | **Phase 8 — Scheduled retention** (config-driven caller of planner/apply). |
| [LOG_ROTATION.md](./LOG_ROTATION.md) | **Phase 9 — Log rotation** (bounds active logs; retention owns expiry). |
| [DATABASE_BACKUP.md](./DATABASE_BACKUP.md) | **Phase 10 — Database backup** (SQLite snapshots; retention owns expiry). |
| [STORAGE_UI.md](./STORAGE_UI.md) | **Phase 11 — Operations UI Storage View** (read-only operator dashboard). |
| [STORAGE_SAFETY_SMOKE.md](./STORAGE_SAFETY_SMOKE.md) | **Phase 12 — Storage Safety & Integration smoke** (end-to-end safety validation). |

| Phase | Status |
| --- | --- |
| 1 Inventory | Complete (documentation) |
| 2 Retention policy configuration | Complete (config + validation only) |
| 3 Artifact classification | Complete (metadata only) |
| 4 Retention dry-run planner | Complete (plan only — no deletion) |
| 5 Safe apply mode | Complete (controlled deletion) |
| 6 Retention reports | Complete (stable operational interface) |
| 7 Disk pressure checks | Complete (awareness + production gate — no automatic cleanup) |
| 8 Scheduled retention | Complete (conservative automation — production defaults to dry-run) |
| 9 Log rotation | Complete (active log bounding; retention owns expiry) |
| 10 Database backup | Complete (SQLite snapshots; retention owns expiry) |
| 11 Operations UI Storage View | Complete (read-only operator dashboard) |
| 12 Storage Safety & Integration smoke | Complete (end-to-end safety validation) |

Related:

* Environment paths: `scripts/config/state_paths.py`, `config/environments/*.yaml`
* Retention policy (config only, not enforced): `config/system/system.yaml` → `storage.*`, with optional overrides in `config/environments/{dev,prod}.yaml`
* Classifier: `scripts/storage/artifact_classifier.py`
* Dry-run planner: `scripts/storage/retention_planner.py`
* Apply executor: `scripts/storage/retention_apply.py`
* Reports: `scripts/storage/retention_report.py` (`load_retention_report`, `load_latest_retention_report`)
* Disk pressure: `scripts/storage/disk_pressure.py` (`can_start_new_job`, `evaluate_disk_pressure`)
* Scheduled retention: `scripts/storage/retention_schedule.py` (`run_scheduled_retention`)
* Log rotation: `scripts/storage/log_rotation.py` (`run_log_rotation`)
* Database backup: `scripts/storage/database_backup.py` (`run_database_backup`)
* CLI: `scripts/retention.py` (manual)
* Scheduled trigger: `scripts/ops/run-scheduled-retention.sh`
* Log rotation trigger: `scripts/ops/run-log-rotation.sh`
* Database backup trigger: `scripts/ops/run-database-backup.sh`
* Observability artifact listing (UI): `scripts/observability/artifacts.py` (separate from retention classification)
* Shared type names: `scripts/storage/artifact_types.py`
