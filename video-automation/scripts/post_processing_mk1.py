"""Post-processing MK1 entrypoint and input contract.

Establishes the clean boundary between:

    processing_mk1
        ↓
    raw_candidate_pool.json
        ↓
    post_processing_mk1

This module loads and validates the raw candidate pool produced by processing,
resolves the source video path, creates the post-processing output directory
structure, and returns a structured READY_FOR_SELECTION result by default.
When explicitly requested it runs the deterministic MK1 post-processing flow:
selection gate → fixed universal conveyor → post-processing report → local
output-funnel handoff artifact.

This module deliberately does NOT:
- rediscover clips
- implement new selection logic
- implement post-processing modules
- perform AI/LLM inspection
- upload, schedule, or distribute clips
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import processing_contracts as contracts
from post_processing_conveyor import (
    CONVEYOR_STATUS_FAILED,
    run_fixed_mk1_universal_conveyor,
)
from post_processing_report_v1 import (
    build_post_processing_report,
    write_post_processing_report,
)
from processing_contracts import ProcessingContractValidationError
from selection_gate_v1 import (
    SELECTION_RESULT_FILENAME,
    STATUS_SELECTION_COMPLETE,
    STATUS_SELECTION_FAILED,
    run_selection_gate_v1,
)

POST_PROCESSING_VERSION = "post_processing_mk1_v1"
POST_PROCESSING_ENTRYPOINT_SCHEMA_VERSION = "post_processing_entrypoint_result_v1"
OUTPUT_FUNNEL_HANDOFF_SCHEMA_VERSION = "output_funnel_handoff_v1"

STATUS_READY_FOR_SELECTION = "READY_FOR_SELECTION"
STATUS_INPUT_CONTRACT_FAILED = "INPUT_CONTRACT_FAILED"
STATUS_POST_PROCESSING_COMPLETE = "POST_PROCESSING_COMPLETE"
STATUS_POST_PROCESSING_PARTIAL = "POST_PROCESSING_PARTIAL"
STATUS_POST_PROCESSING_FAILED = "POST_PROCESSING_FAILED"
STATUS_NO_CANDIDATES_SELECTED = "NO_CANDIDATES_SELECTED"
STATUS_NO_FINISHED_CLIPS = "NO_FINISHED_CLIPS"
STATUS_READY_FOR_OUTPUT_FUNNEL = "READY_FOR_OUTPUT_FUNNEL"


class PostProcessingInputContractError(RuntimeError):
    """Raised for cleanly classified post-processing input contract failures.

    Use ``code`` to programmatically identify the failure category and
    ``message`` for a human-readable description.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def run_post_processing_mk1(
    raw_candidate_pool_path: str,
    *,
    source_video_path: str | None = None,
    job_metadata: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    output_root: str | None = None,
) -> dict[str, Any]:
    """Load, validate, and stage or execute MK1 post-processing.

    Returns a structured result dict with ``status`` equal to
    ``READY_FOR_SELECTION`` on staging success when execution is disabled, or a
    post-processing completion/failure status when
    ``config["execute_post_processing"]`` is true. Controlled input failures
    return ``INPUT_CONTRACT_FAILED``.

    Source video path resolution order:
    1. ``source_video_path`` argument if provided.
    2. ``source_video_path`` field from the raw candidate pool.
    3. Fail cleanly if neither yields a usable path.

    This function never mutates ``raw_candidate_pool.json`` and never uploads,
    schedules, or distributes clips.

    Args:
        raw_candidate_pool_path: Path to the ``raw_candidate_pool.json``
            produced by ``processing_mk1``.
        source_video_path: Explicit path to the source video.  If omitted
            the pool's ``source_video_path`` field is used as a fallback.
        job_metadata: Optional mapping of job-level metadata (e.g.
            ``{"job_id": "...", "funnel_id": "..."}``.  ``job_id`` is
            resolved from here first, then from the pool.
        config: Optional post-processing configuration mapping. Set
            ``execute_post_processing`` to true to run selection, conveyor,
            report writing, and local output-funnel handoff artifact writing.
        output_root: Root directory under which post-processing output
            directories will be created.  Defaults to the current working
            directory if not provided.

    Returns:
        Structured result dict.
    """
    job_metadata = dict(job_metadata or {})
    config = dict(config or {})

    # ------------------------------------------------------------------
    # 1. Load and validate the raw candidate pool
    # ------------------------------------------------------------------
    pool, load_error = _load_raw_candidate_pool(raw_candidate_pool_path)
    if load_error is not None:
        return _failure_result(
            job_id=job_metadata.get("job_id"),
            raw_candidate_pool_path=raw_candidate_pool_path,
            **load_error,
        )

    # ------------------------------------------------------------------
    # 2. Resolve job_id
    # ------------------------------------------------------------------
    job_id = job_metadata.get("job_id") or (pool.get("job_id") if pool else None)
    if not job_id or not str(job_id).strip():
        return _failure_result(
            job_id=None,
            raw_candidate_pool_path=raw_candidate_pool_path,
            error_code="missing_job_id",
            error_message=(
                "job_id could not be resolved from job_metadata or raw_candidate_pool"
            ),
        )

    # ------------------------------------------------------------------
    # 3. Resolve and validate source video path
    # ------------------------------------------------------------------
    resolved_video_path = source_video_path or (
        pool.get("source_video_path") if pool else None
    )
    if not resolved_video_path:
        return _failure_result(
            job_id=job_id,
            raw_candidate_pool_path=raw_candidate_pool_path,
            error_code="missing_source_video",
            error_message=(
                "source_video_path was not provided and is not present in "
                "raw_candidate_pool"
            ),
        )

    video_error = _validate_source_video(resolved_video_path)
    if video_error is not None:
        return _failure_result(
            job_id=job_id,
            raw_candidate_pool_path=raw_candidate_pool_path,
            **video_error,
        )

    # ------------------------------------------------------------------
    # 4. Resolve output root
    # ------------------------------------------------------------------
    effective_output_root = (
        output_root or config.get("output_root") or os.getcwd()
    )

    # ------------------------------------------------------------------
    # 5. Create post-processing output directory structure
    # ------------------------------------------------------------------
    directories = _create_post_processing_directories(effective_output_root)

    # ------------------------------------------------------------------
    # 6. Count candidates and accumulate warnings
    # ------------------------------------------------------------------
    candidates: list = pool.get("candidates", [])
    raw_candidates_received = len(candidates)
    warnings: list[str] = []
    if raw_candidates_received == 0:
        warnings.append("zero_candidates_received")

    # ------------------------------------------------------------------
    # 7. Resolve the intended post-processing report path
    # ------------------------------------------------------------------
    post_processing_report_path = os.path.join(
        directories["reports"], "post_processing_report.json"
    )

    ready_result = {
        "schema_version": POST_PROCESSING_ENTRYPOINT_SCHEMA_VERSION,
        "post_processing_version": POST_PROCESSING_VERSION,
        "job_id": job_id,
        "status": STATUS_READY_FOR_SELECTION,
        "raw_candidate_pool_path": str(raw_candidate_pool_path),
        "source_video_path": str(resolved_video_path),
        "output_root": str(effective_output_root),
        "directories": directories,
        "post_processing_report_path": post_processing_report_path,
        "raw_candidates_received": raw_candidates_received,
        "job_metadata": job_metadata,
        "config": config,
        "warnings": warnings,
        "errors": [],
    }

    if not bool(config.get("execute_post_processing", False)):
        return ready_result

    return _execute_post_processing_flow(
        ready_result=ready_result,
        raw_candidate_pool=pool,
        raw_candidate_pool_path=str(raw_candidate_pool_path),
        source_video_path=str(resolved_video_path),
        job_metadata=job_metadata,
        config=config,
        directories=directories,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_raw_candidate_pool(
    pool_path: str,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Load and validate the raw candidate pool from disk.

    Returns ``(pool_dict, None)`` on success, or ``(None, error_dict)``
    on any failure.  ``error_dict`` always contains ``error_code`` and
    ``error_message`` keys.

    The file is read as raw bytes so the caller can compare them for
    immutability checks without a second disk read.
    """
    if not os.path.exists(pool_path):
        return None, {
            "error_code": "missing_raw_candidate_pool",
            "error_message": f"raw_candidate_pool.json not found: {pool_path}",
        }

    if not os.path.isfile(pool_path):
        return None, {
            "error_code": "invalid_raw_candidate_pool_path",
            "error_message": (
                f"raw_candidate_pool path is not a file: {pool_path}"
            ),
        }

    try:
        raw_bytes = Path(pool_path).read_bytes()
        pool: Any = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        return None, {
            "error_code": "invalid_raw_candidate_pool_json",
            "error_message": (
                f"raw_candidate_pool.json contains invalid JSON: {exc}"
            ),
        }
    except OSError as exc:
        return None, {
            "error_code": "raw_candidate_pool_read_error",
            "error_message": f"Could not read raw_candidate_pool.json: {exc}",
        }

    try:
        contracts.validate_raw_candidate_pool(pool)
    except ProcessingContractValidationError as exc:
        return None, {
            "error_code": "invalid_raw_candidate_pool_schema",
            "error_message": str(exc),
        }

    return pool, None


def _validate_source_video(video_path: str) -> dict[str, str] | None:
    """Check that the source video path exists and is a regular file.

    Returns ``None`` on success, or an error dict on failure.
    """
    if not os.path.exists(video_path):
        return {
            "error_code": "missing_source_video",
            "error_message": (
                f"source_video_path does not exist: {video_path}"
            ),
        }
    if not os.path.isfile(video_path):
        return {
            "error_code": "source_video_not_a_file",
            "error_message": (
                f"source_video_path is not a regular file: {video_path}"
            ),
        }
    return None


def _create_post_processing_directories(output_root: str) -> dict[str, str]:
    """Create the post-processing directory structure under ``output_root``.

    Returns a mapping of logical name → absolute path for each directory.
    All directories are created with ``exist_ok=True``.
    """
    post_processing_root = os.path.join(output_root, "post_processing")
    directories: dict[str, str] = {
        "post_processing_root": post_processing_root,
        "selection": os.path.join(post_processing_root, "selection"),
        "clips": os.path.join(post_processing_root, "clips"),
        "metadata": os.path.join(post_processing_root, "metadata"),
        "reports": os.path.join(post_processing_root, "reports"),
        "tmp": os.path.join(post_processing_root, "tmp"),
    }
    for path in directories.values():
        os.makedirs(path, exist_ok=True)
    return directories


def _execute_post_processing_flow(
    *,
    ready_result: dict[str, Any],
    raw_candidate_pool: dict[str, Any],
    raw_candidate_pool_path: str,
    source_video_path: str,
    job_metadata: dict[str, Any],
    config: dict[str, Any],
    directories: dict[str, str],
    warnings: list[str],
) -> dict[str, Any]:
    """Run selection, conveyor, report writing, and local handoff artifact."""
    ready_result = {**ready_result, "config": _public_config(config)}
    job_id = str(ready_result["job_id"])
    processing_report_path = _resolve_processing_report_path(job_metadata, config)
    selection_result_path = os.path.join(
        directories["selection"], SELECTION_RESULT_FILENAME
    )
    post_processing_report_path = ready_result["post_processing_report_path"]
    output_funnel_handoff_path = os.path.join(
        directories["reports"], "output_funnel_handoff.json"
    )

    selection_config = _resolve_selection_config(config)
    selection_result: dict[str, Any] | None = None
    conveyor_result: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = []

    try:
        selection_result = run_selection_gate_v1(
            raw_candidate_pool,
            job_metadata=job_metadata,
            config=selection_config,
        )
    except Exception as exc:
        selection_result = {
            "schema_version": "selection_gate_v1",
            "job_id": job_id,
            "selection_mode": selection_config.get("selection_mode", "balanced"),
            "status": STATUS_SELECTION_FAILED,
            "selected_candidates": [],
            "rejected_candidates": [],
            "reserve_candidates": [],
            "selection_summary": {
                "raw_candidates_received": len(raw_candidate_pool.get("candidates") or []),
                "eligible_count": 0,
                "selected_count": 0,
                "rejected_count": 0,
                "reserve_count": 0,
            },
            "config_used": {},
            "warnings": [],
            "errors": [
                {
                    "code": "selection_gate_exception",
                    "message": f"selection gate raised unexpectedly: {exc}",
                }
            ],
        }

    try:
        _write_json(selection_result_path, selection_result)
    except OSError as exc:
        errors.append(
            {
                "code": "selection_result_write_failed",
                "message": f"could not write selection_result.json: {exc}",
            }
        )
        return _execution_failure_result(
            ready_result=ready_result,
            status=STATUS_POST_PROCESSING_FAILED,
            raw_candidate_pool_path=raw_candidate_pool_path,
            processing_report_path=processing_report_path,
            selection_result_path=selection_result_path,
            post_processing_report_path=post_processing_report_path,
            output_funnel_handoff_path=None,
            selection_result=selection_result,
            report=None,
            warnings=warnings,
            errors=errors,
        )

    selected_candidates = list(selection_result.get("selected_candidates") or [])
    if selection_result.get("status") != STATUS_SELECTION_COMPLETE:
        errors.extend(_normalise_error_list(selection_result.get("errors")))
        status = STATUS_POST_PROCESSING_FAILED
    elif not selected_candidates:
        status = STATUS_NO_CANDIDATES_SELECTED
    else:
        module_registry = config.get("module_registry")
        if module_registry is None:
            module_registry = _make_default_module_registry()

        conveyor_result = run_fixed_mk1_universal_conveyor(
            selection_result,
            source_video_path=source_video_path,
            job_metadata=job_metadata,
            config=_resolve_conveyor_config(config),
            directories=directories,
            module_registry=module_registry,
        )
        if conveyor_result.get("status") == CONVEYOR_STATUS_FAILED:
            errors.extend(_normalise_error_list(conveyor_result.get("errors")))

        status = _resolve_execution_status(selection_result, conveyor_result)

    try:
        report = build_post_processing_report(
            job_id=job_id,
            selection_result=selection_result,
            conveyor_result=conveyor_result,
            raw_candidate_pool=raw_candidate_pool,
            raw_candidate_pool_path=raw_candidate_pool_path,
            source_video_path=source_video_path,
            selection_result_path=selection_result_path,
            report_path=post_processing_report_path,
            warnings=warnings,
            diagnostics={
                "processing_report_path": processing_report_path,
                "conveyor_status": (
                    conveyor_result.get("status") if conveyor_result else None
                ),
            },
        )
        write_post_processing_report(report, post_processing_report_path)
    except Exception as exc:
        errors.append(
            {
                "code": "post_processing_report_write_failed",
                "message": f"could not write post_processing_report.json: {exc}",
            }
        )
        return _execution_failure_result(
            ready_result=ready_result,
            status=STATUS_POST_PROCESSING_FAILED,
            raw_candidate_pool_path=raw_candidate_pool_path,
            processing_report_path=processing_report_path,
            selection_result_path=selection_result_path,
            post_processing_report_path=post_processing_report_path,
            output_funnel_handoff_path=None,
            selection_result=selection_result,
            report=report,
            warnings=warnings,
            errors=errors,
        )

    finished_clip_paths = list(report.get("finished_clip_paths") or [])
    per_clip_metadata_paths = list(report.get("per_clip_metadata_paths") or [])
    failed_clips = list(report.get("failed_clips") or [])
    rejected_candidates = list(report.get("rejected_candidates") or [])
    reserve_candidates = list(report.get("reserve_candidates_list") or [])

    handoff_path: str | None = None
    try:
        handoff = _build_output_funnel_handoff(
            job_id=job_id,
            funnel_id=_resolve_funnel_id(raw_candidate_pool, job_metadata, config),
            finished_clip_paths=finished_clip_paths,
            per_clip_metadata_paths=per_clip_metadata_paths,
            post_processing_report_path=post_processing_report_path,
            processing_report_path=processing_report_path,
            raw_candidate_pool_path=raw_candidate_pool_path,
        )
        _write_json(output_funnel_handoff_path, handoff)
        handoff_path = output_funnel_handoff_path
    except OSError as exc:
        errors.append(
            {
                "code": "output_funnel_handoff_write_failed",
                "message": f"could not write output_funnel_handoff.json: {exc}",
            }
        )
        if status == STATUS_POST_PROCESSING_COMPLETE:
            status = STATUS_POST_PROCESSING_PARTIAL

    return {
        **ready_result,
        "status": status,
        "raw_candidate_pool_path": raw_candidate_pool_path,
        "processing_report_path": processing_report_path,
        "selection_result_path": selection_result_path,
        "selection_result": selection_result,
        "post_processing_report_path": post_processing_report_path,
        "post_processing_report": report,
        "output_funnel_handoff_path": handoff_path,
        "conveyor_result": conveyor_result,
        "finished_clip_paths": finished_clip_paths,
        "per_clip_metadata_paths": per_clip_metadata_paths,
        "failed_clips": failed_clips,
        "rejected_candidates": rejected_candidates,
        "reserve_candidates": reserve_candidates,
        "warnings": _dedupe_strings(
            warnings
            + list(selection_result.get("warnings") or [])
            + (list(conveyor_result.get("warnings") or []) if conveyor_result else [])
        ),
        "errors": errors,
    }


def _make_default_module_registry() -> dict[str, Any]:
    """Return fresh real MK1 module instances for a conveyor run."""
    from intelligent_captions_v1 import get_intelligent_captions_v1_module
    from metadata_writer_v1 import get_metadata_writer_v1_module
    from platform_safe_format_v1 import get_platform_safe_format_v1_module
    from render_clip_v1 import get_render_clip_v1_module
    from validation_v1 import get_validation_v1_module

    return {
        "render_clip_v1": get_render_clip_v1_module(),
        "platform_safe_format_v1": get_platform_safe_format_v1_module(),
        "intelligent_captions_v1": get_intelligent_captions_v1_module(),
        "validation_v1": get_validation_v1_module(),
        "metadata_writer_v1": get_metadata_writer_v1_module(),
    }


def _resolve_selection_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("selection_config")
    if isinstance(raw, dict):
        return dict(raw)
    return {
        key: value
        for key, value in config.items()
        if key
        in {
            "selection_mode",
            "max_clips",
            "reserve_count",
            "min_overall_potential",
            "min_confidence",
            "min_duration_sec",
            "max_duration_sec",
            "respect_candidate_warnings",
            "respect_transcript_quality_flags",
            "blocking_warnings",
            "blocking_transcript_quality_flags",
            "allow_reserve_candidates",
        }
    }


def _resolve_conveyor_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("conveyor_config")
    if isinstance(raw, dict):
        return dict(raw)
    return {
        key: value
        for key, value in config.items()
        if key
        not in {
            "execute_post_processing",
            "module_registry",
            "selection_config",
            "conveyor_config",
            "processing_report_path",
            "output_root",
        }
    }


def _public_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in config.items()
        if key != "module_registry"
    }


def _resolve_processing_report_path(
    job_metadata: dict[str, Any],
    config: dict[str, Any],
) -> str | None:
    value = job_metadata.get("processing_report_path") or config.get(
        "processing_report_path"
    )
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_funnel_id(
    raw_candidate_pool: dict[str, Any],
    job_metadata: dict[str, Any],
    config: dict[str, Any],
) -> str | None:
    value = (
        job_metadata.get("funnel_id")
        or config.get("funnel_id")
        or raw_candidate_pool.get("funnel_id")
    )
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_execution_status(
    selection_result: dict[str, Any],
    conveyor_result: dict[str, Any] | None,
) -> str:
    selected_count = len(selection_result.get("selected_candidates") or [])
    if selected_count == 0:
        return STATUS_NO_CANDIDATES_SELECTED
    if conveyor_result is None:
        return STATUS_POST_PROCESSING_FAILED
    if conveyor_result.get("status") == CONVEYOR_STATUS_FAILED:
        return STATUS_POST_PROCESSING_FAILED

    summary = conveyor_result.get("summary") or {}
    passed = int(summary.get("clips_passed") or 0)
    failed = int(summary.get("clips_failed") or 0)

    if passed > 0 and failed > 0:
        return STATUS_POST_PROCESSING_PARTIAL
    if passed > 0:
        return STATUS_POST_PROCESSING_COMPLETE
    if failed > 0:
        return STATUS_NO_FINISHED_CLIPS
    return STATUS_NO_CANDIDATES_SELECTED


def _build_output_funnel_handoff(
    *,
    job_id: str,
    funnel_id: str | None,
    finished_clip_paths: list[str],
    per_clip_metadata_paths: list[str],
    post_processing_report_path: str,
    processing_report_path: str | None,
    raw_candidate_pool_path: str,
) -> dict[str, Any]:
    return {
        "schema_version": OUTPUT_FUNNEL_HANDOFF_SCHEMA_VERSION,
        "job_id": job_id,
        "funnel_id": funnel_id,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "finished_clip_paths": list(finished_clip_paths),
        "per_clip_metadata_paths": list(per_clip_metadata_paths),
        "post_processing_report_path": post_processing_report_path,
        "processing_report_path": processing_report_path,
        "raw_candidate_pool_path": raw_candidate_pool_path,
        "status": STATUS_READY_FOR_OUTPUT_FUNNEL,
    }


def _write_json(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    Path(path).write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def _normalise_error_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    errors: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            errors.append(dict(item))
        else:
            errors.append({"code": "error", "message": str(item)})
    return errors


def _dedupe_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value)
        if text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _execution_failure_result(
    *,
    ready_result: dict[str, Any],
    status: str,
    raw_candidate_pool_path: str,
    processing_report_path: str | None,
    selection_result_path: str | None,
    post_processing_report_path: str,
    output_funnel_handoff_path: str | None,
    selection_result: dict[str, Any] | None,
    report: dict[str, Any] | None,
    warnings: list[str],
    errors: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        **ready_result,
        "status": status,
        "raw_candidate_pool_path": raw_candidate_pool_path,
        "processing_report_path": processing_report_path,
        "selection_result_path": selection_result_path,
        "selection_result": selection_result,
        "post_processing_report_path": post_processing_report_path,
        "post_processing_report": report,
        "output_funnel_handoff_path": output_funnel_handoff_path,
        "finished_clip_paths": list((report or {}).get("finished_clip_paths") or []),
        "per_clip_metadata_paths": list(
            (report or {}).get("per_clip_metadata_paths") or []
        ),
        "failed_clips": list((report or {}).get("failed_clips") or []),
        "rejected_candidates": list((report or {}).get("rejected_candidates") or []),
        "reserve_candidates": list((report or {}).get("reserve_candidates_list") or []),
        "warnings": list(warnings),
        "errors": list(errors),
    }


def _failure_result(
    *,
    job_id: str | None,
    raw_candidate_pool_path: str,
    error_code: str,
    error_message: str,
) -> dict[str, Any]:
    """Build a structured INPUT_CONTRACT_FAILED result."""
    return {
        "schema_version": POST_PROCESSING_ENTRYPOINT_SCHEMA_VERSION,
        "post_processing_version": POST_PROCESSING_VERSION,
        "job_id": job_id,
        "status": STATUS_INPUT_CONTRACT_FAILED,
        "raw_candidate_pool_path": str(raw_candidate_pool_path),
        "error_code": error_code,
        "error_message": error_message,
        "warnings": [],
        "errors": [
            {
                "code": error_code,
                "message": error_message,
            }
        ],
    }
