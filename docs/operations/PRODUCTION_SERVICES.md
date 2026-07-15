# Production Service Inventory

Source of truth for **Reliability & Recovery**.

This document inventories production services as they exist in the repository
today, and records the architectural target from the Reliability & Recovery
plan. It does **not** implement runtime behaviour.

Use this inventory when creating or updating systemd units (Phase 2), startup
ordering (Phase 3), boot verification (Phase 5), and scheduled automation
(Phases 6–10).

---

## Naming conventions

| Layer | Convention | Examples |
| --- | --- | --- |
| Reliability plan (target language) | `mk1-*` | `mk1-ai.service`, `mk1-api.service` |
| Repository / deployed units (current) | `mk04-*` | `mk04-ai-service.service`, `mk04-source-input.service` |
| Remote Operations command modes | short labels | `ai`, `api`, `worker`, `ops-ui`, `output-funnel`, `scheduler` |

**Phase 2 should keep the existing `mk04-*` unit names** unless an explicit
rename is decided later. Renaming would break `scripts/ops` mappings, sudoers
entries, and installed host units.

Operator-facing labels in `status` / `health` / `restart` / `logs` map as
follows (`scripts/ops/ops_readonly.py`):

| Operator mode | systemd unit | Plan role |
| --- | --- | --- |
| `ai` | `mk04-ai-service.service` | AI Service |
| `api` | `mk04-source-input.service` | API / Flask (ingestion) |
| `worker` | `mk04-video-automation.service` | Worker (clipping pipeline server) |
| `ops-ui` | `mk04-ops-ui.service` | Operations UI |
| `output-funnel` | `mk04-output-funnel.service` | Supporting always-running service |
| `scheduler` | *(no unit — cron today)* | Scheduler / Timer |

There is **no separate OS-level worker process**. `video-automation` is a
long-running Flask app that performs transcription, selection, clipping, and
job handling in-process. Remote Operations calls it `worker` because it is the
heavy processing service.

---

## Service categories

Do not mix these categories.

### Always-running services

Long-running processes managed (or intended to be managed) by systemd.

| Service | Unit (current) | Required? |
| --- | --- | --- |
| AI Service | `mk04-ai-service.service` | Required for local-LLM selection path; **optional** if the pipeline uses a remote/OpenAI backend only |
| API (source-input) | `mk04-source-input.service` | **Required** |
| Worker (video-automation) | `mk04-video-automation.service` | **Required** |
| Output funnel | `mk04-output-funnel.service` | **Required** for autonomous publish path |
| Operations UI | `mk04-ops-ui.service` | **Optional** for autonomous pipeline; **required** for operator UI access |
| Ollama (external model backend) | `ollama.service` (vendor unit, if installed) | **Optional** supporting dependency of AI Service |

### Scheduled services

Periodic jobs. Today these are **cron** entries from `deploy/cron/mk04.crontab`,
not systemd timers. Reliability & Recovery prefers systemd timers later; this
inventory records current reality and the target.

| Job | Entrypoint (current) | Required? |
| --- | --- | --- |
| Pipeline / daily funnel trigger | `scripts/ops/run-scheduled.sh` (cron) → `run-pipeline.sh` | **Required** for autonomous ingestion |
| Handoff retry sweeper | `video-automation/scripts/handoff_sweeper.py` | **Required** for orphaned-clip recovery between worker and output-funnel |
| Retention sweeper | `deploy/scripts/retention-sweeper.sh` | **Required** for unattended disk safety |
| Health watchdog | `deploy/scripts/watchdog.sh` | **Required** for unattended failure visibility |

Shared entrypoint: `scripts/ops/run-pipeline.sh` (Phase 6). It validates config,
checks boot readiness, then POSTs `/run-funnel` on source-input. Execution locks
(Phase 7) and full run records (Phase 8) extend this path.

### Manual operational commands

Callable via SSH (and some via Operations UI). Not always-running services.

| Command | Entrypoint | Purpose |
| --- | --- | --- |
| Status | `./scripts/ops/status.sh <env>` | Environment / service summary |
| Health | `./scripts/ops/health.sh <env>` | Readiness / health checks |
| Logs | `./scripts/ops/logs.sh <env> <mode>` | journalctl / scheduler log views |
| Restart | `./scripts/ops/restart.sh <env> <target>` | Controlled systemd restart |
| Disable uploads | `./scripts/ops/disable-uploads.sh <env>` | Runtime upload kill switch |
| Enable uploads | `./scripts/ops/enable-uploads.sh <env> [--confirm]` | Clear upload kill switch |
| Stop scheduler | `./scripts/ops/stop-scheduler.sh <env>` | Block new scheduled runs |
| Start scheduler | `./scripts/ops/start-scheduler.sh <env> [--confirm]` | Allow scheduled runs again |
| Scheduler status | `./scripts/ops/scheduler-status.sh <env>` | Runtime + underlying scheduler state |
| Backup | `./scripts/ops/backup.sh <env>` | Operational backup |
| Cleanup | `./scripts/ops/cleanup.sh <env> [--dry-run\|--apply]` | Controlled cleanup |
| Update | `./update.sh <env>` | Config-aware update path |
| Run (local stack) | `./run.sh --env <env>` | Dev/local startup wrapper (`--check-only` validates only) |
| Doctor | `./deploy/scripts/doctor.sh <env>` | HTTP readiness probes for core services |
| Manual pipeline | `./scripts/ops/run-pipeline.sh <env> --funnel-id <id>` | Shared entrypoint |
| Scheduled pipeline | `./scripts/ops/run-scheduled.sh <env> <funnel_id>` | Thin cron trigger only |
| Manual retention | `./deploy/scripts/retention-sweeper.sh <env> [--dry-run]` | Same path as scheduled retention |
| Manual watchdog | `./deploy/scripts/watchdog.sh <env>` | Same path as scheduled watchdog |

Scheduler stop/start **does not** install or remove cron; it sets a runtime
gate in `data/<env>/control_state.json` that `run-pipeline.sh --trigger scheduled`
respects. Stopping the scheduler must not kill an active job.

---

## Startup order and readiness (Phase 3)

**Principle:** dependencies start in a sensible order where they exist, but
**readiness** (HTTP health), not process startup alone, decides when the
scheduler may begin normal operation.

```text
Machine boot
    ↓
Network ready
    ↓
Environment / config available
    ↓
Always-running services start (soft peer order below)
    ↓
Each service exposes HTTP health when actually usable
    ↓
Trigger (scheduler / manual / SSH / UI)
    ↓
scripts/ops/run-pipeline.sh
    ↓
Config validation + boot readiness
    ├─ NOT READY → abort (exit 4)
    └─ READY → POST /run-funnel (existing production path)
```

### Soft peer order (systemd)

Use `After=` + `Wants=` only — **never** `Requires=`. A slow or missing peer
must not permanently fail a unit.

| Unit | `After=` / `Wants=` | Rationale |
| --- | --- | --- |
| `mk04-source-input` | `network-online.target` | Ingestion entrypoint; no peer wait |
| `mk04-video-automation` | network + soft `mk04-source-input` | Prefer API up for auto-enqueue handoff |
| `mk04-output-funnel` | network + soft `mk04-video-automation` | Prefer worker up for registration handoff |
| `mk04-ai-service` | network + soft `ollama.service` | Independent of API/worker; optional for local LLM |
| `mk04-ops-ui` | `network-online.target` only | Starts independently; UI reports peer failures |

AI is **not** ordered before API. API may start while AI is unavailable; health
surfaces can show `AI unavailable` / WARN. Scheduled runs are gated on
**required** readiness (API, Worker, Output funnel), not on AI or Ops UI.

### Shared pipeline entrypoint and readiness

`scripts/ops/run-pipeline.sh` is the only supported execution path:

1. Config validation (exit 3 on failure)
2. Boot readiness (exit 4 if NOT READY)
3. For `--trigger scheduled` only: runtime scheduler disable → skip (exit 0)
4. Acquire execution lock (`data/<env>/pipeline_execution.lock`); if held →
   SKIPPED (exit 5), including when the lock is stale (not auto-cleared)
5. Create / finalise run record under `runs/<env>/<run_id>/`
6. Invoke existing `POST /run-funnel` (no duplicated pipeline logic)
7. Release lock in `finally`

Boot verification: `boot_verification.py` / `health.sh`. Lock:
`execution_lock.py`. Records: `run_records.py` / [RUN_RECORDS.md](./RUN_RECORDS.md).

---

## Restart policy recommendations

Shipped units use `Restart=always` / `RestartSec=5` (Phase 2 baseline).

| Service | Policy | Notes |
| --- | --- | --- |
| AI Service | `Restart=always`, `RestartSec=5` | Long-running |
| API (source-input) | `Restart=always`, `RestartSec=5` | Required ingestion entrypoint |
| Worker (video-automation) | `Restart=always`, `RestartSec=5` | Restart recovers the *service*, not half-finished jobs |
| Output funnel | `Restart=always`, `RestartSec=5` | Hosts in-process upload/plan workers |
| Operations UI | `Restart=always`, `RestartSec=5` | Optional for pipeline; still auto-recover when enabled |
| Ollama | Vendor default / `Restart=always` if managed | Soft dependency only |
| Scheduled jobs (cron/timer) | N/A (oneshot) | Failures must be recorded; do not auto-resume partial pipeline jobs |
| Manual ops commands | N/A | Operator-invoked |

**Boundary:** service restart is in scope. Automatic partial-job recovery is
**out of scope** for Reliability & Recovery.

---

## Always-running service details

Production paths assume the deployed copy at `/opt/mk04/prod/current` and
service user `mk04:mk04`. Shared environment file: `/etc/mk04/prod/env`.
Optional per-service overrides: `/etc/mk04/prod/services/<name>.env`.

Dev runs from the checkout with `MK04_ENV=dev` and is not required to use these
systemd units.

### AI Service

| Field | Value |
| --- | --- |
| **Service name** | `mk04-ai-service.service` (plan language: `mk1-ai.service`) |
| **Category** | Always-running |
| **Required / optional** | **Optional** for hosts that do not run a local model; **required** when local LLM selection is configured |
| **Purpose** | Local LLM clip / section candidate selection (`/ai/run`, `/health`) fronting Ollama or other backends |
| **Startup command** | `/opt/mk04/prod/current/deploy/scripts/run-ai-service.sh prod` → `ai-service/app.py` |
| **Working directory** | `/opt/mk04/prod/current/ai-service` |
| **Environment file** | `/etc/mk04/prod/env`, `/etc/mk04/prod/services/ai-service.env` |
| **Restart policy** | `Restart=always`, `RestartSec=5` |
| **Log source** | `journalctl -u mk04-ai-service` (`SyslogIdentifier=mk04-ai-service`) |
| **Health check** | `GET http://127.0.0.1:5075/health` (prod); systemd active state via `health.sh` / `status.sh` |
| **Dependencies** | Network; soft dependency on `ollama.service`; `run-ai-service.sh` best-effort starts Ollama |
| **Prod port** | `5075` |

### API (source-input)

| Field | Value |
| --- | --- |
| **Service name** | `mk04-source-input.service` (plan language: `mk1-api.service`) |
| **Category** | Always-running |
| **Required / optional** | **Required** |
| **Purpose** | Ingestion API: funnel run trigger (`POST /run-funnel`), fetch ready long-form input, hand off to video-automation |
| **Startup command** | `/opt/mk04/prod/current/deploy/scripts/run-input-service.sh prod` → `source-input/input_service/app.py` |
| **Working directory** | `/opt/mk04/prod/current` (unit); process `cd`s to `source-input/input_service` |
| **Environment file** | `/etc/mk04/prod/env`, `/etc/mk04/prod/services/source-input.env` |
| **Restart policy** | `Restart=always`, `RestartSec=5` |
| **Log source** | `journalctl -u mk04-source-input` |
| **Health check** | `GET http://127.0.0.1:5060/healthz` (prod); `doctor.sh`; `health.sh` “API health endpoint” |
| **Dependencies** | Network; config/env available. Downstream: video-automation for job enqueue |
| **Prod port** | `5060` |

### Worker (video-automation)

| Field | Value |
| --- | --- |
| **Service name** | `mk04-video-automation.service` (plan language: `mk1-worker.service`) |
| **Category** | Always-running |
| **Required / optional** | **Required** |
| **Purpose** | Clipping pipeline server: accept jobs, transcribe, select, validate, render clips, write reports/analytics, hand off registrations |
| **Startup command** | `/opt/mk04/prod/current/deploy/scripts/run-video-automation.sh prod` → `video-automation/server/app.py` |
| **Working directory** | `/opt/mk04/prod/current` (unit); process `cd`s to `video-automation` |
| **Environment file** | `/etc/mk04/prod/env`, `/etc/mk04/prod/services/video-automation.env` |
| **Restart policy** | `Restart=always`, `RestartSec=5` |
| **Log source** | `journalctl -u mk04-video-automation`; per-job artefacts under jobs/reports paths |
| **Health check** | `GET http://127.0.0.1:5050/healthz` (prod); `doctor.sh`; systemd active state |
| **Dependencies** | Network; ideally API (source-input) for auto-enqueue handoff; AI service when local selection is configured; output-funnel for registration handoff |
| **Prod port** | `5050` |

### Output funnel

| Field | Value |
| --- | --- |
| **Service name** | `mk04-output-funnel.service` |
| **Category** | Always-running (supporting production service) |
| **Required / optional** | **Required** for autonomous upload/publish; pipeline can clip without it, but production distribution cannot |
| **Purpose** | Registration queue, channel routing, schedule windows, in-process upload worker and plan worker |
| **Startup command** | `/opt/mk04/prod/current/deploy/scripts/run-output-funnel.sh prod` → `python -m output_funnel.app` |
| **Working directory** | `/opt/mk04/prod/current` (unit); process `cd`s to `output-funnel` |
| **Environment file** | `/etc/mk04/prod/env`, `/etc/mk04/prod/services/output-funnel.env` |
| **Restart policy** | `Restart=always`, `RestartSec=5` |
| **Log source** | `journalctl -u mk04-output-funnel` (includes upload_worker lines) |
| **Health check** | `GET http://127.0.0.1:5055/healthz` (prod); `doctor.sh`; systemd active state |
| **Dependencies** | Network; ideally video-automation for `/registrations/from-job` handoff |
| **Prod port** | `5055` |
| **In-process workers** | Upload worker and plan worker threads (not separate systemd units) |

### Operations UI

| Field | Value |
| --- | --- |
| **Service name** | `mk04-ops-ui.service` (plan language: `mk1-ops-ui.service`) |
| **Category** | Always-running |
| **Required / optional** | **Optional** for autonomous pipeline; **required** when operators need the control panel |
| **Purpose** | Read-mostly control panel: status, recovery views, controls |
| **Startup command** | `/opt/mk04/prod/current/deploy/scripts/run-ops-ui.sh prod` → `python -m ops_ui` |
| **Working directory** | `/opt/mk04/prod/current/ops-ui` |
| **Environment file** | `/etc/mk04/prod/env`, `/etc/mk04/prod/services/ops-ui.env` |
| **Restart policy** | `Restart=always`, `RestartSec=5` |
| **Log source** | `journalctl -u mk04-ops-ui` |
| **Health check** | `GET http://127.0.0.1:5070/health` (and `/healthz`); systemd active state |
| **Dependencies** | Network; can start without peers but should surface dependency failures in UI |
| **Prod port** | `5070` |
| **Bind address** | `127.0.0.1` only (`OPS_UI_HOST`; never public) |
| **Remote access** | SSH local port forward — [REMOTE_UI_ACCESS.md](./REMOTE_UI_ACCESS.md) |

### Ollama (external supporting)

| Field | Value |
| --- | --- |
| **Service name** | `ollama.service` (vendor-installed; not shipped in `deploy/systemd/`) |
| **Category** | Always-running supporting dependency |
| **Required / optional** | **Optional**; only when AI Service uses a local Ollama backend |
| **Purpose** | Local model runtime for AI Service |
| **Startup command** | Vendor unit / `deploy/scripts/run-ollama.sh` (best-effort from `run-ai-service.sh`) |
| **Working directory** | Vendor-defined |
| **Environment file** | Host / Ollama defaults; AI service may set `MK04_OLLAMA_STRICT`, `OLLAMA_AUTO_PULL_MODEL` |
| **Restart policy (recommended)** | Vendor default; prefer always-restart if managed by this host |
| **Log source** | `journalctl -u ollama` when unit exists |
| **Health check** | Ollama HTTP (typically `http://127.0.0.1:11434`); AI Service `/health` may still pass if backend is optional |
| **Dependencies** | Network |

---

## Scheduled service details

### Pipeline / daily funnel trigger (scheduler)

| Field | Value |
| --- | --- |
| **Service name** | Host `cron` / `crond` (not an mk04 unit). Schedule: `deploy/cron/mk04.crontab` |
| **Category** | Scheduled |
| **Required / optional** | **Required** for autonomous production |
| **Purpose** | Trigger ingestion for one `funnel_id` on a schedule |
| **Startup command (current)** | `scripts/ops/run-scheduled.sh prod <funnel_id>` → `run-pipeline.sh --trigger scheduled` |
| **Working directory** | Deployed repo root via script |
| **Environment file** | `/etc/mk04/prod/env`, source-input service env |
| **Restart policy** | N/A (oneshot). Failures must produce visible records (Phase 8 target) |
| **Log source** | `logger -t mk04-prod-daily-trigger` → journald; `logs.sh … scheduler` |
| **Health check** | `scheduler-status.sh`; `health.sh` Scheduler line (cron active + `/etc/cron.d/mk04` when installed) |
| **Dependencies** | Core always-running services healthy; runtime scheduler gate not disabled; config valid (readiness checks are Phase 6+ targets) |
| **Schedule (current)** | Cron, e.g. `0 8 * * *` per funnel in `deploy/cron/mk04.crontab` |
| **Control plane** | `start-scheduler` / `stop-scheduler` / `scheduler-status` (runtime flag in `data/<env>/control_state.json`) |

### Handoff retry sweeper

| Field | Value |
| --- | --- |
| **Service name** | No unit; cron oneshot |
| **Category** | Scheduled |
| **Required / optional** | **Required** for unattended handoff recovery |
| **Purpose** | Re-POST failed video-automation → output-funnel registrations from `report.json` |
| **Startup command** | `python3 …/video-automation/scripts/handoff_sweeper.py --quiet` |
| **Working directory** | Invoked with explicit env vars in crontab |
| **Environment file** | Crontab sets `PIPELINE_CONFIG_PATH`, `OUTPUT_FUNNEL_URL`, `VIDEO_AUTOMATION_JOBS_DIR` |
| **Restart policy** | N/A |
| **Log source** | Cron / journal markers (`handoff_sweeper`) |
| **Health check** | Indirect via watchdog stalled-job checks |
| **Dependencies** | output-funnel reachable; jobs directory readable |
| **Schedule (current)** | `*/10 * * * *` |

### Retention sweeper

| Field | Value |
| --- | --- |
| **Service name** | No unit; cron oneshot |
| **Category** | Scheduled |
| **Required / optional** | **Required** for unattended disk safety |
| **Purpose** | Tiered cleanup of media and aged job folders |
| **Startup command** | `/opt/mk04/prod/current/deploy/scripts/retention-sweeper.sh prod` |
| **Working directory** | Deployed repo root via script |
| **Environment file** | `/etc/mk04/prod/env` (`MEDIA_RETENTION_DAYS`, `RETENTION_DAYS`) |
| **Restart policy** | N/A |
| **Log source** | `logger` / journald |
| **Health check** | Manual / disk pressure in `health.sh` |
| **Dependencies** | Writable job/output paths |
| **Schedule (current)** | `30 3 * * *` |

### Health watchdog

| Field | Value |
| --- | --- |
| **Service name** | No unit; cron oneshot |
| **Category** | Scheduled |
| **Required / optional** | **Required** for unattended alerting |
| **Purpose** | Periodic doctor + stalled upload + disk checks; write alerts |
| **Startup command** | `/opt/mk04/prod/current/deploy/scripts/watchdog.sh prod` |
| **Working directory** | Deployed repo root via script |
| **Environment file** | `/etc/mk04/prod/env`, output-funnel env; optional `WATCHDOG_NOTIFY` |
| **Restart policy** | N/A |
| **Log source** | `/var/log/mk04/prod/watchdog/last_status.json`, `alerts.log`, `logger -t mk04-prod-watchdog` |
| **Health check** | Presence of recent `last_status.json`; non-zero exit on failure |
| **Dependencies** | Core HTTP services for `doctor.sh` probes |
| **Schedule (current)** | `*/15 * * * *` |

---

## Health and status surfaces (current)

| Surface | What it covers today |
| --- | --- |
| `./scripts/ops/status.sh` | Environment, services, uploads, scheduler summary |
| `./scripts/ops/health.sh` | Config, env file, API `/healthz`, systemd units, scheduler, DB, disk, GPU, upload safety |
| `./deploy/scripts/doctor.sh` | source-input, video-automation, output-funnel `/healthz` |
| Operations UI | Dashboard / recovery views (service-dependent) |
| Watchdog | Periodic doctor + queue/disk alarms |

Not yet implemented (Reliability later phases): boot verification report,
execution lock visibility, run records, shared pipeline readiness gate.

---

## Autonomous production flow (current)

```text
cron / manual / SSH / UI
    ↓
scripts/ops/run-pipeline.sh
    ↓ (config + boot readiness; scheduled respects runtime disable)
POST /run-funnel  (source-input / API)
    ↓
POST /jobs        (video-automation / Worker)
    ↓
POST /registrations/from-job  (output-funnel)
    ↓
in-process plan worker + upload worker
```

Supporting scheduled jobs: handoff sweeper (retry registration), retention
sweeper (disk), watchdog (alerts).

---

## Architectural observations

Document only — **do not fix in this phase**.

1. **Naming mismatch** — Plan uses `mk1-*`; repo and ops use `mk04-*`. Keep
   `mk04-*` for Phase 2 unless a deliberate rename is scheduled.
2. **API vs Worker mapping** — Plan “API” is source-input; plan “Worker” is
   video-automation (Flask server, not a queue consumer process). Operator
   modes already encode this (`api` / `worker`).
3. **Output funnel missing from plan core list** — It is a genuine required
   always-running production service and must remain in the inventory and unit
   set.
4. **AI Service requiredness inconsistency** — `deploy/systemd/README.md`
   marks AI optional; `scripts/ops` restart treats `ai` as a required target.
   Inventory records both: optional for hosts without local LLM, required when
   local selection is configured.
5. **Ops-ui / output-funnel restart classification** — Restart helper marks
   `ops-ui` and `output-funnel` optional; deploy docs treat output-funnel as
   core for autonomous publish. Prefer treating output-funnel as **required**
   in Reliability work.
6. **Shared pipeline entrypoint + lock + records** — `run-pipeline.sh` with
   execution lock and canonical run records (Phases 6–8).
7. **Scheduler is cron** — `run-scheduled.sh` is the only pipeline trigger from
   cron. A future timer must still call that script (no second execution path).
8. **Startup order vs readiness** — Phase 3 keeps AI and ops-ui independent of
   the pipeline peer chain. Scheduled runs gate on HTTP readiness of API,
   Worker, and Output funnel — not on AI process start order.
9. **In-process workers inside output-funnel** — Upload and plan workers are
   threads, not units. Do not invent separate units for them.
10. **Job recovery boundary** — Handoff sweeper retries *registration handoff*
    (idempotent). That is not partial-job resume of render/upload mid-flight.
    Keep that distinction.
11. **DEV path** — Units assume `/opt/mk04/prod/current`. Dev uses checkout +
    `run.sh` / `run-all-local.sh`; do not point prod units at the active
    development tree.
12. **Legacy config references in cron** — Handoff sweeper crontab still sets
    `PIPELINE_CONFIG_PATH=/etc/mk04/prod/video-automation/pipeline_config.json`
    (legacy path). Configuration & Deployment deferred full removal of
    `pipeline_config.json`; note for later cleanup, not Phase 2 blocking if
    the host still provides that file.

---

## Phase status

**Phase 2 (systemd unit files):** complete for the five always-running
`mk04-*` units (`Restart=always`, `RestartSec=5`, journal logging,
`WantedBy=multi-user.target`). See `deploy/systemd/README.md`.

**Phase 3 (startup order and readiness):** complete. Soft peer `After=` /
`Wants=` on pipeline units; ops-ui and AI independent; scheduler gate checks
HTTP readiness of required services before `POST /run-funnel`.

**Phase 4 (restart recovery verification):** complete. Policy-only and live
kill/recover smoke in `scripts/smoke/smoke_restart_recovery.py`; operator guide
in [RESTART_RECOVERY.md](./RESTART_RECOVERY.md).

**Phase 5 (boot verification):** complete. `scripts/ops/boot_verification.py`
reports READY / NOT READY from HTTP readiness and config/database/paths;
surfaced in `health.sh`, `status.sh`, and Operations UI dashboard.

**Phase 6 (shared pipeline entrypoint):** complete. `scripts/ops/run-pipeline.sh`
validates config, checks boot readiness, invokes POST `/run-funnel`, logs under
`runs/<env>/`.

**Phase 7 (execution lock):** complete. Per-environment lock at
`data/<env>/pipeline_execution.lock`; overlapping runs exit `5` with SKIPPED
record; stale locks detected and reported, not auto-cleared.

**Phase 8 (run records):** complete. Canonical history under
`runs/<env>/<run_id>/run_record.json` (+ `run.log`); every trigger leaves a
terminal SUCCESS / FAIL / SKIPPED record. See [RUN_RECORDS.md](./RUN_RECORDS.md).

**Phase 9 (scheduler):** complete. Cron → `scripts/ops/run-scheduled.sh` →
`run-pipeline.sh --trigger scheduled`. See [SCHEDULER.md](./SCHEDULER.md).

**Phase 10 (scheduler control):** complete. Canonical ops interface is
`stop-scheduler` / `start-scheduler` / `scheduler-status` only; runtime flag in
`data/<env>/control_state.json`; stop does not interrupt running pipelines.

**Phase 11 (reliability smoke):** complete. End-to-end validation via
`scripts/smoke/smoke_reliability.py` — see [RELIABILITY_SMOKE.md](./RELIABILITY_SMOKE.md).

**Reliability & Recovery subsystem: complete.** Next: Operations & Observability.

Non-blocking issues to carry forward:

- Align required/optional labels across deploy docs and `scripts/ops`
- Decide whether AI is required on the production host that uses local models
- Plan migration from cron → timer only after `run-pipeline` exists
