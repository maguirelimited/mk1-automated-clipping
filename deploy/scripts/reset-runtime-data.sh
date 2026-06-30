#!/usr/bin/env bash
# Wipe operational/runtime data (jobs, clips, seen URLs, queues, review state,
# analytics events, scratch) while preserving everything required for the system
# to keep working: credentials, operator settings (controls.json), and config
# under MK04_CONFIG_ROOT.
#
# Usage:
#   reset-runtime-data.sh [dev|prod] [--dry-run] [--yes]
#
# Without --yes the script prints what would be removed and exits.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_ARG="${MK04_ENV:-dev}"
DRY_RUN=0
CONFIRM=0

for arg in "$@"; do
  case "$arg" in
    dev|prod) ENV_ARG="$arg" ;;
    --dry-run) DRY_RUN=1 ;;
    --yes) CONFIRM=1 ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh" "$ENV_ARG"

CODE_ROOT="${MK04_CODE_ROOT:-$MK04_ROOT}"
REPO_INPUT_DATA="$CODE_ROOT/source-input/input_service/data"
REPO_VA_ROOT="$CODE_ROOT/video-automation"
REPO_OPS_DATA="$CODE_ROOT/ops-ui/data"
REPO_AI_LOGS="$CODE_ROOT/ai-service/logs"

VA_ROOT="$MK04_RUNTIME_ROOT/video-automation"
SI_ROOT="${INPUT_SERVICE_DATA_DIR:-$MK04_RUNTIME_ROOT/source-input}"
OPS_ROOT="${OPS_UI_DATA_DIR:-$MK04_RUNTIME_ROOT/ops-ui}"
OF_DB="${OUTPUT_FUNNEL_DB:-$MK04_RUNTIME_ROOT/output-funnel/output_funnel.sqlite3}"

EMPTY_SEEN='{"video_ids":[],"urls":[]}'

log() { echo "[reset-runtime-data] $*"; }

run_rm() {
  local label="$1"; shift
  if (( DRY_RUN )); then
    log "DRY_RUN would remove ($label): $*"
    return 0
  fi
  rm -rf "$@"
  log "removed ($label): $*"
}

clear_dir_contents() {
  local label="$1" dir="$2"
  [[ -d "$dir" ]] || { log "skip missing $label dir=$dir"; return 0; }
  local entry
  shopt -s nullglob dotglob
  for entry in "$dir"/*; do
    [[ -e "$entry" ]] || continue
    local base
    base="$(basename "$entry")"
    if [[ "$base" == ".gitkeep" ]]; then
      continue
    fi
    if (( DRY_RUN )); then
      log "DRY_RUN would remove ($label): $entry"
    else
      rm -rf "$entry"
      log "removed ($label): $entry"
    fi
  done
  shopt -u nullglob dotglob
}

reset_seen_urls() {
  local path="$1"
  if (( DRY_RUN )); then
    log "DRY_RUN would reset seen_urls: $path"
    return 0
  fi
  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$EMPTY_SEEN" >"$path"
  log "reset seen_urls: $path"
}

reset_ops_sqlite() {
  local db="$1"
  [[ -f "$db" ]] || { log "skip absent ops db: $db"; return 0; }
  if (( DRY_RUN )); then
    log "DRY_RUN would clear action_log + clip_reviews in $db (controls preserved)"
    return 0
  fi
  python3 - "$db" <<'PY'
import sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
conn.execute("DELETE FROM action_log")
conn.execute("DELETE FROM clip_reviews")
conn.commit()
conn.close()
PY
  log "cleared action_log + clip_reviews in $db"
}

reset_output_funnel_db() {
  local db="$1"
  local removed=0
  for suffix in "" "-wal" "-shm"; do
    local path="${db}${suffix}"
    if [[ -f "$path" ]]; then
      if (( DRY_RUN )); then
        log "DRY_RUN would remove output-funnel db: $path"
      else
        rm -f "$path"
        log "removed output-funnel db: $path"
      fi
      removed=1
    fi
  done
  if (( removed == 0 )); then
    log "output-funnel db already absent: $db"
  fi
}

clear_jsonl_logs() {
  local label="$1" dir="$2"
  [[ -d "$dir" ]] || return 0
  local f
  shopt -s nullglob
  for f in "$dir"/*.jsonl "$dir"/*.ndjson "$dir"/artifacts/*; do
    [[ -e "$f" ]] || continue
    if (( DRY_RUN )); then
      log "DRY_RUN would remove ($label): $f"
    else
      rm -f "$f"
      log "removed ($label): $f"
    fi
  done
  shopt -u nullglob
}

log "environment=$MK04_ENV runtime_root=$MK04_RUNTIME_ROOT code_root=$CODE_ROOT"
log "PRESERVED: credentials/, controls.json, controls table, $MK04_CONFIG_ROOT/*"

if (( ! CONFIRM && ! DRY_RUN )); then
  cat <<EOF
This will permanently delete operational data under:
  $MK04_RUNTIME_ROOT
  selected repo-local runtime copies under $CODE_ROOT

Preserved: credentials, controls.json (+ controls sqlite rows), config trees.

Re-run with --yes to execute, or --dry-run to preview.
EOF
  exit 0
fi

# --- Runtime root (/var/lib/mk04/...) ---
clear_dir_contents "video-automation/jobs" "$VA_ROOT/jobs"
clear_dir_contents "video-automation/output" "$VA_ROOT/output"
clear_dir_contents "video-automation/temp" "$VA_ROOT/temp"
clear_dir_contents "video-automation/input" "$VA_ROOT/input"
clear_dir_contents "video-automation/analytics" "$VA_ROOT/analytics"

clear_dir_contents "source-input/tmp" "$SI_ROOT/tmp"
clear_dir_contents "source-input/ready" "$SI_ROOT/inputs/ready"
clear_dir_contents "source-input/rejected" "$SI_ROOT/inputs/rejected"
clear_dir_contents "source-input/input_jobs" "$SI_ROOT/state/input_jobs"
reset_seen_urls "$SI_ROOT/state/seen_urls.json"
run_rm "source-input/run.lock" "$SI_ROOT/state/run.lock" 2>/dev/null || true

reset_ops_sqlite "$OPS_ROOT/ops_ui.sqlite3"
reset_output_funnel_db "$OF_DB"

# --- Repo-local dev copies (when they differ from runtime root) ---
clear_dir_contents "repo video-automation/jobs" "$REPO_VA_ROOT/jobs"
clear_dir_contents "repo video-automation/output" "$REPO_VA_ROOT/output"
clear_dir_contents "repo video-automation/temp" "$REPO_VA_ROOT/temp"
clear_dir_contents "repo video-automation/input" "$REPO_VA_ROOT/input"
clear_dir_contents "repo video-automation/analytics" "$REPO_VA_ROOT/analytics"

clear_dir_contents "repo source-input/tmp" "$REPO_INPUT_DATA/tmp"
clear_dir_contents "repo source-input/ready" "$REPO_INPUT_DATA/inputs/ready"
clear_dir_contents "repo source-input/rejected" "$REPO_INPUT_DATA/inputs/rejected"
clear_dir_contents "repo source-input/input_jobs" "$REPO_INPUT_DATA/state/input_jobs"
reset_seen_urls "$REPO_INPUT_DATA/state/seen_urls.json"
run_rm "repo source-input/run.lock" "$REPO_INPUT_DATA/state/run.lock" 2>/dev/null || true

reset_ops_sqlite "$REPO_OPS_DATA/ops_ui.sqlite3"
clear_jsonl_logs "ai-service logs" "$REPO_AI_LOGS"

# --- Service debug logs ---
if [[ -n "${MK04_LOG_ROOT:-}" && -d "$MK04_LOG_ROOT" ]]; then
  find "$MK04_LOG_ROOT" -type f \( -name '*.ndjson' -o -name '*.jsonl' -o -name '*.log' \) -print0 2>/dev/null \
    | while IFS= read -r -d '' f; do
        if (( DRY_RUN )); then
          log "DRY_RUN would remove log: $f"
        else
          rm -f "$f"
          log "removed log: $f"
        fi
      done
fi

log "done dry_run=$DRY_RUN environment=$MK04_ENV"
