#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/logs.sh <environment> <mode> [--lines N]

Purpose:
  Show bounded, secret-redacted logs for common operational inspection over SSH.

Environment (required):
  dev | prod

Modes (required):
  api            source-input ingestion API logs (API-facing service)
  worker         video-automation worker logs
  ai             AI service logs
  scheduler      scheduler logs (cron-backed; user-facing name is scheduler)
  errors         recent error lines across safe log sources
  today          recent logs from today where available
  output-funnel  output funnel service logs (optional)

Options:
  --lines N     Number of lines to show (default 200, max 1000)
  -h, --help    Show this help

Examples:
  ./scripts/ops/logs.sh prod errors
  ./scripts/ops/logs.sh prod worker
  ./scripts/ops/logs.sh prod api
  ./scripts/ops/logs.sh prod today
  ./scripts/ops/logs.sh prod worker --lines 100

Exit codes:
  0  Logs found, or source cleanly reported as empty
  1  Invalid usage, or log source unavailable

Notes:
  Read-only. Does not read .env files. Secrets are redacted on a best-effort basis.
  Output is bounded (no follow mode). Deeper investigation may still use manual SSH.
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

  local env mode
  env="$(require_ops_env "$1")"
  mode="$2"
  shift 2

  local lines=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --lines)
        shift
        lines="${1:-}"
        if [[ -z "$lines" ]]; then
          echo "Error: --lines requires a value" >&2
          exit 1
        fi
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
    echo "logs.sh: no usable Python interpreter found" >&2
    exit 1
  }

  local -a args=("$SCRIPT_DIR/logs_report.py" "$env" "$mode")
  if [[ -n "$lines" ]]; then
    args+=(--lines "$lines")
  fi

  exec "$python_bin" "${args[@]}"
}

main "$@"
