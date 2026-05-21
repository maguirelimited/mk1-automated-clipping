# Migration Notes: Internal Job API

The clipping server no longer depends on n8n as part of core processing. n8n should only trigger source input (`POST /run-funnel` on port 5060). When input is ready, the input service automatically enqueues clipping on video-automation (`POST /jobs`).

## New Flow

1. n8n (or cron) calls `POST /run-funnel` with `{ "funnel_id": "..." }`.
2. On `input_ready`, the input service calls video-automation `POST /jobs` with `input_id` (no n8n clipping step).
3. The clipping API creates a job folder with `report.json` and `review.md`, returns `job_id`, and an internal worker runs transcription, selection, validation, clipping, and analytics.
4. Inspect progress via `GET /jobs/<job_id>` or job folders under `video-automation/jobs/`.
5. Clips land in `video-automation/output/`; metadata in `GET /jobs/<job_id>/outputs` and `/output/<clip_file>`.

## Example Requests

Upload and enqueue in one call:

```bash
curl -sS -X POST http://127.0.0.1:5050/jobs \
  -F "video_file=@/path/to/source.mp4" \
  -F 'selection={"max_clips":8,"min_duration_sec":30,"max_duration_sec":120}'
```

Process an existing file in `video-automation/input/`:

```bash
curl -sS -X POST http://127.0.0.1:5050/jobs \
  -H 'Content-Type: application/json' \
  -d '{"video":"source.mp4"}'
```

Poll and fetch outputs:

```bash
curl -sS http://127.0.0.1:5050/jobs/<job_id>
curl -sS http://127.0.0.1:5050/jobs/<job_id>/outputs
```

## Compatibility

`POST /process` and `POST /process-inline` are deprecated wrappers around `POST /jobs`. They return `202 Accepted` with job polling URLs and do not hold the HTTP request open during Whisper, LLM selection, or ffmpeg.

`POST /upload` remains as an optional helper, but callers can upload directly to `POST /jobs`.

n8n does not need to call `POST /jobs`, poll status, or receive webhooks. Manual/API access to `/jobs` remains for debugging and future tooling.

## Configuration

`pipeline_config.json` remains the local source of truth. The async worker defaults are:

```json
"async_worker": {
  "enabled": true,
  "max_concurrent_jobs": 1,
  "job_store_type": "json"
}
```

`job_store_type` is documented as `json`; SQLite is not required for the current single-process worker.
