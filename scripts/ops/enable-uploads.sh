#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/enable-uploads.sh <environment> [--confirm]

Purpose:
  Clear the emergency runtime upload disable (does not force config upload on).

Environment (required):
  dev | prod

Options:
  --confirm     Required to enable uploads in production
  -h, --help    Show this help

Examples:
  ./scripts/ops/enable-uploads.sh dev
  ./scripts/ops/enable-uploads.sh prod --confirm

Notes:
  Sets data/<env>/control_state.json uploads_disabled=false.
  Does not trigger uploads, restart services, or edit Git-controlled config.
  If config uploading.enabled is false, effective real posting stays off.
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
  local args=(enable "$env")
  if [[ "$confirm" == true ]]; then
    args+=(--confirm)
  fi
  exec "$python_bin" "$SCRIPT_DIR/upload_control.py" "${args[@]}"
}

main "$@"
