# mk0.4 systemd units

Drop-in unit files for the always-running production services. They assume:

- PROD deployed copy at `/opt/mk04/prod/current`
- Service user `mk04:mk04`
- venvs created by `deploy/scripts/bootstrap.sh`
- environment config in `/etc/mk04/prod/env`
- optional service-specific overrides in `/etc/mk04/prod/services/*.env`

Do not point these units at the active development checkout. DEV runs from the
repo checkout with `MK04_ENV=dev`; PROD runs from the deployed copy with
`MK04_ENV=prod`.

Service inventory (required vs optional, health checks, categories):
see [`docs/operations/PRODUCTION_SERVICES.md`](../../docs/operations/PRODUCTION_SERVICES.md).

## Units

| Unit | Role | Enable on prod? |
| --- | --- | --- |
| `mk04-source-input.service` | API / ingestion | **Required** |
| `mk04-video-automation.service` | Worker / clipping pipeline | **Required** |
| `mk04-output-funnel.service` | Upload / publish queue | **Required** for autonomous publish |
| `mk04-ai-service.service` | Local LLM selection | Optional (enable when using local models) |
| `mk04-ops-ui.service` | Operations UI | Optional for pipeline; enable for operator UI |

All units use:

- `Restart=always`
- `RestartSec=5`
- `StandardOutput=journal` / `StandardError=journal`
- `WantedBy=multi-user.target` (start on boot when enabled)
- mandatory `EnvironmentFile=/etc/mk04/prod/env` plus optional per-service
  `EnvironmentFile=-/etc/mk04/prod/services/<service>.env`

There are no pipeline timer units here. Scheduled pipeline runs use **cron** →
`scripts/ops/run-scheduled.sh` (see `docs/operations/SCHEDULER.md`).

## Install

```bash
sudo cp deploy/systemd/mk04-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  mk04-source-input.service \
  mk04-video-automation.service \
  mk04-output-funnel.service \
  mk04-ai-service.service \
  mk04-ops-ui.service
```

Omit `mk04-ai-service` and/or `mk04-ops-ui` on hosts that do not need them.
The pipeline services call the local LLM over HTTP only when configured to;
`mk04-ai-service` listens on `127.0.0.1:5075` in prod and is independent —
start, stop, or omit it without affecting the other units.

### Ollama (local model backend)

`mk04-ai-service.service` declares a soft dependency on `ollama.service`
(`Wants=`/`After=`). The official Ollama installer ships that unit; if it is
present systemd starts it first, and if it is absent the `Want` is simply
ignored. Independently, `run-ai-service.sh` best-effort runs
`deploy/scripts/run-ollama.sh` before exec, so Ollama is started/verified even
without a separate unit. That step is non-fatal by default (clip selection
falls back to the configured backend); set `MK04_OLLAMA_STRICT=1` to make a
missing/unreachable backend fail ai-service startup. Model pulling is gated by
`OLLAMA_AUTO_PULL_MODEL` (default `false`) to avoid surprise downloads.

## Verify

```bash
# Syntax check (from repo, before install)
systemd-analyze verify deploy/systemd/mk04-*.service

# After install
systemctl status \
  mk04-source-input \
  mk04-video-automation \
  mk04-output-funnel \
  mk04-ai-service \
  mk04-ops-ui
systemctl is-enabled \
  mk04-source-input \
  mk04-video-automation \
  mk04-output-funnel \
  mk04-ai-service \
  mk04-ops-ui
journalctl -u mk04-output-funnel -n 100 --no-pager
```

Restart recovery (policy-only, then live kill/recover on a host with units):

```bash
python scripts/smoke/smoke_restart_recovery.py --env dev
python scripts/smoke/smoke_restart_recovery.py --env dev --execute
```

See [`docs/operations/RESTART_RECOVERY.md`](../../docs/operations/RESTART_RECOVERY.md).

## Peer ordering and readiness (Phase 3)

Units use **soft** peer dependencies only (`After=` + `Wants=`, never
`Requires=`), so a slow or missing peer does not permanently fail a unit:

| Unit | Soft peers |
| --- | --- |
| `mk04-source-input` | network only |
| `mk04-video-automation` | `mk04-source-input` |
| `mk04-output-funnel` | `mk04-video-automation` |
| `mk04-ai-service` | `ollama.service` (vendor; optional) |
| `mk04-ops-ui` | network only (starts independently) |

Process start order is not the same as operational readiness. Scheduled and
manual production runs use `scripts/ops/run-pipeline.sh`, which validates
config and boot readiness before `POST /run-funnel`. AI and Operations UI may
be down without blocking readiness (optional components).

Services remain crash-safe and idempotent if peers start late.

## Logging

All units use `StandardOutput=journal`, so:

```bash
journalctl -u mk04-output-funnel -f
journalctl -u mk04-output-funnel --since '1 hour ago' | grep upload_worker
```

journald handles rotation. No external logrotate config needed for stdout/stderr.
File-based logs (debug NDJSON sinks, video-automation report.json, etc.) live
in the service runtime folders and are covered by `deploy/logrotate/mk04`.

## Sudoers shape (for ops-ui systemctl access)

If ops-ui runs as a non-root user and needs to restart peer services, drop the
sudoers entries documented in `ops-ui/README.md`. Keep that scope narrow —
specific unit names only, never wildcard.
