# Stage 3.5-M1A Migration Manifest Summary

**Candidate:** `/tmp/mk1_35m1a_candidate_20260716T193823Z`
**UTC:** 2026-07-16T19:39:31Z

## Scope
Record-only migration manifest for 21879 artifacts / 20488 cohorts.
Operator approved all 13 D1B recommendations (record-only).
No physical actions performed.

## Coverage
- Artifacts: 21879 (set hash `c6144b352ea5b3683723fd7e09b6b166c6077e13df340bfaf0d926fb60865fa6`)
- Cohorts: 20488
- Ordering: acyclic layers=3

## Actions
{
  "ACT_ARCHIVE_PRESERVE": 11597,
  "ACT_CREATE_DIRECTORY_ONLY": 4805,
  "ACT_NO_COPY_EXCLUDED": 3085,
  "ACT_PRESERVE_UNRESOLVED_NO_AUTHORITY": 836,
  "ACT_PROTECTED_CREDENTIAL_COPY": 47,
  "ACT_RECORD_ABSENCE": 1326,
  "ACT_RECREATE_SYMLINK": 134,
  "ACT_RETAIN_VALIDATE_SHARED_ASSET": 42,
  "ACT_SQLITE_FAMILY_SNAPSHOT": 5,
  "ACT_TRANSFORMED_COPY": 2
}

## Shared-asset discrimination
{
  "archived_shared_classified": 1792,
  "archived_shared_classified_set_hash": "7ee4f97a3a35dab9da35361ba629018cb7c9020ce36c273ced9bf018ed62907d",
  "mapped_shared_assets": 42,
  "mapped_shared_assets_set_hash": "6607a91920af82e07d21b02d7bdda27e1d59d1b46aaef4de2783e5d9920e643b",
  "nocopy_reproducible_shared": 406,
  "nocopy_reproducible_shared_set_hash": "0671f3a20f1f8a16f70269fd3dbe77d9f288af072b14d40878afa2380898fb35",
  "unresolved_shared_classified": 0,
  "unresolved_shared_classified_set_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
}

## Initial-live
7

## Execution blockers
2

## Flags
migration_executed=false; physical_action_performed=false; stage_3_6_begun=false; stage_4_begun=false


## M1A authority mapping corrections

- `ART-99e7f31067f22cd1` (prod Output Funnel DB main) remapped to `/var/lib/mk04/live/databases/output-funnel/output_funnel.sqlite3` as `INITIAL_LIVE` / `ACT_SQLITE_FAMILY_SNAPSHOT`, remaining `execution_blocked` for GAP-26 conditional inspection with WAL/SHM companions.
- `ART-a0bda5d988a22b4a` remapped to `/var/lib/mk04/live/controls/control_state.json` as canonical fail-closed controls (`uploads_disabled=true`, `scheduler_disabled=true`, `uploads_paused=false`).
- `ART-5fba92e5c8738b83` retained as `ARCHIVE_VALUE_SOURCE` for `uploads_paused`.


## M1B corrections (record-only)

Proven defects corrected from independent M1B review:

1. `ART-3d54d4611b5f00ac` (`seen_urls`): `ACT_TRANSFORMED_COPY`/`XFORM-LIVE-EXPLICIT-REWRITE` → `ACT_VERBATIM_COPY`/`XFORM-NONE` (pure relocation; 20-record identity preserved).
2. `ART-02d1a7ab8c80ef6a` (funnel registry): same correction (no absolute paths in content; unresolved component fields remain unresolved).
3. Ops UI SQLite family transformation rule → `XFORM-NONE` (snapshot without evidenced content rewrite).
4. Controls transform rule expanded with explicit per-field provenance; remains the sole `ACT_TRANSFORMED_COPY`.

M1A candidate preserved byte-for-byte at `/tmp/mk1_35m1a_candidate_20260716T193823Z`.
