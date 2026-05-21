# Output Funnel Mk1

`output-funnel` is a separate service that starts after `video-automation` has successfully produced finished clips. It imports completed clip metadata, verifies files, creates upload jobs, routes them to configured channel profiles, schedules staggered uploads, and publishes through platform adapters.

Mk1 focuses on reliability, queueing, scheduling, and YouTube scheduled publishing. It does not add subtitles, zooms, reframing, B-roll, analytics loops, anti-detection logic, or aggressive scaling.

## Boundary

The input contract is a completed `video-automation` job output payload from `GET /jobs/<job_id>/outputs` or an equivalent `report.json` with `status == "success"`.

Source clips are immutable after registration. Operational changes happen on `upload_jobs`, so retries, metadata formatting, scheduling, and future platform-specific packaging do not mutate the imported clip facts.

## Local Setup

```shell
cd output-funnel
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
```

Runtime state is stored in SQLite at `data/output_funnel.sqlite3` by default. Override paths with:

- `OUTPUT_FUNNEL_DB`
- `OUTPUT_FUNNEL_SETTINGS`
- `OUTPUT_FUNNEL_CHANNELS`
- `OUTPUT_FUNNEL_CONFIG_DIR`
- `OUTPUT_FUNNEL_AUTO_SCHEDULE`
- `OUTPUT_FUNNEL_AUTO_PUBLISH`

Example upload-job metadata lives at `config/upload_job.example.json`. It shows the normalized shape expected around a YouTube Shorts upload: clip path, title, description, tags, privacy, optional `publish_at`, channel profile id, source identifiers, and the platform-neutral `platform_asset_id` result field.

## Run

```shell
python -m output_funnel.app
```

Initialize the database and run one-off worker actions:

```shell
python -m output_funnel.cli init-db
python -m output_funnel.cli register --report-path ../video-automation/jobs/<job>/report.json
python -m output_funnel.cli schedule --all
python -m output_funnel.cli publish-due --limit 5
```

Useful endpoints:

- `POST /registrations/from-job`
- `GET /queue`
- `GET /queue/<upload_job_id>`
- `POST /queue/<upload_job_id>/schedule`
- `POST /queue/schedule-due`
- `POST /queue/publish-due`
- `POST /queue/<upload_job_id>/retry`

`POST /registrations/from-job` auto-schedules registered clips by default when routing/preflight pass. It does not auto-publish unless `OUTPUT_FUNNEL_AUTO_PUBLISH=1` or `automation.auto_publish` is enabled in settings.

## YouTube OAuth

Channel profiles reference token and client-secret paths through environment variable names. Do not commit token files or client secrets.

For YouTube scheduled publishing, the adapter uploads videos as private with `status.publishAt` set to a future UTC timestamp. The normalized publish result stores the YouTube video id as `platform_asset_id`, keeping the queue platform-neutral for future TikTok, Instagram, and X adapters.

## Testing

```shell
python -m pytest
```

The test suite mocks media probing and YouTube publishing. It does not require OAuth credentials or real uploads.
