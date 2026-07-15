# Retention Dry-Run Planner

Storage & Data Management **Phase 4**.

The retention dry-run planner evaluates configured policy against classified
artifacts and produces an explainable plan. **No files are deleted.**

---

## Command

```bash
video-automation/.venv/bin/python scripts/retention.py --dry-run dev
video-automation/.venv/bin/python scripts/retention.py --dry-run prod
```

`--apply` is **not implemented** (Phase 5).

---

## Architecture

```text
ConfigManager (policy)
        │
        ▼
Artifact discovery (enumerate files under env roots)
        │
        ▼
ArtifactClassifier  →  ArtifactRecord  (identify only)
        │
        ▼
RetentionPlanner    →  RetentionPlanReport  (policy decisions)
        │
        ├─ terminal summary (stdout)
        └─ JSON report (reports/<env>/retention/)
```

Strict separation:

| Layer | Responsibility |
| --- | --- |
| Classifier | What artifact is this? |
| Planner | Would policy delete it? Why? |
| Apply (Phase 5) | Execute eligible deletions |

Policy never moves into the classifier. Deletion never moves into the planner.

---

## Decisions

Each file receives one disposition:

| Disposition | Meaning |
| --- | --- |
| `eligible` | Would be deleted when retention is enabled and apply runs |
| `protected` | Would be kept — explicit `reason` required |
| `unknown` | Unclassified artifact — never deleted |

### Protection reasons (examples)

`active_job`, `failed_job` (via `not_expired` under failed retention),
`final_clip_default_protected`, `not_expired`, `unknown_artifact_type`,
`outside_allowed_root`, `upload_state_unknown`, `backup_state_unknown`,
`retention_policy_disabled`, `protected_type`, `age_unknown`,
`no_retention_policy_for_type`

### Deletion reasons (examples)

`expired_source_video`, `expired_intermediate_render`, `expired_temp_file`,
`expired_log`, `expired_<artifact_type>`

---

## Policy inputs

From merged config (`storage.*`):

* `storage.retention.*` day counts per artifact type
* `storage.retention.enabled` — when false, expired files remain protected
  with reason `retention_policy_disabled` (dry-run still evaluates ages)
* `storage.allowed_delete_roots` — paths outside these roots are protected
* `storage.protected_artifact_types`
* `storage.auto_delete_final_clips_prod` / `allow_final_clip_auto_deletion_opt_in`

Failed jobs use `failed_job_artifacts_days` instead of per-type periods.

---

## JSON report

Written to `reports/<env>/retention/retention_<timestamp>.json` (schema version 1).
See [RETENTION_REPORTS.md](./RETENTION_REPORTS.md) for the full schema, latest
pointer (`latest.json`), and loaders.

---

## Not implemented (later phases)

* Apply mode / filesystem deletion
* Disk pressure reactions
* Scheduled retention
* Confirmation prompts

---

## Confirmation (Phase 4)

* Dry-run **never deletes** files.
* Every decision includes an explicit reason.
* Unknown artifacts remain protected.
* Active and failed jobs follow policy rules.
* Production final clips remain protected by default.
