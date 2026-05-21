from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from output_funnel.models import PublishResult, UploadStatus
from output_funnel.preflight import preferred_media_path
from output_funnel.time_utils import parse_iso_datetime

from .base import PlatformAdapter

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class YouTubeAdapter(PlatformAdapter):
    platform = "youtube_shorts"

    def __init__(self, youtube_service: Any | None = None):
        self._youtube_service = youtube_service

    def publish(self, upload_job: dict[str, Any], source_clip: dict[str, Any], profile: dict[str, Any]) -> PublishResult:
        validation_error = self._validate(upload_job, source_clip, profile)
        if validation_error:
            return PublishResult(
                ok=False,
                status=UploadStatus.FAILED_TERMINAL,
                error_category="youtube_validation_error",
                error_message=validation_error,
                retryable=False,
            )

        media_path = preferred_media_path(source_clip)
        assert media_path is not None
        body = self._build_body(upload_job, profile)

        try:
            youtube = self._youtube_service or self._build_service(profile)
            from googleapiclient.http import MediaFileUpload

            media = MediaFileUpload(media_path, mimetype="video/mp4", resumable=True)
            request = youtube.videos().insert(
                part="snippet,status",
                body=body,
                media_body=media,
            )
            response = request.execute()
        except Exception as exc:
            return PublishResult(
                ok=False,
                status=UploadStatus.FAILED_RETRYABLE,
                error_category="youtube_upload_error",
                error_message=str(exc),
                retryable=True,
            )

        if not isinstance(response, dict):
            response = {}
        video_id = str(response.get("id") or "").strip() or None
        if not video_id:
            return PublishResult(
                ok=False,
                status=UploadStatus.FAILED_RETRYABLE,
                response=_safe_response(response),
                error_category="youtube_response_missing_id",
                error_message="YouTube response did not include a video id",
                retryable=True,
            )
        return PublishResult(
            ok=True,
            status=UploadStatus.SCHEDULED_ON_PLATFORM,
            platform_asset_id=video_id,
            scheduled_at=str(upload_job.get("platform_publish_at") or upload_job.get("scheduled_at") or ""),
            response=_safe_response(response),
            retryable=False,
        )

    def _validate(self, upload_job: dict[str, Any], source_clip: dict[str, Any], profile: dict[str, Any]) -> str | None:
        if str(upload_job.get("platform") or "") != self.platform:
            return "upload_job_platform_mismatch"
        media_path = preferred_media_path(source_clip)
        if not media_path or not os.path.isfile(media_path) or os.path.getsize(media_path) <= 0:
            return "media_file_unavailable"
        publish_at = parse_iso_datetime(str(upload_job.get("platform_publish_at") or ""))
        if publish_at is None:
            return "missing_or_invalid_publish_at"
        if publish_at <= datetime.now(UTC) + timedelta(minutes=15):
            return "publish_at_not_safely_future"
        style = profile.get("metadata_style") if isinstance(profile.get("metadata_style"), dict) else {}
        if str(style.get("privacy_status") or "private") != "private":
            return "youtube_scheduled_upload_requires_private_privacy"
        if not str(upload_job.get("normalized_title") or "").strip():
            return "missing_normalized_title"
        if not str(upload_job.get("normalized_description") or "").strip():
            return "missing_normalized_description"
        return None

    def _build_body(self, upload_job: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        style = profile.get("metadata_style") if isinstance(profile.get("metadata_style"), dict) else {}
        publish_at = parse_iso_datetime(str(upload_job.get("platform_publish_at") or ""))
        return {
            "snippet": {
                "title": str(upload_job.get("normalized_title") or ""),
                "description": str(upload_job.get("normalized_description") or ""),
                "categoryId": str(style.get("category_id") or "22"),
                "defaultLanguage": str(style.get("language") or "en"),
            },
            "status": {
                "privacyStatus": "private",
                "publishAt": publish_at.isoformat().replace("+00:00", "Z") if publish_at else None,
                "selfDeclaredMadeForKids": bool(style.get("made_for_kids", False)),
            },
        }

    def _build_service(self, profile: dict[str, Any]) -> Any:
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "YouTube publishing requires google-api-python-client, google-auth, "
                "and google-auth-oauthlib to be installed."
            ) from exc
        credentials_cfg = profile.get("credentials") if isinstance(profile.get("credentials"), dict) else {}
        token_file_env = str(credentials_cfg.get("token_file_env") or "").strip()
        token_file = os.environ.get(token_file_env, "").strip() if token_file_env else ""
        if not token_file:
            raise RuntimeError("YouTube token file env var is not configured")
        credentials = Credentials.from_authorized_user_file(token_file, scopes=[YOUTUBE_UPLOAD_SCOPE])
        return build("youtube", "v3", credentials=credentials)


def _safe_response(response: dict[str, Any]) -> dict[str, Any]:
    snippet = response.get("snippet") if isinstance(response.get("snippet"), dict) else {}
    status = response.get("status") if isinstance(response.get("status"), dict) else {}
    return {
        "id": response.get("id"),
        "snippet": {
            "title": snippet.get("title"),
            "publishedAt": snippet.get("publishedAt"),
        },
        "status": {
            "uploadStatus": status.get("uploadStatus"),
            "privacyStatus": status.get("privacyStatus"),
            "publishAt": status.get("publishAt"),
        },
    }
