#!/usr/bin/env bash
# Health watchdog for autonomous mk1 operation. Designed to be run from cron
# every 5–15 minutes. Failures are:
#   1) appended to a persistent alerts log (so they never vanish if mail /
#      WATCHDOG_NOTIFY are misconfigured)
#   2) summarised to a sentinel JSON file (one-liner tailable status)
#   3) logged to journald via logger(1)
#   4) (optional) piped to $WATCHDOG_NOTIFY for mail / Slack / ntfy
#
# Checks:
#   - deploy/scripts/doctor.sh (input + video + output-funnel /healthz)
#   - output-funnel /admin/stalled-jobs (any rows stuck longer than threshold)
#   - output-funnel /admin/last-upload (no uploads + pending queue → alarm)
#   - disk free on the persistent mk04 paths
#
# Exits 0 on green, non-zero on any failure (so cron MAILTO works too).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -lt 1 || -z "${1:-}" ]]; then
  echo "usage: watchdog.sh <environment>" >&2
  echo "Environment required: dev | development | prod | production" >&2
  exit 2
fi

# shellcheck disable=SC1091
source "$SCRIPT_DIR/env.sh" "$1"
shift || true

load_env_file "$MK04_SERVICE_ENV_DIR/output-funnel.env"
if [[ "$MK04_ENV" == "dev" ]]; then
  load_env_file_defaults "$MK04_ROOT/output-funnel/.env"
fi
mk04_export_runtime

LOG_TAG="mk04-${MK04_ENV}-watchdog"
WATCHDOG_DIR="${WATCHDOG_LOG_DIR:-$MK04_LOG_ROOT/watchdog}"
WATCHDOG_FALLBACK_DIR="$MK04_ROOT/deploy/.watchdog/$MK04_ENV"
SUMMARY_FILE="$(mktemp -t mk04-watchdog.XXXXXX)"
trap 'rm -f "$SUMMARY_FILE"' EXIT

# Disk-free threshold: alarm when ANY of the persistent paths has < this %
# free space. 10% is conservative for a single-host mk1 SSD.
DISK_MIN_FREE_PCT="${WATCHDOG_DISK_MIN_FREE_PCT:-10}"
# Stale-upload threshold: if the last successful upload is older than this
# AND the queue has pending jobs, alarm. Disabled by default until first
# real upload happens; cron environment can set WATCHDOG_STALE_UPLOAD_HOURS.
STALE_UPLOAD_HOURS="${WATCHDOG_STALE_UPLOAD_HOURS:-48}"

# Resolve a writable log directory. Prefer the environment-specific log root;
# fall back to the repo if we lack permission so cron does not silently fail.
init_log_dir() {
  if mkdir -p "$WATCHDOG_DIR" 2>/dev/null && [[ -w "$WATCHDOG_DIR" ]]; then
    return 0
  fi
  WATCHDOG_DIR="$WATCHDOG_FALLBACK_DIR"
  mkdir -p "$WATCHDOG_DIR"
}
init_log_dir
STATUS_FILE="$WATCHDOG_DIR/last_status.json"
ALERTS_FILE="$WATCHDOG_DIR/alerts.log"

log_info() {
  if command -v logger >/dev/null 2>&1; then
    logger -t "$LOG_TAG" -- "$@"
  fi
  echo "[$LOG_TAG] $*"
}

notify() {
  local subject="$1"
  if [[ -n "${WATCHDOG_NOTIFY:-}" ]]; then
    # If the notify command fails, log the failure but never lose the alert
    # itself — alerts.log already holds the canonical record.
    if ! printf '%s\n\n' "$subject" | cat - "$SUMMARY_FILE" | eval "$WATCHDOG_NOTIFY" 2>>"$ALERTS_FILE"; then
      echo "[$(date -u +%FT%TZ)] notify_command_failed: $WATCHDOG_NOTIFY" >>"$ALERTS_FILE"
    fi
  fi
}

declare -a failure_messages=()
fail() {
  failure_messages+=("$1")
}

check_disk_free() {
  local paths=(
    "$MK04_ROOT"
    "$MK04_RUNTIME_ROOT/video-automation/jobs"
    "$MK04_RUNTIME_ROOT/video-automation/output"
    "$MK04_RUNTIME_ROOT/output-funnel"
  )
  local checked=()
  for p in "${paths[@]}"; do
    [[ -e "$p" ]] || continue
    local used_pct
    used_pct="$(df -P "$p" 2>/dev/null | awk 'NR==2 { gsub("%","",$5); print $5 }')"
    if [[ -z "$used_pct" ]]; then
      continue
    fi
    local free_pct=$((100 - used_pct))
    local checked_entry="$p:${free_pct}%free"
    checked+=("$checked_entry")
    if (( free_pct < DISK_MIN_FREE_PCT )); then
      fail "disk_low: $p (${free_pct}% free, threshold ${DISK_MIN_FREE_PCT}%)"
    fi
  done
  echo "disk: ${checked[*]:-no paths checked}"
}

declare -a curl_headers=()
if [[ -n "${OUTPUT_FUNNEL_SECRET:-}" ]]; then
  curl_headers=(-H "X-Output-Funnel-Secret: $OUTPUT_FUNNEL_SECRET")
fi
OUTPUT_FUNNEL_HOST_NORM="${OUTPUT_FUNNEL_HOST:-127.0.0.1}"
[[ "$OUTPUT_FUNNEL_HOST_NORM" == "0.0.0.0" || "$OUTPUT_FUNNEL_HOST_NORM" == "::" ]] && OUTPUT_FUNNEL_HOST_NORM="127.0.0.1"
OUTPUT_FUNNEL_BASE="http://${OUTPUT_FUNNEL_HOST_NORM}:${OUTPUT_FUNNEL_PORT}"

check_stalled_jobs() {
  local url="${OUTPUT_FUNNEL_BASE}/admin/stalled-jobs"
  local response
  if ! response="$(curl -fsS "${curl_headers[@]}" "$url" 2>&1)"; then
    fail "stalled-jobs endpoint unreachable: $response"
    return
  fi
  echo "$response"
  local count
  count="$(echo "$response" | python3 -c "import json,sys
try:
    print(int((json.load(sys.stdin) or {}).get('count') or 0))
except Exception:
    print(-1)")"
  if [[ "$count" -gt 0 ]]; then
    fail "stalled_jobs: $count rows stuck past threshold"
  elif [[ "$count" -lt 0 ]]; then
    fail "stalled_jobs: parse error"
  fi
}

check_last_upload() {
  local url="${OUTPUT_FUNNEL_BASE}/admin/last-upload"
  local response
  if ! response="$(curl -fsS "${curl_headers[@]}" "$url" 2>&1)"; then
    fail "last-upload endpoint unreachable: $response"
    return
  fi
  echo "$response"
  # The endpoint returns last_upload_at (ISO or null) and pending_count.
  # We alarm only when there are pending jobs AND last_upload_at is either
  # null or older than the threshold. A pristine empty queue is fine.
  local verdict
  verdict="$(echo "$response" | STALE_HOURS="$STALE_UPLOAD_HOURS" python3 - <<'PY'
import json, os, sys
from datetime import datetime, timezone
try:
    payload = json.load(sys.stdin) or {}
except Exception:
    print("parse_error"); sys.exit(0)
pending = int(payload.get("pending_count") or 0)
last = payload.get("last_upload_at")
hours = float(os.environ.get("STALE_HOURS", "48"))
if pending <= 0:
    print(f"ok pending={pending} last={last}")
    sys.exit(0)
if last is None:
    print(f"never_uploaded pending={pending}")
    sys.exit(0)
try:
    parsed = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
except Exception:
    print(f"unparseable last={last}")
    sys.exit(0)
if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone.utc)
age_h = (datetime.now(timezone.utc) - parsed).total_seconds() / 3600.0
if age_h > hours:
    print(f"stale_uploads age_h={age_h:.1f} threshold_h={hours} pending={pending} last={last}")
else:
    print(f"ok pending={pending} age_h={age_h:.1f}")
PY
)"
  echo "last_upload_check: $verdict"
  case "$verdict" in
    ok*) ;;
    never_uploaded*|stale_uploads*|unparseable*|parse_error*)
      fail "last_upload: $verdict"
      ;;
  esac
}

{
  echo "== mk04 watchdog $(date -u +%FT%TZ) =="

  echo
  echo "-- disk free --"
  check_disk_free

  echo
  echo "-- doctor.sh --"
  if "$SCRIPT_DIR/doctor.sh" "$MK04_ENV" 2>&1; then
    echo "doctor: OK"
  else
    fail "doctor: FAIL"
  fi

  echo
  echo "-- output-funnel stalled jobs --"
  check_stalled_jobs

  echo
  echo "-- output-funnel last upload --"
  check_last_upload
} | tee "$SUMMARY_FILE"

failures="${#failure_messages[@]}"
timestamp="$(date -u +%FT%TZ)"

write_status() {
  local ok="$1"
  python3 - "$STATUS_FILE" "$timestamp" "$ok" "$failures" "${failure_messages[@]}" <<'PY'
import json, sys
path, ts, ok, failures, *fails = sys.argv[1:]
with open(path, "w", encoding="utf-8") as f:
    json.dump(
        {
            "ok": ok == "1",
            "timestamp": ts,
            "failures": int(failures),
            "failure_messages": fails,
        },
        f,
        indent=2,
        sort_keys=True,
    )
    f.write("\n")
PY
}

if (( failures > 0 )); then
  write_status 0
  {
    echo "[$timestamp] FAIL failures=$failures"
    for msg in "${failure_messages[@]}"; do
      echo "  - $msg"
    done
  } >>"$ALERTS_FILE"
  log_info "FAIL failures=$failures"
  notify "mk04 watchdog FAIL ($failures)"
  exit 1
fi

write_status 1
log_info "OK"
exit 0
