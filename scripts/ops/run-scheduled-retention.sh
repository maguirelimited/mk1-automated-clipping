#!/usr/bin/env bash
# Thin scheduled-retention trigger (Storage & Data Management Phase 8).
#
# The scheduler's only job is to invoke the shared retention schedule entrypoint.
# It does not implement planner, apply, or deletion logic.
#
# Cron (example):
#   30 3 * * * /opt/mk04/prod/current/scripts/ops/run-scheduled-retention.sh prod
#
# Manual:
#   ./scripts/ops/run-scheduled-retention.sh prod
#   ./scripts/ops/run-scheduled-retention.sh dev
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
REPO_ROOT="$(ops_repo_root)"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/run-scheduled-retention.sh <environment>

Purpose:
  Scheduler trigger only. Delegates to run_scheduled_retention.py, which
  loads config and calls the existing retention dry-run planner or apply
  executor. Does not implement retention policy or deletion logic.

Environment (required):
  dev | prod

Examples:
  ./scripts/ops/run-scheduled-retention.sh prod
  ./scripts/ops/run-scheduled-retention.sh dev

Exit codes:
  0 success or skipped (disabled schedule)
  1 retention execution failure
  2 usage
  3 config failure

Notes:
  Mode and enablement come from storage.schedule in config.
  Production defaults to dry_run. Apply requires explicit mode: apply.
  Manual retention CLI (scripts/retention.py) is unchanged.
EOF
}

main() {
  if [[ $# -eq 0 ]] || is_help_flag "${1:-}"; then
    usage
    [[ $# -eq 0 ]] && exit 2
    exit 0
  fi

  local env
  env="$(require_ops_env "$1")" || exit 2

  local log_tag="mk04-${env}-retention-schedule"
  if command -v logger >/dev/null 2>&1; then
    logger -t "$log_tag" -- "scheduled retention trigger env=$env entrypoint=run_scheduled_retention.py"
  fi
  echo "[$log_tag] scheduled retention trigger env=$env"

  local python_bin="${MK04_PYTHON:-}"
  if [[ -z "$python_bin" ]]; then
    if [[ -x "$REPO_ROOT/video-automation/.venv/bin/python" ]]; then
      python_bin="$REPO_ROOT/video-automation/.venv/bin/python"
    else
      python_bin="$(command -v python3)"
    fi
  fi

  exec "$python_bin" "$SCRIPT_DIR/run_scheduled_retention.py" "$env"
}

main "$@"
