#!/usr/bin/env bash
# Explicit environment startup wrapper (Prompt 8).
#
# Usage:
#   ./run.sh --env dev
#   ./run.sh --env prod
#   ./run.sh --env dev --check-only
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/ops/update_lib.sh
source "$ROOT/scripts/ops/update_lib.sh"

usage() {
  cat <<'EOF'
Usage: ./run.sh --env <environment> [options]

Environment (required):
  dev | development
  prod | production

Options:
  --check-only    Validate config and print summary; do not start services
  -h, --help      Show this help

Examples:
  ./run.sh --env dev
  ./run.sh --env prod
  ./run.sh --env dev --check-only
EOF
}

ENV_ARG=""
CHECK_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --check-only)
      CHECK_ONLY=1
      shift
      ;;
    --env)
      if [[ $# -lt 2 ]]; then
        echo "--env requires a value." >&2
        usage >&2
        exit 2
      fi
      ENV_ARG="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$ENV_ARG" ]]; then
  echo "--env is required." >&2
  usage >&2
  exit 2
fi

if ! MK04_ENV="$(normalize_mk04_env "$ENV_ARG")"; then
  usage >&2
  exit 2
fi
export MK04_ENV

CANONICAL_ENV="$(canonical_environment_name "$MK04_ENV")"
ENV_LABEL="$(environment_label "$MK04_ENV")"

if ! PYTHON="$(find_repo_python "$ROOT")"; then
  exit 1
fi
echo "Python interpreter: $PYTHON"
echo "Selected environment: $CANONICAL_ENV ($ENV_LABEL)"
echo "MK04_ENV=$MK04_ENV"

echo
echo "Running config validation before startup ..."
if ! "$PYTHON" "$ROOT/scripts/config/validate_config.py"; then
  echo "Startup aborted: config validation failed." >&2
  exit 1
fi

if ! "$PYTHON" "$ROOT/scripts/config/config_manager.py" \
  --env "$MK04_ENV" \
  --funnel business \
  --platform youtube \
  --print-summary; then
  echo "Startup aborted: ConfigManager summary failed." >&2
  exit 1
fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  echo
  echo "Run check-only: config validation PASS; services not started."
  exit 0
fi

RUN_ALL="$ROOT/deploy/scripts/run-all-local.sh"
if [[ ! -x "$RUN_ALL" ]]; then
  echo "Startup command not wired yet: $RUN_ALL not found or not executable." >&2
  exit 1
fi

echo
echo "Starting mk04 stack via deploy/scripts/run-all-local.sh $MK04_ENV ..."
exec "$RUN_ALL" "$MK04_ENV"
