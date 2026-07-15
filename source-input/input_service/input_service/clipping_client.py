"""Enqueue clipping jobs on the video-automation service after input is ready."""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from .log_util import detail

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 30


def auto_enqueue_enabled() -> bool:
    raw = os.environ.get("CLIPPING_AUTO_ENQUEUE", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def video_automation_base_url() -> str:
    override = os.environ.get("VIDEO_AUTOMATION_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    host = os.environ.get("VIDEO_AUTOMATION_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = os.environ.get("VIDEO_AUTOMATION_PORT", "5050").strip() or "5050"
    return f"http://{host}:{port}"


def enqueue_clipping_job(
    *,
    input_id: str,
    funnel_id: str,
    pipeline_profile: str,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    orchestration_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST ``/jobs`` on video-automation. Returns a normalized result dict."""
    if not auto_enqueue_enabled():
        return {
            "success": False,
            "skipped": True,
            "error": "clipping_auto_enqueue_disabled",
        }

    clean_input_id = str(input_id or "").strip()
    if not clean_input_id:
        return {"success": False, "error": "missing_input_id"}

    base = video_automation_base_url()
    url = f"{base}/jobs"
    body: dict[str, Any] = {
        "input_id": clean_input_id,
        "funnel_id": str(funnel_id or "").strip(),
        "pipeline_profile": str(pipeline_profile or "").strip(),
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if orchestration_context:
        run_id = str(orchestration_context.get("run_id") or "").strip()
        environment = str(orchestration_context.get("environment") or "").strip()
        body["orchestration_context"] = {
            "run_id": run_id,
            "environment": environment or (os.environ.get("MK04_ENV") or "dev").strip(),
            "trigger": str(orchestration_context.get("trigger") or "source_input"),
        }
        if run_id:
            headers["X-MK04-Run-Id"] = run_id
        if environment:
            headers["X-MK04-Environment"] = environment
    payload = json.dumps(body).encode("utf-8")
    secret = os.environ.get("VIDEO_AUTOMATION_SECRET", "").strip()
    if secret:
        headers["X-Video-Automation-Secret"] = secret

    req = urlrequest.Request(url, data=payload, headers=headers, method="POST")
    detail(
        log,
        "Enqueue clipping job: url=%s input_id=%s funnel_id=%s pipeline_profile=%s",
        url,
        clean_input_id,
        body["funnel_id"],
        body["pipeline_profile"],
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout_sec) as resp:
            status = int(resp.status)
            raw = resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
    except urlerror.URLError as exc:
        log.error("Clipping enqueue unreachable: %s", exc)
        return {"success": False, "error": f"clipping_unreachable: {exc}"}
    except Exception as exc:
        log.exception("Clipping enqueue failed")
        return {"success": False, "error": f"clipping_enqueue_exception: {exc}"}

    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}

    if status not in (200, 202):
        err = str(parsed.get("error") or parsed.get("message") or f"http_{status}")
        log.error("Clipping enqueue HTTP %s: %s", status, err[:500])
        return {
            "success": False,
            "http_status": status,
            "error": err,
            "response": parsed,
        }

    job_id = str(parsed.get("job_id") or "").strip()
    if not job_id:
        return {
            "success": False,
            "http_status": status,
            "error": "clipping_enqueue_missing_job_id",
            "response": parsed,
        }

    return {
        "success": True,
        "http_status": status,
        "job_id": job_id,
        "status": str(parsed.get("status") or "queued"),
        "status_url": parsed.get("status_url"),
        "outputs_url": parsed.get("outputs_url"),
        "clipping_base_url": base,
        "response": parsed,
    }


def probe_clipping_health(timeout_sec: float = 5.0) -> tuple[bool, str]:
    """GET ``/healthz`` on video-automation for doctor checks."""
    url = f"{video_automation_base_url()}/healthz"
    try:
        req = urlrequest.Request(url, method="GET")
        with urlrequest.urlopen(req, timeout=timeout_sec) as resp:
            ok = 200 <= int(resp.status) < 300
            return ok, f"HTTP {resp.status} {url}"
    except Exception as exc:
        return False, f"{url}: {exc}"
