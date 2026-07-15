# Remote Operations Scripts

SSH-based operational commands for `dev` and `prod`. Every script supports
`-h` / `--help`. Production is never the default.

**Operator guide:** [docs/operations/RUNBOOK.md](../../docs/operations/RUNBOOK.md)

## Command surface

```bash
./scripts/ops/status.sh prod
./scripts/ops/health.sh prod
./scripts/ops/logs.sh prod errors
./scripts/ops/restart.sh prod worker
./scripts/ops/run-pipeline.sh prod --funnel-id <funnel_id>
./scripts/ops/disable-uploads.sh prod
./scripts/ops/enable-uploads.sh prod --confirm
./scripts/ops/stop-scheduler.sh prod
./scripts/ops/start-scheduler.sh prod --confirm
./scripts/ops/scheduler-status.sh prod
./scripts/ops/backup.sh prod
./scripts/ops/cleanup.sh prod --dry-run
```

Updates use **repo-root** `./update.sh`. `scripts/ops/update.sh` is still a stub
and is not the authoritative update path.

## Scripts

| Script | Purpose | Status |
|--------|---------|--------|
| `status.sh` | Quick read-only summary (includes boot readiness) | Implemented |
| `health.sh` | Deep diagnostics + boot verification (`0` PASS / `1` WARN / `2` FAIL) | Implemented |
| `run-pipeline.sh` | Shared pipeline entrypoint (config + readiness + POST /run-funnel) | Implemented |
| `run-scheduled.sh` | Thin scheduler trigger (`--trigger scheduled` only) | Implemented |
| `logs.sh` | Bounded, best-effort redacted logs | Implemented |
| `restart.sh` | Controlled systemd restart + health | Implemented |
| `disable-uploads.sh` | Runtime upload kill switch | Implemented |
| `enable-uploads.sh` | Clear runtime upload disable | Implemented |
| `stop-scheduler.sh` | **Canonical** pause of new scheduled runs | Implemented |
| `start-scheduler.sh` | **Canonical** resume of scheduled runs | Implemented |
| `scheduler-status.sh` | **Canonical** scheduler control status | Implemented |
| `backup.sh` | Small operational state backup | Implemented |
| `cleanup.sh` | Dry-run cleanup status (apply deferred) | Implemented |
| `update.sh` | Stub — use repo-root `./update.sh` | Stub |

## Pipeline entrypoint

All production pipeline triggers must use:

```bash
./scripts/ops/run-pipeline.sh <env> --funnel-id <id> [--trigger manual_cli|scheduled|...]
```

Exit codes: `0` success, `1` pipeline failure, `2` usage, `3` config, `4` not ready,
`5` lock held (skipped). Each run writes `runs/<env>/<run_id>/run_record.json`
and `run.log`. Lock file: `data/<env>/pipeline_execution.lock`. See
[docs/operations/RUN_RECORDS.md](../../docs/operations/RUN_RECORDS.md).
Cron calls `run-scheduled.sh`, which delegates here with `--trigger scheduled`.
See [docs/operations/SCHEDULER.md](../../docs/operations/SCHEDULER.md).

## Boundaries (short)

- Upload stop does not delete clips or stop processing.
- Scheduler stop does not kill running jobs or disable uploads.
- Restart does not recover half-completed jobs.
- `run-pipeline` acquires a per-environment execution lock; overlapping runs are skipped.
- Stale locks are reported and block new runs but are not auto-deleted.
- `run-pipeline` does not recover half-completed jobs.
- Cleanup currently deletes nothing; `--apply` refuses until Storage & Data Management retention planning exists.
- Operator-facing name is **scheduler** (backend may be cron).

## Shared helpers

- `lib/common.sh` — environment validation and Python discovery
- `update_lib.sh` — used by repo-root `./update.sh` only

## Smoke test

```bash
python scripts/smoke/smoke_reliability.py --env dev
python scripts/smoke/smoke_remote_operations.py --env dev
python scripts/smoke/smoke_remote_operations.py --env prod --safe-only
python scripts/smoke/smoke_restart_recovery.py --env dev
```

Reliability smoke (Phase 11): end-to-end Reliability & Recovery validation.
Safe-only remote ops: status/health/logs, restart dry-run, cleanup dry-run,
scheduler-status, help checks, and prod confirmation-guard refusals. Does not
mutate upload/scheduler state, restart services for real, delete files, or
trigger uploads.

Restart recovery (Phase 4): policy-only by default; `--execute` kills one
service MainPID at a time and verifies systemd recovery (prod needs `--confirm`).

See [docs/operations/RELIABILITY_SMOKE.md](../../docs/operations/RELIABILITY_SMOKE.md),
[docs/operations/REMOTE_OPERATIONS_SMOKE.md](../../docs/operations/REMOTE_OPERATIONS_SMOKE.md),
and [docs/operations/RESTART_RECOVERY.md](../../docs/operations/RESTART_RECOVERY.md).

## Related docs

- [docs/operations/RUNBOOK.md](../../docs/operations/RUNBOOK.md)
- [docs/operations/REMOTE_OPERATIONS_SMOKE.md](../../docs/operations/REMOTE_OPERATIONS_SMOKE.md)
- [docs/operations/README.md](../../docs/operations/README.md)
- [docs/operations/SSH_ACCESS.md](../../docs/operations/SSH_ACCESS.md)
