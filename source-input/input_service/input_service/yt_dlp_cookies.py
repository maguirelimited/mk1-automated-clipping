"""Shared yt-dlp authentication/runtime option helpers.

Set one cookie mode:

- ``YT_DLP_COOKIES_FROM_BROWSER=chrome`` for yt-dlp's browser cookie loader.
- ``YT_DLP_COOKIES_PATH=/path/to/cookies.txt`` for Netscape cookies.txt.

Set ``YT_DLP_JS_RUNTIME=deno`` (or truthy ``YT_DLP_USE_DENO=1``) to enable
yt-dlp's Deno JavaScript runtime for YouTube n-challenge solving.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .log_util import detail

log = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on", "deno"}
_AUTH_SUMMARY_LOGGED = False


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _parse_browser_spec(raw: str) -> tuple[str, str | None, str | None, str | None]:
    """Parse BROWSER[+KEYRING][:PROFILE][::CONTAINER] for yt-dlp's Python API."""
    browser_keyring, sep, rest = raw.partition(":")
    browser, plus, keyring = browser_keyring.partition("+")
    profile: str | None = None
    container: str | None = None
    if sep:
        profile_part, sep2, container_part = rest.partition("::")
        profile = profile_part or None
        container = (container_part or None) if sep2 else None
    return browser, (keyring or None if plus else None), profile, container


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
    return str(path.resolve())


def resolve_yt_dlp_browser_cookies() -> tuple[str, str | None, str | None, str | None] | None:
    """Return yt-dlp ``cookiesfrombrowser`` tuple, or ``None`` when disabled."""
    raw = _env("YT_DLP_COOKIES_FROM_BROWSER")
    if not raw:
        return None
    return _parse_browser_spec(raw)


def resolve_yt_dlp_js_runtimes() -> dict[str, dict[str, str]] | None:
    """Return yt-dlp ``js_runtimes`` dict, currently supporting Deno."""
    runtime = _env("YT_DLP_JS_RUNTIME").lower()
    use_deno = _env("YT_DLP_USE_DENO").lower() in _TRUTHY
    if runtime and runtime != "deno":
        log.warning("Unsupported YT_DLP_JS_RUNTIME=%s; continuing without override", runtime)
        return None
    if runtime == "deno" or use_deno:
        deno_path = _env("YT_DLP_DENO_PATH")
        if deno_path:
            return {"deno": {"path": deno_path}}
        return {"deno": {}}
    return None


def resolve_yt_dlp_remote_components() -> list[str]:
    """Allow yt-dlp's recommended remote EJS challenge solver script by default.

    Recent YouTube extraction can expose only storyboard image formats unless
    yt-dlp can fetch the EJS solver script. This mirrors
    ``--remote-components ejs:github`` while keeping the setting overrideable.
    """
    raw = _env("YT_DLP_REMOTE_COMPONENTS")
    if raw:
        return [item.strip() for item in raw.replace(",", " ").split() if item.strip()]
    return ["ejs:github"]


def _auth_mode_label(
    *,
    browser: tuple[str, str | None, str | None, str | None] | None,
    cookiefile: str | None,
    js_runtimes: dict[str, dict[str, str]] | None,
    remote_components: list[str],
) -> str:
    if browser:
        cookie_mode = f"browser:{browser[0]}"
    elif cookiefile:
        cookie_mode = f"cookies.txt ({cookiefile})"
    else:
        cookie_mode = "none"
    if js_runtimes:
        js_mode = ",".join(sorted(js_runtimes.keys()))
    else:
        js_mode = "default"
    remote_mode = ",".join(remote_components) if remote_components else "none"
    return f"cookie={cookie_mode} js={js_mode} remote={remote_mode}"


def apply_yt_dlp_auth_runtime_options(opts: dict[str, Any]) -> dict[str, Any]:
    """Apply cookie/browser-cookie and JS-runtime options to a yt-dlp options dict."""
    global _AUTH_SUMMARY_LOGGED

    browser = resolve_yt_dlp_browser_cookies()
    cookiefile = None if browser else resolve_yt_dlp_cookiefile()
    if browser:
        opts["cookiesfrombrowser"] = browser
    elif cookiefile:
        opts["cookiefile"] = cookiefile

    js_runtimes = resolve_yt_dlp_js_runtimes()
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes
    remote_components = resolve_yt_dlp_remote_components()
    if remote_components:
        opts["remote_components"] = remote_components

    if not _AUTH_SUMMARY_LOGGED:
        _AUTH_SUMMARY_LOGGED = True
        detail(
            log,
            "yt-dlp runtime: %s",
            _auth_mode_label(
                browser=browser,
                cookiefile=cookiefile,
                js_runtimes=js_runtimes,
                remote_components=remote_components,
            ),
        )
    return opts
