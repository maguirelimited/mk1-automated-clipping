# Log Rotation (Phase 9)

Log rotation keeps **active** logs bounded. The retention engine continues to
own **lifecycle expiry** of rotated logs via `storage.retention.logs_days`.

```text
Active log grows
    ↓
Rotation (size / backup_count / compress)
    ↓
Rotated artifacts under logs/<env>/
    ↓
Retention planner/apply (logs_days)
```

Do not treat rotation as a second retention system.

## Configuration

`storage.log_rotation` in merged config:

| Key | Meaning |
| --- | --- |
| `enabled` | Whether project-log rotation runs |
| `max_bytes` | Rotate when active log reaches this size |
| `backup_count` | Generations kept by rotation (`.1` … `.N`) |
| `compress` | Compress older generations (delaycompress: `.2+`) |
| `journal.system_max_use` | journald `SystemMaxUse` |
| `journal.runtime_max_use` | journald `RuntimeMaxUse` |
| `journal.max_file_sec` | journald `MaxFileSec` |

Expiry of rotated files uses **`storage.retention.logs_days`**, not a separate
rotation retention period.

### Defaults

System / production: 100 MiB, 8 backups, compress on, journal 500M / 100M / 1month.

Development: 50 MiB, 4 backups, smaller journal limits.

## Project logs

Active files under `logs/<env>/` with suffixes `.log`, `.ndjson`, `.jsonl`.

Rotation uses **copy-then-truncate**:

1. Shift existing `.N` generations
2. Copy active log to `.1`
3. Truncate active only after a successful archive

If truncate fails, the active log is left intact and the failure is recorded.
Rotation never truncates without a successful replacement archive.

Rotated names (`app.log.1`, `app.log.2.gz`) classify as `service_log` and are
eligible for retention under `logs_days`.

## Service logs (journald)

Long-running units use `StandardOutput=journal`. Journal growth is bounded by
installing a journald drop-in from config:

```bash
sudo ./deploy/scripts/install-log-rotation.sh prod
```

That writes:

* `/etc/systemd/journald.conf.d/mk04.conf`
* `/etc/logrotate.d/mk04` (deploy file sinks under `/var/log/mk04`)

Journal limits are host policy, not retention deletion of project artifacts.

## Entrypoints

```bash
# Manual / cron
./scripts/ops/run-log-rotation.sh prod

# Host journald + logrotate install
sudo ./deploy/scripts/install-log-rotation.sh prod
```

Cron (prod): `15 3 * * *` → `run-log-rotation.sh` (before scheduled retention).

## Observability

Each run writes:

* `data/<env>/storage/log_rotation_latest.json`
* append to `data/<env>/storage/log_rotation_history.jsonl`

Fields include timestamp, active log sizes, rotated count, compression actions,
failures, and `retention_logs_days` (for operators — not applied by rotation).

## Relationship with retention

| Concern | Owner |
| --- | --- |
| Bound active log size | Log rotation |
| Generations in rotation chain | `backup_count` |
| Delete expired rotated logs | Retention (`logs_days`) |
| Delete expired job logs | Retention (`logs_days`) |

Manual and scheduled retention are unchanged.

## Operational recommendations

1. Enable project rotation in config (default on).
2. Install journald/logrotate on the host once per environment.
3. Keep scheduled retention on dry-run until trusted; rotation does not delete by age.
4. Inspect `log_rotation_latest.json` after failures; active logs should still be present.
