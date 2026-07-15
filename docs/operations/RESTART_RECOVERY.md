# Restart Recovery Verification

Reliability & Recovery **Phase 4**.

Validates that `systemd` automatically recovers always-running production
services after an intentional process kill. This does **not** recover
half-completed jobs.

## Architectural alignment

Build on approved Phases 1–3 only:

* Units use `Restart=always` / `RestartSec=5`
* Soft peer dependencies (`After=` + `Wants=`, never `Requires=`)
* Scheduler gates on **HTTP readiness**, not process start alone
* Recover **services**; do not auto-resume partial jobs

Do not use this phase to redesign startup order, readiness, scheduling, boot
verification, execution locks, or run records.

## Services under test

| Operator mode | Unit | Health probe |
| --- | --- | --- |
| `ai` | `mk04-ai-service.service` | `GET /health` |
| `api` | `mk04-source-input.service` | `GET /healthz` |
| `worker` | `mk04-video-automation.service` | `GET /healthz` |
| `ops-ui` | `mk04-ops-ui.service` | `GET /health` |

Output funnel (`mk04-output-funnel`) uses the same restart policy and may be
included with the smoke helper’s `--include-output-funnel` flag. It is not
required for Phase 4 acceptance (prompt scope is the four services above).

## Expected restart policy

Every unit under test must declare:

```ini
Restart=always
RestartSec=5
```

After `SIGKILL` of the main process, systemd should:

1. Observe the exit
2. Wait `RestartSec` (5 seconds)
3. Start the service again
4. Increment `NRestarts` (visible via `systemctl show` / `systemctl status`)
5. Emit restart activity in `journalctl -u <unit>`

Failures must remain visible: `NRestarts` increases; journal history is not
cleared by a successful recovery.

## Safe policy-only check (default)

No processes are killed. Suitable for any machine, including CI:

```bash
python scripts/smoke/smoke_restart_recovery.py --env dev
python scripts/smoke/smoke_restart_recovery.py --env prod
```

Checks:

* Unit files exist under `deploy/systemd/`
* Each file has `Restart=always` and `RestartSec=5`
* `WantedBy=multi-user.target` is present
* If units are installed on the host, `systemctl show` reports matching restart policy

## Live kill / recover (`--execute`)

Terminates each service’s main PID with `SIGKILL`, one at a time, and waits for
systemd recovery plus HTTP health.

**Development (preferred first run):**

```bash
# Units must be installed and active for the target env.
python scripts/smoke/smoke_restart_recovery.py --env dev --execute
```

**Production (intentional only):**

```bash
python scripts/smoke/smoke_restart_recovery.py --env prod --execute --confirm
```

Prod `--execute` without `--confirm` is refused.

For each service the helper verifies:

1. Unit is active before the kill
2. `NRestarts` is recorded
3. Main PID is killed (`SIGKILL`)
4. Unit becomes `active` again within the wait window
5. `NRestarts` increased (restart is observable, not silent)
6. HTTP health probe succeeds after recovery
7. After all recoveries, scheduler readiness evaluation still runs (required
   services HTTP-ready when those units are in scope)

### Manual equivalent (one service)

```bash
UNIT=mk04-source-input.service

systemctl show "$UNIT" -p ActiveState -p MainPID -p NRestarts -p Restart -p RestartUSec
PID=$(systemctl show -p MainPID --value "$UNIT")
kill -9 "$PID"

# Wait > RestartSec (5s), then:
systemctl status "$UNIT" --no-pager
systemctl show "$UNIT" -p ActiveState -p MainPID -p NRestarts
journalctl -u "$UNIT" -n 30 --no-pager
./scripts/ops/health.sh dev   # or prod
./scripts/ops/logs.sh dev api
```

Repeat for `mk04-ai-service`, `mk04-video-automation`, `mk04-ops-ui`.

## Observability checklist

After a live recovery run:

```text
[ ] systemctl status <unit> shows active (running)
[ ] NRestarts increased vs pre-kill value
[ ] journalctl -u <unit> shows stop/start or restart activity
[ ] HTTP health endpoint responds
[ ] ./scripts/ops/status.sh <env> lists the service as up
[ ] ./scripts/ops/health.sh <env> does not permanently hide the failure
    (NRestarts / journal still show the incident)
```

## Safety boundaries

The live smoke helper:

* Kills **one service at a time** and waits for recovery before the next
* Does **not** disable uploads or the scheduler
* Does **not** delete jobs, clips, or databases
* Does **not** resume or retry half-completed production jobs
* Does **not** change unit files or deployment layout

If a service does not recover, stop and inspect:

```bash
systemctl status <unit> --no-pager -l
journalctl -u <unit> -n 100 --no-pager
./scripts/ops/logs.sh <env> <mode>
```

Do not invent automatic job resume to “fix” a failed recovery test.

## Reports

When not using `--no-report`, results are written under:

```text
reports/<env>/restart_recovery_smoke/smoke_<env>_<timestamp>.json
reports/<env>/restart_recovery_smoke/latest.json
```

## Related

* [PRODUCTION_SERVICES.md](./PRODUCTION_SERVICES.md) — inventory and restart policy
* [deploy/systemd/README.md](../../deploy/systemd/README.md) — unit install
* [RUNBOOK.md](./RUNBOOK.md) — operator restart commands (`restart.sh`)
