# Operations Runbook

Short operator guide for SSH-based remote operations. Prefer copy-paste commands.
For SSH hardening details, see [SSH_ACCESS.md](./SSH_ACCESS.md). To open the
Operations UI from a laptop over an SSH tunnel (UI remains localhost-only), see
[REMOTE_UI_ACCESS.md](./REMOTE_UI_ACCESS.md). For script implementation notes,
see [scripts/ops/README.md](../../scripts/ops/README.md).

All environment-aware commands require `dev` or `prod`. Production is never the
default.

---

## Connect to machine

```bash
ssh maguireltd@<machine-address>
cd ~/mk1-automated-clipping
```

With the recommended SSH alias and `LocalForward` (see
[REMOTE_UI_ACCESS.md](./REMOTE_UI_ACCESS.md)):

```bash
ssh mk1
```

While that session is open, the Operations UI is available on the client at
`http://localhost:<ops-ui-port>/ops` (prod default port `5070`; `/` redirects
to `/ops`). The UI is never exposed publicly; SSH carries the traffic.

On a deployed production host the checkout may be:

```bash
cd /opt/mk04/prod/current
```

Use placeholders only. Do not paste real IPs, hostnames, keys, or secrets into
docs or chat logs.

---

## Daily check

```bash
./scripts/ops/status.sh prod
./scripts/ops/health.sh prod || true
```

- `status` = quick overview (~30s, read-only), includes **Boot readiness**
- `health` = deeper diagnostic, prints full **Boot Verification** (READY / NOT READY)

**Operations UI (daily):** open `/ops` (Operator Console) via SSH tunnel —
see [REMOTE_UI_ACCESS.md](./REMOTE_UI_ACCESS.md). The Console is the canonical
daily landing page; use Runs, Jobs, Outputs, and Failures for drill-down.
**Clip review:** `/ops/outputs` (defaults to latest successful run; use
`?run_id=` for an older run). Legacy `/clip-review` redirects here. Approve/reject/flag
and policy-control POST routes return 410 Gone (retired; they never gated publishing).
Legacy Mission Control (`/dashboard`) and recovery tools remain available but
are not the default workflow. Full UI guide: [ops-ui/README.md](../../ops-ui/README.md).

One command for post-reboot readiness:

```bash
./scripts/ops/health.sh prod || true
# or: python scripts/ops/boot_verification.py prod
```

### Run pipeline (shared entrypoint)

Manual / SSH / UI-style runs:

```bash
./scripts/ops/run-pipeline.sh prod --funnel-id <funnel_id>
```

Scheduled runs (cron uses this path):

```bash
./scripts/ops/run-scheduled.sh prod <funnel_id>
# equivalent: run-pipeline.sh prod --funnel-id <id> --trigger scheduled
```

Install / reboot survival: [SCHEDULER.md](./SCHEDULER.md).

Exit codes: `0` success, `1` pipeline failure, `2` usage, `3` config, `4` not ready,
`5` execution lock held (overlapping or stale; run skipped).
Every invocation writes `runs/prod/<run_id>/run_record.json` and `run.log`
(terminal status SUCCESS / FAIL / SKIPPED). Lock: `data/prod/pipeline_execution.lock`.
See [RUN_RECORDS.md](./RUN_RECORDS.md). Does not recover half-completed jobs or
auto-clear stale locks.


Required components (config, API, worker, output funnel, scheduler, database,
output paths) must PASS for `Boot readiness READY`. AI and Operations UI are
optional (WARN does not block READY).

`health.sh` exit codes: `0`=PASS, `1`=WARN, `2`=FAIL (including NOT READY). Use
`|| true` when you want to keep reading output after a non-zero exit.

Safe practice on dev first:

```bash
./scripts/ops/status.sh dev
./scripts/ops/health.sh dev || true
```

---

## Logs

Bounded, best-effort secret-redacted logs (default 200 lines, max 1000):

```bash
./scripts/ops/logs.sh prod errors
./scripts/ops/logs.sh prod worker
./scripts/ops/logs.sh prod api
./scripts/ops/logs.sh prod ai
./scripts/ops/logs.sh prod scheduler
./scripts/ops/logs.sh prod today
```

Optional line limit:

```bash
./scripts/ops/logs.sh prod worker --lines 100
```

User-facing mode is **scheduler** (backend may be cron). Logs do not read `.env`
files.

---

## Restart services

```bash
./scripts/ops/restart.sh prod worker
./scripts/ops/restart.sh prod api
./scripts/ops/restart.sh prod ai
./scripts/ops/restart.sh prod all --confirm
```

Dry-run:

```bash
./scripts/ops/restart.sh prod worker --dry-run
```

Restarting services does **not** recover half-completed jobs. Check health and
logs after restarting:

```bash
./scripts/ops/health.sh prod || true
./scripts/ops/logs.sh prod errors
```

Production `all` requires `--confirm`.

### Reliability smoke (full subsystem)

```bash
python scripts/smoke/smoke_reliability.py --env dev
python scripts/smoke/smoke_reliability.py --env prod --confirm
```

See [RELIABILITY_SMOKE.md](./RELIABILITY_SMOKE.md).

### Verify automatic restart recovery

Units use `Restart=always` / `RestartSec=5`. Policy-only check (no kills):

```bash
python scripts/smoke/smoke_restart_recovery.py --env dev
```

Live kill/recover (one service at a time; run on dev before prod):

```bash
python scripts/smoke/smoke_restart_recovery.py --env dev --execute
python scripts/smoke/smoke_restart_recovery.py --env prod --execute --confirm
```

Full checklist: [RESTART_RECOVERY.md](./RESTART_RECOVERY.md).

---

## Upload emergency stop

Stop real posting (runtime control only):

```bash
./scripts/ops/disable-uploads.sh prod
./scripts/ops/status.sh prod
./scripts/ops/health.sh prod || true
```

Re-enable (clears runtime disable only; production requires `--confirm`):

```bash
./scripts/ops/enable-uploads.sh prod --confirm
```

Dev:

```bash
./scripts/ops/disable-uploads.sh dev
./scripts/ops/enable-uploads.sh dev
```

Rules:

- `disable-uploads` stops real posting through runtime state
- it does not delete clips
- it does not stop processing
- it does not edit Git config
- `enable-uploads` only clears the runtime disable
- config `uploading.enabled` still controls default upload permission

---

## Scheduler control

```bash
./scripts/ops/scheduler-status.sh prod
./scripts/ops/stop-scheduler.sh prod
./scripts/ops/start-scheduler.sh prod --confirm
```

Dev:

```bash
./scripts/ops/stop-scheduler.sh dev
./scripts/ops/start-scheduler.sh dev
```

Rules:

- `stop-scheduler` / `start-scheduler` / `scheduler-status` are the **only**
  operational controls for pausing/resuming schedule (do not edit cron to pause)
- `stop-scheduler` prevents **new** scheduled runs
- it does not kill running jobs or pipelines
- it does not disable uploads or uninstall cron
- `start-scheduler` does not trigger a run
- production start requires `--confirm`

Scheduler is the operator-facing name. Cron may be the current backend.

---

## Backup and cleanup

```bash
./scripts/ops/backup.sh prod
./scripts/ops/cleanup.sh prod --dry-run
```

Dev:

```bash
./scripts/ops/cleanup.sh dev --dry-run
```

`backup.sh` writes small operational archives under `backups/<env>/` (database,
control state, job/report/run JSON, recent small logs). It excludes media, clips,
`.env`, and credentials. No files are deleted.

Cleanup apply is **not** available yet:

```bash
./scripts/ops/cleanup.sh prod --apply
```

Expected current behaviour: refuses safely until Storage & Data Management
retention planner exists. Cleanup currently deletes nothing. Actual deletion is
deferred to Storage & Data Management.

---

## Update

Authoritative update entrypoint is **repo-root** `./update.sh` (Configuration &
Deployment). `scripts/ops/update.sh` is still a stub — do not use it as the
update path.

**Production code delivery** is only via atomic promotion (not `git pull` inside
`current`):

```bash
# From the development checkout:
./deploy/scripts/promote-to-prod.sh
# Optional: validate / restart the already-selected current release:
./update.sh prod
./scripts/ops/status.sh prod
./scripts/ops/health.sh prod || true
```

Dev:

```bash
git pull
./update.sh dev
./scripts/ops/status.sh dev
./scripts/ops/health.sh dev || true
```

Common options:

```bash
./update.sh prod --check-only
./update.sh prod --no-restart
# --pull is development-only; refused for prod
./update.sh dev --pull
```

`./update.sh prod` validates and may restart the **already selected**
`/opt/mk04/prod/current` release. It must not mutate release contents in place
and must not `git pull` production code.

### Promotion (atomic releases)

```bash
./deploy/scripts/promote-to-prod.sh --dry-run
./deploy/scripts/promote-to-prod.sh --no-restart          # first bootstrap / staging
./deploy/scripts/promote-to-prod.sh                       # normal: switch + restart + health
./deploy/scripts/promote-to-prod.sh --require-clean       # refuse dirty trees
```

Layout:

```text
/opt/mk04/prod/
├── current -> releases/<release_id>
├── previous -> releases/<previous_release_id>
├── releases/
└── dependency-bundles/
```

Dirty trees are allowed by default (warned + recorded). Failed activation rolls
`current` back to `previous` when available. Promotion never enables uploads or
installs cron.

---

## Rollback (cautious)

Automatic rollback runs when post-switch restart/health fails after a promotion
that had a previous release. Manual recovery:

```bash
# Inspect releases
ls -l /opt/mk04/prod/current /opt/mk04/prod/previous /opt/mk04/prod/releases
# Prefer re-promoting a known-good development checkout rather than editing current
./deploy/scripts/promote-to-prod.sh
./scripts/ops/health.sh prod || true
```

Do **not** `git pull` or hand-edit `/opt/mk04/prod/current`. Do not recursively
delete `current`.

---

## Emergency flows

### Bad outputs / stop posting

```bash
./scripts/ops/disable-uploads.sh prod
./scripts/ops/stop-scheduler.sh prod
./scripts/ops/status.sh prod
```

### Service looks broken

```bash
./scripts/ops/status.sh prod
./scripts/ops/health.sh prod || true
./scripts/ops/logs.sh prod errors
./scripts/ops/restart.sh prod worker
```

### Disk pressure

```bash
./scripts/ops/status.sh prod
./scripts/ops/health.sh prod || true
./scripts/ops/cleanup.sh prod --dry-run
```

Cleanup apply is not implemented yet. Do not delete outputs manually.

### Before risky maintenance

```bash
./scripts/ops/stop-scheduler.sh prod
./scripts/ops/disable-uploads.sh prod
./scripts/ops/backup.sh prod
```

---

## What not to do remotely

- Do not develop features directly over SSH.
- Do not manually edit production state unless unavoidable.
- Do not delete outputs manually.
- Do not run experimental scripts against production data.
- Do not bypass upload/scheduler controls.
- Do not expose Operations UI publicly; use an SSH tunnel
  ([REMOTE_UI_ACCESS.md](./REMOTE_UI_ACCESS.md)).
- Do not paste secrets into docs or logs.
- Do not hand-edit `control_state.json`, cron files, systemd units, the
  production database, posting ledger, or job state — use the scripts above.

---

## Dev equivalents (safe testing)

```bash
./scripts/ops/status.sh dev
./scripts/ops/health.sh dev || true
./scripts/ops/disable-uploads.sh dev
./scripts/ops/enable-uploads.sh dev
./scripts/ops/stop-scheduler.sh dev
./scripts/ops/start-scheduler.sh dev
./scripts/ops/cleanup.sh dev --dry-run
./scripts/ops/backup.sh dev
```

### Face-track test mode (dev only)

Gated face-track reframing is **not** enabled in production. To run a controlled
dev batch with `auto` + face-track test mode, follow the checklist in
[video-automation/docs/face_track_test_mode.md](../../video-automation/docs/face_track_test_mode.md)
(enable settings, pre-run safety checks, batch size, Ops UI inspection, go/no-go).

Inspect results at `/ops/outputs` and `/ops/jobs/<job_id>`. Production defaults
remain `blur_background` with face-track test mode off.

---

## Smoke test

Safe automated checks (no prod mutation, no real restart, no deletion, no uploads):

```bash
python scripts/smoke/smoke_remote_operations.py --env dev
python scripts/smoke/smoke_remote_operations.py --env prod --safe-only
```

Full checklist (including manual SSH and intentional prod checks):
[REMOTE_OPERATIONS_SMOKE.md](./REMOTE_OPERATIONS_SMOKE.md).

---

## Related docs

- [SSH Access](./SSH_ACCESS.md) — secure production SSH
- [Remote UI Access](./REMOTE_UI_ACCESS.md) — SSH tunnel to the Operations UI
- [Remote Operations Smoke](./REMOTE_OPERATIONS_SMOKE.md) — smoke checklist and helper
- [Operations README](./README.md) — docs index
- [scripts/ops/README.md](../../scripts/ops/README.md) — script list
- [Configuration README](../configuration/README.md) — config structure
- [deploy/README.md](../../deploy/README.md) — deployment layout and systemd units
- [Face-track test mode (dev operating procedure)](../../video-automation/docs/face_track_test_mode.md) — enable test mode safely, Ops UI review, go/no-go
