#!/usr/bin/env bash
# Daily trigger for source-input. Designed to be the ONLY external trigger
# in autonomous mk1 operation. Cron line:
#
#   0 8 * * * /opt/mk04/prod/current/deploy/scripts/run-funnel-daily.sh prod mfm_business_ai_001
#
# Add one cron line per active funnel_id. Output is logged to journald via
# logger(1); failures send a non-zero exit so cron mails them.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_ARG="${1:-}"
if [[ "$ENV_ARG" == "dev" || "$ENV_ARG" == "prod" ]]; then
  shift
else
  ENV_ARG="${MK04_ENV:-prod}"
fi
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh" "$ENV_ARG"

INPUT_ROOT="$MK04_ROOT/source-input/input_service"
load_env_file "$MK04_SERVICE_ENV_DIR/source-input.env"
if [[ "$MK04_ENV" == "dev" ]]; then
  load_env_file_defaults "$INPUT_ROOT/.env"
fi
mk04_export_runtime

FUNNEL_ID="${1:-${RUN_FUNNEL_ID:-}}"
if [[ -z "$FUNNEL_ID" ]]; then
  echo "usage: run-funnel-daily.sh <funnel_id>" >&2
  exit 2
fi

INPUT_HOST="${INPUT_SERVICE_HOST:-127.0.0.1}"
[[ "$INPUT_HOST" == "0.0.0.0" || "$INPUT_HOST" == "::" ]] && INPUT_HOST="127.0.0.1"
INPUT_BASE_URL="http://${INPUT_HOST}:${INPUT_SERVICE_PORT}"

declare -a headers=(-H "Content-Type: application/json")
if [[ -n "${INPUT_SERVICE_SECRET:-}" ]]; then
  headers+=(-H "X-Input-Service-Secret: $INPUT_SERVICE_SECRET")
fi

LOG_TAG="mk04-${MK04_ENV}-daily-trigger"
log_info() {
  if command -v logger >/dev/null 2>&1; then
    logger -t "$LOG_TAG" -- "$@"
  fi
  echo "[$LOG_TAG] $*"
}

log_info "trigger env=$MK04_ENV funnel_id=$FUNNEL_ID url=$INPUT_BASE_URL/run-funnel"

response="$(curl -fsS -X POST "${headers[@]}" \
  -d "{\"funnel_id\":\"$FUNNEL_ID\"}" \
  "${INPUT_BASE_URL}/run-funnel")" || {
  log_info "FAIL run-funnel http error funnel_id=$FUNNEL_ID"
  exit 1
}

echo "$response"
status="$(echo "$response" | python3 -c "import json,sys; print((json.load(sys.stdin) or {}).get('status') or '')")" || true
log_info "result funnel_id=$FUNNEL_ID status=${status:-unknown}"

case "$status" in
  input_ready|no_input_available)
    exit 0
    ;;
  *)
    log_info "WARN unexpected status funnel_id=$FUNNEL_ID status=$status"
    exit 1
    ;;
esac
