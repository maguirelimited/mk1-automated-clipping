"""Extract face-track / reframe fields from metadata_writer module results."""

from __future__ import annotations

from typing import Any

PLATFORM_SAFE_FORMAT_MODULE = "platform_safe_format_v1"

_REFRAME_FIELDS = (
    "reframe_mode",
    "format_strategy",
    "reframe_attempted",
    "face_track_test_enabled",
    "face_track_attempted",
    "face_track_used",
    "face_track_eligible",
    "face_track_eligibility_reason",
    "face_track_eligibility_fallback",
    "face_track_skip_reason",
    "face_coverage_pct",
    "longest_face_run_pct",
    "leading_no_face_gap_sec",
    "max_no_face_gap_sec",
    "layout_risk",
    "crop_x_range_pct_of_source_width",
    "segments_rendered",
    "segments_merged",
)


def _module_metadata(module: dict[str, Any]) -> dict[str, Any]:
    meta = module.get("metadata")
    return meta if isinstance(meta, dict) else {}


def find_platform_safe_format_module(
    module_results: list[Any] | None,
) -> dict[str, Any] | None:
    """Return the platform_safe_format_v1 module result dict, if present."""
    if not isinstance(module_results, list):
        return None
    for module in module_results:
        if not isinstance(module, dict):
            continue
        name = str(module.get("module_name") or module.get("module") or "").strip()
        if name == PLATFORM_SAFE_FORMAT_MODULE:
            return module
    return None


def extract_reframe_summary(
    module_results: list[Any] | None,
) -> dict[str, Any]:
    """Build a reframe summary dict from metadata_writer module_results."""
    module = find_platform_safe_format_module(module_results)
    if module is None:
        return {"available": False}

    meta = _module_metadata(module)
    if not meta:
        return {"available": False}

    summary: dict[str, Any] = {"available": True, "module_status": module.get("status")}
    for key in _REFRAME_FIELDS:
        if key in meta:
            summary[key] = meta[key]
    return summary


def extract_reframe_summary_from_metadata_payload(
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Extract reframe summary from a metadata_writer JSON payload."""
    if not isinstance(payload, dict):
        return {"available": False}
    modules = payload.get("module_results") or payload.get("modules_applied")
    return extract_reframe_summary(modules if isinstance(modules, list) else None)
