# Migration Notes: Internal Job API

The clipping server no longer depends on n8n as part of core processing. External automation can still call the HTTP API, but the pipeline now owns job creation, status, artifacts, and failure evidence.

## New Flow

1. Send a source video to `POST /jobs`.
2. The API immediately creates a job folder with `report.json` and `review.md`, returns `job_id`, `status`, `status_url`, and `outputs_url`.
3. An internal worker processes one job at a time by default: transcription, AI clip selection, timestamp validation/repair, ffmpeg clipping, analytics, and artifact writes.
4. Poll `GET /jobs/<job_id>` for `queued`, `running`, `success`, or `failed`.
5. Fetch completed clip metadata from `GET /jobs/<job_id>/outputs`; clip files remain available through `/output/<clip_file>`.

## Example Requests

Upload and enqueue in one call:

```bash
curl -sS -X POST http://127.0.0.1:5050/jobs \
  -F "video_file=@/path/to/source.mp4" \
  -F 'selection={"max_clips":3,"min_duration_sec":30,"max_duration_sec":120}'
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

Automation platforms, including n8n, should call `POST /jobs`, poll `status_url`, and retrieve outputs from `outputs_url`. No direct webhook delivery is part of the core pipeline.

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
