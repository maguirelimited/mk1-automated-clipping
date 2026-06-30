"""post_processing_report_v1 — MK1 job-level post-processing report writer.

Aggregates what happened during a full post-processing run into one
deterministic ``post_processing_report.json`` file.

This module deliberately does NOT:
- register output funnels
- upload or schedule clips
- write per-clip metadata (that is metadata_writer_v1's responsibility)
- call AI/LLM services
- generate titles, descriptions, hashtags, or platform metadata
- perform database writes or analytics tracking
- run or re-run the universal conveyor
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPORT_SCHEMA_VERSION = "post_processing_report_v1"
POST_PROCESSING_VERSION = "post_processing_mk1_v1"

FIXED_MK1_MODULE_ORDER = [
    "render_clip_v1",
    "platform_safe_format_v1",
    "intelligent_captions_v1",
    "validation_v1",
    "metadata_writer_v1",
]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_post_processing_report(
    *,
    job_id: str,
    selection_result: dict[str, Any] | None = None,
    conveyor_result: dict[str, Any] | None = None,
    clip_results: list[dict[str, Any]] | None = None,
    raw_candidate_pool: dict[str, Any] | None = None,
    raw_candidate_pool_path: str | None = None,
    source_video_path: str | None = None,
    selection_result_path: str | None = None,
    report_path: str | None = None,
    warnings: list[str] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the job-level post-processing report dict.

    All inputs are optional (except ``job_id``) so the report can be
    constructed incrementally as stages complete.

    Args:
        job_id: Required unique job identifier.
        selection_result: Structured result from selection_gate_v1.
        conveyor_result: Structured result from run_fixed_mk1_universal_conveyor.
        clip_results: Direct list of per-clip result dicts (alternative to
            conveyor_result).
        raw_candidate_pool: Loaded raw candidate pool dict.
        raw_candidate_pool_path: Path to raw_candidate_pool.json.
        source_video_path: Path to the source video file.
        selection_result_path: Path to selection result JSON if written.
        report_path: Path where this report will be/was written.
        warnings: Additional job-level warnings to include.
        diagnostics: Arbitrary diagnostic metadata.

    Returns:
        JSON-serialisable report dict.
    """
    extra_warnings: list[str] = list(warnings or [])
    selection_result = selection_result or {}
    diagnostics = dict(diagnostics or {})

    # ------------------------------------------------------------------
    # Resolve source_video_path
    # ------------------------------------------------------------------
    if not source_video_path:
        source_video_path = (
            str(selection_result.get("source_video_path") or "")
            or str((raw_candidate_pool or {}).get("source_video_path") or "")
        ) or None

    # ------------------------------------------------------------------
    # Resolve flat clip_results list
    # ------------------------------------------------------------------
    effective_clip_results = _resolve_clip_results(conveyor_result, clip_results)

    # ------------------------------------------------------------------
    # Counts — raw candidates
    # ------------------------------------------------------------------
    raw_candidates_received = _count_raw_candidates(raw_candidate_pool, selection_result)

    # ------------------------------------------------------------------
    # Counts — selection
    # ------------------------------------------------------------------
    (
        candidates_selected,
        candidates_rejected,
        reserve_count,
        selected_list,
        rejected_list,
        reserve_list,
        sel_warnings,
    ) = _resolve_selection_counts(selection_result)
    extra_warnings.extend(sel_warnings)

    selection_mode = _safe_str(
        selection_result.get("selection_mode")
        or (selection_result.get("selection_summary") or {}).get("selection_mode")
    )

    # ------------------------------------------------------------------
    # Counts — clips
    # ------------------------------------------------------------------
    clips_attempted = len(effective_clip_results)
    clips_rendered = 0
    clips_passed = 0
    clips_failed = 0

    finished_clip_paths: list[str] = []
    per_clip_metadata_paths: list[str] = []
    failed_clips_list: list[dict[str, Any]] = []
    failed_modules_set: list[dict[str, Any]] = []
    seen_failed_module_keys: set[str] = set()
    modules_seen: set[str] = set()

    normalised_clips: list[dict[str, Any]] = []

    for cr in effective_clip_results:
        clip_entry = _normalise_clip_result(cr)
        normalised_clips.append(clip_entry)

        clip_id = clip_entry["clip_id"]
        candidate_id = clip_entry["source_candidate_id"]
        module_results = clip_entry.get("module_results") or []

        # Collect modules seen
        for mr in module_results:
            if isinstance(mr, dict) and mr.get("module_name"):
                modules_seen.add(str(mr["module_name"]))

        # Count rendered (render_clip_v1 PASS)
        if _module_passed(module_results, "render_clip_v1"):
            clips_rendered += 1

        # Validation outcome
        val_passed = _module_passed(module_results, "validation_v1")
        val_failed = _module_failed(module_results, "validation_v1")
        clip_status_fail = clip_entry.get("status") == "FAIL"

        if val_passed:
            clips_passed += 1
        elif val_failed or clip_status_fail:
            clips_failed += 1

        # Finished clip path (passed only)
        if val_passed:
            fcp = _resolve_finished_clip_path(clip_entry, module_results)
            if fcp:
                finished_clip_paths.append(fcp)

        # Metadata path (both passed and failed)
        mpath = _resolve_metadata_path(clip_entry, module_results)
        if mpath:
            per_clip_metadata_paths.append(mpath)

        # Failed clips list
        if val_failed or clip_status_fail:
            failed_entry = _build_failed_clip_entry(clip_entry, module_results)
            failed_clips_list.append(failed_entry)

        # Failed modules — deduplicated
        for mr in module_results:
            if not isinstance(mr, dict):
                continue
            if mr.get("status") == "FAIL":
                mod_name = str(mr.get("module_name") or "")
                key = f"{clip_id}::{mod_name}"
                if key not in seen_failed_module_keys:
                    seen_failed_module_keys.add(key)
                    failed_modules_set.append({
                        "clip_id": clip_id,
                        "source_candidate_id": candidate_id,
                        "module_name": mod_name,
                        "error_reason": str(mr.get("error_reason") or ""),
                    })

    # ------------------------------------------------------------------
    # Modules run — preserve fixed MK1 order, include any extras
    # ------------------------------------------------------------------
    modules_run = _ordered_modules_run(modules_seen)

    # ------------------------------------------------------------------
    # Rejected/reserve candidates
    # ------------------------------------------------------------------
    normalised_rejected = [_normalise_rejected(r) for r in rejected_list]
    normalised_reserve = [_normalise_reserve(r) for r in reserve_list]

    # ------------------------------------------------------------------
    # Collect warnings from all clip results
    # ------------------------------------------------------------------
    for cr in effective_clip_results:
        for w in (cr.get("warnings") or []):
            if w and w not in extra_warnings:
                extra_warnings.append(str(w))

    now = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "post_processing_version": POST_PROCESSING_VERSION,
        "job_id": str(job_id),
        "created_at": now,
        # Paths
        "source_video_path": source_video_path,
        "raw_candidate_pool_path": raw_candidate_pool_path,
        "selection_result_path": selection_result_path,
        "post_processing_report_path": report_path,
        # Selection provenance
        "selection_mode": selection_mode,
        # Candidate counts
        "raw_candidates_received": raw_candidates_received,
        "candidates_selected": candidates_selected,
        "reserve_candidates": reserve_count,
        "candidates_rejected": candidates_rejected,
        # Clip counts
        "clips_attempted": clips_attempted,
        "clips_rendered": clips_rendered,
        "clips_passed": clips_passed,
        "clips_failed": clips_failed,
        # Module aggregation
        "modules_run": modules_run,
        "failed_modules": failed_modules_set,
        # Paths
        "finished_clip_paths": finished_clip_paths,
        "per_clip_metadata_paths": per_clip_metadata_paths,
        # Detailed lists
        "clips": normalised_clips,
        "failed_clips": failed_clips_list,
        "rejected_candidates": normalised_rejected,
        "reserve_candidates_list": normalised_reserve,
        # Job-level metadata
        "warnings": extra_warnings,
        "diagnostics": diagnostics,
    }


def write_post_processing_report(
    report: dict[str, Any],
    report_path: str | Path,
    *,
    allow_overwrite: bool = True,
    indent: int = 2,
    sort_keys: bool = True,
) -> dict[str, Any]:
    """Write the report dict to disk as JSON.

    Args:
        report: The report dict from :func:`build_post_processing_report`.
        report_path: Destination path for the JSON file.
        allow_overwrite: If False and the file already exists, raise
            ``FileExistsError`` with code ``report_file_exists``.
        indent: JSON pretty-print indent level.
        sort_keys: Sort JSON keys alphabetically.

    Returns:
        Dict with ``report_path``, ``file_size_bytes``, ``schema_version``.

    Raises:
        ValueError: For invalid report or path.
        FileExistsError: When ``allow_overwrite=False`` and the file exists.
        OSError: For directory creation or write failures.
    """
    report_path = str(report_path)
    if not report_path.strip():
        raise ValueError("report_path is empty")

    if os.path.exists(report_path) and not allow_overwrite:
        err = FileExistsError(f"report file already exists: {report_path}")
        err.args = ("report_file_exists", str(report_path))  # type: ignore[assignment]
        raise FileExistsError(f"[report_file_exists] report file already exists: {report_path}")

    parent = os.path.dirname(os.path.abspath(report_path))
    os.makedirs(parent, exist_ok=True)

    try:
        json_str = json.dumps(report, indent=indent, sort_keys=sort_keys, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"[report_json_invalid] report could not be serialised: {exc}") from exc

    try:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(json_str)
    except OSError as exc:
        raise OSError(f"[report_write_failed] could not write report: {exc}") from exc

    # Readback validation
    try:
        with open(report_path, "r", encoding="utf-8") as fh:
            readback = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"[report_readback_failed] written file is not valid JSON: {exc}") from exc

    if not isinstance(readback, dict):
        raise ValueError(
            f"[report_json_not_object] readback is {type(readback).__name__}, expected dict"
        )

    file_size = os.path.getsize(report_path)

    return {
        "report_path": report_path,
        "file_size_bytes": file_size,
        "schema_version": REPORT_SCHEMA_VERSION,
    }


def load_post_processing_report(report_path: str | Path) -> dict[str, Any]:
    """Load and return a post-processing report from disk.

    Raises:
        FileNotFoundError: If the path does not exist.
        ValueError: If the file is not valid JSON or not a dict.
    """
    report_path = str(report_path)
    if not os.path.isfile(report_path):
        raise FileNotFoundError(f"report not found: {report_path}")
    try:
        with open(report_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"report file is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"report file is not a JSON object")
    return data


def validate_post_processing_report(report: Any) -> list[str]:
    """Validate a post-processing report dict.

    Returns a list of error strings.  An empty list means the report is valid.
    """
    errors: list[str] = []

    if not isinstance(report, dict):
        return [f"invalid_report_object: expected dict, got {type(report).__name__}"]

    sv = report.get("schema_version")
    if sv != REPORT_SCHEMA_VERSION:
        errors.append(
            f"invalid_schema_version: expected {REPORT_SCHEMA_VERSION!r}, got {sv!r}"
        )

    if not report.get("job_id") or not str(report["job_id"]).strip():
        errors.append("missing_job_id: job_id is missing or empty")

    count_fields = [
        "raw_candidates_received",
        "candidates_selected",
        "reserve_candidates",
        "candidates_rejected",
        "clips_attempted",
        "clips_rendered",
        "clips_passed",
        "clips_failed",
    ]
    for field in count_fields:
        val = report.get(field)
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            errors.append(
                f"invalid_count_field: {field!r} must be a non-negative integer, got {val!r}"
            )

    list_fields = [
        ("modules_run", "invalid_modules_run"),
        ("finished_clip_paths", "invalid_finished_clip_paths"),
        ("failed_clips", "invalid_failed_clips"),
        ("warnings", "invalid_warnings"),
        ("clips", "invalid_list_field"),
        ("failed_modules", "invalid_list_field"),
        ("per_clip_metadata_paths", "invalid_list_field"),
        ("rejected_candidates", "invalid_list_field"),
        ("reserve_candidates_list", "invalid_list_field"),
    ]
    for field, code in list_fields:
        val = report.get(field)
        if not isinstance(val, list):
            errors.append(f"{code}: {field!r} must be a list, got {type(val).__name__!r}")

    return errors


# ---------------------------------------------------------------------------
# Private helpers — clip results resolution
# ---------------------------------------------------------------------------


def _resolve_clip_results(
    conveyor_result: dict[str, Any] | None,
    clip_results: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return a flat list of per-clip result dicts."""
    if clip_results is not None:
        return list(clip_results)
    if conveyor_result and isinstance(conveyor_result.get("clip_results"), list):
        return list(conveyor_result["clip_results"])
    return []


def _normalise_clip_result(cr: dict[str, Any]) -> dict[str, Any]:
    """Return a normalised clip result entry for the report."""
    clip_id = str(cr.get("clip_id") or "")
    candidate_id = str(cr.get("source_candidate_id") or "")
    status = str(cr.get("status") or "")

    # Try to find output_file_path
    output_file_path = (
        _safe_str(cr.get("output_file_path"))
        or _safe_str(cr.get("final_output_path"))
    )

    # Try to find metadata_path
    metadata_path = _safe_str(cr.get("metadata_path"))
    if not metadata_path:
        module_results = cr.get("module_results") or []
        metadata_path = _resolve_metadata_path(cr, module_results)

    # Validation result from clip result if available
    validation_result = _safe_str(cr.get("validation_result"))
    if not validation_result:
        module_results = cr.get("module_results") or []
        vr_entry = next(
            (m for m in module_results if isinstance(m, dict) and m.get("module_name") == "validation_v1"),
            None,
        )
        if vr_entry:
            validation_result = str(vr_entry.get("status") or "")

    return {
        "clip_id": clip_id,
        "source_candidate_id": candidate_id,
        "status": status,
        "output_file_path": output_file_path,
        "metadata_path": metadata_path,
        "validation_result": validation_result,
        "failed_module": _safe_str(cr.get("failed_module")),
        "failure_reason": _safe_str(cr.get("failure_reason")),
        "module_results": _deep_json_safe(cr.get("module_results") or []),
        "warnings": list(cr.get("warnings") or []),
    }


def _build_failed_clip_entry(
    clip_entry: dict[str, Any],
    module_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a failed clip summary for the report's failed_clips list."""
    return {
        "clip_id": clip_entry["clip_id"],
        "source_candidate_id": clip_entry["source_candidate_id"],
        "output_file_path": clip_entry.get("output_file_path"),
        "metadata_path": clip_entry.get("metadata_path"),
        "failed_module": clip_entry.get("failed_module"),
        "failure_reason": clip_entry.get("failure_reason"),
        "module_results": _deep_json_safe(module_results),
        "warnings": list(clip_entry.get("warnings") or []),
    }


# ---------------------------------------------------------------------------
# Private helpers — counting
# ---------------------------------------------------------------------------


def _count_raw_candidates(
    raw_pool: dict[str, Any] | None,
    selection_result: dict[str, Any],
) -> int:
    if raw_pool and isinstance(raw_pool.get("candidates"), list):
        return len(raw_pool["candidates"])
    summary = selection_result.get("selection_summary") or {}
    raw = summary.get("raw_candidates_received")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return 0


def _resolve_selection_counts(
    selection_result: dict[str, Any],
) -> tuple[int, int, int, list, list, list, list[str]]:
    """Return (selected, rejected, reserve, sel_list, rej_list, res_list, warnings)."""
    warnings: list[str] = []

    selected_list: list = list(selection_result.get("selected_candidates") or [])
    rejected_list: list = list(selection_result.get("rejected_candidates") or [])
    reserve_list: list = list(selection_result.get("reserve_candidates") or [])

    summary = selection_result.get("selection_summary") or {}

    # Prefer actual list lengths; fall back to summary
    candidates_selected = len(selected_list) if selected_list else _safe_int(summary.get("selected_count"), 0)
    candidates_rejected = len(rejected_list) if rejected_list else _safe_int(summary.get("rejected_count"), 0)
    reserve_count = len(reserve_list) if reserve_list else _safe_int(summary.get("reserve_count"), 0)

    # Check for mismatches only when summary AND list both exist
    for field, list_count, summary_key in [
        ("selected", len(selected_list), "selected_count"),
        ("rejected", len(rejected_list), "rejected_count"),
        ("reserve", len(reserve_list), "reserve_count"),
    ]:
        summary_val = summary.get(summary_key)
        if (
            selected_list or rejected_list or reserve_list  # lists are present
            and summary_val is not None
            and isinstance(summary_val, int)
            and summary_val != list_count
        ):
            warnings.append("selection_count_mismatch")
            break

    return (
        candidates_selected,
        candidates_rejected,
        reserve_count,
        selected_list,
        rejected_list,
        reserve_list,
        warnings,
    )


# ---------------------------------------------------------------------------
# Private helpers — path resolution
# ---------------------------------------------------------------------------


def _resolve_finished_clip_path(
    clip_entry: dict[str, Any],
    module_results: list[dict[str, Any]],
) -> str | None:
    """Return the final output clip path for a passed clip."""
    # 1. metadata_writer_v1 output_path
    mw = _find_module_result(module_results, "metadata_writer_v1")
    if mw:
        p = _safe_str(mw.get("output_path"))
        if p:
            return p
        p = _safe_str((mw.get("metadata") or {}).get("output_file_path"))
        if p:
            return p

    # 2. validation_v1 output_path
    vr = _find_module_result(module_results, "validation_v1")
    if vr:
        p = _safe_str(vr.get("output_path"))
        if p:
            return p

    # 3. normalised clip entry
    p = _safe_str(clip_entry.get("output_file_path"))
    if p:
        return p

    # 4. final_output_path from conveyor
    p = _safe_str(clip_entry.get("final_output_path"))
    return p


def _resolve_metadata_path(
    clip_entry: dict[str, Any],
    module_results: list[dict[str, Any]],
) -> str | None:
    """Return the metadata JSON path for this clip if available."""
    # 1. metadata_writer_v1 module result metadata
    mw = _find_module_result(module_results, "metadata_writer_v1")
    if mw:
        p = _safe_str((mw.get("metadata") or {}).get("metadata_path"))
        if p:
            return p

    # 2. clip entry direct
    p = _safe_str(clip_entry.get("metadata_path"))
    return p


def _find_module_result(
    module_results: list[dict[str, Any]],
    name: str,
) -> dict[str, Any] | None:
    for mr in module_results:
        if isinstance(mr, dict) and mr.get("module_name") == name:
            return mr
    return None


def _module_passed(module_results: list[dict[str, Any]], name: str) -> bool:
    mr = _find_module_result(module_results, name)
    return mr is not None and mr.get("status") == "PASS"


def _module_failed(module_results: list[dict[str, Any]], name: str) -> bool:
    mr = _find_module_result(module_results, name)
    return mr is not None and mr.get("status") == "FAIL"


# ---------------------------------------------------------------------------
# Private helpers — candidate normalisation
# ---------------------------------------------------------------------------


def _normalise_rejected(r: Any) -> dict[str, Any]:
    if not isinstance(r, dict):
        return {"candidate_id": str(r), "reason": None, "score": None, "metadata": {}}
    return {
        "candidate_id": _safe_str(r.get("candidate_id")),
        "reason": _safe_str(r.get("reason") or r.get("rejection_reason")),
        "score": r.get("score") or r.get("overall_score"),
        "metadata": _safe_dict(r),
    }


def _normalise_reserve(r: Any) -> dict[str, Any]:
    if not isinstance(r, dict):
        return {"candidate_id": str(r), "rank": None, "reason": None, "metadata": {}}
    return {
        "candidate_id": _safe_str(r.get("candidate_id")),
        "rank": r.get("rank"),
        "reason": _safe_str(r.get("reason") or r.get("reserve_reason")),
        "metadata": _safe_dict(r),
    }


# ---------------------------------------------------------------------------
# Private helpers — module ordering
# ---------------------------------------------------------------------------


def _ordered_modules_run(modules_seen: set[str]) -> list[str]:
    """Return modules in fixed MK1 order, with any unexpected modules appended."""
    ordered: list[str] = []
    for name in FIXED_MK1_MODULE_ORDER:
        if name in modules_seen:
            ordered.append(name)
    # Append any extra modules not in the fixed list
    for name in sorted(modules_seen):
        if name not in ordered:
            ordered.append(name)
    return ordered


# ---------------------------------------------------------------------------
# Private type helpers
# ---------------------------------------------------------------------------


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value >= 0:
        return value
    return default


def _safe_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return _deep_json_safe(value)
    return {}


def _deep_json_safe(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _deep_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_deep_json_safe(v) for v in obj]
    return str(obj)
