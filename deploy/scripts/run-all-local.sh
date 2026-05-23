#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh"

pids=()
labels=()

start_service() {
  local label="$1"
  shift
  echo "Starting $label..."
  "$@" &
  pids+=("$!")
  labels+=("$label")
}

cleanup() {
  local exit_code=$?
  if ((${#pids[@]} > 0)); then
    echo
    echo "Stopping mk04 local services..."
    for pid in "${pids[@]}"; do
      kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
  fi
  exit "$exit_code"
}

trap cleanup INT TERM EXIT

start_service "source-input" "$SCRIPT_DIR/run-input-service.sh"
start_service "video-automation" "$SCRIPT_DIR/run-video-automation.sh"

start_service "output-funnel" bash -c '
  set -euo pipefail
  cd "$1/output-funnel"
  exec "${OUTPUT_FUNNEL_PYTHON_BIN:-${PYTHON_BIN:-.venv/bin/python}}" -m output_funnel.app
' bash "$MK04_ROOT"

start_service "ops-ui" bash -c '
  set -euo pipefail
  cd "$1/ops-ui"
  exec "${OPS_UI_PYTHON_BIN:-${PYTHON_BIN:-.venv/bin/python}}" -m ops_ui
' bash "$MK04_ROOT"

cat <<EOF

mk04 local services are starting:
  source-input      http://127.0.0.1:5060
  video-automation  http://127.0.0.1:5050
  output-funnel     http://127.0.0.1:5055
  ops-ui            http://127.0.0.1:5070

Press Ctrl+C to stop all services.
EOF

while true; do
  sleep 2
  for idx in "${!pids[@]}"; do
    pid="${pids[$idx]}"
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "${labels[$idx]} exited; stopping the rest." >&2
      exit 1
    fi
  done
done
