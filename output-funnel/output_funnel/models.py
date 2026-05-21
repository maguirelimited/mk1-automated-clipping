from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Platform = Literal["youtube_shorts", "tiktok", "instagram_reels", "x"]


class UploadStatus:
    REGISTERED = "registered"
    ROUTED = "routed"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    UPLOADED = "uploaded"
    SCHEDULED_ON_PLATFORM = "scheduled_on_platform"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {
    UploadStatus.UPLOADED,
    UploadStatus.SCHEDULED_ON_PLATFORM,
    UploadStatus.FAILED_TERMINAL,
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
