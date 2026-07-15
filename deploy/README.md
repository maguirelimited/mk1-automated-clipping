# mk0.4 Deployment Runbook

This directory contains the portable setup path for running the mk0.4 services
outside the original development machine. The current application is Python,
bash, and PATH-based CLIs; Linux readiness is mostly about installing the
runtime stack, setting real absolute paths, and keeping runtime storage durable.

## Runtime Environments

The repo now runs one codebase with environment-specific runtime roots. Select
the runtime with `MK04_ENV=dev|prod` or by passing the environment as the first
argument to the run scripts, for example `./deploy/scripts/run-all-local.sh dev`.

Recommended roots:

- DEV code: `/Users/anthonymaguire/VAmk0.4`
- PROD code: `/opt/mk04/prod/current`
- Config: `/etc/mk04/dev` and `/etc/mk04/prod`
- Runtime data: `/var/lib/mk04/dev` and `/var/lib/mk04/prod`
- Logs: `/var/log/mk04/dev` and `/var/log/mk04/prod`

`deploy/scripts/env.sh` centralizes path, port, service URL, database, controls,
log, scheduler, and upload-mode resolution. PROD refuses to start from an active
user checkout and expects the deployed copy at `/opt/mk04/prod/current`. The
supported production startup path is `deploy/scripts/run-*.sh prod`; direct
Python entrypoints are unsupported for PROD and fail closed when required prod
runtime variables are missing.

Default ports are DEV `5160/5150/5155/5175/5170` and PROD
`5060/5050/5055/5075/5070` for source-input, video-automation, output-funnel,
ai-service, and ops-ui respectively. STAGING can be added later by adding a new
env branch and matching config directory.

The hard upload gate is `MK04_UPLOAD_MODE=dry_run|real`. DEV defaults to
`dry_run`; `real` is only accepted when `MK04_ENV=prod`. In dry-run mode,
output-funnel claims due jobs and records a safe dry-run result without calling
the platform adapter.

## Canonical DEV → PROD Promotion

Production code delivery is **only** through the atomic promoter:

```bash
./deploy/scripts/promote-to-prod.sh --dry-run
./deploy/scripts/promote-to-prod.sh --no-restart   # first bootstrap / no systemd yet
./deploy/scripts/promote-to-prod.sh                # normal: validate staging, switch, restart, health
```

Layout under `/opt/mk04/prod` (override with `MK04_PROD_BASE`):

```text
current -> releases/<UTC>_<shortcommit>[_dirty]
previous -> releases/<previous>
releases/
dependency-bundles/<dependency_hash>/
```

Behaviour:

- Snapshots into `releases/.staging-<id>`, validates **that** tree, then atomically
  renames and switches the `current` symlink.
- Dirty working trees are allowed by default (warned + recorded); `--require-clean`
  refuses them.
- Retains current, previous, and two older releases by default (`--retain-releases`).
- Restart/health failure restores `previous` when available.
- Never enables uploads, never installs cron, never runs a content pipeline.
- Do **not** `git pull` or hand-edit `/opt/mk04/prod/current`.
- Do **not** use a hand-typed raw `rsync` into `current`.

Repo-root `./update.sh prod` validates/restarts the **already selected** current
release. It refuses `--pull` for production.

See [docs/operations/RUNBOOK.md](../docs/operations/RUNBOOK.md) for operator
checks, flags, and dual config roots. For secure remote access, see
[docs/operations/SSH_ACCESS.md](../docs/operations/SSH_ACCESS.md). To open the
Operations UI from a laptop without public exposure, use an SSH local port
forward — see [docs/operations/REMOTE_UI_ACCESS.md](../docs/operations/REMOTE_UI_ACCESS.md).

1. Run DEV and verify the change:

```bash
cd /path/to/dev/checkout
./deploy/scripts/run-all-local.sh dev
./deploy/scripts/doctor.sh dev
```

2. Preview / promote:

```bash
./deploy/scripts/promote-to-prod.sh --dry-run
./deploy/scripts/promote-to-prod.sh --no-restart   # if services not installed yet
# or:
./deploy/scripts/promote-to-prod.sh
```

3. First bootstrap (host not yet provisioned — separate from promotion):

```bash
# After a --no-restart promotion created current -> releases/...
sudo ./deploy/scripts/bootstrap.sh prod   # creates runtime dirs; deps come from dependency-bundles
./deploy/scripts/doctor.sh prod --preflight-only
```

Keep `MK04_UPLOAD_MODE=dry_run` until the production UI banner, credentials,
channels, controls, and queue state are verified. Only then switch
`MK04_UPLOAD_MODE=real` in `/etc/mk04/prod/env` and restart output-funnel.

If `current` is still a **legacy real directory** (pre-symlink layout), promotion
refuses to overwrite or delete it. Migrate manually into `releases/<id>/` and
point `current` at that release before using the promoter again.

## Services

- **source-input** (`127.0.0.1:5060` in prod): fetches one ready long-form
  input for a configured funnel.
- **video-automation** (`127.0.0.1:5050` in prod): transcribes, selects,
  validates, clips, and writes analytics events.
- **output-funnel** (`127.0.0.1:5055` in prod): registers completed clips,
  routes to channel profiles, schedules upload windows, and publishes via the
  YouTube adapter. Hosts an in-process upload worker and (when enabled) plan
  worker so external cron is only needed for the daily ingestion trigger.
- **ai-service** (`127.0.0.1:5075` in prod, optional): local LLM endpoint
  (`/ai/run`, `/health`) that fronts an Ollama backend for clip selection.
  Independent and runnable on its own; pipeline services only call it when
  configured to and keep functioning if it is stopped or removed.
- **ops-ui** (`127.0.0.1:5070` in prod, optional): read-mostly control
  panel. Not required for autonomous operation; pipeline services keep
  functioning if it is stopped or removed. Binds to localhost only so it is
  never publicly reachable. Operators access it remotely through an SSH tunnel
  (`LocalForward` to `127.0.0.1:5070`); see
  [docs/operations/REMOTE_UI_ACCESS.md](../docs/operations/REMOTE_UI_ACCESS.md).

All services are intentionally independent. The expected autonomous flow is:

```text
daily cron / n8n ─▶ POST /run-funnel (source-input)
                  ─▶ auto POST /jobs (video-automation)
                  ─▶ on success, POST /registrations/from-job (output-funnel)
                  ─▶ auto plan_due (schedule slots)
                  ─▶ in-process upload worker ─▶ upload at upload_at
```

## Fresh Ubuntu/Linux Setup

Run these from the Linux host that will execute the services. PROD should run
from a deployed copy, not the active development checkout.

```bash
sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv python3-pip \
  ffmpeg curl git

sudo mkdir -p /opt/mk04/prod /var/lib/mk04/locks
./deploy/scripts/promote-to-prod.sh --no-restart
# dependency-bundles are prepared during promotion; bootstrap seeds runtime dirs:
sudo ./deploy/scripts/bootstrap.sh prod
./deploy/scripts/doctor.sh prod --preflight-only
```

`ffmpeg` packages usually include `ffprobe`; confirm both resolve on `PATH`.
Install the Whisper CLI separately so the command `whisper` is available to the
same user/process that runs video automation. Linux installs may require a
deliberate PyTorch choice: CPU-only is conservative and simplest, CUDA can be
faster but depends on the host GPU, driver, and PyTorch wheel selection. Do not
add a GPU-specific install to bootstrap unless that host is intentionally GPU
managed.

Example CPU-oriented install into the video automation venv:

```bash
/opt/mk04/prod/current/video-automation/.venv/bin/python -m pip install -U openai-whisper
/opt/mk04/prod/current/video-automation/.venv/bin/whisper --help >/dev/null
```

If `whisper` is installed only inside the venv, run the service through the
provided run script or set `PATH`/`PYTHON_BIN` in your process manager so
`whisper` resolves.

## Configure Environment

Bootstrap creates runtime directories and seeds examples from `deploy/env/$MK04_ENV` into:

- `/etc/mk04/$MK04_ENV/env`
- `/etc/mk04/$MK04_ENV/source-input/funnels.json`
- `/etc/mk04/$MK04_ENV/video-automation/pipeline_config.json`
- `/etc/mk04/$MK04_ENV/video-automation/video_pipeline_profiles.json`
- `/etc/mk04/$MK04_ENV/video-automation/funnels/*.json`
- `/etc/mk04/$MK04_ENV/output-funnel/settings.json`
- `/etc/mk04/$MK04_ENV/output-funnel/channels.json`

Edit them on the Linux host before starting the services.

Required or strongly recommended values:

- `OPENAI_API_KEY` in `/etc/mk04/$MK04_ENV/env`; the doctor reports presence but
  never prints the key.
- `MK04_UPLOAD_MODE=dry_run` until you intentionally enable real prod uploads.
- `OUTPUT_FUNNEL_URL` and `OUTPUT_FUNNEL_HANDOFF_ENABLED=1` so completed clipping jobs register downstream.
  Raise `OUTPUT_FUNNEL_HANDOFF_TIMEOUT_SEC` from the 2s default to ~30s on
  slower hardware to absorb preflight latency.
- `YT_DLP_COOKIES_PATH` pointing at a
  durable Netscape `cookies.txt` so YouTube ingestion survives sign-in
  challenges on a headless server.
- YouTube OAuth token/client secret paths referenced by `channels.json`.

Keep these paths pinned in production so systemd, containers, or background
shells do not depend on the current working directory.

## Autonomous (`mk1`) Mode Checklist

This is the minimum required to run unattended:

1. Bootstrap on the target host: `./deploy/scripts/bootstrap.sh` creates all
   four venvs, copies `.env.example` files, and seeds
   `/etc/mk04/$MK04_ENV` from `deploy/env/$MK04_ENV`.
2. Edit secrets: `OPENAI_API_KEY` and (optionally) `OUTPUT_FUNNEL_SECRET`
   in `/etc/mk04/prod/env`.
3. Place YouTube OAuth artefacts on durable storage and reference them in
   `/etc/mk04/prod/env` (see **OAuth Refresh Path** below).
4. Confirm autonomy toggles in `/etc/mk04/prod/env`:
   - `OUTPUT_FUNNEL_AUTO_SCHEDULE=1`
   - `OUTPUT_FUNNEL_AUTO_UPLOAD=0` (uploads come from the worker, not handoff)
   - `OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED=1`, interval ≥15s
   - `OUTPUT_FUNNEL_PLAN_WORKER_ENABLED=1` (catches up scheduling if handoff
     fails once; see **Stalled Job Recovery** below)
   - `MK04_UPLOAD_MODE=dry_run` until the final deliberate switch to `real`.
5. Install systemd units from `deploy/systemd/` and enable all four (see
   `deploy/systemd/README.md`). Restart=always covers process crashes and
   reboots.
6. Install one cron entry per active `funnel_id` from `deploy/cron/mk04.crontab`
   (or import `deploy/n8n/daily-funnel-trigger.json` into n8n).
7. Install the watchdog cron entry so failures page out via journald and the
   optional `WATCHDOG_NOTIFY` hook (see **Watchdog** below).
8. Optional: install `deploy/logrotate/mk04` at `/etc/logrotate.d/mk04` for
   file-based log sinks. Systemd-journald already rotates stdout/stderr.

After this, the operator's only required interactions are: rotating YouTube
OAuth artefacts when they expire, refreshing `cookies.txt` when YouTube
challenges, and acting on watchdog alerts.

## Readiness Checks

Before starting services, check local binaries:

```bash
python3 --version
command -v ffmpeg
command -v ffprobe
command -v whisper
```

With both services running, use the repo doctor:

```bash
./deploy/scripts/doctor.sh dev
```

Or call endpoints directly:

```bash
curl -fsS http://127.0.0.1:5160/healthz   # dev source-input
curl -fsS http://127.0.0.1:5150/healthz   # dev video-automation
curl -fsS http://127.0.0.1:5160/doctor
curl -fsS http://127.0.0.1:5150/doctor
```

If `INPUT_SERVICE_SECRET` is set, include
`-H "X-Input-Service-Secret: $INPUT_SERVICE_SECRET"` when calling source-input
`/doctor`, `/funnels`, or `/run-funnel`.

The doctors are safe readiness checks. They validate the Python executable,
virtualenv status, Flask/import availability where relevant, required CLIs,
OpenAI key presence, config files, funnel config, and runtime path existence /
writability.

`GET /doctor` on both services always returns **HTTP 200** when the process is
reachable; use the JSON field **`"ok": true|false`** for readiness (failed
checks set `"ok": false` without a 5xx status). Tools that treat any non-2xx as
“offline” should probe **`GET /healthz`** for liveness or parse **`"ok"`** on
`/doctor`. The repo **`deploy/scripts/doctor.sh`** exits non-zero if **`"ok"`**
is false.

## Run Manually During Testing

To start the local mk1 stack from one terminal during development:

```bash
cd /Users/anthonymaguire/VAmk0.4
./deploy/scripts/run-all-local.sh dev
```

This starts source-input, video-automation, output-funnel, ai-service, and
ops-ui, then stops all child processes when you press `Ctrl+C`. It is a local
development helper; use systemd units for long-running Ubuntu deployments.

Each service is independent and can be started on its own with its matching
`run-*.sh` script — handy when you only want the local LLM endpoint without the
rest of the stack:

```bash
cd /Users/anthonymaguire/VAmk0.4
./deploy/scripts/run-ai-service.sh dev   # binds 127.0.0.1:5175 (dev), 5075 (prod)
```

The ai-service needs a reachable model backend (Ollama by default at
`http://localhost:11434`). It still starts and serves `/health` if the backend
is down — health just reports `backend_reachable: false`.

Terminal 1:

```bash
cd /Users/anthonymaguire/VAmk0.4
./deploy/scripts/run-input-service.sh dev
```

Terminal 2:

```bash
cd /Users/anthonymaguire/VAmk0.4
./deploy/scripts/run-video-automation.sh dev
```

Defaults:

- DEV binds source-input `5160`, video-automation `5150`, output-funnel `5155`, ai-service `5175`, ops-ui `5170`.
- PROD binds source-input `5060`, video-automation `5050`, output-funnel `5055`, ai-service `5075`, ops-ui `5070`.
- All of the above bind to `127.0.0.1` by default (`*_HOST` / `OPS_UI_HOST`).

### Operations UI remote access (standard production setup)

The Operations UI must remain **private-by-default**:

- `OPS_UI_HOST=127.0.0.1` (default in `deploy/scripts/run-ops-ui.sh` and env examples)
- Do **not** open the Ops UI port in the host firewall
- Do **not** put the UI on a public reverse proxy or cloud tunnel for operator access

Remote operators use **SSH local port forwarding**. SSH authenticates the
operator and encrypts the session; the browser on the client talks to
`http://localhost:<ops-ui-port>`, which is forwarded to the server’s loopback.
Full alias configuration, verification, and troubleshooting:

[docs/operations/REMOTE_UI_ACCESS.md](../docs/operations/REMOTE_UI_ACCESS.md)

If callers are remote, open firewall/security-group access only for the ports
they need. Keep source-input bound to localhost unless a remote orchestrator
must call it, and use `INPUT_SERVICE_SECRET` if it is exposed beyond localhost.
Likewise, keep video-automation and output-funnel private unless a trusted
caller requires remote access, and set `VIDEO_AUTOMATION_SECRET` /
`OUTPUT_FUNNEL_SECRET` when they are reachable beyond the local host.
**Never** treat the Operations UI as a public caller — use the SSH tunnel path
above instead.
Manual logs go to the terminal. Under a process manager, logs go to that
manager's stdout/stderr collection, such as `journalctl` for systemd.

## Minimal End-to-End Smoke Test

This exercises the service boundary without changing pipeline behavior.

1. Confirm services and doctors:

   ```bash
   ./deploy/scripts/doctor.sh
   ```

2. Ask source-input for one ready funnel input:

   ```bash
   curl -fsS \
     -H "Content-Type: application/json" \
     -d '{"funnel_id":"mfm_business_ai_001"}' \
     http://127.0.0.1:5160/run-funnel
   ```

   If `INPUT_SERVICE_SECRET` is configured, add
   `-H "X-Input-Service-Secret: $INPUT_SERVICE_SECRET"`.

3. If the response returns `status: "input_ready"`, source-input has already
   enqueued clipping on video-automation through `POST /jobs`. Inspect the
   returned metadata or `video-automation/jobs/` for the created job.

   ```bash
   curl -fsS http://127.0.0.1:5150/jobs/<job_id>
   ```

4. Fetch one returned clip URL:

   ```bash
   curl -fS -o /tmp/mk04-smoke-clip.mp4 \
     http://127.0.0.1:5150/output/<clip_file>
   ```

This final step can consume OpenAI and Whisper runtime, so use a small known
test video when possible.

## yt-dlp on Headless Linux

YouTube bot checks can differ between a laptop and a headless Linux host. The
input service already supports these runtime options:

- `YT_DLP_COOKIES_PATH=/absolute/linux/path/to/cookies.txt` is usually the most
  reliable server option. Export a Netscape-format `cookies.txt` from a browser
  and place it where the service user can read it.
- `YT_DLP_COOKIES_FROM_BROWSER=chrome` can work, but on headless Linux it may
  need Linux-specific browser/profile/keyring values and a real browser data
  directory.
- `YT_DLP_JS_RUNTIME=deno` or `YT_DLP_USE_DENO=1` can enable Deno for YouTube
  JavaScript challenge solving if needed.
- Keep `yt-dlp` updated in the input-service venv when YouTube behavior changes.

## Process Managers

For systemd, supervisord, Docker, or Kubernetes, use the run scripts as the
command entrypoints or translate their env exports into the process manager.
The important contract is:

- Load `MK04_ENV` and `/etc/mk04/$MK04_ENV/env`.
- Run from the service root, or set the same env vars the run scripts export.
- Keep `ffmpeg`, `ffprobe`, and `whisper` available on `PATH`.
- Use `PYTHON_BIN` only if you intentionally run a different Python than the
  repo venv.
- Persist the runtime directories listed below.
- Never run PROD from the active dev checkout; promote by copying the repo to
  `/opt/mk04/prod/current` and restarting the prod units.

Do not point multiple live workers at the same mutable runtime folders unless
you have designed external locking and storage isolation.

## Persistent Storage

Keep `/var/lib/mk04/$MK04_ENV` on durable disk if you need duplicate tracking,
auditability, clips, or troubleshooting after restarts:

- `source-input/state/seen_urls.json` — dedupe state; losing
  it causes duplicate downloads.
- `source-input/inputs/ready/`
- `source-input/inputs/rejected/`
- `source-input/tmp/` for in-flight downloads and debugging
- `video-automation/input/`
- `video-automation/output/`
- `video-automation/jobs/` — per-job `report.json`, `analytics.json`,
  `transcript_payload.json`, `selection.json`, clips.
- `video-automation/analytics/` — append-only event + feedback JSONL.
- `video-automation/temp/` if `artifact_retention.temp_policy` keeps debug
  artifacts
- `output-funnel/output_funnel.sqlite3` — upload queue truth. Losing it
  loses scheduled and in-flight uploads.
- `ops-ui/ops_ui.sqlite3` and `ops-ui/controls.json` — operator
  audit and emergency pause flags.
- `/var/lib/mk04/$MK04_ENV/credentials/*.token.json` and
  `*.client_secret.json` — YouTube OAuth artefacts.
- `/var/lib/mk04/$MK04_ENV/credentials/youtube.cookies.txt` — yt-dlp session cookies.

In ephemeral containers or short-lived VMs, mount these as volumes. Losing
`seen_urls.json` causes duplicate source selection; losing
`output_funnel.sqlite3` loses planned uploads and breaks
`platform_asset_id` traceability; losing token files forces a re-OAuth.

Recommended backups (autonomous mode):

- Daily `sqlite3 .backup` of `output_funnel.sqlite3` and `ops_ui.sqlite3`.
- Daily archive of `seen_urls.json` and `video-automation/analytics/*.jsonl`.

## OAuth Refresh Path (YouTube)

Channel profiles reference token + client-secret files by **environment
variable name**, not path. The actual paths live in `/etc/mk04/$MK04_ENV/env`:

```bash
MFM_BUSINESS_AI_YT_TOKEN_FILE=/var/lib/mk04/prod/credentials/mfm_business_ai.token.json
MFM_BUSINESS_AI_YT_CLIENT_SECRET_FILE=/var/lib/mk04/prod/credentials/mfm_business_ai.client_secret.json
```

Both files must be readable by the `mk04` service user and writable for the
token file (Google API client rewrites it on refresh). Recommended setup:

```bash
sudo mkdir -p /var/lib/mk04/prod/credentials
sudo chown -R mk04:mk04 /var/lib/mk04
sudo chmod 700 /var/lib/mk04/prod/credentials
sudo chmod 600 /var/lib/mk04/prod/credentials/*.json
```

The systemd unit files load `/etc/mk04/prod/env`, so the env reaching the
service matches manual test runs that source `deploy/scripts/env.sh prod`.
When tokens expire, the next real upload attempt records an OAuth error in
`/var/lib/mk04/prod/output-funnel/output_funnel.sqlite3` and the watchdog (or an upload that
hits `failed_terminal` after `publisher.max_attempts`) will surface it — at
that point, redo the OAuth flow, drop a fresh `*.token.json` into the same
path, and `systemctl restart mk04-output-funnel`.

## Watchdog

`deploy/scripts/watchdog.sh` runs every 15 minutes and asserts:

- `doctor.sh` (input + video + output-funnel `/healthz`)
- `GET /admin/stalled-jobs` (anything stuck longer than thresholds)
- `GET /admin/last-upload` (queue has pending jobs but nothing has uploaded
  recently → alarm; threshold via `WATCHDOG_STALE_UPLOAD_HOURS`, default 48)
- Free disk on the persistent paths (threshold via
  `WATCHDOG_DISK_MIN_FREE_PCT`, default 10)

On failure it writes three places so nothing vanishes silently:

- `/var/log/mk04/$MK04_ENV/watchdog/last_status.json` (single-file tailable status)
- `/var/log/mk04/$MK04_ENV/watchdog/alerts.log` (append-only history)
- journald via `logger -t mk04-$MK04_ENV-watchdog`

If `WATCHDOG_NOTIFY` is set, the script pipes the summary to it for
mail/Slack/ntfy delivery; if that command itself fails, the failure is
appended to `alerts.log` so you still have a record. Cron's `MAILTO` works
too as a fallback. If `/var/log/mk04` is not writable, the script falls back
to `deploy/.watchdog/` so a fresh box still records alerts.

Install via:

```cron
*/15 * * * * /opt/mk04/prod/current/deploy/scripts/watchdog.sh prod >/dev/null 2>&1
```

## Stalled / Orphaned Job Recovery

Three layers cover the autonomy gap between clipping and publishing:

1. **Plan worker** (`output-funnel`): every 300s, sweeps any `registered` /
   `routed` rows back into the planned schedule. Configured via
   `OUTPUT_FUNNEL_PLAN_WORKER_INTERVAL`.
2. **Handoff retry sweeper** (`video-automation/scripts/handoff_sweeper.py`):
   every 10 min, walks `video-automation/jobs/<...>/report.json` and
   re-POSTs to `/registrations/from-job` when the original one-shot handoff
   failed. Self-contained stdlib script; idempotent because output-funnel
   dedupes by clip durable id. Bounded by `--max-attempts` (5) and
   `--max-age-hours` (24) to avoid hammering ancient failures.
3. **Stalled-job alarm** (`GET /admin/stalled-jobs`): if anything stays in
   `registered` / `routed` / `uploading` longer than
   `settings.json:stalled_jobs.*_seconds`, the watchdog fails loud.

## Retention Sweeper

`deploy/scripts/retention-sweeper.sh` does **tiered** cleanup so large media
does not build up forever while small per-job metadata is kept long enough for
debugging, analytics, review, and training.

Two age thresholds:

- `MEDIA_RETENTION_DAYS` (default 5) — large media. Removes per-job
  `jobs/*/input_*` source copies, per-job `jobs/*/clips/*` clip mirrors,
  `output/*` clip files, `temp/*` scratch, and orphaned `input/*` source files.
- `RETENTION_DAYS` (default 14) — whole per-job folders. A `jobs/<job>/` folder
  is deleted (metadata included) only once nothing inside it is newer than this
  threshold.

Preserved until the whole job folder ages out (then removed with it):
`report.json`, `selection.json`, `transcript.json`, `transcript_payload.json`,
`analytics.json`, `review.md`, `task.json`. Never swept at any threshold:
`analytics/*.jsonl`, source-input state (`seen_urls.json`, `input_jobs/*`), and
`output-funnel/output_funnel.sqlite3` — the sweeper does not touch those dirs.

Set both knobs in `/etc/mk04/$MK04_ENV/env`, or override per-invocation with
`--media-days N` / `--days N`. Pass `--dry-run` for a no-op preview and
`--quiet` to suppress stdout (journald logging via `logger` still happens).

Installed nightly in prod via `deploy/cron/mk04.crontab`. Dev is manual; run on
demand (preview first, then for real):

```bash
./deploy/scripts/retention-sweeper.sh dev --dry-run
./deploy/scripts/retention-sweeper.sh dev
```

A commented dev cron line is included in `deploy/cron/mk04.crontab` for dev
boxes that should self-clean.

## Normal Orchestration

1. `POST /run-funnel` to source-input with `{ "funnel_id": "..." }` (one cron
   line per active funnel; see `deploy/cron/mk04.crontab`).
2. On `input_ready`, source-input enqueues video-automation `POST /jobs`
   internally.
3. Clips appear under `video-automation/output/`; per-job artefacts live
   under `video-automation/jobs/<job_id>/`.
4. video-automation POSTs the success report to
   `output-funnel/registrations/from-job`. Output-funnel auto-plans upload
   slots and the in-process upload worker processes each clip when its
   `upload_at` arrives. With `MK04_UPLOAD_MODE=dry_run`, this records a safe
   dry-run upload; with `MK04_ENV=prod` and `MK04_UPLOAD_MODE=real`, it calls
   the platform adapter.
5. The watchdog polls every 15 minutes; operator alerts only on
   non-zero exit.
6. Optional: `POST /analytics/feedback` on video-automation once platform
   metrics are available (deferred until views are proven).

## Common Failures

- **video `/doctor` says `OPENAI_API_KEY` missing**: set it in
  `video-automation/.env` or the process manager.
- **video `/doctor` says `whisper` missing**: install the Whisper CLI into the
  runtime path used by the service.
- **doctor reports `python_venv` false**: the service is running outside the
  expected venv. Use the run scripts or set `PYTHON_BIN` intentionally.
- **doctor reports a path is not writable**: fix ownership/permissions for the
  service user or mount a writable volume.
- **source `/doctor` says funnels config invalid**: fix
  `source-input/input_service/config/funnels.json`; use `GET /funnels` to view
  the validated manifest.
