#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/health.sh <environment> [--boot-readiness]

Purpose:
  Deeper read-only health diagnostics for the selected environment.
  Answers whether the environment is safe and ready enough to operate.

Environment (required):
  dev | prod

Options:
  --boot-readiness
                Print the full health report, but exit using the Boot readiness
                contract (READY / NOT READY) instead of Overall PASS/WARN/FAIL.
  -h, --help    Show this help

Examples:
  ./scripts/ops/health.sh prod
  ./scripts/ops/health.sh prod --boot-readiness
  ./scripts/ops/health.sh dev

Exit codes:
  (default)
  0  Overall PASS
  1  Overall WARN
  2  Overall FAIL

  (--boot-readiness)
  0  Boot readiness READY
  1  Boot readiness READY with optional component warnings
  2  Boot readiness NOT READY (or missing/malformed boot result)

Notes:
  status.sh is the quick summary; health.sh runs deeper checks and prints a
  Boot Verification section (READY / NOT READY) for config, AI, API, worker,
  output funnel, operations UI, scheduler, database, and output paths.
  Required failures make overall FAIL; optional services (AI, ops-ui) may WARN
  without blocking READY. Execution lock state is reported (active/stale/none);
  queue state remains not yet available. The only write performed is a tiny
  temporary file under the canonical runtime data/cache path for writeability
  testing, removed immediately. Production never writes under current/releases.
EOF
}

_ops_env_sh_for() {
  local env="$1"
  local repo_root
  repo_root="$(ops_repo_root)"
  if [[ "$env" == "prod" ]]; then
    # Load production env via the logical deployment entry so path preflight
    # matches service runners (script root resolves to the active release).
    local prod_base="${MK04_PROD_BASE:-/opt/mk04/prod}"
    local candidate="${prod_base}/current/deploy/scripts/env.sh"
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
    echo "health.sh: production env.sh missing at $candidate (promote/install first)" >&2
    return 1
  fi
  printf '%s\n' "$repo_root/deploy/scripts/env.sh"
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

  local boot_readiness=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --boot-readiness)
        boot_readiness=1
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown option: $1" >&2
        usage >&2
        exit 2
        ;;
    esac
    shift
  done

  local env_sh
  env_sh="$(_ops_env_sh_for "$env")" || exit 2
  # shellcheck disable=SC1090
  source "$env_sh" "$env"

  local python_bin
  python_bin="$(ops_find_python)" || {
    echo "health.sh: no usable Python interpreter found" >&2
    exit 2
  }

  local -a args=("$SCRIPT_DIR/health_report.py" "$env")
  if [[ "$boot_readiness" -eq 1 ]]; then
    args+=(--boot-readiness)
  fi
  exec "$python_bin" "${args[@]}"
}

main "$@"
