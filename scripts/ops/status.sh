#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/status.sh <environment>

Purpose:
  Read-only ~30-second operational summary for the selected environment
  (services, posting state, activity, resources, overall status).

Environment (required):
  dev | prod

Options:
  -h, --help    Show this help

Examples:
  ./scripts/ops/status.sh prod
  ./scripts/ops/status.sh dev

Notes:
  Includes boot readiness (READY / NOT READY) for config, core services,
  scheduler, database, and output paths. Queue state, run records, and
  execution locks remain not yet available until later Reliability phases.
  This command does not mutate upload, scheduler, service, or filesystem state.
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
  python_bin="$(ops_find_python)" || {
    echo "status.sh: no usable Python interpreter found" >&2
    exit 1
  }

  exec "$python_bin" "$SCRIPT_DIR/status_report.py" "$env"
}

main "$@"
