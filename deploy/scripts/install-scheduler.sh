#!/usr/bin/env bash
# Install the production cron scheduler (Reliability & Recovery Phase 9).
#
# Installs the existing cron-based schedule so it survives reboot.
# Does not start a pipeline run and does not implement a second scheduler.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

usage() {
  cat <<'EOF'
Usage: ./deploy/scripts/install-scheduler.sh <environment> [--system]

Purpose:
  Install cron entries that trigger scripts/ops/run-scheduled.sh.
  Cron starts with the OS, so the schedule survives reboot.

Environment:
  prod   Install production schedule (default path /opt/mk04/prod/current)
  dev    Print guidance only (dev is manual by default)

Options:
  --system   Install /etc/cron.d/mk04 (requires sudo) instead of user crontab
  -h, --help Show this help

Examples:
  ./deploy/scripts/install-scheduler.sh prod
  sudo ./deploy/scripts/install-scheduler.sh prod --system

After install, verify:
  systemctl is-enabled cron || systemctl is-enabled crond
  crontab -l            # user install
  cat /etc/cron.d/mk04  # system install
  ./scripts/ops/scheduler-status.sh prod
EOF
}

ENV_ARG=""
SYSTEM=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --system)
      SYSTEM=1
      shift
      ;;
    dev|prod|development|production)
      ENV_ARG="$1"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$ENV_ARG" ]]; then
  usage >&2
  exit 2
fi

case "${ENV_ARG,,}" in
  dev|development)
    echo "Dev scheduler mode is manual by default."
    echo "Trigger on demand:"
    echo "  ./scripts/ops/run-scheduled.sh dev <funnel_id>"
    echo "Optional: add a commented line from deploy/cron/mk04.crontab for local cron."
    exit 0
    ;;
  prod|production)
    ;;
  *)
    echo "Invalid environment: $ENV_ARG" >&2
    exit 2
    ;;
esac

CRONTAB_USER="$REPO_ROOT/deploy/cron/mk04.crontab"
CRON_D_SRC="$REPO_ROOT/deploy/cron/mk04.cron.d"

if [[ ! -f "$CRONTAB_USER" ]]; then
  echo "Missing $CRONTAB_USER" >&2
  exit 1
fi

if [[ "$SYSTEM" -eq 1 ]]; then
  if [[ ! -f "$CRON_D_SRC" ]]; then
    echo "Missing $CRON_D_SRC" >&2
    exit 1
  fi
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "System install requires root (sudo)." >&2
    exit 1
  fi
  install -m 644 "$CRON_D_SRC" /etc/cron.d/mk04
  echo "Installed /etc/cron.d/mk04"
else
  # Prefer deployed path in the installed crontab file content.
  crontab "$CRONTAB_USER"
  echo "Installed user crontab from $CRONTAB_USER"
  echo "Current crontab:"
  crontab -l
fi

echo
echo "Ensure cron is enabled at boot:"
echo "  sudo systemctl enable --now cron    # Debian/Ubuntu"
echo "  sudo systemctl enable --now crond   # RHEL/CentOS"
echo
echo "Scheduler trigger paths:"
echo "  pipeline:  scripts/ops/run-scheduled.sh → scripts/ops/run-pipeline.sh --trigger scheduled"
echo "  retention: scripts/ops/run-scheduled-retention.sh → storage.retention_schedule"
echo "             (mode from storage.schedule; production defaults to dry_run)"
echo
echo "Runtime pause/resume (does not uninstall cron):"
echo "  ./scripts/ops/stop-scheduler.sh prod"
echo "  ./scripts/ops/start-scheduler.sh prod --confirm"
echo "  ./scripts/ops/scheduler-status.sh prod"
