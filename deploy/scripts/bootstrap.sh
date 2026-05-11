#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh"

require_command python3
require_command ffmpeg
require_command ffprobe

echo "Bootstrapping mk0.4 under: $MK04_ROOT"

INPUT_ROOT="$MK04_ROOT/source-input/input_service"
VIDEO_ROOT="$MK04_ROOT/video-automation"

python3 -m venv "$INPUT_ROOT/.venv"
"$INPUT_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$INPUT_ROOT/.venv/bin/python" -m pip install -r "$INPUT_ROOT/requirements.txt"

python3 -m venv "$VIDEO_ROOT/.venv"
"$VIDEO_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$VIDEO_ROOT/.venv/bin/python" -m pip install -r "$VIDEO_ROOT/requirements-dev.txt"

mkdir -p \
  "$INPUT_ROOT/data/inputs/ready" \
  "$INPUT_ROOT/data/inputs/rejected" \
  "$INPUT_ROOT/data/state" \
  "$INPUT_ROOT/data/tmp" \
  "$VIDEO_ROOT/input" \
  "$VIDEO_ROOT/output" \
  "$VIDEO_ROOT/temp" \
  "$VIDEO_ROOT/jobs" \
  "$VIDEO_ROOT/analytics"

if [[ ! -f "$INPUT_ROOT/.env" ]]; then
  cp "$INPUT_ROOT/.env.example" "$INPUT_ROOT/.env"
  echo "Created $INPUT_ROOT/.env from .env.example"
fi
if [[ ! -f "$VIDEO_ROOT/.env" ]]; then
  cp "$VIDEO_ROOT/.env.example" "$VIDEO_ROOT/.env"
  echo "Created $VIDEO_ROOT/.env from .env.example"
fi

echo "Bootstrap complete. Fill OPENAI_API_KEY in video-automation/.env before running the clipping service."
