"""Shared yt-dlp cookies resolution (Netscape cookies.txt).

Set ``YT_DLP_COOKIES_PATH`` to an absolute or ``~`` path to help bypass
YouTube bot / "Sign in to confirm" challenges. Export cookies with a browser
extension such as "Get cookies.txt LOCALLY".
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def resolve_yt_dlp_cookiefile() -> str | None:
    """Return a resolved cookies path for yt-dlp's ``cookiefile`` option, or ``None``.

    If ``YT_DLP_COOKIES_PATH`` is unset or the path is missing, returns ``None``
    (yt-dlp runs without cookies). Expects Netscape ``cookies.txt`` format.
    """
    raw = os.environ.get("YT_DLP_COOKIES_PATH", "").strip()
    if not raw:
        log.debug("YT_DLP_COOKIES_PATH unset; yt-dlp runs without browser cookies")
        return None
    path = Path(raw).expanduser()
    if not path.is_file():
        log.warning(
            "YT_DLP_COOKIES_PATH points to a missing or non-file path (%s); "
            "continuing without cookies",
            path,
        )
        return None
    resolved = str(path.resolve())
    log.info("yt-dlp will use Netscape cookies from %s", resolved)
    return resolved
