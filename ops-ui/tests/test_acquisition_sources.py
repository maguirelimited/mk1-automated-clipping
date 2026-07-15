"""Tests for acquisition source type helpers."""

from __future__ import annotations

import pytest

from ops_ui.funnel_management.acquisition_sources import (
    default_per_source_type,
    denormalize_canonical_acquisition_source_type,
    normalize_runtime_acquisition_source_type,
    source_url_placeholder,
    validate_acquisition_source_type,
    validate_per_source_type,
)


@pytest.mark.parametrize(
    ("canonical", "runtime"),
    [
        ("youtube_channel", "youtube_channels"),
        ("youtube_channels", "youtube_channels"),
        ("youtube_playlist", "youtube_playlists"),
        ("youtube_playlists", "youtube_playlists"),
    ],
)
def test_normalize_runtime_acquisition_source_type(canonical: str, runtime: str) -> None:
    assert normalize_runtime_acquisition_source_type(canonical) == runtime


@pytest.mark.parametrize(
    ("runtime", "canonical"),
    [
        ("youtube_channels", "youtube_channel"),
        ("youtube_playlists", "youtube_playlist"),
    ],
)
def test_denormalize_canonical_acquisition_source_type(runtime: str, canonical: str) -> None:
    assert denormalize_canonical_acquisition_source_type(runtime) == canonical


@pytest.mark.parametrize(
    ("acquisition_type", "per_source"),
    [
        ("youtube_channel", "youtube_channel"),
        ("youtube_channels", "youtube_channel"),
        ("youtube_playlist", "youtube_playlist"),
        ("youtube_playlists", "youtube_playlist"),
    ],
)
def test_default_per_source_type(acquisition_type: str, per_source: str) -> None:
    assert default_per_source_type(acquisition_type) == per_source


def test_validate_acquisition_source_type_rejects_unknown() -> None:
    assert validate_acquisition_source_type("yt_dlp_collection", field="Acquisition source type")


def test_validate_per_source_type_rejects_channel_plural() -> None:
    assert validate_per_source_type("youtube_channels", field="Source type")


def test_source_url_placeholder_varies_by_type() -> None:
    assert "playlist" in source_url_placeholder("youtube_playlist")
    assert "@" in source_url_placeholder("youtube_channel")
