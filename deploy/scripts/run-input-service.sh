#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh"

SERVICE_ROOT="$MK04_ROOT/source-input/input_service"
load_env_file "$SERVICE_ROOT/.env"

export INPUT_SERVICE_ROOT="${INPUT_SERVICE_ROOT:-$SERVICE_ROOT}"
export INPUT_SERVICE_HOST="${INPUT_SERVICE_HOST:-127.0.0.1}"
export INPUT_SERVICE_PORT="${INPUT_SERVICE_PORT:-5060}"

cd "$SERVICE_ROOT"
exec "${PYTHON_BIN:-$SERVICE_ROOT/.venv/bin/python}" app.py
