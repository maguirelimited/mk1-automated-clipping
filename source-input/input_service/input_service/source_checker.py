"""Check approved YouTube sources for candidate longform podcast videos.

Mk1 only supports YouTube channels. We use ``yt-dlp`` in metadata-only mode
(``extract_flat='in_playlist'`` against the channel ``/videos`` tab and a
follow-up per-video metadata fetch when needed). We never use the YouTube
Data API for mk1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except Exception:  # pragma: no cover - import-time guard for missing dep
    YoutubeDL = None  # type: ignore[assignment]
    DownloadError = Exception  # type: ignore[assignment,misc]


log = logging.getLogger(__name__)


# How many videos to inspect per source. Channels' /videos tabs are sorted
# newest-first by YouTube, so this caps how far back we look.
DEFAULT_MAX_VIDEOS_PER_SOURCE = 25


class SourceCheckError(Exception):
    """Raised when a source cannot be checked at all (network, auth, etc)."""


@dataclass
class Candidate:
    video_id: str
    url: str
    title: str
    source: str  # the channel / source URL it came from
    channel: str | None = None
    duration_seconds: int | None = None  # None if not known yet
    upload_date: str | None = None  # YYYYMMDD if known
    timestamp: int | None = None  # epoch seconds if known
    is_short: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "url": self.url,
            "title": self.title,
            "source": self.source,
            "channel": self.channel,
            "duration_seconds": self.duration_seconds,
            "upload_date": self.upload_date,
            "timestamp": self.timestamp,
            "is_short": self.is_short,
        }


def _ensure_videos_tab(source_url: str) -> str:
    """Normalise a channel URL to its ``/videos`` tab so flat extraction returns
    actual uploads (and not the home tab's mix of shorts/community posts)."""
    url = source_url.strip().rstrip("/")
    lowered = url.lower()
    if any(seg in lowered for seg in ("/videos", "/streams", "/playlists", "/shorts")):
        return url
    if "youtube.com/" in lowered and ("/@" in lowered or "/channel/" in lowered or "/c/" in lowered or "/user/" in lowered):
        return f"{url}/videos"
    return url


def _flat_extract(url: str, max_items: int) -> dict[str, Any]:
    if YoutubeDL is None:
        raise SourceCheckError("yt-dlp is not installed; cannot check sources.")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "playlist_items": f"1-{max_items}",
    }
    with YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except DownloadError as exc:
            raise SourceCheckError(f"yt-dlp failed for {url}: {exc}") from exc
    return info or {}


def _entries(info: dict[str, Any]) -> Iterable[dict[str, Any]]:
    if not info:
        return []
    if isinstance(info.get("entries"), list):
        # Channel pages typically have a single virtual playlist holding the videos.
        for entry in info["entries"]:
            if not isinstance(entry, dict):
                continue
            inner = entry.get("entries")
            if isinstance(inner, list):
                yield from (e for e in inner if isinstance(e, dict))
            else:
                yield entry
        return
    # Fallback: treat the info dict itself as a single video.
    yield info


def _entry_to_candidate(entry: dict[str, Any], source_url: str) -> Candidate | None:
    video_id = entry.get("id") or entry.get("video_id")
    if not video_id:
        return None

    url = entry.get("webpage_url") or entry.get("url")
    if not url or not isinstance(url, str):
        url = f"https://www.youtube.com/watch?v={video_id}"
    elif url.startswith("/"):  # rare, normalise
        url = f"https://www.youtube.com{url}"
    elif not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={video_id}"

    duration = entry.get("duration")
    duration_int: int | None
    try:
        duration_int = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_int = None

    is_short = False
    lowered_url = url.lower()
    if "/shorts/" in lowered_url:
        is_short = True
    if duration_int is not None and duration_int > 0 and duration_int <= 70:
        # Anything under ~70s on YouTube is effectively a Short.
        is_short = True

    return Candidate(
        video_id=str(video_id),
        url=url,
        title=str(entry.get("title") or ""),
        source=source_url,
        channel=entry.get("channel") or entry.get("uploader"),
        duration_seconds=duration_int,
        upload_date=entry.get("upload_date"),
        timestamp=entry.get("timestamp"),
        is_short=is_short,
    )


def _hydrate_metadata(candidate: Candidate) -> Candidate:
    """Fetch full metadata for a single video when flat extraction is missing
    important fields (typically ``duration`` and ``upload_date``)."""
    if YoutubeDL is None:
        return candidate
    if candidate.duration_seconds and candidate.upload_date:
        return candidate

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(candidate.url, download=False)
    except DownloadError as exc:
        log.warning("Could not hydrate metadata for %s: %s", candidate.url, exc)
        return candidate
    if not isinstance(info, dict):
        return candidate

    duration = info.get("duration")
    try:
        duration_int = int(duration) if duration is not None else candidate.duration_seconds
    except (TypeError, ValueError):
        duration_int = candidate.duration_seconds

    return Candidate(
        video_id=candidate.video_id,
        url=candidate.url,
        title=str(info.get("title") or candidate.title),
        source=candidate.source,
        channel=info.get("channel") or info.get("uploader") or candidate.channel,
        duration_seconds=duration_int,
        upload_date=info.get("upload_date") or candidate.upload_date,
        timestamp=info.get("timestamp") or candidate.timestamp,
        is_short=candidate.is_short or bool(info.get("was_live") is False and (duration_int or 0) <= 70),
        extra=candidate.extra,
    )


def check_sources(
    sources: Iterable[str],
    *,
    max_per_source: int = DEFAULT_MAX_VIDEOS_PER_SOURCE,
    hydrate_missing_duration: bool = True,
) -> list[Candidate]:
    """Return candidate videos found across all approved ``sources``.

    Sources whose extraction fails are logged and skipped; this only raises
    ``SourceCheckError`` if **all** sources fail.
    """
    results: list[Candidate] = []
    failures: list[tuple[str, str]] = []

    for raw_source in sources:
        source_url = _ensure_videos_tab(raw_source)
        try:
            info = _flat_extract(source_url, max_items=max_per_source)
        except SourceCheckError as exc:
            log.warning("Source check failed for %s: %s", source_url, exc)
            failures.append((source_url, str(exc)))
            continue

        for entry in _entries(info):
            cand = _entry_to_candidate(entry, source_url=raw_source)
            if cand is None:
                continue
            results.append(cand)

    if not results and failures:
        # Every source failed -> propagate as a single error.
        joined = "; ".join(f"{u}: {e}" for u, e in failures)
        raise SourceCheckError(f"All sources failed to check: {joined}")

    if hydrate_missing_duration:
        # Many channel listings don't include duration in flat mode, so we need
        # one extra request per candidate that's missing it. The candidate
        # filter still rejects unknown durations as a safety net.
        hydrated: list[Candidate] = []
        for cand in results:
            if cand.duration_seconds is None or not cand.upload_date:
                hydrated.append(_hydrate_metadata(cand))
            else:
                hydrated.append(cand)
        results = hydrated

    return results
