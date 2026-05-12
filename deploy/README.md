# mk0.4 Deployment Runbook

This directory contains the portable setup path for running the mk0.4 services
outside the original development machine. The current application is Python,
bash, and PATH-based CLIs; Linux readiness is mostly about installing the
runtime stack, setting real absolute paths, and keeping runtime storage durable.

## Services

- **source-input** (`127.0.0.1:5060` by default): fetches one ready long-form
  input for a configured funnel.
- **video-automation** (`0.0.0.0:5050` by default): transcribes, selects,
  validates, clips, and writes analytics events.

Both services are intentionally independent. n8n or another scheduler should
call source-input first, then hand the ready file to video-automation.

## Fresh Ubuntu/Linux Setup

Run these from the Linux host that will execute the services. Replace
`/opt/mk04/VAmk0.4` with the real checkout path for your deployment.

```bash
sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv python3-pip \
  ffmpeg curl git

cd /opt/mk04/VAmk0.4
./deploy/scripts/bootstrap.sh
```

`ffmpeg` packages usually include `ffprobe`; confirm both resolve on `PATH`.
Install the Whisper CLI separately so the command `whisper` is available to the
same user/process that runs video automation. Linux installs may require a
deliberate PyTorch choice: CPU-only is conservative and simplest, CUDA can be
faster but depends on the host GPU, driver, and PyTorch wheel selection. Do not
add a GPU-specific install to bootstrap unless that host is intentionally GPU
managed.

Example CPU-oriented install into the video automation venv:

```bash
/opt/mk04/VAmk0.4/video-automation/.venv/bin/python -m pip install -U openai-whisper
/opt/mk04/VAmk0.4/video-automation/.venv/bin/whisper --help >/dev/null
```

If `whisper` is installed only inside the venv, run the service through the
provided run script or set `PATH`/`PYTHON_BIN` in your process manager so
`whisper` resolves.

## Configure Environment

Bootstrap copies examples to:

- `source-input/input_service/.env`
- `video-automation/.env`

Edit them on the Linux host before starting the services.

Required or strongly recommended values:

- `OPENAI_API_KEY` in `video-automation/.env`; the doctor reports presence but
  never prints the key.
- `INPUT_SERVICE_ROOT` as the absolute Linux path to
  `source-input/input_service`.
- `PIPELINE_CONFIG_PATH` as the absolute Linux path to
  `video-automation/config/pipeline_config.json`.
- `VIDEO_PIPELINE_PROFILES_PATH` as the absolute Linux path to
  `video-automation/config/video_pipeline_profiles.json`.
- `VIDEO_AUTOMATION_INPUT_DIR` only if the clipping input folder is outside the
  default repo-owned `video-automation/input` directory.

Keep these paths pinned in production so systemd, containers, or background
shells do not depend on the current working directory.

## Readiness Checks

Before starting services, check local binaries:

```bash
python3 --version
command -v ffmpeg
command -v ffprobe
command -v whisper
```

With both services running, use the repo doctor:

```bash
./deploy/scripts/doctor.sh
```

Or call endpoints directly:

```bash
curl -fsS http://127.0.0.1:5060/healthz
curl -fsS http://127.0.0.1:5050/healthz
curl -fsS http://127.0.0.1:5060/doctor
curl -fsS http://127.0.0.1:5050/doctor
```

If `INPUT_SERVICE_SECRET` is set, include
`-H "X-Input-Service-Secret: $INPUT_SERVICE_SECRET"` when calling source-input
`/doctor`, `/funnels`, or `/run-funnel`.

The doctors are safe readiness checks. They validate the Python executable,
virtualenv status, Flask/import availability where relevant, required CLIs,
OpenAI key presence, config files, funnel config, and runtime path existence /
writability.

## Run Manually During Testing

Terminal 1:

```bash
cd /opt/mk04/VAmk0.4
./deploy/scripts/run-input-service.sh
```

Terminal 2:

```bash
cd /opt/mk04/VAmk0.4
./deploy/scripts/run-video-automation.sh
```

Defaults:

- source-input binds `127.0.0.1:5060`.
- video-automation binds `0.0.0.0:5050`.

If callers are remote, open firewall/security-group access only for the ports
they need. Keep source-input bound to localhost unless a remote orchestrator
must call it, and use `INPUT_SERVICE_SECRET` if it is exposed beyond localhost.
Manual logs go to the terminal. Under a process manager, logs go to that
manager's stdout/stderr collection, such as `journalctl` for systemd.

## Minimal End-to-End Smoke Test

This exercises the service boundary without changing pipeline behavior.

1. Confirm services and doctors:

   ```bash
   ./deploy/scripts/doctor.sh
   ```

2. Ask source-input for one ready funnel input:

   ```bash
   curl -fsS \
     -H "Content-Type: application/json" \
     -d '{"funnel_id":"business_podcasts_001"}' \
     http://127.0.0.1:5060/run-funnel
   ```

   If `INPUT_SERVICE_SECRET` is configured, add
   `-H "X-Input-Service-Secret: $INPUT_SERVICE_SECRET"`.

3. If the response returns `status: "input_ready"`, process the basename that
   was copied into `video-automation/input`. The default copied filename is
   `<funnel_id>_source.mp4`:

   ```bash
   curl -fsS \
     -H "Content-Type: application/json" \
     -d '{"video":"business_podcasts_001_source.mp4","pipeline_profile":"business_podcasts_001"}' \
     http://127.0.0.1:5050/process
   ```

4. Fetch one returned clip URL:

   ```bash
   curl -fS -o /tmp/mk04-smoke-clip.mp4 \
     http://127.0.0.1:5050/output/<clip_file>
   ```

This final step can consume OpenAI and Whisper runtime, so use a small known
test video when possible.

## yt-dlp on Headless Linux

YouTube bot checks can differ between a laptop and a headless Linux host. The
input service already supports these runtime options:

- `YT_DLP_COOKIES_PATH=/absolute/linux/path/to/cookies.txt` is usually the most
  reliable server option. Export a Netscape-format `cookies.txt` from a browser
  and place it where the service user can read it.
- `YT_DLP_COOKIES_FROM_BROWSER=chrome` can work, but on headless Linux it may
  need Linux-specific browser/profile/keyring values and a real browser data
  directory.
- `YT_DLP_JS_RUNTIME=deno` or `YT_DLP_USE_DENO=1` can enable Deno for YouTube
  JavaScript challenge solving if needed.
- Keep `yt-dlp` updated in the input-service venv when YouTube behavior changes.

## Process Managers

For systemd, supervisord, Docker, or Kubernetes, use the run scripts as the
command entrypoints or translate their env exports into the process manager.
The important contract is:

- Load env from the relevant `.env`.
- Run from the service root, or set the same env vars the run scripts export.
- Keep `ffmpeg`, `ffprobe`, and `whisper` available on `PATH`.
- Use `PYTHON_BIN` only if you intentionally run a different Python than the
  repo venv.
- Persist the runtime directories listed below.

Do not point multiple live workers at the same mutable runtime folders unless
you have designed external locking and storage isolation.

## Persistent Storage

Keep these on durable disk if you need duplicate tracking, auditability, clips,
or troubleshooting after restarts:

- `source-input/input_service/data/state/seen_urls.json`
- `source-input/input_service/data/inputs/ready/`
- `source-input/input_service/data/inputs/rejected/`
- `source-input/input_service/data/tmp/` for in-flight downloads and debugging
- `video-automation/input/`
- `video-automation/output/`
- `video-automation/jobs/`
- `video-automation/analytics/`
- `video-automation/temp/` if `artifact_retention.temp_policy` keeps debug
  artifacts

In ephemeral containers or short-lived VMs, mount these as volumes. Losing
`seen_urls.json` can cause duplicate source selection; losing `jobs`, `output`,
or `analytics` removes audit and delivery artifacts.

## n8n and Linux Paths

The n8n context files under `video-automation/context/n8n-context/` are examples
from a developer setup. Any old macOS host paths such as `/Users/...` are not
portable deployment values.

On Linux, configure n8n volumes/workflows to either:

- use real Linux host paths, for example `/var/lib/mk04/video-automation/input`;
  or
- rely on API-returned paths and `/output/<clip_file>` URLs instead of hardcoded
  host paths.

For Docker Engine on Linux, the compose example includes
`host.docker.internal:host-gateway` so n8n containers can reach services running
on the host.

## Normal Orchestration

1. `POST /run-funnel` to source-input with `{ "funnel_id": "..." }`.
2. If `status == "input_ready"`, use the returned `video_path` or the predictable
   copied basename in `video-automation/input/`.
3. `POST /process` to video-automation with the basename and, if needed,
   `pipeline_profile` from source-input. Avoid sending `selection` overrides
   unless it is an explicit runtime exception.
4. Fetch clips from `/output/<clip_file>`.
5. Post platform performance back to `POST /analytics/feedback` with `job_id`,
   `clip_id`, `platform`, `posted_url`, and `metrics`.

## Common Failures

- **video `/doctor` says `OPENAI_API_KEY` missing**: set it in
  `video-automation/.env` or the process manager.
- **video `/doctor` says `whisper` missing**: install the Whisper CLI into the
  runtime path used by the service.
- **doctor reports `python_venv` false**: the service is running outside the
  expected venv. Use the run scripts or set `PYTHON_BIN` intentionally.
- **doctor reports a path is not writable**: fix ownership/permissions for the
  service user or mount a writable volume.
- **source `/doctor` says funnels config invalid**: fix
  `source-input/input_service/config/funnels.json`; use `GET /funnels` to view
  the validated manifest.
- **n8n cannot reach host services on Linux Docker**: keep the
  `host.docker.internal:host-gateway` mapping or use the host IP reachable from
  the container.
- **n8n cannot download clips**: ensure `/output/<clip_file>` is reachable from
  n8n and `video-automation/output` is persistent.
