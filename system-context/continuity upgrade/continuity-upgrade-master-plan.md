# MK1 Continuity Upgrade — Authoritative Master Plan

**Status:** Authoritative master plan after completion of Stage 2  
**System:** MK1 hardware / MK04 application stack  
**Date established:** 15 July 2026  
**Current stage:** Stage 3 — Preserve State and Prepare Recovery  
**Supersedes:** The document titled MK1 Continuity Upgrade Plan with status “Draft before Stage 2”  
**Evidence base:** Stage 1 Infrastructure Reconstruction Record and Stage 2 Continuity Model

## 1. Purpose

The MK1 Continuity Upgrade replaces the current dev/prod promotion model with a simpler and safer operating model that separates:

- editable candidate code;
- accepted code in Git;
- the immutable installed live application;
- persistent live configuration and state;
- preserved historical material;
- shared machine assets;
- isolated test configuration and state.

The upgrade exists to let MK1 continue running a known working version while candidate code is edited, broken, tested and repaired independently. When candidate code is accepted, one deliberate manual installation replaces the application body without replacing the persistent live brain.

The central invariant is:

> Replacing application code must not replace the identity, configuration, connections, controls or accumulated state of MK1.

Installing a new accepted version must not require funnels to be recreated, channels to be reconnected, secrets to be entered again, databases to be substituted with empty copies, or operational memory to be discarded.

The Stage 2 analogy remains authoritative:

- the replaceable application release is the **body**;
- persistent live configuration and state are the **live brain**;
- isolated test configuration and state are the **test brain**;
- a candidate body is tested against the test brain;
- an accepted body is connected to the latest live brain during controlled installation;
- the live and test brains are never merged.

## 2. Authority of the project documents

Three controlling records must be retained:

1. **Stage 1 Infrastructure Reconstruction Record** — authoritative for verified facts about the system as it existed when audited.
2. **Stage 2 Continuity Model** — authoritative for the target architecture and all decisions settled during Stage 2.
3. **This Authoritative Master Plan** — authoritative for implementation order, stage boundaries, completion gates and the current route to completion.

Later stage records may add newer machine evidence and implementation details. They may not silently reopen a settled Stage 2 architectural decision.

If a conflict appears:

- newer verified machine evidence overrides an older description of current machine state;
- the Stage 2 Continuity Model overrides provisional architecture in the old draft plan;
- this master plan controls the order in which the accepted architecture is implemented;
- no dev or prod path becomes authoritative because of its name;
- no unresolved implementation detail permits a settled isolation or ownership rule to be weakened.

The former MK1 Continuity Upgrade Plan marked “Draft before Stage 2” is obsolete and must not be used as live planning context.

## 3. Current project position

| Stage | Status | Controlling result |
| --- | --- | --- |
| 1. Reconstruct Existing Infrastructure | Complete | Evidence-backed Stage 1 Reconstruction Record |
| 2. Define the Continuity Model | Complete | Final Stage 2 Continuity Model |
| 3. Preserve State and Prepare Recovery | In progress | Stage 3 records will be created sequentially |
| 4. Build the Replacement Infrastructure | Not started | Blocked by the Stage 3 gate |
| 5. Rehearse the Replacement | Not started | Blocked by the Stage 4 gate |
| 6. Cut Over to Continuity | Not started | Blocked by the Stage 5 gate |
| 7. Verify Continuity | Not started | Blocked by the Stage 6 gate |
| 8. Retire the Old Infrastructure | Not started | Blocked by the Stage 7 gate and explicit approval |
| 9. Document the Final Workflow | Not started | Completed after the verified as-built system exists |

Stage 3.1-I1, the first read-only information prompt, may already be running when this plan is installed into the working context. Its output is evidence only and does not constitute an accepted change or completed substage until reviewed against this plan.

## 4. Scope

### 4.1 In scope

Continuity covers the infrastructure used to:

- edit candidate application code;
- run isolated candidate tests;
- preserve and classify existing code and state;
- establish Git as complete code provenance;
- select an accepted full Git commit;
- prepare exact runtime dependencies;
- install an immutable accepted release manually;
- bind installed and workspace copies to fixed roles;
- preserve live funnels, configuration, credentials, databases, controls, memory, jobs and media across code replacement;
- migrate configuration and database layouts deliberately;
- coordinate live and test access to the GPU and shared assets;
- start, stop, inspect and recover the live application;
- prove isolation, boot persistence and exact running identity;
- retire obsolete dev/prod wiring only after verification.

### 4.2 Explicitly out of scope

Continuity does not:

- improve discovery, selection, editing or output quality;
- create or connect new platform accounts;
- create missing credentials;
- enable existing credentials;
- enable uploads;
- enable upload workers;
- install or enable scheduling;
- add automatic daily or multi-funnel execution;
- prove a successful content-processing run or post;
- introduce automatic deployment;
- introduce automatic rollback;
- build general off-machine disaster recovery;
- introduce a convenient destructive live reset;
- delete potentially valuable historical material automatically.

The architecture may support later multi-funnel and scheduled operation, but this upgrade does not activate those capabilities.

## 5. Non-negotiable target architecture

### 5.1 Seven distinct logical areas

| Area | Authority | Lifecycle |
| --- | --- | --- |
| Editable workspace | Incomplete candidate work only | May be dirty, broken and actively edited |
| Accepted Git version | Clean full commit SHA reachable from origin/main and deliberately selected | Permanent code provenance; push alone never deploys |
| Installed live application | Immutable materialisation of the accepted commit and exact dependency identity | Replaceable application body |
| Active live state | Only authority for the mutable live brain | Persists across application replacement |
| Historical archive | Valuable inactive or unresolved material | Preserved without becoming current authority |
| Shared machine assets | Validated, predominantly read-only reusable assets | Independent of a particular application commit |
| Isolated test state | Independent persistent test brain | Resettable without affecting live |

These areas may coexist physically on the MK1 machine, but their access boundaries and lifecycles remain distinct.

### 5.2 Target persistent roots

The settled state roots are:

- **/var/lib/mk04/live/** — active live state bundle;
- **/var/lib/mk04/test/default/** — default isolated persistent test bundle;
- **/var/lib/mk04/archive/** — preserved historical and legacy material;
- **/var/lib/mk04/assets/** — shared validated machine assets.

The live and test bundles use the same logical layout so one role-neutral codebase can consume either contract. They remain physically separate and permission-isolated.

The archive is not a second live state root and is not, by itself, an off-machine disaster-recovery backup.

### 5.3 Live and test state-bundle layout

The live bundle is organised around:

- a minimal state-manifest.json identity and layout guard;
- config/funnels for the canonical operational funnel registry;
- config/system for persistent non-secret machine and operator configuration;
- separate database families for Output Funnel and ops-ui;
- source-input memory and durable ledgers;
- jobs and their required frozen context;
- durable input and output media;
- one canonical persistent control authority;
- protected credentials referenced by stable connection identifiers.

Application source, Git metadata, dependency bundles, shared models, logs, reports, locks, sockets, PIDs and disposable scratch do not belong inside the persistent brain.

Missing or corrupt required live state fails closed. It must not be silently replaced with an empty database, empty ledger, default controls or a newly initialised live bundle. Explicit empty initialization is permitted only for a new test bundle.

### 5.4 Code and installation flow

The only valid code flow is:

> editable workspace → clean pushed commit → deliberately selected immutable installation

The accepted version is a clean full SHA reachable from origin/main. No tag is required.

A Git push never changes the running application. The live application never runs from the editable workspace and never depends on Git or network access after installation.

Each accepted installation creates a new immutable release and records:

- the full accepted Git SHA;
- the exact runtime dependency identity;
- its externally assigned role;
- its installation result.

A root-managed current pointer selects the installed live body. Existing immutable releases are not overwritten.

### 5.5 One codebase and externally bound roles

The application remains one role-neutral codebase.

The binding rule is:

> One physical code copy has exactly one role at a time, while the installed live copy and editable workspace copy may run simultaneously.

Role is selected by a small root-managed launch binding outside the application and state bundle. The binding supplies only launch facts such as:

- execution class: live or test;
- state root;
- shared asset root;
- role-appropriate ports;
- scratch root;
- logging root.

Mutable funnel configuration, controls, operational settings and credentials do not live in the launch binding or in /etc projections.

### 5.6 Strict test isolation

- Live services run as the mk04 identity.
- Test services run as a separate mk04-test identity.
- Filesystem permissions prevent test from reading or writing the live bundle and live credentials.
- Live and test have separate configuration, databases, controls, memory, jobs and writable media.
- Test source media is a separate physical copy.
- The default test bundle persists until deliberately reset.
- A test reset can affect only the selected test bundle.
- Selected non-secret fixtures or configuration may be copied administratively into test.
- There is no automatic whole-brain clone.
- Test state is never merged or promoted into live.
- A stale candidate database is never installed into live.
- Test upload is denied unconditionally at the final action boundary, even if candidate code or configuration is faulty.

### 5.7 Funnel and configuration authority

- Git owns generic engine behaviour, schema support and validation rules.
- The persistent state bundle owns operational funnel instances and machine/operator configuration.
- All funnels coexist in one canonical persistent funnel registry.
- There is no global active-funnel switch.
- Every job names a funnel ID.
- Job creation freezes a complete self-contained resolved funnel revision.
- Running and resumable jobs retain their frozen revision.
- Funnel edits affect future jobs immediately.
- Shared templates or profiles are copied into the frozen revision rather than remaining shared mutable dependencies.
- Per-funnel routing and enablement live with the funnel.
- Global emergency controls remain global.
- Permanent component-specific projections in /etc or the code tree are removed after compatibility migration.
- Any temporary compatibility projection is derived and cannot become a peer authority.

### 5.8 Credentials and connection references

Funnels contain stable source and destination connection references, not tokens, passwords, cookies or OAuth files.

Protected credential material lives separately under the role-appropriate credential boundary:

- source credentials may be shared by funnels using the same source identity;
- destination credentials normally correspond to a particular channel or account;
- internal UI and service secrets remain system-scoped;
- test may contain dummy references but receives no real credential material;
- credentials remain outside Git;
- making a credential readable by a live service does not enable uploading;
- Continuity preserves existing credential material but does not create, reconnect or enable it.

### 5.9 Databases, operational memory, jobs and media

- Output Funnel and ops-ui retain separate component databases.
- Component databases do not travel with application code.
- Missing required live databases fail closed.
- Test databases are independently created and resettable.
- Existing dev and prod histories are not merged merely because both exist.
- Seen URLs, ledgers and deduplication facts are persistent operational memory.
- Role-specific operational memory cannot be reset across the live/test boundary.
- Funnel-specific memory is partitioned by funnel where appropriate.
- Job records retain their funnel ID and frozen resolved revision.
- Job-local media remains with its job when it forms one consistency group.
- Input ledgers remain consistent with referenced input files.
- Output Funnel rows remain consistent with referenced assets.
- Live and test never share writable media.
- Completed live material is archived only deliberately.
- Continuity introduces no automatic retention policy.

### 5.10 Controls and fail-closed safety

The target contains one canonical persistent live control authority plus non-bypassable enforcement at the final action boundary.

- Missing or corrupt live controls fail closed.
- Global emergency pause and deny controls remain global.
- Per-funnel enablement and routing remain funnel-scoped.
- Safety is evaluated at action time.
- A frozen job cannot override a later emergency stop.
- Test upload is always denied at the final upload boundary.
- Database rows and component settings may mirror controls for display or audit but cannot become competing authorities.
- Uploads, workers and scheduling remain disabled throughout this upgrade.

### 5.11 Dependencies, models and GPU

- Runtime dependencies are immutable reusable bundles keyed by an exact dependency identity or lock hash.
- Live uses an exact complete runtime lock.
- Development and test use a separate exact lock where their needs differ.
- Ranged requirements alone are insufficient for an accepted runtime.
- Each release records its dependency-bundle identity.
- Dependency bundles are replaceable infrastructure, not live state.
- Ollama remains a shared loopback service.
- Validated Whisper, Hugging Face and comparable reusable models move under the shared asset boundary or remain behind an accessible shared service.
- Candidate execution cannot mutate assets consumed by live.
- Mutable runtime caches are role-specific.
- Live has priority for heavy GPU work.
- Heavy live and heavy test workloads do not run concurrently.
- Light test/UI activity may remain available while live is operating.

### 5.12 Permanent UI and supervised services

- The permanent operator UI runs from the accepted installed release and uses live state.
- The temporary test UI runs from the workspace on alternate ports and uses test state.
- The two UIs may exist simultaneously and must be visibly marked LIVE and TEST.
- Live services are supervised and boot-persistent.
- Test services are manually started and have no live boot wiring.
- Status surfaces report role, full commit, dependency identity, state root, bundle ID and layout version.

### 5.13 Installation, migrations and recovery

One manually invoked guarded installer owns the installation boundary. It is not CI/CD, a Git hook, a scheduler or an automatic deployment system.

The installer must:

1. accept a full approved SHA;
2. verify it is clean and reachable from origin/main;
3. construct the immutable release and exact dependencies away from the active installation;
4. bind the physical copy externally to the live role;
5. verify release, dependency, state-bundle and layout compatibility;
6. refuse installation while an active live job or writer cannot be safely stopped;
7. create a recovery checkpoint before any declared state change;
8. freeze live writers and prevent new work;
9. migrate only the latest frozen live state with explicit versioned migrations;
10. switch current only after staging and preflight succeed;
11. start and verify the selected release;
12. record the selected and previous releases, dependency identity, migration and result.

Code-only recovery retains the prior immutable release and does not copy the live brain.

State-changing recovery checkpoints only the databases or consistency groups affected by the migration. If that checkpoint cannot be created, migration is refused.

Failure before a state change leaves the old live system untouched. After a state migration, older code must not be restarted blindly against incompatible state. Recovery remains manual, and a failed post-migration activation must leave the system stopped with the exact required restore action reported.

## 6. Implementation discipline

### 6.1 Sequential gates

Stages are completed in order. A later stage may be planned, but it may not mutate the machine until the preceding gate has passed.

The normal substage sequence is:

1. read-only information gathering;
2. evidence review;
3. only genuinely necessary design decisions;
4. bounded implementation;
5. direct verification;
6. a written completion record.

Information prompts must not quietly implement fixes. Implementation prompts must name every permitted write and every prohibited boundary.

### 6.2 Existing live system

The existing live system remains available while preservation and replacement infrastructure are prepared alongside it.

It is not refactored in place. Old wiring is not removed first. The workspace is never allowed to become the live code root.

The first Continuity conversion is special because existing code and state are scattered. Later installations reuse the established live brain and do not rebuild it from candidate or rehearsal snapshots.

### 6.3 Preservation and deletion

- Potentially valuable material is preserved until classified.
- A generated-data label does not authorise deletion.
- Archive status does not authorise deletion.
- Unknown and conflicting copies are preserved separately.
- No silent merge is permitted.
- No deletion occurs during Stages 1 through 7.
- Retirement in Stage 8 may detach obsolete wiring, but deletion of preserved material still requires explicit approval.

### 6.4 Secret handling

- Evidence records never contain secret values.
- Credential inventories record presence, reference, owner, permissions and accessibility only.
- Environment files are never dumped wholesale into chat or reports.
- Secrets remain outside Git and outside candidate/test access.

### 6.5 Fail-closed posture

Any uncertainty involving live state identity, upload permission, credential access, database compatibility, active writers or migration recovery blocks the mutating action. It does not trigger an empty initialization or permissive fallback.

## 7. Stage roadmap and completion gates

## Stage 1 — Reconstruct Existing Infrastructure

**Status:** Complete.

Stage 1 mapped the repository, installed releases, systemd services, commands, ports, identities, permissions, configuration loaders, dev/prod branching, databases, state roots, jobs, media, controls, credentials, models, dependencies, backups and boot behaviour.

Its controlling output is the Stage 1 Infrastructure Reconstruction Record.

**Completion gate:** Passed. The system has an evidence-backed map showing what runs, where it runs, what it reads and writes, and the active dependencies on the old dev/prod model.

## Stage 2 — Define the Continuity Model

**Status:** Complete.

Stage 2 settled the seven-area architecture, physical roots, authority rules, strict isolation contract, accepted-code rule, dependency model, funnel/configuration model, control boundary, live/test UI model, GPU coordination, manual installer and recovery model.

Its controlling output is the Stage 2 Continuity Model.

**Completion gate:** Passed. Every important artifact category has an authority, lifecycle, consumer path, access boundary, replacement rule, failure posture and preservation rule. Remaining questions are evidence or implementation details rather than unresolved architecture.

## Stage 3 — Preserve State and Prepare Recovery

**Status:** In progress.

Stage 3 makes later implementation safe. It records the current working system, reconciles code provenance, inventories and classifies all relevant state, resolves competing copies, creates the migration manifest, creates the preservation set and proves recovery without changing which system is live.

Stage 3 does not build the replacement runtime, activate new services, move the live application, enable credentials, upload, schedule work or delete legacy material.

### Stage 3.1 — Record the Current Working Baseline

**Information work**

- Capture current live runtime, services, ports, health and safety state.
- Capture workspace, Git and installed-release identities.
- Refresh the valuable-path and semantic-difference audit.
- Capture unit files, dependencies, launch bindings and reconstruction commands.

**Conditional design question**

- Resolve a genuine semantic code conflict only if refreshed evidence cannot establish the correct preservation result.

**Implementation work**

- Remove the embedded credential from the Git remote without exposing it.
- Refresh the remote safely.
- Recover the two known live-only historical documents.
- Classify untracked code paths.
- Create a clean code-only preservation commit containing valuable application material but no secrets, generated state, caches or machine-specific data.
- Publish the preservation commit through the accepted Git path.
- Verify that a clean checkout reproduces the complete application source baseline.
- Create the current-working-baseline and reconstruction record.

**Success criteria:** The running release, valuable dirty delta, clean preservation commit, services, dependencies, configuration bindings and reconstruction commands are recorded and reproducible.

### Stage 3.2 — Inventory Existing Code and State

**Information work**

- Inventory all existing MK04 state locations.
- Inventory every funnel and configuration representation.
- Inventory component databases, SQLite companion files, controls and operational memory.
- Inventory jobs, media, outputs, reports, analytics and absolute path references.
- Inventory secrets and credentials by metadata only.
- Inventory dependencies, models and shared assets.
- Record size, type, owner, permissions, current consumer and relevant hashes.

**Design questions:** None. This substage gathers evidence.

**Implementation work**

- Produce the canonical versioned Stage 3 artifact inventory.
- Produce hash, ownership and permission evidence without recording secret values.

**Success criteria:** Every relevant artifact across dev, prod, the workspace, installed releases and machine-level locations is accounted for.

### Stage 3.3 — Classify Every Artifact

**Information work**

- Identify current readers and writers.
- Identify database-and-file consistency groups.
- Evaluate whether legacy jobs contain enough frozen context to resume or reproduce.
- Distinguish persistent assets from disposable caches.
- Identify artifacts whose purpose cannot be proved.

**Design questions**

- Which jobs and media remain active or resumable?
- Which material is historical archive only?
- Which reusable assets must be preserved as bytes and which have a verified reproducible acquisition method?
- Which artifacts must remain unresolved?

**Implementation work**

- Apply one explicit classification to every inventory item: initial live, archive, test, shared/replaceable asset or unresolved.
- Create the unresolved-artifact register.
- Do not move or delete classified items.

**Success criteria:** Every artifact has an explicit future treatment or is preserved on the unresolved register.

### Stage 3.4 — Resolve Conflicting Copies

**Information work**

- Compare conflicting funnel and configuration copies.
- Compare dev and prod component database histories.
- Compare operational memory and input ledgers.
- Compare current controls and database mirrors.
- Compare credential references and file presence without reading secret contents.
- Compare duplicate jobs, media and workspace histories.

**Design questions**

- Which funnel and configuration values initialise the live brain?
- Which database becomes active for each component?
- Which alternative histories remain separately archived?
- Which existing values initialise the canonical control authority?
- Which control rows are mirrors only?
- Which credential artifacts are prepared for the protected live store and which remain archived?
- Which unresolved conflicts remain preserved without authority?

**Implementation work**

- Create the conflict-resolution ledger.
- Record the selected initial-live authority for each category.
- Record the archive or unresolved treatment of every alternative copy.
- Do not silently merge competing histories.

**Success criteria:** Each conflicting category has one selected authority or an explicit preserved unresolved state; no decision relies on the dev/prod label alone.

### Stage 3.5 — Create the Migration Manifest

**Information work**

- Map classified source artifacts to target Continuity locations.
- Audit absolute paths and referential dependencies.
- Define required transformations.
- Define ownership, permissions and consumers.
- Define migration order and consistency groups.
- Define preconditions, validation and recovery checkpoints.

**Conditional design questions**

- Should a legacy record with incompatible or incomplete context be deliberately rewritten or preserved as archive-only?
- Does any artifact still lack a safe target or migration action?

**Implementation work**

- Create a machine-readable migration manifest.
- Create its schema and validator.
- Run a non-mutating coverage check.
- Prove every inventoried artifact has an explicit treatment.

**Success criteria:** Every preserved artifact has a source, target or archive treatment, action, ownership, permissions, consistency group, validation and recovery rule.

### Stage 3.6 — Create the Preservation Set

**Information work**

- Confirm storage capacity and the protected preservation destination.
- Determine consistent SQLite and companion-file snapshot methods.
- Determine writer-coordination requirements.
- Define protected credential-copy handling.
- Confirm required model preservation or reproducible acquisition.

**Conditional design question**

- Approve a brief writer pause only if a consistent online snapshot cannot be guaranteed.

**Implementation work**

- Create a protected versioned preservation location.
- Create consistent database and state snapshots.
- Preserve every manifest-selected code and state artifact.
- Preserve credentials without displaying or enabling them.
- Preserve selected jobs, media, history and shared assets.
- Preserve unit files, dependency definitions and installation metadata.
- Seal the preservation set with checksums and permission evidence.

**Success criteria:** All irreplaceable code and state are preserved in a complete, protected and integrity-verifiable set.

### Stage 3.7 — Verify Recovery

**Information work**

- Define the isolated restore location.
- Define the recovery verification matrix.
- Define database, reference, permission and reconstruction checks.

**Design questions:** None. The recovery standard is fixed by this plan.

**Implementation work**

- Restore the preservation set into an isolated non-runtime location.
- Verify checksums.
- Verify SQLite integrity and database-to-file consistency.
- Verify protected ownership and permissions without exposing secrets.
- Verify the clean code checkout and reconstruction records.
- Create the recovery-test evidence report and restoration instructions.

**Success criteria:** The preservation set can be restored separately and its code, state, databases, references and protected material remain consistent and readable.

### Stage 3.8 — Complete the Stage 3 Gate

**Information work**

- Trace every original artifact through inventory, classification, conflict resolution, manifest, preservation and restoration.
- Review unresolved artifacts.
- Reconfirm the current live system remains unchanged.
- Reconfirm uploads, upload workers and scheduling remain disabled.

**Approval questions**

- Does any unresolved artifact still block safe implementation?
- Is the Stage 3 evidence accepted as sufficient to begin Stage 4?

**Implementation work**

- Correct any preservation or coverage gap by returning to the responsible substage.
- Freeze the accepted inventory, ledgers, manifest, checksums and recovery report.
- Create the Stage 3 Completion Record.

**Stage 3 gate:** Every irreplaceable artifact is accounted for, classified or safely unresolved, preserved and demonstrably recoverable before replacement infrastructure touches live state.

## Stage 4 — Build the Replacement Infrastructure

**Status:** Not started.

Stage 4 implements the accepted architecture alongside the old live system.

Implementation order:

1. Introduce one explicit role-neutral runtime contract for execution class, code root, state root, asset root, ports, scratch and logs.
2. Implement the live/test state-bundle layout and minimal manifest validation.
3. Make missing required live state fail closed while retaining explicit test initialization.
4. Move mutable funnel and system configuration to the persistent canonical contract.
5. Implement self-contained frozen funnel revisions for jobs.
6. Migrate components away from permanent /etc and code-tree projections, retaining only temporary derived compatibility where necessary.
7. Implement role-isolated databases, operational memory, jobs and media contracts.
8. Implement the one canonical control authority and final-boundary test upload denial.
9. Create the mk04-test identity, permissions and default persistent test bundle.
10. Separate live and test writable media and mutable caches.
11. Introduce exact runtime and development/test dependency locks and immutable bundles.
12. Make shared models accessible through the asset boundary without candidate mutation.
13. Implement live-priority mutually exclusive heavy GPU ownership.
14. Add preflight checks for accepted SHA, dependencies, state identity, layout compatibility, funnels, databases, controls, credentials by presence only, assets and active writers.
15. Implement explicit versioned configuration and database migrations with affected-group checkpoints.
16. Implement the one manually invoked root-managed installer and installation history.
17. Implement the permanent live services and UI plus manually launched alternate-port test services and UI.
18. Report role, commit, dependency identity, state root, bundle ID and layout version.
19. Add automated tests for wrong-root refusal, test isolation, upload denial, missing-state failure, migration safety and push-without-deployment.

The old system remains live throughout construction. New services do not own live ports or boot wiring during this stage.

**Stage 4 gate:** The new infrastructure exists alongside the old system, passes automated tests, does not modify live state, cannot access live credentials from test and does not own active live ports.

## Stage 5 — Rehearse the Replacement

**Status:** Not started.

Rehearsal must exercise the complete procedure without changing the current live brain.

1. Select a clean candidate SHA as a rehearsal input.
2. Materialise it exactly as the installer would.
3. Prepare its exact dependency bundle.
4. Create a dedicated rehearsal/test bundle through an administrative operation.
5. Use only deliberately selected, sanitised non-secret fixtures and copied consistency groups required to test migrations.
6. Never give the test identity access to live state or live credentials.
7. Run preflight, layout checks and versioned migrations against the rehearsal bundle.
8. Exercise test service start, health, UI access, shutdown and restart on alternate ports.
9. Verify unconditional upload denial.
10. Exercise installation failure before state change.
11. Exercise state-changing failure and manual recovery using rehearsal checkpoints.
12. Verify the rehearsal brain is never promoted or merged into live.
13. Record the exact procedure, results and remaining blockers.

Rehearsal snapshots are evidence and test fixtures only. The future live installation reconnects to the latest frozen live brain and never installs a stale rehearsal or candidate brain.

**Stage 5 gate:** The complete preparation, preflight, migration, activation and manual recovery procedure succeeds against isolated test material without modifying live state or allowing an upload.

## Stage 6 — Cut Over to Continuity

**Status:** Not started.

Cutover is a deliberately initiated one-time conversion:

1. Select and record the clean full SHA reachable from origin/main.
2. Confirm the Stage 5 rehearsal passed for that implementation.
3. Confirm uploads, upload workers and scheduling remain disabled.
4. Confirm no live pipeline job or uncontrolled writer is active.
5. Prepare the immutable release and exact dependencies away from the active installation.
6. Run all non-mutating preflight checks before downtime.
7. Establish final recovery checkpoints for every state consistency group that will change.
8. Stop the old services and freeze writers.
9. Construct the initial live bundle category by category from the latest authorities selected in Stage 3.
10. Apply only the declared path rewrites and versioned migrations.
11. Establish final live/test role bindings, identities and permissions.
12. Install and enable the simplified live service units.
13. Switch the root-managed current pointer only after staging and migration checks pass.
14. Start the accepted release against the latest live brain.
15. Verify role, commit, dependency, state root, bundle ID and layout identities.
16. Record the cutover result and leave the old infrastructure preserved but inactive.

Failure before state mutation leaves the old system available for manual restart. Failure after migration requires the declared state restore before older code is used. There is no automatic rollback.

**Stage 6 gate:** MK1 runs from the selected immutable commit and exact dependencies while using the preserved live identity and latest migrated live state.

## Stage 7 — Verify Continuity

**Status:** Not started.

Verification is infrastructure-only. It must prove:

- the displayed full commit matches the installed release;
- the dependency identity matches the accepted release;
- live reports LIVE and the correct state root and bundle identity;
- test reports TEST and uses only the test root;
- live services and permanent live UI start, stop and restart correctly;
- test services and test UI use alternate ports and have no boot wiring;
- test cannot read live state or credentials;
- test cannot upload under any application configuration;
- required databases open without empty reinitialization;
- pre/post state identities and important record counts remain consistent;
- funnels and frozen job revisions survived;
- memory, ledgers and controls survived;
- credential references and protected credential files survived without being enabled;
- database-to-media and job consistency groups remain intact;
- changing or breaking the workspace cannot alter live;
- pushing Git cannot deploy;
- heavy test work cannot contend with heavy live work;
- reboot returns to the same selected release and live bundle;
- no active service, command or boot path relies on old dev/prod wiring.

No successful content run, platform connection or real post is required.

**Stage 7 gate:** Every Continuity invariant has direct recorded evidence and MK1 passes the reboot and isolation tests.

## Stage 8 — Retire the Old Infrastructure

**Status:** Not started.

Retirement begins only after the Stage 7 gate and explicit operator approval.

It may:

- disable and detach obsolete dev/prod service units;
- remove old workspace-controlled live orchestration;
- remove obsolete promote commands and environment-specific launchers;
- detach obsolete release-selection wiring superseded by the guarded installer;
- remove permanent component projections that would compete with canonical state;
- remove duplicate obsolete UIs and boot paths;
- update operational references to the final Continuity commands.

It may not automatically delete:

- original dev or prod state;
- archived jobs or media;
- conflicting databases;
- credentials;
- old release snapshots;
- Stage 3 preservation material;
- any unresolved artifact.

No fixed time delay substitutes for evidence. Retirement requires the Stage 7 record, no active dependency on old wiring and explicit approval. Deletion is a separate later action requiring classification, preservation evidence and explicit approval.

**Stage 8 gate:** No active service, command, UI, installation or boot path depends on the obsolete dev/prod infrastructure, and no unique code or state has been lost.

## Stage 9 — Document the Final Workflow

**Status:** Not started.

Create the final as-built records and concise operator runbook covering:

- editing candidate code in the workspace;
- starting and stopping the isolated test stack;
- identifying the TEST UI and test bundle;
- resetting or seeding test state safely;
- accepting and pushing a clean commit;
- manually installing an accepted full SHA;
- checking the running role, commit, dependencies and state bundle;
- starting, stopping, restarting and inspecting live services;
- accessing the permanent LIVE UI;
- checking controls and disabled upload/scheduling posture;
- preserving and restoring live state;
- handling code-only recovery;
- handling state-changing recovery;
- inspecting installation history;
- managing shared assets and GPU coordination;
- reboot verification;
- the boundaries of later credential, upload and scheduling work.

The runbook must use the final commands and paths proven by Stage 7. It must not depend on chat history or obsolete dev/prod terminology.

**Stage 9 gate:** A competent operator can perform the normal edit-test-accept-install workflow and the manual recovery workflow using the written documentation alone.

## 8. Required stage records

The project must produce and retain:

### Existing

- Stage 1 Infrastructure Reconstruction Record;
- Stage 2 Continuity Model;
- this Authoritative Master Plan.

### Stage 3

- current-working-baseline record;
- clean code-preservation commit identity;
- canonical artifact inventory;
- classification ledger;
- unresolved-artifact register;
- conflict-resolution ledger;
- initial-live authority record;
- machine-readable migration manifest and schema;
- preservation-set manifest and checksums;
- restoration instructions;
- recovery-test evidence report;
- Stage 3 Completion Record.

### Stage 4

- runtime and state contracts;
- dependency locks and bundle identities;
- migration definitions;
- installer/preflight contract;
- automated isolation and safety test results;
- Stage 4 Completion Record.

### Stages 5–9

- rehearsal record;
- cutover record;
- Continuity verification record;
- retirement record;
- final as-built map;
- final operator runbook.

## 9. Remaining evidence and implementation details

The following are assigned work, not open architecture:

| Detail | Resolution stage |
| --- | --- |
| Classify every dev/prod/workspace artifact | Stage 3 |
| Validate and rewrite absolute paths where necessary | Evidence in Stage 3; implementation in Stage 4/6 |
| Classify older jobs lacking frozen execution context | Stage 3 |
| Select legacy values for the canonical control authority | Stage 3; migration implementation in Stage 4 |
| Inventory credential files and references without exposing values | Stage 3 |
| Select exact installer filename and infrastructure location | Stage 4 |
| Select immutable release-root name, final unit names and test ports | Stage 4 |
| Define exact self-contained funnel-revision serialization | Stage 4 |
| Inventory and make shared model assets accessible | Stage 3 evidence; Stage 4 implementation |
| General off-machine backup location and policy | Explicitly deferred outside Continuity |

None of these details permits:

- dev or prod labels to decide authority;
- test to read live;
- stale snapshots to become live;
- missing live state to initialize empty;
- credentials to enter Git or test;
- automatic deployment or rollback;
- uploads, workers or scheduling to be enabled.

## 10. Locked-decision checklist

Every implementation and later plan must preserve all of the following:

- [x] Seven distinct logical areas.
- [x] Strict live/test isolation.
- [x] Test has no direct live read access and no real credentials.
- [x] Accepted code is a full clean SHA reachable from origin/main.
- [x] No tag is required.
- [x] Git push never deploys.
- [x] Git owns code provenance, not infrastructure state.
- [x] Immutable release directories use a manually selected current pointer.
- [x] One role-neutral codebase.
- [x] Each physical copy has one externally bound role at a time.
- [x] Installed live and editable workspace test copies may run simultaneously.
- [x] Live and test use separate mk04 and mk04-test identities.
- [x] Live, test, archive and shared-asset roots remain separate.
- [x] The default test bundle persists but is independently resettable.
- [x] Live and test brains never merge.
- [x] Candidate databases and stale snapshots are never installed into live.
- [x] One canonical persistent funnel registry.
- [x] There is no global active funnel.
- [x] Jobs name a funnel and freeze a self-contained resolved revision.
- [x] Funnel edits affect future jobs while running jobs keep their frozen revision.
- [x] Funnel records reference protected credentials rather than containing secrets.
- [x] Separate component databases remain separate.
- [x] Operational memory is persistent, role-isolated and funnel-partitioned where appropriate.
- [x] Missing required live state fails closed.
- [x] One canonical persistent control authority.
- [x] Test upload is denied at the final boundary.
- [x] Live and test writable media remain physically separate.
- [x] Completed live history is archived only deliberately.
- [x] The live brain root is /var/lib/mk04/live/.
- [x] The test, archive and asset roots remain separate.
- [x] state-manifest.json is a minimal identity/layout guard, not a duplicate catalogue.
- [x] Logs, locks, sockets, PIDs, diagnostics and disposable scratch remain outside the persistent brain.
- [x] Runtime dependencies are exact immutable bundles.
- [x] Runtime and development/test locks are separate where required.
- [x] The permanent LIVE UI runs from the accepted release.
- [x] The temporary TEST UI runs from the workspace on alternate ports.
- [x] Live has priority and heavy live/test GPU work is mutually exclusive.
- [x] One manually invoked guarded installer owns installation.
- [x] There is no automatic deployment or automatic rollback.
- [x] Code-only recovery retains the old immutable release.
- [x] State-changing migration checkpoints affected consistency groups first.
- [x] Off-machine disaster recovery is acknowledged but deferred.
- [x] Verification is infrastructure-only.
- [x] Continuity does not enable posting, scheduling, workers or credentials.
- [x] Potentially valuable legacy code and state remain preserved until classified and explicitly approved for deletion.

## 11. Overall completion definition

The MK1 Continuity Upgrade is complete only when:

1. the workspace can be changed or broken without affecting live;
2. a Git push cannot deploy;
3. an accepted clean full SHA can be installed manually;
4. the installed release and exact dependencies are visible and reproducible;
5. the persistent live brain survives application replacement;
6. test is physically unable to access live state or credentials;
7. test is physically unable to upload;
8. missing required live state fails closed;
9. reboot restores the same accepted live release and live bundle;
10. old dev/prod wiring is no longer active;
11. all potentially valuable old material remains preserved or has been explicitly approved for deletion;
12. the final operator workflow and recovery procedure can be followed without chat history.

## 12. Immediate next action

1. Remove the obsolete pre-Stage-2 draft plan from the active Cursor context.
2. Keep the Stage 1 Reconstruction Record.
3. Keep the final Stage 2 Continuity Model.
4. Add this Authoritative Master Plan.
5. Allow the already-sent Stage 3.1-I1 read-only prompt to finish.
6. Review its evidence against this plan before issuing Stage 3.1-I2 or any implementation prompt.

No machine-changing Stage 3 prompt is authorised merely because the first information prompt has completed.
