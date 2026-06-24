#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh" "${1:-}"
MODE="${2:-}"

INPUT_ROOT="$MK04_ROOT/source-input/input_service"
VIDEO_ROOT="$MK04_ROOT/video-automation"
OUTPUT_FUNNEL_ROOT="$MK04_ROOT/output-funnel"

load_env_file "$MK04_SERVICE_ENV_DIR/source-input.env"
load_env_file "$MK04_SERVICE_ENV_DIR/video-automation.env"
load_env_file "$MK04_SERVICE_ENV_DIR/output-funnel.env"
if [[ "$MK04_ENV" == "dev" ]]; then
  load_env_file_defaults "$INPUT_ROOT/.env"
  load_env_file_defaults "$VIDEO_ROOT/.env"
  load_env_file_defaults "$OUTPUT_FUNNEL_ROOT/.env"
fi
mk04_export_runtime
mk04_prod_preflight

client_host() {
  local host="$1"
  if [[ "$host" == "0.0.0.0" || "$host" == "::" ]]; then
    echo "127.0.0.1"
  else
    echo "$host"
  fi
}

INPUT_HOST="$(client_host "${INPUT_SERVICE_HOST:-127.0.0.1}")"
VIDEO_HOST="$(client_host "${VIDEO_AUTOMATION_HOST:-127.0.0.1}")"
OUTPUT_FUNNEL_HOST_NORM="$(client_host "${OUTPUT_FUNNEL_HOST:-127.0.0.1}")"
INPUT_BASE_URL="http://${INPUT_HOST}:${INPUT_SERVICE_PORT}"
VIDEO_BASE_URL="http://${VIDEO_HOST}:${VIDEO_AUTOMATION_PORT}"
OUTPUT_FUNNEL_BASE_URL="http://${OUTPUT_FUNNEL_HOST_NORM}:${OUTPUT_FUNNEL_PORT}"

fail() {
  echo "doctor failed: $*" >&2
  exit 1
}

check_file() {
  local label="$1"
  local path="$2"
  [[ -f "$path" ]] || fail "$label missing: $path"
  echo "ok: $label $path"
}

check_dir() {
  local label="$1"
  local path="$2"
  [[ -d "$path" ]] || fail "$label missing: $path"
  [[ -w "$path" ]] || fail "$label not writable: $path"
  echo "ok: $label $path"
}

check_absent_repo_artifact() {
  local rel="$1"
  [[ ! -e "$MK04_ROOT/$rel" ]] || fail "prod deploy contains excluded artifact: $MK04_ROOT/$rel"
}

check_port_conflict() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      echo "info: port $port is already listening"
    else
      echo "ok: port $port free before startup"
    fi
  else
    echo "warn: lsof unavailable; skipping port $port conflict check"
  fi
}

credential_check() {
  local name="$1"
  local value="${!name:-}"
  if [[ -z "$value" ]]; then
    echo "warn: credential path env missing: $name"
    return
  fi
  if [[ "$MK04_ENV" == "prod" ]]; then
    _mk04_assert_prod_path "$name" "$value" "/var/lib/mk04/prod"
  fi
  [[ -f "$value" ]] || echo "warn: credential file missing: $name=$value"
  [[ -f "$value" ]] && echo "ok: credential file present: $name"
}

mk04_banner "doctor"

echo "Local binary checks"
echo "environment: $MK04_ENV"
echo "code root: $MK04_ROOT"
echo "config root: $MK04_CONFIG_ROOT"
echo "runtime root: $MK04_RUNTIME_ROOT"
echo "upload mode: $MK04_UPLOAD_MODE"
require_command python3
require_command curl
require_command ffmpeg
require_command ffprobe
echo "python3: $(command -v python3)"
if command -v whisper >/dev/null 2>&1; then
  echo "whisper: $(command -v whisper)"
else
  echo "whisper: missing (video automation /doctor will fail until installed)" >&2
fi

echo
echo "Environment isolation checks"
check_dir "config root" "$MK04_CONFIG_ROOT"
check_dir "runtime root" "$MK04_RUNTIME_ROOT"
check_dir "log root" "$MK04_LOG_ROOT"
check_dir "input data" "$INPUT_SERVICE_DATA_DIR"
check_dir "video input" "$VIDEO_AUTOMATION_INPUT_DIR"
check_dir "output-funnel runtime" "$(dirname "$OUTPUT_FUNNEL_DB")"
check_dir "ops-ui runtime" "$OPS_UI_DATA_DIR"
check_file "source-input funnels" "$INPUT_SERVICE_CONFIG_DIR/funnels.json"
check_file "pipeline config" "$PIPELINE_CONFIG_PATH"
check_file "video profiles" "$VIDEO_PIPELINE_PROFILES_PATH"
check_dir "video funnel config" "$FUNNEL_CONFIG_DIR"
check_file "output settings" "$OUTPUT_FUNNEL_SETTINGS"
check_file "output channels" "$OUTPUT_FUNNEL_CHANNELS"

echo
echo "Service URL checks"
echo "source-input: $INPUT_BASE_URL"
echo "video-automation: $VIDEO_BASE_URL"
echo "output-funnel: $OUTPUT_FUNNEL_BASE_URL"
echo "ops source-input: $OPS_SOURCE_INPUT_URL"
echo "ops video-automation: $OPS_VIDEO_AUTOMATION_URL"
echo "ops output-funnel: $OPS_OUTPUT_FUNNEL_URL"

echo
echo "Upload safety checks"
case "$MK04_UPLOAD_MODE" in
  dry_run|real) echo "ok: MK04_UPLOAD_MODE=$MK04_UPLOAD_MODE" ;;
  *) fail "invalid MK04_UPLOAD_MODE=$MK04_UPLOAD_MODE" ;;
esac
if [[ "$MK04_UPLOAD_MODE" == "real" && "$MK04_ENV" != "prod" ]]; then
  fail "real uploads are only allowed in prod"
fi
credential_check MFM_BUSINESS_AI_YT_TOKEN_FILE
credential_check MFM_BUSINESS_AI_YT_CLIENT_SECRET_FILE
credential_check YT_DLP_COOKIES_PATH

echo
echo "Port checks"
check_port_conflict "$INPUT_SERVICE_PORT"
check_port_conflict "$VIDEO_AUTOMATION_PORT"
check_port_conflict "$OUTPUT_FUNNEL_PORT"
check_port_conflict "$OPS_UI_PORT"

if [[ "$MK04_ENV" == "prod" ]]; then
  echo
  echo "Production artifact exclusion checks"
  check_absent_repo_artifact ".git"
  check_absent_repo_artifact ".cursor"
  check_absent_repo_artifact ".pytest_cache"
  check_absent_repo_artifact "source-input/input_service/.env"
  check_absent_repo_artifact "video-automation/.env"
  check_absent_repo_artifact "output-funnel/.env"
  check_absent_repo_artifact "ops-ui/.env"
  check_absent_repo_artifact "output-funnel/credentials"
  check_absent_repo_artifact "source-input/input_service/data"
  check_absent_repo_artifact "source-input/input_service/server.log"
  check_absent_repo_artifact "video-automation/input"
  check_absent_repo_artifact "video-automation/output"
  check_absent_repo_artifact "video-automation/jobs"
  check_absent_repo_artifact "video-automation/temp"
  check_absent_repo_artifact "output-funnel/data"
  check_absent_repo_artifact "ops-ui/data"
fi

if [[ "$MODE" == "--preflight-only" ]]; then
  echo
  echo "Preflight-only checks passed."
  exit 0
fi

echo
echo "Input service health: ${INPUT_BASE_URL}/healthz"
if [[ -n "${INPUT_SERVICE_SECRET:-}" ]]; then
  curl -fsS -H "X-Input-Service-Secret: $INPUT_SERVICE_SECRET" "${INPUT_BASE_URL}/healthz"
else
  curl -fsS "${INPUT_BASE_URL}/healthz"
fi

echo
echo "Input service doctor: ${INPUT_BASE_URL}/doctor"
if [[ -n "${INPUT_SERVICE_SECRET:-}" ]]; then
  _input_doc="$(curl -fsS -H "X-Input-Service-Secret: $INPUT_SERVICE_SECRET" "${INPUT_BASE_URL}/doctor")"
else
  _input_doc="$(curl -fsS "${INPUT_BASE_URL}/doctor")"
fi
echo "$_input_doc"
echo "$_input_doc" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('ok') else 1)"

echo
echo "Video automation health: ${VIDEO_BASE_URL}/healthz"
curl -fsS "${VIDEO_BASE_URL}/healthz"
echo
echo
echo "Video automation doctor: ${VIDEO_BASE_URL}/doctor"
_video_doc="$(curl -fsS "${VIDEO_BASE_URL}/doctor")"
echo "$_video_doc"
echo "$_video_doc" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('ok') else 1)"
echo

# output-funnel does not yet expose /doctor; /healthz proves the service is
# up and its sqlite DB is reachable. The watchdog should additionally call
# /admin/stalled-jobs to alert on stuck rows.
echo "Output-funnel health: ${OUTPUT_FUNNEL_BASE_URL}/healthz"
if [[ -n "${OUTPUT_FUNNEL_SECRET:-}" ]]; then
  curl -fsS -H "X-Output-Funnel-Secret: $OUTPUT_FUNNEL_SECRET" "${OUTPUT_FUNNEL_BASE_URL}/healthz"
else
  curl -fsS "${OUTPUT_FUNNEL_BASE_URL}/healthz"
fi
echo
echo
