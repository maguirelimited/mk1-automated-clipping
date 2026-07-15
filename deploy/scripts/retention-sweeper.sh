#!/usr/bin/env bash
# LEGACY pre-Phase-8 ad-hoc cleaner.
#
# Cron no longer invokes this script. Prefer the config-driven retention engine:
#   scripts/ops/run-scheduled-retention.sh <env>
#   scripts/retention.py --dry-run|--apply <env>
# See docs/storage/SCHEDULED_RETENTION.md.
#
# Tiered cleanup of clipping artefacts so disk does not fill on an unattended
# mk1 run, while small metadata is kept long enough for debugging / analytics /
# review / training.
#
# Two age thresholds:
#   MEDIA_RETENTION_DAYS (default 5)  -> large media (source copies + clip mp4s)
#   RETENTION_DAYS       (default 14) -> whole per-job folders incl. metadata
#
# What is deleted at the MEDIA threshold:
#   - video-automation/jobs/*/input_*        (per-job source copy; large dup)
#   - video-automation/jobs/*/clips/*        (per-job clip mirrors; large dup)
#   - video-automation/output/*              (public clip files)
#   - video-automation/temp/*                (whisper / chunk scratch)
#   - video-automation/input/*               (orphaned ready-slot source files)
#   - source-input/tmp/*                     (orphaned/leaked download scratch)
#   - source-input/inputs/rejected/*         (rejected media + .reason.txt sidecars)
#
# What is PRESERVED until the (longer) METADATA threshold, then removed only by
# whole-folder deletion of a fully-aged job dir:
#   - video-automation/jobs/*/report.json, selection.json, transcript.json,
#     transcript_payload.json, analytics.json, review.md, task.json
#
# Explicitly NOT swept (any threshold):
#   - video-automation/analytics/*.jsonl    (feedback / event log)
#   - source-input/state/seen_urls.json     (dedupe state)
#   - source-input/state/input_jobs/*       (input ledger)
#   - source-input/inputs/ready/*           (fallback store of real input)
#   - output-funnel/output_funnel.sqlite3   (queue truth)
#
# Tunable via env: MEDIA_RETENTION_DAYS, RETENTION_DAYS; or pass
# --media-days N / --days N. Use --dry-run to preview.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -lt 1 || -z "${1:-}" ]]; then
  echo "usage: retention-sweeper.sh <environment> [options]" >&2
  echo "Environment required: dev | development | prod | production" >&2
  exit 2
fi

ENV_ARG="$1"
shift
# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh" "$ENV_ARG"

MEDIA_DAYS="${MEDIA_RETENTION_DAYS:-5}"
DAYS="${RETENTION_DAYS:-14}"
DRY_RUN=0
QUIET=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --media-days) MEDIA_DAYS="$2"; shift 2 ;;
    --media-days=*) MEDIA_DAYS="${1#*=}"; shift ;;
    --days) DAYS="$2"; shift 2 ;;
    --days=*) DAYS="${1#*=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --quiet) QUIET=1; shift ;;
    -h|--help)
      cat <<USAGE
Usage: retention-sweeper.sh [dev|prod] [--media-days N] [--days N] [--dry-run] [--quiet]

Tiered cleanup of clipping artefacts:
  --media-days N  large media TTL (default 5; env MEDIA_RETENTION_DAYS)
  --days N        whole per-job folder / metadata TTL (default 14; env RETENTION_DAYS)

Large media (per-job input_* copies, per-job clip mirrors, output clips, temp
scratch, orphaned input/ source files) is removed at the media TTL. Job metadata
(report.json, selection.json, transcript*.json, analytics.json, review.md,
task.json) is preserved until the whole job folder ages past the metadata TTL.
USAGE
      exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if ! [[ "$MEDIA_DAYS" =~ ^[0-9]+$ ]] || (( MEDIA_DAYS < 1 )); then
  echo "Refusing to run with non-positive media retention: $MEDIA_DAYS" >&2
  exit 2
fi
if ! [[ "$DAYS" =~ ^[0-9]+$ ]] || (( DAYS < 1 )); then
  echo "Refusing to run with non-positive retention: $DAYS" >&2
  exit 2
fi

VA_ROOT="$MK04_RUNTIME_ROOT/video-automation"
JOBS_DIR="$VA_ROOT/jobs"
OUTPUT_DIR="$VA_ROOT/output"
TEMP_DIR="$VA_ROOT/temp"
INPUT_DIR="${VIDEO_AUTOMATION_INPUT_DIR:-$VA_ROOT/input}"

# Source-input data root (env.sh exports INPUT_SERVICE_DATA_DIR=$MK04_RUNTIME_ROOT/source-input).
# tmp/ holds in-flight download scratch (leaks on failure); inputs/rejected/
# holds failed-validation media + .reason.txt sidecars. Both are large/low-value
# and never otherwise cleaned. state/ (seen_urls + ledger) and inputs/ready/ are
# deliberately left untouched (see header).
SI_ROOT="${INPUT_SERVICE_DATA_DIR:-$MK04_RUNTIME_ROOT/source-input}"
SI_TMP_DIR="$SI_ROOT/tmp"
SI_REJECTED_DIR="$SI_ROOT/inputs/rejected"

LOG_TAG="mk04-${MK04_ENV}-retention"
log_info() {
  if command -v logger >/dev/null 2>&1; then
    logger -t "$LOG_TAG" -- "$@"
  fi
  (( QUIET )) || echo "[$LOG_TAG] $*"
}

if (( MEDIA_DAYS > DAYS )); then
  log_info "warn media_days=$MEDIA_DAYS exceeds metadata_days=$DAYS"
fi

total_freed_bytes=0
total_removed=0

# Delete every path matched by the supplied find predicate, logging a count and
# size. ``du -sk`` is POSIX (Linux + macOS); ``2>/dev/null || true`` survives
# races where a target disappears mid-sweep.
sweep_find() {
  local label="$1" base="$2"; shift 2
  [[ -d "$base" ]] || { log_info "skip absent $label dir=$base"; return; }

  local stats count kb
  stats="$( {
      find "$base" "$@" -print0 2>/dev/null \
        | xargs -0 -r du -sk 2>/dev/null \
        | awk '{ count += 1; sum += $1 } END { printf "%d %d\n", count+0, sum+0 }'
    } || echo "0 0" )"
  count="${stats%% *}"
  kb="${stats##* }"
  count="${count:-0}"
  kb="${kb:-0}"

  if (( count == 0 )); then
    log_info "$label dir=$base nothing_to_remove"
    return
  fi

  if (( DRY_RUN )); then
    log_info "DRY_RUN $label dir=$base would_remove=$count kb=$kb"
    find "$base" "$@" -print 2>/dev/null | head -20 || true
    return
  fi

  find "$base" "$@" -print0 2>/dev/null | xargs -0 -r rm -rf
  log_info "$label dir=$base removed=$count kb=$kb"
  total_freed_bytes=$((total_freed_bytes + kb * 1024))
  total_removed=$((total_removed + count))
}

# Delete whole per-job folders once nothing inside them is newer than the
# metadata TTL. Keyed off file mtimes (not the directory mtime) so removing
# media earlier in the sweep does not reset the folder's apparent age.
sweep_stale_job_dirs() {
  local base="$1"
  [[ -d "$base" ]] || { log_info "skip absent jobs/whole dir=$base"; return; }

  local jobdir kb count=0 removed_kb=0
  while IFS= read -r -d '' jobdir; do
    if [[ -n "$(find "$jobdir" -type f -mtime -"$DAYS" -print 2>/dev/null)" ]]; then
      continue
    fi
    kb="$(du -sk "$jobdir" 2>/dev/null | awk '{ print $1+0 }')"
    kb="${kb:-0}"
    if (( DRY_RUN )); then
      log_info "DRY_RUN jobs/whole would_remove=$jobdir kb=$kb"
      continue
    fi
    rm -rf "$jobdir" 2>/dev/null || true
    count=$((count + 1))
    removed_kb=$((removed_kb + kb))
  done < <(find "$base" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null)

  (( DRY_RUN )) && return
  log_info "jobs/whole dir=$base removed=$count kb=$removed_kb threshold_days=$DAYS"
  total_freed_bytes=$((total_freed_bytes + removed_kb * 1024))
  total_removed=$((total_removed + count))
}

# Large media inside per-job folders (kept metadata is untouched).
sweep_find "jobs/input-copy" "$JOBS_DIR" \
  -mindepth 2 -maxdepth 2 -type f -name 'input_*' -mtime +"$MEDIA_DAYS"
sweep_find "jobs/clips" "$JOBS_DIR" \
  -mindepth 3 -type f -path '*/clips/*' -mtime +"$MEDIA_DAYS"

# Public + scratch media on the short media TTL.
sweep_find "output" "$OUTPUT_DIR" \
  -mindepth 1 -maxdepth 1 -mtime +"$MEDIA_DAYS"
sweep_find "temp" "$TEMP_DIR" \
  -mindepth 1 -maxdepth 1 -mtime +"$MEDIA_DAYS"
sweep_find "input" "$INPUT_DIR" \
  -mindepth 1 -maxdepth 1 -type f -mtime +"$MEDIA_DAYS"

# Source-input large/low-value media on the short media TTL. tmp/ leaks on
# download failure; rejected/ media (+ its tiny .reason.txt debug sidecar) is
# never otherwise cleaned. state/ and inputs/ready/ are not touched.
sweep_find "source-input/tmp" "$SI_TMP_DIR" \
  -mindepth 1 -maxdepth 1 -mtime +"$MEDIA_DAYS"
sweep_find "source-input/rejected" "$SI_REJECTED_DIR" \
  -mindepth 1 -maxdepth 1 -mtime +"$MEDIA_DAYS"

# Fully-aged job folders (metadata included) on the long metadata TTL.
sweep_stale_job_dirs "$JOBS_DIR"

log_info "summary removed=$total_removed bytes=$total_freed_bytes media_days=$MEDIA_DAYS metadata_days=$DAYS dry_run=$DRY_RUN"
exit 0
