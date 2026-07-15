# Scheduled Retention (Phase 8)

Scheduled retention automates **when** the existing retention engine runs.
It does not change **what** is eligible for deletion.

Rollout remains intentionally conservative:

```text
Manual dry-run
    ↓
Manual apply
    ↓
Scheduled dry-run   ← production default
    ↓
Scheduled apply (explicit opt-in)
    ↓
Disk-pressure-triggered apply (future — not implemented)
```

## Configuration

Scheduling is controlled by `storage.schedule` in merged config:

| Key | Values | Meaning |
| --- | --- | --- |
| `enabled` | `true` / `false` | Whether the scheduled entrypoint may execute work |
| `mode` | `disabled` / `dry_run` / `apply` | What the schedule does when enabled |
| `frequency` | `daily` / `weekly` | Declared cadence (cron fires the trigger) |

### Production defaults

```yaml
storage:
  schedule:
    enabled: true
    mode: dry_run
    frequency: daily
```

Production therefore runs a **scheduled dry-run** only. No files are deleted
unless an operator later sets `mode: apply` **and** `storage.retention.enabled: true`.

### Development defaults

```yaml
storage:
  schedule:
    enabled: false
    mode: disabled
    frequency: daily
```

Dev uses the manual CLI by default.

### Scheduled apply opt-in

Scheduled apply requires **both**:

1. `storage.schedule.mode: apply`
2. `storage.schedule.enabled: true`
3. `storage.retention.enabled: true` (enforced at runtime)

If apply is configured but retention policy is disabled, the run is recorded as
`FAIL` with an explicit reason. No files are deleted.

## Scheduler integration

Uses the Reliability & Recovery cron scheduler — not a second mechanism.

```text
cron
    ↓
scripts/ops/run-scheduled-retention.sh <env>
    ↓
scripts/ops/run_scheduled_retention.py
    ↓
storage.retention_schedule.run_scheduled_retention
    ↓
existing run_retention_dry_run  or  RetentionPlanner + run_retention_apply
```

Install remains `deploy/scripts/install-scheduler.sh`. Cron lines live in
`deploy/cron/mk04.crontab` and `deploy/cron/mk04.cron.d`.

Pipeline scheduling (`run-scheduled.sh`) is unchanged and separate.
`stop-scheduler` / `start-scheduler` control pipeline runs only; retention
enablement is config-driven via `storage.schedule`.

## Execution outcomes

| Outcome | Status | Exit |
| --- | --- | --- |
| Schedule disabled / mode disabled | `SKIPPED` | 0 |
| Dry-run completed | `SUCCESS` | 0 |
| Apply completed | `SUCCESS` | 0 |
| Invalid schedule config | `FAIL` | 3 |
| Planner/apply error or apply without retention enabled | `FAIL` | 1 |

Every outcome writes:

* `data/<env>/storage/scheduled_retention_latest.json`
* append to `data/<env>/storage/scheduled_retention_history.jsonl`

Fields include timestamp, environment, mode, status, duration, report path,
and reason on skip/failure.

Retention reports continue to be written under `reports/<env>/retention/` by
the existing planner/apply code.

## Safety

Scheduled apply reuses `RetentionApplyExecutor` unchanged:

* allowed deletion roots
* active job protection
* final clip protection
* unknown / database protection

There is no automation bypass of confirmation semantics: for scheduled apply,
explicit `mode: apply` in config is the opt-in. Manual CLI still requires
`--confirm` / `--confirm-production`.

## Manual retention

Unchanged:

```bash
python scripts/retention.py --dry-run prod
python scripts/retention.py --apply prod --confirm-production
```

Scheduling is an additional caller of the same engine.

## Relationship to disk pressure

Disk pressure (Phase 7) may **recommend** retention. It does **not** trigger
scheduled or automatic apply. Disk-pressure-triggered retention remains a
future phase.

## Legacy note

`deploy/scripts/retention-sweeper.sh` is a pre-Phase-8 ad-hoc cleaner. Cron no
longer invokes it. Prefer the config-driven retention engine and this scheduled
entrypoint.
