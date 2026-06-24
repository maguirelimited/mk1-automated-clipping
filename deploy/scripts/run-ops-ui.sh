#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh" "${1:-}"

SERVICE_ROOT="$MK04_ROOT/ops-ui"
load_env_file "$MK04_SERVICE_ENV_DIR/ops-ui.env"
if [[ "$MK04_ENV" == "dev" ]]; then
  load_env_file_defaults "$SERVICE_ROOT/.env"
fi
mk04_export_runtime

export OPS_UI_HOST="${OPS_UI_HOST:-127.0.0.1}"
mk04_prod_preflight
mk04_banner "ops-ui"

cd "$SERVICE_ROOT"
exec "${OPS_UI_PYTHON_BIN:-${PYTHON_BIN:-$SERVICE_ROOT/.venv/bin/python}}" -m ops_ui
