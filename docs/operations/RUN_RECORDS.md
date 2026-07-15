# Pipeline Run Records

Reliability & Recovery **Phase 8**.

Canonical history of every pipeline execution. Wrapped by the Operations &
Observability contract as `RunSummary` (see
[observability_contract.md](./observability_contract.md)). Not UI-specific state.

## Location

Environment-separated (never mixed):

```text
runs/dev/<run_id>/
  run_record.json
  run.log
  resolved_config.yaml   # when config validation succeeded

runs/prod/<run_id>/
  ...
```

`run_id` format: `run_<UTC_timestamp>_<trigger>`  
Example: `run_20260704T001200Z_manual_cli`

## Schema (`run_record.json`)

| Field | Type | Notes |
| --- | --- | --- |
| `run_id` | string | Unique within environment |
| `environment` | string | `dev` or `prod` |
| `trigger` | string | `scheduled` \| `manual_cli` \| `operations_ui` \| `remote_ssh` \| `test` |
| `funnel_id` | string | Source-input funnel id |
| `started_at` | ISO-8601 UTC | |
| `finished_at` | ISO-8601 UTC \| null | Set on terminal status |
| `duration_seconds` | number \| null | `finished_at - started_at` |
| `status` | string | See statuses below |
| `failure_reason` | string \| null | Present on FAIL / SKIPPED |
| `jobs_started` | int | Orchestration-level counters |
| `jobs_completed` | int | |
| `jobs_failed` | int | |
| `log_path` | string | Path to `run.log` |
| `report_paths` | string[] | Reserved for linked job reports |
| `code_commit` | string \| null | Short git SHA when available |
| `config_snapshot_path` | string \| null | Path to `resolved_config.yaml` |
| `exit_code` | int \| null | Entrypoint exit code |
| `detail` | string \| null | Operator-facing detail |
| `schema_version` | int | Currently `1` |

### Status values

Terminal only (except brief `RUNNING` while a lock-held run is in progress):

| Status | Meaning |
| --- | --- |
| `RUNNING` | Lock acquired; pipeline invoke in progress |
| `SUCCESS` | Pipeline entrypoint completed successfully |
| `FAIL` | Config, readiness, invoke, or unexpected error |
| `SKIPPED` | Scheduler runtime disable, or execution lock held/stale |

Every run ends in a **terminal** status. `ensure_terminal` forces `FAIL` if a
process dies while `RUNNING`.

## Lifecycle

```text
prepare run dir + run.log
    ↓
config validation ──FAIL──▶ terminal FAIL record
    ↓
boot readiness ──FAIL──▶ terminal FAIL record
    ↓
scheduled gate ──skip──▶ terminal SKIPPED record
    ↓
acquire execution lock ──blocked──▶ terminal SKIPPED record
    ↓
create RUNNING record (+ config snapshot)
    ↓
POST /run-funnel
    ↓
finalize SUCCESS or FAIL
    ↓
release lock (finally)
```

Exactly **one** `run_record.json` per `run_id`. Finalisation is idempotent:
terminal records are never reopened.

## Operational behaviour

Create / inspect:

```bash
./scripts/ops/run-pipeline.sh prod --funnel-id <id> --trigger manual_cli
# prints run_id, log_path, record_path, status

./scripts/ops/status.sh prod    # Last pipeline run line
./scripts/ops/health.sh prod    # Last pipeline run check
```

Records are written even when:

* config validation fails
* boot readiness is NOT READY
* the execution lock blocks the run
* the pipeline invoke fails
* an unexpected exception occurs

## Failure behaviour

| Condition | status | exit code |
| --- | --- | --- |
| Config invalid | `FAIL` | 3 |
| Boot NOT READY | `FAIL` | 4 |
| Scheduler disabled (scheduled only) | `SKIPPED` | 0 |
| Lock held / stale | `SKIPPED` | 5 |
| Pipeline invoke error | `FAIL` | 1 |
| Unexpected exception | `FAIL` | 1 |

## Implementation

| Module | Role |
| --- | --- |
| `scripts/ops/run_records.py` | Schema, write/read, list, finalise |
| `scripts/ops/run_pipeline.py` | Lifecycle integration |
| `scripts/ops/run-pipeline.sh` | Operator entrypoint |

## Out of scope (later)

* Scheduler implementation (Phase 9)
* Run history UI / HTTP APIs (Operations & Observability)
* Job-level recovery or partial resume
* Automatic indexing beyond filesystem layout
