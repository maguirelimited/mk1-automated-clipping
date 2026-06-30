"""Post-processing MK1 entrypoint and input contract.

Establishes the clean boundary between:

    processing_mk1
        ↓
    raw_candidate_pool.json
        ↓
    post_processing_mk1

This module loads and validates the raw candidate pool produced by processing,
resolves the source video path, creates the post-processing output directory
structure, and returns a structured READY_FOR_SELECTION result.

This module deliberately does NOT:
- rediscover clips
- select final clips
- render clips
- run the universal conveyor
- implement selection_gate_v1
- perform AI/LLM inspection
- register output funnels
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import processing_contracts as contracts
from processing_contracts import ProcessingContractValidationError

POST_PROCESSING_VERSION = "post_processing_mk1_v1"
POST_PROCESSING_ENTRYPOINT_SCHEMA_VERSION = "post_processing_entrypoint_result_v1"

STATUS_READY_FOR_SELECTION = "READY_FOR_SELECTION"
STATUS_INPUT_CONTRACT_FAILED = "INPUT_CONTRACT_FAILED"


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
    """Load, validate, and stage the raw candidate pool for post-processing.

    Returns a structured result dict with ``status`` equal to
    ``READY_FOR_SELECTION`` on success, or ``INPUT_CONTRACT_FAILED`` for
    any controlled input failure.

    Source video path resolution order:
    1. ``source_video_path`` argument if provided.
    2. ``source_video_path`` field from the raw candidate pool.
    3. Fail cleanly if neither yields a usable path.

    This function never mutates ``raw_candidate_pool.json``.
    It does not rediscover, select, or render clips.

    Args:
        raw_candidate_pool_path: Path to the ``raw_candidate_pool.json``
            produced by ``processing_mk1``.
        source_video_path: Explicit path to the source video.  If omitted
            the pool's ``source_video_path`` field is used as a fallback.
        job_metadata: Optional mapping of job-level metadata (e.g.
            ``{"job_id": "...", "funnel_id": "..."}``.  ``job_id`` is
            resolved from here first, then from the pool.
        config: Optional post-processing configuration mapping.
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
    # 7. Return structured READY_FOR_SELECTION result
    # ------------------------------------------------------------------
    return {
        "schema_version": POST_PROCESSING_ENTRYPOINT_SCHEMA_VERSION,
        "post_processing_version": POST_PROCESSING_VERSION,
        "job_id": job_id,
        "status": STATUS_READY_FOR_SELECTION,
        "raw_candidate_pool_path": str(raw_candidate_pool_path),
        "source_video_path": str(resolved_video_path),
        "output_root": str(effective_output_root),
        "directories": directories,
        "raw_candidates_received": raw_candidates_received,
        "job_metadata": job_metadata,
        "config": config,
        "warnings": warnings,
        "errors": [],
    }


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
