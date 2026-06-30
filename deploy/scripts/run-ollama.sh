#!/usr/bin/env bash
set -euo pipefail

# Ensure the local Ollama model backend is running for the ai-service.
#
# This is intentionally best-effort: clip selection defaults to the OpenAI
# backend, so a missing Ollama must NOT take down the rest of the local stack.
# Set MK04_OLLAMA_STRICT=1 to make missing/unreachable Ollama a hard failure
# (useful when ai_service is the chosen backend and you want startup to fail
# loudly instead of silently degrading).
#
# Model pulling is gated behind OLLAMA_AUTO_PULL_MODEL to avoid surprise
# multi-GB downloads on every startup.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh" "${1:-}"

SERVICE_ROOT="$MK04_ROOT/ai-service"
load_env_file "$MK04_SERVICE_ENV_DIR/ai-service.env"
if [[ "$MK04_ENV" == "dev" ]]; then
  load_env_file_defaults "$SERVICE_ROOT/.env"
fi
mk04_export_runtime

OLLAMA_BASE_URL="${AI_BASE_URL:-http://localhost:11434}"
OLLAMA_MODEL="${AI_MODEL:-qwen2.5:14b-instruct}"
OLLAMA_AUTO_PULL_MODEL="${OLLAMA_AUTO_PULL_MODEL:-false}"
OLLAMA_START_WAIT_SECONDS="${OLLAMA_START_WAIT_SECONDS:-20}"
OLLAMA_LOG_FILE="${OLLAMA_LOG_FILE:-${MK04_LOG_ROOT:-/tmp}/ollama/ollama-serve.log}"
STRICT="${MK04_OLLAMA_STRICT:-0}"

log() { echo "[run-ollama] $*"; }

tags_url="${OLLAMA_BASE_URL%/}/api/tags"
auto_pull="$(printf '%s' "$OLLAMA_AUTO_PULL_MODEL" | tr '[:upper:]' '[:lower:]')"

fail_or_warn() {
  if [[ "$STRICT" == "1" ]]; then
    log "ERROR: $*"
    exit 1
  fi
  log "WARN: $*"
  log "Continuing without local model; clip selection will use whatever backend is configured."
  exit 0
}

if ! command -v curl >/dev/null 2>&1; then
  fail_or_warn "curl is required to probe Ollama but was not found on PATH."
fi

ollama_reachable() {
  curl -fsS -m 2 "$tags_url" >/dev/null 2>&1
}

if ollama_reachable; then
  log "Ollama already reachable at $OLLAMA_BASE_URL (not starting a second instance)."
else
  if ! command -v ollama >/dev/null 2>&1; then
    fail_or_warn "Ollama is not installed and not reachable at $OLLAMA_BASE_URL. Install it from https://ollama.com/download, then run 'ollama pull $OLLAMA_MODEL'."
  fi
  if pgrep -f "ollama serve" >/dev/null 2>&1; then
    log "An 'ollama serve' process already exists; waiting for it to accept connections."
  else
    mkdir -p "$(dirname "$OLLAMA_LOG_FILE")" 2>/dev/null || true
    log "Starting 'ollama serve' in the background (log: $OLLAMA_LOG_FILE)..."
    nohup ollama serve >"$OLLAMA_LOG_FILE" 2>&1 &
    log "ollama serve started (pid $!)."
  fi
  waited=0
  until ollama_reachable; do
    sleep 1
    waited=$((waited + 1))
    if ((waited >= OLLAMA_START_WAIT_SECONDS)); then
      fail_or_warn "Ollama did not become reachable within ${OLLAMA_START_WAIT_SECONDS}s at $OLLAMA_BASE_URL."
    fi
  done
  log "Ollama is reachable at $OLLAMA_BASE_URL."
fi

model_present() {
  curl -fsS -m 5 "$tags_url" 2>/dev/null | grep -q "\"${OLLAMA_MODEL}\""
}

if model_present; then
  log "Model '$OLLAMA_MODEL' is available."
else
  case "$auto_pull" in
    1 | true | yes | on)
      if ! command -v ollama >/dev/null 2>&1; then
        fail_or_warn "Cannot pull '$OLLAMA_MODEL': ollama CLI not found."
      fi
      log "Model '$OLLAMA_MODEL' is missing and OLLAMA_AUTO_PULL_MODEL is on — pulling now (this can be a large download)."
      if ! ollama pull "$OLLAMA_MODEL"; then
        fail_or_warn "Failed to pull model '$OLLAMA_MODEL'."
      fi
      log "Model '$OLLAMA_MODEL' pulled."
      ;;
    *)
      log "WARN: Model '$OLLAMA_MODEL' is NOT installed and OLLAMA_AUTO_PULL_MODEL is off."
      log "      Run 'ollama pull $OLLAMA_MODEL' or set OLLAMA_AUTO_PULL_MODEL=true."
      log "      Clip selection via the ai_service backend will fail until the model is present."
      ;;
  esac
fi

log "Ollama check complete."
