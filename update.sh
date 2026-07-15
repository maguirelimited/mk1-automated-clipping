#!/usr/bin/env bash
# Safe system update for a single mk04 environment (Prompt 8).
#
# Usage:
#   ./update.sh dev
#   ./update.sh prod
#   ./update.sh prod --no-restart
#   ./update.sh prod --check-only
#   ./update.sh dev --full-tests
#
# Production code delivery is ONLY via:
#   ./deploy/scripts/promote-to-prod.sh
# update.sh prod validates/restarts the already-selected current release.
# Do not git pull inside /opt/mk04/prod/current.
#
# Development workflow:
#   git pull
#   ./update.sh dev
#
# Production code delivery:
#   ./deploy/scripts/promote-to-prod.sh
# then optionally validate/restart the selected current release:
#   ./update.sh prod
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/ops/update_lib.sh
source "$ROOT/scripts/ops/update_lib.sh"

usage() {
  cat <<'EOF'
Usage: ./update.sh <environment> [options]

Environment (required):
  dev | development
  prod | production

Options:
  --pull          Dev only: git pull --ff-only before checks (refused for prod)
  --full-tests    Also run video-automation/tests (slow; 4 known failures may remain)
  --no-restart    Skip systemd service restart even when units exist
  --check-only    Stop after config validation and dependency checks
  -h, --help      Show this help

Examples:
  ./update.sh dev
  ./update.sh prod
  ./deploy/scripts/promote-to-prod.sh   # production code delivery
  ./update.sh prod --no-restart         # validate/restart selected current release
EOF
}

ENV_ARG=""
DO_PULL=0
FULL_TESTS=0
NO_RESTART=0
CHECK_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --pull)
      DO_PULL=1
      shift
      ;;
    --full-tests)
      FULL_TESTS=1
      shift
      ;;
    --no-restart)
      NO_RESTART=1
      shift
      ;;
    --check-only)
      CHECK_ONLY=1
      shift
      ;;
    dev|development|prod|production)
      if [[ -n "$ENV_ARG" ]]; then
        echo "Only one environment argument is allowed." >&2
        usage >&2
        exit 2
      fi
      ENV_ARG="$1"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$ENV_ARG" ]]; then
  echo "Environment argument is required." >&2
  usage >&2
  exit 2
fi

if ! MK04_ENV="$(normalize_mk04_env "$ENV_ARG")"; then
  usage >&2
  exit 2
fi
export MK04_ENV

CANONICAL_ENV="$(canonical_environment_name "$MK04_ENV")"
ENV_LABEL="$(environment_label "$MK04_ENV")"

STARTED_AT="$(iso_utc_now)"
COMMIT="$(git_commit_short "$ROOT")"
WORKTREE="$(git_worktree_state "$ROOT")"

CONFIG_VALIDATION="unknown"
TESTS_STATUS="unknown"
SERVICES_RESTARTED="skipped"
HEALTH_CHECK="not_available"
FINAL_STATUS="failure"
FAIL_MESSAGE=""

write_status() {
  local status="$1"
  write_update_status \
    "$PYTHON" "$ROOT" \
    --env "$MK04_ENV" \
    --status "$status" \
    --started-at "$STARTED_AT" \
    --commit "$COMMIT" \
    --config-validation "$CONFIG_VALIDATION" \
    --tests "$TESTS_STATUS" \
    --services-restarted "$SERVICES_RESTARTED" \
    --health-check "$HEALTH_CHECK" \
    ${FAIL_MESSAGE:+--message "$FAIL_MESSAGE"} || true
}

on_exit() {
  local code=$?
  if [[ "$FINAL_STATUS" == "success" && "$code" -ne 0 ]]; then
    FINAL_STATUS="failure"
    FAIL_MESSAGE="${FAIL_MESSAGE:-update exited with status $code}"
  fi
  if [[ -n "${PYTHON:-}" ]]; then
    write_status "$FINAL_STATUS"
  fi
}
trap on_exit EXIT

echo "============================================================"
echo "mk04 system update"
echo "  environment: $CANONICAL_ENV ($ENV_LABEL)"
echo "  MK04_ENV:    $MK04_ENV"
echo "  commit:      $COMMIT"
echo "  worktree:    $WORKTREE"
echo "============================================================"

if [[ "$WORKTREE" == "dirty" ]]; then
  echo "WARNING: git working tree is dirty. Commit or stash changes before production update."
fi

if [[ "$DO_PULL" -eq 1 ]]; then
  if [[ "$MK04_ENV" == "prod" ]]; then
    FAIL_MESSAGE="update.sh prod refuses --pull: production code delivery is only via deploy/scripts/promote-to-prod.sh"
    echo "ERROR: $FAIL_MESSAGE" >&2
    echo "Promote from the development checkout, then run ./update.sh prod to validate/restart the selected current release." >&2
    exit 2
  fi
  echo "Running git pull --ff-only ..."
  git -C "$ROOT" pull --ff-only
  COMMIT="$(git_commit_short "$ROOT")"
  echo "Updated commit: $COMMIT"
fi

# Production must not mutate an active release tree in place.
if [[ "$MK04_ENV" == "prod" ]]; then
  if [[ -L "$ROOT" ]] || [[ -L "$(dirname "$ROOT")/current" ]]; then
    :
  fi
  # If we are executing from a path that looks like a versioned release, refuse
  # any implication of in-place code delivery. Validation/restart of current is OK.
  case "$ROOT" in
    */releases/*)
      echo "NOTE: running update.sh from a versioned release path."
      echo "      Code delivery must use deploy/scripts/promote-to-prod.sh; this run only validates/restarts."
      ;;
  esac
  if [[ "${MK04_UPDATE_ALLOW_INPLACE_PROD:-}" == "1" ]]; then
    echo "ERROR: MK04_UPDATE_ALLOW_INPLACE_PROD is no longer supported." >&2
    exit 2
  fi
fi

if ! PYTHON="$(find_repo_python "$ROOT")"; then
  FAIL_MESSAGE="Python interpreter not found"
  exit 1
fi
echo "Python interpreter: $PYTHON"

echo
echo "[1/6] Dependency checks"
if ! check_python_dependencies "$PYTHON"; then
  FAIL_MESSAGE="Required Python dependencies missing (yaml, pytest)"
  CONFIG_VALIDATION="fail"
  exit 1
fi

echo
echo "[2/6] Config validation"
if ! "$PYTHON" "$ROOT/scripts/config/validate_config.py"; then
  CONFIG_VALIDATION="fail"
  FAIL_MESSAGE="Config schema validation failed"
  exit 1
fi
CONFIG_VALIDATION="pass"
echo "Config validation: PASS"

echo
echo "[3/6] ConfigManager summary"
if ! "$PYTHON" "$ROOT/scripts/config/config_manager.py" \
  --env "$MK04_ENV" \
  --funnel business \
  --platform youtube \
  --print-summary; then
  CONFIG_VALIDATION="fail"
  FAIL_MESSAGE="ConfigManager summary failed"
  exit 1
fi
echo "ConfigManager summary: PASS"

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  echo
  echo "Check-only mode: skipping tests, service restart, and health checks."
  TESTS_STATUS="skipped"
  SERVICES_RESTARTED="skipped"
  HEALTH_CHECK="not_available"
  FINAL_STATUS="success"
  echo
  echo "============================================================"
  echo "Update status: SUCCESS (check-only)"
  echo "  environment:         $CANONICAL_ENV"
  echo "  commit:              $COMMIT"
  echo "  config_validation:   $CONFIG_VALIDATION"
  echo "  tests:               $TESTS_STATUS"
  echo "  services_restarted:  $SERVICES_RESTARTED"
  echo "  health_check:        $HEALTH_CHECK"
  echo "============================================================"
  exit 0
fi

echo
echo "[4/6] Lightweight tests"
echo "Running: pytest tests/config"
if ! "$PYTHON" -m pytest "$ROOT/tests/config" -q; then
  TESTS_STATUS="fail"
  FAIL_MESSAGE="tests/config failed"
  exit 1
fi
echo "Running: pytest ops-ui/tests"
if ! "$PYTHON" -m pytest "$ROOT/ops-ui/tests" -q; then
  TESTS_STATUS="fail"
  FAIL_MESSAGE="ops-ui/tests failed"
  exit 1
fi
TESTS_STATUS="pass"
echo "Lightweight tests: PASS"

if [[ "$FULL_TESTS" -eq 1 ]]; then
  echo "Running full video-automation/tests (--full-tests)"
  if ! "$PYTHON" -m pytest "$ROOT/video-automation/tests" -q; then
    TESTS_STATUS="fail"
    FAIL_MESSAGE="video-automation/tests failed"
    exit 1
  fi
  echo "Full video tests: PASS"
else
  echo "Full video-automation/tests skipped by default (use --full-tests to include)."
fi

echo
echo "[5/6] Service restart"
if [[ "$NO_RESTART" -eq 1 ]]; then
  echo "Services restarted: skipped (--no-restart)"
  SERVICES_RESTARTED="skipped"
elif ! restart_project_services "$ROOT" "$MK04_ENV"; then
  FAIL_MESSAGE="One or more service restarts failed"
  exit 1
fi

echo
echo "[6/6] Health check"
if ! bounded_health_check "$ROOT" "$MK04_ENV" "$SERVICES_RESTARTED"; then
  FAIL_MESSAGE="Health check failed"
  exit 1
fi

FINAL_STATUS="success"
echo
echo "============================================================"
echo "Update status: SUCCESS"
echo "  environment:         $CANONICAL_ENV"
echo "  commit:              $COMMIT"
echo "  config_validation:   $CONFIG_VALIDATION"
echo "  tests:               $TESTS_STATUS"
echo "  services_restarted:  $SERVICES_RESTARTED"
echo "  health_check:        $HEALTH_CHECK"
echo "  status file:         data/$MK04_ENV/last_update_status.json (via ConfigManager paths)"
echo "============================================================"
exit 0
