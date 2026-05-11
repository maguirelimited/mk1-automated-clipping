# mk0.4 Deployment Runbook

This directory contains the portable setup path for running the mk0.4 services
outside the original development machine.

## Services

- **source-input** (`127.0.0.1:5060` by default): fetches one ready long-form
  input for a configured funnel.
- **video-automation** (`0.0.0.0:5050` by default): transcribes, selects,
  validates, clips, and writes analytics events.

Both services are intentionally independent. n8n or another scheduler should
call source-input first, then hand the ready file to video-automation.

## Host Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` on `PATH`
- `whisper` CLI on `PATH` for video automation
- Network access to YouTube/yt-dlp sources and OpenAI
- `OPENAI_API_KEY` set for video automation

## First-Time Setup

```bash
cd automated-content/VAmk0.4
./deploy/scripts/bootstrap.sh
```

Then edit:

- `source-input/input_service/.env`
- `video-automation/.env`

At minimum set `OPENAI_API_KEY` in `video-automation/.env`. Keep
`PIPELINE_CONFIG_PATH`, `VIDEO_PIPELINE_PROFILES_PATH`, and
`INPUT_SERVICE_ROOT` pinned in production so cloned machines do not depend on
current working directories.

## Run Locally

Terminal 1:

```bash
./deploy/scripts/run-input-service.sh
```

Terminal 2:

```bash
./deploy/scripts/run-video-automation.sh
```

## Readiness Checks

With both services running:

```bash
./deploy/scripts/doctor.sh
```

Or call endpoints directly:

- `GET http://127.0.0.1:5060/doctor`
- `GET http://127.0.0.1:5050/doctor`

## Production Process Managers

For systemd, supervisord, launchd, Docker, or Kubernetes, use the run scripts as
the command entrypoints or translate their env exports into the process manager.
The important contract is:

- set env from the relevant `.env`
- run from the service root
- keep `ffmpeg`, `ffprobe`, and `whisper` available on `PATH`
- keep `video-automation/input`, `output`, `temp`, `jobs`, and `analytics`
  persistent if you need auditability after restarts
- keep `source-input/input_service/data/state/seen_urls.json` persistent so
  duplicate tracking survives restarts

## Normal Orchestration

1. `POST /run-funnel` to source-input with `{ "funnel_id": "..." }`.
2. If `status == "input_ready"`, copy/move the returned `video_path` into
   `video-automation/input/`.
3. `POST /process` to video-automation with the basename and, if needed,
   `pipeline_profile` from source-input. Avoid sending `selection` overrides
   unless it is an explicit runtime exception.
4. Fetch clips from `/output/<clip_file>`.
5. Post platform performance back to `POST /analytics/feedback` with `job_id`,
   `clip_id`, `platform`, `posted_url`, and `metrics`.

## Persistent Artifacts

- `source-input/input_service/data/inputs/ready/<funnel_id>/source.mp4`
- `source-input/input_service/data/state/seen_urls.json`
- `video-automation/jobs/<job>/report.json`
- `video-automation/jobs/<job>/analytics.json`
- `video-automation/analytics/events.jsonl`
- `video-automation/analytics/feedback.jsonl`

## Common Failures

- **video `/doctor` says `OPENAI_API_KEY` missing**: set it in
  `video-automation/.env` or the process manager.
- **video `/doctor` says `whisper` missing**: install the Whisper CLI into the
  runtime path used by the service.
- **source `/doctor` says funnels config invalid**: fix
  `source-input/input_service/config/funnels.json`; use `GET /funnels` to view
  the validated manifest.
- **n8n cannot download clips**: ensure `/output/<clip_file>` is reachable from
  n8n and `video-automation/output` is persistent.
