# Funnel Management Smoke Test

MK1 verification gate for the Funnel Management workflow. Proves that a funnel
can move through template → registry → clone → edit → validate → sync preview →
sync apply without manually editing fragmented runtime config files.

## What it proves

| Area | Checks |
| --- | --- |
| **Templates** | `build_funnel_from_template()` produces draft/disabled/posting-off funnel |
| **Registry** | Save/list/get in temp registry only |
| **Clone** | New ID, draft safety overrides, source unchanged |
| **Edit** | Canonical update, immutable fields preserved |
| **Validation** | Pre-sync dependency gaps; improved readiness after sync (not persisted) |
| **Sync plan** | Dry-run targets source-input, video JSON, output routing; validate-only AI/ConfigManager |
| **Sync apply** | Temp files only; backups; unrelated/template entries preserved |
| **UI routes** | List/detail/edit/sync preview; GET sync no-write; POST confirmation/CSRF |
| **Scope** | No AI alias writes, ConfigManager YAML writes, pipeline globals, credentials/cadence/metadata edits |

## What it does not prove

- Real source downloads, video processing, AI clip selection, or output uploads
- Scheduler/cron/n8n behaviour
- OAuth/token validity
- Production deploy/promotion
- Full operational overlay against live services

## Run (default — fixture mode only)

Uses temporary directories and fixture config files. **Does not touch** real
`source-input/funnels.json`, video funnel configs, output `channels.json`,
`deploy/env/*`, `/etc/mk04/*`, or production config.

```bash
cd ops-ui
.venv/bin/pytest tests/smoke/test_funnel_management_smoke.py -q
```

Wrapper script (same fixture smoke):

```bash
ops-ui/.venv/bin/python scripts/smoke/smoke_funnel_management.py
ops-ui/.venv/bin/python scripts/smoke/smoke_funnel_management.py --keep-temp --verbose
```

Exit codes:

| Exit | Meaning |
| --- | --- |
| `0` | PASS (or WARN in live read-only mode) |
| `1` | FAIL |
| `2` | CLI usage error |

## Optional live read-only mode

Inspects configured paths on the current host and runs sync **preview only**
(`build_plan`). No apply. No writes.

```bash
ops-ui/.venv/bin/python scripts/smoke/smoke_funnel_management.py --live-read-only
```

Use this before manual dev sync to confirm paths resolve. It does not replace
careful review of the sync preview page.

## Before manual dev/prod sync

1. Run fixture smoke (above) — must pass.
2. Optionally run `--live-read-only` on the target host.
3. Open `/funnels/<funnel_id>/sync` in Ops UI; review dry-run plan and resolved paths.
4. For **prod**, type the funnel ID to confirm and ensure backups are expected.
5. Never run prod apply from this smoke test.

## Fixture IDs

Smoke uses dedicated IDs (`smoke_created_funnel_001`, `smoke_clone_funnel_001`, etc.).
It does **not** use `mfm_business_ai_001` as a write target.

## Related tests

Broader funnel-management unit/workflow coverage:

```bash
cd ops-ui
.venv/bin/pytest tests/test_funnel_*.py tests/test_canonical_funnel_schema.py -q
```
