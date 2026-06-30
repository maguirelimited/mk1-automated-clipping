#!/usr/bin/env bash
set -euo pipefail

_MK04_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_MK04_SCRIPT_ROOT="$(cd "$_MK04_SCRIPT_DIR/../.." && pwd)"
_MK04_ENV_ARG="${1:-}"

if [[ -n "$_MK04_ENV_ARG" && "$_MK04_ENV_ARG" != "dev" && "$_MK04_ENV_ARG" != "prod" ]]; then
  echo "Invalid mk04 environment: $_MK04_ENV_ARG (expected dev or prod)" >&2
  exit 2
fi

export MK04_ENV="${_MK04_ENV_ARG:-${MK04_ENV:-dev}}"
case "$MK04_ENV" in
  dev|prod) ;;
  *)
    echo "Invalid MK04_ENV=$MK04_ENV (expected dev or prod)" >&2
    exit 2
    ;;
esac

load_env_file() {
  local file="$1"
  if [[ -f "$file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$file"
    set +a
  fi
}

load_env_file_defaults() {
  local file="$1"
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
    local key="${line%%=*}"
    local value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ -z "${!key+x}" ]]; then
      value="${value#"${value%%[![:space:]]*}"}"
      value="${value%"${value##*[![:space:]]}"}"
      value="${value%\"}"
      value="${value#\"}"
      value="${value%\'}"
      value="${value#\'}"
      export "$key=$value"
    fi
  done < "$file"
}

require_command() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    return 1
  fi
}

mk04_banner() {
  local service="${1:-mk04}"
  cat <<EOF
================================================================
mk04 $service
environment:    $MK04_ENV
code root:      $MK04_ROOT
config root:    $MK04_CONFIG_ROOT
runtime root:   $MK04_RUNTIME_ROOT
log root:       $MK04_LOG_ROOT
upload mode:    ${MK04_UPLOAD_MODE:-dry_run}
scheduler mode: ${MK04_SCHEDULER_MODE:-unknown}
================================================================
EOF
}

_mk04_realpath() {
  if [[ -d "$1" ]]; then
    cd "$1" 2>/dev/null && pwd -P
    return
  fi
  local parent base
  parent="$(dirname "$1")"
  base="$(basename "$1")"
  if [[ -d "$parent" ]]; then
    printf '%s/%s\n' "$(cd "$parent" 2>/dev/null && pwd -P)" "$base"
    return
  fi
  printf '%s\n' "$1"
}

_mk04_export_service_urls() {
  local input_host="${INPUT_SERVICE_HOST:-127.0.0.1}"
  local video_host="${VIDEO_AUTOMATION_HOST:-127.0.0.1}"
  local output_host="${OUTPUT_FUNNEL_HOST:-127.0.0.1}"
  local ai_host="${AI_SERVICE_HOST:-127.0.0.1}"

  [[ "$input_host" == "0.0.0.0" || "$input_host" == "::" ]] && input_host="127.0.0.1"
  [[ "$video_host" == "0.0.0.0" || "$video_host" == "::" ]] && video_host="127.0.0.1"
  [[ "$output_host" == "0.0.0.0" || "$output_host" == "::" ]] && output_host="127.0.0.1"
  [[ "$ai_host" == "0.0.0.0" || "$ai_host" == "::" ]] && ai_host="127.0.0.1"

  export VIDEO_AUTOMATION_BASE_URL="${VIDEO_AUTOMATION_BASE_URL:-http://${video_host}:${VIDEO_AUTOMATION_PORT}}"
  export OUTPUT_FUNNEL_URL="${OUTPUT_FUNNEL_URL:-http://${output_host}:${OUTPUT_FUNNEL_PORT}}"
  export AI_SERVICE_URL="${AI_SERVICE_URL:-http://${ai_host}:${AI_SERVICE_PORT}}"
  export OPS_SOURCE_INPUT_URL="${OPS_SOURCE_INPUT_URL:-http://${input_host}:${INPUT_SERVICE_PORT}}"
  export OPS_VIDEO_AUTOMATION_URL="${OPS_VIDEO_AUTOMATION_URL:-http://${video_host}:${VIDEO_AUTOMATION_PORT}}"
  export OPS_OUTPUT_FUNNEL_URL="${OPS_OUTPUT_FUNNEL_URL:-http://${output_host}:${OUTPUT_FUNNEL_PORT}}"
}

_mk04_path_is_under() {
  local path="$(_mk04_realpath "$1")"
  local root="$(_mk04_realpath "$2")"
  [[ "$path" == "$root" || "$path" == "$root"/* ]]
}

_mk04_fail_prod_preflight() {
  echo "PROD preflight failed: $*" >&2
  exit 2
}

_mk04_assert_prod_path() {
  local name="$1"
  local value="$2"
  local root="$3"
  [[ -n "$value" ]] || _mk04_fail_prod_preflight "$name is empty"
  _mk04_path_is_under "$value" "$root" || _mk04_fail_prod_preflight "$name=$value must be under $root"
}

_mk04_assert_no_dev_reference() {
  local name="$1"
  local value="${2:-}"
  [[ -z "$value" ]] && return 0
  case "$value" in
    *"/dev/"*|*"/mk04/dev"*|*"127.0.0.1:5160"*|*"127.0.0.1:5150"*|*"127.0.0.1:5155"*|*"127.0.0.1:5170"*|*"127.0.0.1:5175"*|*"localhost:5160"*|*"localhost:5150"*|*"localhost:5155"*|*"localhost:5170"*|*"localhost:5175"*)
      _mk04_fail_prod_preflight "$name contains DEV path/port reference: $value"
      ;;
  esac
}

_mk04_assert_prod_url_port() {
  local name="$1"
  local value="$2"
  local expected_port="$3"
  [[ "$value" == *":$expected_port"* ]] || _mk04_fail_prod_preflight "$name=$value must use prod port $expected_port"
}

mk04_export_runtime() {
  local input_port video_port output_port ops_port ai_port scheduler_mode
  case "$MK04_ENV" in
    dev)
      input_port=5160
      video_port=5150
      output_port=5155
      ops_port=5170
      ai_port=5175
      scheduler_mode="manual"
      ;;
    prod)
      input_port=5060
      video_port=5050
      output_port=5055
      ops_port=5070
      ai_port=5075
      scheduler_mode="autonomous"
      ;;
  esac

  export MK04_ROOT="$MK04_CODE_ROOT"
  export MK04_SERVICE_ENV_DIR="${MK04_SERVICE_ENV_DIR:-$MK04_CONFIG_ROOT/services}"

  export INPUT_SERVICE_HOST="${INPUT_SERVICE_HOST:-127.0.0.1}"
  export INPUT_SERVICE_PORT="${INPUT_SERVICE_PORT:-$input_port}"
  export VIDEO_AUTOMATION_HOST="${VIDEO_AUTOMATION_HOST:-127.0.0.1}"
  export VIDEO_AUTOMATION_PORT="${VIDEO_AUTOMATION_PORT:-$video_port}"
  export OUTPUT_FUNNEL_HOST="${OUTPUT_FUNNEL_HOST:-127.0.0.1}"
  export OUTPUT_FUNNEL_PORT="${OUTPUT_FUNNEL_PORT:-$output_port}"
  export OPS_UI_HOST="${OPS_UI_HOST:-127.0.0.1}"
  export OPS_UI_PORT="${OPS_UI_PORT:-$ops_port}"
  export AI_SERVICE_HOST="${AI_SERVICE_HOST:-127.0.0.1}"
  export AI_SERVICE_PORT="${AI_SERVICE_PORT:-$ai_port}"

  export INPUT_SERVICE_ROOT="$MK04_CODE_ROOT/source-input/input_service"
  export INPUT_SERVICE_CONFIG_DIR="${INPUT_SERVICE_CONFIG_DIR:-$MK04_CONFIG_ROOT/source-input}"
  export INPUT_SERVICE_DATA_DIR="${INPUT_SERVICE_DATA_DIR:-$MK04_RUNTIME_ROOT/source-input}"
  export INPUT_JOB_LEDGER_DIR="${INPUT_JOB_LEDGER_DIR:-$INPUT_SERVICE_DATA_DIR/state/input_jobs}"
  export VIDEO_AUTOMATION_PROJECT_ROOT="$MK04_CODE_ROOT"
  export VIDEO_AUTOMATION_INPUT_DIR="${VIDEO_AUTOMATION_INPUT_DIR:-$MK04_RUNTIME_ROOT/video-automation/input}"

  export PIPELINE_CONFIG_PATH="${PIPELINE_CONFIG_PATH:-$MK04_CONFIG_ROOT/video-automation/pipeline_config.json}"
  export VIDEO_PIPELINE_PROFILES_PATH="${VIDEO_PIPELINE_PROFILES_PATH:-$MK04_CONFIG_ROOT/video-automation/video_pipeline_profiles.json}"
  export VIDEO_FUNNELS_CONFIG_DIR="${VIDEO_FUNNELS_CONFIG_DIR:-$MK04_CONFIG_ROOT/video-automation/funnels}"
  export FUNNEL_CONFIG_DIR="${FUNNEL_CONFIG_DIR:-$VIDEO_FUNNELS_CONFIG_DIR}"
  export DEBUG_LOG_PATH="${DEBUG_LOG_PATH:-$MK04_LOG_ROOT/video-automation/debug.log}"
  export AGENT_DEBUG_LOG_PATH="${AGENT_DEBUG_LOG_PATH:-$MK04_LOG_ROOT/video-automation/agent-debug.ndjson}"
  export DEBUG_MODE_LOG_PATH="${DEBUG_MODE_LOG_PATH:-$MK04_LOG_ROOT/video-automation/debug-mode.ndjson}"
  export PIPELINE_DIAGNOSTIC_LOG_PATH="${PIPELINE_DIAGNOSTIC_LOG_PATH:-$MK04_LOG_ROOT/video-automation/diagnostic.ndjson}"

  export OUTPUT_FUNNEL_CONFIG_DIR="${OUTPUT_FUNNEL_CONFIG_DIR:-$MK04_CONFIG_ROOT/output-funnel}"
  export OUTPUT_FUNNEL_SETTINGS="${OUTPUT_FUNNEL_SETTINGS:-$OUTPUT_FUNNEL_CONFIG_DIR/settings.json}"
  export OUTPUT_FUNNEL_CHANNELS="${OUTPUT_FUNNEL_CHANNELS:-$OUTPUT_FUNNEL_CONFIG_DIR/channels.json}"
  export OUTPUT_FUNNEL_DB="${OUTPUT_FUNNEL_DB:-$MK04_RUNTIME_ROOT/output-funnel/output_funnel.sqlite3}"
  export MK04_UPLOAD_MODE="${MK04_UPLOAD_MODE:-dry_run}"

  export OPS_UI_DATA_DIR="${OPS_UI_DATA_DIR:-$MK04_RUNTIME_ROOT/ops-ui}"
  export OPS_UI_DB="${OPS_UI_DB:-$OPS_UI_DATA_DIR/ops_ui.sqlite3}"
  export MK04_CONTROLS_FILE="${MK04_CONTROLS_FILE:-$OPS_UI_DATA_DIR/controls.json}"
  export OPS_INPUT_LEDGER_DIR="${OPS_INPUT_LEDGER_DIR:-$INPUT_JOB_LEDGER_DIR}"
  export OPS_OUTPUT_FUNNEL_DB="${OPS_OUTPUT_FUNNEL_DB:-$OUTPUT_FUNNEL_DB}"
  export OPS_UI_LOG_DIR="${OPS_UI_LOG_DIR:-$MK04_LOG_ROOT/ops-ui}"

  export WATCHDOG_LOG_DIR="${WATCHDOG_LOG_DIR:-$MK04_LOG_ROOT/watchdog}"
  export MK04_SCHEDULER_MODE="${MK04_SCHEDULER_MODE:-$scheduler_mode}"

  if [[ "$MK04_ENV" == "prod" ]]; then
    export OUTPUT_FUNNEL_PLAN_WORKER_ENABLED="${OUTPUT_FUNNEL_PLAN_WORKER_ENABLED:-1}"
    export OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED="${OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED:-1}"
  else
    export OUTPUT_FUNNEL_PLAN_WORKER_ENABLED="${OUTPUT_FUNNEL_PLAN_WORKER_ENABLED:-0}"
    export OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED="${OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED:-0}"
  fi
  export OUTPUT_FUNNEL_AUTO_SCHEDULE="${OUTPUT_FUNNEL_AUTO_SCHEDULE:-1}"
  export OUTPUT_FUNNEL_AUTO_UPLOAD="${OUTPUT_FUNNEL_AUTO_UPLOAD:-0}"
  export OUTPUT_FUNNEL_AUTO_SCHEDULE_LIMIT="${OUTPUT_FUNNEL_AUTO_SCHEDULE_LIMIT:-50}"
  export OUTPUT_FUNNEL_AUTO_UPLOAD_LIMIT="${OUTPUT_FUNNEL_AUTO_UPLOAD_LIMIT:-1}"
  export OUTPUT_FUNNEL_PLAN_WORKER_INTERVAL="${OUTPUT_FUNNEL_PLAN_WORKER_INTERVAL:-300}"
  export OUTPUT_FUNNEL_UPLOAD_WORKER_INTERVAL="${OUTPUT_FUNNEL_UPLOAD_WORKER_INTERVAL:-60}"

  _mk04_export_service_urls

  if [[ "$MK04_UPLOAD_MODE" == "real" && "$MK04_ENV" != "prod" ]]; then
    echo "Refusing MK04_UPLOAD_MODE=real outside MK04_ENV=prod (current: $MK04_ENV)" >&2
    exit 2
  fi
}

mk04_prod_preflight() {
  [[ "$MK04_ENV" == "prod" ]] || return 0

  local script_root_real code_root_real
  script_root_real="$(_mk04_realpath "$_MK04_SCRIPT_ROOT")"
  code_root_real="$(_mk04_realpath "$MK04_CODE_ROOT")"

  [[ "$code_root_real" == "/opt/mk04/prod/current" ]] || _mk04_fail_prod_preflight "MK04_CODE_ROOT must resolve to /opt/mk04/prod/current (got $code_root_real)"
  [[ "$script_root_real" == "$code_root_real" ]] || _mk04_fail_prod_preflight "run prod only from deployed copy at $MK04_CODE_ROOT (script root: $_MK04_SCRIPT_ROOT)"
  case "$code_root_real" in
    /Users/*) _mk04_fail_prod_preflight "refusing active user checkout: $code_root_real" ;;
  esac

  _mk04_assert_prod_path MK04_CODE_ROOT "$MK04_CODE_ROOT" "/opt/mk04/prod/current"
  _mk04_assert_prod_path MK04_ROOT "$MK04_ROOT" "/opt/mk04/prod/current"
  _mk04_assert_prod_path MK04_CONFIG_ROOT "$MK04_CONFIG_ROOT" "/etc/mk04/prod"
  _mk04_assert_prod_path MK04_RUNTIME_ROOT "$MK04_RUNTIME_ROOT" "/var/lib/mk04/prod"
  _mk04_assert_prod_path MK04_LOG_ROOT "$MK04_LOG_ROOT" "/var/log/mk04/prod"
  _mk04_assert_prod_path INPUT_SERVICE_ROOT "$INPUT_SERVICE_ROOT" "/opt/mk04/prod/current"
  _mk04_assert_prod_path INPUT_SERVICE_CONFIG_DIR "$INPUT_SERVICE_CONFIG_DIR" "/etc/mk04/prod"
  _mk04_assert_prod_path INPUT_SERVICE_DATA_DIR "$INPUT_SERVICE_DATA_DIR" "/var/lib/mk04/prod"
  _mk04_assert_prod_path INPUT_JOB_LEDGER_DIR "$INPUT_JOB_LEDGER_DIR" "/var/lib/mk04/prod"
  _mk04_assert_prod_path VIDEO_AUTOMATION_PROJECT_ROOT "$VIDEO_AUTOMATION_PROJECT_ROOT" "/opt/mk04/prod/current"
  _mk04_assert_prod_path VIDEO_AUTOMATION_INPUT_DIR "$VIDEO_AUTOMATION_INPUT_DIR" "/var/lib/mk04/prod"
  _mk04_assert_prod_path PIPELINE_CONFIG_PATH "$PIPELINE_CONFIG_PATH" "/etc/mk04/prod"
  _mk04_assert_prod_path VIDEO_PIPELINE_PROFILES_PATH "$VIDEO_PIPELINE_PROFILES_PATH" "/etc/mk04/prod"
  _mk04_assert_prod_path VIDEO_FUNNELS_CONFIG_DIR "$VIDEO_FUNNELS_CONFIG_DIR" "/etc/mk04/prod"
  _mk04_assert_prod_path FUNNEL_CONFIG_DIR "$FUNNEL_CONFIG_DIR" "/etc/mk04/prod"
  _mk04_assert_prod_path DEBUG_LOG_PATH "$DEBUG_LOG_PATH" "/var/log/mk04/prod"
  _mk04_assert_prod_path AGENT_DEBUG_LOG_PATH "$AGENT_DEBUG_LOG_PATH" "/var/log/mk04/prod"
  _mk04_assert_prod_path DEBUG_MODE_LOG_PATH "$DEBUG_MODE_LOG_PATH" "/var/log/mk04/prod"
  _mk04_assert_prod_path PIPELINE_DIAGNOSTIC_LOG_PATH "$PIPELINE_DIAGNOSTIC_LOG_PATH" "/var/log/mk04/prod"
  _mk04_assert_prod_path OUTPUT_FUNNEL_CONFIG_DIR "$OUTPUT_FUNNEL_CONFIG_DIR" "/etc/mk04/prod"
  _mk04_assert_prod_path OUTPUT_FUNNEL_SETTINGS "$OUTPUT_FUNNEL_SETTINGS" "/etc/mk04/prod"
  _mk04_assert_prod_path OUTPUT_FUNNEL_CHANNELS "$OUTPUT_FUNNEL_CHANNELS" "/etc/mk04/prod"
  _mk04_assert_prod_path OUTPUT_FUNNEL_DB "$OUTPUT_FUNNEL_DB" "/var/lib/mk04/prod"
  _mk04_assert_prod_path OPS_UI_DATA_DIR "$OPS_UI_DATA_DIR" "/var/lib/mk04/prod"
  _mk04_assert_prod_path OPS_UI_DB "$OPS_UI_DB" "/var/lib/mk04/prod"
  _mk04_assert_prod_path MK04_CONTROLS_FILE "$MK04_CONTROLS_FILE" "/var/lib/mk04/prod"
  _mk04_assert_prod_path OPS_INPUT_LEDGER_DIR "$OPS_INPUT_LEDGER_DIR" "/var/lib/mk04/prod"
  _mk04_assert_prod_path OPS_OUTPUT_FUNNEL_DB "$OPS_OUTPUT_FUNNEL_DB" "/var/lib/mk04/prod"
  _mk04_assert_prod_path OPS_UI_LOG_DIR "$OPS_UI_LOG_DIR" "/var/log/mk04/prod"
  _mk04_assert_prod_path WATCHDOG_LOG_DIR "$WATCHDOG_LOG_DIR" "/var/log/mk04/prod"

  local name
  for name in \
    MK04_CODE_ROOT MK04_ROOT MK04_CONFIG_ROOT MK04_RUNTIME_ROOT MK04_LOG_ROOT \
    INPUT_SERVICE_ROOT INPUT_SERVICE_CONFIG_DIR INPUT_SERVICE_DATA_DIR INPUT_JOB_LEDGER_DIR \
    VIDEO_AUTOMATION_PROJECT_ROOT VIDEO_AUTOMATION_INPUT_DIR PIPELINE_CONFIG_PATH VIDEO_PIPELINE_PROFILES_PATH \
    VIDEO_FUNNELS_CONFIG_DIR FUNNEL_CONFIG_DIR OUTPUT_FUNNEL_CONFIG_DIR OUTPUT_FUNNEL_SETTINGS \
    OUTPUT_FUNNEL_CHANNELS OUTPUT_FUNNEL_DB OPS_UI_DATA_DIR OPS_UI_DB MK04_CONTROLS_FILE \
    OPS_INPUT_LEDGER_DIR OPS_OUTPUT_FUNNEL_DB OPS_UI_LOG_DIR WATCHDOG_LOG_DIR \
    VIDEO_AUTOMATION_BASE_URL OUTPUT_FUNNEL_URL AI_SERVICE_URL OPS_SOURCE_INPUT_URL OPS_VIDEO_AUTOMATION_URL OPS_OUTPUT_FUNNEL_URL
  do
    _mk04_assert_no_dev_reference "$name" "${!name:-}"
  done

  [[ "$INPUT_SERVICE_PORT" == "5060" ]] || _mk04_fail_prod_preflight "INPUT_SERVICE_PORT must be 5060 (got $INPUT_SERVICE_PORT)"
  [[ "$VIDEO_AUTOMATION_PORT" == "5050" ]] || _mk04_fail_prod_preflight "VIDEO_AUTOMATION_PORT must be 5050 (got $VIDEO_AUTOMATION_PORT)"
  [[ "$OUTPUT_FUNNEL_PORT" == "5055" ]] || _mk04_fail_prod_preflight "OUTPUT_FUNNEL_PORT must be 5055 (got $OUTPUT_FUNNEL_PORT)"
  [[ "$OPS_UI_PORT" == "5070" ]] || _mk04_fail_prod_preflight "OPS_UI_PORT must be 5070 (got $OPS_UI_PORT)"
  [[ "$AI_SERVICE_PORT" == "5075" ]] || _mk04_fail_prod_preflight "AI_SERVICE_PORT must be 5075 (got $AI_SERVICE_PORT)"
  _mk04_assert_prod_url_port VIDEO_AUTOMATION_BASE_URL "$VIDEO_AUTOMATION_BASE_URL" 5050
  _mk04_assert_prod_url_port OUTPUT_FUNNEL_URL "$OUTPUT_FUNNEL_URL" 5055
  _mk04_assert_prod_url_port AI_SERVICE_URL "$AI_SERVICE_URL" 5075
  _mk04_assert_prod_url_port OPS_SOURCE_INPUT_URL "$OPS_SOURCE_INPUT_URL" 5060
  _mk04_assert_prod_url_port OPS_VIDEO_AUTOMATION_URL "$OPS_VIDEO_AUTOMATION_URL" 5050
  _mk04_assert_prod_url_port OPS_OUTPUT_FUNNEL_URL "$OPS_OUTPUT_FUNNEL_URL" 5055

  [[ -f "$OUTPUT_FUNNEL_SETTINGS" ]] || _mk04_fail_prod_preflight "missing OUTPUT_FUNNEL_SETTINGS: $OUTPUT_FUNNEL_SETTINGS"
  [[ -f "$OUTPUT_FUNNEL_CHANNELS" ]] || _mk04_fail_prod_preflight "missing OUTPUT_FUNNEL_CHANNELS: $OUTPUT_FUNNEL_CHANNELS"
  [[ -f "$PIPELINE_CONFIG_PATH" ]] || _mk04_fail_prod_preflight "missing PIPELINE_CONFIG_PATH: $PIPELINE_CONFIG_PATH"
  [[ -f "$VIDEO_PIPELINE_PROFILES_PATH" ]] || _mk04_fail_prod_preflight "missing VIDEO_PIPELINE_PROFILES_PATH: $VIDEO_PIPELINE_PROFILES_PATH"
  [[ -d "$FUNNEL_CONFIG_DIR" ]] || _mk04_fail_prod_preflight "missing FUNNEL_CONFIG_DIR: $FUNNEL_CONFIG_DIR"

  if [[ "$MK04_UPLOAD_MODE" != "dry_run" && "$MK04_UPLOAD_MODE" != "real" ]]; then
    _mk04_fail_prod_preflight "MK04_UPLOAD_MODE must be dry_run or real (got $MK04_UPLOAD_MODE)"
  fi
}

case "$MK04_ENV" in
  dev)
    export MK04_CODE_ROOT="${MK04_CODE_ROOT:-$_MK04_SCRIPT_ROOT}"
    ;;
  prod)
    export MK04_CODE_ROOT="${MK04_CODE_ROOT:-/opt/mk04/prod/current}"
    ;;
esac

export MK04_CONFIG_ROOT="${MK04_CONFIG_ROOT:-/etc/mk04/$MK04_ENV}"
export MK04_RUNTIME_ROOT="${MK04_RUNTIME_ROOT:-/var/lib/mk04/$MK04_ENV}"
export MK04_LOG_ROOT="${MK04_LOG_ROOT:-/var/log/mk04/$MK04_ENV}"

load_env_file "$MK04_CONFIG_ROOT/env"

if [[ "$MK04_ENV" == "prod" ]]; then
  _script_root_real="$(_mk04_realpath "$_MK04_SCRIPT_ROOT")"
  _code_root_real="$(_mk04_realpath "$MK04_CODE_ROOT")"
  if [[ "$_script_root_real" != "$_code_root_real" ]]; then
    echo "Refusing prod runtime from $_MK04_SCRIPT_ROOT; run deployed copy at $MK04_CODE_ROOT" >&2
    exit 2
  fi
  case "$_code_root_real" in
    /Users/*)
      echo "Refusing prod runtime from active user checkout: $_code_root_real" >&2
      exit 2
      ;;
  esac
fi

mk04_export_runtime
if [[ "${MK04_SKIP_PROD_PREFLIGHT:-0}" != "1" ]]; then
  mk04_prod_preflight
fi
