"""Read-only status probes for the local ai-service.

These helpers are deliberately defensive: the Ops UI must never crash or hang
because the local model backend is slow or absent. ``/health`` is cheap and is
safe to call on every settings page render. ``/diagnostics/model`` runs a real
(small) generation and is only called when the operator clicks the test button.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib import error, request


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_json(url: str, timeout: float) -> tuple[bool, dict[str, Any], int | None]:
    req = request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return True, data if isinstance(data, dict) else {"value": data}, resp.status
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"error": raw}
        return False, data if isinstance(data, dict) else {"value": data}, exc.code
    except Exception as exc:  # connection refused, timeout, DNS, etc.
        return False, {"error": str(exc)}, None


def ai_health(base_url: str, timeout: float) -> dict[str, Any]:
    """Cheap status. Never raises. ``reachable`` is False if the call failed."""
    url = base_url.rstrip("/") + "/health"
    ok, body, status_code = _get_json(url, timeout)
    if not ok:
        return {
            "reachable": False,
            "status": "unreachable",
            "status_code": status_code,
            "provider": None,
            "model_configured": None,
            "backend_reachable": False,
            "model_available": False,
            "error": str(body.get("error") or f"ai-service /health failed (HTTP {status_code})."),
            "checked_at": _now_iso(),
        }
    return {
        "reachable": True,
        "status": str(body.get("status") or "unknown"),
        "status_code": status_code,
        "provider": body.get("provider"),
        "model_configured": body.get("model_configured"),
        "backend_reachable": bool(body.get("backend_reachable")),
        "model_available": bool(body.get("model_available")),
        "error": body.get("error"),
        "checked_at": _now_iso(),
    }


def ai_diagnostics(base_url: str, timeout: float) -> dict[str, Any]:
    """Run a real model generation test. Only call on explicit operator action."""
    url = base_url.rstrip("/") + "/diagnostics/model"
    ok, body, status_code = _get_json(url, timeout)
    if not ok and status_code is None:
        return {
            "ok": False,
            "status": "unreachable",
            "status_code": None,
            "model_used": None,
            "response_text": None,
            "error": str(body.get("error") or "ai-service is unreachable."),
            "checked_at": _now_iso(),
        }
    error_obj = body.get("error")
    if isinstance(error_obj, dict):
        error_message = str(error_obj.get("message") or error_obj.get("code") or "")
    else:
        error_message = str(error_obj) if error_obj else ""
    return {
        "ok": ok and str(body.get("status")) == "ok",
        "status": str(body.get("status") or "unknown"),
        "status_code": status_code,
        "model_used": body.get("model_used"),
        "response_text": body.get("response_text"),
        "error": error_message or None,
        "checked_at": _now_iso(),
    }
