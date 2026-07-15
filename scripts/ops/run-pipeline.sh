#!/usr/bin/env bash
# Shared production pipeline entrypoint (Reliability & Recovery Phase 6).
#
# All trigger sources must use this script:
#   scheduler, manual CLI, remote SSH, Operations UI
#
# Flow:
#   1. require explicit environment
#   2. load environment / service env (ports, secrets)
#   3. validate config
#   4. boot readiness (abort if NOT READY)
#   5. execution lock (overlap prevention)
#   6. run record (RUNNING after lock; terminal on all outcomes)
#   7. invoke existing POST /run-funnel path
#   8. capture logs under runs/<env>/<run_id>/run.log
#   9. release execution lock (always, via finally)
#
# Exit codes:
#   0  success (required jobs terminal success / no_input / scheduled skip)
#   1  pipeline execution failure
#   2  usage / invalid arguments
#   3  config validation failure
#   4  boot readiness NOT READY
#   5  execution lock held (active same-env run; skipped)
#   6  cross-env execution gate refused (e.g. production active/pending)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ops/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

REPO_ROOT="$(ops_repo_root)"

usage() {
  cat <<'EOF'
Usage: ./scripts/ops/run-pipeline.sh <environment> [options]

Purpose:
  Single supported entrypoint for production pipeline execution.
  Validates config, checks boot readiness, then invokes the existing
  source-input POST /run-funnel path. Does not duplicate pipeline logic.

Environment (required):
  dev | prod

Options:
  --funnel-id <id>   Funnel id for POST /run-funnel (or RUN_FUNNEL_ID)
  --trigger <source> scheduled|manual_cli|operations_ui|remote_ssh|test
                     (default: manual_cli)
  -h, --help         Show this help

Examples:
  ./scripts/ops/run-pipeline.sh dev --funnel-id mfm_business_ai_001
  ./scripts/ops/run-pipeline.sh prod --funnel-id mfm_business_ai_001 --trigger scheduled
  RUN_FUNNEL_ID=mfm_business_ai_001 ./scripts/ops/run-pipeline.sh prod

Exit codes:
  0  success
  1  pipeline failure
  2  usage error
  3  config validation failure
  4  boot readiness NOT READY
  5  execution lock held (overlapping or stale lock; run skipped)

Notes:
  Per-environment lock file: data/<env>/pipeline_execution.lock
  Stale locks are reported and block new runs but are not auto-deleted.
  Run records: runs/<env>/<run_id>/run_record.json (see docs/operations/RUN_RECORDS.md).
  Scheduler (Phase 9) must call this script rather than invoking /run-funnel directly.
EOF
}

main() {
  if [[ $# -eq 0 ]]; then
    usage >&2
    exit 2
  fi
  if is_help_flag "$1"; then
    usage
    exit 0
  fi

  local env funnel_id="" trigger="manual_cli"
  env="$(require_ops_env "$1")" || exit 2
  shift

  # Positional funnel_id for compatibility with run-funnel-daily.sh callers.
  if [[ $# -gt 0 && "$1" != -* ]]; then
    funnel_id="$1"
    shift
  fi

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --funnel-id)
        if [[ $# -lt 2 ]]; then
          echo "Error: --funnel-id requires a value" >&2
          exit 2
        fi
        funnel_id="$2"
        shift 2
        ;;
      --trigger)
        if [[ $# -lt 2 ]]; then
          echo "Error: --trigger requires a value" >&2
          exit 2
        fi
        trigger="$2"
        shift 2
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
  done

  if [[ -z "$funnel_id" && -n "${RUN_FUNNEL_ID:-}" ]]; then
    funnel_id="$RUN_FUNNEL_ID"
  fi
  if [[ -z "$funnel_id" ]]; then
    echo "Error: funnel_id required via argument, --funnel-id, or RUN_FUNNEL_ID" >&2
    usage >&2
    exit 2
  fi

  # Load deploy environment so INPUT_SERVICE_* ports/secrets match production.
  # shellcheck disable=SC1091
  source "$REPO_ROOT/deploy/scripts/env.sh" "$env"
  # Optional per-service overrides (secrets, non-default hosts).
  load_env_file "${MK04_SERVICE_ENV_DIR}/source-input.env"
  if [[ "$env" == "dev" ]]; then
    load_env_file_defaults "${MK04_ROOT}/source-input/input_service/.env"
  fi
  mk04_export_runtime

  local python_bin
  python_bin="$(ops_find_python)" || {
    echo "run-pipeline.sh: no usable Python interpreter found" >&2
    exit 1
  }

  exec "$python_bin" "$SCRIPT_DIR/run_pipeline.py" "$env" \
    --funnel-id "$funnel_id" \
    --trigger "$trigger"
}

main "$@"
