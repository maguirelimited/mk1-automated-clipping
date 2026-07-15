#!/usr/bin/env bash
# Thin log-rotation trigger (Storage & Data Management Phase 9).
#
# Rotates active project logs under logs/<env>/. Expired rotated logs are
# removed by the retention engine (storage.retention.logs_days), not here.
#
# Cron (example):
#   15 3 * * * /opt/mk04/prod/current/scripts/ops/run-log-rotation.sh prod
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
REPO_ROOT="$(ops_repo_root)"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/run-log-rotation.sh <environment>

Purpose:
  Rotate oversized active project logs. Does not delete by age — retention
  owns expiry of rotated logs via storage.retention.logs_days.

Environment (required):
  dev | prod

Examples:
  ./scripts/ops/run-log-rotation.sh prod
  ./scripts/ops/run-log-rotation.sh dev

Exit codes:
  0 success, partial, or skipped
  1 rotation failure
  2 usage
  3 config failure
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

  local log_tag="mk04-${env}-log-rotation"
  if command -v logger >/dev/null 2>&1; then
    logger -t "$log_tag" -- "log rotation trigger env=$env entrypoint=run_log_rotation.py"
  fi
  echo "[$log_tag] log rotation trigger env=$env"

  local python_bin="${MK04_PYTHON:-}"
  if [[ -z "$python_bin" ]]; then
    if [[ -x "$REPO_ROOT/video-automation/.venv/bin/python" ]]; then
      python_bin="$REPO_ROOT/video-automation/.venv/bin/python"
    else
      python_bin="$(command -v python3)"
    fi
  fi

  exec "$python_bin" "$SCRIPT_DIR/run_log_rotation.py" "$env"
}

main "$@"
