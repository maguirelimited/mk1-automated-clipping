#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh" "${1:-}"

SERVICE_ROOT="$MK04_ROOT/output-funnel"
load_env_file "$MK04_SERVICE_ENV_DIR/output-funnel.env"
if [[ "$MK04_ENV" == "dev" ]]; then
  load_env_file_defaults "$SERVICE_ROOT/.env"
fi
mk04_export_runtime

export OUTPUT_FUNNEL_HOST="${OUTPUT_FUNNEL_HOST:-127.0.0.1}"
mk04_prod_preflight
mk04_banner "output-funnel"

cd "$SERVICE_ROOT"
exec "${OUTPUT_FUNNEL_PYTHON_BIN:-${PYTHON_BIN:-$SERVICE_ROOT/.venv/bin/python}}" -m output_funnel.app
