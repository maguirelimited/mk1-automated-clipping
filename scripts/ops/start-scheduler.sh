#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/start-scheduler.sh <environment> [--confirm]

Purpose:
  Canonical control to resume scheduled pipeline automation after stop-scheduler.
  Does not trigger a pipeline run and does not install cron/timers.

Environment (required):
  dev | prod

Options:
  --confirm     Required to start scheduler in production
  -h, --help    Show this help

Examples:
  ./scripts/ops/start-scheduler.sh dev
  ./scripts/ops/start-scheduler.sh prod --confirm

Notes:
  Sets data/<env>/control_state.json scheduler_disabled=false.
  Does not start services, trigger runs, install cron, or change upload state.
  Next cron tick uses run-scheduled.sh → run-pipeline.sh --trigger scheduled.
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

  local env confirm=false
  env="$(require_ops_env "$1")"
  shift

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --confirm)
        confirm=true
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown option: $1" >&2
        usage >&2
        exit 1
        ;;
    esac
  done

  local python_bin
  python_bin="$(ops_find_python)"
  local args=(start "$env")
  if [[ "$confirm" == true ]]; then
    args+=(--confirm)
  fi
  exec "$python_bin" "$SCRIPT_DIR/scheduler_control.py" "${args[@]}"
}

main "$@"
