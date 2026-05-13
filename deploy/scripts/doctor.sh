#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh"

INPUT_ROOT="$MK04_ROOT/source-input/input_service"
VIDEO_ROOT="$MK04_ROOT/video-automation"

load_env_file "$INPUT_ROOT/.env"
load_env_file "$VIDEO_ROOT/.env"

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
INPUT_BASE_URL="http://${INPUT_HOST}:${INPUT_SERVICE_PORT:-5060}"
VIDEO_BASE_URL="http://${VIDEO_HOST}:${VIDEO_AUTOMATION_PORT:-5050}"

echo "Local binary checks"
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
