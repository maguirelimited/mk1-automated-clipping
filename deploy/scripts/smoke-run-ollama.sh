#!/usr/bin/env bash
set -uo pipefail

# Lightweight smoke for run-ollama.sh. Verifies:
#   1. the script is syntactically valid,
#   2. a missing/unreachable backend is non-fatal in best-effort (default) mode,
#   3. the same condition is fatal under MK04_OLLAMA_STRICT=1,
#   4. an already-reachable backend is not started a second time.
#
# When a real Ollama is installed/serving on this host, the missing-backend
# assertions are skipped (we cannot simulate "missing" without uninstalling it),
# and the "already reachable" path is exercised instead.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_OLLAMA="$SCRIPT_DIR/run-ollama.sh"
UNREACHABLE_URL="http://127.0.0.1:1"
fail=0

note() { echo "[smoke-run-ollama] $*"; }

bash -n "$RUN_OLLAMA" || { note "FAIL: syntax error"; exit 1; }
note "syntax ok"

run_dev() {
  # Run against an unreachable backend so we never touch a real model.
  AI_BASE_URL="$UNREACHABLE_URL" \
  OLLAMA_START_WAIT_SECONDS=2 \
  MK04_SKIP_PROD_PREFLIGHT=1 \
  "$@" "$RUN_OLLAMA" dev >/tmp/mk04-smoke-ollama.out 2>&1
}

if command -v ollama >/dev/null 2>&1 && curl -fsS -m 2 "$(printf '%s' "${AI_BASE_URL:-http://localhost:11434}")/api/tags" >/dev/null 2>&1; then
  note "SKIP: a real Ollama backend is reachable; missing-backend assertions skipped."
  if MK04_SKIP_PROD_PREFLIGHT=1 "$RUN_OLLAMA" dev >/tmp/mk04-smoke-ollama.out 2>&1; then
    grep -q "already reachable" /tmp/mk04-smoke-ollama.out \
      && note "ok: existing backend not restarted" \
      || { note "FAIL: expected 'already reachable' message"; fail=1; }
  else
    note "FAIL: ensure against a live backend should exit 0"; fail=1
  fi
else
  if run_dev env MK04_OLLAMA_STRICT=0; then
    note "ok: best-effort mode exits 0 when backend is unreachable"
  else
    note "FAIL: best-effort mode should exit 0 when backend unreachable"; fail=1
  fi

  if run_dev env MK04_OLLAMA_STRICT=1; then
    note "FAIL: strict mode should exit non-zero when backend unreachable"; fail=1
  else
    note "ok: strict mode exits non-zero when backend unreachable"
  fi
fi

if ((fail == 0)); then
  echo "run_ollama_launcher_smoke_ok"
  exit 0
fi
exit 1
