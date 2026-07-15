#!/usr/bin/env bash
# Thin scheduled-pipeline trigger (Reliability & Recovery Phase 9).
#
# The scheduler's only job is to invoke the shared pipeline entrypoint.
# It does not implement readiness, locking, run records, or pipeline logic.
#
# Cron (example):
#   0 8 * * * /opt/mk04/prod/current/scripts/ops/run-scheduled.sh prod mfm_business_ai_001
#
# Equivalent:
#   ./scripts/ops/run-pipeline.sh prod --funnel-id <id> --trigger scheduled
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/run-scheduled.sh <environment> <funnel_id>

Purpose:
  Scheduler trigger only. Delegates entirely to run-pipeline.sh with
  --trigger scheduled. Do not call pipeline internals from cron.

Environment (required):
  dev | prod

Arguments:
  funnel_id   Source-input funnel id (same as POST /run-funnel)

Examples:
  ./scripts/ops/run-scheduled.sh prod mfm_business_ai_001
  ./scripts/ops/run-scheduled.sh dev mfm_business_ai_001

Exit codes:
  Propagated from run-pipeline.sh (0 success/skip, 1 pipeline fail,
  2 usage, 3 config, 4 not ready, 5 lock held).

Notes:
  Runtime scheduler disable, readiness, execution lock, and run records are
  enforced inside run-pipeline.sh — not here.
EOF
}

main() {
  if [[ $# -eq 0 ]] || is_help_flag "${1:-}"; then
    usage
    [[ $# -eq 0 ]] && exit 2
    exit 0
  fi

  local env funnel_id
  env="$(require_ops_env "$1")" || exit 2
  shift

  funnel_id="${1:-${RUN_FUNNEL_ID:-}}"
  if [[ -z "$funnel_id" ]]; then
    echo "Error: funnel_id required" >&2
    usage >&2
    exit 2
  fi

  local log_tag="mk04-${env}-scheduler"
  if command -v logger >/dev/null 2>&1; then
    logger -t "$log_tag" -- "scheduled trigger env=$env funnel_id=$funnel_id entrypoint=run-pipeline.sh"
  fi
  echo "[$log_tag] scheduled trigger env=$env funnel_id=$funnel_id"

  # Single execution path — never call Python pipeline modules directly.
  exec "$SCRIPT_DIR/run-pipeline.sh" "$env" \
    --funnel-id "$funnel_id" \
    --trigger scheduled
}

main "$@"
