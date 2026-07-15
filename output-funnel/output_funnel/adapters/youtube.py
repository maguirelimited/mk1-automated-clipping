from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from output_funnel.models import FailureClass, PublishResult, PublishState, UploadStatus
from output_funnel.preflight import preferred_media_path
from output_funnel.time_utils import parse_iso_datetime

from .base import PlatformAdapter

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class YouTubeAdapter(PlatformAdapter):
    platform = "youtube_shorts"
    adapter_version = "1"
    api_version = "youtube.v3"

    def __init__(self, youtube_service: Any | None = None):
        self._youtube_service = youtube_service

    def publish(self, upload_job: dict[str, Any], source_clip: dict[str, Any], profile: dict[str, Any]) -> PublishResult:
        validation_error = self._validate(upload_job, source_clip, profile)
        if validation_error:
            return PublishResult(
                ok=False,
                status=UploadStatus.FAILED_TERMINAL,
                failure_class=FailureClass.PERMANENT_FAILURE,
                error_category="youtube_validation_error",
                error_message=validation_error,
                publish_state=PublishState.FAILED,
                platform_state="failed",
                adapter_version=self.adapter_version,
                api_version=self.api_version,
                retryable=False,
            )

        media_path = preferred_media_path(source_clip)
        assert media_path is not None
        body = self._build_body(upload_job, profile)

        try:
            youtube = self._youtube_service or self._build_service(profile)
            insert_kwargs: dict[str, Any] = {
                "part": "snippet,status",
                "body": body,
            }
            if self._youtube_service is None:
                from googleapiclient.http import MediaFileUpload

                insert_kwargs["media_body"] = MediaFileUpload(
                    media_path,
                    mimetype="video/mp4",
                    resumable=True,
                )
            request = youtube.videos().insert(**insert_kwargs)
            response = request.execute()
        except Exception as exc:
            return PublishResult(
                ok=False,
                status=UploadStatus.FAILED_RETRYABLE,
                failure_class=FailureClass.RETRYABLE,
                error_category="youtube_upload_error",
                error_message=str(exc),
                publish_state=PublishState.FAILED,
                platform_state="failed",
                adapter_version=self.adapter_version,
                api_version=self.api_version,
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
                raw_response=response,
                failure_class=FailureClass.RETRYABLE,
                error_category="youtube_response_missing_id",
                error_message="YouTube response did not include a video id",
                publish_state=PublishState.FAILED,
                platform_state="failed",
                adapter_version=self.adapter_version,
                api_version=self.api_version,
                retryable=True,
            )
        state_error = _verify_scheduled_state(response, body)
        if state_error:
            return PublishResult(
                ok=False,
                status=UploadStatus.FAILED_TERMINAL,
                platform_asset_id=video_id,
                response=_safe_response(response),
                raw_response=response,
                remote_ids={"youtube_video_id": video_id},
                failure_class=FailureClass.PERMANENT_FAILURE,
                error_category="youtube_state_verification_failed",
                error_message=state_error,
                publish_state=PublishState.FAILED,
                platform_state="failed",
                adapter_version=self.adapter_version,
                api_version=self.api_version,
                retryable=False,
            )
        return PublishResult(
            ok=True,
            status=UploadStatus.SCHEDULED_ON_PLATFORM,
            platform_asset_id=video_id,
            scheduled_at=str(upload_job.get("platform_publish_at") or upload_job.get("scheduled_at") or ""),
            response=_safe_response(response),
            raw_response=response,
            remote_ids={"youtube_video_id": video_id},
            publish_state=PublishState.SCHEDULED,
            platform_state="private_scheduled",
            adapter_version=self.adapter_version,
            api_version=self.api_version,
            retryable=False,
        )

    def _validate(self, upload_job: dict[str, Any], source_clip: dict[str, Any], profile: dict[str, Any]) -> str | None:
        if str(upload_job.get("platform") or "") != self.platform:
            return "upload_job_platform_mismatch"
        media_path = preferred_media_path(source_clip)
        if not media_path or not os.path.isfile(media_path) or os.path.getsize(media_path) <= 0:
            return "media_file_unavailable"
        publish_at = parse_iso_datetime(
            str(upload_job.get("platform_publish_at") or upload_job.get("publish_at") or "")
        )
        if publish_at is None:
            return "missing_or_invalid_publish_at"
        min_lead_minutes = _min_publish_at_lead_minutes()
        if publish_at <= datetime.now(UTC) + timedelta(minutes=min_lead_minutes):
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
        publish_at = parse_iso_datetime(
            str(upload_job.get("platform_publish_at") or upload_job.get("publish_at") or "")
        )
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


def _min_publish_at_lead_minutes() -> int:
    raw = os.environ.get("OUTPUT_FUNNEL_YT_MIN_PUBLISH_LEAD_MINUTES", "").strip()
    if raw:
        try:
            return max(15, int(raw))
        except ValueError:
            pass
    try:
        from output_funnel.config import load_settings

        cfg = load_settings()
        yt = cfg.get("youtube") if isinstance(cfg.get("youtube"), dict) else {}
        value = yt.get("min_publish_at_lead_minutes")
        if value is not None:
            return max(15, int(value))
    except Exception:
        pass
    return 15


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


def _verify_scheduled_state(response: dict[str, Any], expected_body: dict[str, Any]) -> str | None:
    status = response.get("status")
    if not isinstance(status, dict):
        return "youtube_response_missing_status"
    expected_status = expected_body.get("status") if isinstance(expected_body.get("status"), dict) else {}
    expected_privacy = str(expected_status.get("privacyStatus") or "").strip()
    actual_privacy = str(status.get("privacyStatus") or "").strip()
    if expected_privacy and actual_privacy != expected_privacy:
        return f"privacy_status_mismatch: expected {expected_privacy!r}, got {actual_privacy!r}"

    expected_publish_at = str(expected_status.get("publishAt") or "").strip()
    actual_publish_at = str(status.get("publishAt") or "").strip()
    if expected_publish_at and not _same_instant(expected_publish_at, actual_publish_at):
        return f"publish_at_mismatch: expected {expected_publish_at!r}, got {actual_publish_at!r}"
    return None


def _same_instant(expected: str, actual: str) -> bool:
    expected_dt = parse_iso_datetime(expected)
    actual_dt = parse_iso_datetime(actual)
    if expected_dt is None or actual_dt is None:
        return expected == actual
    return expected_dt == actual_dt
