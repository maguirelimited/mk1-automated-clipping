# mk0.4 systemd units

Drop-in unit files for the four mk0.4 services. They assume:

- PROD deployed copy at `/opt/mk04/prod/current`
- Service user `mk04:mk04`
- venvs created by `deploy/scripts/bootstrap.sh`
- environment config in `/etc/mk04/prod/env`
- optional service-specific overrides in `/etc/mk04/prod/services/*.env`

Do not point these units at the active development checkout. DEV runs directly
from `/Users/anthonymaguire/VAmk0.4` with `MK04_ENV=dev`; PROD runs from the
deployed copy with `MK04_ENV=prod`.

## Install

```bash
sudo cp deploy/systemd/mk04-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
  mk04-source-input.service \
  mk04-video-automation.service \
  mk04-output-funnel.service \
  mk04-ops-ui.service
```

## Verify

```bash
systemctl status mk04-source-input mk04-video-automation mk04-output-funnel mk04-ops-ui
journalctl -u mk04-output-funnel -n 100 --no-pager
```

## Why ordering matters

`After=` declarations are best-effort: video-automation depends on source-input
only for the n8n-style auto-enqueue handoff, and output-funnel depends on
video-automation only for the `/registrations/from-job` handoff. The services
are crash-safe and idempotent, so out-of-order restarts heal automatically.

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
