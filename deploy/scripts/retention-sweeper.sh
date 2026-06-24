#!/usr/bin/env bash
# Delete clipping artefacts older than the configured threshold so disk does
# not fill on an unattended mk1 run.
#
# Targets:
#   - video-automation/jobs/*  (per-job folders: report.json, transcripts,
#                               clip mirrors, etc.). Heaviest contributor.
#   - video-automation/output/* (the public clip files)
#   - video-automation/temp/*  (whisper / chunk scratch)
#
# Explicitly NOT swept:
#   - video-automation/analytics/*.jsonl    (feedback / event log, kept for
#                                            future selection improvements)
#   - source-input/.../data/state/seen_urls.json  (dedupe state)
#   - source-input/.../data/inputs/ready/   (only video-automation deletes
#                                            after processing)
#   - output-funnel/data/output_funnel.sqlite3  (queue truth)
#
# Tunable via env: RETENTION_DAYS (default 14), or pass --days N.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_ARG="${MK04_ENV:-prod}"
if [[ "${1:-}" == "dev" || "${1:-}" == "prod" ]]; then
  ENV_ARG="$1"
  shift
fi
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh" "$ENV_ARG"

DAYS="${RETENTION_DAYS:-14}"
DRY_RUN=0
QUIET=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --days) DAYS="$2"; shift 2 ;;
    --days=*) DAYS="${1#*=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --quiet) QUIET=1; shift ;;
    -h|--help)
      cat <<USAGE
Usage: retention-sweeper.sh [--days N] [--dry-run] [--quiet]

Deletes clipping artefacts older than --days (default 14).
Set RETENTION_DAYS in the environment to override.
USAGE
      exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if ! [[ "$DAYS" =~ ^[0-9]+$ ]] || (( DAYS < 1 )); then
  echo "Refusing to run with non-positive retention: $DAYS" >&2
  exit 2
fi

declare -a TARGETS=(
  "$MK04_RUNTIME_ROOT/video-automation/jobs"
  "$MK04_RUNTIME_ROOT/video-automation/output"
  "$MK04_RUNTIME_ROOT/video-automation/temp"
)

LOG_TAG="mk04-${MK04_ENV}-retention"
log_info() {
  if command -v logger >/dev/null 2>&1; then
    logger -t "$LOG_TAG" -- "$@"
  fi
  (( QUIET )) || echo "[$LOG_TAG] $*"
}

total_freed_bytes=0
total_removed=0

sweep_dir() {
  local dir="$1"
  [[ -d "$dir" ]] || { log_info "skip absent dir=$dir"; return; }

  # ``du -sk`` is POSIX and works on Linux + macOS. Kilobytes are precise
  # enough for an operations log line. ``2>/dev/null || true`` survives
  # races where a target disappears mid-sweep.
  local removed_count removed_kb stats
  stats="$( {
      find "$dir" -mindepth 1 -maxdepth 1 -mtime +"$DAYS" -print0 \
        | xargs -0 -I{} du -sk "{}" 2>/dev/null \
        | awk '{ count += 1; sum += $1 } END { printf "%d %d\n", count+0, sum+0 }'
    } || echo "0 0" )"
  removed_count="${stats% *}"
  removed_kb="${stats#* }"
  removed_count="${removed_count:-0}"
  removed_kb="${removed_kb:-0}"

  if (( removed_count == 0 )); then
    log_info "dir=$dir nothing_to_remove threshold_days=$DAYS"
    return
  fi

  if (( DRY_RUN )); then
    log_info "DRY_RUN dir=$dir would_remove=$removed_count kb=$removed_kb"
    find "$dir" -mindepth 1 -maxdepth 1 -mtime +"$DAYS" -print | head -20
    return
  fi

  find "$dir" -mindepth 1 -maxdepth 1 -mtime +"$DAYS" -exec rm -rf {} +
  log_info "dir=$dir removed=$removed_count kb=$removed_kb"
  total_freed_bytes=$((total_freed_bytes + removed_kb * 1024))
  total_removed=$((total_removed + removed_count))
}

for t in "${TARGETS[@]}"; do
  sweep_dir "$t"
done

log_info "summary removed=$total_removed bytes=$total_freed_bytes days=$DAYS dry_run=$DRY_RUN"
exit 0
