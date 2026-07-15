#!/usr/bin/env bash
# Shared helpers for update.sh and run.sh (Prompt 8).
# shellcheck shell=bash

update_lib_root() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "$here/../.." && pwd
}

# Canonical shell normalizer lives in scripts/ops/lib/common.sh.
# shellcheck source=scripts/ops/lib/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

normalize_mk04_env() {
  normalize_ops_env "$@"
}

canonical_environment_name() {
  case "$1" in
    dev) echo "development" ;;
    prod) echo "production" ;;
    *) echo "$1" ;;
  esac
}

environment_label() {
  case "$1" in
    dev) echo "DEVELOPMENT" ;;
    prod) echo "PRODUCTION" ;;
    *) echo "${1^^}" ;;
  esac
}

iso_utc_now() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

find_repo_python() {
  local root="$1"
  local candidate
  for candidate in \
    "$root/video-automation/.venv/bin/python" \
    "$root/.venv/bin/python" \
    "$(command -v python3 2>/dev/null || true)" \
    "$(command -v python 2>/dev/null || true)"
  do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done
  echo "No usable Python interpreter found. Expected video-automation/.venv/bin/python or python3 on PATH." >&2
  return 1
}

git_commit_short() {
  local root="$1"
  if git -C "$root" rev-parse --short HEAD >/dev/null 2>&1; then
    git -C "$root" rev-parse --short HEAD
  else
    echo "unknown"
  fi
}

git_worktree_state() {
  local root="$1"
  if ! git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "not_a_git_repo"
    return 0
  fi
  if git -C "$root" diff --quiet && git -C "$root" diff --cached --quiet; then
    echo "clean"
  else
    echo "dirty"
  fi
}

check_python_dependencies() {
  local python="$1"
  "$python" - <<'PY'
import importlib
missing = []
for name in ("yaml", "pytest"):
    try:
        importlib.import_module(name)
    except ImportError:
        missing.append(name)
if missing:
    raise SystemExit("Missing Python modules: " + ", ".join(missing))
print("dependency check: PASS (yaml, pytest importable)")
PY
}

write_update_status() {
  local python="$1"
  local root="$2"
  shift 2
  "$python" "$root/scripts/ops/write_update_status.py" "$@"
}

MK04_KNOWN_SYSTEMD_UNITS=(
  mk04-source-input.service
  mk04-video-automation.service
  mk04-output-funnel.service
  mk04-ai-service.service
  mk04-ops-ui.service
)

restart_project_services() {
  local root="$1"
  local mk04_env="$2"

  if ! command -v systemctl >/dev/null 2>&1; then
    echo "Services restarted: skipped — systemctl not available"
    SERVICES_RESTARTED="skipped"
    return 0
  fi

  local configured=()
  local unit
  for unit in "${MK04_KNOWN_SYSTEMD_UNITS[@]}"; do
    if systemctl list-unit-files "$unit" 2>/dev/null | awk '{print $1}' | grep -qx "$unit"; then
      configured+=("$unit")
    fi
  done

  if ((${#configured[@]} == 0)); then
    echo "Services restarted: skipped — no mk04 systemd units configured"
    SERVICES_RESTARTED="skipped"
    return 0
  fi

  # shellcheck disable=SC1091
  source "$root/deploy/scripts/env.sh" "$mk04_env"

  local failed=0
  for unit in "${configured[@]}"; do
    echo "Restarting $unit ..."
    if ! systemctl restart "$unit"; then
      echo "Service restart failed: $unit" >&2
      failed=1
    fi
  done

  if ((failed != 0)); then
    SERVICES_RESTARTED="fail"
    return 1
  fi

  echo "Services restarted: PASS (${#configured[@]} unit(s))"
  SERVICES_RESTARTED="pass"
  return 0
}

bounded_health_check() {
  local root="$1"
  local mk04_env="$2"
  local services_restarted="$3"

  if [[ "$services_restarted" != "pass" ]]; then
    echo "Health check: local-only (service restart skipped or unavailable)"
    HEALTH_CHECK="local_only"
    echo "  - config validation completed earlier in this run"
    echo "  - service HTTP health not checked because services were not restarted"
    return 0
  fi

  if ! command -v curl >/dev/null 2>&1; then
    echo "Health check: not_available (curl missing)"
    HEALTH_CHECK="not_available"
    return 0
  fi

  # shellcheck disable=SC1091
  source "$root/deploy/scripts/env.sh" "$mk04_env"

  local ok=1
  _health_probe() {
    local label="$1"
    local url="$2"
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      echo "Health check: PASS ($label)"
    else
      echo "Health check: FAIL ($label unreachable at $url)" >&2
      ok=0
    fi
  }
  _health_probe "ops-ui" "http://127.0.0.1:${OPS_UI_PORT}/api/environment"
  _health_probe "video-automation" "http://127.0.0.1:${VIDEO_AUTOMATION_PORT}/healthz"

  if ((ok == 1)); then
    HEALTH_CHECK="pass"
  else
    HEALTH_CHECK="fail"
    return 1
  fi
  return 0
}
