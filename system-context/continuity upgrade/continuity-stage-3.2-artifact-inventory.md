# Stage 3.2 Artifact Inventory (index)

## Purpose and boundary
This dataset is the Stage 3.2 canonical artifact inventory: a human-readable index plus machine-readable manifests.
It records observational evidence only. **Nothing has been classified.** Stage 3.3 has not begun.

## Evidence cutoff
- Cutoff (UTC): `2026-07-16T02:26:38Z` (M1A capture); M1B packaging corrections applied after audit.
- Schema: `mk1.stage32.artifact-inventory.v1`

## Identities
- Repository: `/home/maguireltd/mk1-automated-clipping`
- HEAD: `cd7207d931655d0230fddb306a72f5e61c4327fb`
- Tree: `4e87b63db5b7aa1cb183228a403822a68db14c8f`
- Code baseline: `336946030f5165a910ff95e6a304bd6d5f2e753b`
- Active release: `/opt/mk04/prod/releases/20260714T184302Z_62fdd82_dirty`
- Dependency bundle: `/opt/mk04/prod/dependency-bundles/716deb054e91ccfc44b9`
- Stage 3.1 record: `6b252e42e8957cfb34b1f6a0304279229433045d3beaf20108c39dc76eefce2a`
- Host: `maguireltd-mk1`

## Dataset layout
```
system-context/continuity upgrade/
├── continuity-stage-3.2-artifact-inventory.md
└── stage-3-artifact-inventory/
    ├── inventory-manifest.json
    ├── inventory-schema.json
    ├── roots.jsonl
    ├── coverage.jsonl
    ├── artifacts.jsonl
    ├── relationships.jsonl
    ├── databases.jsonl
    ├── sqlite-schema.jsonl
    ├── jobs.jsonl
    ├── path-references.jsonl
    ├── python-packages.jsonl
    ├── assets.jsonl
    ├── evidence-gaps.jsonl
```

## Summaries
- Roots: 29
- Artifacts: 21879
- Relationships: 14410
- Databases: 7
- SQLite schema objects: 137 (with nested column definitions)
- Jobs: 191
- Path references: 5868 (source + line locator + referenced path)
- Python packages: 452
- Assets: 43
- Coverage records: 153
- Evidence gaps: 189

### Hash coverage
- `NOT_APPLICABLE`: 6539
- `OMITTED_SECRET`: 47
- `VERIFIED`: 15293
- Total artifacts: 21879

### Database / schema
- Seven database families; Output Funnel 16 tables; ops-ui 3 tables; nested columns preserved.
- No database row values are included.

### Credential metadata
Credential-bearing paths: metadata only; `hash_status=OMITTED_SECRET`; no values.

### Dependency / model / asset
Bundle freeze lines total 198 across five live environments. Assets cover model/cache files; physical copies also appear in artifacts with linking relationships.

## Evidence gaps
- `stale_ytdlp_shebang_target`
- `pathref_rebuild_m1b`
- `health_probe_route_correction`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `operator_home_model_cache_inaccessible_to_mk04`
- `permission_mismatch`
- `permission_mismatch`
- `missing_prod_credentials`
- `tmpfs_volatility`
- `missing_exact_dependency_lock`
- `requirements_dev_in_live_bundle`
- `dev_prod_database_divergence`
- `referenced_but_absent_path`
- `pathref_count_discrepancy_i4_vs_m1b`
- `asset_count_reconciliation_i6_vs_m1a`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `job_recorded_running_without_pid`
- `referenced_but_absent_path`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `missing_exact_dependency_lock`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `referenced_but_absent_path`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `referenced_but_absent_path`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `referenced_but_absent_path`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- `broken_symlink`
- … 69 additional gaps in evidence-gaps.jsonl

## Reconstruction / verification
1. Read `inventory-manifest.json` for digests and aggregate SHA-256.
2. Verify each file SHA-256; manifest self-hash is external and excluded from aggregate.
3. Validate JSONL parse, schema enums, and referential integrity.

## Confirmations
- No Stage 3.3 classification has been applied.
- No secret values, database row payloads, or private-key material are included.
