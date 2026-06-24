"""Re-POST output-funnel registrations for clipping jobs whose handoff failed.

``_try_output_funnel_handoff`` in ``server/app.py`` is one-shot: when the
clipping pipeline finishes a job, it POSTs the report to output-funnel once
and records ``output_funnel_handoff`` in ``report.json``. If that POST fails
(network blip, output-funnel restarting, timeout), the clip is otherwise
orphaned — output-funnel never learns it exists.

This sweeper is the safety net. It walks ``jobs/<...>/report.json``, finds
successful jobs whose handoff did not succeed, and re-POSTs them. It is
idempotent: output-funnel ``register_from_payload`` dedupes by clip durable
id, so re-posting an already-registered job is a no-op there.

Designed to be run from cron every 5–15 minutes. Self-contained (stdlib only)
so cron does not need the video-automation venv on PATH.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_MAX_AGE_HOURS = 24
DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_LIMIT = 25


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=".report.",
        suffix=".tmp",
        delete=False,
    ) as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        tmp_path = f.name
    os.replace(tmp_path, str(path))


def _resolve_jobs_dir(repo_root: Path) -> Path:
    """Find the configured ``jobs/`` directory, falling back to the repo default."""
    env = os.environ.get("VIDEO_AUTOMATION_JOBS_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    cfg_path_env = os.environ.get("PIPELINE_CONFIG_PATH", "").strip()
    cfg_path = (
        Path(cfg_path_env).expanduser()
        if cfg_path_env
        else repo_root / "video-automation" / "config" / "pipeline_config.json"
    )
    cfg = _read_json(cfg_path) or {}
    paths = cfg.get("paths") if isinstance(cfg.get("paths"), dict) else {}
    raw = str(paths.get("jobs_folder") or "jobs")
    jobs_dir = Path(raw)
    if not jobs_dir.is_absolute():
        jobs_dir = (repo_root / "video-automation" / jobs_dir).resolve()
    return jobs_dir


def _is_handoff_ok(handoff: Any) -> bool:
    return isinstance(handoff, dict) and handoff.get("ok") is True


def _attempts(handoff: Any) -> int:
    if isinstance(handoff, dict):
        retries = handoff.get("retries")
        if isinstance(retries, list):
            return len(retries)
    return 0


def _is_recent(report_path: Path, *, max_age_hours: float) -> bool:
    try:
        mtime = report_path.stat().st_mtime
    except OSError:
        return False
    age_sec = time.time() - mtime
    return age_sec <= max_age_hours * 3600


def _eligible(report: dict[str, Any], *, max_attempts: int) -> tuple[bool, str]:
    if str(report.get("status") or "") != "success":
        return False, "not_success"
    handoff = report.get("output_funnel_handoff")
    if handoff is None:
        # Job finished without ever attempting handoff (handoff was disabled
        # at the time, or pre-handoff code path). Treat as eligible.
        return True, "no_prior_attempt"
    if _is_handoff_ok(handoff):
        return False, "already_ok"
    if isinstance(handoff, dict) and handoff.get("enabled") is False:
        return False, "handoff_disabled_in_record"
    if _attempts(handoff) >= max_attempts:
        return False, "max_attempts_reached"
    return True, "retryable"


def _post_handoff(
    *,
    url: str,
    report: dict[str, Any],
    report_path: Path,
    secret: str | None,
    timeout: float,
) -> dict[str, Any]:
    body = json.dumps(
        {"report_path": str(report_path.resolve()), "payload": report}
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Output-Funnel-Secret"] = secret
    req = urllib.request.Request(
        f"{url.rstrip('/')}/registrations/from-job",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text or "{}")
            except json.JSONDecodeError:
                parsed = {"raw_response": text[:500]}
            ok = 200 <= int(resp.status) < 300 and parsed.get("success") is not False
            return {
                "ok": bool(ok),
                "status_code": int(resp.status),
                "response": parsed,
            }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status_code": int(exc.code), "error": repr(exc)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def _record_attempt(
    report: dict[str, Any],
    *,
    url: str,
    attempt_result: dict[str, Any],
) -> dict[str, Any]:
    previous = report.get("output_funnel_handoff")
    if not isinstance(previous, dict):
        previous = {"enabled": True}
    retries = previous.get("retries") if isinstance(previous.get("retries"), list) else []
    retries.append(
        {
            "by": "handoff_sweeper",
            "at": _now_iso(),
            "ok": bool(attempt_result.get("ok")),
            "status_code": attempt_result.get("status_code"),
            "error": attempt_result.get("error"),
        }
    )
    new_record = {
        "enabled": True,
        "ok": bool(attempt_result.get("ok")),
        "url": url,
        "status_code": attempt_result.get("status_code"),
        "response": attempt_result.get("response"),
        "error": attempt_result.get("error"),
        "at": _now_iso(),
        "retries": retries,
    }
    return new_record


def sweep(
    *,
    jobs_dir: Path,
    url: str,
    secret: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
    post_fn: Any = None,
) -> dict[str, Any]:
    """Walk jobs_dir and retry handoffs. Returns a structured summary."""
    do_post = post_fn or _post_handoff
    summary: dict[str, Any] = {
        "scanned": 0,
        "eligible": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": [],
        "results": [],
    }
    if not jobs_dir.is_dir():
        summary["error"] = f"jobs_dir_not_found: {jobs_dir}"
        return summary

    candidates = sorted(jobs_dir.glob("*/report.json"))
    for report_path in candidates:
        if summary["eligible"] >= limit:
            break
        summary["scanned"] += 1
        if not _is_recent(report_path, max_age_hours=max_age_hours):
            summary["skipped"].append({"path": str(report_path), "reason": "too_old"})
            continue
        report = _read_json(report_path)
        if report is None:
            summary["skipped"].append({"path": str(report_path), "reason": "unreadable"})
            continue
        ok, reason = _eligible(report, max_attempts=max_attempts)
        if not ok:
            summary["skipped"].append({"path": str(report_path), "reason": reason})
            continue
        summary["eligible"] += 1
        if dry_run:
            summary["results"].append(
                {"path": str(report_path), "would_retry": True, "reason": reason}
            )
            continue
        attempt = do_post(
            url=url,
            report=report,
            report_path=report_path,
            secret=secret,
            timeout=timeout,
        )
        report["output_funnel_handoff"] = _record_attempt(
            report, url=url, attempt_result=attempt
        )
        try:
            _atomic_write_json(report_path, report)
        except OSError as exc:
            summary["failed"] += 1
            summary["results"].append(
                {
                    "path": str(report_path),
                    "ok": False,
                    "write_error": repr(exc),
                    "attempt": attempt,
                }
            )
            continue
        if attempt.get("ok"):
            summary["succeeded"] += 1
        else:
            summary["failed"] += 1
        summary["results"].append(
            {
                "path": str(report_path),
                "ok": bool(attempt.get("ok")),
                "attempt": attempt,
            }
        )
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jobs-dir",
        type=Path,
        default=None,
        help="Override video-automation jobs/ directory. Defaults to config or repo path.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("OUTPUT_FUNNEL_URL", "http://127.0.0.1:5055"),
        help="output-funnel base URL.",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("OUTPUT_FUNNEL_SECRET", ""),
        help="output-funnel shared secret if configured.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help="Max retries recorded on a single report.json before skipping.",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE_HOURS,
        help="Skip reports older than this. Avoids hammering ancient failures.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SEC,
        help="Per-request HTTP timeout (seconds).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Max number of retries per sweep run.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress JSON summary on stdout."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    jobs_dir = args.jobs_dir or _resolve_jobs_dir(repo_root)
    summary = sweep(
        jobs_dir=jobs_dir,
        url=args.url,
        secret=args.secret or None,
        max_attempts=args.max_attempts,
        max_age_hours=args.max_age_hours,
        timeout=args.timeout,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    if not args.quiet:
        json.dump(summary, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0 if summary.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
