"""Shared reframing mode and strategy constants for platform_safe_format_v1."""

from __future__ import annotations

from typing import Any

REFRAME_MODE_BLUR_BACKGROUND = "blur_background"
REFRAME_MODE_AUTO = "auto"
REFRAME_MODE_FACE_TRACK = "face_track"

REFRAME_MODES = (
    REFRAME_MODE_BLUR_BACKGROUND,
    REFRAME_MODE_AUTO,
    REFRAME_MODE_FACE_TRACK,
)
DEFAULT_REFRAME_MODE = REFRAME_MODE_BLUR_BACKGROUND

DEFAULT_FACE_TRACK_TEST_ENABLED = False
FACE_TRACK_SKIP_REASON_TEST_DISABLED = "face_track_test_disabled"

FORMAT_STRATEGY_BLURRED_BACKGROUND = "blurred_background_fit_foreground"
FORMAT_STRATEGY_FACE_TRACK_CROP = "face_track_crop"

WARNING_FACE_TRACK_NOT_IMPLEMENTED = "face_track_not_implemented_using_blur_fallback"
FAIL_REASON_FACE_TRACK_NOT_IMPLEMENTED = "face_track_crop is not implemented yet"
FAIL_REASON_FACE_TRACK_PIPELINE_FAILED = "Face-track pipeline did not produce a usable smoothed crop path."
FAIL_REASON_FACE_TRACK_RENDER_FAILED = "Face-track crop render failed."
FAIL_REASON_FACE_TRACK_NOT_ELIGIBLE = "Clip is not eligible for face-track crop reframing."


def validate_reframe_mode(value: Any) -> str | None:
    """Return an error string when *value* is not a supported reframe mode."""
    if not isinstance(value, str) or value not in REFRAME_MODES:
        return f"invalid reframe_mode: {value!r}; expected one of {list(REFRAME_MODES)}"
    return None


def resolve_face_track_test_enabled(config: dict[str, Any]) -> bool:
    """Return whether face-track testing is enabled for ``auto`` reframe mode."""
    value = config.get("face_track_test_enabled", DEFAULT_FACE_TRACK_TEST_ENABLED)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return DEFAULT_FACE_TRACK_TEST_ENABLED


def resolve_reframe_plan(config: dict[str, Any]) -> dict[str, Any]:
    """Map ``reframe_mode`` to the execution plan for a format module run.

    Returns a dict with keys:
        reframe_mode, format_strategy, use_blur_fallback,
        reframe_attempted, warnings, fail_reason (or None).
    """
    mode = str(config.get("reframe_mode") or DEFAULT_REFRAME_MODE)
    test_enabled = resolve_face_track_test_enabled(config)

    if mode == REFRAME_MODE_FACE_TRACK:
        return {
            "reframe_mode": mode,
            "format_strategy": FORMAT_STRATEGY_FACE_TRACK_CROP,
            "use_blur_fallback": False,
            "attempt_face_pipeline": True,
            "reframe_attempted": False,
            "face_track_test_enabled": test_enabled,
            "face_track_skip_reason": None,
            "warnings": [],
            "fail_reason": None,
        }

    if mode == REFRAME_MODE_AUTO:
        if not test_enabled:
            return {
                "reframe_mode": mode,
                "format_strategy": FORMAT_STRATEGY_BLURRED_BACKGROUND,
                "use_blur_fallback": True,
                "attempt_face_pipeline": False,
                "reframe_attempted": False,
                "face_track_test_enabled": False,
                "face_track_skip_reason": FACE_TRACK_SKIP_REASON_TEST_DISABLED,
                "warnings": [],
                "fail_reason": None,
            }
        return {
            "reframe_mode": mode,
            "format_strategy": FORMAT_STRATEGY_BLURRED_BACKGROUND,
            "use_blur_fallback": True,
            "attempt_face_pipeline": True,
            "reframe_attempted": False,
            "face_track_test_enabled": True,
            "face_track_skip_reason": None,
            "warnings": [],
            "fail_reason": None,
        }

    return {
        "reframe_mode": REFRAME_MODE_BLUR_BACKGROUND,
        "format_strategy": FORMAT_STRATEGY_BLURRED_BACKGROUND,
        "use_blur_fallback": True,
        "attempt_face_pipeline": False,
        "reframe_attempted": False,
        "face_track_test_enabled": test_enabled,
        "face_track_skip_reason": None,
        "warnings": [],
        "fail_reason": None,
    }
