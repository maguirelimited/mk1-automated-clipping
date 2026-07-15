#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/backup.sh <environment>

Purpose:
  Create an environment-scoped backup of small operational state
  (database, control state, job/report JSON, recent small logs).

Environment (required):
  dev | prod

Options:
  -h, --help    Show this help

Examples:
  ./scripts/ops/backup.sh dev
  ./scripts/ops/backup.sh prod

Notes:
  Writes archives under backups/<env>/.
  Does not back up media, clips, .env files, or credentials.
  No files are deleted. No jobs or uploads are changed.
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
  exec "$python_bin" "$SCRIPT_DIR/backup_control.py" "$env"
}

main "$@"
