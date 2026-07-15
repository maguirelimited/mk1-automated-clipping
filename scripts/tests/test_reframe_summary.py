"""Tests for reframe summary extraction from metadata."""

from __future__ import annotations

from observability.reframe_summary import (
    extract_reframe_summary,
    extract_reframe_summary_from_metadata_payload,
)


def _format_module(**metadata):
    return {
        "module_name": "platform_safe_format_v1",
        "status": "PASS",
        "metadata": metadata,
    }


class TestReframeSummaryExtraction:
    def test_extracts_face_track_fields(self) -> None:
        summary = extract_reframe_summary(
            [
                _format_module(
                    reframe_mode="auto",
                    format_strategy="face_track_crop",
                    face_track_test_enabled=True,
                    face_track_attempted=True,
                    face_track_used=True,
                    face_track_eligible=True,
                    face_track_eligibility_reason="eligible",
                    face_coverage_pct=100.0,
                    segments_rendered=34,
                )
            ]
        )
        assert summary["available"] is True
        assert summary["face_track_used"] is True
        assert summary["format_strategy"] == "face_track_crop"
        assert summary["segments_rendered"] == 34

    def test_missing_module_returns_unavailable(self) -> None:
        assert extract_reframe_summary([])["available"] is False

    def test_extract_from_metadata_payload(self) -> None:
        payload = {
            "module_results": [
                _format_module(
                    reframe_mode="auto",
                    format_strategy="blurred_background_fit_foreground",
                    face_track_attempted=True,
                    face_track_used=False,
                    face_track_eligibility_reason="long_no_face_gap",
                )
            ]
        }
        summary = extract_reframe_summary_from_metadata_payload(payload)
        assert summary["available"] is True
        assert summary["face_track_eligibility_reason"] == "long_no_face_gap"
