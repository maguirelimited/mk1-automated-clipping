#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export MK04_SKIP_PROD_PREFLIGHT=1
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh" "${1:-${MK04_ENV:-dev}}"

require_command python3
require_command ffmpeg
require_command ffprobe

echo "Bootstrapping mk0.4 ($MK04_ENV) under: $MK04_ROOT"
echo "Note: Whisper is a runtime CLI dependency for video automation and is not installed by bootstrap."
echo "Install it separately so 'whisper' resolves on PATH for the service user."

INPUT_ROOT="$MK04_ROOT/source-input/input_service"
VIDEO_ROOT="$MK04_ROOT/video-automation"
OUTPUT_FUNNEL_ROOT="$MK04_ROOT/output-funnel"
OPS_UI_ROOT="$MK04_ROOT/ops-ui"

python3 -m venv "$INPUT_ROOT/.venv"
"$INPUT_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$INPUT_ROOT/.venv/bin/python" -m pip install -r "$INPUT_ROOT/requirements.txt"

python3 -m venv "$VIDEO_ROOT/.venv"
"$VIDEO_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$VIDEO_ROOT/.venv/bin/python" -m pip install -r "$VIDEO_ROOT/requirements-dev.txt"

python3 -m venv "$OUTPUT_FUNNEL_ROOT/.venv"
"$OUTPUT_FUNNEL_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$OUTPUT_FUNNEL_ROOT/.venv/bin/python" -m pip install -r "$OUTPUT_FUNNEL_ROOT/requirements.txt"

python3 -m venv "$OPS_UI_ROOT/.venv"
"$OPS_UI_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$OPS_UI_ROOT/.venv/bin/python" -m pip install -r "$OPS_UI_ROOT/requirements.txt"

mkdir -p \
  "$INPUT_ROOT/data/inputs/ready" \
  "$INPUT_ROOT/data/inputs/rejected" \
  "$INPUT_ROOT/data/state" \
  "$INPUT_ROOT/data/tmp" \
  "$VIDEO_ROOT/input" \
  "$VIDEO_ROOT/output" \
  "$VIDEO_ROOT/temp" \
  "$VIDEO_ROOT/jobs" \
  "$VIDEO_ROOT/analytics" \
  "$OUTPUT_FUNNEL_ROOT/data" \
  "$OPS_UI_ROOT/data" \
  "$MK04_CONFIG_ROOT/source-input" \
  "$MK04_CONFIG_ROOT/video-automation/funnels" \
  "$MK04_CONFIG_ROOT/output-funnel" \
  "$MK04_RUNTIME_ROOT/source-input/inputs/ready" \
  "$MK04_RUNTIME_ROOT/source-input/inputs/rejected" \
  "$MK04_RUNTIME_ROOT/source-input/state/input_jobs" \
  "$MK04_RUNTIME_ROOT/source-input/tmp" \
  "$MK04_RUNTIME_ROOT/video-automation/input" \
  "$MK04_RUNTIME_ROOT/video-automation/output" \
  "$MK04_RUNTIME_ROOT/video-automation/temp" \
  "$MK04_RUNTIME_ROOT/video-automation/jobs" \
  "$MK04_RUNTIME_ROOT/video-automation/analytics" \
  "$MK04_RUNTIME_ROOT/output-funnel" \
  "$MK04_RUNTIME_ROOT/ops-ui" \
  "$MK04_LOG_ROOT/video-automation" \
  "$MK04_LOG_ROOT/output-funnel" \
  "$MK04_LOG_ROOT/ops-ui" \
  "$MK04_LOG_ROOT/watchdog"

seed_config() {
  local source="$1"
  local dest="$2"
  if [[ ! -f "$dest" && -f "$source" ]]; then
    cp "$source" "$dest"
    echo "Created $dest"
  fi
}

seed_config "$MK04_ROOT/deploy/env/$MK04_ENV/env.example" "$MK04_CONFIG_ROOT/env"
seed_config "$MK04_ROOT/deploy/env/$MK04_ENV/funnels.json" "$MK04_CONFIG_ROOT/source-input/funnels.json"
seed_config "$MK04_ROOT/deploy/env/$MK04_ENV/pipeline_config.json" "$MK04_CONFIG_ROOT/video-automation/pipeline_config.json"
seed_config "$MK04_ROOT/deploy/env/$MK04_ENV/video_pipeline_profiles.json" "$MK04_CONFIG_ROOT/video-automation/video_pipeline_profiles.json"
seed_config "$MK04_ROOT/deploy/env/$MK04_ENV/settings.json" "$MK04_CONFIG_ROOT/output-funnel/settings.json"
seed_config "$MK04_ROOT/deploy/env/$MK04_ENV/channels.json" "$MK04_CONFIG_ROOT/output-funnel/channels.json"
if [[ -d "$MK04_ROOT/video-automation/config/funnels" ]]; then
  cp -n "$MK04_ROOT"/video-automation/config/funnels/*.json "$MK04_CONFIG_ROOT/video-automation/funnels/" 2>/dev/null || true
fi

if [[ "$MK04_ENV" == "dev" && ! -f "$INPUT_ROOT/.env" ]]; then
  cp "$INPUT_ROOT/.env.example" "$INPUT_ROOT/.env"
  echo "Created $INPUT_ROOT/.env from .env.example"
fi
if [[ "$MK04_ENV" == "dev" && ! -f "$VIDEO_ROOT/.env" ]]; then
  cp "$VIDEO_ROOT/.env.example" "$VIDEO_ROOT/.env"
  echo "Created $VIDEO_ROOT/.env from .env.example"
fi
if [[ "$MK04_ENV" == "dev" && ! -f "$OUTPUT_FUNNEL_ROOT/.env" && -f "$OUTPUT_FUNNEL_ROOT/.env.example" ]]; then
  cp "$OUTPUT_FUNNEL_ROOT/.env.example" "$OUTPUT_FUNNEL_ROOT/.env"
  echo "Created $OUTPUT_FUNNEL_ROOT/.env from .env.example"
fi
if [[ "$MK04_ENV" == "dev" && ! -f "$OUTPUT_FUNNEL_ROOT/config/settings.json" && -f "$OUTPUT_FUNNEL_ROOT/config/settings.example.json" ]]; then
  cp "$OUTPUT_FUNNEL_ROOT/config/settings.example.json" "$OUTPUT_FUNNEL_ROOT/config/settings.json"
  echo "Created $OUTPUT_FUNNEL_ROOT/config/settings.json from .example"
fi
if [[ "$MK04_ENV" == "dev" && ! -f "$OUTPUT_FUNNEL_ROOT/config/channels.json" && -f "$OUTPUT_FUNNEL_ROOT/config/channels.example.json" ]]; then
  cp "$OUTPUT_FUNNEL_ROOT/config/channels.example.json" "$OUTPUT_FUNNEL_ROOT/config/channels.json"
  echo "Created $OUTPUT_FUNNEL_ROOT/config/channels.json from .example (edit before publishing)"
fi

echo "Bootstrap complete. Review $MK04_CONFIG_ROOT/env and service config under $MK04_CONFIG_ROOT before running."
echo "Keep MK04_UPLOAD_MODE=dry_run until prod OAuth/channel credentials are verified."
