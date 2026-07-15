# System-wide cron drop-in for mk04 production scheduling.
# Install:
#   sudo cp /opt/mk04/prod/current/deploy/cron/mk04.cron.d /etc/cron.d/mk04
#   sudo chmod 644 /etc/cron.d/mk04
#
# Requires user `mk04` and deployed tree at /opt/mk04/prod/current.
# Cron daemon must be enabled (survives reboot):
#   sudo systemctl enable --now cron   # or crond

SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

# Scheduled pipeline — thin trigger only (see scripts/ops/run-scheduled.sh).
0 8 * * * mk04 /opt/mk04/prod/current/scripts/ops/run-scheduled.sh prod mfm_business_ai_001 >/dev/null 2>&1

# Supporting jobs (not the pipeline entrypoint).
*/10 * * * * mk04 MK04_ENV=prod PIPELINE_CONFIG_PATH=/etc/mk04/prod/video-automation/pipeline_config.json OUTPUT_FUNNEL_URL=http://127.0.0.1:5055 VIDEO_AUTOMATION_JOBS_DIR=/var/lib/mk04/prod/video-automation/jobs /usr/bin/python3 /opt/mk04/prod/current/video-automation/scripts/handoff_sweeper.py --quiet >/dev/null 2>&1
# Database backup — SQLite snapshot (see scripts/ops/run-database-backup.sh).
0 3 * * * mk04 /opt/mk04/prod/current/scripts/ops/run-database-backup.sh prod >/dev/null 2>&1
# Log rotation — bounds active project logs (see scripts/ops/run-log-rotation.sh).
15 3 * * * mk04 /opt/mk04/prod/current/scripts/ops/run-log-rotation.sh prod >/dev/null 2>&1
# Scheduled retention — thin trigger (see scripts/ops/run-scheduled-retention.sh).
30 3 * * * mk04 /opt/mk04/prod/current/scripts/ops/run-scheduled-retention.sh prod >/dev/null 2>&1
*/15 * * * * mk04 /opt/mk04/prod/current/deploy/scripts/watchdog.sh prod >/dev/null 2>&1
