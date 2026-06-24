from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Any

from output_funnel.adapters.common import (
    HttpResult,
    classify_http_failure,
    credential_env,
    http_client_or_default,
    load_access_token,
    media_file,
    operation_key,
    retry_after_seconds,
    safe_response,
)
from output_funnel.models import FailureClass, PublishResult, PublishState, UploadStatus
from output_funnel.time_utils import parse_iso_datetime

from .base import PlatformAdapter


class FacebookReelsAdapter(PlatformAdapter):
    platform = "facebook_reels"
    adapter_version = "1"
    api_version = "v25.0"

    def __init__(self, http_client: Any | None = None):
        self._http = http_client_or_default(http_client)

    def publish(self, upload_job: dict[str, Any], source_clip: dict[str, Any], profile: dict[str, Any]) -> PublishResult:
        validation_error = self._validate(upload_job, source_clip, profile)
        if validation_error:
            return _failure("facebook_validation_error", validation_error, FailureClass.PERMANENT_FAILURE, False)
        media_path, _ = media_file(upload_job, source_clip)
        assert media_path is not None
        token = load_access_token(profile)
        page_id = _page_id(profile)
        start = self._http.post(
            f"https://graph.facebook.com/{self.api_version}/{page_id}/video_reels",
            params={"access_token": token, "upload_phase": "start"},
        )
        if not start.ok:
            return _http_failure("facebook_reels_start_error", start)
        video_id = str(start.body.get("video_id") or "").strip()
        upload_url = str(start.body.get("upload_url") or "").strip()
        if not video_id or not upload_url:
            return _failure(
                "facebook_reels_start_missing_fields",
                "Facebook start response did not include video_id and upload_url",
                FailureClass.RETRYABLE,
                True,
                raw_response=start.body,
            )
        upload = self._upload_binary(token, upload_url, media_path)
        if not upload.ok:
            return _http_failure("facebook_reels_upload_error", upload, remote_ids={"facebook_video_id": video_id})
        publish_at = parse_iso_datetime(str(upload_job.get("platform_publish_at") or upload_job.get("publish_at") or ""))
        assert publish_at is not None
        finish = self._http.post(
            f"https://graph.facebook.com/{self.api_version}/{page_id}/video_reels",
            params={
                "access_token": token,
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "SCHEDULED",
                "scheduled_publish_time": str(int(publish_at.astimezone(UTC).timestamp())),
                "description": str(upload_job.get("normalized_description") or ""),
                "title": str(upload_job.get("normalized_title") or ""),
            },
        )
        if not finish.ok:
            return _http_failure("facebook_reels_finish_error", finish, remote_ids={"facebook_video_id": video_id})
        post_id = str(finish.body.get("post_id") or finish.body.get("id") or video_id).strip()
        return PublishResult(
            ok=True,
            status=UploadStatus.UPLOADED_SCHEDULED,
            platform_asset_id=post_id,
            scheduled_at=str(upload_job.get("platform_publish_at") or upload_job.get("publish_at") or ""),
            response={"post_id": post_id, "video_id": video_id, "scheduled": True},
            raw_response={"start": start.body, "upload": upload.body, "finish": finish.body},
            remote_ids={
                "facebook_video_id": video_id,
                "facebook_post_id": post_id,
                "operation_key": operation_key(upload_job, media_path),
            },
            publish_state=PublishState.SCHEDULED,
            platform_state="scheduled",
            adapter_version=self.adapter_version,
            api_version=self.api_version,
            retryable=False,
        )

    def reconcile(self, upload_job: dict[str, Any], source_clip: dict[str, Any], profile: dict[str, Any]) -> PublishResult | None:
        remote_ids = upload_job.get("remote_ids") if isinstance(upload_job.get("remote_ids"), dict) else {}
        video_id = str(remote_ids.get("facebook_video_id") or "").strip()
        if not video_id:
            return None
        token = load_access_token(profile)
        result = self._http.get(
            f"https://graph.facebook.com/{self.api_version}/{video_id}",
            params={"access_token": token, "fields": "id,status,scheduled_publish_time"},
        )
        if result.ok:
            return PublishResult(
                ok=True,
                status=UploadStatus.UPLOADED_SCHEDULED,
                platform_asset_id=str(remote_ids.get("facebook_post_id") or video_id),
                response={"reconciled": True, "video_id": video_id},
                raw_response=result.body,
                remote_ids=remote_ids,
                publish_state=PublishState.SCHEDULED,
                platform_state="scheduled",
                adapter_version=self.adapter_version,
                api_version=self.api_version,
            )
        return None

    def _validate(self, upload_job: dict[str, Any], source_clip: dict[str, Any], profile: dict[str, Any]) -> str | None:
        if str(upload_job.get("platform") or "") != self.platform:
            return "upload_job_platform_mismatch"
        if not _page_id(profile):
            return "missing_page_id"
        _path, media_error = media_file(upload_job, source_clip)
        if media_error:
            return media_error
        publish_at = parse_iso_datetime(str(upload_job.get("platform_publish_at") or upload_job.get("publish_at") or ""))
        if publish_at is None:
            return "missing_or_invalid_publish_at"
        return None

    def _upload_binary(self, token: str, upload_url: str, media_path: str) -> HttpResult:
        with open(media_path, "rb") as handle:
            return self._http.post(
                upload_url,
                headers={
                    "Authorization": f"OAuth {token}",
                    "file_offset": "0",
                    "Content-Type": "application/octet-stream",
                },
                data=handle,
                params={"file_size": str(Path(media_path).stat().st_size)},
            )


def _page_id(profile: dict[str, Any]) -> str:
    credentials = profile.get("credentials") if isinstance(profile.get("credentials"), dict) else {}
    page_id = str(credentials.get("page_id") or "").strip()
    if page_id:
        return page_id
    env_name = credential_env(profile, "page_id_env")
    return __import__("os").environ.get(env_name, "").strip() if env_name else ""


def _http_failure(category: str, result: HttpResult, *, remote_ids: dict[str, Any] | None = None) -> PublishResult:
    failure_class, retryable, default_message = classify_http_failure(result)
    response = safe_response(result.body, keep=("error", "errors", "id", "success"))
    retry_after = retry_after_seconds(result)
    if retry_after is not None:
        response["retry_after_seconds"] = retry_after
    return _failure(
        category,
        result.text or default_message or category,
        failure_class,
        retryable,
        response=response,
        raw_response=result.body,
        remote_ids=remote_ids,
    )


def _failure(
    category: str,
    message: str,
    failure_class: str,
    retryable: bool,
    *,
    response: dict[str, Any] | None = None,
    raw_response: dict[str, Any] | None = None,
    remote_ids: dict[str, Any] | None = None,
) -> PublishResult:
    return PublishResult(
        ok=False,
        status=UploadStatus.FAILED_RETRYABLE if retryable else UploadStatus.FAILED_TERMINAL,
        response=response or {},
        raw_response=raw_response or {},
        remote_ids=remote_ids or {},
        failure_class=failure_class,
        error_category=category,
        error_message=message,
        publish_state=PublishState.FAILED,
        platform_state="failed",
        adapter_version=FacebookReelsAdapter.adapter_version,
        api_version=FacebookReelsAdapter.api_version,
        retryable=retryable,
    )
