# Disk Pressure Checks (Phase 7)

Disk pressure awareness protects production by classifying storage health and
refusing new production pipeline runs before disk exhaustion causes unpredictable
job failures.

This phase does **not** delete files, run retention dry-run/apply, or schedule
retention. High pressure may recommend retention; execution remains
operator-controlled until later phases.

## Configuration

Thresholds come from merged retention policy configuration (Phase 2):

| Key | Role |
| --- | --- |
| `storage.disk_pressure.warning_percent` | First elevated state |
| `storage.disk_pressure.urgent_percent` | Stronger warning |
| `storage.disk_pressure.critical_percent` | Critical storage pressure |
| `storage.disk_pressure.reject_new_jobs_percent` | Block new production pipeline runs |

Defaults live in `config/system/system.yaml` with optional overrides in
`config/environments/{dev,prod}.yaml`. Validation enforces ascending order.

## Pressure levels

Classification is deterministic from usage percent and configured thresholds:

| Level | Meaning |
| --- | --- |
| `NORMAL` | Below warning threshold |
| `WARNING` | At or above warning |
| `URGENT` | At or above urgent |
| `CRITICAL` | At or above critical |
| `REJECT_NEW_JOBS` | At or above reject threshold |

At `CRITICAL` and `REJECT_NEW_JOBS`, health/status report that retention is
**recommended**. No retention planner or apply mode is invoked automatically.

## Measured fields

For the environment data root (or repo root fallback):

* total bytes
* used bytes
* free bytes
* usage percent

## Health and status integration

* **Health** (`scripts/ops/health_report.py`): `Disk pressure` check uses the
  shared disk pressure module. Detail includes `storage_state=<LEVEL>`.
* **Status** (`scripts/ops/status_report.py`): resource lines include disk
  usage, free space, and `Storage state`.
* **Observability** (`scripts/observability/populate.py`): `SystemHealth.disk`
  carries `pressure_state` and `retention_recommended`.

## Production job gate

`can_start_new_job(environment, resolved_config)` in
`scripts/storage/disk_pressure.py`:

* **Development**: always allowed (pressure still reported).
* **Production**: allowed below `reject_new_jobs_percent`; blocked at or above.

When blocked, the pipeline entrypoint (`scripts/ops/run_pipeline.py`):

1. Records a `SKIPPED` run record with an explicit reason.
2. Appends structured evidence to
   `data/<env>/storage/disk_pressure_blocks.jsonl`.

Blocked runs are never silent refusals.

## Relationship to retention

| Concern | Phase 7 | Later phases |
| --- | --- | --- |
| Classify disk pressure | Yes | — |
| Recommend retention | Yes (message only) | — |
| Dry-run planner | No | Operator / scheduled |
| Apply mode | No | Operator / scheduled |
| Automatic deletion | No | Scheduled retention only when implemented |

Retention planner and apply behaviour are unchanged.

## Module reference

* `scripts/storage/disk_pressure.py` — thresholds, classification, gate, block records
* `load_disk_pressure_thresholds`, `evaluate_disk_pressure`, `can_start_new_job`,
  `record_disk_pressure_block`
