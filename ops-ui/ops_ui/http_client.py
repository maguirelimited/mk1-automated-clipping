from __future__ import annotations

import json
import os
from typing import Any
from urllib import error, request

from .config import ServiceConfig


def _headers(service: ServiceConfig) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if service.secret_env and service.secret_header:
        secret = os.environ.get(service.secret_env, "").strip()
        if secret:
            headers[service.secret_header] = secret
    return headers


def call_json(
    service: ServiceConfig,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 2.5,
) -> tuple[bool, dict[str, Any], int | None]:
    url = service.base_url.rstrip("/") + "/" + path.lstrip("/")
    headers = _headers(service)
    body: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, headers=headers, method=method)
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
        if not isinstance(data, dict):
            data = {"value": data}
        return False, data, exc.code
    except Exception as exc:
        return False, {"error": str(exc)}, None

