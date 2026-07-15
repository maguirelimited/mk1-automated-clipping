# MK1 Continuity Upgrade — Stage 2 Continuity Model

**Stage:** 2 — Define the Continuity Model  
**Status:** Consolidated architecture for review  
**Evidence base:** Stage 1 Reconstruction Record, the provisional Upgrade Plan, the Stage 2 read-only state audit, and the final decisions made during the Stage 2 conversation  
**System:** MK1 hardware / MK04 application stack  

## 1. Purpose and authority

This document consolidates the final Stage 2 architecture. It defines the logical areas, authorities, lifecycles, access boundaries and replacement rules required by Continuity.

It is not an implementation prompt, deployment script or revised master plan. It does not authorise machine changes. The Upgrade Plan remains provisional until this record has been reviewed and accepted.

The Stage 1 Reconstruction Record remains authoritative for facts about the current machine. This document is authoritative only for the target architecture decisions explicitly settled during Stage 2.

The central invariant is:

> Replacing application code must not replace the identity, configuration, connections, controls or accumulated state of MK1.

The working analogy used during Stage 2 is precise enough to retain:

- the replaceable application release is the **body**;
- persistent live configuration and state are the **live brain**;
- isolated test configuration and state are a **test brain**;
- a new body is tested against the test brain, then connected to the latest live brain during controlled installation;
- the live and test brains are never merged.

## 2. Scope guardrails

Stage 2 defines infrastructure for editing, testing, accepting, installing, running and preserving MK1. It deliberately does not add product capabilities.

The following remain out of scope:

- enabling uploads or proving a successful post;
- creating, connecting or enabling credentials;
- enabling upload workers;
- enabling or installing scheduling;
- adding daily or multi-funnel automation;
- improving content discovery, selection, editing or output quality;
- implementing the architecture;
- writing implementation prompts;
- redesigning general disaster recovery beyond the minimum needed to prevent a state migration from destroying the current live brain.

The architecture may safely support future config-driven multi-funnel operation, but this upgrade does not implement the daily switching or scheduling feature.

## 3. Classification language

This record uses three labels:

- **Verified fact:** established by Stage 1 or the focused read-only state audit.
- **Decision:** the target rule settled during Stage 2.
- **Unresolved:** a later evidence or implementation detail that does not reopen the settled authority and isolation model.

No current directory becomes authoritative merely because it is named `dev` or `prod`.

## 4. Verified facts that constrain the model

The following facts are the principal evidence constraints. Detailed evidence remains in the Stage 1 Reconstruction Record.

1. Systemd already runs an installed application under `/opt/mk04/prod/current`, not directly from the editable workspace.
2. The installed release is a dirty workspace snapshot. Its recorded Git commit does not completely identify its contents.
3. The workspace, the recorded Git commit and the installed release contain different valuable code. Git is not yet a complete code authority.
4. Current `dev`, `prod` and `promote` commands execute orchestration code and Python from the editable workspace, allowing workspace changes to affect live management.
5. The services are process-separated but share mutable paths, controls and cross-component configuration.
6. Most valuable operational history is under `/var/lib/mk04/dev`, while `/var/lib/mk04/prod` is comparatively thin. The labels do not establish authority.
7. Funnel configuration is fragmented between an ops-ui registry, `/etc` projections and AI/ConfigManager files inside the code tree. Proven copies already diverge.
8. Output Funnel and ops-ui use separate SQLite databases. The `MK04_DATABASE_PATH` database is a stub and is not their shared authority.
9. Missing state can silently look like new empty state: seen URLs can start empty, component databases can be created empty, and controls can fall back to empty values.
10. Source-input operational memory currently includes a global seen-URL file and input ledgers. Resetting these during testing could cause real duplicate processing if live and test are not physically separated.
11. Jobs can contain frozen execution context and resolved configuration, but older jobs do not all contain the newer snapshot files.
12. Job records and Output Funnel database rows contain absolute paths to media spread across job directories, input staging and output directories.
13. Current control authority is fragmented across files, environment settings, database rows and worker/upload flags.
14. Existing backups do not cover all important databases, credentials, historical media or the large current state tree.
15. Uploads and scheduling are currently disabled, and no MK1 cron or timer was found.
16. The `mk04` service account can use CUDA and the running services, but cannot access model caches and private files held under the operator home.
17. Ollama is already a shared loopback service. Existing systemd hardening and immutable-release concepts contain reusable safety value.

## 5. Final logical areas and access boundaries

| Logical area | Authority and lifecycle | Permitted access | Prohibited access |
| --- | --- | --- | --- |
| Editable workspace | Authority for incomplete candidate work only. It may be dirty, broken or under active development. | Operator and Cursor edit it. Candidate services may execute from it using the test binding and test identity. | It cannot be a live service code root, contain authoritative live state, access live credentials or deploy itself by being pushed. |
| Accepted Git version | A clean, pushed, full commit SHA reachable from `origin/main`, deliberately selected for installation. Git is version control and provenance—not runtime infrastructure. | The manual installer may materialise the selected commit while constructing a release. | A push cannot install anything. Git does not own runtime configuration, credentials, databases, controls, jobs, media or operational memory. No tag is required. |
| Installed live application | An immutable materialisation of the accepted commit plus an exact runtime dependency identity. It is replaceable code. | Root-managed installation prepares it; live services execute it read-only. | Runtime services cannot modify it. It cannot receive generated configuration, databases, credentials, logs or other mutable state. It cannot depend on the workspace or Git after installation. |
| Active live state | The single authority for the mutable brain currently used by the live application. It survives application replacement. | Fixed live services access only the sections they own or consume. Controlled live interfaces may update authorised configuration and controls. Explicit migrations may modify declared state after a recovery checkpoint. | Workspace/test processes and ordinary installation replacement cannot read or write it. It cannot be replaced by a candidate snapshot or selected by a `prod` label alone. |
| Historical archive | Valuable code, jobs, media, reports, database snapshots and other material not required as current runtime input. Preservation does not imply current authority. | Explicit classification, archival, restoration and approved reporting may access it. | Ordinary live/test services cannot use it as writable working state. Archiving does not authorise deletion. |
| Shared machine assets | Large reusable, predominantly read-only assets whose lifecycle is independent of an application commit. | Live and test may consume validated assets read-only or through a service such as Ollama. | Assets cannot contain credentials, controls, queues or mutable application databases. Candidate execution cannot alter assets used by live execution. |
| Isolated test state | The independent test brain for candidate execution. It persists by default but may be deliberately reset without affecting live. | Workspace services and the temporary test UI may create and change test configuration, databases, memory, jobs and outputs. | It cannot access or become live state, contain real upload credentials, upload, or become authoritative merely through copying or renaming. |

The direction of code movement is:

> editable workspace → clean pushed commit → manually selected immutable installation

There is no reverse state flow hidden inside that path. Installation changes the body, not the brain.

## 6. Physical layout contract

### 6.1 Persistent roots

The target persistent roots are:

```text
/var/lib/mk04/live/          active live state bundle
/var/lib/mk04/test/default/  default isolated test state bundle
/var/lib/mk04/archive/       preserved historical and legacy material
/var/lib/mk04/assets/        shared validated machine assets
```

The live and test roots use the same logical layout so the same role-neutral application code can consume either one. They are physically separate and permission-isolated.

The historical archive is preservation, not a disaster-recovery backup. Ordinary services never treat it as an alternative live state root.

### 6.2 Active live bundle

```text
/var/lib/mk04/live/
├── state-manifest.json
├── config/
│   ├── funnels/
│   └── system/
├── databases/
│   ├── output-funnel/
│   └── ops-ui/
├── memory/
│   └── source-input/
├── jobs/
├── media/
│   ├── inputs/
│   └── outputs/
├── controls/
└── credentials/
```

The categories mean:

- `config/funnels/`: canonical operational funnel records and their self-contained revisions;
- `config/system/`: persistent machine/operator configuration that is not a secret or ephemeral launch binding;
- `databases/`: separate component database families, including the files needed for consistent SQLite handling;
- `memory/`: seen URLs, input ledgers and other durable deduplication or handoff memory;
- `jobs/`: live job records, frozen job configuration and job-local required artifacts;
- `media/inputs/` and `media/outputs/`: durable non-job-local bytes required by live records or resumable processing;
- `controls/`: the canonical persistent operational control authority;
- `credentials/`: protected secret material referenced by configuration but never embedded in funnel records or Git.

Job-local clips and other job consistency artifacts remain under their job. They are not moved merely to make the directory tree look tidier. Any path consolidation must preserve database-and-file consistency.

### 6.3 What is outside a state bundle

The persistent brain excludes:

- application source, virtual environments and dependency bundles;
- Git metadata;
- shared models and assets;
- logs, reports and ordinary run diagnostics;
- process locks, PIDs, sockets and other coordination state;
- large disposable processing scratch;
- backup copies;
- legacy stub databases that are not real component authorities.

Ephemeral coordination belongs beneath a role-specific `/run/mk04/...` boundary. Logs remain outside the bundle under the logging boundary. Large disposable scratch also remains outside the persistent brain and must not be placed in `/run` merely for convenience.

### 6.4 Root-managed binding

A small root-managed binding outside the application and state bundle selects the role and roots for a physical code copy. The live binding is conceptually `/etc/mk04/live.env` and contains only launch information such as:

- execution class (`live` or `test`);
- state root;
- shared asset root;
- role-appropriate ports;
- scratch and log roots.

`/etc` is not a second mutable configuration authority. Application processes cannot write it. Funnel configuration, controls and operational settings do not live there.

The exact syntax and additional strictly necessary launch fields are implementation details; the authority boundary is settled.

## 7. State-manifest contract

`state-manifest.json` is a minimal bundle identity and compatibility guard. It is not a duplicate inventory of the bundle.

It records only the information required to prevent a wrong connection and support layout compatibility:

- a stable opaque `bundle_id`;
- `execution_class` (`live` or `test`);
- `layout_version`;
- initialization time;
- the last-applied layout migration version or identifier.

It does not contain:

- secrets or credential material;
- live controls;
- upload permission;
- application Git SHA;
- funnel contents or funnel hashes;
- a second copy of every path or component database catalogue.

The application release reports its own commit identity. The state bundle reports its own bundle and layout identity.

Missing or corrupt required state in a live bundle is a startup failure, not permission to create an empty replacement. Explicit initialization is allowed for a new test bundle. Live initialization and migration are separate, deliberate administrative operations.

## 8. One codebase, externally bound roles

The application remains one role-neutral codebase. Role is assigned to each physical code copy through the external binding, not by editing code or selecting a dev/prod code branch.

The final rule is:

> One physical code copy has exactly one role at a time, while the installed live copy and editable workspace copy may run simultaneously.

Consequences:

- the installed release is live-bound;
- the editable workspace is test-bound;
- converting an accepted candidate into the installed version is an installation/binding operation, not a coding exercise;
- neither copy offers a normal UI switch that can reconnect it to the other state root;
- role, commit, bundle ID and state root are prominently visible in status and the UI;
- a copy cannot infer its authority from its path name alone.

The permanent operator UI runs from the accepted installed release and uses live state. A temporary test UI may run from the workspace on alternate ports and uses only test state. Both UIs may exist simultaneously and must be visually distinguishable as `LIVE` and `TEST`.

Live services are the supervised, boot-persistent services. Test services are manually invoked and do not acquire live boot wiring.

## 9. Test isolation contract

Strict isolation was selected.

- Live services run as `mk04`.
- Test services run as a separate `mk04-test` identity.
- Filesystem permissions prevent the test identity from reading or writing the live bundle and live credentials.
- Live and test databases, seen URLs, ledgers, jobs, controls, configuration and media are distinct.
- Source media used for testing is a separate physical copy, not direct access to a live working file.
- Shared validated assets may be read by both identities but not mutated by candidate execution.
- The default test bundle persists test history and configuration between sessions until deliberately reset.
- A test reset affects only the selected test bundle.
- Selected non-secret configuration or fixtures may be deliberately copied into test by an administrative operation.
- There is no automatic whole-brain clone and no path by which test state is merged or promoted into live state.
- Test upload is denied at the final action boundary regardless of UI settings, funnel settings or faulty candidate code.

This is stronger than simply giving test a different environment variable. It makes the wrong connection technically inaccessible.

## 10. Funnel and configuration model

### 10.1 Ownership

The release owns generic engine behaviour, validation rules and schema support. The persistent state bundle owns operational funnel instances and machine/operator configuration.

Git therefore versions the code that knows how to interpret a funnel. Git does not own the live funnel records themselves.

Installing code never overwrites existing operational configuration.

### 10.2 Canonical funnels

- All funnels coexist in one canonical persistent funnel registry under the state bundle.
- There is no global `active funnel` that changes the meaning of the whole system.
- Each job explicitly names a funnel ID.
- At job creation, the complete resolved funnel revision is frozen into the job.
- A running or resumable job continues using its frozen revision even if the canonical funnel later changes.
- Funnel edits take effect immediately for future jobs; Stage 2 did not add a separate draft/publish workflow.
- Funnel revisions are self-contained. Shared templates or profiles are copied into a revision rather than remaining shared mutable dependencies.
- Each funnel contains its own enablement/routing configuration, while genuinely global emergency controls remain global.

This makes future multi-funnel daily operation possible without implementing scheduling or automatic switching now.

### 10.3 One configuration authority

The canonical state-bundle record is the permanent authority. The final architecture does not retain permanent component-specific projections under `/etc` or inside the code tree.

Temporary projections are permitted only as migration compatibility while existing components are moved to the canonical contract. They are derived artifacts, never peers of the canonical record, and must not survive as a second permanent authority.

## 11. Credentials and connection references

Actual secret material is separately permissioned under the live credential boundary. Funnel records contain stable references to connections, not tokens, cookies, passwords or OAuth files.

The intended relationship is:

- a funnel identifies the source and destination connections it should use;
- the credential store resolves those identifiers to protected material;
- source credentials may be shared when several funnels use the same source identity;
- destination credentials are normally channel/account specific;
- internal service and UI secrets remain system-scoped rather than pretending to be funnel-specific;
- test funnels may use dummy connection references, but the test identity has no real credential material.

This preserves the user-facing idea that a funnel knows its YouTube destination while preventing the YouTube credential itself from being duplicated into editable funnel JSON.

Continuity does not create, enable or reconnect credentials during this upgrade.

## 12. Operational memory, databases, jobs and media

### 12.1 Operational memory

Seen URLs, input ledgers, deduplication records and comparable accumulated facts are persistent operational memory. They are neither code nor ordinary configuration.

They are partitioned first by execution class and then by funnel identity where the fact is funnel-specific. Test resets cannot clear live memory. A normal UI or test command is technically incapable of clearing live ledgers or databases.

A genuine destructive live reset, if ever required, is a separate explicit administrative operation with a recovery checkpoint. Designing a convenient reset feature is outside this upgrade.

### 12.2 Databases

- Output Funnel and ops-ui retain separate component databases.
- A missing live database is a failure, not a request to auto-create an empty database.
- The test databases are independently created and may be reset.
- Component databases do not travel with application code.
- A candidate/test database is never installed into live.
- Existing current-state databases and histories are not merged merely because they exist under dev/prod labels.
- The one-time Stage 3 migration classifies each current database/history as active live, historical archive, test or unresolved.

### 12.3 Jobs and media

- Job records retain the funnel ID and frozen resolved revision required to reproduce or resume their behaviour.
- Job-local media stays with the job when it is part of the same consistency group.
- Input ledgers and their referenced input files must remain consistent.
- Output Funnel rows and their referenced assets must remain consistent.
- Live and test never point at the same writable media.
- Completed live jobs and media remain active until deliberately archived; Continuity introduces no automatic retention policy.
- Existing workspace and dev-labelled jobs/media default to preserved legacy test/unknown material, not live authority, until explicitly classified.

## 13. Controls and safety boundary

The target has one canonical persistent live control authority plus hard non-bypassable enforcement at the action boundary.

- Missing or corrupt live controls fail closed.
- Global emergency pause/deny controls remain global.
- Per-funnel enablement and routing live with the funnel record.
- Safety is checked at action time; a frozen job snapshot cannot override a later emergency stop.
- Test upload is unconditionally denied at the final upload boundary.
- Database rows or component settings may mirror control state for display or audit, but cannot become competing authorities.

This architecture preserves the current disabled posture. It does not enable uploads, workers or scheduling.

## 14. Dependencies and shared machine assets

Runtime dependencies are prepared as immutable reusable bundles keyed by an exact dependency identity or lock hash.

- Live services use an exact runtime lock covering their complete dependency set.
- Development/test tooling has a separate exact lock where its needs differ.
- Ranged requirements alone are not sufficient to reproduce an accepted runtime.
- A release records the dependency-bundle identity it uses.
- Dependency bundles are outside state and are never mutable runtime authorities.

Ollama remains a shared service. Whisper, Hugging Face and comparable reusable model assets are brought under the shared asset boundary so the service identities can access validated copies without relying on the operator home. Mutable runtime caches are role-specific; they are not silently shared with live.

## 15. GPU coordination

Live has priority for heavy GPU work. Heavy live and heavy test workloads do not run concurrently on the same GPU.

The temporary test UI and light non-GPU checks may remain available while live operates. Any later coordination mechanism enforces exclusive heavy-job ownership without becoming a scheduler or adding automatic workers.

## 16. Accepted-version and installation contract

### 16.1 Accepted version

An installable accepted version is a clean full Git SHA reachable from `origin/main` and explicitly selected by the operator. No tag is required.

The initial accepted Continuity commit cannot be declared merely from the currently recorded SHA because Stage 1 proved that valuable dirty and untracked code exists. That code must first be reconciled and preserved.

A Git push never deploys. Git is required to identify and reconstruct accepted code, but the running application does not depend on a checkout, branch or network connection.

### 16.2 Immutable releases and selection pointer

Each accepted installation creates a new immutable release and records its exact commit and dependency identity. Existing releases remain untouched. A root-managed `current` selection pointer identifies the installed live body.

The editable workspace is never copied directly over the current live installation.

### 16.3 One manually invoked guarded installer

One deliberately invoked installer owns the promotion boundary. It remains a small infrastructure tool—not CI/CD, a Git hook, scheduler, worker system, credential manager or automatic deployment platform.

Its contract is:

1. Accept a full approved SHA and verify it is a clean commit reachable from `origin/main`.
2. Construct the immutable release and exact dependency binding away from the active installation.
3. Bind that physical copy to the live role externally; never import workspace/test state.
4. Verify release completeness, dependency identity, live bundle identity and layout compatibility.
5. Refuse to proceed while a live job or state writer cannot be safely stopped.
6. If state will change, establish the required recovery checkpoint before touching it.
7. Freeze live writers and prevent new live work from beginning.
8. Run only explicitly declared, versioned state migrations against the latest frozen live state.
9. Switch `current` only after staging and preflight succeed.
10. Start the new release with the fixed live binding and verify reported role, commit, bundle and layout identities.
11. Record the selected release, dependency bundle, previous release, migration and result.

That installation record belongs to the root-managed installation history. It is not another mutable application-state authority.

The normal transition is therefore not copying a stale candidate brain into live. The new body reconnects to the latest live brain.

### 16.4 Failure and recovery

- A code-only upgrade retains the prior immutable release and does not require a new state copy.
- A state-changing upgrade checkpoints only the databases or consistency groups the migration will modify.
- If the required checkpoint cannot be created, the migration is refused.
- Routine installation does not copy the entire media collection.
- Failure before state changes leaves the old live system untouched.
- After a state migration, the installer must not blindly restart older code against potentially incompatible state.
- Recovery is manually initiated; no automatic rollback is introduced.
- A failed post-migration activation leaves the system stopped with the exact restore/recovery requirement reported.

Off-machine disaster recovery is an acknowledged unresolved risk outside this Stage 2 model. GitHub can recover accepted code, but not the live brain. This deferral must not be mistaken for evidence that live state is backed up.

## 17. Initial conversion and stale-snapshot rule

The first Continuity conversion is different from later routine installations because current code and state are scattered and incompletely classified.

It must:

- preserve the dirty installed release and valuable workspace delta before declaring Git authority;
- classify existing dev/prod/workspace artifacts by evidence, not names;
- construct the initial live bundle category by category;
- preserve conflicting or unknown copies rather than silently merging or deleting them;
- validate or deliberately rewrite absolute media paths where roots change;
- establish the initial role bindings and permissions;
- keep the old infrastructure available but inactive until Continuity verification passes.

After that one-time conversion, routine upgrades reuse `/var/lib/mk04/live/`. They do not build live state from the snapshot used during development.

Database schema changes always migrate the latest frozen live database. Test database contents never participate. This is the definitive answer to the stale-snapshot problem.

## 18. Infrastructure verification and retirement gate

Verification remains infrastructure-only. It does not enable or require a real upload, scheduler, worker, new credential or successful content-processing run.

Before the old infrastructure is retired, evidence must establish:

- the installed commit and dependency identity match the selected accepted version;
- the running copy reports `LIVE` and the expected live state root and bundle ID;
- test reports `TEST` and cannot access live state or credentials;
- required component databases open without being recreated empty;
- important pre/post record counts and state identities remain consistent;
- funnels, frozen job configuration, memory, controls and credential references survived;
- referenced job/media consistency groups remain intact;
- the permanent live UI and live services start, stop and restart correctly;
- pushing Git or breaking the workspace does not alter live;
- the test UI uses alternate ports and cannot upload;
- reboot returns to the same accepted release and live bundle;
- no active command, service or boot path depends on the old dev/prod wiring.

After verification:

- previous code and original state remain preserved but inactive;
- obsolete services and wiring may be disabled or detached from active operation;
- nothing potentially valuable is deleted automatically;
- each legacy artifact must be classified before deletion;
- deletion requires explicit approval.

## 19. Conflicts with current evidence and the provisional plan

| Current/provisional position | Final Stage 2 resolution |
| --- | --- |
| Current live release is identified by a SHA despite dirty contents. | A release must be a clean materialisation of a full accepted SHA; the current dirty delta is preserved and reconciled first. |
| Workspace commands control live promotion and services. | A root-managed guarded installer and live service boundary replace the workspace dependency. |
| `dev` contains most history while `prod` is running. | Authority is classified artifact by artifact; neither label wins. |
| Draft groups jobs, outputs and history together as active state. | Active/resumable material belongs in live; inactive valuable material belongs in archive; nothing is auto-deleted. |
| Draft calls test state disposable. | The default test bundle persists by default but remains independently resettable and non-authoritative. |
| Current configuration uses canonical registry plus `/etc` and code-tree projections. | One persistent canonical funnel authority; permanent projections are removed after compatibility migration. |
| Current code may create missing databases or memory as empty. | Missing required live state fails startup; only test may initialize empty state normally. |
| Current seen URLs are global and can be reset independently of funnel context. | Operational memory is isolated by role and partitioned by funnel where appropriate. |
| Current controls have several authorities. | One canonical control authority plus non-bypassable action-time enforcement. |
| Credentials are mixed with env and component paths. | Secret material is protected separately; funnels contain stable connection references only. |
| Provisional plan contemplated broad pre-cutover backup/recovery. | Routine Continuity requires only pre-migration recovery of affected consistency groups; off-machine disaster recovery is explicitly deferred. |
| Current `/etc` contains mutable component configuration. | `/etc` contains only root-managed launch binding; applications cannot write it. |
| Current model caches are inaccessible to the service account. | Validated reusable models move under the shared asset boundary or remain behind an accessible shared service. |

## 20. Settled decisions checklist

The following final decisions must appear in every later plan derived from this record:

- [x] Seven logical areas remain distinct: workspace, accepted Git version, installed application, live state, archive, shared assets and test state.
- [x] Strict live/test isolation; test has no direct live read access and no real credentials.
- [x] Full accepted SHA reachable from `origin/main`; no tag requirement; push never deploys.
- [x] Git owns code history/provenance only, not infrastructure state.
- [x] Immutable release directories plus a manually selected `current` pointer.
- [x] One role-neutral codebase; each physical copy has one externally bound role at a time.
- [x] Installed live and workspace test copies may run simultaneously.
- [x] Separate `mk04` and `mk04-test` identities and state roots.
- [x] One persistent default test bundle; reset and selective fixture/config copy remain independent of live.
- [x] Test and live brains never merge; candidate databases or stale snapshots are never installed.
- [x] One canonical persistent funnel registry; no global active funnel.
- [x] Jobs name a funnel and freeze a self-contained resolved revision.
- [x] Funnel edits affect future jobs immediately; running jobs retain their frozen revision.
- [x] Funnel records reference protected credentials rather than contain secret material.
- [x] Separate component databases remain separate.
- [x] Seen URLs, ledgers and dedupe facts are persistent operational memory, isolated by role and partitioned by funnel where appropriate.
- [x] Missing required live state fails closed instead of silently initializing empty state.
- [x] One canonical persistent control authority; test upload is denied at the final boundary.
- [x] Live and test media are separate physical copies; completed live history is archived only deliberately.
- [x] Persistent live brain root is `/var/lib/mk04/live/`; test, archive and asset roots are separate.
- [x] Manifest is a minimal identity/layout guard, not a duplicated state catalogue.
- [x] Logs, locks, sockets, PIDs, diagnostics and disposable scratch are outside the persistent brain.
- [x] Exact immutable runtime dependency bundles; separate runtime and development/test locks.
- [x] Permanent live UI from the accepted release; temporary test UI from the workspace on alternate ports.
- [x] Live has priority and heavy live/test GPU work is mutually exclusive.
- [x] One manually invoked guarded installer; no automatic deployment or rollback.
- [x] Code-only rollback retains the old release; state-changing migrations checkpoint affected consistency groups first.
- [x] Off-machine disaster recovery is acknowledged but deferred from this model.
- [x] Infrastructure-only verification; no posting, scheduling, workers or credential enablement.
- [x] Potentially valuable legacy code/state remains preserved until explicitly classified and approved for deletion.

## 21. Unresolved evidence and implementation details

These items remain open without changing the settled architecture:

1. The Stage 3 migration manifest must classify every existing dev/prod/workspace artifact as initial live state, archive, test, replaceable or unresolved.
2. Absolute paths in jobs and Output Funnel records must be checked before a root move; required path rewriting and referential validation remain evidence tasks.
3. Older jobs lacking execution context need a preservation/resume classification; absence must not be silently repaired with invented configuration.
4. The exact legacy-to-canonical control migration must identify which current values are authoritative and which database rows are mirrors.
5. Credential files and references require a metadata-only inventory and permission plan without exposing secret values.
6. Exact installer filename/location, immutable release-root name, service-unit names and test port numbers are implementation naming details.
7. The exact funnel-revision serialization and validation schema must implement the frozen self-contained revision contract.
8. Shared model assets require an inventory and accessibility plan; candidate execution must not mutate the live-consumed copies.
9. Off-machine backup location, coverage and restore testing remain explicitly deferred disaster-recovery work.

No unresolved item permits dev/prod labels, a stale test snapshot or an empty auto-created database to become live authority.

## 22. Stage 2 completion gate

Stage 2 is complete for review because every important artifact category now has:

- a logical authority;
- a lifecycle independent of path labels;
- a live/test access boundary;
- a relationship to application replacement;
- a failure posture;
- a preservation rule.

The next planning action, after this record is reviewed and accepted, is to recreate the master Continuity Upgrade Plan from this architecture and the Stage 1 evidence. The revised plan must not resurrect superseded Stage 2 options.