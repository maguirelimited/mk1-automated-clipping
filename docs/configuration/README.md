# Configuration

This document explains the config directory structure introduced in the Configuration & Deployment upgrade.

---

## Purpose

The `config/` directory is the central location for all system behaviour settings.

The goal:

> Any important system behaviour should be controlled through clear configuration,
> not hidden inside code, shell scripts, cron files, systemd units, or scattered constants.

Code provides mechanisms. Configuration decides how those mechanisms behave.

---

## Config types

There are five configuration categories. They live in separate subdirectories and are merged in order by ConfigManager (implemented in a later prompt).

### 1. Defaults (`config/defaults/`)

Shared settings that apply across all environments unless a later layer overrides them.

`default.yaml` contains conservative baseline values for selection, posting, and logging.

### 2. Environment config (`config/environments/`)

Describes where the system's state lives and whether uploading is enabled.

| File | Purpose |
|------|---------|
| `dev.yaml` | Development — all paths under `data/dev/`, uploading disabled |
| `prod.yaml` | Production — all paths under `data/prod/`, uploading enabled by default |

**Dev and production must never share state paths, databases, or logs.**

Environment config is the mechanism for that separation. Each file declares its own `paths.*` tree.

### 3. System config (`config/system/`)

Controls global operational behaviour: concurrency, health check intervals, retention policy baseline, disk pressure thresholds, AI model, and service restart policy.

`system.yaml` defines the conservative storage retention baseline (periods, disk pressure, allowed deletion roots, protected artifact types). Environment files may override `storage.*` after the system layer is merged. Retention days and disk pressure percentages must come from configuration — never as magic numbers in code. This phase is policy only; no retention engine or deletion behaviour is implemented yet.

### 4. Funnel config (`config/funnels/`)

Each funnel is a content niche with its own source channels, target platforms, and behaviour preset.

A new funnel is created by adding a new YAML file here, not by modifying core code.

Funnel config stores choices. It does not make business or creative decisions.

### 5. Platform config (`config/platforms/`)

Each platform defines upload/format constraints and upload state.

Platform-specific rules (title length, caption length, aspect ratio, posting windows) must not leak into processing or post-processing code.

### 6. Presets (`config/presets/`)

Presets are named behaviour profiles that control selection thresholds and posting rate.

A funnel declares which preset it uses:

```yaml
funnel:
  preset: growth
```

Available presets:

| Preset | Intent |
|--------|--------|
| `balanced` | Equal weight on quality and volume |
| `growth` | Higher volume, slightly relaxed thresholds |
| `maximum_quality` | Strict thresholds, lower volume |

Presets store behaviour choices. They do not choose funnels, make monetisation decisions, or change creative direction dynamically.

---

## Secrets

**Secrets must never appear in YAML config files.**

Secrets live in:

```text
deploy/env/dev/env.example     (template — fill locally, never commit)
deploy/env/prod/env.example    (template — fill in /etc/mk04/prod/env, never commit)
```

This includes: API keys, OAuth tokens, YouTube cookies, account credentials, bearer tokens, and private keys.

Git-controlled config files must be safe to share publicly.

---

## Dev and production separation

Dev and production use completely separate state:

| Resource | Dev | Production |
|----------|-----|------------|
| Data | `data/dev/` | `data/prod/` |
| Jobs | `jobs/dev/` | `jobs/prod/` |
| Outputs | `outputs/dev/` | `outputs/prod/` |
| Logs | `logs/dev/` | `logs/prod/` |
| Reports | `reports/dev/` | `reports/prod/` |
| Database | `database/dev.db` | `database/prod.db` |

A dev run must be incapable of writing to production paths.

---

## Upload safety

Dev uploads are always disabled in config:

```yaml
# config/environments/dev.yaml
uploading:
  enabled: false
```

Production uploads are enabled by default in config, but **config is not the final authority** in an emergency.

### Runtime kill switch

An emergency upload kill switch operates above config:

```bash
scripts/ops/disable-uploads.sh prod
```

This writes:

```text
data/prod/control_state.json
```

The pipeline checks this runtime control state before any real upload. It takes effect immediately, without requiring a Git commit or service restart.

> Config defines default upload behaviour.
> Runtime control state overrides config immediately.

Re-enabling uploads is also done through the operational scripts, not by editing config.

---

## ConfigManager

ConfigManager is not implemented yet. It will be added in a later prompt.

When implemented, ConfigManager will:

- Load config layers in order: defaults → system → environment → funnel → platform → preset
- Merge layers (later layers override earlier ones; environment may override system storage policy)
- Validate the resolved config
- Resolve environment-specific paths
- Expose a single typed config object
- Save a resolved config snapshot per job

Modules must not open YAML files directly. They should use ConfigManager once it exists.

---

## Config hierarchy (when ConfigManager is implemented)

```text
Defaults
    ↓
System
    ↓
Environment   (may override system storage retention policy)
    ↓
Funnel
    ↓
Platform
    ↓
Preset
    ↓
Local overrides (where allowed)
```

---

## State Path Table

| State type   | Dev path               | Prod path               |
|---|---|---|
| data root    | `data/dev`             | `data/prod`             |
| jobs         | `jobs/dev`             | `jobs/prod`             |
| outputs      | `outputs/dev`          | `outputs/prod`          |
| logs         | `logs/dev`             | `logs/prod`             |
| reports      | `reports/dev`          | `reports/prod`          |
| database     | `database/dev.db`      | `database/prod.db`      |
| clips        | `outputs/dev/clips`    | `outputs/prod/clips`    |
| transcripts  | `data/dev/transcripts` | `data/prod/transcripts` |
| cache        | `data/dev/cache`       | `data/prod/cache`       |

All paths are relative to the repo root. `ConfigManager` resolves them to absolute paths.

---

## EnvironmentStatePaths

`scripts/config/state_paths.py` provides a typed, immutable, environment-scoped state path object.

```python
from config_manager import ConfigManager

config = ConfigManager.load(environment="dev")
state  = config.state_paths   # EnvironmentStatePaths

state.jobs_root                         # absolute Path
state.job_dir("job_20260101T120000Z_x") # Path (validates job_id)
state.log_file("pipeline.log")          # Path (validates name)
state.is_within_environment(some_path)  # bool
state.assert_within_environment(path)   # returns resolved path or raises
state.ensure_directories()              # creates env-scoped dirs only
```

Safety contract:
- Dev state never overlaps prod state
- `assert_within_environment` rejects any path outside this environment's roots
- `job_dir` / `log_file` / `report_dir` / `output_dir` reject traversal, absolute paths, empty strings, and separator characters
- `ensure_directories()` must be called explicitly — `ConfigManager.load()` does NOT mutate the filesystem

---

## Upload state precedence

**Current implementation (Prompt 3 ambiguity — now resolved):**

Config-level upload state follows this precedence:

```
environment uploading.enabled     ← highest priority at config level
    overrides
platform uploading.enabled
    overrides
defaults uploading.enabled
```

This means: `platform uploading.enabled: false` does **not** block uploads when the environment says `uploading.enabled: true`. Platform upload state is a lower-priority default, not a veto.

**Final intended precedence once Remote Administration is implemented:**

```
runtime kill switch (data/<env>/control_state.json)   [NOT YET IMPLEMENTED]
    >
environment uploading.enabled
    >
platform uploading.enabled
```

Do not add logic that allows `platform uploading.enabled: false` to override the environment layer unless a future plan explicitly inverts this precedence.

---

## ConfigManager

`scripts/config/config_manager.py` implements the single normal interface for loading config.

```python
from config_manager import ConfigManager

resolved = ConfigManager.load(
    environment="dev",         # or "prod"; defaults to MK04_ENV or "development"
    funnel_id="business",
    platform_id="youtube",
    preset_id=None,            # defaults to funnel.preset
)

resolved.environment          # "development" | "production"
resolved.uploading_enabled    # bool (env config takes precedence over platform)
resolved.get("selection.max_clips")  # dot-notation access
resolved.paths.jobs_root      # absolute Path
resolved.save_snapshot(job_dir)      # writes resolved_config.yaml
```

Run with `video-automation/.venv/bin/python` (has PyYAML 6.0).

Upload effective state precedence:

```
runtime kill switch (data/<env>/control_state.json)   [NOT YET IMPLEMENTED]
    >
environment uploading.enabled                         ← current highest priority
    >
platform uploading.enabled                            ← does NOT veto environment
```

See "Upload state precedence" section above for full explanation.

CLI helper:

```bash
video-automation/.venv/bin/python scripts/config/config_manager.py \
  --env dev --funnel business --platform youtube --print-summary
```

---

## ExecutionContext

`scripts/config/execution_context.py` provides a stable, immutable provenance record attached to every job.

Every job directory contains two artifacts:

```
<job_dir>/
  resolved_config.yaml      — full merged config snapshot (from ConfigManager)
  execution_context.json    — small JSON provenance record (from ExecutionContext)
```

`execution_context.json` fields:

| Field | Value |
|---|---|
| `schema` | `"execution_context_v1"` |
| `environment` | `"development"` \| `"production"` |
| `job_id` | Validated job identifier |
| `funnel_id` | Active funnel config ID |
| `platform_id` | Active platform config ID |
| `preset_id` | Active preset config ID |
| `config_version` | Version from `defaults/default.yaml` |
| `resolved_config_path` | Path to `resolved_config.yaml` for this job |
| `code_commit` | Short Git SHA or `null` if Git unavailable |

Does NOT contain: secrets, tokens, passwords, full config dump, or business decisions.

Usage:

```python
from execution_context import ExecutionContext

config = ConfigManager.load(environment="dev")
state  = config.state_paths
snap   = config.save_snapshot(state.job_dir(job_id))
ctx    = ExecutionContext.from_resolved_config(config, job_id=job_id, resolved_config_path=snap)
ctx.save(state.job_dir(job_id))   # writes execution_context.json
```

Code commit detection: uses `git rev-parse --short HEAD`. Falls back to `null` if Git is unavailable — job creation never fails due to missing Git.

Environment: selected from `MK04_ENV` env var (defaults to `"development"`) when not explicitly passed. Explicit startup `--env` flag comes in the Remote Administration prompt.

---

## Report and task integration

The following artifacts now include `execution_context`:

| Artifact | Field added |
|---|---|
| `report.json` (queued job) | `"execution_context": {...}` |
| `task.json` (job task) | `"execution_context": {...}` |
| `post_processing_report.json` | `"execution_context": {...}` (optional, defaults to `{}`) |

Pass `execution_context=ctx.to_dict()` to `build_post_processing_report()` to populate it.

---

## Adding a new funnel

1. Create `config/funnels/<funnel_id>.yaml`
2. Declare its `id`, `preset`, target `platforms`, and any source rules
3. No core code changes should be required

## Adding a new platform

1. Create `config/platforms/<platform_id>.yaml`
2. Define format constraints and upload state
3. Platform-specific rules must not leak into processing or post-processing code

---

## Config-driven behaviour (Prompt 6A)

The following behavioural values are now sourced from the resolved config snapshot
(`<job_dir>/resolved_config.yaml`) written at job-creation time.

### What is config-driven

| Behaviour | Config key | Resolved source |
|---|---|---|
| Max clips selected | `selection.max_clips` | `resolved_config.yaml` → `selection.max_clips` |
| Selection mode | `selection.mode` | `resolved_config.yaml` → `selection.mode` |
| Min overall potential threshold | `selection.min_overall_potential` | `resolved_config.yaml` → `selection.min_overall_potential` |
| Min confidence threshold | `selection.min_confidence` | `resolved_config.yaml` → `selection.min_confidence` |
| Exploration ratio (prepared, not yet active) | `selection.exploration_ratio` | `resolved_config.yaml` → `selection.exploration_ratio` |
| Post-processing conveyor module list | `post_processing.conveyor` | `resolved_config.yaml` → `post_processing.conveyor` |
| Platform max duration (informational) | `format.max_duration_seconds` | `resolved_config.yaml` → `format.max_duration_seconds` |
| Platform title max length (informational) | `format.title_max_length` | `resolved_config.yaml` → `format.title_max_length` |
| Platform caption max length (informational) | `format.caption_max_length` | `resolved_config.yaml` → `format.caption_max_length` |
| Platform aspect ratio (informational) | `format.aspect_ratio` | `resolved_config.yaml` → `format.aspect_ratio` |

**Source priority inside a job:**

```
resolved_config.yaml saved in the job directory   ← preferred
    ↓
mk1_settings defaults (env vars, controls.json)   ← legacy / new-job fallback
    ↓
hardcoded defaults in selection_gate_v1.py        ← legacy jobs only
```

**Backward compatibility:**
- Legacy jobs without `resolved_config.yaml` continue to use `mk1_settings` defaults unchanged.
- New jobs using the `balanced` preset produce identical behaviour because the YAML values match the legacy defaults.

### Mismatch documentation

| Field | Config value | Legacy default | Status |
|---|---|---|---|
| `maximum_quality.max_clips` | 3 | 3 (selection_gate_v1) | aligned in 6A |
| `maximum_quality.min_overall_potential` | 8.5 | 8.5 (selection_gate_v1) | aligned in 6A |
| `growth.max_clips` | 8 | 8 (selection_gate_v1) | aligned in 6A |
| `growth.min_confidence` | 0.5 | 0.5 (selection_gate_v1) | aligned in 6A |
| `format.max_duration_seconds` | 60 (platform upload limit) | 120 (selection filter) | **different concepts** — not merged |

Note: `platform.format.max_duration_seconds` (60 s) is the platform upload limit.
`selection.max_duration_sec` (120 s) is the candidate clip length filter.
These are different settings and must not be conflated.

### What is NOT wired yet

| Behaviour | Reason deferred |
|---|---|
| Real upload enable/disable | Requires runtime kill switch (Prompt 7+) |
| Runtime upload kill switch | Requires safe override layer |
| Storage retention / deletion | Policy config complete; engine/deletion deferred |
| Scheduler controls | Requires Remote Administration prompt |
| Broad platform adapter behaviour | Requires platform adapter layer |
| `selection.exploration_ratio` active use | `selection_gate_v1.py` does not yet consume it |
| `format.title_max_length` / `caption_max_length` enforcement | Not consumed in processing modules yet |

### Helper: load_resolved_config_for_job

```python
from execution_context import load_resolved_config_for_job

cfg = load_resolved_config_for_job(job_dir)
# cfg is a plain dict or None for legacy jobs

if cfg:
    max_clips = cfg["selection"]["max_clips"]
    conveyor  = cfg["post_processing"]["conveyor"]
```

Rules:
- Returns `dict` when `resolved_config.yaml` exists and is valid YAML mapping.
- Returns `None` for legacy jobs without the file (no crash).
- Raises `ResolvedConfigLoadError` when the file is present but malformed, unreadable, or not a mapping.
- Never mutates the filesystem.
- Never reads secrets.

**Failure behaviour (Prompt 6A.5):**

| Condition | Behaviour |
|---|---|
| `resolved_config.yaml` missing | Returns `None` — legacy fallback allowed |
| File present but invalid YAML | Raises `ResolvedConfigLoadError` — job fails |
| File present but not a dict (list/scalar/null) | Raises `ResolvedConfigLoadError` — job fails |
| File present but unreadable | Raises `ResolvedConfigLoadError` — job fails |

In the pipeline (`app.py`), a broken snapshot is a hard failure: the job report is marked `failed`, a structured `configuration_error` is recorded, and processing does not continue with legacy defaults.

---

## Prompt 6B: Config-driven platform formatting and captions

Platform formatting and caption layout values are now sourced from the resolved
config snapshot and passed into post-processing modules via `conveyor_config`.

### Config flow

```
resolved_config.yaml (job directory)
    ↓
extract_conveyor_config_from_resolved()
    ↓
app.py merges over processing_settings defaults
    ↓
post_processing_mk1 → run_fixed_mk1_universal_conveyor
    ↓
make_module_context(config=...) → context["config"]
    ↓
platform_safe_format_v1 / intelligent_captions_v1 merge with _DEFAULT_CONFIG
```

Legacy jobs without `resolved_config.yaml` continue using `processing_settings`
defaults and module `_DEFAULT_CONFIG` fallbacks unchanged.

### Config-driven values (Prompt 6B)

| Behaviour | Config key | Module key | Consumer |
|---|---|---|---|
| Output width | `format.width` | `target_width` | `platform_safe_format_v1` |
| Output height | `format.height` | `target_height` | `platform_safe_format_v1` |
| Aspect ratio (metadata) | `format.aspect_ratio` | `platform_aspect_ratio` | carried in conveyor config |
| Platform max duration | `format.max_duration_seconds` | `platform_max_duration_seconds` | carried (not selection filter) |
| Title max length | `format.title_max_length` | `platform_title_max_length` | carried (no new truncation) |
| Caption max length | `format.caption_max_length` | `platform_caption_max_length` | carried (no new truncation) |
| Caption safe-zone top | `captions.safe_zone.top_px` | `safe_zone_top_px` | both format + caption modules |
| Caption safe-zone bottom | `captions.safe_zone.bottom_px` | `safe_zone_bottom_px` | both format + caption modules |
| Caption safe-zone left | `captions.safe_zone.left_px` | `safe_zone_left_px` | both format + caption modules |
| Caption safe-zone right | `captions.safe_zone.right_px` | `safe_zone_right_px` | both format + caption modules |
| Caption font family | `captions.layout.font_family` | `font_family` | `intelligent_captions_v1` |
| Caption font size | `captions.layout.font_size` | `font_size` | `intelligent_captions_v1` |
| Caption max lines | `captions.layout.max_lines` | `max_lines` | `intelligent_captions_v1` |
| Chars per line | `captions.layout.max_chars_per_line` | `max_chars_per_line` | `intelligent_captions_v1` |
| Chars per caption | `captions.layout.max_chars_per_caption` | `max_chars_per_caption` | `intelligent_captions_v1` |

Default YAML values match existing module `_DEFAULT_CONFIG` exactly — no visual
output change for the balanced/growth presets at current settings.

### Duration separation (unchanged)

| Setting | Meaning | Location |
|---|---|---|
| `selection.max_duration_sec` | Candidate clip length filter (120 s legacy default) | selection gate / processing_settings |
| `format.max_duration_seconds` | Platform upload/export limit (60 s for YouTube) | platform config |

These are **not merged**. Prompt 6B does not connect platform duration to
rendering or selection filtering.

### Still in code (technical invariants)

- FFmpeg codec names (`libx264`, `aac`)
- FFmpeg preset (`veryfast`)
- Background blur filter string (`20:1`)
- Module IDs and file extensions
- Caption timing bounds (`min_caption_duration_sec`, `max_caption_duration_sec`)
- Duration tolerance for ffmpeg output checks

### Not wired yet

| Behaviour | Reason |
|---|---|
| `selection.exploration_ratio` active use | Not consumed by selection_gate_v1 |
| Real upload enable/disable | Deferred to upload controls prompt |
| Runtime upload kill switch | Deferred |
| Storage retention/deletion | Policy config complete; engine deferred |
| Title/caption length enforcement in modules | No central truncation exists today |
| Platform duration enforcement in render | format module does not trim by duration today |

### Helper: extract_conveyor_config_from_resolved

```python
from execution_context import extract_conveyor_config_from_resolved

overrides = extract_conveyor_config_from_resolved(resolved_cfg_dict)
conveyor_config.update(overrides)
```

---

## Operations UI environment awareness (Prompt 7)

The Operations UI (`ops-ui/`) displays a persistent read-only environment banner on
every page and a config summary on the dashboard.

Data source: `ops_ui/environment_summary.py` → `ConfigManager.load()` +
`EnvironmentStatePaths` (via `ResolvedConfig.paths`).

Read-only API endpoints:

```text
GET /api/environment
GET /api/config-summary
```

Environment selection uses `MK04_ENV` (via `ops_ui.config.load_settings()`), mapped
to ConfigManager's `development` / `production`. Invalid values show a clear error
state — production never appears unless explicitly selected.

Posting state wording:

```text
Posting config: enabled|disabled
Runtime upload kill switch: not implemented yet
```

No mutating controls, upload execution changes, or config editing were added in
this prompt.

---

## System update and startup (Prompt 8)

Repo-root scripts provide explicit environment selection for updates and startup:

```bash
./update.sh dev
./update.sh prod
./run.sh --env dev
./run.sh --env prod
./run.sh --env dev --check-only
```

See [Operations Runbook](../operations/RUNBOOK.md) for full behaviour, flags, and
honesty rules for service restart and health checks.

### Full smoke test (Prompt 9)

```bash
python scripts/smoke/smoke_config_deployment.py --env dev
python scripts/smoke/smoke_config_deployment.py --env prod
python scripts/smoke/smoke_config_deployment.py --both
pytest tests/smoke
```

Default (no `--env`) runs **dev only**.

### Configuration & Deployment completion checklist

| Item | Status |
|------|--------|
| Config directory + YAML layers | **Complete** |
| Schema validator + ConfigManager | **Complete** |
| EnvironmentStatePaths (dev/prod separation) | **Complete** |
| ExecutionContext + resolved config snapshots | **Complete** |
| Config-driven selection/conveyor/format/caption | **Complete** |
| Strict malformed resolved_config failure | **Complete** |
| Ops UI environment awareness | **Complete** |
| update.sh / run.sh wrappers | **Complete** |
| last_update_status.json | **Complete** |
| Full environment-aware smoke test | **Complete** |
| Legacy pipeline_config.json removal | **Deferred** |
| Runtime upload kill switch | **Deferred** (Remote Administration) |
| Full HTTP service health | **Partially complete** |
| Storage retention policy config | **Complete** (engine/deletion deferred) |
| Storage artifact classification | **Complete** (metadata only; planner deferred) |
| Scheduler controls | **Deferred** |
| Output-funnel execution context | **Deferred** |

**Subsystem validation phase: complete.** Deferred items are handoffs to later plans.

### Dual config roots

| System | Location | Role |
|--------|----------|------|
| **New (authoritative)** | Repo `config/` | YAML merged by ConfigManager; used by update/run scripts and execution context |
| **Legacy** | `MK04_CONFIG_ROOT` / `/etc/mk04/{env}/pipeline_config.json` | Older pipeline settings; not removed in this phase |

Configuration & Deployment work should use repo `config/` as the source of truth.

### Last update status

`./update.sh` writes `data/<env>/last_update_status.json` (via ConfigManager state
paths). The Operations UI dashboard displays status when the file exists.
