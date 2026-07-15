#!/usr/bin/env bash
set -euo pipefail

_MK04_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_MK04_SCRIPT_ROOT="$(cd "$_MK04_SCRIPT_DIR/../.." && pwd)"
_MK04_ENV_ARG="${1:-}"

# Canonical runtime environment tokens: dev | prod.
# Accepts aliases development|production and normalizes immediately.
normalize_mk04_runtime_env() {
  local raw="${1:-}"
  case "${raw,,}" in
    dev|development)
      echo "dev"
      ;;
    prod|production)
      echo "prod"
      ;;
    *)
      echo "Invalid environment: ${raw:-<missing>}. Expected dev, development, prod, or production." >&2
      return 1
      ;;
  esac
}

if [[ -n "$_MK04_ENV_ARG" ]]; then
  MK04_ENV="$(normalize_mk04_runtime_env "$_MK04_ENV_ARG")" || exit 2
elif [[ -n "${MK04_ENV:-}" ]]; then
  MK04_ENV="$(normalize_mk04_runtime_env "$MK04_ENV")" || exit 2
else
  # Intentional documented library default for deploy helpers: development.
  # Never defaults to production.
  MK04_ENV="dev"
fi
export MK04_ENV

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

# Production layout roots. MK04_PROD_BASE is a hermetic test injection hook only;
# production defaults remain /opt/mk04/prod. Not a safety bypass.
: "${MK04_PROD_BASE:=/opt/mk04/prod}"
_MK04_PROD_CURRENT="${MK04_PROD_BASE%/}/current"
_MK04_PROD_RELEASES="${MK04_PROD_BASE%/}/releases"

_mk04_resolve_active_release() {
  # Physical active release: resolved target of the logical current symlink.
  # Fails closed unless current is a symlink into ${_MK04_PROD_RELEASES}/<id>.
  local current="$_MK04_PROD_CURRENT"
  local releases_root="$_MK04_PROD_RELEASES"
  local releases=""
  local target=""

  [[ -e "$current" || -L "$current" ]] || return 1
  [[ -L "$current" ]] || return 2
  [[ -d "$releases_root" ]] || return 4
  releases="$(_mk04_realpath "$releases_root")"
  if ! target="$(readlink -f "$current" 2>/dev/null)"; then
    return 3
  fi
  [[ -n "$target" && -d "$target" ]] || return 3
  case "$target" in
    "$releases"/*)
      # Reject the releases directory itself; require a single release child.
      local rel="${target#"$releases"/}"
      [[ -n "$rel" && "$rel" != *"/"* ]] || return 4
      printf '%s\n' "$target"
      return 0
      ;;
    *)
      return 5
      ;;
  esac
}

_mk04_assert_prod_deployment_root() {
  # Logical entry: ${_MK04_PROD_CURRENT}
  # Physical active release: resolved current target under ${_MK04_PROD_RELEASES}/
  # Executing code (script root + MK04_CODE_ROOT) must resolve to that exact target.
  local active=""
  local rc=0
  local code_logical code_real script_real

  active="$(_mk04_resolve_active_release)" && rc=0 || rc=$?
  case "$rc" in
    0) ;;
    1) _mk04_fail_prod_preflight "missing production current entry: $_MK04_PROD_CURRENT" ;;
    2) _mk04_fail_prod_preflight "$_MK04_PROD_CURRENT must be a symlink (got a real directory or non-link)" ;;
    3) _mk04_fail_prod_preflight "broken or unresolvable production current symlink: $_MK04_PROD_CURRENT" ;;
    4) _mk04_fail_prod_preflight "production current must point at a release directory under $_MK04_PROD_RELEASES/" ;;
    5) _mk04_fail_prod_preflight "production current symlink must target under $_MK04_PROD_RELEASES/ (got $(readlink -f "$_MK04_PROD_CURRENT" 2>/dev/null || readlink "$_MK04_PROD_CURRENT" 2>/dev/null || echo unknown))" ;;
    *) _mk04_fail_prod_preflight "unable to resolve active production release (rc=$rc)" ;;
  esac

  code_logical="${MK04_CODE_ROOT%/}"
  [[ "$code_logical" == "$_MK04_PROD_CURRENT" ]] || _mk04_fail_prod_preflight \
    "MK04_CODE_ROOT must be the logical deployment entry $_MK04_PROD_CURRENT (got ${MK04_CODE_ROOT:-<empty>})"

  code_real="$(_mk04_realpath "$MK04_CODE_ROOT")"
  script_real="$(_mk04_realpath "$_MK04_SCRIPT_ROOT")"

  [[ "$code_real" == "$active" ]] || _mk04_fail_prod_preflight \
    "MK04_CODE_ROOT physical path must be the active release $active (got $code_real)"
  [[ "$script_real" == "$active" ]] || _mk04_fail_prod_preflight \
    "run prod only from the active release via $_MK04_PROD_CURRENT (script root physical: $script_real; active: $active)"

  case "$code_real" in
    /Users/*|/home/*)
      _mk04_fail_prod_preflight "refusing active user checkout: $code_real"
      ;;
  esac

  # Export for callers/diagnostics (non-secret).
  export MK04_ACTIVE_RELEASE="$active"
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
      # First-bootstrap / unscheduled default. Cron install is deliberate and separate.
      scheduler_mode="manual"
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

  # Derived transport mirror of YAML uploading.enabled — never an operator override.
  _mk04_py="${PYTHON_BIN:-$MK04_CODE_ROOT/video-automation/.venv/bin/python}"
  [[ -x "$_mk04_py" ]] || _mk04_py="$(command -v python3 2>/dev/null || true)"
  _mk04_upload_cfg="false"
  if [[ -n "$_mk04_py" ]]; then
    _mk04_upload_cfg="$("$_mk04_py" "$MK04_CODE_ROOT/scripts/config/config_manager.py" \
      --env "$MK04_ENV" --print-json 2>/dev/null | "$_mk04_py" -c \
      'import json,sys
try:
    d=json.load(sys.stdin)
    print("true" if d.get("uploading_enabled") else "false")
except Exception:
    print("false")' 2>/dev/null || echo false)"
  fi
  export MK04_CONFIG_UPLOAD_ENABLED="${_mk04_upload_cfg:-false}"
  unset _mk04_py _mk04_upload_cfg

  export OPS_UI_DATA_DIR="${OPS_UI_DATA_DIR:-$MK04_RUNTIME_ROOT/ops-ui}"
  export OPS_UI_DB="${OPS_UI_DB:-$OPS_UI_DATA_DIR/ops_ui.sqlite3}"
  export MK04_CONTROLS_FILE="${MK04_CONTROLS_FILE:-$OPS_UI_DATA_DIR/controls.json}"
  export OPS_INPUT_LEDGER_DIR="${OPS_INPUT_LEDGER_DIR:-$INPUT_JOB_LEDGER_DIR}"
  export OPS_OUTPUT_FUNNEL_DB="${OPS_OUTPUT_FUNNEL_DB:-$OUTPUT_FUNNEL_DB}"
  export OPS_UI_LOG_DIR="${OPS_UI_LOG_DIR:-$MK04_LOG_ROOT/ops-ui}"

  # Canonical mutable-state path authority (Prompt 3).
  # Production: all mutable categories under /var/lib (logs under /var/log).
  # Development hybrid: jobs/outputs under runtime; orchestration under code root.
  export MK04_JOBS_ROOT="${MK04_JOBS_ROOT:-$MK04_RUNTIME_ROOT/video-automation/jobs}"
  export MK04_OUTPUTS_ROOT="${MK04_OUTPUTS_ROOT:-$MK04_RUNTIME_ROOT/video-automation/output}"
  if [[ "$MK04_ENV" == "prod" ]]; then
    export MK04_DATA_ROOT="${MK04_DATA_ROOT:-$MK04_RUNTIME_ROOT/data}"
    export MK04_RUNS_ROOT="${MK04_RUNS_ROOT:-$MK04_RUNTIME_ROOT/runs}"
    export MK04_REPORTS_ROOT="${MK04_REPORTS_ROOT:-$MK04_RUNTIME_ROOT/reports}"
    export MK04_DATABASE_PATH="${MK04_DATABASE_PATH:-$MK04_RUNTIME_ROOT/database/prod.db}"
    export MK04_REQUIRE_RUNTIME_PATHS="${MK04_REQUIRE_RUNTIME_PATHS:-1}"
  else
    export MK04_DATA_ROOT="${MK04_DATA_ROOT:-$MK04_CODE_ROOT/data/dev}"
    export MK04_RUNS_ROOT="${MK04_RUNS_ROOT:-$MK04_CODE_ROOT/runs/dev}"
    export MK04_REPORTS_ROOT="${MK04_REPORTS_ROOT:-$MK04_CODE_ROOT/reports/dev}"
    export MK04_DATABASE_PATH="${MK04_DATABASE_PATH:-$MK04_CODE_ROOT/database/dev.db}"
  fi
  export MK04_LOGS_ROOT="${MK04_LOGS_ROOT:-$MK04_LOG_ROOT}"
  export MK04_CONTROL_STATE_FILE="${MK04_CONTROL_STATE_FILE:-$MK04_DATA_ROOT/control_state.json}"
  export AI_SERVICE_LOG_DIR="${AI_SERVICE_LOG_DIR:-$MK04_LOG_ROOT/ai-service}"

  # Cross-environment execution gate (Prompt 4).
  # Preserve an operator-supplied MK04_SHARED_LOCK_ROOT.
  # Production defaults to the deployed shared root (preflight verifies it).
  # Development: use the deployed root only when it already exists and is writable;
  # otherwise leave unset so Python may use $MK04_CODE_ROOT/.mk04_locks before bootstrap.
  #
  # MK04_DEPLOYED_LOCK_ROOT is a path-injection hook for hermetic tests (defaults to
  # /var/lib/mk04/locks). It only affects the *dev* "is deployed root usable?" probe —
  # it is not a production safety bypass; prod still hard-defaults to /var/lib/mk04/locks.
  if [[ -n "${MK04_SHARED_LOCK_ROOT:-}" ]]; then
    export MK04_SHARED_LOCK_ROOT
  elif [[ "$MK04_ENV" == "prod" ]]; then
    export MK04_SHARED_LOCK_ROOT="/var/lib/mk04/locks"
  else
    _mk04_deployed_lock_root="${MK04_DEPLOYED_LOCK_ROOT:-/var/lib/mk04/locks}"
    if [[ -d "$_mk04_deployed_lock_root" && -w "$_mk04_deployed_lock_root" ]]; then
      export MK04_SHARED_LOCK_ROOT="$_mk04_deployed_lock_root"
    else
      unset MK04_SHARED_LOCK_ROOT || true
    fi
  fi

  export WATCHDOG_LOG_DIR="${WATCHDOG_LOG_DIR:-$MK04_LOG_ROOT/watchdog}"
  export MK04_SCHEDULER_MODE="${MK04_SCHEDULER_MODE:-$scheduler_mode}"

  # Safe first-bootstrap defaults: plan and upload workers off until deliberately armed.
  export OUTPUT_FUNNEL_PLAN_WORKER_ENABLED="${OUTPUT_FUNNEL_PLAN_WORKER_ENABLED:-0}"
  export OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED="${OUTPUT_FUNNEL_UPLOAD_WORKER_ENABLED:-0}"
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

  _mk04_assert_prod_deployment_root

  _mk04_assert_prod_path MK04_CODE_ROOT "$MK04_CODE_ROOT" "$_MK04_PROD_CURRENT"
  _mk04_assert_prod_path MK04_ROOT "$MK04_ROOT" "$_MK04_PROD_CURRENT"
  _mk04_assert_prod_path MK04_CONFIG_ROOT "$MK04_CONFIG_ROOT" "/etc/mk04/prod"
  _mk04_assert_prod_path MK04_RUNTIME_ROOT "$MK04_RUNTIME_ROOT" "/var/lib/mk04/prod"
  _mk04_assert_prod_path MK04_LOG_ROOT "$MK04_LOG_ROOT" "/var/log/mk04/prod"
  _mk04_assert_prod_path MK04_DATA_ROOT "$MK04_DATA_ROOT" "/var/lib/mk04/prod"
  _mk04_assert_prod_path MK04_JOBS_ROOT "$MK04_JOBS_ROOT" "/var/lib/mk04/prod"
  _mk04_assert_prod_path MK04_OUTPUTS_ROOT "$MK04_OUTPUTS_ROOT" "/var/lib/mk04/prod"
  _mk04_assert_prod_path MK04_RUNS_ROOT "$MK04_RUNS_ROOT" "/var/lib/mk04/prod"
  _mk04_assert_prod_path MK04_REPORTS_ROOT "$MK04_REPORTS_ROOT" "/var/lib/mk04/prod"
  _mk04_assert_prod_path MK04_CONTROL_STATE_FILE "$MK04_CONTROL_STATE_FILE" "/var/lib/mk04/prod"
  _mk04_assert_prod_path AI_SERVICE_LOG_DIR "$AI_SERVICE_LOG_DIR" "/var/log/mk04/prod"
  _mk04_assert_prod_path INPUT_SERVICE_ROOT "$INPUT_SERVICE_ROOT" "$_MK04_PROD_CURRENT"
  _mk04_assert_prod_path INPUT_SERVICE_CONFIG_DIR "$INPUT_SERVICE_CONFIG_DIR" "/etc/mk04/prod"
  _mk04_assert_prod_path INPUT_SERVICE_DATA_DIR "$INPUT_SERVICE_DATA_DIR" "/var/lib/mk04/prod"
  _mk04_assert_prod_path INPUT_JOB_LEDGER_DIR "$INPUT_JOB_LEDGER_DIR" "/var/lib/mk04/prod"
  _mk04_assert_prod_path VIDEO_AUTOMATION_PROJECT_ROOT "$VIDEO_AUTOMATION_PROJECT_ROOT" "$_MK04_PROD_CURRENT"
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
  _mk04_assert_prod_path MK04_SHARED_LOCK_ROOT "$MK04_SHARED_LOCK_ROOT" "/var/lib/mk04/locks"
  [[ -d "$MK04_SHARED_LOCK_ROOT" ]] || _mk04_fail_prod_preflight "MK04_SHARED_LOCK_ROOT does not exist: $MK04_SHARED_LOCK_ROOT (complete production bootstrap)"
  [[ -w "$MK04_SHARED_LOCK_ROOT" ]] || _mk04_fail_prod_preflight "MK04_SHARED_LOCK_ROOT is not writable: $MK04_SHARED_LOCK_ROOT (fix mk04 group permissions / ACLs)"

  local name
  for name in \
    MK04_CODE_ROOT MK04_ROOT MK04_CONFIG_ROOT MK04_RUNTIME_ROOT MK04_LOG_ROOT \
    MK04_DATA_ROOT MK04_JOBS_ROOT MK04_OUTPUTS_ROOT MK04_RUNS_ROOT MK04_REPORTS_ROOT \
    MK04_CONTROL_STATE_FILE AI_SERVICE_LOG_DIR \
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
    export MK04_CODE_ROOT="${MK04_CODE_ROOT:-$_MK04_PROD_CURRENT}"
    ;;
esac

export MK04_CONFIG_ROOT="${MK04_CONFIG_ROOT:-/etc/mk04/$MK04_ENV}"
export MK04_RUNTIME_ROOT="${MK04_RUNTIME_ROOT:-/var/lib/mk04/$MK04_ENV}"
export MK04_LOG_ROOT="${MK04_LOG_ROOT:-/var/log/mk04/$MK04_ENV}"

load_env_file "$MK04_CONFIG_ROOT/env"

if [[ "$MK04_ENV" == "prod" ]]; then
  # Early checkout / stale-release guard (same logical/physical contract as preflight).
  _mk04_assert_prod_deployment_root
fi

mk04_export_runtime
if [[ "${MK04_SKIP_PROD_PREFLIGHT:-0}" != "1" ]]; then
  mk04_prod_preflight
fi
