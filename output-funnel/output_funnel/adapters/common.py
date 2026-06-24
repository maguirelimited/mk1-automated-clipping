from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from output_funnel.models import FailureClass
from output_funnel.preflight import preferred_media_path
from output_funnel.time_utils import now_iso, parse_iso_datetime


@dataclass(frozen=True)
class HttpResult:
    status_code: int
    body: dict[str, Any]
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass(frozen=True)
class TokenHealth:
    ok: bool
    failure_class: str | None = None
    error_message: str | None = None
    warnings: list[str] = field(default_factory=list)
    expires_at: str | None = None
    token_source: str | None = None


class RequestsHttpClient:
    def __init__(self) -> None:
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise RuntimeError("Social adapters require the `requests` package.") from exc
        self._requests = requests

    def post(self, url: str, **kwargs: Any) -> HttpResult:
        return _to_http_result(self._requests.post(url, **kwargs))

    def get(self, url: str, **kwargs: Any) -> HttpResult:
        return _to_http_result(self._requests.get(url, **kwargs))


def _to_http_result(response: Any) -> HttpResult:
    try:
        body = response.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {"value": body}
    return HttpResult(
        status_code=int(getattr(response, "status_code", 0) or 0),
        body=body,
        text=str(getattr(response, "text", "") or ""),
        headers=dict(getattr(response, "headers", {}) or {}),
    )


def http_client_or_default(client: Any | None) -> Any:
    return client if client is not None else RequestsHttpClient()


def credential_env(profile: dict[str, Any], key: str) -> str:
    credentials = profile.get("credentials") if isinstance(profile.get("credentials"), dict) else {}
    return str(credentials.get(key) or "").strip()


def load_access_token(profile: dict[str, Any], *, env_key: str = "access_token_env") -> str:
    env_name = credential_env(profile, env_key)
    token = os.environ.get(env_name, "").strip() if env_name else ""
    if not token:
        raise RuntimeError(f"missing_access_token:{env_name or env_key}")
    return token


def token_health(profile: dict[str, Any]) -> TokenHealth:
    expires_env = credential_env(profile, "token_expires_at_env")
    expires_at = os.environ.get(expires_env, "").strip() if expires_env else ""
    source = credential_env(profile, "access_token_env") or credential_env(profile, "token_file_env")
    if not expires_at:
        return TokenHealth(ok=True, token_source=source or None)
    expires = parse_iso_datetime(expires_at)
    if expires is None:
        return TokenHealth(
            ok=False,
            failure_class=FailureClass.AUTHENTICATION_FAILURE,
            error_message="invalid_token_expires_at",
            expires_at=expires_at,
            token_source=source or None,
        )
    now = parse_iso_datetime(now_iso())
    if now is not None and expires <= now:
        return TokenHealth(
            ok=False,
            failure_class=FailureClass.AUTHENTICATION_FAILURE,
            error_message="token_expired",
            expires_at=expires_at,
            token_source=source or None,
        )
    warnings: list[str] = []
    if now is not None:
        seconds = (expires - now).total_seconds()
        if seconds <= 24 * 3600:
            warnings.append("token_expires_within_24h")
        elif seconds <= 7 * 24 * 3600:
            warnings.append("token_expires_within_7d")
        elif seconds <= 30 * 24 * 3600:
            warnings.append("token_expires_within_30d")
    return TokenHealth(ok=True, warnings=warnings, expires_at=expires_at, token_source=source or None)


def classify_http_failure(result: HttpResult) -> tuple[str, bool, str | None]:
    if result.status_code in {401, 403}:
        return FailureClass.AUTHENTICATION_FAILURE, False, "authentication_failure"
    if result.status_code == 429:
        return FailureClass.RATE_LIMITED, True, "rate_limited"
    if 500 <= result.status_code < 600:
        return FailureClass.RETRYABLE, True, "platform_5xx"
    return FailureClass.PERMANENT_FAILURE, False, "platform_4xx"


def retry_after_seconds(result: HttpResult) -> int | None:
    raw = result.headers.get("retry-after") or result.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return None


def safe_response(value: dict[str, Any], *, keep: tuple[str, ...]) -> dict[str, Any]:
    return {key: value.get(key) for key in keep if key in value}


def media_file(upload_job: dict[str, Any], source_clip: dict[str, Any]) -> tuple[str | None, str | None]:
    path = preferred_media_path(upload_job) or preferred_media_path(source_clip)
    if not path:
        return None, "media_file_unavailable"
    media_path = Path(path)
    if not media_path.is_file() or media_path.stat().st_size <= 0:
        return None, "media_file_unavailable"
    return str(media_path), None


def media_fingerprint(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    try:
        stat = p.stat()
    except OSError:
        return path
    return sha256(f"{p.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")).hexdigest()


def operation_key(upload_job: dict[str, Any], media_path: str | None = None) -> str:
    parts = [
        upload_job.get("id"),
        upload_job.get("publication_id"),
        upload_job.get("platform"),
        upload_job.get("channel_id"),
        upload_job.get("platform_publish_at") or upload_job.get("publish_at"),
        media_fingerprint(media_path),
    ]
    return sha256("|".join("" if part is None else str(part) for part in parts).encode("utf-8")).hexdigest()


def post_json(http: Any, url: str, *, token: str, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> HttpResult:
    return http.post(url, headers={"Authorization": f"Bearer {token}"}, json=json, params=params)


def poll_until(
    *,
    poll_fn: Any,
    done_fn: Any,
    sleep_seconds: float = 5.0,
    max_attempts: int = 30,
) -> HttpResult:
    last: HttpResult | None = None
    for index in range(max(1, int(max_attempts))):
        last = poll_fn()
        if done_fn(last):
            return last
        if index < max_attempts - 1:
            time.sleep(max(0.0, sleep_seconds))
    return last if last is not None else HttpResult(status_code=0, body={})
