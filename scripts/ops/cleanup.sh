#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/cleanup.sh <environment> [--dry-run | --apply]

Purpose:
  Report cleanup readiness for an environment. Deletion is deferred until
  Storage & Data Management retention planning exists.

Environment (required):
  dev | prod

Modes:
  --dry-run     Show cleanup status (no deletion)
  --apply       Refused until a safe retention planner exists

Options:
  -h, --help    Show this help

Examples:
  ./scripts/ops/cleanup.sh dev --dry-run
  ./scripts/ops/cleanup.sh prod --dry-run

Notes:
  Does not delete files, jobs, clips, or database contents.
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

  local env mode=""
  env="$(require_ops_env "$1")"
  shift

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run|--apply)
        mode="$1"
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

  if [[ -z "$mode" ]]; then
    echo "Error: cleanup requires --dry-run or --apply." >&2
    usage >&2
    exit 1
  fi

  local python_bin
  python_bin="$(ops_find_python)"
  exec "$python_bin" "$SCRIPT_DIR/cleanup_control.py" "$env" "$mode"
}

main "$@"
