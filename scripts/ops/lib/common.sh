#!/usr/bin/env bash
# Shared helpers for scripts/ops/* (Remote Operations scaffolding).
# Real operational behaviour belongs in individual scripts, not here.
# shellcheck shell=bash

_ops_lib_dir() {
  cd "$(dirname "${BASH_SOURCE[0]}")" && pwd
}

ops_scripts_dir() {
  cd "$(_ops_lib_dir)/.." && pwd
}

ops_repo_root() {
  cd "$(ops_scripts_dir)/../.." && pwd
}

is_help_flag() {
  case "${1:-}" in
    -h|--help)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

normalize_ops_env() {
  local raw="${1:-}"
  case "${raw,,}" in
    dev|development)
      echo "dev"
      ;;
    prod|production)
      echo "prod"
      ;;
    *)
      echo "Invalid environment: ${raw:-<missing>}. Expected dev, development, prod, or production." >&2
      return 1
      ;;
  esac
}

require_ops_env() {
  local raw="${1:-}"
  if [[ -z "$raw" ]]; then
    echo "Error: environment argument required (dev, development, prod, or production)." >&2
    return 1
  fi
  normalize_ops_env "$raw"
}

print_placeholder() {
  local script_name="$1"
  local future_note="$2"
  shift 2

  echo "${script_name}: placeholder only."
  echo "${future_note}"
  while [[ $# -gt 0 ]]; do
    echo "$1"
    shift
  done
}

ops_find_python() {
  local root
  root="$(ops_repo_root)"
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
  echo "No usable Python interpreter found." >&2
  return 1
}
