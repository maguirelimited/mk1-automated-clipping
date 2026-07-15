#!/usr/bin/env bash
# Thin database-backup trigger (Storage & Data Management Phase 10).
#
# Creates a consistent SQLite snapshot. Expired backups are removed by the
# retention engine (storage.retention.database_backups_days), not here.
#
# Cron (example):
#   0 3 * * * /opt/mk04/prod/current/scripts/ops/run-database-backup.sh prod
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"
REPO_ROOT="$(ops_repo_root)"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/run-database-backup.sh <environment>

Purpose:
  Create a SQLite database backup for the environment. Does not delete old
  backups — retention owns expiry via database_backups_days.

Environment (required):
  dev | prod

Examples:
  ./scripts/ops/run-database-backup.sh prod
  ./scripts/ops/run-database-backup.sh dev

Exit codes:
  0 success or skipped
  1 backup failure
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

  local log_tag="mk04-${env}-database-backup"
  if command -v logger >/dev/null 2>&1; then
    logger -t "$log_tag" -- "database backup trigger env=$env entrypoint=run_database_backup.py"
  fi
  echo "[$log_tag] database backup trigger env=$env"

  local python_bin="${MK04_PYTHON:-}"
  if [[ -z "$python_bin" ]]; then
    if [[ -x "$REPO_ROOT/video-automation/.venv/bin/python" ]]; then
      python_bin="$REPO_ROOT/video-automation/.venv/bin/python"
    else
      python_bin="$(command -v python3)"
    fi
  fi

  exec "$python_bin" "$SCRIPT_DIR/run_database_backup.py" "$env"
}

main "$@"
