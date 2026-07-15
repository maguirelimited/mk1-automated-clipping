#!/usr/bin/env bash
# Install / uninstall thin operator commands: dev, prod, promote.
#
# Default target: /usr/local/bin (requires root write access).
# Tests: --target-dir <tmpdir>
#
# Does not invoke sudo. Does not modify shell rc files. Does not install aliases.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
META_NAME="mk04-operator-commands.meta"
OWNED_MARKER="# mk04-operator-command"

usage() {
  cat <<EOF
Usage: deploy/scripts/install-operator-commands.sh [options]

Options:
  --target-dir DIR   Install directory (default: /usr/local/bin)
  --dev-root DIR     Development checkout to record (default: auto from this script)
  --user             Install to ~/.local/bin (fails clearly if not writable)
  --force            Overwrite existing files only if they are mk04-owned wrappers
  --uninstall        Remove mk04-owned wrappers and meta file
  --check            Validate installed wrappers (non-mutating)
  -h, --help         Show help

This installer does not bootstrap production and does not call sudo.
EOF
}

TARGET_DIR="/usr/local/bin"
USER_MODE=0
FORCE=0
UNINSTALL=0
CHECK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --target-dir)
      TARGET_DIR="${2:-}"; shift 2 ;;
    --dev-root)
      DEV_ROOT="${2:-}"; shift 2 ;;
    --user)
      USER_MODE=1
      TARGET_DIR="${HOME}/.local/bin"
      shift
      ;;
    --force) FORCE=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --check) CHECK=1; shift ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

DEV_ROOT="$(cd "$DEV_ROOT" && pwd)"
META_PATH="$TARGET_DIR/$META_NAME"

require_valid_dev_root() {
  local root="$1"
  if [[ ! -f "$root/scripts/ops/run-pipeline.sh" \
     || ! -f "$root/deploy/scripts/promote-to-prod.sh" \
     || ! -d "$root/config" \
     || ! -f "$root/scripts/ops/operator_commands.py" ]]; then
    echo "ERROR: Not a valid development checkout: $root" >&2
    exit 2
  fi
}

is_owned_wrapper() {
  local path="$1"
  [[ -f "$path" ]] && grep -q "$OWNED_MARKER" "$path" 2>/dev/null
}

find_python() {
  if [[ -x "$DEV_ROOT/video-automation/.venv/bin/python" ]]; then
    echo "$DEV_ROOT/video-automation/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    echo "ERROR: No usable Python interpreter." >&2
    exit 1
  fi
}

write_wrapper() {
  local name="$1"
  local dest="$TARGET_DIR/$name"
  local python
  python="$(find_python)"
  local tmp
  tmp="$(mktemp "$TARGET_DIR/.mk04-wrap.XXXXXX")"
  cat >"$tmp" <<EOF
#!/usr/bin/env bash
$OWNED_MARKER
# Installed by deploy/scripts/install-operator-commands.sh — do not hand-edit.
set -euo pipefail
export MK04_OPERATOR_META="$META_PATH"
export MK04_DEV_ROOT="\${MK04_DEV_ROOT:-$DEV_ROOT}"
exec "$python" "$DEV_ROOT/scripts/ops/operator_commands.py" "$name" "\$@"
EOF
  chmod 0755 "$tmp"
  mv -f "$tmp" "$dest"
}

install_commands() {
  require_valid_dev_root "$DEV_ROOT"

  if [[ "$USER_MODE" -eq 1 ]]; then
    if [[ ! -d "$TARGET_DIR" ]]; then
      echo "ERROR: $TARGET_DIR does not exist." >&2
      exit 1
    fi
    if [[ ! -w "$TARGET_DIR" ]]; then
      echo "ERROR: $TARGET_DIR is not writable by $(id -un)." >&2
      echo "On this host ~/.local/bin may be root-owned. Fix ownership manually or install to a writable --target-dir." >&2
      echo "This installer will not change directory ownership." >&2
      exit 1
    fi
  fi

  if [[ ! -d "$TARGET_DIR" ]]; then
    if mkdir -p "$TARGET_DIR" 2>/dev/null; then
      :
    else
      echo "ERROR: Cannot create $TARGET_DIR (root privileges may be required)." >&2
      echo "Re-run as root or pass --target-dir to a writable location. This script does not invoke sudo." >&2
      exit 1
    fi
  fi
  if [[ ! -w "$TARGET_DIR" ]]; then
    echo "ERROR: $TARGET_DIR is not writable (root privileges may be required)." >&2
    echo "This script does not invoke sudo." >&2
    exit 1
  fi

  local name
  for name in dev prod promote; do
    local dest="$TARGET_DIR/$name"
    if [[ -e "$dest" ]] && ! is_owned_wrapper "$dest"; then
      if [[ "$FORCE" -eq 1 ]]; then
        echo "ERROR: Refusing --force overwrite of unrelated file: $dest" >&2
        echo "Remove it manually if it is safe, then re-run without relying on --force for foreign files." >&2
        exit 1
      fi
      echo "ERROR: Refusing to overwrite unrelated existing command: $dest" >&2
      exit 1
    fi
  done

  local tmp_meta
  tmp_meta="$(mktemp "$TARGET_DIR/.mk04-meta.XXXXXX")"
  cat >"$tmp_meta" <<EOF
# $OWNED_MARKER meta
DEV_ROOT=$DEV_ROOT
INSTALLED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
TARGET_DIR=$TARGET_DIR
EOF
  mv -f "$tmp_meta" "$META_PATH"

  for name in dev prod promote; do
    write_wrapper "$name"
    echo "Installed $TARGET_DIR/$name"
  done
  echo "Recorded DEV_ROOT=$DEV_ROOT"
  echo "Meta: $META_PATH"
}

uninstall_commands() {
  if [[ ! -w "$TARGET_DIR" ]]; then
    echo "ERROR: $TARGET_DIR is not writable." >&2
    exit 1
  fi
  local name
  for name in dev prod promote; do
    local dest="$TARGET_DIR/$name"
    if [[ -e "$dest" ]]; then
      if is_owned_wrapper "$dest"; then
        rm -f "$dest"
        echo "Removed $dest"
      else
        echo "Leaving unrelated file untouched: $dest"
      fi
    fi
  done
  if [[ -f "$META_PATH" ]] && grep -q "$OWNED_MARKER" "$META_PATH" 2>/dev/null; then
    rm -f "$META_PATH"
    echo "Removed $META_PATH"
  fi
}

check_commands() {
  local failed=0
  local name
  for name in dev prod promote; do
    local dest="$TARGET_DIR/$name"
    if [[ ! -x "$dest" ]]; then
      echo "MISSING or not executable: $dest" >&2
      failed=1
      continue
    fi
    if ! is_owned_wrapper "$dest"; then
      echo "NOT mk04-owned: $dest" >&2
      failed=1
      continue
    fi
    if ! "$dest" --help >/dev/null 2>&1 && ! "$dest" -h >/dev/null 2>&1; then
      # argparse --help exits 0; some commands may print usage via argparse
      if ! "$dest" --help; then
        echo "HELP check failed: $dest" >&2
        failed=1
      fi
    else
      echo "OK $dest"
    fi
  done
  if [[ ! -f "$META_PATH" ]]; then
    echo "MISSING meta: $META_PATH" >&2
    failed=1
  fi
  return "$failed"
}

if [[ "$CHECK" -eq 1 ]]; then
  check_commands
  exit $?
fi

if [[ "$UNINSTALL" -eq 1 ]]; then
  uninstall_commands
  exit 0
fi

install_commands
# Non-mutating validation
check_commands || true
