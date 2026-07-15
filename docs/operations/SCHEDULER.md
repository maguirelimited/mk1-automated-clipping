# Scheduled Pipeline Automation

Reliability & Recovery **Phases 9–10**.

The scheduler is a **thin trigger**. It does not implement readiness, locking,
run records, config validation, or pipeline work. Those live in
`scripts/ops/run-pipeline.sh`.

## Canonical operational controls (Phase 10)

Operators use **only** these scripts to pause/resume/inspect scheduling:

```bash
./scripts/ops/stop-scheduler.sh prod
./scripts/ops/start-scheduler.sh prod --confirm
./scripts/ops/scheduler-status.sh prod
```

| Command | Effect |
| --- | --- |
| `stop-scheduler` | Sets `data/<env>/control_state.json` `scheduler_disabled=true`. New scheduled runs are **SKIPPED** by `run-pipeline`. **Does not** kill running pipelines, uninstall cron, or change uploads. |
| `start-scheduler` | Clears the runtime disable. Does **not** trigger a run. Prod requires `--confirm`. |
| `scheduler-status` | Reports runtime control, whether new scheduled runs are allowed, underlying mechanism (cron today), and the pipeline entrypoint. |

Do **not** edit cron by hand to pause production, and do not add alternate control scripts. Host schedule install remains `deploy/scripts/install-scheduler.sh`; day-to-day pause/resume is stop/start only.

Mechanism migration (cron → systemd timer) must keep calling `run-scheduled.sh` and honour the same `control_state.json` flag so these three scripts stay valid.

## Architecture

```text
cron (survives reboot)
    ↓
scripts/ops/run-scheduled.sh <env> <funnel_id>
    ↓
scripts/ops/run-pipeline.sh <env> --funnel-id <id> --trigger scheduled
    ↓
config + boot readiness + execution lock + run record + POST /run-funnel
```

| Layer | Responsibility |
| --- | --- |
| **cron** | Fire at configured times; start at boot |
| **run-scheduled.sh** | Log trigger; call shared pipeline entrypoint only |
| **run-pipeline.sh** | All pipeline execution behaviour |
| **run-scheduled-retention.sh** | Log trigger; call scheduled retention entrypoint only |
| **retention_schedule** | Config-driven dry-run/apply via existing retention engine |
| **run-log-rotation.sh** | Rotate active project logs (size-bounded; retention owns expiry) |
| **run-database-backup.sh** | Create SQLite snapshots (retention owns backup expiry) |

Do **not** call `POST /run-funnel` or Python pipeline modules from cron.

Scheduled retention (Storage Phase 8) shares this cron mechanism. Mode and
enablement come from `storage.schedule` (production defaults to `dry_run`).
See `docs/storage/SCHEDULED_RETENTION.md`.

## Mechanism

This project uses **cron** (not a second scheduler, not systemd timers for the
pipeline). Supporting jobs (watchdog, retention, handoff sweeper) share the
same crontab file but are not the pipeline entrypoint.

Schedule definition: `deploy/cron/mk04.crontab` (user crontab) or
`deploy/cron/mk04.cron.d` (`/etc/cron.d/mk04`).

Active funnel id(s) and clock times are configured **in the crontab lines**
(one line per source-input `funnel_id`). That is the schedule configuration —
do not duplicate it elsewhere.

## Install (reboot survival)

Cron must be enabled as a system service so schedules return after reboot:

```bash
sudo systemctl enable --now cron    # Debian/Ubuntu
# or: sudo systemctl enable --now crond
```

Install the mk04 schedule from the **deployed** production tree:

```bash
# User crontab (as mk04)
sudo -u mk04 /opt/mk04/prod/current/deploy/scripts/install-scheduler.sh prod

# Or system drop-in
sudo /opt/mk04/prod/current/deploy/scripts/install-scheduler.sh prod --system
```

Dev is **manual** by default (`DEFAULT_SCHEDULER_MODE[dev]=manual`). Trigger on
demand:

```bash
./scripts/ops/run-scheduled.sh dev <funnel_id>
```

## Runtime enable / disable

See **Canonical operational controls** above. When disabled,
`run-pipeline.sh --trigger scheduled` writes a **SKIPPED** run record and exits
0. Manual / SSH / UI triggers (`--trigger manual_cli` etc.) are unaffected.

## Scheduled execution flow

| Outcome | Path |
| --- | --- |
| Success | cron → run-scheduled → run-pipeline → SUCCESS run record |
| Not ready | same path → FAIL run record (exit 4) |
| Lock held | same path → SKIPPED run record (exit 5) |
| Scheduler disabled | same path → SKIPPED run record (exit 0) |

Evidence appears in:

* `runs/<env>/<run_id>/run_record.json` (`trigger: scheduled`)
* `runs/<env>/<run_id>/run.log`
* `journalctl` / `logger` tag `mk04-<env>-scheduler`
* `./scripts/ops/status.sh` / `health.sh` (last pipeline run)
* `./scripts/ops/scheduler-status.sh`

## Troubleshooting

| Symptom | Check |
| --- | --- |
| No scheduled runs after reboot | `systemctl status cron` (or `crond`); reinstall crontab |
| Cron fires but nothing runs | `runs/prod/` for FAIL/SKIPPED records; `health.sh prod` |
| Runs while you wanted pause | `scheduler-status.sh prod`; `stop-scheduler.sh prod` |
| Overlap / lock | `data/prod/pipeline_execution.lock`; SKIPPED records |
| Wrong funnel | Edit crontab funnel_id; reinstall schedule |

Manual scheduled-path test (does not require waiting for cron):

```bash
./scripts/ops/run-scheduled.sh prod mfm_business_ai_001
```

## Compatibility

`deploy/scripts/run-funnel-daily.sh` remains as a legacy wrapper that calls
`run-scheduled.sh`. Prefer `scripts/ops/run-scheduled.sh` in new cron lines.

## Out of scope

* systemd timer migration (optional later; still must call `run-scheduled.sh`)
* Scheduler UI / analytics
* Per-job scheduling or queues
* Duplicated readiness / lock / run-record logic
