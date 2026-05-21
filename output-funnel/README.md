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
- `OUTPUT_FUNNEL_AUTO_SCHEDULE_LIMIT`
- `OUTPUT_FUNNEL_AUTO_PUBLISH`
- `OUTPUT_FUNNEL_AUTO_PUBLISH_LIMIT`
- `OUTPUT_FUNNEL_SCHEDULE_TIMEZONE`
- `OUTPUT_FUNNEL_SCHEDULE_LEAD_MINUTES`
- `OUTPUT_FUNNEL_SCHEDULE_MIN_GAP_MINUTES`
- `OUTPUT_FUNNEL_SCHEDULE_MAX_UPLOADS_PER_DAY`

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

## Scheduling Automation

Scheduling and publishing are separate steps. Registration can route and schedule eligible YouTube upload jobs automatically, but it will not upload to YouTube unless publishing is explicitly enabled.

Configure automation in `config/settings.example.json` or your `OUTPUT_FUNNEL_SETTINGS` file:

- `automation.auto_schedule`: schedule newly registered eligible jobs after registration. Defaults to `true`; override with `OUTPUT_FUNNEL_AUTO_SCHEDULE=0` or `1`.
- `automation.schedule_limit`: maximum number of pending `registered`/`routed` jobs to schedule in one automatic or batch pass. Override with `OUTPUT_FUNNEL_AUTO_SCHEDULE_LIMIT`.
- `automation.auto_publish`: publish due scheduled jobs only when explicitly enabled. Defaults to `false`; override with `OUTPUT_FUNNEL_AUTO_PUBLISH=1`.
- `scheduler.default_timezone`, `scheduler.default_lead_minutes`, `scheduler.default_min_gap_minutes`, and `scheduler.default_max_uploads_per_day`: fallback cadence values when a channel profile does not define them. Override with the matching `OUTPUT_FUNNEL_SCHEDULE_*` env vars.
- Channel `cadence` settings in `config/channels.example.json` take precedence over scheduler defaults: `timezone`, `default_lead_minutes`, `min_gap_minutes`, `max_uploads_per_day`, and `allowed_windows`.

To schedule existing pending rows without publishing:

```shell
python -m output_funnel.cli schedule --all
```

Pass `--limit <n>` to override the configured batch limit for that run.

## YouTube OAuth

Channel profiles reference token and client-secret paths through environment variable names. Do not commit token files or client secrets.

For YouTube scheduled publishing, the adapter uploads videos as private with `status.publishAt` set to a future UTC timestamp. The normalized publish result stores the YouTube video id as `platform_asset_id`, keeping the queue platform-neutral for future TikTok, Instagram, and X adapters.

## Testing

```shell
python -m pytest
```

The test suite mocks media probing and YouTube publishing. It does not require OAuth credentials or real uploads.
