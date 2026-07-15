# Observability Contract

Operations & Observability **Phase 1**.

Defines the structured data objects that describe operational state. This is the
single source of truth for:

* future Operations UI pages
* backend JSON endpoints
* SSH / operational scripts that need structured output

Phase 1 defined the contract. Phases 2+ implemented JSON endpoints and the
Operations UI (`ops-ui`). The UI consumes this contract only — it does not add
parallel health logic or filesystem scans.

## Architecture

```text
Infrastructure
  (run records, execution locks, health, scheduler, services, reports)
        ↓
Observability Contract   ← this package
        ↓
Backend JSON endpoints   (ops-ui, read-only)
        ↓
Operations UI            (Flask SSR — Operator Console at /ops)
```

SSH status/health commands and the UI must eventually agree because they consume
the same models.

Never:

```text
UI → random filesystem inspection → different answers from SSH
```

## Package location

```text
scripts/observability/
  __init__.py      # public exports
  schemas.py       # allowed values and field catalogues
  models.py        # dataclasses
  contract.py      # adapters and secret-safety helpers
```

Import path (add `scripts/` to `sys.path`, same pattern as `scripts/config`):

```python
from observability import (
    SystemHealth,
    SystemStatus,
    RunSummary,
    run_summary_from_run_record_dict,
    config_summary_from_operational_state,
)
```

## Design rules

| Rule | Meaning |
| --- | --- |
| UI-independent | No Flask, templates, or ops-ui imports |
| Route-independent | No HTTP handlers |
| Layout-independent | Models do not assume path layouts; adapters accept dicts |
| Secret-safe | `ConfigSummary` is allowlisted; secret-like keys are rejected |
| Missing-safe | Missing artifacts use `exists=False`, not exceptions |
| Wrap, do not redesign | Run records and reports stay authoritative; contract wraps them |

## Schemas

### ServiceStatus

One long-running service.

| Field | Type | Notes |
| --- | --- | --- |
| `service_name` | string | Logical name (e.g. `ai_service`, `worker`) |
| `state` | string | `active` \| `inactive` \| `failed` \| `activating` \| `deactivating` \| `unknown` |
| `health` | string | `PASS` \| `WARN` \| `FAIL` \| `UNKNOWN` |
| `last_checked_at` | string \| null | ISO-8601 UTC |
| `restart_count` | int \| null | When available from systemd |
| `last_restart_at` | string \| null | When available |
| `detail` | string \| null | Operator-facing detail |
| `unit_name` | string \| null | Optional systemd unit name |

Maps from: systemd unit status / boot verification / health report service checks.

### SystemHealth

Readiness: **Is the system safe and ready?**

| Field | Type | Notes |
| --- | --- | --- |
| `overall` | string | `PASS` \| `WARN` \| `FAIL` |
| `environment` | string | `dev` \| `prod` |
| `disk` | DiskState | status + usage_percent |
| `upload` | UploadStateSummary | effective posting state |
| `scheduler` | SchedulerStateSummary | effective scheduler state |
| `services` | ServiceStatus[] | per-service summaries |
| `readiness_failures` | string[] | blocking failures |
| `execution_lock` | ExecutionLockSummary \| null | lock present/stale |
| `boot_readiness` | string \| null | `READY` \| `NOT READY` |
| `checked_at` | string \| null | ISO-8601 UTC |

Maps from: `scripts/ops/health_report.py`, `boot_verification.py`,
`execution_lock.py`, upload/scheduler control state.

### SystemStatus

Activity: **What is happening right now?**

| Field | Type | Notes |
| --- | --- | --- |
| `environment` | string | |
| `state` | string | `idle` \| `running` \| `failing` \| `blocked` |
| `active_run` | ActiveRunRef \| null | current lock-held run |
| `queue` | QueueSummary | pending / running / failed |
| `current_activity` | string \| null | short operator label |
| `recent_summary` | RecentActivitySummary | recent counters |
| `checked_at` | string \| null | |

Maps from: `scripts/ops/status_report.py`, execution lock, run records, job queues.

### RunSummary

One pipeline run.

| Field | Type | Notes |
| --- | --- | --- |
| `run_id` | string | |
| `environment` | string | |
| `trigger` | string | `scheduled` \| `manual_cli` \| `operations_ui` \| `remote_ssh` \| `test` |
| `status` | string | `RUNNING` \| `SUCCESS` \| `FAIL` \| `SKIPPED` |
| `started_at` / `finished_at` | string \| null | |
| `duration_seconds` | number \| null | |
| `jobs_started` / `jobs_completed` / `jobs_failed` | int | |
| `funnel_id` | string \| null | |
| `failure_summary` | FailureSummary \| null | from `failure_reason` |
| `log_path` | string \| null | |
| `report_paths` | string[] | |

Maps from: `runs/<env>/<run_id>/run_record.json` via
`run_summary_from_run_record_dict()`. Does not replace run records.

### JobSummary

Job list entry.

| Field | Type | Notes |
| --- | --- | --- |
| `job_id` | string | |
| `state` | string | `queued` \| `running` \| `completed` \| `failed` \| … |
| `environment` / `run_id` | string \| null | |
| `funnel` / `platform` / `preset` | string \| null | |
| `stage` | string \| null | current stage |
| `runtime_seconds` | number \| null | |
| `outputs` | JobOutputs | candidate/clip counts |
| `failure_summary` | FailureSummary \| null | |

Maps from: job metadata / funnel job records / processing reports (later phases).

### JobDetail

Complete inspected job. Contract only — fields may be empty until indexing and
artifact resolution are implemented.

| Field | Type | Notes |
| --- | --- | --- |
| `job_id` | string | |
| `summary` | JobSummary | list-entry fields |
| `stage_timeline` | StageTimelineEntry[] | source → posting |
| `artifacts` | ArtifactReference[] | |
| `reports` | ArtifactReference[] | |
| `logs` | LogReference[] | |
| `warnings` / `failures` | FailureSummary[] | |
| `clips` | ClipSummary[] | |

### ArtifactReference

One artifact. **Missing artifacts are representable.**

| Field | Type | Notes |
| --- | --- | --- |
| `artifact_type` | string | e.g. `processing_report`, `output_clip` |
| `path` | string \| null | |
| `exists` | bool | `False` when missing |
| `environment` / `job_id` / `run_id` | string \| null | |
| `created_at` | string \| null | |
| `size_bytes` | int \| null | |
| `detail` | string \| null | e.g. `not found` |

Use `ArtifactReference.missing(...)` for absent files without raising.

### ClipSummary

One output clip.

| Field | Type | Notes |
| --- | --- | --- |
| `clip_id` | string | |
| `job_id` | string | |
| `source_candidate` | string \| null | |
| `validation_state` | string | `pending` \| `passed` \| `failed` \| … |
| `posting_state` | string | `pending` \| `posted` \| `disabled` \| … |
| `metadata_reference` | ArtifactReference \| null | |
| `output_path` | string \| null | |

Maps from: post-processing reports, clip metadata, output-funnel registry (later).

### FailureSummary

One operational failure or warning.

| Field | Type | Notes |
| --- | --- | --- |
| `component` | string | module or subsystem |
| `stage` | string \| null | pipeline stage |
| `severity` | string | `info` \| `warn` \| `fail` \| `critical` |
| `reason` | string | |
| `timestamp` | string \| null | |
| `suggested_next_inspection_target` | string \| null | path or label |

### ConfigSummary

Read-only operational configuration. **No secrets.**

| Field | Type | Notes |
| --- | --- | --- |
| `environment` | string | |
| `active_preset` | string \| null | |
| `funnel` | string \| null | |
| `platform` | string \| null | |
| `upload` | UploadStateSummary | enabled / runtime control |
| `scheduler` | SchedulerStateSummary | effective / runtime control |

Allowlisted fields only (`CONFIG_SUMMARY_ALLOWED_FIELDS`). Build via
`config_summary_from_operational_state()` and validate with
`assert_config_summary_safe()`. Secret-like field names (`password`, `token`,
`api_key`, …) are rejected. Raw environment variables are never accepted.

### LogReference

Log pointer without embedding large content.

| Field | Type | Notes |
| --- | --- | --- |
| `source` | string | `api` \| `worker` \| `ai_service` \| `job` \| `run` \| … |
| `path` | string \| null | |
| `job_id` / `run_id` | string \| null | |
| `timestamp_start` / `timestamp_end` | string \| null | |
| `detail` | string \| null | |

## Mapping to existing infrastructure

| Contract | Existing source |
| --- | --- |
| `RunSummary` | `scripts/ops/run_records.py` (`RunRecord`) |
| `ExecutionLockSummary` | `scripts/ops/execution_lock.py` (`LockInspection`) |
| `SystemHealth` | `scripts/ops/health_report.py`, `boot_verification.py` |
| `SystemStatus` | `scripts/ops/status_report.py` |
| `UploadStateSummary` | upload control + config effective upload |
| `SchedulerStateSummary` | scheduler control + underlying cron/timer |
| `ServiceStatus` | systemd unit status / HTTP readiness |
| `JobSummary` / `JobDetail` | `jobs/<env>/*/report.json` via `index.py` |
| `ArtifactReference` | `artifacts.py` resolver under `jobs/<env>/<job_id>/` |
| `ClipSummary` | clip metadata / output-funnel (later) |
| `ConfigSummary` | ConfigManager operational fields only |
| `LogReference` | `scripts/ops/logs_report.py` sources (later) |

## JSON endpoints (Phase 2)

Read-only endpoints on the **Operations UI** service (no second API server):

| Method | Path | `data` payload | Notes |
| --- | --- | --- | --- |
| `GET` | `/health` | `SystemHealth` | JSON when `Accept` does not prefer HTML; browsers get HTML diagnostic page (see below) |
| `GET` | `/status` | `SystemStatus` |
| `GET` | `/services` | `{ environment, services: ServiceStatus[], … }` |
| `GET` | `/runs` | `{ environment, runs: RunSummary[], count }` |
| `GET` | `/runs/<run_id>` | `RunSummary` (404 + `error` when missing) |
| `GET` | `/jobs` | `{ environment, jobs: JobSummary[], count }` |
| `GET` | `/jobs/<job_id>` | `JobDetail` (404 + `error` when missing) |
| `GET` | `/jobs/<job_id>/artifacts` | artifact discovery payload (404 when job missing) |
| `GET` | `/logs/api` | bounded service logs |
| `GET` | `/logs/worker` | bounded service logs |
| `GET` | `/logs/ai` | bounded service logs |
| `GET` | `/logs/scheduler` | bounded service logs |
| `GET` | `/logs/errors` | recent error-level lines |
| `GET` | `/jobs/<job_id>/logs` | bounded job log (404 when job missing) |
| `GET` | `/outputs` | `ClipSummary` list (legacy index; optional `?job_id=`). UI uses run-scoped helpers instead. |
| `GET` | `/outputs/<job_id>/<clip_id>` | clip detail payload |
| `GET` | `/outputs/<job_id>/<clip_id>/media` | safe preview stream (index-gated) |
| `GET` | `/failures` | aggregated failure groups |
| `GET` | `/failures/<group_key>` | one failure group detail |
| `GET` | `/config/current` | read-only configuration view (secret-safe) |

Optional query on log endpoints: `?lines=N` (default 200, max 1000).

Run/job indexing (`scripts/observability/index.py`) reads:

* `runs/<env>/<run_id>/run_record.json` via `scripts/ops/run_records.py`
* `jobs/<env>/<job_id>/report.json` (direct children only; same pattern as status)

Missing resources return HTTP 404 with `data: null` and a structured `error` object
in the envelope.

### Artifact resolver (`GET /jobs/<job_id>/artifacts`)

Read-only discovery of known job artifacts (`scripts/observability/artifacts.py`).
Does not embed report contents, stream logs, or generate previews.

`data` payload:

```json
{
  "environment": "dev",
  "job_id": "job_…",
  "artifacts": [ /* ArtifactReference */ ],
  "logs": [ /* LogReference */ ],
  "count": 8,
  "present_count": 2,
  "missing_count": 6,
  "schema_version": 1
}
```

Expected artifact types (always represented):

| Type | Typical relative path(s) |
| --- | --- |
| `transcript` | `jobs/<env>/<job_id>/transcript.json` |
| `raw_candidate_pool` | `…/raw_candidate_pool.json` |
| `processing_report` | `…/processing_report.json` |
| `selection_result` | `…/post_processing/selection/selection_result.json` or `selection.json` |
| `post_processing_report` | `…/post_processing/reports/post_processing_report.json` |
| `clip_metadata` | `…/post_processing/metadata/*_metadata_writer_v1.json` |
| `output_clip` | `…/clips/*` and `…/post_processing/clips/*` |
| `job_log` | `…/job.log` or `pipeline.log` |

**Missing artifacts:** included with `exists: false` and a short `detail`
(`not found`, `no clip metadata files`, `no output clips`). Missing artifacts
are not errors. The endpoint fails only when the job itself cannot be resolved
(HTTP 404).

**Security:**

* job IDs validated (no path traversal)
* lookup limited to `jobs/<env>/<job_id>/`
* paths are environment-relative (`jobs/dev/…`), never absolute
* no secrets, no user-supplied path following
* no file contents embedded

Artifacts are returned in a **stable order**: transcript → raw candidate pool →
processing report → selection result → post-processing report → clip metadata →
output clips → job log.

### Logs backend

Read-only bounded log access (`scripts/observability/logs.py`) reuses
`scripts/ops/logs_report.py` (journalctl + file tails) so SSH `logs.sh` and the
API share the same sources.

`data` payload:

```json
{
  "environment": "dev",
  "source": "api",
  "status": "ok",
  "limit": 200,
  "count": 2,
  "entries": [
    {
      "timestamp": "2026-07-04T12:00:00Z",
      "severity": "error",
      "source": "api",
      "message": "…"
    }
  ],
  "origin": "journalctl unit mk04-source-input.service",
  "schema_version": 1
}
```

| Field | Notes |
| --- | --- |
| `status` | `ok` \| `empty` \| `unavailable` |
| `limit` | applied line cap after clamping |
| `entries` | `LogEntry` list (redacted) |
| `origin` | safe source label (never an absolute path) |
| `log_reference` | present on job logs only |

**Bounded output:** default 200 lines, max 1000. File tails read at most the last
256 KiB (`logs_report.read_tail_lines`), so large files cannot exhaust memory.

**Redaction:** reuses `logs_report.redact_line` for API keys, bearer tokens,
passwords, and `sk-…` tokens.

**Job logs:** resolved through the artifact resolver under
`jobs/<env>/<job_id>/` only. Missing log file → HTTP 200 with `status: empty`.
Missing job → HTTP 404.

### API envelope

All observability endpoints return a versioned envelope. Contract models are
unchanged and live under `data`:

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-04T12:34:56Z",
  "data": { }
}
```

| Field | Meaning |
| --- | --- |
| `schema_version` | **API envelope** format version (`API_ENVELOPE_SCHEMA_VERSION`) |
| `generated_at` | UTC timestamp when the response was built |
| `data` | Contract payload (`SystemHealth`, `SystemStatus`, …) |

`data.schema_version` (when present) is the **observability contract** version
and is independent of the envelope version. Future optional envelope fields
(warnings, degraded notices, pagination, caching) can be added without changing
contract models.

Helper: `scripts/observability/envelope.py` → `observability_envelope(data)`.

Population lives in `scripts/observability/populate.py` and calls existing
`scripts/ops` health/status/lock helpers. Endpoints always return HTTP 200 with
structured `PASS`/`WARN`/`FAIL` (or activity state); they do not crash with 500
when a subsystem is unavailable.

```bash
curl -sS http://127.0.0.1:5170/health | jq .
curl -sS http://127.0.0.1:5170/status | jq .data.state
curl -sS http://127.0.0.1:5170/services | jq .data.services
curl -sS http://127.0.0.1:5170/runs | jq .data.runs
curl -sS http://127.0.0.1:5170/jobs | jq .data.jobs
curl -sS http://127.0.0.1:5170/jobs/<job_id>/artifacts | jq .data.artifacts
curl -sS 'http://127.0.0.1:5170/logs/errors?lines=50' | jq .data.entries
curl -sS http://127.0.0.1:5170/jobs/<job_id>/logs | jq .data.entries
```

Ports follow `MK04_ENV` (`5170` dev, `5070` prod by default).

## Operations UI (implemented)

The Ops UI (`ops-ui/README.md`) is the daily **Operator Console** at `/ops`.
Navigation tiers:

| Tier | Pages |
| --- | --- |
| Daily loop | Console · Outputs · Runs · Jobs · Failures |
| Diagnostics | Storage · Configuration · Health · Logs |
| Legacy / Advanced | Mission Control · Legacy failed jobs · Recovery · Legacy settings · … |

The shell (`ops_ui/shell.py`) consumes the observability backend only:

| Shell element | Backend source |
| --- | --- |
| Environment banner | `build_system_health()` → `environment` |
| Health badge | `overall` |
| Activity | `build_system_status()` → `state` |
| Upload / Scheduler | health payload |

Routes: `/ops` (Operator Console), `/ops/runs`, `/ops/jobs`, `/ops/outputs`,
`/ops/failures`, `/ops/storage`, `/ops/configuration`, plus legacy pages
(`/dashboard`, `/failed`, `/settings`, …).

Partials: `_console_*.html`, `_ops_loop_nav.html`, `_legacy_page_notice.html`,
`_diagnostic_page_notice.html`. Health/status load once per request
(`g.shell_context`).

### Operator Console (`/ops`)

Landing page. Eight sections: overall health, environment/safety, Needs
Attention, current activity, last run, service summary, safe actions, quick
links. Not a metrics dashboard — no business analytics or publishing approval.

Needs Attention: capped, severity-ordered, actionable items from existing
observability fields only; no fabricated missing state.

Safe controls: `POST /ops/actions/<action>` via `scripts/ops` (auth, CSRF, audit,
confirmation for high-risk actions). Run pipeline is Console-only.

### `/health` HTML vs JSON

Single route, Accept-header negotiation (`ops_ui/observability.py`):

* Browser navigation (prefers `text/html`) → HTML doctor/readiness page
* API clients / default test client → versioned JSON envelope (contract unchanged)

### Job Inspector, Outputs, Failures, Configuration

Same as previously documented phases — UI renders observability payloads only.

**Outputs (`/ops/outputs`)** is the canonical daily clip review surface. It is
run-centric: defaults to the latest successful run, supports `?run_id=` for
previous runs, and shows inline clip previews with title/hook, duration, and
score when present. It is review-only — not an approval gate and not tied to
upload/publishing. Legacy `/clip-review` GET routes redirect here.

Configuration is read-only; editable settings live on Legacy settings (`/settings`).

### Authentication and smoke

Auth: `ops_ui/auth/` — password session, CSRF, audit log.

```bash
ops-ui/.venv/bin/pytest tests/ -q --ignore=tests/smoke
ops-ui/.venv/bin/pytest tests/smoke/test_observability_smoke.py -q
```

## Out of scope (future / not MK1 console)

* Business analytics, RPM, views, revenue metrics
* Publishing approval/reject workflow in the Operator Console
* WebSockets / live log streaming
* React or SPA frontend
* Multi-user RBAC / OAuth
* Mutating observability JSON endpoints (controls use `POST /ops/actions/*` instead)

## Validation

```bash
# From repo root, with scripts/ on PYTHONPATH:
PYTHONPATH=scripts python -c "from observability import SystemHealth, RunSummary; print('ok')"

ops-ui/.venv/bin/pytest tests/observability ops-ui/tests/test_observability_endpoints.py
```
