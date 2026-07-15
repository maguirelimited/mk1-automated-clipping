# Storage Inventory

Storage & Data Management **Phase 1**.

This document is the **source of truth for what the system stores today**.

It does **not** implement retention, deletion, cleanup, lifecycle enforcement,
or automatic scanning.

> Inventory first. Do not implement deletion before artifact locations and
> owners are understood.

Paths below are **environment-relative** from the repository root (or deploy
root), using `<env>` = `dev` | `prod` as configured in
`config/environments/<env>.yaml` and `EnvironmentStatePaths`.

---

## Environment roots (authoritative layout)

| Root | Path pattern | Defined by |
| --- | --- | --- |
| Data | `data/<env>/` | `paths.data_root` |
| Jobs | `jobs/<env>/` | `paths.jobs_root` |
| Outputs | `outputs/<env>/` | `paths.outputs_root` |
| Logs | `logs/<env>/` | `paths.logs_root` |
| Reports | `reports/<env>/` | `paths.reports_root` |
| Database | `database/<env>.db` | `paths.database_path` |
| Clips (derived) | `outputs/<env>/clips/` | `EnvironmentStatePaths.clips_root` |
| Transcripts (derived) | `data/<env>/transcripts/` | `EnvironmentStatePaths.transcripts_root` |
| Cache (derived) | `data/<env>/cache/` | `EnvironmentStatePaths.caches_root` |
| Runs | `runs/<env>/` | Reliability run records (not in `paths:` YAML; used by `run_records.py` / backup) |
| Backups | `backups/<env>/` | `backup_control.py` |

Dev and prod roots must never mix.

---

## Classification legend

| Class | Meaning |
| --- | --- |
| **Business output** | Deliverable content or customer-facing product (e.g. final clips). Not a cleanup target by default. |
| **Operational evidence** | Needed to debug, audit, reproduce, or operate (reports, run records, logs, config snapshots). |
| **Temporary** | Safe to remove once the owning job/step no longer needs it. |

Initial retention recommendations below mirror `config/system/system.yaml` →
`storage.retention` (with optional `config/environments/{dev,prod}.yaml`
overrides) **as configuration intent only**. They are **not enforced** by any
retention service yet.

---

## Inventory

### 1. Source videos

| Field | Value |
| --- | --- |
| **Artifact type** | `source_video` |
| **Current location(s)** | Job-local copies under the active pipeline `jobs_folder` (via `create_job_paths` + `PIPELINE_CONFIG_PATH`). Deployed: `$MK04_RUNTIME_ROOT/video-automation/jobs`. Upstream downloads may also live under source-input service data (ledger `file_path`; deploy: `INPUT_SERVICE_DATA_DIR` / `INPUT_JOB_LEDGER_DIR`). |
| **Owning subsystem** | `source-input` (download/ledger); `video-automation` (job input copy) |
| **Purpose** | Input media for transcription and clipping |
| **Operational evidence** | Yes (debugging failed jobs) |
| **Business output** | No |
| **Temporary** | Partially — job copy is intermediate; ledger file may be longer-lived |
| **Initial retention recommendation** | `source_videos_days: 7` (config intent) |
| **Future deletion eligibility notes** | Only after job completion or safe failure handling; never while job is running |

---

### 2. Transcripts

| Field | Value |
| --- | --- |
| **Artifact type** | `transcript` |
| **Current location(s)** | `jobs/<env>/<job_id>/transcript.json`, `transcript_payload.json`; optional derived root `data/<env>/transcripts/` (path defined; usage may be sparse) |
| **Owning subsystem** | `video-automation` (transcription) |
| **Purpose** | Speech-to-text for discovery and captions |
| **Operational evidence** | Yes |
| **Business output** | No |
| **Temporary** | No |
| **Initial retention recommendation** | `transcripts_days: 30` |
| **Future deletion eligibility notes** | Keep longer for failed jobs (`failed_job_artifacts_days`) |

---

### 3. Raw candidate pools

| Field | Value |
| --- | --- |
| **Artifact type** | `raw_candidate_pool` |
| **Current location(s)** | `jobs/<env>/<job_id>/raw_candidate_pool.json` |
| **Owning subsystem** | `video-automation` / `ai-service` (section candidate discovery) |
| **Purpose** | Processing handoff of scored candidates |
| **Operational evidence** | Yes |
| **Business output** | No |
| **Temporary** | No |
| **Initial retention recommendation** | `raw_candidate_pools_days: 30` |
| **Future deletion eligibility notes** | Useful for selection debugging |

---

### 4. Processing reports

| Field | Value |
| --- | --- |
| **Artifact type** | `processing_report` |
| **Current location(s)** | `jobs/<env>/<job_id>/processing_report.json` |
| **Owning subsystem** | `video-automation` (processing pipeline) |
| **Purpose** | Diagnostics for discovery/scoring |
| **Operational evidence** | Yes |
| **Business output** | No |
| **Temporary** | No |
| **Initial retention recommendation** | `processing_reports_days: 90` |
| **Future deletion eligibility notes** | Small, high-value; prefer long retention |

Also related (same job dir, not always present):

* `transcript_sections.json`
* `section_candidate_discovery.json`
* `report.json` (job lifecycle report — operational evidence)
* `task.json`, `review.md`, `analytics.json`, `execution_context.json`

---

### 5. Selection results

| Field | Value |
| --- | --- |
| **Artifact type** | `selection_result` |
| **Current location(s)** | Preferred: `jobs/<env>/<job_id>/post_processing/selection/selection_result.json`. Legacy/alternate: `jobs/<env>/<job_id>/selection.json` |
| **Owning subsystem** | `video-automation` (selection gate / post-processing) |
| **Purpose** | Records which candidates were selected/rejected/reserve |
| **Operational evidence** | Yes |
| **Business output** | No |
| **Temporary** | No |
| **Initial retention recommendation** | `selection_results_days: 90` (config intent) |
| **Future deletion eligibility notes** | Two path conventions exist; inventory both |

---

### 6. Intermediate renders

| Field | Value |
| --- | --- |
| **Artifact type** | `intermediate_render` |
| **Current location(s)** | Under job post-processing tree when present: `jobs/<env>/<job_id>/post_processing/tmp/`, intermediate files under `post_processing/clips/` before finalization. No single global intermediate root. |
| **Owning subsystem** | `video-automation` (post-processing conveyor / render modules) |
| **Purpose** | Module pipeline intermediates (render/format/caption stages) |
| **Operational evidence** | Limited |
| **Business output** | No |
| **Temporary** | Yes |
| **Initial retention recommendation** | `intermediate_renders_days: 14` |
| **Future deletion eligibility notes** | Prefer delete when final clip exists and job completed; path patterns need classification rules in a later phase |

Plan types `formatted_clip` / `captioned_clip` are **stages within post-processing**, not separate top-level roots today.

---

### 7. Final clips

| Field | Value |
| --- | --- |
| **Artifact type** | `final_clip` (observability: `output_clip`) |
| **Current location(s)** | Primary: `jobs/<env>/<job_id>/clips/*.{mp4,mov,webm,mkv,m4v}`. Also: `jobs/<env>/<job_id>/post_processing/clips/`. Derived global root: `outputs/<env>/clips/` (defined; may be unused or partially used). |
| **Owning subsystem** | `video-automation` (render); `output-funnel` (registration/upload) |
| **Purpose** | Finished short-form video outputs |
| **Operational evidence** | Secondary |
| **Business output** | **Yes — business outputs.** |
| **Temporary** | No |
| **Initial retention recommendation** | Until upload confirmed + backup policy allows. `auto_delete_final_clips_prod: false` in system config. |
| **Future deletion eligibility notes** | **Not a cleanup target by default.** Never delete sole copy if not uploaded / not backed up / still needed for dispute/retry |

---

### 8. Post-processing reports

| Field | Value |
| --- | --- |
| **Artifact type** | `post_processing_report` |
| **Current location(s)** | `jobs/<env>/<job_id>/post_processing/reports/post_processing_report.json` (primary); optional job-root `post_processing_report.json` |
| **Owning subsystem** | `video-automation` (post-processing) |
| **Purpose** | Module results, clip pass/fail, selection counts |
| **Operational evidence** | Yes |
| **Business output** | No |
| **Temporary** | No |
| **Initial retention recommendation** | `post_processing_reports_days: 90` |
| **Future deletion eligibility notes** | High-value for Job Inspector / Failures page |

Related: `output_funnel_handoff.json` under the same reports directory when present.

---

### 9. Per-clip metadata

| Field | Value |
| --- | --- |
| **Artifact type** | `clip_metadata` |
| **Current location(s)** | `jobs/<env>/<job_id>/post_processing/metadata/<clip_id>_metadata_writer_v1.json` |
| **Owning subsystem** | `video-automation` (`metadata_writer_v1`) |
| **Purpose** | Per-clip editorial/validation/module metadata |
| **Operational evidence** | Yes |
| **Business output** | No (supports business outputs) |
| **Temporary** | No |
| **Initial retention recommendation** | `clip_metadata_days: 180` |
| **Future deletion eligibility notes** | Small; keep longer than media intermediates |

---

### 10. Run records

| Field | Value |
| --- | --- |
| **Artifact type** | `run_record` |
| **Current location(s)** | `runs/<env>/<run_id>/run_record.json` |
| **Owning subsystem** | Reliability orchestration (`scripts/ops/run_records.py`, `run_pipeline.py`) |
| **Purpose** | Canonical pipeline run history for observability |
| **Operational evidence** | Yes |
| **Business output** | No |
| **Temporary** | No |
| **Initial retention recommendation** | `run_records_days: 90` |
| **Future deletion eligibility notes** | Small; keep for operator history |

Also in the same run directory: `run.log`, optional `resolved_config.yaml` (config snapshot for that run).

---

### 11. Job logs

| Field | Value |
| --- | --- |
| **Artifact type** | `job_log` |
| **Current location(s)** | `jobs/<env>/<job_id>/job.log` or `pipeline.log` (when present); run-level `runs/<env>/<run_id>/run.log` |
| **Owning subsystem** | `video-automation` / pipeline entrypoint |
| **Purpose** | Per-job or per-run operational logs |
| **Operational evidence** | Yes |
| **Business output** | No |
| **Temporary** | No |
| **Initial retention recommendation** | `logs_days: 30` (shared logs policy intent) |
| **Future deletion eligibility notes** | Keep longer for failed jobs |

---

### 12. Service logs

| Field | Value |
| --- | --- |
| **Artifact type** | `service_log` |
| **Current location(s)** | Primary: **systemd journal** (`journalctl -u mk04-*.service`). Secondary file roots: `logs/<env>/` (and optional subdirs such as `source-input`, `video-automation`, `ai-service` when written). Deploy may also use `/var/log/mk04/<env>/`. |
| **Owning subsystem** | Each long-running service (`source-input`, `video-automation`, `ai-service`, `output-funnel`, `ops-ui`); ops `logs_report.py` |
| **Purpose** | Service runtime diagnostics |
| **Operational evidence** | Yes |
| **Business output** | No |
| **Temporary** | No (rotated by journald / future retention) |
| **Initial retention recommendation** | `logs_days: 30` |
| **Future deletion eligibility notes** | Journal retention is host/journald policy; file logs under `logs/<env>/` are in-repo |

---

### 13. Configuration snapshots

| Field | Value |
| --- | --- |
| **Artifact type** | `config_snapshot` |
| **Current location(s)** | Per job: `jobs/<env>/<job_id>/resolved_config.yaml`. Per run: `runs/<env>/<run_id>/resolved_config.yaml` when config validation succeeded. Live config tree: `config/` (version-controlled, not a snapshot). |
| **Owning subsystem** | ConfigManager / execution context / run_pipeline |
| **Purpose** | Reproducibility of what config a job/run used |
| **Operational evidence** | Yes |
| **Business output** | No |
| **Temporary** | No |
| **Initial retention recommendation** | `config_snapshots_days: 180` |
| **Future deletion eligibility notes** | Small; important for audit |

---

### 14. Database

| Field | Value |
| --- | --- |
| **Artifact type** | `database` |
| **Current location(s)** | Pipeline/config path: `database/<env>.db`. **Also:** output-funnel SQLite (service-local, often under output-funnel data dir / deploy runtime). **Also:** Ops UI control DB `ops-ui/data/ops_ui.sqlite3` (or `OPS_UI_DB`). |
| **Owning subsystem** | Config paths / output-funnel / ops-ui |
| **Purpose** | Upload queue, control state, clip reviews, action audit |
| **Operational evidence** | Yes |
| **Business output** | No |
| **Temporary** | No |
| **Initial retention recommendation** | Not a timed delete target; backup/rotate policy later |
| **Future deletion eligibility notes** | Multiple DB files exist; inventory must not assume a single database |

---

### 15. Database backups

| Field | Value |
| --- | --- |
| **Artifact type** | `database_backup` |
| **Current location(s)** | Operational backups: `backups/<env>/backup_<env>_<timestamp>.tar.gz` (+ `.manifest.json`). Includes small operational files (json/yaml/log/db within size limits); **excludes** media clips and secrets. |
| **Owning subsystem** | `scripts/ops/backup_control.py` / `backup.sh` |
| **Purpose** | Recover control/config/run metadata, not full media archive |
| **Operational evidence** | Yes |
| **Business output** | No |
| **Temporary** | No |
| **Initial retention recommendation** | Policy-based rotation (not yet implemented as retention service) |
| **Future deletion eligibility notes** | Rotate older archives; never treat as sole copy of final clips (clips excluded from these backups) |

---

### 16. Temporary files

| Field | Value |
| --- | --- |
| **Artifact type** | `temporary_file` |
| **Current location(s)** | `jobs/<env>/<job_id>/post_processing/tmp/`; `data/<env>/cache/`; health write probes under data cache; OS temp used by tools (outside inventory roots) |
| **Owning subsystem** | Various (post-processing, health checks, tools) |
| **Purpose** | Short-lived work files |
| **Operational evidence** | No |
| **Business output** | No |
| **Temporary** | Yes |
| **Initial retention recommendation** | Short; delete aggressively once safe |
| **Future deletion eligibility notes** | Prefer explicit tmp/cache markers only |

---

### 17. Additional artifacts present today (documented for completeness)

| Artifact type | Location(s) | Owner | Class | Notes |
| --- | --- | --- | --- | --- |
| `job_report` | `jobs/<env>/<job_id>/report.json` | video-automation | Operational evidence | Job lifecycle status |
| `execution_context` | `jobs/<env>/<job_id>/execution_context.json` | video-automation | Operational evidence | Env/funnel/platform/preset provenance |
| `control_state` | `data/<env>/control_state.json` | ops (upload/scheduler kill switches) | Operational evidence | Runtime controls |
| `last_update_status` | `data/<env>/last_update_status.json` | update scripts | Operational evidence | Last `./update.sh` result |
| `pipeline_execution_lock` | `data/<env>/pipeline_execution.lock` | run_pipeline | Operational evidence | Overlap prevention |
| `input_ledger_record` | source-input ledger dir (`…/state/input_jobs/`) | source-input | Operational evidence | Ingestion ledger |
| `ops_ui_controls` | `ops-ui/data/controls.json` (or `MK04_CONTROLS_FILE`) | ops-ui | Operational evidence | UI control flags / AI overrides |

---

## Planned / not yet implemented

These appear in the Storage & Data Management plan or path tables but are **not** fully realized as dedicated stores:

| Artifact type | Status |
| --- | --- |
| Dedicated global `outputs/<env>/clips/` population | Path defined; primary finals live under **job** `clips/` today |
| Dedicated `data/<env>/transcripts/` archive | Path defined; primary transcripts live under **job** dirs |
| Separate top-level `formatted_clip` / `captioned_clip` roots | Stages inside post-processing, not separate inventory roots |
| Enforced retention / deletion service | Config thresholds exist; **no enforcement code** in this phase |
| Full media backup of final clips | Operational backup **excludes** clips by design |

Do not invent paths for these.

---

## Inconsistencies (document only — do not fix here)

1. **Dual clip locations:** finals under `jobs/<env>/<job_id>/clips/` vs derived `outputs/<env>/clips/`. Observability indexes job-local clips.
2. **Dual selection paths:** `selection.json` vs `post_processing/selection/selection_result.json`.
3. **Reports root vs job-local reports:** `reports/<env>/` is configured, but processing/post-processing reports primarily live under job directories.
4. **Runs root outside `paths:` YAML:** `runs/<env>/` is used by Reliability but not listed in environment YAML `paths:` (backup and run_records know about it separately).
5. **Multiple databases:** `database/<env>.db`, output-funnel SQLite, ops-ui SQLite — retention must not assume one file.
6. **Config retention thresholds are not enforced:** values in `storage.retention` (and related `storage.*` policy fields) are documentation/intent until a later Storage phase implements a retention engine.

---

## Ownership summary

| Subsystem | Primary artifacts |
| --- | --- |
| source-input | Source downloads, input ledger |
| video-automation | Job dir artifacts (transcript → clips, reports, metadata) |
| ai-service | Contributes to candidate discovery (artifacts written via video-automation job dir) |
| output-funnel | Upload DB / registration (not final media ownership) |
| Reliability ops (`scripts/ops`) | Run records, run logs, execution lock, control_state, backups |
| ops-ui | Control DB, controls.json, action audit |
| systemd/journald | Service logs |

---

## Shared type names (for later phases)

Canonical string identifiers for retention work live in:

`scripts/storage/artifact_types.py`

Path classification (Phase 3) lives in:

`scripts/storage/artifact_classifier.py`

These align with this inventory and with observability artifact names where they overlap. Classification produces metadata only — **no retention planning or deletion**.

---

## Confirmation (Phase 1)

* No retention behaviour has been implemented.
* No deletion logic exists in this phase.
* No cleanup behaviour has been introduced.
* This phase is **documentation / inventory only**.
