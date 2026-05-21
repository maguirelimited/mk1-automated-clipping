# `selection` schema

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


## Notes for future fields

- New per-run knobs should be added to `_run_pipeline`'s unpack block (currently `app.py:402-414`) and documented here in the same table.
- If a knob also needs to influence the LLM prompt, add it to the subprocess JSON (`app.py:495-514`) and read it from `selection_options` inside `select_clip.py::_select_segments`.
- Do not bake niche-specific knobs (DOAC, business podcasts, etc.) into the schema. Reusability across niches is part of the long-term vision; per-niche behavior belongs in the input service, not the clipper.
