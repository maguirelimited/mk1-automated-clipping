#!/usr/bin/env bash
# Install host log rotation artifacts from storage.log_rotation config.
#
# Writes:
#   /etc/logrotate.d/mk04          (deploy file sinks under /var/log/mk04)
#   /etc/systemd/journald.conf.d/mk04.conf  (journal size limits)
#
# Project logs under logs/<env>/ are rotated by scripts/ops/run-log-rotation.sh,
# not by this host install.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

usage() {
  cat <<'EOF'
Usage: sudo ./deploy/scripts/install-log-rotation.sh <environment>

Purpose:
  Render and install journald + logrotate snippets from storage.log_rotation.
  Does not rotate project logs (use scripts/ops/run-log-rotation.sh).

Environment:
  dev | prod

Requires root for writing /etc paths.
EOF
}

if [[ $# -eq 0 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  [[ $# -eq 0 ]] && exit 2
  exit 0
fi

ENV_ARG="$1"
case "${ENV_ARG,,}" in
  dev|development) ENV_TOKEN="dev"; CANONICAL="development" ;;
  prod|production) ENV_TOKEN="prod"; CANONICAL="production" ;;
  *)
    echo "Invalid environment: $ENV_ARG" >&2
    exit 2
    ;;
esac

if [[ "$(id -u)" -ne 0 ]]; then
  echo "install-log-rotation.sh requires root (sudo)." >&2
  exit 1
fi

PYTHON_BIN="${MK04_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$REPO_ROOT/video-automation/.venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_ROOT/video-automation/.venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3)"
  fi
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

"$PYTHON_BIN" - <<PY
import sys
from pathlib import Path

repo = Path(${REPO_ROOT@Q})
sys.path.insert(0, str(repo / "scripts" / "config"))
sys.path.insert(0, str(repo / "scripts"))

from config_manager import ConfigManager
from storage.log_rotation import (
    load_log_rotation_config,
    render_journald_dropin,
    render_logrotate_config,
)

resolved = ConfigManager.load(
    environment=${CANONICAL@Q},
    funnel_id="business",
    platform_id="youtube",
    config_root=repo / "config",
)
config = load_log_rotation_config(resolved)
out = Path(${TMP_DIR@Q})
(out / "mk04").write_text(render_logrotate_config(config, env_token=${ENV_TOKEN@Q}), encoding="utf-8")
(out / "mk04.conf").write_text(render_journald_dropin(config), encoding="utf-8")
print("rendered logrotate and journald drop-in")
PY

install -d -m 755 /etc/logrotate.d
install -d -m 755 /etc/systemd/journald.conf.d
install -m 644 "$TMP_DIR/mk04" /etc/logrotate.d/mk04
install -m 644 "$TMP_DIR/mk04.conf" /etc/systemd/journald.conf.d/mk04.conf

if command -v systemctl >/dev/null 2>&1; then
  systemctl restart systemd-journald || true
fi

echo "Installed:"
echo "  /etc/logrotate.d/mk04"
echo "  /etc/systemd/journald.conf.d/mk04.conf"
echo
echo "Project log rotation (cron or manual):"
echo "  $REPO_ROOT/scripts/ops/run-log-rotation.sh $ENV_TOKEN"
