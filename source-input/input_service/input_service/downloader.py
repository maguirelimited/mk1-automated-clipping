"""Download a single candidate video using yt-dlp.

We download to a per-funnel temp directory and return the resulting file
path on success. The caller (``runner.run_funnel``) is responsible for
validating, then moving the file into the ready location, then marking it
as seen. We never download more than one video here.

If ``YT_DLP_COOKIES_PATH`` points to a Netscape ``cookies.txt`` file, it is
passed to yt-dlp (helps with YouTube bot challenges). See ``yt_dlp_cookies``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except Exception:  # pragma: no cover
    YoutubeDL = None  # type: ignore[assignment]
    DownloadError = Exception  # type: ignore[assignment,misc]

from . import paths
from .source_checker import Candidate
from .yt_dlp_cookies import resolve_yt_dlp_cookiefile


log = logging.getLogger(__name__)


class DownloadFailed(Exception):
    pass


@dataclass
class DownloadResult:
    file_path: Path
    candidate: Candidate


def _build_outtmpl(tmp_dir: Path, video_id: str) -> str:
    # Force a stable filename so we always know what to validate / move.
    return str(tmp_dir / f"{video_id}.%(ext)s")


def _ydl_options(tmp_dir: Path, video_id: str) -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "outtmpl": _build_outtmpl(tmp_dir, video_id),
        "format": "bv*+ba/b",  # best video + best audio, fallback to best
        "merge_output_format": "mp4",
        "restrictfilenames": True,
        "noplaylist": True,
        "concurrent_fragment_downloads": 4,
        "retries": 3,
        "fragment_retries": 3,
        "overwrites": True,
        # We deliberately do not pass any postprocessors that would change
        # codecs; mk1 just needs a file we can clip downstream.
    }


def download_candidate(candidate: Candidate, *, funnel_id: str) -> DownloadResult:
    """Download ``candidate`` and return the on-disk path. Raises ``DownloadFailed``."""
    if YoutubeDL is None:
        raise DownloadFailed("yt-dlp is not installed; cannot download.")

    tmp_dir = paths.funnel_tmp_dir(funnel_id)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Clean any prior partials for this video id so we don't accidentally
    # validate stale bytes.
    for existing in tmp_dir.glob(f"{candidate.video_id}.*"):
        try:
            existing.unlink()
        except OSError:
            pass

    opts = _ydl_options(tmp_dir, candidate.video_id)
    cookiefile = resolve_yt_dlp_cookiefile()
    if cookiefile:
        opts["cookiefile"] = cookiefile

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(candidate.url, download=True)
            if not isinstance(info, dict):
                raise DownloadFailed(f"yt-dlp returned no info for {candidate.url}")
            file_path = Path(ydl.prepare_filename(info))
    except DownloadError as exc:
        raise DownloadFailed(f"yt-dlp download failed for {candidate.url}: {exc}") from exc

    if not file_path.exists():
        # When merging, the merged file's extension may differ. Look for any
        # file we wrote with this video_id prefix.
        matches = sorted(tmp_dir.glob(f"{candidate.video_id}.*"))
        if not matches:
            raise DownloadFailed(
                f"yt-dlp finished but no file found for {candidate.video_id} in {tmp_dir}"
            )
        # Prefer .mp4, otherwise the largest file.
        mp4 = [m for m in matches if m.suffix.lower() == ".mp4"]
        file_path = mp4[0] if mp4 else max(matches, key=lambda p: p.stat().st_size)

    return DownloadResult(file_path=file_path, candidate=candidate)
