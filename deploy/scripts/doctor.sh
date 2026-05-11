#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh"

INPUT_ROOT="$MK04_ROOT/source-input/input_service"
VIDEO_ROOT="$MK04_ROOT/video-automation"

load_env_file "$INPUT_ROOT/.env"
load_env_file "$VIDEO_ROOT/.env"

INPUT_URL="http://${INPUT_SERVICE_HOST:-127.0.0.1}:${INPUT_SERVICE_PORT:-5060}/doctor"
VIDEO_URL="http://${VIDEO_AUTOMATION_HOST:-127.0.0.1}:${VIDEO_AUTOMATION_PORT:-5050}/doctor"

echo "Local binary checks"
require_command python3
require_command ffmpeg
require_command ffprobe
if command -v whisper >/dev/null 2>&1; then
  echo "whisper: $(command -v whisper)"
else
  echo "whisper: missing (video automation /doctor will fail until installed)" >&2
fi

echo
echo "Input service doctor: $INPUT_URL"
if [[ -n "${INPUT_SERVICE_SECRET:-}" ]]; then
  curl -fsS -H "X-Input-Service-Secret: $INPUT_SERVICE_SECRET" "$INPUT_URL"
else
  curl -fsS "$INPUT_URL"
fi

echo
echo
echo "Video automation doctor: $VIDEO_URL"
curl -fsS "$VIDEO_URL"
echo
