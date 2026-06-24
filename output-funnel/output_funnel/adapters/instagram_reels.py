from __future__ import annotations

import os
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
    poll_until,
    retry_after_seconds,
    safe_response,
)
from output_funnel.models import FailureClass, PublishResult, PublishState, UploadStatus

from .base import PlatformAdapter


class InstagramReelsAdapter(PlatformAdapter):
    platform = "instagram_reels"
    adapter_version = "1"
    api_version = "v25.0"

    def __init__(self, http_client: Any | None = None):
        self._http = http_client_or_default(http_client)

    def publish(self, upload_job: dict[str, Any], source_clip: dict[str, Any], profile: dict[str, Any]) -> PublishResult:
        validation_error = self._validate(upload_job, source_clip, profile)
        if validation_error:
            return _failure("instagram_validation_error", validation_error, FailureClass.PERMANENT_FAILURE, False)
        media_path, _ = media_file(upload_job, source_clip)
        assert media_path is not None
        token = load_access_token(profile)
        ig_user_id = _ig_user_id(profile)
        remote_ids = upload_job.get("remote_ids") if isinstance(upload_job.get("remote_ids"), dict) else {}
        container_id = str(remote_ids.get("instagram_container_id") or "").strip()
        if not container_id:
            create = self._create_container(token, ig_user_id, upload_job)
            if not create.ok:
                return _http_failure("instagram_container_create_error", create)
            container_id = str(create.body.get("id") or "").strip()
            if not container_id:
                return _failure(
                    "instagram_container_missing_id",
                    "Instagram create response did not include a container id",
                    FailureClass.RETRYABLE,
                    True,
                    raw_response=create.body,
                )
            upload = self._upload_binary(token, container_id, media_path)
            if not upload.ok:
                return _http_failure(
                    "instagram_resumable_upload_error",
                    upload,
                    remote_ids={"instagram_container_id": container_id},
                )
        status = self._poll_container(token, container_id)
        if not status.ok:
            return _http_failure(
                "instagram_container_status_error",
                status,
                remote_ids={"instagram_container_id": container_id},
            )
        status_code = str(status.body.get("status_code") or "").upper()
        if status_code not in {"FINISHED", "PUBLISHED"}:
            return _failure(
                "instagram_container_not_finished",
                f"Instagram container status is {status_code or 'unknown'}",
                FailureClass.RETRYABLE,
                True,
                raw_response=status.body,
                remote_ids={"instagram_container_id": container_id},
            )
        publish = self._http.post(
            f"https://graph.facebook.com/{self.api_version}/{ig_user_id}/media_publish",
            params={"access_token": token, "creation_id": container_id},
        )
        if not publish.ok:
            return _http_failure(
                "instagram_media_publish_error",
                publish,
                remote_ids={"instagram_container_id": container_id},
            )
        media_id = str(publish.body.get("id") or "").strip()
        if not media_id:
            return _failure(
                "instagram_publish_missing_media_id",
                "Instagram publish response did not include media id",
                FailureClass.RETRYABLE,
                True,
                raw_response=publish.body,
                remote_ids={"instagram_container_id": container_id},
            )
        return PublishResult(
            ok=True,
            status=UploadStatus.UPLOADED_SCHEDULED,
            platform_asset_id=media_id,
            response={"media_id": media_id, "container_id": container_id, "published": True},
            raw_response={"status": status.body, "publish": publish.body},
            remote_ids={
                "instagram_container_id": container_id,
                "instagram_media_id": media_id,
                "operation_key": operation_key(upload_job, media_path),
            },
            publish_state=PublishState.PUBLISHED,
            platform_state="published",
            adapter_version=self.adapter_version,
            api_version=self.api_version,
            retryable=False,
        )

    def reconcile(self, upload_job: dict[str, Any], source_clip: dict[str, Any], profile: dict[str, Any]) -> PublishResult | None:
        remote_ids = upload_job.get("remote_ids") if isinstance(upload_job.get("remote_ids"), dict) else {}
        media_id = str(remote_ids.get("instagram_media_id") or upload_job.get("platform_asset_id") or "").strip()
        if not media_id:
            return None
        token = load_access_token(profile)
        result = self._http.get(
            f"https://graph.facebook.com/{self.api_version}/{media_id}",
            params={"access_token": token, "fields": "id,media_type,permalink"},
        )
        if result.ok:
            return PublishResult(
                ok=True,
                status=UploadStatus.UPLOADED_SCHEDULED,
                platform_asset_id=media_id,
                response={"reconciled": True, "media_id": media_id},
                raw_response=result.body,
                remote_ids=remote_ids,
                publish_state=PublishState.PUBLISHED,
                platform_state="published",
                adapter_version=self.adapter_version,
                api_version=self.api_version,
            )
        return None

    def _validate(self, upload_job: dict[str, Any], source_clip: dict[str, Any], profile: dict[str, Any]) -> str | None:
        if str(upload_job.get("platform") or "") != self.platform:
            return "upload_job_platform_mismatch"
        if not _ig_user_id(profile):
            return "missing_ig_user_id"
        _path, media_error = media_file(upload_job, source_clip)
        return media_error

    def _create_container(self, token: str, ig_user_id: str, upload_job: dict[str, Any]) -> HttpResult:
        return self._http.post(
            f"https://graph.facebook.com/{self.api_version}/{ig_user_id}/media",
            params={
                "access_token": token,
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": str(upload_job.get("normalized_description") or ""),
            },
        )

    def _upload_binary(self, token: str, container_id: str, media_path: str) -> HttpResult:
        with open(media_path, "rb") as handle:
            return self._http.post(
                f"https://rupload.facebook.com/ig-api-upload/{self.api_version}/{container_id}",
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(Path(media_path).stat().st_size),
                    "Content-Type": "application/octet-stream",
                },
                data=handle,
            )

    def _poll_container(self, token: str, container_id: str) -> HttpResult:
        return poll_until(
            poll_fn=lambda: self._http.get(
                f"https://graph.facebook.com/{self.api_version}/{container_id}",
                params={"access_token": token, "fields": "status_code"},
            ),
            done_fn=lambda r: str(r.body.get("status_code") or "").upper() in {"FINISHED", "ERROR", "EXPIRED", "PUBLISHED"},
            sleep_seconds=5,
            max_attempts=30,
        )


def _ig_user_id(profile: dict[str, Any]) -> str:
    credentials = profile.get("credentials") if isinstance(profile.get("credentials"), dict) else {}
    raw = str(credentials.get("ig_user_id") or "").strip()
    if raw:
        return raw
    env_name = credential_env(profile, "ig_user_id_env")
    return os.environ.get(env_name, "").strip() if env_name else ""


def _http_failure(category: str, result: HttpResult, *, remote_ids: dict[str, Any] | None = None) -> PublishResult:
    failure_class, retryable, default_message = classify_http_failure(result)
    response = safe_response(result.body, keep=("error", "errors", "id", "status_code"))
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
        adapter_version=InstagramReelsAdapter.adapter_version,
        api_version=InstagramReelsAdapter.api_version,
        retryable=retryable,
    )
