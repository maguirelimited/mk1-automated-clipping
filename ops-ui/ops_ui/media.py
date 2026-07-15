"""Clip preview streaming helpers for the ops UI."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from flask import Response, send_file

from .config import Settings, ServiceConfig

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from observability.outputs import (  # noqa: E402
    report_clip_filename,
    resolve_clip_media_by_filename,
    resolve_clip_media_path,
)


def service_by_key(settings: Settings, key: str) -> ServiceConfig | None:
    return next((svc for svc in settings.services if svc.key == key), None)


def _video_service_headers(service: ServiceConfig) -> dict[str, str]:
    headers = {"Accept": "video/*,*/*"}
    if service.secret_env and service.secret_header:
        secret = os.environ.get(service.secret_env, "").strip()
        if secret:
            headers[service.secret_header] = secret
    return headers


def proxy_video_automation_clip(
    settings: Settings,
    clip_file: str,
    *,
    download: bool = False,
    timeout_sec: float | None = None,
) -> Response:
    from urllib import error, request as urlrequest

    svc = service_by_key(settings, "video-automation")
    if svc is None:
        return Response("video-automation not configured", status=503)

    safe_name = os.path.basename(str(clip_file or ""))
    if not safe_name:
        return Response("invalid clip file", status=400)

    url = svc.base_url.rstrip("/") + f"/output/{safe_name}"
    req = urlrequest.Request(
        url,
        headers=_video_service_headers(svc),
        method="GET",
    )
    timeout = timeout_sec if timeout_sec is not None else max(settings.service_timeout_sec, 15.0)
    try:
        upstream = urlrequest.urlopen(req, timeout=timeout)
    except error.HTTPError as exc:
        return Response(
            exc.read(),
            status=exc.code,
            content_type=exc.headers.get_content_type(),
        )
    except Exception as exc:
        return Response(str(exc), status=502)

    response = Response(
        upstream.read(),
        mimetype=upstream.headers.get("Content-Type") or "video/mp4",
    )
    upstream.close()
    if download:
        response.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return response


def stream_local_clip(path: Path, *, download: bool = False) -> Response:
    response = send_file(path, conditional=True)
    if download:
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{path.name}"'
        )
    return response


def stream_output_clip(
    settings: Settings,
    *,
    env_token: str,
    job_id: str,
    clip_id: str,
    download: bool = False,
) -> Response:
    local = resolve_clip_media_path(env_token, job_id, clip_id)
    if local is not None:
        return stream_local_clip(local, download=download)

    clip_file = report_clip_filename(env_token, job_id, clip_id)
    if clip_file:
        return proxy_video_automation_clip(
            settings,
            clip_file,
            download=download,
        )
    return Response("clip media not found", status=404)


def stream_clip_review_media(
    settings: Settings,
    *,
    env_token: str,
    job_id: str,
    clip_file: str,
    download: bool = False,
) -> Response:
    local = resolve_clip_media_by_filename(env_token, job_id, clip_file)
    if local is not None:
        return stream_local_clip(local, download=download)
    return proxy_video_automation_clip(
        settings,
        clip_file,
        download=download,
    )
