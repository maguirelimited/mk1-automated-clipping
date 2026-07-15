# Reliability & Recovery Smoke Test

**Phase 11** — final validation milestone for Reliability & Recovery.

Proves existing components work together through **real operational interfaces**.
Does not add production features or redesign architecture.

## What it validates

| Area | Checks |
| --- | --- |
| **Artifacts** | Ops scripts, unit restart policies, cron → `run-scheduled.sh` |
| **Boot** | `boot_verification` READY/NOT READY consistency |
| **Health / status** | Markers for boot readiness, scheduler, execution lock, last run |
| **Scheduler control** | `stop-scheduler` / `start-scheduler` / `scheduler-status` cycle |
| **Pipeline** | `run-pipeline.sh` and `run-scheduled.sh` produce run records + logs |
| **Run records** | Manual (`test`) and scheduled triggers; readiness FAIL records |
| **Execution lock** | Acquire, inspect, release, stale detection |
| **Restart recovery** | Points at `smoke_restart_recovery.py` for live kill tests |

On a host that is **NOT READY** (services down), lock-block and
scheduler-stop→SKIPPED paths are **SKIP**ped (they require READY). Control
cycle, records on readiness failure, and lock module checks still run.

On a **READY** host, those SKIP checks become live PASS/FAIL.

## Run

```bash
# Development (may toggle scheduler control and restore)
python scripts/smoke/smoke_reliability.py --env dev

# Production (no scheduler mutation unless --confirm)
python scripts/smoke/smoke_reliability.py --env prod
python scripts/smoke/smoke_reliability.py --env prod --confirm

# Optional funnel override
python scripts/smoke/smoke_reliability.py --env dev --funnel-id mfm_business_ai_001
```

Exit codes:

| Exit | Meaning |
| --- | --- |
| `0` | PASS or WARN (SKIPs allowed) |
| `1` | FAIL |
| `2` | Usage error |

Reports:

```text
reports/<env>/reliability_smoke/smoke_<env>_<timestamp>.json
reports/<env>/reliability_smoke/latest.json
```

## Related smokes

| Smoke | Purpose |
| --- | --- |
| `smoke_reliability.py` | End-to-end Reliability subsystem |
| `smoke_restart_recovery.py` | Live systemd kill/recover (`--execute`) |
| `smoke_remote_operations.py` | Remote ops command surface |

## Manual checklist (full host)

When services are installed and READY:

```bash
python scripts/smoke/smoke_reliability.py --env prod --confirm
python scripts/smoke/smoke_restart_recovery.py --env prod --execute --confirm
./scripts/ops/health.sh prod || true
./scripts/ops/status.sh prod
```

After reboot: confirm `health.sh` shows Boot readiness READY and cron still
fires `run-scheduled.sh` (see [SCHEDULER.md](./SCHEDULER.md)).

## Subsystem complete

If this smoke exits 0 (PASS or WARN with only environment-limited SKIPs) and
unit tests pass, **Reliability & Recovery is complete**. Next subsystem:
**Operations & Observability**.
