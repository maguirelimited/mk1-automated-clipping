#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh" "${1:-}"

SERVICE_ROOT="$MK04_ROOT/video-automation"
load_env_file "$MK04_SERVICE_ENV_DIR/video-automation.env"
if [[ "$MK04_ENV" == "dev" ]]; then
  load_env_file_defaults "$SERVICE_ROOT/.env"
fi
mk04_export_runtime

export VIDEO_AUTOMATION_HOST="${VIDEO_AUTOMATION_HOST:-127.0.0.1}"
mk04_prod_preflight
mk04_banner "video-automation"

cd "$SERVICE_ROOT"
exec "${PYTHON_BIN:-$SERVICE_ROOT/.venv/bin/python}" server/app.py
