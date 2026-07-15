# MK1 Continuity Upgrade — Stage 1 Infrastructure Reconstruction Record

**Stage:** 1 — Reconstruct the Existing Infrastructure  
**Status:** Complete  
**Evidence date:** 15 July 2026  
**System:** MK1 hardware / MK04 application stack  

## 1. Purpose of this document

This document records everything established during Stage 1 of the MK1 Continuity Upgrade. It combines:

- the initial Continuity concept and scope agreed in the planning conversation;
- the Stage 1A host and service audit;
- the Stage 1B code-preservation and runtime-coupling audit;
- the Stage 1C persistent-state and host-capability audit;
- the manual privileged checks used to close the remaining audit unknowns;
- the interpretations, corrections and planning conclusions accepted after reviewing those audits.

This is a reconstruction and planning record. It is not the final Continuity architecture and it does not authorise implementation.

The draft master plan will be recreated only after Stage 2 has resolved the target architecture. This Stage 1 document remains supporting evidence for that future plan.

## 2. Upgrade identity and core invariant

The upgrade name selected in this planning conversation is:

> **MK1 Continuity Upgrade**

The name describes the central invariant:

> Replacing application code must not replace the identity, configuration, connections, controls or accumulated state of MK1.

Installing a new accepted application version must not require secrets to be re-entered, channels to be reconnected, funnels to be recreated or databases to be reconstructed manually.

The intended conceptual separation remains:

| Concept | Meaning |
| --- | --- |
| Workspace | The Git working repository edited and tested with Cursor; allowed to contain incomplete or broken work |
| Accepted version | A specific pushed Git commit deliberately approved for installation |
| Live application | A clean installed copy of the accepted version, independent of the editable workspace |
| Active live state | Configuration and mutable state required by the running system |
| Historical archive | Valuable accumulated jobs, media, reports and databases that must survive but need not all be active runtime inputs |
| Shared machine assets | Large reusable models and assets that survive application replacement |
| Test state | Disposable or resettable writable state used by workspace tests, isolated from live state and credentials |

The audits showed that some of this separation already exists physically. The upgrade is therefore not starting from zero. Its task is to make the separation complete, understandable and safe.

## 3. Scope agreed during Stage 1

### 3.1 In scope

The Continuity Upgrade covers infrastructure used to:

- edit application code;
- test candidate code without affecting the running application;
- identify and accept a Git version;
- install an accepted version manually;
- start and supervise the live services;
- separate replaceable code from persistent state;
- preserve configuration, credentials, databases, controls and history;
- prepare dependencies and validate machine requirements;
- migrate configuration and database schemas when required;
- recover manually from a failed replacement;
- verify the exact running commit and boot behaviour;
- retire the obsolete dev/prod infrastructure after successful verification.

### 3.2 Explicitly out of scope

The following must not be pulled into the Continuity Upgrade merely because the audit mentioned them:

- improving clip discovery, selection, editing or output quality;
- connecting new channels;
- creating missing OAuth credentials;
- enabling real uploads;
- enabling upload workers;
- installing or enabling scheduling;
- creating a daily cron;
- proving a successful content-processing run;
- proving a successful post;
- migrating every historical artifact into the active runtime by default;
- unrelated product features;
- unrelated security-platform work;
- general operational improvements such as log rotation unless they are strictly required by the target Continuity infrastructure.

Uploads and scheduling must remain disabled throughout this infrastructure project.

## 4. Planning and implementation workflow agreed in this chat

The conversation workflow is now:

1. This chat remains the planning and architecture record.
2. Stage 1 evidence is preserved separately from the future master plan.
3. Stage 2 decisions will be discussed deliberately in this chat.
4. The master Continuity plan will be recreated only after the architecture is complete and checked for scope drift and missing details.
5. A separate chat will be opened for implementation prompts and Cursor output review.
6. Implementation prompts will follow the recreated master plan rather than relying on memory or scattered chat messages.

## 5. Stage 1 method and completion gate

Stage 1 was deliberately divided into three read-only audits and a small set of operator checks.

| Audit | Purpose | Result |
| --- | --- | --- |
| Stage 1A | Map the host, services, ports, installation, configuration roots, state roots, deployment flow and active dev/prod dependencies | Accepted |
| Stage 1B | Reconcile Git, live and workspace code; map component coupling, runtime writes and dependencies | Accepted |
| Stage 1C | Inventory persistent state, funnels, databases, jobs, controls, credentials, backups and service-account capability | Accepted |
| Privileged closure checks | Check root/mk04 crontabs, user timers and direct service-account access | Completed |

Stage 1 is complete because there is now evidence for:

- what code runs;
- where it runs from;
- which services run continuously;
- what each service reads and writes;
- where valuable code and state exist;
- how dev/prod is embedded;
- which state copies agree or conflict;
- what dependencies the application requires;
- what safety controls are effective;
- what the service account can access;
- what scheduling mechanisms exist;
- what current backups do and do not protect.

Stage 1 did not choose the future authority or target paths. Those are Stage 2 decisions.

## 6. Executive findings

The most important findings are:

1. **The running application is already separated from the editable workspace.** Systemd runs an installed release under `/opt/mk04/prod/current`; it does not run service code directly from the workspace.
2. **The installed release is not a clean Git version.** It was promoted from a heavily dirty workspace and contains hundreds of valuable files absent from `origin/main`.
3. **Git is not yet the real source of truth.** The current code authority is temporarily the reconciled workspace plus two live-only documents, not the old Git commit alone.
4. **Operator tooling still depends on the dirty workspace.** The `dev`, `prod` and `promote` commands execute orchestration code and Python from the editable workspace.
5. **Service processes are separated, but their mutable state is not.** Services share filesystem paths, controls and cross-component configuration; ops-ui writes into `/etc` and the code tree.
6. **Most valuable operational history is under dev-labelled paths.** The running prod stack has little data, while `/var/lib/mk04/dev` contains about 39 GiB of jobs, source media, outputs, analytics and database history.
7. **Funnel state is fragmented.** `gta_clips_002` exists in both ops-ui registries and in dev runtime projections, but is absent from key prod runtime files because prod sync failed against read-only `/etc`.
8. **Current backups are inadequate.** They do not cover the main dev state tree, credentials, important live databases or most historical media.
9. **Uploads and scheduling are effectively disabled.** Multiple independent gates block real upload, and no MK1 cron or timer exists.
10. **The service account can run the production stack and use CUDA, but cannot access models or credentials stored in the operator’s home or dev-only private files.**
11. **Disk capacity is not a migration blocker.** Approximately 517 GiB was free during the audit, allowing old and new copies to coexist during preservation and rehearsal.
12. **The existing release staging, dependency bundles and systemd hardening contain useful safety.** The evidence does not support automatically deleting every existing deployment mechanism simply because the dev/prod operator model is being replaced.

## 7. Current host and live runtime

### 7.1 Host-level facts

| Item | Current state |
| --- | --- |
| Host | Ubuntu Linux on MK1 hardware |
| Service account | `mk04`, UID 994, primary group `mk04`, shell `nologin`, home `/var/lib/mk04` |
| Operator account | `maguireltd`, member of `mk04` and `ollama` groups |
| GPU | NVIDIA RTX 4070 SUPER |
| Production torch | `2.8.0+cu128` |
| CUDA as `mk04` | Available: `True` |
| Media tools | `/usr/bin/ffmpeg`, `/usr/bin/ffprobe` |
| Ollama | Enabled and active on loopback port 11434 |
| Free disk during audit | Approximately 517 GiB |

### 7.2 Live application identity

The live application is:

```text
/opt/mk04/prod/current
  → /opt/mk04/prod/releases/20260714T184302Z_62fdd82_dirty
```

Recorded commit:

```text
62fdd82433b8b0b08b4182739cba6c76a3b538a0
```

The release was activated from a dirty workspace on 14 July 2026. The commit record therefore does not fully identify the running file tree.

### 7.3 Running services

Five MK04 services are enabled and active under systemd.

| Service | Port | Bind | Working code | Main role |
| --- | ---: | --- | --- | --- |
| `mk04-source-input` | 5060 | `127.0.0.1` | installed release | Source acquisition and ingestion |
| `mk04-video-automation` | 5050 | `127.0.0.1` | installed release | Pipeline processing |
| `mk04-output-funnel` | 5055 | `127.0.0.1` | installed release | Output registration and upload queue |
| `mk04-ops-ui` | 5070 | `127.0.0.1` | installed release | Operator UI and funnel/configuration management |
| `mk04-ai-service` | 5075 | `127.0.0.1` | installed release | Local AI orchestration |

Ollama listens on loopback port 11434.

The old dev ports 5150, 5155, 5160, 5170 and 5175 were not listening during the audit. There is no separately installed dev systemd stack.

### 7.4 Unit hardening

All five MK04 units use:

- `User=mk04`;
- `ProtectSystem=full`;
- `ProtectHome=true`;
- `PrivateTmp=true`;
- `NoNewPrivileges=true`;
- `Restart=always`.

These protections are useful and should not be weakened merely to permit misplaced writes. The observed `/etc` write failure is evidence that mutable funnel configuration is in the wrong location, not evidence that `ProtectSystem` should be removed.

### 7.5 Boot behaviour

The five units are enabled under `multi-user.target` and restart automatically. The machine currently boots into the installed release selected by `/opt/mk04/prod/current`.

No MK1 timer or cron is installed.

## 8. Current deployment and operator flow

### 8.1 Promotion behaviour

The existing `promote` flow:

1. runs from workspace code;
2. snapshots the workspace using rsync or a Python fallback;
3. includes modified tracked files;
4. includes untracked files unless excluded;
5. prepares or reuses a dependency bundle;
6. creates an installed release directory;
7. moves the `current` and `previous` symlinks;
8. writes release metadata;
9. restarts the systemd services.

The promotion excludes common secrets, environments, caches, databases, jobs, outputs and media directories. However, `runs/` is not excluded, which caused dev run records to be copied into the installed release.

### 8.2 Operator-command dependency

The commands:

```text
/usr/local/bin/dev
/usr/local/bin/prod
/usr/local/bin/promote
```

all execute `scripts/ops/operator_commands.py` using the workspace’s video-automation Python environment. Consequently:

- the live services do not run from the workspace;
- but management of the live services can still be changed or broken by uncommitted workspace edits;
- a dirty workspace can determine what is installed;
- a Git push is not currently the acceptance boundary.

### 8.3 Planning conclusion

The unsafe part of the existing deployment model is not simply the existence of a staged release or `current` symlink. The principal problems are:

- dirty and untracked code can become live;
- operator tooling runs from the mutable workspace;
- acceptance is not tied to a pushed clean commit;
- state ownership is split and environment-dependent.

Stage 2 must evaluate the existing staging, dependency-bundle and symlink mechanisms individually. Their useful safety may be retained behind a simpler manual install workflow. No decision to retain or remove them has yet been made.

## 9. Code preservation findings

### 9.1 Three code states

| State | Identity | Character |
| --- | --- | --- |
| Local `origin/main` | Commit `62fdd82…`, dated 30 June 2026 | 330 tracked files; network freshness not checked |
| Installed live release | Same recorded commit plus dirty snapshot | About 716 non-venv files plus dependency links |
| Editable workspace | Same HEAD plus extensive changes | 133 modified tracked files and roughly 1,789 untracked files, mostly mixed source and runtime data |

### 9.2 Valuable code delta

The Stage 1B classification found:

- 100 modified tracked application/deployment/test files that are byte-identical between the workspace and the live release but differ from `origin/main`;
- approximately 367 valuable untracked source/configuration/deployment/test/documentation paths present identically in the workspace and live release;
- no modified tracked file whose live content differs from the corresponding current workspace content;
- two valuable live-only historical documents:
  - `system-context/selection-upgrade/architecture-guardrails.md`;
  - `system-context/selection-upgrade/rough-plan.md`;
- one workspace-only Continuity planning document;
- large amounts of generated jobs, caches, reports and runtime data that are not code but have not been authorised for deletion.

Important untracked product work includes:

- funnel rule registry and GTA-specific AI rules;
- the redesigned authenticated ops UI;
- funnel-management code and templates;
- runtime upload authority and controls;
- configuration/runtime-path infrastructure;
- reliability, observability, storage and operations scripts;
- face-tracking reframing code and model asset;
- large test suites and operational documentation;
- the top-level `run.sh` and `update.sh` workflows.

### 9.3 Temporary code authority

Until preservation is performed, neither `origin/main` nor the installed release alone is the complete source of truth.

The temporary preservation baseline is:

> The carefully classified current workspace, plus the two live-only historical documents, excluding generated data, secrets, caches and machine-specific state.

Before the first implementation change, Stage 3 must:

1. refresh the Git remote safely after removing the embedded credential from the remote URL;
2. re-run the valuable-path inventory;
3. review meaningful code differences semantically;
4. recover the two live-only documents;
5. classify all untracked paths;
6. create a clean preservation commit;
7. verify that a clean checkout contains everything needed to reproduce the application.

A dirty release name containing the old commit hash must never again be treated as sufficient version identity.

## 10. Application architecture and coupling

### 10.1 Component structure

| Component | Entrypoint | Main dependencies |
| --- | --- | --- |
| Source input | `source-input/input_service/app.py` | `/etc` funnels, source state, video-automation input and API |
| Video automation | `video-automation/server/app.py` | pipeline configuration, source-input code imports, AI HTTP, output-funnel HTTP, GPU/media tools |
| Output funnel | `python -m output_funnel.app` | channels/settings, output database, controls and upload authority |
| Ops UI | `python -m ops_ui` | all service APIs, funnel registry, controls, databases and cross-component configuration writes |
| AI service | `ai-service/app.py` | Ollama, AI registry/prompts, controls and decision artifacts |
| Shared infrastructure | `scripts/config`, `scripts/ops`, `scripts/observability`, `scripts/storage` | imported or executed across components |

The services have distinct processes but are not fully modular in state ownership.

### 10.2 Important cross-component couplings

| Coupling | Mechanism | Consequence |
| --- | --- | --- |
| Source input → video automation | Filesystem write plus HTTP job creation | Both directory and API contracts must agree |
| Video automation → source input | Direct Python imports from source-input ledger/duplicate code | Code boundary is not independent |
| Video automation → AI service | HTTP | Relatively clean boundary |
| Video automation → output funnel | HTTP plus absolute media paths | Shared filesystem assumptions remain |
| Ops UI → all components | HTTP plus writes into other components’ config | UI can mutate multiple authorities |
| Multiple services → controls | Shared control files | Control ownership and precedence are unclear |
| All components → shared scripts | Python imports and operator scripts | Installed code must preserve shared-module resolution |

### 10.3 Runtime mutation problems

Current code can write to:

- `/etc/mk04/<env>/source-input/funnels.json` and backups;
- `/etc/mk04/<env>/video-automation/funnels/<id>.json`;
- `/etc/mk04/<env>/output-funnel/channels.json`;
- `/etc/mk04/<env>/video-automation/pipeline_config.json`;
- `ai-service/config/funnel_rule_registry.json` under the code root;
- `ai-service/prompts/funnel_rules/` under the code root;
- `config/funnels/<id>.yaml` under the code root;
- workspace fallback paths for controls, runs, logs and databases;
- canonical `/var/lib` runtime paths when environment configuration is complete.

This violates the desired separation between replaceable code and mutable installation state.

## 11. Current configuration and state topology

### 11.1 Main roots

| Root | Current purpose |
| --- | --- |
| `/home/maguireltd/mk1-automated-clipping` | Editable code plus hybrid dev/runtime data |
| `/opt/mk04/prod/current` | Installed live code selected through a symlink |
| `/opt/mk04/prod/releases` | Installed release snapshots |
| `/opt/mk04/prod/dependency-bundles` | Per-requirements-hash Python environments |
| `/etc/mk04/dev` | Dev environment and runtime configuration |
| `/etc/mk04/prod` | Live prod environment and runtime configuration |
| `/var/lib/mk04/dev` | Most historical processing state and media |
| `/var/lib/mk04/prod` | Thin live state used by the running services |
| `/var/lib/mk04/locks` | Shared execution/promotion coordination |
| `/var/log/mk04/dev` and `prod` | File log roots; journald is the main prod log source |

### 11.2 Size and importance

| State area | Approximate content | Planning significance |
| --- | ---: | --- |
| `/var/lib/mk04/prod` | 4.4 MiB, seven files | Behaviourally live but mostly empty |
| `/var/lib/mk04/dev` | 39 GiB, about 729 files | Main historical state and media |
| Workspace `jobs/` | 1.4 GiB, about 982 files | Separate older history requiring reconciliation |
| Ollama model tree | 24 GiB | Shared machine asset used by live AI |
| Operator Hugging Face cache | 1.5 GiB | User-owned models inaccessible to `mk04` |
| Operator Whisper cache | 73 MiB | User-owned model inaccessible to `mk04` |

The future Continuity state model cannot simply rename the current prod tree and discard dev. Authority must be selected category by category.

## 12. Funnel and configuration findings

### 12.1 Current funnel sync direction

The ops-ui registry object is currently projected into multiple formats:

1. source-input `funnels.json` under `/etc`;
2. video-automation per-funnel JSON under `/etc`;
3. output-funnel channel configuration under `/etc`;
4. AI funnel-rule registry under the code tree;
5. AI prompt/rule files under the code tree;
6. ConfigManager funnel YAML under the code tree.

Runtime services read these projections rather than using the ops-ui registry directly.

### 12.2 `gta_clips_002` reconciliation

| Representation | Current state |
| --- | --- |
| Dev ops-ui registry | Present |
| Prod ops-ui registry | Present and byte-identical to dev |
| Dev source-input `funnels.json` | Contains `gta_clips_002` |
| Prod source-input `funnels.json` | Does not contain `gta_clips_002` |
| Dev video-automation funnel JSON | Present |
| Prod video-automation funnel JSON | Missing |
| ConfigManager GTA YAML | Present and identical in workspace/live release |
| AI registry mapping | Present and identical in workspace/live release |
| GTA prompt/rule file | Present and identical in workspace/live release |

The prod registry contains the funnel, but the live runtime projections are incomplete.

The cause is evidenced: ops-ui attempted to create a backup under `/etc/mk04/prod`, and systemd correctly denied the write because of `ProtectSystem=full`.

### 12.3 Planning interpretation

The audit supports the following Stage 2 requirement:

> A user-editable funnel instance and its generated runtime projections cannot remain split between persistent registry state, read-only `/etc` and the installed code tree.

It does not yet decide whether the future canonical funnel object is the existing registry format or another persistent schema.

## 13. Database findings

All examined SQLite databases passed read-only integrity checks.

| Database | Current use | Significant content |
| --- | --- | --- |
| Prod ops-ui DB | Used by live UI | 18 action-log rows; no clip reviews or database controls |
| Dev ops-ui DB | Not currently served | 25 action-log rows, 38 control rows |
| Workspace ops-ui fallback DB | Not active | Small stale history |
| Prod output-funnel DB | Used by live service | Schema version 7; content tables empty; live WAL/SHM present |
| Dev output-funnel DB | Historical | Schema version 7; 10 source jobs, 52 clips and associated assets/variants/events; no upload jobs |
| Workspace `database/dev.db` | Placeholder | One stub table/row |
| Prod `database/` directory | Reserved path | Empty; expected `prod.db` was never created |

The dev and prod output-funnel schemas agree, but their histories do not. Stage 2 must decide whether active history is migrated, archived or preserved separately. No database merge has been authorised.

## 14. Jobs, media, analytics and history

### 14.1 Main historical tree

`/var/lib/mk04/dev/video-automation/jobs` contains:

- 19 job directories;
- approximately 20.8 GiB;
- work dated 5–10 July 2026;
- GTA and MFM-related jobs;
- successful, failed and two apparently running statuses.

No process had a working directory under those jobs and the shared gate was free. The two “running” statuses are probably stale, but all jobs must be preserved until explicitly classified.

Associated dev trees contain approximately:

- 8.9 GiB of input media;
- 9.0 GiB of temporary/chunk media;
- 0.5 GiB of output media;
- source-input ledgers;
- analytics and run records.

### 14.2 Separate workspace history

The workspace contains:

- about 1.4 GiB under `jobs/dev`;
- 102 run-record files under `runs/dev`;
- reports, storage markers and AI decision artifacts;
- a subset of `runs/dev` accidentally copied into the live release.

### 14.3 Accepted planning rule

Stage 1B used “state” or “generated” classifications to distinguish these files from source code. Those labels do not authorise deletion.

The accepted rule is:

> No job, media, database, report, credential, control or historical artifact will be discarded until Stage 2 classifies its future role and Stage 3 preserves the required copy.

Preservation may mean active migration, read-only archive or verified backup. It does not necessarily mean loading all historical media into the future live runtime.

## 15. Controls, uploads and scheduling

### 15.1 Current upload gates

Real uploads are blocked by several independent mechanisms:

| Gate | Effective live value |
| --- | --- |
| `MK04_UPLOAD_MODE` | `dry_run` |
| ConfigManager `uploading.enabled` | `false` |
| Runtime `control_state.uploads_disabled` | `true` |
| Output-funnel plan worker | Disabled |
| Output-funnel upload worker | Disabled |
| `OUTPUT_FUNNEL_AUTO_UPLOAD` | Disabled |
| Prod credentials | Required files missing |

The dev environment is additionally rejected as non-production by the upload authority. Its `uploads_disabled=false` control-state value does not enable real API upload.

### 15.2 Control-plane conflict

Current safety and operational decisions are distributed between:

- environment modes;
- Git-controlled YAML;
- `control_state.json`;
- ops-ui `controls.json`;
- output-funnel settings;
- worker-enable environment variables.

This is safe in the sense that several gates currently fail closed, but difficult to understand. Stage 2 must define the future persistent safety authority and the relationship between hard safety mode and operator controls.

### 15.3 Scheduling verification

The following were checked:

- no MK04 systemd timer;
- no MK04 file under `/etc/cron.d`;
- no `maguireltd` crontab;
- no `mk04` crontab;
- no root crontab;
- no relevant `maguireltd` user timer;
- only normal Ubuntu/Snap maintenance user timers;
- no active dev ports;
- no observed pipeline process outside the five systemd services.

Therefore:

> No MK1 scheduling mechanism is currently installed or active.

Continuity must preserve this disabled/manual state. It must not install scheduling as a side effect.

## 16. Secrets and credentials

### 16.1 Environment-secret presence

The audit recorded only presence states, never secret values.

- Prod contains non-empty service-to-service secrets and ops-ui authentication secrets.
- Dev contains an OpenAI API key and ops-ui authentication secrets.
- Prod `OPENAI_API_KEY` is empty because the current live AI backend is local.
- Dev service-to-service secrets are empty.

### 16.2 Platform credential files

| Credential | Current state |
| --- | --- |
| Prod yt-dlp cookie path | Configured, file missing |
| Prod YouTube OAuth token | Configured, file missing |
| Prod YouTube client secret | Configured, file missing |
| Dev yt-dlp cookies | Present, non-empty, mode 600, owned by `maguireltd` |
| Dev OAuth token/client secret | Missing |

The `mk04` account cannot read the dev cookie file.

### 16.3 Accepted planning rules

- Missing credentials will not be created as part of Continuity.
- Existing credential material must be preserved without enabling upload.
- Credentials required by a live service must eventually live outside the operator home and be deliberately readable by the service account.
- Credential placement and upload authority are separate decisions; making a file readable must not make real upload possible.
- Secrets remain outside Git.

## 17. Dependencies, models and service-account capability

### 17.1 Python dependencies

The application currently uses per-service `requirements*.txt` files with version ranges rather than complete lockfiles.

The existing installer hashes five requirements files and creates a shared dependency bundle:

```text
/opt/mk04/prod/dependency-bundles/716deb054e91ccfc44b9/
```

Each installed service `.venv` is a symlink into that bundle.

The active video-automation environment contains approximately 136 packages, including:

- torch `2.8.0+cu128`;
- WhisperX `3.8.6`;
- Flask `3.1.3`.

The production bundle currently includes development/test dependencies because promotion uses `video-automation/requirements-dev.txt`.

Stage 2 must decide whether to retain dependency bundles, introduce lockfiles, separate test dependencies and/or use application-local environments.

### 17.2 Machine dependencies

The live system depends on:

- FFmpeg and ffprobe;
- NVIDIA driver and CUDA-compatible PyTorch;
- Ollama;
- the configured `qwen2.5:14b-instruct` model;
- yt-dlp inside the source-input bundle;
- WhisperX and model caches for transcription;
- a code-shipped BlazeFace model for optional reframing.

No Node/Remotion package manifest currently exists.

### 17.3 Model accessibility

| Asset | Location | Live accessibility |
| --- | --- | --- |
| Ollama models | `/usr/share/ollama/.ollama`, about 24 GiB | Available through Ollama HTTP |
| Whisper cache | `/home/maguireltd/.cache/whisper`, about 73 MiB | `mk04` read failed; also blocked by `ProtectHome=true` |
| Hugging Face cache | `/home/maguireltd/.cache/huggingface`, about 1.5 GiB | `mk04` read failed; also blocked by `ProtectHome=true` |
| BlazeFace file | Shipped inside code tree | Available in current release |

The production video-automation Python environment successfully imported torch as `mk04` and reported CUDA available.

Stage 2 must provide a service-accessible persistent location for any transcription models required by live operation, or define an explicit validated download/preparation step.

## 18. Backups and recovery gaps

### 18.1 Existing backup coverage

Current backup artifacts cover only small portions of the system:

- a backup of the stub workspace `dev.db`;
- a few source-input funnel backups;
- a few AI registry backups;
- installed dirty code releases and a `previous` symlink.

The existing operations backup script intentionally excludes credentials, media and large databases.

### 18.2 Not currently protected

No examined backup covers the complete required set of:

- `/var/lib/mk04/dev` and its 39 GiB of history;
- prod or dev output-funnel databases with associated WAL state;
- ops-ui databases;
- live control state;
- dev cookies;
- machine models and caches;
- the complete reconciled dirty application source.

Installed releases are code snapshots, not state backups.

### 18.3 Stage 3 requirement

Before live infrastructure is changed, Stage 3 must create and verify a preservation set that includes:

- the reconciled code baseline;
- all unique funnel/configuration representations needed for migration;
- environment and machine configuration;
- secrets and credentials with safe permissions;
- databases including consistent SQLite snapshots;
- controls and lock-state definitions;
- jobs, outputs, inputs, analytics and history selected for preservation/archive;
- required models/assets or a verified reproducible acquisition method;
- current unit files and installation metadata;
- a manifest and restoration instructions.

## 19. Prioritised risks discovered

### 19.1 Preservation-critical

1. Hundreds of valuable files are uncommitted and absent from `origin/main`.
2. Current backups do not cover the primary state tree.
3. Valuable state is split across dev, prod and workspace paths.
4. The only existing YouTube cookie is private to the operator account.
5. Historical databases and live databases contain different records.

### 19.2 Architecture-critical

1. Ops-ui writes user configuration into read-only `/etc` and replaceable code.
2. Operator commands depend on dirty workspace code and its interpreter.
3. Funnel authority is fragmented across registry, JSON, YAML and prompt/AI mapping files.
4. Dev path handling is hybrid and can fall back into the repository.
5. Several independent control planes make effective operational state difficult to reason about.
6. Candidate and live work share GPU, Ollama and lock concepts without a final isolation contract.
7. Models in the operator home are inaccessible to the hardened service account.
8. Requirement ranges do not guarantee a completely reproducible environment.

### 19.3 Later cleanup or separate maintenance

The following findings are real but must not drive scope expansion:

- `runs/dev` leaking into installed releases;
- stale release staging directories;
- absent logrotate installation;
- repository templates for cron that are not installed;
- missing production platform credentials;
- the Git remote containing an embedded credential;
- old or duplicate runtime reports.

The Git remote credential must be remediated before a preservation push, but it is not a reason to redesign the entire secret system.

## 20. Planning conclusions accepted after Stage 1

The following conclusions are now accepted inputs to Stage 2:

1. **Stage 1 is complete.** Further broad auditing is not required before architecture decisions begin.
2. **The existing live system remains untouched during design and construction.** The replacement will be built alongside it and rehearsed before cutover.
3. **Git will become the accepted code authority, but it is not the authority yet.** A preservation/reconciliation step is required first.
4. **A Git push will not automatically deploy.** Installation remains explicitly initiated by the operator.
5. **The live application must never run from the editable workspace.**
6. **The exact installed commit must be visible.** Dirty/uncommitted application installs are prohibited in the future model.
7. **No state is deleted based solely on a code audit classification.** Active migration, archival preservation and eventual deletion are separate decisions.
8. **Mutable user configuration cannot remain in the installed code tree.**
9. **The `/etc` write failure should be solved by moving mutable state, not weakening systemd hardening.**
10. **The future state model must distinguish active live state, historical archive, shared machine assets and disposable test state.**
11. **The future test workspace receives no real upload credentials and cannot write to live state.**
12. **Uploading and scheduling remain disabled throughout the upgrade.**
13. **Service-accessible model and credential locations are required.** Operator-home caches and private files are not a valid live dependency.
14. **The current release staging, dependency bundle and symlink mechanisms are not automatically condemned.** Stage 2 will decide whether their safety is worth retaining behind a simpler workflow.
15. **The permanent daily UI cannot safely be a disposable candidate UI if it edits live state.** The proposed solution is one accepted live MK1 UI plus temporary isolated test UI instances, but the final UI decision remains for Stage 2.
16. **Brief planned cutover downtime is the current simplicity-oriented recommendation, not yet a locked design decision.**
17. **Recovery remains manually initiated.** No automatic deployment or automatic rollback system is required.

## 21. Decisions deliberately deferred to Stage 2

Stage 1 provides evidence but does not decide:

1. the exact workspace, installed-code, active-state, archive, shared-asset and test-state paths;
2. whether accepted versions are represented only by `origin/main`, by a tag or by another explicit approval record;
3. whether the current `current` symlink/release staging is retained, simplified or replaced;
4. whether dependency bundles are retained or replaced by application-local/versioned environments;
5. whether full lockfiles are introduced;
6. the canonical persistent funnel representation;
7. which funnel projections remain necessary and who generates them;
8. which dev and prod configuration values become the future active values;
9. whether historical databases are merged, archived or preserved separately;
10. whether the 39 GiB dev tree becomes active state, an archive or a mixture;
11. how the separate workspace jobs tree relates to the `/var/lib` history;
12. the single persistent upload/scheduler control plane and its fail-closed defaults;
13. which existing secrets and credentials are migrated into the live credential area;
14. where Whisper/Hugging Face models will live;
15. how live and test jobs coordinate GPU and Ollama use;
16. the one permanent UI arrangement and temporary test UI behaviour;
17. final service names, ports and boot wiring;
18. configuration and database migration/versioning rules;
19. the precise manual install, cutover and recovery procedures;
20. the observation period and conditions for retiring old infrastructure.

## 22. Stage 2 decision sequence

Stage 2 will proceed in this order:

1. Define the logical areas and access boundaries.
2. Decide the accepted-code and installation model.
3. Classify active state, historical archives, shared assets and test state.
4. Assign one authority for each configuration category.
5. Define funnel ownership and derived projections.
6. Define database and historical-data preservation.
7. Define controls, secrets and credential ownership.
8. Define workspace-test isolation.
9. Define dependencies, models and GPU coordination.
10. Define services, UI, ports and boot behaviour.
11. Define manual installation, migration and recovery.
12. Validate the complete architecture for missing details, contradictions and unnecessary complexity.
13. Recreate the authoritative MK1 Continuity Upgrade Plan.

## 23. Stage 1 completion statement

Stage 1 is formally complete.

The existing system is sufficiently reconstructed to begin architecture decisions without relying on assumptions. The evidence shows both what is already useful and what must change. The target design can now be developed without confusing labels such as dev/prod with the actual lifecycle boundaries between editable code, accepted code, live code, active state, archives, machine assets and test state.

The next planning task is:

> **Stage 2.1 — Define the logical areas and their access boundaries.**

No implementation is authorised by this document.