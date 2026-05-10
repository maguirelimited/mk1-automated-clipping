"""Mk1 standalone podcast input funnel service.

This package is intentionally independent of the clipping / transcription /
upload pipeline. It only finds, downloads, validates, and stores ONE ready
longform podcast video per run.
"""

__all__ = [
    "funnel_loader",
    "source_checker",
    "candidate_filter",
    "duplicate_store",
    "downloader",
    "media_validator",
    "storage",
    "runner",
]
