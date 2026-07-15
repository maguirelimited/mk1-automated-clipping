#!/usr/bin/env bash
# Compatibility wrapper for historical cron lines.
#
# Reliability & Recovery Phase 9: scheduled automation uses
# scripts/ops/run-scheduled.sh, which only invokes run-pipeline.sh.
#
# Prefer:
#   ./scripts/ops/run-scheduled.sh prod <funnel_id>
#
# Legacy:
#   ./deploy/scripts/run-funnel-daily.sh prod <funnel_id>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

usage() {
  echo "usage: run-funnel-daily.sh <environment> <funnel_id>" >&2
  echo "Environment required: dev | development | prod | production" >&2
}

if [[ $# -lt 1 || -z "${1:-}" ]]; then
  usage
  exit 2
fi

ENV_ARG="$1"
shift

FUNNEL_ID="${1:-${RUN_FUNNEL_ID:-}}"
if [[ -z "$FUNNEL_ID" ]]; then
  usage
  exit 2
fi

# Normalize aliases (dev|development|prod|production) via ops helper, then
# delegate to the shared scheduled entrypoint.
# shellcheck source=scripts/ops/lib/common.sh
source "$REPO_ROOT/scripts/ops/lib/common.sh"
ENV_ARG="$(require_ops_env "$ENV_ARG")" || exit 2

exec "$REPO_ROOT/scripts/ops/run-scheduled.sh" "$ENV_ARG" "$FUNNEL_ID"
