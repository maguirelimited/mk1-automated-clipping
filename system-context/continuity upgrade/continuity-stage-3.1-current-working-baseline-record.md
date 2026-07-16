# MK1 Continuity Upgrade — Stage 3.1 Current Working Baseline and Reconstruction Record

- **Stage:** 3.1 — Record the Current Working Baseline
- **Status:** Complete
- **Completion date:** 16 July 2026
- **System:** MK1 hardware / MK04 application stack
- **Evidence cut-off:** 2026-07-16T01:16:27+01:00 (Europe/London)
- **Published code baseline (commit):** `336946030f5165a910ff95e6a304bd6d5f2e753b`
- **Published code baseline (tree):** `faa27e98b843766f48406c8f01c31ff2f25be9ed`
- **Currently deployed release:** `/opt/mk04/prod/releases/20260714T184302Z_62fdd82_dirty`
- **Authority:** This record is subordinate to the Authoritative Master Plan, the Stage 1 Infrastructure Reconstruction Record, and the Stage 2 Continuity Model. It does not replace them.
- **Publication vs deployment:** Publishing the Git baseline did **not** deploy that baseline. Live runtime remains the dirty installed release named above.
- **Record-containing commit:** Any future Git commit that adds this file is documentation-only. That commit identity is **not** the code-baseline identity (`336946030f5165a910ff95e6a304bd6d5f2e753b`).

---

## 1. Purpose, authority and scope

This record freezes a readable account of:

1. the **current working system** on host `maguireltd-mk1` as observed through Stage 3.1 evidence;
2. the **published source baseline** now at `origin/main`;
3. the **reconstruction inputs** required to rebuild the old installed system or to check out the published source tree.

It records state and reconstruction procedure. It does **not** implement the Stage 2 Continuity runtime, installer, or migration. Stages 3.2 and later remain pending. Uploads, upload workers, and automatic scheduling remain out of scope and disabled for Continuity work.

Relationship:

| Authority | Role relative to this record |
| --- | --- |
| Stage 2 Continuity Model | Defines the target Continuity architecture; not implemented here |
| Stage 1 Reconstruction Record | Prior infrastructure reconstruction evidence; reused and cited |
| Authoritative Master Plan | Overall Continuity programme authority |
| This Stage 3.1 record | Current baseline + old-installation reconstruction inputs |

---

## 2. Stage 3.1 completion statement

| Stage 3.1 success criterion | Satisfied? | Evidence class |
| --- | --- | --- |
| Running release recorded | Yes | VERIFIED |
| Valuable dirty code reconciled into a preservation candidate | Yes | VERIFIED |
| Clean preservation baseline published to GitHub `main` | Yes | VERIFIED |
| Clean remote checkout verified (including post-hygiene) | Yes | VERIFIED |
| Services and health recorded | Yes | VERIFIED |
| Dependencies and machine requirements recorded | Yes | VERIFIED (bundle + inventories); DEFERRED UNKNOWN (byte-exact lock) |
| Launch/configuration bindings recorded (non-secret) | Yes | VERIFIED |
| Old-installation reconstruction procedure recorded | Yes | VERIFIED (procedure written; not executed in Stage 3.1) |
| Git push proven not to deploy | Yes | VERIFIED |
| Live runtime and mutable state unchanged by publication | Yes | VERIFIED |

---

## 3. Evidence method and classification

| Label | Meaning |
| --- | --- |
| `VERIFIED` | Confirmed against current host, Git objects, HTTP health, or readable files at or after the evidence cut-off |
| `OPERATOR-ATTESTED` | Stated by the operator outside automated re-check in this cut-off (dated when known) |
| `INFERRED` | Derived from verified facts without direct re-observation |
| `DEFERRED UNKNOWN` | Required later; destination stage named |

**Limitations**

- Passwordless sudo is unavailable (`sudo -n` requires interactive auth). Root-only materials that could not be re-read are inherited from Stage 3.1-I4 (observed **15 July 2026**) and labelled accordingly.
- `/usr/local/bin/mk04-operator-commands.meta` was root-mode `0600` and unread in I4 — DEFERRED UNKNOWN pending privileged capture.
- Cron absence for `mk04` and `root` was **OPERATOR-ATTESTED on 15 July 2026** after I1. Re-checked at this cut-off for the operator crontab, `/etc/cron.d/mk04*`, and mk04-related systemd timers (absent) — VERIFIED for those scopes; root crontab not re-read without sudo.

---

## 4. Current host and role identities

| Item | Value | Class |
| --- | --- | --- |
| Hostname | `maguireltd-mk1` | VERIFIED |
| Operator account | `maguireltd` (uid 1000); groups include `sudo`, `mk04`, `ollama` | VERIFIED |
| Service account | `mk04` (uid 994, gid 972); home `/var/lib/mk04`; shell `nologin` | VERIFIED |
| Ollama account | `ollama` (uid 997, gid 973); home `/usr/share/ollama`; shell `/bin/false` | VERIFIED |
| OS | Ubuntu 26.04 LTS (resolute) | VERIFIED |
| Kernel | `7.0.0-27-generic` (#27-Ubuntu SMP PREEMPT_DYNAMIC, x86_64) | VERIFIED |
| System Python | `/usr/bin/python3.11` → Python 3.11.15 | VERIFIED |
| Service Python | Python 3.11 from dependency-bundle component venvs (`…/bin/python`) | VERIFIED |
| GPU | NVIDIA GeForce RTX 4070 SUPER | VERIFIED |
| NVIDIA driver | 595.71.05 | VERIFIED |
| CUDA (driver-reported) | 13.2 | VERIFIED |
| Torch CUDA build (VA bundle) | `2.8.0+cu128` (CUDA 12.8) | VERIFIED |
| FFmpeg | 8.0.1-3ubuntu2 | VERIFIED |
| Ollama | 0.30.11 | VERIFIED |

---

## 5. Current deployed release

| Item | Value | Class |
| --- | --- | --- |
| `current` | `/opt/mk04/prod/current` → `/opt/mk04/prod/releases/20260714T184302Z_62fdd82_dirty` | VERIFIED |
| `previous` | `/opt/mk04/prod/previous` → `/opt/mk04/prod/releases/20260714T153405Z_62fdd82_dirty` | VERIFIED |
| Recorded source SHA in release | `62fdd82433b8b0b08b4182739cba6c76a3b538a0` | VERIFIED |
| Release dirty flag | `true` | VERIFIED |
| `release_manifest.json` SHA-256 | `69372fbe5e92e8da1698e36009721686405dffc8f7ff6dedde95e3e84ad3aff4` | VERIFIED |
| Manifest dirty_files count | 133 | VERIFIED |
| Manifest untracked_files count | 200 | VERIFIED |
| Promotion status file | `/opt/mk04/prod/last_promotion_status.json` SHA-256 `33cae056af76d99aa3c9a5ce858bb4f9fb1c2835b51e157ae9240e6d387d79da` | VERIFIED |
| Dependency bundle | `/opt/mk04/prod/dependency-bundles/716deb054e91ccfc44b9` (hash `716deb054e91ccfc44b9`, ~7.9G) | VERIFIED |
| Bundle identity.json SHA-256 | `c84dc6b86ce5155654a9e785d4a2332413a28eac5099671a99b18d559d98c75a` | VERIFIED |
| Release inventory hash (names under `/opt/mk04/prod/releases`) | `a093ba3a556d70387bec1895337232230515954bf5349ea33cc4047a0bf99194` | VERIFIED |

**Deployed vs published:** the live release is a **dirty promotion** of recorded SHA `62fdd824…` plus workspace-dirty and untracked content captured into that release directory. The published Git baseline is `33694603…` (tree `faa27e98…`). They are different identities.

**Why the recorded SHA alone cannot reconstruct the running source:** the release is marked dirty; the manifest lists 133 dirty paths and 200 untracked paths relative to that SHA; runtime and generated artifacts were present in the promotion snapshot; and valuable untracked application work existed only in the workspace/live overlay until Stage 3.1 preservation published it.

---

## 6. Services, ports, boot and health

All five MK04 units: **enabled** and **active (running)** at cut-off. User/group **`mk04`/`mk04`**. Restart **`always`**. Hardening common to all five: `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=full`, `ProtectHome=true`. Primary `EnvironmentFile=/etc/mk04/prod/env` (required). Optional per-service `EnvironmentFile=-/etc/mk04/prod/services/<name>.env` — **none of those override files exist** at cut-off.

| Unit | Port | Health | Expected | WorkingDirectory | ExecStart | Installed unit SHA-256 | Ordering |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `mk04-ai-service` | 5075 | `/health` | 200 | `/opt/mk04/prod/current/ai-service` | `…/run-ai-service.sh prod` | `262f1a8a88584a23581c34df00bc01e2f20e8fcded898ca6481a69a5261fac62` | After `network-online.target` `ollama.service`; Wants both |
| `mk04-ops-ui` | 5070 | `/health` | 401 (auth gate) | `/opt/mk04/prod/current/ops-ui` | `…/run-ops-ui.sh prod` | `4443c1c9b5ecb09f6d905b2926d67d46ce2ba63f9a77889069709bee515d3620` | After/Wants `network-online.target` |
| `mk04-output-funnel` | 5055 | `/healthz` | 200 | `/opt/mk04/prod/current` | `…/run-output-funnel.sh prod` | `c9e5c1b5a4fec6f11ae2a5d21a9089f19c571210a679ecd5ac661dc0a404dd11` | After/Wants `network-online.target` `mk04-video-automation.service` |
| `mk04-source-input` | 5060 | `/healthz` | 200 | `/opt/mk04/prod/current` | `…/run-input-service.sh prod` | `63a2f6e7eb507686d5cd0e61d5e2e2c37402c588306de3675699ebcfacc4ba75` | After/Wants `network-online.target` |
| `mk04-video-automation` | 5050 | `/healthz` | 200 | `/opt/mk04/prod/current` | `…/run-video-automation.sh prod` | `2330a4f3876c8d9cab74c4dd4fa4e29f38fb1e2537564acf4cc0224bee74677a` | After/Wants `network-online.target` `mk04-source-input.service` |

**Ollama:** unit `ollama.service` enabled/active; `User=ollama`; `ExecStart=/usr/local/bin/ollama serve`; listens on `127.0.0.1:11434`. AI service is ordered after Ollama.

Health at cut-off (VERIFIED): 5060/5050/5055/5075 → 200; 5070 → 401.

---

## 7. Safety posture

| Control | Value | Class / date |
| --- | --- | --- |
| `MK04_UPLOAD_MODE` | `dry_run` | VERIFIED (2026-07-16T01:16:27+01:00) |
| `OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED` | `0` | VERIFIED |
| `OUTPUT_FUNNEL_AUTO_UPLOAD` | `0` | VERIFIED |
| `MK04_SCHEDULER_MODE` | `manual` | VERIFIED |
| Runtime `control_state.json` | `uploads_disabled=true`, `scheduler_disabled=true` | VERIFIED (`/var/lib/mk04/prod/data/control_state.json`, updated_at `2026-07-14T01:53:13Z`) |
| Workspace `ops-ui/data/controls.json` | present; `uploads_paused=false` (UI flag) — does **not** override env dry-run / runtime disable | VERIFIED |
| mk04-related systemd timers | none | VERIFIED (2026-07-16T01:16:27+01:00) |
| `/etc/cron.d/mk04*` | absent | VERIFIED |
| Operator crontab | empty/absent | VERIFIED |
| `mk04` / `root` crontabs | absent | OPERATOR-ATTESTED 15 July 2026 (post-I1) |
| Credential material | secret-bearing env keys present or empty as listed in section 14; `/var/lib/mk04/prod/credentials/` empty of files | VERIFIED (presence only) |

**No Stage 3.1 action enabled uploads, workers, or scheduling.**

---

## 8. Git provenance and authentication

| Item | Value | Class |
| --- | --- | --- |
| Original Git base SHA (pre-preservation) | `62fdd82433b8b0b08b4182739cba6c76a3b538a0` | VERIFIED |
| Initial preservation commit | `4490ef174deebcb0f3edfb6d74bedf440b76823a` | VERIFIED |
| Preservation tree | `db198d11986561ee353c3e87382b3a8728c541f3` | VERIFIED |
| Preservation subject | `Stage 3.1: preserve current MK04 application baseline` | VERIFIED |
| Final hygiene commit (code baseline) | `336946030f5165a910ff95e6a304bd6d5f2e753b` | VERIFIED |
| Final tree | `faa27e98b843766f48406c8f01c31ff2f25be9ed` | VERIFIED |
| Hygiene subject | `Stage 3.1: normalize continuity records and remove redundant archive` | VERIFIED |
| Origin URL | `git@github-mk1-automated-clipping:maguirelimited/mk1-automated-clipping.git` | VERIFIED |
| SSH alias | `Host github-mk1-automated-clipping` → IdentityFile deploy key | VERIFIED |
| Deploy-key fingerprint | `SHA256:1ikMbh5+R71P0Vi07CFX+6meYJaLIG47bKcbC+FBn7U` | VERIFIED |
| Strict host-key verification | used with repository-specific alias (M1) | VERIFIED (prior Stage 3.1-M1) |
| Read/write GitHub verification | successful normal fast-forward push of both preservation and hygiene commits (`HEAD:refs/heads/main`, non-force) | VERIFIED |
| Former HTTPS token in remote | revoked | OPERATOR-ATTESTED (M1) |
| Token residue in scoped locations | none found | VERIFIED (M1 scan) |
| Rejected Cursor-trailer commit `82394585b87d9717d71b33dc5a71effc5650e9a4` | never pushed; absent from remote refs | VERIFIED (M4C/M5C) |
| Push deploys release? | No — `current`/`previous`/inventory unchanged across both publications | VERIFIED |

Private-key material and any historical token values are intentionally omitted.

---

## 9. Dirty-code reconciliation

Starting from workspace HEAD `62fdd824…` before preservation:

| Fact | Count / identity | Class |
| --- | --- | --- |
| Initial tracked modified | 125 | VERIFIED (I2/M3 classification basis) |
| Initial tracked deleted | 8 | VERIFIED |
| `INCLUDE_CURRENT` preserved modifications | 102 | VERIFIED |
| `INCLUDE_CURRENT` manifest SHA-256 | `ff90590e116439a152fa0171de23a8047032a32b95abe6903f93a9c0adb7d548` | VERIFIED |
| Why 102 ≠ earlier “100 valuable” | I3 counted 100 valuable tracked mods; M3A added **two approved modified `.env.example` files** → 102 | VERIFIED |
| `RETAIN_HEAD` generated/runtime (removed from new tree) | 23 | VERIFIED |
| `ACCEPT_DELETION` | 8 | VERIFIED |
| `ACCEPT_DELETION` manifest SHA-256 | `1e8ee039c4e85a3a1a02e6b922d72a01313f5c89f5af07c7d07f7bc24b0d1431` | VERIFIED |
| Additional unmodified tracked bytecode removed | 9 (classification) | VERIFIED (M3) |
| Tracked log files removed | 2 | VERIFIED |
| `REMOVE_FROM_CANDIDATE` generated-removal set | 34 | VERIFIED |
| `REMOVE_FROM_CANDIDATE` SHA-256 | `927b7e0ad2af7f46bc1a6e07c9dc6f8e37a55e5dd152a09aaf0043de91c7d5da` | VERIFIED |
| Untracked INCLUDE additions | 367 | VERIFIED |
| Untracked INCLUDE SHA-256 | `911e7c11794b3523f9f7d441d5ffa288e0ec2d689388144373292dae1fbe8d54` | VERIFIED |
| Untracked EXCLUDE at classification | 1428 | VERIFIED |
| Untracked EXCLUDE SHA-256 | `8a70ba6f06245643e2f853d621d97a9d952261fb4ece52bb71e9ed8784184f4f` | VERIFIED |
| Large ignored roots | per-service venvs, caches, `__pycache__`, pytest caches, runs, locks (tens of thousands of ignored files) | VERIFIED |
| Initial preservation diff vs `62fdd824…` | M=102, A=367, D=42 | VERIFIED |
| D=42 composition | 8 `ACCEPT_DELETION` + 34 `REMOVE_FROM_CANDIDATE` (= 23 `RETAIN_HEAD` + 9 unmodified bytecode + 2 tracked logs) = 42 | VERIFIED |
| Workspace vs live valuable fork | none found (identical valuable content) | VERIFIED (I2/I3) |

### Exact eight accepted deletions

1. `ai-service/prompts/section_candidate_discovery_v1.txt` — superseded prompt; absent from live  
2. `ops-ui/templates/clip_review.html` — removed UI; absent from live  
3. `ops-ui/templates/clip_review_detail.html` — removed UI; absent from live  
4. `source-input/input_service/control_gate.py` — relocated under package  
5. `source-input/input_service/data/state/input_jobs/input_20260523T002537Z_e56fa8c7.json` — runtime job state  
6. `upgrade-context/post-processing-upgrade.md` — legacy memo; remain deleted (recoverable from history)  
7. `upgrade-context/processing-upgrade.md` — legacy memo; remain deleted (recoverable from history)  
8. `video-automation/analytics/events.jsonl` — runtime analytics  

---

## 10. Code-selection decisions

- **BlazeFace** `video-automation/models/blaze_face_short_range.tflite` included (small, code-coupled; SHA-256 `b4578f35940bf5a1a655214a1cce5cab13eba73c1297cd78e1a04c2380b0152f`, 229746 bytes).
- Four safe `.env.example` files and four `.gitkeep` files preserved.
- Canonical master plan retained **exactly once** (no polluted duplicate packaging).
- Two live-only selection-upgrade documents recovered into the tree.
- Legacy `upgrade-context/*` memos remain deleted; blobs remain in Git history.
- Generated/runtime artifacts excluded from Git (controls DB, seen-URLs, bytecode, analytics, job JSON, etc.).
- Redundant `output-funnel/context/output-funnel-python-scripts.zip` removed from the Git tree after M5A proved no unique source and no consumer; physical workspace copy remains ignored; blob recoverable from history (`34abe7dae64fb4a444bedd1417471ac7027af7de` / parent trees).
- Trailing-whitespace / EOF findings were **not** rewritten during preservation (intentional).
- Stage 1/2 final-newline normalization occurred only in the later hygiene commit.

---

## 11. Controlling-document identities

| Document | Path | SHA-256 | Final LF |
| --- | --- | --- | --- |
| Stage 1 | `system-context/continuity upgrade/continuity-upgrade-plan-p1.md` | `c8e21006739366607884637658bd6f3e01fef468b954ca6ceca9a00c1a601469` | yes |
| Stage 2 | `system-context/continuity upgrade/continuity-upgrade-plan-p2.md` | `37aa3d720ffca63d7d4daa8311f331065bc4909f334aac0e6f7e9622ca75613b` | yes |
| Master plan | `system-context/continuity upgrade/continuity-upgrade-master-plan.md` | `73299c6d62ea50a7fe82bb758cde6a8c90c5b6edbe5be436875c9cc6766bcd94` | yes |
| Selection guardrails | `system-context/selection-upgrade/architecture-guardrails.md` | `77ce7da19349229b7172c1c4028e441ae7ab79d26a7bd5bee1d4e9b308c3b141` | yes |
| Selection rough plan | `system-context/selection-upgrade/rough-plan.md` | `138d6b7f3f3806d07aec63ffcb8f1a27eb66b03ec3b384cf8b4d24c581280c0c` | no (historical recovered bytes) |
| Root `.gitignore` | `.gitignore` | `d60c9081e8c000c859af49673dd265980e89344f273fbc9be24d319565e6dc79` | yes |

Exactly one copy of each controlling basename exists in the published tree. The obsolete pre-Stage-2 draft plan is not authoritative and is not treated as such here.

---

## 12. Published baseline contents

Final tree `faa27e98b843766f48406c8f01c31ff2f25be9ed` (commit `336946030f5165a910ff95e6a304bd6d5f2e753b`):

| Metric | Count |
| --- | ---: |
| Tracked paths | 654 |
| Python files | 429 |
| Bash scripts | 42 |
| Executable mode `100755` | 50 (33 additions vs `62fdd824…`) |
| Approved binary | 1 (BlazeFace) |
| Requirements-named files | 8 |
| Test paths | 194 |
| Examples | 10 |
| `.gitkeep` | 4 |
| `deploy/` | 44 |
| `config/` | 14 |
| `system-context/` + `docs/` | 31 |

**Entry points present:** `ai-service/app.py`, `ops-ui/ops_ui/app.py`, `output-funnel/output_funnel/app.py`, `source-input/input_service/app.py`, `video-automation/server/app.py`.

**Systemd templates present:** all five under `deploy/systemd/`.

**Absent from Git tree:** redundant ZIP; runtime trio; databases; real credentials; virtualenvs; dependency installations; pycache.

---

## 13. Clean-checkout proof

Independent SSH clones of remote `main` were performed in Stage 3.1-M5 and Stage 3.1-M5D (no local-repo clone, no tags/submodules/LFS, no dependency install, no application execution).

| Check | Result |
| --- | --- |
| Transport | `git@github-mk1-automated-clipping:maguirelimited/mk1-automated-clipping.git` |
| HEAD | `336946030f5165a910ff95e6a304bd6d5f2e753b` |
| Tree | `faa27e98b843766f48406c8f01c31ff2f25be9ed` |
| Parent | `4490ef174deebcb0f3edfb6d74bedf440b76823a` |
| Status | clean |
| Python 3.11 `py_compile` | 429 files, 0 failures |
| `bash -n` | 42 files, 0 failures |
| Secret detectors | only synthetic `openai_sk` fixtures in `ops-ui/tests/test_security_redaction.py`, `tests/config/test_config_schema.py`, `tests/observability/test_config_view.py` |
| Live system during verification | unchanged |

---

## 14. Launch and configuration bindings

Protected env file `/etc/mk04/prod/env`:

| Metadata | Value | Class |
| --- | --- | --- |
| Owner:group | `root:mk04` | VERIFIED |
| Mode | `0640` | VERIFIED |
| SHA-256 | `15e24ae9d3d8ebdf266e8cfad4f3847cdde8368ed53c509f4af1d394ff0c726f` | VERIFIED |

**Non-secret bindings (values):**

| Name | Value |
| --- | --- |
| `MK04_ENV` | `prod` |
| `MK04_CODE_ROOT` | `/opt/mk04/prod/current` |
| `MK04_CONFIG_ROOT` | `/etc/mk04/prod` |
| `MK04_RUNTIME_ROOT` | `/var/lib/mk04/prod` |
| `MK04_LOG_ROOT` | `/var/log/mk04/prod` |
| `INPUT_SERVICE_PORT` | `5060` |
| `VIDEO_AUTOMATION_PORT` | `5050` |
| `OUTPUT_FUNNEL_PORT` | `5055` |
| `OPS_UI_HOST` | `127.0.0.1` |
| `OPS_UI_PORT` | `5070` |
| `AI_SERVICE_PORT` | `5075` |
| `AI_PROVIDER` | `ollama` |
| `AI_MODEL` | `qwen2.5:14b-instruct` |
| `AI_BASE_URL` | `http://localhost:11434` |
| `OLLAMA_AUTO_PULL_MODEL` | `false` |
| `MK04_UPLOAD_MODE` | `dry_run` |
| `MK04_SCHEDULER_MODE` | `manual` |
| `OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED` | `0` |
| `OUTPUT_FUNNEL_AUTO_UPLOAD` | `0` |

**Secret-bearing key names (presence only; values not recorded):**

| Name | Non-empty? |
| --- | --- |
| `OPENAI_API_KEY` | no (blank) |
| `INPUT_SERVICE_SECRET` | yes |
| `VIDEO_AUTOMATION_SECRET` | yes |
| `OUTPUT_FUNNEL_SECRET` | yes |
| `YT_DLP_COOKIES_PATH` | yes (path configured) |
| `MFM_BUSINESS_AI_YT_TOKEN_FILE` | yes |
| `MFM_BUSINESS_AI_YT_CLIENT_SECRET_FILE` | yes |
| `OPS_UI_OPERATOR_PASSWORD` | yes |
| `OPS_UI_SECRET_KEY` | yes |

Intent of the blank OpenAI key is **DEFERRED UNKNOWN** (not established in Stage 3.1).

**Launch scripts:** `deploy/scripts/run-*-service.sh` / `run-ops-ui.sh` / `run-output-funnel.sh` / `run-input-service.sh` / `run-video-automation.sh` resolve under `MK04_CODE_ROOT` and select component Python from the dependency bundle / venv layout via `env.sh`.

**Workspace coupling:** historical `dev` / `prod` / `promote` tooling remains present as current operational reality for the old installation; Continuity Stage 2 does **not** treat labelled `dev`/`prod` pathing as the future architecture recommendation.

**Operator wrappers / metadata:** installation scripts exist under `deploy/scripts/`; unread root-only `mk04-operator-commands.meta` remains DEFERRED UNKNOWN (I4).

---

## 15. Dependency baseline

| Item | Value | Class |
| --- | --- | --- |
| Bundle path | `/opt/mk04/prod/dependency-bundles/716deb054e91ccfc44b9` | VERIFIED |
| Bundle hash / identity | `716deb054e91ccfc44b9` | VERIFIED |
| Python | 3.11.15 (system and bundle) | VERIFIED |
| Video Automation requirements entry | uses `requirements-dev.txt` (includes `-r requirements.txt` plus pytest pin) | VERIFIED |
| Byte-exact reproducible lock file | not present | VERIFIED |
| Preservation requirement | keep this existing bundle for old-installation reconstruction | VERIFIED |

**Identifying package versions (video-automation bundle unless noted):**

| Package | Version |
| --- | --- |
| torch | `2.8.0+cu128` |
| torchvision | `0.23.0` |
| torchaudio | `2.8.0` |
| whisperx | `3.8.6` |
| faster-whisper | `1.2.1` |
| Flask | `3.1.3` (all components) |
| yt-dlp | `2026.7.4` (source-input) |
| transformers | `4.57.6` |
| openai | `2.45.0` |
| numpy | `2.4.6` |

Full sanitized per-component inventories are in **Appendix B**. There is no substitute for preserving the on-disk bundle until Continuity supplies a locked rebuild path.

**System packages (partial):** FFmpeg 8.0.1-3ubuntu2; Ollama 0.30.11; NVIDIA driver 595.71.05 — VERIFIED.

---

## 16. Models and shared assets

| Asset | Fact | Class |
| --- | --- | --- |
| Ollama models present | `qwen2.5:14b-instruct` (9.0 GB); `Jotschi/dolphin-mistral-24b:24b-instruct-q5_0` (16 GB) | VERIFIED |
| Configured AI model | `qwen2.5:14b-instruct` | VERIFIED |
| Ollama store | `/usr/share/ollama/.ollama` ≈ 24G | VERIFIED |
| BlazeFace | path/hash/size in section 10 | VERIFIED |
| Operator HF cache | `~/.cache/huggingface` ≈ 1.5G | VERIFIED |
| Operator torch cache | `~/.cache/torch` ≈ 361M | VERIFIED |
| Operator whisper cache | `~/.cache/whisper` ≈ 73M | VERIFIED |

**Service access to operator caches:** MK04 units run as `mk04` with `ProtectHome=true`, so they cannot use `maguireltd` home caches. Local Ollama store must be preserved for AI behaviour. Full asset classification remains **Stage 3.2**.

---

## 17. Mutable state and protected material known so far

**Known prod roots**

| Root | Path |
| --- | --- |
| Config | `/etc/mk04/prod` |
| Runtime | `/var/lib/mk04/prod` |
| Logs | `/var/log/mk04/prod` |
| Code (live) | `/opt/mk04/prod/current` |

**Runtime trio (workspace copies used in publication proofs; also mirrored under runtime root):**

| File | SHA-256 at cut-off |
| --- | --- |
| `ops-ui/data/controls.json` | `2de776ad1ba8ffbf1969a65ba03482c8f256bbc69ec29f3aee3786c34b3b3449` |
| `ops-ui/data/ops_ui.sqlite3` | `dbfd3ff887dd7a1a879235e71a89c1f48ca1c9b6a2a277efa46b9c02e4d9953d` |
| `source-input/input_service/data/state/seen_urls.json` | `ab8b95670d1bbfdff24b6e7ed0798bc43dee5c933741462b2a4fef6ce3514821` |

**Credentials directory** `/var/lib/mk04/prod/credentials/` exists and is empty of files at cut-off; token/cookie/client-secret **paths** are configured via env (values not recorded).

Valuable `dev`-labelled history exists under logs/workspace but is **not classified** yet. Excluded workspace runtime/generated material remains physically present. **No cleanup or deletion authorization** was given in Stage 3.1.

Full authority selection of state belongs to **Stages 3.2–3.4**.

---

## 18. Reconstruction procedure for the current old installation

This reconstructs the **old current system**, not the future Continuity installer. **Not executed** during Stage 3.1.

Fail closed if: dependency bundle missing/corrupt; `current`/`previous` targets missing; `/etc/mk04/prod/env` missing or unreadable by `mk04`; unit files missing; Ollama store missing when AI selection is required; health checks fail after start.

1. **Preserve first.** Snapshot or otherwise retain `/opt/mk04/prod/releases/20260714T184302Z_62fdd82_dirty`, `/opt/mk04/prod/dependency-bundles/716deb054e91ccfc44b9`, `/etc/mk04/prod`, `/var/lib/mk04/prod`, `/var/log/mk04/prod`, and `/usr/share/ollama/.ollama` before any destructive change.
2. **Restore release directories** to the same paths; ensure ownership/group `maguireltd`/`mk04` patterns match the surviving release (do not invent new layouts).
3. **Restore pointers:** `current` → `…/20260714T184302Z_62fdd82_dirty`; `previous` → `…/20260714T153405Z_62fdd82_dirty`.
4. **Restore dependency bundle** at the exact hash path `716deb054e91ccfc44b9` and confirm `BUNDLE_COMPLETE` / `identity.json`.
5. **Restore units** under `/etc/systemd/system/mk04-*.service` to the installed hashes in section 6; `systemctl daemon-reload`.
6. **Restore protected env** to `/etc/mk04/prod/env` with mode `0640`, owner `root:mk04`, without printing contents. Confirm SHA-256 if a known-good copy exists.
7. **Restore state/log/config roots** and credential path targets referenced by env (without logging secret values).
8. **Preserve/restore Ollama store** and ensure `ollama.service` can start.
9. **Users/groups:** ensure `mk04` and `ollama` exist with prior uids/gids; operator in `mk04` group as required for admin access patterns already used on this host.
10. **Start order (illustrative):** `ollama` → `mk04-source-input` → `mk04-video-automation` → `mk04-output-funnel` → `mk04-ai-service` → `mk04-ops-ui` (respect unit After/Wants).
11. **Health commands** — see Appendix C.
12. **Operator wrappers:** restore `/usr/local/bin` mk04 operator command set when privileged metadata/scripts are available (I4 unread meta remains a gap).
13. **Do not enable** uploads, workers, or schedulers as part of reconstruction unless separately authorized.

---

## 19. Reconstruction procedure for the published source baseline

Safe, non-credentialed checkout of the published code baseline:

```bash
git clone --branch main --single-branch --no-tags \
  git@github-mk1-automated-clipping:maguirelimited/mk1-automated-clipping.git \
  mk1-baseline-check
cd mk1-baseline-check
git rev-parse HEAD   # expect 336946030f5165a910ff95e6a304bd6d5f2e753b
git rev-parse 'HEAD^{tree}'  # expect faa27e98b843766f48406c8f01c31ff2f25be9ed
git switch --detach 336946030f5165a910ff95e6a304bd6d5f2e753b
git status --porcelain  # expect empty
```

Verify controlling-document SHA-256 values from section 11 (for example `sha256sum` on each path).  

**Intentionally absent from this checkout:** dependency bundle, live state, credentials, Ollama models, and runtime databases. The checkout is source evidence, not a runnable production restore.

---

## 20. No-deployment evidence

Across publication of `4490ef174deebcb0f3edfb6d74bedf440b76823a` and `336946030f5165a910ff95e6a304bd6d5f2e753b`:

| Signal | Result |
| --- | --- |
| `current` / `previous` | unchanged at the dirty release pair in section 5 |
| Release inventory hash | unchanged `a093ba3a556d70387bec1895337232230515954bf5349ea33cc4047a0bf99194` |
| Five units enabled/active | unchanged |
| Health codes | unchanged (200/200/200/200/401) |
| Runtime trio hashes | unchanged at values in section 17 |
| Restarts / release activation / state initialization caused by Git push | none observed |
| Final Git `main` | `336946030f5165a910ff95e6a304bd6d5f2e753b` |

---

## 21. Known limitations and deferred work

| Item | Destination |
| --- | --- |
| Complete state inventory and authority classification | Stages 3.2–3.4 |
| Exact migration manifest | Stage 3.5 |
| Protected preservation set | Stage 3.6 |
| Restore test | Stage 3.7 |
| Stage 3 gate | Stage 3.8 |
| Inaccessible operator model caches for `mk04` | Stage 3.2+ asset planning |
| Unread operator-command metadata | privileged capture, then Continuity ops docs |
| Missing byte-exact dependency lock | Continuity packaging stages; until then preserve bundle |
| Off-machine disaster recovery | deferred by Stage 2 policy |
| Blank `OPENAI_API_KEY` intent | DEFERRED UNKNOWN |
| Upload / worker / scheduling enablement | not authorized |
| Physical redundant ZIP still on workspace | later cleanup authorization only |
| Stage 4 Continuity implementation | not authorized by Stage 3.1; Stages 3.2–3.8 remain pending first |
| Root crontab re-verification without sudo | optional privileged re-check |

---

## 22. Stage 3.1 gate checklist

| Criterion | Pass/Fail |
| --- | --- |
| Running release recorded | PASS |
| Dirty code reconciled | PASS |
| Clean baseline published | PASS |
| Clean remote checkout verified | PASS |
| Services/health recorded | PASS |
| Dependencies/machine requirements recorded | PASS |
| Launch/config bindings recorded | PASS |
| Old-installation reconstruction procedure recorded | PASS |
| Git push proven non-deploying | PASS |
| Live runtime/state unchanged by publication | PASS |

**Conclusion:** Stage 3.1 is **complete**. The final published code baseline is `336946030f5165a910ff95e6a304bd6d5f2e753b`. The currently deployed runtime remains `/opt/mk04/prod/releases/20260714T184302Z_62fdd82_dirty`. No later stage is implicitly authorized beyond proceeding to **Stage 3.2 evidence gathering**. Stage 4 is not authorized.

---

## Appendix A — Critical hashes and identities

| Identity | Value |
| --- | --- |
| Code baseline commit | `336946030f5165a910ff95e6a304bd6d5f2e753b` |
| Code baseline tree | `faa27e98b843766f48406c8f01c31ff2f25be9ed` |
| Hygiene parent / preservation commit | `4490ef174deebcb0f3edfb6d74bedf440b76823a` |
| Preservation tree | `db198d11986561ee353c3e87382b3a8728c541f3` |
| Pre-preservation base | `62fdd82433b8b0b08b4182739cba6c76a3b538a0` |
| Rejected local trailer commit (not on remote) | `82394585b87d9717d71b33dc5a71effc5650e9a4` |
| Deployed release id | `20260714T184302Z_62fdd82_dirty` |
| Dependency bundle hash | `716deb054e91ccfc44b9` |
| Physical ignored ZIP SHA-256 | `24af98d15700190113462664f733f1e84997cc8856bb0337a9729c8e19bd4e45` |
| `/etc/mk04/prod/env` SHA-256 | `15e24ae9d3d8ebdf266e8cfad4f3847cdde8368ed53c509f4af1d394ff0c726f` |
| Deploy-key fingerprint | `SHA256:1ikMbh5+R71P0Vi07CFX+6meYJaLIG47bKcbC+FBn7U` |

---

## Appendix B — Component package inventories

Sanitized `pip freeze` from bundle `716deb054e91ccfc44b9` at cut-off. VCS/URL references, if any, are replaced with `==<non-index-or-vcs-ref>` (none expected in these inventories). Package names and versions only.

### ai-service (18 packages)

- `attrs==26.1.0`
- `blinker==1.9.0`
- `certifi==2026.6.17`
- `charset-normalizer==3.4.9`
- `click==8.4.2`
- `Flask==3.1.3`
- `idna==3.18`
- `itsdangerous==2.2.0`
- `Jinja2==3.1.6`
- `jsonschema-specifications==2025.9.1`
- `jsonschema==4.26.0`
- `MarkupSafe==3.0.3`
- `referencing==0.37.0`
- `requests==2.34.2`
- `rpds-py==2026.6.3`
- `typing_extensions==4.16.0`
- `urllib3==2.7.0`
- `Werkzeug==3.1.8`

### ops-ui (8 packages)

- `blinker==1.9.0`
- `click==8.4.2`
- `Flask==3.1.3`
- `itsdangerous==2.2.0`
- `Jinja2==3.1.6`
- `MarkupSafe==3.0.3`
- `PyYAML==6.0.3`
- `Werkzeug==3.1.8`

### output-funnel (30 packages)

- `blinker==1.9.0`
- `certifi==2026.6.17`
- `cffi==2.1.0`
- `charset-normalizer==3.4.9`
- `click==8.4.2`
- `cryptography==49.0.0`
- `Flask==3.1.3`
- `google-api-core==2.31.0`
- `google-api-python-client==2.198.0`
- `google-auth-httplib2==0.4.0`
- `google-auth-oauthlib==1.4.0`
- `google-auth==2.56.0`
- `googleapis-common-protos==1.75.0`
- `httplib2==0.32.0`
- `idna==3.18`
- `itsdangerous==2.2.0`
- `Jinja2==3.1.6`
- `MarkupSafe==3.0.3`
- `oauthlib==3.3.1`
- `proto-plus==1.28.1`
- `protobuf==7.35.1`
- `pyasn1==0.6.4`
- `pyasn1_modules==0.4.2`
- `pycparser==3.0`
- `pyparsing==3.3.2`
- `requests-oauthlib==2.0.0`
- `requests==2.34.2`
- `uritemplate==4.2.0`
- `urllib3==2.7.0`
- `Werkzeug==3.1.8`

### source-input (8 packages)

- `blinker==1.9.0`
- `click==8.4.2`
- `Flask==3.1.3`
- `itsdangerous==2.2.0`
- `Jinja2==3.1.6`
- `MarkupSafe==3.0.3`
- `Werkzeug==3.1.8`
- `yt-dlp==2026.7.4`

### video-automation (134 packages)

- `aiohappyeyeballs==2.7.1`
- `aiohttp==3.14.1`
- `aiosignal==1.4.0`
- `alembic==1.18.5`
- `annotated-types==0.7.0`
- `antlr4-python3-runtime==4.9.3`
- `anyio==4.14.2`
- `asteroid-filterbanks==0.4.0`
- `attrs==26.1.0`
- `av==18.0.0`
- `blinker==1.9.0`
- `certifi==2026.6.17`
- `charset-normalizer==3.4.9`
- `click==8.4.2`
- `colorlog==6.10.1`
- `contourpy==1.3.3`
- `ctranslate2==4.8.1`
- `cycler==0.12.1`
- `defusedxml==0.7.1`
- `distro==1.9.0`
- `einops==0.8.2`
- `faster-whisper==1.2.1`
- `filelock==3.29.7`
- `Flask==3.1.3`
- `flatbuffers==25.12.19`
- `fonttools==4.63.0`
- `frozenlist==1.8.0`
- `fsspec==2026.6.0`
- `googleapis-common-protos==1.75.0`
- `greenlet==3.5.3`
- `grpcio==1.82.1`
- `h11==0.16.0`
- `hf-xet==1.5.1`
- `httpcore==1.0.9`
- `httpx==0.28.1`
- `huggingface_hub==0.36.2`
- `idna==3.18`
- `iniconfig==2.3.0`
- `itsdangerous==2.2.0`
- `Jinja2==3.1.6`
- `jiter==0.16.0`
- `joblib==1.5.3`
- `julius==0.2.8`
- `kiwisolver==1.5.0`
- `lightning-utilities==0.15.3`
- `lightning==2.6.5`
- `Mako==1.3.12`
- `markdown-it-py==4.2.0`
- `MarkupSafe==3.0.3`
- `matplotlib==3.11.0`
- `mdurl==0.1.2`
- `mpmath==1.3.0`
- `multidict==6.7.1`
- `narwhals==2.24.0`
- `networkx==3.6.1`
- `nltk==3.10.0`
- `numpy==2.4.6`
- `nvidia-cublas-cu12==12.8.4.1`
- `nvidia-cuda-cupti-cu12==12.8.90`
- `nvidia-cuda-nvrtc-cu12==12.8.93`
- `nvidia-cuda-runtime-cu12==12.8.90`
- `nvidia-cudnn-cu12==9.10.2.21`
- `nvidia-cufft-cu12==11.3.3.83`
- `nvidia-cufile-cu12==1.13.1.3`
- `nvidia-curand-cu12==10.3.9.90`
- `nvidia-cusolver-cu12==11.7.3.90`
- `nvidia-cusparse-cu12==12.5.8.93`
- `nvidia-cusparselt-cu12==0.7.1`
- `nvidia-nccl-cu12==2.27.3`
- `nvidia-nvjitlink-cu12==12.8.93`
- `nvidia-nvtx-cu12==12.8.90`
- `omegaconf==2.3.1`
- `onnxruntime==1.27.0`
- `openai==2.45.0`
- `opentelemetry-api==1.43.0`
- `opentelemetry-exporter-otlp-proto-common==1.43.0`
- `opentelemetry-exporter-otlp-proto-grpc==1.43.0`
- `opentelemetry-exporter-otlp-proto-http==1.43.0`
- `opentelemetry-exporter-otlp==1.43.0`
- `opentelemetry-proto==1.43.0`
- `opentelemetry-sdk==1.43.0`
- `opentelemetry-semantic-conventions==0.64b0`
- `optuna==4.9.0`
- `packaging==26.2`
- `pandas==3.0.3`
- `pillow==12.3.0`
- `pluggy==1.6.0`
- `primePy==1.3`
- `propcache==0.5.2`
- `protobuf==7.35.1`
- `pyannote-audio==4.0.7`
- `pyannote-core==6.0.1`
- `pyannote-database==6.1.1`
- `pyannote-metrics==4.1`
- `pyannote-pipeline==4.0.0`
- `pyannoteai-sdk==0.4.0`
- `pydantic==2.13.4`
- `pydantic_core==2.46.4`
- `Pygments==2.20.0`
- `pyparsing==3.3.2`
- `pytest==8.4.2`
- `python-dateutil==2.9.0.post0`
- `pytorch-lightning==2.6.5`
- `pytorch-metric-learning==2.9.0`
- `PyYAML==6.0.3`
- `regex==2026.7.10`
- `requests==2.34.2`
- `rich==15.0.0`
- `safetensors==0.8.0`
- `scikit-learn==1.9.0`
- `scipy==1.17.1`
- `six==1.17.0`
- `sniffio==1.3.1`
- `sortedcontainers==2.4.0`
- `SQLAlchemy==2.0.51`
- `sympy==1.14.0`
- `threadpoolctl==3.6.0`
- `tokenizers==0.22.2`
- `torch-audiomentations==0.12.0`
- `torch==2.8.0`
- `torch_pitch_shift==1.2.5`
- `torchaudio==2.8.0`
- `torchcodec==0.7.0`
- `torchmetrics==1.9.0`
- `torchvision==0.23.0`
- `tqdm==4.68.4`
- `transformers==4.57.6`
- `triton==3.4.0`
- `typing-inspection==0.4.2`
- `typing_extensions==4.16.0`
- `urllib3==2.7.0`
- `Werkzeug==3.1.8`
- `whisperx==3.8.6`
- `yarl==1.24.2`

---

## Appendix C — Exact health commands

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5060/healthz   # expect 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5050/healthz   # expect 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5055/healthz   # expect 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5075/health    # expect 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5070/health    # expect 401
```

---

## Appendix D — Evidence / prompt chronology (I1–M5D)

| Step | Focus |
| --- | --- |
| 3.1-I1 | Live runtime, safety, services, health |
| 3.1-I2 | Three-state provenance (workspace / live / Git) |
| 3.1-I3 | Semantic preservation boundary |
| 3.1-I4 | Reconstruction inputs; auth readiness; root-only gaps |
| 3.1-M1A–M1D | SSH deploy key; clean remote; token residue |
| 3.1-M2 | Document recovery; canonical master |
| 3.1-M3A–M3C | Classification manifests; ignore hygiene; rehearsal tree |
| 3.1-M4A–M4C | Stage/commit/publish preservation; replace trailer commit |
| 3.1-M5 | Clean remote checkout of preservation commit |
| 3.1-M5A | Document hash reconciliation; ZIP classification |
| 3.1-M5B–M5C | Normalize Stage 1/2 newlines; ignore+untrack ZIP; publish hygiene |
| 3.1-M5D | Final clean remote-checkout verification |
| 3.1-M6A | This record (create only; not yet committed) |

---

## Appendix E — Accepted deviations and resolution

| Deviation | Resolution |
| --- | --- |
| Unsafe remote credential material | Replaced with SSH deploy-key alias; token revoked (operator-attested) |
| Extra `.env.example` modifications beyond initial “100” | Explicitly included → `INCLUDE_CURRENT` 102 |
| Generated tracked artifacts in dirty tree | Removed via `REMOVE_FROM_CANDIDATE` / D=42 hygiene |
| Cursor `Co-authored-by` trailer on first commit object | Rejected; replaced via `commit-tree` (never pushed) |
| Stage 1/2 missing final newline vs canonical uploads | Normalized in hygiene commit only |
| Redundant Output Funnel ZIP in tree | Removed from Git; physical copy ignored; history retained |

---

*End of Stage 3.1 Current Working Baseline and Reconstruction Record.*
