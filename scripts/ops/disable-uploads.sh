#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/disable-uploads.sh <environment>

Purpose:
  Disable runtime uploads via environment-scoped control state (not Git config).

Environment (required):
  dev | prod

Options:
  -h, --help    Show this help

Examples:
  ./scripts/ops/disable-uploads.sh dev
  ./scripts/ops/disable-uploads.sh prod

Notes:
  Sets data/<env>/control_state.json uploads_disabled=true.
  Does not stop processing, delete clips, or edit Git-controlled config.
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
  exec "$python_bin" "$SCRIPT_DIR/upload_control.py" disable "$env"
}

main "$@"
