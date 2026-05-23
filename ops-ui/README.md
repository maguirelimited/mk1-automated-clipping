# mk04 Ops UI

This is the first internal operations/control panel for the single-machine mk1
deployment. It is intentionally small: a standalone Flask app with
server-rendered HTML, no frontend build step, and no business logic imported
from the pipeline services.

## Architecture

The UI is a control-plane process that sits above the existing services:

- `source-input` remains the ingestion service on `127.0.0.1:5060`.
- `video-automation` remains the clipping worker/API on `127.0.0.1:5050`.
- `output-funnel` remains the upload queue/API on `127.0.0.1:5055`.
- `ops-ui` runs separately on `127.0.0.1:5070`.

The UI reads service state through HTTP APIs first:

- `source-input`: `/healthz`, `/funnels`, `/run-funnel`
- `video-automation`: `/healthz`, `/jobs`
- `output-funnel`: `/healthz`, `/queue`, `/queue/plan-due`, `/queue/upload-due`

It uses systemd for process control and journal logs on Ubuntu. It keeps only
UI-owned control state in `ops-ui/data/ops_ui.sqlite3`: pause flags and a small
operator action audit. The pipeline services keep functioning if this UI is
stopped or deleted.

## Stack

- Python 3.10+
- Flask
- SQLite for local UI control state
- systemd/journalctl on Ubuntu for service operations
- Plain HTML/CSS templates

This is enough for mk1 because the primary workflow is operational visibility
and manual control, not consumer-grade interactivity.

## Run Locally

```bash
cd ops-ui
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m ops_ui
```

Open `http://127.0.0.1:5070`.

Useful environment variables:

```bash
OPS_UI_HOST=127.0.0.1
OPS_UI_PORT=5070
OPS_SOURCE_INPUT_URL=http://127.0.0.1:5060
OPS_VIDEO_AUTOMATION_URL=http://127.0.0.1:5050
OPS_OUTPUT_FUNNEL_URL=http://127.0.0.1:5055
OPS_SOURCE_INPUT_UNIT=mk04-source-input.service
OPS_VIDEO_AUTOMATION_UNIT=mk04-video-automation.service
OPS_OUTPUT_FUNNEL_UNIT=mk04-output-funnel.service
```

If service secrets are configured, run `ops-ui` with the matching environment
variables (`INPUT_SERVICE_SECRET`, `VIDEO_AUTOMATION_SECRET`,
`OUTPUT_FUNNEL_SECRET`) so it can call protected endpoints.

## Ubuntu Service Control

On Ubuntu, create systemd units for the three existing services and optionally
for `ops-ui`. The UI defaults expect these unit names:

- `mk04-source-input.service`
- `mk04-video-automation.service`
- `mk04-output-funnel.service`
- `mk04-ops-ui.service`

The UI executes:

```bash
systemctl start mk04-source-input.service
systemctl stop mk04-source-input.service
systemctl restart mk04-source-input.service
journalctl -u mk04-source-input.service -n 80 --no-pager
```

If `ops-ui` is not running as root, allow only the required service actions via
sudoers rather than giving broad shell access.

Example sudoers shape:

```sudoers
mk04 ALL=NOPASSWD: /bin/systemctl start mk04-source-input.service, /bin/systemctl stop mk04-source-input.service, /bin/systemctl restart mk04-source-input.service
mk04 ALL=NOPASSWD: /bin/systemctl start mk04-video-automation.service, /bin/systemctl stop mk04-video-automation.service, /bin/systemctl restart mk04-video-automation.service
mk04 ALL=NOPASSWD: /bin/systemctl start mk04-output-funnel.service, /bin/systemctl stop mk04-output-funnel.service, /bin/systemctl restart mk04-output-funnel.service
```

Keep the services bound to localhost unless there is a specific trusted remote
operator path.

## MVP Screens

The first dashboard includes:

- Service cards with HTTP liveness and systemd state.
- Start, stop, and restart controls for each service.
- Funnel run control with ingestion pause (enforced in `source-input` via shared controls file).
- Output planning/upload controls with emergency upload stop (enforced in `output-funnel`).
- Video job status and current stage.
- Upload queue status, retry count, schedule/upload times, and title.
- Recent failures from video jobs and upload queue rows.
- Basic machine stats: CPU count/load, RAM, disk, GPU via `nvidia-smi` if present.
- Recent journal logs per service.
- Local operator action audit.

### Recovery / reliability (`/failed`, `/recovery`)

- **Failed jobs** page: failure reason, stage, timestamps, retry counts; per-row upload retry; video re-queue via `input_id`; bulk upload retry; dead-letter section (`failed_terminal`, `missed_upload_window`).
- **Recovery** page: global pause/sync status, queue DB path, persisted queued jobs after restart, stuck-job heuristics, recent recovery actions.

Shared controls file (default `ops-ui/data/controls.json`, override with `MK04_CONTROLS_FILE`):

```json
{
  "ingestion_paused": false,
  "uploads_paused": false,
  "updated_at": "..."
}
```

`source-input` rejects `POST /run-funnel` when `ingestion_paused` is true. `output-funnel` skips `POST /queue/upload-due` and the background upload worker when `uploads_paused` is true.

Optional stuck thresholds: `OPS_UI_STUCK_RUNNING_SEC`, `OPS_UI_STUCK_QUEUED_SEC`, `OPS_UI_STUCK_UPLOADING_SEC`. Funnel run timeout: `OPS_UI_FUNNEL_RUN_TIMEOUT_SEC` (default 900).

### Funnel management (`/funnels`)

- Per-funnel overview: health, status, last run / last success (input ledger), failure counts, queue depth, active video/upload jobs.
- **Pause/resume** per funnel (UI-owned; blocks manual **Run** from Mission Control).
- **Manual trigger** per row; trigger history from the UI audit log; journal snippet filtered by funnel id.
- Read-only config visibility: active sources, pipeline profile, platform targets, clip `max_clips`, channel cadence (`OUTPUT_FUNNEL_CHANNELS` or `output-funnel/config/channels.example.json`).
- Config **enable/disable** (`active` in `source-input` `funnels.json`) is shown but not edited in the UI.

### Deep diagnostics

- **Job detail** (`/jobs/video/<job_id>`, `/jobs/upload/<id>`): pipeline stage breakdown, timings, funnel/profile, input-ledger context, transcript/selection/metadata viewers, clip file table, ffmpeg-related error excerpts, traceback blocks, filtered journal snippet, raw debug JSON. Dashboard and failed-job tables link into these pages.
- **Health** (`/health`): aggregates `GET /doctor` from source-input and video-automation; output-funnel `GET /healthz` plus local SQLite `PRAGMA quick_check`; improved GPU table.
- **Logs** (`/logs`): search by free text and/or job id, download filtered journal export (`/logs/download`).

Optional local paths when Mission Control runs on the pipeline host:

```bash
OPS_INPUT_LEDGER_DIR=/path/to/source-input/.../data/state/input_jobs
OPS_OUTPUT_FUNNEL_DB=/path/to/output-funnel/data/output_funnel.sqlite3
```

Artifact JSON/transcript viewers read files from paths returned by `GET /jobs/<id>/debug` (same machine as video-automation job dirs).

## Boundaries

The UI does not import pipeline business logic. Pause flags are mirrored to a JSON file that services read; callers that bypass `run-funnel` / `upload-due` (direct n8n hooks, manual API) are not blocked.

