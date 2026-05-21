# Mk1 Input Service

Standalone podcast input funnel module. Triggered by n8n via HTTP, finds **one**
valid non-duplicate longform YouTube video for a configured funnel, downloads
it, validates it, and stores it in a predictable ready-input location.

This service is intentionally **separate** from the clipping / transcription /
upload pipeline. It does not import, call, or trigger any of those.

## Layout

```
input_service/
  app.py                       # Flask entrypoint, POST /run-funnel
  requirements.txt
  config/
    funnels.json               # funnel definitions
  input_service/               # the importable package
    funnel_loader.py
    source_checker.py
    candidate_filter.py
    duplicate_store.py
    downloader.py
    media_validator.py
    storage.py
    runner.py                  # run_funnel orchestration
    paths.py
  data/
    inputs/
      ready/<funnel_id>/source.mp4
      rejected/<funnel_id>/...
    state/
      seen_urls.json
    tmp/<funnel_id>/...        # in-flight downloads
```

## Requirements

- Python 3.10+
- `ffmpeg` / `ffprobe` available on `PATH` (used for media validation)
- Python packages from `requirements.txt`:

```bash
cd input_service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run the server

```bash
cd input_service
python app.py
```

Defaults: binds `127.0.0.1:5060`. Override with env vars:

| Variable | Default | Purpose |
| --- | --- | --- |
| `INPUT_SERVICE_HOST` | `127.0.0.1` | Bind host |
| `INPUT_SERVICE_PORT` | `5060` | Bind port (kept distinct from clipping server) |
| `INPUT_SERVICE_SECRET` | _(unset)_ | If set, `POST /run-funnel` requires header `X-Input-Service-Secret: <value>` |
| `INPUT_SERVICE_LOG_LEVEL` | `INFO` | Python log level |
| `INPUT_SERVICE_ROOT` | _(auto)_ | Override the project root used for `data/` and `config/` |

## API

### `POST /run-funnel`

Request:

```json
{ "funnel_id": "business_podcasts_001" }
```

Possible responses (always JSON):

- **200** input ready:
  ```json
  {
    "success": true,
    "status": "input_ready",
    "funnel_id": "business_podcasts_001",
    "video_path": ".../data/inputs/ready/business_podcasts_001/source.mp4",
    "source_url": "https://www.youtube.com/watch?v=...",
    "title": "Episode title"
  }
  ```
- **200** no valid input:
  ```json
  {
    "success": true,
    "status": "no_input_available",
    "funnel_id": "business_podcasts_001",
    "reason": "All candidate videos have already been used."
  }
  ```
- **409** another run already in progress:
  ```json
  {
    "success": false,
    "status": "already_running",
    "funnel_id": "business_podcasts_001",
    "error": "another run is already in progress"
  }
  ```
- **400 / 401 / 500** failure:
  ```json
  {
    "success": false,
    "status": "failed",
    "funnel_id": "business_podcasts_001",
    "error": "unknown_funnel: ..."
  }
  ```

### `GET /healthz`

Returns `{"ok": true, "service": "input_service"}`.

## Funnel config

`config/funnels.json` is a JSON list of funnel objects. Each object must include:

- `funnel_id` (string)
- `angle` (string)
- `source_type` (mk1: `"youtube_channels"` only)
- `sources` (list of YouTube channel URLs)
- `min_duration_minutes` (int)
- `max_duration_minutes` (int)
- `active` (bool)

Example:

```json
[
  {
    "funnel_id": "business_podcasts_001",
    "angle": "business founder podcasts",
    "source_type": "youtube_channels",
    "sources": [
      "https://www.youtube.com/@examplechannel1",
      "https://www.youtube.com/@examplechannel2"
    ],
    "min_duration_minutes": 25,
    "max_duration_minutes": 180,
    "active": true
  }
]
```

## Filtering rules (deterministic, no AI)

A candidate must:

- come from an approved source listed in the funnel
- have a known duration that fits `[min_duration_minutes, max_duration_minutes]`
- not already be in `data/state/seen_urls.json`
- not be an obvious Short (URL contains `/shorts/` or duration ≤ 70s)
- not contain a blocked term in its title (whole-word, case-insensitive):
  `shorts, short, clip, clips, highlight, highlights, trailer, teaser, preview, compilation`

Remaining candidates are sorted **newest first** by upload timestamp.

## Concurrency model

A single in-process lock guards `POST /run-funnel`. A second concurrent call
returns `409 already_running` instead of starting a parallel download.

## Marking videos as "seen"

`data/state/seen_urls.json` tracks both `video_ids` and `urls`. Entries are
only added after a **successful** download AND validation. Failed downloads
and failed validations are never marked as seen.

## n8n integration

n8n only triggers input ingestion. Clipping starts automatically when input is ready.

```
n8n daily trigger (or manual)
  → HTTP POST http://host.docker.internal:5060/run-funnel
      body: { "funnel_id": "business_podcasts_001" }
  → if status == "input_ready", done (clipping enqueued internally)
  → if status == "no_input_available", stop or notify
```

Both services must be running: input on **5060**, video-automation on **5050**.
