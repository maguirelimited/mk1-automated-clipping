"""JSON-backed duplicate store for processed videos.

Mk1 keeps state in a single file (``data/state/seen_urls.json``). We track
both ``video_ids`` and ``urls`` so callers can match either. A video is only
marked as seen after a successful download AND validation.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from . import paths


_LOCK = threading.Lock()


@dataclass
class _State:
    video_ids: set[str]
    urls: set[str]


def _empty_state() -> _State:
    return _State(video_ids=set(), urls=set())


def _read(file: Path) -> _State:
    if not file.exists():
        return _empty_state()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(raw, dict):
        return _empty_state()
    return _State(
        video_ids={str(x) for x in raw.get("video_ids", []) if isinstance(x, (str, int))},
        urls={str(x) for x in raw.get("urls", []) if isinstance(x, str)},
    )


def _atomic_write(file: Path, state: _State) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "video_ids": sorted(state.video_ids),
        "urls": sorted(state.urls),
    }
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=str(file.parent),
        prefix=".seen_urls.",
        suffix=".tmp",
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, file)


class DuplicateStore:
    """Process-safe-ish JSON duplicate store.

    Designed for one run at a time (we hold a process lock around the
    file). For mk1 that matches the agreed concurrency model; the HTTP
    layer additionally guarantees only one ``run_funnel`` runs at a time.
    """

    def __init__(self, file: Path | None = None) -> None:
        self._file = file or paths.SEEN_FILE

    @property
    def file(self) -> Path:
        return self._file

    def reload(self) -> _State:
        with _LOCK:
            return _read(self._file)

    def is_seen(self, *, video_id: str | None = None, url: str | None = None) -> bool:
        state = self.reload()
        if video_id and str(video_id) in state.video_ids:
            return True
        if url and url in state.urls:
            return True
        return False

    def mark_seen(self, *, video_id: str | None = None, url: str | None = None) -> None:
        if not video_id and not url:
            raise ValueError("mark_seen requires video_id or url")
        with _LOCK:
            state = _read(self._file)
            if video_id:
                state.video_ids.add(str(video_id))
            if url:
                state.urls.add(url)
            _atomic_write(self._file, state)
