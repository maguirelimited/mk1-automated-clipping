"""Deterministic filtering and ordering of candidate videos.

No AI ranking. Rules only:

* duration must fit funnel min/max
* skip duplicates (already in the seen-store)
* skip obvious Shorts/clips/highlights/trailers/teasers/previews/compilations
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .duplicate_store import DuplicateStore
from .funnel_loader import Funnel
from .source_checker import Candidate


# Whole-word match against the title. Order doesn't matter.
BLOCKED_TERMS = (
    "shorts",
    "short",
    "clip",
    "clips",
    "highlight",
    "highlights",
    "trailer",
    "teaser",
    "preview",
    "compilation",
)

_BLOCKED_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in BLOCKED_TERMS) + r")\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class RejectedCandidate:
    candidate: Candidate
    reason: str


def _has_blocked_term(title: str) -> str | None:
    if not title:
        return None
    match = _BLOCKED_RE.search(title)
    return match.group(1).lower() if match else None


def _sort_key(c: Candidate) -> tuple[int, int]:
    """Newest first. Use ``timestamp`` if present, else ``upload_date``, else 0."""
    ts = c.timestamp if isinstance(c.timestamp, int) else 0
    ud = 0
    if c.upload_date and c.upload_date.isdigit() and len(c.upload_date) == 8:
        ud = int(c.upload_date)
    return (ts, ud)


def filter_candidates(
    candidates: list[Candidate],
    funnel: Funnel,
    seen: DuplicateStore,
) -> tuple[list[Candidate], list[RejectedCandidate]]:
    """Return ``(valid_sorted_newest_first, rejected_with_reasons)``."""
    valid: list[Candidate] = []
    rejected: list[RejectedCandidate] = []

    min_s = funnel.min_duration_seconds
    max_s = funnel.max_duration_seconds

    for cand in candidates:
        if cand.is_short:
            rejected.append(RejectedCandidate(cand, "is_short"))
            continue

        blocked = _has_blocked_term(cand.title)
        if blocked:
            rejected.append(RejectedCandidate(cand, f"title_blocked:{blocked}"))
            continue

        if cand.duration_seconds is None:
            rejected.append(RejectedCandidate(cand, "duration_unknown"))
            continue
        if cand.duration_seconds < min_s:
            rejected.append(RejectedCandidate(cand, "below_min_duration"))
            continue
        if cand.duration_seconds > max_s:
            rejected.append(RejectedCandidate(cand, "above_max_duration"))
            continue

        if seen.is_seen(video_id=cand.video_id, url=cand.url):
            rejected.append(RejectedCandidate(cand, "duplicate"))
            continue

        valid.append(cand)

    valid.sort(key=_sort_key, reverse=True)
    return valid, rejected
