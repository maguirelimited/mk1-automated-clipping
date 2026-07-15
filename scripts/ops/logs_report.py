#!/usr/bin/env python3
"""Read-only log retrieval for scripts/ops/logs.sh."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ops_readonly import (  # noqa: E402
    DEFAULT_LOG_LINES,
    LOG_MODE_UNITS,
    LOG_MODES,
    MAX_LOG_LINES,
    REPO_ROOT,
    SCHEDULER_LOG_MARKERS,
    canonical_env,
    ensure_config_scripts_on_path,
    env_label,
    mk04_env,
    run_command,
    systemd_not_running,
    systemctl_available,
)

ensure_config_scripts_on_path()
from config_manager import ConfigError, ConfigManager  # noqa: E402
from state_paths import EnvironmentStatePaths  # noqa: E402

ERROR_LINE_RE = re.compile(
    r"ERROR|FAIL(?:ED|URE)?|Traceback|Exception|CRITICAL",
    re.IGNORECASE,
)

_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b("
    r"OPENAI_API_KEY|API_KEY|TOKEN|SECRET|PASSWORD|AUTHORIZATION|BEARER|COOKIE|"
    r"CLIENT_SECRET|ACCESS_TOKEN|REFRESH_TOKEN"
    r")\s*=\s*\S+"
)

_AUTH_BEARER_RE = re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)\S+")
_PASSWORD_RE = re.compile(r"(?i)(password\s*=\s*)\S+")
_SK_TOKEN_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b")

LOG_EXTENSIONS = {".log", ".txt", ".ndjson", ".jsonl"}


@dataclass
class LogResult:
    source: str
    lines: list[str]
    unavailable: bool = False
    empty: bool = False


def clamp_lines(raw: int) -> int:
    if raw < 1:
        return DEFAULT_LOG_LINES
    return min(raw, MAX_LOG_LINES)


def redact_line(line: str) -> str:
    text = _SECRET_ASSIGN_RE.sub(r"\1=<redacted>", line)
    text = _AUTH_BEARER_RE.sub(r"\1<redacted>", text)
    text = _PASSWORD_RE.sub(r"\1<redacted>", text)
    text = _SK_TOKEN_RE.sub("<redacted>", text)
    return text


def redact_lines(lines: list[str]) -> list[str]:
    return [redact_line(line) for line in lines]


def load_state(mk04_env_token: str) -> tuple[EnvironmentStatePaths | None, str | None]:
    try:
        resolved = ConfigManager.load(
            environment=canonical_env(mk04_env_token),
            config_root=REPO_ROOT / "config",
        )
    except ConfigError as exc:
        return None, str(exc)[:200]
    return EnvironmentStatePaths.from_resolved_config(resolved), None


def is_allowed_log_path(path: Path, mk04_env_token: str, state: EnvironmentStatePaths) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if state.is_within_environment(resolved):
        return True
    env_marker = f"/mk04/{mk04_env_token}/"
    if env_marker in str(resolved):
        return True
    repo_logs = (REPO_ROOT / "logs" / mk04_env_token).resolve()
    try:
        if repo_logs == resolved or repo_logs in resolved.parents:
            return True
    except OSError:
        return False
    return False


def journalctl_available() -> bool:
    from shutil import which

    return which("journalctl") is not None and systemctl_available()


def fetch_journalctl_unit(
    unit: str,
    *,
    lines: int,
    since_today: bool = False,
) -> LogResult:
    if not journalctl_available():
        return LogResult(source=f"journalctl unit {unit}", lines=[], unavailable=True)

    args = ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "--output=short-iso"]
    if since_today:
        args = ["journalctl", "-u", unit, "--since", "today", "-n", str(lines), "--no-pager", "--output=short-iso"]

    result = run_command(args, timeout=30.0)
    if result is None:
        return LogResult(source=f"journalctl unit {unit}", lines=[], unavailable=True)

    stderr = result.stderr.strip()
    if systemd_not_running(stderr):
        return LogResult(source=f"journalctl unit {unit}", lines=[], unavailable=True)

    output_lines = [line for line in result.stdout.splitlines() if line.strip()]
    if result.returncode not in {0, 1} and not output_lines:
        detail = stderr or f"journalctl exit {result.returncode}"
        return LogResult(source=f"journalctl unit {unit}", lines=[detail], unavailable=True)

    if not output_lines or (len(output_lines) == 1 and "-- No entries --" in output_lines[0]):
        return LogResult(source=f"journalctl unit {unit}", lines=[], empty=True)

    return LogResult(source=f"journalctl unit {unit}", lines=output_lines[-lines:])


def fetch_scheduler_journal(*, lines: int, since_today: bool = False) -> LogResult:
    if not journalctl_available():
        return LogResult(source="journalctl scheduler entries", lines=[], unavailable=True)

    args = ["journalctl", "-n", str(max(lines * 3, 200)), "--no-pager", "--output=short-iso"]
    if since_today:
        args = ["journalctl", "--since", "today", "-n", str(max(lines * 3, 200)), "--no-pager", "--output=short-iso"]

    result = run_command(args, timeout=30.0)
    if result is None:
        return LogResult(source="journalctl scheduler entries", lines=[], unavailable=True)

    stderr = result.stderr.strip()
    if systemd_not_running(stderr):
        return LogResult(source="journalctl scheduler entries", lines=[], unavailable=True)

    matched: list[str] = []
    for line in result.stdout.splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in SCHEDULER_LOG_MARKERS):
            matched.append(line)
    matched = matched[-lines:]
    if not matched:
        return LogResult(source="journalctl scheduler entries", lines=[], empty=True)
    return LogResult(source="journalctl scheduler entries (mk04/cron-backed)", lines=matched)


def file_candidates_for_mode(mode: str, state: EnvironmentStatePaths, mk04_env_token: str) -> list[Path]:
    roots = [state.logs_root, state.data_root / "logs", state.reports_root]
    deploy_log_root = Path(f"/var/log/mk04/{mk04_env_token}")
    if deploy_log_root.is_dir():
        roots.append(deploy_log_root)

    mode_dirs = {
        "api": ["source-input", "input", "api"],
        "worker": ["video-automation", "worker"],
        "ai": ["ai-service", "ai", "ollama"],
        "output-funnel": ["output-funnel", "funnel"],
        "scheduler": ["watchdog", "scheduler", "cron"],
    }.get(mode, [])

    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        if not is_allowed_log_path(root, mk04_env_token, state):
            continue
        for sub in mode_dirs:
            subdir = root / sub
            if subdir.is_dir() and is_allowed_log_path(subdir, mk04_env_token, state):
                for path in list(subdir.glob("*")) + list(subdir.glob("*/*")):
                    if path.is_file() and path.suffix.lower() in LOG_EXTENSIONS:
                        if is_allowed_log_path(path, mk04_env_token, state):
                            resolved = path.resolve()
                            if resolved not in seen:
                                seen.add(resolved)
                                candidates.append(resolved)
        for path in root.glob("*.log"):
            if is_allowed_log_path(path, mk04_env_token, state):
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    candidates.append(resolved)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[:20]


# Cap bytes read from the end of a file so large logs cannot exhaust memory.
MAX_TAIL_BYTES = 256 * 1024


def read_tail_lines(path: Path, *, max_lines: int) -> list[str]:
    """Return the last ``max_lines`` lines without loading the whole file."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    try:
        with path.open("rb") as handle:
            if size > MAX_TAIL_BYTES:
                handle.seek(-MAX_TAIL_BYTES, 2)
                data = handle.read()
                newline = data.find(b"\n")
                if newline >= 0:
                    data = data[newline + 1 :]
            else:
                data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-max_lines:]


def fetch_file_logs(mode: str, state: EnvironmentStatePaths, mk04_env_token: str, *, lines: int) -> LogResult:
    candidates = file_candidates_for_mode(mode, state, mk04_env_token)
    if not candidates:
        return LogResult(source=f"file logs under {state.logs_root}", lines=[], unavailable=True)

    collected: list[str] = []
    source = str(candidates[0])
    for path in candidates[:5]:
        chunk = read_tail_lines(path, max_lines=lines)
        if chunk:
            prefix = f"[{path.name}]"
            collected.extend(f"{prefix} {line}" if not line.startswith("[") else line for line in chunk)
        if len(collected) >= lines:
            break
    collected = collected[-lines:]
    if not collected:
        return LogResult(source=source, lines=[], empty=True)
    return LogResult(source=source, lines=collected)


def fetch_service_logs(
    mode: str,
    state: EnvironmentStatePaths,
    mk04_env_token: str,
    *,
    lines: int,
    since_today: bool = False,
) -> LogResult:
    unit = LOG_MODE_UNITS.get(mode)
    if unit:
        journal = fetch_journalctl_unit(unit, lines=lines, since_today=since_today)
        if not journal.unavailable and (journal.lines or journal.empty):
            return journal
        file_result = fetch_file_logs(mode, state, mk04_env_token, lines=lines)
        if not file_result.unavailable:
            return file_result
        if journal.unavailable and file_result.unavailable:
            return LogResult(source=f"journalctl unit {unit}", lines=[], unavailable=True)
        return journal

    if mode == "scheduler":
        journal = fetch_scheduler_journal(lines=lines, since_today=since_today)
        if not journal.unavailable and (journal.lines or journal.empty):
            return journal
        file_result = fetch_file_logs("scheduler", state, mk04_env_token, lines=lines)
        if not file_result.unavailable:
            return file_result
        return LogResult(source="scheduler logs", lines=[], unavailable=True)

    return LogResult(source=f"mode {mode}", lines=[], unavailable=True)


def fetch_errors(state: EnvironmentStatePaths, mk04_env_token: str, *, lines: int) -> LogResult:
    collected: list[str] = []
    sources: list[str] = []

    if journalctl_available():
        for unit in LOG_MODE_UNITS.values():
            result = fetch_journalctl_unit(unit, lines=max(lines, 100))
            if result.unavailable or not result.lines:
                continue
            sources.append(result.source)
            for line in result.lines:
                if ERROR_LINE_RE.search(line):
                    collected.append(f"[{unit}] {line}")
            if len(collected) >= lines:
                break

    if len(collected) < lines:
        for mode in ("worker", "api", "ai", "output-funnel", "scheduler"):
            for path in file_candidates_for_mode(mode, state, mk04_env_token)[:10]:
                for line in read_tail_lines(path, max_lines=200):
                    if ERROR_LINE_RE.search(line):
                        collected.append(f"[{path.name}] {line}")
                    if len(collected) >= lines:
                        break
                if len(collected) >= lines:
                    break

    collected = collected[-lines:]
    source = ", ".join(sources[:3]) if sources else f"files under {state.logs_root}"
    if not collected:
        return LogResult(source=source, lines=[], empty=True)
    return LogResult(source=source, lines=collected)


def fetch_today(state: EnvironmentStatePaths, mk04_env_token: str, *, lines: int) -> LogResult:
    collected: list[str] = []
    sources: list[str] = []
    today = datetime.now(timezone.utc).date()

    for mode, unit in LOG_MODE_UNITS.items():
        result = fetch_journalctl_unit(unit, lines=lines, since_today=True)
        if not result.unavailable and result.lines:
            sources.append(result.source)
            collected.extend(f"[{mode}] {line}" for line in result.lines)
        if len(collected) >= lines:
            break

    sched = fetch_scheduler_journal(lines=lines, since_today=True)
    if not sched.unavailable and sched.lines:
        sources.append(sched.source)
        collected.extend(f"[scheduler] {line}" for line in sched.lines)

    if len(collected) < lines:
        for mode in LOG_MODE_UNITS:
            for path in file_candidates_for_mode(mode, state, mk04_env_token)[:10]:
                try:
                    if datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date() != today:
                        continue
                except OSError:
                    continue
                for line in read_tail_lines(path, max_lines=100):
                    collected.append(f"[{path.name}] {line}")
                    if len(collected) >= lines:
                        break

    collected = collected[-lines:]
    source = ", ".join(sources[:4]) if sources else f"logs under {state.logs_root}"
    if not collected:
        return LogResult(source=source, lines=[], empty=True)
    return LogResult(source=source, lines=collected)


def render_logs(
    *,
    env_label_text: str,
    mode: str,
    lines: int,
    result: LogResult,
) -> tuple[str, int]:
    header = [
        "Remote Operations Logs",
        "",
        f"Environment: {env_label_text}",
        f"Mode: {mode}",
        f"Source: {result.source}",
        f"Lines: {lines}",
        "",
    ]

    if result.unavailable:
        body = ["Log source not yet available."]
        return "\n".join(header + body), 1

    if result.empty or not result.lines:
        body = ["No logs found for this source."]
        return "\n".join(header + body), 0

    body = redact_lines(result.lines)
    return "\n".join(header + body), 0


def build_logs(
    mk04_env_token: str,
    mode: str,
    *,
    lines: int,
) -> tuple[str, int]:
    canonical = canonical_env(mk04_env_token)
    label = env_label(canonical)
    state, config_error = load_state(mk04_env_token)

    if state is None:
        result = LogResult(source="config paths", lines=[f"config load failed: {config_error}"], unavailable=True)
        return render_logs(env_label_text=label, mode=mode, lines=lines, result=result)

    if mode == "errors":
        result = fetch_errors(state, mk04_env_token, lines=lines)
        if result.empty:
            text, _ = render_logs(env_label_text=label, mode=mode, lines=lines, result=result)
            return text.replace("No logs found for this source.", "No recent error lines found."), 0
        return render_logs(env_label_text=label, mode=mode, lines=lines, result=result)

    if mode == "today":
        result = fetch_today(state, mk04_env_token, lines=lines)
        return render_logs(env_label_text=label, mode=mode, lines=lines, result=result)

    result = fetch_service_logs(mode, state, mk04_env_token, lines=lines)
    return render_logs(env_label_text=label, mode=mode, lines=lines, result=result)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only logs collector for scripts/ops/logs.sh")
    parser.add_argument("environment", help="dev or prod")
    parser.add_argument("mode", help="api|worker|ai|scheduler|errors|today|output-funnel")
    parser.add_argument(
        "--lines",
        type=int,
        default=DEFAULT_LOG_LINES,
        help=f"Number of lines (default {DEFAULT_LOG_LINES}, max {MAX_LOG_LINES})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if not argv:
        argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help"}:
        print(
            "Usage: logs_report.py <dev|prod> <mode> [--lines N]\n"
            "Modes: api, worker, ai, scheduler, errors, today, output-funnel\n"
            "Exit codes: 0=success/empty, 1=usage or unavailable source"
        )
        return 0 if argv and argv[0] in {"-h", "--help"} else 1

    try:
        args = parse_args(argv)
        canonical_env(args.environment)
    except (ValueError, SystemExit) as exc:
        if isinstance(exc, SystemExit):
            raise
        print(str(exc), file=sys.stderr)
        return 1

    mode = args.mode.strip().lower()
    if mode not in LOG_MODES:
        print(f"Invalid mode: {mode!r}. Expected one of: {', '.join(sorted(LOG_MODES))}", file=sys.stderr)
        return 1

    lines = clamp_lines(args.lines)
    if args.lines > MAX_LOG_LINES:
        print(f"Note: line count clamped to {MAX_LOG_LINES}", file=sys.stderr)

    mk04_env_token = mk04_env(canonical_env(args.environment))
    text, code = build_logs(mk04_env_token, mode, lines=lines)
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
