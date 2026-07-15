# Remote Operations Smoke Test

Final validation checklist for the Remote Operations subsystem. Proves the
production machine can be operated remotely using the existing command layer.

This smoke does **not** prove remote access unless it was actually run over SSH
from another device or network.

---

## 1. Purpose

This smoke proves:

- remote access works (manual checklist)
- operator commands work (`status`, `health`, `logs`, restart dry-run, etc.)
- dangerous actions have confirmation guards
- runtime upload control works (manual / dev)
- runtime scheduler control works (manual / dev)
- cleanup still deletes nothing

It does **not** implement new operational behaviour, job recovery, cleanup
deletion, or real uploads.

---

## 2. Pre-flight safety

- Do not run prod enable commands unless you intend to re-enable the control.
- Do not run real prod restart-all unless intentionally testing recovery.
- Do not run cleanup apply expecting deletion; it should refuse.
- Do not trigger real uploads.
- Do not manually edit `control_state.json`.

Automated smoke is **safe-only**. It never mutates prod upload/scheduler state,
never restarts services for real, never deletes files, and never runs uploads.

---

## 3. Automated local smoke

Safe helper (non-mutating):

```bash
python scripts/smoke/smoke_remote_operations.py --env dev
python scripts/smoke/smoke_remote_operations.py --env prod --safe-only
```

Prod requires `--safe-only`.

### What the helper runs

For both environments:

```bash
./scripts/ops/status.sh <env>
./scripts/ops/health.sh <env> || true
./scripts/ops/logs.sh <env> errors --lines 50 || true
./scripts/ops/restart.sh <env> worker --dry-run
./scripts/ops/cleanup.sh <env> --dry-run
./scripts/ops/scheduler-status.sh <env>
```

Plus script-existence and `--help` checks (including `backup.sh --help`).

For prod `--safe-only`, it also verifies confirmation guards refuse safely:

```bash
./scripts/ops/start-scheduler.sh prod
./scripts/ops/enable-uploads.sh prod
./scripts/ops/restart.sh prod all
./scripts/ops/cleanup.sh prod --apply
```

Those must exit non-zero and mutate nothing.

### What the helper never runs

```bash
./scripts/ops/disable-uploads.sh prod
./scripts/ops/enable-uploads.sh prod --confirm
./scripts/ops/stop-scheduler.sh prod
./scripts/ops/start-scheduler.sh prod --confirm
./scripts/ops/restart.sh prod all --confirm
./scripts/ops/restart.sh prod worker
./scripts/ops/backup.sh prod
```

Mutating dev upload/scheduler checks are left to the manual checklist so the
helper stays simple and always restore-safe.

Reports are written under:

```text
reports/<env>/remote_operations_smoke/
```

including `latest.json`.

---

## 4. Manual remote smoke checklist

Do not automate these. Run from another device/network when validating SSH.

```text
[ ] SSH in from another device or network
[ ] Confirm key login works
[ ] Confirm password login fails
[ ] Confirm root login fails
[ ] Confirm user can sudo
[ ] Confirm firewall is enabled
[ ] Run status
[ ] Run health
[ ] Run logs errors
[ ] Run restart dry-run
[ ] Run scheduler-status
[ ] Stop/start scheduler in dev
[ ] Disable/enable uploads in dev
[ ] Confirm cleanup dry-run deletes nothing
[ ] Confirm cleanup apply refuses safely
```

### Production optional checks (intentional only)

```text
[ ] Disable prod uploads only if you intentionally want posting blocked
[ ] Re-enable prod uploads only with --confirm and only when safe
[ ] Stop prod scheduler only if you intentionally want scheduled runs paused
[ ] Start prod scheduler only with --confirm and only when safe
[ ] Restart prod service only if intentionally testing service recovery
```

Example manual commands (dev):

```bash
./scripts/ops/status.sh dev
./scripts/ops/health.sh dev || true
./scripts/ops/logs.sh dev errors
./scripts/ops/restart.sh dev worker --dry-run
./scripts/ops/scheduler-status.sh dev
./scripts/ops/stop-scheduler.sh dev
./scripts/ops/start-scheduler.sh dev
./scripts/ops/disable-uploads.sh dev
./scripts/ops/enable-uploads.sh dev
./scripts/ops/cleanup.sh dev --dry-run
./scripts/ops/cleanup.sh dev --apply
```

---

## 5. Expected results

| Check | Expected |
|-------|----------|
| `status.sh` | Runs and prints environment |
| `health.sh` | Runs; may return WARN/FAIL depending on current service state |
| `logs.sh` | Bounded output (or clean unavailable message) |
| restart dry-run | Mutates nothing |
| prod `enable-uploads` without `--confirm` | Refuses |
| prod `start-scheduler` without `--confirm` | Refuses |
| prod `restart all` without `--confirm` | Refuses |
| `cleanup --dry-run` | Deletes nothing |
| `cleanup --apply` | Refuses safely |

`health.sh` non-zero does **not** fail the automated smoke by itself; it is
reported as WARN because live services are not required for smoke completion.

---

## 6. What counts as pass

The Remote Operations subsystem passes this iteration when:

- SSH access is documented and manually verified
- `status` works
- `health` works
- `logs` work
- restart dry-run works
- upload kill switch works in dev
- scheduler control works in dev
- cleanup dry-run deletes nothing
- dangerous prod commands have confirmation guards
- runbook exists
- no public unauthenticated admin surface was added

Automated smoke alone is not enough for full pass: SSH and mutating dev controls
remain manual.

---

## Related docs

- [RUNBOOK.md](./RUNBOOK.md) — operator command recipes
- [SSH_ACCESS.md](./SSH_ACCESS.md) — SSH hardening checklist
- [scripts/ops/README.md](../../scripts/ops/README.md) — script index
