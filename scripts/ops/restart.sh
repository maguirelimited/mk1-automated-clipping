#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/restart.sh <environment> <target> [options]

Purpose:
  Restart selected mk04 systemd services, then run the health check.

Environment (required):
  dev | prod

Targets (required):
  api | worker | ai | all
  ops-ui | output-funnel   (optional, when mapped in deploy/systemd)

Options:
  --dry-run       Show what would be restarted; do not restart
  --confirm       Required for `./scripts/ops/restart.sh prod all`
  --skip-health   Restart only; do not run the embedded health check
  -h, --help      Show this help

Examples:
  ./scripts/ops/restart.sh prod worker
  ./scripts/ops/restart.sh prod ai
  ./scripts/ops/restart.sh prod api
  ./scripts/ops/restart.sh prod all --confirm
  ./scripts/ops/restart.sh prod worker --dry-run

Notes:
  Manual operational action only. Does not recover half-completed jobs.
  Health check runs after restart (exit codes: 0=PASS, 1=WARN, 2=FAIL).
  Check logs if restart or health fails.
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

  if [[ $# -lt 2 ]]; then
    usage >&2
    exit 1
  fi

  local env target
  env="$(require_ops_env "$1")"
  target="$2"
  shift 2

  local dry_run=0 confirm=0 skip_health=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run)
        dry_run=1
        ;;
      --confirm)
        confirm=1
        ;;
      --skip-health)
        skip_health=1
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
    shift
  done

  local python_bin
  python_bin="$(ops_find_python)" || {
    echo "restart.sh: no usable Python interpreter found" >&2
    exit 1
  }

  local -a args=("$SCRIPT_DIR/restart_service.py" "$env" "$target")
  if [[ "$dry_run" -eq 1 ]]; then
    args+=(--dry-run)
  fi
  if [[ "$confirm" -eq 1 ]]; then
    args+=(--confirm)
  fi
  if [[ "$skip_health" -eq 1 ]]; then
    args+=(--skip-health)
  fi

  exec "$python_bin" "${args[@]}"
}

main "$@"
