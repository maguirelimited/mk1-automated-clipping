# Artifact Classification

Storage & Data Management **Phase 3**.

This document describes the **classification layer**: how the system answers
“What artifact is this?”

It does **not** plan deletion, evaluate retention periods, scan for expired
files, or delete anything. Those belong to later phases (planner, dry-run,
apply).

---

## Module layout

| Module | Role |
| --- | --- |
| `scripts/storage/artifact_types.py` | Canonical type names and static labels only |
| `scripts/storage/artifact_record.py` | Structured `ArtifactRecord` / `DeletionEligibility` |
| `scripts/storage/artifact_classifier.py` | Environment-scoped `ArtifactClassifier` |

Entry points:

```python
from config_manager import ConfigManager
from storage import ArtifactClassifier, classify_artifact

resolved = ConfigManager.load(environment="dev", funnel_id="business", platform_id="youtube")
record = ArtifactClassifier(resolved).classify("/path/to/file")
# or
record = classify_artifact(path, resolved=resolved)
```

Paths, environments, and `storage.protected_artifact_types` come from
**ConfigManager** (Phase 2 policy). Roots are never hardcoded.

---

## Supported artifact types

Classification may emit any type in `ARTIFACT_TYPES`, including:

| Type | Typical location (inventory) |
| --- | --- |
| `source_video` | `jobs/<env>/<job_id>/input_*` media |
| `transcript` | `jobs/.../transcript.json`, `transcript_payload.json`; exact names under `data/<env>/transcripts/` |
| `raw_candidate_pool` | `jobs/.../raw_candidate_pool.json` |
| `processing_report` | `processing_report.json`, related discovery JSON |
| `selection_result` | `post_processing/selection/selection_result.json` or legacy `selection.json` |
| `intermediate_render` | Media under `post_processing/tmp/` |
| `formatted_clip` | `post_processing/clips/*` with explicit `formatted` in the filename |
| `captioned_clip` | `post_processing/clips/*` with explicit `captioned` in the filename |
| `final_clip` | `jobs/.../clips/*`, other `post_processing/clips/*`, `outputs/<env>/clips/*` |
| `clip_metadata` | `post_processing/metadata/*_metadata_writer_v1.json` |
| `post_processing_report` | Preferred reports path under `post_processing/reports/` |
| `run_record` | `runs/<env>/<run_id>/run_record.json` |
| `job_log` | `job.log` / `pipeline.log`; run-level `run.log` |
| `service_log` | Files under `logs/<env>/` with log-like names |
| `temporary_file` | Non-media under `post_processing/tmp/`; files under `data/<env>/cache/` |
| `database_backup` | `backups/<env>/backup_*.tar.gz` (+ `.manifest.json`); `backups/<env>/database/db_*.sqlite3` (+ `.manifest.json`) |
| `config_snapshot` | `resolved_config.yaml` on job or run |
| `database` | Exact `paths.database_path` only |
| `unknown` | Anything not matched by an explicit rule |

Additional inventory types (`job_report`, `execution_context`, `control_state`,
etc.) are also recognized when paths match known conventions.

---

## Classification flow

```text
path + ResolvedConfig
        │
        ▼
Resolve path; attach size / mtime / age when the file exists
        │
        ▼
Is path inside this environment’s roots
(jobs, data, outputs, logs, reports, database parent,
 runs/<token>, backups/<token>)?
        │
        ├─ no  → unknown + note outside_environment_roots
        │
        ▼
Match by specificity:
  1. Exact database path identity
  2. backups/<env>/ naming
  3. runs/<env>/<run_id>/ known files
  4. jobs/<env>/<job_id>/ known layout + filenames
  5. outputs clips / transcripts archive / cache / data root / logs / reports
        │
        ▼
If job-scoped: load report.json + execution_context.json
(for job state and run_id only — never invent IDs)
        │
        ▼
Attach protection_flags and descriptive deletion_eligibility
        │
        ▼
ArtifactRecord
```

### Design rules

* **Deterministic** — same path + config + clock → same record.
* **Environment-aware** — a development classifier never labels production paths
  as development artifacts (and vice versa).
* **Policy-neutral** — retention day counts are not read for decisions.
* **Prefer structure over heuristics** — directory + known filename first;
  extension alone never invents a type.
* **Unknown stays unknown** — no silent promotion by age or extension.

---

## Unknown artifacts

When no rule matches:

* `artifact_type` is `unknown`
* `protection_flags` includes `unknown`
* `deletion_eligibility` is `{ eligible: false, reason: "unknown" }`
* `notes` may include why (e.g. `job_path_untyped`, `reports_root_untyped`)
* Classification never raises solely because a file is unknown or missing

Missing files can still be classified by path pattern (`exists: false`,
`path_does_not_exist` note).

---

## Protection flags (metadata only)

Flags describe state. They do **not** authorize deletion.

| Flag | Meaning |
| --- | --- |
| `active_job` | Owning job is `running` or `queued` |
| `failed_job` | Owning job is `failed` |
| `final_clip` | Type is `final_clip` |
| `database` | Type is `database` (active DB path) |
| `protected_type` | Type is listed in `storage.protected_artifact_types` |
| `unknown` | Type is `unknown` |

---

## Deletion eligibility (descriptive only)

This phase exposes eligibility **hints** for the planner. It does **not**
evaluate retention periods.

| eligible | reason | When |
| --- | --- | --- |
| `false` | `active_job` | Job is active |
| `false` | `database` | Active database |
| `false` | `final_clip` | Final clip (business output) |
| `false` | `protected_type` | Config-protected type |
| `false` | `unknown` | Unclassified artifact |
| `unknown` | `failed_job` | Failed job — planner chooses longer retention |
| `unknown` | `planner_not_implemented` | Default for classifiable, non-blocked artifacts |

Phase 4 (retention planner) is the first component allowed to turn these
records into delete / keep decisions.

---

## Boundaries

| Layer | Responsibility |
| --- | --- |
| **Phase 1 — Inventory** | Document what exists and where |
| **Phase 2 — Policy config** | Retention periods, disk pressure, protected types (config only) |
| **Phase 3 — Classification** | Identify and describe a path (`ArtifactRecord`) |
| **Phase 4+ — Planner / apply** | Retention decisions, dry-run, deletion, reports |

Classification must not import or call a retention planner. The planner must
consume `ArtifactRecord` instances rather than re-implementing type detection.

---

## Confirmation (Phase 3)

* No retention planning is implemented.
* No deletion logic is implemented.
* No filesystem cleanup or expired-file scanning is implemented.
* No dry-run or apply mode is implemented.
* This phase is **classification metadata only**.
