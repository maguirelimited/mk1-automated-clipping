#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/update.sh <environment> [options]

Purpose:
  Remote-friendly entry point for system updates. Will delegate to the existing
  repo-root ./update.sh (Configuration & Deployment Prompt 8).

Environment (required):
  dev | prod

Options (future — passed through to repo-root ./update.sh):
  --pull          git pull --ff-only before checks
  --full-tests    also run video-automation/tests
  --no-restart    skip systemd restarts
  --check-only    validate config and dependencies only
  -h, --help      Show this help

Examples:
  ./scripts/ops/update.sh prod
  ./scripts/ops/update.sh prod --check-only
  ./scripts/ops/update.sh dev --pull

Status:
  Placeholder only. This script will become a thin wrapper around ./update.sh
  in a later Remote Operations prompt. It does not duplicate update logic.
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
  shift

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --pull|--full-tests|--no-restart|--check-only)
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

  print_placeholder "update.sh" \
    "This script will later delegate to repo-root ./update.sh (Configuration & Deployment Prompt 8)." \
    "Environment: ${env}" \
    "No update was run." \
    "No system state was changed."
}

main "$@"
