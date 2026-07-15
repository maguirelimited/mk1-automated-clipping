#!/usr/bin/env bash
# Canonical production promotion entrypoint.
# Delegates to scripts/ops/promote_release.py (atomic versioned releases).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<EOF
Usage: deploy/scripts/promote-to-prod.sh [options]

Atomically promote the development checkout into versioned production releases:

  \$MK04_PROD_BASE/current  -> releases/<release_id>
  \$MK04_PROD_BASE/previous -> releases/<previous_id>
  \$MK04_PROD_BASE/releases/
  \$MK04_PROD_BASE/dependency-bundles/

Options:
  --dry-run                 Show plan without changing production
  --require-clean           Refuse dirty/untracked source trees
  --no-restart              Activate release without restarting services
  --full-tests              Also run video-automation/tests during staging validation
  --retain-releases N       Keep N newest releases (default 4: current+previous+2)
  --allow-first-bootstrap   Allow activation when systemd units are not installed
  --prod-base PATH          Override production base (default /opt/mk04/prod)
  --source PATH             Override source checkout
  -h, --help                Show this help

Promotion never enables uploads, never installs cron, and never runs a content pipeline.
Production code delivery must go through this command (not git pull inside current).
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

exec "$PYTHON" "$ROOT/scripts/ops/promote_release.py" "$@"
