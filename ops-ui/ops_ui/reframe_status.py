"""Classify face-track / reframe outcomes for Ops UI display."""

from __future__ import annotations

from typing import Any

FORMAT_STRATEGY_FACE_TRACK = "face_track_crop"
FORMAT_STRATEGY_BLUR = "blurred_background_fit_foreground"

NORMAL_FALLBACK_REASONS = frozenset(
    {
        "eligible",
        "face_track_test_disabled",
        "leading_no_face_gap",
        "insufficient_face_coverage",
        "long_no_face_gap",
        "insufficient_sustained_face_run_pct",
    }
)

_STATUS_LABELS = {
    "face_track_used": "Used face-track",
    "blur_fallback": "Blur fallback",
    "blur_default": "Blur default",
    "disabled": "Disabled",
    "failed": "Failed",
    "unknown": "Unknown",
}

_BADGE_LABELS = {
    "face_track_used": "Face-track",
    "blur_fallback": "Fallback",
    "blur_default": "Blur",
    "disabled": "Disabled",
    "failed": "Failed",
    "unknown": "Unknown",
}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def check_reframe_consistency(summary: dict[str, Any]) -> list[str]:
    """Return human-readable warnings for impossible metadata combinations."""
    if not summary.get("available"):
        return []

    warnings: list[str] = []
    face_track_used = _truthy(summary.get("face_track_used"))
    face_track_eligible = summary.get("face_track_eligible")
    face_track_attempted = _truthy(summary.get("face_track_attempted"))
    face_track_test_enabled = summary.get("face_track_test_enabled")
    format_strategy = str(summary.get("format_strategy") or "")
    reframe_mode = str(summary.get("reframe_mode") or "")

    if face_track_used and face_track_eligible is False:
        warnings.append("face_track_used is true but face_track_eligible is false")
    if face_track_used and format_strategy and format_strategy != FORMAT_STRATEGY_FACE_TRACK:
        warnings.append(
            f"face_track_used is true but format_strategy is {format_strategy!r}"
        )
    if format_strategy == FORMAT_STRATEGY_FACE_TRACK and not face_track_used:
        warnings.append("format_strategy is face_track_crop but face_track_used is false")
    if (
        reframe_mode == "auto"
        and face_track_test_enabled is False
        and face_track_attempted
    ):
        warnings.append(
            "face_track_attempted is true while face_track_test_enabled is false"
        )
    if not format_strategy:
        warnings.append("format_strategy is missing from platform_safe_format metadata")

    return warnings


def classify_reframe_status(summary: dict[str, Any]) -> dict[str, Any]:
    """Classify reframe outcome for badges and detail pages."""
    if not summary.get("available"):
        return {
            "status": "unknown",
            "label": _STATUS_LABELS["unknown"],
            "badge": _BADGE_LABELS["unknown"],
            "reason": None,
            "severity": "muted",
            "tone": "muted",
            "needs_attention": False,
        }

    reframe_mode = str(summary.get("reframe_mode") or "")
    format_strategy = str(summary.get("format_strategy") or "")
    module_status = str(summary.get("module_status") or "").upper()
    face_track_used = _truthy(summary.get("face_track_used"))
    face_track_attempted = _truthy(summary.get("face_track_attempted"))
    face_track_test_enabled = summary.get("face_track_test_enabled")
    skip_reason = str(summary.get("face_track_skip_reason") or "").strip() or None
    eligibility_reason = (
        str(summary.get("face_track_eligibility_reason") or "").strip() or None
    )
    inconsistencies = check_reframe_consistency(summary)

    reason = eligibility_reason or skip_reason
    needs_attention = bool(inconsistencies)

    if inconsistencies:
        status = "failed"
        label = _STATUS_LABELS["failed"]
        badge = _BADGE_LABELS["failed"]
        reason = inconsistencies[0]
        severity = "error"
        tone = "bad"
    elif reframe_mode == "face_track" and module_status == "FAIL":
        status = "failed"
        label = _STATUS_LABELS["failed"]
        badge = _BADGE_LABELS["failed"]
        reason = reason or "face_track_mode_failed"
        severity = "error"
        tone = "bad"
        needs_attention = True
    elif face_track_used:
        status = "face_track_used"
        label = _STATUS_LABELS["face_track_used"]
        badge = _BADGE_LABELS["face_track_used"]
        reason = reason or "eligible"
        severity = "ok"
        tone = "ok"
    elif reframe_mode == "blur_background":
        status = "blur_default"
        label = _STATUS_LABELS["blur_default"]
        badge = _BADGE_LABELS["blur_default"]
        reason = skip_reason
        severity = "ok"
        tone = "muted"
    elif skip_reason == "face_track_test_disabled" or (
        reframe_mode == "auto"
        and face_track_test_enabled is False
        and not face_track_attempted
    ):
        status = "disabled"
        label = _STATUS_LABELS["disabled"]
        badge = _BADGE_LABELS["disabled"]
        reason = skip_reason or "face_track_test_disabled"
        severity = "info"
        tone = "muted"
    elif face_track_attempted and not face_track_used:
        status = "blur_fallback"
        label = _STATUS_LABELS["blur_fallback"]
        badge = _BADGE_LABELS["blur_fallback"]
        reason = reason or "face_track_not_eligible"
        severity = "info"
        tone = "muted"
    elif format_strategy == FORMAT_STRATEGY_BLUR:
        status = "blur_default"
        label = _STATUS_LABELS["blur_default"]
        badge = _BADGE_LABELS["blur_default"]
        reason = skip_reason
        severity = "ok"
        tone = "muted"
    else:
        status = "unknown"
        label = _STATUS_LABELS["unknown"]
        badge = _BADGE_LABELS["unknown"]
        severity = "muted"
        tone = "muted"

    return {
        "status": status,
        "label": label,
        "badge": badge,
        "reason": reason,
        "severity": severity,
        "tone": tone,
        "needs_attention": needs_attention,
        "is_normal_fallback": status == "blur_fallback"
        and (reason in NORMAL_FALLBACK_REASONS or reason is None),
    }


def build_reframe_display(summary: dict[str, Any]) -> dict[str, Any]:
    """Merge raw summary, classification, and consistency warnings."""
    classified = classify_reframe_status(summary)
    inconsistencies = check_reframe_consistency(summary)
    detail_lines = _detail_lines(summary, classified)
    metrics = _metric_lines(summary)
    return {
        **summary,
        **classified,
        "consistency_warnings": inconsistencies,
        "detail_lines": detail_lines,
        "metrics": metrics,
        "detail_title": _detail_title(classified),
    }


def aggregate_reframe_counts(displays: list[dict[str, Any]]) -> dict[str, int]:
    """Summarize reframe outcomes across multiple clips."""
    counts = {
        "available": 0,
        "face_track_used": 0,
        "blur_fallback": 0,
        "blur_default": 0,
        "disabled": 0,
        "failed": 0,
        "unknown": 0,
    }
    for display in displays:
        if not display.get("available"):
            counts["unknown"] += 1
            continue
        counts["available"] += 1
        status = str(display.get("status") or "unknown")
        if status in counts:
            counts[status] += 1
        else:
            counts["unknown"] += 1
    return counts


def format_reframe_aggregate_summary(counts: dict[str, int]) -> str | None:
    """One-line operator summary, e.g. '3 face-track, 23 blur fallback'."""
    if counts.get("available", 0) <= 0:
        return None
    parts: list[str] = []
    if counts.get("face_track_used"):
        n = counts["face_track_used"]
        parts.append(f"{n} face-track")
    fallback = counts.get("blur_fallback", 0) + counts.get("blur_default", 0)
    if fallback:
        parts.append(f"{fallback} blur fallback")
    if counts.get("disabled"):
        parts.append(f"{counts['disabled']} disabled")
    if counts.get("failed"):
        parts.append(f"{counts['failed']} failed")
    if not parts:
        return None
    return ", ".join(parts)


def _detail_title(classified: dict[str, Any]) -> str:
    status = classified.get("status")
    reason = classified.get("reason")
    if status == "face_track_used":
        return "Face-track: Used"
    if status == "blur_fallback" and reason:
        return f"Face-track: Fallback — {reason}"
    if status == "disabled" and reason:
        return f"Face-track: Disabled — {reason}"
    if status == "failed" and reason:
        return f"Face-track: Failed — {reason}"
    return str(classified.get("label") or "Reframing")


def _detail_lines(summary: dict[str, Any], classified: dict[str, Any]) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    reframe_mode = summary.get("reframe_mode")
    if reframe_mode:
        lines.append(("Mode", str(reframe_mode)))

    test_enabled = summary.get("face_track_test_enabled")
    if test_enabled is not None:
        lines.append(("Test mode", "on" if _truthy(test_enabled) else "off"))

    strategy = summary.get("format_strategy")
    if strategy:
        lines.append(("Strategy used", str(strategy)))

    if summary.get("face_track_attempted") is not None:
        lines.append(
            (
                "Face-track attempted",
                "yes" if _truthy(summary.get("face_track_attempted")) else "no",
            )
        )

    eligible = summary.get("face_track_eligible")
    if eligible is not None:
        lines.append(("Eligibility", "eligible" if _truthy(eligible) else "not eligible"))

    reason = classified.get("reason")
    if reason:
        label = "Reason" if classified.get("status") != "disabled" else "Skip reason"
        lines.append((label, reason))

    fallback = summary.get("face_track_eligibility_fallback")
    if fallback and classified.get("status") == "blur_fallback":
        lines.append(("Fallback", str(fallback)))

    return lines


def _metric_lines(summary: dict[str, Any]) -> list[tuple[str, str]]:
    """Key eligibility metrics for detail view (compact subset)."""
    specs = (
        ("face_coverage_pct", "{:.1f}%"),
        ("longest_face_run_pct", "{:.1f}%"),
        ("leading_no_face_gap_sec", "{:.1f}s"),
        ("max_no_face_gap_sec", "{:.1f}s"),
        ("layout_risk", "{}"),
        ("crop_x_range_pct_of_source_width", "{:.1f}%"),
        ("segments_rendered", "{}"),
        ("segments_merged", "{}"),
    )
    lines: list[tuple[str, str]] = []
    for key, fmt in specs:
        value = summary.get(key)
        if value is None:
            continue
        label = key.replace("_", " ")
        if key == "layout_risk":
            lines.append((label, "yes" if _truthy(value) else "no"))
        elif isinstance(value, float):
            lines.append((label, fmt.format(value)))
        else:
            lines.append((label, str(value)))
    return lines
