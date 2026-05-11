"""Check configured sources for candidate longform videos.

mk0.4 keeps ingestion lightweight by routing source types through yt-dlp where
possible. Channel-like YouTube sources are normalised to ``/videos``; playlist
and generic yt-dlp collection sources are passed through unchanged.
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

from .yt_dlp_cookies import apply_yt_dlp_auth_runtime_options


log = logging.getLogger(__name__)


# How many videos to inspect per source. Channels' /videos tabs are sorted
# newest-first by YouTube, so this caps how far back we look.
DEFAULT_MAX_VIDEOS_PER_SOURCE = 25
CHANNEL_SOURCE_TYPES = {"youtube_channel", "youtube_channels"}


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


def _normalise_source_url(source_url: str, source_type: str) -> str:
    if source_type in CHANNEL_SOURCE_TYPES:
        return _ensure_videos_tab(source_url)
    return source_url.strip()


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
    opts = apply_yt_dlp_auth_runtime_options(opts)
    log.info("Checking source with yt-dlp: url=%s max_items=%s", url, max_items)
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


def _entry_to_candidate(
    entry: dict[str, Any],
    source_url: str,
    *,
    source_id: str | None = None,
    source_type: str | None = None,
    source_label: str | None = None,
    hydrate_missing_duration: bool = True,
    title_blocklist: tuple[str, ...] = (),
    title_allowlist: tuple[str, ...] = (),
) -> Candidate | None:
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
    opts = apply_yt_dlp_auth_runtime_options(opts)
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


def _source_attr(source: Any, name: str, default: Any = None) -> Any:
    return getattr(source, name, default)


def check_sources(
    sources: Iterable[Any],
    *,
    max_per_source: int = DEFAULT_MAX_VIDEOS_PER_SOURCE,
    hydrate_missing_duration: bool = True,
    max_candidates: int | None = None,
) -> list[Candidate]:
    """Return candidate videos found across all approved ``sources``.

    Sources whose extraction fails are logged and skipped; this only raises
    ``SourceCheckError`` if **all** sources fail.
    """
    results: list[Candidate] = []
    failures: list[tuple[str, str]] = []
    target_candidates = max(1, int(max_candidates)) if max_candidates else None

    for raw_source in sources:
        if target_candidates and len(results) >= target_candidates:
            break
        if isinstance(raw_source, str):
            configured_url = raw_source
            source_type = "youtube_channels"
            source_id = None
            source_label = None
            source_active = True
            local_max = max_per_source
            local_hydrate = hydrate_missing_duration
            title_blocklist: tuple[str, ...] = ()
            title_allowlist: tuple[str, ...] = ()
        else:
            configured_url = str(_source_attr(raw_source, "url", "") or "")
            source_type = str(_source_attr(raw_source, "source_type", "youtube_channels"))
            source_id = _source_attr(raw_source, "source_id", None)
            source_label = _source_attr(raw_source, "label", None)
            source_active = bool(_source_attr(raw_source, "active", True))
            local_max = int(_source_attr(raw_source, "max_videos_per_source", 0) or max_per_source)
            local_hydrate = bool(_source_attr(raw_source, "hydrate_missing_duration", hydrate_missing_duration))
            title_blocklist = tuple(_source_attr(raw_source, "title_blocklist", ()) or ())
            title_allowlist = tuple(_source_attr(raw_source, "title_allowlist", ()) or ())

        if not source_active:
            continue

        source_url = _normalise_source_url(configured_url, source_type)
        log.info(
            "Source being checked: source_id=%s type=%s url=%s max_items=%s",
            source_id or "<legacy>",
            source_type,
            source_url,
            local_max,
        )
        try:
            info = _flat_extract(source_url, max_items=local_max)
        except SourceCheckError as exc:
            log.warning("Source check failed for %s: %s", source_url, exc)
            failures.append((source_url, str(exc)))
            continue

        source_count = 0
        for entry in _entries(info):
            cand = _entry_to_candidate(
                entry,
                source_url=configured_url,
                source_id=str(source_id) if source_id else None,
                source_type=source_type,
                source_label=str(source_label) if source_label else None,
                hydrate_missing_duration=local_hydrate,
                title_blocklist=title_blocklist,
                title_allowlist=title_allowlist,
            )
            if cand is None:
                continue
            cand.extra.update(
                {
                    "source_id": str(source_id) if source_id else None,
                    "source_type": source_type,
                    "source_label": str(source_label) if source_label else None,
                    "hydrate_missing_duration": local_hydrate,
                    "title_blocklist": list(title_blocklist),
                    "title_allowlist": list(title_allowlist),
                }
            )
            results.append(cand)
            source_count += 1
            if target_candidates and len(results) >= target_candidates:
                break
        log.info(
            "Candidate videos found for source_id=%s: %s (total_so_far=%s)",
            source_id or "<legacy>",
            source_count,
            len(results),
        )

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
            if not bool(cand.extra.get("hydrate_missing_duration", True)):
                hydrated.append(cand)
            elif cand.duration_seconds is None or not cand.upload_date:
                log.info("Hydrating candidate metadata: url=%s", cand.url)
                hydrated.append(_hydrate_metadata(cand))
            else:
                hydrated.append(cand)
        results = hydrated

    log.info("Candidate source check complete: candidates=%s", len(results))

    return results
