# Safe Apply Mode

Storage & Data Management **Phase 5**.

Apply mode executes a **previously built dry-run plan** with conservative
per-file safety checks. It is the first phase that mutates the filesystem.

---

## Command

```bash
# Plan (required before trusting apply)
video-automation/.venv/bin/python scripts/retention.py --dry-run dev

# Apply — development requires --confirm
video-automation/.venv/bin/python scripts/retention.py --apply dev --confirm

# Apply — production requires --confirm-production (never implicit)
video-automation/.venv/bin/python scripts/retention.py --apply prod --confirm-production

# Apply an existing plan report
video-automation/.venv/bin/python scripts/retention.py --apply dev --confirm \
  --plan-report reports/dev/retention/retention_20260704T120000Z.json
```

`storage.retention.enabled` must be `true` for apply to run.

---

## Architecture

```text
Filesystem
      ↓
Discovery (dry-run planner)
      ↓
ArtifactClassifier
      ↓
RetentionPlanner  →  dry-run plan JSON
      ↓
RetentionApplyExecutor  →  apply report JSON
```

The apply executor is **not** a second planner. It:

1. Consumes `eligible_files` from the dry-run plan
2. Re-validates safety immediately before each deletion
3. Skips and logs anything that fails validation
4. Never re-evaluates retention periods

---

## Confirmation workflow

| Environment | Requirement |
| --- | --- |
| Development | `--confirm` |
| Production | `--confirm-production` |

`retention --apply prod` alone **never** deletes files.

---

## Per-file safety checks

Before `unlink()`:

* Resolve absolute path (no symlink following for deletion target)
* File exists and is a regular file
* Not a symlink (`symlink_detected`)
* Inside configured `allowed_delete_roots` (canonical resolved paths)
* Artifact type matches plan entry (`planner_mismatch` if not)
* Environment matches
* Classifier does not report `unknown`
* Not `database` or config `protected_artifact_types`
* Production `final_clip` blocked unless explicit opt-in flags set
* Job not active (`running` / `queued`) since plan was built (`active_job`)

---

## Skip / failure reasons

| Reason | Meaning |
| --- | --- |
| `outside_allowed_root` | Resolved path outside allowed roots |
| `symlink_detected` | Symlink — never followed for deletion |
| `planner_mismatch` | Type or disposition no longer matches plan |
| `protected_type` | Protected artifact (incl. database) |
| `final_clip_default_protected` | Production final clip default |
| `active_job` | Job became active since plan |
| `unknown_artifact_type` | Classifier cannot identify file |
| `file_not_found` | Missing at apply time |
| `filesystem_error` | Permission or IO failure (`FAILED` outcome) |

---

## Deletion logging

Each attempt produces a `DeletionRecord`:

* `outcome`: `DELETED`, `SKIPPED`, or `FAILED`
* `planner_reason`, `skip_reason`, `error` (when applicable)
* paths, size, timestamp, environment

---

## Apply report

Written to `reports/<env>/retention/<run_id>_apply.json` (does **not** overwrite
dry-run reports). Schema version 1 with shared summary fields — see
[RETENTION_REPORTS.md](./RETENTION_REPORTS.md).

---

## Not implemented (later phases)

* Scheduled apply
* Disk-pressure triggered deletion
* UI controls / background cleanup service
* Retry logic
* Deletion of unknown artifacts
* Deletion outside configured roots

---

## Confirmation (Phase 5)

* Apply only deletes planner-approved eligible files that pass safety checks
* Production requires `--confirm-production`
* Every deletion attempt is logged
* Partial failures do not abort the run
* Unknown and protected artifacts cannot be deleted
