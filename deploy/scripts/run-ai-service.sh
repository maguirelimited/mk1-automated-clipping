#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh" "${1:-}"

SERVICE_ROOT="$MK04_ROOT/ai-service"
load_env_file "$MK04_SERVICE_ENV_DIR/ai-service.env"
if [[ "$MK04_ENV" == "dev" ]]; then
  load_env_file_defaults "$SERVICE_ROOT/.env"
fi
mk04_export_runtime

export AI_SERVICE_HOST="${AI_SERVICE_HOST:-127.0.0.1}"
mk04_prod_preflight
mk04_banner "ai-service"

# Best-effort: ensure the local Ollama model backend is up before the service.
# Non-fatal so a missing Ollama never blocks ai-service startup (clip selection
# defaults to the OpenAI backend). run-all-local sets MK04_ENSURE_OLLAMA=0 when
# it has already ensured Ollama itself.
if [[ "${MK04_ENSURE_OLLAMA:-1}" == "1" ]]; then
  "$SCRIPT_DIR/run-ollama.sh" "$MK04_ENV" || true
fi

cd "$SERVICE_ROOT"
exec "${AI_SERVICE_PYTHON_BIN:-${PYTHON_BIN:-$SERVICE_ROOT/.venv/bin/python}}" app.py
