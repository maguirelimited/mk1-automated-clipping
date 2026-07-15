"""Tests for face-track / reframe Ops UI classification."""

from __future__ import annotations

from ops_ui.reframe_status import (
    build_reframe_display,
    check_reframe_consistency,
    classify_reframe_status,
)


def _summary(**overrides):
    base = {
        "available": True,
        "reframe_mode": "auto",
        "format_strategy": "blurred_background_fit_foreground",
        "face_track_test_enabled": True,
        "face_track_attempted": True,
        "face_track_used": False,
        "face_track_eligible": False,
        "module_status": "PASS",
    }
    base.update(overrides)
    return base


class TestClassifyReframeStatus:
    def test_face_track_used(self) -> None:
        result = classify_reframe_status(
            _summary(
                face_track_used=True,
                face_track_eligible=True,
                format_strategy="face_track_crop",
                face_track_eligibility_reason="eligible",
            )
        )
        assert result["status"] == "face_track_used"
        assert result["badge"] == "Face-track"
        assert result["tone"] == "ok"
        assert result["needs_attention"] is False

    def test_blur_fallback_with_reason(self) -> None:
        result = classify_reframe_status(
            _summary(face_track_eligibility_reason="leading_no_face_gap")
        )
        assert result["status"] == "blur_fallback"
        assert result["badge"] == "Fallback"
        assert result["reason"] == "leading_no_face_gap"
        assert result["is_normal_fallback"] is True

    def test_disabled_test_mode(self) -> None:
        result = classify_reframe_status(
            _summary(
                face_track_test_enabled=False,
                face_track_attempted=False,
                face_track_skip_reason="face_track_test_disabled",
            )
        )
        assert result["status"] == "disabled"
        assert result["badge"] == "Disabled"
        assert result["reason"] == "face_track_test_disabled"

    def test_blur_default_production_mode(self) -> None:
        result = classify_reframe_status(
            _summary(
                reframe_mode="blur_background",
                face_track_attempted=False,
                face_track_test_enabled=False,
            )
        )
        assert result["status"] == "blur_default"
        assert result["badge"] == "Blur"

    def test_strict_face_track_failure(self) -> None:
        result = classify_reframe_status(
            _summary(
                reframe_mode="face_track",
                module_status="FAIL",
                face_track_eligibility_reason="insufficient_face_coverage",
            )
        )
        assert result["status"] == "failed"
        assert result["badge"] == "Failed"
        assert result["needs_attention"] is True

    def test_unknown_without_metadata(self) -> None:
        result = classify_reframe_status({"available": False})
        assert result["status"] == "unknown"
        assert result["badge"] == "Unknown"


class TestReframeConsistency:
    def test_impossible_used_without_eligible(self) -> None:
        warnings = check_reframe_consistency(
            _summary(
                face_track_used=True,
                face_track_eligible=False,
                format_strategy="face_track_crop",
            )
        )
        assert any("face_track_eligible is false" in warning for warning in warnings)

    def test_impossible_strategy_mismatch(self) -> None:
        warnings = check_reframe_consistency(
            _summary(
                face_track_used=True,
                face_track_eligible=True,
                format_strategy="blurred_background_fit_foreground",
            )
        )
        assert any("format_strategy" in warning for warning in warnings)

    def test_normal_fallback_not_flagged_as_inconsistent(self) -> None:
        warnings = check_reframe_consistency(
            _summary(face_track_eligibility_reason="long_no_face_gap")
        )
        assert warnings == []


class TestBuildReframeDisplay:
    def test_detail_lines_for_fallback(self) -> None:
        display = build_reframe_display(
            _summary(face_track_eligibility_reason="insufficient_face_coverage")
        )
        assert display["detail_title"].startswith("Face-track: Fallback")
        labels = [line[0] for line in display["detail_lines"]]
        assert "Mode" in labels
        assert "Reason" in labels

    def test_metrics_included_when_present(self) -> None:
        display = build_reframe_display(
            _summary(
                face_track_used=True,
                face_track_eligible=True,
                format_strategy="face_track_crop",
                face_coverage_pct=95.7,
                segments_rendered=27,
            )
        )
        metric_labels = [line[0] for line in display["metrics"]]
        assert "face coverage pct" in metric_labels
        assert "segments rendered" in metric_labels
