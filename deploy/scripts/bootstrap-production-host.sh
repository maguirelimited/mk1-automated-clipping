#!/usr/bin/env bash
# Canonical first-production-host bootstrap entrypoint.
# Delegates to scripts/ops/bootstrap_production_host.py.
#
# Does not install cron. Does not run a content pipeline. Does not enable uploads.
# Privileged mutation requires --apply. Prefer --dry-run / --plan-only first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<EOF
Usage: deploy/scripts/bootstrap-production-host.sh [options]

Idempotent production host bootstrap with explicit phases:

  prepare-host | seed-config | promote | component-bootstrap |
  reconcile-permissions | install-services | install-commands | verify

Common options:
  --operator USER       Human operator to add to mk04 group (default: maguireltd)
  --dev-root PATH       Absolute development checkout
  --dry-run             Plan only; no writes
  --plan-only           Print plan and exit
  --apply               Required for real host mutation
  --phase NAME          Run one phase (repeatable)
  --stop-before PHASE   Pause before a phase (operator gate)
  --commands-target DIR Install one-word commands here (default /usr/local/bin)
  -h, --help            Show help

Safety:
  - Never installs cron
  - Never runs a content pipeline
  - Never prints secret values
  - Never recursively modifies /etc/mk04/dev, /var/lib/mk04/dev, /var/log/mk04/dev
  - Lock directory is created before /etc/mk04/prod
EOF
  exit 0
fi

PYTHON=""
if [[ -x "$ROOT/video-automation/.venv/bin/python" ]]; then
  PYTHON="$ROOT/video-automation/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "No usable Python interpreter found." >&2
  exit 2
fi

exec "$PYTHON" "$ROOT/scripts/ops/bootstrap_production_host.py" "$@"
