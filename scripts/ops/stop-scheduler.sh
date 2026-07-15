#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/stop-scheduler.sh <environment>

Purpose:
  Canonical control to pause scheduled pipeline automation.
  Blocks new runs from run-scheduled.sh / cron without touching the host schedule.

Environment (required):
  dev | prod

Options:
  -h, --help    Show this help

Examples:
  ./scripts/ops/stop-scheduler.sh dev
  ./scripts/ops/stop-scheduler.sh prod

Notes:
  Sets data/<env>/control_state.json scheduler_disabled=true.
  Does not kill running pipelines, uninstall cron/timers, stop services,
  or change upload state. Resume with start-scheduler.sh.
EOF
}

main() {
  if [[ $# -eq 0 ]]; then
    usage >&2
    exit 1
  fi
  if is_help_flag "$1"; then
    usage
    exit 0
  fi

  local env
  env="$(require_ops_env "$1")"

  local python_bin
  python_bin="$(ops_find_python)"
  exec "$python_bin" "$SCRIPT_DIR/scheduler_control.py" stop "$env"
}

main "$@"
