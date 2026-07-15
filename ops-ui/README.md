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

## Funnel Management MK1 (canonical schema)

The operator-facing funnel schema lives in `ops_ui/funnel_management/schema.py`.
It defines **persistent funnel configuration only** (`schema_version`, `identity`,
`acquisition`, `processing`, `distribution`, `mappings`).

- **Readiness** is derived later by the validation engine — not stored in the schema.
- **Operations** (pause, manual run, archive) are UI/service actions — not schema fields.
- **Templates** are creation tools — only optional `identity.template_source` metadata is stored.

Prompt 4 implements parse/serialise (`load_canonical_funnel`, `dump_canonical_funnel`) and
strict validation only.

The **Funnel Registry** (`ops_ui/funnel_management/registry.py`) stores one validated
canonical funnel per file under a local registry directory (default:
`ops-ui/data/funnel_registry`, override with `OPS_FUNNEL_REGISTRY_DIR`). It does not import
existing subsystem configs, calculate readiness, store runtime state, or sync to runtime
config files yet — those come in later prompts (import, validation, UI, sync).

Prompt 6 adds a **read-only import layer** (`ops_ui/funnel_management/importer.py`) that
reads existing source-input, video-automation, output-funnel, and ai-service config files
and builds canonical funnel objects. It can save imported funnels into the local registry
via `import_to_registry()` but does not write back to runtime configs, calculate readiness,
or modify production config.

Prompt 7 adds a **validation engine** (`ops_ui/funnel_management/validation.py`) that
derives readiness from canonical funnels plus optional dependency paths. Validation separates
configuration checks (reusing the Prompt 4 schema) from dependency checks (source-input,
processing, AI rules, output routes, ConfigManager mapping). Results are returned as a
report only — readiness is not persisted into canonical config, runtime files are not
modified, and UI pages will consume these reports in later prompts.

Prompt 8 updates **`/funnels`** to list canonical funnels from the registry with derived
readiness from the validation engine. Operational run health (pause, last run, failures,
queue depth) is shown separately in a compact overlay. The page is read-only for canonical
config; create, clone, edit, and sync flows come in later prompts.

Prompt 9 adds **`/funnels/<funnel_id>`**, a read-only detail page for one canonical funnel.
It shows identity, acquisition, processing, distribution, mappings, derived readiness issues,
and compact operational state. Readiness is not persisted; edit, clone, and sync come later.

Prompt 10 adds **built-in funnel templates** (`ops_ui/funnel_management/funnel_templates.py`).
Templates are creation tools only: they generate draft canonical funnels with conservative
defaults (`draft`, `enabled=false`, `posting_enabled=false`, empty sources/routes unless
provided). Templates do not write to the registry or runtime configs; the Create Funnel Wizard
comes in Prompt 11.

Prompt 11 adds **`/funnels/new`**, a CSRF-protected create wizard that saves draft funnels
from built-in templates to the canonical registry only. New funnels remain disabled with
posting off; runtime config sync is not implemented yet. After create, list/detail pages
show derived readiness and what is still missing.

Prompt 12 adds **`/funnels/<funnel_id>/clone`**, which copies an existing registry funnel
into a new draft canonical funnel (always disabled, posting off). Clone writes only to the
registry; copied channel routes are canonical intent until output-funnel sync is added later.

Prompt 13 adds **`/funnels/<funnel_id>/edit`**, which updates identity, acquisition, processing,
distribution route references, and mappings in the canonical registry only. Editing does not
sync runtime configs; readiness remains derived after save. Prompt text, credentials, analytics,
and scheduler controls are out of scope.

Prompt 15 adds a backend **configuration synchronisation layer** (`FunnelSynchronizer`) that can
build dry-run plans and explicitly apply projections to source-input `funnels.json`, video
`config/funnels/<funnel_id>.json`, and output-funnel `channels.json` routing membership. It
validates AI aliases and ConfigManager mappings without writing them. UI confirmation workflow
comes in Prompt 16.

## Run Locally

```bash
cd ops-ui
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m ops_ui
```

Open `http://127.0.0.1:5070/ops` — the **Operator Console** is the canonical
daily landing page (`/` redirects to `/ops`).

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

## Authentication (Phase 13)

The Operations UI requires a single operator password before any operational page
or JSON observability endpoint is available.

| Variable | Purpose |
| --- | --- |
| `OPS_UI_OPERATOR_PASSWORD` | Operator password (required when auth is enabled) |
| `OPS_UI_SECRET_KEY` | Flask session signing key (set a long random value in prod) |
| `OPS_UI_SESSION_LIFETIME_MINUTES` | Inactivity timeout (default `60`) |
| `OPS_UI_AUTH_DISABLED` | Set to `1` only for local automated tests |

Behaviour:

* Unauthenticated requests to `/ops/*` redirect to `/login`
* Unauthenticated JSON/API routes return `401`
* Sessions expire after inactivity; logout clears the session
* CSRF tokens are issued for login and logout (foundation for Phase 14 forms)
* Login, logout, auth failures, and session expiry are written to `action_log`

Auth is implemented under `ops_ui/auth/` and is separate from route handlers.
No RBAC, OAuth, MFA, or multi-user management.

**Do not expose the Ops UI on the public internet.** It binds to `127.0.0.1` by
default. For remote operator access from a laptop, use an SSH local port forward
(not a public bind or reverse proxy):
[docs/operations/REMOTE_UI_ACCESS.md](../docs/operations/REMOTE_UI_ACCESS.md).

## Operator Console (`/ops`)

`/ops` is the daily **MK1 Operator Console** — the front panel of the machine.
It answers the current state of the system and what the operator should do next.
It is intentionally **not** a metrics dashboard: no charts, no business
analytics, no publishing approval workflow.

### Mental model

| Tier | Pages | Role |
| --- | --- | --- |
| **Daily loop** | Console · Outputs · Runs · Jobs · Failures | Normal operator workflow |
| **Diagnostics** | Storage · Configuration · Health · Logs | Investigation and verification |
| **Legacy / Advanced** | Mission Control · Legacy failed jobs · Recovery · Legacy settings · Funnels · Publishing | Deep tools; not the default path |

Modern `/ops/*` pages are drill-downs from the Console. Legacy tools remain
available but are labelled **Advanced / legacy** in the UI and are muted in
Console quick links.

### Console sections (top to bottom)

1. **Overall health** — aggregate readiness from observability `/health`
2. **Environment / safety** — dev vs production, upload and scheduler state
3. **Needs Attention** — concise operator to-do list (see below)
4. **Current activity** — idle / running / failing; link to active run when present
5. **Last run** — most recent completed or failed run with drill-down links
6. **Automation / service summary** — compact service health table
7. **Safe actions** — Run pipeline and operational controls (Console only)
8. **Quick links** — daily loop, diagnostics, and muted legacy shortcuts

Data comes from the observability backend only (`scripts/observability/*`).
If the backend is disconnected, the banner shows **DISCONNECTED** and values
are **not fabricated**.

Implementation: `ops_ui/overview.py`, `ops_ui/shell.py`.

### Daily workflow

```text
Open /ops
Check health and Needs Attention
Run pipeline if appropriate (Safe actions)
Monitor current activity or last run
Inspect Runs → Jobs → Outputs as needed
Use Failures when something breaks
Return to Console
```

Detail pages (`/ops/runs/<id>`, `/ops/jobs/<id>`, `/ops/outputs/...`) link back
to the Console and to the next relevant drill-down in the loop.

### Needs Attention

Needs Attention is an operator to-do list derived from **existing**
observability state only:

* system health (PASS / WARN / FAIL)
* readiness and configuration failures
* unhealthy services, disk pressure, stale execution lock
* upload / scheduler state (production warnings)
* failed or blocked pipeline activity
* last run failed (only when a real `run_id` exists — no invented links)

Properties: concise (capped at five visible items), severity-ordered (action →
warning → info), actionable (each item has an explanation and link). Overflow
items point to **Failures**. Empty state: “Nothing needs attention right now.”

### Outputs (`/ops/outputs`)

The canonical **daily clip review** surface. Run-centric: it answers whether
the latest successful run produced good clips.

* **Default:** latest successful run (`SUCCESS` from run records)
* **Previous runs:** `?run_id=<run_id>`
* **Deep-link fallback:** `?job_id=<job_id>` when run context must be resolved
  from a job (not the primary operator path)
* **Clip cards:** inline preview, title/hook, duration, score when present, job
  identity
* **Detail:** `/ops/outputs/<job_id>/<clip_id>` for diagnostic inspection and
  preview
* **Review only** — inspect clips from a successful run
* **Not** an approval gate, upload manager, analytics dashboard, or publishing
  workflow
* The main list page does **not** show validation/posting/reframe badges or
  approve/reject controls

Data comes from observability run-scoped indexing
(`scripts/observability/outputs.py`: `latest_successful_run_id`,
`list_clips_for_run`). The legacy JSON index `GET /outputs` (optional
`?job_id=`) remains for API clients; the UI is run-centric.

Run and job drill-down pages link to `/ops/outputs?run_id=<run_id>` when run
context is available.

**Face-track / reframe outcomes** — see the **Reframing** section on clip detail
and reframe badges on the Job Inspector. Badge meanings, acceptable outcomes,
and the full dev operating procedure (enable test mode, pre-run checklist,
go/no-go) are in the canonical doc:
[video-automation/docs/face_track_test_mode.md](../video-automation/docs/face_track_test_mode.md).

Quick pointer: **Face-track** = used crop; **Fallback** = eligibility rejected
(blur used, normal); **Unknown** = old job without reframe metadata. Fallback
is not an error.

**Legacy Clip Review (`/clip-review`) — deprecated.** GET requests redirect to
`/ops/outputs`. POST approve/reject/flag and policy-control toggles return **410
Gone** (retired; they never gated publishing). Feedback and requeue POST routes
remain for backwards compatibility but are not linked from normal navigation.

### Failures (`/ops/failures`)

Daily failure triage. Aggregates failed runs, failed jobs, unhealthy services,
and validation failures from existing observability sources
(`scripts/observability/failures.py`, `GET /failures`). Groups by category with
suggested next inspection targets. **Open in Jobs** links to the modern filtered
jobs list — not to legacy `/failed`.

**Legacy failed jobs** (`/failed`) retains upload-queue and recovery controls;
use it for advanced recovery, not daily triage.

### Diagnostics

| Page | Path | Purpose |
| --- | --- | --- |
| Storage | `/ops/storage` | Disk, retention, backup, storage warnings |
| Configuration | `/ops/configuration` | Read-only ConfigManager view — verification only, no editing |
| Health | `/health` | Service doctor and readiness checks (HTML in browser) |
| Logs | `/logs` | Service journal search and download |

**Configuration vs settings:** `/ops/configuration` is read-only verification.
**Legacy settings** (`/settings`) is the editable service-settings page (AI,
transcription, etc.).

**Failures vs Health vs Logs:** Failures is operator triage (what broke, where
to inspect). Health is aggregate service doctor checks. Logs is raw journal
search. Health and Logs are **diagnostic**, not obsolete legacy.

### Safe controls (Console)

| Risk | Actions |
| --- | --- |
| Lower | Refresh health, **validate config** (ConfigManager diagnostic — does not edit config), stop/start scheduler, disable uploads, trigger **dev** run |
| Higher (extra confirm) | Enable uploads, restart service, restart all, trigger **prod** run |

* **Run pipeline** appears only on the Console — no duplicate primary button elsewhere
* Auth, CSRF on every POST, audit logging, and confirmation pages for high-risk
  actions are unchanged
* Production run and enable-uploads require explicit confirmation; prod run is
  blocked when the UI environment is dev

Route: `POST /ops/actions/<action>` (`ops_ui/controls.py`).

### `/health` compatibility

One route serves two clients via **Accept-header negotiation**:

| Client | `Accept` | Response |
| --- | --- | --- |
| Browser (nav link to Health) | prefers `text/html` | HTML diagnostic page (`health.html`) |
| API clients, curl, test client default | JSON or neutral | Versioned JSON envelope (`SystemHealth` under `data`) |

There is no route shadowing — a single handler in `ops_ui/observability.py`
branches on `_wants_health_html()`. The JSON contract is unchanged; see
`tests/test_observability_endpoints.py`.

Shell banner and badge still consume the same `build_system_health()` data as
`GET /health` JSON.

## Navigation and routes

Primary nav (daily): **Console · Outputs · Runs · Jobs · Failures**

Secondary nav (diagnostics): **Storage · Configuration · Health · Logs**

Advanced / legacy bar: Mission Control, Funnels, Publishing,
Legacy failed jobs, Recovery, Legacy settings

| Path | Role |
| --- | --- |
| `/` | Redirects to `/ops` |
| `/ops` | **Operator Console** (daily landing page) |
| `/ops/runs` | Run list (filters: status, trigger, funnel) |
| `/ops/runs/<run_id>` | Run detail — what happened during this run |
| `/ops/jobs` | Job list (filters: state, funnel, run_id, …) |
| `/ops/jobs/<job_id>` | **Job Inspector** (observability `JobDetail`) |
| `/ops/outputs` | **Clip review** — run-centric; latest successful run by default |
| `/ops/outputs?run_id=<run_id>` | Clips from a selected successful run |
| `/ops/outputs/<job_id>/<clip_id>` | Clip detail and preview |
| `/ops/failures` | Aggregated failure triage |
| `/ops/failures/<group_key>` | Failure group detail |
| `/ops/storage` | Storage diagnostic (read-only) |
| `/ops/configuration` | Configuration viewer (read-only) |
| `/dashboard` | Legacy **Mission Control** |
| `/clip-review` | **Deprecated** — redirects to `/ops/outputs` |
| `/failed` | Legacy failed jobs (+ recovery actions) |
| `/settings` | Legacy settings (editable) |
| `/recovery` | Recovery / reliability tools |
| `/health` | Diagnostic (HTML in browser) or JSON API |
| `/logs` | Service journal search |

The application shell (`ops_ui/shell.py`) loads health/status once per request
into `g.shell_context` and shares it with all pages.

### Runs and Jobs

List pages consume `GET /runs` and `GET /jobs` via `ops_ui/lists.py`. Rows link
to detail views. Run detail shows related jobs, failure links, and output
shortcuts. Job Inspector renders one `JobDetail` from
`scripts/observability/job_inspector.py` — missing artifacts show **Not
available** / **missing**, never a crash.

Reusable partials: `_ops_loop_nav.html`, `_console_*.html`, `_jobs_table.html`,
`_filter_bar.html`, `_legacy_page_notice.html`, `_diagnostic_page_notice.html`.

### Job Inspector (`/ops/jobs/<job_id>`)

Centrepiece drill-down page. Aggregates job summary, pipeline timeline, artifacts,
report summaries, failures/warnings, and output summary. Links to Runs, Outputs,
and Failures as appropriate.

### Configuration Viewer (`/ops/configuration`)

Read-only operator view via ConfigManager (`scripts/observability/config_view.py`,
`GET /config/current`). Shows environment, validation, upload/scheduler state,
and a redacted configuration snapshot. **No editing** on this page — use Legacy
settings for changes.

## Known follow-ups (non-blocking)

* Remove or archive unused legacy partials (`_controls_panel.html`,
  `_health_card.html`, `_resource_panel.html`, `_attention_panel.html`)
* Optional manual staging walkthrough with real (possibly degraded) services
* Possible dev-mode attention tuning when uploads are intentionally disabled
* Output list enrichment (`get_job_detail` per job) may need revisiting if output
  volume grows significantly

## Legacy and advanced tools

These pages remain reachable but are **not** the daily operator path. The UI
shows an **Advanced / legacy** notice on key legacy pages with a link back to
the Operator Console or the modern equivalent.

Deprecated legacy Clip Review (`/clip-review`) GET routes redirect to Outputs.
The `clip_review.py` module, `clip_reviews` SQLite table, and legacy POST routes
remain for backwards compatibility only — not part of the MK1 daily workflow.

### Mission Control (`/dashboard`)

Legacy dashboard (pre–Operator Console). Includes:
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

- **Legacy failed jobs** (`/failed`): failure reason, stage, timestamps, retry counts; per-row upload retry; video re-queue via `input_id`; bulk upload retry; dead-letter section (`failed_terminal`, `missed_upload_window`). For daily triage use `/ops/failures` instead.
- **Recovery** page: global pause/sync status, queue DB path, persisted queued jobs after restart, stuck-job heuristics, recent recovery actions.

Shared controls file (default `ops-ui/data/controls.json`, override with `MK04_CONTROLS_FILE`):

```json
{
  "ingestion_paused": false,
  "uploads_paused": false,
  "human_approval_required": false,
  "publish_approved_only": false,
  "ai_config": {
    "clip_selection_backend": "ai_service",
    "ai_model": "qwen2.5:14b-instruct"
  },
  "updated_at": "..."
}
```

`source-input` rejects `POST /run-funnel` when `ingestion_paused` is true. `output-funnel` skips `POST /queue/upload-due` and the background upload worker when `uploads_paused` is true.

`human_approval_required` and `publish_approved_only` are legacy Clip Review
metadata flags (stored in this file for historical compatibility). They are **not**
enforced by `output-funnel` and are not part of the daily MK1 operator workflow.
Use `/ops/outputs` for post-run clip review instead.

### Settings (`/settings`) — Legacy settings

Editable service settings (not the read-only Configuration viewer):

- **Transcription**: WhisperX model selection (persisted in video-automation pipeline config).
- **Local AI & clip selection**: view/edit the clip-selection backend (`ai_service` default, `openai` legacy) and the local-model config (`ai_service_url`, timeouts, `ai_model`, `ai_base_url`, temperature, top-p, max tokens). Saved values are written into the `ai_config` block of `controls.json`; `video-automation` and `ai-service` read that same file (resolution: per-run option → UI value → env var → default). A status panel shows ai-service/Ollama reachability, the configured model, and model availability from `GET /health`; a **Test model** button runs `GET /diagnostics/model` on demand. Secrets are never displayed. There is no silent fallback to OpenAI when `ai_service` is selected.

Optional stuck thresholds: `OPS_UI_STUCK_RUNNING_SEC`, `OPS_UI_STUCK_QUEUED_SEC`, `OPS_UI_STUCK_UPLOADING_SEC`. Funnel run timeout: `OPS_UI_FUNNEL_RUN_TIMEOUT_SEC` (default 900).

### Funnel management (`/funnels`, `/funnels/<funnel_id>`, `/funnels/new`, `/funnels/<id>/clone`, `/funnels/<id>/edit`)

- **List** (`/funnels`): canonical funnels from the registry with derived readiness; compact operational overlay.
- **Detail** (`/funnels/<funnel_id>`): read-only canonical config sections, readiness summary, mappings, and operational state (separate from readiness). Run/Pause/Resume reuse existing controls; **Clone** and **Edit** open registry workflows.
- **Create** (`/funnels/new`): build a draft funnel from a built-in template and save to the registry only (CSRF-protected when auth is enabled). Defaults: `draft`, disabled, posting off.
- **Clone** (`/funnels/<funnel_id>/clone`): copy an existing registry funnel into a new draft ID (CSRF-protected when auth enabled). Clone is always disabled with posting off; copied routes are intent only until sync.
- **Edit** (`/funnels/<funnel_id>/edit`): update MK1-safe canonical fields (identity, acquisition, processing, distribution intent, mappings). Registry-only until sync is applied.
- **Sync** (`/funnels/<funnel_id>/sync`): preview and apply runtime config synchronisation. `GET` is dry-run only (uses `FunnelSynchronizer.build_plan()`). `POST` requires confirmation and CSRF when auth is enabled. Production sync requires typing the funnel ID to confirm and creates backups by default. Writes only source-input `funnels.json`, video per-funnel JSON, and output channel `accepted_funnel_ids`. AI aliases and ConfigManager YAML are validate-only. Smoke testing is separate (Prompt 17). Create, clone, and edit do not auto-sync.
- Empty registry shows import reminder plus Create Funnel link; no auto-import.

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

## Observability smoke

End-to-end operator workflow check (auth, Console, health JSON, config, controls,
logout). Non-destructive:

```bash
ops-ui/.venv/bin/python scripts/smoke/smoke_observability.py --env dev
ops-ui/.venv/bin/pytest tests/smoke/test_observability_smoke.py -q
```

## Funnel Management smoke

MK1 end-to-end config management workflow (template → registry → clone → edit →
validate → sync) using **temporary fixture config only** by default:

```bash
cd ops-ui && .venv/bin/pytest tests/smoke/test_funnel_management_smoke.py -q
ops-ui/.venv/bin/python scripts/smoke/smoke_funnel_management.py
```

See [docs/operations/FUNNEL_MANAGEMENT_SMOKE.md](../docs/operations/FUNNEL_MANAGEMENT_SMOKE.md).

Unit and workflow tests (broader, no live services required):

```bash
cd ops-ui && .venv/bin/pytest tests/ -q --ignore=tests/smoke
```

## Boundaries

The UI does not import pipeline business logic. Pause flags are mirrored to a JSON file that services read; callers that bypass `run-funnel` / `upload-due` (direct n8n hooks, manual API) are not blocked.

