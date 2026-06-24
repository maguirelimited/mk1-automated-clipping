#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PROD_ROOT="${MK04_PROD_ROOT:-/opt/mk04/prod/current}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<EOF
Usage: deploy/scripts/promote-to-prod.sh [--dry-run]

Copies the current development codebase into $PROD_ROOT with dev/local
artifacts excluded. Runtime state, production config, logs, credentials, and
databases remain outside the repo under /etc/mk04/prod, /var/lib/mk04/prod,
and /var/log/mk04/prod.
EOF
  exit 0
fi

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
elif [[ -n "${1:-}" ]]; then
  echo "Unknown argument: $1" >&2
  exit 2
fi

if [[ "$DEV_ROOT" == "$PROD_ROOT" ]]; then
  echo "Refusing to promote: source and destination are the same path: $DEV_ROOT" >&2
  exit 2
fi

case "$DEV_ROOT" in
  /opt/mk04/prod/current|/opt/mk04/prod/current/*)
    echo "Refusing to promote from the production deployed copy: $DEV_ROOT" >&2
    exit 2
    ;;
esac

require_command() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 2
  fi
}

require_command rsync
require_command mkdir

run_privileged() {
  if [[ -w "$(dirname "$PROD_ROOT")" || -w "$PROD_ROOT" ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

RSYNC_ARGS=(
  -a
  --delete
  --exclude ".git/"
  --exclude ".cursor/"
  --exclude ".DS_Store"
  --exclude "**/.venv/"
  --exclude "**/__pycache__/"
  --exclude ".pytest_cache/"
  --exclude "**/.pytest_cache/"
  --exclude "*.pyc"
  --exclude "*.pyo"
  --exclude "*.log"
  --exclude "*.ndjson"
  --exclude "*.sqlite3"
  --exclude "*.sqlite"
  --exclude "*.db"
  --exclude ".env"
  --exclude ".env.*"
  --exclude "**/.env"
  --exclude "**/.env.*"
  --exclude "logs/"
  --exclude "**/logs/"
  --exclude "log/"
  --exclude "**/log/"
  --exclude "tmp/"
  --exclude "temp/"
  --exclude "**/tmp/"
  --exclude "**/temp/"
  --exclude "uploads/"
  --exclude "downloads/"
  --exclude "credentials/"
  --exclude "**/uploads/"
  --exclude "**/downloads/"
  --exclude "**/credentials/"
  --exclude "**/n8n_data/"
  --exclude "**/binaryData/"
  --exclude "source-input/input_service/data/"
  --exclude "source-input/input_service/run*.json"
  --exclude "video-automation/input/"
  --exclude "video-automation/output/"
  --exclude "video-automation/jobs/"
  --exclude "video-automation/temp/"
  --exclude "video-automation/analytics/"
  --exclude "output-funnel/data/"
  --exclude "ops-ui/data/"
  --exclude "coverage/"
  --exclude "htmlcov/"
  --exclude ".coverage"
  --exclude "**/.mypy_cache/"
  --exclude "**/.ruff_cache/"
  --exclude "**/node_modules/"
)

if ((DRY_RUN)); then
  RSYNC_ARGS+=(--dry-run --itemize-changes)
fi

echo "================================================================"
echo "mk04 production promotion"
echo "source:      $DEV_ROOT"
echo "destination: $PROD_ROOT"
echo "mode:        $([[ "$DRY_RUN" == "1" ]] && echo dry-run || echo apply)"
echo "================================================================"

run_privileged mkdir -p "$PROD_ROOT"
run_privileged rsync "${RSYNC_ARGS[@]}" "$DEV_ROOT/" "$PROD_ROOT/"

if ((DRY_RUN == 0)); then
  run_privileged rm -rf \
    "$PROD_ROOT/.git" \
    "$PROD_ROOT/.cursor" \
    "$PROD_ROOT/.pytest_cache" \
    "$PROD_ROOT/source-input/input_service/.env" \
    "$PROD_ROOT/video-automation/.env" \
    "$PROD_ROOT/output-funnel/.env" \
    "$PROD_ROOT/ops-ui/.env" \
    "$PROD_ROOT/output-funnel/credentials" \
    "$PROD_ROOT/source-input/input_service/data" \
    "$PROD_ROOT/source-input/input_service"/run*.json \
    "$PROD_ROOT/video-automation/input" \
    "$PROD_ROOT/video-automation/output" \
    "$PROD_ROOT/video-automation/jobs" \
    "$PROD_ROOT/video-automation/temp" \
    "$PROD_ROOT/video-automation/analytics" \
    "$PROD_ROOT/output-funnel/data" \
    "$PROD_ROOT/ops-ui/data" \
    2>/dev/null || true
  while IFS= read -r -d '' path; do
    run_privileged rm -rf "$path"
  done < <(
    find "$PROD_ROOT" \
      \( -path "*/.venv" -o -path "*/.venv/*" \) -prune -o \
      \( \
        -name "__pycache__" -o \
        -name "*.log" -o \
        -name "*.ndjson" -o \
        -name "*.sqlite3" -o \
        -name "*.sqlite" -o \
        -name "*.db" \
      \) -print0
  )
fi

echo
echo "Promotion copy complete."
echo "Next:"
echo "  cd $PROD_ROOT"
echo "  ./deploy/scripts/doctor.sh prod --preflight-only"
echo "  ./deploy/scripts/doctor.sh prod"
