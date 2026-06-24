#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh" "${1:-dev}"

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

start_service "source-input" "$SCRIPT_DIR/run-input-service.sh" "$MK04_ENV"
start_service "video-automation" "$SCRIPT_DIR/run-video-automation.sh" "$MK04_ENV"
start_service "output-funnel" "$SCRIPT_DIR/run-output-funnel.sh" "$MK04_ENV"
start_service "ops-ui" "$SCRIPT_DIR/run-ops-ui.sh" "$MK04_ENV"

cat <<EOF

mk04 $MK04_ENV services are starting from $MK04_ROOT:
  source-input      $OPS_SOURCE_INPUT_URL
  video-automation  $OPS_VIDEO_AUTOMATION_URL
  output-funnel     $OPS_OUTPUT_FUNNEL_URL
  ops-ui            http://127.0.0.1:$OPS_UI_PORT
  upload-mode       $MK04_UPLOAD_MODE

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
