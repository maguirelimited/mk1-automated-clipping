"""Fixed MK1 Universal Conveyor — Post-Processing orchestration.

Defines and enforces the final MK1 conveyor order:

    Selected Candidate
        ↓
    render_clip_v1
        ↓
    platform_safe_format_v1
        ↓
    intelligent_captions_v1
        ↓
    validation_v1
        ↓
    metadata_writer_v1

Every selected candidate goes through the same required module chain in this
exact order.  Module order is stable.  All five modules are required.  No
optional modules exist in MK1.  No config-based reordering is supported.

This module is orchestration only.  It does NOT implement:
- actual render_clip_v1 (ffmpeg clipping)
- actual platform_safe_format_v1 (9:16 crop/pad/scale)
- actual intelligent_captions_v1 (transcript caption generation)
- actual validation_v1 (ffprobe/playability checks)
- actual metadata_writer_v1 (per-clip metadata files)
- post_processing_report.json writing
- output-funnel registration
- AI/LLM calls
"""

from __future__ import annotations

import copy
import json
from typing import Any

from post_processing_modules import (
    CHAIN_STATUS_PASS,
    PostProcessingModule,
    make_module_context,
    run_module_chain,
)

# ---------------------------------------------------------------------------
# Schema / version constants
# ---------------------------------------------------------------------------

CONVEYOR_SCHEMA_VERSION = "fixed_mk1_universal_conveyor_result_v1"
POST_PROCESSING_VERSION = "post_processing_mk1_v1"

# ---------------------------------------------------------------------------
# Fixed MK1 conveyor module order — single source of truth
# ---------------------------------------------------------------------------

FIXED_MK1_CONVEYOR_MODULES = [
    "render_clip_v1",
    "platform_safe_format_v1",
    "intelligent_captions_v1",
    "validation_v1",
    "metadata_writer_v1",
]

# ---------------------------------------------------------------------------
# Conveyor-level status constants
# ---------------------------------------------------------------------------

CONVEYOR_STATUS_COMPLETE = "CONVEYOR_COMPLETE"
CONVEYOR_STATUS_FAILED = "CONVEYOR_FAILED"

# ---------------------------------------------------------------------------
# Per-clip status constants
# ---------------------------------------------------------------------------

CLIP_STATUS_PASS = "PASS"
CLIP_STATUS_FAIL = "FAIL"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_fixed_mk1_universal_conveyor(
    selection_result: Any,
    *,
    source_video_path: str,
    job_metadata: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    directories: dict[str, str] | None = None,
    module_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the fixed MK1 universal conveyor for all selected candidates.

    Processes every selected candidate through the same five-module chain in
    the fixed order defined by :data:`FIXED_MK1_CONVEYOR_MODULES`.

    Args:
        selection_result: Structured selection gate result dict from
            ``selection_gate_v1``.  Must contain a ``selected_candidates``
            list.
        source_video_path: Path to the source video file.  Passed as the
            initial ``input_path`` to ``render_clip_v1``.
        job_metadata: Optional job-level metadata.  ``job_id`` is resolved
            from here first, then from ``selection_result``.
        config: Optional conveyor-level configuration.  MK1 does not support
            config-based module reordering.
        directories: Post-processing directory paths (keys: ``clips``,
            ``metadata``, ``tmp``, ``post_processing_root``).  If omitted,
            paths default to ``None`` in module contexts.
        module_registry: Mapping from module name to module implementation
            (``PostProcessingModule`` instance or callable).  All five fixed
            modules must be present.  If any are missing the conveyor fails
            cleanly with ``CONVEYOR_FAILED``.

    Returns:
        Structured conveyor result dict with ``status`` equal to
        ``CONVEYOR_COMPLETE`` or ``CONVEYOR_FAILED``.
    """
    job_metadata = dict(job_metadata or {})
    config = dict(config or {})
    directories = dict(directories or {})
    registry: dict[str, Any] = dict(module_registry or {})

    # ------------------------------------------------------------------
    # 1. Validate selection_result input
    # ------------------------------------------------------------------
    validation_failure = _validate_selection_result(selection_result)
    if validation_failure is not None:
        return _conveyor_failed_result(
            job_id=job_metadata.get("job_id"),
            **validation_failure,
        )

    # ------------------------------------------------------------------
    # 2. Resolve job_id
    # ------------------------------------------------------------------
    job_id: str | None = job_metadata.get("job_id") or (
        selection_result.get("job_id") if isinstance(selection_result, dict) else None
    )
    if not job_id or not str(job_id).strip():
        return _conveyor_failed_result(
            job_id=None,
            error_code="missing_job_id",
            error_message=(
                "job_id could not be resolved from job_metadata or selection_result"
            ),
        )

    # ------------------------------------------------------------------
    # 3. Resolve ordered module name list
    #    Prefer conveyor_module_list from job_metadata (config-driven, Prompt 6A).
    #    Fall back to FIXED_MK1_CONVEYOR_MODULES for legacy jobs.
    # ------------------------------------------------------------------
    _cfg_module_list = job_metadata.get("conveyor_module_list")
    if isinstance(_cfg_module_list, list) and _cfg_module_list:
        resolved_module_names: list[str] = [str(m) for m in _cfg_module_list]
        # Unknown module names fail clearly for config-driven jobs.
        _unknown = [n for n in resolved_module_names if n not in registry]
        if _unknown:
            return _conveyor_failed_result(
                job_id=job_id,
                error_code="unknown_conveyor_module",
                error_message=(
                    f"config-specified conveyor module(s) not in registry: {_unknown}"
                ),
                extra={"unknown_modules": _unknown},
            )
    else:
        resolved_module_names = list(FIXED_MK1_CONVEYOR_MODULES)
        # Validate registry against fixed list for legacy jobs.
        missing_modules = [n for n in resolved_module_names if n not in registry]
        if missing_modules:
            return _conveyor_failed_result(
                job_id=job_id,
                error_code="missing_required_conveyor_module",
                error_message=(
                    f"missing required conveyor modules: {missing_modules}"
                ),
                extra={"missing_modules": missing_modules},
            )

    # ------------------------------------------------------------------
    # 4. Resolve ordered module list from registry
    # ------------------------------------------------------------------
    ordered_modules = [registry[name] for name in resolved_module_names]

    # ------------------------------------------------------------------
    # 5. Iterate over selected candidates
    # ------------------------------------------------------------------
    selected_candidates: list[dict[str, Any]] = selection_result.get(
        "selected_candidates", []
    )
    conveyor_warnings: list[str] = []
    conveyor_errors: list[str] = []

    if len(selected_candidates) == 0:
        conveyor_warnings.append("zero_selected_candidates")

    clip_results: list[dict[str, Any]] = []
    clips_passed = 0
    clips_failed = 0

    for candidate in selected_candidates:
        if not isinstance(candidate, dict):
            clip_result = _make_clip_fail_result(
                clip_id=None,
                source_candidate_id=None,
                selected_candidate=candidate,
                module_results=[],
                failed_module=None,
                failure_reason="invalid_candidate_shape: candidate is not a dict",
                warnings=[],
            )
            clip_results.append(clip_result)
            clips_failed += 1
            continue

        candidate_id: str | None = candidate.get("candidate_id")
        clip_id = _make_clip_id(job_id, candidate_id, rank=candidate.get("rank"))

        context = make_module_context(
            job_id=job_id,
            candidate_id=candidate_id,
            source_video_path=source_video_path,
            working_dir=directories.get("post_processing_root"),
            clip_dir=directories.get("clips"),
            metadata_dir=directories.get("metadata"),
            tmp_dir=directories.get("tmp"),
            config=config,
            selection_result=copy.deepcopy(selection_result),
            selected_candidate=candidate,
        )
        context["clip_id"] = clip_id
        context["source_candidate"] = copy.deepcopy(
            candidate.get("source_candidate") or candidate
        )
        context["post_processing_dirs"] = copy.deepcopy(directories)
        # Propagate execution provenance to each clip's module context so the
        # metadata writer can include it in per-clip metadata JSON.
        execution_context = job_metadata.get("execution_context")
        if execution_context is not None:
            context["execution_context"] = dict(execution_context)

        chain_result = run_module_chain(
            ordered_modules,
            context,
            initial_input_path=source_video_path,
        )

        chain_passed = chain_result.get("status") == CHAIN_STATUS_PASS
        clip_status = CLIP_STATUS_PASS if chain_passed else CLIP_STATUS_FAIL

        if chain_passed:
            clips_passed += 1
            clip_result = _make_clip_pass_result(
                clip_id=clip_id,
                source_candidate_id=candidate_id,
                selected_candidate=candidate,
                module_results=chain_result.get("module_results", []),
                final_output_path=chain_result.get("final_output_path"),
                warnings=chain_result.get("warnings", []),
            )
        else:
            clips_failed += 1
            clip_result = _make_clip_fail_result(
                clip_id=clip_id,
                source_candidate_id=candidate_id,
                selected_candidate=candidate,
                module_results=chain_result.get("module_results", []),
                failed_module=chain_result.get("failed_module"),
                failure_reason=_extract_chain_failure_reason(chain_result),
                warnings=chain_result.get("warnings", []),
            )

        clip_results.append(clip_result)

    clips_attempted = clips_passed + clips_failed

    return {
        "schema_version": CONVEYOR_SCHEMA_VERSION,
        "post_processing_version": POST_PROCESSING_VERSION,
        "job_id": job_id,
        "status": CONVEYOR_STATUS_COMPLETE,
        "required_modules": list(FIXED_MK1_CONVEYOR_MODULES),
        "clip_results": clip_results,
        "summary": {
            "selected_candidates_received": len(selected_candidates),
            "clips_attempted": clips_attempted,
            "clips_passed": clips_passed,
            "clips_failed": clips_failed,
        },
        "warnings": conveyor_warnings,
        "errors": conveyor_errors,
    }


# ---------------------------------------------------------------------------
# Clip ID helpers
# ---------------------------------------------------------------------------


def _make_clip_id(
    job_id: str,
    candidate_id: str | None,
    *,
    rank: Any = None,
) -> str:
    """Return a deterministic clip ID.

    Format:  ``{job_id}_{candidate_id}``
    Falls back to ``{job_id}_clip_{rank}`` if candidate_id is missing.
    Falls back to ``{job_id}_clip_unknown`` if both are missing.
    """
    if candidate_id and str(candidate_id).strip():
        return f"{job_id}_{candidate_id}"
    if rank is not None:
        return f"{job_id}_clip_{rank}"
    return f"{job_id}_clip_unknown"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_selection_result(
    selection_result: Any,
) -> dict[str, str] | None:
    """Validate the selection_result shape.

    Returns ``None`` on success, or an error dict with ``error_code`` and
    ``error_message`` on failure.
    """
    if not isinstance(selection_result, dict):
        return {
            "error_code": "invalid_selection_result",
            "error_message": (
                f"selection_result must be a dict, got "
                f"{type(selection_result).__name__}"
            ),
        }

    if "selected_candidates" not in selection_result:
        return {
            "error_code": "missing_selected_candidates",
            "error_message": (
                "selection_result is missing required key: selected_candidates"
            ),
        }

    selected = selection_result["selected_candidates"]
    if not isinstance(selected, list):
        return {
            "error_code": "invalid_selected_candidates",
            "error_message": (
                f"selection_result.selected_candidates must be a list, got "
                f"{type(selected).__name__}"
            ),
        }

    return None


def _find_missing_modules(registry: dict[str, Any]) -> list[str]:
    """Return the names of required modules absent from the registry."""
    return [name for name in FIXED_MK1_CONVEYOR_MODULES if name not in registry]


# ---------------------------------------------------------------------------
# Chain result helpers
# ---------------------------------------------------------------------------


def _extract_chain_failure_reason(chain_result: dict[str, Any]) -> str | None:
    """Extract a human-readable failure reason from a chain result."""
    errors = chain_result.get("errors") or []
    if errors and isinstance(errors[0], dict):
        return errors[0].get("reason")
    return None


# ---------------------------------------------------------------------------
# Per-clip result builders
# ---------------------------------------------------------------------------


def _make_clip_pass_result(
    *,
    clip_id: str | None,
    source_candidate_id: str | None,
    selected_candidate: dict[str, Any],
    module_results: list[dict[str, Any]],
    final_output_path: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "clip_id": clip_id,
        "source_candidate_id": source_candidate_id,
        "status": CLIP_STATUS_PASS,
        "selected_candidate": selected_candidate,
        "module_results": module_results,
        "final_output_path": final_output_path,
        "failed_module": None,
        "failure_reason": None,
        "warnings": list(warnings),
    }


def _make_clip_fail_result(
    *,
    clip_id: str | None,
    source_candidate_id: str | None,
    selected_candidate: Any,
    module_results: list[dict[str, Any]],
    failed_module: str | None,
    failure_reason: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    return {
        "clip_id": clip_id,
        "source_candidate_id": source_candidate_id,
        "status": CLIP_STATUS_FAIL,
        "selected_candidate": selected_candidate,
        "module_results": module_results,
        "final_output_path": None,
        "failed_module": failed_module,
        "failure_reason": failure_reason,
        "warnings": list(warnings),
    }


# ---------------------------------------------------------------------------
# Conveyor-level failure result builder
# ---------------------------------------------------------------------------


def _conveyor_failed_result(
    *,
    job_id: str | None,
    error_code: str,
    error_message: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured CONVEYOR_FAILED result."""
    result: dict[str, Any] = {
        "schema_version": CONVEYOR_SCHEMA_VERSION,
        "post_processing_version": POST_PROCESSING_VERSION,
        "job_id": job_id,
        "status": CONVEYOR_STATUS_FAILED,
        "required_modules": list(FIXED_MK1_CONVEYOR_MODULES),
        "clip_results": [],
        "summary": {
            "selected_candidates_received": 0,
            "clips_attempted": 0,
            "clips_passed": 0,
            "clips_failed": 0,
        },
        "warnings": [],
        "errors": [
            {
                "code": error_code,
                "message": error_message,
            }
        ],
    }
    if extra:
        result.update(extra)
    return result
