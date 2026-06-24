from __future__ import annotations

from pathlib import Path
from typing import Any

from output_funnel.adapters.common import (
    HttpResult,
    classify_http_failure,
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


class XAdapter(PlatformAdapter):
    platform = "x"
    adapter_version = "1"
    api_version = "2"

    def __init__(self, http_client: Any | None = None):
        self._http = http_client_or_default(http_client)

    def publish(self, upload_job: dict[str, Any], source_clip: dict[str, Any], profile: dict[str, Any]) -> PublishResult:
        validation_error = self._validate(upload_job, source_clip)
        if validation_error:
            return _failure(
                "x_validation_error",
                validation_error,
                failure_class=FailureClass.PERMANENT_FAILURE,
                retryable=False,
            )
        media_path, _ = media_file(upload_job, source_clip)
        assert media_path is not None
        token = load_access_token(profile)
        media_id = str((upload_job.get("remote_ids") or {}).get("x_media_id") or "")
        if not media_id:
            media_id = self._upload_media(token, media_path)
        text = str(upload_job.get("normalized_description") or upload_job.get("normalized_title") or "")
        post_result = self._post_tweet(token, text=text, media_id=media_id)
        if not post_result.ok:
            return _http_failure("x_post_error", post_result, remote_ids={"x_media_id": media_id})
        tweet = post_result.body.get("data") if isinstance(post_result.body.get("data"), dict) else post_result.body
        tweet_id = str(tweet.get("id") or "").strip()
        if not tweet_id:
            return _failure(
                "x_response_missing_id",
                "X response did not include a post id",
                failure_class=FailureClass.RETRYABLE,
                retryable=True,
                response=safe_response(post_result.body, keep=("data", "errors")),
                raw_response=post_result.body,
                remote_ids={"x_media_id": media_id},
            )
        return PublishResult(
            ok=True,
            status=UploadStatus.UPLOADED_SCHEDULED,
            platform_asset_id=tweet_id,
            response={"id": tweet_id, "media_id": media_id},
            raw_response=post_result.body,
            remote_ids={"x_media_id": media_id, "x_post_id": tweet_id, "operation_key": operation_key(upload_job, media_path)},
            publish_state=PublishState.PUBLISHED,
            platform_state="published",
            adapter_version=self.adapter_version,
            api_version=self.api_version,
            retryable=False,
        )

    def reconcile(self, upload_job: dict[str, Any], source_clip: dict[str, Any], profile: dict[str, Any]) -> PublishResult | None:
        remote_ids = upload_job.get("remote_ids") if isinstance(upload_job.get("remote_ids"), dict) else {}
        post_id = str(remote_ids.get("x_post_id") or upload_job.get("platform_asset_id") or "").strip()
        if not post_id:
            return None
        token = load_access_token(profile)
        result = self._http.get(
            f"https://api.x.com/2/tweets/{post_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if result.ok:
            return PublishResult(
                ok=True,
                status=UploadStatus.UPLOADED_SCHEDULED,
                platform_asset_id=post_id,
                response={"reconciled": True, "id": post_id},
                raw_response=result.body,
                remote_ids={**remote_ids, "x_post_id": post_id},
                publish_state=PublishState.PUBLISHED,
                platform_state="published",
                adapter_version=self.adapter_version,
                api_version=self.api_version,
            )
        return None

    def _validate(self, upload_job: dict[str, Any], source_clip: dict[str, Any]) -> str | None:
        if str(upload_job.get("platform") or "") != self.platform:
            return "upload_job_platform_mismatch"
        _path, media_error = media_file(upload_job, source_clip)
        if media_error:
            return media_error
        text = str(upload_job.get("normalized_description") or upload_job.get("normalized_title") or "").strip()
        if not text:
            return "missing_post_text"
        if len(text) > 280:
            return "post_text_too_long"
        return None

    def _upload_media(self, token: str, media_path: str) -> str:
        size = Path(media_path).stat().st_size
        init = self._http.post(
            "https://api.x.com/2/media/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={
                "command": (None, "INIT"),
                "media_type": (None, "video/mp4"),
                "media_category": (None, "tweet_video"),
                "total_bytes": (None, str(size)),
            },
        )
        if not init.ok:
            raise RuntimeError(f"x_media_init_failed:{init.status_code}:{init.text}")
        media_id = str(init.body.get("media_id_string") or init.body.get("media_id") or "").strip()
        if not media_id:
            raise RuntimeError("x_media_init_missing_media_id")
        with open(media_path, "rb") as handle:
            append = self._http.post(
                "https://api.x.com/2/media/upload",
                headers={"Authorization": f"Bearer {token}"},
                files={
                    "command": (None, "APPEND"),
                    "media_id": (None, media_id),
                    "segment_index": (None, "0"),
                    "media": (Path(media_path).name, handle, "video/mp4"),
                },
            )
        if not append.ok:
            raise RuntimeError(f"x_media_append_failed:{append.status_code}:{append.text}")
        finalize = self._http.post(
            "https://api.x.com/2/media/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={"command": (None, "FINALIZE"), "media_id": (None, media_id)},
        )
        if not finalize.ok:
            raise RuntimeError(f"x_media_finalize_failed:{finalize.status_code}:{finalize.text}")
        processing = finalize.body.get("processing_info") if isinstance(finalize.body.get("processing_info"), dict) else {}
        if processing:
            poll_until(
                poll_fn=lambda: self._http.get(
                    "https://api.x.com/2/media/upload",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"command": "STATUS", "media_id": media_id},
                ),
                done_fn=lambda r: str(
                    (r.body.get("processing_info") if isinstance(r.body.get("processing_info"), dict) else {}).get("state")
                    or "succeeded"
                )
                in {"succeeded", "failed"},
                sleep_seconds=5,
                max_attempts=30,
            )
        return media_id

    def _post_tweet(self, token: str, *, text: str, media_id: str) -> HttpResult:
        return self._http.post(
            "https://api.x.com/2/tweets",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": text, "media": {"media_ids": [media_id]}},
        )


def _http_failure(category: str, result: HttpResult, *, remote_ids: dict[str, Any] | None = None) -> PublishResult:
    failure_class, retryable, default_message = classify_http_failure(result)
    response = safe_response(result.body, keep=("error", "errors", "title", "detail", "type"))
    retry_after = retry_after_seconds(result)
    if retry_after is not None:
        response["retry_after_seconds"] = retry_after
    return _failure(
        category,
        result.text or default_message or category,
        failure_class=failure_class,
        retryable=retryable,
        response=response,
        raw_response=result.body,
        remote_ids=remote_ids or {},
    )


def _failure(
    category: str,
    message: str,
    *,
    failure_class: str,
    retryable: bool,
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
        adapter_version=XAdapter.adapter_version,
        api_version=XAdapter.api_version,
        retryable=retryable,
    )
