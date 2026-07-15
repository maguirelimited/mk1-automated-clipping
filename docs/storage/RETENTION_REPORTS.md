# Retention Reports

Storage & Data Management **Phase 6**.

Retention reports are the **authoritative operational record** of retention
activity. Future consumers (Operations UI, health/status, scheduled retention)
should load these reports rather than reconstructing retention state.

This phase does **not** change planner or apply behaviour.

---

## Location

```text
reports/<env>/retention/
```

| File | Meaning |
| --- | --- |
| `retention_<timestamp>.json` | Dry-run plan report |
| `retention_<timestamp>_apply.json` | Apply execution report |
| `latest.json` | Pointer to the most recently written report |

Historical reports are **never overwritten**. Each run writes a new file.

---

## Schema version

Every report includes:

```json
{
  "schema_version": 1
}
```

Future changes should **extend** the schema. Older reports load via
`load_retention_report()`, which fills missing summary fields where practical.

Constants:

* `RETENTION_REPORT_SCHEMA_VERSION = 1`
* `PLANNER_VERSION = "retention_planner.v1"`
* `APPLY_VERSION = "retention_apply.v1"`

---

## Shared fields (dry-run and apply)

| Field | Description |
| --- | --- |
| `schema_version` | Report schema version |
| `retention_run_id` | Unique run identifier |
| `environment` | `development` / `production` |
| `mode` | `dry-run` / `apply` |
| `planner_version` | Planner implementation version |
| `policy_version` | Config policy version string |
| `started_at` / `finished_at` | ISO-8601 timestamps |
| `duration_seconds` | Wall-clock duration |
| `files_considered` | Files examined |
| `files_eligible` | Planner-approved for deletion |
| `files_deleted` | Successfully deleted (apply only; 0 for dry-run) |
| `files_protected` | Protected by policy |
| `files_unknown` | Unknown artifact type |
| `files_skipped` | Apply-time skips (0 for dry-run) |
| `files_failed` | Apply-time failures (0 for dry-run) |
| `bytes_considered` | Total size of considered files |
| `bytes_reclaimable` | Eligible size (plan) |
| `bytes_reclaimed` | Actually deleted size (apply) |
| `protection_summary` | Grouped protection counts |
| `skip_summary` | Grouped skip / protection reasons |
| `error_summary` | Grouped errors |

### Protection summary

```json
{
  "protected_active_jobs": 1,
  "protected_failed_jobs": 5,
  "protected_final_clips": 8,
  "protected_databases": 1,
  "protected_unknown": 3
}
```

### Skip summary

Reason → count, for example:

`active_job`, `failed_job`, `unknown_artifact_type`, `outside_allowed_root`,
`symlink_detected`, `planner_mismatch`, `protected_type`,
`retention_policy_disabled`, `not_expired`

---

## Per-file records

Detailed lists remain:

* Dry-run: `eligible_files`, `protected_files`, `unknown_files`
* Apply: `deletions`

Each entry includes path, artifact type, size, age (when known), planner reason,
outcome, and skip/failure reason when applicable.

---

## Latest report discovery

```python
from storage import load_latest_retention_report, load_retention_report

report = load_latest_retention_report("reports/dev/retention")
report = load_retention_report("reports/dev/retention/retention_….json")
```

`latest.json` points at the newest report filename. It is updated on every write.
Historical files remain on disk.

---

## Intended consumers (later phases)

* Operations UI — display last retention run
* Health / status — surface reclaimable space and failures
* SSH / ops tooling — inspect without re-running retention
* Scheduled retention — attach schedule metadata to the same schema

Do not rebuild retention state from the filesystem when a report exists.

---

## Confirmation (Phase 6)

* Report history is preserved
* Previous reports are not overwritten
* Detailed per-file records remain
* Retention behaviour is unchanged
* Operations UI integration is not implemented
* Scheduled retention is not implemented
