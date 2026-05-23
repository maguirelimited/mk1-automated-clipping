from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Platform = Literal["youtube_shorts", "tiktok", "instagram_reels", "x"]


class UploadStatus:
    """Lifecycle states for an upload_job row.

    Boundary between ``planned`` and ``pending_upload``:
      - ``planned``         The plan exists (publish_at + upload_at). No
                            upload attempt has happened yet. This is the
                            resting state after :func:`plan_upload_job`.
      - ``pending_upload``  A previous attempt failed retryably and the
                            job is queued for another attempt within the
                            existing upload window. Set by
                            :func:`retry_upload_job` after a
                            ``failed_retryable``.

    Both are equally eligible for ``claim_upload_due_jobs``; the
    distinction is purely informational ("fresh job" vs "in-retry").

    ``registered`` and ``routed`` are upstream stages owned by the
    registry/router and are part of the wider funnel; they remain.
    """

    REGISTERED = "registered"
    ROUTED = "routed"
    PLANNED = "planned"
    PENDING_UPLOAD = "pending_upload"
    UPLOADING = "uploading"
    UPLOADED_SCHEDULED = "uploaded_scheduled"
    PUBLISHED = "published"
    FAILED_UPLOAD = "failed_upload"
    MISSED_UPLOAD_WINDOW = "missed_upload_window"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"

    SCHEDULED = "planned"
    PUBLISHING = "uploading"
    UPLOADED = "uploaded_scheduled"
    SCHEDULED_ON_PLATFORM = "uploaded_scheduled"


LEGACY_STATUS_ALIASES: dict[str, str] = {
    "scheduled": UploadStatus.PLANNED,
    "publishing": UploadStatus.UPLOADING,
    "uploaded": UploadStatus.UPLOADED_SCHEDULED,
    "scheduled_on_platform": UploadStatus.UPLOADED_SCHEDULED,
}


def canonical_status(value: str | None) -> str | None:
    if value is None:
        return None
    return LEGACY_STATUS_ALIASES.get(value, value)


TERMINAL_STATUSES = {
    UploadStatus.UPLOADED_SCHEDULED,
    UploadStatus.PUBLISHED,
    UploadStatus.FAILED_UPLOAD,
    UploadStatus.FAILED_TERMINAL,
    UploadStatus.MISSED_UPLOAD_WINDOW,
    UploadStatus.CANCELLED,
}


@dataclass(frozen=True)
class SourceClip:
    source_job_id: str
    clip_id: str
    clip_index: int | None = None
    start: str | None = None
    end: str | None = None
    duration_sec: float | None = None
    clip_file: str | None = None
    clip_path: str | None = None
    job_clip_path: str | None = None
    title: str | None = None
    hook: str | None = None
    caption: str | None = None
    reason: str | None = None
    scores: dict[str, Any] = field(default_factory=dict)
    composite_score: float | None = None
    clip_validation: dict[str, Any] = field(default_factory=dict)
    source_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    media_path: str | None
    file_size_bytes: int | None = None
    ffprobe_duration_sec: float | None = None
    expected_duration_sec: float | None = None
    issues: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MetadataResult:
    title: str
    description: str
    hashtags: list[str]
    publish_at: str | None = None
    issues: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RouteResult:
    matched: bool
    channel_id: str | None = None
    profile: dict[str, Any] | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PublishResult:
    ok: bool
    status: str
    platform_asset_id: str | None = None
    scheduled_at: str | None = None
    response: dict[str, Any] = field(default_factory=dict)
    error_category: str | None = None
    error_message: str | None = None
    retryable: bool = False
