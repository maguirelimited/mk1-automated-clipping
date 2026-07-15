# Operations Documentation

Guides for running, updating, and accessing the mk0.4 production system.

| Document | Description |
|----------|-------------|
| [RUNBOOK.md](./RUNBOOK.md) | **Primary operator guide** — SSH daily checks, logs, restart, upload/scheduler controls, backup, cleanup, update, emergencies |
| [PRODUCTION_SERVICES.md](./PRODUCTION_SERVICES.md) | **Service inventory** — always-running, scheduled, and manual ops (Reliability & Recovery source of truth) |
| [RUN_RECORDS.md](./RUN_RECORDS.md) | **Run records** — canonical pipeline execution history under `runs/<env>/` |
| [SCHEDULER.md](./SCHEDULER.md) | **Scheduler** — thin cron trigger → `run-scheduled.sh` → `run-pipeline.sh` |
| [observability_contract.md](./observability_contract.md) | **Observability contract** — models, endpoints, UI, auth, controls (Operations & Observability Phases 1–14) |
| [RELIABILITY_SMOKE.md](./RELIABILITY_SMOKE.md) | **Reliability smoke** — end-to-end validation of Reliability & Recovery |
| [RESTART_RECOVERY.md](./RESTART_RECOVERY.md) | **Restart recovery** — verify systemd recovers services after intentional kill |
| [REMOTE_OPERATIONS_SMOKE.md](./REMOTE_OPERATIONS_SMOKE.md) | Smoke checklist + safe automated helper for Remote Operations |
| [SSH_ACCESS.md](./SSH_ACCESS.md) | Secure SSH access (keys, firewall, checklist) |
| [REMOTE_UI_ACCESS.md](./REMOTE_UI_ACCESS.md) | **Remote Ops UI** — SSH local port forward to localhost-only UI |

## Start here

After SSH:

```bash
cd ~/mk1-automated-clipping   # or /opt/mk04/prod/current on deployed prod
./scripts/ops/status.sh prod
./scripts/ops/health.sh prod || true
```

To open the Operations UI from a laptop without public exposure, configure an
SSH alias with `LocalForward` and browse `http://localhost:<ops-ui-port>` —
see [REMOTE_UI_ACCESS.md](./REMOTE_UI_ACCESS.md).

Full command recipes (upload stop, scheduler, backup, update, rollback, emergencies)
are in [RUNBOOK.md](./RUNBOOK.md).

## Script index

Implemented under `scripts/ops/` (Prompts 3–9):

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

Updates use **repo-root** `./update.sh` (not `scripts/ops/update.sh`, which is still a stub):

```bash
git pull
./update.sh prod
```

Safe automated smoke (non-mutating):

```bash
python scripts/smoke/smoke_reliability.py --env dev
python scripts/smoke/smoke_remote_operations.py --env dev
python scripts/smoke/smoke_remote_operations.py --env prod --safe-only
python scripts/smoke/smoke_restart_recovery.py --env dev
ops-ui/.venv/bin/python scripts/smoke/smoke_observability.py --env dev
ops-ui/.venv/bin/pytest tests/smoke/test_observability_smoke.py -q
```

Live restart recovery (kills one service at a time; use on dev first):

```bash
python scripts/smoke/smoke_restart_recovery.py --env dev --execute
python scripts/smoke/smoke_restart_recovery.py --env prod --execute --confirm
```

See [REMOTE_OPERATIONS_SMOKE.md](./REMOTE_OPERATIONS_SMOKE.md) and
[RESTART_RECOVERY.md](./RESTART_RECOVERY.md).
See [scripts/ops/README.md](../../scripts/ops/README.md) for the full script list.

For configuration details, see [Configuration README](../configuration/README.md).
For deployment layout and systemd units, see [deploy/README.md](../../deploy/README.md).
For storage artifact locations and owners (inventory only), see
[Storage Inventory](../storage/STORAGE_INVENTORY.md).
