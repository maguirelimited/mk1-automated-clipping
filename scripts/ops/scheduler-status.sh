#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/scheduler-status.sh <environment>

Purpose:
  Canonical status for scheduled pipeline automation.
  Reports runtime control (stop/start-scheduler), whether new scheduled runs
  are allowed, underlying mechanism (cron today), and the pipeline entrypoint.

Environment (required):
  dev | prod

Options:
  -h, --help    Show this help

Examples:
  ./scripts/ops/scheduler-status.sh dev
  ./scripts/ops/scheduler-status.sh prod

Notes:
  Does not modify state. Mechanism details (cron vs future timer) are reported
  behind this interface so operators use stop/start/status only.
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
  exec "$python_bin" "$SCRIPT_DIR/scheduler_control.py" status "$env"
}

main "$@"
