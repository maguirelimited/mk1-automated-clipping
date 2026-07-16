# Stage 3.3-M1B — Classification ledger (corrected arithmetic)

Observed at: 2026-07-16T13:18:00Z
Corrects M1A `/tmp/mk1_33m1a_candidate_20260716T130247Z`.

## Classification totals

```
{
  "ARCHIVE": 16678,
  "SHARED_REPLACEABLE_ASSET": 2240,
  "UNRESOLVED": 2961
}
```

Arithmetic: 21879 = 0 INITIAL_LIVE + 16678 ARCHIVE + 0 TEST + 2240 SHARED_REPLACEABLE_ASSET + 2961 UNRESOLVED

## Job-group arithmetic (corrected)

- Unique job-related unresolved groups: **47** = 19 job-local + 15 handoffs + 13 ledgers
- Policy-trigger incidences: **49** = 19 + 15 + 13 + 2 recorded-running conditions
- Overlapping recorded-running trigger incidences: **2** (conditions on existing job-local groups; not additional unique group IDs)
- Non-job unresolved groups: **382**
- Total unresolved groups: **429** = 382 + 47

## Job/media artifacts (unique IDs)

- Unresolved job/media artifacts (47-group union): **835**
- Archived workspace job/media artifacts (172 groups): **2532**
- Prior M1A unresolved job/media figure 892 included OF `database_external_reference_set` members; those remain UNRESOLVED under non-job groups but are excluded from the job/media unique union.

## Supported-but-unproven (6473)

```
{
  "ARCHIVE": 4148,
  "SHARED_REPLACEABLE_ASSET": 2210,
  "UNRESOLVED": 115
}
```

## Byte policies (2240)

```
{
  "DUPLICATE_OF_VERIFIED_LOGICAL_ASSET": 8,
  "PRESERVE_EXACT_LOCAL_BYTES": 1826,
  "RECONSTRUCT_FROM_ACCEPTED_GIT": 21,
  "REGENERATE_FROM_PINNED_LOCAL_INPUTS": 385
}
```

## Unresolved register

- Artifacts: 2961
- Groups: 429

Classification records future treatment only; ARCHIVE does not imply deletion; nothing was moved or activated.
