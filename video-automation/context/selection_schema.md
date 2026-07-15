# `selection` schema

**Selection upgrade guardrails:** MK1-first formalisation route, stage
boundaries, and no-touch areas are documented in
[`system-context/selection-upgrade/architecture-guardrails.md`](../../system-context/selection-upgrade/architecture-guardrails.md).

**MK1 canonical candidate schema:** field definitions, stage ownership, and
validation rules are documented in
[`mk1_candidate_schema.md`](mk1_candidate_schema.md).

The `selection` object on the `/jobs` request body is the per-run knob channel for the clipper. `pipeline_config.json` is the local default source of truth; request values override it for that job.

This document lists every field that `_run_pipeline` (`server/app.py`) currently reads from `selection_policy`, including types, defaults, and downstream effect.

## Fields read by `_run_pipeline`

`_run_pipeline` lives at `server/app.py:395` and unpacks `selection_policy` at lines 403-414.

| Field | Type | Default | Read at | Effect |
|---|---|---|---|---|
| `min_duration_sec` | number (seconds) | `pipeline_config.json` → `selection.min_clip_duration_sec` (currently `30`); hardcoded fallback `5` | `app.py:404-406` | Lower bound on clip length. Forwarded to `select_clip.py` (subprocess JSON) and to `validate_and_repair_selection`. |
| `max_duration_sec` | number (seconds) | `pipeline_config.json` → `selection.max_clip_duration_sec` (currently `60`); hardcoded fallback `30` | `app.py:407-409` | Upper bound on clip length. Forwarded to `select_clip.py` and to `validate_and_repair_selection`. |
| `max_overlap_sec` | number (seconds) | `pipeline_config.json` → `selection.max_overlap_sec` (currently `2`); hardcoded fallback `2` | `app.py:410-412` | Maximum permitted overlap between adjacent selected clips before deduping in `postprocess_segments`. |
| `max_clips` | integer | `5` (hardcoded) | `app.py:403` | Maximum selected clips to request and postprocess. |
| `include_reasons` | boolean | `false` | `app.py:413` | When `true` (and `include_clip_metadata` is `false`), each entry in the response `clips[]` includes the model's `reason` string. |
| `include_clip_metadata` | boolean | `true` | `app.py:414` | When `true`, each entry in the response `clips[]` includes the full metadata bundle: `title`, `hook`, `caption`, `scores`, `composite_score`, `reason`. Wins over `include_reasons` when both are set. |

### Implicitly threaded (not from the request body)

These are not read from `selection`, but are forwarded into the selector subprocess in the same JSON arg (`app.py:495-514`) for completeness:

| Field | Source | Why it's there |
|---|---|---|
| `video_duration_sec` | ffprobe on the input video (`app.py:434`) | Hard upper bound for the model's timestamps and for `postprocess_segments` (`pipeline_utils.py:278, 296-297`). Never sourced from `selection_policy`. |

## Field defaults vs. config

Defaults flow in this order (first non-null wins):

1. `selection_policy[<field>]` — the value sent on this request.
2. `pipeline_config.json` → `selection.<field>` — repo-level default.
3. Hardcoded fallback in `_run_pipeline` — last resort if the config is missing the key.

`pipeline_config.json` should be treated as default-only. If a value needs to vary per run, set it in the request `selection` payload.

## API example

Minimal request body sent to `POST /jobs` after the source file has already been copied or moved into the configured `input/` folder:

```json
{
  "video": "doac_episode_2024_07_15.mp4",
  "selection": {
    "min_duration_sec": 30,
    "max_duration_sec": 60,
    "max_overlap_sec": 2,
    "max_clips": 3,
    "include_clip_metadata": true,
    "include_reasons": false
  }
}
```

`/process` and `/process-inline` are deprecated compatibility wrappers around `/jobs`. `POST /jobs` also accepts multipart `video_file` directly.


## Clip-selection backend (`ai-service` integration)

Clip selection has two judgement backends. The default is the local
`ai-service` path (Ollama-backed clip judgement via `POST /ai/run`). The legacy
`openai` backend uses the inline OpenAI call in `scripts/select_clip.py`.

```text
selection_backend = "ai_service"  (default) -> POST /ai/run task_type=clip_selection
selection_backend = "openai"                  -> inline OpenAI call
```

Resolution order (first definite value wins):

1. `selection.selection_backend` on the request body (per-run override).
2. Ops UI saved setting in the shared `controls.json` (`ai_config.clip_selection_backend`).
3. `CLIP_SELECTION_BACKEND` environment variable.
4. Default `ai_service`.

This is implemented in `scripts/ai_settings.py` (`resolve_clip_selection_backend`)
and consumed by `scripts/select_clip.py`. The operator normally chooses the
backend from the Ops UI **Settings → Local AI & clip selection** page; manual
`.env` editing is only a fallback. video-automation reads the same shared file
the Ops UI writes — it never calls the Ops UI over HTTP for config.

### Configuration sources

The Ops UI persists these under `ai_config` in `controls.json`. Each value
resolves UI saved value → environment variable → built-in default.

| Setting (UI) | Env var fallback | Default | Effect |
|---|---|---|---|
| `clip_selection_backend` | `CLIP_SELECTION_BACKEND` | `ai_service` | `openai` uses the legacy inline OpenAI path; `ai_service` routes clip selection to the local `ai-service`. |
| `ai_service_url` | `AI_SERVICE_URL` | `http://127.0.0.1:5075` | Base URL of the local `ai-service`. |
| `ai_service_timeout_seconds` | `AI_SERVICE_TIMEOUT_SECONDS` | `180` | Per-request timeout for the `/ai/run` call. |

The model-level settings (`ai_provider`, `ai_model`, `ai_base_url`,
`ai_timeout_seconds`, `ai_temperature`, `ai_top_p`, `ai_max_tokens`) are also
saved by the Ops UI and consumed by `ai-service` with the same precedence.

If the shared file is missing or invalid, resolution falls back cleanly to the
environment variable and then the default, so env-only setups keep working.

MK1 has **no cloud fallback**: when `ai_service` is selected and the AI service
fails, `video-automation` does not silently fall back to OpenAI and does not
fabricate clip candidates.

## MK1 section candidate discovery (processing pipeline)

When `processing_pipeline_mode=mk1`, section-level raw candidate discovery is
an AI-backed processing step separate from legacy clip selection above.

**Prompt ownership:** `ai-service` builds the model prompt (base instructions,
funnel-specific judgement rules, and request context) inside
`POST /ai/run` for `task_type=section_candidate_discovery`. video-automation
does not construct prompts in production.

**video-automation responsibilities:** orchestration via
`scripts/section_candidate_discovery.py` — HTTP client
(`AiServiceSectionDiscoveryClient`), batching, candidate caps, boundary sanity,
overlap deduplication, artifact writing, and local result validation.

```text
run_processing_pipeline()
  -> AiServiceSectionDiscoveryClient.discover_section(section, config)
  -> POST /ai/run  (structured input only; no prompt text from VA)
  -> ai-service prompt builder + model + schema validation + semantic normalisation
  -> video-automation post-processing on structured result
```

Tests should simulate ai-service responses with structured fake
`discover_section()` clients, not local prompt builders.

### How `clip_selection` calls `ai-service`

`scripts/ai_service_client.py` builds the `/ai/run` request envelope:

```text
task_type      = clip_selection
job_id         = job id (job truth stays in video-automation)
funnel_id      = forwarded when available
input          = transcript text, timed segments, duration, funnel_rules, chunking_options
prompt_version = clip_selection_v2
schema_version = clip_candidates_v2
```

`ai-service` only judges. It does not write `video-automation` state, decide the
overall job status, or own retries.

### Result handling

The client classifies every reply into one explicit outcome, mapped onto the
existing selector subprocess error contract so the current job/report/error
handling stays in control:

| `ai-service` reply | Client outcome | `select_clip.py` behaviour |
|---|---|---|
| HTTP 200 `usable=true` with candidates | `usable` | Map candidates → segments → `postprocess_segments`; continue pipeline. |
| HTTP 200 `usable=false` (or no candidates) | `no_clip` | Raise `SELECTOR_REJECTED_AFTER_POSTFILTER` → HTTP 422 controlled no-clip; never force a bad clip. |
| HTTP 503 `AI_BUSY` | `busy` (retryable) | Raise `AI_SERVICE_BUSY` → HTTP 503; retried later via the existing job/retry mechanism. |
| 4xx/5xx, `MODEL_CALL_FAILED`, `MODEL_OUTPUT_INVALID`, non-JSON, connection refused, timeout | `ai_failure` | Raise `AI_SERVICE_FAILED …` → HTTP 500 controlled AI failure; logs/report preserved for debugging. |

`AI_BUSY` is retryable because `ai-service` runs one heavy local-model task at a
time and never queues internally. `usable=false` is a valid "no good clip"
judgement, not a crash.

## GPU phase control (local backend only)

WhisperX transcription and the local LLM (Qwen via Ollama) are both heavy GPU
phases. On a ~12GB card the 14B model can occupy ~9.8GB resident, leaving too
little VRAM for WhisperX `medium`, which then fails with CUDA out-of-memory.
This is a GPU resource-sequencing problem, not a clip-selection logic bug.

MK1 makes the two phases explicit and sequential. The orchestrator
(`server/app.py::_run_pipeline`) calls into `scripts/gpu_phase_control.py`:

```text
before WhisperX transcription:
    prepare_gpu_for_transcription()   # ask Ollama to release the model
run WhisperX transcription
before local clip selection:
    allow_ai_service_selection()      # log marker; Ollama reloads lazily
```

`prepare_gpu_for_transcription()` is **backend-aware** and only acts when the
resolved clip-selection backend is `ai_service`. It uses Ollama's supported
`keep_alive=0` unload (a `POST /api/generate` with `{"keep_alive": 0}`) — the
least disruptive option. It never kills the Ollama process, never kills GPU
processes, never restarts the stack, and never switches backends.

It is safe in every degraded case (no crash, controlled log/warning):

```text
backend == openai            -> no action
Ollama not installed/running -> "nothing to release", continue
nvidia-smi missing/driver down -> skip GPU numbers, continue
WhisperX on CPU              -> skip release
unload fails / VRAM stays low -> warn + recommend small/tiny or CPU WhisperX
phase control disabled        -> no action
```

When `nvidia-smi` is available it captures `used / total / free` VRAM before and
after, and a simple compute-process list, into the job report
(`gpu_phase_transcription`). A pressure warning is appended to the job
`warnings` (it does **not** auto-downgrade the WhisperX model).

Two layers reduce VRAM pressure:

1. **Proactive release before transcription** (`prepare_gpu_for_transcription`).
2. **Bounded model keep-alive** in `ai-service` (`AI_KEEP_ALIVE`, default `5m`),
   so the model is evicted after the idle window instead of being pinned in
   VRAM forever.

### Config

| Setting (UI) | Env var | Default | Effect |
|---|---|---|---|
| `local_ai_gpu_phase_control_enabled` | `LOCAL_AI_GPU_PHASE_CONTROL_ENABLED` | `true` | When on, release the local model before WhisperX (ai_service only). |
| `local_ai_warn_on_gpu_pressure` | `LOCAL_AI_WARN_ON_GPU_PRESSURE` | `true` | Warn + recommend a smaller/CPU WhisperX model under VRAM pressure. |
| `ai_keep_alive` | `AI_KEEP_ALIVE` | `5m` | How long Ollama keeps the model resident after a call (ai-service side). |

Each resolves UI saved value → env var → default, like the other AI settings.

### Honest limitation

`keep_alive=0` unload is the safest Ollama-supported release, but it is
**advisory**: another process or a concurrent `ai-service` request can re-load
the model, and unload timing is not instantaneous. The controller waits briefly
and re-checks VRAM, but it does not block forever or guarantee a free GPU. If
free VRAM stays below what the selected WhisperX model needs, it warns and
recommends `WHISPERX_MODEL=small`/`tiny` or `WHISPERX_DEVICE=cpu`; it never
silently downgrades the model. Verify on a real GPU box with
`python3 video-automation/scripts/smoke_gpu_phase_control.py`.

## Notes for future fields

- New per-run knobs should be added to `_run_pipeline`'s unpack block (currently `app.py:402-414`) and documented here in the same table.
- If a knob also needs to influence the LLM prompt, add it to the subprocess JSON (`app.py:495-514`) and read it from `selection_options` inside `select_clip.py::_select_segments`.
- Do not bake niche-specific knobs (DOAC, business podcasts, etc.) into the schema. Reusability across niches is part of the long-term vision; per-niche behavior belongs in the input service, not the clipper.
