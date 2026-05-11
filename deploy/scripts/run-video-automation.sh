#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh"

SERVICE_ROOT="$MK04_ROOT/video-automation"
load_env_file "$SERVICE_ROOT/.env"

export PIPELINE_CONFIG_PATH="${PIPELINE_CONFIG_PATH:-$SERVICE_ROOT/config/pipeline_config.json}"
export VIDEO_PIPELINE_PROFILES_PATH="${VIDEO_PIPELINE_PROFILES_PATH:-$SERVICE_ROOT/config/video_pipeline_profiles.json}"
export VIDEO_AUTOMATION_HOST="${VIDEO_AUTOMATION_HOST:-0.0.0.0}"
export VIDEO_AUTOMATION_PORT="${VIDEO_AUTOMATION_PORT:-5050}"

cd "$SERVICE_ROOT"
exec "${PYTHON_BIN:-$SERVICE_ROOT/.venv/bin/python}" server/app.py
